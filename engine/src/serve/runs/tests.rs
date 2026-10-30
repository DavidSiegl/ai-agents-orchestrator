//! TestRunWatch, and plain_name and ProjectRuns' refusals as TestServeEndpoints holds them, without a server.

use std::sync::atomic::{AtomicI64, Ordering};

use super::*;
use crate::constants::{APPROVE, HEARTBEAT_SECONDS, STALE_SECONDS};
use crate::runs::STALE;
use crate::serve::fake::{FakeConsole, ServeHost};

const SERVE_ID: &str = "20261024-120000-5e7e00";

fn run_dir() -> String {
    format!("/proj/{RUNS_DIR}/{SERVE_ID}")
}

/// serve_state(**kw) of the Python tests: the run's state with the extra keys.
fn serve_state(extra: &[(&str, Json)]) -> String {
    let mut state: Map<Json> = [
        ("run_id", Json::from(SERVE_ID)),
        ("task", Json::from("add a rate limiter")),
        ("phase", Json::from("build")),
        ("round", Json::from(1)),
    ]
    .into_iter()
    .collect();
    for (key, value) in extra {
        state.insert(*key, value.clone());
    }
    Json::Object(state).dumps()
}

struct Watched {
    host: Arc<ServeHost>,
    project: ProjectRuns,
}

impl Watched {
    fn new() -> Watched {
        let host = Arc::new(ServeHost::on(None));
        let project = ProjectRuns::new(host.clone(), Arc::new(FakeConsole::default()), "/proj".to_string(), vec![]);
        Watched { host, project }
    }

    fn save(&self, age: u64, extra: &[(&str, Json)]) {
        let mut s = self.host.state();
        s.files.insert(format!("{}/state.json", run_dir()), serve_state(extra));
        s.ages.insert(SERVE_ID.to_string(), age);
    }
}

fn kinds(events: &[Event]) -> Vec<&str> {
    events.iter().map(|e| e.kind).collect()
}

#[test]
fn first_poll_sends_everything() {
    let w = Watched::new();
    w.save(0, &[]);
    w.host.state().files.insert(format!("{}/spec.md", run_dir()), "# Spec\n".to_string());
    let mut watch = RunWatch::new(&w.project);
    let events = watch.poll();
    let runs: Vec<(&str, &str)> = events.iter().map(|e| (e.kind, e.payload.get("run").and_then(Json::as_str).unwrap())).collect();
    assert_eq!(runs, [("record", SERVE_ID), ("files", SERVE_ID)]);
    assert_eq!(events[0].payload.get("record").and_then(|r| r.get("phase")), Some(&Json::from("build")));
    assert_eq!(events[1].payload.get("files").unwrap().dumps(), r#"[{"name": "spec.md", "mtime": 0}]"#);
    assert_eq!(watch.poll(), vec![]);
}

#[test]
fn an_older_heartbeat_alone_is_no_event() {
    let w = Watched::new();
    w.save(10, &[]);
    let mut watch = RunWatch::new(&w.project);
    watch.poll();
    w.save(50, &[]);
    assert_eq!(watch.poll(), vec![]);
}

#[test]
fn a_run_turning_stale_is_an_event() {
    let w = Watched::new();
    w.save(10, &[]);
    let mut watch = RunWatch::new(&w.project);
    watch.poll();
    w.save(STALE_SECONDS + 1, &[]);
    let events = watch.poll();
    assert_eq!(kinds(&events), ["record"]);
    assert_eq!(events[0].payload.get("record").and_then(|r| r.get("health")), Some(&Json::from(STALE)));
}

#[test]
fn a_finished_runs_files_are_read_again_only_when_it_changes() {
    let w = Watched::new();
    let done = [("phase", Json::from("done")), ("verdict", Json::from(APPROVE))];
    w.save(0, &done);
    let mut watch = RunWatch::new(&w.project);
    watch.poll();
    w.host.state().files.insert(format!("{}/summary.md", run_dir()), "done".to_string());
    assert_eq!(watch.poll(), vec![]);
    w.save(0, &[done[0].clone(), done[1].clone(), ("closed", Json::from("2026-10-24T12:00:00Z"))]);
    assert_eq!(kinds(&watch.poll()), ["record", "files"]);
}

#[test]
fn a_run_of_this_host_whose_orchestrator_exited_is_stale() {
    static ASKED: AtomicI64 = AtomicI64::new(0);
    let w = Watched::new();
    let owner = Json::Object(
        [("host", Json::from(local_host())), ("pid", Json::from(4242)), ("started_at", Json::from("x"))].into_iter().collect(),
    );
    w.save(HEARTBEAT_SECONDS + 1, &[("owner", owner)]);
    let console = Arc::new(FakeConsole::default());
    let project = ProjectRuns::new(w.host.clone(), console, "/proj".to_string(), vec![]).with_pid_alive(|pid| {
        ASKED.fetch_add(1, Ordering::SeqCst);
        assert_eq!(pid, 4242);
        false
    });
    let events = RunWatch::new(&project).poll();
    assert_eq!(kinds(&events), ["record", "files"]);
    assert_eq!(ASKED.load(Ordering::SeqCst), 1);
    let record = events[0].payload.get("record").unwrap();
    assert_eq!(
        (record.get("health"), record.get("health_reason")),
        (Some(&Json::from(STALE)), Some(&Json::from("orchestrator pid 4242 exited")))
    );
}

#[test]
fn a_problem_is_told_once_and_its_end_too() {
    let w = Watched::new();
    w.host.state().run_states_error = Some("ssh: connection refused".to_string());
    let mut watch = RunWatch::new(&w.project);
    let problem = watch.poll();
    assert_eq!(kinds(&problem), ["problem"]);
    assert_eq!(problem[0].payload.dumps(), r#"{"error": "ssh: connection refused"}"#);
    assert_eq!(watch.poll(), vec![]);
    w.host.state().run_states_error = None;
    w.save(0, &[]);
    let events = watch.poll();
    assert_eq!(kinds(&events), ["problem", "record", "files"]);
    assert_eq!(events[0].payload.dumps(), r#"{"error": null}"#);
    assert_eq!(watch.poll(), vec![]);
}

#[test]
fn a_run_whose_state_is_gone_gets_a_null_record() {
    let w = Watched::new();
    w.save(0, &[]);
    let mut watch = RunWatch::new(&w.project);
    watch.poll();
    w.host.state().files.clear();
    let events = watch.poll();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].sse(), format!("event: record\ndata: {{\"run\": \"{SERVE_ID}\", \"record\": null}}\n\n"));
}

#[test]
fn a_run_id_that_is_not_a_plain_name_is_a_problem() {
    // A state.json whose run_id would lead the file listing out of the runs directory.
    let w = Watched::new();
    w.host.state().files.insert(format!("{}/state.json", run_dir()), r#"{"run_id": "../.."}"#.to_string());
    let events = RunWatch::new(&w.project).poll();
    assert_eq!(events[0].payload.dumps(), r#"{"error": "'../..' is not a name in the run directory"}"#);
    assert_eq!(events.len(), 1);
}

#[test]
fn plain_names_and_the_rest() {
    for name in ["spec.md", "..x", "a..b", ".hidden", "é", " ", "x\\y"] {
        assert_eq!(plain_name(name), Ok(name), "{name:?}");
    }
    for (name, said) in [
        ("", "''"),
        (".", "'.'"),
        ("..", "'..'"),
        ("../x", "'../x'"),
        ("/etc/passwd", "'/etc/passwd'"),
        ("a/", "'a/'"),
        ("spec\0.md", "'spec\\x00.md'"),
    ] {
        assert_eq!(plain_name(name), Err(RunReadError::Refused(format!("{said} is not a name in the run directory"))));
    }
}

#[test]
fn state_json_and_names_outside_the_run_are_refused_unread() {
    let w = Watched::new();
    w.save(0, &[]);
    w.host.state().files.insert("/proj/secret".to_string(), "secret".to_string());
    let refused = |run: &str, name: &str| match w.project.read(run, name) {
        Err(RunReadError::Refused(why)) => why,
        other => panic!("{run:?} {name:?} gave {other:?}"),
    };
    assert_eq!(refused(SERVE_ID, "state.json"), "state.json is not a handoff file");
    // The run id is checked first, as Python's f-string evaluates it first.
    assert_eq!(refused("..", "../secret"), "'..' is not a name in the run directory");
    assert_eq!(refused(SERVE_ID, "../../../secret"), "'../../../secret' is not a name in the run directory");
    assert!(matches!(w.project.files("../.."), Err(RunReadError::Refused(_))));
    assert_eq!(w.host.state().reads, Vec::<String>::new());
    assert_eq!(w.project.read(SERVE_ID, "spec.md"), Ok(None));
    assert_eq!(w.host.state().reads, [format!("{}/spec.md", run_dir())]);
}

#[test]
fn an_agent_is_reached_in_the_herdr_server_its_run_saved() {
    let host = Arc::new(ServeHost::on(None));
    let console = Arc::new(FakeConsole::default());
    let project = ProjectRuns::new(host.clone(), console.clone(), "/proj".to_string(), vec![]);
    console.state().screens.insert("build-5e7e00".to_string(), "> ready".to_string());
    let agents = Json::parse(r#"{"build": {"name": "build-5e7e00", "pane": "w1:p2"}}"#).unwrap();
    let save = |server: Option<&str>| {
        let mut extra = vec![("agents", agents.clone())];
        extra.extend(server.map(|s| ("herdr_server", Json::parse(s).unwrap())));
        host.state().files.insert(format!("{}/state.json", run_dir()), serve_state(&extra));
    };
    let work = r#"{"HERDR_SOCKET_PATH": null, "HERDR_SESSION": "work"}"#;
    save(Some(work));
    assert_eq!(project.agent_text(SERVE_ID, "build", 10), Ok("> ready".to_string()));
    project.type_into(SERVE_ID, "build", &AgentInput::Keys(vec!["enter".to_string()])).unwrap();
    // The default session's, neither variable set, is reached as one too: serve's own variables are not its.
    save(Some(r#"{"HERDR_SOCKET_PATH": null, "HERDR_SESSION": null}"#));
    project.type_into(SERVE_ID, "build", &AgentInput::Text("go".to_string())).unwrap();
    // A run saved before runs saved their server, or saved with null, names none.
    save(None);
    project.agent_text(SERVE_ID, "build", 10).unwrap();
    save(Some("null"));
    project.agent_text(SERVE_ID, "build", 10).unwrap();
    let servers: Vec<Option<String>> =
        console.state().servers.iter().map(|s| s.as_ref().map(|s| s.to_json().dumps())).collect();
    let default = r#"{"HERDR_SOCKET_PATH": null, "HERDR_SESSION": null}"#;
    let expected = [Some(work), Some(work), Some(default), None, None].map(|s| s.map(String::from));
    assert_eq!(servers, expected);
    // A server of another shape is no server to guess at.
    save(Some(r#""work""#));
    let e = project.agent_text(SERVE_ID, "build", 10).unwrap_err();
    assert_eq!(e.to_string(), format!("{}/state.json: herdr_server is not an object of herdr's variables", run_dir()));
    assert_eq!(console.state().typed.len(), 5);
}
