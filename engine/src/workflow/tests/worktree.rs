//! TestWorktreeRun: run --worktree, a run in a git worktree of its own that leaves the project's checkout alone.
//! The worktree cases of TestOneRunPerCheckout and TestHarnessFlags are in one_run.rs and harnesses.rs, on
//! worktree_run.
//!
//! Not here: test_a_run_flag and test_flag_reaches_the_workflow, parse_args', new_state's and main's (rust-engine
//! ticket 09).

use std::cell::RefCell;
use std::collections::{BTreeSet, VecDeque};

use super::*;
use crate::pull_request::commit_note;

/// The worktree of a run --worktree run of the default run: Python's WORKTREE.
pub(super) const WORKTREE: &str = "/proj/.orchestrator/worktrees/a1b2c3";
/// The default run's branch, for its spec's title.
const RUN_BRANCH: &str = "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3";

/// build_turn, in the run's worktree.
fn worktree_build_turn(n: u32) -> Turn {
    Box::new(move |_, state, host| {
        host.write(&format!("{}/limiter.py", state.work_dir()), &format!("version {n}"), false).unwrap();
        host.write(&state.build_path(n), &format!("report {n}"), false).unwrap();
        "done".to_string()
    })
}

/// A new run --worktree run on host, its roles played by script, by default one the Reviewer approves in round 1,
/// with a Builder that writes in the worktree whatever the script says: Python's worktree_run.
pub(super) fn worktree_run(host: Rc<FakeHost>, script: Option<Script>) -> Fixture {
    let mut state = new_state();
    state.worktree = Some(WORKTREE.to_string());
    let mut script =
        script.unwrap_or_else(|| vec![("spec", vec![spec_turn()]), ("review", vec![review_turn(1, APPROVE)])]);
    script.retain(|(role, _)| *role != "build");
    script.push(("build", vec![worktree_build_turn(1)]));
    Fixture::on(host, state, script)
}

/// A worktree run that an earlier orchestrator took to phase in round rnd, with its branch.
fn saved_worktree_run(phase: &str, rnd: u32) -> RunState {
    let mut state = saved_run(phase, rnd, FULL);
    state.worktree = Some(WORKTREE.to_string());
    state.pull_request = true;
    state.base_branch = Some("main".into());
    state.branch = Some(RUN_BRANCH.into());
    state
}

/// The arguments of each git command run in cwd, in order.
fn git_in(host: &FakeHost, cwd: &str) -> Vec<Vec<String>> {
    host.state().git_log.iter().filter(|(dir, _)| dir == cwd).map(|(_, args)| args.clone()).collect()
}

/// Whether a git command of the host's starts with these arguments.
fn ran(host: &FakeHost, start: &[&str]) -> bool {
    host.state().git_calls.iter().any(|args| args.starts_with(&strings(start)))
}

#[test]
fn dirty_checkout_on_another_branch_is_left_alone() {
    let host = FakeHost::new(Some("abc123"), "feature");
    host.state().changed.insert("/proj/wip.py".into());
    // So the base is merged into the run's branch before the push.
    host.state().upstream = Some("u1".into());
    let f = worktree_run(Rc::new(host), None);
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let in_checkout = git_in(&f.host, "/proj");
    let touches = |a: &Vec<String>| {
        ["switch", "status", "add", "commit", "push"].contains(&a[0].as_str()) || a.starts_with(&strings(&["merge", "--ff-only"]))
    };
    assert!(!in_checkout.iter().any(touches), "{in_checkout:?}");
    let in_worktree = git_in(&f.host, WORKTREE);
    assert!(in_worktree.contains(&strings(&["switch", "-c", RUN_BRANCH, "origin/feature"])));
    assert!(in_worktree.contains(&strings(&["merge", "--no-edit", "--quiet", "origin/feature"])));
    assert!(in_worktree.contains(&strings(&["push", "--quiet", "--set-upstream", "origin", RUN_BRANCH])));
    let first: BTreeSet<&str> = in_worktree.iter().map(|a| a[0].as_str()).collect();
    assert!(["status", "add", "commit"].iter().all(|word| first.contains(word)), "{first:?}");
    let s = f.host.state();
    assert_eq!((s.branch.as_str(), s.changed.iter().map(String::as_str).collect::<Vec<_>>()), ("feature", vec!["/proj/wip.py"]));
    assert_eq!((s.prs[0].base.as_str(), s.prs[0].head.as_str()), ("feature", RUN_BRANCH));
}

#[test]
fn worktree_is_added_from_origin_before_the_workspace_opens() {
    let f = worktree_run(Rc::new(FakeHost::default()), None);
    let at_workspace = Rc::new(RefCell::new(vec![]));
    let (record, host, state_path) = (Rc::clone(&at_workspace), Rc::clone(&f.host), format!("{}/state.json", f.state.dir()));
    f.herdr.state().before_call = Some(Box::new(move |call| {
        if let Call::Workspace { .. } = call {
            let saved = Json::parse(host.state().files.get(&state_path).unwrap()).unwrap();
            let added = host.state().worktrees.contains_key(WORKTREE);
            record.borrow_mut().push((saved.get("worktree").cloned(), added));
        }
    }));
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    let steps: Vec<(String, Vec<String>)> = f
        .host
        .state()
        .git_log
        .iter()
        .filter(|(_, args)| args[0] == "fetch" || args[0] == "worktree")
        .cloned()
        .collect();
    assert_eq!(
        steps[..2],
        [
            ("/proj".to_string(), strings(&["fetch", "--quiet", "origin", "main"])),
            ("/proj".to_string(), strings(&["worktree", "add", "--detach", WORKTREE, "origin/main"])),
        ]
    );
    assert_eq!(*at_workspace.borrow(), [(Some(Json::from(WORKTREE)), true)]);
    assert_eq!(wf.state().base_branch.as_deref(), Some("main"));
}

#[test]
fn agents_work_in_the_worktree() {
    let seen = seen();
    let script = vec![("spec", vec![recording(spec_turn(), &seen)]), ("review", vec![recording(review_turn(1, APPROVE), &seen)])];
    let f = worktree_run(Rc::new(FakeHost::default()), Some(script));
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let cwds: Vec<(String, String)> =
        ["workspace", "split", "split"].iter().map(|kind| (kind.to_string(), WORKTREE.to_string())).collect();
    assert_eq!(f.herdr.state().cwds, cwds);
    assert!(seen.borrow()[0].contains(&format!("Read the code in {WORKTREE} first")));
    assert!(seen.borrow()[1].contains(&format!("`git diff abc123` in {WORKTREE},")));
}

#[test]
fn needs_a_repository_a_branch_and_an_origin() {
    // Each host the run is refused on, and why.
    type Case = (fn() -> FakeHost, &'static str);
    let cases: [Case; 3] = [
        (|| FakeHost::new(None, "main"), "/proj is not a git repository with a commit, which --worktree needs"),
        (|| FakeHost::new(Some("abc123"), "HEAD"), "/proj is on a detached HEAD"),
        (
            || {
                let host = FakeHost::default();
                host.state().has_origin = false;
                host
            },
            "/proj has no origin",
        ),
    ];
    for (host, message) in cases {
        for pull_request in [true, false] {
            let f = worktree_run(Rc::new(host()), None);
            let error = failure(f.workflow(RunSettings { pull_request, ..RunSettings::default() }).run());
            assert!(error.contains(message), "{error}");
            assert!(f.herdr.state().calls.is_empty());
            assert!(f.host.state().worktrees.is_empty());
        }
    }
}

#[test]
fn publishing_removes_the_worktree() {
    let f = worktree_run(Rc::new(FakeHost::default()), None);
    let removed = Rc::new(RefCell::new(vec![]));
    let (record, host, state_path) = (Rc::clone(&removed), Rc::clone(&f.host), format!("{}/state.json", f.state.dir()));
    f.host.state().before_git = Some(Box::new(move |cwd, args| {
        if args.starts_with(&["worktree", "remove"]) {
            let saved = Json::parse(host.state().files.get(&state_path).unwrap()).unwrap();
            let path = args.last().unwrap().to_string();
            record.borrow_mut().push((cwd.to_string(), path, saved.get("pr_url").map(Json::python_str)));
        }
    }));
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let url = "https://github.com/o/r/pull/7".to_string();
    assert_eq!(*removed.borrow(), [("/proj".to_string(), WORKTREE.to_string(), Some(url))]);
    assert!(f.host.state().git_log.contains(&("/proj".into(), strings(&["worktree", "remove", WORKTREE]))));
    assert!(f.host.state().worktrees.is_empty());
    assert!(!ran(&f.host, &["switch", "--quiet"]));
    assert_eq!(f.herdr.state().calls.last(), Some(&Call::Close { workspace: "w1".into() }));
}

#[test]
fn a_failed_removal_is_only_logged() {
    let host = FakeHost::default();
    host.state().remove_error =
        Some(format!("fatal: '{WORKTREE}' contains modified or untracked files, use --force to delete it"));
    let f = worktree_run(Rc::new(host), None);
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(wf.state().phase, "done");
    let log = f.told.lines().join("\n");
    assert!(log.contains(&format!("could not remove worktree {WORKTREE}: ")), "{log}");
    assert!(log.contains("use --force to delete it"), "{log}");
    assert!(f.host.state().worktrees.contains_key(WORKTREE));
}

#[test]
fn no_pr_keeps_the_worktree_with_the_change() {
    let f = worktree_run(Rc::new(FakeHost::default()), None);
    assert_eq!(f.workflow(no_pr()).run(), verdict(APPROVE));
    assert!(f.told.lines().contains(&format!("the change is in worktree {WORKTREE}")));
    let s = f.host.state();
    assert_eq!(s.worktrees.iter().map(|(p, b)| (p.to_string(), b.clone())).collect::<Vec<_>>(), [(WORKTREE.into(), "HEAD".into())]);
    assert_eq!(s.changed.iter().collect::<Vec<_>>(), [&format!("{WORKTREE}/limiter.py")]);
    drop(s);
    assert!(!ran(&f.host, &["switch"]) && !ran(&f.host, &["commit"]) && !ran(&f.host, &["push"]));
    assert!(!called(&f, &Call::Close { workspace: "w1".into() }));
}

#[test]
fn a_failed_run_keeps_the_worktree() {
    let f = worktree_run(Rc::new(FakeHost::default()), None);
    let nothing = writes(|s| s.build_path(1), "nothing to do", "done");
    f.herdr.state().script.insert("build".into(), VecDeque::from(vec![nothing]));
    let error = failure(f.workflow(RunSettings::default()).run());
    assert!(error.contains("changed no files"), "{error}");
    let worktrees = f.host.state().worktrees.clone();
    assert_eq!(worktrees.iter().map(|(p, b)| (p.to_string(), b.clone())).collect::<Vec<_>>(), [(WORKTREE.into(), RUN_BRANCH.into())]);
}

#[test]
fn resume_reuses_the_worktree() {
    let host = FakeHost::default();
    host.state().worktrees.insert(WORKTREE, RUN_BRANCH.to_string());
    host.state().changed.insert(format!("{WORKTREE}/limiter.py"));
    let f = Fixture::resume(Rc::new(host), saved_worktree_run("review", 1), vec![("review", vec![review_turn(1, APPROVE)])], &[], &[]);
    f.put(&f.state.build_path(1), "report 1");
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert!(!ran(&f.host, &["worktree", "add"]));
    let note = commit_note(wf.state());
    let commit = strings(&["commit", "--quiet", "-m", "add a rate limiter", "-m", &note]);
    let log = f.host.state().git_log.clone();
    assert!(log.iter().any(|(cwd, args)| cwd == WORKTREE && args.starts_with(&commit)), "{log:?}");
}

#[test]
fn resume_before_the_first_edit_reuses_the_worktree() {
    let host = FakeHost::default();
    host.state().worktrees.insert(WORKTREE, "HEAD".to_string());
    let mut state = saved_run("spec", 0, &["spec"]);
    (state.worktree, state.pull_request) = (Some(WORKTREE.into()), true);
    let script = vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![worktree_build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ];
    let f = Fixture::resume(Rc::new(host), state, script, &[], &[]);
    assert_eq!(f.workflow(f.as_saved()).run(), verdict(APPROVE));
    assert!(!ran(&f.host, &["worktree", "add"]));
}

#[test]
fn resume_refuses_a_worktree_that_is_gone() {
    for registered in [None, Some("/proj/.orchestrator/worktrees/ffffff")] {
        let host = FakeHost::default();
        if let Some(other) = registered {
            host.state().worktrees.insert(other, "HEAD".to_string());
        }
        let f = Fixture::resume(Rc::new(host), saved_worktree_run("review", 1), vec![("review", vec![])], &[], &[]);
        let error = failure(f.workflow(f.as_saved()).run());
        let expected = format!("the worktree of run {}, {WORKTREE}, is gone", f.state.run_id);
        assert!(error.contains(&expected), "{error}");
        assert!(f.herdr.state().calls.is_empty());
        assert!(!ran(&f.host, &["worktree", "add"]));
    }
}

#[test]
fn resume_with_a_pull_request_finishes_without_the_worktree() {
    let mut state = saved_worktree_run("publish", 1);
    (state.verdict, state.pr_url) = (Some(APPROVE.into()), Some("https://github.com/o/r/pull/7".into()));
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, vec![], &[], &[]);
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(wf.state().phase, "done");
    assert!(!ran(&f.host, &["worktree"]) && !ran(&f.host, &["switch"]));
    assert_eq!(f.herdr.state().calls.last(), Some(&Call::Close { workspace: "w1".into() }));
}

#[test]
fn a_state_saved_without_worktree_resumes_in_place() {
    let mut state = saved_run("review", 1, FULL);
    state.pull_request = true;
    (state.base_branch, state.branch) = (Some("main".into()), Some(RUN_BRANCH.into()));
    let Json::Object(mut saved) = state.to_json() else { unreachable!() };
    saved.remove("worktree");
    let state = RunState::from_saved(&saved).unwrap();
    assert_eq!((state.worktree.as_deref(), state.work_dir()), (None, "/proj"));
    let host = FakeHost::new(Some("abc123"), RUN_BRANCH);
    host.state().changed.insert("/proj/limiter.py".into());
    let f = Fixture::resume(Rc::new(host), state, vec![("review", vec![review_turn(1, APPROVE)])], &[], &[]);
    f.put(&f.state.build_path(1), "report 1");
    assert_eq!(f.workflow(f.as_saved()).run(), verdict(APPROVE));
    let s = f.host.state();
    assert_eq!(s.git_log.iter().map(|(cwd, _)| cwd.as_str()).collect::<BTreeSet<_>>(), BTreeSet::from(["/proj"]));
    assert_eq!(s.git_calls.last(), Some(&strings(&["switch", "--quiet", "main"])));
}
