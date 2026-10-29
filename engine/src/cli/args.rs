//! The command line as orchestrator.py's parse_args reads it, on clap: the subcommands with their flags, and the
//! checks argparse's types cannot make (check_flag_values, check_spec_args). Ticket 08 has run and resume only; the
//! other subcommands of parse_args join [`parser`] and [`Command`] with ticket 09.
//!
//! The flags come in groups that several subcommands share, as argparse's parent parsers: [`Target`] (where the
//! project is), [`Settings`] (how a run goes, which resume may change) and [`Starts`] (what a run is started with).

use std::ffi::OsString;

use clap::builder::PossibleValuesParser;
use clap::error::ErrorKind;
use clap::{Arg, ArgAction, ArgMatches};

use crate::constants::{
    AGENT_KINDS, DEFAULT_AGENT, DEFAULT_MAX_QUALITY_ROUNDS, DEFAULT_MAX_ROUNDS, DEFAULT_TURN_TIMEOUT, ROLE_LABELS,
};
use crate::env::ConfigDir;
use crate::pipeline::WORKFLOWS;
use crate::workflow_file::workflow_names;

/// What the command line asks for: one subcommand and its flags.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Command {
    /// `run`: start a run of a workflow for a task.
    Run(RunArgs),
    /// `resume`: go on with a run whose orchestrator has stopped.
    Resume(ResumeArgs),
}

/// Where the project is: `--machine` and `--cwd`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Target {
    /// The saved herdr machine the agents run on; None for this one. Never empty: an empty --machine is none.
    pub machine: Option<String>,
    /// The project directory, on the machine if there is one; None for the current directory.
    pub cwd: Option<String>,
}

/// How a run goes, as run and resume take them. Each is None when not given: run then takes the default, and resume
/// keeps what the run was started with.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Settings {
    /// --max-rounds: review rounds before giving up.
    pub max_rounds: Option<i64>,
    /// --timeout: seconds a turn may take.
    pub timeout: Option<i64>,
    /// --permission-mode, in Claude Code's names, as given; Some("") when given empty.
    pub permission_mode: Option<String>,
    /// --model: the model of every role.
    pub model: Option<String>,
    /// --spec-model, --build-model and --review-model, by the role of ROLE_LABELS they name.
    pub label_models: Vec<(String, String)>,
    /// Each --role-model ROLE=MODEL, in order.
    pub role_model: Vec<(String, String)>,
    /// --agent: the harness of every role, one of AGENT_KINDS.
    pub agent: Option<String>,
    /// Each --role-agent ROLE=KIND, in order, KIND one of AGENT_KINDS.
    pub role_agent: Vec<(String, String)>,
    /// --max-quality-rounds: analyses per round with the quality gate.
    pub max_quality_rounds: Option<i64>,
}

/// What a new run is started with, which a resumed one keeps: its workflow, where it works and its quality gate.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Starts {
    /// --workflow: a built-in workflow's name or a workflow file's in the configuration directory; "default" unless
    /// given, and unused when workflow_file is given.
    pub workflow: String,
    /// --workflow-file: the path of a workflow file on this machine.
    pub workflow_file: Option<String>,
    /// --worktree: build in a git worktree of the run's own.
    pub worktree: bool,
    /// --quality-gate: the Jenkins job's full name.
    pub quality_gate: Option<String>,
}

/// `run`'s flags and arguments.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunArgs {
    pub target: Target,
    pub settings: Settings,
    pub starts: Starts,
    /// What to build; None only with spec, whose title is then the task.
    pub task: Option<String>,
    /// --spec: the spec file, on this machine, that the run starts from instead of an interview.
    pub spec: Option<String>,
    /// --no-pr: leave the change uncommitted instead of opening a pull request.
    pub no_pr: bool,
}

/// `resume`'s flags and arguments.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResumeArgs {
    pub target: Target,
    pub settings: Settings,
    /// The run's id, or the key at its end.
    pub run_ref: String,
    /// --force: take over a run that still looks alive.
    pub force: bool,
    /// --quality-gate: turn the gate on with this job.
    pub quality_gate: Option<String>,
    /// --no-quality-gate: turn the run's gate off for good.
    pub no_quality_gate: bool,
}

impl Command {
    /// Where the project is, as the human gave it.
    pub fn target(&self) -> &Target {
        match self {
            Command::Run(a) => &a.target,
            Command::Resume(a) => &a.target,
        }
    }
}

impl Target {
    /// The --machine and --cwd flags as the human gave them, to repeat in a command the human is told to run.
    pub fn flags(&self) -> Vec<String> {
        let mut out = Vec::new();
        if let Some(machine) = &self.machine {
            out.extend(["--machine".to_string(), machine.clone()]);
        }
        if let Some(cwd) = &self.cwd {
            out.extend(["--cwd".to_string(), cwd.clone()]);
        }
        out
    }
}

/// What the human wrote after the program's name, read: the Command, or the clap error that ends the program instead.
///
/// The error is a usage error (exit status 2, its text naming the problem, for clap's print), or the help a --help
/// asked for (exit status 0). config is where the workflow files that --workflow offers are.
pub fn parse_args(argv: impl IntoIterator<Item = OsString>, config: &ConfigDir) -> Result<Command, clap::Error> {
    let mut p = parser(config);
    let matches = p.try_get_matches_from_mut(std::iter::once(OsString::from("orchestrator")).chain(argv))?;
    let command = match matches.subcommand() {
        Some(("run", m)) => Command::Run(RunArgs {
            target: target(m),
            settings: settings(m),
            starts: Starts {
                workflow: string(m, "workflow").unwrap_or_else(|| "default".to_string()),
                workflow_file: string(m, "workflow-file"),
                worktree: m.get_flag("worktree"),
                quality_gate: string(m, "quality-gate"),
            },
            task: string(m, "task"),
            spec: string(m, "spec"),
            no_pr: m.get_flag("no-pr"),
        }),
        Some(("resume", m)) => Command::Resume(ResumeArgs {
            target: target(m),
            settings: settings(m),
            run_ref: string(m, "run_ref").unwrap_or_default(),
            force: m.get_flag("force"),
            quality_gate: string(m, "quality-gate"),
            no_quality_gate: m.get_flag("no-quality-gate"),
        }),
        _ => unreachable!("clap requires one of the subcommands parser defines"),
    };
    check_spec_args(&matches, &mut p)?;
    check_flag_values(&command, &mut p)?;
    Ok(command)
}

/// The program's parser: its subcommands with their flags and help.
pub fn parser(config: &ConfigDir) -> clap::Command {
    let run = clap::Command::new("run")
        .about("run the handoff workflow for a task")
        .args(target_args())
        .args(settings_args())
        .args(starts_args(config))
        .arg(Arg::new("task").help(
            "what to build, as you would tell the Spec Collector; optional with --spec, which then takes the \
             spec's title",
        ))
        // Run only: a resumed run already has its spec.
        .arg(Arg::new("spec").long("spec").value_name("FILE").help(
            "skip the interview: copy the spec at FILE, on this machine, into the run as the first step's file, and \
             start at the step after it. Needs a workflow whose first step writes the spec, as default's does",
        ))
        // Run only: a resumed run may already be on its own branch, so whether it ends in a pull request is fixed.
        .arg(Arg::new("no-pr").long("no-pr").action(ArgAction::SetTrue).help(
            "leave the change uncommitted in the working tree and the workspace open, instead of opening a pull \
             request",
        ));
    let resume = clap::Command::new("resume")
        .about("continue a run whose orchestrator has stopped")
        .long_about(
            "Continue a run whose orchestrator has stopped. A settings flag overrides what the run was started \
             with; an unset one keeps it, not the default shown.",
        )
        .args(target_args())
        .args(settings_args())
        .arg(Arg::new("run_ref").value_name("RUN").required(true).help("the run id, or the six-character key at its end"))
        .arg(Arg::new("force").long("force").action(ArgAction::SetTrue).help("take over a run that still looks alive"))
        // Hidden and refused by check_spec_args: a resumed run keeps its spec, and an abbreviation of --spec-model
        // must not be taken for it.
        .arg(Arg::new("spec").long("spec").num_args(0..=1).default_missing_value("").hide(true))
        .arg(
            Arg::new("quality-gate").long("quality-gate").value_name("JOB").conflicts_with("no-quality-gate").help(
                "turn the quality gate on with this Jenkins job, as run --quality-gate has it, for a run started \
                 without it; a Builder report not yet judged is judged before the Reviewer starts, and otherwise \
                 the gate starts at the Builder's next turn",
            ),
        )
        .arg(Arg::new("no-quality-gate").long("no-quality-gate").action(ArgAction::SetTrue).help(
            "turn the run's quality gate off for good, deleting its refs on origin; an analysis under way is \
             dropped, and the Reviewer is told what the gate last found",
        ));
    clap::Command::new("orchestrator")
        .about(
            "Role-based handoff between agent sessions in herdr, each role in the harness of its choice; by default \
             Spec Collector -> Builder -> Reviewer, all in Claude Code.",
        )
        .subcommand_required(true)
        .arg_required_else_help(true)
        // argparse takes an unambiguous abbreviation of a long flag.
        .infer_long_args(true)
        .subcommands([run, resume])
}

/// --machine and --cwd.
fn target_args() -> Vec<Arg> {
    vec![
        Arg::new("machine").long("machine").help("saved herdr machine to run the agents on"),
        Arg::new("cwd").long("cwd").help("project directory (on the machine, if --machine is given)"),
    ]
}

/// The flags of Settings.
fn settings_args() -> Vec<Arg> {
    let number = || clap::value_parser!(i64);
    let mut args = vec![
        Arg::new("max-rounds")
            .long("max-rounds")
            .value_parser(number())
            .allow_negative_numbers(true)
            .help(format!("review rounds before giving up (default {DEFAULT_MAX_ROUNDS})")),
        Arg::new("timeout")
            .long("timeout")
            .value_parser(number())
            .allow_negative_numbers(true)
            .help(format!("seconds a Builder or Reviewer turn may take (default {DEFAULT_TURN_TIMEOUT})")),
        Arg::new("permission-mode").long("permission-mode").help(
            "permission mode for every role, by Claude Code's names, e.g. auto or acceptEdits. claude takes any as \
             it is. gemini gets --approval-mode default, auto_edit or yolo for default, acceptEdits or \
             bypassPermissions; codex nothing for default, --full-auto for acceptEdits, \
             --dangerously-bypass-approvals-and-sandbox for bypassPermissions and --sandbox read-only for plan. Any \
             other mode, and every mode for opencode and pi, is not passed, and the log says so",
        ),
        Arg::new("model")
            .long("model")
            .help("model for every role, passed to its harness as --model, e.g. sonnet or claude-opus-5-5"),
    ];
    for (role, label) in ROLE_LABELS {
        args.push(
            Arg::new(format!("{role}-model"))
                .long(format!("{role}-model"))
                .value_name("MODEL")
                .help(format!("model for the {label}; overrides --model")),
        );
    }
    args.extend([
        Arg::new("role-model")
            .long("role-model")
            .value_name("ROLE=MODEL")
            .action(ArgAction::Append)
            .value_parser(role_model_arg)
            .help(
                "model for one role of the workflow, by its key; overrides --model and the workflow's own model for \
                 it; repeatable",
            ),
        Arg::new("agent").long("agent").value_name("KIND").value_parser(PossibleValuesParser::new(AGENT_KINDS)).help(
            format!(
                "agent harness for every role, one of {}; overrides the agent key of a role in the workflow file \
                 (default {DEFAULT_AGENT}). Each harness a run uses needs its CLI on PATH and `herdr integration \
                 install KIND` where the agents run",
                AGENT_KINDS.join(", ")
            ),
        ),
        Arg::new("role-agent")
            .long("role-agent")
            .value_name("ROLE=KIND")
            .action(ArgAction::Append)
            .value_parser(role_agent_arg)
            .help("agent harness for one role of the workflow, by its key; overrides --agent; repeatable"),
        Arg::new("max-quality-rounds")
            .long("max-quality-rounds")
            .value_name("N")
            .value_parser(number())
            .allow_negative_numbers(true)
            .help(format!(
                "with --quality-gate: quality builds per review round before the Reviewer gets the change anyway \
                 (default {DEFAULT_MAX_QUALITY_ROUNDS})"
            )),
    ]);
    args
}

/// The flags of Starts. --workflow offers the workflows in config, or the built-in ones where that cannot be listed,
/// as the workflows command then says why.
fn starts_args(config: &ConfigDir) -> Vec<Arg> {
    let offered = workflow_names(config).unwrap_or_else(|_| WORKFLOWS.iter().map(|n| n.to_string()).collect());
    let workflows_dir = config.workflows_dir();
    vec![
        Arg::new("workflow")
            .long("workflow")
            .value_parser(PossibleValuesParser::new(offered))
            .conflicts_with("workflow-file")
            .help(format!(
                "the roles and handoffs the run goes through: a built-in workflow or one in {workflows_dir} \
                 (default default); see the workflows command. With --spec, and for drain, its first step must write \
                 the spec"
            )),
        Arg::new("workflow-file")
            .long("workflow-file")
            .value_name("FILE")
            .help("like --workflow, for the workflow file at FILE, on this machine"),
        Arg::new("worktree").long("worktree").action(ArgAction::SetTrue).help(
            "build in a git worktree of the run's own, .orchestrator/worktrees/<key>, from origin's version of the \
             checked-out branch, instead of in the checkout itself, which then need not be clean and stays free to \
             use. Needs a branch checked out and an origin. The worktree is removed once the pull request is open; \
             when the run fails, or for run --no-pr, it stays",
        ),
        Arg::new("quality-gate").long("quality-gate").value_name("JOB").help(
            "after each Builder turn, have this Jenkins job (its full name, folders included) judge the change with \
             `gate.py`, and send the findings back to the Builder; needs JENKINS_URL, JENKINS_USER and \
             JENKINS_TOKEN, from the environment or ~/.config/ai-agents-orchestrator/ci.env",
        ),
    ]
}

/// ROLE=MODEL, each part non-empty.
fn role_model_arg(text: &str) -> Result<(String, String), String> {
    match text.split_once('=') {
        Some((role, model)) if !role.is_empty() && !model.is_empty() => Ok((role.to_string(), model.to_string())),
        _ => Err(format!("expected ROLE=MODEL, not {}", crate::py::text::repr(text))),
    }
}

/// ROLE=KIND, ROLE non-empty and KIND one of AGENT_KINDS.
fn role_agent_arg(text: &str) -> Result<(String, String), String> {
    match text.split_once('=') {
        Some((role, kind)) if !role.is_empty() && AGENT_KINDS.contains(&kind) => {
            Ok((role.to_string(), kind.to_string()))
        }
        _ => Err(format!(
            "expected ROLE=KIND, where KIND is one of {}; not {}",
            AGENT_KINDS.join(", "),
            crate::py::text::repr(text)
        )),
    }
}

/// A usage error for a run with neither a task nor --spec, or a resume given --spec.
fn check_spec_args(matches: &ArgMatches, p: &mut clap::Command) -> Result<(), clap::Error> {
    match matches.subcommand() {
        Some(("run", m)) if !m.contains_id("task") && !m.contains_id("spec") => {
            Err(subcommand_error(p, "run", "a task or --spec FILE is needed"))
        }
        Some(("resume", m)) if m.contains_id("spec") => {
            Err(subcommand_error(p, "resume", "unrecognized arguments: --spec; a resumed run keeps the spec it has"))
        }
        _ => Ok(()),
    }
}

/// A usage error for flag values the parser cannot rule out: --machine without --cwd, a --max-rounds or
/// --max-quality-rounds below 1, a run given --max-quality-rounds without --quality-gate, a resume given it with
/// --no-quality-gate, and a --quality-gate that is not a Jenkins job's full name.
fn check_flag_values(command: &Command, p: &mut clap::Command) -> Result<(), clap::Error> {
    let error = |p: &mut clap::Command, message: &str| Err(p.error(ErrorKind::ValueValidation, message));
    let (target, settings) = match command {
        Command::Run(a) => (&a.target, &a.settings),
        Command::Resume(a) => (&a.target, &a.settings),
    };
    let (quality_gate, no_quality_gate) = match command {
        Command::Run(a) => (a.starts.quality_gate.as_deref(), false),
        Command::Resume(a) => (a.quality_gate.as_deref(), a.no_quality_gate),
    };
    if target.machine.is_some() && target.cwd.as_deref().is_none_or(str::is_empty) {
        return error(p, "--cwd is required with --machine");
    }
    if settings.max_rounds.is_some_and(|n| n < 1) {
        return error(p, "--max-rounds must be at least 1");
    }
    if let Some(n) = settings.max_quality_rounds {
        if n < 1 {
            return error(p, "--max-quality-rounds must be at least 1");
        }
        if matches!(command, Command::Run(_)) && quality_gate.is_none_or(str::is_empty) {
            return error(p, "--max-quality-rounds needs --quality-gate");
        }
        if no_quality_gate {
            return error(p, "--max-quality-rounds does not apply with --no-quality-gate");
        }
    }
    if quality_gate.is_some_and(|job| !is_job_name(job)) {
        return error(p, "--quality-gate takes a Jenkins job's full name, such as folder/job");
    }
    Ok(())
}

/// Whether job is a Jenkins job's full name: names joined by "/", none empty.
fn is_job_name(job: &str) -> bool {
    job.split('/').all(|part| !part.is_empty())
}

/// A usage error of the subcommand name, with its usage.
fn subcommand_error(p: &mut clap::Command, name: &str, message: &str) -> clap::Error {
    match p.find_subcommand_mut(name) {
        Some(sub) => sub.error(ErrorKind::MissingRequiredArgument, message),
        None => p.error(ErrorKind::MissingRequiredArgument, message),
    }
}

/// The value of the flag or argument id as given; None when it was not.
fn string(m: &ArgMatches, id: &str) -> Option<String> {
    m.get_one::<String>(id).cloned()
}

/// The Target in m.
fn target(m: &ArgMatches) -> Target {
    Target { machine: string(m, "machine").filter(|s| !s.is_empty()), cwd: string(m, "cwd").filter(|s| !s.is_empty()) }
}

/// The Settings in m.
fn settings(m: &ArgMatches) -> Settings {
    let pairs = |id: &str| m.get_many::<(String, String)>(id).map(|v| v.cloned().collect()).unwrap_or_default();
    Settings {
        max_rounds: m.get_one::<i64>("max-rounds").copied(),
        timeout: m.get_one::<i64>("timeout").copied(),
        permission_mode: string(m, "permission-mode"),
        model: string(m, "model"),
        label_models: ROLE_LABELS
            .iter()
            .filter_map(|(role, _)| string(m, &format!("{role}-model")).map(|model| (role.to_string(), model)))
            .collect(),
        role_model: pairs("role-model"),
        agent: string(m, "agent"),
        role_agent: pairs("role-agent"),
        max_quality_rounds: m.get_one::<i64>("max-quality-rounds").copied(),
    }
}

#[cfg(test)]
mod tests;
