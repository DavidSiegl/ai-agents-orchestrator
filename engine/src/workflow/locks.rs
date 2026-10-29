//! Locked files (Step::locks): the locking step's files held as it left them, restored after every other editing
//! turn that changes them, and the verdict step told what was restored.

use std::collections::BTreeSet;

use super::{Outcome, Stopped, Workflow};
use crate::error::OrchestratorError;
use crate::pipeline::Step;
use crate::prompts::Prompt;
use crate::py::json::{Json, Map};
use crate::py::text::{basename, splitext};

/// What one editing turn changed in other steps' locked files, as a RunState::restored record holds it.
#[derive(Clone, Debug, PartialEq)]
struct Restored {
    /// The round of the turn.
    round: u32,
    /// The basename of the diff of those changes, in the run directory.
    file: String,
    /// The paths the turn changed, relative to the repository's top level, by the id of the step that locked them, in
    /// the order of RunState::locked.
    paths: Map<Vec<String>>,
}

impl Restored {
    /// The record's turn. Errors: a field missing or of another type than the engines write, as no engine's
    /// state.json has, where the Python engine fails on a KeyError or a TypeError.
    fn read(record: &Map<Json>) -> Result<Restored, OrchestratorError> {
        let field = |key: &str| {
            record.get(key).ok_or_else(|| OrchestratorError(format!("a restored record in state.json has no {key}")))
        };
        let invalid = |key: &str, value: &Json, what: &str| {
            OrchestratorError(format!("a restored record in state.json has {key} {}, not {what}", value.dumps()))
        };
        let round = field("round")?;
        let round = round.as_int().and_then(|n| u32::try_from(n).ok()).ok_or_else(|| invalid("round", round, "a round"))?;
        let file = field("file")?;
        let file = file.as_str().ok_or_else(|| invalid("file", file, "a string"))?.to_string();
        let paths = field("paths")?;
        let lists = paths.as_object().and_then(|lockers| {
            lockers
                .iter()
                .map(|(locker, paths)| {
                    let paths = paths.as_array()?.iter().map(|p| p.as_str().map(String::from)).collect::<Option<_>>()?;
                    Some((locker.to_string(), paths))
                })
                .collect::<Option<Map<Vec<String>>>>()
        });
        let paths = lists.ok_or_else(|| invalid("paths", paths, "lists of paths by step"))?;
        Ok(Restored { round, file, paths })
    }

    /// The turn as a RunState::restored record, with its fields in the order the Python engine writes them.
    fn record(&self) -> Map<Json> {
        let paths = self.paths.iter().map(|(locker, paths)| {
            (locker, Json::Array(paths.iter().map(|p| Json::from(p.as_str())).collect()))
        });
        [
            ("round", Json::from(i64::from(self.round))),
            ("file", Json::from(self.file.as_str())),
            ("paths", Json::Object(paths.collect())),
        ]
        .into_iter()
        .collect()
    }
}

impl Workflow<'_> {
    /// After the editing step's turn in round n: restore the files other steps locked that the turn changed, then lock
    /// the step's own files if it locks them. A resume before the run moves on does it again, harmlessly.
    ///
    /// Errors: git fails, saving fails, or the step locks before the run has a base, which goto rules out.
    pub(super) fn hold_locks(&mut self, step: &Step, n: u32) -> Outcome<()> {
        let file = format!("locked-{}.diff", splitext(basename(&self.own_path(step, n))).0);
        let saved = self.restored()?.into_iter().find(|r| r.file == file);
        let record = match saved {
            Some(record) => Some(record),
            None => self.locked_changes(step, n, &file)?,
        };
        let work_dir = self.state.work_dir().to_string();
        // A saved record is restored again: an orchestrator may have stopped before or while restoring it.
        for (locker, paths) in record.iter().flat_map(|r| r.paths.iter()) {
            self.host.restore(&work_dir, &self.state.lock_ref(locker), paths)?;
            self.human.log(&format!(
                "[{}] restored {}, which step {locker} locked; the changes are in {file}",
                step.role,
                paths.join(", ")
            ));
        }
        if step.locks {
            let Some(base) = self.state.base.clone() else {
                return Err(Stopped::Failed(OrchestratorError("a lock before the run has a base".into())));
            };
            let locked = self.host.lock(&work_dir, &base, &self.state.lock_ref(&step.id))?;
            let count = locked.len();
            self.state.locked.insert(step.id.clone(), locked);
            self.save()?;
            self.human.log(&format!("[{}] locked {count} files for the steps after it", step.role));
        }
        Ok(())
    }

    /// The record of what the step's turn in round n changed in other steps' locked files, saved with its diff
    /// written to file in the run directory; None when it changed none of them.
    fn locked_changes(&mut self, step: &Step, n: u32, file: &str) -> Outcome<Option<Restored>> {
        let s = &self.state;
        let mut paths = Map::new();
        let mut diffs = String::new();
        for (locker, locked) in s.locked.iter().filter(|(locker, _)| *locker != step.id) {
            let (differ, diff) = self.host.locked_changes(s.work_dir(), &s.lock_ref(locker), locked)?;
            if !differ.is_empty() {
                paths.insert(locker, differ);
                diffs.push_str(&diff);
            }
        }
        if paths.is_empty() {
            return Ok(None);
        }
        self.host.write(&format!("{}/{file}", s.dir()), &diffs, false)?;
        let record = Restored { round: n, file: file.to_string(), paths };
        self.state.restored.push(record.record());
        self.save()?;
        Ok(Some(record))
    }

    /// What the verdict step hears of the locked files restored in round n: LOCKED_NOTE after a blank line; "" when
    /// none was. Errors: a restored record is malformed (Restored::read), or the prompt cannot be filled in.
    pub(super) fn locks_note(&self, n: u32) -> Result<String, OrchestratorError> {
        let records: Vec<Restored> = self.restored()?.into_iter().filter(|r| r.round == n).collect();
        if records.is_empty() {
            return Ok(String::new());
        }
        let paths: BTreeSet<&str> =
            records.iter().flat_map(|r| r.paths.values().flatten().map(String::as_str)).collect();
        let lockers: BTreeSet<&str> = records.iter().flat_map(|r| r.paths.keys()).collect();
        let paths: Vec<String> = paths.iter().map(|p| format!("`{p}`")).collect();
        let dir = self.state.dir();
        let diff_paths: Vec<String> = records.iter().map(|r| format!("{dir}/{}", r.file)).collect();
        let lockers: Vec<&str> = lockers.into_iter().collect();
        let note = self.fill(Prompt::LockedNote, &[
            ("paths", &paths.join(", ")),
            ("steps", &lockers.join(", ")),
            ("diff_paths", &diff_paths.join(", ")),
        ])?;
        Ok(format!("\n\n{note}"))
    }

    /// At the end of a run, delete its local refs; a failure is only logged.
    pub(super) fn clean_up_locks(&self) {
        let s = &self.state;
        if s.locked.is_empty() {
            return;
        }
        let prefix = s.refs_prefix();
        if let Err(e) = self.host.delete_refs(&s.cwd, &prefix) {
            self.human.log(&format!("could not delete the refs under {prefix}: {e}"));
        }
    }

    /// RunState::restored, read. Errors: as Restored::read's.
    fn restored(&self) -> Result<Vec<Restored>, OrchestratorError> {
        self.state.restored.iter().map(Restored::read).collect()
    }
}
