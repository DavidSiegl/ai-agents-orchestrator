//! TestRunWatch, and plain_name and ProjectRuns' refusals as TestServeEndpoints holds them, without a server.

use std::sync::atomic::{AtomicI64, Ordering};

use super::*;
use crate::constants::{APPROVE, HEARTBEAT_SECONDS, STALE_SECONDS};
use crate::runs::STALE;
use crate::serve::fake::ServeHost;

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
        let project = ProjectRuns::new(host.clone(), "/proj".to_string(), vec![]);
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
    let project = ProjectRuns::new(w.host.clone(), "/proj".to_string(), vec![]).with_pid_alive(|pid| {
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
