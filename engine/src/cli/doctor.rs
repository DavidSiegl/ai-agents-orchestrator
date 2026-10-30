//! `doctor`: what a run needs, checked before the first run fails on it, as orchestrator.py's check_setup and
//! report_checks check and report it. Python's last check, tkinter for the GUI, is not made: the Rust engine has no
//! GUI (the rust-engine spec's Decisions).

use super::{Local, Target};
use crate::constants::{AGENT_KINDS, DEFAULT_AGENT, EXIT_ERROR, GITLEAKS_INSTALL, NETWORK_TIMEOUT, REMOTE};
use crate::error::OrchestratorError;
use crate::herdr::ProcessHerdr;
use crate::host::{checked, decoded, Completed, ExecError, Host, ProcessHost, DEFAULT_TIMEOUT};
use crate::py::text::{is_space, repr, splitlines, strip};

/// The oldest herdr a run works with, by the numbers its --version prints.
pub const HERDR_MIN_VERSION: [u64; 2] = [0, 9];
/// The oldest git a run works with, by the numbers its --version prints.
pub const GIT_MIN_VERSION: [u64; 2] = [2, 36];
/// The exit status a POSIX shell gives a command it cannot find, as the shell an SSH command runs in does.
const COMMAND_NOT_FOUND_EXIT: i32 = 127;

/// A version as it is written, such as 0.9 for [0, 9].
pub fn dotted(version: &[u64]) -> String {
    version.iter().map(u64::to_string).collect::<Vec<_>>().join(".")
}

/// How much a failed Check matters, as README's Requirements lists them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    /// No run starts without it.
    Required,
    /// No run opens its pull request without it.
    NeededForPr,
    /// Only some runs need it.
    Optional,
}

impl Level {
    /// The level as Python's REQUIRED, NEEDED_FOR_PR and OPTIONAL name it.
    pub fn as_str(self) -> &'static str {
        match self {
            Level::Required => "required",
            Level::NeededForPr => "needed-for-pr",
            Level::Optional => "optional",
        }
    }
}

/// One thing a run needs, as check_setup found it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Check {
    /// What was checked, such as "herdr" or "codex integration".
    pub name: String,
    pub level: Level,
    pub ok: bool,
    /// What was found, such as a version or a path; when not ok, why, or for a skipped check the check it needs.
    pub detail: String,
    /// A command to run or one sentence; empty when ok; for a skipped check, a pointer to its cause.
    pub fix: String,
}

/// A requirement a probe found unmet, or could not check: what it found, and the fix that replaces the check's own,
/// when it has one. Python's _Unmet, and the OrchestratorError a probe raises.
#[derive(Debug)]
struct Unmet {
    detail: String,
    fix: Option<String>,
}

impl Unmet {
    fn new(detail: impl Into<String>) -> Unmet {
        Unmet { detail: detail.into(), fix: None }
    }

    fn with_fix(detail: impl Into<String>, fix: &str) -> Unmet {
        Unmet { detail: detail.into(), fix: Some(fix.to_string()) }
    }
}

impl From<OrchestratorError> for Unmet {
    fn from(e: OrchestratorError) -> Unmet {
        Unmet::new(e.0)
    }
}

/// What a probe gives: the detail of a requirement met.
type Probed = Result<String, Unmet>;

/// Print a line for each check by say, `ok`, `missing`, or `warn` for an Optional one that failed, then its name and
/// detail, with its fix on an indented line under it when it has one. Returns the exit status: 0 when every Required
/// and NeededForPr check is ok, EXIT_ERROR otherwise.
pub fn report_checks(checks: &[Check], say: &dyn Fn(&str)) -> i32 {
    for c in checks {
        let word = if c.ok {
            "ok"
        } else if c.level == Level::Optional {
            "warn"
        } else {
            "missing"
        };
        say(&format!("{word:<8}{}: {}", c.name, c.detail));
        if !c.fix.is_empty() {
            say(&format!("{:<8}fix: {}", "", c.fix));
        }
    }
    if checks.iter().all(|c| c.ok || c.level == Level::Optional) {
        0
    } else {
        EXIT_ERROR
    }
}

/// Print check_setup's checks where the agents run, this machine or the herdr machine target names, and of the
/// project at its cwd when it has one, as report_checks does, and return report_checks' exit status. Errors: only the
/// interrupt, once local's is raised, which nothing is printed for.
pub(super) fn doctor_command(local: &Local, target: &Target) -> Result<i32, OrchestratorError> {
    let herdr = (local.herdr)(target.machine.as_deref());
    let checks = check_setup(local.host, &herdr, target.cwd.as_deref(), local.in_herdr_pane);
    if local.interrupt.is_raised() {
        return Err(OrchestratorError::new("interrupted"));
    }
    Ok(report_checks(&checks, &|line| (local.stdout)(&format!("{line}\n"))))
}

/// A Check of each thing a run needs, in this order: herdr, reaching herdr, with herdr's machine herdr on that
/// machine, each harness's CLI and herdr integration, git, gh, gitleaks, and with cwd the project's git checkout and
/// its origin.
///
/// host runs commands on this machine, where the orchestrator runs and whose herdr a run drives. Without a machine the
/// agents run here too, and herdr is reached from a herdr pane, which in_herdr_pane says this process is in. With
/// one, this herdr must find the machine saved and enabled, and every later check runs on its SSH target, through a
/// Host that starts commands as host does (ProcessHost::over_ssh). cwd is the project's directory where the agents
/// run, a leading ~ meaning the home directory there, as for a run; without it the project is not checked.
///
/// Only reads: it installs, logs in and writes nothing. A check that fails, or cannot run or times out, has ok false
/// and the error as its detail. A check that needs an earlier one that failed is skipped: ok false, with a detail
/// naming that one. So one failure does not stop the rest, and nothing is an error.
pub fn check_setup(host: &ProcessHost, herdr: &ProcessHerdr, cwd: Option<&str>, in_herdr_pane: bool) -> Vec<Check> {
    let mut checks: Vec<Check> = Vec::new();
    let install_herdr = format!("install herdr {} or later, or update it with `herdr update`", dotted(&HERDR_MIN_VERSION));
    let mut found = check(&mut checks, "herdr", Level::Required, &install_herdr, None, "", &mut || {
        tool_version(host, "herdr", &HERDR_MIN_VERSION)
    });
    let (reach_host, reach) = reach_herdr(host, herdr, &found, in_herdr_pane);
    checks.push(reach.clone());
    // On a machine every check needs reaching it; reach_host is only used once it is.
    let machine = herdr.machine();
    let on_machine = machine.map(|_| reach);
    let agents = reach_host.as_ref().unwrap_or(host);
    if let Some(machine) = machine {
        let name = format!("herdr on {machine}");
        found = check(&mut checks, &name, Level::Required, &install_herdr, on_machine.as_ref(), "", &mut || {
            tool_version(agents, "herdr", &HERDR_MIN_VERSION)
        });
    }
    let mut integrations = IntegrationStatus::new(agents);
    for kind in AGENT_KINDS {
        let (level, note) = if kind == DEFAULT_AGENT {
            (Level::Required, String::new())
        } else {
            (Level::Optional, format!("needed only for a role that runs in {kind}"))
        };
        let fix = format!("install the {kind} CLI where the agents run");
        check(&mut checks, &format!("{kind} CLI"), level, &fix, on_machine.as_ref(), &note, &mut || {
            on_path(agents, kind)
        });
        let fix = format!("herdr integration install {kind}");
        check(&mut checks, &format!("{kind} integration"), level, &fix, Some(&found), &note, &mut || {
            integrations.current(kind)
        });
    }
    let fix = format!("install git {} or later", dotted(&GIT_MIN_VERSION));
    let git = check(&mut checks, "git", Level::Required, &fix, on_machine.as_ref(), "", &mut || {
        tool_version(agents, "git", &GIT_MIN_VERSION)
    });
    let fix = "install gh (https://cli.github.com)";
    check(&mut checks, "gh", Level::NeededForPr, fix, on_machine.as_ref(), "", &mut || gh_logged_in(agents));
    check(&mut checks, "gitleaks", Level::NeededForPr, GITLEAKS_INSTALL, on_machine.as_ref(), "", &mut || {
        agents.gitleaks_problem().map_or_else(|| Ok("`gitleaks git` runs".to_string()), |p| Err(Unmet::new(p)))
    });
    if let Some(cwd) = cwd {
        let fix = "pass --cwd a git checkout, or make one with git clone or git init";
        let checkout = check(&mut checks, "git checkout", Level::NeededForPr, fix, Some(&git), "", &mut || {
            top_level(agents, cwd)
        });
        let fix = format!("git remote add {REMOTE} <url>");
        check(&mut checks, "origin", Level::NeededForPr, &fix, Some(&checkout), "", &mut || {
            Ok(strip(&agents.git(&checkout.detail, &["remote", "get-url", REMOTE])?).to_string())
        });
    }
    checks
}

/// Add to checks, and return, the Check of probe, named name at level, with fix its fix when the probe brings none;
/// or, when needs is given and not ok, a Check skipped for it, probe not run. note is added to the detail of a Check
/// that is not ok. Python's _check_unless_skipped.
fn check(
    checks: &mut Vec<Check>,
    name: &str,
    level: Level,
    fix: &str,
    needs: Option<&Check>,
    note: &str,
    probe: &mut dyn FnMut() -> Probed,
) -> Check {
    let mut c = match needs {
        Some(needs) if !needs.ok => skipped(name, level, needs),
        _ => match probe() {
            Ok(detail) => Check { name: name.to_string(), level, ok: true, detail, fix: String::new() },
            Err(unmet) => Check {
                name: name.to_string(),
                level,
                ok: false,
                detail: unmet.detail,
                fix: unmet.fix.unwrap_or_else(|| fix.to_string()),
            },
        },
    };
    if !c.ok && !note.is_empty() {
        c.detail = format!("{}; {note}", c.detail);
    }
    checks.push(c.clone());
    c
}

/// The Check of name, skipped because needs is not ok: not ok itself, its fix pointing at needs.
fn skipped(name: &str, level: Level, needs: &Check) -> Check {
    Check {
        name: name.to_string(),
        level,
        ok: false,
        detail: format!("skipped: needs {}", needs.name),
        fix: format!("fix {} first", needs.name),
    }
}

/// The Host the agents run on, None when it cannot be reached, and the Check of reaching herdr: as connect needs it,
/// from a herdr pane without herdr's machine; with one, the machine saved and enabled, and its SSH target answering
/// non-interactive SSH, which is skipped when local_herdr, the Check of herdr here, is not ok.
fn reach_herdr(
    host: &ProcessHost,
    herdr: &ProcessHerdr,
    local_herdr: &Check,
    in_herdr_pane: bool,
) -> (Option<ProcessHost>, Check) {
    let name = "reaching herdr";
    let reached = |ok: bool, detail: String, fix: String| Check { name: name.into(), level: Level::Required, ok, detail, fix };
    let Some(machine) = herdr.machine() else {
        return match in_herdr_pane {
            true => (Some(host.clone()), reached(true, "inside a herdr pane".into(), String::new())),
            false => {
                let fix = "run from a herdr pane, or pass --machine".to_string();
                (Some(host.clone()), reached(false, "not inside a herdr pane".into(), fix))
            }
        };
    };
    if !local_herdr.ok {
        return (None, skipped(name, Level::Required, local_herdr));
    }
    let remote = herdr.ssh_target().map_err(OrchestratorError::from).and_then(|target| {
        let remote = host.over_ssh(&target);
        remote.check(&["true"], None, DEFAULT_TIMEOUT)?;
        Ok((remote, target))
    });
    match remote {
        Ok((remote, target)) => {
            (Some(remote), reached(true, format!("herdr machine {machine}, over SSH to {target}"), String::new()))
        }
        Err(e) => {
            let fix = format!("save herdr machine {machine} and enable it, with non-interactive SSH to its target");
            (None, reached(false, e.0, fix))
        }
    }
}

/// Where command is on host's PATH. Unmet: it is not there.
fn on_path(host: &dyn Host, command: &str) -> Probed {
    let proc = host.run(&["sh", "-c", "command -v -- \"$1\"", "_", command], None, DEFAULT_TIMEOUT)?;
    let path = strip(&proc.stdout);
    if proc.status != 0 || path.is_empty() {
        return Err(Unmet::new(format!("{command} is not on PATH")));
    }
    Ok(path.to_string())
}

/// argv's exit status and output, as host.run gives them, run within timeout seconds. Unmet as on_path's, when argv[0]
/// is not on host's PATH: it cannot be started here, or over SSH the remote shell exits COMMAND_NOT_FOUND_EXIT; and
/// with the error for any other failure to run it.
fn run_tool(host: &dyn Host, argv: &[&str], timeout: u64) -> Result<Completed, Unmet> {
    let not_on_path = || Unmet::new(format!("{} is not on PATH", argv[0]));
    let proc = match host.exec(argv, None, timeout) {
        Err(ExecError::NotFound(_)) => return Err(not_on_path()),
        Err(ExecError::Failed(e)) => return Err(e.into()),
        Ok(exec) => decoded(exec)?,
    };
    if host.ssh_target().is_some() && proc.status == COMMAND_NOT_FOUND_EXIT {
        return Err(not_on_path());
    }
    Ok(proc)
}

/// What `command --version` prints, once command is on host's PATH and the first version number it prints is
/// minimum or later. Unmet: it is missing, prints no version or an older one, or fails.
fn tool_version(host: &dyn Host, command: &str, minimum: &[u64]) -> Probed {
    let argv = [command, "--version"];
    let said = strip(&checked(host, &argv, run_tool(host, &argv, DEFAULT_TIMEOUT)?)?).to_string();
    let Some(number) = version_number(&said) else {
        return Err(Unmet::new(format!("{command} --version prints no version number: {}", repr(&said))));
    };
    let parts: Vec<u64> = number.split('.').map(|n| n.parse().unwrap_or(u64::MAX)).collect();
    if parts.as_slice() < minimum {
        return Err(Unmet::new(format!("{command} {number} is older than {}", dotted(minimum))));
    }
    Ok(said)
}

/// The first version number in text, as Python's `\d+(?:\.\d+)+` finds it: ASCII digits, then one or more groups of
/// a dot and digits.
fn version_number(text: &str) -> Option<&str> {
    let bytes = text.as_bytes();
    let digits_from = |i: usize| bytes[i..].iter().take_while(|b| b.is_ascii_digit()).count();
    let mut i = 0;
    while i < bytes.len() {
        let n = digits_from(i);
        if n == 0 {
            i += 1;
            continue;
        }
        let mut end = i + n;
        while end + 1 < bytes.len() && bytes[end] == b'.' && bytes[end + 1].is_ascii_digit() {
            end += 1 + digits_from(end + 1);
        }
        if end > i + n {
            return Some(&text[i..end]);
        }
        // A number without a dot after it is no version; one of its own digits cannot start one either.
        i += n;
    }
    None
}

/// The states `herdr integration status` gives the harnesses on a host, asked once, when first needed.
struct IntegrationStatus<'h> {
    host: &'h dyn Host,
    /// By harness kind; or why herdr could not be asked; None until asked.
    states: Option<Result<Vec<(String, String)>, String>>,
}

impl<'h> IntegrationStatus<'h> {
    fn new(host: &'h dyn Host) -> IntegrationStatus<'h> {
        IntegrationStatus { host, states: None }
    }

    /// The state of kind's integration when it is current, such as "current (v10)". Unmet: it is in any other, or not
    /// listed, and with herdr's fix when herdr cannot be asked.
    fn current(&mut self, kind: &str) -> Probed {
        let host = self.host;
        let states = self.states.get_or_insert_with(|| {
            host.check(&["herdr", "integration", "status"], None, DEFAULT_TIMEOUT)
                .map(|listing| integration_states(&listing))
                .map_err(|e| e.0)
        });
        let states = states.as_ref().map_err(|e| {
            Unmet::with_fix(e.clone(), "update herdr with `herdr update`, then check that `herdr integration status` runs")
        })?;
        let Some((_, state)) = states.iter().find(|(k, _)| k == kind) else {
            return Err(Unmet::new(format!("herdr integration status does not list {kind}")));
        };
        if !state.starts_with("current") {
            return Err(Unmet::new(state.clone()));
        }
        Ok(state.clone())
    }
}

/// Each harness's integration state, by its kind, from `herdr integration status`, whose lines read
/// `<kind>: current (vN) (<path>)`, `<kind>: not installed (<path>)` or the like for another state, and a kind may
/// carry a note, as `letta (experimental): ...`. The state is what follows the colon, the path left out. A kind listed
/// twice has its last state. Python's integration_states and its pattern `(\S+)[^:]*: (.*?)(?: \([^()]*\))?`.
pub fn integration_states(listing: &str) -> Vec<(String, String)> {
    let mut states: Vec<(String, String)> = Vec::new();
    for line in splitlines(listing) {
        let line = crate::py::text::strip(line);
        let Some((kind, state)) = integration_line(line) else { continue };
        match states.iter_mut().find(|(k, _)| *k == kind) {
            Some(entry) => entry.1 = state,
            None => states.push((kind, state)),
        }
    }
    states
}

/// The kind and state one stripped line of `herdr integration status` gives, as integration_states reads it; None
/// for a line in no such form.
fn integration_line(line: &str) -> Option<(String, String)> {
    // The kind is the longest run of non-blanks at the start after which the first colon is followed by a space.
    let run = line.char_indices().find(|&(_, c)| is_space(c)).map_or(line.len(), |(i, _)| i);
    let ends = line[..run].char_indices().map(|(i, c)| i + c.len_utf8()).rev();
    for end in ends {
        let rest = &line[end..];
        let Some(colon) = rest.find(':') else { continue };
        if rest[colon + 1..].starts_with(' ') {
            let state = &rest[colon + 2..];
            return Some((line[..end].to_string(), without_path(state).to_string()));
        }
    }
    None
}

/// state without a last ` (<path>)`, a parenthesis holding no parenthesis that ends it.
fn without_path(state: &str) -> &str {
    let Some(inner) = state.strip_suffix(')') else { return state };
    match inner.rfind(['(', ')']) {
        Some(open) if inner[open..].starts_with('(') && inner[..open].ends_with(' ') => &state[..open - 1],
        _ => state,
    }
}

/// That gh is on host's PATH and logged in. Unmet with the fix for what is not.
fn gh_logged_in(host: &dyn Host) -> Probed {
    let proc = run_tool(host, &["gh", "auth", "status"], NETWORK_TIMEOUT)?;
    if proc.status != 0 {
        let said = Some(strip(&proc.stderr)).filter(|s| !s.is_empty()).unwrap_or_else(|| strip(&proc.stdout));
        let said: Vec<&str> = splitlines(said).into_iter().take(4).map(strip).collect();
        let said = said.join(" ");
        let detail = if said.is_empty() { String::new() } else { format!(": {said}") };
        return Err(Unmet::with_fix(format!("gh auth status fails{detail}"), "gh auth login"));
    }
    Ok("logged in".to_string())
}

/// The top level of the git checkout cwd is in on host, a leading ~ in cwd being host's home as Host::resolve_dir
/// reads it. Unmet: cwd is no directory there, or in no checkout.
fn top_level(host: &dyn Host, cwd: &str) -> Probed {
    let path = host.resolve_dir(cwd).map_err(|e| Unmet::new(format!("{cwd} is not a directory: {e}")))?;
    let proc = host.git_run(&path, &["rev-parse", "--show-toplevel"], DEFAULT_TIMEOUT)?;
    if proc.status != 0 {
        let first = splitlines(strip(&proc.stderr)).first().map(|l| format!(": {l}")).unwrap_or_default();
        return Err(Unmet::new(format!("{cwd} is not in a git checkout{first}")));
    }
    Ok(strip(&proc.stdout).to_string())
}

#[cfg(test)]
mod tests;
