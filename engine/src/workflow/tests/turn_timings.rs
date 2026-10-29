//! TestTurnTimings and TestRunMetrics: each turn's record on the host's clock, summary.md at every end and
//! metrics.json at a run's end, with each agent turn's tokens.
//!
//! The turn_tokens half of TestRunMetrics' test_a_gated_run_of_two_rounds_records_its_turns_and_quality_rounds, which
//! calls turn_tokens on a log it makes, is crate::summary's (turn_tokens_sums_the_answers_within_the_turn_as_the_python_engine_does).
//!
//! Not here: test_a_seeded_spec_has_no_record, in seeded_spec.rs; test_the_metrics_command_prints_a_line_per_run_that_is_done,
//! the metrics command's, in crate::cli::listing's tests. test_durations, test_a_pipe_in_a_cell_does_not_split_it,
//! test_turn_tokens_needs_a_known_window_and_a_harness_it_reads and test_session_log_finds_the_file_under_home are
//! crate::summary's and crate::host's (ticket 06).

use std::cell::RefCell;

use super::*;
use crate::host::fake::HOST_EPOCH;
use crate::test_support::GOLDEN_SUMMARY;

/// The run's summary.md, if written.
fn summary(f: &Fixture) -> Option<String> {
    f.host.state().files.get(&format!("{}/summary.md", f.state.dir())).cloned()
}

fn settings(change: impl FnOnce(&mut RunSettings)) -> RunSettings {
    let mut settings = RunSettings::default();
    change(&mut settings);
    settings
}

#[test]
fn each_turn_of_an_approved_run_is_recorded_on_the_host_clock() {
    let f = Fixture::new(vec![
        ("spec", vec![taking(600, spec_turn())]),
        ("build", vec![taking(1200, build_turn(1))]),
        ("review", vec![taking(300, review_turn(1, APPROVE))]),
    ]);
    let mut wf = f.workflow(settings(|s| {
        s.agents.models.insert("review", "opus".into());
        s.agents.kinds.insert("build", "codex".into());
    }));
    assert_eq!(wf.run(), verdict(APPROVE));
    let expected = [
        record("spec", "spec.md", Some(0), Some(600)).with("round", 0).with("human_paced", true),
        record("build", "build-1.md", Some(600), Some(1800)).with("agent", "codex"),
        record("review", "review-1.md", Some(1800), Some(2100)).with("model", "opus"),
    ];
    assert_eq!(expected, wf.state().turns[..]);
    let saved = f.saved();
    let turns: Vec<Map<Json>> = saved.get("turns").unwrap().as_array().unwrap().iter().map(|t| t.as_object().unwrap().clone()).collect();
    assert_eq!(expected, turns[..]);
    // The workflow's own clock, which stamps the heartbeat, is HOST_EPOCH behind the host's.
    assert_eq!(text(&saved, "heartbeat_at").as_deref(), Some("1970-01-01T00:35:00+00:00"));
    assert_eq!(summary(&f).as_deref(), Some(GOLDEN_SUMMARY));
    let body = f.host.state().prs[0].body.clone();
    let timings = GOLDEN_SUMMARY.split("## Turns\n\n").nth(1).unwrap().split("\n\n").next().unwrap();
    assert!(body.ends_with(&format!("<details>\n<summary>Run timings</summary>\n\n{timings}\n\n</details>\n")), "{body}");
}

#[test]
fn the_end_is_the_files_mtime_even_when_no_orchestrator_watched() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![working()])]);
    let interrupt = f.interrupt.clone();
    f.clock.hook(move |now| {
        if now >= 900.0 {
            interrupt.raise();
        }
    });
    assert_eq!(f.workflow(RunSettings::default()).run(), Err(Stopped::Interrupted));
    let saved = RunState::from_saved(&f.saved()).unwrap();
    assert_eq!(record("build", "build-1.md", Some(0), None), *saved.turns.last().unwrap());
    let written = summary(&f).unwrap();
    assert!(written.contains("- **Outcome:** error: interrupted\n"), "{written}");
    assert!(written.contains("| build | build | 1 | claude | default | 2026-10-03 04:00:00 | ? | build-1.md |\n"), "{written}");

    // The Builder finishes while no orchestrator runs, and one resumes the run much later. The host keeps this
    // fixture's clock, which the resumed run's turn moves on too.
    f.clock.set(1200.0);
    build_turn(1)("", &saved, &f.host);
    f.clock.set(5000.0);
    let review = vec![("review", vec![taking(100, review_turn(1, APPROVE))])];
    let f2 = Fixture::resume(Rc::clone(&f.host), saved, review, &["build"], &[]);
    let mut wf = f2.workflow(f2.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    let builds: Vec<&Map<Json>> = wf.state().turns.iter().filter(|t| t.get("file") == Some(&Json::from("build-1.md"))).collect();
    assert_eq!([record("build", "build-1.md", Some(0), Some(1200))], builds.into_iter().cloned().collect::<Vec<_>>()[..]);
    assert_eq!(record("review", "review-1.md", Some(5000), Some(5100)), *wf.state().turns.last().unwrap());
    let written = summary(&f2).unwrap();
    assert!(written.contains("- **Outcome:** APPROVE\n"), "{written}");
    assert!(
        written.contains(
            "| build | build | 1 | claude | default | 2026-10-03 04:00:00 | 20m 00s | build-1.md |\n\
             | review | review | 1 | claude | default | 2026-10-03 05:23:20 | 1m 40s | review-1.md |\n"
        ),
        "{written}"
    );
}

#[test]
fn a_restarted_agent_keeps_its_turns_record() {
    let mut state = saved_run("build", 2, FULL);
    state.prompted = Some("build-2.md".into());
    state.turns = vec![record("build", "build-2.md", Some(-1000), None).with("round", 2).map()];
    let script = vec![("build", vec![taking(50, build_turn(2))]), ("review", vec![review_turn(2, APPROVE)])];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, script, &["review"], &[]);
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert!(called(&f, &Call::Prompt { name: "build-a1b2c3".into() }));
    let files: Vec<&Json> = wf.state().turns.iter().map(|t| t.get("file").unwrap()).collect();
    assert_eq!(files, [&Json::from("build-2.md"), &Json::from("review-2.md")]);
    assert_eq!(record("build", "build-2.md", Some(-1000), Some(50)).with("round", 2), wf.state().turns[0]);
}

#[test]
fn a_record_closed_before_the_run_moved_on_is_not_recorded_again() {
    let closed = record("build", "build-1.md", Some(-100), Some(-50));
    let mut state = saved_run("build", 1, &["spec", "build"]);
    state.prompted = Some("build-1.md".into());
    state.turns = vec![closed.map()];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, vec![("review", vec![review_turn(1, APPROVE)])], &[], &[]);
    f.put(&f.state.build_path(1), "report");
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    let turns = &wf.state().turns;
    assert_eq!([closed], turns[..turns.len() - 1]);
    assert_eq!(turns.last().unwrap().get("file").unwrap(), &Json::from("review-1.md"));
}

#[test]
fn a_rejected_verdict_keeps_its_record_under_its_new_name() {
    let f = Fixture::new(vec![
        ("spec", vec![spec_turn()]),
        ("build", vec![build_turn(1)]),
        ("review", vec![taking(60, malformed_review(1, "looks fine")), taking(30, review_turn(1, APPROVE))]),
    ]);
    let mut wf = f.workflow(RunSettings::default());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(
        [record("review", "review-1.rejected.md", Some(0), Some(60)), record("review", "review-1.md", Some(60), Some(90))],
        wf.state().turns[2..]
    );
    let rejected = format!("{}/review-1.rejected.md", f.state.dir());
    assert_eq!(f.host.state().mtimes.get(&rejected), Some(&(HOST_EPOCH + 60)));
}

#[test]
fn a_state_saved_before_turns_resumes_and_records_its_files_without_a_start() {
    let mut state = saved_run("review", 1, FULL);
    state.prompted = Some("review-1.md".into());
    state.pull_request = true;
    state.base_branch = Some("main".into());
    state.branch = Some("orchestrator/x-a1b2c3".into());
    let mut saved = state.to_json().as_object().unwrap().clone();
    saved.remove("turns");
    let state = RunState::from_saved(&saved).unwrap();
    assert!(state.turns.is_empty());
    let host = Rc::new(FakeHost::new(Some("def456"), "orchestrator/x-a1b2c3"));
    let f = Fixture::resume(host, state, vec![], &["review"], &[]);
    let review = f.state.review_path(1);
    f.put(&review, &format!("VERDICT: {APPROVE}\n"));
    f.host.state().mtimes.insert(review, HOST_EPOCH - 60);
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!([record("review", "review-1.md", None, Some(-60))], wf.state().turns[..]);
    let written = summary(&f).unwrap();
    assert!(written.contains("- **Wall time:** ?, from the first turn's start"), "{written}");
    let row = "| review | review | 1 | claude | default | ? | ? | review-1.md |";
    assert!(written.contains(&format!("{row}\n")), "{written}");
    assert!(f.host.state().prs[0].body.contains(row));
}

#[test]
fn every_end_writes_a_summary() {
    let d = new_state().dir();
    let solo = crate::test_support::solo();
    let cases: Vec<(&str, RunState, Script, RunSettings, Vec<String>)> = vec![
        (
            "changes requested",
            new_state(),
            vec![("spec", vec![spec_turn()]), ("build", vec![build_turn(1)]), ("review", vec![review_turn(1, CHANGES_REQUESTED)])],
            max_rounds(1),
            strings(&[
                "- **Outcome:** CHANGES_REQUESTED\n",
                "- Round 1: review CHANGES_REQUESTED\n",
                "- **Pull request:** https://github.com/o/r/pull/7\n",
            ]),
        ),
        (
            "finished",
            new_run(&solo),
            vec![("build", vec![solo_turn()])],
            RunSettings { pipeline: Some(solo.clone()), pull_request: false, ..RunSettings::default() },
            strings(&[
                "- **Workflow:** solo\n",
                "- **Outcome:** FINISHED\n",
                "| Builder (build) | claude | default |\n",
                "- **Pull request:** none\n- **Branch:** none\n",
                "- Round 1: no verdict\n",
            ]),
        ),
        (
            "failed",
            new_state(),
            vec![("spec", vec![spec_turn()]), ("build", vec![idle()])],
            RunSettings { turn_timeout: 60, ..RunSettings::default() },
            vec![
                format!("- **Outcome:** error: the Builder did not write {d}/build-1.md within 60s; see pane w1:p2\n"),
                "| build | build | 1 | claude | default | 2026-10-03 04:00:00 | ? | build-1.md |\n".into(),
                "- Round 1: no verdict\n".into(),
            ],
        ),
    ];
    for (name, state, script, settings, parts) in cases {
        let f = Fixture::on(Rc::new(FakeHost::default()), state, script);
        let _ = f.workflow(settings).run();
        let written = summary(&f).unwrap_or_else(|| panic!("{name}: no summary"));
        for part in parts {
            assert!(written.contains(&part), "{name}: {part:?} not in {written}");
        }
    }
}

#[test]
fn a_run_without_turns_says_so() {
    let mut state = saved_run("publish", 1, &["build"]);
    state.base_branch = Some("main".into());
    state.branch = Some("orchestrator/x-a1b2c3".into());
    state.verdict = Some("FINISHED".into());
    state.workflow = "solo".into();
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, vec![], &[], &[]);
    let settings = RunSettings { pipeline: Some(crate::test_support::solo()), ..f.as_saved() };
    assert_eq!(f.workflow(settings).run(), verdict("FINISHED"));
    assert!(summary(&f).unwrap().contains("## Turns\n\nNo turns were recorded.\n"));
}

#[test]
fn a_run_taken_over_writes_no_summary() {
    let other = Json::parse(r#"{"host": "laptop", "pid": 4242, "started_at": "2026-09-30T07:00:00+00:00"}"#).unwrap();
    for interrupted in [false, true] {
        let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![idle()])]);
        let path = format!("{}/state.json", f.state.dir());
        let (host, interrupt, theirs) = (Rc::clone(&f.host), f.interrupt.clone(), other.clone());
        f.clock.hook(move |now| {
            let Ok(Json::Object(mut saved)) = Json::parse(&host.state().files.get(&path).unwrap().clone()) else { panic!() };
            if now > 30.0 && saved.get("owner") != Some(&theirs) {
                saved.insert("owner", theirs.clone());
                host.state().files.insert(path.as_str(), Json::Object(saved).dumps());
                if interrupted {
                    interrupt.raise();
                }
            }
        });
        let outcome = f.workflow(RunSettings::default()).run();
        if interrupted {
            assert_eq!(outcome, Err(Stopped::Interrupted));
            let expected = "could not record the failure in state.json: run 20260929-120000-a1b2c3 was taken over by pid \
                            4242 on laptop";
            assert!(f.told.lines().iter().any(|l| l.contains(expected)), "{:?}", f.told.lines());
        } else {
            assert!(matches!(outcome, Err(Stopped::TakenOver(_))), "{outcome:?}");
        }
        assert_eq!(summary(&f), None, "interrupted: {interrupted}");
    }
}

#[test]
fn a_failure_that_cannot_be_saved_still_writes_a_summary() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![idle()])]);
    f.host.state().before_write = Some(Box::new(|path, text| {
        if path.ends_with("/state.json") && Json::parse(text).unwrap().get("error").is_some_and(Json::truthy) {
            return Err(OrchestratorError("ssh: connection reset".into()));
        }
        Ok(())
    }));
    let message = failure(f.workflow(RunSettings { turn_timeout: 60, ..RunSettings::default() }).run());
    assert!(message.contains("did not write"), "{message}");
    assert!(f.told.lines().iter().any(|l| l.starts_with("could not record the failure in state.json: ")));
    assert!(summary(&f).unwrap().contains("- **Outcome:** error: the Builder did not write"));
}

#[test]
fn a_summary_that_cannot_be_written_changes_no_outcome() {
    fn failing_summary() -> Rc<FakeHost> {
        let host = FakeHost::default();
        host.state().before_write = Some(Box::new(|path, _| {
            if path.ends_with("/summary.md") {
                return Err(OrchestratorError("disk full".into()));
            }
            Ok(())
        }));
        Rc::new(host)
    }
    let f = Fixture::on(failing_summary(), new_state(), approved());
    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));
    assert!(f.told.lines().contains(&"could not write summary.md: disk full".to_string()));
    assert_eq!(text(&f.saved(), "phase").as_deref(), Some("done"));

    let f = Fixture::on(failing_summary(), new_state(), vec![("spec", vec![spec_turn()]), ("build", vec![idle()])]);
    let message = failure(f.workflow(RunSettings { turn_timeout: 60, ..RunSettings::default() }).run());
    assert!(message.contains("did not write"), "{message}");
    assert!(f.told.lines().contains(&"could not write summary.md: disk full".to_string()));
}

// -- TestRunMetrics ----------------------------------------------------------------------------------------------

/// A Claude Code session log: for each (host clock reading, input, output) an answer of the model, written as
/// Claude Code writes it, one line per content block, each with the whole answer's usage.
fn claude_log(answers: &[(i64, i64, i64)]) -> String {
    let mut lines = vec![format!(r#"{{"type": "user", "timestamp": "{}", "message": {{"role": "user"}}}}"#, at(0).replace("+00:00", "Z"))];
    for (k, (seconds, tokens_in, tokens_out)) in answers.iter().enumerate() {
        let usage = format!(
            r#"{{"input_tokens": {tokens_in}, "output_tokens": {tokens_out}, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 1}}"#
        );
        let time = at(*seconds).replace("+00:00", ".250Z");
        for _ in 0..2 {
            lines.push(format!(r#"{{"type": "assistant", "timestamp": "{time}", "message": {{"id": "msg_{k}", "usage": {usage}}}}}"#));
        }
    }
    lines.join("\n") + "\n"
}

#[test]
fn a_failed_run_writes_no_metrics() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![idle()])]);
    failure(f.workflow(RunSettings { turn_timeout: 5, ..RunSettings::default() }).run());
    let files = f.host.state().files.clone();
    assert!(files.contains_key(&format!("{}/summary.md", f.state.dir())));
    assert!(!files.contains_key(&format!("{}/metrics.json", f.state.dir())));
}

#[test]
fn tokens_come_from_the_session_the_turn_ran_in() {
    let f = Fixture::new(vec![("spec", vec![spec_turn()]), ("build", vec![taking(100, build_turn(1))])]);
    // The sessions are known once both agents have started: the logs are put in place during the review, which
    // Python's test does just before publishing.
    let sessions = Rc::new(RefCell::new(Map::new()));
    let (herdr, known) = (Rc::clone(&f.herdr), Rc::clone(&sessions));
    let mut review = review_turn(1, APPROVE);
    let with_logs: Turn = Box::new(move |prompt, state, host| {
        for role in ["build", "review"] {
            let session = herdr.state().sessions[&format!("{role}-a1b2c3")].clone();
            known.borrow_mut().insert(role, session);
        }
        let known = known.borrow();
        let mut h = host.state();
        h.session_logs.insert(("claude".into(), known.get("build").unwrap().clone()), claude_log(&[(50, 10, 20), (90, 1, 2), (500, 9, 9)]));
        let pi = [
            format!(
                r#"{{"type": "message", "timestamp": "{}", "message": {{"role": "assistant", "usage": {{"input": 5, "output": 6, "cacheRead": 2}}}}}}"#,
                at(130).replace("+00:00", "Z")
            ),
            format!(
                r#"{{"type": "message", "timestamp": "{}", "message": {{"role": "user", "usage": {{"input": 100, "output": 100}}}}}}"#,
                at(140).replace("+00:00", "Z")
            ),
            "not json".to_string(),
        ];
        h.session_logs.insert(("pi".into(), known.get("review").unwrap().clone()), pi.join("\n"));
        drop(h);
        review(prompt, state, host)
    });
    f.play("review", vec![taking(60, with_logs)]);
    let mut wf = f.workflow(settings(|s| {
        s.agents.kinds.insert("review", "pi".into());
    }));
    assert_eq!(wf.run(), verdict(APPROVE));
    let metrics = Json::parse(f.host.state().files.get(&format!("{}/metrics.json", f.state.dir())).unwrap()).unwrap();
    let tokens: Vec<(String, String)> = metrics.get("turns").unwrap()
        .as_array()
        .unwrap()
        .iter()
        .map(|t| (t.get("role").unwrap().python_str(), t.get("tokens").unwrap().dumps()))
        .collect();
    assert_eq!(tokens, [
        ("spec".to_string(), r#""unknown""#.to_string()),
        ("build".into(), r#"{"input": 11, "output": 22, "cache_read": 14, "cache_write": 2}"#.into()),
        ("review".into(), r#"{"input": 5, "output": 6, "cache_read": 2, "cache_write": 0}"#.into()),
    ]);
    let known = sessions.borrow();
    let turn_sessions: Vec<Json> = wf.state().turns[1..].iter().map(|t| t.get("session").unwrap().clone()).collect();
    assert_eq!(turn_sessions, [Json::from(known.get("build").unwrap().as_str()), Json::from(known.get("review").unwrap().as_str())]);
}

#[test]
fn a_turn_that_changed_sessions_has_unknown_tokens() {
    let mut state = saved_run("build", 1, &["spec", "build"]);
    state.prompted = Some("build-1.md".into());
    state.turns = vec![record("build", "build-1.md", Some(-100), None).with("session", "s-old").with("tokens", Json::Null).map()];
    let f = Fixture::resume(Rc::new(FakeHost::default()), state, vec![
        ("build", vec![build_turn(1)]),
        ("review", vec![review_turn(1, APPROVE)]),
    ], &[], &[("build", "s-old")]);
    f.herdr.state().lost_sessions.insert("s-old".into());
    f.host.state().session_logs.insert(("claude".into(), "s-old".into()), claude_log(&[(-50, 1, 1)]));
    let mut wf = f.workflow(f.as_saved());
    assert_eq!(wf.run(), verdict(APPROVE));
    let first = &wf.state().turns[0];
    assert_eq!((first.get("session").unwrap(), first.get("tokens").unwrap()), (&Json::Null, &Json::from("unknown")));
}

// -- The cases of a gated run -------------------------------------------------------------------------------------

fn field<'j>(t: &'j Map<Json>, key: &str) -> &'j Json {
    t.get(key).unwrap_or_else(|| panic!("no {key} in {t:?}"))
}

#[test]
fn quality_rounds_and_answers_have_records_of_their_own() {
    let f = gated_building(
        vec![
            ("spec", vec![spec_turn()]),
            ("build", vec![build_turn(1), fix_turn(1, 1)]),
            ("review", vec![review_turn(1, APPROVE)]),
        ],
        vec![red(), green()],
    );
    let mut wf = f.workflow(RunSettings::default());

    assert_eq!(wf.run(), verdict(APPROVE));
    let turns = wf.state().turns.clone();
    let shape: Vec<(Json, Json, Json, Json, Json)> = turns
        .iter()
        .map(|t| {
            let get = |k: &str| field(t, k).clone();
            (get("file"), get("step"), get("role"), get("round"), get("quality_round"))
        })
        .collect();
    let row = |file: &str, step: &str, n: i64, q: i64| {
        (Json::from(file), Json::from(step), Json::from(step), Json::from(n), Json::from(q))
    };
    assert_eq!(shape, [
        row("spec.md", "spec", 0, 0),
        row("build-1.md", "build", 1, 0),
        row("quality-1-1.md", "quality", 1, 1),
        row("build-1-q1.md", "build", 1, 1),
        row("quality-1-2.md", "quality", 1, 2),
        row("review-1.md", "review", 1, 0),
    ]);
    let quality: Vec<&Map<Json>> = turns.iter().filter(|t| t.get("role") == Some(&Json::from("quality"))).collect();
    for t in &quality {
        assert_eq!((field(t, "agent"), field(t, "model"), field(t, "human_paced")), (&Json::Null, &Json::Null, &Json::Bool(false)));
    }
    let seconds = |t: &Map<Json>, k: &str| crate::summary::epoch_seconds(field(t, k).as_str().unwrap()).unwrap();
    for t in &turns {
        assert!(seconds(t, "started_at") <= seconds(t, "ended_at"));
        assert_eq!(field(t, "seconds"), &Json::from(seconds(t, "ended_at") - seconds(t, "started_at")));
    }
    // Each quality round polls Jenkins, which takes the host's time too.
    assert!(field(quality[0], "seconds").as_int().unwrap() > 0);
    let summary = summary(&f).unwrap();
    assert!(summary.contains("| quality | quality | 1 q1 | - | - |"), "{summary}");
    assert!(summary.contains("- Round 1: quality gate ERROR, OK; review APPROVE\n"), "{summary}");
}

#[test]
fn a_resumed_analysis_keeps_its_start() {
    let mut state = quality_state(1);
    let host = Rc::new(FakeHost::default());
    let fake_ci_build = 41;
    state.ci = ci_of(&[
        ("ref", REF_1_1.into()),
        ("sha", "snap9".into()),
        ("queue_url", format!("{}/queue/item/1/", crate::ci::fake::JENKINS).into()),
        ("build", fake_ci_build.into()),
    ]);
    state.turns = vec![record("quality", "quality-1-1.md", Some(-500), None)
        .with("quality_round", 1)
        .with("agent", Json::Null)
        .map()];
    let f = Fixture::resume_gated(host, state, vec![("review", vec![review_turn(1, APPROVE)])], &[]);
    assert_eq!(f.ci.state().add_build(REF_1_1, green()), fake_ci_build);
    let mut wf = f.workflow(f.as_saved_gated());

    assert_eq!(wf.run(), verdict(APPROVE));
    // build-1.md was written before turns were recorded.
    let files: Vec<&Json> = wf.state().turns.iter().map(|t| field(t, "file")).collect();
    assert_eq!(files, [&Json::from("quality-1-1.md"), &Json::from("review-1.md")]);
    assert_eq!(field(&wf.state().turns[0], "started_at"), &Json::from(at(-500)));
}

#[test]
fn a_gated_run_of_two_rounds_records_its_turns_and_quality_rounds() {
    let f = gated_building(
        vec![
            ("spec", vec![taking(600, spec_turn())]),
            (
                "build",
                vec![
                    taking(100, build_turn(1)),
                    taking(50, fix_turn(1, 1)),
                    taking(100, build_turn(2)),
                    taking(50, fix_turn(2, 1)),
                ],
            ),
            ("review", vec![taking(30, review_turn(1, CHANGES_REQUESTED)), taking(30, review_turn(2, APPROVE))]),
        ],
        vec![red(), green(), red(), red()],
    );

    assert_eq!(f.workflow(RunSettings::default()).run(), verdict(APPROVE));

    let path = format!("{}/metrics.json", f.state.dir());
    let written = f.host.state().files.get(&path).cloned().unwrap();
    assert_eq!(written.matches('\n').count(), 1);
    let metrics = Json::parse(&written).unwrap();
    let metrics = metrics.as_object().unwrap();
    let turns: Vec<&Map<Json>> = field(metrics, "turns").as_array().unwrap().iter().map(|t| t.as_object().unwrap()).collect();
    let shape: Vec<(Json, Json, Json, Json)> = turns
        .iter()
        .map(|t| (field(t, "step").clone(), field(t, "round").clone(), field(t, "quality_round").clone(), field(t, "harness").clone()))
        .collect();
    let row = |step: &str, n: i64, q: i64| {
        let harness = if step == "quality" { Json::Null } else { Json::from("claude") };
        (Json::from(step), Json::from(n), Json::from(q), harness)
    };
    assert_eq!(shape, [
        row("spec", 0, 0),
        row("build", 1, 0),
        row("quality", 1, 1),
        row("build", 1, 1),
        row("quality", 1, 2),
        row("review", 1, 0),
        row("build", 2, 0),
        row("quality", 2, 1),
        row("build", 2, 1),
        row("quality", 2, 2),
        row("review", 2, 0),
    ]);
    assert_eq!(field(turns[0], "seconds"), &Json::from(600));
    let expected: Map<Json> = [
        ("step", Json::from("build")),
        ("role", Json::from("build")),
        ("harness", Json::from("claude")),
        ("model", Json::Null),
        ("round", Json::from(1)),
        ("quality_round", Json::from(1)),
        ("human_paced", Json::Bool(false)),
        ("started_at", Json::from(at(730))),
        ("ended_at", Json::from(at(780))),
        ("seconds", Json::from(50)),
        ("tokens", Json::from("unknown")),
    ]
    .into_iter()
    .collect();
    assert_eq!(turns[3], &expected);
    assert_eq!(field(turns[2], "tokens"), &Json::Null);
    let tokens: Vec<&Json> = turns.iter().take(3).map(|t| field(t, "tokens")).collect();
    assert_eq!(tokens, [&Json::from("unknown"), &Json::from("unknown"), &Json::Null]);
    let ruff = crate::quality::finding_fingerprint(Some("limiter.py"), "ruff F401", "`os` imported but unused");
    let round = |n: i64, q: i64, failed: bool| -> Json {
        let fingerprints: Map<Json> = if failed {
            [("ruff", Json::Array(vec![Json::from(ruff.as_str())]))].into_iter().collect()
        } else {
            Map::new()
        };
        Json::Object(
            [
                ("round", Json::from(n)),
                ("quality_round", Json::from(q)),
                ("gate", Json::from(if failed { "ERROR" } else { "OK" })),
                ("conditions", Json::Array(vec![])),
                ("fingerprints_by_check", Json::Object(fingerprints)),
            ]
            .into_iter()
            .collect(),
        )
    };
    let quality_rounds = Json::Array(vec![round(1, 1, true), round(1, 2, false), round(2, 1, true), round(2, 2, true)]);
    assert_eq!(field(metrics, "quality_rounds"), &quality_rounds);
    let expected = [
        ("version", Json::from(1)),
        ("run_id", Json::from(RUN_ID)),
        ("workflow", Json::from("default")),
        ("outcome", Json::from(APPROVE)),
        ("rounds", Json::from(2)),
        ("quality_gate", Json::Bool(true)),
        ("max_quality_rounds", Json::from(3)),
        ("pull_request", Json::from("https://github.com/o/r/pull/7")),
        ("restored_locked_files", Json::from(0)),
    ];
    for (key, value) in expected {
        assert_eq!(field(metrics, key), &value, "{key}");
    }
    let claude: Json = Json::Object([("harness", Json::from("claude")), ("model", Json::Null)].into_iter().collect());
    let roles: Map<Json> = [("spec", claude.clone()), ("build", claude.clone()), ("review", claude)].into_iter().collect();
    assert_eq!(field(metrics, "roles"), &Json::Object(roles));
    assert_eq!(field(metrics, "started_at"), &Json::from(at(0)));
    let ended = crate::summary::epoch_seconds(field(metrics, "ended_at").as_str().unwrap()).unwrap();
    assert_eq!(field(metrics, "seconds"), &Json::from(ended - HOST_EPOCH));
    assert_eq!(f.saved().get("quality_rounds"), Some(&quality_rounds));
}

#[test]
fn a_resumed_quality_round_is_recorded_once() {
    let mut state = quality_state(1);
    let recorded: Map<Json> = [
        ("round", Json::from(1)),
        ("quality_round", Json::from(1)),
        ("gate", Json::from("OK")),
        ("conditions", Json::Array(vec![])),
        ("fingerprints_by_check", Json::Object(Map::new())),
    ]
    .into_iter()
    .collect();
    state.quality_rounds = vec![recorded];
    let path = state.quality_path(1, 1);
    let f = Fixture::resume_gated(Rc::new(FakeHost::default()), state, vec![("review", vec![review_turn(1, APPROVE)])], &[]);
    f.put(&path, "GATE: OK\n");
    let mut wf = f.workflow(f.as_saved_gated());
    assert_eq!(wf.run(), verdict(APPROVE));
    assert_eq!(wf.state().quality_rounds.len(), 1);
}
