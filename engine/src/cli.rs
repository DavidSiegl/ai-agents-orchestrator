//! The orchestrator program's command line, as orchestrator.py's main: [`main`] reads the arguments (crate::cli::args),
//! connects to herdr and the project's host, runs the command and turns how it ended into the program's exit status
//! and its last lines. A command joins as a [`Command`] arm of [`run_command`], given the same [`Connected`], or of
//! [`herdrless_command`] for one that needs no herdr; ticket 09a has `run`, `resume`, the commands about a project's
//! runs (crate::cli::listing, prune, close) and `workflows`.

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
use crate::py::json::Json;
use crate::pypi::PyPI;
use crate::runs::{local_host, pid_alive};
use crate::spec_file::{read_spec_file, SpecFile};
use crate::state::RunState;
use crate::workflow::{Collaborators, LocalHuman, RunSettings, Stopped, SystemClocks, Workflow};

pub mod args;
mod close;
pub mod listing;
pub mod prune;
mod start;
mod workflows;

pub use args::{parse_args, Command, RunsCommand, Target};
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
    /// Writes text on the program's standard output as it is, a line with its line end: what a command prints.
    pub print: &'a dyn Fn(&str),
}

impl Connected<'_> {
    /// Print line and a line end on standard output, as Python's print(line).
    pub fn say(&self, line: &str) {
        (self.print)(&format!("{line}\n"));
    }

    /// Print text on standard output as it is, as Python's print(text, end="").
    pub fn print(&self, text: &str) {
        (self.print)(text);
    }
}

/// How a command ended, when it ended as it should.
pub(super) enum Ended {
    /// A run or a resume, to its verdict.
    Ran(Box<Ran>),
    /// Any other command, which has printed what it had, with the program's exit status.
    Status(i32),
}

/// How a command that ran a workflow ended, for the program's last line and exit status (finish).
pub(super) struct Ran {
    /// What Workflow::run returned: the run's verdict; None prints as Python's str(None) does, "None".
    verdict: Option<String>,
    /// The run's state as the Workflow left it, at its end.
    state: RunState,
    /// The run's workflow.
    pipeline: Pipeline,
}

/// Why a command did not end as it should.
pub(super) enum Stop {
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
    let print = |text: &str| {
        // A line stdout does not take is lost, as after a hangup, rather than a panic that would end the program before
        // interrupt::exit ends it by the SIGHUP: the status still tells.
        let mut stdout = std::io::stdout().lock();
        let _ = stdout.write_all(text.as_bytes()).and_then(|()| stdout.flush());
    };
    let ended = match herdrless_command(&command, &config, &print, interrupt) {
        Some(ended) => ended,
        None => connect(&command, interrupt, &LocalHuman::new(interrupt), &config, &print),
    };
    ended_status(ended, &command, &print)
}

/// Run command if it is one that needs no herdr pane, on this machine, printing by print, and say how it ended; None
/// for any other command, which main runs after connect. Python's herdrless_command.
fn herdrless_command(
    command: &Command,
    config: &ConfigDir,
    print: &dyn Fn(&str),
    interrupt: &Interrupt,
) -> Option<Result<Ended, Stop>> {
    // Workflows are on this machine, so listing them needs no herdr.
    let Command::Workflows(reference) = command else { return None };
    let ran = workflows::workflows_command(config, reference.as_deref(), print);
    Some(match ran {
        Err(e) => Err(stop(e, interrupt, None)),
        Ok(()) if interrupt.is_raised() => Err(Stop::Interrupted(None)),
        Ok(()) => Ok(Ended::Status(0)),
    })
}

/// The exit status of a command that ended so, its last line printed: finish's for a run, by print, the command's
/// own for any other, else the error or the interrupted note, on stderr.
fn ended_status(ended: Result<Ended, Stop>, command: &Command, print: &dyn Fn(&str)) -> i32 {
    match ended {
        Ok(Ended::Ran(ran)) => finish(ran.verdict.as_deref().unwrap_or("None"), &ran.state, &ran.pipeline, print),
        Ok(Ended::Status(status)) => status,
        // A line stderr does not take is lost, as after a hangup (finish says why).
        Err(Stop::Failed(e)) => {
            let _ = writeln!(std::io::stderr(), "error: {e}");
            EXIT_ERROR
        }
        Err(Stop::Interrupted(run_id)) => {
            let target = command.target().map(Target::flags).unwrap_or_default();
            let _ = writeln!(std::io::stderr(), "{}", interrupted_note(run_id.as_deref(), &target));
            EXIT_INTERRUPTED
        }
    }
}

/// Connect to the herdr and the host command names, on this machine (which must be a herdr pane) or over SSH to its
/// --machine, and run it, printing by print. Python's connect, then the rest of main.
fn connect(
    command: &Command,
    interrupt: &Interrupt,
    human: &LocalHuman,
    config: &ConfigDir,
    print: &dyn Fn(&str),
) -> Result<Ended, Stop> {
    let stopped = |e: OrchestratorError| stop(e, interrupt, None);
    let machine = command.target().and_then(|t| t.machine.as_deref());
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
        print,
    };
    run_command(command, &world)
}

/// Whether this process runs in a pane herdr manages, which a run without a machine needs: without a machine, herdr
/// commands target whichever session is focused, and only such a pane knows it is talking to its own.
fn in_herdr_pane() -> bool {
    std::env::var_os("HERDR_ENV").is_some_and(|v| v == "1")
}

/// Run command in world, in the project its target names, as world's host resolves it: a run or a resume, to its
/// verdict, or a command about the project's runs. A command the Interrupt stopped, by failing or by its end, ends
/// interrupted.
fn run_command(command: &Command, world: &Connected) -> Result<Ended, Stop> {
    let interrupt = world.with.interrupt;
    let stopped = |e: OrchestratorError| stop(e, interrupt, None);
    let cwd = project_dir(command.target(), world).map_err(stopped)?;
    let (state, pipeline, spec) = match command {
        Command::Run(args) => new_run(args, &cwd, world).map_err(stopped)?,
        Command::Resume(args) => {
            let (state, pipeline) = resumed_state(args, &cwd, world).map_err(stopped)?;
            (state, pipeline, None)
        }
        Command::Runs(target, runs) => {
            runs_command(runs, world, &cwd, &target.flags()).map_err(stopped)?;
            return if interrupt.is_raised() { Err(Stop::Interrupted(None)) } else { Ok(Ended::Status(0)) };
        }
        Command::Workflows(_) => unreachable!("main runs workflows without herdr (herdrless_command)"),
    };
    if interrupt.is_raised() {
        return Err(Stop::Interrupted(Some(state.run_id)));
    }
    run_workflow(world, state, pipeline, spec).map(|ran| Ended::Ran(Box::new(ran)))
}

/// The project directory target names, the current directory without one, as world's host resolves it. Errors: the
/// current directory cannot be read, or the host cannot resolve the directory.
fn project_dir(target: Option<&Target>, world: &Connected) -> Result<String, OrchestratorError> {
    let cwd = match target.and_then(|t| t.cwd.clone()) {
        Some(cwd) => cwd,
        None => current_dir()?,
    };
    world.with.host.resolve_dir(&cwd)
}

/// The state a new run of args in the project at cwd starts from, its workflow and, for run --spec, its spec. A run
/// that opens a pull request first prunes the branches of the project's earlier runs (prune::sweep_branches).
/// Errors: what each step gives, a command a Ctrl-C stopped among them, which run_command takes for the interrupt.
fn new_run(
    args: &args::RunArgs,
    cwd: &str,
    world: &Connected,
) -> Result<(RunState, Pipeline, Option<SpecFile>), OrchestratorError> {
    let spec = args.spec.as_deref().map(read_spec_file).transpose()?;
    // Only a run that opens a pull request: a --no-pr run's project may have no gh.
    if !args.no_pr {
        prune::sweep_branches(world, cwd)?;
    }
    let (state, pipeline) = new_state(args, cwd, spec.as_ref(), world)?;
    Ok((state, pipeline, spec))
}

/// Run runs, a command about the runs of the project at cwd, in world, printing what it prints; target is the
/// --machine and --cwd flags the human gave, which a resume command it prints repeats. Errors: what the command
/// gives.
fn runs_command(runs: &RunsCommand, world: &Connected, cwd: &str, target: &[String]) -> Result<(), OrchestratorError> {
    let host = world.with.host;
    match runs {
        RunsCommand::List { json: true, pr_state } => {
            let mut records: Vec<_> = host
                .run_states(cwd)?
                .iter()
                .map(|(age, s)| listing::run_record(s, *age, world.local_host, world.with.pid_alive, target))
                .collect();
            if *pr_state {
                listing::fill_pr_states(world, cwd, &mut records)?;
            }
            let document: Json = Json::Object(
                [
                    ("version", Json::from(listing::RUN_RECORD_VERSION)),
                    ("runs", Json::Array(records.into_iter().map(Json::Object).collect())),
                ]
                .into_iter()
                .collect(),
            );
            world.say(&document.dumps_indented(2));
        }
        RunsCommand::List { json: false, .. } => listing::print_runs(world, &host.run_states(cwd)?, target),
        RunsCommand::Show(run_ref) => listing::show_run(world, cwd, run_ref, target)?,
        RunsCommand::Close(run_ref) => {
            close::close_run(world, cwd, run_ref)?;
        }
        RunsCommand::Metrics => {
            for record in host.run_metrics(cwd)? {
                world.say(&record.dumps());
            }
        }
        RunsCommand::Prune { dry_run } => prune::print_pruned(world, &prune::prune_branches(world, cwd, *dry_run, true)?),
    }
    Ok(())
}

/// Set key to recorded in the run's state.json at path on host, unless the run has saved since it was read as saved;
/// for what prune and close write about a run they do not drive. The file keeps its modification time, which is the
/// run's heartbeat: a stale run must not look live. meanwhile says what was being done, for the error.
///
/// Errors: the file is gone or changed (naming key and meanwhile), is not JSON, or cannot be read or written.
pub fn record_in_state(
    host: &dyn crate::host::Host,
    path: &str,
    saved: &Json,
    key: &str,
    recorded: Json,
    meanwhile: &str,
) -> Result<(), OrchestratorError> {
    let text = host.read(path)?;
    let unchanged = match &text {
        None => false,
        Some(text) => Json::parse(text).map_err(|e| OrchestratorError(format!("corrupt {path}: {e}")))? == *saved,
    };
    if !unchanged {
        return Err(OrchestratorError(format!("{path} changed while {meanwhile}; {key} is not recorded")));
    }
    let Json::Object(mut updated) = saved.clone() else {
        return Err(OrchestratorError(format!("corrupt {path}: not a JSON object")));
    };
    updated.insert(key, recorded);
    host.write(path, &format!("{}\n", Json::Object(updated).dumps()), true)
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

/// Print how the run ended by print, its verdict and its pull request or result file, and return its exit status: 0
/// for a verdict of SUCCEEDED, EXIT_CONFLICT for one whose pull request conflicts with its base,
/// EXIT_CHANGES_REQUESTED for any other.
fn finish(verdict: &str, state: &RunState, pipeline: &Pipeline, print: &dyn Fn(&str)) -> i32 {
    let conflict = if state.conflicts.is_empty() {
        String::new()
    } else {
        format!(" (a draft: it conflicts with {})", state.base_branch.as_deref().unwrap_or("None"))
    };
    let result = pipeline.result().last_path(&state.dir(), state.round, state.quality_round);
    print(&format!("{verdict}: {}{conflict}\n", state.pr_url.as_deref().filter(|u| !u.is_empty()).unwrap_or(&result)));
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
mod fixture;
#[cfg(test)]
mod tests;
