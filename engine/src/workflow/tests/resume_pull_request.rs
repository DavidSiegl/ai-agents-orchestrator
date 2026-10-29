//! TestResumePullRequest: a resumed run that ends in a pull request keeps its base branch, finishes publishing where
//! an earlier orchestrator stopped, and refuses to go on while the checkout is on another branch than the run's.
//!
//! Not here: the quality phase of test_resume_on_another_branch_fails_before_any_turn, with the quality round
//! (rust-engine ticket 08, part 5), which also checks that Jenkins was asked nothing; test_cli_exits_1_on_another_branch
//! and test_no_pr_is_saved_with_the_run, which drive main and parse_args (ticket 09).

use super::pull_request::{args, git_calls, BRANCH, FAST_FORWARD, FETCH, MERGE};
use super::*;

const ON_BRANCH: &[&str] = &["rev-parse", "--abbrev-ref", "HEAD"];
/// The git commands that would change the tree, the branch or origin.
const MUTATING: &[&str] = &["add", "commit", "merge", "push", "switch"];

/// A saved run of the default workflow in phase and round 1, ending in a pull request from BRANCH into main.
fn pr_run(phase: &str) -> RunState {
    let mut state = saved_run(phase, 1, FULL);
    state.pull_request = true;
    state.base_branch = Some("main".into());
    state.branch = Some(BRANCH.into());
    state
}

/// pr_run in phase publish, approved.
fn approved_publish() -> RunState {
    let mut state = pr_run("publish");
    state.verdict = Some(APPROVE.into());
    state
}

/// A host whose /proj is at head, on branch.
fn host_on(head: &str, branch: &str) -> Rc<FakeHost> {
    Rc::new(FakeHost::new(Some(head), branch))
}

fn count(calls: &[Vec<String>], call: &[&str]) -> usize {
    calls.iter().filter(|c| **c == args(call)).count()
}

/// The run fails naming both branches, state.json records it as resumable, and nothing in git_calls changed the
/// tree, the branch or origin. The state saved.
fn assert_refused(f: &Fixture, current: &str) -> Map<Json> {
    let message = failure(f.workflow(f.as_saved()).run());
    assert_eq!(
        message,
        format!("/proj is on {current}, not {BRANCH}, which holds this run's change; check out {BRANCH} and resume")
    );
    let saved = f.saved();
    assert_eq!((text(&saved, "error"), saved.get("owner")), (Some(message), Some(&Json::Null)));
    assert!(!git_calls(f).iter().any(|c| MUTATING.contains(&c[0].as_str())), "{:?}", git_calls(f));
    assert!(f.host.state().prs.is_empty());
    saved
}

#[test]
fn run_saved_before_pull_requests_opens_none() {
    let mut saved = saved_run("review", 1, FULL).to_json().as_object().unwrap().clone();
    saved.remove("pull_request");
    let state = RunState::from_saved(&saved).unwrap();
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, vec![("review", vec![review_turn(1, APPROVE)])], &[], &[]);
    assert_eq!(f.workflow(f.as_saved()).run(), verdict(APPROVE));
    assert!(f.host.state().prs.is_empty());
    assert!(!called(&f, &Call::Close { workspace: "w1".into() }));
}

#[test]
fn resumed_review_ends_in_a_pull_request() {
    let host = host_on("abc123", BRANCH);
    host.state().changed.insert("/proj/limiter.py".into());
    let f = Fixture::resume(host, pr_run("review"), vec![("review", vec![review_turn(1, APPROVE)])], &[], &[]);
    assert_eq!(f.workflow(f.as_saved()).run(), verdict(APPROVE));
    let host = f.host.state();
    let prs: Vec<(&str, &str)> = host.prs.iter().map(|p| (p.base.as_str(), p.head.as_str())).collect();
    assert_eq!(prs, [("main", BRANCH)]);
    assert_eq!(host.branch, "main");
}

#[test]
fn spec_resumed_after_switching_keeps_the_base_branch() {
    // The earlier orchestrator stopped between `git switch -c` and saving the build phase.
    let mut state = saved_run("spec", 0, &["spec"]);
    state.pull_request = true;
    state.base_branch = Some("main".into());
    let f = Fixture::resume(host_on("abc123", BRANCH), state, vec![
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ], &[], &[]);
    f.put(&f.state.spec_path(), "# Add a token-bucket rate limiter\n");
    assert_eq!(f.workflow(f.as_saved()).run(), verdict(APPROVE));
    let calls = git_calls(&f);
    assert_eq!(count(&calls, &["switch", "-c", BRANCH]), 0);
    assert_eq!(f.host.state().prs[0].base, "main");
    // Neither before the interview nor before branching: the only fetch is the one at publish.
    assert_eq!(count(&calls, FAST_FORWARD), 0);
    assert_eq!(count(&calls, FETCH), 1);
}

#[test]
fn publish_resumed_after_the_commit_does_not_commit_again() {
    let f = Fixture::resume(host_on("def456", BRANCH), approved_publish(), vec![], &[], &[]);
    assert_eq!(f.workflow(f.as_saved()).run(), verdict(APPROVE));
    let calls = git_calls(&f);
    assert!(!calls.iter().any(|c| c[0] == "commit"));
    assert_eq!(count(&calls, &["push", "--quiet", "--set-upstream", "origin", BRANCH]), 1);
    assert_eq!(f.host.state().prs.len(), 1);
    assert!(!f.herdr.state().calls.iter().any(|c| matches!(c, Call::Workspace { .. })));
    // Whether HEAD is the Builder's commit or the merge commit after it, origin's base is in it.
    assert_eq!(count(&calls, MERGE), 0);
}

#[test]
fn publish_resumed_mid_merge_aborts_it_before_committing() {
    let host = host_on("def456", BRANCH);
    {
        let mut h = host.state();
        (h.upstream, h.conflicts, h.merge_head) = (Some("u3".into()), vec!["limiter.py".into()], true);
        // The conflict markers.
        h.changed = ["/proj/limiter.py".to_string()].into();
    }
    let f = Fixture::resume(host, approved_publish(), vec![], &[], &[]);
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    let calls = git_calls(&f);
    // The branch is checked at the start and again in publish, before anything touches the tree.
    assert_eq!(calls[..5], [
        args(ON_BRANCH),
        args(ON_BRANCH),
        args(&["rev-parse", "-q", "--verify", "MERGE_HEAD"]),
        args(&["merge", "--abort"]),
        args(&["status", "--porcelain"]),
    ]);
    assert!(!calls.iter().any(|c| c[0] == "add" || c[0] == "commit"));
    // The merge is tried again, conflicts again, and is aborted again.
    assert_eq!(count(&calls, &["merge", "--abort"]), 2);
    assert!(f.host.state().prs[0].draft);
    assert_eq!(wf.state().conflicts, ["limiter.py"]);
}

#[test]
fn publish_resumed_after_the_pull_request_only_switches_back() {
    let mut state = approved_publish();
    state.pr_url = Some("https://github.com/o/r/pull/7".into());
    let f = Fixture::resume(host_on("abc123", BRANCH), state, vec![], &[], &[]);
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert!(f.host.state().prs.is_empty());
    assert_eq!(git_calls(&f), [args(&["switch", "--quiet", "main"])]);
    assert_eq!(wf.state().phase, "done");
}

#[test]
fn resume_on_another_branch_fails_before_any_turn() {
    // HEAD: detached.
    for current in ["main", "HEAD"] {
        for phase in ["build", "review"] {
            let host = host_on("abc123", current);
            host.state().changed.insert("/proj/limiter.py".into());
            let f = Fixture::resume(host, pr_run(phase), vec![
                ("build", vec![build_turn(1)]),
                ("review", vec![review_turn(1, APPROVE)]),
            ], &[], &[]);
            let saved = assert_refused(&f, current);
            assert_eq!(text(&saved, "phase").as_deref(), Some(phase));
            assert_eq!(f.herdr.state().calls, []);
            assert_eq!(git_calls(&f), [args(ON_BRANCH)]);
        }
    }
}

#[test]
fn publish_resumed_on_another_branch_touches_nothing() {
    let host = host_on("def456", "main");
    {
        let mut h = host.state();
        h.changed = ["/proj/limiter.py".to_string()].into();
        h.merge_head = true;
    }
    let f = Fixture::resume(host, approved_publish(), vec![], &[], &[]);
    assert_refused(&f, "main");
    assert_eq!(git_calls(&f), [args(ON_BRANCH)]);
    assert!(f.host.state().merge_head);
}

#[test]
fn branch_switched_during_a_turn_is_refused_before_the_commit() {
    /// turn, the human checking out main while it runs.
    fn switching(mut turn: Turn) -> Turn {
        Box::new(move |prompt, state, host| {
            host.state().branch = "main".into();
            // From here on, git_calls holds only what ran after the switch.
            host.state().git_calls.clear();
            turn(prompt, state, host)
        })
    }
    for role in ["build", "review"] {
        let mut script = approved();
        for (r, turns) in &mut script {
            if *r == role {
                let turn = turns.pop().unwrap();
                turns.push(switching(turn));
            }
        }
        let f = Fixture::new(script);
        let saved = {
            let message = failure(f.workflow(RunSettings::default()).run());
            assert_eq!(
                message,
                format!("/proj is on main, not {BRANCH}, which holds this run's change; check out {BRANCH} and resume")
            );
            let saved = f.saved();
            assert_eq!((text(&saved, "error"), saved.get("owner")), (Some(message), Some(&Json::Null)));
            assert!(f.host.state().prs.is_empty());
            saved
        };
        assert_eq!(text(&saved, "phase").as_deref(), Some("publish"), "{role}");
        assert_eq!(git_calls(&f), [args(ON_BRANCH)], "{role}");

        f.host.state().branch = BRANCH.into();
        let f2 = Fixture::resume(Rc::clone(&f.host), RunState::from_saved(&saved).unwrap(), vec![], &[], &[]);
        let mut wf2 = f2.workflow(f2.as_saved());
        assert_eq!(wf2.run(), verdict(APPROVE), "{role}");
        let host = f.host.state();
        let prs: Vec<(&str, &str)> = host.prs.iter().map(|p| (p.base.as_str(), p.head.as_str())).collect();
        assert_eq!(prs, [("main", BRANCH)]);
        assert_eq!(host.git_calls.iter().filter(|c| c[0] == "commit").count(), 1);
        assert_eq!(wf2.state().error, None);
    }
}

#[test]
fn runs_without_a_branch_to_guard_are_not_checked() {
    let mut published = approved_publish();
    published.pr_url = Some("https://github.com/o/r/pull/7".into());
    let cases: Vec<(&str, RunState, Script)> = vec![
        ("no-pr", saved_run("build", 1, FULL), vec![
            ("build", vec![build_turn(1)]),
            ("review", vec![review_turn(1, APPROVE)]),
        ]),
        ("pr_url set", published, vec![]),
    ];
    for (name, state, script) in cases {
        let f = Fixture::resume(host_on("abc123", "feature"), state, script, &[], &[]);
        assert_eq!(f.workflow(f.as_saved()).run(), verdict(APPROVE), "{name}");
        assert_eq!(count(&git_calls(&f), ON_BRANCH), 0, "{name}");
    }
}
