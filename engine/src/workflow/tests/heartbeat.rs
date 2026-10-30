//! TestHeartbeat: state.json kept fresh while a turn runs, the run's owner, a takeover, an interrupt and a failure.

use std::cell::RefCell;

use super::*;
use crate::constants::HEARTBEAT_SECONDS;
use crate::herdr::HerdrError;
use crate::runs::local_host;

/// How many times the host wrote the run's state.json.
fn state_writes(f: &Fixture) -> usize {
    let path = format!("{}/state.json", f.state.dir());
    f.host.state().writes.iter().filter(|w| **w == path).count()
}

/// The saved state.json's field key, as the host holds it now.
fn saved_field(host: &FakeHost, path: &str, key: &str) -> Json {
    let text = host.state().files.get(path).cloned().expect("a state.json");
    Json::parse(&text).unwrap().get(key).cloned().unwrap_or(Json::Null)
}

#[test]
fn heartbeat_during_a_long_interview() {
    let f = Fixture::new(vec![("spec", vec![idle()])]);
    let path = format!("{}/state.json", f.state.dir());
    let beats = Rc::new(RefCell::new(vec![]));
    let (host, interrupt, seen) = (Rc::clone(&f.host), f.interrupt.clone(), Rc::clone(&beats));
    f.clock.hook(move |now| {
        seen.borrow_mut().push(saved_field(&host, &path, "heartbeat_at"));
        if now > (10 * STALL_SECONDS) as f64 {
            interrupt.raise();
        }
    });
    assert_eq!(f.workflow(RunSettings::default()).run(), Err(Stopped::Interrupted));
    // About one write a minute over the 30-minute interview, not one per 3 s poll.
    let minutes = (10 * STALL_SECONDS / HEARTBEAT_SECONDS) as f64;
    assert!((state_writes(&f) as f64 - minutes).abs() < 8.0, "{} writes", state_writes(&f));
    let mut distinct = beats.borrow().iter().map(Json::dumps).collect::<Vec<_>>();
    distinct.dedup();
    assert!(distinct.len() as f64 > minutes - 2.0, "{} beats", distinct.len());
}

#[test]
fn owner_is_recorded_and_released() {
    let owners = Rc::new(RefCell::new(vec![]));
    let seen = Rc::clone(&owners);
    let mut spec = spec_turn();
    let path = format!("{}/state.json", new_state().dir());
    let watching: Turn = Box::new(move |prompt, state, host| {
        seen.borrow_mut().push(saved_field(host, &path, "owner"));
        spec(prompt, state, host)
    });
    let f = Fixture::new(vec![
        ("spec", vec![watching]),
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    f.workflow(RunSettings::default()).run().unwrap();
    let owner = owners.borrow()[0].clone();
    assert_eq!(owner.get("pid"), Some(&Json::from(i64::from(std::process::id()))));
    assert_eq!(owner.get("host"), Some(&Json::from(local_host())));
    assert_eq!(f.saved().get("owner"), Some(&Json::Null));
}

#[test]
fn startup_dialog_waits_in_steps_with_heartbeats() {
    let f = Fixture::new(approved());
    f.herdr.state().blocked_at_start.insert("spec".into());
    let waits = Rc::new(RefCell::new(vec![]));
    let (seen, clock) = (Rc::clone(&waits), Rc::clone(&f.clock));
    f.herdr.state().wait_with = Some(Box::new(move |_, timeout_ms, _| {
        seen.borrow_mut().push(timeout_ms);
        clock.advance(timeout_ms.unwrap_or(0) as f64 / 1000.0);
        if seen.borrow().len() < 4 {
            return Err(HerdrError::new("timeout", "timed out waiting for agent status"));
        }
        Ok("idle".into())
    }));
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert_eq!(*waits.borrow(), [Some(HEARTBEAT_SECONDS * 1000); 4]);
    // Three during the dialog, on top of the phase saves.
    assert!(state_writes(&f) >= 3 + 5, "{} writes", state_writes(&f));
}

#[test]
fn failed_heartbeat_is_not_fatal() {
    // No pull request: the Builder here writes its report behind the flaky write and changes no files.
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![idle()]),
        ("review", vec![review_turn(1, APPROVE)]),
    ]);
    let failures = Rc::new(RefCell::new(vec![]));
    let (failed, clock) = (Rc::clone(&failures), Rc::clone(&f.clock));
    f.host.state().before_write = Some(Box::new(move |path, _| {
        if path.ends_with("state.json") && clock.now() > 100.0 && failed.borrow().is_empty() {
            failed.borrow_mut().push(path.to_string());
            return Err(OrchestratorError("ssh: connection reset".into()));
        }
        Ok(())
    }));
    let (host, build) = (Rc::clone(&f.host), f.state.build_path(1));
    f.clock.hook(move |now| {
        if now > 200.0 && host.read(&build).unwrap().is_none() {
            host.state().files.insert(build.as_str(), "r".to_string());
        }
    });
    assert_eq!(f.workflow(no_pr()).run(), verdict(APPROVE));
    assert_eq!(failures.borrow().len(), 1);
    assert!(f.told.lines().contains(&"heartbeat failed: ssh: connection reset".into()));
}

#[test]
fn takeover_stops_without_saving() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![idle()])]);
    let path = format!("{}/state.json", f.state.dir());
    let other: Json = Json::parse(r#"{"host": "laptop", "pid": 4242, "started_at": "2026-09-30T07:00:00+00:00"}"#).unwrap();
    let (host, theirs) = (Rc::clone(&f.host), other.clone());
    f.clock.hook(move |now| {
        if now > 30.0 && saved_field(&host, &path, "owner") != theirs {
            let mut saved = match Json::parse(host.state().files.get(&path).unwrap()) {
                Ok(Json::Object(saved)) => saved,
                other => panic!("{other:?}"),
            };
            saved.insert("owner", theirs.clone());
            host.state().files.insert(path.as_str(), Json::Object(saved).dumps());
        }
    });
    match f.workflow(RunSettings::default()).run() {
        Err(Stopped::TakenOver(e)) => assert!(e.0.contains("taken over by pid 4242 on laptop"), "{e}"),
        other => panic!("not taken over: {other:?}"),
    }
    let saved = f.saved();
    assert_eq!(saved.get("owner"), Some(&other));
    assert_eq!(saved.get("error"), Some(&Json::Null));
}

#[test]
fn interrupt_is_recorded() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()])]);
    let interrupt = f.interrupt.clone();
    let interrupted: Turn = Box::new(move |_, _, _| {
        interrupt.raise();
        "working".to_string()
    });
    f.herdr.state().script.insert("build".into(), vec![interrupted].into());
    assert_eq!(f.workflow(RunSettings::default()).run(), Err(Stopped::Interrupted));
    let saved = f.saved();
    assert_eq!((text(&saved, "error").as_deref(), saved.get("owner")), (Some("interrupted"), Some(&Json::Null)));
}

#[test]
fn failure_releases_the_run() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![idle()])]);
    failure(f.workflow(RunSettings { turn_timeout: 60, ..RunSettings::default() }).run());
    assert_eq!(f.saved().get("owner"), Some(&Json::Null));
}

/// What summary.md says of a run that failed: written by release, as for one that is done.
#[test]
fn a_released_run_has_its_summary() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![idle()])]);
    failure(f.workflow(RunSettings { turn_timeout: 60, ..RunSettings::default() }).run());
    let summary = f.host.state().files.get(&format!("{}/summary.md", f.state.dir())).cloned();
    assert!(summary.is_some_and(|s| s.contains("did not write")));
}
