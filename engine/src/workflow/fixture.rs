//! What the Workflow tests run on, as tests/test_orchestrator.py's make_workflow, saved_run and resume build it: a
//! FakeHost, a FakeHerdr playing the roles' scripts, a FakeClock, the human as Told, and the turn helpers.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

use super::{Clocks, Collaborators, Human, RunSettings, Workflow};
use crate::ci::fake::{jenkins_on, FakeJenkins, Outcome};
use crate::ci::Jenkins;
use crate::herdr::fake::{FakeHerdr, Turn};
use crate::http::Http;
use crate::pypi::fake::FakePyPI;
use crate::pypi::PyPI;
use crate::host::fake::{FakeClock, FakeHost};
use crate::host::Host;
use crate::interrupt::Interrupt;
use crate::py::json::{Json, Map};
use crate::runs::pid_alive;
use crate::state::RunState;

/// The run every test starts unless it says otherwise: Python's RunState("20260929-120000-a1b2c3", ...).
pub(crate) const RUN_ID: &str = "20260929-120000-a1b2c3";
pub(crate) const TASK: &str = "add a rate limiter";

/// A FakeClock as a Workflow's time: its monotonic and wall clocks both read the fake's time.
impl Clocks for FakeClock {
    fn sleep(&self, seconds: f64) {
        FakeClock::sleep(self, seconds);
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
    /// The Jenkins behind jenkins, Python's FakeCI: there for every fixture, so that a run without the quality gate
    /// can be shown to ask it nothing.
    pub ci: Arc<FakeJenkins>,
    /// The quality job the Workflow is given: None for a run without the quality gate, until gated gives it one.
    pub jenkins: Option<Jenkins>,
    /// The PyPI behind the Workflow's index, with no project unless a test adds them.
    pub pypi: Arc<FakePyPI>,
    /// The index the Workflow is given, on pypi.
    index: PyPI,
}

/// The default run's new state.
pub(crate) fn new_state() -> RunState {
    RunState::new(RUN_ID, TASK, "/proj")
}

/// A new run's state of pipeline, as main makes it for the workflow: Python's new_run, in the workflow's first step.
pub(crate) fn new_run(pipeline: &crate::pipeline::Pipeline) -> RunState {
    let mut state = new_state();
    state.phase = pipeline.steps()[0].id.clone();
    state.workflow = pipeline.name().to_string();
    state
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
        let interrupt = Interrupt::new();
        herdr.state().interrupt = Some(interrupt.clone());
        let clock = Rc::new(FakeClock::default());
        if host.state().clock.is_none() {
            host.state().clock = Some(Rc::clone(&clock));
        }
        let pypi = FakePyPI::from_json(Map::new());
        let index = PyPI::new(Arc::clone(&pypi) as Arc<dyn Http>);
        Fixture {
            host,
            herdr,
            clock,
            told: Told::default(),
            interrupt,
            pid_alive: Box::new(pid_alive),
            state,
            ci: FakeJenkins::new(),
            jenkins: None,
            pypi,
            index,
        }
    }

    /// The fixture with the quality gate on: the Workflow is given the quality job on ci, with every credential, as
    /// Python's gated gives it.
    pub fn gated(mut self) -> Fixture {
        self.jenkins = Some(jenkins_on(&self.ci));
        self
    }

    /// The fixture with the quality gate on, asked with these credentials of CI_ENV, by name: Python's gated with
    /// env.
    pub fn gated_with(mut self, credentials: &Map<String>) -> Fixture {
        let ci_env = "/home/u/.config/ai-agents-orchestrator/ci.env";
        self.jenkins = Some(Jenkins::new(crate::ci::fake::JOB, credentials, ci_env, Arc::clone(&self.ci) as Arc<dyn Http>));
        self
    }

    /// The fixture whose Workflow asks this PyPI instead.
    pub fn with_pypi(mut self, pypi: Arc<FakePyPI>) -> Fixture {
        self.index = PyPI::new(Arc::clone(&pypi) as Arc<dyn Http>);
        self.pypi = pypi;
        self
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
            jenkins: self.jenkins.as_ref(),
            pypi: &self.index,
        };
        Workflow::new(with, self.state.clone(), settings)
    }

    /// Add turns to those the role's agent plays, after the ones it has: for a turn that needs the fixture's own
    /// fakes, which the script is written before.
    pub fn play(&self, role: &str, turns: Vec<Turn>) {
        self.herdr.state().script.entry(role.to_string()).or_default().extend(turns);
    }

    /// The settings a resume of the fixture's state runs with: the defaults, the pull request as the state saved it,
    /// as main gives a resumed run the choice it was started with (Python's resume).
    pub fn as_saved(&self) -> RunSettings {
        RunSettings { pull_request: self.state.pull_request, ..RunSettings::default() }
    }

    /// Put text at path as Python's tests put a file straight into FakeHost.files: no write is recorded, and the
    /// file has no mtime of its own, so mtime() gives the time it is asked.
    pub fn put(&self, path: &str, text: &str) {
        self.host.state().files.insert(path, text.to_string());
    }

    /// The run's state.json as the host holds it now.
    pub fn saved(&self) -> Map<Json> {
        let text = self.host.state().files.get(&format!("{}/state.json", self.state.dir())).cloned();
        match Json::parse(&text.expect("the run has a state.json")) {
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

/// The Builder's one turn of test_support's solo workflow: it changes /proj/limiter.py and reports.
pub(crate) fn solo_turn() -> Turn {
    Box::new(|_, state, host| {
        host.write("/proj/limiter.py", "version 1", false).expect("the fake host writes");
        host.write(&format!("{}/build.md", state.dir()), "solo report", false).expect("the fake host writes");
        "done".to_string()
    })
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

/// The prompt that tells a resumed agent to go on writing path.
pub(crate) fn continue_prompt(path: &str) -> String {
    let value = |name: &str| (name == "path").then(|| path.to_string());
    crate::py::format::format(crate::prompts::CONTINUE_PROMPT, &value).expect("the built-in prompt fills in")
}

/// turn, its agent working for seconds of the host's time before it plays turn: the host's clock moves on without
/// the workflow sleeping. Panics on a host without a clock, which Fixture::on gives every host.
pub(crate) fn taking(seconds: u32, mut turn: Turn) -> Turn {
    Box::new(move |prompt, state, host| {
        let clock = host.state().clock.clone().expect("a host with a clock");
        clock.advance(f64::from(seconds));
        turn(prompt, state, host)
    })
}

/// turn, after which the human presses Ctrl-C, interrupting its prompt (FakeHerdrState::interrupt): Python's turn that
/// plays turn and then raises KeyboardInterrupt.
pub(crate) fn then_interrupted(interrupt: &Interrupt, mut turn: Turn) -> Turn {
    let interrupt = interrupt.clone();
    Box::new(move |prompt, state, host| {
        let status = turn(prompt, state, host);
        interrupt.raise();
        status
    })
}

/// A turn whose agent works on and writes nothing, until the test writes its file.
pub(crate) fn working() -> Turn {
    showing("working")
}

/// The time on a FakeHost whose clock reads seconds, as a turn record has it.
pub(crate) fn at(seconds: i64) -> String {
    crate::summary::iso_utc(crate::host::fake::HOST_EPOCH + seconds)
}

/// The turn record of a step that started and ended at these host clock readings (None: not known), as Python's
/// turn() makes it: equal to a record that holds each of its fields, whatever session and tokens that has unless
/// with gives them.
#[derive(Clone, Debug)]
pub(crate) struct Record(Map<Json>);

/// record of a turn of round 1 and quality round 0, by step's own role, in claude with no model, not human-paced.
pub(crate) fn record(step: &str, file: &str, start: Option<i64>, end: Option<i64>) -> Record {
    let time = |t: Option<i64>| t.map_or(Json::Null, |t| Json::from(at(t)));
    let seconds = match (start, end) {
        (Some(start), Some(end)) => Json::from(end - start),
        _ => Json::Null,
    };
    Record(
        [
            ("step", Json::from(step)),
            ("role", Json::from(step)),
            ("round", Json::from(1)),
            ("quality_round", Json::from(0)),
            ("file", Json::from(file)),
            ("agent", Json::from("claude")),
            ("model", Json::Null),
            ("human_paced", Json::Bool(false)),
            ("started_at", time(start)),
            ("ended_at", time(end)),
            ("seconds", seconds),
        ]
        .into_iter()
        .collect(),
    )
}

impl Record {
    /// The record with field key holding value instead, or as well.
    pub fn with(mut self, key: &str, value: impl Into<Json>) -> Record {
        self.0.insert(key, value.into());
        self
    }

    /// The record as a saved run holds it, Python's dict(turn(...)): only its fields.
    pub fn map(&self) -> Map<Json> {
        self.0.clone()
    }
}

impl PartialEq<Map<Json>> for Record {
    fn eq(&self, other: &Map<Json>) -> bool {
        self.0.iter().all(|(key, value)| other.get(key) == Some(value))
    }
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

// -- The quality gate's, by Python's names -----------------------------------------------------------------------

/// The ref of round 1's first quality round.
pub(crate) const REF_1_1: &str = "orchestrator-ci/a1b2c3-1-q1";

/// A build whose gate fails on an unused import: Python's outcome(**RED).
pub(crate) fn red() -> Outcome {
    Outcome::gate("ERROR", &["`limiter.py:3` error ruff F401: `os` imported but unused"])
}

/// A build whose gate fails on something else than red's: outcome(**RED2).
pub(crate) fn red2() -> Outcome {
    Outcome::gate("ERROR", &["`limiter.py:9` error mypy arg-type: Argument 1 has incompatible type"])
}

/// A build whose gate passes: outcome().
pub(crate) fn green() -> Outcome {
    Outcome::gate("OK", &[])
}

/// The Builder's answer to quality-<n>-<q>.md: it changes /proj/limiter.py and reports.
pub(crate) fn fix_turn(n: u32, q: u32) -> Turn {
    Box::new(move |_, state, host| {
        host.write("/proj/limiter.py", &format!("version {n}.{q}"), false).expect("the fake host writes");
        host.write(&state.quality_build_path(n, q), &format!("report {n} q{q}"), false).expect("the fake host writes");
        "done".to_string()
    })
}

/// FakeJenkins's request names without repeats in a row, which polls make.
pub(crate) fn collapsed(names: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for name in names {
        if out.last() != Some(name) {
            out.push(name.clone());
        }
    }
    out
}

/// Whether text holds the Jenkins token of the tests: Python's leaks.
pub(crate) fn holds_token(text: &str) -> bool {
    text.contains(crate::ci::fake::JENKINS_TOKEN)
}

/// A run saved in quality round q of round 1 of the default workflow, with the gate's job: Python's quality_state.
pub(crate) fn quality_state(q: u32) -> RunState {
    let mut state = saved_run("quality", 1, &["spec", "build"]);
    state.quality_job = Some(crate::ci::fake::JOB.to_string());
    state.quality_round = q;
    state
}

/// make_workflow with the quality gate, its builds playing outcomes, in order, and then green ones.
pub(crate) fn gated_building(script: Vec<(&str, Vec<Turn>)>, outcomes: Vec<Outcome>) -> Fixture {
    let f = Fixture::new(script).gated();
    f.ci.state().outcomes = outcomes.into();
    f
}

/// RunState::ci of a round in flight, as pairs of its fields.
pub(crate) fn ci_of(fields: &[(&str, Json)]) -> Map<Json> {
    fields.iter().cloned().collect()
}

impl Fixture {
    /// Python's resume_gated: a resume of state with the gate on, the ref in flight, if any, on origin, and round 1's
    /// build report written.
    pub fn resume_gated(host: Rc<FakeHost>, state: RunState, script: Vec<(&str, Vec<Turn>)>, alive: &[&str]) -> Fixture {
        if let (Some(Json::Str(git_ref)), Some(Json::Str(sha))) = (state.ci.get("ref"), state.ci.get("sha")) {
            let mut h = host.state();
            if !h.remote.contains_key(git_ref) {
                h.remote.insert(git_ref.as_str(), sha.clone());
            }
        }
        host.state().files.insert(state.build_path(1), "report 1".to_string());
        Fixture::resume(host, state, script, alive, &[]).gated()
    }

    /// The text of the run's quality-<n>-<q>.md. Panics when it is not written.
    pub fn quality_file(&self, n: u32, q: u32) -> String {
        let path = self.state.quality_path(n, q);
        self.host.state().files.get(&path).cloned().unwrap_or_else(|| panic!("no {path}"))
    }

    /// The settings a resume with the gate runs with: as_saved's, with the saved limit of quality rounds, as Python's
    /// resume_gated gives it.
    pub fn as_saved_gated(&self) -> RunSettings {
        RunSettings { max_quality_rounds: self.state.max_quality_rounds, ..self.as_saved() }
    }
}

/// template, one of crate::prompts' prompts, filled in with values by name.
pub(crate) fn filled(template: &str, values: &[(&str, &str)]) -> String {
    let value = |name: &str| values.iter().find(|(n, _)| *n == name).map(|(_, v)| v.to_string());
    crate::py::format::format(template, &value).expect("the prompt fills in")
}

/// The QUALITY_FIX_PROMPT the Builder of state's run hears for quality round q of round n.
pub(crate) fn quality_fix(state: &RunState, n: u32, q: u32) -> String {
    let (quality_path, report_path) = (state.quality_path(n, q), state.quality_build_path(n, q));
    filled(crate::prompts::QUALITY_FIX_PROMPT, &[("quality_path", &quality_path), ("report_path", &report_path)])
}

/// note, one of the quality notes the verdict step hears, of the quality file at quality_path.
pub(crate) fn quality_note(note: &str, quality_path: &str) -> String {
    filled(note, &[("quality_path", quality_path)])
}
