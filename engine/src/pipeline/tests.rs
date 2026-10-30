use super::*;
use crate::test_support::{four, four_roles, four_steps, impl_, quick, replaced, roles, solo};

fn step(id: &str, file: &str) -> Step {
    Step::new(id, id, file, "go")
}

fn with(mut st: Step, change: impl FnOnce(&mut Step)) -> Step {
    change(&mut st);
    st
}

fn bad(steps: Vec<Step>) -> String {
    Pipeline::new("bad", roles(&[("build", "Builder"), ("review", "Reviewer")]), steps).unwrap_err()
}

#[test]
fn default_is_the_only_registered_workflow() {
    assert_eq!(WORKFLOWS, ["default"]);
    assert_eq!(DEFAULT_WORKFLOW.name(), "default");
    assert_eq!(workflow_shape(&DEFAULT_WORKFLOW), "spec -> build -> review (back to build)");
    assert!(is_built_in(&DEFAULT_WORKFLOW, &Prompts::default()));
    assert!(!is_built_in(&quick(), &Prompts::default()));
    assert_eq!(
        find_workflow("quick", &Prompts::default()).unwrap_err().0,
        "unknown workflow quick; this orchestrator has default"
    );
}

#[test]
fn invalid_definitions() {
    let build = with(step("build", "a-{n}.md"), |s| s.edits = true);
    let review = step("review", "b-{n}.md");
    let cases: Vec<(&str, Vec<Step>)> = vec![
        ("duplicate step id build", vec![build.clone(), with(step("build", "b-{n}.md"), |s| s.role = "review".into())]),
        ("duplicate handoff file a-{n}.md", vec![build.clone(), step("review", "a-{n}.md")]),
        (
            "step review loops back to nope, which is not a step",
            vec![build.clone(), with(step("review", "b-{n}.md"), |s| s.loop_to = Some("nope".into()))],
        ),
        (
            "step build loops back to review, which does not come before it",
            vec![with(step("build", "a-{n}.md"), |s| { s.edits = true; s.loop_to = Some("review".into()) }), review.clone()],
        ),
        (
            "step build loops back to build, which does not come before it",
            vec![with(step("build", "a-{n}.md"), |s| { s.edits = true; s.loop_to = Some("build".into()) }), review.clone()],
        ),
        (
            "steps build, review each loop back; only one verdict loop is supported",
            vec![
                with(step("prep", "p-{n}.md"), |s| { s.role = "build".into(); s.edits = true }),
                with(step("build", "a-{n}.md"), |s| s.loop_to = Some("prep".into())),
                with(step("review", "b-{n}.md"), |s| s.loop_to = Some("build".into())),
            ],
        ),
        (
            "steps build, review are each quality-gated; only one may be",
            vec![
                with(step("build", "a-{n}.md"), |s| { s.edits = true; s.quality_gated = true }),
                with(step("review", "b-{n}.md"), |s| { s.edits = true; s.quality_gated = true }),
            ],
        ),
        (
            "step build is quality-gated, so it must edit",
            vec![
                with(step("prep", "p-{n}.md"), |s| { s.role = "review".into(); s.edits = true }),
                with(step("build", "a-{n}.md"), |s| s.quality_gated = true),
            ],
        ),
        (
            "step build is quality-gated, so its handoff file needs {n}",
            vec![with(step("build", "a.md"), |s| { s.edits = true; s.quality_gated = true }), review.clone()],
        ),
        (
            "step review is quality-gated, so it cannot be the verdict step",
            vec![
                build.clone(),
                with(step("review", "b-{n}.md"), |s| { s.edits = true; s.quality_gated = true; s.loop_to = Some("build".into()) }),
            ],
        ),
        ("role review has no step", vec![build.clone()]),
        (
            "step review is in the review loop, so its handoff file needs {n}",
            vec![build.clone(), with(step("review", "b.md"), |s| s.loop_to = Some("build".into()))],
        ),
        (
            "step build: a per-round handoff file needs an editing step at or before it",
            vec![step("build", "a-{n}.md"), with(step("review", "b-{n}.md"), |s| s.edits = true)],
        ),
        ("step id done is reserved", vec![build.clone(), with(step("done", "b.md"), |s| s.role = "review".into())]),
        ("step id quality is reserved", vec![build.clone(), with(step("quality", "b.md"), |s| s.role = "review".into())]),
        ("has no steps", vec![]),
        (
            "step build: handoff file a-{round}.md may use only {n}",
            vec![with(step("build", "a-{round}.md"), |s| s.edits = true), review.clone()],
        ),
        (
            "prompt placeholder prev_review_path names the files of two steps",
            vec![build.clone(), review.clone(), with(step("prev_review", "c-{n}.md"), |s| s.role = "review".into())],
        ),
    ];
    for (problem, steps) in cases {
        let e = bad(steps);
        assert!(e.starts_with(&format!("workflow bad: {problem}")), "{problem}: {e}");
    }
}

#[test]
fn role_without_label() {
    let steps = vec![with(step("build", "a.md"), |s| s.edits = true), step("review", "b.md")];
    let e = Pipeline::new("bad", roles(&[("review", "Reviewer")]), steps.clone()).unwrap_err();
    assert_eq!(e, "workflow bad: step build: role build has no label");
    let e = Pipeline::new("bad", roles(&[("build", ""), ("review", "Reviewer")]), steps).unwrap_err();
    assert_eq!(e, "workflow bad: role build has no label");
}

#[test]
fn unknown_placeholder() {
    let st = with(Step::new("build", "build", "a-{n}.md", "write {spec_path}"), |s| s.edits = true);
    let e = Pipeline::new("bad", roles(&[("build", "Builder")]), vec![st]).unwrap_err();
    assert_eq!(e, "workflow bad: step build: unknown prompt placeholder spec_path");
}

#[test]
fn quality_fix_report_names() {
    assert_eq!(Step::new("b", "b", "build-{n}.md", "go").path("/d", 2, 3), "/d/build-2-q3.md");
    assert_eq!(Step::new("b", "b", "impl-{n}", "go").path("/d", 1, 1), "/d/impl-1-q1");
    let gated = with(Step::new("b", "b", "build-{n}.md", "go"), |s| s.quality_gated = true);
    assert_eq!(gated.last_path("/d", 2, 3), "/d/build-2-q2.md");
    assert_eq!(gated.last_path("/d", 2, 0), "/d/build-2.md");
    assert_eq!(Step::new("b", "b", "build-{n}.md", "go").last_path("/d", 2, 3), "/d/build-2.md");
}

#[test]
fn steps_whose_files_can_share_a_name() {
    let default = &*DEFAULT_WORKFLOW;
    let (spec, build, review) = (&default.steps()[0], &default.steps()[1], &default.steps()[2]);
    let b = with(Step::new("b", "b", "x{n}.md", "{path}"), |s| s.edits = true);
    let gated_a = with(Step::new("a", "a", "b.{n}", "{path}"), |s| { s.edits = true; s.quality_gated = true });
    let looping = |id: &str, role: &str, file: &str| with(Step::new(id, role, file, "{path}"), |s| s.loop_to = Some("a".into()));
    let editing = |id: &str, file: &str| with(Step::new(id, id, file, "{path}"), |s| s.edits = true);
    let cases: Vec<(&str, Vec<Step>)> = vec![
        ("steps spec and build can both write build-1.md", vec![Step::new("spec", "spec", "build-1.md", "{path}"), build.clone(), review.clone()]),
        (
            "steps build and review can both write build-1-q1.md",
            vec![spec.clone(), build.clone(), with(Step::new("review", "review", "build-{n}-q1.md", "{path}"), |s| s.loop_to = Some("build".into()))],
        ),
        ("steps b and r can both write x1.md", vec![b.clone(), Step::new("r", "b", "x1.md", "{path}")]),
        // {n} in the extension: a quality answer goes before it, as b-q3.1.
        ("steps a and r can both write b-q3.1", vec![gated_a.clone(), looping("r", "r", "b-q3.{n}")]),
        ("steps a and z can both write b-q3.1", vec![gated_a.clone(), Step::new("z", "z", "b-q3.1", "{path}")]),
        // Rounds that differ give one name: round 3 of a and round 5 of r.
        ("steps a and r can both write x35.md", vec![editing("a", "x{n}5.md"), looping("r", "r", "x3{n}.md")]),
        ("steps a and r can both write x3-5.md", vec![editing("a", "x{n}-5.md"), looping("r", "r", "x3-{n}.md")]),
        ("steps a and r can both write 31.33", vec![editing("a", "{n}.33"), looping("r", "r", "31.{n}")]),
    ];
    for (message, steps) in cases {
        let role_map = steps.iter().map(|st| (st.role.clone(), st.role.clone())).collect();
        let e = Pipeline::new("bad", role_map, steps).unwrap_err();
        assert!(e.contains(message), "{message}: {e}");
    }
    // The gated step's own answers, and names that only look alike, are not collisions.
    let ok = |steps: Vec<Step>| Pipeline::new("ok", roles(&[("b", "B")]), steps).unwrap();
    let back_to_b = |file: &str| with(Step::new("r", "b", file, "{path}"), |s| s.loop_to = Some("b".into()));
    ok(vec![with(Step::new("b", "b", "x{n}.md", "{path}"), |s| { s.edits = true; s.quality_gated = true }), back_to_b("x{n}-r.md")]);
    ok(vec![Step::new("s", "b", "notes", "{path}"), with(Step::new("b", "b", "notes-{n}", "{path}"), |s| { s.edits = true; s.quality_gated = true })]);
    // A round number is never 0 and has no leading zero, and a digit beside {n} is fine on its own.
    ok(vec![Step::new("s", "b", "x0.md", "{path}"), with(Step::new("b", "b", "x{n}.md", "{path}"), |s| s.edits = true), back_to_b("x0{n}.md")]);
    ok(vec![with(Step::new("b", "b", "x-{n}.md", "{path}"), |s| s.edits = true), back_to_b("x3{n}.md")]);
}

#[test]
fn a_model_or_agent_for_a_role_not_in_the_workflow() {
    let steps = vec![Step::new("build", "build", "b.md", "{path}")];
    let e = Pipeline::with("bad", roles(&[("build", "Builder")]), steps.clone(), PipelineSettings {
        models: roles(&[("x", "m")]),
        ..PipelineSettings::default()
    })
    .unwrap_err();
    assert_eq!(e, "workflow bad: a model is set for role x, which is not one of the workflow's roles");
    let e = replaced(&quick(), |s| s.agents = roles(&[("x", "pi")])).unwrap_err();
    assert_eq!(e, "workflow quick: an agent is set for role x, which is not one of the workflow's roles");
    let e = replaced(&quick(), |s| s.agents = roles(&[("review", "cursor")])).unwrap_err();
    assert_eq!(
        e,
        "workflow quick: role review: agent cursor is not a harness this orchestrator supports; it takes claude, \
         codex, gemini, opencode, pi"
    );
}

#[test]
fn only_an_editing_step_may_lock() {
    let q = quick();
    let steps = vec![q.steps()[0].clone(), with(q.steps()[1].clone(), |s| s.locks = true)];
    assert!(Pipeline::new("bad", q.roles().clone(), steps).unwrap_err().ends_with("step review locks the files it changes, so it must edit"));
    // Its lock would hold the earlier step's work too.
    let steps = four_steps().into_iter().map(|st| with(st, |s| s.locks = s.id == "build")).collect();
    assert!(Pipeline::new("bad", four_roles(), steps).unwrap_err().ends_with("step build locks the files it changes, so it must be the first editing step"));
    // A second turn of it would lock the Builder's work of the round before.
    let steps = four_steps()
        .into_iter()
        .map(|st| match st.id.as_str() {
            "tests" => with(st, |s| { s.locks = true; s.file = "tests-{n}.md".into() }),
            "review" => with(st, |s| s.loop_to = Some("tests".into())),
            _ => st,
        })
        .collect();
    assert!(Pipeline::new("bad", four_roles(), steps).unwrap_err().ends_with("step tests locks the files it changes, so the verdict loop must not run it again"));
}

#[test]
fn proof_command_needs_a_verdict_step_and_names_the_proof_once() {
    let e = replaced(&solo(), |s| s.proof_command = Some("sh {proofs}".into())).unwrap_err();
    assert_eq!(e, "workflow solo: proof_command runs the verdict step's proofs, and the workflow has no verdict step");
    for command in ["sh", "sh {proofs} {proofs}", "sh {proofs} {task}", "sh {proofs"] {
        let e = replaced(&quick(), |s| s.proof_command = Some(command.into())).unwrap_err();
        assert!(e.starts_with("workflow quick: proof_command"), "{command}: {e}");
    }
    assert!(replaced(&quick(), |s| s.proof_command = Some("sh {proofs}".into())).is_ok());
}

#[test]
fn a_change_budget_below_one_is_refused() {
    let e = replaced(&quick(), |s| s.max_files = 0).unwrap_err();
    assert_eq!(e, "workflow quick: max_files must be at least 1");
    let e = replaced(&quick(), |s| s.max_lines = -1).unwrap_err();
    assert_eq!(e, "workflow quick: max_lines must be at least 1");
}

#[test]
fn the_pipelines_parts() {
    let p = four();
    assert_eq!(p.step("tests").map(|st| st.file.as_str()), Some("tests.md"));
    assert_eq!(p.step("nope"), None);
    assert_eq!(p.after("tests").map(|st| st.id.as_str()), Some("build"));
    assert_eq!(p.after("review"), None);
    assert_eq!(p.first_edit().map(|st| st.id.as_str()), Some("tests"));
    assert_eq!(p.last_edit().map(|st| st.id.as_str()), Some("build"));
    assert_eq!(p.verdict_step().map(|st| st.id.as_str()), Some("review"));
    assert_eq!(p.gated(), None);
    assert_eq!(p.contract().map(|st| st.id.as_str()), Some("spec"));
    assert_eq!(p.result().id, "review");
    assert_eq!(quick().contract(), None);
    assert_eq!(solo().result().id, "build");
    assert!(!p.steps()[0].can_block() && p.steps()[1].can_block() && !p.steps()[3].can_block());
    let names: Vec<String> = p.path_placeholders().into_iter().take(3).map(|(name, _, _)| name).collect();
    assert_eq!(names, ["spec_path", "prev_spec_path", "earlier_spec_paths"]);
    assert_eq!(DEFAULT_WORKFLOW.gated().map(|st| st.id.as_str()), Some("build"));
    assert_eq!(impl_().gated().map(|st| st.name(2, 1)), Some("impl-2-q1.md".to_string()));
    assert_eq!((p.max_files(), p.max_lines(), p.proof_command(), p.description()), (15, 600, None, ""));
    assert!(p.models().is_empty() && p.agents().is_empty());
}

#[test]
fn run_files_are_the_runs_own() {
    for name in ["state.json", "summary.md", "metrics.json", "quality-1-2.md", "locked-x", "proofs-1"] {
        assert!(is_run_file(name), "{name}");
    }
    for name in ["spec.md", "state.jsonx", "quality-a\nb", "quality"] {
        assert!(!is_run_file(name), "{name:?}");
    }
}

#[test]
fn the_default_is_worded_by_its_prompts() {
    let mut overrides = Map::new();
    overrides.insert("BUILD_PROMPT", "Build {spec_path} in {cwd}; report to {build_path}.".to_string());
    let prompts = Prompts::from_overrides(&overrides).unwrap();
    let p = find_workflow("default", &prompts).unwrap();
    assert_eq!(p.step("build").unwrap().prompt, "Build {spec_path} in {cwd}; report to {build_path}.");
    assert!(is_built_in(&p, &prompts));
    assert!(!is_built_in(&p, &Prompts::default()));
}
