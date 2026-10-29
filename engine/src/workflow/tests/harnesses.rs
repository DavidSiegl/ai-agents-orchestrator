//! TestHarnesses, the Workflow's and resumable_state's cases of TestHarnessFlags, and TestWorkflowFileAgents'
//! test_saved_with_the_run: each role's agent started, resumed and saved in its harness.
//!
//! Not here: TestHarnessFlags' cases of parse_args, new_state and main (test_precedence, test_flags_reach_the_workflow,
//! test_role_agent_for_a_role_the_workflow_lacks, test_unknown_kinds_are_argparse_errors), which ticket 09 ports with
//! the CLI; and TestWorkflowFileAgents' test_workflows_prints_it, the workflows command's (ticket 09).
//! TestWorkflowFileAgents' other cases, and TestHarnessFlags' of launch_args and of reading a 0.3.0 state, are
//! crate::workflow_file's, crate::harness's and crate::state's (tickets 04 and 06).

use super::worktree::worktree_run;
use crate::test_support::resumable;
use super::*;

const KINDS: &[&str] = &["claude", "codex", "gemini", "opencode", "pi"];
const MODES: &[&str] = &["default", "acceptEdits", "bypassPermissions", "plan", "auto", "dontAsk"];

/// The spec's table of what --permission-mode becomes in each harness; None is dropped.
fn permissions(kind: &str, mode: &str) -> Option<Vec<String>> {
    let args: Option<&[&str]> = match (kind, mode) {
        ("claude", _) => return Some(strings(&["--permission-mode", mode])),
        ("gemini", "default") => Some(&["--approval-mode", "default"]),
        ("gemini", "acceptEdits") => Some(&["--approval-mode", "auto_edit"]),
        ("gemini", "bypassPermissions") => Some(&["--approval-mode", "yolo"]),
        ("codex", "default") => Some(&[]),
        ("codex", "acceptEdits") => Some(&["--full-auto"]),
        ("codex", "bypassPermissions") => Some(&["--dangerously-bypass-approvals-and-sandbox"]),
        ("codex", "plan") => Some(&["--sandbox", "read-only"]),
        _ => None,
    };
    args.map(strings)
}

/// Every role of the default workflow in harness kind.
fn every_role(kind: &str) -> Map<String> {
    FULL.iter().map(|role| (*role, kind.to_string())).collect()
}

/// AgentSettings of a permission mode, the models and the harnesses by role.
fn agent_settings(permission_mode: Option<&str>, models: &[(&str, &str)], kinds: Map<String>) -> AgentSettings {
    AgentSettings {
        permission_mode: permission_mode.map(String::from),
        models: models.iter().map(|(r, m)| (*r, m.to_string())).collect(),
        kinds,
    }
}

/// The default settings, with agents.
fn with_agents(agents: AgentSettings) -> RunSettings {
    RunSettings { agents, ..RunSettings::default() }
}

/// (role, harness, arguments) of each agent started, in order.
type Started = Vec<(String, String, Vec<String>)>;

/// A whole run of the default workflow with settings: the agents it started, and what it logged.
fn run_default(settings: RunSettings) -> (Started, Vec<String>) {
    let f = Fixture::new(approved());
    assert_eq!(f.workflow(settings).run(), verdict(APPROVE));
    let kinds = f.herdr.state().kinds.clone();
    let kind_of = |name: &str| kinds.iter().find(|(n, _)| n == name).map(|(_, kind)| kind.clone()).expect("a start's kind");
    let started = starts(&f).into_iter().map(|(name, _, args)| (role_of(&name), kind_of(&name), args));
    (started.collect(), f.told.lines())
}

/// The role of an agent named <role>-<key>.
fn role_of(name: &str) -> String {
    name.split('-').next().unwrap().to_string()
}

/// The starts of the default workflow's roles in order, all in harness kind, each with its args.
fn expected_starts(kind: &str, args: [Vec<String>; 3]) -> Started {
    FULL.iter().zip(args).map(|(role, args)| (role.to_string(), kind.to_string(), args)).collect()
}

/// The log lines that say a harness drops the permission mode.
fn dropped(logged: &[String]) -> Vec<String> {
    logged.iter().filter(|l| l.contains("no equivalent")).cloned().collect()
}

#[test]
fn a_new_agent_in_each_harness() {
    for kind in KINDS {
        let (started, _) = run_default(with_agents(agent_settings(None, &[("build", "m")], every_role(kind))));
        assert_eq!(started, expected_starts(kind, [vec![], strings(&["--model", "m"]), vec![]]), "{kind}");
    }
}

#[test]
fn each_permission_mode_in_each_harness() {
    for kind in KINDS {
        for mode in MODES {
            let (started, logged) = run_default(with_agents(agent_settings(Some(mode), &[("review", "m")], every_role(kind))));
            let expected = permissions(kind, mode);
            let args = expected.clone().unwrap_or_default();
            let review = [args.clone(), strings(&["--model", "m"])].concat();
            assert_eq!(started, expected_starts(kind, [args.clone(), args, review]), "{kind} {mode}");
            let told: Vec<String> = match expected {
                None => FULL
                    .iter()
                    .map(|role| format!("[{role}] {kind} has no equivalent of --permission-mode {mode}; starting it without one"))
                    .collect(),
                Some(_) => vec![],
            };
            assert_eq!(dropped(&logged), told, "{kind} {mode}");
        }
    }
}

#[test]
fn without_a_harness_every_role_is_claude_as_before() {
    let (started, logged) = run_default(with_agents(agent_settings(Some("auto"), &[], Map::new())));
    let auto = strings(&["--permission-mode", "auto"]);
    assert_eq!(started, expected_starts("claude", [auto.clone(), auto.clone(), auto]));
    assert!(dropped(&logged).is_empty());
}

#[test]
fn a_dropped_mode_is_logged_once_per_role() {
    let mut state = saved_run("build", 2, FULL);
    state.agents.get_mut("build").unwrap().insert("kind", "pi".to_string());
    let script = vec![("build", vec![build_turn(2)]), ("review", vec![review_turn(2, APPROVE)])];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, script, &["review"], &[("build", "s-gone")]);
    f.herdr.state().lost_sessions.insert("s-gone".into());
    let settings = RunSettings { agents: agent_settings(Some("plan"), &[], every_role("pi")), ..f.as_saved() };
    assert_eq!(f.workflow(settings).run(), verdict(APPROVE));
    let args: Vec<Vec<String>> = starts(&f).into_iter().map(|(_, _, args)| args).collect();
    assert_eq!(args, [strings(&["--session", "s-gone"]), vec![]]);
    assert_eq!(dropped(&f.told.lines()).len(), 1);
}

#[test]
fn an_exited_agent_resumes_in_its_own_harness() {
    let resumed = |kind: &str| -> Vec<String> {
        strings(match kind {
            "claude" => &["--permission-mode", "acceptEdits", "--resume", "s-build", "--model", "m"],
            "gemini" => &["--approval-mode", "auto_edit", "--resume", "s-build", "--model", "m"],
            "codex" => &["resume", "s-build", "--full-auto", "--model", "m"],
            "pi" | "opencode" => &["--session", "s-build", "--model", "m"],
            _ => unreachable!("{kind}"),
        })
    };
    for kind in KINDS {
        let seen = seen();
        let mut state = saved_run("build", 2, FULL);
        state.prompted = Some("build-2.md".into());
        state.agents.get_mut("build").unwrap().insert("kind", kind.to_string());
        let script = vec![("build", vec![recording(build_turn(2), &seen)]), ("review", vec![review_turn(2, APPROVE)])];
        let f = Fixture::resume(Rc::new(FakeHost::default()), state, script, &["review"], &[("build", "s-build")]);
        let settings = RunSettings { agents: agent_settings(Some("acceptEdits"), &[("build", "m")], every_role(kind)), ..f.as_saved() };
        let mut wf = f.workflow(settings);
        assert_eq!(wf.run(), verdict(APPROVE), "{kind}");
        assert_eq!(starts(&f), [("build-a1b2c3".into(), "w1:p2".into(), resumed(kind))], "{kind}");
        assert_eq!(f.herdr.state().kinds, [("build-a1b2c3".to_string(), kind.to_string())]);
        assert_eq!(*seen.borrow(), [continue_prompt(&wf.state().build_path(2))]);
        assert_eq!(wf.state().agents.get("build").unwrap().get("kind").map(String::as_str), Some(*kind));
    }
}

#[test]
fn a_changed_harness_starts_fresh() {
    // The saved record without a kind is a claude agent's, as 0.3.0 saved it.
    for (saved_kind, kind) in [(None, "codex"), (Some("claude"), "pi"), (Some("gemini"), "claude")] {
        let seen = seen();
        let mut state = saved_run("build", 2, FULL);
        state.prompted = Some("build-2.md".into());
        if let Some(saved_kind) = saved_kind {
            state.agents.get_mut("build").unwrap().insert("kind", saved_kind.to_string());
        }
        let script = vec![("build", vec![recording(build_turn(2), &seen)]), ("review", vec![review_turn(2, APPROVE)])];
        let f = Fixture::resume(Rc::new(FakeHost::default()), state, script, &["review"], &[("build", "s-build")]);
        let kinds = [("build", kind.to_string())].into_iter().collect();
        let settings = RunSettings { agents: agent_settings(None, &[], kinds), ..f.as_saved() };
        assert_eq!(f.workflow(settings).run(), verdict(APPROVE), "{kind}");
        assert_eq!(starts(&f), [("build-a1b2c3".into(), "w1:p2".into(), vec![])]);
        assert_eq!(f.herdr.state().kinds, [("build-a1b2c3".to_string(), kind.to_string())]);
        let first = &seen.borrow()[0];
        assert!(first.contains("You are the Builder"), "{first}");
        assert!(first.contains("This is round 2, and you are a fresh session"), "{first}");
        let told = format!(
            "[build] session s-build is {}'s, and the role runs in {kind} now; starting a fresh one",
            saved_kind.unwrap_or("claude")
        );
        assert!(f.told.lines().contains(&told), "{:?}", f.told.lines());
    }
}

#[test]
fn a_live_agent_keeps_its_harness() {
    let script = vec![("review", vec![review_turn(1, APPROVE)])];
    let f = Fixture::resume(Rc::new(FakeHost::default()), saved_run("review", 1, FULL), script, &["review"], &[]);
    f.put(&f.state.build_path(1), "report 1");
    let settings = RunSettings { agents: agent_settings(None, &[], every_role("codex")), ..f.as_saved() };
    assert_eq!(f.workflow(settings).run(), verdict(APPROVE));
    assert!(f.herdr.state().kinds.is_empty());
    assert!(called(&f, &Call::Prompt { name: "review-a1b2c3".into() }));
}

#[test]
fn every_roles_harness_is_saved() {
    let f = Fixture::new(approved());
    let kinds = [("review", "pi".to_string())].into_iter().collect();
    f.workflow(with_agents(agent_settings(Some("plan"), &[], kinds))).run().unwrap();
    let saved = f.saved();
    assert_eq!(saved.get("agent_kinds").unwrap().dumps(), r#"{"spec": "claude", "build": "claude", "review": "pi"}"#);
    assert_eq!(text(&saved, "permission_mode").as_deref(), Some("plan"));
    assert!(!saved.contains_key("agent_args"));
    let agents = saved.get("agents").unwrap().as_object().unwrap();
    let recorded: Vec<(&str, &str)> = agents.iter().map(|(role, a)| (role, a.get("kind").unwrap().as_str().unwrap())).collect();
    assert_eq!(recorded, [("spec", "claude"), ("build", "claude"), ("review", "pi")]);
}

// -- TestHarnessFlags, the Workflow's cases ------------------------------------------------------------------------

#[test]
fn role_agent_starts_that_role_in_its_harness() {
    // The harnesses new_state gives `run task --agent gemini --role-agent review=pi`, as test_precedence (ticket 09)
    // checks.
    let expected = [("spec", "gemini"), ("build", "gemini"), ("review", "pi")];
    let f = Fixture::new(approved());
    let kinds = expected.iter().map(|(r, k)| (*r, k.to_string())).collect();
    f.workflow(with_agents(agent_settings(None, &[], kinds))).run().unwrap();
    let started: Vec<(String, String)> = f.herdr.state().kinds.iter().map(|(name, kind)| (role_of(name), kind.clone())).collect();
    assert_eq!(started, expected.map(|(r, k)| (r.to_string(), k.to_string())));
}

/// state.json as 0.3.0 wrote it, in phase and round 2: Claude Code's agent_args, no harnesses, agent records without a
/// kind; the Builder's in session s-build.
fn v030_saved(phase: &str) -> Map<Json> {
    let mut state = saved_run(phase, 2, FULL);
    state.prompted = Some("build-2.md".into());
    state.agents.get_mut("build").unwrap().insert("session", "s-build".to_string());
    let mut saved = state.to_json().as_object().unwrap().clone();
    for key in ["permission_mode", "agent_kinds"] {
        saved.remove(key);
    }
    saved.insert("agent_args", Json::parse(r#"["--permission-mode", "acceptEdits"]"#).unwrap());
    saved
}

#[test]
fn resume_keeps_or_overrides_the_saved_harnesses() {
    let mut saved = saved_run("build", 2, FULL).to_json().as_object().unwrap().clone();
    let kinds = r#"{"spec": "claude", "build": "codex", "review": "gemini"}"#;
    saved.insert("agent_kinds", Json::parse(kinds).unwrap());
    let cases: [(&[&str], Map<String>); 3] = [
        (&[], [("spec", "claude"), ("build", "codex"), ("review", "gemini")].map(|(r, k)| (r, k.to_string())).into_iter().collect()),
        (&["--agent", "pi"], every_role("pi")),
        (
            &["--role-agent", "review=opencode"],
            [("spec", "claude"), ("build", "codex"), ("review", "opencode")].map(|(r, k)| (r, k.to_string())).into_iter().collect(),
        ),
    ];
    for (flags, kinds) in cases {
        assert_eq!(resumable(flags, &saved).unwrap().agent_kinds, kinds, "{flags:?}");
    }
}

#[test]
fn resume_with_a_new_harness_relaunches_in_it() {
    let state = resumable(&["--agent", "pi"], &v030_saved("review")).unwrap();
    let agents = agent_settings(state.permission_mode.as_deref(), &[], state.agent_kinds.clone());
    let script = vec![("review", vec![review_turn(2, APPROVE)])];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, script, &[], &[]);
    f.put(&f.state.build_path(2), "report 2");
    assert_eq!(f.workflow(RunSettings { agents, ..f.as_saved() }).run(), verdict(APPROVE));
    assert_eq!(f.herdr.state().kinds, [("review-a1b2c3".to_string(), "pi".to_string())]);
}

#[test]
fn a_030_state_resumes_on_claude() {
    let state = resumable(&[], &v030_saved("build")).unwrap();
    assert_eq!((state.permission_mode.as_deref(), state.agent_kinds.is_empty()), (Some("acceptEdits"), true));
    let seen = seen();
    let mut agents = agent_settings(state.permission_mode.as_deref(), &[], state.agent_kinds.clone());
    agents.models = state.models.clone();
    let script = vec![("build", vec![recording(build_turn(2), &seen)]), ("review", vec![review_turn(2, APPROVE)])];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, script, &[], &[]);
    let mut wf = f.workflow(RunSettings { agents, ..f.as_saved() });
    assert_eq!(wf.run(), verdict(APPROVE));
    let args: Vec<Vec<String>> = starts(&f).into_iter().map(|(_, _, args)| args).collect();
    let accept = strings(&["--permission-mode", "acceptEdits"]);
    assert_eq!(args, [strings(&["--permission-mode", "acceptEdits", "--resume", "s-build"]), accept]);
    let kinds = f.herdr.state().kinds.clone();
    assert_eq!(kinds, [("build-a1b2c3".to_string(), "claude".to_string()), ("review-a1b2c3".into(), "claude".into())]);
    assert_eq!(*seen.borrow(), [continue_prompt(&wf.state().build_path(2))]);
    let saved = f.saved();
    assert_eq!(text(&saved, "permission_mode").as_deref(), Some("acceptEdits"));
    assert_eq!(saved.get("agent_kinds").unwrap().dumps(), r#"{"spec": "claude", "build": "claude", "review": "claude"}"#);
}

// -- TestWorkflowFileAgents ----------------------------------------------------------------------------------------

#[test]
fn saved_with_the_run() {
    let quick = crate::test_support::quick();
    let mut settings = quick.settings().clone();
    settings.agents.insert("review", "codex".to_string());
    let p = crate::pipeline::Pipeline::with("coded", quick.roles().clone(), quick.steps().to_vec(), settings).unwrap();
    let f = Fixture::on(Rc::new(FakeHost::default()), new_run(&p), vec![
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    let mut wf = f.workflow(RunSettings { pipeline: Some(p.clone()), pull_request: false, ..RunSettings::default() });
    assert_eq!(wf.run(), verdict(APPROVE));
    let definition = wf.state().workflow_definition.clone().unwrap();
    let review = definition.get("roles").and_then(|r| r.get("review")).unwrap();
    assert_eq!(review.dumps(), r#"{"label": "Reviewer", "agent": "codex"}"#);
    let back = crate::workflow_file::run_pipeline("coded", Some(&definition), &crate::prompts::Prompts::default()).unwrap();
    assert_eq!(back, p);
}

#[test]
fn run_dir_granted_in_worktree_runs_only() {
    // TestHarnessFlags.test_run_dir_granted_in_worktree_runs_only, its Workflow half; the launch_args half is
    // crate::harness's.
    let f = worktree_run(Rc::new(FakeHost::default()), None);
    f.workflow(RunSettings::default()).run().unwrap();
    let args: Vec<Vec<String>> = starts(&f).into_iter().map(|(_, _, args)| args).collect();
    assert_eq!(args, vec![strings(&["--add-dir", &f.state.dir()]); 3]);
    let f = Fixture::new(approved());
    f.workflow(RunSettings::default()).run().unwrap();
    let args: Vec<Vec<String>> = starts(&f).into_iter().map(|(_, _, args)| args).collect();
    assert_eq!(args, vec![Vec::<String>::new(); 3]);
}
