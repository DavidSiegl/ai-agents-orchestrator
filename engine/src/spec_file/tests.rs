//! read_spec_file and SpecFile::task as TestSpecCLI's cases see them through main; the CLI half of each, and the
//! class's other cases, are main's (crate::cli's tests and crate::cli::args').

use super::*;
use crate::test_support::TempDir;

/// CRLF and trailing blanks, which the run's copy keeps: Python's GIVEN_SPEC.
const GIVEN_SPEC: &str = "# Add a token-bucket rate limiter\r\n\r\n## Goal\r\nlimit requests  \n\n";

fn file(dir: &TempDir, name: &str, bytes: &[u8]) -> String {
    let path = dir.join(name);
    std::fs::write(&path, bytes).expect("a temporary file");
    path
}

#[test]
fn the_spec_is_read_with_its_line_ends() {
    // test_the_spec_reaches_the_workflow, its SpecFile and task.
    let dir = TempDir::new();
    let path = file(&dir, "spec.md", GIVEN_SPEC.as_bytes());
    let spec = read_spec_file(&path).unwrap();
    assert_eq!(spec, SpecFile { path: path.clone(), text: GIVEN_SPEC.into() });
    assert_eq!(spec.task(), "Add a token-bucket rate limiter");
}

#[test]
fn an_untitled_spec_names_the_task_after_its_file() {
    let dir = TempDir::new();
    let spec = read_spec_file(&file(&dir, "rate-limiter.v2.md", b"Limit the requests.\n")).unwrap();
    assert_eq!(spec.task(), "rate-limiter.v2");
}

#[test]
fn an_unreadable_spec_is_an_error() {
    let dir = TempDir::new();
    let missing = dir.join("nosuch.md");
    let error = |path: &str| read_spec_file(path).unwrap_err().0;
    assert_eq!(error(&missing), format!("cannot read spec file {missing}: No such file or directory"));
    assert_eq!(error(&dir.path()), format!("cannot read spec file {}: Is a directory", dir.path()));
    let binary = file(&dir, "spec.bin", b"\xff\xfe# spec");
    assert_eq!(
        error(&binary),
        format!("spec file {binary} is not UTF-8 text: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte")
    );
}

#[test]
fn an_empty_spec_is_an_error() {
    let dir = TempDir::new();
    for text in ["", " \n\t\r\n"] {
        let path = file(&dir, "blank.md", text.as_bytes());
        assert_eq!(read_spec_file(&path).unwrap_err().0, format!("spec file {path} is empty"));
    }
}
