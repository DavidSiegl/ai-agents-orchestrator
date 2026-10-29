//! close, through main's command on the CLI fixture: a FakeHost and a FakeHerdr whose workspace w1 is open. Python's
//! TestClose, case for case, but test_resume_refuses_a_closed_run_even_with_force, which is
//! engine/src/cli/start/tests.rs' a_closed_run_is_not_resumed_even_with_force (resumable_state's); and
//! TestHostGit.test_close_on_real_git, on a real repository with a worktree. test_on_a_machine, which checks that main
//! connects to --machine's herdr, is a check of target parsing here: the fixture is the connection. Where Python
//! replaces close_workspace, the Rust case scripts the FakeHerdr: its close_error, and its before_call to save the
//! state meanwhile.

use std::rc::Rc;

use super::*;
use crate::cli::fixture::Cli;
use crate::cli::{Command, RunsCommand, Target};
use crate::constants::{APPROVE, EXIT_ERROR, HEARTBEAT_SECONDS, STALE_SECONDS, WORKTREES_DIR};
use crate::herdr::fake::Call;
use crate::herdr::HerdrError;
use crate::host::tests::Repo;
use crate::host::Host;
use crate::py::json::Map;
use crate::test_support::resumable;
use crate::workflow::fixture::saved_run;

const CLOSE_ID: &str = "20261001-120000-a1b2c3";
const CLOSE_BRANCH: &str = "orchestrator/x-a1b2c3";

fn close_state() -> String {
    format!("/proj/.orchestrator/runs/{CLOSE_ID}/state.json")
}

fn worktree() -> String {
    format!("/proj/{WORKTREES_DIR}/a1b2c3")
}

/// TestClose's world: the CLI fixture, workspace w1 open, no live pid.
struct Close {
    cli: Cli,
}

/// Fields put over add's defaults.
type Fields<'a> = &'a [(&'a str, Json)];

impl Close {
    fn new() -> Close {
        let mut cli = Cli::new();
        cli.f.herdr.state().workspaces.insert("w1".into());
        cli.f.pid_alive = Box::new(|_| false);
        Close { cli }
    }

    /// A run in workspace w1 that failed, a --worktree run unless worktree is false, whose worktree holds a.py
    /// uncommitted and whose branch is at its base, saved age seconds ago with fields; returns its saved state.
    fn add(&self, age: u64, worktree: bool, fields: Fields) -> Map<Json> {
        let mut state = RunState::new(CLOSE_ID, "a task", "/proj");
        state.phase = "build".into();
        state.round = 1;
        state.workspace_id = "w1".into();
        state.base = Some("base1".into());
        state.base_branch = Some("main".into());
        state.branch = Some(CLOSE_BRANCH.into());
        state.error = Some("interrupted".into());
        state.worktree = worktree.then(self::worktree);
        let Json::Object(mut saved) = state.to_json() else { unreachable!("a state is saved as an object") };
        for (key, value) in fields {
            saved.insert(*key, value.clone());
        }
        let mut host = self.cli.f.host.state();
        host.files.insert(close_state(), format!("{}\n", Json::Object(saved.clone()).dumps()));
        host.ages.insert(CLOSE_ID.into(), age);
        if worktree {
            host.worktrees.insert(self::worktree(), CLOSE_BRANCH.into());
            host.changed.insert(format!("{}/a.py", self::worktree()));
            host.branches.insert(CLOSE_BRANCH, "base1".into());
        } else {
            host.branch = CLOSE_BRANCH.into();
            host.changed.insert("/proj/a.py".into());
        }
        host.ahead.insert(CLOSE_BRANCH.into(), 0);
        saved
    }

    /// main's close of run_ref (a1b2c3 by default) and more flags in /proj: its exit status and standard output.
    fn close(&self, more: &[&str]) -> (i32, String) {
        let mut argv = vec!["close", "--cwd", "/proj"];
        argv.extend(if more.is_empty() { &["a1b2c3"][..] } else { more });
        let status = self.cli.main(&argv);
        (status, self.cli.take_out())
    }

    /// The error close gives.
    fn error(&self) -> String {
        self.cli.error(&["close", "--cwd", "/proj", "a1b2c3"])
    }

    fn saved(&self) -> Map<Json> {
        self.cli.saved(CLOSE_ID).as_object().cloned().expect("an object")
    }

    fn closed(&self) -> Json {
        self.saved().get("closed").cloned().unwrap_or(Json::Null)
    }

    /// Everything of the project close might change: files, worktrees, branches and uncommitted changes.
    fn project(&self) -> String {
        let h = self.cli.f.host.state();
        format!("{:?}", (h.files.clone(), h.worktrees.clone(), h.branches.clone(), h.changed.clone()))
    }

    fn calls(&self) -> Vec<Call> {
        self.cli.f.herdr.state().calls.clone()
    }

    fn assert_closed(&self, line: &str) {
        assert_eq!(self.closed(), Json::from(line));
        assert_eq!(self.cli.f.host.state().mtime_kept, [close_state()]);
    }
}

fn closing_w1() -> Vec<Call> {
    vec![Call::Close { workspace: "w1".into() }]
}

fn me() -> Json {
    Json::Object(
        [("host", Json::from("here")), ("pid", Json::from(4242)), ("started_at", Json::from("2026-09-30T07:00:00+00:00"))]
            .into_iter()
            .collect(),
    )
}

#[test]
fn running_run_is_refused_untouched() {
    let c = Close::new();
    c.add(0, true, &[("error", Json::Null), ("owner", me())]);
    let before = c.project();
    assert_eq!(c.close(&[]), (EXIT_ERROR, String::new()));
    assert_eq!(
        c.error(),
        format!("run {CLOSE_ID} is still running (pid 4242 on here, beat 0s ago); stop it before closing it")
    );
    assert!(c.calls().is_empty());
    assert!(c.cli.f.host.state().git_calls.is_empty() && c.cli.f.host.state().writes.is_empty());
    assert_eq!(c.project(), before);
}

#[test]
fn failed_worktree_run_loses_workspace_worktree_and_branch() {
    let c = Close::new();
    c.add(0, true, &[]);
    let (status, out) = c.close(&[]);
    assert_eq!(status, 0);
    let wt = worktree();
    assert_eq!(
        out,
        format!(
            "closed workspace w1\ndiscarding in {wt}:\n    ?? {wt}/a.py\ndeleted worktree {wt}\n\
             deleted branch {CLOSE_BRANCH}\nclosed run {CLOSE_ID}: workspace closed; worktree deleted; branch deleted\n"
        )
    );
    assert_eq!(c.calls(), closing_w1());
    {
        let h = c.cli.f.host.state();
        let removal = ("/proj".to_string(), ["worktree", "remove", "--force", &wt].map(String::from).to_vec());
        assert!(h.git_log.contains(&removal), "{:?}", h.git_log);
        assert!(h.worktrees.is_empty() && h.changed.is_empty() && h.branches.is_empty());
    }
    c.assert_closed("workspace closed; worktree deleted; branch deleted");
}

#[test]
fn a_clean_worktree_lists_nothing_to_discard() {
    let c = Close::new();
    c.add(0, true, &[]);
    c.cli.f.host.state().changed.clear();
    assert!(!c.close(&[]).1.contains("discarding"));
}

#[test]
fn branch_with_commits_beyond_base_is_kept() {
    for (n, why) in [(1, "1 commit beyond base"), (3, "3 commits beyond base")] {
        let c = Close::new();
        c.add(0, true, &[]);
        c.cli.f.host.state().ahead.insert(CLOSE_BRANCH.into(), n);
        let (status, out) = c.close(&[]);
        assert_eq!(status, 0);
        assert!(out.contains(&format!("kept branch {CLOSE_BRANCH}: {why}\n")), "{out}");
        assert!(c.cli.f.host.state().branches.contains_key(CLOSE_BRANCH));
        assert!(c.cli.f.host.state().worktrees.is_empty());
        c.assert_closed(&format!("workspace closed; worktree deleted; branch kept: {why}"));
    }
}

#[test]
fn branch_of_a_run_without_a_base_is_kept() {
    let c = Close::new();
    c.add(0, true, &[("base", Json::Null)]);
    c.close(&[]);
    assert!(c.cli.f.host.state().branches.contains_key(CLOSE_BRANCH));
    c.assert_closed("workspace closed; worktree deleted; branch kept: the run has no base to compare it with");
}

#[test]
fn stale_runs_are_cleaned_up_as_failed_ones() {
    let cases: [(u64, Vec<(&str, Json)>); 2] =
        [(STALE_SECONDS + 1, vec![("error", Json::Null)]), (HEARTBEAT_SECONDS + 1, vec![("error", Json::Null), ("owner", me())])];
    for (age, fields) in cases {
        let c = Close::new();
        c.add(age, true, &fields);
        assert_eq!(c.close(&[]).0, 0);
        assert_eq!(c.calls(), closing_w1());
        assert!(c.cli.f.host.state().worktrees.is_empty() && c.cli.f.host.state().branches.is_empty());
        c.assert_closed("workspace closed; worktree deleted; branch deleted");
    }
}

#[test]
fn failed_in_place_run_only_loses_its_workspace() {
    let c = Close::new();
    c.add(0, false, &[]);
    assert_eq!(c.close(&[]), (0, format!("closed workspace w1\nclosed run {CLOSE_ID}: workspace closed\n")));
    assert_eq!(c.calls(), closing_w1());
    {
        let h = c.cli.f.host.state();
        assert!(h.git_calls.is_empty());
        assert_eq!(h.branch, CLOSE_BRANCH);
        assert_eq!(h.changed.iter().collect::<Vec<_>>(), ["/proj/a.py"]);
    }
    c.assert_closed("workspace closed");
}

#[test]
fn finished_no_pr_worktree_run_keeps_its_worktree() {
    let c = Close::new();
    c.add(0, true, &[("phase", Json::from("done")), ("error", Json::Null), ("verdict", Json::from(APPROVE))]);
    let (status, out) = c.close(&[]);
    assert_eq!(status, 0);
    let wt = worktree();
    assert!(out.contains(&format!("worktree kept: {wt}")), "{out}");
    assert_eq!(c.calls(), closing_w1());
    {
        let h = c.cli.f.host.state();
        assert_eq!(h.worktrees.get(&wt).map(String::as_str), Some(CLOSE_BRANCH));
        assert_eq!(h.changed.iter().cloned().collect::<Vec<_>>(), [format!("{wt}/a.py")]);
        assert!(h.branches.contains_key(CLOSE_BRANCH));
    }
    c.assert_closed(&format!("workspace closed; worktree kept: {wt}"));
}

#[test]
fn finished_worktree_run_whose_worktree_went_with_its_pull_request() {
    let c = Close::new();
    let pr = ("pr_url", Json::from("https://github.com/o/r/pull/7"));
    c.add(0, true, &[("phase", Json::from("done")), ("error", Json::Null), ("verdict", Json::from(APPROVE)), pr]);
    c.cli.f.host.state().worktrees = Map::new();
    c.close(&[]);
    c.assert_closed("workspace closed");
}

#[test]
fn a_workspace_herdr_lacks_is_already_gone() {
    for workspace in ["w1", ""] {
        let c = Close::new();
        c.cli.f.herdr.state().workspaces.clear();
        c.add(0, true, &[("workspace_id", Json::from(workspace))]);
        let (status, out) = c.close(&[]);
        assert_eq!(status, 0);
        assert!(!out.contains("closed workspace"));
        assert!(c.calls().is_empty());
        c.assert_closed("workspace already gone; worktree deleted; branch deleted");
    }
}

#[test]
fn a_worktree_already_gone_still_has_its_branch_deleted() {
    let c = Close::new();
    c.add(0, true, &[]);
    c.cli.f.host.state().worktrees = Map::new();
    c.close(&[]);
    assert!(c.cli.f.host.state().branches.is_empty());
    c.assert_closed("workspace closed; worktree already gone; branch deleted");

    let c = Close::new();
    c.add(0, true, &[]);
    c.cli.f.host.state().worktrees = Map::new();
    c.cli.f.host.state().branches = Map::new();
    c.close(&[]);
    c.assert_closed("workspace closed; worktree already gone");
}

#[test]
fn second_close_only_prints_the_record() {
    let c = Close::new();
    c.add(0, true, &[]);
    c.close(&[]);
    c.cli.f.herdr.state().calls.clear();
    c.cli.f.host.state().git_calls.clear();
    let writes = c.cli.f.host.state().writes.clone();
    assert_eq!(
        c.close(&[]),
        (0, format!("run {CLOSE_ID} is already closed: workspace closed; worktree deleted; branch deleted\n"))
    );
    assert!(c.calls().is_empty() && c.cli.f.host.state().git_calls.is_empty());
    assert_eq!(c.cli.f.host.state().writes, writes);
}

#[test]
fn resume_of_a_closed_run_exits_1() {
    let c = Close::new();
    c.add(0, true, &[]);
    c.close(&[]);
    for flags in [&[][..], &["--force"][..]] {
        let argv: Vec<&str> = ["resume", "--cwd", "/proj", "a1b2c3"].iter().chain(flags).copied().collect();
        assert_eq!(c.cli.main(&argv), EXIT_ERROR);
        assert!(c.cli.error(&argv).contains(&format!("run {CLOSE_ID} was closed; start a new run")));
    }
}

#[test]
fn state_saved_before_close_existed_loads_and_resumes() {
    let Json::Object(mut saved) = saved_run("build", 1, &["spec", "build"]).to_json() else { unreachable!() };
    saved.remove("closed");
    assert_eq!(resumable(&[], &saved).unwrap().closed, None);
}

#[test]
fn state_saved_meanwhile_is_not_overwritten() {
    let c = Close::new();
    c.add(0, true, &[]);
    // As the workspace closes.
    let host = Rc::clone(&c.cli.f.host);
    c.cli.f.herdr.state().before_call = Some(Box::new(move |call| {
        if matches!(call, Call::Close { .. }) {
            let text = host.state().files.get(&close_state()).cloned().unwrap();
            let Ok(Json::Object(mut saved)) = Json::parse(&text) else { unreachable!() };
            saved.insert("round", Json::from(2));
            host.state().files.insert(close_state(), Json::Object(saved).dumps());
        }
    }));
    assert_eq!(c.close(&[]).0, EXIT_ERROR);
    let saved = c.saved();
    assert_eq!((saved.get("round"), saved.get("closed")), (Some(&Json::from(2)), Some(&Json::Null)));
}

#[test]
fn the_error_of_a_state_saved_meanwhile_names_the_file() {
    let c = Close::new();
    c.add(0, true, &[]);
    let host = Rc::clone(&c.cli.f.host);
    c.cli.f.herdr.state().before_call = Some(Box::new(move |_| {
        let path = close_state();
        let text = host.state().files.get(&path).cloned().unwrap();
        host.state().files.insert(path, text.replace("\"round\": 1", "\"round\": 2"));
    }));
    assert_eq!(c.error(), format!("{} changed while the run was closed; closed is not recorded", close_state()));
}

#[test]
fn herdr_failure_records_nothing() {
    let c = Close::new();
    c.add(0, true, &[]);
    c.cli.f.herdr.state().close_error = Some(HerdrError::new("server_error", "herdr is down"));
    assert_eq!(c.close(&[]).0, EXIT_ERROR);
    assert!(c.error().contains("herdr is down"));
    assert!(c.cli.f.host.state().git_calls.is_empty() && c.cli.f.host.state().writes.is_empty());
    assert_eq!(c.closed(), Json::Null);
}

#[test]
fn git_failure_records_nothing_and_a_second_close_finishes() {
    let c = Close::new();
    c.add(0, true, &[]);
    c.cli.f.host.state().remove_error = Some("fatal: cannot remove a locked working tree".into());
    assert_eq!(c.close(&[]).0, EXIT_ERROR);
    // A second close, which fails as the first did, the workspace gone with the first.
    assert!(c.error().contains("cannot remove a locked working tree"));
    assert!(c.cli.f.host.state().writes.is_empty());
    assert_eq!(c.closed(), Json::Null);
    assert!(c.cli.f.host.state().branches.contains_key(CLOSE_BRANCH));

    c.cli.f.host.state().remove_error = None;
    assert_eq!(c.close(&[]).0, 0);
    c.assert_closed("workspace already gone; worktree deleted; branch deleted");
}

#[test]
fn on_a_machine() {
    let c = Close::new();
    c.add(0, true, &[]);
    assert_eq!(c.close(&["a1b2c3", "--machine", "m"]).0, 0);
    let target = Target { machine: Some("m".into()), cwd: Some("/proj".into()) };
    assert_eq!(c.cli.command(&["close", "--cwd", "/proj", "a1b2c3", "--machine", "m"]).target(), Some(&target));
}

#[test]
fn args() {
    let c = Cli::new();
    let target = Target { machine: Some("m".into()), cwd: Some("~/proj".into()) };
    assert_eq!(
        c.command(&["close", "a1b2c3", "--machine", "m", "--cwd", "~/proj"]),
        Command::Runs(target, RunsCommand::Close("a1b2c3".into()))
    );
    for argv in [&["close", "a1b2c3", "--machine", "m"][..], &["close"][..]] {
        let refused = crate::cli::parse_args(argv.iter().map(std::ffi::OsString::from), &c.config).unwrap_err();
        assert_eq!(refused.exit_code(), 2, "{argv:?}");
    }
}

// -- TestHostGit.test_close_on_real_git ------------------------------------------------------------------------------

#[test]
fn close_on_real_git() {
    // A failed --worktree run: its dirty worktree is removed by force, and its branch, at base, deleted.
    let repo = Repo::new(&[("app.py", "one\n"), (".orchestrator/.gitignore", "*\n")]);
    let base = repo.base().to_string();
    let wt = format!("{}/{WORKTREES_DIR}/a1b2c3", repo.path);
    repo.host.add_worktree(&repo.path, &wt, &base).unwrap();
    repo.host.git(&wt, &["switch", "--quiet", "-c", CLOSE_BRANCH]).unwrap();
    crate::host::tests::put(&format!("{wt}/app.py"), "changed\n");
    crate::host::tests::put(&format!("{wt}/new.py"), "new\n");
    let mut state = RunState::new(CLOSE_ID, "x", &repo.path);
    state.phase = "build".into();
    state.round = 1;
    state.workspace_id = "w1".into();
    state.base = Some(base);
    state.base_branch = Some("main".into());
    state.branch = Some(CLOSE_BRANCH.into());
    state.error = Some("interrupted".into());
    state.worktree = Some(wt.clone());
    let path = format!("{}/state.json", state.dir());
    repo.host.write(&path, &state.to_text(), false).unwrap();
    crate::host::tests::set_mtime(&path, 1_000_000_000);
    let mut cli = Cli::new();
    cli.f.pid_alive = Box::new(|_| false);
    let closed = cli
        .with(|world| {
            let mut world = *world;
            world.with.host = &repo.host;
            close_run(&world, &repo.path, "a1b2c3")
        })
        .unwrap();
    assert_eq!(closed, "workspace already gone; worktree deleted; branch deleted");
    let out = cli.take_out();
    assert!(out.contains(&format!("discarding in {wt}:\n     M app.py\n    ?? new.py\n")), "{out}");
    assert!(!std::path::Path::new(&wt).exists());
    assert!(!repo.git(&["worktree", "list", "--porcelain"]).contains(&wt));
    assert_eq!(repo.host.branch_commit(&repo.path, CLOSE_BRANCH).unwrap(), None);
    let saved = Json::parse(&repo.host.read(&path).unwrap().unwrap()).unwrap();
    assert_eq!(saved.get("closed"), Some(&Json::from(closed.as_str())));
    assert_eq!(repo.host.mtime(&path).unwrap(), 1_000_000_000);
    assert!(cli.f.herdr.state().calls.is_empty());
}
