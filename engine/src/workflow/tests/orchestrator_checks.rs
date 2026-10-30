//! TestOrchestratorChecks' cases that need the Workflow: the checks a quality round runs itself after the build, with
//! the run's spec, driven through a gated run.
//!
//! test_a_dependency_the_spec_names_passes_with_its_metadata is here too, through a run, to show that the round's spec
//! is the contract step's file. Its other cases judge the build's quality file the same way whether a quality round or a test calls judge_run, and
//! are crate::checks' (engine/src/checks/tests.rs, rust-engine ticket 07): they need nothing more here.

use std::sync::Arc;

use super::*;
use crate::checks::tests::{pyproject_diff, pyproject_with, SPEC_NAMING_LIMITS, TODAY};
use crate::ci::fake::Outcome;
use crate::constants::QUALITY_TIMEOUT;
use crate::pypi::fake::{FakePyPI, ProjectJson};
use crate::test_support::impl_;

/// The pyproject.toml of the run's base and of its first snapshot, without and with requirements, as FakeHost's
/// committed files: Python's pyproject_files.
fn pyproject_files(host: &FakeHost, requirements: &[&str]) {
    let mut h = host.state();
    h.committed.insert(("abc123".into(), "pyproject.toml".into()), pyproject_with(&[]));
    h.committed.insert(("snap1".into(), "pyproject.toml".into()), pyproject_with(requirements));
}

/// PyPI knowing limits.
fn limits() -> Arc<FakePyPI> {
    FakePyPI::with(&[("limits", ProjectJson::default())])
}

/// A gated fixture on host and pypi, its clocks reading TODAY, as Python's wf.wallclock = TODAY sets the time the
/// round judges on; its one build plays build.
fn checked(host: FakeHost, script: Script, pypi: Arc<FakePyPI>, build: Outcome) -> Fixture {
    let f = Fixture::on(Rc::new(host), new_state(), script).gated().with_pypi(pypi);
    f.clock.set(TODAY);
    f.ci.state().outcomes = [build].into();
    f
}

fn one_round() -> RunSettings {
    RunSettings { max_quality_rounds: 1, ..RunSettings::default() }
}

/// The spec's turn, writing spec.
fn spec_of(spec: &str) -> Turn {
    writes(|s| s.spec_path(), spec, "done")
}

#[test]
fn a_build_that_failed_before_the_gate_is_not_checked() {
    let host = FakeHost::default();
    host.state().diff = pyproject_diff(&["requests"]);
    pyproject_files(&host, &["requests"]);
    let script = vec![
        ("spec", vec![spec_of(SPEC_NAMING_LIMITS)]),
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ];
    let f = checked(host, script, limits(), Outcome { console: "boom".into(), ..Outcome::unarchived() });

    assert_eq!(f.workflow(one_round()).run(), verdict(APPROVE));
    let quality = f.host.state().files.get(&f.state.quality_path(1, 1)).cloned().unwrap();
    assert!(quality.starts_with("GATE: BUILD_FAILED\n"), "{quality}");
    assert!(!quality.contains("orchestrator's checks"));
    assert!(f.pypi.state().asked.is_empty());
}

#[test]
fn a_dependency_the_spec_names_passes_with_its_metadata() {
    // Through the Workflow: the round's spec is the contract step's file, which names limits, and not the task.
    let host = FakeHost::default();
    host.state().diff = pyproject_diff(&["limits>=3"]);
    pyproject_files(&host, &["limits>=3"]);
    let script = vec![
        ("spec", vec![spec_of(SPEC_NAMING_LIMITS)]),
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ];
    let f = checked(host, script, limits(), Outcome::gate("OK", &[]));

    assert_eq!(f.workflow(one_round()).run(), verdict(APPROVE));
    let quality = f.quality_file(1, 1);
    assert!(quality.starts_with("GATE: OK\n"), "{quality}");
    assert!(quality.contains(
        "\n1. `pyproject.toml:5` warning new-dependencies added: `limits`, which the spec names: on PyPI since \
         2015-01-03, latest 3.13.0 of 2024-06-22, license MIT.\n"
    ));
    assert_eq!(f.pypi.state().asked, ["limits"]);
}

#[test]
fn slow_pypi_keeps_the_run_alive_and_stops_at_the_rounds_deadline() {
    let pypi = FakePyPI::with(&[
        ("limits", ProjectJson::default()),
        ("rich", ProjectJson::default()),
        ("attrs", ProjectJson::default()),
    ]);
    let host = FakeHost::default();
    host.state().diff = pyproject_diff(&["limits", "rich", "attrs"]);
    pyproject_files(&host, &["limits", "rich", "attrs"]);
    let script = vec![
        ("spec", vec![spec_of("# Throttle\n\nUse limits, rich and attrs, in pyproject.toml.\n")]),
        ("build", vec![build_turn(1), fix_turn(1, 1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ];
    let f = checked(host, script, Arc::clone(&pypi), green());
    // Each request takes half the round's time.
    pypi.state().clock = Some((f.clock.time(), QUALITY_TIMEOUT as f64 / 2.0));
    // What Python's test logs on each heartbeat: one that saves state.json between the asks keeps the run alive.
    let beats = Arc::clone(&pypi);
    f.host.state().before_write = Some(Box::new(move |path, _| {
        if path.ends_with("/state.json") {
            beats.state().events.push("beat".into());
        }
        Ok(())
    }));

    assert_eq!(f.workflow(one_round()).run(), verdict(APPROVE));

    let quality = f.host.state().files.get(&f.state.quality_path(1, 1)).cloned().unwrap();
    assert_eq!(pypi.state().asked, ["limits", "rich"]);
    assert!(
        quality.contains(
            "unchecked: PyPI could not be asked about `attrs`: the quality round had too little time left to ask;"
        ),
        "{quality}"
    );
    assert!(quality.starts_with("GATE: ERROR\n"));
    let events = pypi.state().events.clone();
    let asks: Vec<usize> = (0..events.len()).filter(|&i| events[i] != "beat").collect();
    assert!(events[asks[0]..asks[1]].contains(&"beat".to_string()), "{events:?}");
}

#[test]
fn without_the_quality_gate_none_of_this_runs() {
    let host = Rc::new(FakeHost::default());
    host.state().diff = pyproject_diff(&["requests"]);
    pyproject_files(&host, &["requests"]);
    let f = Fixture::on(host, new_state(), approved()).with_pypi(limits());

    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert!(f.pypi.state().asked.is_empty());
    assert!(f.host.state().file_reads.is_empty());
    assert!(!f.host.state().files.keys().any(|p| p.contains("quality-")));
}

#[test]
fn the_spec_is_the_runs_task_without_a_contract_step() {
    let host = FakeHost::default();
    host.state().diff = pyproject_diff(&["limits"]);
    pyproject_files(&host, &["limits"]);
    let pipeline = impl_();
    let mut state = new_run(&pipeline);
    state.task = "limit requests with limits in pyproject.toml".into();
    // Python's impl_turn(1).
    let impl_turn: Turn = Box::new(|_, state, host| {
        host.write("/proj/limiter.py", "version 1.0", false).expect("the fake host writes");
        host.write(&format!("{}/impl-1.md", state.dir()), "impl 1 q0", false).expect("the fake host writes");
        "done".to_string()
    });
    let script = vec![("impl", vec![impl_turn]), ("review", vec![review_turn(1, APPROVE)])];
    let f = Fixture::on(Rc::new(host), state, script).gated().with_pypi(limits());
    f.clock.set(TODAY);
    let mut wf = f.workflow(RunSettings { pipeline: Some(pipeline), ..RunSettings::default() });

    assert_eq!(wf.run(), verdict(APPROVE));
    let quality = f.host.state().files.get(&f.state.quality_path(1, 1)).cloned().unwrap();
    assert!(quality.starts_with("GATE: OK\n"), "{quality}");
    assert!(quality.contains("new-dependencies added: `limits`, which the spec names"), "{quality}");
}
