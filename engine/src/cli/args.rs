//! The command line as orchestrator.py's parse_args reads it, on clap: the subcommands with their flags, and the
//! checks argparse's types cannot make (check_flag_values, check_spec_args). Ticket 09a has run, resume, the commands
//! about a project's runs and workflows, and 09b judged-in-quality; drain, refresh and doctor join [`parser`] and
//! [`Command`] with the rest of ticket 09b; gui is not ported (the rust-engine spec's Decisions), and serve is ticket
//! 10's.
//!
//! The flags come in groups that several subcommands share, as argparse's parent parsers: [`Target`] (where the
//! project is), [`Settings`] (how a run goes, which resume may change) and [`Starts`] (what a run is started with).

use std::ffi::OsString;

use clap::builder::PossibleValuesParser;
use clap::error::ErrorKind;
use clap::{Arg, ArgAction, ArgMatches};

use crate::constants::{
    AGENT_KINDS, CLOSED_BRANCH_GRACE, DEFAULT_AGENT, DEFAULT_MAX_QUALITY_ROUNDS, DEFAULT_MAX_ROUNDS,
    DEFAULT_TURN_TIMEOUT, EXIT_ERROR, REMOTE, ROLE_LABELS,
};
use crate::cli::listing::RUN_RECORD_VERSION;
use crate::pipeline::METRICS_FILE;
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
    /// A command about the runs of the project at the Target, which starts none: Python's RUNS_COMMANDS.
    Runs(Target, RunsCommand),
    /// `workflows`: list the workflows, or print the one named, a name or a file's path, as a workflow file.
    Workflows(Option<String>),
    /// `judged-in-quality`: whether a build of the quality job already judged the current directory's HEAD as a
    /// pull request's build into the branch would.
    JudgedInQuality {
        /// --quality-gate: the quality job's full name.
        job: String,
        /// --target: the branch the pull request would merge into.
        branch: String,
    },
}

/// A command about a project's runs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RunsCommand {
    /// `list`: each run's line; with json, one JSON document of run records instead, each with its pull request's
    /// state from gh when pr_state.
    List { json: bool, pr_state: bool },
    /// `metrics`: the metrics.json of each run that is done, a line each.
    Metrics,
    /// `show`: one run in full, by its id or key.
    Show(String),
    /// `prune`: delete the branches of runs whose pull request is done; with dry_run, only say what would go.
    Prune { dry_run: bool },
    /// `close`: clean up after a run that is finished, failed or stale, by its id or key.
    Close(String),
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
    /// Where the project is.
    pub target: Target,
    /// How the run goes, each unset one at its default.
    pub settings: Settings,
    /// What the run is started with.
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
    /// Where the project is, and the machine this resume addresses, which the run then saves.
    pub target: Target,
    /// The run's settings this resume changes; each unset one keeps the saved one.
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
    /// Where the project is, as the human gave it; None for workflows and judged-in-quality, which take no
    /// --machine or --cwd.
    pub fn target(&self) -> Option<&Target> {
        match self {
            Command::Run(a) => Some(&a.target),
            Command::Resume(a) => Some(&a.target),
            Command::Runs(target, _) => Some(target),
            Command::Workflows(_) | Command::JudgedInQuality { .. } => None,
        }
    }

    /// The run's settings as the flags give them; None for a command that starts no run.
    pub fn settings(&self) -> Option<&Settings> {
        match self {
            Command::Run(a) => Some(&a.settings),
            Command::Resume(a) => Some(&a.settings),
            _ => None,
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
            starts: starts(m),
            task: string(m, "task"),
            spec: string(m, "spec"),
            no_pr: m.get_flag("no-pr"),
        }),
        Some(("judged-in-quality", m)) => Command::JudgedInQuality {
            job: string(m, "quality-gate").unwrap_or_default(),
            branch: string(m, "target").unwrap_or_default(),
        },
        Some(("resume", m)) => Command::Resume(ResumeArgs {
            target: target(m),
            settings: settings(m),
            run_ref: string(m, "run_ref").unwrap_or_default(),
            force: m.get_flag("force"),
            quality_gate: string(m, "quality-gate"),
            no_quality_gate: m.get_flag("no-quality-gate"),
        }),
        Some(("list", m)) => {
            Command::Runs(target(m), RunsCommand::List { json: m.get_flag("json"), pr_state: m.get_flag("pr-state") })
        }
        Some(("metrics", m)) => Command::Runs(target(m), RunsCommand::Metrics),
        Some(("show", m)) => Command::Runs(target(m), RunsCommand::Show(string(m, "run_ref").unwrap_or_default())),
        Some(("prune", m)) => Command::Runs(target(m), RunsCommand::Prune { dry_run: m.get_flag("dry-run") }),
        Some(("close", m)) => Command::Runs(target(m), RunsCommand::Close(string(m, "run_ref").unwrap_or_default())),
        Some(("workflows", m)) => Command::Workflows(string(m, "workflow")),
        _ => unreachable!("clap requires one of the subcommands parser defines"),
    };
    check_spec_args(&matches, &mut p)?;
    if let Command::Runs(_, RunsCommand::List { json: false, pr_state: true }) = command {
        return Err(subcommand_error(&mut p, "list", "--pr-state needs --json"));
    }
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
        .arg(Arg::new("run_ref").value_name("RUN").required(true).help(RUN_REF_HELP))
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
    let run_ref = || Arg::new("run_ref").value_name("RUN").required(true).help(RUN_REF_HELP);
    let list = clap::Command::new("list")
        .about("list the runs in the project directory")
        .long_about("List the runs in the project directory: each run's id, phase, round and outcome, and its task.")
        .args(target_args())
        .arg(Arg::new("json").long("json").action(ArgAction::SetTrue).help(format!(
            "print one JSON document instead, {{\"version\": {RUN_RECORD_VERSION}, \"runs\": [...]}}, with a record \
             of each run's state"
        )))
        .arg(
            Arg::new("pr-state")
                .long("pr-state")
                .action(ArgAction::SetTrue)
                .help("with --json, ask gh for the state of each run's pull request, one call per run"),
        );
    let metrics = clap::Command::new("metrics")
        .about("print the metrics of every run that is done, a JSON line each")
        .long_about(format!(
            "Print the {METRICS_FILE} of every run in the project directory that is done, one JSON object per line, \
             oldest run first, for jq or a spreadsheet: its outcome, roles, turns with their times and tokens, and \
             quality rounds. A run that is not done has none and is left out."
        ))
        .args(target_args());
    let show = clap::Command::new("show")
        .about("print one run in full: its state, files and last review")
        .long_about(
            "Print one run: its line as list shows it and its whole task, its workflow, branch and pull request, \
             each role's agent, the files in its run directory, and the last review.",
        )
        .args(target_args())
        .arg(run_ref());
    let prune = clap::Command::new("prune")
        .about("delete the branches of runs whose pull request is done")
        .long_about(format!(
            "Delete the branch of each run whose pull request is merged, or closed for {} days, locally and on \
             {REMOTE}, and print what became of each. A branch with commits its pull request lacks, or one checked \
             out, is kept. Every run that opens a pull request does this first, except for branches kept before.",
            CLOSED_BRANCH_GRACE / 86400
        ))
        .args(target_args())
        .arg(
            Arg::new("dry-run")
                .long("dry-run")
                .action(ArgAction::SetTrue)
                .help("print what would be deleted, and delete nothing"),
        );
    let close = clap::Command::new("close")
        .about("clean up after a finished or failed run: its workspace, worktree and branch")
        .long_about(
            "Clean up after a run that is finished, failed or stale, and record it in the run's state, so that \
             resume refuses it. Closes the run's herdr workspace. A failed or stale --worktree run also loses its \
             worktree, uncommitted changes included, which are listed first, and its branch, unless that has \
             commits beyond the run's base. A finished run keeps its worktree; an in-place run keeps its checkout \
             and branch. A running run is refused.",
        )
        .args(target_args())
        .arg(run_ref());
    let workflows = clap::Command::new("workflows")
        .about("list the workflows, or print one as a workflow file")
        .long_about(format!(
            "Without WORKFLOW, list the workflows `run --workflow` offers: the built-in ones and the workflow files \
             in {}. With it, print that workflow, or the file at that path, as a workflow file, a starting point \
             for your own.",
            config.workflows_dir()
        ))
        .arg(Arg::new("workflow").value_name("WORKFLOW").help("a workflow's name, or a file's path"));
    let judged = clap::Command::new("judged-in-quality")
        .about("exit 0 when a quality build already judged HEAD as its pull request's build would")
        .long_about(format!(
            "Exit 0 when a build of the quality job JOB already judged HEAD as a pull request's build into BRANCH \
             would, so that build need not judge it again; docs/quality-gate.md says when. Fetches BRANCH, and the \
             main commit a build names. Otherwise, and on any error, exit {EXIT_ERROR}, saying why in one line on \
             standard error. Needs JENKINS_URL, JENKINS_USER and JENKINS_TOKEN, from the environment or {}.",
            config.ci_env_path()
        ))
        .arg(
            Arg::new("quality-gate")
                .long("quality-gate")
                .value_name("JOB")
                .required(true)
                .help("the quality job's full name, folders included"),
        )
        .arg(
            Arg::new("target")
                .long("target")
                .value_name("BRANCH")
                .required(true)
                .help("the branch the pull request would merge into"),
        );
    clap::Command::new("orchestrator")
        .about(
            "Role-based handoff between agent sessions in herdr, each role in the harness of its choice; by default \
             Spec Collector -> Builder -> Reviewer, all in Claude Code.",
        )
        .subcommand_required(true)
        .arg_required_else_help(true)
        // argparse takes an unambiguous abbreviation of a long flag.
        .infer_long_args(true)
        // argparse takes the last of a flag given twice, where clap would refuse it.
        .subcommands(
            [run, resume, list, metrics, show, prune, close, workflows, judged]
                .map(|c| c.args_override_self(true)),
        )
}

/// What a RUN argument takes.
const RUN_REF_HELP: &str = "the run id, or the six-character key at its end";

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

/// The Starts in m.
fn starts(m: &ArgMatches) -> Starts {
    Starts {
        workflow: string(m, "workflow").unwrap_or_else(|| "default".to_string()),
        workflow_file: string(m, "workflow-file"),
        worktree: m.get_flag("worktree"),
        quality_gate: string(m, "quality-gate"),
    }
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
/// --max-quality-rounds below 1, a run given --max-quality-rounds without --quality-gate, a resume given it
/// with --no-quality-gate, and a --quality-gate that is not a Jenkins job's full name.
///
/// A flag command does not take is not checked.
fn check_flag_values(command: &Command, p: &mut clap::Command) -> Result<(), clap::Error> {
    let error = |p: &mut clap::Command, message: &str| Err(p.error(ErrorKind::ValueValidation, message));
    if command.target().is_some_and(|t| t.machine.is_some() && t.cwd.is_none()) {
        return error(p, "--cwd is required with --machine");
    }
    let (quality_gate, no_quality_gate) = match command {
        Command::Run(a) => (a.starts.quality_gate.as_deref(), false),
        Command::Resume(a) => (a.quality_gate.as_deref(), a.no_quality_gate),
        Command::JudgedInQuality { job, .. } => (Some(job.as_str()), false),
        _ => (None, false),
    };
    let unset = Settings::default();
    let settings = command.settings().unwrap_or(&unset);
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

/// A usage error of the subcommand name, one parser defines, with its usage.
fn subcommand_error(p: &mut clap::Command, name: &str, message: &str) -> clap::Error {
    let sub = p.find_subcommand_mut(name).expect("check_spec_args names the subcommands parser defines");
    sub.error(ErrorKind::MissingRequiredArgument, message)
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
