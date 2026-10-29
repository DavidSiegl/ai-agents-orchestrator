//! The workflows command, through main as herdrless_command runs it. Python's cases, by class:
//! TestWorkflowFileCLI's test_workflows_lists_them and test_workflows_prints_one_as_a_file,
//! TestWorkflowFileAgents.test_workflows_prints_it, and TestPromptFiles' test_workflows_lists_the_prompt_files,
//! test_workflows_says_why_the_prompt_files_would_stop_a_run and
//! test_workflows_prints_the_default_as_the_prompt_files_word_it.

use crate::cli::fixture::Cli;
use crate::env::ConfigDir;
use crate::pipeline::DEFAULT_WORKFLOW;
use crate::prompts::Prompts;
use crate::test_support::TempDir;
use crate::workflow_file::{load_workflow_file, parse_toml, parse_workflow};

/// The repository's example workflow file of name.
fn example(name: &str) -> String {
    format!("{}/../examples/workflows/{name}.toml", env!("CARGO_MANIFEST_DIR"))
}

/// A Cli whose configuration directory, d, holds files, each a (path in it, text) pair.
fn configured(d: &TempDir, files: &[(&str, &str)]) -> Cli {
    for (path, text) in files {
        let path = d.join(path);
        std::fs::create_dir_all(std::path::Path::new(&path).parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }
    let mut cli = Cli::new();
    cli.config = ConfigDir::at(&d.path());
    cli
}

/// What `workflows` with argv prints, having exited 0 without asking herdr anything.
fn printed(cli: &Cli, argv: &[&str]) -> String {
    let argv: Vec<&str> = ["workflows"].iter().chain(argv).copied().collect();
    assert_eq!(cli.main(&argv), 0, "{argv:?}");
    assert!(cli.f.herdr.state().calls.is_empty());
    cli.take_out()
}

#[test]
fn workflows_lists_them() {
    let d = TempDir::new();
    let team = std::fs::read_to_string(example("product-team")).unwrap();
    let cli = configured(&d, &[("workflows/product-team.toml", &team), ("workflows/broken.toml", "[[steps]]\nid = 3\n")]);
    let workflows = cli.config.workflows_dir();
    assert_eq!(printed(&cli, &[]).lines().collect::<Vec<_>>(), [
        "default  built in".to_string(),
        "    spec -> build -> review (back to build)".into(),
        format!("broken  {workflows}/broken.toml"),
        "    error: step 1: file is missing".into(),
        format!("product-team  {workflows}/product-team.toml"),
        "    spec -> design -> tests -> build -> review (back to build)".into(),
        "    Product Manager -> Architect -> QA Engineer -> Engineer <-> Tech Lead".into(),
        String::new(),
        format!(
            "Add one as {workflows}/NAME.toml; `orchestrator.py workflows default` prints the default as a file to \
             start from."
        ),
    ]);
}

#[test]
fn workflows_prints_one_as_a_file() {
    let d = TempDir::new();
    let team = std::fs::read_to_string(example("product-team")).unwrap();
    let cli = configured(&d, &[("workflows/product-team.toml", &team)]);
    let expected = load_workflow_file(&example("product-team"), &Prompts::default()).unwrap();
    for reference in ["product-team".to_string(), example("product-team")] {
        let text = printed(&cli, &[&reference]);
        let printed = parse_workflow("product-team", &parse_toml(&text).unwrap(), &Prompts::default()).unwrap();
        assert_eq!(printed, expected, "{reference}");
    }
}

#[test]
fn workflows_prints_a_files_agent() {
    let d = TempDir::new();
    let coded = "roles.review = { agent = \"codex\" }\n[[steps]]\nuse = \"spec\"\n[[steps]]\nuse = \"build\"\n\
                 [[steps]]\nuse = \"review\"\n";
    let cli = configured(&d, &[("workflows/coded.toml", coded)]);
    let text = printed(&cli, &["coded"]);
    assert!(text.contains("[roles.review]\nlabel = \"Reviewer\"\nagent = \"codex\"\n"), "{text}");
}

#[test]
fn an_unknown_or_broken_workflow_is_an_error() {
    let d = TempDir::new();
    let cli = configured(&d, &[("workflows/broken.toml", "[[steps]]\nid = 3\n")]);
    assert_eq!(cli.error(&["workflows", "nosuch"]), "unknown workflow nosuch; there are broken, default");
    let broken = cli.config.workflows_dir() + "/broken.toml";
    assert_eq!(cli.error(&["workflows", &broken]), format!("{broken}: step 1: file is missing"));
    assert_eq!(cli.main(&["workflows", "nosuch"]), crate::constants::EXIT_ERROR);
    assert_eq!(cli.take_out(), "");
}

#[test]
fn workflows_lists_the_prompt_files() {
    let d = TempDir::new();
    let cli = configured(&d, &[
        ("prompts/QUALITY_OFF_NOTE.md", "Off: {quality_paths}.\n"),
        ("prompts/BUILD_PROMPT.md", "Build {spec_path} in {cwd}; report to {build_path}.\n"),
    ]);
    let prompts = cli.config.prompts_dir();
    let out = printed(&cli, &[]);
    let expected = format!(
        "\nPrompt files in {prompts}, in place of the built-in texts:\n    BUILD_PROMPT  {prompts}/BUILD_PROMPT.md\n    \
         QUALITY_OFF_NOTE  {prompts}/QUALITY_OFF_NOTE.md\n"
    );
    assert!(out.ends_with(&expected), "{out}");
}

#[test]
fn workflows_says_why_the_prompt_files_would_stop_a_run() {
    let d = TempDir::new();
    let cli = configured(&d, &[("prompts/BUILD_PROMPT.md", "Build.\n")]);
    let prompts = cli.config.prompts_dir();
    let out = printed(&cli, &[]);
    let expected = format!(
        "\nPrompt files in {prompts}:\n    error: {prompts}/BUILD_PROMPT.md: placeholder {{build_path}} is missing; \
         BUILD_PROMPT takes {{build_path}}, {{cwd}}, {{spec_path}}\n"
    );
    assert!(out.ends_with(&expected), "{out}");
    // Without prompt files, the list ends at how to add a workflow.
    let cli = configured(&TempDir::new(), &[]);
    assert!(printed(&cli, &[]).ends_with("prints the default as a file to start from.\n"));
}

#[test]
fn workflows_prints_the_default_as_the_prompt_files_word_it() {
    let d = TempDir::new();
    let build = "Build {spec_path} in {cwd}; report to {build_path}.";
    let cli = configured(&d, &[("prompts/BUILD_PROMPT.md", &format!("{build}\n"))]);
    let text = printed(&cli, &["default"]);
    let default = parse_workflow("default", &parse_toml(&text).unwrap(), &Prompts::default()).unwrap();
    assert_eq!(default.step("build").unwrap().prompt, build);
    assert_ne!(default, *DEFAULT_WORKFLOW);
}
