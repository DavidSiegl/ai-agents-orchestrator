//! The command line: what parse_args reads, the usage errors of check_flag_values and check_spec_args, and the flags
//! each command's help names, held to orchestrator.py's (the oracle engine/tests/oracle/cli.json). Python's cases of
//! parse_args alone, by class: TestCLI's test_machine_requires_cwd, test_max_rounds_must_be_positive and
//! test_model_flags_are_run_only; TestResumeCLI.test_machine_requires_cwd; TestWorkflowCLI's
//! test_unknown_workflow_is_an_argparse_error and test_resume_has_no_workflow_flag; TestWorkflowFileCLI.test_bad_flags;
//! TestSpecCLI's test_a_task_or_spec_is_needed, test_resume_takes_no_spec and test_run_help_lists_spec;
//! TestHarnessFlags.test_unknown_kinds_are_argparse_errors; TestQualityCLI.test_flag_checks; the parse_args halves of
//! TestResumePullRequest.test_no_pr_is_saved_with_the_run and TestWorktreeRun.test_a_run_flag; TestListJSON's
//! test_pr_state_needs_json. Their cases of main are crate::cli's tests; the others here are the Rust engine's.

use clap::error::ErrorKind;

use super::*;
use crate::py::json::Json;
use crate::test_support::TempDir;

fn parse(argv: &[&str]) -> Result<Command, clap::Error> {
    parse_args(argv.iter().map(OsString::from), &ConfigDir::at("/nowhere"))
}

/// The usage error parse gives argv, as clap words it, with its exit status.
fn refused(argv: &[&str]) -> (String, i32) {
    match parse(argv) {
        Ok(command) => panic!("{argv:?} parsed: {command:?}"),
        Err(e) => (e.to_string(), e.exit_code()),
    }
}

fn run(argv: &[&str]) -> RunArgs {
    match parse(&[&["run"][..], argv].concat()) {
        Ok(Command::Run(args)) => args,
        other => panic!("{argv:?}: {other:?}"),
    }
}

fn resume(argv: &[&str]) -> ResumeArgs {
    match parse(&[&["resume"][..], argv].concat()) {
        Ok(Command::Resume(args)) => args,
        other => panic!("{argv:?}: {other:?}"),
    }
}

fn pair(a: &str, b: &str) -> (String, String) {
    (a.to_string(), b.to_string())
}

#[test]
fn a_run_with_nothing_but_its_task_has_every_flag_unset() {
    let args = run(&["--", "Add a subtract function"]);
    assert_eq!(args.task.as_deref(), Some("Add a subtract function"));
    assert_eq!((args.spec, args.no_pr, args.target), (None, false, Target::default()));
    assert_eq!(args.settings, Settings::default());
    let starts = Starts { workflow: "default".into(), workflow_file: None, worktree: false, quality_gate: None };
    assert_eq!(args.starts, starts);
}

#[test]
fn every_flag_of_run() {
    let args = run(&[
        "--machine", "box", "--cwd", "/p", "--max-rounds", "4", "--timeout", "90", "--permission-mode", "plan",
        "--model", "m", "--spec-model", "s", "--review-model", "r", "--role-model", "build=b", "--role-model",
        "build=b2", "--agent", "codex", "--role-agent", "review=gemini", "--max-quality-rounds", "2",
        "--workflow-file", "/w.toml", "--worktree", "--quality-gate", "f/job", "--spec", "/s.md", "--no-pr", "task",
    ]);
    assert_eq!(args.target, Target { machine: Some("box".into()), cwd: Some("/p".into()) });
    assert_eq!(args.target.flags(), ["--machine", "box", "--cwd", "/p"]);
    let s = &args.settings;
    assert_eq!((s.max_rounds, s.timeout, s.max_quality_rounds), (Some(4), Some(90), Some(2)));
    assert_eq!((s.permission_mode.as_deref(), s.model.as_deref(), s.agent.as_deref()), (Some("plan"), Some("m"), Some("codex")));
    assert_eq!(s.label_models, [pair("spec", "s"), pair("review", "r")]);
    assert_eq!(s.role_model, [pair("build", "b"), pair("build", "b2")]);
    assert_eq!(s.role_agent, [pair("review", "gemini")]);
    assert_eq!((args.starts.workflow_file.as_deref(), args.starts.worktree), (Some("/w.toml"), true));
    assert_eq!(args.starts.quality_gate.as_deref(), Some("f/job"));
    assert_eq!((args.spec.as_deref(), args.no_pr, args.task.as_deref()), (Some("/s.md"), true, Some("task")));
}

#[test]
fn every_flag_of_resume() {
    let args = resume(&["a1b2c3", "--force", "--quality-gate", "job", "--max-rounds", "5"]);
    assert_eq!((args.run_ref.as_str(), args.force, args.quality_gate.as_deref()), ("a1b2c3", true, Some("job")));
    assert_eq!((args.no_quality_gate, args.settings.max_rounds), (false, Some(5)));
    assert!(resume(&["a1b2c3", "--no-quality-gate"]).no_quality_gate);
    // argparse takes an abbreviation that names one flag.
    assert_eq!(resume(&["a1b2c3", "--max-r", "2"]).settings.max_rounds, Some(2));
}

#[test]
fn a_workflow_by_name_is_one_the_configuration_offers() {
    let d = TempDir::new();
    std::fs::create_dir_all(d.join("workflows")).unwrap();
    std::fs::write(d.join("workflows/mine.toml"), "").unwrap();
    let config = ConfigDir::at(&d.path());
    let parsed = parse_args(["run", "--workflow", "mine", "t"].map(OsString::from), &config);
    assert!(matches!(parsed, Ok(Command::Run(RunArgs { starts: Starts { ref workflow, .. }, .. })) if workflow == "mine"));
    let e = parse_args(["run", "--workflow", "theirs", "t"].map(OsString::from), &config).unwrap_err();
    assert_eq!(e.kind(), ErrorKind::InvalidValue);
    // Where the directory cannot be listed, the built-in ones.
    std::fs::write(d.join("file"), "").unwrap();
    let config = ConfigDir::at(&d.join("file"));
    assert!(parse_args(["run", "--workflow", "default", "t"].map(OsString::from), &config).is_ok());
}

#[test]
fn usage_errors_exit_2_naming_the_problem() {
    let cases: &[(&[&str], &str)] = &[
        (&["run"], "a task or --spec FILE is needed"),
        (&["resume", "a1b2c3", "--spec"], "unrecognized arguments: --spec; a resumed run keeps the spec it has"),
        (&["resume", "a1b2c3", "--spec", "x.md"], "a resumed run keeps the spec it has"),
        (&["run", "--machine", "box", "t"], "--cwd is required with --machine"),
        (&["resume", "a1b2c3", "--machine", "box", "--cwd", ""], "--cwd is required with --machine"),
        (&["run", "--max-rounds", "0", "t"], "--max-rounds must be at least 1"),
        (&["resume", "a1b2c3", "--max-rounds", "-1"], "--max-rounds must be at least 1"),
        (&["run", "--max-quality-rounds", "0", "--quality-gate", "j", "t"], "--max-quality-rounds must be at least 1"),
        (&["run", "--max-quality-rounds", "2", "t"], "--max-quality-rounds needs --quality-gate"),
        (&["run", "--max-quality-rounds", "2", "--quality-gate", "", "t"], "--max-quality-rounds needs --quality-gate"),
        (&["resume", "a1b2c3", "--max-quality-rounds", "2", "--no-quality-gate"], "does not apply with --no-quality-gate"),
        (&["run", "--quality-gate", "a//b", "t"], "--quality-gate takes a Jenkins job's full name"),
        (&["resume", "a1b2c3", "--quality-gate", "/a"], "--quality-gate takes a Jenkins job's full name"),
        (&["run", "--role-model", "build", "t"], "expected ROLE=MODEL, not 'build'"),
        (&["run", "--role-model", "=m", "t"], "expected ROLE=MODEL"),
        (&["run", "--role-agent", "build=vim", "t"], "expected ROLE=KIND, where KIND is one of claude, codex, gemini, opencode, pi; not 'build=vim'"),
        (&["run", "--agent", "vim", "t"], "vim"),
        (&["run", "--max-rounds", "two", "t"], "two"),
        (&["run", "--workflow", "default", "--workflow-file", "/w.toml", "t"], "--workflow-file"),
        (&["resume", "a1b2c3", "--quality-gate", "j", "--no-quality-gate"], "--no-quality-gate"),
        (&["resume"], "RUN"),
        (&["list", "--pr-state"], "--pr-state needs --json"),
        (&["show"], "RUN"),
        (&["list", "--model", "sonnet"], "--model"),
        (&["serve"], "serve"),
    ];
    for (argv, why) in cases {
        let (message, status) = refused(argv);
        assert!(message.contains(why), "{argv:?}: {message}");
        assert_eq!(status, 2, "{argv:?}");
    }
}

#[test]
fn help_exits_0_and_no_arguments_print_it() {
    for argv in [&["--help"][..], &["run", "--help"], &["resume", "-h"]] {
        let e = parse(argv).unwrap_err();
        assert_eq!((e.kind(), e.exit_code()), (ErrorKind::DisplayHelp, 0), "{argv:?}");
    }
    let e = parse(&["run", "--help"]).unwrap_err().to_string();
    for flag in ["--worktree", "--quality-gate", "--build-model", "--role-agent", "--no-pr", "--spec"] {
        assert!(e.contains(flag), "{flag}");
    }
    let e = parse(&[]).unwrap_err();
    assert_eq!(e.kind(), ErrorKind::DisplayHelpOnMissingArgumentOrSubcommand);
    assert!(e.to_string().contains("resume"));
}

// -- Python's cases of parse_args ------------------------------------------------------------------------------------

#[test]
fn machine_requires_cwd() {
    for argv in [&["run", "task", "--machine", "remote"][..], &["resume", "a1b2c3", "--machine", "m"], &["list", "--machine", "m"]] {
        assert!(refused(argv).0.contains("--cwd is required with --machine"), "{argv:?}");
    }
}

#[test]
fn max_rounds_must_be_positive() {
    assert_eq!(refused(&["run", "task", "--max-rounds", "0"]).1, 2);
}

#[test]
fn model_flags_are_run_only() {
    let (message, status) = refused(&["list", "--model", "sonnet"]);
    assert!(message.contains("--model"), "{message}");
    assert_eq!(status, 2);
}

#[test]
fn unknown_workflow_is_a_usage_error() {
    let (message, status) = refused(&["run", "task", "--workflow", "nope"]);
    // clap's wording of argparse's "argument --workflow: invalid choice: 'nope' (choose from default)".
    assert!(message.contains("invalid value 'nope' for '--workflow"), "{message}");
    assert!(message.contains("[possible values: default]"), "{message}");
    assert_eq!(status, 2);
}

#[test]
fn flags_of_how_a_run_starts_are_run_only() {
    // A resumed run keeps its workflow, where it works and whether it ends in a pull request.
    for flag in [&["--workflow", "default"][..], &["--workflow-file", "/w.toml"], &["--worktree"], &["--no-pr"]] {
        let argv: Vec<&str> = ["resume", "a1b2c3"].iter().chain(flag).copied().collect();
        let (message, status) = refused(&argv);
        assert!(message.contains(&format!("unexpected argument '{}'", flag[0])), "{message}");
        assert_eq!(status, 2);
    }
}

#[test]
fn bad_workflow_file_flags() {
    for argv in [&["run", "task", "--role-model", "tests"][..], &["run", "task", "--workflow", "default", "--workflow-file", "x.toml"]] {
        assert_eq!(refused(argv).1, 2, "{argv:?}");
    }
}

#[test]
fn a_task_or_spec_is_needed() {
    let (message, status) = refused(&["run"]);
    assert!(message.contains("a task or --spec FILE is needed"), "{message}");
    // The run command's usage, as argparse's "orchestrator.py run: error: ...".
    assert!(message.contains("Usage: orchestrator run"), "{message}");
    assert_eq!(status, 2);
}

#[test]
fn resume_takes_no_spec() {
    // Not even as an abbreviation of --spec-model, which an abbreviating parser would otherwise make of it.
    let spec = "--spec=/s.md";
    for flags in [&["--spec", "spec.md"][..], &["--spec"], &[spec]] {
        let argv: Vec<&str> = ["resume", "a1b2c3"].iter().chain(flags).copied().collect();
        let (message, status) = refused(&argv);
        assert!(message.contains("unrecognized arguments: --spec"), "{flags:?}: {message}");
        assert_eq!(status, 2);
    }
    assert_eq!(resume(&["a1b2c3", "--spec-model", "opus"]).settings.label_models, [pair("spec", "opus")]);
    assert_eq!(run(&["t", "--spec-model", "opus"]).settings.label_models, [pair("spec", "opus")]);
}

#[test]
fn run_help_lists_spec_and_worktree() {
    let help = parse(&["run", "--help"]).unwrap_err().to_string();
    assert!(help.contains("--spec <FILE>") && help.contains("--worktree"), "{help}");
}

#[test]
fn unknown_kinds_are_usage_errors() {
    let argvs: [&[&str]; 5] = [
        &["run", "task", "--agent", "cursor"],
        &["resume", "a1b2c3", "--agent", "cursor"],
        &["run", "task", "--role-agent", "review=cursor"],
        &["resume", "a1b2c3", "--role-agent", "review"],
        &["run", "task", "--role-agent", "=pi"],
    ];
    for argv in argvs {
        let (message, status) = refused(argv);
        assert!(message.contains("claude, codex, gemini, opencode, pi"), "{argv:?}: {message}");
        assert_eq!(status, 2);
    }
}

#[test]
fn quality_flag_checks() {
    let argvs: [&[&str]; 7] = [
        &["run", "t", "--quality-gate", "folder/job", "--max-quality-rounds", "0"],
        &["run", "t", "--max-quality-rounds", "2"],
        &["run", "t", "--quality-gate", "/folder/"],
        &["resume", "a1b2c3", "--quality-gate", "/folder/"],
        &["resume", "a1b2c3", "--quality-gate", "folder/job", "--no-quality-gate"],
        &["resume", "a1b2c3", "--no-quality-gate", "--max-quality-rounds", "2"],
        &["resume", "a1b2c3", "--max-quality-rounds", "0"],
    ];
    for argv in argvs {
        assert_eq!(refused(argv).1, 2, "{argv:?}");
    }
    let args = run(&["t", "--quality-gate", "folder/job", "--max-quality-rounds", "2"]);
    assert_eq!((args.starts.quality_gate.as_deref(), args.settings.max_quality_rounds), (Some("folder/job"), Some(2)));
}

#[test]
fn the_commands_about_runs_and_workflows() {
    let target = Target { machine: Some("m".into()), cwd: Some("~/p".into()) };
    let cases: [(&[&str], Command); 8] = [
        (&["list"], Command::Runs(Target::default(), RunsCommand::List { json: false, pr_state: false })),
        (&["list", "--json", "--pr-state", "--machine", "m", "--cwd", "~/p"], Command::Runs(target.clone(), RunsCommand::List { json: true, pr_state: true })),
        (&["metrics"], Command::Runs(Target::default(), RunsCommand::Metrics)),
        (&["show", "a1b2c3"], Command::Runs(Target::default(), RunsCommand::Show("a1b2c3".into()))),
        (&["prune", "--dry-run"], Command::Runs(Target::default(), RunsCommand::Prune { dry_run: true })),
        (&["close", "--cwd", "~/p", "a1b2c3"], Command::Runs(Target { machine: None, cwd: Some("~/p".into()) }, RunsCommand::Close("a1b2c3".into()))),
        (&["workflows"], Command::Workflows(None)),
        (&["workflows", "quick"], Command::Workflows(Some("quick".into()))),
    ];
    for (argv, command) in cases {
        assert_eq!(parse(argv).unwrap(), command, "{argv:?}");
    }
    assert_eq!(parse(&["workflows"]).unwrap().target(), None);
    // workflows names no project.
    assert!(refused(&["workflows", "--cwd", "/p"]).0.contains("--cwd"));
}

// -- The flags each command's help names, as orchestrator.py's ------------------------------------------------------



/// The flags text names, as engine/tests/oracle/cli.py finds them: each long flag (two hyphens, lowercase words
/// joined by hyphens) or -h that stands alone, not part of a longer word.
fn flags_in(text: &str) -> std::collections::BTreeSet<String> {
    let word = |c: char| c.is_alphanumeric() || c == '_' || c == '-';
    text.split(|c: char| !word(c))
        .filter(|w| {
            let long = w.strip_prefix("--").is_some_and(|rest| {
                rest.starts_with(|c: char| c.is_ascii_lowercase())
                    && rest.ends_with(|c: char| c.is_ascii_lowercase())
                    && rest.chars().all(|c| c.is_ascii_lowercase() || c == '-')
            });
            long || *w == "-h"
        })
        .map(String::from)
        .collect()
}

#[test]
fn help_names_the_flags_python_names() {
    let oracle = crate::test_support::oracle("cli.json");
    let names = |key: &str| -> Vec<String> {
        oracle.get(key).and_then(Json::as_array).expect("the oracle lists them").iter().map(Json::python_str).collect()
    };
    let commands = names("commands");
    let flags = oracle.get("flags").and_then(Json::as_object).expect("the oracle has the flags");
    let p = parser(&ConfigDir::at("/nowhere"));
    let ported: Vec<String> = p.get_subcommands().map(|c| c.get_name().to_string()).collect();
    // The program's own help, then each command's.
    for (name, argv) in std::iter::once(("", vec!["--help"])).chain(ported.iter().map(|c| (c.as_str(), vec![c.as_str(), "--help"]))) {
        let help = parse(&argv).unwrap_err().to_string();
        let python: std::collections::BTreeSet<String> = flags
            .get(name)
            .and_then(Json::as_array)
            .unwrap_or_else(|| panic!("orchestrator.py has no command {name}"))
            .iter()
            .map(Json::python_str)
            .collect();
        assert_eq!(flags_in(&help), python, "{name:?}'s help:\n{help}");
    }
    let missing: Vec<&str> =
        commands.iter().map(String::as_str).filter(|c| !ported.iter().any(|p| p == c) && !["gui", "serve"].contains(c)).collect();
    assert_eq!(missing, Vec::<&str>::new());
    assert!(ported.iter().all(|c| commands.contains(c)), "{ported:?}");
}

#[test]
fn a_flag_given_twice_takes_the_last() {
    // As argparse's store action does.
    let args = run(&["--cwd", "/a", "--max-rounds", "2", "--cwd", "/b", "--max-rounds", "3", "--no-pr", "--no-pr", "t"]);
    assert_eq!((args.target.cwd.as_deref(), args.settings.max_rounds, args.no_pr), (Some("/b"), Some(3), true));
    let show = parse(&["show", "--machine", "a", "--machine", "b", "--cwd", "/p", "x"]).unwrap();
    assert_eq!(show.target().and_then(|t| t.machine.as_deref()), Some("b"));
    assert!(matches!(parse(&["list", "--json", "--json"]), Ok(Command::Runs(_, RunsCommand::List { json: true, .. }))));
}
