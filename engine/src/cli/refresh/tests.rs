//! tests/test_orchestrator.py's TestRefresh, ported case for case: refresh through main's dispatch (run_command), on
//! a FakeHost whose gh is scripted by pull_requests. test_args is the parse_args half of the class, here too.

use super::super::fixture::Cli;
use crate::cli::{Command, Stop, Target};
use crate::constants::{APPROVE, CHANGES_REQUESTED, EXIT_ERROR, EXIT_WAITING};
use crate::host::fake::{FakePr, PrEdit};
use crate::host::Completed;
use crate::pull_request::{conflict_warning, pr_body, GateEvidence};
use crate::py::json::{Json, Map};
use crate::state::RunState;

const REFRESH_ID: &str = "20261001-120000-a1b2c3";
const REFRESH_URL: &str = "https://github.com/o/r/pull/7";

/// The path of run_id's state.json in /proj.
fn state_path(run_id: &str) -> String {
    format!("/proj/.orchestrator/runs/{run_id}/state.json")
}

/// A done run's state, as add_run saves it.
fn done_run(run_id: &str, url: &str, conflicts: &[&str], verdict: &str) -> RunState {
    let mut state = RunState::new(run_id, "a task", "/proj");
    state.phase = "done".into();
    state.round = 1;
    state.base_branch = Some("main".into());
    state.branch = Some("orchestrator/x".into());
    state.pr_url = Some(url.into());
    state.verdict = Some(verdict.into());
    state.conflicts = conflicts.iter().map(|c| c.to_string()).collect();
    state
}

/// What gh says of a pull request: OPEN, a draft, MERGEABLE and with body, with fields in place of those.
fn pr(body: &str, fields: &[(&str, Json)]) -> FakePr {
    let mut pr: Map<Json> = [
        ("state", Json::from("OPEN")),
        ("isDraft", Json::Bool(true)),
        ("mergeable", Json::from("MERGEABLE")),
        ("body", Json::from(body)),
    ]
    .into_iter()
    .collect();
    for (key, value) in fields {
        pr.insert(*key, value.clone());
    }
    FakePr::Prints(Json::Object(pr))
}

/// The body pr_body writes for state.
fn body_of(state: &RunState) -> String {
    pr_body(state, "spec", "report", "VERDICT: APPROVE", &GateEvidence::default())
}

/// The body pr_body writes for the run once its conflicts are gone.
fn clean_body(verdict: &str) -> String {
    let mut state = RunState::new(REFRESH_ID, "a task", "/proj");
    state.round = 1;
    state.base_branch = Some("main".into());
    state.verdict = Some(verdict.into());
    body_of(&state)
}

/// A world on a FakeHost, with done runs added by add.
struct Refresh {
    cli: Cli,
}

impl Refresh {
    fn new() -> Refresh {
        Refresh { cli: Cli::new() }
    }

    /// A done run whose pull request at url was opened over conflicts, and what gh says of it: pr(body) with fields,
    /// body the one pr_body wrote unless given. The run's saved state.
    fn add(&self, run_id: &str, url: &str, conflicts: &[&str], verdict: &str, body: Option<&str>, fields: &[(&str, Json)]) -> Json {
        let state = done_run(run_id, url, conflicts, verdict);
        let text = state.to_text();
        let body = body.map_or_else(|| body_of(&state), String::from);
        let mut host = self.cli.f.host.state();
        host.files.insert(format!("{}/state.json", state.dir()), text.clone());
        host.pull_requests.insert(url.to_string(), pr(&body, fields));
        Json::parse(&text).unwrap()
    }

    /// add with the defaults: REFRESH_ID, REFRESH_URL, conflicts in a.py, APPROVE.
    fn add_run(&self, fields: &[(&str, Json)]) -> Json {
        self.add(REFRESH_ID, REFRESH_URL, &["a.py"], APPROVE, None, fields)
    }

    /// Replace the run's saved state with saved, with fields changed.
    fn resave(&self, run_id: &str, saved: &Json, fields: &[(&str, Json)]) {
        let mut saved = saved.as_object().unwrap().clone();
        for (key, value) in fields {
            saved.insert(*key, value.clone());
        }
        self.cli.f.host.state().files.insert(state_path(run_id), Json::Object(saved).dumps());
    }

    fn saved(&self) -> Json {
        self.cli.saved(REFRESH_ID)
    }

    /// main's exit status for refresh with argv, and what it printed.
    fn refresh(&self, argv: &[&str]) -> (i32, String) {
        let argv: Vec<&str> = ["refresh", "--cwd", "/proj"].iter().chain(argv).copied().collect();
        let status = self.cli.main(&argv);
        (status, self.cli.take_out())
    }

    /// The error main prints for refresh with argv, without its "error: ".
    fn error(&self, argv: &[&str]) -> String {
        let argv: Vec<&str> = ["refresh", "--cwd", "/proj"].iter().chain(argv).copied().collect();
        self.cli.error(&argv)
    }

    fn pr_edits(&self) -> Vec<PrEdit> {
        self.cli.f.host.state().pr_edits.clone()
    }

    /// The URL each gh command asked about, in order.
    fn gh_urls(&self) -> Vec<String> {
        self.cli.f.host.state().gh_calls.iter().map(|c| c[2].clone()).collect()
    }
}

fn body_edit(url: &str, body: &str) -> PrEdit {
    PrEdit::Body { url: url.into(), body: body.into() }
}

fn ready(url: &str) -> PrEdit {
    PrEdit::Ready { url: url.into() }
}

#[test]
fn a_cleanly_merging_approved_draft_loses_its_warning_and_is_marked_ready() {
    let t = Refresh::new();
    t.add_run(&[]);
    let (status, out) = t.refresh(&["a1b2c3"]);
    assert_eq!(status, 0);
    assert_eq!(out, format!("{REFRESH_ID}: conflict cleared, ready for review  {REFRESH_URL}\n"));
    assert_eq!(t.pr_edits(), [body_edit(REFRESH_URL, &clean_body(APPROVE)), ready(REFRESH_URL)]);
    assert_eq!(t.saved().get("conflicts"), Some(&Json::Array(vec![])));
    assert_eq!(t.cli.f.host.state().mtime_kept, [state_path(REFRESH_ID)]);
}

#[test]
fn a_run_that_did_not_succeed_keeps_its_draft() {
    let t = Refresh::new();
    t.add(REFRESH_ID, REFRESH_URL, &["a.py"], CHANGES_REQUESTED, None, &[]);
    let (status, out) = t.refresh(&[REFRESH_ID]);
    assert_eq!(status, 0);
    assert_eq!(out, format!("{REFRESH_ID}: conflict cleared  {REFRESH_URL}\n"));
    assert_eq!(t.pr_edits(), [body_edit(REFRESH_URL, &clean_body(CHANGES_REQUESTED))]);
    assert_eq!(t.saved().get("conflicts"), Some(&Json::Array(vec![])));
}

#[test]
fn a_pull_request_already_out_of_draft_is_not_marked_again() {
    let t = Refresh::new();
    t.add_run(&[("isDraft", Json::Bool(false))]);
    assert_eq!(t.refresh(&[REFRESH_ID]), (0, format!("{REFRESH_ID}: conflict cleared  {REFRESH_URL}\n")));
    assert!(matches!(&t.pr_edits()[..], [PrEdit::Body { .. }]), "{:?}", t.pr_edits());
}

#[test]
fn a_pull_request_that_still_conflicts_or_is_not_known_is_left() {
    for (mergeable, line) in [("CONFLICTING", "still conflicts"), ("UNKNOWN", "mergeability not known yet, try again")] {
        let t = Refresh::new();
        let saved = t.add_run(&[("mergeable", Json::from(mergeable))]);
        let (status, out) = t.refresh(&[REFRESH_ID]);
        assert_eq!(status, EXIT_WAITING);
        assert_eq!(out, format!("{REFRESH_ID}: {line}  {REFRESH_URL}\n"));
        assert_eq!((t.pr_edits(), t.cli.f.host.state().writes.clone()), (vec![], vec![]));
        assert_eq!(t.saved(), saved);
    }
}

#[test]
fn a_merged_or_closed_pull_request_only_forgets_the_conflict() {
    for state in ["MERGED", "CLOSED"] {
        let t = Refresh::new();
        // Whatever GitHub says of its mergeability, which decides nothing once it is not open.
        t.add_run(&[("state", Json::from(state)), ("mergeable", Json::from("CONFLICTING"))]);
        let line = format!("{REFRESH_ID}: {}, conflict forgotten\n", state.to_lowercase());
        assert_eq!(t.refresh(&[REFRESH_ID]), (0, line));
        assert_eq!(t.pr_edits(), []);
        assert_eq!(t.saved().get("conflicts"), Some(&Json::Array(vec![])));
    }
}

#[test]
fn human_edits_to_the_body_are_kept() {
    let t = Refresh::new();
    let warning = conflict_warning(Some("main"), &["a.py".to_string()]);
    let body = format!("{warning}\n\nA human's note.\n\nOpened by run x.\n");
    t.add(REFRESH_ID, REFRESH_URL, &["a.py"], APPROVE, Some(&body), &[]);
    t.refresh(&[REFRESH_ID]);
    assert_eq!(t.pr_edits()[0], body_edit(REFRESH_URL, "A human's note.\n\nOpened by run x.\n"));
}

#[test]
fn a_body_whose_warning_is_gone_is_not_edited() {
    for (verdict, edits) in [(APPROVE, vec![ready(REFRESH_URL)]), (CHANGES_REQUESTED, vec![])] {
        let t = Refresh::new();
        t.add(REFRESH_ID, REFRESH_URL, &["a.py"], verdict, Some("Edited by hand.\n"), &[]);
        assert_eq!(t.refresh(&[REFRESH_ID]).0, 0);
        assert_eq!(t.pr_edits(), edits);
        assert_eq!(t.saved().get("conflicts"), Some(&Json::Array(vec![])));
    }
}

#[test]
fn without_a_run_every_done_run_with_a_pull_request_and_conflicts_in_run_order() {
    let t = Refresh::new();
    let run = |k: char, n: u32| {
        (format!("https://github.com/o/r/pull/{n}"), format!("20261001-12000{n}-{}", k.to_string().repeat(6)))
    };
    let [a, b, c, d, e, f] = [('a', 1), ('b', 2), ('c', 3), ('d', 4), ('e', 5), ('f', 6)].map(|(k, n)| run(k, n));
    // Added out of order; d has no conflicts, e no pull request, f is not done.
    t.add(&c.1, &c.0, &["a.py"], APPROVE, None, &[("mergeable", Json::from("CONFLICTING"))]);
    t.add(&a.1, &a.0, &["a.py"], APPROVE, None, &[]);
    t.add(&b.1, &b.0, &["a.py"], APPROVE, None, &[("state", Json::from("MERGED"))]);
    t.add(&d.1, &d.0, &[], APPROVE, None, &[]);
    let saved = t.add(&e.1, &e.0, &["a.py"], APPROVE, None, &[]);
    t.resave(&e.1, &saved, &[("pr_url", Json::Null)]);
    let saved = t.add(&f.1, &f.0, &["a.py"], APPROVE, None, &[]);
    t.resave(&f.1, &saved, &[("phase", Json::from("review"))]);
    let (status, out) = t.refresh(&[]);
    assert_eq!(status, EXIT_WAITING);
    assert_eq!(
        out,
        format!(
            "{}: conflict cleared, ready for review  {}\n{}: merged, conflict forgotten\n{}: still conflicts  {}\n",
            a.1, a.0, b.1, c.1, c.0
        )
    );
    assert_eq!(t.gh_urls(), [a.0, b.0, c.0]);
}

#[test]
fn without_a_run_and_nothing_to_refresh() {
    let t = Refresh::new();
    t.add(REFRESH_ID, REFRESH_URL, &[], APPROVE, None, &[]);
    assert_eq!(t.refresh(&[]), (0, String::new()));
    assert!(t.cli.f.host.state().gh_calls.is_empty());
}

#[test]
fn a_named_run_without_conflicts_or_not_done() {
    let t = Refresh::new();
    let saved = t.add(REFRESH_ID, REFRESH_URL, &[], APPROVE, None, &[]);
    assert_eq!(t.refresh(&[REFRESH_ID]), (0, format!("{REFRESH_ID}: no conflict to clear\n")));
    t.resave(REFRESH_ID, &saved, &[("phase", Json::from("build"))]);
    assert_eq!(t.refresh(&[REFRESH_ID]), (EXIT_ERROR, String::new()));
    assert!(t.error(&[REFRESH_ID]).contains(&format!("run {REFRESH_ID} is not done")));
    assert_eq!(t.refresh(&["ffffff"]), (EXIT_ERROR, String::new()));
    assert!(t.cli.f.host.state().gh_calls.is_empty());
}

#[test]
fn a_gh_failure_names_the_run() {
    let t = Refresh::new();
    t.add_run(&[]);
    let failed = Completed { status: 1, stdout: String::new(), stderr: "HTTP 502".into() };
    t.cli.f.host.state().pull_requests.insert(REFRESH_URL.into(), FakePr::Fails(failed));
    let error = t.error(&[REFRESH_ID]);
    assert!(error.starts_with(&format!("run {REFRESH_ID}: ")), "{error}");
    assert!(error.contains("gh pr view") && error.ends_with(" failed: HTTP 502"), "{error}");
    assert_eq!(t.refresh(&[REFRESH_ID]), (EXIT_ERROR, String::new()));
}

#[test]
fn a_state_saved_meanwhile_is_not_overwritten() {
    let t = Refresh::new();
    let saved = t.add_run(&[]);
    let path = state_path(REFRESH_ID);
    // The run is resumed once refresh has read its state: the file holds what the resume saved since.
    t.resave(REFRESH_ID, &saved, &[("round", Json::from(2))]);
    t.cli.f.host.state().run_states_with = Some(Box::new(move |_: &str| Some(Ok(vec![(0, saved.clone())]))));
    let error = t.error(&[REFRESH_ID]);
    assert!(error.contains(&format!("run {REFRESH_ID}: {path} changed while its pull request was refreshed")), "{error}");
    assert_eq!(t.saved().get("conflicts"), Some(&Json::Array(vec![Json::from("a.py")])));
    assert_eq!(t.saved().get("round"), Some(&Json::from(2)));
}

#[test]
fn show_no_longer_lists_the_conflicts() {
    let t = Refresh::new();
    t.add_run(&[]);
    t.cli.main(&["show", "--cwd", "/proj", REFRESH_ID]);
    assert!(t.cli.take_out().contains("conflicts  a.py\n"));
    t.refresh(&[REFRESH_ID]);
    t.cli.main(&["show", "--cwd", "/proj", REFRESH_ID]);
    assert!(!t.cli.take_out().contains("conflicts"));
}

#[test]
fn args() {
    let t = Refresh::new();
    let target = Target { machine: Some("m".into()), cwd: Some("~/proj".into()) };
    let command = t.cli.command(&["refresh", "a1b2c3", "--machine", "m", "--cwd", "~/proj"]);
    assert_eq!(command, Command::Refresh(target, Some("a1b2c3".into())));
    assert_eq!(t.cli.command(&["refresh"]), Command::Refresh(Target::default(), None));
}

#[test]
fn an_interrupted_refresh_ends_interrupted() {
    let t = Refresh::new();
    t.add_run(&[]);
    t.cli.f.interrupt.raise();
    let ended = t.cli.ended(&["refresh", "--cwd", "/proj", REFRESH_ID]);
    assert!(matches!(ended, Err(Stop::Interrupted(None))));
}
