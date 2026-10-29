//! TestDefaultWorkflowPrompts, TestWorkflowPromptsGolden and the Workflow-driven cases of TestPromptFiles: the
//! prompts each workflow sends as Workflow fills them in, held to the Python engine's texts, and the prompt files that
//! word them.
//!
//! Not here: TestPromptFiles' cases of main and drain (test_bad_files_stop_run_before_any_agent,
//! test_a_bad_file_stops_drain_before_any_run, test_workflows_*) are the CLI's (ticket 09), and
//! test_a_state_that_replaces_an_unknown_prompt resumable_state's (crate::cli, cli/start/tests.rs); its other cases are crate::prompts' and
//! crate::workflow_file's (ticket 04). The cases here that Python starts with new_state run on the state it would give,
//! built by hand (new_state is the CLI's).

use super::*;
use crate::ci::fake::Outcome;
use crate::env::ConfigDir;
use crate::pipeline::{find_workflow, Pipeline, DEFAULT_WORKFLOW};
use crate::prompts::{load_prompts, FIX_PROMPT};
use crate::test_support::{quick, TempDir, EXAMPLES};
use crate::workflow_file::load_workflow_file;

const D: &str = "/proj/.orchestrator/runs/20260929-120000-a1b2c3";

// The default workflow's prompts as commit d6bb100 sent them, recorded by running the scenarios of
// TestDefaultWorkflowPrompts on that commit's code, as Python's GOLDEN holds them: since then, SPEC_PROMPT's "Separate
// Claude Code sessions" became "Separate agent sessions", and every prompt the Builder gets ends with BLOCKED_NOTE.

pub(super) const FRESH_BUILD_2: &str = concat!(
    "You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The spec in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md was agreed with the human by a separate ",
    "session; it is your contract.\n\nImplement it in /proj, following the conventions of the ",
    "surrounding code. Verify the change the way the spec's Verification section says, and run the ",
    "tests. Do not commit, push or switch branches; leave the changes in the working tree for the ",
    "Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your ",
    "report.\n\nAs your last step, write a report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md in a single write; it hands the work to ",
    "the Reviewer: the files you changed and why, how you verified the change (commands and a summary of ",
    "their results), and any acceptance criterion you did not meet, with the reason.\n\nThis is round 2, ",
    "and you are a fresh session. An earlier Builder session did the earlier turns; its changes are ",
    "already in the working tree, and its reports are ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1.md.\n\nThe Reviewer requested changes; the ",
    "findings are in /proj/.orchestrator/runs/20260929-120000-a1b2c3/review-1.md. Fix each finding, or ",
    "explain in your report why it is wrong. Re-run the verification. Do not commit, push or switch ",
    "branches. As your last step, write a new report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md in a single write, in the same shape as ",
    "before, answering each finding by its number.\n\nIf you cannot continue without a decision from the ",
    "human, write the report with a first line `BLOCKED: <your question>` instead.",
);

const FRESH_REVIEW_2: &str = concat!(
    "You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did not ",
    "write this change. Judge it only against the spec in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md and the code itself.\n\nThe change: `git ",
    "diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\nThe Builder's ",
    "report is in /proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md. Treat its claims as ",
    "unverified: check them, and run the verification yourself. Do not modify any file other than your ",
    "review.\n\nAs your last step, write your review to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/review-2.md in a single write. Its first line must ",
    "be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered findings, each ",
    "with file:line, what is wrong, and which acceptance criterion it violates or what failure it ",
    "causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. ",
    "Style preferences are not defects.\n\nThis is round 2, and you are a fresh session. The earlier ",
    "reviews of this change are /proj/.orchestrator/runs/20260929-120000-a1b2c3/review-1.md; check that ",
    "the Builder has answered each of their findings.",
);

const CONTINUE_BUILD_2: &str = concat!(
    "Your session was restarted in the middle of this turn. Continue where you left off; the turn still ",
    "ends when you write /proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md in a single write.",
);

const SPEC_INTERVIEW: &str = concat!(
    "You are the Spec Collector, the first of three roles (Spec Collector -> Builder -> Reviewer). ",
    "Separate agent sessions play the Builder and the Reviewer; they will know only what you write ",
    "down. A human is at this terminal and answers you directly.\n\nTask from the human:\nadd a rate ",
    "limiter\n\nInterview the human until the requirements are unambiguous: the goal, what is in and ",
    "out of scope, testable acceptance criteria, constraints, and how the result will be verified. ",
    "Read the code in /proj first so your questions are grounded and you can cite the files the ",
    "change touches. Ask a few questions at a time. Do not write or change any code.\n\nWhen the human ",
    "approves the spec, write it in a single write to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md as Markdown. Its first line is a `# ` ",
    "heading: a short imperative title for the change, under 70 characters; it becomes the commit ",
    "subject and the pull request title. Then these `##` sections: Goal, Scope, Non-goals, ",
    "Acceptance criteria (a numbered list, each one checkable), Relevant code (file:line), ",
    "Verification. Writing that file hands the work to the Builder, so write it only after the human ",
    "approves it.",
);

const BUILD_1: &str = concat!(
    "You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The ",
    "spec in /proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md was agreed with the human by a ",
    "separate session; it is your contract.\n\nImplement it in /proj, following the conventions of the ",
    "surrounding code. Verify the change the way the spec's Verification section says, and run the ",
    "tests. Do not commit, push or switch branches; leave the changes in the working tree for the ",
    "Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your ",
    "report.\n\nAs your last step, write a report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1.md in a single write; it hands the work ",
    "to the Reviewer: the files you changed and why, how you verified the change (commands and a ",
    "summary of their results), and any acceptance criterion you did not meet, with the reason.\n\nIf ",
    "you cannot continue without a decision from the human, write the report with a first line ",
    "`BLOCKED: <your question>` instead.",
);

const QUALITY_FIX_1_1: &str = concat!(
    "The quality gate did not pass on your change; the findings are in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/quality-1-1.md. Fix each numbered issue, the ",
    "failed conditions, the failing tests and a failed build, or explain in your report why one ",
    "should stand. Answer each numbered finding to weigh the same way; those never fail the gate, ",
    "but the Reviewer reads your answers. Re-run the verification. Do not commit, push or switch ",
    "branches. As your last step, write a new report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1-q1.md in a single write, in the same ",
    "shape as before, answering each numbered issue and finding to weigh by its number.\n\nIf you ",
    "cannot continue without a decision from the human, write the report with a first line `BLOCKED: ",
    "<your question>` instead.",
);

const REVIEW_1_PASSED: &str = concat!(
    "You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did ",
    "not write this change. Judge it only against the spec in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md and the code itself.\n\nThe change: `git ",
    "diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\nThe Builder's ",
    "report is in /proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1-q1.md. Treat its claims as ",
    "unverified: check them, and run the verification yourself. Do not modify any file other than ",
    "your review.\n\nAs your last step, write your review to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/review-1.md in a single write. Its first line ",
    "must be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered ",
    "findings, each with file:line, what is wrong, and which acceptance criterion it violates or ",
    "what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, ",
    "a broken test. Style preferences are not defects.\n\nThe quality gate passed on this change; its ",
    "report is in /proj/.orchestrator/runs/20260929-120000-a1b2c3/quality-1-2.md.",
);

const FIX_2: &str = concat!(
    "The Reviewer requested changes; the findings are in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/review-1.md. Fix each finding, or explain in ",
    "your report why it is wrong. Re-run the verification. Do not commit, push or switch branches. ",
    "As your last step, write a new report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md in a single write, in the same shape ",
    "as before, answering each finding by its number.\n\nIf you cannot continue without a decision ",
    "from the human, write the report with a first line `BLOCKED: <your question>` instead.",
);

const QUALITY_FIX_2_1: &str = concat!(
    "The quality gate did not pass on your change; the findings are in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/quality-2-1.md. Fix each numbered issue, the ",
    "failed conditions, the failing tests and a failed build, or explain in your report why one ",
    "should stand. Answer each numbered finding to weigh the same way; those never fail the gate, ",
    "but the Reviewer reads your answers. Re-run the verification. Do not commit, push or switch ",
    "branches. As your last step, write a new report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2-q1.md in a single write, in the same ",
    "shape as before, answering each numbered issue and finding to weigh by its number.\n\nIf you ",
    "cannot continue without a decision from the human, write the report with a first line `BLOCKED: ",
    "<your question>` instead.",
);

const RECHECK_2_UNRESOLVED: &str = concat!(
    "The Builder has answered your review; the new report is in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2-q1.md. Review the change again (`git ",
    "diff abc123` in /proj, plus the untracked files `git status --porcelain` lists) against the ",
    "spec in /proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md and your previous findings, ",
    "checking the Builder's claims rather than trusting them. As your last step, write the review to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/review-2.md in a single write, with the same ",
    "first-line verdict and numbered findings as before.\n\nThe quality gate still did not pass after ",
    "the Builder's last quality round; its unresolved findings are in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/quality-2-2.md. Weigh them as you would your ",
    "own.",
);

const REVIEW_1_UNRESOLVED: &str = concat!(
    "You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did ",
    "not write this change. Judge it only against the spec in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md and the code itself.\n\nThe change: `git ",
    "diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\nThe Builder's ",
    "report is in /proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1-q1.md. Treat its claims as ",
    "unverified: check them, and run the verification yourself. Do not modify any file other than ",
    "your review.\n\nAs your last step, write your review to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/review-1.md in a single write. Its first line ",
    "must be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered ",
    "findings, each with file:line, what is wrong, and which acceptance criterion it violates or ",
    "what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, ",
    "a broken test. Style preferences are not defects.\n\nThe quality gate still did not pass after ",
    "the Builder's last quality round; its unresolved findings are in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/quality-1-2.md. Weigh them as you would your ",
    "own.",
);

const RECHECK_2_PASSED: &str = concat!(
    "The Builder has answered your review; the new report is in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md. Review the change again (`git diff ",
    "abc123` in /proj, plus the untracked files `git status --porcelain` lists) against the spec in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md and your previous findings, checking ",
    "the Builder's claims rather than trusting them. As your last step, write the review to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/review-2.md in a single write, with the same ",
    "first-line verdict and numbered findings as before.\n\nThe quality gate passed on this change; ",
    "its report is in /proj/.orchestrator/runs/20260929-120000-a1b2c3/quality-2-1.md.",
);

const FRESH_BUILD_2_AFTER_QUALITY_ROUNDS: &str = concat!(
    "You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The ",
    "spec in /proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md was agreed with the human by a ",
    "separate session; it is your contract.\n\nImplement it in /proj, following the conventions of the ",
    "surrounding code. Verify the change the way the spec's Verification section says, and run the ",
    "tests. Do not commit, push or switch branches; leave the changes in the working tree for the ",
    "Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your ",
    "report.\n\nAs your last step, write a report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md in a single write; it hands the work ",
    "to the Reviewer: the files you changed and why, how you verified the change (commands and a ",
    "summary of their results), and any acceptance criterion you did not meet, with the reason.\n\n",
    "This is round 2, and you are a fresh session. An earlier Builder session did the earlier turns; ",
    "its changes are already in the working tree, and its reports are ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1.md, ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1-q1.md, ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1-q2.md.\n\nThe Reviewer requested changes; ",
    "the findings are in /proj/.orchestrator/runs/20260929-120000-a1b2c3/review-1.md. Fix each ",
    "finding, or explain in your report why it is wrong. Re-run the verification. Do not commit, ",
    "push or switch branches. As your last step, write a new report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md in a single write, in the same shape ",
    "as before, answering each finding by its number.\n\nIf you cannot continue without a decision ",
    "from the human, write the report with a first line `BLOCKED: <your question>` instead.",
);

const FRESH_QUALITY_FIX_2_1: &str = concat!(
    "You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The ",
    "spec in /proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md was agreed with the human by a ",
    "separate session; it is your contract.\n\nImplement it in /proj, following the conventions of the ",
    "surrounding code. Verify the change the way the spec's Verification section says, and run the ",
    "tests. Do not commit, push or switch branches; leave the changes in the working tree for the ",
    "Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your ",
    "report.\n\nAs your last step, write a report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2-q1.md in a single write; it hands the ",
    "work to the Reviewer: the files you changed and why, how you verified the change (commands and ",
    "a summary of their results), and any acceptance criterion you did not meet, with the reason.\n\n",
    "This is round 2, and you are a fresh session. An earlier Builder session did the earlier turns; ",
    "its changes are already in the working tree, and its reports are ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1.md, ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-1-q1.md, ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md.\n\nThe quality gate did not pass on ",
    "your change; the findings are in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/quality-2-1.md. Fix each numbered issue, the ",
    "failed conditions, the failing tests and a failed build, or explain in your report why one ",
    "should stand. Answer each numbered finding to weigh the same way; those never fail the gate, ",
    "but the Reviewer reads your answers. Re-run the verification. Do not commit, push or switch ",
    "branches. As your last step, write a new report to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2-q1.md in a single write, in the same ",
    "shape as before, answering each numbered issue and finding to weigh by its number.\n\nIf you ",
    "cannot continue without a decision from the human, write the report with a first line `BLOCKED: ",
    "<your question>` instead.",
);

const FRESH_REVIEW_2_PASSED: &str = concat!(
    "You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did ",
    "not write this change. Judge it only against the spec in ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/spec.md and the code itself.\n\nThe change: `git ",
    "diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\nThe Builder's ",
    "report is in /proj/.orchestrator/runs/20260929-120000-a1b2c3/build-2.md. Treat its claims as ",
    "unverified: check them, and run the verification yourself. Do not modify any file other than ",
    "your review.\n\nAs your last step, write your review to ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/review-2.md in a single write. Its first line ",
    "must be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered ",
    "findings, each with file:line, what is wrong, and which acceptance criterion it violates or ",
    "what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, ",
    "a broken test. Style preferences are not defects.\n\nThe quality gate passed on this change; its ",
    "report is in /proj/.orchestrator/runs/20260929-120000-a1b2c3/quality-2-1.md.\n\nThis is round 2, ",
    "and you are a fresh session. The earlier reviews of this change are ",
    "/proj/.orchestrator/runs/20260929-120000-a1b2c3/review-1.md; check that the Builder has ",
    "answered each of their findings.",
);

const GOLDEN_PR_BODY: &str = concat!(
    "Opened by Octoflow AI run `20260929-120000-a1b2c3`. Reviewer verdict after 2 rounds: ",
    "**APPROVE**.\n\n| Commit trailer | Value |\n| --- | --- |\n| Orchestrator-Run | ",
    "`20260929-120000-a1b2c3` |\n| Orchestrator-Workflow | `default` |\n| Orchestrator-Role | ",
    "`spec=claude/default` |\n| Orchestrator-Role | `build=claude/default` |\n| Orchestrator-Role | ",
    "`review=claude/default` |\n| Orchestrator-Gate | `ERROR after 2 quality rounds` |\n\n<details ",
    "open>\n<summary>Spec</summary>\n\n# Add a token-bucket rate limiter\n\n## Goal\nlimit requests\n\n",
    "</details>\n\n<details>\n<summary>Builder report (round 2)</summary>\n\nreport 2 q1\n\n</details>\n\n",
    "<details>\n<summary>Quality gate (round 2)</summary>\n\nGATE: ERROR\n\nJenkins build #44 judged ",
    "snapshot `snap4` (round 2, quality round 2 of 2) and ended UNSTABLE.\n\n`orchestrator.py gate` ",
    "judged the working tree.\n\n## Issues on changed lines\n\n1. `limiter.py:9` error mypy arg-type: ",
    "Argument 1 has incompatible type\n\n</details>\n\n<details>\n<summary>Review (round 2)</summary>\n\n",
    "**VERDICT: APPROVE**\n1. finding\n\n</details>\n\n<details>\n<summary>Run timings</summary>\n\n| step | ",
    "role | round | harness | model | start (UTC) | duration | file |\n| --- | --- | --- | --- | --- ",
    "| --- | --- | --- |\n| spec (human-paced) | spec | 0 | claude | default | 2026-10-03 04:00:00 | ",
    "0s | spec.md |\n| build | build | 1 | claude | default | 2026-10-03 04:00:00 | 0s | build-1.md |\n",
    "| quality | quality | 1 q1 | - | - | 2026-10-03 04:00:00 | 30s | quality-1-1.md |\n| build | ",
    "build | 1 q1 | claude | default | 2026-10-03 04:00:30 | 0s | build-1-q1.md |\n| quality | ",
    "quality | 1 q2 | - | - | 2026-10-03 04:00:30 | 30s | quality-1-2.md |\n| review | review | 1 | ",
    "claude | default | 2026-10-03 04:01:00 | 0s | review-1.md |\n| build | build | 2 | claude | ",
    "default | 2026-10-03 04:01:00 | 0s | build-2.md |\n| quality | quality | 2 q1 | - | - | ",
    "2026-10-03 04:01:00 | 30s | quality-2-1.md |\n| build | build | 2 q1 | claude | default | ",
    "2026-10-03 04:01:30 | 0s | build-2-q1.md |\n| quality | quality | 2 q2 | - | - | 2026-10-03 ",
    "04:01:30 | 30s | quality-2-2.md |\n| review | review | 2 | claude | default | 2026-10-03 ",
    "04:02:00 | 0s | review-2.md |\n\n</details>\n",
);

#[test]
fn fresh_builder_in_round_two() {
    let seen = seen();
    let mut state = saved_run("build", 2, FULL);
    state.prompted = Some("build-2.md".into());
    let script = vec![("build", vec![recording(build_turn(2), &seen)]), ("review", vec![review_turn(2, APPROVE)])];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, script, &["review"], &[]);
    f.workflow(f.as_saved()).run().unwrap();
    assert_eq!(*seen.borrow(), [FRESH_BUILD_2]);
}

#[test]
fn fresh_reviewer_in_round_two() {
    let seen = seen();
    let script = vec![("review", vec![recording(review_turn(2, APPROVE), &seen)])];
    let f = Fixture::resume(Rc::new(FakeHost::default()), saved_run("review", 2, FULL), script, &[], &[]);
    f.put(&f.state.build_path(2), "report 2");
    f.workflow(f.as_saved()).run().unwrap();
    assert_eq!(*seen.borrow(), [FRESH_REVIEW_2]);
}

#[test]
fn continue_after_a_resumed_session() {
    let seen = seen();
    let mut state = saved_run("build", 2, FULL);
    state.prompted = Some("build-2.md".into());
    let script = vec![("build", vec![recording(build_turn(2), &seen)]), ("review", vec![review_turn(2, APPROVE)])];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, script, &["review"], &[("build", "s-build")]);
    f.workflow(f.as_saved()).run().unwrap();
    assert_eq!(*seen.borrow(), [CONTINUE_BUILD_2]);
}

/// A run with the gate and two quality rounds a round, its Builder taking the turns in build and its Reviewer those in
/// review, run to APPROVE: its fixture, every prompt in order, and the (phase, round, quality round) state.json was
/// saved with, each once, in the order first saved.
fn two_gated_rounds(outcomes: Vec<Outcome>, build: Vec<Turn>, review: Vec<Turn>) -> (Fixture, Vec<String>, Vec<(String, i128, i128)>) {
    let seen = seen();
    let f = Fixture::new(vec![
        ("spec", vec![recording(spec_turn(), &seen)]),
        ("build", build.into_iter().map(|b| recording(b, &seen)).collect()),
        ("review", review.into_iter().map(|r| recording(r, &seen)).collect()),
    ])
    .gated();
    f.ci.state().outcomes = outcomes.into();
    let phases = Rc::new(std::cell::RefCell::new(Vec::new()));
    let saved = Rc::clone(&phases);
    f.host.state().before_write = Some(Box::new(move |path, text| {
        if path.ends_with("state.json") {
            let Ok(Json::Object(state)) = Json::parse(text) else { panic!("state.json is no object") };
            let int = |key: &str| state.get(key).and_then(Json::as_int).expect("a saved count");
            let phase = (text_of(&state, "phase"), int("round"), int("quality_round"));
            if !saved.borrow().contains(&phase) {
                saved.borrow_mut().push(phase);
            }
        }
        Ok(())
    }));
    assert_eq!(f.workflow(RunSettings { max_quality_rounds: 2, ..RunSettings::default() }).run(), verdict(APPROVE));
    let seen = seen.borrow().clone();
    let phases = phases.borrow().clone();
    (f, seen, phases)
}

fn text_of(saved: &Map<Json>, key: &str) -> String {
    saved.get(key).and_then(Json::as_str).expect("a saved text").to_string()
}

#[test]
fn gate_passes_after_a_fix_then_runs_out() {
    let (f, seen, phases) = two_gated_rounds(
        vec![red(), green(), red(), red2()],
        vec![build_turn(1), fix_turn(1, 1), build_turn(2), fix_turn(2, 1)],
        vec![review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)],
    );

    let golden = [SPEC_INTERVIEW, BUILD_1, QUALITY_FIX_1_1, REVIEW_1_PASSED, FIX_2, QUALITY_FIX_2_1, RECHECK_2_UNRESOLVED];
    assert_eq!(seen, golden);
    let expected: Vec<(String, i128, i128)> = [
        ("spec", 0, 0),
        ("build", 1, 0),
        ("quality", 1, 1),
        ("build", 1, 1),
        ("quality", 1, 2),
        ("review", 1, 2),
        ("build", 2, 0),
        ("quality", 2, 1),
        ("build", 2, 1),
        ("quality", 2, 2),
        ("review", 2, 2),
        ("publish", 2, 2),
        ("done", 2, 2),
    ]
    .iter()
    .map(|(p, n, q)| (p.to_string(), *n, *q))
    .collect();
    assert_eq!(phases, expected);
    let written: Vec<String> = f
        .host
        .state()
        .writes
        .iter()
        .filter(|p| p.starts_with(D) && !p.ends_with("state.json"))
        .map(|p| crate::py::text::basename(p).to_string())
        .collect();
    assert_eq!(written, [
        "spec.md",
        "build-1.md",
        "quality-1-1.md",
        "build-1-q1.md",
        "quality-1-2.md",
        "review-1.md",
        "build-2.md",
        "quality-2-1.md",
        "build-2-q1.md",
        "quality-2-2.md",
        "review-2.md",
        "summary.md",
        "metrics.json"
    ]);
    let layout: Vec<Call> = f
        .herdr
        .state()
        .calls
        .iter()
        .filter(|c| matches!(c, Call::Rename { .. } | Call::Split { .. }))
        .cloned()
        .collect();
    let rename = |pane: &str, label: &str| Call::Rename { pane: pane.into(), label: label.into() };
    assert_eq!(layout, [
        rename("w1:p1", "Spec Collector"),
        split("w1:p1", Direction::Right),
        rename("w1:p2", "Builder"),
        split("w1:p2", Direction::Down),
        rename("w1:p3", "Reviewer"),
    ]);
    let pr = f.host.state().prs[0].clone();
    assert_eq!(pr.title, "Add a token-bucket rate limiter");
    assert_eq!(pr.body, GOLDEN_PR_BODY);
}

#[test]
fn gate_runs_out_then_passes() {
    let (_, seen, _) = two_gated_rounds(
        vec![red(), red2(), green()],
        vec![build_turn(1), fix_turn(1, 1), build_turn(2)],
        vec![review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)],
    );
    assert_eq!(seen, [SPEC_INTERVIEW, BUILD_1, QUALITY_FIX_1_1, REVIEW_1_UNRESOLVED, FIX_2, RECHECK_2_PASSED]);
}

/// saved_run of the default workflow's three roles with the gate's job.
fn gated_run(phase: &str, rnd: u32) -> RunState {
    let mut state = saved_run(phase, rnd, FULL);
    state.quality_job = Some(crate::ci::fake::JOB.to_string());
    state
}

#[test]
fn fresh_builder_in_round_two_after_quality_rounds() {
    let seen = seen();
    let script = vec![("build", vec![recording(build_turn(2), &seen)]), ("review", vec![review_turn(2, APPROVE)])];
    let f = Fixture::resume_gated(Rc::new(FakeHost::default()), gated_run("build", 2), script, &["review"]);
    f.put(&f.state.quality_build_path(1, 1), "r");
    f.put(&f.state.quality_build_path(1, 2), "r");
    f.workflow(f.as_saved_gated()).run().unwrap();
    assert_eq!(*seen.borrow(), [FRESH_BUILD_2_AFTER_QUALITY_ROUNDS]);
}

#[test]
fn fresh_builder_answering_a_quality_round_in_round_two() {
    let seen = seen();
    let mut state = gated_run("build", 2);
    (state.quality_round, state.prompted) = (1, Some("build-2-q1.md".into()));
    let script = vec![("build", vec![recording(fix_turn(2, 1), &seen)]), ("review", vec![review_turn(2, APPROVE)])];
    let f = Fixture::resume_gated(Rc::new(FakeHost::default()), state, script, &["review"]);
    f.put(&f.state.quality_build_path(1, 1), "r");
    f.put(&f.state.build_path(2), "r");
    f.put(&f.state.quality_path(2, 1), "GATE: ERROR\n");
    f.workflow(f.as_saved_gated()).run().unwrap();
    assert_eq!(*seen.borrow(), [FRESH_QUALITY_FIX_2_1]);
}

#[test]
fn fresh_reviewer_in_round_two_with_the_gate() {
    let seen = seen();
    let mut state = gated_run("review", 2);
    state.quality_round = 1;
    let script = vec![("review", vec![recording(review_turn(2, APPROVE), &seen)])];
    let f = Fixture::resume_gated(Rc::new(FakeHost::default()), state, script, &[]);
    f.put(&f.state.build_path(2), "r");
    f.put(&f.state.quality_path(2, 1), "GATE: OK\n");
    f.workflow(f.as_saved_gated()).run().unwrap();
    assert_eq!(*seen.borrow(), [FRESH_REVIEW_2_PASSED]);
}

// -- TestWorkflowPromptsGolden: each step's prompts in the built-in workflow and in each file of examples/workflows/,
// as Workflow renders them for a new run on the fakes, held to the golden file tests/golden/prompts_<name>.txt that
// the Python engine's test holds them to: round 1, and for the steps the verdict step loops back over, round 2 as the
// running session and as a fresh one hears it. --------------------------------------------------------------------

/// The prompts of pipeline's steps as the golden file has them.
fn rendered(pipeline: &Pipeline) -> String {
    let f = Fixture::new(vec![]);
    let mut wf = f.workflow(RunSettings { pipeline: Some(pipeline.clone()), ..RunSettings::default() });
    wf.state.base = Some("abc123".into());
    let steps = pipeline.steps();
    let looped = match pipeline.verdict_step() {
        Some(verdict) => {
            let at = |id: &str| steps.iter().position(|st| st.id == id).unwrap();
            &steps[at(verdict.loop_to.as_deref().unwrap())..=at(&verdict.id)]
        }
        None => &[],
    };
    let mut parts = vec![];
    for step in steps {
        let (text, _) = wf.prompts(step, 1).unwrap();
        parts.push(format!("## {}, round 1\n\n{text}", step.id));
    }
    for step in looped {
        let (text, fresh) = wf.prompts(step, 2).unwrap();
        let fresh = fresh.unwrap_or_else(|| "None".into());
        parts.push(format!("## {}, round 2\n\n{text}", step.id));
        parts.push(format!("## {}, round 2, a fresh session\n\n{fresh}", step.id));
    }
    parts.join("\n\n") + "\n"
}

/// Fail unless text is tests/golden/<name>.txt, byte for byte: Python's assert_golden, which rewrites the file.
fn assert_golden(name: &str, text: &str) {
    let path = format!("{}/../tests/golden/{name}.txt", env!("CARGO_MANIFEST_DIR"));
    let want = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("no golden file {path}: {e}"));
    assert!(text == want, "{path} differs from the text written now:\n{text}");
}

#[test]
fn the_built_in_workflow() {
    assert_golden("prompts_default", &rendered(&DEFAULT_WORKFLOW));
}

#[test]
fn each_example_workflow() {
    let mut names: Vec<String> =
        std::fs::read_dir(EXAMPLES).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
    names.sort();
    // Not in Python's case: so that a wrong or empty EXAMPLES cannot pass it; a new example adds one here.
    assert_eq!(names.len(), 3);
    for name in names {
        let pipeline = load_workflow_file(&format!("{EXAMPLES}/{name}"), &Default::default()).unwrap();
        assert_golden(&format!("prompts_{}", name.strip_suffix(".toml").unwrap()), &rendered(&pipeline));
    }
}

// -- TestPromptFiles: prompt files in the config directory, in place of the built-in prompts. ------------------------

/// A configuration directory whose prompt files are files, each (name, text) written with the newline an editor
/// ends it with.
fn config_with(files: &[(&str, &str)]) -> (TempDir, ConfigDir) {
    let dir = TempDir::new();
    let config = ConfigDir::at(&dir.path());
    std::fs::create_dir_all(config.prompts_dir()).unwrap();
    for (name, text) in files {
        std::fs::write(format!("{}/{name}", config.prompts_dir()), format!("{text}\n")).unwrap();
    }
    (dir, config)
}

/// A new run's state and workflow as `run task` makes them with config's prompt files: new_state's.
fn started(config: &ConfigDir) -> (RunState, Pipeline) {
    let prompts = load_prompts(config).unwrap();
    let pipeline = find_workflow("default", &prompts).unwrap();
    let mut state = RunState::new(crate::workflow::fixture::RUN_ID, "task", "/proj");
    (state.phase, state.workflow) = (pipeline.steps()[0].id.clone(), pipeline.name().to_string());
    state.prompt_overrides = prompts.overrides();
    (state, pipeline)
}

fn no_pr_of(pipeline: Pipeline) -> RunSettings {
    RunSettings { pipeline: Some(pipeline), pull_request: false, ..RunSettings::default() }
}

#[test]
fn a_build_prompt_file_words_the_builders_first_turn() {
    let build = "Build {spec_path} in {cwd}; report to {build_path}.";
    let (_dir, config) = config_with(&[("BUILD_PROMPT.md", build)]);
    let (state, pipeline) = started(&config);
    let seen = seen();
    let f = Fixture::on(Rc::new(FakeHost::default()), state, vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![recording(build_turn(1), &seen)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    let mut wf = f.workflow(no_pr_of(pipeline));
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(*seen.borrow(), [format!("Build {D}/spec.md in /proj; report to {D}/build-1.md.{}", note())]);
    let overrides: Vec<(&str, &String)> = wf.state().prompt_overrides.iter().collect();
    assert_eq!(overrides, [("BUILD_PROMPT", &build.to_string())]);
    // Its name and the overrides give the workflow back, so the run saves no definition of it.
    assert_eq!(wf.state().workflow_definition, None);
}

#[test]
fn a_follow_up_file_reaches_the_turn_that_uses_it() {
    let (_dir, config) = config_with(&[("FIX_PROMPT.md", "Fix {prev_review_path}; report to {build_path}.")]);
    let (state, pipeline) = started(&config);
    let seen = seen();
    let f = Fixture::on(Rc::new(FakeHost::default()), state, vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1), recording(build_turn(2), &seen)]),
        ("review", vec![review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)]),
    ]);
    assert_eq!(f.workflow(no_pr_of(pipeline)).run(), verdict(APPROVE));
    assert_eq!(*seen.borrow(), [format!("Fix {D}/review-1.md; report to {D}/build-2.md.{}", note())]);
}

#[test]
fn a_note_file_reaches_the_turn_that_uses_it() {
    let (_dir, config) = config_with(&[("QUALITY_PASSED_NOTE.md", "Gate passed: {quality_path}.")]);
    let mut state = quality_state(1);
    state.prompt_overrides = load_prompts(&config).unwrap().overrides();
    let reviews = seen();
    let script = vec![("review", vec![recording(review_turn(1, APPROVE), &reviews)])];
    let f = Fixture::resume_gated(Rc::new(FakeHost::default()), state, script, &[]);
    f.put(&f.state.quality_path(1, 1), "GATE: OK\n");

    assert_eq!(f.workflow(f.as_saved_gated()).run(), verdict(APPROVE));
    let review = reviews.borrow()[0].clone();
    assert!(review.ends_with(&format!("\n\nGate passed: {D}/quality-1-1.md.")), "{review}");
    assert!(!review.contains("The quality gate passed"));
}

#[test]
fn a_note_file_reaches_every_workflow() {
    let (_dir, config) = config_with(&[("BLOCKED_NOTE.md", "Stuck? Write `BLOCKED: <question>` {{and stop}}.")]);
    let quick = quick();
    let mut state = new_run(&quick);
    state.prompt_overrides = load_prompts(&config).unwrap().overrides();
    let seen = seen();
    let f = Fixture::on(Rc::new(FakeHost::default()), state, vec![
        ("build", vec![recording(build_turn(1), &seen)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    assert_eq!(f.workflow(no_pr_of(quick)).run(), verdict(APPROVE));
    let expected = format!("Build add a rate limiter in /proj; report to {D}/build-1.md.\n\nStuck? Write `BLOCKED: <question>` {{and stop}}.");
    assert_eq!(*seen.borrow(), [expected]);
}

#[test]
fn a_resume_keeps_the_prompts_the_run_started_with() {
    for edit in ["edited", "deleted"] {
        let (_dir, config) = config_with(&[("FIX_PROMPT.md", "Fix {prev_review_path}; report to {build_path}.")]);
        let path = format!("{}/FIX_PROMPT.md", config.prompts_dir());
        let (state, pipeline) = started(&config);
        let f = Fixture::on(Rc::new(FakeHost::default()), state, vec![
            ("spec", vec![spec_turn()]),
            ("build", vec![build_turn(1)]),
        ]);
        f.play("review", vec![then_interrupted(&f.interrupt, idle())]);
        assert_eq!(f.workflow(no_pr_of(pipeline)).run(), Err(Stopped::Interrupted));
        if edit == "edited" {
            std::fs::write(&path, "Changed {prev_review_path}; report to {build_path}.\n").unwrap();
        } else {
            std::fs::remove_file(&path).unwrap();
        }
        let seen = seen();
        let saved = RunState::from_saved(&f.saved()).unwrap();
        let f2 = Fixture::resume(Rc::clone(&f.host), saved, vec![
            ("review", vec![review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)]),
            ("build", vec![recording(build_turn(2), &seen)]),
        ], &[], &[]);
        assert_eq!(f2.workflow(f2.as_saved()).run(), verdict(APPROVE), "{edit}");
        assert_eq!(seen.borrow().len(), 1, "{edit}");
        assert!(seen.borrow()[0].contains(&format!("Fix {D}/review-1.md; report to {D}/build-2.md.")), "{edit}");
        assert!(!seen.borrow()[0].contains("Changed"), "{edit}");
    }
}

#[test]
fn a_state_saved_before_prompt_files_resumes_with_the_built_in_texts() {
    let (_dir, _config) = config_with(&[("FIX_PROMPT.md", "Fix {prev_review_path}; report to {build_path}.")]);
    let Json::Object(mut saved) = saved_run("build", 2, FULL).to_json() else { unreachable!() };
    saved.remove("prompt_overrides");
    let state = RunState::from_saved(&saved).unwrap();
    let seen = seen();
    let script = vec![("build", vec![recording(build_turn(2), &seen)]), ("review", vec![review_turn(2, APPROVE)])];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, script, &[], &[]);
    f.put(&f.state.review_path(1), &format!("**VERDICT: {CHANGES_REQUESTED}**\n1. finding"));
    f.put(&f.state.build_path(1), "report 1");
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert!(wf.state().prompt_overrides.is_empty());
    let fix = |name: &str| match name {
        "prev_review_path" => Some(format!("{D}/review-1.md")),
        "build_path" => Some(format!("{D}/build-2.md")),
        _ => None,
    };
    let built_in = crate::py::format::format(FIX_PROMPT, &fix).unwrap();
    assert!(seen.borrow()[0].contains(&built_in), "{}", seen.borrow()[0]);
}
