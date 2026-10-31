//! The hidden acceptance test of the eval task `docs`: kvstore's README usage and doc comments, its code unchanged
//! (task.md). `cargo xtask eval` copies it into the clone's tests/ after the run and runs it with the crate's own tests
//! (xtask/src/eval.rs).

use std::path::Path;
use std::process::Command;

const SOURCES: [&str; 2] = ["src/lib.rs", "src/main.rs"];
const COMMANDS: [&str; 4] = ["get", "set", "delete", "list"];

fn read(path: &str) -> String {
    std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join(path)).unwrap_or_else(|e| panic!("{path}: {e}"))
}

/// The text of the file at path, relative to the crate, as the task found it: origin/main's, which a run without a
/// pull request never moves.
fn original(path: &str) -> String {
    let output = Command::new("git")
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .args(["show", &format!("origin/main:{path}")])
        .output()
        .expect("git runs");
    assert!(output.status.success(), "git show: {}", String::from_utf8_lossy(&output.stderr));
    String::from_utf8(output.stdout).expect("the file is UTF-8")
}

/// source's code: each line with its comment, from `//` on, and its trailing whitespace removed, blank lines left
/// out. kvstore's code has no `//` inside a string.
fn code(source: &str) -> Vec<String> {
    source
        .lines()
        .map(|line| line.split("//").next().unwrap_or("").trim_end().to_string())
        .filter(|line| !line.trim().is_empty())
        .collect()
}

/// The lines of README.md's `## Usage` section, after its heading, up to the next heading of level 1 or 2 outside a
/// fenced block; empty without one. Each line is paired with whether it is inside a fenced block (CommonMark: a run
/// of three or more backticks or tildes, indented by up to three spaces, closed by a run of the same character at
/// least as long with nothing after it).
fn usage() -> Vec<(bool, String)> {
    let mut section = vec![];
    let mut inside = false;
    let mut fence: Option<(char, usize)> = None;
    for line in read("README.md").lines() {
        let indent = line.len() - line.trim_start_matches(' ').len();
        let rest = line.trim_start_matches(' ');
        let marker = rest.chars().next().filter(|c| matches!(c, '`' | '~'));
        let run = marker.map_or(0, |c| rest.chars().take_while(|&x| x == c).count());
        let fenced_before = fence.is_some();
        match (fence, marker) {
            (None, Some(c)) if indent <= 3 && run >= 3 => fence = Some((c, run)),
            (Some((c, n)), Some(m)) if m == c && indent <= 3 && run >= n && rest[run..].trim().is_empty() => {
                fence = None
            }
            _ => {}
        }
        let heading = !fenced_before && indent <= 3 && (rest.starts_with("# ") || rest.starts_with("## "));
        if heading && inside {
            break;
        }
        if inside {
            section.push((fenced_before && fence.is_some(), line.to_string()));
        }
        if heading && rest.trim_end() == "## Usage" {
            inside = true;
        }
    }
    section
}

#[test]
fn code_unchanged() {
    for path in SOURCES {
        assert_eq!(code(&read(path)), code(&original(path)), "{path} changed beyond its comments");
    }
}

#[test]
fn every_function_has_a_doc_comment() {
    assert!(
        read("src/main.rs").lines().any(|line| line.trim_start().starts_with("//!")),
        "src/main.rs has no //! comment"
    );
    for path in SOURCES {
        let source = read(path);
        let lines: Vec<&str> = source.lines().map(str::trim_start).collect();
        for (i, line) in lines.iter().enumerate() {
            let Some(name) = line.strip_prefix("pub fn ").or_else(|| line.strip_prefix("fn ")) else { continue };
            let before = lines[..i].iter().rev().find(|line| !line.starts_with("#["));
            let name = name.split(['(', '<']).next().unwrap_or(name);
            assert!(before.is_some_and(|line| line.starts_with("///")), "{path}: fn {name} has no doc comment");
        }
    }
}

#[test]
fn usage_shows_each_command() {
    let section = usage();
    assert!(!section.is_empty(), "README.md has no ## Usage section");
    let examples: Vec<Vec<&str>> = section
        .iter()
        .filter(|(fenced, _)| *fenced)
        .map(|(_, line)| line.split_whitespace().collect::<Vec<_>>())
        .collect();
    for command in COMMANDS {
        let shown = examples.iter().any(|words| {
            let start = words.iter().position(|w| *w == "kvstore" || *w == "run");
            start.is_some_and(|i| words[i + 1..].contains(&command))
        });
        assert!(shown, "no example in a fenced block of ## Usage runs `kvstore {command}`");
    }
    let text: String = section.iter().map(|(_, line)| format!("{line}\n")).collect();
    assert!(text.contains("--file"), "## Usage does not name --file");
    assert!(text.contains("store.txt"), "## Usage does not name --file's default, store.txt");
}

#[test]
fn usage_says_a_missing_key_exits_1() {
    let text: String = usage().iter().map(|(_, line)| format!("{line}\n")).collect::<String>().to_lowercase();
    let said = text.split(". ").any(|sentence| {
        let start = sentence.find("exit").or_else(|| sentence.find("status"));
        start.is_some_and(|i| sentence[i..].split(|c: char| !c.is_ascii_alphanumeric()).any(|word| word == "1"))
    });
    assert!(said, "## Usage does not say that a missing key exits with status 1");
}
