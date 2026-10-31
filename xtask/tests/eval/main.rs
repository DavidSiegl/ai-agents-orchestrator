//! The eval runner, xtask::eval, with the fake-engine binary as its engine (src/bin/fake-engine.rs), and the eval
//! tasks themselves: each hidden test rejects its task's starting point and accepts a solution of it (solutions.rs).
//! No real agent runs here; docs/design.md, Evals, says how a real eval is run.

mod solutions;

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use xtask::eval::{self, Args, Eval, Halt, Task, Tokens, Trial};
use xtask::{bundles, TempDir};

const RUN_ID: &str = "20261028-120000-abc123";
const TASK_NAMES: [&str; 5] = ["bugfix", "docs", "feature", "no-dependency", "refactor"];

fn strings(words: &[&str]) -> Vec<String> {
    words.iter().map(|word| word.to_string()).collect()
}

fn real_tasks_dir() -> PathBuf {
    eval::tasks_dir(&xtask::checkout())
}

/// What the fake engine plays: the files it writes, its run's exit status, its run directory and metrics.json.
struct Plan {
    files: Vec<(String, String)>,
    exit: u8,
    run_id: Option<&'static str>,
    metrics: Option<Value>,
}

impl Default for Plan {
    fn default() -> Plan {
        Plan { files: vec![], exit: 0, run_id: Some(RUN_ID), metrics: None }
    }
}

/// The fake engine, with its plan and log in a directory of the test's own.
struct FakeEngine {
    dir: TempDir,
}

impl FakeEngine {
    fn new() -> FakeEngine {
        FakeEngine { dir: TempDir::new("xtask-eval-test-").expect("a temporary directory") }
    }

    fn plan(&self, plan: Plan) {
        let files: serde_json::Map<String, Value> = plan.files.into_iter().map(|(p, t)| (p, json!(t))).collect();
        let plan = json!({"files": files, "exit": plan.exit, "run_id": plan.run_id, "metrics": plan.metrics});
        fs::write(self.dir.path().join("plan.json"), plan.to_string()).expect("the plan is written");
    }

    /// The engine's command line: the fake, given its plan and log by env.
    fn command(&self) -> Vec<String> {
        vec![
            "env".to_string(),
            format!("FAKE_ENGINE_PLAN={}", self.dir.path().join("plan.json").display()),
            format!("FAKE_ENGINE_LOG={}", self.dir.path().join("log.jsonl").display()),
            env!("CARGO_BIN_EXE_fake-engine").to_string(),
        ]
    }

    /// Each invocation's log entry, in order.
    fn log(&self) -> Vec<Value> {
        let Ok(text) = fs::read_to_string(self.dir.path().join("log.jsonl")) else { return vec![] };
        text.lines().map(|line| serde_json::from_str(line).expect("a log line is JSON")).collect()
    }

    fn trial(&self, task: &str, run_args: &[&str], keep: bool, repeat: u32) -> Trial {
        let (task,) = load(task);
        eval::run_trial(&task, repeat, &self.command(), &strings(run_args), keep).expect("the trial runs")
    }
}

fn load(name: &str) -> (Task,) {
    let mut tasks = eval::load_tasks(Some(&strings(&[name])), &real_tasks_dir()).expect("the task loads");
    (tasks.remove(0),)
}

/// The argument after flag in an invocation's argv.
fn after<'a>(argv: &'a Value, flag: &str) -> &'a str {
    let argv = argv.as_array().expect("argv is an array");
    let i = argv.iter().position(|arg| arg == flag).unwrap_or_else(|| panic!("{flag} is given"));
    argv[i + 1].as_str().expect("an argument is a string")
}

/// A metrics.json of a run with a build and a review turn, the build's tokens given, and a quality analysis.
fn metrics(tokens: Value, quality_rounds: usize) -> Value {
    let turns = json!([
        {"step": "build", "harness": "claude", "tokens": tokens},
        {"step": "quality", "harness": null, "tokens": null},
        {"step": "review", "harness": "claude", "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0}},
    ]);
    let rounds: Vec<Value> = (1..=quality_rounds).map(|n| json!({"round": 1, "quality_round": n})).collect();
    json!({"version": 1, "outcome": "APPROVE", "rounds": 2, "turns": turns, "quality_rounds": rounds})
}

fn build_tokens() -> Value {
    json!({"input": 10, "output": 5, "cache_read": 100, "cache_write": 1})
}

/// The task's hidden test rejects its starting point, naming what did not pass on its output's last line, and
/// accepts its solution.
fn judge(task: &str) {
    let engine = FakeEngine::new();
    engine.plan(Plan::default());
    let untouched = engine.trial(task, &[], false, 1);
    assert!(!untouched.passed, "{}", untouched.hidden_test_output);
    let last = untouched.hidden_test_output.lines().last().unwrap_or_default();
    assert!(last.starts_with("not accepted: "), "{last}");
    engine.plan(Plan { files: solutions::solution(task), ..Plan::default() });
    let solved = engine.trial(task, &[], false, 1);
    assert!(solved.passed, "{}", solved.hidden_test_output);
}

#[test]
fn bugfix_is_judged() {
    judge("bugfix");
}

#[test]
fn docs_is_judged() {
    judge("docs");
}

#[test]
fn feature_is_judged() {
    judge("feature");
}

#[test]
fn no_dependency_is_judged() {
    judge("no-dependency");
}

#[test]
fn refactor_is_judged() {
    judge("refactor");
}

#[test]
fn a_cargo_toml_that_leaves_the_hidden_test_unbuilt_fails_the_trial() {
    let engine = FakeEngine::new();
    let mut files = solutions::solution("feature");
    let manifest = xtask::toy_repos::TOY_REPOS.iter().find(|r| r.task == "feature").expect("feature").files;
    let toml = manifest.iter().find(|(p, _)| *p == "Cargo.toml").expect("its Cargo.toml").1;
    files.push(("Cargo.toml".to_string(), toml.replace("publish = false\n", "publish = false\nautotests = false\n")));
    engine.plan(Plan { files, ..Plan::default() });
    let trial = engine.trial("feature", &[], false, 1);
    assert!(!trial.passed, "{}", trial.hidden_test_output);
}

#[test]
fn every_task_has_a_toy_repository() {
    let tasks: Vec<String> =
        eval::load_tasks(None, &real_tasks_dir()).expect("the tasks load").into_iter().map(|t| t.name).collect();
    let repos: Vec<&str> = xtask::toy_repos::TOY_REPOS.iter().map(|repo| repo.task).collect();
    assert_eq!(tasks, TASK_NAMES);
    assert_eq!(repos, TASK_NAMES);
}

#[test]
fn the_engine_runs_the_spec_without_a_pull_request_in_the_clone() {
    let engine = FakeEngine::new();
    engine.plan(Plan::default());
    engine.trial("feature", &["--workflow", "quick", "--model", "sonnet"], false, 1);
    let log = engine.log();
    let [run, close] = log.as_slice() else { panic!("not a run and a close: {log:?}") };
    let argv: Vec<&str> = run["argv"].as_array().expect("argv").iter().map(|a| a.as_str().expect("a string")).collect();
    assert_eq!(argv[..2], ["run", "--no-pr"]);
    assert_eq!(argv[argv.len() - 4..], ["--workflow", "quick", "--model", "sonnet"]);
    assert_eq!(close["argv"], json!(["close", RUN_ID, "--cwd", after(&run["argv"], "--cwd")]));
    let (task,) = load("feature");
    assert_eq!(run["spec_text"], fs::read_to_string(task.spec()).expect("task.md reads"));
}

#[test]
fn the_hidden_test_never_reaches_the_agents() {
    let engine = FakeEngine::new();
    engine.plan(Plan::default());
    engine.trial("refactor", &[], false, 1);
    let log = engine.log();
    let run = &log[0];
    let paths = run["paths"].as_array().expect("paths");
    assert!(
        paths.iter().all(|p| !p.as_str().is_some_and(|p| p.contains("hidden") || p.ends_with("task.md"))),
        "{paths:?}"
    );
    let tasks = real_tasks_dir();
    let (origin, spec) = (run["origin"].as_str().expect("origin"), run["spec"].as_str().expect("spec"));
    assert!(!Path::new(origin).starts_with(&tasks), "{origin}");
    assert!(!Path::new(spec).starts_with(&tasks), "{spec}");
    assert!(!Path::new(spec).starts_with(after(&run["argv"], "--cwd")), "the spec is inside the clone");
}

#[test]
fn metrics_fill_the_trial() {
    let engine = FakeEngine::new();
    engine.plan(Plan {
        files: solutions::solution("bugfix"),
        metrics: Some(metrics(build_tokens(), 2)),
        ..Plan::default()
    });
    let trial = engine.trial("bugfix", &[], false, 3);
    assert_eq!((trial.task.as_str(), trial.repeat, trial.passed, trial.engine_exit), ("bugfix", 3, true, 0));
    assert_eq!(
        (trial.run_id.as_deref(), trial.outcome.as_deref(), trial.rounds, trial.quality_rounds),
        (Some(RUN_ID), Some("APPROVE"), Some(2), Some(2))
    );
    assert_eq!(trial.tokens, Some(Tokens { input: 11, output: 6, cache_read: 100, cache_write: 1 }));
    assert!(trial.seconds >= 0.0);
    assert!(trial.hidden_test_output.contains("test result: ok"), "{}", trial.hidden_test_output);
}

#[test]
fn one_turn_of_unknown_tokens_leaves_the_runs_unknown() {
    let engine = FakeEngine::new();
    engine.plan(Plan { metrics: Some(metrics(json!("unknown"), 0)), ..Plan::default() });
    assert_eq!(engine.trial("feature", &[], false, 1).tokens, None);
}

#[test]
fn metrics_of_the_wrong_shape_are_unknown() {
    let engine = FakeEngine::new();
    let odd = json!({"outcome": 1, "rounds": "two", "quality_rounds": {"round": 1}, "turns": "none"});
    engine.plan(Plan { metrics: Some(odd), ..Plan::default() });
    let trial = engine.trial("feature", &[], false, 1);
    assert_eq!((trial.outcome, trial.rounds, trial.quality_rounds, trial.tokens), (None, None, None, None));
    assert_eq!(trial.run_id.as_deref(), Some(RUN_ID));
}

#[test]
fn a_run_that_failed_without_metrics_is_still_judged() {
    let engine = FakeEngine::new();
    engine.plan(Plan { files: solutions::solution("feature"), exit: 1, ..Plan::default() });
    let trial = engine.trial("feature", &[], false, 1);
    assert_eq!((trial.passed, trial.engine_exit, trial.run_id.as_deref()), (true, 1, Some(RUN_ID)));
    assert_eq!((trial.outcome, trial.rounds, trial.tokens), (None, None, None));
}

#[test]
fn a_run_that_made_no_run_directory_is_not_closed() {
    let engine = FakeEngine::new();
    engine.plan(Plan { exit: 1, run_id: None, ..Plan::default() });
    let trial = engine.trial("feature", &[], false, 1);
    assert_eq!((trial.passed, trial.run_id), (false, None));
    let commands: Vec<Value> = engine.log().iter().map(|entry| entry["argv"][0].clone()).collect();
    assert_eq!(commands, [json!("run")]);
}

#[test]
fn the_clone_is_removed_unless_kept() {
    let engine = FakeEngine::new();
    engine.plan(Plan::default());
    engine.trial("feature", &[], false, 1);
    let work =
        |entry: &Value| Path::new(after(&entry["argv"], "--cwd")).parent().expect("the clone's parent").to_path_buf();
    assert!(!work(&engine.log()[0]).exists());
    engine.trial("feature", &[], true, 1);
    let kept = work(&engine.log()[2]);
    let exists = kept.join("repo/.git").is_dir();
    fs::remove_dir_all(&kept).expect("the kept directory is removed");
    assert!(exists, "{} was not kept", kept.display());
}

#[test]
fn an_unreadable_bundle_is_an_error_of_the_runners() {
    let dir = TempDir::new("xtask-eval-test-").expect("a temporary directory");
    for name in ["task.md", "repo.bundle", "hidden_test.rs"] {
        fs::write(dir.path().join(name), "not a bundle\n").expect("written");
    }
    let task = Task { name: "broken".to_string(), dir: dir.path().to_path_buf() };
    match eval::run_trial(&task, 1, &strings(&["true"]), &[], false) {
        Err(Halt::Failed(e)) => assert!(e.0.contains("could not clone"), "{e}"),
        other => panic!("not an error of the runner's: {other:?}"),
    }
}

#[test]
fn every_task_by_default_in_name_order() {
    let tasks = eval::load_tasks(None, &real_tasks_dir()).expect("the tasks load");
    assert_eq!(tasks.iter().map(|t| t.name.as_str()).collect::<Vec<_>>(), TASK_NAMES);
}

#[test]
fn named_tasks_in_the_order_given() {
    let tasks = eval::load_tasks(Some(&strings(&["refactor", "docs"])), &real_tasks_dir()).expect("the tasks load");
    assert_eq!(tasks.iter().map(|t| t.name.as_str()).collect::<Vec<_>>(), ["refactor", "docs"]);
}

#[test]
fn an_unknown_task_is_an_error() {
    let e = eval::load_tasks(Some(&strings(&["nope"])), &real_tasks_dir()).expect_err("nope is no task");
    assert!(e.0.starts_with("task nope: task.md, repo.bundle, hidden_test.rs missing in "), "{e}");
}

fn trial(task: &str, passed: bool, seconds: f64) -> Trial {
    Trial {
        task: task.to_string(),
        repeat: 1,
        passed,
        engine_exit: 0,
        seconds,
        hidden_test_output: "test result: ok".to_string(),
        run_id: None,
        outcome: None,
        rounds: None,
        quality_rounds: None,
        tokens: None,
    }
}

fn tokens(input: u64, cache_read: u64) -> Option<Tokens> {
    Some(Tokens { input, output: 500, cache_read, cache_write: 0 })
}

#[test]
fn a_table_row_per_task_and_one_for_all() {
    let trials = [
        Trial {
            rounds: Some(1),
            quality_rounds: Some(0),
            tokens: tokens(1000, 1_000_000),
            ..trial("feature", true, 90.0)
        },
        Trial {
            rounds: Some(3),
            quality_rounds: Some(2),
            tokens: tokens(3000, 2_000_000),
            ..trial("feature", false, 150.0)
        },
        trial("docs", true, 30.4),
    ];
    assert_eq!(
        eval::table(&trials),
        "task     runs  passed     rounds  quality rounds  wall time      tokens\n\
         feature     2     1/2        2.0             1.0      2m00s        1.5M\n\
         docs        1     1/1          ?               ?      0m30s           ?\n\
         all         3     2/3  2.0 (2/3)       1.0 (2/3)      1m30s  1.5M (2/3)\n"
    );
}

#[test]
fn one_task_has_no_all_row() {
    let only = Trial {
        rounds: Some(1),
        quality_rounds: Some(0),
        tokens: Some(Tokens { input: 900, ..Tokens::default() }),
        ..trial("feature", true, 60.0)
    };
    assert_eq!(
        eval::table(&[only]),
        "task     runs  passed  rounds  quality rounds  wall time  tokens\n\
         feature     1     1/1     1.0             0.0      1m00s     900\n"
    );
}

#[test]
fn judge_names_the_failed_tests_last_by_target() {
    let text = "     Running unittests src/lib.rs (target/debug/deps/durations-1a)\nrunning 1 test\n\
                test one_unit ... FAILED\n     Running tests/hidden_test.rs (target/debug/deps/hidden_test-2b)\n\
                running 2 tests\ntest a ... ok\ntest one_unit ... FAILED\n\nerror: 2 targets failed\n";
    let (passed, output) = eval::judge(text, false);
    assert!(!passed);
    assert_eq!(output.lines().last(), Some("not accepted: lib::one_unit, hidden_test::one_unit"));
    let (passed, unbuilt) = eval::judge("error[E0432]: unresolved import\n", false);
    assert!(!passed);
    assert!(unbuilt.lines().last().is_some_and(|line| line.starts_with("not accepted: no test failed")), "{unbuilt}");
}

#[test]
fn judge_accepts_only_when_the_hidden_test_ran() {
    let ran = "     Running tests/hidden_test.rs (target/debug/deps/hidden_test-2b)\ntest result: ok. 1 passed\n";
    assert_eq!(eval::judge(ran, true), (true, ran.trim_end().to_string()));
    let elsewhere = "     Running tests/other.rs (target/debug/deps/hidden_test-2b)\ntest result: ok. 1 passed\n";
    let (passed, output) = eval::judge(elsewhere, true);
    assert!(!passed);
    assert!(output.ends_with("not accepted: tests/hidden_test.rs did not run; Cargo.toml keeps it out"), "{output}");
}

#[test]
fn judge_keeps_the_last_lines() {
    let long: String =
        (0..40).map(|n| format!("line {n}\n")).collect::<String>() + "     Running tests/hidden_test.rs (x)\n";
    let (_, passed) = eval::judge(&long, true);
    assert_eq!(passed.lines().count(), eval::HIDDEN_TEST_TAIL_LINES);
    assert_eq!(passed.lines().last(), Some("     Running tests/hidden_test.rs (x)"));
    assert_eq!(eval::judge(&long, false).1.lines().count(), eval::HIDDEN_TEST_TAIL_LINES);
}

fn evaluation() -> Eval {
    Eval {
        date: "2026-10-28".to_string(),
        config: "workflow-quick".to_string(),
        run_args: strings(&["--workflow", "quick"]),
        engine: "target/debug/orchestrator".to_string(),
        commit: "abc".to_string(),
    }
}

#[test]
fn a_record_is_the_evals_fields_then_the_trials() {
    let record =
        evaluation().record(&Trial { run_id: Some("r".to_string()), rounds: Some(1), ..trial("feature", true, 1.0) });
    let keys: Vec<usize> =
        ["\"version\"", "\"date\"", "\"config\"", "\"run_args\"", "\"engine\"", "\"commit\"", "\"task\""]
            .iter()
            .map(|key| record.find(key).unwrap_or_else(|| panic!("{key} missing in {record}")))
            .collect();
    assert!(keys.windows(2).all(|pair| pair[0] < pair[1]), "{record}");
    let value: Value = serde_json::from_str(&record).expect("a record is JSON");
    assert_eq!(
        (&value["version"], &value["config"], &value["passed"], &value["rounds"], &value["tokens"]),
        (&json!(eval::RECORD_VERSION), &json!("workflow-quick"), &json!(true), &json!(1), &Value::Null)
    );
    assert_eq!(evaluation().results_path(Path::new("r")), Path::new("r/2026-10-28-workflow-quick.jsonl"));
}

#[test]
fn records_are_appended_a_line_each() {
    let dir = TempDir::new("xtask-eval-test-").expect("a temporary directory");
    let path = dir.path().join("results/x.jsonl");
    eval::append_records(&path, &strings(&[r#"{"a": 1}"#])).expect("appended");
    eval::append_records(&path, &strings(&[r#"{"a": 2}"#, r#"{"a": 3}"#])).expect("appended");
    assert_eq!(fs::read_to_string(&path).expect("read"), "{\"a\": 1}\n{\"a\": 2}\n{\"a\": 3}\n");
}

#[test]
fn config_name() {
    assert_eq!(eval::config_name(&[]), "default");
    assert_eq!(
        eval::config_name(&strings(&["--workflow", "quick", "--role-agent", "review=codex"])),
        "workflow-quick-role-agent-review-codex"
    );
    assert_eq!(eval::config_name(&[format!("--model {}", "x".repeat(100))]).len(), 60);
}

#[test]
fn a_tracked_change_marks_the_commit_dirty() {
    let dir = TempDir::new("xtask-eval-test-").expect("a temporary directory");
    let bundle = dir.path().join("b.bundle");
    let commit = bundles::make_bundle(&[("prompt.txt", "v1\n")], &bundle, "m").expect("the bundle is made");
    let repo = dir.path().join("repo");
    let cloned = std::process::Command::new("git").arg("clone").arg("--quiet").arg(&bundle).arg(&repo).status();
    assert!(cloned.expect("git runs").success());
    assert_eq!(eval::head_commit(&repo), commit);
    fs::write(repo.join("untracked.txt"), "x\n").expect("written");
    assert_eq!(eval::head_commit(&repo), commit);
    fs::create_dir_all(repo.join("evals/results")).expect("made");
    fs::write(repo.join("evals/results/x.jsonl"), "{}\n").expect("written");
    let added = std::process::Command::new("git").arg("-C").arg(&repo).args(["add", "evals"]).status();
    assert!(added.expect("git runs").success());
    assert_eq!(eval::head_commit(&repo), commit, "a results file is not a change");
    fs::write(repo.join("prompt.txt"), "v2\n").expect("written");
    assert_eq!(eval::head_commit(&repo), format!("{commit}-dirty"));
    assert_eq!(eval::head_commit(dir.path()), "unknown");
}

#[test]
fn run_args_follow_the_double_dash() {
    let args = eval::parse_args(&strings(&["--tasks", "a,b", "--repeat=2", "--keep", "--", "--model", "sonnet"]));
    let expected = Args {
        tasks: Some(strings(&["a", "b"])),
        repeat: 2,
        config: None,
        keep: true,
        run_args: strings(&["--model", "sonnet"]),
    };
    assert_eq!(args, Ok(expected));
}

#[test]
fn the_runners_own_flags_are_refused_in_run_args() {
    for flag in ["--no-pr", "--spec=x", "--cwd", "--machine", "--worktree", "--workt", "--cw", "--sp=x"] {
        let e = eval::parse_args(&strings(&["--", flag])).expect_err(flag);
        assert!(e.starts_with("RUN_ARGS cannot hold"), "{flag}: {e}");
    }
}

#[test]
fn the_engines_other_flags_pass() {
    let run_args = ["--workflow", "quick", "--model", "sonnet", "--max-rounds", "2", "--", "x"];
    let mut argv = vec!["--"];
    argv.extend(run_args);
    assert_eq!(eval::parse_args(&strings(&argv)).map(|args| args.run_args), Ok(strings(&run_args)));
}

#[test]
fn the_runners_flags_by_any_start_of_one() {
    let args = eval::parse_args(&strings(&["--ta", "docs", "--rep=3", "--con", "c", "--k"])).expect("they parse");
    assert_eq!(
        (args.tasks, args.repeat, args.config, args.keep),
        (Some(strings(&["docs"])), 3, Some("c".into()), true)
    );
}

#[test]
fn usage_errors() {
    for argv in
        [&["--repeat", "0"][..], &["--repeat", "x"], &["--tasks"], &["--nope"], &["--keep=1"], &["-x"], &["--t"]]
    {
        assert!(eval::parse_args(&strings(argv)).is_err(), "{argv:?}");
    }
}

#[test]
fn the_engine_is_eval_engine_split_as_a_shell_splits_it() {
    let engine = eval::engine_command(Some(r#"target/debug/octoflow --flag 'a b' "c \"d\"" e\ f"#), Path::new("/"));
    assert_eq!(engine, Ok(strings(&["target/debug/octoflow", "--flag", "a b", "c \"d\"", "e f"])));
    assert!(eval::engine_command(Some("x 'unclosed"), Path::new("/")).is_err());
    assert!(
        eval::engine_command(Some("x \\"), Path::new("/")).is_err(),
        "a trailing backslash, as shlex.split refuses"
    );
}

#[test]
fn the_checkouts_engine_is_built_when_eval_engine_is_unset_or_empty() {
    let engine = eval::engine_command(Some(""), &xtask::checkout()).expect("the engine builds");
    let [path] = engine.as_slice() else { panic!("not one path: {engine:?}") };
    assert!(Path::new(path).is_file(), "{path}");
    assert_eq!(Path::new(path).file_name().and_then(|n| n.to_str()), Some("orchestrator"));
}

/// A checkout of its own with the real task named, and nothing else.
fn checkout_with(task: &str) -> TempDir {
    let dir = TempDir::new("xtask-eval-checkout-").expect("a temporary directory");
    let tasks = eval::tasks_dir(dir.path()).join(task);
    fs::create_dir_all(&tasks).expect("made");
    for name in ["task.md", "repo.bundle", "hidden_test.rs"] {
        fs::copy(real_tasks_dir().join(task).join(name), tasks.join(name)).expect("copied");
    }
    dir
}

#[test]
fn run_prints_the_table_and_appends_a_line_per_trial() {
    let engine = FakeEngine::new();
    engine.plan(Plan {
        files: solutions::solution("feature"),
        metrics: Some(metrics(build_tokens(), 0)),
        ..Plan::default()
    });
    let checkout = checkout_with("feature");
    let mut out = vec![];
    let argv = strings(&["--tasks", "feature", "--repeat", "2", "--", "--model", "sonnet"]);
    let status = eval::run(&argv, checkout.path(), Some(&engine.command().join(" ")), &mut out);
    let out = String::from_utf8(out).expect("the table is UTF-8");
    assert_eq!(status, 0, "{out}");
    let row = out.lines().nth(1).expect("a row");
    let shown = out.lines().last().and_then(|line| line.strip_prefix("results appended to ")).expect("where they went");
    let cwd = std::env::current_dir().expect("the test's directory");
    assert!(shown.starts_with("../"), "{shown}: not relative to {}", cwd.display());
    assert_eq!(
        cwd.join(shown).canonicalize().ok(),
        checkout.path().join("evals/results").join(Path::new(shown).file_name().expect("a name")).canonicalize().ok()
    );
    let cells: Vec<&str> = row.split_whitespace().collect();
    assert_eq!((cells[..5].to_vec(), cells[6]), (vec!["feature", "2", "2/2", "2.0", "0.0"], "118"), "{row}");
    let results = checkout.path().join("evals/results");
    let names: Vec<String> = fs::read_dir(&results)
        .expect("results")
        .map(|e| e.expect("entry").file_name().to_string_lossy().into())
        .collect();
    let [name] = names.as_slice() else { panic!("not one results file: {names:?}") };
    assert!(name.ends_with("-model-sonnet.jsonl") && name.len() == "2026-10-31-model-sonnet.jsonl".len(), "{name}");
    let records: Vec<Value> = fs::read_to_string(results.join(name))
        .expect("read")
        .lines()
        .map(|l| serde_json::from_str(l).expect("JSON"))
        .collect();
    let fields: Vec<(Value, Value, Value, Value)> = records
        .iter()
        .map(|r| (r["task"].clone(), r["repeat"].clone(), r["passed"].clone(), r["run_args"].clone()))
        .collect();
    let run_args = json!(["--model", "sonnet"]);
    assert_eq!(
        fields,
        [
            (json!("feature"), json!(1), json!(true), run_args.clone()),
            (json!("feature"), json!(2), json!(true), run_args)
        ]
    );
    assert!(records.iter().all(|r| r["commit"] == "unknown" && r["engine"] == json!(engine.command().join(" "))));
}

#[test]
fn an_unknown_task_exits_1_and_writes_nothing() {
    let checkout = checkout_with("feature");
    let mut out = vec![];
    assert_eq!(eval::run(&strings(&["--tasks", "nope"]), checkout.path(), Some("true"), &mut out), 1);
    assert!(out.is_empty());
    assert!(!checkout.path().join("evals/results").exists());
}

#[test]
fn a_usage_error_exits_2_and_help_0() {
    let checkout = checkout_with("feature");
    let mut out = vec![];
    assert_eq!(eval::run(&strings(&["--repeat", "0"]), checkout.path(), Some("true"), &mut out), 2);
    assert_eq!(eval::run(&strings(&["--help"]), checkout.path(), Some("true"), &mut out), 0);
    assert!(String::from_utf8_lossy(&out).starts_with("usage: cargo xtask eval"));
}
