//! The orchestrator program as the human runs it (src/main.rs): its exit statuses and last lines, with herdr played
//! by a script on its PATH. What it does in between, run and resume to their verdicts, is the parity scenarios'
//! (tests/parity) and crate::cli's tests'.
//!
//! Linux only, as the engine's other process tests: GNU env starts the program with SIGINT, SIGTERM and SIGHUP at
//! their default, whatever the test's own process does with them (Jenkins runs the tests with SIGINT ignored).

use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

use nix::sys::signal::{killpg, Signal};
use nix::unistd::Pid;

/// How long a test waits for the program to get somewhere, before it fails.
const PATIENCE: Duration = Duration::from_secs(30);

/// A directory of the test's own, removed when it is dropped.
struct Dir(PathBuf);

impl Dir {
    fn new(name: &str) -> Dir {
        let base = std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, PathBuf::from);
        let dir = base.join(format!("octoflow-program-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("bin")).expect("a directory for the test");
        Dir(dir)
    }

    /// Put an executable script named name, whose text is script, in the directory's bin.
    fn tool(&self, name: &str, script: &str) {
        let path = self.0.join("bin").join(name);
        std::fs::write(&path, script).expect("the script is written");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).expect("the script is executable");
    }
}

impl Drop for Dir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// The program, with args, started with SIGINT, SIGTERM and SIGHUP at their default, in a process group of its own,
/// its PATH beginning with dir's bin, and HERDR_ENV as herdr_env gives it.
fn program(dir: &Dir, args: &[&str], herdr_env: Option<&str>) -> Command {
    let mut command = Command::new("env");
    command.arg("--default-signal=INT,TERM,HUP").arg(env!("CARGO_BIN_EXE_orchestrator")).args(args);
    let path = format!("{}:{}", dir.0.join("bin").display(), std::env::var("PATH").unwrap_or_default());
    command.env("PATH", path).env("XDG_CONFIG_HOME", dir.0.join("config")).env_remove("HERDR_ENV");
    if let Some(value) = herdr_env {
        command.env("HERDR_ENV", value);
    }
    command.current_dir(&dir.0).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).process_group(0);
    command
}

fn stderr(out: &Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

/// Wait until path exists; panics after PATIENCE.
fn wait_for(path: &Path) {
    let start = Instant::now();
    while !path.exists() {
        assert!(start.elapsed() < PATIENCE, "{} was not written", path.display());
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn a_run_outside_a_herdr_pane_without_a_machine_is_refused() {
    let dir = Dir::new("pane");
    let out = program(&dir, &["run", "a task"], None).output().expect("the program runs");
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(stderr(&out), "error: not inside a herdr pane; run from herdr, or pass --machine\n");
    assert!(out.stdout.is_empty());
}

#[test]
fn a_command_about_runs_outside_a_herdr_pane_is_refused_but_workflows_is_not() {
    // TestCLI.test_refuses_outside_herdr, and the workflows command, which needs no herdr.
    let dir = Dir::new("herdrless");
    let out = program(&dir, &["list"], Some("")).output().expect("the program runs");
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(stderr(&out), "error: not inside a herdr pane; run from herdr, or pass --machine\n");
    let out = program(&dir, &["workflows"], None).output().expect("the program runs");
    assert_eq!(out.status.code(), Some(0), "{}", stderr(&out));
    assert!(String::from_utf8_lossy(&out.stdout).starts_with("default  built in\n"));
}

#[test]
fn standard_output_that_takes_no_write_is_an_error() {
    // As Python's print raising OSError ends orchestrator.py with status 1: what the command prints is its product.
    let dir = Dir::new("full");
    let full = std::fs::OpenOptions::new().write(true).open("/dev/full").expect("/dev/full opens");
    let out = program(&dir, &["workflows"], None).stdout(full).output().expect("the program runs");
    assert_eq!(out.status.code(), Some(1));
    assert!(stderr(&out).starts_with("error: cannot write standard output: "), "{}", stderr(&out));
}

#[test]
fn no_arguments_print_the_help_as_a_usage_error() {
    // The tk GUI is not ported (the rust-engine spec's Decisions): the help, where Python without a display says the
    // command is missing, both with exit status 2.
    let dir = Dir::new("no-args");
    let out = program(&dir, &[], None).output().expect("the program runs");
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("Commands:") && stderr(&out).contains("workflows"), "{}", stderr(&out));
    assert!(out.stdout.is_empty());
}

#[test]
fn a_usage_error_exits_2() {
    let dir = Dir::new("usage");
    let out = program(&dir, &["run", "--max-rounds", "0", "t"], Some("1")).output().expect("the program runs");
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("--max-rounds must be at least 1"), "{}", stderr(&out));
    let out = program(&dir, &["--help"], None).output().expect("the program runs");
    assert_eq!(out.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&out.stdout).contains("resume"));
}

#[test]
fn a_ctrl_c_before_the_run_exits_130_with_the_interrupted_note() {
    // herdr is asked for the machine's SSH target, and hangs there until the Ctrl-C stops it.
    let dir = Dir::new("ctrl-c");
    let asked = dir.0.join("asked");
    dir.tool("herdr", &format!("#!/bin/sh\necho \"$@\" > '{}'\nexec sleep 600\n", asked.display()));
    let child = program(&dir, &["resume", "a1b2c3", "--machine", "box", "--cwd", "/p"], None)
        .spawn()
        .expect("the program starts");
    wait_for(&asked);
    let started = Instant::now();
    // The group the program leads, which this test made for it.
    killpg(Pid::from_raw(child.id() as i32), Signal::SIGINT).expect("the program's group is signalled");
    let out = child.wait_with_output().expect("the program ends");
    assert!(started.elapsed() < PATIENCE);
    assert_eq!(std::fs::read_to_string(&asked).unwrap(), "machine list --json\n");
    assert_eq!(out.status.code(), Some(130), "{}", stderr(&out));
    assert_eq!(stderr(&out), "interrupted; the role agents keep running in herdr\n");
}

#[test]
fn a_sigterm_or_a_sighup_before_the_run_ends_the_program_by_it_with_the_interrupted_note() {
    for signal in [Signal::SIGTERM, Signal::SIGHUP] {
        // As in the Ctrl-C's case: herdr hangs until the signal stops it, and leaves its pid.
        let dir = Dir::new(&format!("signal-{}", signal as i32));
        let pid = dir.0.join("pid");
        dir.tool("herdr", &format!("#!/bin/sh\necho $$ > '{}'\nexec sleep 600\n", pid.display()));
        let child = program(&dir, &["resume", "a1b2c3", "--machine", "box", "--cwd", "/p"], None)
            .spawn()
            .expect("the program starts");
        wait_for(&pid);
        let herdr = std::fs::read_to_string(&pid).expect("herdr's pid").trim().to_string();
        let started = Instant::now();
        killpg(Pid::from_raw(child.id() as i32), signal).expect("the program's group is signalled");
        let out = child.wait_with_output().expect("the program ends");
        assert!(started.elapsed() < PATIENCE);
        assert_eq!(out.status.signal(), Some(signal as i32), "{signal}: {:?}, {}", out.status, stderr(&out));
        assert_eq!(stderr(&out), "interrupted; the role agents keep running in herdr\n");
        // herdr ran in a group of its own, which the signal did not reach: the program stopped it.
        assert!(!Path::new(&format!("/proc/{herdr}")).exists(), "herdr outlived the {signal}");
    }
}

#[test]
fn a_resume_of_no_run_names_where_it_looked() {
    // In a herdr pane, the project being the current directory: everything but the run's state is there.
    let dir = Dir::new("no-run");
    let out = program(&dir, &["resume", "b0b0b0"], Some("1")).output().expect("the program runs");
    assert_eq!(out.status.code(), Some(1));
    let project = std::fs::canonicalize(&dir.0).expect("the directory resolves");
    assert_eq!(stderr(&out), format!("error: no run b0b0b0 under {}/.orchestrator/runs\n", project.display()));
}

#[test]
fn a_run_is_named_by_the_time_in_the_machine_s_zone() {
    // herdr fails the run's first command, once its state is saved under its id.
    let dir = Dir::new("run-id");
    dir.tool("herdr", "#!/bin/sh\nexit 1\n");
    // A zone as a POSIX rule, which needs no database: 5:45 east of UTC.
    let zone = "<+0545>-5:45";
    let clock = jiff::tz::TimeZone::posix(zone).expect("a zone");
    let shown = |at: jiff::Timestamp| clock.to_datetime(at).strftime("%Y%m%d-%H%M%S").to_string();
    let before = shown(jiff::Timestamp::now());
    let out = program(&dir, &["run", "--no-pr", "a task"], Some("1")).env("TZ", zone).output().expect("the program runs");
    let after = shown(jiff::Timestamp::now());
    assert_eq!(out.status.code(), Some(1), "{}", stderr(&out));
    let runs: Vec<String> = std::fs::read_dir(dir.0.join(".orchestrator/runs"))
        .expect("the run's state was saved")
        .map(|entry| entry.expect("an entry").file_name().into_string().expect("a UTF-8 name"))
        .collect();
    let [id] = &runs[..] else { panic!("runs: {runs:?}") };
    let time = &id[..15];
    assert!(before.as_str() <= time && time <= after.as_str(), "{id} is not between {before} and {after}");
}

// -- serve: TestServeCommand's process cases, and project_host's (TestProjectHost) through it ---------------------

/// A started serve, its first line read: the process, its url and token, and the rest of its standard output.
struct Serving {
    child: std::process::Child,
    url: String,
    token: String,
    rest: std::io::BufReader<std::process::ChildStdout>,
}

impl Serving {
    /// serve with args, started as program starts it, once it has printed its first line, which must be its one
    /// JSON line with exactly url and token.
    fn start(dir: &Dir, args: &[&str]) -> Serving {
        Serving::start_as(program(dir, &[&["serve"], args].concat(), None))
    }

    fn start_as(mut command: Command) -> Serving {
        use std::io::BufRead;
        let mut child = command.spawn().expect("serve starts");
        let mut rest = std::io::BufReader::new(child.stdout.take().expect("its stdout is piped"));
        let mut line = String::new();
        rest.read_line(&mut line).expect("serve prints its line");
        let first: serde_json::Value = serde_json::from_str(&line).unwrap_or_else(|e| panic!("{line:?}: {e}"));
        let keys: Vec<&String> = first.as_object().expect("an object").keys().collect();
        assert_eq!(keys, ["url", "token"], "{line}");
        let field = |key: &str| first[key].as_str().expect("a string").to_string();
        Serving { child, url: field("url"), token: field("token"), rest }
    }

    fn port(&self) -> u16 {
        self.url.strip_prefix("http://127.0.0.1:").and_then(|p| p.parse().ok()).unwrap_or_else(|| panic!("{}", self.url))
    }

    /// The status and body of a GET of target with the token in its query.
    fn get(&self, target: &str) -> (u16, String) {
        use std::io::{Read, Write};
        let mut conn = std::net::TcpStream::connect(("127.0.0.1", self.port())).expect("serve takes the connection");
        conn.set_read_timeout(Some(PATIENCE)).unwrap();
        let sep = if target.contains('?') { '&' } else { '?' };
        let request = format!(
            "GET {target}{sep}token={} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
            self.token,
            self.port()
        );
        conn.write_all(request.as_bytes()).unwrap();
        let mut reply = String::new();
        let _ = conn.read_to_string(&mut reply);
        let (head, body) = reply.split_once("\r\n\r\n").unwrap_or_else(|| panic!("no reply: {reply:?}"));
        let status = head.split(' ').nth(1).and_then(|s| s.parse().ok()).unwrap_or_else(|| panic!("{head}"));
        (status, body.to_string())
    }

    /// Signal the group serve leads, which this test made for it, and give how it ended and what it wrote after its
    /// first line, on stdout and on stderr.
    fn stop(mut self, signal: Signal) -> (std::process::ExitStatus, String, String) {
        use std::io::Read;
        killpg(Pid::from_raw(self.child.id() as i32), signal).expect("serve's group is signalled");
        let mut out = String::new();
        self.rest.read_to_string(&mut out).expect("serve's stdout is read");
        let mut err = String::new();
        self.child.stderr.take().expect("its stderr is piped").read_to_string(&mut err).expect("serve's stderr is read");
        (self.child.wait().expect("serve ends"), out, err)
    }
}

/// The path a URL's query takes for dir, percent-encoded.
fn encoded(dir: &Path) -> String {
    let unreserved = |b: u8| b.is_ascii_alphanumeric() || b"/-_.".contains(&b);
    let escaped = |b: u8| if unreserved(b) { char::from(b).to_string() } else { format!("%{b:02X}") };
    dir.display().to_string().bytes().map(escaped).collect()
}

#[test]
fn serve_prints_one_json_line_then_serves_until_a_sigint_or_sigterm() {
    // Without a herdr pane: serve needs none.
    for signal in [Signal::SIGTERM, Signal::SIGINT] {
        let dir = Dir::new(&format!("serve-{}", signal as i32));
        let serving = Serving::start(&dir, &["--origin", "tauri://localhost"]);
        assert!(serving.url.starts_with("http://127.0.0.1:"), "{}", serving.url);
        assert_eq!(serving.token.len(), 43);
        let (status, body) = serving.get(&format!("/runs?cwd={}", encoded(&dir.0)));
        assert_eq!((status, body.as_str()), (200, r#"{"version": 1, "runs": []}"#));
        let (ended, out, err) = serving.stop(signal);
        assert_eq!(ended.code(), Some(0), "{signal}: {ended:?}");
        // Nothing more on standard output or standard error: no request is logged, the token least of all.
        assert_eq!((out.as_str(), err.as_str()), ("", ""), "{signal}");
    }
}

#[test]
fn serve_ends_at_a_sighup_by_it() {
    // As SIGHUP's default action ends Python's serve, which handles only SIGINT and SIGTERM.
    let dir = Dir::new("serve-hup");
    let (ended, _, err) = Serving::start(&dir, &[]).stop(Signal::SIGHUP);
    assert_eq!(ended.signal(), Some(Signal::SIGHUP as i32), "{ended:?}: {err}");
}

#[test]
fn serve_stops_at_a_sigint_it_started_with_ignored() {
    // As Python's signal.signal replaces the disposition a Jenkins sh step's SIGINT has: ignored.
    let dir = Dir::new("serve-ignored");
    let command = program(&dir, &["serve"], None);
    let args: Vec<_> = command.get_args().map(|a| a.to_os_string()).collect();
    let mut ignoring = Command::new("env");
    ignoring.args(["--default-signal=TERM,HUP", "--ignore-signal=INT"]).args(&args[1..]);
    for (key, value) in command.get_envs() {
        match value {
            Some(value) => ignoring.env(key, value),
            None => ignoring.env_remove(key),
        };
    }
    ignoring.current_dir(&dir.0).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).process_group(0);
    let (ended, _, err) = Serving::start_as(ignoring).stop(Signal::SIGINT);
    assert_eq!(ended.code(), Some(0), "{ended:?}: {err}");
}

#[test]
fn serve_with_a_closed_stdout_ends_naming_why() {
    // serve's standard output is a pipe whose reader has gone, as when the app quit before reading the line.
    let dir = Dir::new("serve-closed");
    let (reader, writer) = nix::unistd::pipe().expect("a pipe");
    drop(reader);
    let child = program(&dir, &["serve"], None).stdout(Stdio::from(writer)).spawn().expect("serve starts");
    let out = child.wait_with_output().expect("serve ends");
    assert_eq!(out.status.code(), Some(1), "{:?}", out.status);
    assert_eq!(stderr(&out), "error: cannot print the server's address: [Errno 32] Broken pipe\n");
}

#[test]
fn serve_reaches_a_machine_by_its_saved_target() {
    // TestProjectHost: herdr names the machine's SSH target, and ssh, played by a script here, runs each command
    // where the project is; with no machine, herdr is not asked.
    let dir = Dir::new("serve-machine");
    let asked = dir.0.join("asked");
    let machines = r#"[{"label": "box", "id": "m1", "enabled": true, "target": "me@box"}]"#;
    dir.tool("herdr", &format!("#!/bin/sh\necho \"herdr $*\" >> '{}'\necho '{machines}'\n", asked.display()));
    let ssh = format!("#!/bin/sh\nfor a; do last=$a; done\necho \"ssh $*\" >> '{}'\nexec sh -c \"$last\"\n", asked.display());
    dir.tool("ssh", &ssh);
    let project = dir.0.join("proj");
    std::fs::create_dir_all(project.join(".orchestrator/runs/20261024-120000-5e7e00")).unwrap();
    let state = r#"{"run_id": "20261024-120000-5e7e00", "task": "t", "phase": "build", "round": 1}"#;
    std::fs::write(project.join(".orchestrator/runs/20261024-120000-5e7e00/state.json"), state).unwrap();
    let serving = Serving::start(&dir, &[]);
    let (status, body) = serving.get(&format!("/runs?cwd={}", encoded(&project)));
    assert_eq!(status, 200, "{body}");
    assert!(!asked.exists(), "{:?}", std::fs::read_to_string(&asked));
    let (status, body) = serving.get(&format!("/runs?cwd={}&machine=box", encoded(&project)));
    assert_eq!(status, 200, "{body}");
    let record: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(record["runs"][0]["id"], "20261024-120000-5e7e00");
    let log = std::fs::read_to_string(&asked).expect("herdr and ssh were asked");
    let mut lines = log.lines();
    assert_eq!(lines.next(), Some("herdr machine list --json"));
    assert!(lines.all(|l| l.starts_with("ssh -o BatchMode=yes ") && l.contains(" me@box ")), "{log}");
    let (status, body) = serving.get(&format!("/runs?cwd={}&machine=nosuch", encoded(&project)));
    assert_eq!((status, body.as_str()), (502, r#"{"error": "no saved herdr machine named nosuch"}"#));
    let (ended, _, err) = serving.stop(Signal::SIGTERM);
    assert_eq!(ended.code(), Some(0), "{err}");
}
