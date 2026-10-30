//! TestSeededSpec, and TestTurnTimings' test_a_seeded_spec_has_no_record: a run given its spec with run --spec skips
//! the contract step's turn, its spec written as that step's file.
//!
//! Not here: TestSpecCLI, main's (crate::cli's tests and crate::cli::args'), but for its read_spec_file cases, which
//! are crate::spec_file's.

use std::cell::RefCell;

use super::*;
use crate::pipeline::{Pipeline, Step, DEFAULT_WORKFLOW};
use crate::spec_file::SpecFile;
use crate::test_support::{quick, roles};

const D: &str = "/proj/.orchestrator/runs/20260929-120000-a1b2c3";
/// CRLF and trailing blanks, which the run's copy keeps.
const GIVEN_SPEC: &str = "# Add a token-bucket rate limiter\r\n\r\n## Goal\r\nlimit requests  \n\n";
const SPEC_SOURCE: &str = "/home/me/spec.md";

/// A new run's state, as main makes it for pipeline with --spec.
fn seeded_run(pipeline: &Pipeline) -> RunState {
    let mut state = new_state();
    state.phase = pipeline.seeded_start().unwrap().id.clone();
    state.workflow = pipeline.name().to_string();
    state
}

/// The settings of a run of pipeline given text as its spec from SPEC_SOURCE.
fn given(pipeline: &Pipeline, text: &str) -> RunSettings {
    let spec = SpecFile { path: SPEC_SOURCE.into(), text: text.into() };
    RunSettings { pipeline: Some(pipeline.clone()), spec: Some(spec), ..RunSettings::default() }
}

/// A new run of pipeline given text as its spec, its roles played by script, run: the fixture, its verdict and the
/// first state.json it wrote.
fn run_seeded(script: Script, text: &str, pipeline: &Pipeline) -> (Fixture, Result<Option<String>, Stopped>, Map<Json>) {
    let f = Fixture::on(Rc::new(FakeHost::default()), seeded_run(pipeline), script);
    let first = Rc::new(RefCell::new(None));
    let record = Rc::clone(&first);
    f.host.state().before_write = Some(Box::new(move |path, text| {
        let mut first = record.borrow_mut();
        if path.ends_with("/state.json") && first.is_none() {
            let Ok(Json::Object(saved)) = Json::parse(text) else { panic!("state.json is no object") };
            *first = Some(saved);
        }
        Ok(())
    }));
    let verdict = f.workflow(given(pipeline, text)).run();
    let first = first.borrow_mut().take().expect("a state.json written");
    (f, verdict, first)
}

/// The (name, pane) of each agent started.
fn started(f: &Fixture) -> Vec<(String, String)> {
    starts(f).into_iter().map(|(name, pane, _)| (name, pane)).collect()
}

fn agents_started(names: &[(&str, &str)]) -> Vec<(String, String)> {
    names.iter().map(|(name, pane)| (name.to_string(), pane.to_string())).collect()
}

#[test]
fn the_spec_is_the_contract_and_the_interview_is_skipped() {
    let seen = seen();
    let (f, verdict_given, first_saved) = run_seeded(
        vec![("build", vec![recording(build_turn(1), &seen)]), ("review", vec![review_turn(1, APPROVE)])],
        GIVEN_SPEC,
        &DEFAULT_WORKFLOW,
    );
    assert_eq!(verdict_given, verdict(APPROVE));
    let spec = format!("{D}/spec.md");
    assert_eq!(f.host.state().files.get(&spec).map(String::as_str), Some(GIVEN_SPEC));
    let writes = f.host.state().writes.clone();
    let index = |path: &str| writes.iter().position(|w| w == path).unwrap();
    assert!(index(&spec) < index(&format!("{D}/state.json")));
    let agents = first_saved.get("agents").map(Json::dumps);
    assert_eq!(
        (text(&first_saved, "phase"), first_saved.get("round"), agents),
        (Some("build".into()), Some(&Json::from(0)), Some("{}".into()))
    );
    assert!(seen.borrow()[0].contains(&format!("The spec in {D}/spec.md was agreed")));
    // No Spec Collector: no agent, no pane, no notification.
    assert_eq!(started(&f), agents_started(&[("build-a1b2c3", "w1:p1"), ("review-a1b2c3", "w1:p2")]));
    assert_eq!(calls_of(&f, "split"), [split("w1:p1", Direction::Right)]);
    assert_eq!(f.herdr.state().panes, 2);
    assert!(calls_of(&f, "focus").is_empty());
    assert!(!f.told.titles().iter().any(|t| t.contains("waiting")));
    let saved = f.saved();
    let roles: Vec<&str> = saved.get("agents").and_then(Json::as_object).unwrap().keys().collect();
    assert_eq!(roles, ["build", "review"]);
    let log = f.told.lines();
    let copied = format!("[spec] spec.md is a copy of {SPEC_SOURCE}; skipping the Spec Collector's interview");
    assert!(log.contains(&copied), "{log:?}");
    assert!(!log.iter().any(|l| l.contains("already written")));
    let pr = f.host.state().prs[0].clone();
    assert_eq!(
        (pr.title.as_str(), pr.head.as_str()),
        ("Add a token-bucket rate limiter", "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3")
    );
}

#[test]
fn a_spec_without_a_title_titles_by_the_task() {
    let script = vec![("build", vec![build_turn(1)]), ("review", vec![review_turn(1, APPROVE)])];
    let (f, verdict_given, _) = run_seeded(script, "Limit the requests.\n", &DEFAULT_WORKFLOW);
    assert_eq!(verdict_given, verdict(APPROVE));
    let pr = f.host.state().prs[0].clone();
    assert_eq!((pr.title.as_str(), pr.head.as_str()), ("add a rate limiter", "orchestrator/add-a-rate-limiter-a1b2c3"));
}

#[test]
fn a_seeded_run_resumes_like_any_other() {
    let f = Fixture::on(Rc::new(FakeHost::default()), seeded_run(&DEFAULT_WORKFLOW), vec![("build", vec![build_turn(1)])]);
    f.play("review", vec![then_interrupted(&f.interrupt, idle())]);
    assert_eq!(f.workflow(given(&DEFAULT_WORKFLOW, GIVEN_SPEC)).run(), Err(Stopped::Interrupted));
    let saved = RunState::from_saved(&f.saved()).unwrap();
    assert_eq!((saved.phase.as_str(), saved.round, saved.error.as_deref()), ("review", 1, Some("interrupted")));
    // As main resumes it: no spec given, and here a new workspace, since herdr lost the old one.
    let pull_request = saved.pull_request;
    let f2 = Fixture::on(Rc::clone(&f.host), saved, vec![("review", vec![review_turn(1, APPROVE)])]);
    assert_eq!(f2.workflow(RunSettings { pull_request, ..RunSettings::default() }).run(), verdict(APPROVE));
    assert_eq!(f2.host.state().files.get(&format!("{D}/spec.md")).map(String::as_str), Some(GIVEN_SPEC));
    assert_eq!(started(&f2), agents_started(&[("review-a1b2c3", "w1:p2")]));
    assert_eq!(calls_of(&f2, "split"), [split("w1:p1", Direction::Right)]);
    assert_eq!(f2.host.state().prs[0].title, "Add a token-bucket rate limiter");
}

#[test]
fn a_contract_role_with_a_later_step_lays_out_last() {
    let plan = Step::new("plan", "plan", "plan.md", "Plan {task}; write {path}.");
    let mut build = Step::new("build", "build", "build-{n}.md", "Build {plan_path}; report to {build_path}.");
    (build.again, build.edits) = (Some("Fix {prev_check_path}; report to {build_path}.".into()), true);
    let mut check = Step::new("check", "plan", "check-{n}.md", "Check {change} against {plan_path}; write {path}.");
    check.loop_to = Some("build".into());
    let planned =
        Pipeline::new("planned", roles(&[("plan", "Planner"), ("build", "Builder")]), vec![plan, build, check]).unwrap();
    let seen = seen();
    let script = vec![
        ("build", vec![recording(build_turn(1), &seen)]),
        ("plan", vec![writes(|s| format!("{}/check-1.md", s.dir()), &format!("VERDICT: {APPROVE}\n"), "done")]),
    ];
    let (f, verdict_given, _) = run_seeded(script, GIVEN_SPEC, &planned);
    assert_eq!(verdict_given, verdict(APPROVE));
    assert_eq!(*seen.borrow(), [format!("Build {D}/plan.md; report to {D}/build-1.md.{}", note())]);
    assert_eq!(started(&f), agents_started(&[("build-a1b2c3", "w1:p1"), ("plan-a1b2c3", "w1:p2")]));
    assert_eq!(calls_of(&f, "split"), [split("w1:p1", Direction::Right)]);
    let copied = format!("[plan] plan.md is a copy of {SPEC_SOURCE}; skipping the Planner's turn");
    assert!(f.told.lines().contains(&copied), "{:?}", f.told.lines());
}

#[test]
fn a_workflow_without_a_contract_step_cannot_take_a_spec() {
    assert_eq!(
        quick().seeded_start().unwrap_err().0,
        "workflow quick starts with step build, which edits or writes a file per round; --spec needs a first step \
         that writes the spec"
    );
}

#[test]
fn a_workflow_with_only_a_contract_step_cannot_take_a_spec() {
    let solo = Pipeline::new("solo", roles(&[("spec", "Spec Collector")]), vec![Step::new("spec", "spec", "spec.md", "Write {path}.")])
        .unwrap();
    let error = solo.seeded_start().unwrap_err().0;
    assert!(error.starts_with("workflow solo has no step after spec, so with --spec"), "{error}");
}

#[test]
fn a_seeded_spec_has_no_record() {
    // TestTurnTimings.test_a_seeded_spec_has_no_record.
    let script = vec![("build", vec![build_turn(1)]), ("review", vec![review_turn(1, APPROVE)])];
    let f = Fixture::on(Rc::new(FakeHost::default()), seeded_run(&DEFAULT_WORKFLOW), script);
    let mut wf = f.workflow(given(&DEFAULT_WORKFLOW, GIVEN_SPEC));
    assert_eq!(wf.run(), verdict(APPROVE));
    let files: Vec<String> = wf.state().turns.iter().map(|t| t.get("file").map(Json::python_str).unwrap_or_default()).collect();
    assert_eq!(files, ["build-1.md", "review-1.md"]);
}
