//! TestSecretScanWorkflow: the scan before each of a run's pushes, on FakeHost's scripted findings: a quality
//! snapshot holding a secret goes back to the Builder unpushed, a secret in the run's base fails the run, and one on
//! the run's branch stops publish until it is rewritten; and gitleaks needed only by a run that pushes.

use std::cell::RefCell;

use super::*;
use crate::host::fake::SCANNED;
use crate::host::tests::FAKE_TOKEN;
use crate::prompts::QUALITY_STUCK_NOTE;
use crate::quality::{finding_fingerprint, Leak, QUALITY_LEAK, QUALITY_OK};
use crate::quality::parse_gate;
use super::pull_request::MERGE;

/// A secret in a commit of the human's: Python's LEAK.
fn leak() -> Leak {
    Leak {
        rule: "github-pat".into(),
        file: "limiter.py".into(),
        line: 3,
        commit: "c0ffee".repeat(6) + "c0ff",
        fingerprint: "c0ffee:limiter.py:github-pat:3".into(),
    }
}

/// leak() in the commit scanned: Python's SNAPSHOT_LEAK.
fn snapshot_leak() -> Leak {
    Leak { commit: SCANNED.into(), ..leak() }
}

fn host_with(leaks: Vec<Leak>) -> Rc<FakeHost> {
    let host = FakeHost::default();
    host.state().leaks = leaks;
    Rc::new(host)
}

fn gated_on(host: Rc<FakeHost>, script: Script) -> Fixture {
    Fixture::on(host, new_state(), script).gated()
}

/// The paths of the files that hold the fake secret.
fn holding_the_secret(f: &Fixture) -> Vec<String> {
    f.host.state().files.iter().filter(|(_, text)| text.contains(FAKE_TOKEN)).map(|(p, _)| p.to_string()).collect()
}

fn log_holds_the_secret(f: &Fixture) -> bool {
    f.told.lines().iter().any(|l| l.contains(FAKE_TOKEN))
}

/// The Builder's answer to quality round q of round 1 that also takes all but the first remaining secrets out.
fn fix_keeping(remaining: usize) -> Turn {
    Box::new(move |prompt, state, host| {
        host.state().leaks.truncate(remaining);
        let mut fix = fix_turn(1, state.quality_round);
        fix(prompt, state, host)
    })
}

#[test]
fn a_secret_in_a_snapshot_goes_back_to_the_builder() {
    let builds = seen();
    let f = gated_on(
        host_with(vec![snapshot_leak()]),
        vec![
            ("spec", vec![spec_turn()]),
            // The Builder takes the secret out of the working tree.
            ("build", vec![build_turn(1), recording(fix_keeping(0), &builds)]),
            ("review", vec![review_turn(1, APPROVE)]),
        ],
    );
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    let state = &f.state;
    let quality = f.quality_file(1, 1);
    assert_eq!(parse_gate(&quality), Some(QUALITY_LEAK));
    assert!(quality.contains("so it was not pushed and Jenkins did not judge the change"));
    let in_snapshot = Leak { commit: "snap1".into(), ..leak() };
    assert!(quality.contains(&format!("## Secrets found\n\n1. {in_snapshot}\n")), "{quality}");
    assert_eq!(wf.state().quality_rounds[0].get("gate"), Some(&Json::from(QUALITY_LEAK)));
    assert_eq!(*builds.borrow(), [quality_fix(state, 1, 1) + &note()]);
    // Only the clean second snapshot was pushed and built.
    let pushes: Vec<Vec<String>> = f
        .host
        .state()
        .git_calls
        .iter()
        .filter(|c| c.len() > 2 && c[..3] == ["push", "--quiet", "--force"])
        .cloned()
        .collect();
    assert_eq!(pushes, [strings(&["push", "--quiet", "--force", "origin", "snap2:refs/heads/orchestrator-ci/a1b2c3-1-q2"])]);
    assert_eq!(f.ci.state().triggered(), ["orchestrator-ci/a1b2c3-1-q2"]);
    assert!(f.quality_file(1, 2).starts_with("GATE: OK"));
    let scanned: Vec<String> = f.host.state().scans.iter().take(2).map(|s| s.0.clone()).collect();
    assert_eq!(scanned, ["snap1", "snap2"]);
    // The report gitleaks wrote is the only place the secret is.
    assert!(!log_holds_the_secret(&f));
    assert_eq!(holding_the_secret(&f), [state.leaks_path("snapshot")]);
    assert!(!f.host.state().prs[0].body.contains(FAKE_TOKEN));
}

#[test]
fn a_resume_finds_the_leak_quality_file() {
    let host = host_with(vec![snapshot_leak()]);
    let f = gated_on(Rc::clone(&host), vec![("spec", vec![spec_turn()]), ("build", vec![build_turn(1)])]);
    f.play("build", vec![then_interrupted(&f.interrupt, idle())]);
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), Err(Stopped::Interrupted));
    let s = wf.state();
    assert_eq!((s.phase.as_str(), s.quality_round, s.ci.clone()), ("build", 1, Map::new()));
    // As if the orchestrator had stopped after writing the quality file, before it moved on.
    let mut state = s.clone();
    state.phase = "quality".into();
    let counts = |host: &FakeHost| {
        let h = host.state();
        (h.scans.len(), h.snapshots.len())
    };
    let (scans, snapshots) = counts(&host);
    let before_fix = Rc::new(RefCell::new(Vec::new()));
    let counted = Rc::clone(&before_fix);
    let mut fix = fix_keeping(0);
    let fix: Turn = Box::new(move |prompt, state, host| {
        counted.borrow_mut().push(counts(host));
        fix(prompt, state, host)
    });
    let g = Fixture::on(Rc::clone(&host), state, vec![("build", vec![fix]), ("review", vec![review_turn(1, APPROVE)])])
        .gated();
    assert_eq!(g.workflow(RunSettings::default()).run(), verdict(APPROVE));
    // The LEAK file was taken as written: no new snapshot or scan before the fix turn.
    assert_eq!(*before_fix.borrow(), [(scans, snapshots)]);
    assert_eq!(parse_gate(&g.quality_file(1, 1)), Some(QUALITY_LEAK));
    assert_eq!(g.ci.state().triggered(), ["orchestrator-ci/a1b2c3-1-q2"]);
}

#[test]
fn a_secret_the_builder_leaves_in_stops_the_loop_early() {
    let reviews = seen();
    let f = gated_on(
        host_with(vec![snapshot_leak()]),
        vec![
            ("spec", vec![spec_turn()]),
            ("build", vec![build_turn(1), fix_turn(1, 1)]),
            ("review", vec![recording(review_turn(1, APPROVE), &reviews)]),
        ],
    );
    let mut wf = f.workflow(RunSettings::default());
    // The secret is still in the branch at publish, which ends the run.
    let e = failure(wf.run());
    assert!(e.contains("git reset --soft"), "{e}");
    let s = wf.state();
    assert_eq!((s.quality_round, prompts_to(&f, "build-a1b2c3")), (2, 2));
    assert!(reviews.borrow()[0].contains(&quality_note(QUALITY_STUCK_NOTE, &s.quality_path(1, 2))));
    let fingerprint = Json::Array(vec![Json::from(finding_fingerprint(Some("limiter.py"), "gitleaks github-pat", "") + "-1")]);
    let found: Vec<Option<&Json>> =
        s.quality_rounds.iter().map(|r| r.get("fingerprints_by_check").and_then(|f| f.get("gitleaks"))).collect();
    assert_eq!(found, [Some(&fingerprint), Some(&fingerprint)]);
    assert!(f.ci.state().triggered().is_empty());
}

#[test]
fn removing_one_of_two_secrets_of_a_kind_keeps_the_loop_going() {
    let f = gated_on(
        host_with(vec![snapshot_leak(), Leak { line: 9, ..snapshot_leak() }]),
        vec![
            ("spec", vec![spec_turn()]),
            ("build", vec![build_turn(1), fix_keeping(1), fix_keeping(0)]),
            ("review", vec![review_turn(1, APPROVE)]),
        ],
    );
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    let gates: Vec<Option<&Json>> = wf.state().quality_rounds.iter().map(|r| r.get("gate")).collect();
    let (leaked, ok) = (Json::from(QUALITY_LEAK), Json::from(QUALITY_OK));
    assert_eq!(gates, [Some(&leaked), Some(&leaked), Some(&ok)]);
    assert_eq!(prompts_to(&f, "build-a1b2c3"), 3);
}

#[test]
fn a_secret_in_the_base_fails_the_run() {
    // In base or below it, not in the snapshot: the human's commit.
    let f = gated_on(host_with(vec![snapshot_leak(), leak()]), approved());
    let mut wf = f.workflow(RunSettings::default());
    let e = failure(wf.run());
    assert!(e.contains("the human's commits, not the Builder's to fix"), "{e}");
    assert!(e.contains("`limiter.py:3` github-pat in c0ffeec0ffee"), "{e}");
    let base: String = wf.state().base.clone().unwrap().chars().take(12).collect();
    assert!(e.contains(&format!("in the run's base {base}")), "{e}");
    assert!(!f.host.state().files.contains_key(&f.state.quality_path(1, 1)));
    assert!(f.ci.state().triggered().is_empty());
    assert!(wf.state().ci.is_empty());
    assert!(!f.host.state().git_calls.iter().any(|c| c[0] == "push"));
}

#[test]
fn a_secret_on_the_branch_stops_publish_until_it_is_rewritten() {
    let host = host_with(vec![leak()]);
    host.state().upstream = Some("u3".into());
    let f = Fixture::on(Rc::clone(&host), new_state(), approved());
    let mut wf = f.workflow(RunSettings::default());
    let e = failure(wf.run());
    let s = wf.state().clone();
    assert!(e.contains(&format!("git reset --soft {}", s.base.as_deref().unwrap())), "{e}");
    assert!(e.contains("`limiter.py:3` github-pat"), "{e}");
    // Scanned before origin's base is merged in, so the rewrite never has to undo a merge.
    let calls = host.state().git_calls.clone();
    assert!(!calls.iter().any(|c| c[0] == "push"));
    assert!(!calls.contains(&strings(MERGE)));
    let scanned: Vec<String> = host.state().scans.iter().map(|s| s.0.clone()).collect();
    assert_eq!(scanned, [format!("refs/heads/{}", s.branch.as_deref().unwrap())]);
    // The report gitleaks wrote is the only place the secret is.
    assert!(!log_holds_the_secret(&f) && !e.contains(FAKE_TOKEN));
    assert_eq!(holding_the_secret(&f), [s.leaks_path("publish")]);
    assert_eq!(s.phase, "publish");
    assert!(host.state().prs.is_empty());
    assert_eq!(text(&f.saved(), "phase").as_deref(), Some("publish"));
    // Rewritten, the branch passes the scan, and the resume pushes it.
    host.state().leaks.clear();
    let g = Fixture::on(Rc::clone(&host), s.clone(), vec![]);
    assert_eq!(g.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let push = strings(&["push", "--quiet", "--set-upstream", "origin", s.branch.as_deref().unwrap()]);
    assert!(host.state().git_calls.contains(&push));
    assert_eq!(host.state().prs.len(), 1);
}

#[test]
fn a_run_that_pushes_needs_gitleaks_before_the_interview() {
    for gate in [false, true] {
        let host = FakeHost::default();
        host.state().gitleaks_missing = true;
        let f = Fixture::on(Rc::new(host), new_state(), approved());
        let (f, settings) = if gate { (f.gated(), no_pr()) } else { (f, RunSettings::default()) };
        let e = failure(f.workflow(settings).run());
        assert!(e.contains("install gitleaks 8.19"), "gate {gate}: {e}");
        assert!(names(&f, "start").is_empty(), "gate {gate}");
    }
}

#[test]
fn a_run_that_pushes_nothing_needs_no_gitleaks() {
    let host = FakeHost::default();
    host.state().gitleaks_missing = true;
    let f = Fixture::on(Rc::new(host), new_state(), approved());
    assert_eq!(f.workflow(no_pr()).run(), verdict(APPROVE));
    assert!(f.host.state().scans.is_empty());
}
