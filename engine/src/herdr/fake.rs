//! FakeHerdr, the herdr the Workflow tests run on: each role's agent played by a script of turns, as
//! tests/test_orchestrator.py's FakeHerdr plays it.

use std::cell::{RefCell, RefMut};
use std::collections::{HashMap, HashSet, VecDeque};
use std::rc::Rc;

use super::{Agent, Direction, Herdr, HerdrError};
use crate::harness::{harness, Resume};
use crate::host::fake::FakeHost;
use crate::host::Host;
use crate::state::RunState;

/// One turn of a role's agent: given the prompt it was sent, the run's state and the host, it may write files, as an
/// agent writes its handoff file, and gives the status the agent shows afterwards.
pub(crate) type Turn = Box<dyn FnMut(&str, &RunState, &FakeHost) -> String>;

/// herdr played in memory: each agent prompted plays its role's next turn from the script at once. An agent's role is
/// its name up to the first `-`, as the engine names agents `<role>-<key>`.
pub(crate) struct FakeHerdr {
    /// The machine the turns write to.
    host: Rc<FakeHost>,
    /// What it holds and records (FakeHerdrState), behind a RefCell as the trait's methods take &self.
    state: RefCell<FakeHerdrState>,
}

/// A command FakeHerdr was given, as Python's FakeHerdr records it in calls.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Call {
    Workspace { label: String },
    Split { pane: String, direction: Direction },
    Rename { pane: String, label: String },
    Start { name: String, pane: String, args: Vec<String> },
    Prompt { name: String },
    Focus { name: String },
    Close { workspace: String },
}

/// What a FakeHerdr holds and records. Each field is what Python's FakeHerdr has of that name.
pub(crate) struct FakeHerdrState {
    /// The run's state as a turn is given it while the host holds no state.json of the run: the test's. Once it holds
    /// one, a turn is given that: the Workflow's state as the run last saved it (its claim, and each phase and step),
    /// its worktree included, but behind the live state by whatever changed since, as Python's Workflow saves a turn's
    /// prompted marker only after the prompt. Python's turns share the live state, and may change it; a port of a test
    /// whose turn needs either needs the Workflow's own state handed to the fake (ticket 08).
    pub run: RunState,
    /// By role, its agent's turns still to play, in order.
    pub script: HashMap<String, VecDeque<Turn>>,
    /// Every command that changes something or shows the human something, in order.
    pub calls: Vec<Call>,
    /// How many panes were made, the root pane of each workspace included.
    pub panes: usize,
    /// The status of each agent alive, by name; an agent that has exited has none.
    pub statuses: HashMap<String, String>,
    /// The session each agent runs in, by name.
    pub sessions: HashMap<String, String>,
    /// Sessions a harness cannot resume: an agent started into one exits at once.
    pub lost_sessions: HashSet<String>,
    /// (name, kind) of each agent started, in order.
    pub kinds: Vec<(String, String)>,
    /// The workspaces open.
    pub workspaces: HashSet<String>,
    /// The panes open.
    pub live_panes: HashSet<String>,
    /// The until of each wait but a delivery check's.
    pub waits: Vec<Vec<String>>,
    /// (name, timeout_ms) of each wait for an agent to take a prompt: a wait until working.
    pub deliveries: Vec<(String, Option<u64>)>,
    /// By role, how many of its next prompts its agent drops: it stays as it was and writes nothing.
    pub dropped: HashMap<String, u32>,
    /// (name, text) of each prompt sent, dropped or not.
    pub prompts: Vec<(String, String)>,
    /// By name, whether the agent took its last prompt.
    pub taken: HashMap<String, bool>,
    /// Roles whose agent is blocked on a startup dialog when it starts.
    pub blocked_at_start: HashSet<String>,
    /// ("workspace" or "split", cwd) of each workspace created and pane split.
    pub cwds: Vec<(String, String)>,
    /// What every wait does instead of the above, when set, given (name, timeout_ms, until): a Python test's own
    /// FakeHerdr.wait.
    pub wait_with: Option<WaitWith>,
    /// The error close_workspace fails with, when set.
    pub close_error: Option<HerdrError>,
}

/// A test's own wait (FakeHerdrState::wait_with).
pub(crate) type WaitWith = Box<dyn FnMut(&str, Option<u64>, &[&str]) -> Result<String, HerdrError>>;

impl FakeHerdr {
    /// herdr playing each role's agent from script, turn by turn, on host, each turn given run.
    pub fn new(host: Rc<FakeHost>, run: RunState, script: Vec<(&str, Vec<Turn>)>) -> FakeHerdr {
        let script = script.into_iter().map(|(role, turns)| (role.to_string(), VecDeque::from(turns))).collect();
        FakeHerdr {
            host,
            state: RefCell::new(FakeHerdrState {
                run,
                script,
                calls: vec![],
                panes: 0,
                statuses: HashMap::new(),
                sessions: HashMap::new(),
                lost_sessions: HashSet::new(),
                kinds: vec![],
                workspaces: HashSet::new(),
                live_panes: HashSet::new(),
                waits: vec![],
                deliveries: vec![],
                dropped: HashMap::new(),
                prompts: vec![],
                taken: HashMap::new(),
                blocked_at_start: HashSet::new(),
                cwds: vec![],
                wait_with: None,
                close_error: None,
            }),
        }
    }

    /// The wait the fake plays when no wait_with is set: a wait until working (a delivery check) answers whether
    /// the agent took its last prompt, timing out when it did not and failing with agent_not_found for an agent that
    /// has exited; any other wait is recorded in waits and answers done. For a test's wait_with that changes the
    /// fake and then waits as it does.
    pub fn own_wait(&self, name: &str, timeout_ms: Option<u64>, until: &[&str]) -> Result<String, HerdrError> {
        let mut s = self.state();
        if until.contains(&"working") {
            // A taken prompt had the agent working, whatever it shows now.
            s.deliveries.push((name.to_string(), timeout_ms));
            if !s.statuses.contains_key(name) {
                return Err(HerdrError::new("agent_not_found", format!("agent target {name} not found")));
            }
            if !s.taken.get(name).copied().unwrap_or(false) {
                return Err(HerdrError::new("timeout", format!("agent {name} did not reach {until:?}")));
            }
            return Ok("working".to_string());
        }
        s.waits.push(until.iter().map(|u| u.to_string()).collect());
        Ok("done".to_string())
    }

    /// What the fake holds and records, to script it or look at it; dropped before the next command it is given.
    pub fn state(&self) -> RefMut<'_, FakeHerdrState> {
        self.state.borrow_mut()
    }
}

impl FakeHerdrState {
    /// A new pane of workspace w1.
    fn pane(&mut self) -> String {
        self.panes += 1;
        let pane = format!("w1:p{}", self.panes);
        self.live_panes.insert(pane.clone());
        pane
    }
}

/// The role of the agent named name.
fn role(name: &str) -> &str {
    name.split('-').next().unwrap_or(name)
}

/// The session a harness's arguments resume, as each harness takes them; None for a fresh one.
pub(crate) fn resumed_session(kind: &str, args: &[String]) -> Option<String> {
    match harness(kind).expect("a harness of AGENT_KINDS").resume {
        Resume::Subcommand(word) => (args.first().map(String::as_str) == Some(word)).then(|| args.get(1).cloned())?,
        Resume::Flag(flag) => args.get(args.iter().position(|a| a == flag)? + 1).cloned(),
    }
}

impl Herdr for FakeHerdr {
    fn create_workspace(&self, cwd: &str, label: &str) -> Result<(String, String), HerdrError> {
        let mut s = self.state();
        s.calls.push(Call::Workspace { label: label.to_string() });
        s.cwds.push(("workspace".to_string(), cwd.to_string()));
        s.workspaces.insert("w1".to_string());
        Ok(("w1".to_string(), s.pane()))
    }

    fn split(&self, pane: &str, direction: Direction, cwd: &str) -> Result<String, HerdrError> {
        let mut s = self.state();
        s.calls.push(Call::Split { pane: pane.to_string(), direction });
        s.cwds.push(("split".to_string(), cwd.to_string()));
        Ok(s.pane())
    }

    fn rename_pane(&self, pane: &str, label: &str) -> Result<(), HerdrError> {
        self.state().calls.push(Call::Rename { pane: pane.to_string(), label: label.to_string() });
        Ok(())
    }

    fn start_agent(&self, name: &str, pane: &str, kind: &str, args: &[String]) -> Result<bool, HerdrError> {
        let mut s = self.state();
        s.calls.push(Call::Start { name: name.to_string(), pane: pane.to_string(), args: args.to_vec() });
        s.kinds.push((name.to_string(), kind.to_string()));
        let session = resumed_session(kind, args).unwrap_or_else(|| format!("session-{}", s.calls.len()));
        if s.lost_sessions.contains(&session) {
            return Ok(true);
        }
        s.statuses.insert(name.to_string(), "idle".to_string());
        s.sessions.insert(name.to_string(), session);
        Ok(!s.blocked_at_start.contains(role(name)))
    }

    fn prompt(&self, name: &str, text: &str) -> Result<(), HerdrError> {
        let (mut turn, run) = {
            let mut s = self.state();
            s.calls.push(Call::Prompt { name: name.to_string() });
            s.prompts.push((name.to_string(), text.to_string()));
            if let Some(n) = s.dropped.get_mut(role(name)).filter(|n| **n > 0) {
                *n -= 1;
                s.taken.insert(name.to_string(), false);
                return Ok(());
            }
            s.taken.insert(name.to_string(), true);
            let turns = s.script.get_mut(role(name)).unwrap_or_else(|| panic!("no script for role {}", role(name)));
            let turn = turns.pop_front().unwrap_or_else(|| panic!("{name} was prompted after its last turn"));
            (turn, s.run.clone())
        };
        let saved = self.host.read(&format!("{}/state.json", run.dir())).expect("the fake host reads its files");
        let run = saved.map_or(run, |text| RunState::from_text(&text).expect("the run's state.json reads"));
        // Played with the fake's state released, so that a turn may script the fake itself.
        let status = turn(text, &run, &self.host);
        self.state().statuses.insert(name.to_string(), status);
        Ok(())
    }

    fn wait(&self, name: &str, timeout_ms: Option<u64>, until: &[&str]) -> Result<String, HerdrError> {
        // Played with the fake's state released, so that the test's wait may script the fake itself.
        let wait_with = self.state().wait_with.take();
        if let Some(mut wait) = wait_with {
            let answer = wait(name, timeout_ms, until);
            self.state().wait_with = Some(wait);
            return answer;
        }
        self.own_wait(name, timeout_ms, until)
    }

    fn agent(&self, name: &str) -> Result<Option<Agent>, HerdrError> {
        let s = self.state();
        Ok(s.statuses.get(name).map(|status| Agent { status: status.clone(), session: s.sessions.get(name).cloned() }))
    }

    fn workspace_exists(&self, workspace: &str) -> Result<bool, HerdrError> {
        Ok(self.state().workspaces.contains(workspace))
    }

    fn pane_exists(&self, pane: &str) -> Result<bool, HerdrError> {
        Ok(self.state().live_panes.contains(pane))
    }

    fn focus(&self, name: &str) -> Result<(), HerdrError> {
        self.state().calls.push(Call::Focus { name: name.to_string() });
        Ok(())
    }

    fn close_workspace(&self, workspace: &str) -> Result<(), HerdrError> {
        let mut s = self.state();
        s.calls.push(Call::Close { workspace: workspace.to_string() });
        if let Some(e) = s.close_error.clone() {
            return Err(e);
        }
        s.workspaces.remove(workspace);
        Ok(())
    }
}
