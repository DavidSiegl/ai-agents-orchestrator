//! The xtask binary as `cargo xtask` runs it: its commands' usage errors, the bundles it writes, and a Ctrl-C during
//! an eval.

use std::io::{BufRead, BufReader, Read};
use std::process::{Command, Stdio};

use nix::sys::signal::{kill, Signal};
use nix::unistd::Pid;
use xtask::bundles::{stale_bundles, write_bundles};
use xtask::toy_repos::TOY_REPOS;
use xtask::TempDir;

fn xtask(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_xtask")).args(args).output().expect("xtask runs")
}

#[test]
fn usage_errors_exit_2() {
    for args in [&[][..], &["nope"], &["bundles", "--nope"], &["eval", "--repeat", "0"]] {
        let output = xtask(args);
        assert_eq!(output.status.code(), Some(2), "{args:?}");
        assert!(String::from_utf8_lossy(&output.stderr).contains("usage: cargo xtask"), "{args:?}");
    }
}

#[test]
fn written_bundles_pass_the_check() {
    let dir = TempDir::new("xtask-cli-test-").expect("a temporary directory");
    let written = write_bundles(&TOY_REPOS[..2], dir.path()).expect("the bundles are written");
    let paths: Vec<_> = written.iter().map(|(path, _)| path.clone()).collect();
    assert_eq!(paths, [dir.path().join("bugfix/repo.bundle"), dir.path().join("docs/repo.bundle")]);
    assert_eq!(stale_bundles(&TOY_REPOS[..2], dir.path()), Ok(vec![]));
    assert_eq!(stale_bundles(&TOY_REPOS[..3], dir.path()), Ok(vec![dir.path().join("feature/repo.bundle")]));
}

#[test]
fn a_ctrl_c_waits_for_the_engine_keeps_the_clone_and_exits_130() {
    // An engine that says it started, on the runner's standard error, and outlives the Ctrl-C, which reaches only the
    // runner here, not a terminal's whole group.
    let mut child = Command::new(env!("CARGO_BIN_EXE_xtask"))
        .args(["eval", "--tasks", "feature"])
        .env("EVAL_ENGINE", "sh -c 'echo engine started >&2; sleep 2'")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("xtask runs");
    let mut stderr = BufReader::new(child.stderr.take().expect("its stderr is piped"));
    let mut seen = String::new();
    while !seen.contains("engine started") {
        let mut line = String::new();
        assert_ne!(stderr.read_line(&mut line).expect("stderr reads"), 0, "xtask ended first: {seen}");
        seen.push_str(&line);
    }
    kill(Pid::from_raw(i32::try_from(child.id()).expect("a pid")), Signal::SIGINT).expect("SIGINT is sent");
    stderr.read_to_string(&mut seen).expect("stderr reads");
    let output = child.wait_with_output().expect("xtask is waited for");
    let kept = seen.lines().find_map(|line| line.strip_prefix("interrupted: the run's clone stays in "));
    let repo = kept.map(std::path::Path::new);
    let exists = repo.is_some_and(|repo| repo.join(".git").is_dir());
    if let Some(work) = repo.and_then(|repo| repo.parent()) {
        std::fs::remove_dir_all(work).expect("the kept directory is removed");
    }
    assert_eq!(output.status.code(), Some(130), "{seen}");
    assert!(exists, "{seen}");
    assert!(output.stdout.is_empty(), "no trial ended, so no table: {}", String::from_utf8_lossy(&output.stdout));
}
