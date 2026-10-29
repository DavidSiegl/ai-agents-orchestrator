//! The phase machine: [`Workflow`] drives one run through the steps of its workflow in a herdr workspace, as
//! orchestrator.py's Workflow does, and is the same code for a new run and a resumed one.
//!
//! How it is given what it talks to (design-it-twice, issue rust-engine/08's Plan): borrowed [`Collaborators`], the
//! real ones from the CLI and the fakes from the tests, which keep the fakes and read them afterwards; the state it
//! owns, read back through [`Workflow::state`]. An environment trait the Workflow is generic over, and a sans-IO
//! machine a driver feeds, were weighed and not taken: the first needs a type per variant of a fake and forwards
//! everything, the second has no Python counterpart to port case for case.

use std::collections::HashSet;
use std::fmt;
use std::rc::Rc;

use crate::constants::{
    APPROVE, CHANGES_REQUESTED, DEFAULT_AGENT, DEFAULT_MAX_QUALITY_ROUNDS, DEFAULT_MAX_ROUNDS, DEFAULT_TURN_TIMEOUT,
    FINISHED, HEARTBEAT_SECONDS, REMOTE, SUCCEEDED,
};
use crate::error::OrchestratorError;
use crate::handoff::parse_verdict;
use crate::harness::{harness, AgentSettings};
use crate::herdr::{Herdr, HerdrError};
use crate::host::{Host, PushError};
use crate::interrupt::Interrupt;
use crate::pipeline::{is_built_in, Pipeline, Step, DONE, METRICS_FILE, PUBLISH, QUALITY, SUMMARY_FILE};
use crate::prompts::{Prompt, Prompts};
use crate::pull_request::{branch_name, commit_note, pr_body, provenance, spec_title, GateEvidence, QualityFile};
use crate::py::format::format;
use crate::py::json::{Json, Map};
use crate::py::text::{basename, rstrip, strip};
use crate::quality::parse_gate;
use crate::runs::{local_host, run_health, RUNNING};
use crate::spec_file::SpecFile;
use crate::state::RunState;
use crate::summary::{iso_utc, run_metrics, run_summary, turn_tokens};
use crate::workflow_file::{run_pipeline, workflow_definition};

mod turn;

#[cfg(test)]
pub(crate) mod fixture;
#[cfg(test)]
mod tests;

/// The time a Workflow keeps: given to it, so that the tests run one on a clock that sleeps at once.
pub trait Clocks {
    /// Wait that many seconds. A fake's time passes only here.
    fn sleep(&self, seconds: f64);
    /// Seconds on a clock that only goes forward, for deadlines and stalls.
    fn monotonic(&self) -> f64;
    /// Seconds since the epoch, for the times state.json records.
    fn wall(&self) -> f64;
}

/// The human watching the run: told what needs them in a notification, and everything else in log lines, as
/// orchestrator.py's notify parameter and its log, which prints each line to stderr.
pub trait Human {
    /// A notification, with its title and body.
    fn notify(&self, title: &str, body: &str);
    /// One log line, without the indent the terminal shows it with.
    fn log(&self, line: &str);
}

/// What a Workflow talks to, each borrowed for as long as it runs.
#[derive(Clone, Copy)]
pub struct Collaborators<'a> {
    /// The machine the agents run on: the project's files and git, gh and gitleaks.
    pub host: &'a dyn Host,
    /// The herdr the agents run in.
    pub herdr: &'a dyn Herdr,
    /// The time the run keeps: its deadlines, pauses and the times state.json records.
    pub clocks: &'a dyn Clocks,
    /// Who is told what needs them, and everything the run does.
    pub human: &'a dyn Human,
    /// The Ctrl-C: once raised, the run stops at its next pause, or at the failure of what was running when it was
    /// raised, as interrupted.
    pub interrupt: &'a Interrupt,
    /// Whether a process of a pid exists on this machine, for judging the checkout's other runs (run_health):
    /// crate::runs::pid_alive, which a test replaces.
    pub pid_alive: &'a dyn Fn(i128) -> bool,
}

/// How a run is to go, as Workflow's keyword arguments in orchestrator.py: each but spec saved in the state, so that a
/// resume starts from them.
#[derive(Clone, Debug, PartialEq)]
pub struct RunSettings {
    /// How many rounds the verdict step's loop takes at most: a verdict other than APPROVE in a round before round
    /// max_rounds sends the run back to the loop's target in the next round, and from round max_rounds on the run
    /// goes on past the verdict step whatever the verdict.
    pub max_rounds: u32,
    /// Seconds a turn of a step that is not human-paced may take.
    pub turn_timeout: u64,
    pub agents: AgentSettings,
    /// Whether the run ends in a pull request: the editing steps work on a branch of their own, which is committed,
    /// pushed and opened as a pull request against the branch the run started on, checked out again afterwards.
    pub pull_request: bool,
    /// Analyses per round, with the quality gate.
    pub max_quality_rounds: u32,
    /// The run's workflow; None for the one the state names.
    pub pipeline: Option<Pipeline>,
    /// The spec given with run --spec, for a new run whose state starts at the step after the contract step
    /// (Pipeline::seeded_start): written as the contract step's file before the run's first state.json, where a
    /// resume, which is given none, finds it. None for every other run.
    pub spec: Option<SpecFile>,
}

impl Default for RunSettings {
    fn default() -> RunSettings {
        RunSettings {
            max_rounds: DEFAULT_MAX_ROUNDS,
            turn_timeout: DEFAULT_TURN_TIMEOUT,
            agents: AgentSettings::default(),
            pull_request: true,
            max_quality_rounds: DEFAULT_MAX_QUALITY_ROUNDS,
            pipeline: None,
            spec: None,
        }
    }
}

/// Why a run stopped before it was done.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Stopped {
    /// It failed; state.json records why, and that no orchestrator drives it now. Python's OrchestratorError.
    Failed(OrchestratorError),
    /// Another orchestrator took the run over, so this one stopped without saving anything more: Python's
    /// RunTakenOver.
    TakenOver(OrchestratorError),
    /// The human's Ctrl-C (Interrupt); state.json records "interrupted". Python's KeyboardInterrupt.
    Interrupted,
}

impl fmt::Display for Stopped {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Stopped::Failed(e) | Stopped::TakenOver(e) => write!(f, "{e}"),
            Stopped::Interrupted => f.write_str("interrupted"),
        }
    }
}

impl From<OrchestratorError> for Stopped {
    fn from(e: OrchestratorError) -> Stopped {
        Stopped::Failed(e)
    }
}

impl From<HerdrError> for Stopped {
    fn from(e: HerdrError) -> Stopped {
        Stopped::Failed(e.into())
    }
}

/// What the Workflow's steps return: their result, or why the run stops.
type Outcome<T> = Result<T, Stopped>;

/// How a role's agent came to be ready for a turn, which decides what it is prompted with.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum How {
    /// The role's first agent in this run.
    New,
    /// Still running from before.
    Alive,
    /// Had exited; relaunched into its saved agent session.
    Resumed,
    /// Had exited; relaunched in a fresh session that has lost its earlier turns.
    Restarted,
}

/// Drives one run through the steps of its workflow in a herdr workspace.
///
/// A role's turn ends when it writes its handoff file, not when herdr reports it settled: an agent can end a turn
/// while a background task it started is still running and resume when the task completes, as Claude Code does, so
/// idle or done can come mid-work.
///
/// The same code starts a new run and resumes an interrupted one. A new run is a resume from the first step with no
/// workspace; every step first looks for what an earlier orchestrator, or a role working while none was watching,
/// already did.
///
/// With pull_request, the editing steps work on a new branch, and the finished change is committed, pushed and
/// opened as a pull request against the branch the run started on, which is checked out again afterwards. The
/// workspace is then closed. A run that fails keeps its workspace and branch, to see what happened.
pub struct Workflow<'a> {
    host: &'a dyn Host,
    herdr: &'a dyn Herdr,
    clocks: &'a dyn Clocks,
    human: &'a dyn Human,
    interrupt: &'a Interrupt,
    pid_alive: &'a dyn Fn(i128) -> bool,
    state: RunState,
    /// The run's workflow, shared so that a step borrowed from it outlives no borrow of the Workflow.
    pipeline: Rc<Pipeline>,
    /// Every prompt the workflow sends that its definition does not word: the follow-ups and notes.
    prompts: Prompts,
    /// RunSettings::spec.
    spec: Option<SpecFile>,
    /// This orchestrator as state.json's owner records it, {"host", "pid", "started_at"}: the run is this one's
    /// while the saved owner equals it.
    me: Map<Json>,
    /// When state.json was last written by this orchestrator, or a failed heartbeat last tried to, on the monotonic
    /// clock; 0 before.
    last_write: f64,
    /// The roles whose harness has no equivalent of the permission mode and that the log has said so for.
    told_dropped: HashSet<String>,
}

impl<'a> Workflow<'a> {
    /// A Workflow for the run state describes, with settings saved into it; settings.pipeline, else the workflow the
    /// state names (and its saved definition), is the run's.
    ///
    /// Errors: the state's prompt overrides or saved workflow cannot be read, or the run is to end in a pull request
    /// and its workflow has no editing step.
    pub fn new(with: Collaborators<'a>, mut state: RunState, settings: RunSettings) -> Result<Workflow<'a>, OrchestratorError> {
        let prompts = state.prompts()?;
        let pipeline = match settings.pipeline {
            Some(p) => p,
            None => run_pipeline(&state.workflow, state.workflow_definition.as_ref(), &prompts)?,
        };
        // Publishing commits on the run's branch, which only the first editing step creates.
        if settings.pull_request && pipeline.first_edit().is_none() {
            return Err(OrchestratorError(format!(
                "workflow {} has no editing step, so it cannot end in a pull request; pass --no-pr",
                pipeline.name()
            )));
        }
        state.workflow = pipeline.name().to_string();
        state.workflow_definition =
            if is_built_in(&pipeline, &prompts) { None } else { Some(workflow_definition(&pipeline)) };
        state.max_rounds = settings.max_rounds;
        state.turn_timeout = settings.turn_timeout;
        let agents = settings.agents;
        state.permission_mode = agents.permission_mode;
        state.models = agents.models;
        state.agent_kinds = pipeline
            .roles()
            .keys()
            .map(|role| (role, agents.kinds.get(role).cloned().unwrap_or_else(|| DEFAULT_AGENT.to_string())))
            .collect();
        state.pull_request = settings.pull_request;
        state.quality_job = None;
        state.max_quality_rounds = settings.max_quality_rounds;
        let me = [
            ("host", Json::from(local_host())),
            ("pid", Json::from(i64::from(std::process::id()))),
            ("started_at", Json::from(iso_utc(with.clocks.wall().floor() as i64))),
        ]
        .into_iter()
        .collect();
        Ok(Workflow {
            host: with.host,
            herdr: with.herdr,
            clocks: with.clocks,
            human: with.human,
            interrupt: with.interrupt,
            pid_alive: with.pid_alive,
            state,
            pipeline: Rc::new(pipeline),
            prompts,
            spec: settings.spec,
            me,
            last_write: 0.0,
            told_dropped: HashSet::new(),
        })
    }

    /// The run's state as it stands.
    pub fn state(&self) -> &RunState {
        &self.state
    }

    /// Run every phase not yet done and return the final verdict: the verdict step's, or for a workflow without one,
    /// FINISHED. A run already done returns its saved verdict.
    ///
    /// Errors: why the run stopped (Stopped); for a failure or an interrupt, after recording it in state.json and
    /// writing summary.md, as far as saving still works.
    pub fn run(&mut self) -> Result<Option<String>, Stopped> {
        if self.state.phase == DONE {
            return Ok(self.state.verdict.clone());
        }
        self.check_phase()?;
        match self.walk_to_the_end() {
            Ok(()) => {}
            Err(Stopped::TakenOver(e)) => return Err(Stopped::TakenOver(e)),
            // A failure that met the Ctrl-C is the command the interrupt stopped.
            Err(Stopped::Failed(e)) if !self.interrupt.is_raised() => {
                self.release(&e.0);
                return Err(Stopped::Failed(e));
            }
            Err(_) => {
                self.release("interrupted");
                return Err(Stopped::Interrupted);
            }
        }
        self.state.phase = DONE.to_string();
        // Saved first: the run is done whatever counting the tokens meets.
        self.save()?;
        self.count_tokens();
        self.state.owner = None;
        self.save()?;
        self.write_summary();
        self.write_metrics();
        let s = &self.state;
        if let (Some(worktree), false) = (&s.worktree, s.pull_request) {
            self.human.log(&format!("the change is in worktree {worktree}"));
        }
        let verdict = s.verdict.clone().unwrap_or_else(|| "None".to_string());
        let body = s.pr_url.as_deref().filter(|url| !url.is_empty()).unwrap_or(&s.task);
        self.human.notify(&format!("Run finished: {verdict}"), body);
        if self.state.pull_request {
            self.close_workspace();
        }
        Ok(self.state.verdict.clone())
    }

    /// Everything of run between checking the phase and the end: what a failure in it releases the run for.
    fn walk_to_the_end(&mut self) -> Outcome<()> {
        self.claim()?;
        self.check_start()?;
        // Publishing needs no agent, so a resume there opens no workspace only to close it.
        if self.state.phase != PUBLISH {
            self.prepare()?;
        }
        self.walk()?;
        if self.pipeline.verdict_step().is_none() {
            self.state.verdict = Some(FINISHED.to_string());
        }
        if self.state.pull_request {
            self.publish()?;
        }
        Ok(())
    }

    fn check_phase(&self) -> Result<(), OrchestratorError> {
        let (s, p) = (&self.state, &self.pipeline);
        let known = p.steps().iter().any(|st| st.id == s.phase)
            || s.phase == PUBLISH
            || (s.phase == QUALITY && p.gated().is_some());
        if known {
            return Ok(());
        }
        Err(OrchestratorError(format!(
            "run {} is in phase {}, which workflow {} has no step for",
            s.run_id,
            s.phase,
            p.name()
        )))
    }

    /// Fail before the next turn if the run could not finish.
    fn check_start(&mut self) -> Outcome<()> {
        self.check_alone()?;
        // Round 0: the run has not reached its first editing step, so it has no base or branch yet.
        if self.state.round == 0 {
            return self.check_before_interview();
        }
        if self.state.worktree.is_some() {
            self.check_worktree()?;
        }
        Ok(self.check_branch()?)
    }

    /// Open the run's worktree, if it has one, and fail if the run could not push what it makes or open its pull
    /// request, before the Spec Collector's first turn.
    fn check_before_interview(&mut self) -> Outcome<()> {
        if self.state.worktree.is_some() {
            self.open_worktree()?;
        }
        if self.state.pull_request {
            self.host.check_gitleaks()?;
            if self.state.worktree.is_none() {
                self.check_repo()?;
            }
        }
        Ok(())
    }

    /// Before the interview, check out origin's base branch, detached, in the run's worktree.
    ///
    /// The base branch is the one checked out in the project. The checkout itself is left as it is, so it need not be
    /// clean or up to date, and the human can go on working in it.
    fn open_worktree(&mut self) -> Outcome<()> {
        let (cwd, worktree) = (self.state.cwd.clone(), self.state.worktree.clone().unwrap_or_default());
        if self.host.git_head(&cwd)?.is_none() {
            return Err(Stopped::Failed(OrchestratorError(format!(
                "{cwd} is not a git repository with a commit, which --worktree needs"
            ))));
        }
        let branch = strip(&self.host.git(&cwd, &["rev-parse", "--abbrev-ref", "HEAD"])?).to_string();
        if branch == "HEAD" {
            return Err(Stopped::Failed(OrchestratorError(format!(
                "{cwd} is on a detached HEAD; --worktree bases the run on the checked-out branch, so check one out"
            ))));
        }
        if !self.host.has_origin(&cwd)? {
            return Err(Stopped::Failed(OrchestratorError(format!(
                "--worktree starts the run from {REMOTE}'s branch, and {cwd} has no {REMOTE}"
            ))));
        }
        // Kept once recorded, as check_repo keeps it.
        if self.state.base_branch.as_deref().is_none_or(str::is_empty) {
            self.state.base_branch = Some(branch);
        }
        let run_id = self.state.run_id.clone();
        // A resume before the first editing step: the run's worktree holds nothing it needs to keep.
        if self.host.is_worktree(&cwd, &worktree)? {
            self.human.log(&format!("run {run_id} resumes in worktree {worktree}"));
            return Ok(());
        }
        let base_branch = self.base_branch()?;
        let upstream = self.host.fetch(&cwd, &base_branch)?;
        self.host.add_worktree(&cwd, &worktree, &upstream)?;
        self.human.log(&format!("run {run_id} in worktree {worktree}, at {REMOTE}/{base_branch}"));
        self.save()
    }

    /// Fail if the run's worktree, which holds its change, is gone; unless the pull request has the change.
    fn check_worktree(&self) -> Result<(), OrchestratorError> {
        let s = &self.state;
        let worktree = s.worktree.as_deref().unwrap_or_default();
        if !s.pr_url.as_deref().unwrap_or_default().is_empty() || self.host.is_worktree(&s.cwd, worktree)? {
            return Ok(());
        }
        Err(OrchestratorError(format!(
            "the worktree of run {}, {worktree}, is gone or no longer a git worktree, and with it the run's change; \
             the run cannot go on",
            s.run_id
        )))
    }

    /// Fail if another run in this checkout is live: the two would share one working tree.
    ///
    /// A worktree run shares no working tree, neither with another worktree run nor with the run in the checkout.
    /// After claim, so of two runs that start together each sees the other's state.json and both stop, rather than
    /// both going on.
    fn check_alone(&self) -> Result<(), OrchestratorError> {
        let s = &self.state;
        if s.worktree.is_some() {
            return Ok(());
        }
        let me = self.me.get("host").and_then(Json::as_str).unwrap_or_default();
        for (age, other) in self.host.run_states(&s.cwd)? {
            if other.get("run_id").and_then(Json::as_str) == Some(&s.run_id)
                || other.get("worktree").is_some_and(Json::truthy)
            {
                continue;
            }
            if let Some((RUNNING, why)) = run_health(&other, age, me, self.pid_alive) {
                let id = other.get("run_id").map_or("None".to_string(), Json::python_str);
                return Err(OrchestratorError(format!(
                    "run {id} is running in {} ({why}); one run at a time per checkout: wait for it, stop it, or \
                     use another clone",
                    s.cwd
                )));
            }
        }
        Ok(())
    }

    pub(crate) fn claim(&mut self) -> Outcome<()> {
        let cwd = self.state.cwd.clone();
        self.ignore_orchestrator_dir(&cwd)?;
        if self.spec.is_some() {
            self.seed_spec()?;
        }
        self.state.error = None;
        self.state.owner = Some(self.me.clone());
        // Not save: the caller has already decided that any earlier owner is gone.
        Ok(self.write_state()?)
    }

    /// Make git ignore everything under the .orchestrator directory of the working tree at tree, by a .gitignore of
    /// `*` there, unless it has a .gitignore already. Keeps run files out of `git status`, so the Reviewer sees only
    /// the Builder's changes and the commit holds nothing else.
    fn ignore_orchestrator_dir(&self, tree: &str) -> Result<(), OrchestratorError> {
        let ignore = format!("{tree}/.orchestrator/.gitignore");
        if self.host.read(&ignore)?.is_none() {
            self.host.write(&ignore, "*\n", false)?;
        }
        Ok(())
    }

    /// Write the spec given with run --spec (RunSettings::spec) as the contract step's file.
    ///
    /// After .gitignore, which keeps it out of the clean-tree check, and before the first state.json, which starts
    /// the run after the contract step: a resume of that run must find the file.
    fn seed_spec(&self) -> Result<(), OrchestratorError> {
        let (Some(spec), Some(contract)) = (&self.spec, self.pipeline.contract()) else {
            return Err(OrchestratorError(format!("workflow {} has no step to seed a spec in", self.pipeline.name())));
        };
        let path = contract.path(&self.state.dir(), 0, 0);
        self.host.write(&path, &spec.text, false)?;
        let skipped = if contract.human_paced { "interview" } else { "turn" };
        self.human.log(&format!(
            "[{}] {} is a copy of {}; skipping the {}'s {skipped}",
            contract.role,
            basename(&path),
            spec.path,
            self.label(&contract.role)
        ));
        Ok(())
    }

    /// Record why the run stopped and that no process drives it now, as far as saving still works.
    fn release(&mut self, error: &str) {
        self.state.error = Some(error.to_string());
        self.state.owner = None;
        if let Err(e) = self.save() {
            self.human.log(&format!("could not record the failure in state.json: {e}"));
            if let Stopped::TakenOver(_) = e {
                // The run is another orchestrator's now, and so is its summary.
                return;
            }
        }
        self.write_summary();
    }

    /// Write summary.md for a run that just ended; a failure costs only the summary, so it is only logged.
    fn write_summary(&self) {
        let written = self.round_results().and_then(|rounds| {
            let s = &self.state;
            let summary = run_summary(s, &self.pipeline, &rounds, self.host.now()?);
            self.host.write(&format!("{}/{SUMMARY_FILE}", s.dir()), &summary, false)
        });
        if let Err(e) = written {
            self.human.log(&format!("could not write {SUMMARY_FILE}: {e}"));
        }
    }

    /// Write metrics.json for a run that is done; as for the summary, a failure is only logged.
    fn write_metrics(&self) {
        let s = &self.state;
        let written = self.host.now().and_then(|now| {
            self.host.write(&format!("{}/{METRICS_FILE}", s.dir()), &(run_metrics(s, now).dumps() + "\n"), false)
        });
        if let Err(e) = written {
            self.human.log(&format!("could not write {METRICS_FILE}: {e}"));
        }
    }

    /// Give each agent turn the tokens its harness recorded for it (turn_tokens), "unknown" where it recorded none
    /// the orchestrator can read. A session log that cannot be read costs only its turns' counts.
    fn count_tokens(&mut self) {
        let mut usage = std::collections::HashMap::new();
        for i in 0..self.state.turns.len() {
            let t = &self.state.turns[i];
            let Some(agent) = t.get("agent").filter(|a| a.truthy()).map(Json::python_str) else { continue };
            let session = t.get("session").filter(|s| s.truthy()).map(Json::python_str).unwrap_or_default();
            let key = (agent, session);
            if !usage.contains_key(&key) {
                let read = || -> Result<_, OrchestratorError> {
                    if key.1.is_empty() {
                        return Ok(None);
                    }
                    let Some(log) = self.host.session_log(&key.0, &key.1)? else { return Ok(None) };
                    Ok(Some(harness(&key.0)?.session_usage(&log)))
                };
                let counted = read().unwrap_or_else(|e| {
                    self.human.log(&format!("could not read the {} session log of {}: {e}", key.0, key.1));
                    None
                });
                usage.insert(key.clone(), counted);
            }
            let tokens = turn_tokens(t, usage[&key].as_deref());
            self.state.turns[i].insert("tokens", tokens);
        }
    }

    /// Per round, its quality gates and its verdict, as its handoff files say.
    fn round_results(&self) -> Result<Vec<String>, OrchestratorError> {
        let s = &self.state;
        let verdict_step = self.pipeline.verdict_step();
        let mut lines = vec![];
        for n in 1..=s.round {
            let mut parts = vec![];
            let mut gates = vec![];
            while s.had_quality_gate() {
                let Some(text) = self.host.read(&s.quality_path(n, gates.len() as u32 + 1))? else { break };
                gates.push(parse_gate(&text).unwrap_or("?"));
            }
            if !gates.is_empty() {
                parts.push(format!("quality gate {}", gates.join(", ")));
            }
            if let Some(step) = verdict_step {
                if let Some(text) = self.host.read(&step.path(&s.dir(), n, 0))? {
                    parts.push(format!("{} {}", step.id, parse_verdict(&text).unwrap_or("?")));
                }
            }
            let parts = if parts.is_empty() { "no verdict".to_string() } else { parts.join("; ") };
            lines.push(format!("Round {n}: {parts}"));
        }
        Ok(lines)
    }

    /// Fail before the interview if the change could not become a pull request.
    fn check_repo(&mut self) -> Result<(), OrchestratorError> {
        let cwd = self.state.cwd.clone();
        if self.host.git_head(&cwd)?.is_none() {
            return Err(OrchestratorError(format!("{cwd} is not a git repository with a commit; pass --no-pr")));
        }
        let branch = strip(&self.host.git(&cwd, &["rev-parse", "--abbrev-ref", "HEAD"])?).to_string();
        if branch == "HEAD" {
            return Err(OrchestratorError(format!(
                "{cwd} is on a detached HEAD; check out the branch the pull request targets"
            )));
        }
        // Kept once recorded: a run resumed after it switched to its own branch still targets the first one.
        if self.state.base_branch.as_deref().is_none_or(str::is_empty) {
            self.state.base_branch = Some(branch.clone());
        }
        self.require_clean()?;
        // So the Spec Collector reads current code. A resume already on the run's own branch leaves it to
        // switch_to_branch.
        let base_branch = self.base_branch()?;
        if branch == base_branch {
            self.host.fast_forward(&cwd, &base_branch)?;
        }
        Ok(())
    }

    /// Fail if HEAD is not the run's branch, which holds the Builder's uncommitted change until publishing.
    ///
    /// Otherwise the commit lands on whatever the human checked out, and the run's branch is pushed without it.
    fn check_branch(&self) -> Result<(), OrchestratorError> {
        let s = &self.state;
        let branch = match &s.branch {
            Some(b) if s.pull_request && !b.is_empty() && s.pr_url.as_deref().is_none_or(str::is_empty) => b,
            _ => return Ok(()),
        };
        let current = strip(&self.host.git(s.work_dir(), &["rev-parse", "--abbrev-ref", "HEAD"])?).to_string();
        // A detached HEAD reads as "HEAD", so it never matches.
        if &current != branch {
            return Err(OrchestratorError(format!(
                "{} is on {current}, not {branch}, which holds this run's change; check out {branch} and resume",
                s.work_dir()
            )));
        }
        Ok(())
    }

    /// Fail if the working tree has uncommitted changes: the commit takes every change in it, so it must hold only
    /// the Builder's.
    fn require_clean(&self) -> Result<(), OrchestratorError> {
        let work_dir = self.state.work_dir();
        let dirty = self.host.git(work_dir, &["status", "--porcelain"])?;
        if !strip(&dirty).is_empty() {
            return Err(OrchestratorError(format!(
                "{work_dir} has uncommitted changes; commit or stash them first:\n{}",
                rstrip(&dirty)
            )));
        }
        Ok(())
    }

    /// The branch the pull request targets, recorded before the interview. Errors: none is recorded.
    fn base_branch(&self) -> Result<String, OrchestratorError> {
        self.state.base_branch.clone().ok_or_else(|| OrchestratorError("the run has no base branch yet".into()))
    }

    /// Open the run's herdr workspace, or find it from before.
    fn prepare(&mut self) -> Outcome<()> {
        let s = &mut self.state;
        if !s.workspace_id.is_empty() {
            if self.herdr.workspace_exists(&s.workspace_id)? {
                self.human.log(&format!("run {} resumes in herdr workspace {}", s.run_id, s.workspace_id));
                return Ok(());
            }
            // The handoff files are the whole contract between roles, so a new workspace loses nothing a role
            // needs; its panes went with the old one.
            self.human.log(&format!("herdr workspace {} is gone; opening a new one", s.workspace_id));
            for agent in s.agents.values_mut() {
                agent.insert("pane", String::new());
            }
        }
        let label: String = format!("{} {}", s.key(), s.task).chars().take(40).collect();
        let (workspace, root) = self.herdr.create_workspace(s.work_dir(), &label)?;
        (s.workspace_id, s.root_pane) = (workspace, root);
        self.human.log(&format!("run {} in herdr workspace {}", s.run_id, s.workspace_id));
        self.save()
    }

    /// The step of the current phase. Errors: the workflow has none, which check_phase rules out.
    fn current_step(&self) -> Result<Step, OrchestratorError> {
        let phase = &self.state.phase;
        self.pipeline.step(phase).cloned().ok_or_else(|| {
            OrchestratorError(format!("workflow {} has no step {phase}", self.pipeline.name()))
        })
    }

    /// Take the turn of the current step and of each one after it, looping back as the verdict says.
    fn walk(&mut self) -> Outcome<()> {
        if self.state.phase == PUBLISH {
            return Ok(());
        }
        if self.state.round == 0 {
            let step = self.current_step()?;
            if step.edits {
                // A run whose first step edits.
                self.goto(&step, 0)?;
            }
        }
        let pipeline = Rc::clone(&self.pipeline);
        loop {
            let n = self.state.round;
            let step = self.current_step()?;
            if !self.take_turn(&step, n)? {
                continue;
            }
            match pipeline.after(&step.id) {
                None => return Ok(()),
                Some(following) => self.goto(following, n)?,
            }
        }
    }

    /// The step's turn in round n; false when the run goes on elsewhere than the next step.
    fn take_turn(&mut self, step: &Step, n: u32) -> Outcome<bool> {
        let out = self.step_turn(step, n)?;
        if step.quality_gated && self.state.quality_round > 0 {
            // The report goes to the next quality round. A round whose gate resume turned off since its last quality
            // round never analyses it, and the next step reads the report all the same (Step::last_path).
            self.state.quality_round += 1;
        }
        let Some(loop_to) = &step.loop_to else { return Ok(true) };
        // turn accepts no verdict file without one.
        let verdict = parse_verdict(&out).unwrap_or_default();
        self.state.verdict = Some(verdict.to_string());
        self.human.log(&format!("[{}] round {n}: {verdict}", step.role));
        if verdict != APPROVE && n < self.state.max_rounds {
            self.state.quality_round = 0;
            let pipeline = Rc::clone(&self.pipeline);
            let target = pipeline.step(loop_to).ok_or_else(|| OrchestratorError(format!("no step {loop_to}")))?;
            self.goto(target, n + 1)?;
            return Ok(false);
        }
        Ok(true)
    }

    /// Make step the current one, in round n, and save that; the first editing step starts round 1.
    fn goto(&mut self, step: &Step, mut n: u32) -> Outcome<()> {
        if n == 0 && step.edits {
            if self.state.pull_request {
                self.switch_to_branch()?;
            }
            // Only here, before the first editing step and after the base branch was fast-forwarded: every later
            // step, resumed or not, diffs against this base, even if the human commits the Builder's work meanwhile.
            self.state.base = self.host.git_head(self.state.work_dir())?;
            n = 1;
        }
        self.state.phase = step.id.clone();
        self.state.round = n;
        self.save()
    }

    /// The step's turn in round n: its handoff file, once written.
    fn step_turn(&mut self, step: &Step, n: u32) -> Outcome<String> {
        let (text, fresh_text) = self.prompts(step, n)?;
        let turn = turn::Turn {
            role: step.role.clone(),
            path: self.own_path(step, n),
            text,
            fresh_text,
            // Paced by the human, so the turn has no deadline, and an idle agent is normal: it is waiting for the
            // human's reply.
            timeout: (!step.human_paced).then_some(self.state.turn_timeout),
            watch_stalls: !step.human_paced,
            announce: if step.human_paced { turn::Announce::Interview } else { turn::Announce::Nothing },
            verdict: step.loop_to.is_some(),
            blocks: step.can_block(),
        };
        self.turn(turn)
    }

    /// The file the step's turn in round n writes: for the gated step, its answer to the quality round if any.
    fn own_path(&self, step: &Step, n: u32) -> String {
        let s = &self.state;
        step.path(&s.dir(), n, if step.quality_gated { s.quality_round } else { 0 })
    }

    /// The step's prompt for its turn in round n, and the one for a fresh session that has not seen the turns before
    /// it; each ends with BLOCKED_NOTE when the step can block.
    fn prompts(&self, step: &Step, n: u32) -> Result<(String, Option<String>), OrchestratorError> {
        let (text, fresh) = self.step_prompts(step, n)?;
        if !step.can_block() {
            return Ok((text, fresh));
        }
        // Formatted with nothing, as each prompt is a template (Prompts): an override may write a brace as {{.
        let note = self.fill(Prompt::BlockedNote, &[])?;
        Ok((format!("{text}\n\n{note}"), fresh.map(|f| if f.is_empty() { f } else { format!("{f}\n\n{note}") })))
    }

    /// prompts as the step's definition words them.
    fn step_prompts(&self, step: &Step, n: u32) -> Result<(String, Option<String>), OrchestratorError> {
        let s = &self.state;
        let q = if step.quality_gated { s.quality_round } else { 0 };
        let mut fill = Map::new();
        for name in step.placeholders() {
            let value = self.placeholder(&name, step, n)?;
            fill.insert(name, value);
        }
        let template = |t: &str| format(t, &|name| fill.get(name).cloned()).map_err(|e| prompt_error(&step.id, &e));
        let first = template(&step.prompt)?;
        let follow = if q > 0 {
            let report_path = self.own_path(step, n);
            let quality_path = s.quality_path(n, q);
            self.fill(Prompt::QualityFixPrompt, &[("quality_path", &quality_path), ("report_path", &report_path)])?
        } else if n > 1 {
            template(step.again.as_deref().filter(|a| !a.is_empty()).unwrap_or(&step.prompt))?
        } else {
            return Ok((first, None));
        };
        let mut fresh = vec![first];
        if let Some(note) = step.fresh_note.as_deref().filter(|f| !f.is_empty()) {
            fresh.push(template(note)?);
        }
        // A quality fix always asks for work that the first prompt does not.
        if q > 0 || step.fresh_repeats_again {
            fresh.push(follow.clone());
        }
        Ok((follow, Some(fresh.join("\n\n"))))
    }

    /// The value of a prompt placeholder (see Step) in the step's turn in round n.
    fn placeholder(&self, name: &str, step: &Step, n: u32) -> Result<String, OrchestratorError> {
        let s = &self.state;
        match name {
            "task" => return Ok(s.task.clone()),
            "cwd" => return Ok(s.work_dir().to_string()),
            "n" => return Ok(n.to_string()),
            "approve" => return Ok(APPROVE.to_string()),
            "changes" => return Ok(CHANGES_REQUESTED.to_string()),
            "proofs_dir" => return Ok(s.proofs_dir(n)),
            "change" => return Ok(self.change_description()),
            "path" => return Ok(self.own_path(step, n)),
            _ => {}
        }
        let placeholders = self.pipeline.path_placeholders();
        let (_, prefix, other) = placeholders
            .iter()
            .find(|(x, _, _)| x == name)
            .ok_or_else(|| OrchestratorError(format!("step {} has no placeholder {name}", step.id)))?;
        match *prefix {
            // Round 0's previous round is -1, as Python's n - 1 gives it, whose file no turn writes.
            "prev_" if n == 0 => Ok(other.path(&s.dir(), -1, 0)),
            "prev_" => Ok(self.reports(other, n - 1)?.pop().unwrap_or_default()),
            "earlier_" => {
                let mut reports = vec![];
                for i in 1..n {
                    reports.extend(self.reports(other, i)?);
                }
                if other.id == step.id && step.quality_gated {
                    reports.extend((0..s.quality_round).map(|j| step.path(&s.dir(), n, j)));
                }
                Ok(reports.join(", "))
            }
            _ if other.id == step.id => Ok(self.own_path(step, n)),
            _ => Ok(other.last_path(&s.dir(), n, s.quality_round)),
        }
    }

    /// The step's files of round n, oldest first; for the gated step, its answers to quality rounds too.
    fn reports(&self, step: &Step, n: u32) -> Result<Vec<String>, OrchestratorError> {
        let s = &self.state;
        let mut reports = vec![step.path(&s.dir(), n, 0)];
        // How many quality rounds an earlier round took is recorded only by the files it left.
        while s.had_quality_gate() && step.quality_gated {
            let next = step.path(&s.dir(), n, reports.len() as u32);
            if self.host.read(&next)?.is_none() {
                break;
            }
            reports.push(next);
        }
        Ok(reports)
    }

    /// One of the prompts the workflow's definition does not word, filled in with values.
    fn fill(&self, p: Prompt, values: &[(&str, &str)]) -> Result<String, OrchestratorError> {
        let value = |name: &str| values.iter().find(|(n, _)| *n == name).map(|(_, v)| v.to_string());
        format(self.prompts.text(p), &value).map_err(|e| prompt_error(p.name(), &e))
    }

    /// Before the first editing step: create the run's branch from the base branch, brought up to origin's, unless
    /// the checkout is on it already.
    fn switch_to_branch(&mut self) -> Outcome<()> {
        let pipeline = Rc::clone(&self.pipeline);
        let spec = self.last_text(pipeline.contract(), 0)?;
        let title = spec_title(&spec).filter(|t| !t.is_empty()).unwrap_or_else(|| self.state.task.clone());
        let branch = branch_name(&title, &self.state.run_id);
        self.state.branch = Some(branch.clone());
        let (cwd, work_dir) = (self.state.cwd.clone(), self.state.work_dir().to_string());
        // Already there when an earlier orchestrator stopped between the switch and saving the next phase.
        let current = strip(&self.host.git(&work_dir, &["rev-parse", "--abbrev-ref", "HEAD"])?).to_string();
        if current != branch {
            // Checked again: the human may have touched the tree during the interview.
            self.require_clean()?;
            // Again, because the interview can take hours: the branch starts from origin's latest base.
            let base_branch = self.base_branch()?;
            if self.state.worktree.is_some() {
                // From origin's base itself: the worktree's detached HEAD is where it was before the interview, and
                // the project's own base branch is the human's.
                let upstream = self.host.fetch(&work_dir, &base_branch)?;
                self.host.git(&work_dir, &["switch", "-c", &branch, &upstream])?;
            } else {
                if current != base_branch {
                    return Err(Stopped::Failed(OrchestratorError(format!(
                        "{cwd} is on {current}, not {base_branch}, which the pull request targets; check out \
                         {base_branch} and resume"
                    ))));
                }
                self.host.fast_forward(&cwd, &base_branch)?;
                self.host.git(&cwd, &["switch", "-c", &branch])?;
            }
        }
        self.human.log(&format!("building on branch {branch}"));
        Ok(())
    }

    /// Commit the change, merge the latest base into it, push its branch and open a pull request for it.
    ///
    /// Each step first checks whether an earlier orchestrator already took it, so a resume in this phase finishes the
    /// job instead of committing, merging or opening a second pull request.
    fn publish(&mut self) -> Outcome<()> {
        let pipeline = Rc::clone(&self.pipeline);
        self.state.phase = PUBLISH.to_string();
        self.save()?;
        if self.state.pr_url.as_deref().is_none_or(str::is_empty) {
            let wd = self.state.work_dir().to_string();
            // Again: the human may have switched branches during a turn.
            self.check_branch()?;
            // An earlier orchestrator stopped mid-merge: committing now would commit the conflict markers.
            if self.host.abort_merge(&wd)? {
                self.human.log(&format!("aborted the merge an earlier orchestrator left in {wd}"));
            }
            let spec = self.last_text(pipeline.contract(), 0)?;
            let s = &self.state;
            let first_line = strip(&s.task).split('\n').next().unwrap_or_default().to_string();
            let title = spec_title(&spec).filter(|t| !t.is_empty()).unwrap_or(first_line);
            let title: String = if title.is_empty() { s.run_id.clone() } else { title }.chars().take(72).collect();
            let quality = self.gate_evidence(s.round)?;
            if !strip(&self.host.git(&wd, &["status", "--porcelain"])?).is_empty() {
                self.host.git(&wd, &["add", "--all"])?;
                let trailers: Vec<String> = provenance(s, &quality)
                    .into_iter()
                    .flat_map(|(key, said)| ["--trailer".to_string(), format!("{key}: {said}")])
                    .collect();
                let note = commit_note(s);
                let mut args = vec!["commit", "--quiet", "-m", &title, "-m", &note];
                args.extend(trailers.iter().map(String::as_str));
                self.host.git(&wd, &args)?;
            } else if self.host.git_head(&wd)? == s.base {
                let branch = s.branch.as_deref().unwrap_or("None");
                return Err(Stopped::Failed(OrchestratorError(format!(
                    "the Builder changed no files; there is nothing to commit on {branch}"
                ))));
            }
            let leaks = self.state.leaks_path("publish");
            // Scanned before the merge too, so that rewriting the run's commits never has to undo a merge.
            self.rewrite_on_leak(|host, base, branch| {
                host.refuse_leaks(&wd, &format!("refs/heads/{branch}"), base, &leaks)
            })?;
            // The base moved on while the run built and reviewed. A conflict is left to the human: the branch is
            // pushed without the merge and the pull request says what conflicts.
            let base_branch = self.base_branch()?;
            self.state.conflicts = self.host.merge_upstream(&wd, &base_branch)?;
            self.save()?;
            let s = &self.state;
            if !s.conflicts.is_empty() {
                self.human.log(&format!(
                    "{} conflicts with {REMOTE}/{base_branch} in {}; the pull request will be a draft",
                    s.branch.as_deref().unwrap_or("None"),
                    s.conflicts.join(", ")
                ));
            }
            self.rewrite_on_leak(|host, base, branch| host.push_branch(&wd, branch, base, &leaks))?;
            let report = self.last_text(pipeline.last_edit(), self.state.round)?;
            let verdict = self.last_text(pipeline.verdict_step(), self.state.round)?;
            let s = &self.state;
            let body = pr_body(s, &spec, &report, &verdict, &quality);
            let draft = !SUCCEEDED.contains(&s.verdict.as_deref().unwrap_or_default()) || !s.conflicts.is_empty();
            let head = s.branch.clone().unwrap_or_default();
            let url = self.host.create_pr(&wd, &base_branch, &head, &title, &body, draft)?;
            self.state.pr_url = Some(url.clone());
            self.save()?;
            self.human.log(&format!("opened {url}"));
            if !self.state.conflicts.is_empty() {
                self.human.notify(&format!("Pull request conflicts with {base_branch}"), &url);
            }
        }
        Ok(self.leave_work_dir()?)
    }

    /// push(host, base, branch), a scan of the run's branch, or its push, at publish. A secret found becomes the
    /// error that ends the run, still in phase publish: the commit holding the secret is already on the local branch,
    /// so fixing the working tree is not enough, and the error says how to rewrite it before a resume.
    fn rewrite_on_leak(
        &self,
        push: impl FnOnce(&dyn Host, &str, &str) -> Result<(), PushError>,
    ) -> Result<(), OrchestratorError> {
        let s = &self.state;
        let (Some(base), Some(branch)) = (&s.base, &s.branch) else {
            return Err(OrchestratorError("a publish before the run has a base and a branch".into()));
        };
        match push(self.host, base, branch) {
            Ok(()) => Ok(()),
            Err(PushError::Failed(e)) => Err(e),
            Err(PushError::Leaks(e)) => Err(OrchestratorError(format!(
                "{e}\nThose commits are on {branch} in {} and not on {REMOTE}. Rewrite them without the secret, for \
                 example with `git reset --soft {base}` there, remove it from the files, and resume the run: it stays \
                 in phase {PUBLISH} and commits again.",
                s.work_dir()
            ))),
        }
    }

    /// Once the pull request has the change: check the base branch out again, or remove the run's worktree.
    ///
    /// The base branch leaves the checkout where the human had it, so the next run branches from there too. The
    /// worktree goes without --force, which git refuses for one with untracked or modified files. publish has
    /// committed them all, so a refusal means someone changed the worktree since; that, like any failure in removing
    /// it, is only logged. The run's branch stays either way.
    fn leave_work_dir(&self) -> Result<(), OrchestratorError> {
        let s = &self.state;
        let Some(worktree) = &s.worktree else {
            let base_branch = self.base_branch()?;
            return self.host.git(&s.cwd, &["switch", "--quiet", &base_branch]).map(drop);
        };
        let removed = self.host.is_worktree(&s.cwd, worktree).and_then(|is| {
            if is {
                self.host.remove_worktree(&s.cwd, worktree, false)?;
                self.human.log(&format!("removed worktree {worktree}"));
            }
            Ok(())
        });
        if let Err(e) = removed {
            self.human.log(&format!("could not remove worktree {worktree}: {e}"));
        }
        Ok(())
    }

    /// The step's last file of round n, or "" for no step or no file.
    fn last_text(&self, step: Option<&Step>, n: u32) -> Result<String, OrchestratorError> {
        let Some(step) = step else { return Ok(String::new()) };
        let s = &self.state;
        Ok(self.host.read(&step.last_path(&s.dir(), n, s.quality_round))?.unwrap_or_default())
    }

    /// Close the run's workspace once the pull request carries the change; a failure leaves only clutter behind, so
    /// it is only logged.
    fn close_workspace(&self) {
        let workspace = &self.state.workspace_id;
        if let Err(e) = self.herdr.close_workspace(workspace) {
            self.human.log(&format!("could not close herdr workspace {workspace}: {e}"));
        }
    }

    /// Round n's last quality file that is written, as (its quality round, its text); None when there is none.
    ///
    /// It is the current quality round's, except when resume --no-quality-gate turned the gate off before that
    /// round's analysis finished, or in the gated step's turn after it (take_turn): the one before then.
    fn last_quality(&self, n: u32) -> Result<Option<(u32, String)>, OrchestratorError> {
        let s = &self.state;
        for q in [s.quality_round, s.quality_round.saturating_sub(1)] {
            if q > 0 {
                if let Some(text) = self.host.read(&s.quality_path(n, q))? {
                    return Ok(Some((q, text)));
                }
            }
        }
        Ok(None)
    }

    /// Whether the current round's change went unjudged: resume --no-quality-gate turned the gate off, and last, the
    /// round's last quality file as last_quality gives it, is older than the Builder's latest report in the round, or
    /// is None: the gate was turned off before the round's first analysis finished, or in an earlier round.
    fn unjudged(&self, last: Option<&(u32, String)>) -> bool {
        let s = &self.state;
        s.quality_gate_disabled && last.is_none_or(|(q, _)| *q < s.quality_round)
    }

    /// What the quality gate said of round n's change, the run's final one: round n's last quality file, or for an
    /// unjudged change (unjudged) the run's last one from any round.
    fn gate_evidence(&self, n: u32) -> Result<GateEvidence, OrchestratorError> {
        let s = &self.state;
        let last = self.last_quality(n)?;
        let unjudged = self.unjudged(last.as_ref());
        if last.is_some() || !unjudged {
            let last = last.map(|(quality_round, text)| QualityFile { round: n, quality_round, text });
            return Ok(GateEvidence { last, unjudged });
        }
        // Round n has none, and quality_round no longer says how far an earlier round got: its quality files are
        // numbered from 1 without gaps, so its last is the one before the first missing.
        for m in (1..n).rev() {
            let mut found = None;
            let mut q = 1;
            while let Some(text) = self.host.read(&s.quality_path(m, q))? {
                found = Some(QualityFile { round: m, quality_round: q, text });
                q += 1;
            }
            if found.is_some() {
                return Ok(GateEvidence { last: found, unjudged: true });
            }
        }
        Ok(GateEvidence { last: None, unjudged: true })
    }

    /// What the step's prompt tells the agent the change is: the diff against the base, or for a project that is not
    /// a git repository, the Builder's report.
    fn change_description(&self) -> String {
        let s = &self.state;
        match s.base.as_deref().filter(|b| !b.is_empty()) {
            Some(base) => format!(
                "`git diff {base}` in {}, plus the untracked files `git status --porcelain` lists",
                s.work_dir()
            ),
            None => format!("the files the Builder's report lists, in {} (not a git repository)", s.work_dir()),
        }
    }

    fn state_path(&self) -> String {
        format!("{}/state.json", self.state.dir())
    }

    /// Write state.json, unless another orchestrator has taken the run over since this one claimed it.
    fn save(&mut self) -> Outcome<()> {
        let path = self.state_path();
        if let Some(saved) = self.host.read(&path)? {
            let owner = match Json::parse(&saved) {
                Ok(Json::Object(saved)) => saved.get("owner").cloned().unwrap_or(Json::Null),
                Ok(other) => {
                    let kind = other.python_type();
                    return Err(Stopped::Failed(OrchestratorError(format!(
                        "corrupt {path}: '{kind}' object has no attribute 'get'"
                    ))));
                }
                Err(e) => return Err(Stopped::Failed(OrchestratorError(format!("corrupt {path}: {e}")))),
            };
            if owner != Json::Object(self.me.clone()) {
                let who = match &owner {
                    o if o.truthy() => {
                        let field = |key: &str| o.get(key).map_or("None".to_string(), Json::python_str);
                        format!("pid {} on {}", field("pid"), field("host"))
                    }
                    _ => "another orchestrator".to_string(),
                };
                return Err(Stopped::TakenOver(OrchestratorError(format!(
                    "run {} was taken over by {who}; stopping without saving",
                    self.state.run_id
                ))));
            }
        }
        Ok(self.write_state()?)
    }

    fn write_state(&mut self) -> Result<(), OrchestratorError> {
        self.state.heartbeat_at = Some(self.timestamp());
        self.host.write(&self.state_path(), &self.state.to_text(), false)?;
        self.last_write = self.clocks.monotonic();
        Ok(())
    }

    /// Keep state.json fresh while a turn runs, so the run does not look stale: save it once HEARTBEAT_SECONDS have
    /// passed since the last write. Also where a wait meets the Ctrl-C.
    fn heartbeat(&mut self) -> Outcome<()> {
        self.check_interrupt()?;
        if self.clocks.monotonic() - self.last_write < HEARTBEAT_SECONDS as f64 {
            return Ok(());
        }
        match self.save() {
            Err(Stopped::Failed(e)) => {
                // A run looks stale only after several missed beats, so one failure costs nothing; the next try comes
                // a full interval later rather than on every poll.
                self.human.log(&format!("heartbeat failed: {e}"));
                self.last_write = self.clocks.monotonic();
                Ok(())
            }
            other => other,
        }
    }

    /// Sleep that many seconds, unless the human has asked the run to stop, before or meanwhile.
    fn pause(&self, seconds: u64) -> Outcome<()> {
        self.check_interrupt()?;
        self.clocks.sleep(seconds as f64);
        self.check_interrupt()
    }

    fn check_interrupt(&self) -> Outcome<()> {
        if self.interrupt.is_raised() {
            return Err(Stopped::Interrupted);
        }
        Ok(())
    }

    /// The wall clock's time to the second, as state.json records times.
    fn timestamp(&self) -> String {
        iso_utc(self.clocks.wall().floor() as i64)
    }
}

/// The error of a prompt template named name that cannot be filled in, which the checks of Pipeline and of prompt
/// files leave no case for.
fn prompt_error(name: &str, e: &crate::py::format::FormatError) -> OrchestratorError {
    OrchestratorError(format!("prompt {name} cannot be filled in: {}", e.named()))
}
