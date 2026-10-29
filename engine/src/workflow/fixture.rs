//! What the Workflow tests run on, as tests/test_orchestrator.py's make_workflow, saved_run and resume build it: a
//! FakeHost, a FakeHerdr playing the roles' scripts, a FakeClock, the human as Told, and the turn helpers.

use std::cell::{Cell, RefCell};
use std::rc::Rc;

use super::{Clocks, Collaborators, Human, RunSettings, Workflow};
use crate::herdr::fake::{FakeHerdr, Turn};
use crate::host::fake::FakeHost;
use crate::host::Host;
use crate::interrupt::Interrupt;
use crate::py::json::Map;
use crate::runs::pid_alive;
use crate::state::RunState;

/// The run every test starts unless it says otherwise: Python's RunState("20260929-120000-a1b2c3", ...).
pub(crate) const RUN_ID: &str = "20260929-120000-a1b2c3";
pub(crate) const TASK: &str = "add a rate limiter";

/// What a test plays after each sleep, given the time then.
type ClockHook = Box<dyn FnMut(f64)>;

/// Time that passes only when the workflow sleeps; hooks play the world meanwhile, each given the time now.
#[derive(Default)]
pub(crate) struct FakeClock {
    now: Cell<f64>,
    hooks: RefCell<Vec<ClockHook>>,
}

impl FakeClock {
    pub fn now(&self) -> f64 {
        self.now.get()
    }

    /// Let seconds pass without the workflow sleeping, as a test's own herdr wait does.
    pub fn advance(&self, seconds: f64) {
        self.now.set(self.now.get() + seconds);
    }

    /// Run hook after every sleep.
    pub fn hook(&self, hook: impl FnMut(f64) + 'static) {
        self.hooks.borrow_mut().push(Box::new(hook));
    }
}

impl Clocks for FakeClock {
    fn sleep(&self, seconds: f64) {
        self.advance(seconds);
        let now = self.now();
        for hook in self.hooks.borrow_mut().iter_mut() {
            hook(now);
        }
    }

    fn monotonic(&self) -> f64 {
        self.now()
    }

    fn wall(&self) -> f64 {
        self.now()
    }
}

/// The human as the tests see them: every notification and log line, in order.
#[derive(Default)]
pub(crate) struct Told {
    /// (title, body) of each notification.
    pub notes: RefCell<Vec<(String, String)>>,
    pub log: RefCell<Vec<String>>,
}

impl Told {
    /// The notifications' titles, as Python's tests collect notes.
    pub fn titles(&self) -> Vec<String> {
        self.notes.borrow().iter().map(|(title, _)| title.clone()).collect()
    }

    pub fn lines(&self) -> Vec<String> {
        self.log.borrow().clone()
    }
}

impl Human for Told {
    fn notify(&self, title: &str, body: &str) {
        self.notes.borrow_mut().push((title.to_string(), body.to_string()));
    }

    fn log(&self, line: &str) {
        self.log.borrow_mut().push(line.to_string());
    }
}

/// A Workflow's fakes, kept by the test while the Workflow borrows them, to script and read before, during and after
/// its run.
pub(crate) struct Fixture {
    pub host: Rc<FakeHost>,
    pub herdr: Rc<FakeHerdr>,
    pub clock: Rc<FakeClock>,
    pub told: Told,
    pub interrupt: Interrupt,
    /// The Workflow's pid_alive: crate::runs::pid_alive unless a test sets its own before making the Workflow.
    pub pid_alive: Box<dyn Fn(i128) -> bool>,
    /// The state the Workflow starts from.
    pub state: RunState,
}

/// The default run's new state.
pub(crate) fn new_state() -> RunState {
    RunState::new(RUN_ID, TASK, "/proj")
}

impl Fixture {
    /// make_workflow: a new run on a default FakeHost, the roles played by script.
    pub fn new(script: Vec<(&str, Vec<Turn>)>) -> Fixture {
        Fixture::on(Rc::new(FakeHost::default()), new_state(), script)
    }

    /// make_workflow with host and state: the run state describes on host, the roles played by script. The host's
    /// clock becomes the fixture's unless it has one.
    pub fn on(host: Rc<FakeHost>, state: RunState, script: Vec<(&str, Vec<Turn>)>) -> Fixture {
        let herdr = Rc::new(FakeHerdr::new(Rc::clone(&host), state.clone(), script));
        let clock = Rc::new(FakeClock::default());
        if host.state().clock.is_none() {
            let time = Rc::clone(&clock);
            host.state().clock = Some(Rc::new(move || time.now()));
        }
        Fixture {
            host,
            herdr,
            clock,
            told: Told::default(),
            interrupt: Interrupt::new(),
            pid_alive: Box::new(pid_alive),
            state,
        }
    }

    /// resume: the saved run state describes, in workspace w1 whose panes its agents had, those of the roles alive
    /// still running, each of sessions' roles in its session, and each of files' paths written.
    pub fn resume(
        host: Rc<FakeHost>,
        mut state: RunState,
        script: Vec<(&str, Vec<Turn>)>,
        alive: &[&str],
        sessions: &[(&str, &str)],
    ) -> Fixture {
        for (role, session) in sessions {
            state.agents.get_mut(role).expect("a saved agent").insert("session", session.to_string());
        }
        let f = Fixture::on(host, state, script);
        {
            let mut h = f.herdr.state();
            h.workspaces.insert("w1".to_string());
            let panes: Vec<String> = f.state.agents.values().filter_map(|a| a.get("pane").cloned()).collect();
            h.live_panes.extend(panes);
            h.live_panes.insert("w1:p1".to_string());
            h.panes = 3;
            for role in alive {
                h.statuses.insert(format!("{role}-a1b2c3"), "working".to_string());
            }
        }
        f
    }

    /// A Workflow of the fixture's state on its fakes. Panics on what Workflow::new refuses, which try_workflow gives.
    pub fn workflow(&self, settings: RunSettings) -> Workflow<'_> {
        self.try_workflow(settings).expect("Workflow::new takes the settings")
    }

    pub fn try_workflow(&self, settings: RunSettings) -> Result<Workflow<'_>, crate::error::OrchestratorError> {
        let with = Collaborators {
            host: &*self.host as &dyn Host,
            herdr: &*self.herdr,
            clocks: &*self.clock,
            human: &self.told,
            interrupt: &self.interrupt,
            pid_alive: &*self.pid_alive,
        };
        Workflow::new(with, self.state.clone(), settings)
    }

    /// The run's state.json as the host holds it now.
    pub fn saved(&self) -> Map<crate::py::json::Json> {
        let text = self.host.state().files.get(&format!("{}/state.json", self.state.dir())).cloned();
        match crate::py::json::Json::parse(&text.expect("the run has a state.json")) {
            Ok(crate::py::json::Json::Object(saved)) => saved,
            other => panic!("state.json is no object: {other:?}"),
        }
    }
}

// -- The turns the tests script, by Python's names ---------------------------------------------------------------

/// A turn that writes text to the path path_of gives for the run's state, then shows status.
pub(crate) fn writes(path_of: impl Fn(&RunState) -> String + 'static, text: &str, status: &str) -> Turn {
    let (text, status) = (text.to_string(), status.to_string());
    Box::new(move |_, state, host| {
        host.write(&path_of(state), &text, false).expect("the fake host writes");
        status.clone()
    })
}

/// A turn that writes nothing and settles.
pub(crate) fn idle() -> Turn {
    Box::new(|_, _, _| "done".to_string())
}

/// A turn that shows status and writes nothing.
pub(crate) fn showing(status: &str) -> Turn {
    let status = status.to_string();
    Box::new(move |_, _, _| status.clone())
}

pub(crate) const SPEC: &str = "# Add a token-bucket rate limiter\n\n## Goal\nlimit requests";

/// The Spec Collector's turn: the spec, titled "Add a token-bucket rate limiter".
pub(crate) fn spec_turn() -> Turn {
    writes(|s| s.spec_path(), SPEC, "done")
}

/// The Builder's turn of round n: it changes /proj/limiter.py and reports.
pub(crate) fn build_turn(n: u32) -> Turn {
    Box::new(move |_, state, host| {
        host.write("/proj/limiter.py", &format!("version {n}"), false).expect("the fake host writes");
        host.write(&state.build_path(n), &format!("report {n}"), false).expect("the fake host writes");
        "done".to_string()
    })
}

/// The Reviewer's turn of round n, with verdict.
pub(crate) fn review_turn(n: u32, verdict: &str) -> Turn {
    writes(move |s| s.review_path(n), &format!("**VERDICT: {verdict}**\n1. finding"), "done")
}

/// A review of round n that starts with no VERDICT line.
pub(crate) fn malformed_review(n: u32, text: &str) -> Turn {
    writes(move |s| s.review_path(n), text, "done")
}

/// turn, recording each prompt it is given in seen.
pub(crate) fn recording(mut turn: Turn, seen: &Rc<RefCell<Vec<String>>>) -> Turn {
    let seen = Rc::clone(seen);
    Box::new(move |prompt, state, host| {
        seen.borrow_mut().push(prompt.to_string());
        turn(prompt, state, host)
    })
}

/// A list the recording turns fill.
pub(crate) fn seen() -> Rc<RefCell<Vec<String>>> {
    Rc::new(RefCell::new(vec![]))
}

/// The retry prompt for the verdict file at path.
pub(crate) fn retry_prompt(path: &str) -> String {
    let rejected = format!("{}.rejected.md", path.strip_suffix(".md").unwrap_or(path));
    let value = |name: &str| match name {
        "path" => Some(path.to_string()),
        "rejected_path" => Some(rejected.clone()),
        "approve" => Some(crate::constants::APPROVE.to_string()),
        "changes" => Some(crate::constants::CHANGES_REQUESTED.to_string()),
        _ => None,
    };
    crate::py::format::format(crate::prompts::RETRY_VERDICT_PROMPT, &value).expect("the built-in prompt fills in")
}

/// What every prompt of a step that can block ends with.
pub(crate) fn note() -> String {
    let text = crate::py::format::format(crate::prompts::BLOCKED_NOTE, &|_| None).expect("the built-in note fills in");
    format!("\n\n{text}")
}

/// A RunState saved by an earlier orchestrator in phase and round rnd, in workspace w1 whose root pane is w1:p1, with
/// agents for roles: Python's saved_run. Its base is abc123 past the spec phase.
pub(crate) fn saved_run(phase: &str, rnd: u32, roles: &[&str]) -> RunState {
    let mut state = new_state();
    state.phase = phase.to_string();
    state.round = rnd;
    state.workspace_id = "w1".to_string();
    state.root_pane = "w1:p1".to_string();
    state.base = (phase != "spec").then(|| "abc123".to_string());
    for role in roles {
        let pane = match *role {
            "spec" => "w1:p1",
            "build" => "w1:p2",
            _ => "w1:p3",
        };
        let agent = [("name", format!("{role}-a1b2c3")), ("pane", pane.to_string())].into_iter().collect();
        state.agents.insert(*role, agent);
    }
    state
}
