//! The Ctrl-C while the run waits on herdr, and once it is done: not in Python, whose herdr gets the terminal's SIGINT
//! itself and whose tests raise KeyboardInterrupt in a fake. Here a real ProcessHerdr, running a herdr script, is what
//! the Ctrl-C stops (TODO.md's Ctrl-C entry); the interrupted proof, the command case, is proofs.rs's.

use super::*;
use crate::herdr::{Herdr, ProcessHerdr};
use crate::host::tests::{environment, tool};
use crate::test_support::{ended, raise_once_written, TempDir};

/// A ProcessHerdr of the fixture's Ctrl-C whose herdr is script, in a directory of its own.
fn real_herdr(f: &Fixture, d: &TempDir, script: &str) -> ProcessHerdr {
    tool(&d.path(), "herdr", script);
    ProcessHerdr::in_environment(None, environment(Some(&d.path()), &[]).into_iter(), &f.interrupt)
}

/// The run as it was saved, which must say it was interrupted and that no orchestrator drives it, once the Workflow
/// caught the Ctrl-C to save that.
fn interrupted(f: &Fixture) -> RunState {
    assert!(!f.interrupt.is_raised());
    let saved = RunState::from_saved(&f.saved()).unwrap();
    assert_eq!(saved.error.as_deref(), Some("interrupted"));
    assert_eq!(saved.owner, None);
    saved
}

#[test]
fn a_ctrl_c_while_herdr_waits_stops_the_wait_and_the_run() {
    let d = TempDir::new();
    let pid = d.join("pid");
    let f = Fixture::new(vec![("spec", vec![working()])]);
    let herdr = real_herdr(&f, &d, &format!("#!/bin/sh\necho $$ > '{pid}'\nexec sleep 600\n"));
    f.herdr.state().wait_with = Some(Box::new(move |name, timeout_ms, until| herdr.wait(name, timeout_ms, until)));
    let raising = raise_once_written(&pid, &f.interrupt);
    assert_eq!(f.workflow(RunSettings::default()).run(), Err(Stopped::Interrupted));
    raising.join().unwrap();
    assert!(ended(&pid), "herdr agent wait outlived the Ctrl-C");
    // The wait was the delivery check: the prompt never counted as taken.
    let saved = interrupted(&f);
    assert_eq!((saved.prompted, saved.turns.len()), (None, 0));
}

#[test]
fn a_ctrl_c_during_a_prompt_herdr_delivered_records_no_prompt() {
    // As Python's KeyboardInterrupt in herdr's prompt: no prompted, no turn record, whatever herdr did with the text.
    let d = TempDir::new();
    let delivered = d.join("delivered");
    let f = Fixture::new(vec![("spec", vec![spec_turn()])]);
    let script = format!("#!/bin/sh\ntrap 'exit 0' INT\necho \"$4\" > '{delivered}'\nwhile :; do sleep 0.05; done\n");
    let herdr = real_herdr(&f, &d, &script);
    f.herdr.state().prompt_with = Some(Box::new(move |name, text| herdr.prompt(name, text)));
    let raising = raise_once_written(&delivered, &f.interrupt);
    assert_eq!(f.workflow(RunSettings::default()).run(), Err(Stopped::Interrupted));
    raising.join().unwrap();
    assert!(std::fs::read_to_string(&delivered).unwrap().len() > 1, "herdr was given no prompt");
    let saved = interrupted(&f);
    assert_eq!((saved.prompted, saved.turns.len()), (None, 0));
    // The Spec Writer's turn was never played.
    assert_eq!(f.herdr.state().prompts.len(), 0);
}

#[test]
fn a_ctrl_c_once_the_run_is_done_stops_what_is_left_of_its_end() {
    // As Python's KeyboardInterrupt after the run's last save: the run stays done, and is not released again.
    let f = Fixture::new(approved());
    let (interrupt, summary) = (f.interrupt.clone(), format!("{}/summary.md", f.state.dir()));
    f.host.state().before_write = Some(Box::new(move |path, _| {
        if path == summary {
            interrupt.raise();
        }
        Ok(())
    }));
    assert_eq!(f.workflow(RunSettings::default()).run(), Err(Stopped::Interrupted));
    let saved = RunState::from_saved(&f.saved()).unwrap();
    assert_eq!((saved.phase.as_str(), saved.error, saved.owner), ("done", None, None));
    assert!(!f.host.state().files.contains_key(&format!("{}/metrics.json", f.state.dir())));
    assert!(!f.told.titles().iter().any(|t| t.starts_with("Run finished")), "{:?}", f.told.titles());
    assert!(!f.herdr.state().calls.iter().any(|c| matches!(c, Call::Close { .. })));
}

#[test]
fn a_ctrl_c_that_fails_the_done_runs_save_is_the_interrupt_not_a_failure() {
    // On a ProcessHost the save is a command, which the Ctrl-C fails: that is the interrupt, as Python's
    // KeyboardInterrupt in the save is, not an error the CLI would print.
    let f = Fixture::new(approved());
    let (interrupt, state) = (f.interrupt.clone(), format!("{}/state.json", f.state.dir()));
    f.host.state().before_write = Some(Box::new(move |path, text| {
        if path == state && text.contains("\"phase\": \"done\"") {
            interrupt.raise();
            return Err(crate::error::OrchestratorError(format!("{path}: interrupted")));
        }
        Ok(())
    }));
    assert_eq!(f.workflow(RunSettings::default()).run(), Err(Stopped::Interrupted));
    assert_eq!(RunState::from_saved(&f.saved()).unwrap().error, None);
}

/// A run resumed at publish, with locked files, and with or without a quality gate that was on, whose human presses
/// Ctrl-C at its first git_command; the fixture, once its run has stopped as interrupted.
fn interrupted_at_the_end(had_gate: bool, git_command: &'static str) -> Fixture {
    let mut state = saved_run("publish", 1, &["build"]);
    state.pull_request = true;
    (state.base_branch, state.branch) = (Some("main".into()), Some("orchestrator/x-a1b2c3".into()));
    (state.verdict, state.workflow) = (Some(crate::constants::FINISHED.into()), "solo".into());
    state.quality_gate_disabled = had_gate;
    state.ci = [("build", Json::from(41))].into_iter().collect();
    state.locked = [("build", vec!["tests/test_x.py".to_string()])].into_iter().collect();
    let host = Rc::new(FakeHost::new(Some("def456"), "orchestrator/x-a1b2c3"));
    host.state().refs.insert(format!("{}build/1", state.refs_prefix()), "abc123".to_string());
    let f = Fixture::resume(Rc::clone(&host), state, vec![], &[], &[]);
    let interrupt = f.interrupt.clone();
    host.state().before_git = Some(Box::new(move |_, args| {
        if args.first() == Some(&git_command) {
            interrupt.raise();
        }
    }));
    let settings = RunSettings { pipeline: Some(crate::test_support::solo()), ..f.as_saved() };
    assert_eq!(f.workflow(settings).run(), Err(Stopped::Interrupted));
    assert_eq!(interrupted(&f).phase, "publish");
    f
}

#[test]
fn a_ctrl_c_during_the_end_clean_ups_releases_the_run_and_keeps_its_build() {
    // As Python's KeyboardInterrupt in _clean_up_quality, which its logged failures do not catch: the run is released
    // as interrupted, with RunState::ci kept for a resume, and its local refs are not deleted.
    let f = interrupted_at_the_end(true, "ls-remote");
    assert_eq!(RunState::from_saved(&f.saved()).unwrap().ci, [("build", Json::from(41))].into_iter().collect::<Map<Json>>());
    assert_eq!(f.host.state().refs.len(), 1, "the local refs were deleted");
    // And at publish's last command, which a FakeHost lets succeed where a ProcessHost fails it: the run is released
    // as interrupted all the same.
    interrupted_at_the_end(false, "switch");
}

#[test]
fn a_ctrl_c_that_fails_a_heartbeat_is_the_interrupt_not_a_missed_beat() {
    let f = Fixture::new(vec![("spec", vec![working()])]);
    let (interrupt, clock, state) = (f.interrupt.clone(), Rc::clone(&f.clock), format!("{}/state.json", f.state.dir()));
    f.host.state().before_write = Some(Box::new(move |path, _| {
        if path == state && clock.now() >= crate::constants::HEARTBEAT_SECONDS as f64 {
            interrupt.raise();
            return Err(crate::error::OrchestratorError(format!("{path}: interrupted")));
        }
        Ok(())
    }));
    assert_eq!(f.workflow(RunSettings::default()).run(), Err(Stopped::Interrupted));
    assert!(!f.told.lines().iter().any(|line| line.starts_with("heartbeat failed")), "{:?}", f.told.lines());
}
