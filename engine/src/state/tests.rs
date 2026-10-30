use super::*;
use crate::test_support::{oracle, recordings};

/// The state.json texts a recording holds, as engine/tests/oracle/states.py's saved_states finds them: each one the
/// ledger saw, as json.dumps writes it, then the run files'.
fn saved_states(recording: &Json) -> Vec<String> {
    fn walk(x: &Json, found: &mut Vec<String>) {
        match x {
            Json::Object(map) => {
                let is_state = map.get("path").and_then(Json::as_str).is_some_and(|p| p.ends_with("state.json"));
                if let (true, Some(saved @ Json::Object(_))) = (is_state, map.get("saved")) {
                    found.push(saved.dumps() + "\n");
                }
                map.values().for_each(|v| walk(v, found));
            }
            Json::Array(items) => items.iter().for_each(|v| walk(v, found)),
            _ => {}
        }
    }
    let mut found = Vec::new();
    walk(recording.get("events").unwrap(), &mut found);
    let files = recording.get("run_files").and_then(Json::as_object).unwrap();
    found.extend(files.iter().filter(|(path, _)| path.ends_with("state.json")).map(|(_, text)| text.as_str().unwrap().to_string()));
    found
}

#[test]
fn every_recorded_state_reads_and_writes_back_byte_for_byte() {
    let mut texts: Vec<String> = Vec::new();
    for (_, recording) in recordings() {
        for text in saved_states(&recording) {
            if !texts.contains(&text) {
                texts.push(text);
            }
        }
    }
    let states = oracle("states.json");
    assert_eq!(Some(texts.len() as i128), states.get("recorded").and_then(Json::as_int), "states.py counted others");
    let changed = states.get("changed").and_then(Json::as_array).unwrap();
    for text in &texts {
        if changed.iter().any(|c| c.get("text").and_then(Json::as_str) == Some(text)) {
            continue;
        }
        let state = RunState::from_text(text).unwrap_or_else(|e| panic!("{e}: {text}"));
        assert_eq!(state.to_text(), *text);
    }
    // The ones the Python engine writes back otherwise, older states among them.
    assert!(!changed.is_empty());
    for case in changed {
        let text = case.get("text").and_then(Json::as_str).unwrap();
        let back = case.get("back").unwrap();
        match RunState::from_text(text) {
            Ok(state) => assert_eq!(Some(state.to_text().as_str()), back.get("text").and_then(Json::as_str), "{text}"),
            Err(e) => assert!(back.get("error").is_some(), "{text}: refused with {e}, read by Python"),
        }
    }
}

#[test]
fn fields_it_does_not_know_are_kept_and_written_back() {
    let mut state = RunState::new("20260929-120000-a1b2c3", "t", "/proj");
    state.unknown.insert("later_field", Json::parse(r#"{"b": [1, 2.5], "a": null}"#).unwrap());
    state.unknown.insert("another", Json::from("x"));
    let text = state.to_text();
    assert!(text.ends_with(", \"later_field\": {\"b\": [1, 2.5], \"a\": null}, \"another\": \"x\"}\n"), "{text}");
    let back = RunState::from_text(&text).unwrap();
    assert_eq!(back, state);
    assert_eq!(back.to_text(), text);
}

#[test]
fn fields_lists_every_field_to_json_writes_and_no_unknown_one_replaces_it() {
    // herdr_server is written only when known: the default session is.
    let mut full = RunState::new("r", "t", "/p");
    full.herdr_server = HerdrServer::of_environment(&[]);
    let written: Vec<String> = match full.to_json() {
        Json::Object(map) => map.keys().map(String::from).collect(),
        _ => panic!("an object"),
    };
    assert_eq!(written, FIELDS[..FIELDS.len() - 1]);
    let mut state = RunState::new("r", "t", "/p");
    state.unknown.insert("phase", Json::from("stale"));
    assert!(state.to_text().contains("\"phase\": \"spec\""));
    assert!(!state.to_text().contains("stale"));
}

#[test]
fn a_new_state_writes_what_the_python_engines_does() {
    let state = RunState::new("20260929-120000-a1b2c3", "add a rate limiter", "/proj");
    let text = state.to_text();
    assert!(text.starts_with(
        "{\"run_id\": \"20260929-120000-a1b2c3\", \"task\": \"add a rate limiter\", \"cwd\": \"/proj\", \"machine\": null, \
         \"phase\": \"spec\", \"round\": 0, \"workspace_id\": \"\", \"root_pane\": \"\", \"base\": null,"
    ));
    assert!(text.ends_with("\"blocked\": null, \"locked\": {}, \"restored\": [], \"proofs\": []}\n"), "{text}");
}

/// The herdr server a CLI reaches with these variables set and no other.
fn server(set: &[(&str, &str)]) -> HerdrServer {
    let vars: Vec<_> = set.iter().map(|(k, v)| (std::ffi::OsString::from(k), std::ffi::OsString::from(v))).collect();
    HerdrServer::of_environment(&vars).unwrap()
}

#[test]
fn the_herdr_server_is_written_after_the_machine_and_read_back() {
    let mut state = RunState::new("20260929-120000-a1b2c3", "t", "/proj");
    state.herdr_server = Some(server(&[("HERDR_SOCKET_PATH", "/s/herdr.sock")]));
    let text = state.to_text();
    let saved = r#""machine": null, "herdr_server": {"HERDR_SOCKET_PATH": "/s/herdr.sock", "HERDR_SESSION": null}, "#;
    assert!(text.contains(saved), "{text}");
    assert_eq!(RunState::from_text(&text).unwrap(), state);
    // The default session, neither variable set, is a server too, and not the absence of one.
    state.herdr_server = Some(server(&[]));
    let back = RunState::from_text(&state.to_text()).unwrap();
    assert_eq!(back.herdr_server, Some(server(&[])));
    state.herdr_server = Some(server(&[("HERDR_SESSION", "work")]));
    assert_eq!(RunState::from_text(&state.to_text()).unwrap(), state);
}

#[test]
fn a_state_saved_before_the_herdr_server_has_none_and_writes_back_without_it() {
    let text = RunState::new("20260929-120000-a1b2c3", "t", "/proj").to_text();
    assert!(!text.contains("herdr_server"), "{text}");
    let state = RunState::from_text(&text).unwrap();
    assert_eq!(state.herdr_server, None);
    assert_eq!(state.to_text(), text);
    let text = r#"{"run_id": "r1", "task": "t", "cwd": "/p", "herdr_server": null}"#;
    assert_eq!(RunState::from_text(text).unwrap().herdr_server, None);
    // A variable left out of the object was unset.
    let text = r#"{"run_id": "r1", "task": "t", "cwd": "/p", "herdr_server": {"HERDR_SESSION": "work"}}"#;
    assert_eq!(RunState::from_text(text).unwrap().herdr_server, Some(server(&[("HERDR_SESSION", "work")])));
    for value in ["\"work\"", "{\"HERDR_SESSION\": 1}", "[]"] {
        let text = format!(r#"{{"run_id": "r1", "task": "t", "cwd": "/p", "herdr_server": {value}}}"#);
        let e = RunState::from_text(&text).unwrap_err();
        assert_eq!(e.0, "run state r1 is invalid: herdr_server is not an object of herdr's variables");
    }
}

#[test]
fn a_state_without_its_required_fields_or_with_a_wrong_type_is_refused_naming_the_run() {
    let e = RunState::from_text(r#"{"run_id": "r1", "task": "t"}"#).unwrap_err();
    assert_eq!(e.0, "run state r1 is incomplete: it has no cwd");
    let e = RunState::from_text(r#"{"task": "t", "cwd": "/p"}"#).unwrap_err();
    assert_eq!(e.0, "run state ? is incomplete: it has no run_id");
    for (field, value) in [
        ("round", "\"1\""), ("round", "-1"), ("phase", "1"), ("machine", "3"), ("conflicts", "[1]"), ("agents", "{\"a\": 1}"),
        ("pull_request", "1"), ("owner", "[]"), ("ci", "null"), ("turns", "[1]"), ("locked", "{\"s\": [1]}"),
        ("models", "{\"a\": null}"), ("run_id", "5"),
    ] {
        let text = format!(r#"{{"run_id": "r1", "task": "t", "cwd": "/p", "{field}": {value}}}"#);
        let e = RunState::from_text(&text).unwrap_err();
        assert!(e.0.contains(&format!("is invalid: {field} is not")), "{field}: {e}");
    }
    assert!(RunState::from_text("[]").is_err());
    assert!(RunState::from_text("{").is_err());
}

#[test]
fn a_state_saved_by_0_3_0_reads_its_agent_args_as_the_permission_mode() {
    let state = RunState::from_text(r#"{"run_id": "r", "task": "t", "cwd": "/p", "agent_args": ["--permission-mode", "plan"]}"#).unwrap();
    assert_eq!(state.permission_mode.as_deref(), Some("plan"));
    assert!(!state.to_text().contains("agent_args"));
    let kept = RunState::from_text(r#"{"run_id": "r", "task": "t", "cwd": "/p", "permission_mode": null, "agent_args": ["--permission-mode", "plan"]}"#).unwrap();
    assert_eq!(kept.permission_mode, None);
    let e = RunState::from_text(r#"{"run_id": "r", "task": "t", "cwd": "/p", "agent_args": ["--model", null, true]}"#).unwrap_err();
    assert_eq!(e.0, "run state r has agent_args ['--model', None, True], which name no permission mode");
}

#[test]
fn the_root_pane_comes_from_the_workflows_first_role() {
    let agents = r#""agents": {"review": {"name": "review-abc", "pane": "w9:p2"}, "build": {"name": "build-abc", "pane": "w9:p1"}}"#;
    let quick = crate::workflow_file::workflow_definition(&crate::test_support::quick()).dumps();
    let text = format!(r#"{{"run_id": "r-abc", "task": "t", "cwd": "/p", "workflow": "quick", "workflow_definition": {quick}, {agents}}}"#);
    assert_eq!(RunState::from_text(&text).unwrap().root_pane, "w9:p1");
    // A workflow this orchestrator does not know leaves it empty.
    let text = format!(r#"{{"run_id": "r-abc", "task": "t", "cwd": "/p", "workflow": "quick", {agents}}}"#);
    assert_eq!(RunState::from_text(&text).unwrap().root_pane, "");
}

#[test]
fn the_runs_paths() {
    let mut s = RunState::new("20260929-120000-a1b2c3", "t", "/proj");
    let d = "/proj/.orchestrator/runs/20260929-120000-a1b2c3";
    assert_eq!(s.key(), "a1b2c3");
    assert_eq!(s.dir(), d);
    assert_eq!(s.work_dir(), "/proj");
    assert_eq!(s.refs_prefix(), "refs/orchestrator/a1b2c3/");
    assert_eq!(s.lock_ref("tests"), "refs/orchestrator/a1b2c3/locked/tests");
    assert_eq!(s.proofs_dir(2), format!("{d}/proofs-2"));
    assert_eq!(s.spec_path(), format!("{d}/spec.md"));
    assert_eq!(s.review_path(2), format!("{d}/review-2.md"));
    assert_eq!(s.leaks_path("snapshot"), format!("{d}/leaks-snapshot.json"));
    assert_eq!(s.quality_path(1, 2), format!("{d}/quality-1-2.md"));
    assert_eq!(s.builder_report_path(1, 0), format!("{d}/build-1.md"));
    assert_eq!(s.builder_report_path(1, 2), format!("{d}/build-1-q2.md"));
    assert_eq!(s.last_report_path(1), format!("{d}/build-1.md"));
    s.quality_round = 3;
    assert_eq!(s.last_report_path(1), format!("{d}/build-1-q2.md"));
    assert!(!s.had_quality_gate());
    s.quality_gate_disabled = true;
    assert!(s.had_quality_gate());
    s.worktree = Some("/proj/.orchestrator/worktrees/a1b2c3".into());
    assert_eq!(s.work_dir(), "/proj/.orchestrator/worktrees/a1b2c3");
    assert_eq!(s.prompts().unwrap(), Prompts::default());
    s.prompt_overrides.insert("LATER_NOTE", "x".to_string());
    assert!(s.prompts().unwrap_err().0.contains("replaces prompt LATER_NOTE"));
}

#[test]
fn a_state_with_a_field_it_does_not_know_and_without_later_ones_reads_with_their_defaults() {
    // TestResume.test_from_dict_ignores_unknown_keys_and_fills_new_ones; the unknown field is kept, as
    // fields_it_does_not_know_are_kept_and_written_back says.
    let text = r#"{"run_id": "r-abc", "task": "t", "cwd": "/p", "later_field": 1,
        "agents": {"spec": {"name": "spec-abc", "pane": "w9:p1"}}}"#;
    let state = RunState::from_text(text).unwrap();
    assert_eq!((state.root_pane.as_str(), state.prompted, state.owner), ("w9:p1", None, None));
    assert_eq!(state.max_rounds, crate::constants::DEFAULT_MAX_ROUNDS);
}

#[test]
fn a_state_saved_before_the_gate_has_none() {
    // TestQualityCLI.test_state_saved_before_the_gate_has_none, on a review saved in round 1: its one case that is
    // RunState's. Its others are the CLI's (test_flag_checks, test_gate_reaches_the_workflow,
    // test_list_shows_the_quality_phase: crate::cli's tests) and resumable_state's
    // (test_resume_limit_against_the_saved_quality_round, test_resume_limit_needs_the_gate: cli/start/tests.rs).
    let mut state = RunState::new("20260929-120000-a1b2c3", "add a rate limiter", "/proj");
    (state.phase, state.round) = ("review".into(), 1);
    let mut saved = state.to_json().as_object().unwrap().clone();
    for key in ["quality_job", "max_quality_rounds", "quality_round", "ci"] {
        assert!(saved.remove(key).is_some(), "{key}");
    }
    let state = RunState::from_saved(&saved).unwrap();
    assert_eq!((state.quality_job.as_deref(), state.quality_round, state.ci.is_empty()), (None, 0, true));
    assert_eq!(state.last_report_path(1), state.build_path(1));
}
