use super::*;
use crate::py::json::Json;
use crate::test_support::{oracle, Rng};

fn case_text(case: &Json, key: &str) -> String {
    case.get(key).and_then(Json::as_str).unwrap_or_else(|| panic!("no {key}")).to_string()
}

/// The quality file of quality.json named from.
fn quality_file(from: &str) -> String {
    let quality = oracle("quality.json");
    let files = quality.get("files").and_then(Json::as_array).unwrap();
    let file = files.iter().find(|f| f.get("from").and_then(Json::as_str) == Some(from)).unwrap_or_else(|| panic!("no {from}"));
    case_text(file, "text")
}

fn fingerprints_json(failures: &GateFailures) -> String {
    let map = failures.fingerprints.iter().map(|(k, v)| (k, Json::Array(v.iter().map(|f| Json::from(f.as_str())).collect())));
    Json::Object(map.collect()).dumps()
}

#[test]
fn every_quality_file_reads_as_the_python_engine_reads_it() {
    let quality = oracle("quality.json");
    let files = quality.get("files").and_then(Json::as_array).unwrap();
    assert!(files.len() >= 25, "quality.json holds {} files", files.len());
    for file in files {
        let (from, text) = (case_text(file, "from"), case_text(file, "text"));
        assert_eq!(parse_gate(&text), file.get("gate").and_then(Json::as_str), "{from}");
        let failures = gate_failures(&text);
        let conditions = Json::Array(failures.conditions.iter().map(|c| Json::from(c.as_str())).collect());
        assert_eq!(conditions.dumps(), file.get("conditions").unwrap().dumps(), "{from}");
        assert_eq!(fingerprints_json(&failures), file.get("fingerprints").unwrap().dumps(), "{from}");
    }
}

fn leak(value: &Json) -> Leak {
    let field = |key: &str| case_text(value, key);
    Leak {
        rule: field("rule"),
        file: field("file"),
        line: value.get("line").and_then(Json::as_int).unwrap() as u64,
        commit: field("commit"),
        fingerprint: field("fingerprint"),
    }
}

#[test]
fn the_engines_quality_files_are_written_as_the_python_engine_writes_them() {
    let quality = oracle("quality.json");
    for case in quality.get("leak_reports").and_then(Json::as_array).unwrap() {
        let leaks: Vec<Leak> = case.get("leaks").and_then(Json::as_array).unwrap().iter().map(leak).collect();
        assert_eq!(leak_report(&leaks, &case_text(case, "intro")), case_text(case, "text"));
    }
    for case in quality.get("build_failed").and_then(Json::as_array).unwrap() {
        let tests: Option<Vec<(String, String)>> = case.get("tests").and_then(Json::as_array).map(|tests| {
            tests.iter().map(|t| {
                let pair = t.as_array().unwrap();
                (pair[0].as_str().unwrap().to_string(), pair[1].as_str().unwrap().to_string())
            }).collect()
        });
        let text = build_failed_report(&case_text(case, "intro"), tests.as_deref(), &case_text(case, "console"));
        assert_eq!(text, case_text(case, "text"));
    }
    for case in quality.get("one_line").and_then(Json::as_array).unwrap() {
        let pair = case.as_array().unwrap();
        assert_eq!(one_line(pair[0].as_str().unwrap()), pair[1].as_str().unwrap());
    }
    for case in quality.get("fingerprints").and_then(Json::as_array).unwrap() {
        let c = case.as_array().unwrap();
        assert_eq!(finding_fingerprint(c[0].as_str(), c[1].as_str().unwrap(), c[2].as_str().unwrap()), c[3].as_str().unwrap());
    }
}

/// LEAK of tests/test_orchestrator.py, with changes.
fn a_leak(change: impl FnOnce(&mut Leak)) -> Leak {
    let mut leak = Leak {
        rule: "github-pat".into(),
        file: "limiter.py".into(),
        line: 3,
        commit: "c0ffee".repeat(6) + "c0ff",
        fingerprint: "c0ffee:limiter.py:github-pat:3".into(),
    };
    change(&mut leak);
    leak
}

#[test]
fn reads_the_secrets_of_a_leak_file_whichever_snapshot_and_line() {
    let report = |leaks: &[Leak]| gate_failures(&leak_report(leaks, "found."));
    let first = report(&[a_leak(|l| l.commit = "snap1".into())]);
    let moved = a_leak(|l| {
        l.line = 9;
        l.commit = "snap2".into();
        l.fingerprint = "snap2:limiter.py:github-pat:9".into();
    });
    let pat = finding_fingerprint(Some("limiter.py"), "gitleaks github-pat", "");
    assert_eq!(fingerprints_json(&first), format!("{{\"gitleaks\": [\"{pat}-1\"]}}"));
    assert!(report(std::slice::from_ref(&moved)).repeats(&first));
    let other_rule = Leak { rule: "aws-access-token".into(), ..moved.clone() };
    assert!(!report(&[other_rule]).repeats(&first));
    // Of a file's two keys of a kind, one removed is progress, not the same again.
    let two = report(&[a_leak(|l| l.commit = "snap1".into()), moved.clone()]);
    assert!(!report(std::slice::from_ref(&moved)).repeats(&two));
    assert!(report(&[moved, a_leak(|l| l.commit = "snap3".into())]).repeats(&two));
}

#[test]
fn a_coverage_condition_repeated_is_stuck_until_its_words_change() {
    let fifty = gate_failures(&quality_file("coverage 50.0"));
    let sixty = gate_failures(&quality_file("coverage 60.0"));
    assert!(fifty.repeats(&gate_failures(&quality_file("coverage 50.0"))));
    assert!(!sixty.repeats(&fifty));
    assert!(fifty.fingerprints.is_empty());
}

#[test]
fn reads_failing_findings_and_tests_and_leaves_findings_to_weigh_out() {
    let failures = gate_failures(&quality_file("findings, tests and findings to weigh"));
    assert_eq!(
        failures.conditions,
        ["1 tests fail; each is listed below.", "1 findings of the `diff-budget` check; each is listed below."]
    );
    let fp = |path: Option<&str>, rule: &str, message: &str| Json::Array(vec![Json::from(finding_fingerprint(path, rule, message))]);
    let expected: Map<Json> = [
        ("ruff", fp(Some("a.py"), "ruff F401", "`os` unused")),
        ("mypy", fp(Some("b.py"), "mypy", "bad: 2 errors")),
        ("tests", fp(Some("tests.test_a.T.test_x"), "tests", "")),
        ("diff-budget", fp(None, "diff-budget", "too big: 900 lines")),
    ]
    .into_iter()
    .collect();
    assert_eq!(Json::parse(&fingerprints_json(&failures)).unwrap(), Json::Object(expected));
}

#[test]
fn a_fingerprint_ignores_digits_and_keeps_paths_and_rules_apart() {
    let fp = finding_fingerprint;
    assert_eq!(fp(Some("a.py"), "ruff C901", "too complex (16 > 15)"), fp(Some("a.py"), "ruff C901", "too complex (17 > 15)"));
    assert_ne!(fp(Some("a.py"), "ruff C901", "m"), fp(Some("b.py"), "ruff C901", "m"));
    assert_ne!(fp(Some("a.py"), "ruff C901", "m"), fp(Some("a.py"), "ruff E501", "m"));
    assert_eq!(fp(None, "x", "y").len(), 16);
}

#[test]
fn nothing_failed_repeats_nothing() {
    let ok = gate_failures(&quality_file("ok"));
    assert_eq!(ok, GateFailures::default());
    assert!(!ok.repeats(&ok));
}

#[test]
fn build_failed_reports() {
    let text = build_failed_report("intro", None, "last\nlines");
    assert!(text.starts_with("GATE: BUILD_FAILED\n\nintro\n"));
    assert!(text.contains("## Failing tests\n\nJenkins has no test report for this build.\n"));
    assert!(text.contains(&format!("## The last {CONSOLE_TAIL_LINES} lines of the build's console\n\n````\nlast\nlines\n````\n")));
    assert!(!text.contains("Issues on changed lines") && !text.contains("Findings to weigh"));
    let tests = [("t.T.test_x".to_string(), "AssertionError\ntraceback".to_string())];
    let text = build_failed_report("intro", Some(&tests), "");
    assert!(text.contains("## Failing tests\n\n- `t.T.test_x`: AssertionError\n"));
    assert!(!text.contains("console"));
    assert!(build_failed_report("intro", Some(&[]), "").contains("## Failing tests\n\nNone.\n"));
    let tests: Vec<(String, String)> = (0..QUALITY_FINDINGS_LIMIT + 3).map(|k| (format!("t.T.test_{k}"), String::new())).collect();
    let text = build_failed_report("intro", Some(&tests), "");
    assert!(text.contains("- `t.T.test_0`: failed\n") && text.contains("- and 3 more.\n"));
    assert!(!text.contains(&format!("test_{QUALITY_FINDINGS_LIMIT}`")));
}

#[test]
fn read_back_the_gate_of_a_build_failed_report() {
    let mut rng = Rng::new(20);
    for _ in 0..200 {
        let (intro, console) = (rng.text(20, ""), rng.text(20, ""));
        assert_eq!(parse_gate(&build_failed_report(&intro, None, &console)), Some(QUALITY_BUILD_FAILED));
    }
}

#[test]
fn lines_that_only_look_like_findings_are_not_read_as_them() {
    for line in [
        "1. `a.py:3` error", "1. `a.py:x` error r: m", "x. `a.py:3` error r: m", "1. `` error r: m",
        "1. `a.py:3` error : m", "1.`a.py:3` error r: m", "1. `a.py:3`  error r: m",
    ] {
        assert!(numbered_finding(line).is_none(), "{line}");
    }
    let found = numbered_finding("12. `a`b.py:3` (the whole file)` error ruff X: y: z").unwrap();
    // The shortest path, as Python's re reads it.
    assert_eq!((found.path, found.rule, found.message), (Some("a`b.py"), "whole file)` error ruff X", "y: z"));
    assert_eq!(failing_test("- `a`: b`: c"), Some("a`: b"));
    assert_eq!(failing_test("- ``: c"), None);
    assert_eq!(secret_found("1. `a:b.py:3` r in c (fingerprint `x`)"), Some(("a:b.py", "r")));
    for line in ["1. `a.py:3` r in c (fingerprint `x`", "1. `a.py:` r in c (fingerprint `x`)", "1. `a.py:3` r in  (fingerprint `x`)"] {
        assert_eq!(secret_found(line), None, "{line}");
    }
}
