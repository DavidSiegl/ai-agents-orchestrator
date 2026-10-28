use super::*;
use crate::pipeline::DEFAULT_WORKFLOW;
use crate::test_support::helper_cases;
use crate::workflow_file::saved_pipeline;

#[test]
fn every_recorded_run_is_summed_up_as_the_python_engine_does() {
    let cases = helper_cases();
    assert!(cases.len() >= 40, "helpers.json holds {} runs", cases.len());
    for case in cases {
        let expected = |key: &str| case.expected.get(key).and_then(Json::as_str).map(String::from);
        let place = format!("{} {}", expected("recording").unwrap(), expected("run_dir").unwrap());
        let pipeline = saved_pipeline(&case.saved);
        let rounds: Vec<String> = (1..=case.state.round).map(|k| format!("Round {k}: as recorded")).collect();
        assert_eq!(pipeline.map(|p| run_summary(&case.state, &p, &rounds, case.now)), expected("run_summary"), "{place}");
        assert_eq!(Some(run_metrics(&case.state, case.now).dumps()), expected("run_metrics"), "{place}");
        let lines = Json::Array(turn_lines(&case.state.turns).into_iter().map(Json::from).collect());
        assert_eq!(Some(&lines), case.expected.get("turn_lines"), "{place}");
    }
}

#[test]
fn durations_and_ages() {
    for (seconds, text) in [(0, "0s"), (59, "59s"), (65, "1m 05s"), (3599, "59m 59s"), (3725, "1h 02m")] {
        assert_eq!(format_duration(seconds), text);
    }
    for (seconds, text) in [(0, "0s"), (59, "59s"), (65, "1m"), (3725, "1h2m")] {
        assert_eq!(format_age(seconds), text);
    }
}

#[test]
fn times_are_written_and_read_as_python_does() {
    assert_eq!(iso_utc(0), "1970-01-01T00:00:00+00:00");
    assert_eq!(iso_utc(1_791_000_000), "2026-10-03T04:00:00+00:00");
    assert_eq!(iso_utc(951_782_400), "2000-02-29T00:00:00+00:00");
    assert_eq!(iso_utc(-1), "1969-12-31T23:59:59+00:00");
    for seconds in [0, 1_791_000_000, 951_782_400, 4_102_444_799, -86_400] {
        assert_eq!(epoch_seconds(&iso_utc(seconds)), Ok(seconds));
    }
    assert_eq!(epoch_seconds("2026-10-03T06:00:00+02:00"), Ok(1_791_000_000));
    assert_eq!(epoch_seconds("2026-10-03T04:00:00.999Z"), Ok(1_791_000_000));
    assert_eq!(epoch_seconds("2026-10-03 04:00"), Ok(1_791_000_000));
    assert_eq!(epoch_seconds("2026-10-03"), Ok(1_790_985_600));
    assert_eq!(epoch_seconds("2026-10-03T04:00:00-0130"), Ok(1_791_005_400));
    // Midnight as hour 24, and any one character between the date and the time, as fromisoformat reads them.
    assert_eq!(epoch_seconds("2026-10-03T24:00:00"), Ok(1_791_072_000));
    assert_eq!(epoch_seconds("2026-10-03X04:00"), Ok(1_791_000_000));
    // int() of a time before the epoch with a fraction rounds toward it.
    assert_eq!(epoch_seconds("1969-12-31T23:59:59.5Z"), Ok(0));
    for bad in [
        "<time>",
        "garbled",
        "2026-13-01",
        "2026-02-30T00:00:00",
        "2026-10-03T24:00:01",
        "2026-1-030",
        "2026-10-03T04:00:00+2:00",
    ] {
        assert!(epoch_seconds(bad).is_err(), "{bad}");
    }
}

/// A turn record that started and ended this many seconds after 2026-10-03T04:00:00, as tests/test_orchestrator.py's
/// turn() makes one.
fn turn(step: &str, file: &str, start: Option<i64>, end: Option<i64>, change: impl FnOnce(&mut Map<Json>)) -> Map<Json> {
    let at = |s: Option<i64>| Json::from(s.map(|s| iso_utc(1_791_000_000 + s)));
    let mut t: Map<Json> = [
        ("step", Json::from(step)), ("role", Json::from(step)), ("round", Json::from(1)), ("quality_round", Json::from(0)),
        ("file", Json::from(file)), ("agent", Json::from("claude")), ("model", Json::Null), ("human_paced", Json::Bool(false)),
        ("started_at", at(start)), ("ended_at", at(end)),
        ("seconds", Json::from(start.zip(end).map(|(s, e)| e - s))),
    ].into_iter().collect();
    change(&mut t);
    t
}

#[test]
fn a_pipe_in_a_cell_does_not_split_it() {
    let table = turns_table(&[turn("build", "a|b.md", Some(0), Some(5), |t| t.insert("model", Json::from("x|y")))]);
    assert!(table.ends_with("| build | build | 1 | claude | x\\|y | 2026-10-03 04:00:00 | 5s | a\\|b.md |"), "{table}");
}

const GOLDEN_SUMMARY: &str = "\
# Run 20260929-120000-a1b2c3

- **Workflow:** default
- **Outcome:** APPROVE
- **Pull request:** https://github.com/o/r/pull/7
- **Branch:** `orchestrator/add-a-token-bucket-rate-limiter-a1b2c3`
- **Wall time:** 35m 00s, from the first turn's start to 2026-10-03T04:35:00+00:00

## Task

add a rate limiter

## Roles

| role | harness | model |
| --- | --- | --- |
| Spec Collector (spec) | claude | default |
| Builder (build) | codex | default |
| Reviewer (review) | claude | opus |

## Turns

| step | role | round | harness | model | start (UTC) | duration | file |
| --- | --- | --- | --- | --- | --- | --- | --- |
| spec (human-paced) | spec | 0 | claude | default | 2026-10-03 04:00:00 | 10m 00s | spec.md |
| build | build | 1 | codex | default | 2026-10-03 04:10:00 | 20m 00s | build-1.md |
| review | review | 1 | claude | opus | 2026-10-03 04:30:00 | 5m 00s | review-1.md |

## Rounds

- Round 1: review APPROVE
";

fn approved_run() -> RunState {
    let mut s = RunState::new("20260929-120000-a1b2c3", "add a rate limiter", "/proj");
    s.verdict = Some("APPROVE".into());
    s.pr_url = Some("https://github.com/o/r/pull/7".into());
    s.branch = Some("orchestrator/add-a-token-bucket-rate-limiter-a1b2c3".into());
    s.round = 1;
    for (role, kind) in [("spec", "claude"), ("build", "codex"), ("review", "claude")] {
        s.agents.insert(role, [("name", format!("{role}-a1b2c3")), ("pane", "p1".into()), ("kind", kind.into())].into_iter().collect());
    }
    s.models.insert("review", "opus".to_string());
    s.turns = vec![
        turn("spec", "spec.md", Some(0), Some(600), |t| { t.insert("round", Json::from(0)); t.insert("human_paced", Json::Bool(true)); }),
        turn("build", "build-1.md", Some(600), Some(1800), |t| { t.insert("agent", Json::from("codex")); }),
        turn("review", "review-1.md", Some(1800), Some(2100), |t| { t.insert("model", Json::from("opus")); }),
    ];
    s
}

#[test]
fn the_summary_of_an_approved_run() {
    let summary = run_summary(&approved_run(), &DEFAULT_WORKFLOW, &["Round 1: review APPROVE".to_string()], 1_791_000_000 + 2100);
    assert_eq!(summary, GOLDEN_SUMMARY);
}

#[test]
fn a_run_without_turns_or_an_end_says_so() {
    let mut s = RunState::new("r-a1b2c3", "  t \n", "/proj");
    s.error = Some("the Builder did not write it".into());
    let summary = run_summary(&s, &DEFAULT_WORKFLOW, &[], 0);
    assert!(summary.contains("- **Outcome:** error: the Builder did not write it\n- **Pull request:** none\n- **Branch:** none\n"));
    assert!(summary.contains("- **Wall time:** ?, from the first turn's start to 1970-01-01T00:00:00+00:00\n"));
    assert!(summary.contains("## Task\n\nt\n") && summary.ends_with("## Turns\n\nNo turns were recorded.\n"));
    s.error = None;
    s.agent_kinds.insert("build", "pi".to_string());
    let summary = run_summary(&s, &DEFAULT_WORKFLOW, &[], 0);
    assert!(summary.contains("- **Outcome:** unfinished\n") && summary.contains("| Builder (build) | pi | default |"));
    let metrics = run_metrics(&s, 10);
    assert_eq!(metrics.get("outcome"), Some(&Json::from("unfinished")));
    assert_eq!(metrics.get("started_at"), Some(&Json::Null));
    assert_eq!(metrics.get("max_quality_rounds"), Some(&Json::Null));
}

#[test]
fn turns_with_unknown_starts_and_ends_and_quality_rounds() {
    let open = turn("build", "build-1.md", None, None, |_| {});
    let analysis = turn("quality", "quality-1-1.md", Some(0), Some(65), |t| {
        t.insert("agent", Json::Null);
        t.insert("quality_round", Json::from(1));
    });
    let lines = turn_lines(&[open, analysis.clone()]);
    assert_eq!(lines, ["build    1     claude  ?                    ?       build-1.md", "quality  1 q1  -       2026-10-03 04:00:00  1m 05s  quality-1-1.md"]);
    let mut s = approved_run();
    s.turns = vec![analysis];
    s.restored = vec![Json::parse(r#"{"round": 1, "file": "locked-build-1.diff", "paths": {"tests": ["a", "b"], "x": ["c"]}}"#).unwrap().as_object().unwrap().clone()];
    let metrics = run_metrics(&s, 1_791_000_100);
    assert_eq!(metrics.get("restored_locked_files"), Some(&Json::from(3)));
    assert_eq!(metrics.get("seconds"), Some(&Json::from(100)));
    let turns = metrics.get("turns").and_then(Json::as_array).unwrap();
    assert_eq!(turns[0].get("tokens"), Some(&Json::Null));
}
