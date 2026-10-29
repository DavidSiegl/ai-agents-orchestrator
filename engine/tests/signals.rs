//! The engine's SIGINT, SIGTERM and SIGHUP (octoflow_engine::interrupt::by_signals and exit), in processes of its own:
//! a signal to a process signals all of it, and by_signals must be called before any other thread starts, which
//! libtest's own threads rule out. So this test binary has a main of its own (harness = false), which runs as a parent
//! that starts itself again as the engine, in a process group of its own that the parent then signals, as the
//! terminal's Ctrl-C and hangup and the desktop app's Stop signal the engine's group. The parent answers the parts of
//! libtest's command line that cargo test and cargo-nextest use (Cli), and refuses any other option.
//!
//! Linux only, as the engine's other process tests: the engine reads /proc, and GNU env starts the engine with each
//! signal at its default or ignored, whatever the test's own process does (Jenkins runs the tests with SIGINT ignored,
//! nohup with SIGHUP ignored).

use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, Stdio};
use std::time::{Duration, Instant};

use nix::sys::signal::{killpg, Signal};
use nix::unistd::Pid;
use octoflow_engine::host::{Host, ProcessHost};
use octoflow_engine::interrupt::by_signals;
use octoflow_engine::workflow::{Clocks, SystemClocks};

/// Set in the engine's environment, to `<case>:<directory>`: which case it plays, and where it writes what it saw.
const ENGINE: &str = "OCTOFLOW_SIGNALS_ENGINE";
/// How long the parent waits for the engine to get somewhere, before it fails.
const PATIENCE: Duration = Duration::from_secs(30);

/// Each test, by name: the parent's side of a case.
const TESTS: &[(&str, fn())] = &[
    ("each_sigint_raises_the_interrupt_and_stops_the_running_command", each_sigint_raises_the_interrupt),
    ("an_ignored_sigint_stays_ignored", an_ignored_sigint_stays_ignored),
    ("a_sigterm_stops_the_running_command_and_ends_the_engine_by_it", a_sigterm_ends_the_engine_by_it),
    ("a_sighup_stops_the_running_command_and_ends_the_engine_by_it", a_sighup_ends_the_engine_by_it),
    ("an_ignored_sighup_stays_ignored_for_the_engine_and_its_commands", an_ignored_sighup_stays_ignored),
];

fn main() -> ExitCode {
    if let Ok(engine) = std::env::var(ENGINE) {
        let (case, dir) = engine.split_once(':').expect("<case>:<directory>");
        play(case, Path::new(dir));
        return ExitCode::SUCCESS;
    }
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cli) = Cli::parse(&args) else { return ExitCode::from(101) };
    let chosen = TESTS.iter().filter(|(name, _)| cli.runs(name));
    if cli.list {
        for (name, _) in chosen {
            println!("{name}: test");
        }
        return ExitCode::SUCCESS;
    }
    let mut failed = 0;
    for (name, test) in chosen {
        let passed = std::panic::catch_unwind(test).is_ok();
        println!("test {name} ... {}", if passed { "ok" } else { "FAILED" });
        failed += usize::from(!passed);
    }
    if failed == 0 { ExitCode::SUCCESS } else { ExitCode::FAILURE }
}

/// What the command line asks of the parent, in libtest's terms.
struct Cli {
    /// --list: name the tests rather than run them.
    list: bool,
    /// --exact: a filter or a skip is a whole name, not a part of one.
    exact: bool,
    /// --ignored: only the ignored tests, of which there are none.
    ignored_only: bool,
    /// The names, or parts of names, of the tests to run; every test when empty.
    filters: Vec<String>,
    /// --skip's: the tests not run, each a whole name under --exact, else a part of one, as a filter is.
    skips: Vec<String>,
}

impl Cli {
    /// The command line args, after the program's name; None, having said why on stderr, for an option libtest does
    /// not have or that this harness cannot honour.
    fn parse(args: &[String]) -> Option<Cli> {
        let mut cli = Cli { list: false, exact: false, ignored_only: false, filters: vec![], skips: vec![] };
        let mut args = args.iter();
        while let Some(arg) = args.next() {
            let (option, inline) = match arg.split_once('=') {
                Some((option, value)) if arg.starts_with("--") => (option, Some(value.to_string())),
                _ => (arg.as_str(), None),
            };
            let mut value = || {
                let value = inline.clone().or_else(|| args.next().cloned());
                if value.is_none() {
                    eprintln!("signals: {option} takes a value");
                }
                value
            };
            match option {
                "--list" => cli.list = true,
                "--exact" => cli.exact = true,
                "--ignored" => cli.ignored_only = true,
                // Every test runs, one after the other, whatever these say.
                "--include-ignored" | "--nocapture" | "--show-output" | "--quiet" | "-q" => {}
                "--test-threads" | "--format" | "--color" | "-Z" => {
                    value()?;
                }
                "--skip" => cli.skips.push(value()?),
                option if option.starts_with('-') => {
                    eprintln!("signals: {option} is not an option this harness takes");
                    return None;
                }
                _ => cli.filters.push(arg.clone()),
            }
        }
        Some(cli)
    }

    /// Whether the test named name is to be run (or listed).
    fn runs(&self, name: &str) -> bool {
        // As libtest, --exact holds for the skips too.
        let named = |f: &String| if self.exact { f == name } else { name.contains(f.as_str()) };
        !self.ignored_only && (self.filters.is_empty() || self.filters.iter().any(named)) && !self.skips.iter().any(named)
    }
}

/// The engine's side of case, writing what it saw into dir.
fn play(case: &str, dir: &Path) {
    // First, before any thread starts.
    let interrupt = by_signals().expect("the signals handled");
    let host = ProcessHost::local(&interrupt);
    let write = |name: &str, text: &str| std::fs::write(dir.join(name), text).expect("the engine writes");
    let at = |name: &str| dir.join(name).to_str().expect("a UTF-8 path").to_string();
    match case {
        "handled" => {
            // A command the engine starts blocks none of the signals.
            let mask = host.check(&["grep", "^SigBlk:", "/proc/self/status"], None, 60).expect("grep runs");
            write("mask", &mask);
            // The first SIGINT cuts a sleep short.
            write("sleeping", "1");
            SystemClocks::new(&interrupt).expect("the clocks").sleep(600.0);
            write("woke", &format!("{}", by_signals().expect("the same one").is_raised()));
            interrupt.catch();
            // The second stops the command.
            let script = "echo $$ > \"$1\"; exec sleep 600";
            let e = host.run(&["sh", "-c", script, "_", &at("pid")], None, 600).expect_err("the command was stopped");
            write("command", &e.0);
        }
        "ignored" => {
            // The command says which signals it ignores, by SigIgn's mask.
            let script = "echo $$ > \"$1\"; while [ ! -e \"$2\" ]; do sleep 0.05; done; grep ^SigIgn: /proc/self/status";
            let ran = host.check(&["sh", "-c", script, "_", &at("pid"), &at("go")], None, 600).expect("the command ran");
            write("command", &format!("{}, raised: {}", ran.trim(), interrupt.is_raised()));
        }
        "ended" => {
            let script = "echo $$ > \"$1\"; exec sleep 600";
            let e = host.run(&["sh", "-c", script, "_", &at("pid")], None, 600).expect_err("the command was stopped");
            write("command", &e.0);
            // As the program ends: by the signal, whatever the status.
            octoflow_engine::interrupt::exit(0);
        }
        _ => panic!("no case {case}"),
    }
}

/// The engine, playing case in a process group of its own with SIGINT, SIGTERM and SIGHUP at their default but those
/// ignored names, comma-separated as env takes them, writing into dir, its stderr in dir/stderr.
fn engine(case: &str, dir: &Path, ignored: &str) -> Child {
    let stderr = std::fs::File::create(dir.join("stderr")).expect("a file for the engine's stderr");
    Command::new("env")
        .args(["--default-signal=INT,TERM,HUP", &format!("--ignore-signal={ignored}")])
        .arg(std::env::current_exe().expect("this test's path"))
        .env(ENGINE, format!("{case}:{}", dir.display()))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(stderr)
        .process_group(0)
        .spawn()
        .expect("the engine starts")
}

/// signal to the engine's group, which the test made.
fn send(engine: &Child, signal: Signal) {
    killpg(Pid::from_raw(engine.id() as i32), signal).expect("the signal sent");
}

/// Wait for path to have something in it.
fn written(path: &Path) -> String {
    let deadline = Instant::now() + PATIENCE;
    loop {
        if let Ok(text) = std::fs::read_to_string(path) {
            if !text.is_empty() {
                return text;
            }
        }
        assert!(Instant::now() < deadline, "{} was not written", path.display());
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// Wait for the engine to exit, failing with its stderr unless it exits 0; killed if it takes too long.
fn exited(engine: Child, dir: &Path) {
    let (status, stderr) = ended_with(engine, dir);
    assert!(status.success(), "the engine failed, {status}: {stderr}");
}

/// How the engine ended, waited for as long as PATIENCE and killed after, and its stderr.
fn ended_with(mut engine: Child, dir: &Path) -> (std::process::ExitStatus, String) {
    let deadline = Instant::now() + PATIENCE;
    let status = loop {
        if let Some(status) = engine.try_wait().expect("the engine is waited for") {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = killpg(Pid::from_raw(engine.id() as i32), Signal::SIGKILL);
            let _ = engine.wait();
            panic!("the engine did not exit");
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    (status, std::fs::read_to_string(dir.join("stderr")).unwrap_or_default())
}

/// Whether the process of the pid in path has ended (gone, or a zombie), within PATIENCE.
fn ended(path: &Path) -> bool {
    let pid = std::fs::read_to_string(path).expect("a pid file").trim().to_string();
    let deadline = Instant::now() + PATIENCE;
    while Instant::now() < deadline {
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(stat) if !stat.rsplit_once(") ").is_some_and(|(_, rest)| rest.starts_with('Z')) => {}
            _ => return true,
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    false
}

/// A directory of its own for one case, removed when dropped.
struct Scratch(PathBuf);

impl Scratch {
    fn new(case: &str) -> Scratch {
        let dir = std::env::temp_dir().join(format!("octoflow-signals-{case}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("a scratch directory");
        Scratch(dir)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// The mask of a SigIgn or SigBlk line of /proc/<pid>/status, such as `SigBlk:\t0000000000000002`.
fn mask_of(line: &str) -> u64 {
    let hex = line.split_once(':').map_or(line, |(_, hex)| hex);
    u64::from_str_radix(hex.trim(), 16).unwrap_or_else(|_| panic!("{line:?} is no mask"))
}

/// What the "ignored" case's command wrote: the mask of the signals it ignored, and whether the Interrupt was raised.
fn ignored_by_the_command(dir: &Path) -> (u64, String) {
    let command = written(&dir.join("command"));
    let (ignored, raised) = command.split_once(", ").unwrap_or_else(|| panic!("{command:?}"));
    (mask_of(ignored), raised.to_string())
}

/// signal's bit in a mask of /proc/<pid>/status.
fn bit(signal: Signal) -> u64 {
    1 << (signal as i32 - 1)
}

fn each_sigint_raises_the_interrupt() {
    let scratch = Scratch::new("handled");
    let dir = scratch.0.as_path();
    let engine = engine("handled", dir, "");
    written(&dir.join("sleeping"));
    send(&engine, Signal::SIGINT);
    assert_eq!(written(&dir.join("woke")), "true");
    written(&dir.join("pid"));
    send(&engine, Signal::SIGINT);
    exited(engine, dir);
    assert!(written(&dir.join("command")).ends_with(": interrupted"));
    assert!(ended(&dir.join("pid")), "the command outlived the SIGINT");
    let blocked = mask_of(&written(&dir.join("mask")));
    for signal in [Signal::SIGINT, Signal::SIGTERM, Signal::SIGHUP] {
        assert_eq!(blocked & bit(signal), 0, "the command started with {signal} blocked");
    }
}

fn an_ignored_sigint_stays_ignored() {
    let scratch = Scratch::new("ignored");
    let dir = scratch.0.as_path();
    let mut engine = engine("ignored", dir, "INT");
    written(&dir.join("pid"));
    send(&engine, Signal::SIGINT);
    // Longer than a stopped command's grace: had the SIGINT been taken, the command would have failed by now.
    std::thread::sleep(Duration::from_millis(500));
    assert!(engine.try_wait().expect("the engine is waited for").is_none(), "the SIGINT ended the engine");
    std::fs::write(dir.join("go"), "").expect("go written");
    exited(engine, dir);
    let (ignored, raised) = ignored_by_the_command(dir);
    assert_eq!(raised, "raised: false");
    assert_ne!(ignored & bit(Signal::SIGINT), 0, "the command does not ignore SIGINT");
}

/// The engine, its command running, sent signal to its group: the command is stopped, and the engine ends by signal.
fn ends_the_engine_by(signal: Signal) {
    let case = format!("ended-{}", signal as i32);
    let scratch = Scratch::new(&case);
    let dir = scratch.0.as_path();
    let engine = engine("ended", dir, "");
    written(&dir.join("pid"));
    send(&engine, signal);
    let (status, stderr) = ended_with(engine, dir);
    assert_eq!(status.signal(), Some(signal as i32), "the engine ended {status}, not by {signal}: {stderr}");
    assert!(written(&dir.join("command")).ends_with(": interrupted"));
    assert!(ended(&dir.join("pid")), "the command outlived the {signal}");
}

fn a_sigterm_ends_the_engine_by_it() {
    ends_the_engine_by(Signal::SIGTERM);
}

fn a_sighup_ends_the_engine_by_it() {
    ends_the_engine_by(Signal::SIGHUP);
}

fn an_ignored_sighup_stays_ignored() {
    // As nohup starts it.
    let scratch = Scratch::new("hup-ignored");
    let dir = scratch.0.as_path();
    let mut engine = engine("ignored", dir, "HUP");
    written(&dir.join("pid"));
    send(&engine, Signal::SIGHUP);
    // Longer than a stopped command's grace: had the SIGHUP been taken, the command would have failed by now.
    std::thread::sleep(Duration::from_millis(500));
    assert!(engine.try_wait().expect("the engine is waited for").is_none(), "the SIGHUP ended the engine");
    std::fs::write(dir.join("go"), "").expect("go written");
    exited(engine, dir);
    let (ignored, raised) = ignored_by_the_command(dir);
    assert_eq!(raised, "raised: false");
    assert_ne!(ignored & bit(Signal::SIGHUP), 0, "the command does not ignore SIGHUP, as nohup's would");
    assert_eq!(ignored & bit(Signal::SIGINT), 0, "the command ignores SIGINT");
}
