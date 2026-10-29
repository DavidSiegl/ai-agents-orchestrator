//! TestBlocked and TestBlockedResume: a step that can block writes a BLOCKED report, the turn waits for the human's
//! answer and then for the report again, and an orchestrator killed at each point of a block resumes from there.
//!
//! Not here: TestBlocked's test_a_blocked_quality_answer_waits_then_goes_back_to_the_quality_round and
//! test_the_note_is_in_every_prompt_of_the_steps_that_can_block_only, which run the quality gate, with the quality
//! round (rust-engine ticket 08, part 5); the print_runs half of test_the_wait_is_saved_and_listed, list's (ticket
//! 09); TestBlockedListing, list's and show's (ticket 09); TestParseBlocked, crate::handoff's and crate::pipeline's
//! (ticket 04).

use std::cell::RefCell;
use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};

use super::*;
use crate::constants::NO_QUESTION;
use crate::host::fake::HOST_EPOCH;
use crate::pipeline::DEFAULT_WORKFLOW;
use crate::test_support::EXAMPLES;
use crate::workflow_file::{load_workflow_file, workflow_toml};

const D: &str = "/proj/.orchestrator/runs/20260929-120000-a1b2c3";
const QUESTION: &str = "which database?";

fn build_1() -> String {
    format!("{D}/build-1.md")
}

fn blocked_1() -> String {
    format!("{D}/build-1.blocked-1.md")
}

/// A turn whose agent cannot go on without the human: it writes a BLOCKED report asking question, and waits.
fn blocked_report(path_of: impl Fn(&RunState) -> String + 'static, question: &str) -> Turn {
    writes(path_of, &format!("BLOCKED: {question}\n\nThe spec names no database."), "idle")
}

/// A turn whose agent waits for the human's answer in its pane, idle, then finishes the work and writes text
/// 10 * STALL_SECONDS of the host's time later, as the host's clock plays it.
fn answered(path_of: impl Fn(&RunState) -> String + 'static, text: &str) -> Turn {
    let text = text.to_string();
    Box::new(move |_, state, host| {
        let clock = host.state().clock.clone().expect("a host with a clock");
        let due = clock.now() + 10.0 * STALL_SECONDS as f64;
        let (path, host, text) = (path_of(state), Rc::clone(host), text.clone());
        let mut done = false;
        clock.hook(move |now| {
            if !done && now >= due {
                done = true;
                host.write("/proj/limiter.py", "answered", false).expect("the fake host writes");
                host.write(&path, &text, false).expect("the fake host writes");
            }
        });
        "idle".to_string()
    })
}

/// The prompt of the wait for the human's answer to the k-th block of the report at path.
fn blocked_prompt(path: &str, k: u32) -> String {
    let (stem, ext) = crate::py::text::splitext(path);
    let blocked = format!("{stem}.blocked-{k}{ext}");
    let value = |name: &str| match name {
        "path" => Some(path.to_string()),
        "blocked_path" => Some(blocked.clone()),
        _ => None,
    };
    crate::py::format::format(crate::prompts::BLOCKED_PROMPT, &value).expect("the built-in prompt fills in")
}

/// The names of the blocked reports on the host, sorted.
fn blocked_files(host: &FakeHost) -> Vec<String> {
    let mut names: Vec<String> = host
        .state()
        .files
        .keys()
        .filter(|p| p.contains(".blocked-"))
        .map(|p| crate::py::text::basename(p).to_string())
        .collect();
    names.sort();
    names
}

/// Fail, rather than poll forever, a run that is still waiting after 100 * STALL_SECONDS: no turn here takes that
/// long, and a blocked turn has no timeout of its own.
fn bounded(f: &Fixture) {
    let limit = 100.0 * STALL_SECONDS as f64;
    f.clock.hook(move |now| assert!(now <= limit, "the run is still waiting after {limit}s"));
}

/// The prompts of herdr's commands that are prompts or focuses, as (kind, name).
fn prompts_and_focuses(f: &Fixture) -> Vec<(&'static str, String)> {
    let calls = f.herdr.state().calls.clone();
    calls
        .into_iter()
        .filter_map(|c| match c {
            Call::Prompt { name } => Some(("prompt", name)),
            Call::Focus { name } => Some(("focus", name)),
            _ => None,
        })
        .collect()
}

/// A default run whose Builder takes the turns in build, and the Reviewer review or else one APPROVE in round 1:
/// the fixture, bounded, with the prompts the Builder and the Reviewer saw, and the state saved as the Reviewer got
/// its first prompt.
struct Blocked {
    f: Fixture,
    builds: Rc<RefCell<Vec<String>>>,
    reviews: Rc<RefCell<Vec<String>>>,
    at_review: Rc<RefCell<Vec<Map<Json>>>>,
}

fn run_blocked(build: Vec<Turn>, review: Option<Vec<Turn>>) -> Blocked {
    let (builds, reviews, at_review) = (seen(), seen(), Rc::new(RefCell::new(vec![])));
    let review = review.unwrap_or_else(|| {
        let (at_review, mut approve) = (Rc::clone(&at_review), review_turn(1, APPROVE));
        let first: Turn = Box::new(move |prompt, state, host| {
            let saved = host.state().files.get(&format!("{D}/state.json")).cloned().expect("a saved state");
            let Ok(Json::Object(saved)) = Json::parse(&saved) else { panic!("state.json is no object") };
            at_review.borrow_mut().push(saved);
            approve(prompt, state, host)
        });
        vec![first]
    });
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", build.into_iter().map(|b| recording(b, &builds)).collect()),
        ("review", review.into_iter().map(|r| recording(r, &reviews)).collect()),
    ]);
    bounded(&f);
    Blocked { f, builds, reviews, at_review }
}

fn build_path(n: u32) -> impl Fn(&RunState) -> String {
    move |s| s.build_path(n)
}

#[test]
fn a_blocked_report_waits_for_the_human_then_goes_to_the_reviewer() {
    let b = run_blocked(vec![blocked_report(build_path(1), QUESTION), answered(build_path(1), "report 1")], None);
    let f = &b.f;
    let settings = RunSettings { turn_timeout: 60, ..RunSettings::default() };
    let mut wf = f.workflow(settings);
    assert_eq!(wf.run(), verdict(APPROVE));
    let files = f.host.state().files.clone();
    assert!(files.get(&blocked_1()).unwrap().starts_with(&format!("BLOCKED: {QUESTION}\n")));
    assert_eq!(files.get(&build_1()).map(String::as_str), Some("report 1"));
    assert_eq!(blocked_files(&f.host), ["build-1.blocked-1.md"]);
    assert_eq!(b.builds.borrow()[1], blocked_prompt(&build_1(), 1));
    assert!(f.told.notes.borrow().contains(&("Builder is blocked: which database?".into(), "pane w1:p2".into())));
    // Focused, then prompted; the Reviewer only once the Builder has written its report again.
    let expected: Vec<(&str, String)> = [
        ("focus", "spec"),
        ("prompt", "spec"),
        ("prompt", "build"),
        ("focus", "build"),
        ("prompt", "build"),
        ("prompt", "review"),
    ]
    .into_iter()
    .map(|(kind, role)| (kind, format!("{role}-a1b2c3")))
    .collect();
    assert_eq!(prompts_and_focuses(f), expected);
    // The wait outlasted --timeout and STALL_SECONDS, unremarked.
    assert!(f.clock.now() >= 10.0 * STALL_SECONDS as f64);
    assert!(!f.told.titles().iter().any(|t| t.contains("idle")));
    let at_review = &b.at_review.borrow()[0];
    assert_eq!((at_review.get("blocked"), at_review.get("round")), (Some(&Json::Null), Some(&Json::from(1))));
    assert!(b.reviews.borrow()[0].contains(&format!("The Builder's report is in {}.", build_1())));
    assert_eq!(wf.state().blocked, None);
}

#[test]
fn a_dropped_answer_prompt_is_sent_once_more() {
    let b = run_blocked(vec![], None);
    let f = &b.f;
    let herdr = Rc::clone(&f.herdr);
    let mut report = blocked_report(build_path(1), QUESTION);
    let blocking: Turn = Box::new(move |prompt, state, host| {
        // The prompt after the human's answer.
        herdr.state().dropped.insert("build".into(), 1);
        report(prompt, state, host)
    });
    f.play("build", vec![blocking, answered(build_path(1), "report 1")]);
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let builds: Vec<String> =
        f.herdr.state().prompts.iter().filter(|(name, _)| name == "build-a1b2c3").map(|(_, t)| t.clone()).collect();
    assert_eq!(builds[1..], [blocked_prompt(&build_1(), 1), blocked_prompt(&build_1(), 1)]);
    assert_eq!(f.host.state().files.get(&build_1()).map(String::as_str), Some("report 1"));
}

#[test]
fn the_wait_is_saved() {
    // test_the_wait_is_saved_and_listed, without its print_runs half.
    let during = Rc::new(RefCell::new(vec![]));
    let (record, mut answer) = (Rc::clone(&during), answered(build_path(1), "report 1"));
    let wait: Turn = Box::new(move |prompt, state, host| {
        let saved = host.state().files.get(&format!("{D}/state.json")).cloned().expect("a saved state");
        record.borrow_mut().push(Json::parse(&saved).unwrap());
        answer(prompt, state, host)
    });
    let b = run_blocked(vec![blocked_report(build_path(1), QUESTION), wait], None);
    assert_eq!(b.f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let saved = &during.borrow()[0];
    assert_eq!(saved.get("blocked").unwrap().dumps(), r#"{"file": "build-1.md", "question": "which database?", "k": 1}"#);
    assert_eq!(saved.get("prompted"), Some(&Json::Null));
}

#[test]
fn a_question_left_out() {
    let b = run_blocked(vec![blocked_report(build_path(1), ""), answered(build_path(1), "report 1")], None);
    assert_eq!(b.f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let note = (format!("Builder is blocked: {NO_QUESTION}"), "pane w1:p2".to_string());
    assert_eq!(note.0, "Builder is blocked: (no question given)");
    assert!(b.f.told.notes.borrow().contains(&note));
}

#[test]
fn blocking_again_waits_again() {
    let b = run_blocked(
        vec![
            blocked_report(build_path(1), QUESTION),
            answered(build_path(1), "BLOCKED: postgres or sqlite?"),
            answered(build_path(1), "report 1"),
        ],
        None,
    );
    let f = &b.f;
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert_eq!(blocked_files(&f.host), ["build-1.blocked-1.md", "build-1.blocked-2.md"]);
    let second = f.host.state().files.get(&format!("{D}/build-1.blocked-2.md")).cloned();
    assert_eq!(second.as_deref(), Some("BLOCKED: postgres or sqlite?"));
    assert_eq!(b.builds.borrow()[1..], [blocked_prompt(&build_1(), 1), blocked_prompt(&build_1(), 2)]);
    let blocked: Vec<String> = f.told.titles().into_iter().filter(|t| t.contains("blocked")).collect();
    assert_eq!(blocked, ["Builder is blocked: which database?", "Builder is blocked: postgres or sqlite?"]);
    assert_eq!(prompts_to(f, "review-a1b2c3"), 1);
}

#[test]
fn each_rounds_report_has_its_own_count() {
    let b = run_blocked(
        vec![
            blocked_report(build_path(1), QUESTION),
            answered(build_path(1), "report 1"),
            blocked_report(build_path(2), QUESTION),
            answered(build_path(2), "report 2"),
        ],
        Some(vec![review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)]),
    );
    let mut wf = b.f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(blocked_files(&b.f.host), ["build-1.blocked-1.md", "build-2.blocked-1.md"]);
    assert_eq!(wf.state().round, 2);
}

#[test]
fn an_empty_report_after_a_block_aborts_and_ends_the_block() {
    let b = run_blocked(vec![blocked_report(build_path(1), QUESTION), answered(build_path(1), "\n")], None);
    let message = failure(b.f.workflow(RunSettings::default()).run());
    assert!(message.contains("build-1.md was written empty by the Builder"), "{message}");
    let saved = b.f.saved();
    assert_eq!((saved.get("blocked"), saved.get("prompted")), (Some(&Json::Null), Some(&Json::Null)));
}

#[test]
fn a_step_from_a_workflow_file_can_block() {
    let team = load_workflow_file(&format!("{EXAMPLES}/product-team.toml"), &Default::default()).unwrap();
    let tests_md = |s: &RunState| format!("{}/tests.md", s.dir());
    let (blocks, specs) = (seen(), seen());
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&team), vec![
        ("spec", vec![recording(spec_turn(), &specs)]),
        ("design", vec![writes(|s| format!("{}/design.md", s.dir()), "the design", "done")]),
        (
            "tests",
            vec![
                recording(blocked_report(tests_md, "which test runner?"), &blocks),
                recording(answered(tests_md, "tests written"), &blocks),
            ],
        ),
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    bounded(&f);
    let settings = RunSettings { pipeline: Some(team), ..RunSettings::default() };
    assert_eq!(f.workflow(settings).run(), verdict(APPROVE));
    assert_eq!(blocked_files(&f.host), ["tests.blocked-1.md"]);
    assert!(blocks.borrow()[0].ends_with(&note()));
    assert_eq!(blocks.borrow()[1], blocked_prompt(&format!("{D}/tests.md"), 1));
    let note = ("QA Engineer is blocked: which test runner?".to_string(), "pane w1:p3".to_string());
    assert!(f.told.notes.borrow().contains(&note));
    assert!(!specs.borrow()[0].contains("BLOCKED"));
}

#[test]
fn a_blocked_verdict_file_gets_the_verdict_retry() {
    let review = |s: &RunState| s.review_path(1);
    let b = run_blocked(
        vec![build_turn(1)],
        Some(vec![writes(review, "BLOCKED: should I approve?", "done"), review_turn(1, APPROVE)]),
    );
    let f = &b.f;
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(b.reviews.borrow()[1], retry_prompt(&wf.state().review_path(1)));
    let rejected = f.host.state().files.get(&format!("{D}/review-1.rejected.md")).cloned();
    assert_eq!(rejected.as_deref(), Some("BLOCKED: should I approve?"));
    assert!(blocked_files(&f.host).is_empty());
    assert!(!f.told.titles().iter().any(|t| t.contains("blocked")));
}

#[test]
fn workflow_definitions_do_not_contain_the_note() {
    let default = &*DEFAULT_WORKFLOW;
    assert!(!workflow_toml(default).contains("BLOCKED"));
    assert!(!default.steps().iter().flat_map(|st| st.templates()).any(|t| t.contains("BLOCKED")));
}

#[test]
fn the_wait_and_the_rewrite_are_a_human_paced_turn() {
    let b = run_blocked(vec![taking(600, blocked_report(build_path(1), QUESTION)), answered(build_path(1), "report 1")], None);
    let f = &b.f;
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    let end = f.host.state().mtimes[&build_1()] - HOST_EPOCH;
    let turns = &wf.state().turns;
    assert_eq!(turns.len(), 4);
    assert_eq!(record("build", "build-1.blocked-1.md", Some(0), Some(600)), turns[1]);
    assert_eq!(record("build", "build-1.md", Some(600), Some(end)).with("human_paced", true), turns[2]);
    assert_eq!(f.host.state().mtimes[&blocked_1()], HOST_EPOCH + 600);
    let summary = f.host.state().files.get(&format!("{D}/summary.md")).cloned().unwrap();
    assert!(
        summary.contains(
            "| build | build | 1 | claude | default | 2026-10-03 04:00:00 | 10m 00s | build-1.blocked-1.md |\n| build \
             (human-paced) | build | 1 | claude | default | 2026-10-03 04:10:00 |"
        ),
        "{summary}"
    );
}

#[test]
fn a_state_saved_before_blocks_loads_and_resumes() {
    let mut saved = saved_run("build", 1, &["spec", "build"]);
    saved.prompted = Some("build-1.md".into());
    let Json::Object(mut saved) = saved.to_json() else { unreachable!() };
    saved.remove("blocked");
    let state = RunState::from_saved(&saved).unwrap();
    assert_eq!(state.blocked, None);
    let f = Fixture::resume(Rc::new(FakeHost::default()), state.clone(), vec![("review", vec![review_turn(1, APPROVE)])], &["build"], &[]);
    let host = Rc::clone(&f.host);
    let mut build = build_turn(1);
    f.clock.hook(move |_| {
        build("", &state, &host);
    });
    assert_eq!(f.workflow(f.as_saved()).run(), verdict(APPROVE));
    assert_eq!(prompts_to(&f, "build-a1b2c3"), 0);
}

// -- TestBlockedResume: the orchestrator dies at each point of a block, and a resume carries on from there. -------

/// The orchestrator process dies here: unwound past every handler of the Workflow's, it saves nothing more. Python's
/// Killed, a BaseException.
struct Killed;

/// Kill the orchestrator: unwind with Killed, past the panic hook, as nothing but the test's catch sees it.
fn die() -> ! {
    resume_unwind(Box::new(Killed))
}

/// What a KillingHost asks of each write and rename: (what, path, text), what being "write" or "rename" and text the
/// written text; true kills the orchestrator there.
type KillAt = fn(&str, &str, Option<&str>) -> bool;

/// A FakeHost whose orchestrator is killed at the first write or rename kill_at picks: Python's KillingHost.
fn killing_host(kill_at: KillAt) -> Rc<FakeHost> {
    let host = Rc::new(FakeHost::default());
    let mut s = host.state();
    s.before_write = Some(Box::new(move |path, text| {
        if kill_at("write", path, Some(text)) {
            die();
        }
        Ok(())
    }));
    s.before_rename = Some(Box::new(move |path, _| {
        if kill_at("rename", path, None) {
            die();
        }
    }));
    drop(s);
    host
}

fn saving_a_block(what: &str, path: &str, text: Option<&str>) -> bool {
    let blocked = || Json::parse(text.unwrap_or_default()).is_ok_and(|s| s.get("blocked").is_some_and(|b| !b.is_null()));
    what == "write" && path.ends_with("state.json") && blocked()
}

fn renaming_a_report(what: &str, path: &str, _: Option<&str>) -> bool {
    what == "rename" && path == build_1()
}

fn never(_: &str, _: &str, _: Option<&str>) -> bool {
    false
}

/// Where kill kills the orchestrator besides the host's own kill_at.
#[derive(PartialEq)]
enum Also {
    Nowhere,
    /// When it focuses the Builder's pane.
    AtFocus,
    /// In the wait after the Builder's answer prompt.
    InWait,
}

/// Run a default run, its Builder blocking, on host until the kill; the state.json it left, and the prompts the
/// Builder got after its BLOCKED report. The host's kill_at is disarmed afterwards.
fn kill(host: Rc<FakeHost>, also: Also) -> (RunState, Vec<String>) {
    let wait = seen();
    let waits = recording(showing("idle"), &wait);
    let f = Fixture::on(Rc::clone(&host), new_state(), vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![blocked_report(build_path(1), QUESTION), waits]),
    ]);
    if also == Also::AtFocus {
        f.herdr.state().before_call = Some(Box::new(|call| {
            if *call == (Call::Focus { name: "build-a1b2c3".into() }) {
                die();
            }
        }));
    }
    if also == Also::InWait {
        let wait = Rc::clone(&wait);
        f.clock.hook(move |_| {
            if !wait.borrow().is_empty() {
                die();
            }
        });
    }
    bounded(&f);
    let died = catch_unwind(AssertUnwindSafe(|| f.workflow(RunSettings::default()).run()));
    assert!(died.is_err_and(|e| e.is::<Killed>()), "the orchestrator was not killed");
    host.state().before_write = None;
    host.state().before_rename = None;
    let state = RunState::from_saved(&f.saved()).unwrap();
    let prompts = wait.borrow().clone();
    (state, prompts)
}

/// Resume state on host, the Builder writing its report once the wait is under way, alive or in sessions as given;
/// what it was prompted with.
fn resume_blocked(state: RunState, host: Rc<FakeHost>, alive: &[&str], sessions: &[(&str, &str)]) -> Vec<String> {
    let seen = seen();
    let f = Fixture::resume(Rc::clone(&host), state.clone(), vec![
        ("build", vec![recording(idle(), &seen)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ], alive, sessions);
    let mut build = build_turn(1);
    f.clock.hook(move |_| {
        let answer = !blocked_files(&host).is_empty() && !host.state().files.contains_key(&build_1());
        if answer {
            build("", &state, &host);
        }
    });
    bounded(&f);
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(blocked_files(&f.host), ["build-1.blocked-1.md"]);
    assert!(f.host.state().files.get(&blocked_1()).unwrap().starts_with("BLOCKED: "));
    assert!(f.told.notes.borrow().contains(&("Builder is blocked: which database?".into(), "pane w1:p2".into())));
    assert!(called(&f, &Call::Focus { name: "build-a1b2c3".into() }));
    let s = wf.state();
    let files: Vec<String> =
        s.turns.iter().filter(|t| t.get("step") == Some(&Json::from("build"))).map(|t| t.get("file").map(Json::python_str).unwrap_or_default()).collect();
    assert_eq!(files, ["build-1.blocked-1.md", "build-1.md"]);
    assert_eq!(s.turns[2].get("human_paced"), Some(&Json::Bool(true)));
    assert_eq!(s.blocked, None);
    let prompts = seen.borrow().clone();
    prompts
}

#[test]
fn killed_before_saving_the_block() {
    let host = killing_host(saving_a_block);
    let (state, _) = kill(Rc::clone(&host), Also::Nowhere);
    assert_eq!(state.blocked, None);
    assert!(host.state().files.contains_key(&build_1()));
    assert_eq!(resume_blocked(state, host, &["build"], &[]), [blocked_prompt(&build_1(), 1)]);
}

#[test]
fn killed_between_saving_the_block_and_the_rename() {
    let host = killing_host(renaming_a_report);
    let (state, _) = kill(Rc::clone(&host), Also::Nowhere);
    let block = state.blocked.clone().map(|b| Json::Object(b).dumps());
    assert_eq!(block.as_deref(), Some(r#"{"file": "build-1.md", "question": "which database?", "k": 1}"#));
    assert!(host.state().files.contains_key(&build_1()));
    assert!(!host.state().files.contains_key(&blocked_1()));
    assert_eq!(resume_blocked(state, host, &["build"], &[]), [blocked_prompt(&build_1(), 1)]);
}

#[test]
fn killed_after_the_rename_before_the_prompt() {
    let host = killing_host(never);
    let (state, wait) = kill(Rc::clone(&host), Also::AtFocus);
    let k = state.blocked.as_ref().and_then(|b| b.get("k").cloned());
    assert_eq!((k, state.prompted.clone(), wait), (Some(Json::from(1)), None, vec![]));
    assert!(!host.state().files.contains_key(&build_1()));
    assert_eq!(resume_blocked(state, host, &["build"], &[]), [blocked_prompt(&build_1(), 1)]);
}

/// A run killed in the wait for the human's answer, its answer prompt delivered: its state and host.
fn killed_waiting() -> (RunState, Rc<FakeHost>) {
    let host = killing_host(never);
    let (state, wait) = kill(Rc::clone(&host), Also::InWait);
    let k = state.blocked.as_ref().and_then(|b| b.get("k").cloned());
    let expected = (Some(Json::from(1)), Some("build-1.md".to_string()), vec![blocked_prompt(&build_1(), 1)]);
    assert_eq!((k, state.prompted.clone(), wait), expected);
    (state, host)
}

#[test]
fn killed_waiting_with_the_agent_alive_only_waits() {
    let (state, host) = killed_waiting();
    assert_eq!(resume_blocked(state, host, &["build"], &[]), Vec::<String>::new());
}

#[test]
fn killed_waiting_with_the_agent_resumed_continues() {
    let (state, host) = killed_waiting();
    assert_eq!(resume_blocked(state, host, &[], &[("build", "s-build")]), [continue_prompt(&build_1())]);
}

#[test]
fn killed_waiting_with_the_agent_restarted_gets_the_blocked_prompt() {
    let (mut state, host) = killed_waiting();
    state.agents.get_mut("build").unwrap().remove("session");
    assert_eq!(resume_blocked(state, host, &[], &[]), [blocked_prompt(&build_1(), 1)]);
}

#[test]
fn a_report_written_while_no_orchestrator_watched_ends_the_wait() {
    let (state, host) = killed_waiting();
    build_turn(1)("", &state, &host);
    let f = Fixture::resume(host, state, vec![("review", vec![review_turn(1, APPROVE)])], &["build"], &[]);
    bounded(&f);
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(f.told.titles(), [format!("Run finished: {APPROVE}")]);
    assert_eq!(prompts_to(&f, "build-a1b2c3"), 0);
    assert_eq!(blocked_files(&f.host), ["build-1.blocked-1.md"]);
    assert_eq!(wf.state().blocked, None);
}
