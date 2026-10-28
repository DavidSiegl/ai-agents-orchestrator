//! herdr as the engine drives it: tests/test_orchestrator.py's TestHerdr, TestHerdrLookups and TestChildEnvironment's
//! herdr case, ported case for case on a ProcessHerdr whose commands a closure answers, as Python's Herdr(run=...);
//! real processes for what only they show; every herdr command the parity scenarios recorded; and the FakeHerdr the
//! Workflow tests of ticket 08 run on.

use std::rc::Rc;
use std::sync::{Arc, Mutex};

use super::fake::{resumed_session, Call, FakeHerdr, Turn};
use super::*;
use crate::harness::harness;
use crate::host::fake::FakeHost;
use crate::host::tests::{assert_scrubbed, environment, tool, with_credentials};
use crate::host::Host;
use crate::state::RunState;
use crate::test_support::{recordings, TempDir};

/// Each command a ProcessHerdr ran, with the limit on its process.
type Commands = Arc<Mutex<Vec<(Vec<String>, Option<Duration>)>>>;

/// A ProcessHerdr forwarding to machine whose every command exits with status, printing stdout and stderr, and what
/// it ran.
fn answering(machine: Option<&str>, status: i32, stdout: &str, stderr: &str) -> (ProcessHerdr, Commands) {
    let ran: Commands = Arc::default();
    let (seen, stdout, stderr) = (ran.clone(), stdout.as_bytes().to_vec(), stderr.as_bytes().to_vec());
    let herdr = ProcessHerdr::with_runner(machine, move |argv, limit| {
        seen.lock().unwrap().push((argv.to_vec(), limit));
        Ok((status, stdout.clone(), stderr.clone()))
    });
    (herdr, ran)
}

/// What herdr prints for a command whose result is result, a JSON object.
fn result(result: &str) -> String {
    format!("{{\"id\": \"cli:x\", \"result\": {result}}}")
}

/// What herdr prints on stderr for an error of code.
fn error(code: &str, message: &str) -> String {
    format!("{{\"error\": {{\"code\": \"{code}\", \"message\": \"{message}\"}}}}")
}

/// The argv and limit of the last command run.
fn last(ran: &Commands) -> (Vec<String>, Option<Duration>) {
    ran.lock().unwrap().last().cloned().expect("a command ran")
}

fn argv(words: &[&str]) -> Vec<String> {
    words.iter().map(|w| w.to_string()).collect()
}

// -- TestHerdr -------------------------------------------------------------------------------------------------------

#[test]
fn call_returns_result_and_forwards_machine() {
    let (herdr, ran) = answering(Some("remote"), 0, &result(r#"{"agent": {"agent_status": "idle"}}"#), "");
    assert_eq!(herdr.status("build-x"), Ok(Some("idle".to_string())));
    assert_eq!(last(&ran).0, argv(&["herdr", "--machine", "remote", "agent", "get", "build-x"]));
    assert_eq!(herdr.machine(), Some("remote"));
    // An empty machine is none, as Python takes it.
    let (herdr, ran) = answering(Some(""), 0, &result("{}"), "");
    herdr.focus("x").unwrap();
    assert_eq!(last(&ran).0, argv(&["herdr", "agent", "focus", "x"]));
    assert_eq!(herdr.machine(), None);
}

#[test]
fn prompt_does_not_wait() {
    let (herdr, ran) = answering(None, 0, &result(r#"{"type": "agent_prompted"}"#), "");
    herdr.prompt("spec-x", "hi").unwrap();
    assert_eq!(last(&ran), (argv(&["herdr", "agent", "prompt", "spec-x", "hi"]), Some(Duration::from_secs(120))));
}

#[test]
fn error_json_raises_with_code() {
    let (herdr, _) = answering(None, 1, "", &error("agent_blocked", "waiting"));
    let e = herdr.call(&["agent", "prompt", "x", "y"]).unwrap_err();
    assert_eq!(e.code(), Some("agent_blocked"));
    assert_eq!(e.to_string(), "herdr agent_blocked: waiting");
    assert_eq!(OrchestratorError::from(e).0, "herdr agent_blocked: waiting");
    // On stdout, when stderr holds none.
    let (herdr, _) = answering(None, 1, &error("server_unavailable", "down"), "warning: x");
    assert_eq!(herdr.focus("x").unwrap_err().code(), Some("server_unavailable"));
}

#[test]
fn plain_text_error() {
    let (herdr, _) = answering(None, 2, "", "unknown flag\n");
    assert_eq!(herdr.call(&["agent", "bogus"]).unwrap_err().to_string(), "herdr exit_2: unknown flag");
    let (herdr, _) = answering(None, -9, " killed ", "");
    assert_eq!(herdr.call(&["agent", "bogus"]).unwrap_err().code(), Some("exit_-9"));
    // An error object whose code is no string is still shown, and matches no code the engine asks for.
    let (herdr, _) = answering(None, 1, "", r#"{"error": {"code": 5, "message": ["m"]}}"#);
    assert_eq!(herdr.focus("x").unwrap_err().to_string(), r#"herdr 5: ["m"]"#);
    // An error answer without its code or message is plain text.
    let (herdr, _) = answering(None, 1, "", r#"{"error": {"code": "c"}}"#);
    assert_eq!(herdr.focus("x").unwrap_err().code(), Some("exit_1"));
}

#[test]
fn status_is_none_after_exit() {
    let (herdr, _) = answering(None, 1, "", &error("agent_not_found", "gone"));
    assert_eq!(herdr.status("spec-x"), Ok(None));
    assert_eq!(herdr.agent("spec-x"), Ok(None));
}

#[test]
fn start_agent_passes_the_harness_args_after_separator() {
    let (herdr, ran) = answering(None, 0, &result("{}"), "");
    assert_eq!(herdr.start_agent("build-x", "w1:p2", "claude", &argv(&["--permission-mode", "auto"])), Ok(true));
    let (args, limit) = last(&ran);
    assert_eq!(args[args.iter().position(|a| a == "--kind").unwrap() + 1], "claude");
    assert_eq!(args[args.len() - 3..], argv(&["--", "--permission-mode", "auto"]));
    assert_eq!(limit, Some(Duration::from_secs(120)));
    herdr.start_agent("review-x", "w1:p3", "codex", &[]).unwrap();
    assert_eq!(
        last(&ran).0,
        argv(&["herdr", "agent", "start", "review-x", "--kind", "codex", "--pane", "w1:p3", "--timeout", "60000"])
    );
}

#[test]
fn start_agent_blocked_at_startup() {
    let (herdr, _) = answering(None, 1, "", &error("agent_not_ready", "blocked during startup"));
    assert_eq!(herdr.start_agent("spec-x", "w1:p1", "claude", &[]), Ok(false));
    let (herdr, _) = answering(None, 1, "", &error("pane_not_found", "no pane"));
    assert_eq!(herdr.start_agent("spec-x", "w1:p1", "claude", &[]).unwrap_err().code(), Some("pane_not_found"));
}

#[test]
fn wait_passes_each_until() {
    let (herdr, ran) = answering(None, 0, &result(r#"{"agent": {"agent_status": "idle"}}"#), "");
    assert_eq!(herdr.wait("x", Some(1000), &["working", "idle"]), Ok("idle".to_string()));
    let (args, limit) = last(&ran);
    let expected = ["herdr", "agent", "wait", "x", "--timeout", "1000", "--until", "working", "--until", "idle"];
    assert_eq!(args, argv(&expected));
    assert_eq!(limit, Some(Duration::from_secs(61)));
    // A time that is not whole seconds bounds the process as precisely.
    herdr.wait("x", Some(1500), &[]).unwrap();
    assert_eq!(last(&ran).1, Some(Duration::from_millis(61_500)));
}

#[test]
fn unbounded_wait_has_no_process_limit() {
    let (herdr, ran) = answering(None, 0, &result(r#"{"agent": {"agent_status": "idle"}}"#), "");
    herdr.wait("x", None, &["idle"]).unwrap();
    let (args, limit) = last(&ran);
    assert!(!args.contains(&"--timeout".to_string()));
    assert_eq!(limit, None);
}

#[test]
fn ssh_target_by_label() {
    let profiles = r#"[{"id": "5d45", "label": "remote", "target": "remote-host", "enabled": true}]"#;
    let (herdr, ran) = answering(Some("remote"), 0, profiles, "");
    assert_eq!(herdr.ssh_target(), Ok("remote-host".to_string()));
    // Machine management is local; forwarding it would be rejected by herdr.
    assert_eq!(last(&ran), (argv(&["herdr", "machine", "list", "--json"]), Some(Duration::from_secs(120))));
    let (herdr, _) = answering(Some("5d45"), 0, profiles, "");
    assert_eq!(herdr.ssh_target(), Ok("remote-host".to_string()));
}

#[test]
fn ssh_target_unknown_machine() {
    let (herdr, _) = answering(Some("nope"), 0, "[]", "");
    assert_eq!(herdr.ssh_target().unwrap_err().to_string(), "no saved herdr machine named nope");
    let (herdr, _) = answering(Some("nope"), 0, r#"[{"label": 5, "id": "x"}]"#, "");
    assert_eq!(herdr.ssh_target().unwrap_err().code(), None);
}

#[test]
fn ssh_target_of_a_disabled_or_unreadable_machine() {
    let (herdr, _) = answering(Some("box"), 0, r#"[{"label": "box", "target": "t", "enabled": false}]"#, "");
    assert_eq!(herdr.ssh_target().unwrap_err().to_string(), "herdr machine box is disabled");
    for printed in ["{}", "nonsense", r#"["box"]"#, r#"[{"label": "box", "enabled": true}]"#] {
        let (herdr, _) = answering(Some("box"), 0, printed, "");
        let e = herdr.ssh_target().unwrap_err();
        assert_eq!(e.code(), Some("bad_response"), "{printed}");
        assert!(e.to_string().starts_with("herdr bad_response: machine list: "), "{e}");
    }
}

#[test]
fn missing_binary() {
    let herdr = ProcessHerdr::with_runner(None, |_, _| Err(ProcessError::Start(std::io::ErrorKind::NotFound.into())));
    let e = herdr.call(&["agent", "list"]).unwrap_err();
    assert_eq!((e.code(), e.to_string()), (None, "herdr is not on PATH".to_string()));
    // And on a real PATH without it.
    let d = TempDir::new();
    let vars = environment(None, &[("PATH", &d.path())]);
    let e = ProcessHerdr::in_environment(None, vars.into_iter()).focus("x").unwrap_err();
    assert_eq!(e.to_string(), "herdr is not on PATH");
}

#[test]
fn a_herdr_that_cannot_be_run_or_read_names_the_command() {
    let denied = || Err(ProcessError::Start(std::io::ErrorKind::PermissionDenied.into()));
    let herdr = ProcessHerdr::with_runner(None, move |_, _| denied());
    assert!(herdr.focus("x").unwrap_err().to_string().starts_with("herdr agent focus x: "));
    // A prompt's text stays out of the error, as out of Python's OSError.
    let e = herdr.prompt("spec-x", "a long prompt").unwrap_err().to_string();
    assert!(e.starts_with("herdr agent prompt spec-x: ") && !e.contains("long"), "{e}");
    let herdr = ProcessHerdr::with_runner(None, |_, _| Err(ProcessError::Io(std::io::ErrorKind::BrokenPipe.into())));
    assert_eq!(herdr.focus("x").unwrap_err().code(), None);
    let herdr = ProcessHerdr::with_runner(None, |_, _| Ok((0, b"\xff".to_vec(), vec![])));
    let e = herdr.focus("x").unwrap_err();
    assert!(e.to_string().starts_with("herdr agent focus x: printed what is not UTF-8"), "{e}");
}

#[test]
fn an_answer_that_is_no_result_is_a_bad_response() {
    for (printed, why) in [
        ("not json", "agent focus: "),
        (r#"{"id": "cli:x"}"#, "agent focus: 'result'"),
        ("[1]", "agent focus: the answer is not an object: [1]"),
    ] {
        let (herdr, _) = answering(None, 0, printed, "");
        let e = herdr.focus("x").unwrap_err();
        assert_eq!(e.code(), Some("bad_response"));
        assert!(e.to_string().starts_with(&format!("herdr bad_response: {why}")), "{e}");
    }
    // A result without what the command gives.
    let (herdr, _) = answering(None, 0, &result("{}"), "");
    let e = herdr.agent("x").unwrap_err();
    assert_eq!(e.to_string(), "herdr bad_response: agent get: no agent.agent_status in the answer");
    assert_eq!(herdr.wait("x", None, &[]).unwrap_err().code(), Some("bad_response"));
    assert_eq!(herdr.split("p", Direction::Down, "/proj").unwrap_err().code(), Some("bad_response"));
    let e = herdr.create_workspace("/proj", "l").unwrap_err();
    assert_eq!(e.to_string(), "herdr bad_response: workspace create: no workspace.workspace_id in the answer");
}

#[test]
fn a_command_that_does_not_return_is_a_timeout() {
    let d = TempDir::new();
    tool(&d.path(), "herdr", "#!/bin/sh\nsleep 5\n");
    let list = |machine: Option<&str>| {
        let herdr = ProcessHerdr::in_environment(machine, environment(Some(&d.path()), &[]).into_iter());
        herdr.call_within(&["agent", "list"], Some(Duration::from_millis(200))).unwrap_err()
    };
    let e = list(None);
    let expected = (Some("timeout"), "herdr timeout: herdr agent list did not return".to_string());
    assert_eq!((e.code(), e.to_string()), expected);
    // Python names the first three words herdr was given, the machine's among them.
    let e = list(Some("box"));
    assert_eq!(e.to_string(), "herdr timeout: herdr --machine box agent did not return");
}

#[test]
fn an_unbounded_wait_runs_the_cli_as_long_as_it_takes() {
    let d = TempDir::new();
    let answer = result(r#"{"agent": {"agent_status": "done"}}"#);
    tool(&d.path(), "herdr", &format!("#!/bin/sh\nsleep 0.2\necho '{answer}'\n"));
    let herdr = ProcessHerdr::in_environment(None, environment(Some(&d.path()), &[]).into_iter());
    assert_eq!(herdr.wait("x", None, &["done"]), Ok("done".to_string()));
}

#[test]
fn the_other_commands_and_their_answers() {
    let answer =
        r#"{"workspace": {"workspace_id": "w1"}, "root_pane": {"pane_id": "w1:p1"}, "pane": {"pane_id": "w1:p2"}}"#;
    let (herdr, ran) = answering(Some("box"), 0, &result(answer), "");
    let with = |words: &[&str]| argv(&[&["herdr", "--machine", "box"][..], words].concat());
    assert_eq!(herdr.create_workspace("/proj", "a1b2c3 task"), Ok(("w1".to_string(), "w1:p1".to_string())));
    assert_eq!(last(&ran).0, with(&["workspace", "create", "--cwd", "/proj", "--label", "a1b2c3 task", "--no-focus"]));
    assert_eq!(herdr.split("w1:p1", Direction::Right, "/proj"), Ok("w1:p2".to_string()));
    assert_eq!(last(&ran).0, with(&["pane", "split", "w1:p1", "--direction", "right", "--cwd", "/proj", "--no-focus"]));
    herdr.split("w1:p2", Direction::Down, "/wt").unwrap();
    assert_eq!(last(&ran).0[7], "down");
    herdr.rename_pane("w1:p2", "Builder").unwrap();
    assert_eq!(last(&ran).0, with(&["pane", "rename", "w1:p2", "Builder"]));
    herdr.close_workspace("w1").unwrap();
    assert_eq!(last(&ran).0, with(&["workspace", "close", "w1"]));
    herdr.notify("Run finished: APPROVE", "the task").unwrap();
    let expected = ["notification", "show", "Run finished: APPROVE", "--body", "the task", "--sound", "request"];
    assert_eq!(last(&ran).0, with(&expected));
    assert!(format!("{herdr:?}").contains("box"));
}

// -- TestHerdrLookups ------------------------------------------------------------------------------------------------

#[test]
fn agent_record() {
    let record = r#"{"agent": {"agent_status": "idle", "agent_session": {"value": "abc"}}}"#;
    let (herdr, _) = answering(None, 0, &result(record), "");
    let expected = Agent { status: "idle".to_string(), session: Some("abc".to_string()) };
    assert_eq!(herdr.agent("spec-x"), Ok(Some(expected)));
    // No session yet, an empty one, or one herdr reports as no string.
    for record in [
        r#"{"agent": {"agent_status": "working"}}"#,
        r#"{"agent": {"agent_status": "working", "agent_session": null}}"#,
        r#"{"agent": {"agent_status": "working", "agent_session": {"value": ""}}}"#,
        r#"{"agent": {"agent_status": "working", "agent_session": {"value": 5}}}"#,
    ] {
        let (herdr, _) = answering(None, 0, &result(record), "");
        assert_eq!(herdr.agent("x").unwrap().unwrap().session, None, "{record}");
    }
}

#[test]
fn workspace_and_pane_exist() {
    let (herdr, ran) = answering(None, 0, &result("{}"), "");
    assert_eq!(herdr.workspace_exists("wA"), Ok(true));
    assert_eq!(last(&ran).0, argv(&["herdr", "workspace", "get", "wA"]));
    assert_eq!(herdr.pane_exists("wA:p1"), Ok(true));
    assert_eq!(last(&ran).0, argv(&["herdr", "pane", "get", "wA:p1"]));
}

#[test]
fn workspace_and_pane_gone() {
    let (herdr, _) = answering(None, 1, "", &error("workspace_not_found", "x"));
    assert_eq!(herdr.workspace_exists("wA"), Ok(false));
    let (herdr, _) = answering(None, 1, "", &error("pane_not_found", "x"));
    assert_eq!(herdr.pane_exists("wA:p1"), Ok(false));
}

#[test]
fn other_errors_raise() {
    let (herdr, _) = answering(None, 1, "", &error("server_unavailable", "x"));
    assert_eq!(herdr.pane_exists("wA:p1").unwrap_err().code(), Some("server_unavailable"));
    assert_eq!(herdr.workspace_exists("wA").unwrap_err().code(), Some("server_unavailable"));
    assert_eq!(herdr.agent("x").unwrap_err().code(), Some("server_unavailable"));
}

// -- TestChildEnvironment --------------------------------------------------------------------------------------------

#[test]
fn herdr_gets_no_credentials() {
    let d = TempDir::new();
    let env = d.join("env");
    tool(&d.path(), "herdr", &format!("#!/bin/sh\nenv > '{env}'\necho '{{\"result\": {{}}}}'\n"));
    let vars = environment(Some(&d.path()), &with_credentials());
    ProcessHerdr::in_environment(None, vars.into_iter()).focus("x").unwrap();
    assert_scrubbed(&std::fs::read_to_string(env).unwrap());
}

// -- What the parity scenarios recorded ------------------------------------------------------------------------------

/// Give herdr the method call that makes the recorded command args (without --machine); None for a command that is
/// not one of ProcessHerdr's (the doctor's --version and integration status). Its answer is not looked at.
fn replay(herdr: &ProcessHerdr, args: &[String]) -> Option<()> {
    let a: Vec<&str> = args.iter().map(String::as_str).collect();
    let after = |flag: &str| a.iter().position(|w| *w == flag).map(|i| a[i + 1]);
    match a[..] {
        ["workspace", "create", ..] => drop(herdr.create_workspace(after("--cwd")?, after("--label")?)),
        ["pane", "split", pane, ..] => {
            let direction = if after("--direction")? == "right" { Direction::Right } else { Direction::Down };
            drop(herdr.split(pane, direction, after("--cwd")?));
        }
        ["pane", "rename", pane, label] => drop(herdr.rename_pane(pane, label)),
        ["agent", "start", name, ..] => {
            let rest = a.iter().position(|w| *w == "--").map_or(vec![], |i| args[i + 1..].to_vec());
            drop(herdr.start_agent(name, after("--pane")?, after("--kind")?, &rest));
        }
        ["agent", "prompt", name, text] => drop(herdr.prompt(name, text)),
        ["agent", "wait", name, ..] => {
            let until: Vec<&str> =
                a.iter().enumerate().filter(|(_, w)| **w == "--until").map(|(i, _)| a[i + 1]).collect();
            drop(herdr.wait(name, after("--timeout").map(|t| t.parse().unwrap()), &until));
        }
        ["agent", "get", name] => drop(herdr.agent(name)),
        ["pane", "get", pane] => drop(herdr.pane_exists(pane)),
        ["workspace", "get", workspace] => drop(herdr.workspace_exists(workspace)),
        ["agent", "focus", name] => drop(herdr.focus(name)),
        ["workspace", "close", workspace] => drop(herdr.close_workspace(workspace)),
        ["notification", "show", title, ..] => drop(herdr.notify(title, after("--body")?)),
        ["machine", "list", "--json"] => drop(herdr.ssh_target()),
        _ => return None,
    }
    Some(())
}

#[test]
fn every_recorded_herdr_command_is_the_one_processherdr_gives() {
    let mut replayed = 0;
    for (scenario, recording) in recordings() {
        for events in recording.get("events").and_then(Json::as_object).unwrap().values() {
            for event in events.as_array().unwrap() {
                let Some(args) = event.get("herdr").and_then(Json::as_array) else { continue };
                let args: Vec<String> = args.iter().map(|a| a.as_str().unwrap().to_string()).collect();
                let (machine, command) = match &args[..] {
                    [flag, machine, rest @ ..] if flag == "--machine" => (Some(machine.as_str()), rest.to_vec()),
                    _ => (None, args.clone()),
                };
                let (herdr, ran) = answering(machine, 0, &result("{}"), "");
                if replay(&herdr, &command).is_none() {
                    continue;
                }
                let first = ran.lock().unwrap().first().cloned().unwrap().0;
                assert_eq!(first[1..], args[..], "{scenario}");
                replayed += 1;
            }
        }
    }
    assert!(replayed > 900, "{replayed} commands replayed");
}

#[test]
fn every_recorded_agent_starts_with_the_launch_arguments_of_its_run() {
    let mut starts = 0;
    for (scenario, recording) in recordings() {
        // The run's state as last saved before each command, across the scenario's invocations in order.
        let mut state: Option<RunState> = None;
        let events = recording.get("events").and_then(Json::as_object).unwrap();
        let mut invocations: Vec<(u32, &Json)> = events.iter().map(|(n, e)| (n.parse().unwrap(), e)).collect();
        invocations.sort_by_key(|(n, _)| *n);
        for event in invocations.iter().flat_map(|(_, e)| e.as_array().unwrap()) {
            if let Some(saved) = event.get("state").and_then(|s| s.get("saved")).and_then(Json::as_object) {
                state = Some(RunState::from_saved(saved).unwrap());
                continue;
            }
            let Some(args) = event.get("herdr").and_then(Json::as_array) else { continue };
            let args: Vec<&str> = args.iter().map(|a| a.as_str().unwrap()).collect();
            let args = if args[0] == "--machine" { &args[2..] } else { &args[..] };
            let ["agent", "start", name, "--kind", kind, "--pane", _, "--timeout", _, rest @ ..] = args else {
                continue;
            };
            let s = state.as_ref().unwrap_or_else(|| panic!("{scenario}: {name} starts before the run is saved"));
            let role = name.split('-').next().unwrap();
            let given: Vec<String> = rest.iter().skip(1).map(|a| a.to_string()).collect();
            // The session it resumes, when it does, is its role's agent's as last saved.
            let session = resumed_session(kind, &given);
            if let Some(session) = &session {
                assert_eq!(s.agents.get(role).and_then(|a| a.get("session")), Some(session), "{scenario} {name}");
            }
            let extra_dir = s.worktree.is_some().then(|| s.dir());
            let expected = harness(kind).unwrap().launch_args(
                s.permission_mode.as_deref(),
                s.models.get(role).map(String::as_str),
                session.as_deref(),
                extra_dir.as_deref(),
            );
            assert_eq!(given, expected, "{scenario} {name}");
            starts += 1;
        }
    }
    assert!(starts > 100, "{starts} starts checked");
}

// -- FakeHerdr -------------------------------------------------------------------------------------------------------

/// A turn that writes text to the path path_of gives for the run, then shows status.
fn writes(path_of: fn(&RunState) -> String, text: &'static str, status: &'static str) -> Turn {
    Box::new(move |_, run, host| {
        host.write(&path_of(run), text, false).unwrap();
        status.to_string()
    })
}

#[test]
fn fakeherdr_plays_each_role_s_turns_as_python_s_does() {
    let host = Rc::new(FakeHost::new(Some("abc123"), "main"));
    let run = RunState::new("20260929-120000-a1b2c3", "add a rate limiter", "/proj");
    let spec: Turn = writes(|s| s.spec_path(), "# Spec", "done");
    let prompts = Rc::new(std::cell::RefCell::new(vec![]));
    let seen = prompts.clone();
    let build: Turn = Box::new(move |prompt, _, _| {
        seen.borrow_mut().push(prompt.to_string());
        "idle".to_string()
    });
    let herdr = FakeHerdr::new(host.clone(), run.clone(), vec![("spec", vec![spec]), ("build", vec![build])]);

    let (workspace, root) = herdr.create_workspace("/proj", "a1b2c3 task").unwrap();
    assert_eq!((workspace.as_str(), root.as_str()), ("w1", "w1:p1"));
    assert!(herdr.workspace_exists("w1").unwrap() && herdr.pane_exists("w1:p1").unwrap());
    let pane = herdr.split(&root, Direction::Right, "/proj").unwrap();
    assert_eq!(pane, "w1:p2");
    herdr.rename_pane(&pane, "Builder").unwrap();

    assert_eq!(herdr.start_agent("spec-a1b2c3", &root, "claude", &[]), Ok(true));
    assert_eq!(herdr.status("spec-a1b2c3"), Ok(Some("idle".to_string())));
    herdr.prompt("spec-a1b2c3", "You are the Spec Collector").unwrap();
    assert_eq!(host.read(&run.spec_path()).unwrap().as_deref(), Some("# Spec"));
    assert_eq!(herdr.wait("spec-a1b2c3", Some(5000), &["working", "blocked"]), Ok("working".to_string()));
    assert_eq!(herdr.wait("spec-a1b2c3", None, &["done"]), Ok("done".to_string()));
    assert_eq!(herdr.agent("spec-a1b2c3").unwrap().unwrap().status, "done");

    // A role blocked at its start, an agent that drops its prompt, and one resumed into a session it lost.
    herdr.state().blocked_at_start.insert("build".to_string());
    herdr.state().dropped.insert("build".to_string(), 1);
    let resume = argv(&["resume", "s-1", "--full-auto"]);
    assert_eq!(herdr.start_agent("build-a1b2c3", &pane, "codex", &resume), Ok(false));
    assert_eq!(herdr.agent("build-a1b2c3").unwrap().unwrap().session.as_deref(), Some("s-1"));
    herdr.prompt("build-a1b2c3", "first").unwrap();
    let e = herdr.wait("build-a1b2c3", Some(1000), &["working"]).unwrap_err();
    assert_eq!(e.code(), Some("timeout"));
    herdr.prompt("build-a1b2c3", "second").unwrap();
    assert_eq!(*prompts.borrow(), ["second"]);
    herdr.state().lost_sessions.insert("s-gone".to_string());
    assert_eq!(herdr.start_agent("review-a1b2c3", "w1:p3", "pi", &argv(&["--session", "s-gone"])), Ok(true));
    assert_eq!(herdr.status("review-a1b2c3"), Ok(None));
    let e = herdr.wait("review-a1b2c3", Some(1000), &["working"]).unwrap_err();
    assert_eq!(e.code(), Some("agent_not_found"));

    // Once the run's state.json is saved, a turn sees the state as the Workflow saved it, as Python's turns share it.
    let mut saved = run.clone();
    saved.worktree = Some("/proj/.orchestrator/worktrees/a1b2c3".to_string());
    host.write(&format!("{}/state.json", run.dir()), &saved.to_text(), false).unwrap();
    let worktree: Turn = Box::new(|_, run, _| run.worktree.clone().unwrap_or_default());
    herdr.state().script.insert("review".to_string(), [worktree].into());
    assert_eq!(herdr.start_agent("review-b", "w1:p4", "claude", &[]), Ok(true));
    herdr.prompt("review-b", "review").unwrap();
    assert_eq!(herdr.status("review-b"), Ok(Some("/proj/.orchestrator/worktrees/a1b2c3".to_string())));

    herdr.focus("build-a1b2c3").unwrap();
    herdr.close_workspace("w1").unwrap();
    assert!(!herdr.workspace_exists("w1").unwrap());
    let s = herdr.state();
    let start = |name: &str, pane: &str, args: Vec<String>| Call::Start { name: name.into(), pane: pane.into(), args };
    let prompt = |name: &str| Call::Prompt { name: name.into() };
    assert_eq!(s.calls, [
        Call::Workspace { label: "a1b2c3 task".into() },
        Call::Split { pane: "w1:p1".into(), direction: Direction::Right },
        Call::Rename { pane: "w1:p2".into(), label: "Builder".into() },
        start("spec-a1b2c3", "w1:p1", vec![]),
        prompt("spec-a1b2c3"),
        start("build-a1b2c3", "w1:p2", resume),
        prompt("build-a1b2c3"),
        prompt("build-a1b2c3"),
        start("review-a1b2c3", "w1:p3", argv(&["--session", "s-gone"])),
        start("review-b", "w1:p4", vec![]),
        prompt("review-b"),
        Call::Focus { name: "build-a1b2c3".into() },
        Call::Close { workspace: "w1".into() },
    ]);
    assert_eq!(s.kinds.iter().map(|(_, k)| k.as_str()).collect::<Vec<_>>(), ["claude", "codex", "pi", "claude"]);
    assert_eq!(s.sessions.get("spec-a1b2c3").map(String::as_str), Some("session-4"));
    assert_eq!(s.waits, [vec!["done".to_string()]]);
    assert_eq!(s.deliveries.len(), 3);
    assert_eq!(s.prompts.len(), 4);
    assert_eq!(s.cwds, [("workspace".to_string(), "/proj".to_string()), ("split".to_string(), "/proj".to_string())]);
    assert_eq!(s.panes, 2);
    assert!(s.script.values().all(|turns| turns.is_empty()));
    assert_eq!(s.run.run_id, run.run_id);
}

#[test]
fn a_resumed_session_is_read_in_each_harness_s_syntax() {
    for (kind, args, session) in [
        ("claude", vec!["--permission-mode", "plan", "--resume", "s"], Some("s")),
        ("codex", vec!["resume", "s", "--full-auto"], Some("s")),
        ("codex", vec!["--full-auto", "resume", "s"], None),
        ("gemini", vec!["--resume", "s"], Some("s")),
        ("opencode", vec!["--session", "s"], Some("s")),
        ("pi", vec!["--model", "m"], None),
        ("pi", vec!["--session"], None),
    ] {
        assert_eq!(resumed_session(kind, &argv(&args)).as_deref(), session, "{kind} {args:?}");
    }
}

#[test]
fn errors_and_directions_as_herdr_takes_them() {
    let failed = HerdrError::Failed(OrchestratorError::new("herdr is not on PATH"));
    assert_eq!((failed.code(), failed.to_string()), (None, "herdr is not on PATH".to_string()));
    assert_eq!(OrchestratorError::from(failed).0, "herdr is not on PATH");
    assert_eq!((Direction::Right.as_str(), Direction::Down.as_str()), ("right", "down"));
}
