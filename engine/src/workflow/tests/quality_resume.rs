//! TestQualityResume: a run resumed in a quality round, or in the gated step's turn around one, picks the round up
//! from the furthest link of its chain in RunState::ci (the quality file, the build, the queue item, the pushed ref),
//! and a resumed Builder or Reviewer hears of the quality rounds before it.

use std::sync::Arc;

use super::*;
use crate::ci::fake::{credentials, Outcome, JENKINS, JOB};
use crate::constants::{CI_POLL_SECONDS, QUALITY_TIMEOUT};
use crate::prompts::QUALITY_UNRESOLVED_NOTE;

/// The number of the first build a FakeJenkins adds.
const FIRST_BUILD: i64 = 41;

/// RunState::ci of round 1's first quality round in flight, its snapshot snap9, with these fields after ref and sha.
fn in_flight(fields: &[(&str, Json)]) -> Map<Json> {
    let mut ci = ci_of(&[("ref", REF_1_1.into()), ("sha", "snap9".into())]);
    for (key, value) in fields {
        ci.insert(*key, value.clone());
    }
    ci
}

fn queue_url() -> (&'static str, Json) {
    ("queue_url", format!("{JENKINS}/queue/item/1/").into())
}

/// quality_state(q) with ci.
fn quality_in(q: u32, ci: Map<Json>) -> RunState {
    let mut state = quality_state(q);
    state.ci = ci;
    state
}

/// resume_gated of state on a default FakeHost, the review approving.
fn resumed(state: RunState) -> Fixture {
    Fixture::resume_gated(Rc::new(FakeHost::default()), state, vec![("review", vec![review_turn(1, APPROVE)])], &[])
}

/// Add a build of git_ref that plays outcome to f's Jenkins; it must be the build numbered number.
fn add_build(f: &Fixture, git_ref: &str, outcome: Outcome, number: i64) {
    assert_eq!(f.ci.state().add_build(git_ref, outcome), number);
}

fn snapshot_pushed(f: &Fixture) -> bool {
    f.host.state().git_calls.iter().any(|c| c.len() > 2 && c[..3] == ["push", "--quiet", "--force"])
}

#[test]
fn resume_with_a_build_only_polls_it() {
    let f = resumed(quality_in(1, in_flight(&[queue_url(), ("build", FIRST_BUILD.into())])));
    add_build(&f, REF_1_1, green(), FIRST_BUILD);

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    assert!(f.host.state().snapshots.is_empty());
    assert!(!snapshot_pushed(&f));
    assert_eq!(collapsed(&f.ci.state().names), strings(&["build", "quality", "build", "quality", "build", "quality"]));
    assert!(f.quality_file(1, 1).contains("`snap9`"));
}

#[test]
fn resume_with_the_quality_file_archived_and_the_build_running_takes_the_file() {
    let polls = (2 * QUALITY_TIMEOUT / CI_POLL_SECONDS) as u32;
    let f = resumed(quality_in(1, in_flight(&[queue_url(), ("build", FIRST_BUILD.into())])));
    add_build(&f, REF_1_1, Outcome { polls, after_gate: polls, ..green() }, FIRST_BUILD);

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));

    assert_eq!(f.ci.state().names, strings(&["build", "quality", "build", "quality"]));
    assert!(f.quality_file(1, 1).contains(
        "Jenkins build #41 judged snapshot `snap9` (round 1, quality round 1 of 3) and was still running when its \
         quality.md was read.\n"
    ));
}

#[test]
fn resume_of_a_state_saved_with_sonarqube() {
    // A SonarQube-era build of the snapshot: it archived no quality.md.
    let state = quality_in(
        1,
        in_flight(&[queue_url(), ("build", FIRST_BUILD.into()), ("ce_task", format!("task-{FIRST_BUILD}").into())]),
    );
    let Ok(Json::Object(mut saved)) = Json::parse(&state.to_text()) else { panic!("a state is an object") };
    saved.insert("quality_project", "py-ai-agents-orchestrator-a1b2c3".into());
    saved.insert("quality_baseline", "analysis-base".into());
    let state = RunState::from_text(&Json::Object(saved).dumps()).expect("an old state reads");
    let builds = seen();
    let f = Fixture::resume_gated(
        Rc::new(FakeHost::default()),
        state,
        vec![("build", vec![recording(fix_turn(1, 1), &builds)]), ("review", vec![review_turn(1, APPROVE)])],
        &[],
    );
    add_build(&f, REF_1_1, Outcome { result: "SUCCESS".into(), ..Outcome::unarchived() }, FIRST_BUILD);

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    let quality = f.quality_file(1, 1);
    assert!(quality.starts_with("GATE: BUILD_FAILED\n"), "{quality}");
    assert!(quality.contains(
        "Jenkins build #41 of snapshot `snap9` (round 1, quality round 1 of 3) ended SUCCESS before the quality gate \
         ran"
    ));
    assert_eq!(builds.borrow().len(), 1);
    assert!(f.quality_file(1, 2).starts_with("GATE: OK"));
    // Python drops the fields it no longer knows when it saves; this engine keeps them as they were read
    // (RunState::unknown), and reads nothing from them.
    assert_eq!(f.saved().get("quality_project"), Some(&Json::from("py-ai-agents-orchestrator-a1b2c3")));
}

#[test]
fn resume_with_only_the_ref_adopts_its_build() {
    let f = resumed(quality_in(1, in_flight(&[])));
    add_build(&f, REF_1_1, green(), FIRST_BUILD);
    add_build(&f, "orchestrator-ci/a1b2c3-1-q2", green(), FIRST_BUILD + 1);

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    assert!(!f.ci.state().names.contains(&"trigger".to_string()));
    assert!(f.host.state().snapshots.is_empty());
    assert!(f.quality_file(1, 1).contains("Jenkins build #41"));
}

#[test]
fn resume_with_only_the_ref_and_no_build_triggers_one() {
    let f = resumed(quality_in(1, in_flight(&[])));

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    assert_eq!(collapsed(&f.ci.state().names)[..2], strings(&["find", "trigger"]));
    assert!(f.host.state().snapshots.is_empty());
}

#[test]
fn forgotten_queue_item_finds_its_build() {
    let f = resumed(quality_in(1, in_flight(&[queue_url()])));
    add_build(&f, REF_1_1, green(), FIRST_BUILD);
    f.ci.state().forgotten = true;

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    assert_eq!(collapsed(&f.ci.state().names)[..3], strings(&["queue", "find", "build"]));
    assert!(f.quality_file(1, 1).contains("Jenkins build #41"));
}

#[test]
fn forgotten_queue_item_without_a_build_fails_the_run() {
    let f = Fixture::resume_gated(Rc::new(FakeHost::default()), quality_in(1, in_flight(&[queue_url()])), vec![], &[]);
    f.ci.state().forgotten = true;

    let mut wf = f.workflow(f.as_saved_gated());
    let e = failure(wf.run());
    assert!(e.contains("Jenkins forgot queue item ") && e.contains("none of the job's last 20"), "{e}");
    assert_eq!(wf.state().ci, in_flight(&[]));
}

#[test]
fn discarded_build_is_cut_back_so_a_resume_builds_again() {
    let host = Rc::new(FakeHost::default());
    let state = quality_in(1, in_flight(&[queue_url(), ("build", FIRST_BUILD.into())]));
    let f = Fixture::resume_gated(Rc::clone(&host), state, vec![], &[]);

    let mut wf = f.workflow(f.as_saved_gated());
    let e = failure(wf.run());
    assert!(e.contains(&format!("Jenkins build #41 of {REF_1_1} is gone")), "{e}");
    assert_eq!(wf.state().ci, in_flight(&[]));

    // Resumed on the same Jenkins and host.
    let mut again =
        Fixture::resume_gated(Rc::clone(&host), wf.state().clone(), vec![("review", vec![review_turn(1, APPROVE)])], &[]);
    again.ci = Arc::clone(&f.ci);
    let again = again.gated();
    assert_eq!(again.workflow(again.as_saved_gated()).run(), verdict(APPROVE));
    assert_eq!(f.ci.state().triggered(), [REF_1_1]);
}

#[test]
fn a_leftover_ref_of_another_round_is_dropped() {
    let f = resumed(quality_in(2, in_flight(&[queue_url()])));
    f.put(&f.state.quality_build_path(1, 1), "report 1 q1");
    // The run's end cannot list origin's refs, so only the analysis that drops the leftover deletes it.
    f.host.state().list_remote_error = Some("ssh: connection reset".into());

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    assert!(!f.host.state().remote.contains_key(REF_1_1));
    assert_eq!(f.ci.state().triggered(), ["orchestrator-ci/a1b2c3-1-q2"]);
}

#[test]
fn resume_with_the_quality_file_makes_no_request() {
    let f = resumed(quality_state(1));
    f.put(&f.state.quality_path(1, 1), "GATE: OK\n");

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    assert!(f.ci.state().requests.is_empty());
}

#[test]
fn quality_file_without_a_gate_is_refused() {
    let f = Fixture::resume_gated(Rc::new(FakeHost::default()), quality_state(1), vec![], &[]);
    f.put(&f.state.quality_path(1, 1), "all good\n");

    let e = failure(f.workflow(f.as_saved_gated()).run());
    assert!(e.contains("quality-1-1.md does not start with a GATE line; fix it"), "{e}");
    assert!(f.ci.state().requests.is_empty());
}

#[test]
fn build_ends_alike_live_and_resumed() {
    let cases = [
        ("no quality file", Outcome::unarchived(), "GATE: BUILD_FAILED"),
        ("quality file after FAILURE", Outcome { result: "FAILURE".into(), ..green() }, "GATE: OK"),
        ("aborted, no quality file", Outcome { result: "ABORTED".into(), ..Outcome::unarchived() }, "was aborted before"),
    ];
    for (name, outcome, expected) in cases {
        let state = quality_in(1, in_flight(&[queue_url(), ("build", FIRST_BUILD.into())]));
        let f = Fixture::resume_gated(
            Rc::new(FakeHost::default()),
            state,
            vec![("build", vec![fix_turn(1, 1)]), ("review", vec![review_turn(1, APPROVE)])],
            &[],
        );
        add_build(&f, REF_1_1, outcome, FIRST_BUILD);
        let mut wf = f.workflow(f.as_saved_gated());
        if expected.starts_with("GATE") {
            assert_eq!(wf.run(), verdict(APPROVE), "{name}");
            assert!(f.quality_file(1, 1).starts_with(expected), "{name}");
        } else {
            let e = failure(wf.run());
            assert!(e.contains(expected), "{name}: {e}");
            assert_eq!(wf.state().ci, in_flight(&[("rejected", Json::Array(vec![FIRST_BUILD.into()]))]), "{name}");
        }
    }
}

#[test]
fn fresh_builder_answering_a_quality_round_is_briefed() {
    let seen = seen();
    let mut state = saved_run("build", 1, FULL);
    state.quality_job = Some(JOB.to_string());
    state.quality_round = 1;
    state.prompted = Some("build-1-q1.md".to_string());
    let f = Fixture::resume_gated(
        Rc::new(FakeHost::default()),
        state,
        vec![("build", vec![recording(fix_turn(1, 1), &seen)]), ("review", vec![review_turn(1, APPROVE)])],
        &["review"],
    );
    f.put(&f.state.quality_path(1, 1), "GATE: ERROR\n");

    let mut wf = f.workflow(f.as_saved_gated());
    assert_eq!(wf.run(), verdict(APPROVE));
    let first = seen.borrow()[0].clone();
    assert!(first.contains("You are the Builder"));
    assert!(first.contains(&format!(
        "you are a fresh session. An earlier Builder session did the earlier turns; its changes are already in the \
         working tree, and its reports are {}.",
        f.state.build_path(1)
    )));
    assert!(first.ends_with(&(quality_fix(&f.state, 1, 1) + &note())), "{first}");
    assert_eq!(wf.state().quality_round, 2);
}

#[test]
fn fresh_builder_in_a_later_round_hears_of_every_earlier_report() {
    let seen = seen();
    let mut state = saved_run("build", 2, FULL);
    state.quality_job = Some(JOB.to_string());
    let f = Fixture::resume_gated(
        Rc::new(FakeHost::default()),
        state,
        vec![("build", vec![recording(build_turn(2), &seen)]), ("review", vec![review_turn(2, APPROVE)])],
        &["review"],
    );
    let s = &f.state;
    f.put(&s.quality_build_path(1, 1), "r");
    f.put(&s.quality_build_path(1, 2), "r");

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    let reports = [s.build_path(1), s.quality_build_path(1, 1), s.quality_build_path(1, 2)].join(", ");
    assert!(seen.borrow()[0].contains(&format!("its reports are {reports}.")));
}

#[test]
fn the_last_allowed_quality_round_goes_to_review() {
    let reviews = seen();
    let mut state = quality_state(2);
    state.max_quality_rounds = 2;
    let f = Fixture::resume_gated(
        Rc::new(FakeHost::default()),
        state,
        vec![("review", vec![recording(review_turn(1, APPROVE), &reviews)])],
        &[],
    );
    f.ci.state().outcomes.push_back(red());
    f.put(&f.state.quality_build_path(1, 1), "report 1 q1");

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    assert_eq!(prompts_to(&f, "build-a1b2c3"), 0);
    assert!(reviews.borrow()[0].contains(&quality_note(QUALITY_UNRESOLVED_NOTE, &f.state.quality_path(1, 2))));
}

#[test]
fn resume_checks_the_credentials_before_the_builder() {
    let mut state = saved_run("build", 1, &["spec", "build"]);
    state.quality_job = Some(JOB.to_string());
    let mut env = credentials();
    env.remove("JENKINS_TOKEN");
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, vec![("build", vec![build_turn(1)])], &[], &[])
        .gated_with(&env);

    let e = failure(f.workflow(f.as_saved()).run());
    assert!(e.contains("needs JENKINS_TOKEN"), "{e}");
    assert_eq!(prompts_to(&f, "build-a1b2c3"), 0);
}

#[test]
fn a_change_the_gate_was_turned_off_before_judging_is_said_to_be_unjudged() {
    // Not one of Python's cases here: resume --no-quality-gate turned the gate off after round 1's first quality round
    // and the Builder's answer to it, so the Reviewer hears that nothing judged the final change (TestQualityGateOffOnResume,
    // with turn_quality_gate_off, is gate_switch.rs's).
    let mut state = saved_run("review", 1, FULL);
    (state.quality_gate_disabled, state.quality_round) = (true, 2);
    let reviews = seen();
    let f = Fixture::resume(
        Rc::new(FakeHost::default()),
        state,
        vec![("review", vec![recording(review_turn(1, APPROVE), &reviews)])],
        &[],
        &[],
    );
    f.put(&f.state.build_path(1), "report 1");
    f.put(&f.state.quality_path(1, 1), "GATE: ERROR\n");
    f.put(&f.state.quality_build_path(1, 1), "report 1 q1");

    assert_eq!(f.workflow(f.as_saved()).run(), verdict(APPROVE));
    let off = filled(crate::prompts::QUALITY_OFF_NOTE, &[("quality_paths", &f.state.quality_path(1, 1))]);
    assert!(reviews.borrow()[0].ends_with(&format!("\n\n{off}")), "{}", reviews.borrow()[0]);
}
