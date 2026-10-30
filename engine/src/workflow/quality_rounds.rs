//! The quality gate's rounds (docs/quality-gate.md): after each turn of the quality-gated step, a quality round has
//! Jenkins judge a snapshot of the change, adds what the orchestrator's own checks find, and sends a change the gate
//! does not pass back to that step, up to max_quality_rounds analyses per round; what the verdict step hears of them;
//! and the snapshots' refs on origin, deleted once they are of no more use.
//!
//! Every link of a round's chain (the snapshot pushed, the build triggered, its number) is saved in RunState::ci as
//! soon as it is known, so that a resume picks the round up from the furthest one.

use std::cell::RefCell;
use std::rc::Rc;

use super::{Human, Outcome, Stopped, Workflow};
use crate::checks::{judge_run, orchestrator_checks, RunChange, SLOW_STEP_MARGIN};
use crate::ci::{CIError, Jenkins};
use crate::constants::{CI_POLL_SECONDS, CI_REF_PREFIX, QUALITY_TIMEOUT, REMOTE};
use crate::error::OrchestratorError;
use crate::host::{Host, LeaksFound, PushError};
use crate::patch::added_lines;
use crate::pipeline::{Step, QUALITY};
use crate::prompts::Prompt;
use crate::py::json::{Json, Map};
use crate::py::text::{basename, strip};
use crate::quality::{build_failed_report, gate_failures, leak_report, parse_gate, under_gate_line, QUALITY_OK};
use crate::state::RunState;

/// What a quality round's analysis of its snapshot came to (Workflow::analyse).
enum Analysis {
    /// Jenkins built the snapshot, as build number build. result is None for a build still running the stages after
    /// its Gate stage, which cannot change the verdict its quality file holds; quality, the quality file it archived,
    /// is None for a build that ended before its Gate stage. result is never None without a quality file.
    Built { build: i64, result: Option<String>, quality: Option<String> },
    /// gitleaks found secrets in the snapshot itself, and in nothing below it, so nothing was pushed or triggered.
    Leaked(LeaksFound),
}

/// What a poll of Jenkins (Workflow::poll) gives on each call: the answer it waits for, None while it still waits,
/// or why the call failed. A CIError::Request that may pass is retried; any other error stops the poll.
type Polled<T> = Result<Option<T>, CIError>;

impl<'a> Workflow<'a> {
    /// The current quality round of round n, of the quality-gated step gated; false when its gate sends the change
    /// back to gated: when it failed, and not on just what the quality round before failed on (stuck).
    ///
    /// Errors: the round's own (quality), and saving.
    pub(super) fn judge_quality(&mut self, gated: &Step, n: u32) -> Outcome<bool> {
        let q = self.state.quality_round;
        let gate = self.quality(n, q)?;
        // As for review rounds: a saved round beyond a lowered limit ends here too.
        if gate == QUALITY_OK || q >= self.state.max_quality_rounds {
            return Ok(true);
        }
        if self.stuck(n, q)? {
            self.human.log(&format!(
                "[quality] round {n}, quality round {q} failed on just what quality round {} failed on; going on \
                 rather than back to {}",
                q - 1,
                gated.id
            ));
            return Ok(true);
        }
        self.state.phase = gated.id.clone();
        self.save()?;
        Ok(false)
    }

    /// Whether quality round q of round n failed on the failures of quality round q-1 again (GateFailures::repeats):
    /// the gated step's turn between them fixed none of them. False for the first quality round, and when either
    /// file is missing.
    fn stuck(&self, n: u32, q: u32) -> Result<bool, OrchestratorError> {
        if q < 2 {
            return Ok(false);
        }
        let s = &self.state;
        let before = self.host.read(&s.quality_path(n, q - 1))?;
        let after = self.host.read(&s.quality_path(n, q))?;
        Ok(match (before, after) {
            (Some(before), Some(after)) => gate_failures(&after).repeats(&gate_failures(&before)),
            _ => false,
        })
    }

    /// What the verdict step hears of round n's last quality file, after a blank line: QUALITY_PASSED_NOTE, or the
    /// findings left (QUALITY_STUCK_NOTE when the last quality round was stuck, else QUALITY_UNRESOLVED_NOTE), or for
    /// an unjudged change (unjudged) QUALITY_OFF_NOTE naming each of the round's quality files; "" without one.
    /// Errors: a file cannot be read, or the prompt cannot be filled in.
    pub(super) fn quality_note(&self, n: u32) -> Result<String, OrchestratorError> {
        let s = &self.state;
        let Some(last) = self.last_quality(n)? else { return Ok(String::new()) };
        if self.unjudged(Some(&last)) {
            let paths: Vec<String> = (1..=last.0).map(|k| s.quality_path(n, k)).collect();
            let note = self.fill(Prompt::QualityOffNote, &[("quality_paths", &paths.join(", "))])?;
            return Ok(format!("\n\n{note}"));
        }
        let (q, text) = last;
        let note = if parse_gate(&text) == Some(QUALITY_OK) {
            Prompt::QualityPassedNote
        } else if self.stuck(n, q)? {
            Prompt::QualityStuckNote
        } else {
            Prompt::QualityUnresolvedNote
        };
        Ok(format!("\n\n{}", self.fill(note, &[("quality_path", &s.quality_path(n, q))])?))
    }

    /// Whether round n's last quality round passed the gate; true for a run without the gate, or whose round had no
    /// quality round.
    pub(super) fn gate_passed(&self, n: u32) -> Result<bool, OrchestratorError> {
        let s = &self.state;
        if self.jenkins.is_none() || s.quality_round == 0 {
            return Ok(true);
        }
        // With the gate on, the quality loop has written the current quality round's file by now; one missing fails.
        let last = self.last_quality(n)?;
        Ok(last.is_some_and(|(q, text)| q == s.quality_round && parse_gate(&text) == Some(QUALITY_OK)))
    }

    /// The run's quality job. Errors: the run has no quality gate, which only a quality round asks it for.
    fn jenkins(&self) -> Result<&'a Jenkins, OrchestratorError> {
        self.jenkins.ok_or_else(|| OrchestratorError("a quality round in a run without the quality gate".into()))
    }

    /// Quality round q of round n: have Jenkins judge the change, write quality-<n>-<q>.md, and return its gate. A
    /// snapshot gitleaks finds a secret in is not pushed, and its quality file is QUALITY_LEAK.
    ///
    /// Each step first looks for what an earlier orchestrator already did, furthest first: the quality file; then, in
    /// analyse, the chain in RunState::ci. Errors: Jenkins fails in a way that does not pass, or not within
    /// QUALITY_TIMEOUT seconds of the round's start; the build was aborted, cancelled or discarded; the quality file
    /// does not start with a GATE line; a secret is in the run's base; git, a file or saving fails; the Ctrl-C.
    fn quality(&mut self, n: u32, q: u32) -> Outcome<&'static str> {
        let path = self.state.quality_path(n, q);
        let file = basename(&path).to_string();
        let git_ref = format!("{CI_REF_PREFIX}{}-{n}-q{q}", self.state.key());
        let text = match self.host.read(&path)? {
            Some(text) => {
                self.human.log(&format!("[quality] {file} is already written"));
                text
            }
            None => {
                if self.open_turn(&file).is_none() {
                    let record = self.turn_record(QUALITY, &file, Some(self.host.now()?));
                    self.state.turns.push(record);
                    self.save()?;
                }
                // Per round, and afresh on a resume.
                let deadline = self.clocks.monotonic() + QUALITY_TIMEOUT as f64;
                let message = format!("Orchestrator run {}: round {n}, quality round {q}", self.state.run_id);
                let base = self.state.base.clone().ok_or_else(|| {
                    Stopped::Failed(OrchestratorError("a quality round before the run has a base".into()))
                })?;
                let text = match self.analyse(&git_ref, deadline, &base, &message)? {
                    Analysis::Leaked(found) => self.leak_report(n, q, &found),
                    Analysis::Built { build, result, quality } => {
                        let report =
                            self.quality_report(n, q, build, result.as_deref(), quality.as_deref(), deadline)?;
                        self.jenkins()?.mask(&report)
                    }
                };
                self.host.write(&path, &text, false)?;
                text
            }
        };
        self.end_turn(QUALITY, &path)?;
        let Some(gate) = parse_gate(&text) else { return Err(self.reject(&path, "does not start with a GATE line")) };
        let (round, quality_round) = (Json::from(i64::from(n)), Json::from(i64::from(q)));
        let recorded = self
            .state
            .quality_rounds
            .iter()
            .any(|r| r.get("round") == Some(&round) && r.get("quality_round") == Some(&quality_round));
        if !recorded {
            let failures = gate_failures(&text);
            let strings = |items: &[String]| Json::Array(items.iter().map(|i| Json::from(i.as_str())).collect());
            let fingerprints = failures.fingerprints.iter().map(|(check, found)| (check, strings(found))).collect();
            let record = [
                ("round", round),
                ("quality_round", quality_round),
                ("gate", Json::from(gate)),
                ("conditions", strings(&failures.conditions)),
                ("fingerprints_by_check", Json::Object(fingerprints)),
            ];
            self.state.quality_rounds.push(record.into_iter().collect());
        }
        self.human.log(&format!("[quality] round {n}, quality round {q}: {gate}"));
        self.finish_analysis(&git_ref)?;
        Ok(gate)
    }

    /// Have Jenkins judge a snapshot of the change, a commit with message whose only parent is base, pushed to the
    /// branch git_ref; what it came to, as soon as the build has archived its quality file or has ended, whichever
    /// comes first.
    ///
    /// The snapshot is pushed once gitleaks, by base's rules, finds no secret in it or in the commits below it that
    /// origin lacks; on a secret, nothing is pushed or triggered and RunState::ci is left empty, and the analysis is
    /// Leaked when every secret is in the snapshot. Each link of the chain is saved in RunState::ci as soon as it is
    /// known, so a resume picks it up from the furthest one; a ce_task a SonarQube-era orchestrator saved is ignored,
    /// and its build, which archived no quality file, counts as one that failed.
    ///
    /// Errors: as quality's; a secret in base or below it, which is the human's to rewrite.
    fn analyse(&mut self, git_ref: &str, deadline: f64, base: &str, message: &str) -> Outcome<Analysis> {
        let fresh = self.state.ci.get("ref").and_then(Json::as_str) != Some(git_ref);
        if fresh {
            if let Some(old) = self.state.ci.get("ref").filter(|r| r.truthy()).map(Json::python_str) {
                delete_ci_ref(self.host, self.human, &self.state.cwd, &old);
                self.state.ci = Map::new();
            }
            let work_dir = self.state.work_dir().to_string();
            let sha = self.host.snapshot(&work_dir, base, message)?;
            let report_path = self.state.leaks_path("snapshot");
            match self.host.push_ref(&work_dir, &sha, git_ref, base, &report_path) {
                Ok(()) => {}
                Err(PushError::Failed(e)) => return Err(e.into()),
                // The snapshot's only parent is base, so a secret in any other commit is in base or below it: the
                // human's history, which the Builder cannot rewrite.
                Err(PushError::Leaks(found)) if found.leaks.iter().any(|leak| leak.commit != sha) => {
                    let (snapshot, base): (String, String) = (sha.chars().take(12).collect(), base.chars().take(12).collect());
                    return Err(Stopped::Failed(OrchestratorError(format!(
                        "{found}\nThose not in snapshot {snapshot} are in the run's base {base} or below it, which \
                         {REMOTE} does not have: the human's commits, not the Builder's to fix. Rewrite them without \
                         the secret, or list a finding that is not one in .gitleaksignore there, and start a new run."
                    ))));
                }
                Err(PushError::Leaks(found)) => return Ok(Analysis::Leaked(found)),
            }
            self.state.ci = [("ref", Json::from(git_ref)), ("sha", Json::from(sha))].into_iter().collect();
            self.save()?;
        }
        if !self.state.ci.contains_key("build") {
            self.start_build(git_ref, deadline, !fresh)?;
        }
        let number = self.ci_build()?;
        // The last read of the file while the build ran; None before one found it.
        let mut earlier: Option<String> = None;
        let gate_or_end = |wf: &mut Self| -> Polled<(Option<String>, Option<String>)> {
            let result = wf.build_result(git_ref, number)?;
            let quality = wf.jenkins()?.quality_file(number)?;
            if result.is_some() {
                return Ok(Some((result, quality)));
            }
            // Jenkins serves an artifact while archiveArtifacts is still copying it into place, so one read of a
            // running build's file may be cut short. Two reads that agree, at least a poll interval apart, and hold
            // a whole non-blank line, as the GATE line is, are taken as whole: so an empty copy or one cut inside its
            // first line is not, while a whole first line that is no GATE line is, for quality to report. A
            // heuristic, which a copy stalled that long past its first line and then resumed defeats (TODO.md). A
            // build that has ended has finished archiving, so one read then is enough.
            if let Some(text) = quality.as_deref().filter(|text| earlier.as_deref() == Some(*text)) {
                let mut lines: Vec<&str> = text.split('\n').collect();
                lines.pop();
                if lines.iter().any(|line| !strip(line).is_empty()) {
                    return Ok(Some((None, quality)));
                }
            }
            earlier = quality;
            Ok(None)
        };
        let waiting = format!("waiting for Jenkins build #{number} of {git_ref} to archive quality.md or end");
        let (result, quality) = self.poll(&waiting, deadline, gate_or_end)?;
        if quality.is_none() && result.as_deref() == Some("ABORTED") {
            self.cut_back(Some(number));
            return Err(Stopped::Failed(OrchestratorError(format!(
                "Jenkins build #{number} of {git_ref} was aborted before the quality gate ran, so nothing judged the \
                 change; resume to build it again"
            ))));
        }
        Ok(Analysis::Built { build: number, result, quality })
    }

    /// Record in RunState::ci the build of git_ref: one triggered now, or with adopt one an earlier orchestrator
    /// triggered and did not record.
    fn start_build(&mut self, git_ref: &str, deadline: f64, adopt: bool) -> Outcome<()> {
        if !self.state.ci.contains_key("queue_url") {
            // The ref names one round of one run, so a build of it is this round's, triggered by an orchestrator that
            // stopped before it recorded the queue item.
            let found = if adopt {
                let rejected = self.ci_rejected();
                let waiting = format!("looking for a Jenkins build of {git_ref}");
                self.once(&waiting, deadline, |wf| wf.jenkins()?.find_build(git_ref, &rejected))?
            } else {
                None
            };
            if let Some(found) = found {
                self.human.log(&format!("[quality] adopting Jenkins build #{found} of {git_ref}"));
                self.state.ci.insert("build", Json::from(found));
                return self.save();
            }
            let waiting = format!("triggering Jenkins job {} for {git_ref}", self.jenkins()?.job());
            let queue_url = self.once(&waiting, deadline, |wf| wf.jenkins()?.trigger(git_ref))?;
            self.state.ci.insert("queue_url", Json::from(queue_url));
            self.save()?;
        }
        let waiting = format!("waiting for Jenkins to start the build of {git_ref}");
        let build = self.poll(&waiting, deadline, |wf| wf.queued_build(git_ref))?;
        self.state.ci.insert("build", Json::from(build));
        self.save()
    }

    /// The number of the build RunState::ci's queue item started, of git_ref; None while it waits. A queue item
    /// Jenkins forgot is found among the job's builds instead.
    ///
    /// Errors: a failed request; refused, with RunState::ci cut back so that a resume triggers a new build, for an
    /// item cancelled or forgotten without a build, or a queue_url that is not a string.
    fn queued_build(&mut self, git_ref: &str) -> Polled<i64> {
        let queue_url = self.ci_text("queue_url")?;
        let jenkins = self.jenkins()?;
        let item = match jenkins.queue_item(&queue_url) {
            Ok(item) => item,
            // Jenkins forgets a queue item some minutes after its build started.
            Err(e) if e.status() == Some(404) => {
                return match jenkins.find_build(git_ref, &self.ci_rejected())? {
                    Some(found) => Ok(Some(found)),
                    None => {
                        self.cut_back(None);
                        Err(CIError::Refused(OrchestratorError(format!(
                            "Jenkins forgot queue item {queue_url}, and none of the job's last 20 builds is of \
                             {git_ref}; resume to trigger a new one"
                        ))))
                    }
                };
            }
            Err(e) => return Err(e),
        };
        if item.cancelled {
            self.cut_back(None);
            return Err(CIError::Refused(OrchestratorError(format!(
                "the build of {git_ref} was cancelled in Jenkins's queue, so nothing judged the change; resume to \
                 trigger a new one"
            ))));
        }
        Ok(item.build)
    }

    /// Jenkins::build_result of number, which RunState::ci records as the build of git_ref. Errors: a failed
    /// request; refused, with RunState::ci cut back so that a resume builds git_ref again, for a build Jenkins no
    /// longer has.
    fn build_result(&mut self, git_ref: &str, number: i64) -> Result<Option<String>, CIError> {
        match self.jenkins()?.build_result(number) {
            // The Jenkinsfile's buildDiscarder drops the job's oldest builds, which a run stopped mid-round and resumed
            // much later can still name.
            Err(e) if e.status() == Some(404) => {
                self.cut_back(None);
                Err(CIError::Refused(OrchestratorError(format!(
                    "Jenkins build #{number} of {git_ref} is gone, discarded by the job; resume to build it again"
                ))))
            }
            answer => answer,
        }
    }

    /// Keep only the pushed commit in RunState::ci, so that a resume builds it anew, never adopting a build rejected
    /// earlier or the rejected one given.
    fn cut_back(&mut self, rejected: Option<i64>) {
        let ci = &self.state.ci;
        let mut skip = ci.get("rejected").and_then(Json::as_array).cloned().unwrap_or_default();
        skip.extend(rejected.map(Json::from));
        let field = |key: &str| ci.get(key).cloned().unwrap_or(Json::Null);
        let mut cut: Map<Json> = [("ref", field("ref")), ("sha", field("sha"))].into_iter().collect();
        if !skip.is_empty() {
            cut.insert("rejected", Json::Array(skip));
        }
        self.state.ci = cut;
    }

    /// The quality file of quality round q of round n, built as build and having the result and quality file
    /// analyse gives: the build's own file with the orchestrator's checks added (judged_by_orchestrator), naming the
    /// build under its GATE line; or, for a build that ended before its Gate stage, a BUILD_FAILED one with the
    /// failing tests or the console's end. Not yet masked.
    fn quality_report(
        &mut self,
        n: u32,
        q: u32,
        build: i64,
        result: Option<&str>,
        quality: Option<&str>,
        deadline: f64,
    ) -> Outcome<String> {
        let sha = self.ci_text("sha")?;
        let place = self.place(n, q);
        if let Some(quality) = quality {
            let how_it_stood = match result {
                None => "was still running when its quality.md was read".to_string(),
                Some(result) => format!("ended {result}"),
            };
            let judged = self.judged_by_orchestrator(quality, &sha, deadline)?;
            let line = format!("Jenkins build #{build} judged snapshot `{sha}` ({place}) and {how_it_stood}.");
            return Ok(under_gate_line(&judged, &line));
        }
        let waiting = format!("reading the test report of Jenkins build #{build}");
        let tests = self.once(&waiting, deadline, |wf| wf.jenkins()?.failed_tests(build))?;
        let console = if tests.as_ref().is_some_and(|t| !t.is_empty()) {
            String::new()
        } else {
            let waiting = format!("reading the console of Jenkins build #{build}");
            self.once(&waiting, deadline, |wf| wf.jenkins()?.console_tail(build))?
        };
        let intro = format!(
            "Jenkins build #{build} of snapshot `{sha}` ({place}) ended {} before the quality gate ran, so nothing \
             judged the change. Make it build first.",
            result.unwrap_or("None")
        );
        Ok(build_failed_report(&intro, tests.as_deref(), &console))
    }

    /// Quality round q of round n, as a quality file's intro names it.
    fn place(&self, n: u32, q: u32) -> String {
        format!("round {n}, quality round {q} of {}", self.state.max_quality_rounds)
    }

    /// The quality file of quality round q of round n, whose snapshot was not pushed for the secrets found.
    fn leak_report(&self, n: u32, q: u32, found: &LeaksFound) -> String {
        let intro = format!(
            "{} in the snapshot of {}, so it was not pushed and Jenkins did not judge the change. Take each secret \
             below out of the change; a comment telling gitleaks to allow its line counts as one too. If one is not a \
             secret, say so in your report: only the human can let it through, and not in this run, which keeps the \
             rules of its base: listed as `file:rule:line` in .gitleaksignore on the base branch, it passes in a run \
             started after that.",
            found.found(),
            self.place(n, q)
        );
        leak_report(&found.leaks, &intro)
    }

    /// The build's quality file with what the orchestrator's checks find in snapshot sha added (judge_run), their
    /// slow steps kept to the round's deadline, a monotonic time.
    ///
    /// The checks' in_time keeps the run's heartbeat going between their slow steps. A stop the heartbeat meets (a
    /// takeover, the Ctrl-C) lets the checks start no more slow steps, and is the error once they are done. Errors:
    /// that stop; git or a check fails.
    fn judged_by_orchestrator(&mut self, quality: &str, sha: &str, deadline: f64) -> Outcome<String> {
        let base = self
            .state
            .base
            .clone()
            .ok_or_else(|| Stopped::Failed(OrchestratorError("a quality round before the run has a base".into())))?;
        let pipeline = Rc::clone(&self.pipeline);
        // A workflow without a contract step has only its task to say what the change is for.
        let spec = Some(self.last_text(pipeline.contract(), 0)?).filter(|s| !s.is_empty());
        let spec = spec.unwrap_or_else(|| self.state.task.clone());
        let (host, work_dir) = (self.host, self.state.work_dir().to_string());
        let added = added_lines(&host.change_diff(&work_dir, &base, Some(sha))?)?;
        let changed = added.iter().map(|(path, lines)| (path.to_string(), lines.keys().copied().collect())).collect();
        let files = host.changed_line_counts(&work_dir, &base, sha)?;
        let file_at = |commit: &str, path: &str| host.file_at(&work_dir, commit, path);
        let now = self.clocks.wall();
        let checks = orchestrator_checks(&pipeline, self.indexes);
        let stopped: RefCell<Option<Stopped>> = RefCell::new(None);
        let me = RefCell::new(self);
        let in_time = || {
            if stopped.borrow().is_some() {
                return false;
            }
            let mut wf = me.borrow_mut();
            match wf.heartbeat() {
                Ok(()) => wf.clocks.monotonic() < deadline - SLOW_STEP_MARGIN as f64,
                Err(stop) => {
                    *stopped.borrow_mut() = Some(stop);
                    false
                }
            }
        };
        let change =
            RunChange { spec, base, commit: sha.to_string(), changed, files, file_at: &file_at, now, in_time: &in_time };
        let judged = judge_run(quality, &checks, &change);
        if let Some(stop) = stopped.take() {
            return Err(stop);
        }
        Ok(judged?)
    }

    /// Call poll every CI_POLL_SECONDS until it gives an answer, and return that, keeping the heartbeat between the
    /// calls. waiting says what is waited for, in the errors and the log.
    ///
    /// A failure that may pass, such as Jenkins being unreachable, is retried until the round's deadline, a
    /// monotonic time; any other fails the run at once. Either way RunState::ci keeps what is known so far. Errors:
    /// one that does not pass, named after waiting, or a refusal as it is; no answer by the deadline, naming the last
    /// failure; a takeover or the Ctrl-C.
    fn poll<T>(&mut self, waiting: &str, deadline: f64, mut poll: impl FnMut(&mut Self) -> Polled<T>) -> Outcome<T> {
        // The failure of the last call, while the calls fail in a way that may pass.
        let mut failing: Option<CIError> = None;
        loop {
            match poll(self) {
                Ok(answer) => {
                    if failing.take().is_some() {
                        self.human.log(&format!("[quality] {waiting}: answered again"));
                    }
                    if let Some(answer) = answer {
                        return Ok(answer);
                    }
                }
                Err(CIError::Refused(e)) => return Err(Stopped::Failed(e)),
                Err(e) if !e.transient() => {
                    return Err(Stopped::Failed(OrchestratorError(format!("quality gate: {waiting}: {e}"))));
                }
                Err(e) => {
                    if failing.is_none() {
                        self.human.log(&format!("[quality] {waiting}: {e}; retrying"));
                    }
                    failing = Some(e);
                }
            }
            if self.clocks.monotonic() >= deadline {
                let why = failing.map(|e| format!("; last error: {e}")).unwrap_or_default();
                return Err(Stopped::Failed(OrchestratorError(format!(
                    "quality gate: {waiting} did not finish within {QUALITY_TIMEOUT}s of the round's start{why}; resume \
                     to go on"
                ))));
            }
            self.heartbeat()?;
            self.pause(CI_POLL_SECONDS)?;
        }
    }

    /// call's answer, retried as poll retries; an answer of None counts here too.
    fn once<T>(&mut self, waiting: &str, deadline: f64, mut call: impl FnMut(&mut Self) -> Result<T, CIError>) -> Outcome<T> {
        self.poll(waiting, deadline, |wf| call(wf).map(Some))
    }

    /// After the analysis of git_ref: delete the ref on origin and forget it, as its build no longer needs it.
    fn finish_analysis(&mut self, git_ref: &str) -> Outcome<()> {
        if self.state.ci.get("ref").and_then(Json::as_str) == Some(git_ref) {
            // Jenkins has checked the ref out by now, though the build may still run the stages after its Gate stage
            // (analyse); the Jenkinsfile's Checkout comment says why that is enough. A leftover is only clutter, which
            // the run's end tries again.
            delete_ci_ref(self.host, self.human, &self.state.cwd, git_ref);
            self.state.ci = Map::new();
        }
        self.save()
    }

    /// At the end of a run that has or had the quality gate, delete its refs on origin, again for one whose gate
    /// resume turned off, in case deleting them then failed; a failure is only logged.
    ///
    /// Errors: the Ctrl-C, raised meanwhile, which keeps RunState::ci, as Python's KeyboardInterrupt in
    /// delete_ci_refs does, for a resume to find the build.
    pub(super) fn clean_up_quality(&mut self) -> Outcome<()> {
        if self.state.had_quality_gate() {
            let ci = self.state.ci.clone();
            delete_ci_refs(self.host, self.human, &mut self.state);
            if self.interrupt.is_raised() {
                self.state.ci = ci;
                return Err(Stopped::Interrupted);
            }
        }
        Ok(())
    }

    /// RunState::ci's field key, a string. Errors: it is missing or not a string, as no engine saves it.
    fn ci_text(&self, key: &str) -> Result<String, OrchestratorError> {
        match self.state.ci.get(key) {
            Some(Json::Str(text)) => Ok(text.clone()),
            other => Err(ci_error(key, other)),
        }
    }

    /// RunState::ci's build number. Errors: it is not an integer, as no engine saves it.
    fn ci_build(&self) -> Result<i64, OrchestratorError> {
        let build = self.state.ci.get("build");
        build.and_then(Json::as_int).and_then(|number| i64::try_from(number).ok()).ok_or_else(|| ci_error("build", build))
    }

    /// The builds RunState::ci records as rejected, which a resume never adopts; an entry that is no build number is
    /// none.
    fn ci_rejected(&self) -> Vec<i64> {
        let rejected = self.state.ci.get("rejected").and_then(Json::as_array).map_or(&[][..], Vec::as_slice);
        rejected.iter().filter_map(|number| number.as_int().and_then(|number| i64::try_from(number).ok())).collect()
    }
}

/// The error of a RunState::ci field, key, that is missing or of another type than the engines save.
fn ci_error(key: &str, value: Option<&Json>) -> OrchestratorError {
    let value = value.map_or("nothing".to_string(), Json::dumps);
    OrchestratorError(format!("state.json's ci holds {value} as its {key}, which no orchestrator saves"))
}

/// Nothing when the checkout at cwd has an origin, which the quality gate pushes snapshots to; an error saying so
/// otherwise, or when git fails.
pub(super) fn require_origin(host: &dyn Host, cwd: &str) -> Result<(), OrchestratorError> {
    if host.has_origin(cwd)? {
        return Ok(());
    }
    Err(OrchestratorError(format!("the quality gate pushes snapshots to {REMOTE}, and {cwd} has no {REMOTE}")))
}

/// Delete the run's quality snapshot refs on origin and forget its build in flight (RunState::ci); a failure is only
/// logged, to human.
pub(super) fn delete_ci_refs(host: &dyn Host, human: &dyn Human, state: &mut RunState) {
    let refs = host.remote_branches(&state.cwd, &format!("{CI_REF_PREFIX}{}-", state.key())).unwrap_or_else(|e| {
        human.log(&format!("could not list the {CI_REF_PREFIX} branches on {REMOTE}: {e}"));
        Vec::new()
    });
    for git_ref in refs {
        delete_ci_ref(host, human, &state.cwd, &git_ref);
    }
    state.ci = Map::new();
}

/// Delete the branch git_ref on cwd's origin; a failure is only logged, to human.
fn delete_ci_ref(host: &dyn Host, human: &dyn Human, cwd: &str, git_ref: &str) {
    if let Err(e) = host.delete_remote_branch(cwd, git_ref) {
        human.log(&format!("could not delete {git_ref} on {REMOTE}: {e}"));
    }
}
