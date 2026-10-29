//! Host's operations on a real machine: tests/test_orchestrator.py's TestHost, TestHostGit, TestHostSnapshot,
//! TestAtomicWrite, TestRunFiles, TestHostPullRequest and TestChildEnvironment, ported case for case, against real git
//! in temporary repositories, with gh, date and ssh played by scripts on the Host's PATH.

use std::ffi::OsString;
use std::os::unix::fs::PermissionsExt;

use super::*;
use crate::interrupt::Interrupt;
use crate::test_support::{ended, oracle, raise_once_written, TempDir, HEARS_SIGINT};

/// A token-shaped string, built so that no scanner takes this file for a leak.
pub(crate) const FAKE_TOKEN: &str = concat!("ghp_", "Zq7Zq7Zq7Zq7", "Zq7Zq7Zq7Zq7", "Zq7Zq7Zq7Zq7");

// -- What the tests share --------------------------------------------------------------------------------------------

/// The engine's environment, with dir first on PATH and each of set given: what a test's Host starts its commands in.
pub(crate) fn environment(dir: Option<&str>, set: &[(&str, &str)]) -> Vec<(OsString, OsString)> {
    let mut vars: Vec<(OsString, OsString)> =
        std::env::vars_os().filter(|(k, _)| !set.iter().any(|(name, _)| k == name)).collect();
    if let Some(dir) = dir {
        let path = std::env::var("PATH").unwrap_or_default();
        vars.retain(|(k, _)| k != "PATH");
        vars.push(("PATH".into(), format!("{dir}:{path}").into()));
    }
    vars.extend(set.iter().map(|(k, v)| (OsString::from(k), OsString::from(v))));
    vars
}

/// A Host on this machine whose commands find the tools in dir first.
pub(crate) fn host_with_tools(dir: &str) -> ProcessHost {
    ProcessHost::in_environment(environment(Some(dir), &[]).into_iter(), &Interrupt::new())
}

/// Write an executable script named name into dir.
pub(crate) fn tool(dir: &str, name: &str, script: &str) {
    std::fs::create_dir_all(dir).expect("the tools' directory");
    let path = format!("{dir}/{name}");
    std::fs::write(&path, script).expect("the script");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).expect("an executable script");
}

/// Write text to the file at path, making its directories.
pub(crate) fn put(path: &str, text: &str) {
    if let Some((dir, _)) = path.rsplit_once('/') {
        std::fs::create_dir_all(dir).expect("the file's directory");
    }
    std::fs::write(path, text).expect("the file");
}

/// The text of the file at path.
pub(crate) fn read(path: &str) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{path}: {e}"))
}

/// Set the file's modification time, in seconds since the epoch.
pub(crate) fn set_mtime(path: &str, seconds: i64) {
    let out = std::process::Command::new("touch").args(["-d", &format!("@{seconds}"), path]).output().expect("touch");
    assert!(out.status.success(), "touch {path}: {}", String::from_utf8_lossy(&out.stderr));
}

/// A git repository of its own, on branch main, configured to commit as `t <t@example.com>` unsigned whatever the
/// user's git configuration says, as tests/gitrepo.py's RepositoryTest makes it; its base commit holds base_files
/// unless they are none.
pub(crate) struct Repo {
    /// Holds the repository and outside; removes both when dropped.
    pub _tmp: TempDir,
    /// The repository's top level.
    pub path: String,
    /// An empty directory that is not inside the repository, for what a test keeps out of the tree.
    pub outside: String,
    /// The Host the tests run git through.
    pub host: ProcessHost,
    /// The base commit; None when base_files are none.
    pub base: Option<String>,
}

impl Repo {
    pub fn new(base_files: &[(&str, &str)]) -> Repo {
        Repo::with_host(base_files, ProcessHost::local(&Interrupt::new()))
    }

    /// new, with host as the Host.
    pub fn with_host(base_files: &[(&str, &str)], host: ProcessHost) -> Repo {
        let tmp = TempDir::new();
        let (path, outside) = (tmp.join("repo"), tmp.join("outside"));
        std::fs::create_dir_all(&outside).expect("outside");
        host.git(&tmp.path(), &["init", "--quiet", "-b", "main", &path]).expect("git init");
        let mut repo = Repo { _tmp: tmp, path, outside, host, base: None };
        for (key, value) in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")] {
            repo.git(&["config", key, value]);
        }
        if !base_files.is_empty() {
            repo.base = Some(repo.commit(base_files, "base"));
        }
        repo
    }

    /// git args in the repository's standard output; panics when git fails.
    pub fn git(&self, args: &[&str]) -> String {
        self.host.git(&self.path, args).unwrap_or_else(|e| panic!("{e}"))
    }

    /// Write text to name, relative to the repository.
    pub fn put(&self, name: &str, text: &str) {
        put(&format!("{}/{name}", self.path), text);
    }

    /// Put files, then commit every change in the working tree with message; the new commit.
    pub fn commit(&self, files: &[(&str, &str)], message: &str) -> String {
        for (name, text) in files {
            self.put(name, text);
        }
        self.git(&["add", "-A"]);
        self.git(&["commit", "--quiet", "-m", message]);
        strip(&self.git(&["rev-parse", "HEAD"])).to_string()
    }

    pub fn base(&self) -> &str {
        self.base.as_deref().expect("a base commit")
    }
}

/// An ssh played by a script on the Host's PATH: it records each call's arguments, one file per call, NUL-separated,
/// and runs the command it was given with sh, here, as sshd would run it there.
struct FakeSsh {
    dir: TempDir,
    host: ProcessHost,
}

impl FakeSsh {
    /// A host over this fake ssh to remote-host, in the engine's environment with set added.
    fn new(set: &[(&str, &str)]) -> FakeSsh {
        let dir = TempDir::new();
        let bin = dir.join("bin");
        tool(
            &bin,
            "ssh",
            "#!/bin/sh\nd=$(dirname \"$0\")/calls; mkdir -p \"$d\"; n=$(ls \"$d\" | wc -l)\n\
             for a in \"$@\"; do printf '%s\\0' \"$a\"; done > \"$d/$n\"\n\
             eval \"last=\\${$#}\"\nexec sh -c \"$last\"\n",
        );
        let host = ProcessHost::in_environment(environment(Some(&bin), set).into_iter(), &Interrupt::new()).over_ssh("remote-host");
        FakeSsh { dir, host }
    }

    /// The arguments of each call of ssh, in order.
    fn calls(&self) -> Vec<Vec<String>> {
        let dir = self.dir.join("bin/calls");
        let n = std::fs::read_dir(&dir).map(|d| d.count()).unwrap_or(0);
        (0..n)
            .map(|i| {
                let bytes = std::fs::read(format!("{dir}/{i}")).expect("a call");
                let text = String::from_utf8(bytes).expect("UTF-8 arguments");
                std::iter::once("ssh").chain(text.split_terminator('\0')).map(String::from).collect()
            })
            .collect()
    }

    /// The arguments of the last call.
    fn last(&self) -> Vec<String> {
        self.calls().pop().expect("ssh was called")
    }
}

/// ["ssh", "-o", "BatchMode=yes"], the start of every command over SSH, before the sharing options.
const SSH: [&str; 3] = ["ssh", "-o", "BatchMode=yes"];

// -- TestHost --------------------------------------------------------------------------------------------------------

#[test]
fn ssh_wraps_command() {
    let home = TempDir::new();
    std::fs::create_dir_all(home.join("proj")).unwrap();
    let ssh = FakeSsh::new(&[("HOME", &home.path())]);
    let cwd = ssh.host.resolve_dir("~/proj").unwrap();
    assert_eq!(cwd, std::fs::canonicalize(home.join("proj")).unwrap().to_str().unwrap());
    let argv = ssh.last();
    assert_eq!(argv[..3], SSH);
    assert_eq!(argv[argv.len() - 2], "remote-host");
    assert!(argv[argv.len() - 1].contains("\"$HOME$1\""), "{argv:?}");
    assert!(argv[argv.len() - 1].ends_with(" _ /proj"), "{argv:?}");
}

#[test]
fn commands_over_ssh_share_one_connection_through_a_private_directory() {
    let ssh = FakeSsh::new(&[]);
    ssh.host.run(&["true"], None, DEFAULT_TIMEOUT).unwrap();
    ssh.host.run(&["true"], None, DEFAULT_TIMEOUT).unwrap();
    let [first, second] = <[Vec<String>; 2]>::try_from(ssh.calls()).unwrap();
    assert_eq!(first, second);
    let options = &first[3..first.len() - 2];
    let path = options[5].strip_prefix("ControlPath=").unwrap().strip_suffix("/%C").unwrap();
    let mut expected = vec![];
    for option in ["ControlMaster=auto", "ControlPersist=60", &format!("ControlPath={path}/%C")]
        .into_iter()
        .chain(["ServerAliveInterval=10", "ServerAliveCountMax=3"])
    {
        expected.extend(["-o".to_string(), option.to_string()]);
    }
    assert_eq!(options, expected);
    assert_eq!(std::fs::metadata(path).unwrap().permissions().mode() & 0o7777, 0o700);
    // The last of the hosts that share the directory removes it.
    let copy = ssh.host.clone();
    drop(ssh);
    assert!(std::path::Path::new(path).exists());
    drop(copy);
    assert!(!std::path::Path::new(path).exists());
}

/// A short directory under /tmp, for an XDG_RUNTIME_DIR whose control sockets' paths ssh accepts.
fn runtime_dir() -> String {
    let made = std::process::Command::new("mktemp").args(["-d", "/tmp/o-XXXXXX"]).output().expect("mktemp");
    String::from_utf8(made.stdout).unwrap().trim().to_string()
}

#[test]
fn a_control_directory_that_has_gone_is_made_again() {
    let runtime = runtime_dir();
    let ssh = FakeSsh::new(&[("XDG_RUNTIME_DIR", &runtime)]);
    let first = ssh.host.ssh_sharing_options()[5].clone();
    assert!(first.starts_with(&format!("ControlPath={runtime}/octoflow-ssh-")), "{first}");
    let dir = first.strip_prefix("ControlPath=").unwrap().strip_suffix("/%C").unwrap().to_string();
    std::fs::remove_dir_all(&dir).unwrap();
    let again = ssh.host.ssh_sharing_options()[5].clone();
    assert_ne!(first, again);
    let made = again.strip_prefix("ControlPath=").unwrap().strip_suffix("/%C").unwrap().to_string();
    assert!(std::path::Path::new(&made).is_dir());
    drop(ssh);
    std::fs::remove_dir_all(&runtime).unwrap();
}

#[test]
fn a_control_directory_others_could_use_is_replaced() {
    let runtime = runtime_dir();
    let ssh = FakeSsh::new(&[("XDG_RUNTIME_DIR", &runtime)]);
    let first = ssh.host.ssh_sharing_options()[5].clone();
    let dir = first.strip_prefix("ControlPath=").unwrap().strip_suffix("/%C").unwrap().to_string();
    // Readable by its group: no longer private.
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o740)).unwrap();
    let second = ssh.host.ssh_sharing_options()[5].clone();
    assert_ne!(second, first);
    let dir = second.strip_prefix("ControlPath=").unwrap().strip_suffix("/%C").unwrap().to_string();
    // A name another could make again, as a link.
    std::fs::remove_dir_all(&dir).unwrap();
    std::os::unix::fs::symlink(&runtime, &dir).unwrap();
    assert!(!ssh.host.ssh_sharing_options()[5].contains(&dir));
    std::fs::remove_file(&dir).unwrap();
    std::fs::remove_dir_all(first.strip_prefix("ControlPath=").unwrap().strip_suffix("/%C").unwrap()).ok();
    drop(ssh);
    std::fs::remove_dir_all(&runtime).unwrap();
}

#[test]
fn an_unusable_runtime_directory_falls_back_to_tmp() {
    let runtime = TempDir::new();
    let spaced = runtime.join("my dir");
    std::fs::create_dir_all(&spaced).unwrap();
    for place in [runtime.join("missing"), spaced, "/tmp/\"".to_string(), String::new()] {
        let ssh = FakeSsh::new(&[("XDG_RUNTIME_DIR", &place)]);
        assert!(ssh.host.ssh_sharing_options()[5].starts_with("ControlPath=/tmp/octoflow-ssh-"), "{place}");
    }
}

#[test]
fn read_missing_file() {
    let d = TempDir::new();
    assert_eq!(ProcessHost::local(&Interrupt::new()).read(&d.join("spec.md")).unwrap(), None);
    // A directory is no file either.
    assert_eq!(ProcessHost::local(&Interrupt::new()).read(&d.path()).unwrap(), None);
}

#[test]
fn read_failure_raises() {
    let d = TempDir::new();
    let path = d.join("spec.md");
    put(&path, "secret");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o000)).unwrap();
    if std::fs::read(&path).is_ok() {
        return; // Run as root, which reads any file.
    }
    let e = ProcessHost::local(&Interrupt::new()).read(&path).unwrap_err();
    assert_eq!(e.0, format!("reading {path}: cat: {path}: Permission denied"));
}

/// A gh on PATH that writes its arguments, its working directory and its input beside itself, and prints what the
/// file `prints` there holds, exiting with the status the file `status` holds.
fn fake_gh(bin: &str) {
    tool(
        bin,
        "gh",
        "#!/bin/sh\nd=$(dirname \"$0\")\nfor a in \"$@\"; do printf '%s\\0' \"$a\"; done > \"$d/gh-argv\"\n\
         pwd > \"$d/gh-cwd\"\ncat > \"$d/gh-input\"\ncat \"$d/prints\" 2>/dev/null\n\
         cat \"$d/says\" >&2 2>/dev/null\nexit $(cat \"$d/status\" 2>/dev/null || echo 0)\n",
    );
}

/// The arguments the fake gh in bin got last.
fn gh_argv(bin: &str) -> Vec<String> {
    read(&format!("{bin}/gh-argv")).split_terminator('\0').map(String::from).collect()
}

#[test]
fn create_pr_runs_gh_in_the_project() {
    let project = TempDir::new();
    let ssh = FakeSsh::new(&[]);
    let bin = ssh.dir.join("bin");
    fake_gh(&bin);
    put(&format!("{bin}/prints"), "Creating pull request\nhttps://github.com/o/r/pull/7\n");
    let cwd = project.path();
    let url = ssh.host.create_pr(&cwd, "main", "orchestrator/x", "Title", "body", true).unwrap();
    assert_eq!(url, "https://github.com/o/r/pull/7");
    let remote = ssh.last().pop().unwrap();
    assert!(remote.contains(&format!("_ {cwd} gh pr create --base main --head orchestrator/x")), "{remote}");
    assert!(remote.ends_with("--body-file - --draft"), "{remote}");
    assert_eq!(read(&format!("{bin}/gh-input")), "body");
    assert_eq!(strip(&read(&format!("{bin}/gh-cwd"))), std::fs::canonicalize(&cwd).unwrap().to_str().unwrap());
    assert_eq!(gh_argv(&bin), ["pr", "create", "--base", "main", "--head", "orchestrator/x", "--title", "Title",
        "--body-file", "-", "--draft"]);
    // Printing nothing is no pull request.
    put(&format!("{bin}/prints"), "\n");
    let e = ssh.host.create_pr(&cwd, "main", "orchestrator/x", "Title", "body", false).unwrap_err();
    assert_eq!(e.0, "gh pr create printed no URL for orchestrator/x");
    assert_eq!(gh_argv(&bin).last().unwrap(), "-");
}

#[test]
fn git_head_outside_repo() {
    let d = TempDir::new();
    assert_eq!(ProcessHost::local(&Interrupt::new()).git_head(&d.path()).unwrap(), None);
}

#[test]
fn run_states() {
    let d = TempDir::new();
    let cwd = d.path();
    let host = ProcessHost::local(&Interrupt::new());
    let runs = format!("{cwd}/{RUNS_DIR}");
    put(&format!("{runs}/a/state.json"), "{\"run_id\": \"a\"}\n");
    put(&format!("{runs}/b/state.json"), "{\"run_id\": \"b\"}");
    put(&format!("{runs}/c/other.json"), "{}");
    std::fs::create_dir_all(format!("{runs}/d/state.json")).unwrap();
    let now = host.now().unwrap();
    set_mtime(&format!("{runs}/a/state.json"), now - 12);
    // A file stamped ahead of the clock is no younger than new.
    set_mtime(&format!("{runs}/b/state.json"), now + 1000);
    let mut states = host.run_states(&cwd).unwrap();
    states.sort_by_key(|(_, s)| s.get("run_id").and_then(Json::as_str).map(String::from));
    assert!((12..15).contains(&states[0].0), "{states:?}");
    assert_eq!(states[0].1.get("run_id").and_then(Json::as_str), Some("a"));
    assert_eq!((states[1].0, states[1].1.get("run_id").and_then(Json::as_str)), (0, Some("b")));
    assert_eq!(states.len(), 2);
    assert_eq!(host.run_states(&d.join("nosuch")).unwrap(), vec![]);
}

#[test]
fn run_states_corrupt() {
    let d = TempDir::new();
    put(&d.join(&format!("{RUNS_DIR}/a/state.json")), "{\"run_id\": \n");
    let e = ProcessHost::local(&Interrupt::new()).run_states(&d.path()).unwrap_err();
    assert!(e.0.starts_with(&format!("corrupt run state under {}/{RUNS_DIR}: ", d.path())), "{e}");
}

#[test]
fn real_filesystem_roundtrip() {
    let d = TempDir::new();
    let host = ProcessHost::local(&Interrupt::new());
    let path = d.join("nested/file.md");
    assert_eq!(host.read(&path).unwrap(), None);
    host.write(&path, "hello", false).unwrap();
    assert_eq!(host.read(&path).unwrap().as_deref(), Some("hello"));
    let resolved = host.check(&["sh", "-c", "cd -- \"$1\" && pwd", "_", &d.path()], None, 60).unwrap();
    assert_eq!(host.resolve_dir(&d.path()).unwrap(), strip(&resolved));
    host.rename(&path, &d.join("nested/file.rejected.md")).unwrap();
    assert_eq!(host.read(&path).unwrap(), None);
    assert_eq!(host.read(&d.join("nested/file.rejected.md")).unwrap().as_deref(), Some("hello"));
    let e = host.resolve_dir(&d.join("nosuch")).unwrap_err();
    assert!(e.0.contains("failed: ") && e.0.contains("nosuch"), "{e}");
}

#[test]
fn now_and_mtime_on_the_real_host() {
    let d = TempDir::new();
    let path = d.join("review 1.md");
    let host = ProcessHost::local(&Interrupt::new());
    host.write(&path, "x", false).unwrap();
    set_mtime(&path, 1_700_000_000);
    assert_eq!(host.mtime(&path).unwrap(), 1_700_000_000);
    let wall = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64;
    assert!((host.now().unwrap() - wall).abs() <= 5);
    let e = host.mtime(&d.join("missing.md")).unwrap_err();
    assert!(e.0.contains("stat"), "{e}");
}

#[test]
fn now_and_mtime_over_ssh() {
    let d = TempDir::new();
    let path = d.join("review-1.md");
    put(&path, "x");
    set_mtime(&path, 1_791_000_000);
    let ssh = FakeSsh::new(&[]);
    assert!(ssh.host.now().unwrap() > 1_700_000_000);
    assert_eq!(ssh.last()[ssh.last().len() - 1], "date +%s");
    assert_eq!(ssh.host.mtime(&path).unwrap(), 1_791_000_000);
    let argv = ssh.last();
    assert_eq!(argv[argv.len() - 2], "remote-host");
    assert!(argv[argv.len() - 1].ends_with(&format!(" _ {path}")), "{argv:?}");
}

#[test]
fn a_time_that_is_no_number_raises() {
    let bin = TempDir::new();
    tool(&bin.path(), "date", "#!/bin/sh\necho soon\n");
    let e = host_with_tools(&bin.path()).now().unwrap_err();
    assert_eq!(e.0, "date +%s printed 'soon', not a time");
    assert_eq!(seconds(" 1_000\n", "x"), Ok(1000));
    for bad in ["", "_1", "1_", "1__0", "1.5", "99999999999999999999"] {
        assert!(seconds(bad, "x").is_err(), "{bad}");
    }
}

#[test]
fn rename_over_ssh() {
    let d = TempDir::new();
    let (from, to) = (d.join("review-1.md"), d.join("review 1.rejected.md"));
    put(&from, "x");
    let ssh = FakeSsh::new(&[]);
    ssh.host.rename(&from, &to).unwrap();
    let argv = ssh.last();
    assert_eq!(argv[argv.len() - 1], format!("mv -f -- {from} '{to}'"));
    assert_eq!(read(&to), "x");
}

#[test]
fn rename_failure_raises() {
    let d = TempDir::new();
    let e = ProcessHost::local(&Interrupt::new()).rename(&d.join("review-1.md"), &d.join("review-1.rejected.md")).unwrap_err();
    assert!(e.0.contains("No such file or directory"), "{e}");
    assert!(e.0.starts_with("mv -f -- "), "{e}");
    let ssh = FakeSsh::new(&[]);
    let e = ssh.host.rename(&d.join("review-1.md"), &d.join("b.md")).unwrap_err();
    assert!(e.0.contains(" failed on remote-host: "), "{e}");
}

#[test]
fn output_that_is_not_utf8_raises_naming_the_command() {
    let host = ProcessHost::local(&Interrupt::new());
    let e = host.run(&["printf", "\\351"], None, 60).unwrap_err();
    assert!(e.0.starts_with("printf '\\351': printed what is not UTF-8: 'utf-8' codec can't decode byte 0xe9"), "{e}");
    assert_eq!(host.run_verbatim(&["printf", "\\351"], None, 60).unwrap().stdout, b"\xe9");
    let e = host.run(&["sh", "-c", "printf '\\351' >&2"], None, 60).unwrap_err();
    assert!(e.0.contains("printed what is not UTF-8"), "{e}");
    let verbatim = host.run_verbatim(&["sh", "-c", "printf 'a\\351' >&2"], None, 60).unwrap();
    assert_eq!(verbatim.stderr, "a\u{fffd}");
}

// -- How a command runs: the fixes the Python engine's process handling holds -----------------------------------------

#[test]
fn line_ends_are_made_newlines_in_text_and_kept_verbatim() {
    let host = ProcessHost::local(&Interrupt::new());
    let argv = ["printf", "a\\r\\nb\\rc\\n"];
    assert_eq!(host.run(&argv, None, 60).unwrap().stdout, "a\nb\nc\n");
    assert_eq!(host.run_verbatim(&argv, None, 60).unwrap().stdout, b"a\r\nb\rc\n");
}

#[test]
fn output_and_input_beyond_a_pipe_s_buffer_are_read_and_written_whole() {
    let host = ProcessHost::local(&Interrupt::new());
    let input = "x".repeat(1 << 20);
    // cat echoes its input to both outputs while it reads, so neither side may wait for the other to finish.
    let proc = host.run(&["sh", "-c", "tee /dev/stderr"], Some(&input), 60).unwrap();
    assert_eq!((proc.stdout.len(), proc.stderr.len()), (input.len(), input.len()));
    // A command without input reads none.
    assert_eq!(host.run(&["cat"], None, 60).unwrap().stdout, "");
    // A command that stops reading its input is judged by its status.
    let proc = host.run(&["sh", "-c", "exit 4"], Some(&input), 60).unwrap();
    assert_eq!(proc.status, 4);
}

#[test]
fn a_command_that_runs_out_of_time_is_killed_with_its_process_group() {
    let d = TempDir::new();
    let pid_file = d.join("pid");
    let host = ProcessHost::local(&Interrupt::new());
    let started = std::time::Instant::now();
    let script = "sleep 30 & echo $! > \"$1\"; wait";
    let e = host.run(&["sh", "-c", script, "_", &pid_file], None, 1).unwrap_err();
    assert!(started.elapsed() < std::time::Duration::from_secs(10));
    assert_eq!(
        e.0,
        format!(
            "sh -c 'sleep 30 & echo $! > \"$1\"; wait' _ {pid_file}: Command '['sh', '-c', 'sleep 30 & echo $! > \
             \"$1\"; wait', '_', '{pid_file}']' timed out after 1 seconds"
        )
    );
    let pid = strip(&read(&pid_file)).to_string();
    // The background sleep went with its group: no process has its pid any more, or only as a zombie being reaped.
    let gone = (0..100).any(|_| {
        let state = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap_or_default();
        let alive = state.split(") ").nth(1).is_some_and(|rest| !rest.starts_with('Z'));
        if alive {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        !alive
    });
    assert!(gone, "sleep {pid} outlived its command");
}

#[test]
fn a_child_that_holds_the_input_open_unread_ends_the_command_at_its_time() {
    let host = ProcessHost::local(&Interrupt::new());
    let input = "x".repeat(1 << 20);
    let started = std::time::Instant::now();
    // The command exits at once, but the subshell it leaves keeps its input open without reading it (given it
    // through fd 3, as sh gives a background job /dev/null as its input).
    let script = "exec 3<&0; (exec <&3 >/dev/null 2>&1 3<&-; sleep 30) & exit 0";
    let e = host.run(&["sh", "-c", script], Some(&input), 1).unwrap_err();
    assert!(e.0.ends_with("timed out after 1 seconds"), "{e}");
    assert!(started.elapsed() < std::time::Duration::from_secs(10));
}

#[test]
fn a_child_that_holds_the_output_open_ends_the_command_at_its_time() {
    let host = ProcessHost::local(&Interrupt::new());
    let started = std::time::Instant::now();
    let e = host.run(&["sh", "-c", "sleep 30 & echo started"], None, 1).unwrap_err();
    assert!(e.0.ends_with("timed out after 1 seconds"), "{e}");
    assert!(started.elapsed() < std::time::Duration::from_secs(10));
}

#[test]
fn a_command_that_cannot_be_started_names_it() {
    let e = ProcessHost::local(&Interrupt::new()).run(&["no-such-command-here", "x"], None, 60).unwrap_err();
    assert_eq!(e.0, "no-such-command-here x: [Errno 2] No such file or directory: 'no-such-command-here'");
}

#[test]
fn a_command_ended_by_a_signal_has_its_negative_number() {
    let proc = ProcessHost::local(&Interrupt::new()).run(&["sh", "-c", "kill -TERM $$"], None, 60).unwrap();
    assert_eq!(proc.status, -15);
}

// -- The Ctrl-C (not in Python, whose commands get the terminal's SIGINT themselves) ----------------------------------

#[test]
fn a_ctrl_c_stops_the_running_command_with_its_process_group() {
    let d = TempDir::new();
    let (pid, background, interrupt) = (d.join("pid"), d.join("background"), Interrupt::new());
    let host = ProcessHost::local(&interrupt);
    let raising = raise_once_written(&pid, &interrupt);
    let started = std::time::Instant::now();
    let script = "sleep 600 & echo $! > \"$2\"; echo $$ > \"$1\"; wait";
    let e = host.run(&["sh", "-c", script, "_", &pid, &background], None, 600).unwrap_err();
    raising.join().unwrap();
    assert!(started.elapsed() < std::time::Duration::from_secs(60));
    assert_eq!(e.0, format!("sh -c 'sleep 600 & echo $! > \"$2\"; echo $$ > \"$1\"; wait' _ {pid} {background}: interrupted"));
    assert!(ended(&pid), "the command outlived the Ctrl-C");
    assert!(ended(&background), "what it started outlived the Ctrl-C");
}

#[test]
fn a_command_hears_the_ctrl_c_first_and_fails_even_when_it_then_succeeds() {
    // SIGINT first, so that git, say, removes its lock files; whatever the command does then, it was interrupted.
    let d = TempDir::new();
    let (pid, heard, interrupt) = (d.join("pid"), d.join("heard"), Interrupt::new());
    let host = ProcessHost::local(&interrupt);
    let raising = raise_once_written(&pid, &interrupt);
    let script = "trap 'echo SIGINT > \"$2\"; exit 0' INT; echo $$ > \"$1\"; while :; do sleep 0.05; done";
    let e = host.run(&[&HEARS_SIGINT[..], &["sh", "-c", script, "_", &pid, &heard]].concat(), None, 600).unwrap_err();
    raising.join().unwrap();
    assert!(e.0.ends_with(": interrupted"), "{e}");
    assert_eq!(read(&heard), "SIGINT\n");
}

#[test]
fn a_command_that_ignores_the_ctrl_c_is_killed() {
    let d = TempDir::new();
    let (pid, interrupt) = (d.join("pid"), Interrupt::new());
    let host = ProcessHost::local(&interrupt);
    let raising = raise_once_written(&pid, &interrupt);
    let script = "trap '' INT; echo $$ > \"$1\"; while :; do sleep 0.05; done";
    let e = host.run(&["sh", "-c", script, "_", &pid], None, 600).unwrap_err();
    raising.join().unwrap();
    assert!(e.0.ends_with(": interrupted"), "{e}");
    assert!(ended(&pid), "the command outlived the Ctrl-C");
}

#[test]
fn a_command_that_closed_its_output_is_stopped_too_and_hears_the_ctrl_c_first() {
    let d = TempDir::new();
    let (pid, heard, interrupt) = (d.join("pid"), d.join("heard"), Interrupt::new());
    let host = ProcessHost::local(&interrupt);
    let raising = raise_once_written(&pid, &interrupt);
    // It takes a while over its SIGINT, as git may: the grace waits for it.
    let script = "exec >/dev/null 2>&1; trap 'sleep 0.02; echo SIGINT > \"$2\"; exit 0' INT; echo $$ > \"$1\"; \
                  while :; do sleep 0.05; done";
    let e = host.run(&[&HEARS_SIGINT[..], &["sh", "-c", script, "_", &pid, &heard]].concat(), None, 600).unwrap_err();
    raising.join().unwrap();
    assert!(e.0.ends_with(": interrupted"), "{e}");
    assert!(ended(&pid), "the command outlived the Ctrl-C");
    assert_eq!(read(&heard), "SIGINT\n");
}

#[test]
fn no_command_starts_while_the_ctrl_c_is_pending() {
    let d = TempDir::new();
    let (ran, interrupt) = (d.join("ran"), Interrupt::new());
    let host = ProcessHost::local(&interrupt);
    interrupt.raise();
    let e = host.run(&["touch", &ran], None, 60).unwrap_err();
    assert_eq!(e.0, format!("touch {ran}: interrupted"));
    assert!(!std::path::Path::new(&ran).exists());
    // Caught, as the Workflow catches it to record the stop, commands run again.
    interrupt.catch();
    host.check(&["touch", &ran], None, 60).unwrap();
    assert!(std::path::Path::new(&ran).exists());
}

// -- TestChildEnvironment --------------------------------------------------------------------------------------------

/// The engine's environment with the CI credentials, git's pathspec switches and KEEP_ME set.
pub(crate) fn with_credentials() -> Vec<(&'static str, &'static str)> {
    let mut set: Vec<(&str, &str)> = vec![
        ("JENKINS_URL", "https://jenkins.example/"),
        ("JENKINS_USER", "agents"),
        ("JENKINS_TOKEN", "jenkins-secret-token"),
        ("KEEP_ME", "1"),
    ];
    set.extend(crate::constants::GIT_PATHSPEC_ENV.iter().map(|k| (*k, "1")));
    set
}

/// Panics unless env, the output of `env`, keeps KEEP_ME and holds none of the credentials or switches.
pub(crate) fn assert_scrubbed(env: &str) {
    assert!(env.lines().any(|l| l == "KEEP_ME=1"), "{env}");
    for k in crate::constants::CI_ENV.iter().chain(&crate::constants::GIT_PATHSPEC_ENV) {
        assert!(!env.lines().any(|l| l.starts_with(&format!("{k}="))), "{k} reached the process");
    }
    assert!(!env.contains("jenkins-secret-token"));
}

#[test]
fn a_real_process_sees_no_credentials() {
    let host = ProcessHost::in_environment(environment(None, &with_credentials()).into_iter(), &Interrupt::new());
    assert_scrubbed(&host.check(&["env"], None, 60).unwrap());
}

#[test]
fn host_commands_over_ssh_get_no_credentials() {
    // The fake ssh runs the command here, so env shows what ssh itself was started with.
    let ssh = FakeSsh::new(&with_credentials());
    assert_scrubbed(&ssh.host.check(&["env"], None, 60).unwrap());
}

// -- TestAtomicWrite, TestRunFiles and the session logs --------------------------------------------------------------

#[test]
fn real_filesystem_state() {
    let d = TempDir::new();
    let host = ProcessHost::local(&Interrupt::new());
    let run_dir = d.join(&format!("{RUNS_DIR}/r-abc"));
    host.write(&format!("{run_dir}/state.json"), "{\"run_id\": \"r-abc\"}\n", false).unwrap();
    host.write(&format!("{run_dir}/state.json"), "{\"run_id\": \"r-abc\", \"phase\": \"build\"}\n", false).unwrap();
    let names: Vec<String> =
        std::fs::read_dir(&run_dir).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
    assert_eq!(names, ["state.json"]);
    let [(age, state)] = <[(u64, Json); 1]>::try_from(host.run_states(&d.path()).unwrap()).unwrap();
    assert_eq!(state.get("phase").and_then(Json::as_str), Some("build"));
    assert!(age < 5);
}

#[test]
fn keep_mtime() {
    let d = TempDir::new();
    let (host, path) = (ProcessHost::local(&Interrupt::new()), d.join("state.json"));
    host.write(&path, "old\n", false).unwrap();
    set_mtime(&path, 1_000_000_000);
    host.write(&path, "new\n", true).unwrap();
    assert_eq!((host.read(&path).unwrap().unwrap(), host.mtime(&path).unwrap()), ("new\n".to_string(), 1_000_000_000));
    assert_eq!(std::fs::read_dir(d.path()).unwrap().count(), 1);
    host.write(&path, "newer\n", false).unwrap();
    assert!(host.mtime(&path).unwrap() > 1_000_000_000);
    // A write that fails leaves no file aside.
    std::fs::set_permissions(d.path(), std::fs::Permissions::from_mode(0o500)).unwrap();
    let failed = host.write(&path, "x", false);
    std::fs::set_permissions(d.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    if std::fs::metadata(d.path()).unwrap().permissions().readonly() || failed.is_ok() {
        return; // Run as root, which writes anywhere.
    }
    assert_eq!(std::fs::read_dir(d.path()).unwrap().count(), 1);
}

#[test]
fn run_files_of_a_real_directory_oldest_first() {
    let d = TempDir::new();
    let host = ProcessHost::local(&Interrupt::new());
    let run_dir = d.join(&format!("{RUNS_DIR}/r-abc"));
    // Name order is a, b, c, d; modification order is c, a, then b and d together.
    for (name, mtime) in [("b.md", 300), ("a.md", 200), ("c.md", 100), ("d.md", 300), ("state.json", 50),
        ("state.json.tmp.123", 60), ("spec.md.tmp.9", 70)]
    {
        host.write(&format!("{run_dir}/{name}"), name, false).unwrap();
        set_mtime(&format!("{run_dir}/{name}"), mtime);
    }
    std::fs::create_dir_all(format!("{run_dir}/sub")).unwrap();
    let files = host.run_files(&run_dir).unwrap();
    let expected: Vec<RunFile> = [("c.md", 100), ("a.md", 200), ("b.md", 300), ("d.md", 300)]
        .map(|(n, mtime)| RunFile { path: format!("{run_dir}/{n}"), mtime })
        .into();
    assert_eq!(files, expected);
    assert_eq!(host.run_files(&d.join("nosuch")).unwrap(), vec![]);
}

#[test]
fn run_files_in_one_call_over_ssh() {
    let d = TempDir::new();
    for (name, mtime) in [("b.md", 5), ("a b.md", 5), ("c.md", 3)] {
        put(&d.join(name), name);
        set_mtime(&d.join(name), mtime);
    }
    let ssh = FakeSsh::new(&[]);
    let files = ssh.host.run_files(&d.path()).unwrap();
    let names: Vec<(String, i64)> = files.into_iter().map(|f| (f.path, f.mtime)).collect();
    let dir = d.path();
    assert_eq!(names, [(format!("{dir}/c.md"), 3), (format!("{dir}/a b.md"), 5), (format!("{dir}/b.md"), 5)]);
    assert_eq!(ssh.calls().len(), 1);
    let remote = ssh.last().pop().unwrap();
    assert!(remote.contains("stat -c %Y") && remote.contains("stat -f %m"), "{remote}");
}

#[test]
fn run_files_failure_and_a_time_that_is_not_one() {
    let bin = TempDir::new();
    tool(&bin.path(), "stat", "#!/bin/sh\necho soon\n");
    let d = TempDir::new();
    put(&d.join("a.md"), "a");
    let e = host_with_tools(&bin.path()).run_files(&d.path()).unwrap_err();
    assert_eq!(e.0, format!("listing {}: stat printed 'soon', not a time", d.path()));
    tool(&bin.path(), "stat", "#!/bin/sh\necho Permission denied >&2; exit 1\n");
    let e = host_with_tools(&bin.path()).run_files(&d.path()).unwrap_err();
    assert!(e.0.contains("Permission denied"), "{e}");
}

#[test]
fn session_log_finds_the_file_under_home() {
    let home = TempDir::new();
    put(&home.join(".claude/projects/-proj/abc-123.jsonl"), "log\n");
    put(&home.join(".pi/agent/sessions/x/2026_s-1.jsonl"), "pi\n");
    let host = ProcessHost::in_environment(environment(None, &[("HOME", &home.path())]).into_iter(), &Interrupt::new());
    assert_eq!(host.session_log("claude", "abc-123").unwrap().as_deref(), Some(&b"log\n"[..]));
    assert_eq!(host.session_log("pi", "s-1").unwrap().as_deref(), Some(&b"pi\n"[..]));
    assert_eq!(host.session_log("claude", "other").unwrap(), None);
    assert_eq!(host.session_log("claude", "*").unwrap(), None);
    assert_eq!(host.session_log("claude", "").unwrap(), None);
    assert_eq!(host.session_log("codex", "abc-123").unwrap(), None);
}

#[test]
fn run_metrics_reads_each_run_s_line() {
    let d = TempDir::new();
    let host = ProcessHost::local(&Interrupt::new());
    let runs = d.join(RUNS_DIR);
    put(&format!("{runs}/a/metrics.json"), "{\"run\": \"a\"}");
    put(&format!("{runs}/b/metrics.json"), "{\"run\": \"b\"}\n");
    put(&format!("{runs}/c/state.json"), "{}");
    let metrics = host.run_metrics(&d.path()).unwrap();
    let runs_read: Vec<&str> = metrics.iter().filter_map(|m| m.get("run").and_then(Json::as_str)).collect();
    assert_eq!(runs_read, ["a", "b"]);
    put(&format!("{runs}/c/metrics.json"), "{");
    let e = host.run_metrics(&d.path()).unwrap_err();
    assert!(e.0.starts_with(&format!("corrupt metrics.json under {}/{RUNS_DIR}: ", d.path())), "{e}");
}

// -- TestHostPullRequest ---------------------------------------------------------------------------------------------

const URL: &str = "https://github.com/o/r/pull/7";

/// A Host with the fake gh, printing prints and exiting with status, and the fake gh's directory.
fn gh_host(prints: &str, says: &str, status: i32) -> (ProcessHost, TempDir) {
    let bin = TempDir::new();
    fake_gh(&bin.path());
    put(&bin.join("prints"), prints);
    put(&bin.join("says"), says);
    put(&bin.join("status"), &status.to_string());
    (host_with_tools(&bin.path()), bin)
}

#[test]
fn pull_request_state() {
    let project = TempDir::new();
    let open = format!("{{\"state\": \"OPEN\", \"headRefOid\": \"{}\", \"closedAt\": null}}", "a".repeat(40));
    let (host, bin) = gh_host(&open, "", 0);
    let pr = host.pull_request(&project.path(), URL).unwrap();
    assert_eq!(pr, PullRequest { state: "OPEN".into(), head_ref_oid: "a".repeat(40), closed_at: None });
    assert_eq!(gh_argv(&bin.path()), ["pr", "view", URL, "--json", "state,headRefOid,closedAt"]);
    let head = "b".repeat(64);
    let at = "2026-10-01T12:00:00Z";
    let merged = format!("{{\"state\": \"MERGED\", \"headRefOid\": \"{head}\", \"closedAt\": \"{at}\"}}");
    let (host, _bin) = gh_host(&merged, "", 0);
    assert_eq!(host.pull_request(&project.path(), URL).unwrap().closed_at.as_deref(), Some("2026-10-01T12:00:00Z"));
}

#[test]
fn pull_request_unexpected_output() {
    let open = |key: &str, value: &str| {
        let mut fields = vec![
            ("state", "\"OPEN\"".to_string()),
            ("headRefOid", format!("\"{}\"", "a".repeat(40))),
            ("closedAt", "null".to_string()),
        ];
        fields.retain(|(k, _)| *k != key);
        if !value.is_empty() {
            fields.push((key, value.to_string()));
        }
        format!("{{{}}}", fields.iter().map(|(k, v)| format!("\"{k}\": {v}")).collect::<Vec<_>>().join(", "))
    };
    for (out, why) in [
        ("not json".to_string(), ": "),
        ("[]".to_string(), ": list indices must be integers or slices, not str"),
        ("\"x\"".to_string(), ": string indices must be integers, not 'str'"),
        ("null".to_string(), ": 'NoneType' object is not subscriptable"),
        ("{\"state\": \"MERGED\"}".to_string(), ": 'headRefOid'"),
        ("{\"state\": 5}".to_string(), ", not a pull request's state"),
        (open("headRefOid", "\"main\""), ", not a pull request's state"),
        // A head that is no commit is judged before closedAt is read.
        ("{\"state\": \"OPEN\", \"headRefOid\": \"main\"}".to_string(), ", not a pull request's state"),
        (open("headRefOid", "7"), ": expected string or bytes-like object, got 'int'"),
        (open("closedAt", "5"), ", not a pull request's state"),
        (open("state", "1"), ", not a pull request's state"),
    ] {
        let (host, _bin) = gh_host(&out, "", 0);
        let e = host.pull_request("/", URL).unwrap_err();
        let GhError::Failed(e) = e else { panic!("{out}: {e:?}") };
        let printed = format!("gh pr view {URL} printed {}", crate::py::text::repr(&out));
        assert!(e.0.starts_with(&format!("{printed}{why}")), "{out}: {e}");
    }
}

#[test]
fn unusable_gh() {
    let (host, _bin) = gh_host("", "gh auth login\n", 4);
    let e = host.pull_request("/", URL).unwrap_err();
    assert_eq!(
        e,
        GhError::Unusable(OrchestratorError(format!(
            "gh pr view {URL} --json state,headRefOid,closedAt failed with status 4: gh auth login"
        )))
    );
    let (host, _bin) = gh_host("", "HTTP 502", 1);
    let e = host.pull_request("/", URL).unwrap_err();
    assert!(matches!(&e, GhError::Failed(e) if e.0.ends_with("failed: HTTP 502")), "{e:?}");
    let said = format!("gh pr view {URL} --json state,headRefOid,closedAt failed: HTTP 502");
    assert_eq!(OrchestratorError::from(e).0, said);
    // gh is not installed: sh's status 127.
    let bin = TempDir::new();
    std::os::unix::fs::symlink("/bin/sh", bin.join("sh")).unwrap();
    let host = ProcessHost::in_environment(environment(None, &[("PATH", &bin.path())]).into_iter(), &Interrupt::new());
    assert!(matches!(host.pull_request("/", URL), Err(GhError::Unusable(_))));
}

#[test]
fn merge_view() {
    let pr = "{\"state\": \"OPEN\", \"isDraft\": true, \"mergeable\": \"MERGEABLE\", \"body\": \"text\"}";
    let (host, bin) = gh_host(pr, "", 0);
    let merge = host.pull_request_merge("/", URL).unwrap();
    assert_eq!(
        merge,
        PullRequestMerge { state: "OPEN".into(), is_draft: true, mergeable: "MERGEABLE".into(), body: "text".into() }
    );
    assert_eq!(gh_argv(&bin.path()), ["pr", "view", URL, "--json", "state,isDraft,mergeable,body"]);
    let not_a_state = ", not a pull request's state";
    for (bad, why) in [
        (pr.replace("MERGEABLE", "BLOCKED"), not_a_state),
        (pr.replace("\"MERGEABLE\"", "[\"MERGEABLE\"]"), not_a_state),
        (pr.replace("true", "\"yes\""), not_a_state),
        (pr.replace("\"text\"", "null"), not_a_state),
        ("{\"state\": \"OPEN\"}".to_string(), ": 'isDraft'"),
        ("{\"state\": 1}".to_string(), not_a_state),
        ("{\"state\": \"OPEN\", \"isDraft\": false, \"mergeable\": \"UNKNOWN\"}".to_string(), ": 'body'"),
        ("[1]".to_string(), ": list indices must be integers or slices, not str"),
    ] {
        let (host, _bin) = gh_host(&bad, "", 0);
        let e = OrchestratorError::from(host.pull_request_merge("/", URL).unwrap_err());
        let printed = format!("gh pr view {URL} printed {}", crate::py::text::repr(&bad));
        assert!(e.0.starts_with(&format!("{printed}{why}")), "{bad}: {e}");
    }
    let (host, _bin) = gh_host("", "gh auth login\n", 4);
    assert!(matches!(host.pull_request_merge("/", URL), Err(GhError::Unusable(_))));
}

#[test]
fn edit_body_and_ready_run_gh_in_the_project() {
    let project = TempDir::new();
    let cwd = project.path();
    let ssh = FakeSsh::new(&[]);
    let bin = ssh.dir.join("bin");
    fake_gh(&bin);
    ssh.host.edit_pr_body(&cwd, URL, "new body").unwrap();
    let edit = ssh.last().pop().unwrap();
    assert!(edit.ends_with(&format!("_ {cwd} gh pr edit {URL} --body-file -")), "{edit}");
    assert_eq!(read(&format!("{bin}/gh-input")), "new body");
    ssh.host.mark_pr_ready(&cwd, URL).unwrap();
    let ready = ssh.last().pop().unwrap();
    assert!(ready.ends_with(&format!("_ {cwd} gh pr ready {URL}")), "{ready}");
}

#[test]
fn a_failed_edit_raises() {
    let (host, bin) = gh_host("", "HTTP 422", 1);
    let e = host.mark_pr_ready(&bin.path(), URL).unwrap_err();
    assert!(e.0.contains("gh pr ready ") && e.0.ends_with(" failed: HTTP 422"), "{e}");
}

// -- TestHostGit: a bare origin, our checkout A and someone else's B -------------------------------------------------

/// TestHostGit's setup: a bare origin, our checkout a and someone else's b, both at its main, which holds app.py.
struct Clones {
    tmp: TempDir,
    host: ProcessHost,
    a: String,
    b: String,
}

impl Clones {
    fn new() -> Clones {
        let tmp = TempDir::new();
        let host = ProcessHost::local(&Interrupt::new());
        let dir = tmp.path();
        let origin = format!("{dir}/origin.git");
        host.check(&["git", "init", "--quiet", "--bare", "-b", "main", &origin], None, 60).unwrap();
        let clone = |name: &str| {
            let path = format!("{dir}/{name}");
            host.check(&["git", "clone", "--quiet", &format!("{dir}/origin.git"), &path], None, 60).unwrap();
            for (key, value) in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")] {
                host.git(&path, &["config", key, value]).unwrap();
            }
            path
        };
        let (a, b) = (clone("a"), clone("b"));
        let clones = Clones { tmp, host, a, b };
        clones.commit(&clones.b, "app.py", "one\n");
        clones.sh(&["git", "-C", &clones.b, "push", "--quiet", "origin", "main"]);
        clones.sh(&["git", "-C", &clones.a, "pull", "--quiet", "origin", "main"]);
        clones
    }

    fn dir(&self) -> String {
        self.tmp.path()
    }

    fn sh(&self, argv: &[&str]) -> String {
        self.host.check(argv, None, 60).unwrap_or_else(|e| panic!("{e}"))
    }

    fn commit(&self, repo: &str, name: &str, text: &str) -> String {
        put(&format!("{repo}/{name}"), text);
        self.sh(&["git", "-C", repo, "add", name]);
        self.sh(&["git", "-C", repo, "commit", "--quiet", "-m", &format!("change {name}")]);
        self.head(repo)
    }

    fn head(&self, repo: &str) -> String {
        self.host.git_head(repo).unwrap().expect("a commit")
    }

    fn push_from_b(&self, name: &str, text: &str) -> String {
        let upstream = self.commit(&self.b, name, text);
        self.sh(&["git", "-C", &self.b, "push", "--quiet", "origin", "main"]);
        upstream
    }

    fn feature_branch(&self) -> String {
        self.sh(&["git", "-C", &self.a, "switch", "--quiet", "-c", "feature"]);
        self.commit(&self.a, "app.py", "one\nfeature\n")
    }

    /// A run branch with one commit, pushed from a; its commit.
    fn pushed_run_branch(&self) -> String {
        self.sh(&["git", "-C", &self.a, "switch", "--quiet", "-c", "orchestrator/x-a1b2c3"]);
        let head = self.commit(&self.a, "x.py", "x\n");
        self.sh(&["git", "-C", &self.a, "push", "--quiet", "origin", "orchestrator/x-a1b2c3"]);
        self.sh(&["git", "-C", &self.a, "switch", "--quiet", "main"]);
        head
    }

    /// Write a file of checkout a, or delete it with no text.
    fn put(&self, name: &str, text: Option<&str>) {
        let path = format!("{}/{name}", self.a);
        match text {
            Some(text) => put(&path, text),
            None => std::fs::remove_file(&path).unwrap(),
        }
    }
}

#[test]
fn file_at_reads_a_file_as_a_commit_has_it() {
    let c = Clones::new();
    let first = c.head(&c.a);
    let second = c.commit(&c.a, "sub/lib.py", "two\n");
    let at = |cwd: &str, commit: &str, path: &str| c.host.file_at(cwd, commit, path).unwrap();
    assert_eq!(at(&c.a, &second, "sub/lib.py").as_deref(), Some(&b"two\n"[..]));
    assert_eq!(at(&format!("{}/sub", c.a), &second, "sub/lib.py").as_deref(), Some(&b"two\n"[..]));
    assert_eq!(at(&c.a, &first, "sub/lib.py"), None);
    assert_eq!(at(&c.a, &second, "sub"), None);
    // git ls-tree lists a directory's entries, not the directory, for a path ending in "/" and for none.
    for path in ["sub/", ""] {
        assert_eq!(at(&c.a, &second, path), None, "{path:?}");
    }
    assert_eq!(at(&c.a, &first, "app.py").as_deref(), Some(&b"one\n"[..]));
    assert!(c.host.file_at(&c.a, &"0".repeat(40), "app.py").is_err());
}

#[test]
fn changed_line_counts_pair_renames_and_count_binary_files_as_none() {
    let c = Clones::new();
    c.commit(&c.a, "gone.py", "x\ny\n");
    let lines: String = (0..20).map(|i| format!("line {i}\n")).collect();
    let base = c.commit(&c.a, "lib.py", &lines);
    c.sh(&["git", "-C", &c.a, "rm", "--quiet", "gone.py"]);
    std::fs::create_dir_all(format!("{}/sub dir", c.a)).unwrap();
    c.sh(&["git", "-C", &c.a, "mv", "lib.py", "sub dir/lib.py"]);
    put(&format!("{}/sub dir/lib.py", c.a), &format!("{lines}added\n"));
    c.put("app.py", Some("two\n"));
    std::fs::write(format!("{}/logo.png", c.a), b"\x00\x01\x02").unwrap();
    c.sh(&["git", "-C", &c.a, "add", "-A"]);
    c.sh(&["git", "-C", &c.a, "commit", "--quiet", "-m", "change"]);
    let mut counts = c.host.changed_line_counts(&c.a, &base, &c.head(&c.a)).unwrap();
    counts.sort();
    let file = |base_path: &str, path: &str, added, removed| ChangedFile {
        base_path: base_path.into(),
        path: path.into(),
        added,
        removed,
    };
    assert_eq!(
        counts,
        [file("app.py", "app.py", 1, 1), file("gone.py", "gone.py", 0, 2), file("lib.py", "sub dir/lib.py", 1, 0),
            file("logo.png", "logo.png", 0, 0)]
    );
}

#[test]
fn numstat_that_is_not_git_s_is_refused() {
    for out in [&b"x\t1\tf\0"[..], b"1\t1\0", b"1\t1\t\0old"] {
        assert!(numstat(out).is_err(), "{out:?}");
    }
    assert_eq!(numstat(b"").unwrap(), vec![]);
}

#[test]
fn fast_forward_moves_the_base_to_origins() {
    let c = Clones::new();
    let upstream = c.push_from_b("lib.py", "new\n");
    c.host.fast_forward(&c.a, "main").unwrap();
    assert_eq!(c.head(&c.a), upstream);
}

#[test]
fn fast_forward_leaves_a_base_ahead_of_origin() {
    let c = Clones::new();
    let ahead = c.commit(&c.a, "local.py", "mine\n");
    c.host.fast_forward(&c.a, "main").unwrap();
    assert_eq!(c.head(&c.a), ahead);
}

#[test]
fn fast_forward_refuses_a_diverged_base() {
    let c = Clones::new();
    c.push_from_b("lib.py", "theirs\n");
    let mine = c.commit(&c.a, "local.py", "mine\n");
    let e = c.host.fast_forward(&c.a, "main").unwrap_err();
    assert!(
        e.0.starts_with(
            "main has diverged from origin/main; reconcile it first. git merge --ff-only --quiet origin/main failed"
        ),
        "{e}"
    );
    assert_eq!(c.head(&c.a), mine);
}

#[test]
fn fast_forward_that_cannot_merge_a_base_behind_says_so() {
    let c = Clones::new();
    c.push_from_b("app.py", "theirs\n");
    // An uncommitted edit of the file the fast-forward would change stops it.
    c.put("app.py", Some("mine\n"));
    let e = c.host.fast_forward(&c.a, "main").unwrap_err();
    assert!(e.0.starts_with("main cannot be fast-forwarded to origin/main; reconcile it first."), "{e}");
}

#[test]
fn fetch_of_a_branch_origin_lacks() {
    let c = Clones::new();
    let e = c.host.fast_forward(&c.a, "develop").unwrap_err();
    let said = format!("could not fetch develop from origin: git -C {} fetch --quiet origin develop failed", c.a);
    assert!(e.0.starts_with(&said), "{e}");
}

#[test]
fn merge_of_a_moved_base_is_a_merge_commit() {
    let c = Clones::new();
    let mine = c.feature_branch();
    let upstream = c.push_from_b("lib.py", "new\n");
    assert_eq!(c.host.merge_upstream(&c.a, "main").unwrap(), Vec::<String>::new());
    let parents = c.sh(&["git", "-C", &c.a, "rev-list", "--parents", "-n", "1", "HEAD"]);
    assert_eq!(parents.split_whitespace().skip(1).collect::<Vec<_>>(), [mine, upstream]);
}

#[test]
fn merge_is_skipped_when_the_base_is_in_head() {
    let c = Clones::new();
    c.feature_branch();
    c.push_from_b("lib.py", "new\n");
    c.host.merge_upstream(&c.a, "main").unwrap();
    let merged = c.head(&c.a);
    assert!(c.host.merge_upstream(&c.a, "main").unwrap().is_empty());
    assert_eq!(c.head(&c.a), merged);
}

#[test]
fn conflicting_merge_is_aborted_and_reported() {
    let c = Clones::new();
    let mine = c.feature_branch();
    c.push_from_b("app.py", "one\ntheirs\n");
    assert_eq!(c.host.merge_upstream(&c.a, "main").unwrap(), ["app.py"]);
    assert_eq!(c.head(&c.a), mine);
    assert_eq!(c.sh(&["git", "-C", &c.a, "status", "--porcelain"]), "");
    assert!(!c.host.abort_merge(&c.a).unwrap());
}

#[test]
fn a_merge_that_fails_without_a_conflict_raises() {
    let c = Clones::new();
    c.feature_branch();
    c.push_from_b("lib.py", "theirs\n");
    // An untracked file the merge would overwrite stops it before any conflict.
    c.put("lib.py", Some("mine\n"));
    let e = c.host.merge_upstream(&c.a, "main").unwrap_err();
    assert!(e.0.starts_with(&format!("git merge origin/main failed in {}: ", c.a)), "{e}");
}

#[test]
fn abort_merge_ends_a_half_done_merge() {
    let c = Clones::new();
    let mine = c.feature_branch();
    c.push_from_b("app.py", "one\ntheirs\n");
    c.sh(&["git", "-C", &c.a, "fetch", "--quiet", "origin", "main"]);
    assert_eq!(c.host.git_run(&c.a, &["merge", "origin/main"], 60).unwrap().status, 1);
    assert!(c.host.abort_merge(&c.a).unwrap());
    assert_eq!(c.head(&c.a), mine);
    assert_eq!(c.sh(&["git", "-C", &c.a, "status", "--porcelain"]), "");
}

#[test]
fn lease_guarded_delete_at_the_expected_commit() {
    let c = Clones::new();
    let head = c.pushed_run_branch();
    assert_eq!(c.host.remote_branch_commit(&c.a, "orchestrator/x-a1b2c3").unwrap(), Some(head.clone()));
    assert_eq!(c.host.remote_branches(&c.a, "orchestrator/").unwrap(), ["orchestrator/x-a1b2c3"]);
    c.host.delete_remote_branch_at(&c.a, "orchestrator/x-a1b2c3", &head).unwrap();
    assert_eq!(c.host.remote_branch_commit(&c.a, "orchestrator/x-a1b2c3").unwrap(), None);
    assert_eq!(c.sh(&["git", "-C", &c.a, "for-each-ref", "refs/remotes/origin/orchestrator/"]), "");
}

#[test]
fn lease_guarded_delete_refuses_a_branch_that_moved() {
    let c = Clones::new();
    let head = c.pushed_run_branch();
    c.sh(&["git", "-C", &c.b, "fetch", "--quiet", "origin", "orchestrator/x-a1b2c3:orchestrator/x-a1b2c3"]);
    c.sh(&["git", "-C", &c.b, "switch", "--quiet", "orchestrator/x-a1b2c3"]);
    let moved = c.commit(&c.b, "x.py", "x\ntheirs\n");
    c.sh(&["git", "-C", &c.b, "push", "--quiet", "origin", "orchestrator/x-a1b2c3"]);
    let e = c.host.delete_remote_branch_at(&c.a, "orchestrator/x-a1b2c3", &head).unwrap_err();
    let lease = format!("--force-with-lease=refs/heads/orchestrator/x-a1b2c3:{head} origin --delete ");
    assert!(e.0.contains(&lease) && e.0.contains(" failed: ") && e.0.contains("stale info"), "{e}");
    assert_eq!(c.host.remote_branch_commit(&c.a, "orchestrator/x-a1b2c3").unwrap(), Some(moved));
}

#[test]
fn remote_branch_commit_is_the_exact_name() {
    let c = Clones::new();
    c.pushed_run_branch();
    assert_eq!(c.host.remote_branch_commit(&c.a, "x-a1b2c3").unwrap(), None);
    c.host.delete_remote_branch(&c.a, "orchestrator/x-a1b2c3").unwrap();
    assert!(c.host.remote_branches(&c.a, "orchestrator/").unwrap().is_empty());
}

#[test]
fn checked_out_branches_of_every_worktree() {
    let c = Clones::new();
    let wt = format!("{}/wt", c.dir());
    c.sh(&["git", "-C", &c.a, "worktree", "add", "--quiet", "-b", "orchestrator/y-a1b2c3", &wt]);
    let real = |p: &str| std::fs::canonicalize(p).unwrap().to_str().unwrap().to_string();
    let branches = c.host.checked_out_branches(&c.a).unwrap();
    let listed: Vec<(&str, &String)> = branches.iter().collect();
    assert_eq!(listed, [("main", &real(&c.a)), ("orchestrator/y-a1b2c3", &real(&wt))]);
    assert_eq!(c.host.checked_out_branches(&wt).unwrap(), branches);
}

#[test]
fn local_branch_steps() {
    let c = Clones::new();
    let head = c.pushed_run_branch();
    let older = strip(&c.sh(&["git", "-C", &c.a, "rev-parse", "main"])).to_string();
    let branch = "orchestrator/x-a1b2c3";
    assert_eq!(c.host.branch_commit(&c.a, branch).unwrap(), Some(head.clone()));
    assert_eq!(c.host.branch_commit(&c.a, "orchestrator/none").unwrap(), None);
    assert!(c.host.branch_commit(&format!("{}/nowhere", c.dir()), branch).is_err());
    assert!(c.host.is_ancestor(&c.a, &older, &head).unwrap());
    assert!(!c.host.is_ancestor(&c.a, &head, &older).unwrap());
    assert!(c.host.is_ancestor(&c.a, "nosuch", &head).is_err());
    assert_eq!(c.host.commits_beyond(&c.a, &older, branch).unwrap(), 1);
    assert!(c.host.has_commit(&c.a, &head).unwrap());
    assert!(!c.host.has_commit(&c.a, &"f".repeat(40)).unwrap());
    c.host.delete_tracking_ref(&c.a, branch).unwrap();
    c.host.delete_tracking_ref(&c.a, branch).unwrap();
    assert_eq!(c.sh(&["git", "-C", &c.a, "for-each-ref", "refs/remotes/origin/orchestrator/"]), "");
    c.host.delete_branch(&c.a, branch).unwrap();
    assert_eq!(c.host.branch_commit(&c.a, branch).unwrap(), None);
}

#[test]
fn a_count_that_is_no_count_raises() {
    let bin = TempDir::new();
    tool(&bin.path(), "git", "#!/bin/sh\necho many\n");
    let e = host_with_tools(&bin.path()).commits_beyond("/r", "c0ffee", "b").unwrap_err();
    assert_eq!(e.0, "git rev-list --count c0ffee..refs/heads/b printed 'many', not a count");
}

#[test]
fn has_origin_on_real_git() {
    let c = Clones::new();
    assert!(c.host.has_origin(&c.a).unwrap());
    c.sh(&["git", "-C", &c.a, "remote", "remove", "origin"]);
    assert!(!c.host.has_origin(&c.a).unwrap());
    // Outside a repository git cannot name a URL either.
    assert!(!c.host.has_origin(&c.dir()).unwrap());
}

#[test]
fn worktree_steps() {
    let c = Clones::new();
    put(&format!("{}/.orchestrator/.gitignore", c.a), "*\n");
    let wt = format!("{}/.orchestrator/worktrees/a1b2c3", c.a);
    let upstream = c.push_from_b("lib.py", "new\n");
    assert!(!c.host.is_worktree(&c.a, &wt).unwrap());
    c.host.add_worktree(&c.a, &wt, &c.host.fetch(&c.a, "main").unwrap()).unwrap();
    assert!(c.host.is_worktree(&c.a, &wt).unwrap());
    assert_eq!(c.head(&wt), upstream);
    assert_eq!(c.sh(&["git", "-C", &wt, "rev-parse", "--abbrev-ref", "HEAD"]), "HEAD\n");
    assert_eq!(c.sh(&["git", "-C", &c.a, "status", "--porcelain"]), "");
    // Another clone's checkout.
    assert!(!c.host.is_worktree(&c.a, &c.b).unwrap());
    assert!(!c.host.is_worktree(&c.a, &format!("{}/nowhere", c.dir())).unwrap());
    put(&format!("{wt}/new.py"), "x\n");
    let e = c.host.remove_worktree(&c.a, &wt, false).unwrap_err();
    assert!(e.0.contains("worktree remove ") && e.0.contains(" failed: ") && e.0.contains("untracked"), "{e}");
    std::fs::remove_file(format!("{wt}/new.py")).unwrap();
    c.host.remove_worktree(&c.a, &wt, false).unwrap();
    assert!(!std::path::Path::new(&wt).exists());
    assert!(!c.host.is_worktree(&c.a, &wt).unwrap());
    // A dirty one goes by force.
    c.host.add_worktree(&c.a, &wt, "HEAD").unwrap();
    put(&format!("{wt}/new.py"), "x\n");
    c.host.remove_worktree(&c.a, &wt, true).unwrap();
    assert!(!std::path::Path::new(&wt).exists());
}

#[test]
fn a_deleted_worktree_is_no_worktree() {
    let c = Clones::new();
    let wt = format!("{}/wt", c.dir());
    c.host.add_worktree(&c.a, &wt, "HEAD").unwrap();
    std::fs::remove_dir_all(&wt).unwrap();
    assert!(c.sh(&["git", "-C", &c.a, "worktree", "list", "--porcelain"]).contains(&format!("worktree {wt}")));
    assert!(!c.host.is_worktree(&c.a, &wt).unwrap());
}

#[test]
fn worktree_through_a_symlink() {
    let c = Clones::new();
    let link = format!("{}/link", c.dir());
    std::os::unix::fs::symlink(&c.a, &link).unwrap();
    let wt = format!("{link}/wt");
    c.host.add_worktree(&link, &wt, "HEAD").unwrap();
    assert!(c.host.is_worktree(&link, &wt).unwrap());
    assert!(c.host.is_worktree(&c.a, &format!("{}/wt", c.a)).unwrap());
}

/// The lines added_lines finds in a change_diff, by file.
fn changed_lines(diff: &[u8]) -> Vec<(String, Vec<u64>)> {
    let added = crate::patch::added_lines(diff).unwrap();
    added.iter().map(|(p, lines)| (p.to_string(), lines.keys().copied().collect())).collect()
}

#[test]
fn snapshot_in_a_worktree_holds_only_its_change() {
    let c = Clones::new();
    let base = c.head(&c.a);
    let wt = format!("{}/wt", c.dir());
    c.host.add_worktree(&c.a, &wt, &base).unwrap();
    c.put("app.py", Some("one\nchecked out\n"));
    c.put("mine.py", Some("mine\n"));
    put(&format!("{wt}/feature.py"), "feature\n");
    let sha = c.host.snapshot(&wt, &base, "snapshot").unwrap();
    assert_eq!(c.sh(&["git", "-C", &wt, "diff", "--name-only", &base, &sha]), "feature.py\n");
    let diff = c.host.change_diff(&wt, &base, Some(&sha)).unwrap();
    assert_eq!(changed_lines(&diff), [("feature.py".to_string(), vec![1])]);
    assert_eq!(c.sh(&["git", "-C", &c.a, "status", "--porcelain"]), " M app.py\n?? mine.py\n");
}

#[test]
fn locked_files_are_restored_and_the_index_is_left_alone() {
    let c = Clones::new();
    c.commit(&c.a, "gone.py", "old\n");
    c.commit(&c.a, "removed.py", "old\n");
    let base = c.head(&c.a);
    let git_ref = "refs/orchestrator/a1b2c3/locked/tests";
    c.put("tests/test_[x].py", Some("assert f()\n"));
    c.put("app.py", Some("one\ntested\n"));
    c.put("gone.py", None);
    // Deleted from the index too, so that git restore would know it from neither side.
    c.sh(&["git", "-C", &c.a, "rm", "--quiet", "removed.py"]);
    let all = ["app.py", "gone.py", "removed.py", "tests/test_[x].py"];
    assert_eq!(c.host.lock(&c.a, &base, git_ref).unwrap(), all);
    let locked = c.host.lock(&c.a, &base, git_ref).unwrap();

    // A later step edits one, deletes another, brings back the third and changes a file nobody locked.
    c.put("tests/test_[x].py", None);
    c.put("app.py", Some("one\nweakened\n"));
    c.put("gone.py", Some("back\n"));
    c.put("removed.py", Some("back\n"));
    c.put("feature.py", Some("new\n"));
    std::fs::create_dir_all(format!("{}/sub", c.a)).unwrap();
    let sub = format!("{}/sub", c.a);
    let (differ, diff) = c.host.locked_changes(&sub, git_ref, &locked).unwrap();
    assert_eq!(differ, all);
    assert!(diff.contains("-tested\n+weakened\n") && diff.contains("deleted file mode"), "{diff}");
    let porcelain = c.sh(&["git", "-C", &c.a, "status", "--porcelain"]);

    c.host.restore(&sub, git_ref, &differ).unwrap();
    let file = |name: &str| read(&format!("{}/{name}", c.a));
    assert_eq!((file("tests/test_[x].py"), file("app.py"), file("feature.py")),
        ("assert f()\n".to_string(), "one\ntested\n".to_string(), "new\n".to_string()));
    assert!(!std::path::Path::new(&format!("{}/gone.py", c.a)).exists());
    assert!(!std::path::Path::new(&format!("{}/removed.py", c.a)).exists());
    assert_eq!(c.host.locked_changes(&c.a, git_ref, &locked).unwrap(), (vec![], String::new()));
    assert_eq!(c.host.locked_changes(&c.a, git_ref, &[]).unwrap(), (vec![], String::new()));
    c.host.restore(&c.a, git_ref, &[]).unwrap();
    // The index holds only the deletion staged above.
    assert_eq!(c.sh(&["git", "-C", &c.a, "diff", "--cached", "--name-only"]), "removed.py\n");
    assert_ne!(c.sh(&["git", "-C", &c.a, "status", "--porcelain"]), porcelain);

    let other = "refs/orchestrator/d4e5f6/locked/tests";
    c.sh(&["git", "-C", &c.a, "update-ref", other, &base]);
    c.host.delete_refs(&c.a, "refs/orchestrator/a1b2c3/").unwrap();
    let refs = c.sh(&["git", "-C", &c.a, "for-each-ref", "--format=%(refname)", "refs/orchestrator/"]);
    assert_eq!(refs, format!("{other}\n"));
    c.host.delete_refs(&c.a, "refs/orchestrator/a1b2c3/").unwrap();
}

// -- TestHostSnapshot ------------------------------------------------------------------------------------------------

#[test]
fn snapshot_of_the_working_tree() {
    let r = Repo::new(&[]);
    let files = [("keep.py", "a\n"), ("mod.py", "1\n2\n"), ("del.py", "x\n"), (".gitignore", "*.log\n")];
    let base = r.commit(&files, "base");
    r.put("mod.py", "1\ntwo\n3\n");
    std::fs::remove_file(format!("{}/del.py", r.path)).unwrap();
    r.put("untracked.py", "new\n");
    r.put("debug.log", "ignored\n");
    r.put(".orchestrator/.gitignore", "*\n");
    r.put(".orchestrator/runs/r/state.json", "{}\n");
    // A staged change, to show the index is left as it was.
    r.git(&["add", "mod.py"]);
    let look = || [r.git(&["rev-parse", "HEAD"]), r.git(&["ls-files", "--stage"]), r.git(&["status", "--porcelain"])];
    let before = look();
    let sha = r.host.snapshot(&r.path, &base, "snapshot").unwrap();
    assert_eq!(look(), before);
    let tree = r.git(&["ls-tree", "-r", "--name-only", &sha]);
    assert_eq!(tree.split_whitespace().collect::<Vec<_>>(), [".gitignore", "keep.py", "mod.py", "untracked.py"]);
    assert_eq!(r.git(&["show", &format!("{sha}:mod.py")]), "1\ntwo\n3\n");
    assert_eq!(r.git(&["rev-parse", &format!("{sha}^")]), r.git(&["rev-parse", &base]));
    assert_eq!(strip(&r.git(&["log", "--format=%s", "-n", "1", &sha])), "snapshot");
    assert_eq!(
        changed_lines(&r.host.change_diff(&r.path, &base, Some(&sha)).unwrap()),
        [("mod.py".to_string(), vec![2, 3]), ("untracked.py".to_string(), vec![1])]
    );
    // Without a commit, the diff is the working tree's tracked files'.
    assert_eq!(changed_lines(&r.host.change_diff(&r.path, &base, None).unwrap()), [("mod.py".to_string(), vec![2, 3])]);
}

#[test]
fn snapshot_of_a_repository_without_an_index() {
    let r = Repo::new(&[]);
    r.git(&["commit", "--quiet", "--allow-empty", "-m", "empty"]);
    let base = r.host.git_head(&r.path).unwrap().unwrap();
    r.put("first.py", "x\n");
    let sha = r.host.snapshot(&r.path, &base, "snapshot").unwrap();
    assert_eq!(strip(&r.git(&["ls-tree", "-r", "--name-only", &sha])), "first.py");
}

#[test]
fn a_snapshot_git_cannot_make_raises() {
    let r = Repo::new(&[("a", "a\n")]);
    let e = r.host.snapshot(&r.path, &"0".repeat(40), "snapshot").unwrap_err();
    assert!(e.0.contains(" failed: "), "{e}");
    let bin = TempDir::new();
    tool(&bin.path(), "git", "#!/bin/sh\ncase $1 in commit-tree) echo notacommit;; esac\n");
    let e = host_with_tools(&bin.path()).snapshot(&r.path, r.base(), "s").unwrap_err();
    assert_eq!(e.0, format!("snapshot of {}: git commit-tree printed 'notacommit', not a commit", r.path));
}

// -- What the Python engine's constants hold --------------------------------------------------------------------------

#[test]
fn host_s_constants_are_the_python_engine_s() {
    let recorded = oracle("host.json");
    let constants = recorded.get("constants").unwrap();
    let int = |name: &str| constants.get(name).and_then(Json::as_int).unwrap();
    assert_eq!(int("MISSING_FILE_STATUS"), i128::from(MISSING_FILE_STATUS));
    let unusable = constants.get("GH_UNUSABLE_STATUS").and_then(Json::as_array).unwrap();
    let unusable: Vec<i128> = unusable.iter().filter_map(Json::as_int).collect();
    assert_eq!(unusable, GH_UNUSABLE_STATUS.map(i128::from));
    assert_eq!(constants.get("IN_DIR").and_then(Json::as_str), Some(IN_DIR));
    assert_eq!(int("SSH_CONTROL_PERSIST_SECONDS"), i128::from(process::SSH_CONTROL_PERSIST_SECONDS));
    assert_eq!(int("SSH_ALIVE_INTERVAL_SECONDS"), i128::from(process::SSH_ALIVE_INTERVAL_SECONDS));
    assert_eq!(int("SSH_ALIVE_COUNT"), i128::from(process::SSH_ALIVE_COUNT));
    let logs = constants.get("SESSION_LOGS").and_then(Json::as_object).unwrap();
    let ours: Vec<(&str, &str)> = logs.iter().map(|(k, v)| (k, v.as_str().unwrap())).collect();
    let harnesses: Vec<(&str, &str)> =
        crate::harness::HARNESSES.iter().filter_map(|h| h.session_log.map(|log| (h.kind, log.glob))).collect();
    assert_eq!(ours, harnesses);
}

#[test]
fn checked_names_the_command_and_the_target() {
    let failed = Completed::new(2, "out", " oops \n");
    let e = checked(&ProcessHost::local(&Interrupt::new()), &["git", "a b"], failed.clone()).unwrap_err();
    assert_eq!(e.0, "git 'a b' failed: oops");
    let remote = ProcessHost::local(&Interrupt::new()).over_ssh("box");
    assert_eq!(checked(&remote, &["x"], failed).unwrap_err().0, "x failed on box: oops");
    assert_eq!(checked(&remote, &["x"], Completed::new(0, "out", "")).unwrap(), "out");
}
