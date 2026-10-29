//! TestLockedFiles: the Test Writer locks the tests it writes, a later editing turn that changes them has them
//! restored, and the Reviewer is told.
//!
//! Not here: test_only_an_editing_step_may_lock and test_locks_round_trip_through_a_workflow_file, which are
//! crate::pipeline's and crate::workflow_file's (ticket 04).

use super::*;
use crate::pipeline::Pipeline;
use crate::test_support::{four, locking, object as record};
use crate::workflow_file::workflow_definition;

const D: &str = "/proj/.orchestrator/runs/20260929-120000-a1b2c3";
const TEST_FILE: &str = "/proj/test_limiter.py";
/// The ref that holds the files the Test Writer locked.
const LOCK_REF: &str = "refs/orchestrator/a1b2c3/locked/tests";

fn with_pipeline(p: &Pipeline, settings: RunSettings) -> RunSettings {
    RunSettings { pipeline: Some(p.clone()), ..settings }
}

/// The Test Writer's turn, which writes the test it locks.
fn locked_tests_turn() -> Turn {
    let mut tests = write_tests();
    Box::new(move |prompt, state, host| {
        host.write(TEST_FILE, "assert limit() == 3", false).expect("the fake host writes");
        tests(prompt, state, host)
    })
}

/// The Builder's turn in round n, which also rewrites the locked test as test, or deletes it for None.
fn build_touching_tests(n: u32, test: Option<&str>) -> Turn {
    let (test, mut build) = (test.map(String::from), build_turn(n));
    Box::new(move |prompt, state, host| {
        match &test {
            None => host.delete(TEST_FILE),
            Some(text) => host.write(TEST_FILE, text, false).expect("the fake host writes"),
        }
        build(prompt, state, host)
    })
}

/// A new run of LOCKING whose Builder takes builds and whose Reviewer reviews, or else approves in round 1: the
/// fixture, and the Reviewer's prompts.
fn run_locking(builds: Vec<Turn>, reviews: Option<Vec<Turn>>) -> (Fixture, Rc<std::cell::RefCell<Vec<String>>>) {
    let seen = seen();
    let reviews = reviews.unwrap_or_else(|| vec![review_turn(1, APPROVE)]);
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&locking()), vec![
        ("spec", vec![spec_turn()]),
        ("tests", vec![locked_tests_turn()]),
        ("build", builds),
        ("review", reviews.into_iter().map(|r| recording(r, &seen)).collect()),
    ]);
    (f, seen)
}

/// LOCKED_NOTE as the Reviewer hears it, after a blank line.
fn locked_note(steps: &str, paths: &str, diff_paths: &str) -> String {
    let value = |name: &str| match name {
        "steps" => Some(steps.to_string()),
        "paths" => Some(paths.to_string()),
        "diff_paths" => Some(diff_paths.to_string()),
        _ => None,
    };
    let note = crate::py::format::format(crate::prompts::LOCKED_NOTE, &value).expect("the built-in note fills in");
    format!("\n\n{note}")
}

/// RunState::locked as the Test Writer leaves it.
fn tests_locked() -> Map<Vec<String>> {
    [("tests", strings(&["test_limiter.py"]))].into_iter().collect()
}

#[test]
fn an_edited_locked_test_is_restored_and_the_reviewer_told() {
    let (f, seen) = run_locking(vec![build_touching_tests(1, Some("assert True"))], None);
    let mut wf = f.workflow(with_pipeline(&locking(), RunSettings::default()));
    assert_eq!(wf.run(), verdict(APPROVE));

    let h = f.host.state();
    assert_eq!(h.restores, [(LOCK_REF.to_string(), strings(&["test_limiter.py"]))]);
    assert_eq!(h.files.get(TEST_FILE).unwrap(), "assert limit() == 3");
    // A file nobody locked keeps the Builder's change.
    assert_eq!(h.files.get("/proj/limiter.py").unwrap(), "version 1");
    assert_eq!(
        h.files.get(&format!("{D}/locked-build-1.diff")).unwrap(),
        "--- test_limiter.py: assert limit() == 3\n+++ test_limiter.py: assert True\n"
    );
    assert_eq!(wf.state().locked, tests_locked());
    assert_eq!(
        wf.state().restored,
        [record(r#"{"round": 1, "file": "locked-build-1.diff", "paths": {"tests": ["test_limiter.py"]}}"#)]
    );
    let note = locked_note("tests", "`test_limiter.py`", &format!("{D}/locked-build-1.diff"));
    assert!(seen.borrow()[0].contains(&note), "{}", seen.borrow()[0]);
    // done: the run's local refs are gone.
    assert!(h.refs.is_empty());
}

#[test]
fn a_deleted_locked_test_comes_back() {
    let (f, _) = run_locking(vec![build_touching_tests(1, None)], None);
    assert_eq!(f.workflow(with_pipeline(&locking(), RunSettings::default())).run(), verdict(APPROVE));
    let h = f.host.state();
    assert_eq!(h.files.get(TEST_FILE).unwrap(), "assert limit() == 3");
    assert!(h.files.get(&format!("{D}/locked-build-1.diff")).unwrap().contains("+++ test_limiter.py: None"));
}

#[test]
fn untouched_locked_files_need_no_restore() {
    let (f, seen) = run_locking(
        vec![build_turn(1), build_touching_tests(2, Some("assert True"))],
        Some(vec![review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)]),
    );
    assert_eq!(f.workflow(with_pipeline(&locking(), RunSettings::default())).run(), verdict(APPROVE));
    let h = f.host.state();
    assert_eq!(h.restores, [(LOCK_REF.to_string(), strings(&["test_limiter.py"]))]);
    assert!(!h.files.contains_key(&format!("{D}/locked-build-1.diff")));
    assert!(!seen.borrow()[0].contains("locked"));
    assert!(seen.borrow()[1].contains(&format!("{D}/locked-build-2.diff")));
}

#[test]
fn a_workflow_without_locks_restores_nothing() {
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&four()), vec![
        ("spec", vec![spec_turn()]),
        ("tests", vec![locked_tests_turn()]),
        ("build", vec![build_touching_tests(1, Some("assert True"))]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    let mut wf = f.workflow(with_pipeline(&four(), RunSettings::default()));
    assert_eq!(wf.run(), verdict(APPROVE));
    let h = f.host.state();
    assert_eq!((h.files.get(TEST_FILE).unwrap().as_str(), h.restores.len(), h.refs.len()), ("assert True", 0, 0));
    assert_eq!((wf.state().locked.len(), wf.state().restored.len()), (0, 0));
}

#[test]
fn a_resume_between_the_turn_and_the_restore_still_restores() {
    let (f, _) = run_locking(vec![], None);
    f.play("build", vec![then_interrupted(&f.interrupt, build_touching_tests(1, Some("assert True")))]);
    assert_eq!(f.workflow(with_pipeline(&locking(), RunSettings::default())).run(), Err(Stopped::Interrupted));
    assert_eq!(f.host.state().files.get(TEST_FILE).unwrap(), "assert True");
    let saved = f.saved();
    assert_eq!(
        (saved.get("phase").unwrap().dumps(), saved.get("locked").unwrap().dumps(), saved.get("restored").unwrap().dumps()),
        (r#""build""#.to_string(), r#"{"tests": ["test_limiter.py"]}"#.to_string(), "[]".to_string())
    );

    let state = RunState::from_saved(&saved).unwrap();
    let f2 = Fixture::resume(Rc::clone(&f.host), state, vec![("review", vec![review_turn(1, APPROVE)])], &[], &[]);
    let mut wf = f2.workflow(with_pipeline(&locking(), f2.as_saved()));
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(f2.host.state().files.get(TEST_FILE).unwrap(), "assert limit() == 3");
    let files: Vec<&str> = wf.state().restored.iter().map(|r| r.get("file").and_then(Json::as_str).unwrap()).collect();
    assert_eq!(files, ["locked-build-1.diff"]);
}

#[test]
fn a_record_saved_before_the_restore_is_restored_on_resume() {
    let mut state = saved_run("build", 1, &["spec", "build"]);
    state.workflow = "locking".to_string();
    state.workflow_definition = Some(workflow_definition(&locking()));
    state.locked = tests_locked();
    state.restored = vec![record(r#"{"round": 1, "file": "locked-build-1.diff", "paths": {"tests": ["test_limiter.py"]}}"#)];
    let host = Rc::new(FakeHost::default());
    {
        let mut h = host.state();
        h.refs.insert(LOCK_REF, "snap0".to_string());
        h.trees.insert("snap0".to_string(), [(TEST_FILE.to_string(), Some("assert limit() == 3".to_string()))].into());
        h.files.insert(TEST_FILE, "assert True".to_string());
    }
    let f = Fixture::resume(host, state, vec![("review", vec![review_turn(1, APPROVE)])], &[], &[]);
    f.put(&format!("{D}/build-1.md"), "report 1");
    f.put(&format!("{D}/tests.md"), "tests");
    let mut wf = f.workflow(with_pipeline(&locking(), f.as_saved()));
    assert_eq!(wf.run(), verdict(APPROVE));
    let h = f.host.state();
    assert_eq!(h.files.get(TEST_FILE).unwrap(), "assert limit() == 3");
    assert_eq!(wf.state().restored.len(), 1);
    assert!(!h.writes.contains(&format!("{D}/locked-build-1.diff")));
}

#[test]
fn a_malformed_restored_record_stops_the_run_naming_it() {
    // Python's engine fails on a KeyError when the Reviewer's prompt reads the record; no engine writes one so.
    let mut state = saved_run("build", 1, &["spec", "build"]);
    state.locked = tests_locked();
    state.restored = vec![record(r#"{"round": 1, "file": "locked-build-1.diff"}"#)];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, vec![], &[], &[]);
    f.put(&format!("{D}/build-1.md"), "report 1");
    let e = failure(f.workflow(with_pipeline(&locking(), f.as_saved())).run());
    assert_eq!(e, "a restored record in state.json has no paths");
}

#[test]
fn the_gate_judges_the_restored_tests() {
    let base = locking();
    let steps = base.steps().iter().cloned().map(|mut st| {
        st.quality_gated |= st.id == "build";
        st
    });
    let pipeline = Pipeline::new("locking", base.roles().clone(), steps.collect()).unwrap();
    let mut fix_1 = fix_turn(1, 1);
    let fix: Turn = Box::new(move |prompt, state, host| {
        host.write(TEST_FILE, "assert 1", false).expect("the fake host writes");
        fix_1(prompt, state, host)
    });
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&pipeline), vec![
        ("spec", vec![spec_turn()]),
        ("tests", vec![locked_tests_turn()]),
        ("build", vec![build_touching_tests(1, Some("assert True")), fix]),
        ("review", vec![review_turn(1, APPROVE)]),
    ])
    .gated();
    f.ci.state().outcomes = vec![red(), green()].into();

    let mut wf = f.workflow(with_pipeline(&pipeline, RunSettings::default()));
    assert_eq!(wf.run(), verdict(APPROVE));
    let h = f.host.state();
    let judged: Vec<Option<String>> = h
        .snapshots
        .iter()
        .enumerate()
        .filter(|(_, (_, message))| message.contains("quality round"))
        .map(|(i, _)| h.trees[&format!("snap{}", i + 1)].get(TEST_FILE).cloned().flatten())
        .collect();
    let restored = Some("assert limit() == 3".to_string());
    assert_eq!(judged, [restored.clone(), restored]);
    let files: Vec<Option<&str>> = wf.state().restored.iter().map(|r| r.get("file").and_then(Json::as_str)).collect();
    assert_eq!(files, [Some("locked-build-1.diff"), Some("locked-build-1-q1.diff")]);
}
