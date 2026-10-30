//! The Python tests' ServeHost: a Host that serve's threads can share, its files in memory; and FakeConsole, the herdr
//! its runs' agents are in.

use std::collections::BTreeMap;
use std::sync::{Mutex, MutexGuard};

use crate::constants::RUNS_DIR;
use crate::error::OrchestratorError;
use crate::herdr::{AgentConsole, HerdrError};
use crate::host::{Exec, ExecError, Host, RunFile};
use crate::py::json::Json;

/// A Host whose files are `files`, whose run_files lists those under a run directory by their `mtimes`, as the real
/// Host's does, and which records the path of every read in `reads`. It runs no command.
#[derive(Default)]
pub struct ServeHost {
    /// The SSH target it stands for; None for this machine.
    pub ssh_target: Option<String>,
    state: Mutex<Files>,
}

/// What a ServeHost holds, changed by the tests while a server reads it.
#[derive(Default)]
pub struct Files {
    /// By path, each file's text.
    pub files: BTreeMap<String, String>,
    /// By path, a file's mtime; 0 for a file not here.
    pub mtimes: BTreeMap<String, i64>,
    /// By run id, the seconds since its state.json was written; 0 for a run not here.
    pub ages: BTreeMap<String, u64>,
    /// The path of each read, in order.
    pub reads: Vec<String>,
    /// While set, what run_states fails with.
    pub run_states_error: Option<String>,
    /// How many times run_states was called: a poll of the project each.
    pub run_states_calls: usize,
}

impl ServeHost {
    /// One for a project on ssh_target, None for this machine.
    pub fn on(ssh_target: Option<&str>) -> ServeHost {
        ServeHost { ssh_target: ssh_target.map(String::from), ..ServeHost::default() }
    }

    /// Its files, to read or change.
    pub fn state(&self) -> MutexGuard<'_, Files> {
        self.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl Host for ServeHost {
    fn ssh_target(&self) -> Option<&str> {
        self.ssh_target.as_deref()
    }

    fn exec(&self, argv: &[&str], _stdin: Option<&[u8]>, _timeout: u64) -> Result<Exec, ExecError> {
        panic!("ServeHost runs no command, asked for {argv:?}")
    }

    fn read(&self, path: &str) -> Result<Option<String>, OrchestratorError> {
        let mut s = self.state();
        s.reads.push(path.to_string());
        Ok(s.files.get(path).cloned())
    }

    fn run_states(&self, cwd: &str) -> Result<Vec<(u64, Json)>, OrchestratorError> {
        let mut s = self.state();
        s.run_states_calls += 1;
        if let Some(e) = &s.run_states_error {
            return Err(OrchestratorError(e.clone()));
        }
        let runs = format!("{cwd}/{RUNS_DIR}/");
        let mut states = vec![];
        for (path, text) in &s.files {
            if let Some((run_id, "state.json")) = path.strip_prefix(&runs).and_then(|rest| rest.split_once('/')) {
                states.push((s.ages.get(run_id).copied().unwrap_or(0), Json::parse(text).map_err(OrchestratorError)?));
            }
        }
        Ok(states)
    }

    fn run_files(&self, run_dir: &str) -> Result<Vec<RunFile>, OrchestratorError> {
        let s = self.state();
        let mut files: Vec<(i64, String)> = s
            .files
            .keys()
            .filter(|p| p.starts_with(&format!("{run_dir}/")) && **p != format!("{run_dir}/state.json"))
            .map(|p| (s.mtimes.get(p).copied().unwrap_or(0), p.clone()))
            .collect();
        files.sort();
        Ok(files.into_iter().map(|(mtime, path)| RunFile { path, mtime }).collect())
    }
}

/// An AgentConsole whose agents show `screens`, by herdr name, and which records what is typed into them in `typed`.
/// An agent without a screen has exited (herdr's agent_not_found).
#[derive(Default)]
pub struct FakeConsole {
    state: Mutex<Consoled>,
}

/// What a FakeConsole holds and records.
#[derive(Default)]
pub struct Consoled {
    /// By agent name, what its terminal shows.
    pub screens: BTreeMap<String, String>,
    /// Each command, as `read NAME LINES`, `submit NAME TEXT` or `keys NAME KEY...`, in order.
    pub typed: Vec<String>,
    /// While true, every command fails as herdr does when it cannot be reached (still recorded in `typed`).
    pub down: bool,
}

impl FakeConsole {
    /// What the console holds and has recorded, to set its screens or `down` and to read `typed`; the console waits
    /// for its commands until the guard is dropped.
    pub fn state(&self) -> MutexGuard<'_, Consoled> {
        self.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// The agent's screen, after recording command; herdr's errors as the type says.
    fn answer(&self, name: &str, command: String) -> Result<String, HerdrError> {
        let mut s = self.state();
        s.typed.push(command);
        if s.down {
            return Err(HerdrError::Failed(OrchestratorError::new("herdr is not on PATH")));
        }
        s.screens.get(name).cloned().ok_or_else(|| HerdrError::new("agent_not_found", format!("no agent {name}")))
    }
}

impl AgentConsole for FakeConsole {
    fn read(&self, name: &str, lines: u32) -> Result<String, HerdrError> {
        self.answer(name, format!("read {name} {lines}"))
    }

    fn submit(&self, name: &str, text: &str) -> Result<(), HerdrError> {
        self.answer(name, format!("submit {name} {text}")).map(drop)
    }

    fn send_keys(&self, name: &str, keys: &[&str]) -> Result<(), HerdrError> {
        self.answer(name, format!("keys {name} {}", keys.join(" "))).map(drop)
    }
}
