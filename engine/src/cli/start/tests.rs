//! The state a run starts from. Python's cases of resumable_state, by class: TestFindRun, TestResumableState,
//! TestOneRunPerCheckout's two resume --force cases, TestWorkflowFiles' and TestPromptFiles' saved definitions and
//! prompts that stop a resume, and TestQualityCLI's resume limits. TestHarnessFlags' resumable_state halves are in
//! crate::workflow's harnesses tests, beside the Workflow halves of the same cases. new_state has no Python test but
//! through main (TestCLI, crate::cli's tests); its cases here are the Rust engine's.

use std::rc::Rc;

use super::*;
use crate::ci::fake::JOB;
use crate::cli::args::{parse_args, Command};
use crate::constants::APPROVE;
use crate::env::ConfigDir;
use crate::host::fake::FakeHost;
use crate::herdr::HerdrServer;
use crate::host::Host;
use crate::runs::local_host;
use crate::test_support::{quick, resumable, solo, TempDir};
use crate::workflow::fixture::{quality_state, review_turn, saved_run, Fixture};
use crate::workflow_file::{workflow_definition, workflow_toml};

/// resume's arguments in argv, after `resume`.
fn resume_args(argv: &[&str]) -> ResumeArgs {
    let argv = ["resume"].iter().chain(argv).map(std::ffi::OsString::from);
    match parse_args(argv, &ConfigDir::at("/nowhere")) {
        Ok(Command::Resume(args)) => args,
        other => panic!("not a resume: {other:?}"),
    }
}

/// run's arguments in argv, after `run`, with the configuration directory config.
fn run_args(argv: &[&str], config: &ConfigDir) -> RunArgs {
    let argv = ["run"].iter().chain(argv).map(std::ffi::OsString::from);
    match parse_args(argv, config) {
        Ok(Command::Run(args)) => args,
        other => panic!("not a run: {other:?}"),
    }
}

/// state's state.json, with the fields of more put in.
fn saved_with(state: &RunState, more: &[(&str, Json)]) -> Map<Json> {
    let Json::Object(mut saved) = state.to_json() else { unreachable!("a state is saved as an object") };
    for (key, value) in more {
        saved.insert(*key, value.clone());
    }
    saved
}

/// map as JSON text, in its order.
fn dumps(map: &Map<String>) -> String {
    Json::Object(map.iter().map(|(k, v)| (k, Json::from(v.as_str()))).collect()).dumps()
}

fn error(result: Result<RunState, OrchestratorError>) -> String {
    match result {
        Ok(state) => panic!("resumed: {}", state.run_id),
        Err(e) => e.0,
    }
}

// -- TestFindRun ---------------------------------------------------------------------------------------------------

fn runs() -> Vec<(u64, Json)> {
    ["20260930-070000-c0ffee", "20260930-080000-beef00", "20260929-080000-beef00"]
        .iter()
        .zip(1..)
        .map(|(id, age)| (age, Json::Object([("run_id", Json::from(*id))].into_iter().collect())))
        .collect()
}

#[test]
fn by_key_or_full_id() {
    assert_eq!(find_run(&runs(), "c0ffee", "/p").unwrap().0, 1);
    assert_eq!(find_run(&runs(), "20260929-080000-beef00", "/p").unwrap().0, 3);
}

#[test]
fn ambiguous_key() {
    let e = find_run(&runs(), "beef00", "/p").unwrap_err();
    assert_eq!(
        e.0,
        "beef00 matches several runs (20260930-080000-beef00, 20260929-080000-beef00); give the full run id"
    );
}

#[test]
fn no_match() {
    assert_eq!(find_run(&runs(), "abcdef", "/p").unwrap_err().0, "no run abcdef under /p/.orchestrator/runs");
}

// -- TestResumableState --------------------------------------------------------------------------------------------

/// TestResumableState's saved run: in round 2 of build, with settings of its own, and the fields of more.
fn saved_settings(more: &[(&str, Json)]) -> Map<Json> {
    let mut state = saved_run("build", 2, &["spec", "build"]);
    state.max_rounds = 3;
    state.turn_timeout = 600;
    state.permission_mode = Some("auto".into());
    state.models = [("build", "sonnet".to_string())].into_iter().collect();
    saved_with(&state, more)
}

#[test]
fn saved_settings_are_kept() {
    let state = resumable(&[], &saved_settings(&[])).unwrap();
    assert_eq!((state.max_rounds, state.turn_timeout, state.permission_mode.as_deref()), (3, 600, Some("auto")));
    assert_eq!(dumps(&state.models), r#"{"build": "sonnet"}"#);
}

#[test]
fn flags_override_saved_settings() {
    let flags = ["--max-rounds", "5", "--timeout", "60", "--permission-mode", "acceptEdits", "--review-model", "opus"];
    let state = resumable(&flags, &saved_settings(&[])).unwrap();
    assert_eq!((state.max_rounds, state.turn_timeout, state.permission_mode.as_deref()), (5, 60, Some("acceptEdits")));
    assert_eq!(dumps(&state.models), r#"{"build": "sonnet", "review": "opus"}"#);
}

#[test]
fn max_rounds_below_the_saved_round() {
    let e = error(resumable(&["--max-rounds", "1"], &saved_settings(&[])));
    assert_eq!(e, "run 20260929-120000-a1b2c3 is already in round 2; --max-rounds 1 is too low");
}

#[test]
fn live_run_needs_force() {
    let me = [("host", Json::from("here")), ("pid", Json::from(42)), ("started_at", Json::from("x"))];
    let saved = Json::Object(saved_settings(&[("owner", Json::Object(me.into_iter().collect()))]));
    let alive = |_: i128| true;
    let e = resumable_state(&[(30, saved.clone())], &resume_args(&["a1b2c3"]), "/proj", "here", &alive).unwrap_err();
    assert_eq!(
        e.0,
        "run 20260929-120000-a1b2c3 looks alive (pid 42 on here, beat 30s ago); pass --force to take it over"
    );
    let forced = resumable_state(&[(30, saved)], &resume_args(&["a1b2c3", "--force"]), "/proj", "here", &alive);
    assert_eq!(forced.unwrap().0.run_id, "20260929-120000-a1b2c3");
}

#[test]
fn machine_of_the_resume_is_saved() {
    let state = resumable(&["--machine", "remote", "--cwd", "~/p"], &saved_settings(&[])).unwrap();
    assert_eq!(state.machine.as_deref(), Some("remote"));
    // And without --machine none, whatever the run was started with.
    let state = resumable(&[], &saved_settings(&[("machine", Json::from("box"))])).unwrap();
    assert_eq!(state.machine, None);
}

#[test]
fn a_closed_run_is_not_resumed_even_with_force() {
    let saved = saved_settings(&[("closed", Json::from("2026-10-01T00:00:00+00:00"))]);
    assert_eq!(error(resumable(&["--force"], &saved)), "run 20260929-120000-a1b2c3 was closed; start a new run");
}

// -- TestOneRunPerCheckout: resume --force -------------------------------------------------------------------------

/// resume --force of run a1b2c3 on host, which looks alive: saved 30 s ago by a live pid on this host. The fixture of
/// the state resumable_state gives, its pid_alive taking every pid for alive.
fn force_resume(host: Rc<FakeHost>) -> Fixture {
    let mut saved = saved_run("review", 1, &["spec", "build", "review"]);
    let owner = [("host", Json::from(local_host())), ("pid", Json::from(4242)), ("started_at", Json::from("x"))];
    saved.owner = Some(owner.into_iter().collect());
    host.state().files.insert(format!("{}/state.json", saved.dir()), saved.to_text());
    host.state().ages.insert(saved.run_id.clone(), 30);
    let runs = host.run_states("/proj").unwrap();
    let (state, _) = resumable_state(&runs, &resume_args(&["a1b2c3", "--force"]), "/proj", &local_host(), &|_| true)
        .expect("--force resumes a run that looks alive");
    let mut f = Fixture::resume(host, state, vec![("review", vec![review_turn(1, APPROVE)])], &[], &[]);
    f.pid_alive = Box::new(|_| true);
    f
}

#[test]
fn force_takes_over_the_run_itself() {
    let f = force_resume(Rc::new(FakeHost::default()));
    assert_eq!(f.workflow(f.as_saved()).run(), Ok(Some(APPROVE.to_string())));
}

#[test]
fn force_does_not_override_another_live_run() {
    // TestOneRunPerCheckout's other run, saved 16 s ago by a live orchestrator of this host.
    let other_id = "20261009-220151-537935";
    let host = Rc::new(FakeHost::default());
    let mut other = RunState::new(other_id, "another task", "/proj");
    other.phase = "build".into();
    other.round = 1;
    let owner = [
        ("host", Json::from(local_host())),
        ("pid", Json::from(3_354_482)),
        ("started_at", Json::from("2026-10-09T22:01:51+00:00")),
    ];
    other.owner = Some(owner.into_iter().collect());
    let other_path = format!("{}/state.json", other.dir());
    host.state().files.insert(other_path.clone(), other.to_text());
    host.state().ages.insert(other_id.to_string(), 16);
    let f = force_resume(host);
    let e = f.workflow(f.as_saved()).run().unwrap_err();
    let expected = format!(
        "run {other_id} is running in /proj (pid 3354482 on {}, beat 16s ago); one run at a time per checkout: wait \
         for it, stop it, or use another clone",
        local_host()
    );
    assert_eq!(e.to_string(), expected);
    let saved = f.saved();
    assert_eq!((saved.get("error"), saved.get("owner")), (Some(&Json::from(expected)), Some(&Json::Null)));
    assert!(f.herdr.state().calls.is_empty());
    assert_eq!(f.host.state().files.get(&other_path), Some(&other.to_text()));
}

// -- TestWorkflowFiles and TestPromptFiles: what a resume cannot read ------------------------------------------------

#[test]
fn a_saved_definition_that_cannot_be_filled_in_stops_the_resume() {
    let Json::Object(mut definition) = workflow_definition(&quick()) else { unreachable!("a definition is a table") };
    let Some(Json::Array(steps)) = definition.get_mut("steps") else { unreachable!("a definition has steps") };
    let Some(Json::Object(first)) = steps.get_mut(0) else { unreachable!("quick has a first step") };
    first.insert("prompt", Json::from("{task:d}"));
    let more = [("workflow", Json::from("quick")), ("workflow_definition", Json::Object(definition))];
    let e = error(resumable(&[], &saved_with(&saved_run("build", 1, &[]), &more)));
    assert!(e.contains("saved with the run is invalid: workflow quick: step build: prompt cannot be filled in"), "{e}");
}

#[test]
fn a_corrupt_saved_definition_is_an_error() {
    let definition = Json::Object([("steps", Json::Array(vec![]))].into_iter().collect());
    let more = [("workflow", Json::from("quick")), ("workflow_definition", definition)];
    let e = error(resumable(&[], &saved_with(&saved_run("build", 1, &[]), &more)));
    assert!(e.contains("the definition of workflow quick saved with the run is invalid: it has no [[steps]]"), "{e}");
}

#[test]
fn a_state_that_replaces_an_unknown_prompt() {
    let overrides = Json::Object([("LATER_NOTE", Json::from("x"))].into_iter().collect());
    let e = error(resumable(&[], &saved_with(&saved_run("build", 1, &[]), &[("prompt_overrides", overrides)])));
    assert!(
        e.contains("run state 20260929-120000-a1b2c3 replaces prompt LATER_NOTE, which this orchestrator lacks"),
        "{e}"
    );
}

// -- TestQualityCLI: a resume's quality round limit ------------------------------------------------------------------

/// The state.json of a run in phase, round rnd and quality round q, with the quality gate.
fn gated_run(phase: &str, rnd: u32, q: u32) -> Map<Json> {
    let mut state = saved_run(phase, rnd, &["spec", "build"]);
    state.quality_job = Some(JOB.to_string());
    state.quality_round = q;
    saved_with(&state, &[])
}

#[test]
fn resume_limit_against_the_saved_quality_round() {
    let quality = saved_with(&quality_state(2), &[]);
    let e = error(resumable(&["--max-quality-rounds", "1"], &quality));
    assert!(e.ends_with("already in quality round 2 of its quality phase; --max-quality-rounds 1 is too low"), "{e}");
    assert_eq!(resumable(&["--max-quality-rounds", "2"], &quality).unwrap().max_quality_rounds, 2);

    let build = gated_run("build", 1, 2);
    let e = error(resumable(&["--max-quality-rounds", "2"], &build));
    assert!(e.contains("already in quality round 2 of its build phase"), "{e}");
    assert_eq!(resumable(&["--max-quality-rounds", "3"], &build).unwrap().max_quality_rounds, 3);
    // Round q = 0 has had no quality round yet.
    resumable(&["--max-quality-rounds", "1"], &gated_run("build", 2, 0)).unwrap();
    // Without the flag, the saved limit is checked the same way.
    let mut past = gated_run("build", 1, 3);
    past.insert("max_quality_rounds", Json::from(3));
    assert!(error(resumable(&[], &past)).ends_with("--max-quality-rounds 3 is too low"));
}

#[test]
fn resume_limit_needs_the_gate() {
    let saved = saved_with(&saved_run("build", 1, &[]), &[]);
    assert!(error(resumable(&["--max-quality-rounds", "2"], &saved)).contains("has no quality gate"));
    // The gate this resume turns on is enough.
    let state = resumable(&["--quality-gate", JOB, "--max-quality-rounds", "2"], &saved).unwrap();
    assert_eq!(state.max_quality_rounds, 2);
}

// -- new_state ---------------------------------------------------------------------------------------------------

type MakeJenkins = dyn Fn(&str) -> Result<crate::ci::Jenkins, OrchestratorError>;

/// A Connected of f's fakes, the configuration directory config, and jenkins for a run's quality job.
fn connected<'a>(f: &'a Fixture, config: &'a ConfigDir, jenkins: &'a MakeJenkins) -> Connected<'a> {
    Connected {
        with: f.collaborators(),
        jenkins,
        config,
        local_host: "here",
        stdout: &|_| {},
        stderr: &|_| {},
        workflow: &crate::cli::run_workflow,
    }
}

fn no_jenkins(job: &str) -> Result<crate::ci::Jenkins, OrchestratorError> {
    Err(OrchestratorError(format!("no Jenkins for {job} here")))
}

#[test]
fn a_new_run_takes_the_defaults_and_the_flags() {
    let (f, d) = (Fixture::new(vec![]), TempDir::new());
    let config = ConfigDir::at(&d.path());
    let world = connected(&f, &config, &no_jenkins);
    let (state, pipeline) = new_state(&run_args(&["--", "a task"], &config), "/proj", None, &world).unwrap();
    assert_eq!((state.task.as_str(), state.cwd.as_str(), state.phase.as_str()), ("a task", "/proj", "spec"));
    assert_eq!((state.workflow.as_str(), pipeline.name()), ("default", "default"));
    assert_eq!((state.max_rounds, state.turn_timeout, state.max_quality_rounds), (3, 1800, 3));
    assert_eq!((state.pull_request, &state.worktree, &state.quality_job, &state.machine), (true, &None, &None, &None));
    assert!(state.models.is_empty() && state.agent_kinds.is_empty() && state.prompt_overrides.is_empty());
    let id = &state.run_id;
    assert!(id.len() == 22 && id.as_bytes()[8] == b'-' && id.as_bytes()[15] == b'-', "{id}");
    assert!(id.chars().all(|c| c.is_ascii_hexdigit() || c == '-'), "{id}");

    let flags = [
        "--max-rounds", "5", "--timeout", "60", "--permission-mode", "plan", "--model", "m", "--build-model", "b",
        "--role-model", "review=r", "--agent", "codex", "--role-agent", "spec=gemini", "--no-pr", "--worktree",
        "--quality-gate", JOB, "--max-quality-rounds", "2", "--machine", "box", "--cwd", "/p", "--", "a task",
    ];
    let (state, _) = new_state(&run_args(&flags, &config), "/proj", None, &world).unwrap();
    assert_eq!((state.max_rounds, state.turn_timeout, state.max_quality_rounds), (5, 60, 2));
    assert_eq!((state.permission_mode.as_deref(), state.machine.as_deref()), (Some("plan"), Some("box")));
    assert_eq!(dumps(&state.models), r#"{"spec": "m", "build": "b", "review": "r"}"#);
    assert_eq!(dumps(&state.agent_kinds), r#"{"spec": "gemini", "build": "codex", "review": "codex"}"#);
    assert_eq!((state.pull_request, state.quality_job.as_deref()), (false, Some(JOB)));
    assert_eq!(state.worktree, Some(format!("/proj/.orchestrator/worktrees/{}", state.key())));
    // A timeout of 0 is none given, as Python's `args.timeout or DEFAULT_TURN_TIMEOUT` takes it.
    let (state, _) = new_state(&run_args(&["--timeout", "0", "t"], &config), "/proj", None, &world).unwrap();
    assert_eq!(state.turn_timeout, 1800);
}

#[test]
fn a_new_run_from_a_spec_starts_after_its_contract_step_and_takes_its_title() {
    let (f, d) = (Fixture::new(vec![]), TempDir::new());
    let config = ConfigDir::at(&d.path());
    let world = connected(&f, &config, &no_jenkins);
    let spec = crate::spec_file::SpecFile { path: "/s.md".into(), text: "# Add subtract\n\nbody\n".into() };
    let (state, _) = new_state(&run_args(&["--spec", "/s.md"], &config), "/proj", Some(&spec), &world).unwrap();
    assert_eq!((state.task.as_str(), state.phase.as_str()), ("Add subtract", "build"));
    // A task given is kept.
    let (state, _) = new_state(&run_args(&["--spec", "/s.md", "mine"], &config), "/proj", Some(&spec), &world).unwrap();
    assert_eq!(state.task, "mine");
}

#[test]
fn a_new_run_of_a_workflow_file_or_a_named_one() {
    let (f, d) = (Fixture::new(vec![]), TempDir::new());
    std::fs::create_dir_all(d.join("workflows")).unwrap();
    std::fs::write(d.join("workflows/quick.toml"), workflow_toml(&quick())).unwrap();
    std::fs::write(d.join("solo.toml"), workflow_toml(&solo())).unwrap();
    let config = ConfigDir::at(&d.path());
    let world = connected(&f, &config, &no_jenkins);
    let (state, pipeline) = new_state(&run_args(&["--workflow", "quick", "t"], &config), "/proj", None, &world).unwrap();
    assert_eq!((state.workflow.as_str(), state.phase.as_str(), pipeline.name()), ("quick", "build", "quick"));
    let file = d.join("solo.toml");
    let (state, _) = new_state(&run_args(&["--workflow-file", &file, "t"], &config), "/proj", None, &world).unwrap();
    assert_eq!(state.workflow, "solo");
    // Roles the workflow lacks.
    let flags = ["--workflow-file", &file, "--role-model", "review=x", "t"];
    let e = new_state(&run_args(&flags, &config), "/proj", None, &world).unwrap_err();
    assert_eq!(e.0, "--role-model names role review, which the workflow does not have; its roles are build");
    let flags = ["--workflow-file", &file, "--role-agent", "spec=pi", "t"];
    let e = new_state(&run_args(&flags, &config), "/proj", None, &world).unwrap_err();
    assert!(e.0.starts_with("--role-agent names role spec"), "{e}");
}

// -- resumed_state -------------------------------------------------------------------------------------------------

#[test]
fn a_resume_finds_its_run_on_the_host_and_switches_its_gate() {
    let host = Rc::new(FakeHost::default());
    let mut saved = saved_run("build", 1, &["spec", "build"]);
    saved.quality_job = Some(JOB.to_string());
    host.state().files.insert(format!("{}/state.json", saved.dir()), saved.to_text());
    host.state().ages.insert(saved.run_id.clone(), 10_000);
    let f = Fixture::on(host, saved, vec![]);
    let config = ConfigDir::at("/nowhere");
    let world = connected(&f, &config, &no_jenkins);
    let (state, pipeline) = resumed_state(&resume_args(&["a1b2c3"]), "/proj", &world).unwrap();
    assert_eq!((state.quality_job.as_deref(), pipeline.name()), (Some(JOB), "default"));
    let (state, _) = resumed_state(&resume_args(&["a1b2c3", "--no-quality-gate"]), "/proj", &world).unwrap();
    assert_eq!((state.quality_job, state.quality_gate_disabled), (None, true));
    // The job of --quality-gate, which must be had first.
    let e = resumed_state(&resume_args(&["a1b2c3", "--quality-gate", "other/job"]), "/proj", &world).unwrap_err();
    assert_eq!(e.0, "no Jenkins for other/job here");
    let e = resumed_state(&resume_args(&["b0b0b0"]), "/proj", &world).unwrap_err();
    assert_eq!(e.0, "no run b0b0b0 under /proj/.orchestrator/runs");
}

/// The herdr server whose session is session, its socket path unset.
fn session(session: &str) -> HerdrServer {
    HerdrServer::from_json(&Json::Object([("HERDR_SESSION", Json::from(session))].into_iter().collect())).unwrap()
}

#[test]
fn a_run_saves_the_herdr_server_it_starts_or_resumes_its_agents_in() {
    let (f, d) = (Fixture::new(vec![]), TempDir::new());
    let config = ConfigDir::at(&d.path());
    let world = connected(&f, &config, &no_jenkins);
    f.herdr.state().server = Some(session("work"));
    let (state, _) = new_state(&run_args(&["t"], &config), "/proj", None, &world).unwrap();
    assert_eq!(state.herdr_server, Some(session("work")));
    // A herdr forwarding to a machine reaches none of this machine's.
    f.herdr.state().server = None;
    let on_box = run_args(&["--machine", "box", "--cwd", "/p", "t"], &config);
    let (state, _) = new_state(&on_box, "/proj", None, &world).unwrap();
    assert_eq!(state.herdr_server, None);

    // A resume saves the server it reaches, over the one the run saved.
    let host = Rc::new(FakeHost::default());
    let mut saved = saved_run("build", 1, &["spec", "build"]);
    saved.herdr_server = Some(session("old"));
    host.state().files.insert(format!("{}/state.json", saved.dir()), saved.to_text());
    host.state().ages.insert(saved.run_id.clone(), 10_000);
    let f = Fixture::on(host, saved, vec![]);
    let world = connected(&f, &config, &no_jenkins);
    f.herdr.state().server = Some(session("new"));
    let (state, _) = resumed_state(&resume_args(&["a1b2c3"]), "/proj", &world).unwrap();
    assert_eq!(state.herdr_server, Some(session("new")));
    f.herdr.state().server = None;
    let on_box = resume_args(&["a1b2c3", "--machine", "box", "--cwd", "/p"]);
    let (state, _) = resumed_state(&on_box, "/proj", &world).unwrap();
    assert_eq!(state.herdr_server, None);
}
