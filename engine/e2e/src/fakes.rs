//! The programs a world puts on the engine's PATH in place of herdr, gh and ssh (`src/bin/`), each run as
//! `fake-PROGRAM WORLD ARGS...` by a shim of the program's name in the world's `bin/`. Each plays its part of the
//! world from the scenario's script (Ledger::script):
//!
//! - herdr keeps workspaces, panes and agents, and plays each role's agent from `script["agents"][role]`, one action
//!   per prompt (`perform` says what an action holds). `--machine M` is taken off the front, and M must be one of
//!   `script["machines"]`.
//! - gh answers `pr create`, `pr view`, `pr edit`, `pr ready` and `auth status` from the pull requests it has opened,
//!   which the world changes between invocations; a head commit is read from the origin repository.
//! - ssh runs its command here with sh, so that `--machine` reaches this machine's files through the engine's remote
//!   code.
//!
//! Every herdr and gh command, and each state.json a command finds changed, goes into the ledger. gitleaks is the
//! real one.

use std::collections::BTreeSet;
use std::fs;
use std::io::Read;
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::Command;

use octoflow_engine::py::json::{Json, Map};

use crate::ledger::{invocation, wait_until, Ledger};
use crate::{array_at, holds, object, object_at, run_dir, str_at, strings};

/// The URL of pull request n as gh prints it once it has opened it.
fn pr_url(n: i128) -> String {
    format!("https://github.com/parity/project/pull/{n}")
}
/// herdr's version as `herdr --version` prints it; doctor needs 0.9 or later.
const HERDR_VERSION: &str = "herdr 0.9.4";
/// The harnesses an agent can run in, as herdr's --kind takes them: each has a current integration, and a CLI on
/// PATH that the world puts there and nothing runs.
pub const AGENT_KINDS: [&str; 5] = ["claude", "codex", "gemini", "opencode", "pi"];

/// A herdr error answer, which herdr prints as {"error": {"code", "message"}} before exiting 1.
struct HerdrFailure {
    code: &'static str,
    message: String,
}

fn failure(code: &'static str, message: String) -> HerdrFailure {
    HerdrFailure { code, message }
}

/// Answer one herdr command as herdr would, logging it; returns the exit status.
pub fn herdr(ledger: &Ledger, args: &[String]) -> i32 {
    let script = ledger.script();
    let pids = ledger.engine_pids();
    let answer = ledger.change(|state| {
        ledger.log(state, Map::from_iter([("herdr", strings(args))]), &invocation(), &pids);
        perform_released(ledger, state);
        let mut command = args;
        if args.first().map(String::as_str) == Some("--machine") {
            let machines = script.get("machines").and_then(Json::as_array).cloned().unwrap_or_default();
            let named = |m: &Json| [str_at(m, "label"), str_at(m, "id")].contains(&args[1].as_str());
            if !machines.iter().any(named) {
                return Err(failure("machine_not_found", format!("no machine {}", args[1])));
            }
            command = &args[2..];
        }
        let words: Vec<&str> = command.iter().map(String::as_str).collect();
        if words.first() == Some(&"--version") {
            return Ok(HERDR_VERSION.to_string());
        }
        if words.starts_with(&["integration", "status"]) {
            let lines: Vec<String> = AGENT_KINDS.iter().map(|k| format!("{k}: current (v10) (/parity/{k})")).collect();
            return Ok(lines.join("\n"));
        }
        if words.starts_with(&["machine", "list", "--json"]) {
            return Ok(script.get("machines").cloned().unwrap_or(Json::Array(Vec::new())).dumps());
        }
        herdr_command(ledger, state, &words).map(|reply| object([("result", reply)]).dumps())
    });
    match answer {
        Ok(out) => {
            println!("{out}");
            0
        }
        Err(e) => {
            let error = object([("code", Json::from(e.code)), ("message", Json::Str(e.message))]);
            eprintln!("{}", object([("error", error)]).dumps());
            1
        }
    }
}

/// The next number of kind, from 1.
fn next_id(state: &mut Map<Json>, kind: &str) -> i128 {
    let counters = object_at(state, "next");
    let n = counters.get(kind).and_then(Json::as_int).unwrap_or(0) + 1;
    counters.insert(kind, Json::Int(n));
    n
}

/// The value after flag in args; panics, failing the fake, when args has no such flag.
fn flag<'a>(args: &[&'a str], flag: &str) -> &'a str {
    let i = args.iter().position(|a| *a == flag).unwrap_or_else(|| panic!("no {flag} in {args:?}"));
    args[i + 1]
}

/// The `result` of one API-backed herdr command, its effects made in state.
fn herdr_command(ledger: &Ledger, state: &mut Map<Json>, args: &[&str]) -> Result<Json, HerdrFailure> {
    match args.first().copied().unwrap_or_default() {
        "workspace" => workspace_command(state, args),
        "pane" => pane_command(state, args),
        "agent" => agent_command(ledger, state, args),
        "notification" if args.get(1) == Some(&"show") => Ok(object([])),
        _ => Err(unknown(args)),
    }
}

/// The verb of a herdr command, its second word, and the words after it.
fn verb_and_rest<'a, 'b>(args: &'a [&'b str]) -> (&'b str, &'a [&'b str]) {
    (args.get(1).copied().unwrap_or_default(), args.get(2..).unwrap_or_default())
}

fn unknown(args: &[&str]) -> HerdrFailure {
    failure("unknown_command", format!("the fake herdr has no command {}", args.join(" ")))
}

/// herdr's `workspace VERB REST...`, args being the whole command.
fn workspace_command(state: &mut Map<Json>, args: &[&str]) -> Result<Json, HerdrFailure> {
    let (verb, rest) = verb_and_rest(args);
    let first = rest.first().copied().unwrap_or_default();
    match verb {
        "create" => {
            let w = format!("w{}", next_id(state, "workspace"));
            let pane = format!("p{}", next_id(state, "pane"));
            let label = Json::from(flag(rest, "--label"));
            let cwd = Json::from(flag(rest, "--cwd"));
            object_at(state, "workspaces").insert(w.clone(), object([("label", label), ("cwd", cwd)]));
            let entry = object([("workspace", Json::Str(w.clone())), ("label", Json::from(""))]);
            object_at(state, "panes").insert(pane.clone(), entry);
            Ok(object([
                ("workspace", object([("workspace_id", Json::Str(w))])),
                ("root_pane", object([("pane_id", Json::Str(pane))])),
            ]))
        }
        "get" if object_at(state, "workspaces").contains_key(first) => {
            Ok(object([("workspace", object([("workspace_id", Json::from(first))]))]))
        }
        "close" if object_at(state, "workspaces").remove(first).is_some() => {
            close_panes(state, first);
            Ok(object([]))
        }
        "get" | "close" => Err(failure("workspace_not_found", format!("no workspace {first}"))),
        _ => Err(unknown(args)),
    }
}

/// Remove workspace's panes, and the agents in them, which exit with their panes.
fn close_panes(state: &mut Map<Json>, workspace: &str) {
    let panes = object_at(state, "panes");
    let gone: Vec<String> = panes
        .iter()
        .filter(|(_, p)| p.get("workspace").and_then(Json::as_str) == Some(workspace))
        .map(|(k, _)| k.to_string())
        .collect();
    for p in &gone {
        panes.remove(p);
    }
    let live: BTreeSet<String> = panes.keys().map(str::to_string).collect();
    let agents = object_at(state, "agents");
    let exited: Vec<String> =
        agents.iter().filter(|(_, a)| !live.contains(str_at(a, "pane"))).map(|(k, _)| k.to_string()).collect();
    for name in &exited {
        agents.remove(name);
    }
}

/// herdr's `pane VERB REST...`, args being the whole command.
fn pane_command(state: &mut Map<Json>, args: &[&str]) -> Result<Json, HerdrFailure> {
    let (verb, rest) = verb_and_rest(args);
    let first = rest.first().copied().unwrap_or_default();
    match verb {
        "split" => {
            let workspace = pane(state, first)?.get("workspace").cloned().unwrap_or(Json::Null);
            let p = format!("p{}", next_id(state, "pane"));
            object_at(state, "panes").insert(p.clone(), object([("workspace", workspace), ("label", Json::from(""))]));
            Ok(object([("pane", object([("pane_id", Json::Str(p))]))]))
        }
        "rename" => {
            let label = Json::from(rest.get(1).copied().unwrap_or_default());
            match pane(state, first)? {
                Json::Object(p) => p.insert("label", label),
                other => panic!("pane {first} is {other:?}"),
            }
            Ok(object([]))
        }
        "get" => {
            pane(state, first)?;
            Ok(object([("pane", object([("pane_id", Json::from(first))]))]))
        }
        _ => Err(unknown(args)),
    }
}

/// herdr's `agent VERB NAME REST...`, args being the whole command.
fn agent_command(ledger: &Ledger, state: &mut Map<Json>, args: &[&str]) -> Result<Json, HerdrFailure> {
    let (verb, rest) = verb_and_rest(args);
    let name = rest.first().copied().unwrap_or_default();
    match verb {
        "start" => {
            let p = flag(rest, "--pane");
            pane(state, p)?;
            let session = format!("session-{}", next_id(state, "session"));
            let agent = object([
                ("pane", Json::from(p)),
                ("kind", Json::from(flag(rest, "--kind"))),
                ("status", Json::from("idle")),
                ("session", Json::Str(session)),
            ]);
            let record = agent_record(name, &agent);
            object_at(state, "agents").insert(name, agent);
            Ok(object([("agent", record)]))
        }
        "get" => {
            let record = agent_record(name, agent(state, name)?);
            reach_holds_on(state, name);
            Ok(object([("agent", record)]))
        }
        "wait" => {
            let a = agent(state, name)?;
            let until: Vec<&str> = rest.windows(2).filter(|w| w[0] == "--until").map(|w| w[1]).collect();
            if !until.is_empty() && !until.contains(&str_at(a, "status")) {
                return Err(failure("timeout", format!("agent {name} did not reach {}", until.join(", "))));
            }
            Ok(object([("agent", agent_record(name, a))]))
        }
        "prompt" => {
            agent(state, name)?;
            prompted(ledger, state, name);
            Ok(object([]))
        }
        "focus" => {
            agent(state, name)?;
            Ok(object([]))
        }
        _ => Err(unknown(args)),
    }
}

/// Mark reached each hold an action of agent name waits on: the engine's first poll of the agent after prompting it,
/// after which it does nothing but poll again until the hold is released.
fn reach_holds_on(state: &mut Map<Json>, name: &str) {
    let holds_on_it: Vec<String> = object_at(state, "held")
        .iter()
        .filter(|(_, entry)| str_at(entry, "name") == name)
        .map(|(hold, _)| hold.to_string())
        .collect();
    let reached = array_at(state, "reached");
    for hold in holds_on_it {
        if !reached.iter().any(|r| r.as_str() == Some(&hold)) {
            reached.push(Json::Str(hold));
        }
    }
}

fn pane<'a>(state: &'a mut Map<Json>, name: &str) -> Result<&'a mut Json, HerdrFailure> {
    object_at(state, "panes").get_mut(name).ok_or_else(|| failure("pane_not_found", format!("no pane {name}")))
}

fn agent<'a>(state: &'a mut Map<Json>, name: &str) -> Result<&'a Json, HerdrFailure> {
    match object_at(state, "agents").get(name) {
        Some(a) => Ok(a),
        None => Err(failure("agent_not_found", format!("no agent {name}"))),
    }
}

fn agent_record(name: &str, agent: &Json) -> Json {
    object([
        ("name", Json::from(name)),
        ("pane_id", crate::at(agent, "pane").clone()),
        ("agent_status", crate::at(agent, "status").clone()),
        ("agent_session", object([("value", crate::at(agent, "session").clone())])),
    ])
}

/// The role of agent name, which the engine names `<role>-<run key>`.
fn role_of(name: &str) -> &str {
    name.rsplit_once('-').map_or(name, |(role, _)| role)
}

/// Play the role's next scripted action for the prompt just sent to agent name: performed now, or held until the world
/// releases its hold, the hold being reached at the engine's first `agent get` of the agent after it. A prompt the
/// script has no action for makes the agent exit, which the engine reports, rather than leave the scenario waiting.
fn prompted(ledger: &Ledger, state: &mut Map<Json>, name: &str) {
    let role = role_of(name);
    let script = ledger.script();
    let actions = script.get("agents").and_then(|a| a.get(role)).and_then(Json::as_array).cloned().unwrap_or_default();
    let consumed = object_at(state, "consumed");
    let k = consumed.get(role).and_then(Json::as_int).unwrap_or(0);
    let Some(action) = usize::try_from(k).ok().and_then(|k| actions.get(k)).cloned() else {
        object_at(state, "agents").remove(name);
        return;
    };
    consumed.insert(role, Json::Int(k + 1));
    if let Some(hold) = action.get("hold").and_then(Json::as_str).filter(|h| !h.is_empty()) {
        if !holds(state, "released", hold) {
            mark_agent(state, name, Json::from("working"));
            object_at(state, "held").insert(hold, object([("name", Json::from(name)), ("action", action.clone())]));
            return;
        }
    }
    perform(ledger, state, name, &action);
}

/// Set agent name's status, as herdr reports it, to `to`; nothing for an agent that has exited.
fn mark_agent(state: &mut Map<Json>, name: &str, to: Json) {
    if let Some(Json::Object(a)) = object_at(state, "agents").get_mut(name) {
        a.insert("status", to);
    }
}

/// Perform each held action whose hold has been released since.
fn perform_released(ledger: &Ledger, state: &mut Map<Json>) {
    let held: Vec<String> = object_at(state, "held").keys().map(str::to_string).collect();
    let released: Vec<String> = held.into_iter().filter(|h| holds(state, "released", h)).collect();
    for hold in released {
        let entry = object_at(state, "held").remove(&hold).unwrap_or(Json::Null);
        let name = str_at(&entry, "name");
        if object_at(state, "agents").contains_key(name) {
            perform(ledger, state, name, crate::at(&entry, "action"));
        }
    }
}

/// Do what the agent name's action says, as its agent would in the turn the prompt starts.
///
/// An action is an object of any of: "edit", {path relative to the run's working directory (its worktree, else the
/// project): text, or null to delete the file}; "write", {path relative to the run directory: text}, written after
/// the edits, so that the handoff file, which ends the turn, comes last; "exit", true for an agent that exits
/// afterwards; "status", the agent's status afterwards, default idle; and "hold", which the world releases.
fn perform(ledger: &Ledger, state: &mut Map<Json>, name: &str, action: &Json) {
    let script = ledger.script();
    let key = name.rsplit_once('-').map_or("", |(_, key)| key);
    let run_dir = run_dir(Path::new(str_at(&script, "project")), key);
    let state_path = run_dir.join("state.json");
    let text = fs::read_to_string(&state_path).unwrap_or_else(|e| panic!("reading {}: {e}", state_path.display()));
    let saved = Json::parse(&text).unwrap_or_else(|e| panic!("{}: {e}", state_path.display()));
    let work_dir = match saved.get("worktree").filter(|w| w.truthy()) {
        Some(worktree) => worktree.as_str().unwrap_or_default().to_string(),
        None => str_at(&saved, "cwd").to_string(),
    };
    for (path, text) in action.get("edit").map(Json::as_object_or_empty).into_iter().flat_map(Map::iter) {
        put(&Path::new(&work_dir).join(path), text);
    }
    for (path, text) in action.get("write").map(Json::as_object_or_empty).into_iter().flat_map(Map::iter) {
        put(&run_dir.join(path), text);
    }
    mark_agent(state, name, action.get("status").cloned().unwrap_or(Json::from("idle")));
    if action.get("exit").is_some_and(Json::truthy) {
        object_at(state, "agents").remove(name);
    }
}

/// Write text to path, making its directories; delete path for null.
fn put(path: &Path, text: &Json) {
    let Some(text) = text.as_str() else {
        fs::remove_file(path).unwrap_or_else(|e| panic!("removing {}: {e}", path.display()));
        return;
    };
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).unwrap_or_else(|e| panic!("making {}: {e}", dir.display()));
    }
    fs::write(path, text).unwrap_or_else(|e| panic!("writing {}: {e}", path.display()));
}

/// Answer one gh command as gh would, logging it with its standard input; returns the exit status.
///
/// `script["gh"]["create_hold"]`, when set, holds `pr create` until the world releases it: the fake waits, so that
/// the engine can be stopped in the middle of publishing.
pub fn gh(ledger: &Ledger, args: &[String]) -> i32 {
    let words: Vec<&str> = args.iter().map(String::as_str).collect();
    let stdin = words.contains(&"--body-file").then(|| {
        let mut text = String::new();
        std::io::stdin().read_to_string(&mut text).unwrap_or_else(|e| panic!("reading gh's standard input: {e}"));
        text
    });
    let hold = logged(ledger, &words, stdin.as_deref());
    if words.starts_with(&["auth", "status"]) {
        println!("github.com\n  Logged in to github.com account parity");
        return 0;
    }
    if let Some(hold) = hold {
        wait_until(ledger, |state| holds(state, "released", &hold));
    }
    let origin = str_at(&ledger.script(), "origin").to_string();
    ledger.change(|state| match words.as_slice() {
        ["pr", "create", ..] => opened(state, &words, stdin),
        ["pr", verb @ ("view" | "edit" | "ready"), target, ..] => {
            let number = target.trim_end_matches('/').rsplit('/').next().unwrap_or_default();
            let Some(Json::Object(pr)) = object_at(state, "prs").get_mut(number) else {
                eprintln!("no pull requests found for {target}");
                return 1;
            };
            match *verb {
                "edit" => pr.insert("body", Json::from(stdin)),
                "ready" => pr.insert("isDraft", Json::Bool(false)),
                _ => println!("{}", viewed(pr, flag(&words, "--json"), &origin).dumps()),
            }
            0
        }
        _ => {
            eprintln!("the fake gh has no command {}", words.join(" "));
            1
        }
    })
}

/// Log gh command words, with its standard input when it read one; the hold `pr create` is to wait on, reached now,
/// when the script holds it.
fn logged(ledger: &Ledger, words: &[&str], stdin: Option<&str>) -> Option<String> {
    let pids = ledger.engine_pids();
    ledger.change(|state| {
        let mut event = Map::from_iter([("gh", strings(words))]);
        if let Some(stdin) = stdin {
            event.insert("stdin", Json::from(stdin));
        }
        ledger.log(state, event, &invocation(), &pids);
        let script = ledger.script();
        let hold = script.get("gh").and_then(|g| g.get("create_hold")).and_then(Json::as_str).map(str::to_string);
        let hold = hold.filter(|h| words.starts_with(&["pr", "create"]) && !h.is_empty());
        if let Some(hold) = &hold {
            array_at(state, "reached").push(Json::Str(hold.clone()));
        }
        hold
    })
}

/// Open the pull request `gh pr create` words asks for, with body, and print its URL; the exit status.
fn opened(state: &mut Map<Json>, words: &[&str], body: Option<String>) -> i32 {
    let n = next_id(state, "pr");
    let pr = object([
        ("base", Json::from(flag(words, "--base"))),
        ("head", Json::from(flag(words, "--head"))),
        ("title", Json::from(flag(words, "--title"))),
        ("body", Json::from(body)),
        ("isDraft", Json::Bool(words.contains(&"--draft"))),
        ("state", Json::from("OPEN")),
        ("closedAt", Json::Null),
        ("mergeable", Json::from("MERGEABLE")),
    ]);
    object_at(state, "prs").insert(n.to_string(), pr);
    println!("{}", pr_url(n));
    0
}

/// The fields of pr that `gh pr view --json fields` gives, fields comma-separated, its head commit read from origin.
/// Panics on a field the fake does not hold.
fn viewed(pr: &Map<Json>, fields: &str, origin: &str) -> Json {
    let head = pr.get("head").and_then(Json::as_str).unwrap_or_default();
    let mut view = pr.clone();
    view.insert("headRefOid", Json::Str(origin_commit(origin, head)));
    let chosen = fields.split(',').map(|k| (k, view.get(k).cloned().unwrap_or_else(|| panic!("gh has no field {k}"))));
    Json::Object(chosen.collect())
}

/// The commit of branch in the origin repository, or 40 zeros when it has none: gh still names the head that the pull
/// request last had.
fn origin_commit(origin: &str, branch: &str) -> String {
    let out = Command::new("git")
        .args(["-C", origin, "rev-parse", "--verify", "--quiet", &format!("refs/heads/{branch}")])
        .output()
        .unwrap_or_else(|e| panic!("running git rev-parse in {origin}: {e}"));
    let sha = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if sha.is_empty() {
        "0".repeat(40)
    } else {
        sha
    }
}

/// Run ssh's command, the arguments after its options and target joined by spaces as ssh joins them, with sh on this
/// machine; its exit status, 128 and the signal's number for a command a signal ended, as a shell reports it.
pub fn ssh(args: &[String]) -> i32 {
    let mut i = 0;
    while args.get(i).is_some_and(|a| a.starts_with('-')) {
        i += if ["-o", "-p", "-i", "-l", "-F"].contains(&args[i].as_str()) { 2 } else { 1 };
    }
    // The command the engine sent, run as sshd's login shell would run it.
    let command = args.get(i + 1..).unwrap_or_default().join(" ");
    let ended = Command::new("sh").args(["-c", &command]).status().unwrap_or_else(|e| panic!("running sh: {e}"));
    ended.code().unwrap_or_else(|| 128 + ended.signal().unwrap_or(0))
}

/// The main of fake-PROGRAM: argv is the program's own, its first argument the world's directory and the rest the
/// command's.
pub fn main(program: fn(&Ledger, &[String]) -> i32) -> ! {
    let argv: Vec<String> = std::env::args().collect();
    let Some(world) = argv.get(1) else {
        eprintln!("usage: {} WORLD ARGS...", argv.first().map_or("fake", String::as_str));
        std::process::exit(2);
    };
    std::process::exit(program(&Ledger::new(Path::new(world)), &argv[2..]))
}
