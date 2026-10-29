//! herdr, the terminal multiplexer the role agents run in, as orchestrator.py's Herdr drives it: the [`Herdr`] trait
//! is the commands a run gives it, and [`ProcessHerdr`] gives them by running the `herdr` CLI.
//!
//! The trait is the commands, not the CLI's protocol: a fake plays a role's agent at the level of a prompt and its
//! handoff file, as tests/test_orchestrator.py's FakeHerdr does (`FakeHerdr`, herdr/fake.rs), and how herdr is
//! invoked and what it answers (its argv, its JSON, its error codes, the limit on its process) is ProcessHerdr's
//! alone. A trait of one primitive, a herdr call that the commands are provided methods on, as Host is built on
//! exec, was weighed and not taken: a fake would then have to read herdr's argv and write its JSON to play an agent,
//! for nothing a run's tests need, and the run would see that primitive too.

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

    /// Send the agent a prompt, without waiting for it to be taken.
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
}

/// What running the herdr CLI once gives: its exit status and both outputs, as run_process gives them.
type Ran = Result<(i32, Vec<u8>, Vec<u8>), ProcessError>;

/// Runs the herdr CLI: its argv, herdr first, and how long its process may run (None: as long as it takes).
type RunHerdr = dyn Fn(&[String], Option<Duration>) -> Ran + Send + Sync;

/// herdr reached by running its CLI here, each command forwarded to a saved SSH machine (herdr's `--machine`) when
/// the run has one. Each command runs in the engine's environment as child_env gives it (crate::env::child_env), so
/// herdr and the agents it starts never inherit the CI credentials, and fails as interrupted when the Interrupt it
/// was given is raised while it runs, even one that herdr had already carried out, such as a prompt delivered
/// (crate::host::run_process).
#[derive(Clone)]
pub struct ProcessHerdr {
    /// The herdr machine its commands are forwarded to, as `herdr machine list` names it by label or id; None for
    /// this machine's herdr. Never empty.
    machine: Option<String>,
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
        let (env, interrupt) = (crate::env::without_withheld(vars), interrupt.clone());
        ProcessHerdr::with_runner(machine, move |argv, limit| run_process(argv, None, limit, &env, &interrupt))
    }

    /// As new, each command run by run in place of a process: Python's Herdr(run=...), for the tests.
    pub(crate) fn with_runner(
        machine: Option<&str>,
        run: impl Fn(&[String], Option<Duration>) -> Ran + Send + Sync + 'static,
    ) -> ProcessHerdr {
        ProcessHerdr { machine: machine.filter(|m| !m.is_empty()).map(String::from), run: Arc::new(run) }
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
        let mut argv: Vec<&str> = vec![];
        if let Some(machine) = &self.machine {
            argv.extend(["--machine", machine]);
        }
        argv.extend(args);
        let out = self.exec(&argv, limit)?;
        let command = args[..args.len().min(2)].join(" ");
        let bad = |why: String| HerdrError::new("bad_response", format!("{command}: {why}"));
        let mut answer = Json::parse(&out).map_err(bad)?;
        match &mut answer {
            Json::Object(map) => map.remove("result").ok_or_else(|| bad("'result'".to_string())),
            _ => Err(bad(format!("the answer is not an object: {}", strip(&out)))),
        }
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
        let (status, stdout, stderr) = match (self.run)(&argv, limit) {
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
        self.call(&["agent", "prompt", name, text]).map(drop)
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
}

#[cfg(test)]
mod tests;
