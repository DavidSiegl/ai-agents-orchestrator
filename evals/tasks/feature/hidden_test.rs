//! The hidden acceptance test of the eval task `feature`: `top_words` and `tally --top N` (task.md). `cargo xtask
//! eval` copies it into the clone's tests/ after the run and runs it with the crate's own tests (xtask/src/eval.rs).

use std::io::Write;
use std::path::Path;
use std::process::{Command, Output, Stdio};

use tally::top_words;

const TEXT: &str = "the cat and the hat and THE bat\n";

/// `tally` run with args, TEXT on its standard input.
fn tally(args: &[&str]) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_tally"))
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("tally starts");
    // A tally that refuses its arguments may exit before it reads, closing the pipe.
    let _ = child.stdin.take().expect("its stdin is piped").write_all(TEXT.as_bytes());
    child.wait_with_output().expect("tally is waited for")
}

fn counts(pairs: &[(&str, usize)]) -> Vec<(String, usize)> {
    pairs.iter().map(|&(word, n)| (word.to_string(), n)).collect()
}

#[test]
fn top_words_orders_by_count_then_alphabetically() {
    assert_eq!(top_words(TEXT, 2), counts(&[("the", 3), ("and", 2)]));
    assert_eq!(top_words(TEXT, 3), counts(&[("the", 3), ("and", 2), ("bat", 1)]));
    assert_eq!(top_words(TEXT, 99), counts(&[("the", 3), ("and", 2), ("bat", 1), ("cat", 1), ("hat", 1)]));
    assert_eq!(top_words(TEXT, 0), counts(&[]));
    assert_eq!(top_words("", 3), counts(&[]));
}

#[test]
fn the_command_line_takes_top() {
    let output = tally(&["--top", "2"]);
    assert_eq!(output.status.code(), Some(0), "{}", String::from_utf8_lossy(&output.stderr));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "3 the\n2 and\n");
}

#[test]
fn without_top_nothing_changed() {
    let output = tally(&[]);
    assert_eq!(output.status.code(), Some(0), "{}", String::from_utf8_lossy(&output.stderr));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "3 the\n2 and\n1 bat\n1 cat\n1 hat\n");
}

#[test]
fn a_bad_top_is_a_usage_error() {
    for bad in ["0", "-2", "x"] {
        let output = tally(&["--top", bad]);
        assert_eq!(output.status.code(), Some(2), "--top {bad}: {}", String::from_utf8_lossy(&output.stdout));
        assert!(!String::from_utf8_lossy(&output.stderr).trim().is_empty(), "--top {bad} says nothing on stderr");
    }
}

#[test]
fn a_test_names_top_words() {
    let tests = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests");
    let named = std::fs::read_dir(&tests)
        .expect("tests/ lists")
        .map(|entry| entry.expect("an entry of tests/ reads").path())
        .filter(|path| path.file_name().is_some_and(|name| name != "hidden_test.rs"))
        .any(|path| std::fs::read_to_string(path).is_ok_and(|text| text.contains("top_words")));
    assert!(named, "no test in tests/ names top_words");
}
