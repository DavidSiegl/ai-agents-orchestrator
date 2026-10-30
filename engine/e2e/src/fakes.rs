//! The programs a world puts on the engine's PATH in place of herdr, gh and ssh: fake-herdr, fake-gh and fake-ssh,
//! each run as `fake-<program> WORLD ARGS...` by a shim of the program's name in the world's bin directory. Each plays
//! its part of the world from the scenario's script, `WORLD/script.json`, which `World` writes:
//!
//! - herdr keeps workspaces, panes and agents, and plays each role's agent from `script["agents"][role]`, one action
//!   per prompt (`perform` says what an action holds). `--machine M` is taken off the front and the machine must be
//!   one of `script["machines"]`.
//! - gh answers `pr create`, `pr view`, `pr edit`, `pr ready` and `auth status` from the pull requests it has opened,
//!   which `World::set_pr` changes between invocations; a head commit is read from the origin repository.
//! - ssh runs its command locally with sh, so that `--machine` reaches this machine's files through the engine's
//!   remote code.
//!
//! Every herdr and gh command, and each state.json a command finds changed, goes into the world's Ledger. Nothing here
//! links the engine, so that the fakes play their part for whichever engine the world drives.

use std::collections::BTreeSet;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::ledger::Ledger;
use crate::pyjson;

/// The environment variable that holds the number of the engine invocation a fake serves, as World numbers them.
pub const INVOCATION_VAR: &str = "E2E_INVOCATION";
/// The URL of pull request {n} as gh prints it once it has opened it.
const PR_URL: &str = "https://github.com/parity/project/pull/";
/// How often a fake waiting on a hold, or on World to record its invocation's process, looks again.
const HOLD_POLL: Duration = Duration::from_millis(50);
/// How long a fake waits for World to record its invocation's process (engine_pids).
const ENGINE_WAIT: Duration = Duration::from_secs(10);
/// herdr's version as `herdr --version` prints it; doctor needs 0.9 or later.
const HERDR_VERSION: &str = "herdr 0.9.4";
/// The harnesses an agent can run in, as herdr's --kind takes them: each has a current integration, and a CLI on PATH
/// that World puts there and nothing runs.
pub const AGENT_KINDS: [&str; 5] = ["claude", "codex", "gemini", "opencode", "pi"];

/// The engine invocation this process serves, as World numbers them in INVOCATION_VAR; "?" outside one.
fn invocation() -> String {
    std::env::var(INVOCATION_VAR).unwrap_or_else(|_| "?".into())
}

/// The pids of this fake's engine invocation that are its ancestors: from the process World started for the
/// invocation (the ledger's "engines") down to this fake's parent, so the engine itself whether it runs this fake
/// directly, through sh or through the fake ssh. Empty outside an invocation. World records the started process just
/// after starting it, which is waited for, briefly.
fn engine_pids(ledger: &Ledger) -> BTreeSet<i64> {
    let n = invocation();
    let deadline = Instant::now() + ENGINE_WAIT;
    let root = loop {
        let root = ledger.change(|state| state.get("engines").and_then(|e| e.get(&n)).and_then(Value::as_i64));
        if root.is_some() || Instant::now() > deadline {
            break root;
        }
        std::thread::sleep(HOLD_POLL);
    };
    let chain = ancestors();
    match root.and_then(|r| chain.iter().position(|&p| p == r)) {
        Some(i) => chain[..=i].iter().copied().collect(),
        None => BTreeSet::new(),
    }
}

/// The pids of this process's ancestors, its parent first, as /proc gives them.
fn ancestors() -> Vec<i64> {
    let mut chain = Vec::new();
    let mut pid = i64::from(std::os::unix::process::parent_id());
    while pid > 1 && !chain.contains(&pid) {
        chain.push(pid);
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat"))
            .unwrap_or_else(|e| panic!("reading /proc/{pid}/stat: {e}"));
        // The command name, field 2, is in parentheses and may hold spaces; the parent is the second field after it.
        pid = stat
            .rsplit_once(')')
            .and_then(|(_, rest)| rest.split_whitespace().nth(1))
            .and_then(|p| p.parse().ok())
            .unwrap_or_else(|| panic!("/proc/{pid}/stat names no parent: {stat}"));
    }
    chain
}

/// Answer one herdr command, args, as herdr would, logging it; the exit status.
pub fn herdr(ledger: &Ledger, args: &[String]) -> i32 {
    let script = ledger.script();
    let pids = engine_pids(ledger);
    let answer = ledger.change(|state| {
        ledger.log(state, event("herdr", json!(args)), &invocation(), &pids);
        perform_released(ledger, state);
        let mut command = args;
        if command.first().map(String::as_str) == Some("--machine") {
            let machine = &args[1];
            let known = script["machines"]
                .as_array()
                .is_some_and(|ms| ms.iter().any(|m| m["label"] == **machine || m["id"] == **machine));
            if !known {
                return Err(HerdrFailure::new("machine_not_found", format!("no machine {machine}")));
            }
            command = &args[2..];
        }
        let words: Vec<&str> = command.iter().map(String::as_str).collect();
        match words.as_slice() {
            ["--version", ..] => Ok(Answer::Text(HERDR_VERSION.into())),
            ["integration", "status", ..] => Ok(Answer::Text(
                AGENT_KINDS.map(|kind| format!("{kind}: current (v10) (/parity/{kind})")).join("\n"),
            )),
            ["machine", "list", "--json", ..] => Ok(Answer::Text(pyjson::dumps(&script["machines"]))),
            _ => herdr_command(ledger, state, &words).map(Answer::Result),
        }
    });
    match answer {
        Ok(Answer::Text(text)) => println!("{text}"),
        Ok(Answer::Result(result)) => println!("{}", pyjson::dumps(&json!({"result": result}))),
        Err(e) => {
            eprintln!("{}", pyjson::dumps(&json!({"error": {"code": e.code, "message": e.message}})));
            return 1;
        }
    }
    0
}

/// What herdr prints for a command that succeeds: a line of its own, or the `result` of an API-backed command.
enum Answer {
    Text(String),
    Result(Value),
}

/// A herdr error answer: herdr prints {"error": {"code", "message"}} and exits 1.
struct HerdrFailure {
    code: &'static str,
    message: String,
}

impl HerdrFailure {
    fn new(code: &'static str, message: String) -> HerdrFailure {
        HerdrFailure { code, message }
    }
}

/// An event of the ledger's log: what kind of command it was, and the command.
fn event(kind: &str, command: Value) -> Map<String, Value> {
    let mut event = Map::new();
    event.insert(kind.into(), command);
    event
}

/// The next number of kind in state, from 1.
fn next_id(state: &mut Map<String, Value>, kind: &str) -> i64 {
    let counters = object(state, "next");
    let n = counters.get(kind).and_then(Value::as_i64).unwrap_or(0) + 1;
    counters.insert(kind.into(), n.into());
    n
}

/// The object under key in state, made empty where there is none.
fn object<'a>(state: &'a mut Map<String, Value>, key: &str) -> &'a mut Map<String, Value> {
    state
        .entry(key)
        .or_insert_with(|| Value::Object(Map::new()))
        .as_object_mut()
        .unwrap_or_else(|| panic!("the ledger's {key} is no object"))
}

/// The list under key in state, made empty where there is none.
fn list<'a>(state: &'a mut Map<String, Value>, key: &str) -> &'a mut Vec<Value> {
    state
        .entry(key)
        .or_insert_with(|| Value::Array(Vec::new()))
        .as_array_mut()
        .unwrap_or_else(|| panic!("the ledger's {key} is no list"))
}

/// The value after flag in args; panics, failing the fake, where it has none.
fn flag<'a>(args: &[&'a str], flag: &str) -> &'a str {
    let i = args.iter().position(|a| *a == flag).unwrap_or_else(|| panic!("no {flag} in {args:?}"));
    args.get(i + 1).unwrap_or_else(|| panic!("nothing after {flag} in {args:?}"))
}

/// The `result` of one API-backed herdr command, its effects made in state; HerdrFailure for herdr's errors.
fn herdr_command(ledger: &Ledger, state: &mut Map<String, Value>, args: &[&str]) -> Result<Value, HerdrFailure> {
    let rest = args.get(2..).unwrap_or_default();
    let first = rest.first().copied().unwrap_or_default();
    match args.get(..2).unwrap_or(args) {
        ["workspace", "create"] => {
            let w = format!("w{}", next_id(state, "workspace"));
            let pane = format!("p{}", next_id(state, "pane"));
            object(state, "workspaces").insert(w.clone(), json!({"label": flag(rest, "--label"), "cwd": flag(rest, "--cwd")}));
            object(state, "panes").insert(pane.clone(), json!({"workspace": w, "label": ""}));
            Ok(json!({"workspace": {"workspace_id": w}, "root_pane": {"pane_id": pane}}))
        }
        ["workspace", "get"] => {
            if !object(state, "workspaces").contains_key(first) {
                return Err(HerdrFailure::new("workspace_not_found", format!("no workspace {first}")));
            }
            Ok(json!({"workspace": {"workspace_id": first}}))
        }
        ["workspace", "close"] => {
            if object(state, "workspaces").shift_remove(first).is_none() {
                return Err(HerdrFailure::new("workspace_not_found", format!("no workspace {first}")));
            }
            object(state, "panes").retain(|_, p| p["workspace"] != first);
            let panes: BTreeSet<String> = object(state, "panes").keys().cloned().collect();
            object(state, "agents").retain(|_, a| a["pane"].as_str().is_some_and(|p| panes.contains(p)));
            Ok(json!({}))
        }
        ["pane", "split"] => {
            let workspace = pane(state, first)?["workspace"].clone();
            let new = format!("p{}", next_id(state, "pane"));
            object(state, "panes").insert(new.clone(), json!({"workspace": workspace, "label": ""}));
            Ok(json!({"pane": {"pane_id": new}}))
        }
        ["pane", "rename"] => {
            pane(state, first)?["label"] = rest[1].into();
            Ok(json!({}))
        }
        ["pane", "get"] => {
            pane(state, first)?;
            Ok(json!({"pane": {"pane_id": first}}))
        }
        ["agent", "start"] => {
            let at = flag(rest, "--pane");
            pane(state, at)?;
            let session = format!("session-{}", next_id(state, "session"));
            let agent = json!({"pane": at, "kind": flag(rest, "--kind"), "status": "idle", "session": session});
            object(state, "agents").insert(first.into(), agent.clone());
            Ok(json!({"agent": agent_record(first, &agent)}))
        }
        ["agent", "get"] => {
            let agent = agent(state, first)?.clone();
            // The engine's first poll of an agent whose action is held: the engine is waiting for it now, and does
            // nothing but poll again until the hold is released.
            let holds: Vec<String> = object(state, "held")
                .iter()
                .filter(|(_, entry)| entry["name"] == first)
                .map(|(hold, _)| hold.clone())
                .collect();
            let reached = list(state, "reached");
            for hold in holds {
                if !reached.contains(&Value::from(hold.as_str())) {
                    reached.push(hold.into());
                }
            }
            Ok(json!({"agent": agent_record(first, &agent)}))
        }
        ["agent", "wait"] => {
            let agent = agent(state, first)?;
            let until: Vec<&str> = rest.windows(2).filter(|w| w[0] == "--until").map(|w| w[1]).collect();
            if !until.is_empty() && !until.iter().any(|u| agent["status"] == *u) {
                return Err(HerdrFailure::new("timeout", format!("agent {first} did not reach {}", until.join(", "))));
            }
            Ok(json!({"agent": agent_record(first, agent)}))
        }
        ["agent", "prompt"] => {
            agent(state, first)?;
            prompted(ledger, state, first);
            Ok(json!({}))
        }
        ["agent", "focus"] => {
            agent(state, first)?;
            Ok(json!({}))
        }
        ["notification", "show"] => Ok(json!({})),
        _ => Err(HerdrFailure::new("unknown_command", format!("the fake herdr has no command {}", args.join(" ")))),
    }
}

fn pane<'a>(state: &'a mut Map<String, Value>, pane: &str) -> Result<&'a mut Value, HerdrFailure> {
    object(state, "panes").get_mut(pane).ok_or_else(|| HerdrFailure::new("pane_not_found", format!("no pane {pane}")))
}

fn agent<'a>(state: &'a mut Map<String, Value>, name: &str) -> Result<&'a mut Value, HerdrFailure> {
    object(state, "agents").get_mut(name).ok_or_else(|| HerdrFailure::new("agent_not_found", format!("no agent {name}")))
}

fn agent_record(name: &str, agent: &Value) -> Value {
    json!({"name": name, "pane_id": agent["pane"], "agent_status": agent["status"],
           "agent_session": {"value": agent["session"]}})
}

/// The role and the run key of agent name, `<role>-<key>`.
fn role_and_key(name: &str) -> (&str, &str) {
    name.rsplit_once('-').unwrap_or_else(|| panic!("the agent {name} names no run"))
}

/// Play the role's next scripted action for the prompt just sent to agent name: performed now, or held until
/// World::release names its hold, the hold being reached at the engine's first `agent get` of the agent after it. A
/// prompt the script has no action for makes the agent exit, which the engine reports, rather than leave the scenario
/// waiting.
fn prompted(ledger: &Ledger, state: &mut Map<String, Value>, name: &str) {
    let role = role_and_key(name).0;
    let actions = ledger.script()["agents"][role].as_array().cloned().unwrap_or_default();
    let k = object(state, "consumed").get(role).and_then(Value::as_u64).unwrap_or(0) as usize;
    let Some(action) = actions.get(k) else {
        object(state, "agents").shift_remove(name);
        return;
    };
    object(state, "consumed").insert(role.into(), (k + 1).into());
    if let Some(hold) = action.get("hold").and_then(Value::as_str) {
        if !list(state, "released").contains(&Value::from(hold)) {
            object(state, "agents")[name]["status"] = "working".into();
            object(state, "held").insert(hold.into(), json!({"name": name, "action": action}));
            return;
        }
    }
    perform(ledger, state, name, action);
}

/// Perform each held action whose hold has been released since.
fn perform_released(ledger: &Ledger, state: &mut Map<String, Value>) {
    let released = list(state, "released").clone();
    let holds: Vec<String> = object(state, "held").keys().filter(|h| released.contains(&Value::from(h.as_str()))).cloned().collect();
    for hold in holds {
        let entry = object(state, "held").shift_remove(&hold).expect("a held action");
        let name = entry["name"].as_str().expect("a held action's agent");
        if object(state, "agents").contains_key(name) {
            perform(ledger, state, name, &entry["action"]);
        }
    }
}

/// Do what the agent name's action says, as its agent would in the turn the prompt starts.
///
/// An action is an object of any of: "edit", {path relative to the run's working directory (its worktree, else the
/// project): text, or null to delete the file}; "write", {path relative to the run directory: text}, written after the
/// edits, so that the handoff file, which ends the turn, comes last; "exit", true for an agent that exits afterwards;
/// "status", the agent's status afterwards, default idle; and "hold", which World::release takes.
fn perform(ledger: &Ledger, state: &mut Map<String, Value>, name: &str, action: &Value) {
    let project = PathBuf::from(ledger.script()["project"].as_str().expect("the script's project"));
    let run_dir = run_dir(&project, role_and_key(name).1);
    let saved_path = run_dir.join("state.json");
    let saved: Value = std::fs::read_to_string(&saved_path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| panic!("no state in {}", saved_path.display()));
    let work_dir = match saved.get("worktree").and_then(Value::as_str).filter(|w| !w.is_empty()) {
        Some(worktree) => PathBuf::from(worktree),
        None => PathBuf::from(saved["cwd"].as_str().expect("the run's cwd")),
    };
    let files = |key: &str| action.get(key).and_then(Value::as_object).cloned().unwrap_or_default();
    for (path, text) in files("edit") {
        put(&work_dir.join(path), text.as_str());
    }
    for (path, text) in files("write") {
        put(&run_dir.join(path), text.as_str());
    }
    let status = action.get("status").cloned().unwrap_or_else(|| "idle".into());
    object(state, "agents")[name]["status"] = status;
    if action.get("exit").and_then(Value::as_bool) == Some(true) {
        object(state, "agents").shift_remove(name);
    }
}

/// The run directory of the run whose key is key.
fn run_dir(project: &Path, key: &str) -> PathBuf {
    let runs = project.join(".orchestrator").join("runs");
    let found: Vec<PathBuf> = std::fs::read_dir(&runs)
        .unwrap_or_else(|e| panic!("listing {}: {e}", runs.display()))
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().ends_with(&format!("-{key}")))
        .map(|e| e.path())
        .collect();
    match found.as_slice() {
        [one] => one.clone(),
        _ => panic!("no single run directory for key {key} in {}: {found:?}", runs.display()),
    }
}

/// Write text to path, making its directories; delete path for None.
fn put(path: &Path, text: Option<&str>) {
    let Some(text) = text else {
        std::fs::remove_file(path).unwrap_or_else(|e| panic!("removing {}: {e}", path.display()));
        return;
    };
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).unwrap_or_else(|e| panic!("making {}: {e}", dir.display()));
    }
    std::fs::write(path, text).unwrap_or_else(|e| panic!("writing {}: {e}", path.display()));
}

/// Answer one gh command, args, as gh would, logging it with its standard input; the exit status.
pub fn gh(ledger: &Ledger, args: &[String]) -> i32 {
    let stdin = args.iter().any(|a| a == "--body-file").then(|| {
        let mut text = String::new();
        std::io::stdin().read_to_string(&mut text).expect("gh's standard input");
        text
    });
    let pids = engine_pids(ledger);
    let words: Vec<&str> = args.iter().map(String::as_str).collect();
    ledger.change(|state| {
        let mut logged = event("gh", json!(args));
        if let Some(stdin) = &stdin {
            logged.insert("stdin".into(), stdin.as_str().into());
        }
        ledger.log(state, logged, &invocation(), &pids);
    });
    if words.get(..2) == Some(&["auth", "status"]) {
        println!("github.com\n  Logged in to github.com account parity");
        return 0;
    }
    let origin = ledger.script()["origin"].as_str().map(PathBuf::from).expect("the script's origin");
    ledger.change(|state| {
        let command = words.get(..2).unwrap_or(&words);
        let n = if command == ["pr", "create"] { next_id(state, "pr") } else { 0 };
        let prs = object(state, "prs");
        match command {
            ["pr", "create"] => {
                prs.insert(
                    n.to_string(),
                    json!({"base": flag(&words, "--base"), "head": flag(&words, "--head"),
                           "title": flag(&words, "--title"), "body": stdin, "isDraft": words.contains(&"--draft"),
                           "state": "OPEN", "closedAt": null, "mergeable": "MERGEABLE"}),
                );
                println!("{PR_URL}{n}");
                0
            }
            ["pr", what @ ("view" | "edit" | "ready")] => {
                let number = words[2].trim_end_matches('/').rsplit('/').next().unwrap_or_default();
                let Some(pr) = prs.get_mut(number) else {
                    eprintln!("no pull requests found for {}", words[2]);
                    return 1;
                };
                match *what {
                    "edit" => pr["body"] = stdin.clone().into(),
                    "ready" => pr["isDraft"] = false.into(),
                    _ => {
                        let mut view = pr.as_object().cloned().unwrap_or_default();
                        let head = pr["head"].as_str().unwrap_or_default();
                        view.insert("headRefOid".into(), origin_commit(&origin, head).into());
                        let fields: Map<String, Value> = flag(&words, "--json")
                            .split(',')
                            .map(|k| (k.to_string(), view.get(k).cloned().unwrap_or(Value::Null)))
                            .collect();
                        println!("{}", pyjson::dumps(&Value::Object(fields)));
                    }
                }
                0
            }
            _ => {
                eprintln!("the fake gh has no command {}", words.join(" "));
                1
            }
        }
    })
}

/// The commit of branch in the origin repository, or 40 zeros when it has none: gh still names the head that the pull
/// request last had.
fn origin_commit(origin: &Path, branch: &str) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(origin)
        .args(["rev-parse", "--verify", "--quiet", &format!("refs/heads/{branch}")])
        .stderr(Stdio::null())
        .output()
        .unwrap_or_else(|e| panic!("running git rev-parse in {}: {e}", origin.display()));
    let sha = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if sha.is_empty() {
        "0".repeat(40)
    } else {
        sha
    }
}

/// Run ssh's command, the arguments after its options and target joined by spaces as ssh joins them, with sh on this
/// machine; its exit status.
pub fn ssh(args: &[String]) -> i32 {
    let mut i = 0;
    while args.get(i).is_some_and(|a| a.starts_with('-')) {
        i += if ["-o", "-p", "-i", "-l", "-F"].contains(&args[i].as_str()) { 2 } else { 1 };
    }
    // The command the engine sent, run as sshd's login shell would run it.
    let command = args.get(i + 1..).unwrap_or_default().join(" ");
    let status = Command::new("sh").args(["-c", &command]).status().unwrap_or_else(|e| panic!("running sh: {e}"));
    status.code().unwrap_or(255)
}

/// The world's directory and the program's arguments, from a fake's own: `fake-<program> WORLD ARGS...`.
pub fn world_and_args() -> (Ledger, Vec<String>) {
    let mut args = std::env::args().skip(1);
    let world = args.next().expect("usage: fake-<program> WORLD ARGS...");
    (Ledger::new(Path::new(&world)), args.collect())
}
