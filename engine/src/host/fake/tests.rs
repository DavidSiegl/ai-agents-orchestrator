//! FakeHost plays what the Workflow tests of ticket 08 will ask of it as tests/test_orchestrator.py's FakeHost does,
//! with Host's own steps running on its git.

use super::*;
use crate::host::{GhError, PullRequest, PushError};

fn proj(name: &str) -> String {
    format!("/proj/{name}")
}

#[test]
fn files_written_outside_the_run_directory_are_changes() {
    let host = FakeHost::default();
    host.write(&proj("app.py"), "x", false).unwrap();
    host.write(&proj(".orchestrator/runs/r/state.json"), "{}", true).unwrap();
    host.write(&proj(&format!("{WORKTREES_DIR}/a1/f.py")), "y", false).unwrap();
    let s = host.state();
    assert_eq!(s.writes.len(), 3);
    assert_eq!(s.mtime_kept, [proj(".orchestrator/runs/r/state.json")]);
    let changed: Vec<&String> = s.changed.iter().collect();
    assert_eq!(changed, [&proj(&format!("{WORKTREES_DIR}/a1/f.py")), &proj("app.py")]);
    drop(s);
    assert_eq!(host.git(&proj(""), &["status", "--porcelain"]).unwrap().lines().count(), 2);
    host.git("/proj", &["commit", "--quiet", "-m", "x"]).unwrap();
    assert!(host.state().changed.is_empty());
    assert_eq!(host.git_head("/proj").unwrap().as_deref(), Some("abc123+commit"));
}

#[test]
fn the_host_s_clock_is_far_from_the_workflow_s() {
    let host = FakeHost::default();
    assert_eq!(host.now().unwrap(), HOST_EPOCH);
    host.state().clock = Some(Rc::new(|| 42.9));
    assert_eq!(host.now().unwrap(), HOST_EPOCH + 42);
    host.write("/proj/review-1.md", "x", false).unwrap();
    host.state().clock = Some(Rc::new(|| 50.0));
    assert_eq!(host.mtime("/proj/review-1.md").unwrap(), HOST_EPOCH + 42);
    assert_eq!(host.mtime("/proj/never-written.md").unwrap(), HOST_EPOCH + 50);
    host.rename("/proj/review-1.md", "/proj/review-1.rejected.md").unwrap();
    assert_eq!(host.read("/proj/review-1.md").unwrap(), None);
    assert_eq!(host.mtime("/proj/review-1.rejected.md").unwrap(), HOST_EPOCH + 42);
    host.delete("/proj/review-1.rejected.md");
    assert!(host.state().changed.contains("/proj/review-1.rejected.md"));
}

#[test]
fn host_s_fetch_and_fast_forward_run_on_its_git() {
    let host = FakeHost::default();
    host.state().upstream = Some("def456".into());
    host.fast_forward("/proj", "main").unwrap();
    assert_eq!(host.git_head("/proj").unwrap().as_deref(), Some("def456"));
    assert_eq!(host.state().git_calls[0], ["fetch", "--quiet", "origin", "main"]);
    host.state().diverged = true;
    let e = host.fast_forward("/proj", "main").unwrap_err();
    assert!(e.0.starts_with("main has diverged from origin/main; reconcile it first."), "{e}");
    host.state().fetch_error = Some("fatal: offline".into());
    let e = host.fetch("/proj", "main").unwrap_err();
    assert_eq!(e.0, "could not fetch main from origin: git -C /proj fetch --quiet origin main failed: fatal: offline");
}

#[test]
fn a_conflicting_merge_is_aborted_on_the_fake_too() {
    let host = FakeHost::default();
    {
        let mut s = host.state();
        s.upstream = Some("def456".into());
        s.conflicts = vec!["app.py".into()];
    }
    assert_eq!(host.merge_upstream("/proj", "main").unwrap(), ["app.py"]);
    let s = host.state();
    assert!(!s.merge_head && s.changed.is_empty());
    drop(s);
    host.state().conflicts.clear();
    assert!(host.merge_upstream("/proj", "main").unwrap().is_empty());
    assert_eq!(host.git_head("/proj").unwrap().as_deref(), Some("merge-def456"));
    assert!(host.merge_upstream("/proj", "main").unwrap().is_empty());
    host.state().merge_head = true;
    assert!(host.abort_merge("/proj").unwrap());
}

#[test]
fn pushes_scan_first_and_land_on_its_origin() {
    let host = FakeHost::default();
    host.push_ref("/proj", "snap1", "orchestrator-ci/a1-1-q1", "abc123", "/r/leaks.json").unwrap();
    assert_eq!(host.remote_branches("/proj", "orchestrator-ci/").unwrap(), ["orchestrator-ci/a1-1-q1"]);
    assert_eq!(host.state().scans, [("snap1".to_string(), "abc123".to_string(), "/r/leaks.json".to_string())]);
    assert_eq!(host.remote_branch_commit("/proj", "orchestrator-ci/a1-1-q1").unwrap().as_deref(), Some("snap1"));
    host.delete_remote_branch("/proj", "orchestrator-ci/a1-1-q1").unwrap();
    assert!(host.delete_remote_branch("/proj", "orchestrator-ci/a1-1-q1").is_err());
    host.state().leaks = vec![Leak {
        rule: "github-pat".into(),
        file: "limiter.py".into(),
        line: 3,
        commit: SCANNED.into(),
        fingerprint: "c0ffee:limiter.py:github-pat:3".into(),
    }];
    let Err(PushError::Leaks(found)) = host.push_ref("/proj", "snap2", "b", "abc123", "/r/leaks.json") else {
        panic!("no leak")
    };
    assert_eq!(found.leaks[0].commit, "snap2");
    assert!(host.read("/r/leaks.json").unwrap().unwrap().contains(FAKE_TOKEN));
    assert!(host.remote_branch_commit("/proj", "b").unwrap().is_none());
    host.check_gitleaks().unwrap();
    host.state().gitleaks_missing = true;
    assert!(host.check_gitleaks().unwrap_err().0.contains("install gitleaks"));
}

#[test]
fn prune_s_git_steps() {
    let host = FakeHost::default();
    {
        let mut s = host.state();
        s.remote.insert("orchestrator/x", "c1".to_string());
        s.tracking.insert("orchestrator/x".into());
        s.branches.insert("orchestrator/x", "c0".to_string());
        s.ancestors.insert(("c0".into(), "c1".into()));
        s.ahead.insert("orchestrator/x".into(), 2);
        s.missing_commits.insert("c9".into());
    }
    assert!(host.delete_remote_branch_at("/proj", "orchestrator/x", "c0").unwrap_err().0.contains("stale info"));
    host.delete_remote_branch_at("/proj", "orchestrator/x", "c1").unwrap();
    assert!(host.state().tracking.is_empty() && host.state().remote.is_empty());
    host.state().tracking.insert("orchestrator/y".into());
    host.delete_tracking_ref("/proj", "orchestrator/y").unwrap();
    assert!(host.state().tracking.is_empty());
    assert_eq!(host.branch_commit("/proj", "orchestrator/x").unwrap().as_deref(), Some("c0"));
    assert_eq!(host.branch_commit("/proj", "orchestrator/none").unwrap(), None);
    assert!(host.is_ancestor("/proj", "c0", "c1").unwrap() && !host.is_ancestor("/proj", "c1", "c0").unwrap());
    assert_eq!(host.commits_beyond("/proj", "c1", "orchestrator/x").unwrap(), 2);
    assert!(host.has_commit("/proj", "c1").unwrap() && !host.has_commit("/proj", "c9").unwrap());
    host.delete_branch("/proj", "orchestrator/x").unwrap();
    assert!(host.state().branches.is_empty());
}

#[test]
fn worktrees_see_their_own_branch_and_changes() {
    let host = FakeHost::new(Some("abc123"), "feature");
    let wt = proj(&format!("{WORKTREES_DIR}/a1b2c3"));
    host.add_worktree("/proj", &wt, "abc123").unwrap();
    assert!(host.is_worktree("/proj", &wt).unwrap());
    let branches = host.checked_out_branches("/proj").unwrap();
    assert_eq!(branches.iter().collect::<Vec<_>>(), [("feature", &"/proj".to_string())]);
    assert_eq!(host.git(&wt, &["rev-parse", "--abbrev-ref", "HEAD"]).unwrap(), "HEAD\n");
    host.git(&wt, &["switch", "-c", "orchestrator/x", "origin/main"]).unwrap();
    assert_eq!(host.git(&wt, &["rev-parse", "--abbrev-ref", "HEAD"]).unwrap(), "orchestrator/x\n");
    host.write(&format!("{wt}/f.py"), "x", false).unwrap();
    host.write("/proj/mine.py", "x", false).unwrap();
    assert_eq!(host.git(&wt, &["status", "--porcelain"]).unwrap(), format!("?? {wt}/f.py\n"));
    assert_eq!(host.git("/proj", &["status", "--porcelain"]).unwrap(), "?? /proj/mine.py\n");
    host.git(&wt, &["commit", "-m", "x"]).unwrap();
    assert_eq!(host.state().changed.len(), 1);
    host.state().remove_error = Some("fatal: contains modified or untracked files".into());
    assert!(host.remove_worktree("/proj", &wt, false).unwrap_err().0.contains("untracked"));
    host.state().remove_error = None;
    host.remove_worktree("/proj", &wt, true).unwrap();
    assert!(!host.is_worktree("/proj", &wt).unwrap());
    assert_eq!(host.git("/proj", &["switch", "--quiet", "main"]).unwrap(), "");
    assert_eq!(host.git("/proj", &["rev-parse", "--abbrev-ref", "HEAD"]).unwrap(), "main\n");
    assert_eq!(host.git("/proj", &["remote", "get-url", "origin"]).unwrap(), "git@github.com:o/r.git\n");
    host.state().has_origin = false;
    assert_eq!(host.git_run("/proj", &["remote", "get-url", "origin"], 60).unwrap().status, 2);
}

#[test]
fn locked_files_play_on_the_snapshots() {
    let host = FakeHost::default();
    host.write("/proj/tests/test_a.py", "assert f()\n", false).unwrap();
    host.write("/proj/app.py", "tested\n", false).unwrap();
    let locked = host.lock("/proj", "abc123", "refs/orchestrator/a1/locked/tests").unwrap();
    assert_eq!(locked, ["app.py", "tests/test_a.py"]);
    host.write("/proj/app.py", "weakened\n", false).unwrap();
    host.delete("/proj/tests/test_a.py");
    let (differ, diff) = host.locked_changes("/proj", "refs/orchestrator/a1/locked/tests", &locked).unwrap();
    assert_eq!(differ, ["app.py", "tests/test_a.py"]);
    assert_eq!(
        diff,
        "--- app.py: tested\n\n+++ app.py: weakened\n\n--- tests/test_a.py: assert f()\n\n+++ tests/test_a.py: None\n"
    );
    host.restore("/proj", "refs/orchestrator/a1/locked/tests", &differ).unwrap();
    assert_eq!(host.read("/proj/app.py").unwrap().as_deref(), Some("tested\n"));
    assert_eq!(host.read("/proj/tests/test_a.py").unwrap().as_deref(), Some("assert f()\n"));
    assert_eq!(host.state().restores.len(), 1);
    host.write("/proj/new.py", "x", false).unwrap();
    let again = host.lock("/proj", "abc123", "refs/orchestrator/a1/locked/new").unwrap();
    host.delete("/proj/new.py");
    host.write("/proj/new.py", "y", false).unwrap();
    host.restore("/proj", "refs/orchestrator/a1/locked/new", &again).unwrap();
    assert_eq!(host.read("/proj/new.py").unwrap().as_deref(), Some("x"));
    host.delete_refs("/proj", "refs/orchestrator/a1/").unwrap();
    assert!(host.state().refs.is_empty());
    assert_eq!(host.snapshot("/proj", "abc123", "s").unwrap(), "snap3");
}

#[test]
fn the_change_s_files_and_counts() {
    let host = FakeHost::default();
    host.state().diff = "diff --git a/f.py b/f.py\n+++ b/f.py\n@@ -0,0 +1,2 @@\n+a\n+b\n".into();
    let counts = host.changed_line_counts("/proj", "abc123", "snap1").unwrap();
    assert_eq!(counts, [ChangedFile { base_path: "f.py".into(), path: "f.py".into(), added: 2, removed: 0 }]);
    assert_eq!(host.change_diff("/proj", "abc123", Some("snap1")).unwrap(), host.state().diff.as_bytes());
    host.state().line_counts = Some(vec![]);
    assert!(host.changed_line_counts("/proj", "abc123", "snap1").unwrap().is_empty());
    host.state().committed.insert(("abc123".into(), "pyproject.toml".into()), "[project]\n".into());
    assert_eq!(host.file_at("/proj", "abc123", "pyproject.toml").unwrap().as_deref(), Some(&b"[project]\n"[..]));
    assert_eq!(host.file_at("/proj", "snap1", "pyproject.toml").unwrap(), None);
    assert_eq!(host.state().file_reads.len(), 2);
}

#[test]
fn pull_requests_through_host_s_views() {
    let host = FakeHost::default();
    let url = "https://github.com/o/r/pull/7";
    let head = "a".repeat(40);
    let pr = Json::parse(&format!("{{\"state\": \"OPEN\", \"headRefOid\": \"{head}\", \"closedAt\": null}}"));
    host.state().pull_requests.insert(url.into(), FakePr::Prints(pr.unwrap()));
    let read = host.pull_request("/proj", url).unwrap();
    assert_eq!(read, PullRequest { state: "OPEN".into(), head_ref_oid: "a".repeat(40), closed_at: None });
    host.state().pull_requests.insert(url.into(), FakePr::Fails(Completed::new(4, "", "gh auth login")));
    assert!(matches!(host.pull_request_merge("/proj", url), Err(GhError::Unusable(_))));
    assert_eq!(host.state().gh_calls.len(), 2);
    assert_eq!(host.create_pr("/proj", "main", "b", "T", "body", true).unwrap(), url);
    let opened =
        OpenedPr { base: "main".into(), head: "b".into(), title: "T".into(), body: "body".into(), draft: true };
    assert_eq!(host.state().prs[0], opened);
    host.edit_pr_body("/proj", url, "new").unwrap();
    host.mark_pr_ready("/proj", url).unwrap();
    let edits = [PrEdit::Body { url: url.into(), body: "new".into() }, PrEdit::Ready { url: url.into() }];
    assert_eq!(host.state().pr_edits, edits);
}

#[test]
fn run_states_and_session_logs() {
    let host = FakeHost::default();
    host.write("/proj/.orchestrator/runs/r1/state.json", "{\"run_id\": \"r1\"}", false).unwrap();
    host.write("/proj/.orchestrator/runs/r1/spec.md", "spec", false).unwrap();
    host.write("/proj/.orchestrator/runs/r2/state.json", "{\"run_id\": \"r2\"}", false).unwrap();
    host.state().ages.insert("r2".into(), 400);
    let states = host.run_states("/proj").unwrap();
    let ages: Vec<u64> = states.iter().map(|(age, _)| *age).collect();
    assert_eq!(ages, [0, 400]);
    host.state().session_logs.insert(("claude".into(), "s1".into()), "log".into());
    assert_eq!(host.session_log("claude", "s1").unwrap().as_deref(), Some(&b"log"[..]));
    assert_eq!(host.session_log("pi", "s1").unwrap(), None);
}

#[test]
#[should_panic(expected = "FakeHost ran a real process")]
fn a_real_process_fails_the_test() {
    let _ = FakeHost::default().run(&["true"], None, 60);
}

#[test]
#[should_panic(expected = "unscripted gh command")]
fn an_unscripted_gh_command_fails_the_test() {
    let _ = FakeHost::default().gh_run("/proj", &["pr", "list"]);
}
