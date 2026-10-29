//! TestOneRunPerCheckout: a run refuses to go on while another run in its checkout is live, since both would edit one
//! tree. Its worktree cases are ported with worktree runs, and its resume --force cases with resumable_state (issue
//! rust-engine/08's Plan, parts 3 and 7).

use super::*;
use crate::constants::{HEARTBEAT_SECONDS, STALE_SECONDS};
use crate::runs::local_host;

const OTHER_ID: &str = "20261009-220151-537935";

fn other_dir() -> String {
    format!("/proj/.orchestrator/runs/{OTHER_ID}")
}

/// A FakeHost whose /proj holds run OTHER_ID, which this host's orchestrator pid 3354482 last saved age seconds ago,
/// its state.json's fields overridden by those of saved.
fn host_with_other_run(age: u64, saved: &[(&str, Json)]) -> Rc<FakeHost> {
    let host = FakeHost::default();
    let mut other = RunState::new(OTHER_ID, "another task", "/proj");
    other.phase = "build".into();
    other.round = 1;
    let Json::Object(mut state) = other.to_json() else { unreachable!() };
    let owner = [
        ("host", Json::from(local_host())),
        ("pid", Json::from(3_354_482)),
        ("started_at", Json::from("2026-10-09T22:01:51+00:00")),
    ];
    state.insert("owner", Json::Object(owner.into_iter().collect()));
    for (key, value) in saved {
        state.insert(*key, value.clone());
    }
    host.state().files.insert(format!("{}/state.json", other_dir()), Json::Object(state).dumps());
    host.state().ages.insert(OTHER_ID.into(), age);
    Rc::new(host)
}

/// The run fails naming the other run, records that in its own state.json, and starts no agent.
fn assert_refused(f: &Fixture, settings: RunSettings, beat: &str) {
    let other_path = format!("{}/state.json", other_dir());
    let other = f.host.state().files.get(&other_path).cloned();
    let message = failure(f.workflow(settings).run());
    let expected = format!(
        "run {OTHER_ID} is running in /proj (pid 3354482 on {}, beat {beat} ago); one run at a time per checkout: \
         wait for it, stop it, or use another clone",
        local_host()
    );
    assert_eq!(message, expected);
    let saved = f.saved();
    assert_eq!((text(&saved, "error"), saved.get("owner")), (Some(message), Some(&Json::Null)));
    assert!(f.herdr.state().calls.is_empty());
    assert_eq!(f.host.state().files.get(&other_path).cloned(), other);
}

#[test]
fn new_run_is_refused_while_another_runs() {
    for pull_request in [true, false] {
        let f = Fixture::on(host_with_other_run(16, &[]), new_state(), approved());
        assert_refused(&f, RunSettings { pull_request, ..RunSettings::default() }, "16s");
    }
}

#[test]
fn resume_is_refused_while_another_runs() {
    for pull_request in [true, false] {
        let mut state = saved_run("review", 2, &["spec", "build", "review"]);
        state.pull_request = pull_request;
        state.base_branch = Some("main".into());
        state.branch = Some("orchestrator/x-a1b2c3".into());
        let script = vec![("review", vec![review_turn(2, APPROVE)])];
        let f = Fixture::resume(host_with_other_run(16, &[]), state, script, &[], &[]);
        assert_refused(&f, RunSettings { pull_request, ..RunSettings::default() }, "16s");
    }
}

#[test]
fn runs_nothing_drives_do_not_block() {
    let done = [("phase", Json::from("done")), ("verdict", Json::from(APPROVE)), ("owner", Json::Null)];
    let failed = [("error", Json::from("interrupted")), ("owner", Json::Null)];
    // (case, age, the saved state's fields, whether its pid is alive)
    type Case<'a> = (&'a str, u64, &'a [(&'a str, Json)], bool);
    let cases: [Case; 4] = [
        ("stale", STALE_SECONDS + 1, &[], true),
        ("dead pid on this host", HEARTBEAT_SECONDS + 1, &[], false),
        ("done", 0, &done, true),
        ("failed", 0, &failed, true),
    ];
    for (name, age, saved, alive) in cases {
        let mut f = Fixture::on(host_with_other_run(age, saved), new_state(), approved());
        f.pid_alive = Box::new(move |_| alive);
        let mut wf = f.workflow(RunSettings::default());
        assert_eq!(wf.run(), verdict(APPROVE), "{name}");
        assert_eq!(wf.state().error, None, "{name}");
    }
}

#[test]
fn live_pid_with_a_late_beat_blocks() {
    let mut f = Fixture::on(host_with_other_run(HEARTBEAT_SECONDS + 1, &[]), new_state(), approved());
    f.pid_alive = Box::new(|_| true);
    assert_refused(&f, RunSettings::default(), "1m");
}

#[test]
fn two_runs_that_claim_together_both_refuse() {
    let host = Rc::new(FakeHost::default());
    let first = Fixture::on(Rc::clone(&host), RunState::new("20261009-220151-aaaaaa", "first", "/proj"), approved());
    // The second lives as long as the host's hook that runs it, for the rest of the test.
    let second: &'static Fixture = Box::leak(Box::new(Fixture::on(
        Rc::clone(&host),
        RunState::new("20261009-220151-bbbbbb", "second", "/proj"),
        approved(),
    )));
    let mut second_run = second.workflow(RunSettings::default());
    let (lister, mut reads) = (Rc::clone(&host), 0);
    // The second claims and checks while the first reads the runs: each finds the other's claim.
    host.state().run_states_with = Some(Box::new(move |cwd| {
        reads += 1;
        if reads > 1 {
            return None;
        }
        second_run.claim().unwrap();
        let runs = lister.run_states(cwd);
        let message = failure(second_run.run());
        assert!(message.starts_with("run 20261009-220151-aaaaaa is running in /proj"), "{message}");
        Some(runs)
    }));
    let message = failure(first.workflow(RunSettings::default()).run());
    assert!(message.starts_with("run 20261009-220151-bbbbbb is running in /proj"), "{message}");
    for f in [&first, second] {
        let saved = f.saved();
        assert!(text(&saved, "error").unwrap().contains("one run at a time per checkout"));
        assert_eq!(saved.get("owner"), Some(&Json::Null));
    }
}
