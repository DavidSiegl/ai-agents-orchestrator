//! tests/test_orchestrator.py's TestWorkflow, TestPullRequest, TestUpToDateBase, TestHeartbeat and
//! TestOneRunPerCheckout, case for case, on the fixture's fakes (fixture.rs).

use std::rc::Rc;

use super::fixture::*;
use super::{RunSettings, Stopped};
use crate::constants::{APPROVE, CHANGES_REQUESTED, STALL_SECONDS};
use crate::error::OrchestratorError;
use crate::handoff::parse_verdict;
use crate::harness::AgentSettings;
use crate::herdr::fake::{Call, Turn};
use crate::herdr::Direction;
use crate::host::fake::FakeHost;
use crate::host::Host;
use crate::py::json::{Json, Map};
use crate::state::RunState;

mod heartbeat;
mod one_run;
mod pull_request;

type Script = Vec<(&'static str, Vec<Turn>)>;

/// The default workflow approved in round 1.
fn approved() -> Script {
    vec![("spec", vec![spec_turn()]), ("build", vec![build_turn(1)]), ("review", vec![review_turn(1, APPROVE)])]
}

/// RunSettings with the run ending in no pull request.
fn no_pr() -> RunSettings {
    RunSettings { pull_request: false, ..RunSettings::default() }
}

fn max_rounds(n: u32) -> RunSettings {
    RunSettings { max_rounds: n, ..RunSettings::default() }
}

/// The message of a run that failed.
fn failure(outcome: Result<Option<String>, Stopped>) -> String {
    match outcome {
        Err(Stopped::Failed(e)) => e.0,
        other => panic!("the run did not fail: {other:?}"),
    }
}

fn verdict(v: &str) -> Result<Option<String>, Stopped> {
    Ok(Some(v.to_string()))
}

/// The names of the agents each command of kind went to, in order.
fn names(f: &Fixture, kind: &str) -> Vec<String> {
    f.herdr
        .state()
        .calls
        .iter()
        .filter_map(|c| match (c, kind) {
            (Call::Start { name, .. }, "start") | (Call::Prompt { name }, "prompt") => Some(name.clone()),
            _ => None,
        })
        .collect()
}

fn text(saved: &Map<Json>, key: &str) -> Option<String> {
    saved.get(key).and_then(Json::as_str).map(String::from)
}

#[test]
fn approved_first_round() {
    let f = Fixture::new(approved());
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(names(&f, "start"), ["spec-a1b2c3", "build-a1b2c3", "review-a1b2c3"]);
    let calls = f.herdr.state().calls.clone();
    assert!(calls.contains(&Call::Split { pane: "w1:p1".into(), direction: Direction::Right }));
    assert!(calls.contains(&Call::Split { pane: "w1:p2".into(), direction: Direction::Down }));
    assert!(calls.contains(&Call::Focus { name: "spec-a1b2c3".into() }));
    assert_eq!(f.host.state().files.get("/proj/.orchestrator/.gitignore").map(String::as_str), Some("*\n"));
    let saved = f.saved();
    assert_eq!(
        (text(&saved, "phase"), text(&saved, "verdict"), text(&saved, "base")),
        (Some("done".into()), Some(APPROVE.into()), Some("abc123".into()))
    );
    assert_eq!(f.told.titles().last().unwrap(), &format!("Run finished: {APPROVE}"));
}

#[test]
fn review_prompt_names_the_base_commit() {
    let seen = seen();
    let script = vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![recording(review_turn(1, APPROVE), &seen)]),
    ];
    let f = Fixture::new(script);
    f.workflow(RunSettings::default()).run().unwrap();
    assert!(seen.borrow()[0].contains("git diff abc123"));
}

#[test]
fn changes_requested_loop_back_to_builder() {
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1), build_turn(2)]),
        ("review", vec![review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)]),
    ]);
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(wf.state().round, 2);
    assert_eq!(
        names(&f, "prompt"),
        ["spec-a1b2c3", "build-a1b2c3", "review-a1b2c3", "build-a1b2c3", "review-a1b2c3"]
    );
}

#[test]
fn stops_after_max_rounds() {
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1), build_turn(2)]),
        ("review", vec![review_turn(1, CHANGES_REQUESTED), review_turn(2, CHANGES_REQUESTED)]),
    ]);
    let mut wf = f.workflow(max_rounds(2));
    assert_eq!(wf.run(), verdict(CHANGES_REQUESTED));
    assert_eq!(wf.state().phase, "done");
}

#[test]
fn blocked_turn_notifies_once_then_waits_for_the_file() {
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![showing("blocked")]),
    ]);
    let (herdr, host) = (Rc::clone(&f.herdr), Rc::clone(&f.host));
    let review = f.state.review_path(1);
    let mut polls = 0;
    f.clock.hook(move |_| {
        polls += 1;
        if polls == 3 {
            herdr.state().statuses.insert("review-a1b2c3".into(), "working".into());
            host.write(&review, "VERDICT: APPROVE", false).unwrap();
        }
    });
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert_eq!(f.told.titles().iter().filter(|t| *t == "Reviewer needs your answer").count(), 1);
}

#[test]
fn settled_status_is_not_the_end_of_a_turn() {
    // Claude Code reports done while a background task it started runs on.
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![showing("done")]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    let (host, build) = (Rc::clone(&f.host), f.state.build_path(1));
    f.clock.hook(move |_| host.write(&build, "report", false).unwrap());
    assert_eq!(f.workflow(no_pr()).run(), verdict(APPROVE));
}

#[test]
fn idle_without_handoff_notifies_the_human() {
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![idle()]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    let (host, build) = (Rc::clone(&f.host), f.state.build_path(1));
    f.clock.hook(move |now| {
        if now > (STALL_SECONDS + 10) as f64 {
            host.write(&build, "report", false).unwrap();
        }
    });
    assert_eq!(f.workflow(no_pr()).run(), verdict(APPROVE));
    let stalls = f.told.titles().iter().filter(|t| *t == "Builder is idle without writing build-1.md").count();
    assert_eq!(stalls, 1);
}

#[test]
fn idle_spec_collector_is_not_a_stall() {
    let f = Fixture::new(vec![
        ("spec", vec![idle()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    let (host, spec) = (Rc::clone(&f.host), f.state.spec_path());
    f.clock.hook(move |now| {
        if now > (10 * STALL_SECONDS) as f64 {
            host.write(&spec, "spec", false).unwrap();
        }
    });
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert!(!f.told.titles().iter().any(|t| t.contains("idle")));
}

#[test]
fn startup_dialog_waits_for_the_human() {
    let f = Fixture::new(approved());
    f.herdr.state().blocked_at_start.insert("spec".into());
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert_eq!(f.told.titles()[0], "Spec Collector needs your answer");
    assert_eq!(f.herdr.state().waits, [["idle", "done"]]);
}

#[test]
fn spec_collector_exiting_aborts() {
    let f = Fixture::new(vec![("spec", vec![idle()])]);
    let herdr = Rc::clone(&f.herdr);
    f.clock.hook(move |_| herdr.state().statuses.clear());
    let message = failure(f.workflow(RunSettings::default()).run());
    assert!(message.contains("Spec Collector exited without writing"), "{message}");
    assert!(text(&f.saved(), "error").unwrap().contains("exited without writing"));
}

#[test]
fn builder_timeout() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![idle()])]);
    let message = failure(f.workflow(RunSettings { turn_timeout: 60, ..RunSettings::default() }).run());
    assert!(message.starts_with("the Builder did not write "), "{message}");
    assert!(message.contains("build-1.md within 60s"), "{message}");
}

#[test]
fn empty_handoff_aborts() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![writes(|s| s.build_path(1), "\n", "done")])]);
    let message = failure(f.workflow(RunSettings::default()).run());
    assert!(message.contains("build-1.md was written empty by the Builder"), "{message}");
}

#[test]
fn review_without_verdict_is_asked_for_once_more() {
    let seen = seen();
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![recording(malformed_review(1, "looks fine"), &seen), recording(review_turn(1, APPROVE), &seen)]),
    ]);
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let path = f.state.review_path(1);
    let rejected = format!("{}/review-1.rejected.md", f.state.dir());
    let seen = seen.borrow();
    assert_eq!(seen[1], retry_prompt(&path));
    assert!(seen[1].contains(&path));
    assert!(seen[1].contains(&rejected));
    assert!(seen[1].contains(&format!("`VERDICT: {APPROVE}`")));
    assert!(seen[1].contains(&format!("`VERDICT: {CHANGES_REQUESTED}`")));
    assert_eq!(names(&f, "prompt").iter().filter(|n| *n == "review-a1b2c3").count(), 2);
    let files = f.host.state().files.clone();
    assert_eq!(files.get(&rejected).map(String::as_str), Some("looks fine"));
    assert_eq!(parse_verdict(files.get(&path).unwrap()), Some(APPROVE));
    let retries: Vec<String> = f.told.lines().into_iter().filter(|l| l.contains("prompting once more")).collect();
    assert_eq!(
        retries,
        ["[review] review-1.md does not start with a VERDICT line; moved it to review-1.rejected.md, prompting once more"]
    );
    // The run goes on as if the first review had been valid: a pull request, and the retry spent.
    assert_eq!(f.host.state().prs.len(), 1);
    let saved = f.saved();
    assert_eq!((text(&saved, "phase"), saved.get("round"), text(&saved, "verdict")), (
        Some("done".into()),
        Some(&Json::from(1)),
        Some(APPROVE.into())
    ));
    assert_eq!(saved.get("retried"), Some(&Json::Array(vec![Json::from("review-1.md")])));
    assert_eq!(saved.get("retrying"), Some(&Json::Null));
}

#[test]
fn empty_review_is_asked_for_once_more() {
    let seen = seen();
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![malformed_review(1, " \n\n"), recording(review_turn(1, CHANGES_REQUESTED), &seen)]),
    ]);
    assert_eq!(f.workflow(max_rounds(1)).run(), verdict(CHANGES_REQUESTED));
    assert_eq!(*seen.borrow(), [retry_prompt(&f.state.review_path(1))]);
    let rejected = format!("{}/review-1.rejected.md", f.state.dir());
    assert_eq!(f.host.state().files.get(&rejected).map(String::as_str), Some(" \n\n"));
}

#[test]
fn review_malformed_twice_aborts() {
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![malformed_review(1, "looks fine"), malformed_review(1, "still no verdict")]),
    ]);
    let message = failure(f.workflow(RunSettings::default()).run());
    assert!(
        message.ends_with(
            "review-1.md does not start with a VERDICT line, and its one retry was already used; fix it, or delete it \
             to have it written again, then resume"
        ),
        "{message}"
    );
    let files = f.host.state().files.clone();
    assert_eq!(files.get(&format!("{}/review-1.rejected.md", f.state.dir())).map(String::as_str), Some("looks fine"));
    assert_eq!(files.get(&f.state.review_path(1)).map(String::as_str), Some("still no verdict"));
    let saved = f.saved();
    assert_eq!(saved.get("retried"), Some(&Json::Array(vec![Json::from("review-1.md")])));
    assert_eq!((saved.get("retrying"), saved.get("prompted")), (Some(&Json::Null), Some(&Json::Null)));

    // A resume does not grant a second retry.
    let state = RunState::from_saved(&saved).unwrap();
    let f2 = Fixture::resume(Rc::clone(&f.host), state, vec![("review", vec![])], &["review"], &[]);
    let message = failure(f2.workflow(RunSettings::default()).run());
    assert!(message.contains("its one retry was already used"), "{message}");
    assert!(!f2.herdr.state().calls.contains(&Call::Prompt { name: "review-a1b2c3".into() }));
}

#[test]
fn empty_review_after_a_retry_aborts() {
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![malformed_review(1, "looks fine"), malformed_review(1, "\n")]),
    ]);
    let message = failure(f.workflow(RunSettings::default()).run());
    assert!(
        message.contains("review-1.md was written empty by the Reviewer, and its one retry was already used; fix it"),
        "{message}"
    );
}

#[test]
fn each_round_has_its_own_retry() {
    let seen = seen();
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1), build_turn(2)]),
        (
            "review",
            vec![
                malformed_review(1, "looks fine"),
                review_turn(1, CHANGES_REQUESTED),
                recording(malformed_review(2, "looks fine"), &seen),
                recording(review_turn(2, APPROVE), &seen),
            ],
        ),
    ]);
    let mut wf = f.workflow(max_rounds(2));
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(seen.borrow()[1], retry_prompt(&f.state.review_path(2)));
    let files = f.host.state().files.clone();
    assert!(files.contains_key(&format!("{}/review-1.rejected.md", f.state.dir())));
    assert!(files.contains_key(&format!("{}/review-2.rejected.md", f.state.dir())));
    // The retries used no review round: two rounds were enough under --max-rounds 2.
    assert_eq!((wf.state().round, wf.state().retried.clone()), (2, vec!["review-1.md".into(), "review-2.md".into()]));
}

#[test]
fn not_a_git_repo() {
    let seen = seen();
    let script = vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![recording(review_turn(1, APPROVE), &seen)]),
    ];
    let f = Fixture::on(Rc::new(FakeHost::new(None, "main")), new_state(), script);
    f.workflow(no_pr()).run().unwrap();
    assert!(seen.borrow()[0].contains("not a git repository"));
}

/// The arguments each role's agent started with, by role.
fn start_args(settings: RunSettings) -> (Vec<(String, Vec<String>)>, Fixture) {
    let f = Fixture::new(approved());
    f.workflow(settings).run().unwrap();
    let args = f
        .herdr
        .state()
        .calls
        .iter()
        .filter_map(|c| match c {
            Call::Start { name, args, .. } => Some((name.split('-').next().unwrap().to_string(), args.clone())),
            _ => None,
        })
        .collect();
    (args, f)
}

fn agents(permission_mode: Option<&str>, models: &[(&str, &str)]) -> RunSettings {
    let agents = AgentSettings {
        permission_mode: permission_mode.map(String::from),
        models: models.iter().map(|(r, m)| (*r, m.to_string())).collect(),
        kinds: Map::new(),
    };
    RunSettings { agents, ..RunSettings::default() }
}

#[test]
fn agent_args_reach_every_role() {
    let (args, f) = start_args(agents(Some("auto"), &[]));
    assert!(args.iter().all(|(_, a)| a == &["--permission-mode", "auto"]), "{args:?}");
    assert!(f.herdr.state().kinds.iter().all(|(_, kind)| kind == "claude"));
}

#[test]
fn no_models_adds_no_model_arg() {
    let (args, _) = start_args(RunSettings::default());
    assert_eq!(args, [("spec".into(), vec![]), ("build".into(), vec![]), ("review".into(), vec![])]);
}

#[test]
fn each_role_gets_its_own_model() {
    let (args, _) = start_args(agents(Some("auto"), &[("spec", "sonnet"), ("review", "claude-opus-5-5")]));
    let strings = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    assert_eq!(
        args,
        [
            ("spec".into(), strings(&["--permission-mode", "auto", "--model", "sonnet"])),
            ("build".into(), strings(&["--permission-mode", "auto"])),
            ("review".into(), strings(&["--permission-mode", "auto", "--model", "claude-opus-5-5"])),
        ]
    );
}

/// What Workflow::new refuses: a run ending in a pull request whose workflow has no editing step.
#[test]
fn a_workflow_without_an_editing_step_cannot_end_in_a_pull_request() {
    let notes = crate::pipeline::Step::new("notes", "writer", "notes.md", "Write notes on {task}.");
    let roles = crate::test_support::roles(&[("writer", "Writer")]);
    let pipeline = crate::pipeline::Pipeline::new("notes", roles, vec![notes]).unwrap();
    let f = Fixture::new(vec![]);
    let refused = f.try_workflow(RunSettings { pipeline: Some(pipeline), ..RunSettings::default() }).err();
    assert_eq!(
        refused,
        Some(OrchestratorError(
            "workflow notes has no editing step, so it cannot end in a pull request; pass --no-pr".into()
        ))
    );
}

/// Each prompt of a step that can block ends with BLOCKED_NOTE, and the verdict step's and the human-paced step's
/// do not.
#[test]
fn only_the_steps_that_can_block_hear_the_blocked_note() {
    let (spec, build, review) = (seen(), seen(), seen());
    let f = Fixture::new(vec![
        ("spec", vec![recording(spec_turn(), &spec)]),
        ("build", vec![recording(build_turn(1), &build)]),
        ("review", vec![recording(review_turn(1, APPROVE), &review)]),
    ]);
    f.workflow(RunSettings::default()).run().unwrap();
    assert!(build.borrow()[0].ends_with(&note()));
    assert!(!spec.borrow()[0].ends_with(&note()));
    assert!(!review.borrow()[0].ends_with(&note()));
}

/// A step before the first editing step is in round 0, so its prev_ placeholders name round -1's file, as Python's
/// n - 1 does, and its earlier_ ones name none.
#[test]
fn a_step_before_round_1_names_round_minus_1() {
    use crate::pipeline::{Pipeline, Step};
    let plan = Step::new("plan", "planner", "plan.md", "Plan {task}; before: {prev_build_path}; [{earlier_build_paths}]");
    let mut build = Step::new("build", "builder", "build-{n}.md", "Build {plan_path}.");
    build.edits = true;
    let roles = crate::test_support::roles(&[("planner", "Planner"), ("builder", "Builder")]);
    let pipeline = Pipeline::new("plan-build", roles, vec![plan, build]).unwrap();
    let seen = seen();
    let dir = new_state().dir();
    let mut state = new_state();
    state.phase = "plan".into();
    let f = Fixture::on(Rc::new(FakeHost::default()), state, vec![
        ("planner", vec![recording(writes(|s| format!("{}/plan.md", s.dir()), "the plan", "done"), &seen)]),
        ("builder", vec![writes(|s| s.build_path(1), "built", "done")]),
    ]);
    let settings = RunSettings { pipeline: Some(pipeline), pull_request: false, ..RunSettings::default() };
    assert_eq!(f.workflow(settings).run(), verdict("FINISHED"));
    assert!(seen.borrow()[0].starts_with(&format!("Plan add a rate limiter; before: {dir}/build--1.md; []")));
}
