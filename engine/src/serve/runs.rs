//! What serve gives of a project's runs, as orchestrator.py's ProjectRuns, plain_name, RunWatch and sse_event: the
//! run records `list --json` prints, each run's files and their text, and the events that tell a stream's client what
//! changed. Every path a client names is checked before anything is read.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

use crate::cli::listing::run_record;
use crate::constants::RUNS_DIR;
use crate::error::OrchestratorError;
use crate::host::Host;
use crate::py::json::{Json, Map};
use crate::py::text::repr;
use crate::runs::{local_host, pid_alive};

/// The keys of a run record that grow with every read of it while nothing happens; a change in them alone is no
/// event. The client ages heartbeat_age itself from when it got the record.
pub const AGING_RECORD_KEYS: [&str; 2] = ["heartbeat_age", "health_reason"];

/// Why serve did not give what a client asked of a run: Python's RunPathRefused, and the OrchestratorError it is a
/// kind of.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RunReadError {
    /// A run id or file name that is not one plain name in the run directory (plain_name), or that names
    /// state.json; nothing was read. serve answers it with 403.
    Refused(String),
    /// The project could not be read.
    Failed(OrchestratorError),
}

impl fmt::Display for RunReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RunReadError::Refused(why) => f.write_str(why),
            RunReadError::Failed(e) => e.fmt(f),
        }
    }
}

impl From<OrchestratorError> for RunReadError {
    fn from(e: OrchestratorError) -> RunReadError {
        RunReadError::Failed(e)
    }
}

/// name, if it names an entry of a directory itself: not empty, `.` or `..`, and without a slash or NUL. Errors:
/// RunReadError::Refused otherwise, naming it as Python's repr writes it.
pub fn plain_name(name: &str) -> Result<&str, RunReadError> {
    if matches!(name, "" | "." | "..") || name.contains(['/', '\0']) {
        return Err(RunReadError::Refused(format!("{} is not a name in the run directory", repr(name))));
    }
    Ok(name)
}

/// One project's runs and their files, as serve's endpoints give them.
pub struct ProjectRuns {
    /// Reaches the project; its commands are stopped when the server stops.
    host: Arc<dyn Host + Send + Sync>,
    /// The project's absolute directory on host.
    cwd: String,
    /// The --machine and --cwd flags as the client gave them (target_args), for the records' resume commands.
    target: Vec<String>,
    /// Whether a process of an id runs on this machine, as run_health asks it: crate::runs::pid_alive, but in tests.
    pid_alive: Arc<dyn Fn(i128) -> bool + Send + Sync>,
}

impl ProjectRuns {
    /// The project in directory cwd, absolute, on host; target is as the field says.
    pub fn new(host: Arc<dyn Host + Send + Sync>, cwd: String, target: Vec<String>) -> ProjectRuns {
        ProjectRuns { host, cwd, target, pid_alive: Arc::new(pid_alive) }
    }

    /// As new, a run's owner on this machine taken to be alive by pid_alive instead.
    #[cfg(test)]
    pub(crate) fn with_pid_alive(self, pid_alive: impl Fn(i128) -> bool + Send + Sync + 'static) -> ProjectRuns {
        ProjectRuns { pid_alive: Arc::new(pid_alive), ..self }
    }

    /// Whether the project is on another machine, read over SSH.
    pub fn remote(&self) -> bool {
        self.host.ssh_target().is_some()
    }

    /// Each run's record, as `list --json` prints them (run_record, this machine being the one serve runs on).
    /// Errors: the runs cannot be read.
    pub fn records(&self) -> Result<Vec<Map<Json>>, OrchestratorError> {
        let here = local_host();
        Ok(self
            .host
            .run_states(&self.cwd)?
            .iter()
            .map(|(age, s)| run_record(s, *age, &here, &*self.pid_alive, &self.target))
            .collect())
    }

    /// The run's files, oldest first, as a JSON list of {"name", "mtime"}, mtime in seconds since the epoch; [] for a
    /// run without a directory. Errors: Refused for a run id that is not a plain name, Failed when the directory
    /// cannot be listed.
    pub fn files(&self, run_id: &str) -> Result<Json, RunReadError> {
        let files = self.host.run_files(&self.dir(run_id)?)?;
        let entry = |path: &str, mtime: i64| {
            let name = path.rsplit('/').next().unwrap_or(path);
            Json::Object([("name", Json::from(name)), ("mtime", Json::from(mtime))].into_iter().collect())
        };
        Ok(Json::Array(files.iter().map(|f| entry(&f.path, f.mtime)).collect()))
    }

    /// The text of the run's file name; None when there is no such file. A symbolic link in the run directory is
    /// followed, as Python's Host.read follows it. Errors: Refused, having read nothing, for state.json or a run id or
    /// name that is not a plain name (the run id checked first); Failed when the file cannot be read.
    pub fn read(&self, run_id: &str, name: &str) -> Result<Option<String>, RunReadError> {
        if name == "state.json" {
            return Err(RunReadError::Refused("state.json is not a handoff file".to_string()));
        }
        let dir = self.dir(run_id)?;
        Ok(self.host.read(&format!("{dir}/{}", plain_name(name)?))?)
    }

    fn dir(&self, run_id: &str) -> Result<String, RunReadError> {
        Ok(format!("{}/{RUNS_DIR}/{}", self.cwd, plain_name(run_id)?))
    }
}

/// One event of a stream, as RunWatch::poll gives it.
#[derive(Clone, Debug, PartialEq)]
pub struct Event {
    /// "record", "files" or "problem", as RunWatch::poll describes them.
    pub kind: &'static str,
    /// What it tells, a JSON object.
    pub payload: Json,
}

impl Event {
    fn new(kind: &'static str, pairs: [(&str, Json); 2]) -> Event {
        Event { kind, payload: Json::Object(pairs.into_iter().collect()) }
    }

    /// The event as one Server-Sent Event of its kind, its payload the one line of JSON in its data field, as
    /// json.dumps writes it: Python's sse_event.
    pub fn sse(&self) -> String {
        format!("event: {}\ndata: {}\n\n", self.kind, self.payload.dumps())
    }
}

/// What one event stream has told its client about a project's runs, and the events that bring it up to date.
pub struct RunWatch<'p> {
    project: &'p ProjectRuns,
    /// By run id, the run's record without AGING_RECORD_KEYS and its files, as last sent.
    sent: BTreeMap<String, (Map<Json>, Json)>,
    /// The error the last problem event reported; None when there was none, or it cleared.
    problem: Option<String>,
}

impl<'p> RunWatch<'p> {
    /// A watch of project that has sent nothing yet.
    pub fn new(project: &'p ProjectRuns) -> RunWatch<'p> {
        RunWatch { project, sent: BTreeMap::new(), problem: None }
    }

    /// The events since the last poll, in order:
    ///
    /// - record, {"run", "record"}: the run's run_record, for a run that appeared or whose record changed other than
    ///   in AGING_RECORD_KEYS; record is null for a run whose state.json is gone, these last, by run id.
    /// - files, {"run", "files"}: the run's files, as ProjectRuns::files lists them, when they changed; after the
    ///   run's record event, if it has one.
    /// - problem, {"error"}: the project could not be read, and why; error is null once it can be again. First.
    ///
    /// The first poll sends every run's record and files. A finished or failed run's files are read again only when
    /// its record changes: nothing drives it. A poll that fails sends only its problem, and changes nothing else of
    /// what the watch holds.
    pub fn poll(&mut self) -> Vec<Event> {
        match self.changes() {
            Ok(events) => self.problem_events(None).into_iter().chain(events).collect(),
            Err(e) => self.problem_events(Some(e.to_string())),
        }
    }

    fn changes(&mut self) -> Result<Vec<Event>, RunReadError> {
        let mut events = vec![];
        let mut sent = BTreeMap::new();
        for record in self.project.records()? {
            // A state.json's run_id is a string; one that is not names the run as Python's str() writes it.
            let id = record.get("id").cloned().unwrap_or(Json::Null);
            let run_id = id.python_str();
            let mut key = record.clone();
            for aging in AGING_RECORD_KEYS {
                key.remove(aging);
            }
            let old = self.sent.get(&run_id);
            let same_record = old.is_some_and(|(old_key, _)| *old_key == key);
            let finished = record.get("health").is_none_or(Json::is_null);
            let files = match old {
                Some((_, old_files)) if same_record && finished => old_files.clone(),
                _ => self.project.files(&run_id)?,
            };
            if !same_record {
                events.push(Event::new("record", [("run", id.clone()), ("record", Json::Object(record))]));
            }
            if old.is_none_or(|(_, old_files)| *old_files != files) {
                events.push(Event::new("files", [("run", id), ("files", files.clone())]));
            }
            sent.insert(run_id, (key, files));
        }
        for gone in self.sent.keys().filter(|id| !sent.contains_key(*id)) {
            events.push(Event::new("record", [("run", Json::from(gone.as_str())), ("record", Json::Null)]));
        }
        self.sent = sent;
        Ok(events)
    }

    fn problem_events(&mut self, error: Option<String>) -> Vec<Event> {
        if error == self.problem {
            return vec![];
        }
        self.problem.clone_from(&error);
        vec![Event { kind: "problem", payload: Json::Object([("error", Json::from(error))].into_iter().collect()) }]
    }
}

#[cfg(test)]
mod tests;
