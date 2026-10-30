//! A world's shared record, `WORLD/fakes/`: the fakes' state and the event log, read and written under one lock by
//! every process of the world (the fakes, and the World in the test's process).

use std::collections::BTreeSet;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use nix::fcntl::{Flock, FlockArg};
use serde_json::{Map, Value};

use crate::dirs;
use crate::pyjson;

/// The ledger of the world in `world`.
///
/// The state is one JSON object, which `change` hands its caller to read and modify. Its keys, each created by the
/// first writer that needs it: engines (per invocation number, the pid of the process World started for it),
/// workspaces, panes and agents (herdr's), prs (gh's), consumed (per role, how many of its scripted actions have been
/// taken), held (per hold name, the action waiting for it), reached and released (hold names), last_states (per
/// state.json path, its text as last logged, heartbeat left out) and next (per kind of id, the next number).
pub struct Ledger {
    dir: PathBuf,
    world: PathBuf,
}

impl Ledger {
    /// The ledger of the world whose directory is world, whose `fakes/` directory must exist.
    pub fn new(world: &Path) -> Ledger {
        Ledger { dir: world.join("fakes"), world: world.to_path_buf() }
    }

    /// What f returns, given the state, locked against every other process of the world until f returns, then
    /// saved. Panics, failing the scenario or the fake, where the lock, the state or the save fails.
    pub fn change<T>(&self, f: impl FnOnce(&mut Map<String, Value>) -> T) -> T {
        let lock = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(self.dir.join("lock"))
            .unwrap_or_else(|e| panic!("opening the ledger's lock in {}: {e}", self.dir.display()));
        // Held until dropped, after the save.
        let lock = Flock::lock(lock, FlockArg::LockExclusive)
            .unwrap_or_else(|(_, e)| panic!("locking the ledger in {}: {e}", self.dir.display()));
        let path = self.dir.join("state.json");
        let mut state = match fs::read_to_string(&path) {
            Ok(text) => match serde_json::from_str(&text) {
                Ok(Value::Object(map)) => map,
                other => panic!("the ledger's {} is no JSON object: {other:?}", path.display()),
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Map::new(),
            Err(e) => panic!("reading {}: {e}", path.display()),
        };
        let out = f(&mut state);
        let tmp = self.dir.join("state.json.tmp");
        fs::write(&tmp, pyjson::dumps(&Value::Object(state))).unwrap_or_else(|e| panic!("writing {}: {e}", tmp.display()));
        fs::rename(&tmp, &path).unwrap_or_else(|e| panic!("replacing {}: {e}", path.display()));
        drop(lock);
        out
    }

    /// Append event, an object, to the log, by invocation `by`, after a `state` event for each run whose state.json
    /// changed since it was last logged and is the run of one of engine_pids or of none (see `changed_states`). Call
    /// inside `change`, whose state is `state`.
    pub fn log(&self, state: &mut Map<String, Value>, event: Map<String, Value>, by: &str, engine_pids: &BTreeSet<i64>) {
        let project = self.script()["project"].as_str().map(PathBuf::from).expect("the script's project");
        let mut lines: Vec<Value> = changed_states(state, &project, engine_pids)
            .into_iter()
            .map(|s| serde_json::json!({"by": by, "state": s}))
            .collect();
        let mut line = Map::new();
        line.insert("by".into(), by.into());
        line.extend(event);
        lines.push(Value::Object(line));
        let path = self.dir.join("events.jsonl");
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .unwrap_or_else(|e| panic!("opening {}: {e}", path.display()));
        let text: String = lines.iter().map(|l| pyjson::dumps(l) + "\n").collect();
        file.write_all(text.as_bytes()).unwrap_or_else(|e| panic!("appending to {}: {e}", path.display()));
    }

    /// Every event logged so far, in order.
    pub fn events(&self) -> Vec<Map<String, Value>> {
        let path = self.dir.join("events.jsonl");
        let text = match fs::read_to_string(&path) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Vec::new(),
            Err(e) => panic!("reading {}: {e}", path.display()),
        };
        text.lines()
            .map(|line| match serde_json::from_str(line) {
                Ok(Value::Object(event)) => event,
                other => panic!("an event of {} is no JSON object: {other:?}", path.display()),
            })
            .collect()
    }

    /// The scenario's script as `World` last wrote it.
    pub fn script(&self) -> Value {
        let path = self.world.join("script.json");
        let text = fs::read_to_string(&path).unwrap_or_else(|e| panic!("reading {}: {e}", path.display()));
        serde_json::from_str(&text).unwrap_or_else(|e| panic!("{} is no JSON: {e}", path.display()))
    }
}

/// The state.json of each run in project whose text changed since it was last logged, as {"path", "text"}, in the
/// order of their paths, recorded as logged in state's last_states.
///
/// A run counts only while engine_pids holds its owner's pid, or it has no owner: another engine process that talks
/// to the world meanwhile, as a taken-over run's old orchestrator does, sees state.json change at times only the
/// scheduler decides. heartbeat_at is left out of the comparison: it changes with time alone.
fn changed_states(state: &mut Map<String, Value>, project: &Path, engine_pids: &BTreeSet<i64>) -> Vec<Value> {
    let runs = project.join(".orchestrator").join("runs");
    let Some(entries) = dirs::entries(&runs) else {
        return Vec::new();
    };
    let last = state.entry("last_states").or_insert_with(|| Value::Object(Map::new()));
    let last = last.as_object_mut().expect("last_states is an object");
    let mut changed = Vec::new();
    for (_, dir) in entries {
        let path = dir.join("state.json");
        // A run directory without its state yet, or with one being written, has nothing to log.
        let text = match fs::read_to_string(&path) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => panic!("reading {}: {e}", path.display()),
        };
        let Ok(Value::Object(mut saved)) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        if let Some(Value::Object(owner)) = saved.get("owner") {
            if !owner.is_empty() && !owner.get("pid").and_then(Value::as_i64).is_some_and(|p| engine_pids.contains(&p)) {
                continue;
            }
        }
        saved.remove("heartbeat_at");
        let key = pyjson::dumps_sorted(&Value::Object(saved));
        let path = path.to_string_lossy().into_owned();
        if last.get(&path).and_then(Value::as_str) != Some(key.as_str()) {
            last.insert(path.clone(), key.into());
            changed.push(serde_json::json!({"path": path, "text": text}));
        }
    }
    changed
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A project of the test's own, named name, with one run, a, which has no state.json yet.
    fn project(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("octoflow-e2e-ledger-{name}-{}", std::process::id()));
        dirs::remove_dir(&dir);
        let runs = dir.join(".orchestrator").join("runs");
        fs::create_dir_all(runs.join("a")).expect("a run directory");
        dir
    }

    #[test]
    fn a_run_without_its_state_yet_has_none_to_log() {
        let dir = project("unsaved");
        let changed = changed_states(&mut Map::new(), &dir, &BTreeSet::new());
        dirs::remove_dir(&dir);
        assert!(changed.is_empty());
    }

    #[test]
    fn a_state_that_cannot_be_read_fails() {
        let dir = project("unreadable");
        fs::create_dir_all(dir.join(".orchestrator/runs/b/state.json")).expect("a state.json that is a directory");
        let read = std::panic::catch_unwind(|| changed_states(&mut Map::new(), &dir, &BTreeSet::new()));
        dirs::remove_dir(&dir);
        assert!(read.is_err(), "a state.json that is a directory was read");
    }
}
