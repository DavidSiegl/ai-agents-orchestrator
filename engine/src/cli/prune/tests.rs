//! Pruning run branches. Python's cases, by class (tests/test_orchestrator.py): all of TestPrune, TestSweepSummary,
//! TestPruneCLI and TestSweepOnRun, and TestHostPullRequest.test_closed_without_a_time, on PruneTest's FakeHost (runs
//! in /proj, each with pull request #<n> on branch orchestrator/task-<key>); and TestHostGit.test_prune_on_real_git, on
//! a real origin and clone. Where a Python case replaces a Host method, the Rust one scripts the FakeHost instead:
//! TestSweepSummary.test_a_sweep_that_fails_is_one_line fails run_states (FakeState::run_states_with) where Python
//! fails checked_out_branches, and test_state_saved_meanwhile_is_not_overwritten rewrites the state at the first git
//! command after gh pr view (FakeState::before_git) where Python rewrites it in gh_run. TestPruneCLI.test_target_flags'
//! usage error is engine/src/cli/args/tests.rs' kind of case, and is kept here with the rest of the class. Besides
//! Python's: the Ctrl-C (a failure while the Interrupt is raised stops prune, the sweep and the run), and a closedAt
//! without an offset read in the machine's zone.

use std::rc::Rc;

use super::*;
use crate::cli::fixture::Cli;
use crate::cli::{parse_args, Command, RunsCommand, Target};
use crate::cli::Stop;
use crate::constants::{APPROVE, STALE_SECONDS};
use crate::host::fake::{FakeHost, FakePr};
use crate::host::Completed;
use crate::py::json::Map;
use crate::summary::iso_utc;
use crate::workflow::fixture::{build_turn, review_turn, saved_run, spec_turn, Fixture};

const PR_HEAD: &str = "1111111111111111111111111111111111111111";
const OLDER: &str = "2222222222222222222222222222222222222222";
const LATER: &str = "3333333333333333333333333333333333333333";
/// 2026-10-09, by the fixture's wall clock.
const NOW: f64 = 1_791_500_000.0;
const DAY: f64 = 86_400.0;

/// What gh pr view prints of a pull request.
fn pr(state: &str, head: &str, closed_at: Option<f64>) -> FakePr {
    let closed = closed_at.map_or(Json::Null, |t| Json::from(iso_utc(t as i64).replace("+00:00", "Z")));
    FakePr::Prints(Json::Object(
        [("state", Json::from(state)), ("headRefOid", Json::from(head)), ("closedAt", closed)].into_iter().collect(),
    ))
}

/// Merged a day ago at PR_HEAD.
fn merged() -> FakePr {
    pr("MERGED", PR_HEAD, Some(NOW - DAY))
}

fn closed(days_ago: f64) -> FakePr {
    pr("CLOSED", PR_HEAD, Some(NOW - days_ago * DAY))
}

fn open() -> FakePr {
    pr("OPEN", PR_HEAD, None)
}

/// gh failing with status and stderr.
fn failing(status: i32, stderr: &str) -> FakePr {
    FakePr::Fails(Completed::new(status, "", stderr))
}

/// A run PruneTest adds: by default finished, with pull request #7, its branch at PR_HEAD locally and on origin.
struct Run {
    key: &'static str,
    pr: FakePr,
    n: u32,
    local: Option<&'static str>,
    remote: Option<&'static str>,
    branch: Option<&'static str>,
    /// Fields of its state.json put over the defaults.
    fields: Vec<(&'static str, Json)>,
}

fn run(key: &'static str, pr: FakePr) -> Run {
    Run { key, pr, n: 7, local: Some(PR_HEAD), remote: Some(PR_HEAD), branch: None, fields: vec![] }
}

impl Run {
    fn n(mut self, n: u32) -> Run {
        self.n = n;
        self
    }

    fn local(mut self, at: Option<&'static str>) -> Run {
        self.local = at;
        self
    }

    fn remote(mut self, at: Option<&'static str>) -> Run {
        self.remote = at;
        self
    }

    fn branch(mut self, branch: &'static str) -> Run {
        self.branch = Some(branch);
        self
    }

    fn with(mut self, key: &'static str, value: impl Into<Json>) -> Run {
        self.fields.push((key, value.into()));
        self
    }
}

/// PruneTest: a Cli on a FakeHost of /proj, its wall clock at NOW, with no live pid.
struct Prune {
    cli: Cli,
}

impl Prune {
    fn new() -> Prune {
        let mut cli = Cli::new();
        cli.f.clock.set(NOW);
        cli.f.pid_alive = Box::new(|_| false);
        Prune { cli }
    }

    fn host(&self) -> std::cell::RefMut<'_, crate::host::fake::FakeState> {
        self.cli.f.host.state()
    }

    /// Add r's run; its run id.
    fn add(&self, r: Run) -> String {
        let run_id = format!("20261001-120000-{}", r.key);
        let branch = r.branch.map_or(format!("orchestrator/task-{}", r.key), String::from);
        let url = format!("https://github.com/o/r/pull/{}", r.n);
        let mut state = RunState::new(&run_id, "a task", "/proj");
        state.phase = "done".into();
        state.verdict = Some(APPROVE.into());
        state.pull_request = true;
        state.base_branch = Some("main".into());
        state.branch = Some(branch.clone());
        state.pr_url = Some(url.clone());
        let Json::Object(mut saved) = state.to_json() else { unreachable!("a state is saved as an object") };
        for (key, value) in r.fields {
            saved.insert(key, value);
        }
        let mut host = self.host();
        host.files.insert(state_path(&run_id), format!("{}\n", Json::Object(saved).dumps()));
        host.pull_requests.insert(url, r.pr);
        if let Some(at) = r.local {
            host.branches.insert(branch.as_str(), at.to_string());
        }
        if let Some(at) = r.remote {
            host.remote.insert(branch.as_str(), at.to_string());
        }
        run_id
    }

    fn saved(&self, run_id: &str) -> Map<Json> {
        self.cli.saved(run_id).as_object().cloned().expect("an object")
    }

    fn cleanup(&self, run_id: &str) -> Json {
        self.saved(run_id).get("branch_cleanup").cloned().unwrap_or(Json::Null)
    }

    /// Replace run_id's state.json with saved.
    fn resave(&self, run_id: &str, saved: Map<Json>) {
        self.host().files.insert(state_path(run_id), Json::Object(saved).dumps());
    }

    /// prune_branches by run id, ordered as returned.
    fn prune(&self, dry_run: bool, revisit: bool) -> Vec<(String, PrunedRun)> {
        let pruned = self.cli.with(|w| prune_branches(w, "/proj", dry_run, revisit)).expect("prune_branches");
        pruned.into_iter().map(|r| (r.run_id.clone(), r)).collect()
    }

    /// The one pruned run of run_id.
    fn pruned(&self, run_id: &str) -> PrunedRun {
        let pruned = self.prune(false, false);
        pruned.into_iter().find(|(id, _)| id == run_id).map(|(_, r)| r).unwrap_or_else(|| panic!("{run_id} not pruned"))
    }

    fn log(&self) -> Vec<String> {
        self.cli.f.told.lines()
    }

    /// The git commands that delete a branch or a ref.
    fn deleting_calls(&self) -> Vec<Vec<String>> {
        let calls = self.host().git_calls.clone();
        calls
            .into_iter()
            .filter(|c| c[0] == "update-ref" || c[..2] == ["branch", "--quiet"] || c.iter().any(|a| a == "--delete"))
            .collect()
    }
}

fn state_path(run_id: &str) -> String {
    format!("/proj/.orchestrator/runs/{run_id}/state.json")
}

fn strings(argv: &[&str]) -> Vec<String> {
    argv.iter().map(|a| a.to_string()).collect()
}

// -- TestPrune -------------------------------------------------------------------------------------------------------

#[test]
fn merged_at_the_pull_requests_head() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()));
    p.host().tracking.insert("orchestrator/task-aaaaaa".into());
    let r = p.pruned(&run_id);
    assert_eq!((r.local.as_str(), r.remote.as_str(), r.cleanup.as_deref()), ("deleted", "deleted", Some("deleted")));
    let host = p.host();
    assert!(host.branches.is_empty() && host.remote.is_empty() && host.tracking.is_empty());
    assert_eq!(host.mtime_kept, [state_path(&run_id)]);
    let lease = format!("--force-with-lease=refs/heads/orchestrator/task-aaaaaa:{PR_HEAD}");
    let push = strings(&["push", "--quiet", &lease, "origin", "--delete", "refs/heads/orchestrator/task-aaaaaa"]);
    assert!(host.git_calls.contains(&push), "{:?}", host.git_calls);
    assert_eq!(host.gh_calls, [strings(&["pr", "view", "https://github.com/o/r/pull/7", "--json", "state,headRefOid,closedAt"])]);
    drop(host);
    assert_eq!(p.cleanup(&run_id), Json::from("deleted"));
}

#[test]
fn local_behind_the_pull_requests_head_is_deleted() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).local(Some(OLDER)));
    p.host().ancestors.insert((OLDER.into(), PR_HEAD.into()));
    let r = p.pruned(&run_id);
    assert_eq!((r.local.as_str(), r.cleanup.as_deref()), ("deleted", Some("deleted")));
    assert!(p.host().git_calls.contains(&strings(&["branch", "--quiet", "-D", "orchestrator/task-aaaaaa"])));
}

#[test]
fn local_ahead_of_the_pull_requests_head_is_kept() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).local(Some(LATER)));
    p.host().ahead.insert("orchestrator/task-aaaaaa".into(), 3);
    let r = p.pruned(&run_id);
    assert_eq!((r.local.as_str(), r.remote.as_str()), ("kept: local has 3 commits beyond PR #7", "deleted"));
    {
        let host = p.host();
        assert_eq!(host.branches.get("orchestrator/task-aaaaaa").map(String::as_str), Some(LATER));
        assert_eq!(host.branches.len(), 1);
        assert!(host.remote.is_empty());
        let count = format!("{PR_HEAD}..refs/heads/orchestrator/task-aaaaaa");
        assert!(host.git_calls.contains(&strings(&["rev-list", "--count", &count])));
    }
    assert_eq!(p.cleanup(&run_id), Json::from("kept: local has 3 commits beyond PR #7"));
}

#[test]
fn one_commit_beyond() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).local(Some(LATER)));
    p.host().ahead.insert("orchestrator/task-aaaaaa".into(), 1);
    assert_eq!(p.pruned(&run_id).local, "kept: local has 1 commit beyond PR #7");
}

#[test]
fn remote_moved_past_the_pull_requests_head_is_kept() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).remote(Some(LATER)));
    let r = p.pruned(&run_id);
    assert_eq!((r.local.as_str(), r.remote.as_str()), ("deleted", "kept: remote has commits beyond PR #7"));
    assert_eq!(p.host().remote.get("orchestrator/task-aaaaaa").map(String::as_str), Some(LATER));
    assert_eq!(p.cleanup(&run_id), Json::from("kept: remote has commits beyond PR #7"));
    assert!(!p.host().git_calls.iter().any(|c| c.iter().any(|a| a == "--delete")));
}

#[test]
fn remote_already_gone_drops_the_stale_tracking_ref() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).remote(None));
    p.host().tracking.insert("orchestrator/task-aaaaaa".into());
    let r = p.pruned(&run_id);
    assert_eq!((r.local.as_str(), r.remote.as_str(), r.cleanup.as_deref()), ("deleted", "gone", Some("deleted")));
    let host = p.host();
    assert!(host.tracking.is_empty());
    assert!(host.git_calls.contains(&strings(&["update-ref", "-d", "refs/remotes/origin/orchestrator/task-aaaaaa"])));
}

#[test]
fn checked_out_branch_is_kept() {
    for place in ["/proj", "/proj-wt"] {
        let p = Prune::new();
        let run_id = p.add(run("aaaaaa", merged()));
        if place == "/proj" {
            p.host().branch = "orchestrator/task-aaaaaa".into();
        } else {
            p.host().worktrees.insert(place, "orchestrator/task-aaaaaa".into());
        }
        let r = p.pruned(&run_id);
        let kept = format!("kept: checked out in {place}");
        assert_eq!((r.local.as_str(), r.remote.as_str()), (kept.as_str(), "deleted"), "{place}");
        assert!(p.host().branches.contains_key("orchestrator/task-aaaaaa"));
        assert_eq!(p.cleanup(&run_id), Json::from(kept));
    }
}

#[test]
fn both_sides_kept_records_both_reasons() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).local(Some(LATER)).remote(Some(LATER)));
    p.host().ahead.insert("orchestrator/task-aaaaaa".into(), 2);
    p.prune(false, false);
    assert_eq!(
        p.cleanup(&run_id),
        Json::from("kept: local has 2 commits beyond PR #7; remote has commits beyond PR #7")
    );
}

#[test]
fn pull_request_head_missing_from_the_clone_keeps_local() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).local(Some(OLDER)));
    p.host().missing_commits.insert(PR_HEAD.into());
    let r = p.pruned(&run_id);
    assert_eq!(r.local, format!("kept: PR #7's head {} is not in this clone; fetch it to decide", &PR_HEAD[..12]));
    assert!(p.host().branches.contains_key("orchestrator/task-aaaaaa"));
}

#[test]
fn pull_requests_not_done_are_skipped() {
    let cases = [(closed(13.0), "PR #7 was closed less than 14 days ago"), (open(), "PR #7 is open")];
    for (pr, why) in cases {
        let p = Prune::new();
        let run_id = p.add(run("aaaaaa", pr));
        let before = p.host().files.get(&state_path(&run_id)).cloned();
        let r = p.pruned(&run_id);
        assert_eq!(r.skipped.as_deref(), Some(why));
        assert_eq!(r.cleanup, None);
        assert!(p.deleting_calls().is_empty());
        assert!(p.host().writes.is_empty());
        assert_eq!(p.host().files.get(&state_path(&run_id)).cloned(), before);
        assert_eq!(r.line(), format!("{run_id}  orchestrator/task-aaaaaa  skipped: {why}"));
    }
}

#[test]
fn closed_15_days_ago_is_deleted() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", closed(15.0)));
    let r = p.pruned(&run_id);
    assert_eq!((r.local.as_str(), r.remote.as_str(), r.cleanup.as_deref()), ("deleted", "deleted", Some("deleted")));
    assert!(p.host().branches.is_empty() && p.host().remote.is_empty());
}

#[test]
fn running_run_is_skipped() {
    // Saved a moment ago by an orchestrator on another host: RUNNING, whatever its pid.
    let p = Prune::new();
    let owner = Json::Object(
        [("host", Json::from("elsewhere")), ("pid", Json::from(1)), ("started_at", Json::from("x"))].into_iter().collect(),
    );
    p.add(run("aaaaaa", merged()).with("phase", "build").with("verdict", Json::Null).with("owner", owner));
    assert!(p.prune(false, false).is_empty());
    assert!(p.host().gh_calls.is_empty() && p.deleting_calls().is_empty());
}

#[test]
fn stale_run_is_a_candidate_and_keeps_its_age() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).with("phase", "publish").with("verdict", Json::Null));
    p.host().ages.insert(run_id.clone(), STALE_SECONDS + 1);
    assert_eq!(p.pruned(&run_id).cleanup.as_deref(), Some("deleted"));
    assert_eq!(p.host().mtime_kept, [state_path(&run_id)]);
}

#[test]
fn branch_without_the_prefix_is_never_touched() {
    let p = Prune::new();
    p.add(run("aaaaaa", merged()).branch("feature/rate-limiter"));
    p.add(run("bbbbbb", merged()).branch("main"));
    assert!(p.prune(false, false).is_empty());
    assert!(p.host().gh_calls.is_empty() && p.deleting_calls().is_empty());
    let names: Vec<String> = p.host().branches.keys().map(String::from).collect();
    assert_eq!(names, ["feature/rate-limiter", "main"]);
}

#[test]
fn runs_without_a_pull_request_are_never_touched() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()));
    let mut saved = p.saved(&run_id);
    saved.insert("pr_url", Json::Null);
    p.resave(&run_id, saved);
    assert!(p.prune(false, false).is_empty());
    assert!(p.host().gh_calls.is_empty());
}

#[test]
fn recorded_runs() {
    let p = Prune::new();
    let done = p.add(run("aaaaaa", merged()).with("branch_cleanup", "deleted"));
    let kept = p.add(run("bbbbbb", merged()).n(8).with("branch_cleanup", "kept: checked out in /proj"));
    assert!(p.prune(false, false).is_empty());
    assert!(p.host().gh_calls.is_empty());
    let pruned = p.prune(false, true);
    assert_eq!(pruned.iter().map(|(id, _)| id.as_str()).collect::<Vec<_>>(), [kept.as_str()]);
    assert_eq!(p.cleanup(&kept), Json::from("deleted"));
    assert_eq!(p.cleanup(&done), Json::from("deleted"));
}

#[test]
fn saved_before_prune_existed() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()));
    let mut saved = p.saved(&run_id);
    saved.remove("branch_cleanup");
    p.resave(&run_id, saved.clone());
    assert_eq!(RunState::from_saved(&saved).unwrap().branch_cleanup, None);
    assert_eq!(p.pruned(&run_id).cleanup.as_deref(), Some("deleted"));
    saved.insert("branch_cleanup", Json::from("deleted"));
    assert_eq!(p.saved(&run_id), saved);
}

#[test]
fn dry_run_deletes_and_records_nothing() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).remote(None));
    p.host().tracking.insert("orchestrator/task-aaaaaa".into());
    let kept = p.add(run("bbbbbb", merged()).n(8).remote(Some(LATER)));
    let pruned: Map<PrunedRun> = p.prune(true, false).into_iter().collect();
    let first = &pruned.get(&run_id).unwrap();
    assert_eq!((first.local.as_str(), first.remote.as_str()), ("would be deleted", "gone"));
    assert_eq!(pruned.get(&kept).unwrap().remote, "kept: remote has commits beyond PR #8");
    assert!(p.deleting_calls().is_empty() && p.host().writes.is_empty());
    assert_eq!(p.host().branches.len(), 2);
    assert!(p.host().tracking.contains("orchestrator/task-aaaaaa"));
}

#[test]
fn gh_failure_on_one_run_skips_only_that_run() {
    let p = Prune::new();
    let failed = p.add(run("aaaaaa", failing(1, "HTTP 502: Bad Gateway")));
    let fine = p.add(run("bbbbbb", merged()).n(8));
    let pruned: Map<PrunedRun> = p.prune(false, false).into_iter().collect();
    assert_eq!(
        pruned.get(&failed).unwrap().skipped.as_deref(),
        Some("failed: gh pr view https://github.com/o/r/pull/7 --json state,headRefOid,closedAt failed: HTTP 502: Bad Gateway")
    );
    let said = format!("could not prune the branch of run {failed}: gh pr view");
    assert!(p.log().iter().any(|l| l.starts_with(&said)), "{:?}", p.log());
    assert_eq!(p.cleanup(&failed), Json::Null);
    assert!(p.host().branches.contains_key("orchestrator/task-aaaaaa"));
    assert_eq!(pruned.get(&fine).unwrap().cleanup.as_deref(), Some("deleted"));
}

#[test]
fn rejected_lease_skips_the_run() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()));
    let host = Rc::clone(&p.cli.f.host);
    p.host().before_git = Some(Box::new(move |_, args| {
        if args.first() == Some(&"push") {
            host.state().remote.insert("orchestrator/task-aaaaaa", LATER.to_string());
        }
    }));
    let r = p.pruned(&run_id);
    assert!(r.skipped.as_deref().unwrap().contains("stale info"), "{r:?}");
    assert!(p.log().iter().any(|l| l.starts_with(&format!("could not prune the branch of run {run_id}"))));
    assert_eq!(p.cleanup(&run_id), Json::Null);
}

#[test]
fn unusable_gh_stops_the_sweep_after_one_line() {
    let cases = [(127, "sh: 1: exec: gh: not found"), (4, "To get started with GitHub CLI, please run:  gh auth login")];
    for (status, stderr) in cases {
        let p = Prune::new();
        p.add(run("aaaaaa", failing(status, stderr)));
        p.add(run("bbbbbb", failing(status, stderr)).n(8));
        assert!(p.prune(false, false).is_empty());
        assert_eq!(p.host().gh_calls.len(), 1);
        let said = format!(
            "stopped pruning run branches: gh pr view https://github.com/o/r/pull/7 --json state,headRefOid,closedAt \
             failed with status {status}: {stderr}"
        );
        assert_eq!(p.log().len(), 1, "{:?}", p.log());
        assert!(p.log()[0].contains(&said), "{:?}", p.log());
        assert!(p.deleting_calls().is_empty());
    }
}

#[test]
fn unreadable_state_skips_the_run() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()));
    let mut saved = p.saved(&run_id);
    saved.remove("task");
    p.resave(&run_id, saved);
    assert!(p.pruned(&run_id).skipped.as_deref().unwrap().contains("is incomplete"));
    assert!(p.deleting_calls().is_empty());
}

#[test]
fn state_saved_meanwhile_is_not_overwritten() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()));
    // At the first git command, which is after gh pr view.
    let (host, path) = (Rc::clone(&p.cli.f.host), state_path(&run_id));
    let mut saved = p.saved(&run_id);
    saved.insert("round", Json::from(2));
    let resumed = Json::Object(saved).dumps();
    p.host().before_git = Some(Box::new(move |_, _| {
        host.state().files.insert(path.as_str(), resumed.clone());
    }));
    assert!(p.pruned(&run_id).skipped.as_deref().unwrap().contains("changed while its branch was pruned"));
    assert_eq!(p.saved(&run_id).get("round"), Some(&Json::from(2)));
    assert_eq!(p.cleanup(&run_id), Json::Null);
}

// -- TestHostPullRequest.test_closed_without_a_time, and closedAt in the machine's zone ----------------------------

fn closed_at(at: Option<&str>) -> PullRequest {
    PullRequest { state: "CLOSED".into(), head_ref_oid: PR_HEAD.into(), closed_at: at.map(String::from) }
}

#[test]
fn closed_without_a_time() {
    let e = pull_request_pending(&closed_at(Some("yesterday")), "7", NOW).unwrap_err();
    assert_eq!(e.0, "PR #7 is closed at 'yesterday', which is not a time");
    let e = pull_request_pending(&closed_at(None), "7", NOW).unwrap_err();
    assert_eq!(e.0, "PR #7 is closed at None, which is not a time");
}

/// What the zone test's child process checks, set to the test's name.
const IN_ZONE: &str = "OCTOFLOW_PRUNE_ZONE_CHILD";

#[test]
fn a_closing_time_without_an_offset_is_read_in_the_machine_s_zone() {
    let name = "cli::prune::tests::a_closing_time_without_an_offset_is_read_in_the_machine_s_zone";
    if std::env::var_os(IN_ZONE).is_none() {
        // The zone is the process's, read from TZ: this test checks in a copy of itself run 5:45 east of UTC.
        let exe = std::env::current_exe().expect("the test binary");
        let out = std::process::Command::new(exe)
            .args(["--exact", name, "--nocapture"])
            .env(IN_ZONE, "1")
            .env("TZ", "<+0545>-5:45")
            .output()
            .expect("the test binary runs");
        assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stdout));
        assert!(String::from_utf8_lossy(&out.stdout).contains("1 passed"), "{}", String::from_utf8_lossy(&out.stdout));
        return;
    }
    // 2026-10-01T12:00:00 here is 06:15 UTC.
    let closed = fromisoformat_timestamp("2026-10-01T06:15:00+00:00").unwrap();
    let grace = CLOSED_BRANCH_GRACE as f64;
    let pr = closed_at(Some("2026-10-01T12:00:00"));
    assert_eq!(pull_request_pending(&pr, "7", closed + grace + 1.0).unwrap(), None);
    assert!(pull_request_pending(&pr, "7", closed + grace - 1.0).unwrap().is_some());
}

// -- TestSweepSummary ------------------------------------------------------------------------------------------------

impl Prune {
    /// sweep_branches, and what it logged.
    fn sweep(&self) -> Vec<String> {
        self.cli.f.told.log.borrow_mut().clear();
        self.cli.with(|w| sweep_branches(w, "/proj")).expect("a sweep without a Ctrl-C");
        self.log()
    }
}

#[test]
fn one_line_when_it_deleted_or_kept() {
    let p = Prune::new();
    p.add(run("aaaaaa", merged()));
    p.add(run("bbbbbb", merged()).n(8));
    p.add(run("cccccc", merged()).n(9).remote(Some(LATER)));
    assert_eq!(p.sweep(), ["deleted 2 branches of earlier runs; kept 1, see prune"]);
}

#[test]
fn single_branches() {
    let p = Prune::new();
    p.add(run("aaaaaa", merged()));
    assert_eq!(p.sweep(), ["deleted 1 branch of earlier runs"]);
    p.add(run("bbbbbb", merged()).n(8).remote(Some(LATER)));
    assert_eq!(p.sweep(), ["kept 1 branch of earlier runs, see prune"]);
}

#[test]
fn nothing_when_there_was_nothing_to_do() {
    let p = Prune::new();
    assert!(p.sweep().is_empty());
    p.add(run("aaaaaa", open()));
    p.add(run("bbbbbb", merged()).n(8).with("branch_cleanup", "kept: checked out in /proj"));
    // Both sides already gone: recorded, but nothing deleted.
    let gone = p.add(run("cccccc", merged()).n(9).local(None).remote(None));
    assert!(p.sweep().is_empty());
    assert_eq!(p.cleanup(&gone), Json::from("deleted"));
}

#[test]
fn a_sweep_that_fails_is_one_line() {
    let p = Prune::new();
    p.add(run("aaaaaa", merged()));
    p.host().run_states_with = Some(Box::new(|_| Some(Err(OrchestratorError::new("git worktree list failed")))));
    assert_eq!(p.sweep(), ["could not prune the branches of earlier runs: git worktree list failed"]);
}

// -- The Ctrl-C ------------------------------------------------------------------------------------------------------

#[test]
fn a_failure_during_a_ctrl_c_stops_prune_and_the_sweep() {
    let p = Prune::new();
    p.add(run("aaaaaa", failing(1, "interrupted")));
    p.add(run("bbbbbb", merged()).n(8));
    p.cli.f.interrupt.raise();
    let e = p.cli.with(|w| prune_branches(w, "/proj", false, false)).unwrap_err();
    assert!(e.0.contains("interrupted"), "{e}");
    // Nothing logged, and the second run not looked at.
    assert!(p.log().is_empty());
    assert_eq!(p.host().gh_calls.len(), 1);
    let e = p.cli.with(|w| sweep_branches(w, "/proj")).unwrap_err();
    assert!(e.0.contains("interrupted"), "{e}");
    assert!(p.log().is_empty());
    // A failure of the listing itself too.
    p.host().run_states_with = Some(Box::new(|_| Some(Err(OrchestratorError::new("ssh: interrupted")))));
    assert!(p.cli.with(|w| sweep_branches(w, "/proj")).is_err());
    assert!(p.log().is_empty());
}

// -- TestPruneCLI ----------------------------------------------------------------------------------------------------

#[test]
fn prints_one_line_per_candidate_run() {
    let p = Prune::new();
    let deleted = p.add(run("aaaaaa", merged()));
    let kept = p.add(run("bbbbbb", merged()).n(8).local(Some(LATER)));
    p.host().ahead.insert("orchestrator/task-bbbbbb".into(), 3);
    let waiting = p.add(run("cccccc", open()).n(9));
    assert_eq!(p.cli.main(&["prune", "--cwd", "/proj"]), 0);
    assert_eq!(
        p.cli.lines(),
        [
            format!("{deleted}  orchestrator/task-aaaaaa  local deleted, remote deleted"),
            format!("{kept}  orchestrator/task-bbbbbb  local kept: local has 3 commits beyond PR #8, remote deleted"),
            format!("{waiting}  orchestrator/task-cccccc  skipped: PR #9 is open"),
        ]
    );
}

#[test]
fn revisits_kept_branches() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()).with("branch_cleanup", "kept: checked out in /proj"));
    assert_eq!(p.cli.main(&["prune", "--cwd", "/proj"]), 0);
    assert_eq!(p.cli.take_out(), format!("{run_id}  orchestrator/task-aaaaaa  local deleted, remote deleted\n"));
}

#[test]
fn dry_run() {
    let p = Prune::new();
    let run_id = p.add(run("aaaaaa", merged()));
    assert_eq!(p.cli.main(&["prune", "--dry-run", "--cwd", "/proj"]), 0);
    assert_eq!(
        p.cli.take_out(),
        format!("{run_id}  orchestrator/task-aaaaaa  local would be deleted, remote would be deleted\n")
    );
    assert!(p.deleting_calls().is_empty() && p.host().writes.is_empty());
}

#[test]
fn nothing_to_prune() {
    let p = Prune::new();
    assert_eq!(p.cli.main(&["prune", "--cwd", "/proj"]), 0);
    assert_eq!(p.cli.take_out(), "No run branches to prune.\n");
}

#[test]
fn unusable_gh_is_not_an_error() {
    let p = Prune::new();
    p.add(run("aaaaaa", failing(127, "exec: gh: not found")));
    assert_eq!(p.cli.main(&["prune", "--cwd", "/proj"]), 0);
    assert_eq!(p.cli.take_out(), "No run branches to prune.\n");
    assert_eq!(p.log().len(), 1);
}

#[test]
fn target_flags() {
    let cli = Cli::new();
    let target = Target { machine: Some("m".into()), cwd: Some("~/proj".into()) };
    assert_eq!(
        cli.command(&["prune", "--dry-run", "--machine", "m", "--cwd", "~/proj"]),
        Command::Runs(target, RunsCommand::Prune { dry_run: true })
    );
    assert_eq!(cli.command(&["prune"]), Command::Runs(Target::default(), RunsCommand::Prune { dry_run: false }));
    let refused = parse_args(["prune", "--machine", "m"].map(std::ffi::OsString::from), &cli.config).unwrap_err();
    assert_eq!(refused.exit_code(), 2);
}

// -- TestSweepOnRun --------------------------------------------------------------------------------------------------

/// The new run's key, as the FakeHerdr's agents of a new run are named.
fn key_of(name: &str) -> &str {
    name.rsplit('-').next().unwrap_or(name)
}

/// A PruneTest whose FakeHerdr plays a new default run to APPROVE, its Spec Collector first recording the branches
/// locally and on origin as the interview starts (seen).
struct SweepOnRun {
    p: Prune,
    seen: Seen,
}

/// The local branches and origin's, by name with their commit, as the interview started; None before it.
type Seen = Rc<std::cell::RefCell<Option<(Map<String>, Map<String>)>>>;

impl SweepOnRun {
    fn new() -> SweepOnRun {
        let host = Rc::new(FakeHost::default());
        let state = crate::workflow::fixture::new_state();
        let seen = Rc::new(std::cell::RefCell::new(None));
        let seen_by_spec = Rc::clone(&seen);
        let mut spec = spec_turn();
        let spec: crate::herdr::fake::Turn = Box::new(move |prompt, s, h| {
            let host = h.state();
            *seen_by_spec.borrow_mut() = Some((host.branches.clone(), host.remote.clone()));
            drop(host);
            spec(prompt, s, h)
        });
        let script = vec![("spec", vec![spec]), ("build", vec![build_turn(1)]), ("review", vec![review_turn(1, APPROVE)])];
        let mut f = Fixture::on(Rc::clone(&host), state.clone(), script);
        // The new run's agents play its state, which the fake finds by the key their names end in.
        let herdr = Rc::clone(&f.herdr);
        let follow = Rc::clone(&host);
        f.herdr.state().prompt_with = Some(Box::new(move |name, _| {
            let key = key_of(name).to_string();
            let found = follow
                .state()
                .files
                .iter()
                .find(|(path, _)| path.ends_with(&format!("-{key}/state.json")))
                .map(|(_, text)| text.clone());
            if let Some(text) = found {
                herdr.state().run = RunState::from_text(&text).expect("the new run's state");
            }
            Ok(())
        }));
        f.clock.set(NOW);
        f.pid_alive = Box::new(|_| false);
        SweepOnRun { p: Prune { cli: Cli::of(f) }, seen }
    }
}

#[test]
fn deletes_merged_branches_before_the_interview() {
    let t = SweepOnRun::new();
    let earlier = t.p.add(run("aaaaaa", merged()));
    assert_eq!(t.p.cli.main(&["run", "--cwd", "/proj", "task"]), 0, "{:?}", t.p.log());
    let (branches, remote) = t.seen.borrow().clone().expect("the interview ran");
    assert!(branches.is_empty());
    assert!(!remote.contains_key("orchestrator/task-aaaaaa"));
    assert_eq!(t.p.cleanup(&earlier), Json::from("deleted"));
    assert!(t.p.log().iter().any(|l| l == "deleted 1 branch of earlier runs"), "{:?}", t.p.log());
}

#[test]
fn a_failing_sweep_leaves_the_run_as_it_was() {
    let t = SweepOnRun::new();
    t.p.add(run("aaaaaa", merged()));
    let failing_states = Rc::new(std::cell::Cell::new(true));
    let once = Rc::clone(&failing_states);
    t.p.host().run_states_with = Some(Box::new(move |_| {
        once.replace(false).then(|| Err(OrchestratorError::new("ssh: connection reset")))
    }));
    assert_eq!(t.p.cli.main(&["run", "--cwd", "/proj", "task"]), 0, "{:?}", t.p.log());
    assert!(!failing_states.get());
    assert!(t.seen.borrow().is_some());
    assert!(t.p.log().iter().any(|l| l == "could not prune the branches of earlier runs: ssh: connection reset"));
    assert!(t.p.host().branches.contains_key("orchestrator/task-aaaaaa"));
    assert!(t.p.cli.take_out().starts_with("APPROVE: "));
}

#[test]
fn no_gh_for_resume_or_no_pr() {
    let t = SweepOnRun::new();
    t.p.add(run("aaaaaa", merged()));
    // A done run resumes to its verdict without a turn.
    let mut resumable = saved_run("done", 1, &["spec", "build", "review"]);
    resumable.verdict = Some(APPROVE.into());
    t.p.host().files.insert(state_path(&resumable.run_id), resumable.to_text());
    t.p.host().ages.insert(resumable.run_id.clone(), 10_000);
    assert_eq!(t.p.cli.main(&["resume", "a1b2c3", "--cwd", "/proj"]), 0);
    assert!(t.p.host().gh_calls.is_empty());
    assert_eq!(t.p.cli.main(&["run", "--no-pr", "--cwd", "/proj", "task"]), 0, "{:?}", t.p.log());
    assert!(t.p.host().gh_calls.is_empty());
    assert!(t.p.host().branches.contains_key("orchestrator/task-aaaaaa"));
}

#[test]
fn a_run_whose_sweep_a_ctrl_c_stopped_is_interrupted_before_its_state() {
    let t = SweepOnRun::new();
    t.p.add(run("aaaaaa", merged()));
    t.p.cli.f.interrupt.raise();
    t.p.host().run_states_with = Some(Box::new(|_| Some(Err(OrchestratorError::new("ssh: interrupted")))));
    let ended = t.p.cli.ended(&["run", "--cwd", "/proj", "task"]);
    assert!(matches!(ended, Err(Stop::Interrupted(None))));
    assert!(t.seen.borrow().is_none());
}

// -- TestHostGit.test_prune_on_real_git ------------------------------------------------------------------------------

#[test]
fn prune_on_real_git() {
    use crate::host::tests::{host_with_tools, tool, Repo};
    use crate::test_support::TempDir;
    // A squash-merged run branch: local behind the PR head, origin at it. gh, a script, says it merged at head.
    let bin = TempDir::new();
    let repo = Repo::with_host(&[("app.py", "one\n"), (".orchestrator/.gitignore", "*\n")], host_with_tools(&bin.path()));
    let origin = format!("{}/origin.git", repo.outside);
    repo.git(&["init", "--quiet", "--bare", "-b", "main", &origin]);
    repo.git(&["remote", "add", "origin", &origin]);
    repo.git(&["push", "--quiet", "origin", "main"]);
    repo.git(&["switch", "--quiet", "-c", "orchestrator/x-a1b2c3"]);
    let behind = repo.commit(&[("x.py", "x\n")], "x");
    let head = repo.commit(&[("x.py", "x\ny\n")], "y");
    repo.git(&["push", "--quiet", "origin", "orchestrator/x-a1b2c3"]);
    repo.git(&["reset", "--quiet", "--hard", &behind]);
    repo.git(&["switch", "--quiet", "main"]);
    let gh = format!("{{\"state\": \"MERGED\", \"headRefOid\": \"{head}\", \"closedAt\": \"2026-10-01T12:00:00Z\"}}");
    tool(&bin.path(), "gh", &format!("#!/bin/sh\necho '{gh}'\n"));
    let mut state = RunState::new("20261001-120000-a1b2c3", "x", &repo.path);
    state.phase = "done".into();
    state.verdict = Some(APPROVE.into());
    state.branch = Some("orchestrator/x-a1b2c3".into());
    state.pr_url = Some("https://github.com/o/r/pull/7".into());
    let path = format!("{}/state.json", state.dir());
    repo.host.write(&path, &state.to_text(), false).unwrap();
    let p = Prune::new();
    let pruned = p
        .cli
        .with(|world| {
            let mut world = *world;
            world.with.host = &repo.host;
            prune_branches(&world, &repo.path, false, false)
        })
        .unwrap();
    let [r] = &pruned[..] else { panic!("{pruned:?}") };
    assert_eq!((r.local.as_str(), r.remote.as_str(), r.cleanup.as_deref()), ("deleted", "deleted", Some("deleted")));
    assert_eq!(repo.git(&["for-each-ref", "refs/heads/orchestrator/", "refs/remotes/origin/orchestrator/"]), "");
    assert_eq!(repo.host.remote_branch_commit(&repo.path, "orchestrator/x-a1b2c3").unwrap(), None);
    let saved = Json::parse(&repo.host.read(&path).unwrap().unwrap()).unwrap();
    assert_eq!(saved.get("branch_cleanup"), Some(&Json::from("deleted")));
}
