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
