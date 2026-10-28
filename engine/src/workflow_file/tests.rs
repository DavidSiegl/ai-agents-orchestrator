use super::*;
use crate::pipeline::workflow_shape;
use crate::prompts::Prompt;
use crate::test_support::{locking, oracle, quick, replaced, roles, TempDir, EXAMPLES};

fn built_in() -> Prompts {
    Prompts::default()
}

fn toml(text: &str) -> Json {
    parse_toml(text).unwrap()
}

/// A configuration directory with a workflows directory, and a writer of workflow files into it.
struct Config {
    dir: TempDir,
    config: ConfigDir,
}

impl Config {
    fn new() -> Self {
        let dir = TempDir::new();
        std::fs::create_dir(dir.join("workflows")).unwrap();
        let config = ConfigDir::at(&dir.join(""));
        Config { dir, config }
    }

    /// The path of the workflow file name.toml written with text.
    fn file(&self, name: &str, text: &str) -> String {
        let path = self.dir.join(&format!("workflows/{name}.toml"));
        std::fs::write(&path, text).unwrap();
        path
    }
}

fn example(name: &str) -> Pipeline {
    load_workflow_file(&format!("{EXAMPLES}/{name}.toml"), &built_in()).unwrap()
}

#[test]
fn every_workflow_parses_to_the_python_engines_definition() {
    let cases = oracle("workflows.json");
    let cases = cases.as_array().unwrap();
    assert!(cases.len() >= 12, "workflows.json holds {} workflows", cases.len());
    for case in cases {
        let (name, from) = (case.get("name").and_then(Json::as_str).unwrap(), case.get("from").unwrap().dumps());
        let text = case.get("toml").and_then(Json::as_str).unwrap();
        let outcome = case.get("outcome").unwrap();
        let parsed = parse_workflow(name, &toml(text), &built_in());
        match outcome.get("error").and_then(Json::as_str) {
            Some(error) => assert_eq!(parsed.unwrap_err(), error, "{from}"),
            None => {
                let p = parsed.unwrap_or_else(|e| panic!("{from}: {e}"));
                let field = |key: &str| outcome.get(key).and_then(Json::as_str).unwrap();
                assert_eq!(workflow_definition(&p).dumps(), field("definition"), "{from}");
                assert_eq!(workflow_toml(&p), field("toml"), "{from}");
                assert_eq!(workflow_shape(&p), field("shape"), "{from}");
                assert_eq!(Some(crate::pipeline::is_built_in(&p, &built_in())), outcome.get("built_in").and_then(Json::as_bool), "{from}");
                // The definition as state.json saves it gives the workflow back too.
                let saved = Json::parse(field("definition")).unwrap();
                assert_eq!(run_pipeline(name, Some(&saved), &built_in()).unwrap(), p, "{from}");
            }
        }
    }
}

#[test]
fn examples_load() {
    let default = &*DEFAULT_WORKFLOW;
    assert_eq!(workflow_shape(&example("quick")), "build -> review (back to build)");
    let team = example("product-team");
    assert_eq!(team.name(), "product-team");
    assert_eq!(workflow_shape(&team), "spec -> design -> tests -> build -> review (back to build)");
    assert_eq!(
        team.roles(),
        &roles(&[("spec", "Product Manager"), ("design", "Architect"), ("tests", "QA Engineer"), ("build", "Engineer"), ("review", "Tech Lead")])
    );
    assert_eq!(team.models(), &roles(&[("tests", "sonnet")]));
    let [spec, design, tests, build, review] = team.steps() else { panic!("five steps") };
    // use keeps every field the file does not set: the interview stays human-paced, the Engineer quality-gated, the
    // Tech Lead the verdict step looping back to the Engineer.
    let mut as_default = spec.clone();
    as_default.prompt = default.step("spec").unwrap().prompt.clone();
    assert_eq!(&as_default, default.step("spec").unwrap());
    assert!(spec.prompt.contains("first line is a `# ` heading"));
    for st in [build, review] {
        let without_texts = |st: &Step| {
            let mut fields = step_fields(st);
            for key in ["prompt", "again", "fresh_note"] {
                fields.remove(key);
            }
            fields
        };
        assert_eq!(without_texts(st), without_texts(default.step(&st.id).unwrap()));
    }
    assert_eq!((design.file.as_str(), design.edits), ("design.md", false));
    assert_eq!((tests.role.as_str(), tests.file.as_str(), tests.edits, tests.locks), ("tests", "tests.md", true, true));
    // Every round of the Engineer and the Tech Lead, and a fresh session of either, hears of the design and the
    // locked acceptance tests, not only the first.
    for st in [build, review] {
        for text in [Some(&st.prompt), st.again.as_ref(), st.fresh_note.as_ref()] {
            let text = text.unwrap();
            assert!(text.contains("{design_path}") && text.contains("{tests_path}"), "{text}");
        }
    }
    assert!(tests.prompt.contains("{design_path}"));
}

#[test]
fn default_round_trips_through_its_file() {
    let text = workflow_toml(&DEFAULT_WORKFLOW);
    assert_eq!(parse_workflow("default", &toml(&text), &built_in()).unwrap(), *DEFAULT_WORKFLOW);
}

#[test]
fn the_change_budget_is_the_workflows() {
    let config = Config::new();
    let step = "id = \"build\"\nfile = \"b-{n}.md\"\nprompt = \"{path}\"\nedits = true\n";
    let p = load_workflow_file(&config.file("small", &format!("max_files = 1\nmax_lines = 100\n[[steps]]\n{step}")), &built_in()).unwrap();
    assert_eq!((p.max_files(), p.max_lines()), (1, 100));
    assert_eq!(parse_workflow("small", &toml(&workflow_toml(&p)), &built_in()).unwrap(), p);
    assert_eq!(workflow_definition(&p).get("max_lines"), Some(&Json::from(100)));
    assert!(workflow_definition(&DEFAULT_WORKFLOW).get("max_files").is_none());
    for (value, message) in [
        ("0", "workflow bad: max_lines must be at least 1"),
        ("-1", "workflow bad: max_lines must be at least 1"),
        ("\"3\"", "max_lines must be a whole number"),
        ("true", "max_lines must be a whole number"),
        ("1.5", "max_lines must be a whole number"),
    ] {
        let path = config.file("bad", &format!("max_lines = {value}\n[[steps]]\n{step}"));
        let e = load_workflow_file(&path, &built_in()).unwrap_err();
        assert!(e.0.contains(message), "{value}: {e}");
    }
}

#[test]
fn any_text_round_trips() {
    let odd = [
        "ends in a quote '", "has ''' inside\nand a newline", "tab\there", "del\x7f", "ünïcode 🦀\n",
        "\nstarts with a newline", "back\\slash \"quoted\"\n",
    ];
    let steps = odd.iter().enumerate().map(|(i, text)| Step::new(&format!("s{i}"), "b", &format!("s{i}.md"), &format!("{text} {{path}}"))).collect();
    let settings = PipelineSettings { models: roles(&[("b", "m")]), description: "with ''' and '".into(), ..PipelineSettings::default() };
    let p = Pipeline::with("odd", roles(&[("b", "Bob's \"pane\"")]), steps, settings).unwrap();
    assert_eq!(parse_workflow("odd", &toml(&workflow_toml(&p)), &built_in()).unwrap(), p);
}

#[test]
fn roles_default_from_the_steps() {
    let config = Config::new();
    let p = load_workflow_file(&config.file("mine", r#"
        [[steps]]
        id = "test_writer"
        file = "tests.md"
        prompt = "Write tests for {task}; report to {path}."
        edits = true

        [[steps]]
        use = "build"
        prompt = "Make {test_writer_path} pass; report to {build_path}."
        again = "Fix them; report to {build_path}."
        fresh_note = "Earlier: {earlier_build_paths}."
        quality_gated = false
    "#), &built_in()).unwrap();
    assert_eq!(p.roles(), &roles(&[("test_writer", "Test Writer"), ("build", "Builder")]));
    assert_eq!(p.steps()[0].role, "test_writer");
    let st = &p.steps()[1];
    assert_eq!((st.edits, st.quality_gated, st.fresh_repeats_again), (true, false, true));
    assert!(p.models().is_empty());
    assert_eq!(default_label("a-b__c-"), "A B C ");
}

#[test]
fn panes_follow_the_steps_not_the_roles_table() {
    let config = Config::new();
    let p = load_workflow_file(&config.file("mine", r#"
        roles.review = { model = "opus" }
        roles.build = "Implementer"
        [[steps]]
        use = "spec"
        [[steps]]
        use = "build"
        [[steps]]
        use = "review"
    "#), &built_in()).unwrap();
    assert_eq!(p.roles(), &roles(&[("spec", "Spec Collector"), ("build", "Implementer"), ("review", "Reviewer")]));
    assert_eq!(p.roles().keys().collect::<Vec<_>>(), ["spec", "build", "review"]);
    assert_eq!(p.models(), &roles(&[("review", "opus")]));
}

#[test]
fn invalid_files_name_the_problem() {
    let config = Config::new();
    let step = "id = \"build\"\nfile = \"b-{n}.md\"\nprompt = \"{path}\"\nedits = true\n";
    let mut cases: Vec<(String, String)> = [
        ("step 1 (build): unknown key edit; did you mean edits?", format!("[[steps]]\n{step}edit = true\n")),
        ("the file: unknown key step; did you mean steps?", format!("[[step]]\n{step}")),
        ("it has no [[steps]]", "description = \"x\"\n".into()),
        ("step 1 (build): file is missing", "[[steps]]\nid = \"build\"\nprompt = \"x\"\n".into()),
        ("step 1 (build): use names no step of the default workflow; it has spec, build, review", format!("[[steps]]\nuse = \"test\"\n{step}")),
        ("step 1 (build): edits must be true or false", format!("[[steps]]\n{}", step.replace("true", "\"yes\""))),
        (
            "step 1 (build): prompt: Single '}' encountered in format string; write a literal brace as {{ or }}",
            "[[steps]]\nid = \"build\"\nfile = \"b-{n}.md\"\nprompt = \"a } b\"\nedits = true\n".into(),
        ),
        ("role build: give a label, or a table with label, model and agent", format!("roles.build = 3\n[[steps]]\n{step}")),
        ("role build: unknown key modle; did you mean model?", format!("roles.build = {{ modle = \"x\" }}\n[[steps]]\n{step}")),
        ("step 1 (a b): id may use only letters, digits, - and _", format!("[[steps]]\n{}", step.replace("\"build\"", "\"a b\""))),
        ("workflow bad: duplicate step id build", format!("[[steps]]\n{step}[[steps]]\n{step}")),
        ("workflow bad: role y has no step", format!("[roles]\ny = {{ model = \"m\" }}\n[[steps]]\n{step}")),
        // tomllib says "Expected '=' after a key"; the toml crate words it its own way.
        ("(at line 1, column 6)", "this is not toml\n".into()),
        ("roles must be a table", format!("roles = 3\n[[steps]]\n{step}")),
        ("description must be a string", format!("description = 3\n[[steps]]\n{step}")),
        ("role a b: a role key may use only letters, digits, - and _", format!("roles.\"a b\" = \"x\"\n[[steps]]\n{step}")),
        ("role build: label must be a string", format!("roles.build = {{ label = 3 }}\n[[steps]]\n{step}")),
        ("step 1 must be a table", "steps = [3]\n".into()),
        ("step 1 (build): prompt must be a string", format!("[[steps]]\n{}", step.replace("\"{path}\"", "3"))),
        // Placeholders that are all known, but that could not be filled in when the step comes up.
        ("step build: prompt cannot be filled in: ValueError: Unknown format code 'd'", format!("[[steps]]\n{}", step.replace("\"{path}\"", "\"{task:d}\""))),
        ("step build: prompt cannot be filled in: ValueError: Unknown conversion specifier z", format!("[[steps]]\n{}", step.replace("\"{path}\"", "\"{task!z}\""))),
        ("step build: again cannot be filled in: KeyError: 'nope'", format!("[[steps]]\n{step}again = \"{{task:{{nope}}}}\"\n")),
    ]
    .into_iter()
    .map(|(m, t)| (m.to_string(), t))
    .collect();
    for name in ["b-{n!s}.md", "b-{n:d}.md", "b-{n:{x}}.md"] {
        cases.push((
            format!("step build: handoff file {name} may use {{n}} only as it is, with no format spec or conversion"),
            format!("[[steps]]\n{}", step.replace("b-{n}.md", name)),
        ));
    }
    for name in ["state.json", "summary.md", "metrics.json", "quality-{n}-1.md", "proofs-{n}", "../b-{n}.md", "/tmp/b-{n}.md", ".."] {
        cases.push((
            format!(
                "step build: handoff file {name} must be a plain file name, and not state.json, summary.md, \
                 metrics.json, quality-*, locked-* or proofs-*, which the run writes itself"
            ),
            format!("[[steps]]\n{}", step.replace("b-{n}.md", name)),
        ));
    }
    for (message, text) in cases {
        let path = config.file("bad", &text);
        let e = load_workflow_file(&path, &built_in()).unwrap_err();
        assert!(e.0.starts_with(&format!("{path}: ")) && e.0.contains(&message), "{message}: {e}");
    }
}

#[test]
fn a_table_with_two_problems_names_the_first_in_the_file_as_python_does() {
    let config = Config::new();
    let step = "id = \"build\"\nfile = \"b-{n}.md\"\nprompt = \"{path}\"\nedits = true\n";
    for (message, text) in [
        ("step 1 (build): unknown key zzz; it takes again,", format!("[[steps]]\n{step}zzz = 1\naaa = 2\n")),
        ("role zz: give a label, or a table with label, model and agent", format!("[roles]\nzz = 3\naa = 4\n[[steps]]\n{step}")),
    ] {
        let path = config.file("bad", &text);
        let e = load_workflow_file(&path, &built_in()).unwrap_err();
        assert!(e.0.contains(message), "{message}: {e}");
    }
}

#[test]
fn a_saved_definition_that_cannot_be_filled_in_or_is_corrupt_is_refused() {
    let Json::Object(mut definition) = workflow_definition(&quick()) else { panic!("an object") };
    let Some(Json::Array(steps)) = definition.get_mut("steps") else { panic!("steps") };
    let Json::Object(first) = &mut steps[0] else { panic!("a step") };
    first.insert("prompt", Json::from("{task:d}"));
    let e = run_pipeline("quick", Some(&Json::Object(definition)), &built_in()).unwrap_err();
    assert!(e.0.starts_with("the definition of workflow quick saved with the run is invalid: workflow quick: step build: prompt cannot be filled in"), "{e}");
    let e = run_pipeline("quick", Some(&Json::parse(r#"{"steps": []}"#).unwrap()), &built_in()).unwrap_err();
    assert_eq!(e.0, "the definition of workflow quick saved with the run is invalid: it has no [[steps]]");
    assert!(run_pipeline("quick", None, &built_in()).unwrap_err().0.starts_with("unknown workflow quick"));
}

#[test]
fn a_file_cannot_replace_a_built_in_workflow_or_take_a_name_no_workflow_can() {
    let config = Config::new();
    let path = config.file("default", "[[steps]]\nuse = \"spec\"\n");
    assert!(load_workflow_file(&path, &built_in()).unwrap_err().0.ends_with("workflow default is built in; give the file another name"));
    let path = config.file("my flow", "[[steps]]\nuse = \"spec\"\n");
    assert!(load_workflow_file(&path, &built_in()).unwrap_err().0.contains("a workflow's name, its file's, may use only letters"));
}

#[test]
fn a_missing_or_undecodable_file() {
    assert_eq!(
        load_workflow_file("/nope/x.toml", &built_in()).unwrap_err().0,
        "cannot read workflow file /nope/x.toml: No such file or directory"
    );
    let config = Config::new();
    let path = config.dir.join("workflows/latin.toml");
    std::fs::write(&path, b"description = \"\xe9\"\n").unwrap();
    assert!(load_workflow_file(&path, &built_in()).unwrap_err().0.starts_with(&format!("{path}: ")));
}

#[test]
fn files_are_offered_by_name() {
    let config = Config::new();
    config.file("product-team", &std::fs::read_to_string(format!("{EXAMPLES}/product-team.toml")).unwrap());
    config.file("broken", "nonsense\n");
    std::fs::write(config.dir.join("workflows/notes.txt"), "not a workflow").unwrap();
    assert_eq!(workflow_names(&config.config).unwrap(), ["broken", "default", "product-team"]);
    assert_eq!(named_workflow(&config.config, "product-team", &built_in()).unwrap().name(), "product-team");
    assert_eq!(named_workflow(&config.config, "default", &built_in()).unwrap(), *DEFAULT_WORKFLOW);
    assert_eq!(
        named_workflow(&config.config, "nope", &built_in()).unwrap_err().0,
        "unknown workflow nope; there are broken, default, product-team"
    );
    let none = ConfigDir::at(&config.dir.join("elsewhere"));
    assert!(workflow_files(&none).unwrap().is_empty());
}

#[test]
fn an_unlistable_workflows_directory() {
    let dir = TempDir::new();
    std::fs::write(dir.join("workflows"), "a file, not a directory").unwrap();
    let config = ConfigDir::at(&dir.join(""));
    assert!(workflow_names(&config).unwrap_err().0.starts_with(&format!("cannot list {}", dir.join("workflows"))));
}

#[test]
fn agents_are_parsed_and_round_tripped() {
    let p = parse_workflow("mine", &toml(
        "roles.review = { agent = \"codex\", model = \"o3\" }\nroles.build = { label = \"Implementer\", agent = \"pi\" }\n\
         [[steps]]\nuse = \"spec\"\n[[steps]]\nuse = \"build\"\n[[steps]]\nuse = \"review\"\n",
    ), &built_in()).unwrap();
    assert_eq!(p.agents(), &roles(&[("review", "codex"), ("build", "pi")]));
    let definition = workflow_definition(&p);
    assert_eq!(
        definition.get("roles").unwrap().dumps(),
        r#"{"spec": {"label": "Spec Collector"}, "build": {"label": "Implementer", "agent": "pi"}, "review": {"label": "Reviewer", "model": "o3", "agent": "codex"}}"#
    );
    let text = workflow_toml(&p);
    assert!(text.contains("[roles.review]\nlabel = \"Reviewer\"\nmodel = \"o3\"\nagent = \"codex\"\n"), "{text}");
    assert_eq!(parse_workflow("mine", &toml(&text), &built_in()).unwrap(), p);
    assert_eq!(parse_workflow("mine", &definition, &built_in()).unwrap(), p);
}

#[test]
fn invalid_agents_name_the_role() {
    let steps = "[[steps]]\nuse = \"spec\"\n[[steps]]\nuse = \"build\"\n[[steps]]\nuse = \"review\"\n";
    assert_eq!(parse_workflow("mine", &toml(steps), &built_in()).unwrap().steps(), DEFAULT_WORKFLOW.steps());
    for (text, message) in [
        ("roles.review = { agent = \"cursor\" }\n", "role review: agent cursor is not a harness this orchestrator supports; it takes claude, codex, gemini, opencode, pi"),
        ("roles.review = { agent = 3 }\n", "role review: agent must be a string"),
        ("roles.review = { agnet = \"pi\" }\n", "role review: unknown key agnet; did you mean agent?"),
    ] {
        let e = parse_workflow("mine", &toml(&format!("{text}{steps}")), &built_in()).unwrap_err();
        assert!(e.contains(message), "{message}: {e}");
    }
}

#[test]
fn locks_round_trip_and_are_checked_in_a_file() {
    let text = workflow_toml(&locking());
    assert!(text.contains("locks = true"));
    assert_eq!(parse_workflow("locking", &toml(&text), &built_in()).unwrap(), locking());
    let step = "id = \"tests\"\nfile = \"t.md\"\nprompt = \"{path}\"\nlocks = true\n";
    let e = parse_workflow("bad", &toml(&format!("[[steps]]\n{step}")), &built_in()).unwrap_err();
    assert!(e.ends_with("step tests locks the files it changes, so it must edit"), "{e}");
    let e = parse_workflow("bad", &toml(&format!("[[steps]]\n{}", step.replace("true", "\"yes\""))), &built_in()).unwrap_err();
    assert!(e.ends_with("locks must be true or false"), "{e}");
}

#[test]
fn proof_command_in_a_file() {
    let p = parse_workflow("proving", &toml(r#"
proof_command = "python -m pytest -q {proofs}"

[[steps]]
use = "spec"

[[steps]]
use = "build"

[[steps]]
use = "review"
prompt = "Back each finding with a test in {proofs_dir}; write {review_path}."
again = "Again, with tests in {proofs_dir}; write {review_path}."
"#), &built_in()).unwrap();
    assert_eq!(p.proof_command(), Some("python -m pytest -q {proofs}"));
    assert!(p.step("review").unwrap().prompt.contains("{proofs_dir}"));
    let proving = replaced(&quick(), |s| s.proof_command = Some("sh {proofs}".into())).unwrap();
    let text = workflow_toml(&proving);
    assert!(text.contains("proof_command = \"sh {proofs}\""));
    assert_eq!(parse_workflow("quick", &toml(&text), &built_in()).unwrap(), proving);
    assert!(!workflow_toml(&quick()).contains("proof_command"));
    let Json::Object(mut definition) = workflow_definition(&quick()) else { panic!("an object") };
    definition.insert("proof_command", Json::from(1));
    assert_eq!(parse_workflow("bad", &Json::Object(definition), &built_in()).unwrap_err(), "proof_command must be a string");
}

#[test]
fn a_workflow_files_own_prompt_is_not_replaced() {
    let mut overrides = Map::new();
    overrides.insert("BUILD_PROMPT", "Build {spec_path} in {cwd}; report to {build_path}.".to_string());
    overrides.insert("FIX_PROMPT", "Fix {prev_review_path}; report to {build_path}.".to_string());
    let prompts = Prompts::from_overrides(&overrides).unwrap();
    let p = load_workflow_file(&format!("{EXAMPLES}/quick.toml"), &prompts).unwrap();
    let build = p.step("build").unwrap();
    // Its own prompt, and the again it takes from the default workflow's build step through use.
    assert!(build.prompt.starts_with("You are the Builder, the first of two roles"), "{}", build.prompt);
    assert_eq!(build.again.as_deref(), Some(prompts.text(Prompt::FixPrompt)));
}

#[test]
fn a_saved_state_names_its_workflow() {
    let saved = |text: &str| Json::parse(text).unwrap().as_object().unwrap().clone();
    assert_eq!(saved_pipeline(&saved(r#"{"run_id": "r"}"#)), Some(DEFAULT_WORKFLOW.clone()));
    assert_eq!(saved_pipeline(&saved(r#"{"workflow": "quick"}"#)), None);
    let definition = workflow_definition(&quick()).dumps();
    let state = saved(&format!(r#"{{"workflow": "quick", "workflow_definition": {definition}, "prompt_overrides": null}}"#));
    assert_eq!(saved_pipeline(&state), Some(quick()));
    assert_eq!(saved_pipeline(&saved(r#"{"prompt_overrides": {"LATER_NOTE": "x"}}"#)), None);
    assert_eq!(saved_pipeline(&saved(r#"{"workflow": 5}"#)), None);
    let worded = saved_pipeline(&saved(r#"{"prompt_overrides": {"FIX_PROMPT": "Fix {prev_review_path}; report to {build_path}."}}"#)).unwrap();
    assert_eq!(worded.step("build").unwrap().again.as_deref(), Some("Fix {prev_review_path}; report to {build_path}."));
}
