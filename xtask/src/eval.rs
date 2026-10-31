//! `cargo xtask eval [--tasks a,b] [--repeat N] [--config NAME] [--keep] [-- RUN_ARGS...]`: run the eval set. Each
//! task under evals/tasks/ goes to the engine, a hidden test judges what its agents made, and the numbers are printed
//! and kept. docs/design.md, Evals, says when to run it and what the numbers mean.
//!
//! Run it from a herdr pane, as `orchestrator run`: every trial is a real run, with real agents. RUN_ARGS, after `--`,
//! are given to the engine's `run` unchanged, such as `--workflow quick --model sonnet`; the runner sets `--no-pr`,
//! `--spec` and `--cwd` itself, and refuses those, `--machine` and `--worktree` there ([`OWN_FLAGS`]).
//!
//! The engine is the command line `EVAL_ENGINE` gives, else this checkout's own, built first ([`built_engine`]), so
//! that a trial never runs a binary older than the commit it is recorded under. It is run as a program, through `run`
//! and `close`, and must keep a run's files as the orchestrator does: the runner finds the run under
//! `.orchestrator/runs/<run id>/` in the clone, and its numbers in `metrics.json` there (docs/design.md, Run metrics).
//!
//! A task is a directory evals/tasks/<name>/ holding:
//!
//! - task.md, the spec the run starts from, as `run --spec` takes one: a workflow whose first step writes the spec,
//!   such as `default`, skips its interview;
//! - repo.bundle, a git bundle of the toy repository, a Rust crate, at its starting commit, on main (`cargo xtask
//!   bundles`, crate::bundles);
//! - hidden_test.rs, the acceptance test: after the run, the runner copies it into the clone's `tests/` and runs
//!   `cargo test --no-fail-fast` there, so that the crate's own tests run beside it; exit status 0 accepts the change.
//!   It may read the clone's origin/main, the starting commit, which the run never moves.
//!
//! Exit status: 0 when every trial ran, whatever its tests said; 1 for an error of the runner's own, such as an unknown
//! task or a bundle that cannot be cloned; 2 for a usage error; 130 when interrupted, after the trials before.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::fd::AsFd;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Child, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::process::{command, interrupted, make_temp_dir, watch_interrupts};
use crate::Error;

/// Where the tasks are, and where the result lines go, relative to the checkout's top.
const TASKS_SUBDIR: &str = "evals/tasks";
const RESULTS_SUBDIR: &str = "evals/results";
/// The version of a result line's shape (Eval::record), raised when a key changes its meaning or goes. 2 since the
/// tasks became Rust crates, whose numbers start a new series.
pub const RECORD_VERSION: u64 = 2;
/// Where a run keeps its files in the project it runs on, one directory per run named by its run id.
const RUNS_DIR: &str = ".orchestrator/runs";
/// How long a hidden test may take, with the crate's own tests and their build, and how many of the last lines of
/// its output a result line keeps.
const HIDDEN_TEST_TIMEOUT: Duration = Duration::from_secs(600);
pub const HIDDEN_TEST_TAIL_LINES: usize = 15;
/// The engine's run flags the runner sets, or whose run would not leave its change in the clone, which it tests.
pub const OWN_FLAGS: [&str; 5] = ["--no-pr", "--spec", "--cwd", "--machine", "--worktree"];
const EXIT_INTERRUPTED: i32 = 130;
/// How a result line names the engine a trial ran without EVAL_ENGINE: this checkout's own build.
pub const DEFAULT_ENGINE: &str = "this checkout's build";
const USAGE: &str = "usage: cargo xtask eval [--tasks a,b] [--repeat N] [--config NAME] [--keep] [-- RUN_ARGS...]";
/// The variables by which a cargo started from a cargo build, such as cargo-llvm-cov's, would build the toy crate
/// with that build's flags, or into its target directory, rather than as a project of its own.
const CARGO_BUILD_ENV: &[&str] = &[
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "RUSTDOCFLAGS",
    "CARGO_ENCODED_RUSTDOCFLAGS",
    "RUSTC_WRAPPER",
    "RUSTC_WORKSPACE_WRAPPER",
    "CARGO_BUILD_TARGET_DIR",
    "CARGO_LLVM_COV",
    "CARGO_LLVM_COV_TARGET_DIR",
    "LLVM_PROFILE_FILE",
];

/// An eval task: the directory evals/tasks/<name>/ holding its task.md, repo.bundle and hidden_test.rs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Task {
    pub name: String,
    pub dir: PathBuf,
}

impl Task {
    pub fn spec(&self) -> PathBuf {
        self.dir.join("task.md")
    }

    pub fn bundle(&self) -> PathBuf {
        self.dir.join(crate::bundles::BUNDLE_NAME)
    }

    pub fn hidden_test(&self) -> PathBuf {
        self.dir.join("hidden_test.rs")
    }
}

/// The four token counts of a run's agent turns, summed (docs/design.md, Token usage).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Tokens {
    pub input: u64,
    pub output: u64,
    pub cache_read: u64,
    pub cache_write: u64,
}

/// What one run of one task came to. Each field that metrics.json gives is None when the run wrote none, as a run
/// that failed or was interrupted does not.
#[derive(Debug, Clone, PartialEq)]
pub struct Trial {
    pub task: String,
    /// Which run of the task in this eval, from 1.
    pub repeat: u32,
    /// Whether the hidden test accepted what the run left in the clone.
    pub passed: bool,
    /// The exit status of the engine's run; minus the signal's number when a signal ended it.
    pub engine_exit: i32,
    /// Wall time of the engine's run, in seconds, measured by the runner.
    pub seconds: f64,
    /// The last HIDDEN_TEST_TAIL_LINES lines of the hidden test's output; when it did not accept, the last of them
    /// names the tests that failed.
    pub hidden_test_output: String,
    /// The run's directory under RUNS_DIR; None when the engine made none.
    pub run_id: Option<String>,
    /// metrics.json's: the verdict, "error" or "unfinished".
    pub outcome: Option<String>,
    pub rounds: Option<u64>,
    /// How many quality builds judged the change, over every round.
    pub quality_rounds: Option<u64>,
    /// Summed over every agent turn; None also when a single turn's are unknown, so that a sum never stands for less
    /// than the whole run.
    pub tokens: Option<Tokens>,
}

/// Why a trial, and with it the eval, stopped before it was judged.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Halt {
    /// An error of the runner's own.
    Failed(Error),
    /// The human's Ctrl-C, once the engine or the hidden test it reached has stopped; the trial's directory is kept.
    Interrupted,
}

impl From<Error> for Halt {
    fn from(e: Error) -> Halt {
        Halt::Failed(e)
    }
}

/// The directory of a checkout's eval tasks.
pub fn tasks_dir(checkout: &Path) -> PathBuf {
    checkout.join(TASKS_SUBDIR)
}

/// The tasks named, in that order, or every task in tasks_dir in name order when names is None. Errors: a name with
/// no task directory, or one missing a file a task needs; tasks_dir cannot be listed.
pub fn load_tasks(names: Option<&[String]>, tasks_dir: &Path) -> Result<Vec<Task>, Error> {
    let names = match names {
        Some(names) => names.to_vec(),
        None => {
            let listed = |e: io::Error| Error(format!("could not list the tasks in {}: {e}", tasks_dir.display()));
            let mut names = vec![];
            for entry in fs::read_dir(tasks_dir).map_err(listed)? {
                let entry = entry.map_err(listed)?;
                if entry.path().is_dir() {
                    names.push(entry.file_name().to_string_lossy().into_owned());
                }
            }
            names.sort();
            names
        }
    };
    let tasks: Vec<Task> = names.into_iter().map(|name| Task { dir: tasks_dir.join(&name), name }).collect();
    for task in &tasks {
        let missing: Vec<String> = [task.spec(), task.bundle(), task.hidden_test()]
            .iter()
            .filter(|path| !path.is_file())
            .map(|path| path.file_name().unwrap_or_default().to_string_lossy().into_owned())
            .collect();
        if !missing.is_empty() {
            return Err(Error(format!("task {}: {} missing in {}", task.name, missing.join(", "), task.dir.display())));
        }
    }
    Ok(tasks)
}

/// The engine every trial runs: eval_engine, EVAL_ENGINE's value, split as a shell splits words, when it is set and
/// not empty; else this checkout's own, as [`built_engine`] builds it. Errors: eval_engine has an unclosed quote, or
/// the build failed.
pub fn engine_command(eval_engine: Option<&str>, checkout: &Path) -> Result<Vec<String>, Error> {
    match eval_engine.filter(|command| !command.is_empty()) {
        Some(command) => {
            split_words(command).ok_or_else(|| Error(format!("EVAL_ENGINE has an unclosed quote: {command}")))
        }
        None => Ok(vec![built_engine(checkout)?.to_string_lossy().into_owned()]),
    }
}

/// command split into words as a POSIX shell splits it, quotes and backslashes removed, without expansions; None
/// when a quote is not closed.
fn split_words(command: &str) -> Option<Vec<String>> {
    let mut words = vec![];
    let mut word: Option<String> = None;
    let mut chars = command.chars();
    while let Some(c) = chars.next() {
        match c {
            c if c.is_whitespace() => words.extend(word.take()),
            '\'' => {
                let w = word.get_or_insert_with(String::new);
                loop {
                    match chars.next()? {
                        '\'' => break,
                        c => w.push(c),
                    }
                }
            }
            '"' => {
                let w = word.get_or_insert_with(String::new);
                loop {
                    match chars.next()? {
                        '"' => break,
                        '\\' => match chars.next()? {
                            c @ ('"' | '\\' | '$' | '`') => w.push(c),
                            '\n' => {}
                            c => {
                                w.push('\\');
                                w.push(c);
                            }
                        },
                        c => w.push(c),
                    }
                }
            }
            '\\' => {
                let w = word.get_or_insert_with(String::new);
                match chars.next() {
                    Some('\n') => {}
                    Some(c) => w.push(c),
                    None => w.push('\\'),
                }
            }
            c => word.get_or_insert_with(String::new).push(c),
        }
    }
    words.extend(word);
    Some(words)
}

/// The path of checkout's orchestrator binary, `cargo build --locked -p octoflow-engine --bin orchestrator` run there
/// first, wherever its target directory is. Errors: cargo is missing or fails, or reports no such binary.
pub fn built_engine(checkout: &Path) -> Result<PathBuf, Error> {
    let output = command("cargo")
        .args(["build", "--locked", "--quiet", "-p", "octoflow-engine", "--bin", "orchestrator"])
        .arg("--message-format=json")
        .current_dir(checkout)
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| match e.kind() {
            io::ErrorKind::NotFound => {
                Error("cargo, which builds the engine, is not on PATH; set EVAL_ENGINE to run another".to_string())
            }
            _ => Error(format!("could not run cargo to build the engine: {e}")),
        })?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(Error(format!("cargo could not build the engine: {}", stderr.trim())));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let paths: Vec<String> = stdout
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|message| message["reason"] == "compiler-artifact")
        .filter_map(|message| message["executable"].as_str().map(str::to_string))
        .collect();
    match paths.as_slice() {
        [path] => Ok(PathBuf::from(path)),
        _ => Err(Error(format!("cargo reported {} binaries for the engine, not one", paths.len()))),
    }
}

/// A name for the configuration run_args give, for the results file's: "default" for none, else the arguments' ASCII
/// letters and digits, lowercased, in runs joined by "-", at most 60 characters.
pub fn config_name(run_args: &[String]) -> String {
    let joined = run_args.join(" ").to_lowercase();
    let words: Vec<&str> =
        joined.split(|c: char| !(c.is_ascii_lowercase() || c.is_ascii_digit())).filter(|w| !w.is_empty()).collect();
    let name: String = words.join("-").chars().take(60).collect();
    match name.trim_matches('-') {
        "" => "default".to_string(),
        name => name.to_string(),
    }
}

/// Run task once: clone its bundle into a directory of its own under the temporary directory, have engine run its
/// spec there with run_args and without a pull request, close the run's workspace, run the hidden test on what the
/// run left, and remove the directory unless keep, saying where it is when kept.
///
/// Neither the hidden test nor the task's directory is reachable from the clone: the spec is a copy beside it, and
/// its origin a copy of the bundle. The engine's output goes to this process's standard error, the hidden test's into
/// the result. Errors: [`Halt::Failed`] when the bundle cannot be cloned or the hidden test cannot be started;
/// [`Halt::Interrupted`] after a Ctrl-C, once the engine has stopped, leaving the directory for a resume.
pub fn run_trial(task: &Task, repeat: u32, engine: &[String], run_args: &[String], keep: bool) -> Result<Trial, Halt> {
    let work = make_temp_dir(&format!("octoflow-eval-{}-", task.name))
        .map_err(|e| Error(format!("task {}: could not make its directory: {e}", task.name)))?;
    let repo = work.join("repo");
    let trial = judged_trial(task, repeat, engine, run_args, &work, &repo);
    if trial == Err(Halt::Interrupted) {
        eprintln!("interrupted: the run's clone stays in {}", repo.display());
    } else if keep {
        eprintln!("{} #{repeat}: kept in {}", task.name, work.display());
    } else if let Err(e) = fs::remove_dir_all(&work) {
        eprintln!("warning: could not remove {}: {e}", work.display());
    }
    trial
}

/// run_trial's trial, in work, with the clone at repo, which it leaves there.
fn judged_trial(
    task: &Task,
    repeat: u32,
    engine: &[String],
    run_args: &[String],
    work: &Path,
    repo: &Path,
) -> Result<Trial, Halt> {
    let spec = isolated_inputs(task, work, repo)?;
    let started = Instant::now();
    let mut argv: Vec<String> = engine.to_vec();
    argv.extend(["run", "--no-pr", "--spec"].map(String::from));
    argv.push(spec.to_string_lossy().into_owned());
    argv.push("--cwd".to_string());
    argv.push(repo.to_string_lossy().into_owned());
    argv.extend(run_args.iter().cloned());
    let engine_exit = run_engine(&argv)?;
    let seconds = (started.elapsed().as_secs_f64() * 10.0).round() / 10.0;
    let known = run_metrics(repo);
    match &known.run_id {
        Some(run_id) => close(engine, run_id, repo),
        None => eprintln!("warning: the engine left no run directory under {RUNS_DIR} in {}", repo.display()),
    }
    let (passed, hidden_test_output) = run_hidden_test(task, repo, work)?;
    Ok(Trial {
        task: task.name.clone(),
        repeat,
        passed,
        engine_exit,
        seconds,
        hidden_test_output,
        run_id: known.run_id,
        outcome: known.outcome,
        rounds: known.rounds,
        quality_rounds: known.quality_rounds,
        tokens: known.tokens,
    })
}

/// Clone task's bundle into repo, its origin a copy of the bundle in work, and copy its spec into work; return the
/// copy's path. Errors: either failed.
fn isolated_inputs(task: &Task, work: &Path, repo: &Path) -> Result<PathBuf, Error> {
    let bundle = work.join("origin.bundle");
    let spec = work.join("task.md");
    let setup =
        |e: io::Error| Error(format!("task {}: could not set up its clone in {}: {e}", task.name, work.display()));
    fs::copy(task.bundle(), &bundle).map_err(setup)?;
    fs::copy(task.spec(), &spec).map_err(setup)?;
    let output = command("git").args(["clone", "--quiet"]).arg(&bundle).arg(repo).output().map_err(setup)?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(Error(format!(
            "task {}: could not clone {}: {}",
            task.name,
            task.bundle().display(),
            stderr.trim()
        )));
    }
    Ok(spec)
}

/// The exit status of argv, run with its output on this process's standard error, so that standard output stays the
/// table's. A Ctrl-C reaches the engine too, in the terminal's foreground group, and it saves its run's state as it
/// stops; once it has, a Ctrl-C makes this [`Halt::Interrupted`].
fn run_engine(argv: &[String]) -> Result<i32, Halt> {
    let start =
        |e: io::Error| Error(format!("could not start the engine, {}: {e}", argv[..2.min(argv.len())].join(" ")));
    let stderr = io::stderr().as_fd().try_clone_to_owned().map_err(start)?;
    let status = command(&argv[0]).args(&argv[1..]).stdout(Stdio::from(stderr)).status().map_err(start)?;
    if interrupted() {
        return Err(Halt::Interrupted);
    }
    Ok(exit_code(status))
}

/// status as a number: the exit status, or minus the number of the signal that ended the process, as Python's
/// subprocess gives it.
fn exit_code(status: ExitStatus) -> i32 {
    status.code().unwrap_or_else(|| -status.signal().unwrap_or(0))
}

/// Whether task's hidden test accepts repo, and the last lines of its output: hidden_test.rs copied into repo's
/// tests/, and `cargo test --no-fail-fast` run there with its build in work, its output in work's hidden_test.log. A
/// hidden test that runs over HIDDEN_TEST_TIMEOUT fails. Errors: [`Halt::Failed`] when it cannot be started;
/// [`Halt::Interrupted`] at a Ctrl-C, once it has been stopped.
fn run_hidden_test(task: &Task, repo: &Path, work: &Path) -> Result<(bool, String), Halt> {
    let failed = |e: io::Error| Error(format!("task {}: could not run its hidden test: {e}", task.name));
    let tests = repo.join("tests");
    fs::create_dir_all(&tests).map_err(failed)?;
    fs::copy(task.hidden_test(), tests.join("hidden_test.rs")).map_err(failed)?;
    let log_path = work.join("hidden_test.log");
    let log = File::create(&log_path).map_err(failed)?;
    let mut cargo = command("cargo");
    cargo.args(["test", "--no-fail-fast"]).current_dir(repo).env("CARGO_TARGET_DIR", work.join("target"));
    for name in CARGO_BUILD_ENV {
        cargo.env_remove(name);
    }
    // A group of its own, so that a timeout or a Ctrl-C ends the test binaries cargo started, not cargo alone.
    cargo.process_group(0).stdin(Stdio::null()).stdout(log.try_clone().map_err(failed)?).stderr(log);
    let child = cargo.spawn().map_err(failed)?;
    let status = match wait_for_group(child, HIDDEN_TEST_TIMEOUT).map_err(failed)? {
        Waited::Exited(status) => status,
        Waited::TimedOut => return Ok((false, format!("the hidden test ran over {}s", HIDDEN_TEST_TIMEOUT.as_secs()))),
        Waited::Interrupted => return Err(Halt::Interrupted),
    };
    let text = fs::read(&log_path).map_err(failed)?;
    Ok((status.success(), test_output(&String::from_utf8_lossy(&text), status.success())))
}

/// How a command started in a process group of its own ended.
enum Waited {
    Exited(ExitStatus),
    /// It ran over its time, and its group was killed.
    TimedOut,
    /// A Ctrl-C arrived while it ran, and its group was killed.
    Interrupted,
}

/// How child, the leader of a process group of its own, ends: polled for until timeout passes or a Ctrl-C arrives,
/// when its group is killed and waited for. Errors: polling or waiting for it failed.
fn wait_for_group(mut child: Child, timeout: Duration) -> io::Result<Waited> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(Waited::Exited(status));
        }
        let stopped = if interrupted() {
            Waited::Interrupted
        } else if Instant::now() >= deadline {
            Waited::TimedOut
        } else {
            std::thread::sleep(Duration::from_millis(50));
            continue;
        };
        let group = nix::unistd::Pid::from_raw(i32::try_from(child.id()).unwrap_or(i32::MAX));
        // The group may have ended since try_wait; then there is nothing to kill.
        let _ = nix::sys::signal::killpg(group, nix::sys::signal::Signal::SIGKILL);
        child.wait()?;
        return Ok(stopped);
    }
}

/// The last HIDDEN_TEST_TAIL_LINES lines of a hidden test's output text; when it did not pass, the last of them
/// names the tests that failed, from cargo test's `test NAME ... FAILED` lines, or says that none failed, as when the
/// tests did not build.
pub fn test_output(text: &str, passed: bool) -> String {
    let mut lines: Vec<&str> = text.trim_end_matches('\n').split('\n').collect();
    let verdict;
    if !passed {
        let failed: Vec<&str> =
            lines.iter().filter_map(|line| line.strip_prefix("test ")?.strip_suffix(" ... FAILED")).collect();
        verdict = match failed.as_slice() {
            [] => "not accepted: no test failed, so the tests did not build or cargo test failed".to_string(),
            names => format!("not accepted: {}", names.join(", ")),
        };
        lines.push(&verdict);
    }
    lines[lines.len().saturating_sub(HIDDEN_TEST_TAIL_LINES)..].join("\n")
}

/// What a run in a clone tells a Trial, each None that a missing or unreadable directory or file leaves unknown.
#[derive(Default)]
struct RunMetrics {
    run_id: Option<String>,
    outcome: Option<String>,
    rounds: Option<u64>,
    quality_rounds: Option<u64>,
    tokens: Option<Tokens>,
}

/// The run in repo: its run id, the name of the last run directory there in name order, and what its metrics.json
/// says (docs/design.md, Run metrics), a value of the wrong type taken as missing.
fn run_metrics(repo: &Path) -> RunMetrics {
    let mut known = RunMetrics::default();
    let Ok(entries) = fs::read_dir(repo.join(RUNS_DIR)) else { return known };
    let mut run_ids: Vec<String> =
        entries.filter_map(Result::ok).map(|e| e.file_name().to_string_lossy().into()).collect();
    run_ids.sort();
    known.run_id = run_ids.pop();
    let Some(run_id) = &known.run_id else { return known };
    let Ok(text) = fs::read_to_string(repo.join(RUNS_DIR).join(run_id).join("metrics.json")) else { return known };
    let Ok(metrics) = serde_json::from_str::<Value>(&text) else { return known };
    known.outcome = metrics["outcome"].as_str().map(str::to_string);
    known.rounds = metrics["rounds"].as_u64();
    known.quality_rounds = match &metrics["quality_rounds"] {
        Value::Array(rounds) => Some(rounds.len() as u64),
        Value::Null => Some(0),
        _ => None,
    };
    let turns = metrics["turns"].as_array().map(Vec::as_slice).unwrap_or_default();
    let agent_turns: Vec<&Value> = turns.iter().filter(|turn| is_truthy(&turn["harness"])).collect();
    known.tokens = (!agent_turns.is_empty())
        .then(|| agent_turns.iter().map(|turn| tokens(&turn["tokens"])).sum::<Option<Tokens>>())
        .flatten();
    known
}

/// Whether value names something, as Python's truth test takes a harness: not null, false, "" or 0.
fn is_truthy(value: &Value) -> bool {
    !matches!(value, Value::Null | Value::Bool(false)) && value != "" && value != 0
}

/// A turn's token counts, a missing count taken as 0; None when they are not an object of counts, as "unknown".
fn tokens(value: &Value) -> Option<Tokens> {
    let counts = value.as_object()?;
    let count = |key: &str| counts.get(key).map_or(Some(0), Value::as_u64);
    Some(Tokens {
        input: count("input")?,
        output: count("output")?,
        cache_read: count("cache_read")?,
        cache_write: count("cache_write")?,
    })
}

impl std::iter::Sum for Tokens {
    fn sum<I: Iterator<Item = Tokens>>(iter: I) -> Tokens {
        iter.fold(Tokens::default(), |a, b| Tokens {
            input: a.input + b.input,
            output: a.output + b.output,
            cache_read: a.cache_read + b.cache_read,
            cache_write: a.cache_write + b.cache_write,
        })
    }
}

impl Tokens {
    /// The four counts added up.
    pub fn total(&self) -> u64 {
        self.input + self.output + self.cache_read + self.cache_write
    }
}

/// Close the run's herdr workspace, which a --no-pr run leaves open; a failure is only reported.
fn close(engine: &[String], run_id: &str, repo: &Path) {
    let output = command(&engine[0]).args(&engine[1..]).args(["close", run_id, "--cwd"]).arg(repo).output();
    match output {
        Err(e) => eprintln!("warning: could not close run {run_id}: {e}"),
        Ok(output) if !output.status.success() => {
            eprintln!("warning: could not close run {run_id}: {}", String::from_utf8_lossy(&output.stderr).trim());
        }
        Ok(_) => {}
    }
}

/// One invocation of the runner: what its result lines share.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Eval {
    /// The day it started, YYYY-MM-DD, in local time.
    pub date: String,
    /// The name of its configuration, config_name's or --config's.
    pub config: String,
    /// What the engine's run was given beyond the runner's own flags.
    pub run_args: Vec<String>,
    /// EVAL_ENGINE, or DEFAULT_ENGINE for this checkout's.
    pub engine: String,
    /// The checkout's HEAD, "-dirty" after it with tracked changes: what the default engine ran with.
    pub commit: String,
}

impl Eval {
    /// <results_dir>/<date>-<config>.jsonl, where its result lines go.
    pub fn results_path(&self, results_dir: &Path) -> PathBuf {
        results_dir.join(format!("{}-{}.jsonl", self.date, self.config))
    }

    /// The result line of trial, without its line end: this eval's fields, then trial's, in that order.
    /// docs/design.md, Evals, gives every key.
    pub fn record(&self, trial: &Trial) -> String {
        let tokens = trial.tokens.map(
            |t| json!({"input": t.input, "output": t.output, "cache_read": t.cache_read, "cache_write": t.cache_write}),
        );
        let pairs: [(&str, Value); 17] = [
            ("version", json!(RECORD_VERSION)),
            ("date", json!(self.date)),
            ("config", json!(self.config)),
            ("run_args", json!(self.run_args)),
            ("engine", json!(self.engine)),
            ("commit", json!(self.commit)),
            ("task", json!(trial.task)),
            ("repeat", json!(trial.repeat)),
            ("passed", json!(trial.passed)),
            ("engine_exit", json!(trial.engine_exit)),
            ("seconds", json!(trial.seconds)),
            ("hidden_test_output", json!(trial.hidden_test_output)),
            ("run_id", json!(trial.run_id)),
            ("outcome", json!(trial.outcome)),
            ("rounds", json!(trial.rounds)),
            ("quality_rounds", json!(trial.quality_rounds)),
            ("tokens", json!(tokens)),
        ];
        let fields: Vec<String> = pairs.iter().map(|(key, value)| format!("{}: {value}", json!(key))).collect();
        format!("{{{}}}", fields.join(", "))
    }
}

/// Append lines to the file at path, each with a line end, making its directory. Errors: it could not.
pub fn append_records(path: &Path, lines: &[String]) -> io::Result<()> {
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)?;
    }
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    let text: String = lines.iter().map(|line| format!("{line}\n")).collect();
    file.write_all(text.as_bytes())
}

/// A table of trials, a row per task in the order they first appear and one for them all when there are several:
/// the runs, how many passed, and the means of rounds, quality rounds, wall time and tokens (all four counts
/// summed), each over the trials that know it, "?" when none does; a mean over fewer than all the row's runs says
/// over how many. Each line ends with a line end.
pub fn table(trials: &[Trial]) -> String {
    let mut groups: Vec<(&str, Vec<&Trial>)> = vec![];
    for trial in trials {
        match groups.iter_mut().find(|(name, _)| *name == trial.task) {
            Some((_, group)) => group.push(trial),
            None => groups.push((&trial.task, vec![trial])),
        }
    }
    if groups.len() > 1 {
        groups.push(("all", trials.iter().collect()));
    }
    let head = ["task", "runs", "passed", "rounds", "quality rounds", "wall time", "tokens"].map(String::from);
    let mut rows = vec![head.to_vec()];
    for (name, group) in &groups {
        let passed = group.iter().filter(|t| t.passed).count();
        rows.push(vec![
            name.to_string(),
            group.len().to_string(),
            format!("{passed}/{}", group.len()),
            mean(group.iter().map(|t| t.rounds.map(|n| n as f64)), tenths),
            mean(group.iter().map(|t| t.quality_rounds.map(|n| n as f64)), tenths),
            mean(group.iter().map(|t| Some(t.seconds)), duration),
            mean(group.iter().map(|t| t.tokens.map(|tokens| tokens.total() as f64)), count),
        ]);
    }
    let widths: Vec<usize> = (0..head.len()).map(|i| rows.iter().map(|row| row[i].len()).max().unwrap_or(0)).collect();
    rows.iter()
        .map(|row| {
            let cells: Vec<String> = row
                .iter()
                .zip(&widths)
                .enumerate()
                .map(|(i, (cell, &w))| if i == 0 { format!("{cell:<w$}") } else { format!("{cell:>w$}") })
                .collect();
            cells.join("  ").trim_end().to_string() + "\n"
        })
        .collect()
}

/// The mean of the values that are not None, as show writes it; "?" when all are None, and " (k/n)" after it when
/// only k of the n are known.
fn mean(values: impl Iterator<Item = Option<f64>>, show: fn(f64) -> String) -> String {
    let values: Vec<Option<f64>> = values.collect();
    let known: Vec<f64> = values.iter().flatten().copied().collect();
    if known.is_empty() {
        return "?".to_string();
    }
    let text = show(known.iter().sum::<f64>() / known.len() as f64);
    if known.len() == values.len() {
        text
    } else {
        format!("{text} ({}/{})", known.len(), values.len())
    }
}

fn tenths(x: f64) -> String {
    format!("{x:.1}")
}

fn duration(seconds: f64) -> String {
    let whole = seconds.round_ties_even() as u64;
    format!("{}m{:02}s", whole / 60, whole % 60)
}

fn count(n: f64) -> String {
    if n >= 1e6 {
        format!("{:.1}M", n / 1e6)
    } else if n >= 1e3 {
        format!("{:.1}k", n / 1e3)
    } else {
        format!("{n:.0}")
    }
}

/// checkout's HEAD, the prompts and workflows the default engine runs with, followed by "-dirty" when a file git
/// tracks differs from it, so that numbers of an uncommitted edit are not credited to the commit without it; the
/// results files, which an eval of earlier the same day appended to, are no change. "unknown" when git cannot say.
pub fn head_commit(checkout: &Path) -> String {
    let git = |args: &[&str]| command("git").arg("-C").arg(checkout).args(args).output();
    let exclude = format!(":(exclude){RESULTS_SUBDIR}");
    let (Ok(head), Ok(changed)) = (git(&["rev-parse", "HEAD"]), git(&["diff", "--quiet", "HEAD", "--", ".", &exclude]))
    else {
        return "unknown".to_string();
    };
    match (head.status.success(), changed.status.code()) {
        (true, Some(0)) => String::from_utf8_lossy(&head.stdout).trim().to_string(),
        (true, Some(1)) => format!("{}-dirty", String::from_utf8_lossy(&head.stdout).trim()),
        _ => "unknown".to_string(),
    }
}

/// The runner's command line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    /// The tasks to run, by name, in order; None for every task.
    pub tasks: Option<Vec<String>>,
    /// Runs of each task, at least 1.
    pub repeat: u32,
    /// The name of the results file's configuration; None to take config_name's.
    pub config: Option<String>,
    /// Keep each trial's directory, and say where.
    pub keep: bool,
    /// RUN_ARGS, those after the first `--`.
    pub run_args: Vec<String>,
}

/// The runner's arguments; RUN_ARGS, those after the first `--`, as run_args. Errors: a usage error, as its message.
pub fn parse_args(argv: &[String]) -> Result<Args, String> {
    let (own, run_args) = match argv.iter().position(|arg| arg == "--") {
        Some(i) => (&argv[..i], argv[i + 1..].to_vec()),
        None => (argv, vec![]),
    };
    let mut args = Args { tasks: None, repeat: 1, config: None, keep: false, run_args };
    let mut own = own.iter();
    while let Some(arg) = own.next() {
        let (flag, inline) = match arg.split_once('=') {
            Some((flag, value)) if flag.starts_with("--") => (flag, Some(value.to_string())),
            _ => (arg.as_str(), None),
        };
        let mut value = || inline.clone().or_else(|| own.next().cloned()).ok_or(format!("{flag} takes a value"));
        match flag {
            "--tasks" => args.tasks = Some(value()?.split(',').map(String::from).collect()),
            "--repeat" => {
                let text = value()?;
                args.repeat = text.parse().map_err(|_| format!("--repeat takes a number, not {text:?}"))?;
            }
            "--config" => args.config = Some(value()?),
            "--keep" if inline.is_none() => args.keep = true,
            _ => return Err(format!("unrecognized argument: {arg}")),
        }
    }
    if args.repeat < 1 {
        return Err("--repeat must be at least 1".to_string());
    }
    // By prefix too: the engine's command line takes --workt for --worktree.
    let refused: Vec<&str> = args
        .run_args
        .iter()
        .map(|arg| arg.split('=').next().unwrap_or(arg))
        .filter(|flag| flag.starts_with("--") && flag.len() > 2 && OWN_FLAGS.iter().any(|own| own.starts_with(flag)))
        .collect();
    if !refused.is_empty() {
        return Err(format!(
            "RUN_ARGS cannot hold {}: the runner sets or refuses {}",
            refused.join(", "),
            OWN_FLAGS.join(", ")
        ));
    }
    Ok(args)
}

/// The command line `cargo xtask eval ...`, on this checkout, with EVAL_ENGINE's engine, a Ctrl-C ending it as the
/// module's comment says; the exit status is the module's.
pub fn main(argv: &[String]) -> i32 {
    if let Err(e) = watch_interrupts() {
        eprintln!("error: could not watch for Ctrl-C: {e}");
        return 1;
    }
    run(argv, &crate::checkout(), std::env::var("EVAL_ENGINE").ok().as_deref(), &mut io::stdout())
}

/// Run the eval set as the module's comment says, with argv the runner's arguments, the tasks of checkout's
/// evals/tasks and its HEAD, and eval_engine as EVAL_ENGINE's value; write its table, or the help, to out, and append
/// its result lines to checkout's evals/results/<date>-<config>.jsonl as each trial ends. The exit status is the
/// module's; one that out could not take is 1.
pub fn run(argv: &[String], checkout: &Path, eval_engine: Option<&str>, out: &mut dyn Write) -> i32 {
    if argv.iter().take_while(|arg| *arg != "--").any(|arg| arg == "-h" || arg == "--help") {
        let help =
            writeln!(out, "{USAGE}\n\nRun the eval set's tasks through the engine and judge each by its hidden test.");
        return i32::from(help.is_err());
    }
    let args = match parse_args(argv) {
        Ok(args) => args,
        Err(message) => {
            eprintln!("{USAGE}\nerror: {message}");
            return 2;
        }
    };
    let evaluation = Eval {
        date: jiff::Zoned::now().date().to_string(),
        config: args.config.clone().unwrap_or_else(|| config_name(&args.run_args)),
        run_args: args.run_args.clone(),
        engine: eval_engine.filter(|e| !e.is_empty()).unwrap_or(DEFAULT_ENGINE).to_string(),
        commit: head_commit(checkout),
    };
    let results_path = evaluation.results_path(&checkout.join(RESULTS_SUBDIR));
    let mut trials = vec![];
    let halt = run_trials(&args, checkout, eval_engine, &evaluation, &results_path, &mut trials);
    let mut exit = match halt {
        Ok(()) => 0,
        Err(Halt::Failed(e)) => {
            eprintln!("error: {e}");
            1
        }
        Err(Halt::Interrupted) => EXIT_INTERRUPTED,
    };
    if !trials.is_empty() {
        let shown = std::env::current_dir()
            .ok()
            .and_then(|cwd| results_path.strip_prefix(cwd).ok().map(Path::to_path_buf))
            .unwrap_or_else(|| results_path.clone());
        if let Err(e) = writeln!(out, "{}results appended to {}", table(&trials), shown.display()) {
            eprintln!("error: could not write the table: {e}");
            exit = exit.max(1);
        }
    }
    exit
}

/// Every trial args ask for, each pushed onto trials and its line appended to results_path as it ends; stopped at
/// the first Halt, or at a Ctrl-C between trials.
fn run_trials(
    args: &Args,
    checkout: &Path,
    eval_engine: Option<&str>,
    evaluation: &Eval,
    results_path: &Path,
    trials: &mut Vec<Trial>,
) -> Result<(), Halt> {
    let engine = engine_command(eval_engine, checkout)?;
    let tasks = load_tasks(args.tasks.as_deref(), &tasks_dir(checkout))?;
    for task in &tasks {
        for repeat in 1..=args.repeat {
            if interrupted() {
                return Err(Halt::Interrupted);
            }
            let trial = run_trial(task, repeat, &engine, &args.run_args, args.keep)?;
            append_records(results_path, &[evaluation.record(&trial)])
                .map_err(|e| Error(format!("could not append to {}: {e}", results_path.display())))?;
            trials.push(trial);
        }
    }
    Ok(())
}
