//! tests/test_orchestrator.py's TestDrain, TestDrainWorkflows and TestPromptFiles.test_a_bad_file_stops_drain_before_any_run,
//! ported case for case: drain through main's dispatch (run_command), on the fixture's FakeHost, each run's
//! Workflow::run played by a script through Connected's workflow, as Python patches Workflow.run.
//!
//! Where Python patches sweep_branches, a case that asks whether drain swept gives the project a run whose sweep
//! asks gh about a pull request no other step asks about (Drain::sweepable). Two cases differ from Python's:
//! test_a_spec_deleted_before_its_run_line_is_written_is_not_run, which patches QueuedSpec.reread to delete the spec
//! between the reads, runs drain_spec on a spec deleted after it was read; and test_a_spec_that_cannot_be_written,
//! which patches os.replace, blocks the file written aside with a directory of its name, which fails as a full disk
//! would, for root too. test_a_relative_dir_is_the_shells is not ported: it changes the process's directory, which
//! the tests share, and read_queue reads DIR with std::fs, relative to that directory as Python's os.listdir does.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::ffi::OsString;
use std::rc::Rc;

use super::queue::*;
use super::*;
use crate::cli::fixture::Cli;
use crate::cli::{interrupted_note, run_command, Ran, RunWorkflow};
use crate::constants::{
    APPROVE, CHANGES_REQUESTED, EXIT_CHANGES_REQUESTED, EXIT_CONFLICT, EXIT_INTERRUPTED, FINISHED, HEARTBEAT_SECONDS,
    QUALITY_GATE_FAILED, STALE_SECONDS, WORKTREES_DIR,
};
use crate::env::ConfigDir;
use crate::host::fake::{FakePr, PrEdit};
use crate::host::Completed;
use crate::pipeline::Pipeline;
use crate::pull_request::conflict_warning;
use crate::py::json::Map;
use crate::spec_file::SpecFile;
use crate::state::RunState;
use crate::test_support::{TempDir, EXAMPLES};
use crate::workflow::fixture::{build_turn, review_turn};

const PR_A: &str = "https://github.com/o/r/pull/1";
const PR_B: &str = "https://github.com/o/r/pull/2";
/// The pull request of the run Drain::sweepable saves, which only the sweep of earlier runs' branches asks gh about.
const SWEPT_PR: &str = "https://github.com/o/r/pull/99";

/// A spec as drain reads it: a title, a Status: line unless status is None, the further header lines, a body.
fn spec_text(title: &str, status: Option<&str>, header: &[&str]) -> String {
    spec_text_nl(title, status, header, "\n")
}

fn spec_text_nl(title: &str, status: Option<&str>, header: &[&str], nl: &str) -> String {
    let mut lines = vec![format!("# {title}"), String::new()];
    if let Some(status) = status {
        lines.push(format!("Status: {status}"));
    }
    lines.extend(header.iter().map(|h| h.to_string()));
    lines.extend(["", "## Goal", "Status: in the body, which drain never reads", ""].map(String::from));
    lines.join(nl)
}

/// A ready spec without further header lines.
fn ready(title: &str) -> String {
    spec_text(title, Some(SPEC_READY), &[])
}

/// Fields of a saved state, each in place of the state's own.
type Fields = Vec<(&'static str, Json)>;

/// How one scripted run ends, given its state to change as the run would.
type Outcome = Box<dyn FnOnce(&mut RunState) -> Result<Option<String>, Stopped>>;

/// A run that opened the pull request pr, with conflicts, and approved.
fn approved(pr: &str, conflicts: &[&str]) -> Outcome {
    let (pr, conflicts): (String, Vec<String>) = (pr.into(), conflicts.iter().map(|c| c.to_string()).collect());
    Box::new(move |state| {
        (state.pr_url, state.conflicts, state.phase) = (Some(pr), conflicts, DONE.into());
        Ok(Some(APPROVE.into()))
    })
}

/// A run that opened PR_A and ended with verdict.
fn ended(verdict: &str) -> Outcome {
    let verdict = verdict.to_string();
    Box::new(move |state| {
        (state.pr_url, state.phase) = (Some(PR_A.into()), DONE.into());
        Ok(Some(verdict))
    })
}

/// A run that stopped so.
fn raising(stopped: Stopped) -> Outcome {
    Box::new(move |_| Err(stopped))
}

/// A run that does what before does to the world, then ends as outcome.
fn after(before: impl FnOnce() + 'static, outcome: Outcome) -> Outcome {
    Box::new(move |state| {
        before();
        outcome(state)
    })
}

/// How a drain ended: its exit status, standard output, and standard error, the log's lines first.
struct Drained {
    status: i32,
    out: String,
    err: String,
}

/// A world on a FakeHost whose project is /proj, with a directory of specs.
struct Drain {
    cli: Cli,
    dir: TempDir,
    /// Each run's state as its Workflow::run got it, and the spec it was given.
    runs: RefCell<Vec<(RunState, Option<SpecFile>)>>,
}

impl Drain {
    fn new() -> Drain {
        Drain { cli: Cli::new(), dir: TempDir::new(), runs: RefCell::new(vec![]) }
    }

    fn dir(&self) -> String {
        self.dir.path()
    }

    fn path(&self, slug: &str) -> String {
        format!("{}/{slug}/spec.md", self.dir())
    }

    fn spec(&self, slug: &str, text: &str) {
        crate::host::tests::put(&self.path(slug), text);
    }

    fn text(&self, slug: &str) -> String {
        std::fs::read_to_string(self.path(slug)).unwrap()
    }

    fn remove(&self, slug: &str) {
        std::fs::remove_file(self.path(slug)).unwrap();
    }

    /// The spec's Status:, Run: and PR: values, None for each it lacks.
    fn header(&self, slug: &str) -> (Option<String>, Option<String>, Option<String>) {
        let text = self.text(slug);
        let field = |k| header_field(&text, k);
        (field(STATUS_KEY), field(RUN_KEY), field(PR_KEY))
    }

    /// The spec's Status: and PR: values.
    fn status_pr(&self, slug: &str) -> (Option<String>, Option<String>) {
        let (status, _, pr) = self.header(slug);
        (status, pr)
    }

    /// Why drain finds the spec deleted, as read_spec_file says it.
    fn gone(&self, slug: &str) -> String {
        format!("cannot read spec file {}: No such file or directory", self.path(slug))
    }

    /// A run of the spec's, as an interrupted drain leaves it in the project's runs, saved age seconds ago.
    fn saved_run(&self, slug: &str, run_id: &str, age: u64, fields: &[(&str, Json)]) {
        let state = RunState::new(run_id, &format!("spec {slug}"), "/proj");
        let mut saved: Map<Json> = Json::parse(&state.to_text()).unwrap().as_object().unwrap().clone();
        for (key, value) in fields {
            saved.insert(*key, value.clone());
        }
        self.cli.save(&Json::Object(saved), age);
    }

    /// What gh says of the pull request at url: state, headRefOid and closedAt, with fields in place of those.
    fn pull_request(&self, url: &str, state: &str, fields: &[(&str, Json)]) {
        let mut pr: Map<Json> =
            [("state", Json::from(state)), ("headRefOid", Json::from("a".repeat(40))), ("closedAt", Json::Null)]
                .into_iter()
                .collect();
        for (key, value) in fields {
            pr.insert(*key, value.clone());
        }
        self.cli.f.host.state().pull_requests.insert(url.into(), FakePr::Prints(Json::Object(pr)));
    }

    /// Give the project a done run whose branch the sweep of earlier runs' branches would prune, so that whether the
    /// sweep ran shows in gh's calls (swept); its gh is logged out, so the sweep stops at it.
    fn sweepable(&self) {
        let fields = [
            ("phase", Json::from(DONE)),
            ("branch", Json::from("orchestrator/swept-5eeeee")),
            ("pr_url", Json::from(SWEPT_PR)),
        ];
        self.saved_run("swept", "20261001-000000-5eeeee", 10_000, &fields);
        let unusable = Completed { status: 4, stdout: String::new(), stderr: "not logged in".into() };
        self.cli.f.host.state().pull_requests.insert(SWEPT_PR.into(), FakePr::Fails(unusable));
    }

    /// Whether drain swept the branches of earlier runs (sweepable).
    fn swept(&self) -> bool {
        self.cli.f.host.state().gh_calls.iter().any(|c| c.get(2).map(String::as_str) == Some(SWEPT_PR))
    }

    /// The URL each gh command but the sweep's asked about, in order.
    fn gh_urls(&self) -> Vec<String> {
        let calls = self.cli.f.host.state().gh_calls.clone();
        calls.into_iter().filter_map(|c| c.get(2).cloned()).filter(|u| u != SWEPT_PR).collect()
    }

    /// main's exit status for drain with argv after DIR, each run ending by the next of outcomes; and what it printed.
    fn drain(&self, outcomes: Vec<Outcome>, argv: &[&str]) -> Drained {
        let dir = self.dir();
        let argv: Vec<&str> = ["drain", &dir, "--cwd", "/proj"].iter().copied().chain(argv.iter().copied()).collect();
        self.drain_argv(outcomes, &argv)
    }

    /// drain, the command line being argv.
    fn drain_argv(&self, outcomes: Vec<Outcome>, argv: &[&str]) -> Drained {
        let script = RefCell::new(VecDeque::from(outcomes));
        let workflow = |_: &Connected, state: RunState, pipeline: Pipeline, spec: Option<SpecFile>| {
            self.runs.borrow_mut().push((state.clone(), spec));
            let outcome = script.borrow_mut().pop_front().expect("a run scripted for each run drain starts");
            let mut state = state;
            let verdict = outcome(&mut state)?;
            Ok(Ran { verdict, state, pipeline })
        };
        let command = self.cli.command(argv);
        let ended = self.cli.with_workflow(&workflow as &RunWorkflow<'_>, |world| run_command(&command, world));
        assert!(script.borrow().is_empty(), "every scripted run ran");
        let mut err: String = self.cli.f.told.lines().iter().map(|l| format!("{l}\n")).collect();
        self.cli.f.told.log.borrow_mut().clear();
        err.push_str(&std::mem::take(&mut *self.cli.err.borrow_mut()));
        let status = match ended {
            Ok(Ended::Status(status)) => status,
            Ok(Ended::Ran(_)) => panic!("drain ended as a run"),
            Err(Stop::Failed(e)) => {
                err.push_str(&format!("error: {e}\n"));
                EXIT_ERROR
            }
            Err(Stop::Interrupted(run_id)) => {
                let target = command.target().map(crate::cli::Target::flags).unwrap_or_default();
                err.push_str(&format!("{}\n", interrupted_note(run_id.as_deref(), &target)));
                EXIT_INTERRUPTED
            }
        };
        Drained { status, out: self.cli.take_out(), err }
    }

    /// The task of each run drain started, in order.
    fn tasks(&self) -> Vec<String> {
        self.runs.borrow().iter().map(|(s, _)| s.task.clone()).collect()
    }

    /// The id of the n-th run drain started.
    fn run_id(&self, n: usize) -> String {
        self.runs.borrow()[n].0.run_id.clone()
    }

    fn run_conflicts(&self, run_id: &str) -> Json {
        self.cli.saved(run_id).get("conflicts").cloned().unwrap()
    }

    /// refreshed_headers of the queue as drain reads it, in /proj, with gh's calls before it forgotten.
    fn refreshed(&self) -> Vec<(usize, Entries)> {
        let specs = read_queue(&self.dir()).unwrap();
        self.cli.f.host.state().gh_calls.clear();
        self.cli.with(|world| refreshed_headers(&specs, world, "/proj", &[], false)).unwrap()
    }

    /// The URL of each gh command, each a `gh pr view`.
    fn views(&self) -> Vec<String> {
        let calls = self.cli.f.host.state().gh_calls.clone();
        calls
            .into_iter()
            .map(|c| {
                assert_eq!(&c[..2], ["pr", "view"]);
                c[2].clone()
            })
            .collect()
    }
}

fn some(text: &str) -> Option<String> {
    Some(text.to_string())
}

fn entries(pairs: &[(&'static str, &str)]) -> Entries {
    pairs.iter().map(|(k, v)| (*k, v.to_string())).collect()
}

#[test]
fn independent_specs_run_in_slug_order() {
    let d = Drain::new();
    d.sweepable();
    d.spec("b-second", &ready("Second"));
    d.spec("a-first", &ready("First"));
    let r = d.drain(vec![approved(PR_A, &[]), approved(PR_B, &[])], &[]);
    assert_eq!(r.status, 0, "{}", r.err);
    assert_eq!(d.tasks(), ["First", "Second"]);
    assert_eq!(d.header("a-first"), (some(SPEC_IN_REVIEW), Some(d.run_id(0)), some(PR_A)));
    assert_eq!(d.header("b-second"), (some(SPEC_IN_REVIEW), Some(d.run_id(1)), some(PR_B)));
    let tail = format!(
        "a-first: ready-for-agent -> in-review  {PR_A}\nb-second: ready-for-agent -> in-review  {PR_B}\nall specs cleared\n"
    );
    assert!(r.out.ends_with(&tail), "{}", r.out);
    assert!(d.swept());
}

#[test]
fn each_run_is_a_run_spec_run_of_its_spec() {
    let d = Drain::new();
    d.spec("a", &ready("First"));
    d.drain(vec![approved(PR_A, &[])], &[]);
    let (state, given) = d.runs.borrow()[0].clone();
    assert_eq!(given, Some(SpecFile { path: d.path("a"), text: ready("First") }));
    assert_eq!((state.phase.as_str(), state.pull_request, state.cwd.as_str()), ("build", true, "/proj"));
}

#[test]
fn the_run_line_is_written_before_the_run() {
    let d = Rc::new(Drain::new());
    d.spec("a", &ready("First"));
    let seen = Rc::new(RefCell::new(vec![]));
    let (dd, s) = (Rc::clone(&d), Rc::clone(&seen));
    d.drain(vec![after(move || s.borrow_mut().push(dd.header("a")), approved(PR_A, &[]))], &[]);
    assert_eq!(*seen.borrow(), [(some(SPEC_READY), Some(d.run_id(0)), None)]);
}

#[test]
fn an_edit_made_while_a_spec_runs_is_kept() {
    let d = Rc::new(Drain::new());
    d.spec("a", &ready("First"));
    d.spec("b", &ready("Second"));
    let dd = Rc::clone(&d);
    let edit = move || {
        // The human adds to a's body, and holds b back, while a runs.
        let path = dd.path("a");
        let text = std::fs::read_to_string(&path).unwrap();
        std::fs::write(&path, format!("{text}A note added during the run.\n")).unwrap();
        dd.spec("b", &spec_text("Second", Some("needs-info"), &[]));
    };
    let r = d.drain(vec![after(edit, approved(PR_A, &[]))], &[]);
    assert_eq!(r.status, 0);
    assert_eq!(d.runs.borrow().len(), 1);
    assert!(d.text("a").ends_with("A note added during the run.\n"));
    assert_eq!(d.status_pr("a"), (some(SPEC_IN_REVIEW), some(PR_A)));
    assert_eq!(d.text("b"), spec_text("Second", Some("needs-info"), &[]));
}

#[test]
fn a_spec_deleted_while_another_runs_is_skipped() {
    let d = Rc::new(Drain::new());
    for (slug, title) in [("a", "First"), ("b", "Second"), ("c", "Third")] {
        d.spec(slug, &ready(title));
    }
    let dd = Rc::clone(&d);
    let r = d.drain(vec![after(move || dd.remove("b"), approved(PR_A, &[])), approved(PR_B, &[])], &[]);
    // b was ready-for-agent when drain last read it, so it is left, gone, and the drain does not end cleared.
    assert_eq!(r.status, EXIT_WAITING);
    assert_eq!(d.tasks(), ["First", "Third"]);
    assert_eq!(r.err.matches(&format!("b: {}; not running it\n", d.gone("b"))).count(), 1, "{}", r.err);
    assert!(!r.out.contains("all specs cleared"));
    let tail = format!("c: ready-for-agent -> in-review  {PR_B}\nb: gone ({})\n", d.gone("b"));
    assert!(r.out.ends_with(&tail), "{}", r.out);
}

#[test]
fn the_running_spec_deleted_during_its_run_is_not_recorded() {
    let d = Rc::new(Drain::new());
    d.spec("a", &ready("First"));
    d.spec("b", &ready("Second"));
    let dd = Rc::clone(&d);
    let r = d.drain(vec![after(move || dd.remove("a"), approved(PR_A, &[])), approved(PR_B, &[])], &[]);
    assert_eq!(r.status, EXIT_WAITING);
    assert_eq!(d.runs.borrow().len(), 2);
    assert!(!std::path::Path::new(&d.path("a")).exists());
    let line = format!("a: {}; not recording Status: in-review, PR: {PR_A}\n", d.gone("a"));
    assert!(r.err.contains(&line), "{}", r.err);
    assert!(!r.err.contains("not running it"));
    assert_eq!(d.status_pr("b"), (some(SPEC_IN_REVIEW), some(PR_B)));
    let tail =
        format!("a: ready-for-agent -> gone\nb: ready-for-agent -> in-review  {PR_B}\na: gone ({})\n", d.gone("a"));
    assert!(r.out.ends_with(&tail), "{}", r.out);
}

#[test]
fn the_running_spec_emptied_during_a_failed_run_is_not_recorded() {
    let d = Rc::new(Drain::new());
    d.spec("a", &ready("First"));
    let dd = Rc::clone(&d);
    let failed = raising(Stopped::Failed(OrchestratorError::new("the Builder's pane is gone")));
    let r = d.drain(vec![after(move || dd.spec("a", ""), failed)], &[]);
    let run_id = d.run_id(0);
    let why = format!("spec file {} is empty", d.path("a"));
    assert_eq!(r.status, EXIT_ERROR);
    assert_eq!(d.text("a"), "");
    assert!(r.err.contains("error: the Builder's pane is gone\n"), "{}", r.err);
    assert!(r.err.contains(&format!("orchestrator.py resume {}", run_id.rsplit('-').next().unwrap())), "{}", r.err);
    assert!(r.err.contains(&format!("a: {why}; not recording Status: ready-for-human\n")), "{}", r.err);
    assert!(!r.err.contains("a is ready-for-human"));
    assert_eq!(r.out, "a: ready-for-agent -> gone\n");
}

#[test]
fn a_spec_deleted_before_its_run_line_is_written_is_not_run() {
    let d = Drain::new();
    d.spec("a", &ready("First"));
    let mut specs = read_queue(&d.dir()).unwrap();
    // The human deletes the spec after drain read it, just before drain_spec writes its Run: line.
    d.remove("a");
    let args = match d.cli.command(&["drain", &d.dir(), "--cwd", "/proj"]) {
        crate::cli::Command::Drain(args) => args,
        other => panic!("{other:?}"),
    };
    let workflow = |_: &Connected, _: RunState, _: Pipeline, _: Option<SpecFile>| -> Result<Ran, Stopped> {
        panic!("a run started")
    };
    let ran = d.cli.with_workflow(&workflow, |world| drain_spec(&args, world, "/proj", &mut specs[0]));
    assert!(matches!(ran, Ok(None)));
    let log = d.cli.f.told.lines();
    let [line] = &log[..] else { panic!("{log:?}") };
    let prefix = format!("a: {}; not recording Run: ", d.gone("a"));
    let run_id = line.strip_prefix(&prefix).unwrap_or_else(|| panic!("{line}"));
    assert_eq!(run_id.len(), "20261024-100000-aaaaaa".len(), "{line}");
    assert!(!specs[0].changed);
    let by_slug = [("a".to_string(), 0)].into_iter().collect();
    let status = d.cli.with(|world| drain_end(&specs, &by_slug, world));
    assert_eq!((status, d.cli.take_out()), (EXIT_WAITING, format!("a: gone ({})\n", d.gone("a"))));
}

#[test]
fn a_blocker_deleted_mid_drain_is_shown_gone_without_its_pull_request() {
    let d = Rc::new(Drain::new());
    d.spec("a", &ready("First"));
    d.spec("b", &spec_text("Second", Some(SPEC_IN_REVIEW), &[&format!("PR: {PR_B}")]));
    d.spec("c", &spec_text("Third", Some(SPEC_READY), &["Blocked by: b"]));
    d.pull_request(PR_B, "OPEN", &[]);
    let dd = Rc::clone(&d);
    let r = d.drain(vec![after(move || dd.remove("b"), approved(PR_A, &[]))], &[]);
    assert_eq!(r.status, EXIT_WAITING);
    assert_eq!(r.err.matches(&format!("b: {}; gone\n", d.gone("b"))).count(), 1, "{}", r.err);
    assert!(!r.err.contains("not running it"));
    assert!(r.out.ends_with(&format!("c waits on:\n    b: gone ({})\n", d.gone("b"))), "{}", r.out);
    assert!(!r.out.contains(PR_B));
}

#[test]
fn a_done_spec_deleted_mid_drain_is_logged_once() {
    let d = Rc::new(Drain::new());
    d.spec("a", &ready("First"));
    d.spec("b", &ready("Second"));
    d.spec("c", &spec_text("Third", Some(SPEC_DONE), &[]));
    let dd = Rc::clone(&d);
    let r = d.drain(vec![after(move || dd.remove("c"), approved(PR_A, &[])), approved(PR_B, &[])], &[]);
    assert_eq!(r.status, 0);
    assert_eq!(r.err.matches("c: ").count(), 1, "{}", r.err);
    assert!(r.err.contains(&format!("c: {}; gone\n", d.gone("c"))));
    assert!(!r.err.contains("not running it"));
    assert!(r.out.ends_with("all specs cleared\n"), "{}", r.out);
}

#[test]
fn a_spec_emptied_then_restored_is_run() {
    let d = Rc::new(Drain::new());
    for (slug, title) in [("a", "First"), ("b", "Second"), ("c", "Third")] {
        d.spec(slug, &ready(title));
    }
    let (d1, d2) = (Rc::clone(&d), Rc::clone(&d));
    let r = d.drain(
        vec![
            after(move || d1.spec("b", ""), approved(PR_A, &[])),
            after(move || d2.spec("b", &ready("Second")), approved(PR_B, &[])),
            approved(PR_B, &[]),
        ],
        &[],
    );
    assert_eq!(r.status, 0);
    assert_eq!(d.tasks(), ["First", "Third", "Second"]);
    let line = format!("b: spec file {} is empty; not running it\n", d.path("b"));
    assert_eq!(r.err.matches(&line).count(), 1, "{}", r.err);
    assert_eq!(d.status_pr("b"), (some(SPEC_IN_REVIEW), some(PR_B)));
    let tail =
        format!("b: ready-for-agent -> in-review  {PR_B}\nc: ready-for-agent -> in-review  {PR_B}\nall specs cleared\n");
    assert!(r.out.ends_with(&tail), "{}", r.out);
}

#[test]
fn a_gone_spec_change_line_ends_gone() {
    let d = Drain::new();
    let file = SpecFile { path: d.path("a"), text: ready("A") };
    let was = some(SPEC_READY);
    let mut spec = QueuedSpec { slug: "a".into(), file, blockers: vec![], was, changed: false, gone: None };
    let told = crate::workflow::fixture::Told::default();
    assert!(spec.update(&entries(&[(STATUS_KEY, SPEC_IN_REVIEW), (PR_KEY, PR_A)]), false, &told).unwrap());
    assert!(!spec.reread());
    assert_eq!(spec.change_line(), "a: ready-for-agent -> gone");
}

#[test]
fn a_blocked_by_line_naming_no_blocker() {
    for line in ["Blocked by: -", "Blocked by: none", "Blocked by: None"] {
        let d = Drain::new();
        d.spec("a", &spec_text("First", Some(SPEC_READY), &[line]));
        let r = d.drain(vec![approved(PR_A, &[])], &[]);
        assert_eq!((r.status, d.runs.borrow().len()), (0, 1), "{line}");
    }
}

#[test]
fn a_run_taken_over_leaves_its_spec_for_the_next_drain() {
    let d = Drain::new();
    d.spec("a", &ready("First"));
    let taken = Stopped::TakenOver(OrchestratorError::new("run x was taken over by host 1"));
    let r = d.drain(vec![raising(taken)], &[]);
    assert_eq!(r.status, EXIT_ERROR);
    let (status, run, _) = d.header("a");
    assert_eq!((status, run), (some(SPEC_READY), Some(d.run_id(0))));
    assert!(r.err.contains("error: run x was taken over by host 1\n"), "{}", r.err);
    assert!(r.err.contains("a stays ready-for-agent; run drain again once that run has ended"), "{}", r.err);
    assert!(!r.err.contains("resume its run"));
}

#[test]
fn a_symlinked_spec_stays_a_link_and_keeps_its_mode() {
    use std::os::unix::fs::PermissionsExt;
    let d = Drain::new();
    let target = format!("{}/elsewhere.md", d.dir());
    std::fs::write(&target, ready("First")).unwrap();
    std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o600)).unwrap();
    std::fs::create_dir(format!("{}/a", d.dir())).unwrap();
    std::os::unix::fs::symlink(&target, d.path("a")).unwrap();
    d.drain(vec![approved(PR_A, &[])], &[]);
    assert!(std::fs::symlink_metadata(d.path("a")).unwrap().file_type().is_symlink());
    assert_eq!(d.status_pr("a"), (some(SPEC_IN_REVIEW), some(PR_A)));
    assert_eq!(std::fs::metadata(&target).unwrap().permissions().mode() & 0o777, 0o600);
}

#[test]
fn a_blocked_spec_waits_for_its_blockers_pull_request() {
    let d = Drain::new();
    d.spec("a", &ready("First"));
    d.spec("b", &spec_text("Second", Some(SPEC_READY), &["Blocked by: a"]));
    let r = d.drain(vec![approved(PR_A, &[])], &[]);
    assert_eq!(r.status, EXIT_WAITING);
    assert_eq!(d.runs.borrow().len(), 1);
    assert_eq!(d.status_pr("a"), (some(SPEC_IN_REVIEW), some(PR_A)));
    assert_eq!(d.header("b"), (some(SPEC_READY), None, None));
    assert!(r.out.ends_with(&format!("b waits on:\n    a: in-review  {PR_A}\n")), "{}", r.out);
}

#[test]
fn the_next_drain_follows_the_blockers_pull_request() {
    let cases: [(&str, bool, i32, &str, &str); 3] = [
        ("MERGED", true, 0, SPEC_DONE, SPEC_IN_REVIEW),
        ("OPEN", false, EXIT_WAITING, SPEC_IN_REVIEW, SPEC_READY),
        ("CLOSED", false, EXIT_WAITING, SPEC_FOR_HUMAN, SPEC_READY),
    ];
    for (pr_state, runs, exit_code, a_after, b_after) in cases {
        let d = Drain::new();
        let header = ["Run: 20261024-100000-aaaaaa", &format!("PR: {PR_A}")];
        d.spec("a", &spec_text("First", Some(SPEC_IN_REVIEW), &header));
        d.spec("b", &spec_text("Second", Some(SPEC_READY), &["Blocked by: a/spec.md"]));
        d.pull_request(PR_A, pr_state, &[]);
        let outcomes = if runs { vec![approved(PR_B, &[])] } else { vec![] };
        let r = d.drain(outcomes, &[]);
        assert_eq!(r.status, exit_code, "{pr_state}: {}", r.err);
        assert_eq!((d.header("a").0, d.header("b").0), (some(a_after), some(b_after)), "{pr_state}");
        assert_eq!(d.runs.borrow().len(), usize::from(runs));
        if pr_state == "OPEN" {
            assert!(!r.out.contains("->"), "{}", r.out);
        } else {
            assert!(r.out.contains(&format!("a: in-review -> {a_after}  {PR_A}\n")), "{}", r.out);
        }
    }
}

#[test]
fn a_run_that_does_not_succeed_stops_the_drain() {
    let cases: [(fn() -> Outcome, i32); 3] = [
        (|| ended(CHANGES_REQUESTED), EXIT_CHANGES_REQUESTED),
        (|| ended(QUALITY_GATE_FAILED), EXIT_CHANGES_REQUESTED),
        (|| approved(PR_A, &["x.py"]), EXIT_CONFLICT),
    ];
    for (outcome, exit_code) in cases {
        let d = Drain::new();
        d.spec("a", &ready("First"));
        d.spec("b", &ready("Second"));
        let r = d.drain(vec![outcome()], &[]);
        assert_eq!(r.status, exit_code);
        assert_eq!(d.header("a"), (some(SPEC_FOR_HUMAN), Some(d.run_id(0)), some(PR_A)));
        assert_eq!(d.header("b"), (some(SPEC_READY), None, None));
        assert!(r.out.contains(&format!("a: ready-for-agent -> ready-for-human  {PR_A}\n")), "{}", r.out);
        assert!(!r.out.contains("all specs cleared"));
    }
}

#[test]
fn a_failed_run_stops_the_drain_with_its_resume_command() {
    let d = Drain::new();
    d.spec("a", &ready("First"));
    d.spec("b", &ready("Second"));
    let r = d.drain(vec![raising(Stopped::Failed(OrchestratorError::new("the Builder's pane is gone")))], &[]);
    let run_id = d.run_id(0);
    assert_eq!(r.status, EXIT_ERROR);
    assert_eq!(d.header("a"), (some(SPEC_FOR_HUMAN), Some(run_id.clone()), None));
    assert_eq!(d.header("b"), (some(SPEC_READY), None, None));
    assert!(r.err.contains("error: the Builder's pane is gone\n"), "{}", r.err);
    assert!(r.err.contains(&format!("orchestrator.py resume {}", run_id.rsplit('-').next().unwrap())), "{}", r.err);
    assert!(r.err.contains("a is ready-for-human\n"), "{}", r.err);
    assert_eq!(r.out, "a: ready-for-agent -> ready-for-human\n");
}

#[test]
fn an_interrupted_run_keeps_its_spec_ready_with_its_run() {
    let d = Drain::new();
    d.spec("a", &ready("First"));
    let dir = d.dir();
    let r = d.drain_argv(vec![raising(Stopped::Interrupted)], &["drain", &dir, "--machine", "m", "--cwd", "~/p"]);
    let run_id = d.run_id(0);
    assert_eq!(r.status, EXIT_INTERRUPTED);
    assert_eq!(d.header("a"), (some(SPEC_READY), Some(run_id.clone()), None));
    let key = run_id.rsplit('-').next().unwrap();
    assert!(r.err.contains(&format!("resume with: orchestrator.py resume {key} --machine m --cwd '~/p'")), "{}", r.err);
    // Its Run: line is a change drain made, printed as Python's finally prints it.
    assert_eq!(r.out, "a: ready-for-agent -> ready-for-agent\n");
}

#[test]
fn a_ctrl_c_between_runs_stops_the_drain_with_the_changes_printed() {
    // The Rust engine's own: a Ctrl-C that no run took, as Python's KeyboardInterrupt anywhere in main.
    let d = Rc::new(Drain::new());
    d.spec("a", &ready("First"));
    d.spec("b", &ready("Second"));
    let dd = Rc::clone(&d);
    let r = d.drain(vec![after(move || dd.cli.f.interrupt.raise(), approved(PR_A, &[]))], &[]);
    assert_eq!(r.status, EXIT_INTERRUPTED);
    assert_eq!(d.tasks(), ["First"]);
    assert_eq!(r.out, format!("APPROVE: {PR_A}\na: ready-for-agent -> in-review  {PR_A}\n"));
    assert!(r.err.ends_with("interrupted; the role agents keep running in herdr\n"), "{}", r.err);
}

#[test]
fn a_finished_run_on_the_run_line_is_taken_up_without_a_new_run() {
    let pr_a = || ("pr_url", Json::from(PR_A));
    let verdict = |v: &str| ("verdict", Json::from(v));
    let cases: [(Fields, &str, Option<&str>); 5] = [
        (vec![verdict(APPROVE), pr_a()], SPEC_IN_REVIEW, Some(PR_A)),
        (vec![verdict(FINISHED), pr_a()], SPEC_IN_REVIEW, Some(PR_A)),
        (vec![verdict(APPROVE), pr_a(), ("conflicts", Json::Array(vec![Json::from("x.py")]))], SPEC_FOR_HUMAN, Some(PR_A)),
        (vec![verdict(CHANGES_REQUESTED), pr_a()], SPEC_FOR_HUMAN, Some(PR_A)),
        (vec![verdict(CHANGES_REQUESTED)], SPEC_FOR_HUMAN, None),
    ];
    for (mut fields, spec_after, pr) in cases {
        let d = Drain::new();
        d.spec("a", &spec_text("First", Some(SPEC_READY), &["Run: 20261024-100000-aaaaaa"]));
        fields.push(("phase", Json::from(DONE)));
        d.saved_run("a", "20261024-100000-aaaaaa", 10_000, &fields);
        d.pull_request(PR_A, "OPEN", &[]);
        let r = d.drain(vec![], &[]);
        assert_eq!(d.header("a"), (some(spec_after), some("20261024-100000-aaaaaa"), pr.map(String::from)));
        assert_eq!(r.status, 0, "{}", r.err);
        assert!(r.out.contains(&format!("a: ready-for-agent -> {spec_after}")), "{}", r.out);
    }
}

#[test]
fn a_finished_runs_pull_request_is_followed_in_the_same_pass() {
    let run_id = "20261024-100000-aaaaaa";
    for (pr_state, spec_after) in [("MERGED", SPEC_DONE), ("CLOSED", SPEC_FOR_HUMAN), ("OPEN", SPEC_IN_REVIEW)] {
        let d = Drain::new();
        d.spec("a", &spec_text("First", Some(SPEC_READY), &[&format!("Run: {run_id}")]));
        let fields = [("phase", Json::from(DONE)), ("verdict", Json::from(APPROVE)), ("pr_url", Json::from(PR_A))];
        d.saved_run("a", run_id, 10_000, &fields);
        d.pull_request(PR_A, pr_state, &[]);
        assert_eq!(d.refreshed(), [(0, entries(&[(STATUS_KEY, spec_after), (PR_KEY, PR_A)]))], "{pr_state}");
        assert_eq!(d.views(), [PR_A]);
    }
}

#[test]
fn one_drain_marks_a_merged_finished_run_done_and_runs_its_dependant() {
    let d = Drain::new();
    let run_id = "20261024-100000-aaaaaa";
    d.spec("a", &spec_text("First", Some(SPEC_READY), &[&format!("Run: {run_id}")]));
    d.spec("b", &spec_text("Second", Some(SPEC_READY), &["Blocked by: a"]));
    let fields = [("phase", Json::from(DONE)), ("verdict", Json::from(APPROVE)), ("pr_url", Json::from(PR_A))];
    d.saved_run("a", run_id, 10_000, &fields);
    d.pull_request(PR_A, "MERGED", &[]);
    let r = d.drain(vec![], &["--dry-run"]);
    assert_eq!(r.status, 0);
    assert_eq!(r.out, format!("a: ready-for-agent -> done  {PR_A}\nwould run, in order: b\n"));
    assert_eq!(d.header("a"), (some(SPEC_READY), some(run_id), None));
    let r = d.drain(vec![approved(PR_B, &[])], &[]);
    assert_eq!(r.status, 0, "{}", r.err);
    assert_eq!(d.header("a"), (some(SPEC_DONE), some(run_id), some(PR_A)));
    assert_eq!(d.tasks(), ["Second"]);
    assert_eq!(d.status_pr("b"), (some(SPEC_IN_REVIEW), some(PR_B)));
}

/// Spec a, of status, whose run with verdict opened PR_A over a conflict in x.py, and what gh says of PR_A: OPEN, a
/// draft and MERGEABLE unless pr says otherwise, its body the warning and a line. The run's id.
fn conflicted_spec(d: &Drain, status: &str, verdict: &str, pr: &[(&str, Json)]) -> String {
    let run_id = "20261024-100000-aaaaaa";
    d.spec("a", &spec_text("First", Some(status), &[&format!("Run: {run_id}"), &format!("PR: {PR_A}")]));
    let fields = [
        ("phase", Json::from(DONE)),
        ("verdict", Json::from(verdict)),
        ("pr_url", Json::from(PR_A)),
        ("base_branch", Json::from("main")),
        ("conflicts", Json::Array(vec![Json::from("x.py")])),
    ];
    d.saved_run("a", run_id, 10_000, &fields);
    let body = format!("{}\n\nOpened by run {run_id}.\n", conflict_warning(Some("main"), &["x.py".to_string()]));
    let mut all = vec![("isDraft", Json::Bool(true)), ("mergeable", Json::from("MERGEABLE")), ("body", Json::from(body))];
    all.extend(pr.iter().cloned());
    let state = pr.iter().find(|(k, _)| *k == "state").map_or("OPEN".to_string(), |(_, v)| v.python_str());
    d.pull_request(PR_A, &state, &all);
    run_id.to_string()
}

#[test]
fn a_spec_for_the_human_only_for_its_conflict_goes_to_review_once_refresh_clears_it() {
    let d = Drain::new();
    let run_id = conflicted_spec(&d, SPEC_FOR_HUMAN, APPROVE, &[]);
    let r = d.drain(vec![], &[]);
    assert_eq!(r.status, 0, "{}", r.err);
    assert_eq!(d.header("a"), (some(SPEC_IN_REVIEW), Some(run_id.clone()), some(PR_A)));
    assert!(r.out.contains(&format!("a: ready-for-human -> in-review  {PR_A}\n")), "{}", r.out);
    assert!(r.err.contains(&format!("{run_id}: conflict cleared, ready for review  {PR_A}\n")), "{}", r.err);
    let body = format!("Opened by run {run_id}.\n");
    let edits = [PrEdit::Body { url: PR_A.into(), body }, PrEdit::Ready { url: PR_A.into() }];
    assert_eq!(d.cli.f.host.state().pr_edits, edits);
    assert_eq!(d.run_conflicts(&run_id), Json::Array(vec![]));
}

#[test]
fn a_spec_for_the_human_stays_when_its_run_did_not_succeed_or_still_conflicts() {
    let cases: [(&str, (&str, &str), usize); 3] =
        [(CHANGES_REQUESTED, ("", ""), 1), (APPROVE, ("mergeable", "CONFLICTING"), 0), (APPROVE, ("state", "CLOSED"), 0)];
    for (verdict, (key, value), edits) in cases {
        let d = Drain::new();
        let pr: Vec<(&str, Json)> = if key.is_empty() { vec![] } else { vec![(key, Json::from(value))] };
        let run_id = conflicted_spec(&d, SPEC_FOR_HUMAN, verdict, &pr);
        let r = d.drain(vec![], &[]);
        assert_eq!(r.status, 0, "{}", r.err);
        assert_eq!(d.header("a"), (some(SPEC_FOR_HUMAN), Some(run_id), some(PR_A)));
        assert!(!r.out.contains("a: "), "{}", r.out);
        let made = d.cli.f.host.state().pr_edits.clone();
        assert_eq!(made.len(), edits, "{made:?}");
        assert!(made.iter().all(|e| matches!(e, PrEdit::Body { .. })));
    }
}

#[test]
fn an_in_review_specs_run_is_refreshed_too() {
    let d = Drain::new();
    let run_id = conflicted_spec(&d, SPEC_IN_REVIEW, APPROVE, &[]);
    let r = d.drain(vec![], &[]);
    assert_eq!(r.status, 0);
    assert_eq!(d.header("a"), (some(SPEC_IN_REVIEW), Some(run_id.clone()), some(PR_A)));
    assert_eq!(d.run_conflicts(&run_id), Json::Array(vec![]));
    assert_eq!(r.out, "all specs cleared\n");
}

#[test]
fn an_in_review_spec_whose_refresh_finds_its_pull_request_merged_is_done_asking_gh_once() {
    let d = Drain::new();
    conflicted_spec(&d, SPEC_IN_REVIEW, APPROVE, &[("state", Json::from("MERGED"))]);
    assert_eq!(d.refreshed(), [(0, entries(&[(STATUS_KEY, SPEC_DONE)]))]);
    assert_eq!(d.views(), [PR_A]);
}

#[test]
fn a_spec_for_the_human_whose_conflict_clears_has_one_entry_and_gh_asked_once() {
    let d = Drain::new();
    conflicted_spec(&d, SPEC_FOR_HUMAN, APPROVE, &[]);
    assert_eq!(d.refreshed(), [(0, entries(&[(STATUS_KEY, SPEC_IN_REVIEW), (PR_KEY, PR_A)]))]);
    assert_eq!(d.views(), [PR_A]);
}

#[test]
fn a_spec_for_the_human_whose_refresh_finds_its_pull_request_merged_is_done_asking_gh_once() {
    let d = Drain::new();
    conflicted_spec(&d, SPEC_FOR_HUMAN, APPROVE, &[("state", Json::from("MERGED"))]);
    assert_eq!(d.refreshed(), [(0, entries(&[(STATUS_KEY, SPEC_DONE)]))]);
    assert_eq!(d.views(), [PR_A]);
}

#[test]
fn a_refresh_of_another_pull_request_than_the_spec_s_settles_nothing_by_it() {
    let d = Drain::new();
    let run_id = conflicted_spec(&d, SPEC_IN_REVIEW, APPROVE, &[("state", Json::from("MERGED"))]);
    d.spec("a", &spec_text("First", Some(SPEC_IN_REVIEW), &[&format!("Run: {run_id}"), &format!("PR: {PR_B}")]));
    d.pull_request(PR_B, "OPEN", &[]);
    assert_eq!(d.refreshed(), []);
    assert_eq!(d.views(), [PR_A, PR_B]);
}

#[test]
fn a_dry_run_shows_the_move_and_refreshes_nothing() {
    let d = Drain::new();
    let run_id = conflicted_spec(&d, SPEC_FOR_HUMAN, APPROVE, &[]);
    let before = d.text("a");
    let r = d.drain(vec![], &["--dry-run"]);
    assert_eq!(r.status, 0);
    assert!(r.out.contains(&format!("a: ready-for-human -> in-review  {PR_A}\n")), "{}", r.out);
    assert!(!r.err.contains("conflict cleared"));
    assert_eq!(d.text("a"), before);
    assert_eq!(d.cli.f.host.state().pr_edits, []);
    assert_eq!(d.run_conflicts(&run_id), Json::Array(vec![Json::from("x.py")]));
}

#[test]
fn an_unfinished_run_on_the_run_line_stops_the_drain_before_any_run() {
    let run_id = "20261024-100000-aaaaaa";
    let stale = HEARTBEAT_SECONDS + STALE_SECONDS;
    let owner = Json::parse(r#"{"host": "elsewhere", "pid": 1}"#).unwrap();
    // The project is --cwd /proj's, which the command repeats, where Python's test patches resolve_dir.
    let resume = "resume it with `orchestrator.py resume aaaaaa --cwd /proj`";
    let cases: [(Option<Fields>, u64, &str, &str); 5] = [
        (
            Some(vec![("owner", owner)]),
            0,
            "is running (pid 1 on elsewhere, beat 0s ago)",
            "wait for it to finish, or remove the Run: line",
        ),
        (Some(vec![]), stale, "is stale (no heartbeat for", resume),
        (Some(vec![("error", Json::from("interrupted"))]), stale, "failed: interrupted", resume),
        (Some(vec![("closed", Json::from("workspace closed"))]), stale, "was closed (workspace closed)", "; remove the Run: line from"),
        (None, 0, "is not under /proj/.orchestrator/runs", "; remove the Run: line from"),
    ];
    for (fields, age, why, remedy) in cases {
        let d = Drain::new();
        d.sweepable();
        d.spec("a", &spec_text("First", Some(SPEC_READY), &[&format!("Run: {run_id}")]));
        d.spec("b", &ready("Second"));
        if let Some(mut fields) = fields {
            fields.push(("phase", Json::from("build")));
            d.saved_run("a", run_id, age, &fields);
        }
        let before = d.text("a");
        let r = d.drain(vec![], &[]);
        assert_eq!(r.status, EXIT_ERROR);
        assert!(r.err.starts_with(&format!("error: spec a: run {run_id}, on its Run: line, {why}")), "{}", r.err);
        assert!(r.err.contains(remedy), "{}", r.err);
        assert!(r.err.contains(&format!("{} to start the spec over", d.path("a"))), "{}", r.err);
        assert_eq!((d.text("a"), d.header("b")), (before, (some(SPEC_READY), None, None)));
        assert_eq!(r.out, "");
        assert!(!d.swept(), "{why}");
    }
}

#[test]
fn a_refresh_that_fails_changes_no_spec() {
    let d = Drain::new();
    d.spec("a", &spec_text("First", Some(SPEC_IN_REVIEW), &[&format!("PR: {PR_A}")]));
    d.spec("b", &spec_text("Second", Some(SPEC_READY), &["Run: 20261024-100000-bbbbbb"]));
    d.pull_request(PR_A, "MERGED", &[]);
    let before = d.text("a");
    let r = d.drain(vec![], &[]);
    assert_eq!(r.status, EXIT_ERROR);
    assert!(r.err.contains("error: spec b: run 20261024-100000-bbbbbb"), "{}", r.err);
    assert_eq!(d.text("a"), before);
}

#[test]
fn bad_blockers_stop_the_drain_before_any_run() {
    let cycle = "error: the specs' blockers form a cycle: b -> c -> b\n";
    let cases: [(&[(&str, &str)], &str); 3] = [
        (&[("a", "Blocked by: nosuch")], "error: spec a is blocked by nosuch, which has no spec.md in"),
        (&[("a", "Blocked by: b"), ("b", "Blocked by: c"), ("c", "Blocked by: b")], cycle),
        (&[("a", "Blocked by: a")], "error: the specs' blockers form a cycle: a -> a\n"),
    ];
    for (blockers, message) in cases {
        let d = Drain::new();
        d.sweepable();
        for slug in ["a", "b", "c"] {
            let header: Vec<&str> = blockers.iter().filter(|(s, _)| *s == slug).map(|(_, h)| *h).collect();
            d.spec(slug, &spec_text(&slug.to_uppercase(), Some(SPEC_READY), &header));
        }
        let r = d.drain(vec![], &[]);
        assert_eq!(r.status, EXIT_ERROR);
        assert!(r.err.starts_with(message), "{}", r.err);
        assert_eq!(r.out, "");
        assert!(!d.swept());
    }
}

#[test]
fn specs_with_any_other_status_are_left_alone() {
    let d = Drain::new();
    let texts = [
        ("a", spec_text("A", None, &[])),
        ("b", spec_text("B", Some("needs-triage"), &[])),
        ("c", spec_text("C", Some(SPEC_FOR_HUMAN), &[&format!("PR: {PR_A}")])),
        ("d", spec_text("D", Some("wontfix"), &["Run: 20261024-100000-dddddd"])),
        ("e", spec_text("E", Some(SPEC_IN_REVIEW), &[])),
        ("f", spec_text("F", Some(SPEC_DONE), &[])),
    ];
    for (slug, text) in &texts {
        d.spec(slug, text);
    }
    std::fs::create_dir_all(format!("{}/no-spec/issues", d.dir())).unwrap();
    let r = d.drain(vec![], &[]);
    assert_eq!((r.status, r.out.as_str()), (0, "all specs cleared\n"));
    for (slug, text) in &texts {
        assert_eq!(d.text(slug), *text, "{slug}");
    }
    assert_eq!(d.gh_urls(), Vec::<String>::new());
}

#[test]
fn only_the_header_lines_drain_owns_change() {
    for nl in ["\n", "\r\n"] {
        let d = Drain::new();
        let header =
            ["Owner: me", "Status:   ready-for-agent  ", "Blocked by:", "Run: stale", "## Notes", "PR: not in the header"];
        d.spec("a", &spec_text_nl("First", None, &header, nl));
        let fields = [("phase", Json::from(DONE)), ("verdict", Json::from(APPROVE)), ("pr_url", Json::from(PR_A))];
        d.saved_run("a", "stale", 0, &fields);
        d.pull_request(PR_A, "OPEN", &[]);
        assert_eq!(d.drain(vec![], &[]).status, 0);
        let pr = format!("PR: {PR_A}");
        let want = ["Owner: me", "Status: in-review", &pr, "Blocked by:", "Run: stale", "## Notes", "PR: not in the header"];
        assert_eq!(d.text("a"), spec_text_nl("First", None, &want, nl), "{nl:?}");
    }
}

#[test]
fn added_lines_go_directly_below_the_status_line() {
    let d = Drain::new();
    d.spec("a", "# First\r\nOwner: me\r\nStatus: ready-for-agent\r\nNote: x\r\n");
    d.drain(vec![approved(PR_A, &[])], &[]);
    let run_id = d.run_id(0);
    let want = format!("# First\r\nOwner: me\r\nStatus: in-review\r\nPR: {PR_A}\r\nRun: {run_id}\r\nNote: x\r\n");
    assert_eq!(d.text("a"), want);
}

#[test]
fn a_after_line_that_ends_the_file() {
    assert_eq!(with_header_field("# T\nStatus: done", "PR", PR_A).unwrap(), format!("# T\nStatus: done\nPR: {PR_A}"));
    let e = with_header_field("# T\n## Status: done\n", "PR", PR_A).unwrap_err();
    assert_eq!(e.0, "the spec has no Status: line to add PR: below");
}

#[test]
fn a_spec_that_cannot_be_written_stops_the_drain_and_keeps_the_old_file() {
    let d = Drain::new();
    d.spec("a", &ready("First"));
    let before = d.text("a");
    // The file written aside cannot be made, as on a full disk: a directory has its name.
    let real = std::fs::canonicalize(d.path("a")).unwrap();
    std::fs::create_dir(format!("{}.tmp.{}", real.display(), std::process::id())).unwrap();
    let r = d.drain(vec![], &[]);
    assert_eq!(r.status, EXIT_ERROR);
    assert!(r.err.contains(&format!("error: cannot write {}: [Errno 21] Is a directory", d.path("a"))), "{}", r.err);
    assert_eq!(d.text("a"), before);
    let names = std::fs::read_dir(format!("{}/a", d.dir())).unwrap().count();
    assert_eq!(names, 2, "spec.md and the directory in the way, and nothing else");
    assert_eq!(d.runs.borrow().len(), 0);
}

#[test]
fn dry_run_changes_nothing_and_says_what_it_would_do() {
    let d = Drain::new();
    d.sweepable();
    d.spec("a", &spec_text("A", Some(SPEC_IN_REVIEW), &[&format!("PR: {PR_A}")]));
    d.spec("b", &spec_text("B", Some(SPEC_READY), &["Blocked by: a"]));
    d.spec("c", &spec_text("C", Some(SPEC_READY), &["Run: 20261024-100000-cccccc"]));
    d.spec("d", &ready("D"));
    d.spec("e", &spec_text("E", Some(SPEC_READY), &["Blocked by: d"]));
    d.pull_request(PR_A, "MERGED", &[]);
    let fields = [("phase", Json::from(DONE)), ("verdict", Json::from(APPROVE)), ("pr_url", Json::from(PR_B))];
    d.saved_run("c", "20261024-100000-cccccc", 0, &fields);
    d.pull_request(PR_B, "OPEN", &[]);
    let texts = || -> Vec<String> { ["a", "b", "c", "d", "e"].iter().map(|s| d.text(s)).collect() };
    let before = texts();
    let r = d.drain(vec![], &["--dry-run"]);
    assert_eq!(r.status, 0);
    assert_eq!(texts(), before);
    assert_eq!(std::fs::read_dir(format!("{}/a", d.dir())).unwrap().count(), 1);
    let want = format!("a: in-review -> done  {PR_A}\nc: ready-for-agent -> in-review  {PR_B}\nwould run, in order: b, d\n");
    assert_eq!(r.out, want);
    assert!(!d.swept());
}

#[test]
fn drain_takes_no_task_spec_or_no_pr() {
    let config = ConfigDir::at("/nowhere");
    let parse = |argv: &[&str]| crate::cli::parse_args(argv.iter().map(OsString::from), &config);
    for argv in [&["--no-pr"][..], &["task", "--spec", "spec.md"], &["--spec", "spec.md"], &["a", "b"]] {
        let argv: Vec<&str> = std::iter::once("drain").chain(argv.iter().copied()).collect();
        assert_eq!(parse(&argv).unwrap_err().exit_code(), 2, "{argv:?}");
    }
    let e = parse(&["drain", "--spec"]).unwrap_err().to_string();
    assert!(e.contains("unrecognized arguments: --spec; drain runs the specs in DIR"), "{e}");
    match parse(&["drain", "--spec-model", "opus"]).unwrap() {
        crate::cli::Command::Drain(args) => {
            assert_eq!(args.run.settings.label_models, [("spec".to_string(), "opus".to_string())]);
            assert_eq!(args.dir, DRAIN_DIR);
            assert_eq!((args.run.task, args.run.spec, args.run.no_pr), (None, None, false));
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn the_flags_reach_every_run() {
    let d = Drain::new();
    d.spec("a", &ready("A"));
    d.spec("b", &ready("B"));
    let example = format!("{EXAMPLES}/spec-build-review.toml");
    let job = crate::ci::fake::JOB;
    let argv = [
        "--workflow-file", &example, "--worktree", "--quality-gate", job, "--max-quality-rounds", "2",
        "--max-rounds", "5", "--timeout", "60", "--permission-mode", "auto", "--model", "m", "--review-model", "r",
        "--agent", "codex",
    ];
    assert_eq!(d.drain(vec![approved(PR_A, &[]), approved(PR_A, &[])], &argv).status, 0);
    let runs = d.runs.borrow();
    assert_eq!(runs.len(), 2);
    for (state, _) in runs.iter() {
        assert_eq!(state.workflow, "spec-build-review");
        assert_eq!((state.quality_job.as_deref(), state.max_quality_rounds, state.max_rounds), (Some(job), 2, 5));
        assert_eq!((state.turn_timeout, state.permission_mode.as_deref()), (60, Some("auto")));
        assert_eq!(state.worktree, Some(format!("/proj/{WORKTREES_DIR}/{}", state.key())));
        let models: Vec<(String, String)> = state.models.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        let want = [("spec", "m"), ("build", "m"), ("review", "r")].map(|(k, v)| (k.to_string(), v.to_string()));
        assert_eq!(models, want);
        assert!(state.agent_kinds.values().all(|k| k == "codex"));
    }
}

#[test]
fn a_workflow_that_cannot_take_a_spec_fails_before_any_run() {
    let d = Drain::new();
    d.sweepable();
    d.spec("a", &ready("A"));
    let quick = format!("{EXAMPLES}/quick.toml");
    let r = d.drain(vec![], &["--workflow-file", &quick]);
    assert_eq!(r.status, EXIT_ERROR);
    assert!(r.err.contains("--spec needs a first step that writes the spec"), "{}", r.err);
    assert!(!d.swept());
    assert_eq!(d.header("a"), (some(SPEC_READY), None, None));
}

#[test]
fn a_missing_dir_is_an_error() {
    let d = Drain::new();
    let nosuch = format!("{}/nosuch", d.dir());
    let r = d.drain_argv(vec![], &["drain", &nosuch, "--cwd", "/proj"]);
    assert_eq!(r.status, EXIT_ERROR);
    assert!(r.err.contains("error: cannot list the specs in"), "{}", r.err);
}

#[test]
fn blocker_slugs_read_slugs_and_spec_paths() {
    assert_eq!(blocker_slugs(Some("a, specs/b/spec.md ,, /c/spec.md")), ["a", "b", "c"]);
    assert_eq!(blocker_slugs(Some(" NONE ")), Vec::<String>::new());
    assert_eq!(blocker_slugs(None), Vec::<String>::new());
}

// -- TestPromptFiles.test_a_bad_file_stops_drain_before_any_run ------------------------------------------------------

#[test]
fn a_bad_prompt_file_stops_drain_before_any_run() {
    let mut d = Drain::new();
    let config = TempDir::new();
    crate::host::tests::put(&config.join("prompts/BUILD_PROMPT.md"), "Build {nope}.");
    d.cli.config = ConfigDir::at(&config.path());
    d.spec("a", &ready("First"));
    let r = d.drain(vec![], &[]);
    assert_eq!(r.status, EXIT_ERROR);
    let want = format!("{}/BUILD_PROMPT.md: placeholder {{nope}}", d.cli.config.prompts_dir());
    assert!(r.err.contains(&want), "{}", r.err);
    assert_eq!(d.runs.borrow().len(), 0);
}

// -- TestDrainWorkflows ----------------------------------------------------------------------------------------------

#[test]
fn two_specs_end_as_two_pull_requests() {
    let d = Drain::new();
    for slug in ["a", "b"] {
        d.spec(slug, &ready(&format!("Spec {slug}")));
    }
    let builds = vec![build_turn(1), build_turn(1)];
    let reviews = vec![review_turn(1, APPROVE), review_turn(1, APPROVE)];
    let dir = d.dir();
    let ended = d.cli.playing(vec![("build", builds), ("review", reviews)], || d.cli.ended(&["drain", &dir, "--cwd", "/proj"]));
    assert!(matches!(ended, Ok(Ended::Status(0))));
    let titles: Vec<String> = d.cli.f.host.state().prs.iter().map(|pr| pr.title.clone()).collect();
    assert_eq!(titles, ["Spec a", "Spec b"]);
    assert!(d.cli.take_out().ends_with("all specs cleared\n"));
    for slug in ["a", "b"] {
        assert_eq!(d.status_pr(slug), (some(SPEC_IN_REVIEW), some("https://github.com/o/r/pull/7")));
    }
}

#[test]
fn a_blocker_with_an_empty_status_waits_as_one_with_none() {
    // As Python's `blocker.status or 'no status'` reads an empty Status: (cross-review 104, F1).
    let d = Drain::new();
    d.spec("a", &spec_text("First", Some(""), &[&format!("PR: {PR_A}")]));
    d.spec("b", &spec_text("Second", Some(SPEC_READY), &["Blocked by: a"]));
    let r = d.drain(vec![], &[]);
    assert_eq!(r.status, EXIT_WAITING);
    assert!(r.out.ends_with(&format!("b waits on:\n    a: no status  {PR_A}\n")), "{}", r.out);
}
