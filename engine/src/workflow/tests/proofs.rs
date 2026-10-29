//! TestProofs and TestProofInAWorktree: the verdict step's proofs, run for real by sh, in a scratch directory that
//! stands in for the project on a FakeHost whose other commands run for real (FakeState::processes), and in a real
//! run's worktree.
//!
//! TestProofInAWorktree's proof is a shell script that finds its repository with git, which looks upwards from the
//! proof as pytest does for its rootdir, rather than Python's pytest proof: the Rust tests have no pytest to run. So
//! its one case, test_a_proof_imports_the_worktrees_code_and_the_commit_holds_no_copy_of_it, is
//! a_proof_runs_on_the_worktrees_code_and_the_commit_holds_no_copy_of_it here.
//! test_the_reviewers_own_proofs_heading_does_not_stop_the_run has the Reviewer write its own "## Proofs" section,
//! where Python's adds it as the review is read.
//!
//! test_a_carried_proof_runs_again_after_the_rounds_last_quality_round_and_before_the_review logs what happens in a
//! file of the scratch project, which the proof appends to too, where Python wraps the Workflow's _judge_quality and
//! _run_proof: a quality round is logged as its snapshot is pushed, and a proof without its round, which its place
//! among the turns gives.
//!
//! Not here: test_proof_command_needs_a_verdict_step_and_names_the_proof_once, test_a_workflow_file_takes_proof_command_and_proofs_dir
//! and test_proof_command_round_trips_through_a_workflow_file, which are crate::pipeline's and crate::workflow_file's
//! (ticket 04).

use std::cell::RefCell;

use super::*;
use crate::constants::PROOF_COPIES_DIR;
use crate::herdr::fake::FakeHerdr;
use crate::host::fake::FakeClock;
use crate::host::tests::Repo;
use crate::host::{ProcessHost, PushError};
use crate::interrupt::Interrupt;
use crate::pipeline::{Pipeline, DEFAULT_WORKFLOW};
use crate::proofs::PROOFS_HEADING;
use crate::test_support::{ended, object as record, quick, raise_once_written, replaced, TempDir};
use crate::workflow::Collaborators;

const COMMAND: &str = "sh {proofs}";

/// How a proof the Reviewer writes exits.
#[derive(Clone, Copy)]
enum Exit {
    /// With this status.
    Status(i32),
    /// With 1 until a Builder turn fixes it (Proving::fixes), then 0.
    Fixable,
}

/// The quick workflow with the proof command `sh {proofs}`.
fn proving() -> Pipeline {
    replaced(&quick(), |s| s.proof_command = Some(COMMAND.into())).unwrap()
}

/// Write the proof file name, exiting as exit says, into dir.
fn write_proof(dir: &str, name: &str, exit: Exit) {
    std::fs::create_dir_all(dir).unwrap();
    let check = match exit {
        Exit::Fixable => format!("test -e fixed-{name} || exit 1\n"),
        Exit::Status(status) => format!("exit {status}\n"),
    };
    std::fs::write(format!("{dir}/{name}"), format!("echo checking {name}\n{check}")).unwrap();
}

/// The fixture of state played by script on a FakeHost whose commands but git run for real.
fn on_real_processes(state: RunState, script: Script) -> Fixture {
    let f = Fixture::on(Rc::new(FakeHost::default()), state, script);
    f.run_processes();
    f
}

/// A quick run in a scratch project whose Reviewer writes, per round, a review with a verdict and proofs: Python's
/// run_proofs.
struct Proving {
    /// Per round, the Reviewer's verdict and its proofs, each a file name and how it exits.
    reviews: Vec<(&'static str, Vec<(&'static str, Exit)>)>,
    /// How many turns the Builder takes.
    builds: u32,
    /// Per Builder turn from round 2 on, the Fixable proofs it makes pass.
    fixes: Vec<Vec<&'static str>>,
    max_rounds: u32,
    pipeline: Pipeline,
    /// What each review says after its numbered findings.
    review_end: &'static str,
}

impl Default for Proving {
    fn default() -> Proving {
        Proving { reviews: vec![], builds: 2, fixes: vec![], max_rounds: 3, pipeline: proving(), review_end: "" }
    }
}

/// The run Proving makes: the fixture and the settings to run it with, the Builder's prompts and the Reviewer's.
struct Proved {
    f: Fixture,
    settings: RunSettings,
    seen: Rc<RefCell<Vec<String>>>,
    reviewed: Rc<RefCell<Vec<String>>>,
}

impl Proving {
    fn run_in(self, proj: &TempDir) -> Proved {
        let (seen, reviewed) = (super::seen(), super::seen());
        let builds = (1..=self.builds)
            .map(|n| {
                let fixed: Vec<&str> = if n >= 2 { self.fixes.get(n as usize - 2).cloned().unwrap_or_default() } else { vec![] };
                let (dir, mut build) = (proj.path(), build_turn(n));
                let turn: Turn = Box::new(move |prompt, state, host| {
                    for name in &fixed {
                        std::fs::write(format!("{dir}/fixed-{name}"), "").unwrap();
                    }
                    build(prompt, state, host)
                });
                recording(turn, &seen)
            })
            .collect();
        let reviews = self
            .reviews
            .into_iter()
            .zip(1..)
            .map(|((verdict, proofs), n)| {
                let text = format!("VERDICT: {verdict}\n1. a bug\n2. another\n3. more\n{}", self.review_end);
                let turn: Turn = Box::new(move |_, state, host| {
                    for (name, exit) in &proofs {
                        write_proof(&state.proofs_dir(n), name, *exit);
                    }
                    host.write(&state.review_path(n), &text, false).unwrap();
                    "done".to_string()
                });
                recording(turn, &reviewed)
            })
            .collect();
        let mut state = RunState::new(RUN_ID, TASK, &proj.path());
        state.phase = "build".to_string();
        state.workflow = self.pipeline.name().to_string();
        let f = on_real_processes(state, vec![("build", builds), ("review", reviews)]);
        let settings = settings(&self.pipeline, self.max_rounds);
        Proved { f, settings, seen, reviewed }
    }
}

/// The settings of a run of pipeline without a pull request, its verdict loop bounded by max_rounds.
fn settings(pipeline: &Pipeline, max_rounds: u32) -> RunSettings {
    RunSettings { pipeline: Some(pipeline.clone()), max_rounds, pull_request: false, ..RunSettings::default() }
}

/// Proving's run of reviews, with its other settings the defaults.
fn run_proofs(proj: &TempDir, reviews: Vec<(&'static str, Vec<(&'static str, Exit)>)>) -> Proved {
    Proving { reviews, ..Proving::default() }.run_in(proj)
}

fn proofs_note(proofs: &str) -> String {
    format!("\n\n{}", filled(crate::prompts::PROOFS_NOTE, &[("proofs", proofs)]))
}

/// The note the Reviewer hears of proofs from earlier rounds run again in proj, items being its list's items.
fn carried_note(proj: &str, items: &[&str]) -> String {
    let proofs = items.join("\n\n");
    let note = filled(crate::prompts::CARRIED_PROOFS_NOTE, &[("command", COMMAND), ("cwd", proj), ("proofs", &proofs)]);
    format!("\n\n{note}")
}

/// (round, written, file, demonstrated) of each proof run the state records.
fn runs(state: &RunState) -> Vec<(i128, i128, String, bool)> {
    state
        .proofs
        .iter()
        .map(|p| {
            let int = |key: &str| p.get(key).and_then(Json::as_int).unwrap();
            (int("round"), int("written"), text(p, "file").unwrap(), p.get("demonstrated") == Some(&Json::Bool(true)))
        })
        .collect()
}

fn review(f: &Fixture, state: &RunState, n: u32) -> String {
    f.host.state().files.get(&state.review_path(n)).cloned().expect("the review")
}

#[test]
fn a_failing_proof_demonstrates_its_finding_and_the_builder_must_make_it_pass() {
    let proj = TempDir::new();
    let p = run_proofs(&proj, vec![
        (CHANGES_REQUESTED, vec![("test_1_bug.sh", Exit::Status(1)), ("test_2.sh", Exit::Status(0)), ("test_3.sh", Exit::Status(4))]),
        (APPROVE, vec![]),
    ]);
    let mut wf = p.f.workflow(p.settings.clone());
    assert_eq!(wf.run(), verdict(APPROVE));

    let state = wf.state();
    let reviewed = review(&p.f, state, 1);
    assert!(reviewed.starts_with("VERDICT: CHANGES_REQUESTED\n1. a bug\n"));
    let proj = proj.path();
    assert!(reviewed.contains(&format!(
        "## Proofs the orchestrator ran\n\nThe orchestrator ran each proof with `sh {{proofs}}` in {proj}, from a copy of \
         its directory under `{proj}/.orchestrator/proofs/`, removed after each run; a path to a proof in its output is \
         the copy's."
    )));
    assert!(reviewed.contains(
        "- `test_1_bug.sh` (finding 1): demonstrated, it fails.\n\n  ````\n  checking test_1_bug.sh\n  ````"
    ));
    assert!(reviewed.contains("- `test_2.sh` (finding 2): not demonstrated, it passes."));
    assert!(reviewed.contains("- `test_3.sh` (finding 3): not demonstrated, it errors, with exit status 4."));
    let firsts: Vec<(Option<i128>, String, bool)> = state
        .proofs
        .iter()
        .filter(|p| p.get("round") == Some(&Json::Int(1)))
        .map(|p| (p.get("finding").and_then(Json::as_int), text(p, "file").unwrap(), p.get("demonstrated") == Some(&Json::Bool(true))))
        .collect();
    assert_eq!(firsts, [
        (Some(1), "test_1_bug.sh".to_string(), true),
        (Some(2), "test_2.sh".to_string(), false),
        (Some(3), "test_3.sh".to_string(), false),
    ]);
    assert_eq!(text(&state.proofs[0], "said").unwrap(), "it fails:\nchecking test_1_bug.sh");
    let seen = p.seen.borrow();
    assert!(seen[1].contains(&proofs_note(&format!("{}/test_1_bug.sh", state.proofs_dir(1)))), "{}", seen[1]);
    assert!(!seen[0].contains("Reviewer backed"));
}

#[test]
fn findings_without_a_proof_stand_as_unproven() {
    let proj = TempDir::new();
    let p = run_proofs(&proj, vec![(CHANGES_REQUESTED, vec![("test_2.sh", Exit::Status(1))]), (APPROVE, vec![])]);
    let mut wf = p.f.workflow(p.settings.clone());
    wf.run().unwrap();
    assert!(review(&p.f, wf.state(), 1).contains("Findings without a proof, unproven: 1, 3."));
}

#[test]
fn no_proofs_directory_leaves_the_review_as_it_is() {
    let proj = TempDir::new();
    let p = run_proofs(&proj, vec![(CHANGES_REQUESTED, vec![]), (APPROVE, vec![])]);
    let mut wf = p.f.workflow(p.settings.clone());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(review(&p.f, wf.state(), 1), "VERDICT: CHANGES_REQUESTED\n1. a bug\n2. another\n3. more\n");
    assert!(wf.state().proofs.is_empty());
    assert!(!p.seen.borrow()[1].contains("Reviewer backed"));
}

#[test]
fn an_approval_runs_no_proof() {
    let proj = TempDir::new();
    let p = Proving { reviews: vec![(APPROVE, vec![("test_1.sh", Exit::Status(1))])], builds: 1, ..Proving::default() }
        .run_in(&proj);
    let mut wf = p.f.workflow(p.settings.clone());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert!(!review(&p.f, wf.state(), 1).contains(PROOFS_HEADING));
    assert!(wf.state().proofs.is_empty());
}

#[test]
fn a_workflow_without_proof_command_runs_none() {
    let proj = TempDir::new();
    let reviews = vec![(CHANGES_REQUESTED, vec![("test_1.sh", Exit::Status(1))]), (APPROVE, vec![])];
    let p = Proving { reviews, pipeline: quick(), ..Proving::default() }.run_in(&proj);
    let mut wf = p.f.workflow(p.settings.clone());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert!(!review(&p.f, wf.state(), 1).contains(PROOFS_HEADING));
}

/// A run in proj resumed in round 1's review, which is review, with proofs recorded: run to its approval in round 2,
/// whose review runs the demonstrated proofs again and finds no file. The fixture, the state it ended in, and the
/// Builder's prompts.
fn resumed_in_review(proj: &TempDir, review: &str, proofs: Vec<Map<Json>>) -> (Fixture, RunState, Vec<String>) {
    let mut state = RunState::new(RUN_ID, TASK, &proj.path());
    (state.phase, state.round, state.workflow, state.base) = ("review".into(), 1, "quick".into(), Some("abc123".into()));
    state.proofs = proofs;
    let seen = seen();
    let f = on_real_processes(state.clone(), vec![
        ("build", vec![recording(build_turn(2), &seen)]),
        ("review", vec![review_turn(2, APPROVE)]),
    ]);
    f.put(&state.review_path(1), review);
    f.put(&state.build_path(1), "report 1");
    let mut wf = f.workflow(settings(&proving(), 3));
    assert_eq!(wf.run(), verdict(APPROVE));
    let end = wf.state().clone();
    drop(wf);
    let prompts = seen.borrow().clone();
    (f, end, prompts)
}

const FIRST_RUN: &str = r#"{"round": 1, "file": "test_1.sh", "finding": 1, "demonstrated": true, "said": "it fails"}"#;

#[test]
fn a_review_judged_before_a_resume_is_not_judged_again() {
    let proj = TempDir::new();
    let judged = format!("VERDICT: CHANGES_REQUESTED\n1. a bug\n\n{PROOFS_HEADING}\n\nalready judged\n");
    let (f, state, seen) = resumed_in_review(&proj, &judged, vec![record(FIRST_RUN)]);
    assert_eq!(review(&f, &state, 1), judged);
    let firsts: Vec<&Map<Json>> = state.proofs.iter().filter(|p| p.get("round") == Some(&Json::Int(1))).collect();
    assert_eq!(firsts, [&record(FIRST_RUN)]);
    assert!(seen[0].contains(&proofs_note(&format!("{}/test_1.sh", state.proofs_dir(1)))));
}

#[test]
fn proofs_recorded_before_a_resume_are_appended_without_running_again() {
    // No proof file is on disk: running one again would fail to find it.
    let proj = TempDir::new();
    let (f, state, _) = resumed_in_review(&proj, "VERDICT: CHANGES_REQUESTED\n1. a bug\n", vec![record(FIRST_RUN)]);
    assert!(review(&f, &state, 1).contains("- `test_1.sh` (finding 1): demonstrated, it fails."));
}

#[test]
fn a_demonstrated_proof_runs_again_every_round_until_it_passes() {
    let proj = TempDir::new();
    let p = Proving {
        reviews: vec![
            (CHANGES_REQUESTED, vec![("test_1_bug.sh", Exit::Fixable), ("test_2.sh", Exit::Fixable), ("test_3.sh", Exit::Status(0))]),
            (CHANGES_REQUESTED, vec![("test_4.sh", Exit::Status(1))]),
            (CHANGES_REQUESTED, vec![]),
            (APPROVE, vec![]),
        ],
        builds: 4,
        fixes: vec![vec!["test_2.sh"], vec![], vec!["test_1_bug.sh"]],
        max_rounds: 4,
        ..Proving::default()
    }
    .run_in(&proj);
    let mut wf = p.f.workflow(p.settings.clone());
    assert_eq!(wf.run(), verdict(APPROVE));
    let state = wf.state();
    let (one, two, proj) = (state.proofs_dir(1), state.proofs_dir(2), proj.path());
    let (reviewed, seen) = (p.reviewed.borrow(), p.seen.borrow());

    assert!(!reviewed[0].contains("run again"));
    let fails_1 = format!("- `{one}/test_1_bug.sh`: still fails, it fails.\n\n  ````\n  checking test_1_bug.sh\n  ````");
    let fails_4 = format!("- `{two}/test_4.sh`: still fails, it fails.\n\n  ````\n  checking test_4.sh\n  ````");
    let passes_2 = format!("- `{one}/test_2.sh`: now passes.");
    let passes_1 = format!("- `{one}/test_1_bug.sh`: now passes.");
    assert!(reviewed[1].ends_with(&carried_note(&proj, &[&fails_1, &passes_2])), "{}", reviewed[1]);
    assert!(reviewed[2].ends_with(&carried_note(&proj, &[&fails_1, &fails_4])));
    assert!(reviewed[3].contains(&carried_note(&proj, &[&passes_1, &fails_4])));
    let expected: Vec<(i128, i128, String, bool)> = [
        (1, 1, "test_1_bug.sh", true), (1, 1, "test_2.sh", true), (1, 1, "test_3.sh", false),
        (2, 1, "test_1_bug.sh", true), (2, 1, "test_2.sh", false), (2, 2, "test_4.sh", true),
        (3, 1, "test_1_bug.sh", true), (3, 2, "test_4.sh", true),
        (4, 1, "test_1_bug.sh", false), (4, 2, "test_4.sh", true),
    ]
    .into_iter()
    .map(|(round, written, file, demonstrated)| (round, written, file.to_string(), demonstrated))
    .collect();
    assert_eq!(runs(state), expected);

    // The step the review loops back to hears every proof that still fails, from whichever round wrote it.
    assert!(seen[1].contains(&proofs_note(&format!("{one}/test_1_bug.sh, {one}/test_2.sh"))));
    assert!(seen[2].contains(&proofs_note(&format!("{one}/test_1_bug.sh, {two}/test_4.sh"))));
    assert!(seen[3].contains(&proofs_note(&format!("{one}/test_1_bug.sh, {two}/test_4.sh"))));

    // Round 1's review is the only one with proofs of its own written into it; reruns leave the reviews alone.
    assert!(!review(&p.f, state, 3).contains(PROOFS_HEADING));
    assert!(!review(&p.f, state, 2).contains("test_1_bug"));
}

#[test]
fn a_round_with_no_proof_to_carry_prompts_the_reviewer_as_before() {
    // Proofs that pass or error are not carried, so round 2's review is prompted as in a run without proofs.
    let proj = TempDir::new();
    let p = run_proofs(&proj, vec![
        (CHANGES_REQUESTED, vec![("test_1.sh", Exit::Status(0)), ("test_2.sh", Exit::Status(4))]),
        (APPROVE, vec![]),
    ]);
    let mut wf = p.f.workflow(p.settings.clone());
    assert_eq!(wf.run(), verdict(APPROVE));
    let rounds: Vec<i128> = runs(wf.state()).into_iter().map(|(round, ..)| round).collect();
    assert_eq!(rounds, [1, 1]);
    let without = run_proofs(&proj, vec![(CHANGES_REQUESTED, vec![]), (APPROVE, vec![])]);
    assert_eq!(without.f.workflow(without.settings.clone()).run(), verdict(APPROVE));
    assert_eq!(*p.reviewed.borrow(), *without.reviewed.borrow());
}

/// A run in proj saved in phase phase of round n, with the proofs runs, round 1's review judged and each round's
/// report before n written: the fixture, on a FakeHost whose processes are real.
fn saved_with_proofs(proj: &TempDir, phase: &str, n: u32, runs: Vec<Map<Json>>, script: Script) -> Fixture {
    let mut state = RunState::new(RUN_ID, TASK, &proj.path());
    (state.phase, state.round, state.workflow, state.base) = (phase.into(), n, "quick".into(), Some("abc123".into()));
    state.proofs = runs;
    let f = on_real_processes(state.clone(), script);
    for i in 1..n {
        f.put(&state.build_path(i), &format!("report {i}"));
    }
    f.put(&state.review_path(1), &format!("VERDICT: CHANGES_REQUESTED\n1. a bug\n\n{PROOFS_HEADING}\n"));
    f
}

#[test]
fn reruns_recorded_before_a_resume_are_not_run_again_and_still_heard() {
    // No proof file is on disk: running one again would record that it errors.
    let proj = TempDir::new();
    let rerun = r#"{"round": 2, "written": 1, "file": "test_1.sh", "finding": 1, "demonstrated": true, "said": "it fails:\nstill broken"}"#;
    let reviewed = seen();
    let f = saved_with_proofs(&proj, "review", 2, vec![record(FIRST_RUN), record(rerun)], vec![
        ("review", vec![recording(review_turn(2, APPROVE), &reviewed)]),
    ]);
    f.put(&f.state.build_path(2), "report 2");
    let mut wf = f.workflow(settings(&proving(), 3));
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(wf.state().proofs, [record(FIRST_RUN), record(rerun)]);
    let item = format!("- `{}/test_1.sh`: still fails, it fails.\n\n  ````\n  still broken\n  ````", f.state.proofs_dir(1));
    assert!(reviewed.borrow()[0].contains(&carried_note(&proj.path(), &[&item])));
}

#[test]
fn a_proof_recorded_before_reruns_existed_runs_again() {
    let proj = TempDir::new();
    let (seen, reviewed) = (seen(), seen());
    let saved = record(&format!(
        r#"{{"run_id": "{RUN_ID}", "task": "{TASK}", "cwd": "{}", "phase": "build", "round": 2, "workflow": "quick",
            "base": "abc123", "proofs": [{FIRST_RUN}]}}"#,
        proj.path()
    ));
    let state = RunState::from_saved(&saved).unwrap();
    std::fs::create_dir_all(state.proofs_dir(1)).unwrap();
    std::fs::write(format!("{}/test_1.sh", state.proofs_dir(1)), "exit 0\n").unwrap();
    let f = saved_with_proofs(&proj, "build", 2, state.proofs.clone(), vec![
        ("build", vec![recording(build_turn(2), &seen)]),
        ("review", vec![recording(review_turn(2, APPROVE), &reviewed)]),
    ]);
    let mut wf = f.workflow(settings(&proving(), 3));
    assert_eq!(wf.run(), verdict(APPROVE));
    let path = format!("{}/test_1.sh", state.proofs_dir(1));
    assert!(seen.borrow()[0].contains(&proofs_note(&path)));
    let rerun = r#"{"round": 2, "written": 1, "file": "test_1.sh", "finding": 1, "demonstrated": false, "said": "it passes"}"#;
    assert_eq!(wf.state().proofs[1], record(rerun));
    assert!(reviewed.borrow()[0].contains(&carried_note(&proj.path(), &[&format!("- `{path}`: now passes.")])));
}

#[test]
fn a_proof_that_errors_when_run_again_stays_carried() {
    // A change that makes a demonstrated proof crash has not made it pass.
    let proj = TempDir::new();
    let first = r#"{"round": 1, "written": 1, "file": "test_1.sh", "finding": 1, "demonstrated": true, "said": "it fails"}"#;
    let (seen, reviewed) = (seen(), seen());
    let f = saved_with_proofs(&proj, "build", 2, vec![record(first)], vec![
        ("build", vec![recording(build_turn(2), &seen), recording(build_turn(3), &seen)]),
        ("review", vec![
            recording(review_turn(2, CHANGES_REQUESTED), &reviewed),
            recording(review_turn(3, APPROVE), &reviewed),
        ]),
    ]);
    let dir = f.state.proofs_dir(1);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(format!("{dir}/test_1.sh"), "echo crashed\nexit 2\n").unwrap();
    let mut wf = f.workflow(settings(&proving(), 3));
    assert_eq!(wf.run(), verdict(APPROVE));
    let path = format!("{dir}/test_1.sh");
    let item = format!("- `{path}`: no longer fails but errors, it errors, with exit status 2.\n\n  ````\n  crashed\n  ````");
    for prompt in reviewed.borrow().iter() {
        assert!(prompt.contains(&carried_note(&proj.path(), &[&item])), "{prompt}");
    }
    assert!(seen.borrow()[1].contains(&proofs_note(&path)));
    let demonstrated: Vec<(i128, bool)> = runs(wf.state()).into_iter().map(|(round, _, _, d)| (round, d)).collect();
    assert_eq!(demonstrated, [(1, true), (2, false), (3, false)]);
}

#[test]
fn the_reviewers_own_proofs_heading_does_not_stop_the_run() {
    let proj = TempDir::new();
    let p = Proving {
        reviews: vec![(CHANGES_REQUESTED, vec![("test_1.sh", Exit::Status(1))]), (APPROVE, vec![])],
        review_end: "\n## Proofs\n\n1. test_1.sh\n",
        ..Proving::default()
    }
    .run_in(&proj);
    let mut wf = p.f.workflow(p.settings.clone());
    assert_eq!(wf.run(), verdict(APPROVE));
    let demonstrated: Vec<bool> = runs(wf.state()).into_iter().filter(|r| r.0 == 1).map(|r| r.3).collect();
    assert_eq!(demonstrated, [true]);
    // Beyond Python's case: the orchestrator's own section follows the Reviewer's.
    let judged = review(&p.f, wf.state(), 1);
    assert!(judged.contains(&format!("## Proofs\n\n1. test_1.sh\n\n{PROOFS_HEADING}\n")), "{judged}");
}

/// The Workflow of a run in proj that has run no proof yet, to run one by hand.
fn unrun(proj: &TempDir) -> Proved {
    run_proofs(proj, vec![(CHANGES_REQUESTED, vec![]), (APPROVE, vec![])])
}

#[test]
fn a_proof_that_runs_too_long_is_stopped_with_what_it_started() {
    let proj = TempDir::new();
    let p = unrun(&proj);
    let mut wf = p.f.workflow(p.settings.clone());
    wf.proof_timeout = 1;
    let dir = wf.state().proofs_dir(1);
    std::fs::create_dir_all(&dir).unwrap();
    let leaked = proj.join("leaked");
    std::fs::write(format!("{dir}/test_1.sh"), format!("(sleep 3; echo leaked > {leaked}) &\nsleep 30\n")).unwrap();
    assert_eq!(wf.run_proof(COMMAND, 1, "test_1.sh"), Ok((false, "it errors, running out of its 1 s".to_string())));
    std::thread::sleep(std::time::Duration::from_secs(4));
    assert!(!std::path::Path::new(&leaked).exists());
}

#[test]
fn a_proof_runs_from_a_copy_of_its_directory_in_the_checkout_which_goes_after_it() {
    let proj = TempDir::new();
    let p = unrun(&proj);
    let wf = p.f.workflow(p.settings.clone());
    let dir = wf.state().proofs_dir(1);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(format!("{dir}/data.txt"), "beside the proof\n").unwrap();
    // Reads the file beside it, and leaves a directory it cannot be removed from as it is.
    std::fs::write(
        format!("{dir}/test_1.sh"),
        "here=$(dirname \"$0\"); echo \"$0\"; cat \"$here/data.txt\"; mkdir \"$here/locked\"; \
         touch \"$here/locked/f\"; chmod 555 \"$here/locked\"; exit 1\n",
    )
    .unwrap();
    let copies = format!("{}/{PROOF_COPIES_DIR}", proj.path());
    assert_eq!(
        wf.run_proof(COMMAND, 1, "test_1.sh"),
        Ok((true, format!("it fails:\n{copies}/1/test_1.sh\nbeside the proof")))
    );
    assert!(!std::path::Path::new(&copies).exists());
    let mut listed: Vec<String> =
        std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
    listed.sort();
    assert_eq!(listed, ["data.txt", "test_1.sh"]);
}

#[test]
fn a_proof_whose_file_or_directory_is_gone_did_not_run() {
    let proj = TempDir::new();
    let p = unrun(&proj);
    let wf = p.f.workflow(p.settings.clone());
    let dir = wf.state().proofs_dir(1);
    // The proof is gone from a directory that stays, as when the Builder moved it into the project; then the
    // directory goes too. A command that exits 1 for a missing file would make either look demonstrated.
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(format!("{dir}/test_2.sh"), "").unwrap();
    let did_not_run = || {
        let (failed, said) = wf.run_proof("test -f {proofs} || exit 1", 1, "test_1.sh").unwrap();
        assert!(!failed);
        assert!(said.starts_with("it did not run: ") && said.contains("proofs-1/test_1.sh is gone"), "{said}");
        assert!(!std::path::Path::new(&format!("{}/{PROOF_COPIES_DIR}", proj.path())).exists());
    };
    did_not_run();
    std::fs::remove_dir_all(&dir).unwrap();
    did_not_run();
}

#[test]
fn an_interrupted_proof_is_no_outcome() {
    // Not in Python's TestProofs: a Ctrl-C is Python's KeyboardInterrupt, which stops the run before a proof's outcome
    // is recorded, and starts no proof once raised.
    let proj = TempDir::new();
    let p = unrun(&proj);
    let wf = p.f.workflow(p.settings.clone());
    let dir = wf.state().proofs_dir(1);
    let ran = proj.join("ran");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(format!("{dir}/test_1.sh"), format!(": > {ran}\nexit 1\n")).unwrap();
    p.f.interrupt.raise();
    assert_eq!(wf.run_proof(COMMAND, 1, "test_1.sh"), Err(Stopped::Interrupted));
    assert!(!std::path::Path::new(&ran).exists(), "the proof ran");
    assert!(!std::path::Path::new(&format!("{}/{PROOF_COPIES_DIR}", proj.path())).exists());
}

#[test]
fn a_ctrl_c_while_a_proof_runs_stops_it_with_what_it_started() {
    // Not in Python's TestProofs: the proof runs under GNU timeout, which leads the process group ProcessHost started,
    // so the group the Ctrl-C stops holds the proof and what it started; and the run records no outcome of it.
    let proj = TempDir::new();
    let (pid, background) = (proj.join("pid"), proj.join("background"));
    let p = run_proofs(&proj, vec![(CHANGES_REQUESTED, vec![]), (APPROVE, vec![])]);
    let dir = p.f.state.proofs_dir(1);
    std::fs::create_dir_all(&dir).unwrap();
    let proof = format!("sleep 600 & echo $! > '{background}'\necho $$ > '{pid}'\nwait\n");
    std::fs::write(format!("{dir}/test_1.sh"), proof).unwrap();
    let raising = raise_once_written(&pid, &p.f.interrupt);
    let started = std::time::Instant::now();
    assert_eq!(p.f.workflow(p.settings.clone()).run(), Err(Stopped::Interrupted));
    raising.join().unwrap();
    // Caught, so that the release's commands ran.
    assert!(!p.f.interrupt.is_raised());
    assert!(started.elapsed() < std::time::Duration::from_secs(60));
    assert!(ended(&pid), "the proof outlived the Ctrl-C");
    assert!(ended(&background), "what the proof started outlived the Ctrl-C");
    // Removed as Python's finally removes them.
    assert!(!std::path::Path::new(&format!("{}/{PROOF_COPIES_DIR}", proj.path())).exists());
    let saved = RunState::from_saved(&p.f.saved()).unwrap();
    assert_eq!((saved.error.as_deref(), saved.phase.as_str(), saved.proofs.len()), (Some("interrupted"), "review", 0));
}

// -- TestProofInAWorktree ------------------------------------------------------------------------------------------

/// The repository's Host, but for publishing's secret scan, push and pull request, which find nothing, push nothing
/// and open https://github.com/o/r/pull/1: Python's patch.object of the three.
struct Unpublished<'h>(&'h ProcessHost);

impl Host for Unpublished<'_> {
    fn ssh_target(&self) -> Option<&str> {
        None
    }

    fn exec(&self, argv: &[&str], stdin: Option<&[u8]>, timeout: u64) -> Result<crate::host::Exec, crate::host::ExecError> {
        self.0.exec(argv, stdin, timeout)
    }

    fn refuse_leaks(&self, _cwd: &str, _commit: &str, _base: &str, _report_path: &str) -> Result<(), PushError> {
        Ok(())
    }

    fn push_branch(&self, _cwd: &str, _branch: &str, _base: &str, _report_path: &str) -> Result<(), PushError> {
        Ok(())
    }

    fn create_pr(
        &self,
        _cwd: &str,
        _base: &str,
        _head: &str,
        _title: &str,
        _body: &str,
        _draft: bool,
    ) -> Result<String, crate::error::OrchestratorError> {
        Ok("https://github.com/o/r/pull/1".to_string())
    }
}

#[test]
fn a_proof_runs_on_the_worktrees_code_and_the_commit_holds_no_copy_of_it() {
    // The checkout and the run's worktree differ in the code the proof reads, and the proof finds that code where
    // git finds the repository it lies in: a proof that ran in the checkout's run directory would read the checkout's.
    const BRANCH: &str = "orchestrator/a1b2c3-rate";
    let repo = Repo::new(&[("limiter.sh", "RATE=base\n")]);
    let origin = format!("{}/origin.git", repo.outside);
    repo.git(&["clone", "--quiet", "--bare", &repo.path, &origin]);
    repo.git(&["remote", "add", "origin", &origin]);
    repo.git(&["fetch", "--quiet", "origin"]);
    let worktree = format!("{}/{}/a1b2c3", repo.path, crate::constants::WORKTREES_DIR);
    repo.git(&["worktree", "add", "--quiet", "-b", BRANCH, &worktree, "main"]);
    std::fs::write(format!("{worktree}/limiter.sh"), "RATE=change\n").unwrap();

    let mut state = RunState::new(RUN_ID, TASK, &repo.path);
    (state.phase, state.round, state.workflow) = ("review".into(), 1, "quick".into());
    (state.base, state.base_branch, state.branch) = (repo.base.clone(), Some("main".into()), Some(BRANCH.into()));
    (state.pull_request, state.worktree) = (true, Some(worktree.clone()));
    let host = Unpublished(&repo.host);
    let herdr = FakeHerdr::new(Rc::new(FakeHost::default()), state.clone(), vec![]);
    let (clock, told, interrupt) = (FakeClock::default(), Told::default(), Interrupt::new());
    let with = Collaborators {
        host: &host,
        herdr: &herdr,
        clocks: &clock,
        human: &told,
        interrupt: &interrupt,
        pid_alive: &crate::runs::pid_alive,
        jenkins: None,
        pypi: &crate::pypi::PyPI::new(crate::pypi::fake::FakePyPI::from_json(Default::default())),
    };
    let mut wf = crate::workflow::Workflow::new(with, state.clone(), settings(&proving(), 3)).unwrap();
    // Ignores the checkout's .orchestrator directory and owns the run, as a run's start does.
    wf.claim().unwrap();

    // Writes beside itself too, which only the copy's directory may hold.
    let dir = state.proofs_dir(1);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        format!("{dir}/test_1_rate.sh"),
        "here=$(dirname \"$0\"); : > \"$here/beside.txt\"\ntop=$(git -C \"$here\" rev-parse --show-toplevel) || exit 2\n\
         . \"$top/limiter.sh\"; echo \"$top/limiter.sh\"; echo \"RATE is $RATE\"; [ \"$RATE\" = base ] || exit 1\n",
    )
    .unwrap();
    let copies = format!("{worktree}/{PROOF_COPIES_DIR}");
    std::fs::create_dir_all(format!("{copies}/1")).unwrap();
    std::fs::write(format!("{copies}/1/test_9_old.sh"), "").unwrap();

    let (failed, said) = wf.run_proof(COMMAND, 1, "test_1_rate.sh").unwrap();

    assert!(failed, "{said}");
    let physical = std::fs::canonicalize(&worktree).unwrap();
    assert!(said.contains(&format!("\n{}/limiter.sh\nRATE is change", physical.display())), "{said}");
    assert!(!std::path::Path::new(&copies).exists());
    assert_eq!(repo.host.read(&format!("{worktree}/.orchestrator/.gitignore")).unwrap().as_deref(), Some("*\n"));
    let listed: Vec<String> =
        std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
    assert_eq!(listed, ["test_1_rate.sh"]);
    assert_eq!(repo.host.git(&worktree, &["status", "--porcelain"]).unwrap(), " M limiter.sh\n");

    wf.publish().unwrap();
    let committed = repo.git(&["ls-tree", "-r", "--name-only", BRANCH]);
    assert_eq!(committed.split_whitespace().collect::<Vec<_>>(), ["limiter.sh"]);
    assert_eq!(repo.git(&["show", &format!("{BRANCH}:limiter.sh")]), "RATE=change\n");
}

#[test]
fn a_carried_proof_runs_again_after_the_rounds_last_quality_round_and_before_the_review() {
    // Round 2's first quality round fails, so its last is the one after the Builder's fix.
    let proj = TempDir::new();
    let dir = proj.path();
    let log = format!("{dir}/events.log");
    let event = {
        let log = log.clone();
        move |line: String| {
            use std::io::Write;
            let mut file = std::fs::OpenOptions::new().create(true).append(true).open(&log).unwrap();
            writeln!(file, "{line}").unwrap();
        }
    };
    let logged = |name: &'static str, mut turn: Turn| -> Turn {
        let event = event.clone();
        Box::new(move |prompt, state, host| {
            event(format!("{name} {}", state.round));
            turn(prompt, state, host)
        })
    };
    let mut review_1 = review_turn(1, CHANGES_REQUESTED);
    let proof_log = log.clone();
    let review_with_proof: Turn = Box::new(move |prompt, state, host| {
        std::fs::create_dir_all(state.proofs_dir(1)).unwrap();
        std::fs::write(format!("{}/test_1.sh", state.proofs_dir(1)), format!("echo proof >> '{proof_log}'\nexit 1\n"))
            .unwrap();
        review_1(prompt, state, host)
    });
    let pipeline = replaced(&DEFAULT_WORKFLOW, |s| s.proof_command = Some(COMMAND.into())).unwrap();
    let mut state = RunState::new(RUN_ID, TASK, &dir);
    (state.phase, state.workflow) = ("build".into(), pipeline.name().to_string());
    let host = Rc::new(FakeHost::default());
    // git ignores the scratch project's .orchestrator, as FakeHost has it ignore /proj's, for the gate's clean-tree
    // check; and a quality round is logged as its snapshot is pushed.
    let (weak, orchestrator_dir, pushed) = (Rc::downgrade(&host), format!("{dir}/.orchestrator/"), event.clone());
    host.state().before_git = Some(Box::new(move |_, args| {
        if let Some(host) = weak.upgrade() {
            host.state().changed.retain(|p| !p.starts_with(&orchestrator_dir));
        }
        if let ["push", "--quiet", "--force", "origin", refspec] = args {
            let round = refspec.rsplit_once("a1b2c3-").map_or("", |(_, nq)| nq).replace("-q", " ");
            pushed(format!("quality round {round}"));
        }
    }));
    let f = Fixture::on(host, state, vec![
        ("build", vec![logged("build", build_turn(1)), logged("build", build_turn(2)), logged("quality fix", fix_turn(2, 1))]),
        ("review", vec![logged("review", review_with_proof), logged("review", review_turn(2, APPROVE))]),
    ])
    .gated();
    f.run_processes();
    f.ci.state().outcomes = vec![green(), red(), green()].into();

    let mut wf = f.workflow(settings(&pipeline, 3));
    assert_eq!(wf.run(), verdict(APPROVE));
    let events = std::fs::read_to_string(&log).unwrap();
    assert_eq!(
        events.lines().collect::<Vec<_>>(),
        [
            "build 1",
            "quality round 1 1",
            "review 1",
            "proof",
            "build 2",
            "quality round 2 1",
            "quality fix 2",
            "quality round 2 2",
            "proof",
            "review 2"
        ]
    );
    let proofs: Vec<(Option<i128>, Option<i128>, Option<bool>)> = wf
        .state()
        .proofs
        .iter()
        .map(|p| {
            let int = |k: &str| p.get(k).and_then(Json::as_int);
            (int("round"), int("written"), p.get("demonstrated").and_then(Json::as_bool))
        })
        .collect();
    assert_eq!(proofs, [(Some(1), Some(1), Some(true)), (Some(2), Some(1), Some(true))]);
}
