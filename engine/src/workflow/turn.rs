//! A role's turn: getting its agent ready, prompting it, and waiting for its handoff file, with the turn's record in
//! RunState.turns.

use super::{How, Outcome, Stopped, Workflow};
use crate::constants::{
    APPROVE, CHANGES_REQUESTED, DEFAULT_AGENT, HEARTBEAT_SECONDS, POLL_SECONDS, PROMPT_DELIVERY_SECONDS,
    STALL_SECONDS,
};
use crate::error::OrchestratorError;
use crate::handoff::{parse_blocked, parse_verdict};
use crate::harness::harness;
use crate::herdr::{Agent, Direction, HerdrError};
use crate::prompts::Prompt;
use crate::py::json::{Json, Map};
use crate::py::text::{basename, splitext, strip};
use crate::summary::{epoch_seconds, iso_utc};

/// What a turn does once its agent is ready, before any prompt.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum Announce {
    Nothing,
    /// The human-paced step's: focus the agent's pane and tell the human it waits for them.
    Interview,
    /// A blocked turn's wait for the human's answer: focus the agent's pane and tell the human its question.
    Blocked { question: String },
}

/// The turn a step asks of a role: what Python's Workflow._turn takes.
pub(super) struct Turn {
    pub role: String,
    /// The handoff file that ends the turn.
    pub path: String,
    /// The prompt for an agent that has seen the role's earlier turns.
    pub text: String,
    /// The prompt for an agent in a new session, which has not; None (or empty) sends text.
    pub fresh_text: Option<String>,
    /// Seconds the turn may take; None for no limit.
    pub timeout: Option<u64>,
    /// Whether an agent idle for STALL_SECONDS without its file is told to the human.
    pub watch_stalls: bool,
    pub announce: Announce,
    pub judged: Judged,
}

/// How a turn judges the handoff file its role wrote, beyond refusing an empty one.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Judged {
    /// Taken as written.
    AsWritten,
    /// The verdict step's: a file without a VERDICT first line is asked for once more (retry) before it ends the run.
    Verdict,
    /// A step that can block's (Step::can_block): a BLOCKED report waits for the human (block), as often as it
    /// comes.
    Blocking,
}

/// Why an agent could not be started: herdr refused, with its code, which a resume into a saved session gets past by
/// starting a fresh one; or the run stops.
enum StartFailure {
    Herdr(HerdrError),
    Stopped(Stopped),
}

impl From<Stopped> for StartFailure {
    fn from(e: Stopped) -> StartFailure {
        StartFailure::Stopped(e)
    }
}

impl From<OrchestratorError> for StartFailure {
    fn from(e: OrchestratorError) -> StartFailure {
        StartFailure::Stopped(Stopped::Failed(e))
    }
}

impl From<HerdrError> for StartFailure {
    fn from(e: HerdrError) -> StartFailure {
        match e {
            HerdrError::Herdr { .. } => StartFailure::Herdr(e),
            HerdrError::Failed(e) => StartFailure::Stopped(Stopped::Failed(e)),
        }
    }
}

impl From<StartFailure> for Stopped {
    fn from(e: StartFailure) -> Stopped {
        match e {
            StartFailure::Herdr(e) => e.into(),
            StartFailure::Stopped(e) => e,
        }
    }
}

impl Workflow<'_> {
    /// The handoff file that ends the role's turn, prompting the role only if it still owes it.
    pub(super) fn turn(&mut self, t: Turn) -> Outcome<String> {
        if t.judged == Judged::Verdict {
            return self.verdict_turn(t);
        }
        let blocks = t.judged == Judged::Blocking;
        let file = basename(&t.path).to_string();
        let block = self.state.blocked.clone().filter(|b| blocks && file_of(b) == Some(&file));
        let out = match block {
            // A resume in the middle of a block.
            Some(block) => {
                self.finish_block(&t.path, &block)?;
                self.unblocked(&t.role, &t.path, block)?
            }
            None => {
                let out = self.written(&t)?;
                match parse_blocked(&out).filter(|_| blocks) {
                    Some(question) => {
                        let block = self.block(&t.role, &t.path, &question)?;
                        self.unblocked(&t.role, &t.path, block)?
                    }
                    None => out,
                }
            }
        };
        if strip(&out).is_empty() {
            let why = format!("was written empty by the {}", self.label(&t.role));
            return Err(self.reject(&t.path, &why));
        }
        Ok(out)
    }

    /// turn for the verdict step: its file, once it starts with a VERDICT line.
    fn verdict_turn(&mut self, mut t: Turn) -> Outcome<String> {
        let file = basename(&t.path).to_string();
        if self.state.retrying.as_deref() == Some(&file) {
            t.text = self.retry_prompt(&t.path)?;
            t.fresh_text = Some(t.text.clone());
        }
        let out = loop {
            let out = self.written(&t)?;
            if parse_verdict(&out).is_some() {
                break out;
            }
            self.retry(&t.role, &t.path, &out)?;
            t.text = self.retry_prompt(&t.path)?;
            t.fresh_text = Some(t.text.clone());
        };
        if self.state.retrying.as_deref() == Some(&file) {
            self.state.retrying = None;
        }
        Ok(out)
    }

    /// The handoff file as the role wrote it, before turn judges it.
    fn written(&mut self, t: &Turn) -> Outcome<String> {
        let (role, path) = (t.role.as_str(), t.path.as_str());
        let file = basename(path).to_string();
        // First, because the role may have written it while no orchestrator was watching.
        if let Some(out) = self.host.read(path)? {
            self.human.log(&format!("[{role}] {file} is already written"));
            self.end_turn(role, path)?;
            return Ok(out);
        }
        let how = self.agent(role)?;
        self.announce(role, how, &t.announce)?;
        self.human.log(&format!("[{role}] working in pane {}", self.agent_field(role, "pane")));
        if let Some(message) = self.prompt_for(how, &file, &t.text, t.fresh_text.as_deref(), path)? {
            // A resumed or restarted turn keeps its record and start. Read before the prompt goes out, so that no
            // agent can write its file before its turn started.
            let started = if self.open_turn(&file).is_some() { None } else { Some(self.host.now()?) };
            self.deliver(role, path, &message)?;
            // Recorded only once the prompt is taken. Dying between sending and confirming costs one duplicate
            // prompt, which the role answers by writing the same file again.
            self.state.prompted = Some(file.clone());
            if let Some(started) = started {
                let record = self.turn_record(role, &file, Some(started));
                self.state.turns.push(record);
            }
            self.save()?;
        }
        let out = self.await_handoff(role, path, t.timeout, t.watch_stalls)?;
        self.human.log(&format!("[{role}] wrote {file}"));
        self.end_turn(role, path)?;
        Ok(out)
    }

    /// What the turn does once the role's agent is ready, as how says it came to be.
    fn announce(&mut self, role: &str, how: How, announce: &Announce) -> Outcome<()> {
        match announce {
            Announce::Nothing => Ok(()),
            Announce::Interview => {
                let (name, pane) = (self.agent_field(role, "name"), self.agent_field(role, "pane"));
                self.herdr.focus(&name)?;
                let label = self.label(role);
                let what = if how == How::Restarted { "restarted; the interview starts over" } else { "is waiting for you" };
                self.human.notify(&format!("{label} {what}"), &format!("pane {pane}: {}", self.state.task));
                self.human.log(&format!("[{role}] answer the {label} in pane {pane}"));
                Ok(())
            }
            Announce::Blocked { question } => {
                self.herdr.focus(&self.agent_field(role, "name"))?;
                self.ask_human(role, &format!("is blocked: {question}"));
                Ok(())
            }
        }
    }

    /// The report of a blocked turn once it is no longer BLOCKED, block being the state's record of the block it is
    /// in (RunState::blocked); each that still is waits again (block).
    fn unblocked(&mut self, role: &str, path: &str, mut block: Map<Json>) -> Outcome<String> {
        let out = loop {
            let out = self.block_answer(role, path, &block)?;
            match parse_blocked(&out) {
                Some(question) => block = self.block(role, path, &question)?,
                None => break out,
            }
        };
        self.state.blocked = None;
        self.save()?;
        Ok(out)
    }

    /// Move a BLOCKED report aside to its next blocked name, and record the block, so that the turn waits for the
    /// human's answer and then for the report again. Returns the record, RunState::blocked.
    fn block(&mut self, role: &str, path: &str, question: &str) -> Outcome<Map<Json>> {
        let file = basename(path).to_string();
        let s = &mut self.state;
        let k = match &s.blocked {
            Some(b) if file_of(b) == Some(&file) => block_number(b)? + 1,
            _ => 1,
        };
        let blocked = blocked_path(path, k);
        // Everything is saved before the rename, which a resume finishes (finish_block): dying at any point leaves the
        // report to be judged again, as block k once more, or the wait under way, never a report lost or counted
        // twice. No prompt counts as delivered, so even an agent that is alive gets BLOCKED_PROMPT.
        s.prompted = None;
        let record: Map<Json> =
            [("file", Json::from(file.as_str())), ("question", Json::from(question)), ("k", Json::Int(k))]
                .into_iter()
                .collect();
        s.blocked = Some(record.clone());
        // The turn's record, which written closed, goes with the file, as in retry; the wait opens a record of its
        // own, human-paced (turn_record).
        let moved = basename(&blocked).to_string();
        if let Some(t) = s.turns.iter_mut().rev().find(|t| file_of(t) == Some(&file)) {
            t.insert("file", Json::from(moved.as_str()));
        }
        self.save()?;
        self.host.rename(path, &blocked)?;
        self.human.log(&format!("[{role}] {file} is blocked: {question}; moved it to {moved}"));
        Ok(record)
    }

    /// Make the rename of the block, RunState::blocked, that the orchestrator saved but may have died before making.
    fn finish_block(&self, path: &str, block: &Map<Json>) -> Result<(), OrchestratorError> {
        let blocked = blocked_path(path, block_number(block)?);
        if self.host.read(&blocked)?.is_none() && self.host.read(path)?.is_some() {
            self.host.rename(path, &blocked)?;
        }
        Ok(())
    }

    /// The role's report once the human has answered the BLOCKED question of block, RunState::blocked, in its pane.
    ///
    /// As for the interview: no turn timeout and no stall notice, since the agent waits for the human. A resumed wait
    /// announces itself again, and prompts as prompt_for does, with BLOCKED_PROMPT in place of the step's.
    fn block_answer(&mut self, role: &str, path: &str, block: &Map<Json>) -> Outcome<String> {
        let blocked = blocked_path(path, block_number(block)?);
        let prompt = self.fill(Prompt::BlockedPrompt, &[("path", path), ("blocked_path", &blocked)])?;
        let question = block_field(block, "question")?.python_str();
        let t = Turn {
            role: role.to_string(),
            path: path.to_string(),
            text: prompt.clone(),
            fresh_text: Some(prompt),
            timeout: None,
            watch_stalls: false,
            announce: Announce::Blocked { question },
            judged: Judged::AsWritten,
        };
        self.written(&t)
    }

    /// Prompt the role's agent with message until it takes it (taken), sending it at most twice.
    ///
    /// Errors: the agent takes neither prompt, naming what a resume does about it, or exits meanwhile; herdr fails.
    fn deliver(&mut self, role: &str, path: &str, message: &str) -> Outcome<()> {
        let name = self.agent_field(role, "name");
        self.herdr.prompt(&name, message)?;
        if self.taken(role, path)? {
            return Ok(());
        }
        self.human.log(&format!("[{role}] did not take its prompt within {PROMPT_DELIVERY_SECONDS}s; sending it again"));
        self.herdr.prompt(&name, message)?;
        if !self.taken(role, path)? {
            let (label, pane) = (self.label(role), self.agent_field(role, "pane"));
            return Err(Stopped::Failed(OrchestratorError(format!(
                "the {label} did not take its prompt to write {path}, sent twice; see pane {pane}, and resume to send \
                 it again"
            ))));
        }
        Ok(())
    }

    /// Whether the role's agent took the prompt just sent, within PROMPT_DELIVERY_SECONDS: herdr reports it working
    /// or blocked, or path, its handoff file, exists. idle and done do not count, since an agent shows them before
    /// taking a prompt too.
    ///
    /// Errors: the agent exits without writing path; herdr fails otherwise than by the wait running out.
    fn taken(&self, role: &str, path: &str) -> Outcome<bool> {
        let name = self.agent_field(role, "name");
        // First, for a turn so quick that the agent is done before the wait begins.
        if self.host.read(path)?.is_some() {
            return Ok(true);
        }
        match self.herdr.wait(&name, Some(PROMPT_DELIVERY_SECONDS * 1000), &["working", "blocked"]) {
            Ok(_) => return Ok(true),
            // herdr's wait on an agent that has exited fails with agent_not_found, which is told apart below.
            Err(e) if matches!(e.code(), Some("timeout" | "agent_not_found")) => {}
            Err(e) => return Err(e.into()),
        }
        if self.host.read(path)?.is_some() {
            return Ok(true);
        }
        if self.herdr.agent(&name)?.is_none() {
            let label = self.label(role);
            return Err(Stopped::Failed(OrchestratorError(format!("the {label} exited without writing {path}"))));
        }
        Ok(false)
    }

    /// Move a malformed verdict file aside so its role writes it again; end the run if its retry is used.
    fn retry(&mut self, role: &str, path: &str, out: &str) -> Outcome<()> {
        let file = basename(path).to_string();
        let why = if strip(out).is_empty() {
            format!("was written empty by the {}", self.label(role))
        } else {
            "does not start with a VERDICT line".to_string()
        };
        if self.state.retried.contains(&file) {
            return Err(self.reject(path, &format!("{why}, and its one retry was already used")));
        }
        let rejected = rejected_path(path);
        // Saved around the rename, so dying at any point leaves the malformed file with its retry unused, no file and
        // no prompt counted as delivered, or the retry under way; never a used retry the role was not asked for.
        self.state.prompted = None;
        self.save()?;
        self.host.rename(path, &rejected)?;
        self.state.retried.push(file.clone());
        self.state.retrying = Some(file.clone());
        // The turn's record, which written closed, goes with the file, whose mtime the rename kept; the retry opens a
        // record of its own.
        let moved = basename(&rejected).to_string();
        if let Some(record) = self.state.turns.iter_mut().rev().find(|t| file_of(t) == Some(&file)) {
            record.insert("file", Json::from(moved.as_str()));
        }
        self.save()?;
        self.human.log(&format!("[{role}] {file} {why}; moved it to {moved}, prompting once more"));
        Ok(())
    }

    fn retry_prompt(&self, path: &str) -> Result<String, OrchestratorError> {
        let rejected = rejected_path(path);
        let values = [("path", path), ("rejected_path", &rejected), ("approve", APPROVE), ("changes", CHANGES_REQUESTED)];
        self.fill(Prompt::RetryVerdictPrompt, &values)
    }

    /// What to prompt an agent that is ready as how says with; None when it only has to be waited for.
    fn prompt_for(
        &self,
        how: How,
        file: &str,
        text: &str,
        fresh_text: Option<&str>,
        path: &str,
    ) -> Result<Option<String>, OrchestratorError> {
        if matches!(how, How::New | How::Restarted) {
            return Ok(Some(fresh_text.filter(|f| !f.is_empty()).unwrap_or(text).to_string()));
        }
        if self.state.prompted.as_deref() != Some(file) {
            return Ok(Some(text.to_string()));
        }
        if how == How::Resumed {
            return Ok(Some(self.fill(Prompt::ContinuePrompt, &[("path", path)])?));
        }
        // Alive and already prompted for this file.
        Ok(None)
    }

    /// Poll until the role writes path, telling the human when it is blocked or stalled.
    fn await_handoff(&mut self, role: &str, path: &str, timeout: Option<u64>, watch_stalls: bool) -> Outcome<String> {
        let (label, file) = (self.label(role), basename(path).to_string());
        let deadline = timeout.map(|t| self.clocks.monotonic() + t as f64);
        // Since when the agent has shown idle or done without the file; None while it works.
        let mut quiet_since: Option<f64> = None;
        // What the human was last told about this turn.
        let mut told: Option<&'static str> = None;
        loop {
            if let Some(out) = self.host.read(path)? {
                return Ok(out);
            }
            let Some(record) = self.herdr.agent(&self.agent_field(role, "name"))? else {
                return Err(Stopped::Failed(OrchestratorError(format!("the {label} exited without writing {path}"))));
            };
            self.note_session(role, &record)?;
            let now = self.clocks.monotonic();
            if let (Some(deadline), Some(timeout)) = (deadline, timeout) {
                if now > deadline {
                    let pane = self.agent_field(role, "pane");
                    return Err(Stopped::Failed(OrchestratorError(format!(
                        "the {label} did not write {path} within {timeout}s; see pane {pane}"
                    ))));
                }
            }
            if !matches!(record.status.as_str(), "idle" | "done") {
                quiet_since = None;
            } else if quiet_since.is_none() {
                quiet_since = Some(now);
            }
            let stalled = watch_stalls && quiet_since.is_some_and(|since| now - since >= STALL_SECONDS as f64);
            told = self.tell_human(role, &record.status, stalled, told, &file);
            self.heartbeat()?;
            self.pause(POLL_SECONDS)?;
        }
    }

    /// Notify the human of a blocked or stalled agent once; what they have been told now.
    fn tell_human(
        &self,
        role: &str,
        status: &str,
        stalled: bool,
        told: Option<&'static str>,
        file: &str,
    ) -> Option<&'static str> {
        if status == "blocked" && told != Some("blocked") {
            self.ask_human(role, "needs your answer");
            return Some("blocked");
        }
        if stalled && told != Some("stalled") {
            self.ask_human(role, &format!("is idle without writing {file}"));
            return Some("stalled");
        }
        if status == "working" {
            None
        } else {
            told
        }
    }

    /// The error that ends the run over a handoff file the role wrote wrong.
    ///
    /// Resume does not judge a role's output; the human fixes the file or deletes it, and a deleted file is asked
    /// for again, so the prompt no longer counts as delivered, and neither does a retry or a block: the deleted file
    /// is asked for with the step's own prompt.
    pub(super) fn reject(&mut self, path: &str, why: &str) -> Stopped {
        self.state.prompted = None;
        self.state.retrying = None;
        self.state.blocked = None;
        Stopped::Failed(OrchestratorError(format!(
            "{path} {why}; fix it, or delete it to have it written again, then resume"
        )))
    }

    /// The index in RunState.turns of the record of the turn under way that writes file; None when there is none.
    pub(super) fn open_turn(&self, file: &str) -> Option<usize> {
        self.state.turns.iter().position(|t| file_of(t) == Some(file) && t.get("ended_at").is_some_and(Json::is_null))
    }

    /// An open record of the current phase's turn that writes file: the current step's, or with no step of the phase,
    /// a quality analysis, which runs no agent. started is None when the turn's start is not known.
    pub(super) fn turn_record(&self, role: &str, file: &str, started: Option<i64>) -> Map<Json> {
        let s = &self.state;
        let step = self.pipeline.step(&s.phase);
        let known = s.agents.get(role);
        let text = |value: Option<&String>| value.map_or(Json::Null, |v| Json::from(v.as_str()));
        let (agent, model, session) = match step {
            None => (Json::Null, Json::Null, Json::Null),
            Some(_) => {
                let kind = known.and_then(|a| a.get("kind")).filter(|k| !k.is_empty());
                let kind = kind.or_else(|| s.agent_kinds.get(role)).map_or(DEFAULT_AGENT, String::as_str);
                (Json::from(kind), text(s.models.get(role)), text(known.and_then(|a| a.get("session"))))
            }
        };
        let blocked = s.blocked.as_ref().is_some_and(|b| file_of(b) == Some(file));
        let quality_round = if step.is_none_or(|st| st.quality_gated) { s.quality_round } else { 0 };
        [
            ("step", Json::from(s.phase.as_str())),
            ("role", Json::from(role)),
            ("round", Json::from(i64::from(s.round))),
            ("quality_round", Json::from(i64::from(quality_round))),
            ("file", Json::from(file)),
            // The agent's session for the whole turn, None when that is not known: end_turn clears it for a turn that
            // ended in another session than it started in.
            ("agent", agent),
            ("model", model),
            ("session", session),
            // The tokens of the turn, which count_tokens counts once the run is done; None until then, and always
            // for a quality analysis.
            ("tokens", Json::Null),
            // A blocked turn's wait for the human's answer, and the rewrite after it, is paced by the human too.
            ("human_paced", Json::from(step.is_some_and(|st| st.human_paced) || blocked)),
            ("started_at", started.map_or(Json::Null, |t| Json::from(iso_utc(t)))),
            ("ended_at", Json::Null),
            ("seconds", Json::Null),
        ]
        .into_iter()
        .collect()
    }

    /// Close the record of the turn that wrote path, found written, at the file's mtime: the turn ended then, even
    /// if no orchestrator was watching. A file whose turn has no record, as one written before turns were recorded,
    /// gets one with no start.
    pub(super) fn end_turn(&mut self, role: &str, path: &str) -> Outcome<()> {
        let file = basename(path);
        let i = match self.open_turn(file) {
            Some(i) => i,
            // Closed already, by an orchestrator that stopped before the run moved on.
            None if self.state.turns.iter().any(|t| file_of(t) == Some(file)) => return Ok(()),
            None => {
                let record = self.turn_record(role, file, None);
                self.state.turns.push(record);
                self.state.turns.len() - 1
            }
        };
        let session = self.state.agents.get(role).and_then(|a| a.get("session")).map(|s| Json::from(s.as_str()));
        if self.state.turns[i].get("session").unwrap_or(&Json::Null) != &session.unwrap_or(Json::Null) {
            self.state.turns[i].insert("session", Json::Null);
        }
        let ended = self.host.mtime(path)?;
        let record = &mut self.state.turns[i];
        record.insert("ended_at", Json::from(iso_utc(ended)));
        if let Some(started) = record.get("started_at").and_then(Json::as_str) {
            let started = epoch_seconds(started)
                .map_err(|e| OrchestratorError(format!("the turn that wrote {file} started at no time: {e}")))?;
            record.insert("seconds", Json::from(ended - started));
        }
        self.save()
    }

    /// Make sure the role's agent is running, and say how.
    fn agent(&mut self, role: &str) -> Outcome<How> {
        let known = self.state.agents.get(role).cloned();
        if let Some(known) = &known {
            if self.herdr.status(known.get("name").map_or("", String::as_str))?.is_some() {
                return Ok(How::Alive);
            }
        }
        let pane = self.pane_for(role)?;
        let session = known.as_ref().and_then(|a| a.get("session")).filter(|s| !s.is_empty()).cloned();
        let kind = known.as_ref().and_then(|a| a.get("kind")).map_or(DEFAULT_AGENT, String::as_str).to_string();
        let target = self.agent_kind(role)?;
        if let Some(session) = &session {
            if kind != target {
                self.human.log(&format!(
                    "[{role}] session {session} is {kind}'s, and the role runs in {target} now; starting a fresh one"
                ));
            } else {
                // Keeps the conversation: for the Spec Collector that is the interview itself.
                match self.start(role, &pane, Some(session)) {
                    Ok(true) => return Ok(How::Resumed),
                    Ok(false) => {}
                    Err(StartFailure::Herdr(e)) => {
                        self.human.log(&format!("[{role}] could not resume session {session}: {e}"));
                    }
                    Err(StartFailure::Stopped(e)) => return Err(e),
                }
                self.human.log(&format!("[{role}] session {session} is gone; starting a fresh one"));
            }
        }
        self.start(role, &pane, None)?;
        Ok(if known.is_some() { How::Restarted } else { How::New })
    }

    /// The pane for the role's next agent: its old one if that survives, else a new split.
    fn pane_for(&self, role: &str) -> Outcome<String> {
        let s = &self.state;
        let roles = self.pane_order(role);
        let i = roles.iter().position(|r| r == role).unwrap_or(0);
        let pane_of = |r: &str| s.agents.get(r).and_then(|a| a.get("pane")).filter(|p| !p.is_empty()).cloned();
        let old = pane_of(role).unwrap_or_else(|| if i == 0 { s.root_pane.clone() } else { String::new() });
        if !old.is_empty() && self.herdr.pane_exists(&old)? {
            return Ok(old);
        }
        // The layout: the second role to the right of the root pane, each further one below the one before.
        let (parent, direction) = if i >= 2 {
            (pane_of(&roles[i - 1]).unwrap_or_default(), Direction::Down)
        } else {
            (s.root_pane.clone(), Direction::Right)
        };
        let mut survivors = vec![parent, s.root_pane.clone()];
        survivors.extend(s.agents.values().map(|a| a.get("pane").cloned().unwrap_or_default()));
        let mut tried = vec![];
        for pane in survivors {
            if pane.is_empty() || pane == old || tried.contains(&pane) {
                continue;
            }
            if self.herdr.pane_exists(&pane)? {
                return Ok(self.herdr.split(&pane, direction, s.work_dir())?);
            }
            tried.push(pane);
        }
        Err(Stopped::Failed(OrchestratorError(format!(
            "no pane of run {} is left in herdr workspace {}",
            s.run_id, s.workspace_id
        ))))
    }

    /// The roles in the order their panes are laid out, the root pane going to the first.
    ///
    /// That is the workflow's order, unless the run skipped its contract step (run --spec): then the first role to
    /// take a turn leads, and the contract's role, which may still have a later step, comes last. RunState.agents
    /// keeps the order the roles' agents first started in, and the role asking now is the first when none has.
    fn pane_order(&self, role: &str) -> Vec<String> {
        let roles: Vec<String> = self.pipeline.roles().keys().map(String::from).collect();
        let first = self.state.agents.keys().next().unwrap_or(role).to_string();
        let Some(contract) = self.pipeline.contract().map(|c| c.role.clone()).filter(|c| *c != first) else {
            return roles;
        };
        let mut order = vec![first.clone()];
        order.extend(roles.into_iter().filter(|r| *r != first && *r != contract));
        order.push(contract);
        order
    }

    /// Start the role's agent in the pane, in its saved session if given one; false when it exited at once.
    fn start(&mut self, role: &str, pane: &str, session: Option<&str>) -> Result<bool, StartFailure> {
        let name = format!("{role}-{}", self.state.key());
        let kind = self.agent_kind(role)?;
        let h = harness(&kind)?;
        let mode = self.state.permission_mode.clone().filter(|m| !m.is_empty());
        if let Some(mode) = &mode {
            if h.permission_args(mode).is_none() && self.told_dropped.insert(role.to_string()) {
                self.human.log(&format!(
                    "[{role}] {kind} has no equivalent of --permission-mode {mode}; starting it without one"
                ));
            }
        }
        self.herdr.rename_pane(pane, &self.label(role))?;
        // The handoff files are outside a worktree run's working directory.
        let extra_dir = self.state.worktree.as_ref().map(|_| self.state.dir());
        let model = self.state.models.get(role).cloned();
        let args = h.launch_args(self.state.permission_mode.as_deref(), model.as_deref(), session, extra_dir.as_deref());
        let ready = self.herdr.start_agent(&name, pane, &kind, &args)?;
        let record = [("name", name.clone()), ("pane", pane.to_string()), ("kind", kind)].into_iter().collect();
        self.state.agents.insert(role, record);
        self.save()?;
        if !ready {
            self.ask_human(role, "needs your answer");
            self.wait_for_startup(&name)?;
        }
        let Some(record) = self.herdr.agent(&name)? else { return Ok(false) };
        self.note_session(role, &record)?;
        Ok(true)
    }

    /// Wait for a startup dialog the human answers.
    ///
    /// The human decides how long that takes, as with the interview, so there is no deadline; the wait is cut into
    /// steps so the heartbeat keeps the run from looking stale.
    fn wait_for_startup(&mut self, name: &str) -> Result<(), StartFailure> {
        loop {
            match self.herdr.wait(name, Some(HEARTBEAT_SECONDS * 1000), &["idle", "done"]) {
                Ok(_) => return Ok(()),
                Err(e) if e.code() == Some("timeout") => {}
                Err(e) => return Err(e.into()),
            }
            self.heartbeat()?;
        }
    }

    /// Save the session herdr reports the role's agent in as soon as it does, so an agent that exits later can be
    /// resumed into it.
    fn note_session(&mut self, role: &str, record: &Agent) -> Outcome<()> {
        let Some(session) = record.session.as_deref().filter(|s| !s.is_empty()) else { return Ok(()) };
        let agent = self.state.agents.get_mut(role).ok_or_else(|| OrchestratorError(format!("no agent of {role}")))?;
        if agent.get("session").map(String::as_str) != Some(session) {
            agent.insert("session", session.to_string());
            self.save()?;
        }
        Ok(())
    }

    /// Tell the human the role's agent is in a state that needs them, naming its pane.
    pub(super) fn ask_human(&self, role: &str, what: &str) {
        let pane = self.agent_field(role, "pane");
        self.human.notify(&format!("{} {what}", self.label(role)), &format!("pane {pane}"));
        self.human.log(&format!("[{role}] {what} (pane {pane})"));
    }

    /// A field of the role's agent record, name or pane; "" for an agent or a field the record lacks.
    fn agent_field(&self, role: &str, field: &str) -> String {
        self.state.agents.get(role).and_then(|a| a.get(field)).cloned().unwrap_or_default()
    }

    /// The harness the role's next agent starts in. Errors: the state names none, which Workflow::new rules out.
    fn agent_kind(&self, role: &str) -> Result<String, OrchestratorError> {
        self.state.agent_kinds.get(role).cloned().ok_or_else(|| OrchestratorError(format!("no harness for role {role}")))
    }

    /// The role's label, as the workflow names it to the human.
    pub(super) fn label(&self, role: &str) -> String {
        self.pipeline.roles().get(role).cloned().unwrap_or_else(|| role.to_string())
    }
}

/// The file a turn record, or the record of a block (RunState::blocked), is of; None for a record without one.
fn file_of(record: &Map<Json>) -> Option<&str> {
    record.get("file").and_then(Json::as_str)
}

/// Where a BLOCKED report of the handoff file at path is moved the k-th time its turn blocks.
fn blocked_path(path: &str, k: i128) -> String {
    let (stem, ext) = splitext(path);
    format!("{stem}.blocked-{k}{ext}")
}

/// The k of a block, RunState::blocked: how many times its turn has blocked, its report's blocked name's number.
/// Errors: as block_field's, and for a k that is no integer.
fn block_number(block: &Map<Json>) -> Result<i128, OrchestratorError> {
    let k = block_field(block, "k")?;
    k.as_int().ok_or_else(|| OrchestratorError(format!("the block saved in state.json has k {}, no number", k.dumps())))
}

/// The field key of a block, RunState::blocked. Errors: the record lacks it, as only a state.json edited by hand
/// does, where Python's engine fails with a KeyError.
fn block_field<'b>(block: &'b Map<Json>, key: &str) -> Result<&'b Json, OrchestratorError> {
    block.get(key).ok_or_else(|| OrchestratorError(format!("the block saved in state.json has no {key}")))
}

/// Where a malformed verdict file is moved, for its retry.
fn rejected_path(path: &str) -> String {
    let (stem, ext) = splitext(path);
    format!("{stem}.rejected{ext}")
}
