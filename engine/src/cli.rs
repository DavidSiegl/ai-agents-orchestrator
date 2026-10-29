//! The orchestrator program's command line, as orchestrator.py's main: [`main`] reads the arguments (crate::cli::args),
//! connects to herdr and the project's host, runs the command and turns how it ended into the program's exit status
//! and its last lines. Ticket 08 has `run` and `resume`; ticket 09 adds the other commands, each a [`Command`] arm
//! of [`run_command`] given the same [`Connected`].

use std::ffi::OsString;
use std::io::Write;

use crate::ci::Jenkins;
use crate::constants::{EXIT_CHANGES_REQUESTED, EXIT_CONFLICT, EXIT_ERROR, EXIT_INTERRUPTED, SUCCEEDED};
use crate::env::ConfigDir;
use crate::error::OrchestratorError;
use crate::harness::AgentSettings;
use crate::herdr::ProcessHerdr;
use crate::host::ProcessHost;
use crate::interrupt::Interrupt;
use crate::pipeline::Pipeline;
use crate::py::text::shlex_join;
use crate::pypi::PyPI;
use crate::runs::{local_host, pid_alive};
use crate::spec_file::{read_spec_file, SpecFile};
use crate::state::RunState;
use crate::workflow::{Collaborators, LocalHuman, RunSettings, Stopped, SystemClocks, Workflow};

pub mod args;
mod start;

pub use args::{parse_args, Command};
pub use start::{new_state, resumable_state, resumed_state};

/// What a command talks to once connected: the Workflow's collaborators but its Jenkins, which each run makes for
/// its own job, and where the orchestrator's configuration is.
#[derive(Clone, Copy)]
pub struct Connected<'a> {
    /// Every collaborator a Workflow is given; its jenkins is None, and a run's own is made by jenkins.
    pub with: Collaborators<'a>,
    /// The quality job of a name, with its credentials (crate::ci::Jenkins::from_env for a real run). Errors: its
    /// credentials cannot be read.
    pub jenkins: &'a dyn Fn(&str) -> Result<Jenkins, OrchestratorError>,
    /// The orchestrator's configuration directory on this machine: its prompt and workflow files and ci.env.
    pub config: &'a ConfigDir,
    /// This machine's name, which an owner record of a run here names (crate::runs::local_host).
    pub local_host: &'a str,
}

/// How a command that ran a workflow ended, for the program's last line and exit status (finish).
struct Ran {
    /// What Workflow::run returned: the run's verdict; None prints as Python's str(None) does, "None".
    verdict: Option<String>,
    /// The run's state as the Workflow left it, at its end.
    state: RunState,
    /// The run's workflow.
    pipeline: Pipeline,
}

/// Why a command did not end as it should.
enum Stop {
    /// The error main prints, with exit status EXIT_ERROR.
    Failed(OrchestratorError),
    /// The Interrupt, raised by a Ctrl-C, a SIGTERM or a SIGHUP (exit status EXIT_INTERRUPTED); the id of the run it
    /// stopped, None before the run's state was made, for the hint that resumes it.
    Interrupted(Option<String>),
}

/// The program: argv are its arguments, without its own name; interrupt is crate::interrupt::by_signals', which the
/// process's SIGINT, SIGTERM and SIGHUP raise rather than end it. Returns its exit status: a usage error's 2 (help's
/// 0), EXIT_ERROR for an error, EXIT_INTERRUPTED once interrupt is raised (crate::interrupt::exit then ends a process
/// that got a SIGTERM or SIGHUP by that signal), or how the run ended (finish). Prints the run's last line on stdout,
/// and its log, errors and the usage on stderr.
pub fn main(argv: impl IntoIterator<Item = OsString>, interrupt: &Interrupt) -> i32 {
    let config = ConfigDir::from_env();
    let command = match parse_args(argv, &config) {
        Ok(command) => command,
        Err(e) => {
            // Nowhere else to tell the human, if stdout or stderr does not take the usage.
            let _ = e.print();
            return e.exit_code();
        }
    };
    let human = LocalHuman::new(interrupt);
    let ended = connect(&command, interrupt, &human, &config);
    ended_status(ended, &command)
}

/// The exit status of a command that ended so, its last line printed: finish's for a run, else the error or the
/// interrupted note, on stderr.
fn ended_status(ended: Result<Ran, Stop>, command: &Command) -> i32 {
    match ended {
        Ok(ran) => finish(ran.verdict.as_deref().unwrap_or("None"), &ran.state, &ran.pipeline),
        // A line stderr does not take is lost, as after a hangup (finish says why).
        Err(Stop::Failed(e)) => {
            let _ = writeln!(std::io::stderr(), "error: {e}");
            EXIT_ERROR
        }
        Err(Stop::Interrupted(run_id)) => {
            let _ = writeln!(std::io::stderr(), "{}", interrupted_note(run_id.as_deref(), &command.target().flags()));
            EXIT_INTERRUPTED
        }
    }
}

/// Connect to the herdr and the host command names, on this machine (which must be a herdr pane) or over SSH to its
/// --machine, and run it. Python's connect, then the rest of main.
fn connect(command: &Command, interrupt: &Interrupt, human: &LocalHuman, config: &ConfigDir) -> Result<Ran, Stop> {
    let stopped = |e: OrchestratorError| stop(e, interrupt, None);
    let machine = command.target().machine.as_deref();
    let herdr = ProcessHerdr::new(machine, interrupt);
    let local = ProcessHost::local(interrupt);
    let host = match machine {
        Some(_) => local.over_ssh(&herdr.ssh_target().map_err(|e| stopped(e.into()))?),
        None if !in_herdr_pane() => {
            return Err(stopped(OrchestratorError::new("not inside a herdr pane; run from herdr, or pass --machine")))
        }
        None => local,
    };
    let clocks = SystemClocks::new(interrupt).map_err(stopped)?;
    let pypi = PyPI::from_env();
    let jenkins = |job: &str| Jenkins::from_env(job, config);
    let here = local_host();
    let world = Connected {
        with: Collaborators {
            host: &host,
            herdr: &herdr,
            clocks: &clocks,
            human,
            interrupt,
            pid_alive: &pid_alive,
            jenkins: None,
            pypi: &pypi,
        },
        jenkins: &jenkins,
        config,
        local_host: &here,
    };
    run_command(command, &world)
}

/// Whether this process runs in a pane herdr manages, which a run without a machine needs: without a machine, herdr
/// commands target whichever session is focused, and only such a pane knows it is talking to its own.
fn in_herdr_pane() -> bool {
    std::env::var_os("HERDR_ENV").is_some_and(|v| v == "1")
}

/// Run command in world: a run or a resume, to its verdict.
fn run_command(command: &Command, world: &Connected) -> Result<Ran, Stop> {
    let interrupt = world.with.interrupt;
    let (state, pipeline, spec) = starting_state(command, world).map_err(|e| stop(e, interrupt, None))?;
    if interrupt.is_raised() {
        return Err(Stop::Interrupted(Some(state.run_id)));
    }
    run_workflow(world, state, pipeline, spec)
}

/// The state command's run starts from, its workflow and, for run --spec, its spec, in the project command names, as
/// world's host resolves it. Errors: what each step gives, a command a Ctrl-C stopped among them, which run_command
/// takes for the interrupt.
fn starting_state(command: &Command, world: &Connected) -> Result<(RunState, Pipeline, Option<SpecFile>), OrchestratorError> {
    let cwd = match &command.target().cwd {
        Some(cwd) => cwd.clone(),
        None => current_dir()?,
    };
    let cwd = world.with.host.resolve_dir(&cwd)?;
    Ok(match command {
        Command::Run(args) => {
            let spec = args.spec.as_deref().map(read_spec_file).transpose()?;
            let (state, pipeline) = new_state(args, &cwd, spec.as_ref(), world)?;
            (state, pipeline, spec)
        }
        Command::Resume(args) => {
            let (state, pipeline) = resumed_state(args, &cwd, world)?;
            (state, pipeline, None)
        }
    })
}

/// Drive the run state describes through pipeline, by the settings saved in state, to its verdict. spec is the file
/// a new run --spec run starts from; None for any other run.
fn run_workflow(
    world: &Connected,
    state: RunState,
    pipeline: Pipeline,
    spec: Option<SpecFile>,
) -> Result<Ran, Stop> {
    let interrupt = world.with.interrupt;
    let jenkins = match state.quality_job.as_deref().filter(|j| !j.is_empty()) {
        Some(job) => Some((world.jenkins)(job).map_err(|e| stop(e, interrupt, Some(&state)))?),
        None => None,
    };
    let settings = RunSettings {
        max_rounds: state.max_rounds,
        turn_timeout: state.turn_timeout,
        agents: AgentSettings {
            permission_mode: state.permission_mode.clone(),
            models: state.models.clone(),
            kinds: state.agent_kinds.clone(),
        },
        pull_request: state.pull_request,
        max_quality_rounds: state.max_quality_rounds,
        pipeline: Some(pipeline.clone()),
        spec,
    };
    let with = Collaborators { jenkins: jenkins.as_ref(), ..world.with };
    let mut workflow = Workflow::new(with, state.clone(), settings).map_err(|e| stop(e, interrupt, Some(&state)))?;
    match workflow.run() {
        Ok(verdict) => Ok(Ran { verdict, state: workflow.state().clone(), pipeline }),
        Err(Stopped::Interrupted) => Err(Stop::Interrupted(Some(workflow.state().run_id.clone()))),
        Err(Stopped::Failed(e) | Stopped::TakenOver(e)) => Err(Stop::Failed(e)),
    }
}

/// What error e means for main: the Ctrl-C when the interrupt is pending, since a command it stopped fails for it,
/// else the error itself. state is the run's, for the hint that resumes it.
fn stop(e: OrchestratorError, interrupt: &Interrupt, state: Option<&RunState>) -> Stop {
    if interrupt.is_raised() {
        Stop::Interrupted(state.map(|s| s.run_id.clone()))
    } else {
        Stop::Failed(e)
    }
}

/// The current directory, as os.getcwd gives it. Errors: it is gone or not UTF-8.
fn current_dir() -> Result<String, OrchestratorError> {
    let dir = std::env::current_dir().map_err(|e| OrchestratorError(crate::error::os_error(&e, None)))?;
    dir.into_os_string()
        .into_string()
        .map_err(|dir| OrchestratorError(format!("the current directory {} is not UTF-8", dir.to_string_lossy())))
}

/// Print how the run ended, its verdict and its pull request or result file, and return its exit status: 0 for a
/// verdict of SUCCEEDED, EXIT_CONFLICT for one whose pull request conflicts with its base, EXIT_CHANGES_REQUESTED for
/// any other.
fn finish(verdict: &str, state: &RunState, pipeline: &Pipeline) -> i32 {
    let conflict = if state.conflicts.is_empty() {
        String::new()
    } else {
        format!(" (a draft: it conflicts with {})", state.base_branch.as_deref().unwrap_or("None"))
    };
    let result = pipeline.result().last_path(&state.dir(), state.round, state.quality_round);
    // A line stdout does not take is lost, as after a hangup, rather than a panic that would end the program before
    // interrupt::exit ends it by the SIGHUP: the status still tells.
    let _ = writeln!(
        std::io::stdout(),
        "{verdict}: {}{conflict}",
        state.pr_url.as_deref().filter(|u| !u.is_empty()).unwrap_or(&result)
    );
    if !SUCCEEDED.contains(&verdict) {
        return EXIT_CHANGES_REQUESTED;
    }
    if state.conflicts.is_empty() {
        0
    } else {
        EXIT_CONFLICT
    }
}

/// The command that resumes the run run_id names, by its key, with target's --machine and --cwd flags.
pub fn resume_command(run_id: &str, target: &[String]) -> String {
    let key = run_id.rsplit('-').next().unwrap_or(run_id);
    let argv: Vec<&str> = ["orchestrator.py", "resume", key].into_iter().chain(target.iter().map(String::as_str)).collect();
    shlex_join(&argv)
}

/// What to tell the human whose Ctrl-C, or a SIGTERM or SIGHUP, stopped the orchestrator, with the command that resumes the run of run_id,
/// target being its --machine and --cwd flags; run_id is None for a Ctrl-C before the run's state existed, and the
/// note names no command.
fn interrupted_note(run_id: Option<&str>, target: &[String]) -> String {
    let hint = run_id.map_or(String::new(), |id| format!("; resume with: {}", resume_command(id, target)));
    format!("interrupted; the role agents keep running in herdr{hint}")
}

#[cfg(test)]
mod tests;
