//! The run and resume command lines: what parse_args reads, and the usage errors of check_flag_values and
//! check_spec_args. Python's cases of parse_args (TestCLI, TestQualityCLI.test_flag_checks, TestHarnessFlags'
//! test_unknown_kinds_are_argparse_errors) are ticket 09's, with the whole command line; these are the Rust engine's.

use clap::error::ErrorKind;

use super::*;
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
        (&["list"], "list"),
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
