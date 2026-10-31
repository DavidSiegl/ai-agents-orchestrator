//! A world's shared record, `WORLD/fakes/`: the fakes' state and the event log, which the fakes and the test's world
//! read and change under one lock, and the scenario's script, `WORLD/script.json`, which the world writes.

use std::collections::BTreeSet;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::thread::sleep;
use std::time::{Duration, Instant};

use nix::fcntl::{Flock, FlockArg};
use octoflow_engine::py::json::{Json, Map};

use crate::{object, object_at, procs};

/// The environment variable that names the engine invocation a fake serves, as the world numbers them from 1.
pub const INVOCATION_VAR: &str = "E2E_INVOCATION";
/// How long a fake waits for the world to record its invocation's process (Ledger::engine_pids).
const ENGINE_WAIT: Duration = Duration::from_secs(10);
/// How often a fake waiting on the ledger looks again.
pub const POLL: Duration = Duration::from_millis(50);

/// A world's ledger.
///
/// The state is one JSON object, which `change` hands its caller to read and modify. Its keys, each created by the
/// first writer that needs it: engines (per invocation number, the pid of the process the world started for it),
/// workspaces, panes and agents (herdr's), prs (gh's), consumed (per role, how many of its scripted actions have been
/// taken), held (per hold name, the action waiting for it), reached and released (hold names), last_states (per
/// state.json path, its text as last logged, heartbeat left out) and next (per kind of id, the next number).
pub struct Ledger {
    /// The world's directory.
    world: PathBuf,
}

impl Ledger {
    /// The ledger of the world in directory world, whose `fakes/` directory must exist.
    pub fn new(world: &Path) -> Ledger {
        Ledger { world: world.to_path_buf() }
    }

    fn path(&self, name: &str) -> PathBuf {
        self.world.join("fakes").join(name)
    }

    /// f's result, given the state, locked against every other process of the world until f returns; the state as f
    /// left it is then saved. Panics, naming the file, when the lock, the state's read or its write fails.
    pub fn change<T>(&self, f: impl FnOnce(&mut Map<Json>) -> T) -> T {
        let lock_path = self.path("lock");
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(&lock_path)
            .unwrap_or_else(|e| panic!("opening {}: {e}", lock_path.display()));
        let _lock = Flock::lock(file, FlockArg::LockExclusive)
            .unwrap_or_else(|(_, e)| panic!("locking {}: {e}", lock_path.display()));
        let path = self.path("state.json");
        let mut state = match fs::read_to_string(&path) {
            Ok(text) => match Json::parse(&text) {
                Ok(Json::Object(state)) => state,
                other => panic!("{} is not a JSON object: {other:?}", path.display()),
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Map::new(),
            Err(e) => panic!("reading {}: {e}", path.display()),
        };
        let result = f(&mut state);
        write_atomically(&path, &Json::Object(state).dumps());
        result
    }

    /// Append event to the log, by invocation `by`, after a `state` event for each run whose state.json changed since
    /// it was last logged and is the run of one of engine_pids or of none (changed_states). Call inside `change`,
    /// whose state is `state`.
    pub fn log(&self, state: &mut Map<Json>, event: Map<Json>, by: &str, engine_pids: &BTreeSet<i32>) {
        let project = crate::str_at(&self.script(), "project").to_string();
        let mut lines = String::new();
        for changed in changed_states(state, Path::new(&project), engine_pids) {
            lines.push_str(&object([("by", Json::from(by)), ("state", changed)]).dumps());
            lines.push('\n');
        }
        let mut line = Map::new();
        line.insert("by", Json::from(by));
        for (k, v) in event {
            line.insert(k, v);
        }
        lines.push_str(&Json::Object(line).dumps());
        lines.push('\n');
        let path = self.path("events.jsonl");
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .unwrap_or_else(|e| panic!("opening {}: {e}", path.display()));
        file.write_all(lines.as_bytes()).unwrap_or_else(|e| panic!("appending to {}: {e}", path.display()));
    }

    /// Every event logged so far, in order, each an object with its invocation under "by".
    pub fn events(&self) -> Vec<Json> {
        let path = self.path("events.jsonl");
        let text = match fs::read_to_string(&path) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Vec::new(),
            Err(e) => panic!("reading {}: {e}", path.display()),
        };
        text.lines().map(|line| Json::parse(line).unwrap_or_else(|e| panic!("{}: {e}", path.display()))).collect()
    }

    /// The scenario's script as the world last wrote it.
    pub fn script(&self) -> Json {
        let path = self.world.join("script.json");
        let text = fs::read_to_string(&path).unwrap_or_else(|e| panic!("reading {}: {e}", path.display()));
        Json::parse(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
    }

    /// The pids of this fake's engine invocation that are its ancestors: from the process the world started for the
    /// invocation (the state's "engines") down to this fake's parent, so the engine itself whether it runs this fake
    /// directly, through sh or through the fake ssh. Empty outside an invocation. The world records the started
    /// process just after starting it, which is waited for, up to ENGINE_WAIT.
    pub fn engine_pids(&self) -> BTreeSet<i32> {
        let n = invocation();
        let deadline = Instant::now() + ENGINE_WAIT;
        let root = loop {
            let root = self.change(|state| object_at(state, "engines").get(&n).and_then(Json::as_int));
            if root.is_some() || Instant::now() > deadline {
                break root;
            }
            sleep(POLL);
        };
        let chain = procs::ancestors();
        match root.and_then(|r| chain.iter().position(|&p| i128::from(p) == r)) {
            Some(i) => chain[..=i].iter().copied().collect(),
            None => BTreeSet::new(),
        }
    }
}

/// The engine invocation this process serves, as INVOCATION_VAR names it; "?" outside one.
pub fn invocation() -> String {
    std::env::var(INVOCATION_VAR).unwrap_or_else(|_| "?".to_string())
}

/// Write text to path through a file beside it, so that a reader finds either the old text or the new.
pub fn write_atomically(path: &Path, text: &str) {
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, text).unwrap_or_else(|e| panic!("writing {}: {e}", tmp.display()));
    fs::rename(&tmp, path).unwrap_or_else(|e| panic!("renaming {} to {}: {e}", tmp.display(), path.display()));
}

/// The state.json of each run in project whose text changed since it was last logged, as {"path", "text"}, in the
/// order of their paths; each is recorded as logged in state.
///
/// A run counts only while engine_pids holds its owner's pid, or it has no owner: another engine process that talks
/// to the world meanwhile, as a taken-over run's old orchestrator does, sees state.json change at times only the
/// scheduler decides. heartbeat_at is left out of the comparison: it changes with time alone.
fn changed_states(state: &mut Map<Json>, project: &Path, engine_pids: &BTreeSet<i32>) -> Vec<Json> {
    let runs = project.join(".orchestrator").join("runs");
    let mut names: Vec<String> = match fs::read_dir(&runs) {
        Ok(entries) => entries.flatten().filter_map(|e| e.file_name().into_string().ok()).collect(),
        Err(_) => return Vec::new(),
    };
    names.sort();
    let last = object_at(state, "last_states");
    let mut changed = Vec::new();
    for name in names {
        let path = runs.join(&name).join("state.json");
        // A run directory without its state yet, or one the engine is writing, is looked at by the next command.
        let Ok(text) = fs::read_to_string(&path) else { continue };
        let Ok(Json::Object(mut saved)) = Json::parse(&text) else { continue };
        if let Some(owner) = saved.get("owner").filter(|o| o.truthy()) {
            let pid = owner.get("pid").and_then(Json::as_int);
            if !pid.is_some_and(|pid| engine_pids.iter().any(|&p| i128::from(p) == pid)) {
                continue;
            }
        }
        saved.remove("heartbeat_at");
        let key = sorted(&Json::Object(saved)).dumps();
        let path = path.to_string_lossy().into_owned();
        if last.get(&path).and_then(Json::as_str) != Some(key.as_str()) {
            last.insert(path.clone(), Json::Str(key));
            changed.push(object([("path", Json::Str(path)), ("text", Json::Str(text))]));
        }
    }
    changed
}

/// json with every object's keys sorted, as json.dumps's sort_keys writes it.
fn sorted(json: &Json) -> Json {
    match json {
        Json::Object(map) => {
            let mut pairs: Vec<(&str, &Json)> = map.iter().collect();
            pairs.sort_by_key(|(k, _)| *k);
            Json::Object(pairs.into_iter().map(|(k, v)| (k, sorted(v))).collect())
        }
        Json::Array(items) => Json::Array(items.iter().map(sorted).collect()),
        other => other.clone(),
    }
}

/// Wait for f to hold under the ledger's lock, polling every POLL.
pub fn wait_until(ledger: &Ledger, mut f: impl FnMut(&mut Map<Json>) -> bool) {
    while !ledger.change(&mut f) {
        sleep(POLL);
    }
}
