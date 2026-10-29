//! main's dispatch on the fixture's fakes (run_command), its exit statuses (finish, ended_status) and the resume hint;
//! and TestQualityGateOnResume's two cases of main, test_the_cli_refuses_with_exit_1_and_leaves_state_json_alone and
//! test_the_cli_runs_the_workflow_with_the_new_job (the class's others are crate::workflow's gate_switch tests). The
//! program itself, parsed arguments to exit status, is engine/tests/program.rs's.

use std::cell::RefCell;
use std::rc::Rc;

use super::*;
use crate::ci::fake::{jenkins_on, FakeJenkins, JOB};
use crate::constants::APPROVE;
use crate::host::fake::FakeHost;
use crate::py::json::Json;
use crate::workflow::fixture::{saved_run, Fixture};

/// The command line argv, after the program's name.
fn command(argv: &[&str]) -> Command {
    parse_args(argv.iter().map(OsString::from), &ConfigDir::at("/nowhere")).expect("a command line parse_args takes")
}

/// A fixture whose /proj holds the run state, saved 10^4 s ago, and that state as saved.
fn saved_on_host(state: RunState) -> (Fixture, String) {
    let host = Rc::new(FakeHost::default());
    let text = state.to_text();
    host.state().files.insert(format!("{}/state.json", state.dir()), text.clone());
    host.state().ages.insert(state.run_id.clone(), 10_000);
    (Fixture::on(host, state, vec![]), text)
}

/// run_command of argv on f's fakes; jobs records each quality job a Jenkins is made for, on ci.
fn run_on(f: &Fixture, argv: &[&str], ci: &Arc<FakeJenkins>, jobs: &RefCell<Vec<String>>) -> Result<Ran, Stop> {
    let config = ConfigDir::at("/nowhere");
    let jenkins = |job: &str| {
        jobs.borrow_mut().push(job.to_string());
        Ok(jenkins_on(ci))
    };
    let mut with = f.collaborators();
    with.jenkins = None;
    run_command(&command(argv), &Connected { with, jenkins: &jenkins, config: &config, local_host: "here" })
}

use std::sync::Arc;

#[test]
fn the_cli_refuses_with_exit_1_and_leaves_state_json_alone() {
    let cases = [
        (&["--quality-gate", JOB][..], "is a --no-pr run in /proj itself"),
        (&["--no-quality-gate"][..], "has no quality gate, so --no-quality-gate does not apply"),
    ];
    for (flags, why) in cases {
        let (f, saved) = saved_on_host(saved_run("build", 1, &["spec", "build"]));
        let argv: Vec<&str> = ["resume", "a1b2c3", "--cwd", "/proj"].iter().chain(flags).copied().collect();
        let ended = run_on(&f, &argv, &FakeJenkins::new(), &RefCell::new(vec![]));
        let Err(Stop::Failed(e)) = &ended else { panic!("{flags:?} did not fail") };
        assert!(e.0.contains(why), "{e}");
        assert_eq!(ended_status(ended, &command(&argv)), EXIT_ERROR);
        // No Workflow ran, and nothing was written.
        assert!(f.herdr.state().calls.is_empty());
        assert_eq!(f.host.state().files.get(&format!("{}/state.json", f.state.dir())), Some(&saved));
    }
}

#[test]
fn the_cli_runs_the_workflow_with_the_new_job() {
    let mut state = saved_run("build", 1, &["spec", "build", "review"]);
    state.pull_request = true;
    state.base_branch = Some("main".into());
    state.branch = Some("orchestrator/x-a1b2c3".into());
    let (f, _) = saved_on_host(state);
    let (ci, jobs) = (FakeJenkins::new(), RefCell::new(vec![]));
    // The Builder, played by no turn, exits: the run fails once its Workflow has saved its state.
    let argv = ["resume", "a1b2c3", "--cwd", "/proj", "--quality-gate", JOB, "--max-quality-rounds", "2"];
    assert!(matches!(run_on(&f, &argv, &ci, &jobs), Err(Stop::Failed(_))));
    // One Jenkins to turn the gate on, which asks for the job's parameters, and the Workflow's own.
    assert_eq!(*jobs.borrow(), [JOB, JOB]);
    assert_eq!(ci.state().names.first().map(String::as_str), Some("parameters"));
    let saved = f.saved();
    assert_eq!((saved.get("quality_job"), saved.get("max_quality_rounds")), (Some(&Json::from(JOB)), Some(&Json::from(2))));
}

#[test]
fn a_run_ends_with_its_verdict_and_its_pull_request() {
    let mut state = saved_run("done", 1, &["spec", "build", "review"]);
    state.verdict = Some(APPROVE.into());
    state.pr_url = Some("https://github.com/o/r/pull/7".into());
    let (f, _) = saved_on_host(state);
    let Ok(ran) = run_on(&f, &["resume", "a1b2c3", "--cwd", "/proj"], &FakeJenkins::new(), &RefCell::new(vec![])) else {
        panic!("a done run resumes to its verdict")
    };
    assert_eq!((ran.verdict.as_deref(), ran.state.pr_url.as_deref()), (Some(APPROVE), Some("https://github.com/o/r/pull/7")));
    assert_eq!(ended_status(Ok(ran), &command(&["resume", "a1b2c3"])), 0);
}

#[test]
fn a_ctrl_c_outside_the_workflow_is_the_interrupt() {
    // Before the run's state exists: the note names no run.
    let (f, _) = saved_on_host(saved_run("build", 1, &["spec", "build"]));
    f.interrupt.raise();
    let argv = ["resume", "b0b0b0", "--cwd", "/proj"];
    let ended = run_on(&f, &argv, &FakeJenkins::new(), &RefCell::new(vec![]));
    assert!(matches!(ended, Err(Stop::Interrupted(None))));
    assert_eq!(ended_status(ended, &command(&argv)), EXIT_INTERRUPTED);
    // Once it exists, before its Workflow starts: the note names it.
    let ended = run_on(&f, &["resume", "a1b2c3", "--cwd", "/proj"], &FakeJenkins::new(), &RefCell::new(vec![]));
    let Err(Stop::Interrupted(Some(run_id))) = ended else { panic!("not interrupted with its run") };
    assert_eq!(run_id, "20260929-120000-a1b2c3");
    assert!(f.herdr.state().calls.is_empty());
}

#[test]
fn finish_names_the_verdict_and_the_exit_status() {
    let mut state = saved_run("done", 2, &[]);
    let pipeline = crate::pipeline::DEFAULT_WORKFLOW.clone();
    assert_eq!(finish(APPROVE, &state, &pipeline), 0);
    assert_eq!(finish("FINISHED", &state, &pipeline), 0);
    assert_eq!(finish("CHANGES_REQUESTED", &state, &pipeline), EXIT_CHANGES_REQUESTED);
    assert_eq!(finish("QUALITY_GATE_FAILED", &state, &pipeline), EXIT_CHANGES_REQUESTED);
    state.conflicts = vec!["calc.py".into()];
    assert_eq!(finish(APPROVE, &state, &pipeline), EXIT_CONFLICT);
    assert_eq!(finish("CHANGES_REQUESTED", &state, &pipeline), EXIT_CHANGES_REQUESTED);
}

#[test]
fn the_resume_hint() {
    let target = ["--machine".to_string(), "box".to_string(), "--cwd".to_string(), "~/my project".to_string()];
    assert_eq!(resume_command("20260929-120000-a1b2c3", &target), "orchestrator.py resume a1b2c3 --machine box --cwd '~/my project'");
    assert_eq!(
        interrupted_note(Some("20260929-120000-a1b2c3"), &[]),
        "interrupted; the role agents keep running in herdr; resume with: orchestrator.py resume a1b2c3"
    );
    assert_eq!(interrupted_note(None, &target), "interrupted; the role agents keep running in herdr");
    assert_eq!(command(&["run", "--machine", "box", "--cwd", "/p", "t"]).target().flags(), target[..2].iter().cloned().chain(["--cwd".into(), "/p".into()]).collect::<Vec<_>>());
}
