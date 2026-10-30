//! herdr, the terminal multiplexer the role agents run in, as orchestrator.py's Herdr drives it: the [`Herdr`] trait
//! is the commands a run gives it, and [`ProcessHerdr`] gives them by running the `herdr` CLI.
//!
//! The trait is the commands, not the CLI's protocol: a fake plays a role's agent at the level of a prompt and its
//! handoff file, as tests/test_orchestrator.py's FakeHerdr does (`FakeHerdr`, herdr/fake.rs), and how herdr is
//! invoked and what it answers (its argv, its JSON, its error codes, the limit on its process) is ProcessHerdr's
//! alone. A trait of one primitive, a herdr call that the commands are provided methods on, as Host is built on
//! exec, was weighed and not taken: a fake would then have to read herdr's argv and write its JSON to play an agent,
//! for nothing a run's tests need, and the run would see that primitive too.
//!
//! [`HerdrServer`] says which of this machine's herdr servers a command reaches: a run saves the one its agents are
//! in, so that serve reaches them through [`AgentConsole`] from whatever environment it was started in.

use std::ffi::OsString;
use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use crate::constants::AGENT_START_TIMEOUT_MS;
use crate::error::OrchestratorError;
use crate::host::{run_process, ProcessError};
use crate::interrupt::Interrupt;
use crate::py::json::Json;
use crate::py::text::{strip, universal_newlines};

#[cfg(test)]
pub(crate) mod fake;

/// How long a herdr command that names no time of its own may run, in seconds.
const CALL_LIMIT_SECONDS: u64 = 120;
/// How much longer than the time herdr is given to wait the CLI's process may run, in milliseconds: the limit only
/// guards against a hung CLI.
const WAIT_MARGIN_MS: u64 = 60_000;

/// Why a herdr command failed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HerdrError {
    /// herdr answered with an error, by herdr's code (such as agent_not_found or timeout; `bad_response` for an
    /// answer that is not one, and `exit_<status>` for a failure herdr gave no code for): Python's HerdrError.
    Herdr { code: String, message: String },
    /// herdr could not be asked, or what it answered is not what was asked for: Python's OrchestratorError.
    Failed(OrchestratorError),
}

impl HerdrError {
    /// An error with herdr's code and message.
    pub fn new(code: &str, message: impl Into<String>) -> HerdrError {
        HerdrError::Herdr { code: code.to_string(), message: message.into() }
    }

    /// herdr's code for the error; None for a failure herdr gave none for.
    pub fn code(&self) -> Option<&str> {
        match self {
            HerdrError::Herdr { code, .. } => Some(code),
            HerdrError::Failed(_) => None,
        }
    }
}

impl fmt::Display for HerdrError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HerdrError::Herdr { code, message } => write!(f, "herdr {code}: {message}"),
            HerdrError::Failed(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for HerdrError {}

impl From<HerdrError> for OrchestratorError {
    fn from(e: HerdrError) -> OrchestratorError {
        match e {
            HerdrError::Failed(e) => e,
            herdr => OrchestratorError(herdr.to_string()),
        }
    }
}

/// The variable that points the herdr CLI at a server's API socket. herdr sets it in every pane it starts, to its own
/// socket, and it wins over HERDR_SESSION (herdr 0.9.1's src/session.rs, active_api_socket_path).
pub const HERDR_SOCKET_PATH: &str = "HERDR_SOCKET_PATH";

/// The variable that picks the named herdr session the CLI reaches, when HERDR_SOCKET_PATH is unset; with neither,
/// it reaches the default session (herdr 0.9.1's src/session.rs, SESSION_ENV_VAR).
pub const HERDR_SESSION: &str = "HERDR_SESSION";

/// Which of this machine's herdr servers the herdr CLI reaches, as the two variables of its environment that pick it,
/// HERDR_SOCKET_PATH and HERDR_SESSION, each as it was set or unset. herdr's own `--session` flag, which wins over
/// both, is never given.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HerdrServer {
    /// HERDR_SOCKET_PATH's value; None when it is unset.
    socket_path: Option<String>,
    /// HERDR_SESSION's value; None when it is unset.
    session: Option<String>,
}

impl HerdrServer {
    /// The server a herdr CLI run in the environment vars reaches; None when either variable is set to what is not
    /// UTF-8, which state.json cannot hold. A variable vars hold twice counts by its last value, as a process gets it.
    pub fn of_environment(vars: &[(OsString, OsString)]) -> Option<HerdrServer> {
        let value = |name: &str| match vars.iter().rev().find(|(k, _)| k == name) {
            None => Some(None),
            Some((_, v)) => v.to_str().map(|v| Some(v.to_string())),
        };
        Some(HerdrServer { socket_path: value(HERDR_SOCKET_PATH)?, session: value(HERDR_SESSION)? })
    }

    /// vars with HERDR_SOCKET_PATH and HERDR_SESSION as the server has them, each one it has unset removed: the
    /// environment in which a herdr CLI reaches the server, whatever vars picked.
    fn select(&self, vars: &[(OsString, OsString)]) -> Vec<(OsString, OsString)> {
        let picks = [(HERDR_SOCKET_PATH, &self.socket_path), (HERDR_SESSION, &self.session)];
        let picked = |k: &OsString| picks.iter().any(|(name, _)| k == name);
        let mut selected: Vec<_> = vars.iter().filter(|(k, _)| !picked(k)).cloned().collect();
        for (name, value) in picks {
            if let Some(value) = value {
                selected.push((OsString::from(name), OsString::from(value)));
            }
        }
        selected
    }

    /// The server as state.json holds it: {"HERDR_SOCKET_PATH": ..., "HERDR_SESSION": ...}, each a string, or null
    /// for a variable that was unset.
    pub fn to_json(&self) -> Json {
        let pairs = [(HERDR_SOCKET_PATH, &self.socket_path), (HERDR_SESSION, &self.session)];
        Json::Object(pairs.into_iter().map(|(name, value)| (name, Json::from(value.clone()))).collect())
    }

    /// The server to_json wrote, a variable missing from the object being unset; None for JSON of another shape.
    pub fn from_json(json: &Json) -> Option<HerdrServer> {
        let map = json.as_object()?;
        let value = |name: &str| match map.get(name) {
            None | Some(Json::Null) => Some(None),
            Some(Json::Str(v)) => Some(Some(v.clone())),
            Some(_) => None,
        };
        Some(HerdrServer { socket_path: value(HERDR_SOCKET_PATH)?, session: value(HERDR_SESSION)? })
    }
}

/// herdr's record of a running agent.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Agent {
    /// Its lifecycle status, such as idle, working, blocked or done.
    pub status: String,
    /// The id of the harness's session it runs in, once herdr knows it (Python's agent_session); None before, and
    /// when herdr reports an empty one.
    pub session: Option<String>,
}

/// Where a new pane goes beside the one it is split from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    Right,
    Down,
}

impl Direction {
    /// The direction as herdr's --direction takes it.
    pub fn as_str(self) -> &'static str {
        match self {
            Direction::Right => "right",
            Direction::Down => "down",
        }
    }
}

/// The commands a run gives herdr: workspaces and panes to show the agents in, and the agents themselves. Every
/// error names herdr's code where herdr gave one (HerdrError::code); each method says which codes it answers instead
/// of failing.
pub trait Herdr {
    /// Create a workspace labelled label whose root pane starts in cwd, without focusing it; gives (workspace id,
    /// root pane id).
    fn create_workspace(&self, cwd: &str, label: &str) -> Result<(String, String), HerdrError>;

    /// Split a new pane, starting in cwd, off pane in direction, without focusing it; gives its id.
    fn split(&self, pane: &str, direction: Direction, cwd: &str) -> Result<String, HerdrError>;

    /// Give pane the label shown on it.
    fn rename_pane(&self, pane: &str, label: &str) -> Result<(), HerdrError>;

    /// Start an agent named name in pane, of the harness kind (herdr's --kind), passing it args. Gives false when it
    /// is blocked on a startup dialog (herdr's agent_not_ready), such as Claude Code's folder-trust question in a
    /// directory it has not seen: herdr keeps the name bound, so the agent can still be waited on.
    fn start_agent(&self, name: &str, pane: &str, kind: &str, args: &[String]) -> Result<bool, HerdrError>;

    /// Send the agent a prompt, without waiting for it to be taken. Errors: Failed, without asking herdr, for text
    /// that starts like a long option, `--` and a letter or digit, which herdr cannot be given (see argument).
    fn prompt(&self, name: &str, text: &str) -> Result<(), HerdrError>;

    /// Wait for the agent to reach one of the statuses until (any change when empty), for at most timeout_ms
    /// milliseconds, or as long as it takes when None; gives the status it reached. Errors: herdr's timeout when the
    /// time runs out first.
    fn wait(&self, name: &str, timeout_ms: Option<u64>, until: &[&str]) -> Result<String, HerdrError>;

    /// herdr's record of the agent; None once it has exited (herdr's agent_not_found).
    fn agent(&self, name: &str) -> Result<Option<Agent>, HerdrError>;

    /// The agent's lifecycle status; None once it has exited.
    fn status(&self, name: &str) -> Result<Option<String>, HerdrError> {
        Ok(self.agent(name)?.map(|agent| agent.status))
    }

    /// Whether the workspace is still there (herdr's workspace_not_found is false).
    fn workspace_exists(&self, workspace: &str) -> Result<bool, HerdrError>;

    /// Whether the pane is still there (herdr's pane_not_found is false).
    fn pane_exists(&self, pane: &str) -> Result<bool, HerdrError>;

    /// Bring the agent's pane to the front.
    fn focus(&self, name: &str) -> Result<(), HerdrError>;

    /// Close the workspace with its panes.
    fn close_workspace(&self, workspace: &str) -> Result<(), HerdrError>;

    /// Which of this machine's herdr servers the commands reach, without asking herdr; None when they are forwarded
    /// to a herdr machine, or when the server cannot be told (HerdrServer::of_environment).
    fn server(&self) -> Option<HerdrServer>;
}

/// A run's agent, as a console reaches it: its herdr name, and the herdr server it runs in.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentAddress {
    /// The agent's herdr name, as its run's state saves it.
    pub name: String,
    /// The server of this machine its run started it in (RunState::herdr_server); None for the one the console's own
    /// environment reaches, and ignored by a console that forwards to a herdr machine.
    pub server: Option<HerdrServer>,
}

/// What a window does with a run's agent, by its address: show what its pane shows, and type into it, as the human
/// would in the pane (serve's /agent endpoints). Every error names herdr's code where herdr gave one, such as
/// agent_not_found for an agent that has exited, or agent_blocked for a prompt to one blocked on a question.
pub trait AgentConsole: Send + Sync {
    /// The last lines of the agent's terminal, at most lines of them, as plain text without its ANSI styling.
    fn read(&self, agent: &AgentAddress, lines: u32) -> Result<String, HerdrError>;

    /// Type text into the agent and submit it, as herdr's `agent prompt` does, without waiting for it to be taken.
    /// Errors: as Herdr::prompt's, so Failed, without asking herdr, for text that starts like a long option.
    fn submit(&self, agent: &AgentAddress, text: &str) -> Result<(), HerdrError>;

    /// Press keys in the agent's terminal, in order, each as herdr names it (`esc`, `enter`, `up`, `ctrl+c`); herdr
    /// checks every key before it sends any, so a key it does not know sends none. Errors: Failed, without asking
    /// herdr, when a key starts like a long option, `--` and a letter or digit, which herdr cannot be given.
    fn send_keys(&self, agent: &AgentAddress, keys: &[&str]) -> Result<(), HerdrError>;
}

/// What running the herdr CLI once gives: its exit status and both outputs, as run_process gives them.
type Ran = Result<(i32, Vec<u8>, Vec<u8>), ProcessError>;

/// Runs the herdr CLI: its argv, herdr first, the environment it runs in, and how long its process may run (None: as
/// long as it takes).
type RunHerdr = dyn Fn(&[String], &[(OsString, OsString)], Option<Duration>) -> Ran + Send + Sync;

/// herdr reached by running its CLI here, each command forwarded to a saved SSH machine (herdr's `--machine`) when
/// the run has one. Each command runs in the engine's environment as child_env gives it (crate::env::child_env), so
/// herdr and the agents it starts never inherit the CI credentials; a console's command to an agent whose run saved
/// its server runs with that server's HERDR_SOCKET_PATH and HERDR_SESSION in place of the engine's (AgentAddress).
/// Each fails as interrupted when the Interrupt it was given is raised while it runs, even one that herdr had already
/// carried out, such as a prompt delivered (crate::host::run_process).
#[derive(Clone)]
pub struct ProcessHerdr {
    /// The herdr machine its commands are forwarded to, as `herdr machine list` names it by label or id; None for
    /// this machine's herdr. Never empty.
    machine: Option<String>,
    /// The environment its commands run in, without the variables child_env withholds.
    env: Arc<Vec<(OsString, OsString)>>,
    /// Runs one herdr command.
    run: Arc<RunHerdr>,
}

impl fmt::Debug for ProcessHerdr {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProcessHerdr").field("machine", &self.machine).finish_non_exhaustive()
    }
}

impl ProcessHerdr {
    /// herdr here, its commands forwarded to machine when given one, an empty machine being none, and stopped by
    /// interrupt.
    pub fn new(machine: Option<&str>, interrupt: &Interrupt) -> ProcessHerdr {
        ProcessHerdr::in_environment(machine, std::env::vars_os(), interrupt)
    }

    /// As new, the CLI found on and run in the environment vars as child_env would give it, were vars the engine's
    /// own: what a test gives in place of the process's environment, which every test shares.
    pub(crate) fn in_environment(
        machine: Option<&str>,
        vars: impl Iterator<Item = (OsString, OsString)>,
        interrupt: &Interrupt,
    ) -> ProcessHerdr {
        let interrupt = interrupt.clone();
        ProcessHerdr {
            machine: machine.filter(|m| !m.is_empty()).map(String::from),
            env: Arc::new(crate::env::without_withheld(vars)),
            run: Arc::new(move |argv, env, limit| run_process(argv, None, limit, env, &interrupt)),
        }
    }

    /// As new, each command run by run in place of a process, in an empty environment: Python's Herdr(run=...), for
    /// the tests.
    #[cfg(test)]
    pub(crate) fn with_runner(
        machine: Option<&str>,
        run: impl Fn(&[String], Option<Duration>) -> Ran + Send + Sync + 'static,
    ) -> ProcessHerdr {
        ProcessHerdr {
            machine: machine.filter(|m| !m.is_empty()).map(String::from),
            env: Arc::default(),
            run: Arc::new(move |argv, _, limit| run(argv, limit)),
        }
    }

    /// This herdr, its commands run so that they reach the agent's server when it names one and they are not
    /// forwarded to a machine: the herdr a console's command to the agent goes to.
    fn reaching(&self, agent: &AgentAddress) -> ProcessHerdr {
        match (&agent.server, &self.machine) {
            (Some(server), None) => ProcessHerdr { env: Arc::new(server.select(&self.env)), ..self.clone() },
            _ => self.clone(),
        }
    }

    /// The herdr machine the commands are forwarded to; None for this machine's herdr.
    pub fn machine(&self) -> Option<&str> {
        self.machine.as_deref()
    }

    /// The SSH target of the machine, for reaching its filesystem, from this machine's saved machines: machine
    /// management is herdr's here, and is never forwarded. Errors: no saved machine is named so, or it is disabled.
    pub fn ssh_target(&self) -> Result<String, HerdrError> {
        let out = self.exec(&["machine", "list", "--json"], Some(Duration::from_secs(CALL_LIMIT_SECONDS)))?;
        let bad = |why: String| HerdrError::new("bad_response", format!("machine list: {why}"));
        let profiles = Json::parse(&out).map_err(bad)?;
        let profiles = profiles.as_array().ok_or_else(|| bad("not a list of machines".to_string()))?;
        // A label or id that is missing or null is Python's None, which a run without a machine matches.
        let names = |p: &Json, key: &str| match p.get(key) {
            None | Some(Json::Null) => Some(None),
            Some(Json::Str(s)) => Some(Some(s.clone())),
            Some(_) => None,
        };
        let machine = self.machine.clone();
        for p in profiles {
            if p.as_object().is_none() {
                return Err(bad(format!("a machine is not an object: {}", p.dumps())));
            }
            if [names(p, "label"), names(p, "id")].contains(&Some(machine.clone())) {
                let name = machine.as_deref().unwrap_or("None");
                if !p.get("enabled").is_some_and(Json::truthy) {
                    return Err(HerdrError::Failed(OrchestratorError(format!("herdr machine {name} is disabled"))));
                }
                return match p.get("target") {
                    Some(Json::Str(target)) => Ok(target.clone()),
                    _ => Err(bad(format!("machine {name} has no target"))),
                };
            }
        }
        let name = machine.as_deref().unwrap_or("None");
        Err(HerdrError::Failed(OrchestratorError(format!("no saved herdr machine named {name}"))))
    }

    /// Show a desktop notification with title and body, and a sound, from herdr.
    pub fn notify(&self, title: &str, body: &str) -> Result<(), HerdrError> {
        self.call(&["notification", "show", title, "--body", body, "--sound", "request"]).map(drop)
    }

    /// call_within, its process given CALL_LIMIT_SECONDS: for a command that does not wait on an agent.
    fn call(&self, args: &[&str]) -> Result<Json, HerdrError> {
        self.call_within(args, Some(Duration::from_secs(CALL_LIMIT_SECONDS)))
    }

    /// Run one API-backed command, forwarded to the machine, its process given limit as exec takes it, and give its
    /// `result`. Errors: as exec's, and herdr answering with what is not a result (bad_response).
    fn call_within(&self, args: &[&str], limit: Option<Duration>) -> Result<Json, HerdrError> {
        let out = self.exec_forwarded(args, limit)?;
        let command = args[..args.len().min(2)].join(" ");
        let bad = |why: String| HerdrError::new("bad_response", format!("{command}: {why}"));
        let mut answer = Json::parse(&out).map_err(bad)?;
        match &mut answer {
            Json::Object(map) => map.remove("result").ok_or_else(|| bad("'result'".to_string())),
            _ => Err(bad(format!("the answer is not an object: {}", strip(&out)))),
        }
    }

    /// The standard output of `herdr args`, forwarded to the machine, run with limit as exec takes it. Errors: exec's.
    fn exec_forwarded(&self, args: &[&str], limit: Option<Duration>) -> Result<String, HerdrError> {
        let mut argv: Vec<&str> = vec![];
        if let Some(machine) = &self.machine {
            argv.extend(["--machine", machine]);
        }
        argv.extend(args);
        self.exec(&argv, limit)
    }

    /// The standard output of `herdr args`, run with limit on its process (None: as long as it takes). Errors: herdr
    /// is not on PATH; its process runs out of time (timeout); the Interrupt is raised while it runs ("herdr ...:
    /// interrupted", no code); it exits with a status other than 0, with the code and
    /// message of the error it printed as JSON on stderr or stdout, else exit_<status> and what it printed; or it
    /// prints what is not UTF-8.
    fn exec(&self, args: &[&str], limit: Option<Duration>) -> Result<String, HerdrError> {
        let argv: Vec<String> = std::iter::once("herdr").chain(args.iter().copied()).map(String::from).collect();
        // The command as an error names it: its first three words, as Python's timeout names it, never a prompt's
        // text, which can be long and goes to the log and state.json with the error.
        let command = args[..args.len().min(3)].join(" ");
        let failed = |why: String| HerdrError::Failed(OrchestratorError(format!("herdr {command}: {why}")));
        let (status, stdout, stderr) = match (self.run)(&argv, &self.env, limit) {
            Ok(ran) => ran,
            Err(ProcessError::Start(e)) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(HerdrError::Failed(OrchestratorError::new("herdr is not on PATH")))
            }
            Err(ProcessError::Start(e) | ProcessError::Io(e)) => return Err(failed(crate::error::os_error(&e, None))),
            Err(ProcessError::Timeout) => {
                return Err(HerdrError::new("timeout", format!("herdr {command} did not return")));
            }
            Err(ProcessError::Interrupted) => return Err(failed("interrupted".to_string())),
        };
        let decode = |bytes: Vec<u8>| {
            String::from_utf8(bytes)
                .map(|text| universal_newlines(&text))
                .map_err(|e| failed(format!("printed what is not UTF-8: {}", e.utf8_error())))
        };
        let (stdout, stderr) = (decode(stdout)?, decode(stderr)?);
        if status != 0 {
            return Err(herdr_error(status, &stdout, &stderr));
        }
        Ok(stdout)
    }
}

/// answer's value, or None when herdr answered with the error code: for a command whose one error is an answer.
fn unless_code<T>(code: &str, answer: Result<T, HerdrError>) -> Result<Option<T>, HerdrError> {
    match answer {
        Ok(value) => Ok(Some(value)),
        Err(e) if e.code() == Some(code) => Ok(None),
        Err(e) => Err(e),
    }
}

/// The error a herdr command that exited with status printed: a server error arrives as `{"error": {"code",
/// "message"}}` on stderr or stdout, a syntax error (exit 2) as plain text.
fn herdr_error(status: i32, stdout: &str, stderr: &str) -> HerdrError {
    // A code or message that is no string is written as JSON, so that it is still shown.
    let text = |value: &Json| value.as_str().map_or_else(|| value.dumps(), String::from);
    for stream in [stderr, stdout] {
        let Ok(answer) = Json::parse(stream) else { continue };
        let Some(error) = answer.get("error").and_then(Json::as_object) else { continue };
        if let (Some(code), Some(message)) = (error.get("code"), error.get("message")) {
            return HerdrError::new(&text(code), text(message));
        }
    }
    let printed = if stderr.is_empty() { stdout } else { stderr };
    HerdrError::new(&format!("exit_{status}"), strip(printed))
}

/// A string field of an answer, at the path of keys; bad_response naming the command and the path when it is not
/// there or not a string.
fn field(answer: &Json, command: &str, path: &[&str]) -> Result<String, HerdrError> {
    let mut value = answer;
    for key in path {
        value = value.get(key).unwrap_or(&Json::Null);
    }
    value
        .as_str()
        .map(String::from)
        .ok_or_else(|| HerdrError::new("bad_response", format!("{command}: no {} in the answer", path.join("."))))
}

/// arg, a prompt's text or a key, once it is safe to pass herdr's `agent prompt` and `agent send-keys` as a positional
/// argument; what names it in the error ("a prompt", "a key"). herdr 0.9.1 reads those positionals as they are and
/// has no `--` to end options there (it would take a `--` as the text or a key itself), but takes its long launch
/// options (--session, --remote, --remote-keybindings, --handoff) out of all of its argv before any command sees it.
/// So arg must not look like a long option: `--` then a letter or digit, wider than the options herdr 0.9.1 takes,
/// against ones a later herdr adds; `-`, `- item` and `---` stay safe. Errors: Failed for an arg that looks like one.
fn argument<'a>(what: &str, arg: &'a str) -> Result<&'a str, HerdrError> {
    if arg.strip_prefix("--").and_then(|rest| rest.chars().next()).is_some_and(|c| c.is_ascii_alphanumeric()) {
        let why = format!("{what} cannot start with -- and a letter or digit: herdr may take it for its own option");
        return Err(HerdrError::Failed(OrchestratorError::new(why)));
    }
    Ok(arg)
}

impl Herdr for ProcessHerdr {
    fn create_workspace(&self, cwd: &str, label: &str) -> Result<(String, String), HerdrError> {
        let args = ["workspace", "create", "--cwd", cwd, "--label", label, "--no-focus"];
        let r = self.call(&args)?;
        Ok((
            field(&r, "workspace create", &["workspace", "workspace_id"])?,
            field(&r, "workspace create", &["root_pane", "pane_id"])?,
        ))
    }

    fn split(&self, pane: &str, direction: Direction, cwd: &str) -> Result<String, HerdrError> {
        let args = ["pane", "split", pane, "--direction", direction.as_str(), "--cwd", cwd, "--no-focus"];
        field(&self.call(&args)?, "pane split", &["pane", "pane_id"])
    }

    fn rename_pane(&self, pane: &str, label: &str) -> Result<(), HerdrError> {
        self.call(&["pane", "rename", pane, label]).map(drop)
    }

    fn start_agent(&self, name: &str, pane: &str, kind: &str, args: &[String]) -> Result<bool, HerdrError> {
        let timeout = AGENT_START_TIMEOUT_MS.to_string();
        let mut argv = vec!["agent", "start", name, "--kind", kind, "--pane", pane, "--timeout", &timeout];
        if !args.is_empty() {
            argv.push("--");
            argv.extend(args.iter().map(String::as_str));
        }
        let limit = Duration::from_millis(AGENT_START_TIMEOUT_MS + WAIT_MARGIN_MS);
        Ok(unless_code("agent_not_ready", self.call_within(&argv, Some(limit)))?.is_some())
    }

    fn prompt(&self, name: &str, text: &str) -> Result<(), HerdrError> {
        self.call(&["agent", "prompt", name, argument("a prompt", text)?]).map(drop)
    }

    fn wait(&self, name: &str, timeout_ms: Option<u64>, until: &[&str]) -> Result<String, HerdrError> {
        let timeout = timeout_ms.map(|ms| ms.to_string());
        let mut args = vec!["agent", "wait", name];
        if let Some(timeout) = &timeout {
            args.extend(["--timeout", timeout]);
        }
        for status in until {
            args.extend(["--until", status]);
        }
        let limit = timeout_ms.map(|ms| Duration::from_millis(ms + WAIT_MARGIN_MS));
        field(&self.call_within(&args, limit)?, "agent wait", &["agent", "agent_status"])
    }

    fn agent(&self, name: &str) -> Result<Option<Agent>, HerdrError> {
        let Some(r) = unless_code("agent_not_found", self.call(&["agent", "get", name]))? else { return Ok(None) };
        let status = field(&r, "agent get", &["agent", "agent_status"])?;
        let session = r.get("agent").and_then(|a| a.get("agent_session")).and_then(|s| s.get("value"));
        let session = session.and_then(Json::as_str).filter(|s| !s.is_empty()).map(String::from);
        Ok(Some(Agent { status, session }))
    }

    fn workspace_exists(&self, workspace: &str) -> Result<bool, HerdrError> {
        Ok(unless_code("workspace_not_found", self.call(&["workspace", "get", workspace]))?.is_some())
    }

    fn pane_exists(&self, pane: &str) -> Result<bool, HerdrError> {
        Ok(unless_code("pane_not_found", self.call(&["pane", "get", pane]))?.is_some())
    }

    fn focus(&self, name: &str) -> Result<(), HerdrError> {
        self.call(&["agent", "focus", name]).map(drop)
    }

    fn close_workspace(&self, workspace: &str) -> Result<(), HerdrError> {
        self.call(&["workspace", "close", workspace]).map(drop)
    }

    fn server(&self) -> Option<HerdrServer> {
        match self.machine {
            Some(_) => None,
            None => HerdrServer::of_environment(&self.env),
        }
    }
}

impl AgentConsole for ProcessHerdr {
    fn read(&self, agent: &AgentAddress, lines: u32) -> Result<String, HerdrError> {
        let lines = lines.to_string();
        // `agent read` prints the text itself, not a JSON answer; its errors are JSON as every command's.
        let args = ["agent", "read", &agent.name, "--source", "recent", "--lines", &lines];
        self.reaching(agent).exec_forwarded(&args, Some(Duration::from_secs(CALL_LIMIT_SECONDS)))
    }

    fn submit(&self, agent: &AgentAddress, text: &str) -> Result<(), HerdrError> {
        self.reaching(agent).prompt(&agent.name, text)
    }

    fn send_keys(&self, agent: &AgentAddress, keys: &[&str]) -> Result<(), HerdrError> {
        let mut args = vec!["agent", "send-keys", agent.name.as_str()];
        for key in keys {
            args.push(argument("a key", key)?);
        }
        self.reaching(agent).call(&args).map(drop)
    }
}

#[cfg(test)]
mod tests;
