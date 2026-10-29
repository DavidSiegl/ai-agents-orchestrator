//! The engine's SIGINT (octoflow_engine::interrupt::on_sigint), in processes of its own: a SIGINT to a process
//! signals all of it, and on_sigint must be called before any other thread starts, which libtest's own threads rule
//! out. So this test binary has a main of its own (harness = false), which runs as a parent that starts itself again
//! as the engine, in a process group of its own that the parent then signals, as the terminal's Ctrl-C and the
//! desktop app's Stop signal the engine's group. The parent answers the parts of libtest's command line that cargo
//! test and cargo-nextest use (Cli), and refuses any other option.
//!
//! Linux only, as the engine's other process tests: the engine reads /proc, and GNU env starts the engine with SIGINT
//! at its default or ignored, whatever the test's own process does (Jenkins runs the tests with SIGINT ignored).

use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, Stdio};
use std::time::{Duration, Instant};

use nix::sys::signal::{killpg, Signal};
use nix::unistd::Pid;
use octoflow_engine::host::{Host, ProcessHost};
use octoflow_engine::interrupt::on_sigint;
use octoflow_engine::workflow::{Clocks, SystemClocks};

/// Set in the engine's environment, to `<case>:<directory>`: which case it plays, and where it writes what it saw.
const ENGINE: &str = "OCTOFLOW_SIGINT_ENGINE";
/// How long the parent waits for the engine to get somewhere, before it fails.
const PATIENCE: Duration = Duration::from_secs(30);

/// Each test, by name: the parent's side of a case.
const TESTS: &[(&str, fn())] = &[
    ("each_sigint_raises_the_interrupt_and_stops_the_running_command", each_sigint_raises_the_interrupt),
    ("an_ignored_sigint_stays_ignored", an_ignored_sigint_stays_ignored),
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
                    eprintln!("sigint: {option} takes a value");
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
                    eprintln!("sigint: {option} is not an option this harness takes");
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
    let interrupt = on_sigint().expect("SIGINT handled");
    let host = ProcessHost::local(&interrupt);
    let write = |name: &str, text: &str| std::fs::write(dir.join(name), text).expect("the engine writes");
    let at = |name: &str| dir.join(name).to_str().expect("a UTF-8 path").to_string();
    match case {
        "handled" => {
            // A command the engine starts does not block SIGINT.
            let mask = host.check(&["grep", "^SigBlk:", "/proc/self/status"], None, 60).expect("grep runs");
            write("mask", &mask);
            // The first SIGINT cuts a sleep short.
            write("sleeping", "1");
            SystemClocks::new(&interrupt).expect("the clocks").sleep(600.0);
            write("woke", &format!("{}", on_sigint().expect("the same one").is_raised()));
            interrupt.catch();
            // The second stops the command.
            let script = "echo $$ > \"$1\"; exec sleep 600";
            let e = host.run(&["sh", "-c", script, "_", &at("pid")], None, 600).expect_err("the command was stopped");
            write("command", &e.0);
        }
        "ignored" => {
            let script = "echo $$ > \"$1\"; while [ ! -e \"$2\" ]; do sleep 0.05; done; echo done";
            let ran = host.check(&["sh", "-c", script, "_", &at("pid"), &at("go")], None, 600).expect("the command ran");
            write("command", &format!("{}, raised: {}", ran.trim(), interrupt.is_raised()));
        }
        _ => panic!("no case {case}"),
    }
}

/// The engine, playing case in a process group of its own with SIGINT as `env --<signal>=INT` leaves it, writing
/// into dir, its stderr in dir/stderr.
fn engine(case: &str, dir: &Path, signal: &str) -> Child {
    let stderr = std::fs::File::create(dir.join("stderr")).expect("a file for the engine's stderr");
    Command::new("env")
        .arg(format!("--{signal}=INT"))
        .arg(std::env::current_exe().expect("this test's path"))
        .env(ENGINE, format!("{case}:{}", dir.display()))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(stderr)
        .process_group(0)
        .spawn()
        .expect("the engine starts")
}

/// SIGINT to the engine's group, which the test made.
fn sigint(engine: &Child) {
    killpg(Pid::from_raw(engine.id() as i32), Signal::SIGINT).expect("SIGINT sent");
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
fn exited(mut engine: Child, dir: &Path) {
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
    let stderr = std::fs::read_to_string(dir.join("stderr")).unwrap_or_default();
    assert!(status.success(), "the engine failed, {status}: {stderr}");
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
        let dir = std::env::temp_dir().join(format!("octoflow-sigint-{case}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("a scratch directory");
        Scratch(dir)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn each_sigint_raises_the_interrupt() {
    let scratch = Scratch::new("handled");
    let dir = scratch.0.as_path();
    let engine = engine("handled", dir, "default-signal");
    written(&dir.join("sleeping"));
    sigint(&engine);
    assert_eq!(written(&dir.join("woke")), "true");
    written(&dir.join("pid"));
    sigint(&engine);
    exited(engine, dir);
    assert!(written(&dir.join("command")).ends_with(": interrupted"));
    assert!(ended(&dir.join("pid")), "the command outlived the SIGINT");
    // Bit 1 of the mask is SIGINT, signal 2.
    let mask = written(&dir.join("mask"));
    let mask = u64::from_str_radix(mask.trim_start_matches("SigBlk:").trim(), 16).expect("a mask");
    assert_eq!(mask & 2, 0, "the command started with SIGINT blocked");
}

fn an_ignored_sigint_stays_ignored() {
    let scratch = Scratch::new("ignored");
    let dir = scratch.0.as_path();
    let mut engine = engine("ignored", dir, "ignore-signal");
    written(&dir.join("pid"));
    sigint(&engine);
    // Longer than a stopped command's grace: had the SIGINT been taken, the command would have failed by now.
    std::thread::sleep(Duration::from_millis(500));
    assert!(engine.try_wait().expect("the engine is waited for").is_none(), "the SIGINT ended the engine");
    std::fs::write(dir.join("go"), "").expect("go written");
    exited(engine, dir);
    assert_eq!(written(&dir.join("command")), "done, raised: false");
}
