//! TestPullRequest and TestUpToDateBase: the run's branch, its commit, the merge of the moved base and the pull
//! request.

use std::cell::RefCell;

use super::*;

/// The default run with one review round per verdict, each Builder turn changing /proj/limiter.py.
fn rounds(verdicts: &[&str]) -> Script {
    let n = verdicts.len() as u32;
    let reviews = verdicts.iter().zip(1..).map(|(v, i)| review_turn(i, v)).collect();
    vec![("spec", vec![spec_turn()]), ("build", (1..=n).map(build_turn).collect()), ("review", reviews)]
}

pub(super) const BRANCH: &str = "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3";

pub(super) fn args(a: &[&str]) -> Vec<String> {
    a.iter().map(|s| s.to_string()).collect()
}

pub(super) fn git_calls(f: &Fixture) -> Vec<Vec<String>> {
    f.host.state().git_calls.clone()
}

fn position(calls: &[Vec<String>], call: &[&str]) -> usize {
    calls.iter().position(|c| *c == args(call)).unwrap_or_else(|| panic!("no git {call:?} in {calls:?}"))
}

#[test]
fn approved_change_becomes_a_pull_request() {
    let f = Fixture::new(rounds(&[APPROVE]));
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let calls = git_calls(&f);
    assert!(calls.contains(&args(&["switch", "-c", BRANCH])));
    let commit = calls.iter().find(|c| c[0] == "commit").unwrap();
    let m = commit.iter().position(|a| a == "-m").unwrap();
    assert_eq!(commit[m + 1], "Add a token-bucket rate limiter");
    assert!(calls.contains(&args(&["push", "--quiet", "--set-upstream", "origin", BRANCH])));
    let host = f.host.state();
    let pr = &host.prs[0];
    assert_eq!((pr.base.as_str(), pr.head.as_str(), pr.draft), ("main", BRANCH, false));
    assert!(pr.body.contains("## Goal"));
    assert_eq!(host.branch, "main");
    drop(host);
    assert_eq!(f.herdr.state().calls.last(), Some(&Call::Close { workspace: "w1".into() }));
    let saved = f.saved();
    assert_eq!(
        (text(&saved, "phase"), text(&saved, "pr_url")),
        (Some("done".into()), Some("https://github.com/o/r/pull/7".into()))
    );
}

#[test]
fn builder_works_on_the_branch() {
    let branches = Rc::new(RefCell::new(vec![]));
    let seen = Rc::clone(&branches);
    let mut build = build_turn(1);
    let on_branch: Turn = Box::new(move |prompt, state, host| {
        let branch = host.state().branch.clone();
        seen.borrow_mut().push(branch);
        build(prompt, state, host)
    });
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![on_branch]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    f.workflow(RunSettings::default()).run().unwrap();
    assert_eq!(*branches.borrow(), [BRANCH]);
}

#[test]
fn changes_still_requested_opens_a_draft() {
    let f = Fixture::new(rounds(&[CHANGES_REQUESTED, CHANGES_REQUESTED]));
    assert_eq!(f.workflow(max_rounds(2)).run(), verdict(CHANGES_REQUESTED));
    let pr = f.host.state().prs[0].clone();
    assert!(pr.draft);
    assert!(pr.body.contains("<details open>\n<summary>Review (round 2)</summary>"));
    assert!(f.herdr.state().calls.contains(&Call::Close { workspace: "w1".into() }));
}

#[test]
fn dirty_tree_fails_before_the_interview() {
    let host = FakeHost::default();
    host.state().changed.insert("/proj/wip.py".into());
    let f = Fixture::on(Rc::new(host), new_state(), vec![("spec", vec![])]);
    let message = failure(f.workflow(RunSettings::default()).run());
    assert!(message.contains("uncommitted changes"), "{message}");
    assert!(f.herdr.state().calls.is_empty());
}

#[test]
fn tree_touched_during_interview_fails_before_branching() {
    let mut spec = spec_turn();
    let spec_and_wip: Turn = Box::new(move |prompt, state, host| {
        host.write("/proj/wip.py", "x", false).unwrap();
        spec(prompt, state, host)
    });
    let f = Fixture::new(vec![("spec", vec![spec_and_wip])]);
    let message = failure(f.workflow(RunSettings::default()).run());
    assert!(message.contains("uncommitted changes"), "{message}");
    assert_eq!(f.host.state().branch, "main");
}

#[test]
fn detached_head_is_refused() {
    let f = Fixture::on(Rc::new(FakeHost::new(Some("abc123"), "HEAD")), new_state(), vec![("spec", vec![])]);
    assert!(failure(f.workflow(RunSettings::default()).run()).contains("detached HEAD"));
}

#[test]
fn not_a_git_repo_needs_no_pr() {
    let f = Fixture::on(Rc::new(FakeHost::new(None, "main")), new_state(), vec![("spec", vec![])]);
    assert!(failure(f.workflow(RunSettings::default()).run()).contains("--no-pr"));
}

#[test]
fn no_changes_keeps_the_workspace_open() {
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![writes(|s| s.build_path(1), "nothing to do", "done")]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    assert!(failure(f.workflow(RunSettings::default()).run()).contains("changed no files"));
    assert!(f.host.state().prs.is_empty());
    assert!(!f.herdr.state().calls.contains(&Call::Close { workspace: "w1".into() }));
}

#[test]
fn close_failure_does_not_fail_the_run() {
    let f = Fixture::new(rounds(&[APPROVE]));
    f.herdr.state().close_error = Some(crate::herdr::HerdrError::new("workspace_not_found", "gone"));
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert!(f.told.lines().contains(&"could not close herdr workspace w1: herdr workspace_not_found: gone".into()));
}

#[test]
fn no_pr_leaves_changes_and_workspace() {
    let f = Fixture::new(rounds(&[APPROVE]));
    assert_eq!(f.workflow(no_pr()).run(), verdict(APPROVE));
    let host = f.host.state();
    assert!(host.git_calls.is_empty());
    assert!(host.prs.is_empty());
    assert_eq!(host.changed.iter().cloned().collect::<Vec<_>>(), ["/proj/limiter.py"]);
    drop(host);
    assert!(!f.herdr.state().calls.contains(&Call::Close { workspace: "w1".into() }));
}

#[test]
fn no_pr_neither_fetches_nor_merges_even_when_origin_moved() {
    let host = FakeHost::default();
    host.state().upstream = Some("u1".into());
    let f = Fixture::on(Rc::new(host), new_state(), rounds(&[APPROVE]));
    let mut wf = f.workflow(no_pr());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert!(f.host.state().git_calls.is_empty());
    assert_eq!(wf.state().base.as_deref(), Some("abc123"));
}

// -- TestUpToDateBase: the base branch is brought up to origin's before the interview and the branch, and merged in
// before the push. ------------------------------------------------------------------------------------------------

pub(super) const FETCH: &[&str] = &["fetch", "--quiet", "origin", "main"];
pub(super) const FAST_FORWARD: &[&str] = &["merge", "--ff-only", "--quiet", "origin/main"];
pub(super) const MERGE: &[&str] = &["merge", "--no-edit", "--quiet", "origin/main"];

/// The default run approved, or ending with verdict, its turns replaced where given.
fn script(spec: Option<Turn>, build: Option<Turn>, review: Option<Turn>, verdict: &str) -> Script {
    vec![
        ("spec", vec![spec.unwrap_or_else(spec_turn)]),
        ("build", vec![build.unwrap_or_else(|| build_turn(1))]),
        ("review", vec![review.unwrap_or_else(|| review_turn(1, verdict))]),
    ]
}

/// turn, after changing the host as change does.
fn after(change: impl Fn(&mut crate::host::fake::FakeState) + 'static, mut turn: Turn) -> Turn {
    Box::new(move |prompt, state, host| {
        change(&mut host.state());
        turn(prompt, state, host)
    })
}

#[test]
fn base_is_fast_forwarded_before_the_interview() {
    let host = FakeHost::default();
    host.state().upstream = Some("u1".into());
    let at_interview = Rc::new(RefCell::new(vec![]));
    let seen = Rc::clone(&at_interview);
    let mut spec = spec_turn();
    let watching: Turn = Box::new(move |prompt, state, h| {
        let s = h.state();
        seen.borrow_mut().push((s.git_calls.clone(), s.head.clone()));
        drop(s);
        spec(prompt, state, h)
    });
    let f = Fixture::on(Rc::new(host), new_state(), script(Some(watching), None, None, APPROVE));
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let (calls, head) = at_interview.borrow()[0].clone();
    assert_eq!(calls[position(&calls, FETCH) + 1], args(FAST_FORWARD));
    assert_eq!(head.as_deref(), Some("u1"));
}

#[test]
fn fetch_failure_stops_before_the_interview() {
    let host = FakeHost::default();
    host.state().fetch_error = Some("fatal: couldn't find remote ref main".into());
    let f = Fixture::on(Rc::new(host), new_state(), vec![("spec", vec![])]);
    let message = failure(f.workflow(RunSettings::default()).run());
    assert!(
        message.contains(
            "could not fetch main from origin: git -C /proj fetch --quiet origin main failed: fatal: couldn't find"
        ),
        "{message}"
    );
    assert!(f.herdr.state().calls.is_empty());
    assert!(text(&f.saved(), "error").unwrap().contains("could not fetch main"));
}

#[test]
fn diverged_base_stops_before_the_interview() {
    let host = FakeHost::default();
    host.state().diverged = true;
    let f = Fixture::on(Rc::new(host), new_state(), vec![("spec", vec![])]);
    let message = failure(f.workflow(RunSettings::default()).run());
    assert!(
        message.contains(
            "main has diverged from origin/main; reconcile it first. git merge --ff-only --quiet origin/main failed"
        ),
        "{message}"
    );
    assert!(f.herdr.state().calls.is_empty());
}

#[test]
fn base_is_fast_forwarded_again_before_branching() {
    let seen = seen();
    let spec = after(|h| h.upstream = Some("u2".into()), spec_turn());
    let f = Fixture::new(script(Some(spec), None, Some(recording(review_turn(1, APPROVE), &seen)), APPROVE));
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    let calls = git_calls(&f);
    let switch = position(&calls, &["switch", "-c", BRANCH]);
    assert_eq!(calls[switch - 2..switch], [args(FETCH), args(FAST_FORWARD)]);
    assert_eq!(wf.state().base.as_deref(), Some("u2"));
    assert!(seen.borrow()[0].contains("git diff u2"));
}

#[test]
fn switching_away_from_the_base_during_the_interview_is_refused() {
    let spec = after(|h| h.branch = "wip".into(), spec_turn());
    let f = Fixture::new(script(Some(spec), None, None, APPROVE));
    assert!(failure(f.workflow(RunSettings::default()).run()).contains("is on wip, not main"));
    assert!(!git_calls(&f).contains(&args(&["switch", "-c", BRANCH])));
}

#[test]
fn moved_base_is_merged_before_the_push() {
    let build = after(|h| h.upstream = Some("u3".into()), build_turn(1));
    let f = Fixture::new(script(None, Some(build), None, APPROVE));
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let calls = git_calls(&f);
    let commit = calls.iter().position(|c| c[0] == "commit").unwrap();
    let push = position(&calls, &["push", "--quiet", "--set-upstream", "origin", BRANCH]);
    assert!(commit < position(&calls, MERGE));
    assert!(position(&calls, MERGE) < push);
    let host = f.host.state();
    assert_eq!(host.head.as_deref(), Some("merge-u3"));
    assert!(!host.prs[0].draft);
    assert!(!host.prs[0].body.starts_with("> [!WARNING]"));
    drop(host);
    assert_eq!(f.saved().get("conflicts"), Some(&Json::Array(vec![])));
    assert!(!f.told.titles().iter().any(|t| t.contains("conflicts")));
}

#[test]
fn base_already_merged_is_not_merged_again() {
    let f = Fixture::new(script(None, None, None, APPROVE));
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let calls = git_calls(&f);
    let push = position(&calls, &["push", "--quiet", "--set-upstream", "origin", BRANCH]);
    // Then the branch's commit is read, for the secret scan before the push.
    let branch_ref = format!("refs/heads/{BRANCH}");
    let before = [
        args(FETCH),
        args(&["merge-base", "--is-ancestor", "origin/main", "HEAD"]),
        args(&["rev-parse", "--verify", &branch_ref]),
    ];
    assert_eq!(calls[push - 3..push], before);
    assert!(!calls.contains(&args(MERGE)));
}

/// A run whose base conflicts with the Builder's change when it publishes, ending with verdict in round 1; the
/// fixture and the run's state at its end.
fn conflicting(verdict_word: &str) -> (Fixture, RunState) {
    let build = after(
        |h| {
            h.upstream = Some("u3".into());
            h.conflicts = vec!["limiter.py".into(), "docs/api.md".into()];
        },
        build_turn(1),
    );
    let f = Fixture::new(script(None, Some(build), None, verdict_word));
    let state = {
        let mut wf = f.workflow(max_rounds(1));
        assert_eq!(wf.run(), verdict(verdict_word));
        wf.state().clone()
    };
    (f, state)
}

#[test]
fn conflict_is_aborted_and_opens_a_draft_that_says_so() {
    let (f, _) = conflicting(APPROVE);
    let calls = git_calls(&f);
    let push = position(&calls, &["push", "--quiet", "--set-upstream", "origin", BRANCH]);
    assert!(position(&calls, MERGE) < position(&calls, &["merge", "--abort"]));
    assert!(position(&calls, &["merge", "--abort"]) < push);
    let host = f.host.state();
    // Pushed without the merge.
    assert_eq!(host.head.as_deref(), Some("abc123+commit"));
    assert!(!host.merge_head);
    let pr = &host.prs[0];
    assert!(pr.draft);
    let warning = pr.body.split("\n\n").next().unwrap();
    assert!(warning.starts_with("> [!WARNING]"));
    for name in ["`main`", "`limiter.py`", "`docs/api.md`"] {
        assert!(warning.contains(name), "{warning}");
    }
    drop(host);
    let conflicts = Json::Array(vec![Json::from("limiter.py"), Json::from("docs/api.md")]);
    assert_eq!(f.saved().get("conflicts"), Some(&conflicts));
    assert!(f.told.titles().contains(&"Pull request conflicts with main".into()));
}

#[test]
fn conflict_with_changes_requested_is_a_draft_too() {
    let (f, state) = conflicting(CHANGES_REQUESTED);
    assert!(f.host.state().prs[0].draft);
    assert_eq!(state.conflicts, ["limiter.py", "docs/api.md"]);
}

#[test]
fn fetch_failure_at_publish_stops_and_resume_retries() {
    let offline = |h: &mut crate::host::fake::FakeState| {
        h.fetch_error = Some("fatal: unable to access 'https://github.com/o/r/': Could not resolve host".into());
    };
    let f = Fixture::new(script(None, Some(after(offline, build_turn(1))), None, APPROVE));
    assert!(failure(f.workflow(max_rounds(1)).run()).contains("could not fetch main from origin"));
    assert!(f.host.state().prs.is_empty());
    assert!(!git_calls(&f).iter().any(|c| c[0] == "push"));
    let saved = f.saved();
    assert_eq!(text(&saved, "phase").as_deref(), Some("publish"));

    f.host.state().fetch_error = None;
    let state = RunState::from_saved(&saved).unwrap();
    let pull_request = state.pull_request;
    let f2 = Fixture::resume(Rc::clone(&f.host), state, vec![], &[], &[]);
    assert_eq!(f2.workflow(RunSettings { pull_request, ..RunSettings::default() }).run(), verdict(APPROVE));
    assert_eq!(f.host.state().prs.len(), 1);
    assert_eq!(git_calls(&f).iter().filter(|c| c[0] == "commit").count(), 1);
}
