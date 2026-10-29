//! tests/test_orchestrator.py's TestRunHealth (its print_runs cases are crate::cli::listing's tests) and TestPidAlive.

use super::*;
use crate::constants::APPROVE;

const ME_PID: i64 = 4242;

/// Python's run_record: a run of 20260930-070000-c0ffee in round 1 of phase, with owner, error and verdict.
fn run_record(phase: &str, owner: Option<Json>, error: Option<&str>, verdict: Option<&str>) -> Json {
    let text = |s: Option<&str>| s.map_or(Json::Null, Json::from);
    Json::Object(
        [
            ("run_id", Json::from("20260930-070000-c0ffee")),
            ("task", Json::from("add a rate limiter")),
            ("phase", Json::from(phase)),
            ("round", Json::from(1)),
            ("owner", owner.unwrap_or(Json::Null)),
            ("error", text(error)),
            ("verdict", text(verdict)),
        ]
        .into_iter()
        .collect(),
    )
}

/// ME, the owner on host "here", or on another host.
fn owner(host: &str) -> Json {
    Json::Object(
        [
            ("host", Json::from(host)),
            ("pid", Json::from(ME_PID)),
            ("started_at", Json::from("2026-09-30T07:00:00+00:00")),
        ]
        .into_iter()
        .collect(),
    )
}

fn health(record: &Json, age: u64, alive: bool) -> Option<(&'static str, String)> {
    run_health(record, age, "here", &|_| alive)
}

#[test]
fn fresh_heartbeat_is_running() {
    let got = health(&run_record("build", Some(owner("here")), None, None), 40, true);
    assert_eq!(got, Some((RUNNING, "pid 4242 on here, beat 40s ago".to_string())));
}

#[test]
fn old_heartbeat_is_stale() {
    let got = health(&run_record("build", Some(owner("here")), None, None), STALE_SECONDS + 420, true);
    assert_eq!(got, Some((STALE, "no heartbeat for 12m".to_string())));
}

#[test]
fn dead_local_pid_is_stale_once_a_beat_is_late() {
    let record = run_record("build", Some(owner("here")), None, None);
    assert_eq!(health(&record, HEARTBEAT_SECONDS + 1, false).unwrap(), (STALE, "orchestrator pid 4242 exited".into()));
    assert_eq!(health(&record, 10, false).unwrap().0, RUNNING);
}

#[test]
fn pid_on_another_host_is_not_checked() {
    assert_eq!(health(&run_record("build", Some(owner("laptop")), None, None), 120, false).unwrap().0, RUNNING);
}

#[test]
fn run_from_before_owners_existed() {
    let got = health(&run_record("spec", None, None, None), 7 * 3600 + 720, true);
    assert_eq!(got, Some((STALE, "no heartbeat for 7h12m".to_string())));
}

#[test]
fn finished_and_failed_runs_are_never_stale() {
    assert_eq!(health(&run_record("done", None, None, Some(APPROVE)), 1_000_000, true), None);
    assert_eq!(health(&run_record("build", None, Some("interrupted"), None), 1_000_000, true), None);
}

#[test]
fn own_process_is_alive() {
    assert!(pid_alive(std::process::id() as i128));
}

#[test]
fn exited_process_is_not() {
    // A child that has exited and been waited for has no process left; its pid is free until reused.
    let mut child = std::process::Command::new("true").spawn().expect("true starts");
    let pid = child.id();
    child.wait().expect("true ends");
    assert!(!pid_alive(pid as i128));
    assert!(!pid_alive(i128::from(i32::MAX) + 1));
}

#[test]
fn an_id_of_0_or_below_names_a_group() {
    // This process's own group holds this process; -1 leaves this process and init out, and holds the test runner
    // that started it.
    assert!(pid_alive(0));
    assert!(pid_alive(-1));
}

#[test]
fn another_users_process_is_alive() {
    // pid 1 is root's init: kill answers EPERM to any other user, and succeeds for root.
    assert!(pid_alive(1));
}

#[test]
fn local_host_is_the_systems_name() {
    let name = std::fs::read_to_string("/proc/sys/kernel/hostname").map(|n| n.trim().to_string());
    if let Ok(name) = name {
        assert_eq!(local_host(), name);
    }
    assert!(!local_host().is_empty());
}
