//! TestOtherWorkflows, TestWorkflowWithoutAVerdict, TestSavedBeforeWorkflows, TestSpecBuildReviewExample and the
//! Workflow-driven cases of TestWorkflowDefinitions and TestWorkflowFiles: workflows other than the default, a run
//! saved before workflows existed, and a workflow file's run.
//!
//! Not here: the cases that run the quality gate, with the quality round (rust-engine ticket 08, part 5):
//! TestOtherWorkflows' test_gated_step_with_another_name, test_fresh_gated_step_answering_a_quality_round and
//! test_resumed_gated_step_checks_the_credentials_first, TestWorkflowWithoutAVerdict's
//! test_a_gate_that_still_fails_is_not_finished, test_a_missing_last_quality_file_fails_the_gate and
//! test_a_gate_that_passes_is_finished, TestSavedBeforeWorkflows' test_quality_resumes_under_the_default and
//! TestWorkflowDefinitions' test_the_gate_needs_a_gated_step. The CLI's (ticket 09): role_models' half of
//! TestOtherWorkflows.test_model_reaches_a_role_without_its_own_flag, TestWorkflowWithoutAVerdict's
//! test_a_failed_gate_exits_as_changes_requested and test_finished_exits_zero, and TestWorkflowFiles'
//! test_list_reads_the_gated_step_from_the_saved_definition; resumable_state's (part 7): TestWorkflowFiles'
//! test_a_saved_definition_that_cannot_be_filled_in_stops_the_resume and test_a_corrupt_saved_definition_is_an_error.
//! The other cases of TestWorkflowDefinitions and TestWorkflowFiles, and TestSavedBeforeWorkflows'
//! test_root_pane_comes_from_the_workflows_first_role, are crate::pipeline's, crate::workflow_file's and
//! crate::state's (ticket 04).

use super::*;
use crate::constants::FINISHED;
use crate::pipeline::{Pipeline, Step, DEFAULT_WORKFLOW};
use crate::test_support::{four, quick, roles, solo, TempDir, EXAMPLES};
use crate::workflow_file::{load_workflow_file, workflow_definition};

const D: &str = "/proj/.orchestrator/runs/20260929-120000-a1b2c3";
/// What a step's {change} is in a run in /proj based on abc123.
const CHANGE: &str = "`git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists";

fn with_pipeline(p: &Pipeline) -> RunSettings {
    RunSettings { pipeline: Some(p.clone()), ..RunSettings::default() }
}

/// The arguments of the commit the run made.
fn commit_args(f: &Fixture) -> Vec<String> {
    f.host.state().git_calls.iter().find(|c| c[0] == "commit").cloned().expect("a commit")
}

#[test]
fn workflow_without_a_spec_step() {
    let quick = quick();
    let (seen, at_first_build) = (seen(), Rc::new(std::cell::RefCell::new(vec![])));
    let (record, mut build) = (Rc::clone(&at_first_build), build_turn(1));
    let building: Turn = Box::new(move |prompt, state, host| {
        record.borrow_mut().push((state.base.clone(), host.state().branch.clone(), state.round));
        build(prompt, state, host)
    });
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&quick), vec![
        ("build", vec![recording(building, &seen)]),
        ("review", vec![recording(review_turn(1, APPROVE), &seen)]),
    ]);
    assert_eq!(f.workflow(with_pipeline(&quick)).run(), verdict(APPROVE));
    let branch = "orchestrator/add-a-rate-limiter-a1b2c3";
    assert_eq!(*at_first_build.borrow(), [(Some("abc123".to_string()), branch.to_string(), 1)]);
    assert_eq!(
        *seen.borrow(),
        [
            format!("Build add a rate limiter in /proj; report to {D}/build-1.md.{}", note()),
            format!("Review {CHANGE} against add a rate limiter; write {D}/review-1.md."),
        ]
    );
    assert_eq!(names(&f, "start"), ["build-a1b2c3", "review-a1b2c3"]);
    assert_eq!(calls_of(&f, "split"), [split("w1:p1", Direction::Right)]);
    assert!(calls_of(&f, "focus").is_empty());
    let pr = f.host.state().prs[0].clone();
    assert_eq!((pr.title.as_str(), pr.head.as_str()), ("add a rate limiter", branch));
    let commit = commit_args(&f);
    assert_eq!(commit[commit.iter().position(|a| a == "-m").unwrap() + 1], "add a rate limiter");
    let saved = f.saved();
    assert_eq!((text(&saved, "workflow"), text(&saved, "phase")), (Some("quick".into()), Some("done".into())));
}

#[test]
fn verdict_step_of_another_name_gets_one_retry() {
    let mut build = Step::new("build", "build", "build-{n}.md", "Build {task}; report to {build_path}.");
    (build.again, build.edits) = (Some("Fix {prev_check_path}; report to {build_path}.".into()), true);
    let mut check = Step::new("check", "check", "check-{n}.md", "Check {change}; write {path}.");
    check.loop_to = Some("build".into());
    let checked = Pipeline::new("checked", roles(&[("build", "Builder"), ("check", "Checker")]), vec![build, check]).unwrap();
    let seen = seen();
    let check_1 = |s: &RunState| format!("{}/check-1.md", s.dir());
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&checked), vec![
        ("build", vec![build_turn(1)]),
        (
            "check",
            vec![writes(check_1, "fine by me", "done"), recording(writes(check_1, &format!("VERDICT: {APPROVE}\n"), "done"), &seen)],
        ),
    ]);
    let mut wf = f.workflow(with_pipeline(&checked));
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(*seen.borrow(), [retry_prompt(&format!("{D}/check-1.md"))]);
    assert_eq!(f.host.state().files.get(&format!("{D}/check-1.rejected.md")).map(String::as_str), Some("fine by me"));
    assert_eq!(wf.state().retried, ["check-1.md"]);
}

#[test]
fn four_roles_loop_back_to_the_builder_only() {
    let four = four();
    let seen = seen();
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&four), vec![
        ("spec", vec![spec_turn()]),
        ("tests", vec![write_tests()]),
        ("build", vec![recording(build_turn(1), &seen), recording(build_turn(2), &seen)]),
        ("review", vec![review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)]),
    ]);
    let mut wf = f.workflow(with_pipeline(&four));
    assert_eq!(wf.run(), verdict(APPROVE));
    let prompted = ["spec", "tests", "build", "review", "build", "review"].map(|r| format!("{r}-a1b2c3"));
    assert_eq!(names(&f, "prompt"), prompted);
    assert_eq!(
        calls_of(&f, "split"),
        [split("w1:p1", Direction::Right), split("w1:p2", Direction::Down), split("w1:p3", Direction::Down)]
    );
    let labels: Vec<String> = calls_of(&f, "rename")
        .into_iter()
        .map(|c| match c {
            Call::Rename { label, .. } => label,
            _ => unreachable!(),
        })
        .collect();
    assert_eq!(labels, ["Spec Collector", "Test Writer", "Builder", "Reviewer"]);
    assert_eq!(
        *seen.borrow(),
        [
            format!("Build {D}/spec.md against {D}/tests.md; report to {D}/build-1.md.{}", note()),
            format!("Fix {D}/review-1.md; report to {D}/build-2.md.{}", note()),
        ]
    );
    assert_eq!(f.told.titles()[0], "Spec Collector is waiting for you");
    let pr = f.host.state().prs[0].clone();
    assert_eq!(pr.title, "Add a token-bucket rate limiter");
    assert!(pr.body.contains("<summary>Builder report (round 2)</summary>\n\nreport 2"));
    assert_eq!(wf.state().round, 2);
}

#[test]
fn interrupted_mid_loop_resumes_at_the_right_step() {
    let four = four();
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&four), vec![
        ("spec", vec![spec_turn()]),
        ("tests", vec![write_tests()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, CHANGES_REQUESTED)]),
    ]);
    f.play("build", vec![then_interrupted(&f.interrupt, build_turn(2))]);
    assert_eq!(f.workflow(with_pipeline(&four)).run(), Err(Stopped::Interrupted));
    let saved = f.saved();
    let fields = ["workflow", "phase", "error"].map(|key| text(&saved, key).unwrap_or_default());
    assert_eq!((fields, saved.get("round")), (["four", "build", "interrupted"].map(String::from), Some(&Json::from(2))));
    let seen = seen();
    let state = RunState::from_saved(&saved).unwrap();
    let f2 = Fixture::resume(Rc::clone(&f.host), state, vec![("review", vec![recording(review_turn(2, APPROVE), &seen)])], &[], &[]);
    f2.herdr.state().panes = 4;
    let mut wf = f2.workflow(RunSettings { pipeline: Some(four), ..f2.as_saved() });
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(names(&f2, "prompt"), ["review-a1b2c3"]);
    assert_eq!(names(&f2, "start"), ["review-a1b2c3"]);
    assert_eq!(*seen.borrow(), [format!("Recheck; write {D}/review-2.md.")]);
    let s = wf.state();
    assert_eq!((s.round, s.phase.as_str(), s.verdict.as_deref()), (2, "done", Some(APPROVE)));
}

#[test]
fn model_reaches_a_role_without_its_own_flag() {
    // Its Workflow half: the models role_models gives for --model X --build-model Y (ticket 09).
    let four = four();
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&four), vec![
        ("spec", vec![spec_turn()]),
        ("tests", vec![write_tests()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    let mut settings = with_pipeline(&four);
    settings.agents.models = [("spec", "X"), ("tests", "X"), ("build", "Y"), ("review", "X")]
        .into_iter()
        .map(|(role, model)| (role, model.to_string()))
        .collect();
    f.workflow(settings).run().unwrap();
    let start = Call::Start { name: "tests-a1b2c3".into(), pane: "w1:p2".into(), args: strings(&["--model", "X"]) };
    assert!(called(&f, &start));
}

#[test]
fn phase_outside_the_workflow_is_refused() {
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&four()), vec![]);
    let error = failure(f.workflow(with_pipeline(&quick())).run());
    assert!(error.contains("phase spec, which workflow quick has no step for"), "{error}");
}

// -- TestWorkflowWithoutAVerdict --------------------------------------------------------------------------------

#[test]
fn a_run_finishes_and_opens_a_ready_pull_request() {
    let solo = solo();
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&solo), vec![("build", vec![solo_turn()])]);
    assert_eq!(f.workflow(with_pipeline(&solo)).run(), verdict(FINISHED));
    let pr = f.host.state().prs[0].clone();
    assert!(!pr.draft);
    // The head line, then the commit's trailers, then the spec.
    let parts: Vec<&str> = pr.body.splitn(3, "\n\n").collect();
    assert_eq!(
        parts[0],
        "Opened by Octoflow AI run `20260929-120000-a1b2c3`. **FINISHED**: its workflow has no review step, so no \
         agent reviewed this change."
    );
    assert_eq!(
        parts[1],
        "| Commit trailer | Value |\n| --- | --- |\n| Orchestrator-Run | `20260929-120000-a1b2c3` |\n| \
         Orchestrator-Workflow | `solo` |\n| Orchestrator-Role | `build=claude/default` |"
    );
    assert!(parts[2].starts_with("<details open>\n<summary>Spec</summary>"));
    assert!(pr.body.contains("<summary>Builder report (round 1)</summary>\n\nsolo report"));
    assert!(!pr.body.contains("<summary>Review"));
    let commit = commit_args(&f);
    assert_eq!(
        commit[5..],
        strings(&[
            "Orchestrator run 20260929-120000-a1b2c3: FINISHED, unreviewed.",
            "--trailer",
            "Orchestrator-Run: 20260929-120000-a1b2c3",
            "--trailer",
            "Orchestrator-Workflow: solo",
            "--trailer",
            "Orchestrator-Role: build=claude/default",
        ])
    );
    assert!(f.told.titles().contains(&"Run finished: FINISHED".to_string()));
    let saved = f.saved();
    assert_eq!((text(&saved, "phase"), text(&saved, "verdict")), (Some("done".into()), Some(FINISHED.into())));
}

#[test]
fn a_resume_at_publish_keeps_the_outcome() {
    let mut state = saved_run("publish", 1, &["build"]);
    state.pull_request = true;
    (state.base_branch, state.branch) = (Some("main".into()), Some("orchestrator/x-a1b2c3".into()));
    (state.verdict, state.workflow) = (Some(FINISHED.into()), "solo".into());
    let host = FakeHost::new(Some("def456"), "orchestrator/x-a1b2c3");
    let f = Fixture::resume(Rc::new(host), state, vec![], &[], &[]);
    assert_eq!(f.workflow(RunSettings { pipeline: Some(solo()), ..f.as_saved() }).run(), verdict(FINISHED));
    assert!(!f.host.state().prs[0].draft);
}

// -- TestSavedBeforeWorkflows: state.json as commit d6bb100 wrote it, without a workflow field. --------------------

/// state as a run saved before workflows existed would read: without its workflow field.
fn old(state: &RunState) -> RunState {
    let Json::Object(mut saved) = state.to_json() else { unreachable!() };
    saved.remove("workflow");
    RunState::from_saved(&saved).unwrap()
}

#[test]
fn each_phase_resumes_under_the_default() {
    let with = |mut state: RunState, change: fn(&mut RunState)| {
        change(&mut state);
        state
    };
    // By phase, the saved run, its script, and a file the run wrote before.
    type Case = (&'static str, RunState, Script, Option<(String, &'static str)>);
    let build_and_review = vec![("build", vec![build_turn(1)]), ("review", vec![review_turn(1, APPROVE)])];
    let report = Some((format!("{D}/build-2.md"), "report 2"));
    let cases: Vec<Case> = vec![
        ("spec", with(saved_run("spec", 0, &["spec"]), |s| s.pull_request = true), approved(), None),
        ("build", saved_run("build", 1, FULL), build_and_review, None),
        ("review", saved_run("review", 2, FULL), vec![("review", vec![review_turn(2, APPROVE)])], report),
        (
            "publish",
            with(saved_run("publish", 1, FULL), |s| {
                s.pull_request = true;
                (s.base_branch, s.branch) = (Some("main".into()), Some("orchestrator/x-a1b2c3".into()));
                s.verdict = Some(APPROVE.into());
            }),
            vec![],
            None,
        ),
        ("done", with(saved_run("done", 1, FULL), |s| s.verdict = Some(APPROVE.into())), vec![], None),
    ];
    for (phase, state, script, file) in cases {
        let head = if phase == "publish" { "def456" } else { "abc123" };
        let host = FakeHost::new(Some(head), state.branch.as_deref().unwrap_or("main"));
        let f = Fixture::resume(Rc::new(host), old(&state), script, &[], &[]);
        if let Some((path, text)) = file {
            f.put(&path, text);
        }
        let mut wf = f.workflow(f.as_saved());
        assert_eq!(wf.state().workflow, "default", "{phase}");
        assert_eq!(wf.run(), verdict(APPROVE), "{phase}");
        assert_eq!(wf.state().phase, "done", "{phase}");
    }
}

// -- The Workflow-driven cases of TestWorkflowDefinitions and TestWorkflowFiles, and TestSpecBuildReviewExample ------

#[test]
fn a_pull_request_needs_an_editing_step() {
    let talk = Pipeline::new("talk", roles(&[("spec", "Spec Collector")]), vec![Step::new("spec", "spec", "spec.md", "{task}")])
        .unwrap();
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&talk), vec![]);
    let refused = f.try_workflow(with_pipeline(&talk)).err().map(|e| e.0);
    let expected = "workflow talk has no editing step, so it cannot end in a pull request; pass --no-pr";
    assert_eq!(refused.as_deref(), Some(expected));
    let notes = writes(|s| format!("{}/spec.md", s.dir()), "notes", "done");
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&talk), vec![("spec", vec![notes])]);
    let mut wf = f.workflow(RunSettings { pull_request: false, ..with_pipeline(&talk) });
    assert_eq!(wf.run(), verdict(FINISHED));
    assert_eq!(wf.state().phase, "done");
}

#[test]
fn a_run_keeps_its_workflow_when_the_file_is_gone() {
    let workflows = TempDir::new();
    let path = workflows.join("quick.toml");
    std::fs::copy(format!("{EXAMPLES}/quick.toml"), &path).unwrap();
    let quick = load_workflow_file(&path, &Default::default()).unwrap();
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&quick), vec![
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, CHANGES_REQUESTED)]),
    ]);
    f.play("build", vec![then_interrupted(&f.interrupt, build_turn(2))]);
    assert_eq!(f.workflow(with_pipeline(&quick)).run(), Err(Stopped::Interrupted));
    let saved = f.saved();
    assert_eq!(saved.get("workflow_definition"), Some(&workflow_definition(&quick)));
    std::fs::remove_file(&path).unwrap();
    let seen = seen();
    let state = RunState::from_saved(&saved).unwrap();
    assert_eq!(Some(&state.root_pane), state.agents.get("build").and_then(|a| a.get("pane")));
    let f2 = Fixture::resume(Rc::clone(&f.host), state, vec![("review", vec![recording(review_turn(2, APPROVE), &seen)])], &[], &[]);
    let mut wf = f2.workflow(f2.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(*wf.pipeline, quick);
    let answered = format!("The Builder has answered your review; the new report is in {D}/build-2.md.");
    assert!(seen.borrow()[0].starts_with(&answered), "{}", seen.borrow()[0]);
}

#[test]
fn a_built_in_workflow_is_saved_by_name_only() {
    let f = Fixture::new(approved());
    let mut wf = f.workflow(no_pr());
    wf.run().unwrap();
    assert_eq!((wf.state().workflow.as_str(), wf.state().workflow_definition.as_ref()), ("default", None));
}

#[test]
fn spec_build_review_is_the_default_workflow() {
    // TestSpecBuildReviewExample.test_it_is_the_default_workflow. Fails when a built-in prompt or step changes;
    // regenerate the file with `workflows default`.
    let example = load_workflow_file(&format!("{EXAMPLES}/spec-build-review.toml"), &Default::default()).unwrap();
    assert_eq!(workflow_definition(&example), workflow_definition(&DEFAULT_WORKFLOW));
}
