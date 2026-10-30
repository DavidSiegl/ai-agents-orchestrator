//! What `list`, `list --json`, `show` and `metrics` print. Python's cases, by class: TestRunHealth's test_print_runs
//! and test_print_closed_runs (its run_health cases are crate::runs'); TestRunRecord; TestListJSON;
//! TestCLI.test_list_on_machine_uses_ssh_host; TestShow; TestQualityGateOffOnResume.test_show_names_the_gate;
//! TestBlockedListing but test_status_tag, which is the GUI's (not ported, the rust-engine spec's Decisions); the
//! print_runs half of TestBlocked.test_the_wait_is_saved_and_listed, on the state its Workflow half saves
//! (crate::workflow's blocked tests), built here by hand; TestQualityCLI.test_list_shows_the_quality_phase;
//! TestWorkflowFiles.test_list_reads_the_gated_step_from_the_saved_definition; and
//! TestRunMetrics.test_the_metrics_command_prints_a_line_per_run_that_is_done.
//!
//! Where Python patches Host.resolve_dir to /proj, these give --cwd /proj, which the FakeHost resolves to itself.
//! TestShow.test_on_a_machine_everything_goes_over_ssh runs show_run on a ProcessHost over an ssh that runs each
//! command here, on a project in a TempDir, rather than through main, whose connect would ask herdr for the machine.

use std::rc::Rc;

use super::*;
use crate::cli::fixture::Cli;
use crate::constants::{APPROVE, CHANGES_REQUESTED, EXIT_ERROR, STALE_SECONDS};
use crate::host::fake::{FakeHost, FakePr, HOST_EPOCH};
use crate::host::{Completed, ProcessHost};
use crate::interrupt::Interrupt;
use crate::summary::iso_utc;
use crate::test_support::{impl_, solo, TempDir};
use crate::workflow_file::workflow_definition;

const RUN_ID: &str = "20260930-070000-c0ffee";
const PR_URL: &str = "https://github.com/o/r/pull/7";
const QUESTION: &str = "which database?";
const SHOW_ID: &str = "20260929-120000-a1b2c3";
const SHOW_DIR: &str = "/proj/.orchestrator/runs/20260929-120000-a1b2c3";
const JOB: &str = crate::ci::fake::JOB;

/// The keys of a run record, in the order run_record gives them.
const RUN_RECORD_KEYS: [&str; 23] = [
    "id", "key", "task", "workflow", "steps", "phase", "round", "quality_round", "role", "pane", "agents", "health",
    "health_reason", "heartbeat_age", "waiting", "verdict", "error", "closed", "branch", "worktree", "pr", "turns",
    "resume_command",
];

/// JSON text as a value.
fn json(text: &str) -> Json {
    Json::parse(text).unwrap_or_else(|e| panic!("{text}: {e}"))
}

/// Python's run_record helper: a run of phase in round 1, its owner, error and verdict null.
fn run(phase: &str) -> Json {
    json(&format!(
        r#"{{"run_id": "{RUN_ID}", "task": "add a rate limiter", "phase": "{phase}", "round": 1, "owner": null,
            "error": null, "verdict": null}}"#
    ))
}

/// state with each of fields set, as Python's {**state, **fields}.
fn with(state: Json, fields: &[(&str, Json)]) -> Json {
    let Json::Object(mut map) = state else { panic!("a state is an object") };
    for (key, value) in fields {
        map.insert(*key, value.clone());
    }
    Json::Object(map)
}

/// The owner record of a run on this machine, "here": Python's ME.
fn me() -> Json {
    json(r#"{"host": "here", "pid": 4242, "started_at": "2026-09-30T07:00:00+00:00"}"#)
}

/// A run in build owned by ME.
fn mine() -> Json {
    with(run("build"), &[("owner", me())])
}

/// A BLOCKED report's wait, as the Workflow saves it: Python's BLOCK.
fn block() -> Json {
    json(&format!(r#"{{"file": "build-1.md", "question": "{QUESTION}", "k": 1}}"#))
}

/// run_record of state, saved age seconds ago, on this machine "here" where every pid is alive or none is.
fn record(state: &Json, age: u64, alive: bool, target: &[&str]) -> Map<Json> {
    let target: Vec<String> = target.iter().map(|s| s.to_string()).collect();
    run_record(state, age, "here", &move |_| alive, &target)
}

/// record's value of key.
fn key(record: &Map<Json>, key: &str) -> Json {
    record.get(key).unwrap_or_else(|| panic!("no {key}")).clone()
}

/// A world whose every pid is alive.
fn alive() -> Cli {
    let mut cli = Cli::new();
    cli.f.pid_alive = Box::new(|_| true);
    cli
}

/// The run lines print_runs prints for runs, without the task lines under them, every pid alive.
fn listed(runs: &[(u64, Json)], target: &[&str]) -> Vec<String> {
    let cli = alive();
    let target: Vec<String> = target.iter().map(|s| s.to_string()).collect();
    cli.with(|world| print_runs(world, runs, &target));
    cli.lines().into_iter().step_by(2).collect()
}

// -- TestRunHealth's print_runs cases --------------------------------------------------------------------------------

#[test]
fn print_runs_lists_each_run_and_its_task() {
    let runs = [
        (STALE_SECONDS + 1, run("spec")),
        (40, mine()),
        (1_000_000, with(run("done"), &[("verdict", Json::from(APPROVE))])),
        (1_000_000, with(run("build"), &[("error", Json::from("interrupted"))])),
    ];
    assert_eq!(listed(&runs, &["--machine", "remote", "--cwd", "~/proj"]), [
        "20260930-070000-c0ffee  spec     round 1  stale: no heartbeat for 5m; resume: orchestrator.py resume c0ffee --machine remote --cwd '~/proj'",
        "20260930-070000-c0ffee  build    round 1  running: pid 4242 on here, beat 40s ago",
        "20260930-070000-c0ffee  done     round 1  APPROVE",
        "20260930-070000-c0ffee  build    round 1  error: interrupted",
    ]);
    let cli = alive();
    cli.with(|world| print_runs(world, &runs, &[]));
    assert_eq!(cli.lines()[1], "    add a rate limiter");
    cli.with(|world| print_runs(world, &[], &[]));
    assert_eq!(cli.take_out(), "No runs.\n");
}

#[test]
fn print_closed_runs() {
    let closed = "workspace closed; worktree deleted";
    let runs = [
        (STALE_SECONDS + 1, with(run("build"), &[("closed", Json::from(closed))])),
        (1_000_000, with(run("done"), &[("verdict", Json::from(APPROVE)), ("closed", Json::from("workspace closed"))])),
        (1_000_000, with(run("done"), &[("closed", Json::from("workspace already gone"))])),
        (1_000_000, with(run("build"), &[("error", Json::from("interrupted")), ("closed", Json::from(closed))])),
    ];
    assert_eq!(listed(&runs, &[]), [
        format!("20260930-070000-c0ffee  build    round 1  closed: {closed}"),
        "20260930-070000-c0ffee  done     round 1  APPROVE; closed: workspace closed".to_string(),
        "20260930-070000-c0ffee  done     round 1  closed: workspace already gone".to_string(),
        format!("20260930-070000-c0ffee  build    round 1  error: interrupted; closed: {closed}"),
    ]);
}

// -- TestRunRecord ---------------------------------------------------------------------------------------------------

/// A human-paced turn of step by role that has not ended: Python's open_turn.
fn open_turn(step: &str, role: &str) -> Json {
    json(&format!(
        r#"{{"step": "{step}", "role": "{role}", "round": 1, "quality_round": 0, "file": "{step}.md",
            "agent": "claude", "model": null, "human_paced": true, "started_at": "2026-09-30T07:00:00Z",
            "ended_at": null, "seconds": null}}"#
    ))
}

#[test]
fn every_key_is_present() {
    let state = with(mine(), &[("branch", Json::from("orchestrator/x")), ("pr_url", Json::from(PR_URL))]);
    let record = record(&state, 40, true, &[]);
    assert_eq!(record.keys().collect::<Vec<_>>(), RUN_RECORD_KEYS);
    assert_eq!(key(&record, "id"), Json::from(RUN_ID));
    assert_eq!(key(&record, "key"), Json::from("c0ffee"));
    assert_eq!(key(&record, "workflow"), Json::from("default"));
    assert_eq!(
        key(&record, "steps"),
        json(r#"[{"id": "spec", "role": "spec"}, {"id": "build", "role": "build"}, {"id": "review", "role": "review"}]"#)
    );
    assert_eq!([key(&record, "phase"), key(&record, "round"), key(&record, "role")], [
        Json::from("build"),
        Json::from(1),
        Json::from("build")
    ]);
    assert_eq!([key(&record, "health"), key(&record, "health_reason"), key(&record, "heartbeat_age")], [
        Json::from("running"),
        Json::from("pid 4242 on here, beat 40s ago"),
        Json::from(40)
    ]);
    assert_eq!(key(&record, "pr"), json(&format!(r#"{{"url": "{PR_URL}", "state": null, "error": null}}"#)));
    assert_eq!(key(&record, "branch"), Json::from("orchestrator/x"));
    assert_eq!(key(&record, "turns"), json("[]"));
    assert_eq!((key(&record, "waiting"), key(&record, "resume_command")), (Json::Null, Json::Null));
    Json::Object(record).dumps();
}

#[test]
fn the_agents_are_those_started_with_their_names_and_kinds() {
    let agents = json(
        r#"{"spec": {"name": "spec-c0ffee", "pane": "w1:p1"},
            "build": {"name": "build-c0ffee", "pane": "w1:p2", "kind": "codex"},
            "review": {"name": "", "pane": ""}}"#,
    );
    let record = record(&with(mine(), &[("agents", agents)]), 40, true, &[]);
    assert_eq!(
        key(&record, "agents"),
        json(
            r#"[{"role": "spec", "name": "spec-c0ffee", "kind": "claude"},
                {"role": "build", "name": "build-c0ffee", "kind": "codex"}]"#
        )
    );
    assert_eq!(key(&self::record(&mine(), 40, true, &[]), "agents"), json("[]"));
}

#[test]
fn the_task_is_whole() {
    let task = "x".repeat(300);
    assert_eq!(key(&record(&with(run("build"), &[("task", Json::from(task.as_str()))]), 40, true, &[]), "task"), Json::from(task));
}

#[test]
fn a_state_from_before_the_newer_fields() {
    let state = json(&format!(r#"{{"run_id": "{RUN_ID}", "task": "t", "phase": "review", "round": 2}}"#));
    let record = record(&state, STALE_SECONDS + 1, true, &[]);
    assert_eq!(record.keys().collect::<Vec<_>>(), RUN_RECORD_KEYS);
    assert_eq!(
        [key(&record, "turns"), key(&record, "quality_round"), key(&record, "closed"), key(&record, "worktree")],
        [json("[]"), Json::Null, Json::Null, Json::Null]
    );
    assert_eq!(key(&record, "waiting"), json(r#"{"reason": "stale", "detail": "orchestrator.py resume c0ffee"}"#));
}

#[test]
fn a_workflow_this_orchestrator_cannot_load() {
    let state = with(run("build"), &[("workflow", Json::from("gone")), ("quality_round", Json::from(2))]);
    let record = record(&state, 40, true, &[]);
    assert_eq!(record.keys().collect::<Vec<_>>(), RUN_RECORD_KEYS);
    assert_eq!(
        [key(&record, "workflow"), key(&record, "steps"), key(&record, "role"), key(&record, "pane")],
        [Json::from("gone"), Json::Null, Json::Null, Json::Null]
    );
    assert_eq!(key(&record, "quality_round"), Json::Null);
    let quality = with(run("quality"), &[("workflow", Json::from("gone")), ("quality_round", Json::from(2))]);
    assert_eq!(key(&super::tests::record(&quality, 40, true, &[]), "quality_round"), Json::from(2));
}

#[test]
fn no_role_outside_the_steps() {
    for phase in ["quality", "publish", "done"] {
        assert_eq!(key(&record(&run(phase), 40, true, &[]), "role"), Json::Null, "{phase}");
    }
}

#[test]
fn quality_round_only_in_the_quality_loop_or_gated_step() {
    for (phase, expected) in [("build", Json::from(2)), ("quality", Json::from(2)), ("review", Json::Null)] {
        let state = with(run(phase), &[("quality_round", Json::from(2))]);
        assert_eq!(key(&record(&state, 40, true, &[]), "quality_round"), expected, "{phase}");
    }
}

#[test]
fn blocked_waits_for_the_answer() {
    let state = with(mine(), &[
        ("blocked", json(r#"{"file": "build-1.md", "question": "which db?", "k": 1}"#)),
        ("agents", json(r#"{"build": {"name": "build-c0ffee", "pane": "w1:p2"}}"#)),
        ("turns", Json::Array(vec![open_turn("spec", "spec")])),
    ]);
    let record = record(&state, 40, true, &[]);
    assert_eq!(key(&record, "waiting"), json(r#"{"reason": "blocked", "detail": "which db?"}"#));
    assert_eq!(key(&record, "pane"), Json::from("w1:p2"));
}

#[test]
fn an_interview_waits_while_the_human_paced_turn_is_open() {
    let waiting = |state: &Json| key(&record(state, 40, true, &[]), "waiting");
    let spec = with(run("spec"), &[("owner", me())]);
    let state = with(spec.clone(), &[("turns", Json::Array(vec![open_turn("spec", "spec")]))]);
    assert_eq!(waiting(&state), json(r#"{"reason": "interview", "detail": "spec"}"#));
    let ended = with(open_turn("spec", "spec"), &[("ended_at", Json::from("2026-09-30T07:05:00Z"))]);
    assert_eq!(waiting(&with(state, &[("turns", Json::Array(vec![ended]))])), Json::Null);
    assert_eq!(waiting(&spec), Json::Null);
    // A Builder turn is not human-paced, open or not.
    assert_eq!(waiting(&with(mine(), &[("turns", Json::Array(vec![open_turn("build", "build")]))])), Json::Null);
}

#[test]
fn a_stale_run_waits_to_be_resumed_unless_closed() {
    let record_of = |state: &Json, target: &[&str]| record(state, STALE_SECONDS + 1, true, target);
    let stale = record_of(&mine(), &["--machine", "m", "--cwd", "~/p"]);
    let command = "orchestrator.py resume c0ffee --machine m --cwd '~/p'";
    assert_eq!(key(&stale, "waiting"), json(&format!(r#"{{"reason": "stale", "detail": "{command}"}}"#)));
    assert_eq!(key(&stale, "resume_command"), Json::from(command));
    let closed = record_of(&with(mine(), &[("closed", Json::from("workspace closed"))]), &[]);
    assert_eq!(key(&closed, "health"), Json::from("stale"));
    assert_eq!((key(&closed, "waiting"), key(&closed, "resume_command")), (Json::Null, Json::Null));
}

#[test]
fn a_blocked_stale_run_waits_to_be_resumed() {
    let state = with(mine(), &[("blocked", json(r#"{"file": "build-1.md", "question": "q", "k": 1}"#))]);
    assert_eq!(key(&record(&state, STALE_SECONDS + 1, true, &[]), "waiting").get("reason"), Some(&Json::from("stale")));
}

#[test]
fn finished_and_failed_runs_wait_for_nothing() {
    for state in [with(run("done"), &[("verdict", Json::from(APPROVE))]), with(run("build"), &[("error", Json::from("interrupted"))])] {
        let record = record(&state, 1_000_000, true, &[]);
        assert_eq!([key(&record, "health"), key(&record, "waiting"), key(&record, "resume_command")], [
            Json::Null,
            Json::Null,
            Json::Null
        ]);
    }
}

#[test]
fn plain_lines_in_each_state() {
    let blocked = with(mine(), &[
        ("blocked", json(r#"{"file": "build-1.md", "question": "which db?", "k": 1}"#)),
        ("agents", json(r#"{"build": {"name": "build-c0ffee", "pane": "w1:p2"}}"#)),
    ]);
    let approved = with(run("done"), &[("verdict", Json::from(APPROVE))]);
    let runs = [
        (40, mine()),
        (STALE_SECONDS + 1, mine()),
        (40, blocked),
        (STALE_SECONDS + 1, with(run("build"), &[("closed", Json::from("workspace closed"))])),
        (1_000_000, with(run("build"), &[("error", Json::from("interrupted"))])),
        (1_000_000, with(approved.clone(), &[("pr_url", Json::from(PR_URL))])),
        (1_000_000, with(approved, &[("workflow", Json::from("gone"))])),
        (40, with(run("quality"), &[("owner", me()), ("quality_round", Json::from(2))])),
    ];
    let rows = run_rows(&runs, "here", &|_| true, &[]);
    assert_eq!(rows.iter().map(run_line).collect::<Vec<_>>(), [
        "20260930-070000-c0ffee  build    round 1  running: pid 4242 on here, beat 40s ago".to_string(),
        "20260930-070000-c0ffee  build    round 1  stale: no heartbeat for 5m; resume: orchestrator.py resume c0ffee".into(),
        "20260930-070000-c0ffee  build    round 1  blocked: which db? (pane w1:p2)".into(),
        "20260930-070000-c0ffee  build    round 1  closed: workspace closed".into(),
        "20260930-070000-c0ffee  build    round 1  error: interrupted".into(),
        format!("20260930-070000-c0ffee  done     round 1  APPROVE  {PR_URL}"),
        "20260930-070000-c0ffee  done     round 1  APPROVE  [gone workflow]".into(),
        "20260930-070000-c0ffee  quality  round 1 q2  running: pid 4242 on here, beat 40s ago".into(),
    ]);
}

#[test]
fn show_starts_with_the_list_line() {
    let cli = alive();
    let state = with(run("done"), &[("verdict", Json::from(APPROVE)), ("cwd", Json::from("/proj")), ("pr_url", Json::from(PR_URL))]);
    cli.save(&state, 0);
    cli.with(|world| show_run(world, "/proj", "c0ffee", &[])).unwrap();
    assert_eq!(cli.lines()[0], format!("20260930-070000-c0ffee  done     round 1  APPROVE  {PR_URL}"));
}

#[test]
fn pr_states_are_filled_per_run() {
    let cli = Cli::new();
    let (unusable, broken) = (format!("{PR_URL}/unusable"), format!("{PR_URL}/broken"));
    {
        let mut host = cli.f.host.state();
        let merged = json(&format!(r#"{{"state": "MERGED", "headRefOid": "{}", "closedAt": "2026-09-30T08:00:00Z"}}"#, "a".repeat(40)));
        host.pull_requests.insert(PR_URL.into(), FakePr::Prints(merged));
        host.pull_requests.insert(unusable.clone(), FakePr::Fails(Completed::new(4, "", "gh: not logged in")));
        host.pull_requests.insert(broken.clone(), FakePr::Fails(Completed::new(0, "nonsense", "")));
    }
    let mut records: Vec<Map<Json>> = [PR_URL, &unusable, &broken]
        .iter()
        .map(|url| record(&with(run("done"), &[("pr_url", Json::from(*url))]), 40, true, &[]))
        .collect();
    records.push(record(&run("done"), 40, true, &[]));
    cli.with(|world| fill_pr_states(world, "/proj", &mut records)).unwrap();
    let pr = |i: usize, field: &str| records[i].get("pr").unwrap().get(field).cloned().unwrap_or(Json::Null);
    assert_eq!(key(&records[0], "pr"), json(&format!(r#"{{"url": "{PR_URL}", "state": "MERGED", "error": null}}"#)));
    assert_eq!(pr(1, "state"), Json::Null);
    assert!(pr(1, "error").python_str().contains("not logged in"), "{:?}", pr(1, "error"));
    assert_eq!(pr(2, "state"), Json::Null);
    assert!(pr(2, "error").python_str().contains("nonsense"), "{:?}", pr(2, "error"));
    assert_eq!(key(&records[3], "pr"), Json::Null);
    assert_eq!(cli.f.host.state().gh_calls.len(), 3);
}

#[test]
fn a_lookup_the_interrupt_failed_stops_the_listing() {
    let cli = Cli::new();
    let failing = FakePr::Fails(Completed::new(1, "", "killed"));
    cli.f.host.state().pull_requests.insert(PR_URL.into(), failing);
    let mut records = vec![record(&with(run("done"), &[("pr_url", Json::from(PR_URL))]), 40, true, &[])];
    cli.f.interrupt.raise();
    assert!(cli.with(|world| fill_pr_states(world, "/proj", &mut records)).is_err());
    assert_eq!(records[0].get("pr").unwrap().get("error"), Some(&Json::Null));
}

// -- TestListJSON, and TestCLI.test_list_on_machine_uses_ssh_host ----------------------------------------------------

/// The document `list --json` with flags prints for runs, saved in /proj, checking it is one line of output, exit
/// status 0 and no herdr call.
fn list_json(cli: &Cli, flags: &[&str], runs: &[(u64, Json)]) -> Json {
    for (age, state) in runs {
        cli.save(state, *age);
    }
    let argv: Vec<&str> = ["list", "--json", "--cwd", "/proj"].iter().chain(flags).copied().collect();
    assert_eq!(cli.main(&argv), 0);
    assert!(cli.f.herdr.state().calls.is_empty(), "herdr was called");
    let out = cli.take_out();
    assert_eq!(out.matches('\n').count(), out.lines().count());
    assert!(out.ends_with("}\n"), "{out}");
    json(&out)
}

#[test]
fn no_runs() {
    assert_eq!(list_json(&Cli::new(), &[], &[]), json(r#"{"version": 1, "runs": []}"#));
}

#[test]
fn one_record_per_run_in_order() {
    let cli = Cli::new();
    let runs = [(40, mine()), (1_000_000, with(run("done"), &[("verdict", Json::from(APPROVE)), ("run_id", Json::from("x-beef00"))]))];
    let doc = list_json(&cli, &[], &runs);
    let records = doc.get("runs").and_then(Json::as_array).unwrap();
    assert_eq!(records.iter().map(|r| r.get("key").unwrap().python_str()).collect::<Vec<_>>(), ["c0ffee", "beef00"]);
    assert_eq!(records[0], Json::Object(run_record(&runs[0].1, 40, "here", &crate::runs::pid_alive, &[])));
    // Written as json.dumps(..., indent=2) writes it.
    assert_eq!(doc.dumps_indented(2), {
        cli.main(&["list", "--json", "--cwd", "/proj"]);
        cli.take_out().trim_end().to_string()
    });
}

#[test]
fn no_gh_without_pr_state() {
    let cli = Cli::new();
    let doc = list_json(&cli, &[], &[(1_000_000, with(run("done"), &[("pr_url", Json::from(PR_URL))]))]);
    assert_eq!(doc.get("runs").unwrap().as_array().unwrap()[0].get("pr"), Some(&json(&format!(r#"{{"url": "{PR_URL}", "state": null, "error": null}}"#))));
    assert!(cli.f.host.state().gh_calls.is_empty());
}

#[test]
fn pr_state() {
    let cli = Cli::new();
    let open = json(&format!(r#"{{"state": "OPEN", "headRefOid": "{}", "closedAt": null}}"#, "a".repeat(40)));
    cli.f.host.state().pull_requests.insert(PR_URL.into(), FakePr::Prints(open));
    let runs = [(1_000_000, with(run("done"), &[("pr_url", Json::from(PR_URL))])), (1_000_000, with(run("done"), &[("run_id", Json::from("x-beef00"))]))];
    let doc = list_json(&cli, &["--pr-state"], &runs);
    assert_eq!(cli.f.host.state().gh_calls, [vec!["pr", "view", PR_URL, "--json", "state,headRefOid,closedAt"]]);
    assert_eq!(doc.get("runs").unwrap().as_array().unwrap()[0].get("pr").unwrap().get("state"), Some(&Json::from("OPEN")));
}

#[test]
fn unusable_gh_still_lists() {
    let cli = Cli::new();
    let missing = FakePr::Fails(Completed::new(127, "", "sh: 1: exec: gh: not found"));
    cli.f.host.state().pull_requests.insert(PR_URL.into(), missing);
    let doc = list_json(&cli, &["--pr-state"], &[(1_000_000, with(run("done"), &[("pr_url", Json::from(PR_URL))]))]);
    let pr = doc.get("runs").unwrap().as_array().unwrap()[0].get("pr").unwrap().clone();
    assert_eq!(pr.get("state"), Some(&Json::Null));
    assert_eq!(
        pr.get("error").unwrap().python_str(),
        format!("gh pr view {PR_URL} --json state,headRefOid,closedAt failed with status 127: sh: 1: exec: gh: not found")
    );
}

#[test]
fn pr_state_needs_json() {
    let argv = ["list", "--pr-state"].map(std::ffi::OsString::from);
    let e = crate::cli::parse_args(argv, &crate::env::ConfigDir::at("/nowhere")).unwrap_err();
    assert_eq!(e.exit_code(), 2);
    assert!(e.to_string().contains("--pr-state needs --json"), "{e}");
}

#[test]
fn on_a_machine() {
    // The FakeHost resolves a path to itself, so the runs are asked of ~/proj as given.
    let cli = Cli::new();
    let asked = Rc::new(std::cell::RefCell::new(vec![]));
    let states = Rc::clone(&asked);
    cli.f.host.state().run_states_with = Some(Box::new(move |cwd| {
        states.borrow_mut().push(cwd.to_string());
        Some(Ok(vec![(STALE_SECONDS + 1, mine())]))
    }));
    assert_eq!(cli.main(&["list", "--json", "--machine", "remote", "--cwd", "~/proj"]), 0);
    assert_eq!(*asked.borrow(), ["~/proj"]);
    let doc = json(&cli.take_out());
    assert_eq!(
        doc.get("runs").unwrap().as_array().unwrap()[0].get("resume_command"),
        Some(&Json::from("orchestrator.py resume c0ffee --machine remote --cwd '~/proj'"))
    );
    assert!(cli.f.herdr.state().calls.is_empty());
    cli.f.host.state().run_states_with = Some(Box::new(|_| Some(Ok(vec![]))));
    assert_eq!(cli.main(&["list", "--machine", "remote", "--cwd", "~/proj"]), 0);
    assert_eq!(cli.take_out(), "No runs.\n");
}

// -- TestShow --------------------------------------------------------------------------------------------------------

/// The task of the run show prints: longer than list's 100 characters.
fn long_task() -> String {
    "add a token-bucket rate limiter to the API client ".repeat(4)
}

/// Python's show_state: a run of the default workflow, done with APPROVE, in workspace w1, with fields set.
fn show_state(fields: &[(&str, Json)]) -> Json {
    let state = json(&format!(
        r#"{{"run_id": "{SHOW_ID}", "task": "{}", "cwd": "/proj", "phase": "done", "round": 1, "verdict": "APPROVE",
            "workspace_id": "w1",
            "agents": {{"spec": {{"name": "spec-a1b2c3", "pane": "w1:p1"}},
                        "build": {{"name": "build-a1b2c3", "pane": "w1:p2", "kind": "codex"}},
                        "review": {{"name": "review-a1b2c3", "pane": "w1:p3", "kind": "claude"}}}}}}"#,
        long_task()
    ));
    with(state, fields)
}

/// A world whose /proj holds states, each saved 10^6 s ago: Python's ShowHost.
fn show_host(states: &[Json]) -> Cli {
    let cli = alive();
    for state in states {
        cli.save(state, 1_000_000);
    }
    cli
}

/// Put a file of the shown run.
fn put(cli: &Cli, name: &str, text: &str) {
    cli.f.host.state().files.insert(format!("{SHOW_DIR}/{name}"), text.to_string());
}

/// main's exit status for `show ref` in /proj, and what it printed on stdout.
fn show(cli: &Cli, run_ref: &str) -> (i32, String) {
    let status = cli.main(&["show", run_ref, "--cwd", "/proj"]);
    (status, cli.take_out())
}

/// What `show a1b2c3` prints of states.
fn shown(states: &[Json]) -> String {
    let (status, out) = show(&show_host(states), "a1b2c3");
    assert_eq!(status, 0);
    out
}

/// Whether some line of out starts with label and a space.
fn has_line(out: &str, label: &str) -> bool {
    out.lines().any(|line| line.starts_with(&format!("{label} ")))
}

#[test]
fn key_and_full_id_print_the_same() {
    let cli = show_host(&[show_state(&[]), show_state(&[("run_id", Json::from("20260929-130000-ffffff"))])]);
    put(&cli, "review-1.md", "VERDICT: APPROVE\n\nfine\n");
    let by_key = show(&cli, "a1b2c3");
    assert_eq!(by_key, show(&cli, SHOW_ID));
    assert_eq!(by_key.0, 0);
    assert!(by_key.1.contains(SHOW_ID));
    assert!(!by_key.1.contains("ffffff"));
}

#[test]
fn unknown_or_ambiguous_run() {
    let cli = show_host(&[show_state(&[]), show_state(&[("run_id", Json::from("20260930-120000-a1b2c3"))])]);
    let cases = [
        ("nosuch", "no run nosuch under /proj/.orchestrator/runs".to_string()),
        ("a1b2c3", format!("a1b2c3 matches several runs ({SHOW_ID}, 20260930-120000-a1b2c3); give the full run id")),
    ];
    for (run_ref, message) in cases {
        assert_eq!(cli.error(&["show", run_ref, "--cwd", "/proj"]), message);
        assert_eq!(show(&cli, run_ref), (EXIT_ERROR, String::new()));
    }
}

#[test]
fn starts_with_the_list_line_and_the_whole_task() {
    let cases = [
        show_state(&[("pr_url", Json::from(PR_URL))]),
        show_state(&[("phase", Json::from("build")), ("owner", me()), ("verdict", Json::Null), ("workflow", Json::from("nosuch"))]),
        show_state(&[("phase", Json::from("review")), ("verdict", Json::Null), ("error", Json::from("interrupted"))]),
    ];
    for saved in cases {
        let cli = show_host(&[saved]);
        let lines: Vec<String> = show(&cli, "a1b2c3").1.lines().map(String::from).collect();
        assert_eq!(cli.main(&["list", "--cwd", "/proj"]), 0);
        assert_eq!(lines[0], cli.lines()[0]);
        assert_eq!(lines[1], format!("    {}", long_task()));
        assert!(long_task().len() > 100);
    }
}

#[test]
fn branch_pull_request_and_conflicts() {
    let published = show_state(&[
        ("branch", Json::from("orchestrator/x-a1b2c3")),
        ("base_branch", Json::from("main")),
        ("pr_url", Json::from(PR_URL)),
        ("conflicts", json(r#"["a.py", "b c.py"]"#)),
    ]);
    let out = shown(&[published]);
    assert!(out.contains(&format!(
        "\nbranch     orchestrator/x-a1b2c3\nbase       main\npr         {PR_URL}\nconflicts  a.py, b c.py\nworkspace  w1\n"
    )), "{out}");
    let out = shown(&[show_state(&[("pull_request", Json::Bool(false))])]);
    for label in ["branch", "base", "pr", "conflicts"] {
        assert!(!has_line(&out, label), "{label}: {out}");
    }
}

#[test]
fn branch_cleanup_once_set() {
    let published = show_state(&[("branch", Json::from("orchestrator/x-a1b2c3")), ("pr_url", Json::from(PR_URL))]);
    let out = shown(&[with(published.clone(), &[("branch_cleanup", Json::from("kept: checked out in /proj"))])]);
    assert!(out.contains(&format!("pr         {PR_URL}\ncleanup    kept: checked out in /proj\n")), "{out}");
    for saved in [published.clone(), with(published, &[("branch_cleanup", Json::Null)])] {
        assert!(!has_line(&shown(&[saved]), "cleanup"));
    }
}

#[test]
fn closed_once_set() {
    let out = shown(&[show_state(&[("closed", Json::from("workspace closed; worktree deleted; branch deleted"))])]);
    assert!(out.contains("\nworkspace  w1\nclosed     workspace closed; worktree deleted; branch deleted\n"), "{out}");
    for saved in [show_state(&[]), show_state(&[("closed", Json::Null)])] {
        assert!(!has_line(&shown(&[saved]), "closed"));
    }
}

/// Python's turn: a turn record of step, of round 1 unless change says otherwise, that started and ended at these
/// readings of the host's clock, None for one not known.
fn turn(step: &str, file: &str, start: Option<i64>, end: Option<i64>, fields: &[(&str, Json)]) -> Json {
    let at = |s: Option<i64>| s.map_or(Json::Null, |s| Json::from(iso_utc(HOST_EPOCH + s)));
    let record = json(&format!(
        r#"{{"step": "{step}", "role": "{step}", "round": 1, "quality_round": 0, "file": "{file}", "agent": "claude",
            "model": null, "human_paced": false}}"#
    ));
    let seconds = start.zip(end).map_or(Json::Null, |(s, e)| Json::from(e - s));
    with(with(record, &[("started_at", at(start)), ("ended_at", at(end)), ("seconds", seconds)]), fields)
}

#[test]
fn turns_once_recorded() {
    let turns = Json::Array(vec![
        turn("spec", "spec.md", Some(0), Some(4000), &[("round", Json::from(0)), ("human_paced", Json::Bool(true))]),
        turn("build", "build-1.md", Some(4000), Some(4600), &[("agent", Json::from("codex"))]),
        turn("quality", "quality-1-1.md", Some(4600), Some(4650), &[("quality_round", Json::from(1)), ("agent", Json::Null)]),
        turn("review", "review-1.md", None, Some(4700), &[]),
        turn("build", "build-2.md", Some(4800), None, &[("round", Json::from(2)), ("agent", Json::from("codex"))]),
    ]);
    let out = shown(&[show_state(&[("turns", turns)])]);
    assert!(out.contains(concat!(
        "\nturns\n",
        "    spec     0     claude  2026-10-03 04:00:00  1h 06m   spec.md\n",
        "    build    1     codex   2026-10-03 05:06:40  10m 00s  build-1.md\n",
        "    quality  1 q1  -       2026-10-03 05:16:40  50s      quality-1-1.md\n",
        "    review   1     claude  ?                    ?        review-1.md\n",
        "    build    2     codex   2026-10-03 05:20:00  ?        build-2.md\n",
    )), "{out}");
    for saved in [show_state(&[]), show_state(&[("turns", json("[]"))])] {
        assert!(!shown(&[saved]).lines().any(|l| l.starts_with("turns")));
    }
}

#[test]
fn worktree_of_a_worktree_run() {
    let worktree = "/proj/.orchestrator/worktrees/a1b2c3";
    let out = shown(&[show_state(&[("worktree", Json::from(worktree)), ("branch", Json::from("orchestrator/x-a1b2c3"))])]);
    assert!(out.contains(&format!("\nworktree   {worktree}\nbranch     orchestrator/x-a1b2c3\n")), "{out}");
    assert!(!has_line(&shown(&[show_state(&[])]), "worktree"));
}

#[test]
fn every_agent_with_its_kind_and_pane() {
    let out = shown(&[show_state(&[])]);
    assert!(out.contains("\nagents\n    spec    claude    w1:p1\n    build   codex     w1:p2\n    review  claude    w1:p3\n"), "{out}");
}

#[test]
fn files_oldest_first() {
    let cli = show_host(&[show_state(&[])]);
    for name in ["spec.md", "build-1.md", "review-1.md"] {
        put(&cli, name, "x");
    }
    let out = show(&cli, "a1b2c3").1;
    assert!(out.contains(&format!("\nfiles\n    {SHOW_DIR}/spec.md\n    {SHOW_DIR}/build-1.md\n    {SHOW_DIR}/review-1.md\n")), "{out}");
}

#[test]
fn the_last_review_in_a_later_round() {
    let cli = show_host(&[show_state(&[("phase", Json::from("build")), ("round", Json::from(2)), ("verdict", Json::from(CHANGES_REQUESTED))])]);
    put(&cli, "review-1.md", "\n**VERDICT: CHANGES_REQUESTED**\n\n## Findings\n\n1. a bug\n");
    let out = show(&cli, "a1b2c3").1;
    assert!(out.ends_with(&format!("\nlast review: {SHOW_DIR}/review-1.md\n\n\n## Findings\n\n1. a bug\n")), "{out}");
    assert!(!out.contains("VERDICT"));
}

#[test]
fn a_review_without_a_verdict_line_is_shown_whole() {
    let cli = show_host(&[show_state(&[("phase", Json::from("review")), ("verdict", Json::Null)])]);
    put(&cli, "review-1.md", "half a review");
    let out = show(&cli, "a1b2c3").1;
    assert!(out.ends_with(&format!("\nlast review: {SHOW_DIR}/review-1.md\nhalf a review\n")), "{out}");
}

#[test]
fn no_last_review() {
    let solo = workflow_definition(&solo());
    let cases = [
        show_state(&[("workflow", Json::from("solo")), ("workflow_definition", solo), ("verdict", Json::from("FINISHED"))]),
        show_state(&[("phase", Json::from("spec")), ("round", Json::from(0)), ("verdict", Json::Null)]),
        show_state(&[("workflow", Json::from("nosuch"))]),
    ];
    for saved in &cases {
        let cli = show_host(std::slice::from_ref(saved));
        for name in ["review-1.md", "build.md", "build-1.md"] {
            put(&cli, name, "VERDICT: APPROVE\n");
        }
        let (status, out) = show(&cli, "a1b2c3");
        assert_eq!(status, 0);
        assert!(!out.contains("last review"), "{out}");
    }
    assert!(shown(&cases[2..]).contains("\nworkflow   nosuch: this orchestrator cannot load it\n"));
    assert!(shown(&cases[..1]).contains("\nworkflow   solo: build\n"));
}

/// An ssh on PATH, in dir's bin, that logs the command it is given, one line a call in dir's calls, and runs it here
/// with sh, as sshd would run it there.
fn fake_ssh(dir: &TempDir) -> ProcessHost {
    let bin = dir.join("bin");
    std::fs::create_dir_all(&bin).unwrap();
    let ssh = format!("{bin}/ssh");
    let script = format!(
        "#!/bin/sh\n[ \"$1\" = -o ] || exit 99\neval \"last=\\${{$#}}\"\nprintf '%s\\n' \"$last\" | tr '\\n' ' ' >> '{}'\necho >> '{}'\nexec sh -c \"$last\"\n",
        dir.join("calls"),
        dir.join("calls")
    );
    std::fs::write(&ssh, script).unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&ssh, std::fs::Permissions::from_mode(0o755)).unwrap();
    let path = format!("{bin}:{}", std::env::var("PATH").unwrap_or_default());
    let vars = std::env::vars_os().filter(|(k, _)| k != "PATH").chain([("PATH".into(), path.into())]);
    ProcessHost::in_environment(vars, &Interrupt::new()).over_ssh("remote-host")
}

#[test]
fn on_a_machine_everything_goes_over_ssh() {
    let dir = TempDir::new();
    let proj = dir.join("proj");
    let run_dir = format!("{proj}/.orchestrator/runs/{SHOW_ID}");
    std::fs::create_dir_all(&run_dir).unwrap();
    let saved = show_state(&[("phase", Json::from("build")), ("round", Json::from(2)), ("verdict", Json::from(CHANGES_REQUESTED))]);
    let at = |seconds| std::time::UNIX_EPOCH + std::time::Duration::from_secs(seconds);
    let write = |name: &str, text: &str, seconds: u64| {
        let path = format!("{run_dir}/{name}");
        std::fs::write(&path, text).unwrap();
        std::fs::File::options().write(true).open(&path).unwrap().set_modified(at(seconds)).unwrap();
    };
    write("state.json", &saved.dumps(), 1_000);
    write("spec.md", "spec", 7);
    write("review-1.md", "VERDICT: CHANGES_REQUESTED\nremote findings\n", 9);
    write("build-1.md", "report", 8);
    let host = fake_ssh(&dir);
    let cli = alive();
    let target = ["--machine", "M", "--cwd", "~/proj"].map(String::from);
    cli.with(|world| {
        let mut world = *world;
        world.with.host = &host;
        show_run(&world, &proj, "a1b2c3", &target)
    })
    .unwrap();
    let out = cli.take_out();
    let review = format!("{run_dir}/review-1.md");
    assert!(out.ends_with(&format!(
        "\nfiles\n    {run_dir}/spec.md\n    {run_dir}/build-1.md\n    {review}\n\nlast review: {review}\nremote findings\n"
    )), "{out}");
    assert!(out.contains("resume: orchestrator.py resume a1b2c3 --machine M --cwd '~/proj'"), "{out}");
    // Each command went through ssh, the reads of the reviews among them, latest round first.
    let calls = std::fs::read_to_string(dir.join("calls")).unwrap();
    let reads: Vec<&str> = calls.lines().filter(|c| c.contains("/review-")).collect();
    assert_eq!(reads.len(), 2, "{calls}");
    assert!(reads[0].contains("review-2.md") && reads[1].contains("review-1.md"), "{calls}");
    assert!(calls.lines().any(|c| c.contains("state.json")), "{calls}");
}

#[test]
fn help() {
    let config = crate::env::ConfigDir::at("/nowhere");
    for (argv, text) in [(&["--help"][..], "show"), (&["show", "--help"][..], "RUN")] {
        let e = crate::cli::parse_args(argv.iter().map(std::ffi::OsString::from), &config).unwrap_err();
        assert_eq!(e.exit_code(), 0);
        let help = e.to_string();
        assert!(help.contains(text), "{help}");
        if argv[0] == "show" {
            assert!(help.contains("the run id, or the six-character key at its end"), "{help}");
        }
    }
}

// -- TestQualityGateOffOnResume.test_show_names_the_gate -------------------------------------------------------------

#[test]
fn show_names_the_gate() {
    let cases = [
        (vec![("quality_job", Json::from(JOB))], Some(format!("quality    {JOB}"))),
        (vec![("quality_gate_disabled", Json::Bool(true))], Some("quality    off (turned off by resume)".to_string())),
        (vec![], None),
    ];
    for (fields, line) in cases {
        let cli = alive();
        let mut fields = fields;
        fields.push(("cwd", Json::from("/proj")));
        cli.save(&with(run("build"), &fields), 0);
        cli.with(|world| show_run(world, "/proj", "c0ffee", &[])).unwrap();
        let quality: Vec<String> = cli.lines().into_iter().filter(|l| l.starts_with("quality ")).collect();
        assert_eq!(quality, line.into_iter().collect::<Vec<_>>());
    }
}

// -- TestBlockedListing, and TestBlocked.test_the_wait_is_saved_and_listed's print_runs half -------------------------

#[test]
fn a_running_blocked_run_shows_its_question_and_pane() {
    let blocked = with(mine(), &[("blocked", block()), ("agents", json(r#"{"build": {"pane": "w1:p2"}}"#))]);
    assert_eq!(listed(&[(40, blocked.clone()), (STALE_SECONDS + 1, blocked)], &[]), [
        format!("20260930-070000-c0ffee  build    round 1  blocked: {QUESTION} (pane w1:p2)"),
        "20260930-070000-c0ffee  build    round 1  stale: no heartbeat for 5m; resume: orchestrator.py resume c0ffee".into(),
    ]);
}

#[test]
fn without_a_known_pane_only_the_question() {
    let unknown = with(mine(), &[("blocked", block()), ("workflow", Json::from("nosuch"))]);
    let no_agent = with(mine(), &[("blocked", block())]);
    assert_eq!(listed(&[(40, unknown), (40, no_agent)], &[]), [
        format!("20260930-070000-c0ffee  build    round 1  blocked: {QUESTION}  [nosuch workflow]"),
        format!("20260930-070000-c0ffee  build    round 1  blocked: {QUESTION}"),
    ]);
}

#[test]
fn show_of_a_blocked_run() {
    let cli = alive();
    cli.save(&show_state(&[("phase", Json::from("build")), ("owner", me()), ("verdict", Json::Null), ("blocked", block())]), 40);
    assert_eq!(cli.main(&["show", "a1b2c3", "--cwd", "/proj"]), 0);
    assert_eq!(cli.lines()[0], format!("{SHOW_ID}  build    round 1  blocked: {QUESTION} (pane w1:p2)"));
}

#[test]
fn the_wait_is_listed() {
    // The state a Workflow saves while the Builder's BLOCKED report waits (crate::workflow's blocked tests).
    let saved = json(&format!(
        r#"{{"run_id": "20260929-120000-a1b2c3", "task": "add a rate limiter", "cwd": "/proj", "phase": "build",
            "round": 1, "owner": {{"host": "box", "pid": 7, "started_at": "2026-09-29T12:00:00+00:00"}},
            "agents": {{"spec": {{"name": "spec-a1b2c3", "pane": "w1:p1"}}, "build": {{"name": "build-a1b2c3", "pane": "w1:p2"}}}},
            "blocked": {{"file": "build-1.md", "question": "{QUESTION}", "k": 1}}, "prompted": null}}"#
    ));
    let cli = alive();
    cli.with(|world| {
        let world = Connected { local_host: "box", ..*world };
        print_runs(&world, &[(40, saved)], &[]);
    });
    assert_eq!(cli.lines()[0], format!("20260929-120000-a1b2c3  build    round 1  blocked: {QUESTION} (pane w1:p2)"));
}

// -- TestQualityCLI.test_list_shows_the_quality_phase, TestWorkflowFiles' gated step ---------------------------------

#[test]
fn list_shows_the_quality_phase() {
    let lines = listed(&[(1_000_000, with(run("quality"), &[("quality_round", Json::from(2))]))], &[]);
    assert!(lines[0].starts_with("20260930-070000-c0ffee  quality  round 1 q2  stale"), "{}", lines[0]);
}

#[test]
fn list_reads_the_gated_step_from_the_saved_definition() {
    let state = with(run("impl"), &[
        ("quality_round", Json::from(2)),
        ("workflow", Json::from("impl")),
        ("workflow_definition", workflow_definition(&impl_())),
    ]);
    let lines = listed(&[(1_000_000, state)], &[]);
    assert!(lines[0].starts_with("20260930-070000-c0ffee  impl     round 1 q2"), "{}", lines[0]);
}

// -- TestRunMetrics.test_the_metrics_command_prints_a_line_per_run_that_is_done --------------------------------------

#[test]
fn the_metrics_command_prints_a_line_per_run_that_is_done() {
    let proj = TempDir::new();
    let runs = proj.join(".orchestrator/runs");
    for (run_id, metrics) in [
        ("1-aaaaaa", Some(r#"{"run_id": "1-aaaaaa", "outcome": "APPROVE"}"#)),
        ("2-bbbbbb", None),
        ("3-cccccc", Some("{\n \"run_id\": \"3-cccccc\",\n \"outcome\": \"error\"\n}")),
    ] {
        std::fs::create_dir_all(format!("{runs}/{run_id}")).unwrap();
        if let Some(metrics) = metrics {
            std::fs::write(format!("{runs}/{run_id}/metrics.json"), metrics).unwrap();
        }
    }
    let cli = Cli::on(Rc::new(FakeHost::default()));
    cli.f.run_processes();
    let argv = ["metrics", "--cwd", &proj.path()];
    // A file that is not one JSON line is corrupt; the run writes each as one.
    assert_eq!(cli.main(&argv), EXIT_ERROR);
    std::fs::write(format!("{runs}/3-cccccc/metrics.json"), "{\"run_id\": \"3-cccccc\", \"outcome\": \"error\"}\n").unwrap();
    cli.take_out();
    assert_eq!(cli.main(&argv), 0);
    assert_eq!(cli.lines(), [
        r#"{"run_id": "1-aaaaaa", "outcome": "APPROVE"}"#,
        r#"{"run_id": "3-cccccc", "outcome": "error"}"#,
    ]);
}
