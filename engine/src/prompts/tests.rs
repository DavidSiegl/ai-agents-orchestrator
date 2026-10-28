use std::os::unix::fs::PermissionsExt;

use super::*;
use crate::py::json::Json;
use crate::test_support::{oracle, TempDir};

#[test]
fn every_built_in_text_is_the_python_engines() {
    let texts = oracle("texts.json");
    let python = texts.get("prompts").and_then(Json::as_object).unwrap();
    let names: Vec<&str> = Prompt::ALL.iter().map(|p| p.name()).collect();
    assert_eq!(names, python.keys().collect::<Vec<_>>());
    for p in Prompt::ALL {
        assert_eq!(Some(p.built_in()), python.get(p.name()).and_then(Json::as_str), "{}", p.name());
    }
}

#[test]
fn every_built_in_prompt_is_overridable() {
    for p in Prompt::ALL {
        assert_eq!(Prompt::named(p.name()), Some(*p));
        assert_eq!(Prompts::default().text(*p), p.built_in());
        assert_eq!(prompt_problem(p.name(), p.built_in()), None, "{}", p.name());
    }
}

/// A directory of prompt files, each written as an editor would, with a newline at its end.
fn prompt_dir(files: &[(&str, &[u8])]) -> TempDir {
    let dir = TempDir::new();
    for (name, text) in files {
        let mut bytes = text.to_vec();
        bytes.push(b'\n');
        std::fs::write(dir.join(name), bytes).unwrap();
    }
    dir
}

#[test]
fn bad_files_are_refused_naming_them() {
    let cases: [(&str, &[u8], &str); 8] = [
        ("BUILDER_PROMPT.md", b"Build {spec_path}.", "BUILDER_PROMPT names no built-in prompt; did you mean BUILD_PROMPT.md?"),
        ("README.md", b"notes", "README names no built-in prompt; the names are BLOCKED_NOTE, BLOCKED_PROMPT, "),
        (
            "BUILD_PROMPT.md",
            b"Build {spec_path} in {cwd} {nope}; report to {build_path}.",
            "placeholder {nope} is not one of the built-in text's; BUILD_PROMPT takes {build_path}, {cwd}, {spec_path}",
        ),
        (
            "BUILD_PROMPT.md",
            b"Build {spec_path} in {cwd}.",
            "placeholder {build_path} is missing; BUILD_PROMPT takes {build_path}, {cwd}, {spec_path}",
        ),
        ("BLOCKED_NOTE.md", b"Ask {}.", "placeholder {} is not one of the built-in text's; BLOCKED_NOTE takes none"),
        ("CONTINUE_PROMPT.md", b"Write {path!z}.", "cannot be filled in: ValueError: Unknown conversion specifier z"),
        (
            "CONTINUE_PROMPT.md",
            b"Write {path}}.",
            "Single '}' encountered in format string; write a literal brace as {{ or }}",
        ),
        ("FIX_PROMPT.md", b"Fix \xe9", "not UTF-8 text"),
    ];
    for (name, text, message) in cases {
        let dir = prompt_dir(&[(name, text)]);
        let e = load_prompts_from(dir.join("").trim_end_matches('/')).unwrap_err();
        let path = dir.join(name);
        assert!(e.0.starts_with(&format!("{path}: {message}")), "{name}: {e}");
    }
}

#[test]
fn an_unreadable_file_names_it() {
    let dir = TempDir::new();
    let path = dir.join("FIX_PROMPT.md");
    std::fs::create_dir(&path).unwrap();
    let e = load_prompts_from(dir.join("").trim_end_matches('/')).unwrap_err();
    assert_eq!(e.0, format!("cannot read prompt file {path}: Is a directory"));
}

#[test]
fn an_unlistable_directory_names_it() {
    let dir = TempDir::new();
    let prompts = dir.join("prompts");
    std::fs::create_dir(&prompts).unwrap();
    std::fs::set_permissions(&prompts, std::fs::Permissions::from_mode(0o000)).unwrap();
    let listed = load_prompts_from(&prompts);
    std::fs::set_permissions(&prompts, std::fs::Permissions::from_mode(0o700)).unwrap();
    // root lists any directory; anyone else is refused.
    if let Err(e) = listed {
        assert_eq!(e.0, format!("cannot list {prompts}: Permission denied"));
    }
}

#[test]
fn without_prompt_files_nothing_changes() {
    let dir = prompt_dir(&[("notes.txt", b"not a prompt file")]);
    assert_eq!(load_prompts_from(dir.join("").trim_end_matches('/')).unwrap(), Prompts::default());
    assert_eq!(load_prompts_from(&dir.join("missing")).unwrap(), Prompts::default());
    assert!(prompts_dir().ends_with("/ai-agents-orchestrator/prompts"));
}

#[test]
fn an_override_ends_with_one_newline_less_and_its_line_ends_read_as_newlines() {
    let dir = prompt_dir(&[("BLOCKED_NOTE.md", b"Ask.\n"), ("CONTINUE_PROMPT.md", b"Go on\r\nto\r{path}.\r")]);
    let prompts = load_prompts_from(dir.join("").trim_end_matches('/')).unwrap();
    assert_eq!(prompts.text(Prompt::BlockedNote), "Ask.\n");
    // "\r\n" with the editor's newline is the one trailing newline left out.
    assert_eq!(prompts.text(Prompt::ContinuePrompt), "Go on\nto\n{path}.");
    let overrides = prompts.overrides();
    assert_eq!(overrides.keys().collect::<Vec<_>>(), ["CONTINUE_PROMPT", "BLOCKED_NOTE"]);
    assert_eq!(Prompts::from_overrides(&overrides).unwrap(), prompts);
}

#[test]
fn a_state_that_replaces_an_unknown_prompt() {
    let overrides: Map<String> = [("LATER_NOTE", "x".to_string()), ("ANOTHER", "y".to_string())].into_iter().collect();
    assert_eq!(
        saved_prompts(&overrides, "20260929-120000-a1b2c3").unwrap_err().0,
        "run state 20260929-120000-a1b2c3 replaces prompt ANOTHER, which this orchestrator lacks"
    );
    assert_eq!(saved_prompts(&Map::new(), "r").unwrap(), Prompts::default());
}

#[test]
fn a_problem_names_no_prompt_that_does_not_exist() {
    assert_eq!(prompt_problem("NO_SUCH_PROMPT", "x"), None);
}
