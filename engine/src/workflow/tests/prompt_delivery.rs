//! TestPromptDelivery: a prompt counts as delivered only once its agent takes it, and is sent once more when it does
//! not.

use std::cell::RefCell;

use super::*;
use crate::constants::PROMPT_DELIVERY_SECONDS;
use crate::herdr::HerdrError;

const BUILD: &str = "build-a1b2c3";

/// The default run approved in round 1, the Builder playing build.
fn default_run(build: Turn) -> Fixture {
    Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![build]), ("review", vec![review_turn(1, APPROVE)])])
}

/// The texts of the prompts sent to the Builder, dropped or not.
fn build_prompts(f: &Fixture) -> Vec<String> {
    f.herdr.state().prompts.iter().filter(|(name, _)| name == BUILD).map(|(_, text)| text.clone()).collect()
}

/// A default run whose Builder drops both prompts: the fixture, the run's error and the state it saved.
fn failed_twice() -> (Fixture, String, Map<Json>) {
    let f = default_run(build_turn(1));
    f.herdr.state().dropped.insert("build".into(), 2);
    let error = failure(f.workflow(RunSettings::default()).run());
    let saved = f.saved();
    (f, error, saved)
}

#[test]
fn an_agent_that_turns_working_is_prompted_once() {
    let f = Fixture::new(vec![("spec", vec![idle()]), ("build", vec![build_turn(1)]), ("review", vec![review_turn(1, APPROVE)])]);
    let prompted = Rc::new(RefCell::new(vec![]));
    let (host, seen, spec, path) = (Rc::clone(&f.host), Rc::clone(&prompted), f.state.spec_path(), f.state.dir() + "/state.json");
    f.clock.hook(move |now| {
        if now > STALL_SECONDS as f64 && host.read(&spec).unwrap().is_none() {
            let saved = Json::parse(&host.read(&path).unwrap().unwrap()).unwrap();
            seen.borrow_mut().push(saved.get("prompted").cloned().unwrap());
            host.write(&spec, "spec", false).unwrap();
        }
    });
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert_eq!(prompts_to(&f, "spec-a1b2c3"), 1);
    assert_eq!(*prompted.borrow(), [Json::from("spec.md")]);
    assert_eq!(PROMPT_DELIVERY_SECONDS, 30);
    assert_eq!(f.herdr.state().deliveries, [("spec-a1b2c3".to_string(), Some(30_000))]);
}

#[test]
fn an_agent_that_writes_its_file_unseen_working_is_prompted_once() {
    let f = default_run(build_turn(1));
    // Every turn here writes its file during the prompt, before a wait could see the agent working.
    f.herdr.state().wait_with = Some(Box::new(|name, _, _| panic!("{name} was waited on")));
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(build_prompts(&f).len(), 1);
    assert_eq!(wf.state().prompted.as_deref(), Some("review-1.md"));
}

#[test]
fn a_file_written_while_the_wait_times_out_counts_as_taken() {
    let f = default_run(idle());
    let (host, state, mut build) = (Rc::clone(&f.host), f.state.clone(), build_turn(1));
    f.herdr.state().wait_with = Some(Box::new(move |_, _, _| {
        build("", &state, &host);
        Err(HerdrError::new("timeout", "agent wait timed out"))
    }));
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert_eq!(build_prompts(&f).len(), 1);
}

#[test]
fn a_dropped_prompt_is_sent_once_more() {
    let f = default_run(build_turn(1));
    f.herdr.state().dropped.insert("build".into(), 1);
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    let prompts = build_prompts(&f);
    let [first, again] = &prompts[..] else { panic!("{prompts:?}") };
    assert_eq!(again, first);
    assert!(f.told.lines().contains(&"[build] did not take its prompt within 30s; sending it again".to_string()));
    assert_eq!(f.herdr.state().deliveries, [(BUILD.to_string(), Some(30_000))]);
}

#[test]
fn two_dropped_prompts_fail_the_turn_unrecorded() {
    let (f, error, saved) = failed_twice();
    let d = f.state.dir();
    assert_eq!(
        error,
        format!("the Builder did not take its prompt to write {d}/build-1.md, sent twice; see pane w1:p2, and resume to send it again")
    );
    assert_eq!(text(&saved, "prompted").as_deref(), Some("spec.md"));
    let files: Vec<&str> = saved.get("turns").unwrap().as_array().unwrap().iter().filter_map(|t| t.get("file")?.as_str()).collect();
    assert!(!files.contains(&"build-1.md"), "{files:?}");
}

#[test]
fn a_resume_after_two_dropped_prompts_sends_the_prompt_again() {
    // Alive, and exited with a session to resume: neither is only waited on, and both get the step's prompt.
    for (alive, sessions) in [(&["build"][..], &[][..]), (&[][..], &[("build", "s-build")][..])] {
        let (f, _, saved) = failed_twice();
        let seen = seen();
        let script = vec![("build", vec![recording(build_turn(1), &seen)]), ("review", vec![review_turn(1, APPROVE)])];
        let f2 = Fixture::resume(Rc::clone(&f.host), RunState::from_saved(&saved).unwrap(), script, alive, sessions);
        assert_eq!(f2.workflow(f2.as_saved()).run(), verdict(APPROVE), "{alive:?} {sessions:?}");
        let seen = seen.borrow();
        assert_eq!(seen.len(), 1, "{alive:?} {sessions:?}");
        assert!(seen[0].starts_with("You are the Builder"), "{}", seen[0]);
    }
}

#[test]
fn a_herdr_error_other_than_a_timeout_propagates() {
    let f = default_run(idle());
    f.herdr.state().wait_with = Some(Box::new(|_, _, _| Err(HerdrError::new("server_error", "herdr is gone"))));
    let message = failure(f.workflow(RunSettings::default()).run());
    assert!(message.starts_with("herdr server_error"), "{message}");
}

#[test]
fn an_agent_exiting_while_its_prompt_is_awaited_fails_the_turn() {
    let f = default_run(idle());
    let herdr = Rc::clone(&f.herdr);
    // The agent exits, and the fake's own wait then finds no agent to wait on, as herdr does.
    f.herdr.state().wait_with = Some(Box::new(move |name, _, _| {
        herdr.state().statuses.remove(name);
        Err(HerdrError::new("agent_not_found", format!("agent target {name} not found")))
    }));
    let message = failure(f.workflow(RunSettings::default()).run());
    assert_eq!(message, format!("the Builder exited without writing {}/build-1.md", f.state.dir()));
}
