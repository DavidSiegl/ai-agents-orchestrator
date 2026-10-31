//! `cargo xtask bundles`: each task's bundle is of its toy repository, and a bundle's commit is decided by its files.

use std::fs;
use std::process::Command;

use xtask::bundles::{bundle_commit, make_bundle, stale_bundles};
use xtask::toy_repos::{ToyRepo, REFACTOR_LIB, TOY_REPOS};
use xtask::TempDir;

#[test]
fn the_bundles_are_of_the_toy_repositories() {
    let tasks = xtask::eval::tasks_dir(&xtask::checkout());
    assert_eq!(stale_bundles(TOY_REPOS, &tasks), Ok(vec![]), "run `cargo xtask bundles`");
}

#[test]
fn a_changed_toy_repository_is_caught_by_the_check() {
    let tasks = xtask::eval::tasks_dir(&xtask::checkout());
    let changed = [ToyRepo { task: "feature", files: &[("README.md", "changed\n")] }];
    assert_eq!(stale_bundles(&changed, &tasks), Ok(vec![tasks.join("feature/repo.bundle")]));
    let missing = [ToyRepo { task: "nope", files: &[("README.md", "nope\n")] }];
    assert_eq!(stale_bundles(&missing, &tasks), Ok(vec![tasks.join("nope/repo.bundle")]));
}

#[test]
fn a_bundle_holds_one_commit_on_main_whose_id_its_files_decide() {
    let dir = TempDir::new("xtask-bundles-test-").expect("a temporary directory");
    let files = [("a.txt", "a\n"), ("dir/b.txt", "b\n")];
    let (first, second) = (dir.path().join("1.bundle"), dir.path().join("2.bundle"));
    let commit = make_bundle(&files, &first, "m").expect("made");
    assert_eq!(make_bundle(&files, &second, "m"), Ok(commit.clone()));
    assert_eq!(bundle_commit(&first), Ok(commit.clone()));
    assert_ne!(make_bundle(&[("a.txt", "A\n"), ("dir/b.txt", "b\n")], &second, "m"), Ok(commit.clone()));
    assert_ne!(make_bundle(&files, &second, "another message"), Ok(commit.clone()));
    let clone = dir.path().join("clone");
    assert!(Command::new("git")
        .arg("clone")
        .arg("--quiet")
        .arg(&first)
        .arg(&clone)
        .status()
        .expect("git runs")
        .success());
    let mut entries: Vec<String> =
        fs::read_dir(&clone).expect("lists").map(|e| e.expect("entry").file_name().to_string_lossy().into()).collect();
    entries.sort();
    assert_eq!(entries, [".git", "a.txt", "dir"]);
    let log = Command::new("git").arg("-C").arg(&clone).args(["log", "--format=%H %D"]).output().expect("git runs");
    assert_eq!(String::from_utf8_lossy(&log.stdout), format!("{commit} HEAD -> main, origin/main, origin/HEAD\n"));
}

#[test]
fn the_check_holds_whatever_git_repository_the_environment_names() {
    let dir = TempDir::new("xtask-bundles-test-").expect("a temporary directory");
    let checked =
        Command::new(env!("CARGO_BIN_EXE_xtask")).args(["bundles", "--check"]).env("GIT_DIR", dir.path()).status();
    assert!(checked.expect("xtask runs").success(), "GIT_DIR misled the check");
}

#[test]
fn not_a_bundle_is_an_error() {
    let dir = TempDir::new("xtask-bundles-test-").expect("a temporary directory");
    let path = dir.path().join("x.bundle");
    fs::write(&path, "no bundle\n").expect("written");
    assert!(bundle_commit(&path).is_err());
}

/// text without its whitespace, so that a copy of code compares equal however it was indented or wrapped.
fn squeezed(text: &str) -> String {
    text.chars().filter(|c| !c.is_whitespace()).collect()
}

#[test]
fn the_refactor_tasks_hidden_test_holds_its_starting_code() {
    let path = xtask::eval::tasks_dir(&xtask::checkout()).join("refactor/hidden_test.rs");
    let hidden = fs::read_to_string(&path).expect("the hidden test reads");
    let original = format!("mod original {{{REFACTOR_LIB}}}");
    assert!(squeezed(&hidden).contains(&squeezed(&original)), "{} holds another src/lib.rs", path.display());
}
