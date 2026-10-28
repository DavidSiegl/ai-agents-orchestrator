//! tests/test_orchestrator.py's TestJudgeRun (its quality files written by gate.py, in checks.json), TestDiffBudget,
//! and TestOrchestratorChecks without the Workflow that runs them in a quality round; and judge_run on every quality
//! file the parity recordings hold.

use std::cell::RefCell;
use std::sync::Arc;

use super::*;
use crate::ci::fake::Outcome;
use crate::http::Http;
use crate::patch::added_lines;
use crate::pipeline::default_workflow;
use crate::prompts::Prompts;
use crate::py::json::Json;
use crate::pypi::fake::{FakePyPI, ProjectJson};
use crate::quality::parse_gate;
use crate::test_support::{oracle, quick, replaced};

/// 2026-10-23, the day the young-package tests run on.
pub(crate) const TODAY: f64 = 1_792_713_600.0;
pub(crate) const SPEC_NAMING_LIMITS: &str =
    "# Add a token-bucket rate limiter\n\n## Goal\nThrottle the client with the `Limits` package, declared in \
        pyproject.toml.\n";

/// A pyproject.toml depending on httpx, then on requirements, from line 5 on.
pub(crate) fn pyproject_with(requirements: &[&str]) -> String {
    let deps: String = ["httpx>=0.27"].iter().chain(requirements).map(|r| format!("    \"{r}\",\n")).collect();
    format!("[project]\nname = \"limiter\"\ndependencies = [\n{deps}]\n")
}

/// `git diff -U0` of pyproject_with(&[]) becoming pyproject_with(requirements).
pub(crate) fn pyproject_diff(requirements: &[&str]) -> String {
    let added: String = requirements.iter().map(|r| format!("+    \"{r}\",\n")).collect();
    format!(
        "diff --git a/pyproject.toml b/pyproject.toml\nindex 1111111..2222222 100644\n--- a/pyproject.toml\n+++ \
            b/pyproject.toml\n\
         @@ -4,0 +5,{} @@ dependencies = [\n{added}",
        requirements.len()
    )
}

/// The pyproject.toml of base and of new, without and with requirements.
pub(crate) fn pyproject_files(requirements: &[&str]) -> Vec<(&'static str, &'static str, Option<String>)> {
    vec![
        ("base", "pyproject.toml", Some(pyproject_with(&[]))),
        ("new", "pyproject.toml", Some(pyproject_with(requirements))),
    ]
}

pub(crate) fn new_file_diff(path: &str, lines: usize) -> String {
    let added: String = (0..lines).map(|i| format!("+line {i}\n")).collect();
    format!(
        "diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ \
        b/{path}\n@@ -0,0 +1,{lines} @@\n{added}"
    )
}

pub(crate) fn changed(base_path: &str, path: &str, added: u64, removed: u64) -> ChangedFile {
    ChangedFile { base_path: base_path.to_string(), path: path.to_string(), added, removed }
}

/// The change of a quality round from base to new, as Python's run_change makes it: its diff, each file's text by
/// (commit, path), its spec, and its ChangedFiles, by default each file the diff adds lines to, under its own path,
/// with those lines and none removed.
pub(crate) struct Change {
    pub diff: String,
    pub files: Vec<(&'static str, String, Option<String>)>,
    pub spec: String,
    pub counts: Option<Vec<ChangedFile>>,
}

impl Change {
    pub fn new(diff: &str) -> Change {
        Change { diff: diff.to_string(), files: Vec::new(), spec: SPEC_NAMING_LIMITS.to_string(), counts: None }
    }

    pub fn files(mut self, files: Vec<(&'static str, &str, Option<String>)>) -> Change {
        self.files = files.into_iter().map(|(commit, path, text)| (commit, path.to_string(), text)).collect();
        self
    }

    pub fn spec(mut self, spec: &str) -> Change {
        self.spec = spec.to_string();
        self
    }

    pub fn counts(mut self, counts: Vec<ChangedFile>) -> Change {
        self.counts = Some(counts);
        self
    }

    /// f's answer for the RunChange on TODAY, its in_time answering with in_time.
    pub fn run<R>(&self, in_time: &dyn Fn() -> bool, f: impl FnOnce(&RunChange) -> R) -> R {
        let added = added_lines(self.diff.as_bytes()).unwrap();
        let changed = added.iter().map(|(path, lines)| (path.to_string(), lines.keys().copied().collect())).collect();
        let files = self
            .counts
            .clone()
            .unwrap_or_else(|| added.iter().map(|(path, lines)| changed_file(path, lines.len())).collect());
        let file_at = |commit: &str, path: &str| {
            Ok(self
                .files
                .iter()
                .find(|(c, p, _)| *c == commit && p == path)
                .and_then(|(_, _, text)| text.clone())
                .map(String::into_bytes))
        };
        let change = RunChange {
            spec: self.spec.clone(),
            base: "base".into(),
            commit: "new".into(),
            changed,
            files,
            file_at: &file_at,
            now: TODAY,
            in_time,
        };
        f(&change)
    }
}

fn changed_file(path: &str, lines: usize) -> ChangedFile {
    changed(path, path, lines as u64, 0)
}

pub(crate) fn always() -> bool {
    true
}

fn finding(json: &Json) -> Finding {
    let text = |k: &str| json.get(k).and_then(Json::as_str).unwrap_or_default().to_string();
    Finding {
        path: json.get("path").and_then(Json::as_str).map(str::to_string),
        line: json.get("line").and_then(Json::as_int).map(|n| n as u64),
        severity: if text("severity") == "error" { Severity::Error } else { Severity::Warning },
        rule: text("rule"),
        message: text("message"),
    }
}

fn int(json: &Json, key: &str) -> i64 {
    json.get(key).and_then(Json::as_int).unwrap() as i64
}

#[test]
fn judge_run_adds_the_checks_findings_as_the_python_engine_does() {
    for case in oracle("checks.json").get("judge_run").and_then(Json::as_array).unwrap() {
        let checks: Vec<OrchestratorCheck> = case
            .get("checks")
            .and_then(Json::as_array)
            .unwrap()
            .iter()
            .map(|c| {
                let list = |k: &str| c.get(k).and_then(Json::as_array).unwrap().iter().map(finding).collect::<Vec<_>>();
                let found = CheckFindings { issues: list("issues"), to_weigh: list("to_weigh") };
                let name: &'static str =
                    Box::leak(c.get("name").and_then(Json::as_str).unwrap().to_string().into_boxed_str());
                OrchestratorCheck { name, find: Box::new(move |_| Ok(found.clone())) }
            })
            .collect();
        let quality = case.get("quality").and_then(Json::as_str).unwrap();
        let out = Change::new("").run(&always, |change| judge_run(quality, &checks, change).unwrap());
        assert_eq!(out, case.get("out").and_then(Json::as_str).unwrap(), "{quality:?}");
    }
}

#[test]
fn judge_run_on_every_recorded_quality_file_gives_the_recorded_result() {
    let cases = oracle("checks.json");
    let cases = cases.get("recorded").and_then(Json::as_array).unwrap();
    assert!(cases.len() >= 8, "the recordings' judged quality files");
    for case in cases {
        let text = |k: &str| case.get(k).and_then(Json::as_str).unwrap().to_string();
        let mut files = Vec::new();
        for (commit, key) in [("base", "base_files"), ("new", "new_files")] {
            for (path, text) in case.get(key).and_then(Json::as_object).unwrap().iter() {
                files.push((commit, path.to_string(), text.as_str().map(str::to_string)));
            }
        }
        let counts = case
            .get("counts")
            .and_then(Json::as_array)
            .unwrap()
            .iter()
            .map(|c| {
                let c = c.as_array().unwrap();
                changed(
                    c[0].as_str().unwrap(),
                    c[1].as_str().unwrap(),
                    c[2].as_int().unwrap() as u64,
                    c[3].as_int().unwrap() as u64,
                )
            })
            .collect();
        let change = Change { diff: text("diff"), files, spec: text("spec"), counts: Some(counts) };
        let pypi = PyPI::new(FakePyPI::from_json(case.get("pypi").and_then(Json::as_object).unwrap().clone()));
        let pipeline = replaced(&default_workflow(&Prompts::default()), |s| {
            s.max_files = int(case, "max_files");
            s.max_lines = int(case, "max_lines");
        })
        .unwrap();
        let out = change.run(&always, |change| {
            judge_run(&text("quality"), &orchestrator_checks(&pipeline, &pypi), change).unwrap()
        });
        assert_eq!(out, text("out"), "{}", text("from"));
    }
}

#[test]
fn the_budget_counts_files_and_lines_against_the_workflows() {
    let change = Change::new("").counts(vec![
        changed("a.py", "a.py", 10, 0),
        changed("b.py", "b.py", 10, 0),
        changed("old.py", "c.py", 1, 5),
    ]);
    change.run(&always, |c| {
        assert_eq!(diff_budget(c, 3, 26), CheckFindings::default());
        assert_eq!(
            diff_budget(c, 2, 26).to_weigh[0].message,
            "The change touches 3 files, over the workflow's \
            max_files of 2. Say in your report why it cannot be smaller."
        );
        assert_eq!(
            diff_budget(c, 3, 25).to_weigh[0].message,
            "The change touches 26 lines (21 added, 5 removed), over \
            the workflow's max_lines of 25. Say in your report why it cannot be smaller."
        );
        assert_eq!(
            diff_budget(c, 2, 25).to_weigh[0].message,
            "The change touches 3 files, \
            over the workflow's max_files of 2, and 26 lines (21 added, 5 removed), over its max_lines of 25. \
            Say in your report why it cannot be smaller."
        );
    });
}

#[test]
fn a_path_the_spec_mentions_by_path_or_name_is_expected() {
    let diff = new_file_diff("src/limiter.py", 1)
        + &new_file_diff("tests/test_limiter.py", 1)
        + &new_file_diff("README.md", 1);
    let found =
        Change::new(&diff).spec("Add src/limiter.py, tested in test_limiter.py.").run(&always, unexpected_paths);
    assert!(found.issues.is_empty());
    assert_eq!(found.to_weigh.iter().map(|f| f.path.clone().unwrap()).collect::<Vec<_>>(), ["README.md"]);
}

#[test]
fn checks_come_from_the_workflow() {
    let p = replaced(&quick(), |s| {
        s.max_files = 3;
        s.max_lines = 40;
    })
    .unwrap();
    let pypi = PyPI::new(FakePyPI::with(&[]));
    let checks = orchestrator_checks(&p, &pypi);
    assert_eq!(
        checks.iter().map(|c| c.name).collect::<Vec<_>>(),
        ["new-dependencies", "diff-budget", "unexpected-paths"]
    );
    let found = Change::new(&new_file_diff("README.md", 41)).run(&always, |c| (checks[1].find)(c).unwrap());
    assert!(found.to_weigh[0].message.contains("over the workflow's max_lines of 40"));
    let paths = Change::new(&new_file_diff("README.md", 1)).run(&always, |c| (checks[2].find)(c).unwrap());
    assert_eq!(paths.to_weigh.len(), 1);
}

#[test]
fn a_check_s_error_fails_judge_run() {
    let failing = OrchestratorCheck { name: "c", find: Box::new(|_| Err(OrchestratorError::new("git failed"))) };
    let e = Change::new("").run(&always, |c| judge_run("GATE: OK\n", &[failing], c).unwrap_err());
    assert_eq!(e, OrchestratorError::new("git failed"));
}

#[test]
fn severities_are_written_as_the_gate_writes_them() {
    assert_eq!((Severity::Error.as_str(), Severity::Warning.as_str()), ("error", "warning"));
    assert_eq!(Finding::error(Some("a.py"), Some(3), "r", "m".to_string()).numbered(7), "7. `a.py:3` error r: m");
    assert_eq!(
        Finding::warning(Some("a.py"), None, "r", "m".into()).numbered(1),
        "1. `a.py (the whole file)` warning r: m"
    );
}

// -- TestOrchestratorChecks: the checks of a quality round on the build's quality file -------------------------------

/// The quality file of a quality round whose build played build and whose snapshot holds change, as the default
/// workflow's orchestrator_checks judge it with pypi, on TODAY.
fn quality(change: &Change, pypi: &Arc<FakePyPI>, build: Outcome) -> String {
    let pipeline = default_workflow(&Prompts::default());
    let pypi = PyPI::new(Arc::clone(pypi) as Arc<dyn Http>);
    change.run(&always, |c| judge_run(&build.quality.unwrap(), &orchestrator_checks(&pipeline, &pypi), c).unwrap())
}

fn limits() -> Arc<FakePyPI> {
    FakePyPI::with(&[("limits", ProjectJson::default())])
}

fn ok() -> Outcome {
    Outcome::gate("OK", &[])
}

fn red() -> Outcome {
    Outcome::gate("ERROR", &["`limiter.py:3` error ruff F401: `os` imported but unused"])
}

fn adding(requirements: &[&str]) -> Change {
    Change::new(&pyproject_diff(requirements)).files(pyproject_files(requirements))
}

#[test]
fn a_dependency_the_spec_names_passes_with_its_metadata() {
    let pypi = limits();
    let q = quality(&adding(&["limits>=3"]), &pypi, ok());
    assert!(q.starts_with("GATE: OK\n"), "{q}");
    assert!(!q.contains("## Failed conditions"));
    assert!(q.contains("\n## The orchestrator's checks\n"));
    assert!(q.contains("These do not fail the gate."));
    assert!(
        q.contains(
            "\n1. `pyproject.toml:5` warning new-dependencies added: `limits`, which the spec names: on \
        PyPI since 2015-01-03, latest 3.13.0 of 2024-06-22, license MIT.\n"
        ),
        "{q}"
    );
    assert_eq!(pypi.state().asked, ["limits"]);
}

#[test]
fn a_dependency_the_spec_does_not_name_fails() {
    let pypi = FakePyPI::with(&[(
        "requests",
        ProjectJson { first: "2011-02-14", version: "2.32.3", license: Some("Apache-2.0"), ..ProjectJson::default() },
    )]);
    let q = quality(&adding(&["requests>=2"]), &pypi, ok());
    assert!(q.starts_with("GATE: ERROR\n"), "{q}");
    assert!(q.contains(
        "## Failed conditions\n\n- 1 findings of the `new-dependencies` check; each is listed \
        below.\n\n## Issues on changed lines\n"
    ));
    assert!(
        q.contains(
            "\n1. `pyproject.toml:5` error new-dependencies not-in-spec: `requests` is a new dependency \
        that the spec does not name; on PyPI since 2011-02-14, latest 2.32.3 of \
        2024-06-22, license Apache-2.0.\n"
        ),
        "{q}"
    );
    assert_eq!(parse_gate(&q), Some("ERROR"));
}

#[test]
fn a_failing_finding_is_numbered_after_the_builds_own() {
    let q = quality(&adding(&["requests>=2"]), &limits(), red());
    assert!(q.starts_with("GATE: ERROR\n"));
    assert!(q.contains("1. `limiter.py:3` error ruff F401"));
    assert!(
        q.contains(
            "\n2. `pyproject.toml:5` error new-dependencies not-in-spec: `requests` is a new dependency \
        that the spec does not name; PyPI does not know it either.\n"
        ),
        "{q}"
    );
}

#[test]
fn a_dependency_pypi_does_not_know_fails() {
    let pypi = limits();
    let q = quality(&adding(&["limitz"]).spec("# Add a rate limiter\n\nUse the `limitz` package.\n"), &pypi, ok());
    assert!(q.starts_with("GATE: ERROR\n"));
    assert!(q.contains(
        "1. `pyproject.toml:5` error new-dependencies not-on-pypi: PyPI does not know `limitz`: \
        the name may be mistyped, or name a package that does not exist yet, which anyone \
        could register (slopsquatting).\n"
    ));
    assert_eq!(pypi.state().asked, ["limitz"]);
}

#[test]
fn a_pypi_timeout_is_unchecked_never_passed() {
    let pypi = limits();
    pypi.state().down = true;
    let q = quality(&adding(&["limits>=3"]), &pypi, ok());
    assert!(q.starts_with("GATE: ERROR\n"));
    assert!(
        q.contains(
            "1. The change as a whole: error new-dependencies unchecked: PyPI could not be asked about \
        `limits`: GET https://pypi.org/pypi/limits/json: timed out; whether they exist is not known.\n"
        ),
        "{q}"
    );
    assert!(!q.contains("which the spec names"));
}

#[test]
fn a_dependency_younger_than_fourteen_days_is_flagged_not_failed() {
    let pypi = FakePyPI::with(&[(
        "limits",
        ProjectJson { first: "2026-10-20", latest: "2026-10-21", version: "0.2", ..ProjectJson::default() },
    )]);
    let q = quality(&adding(&["limits"]), &pypi, ok());
    assert!(q.starts_with("GATE: OK\n"));
    assert!(
        q.contains(
            "1. `pyproject.toml:5` warning new-dependencies young: \
        `limits`, which the spec names: on PyPI since 2026-10-20, latest 0.2 of 2026-10-21, license MIT; \
        it is younger than 14 days, so few have used it yet.\n"
        ),
        "{q}"
    );
}

#[test]
fn the_budget_and_unexpected_paths_are_reported_and_never_change_the_gate() {
    let diff: String = (0..16).map(|i| new_file_diff(&format!("src/m{i}.py"), 40)).collect();
    for (build, word) in [(ok(), "OK"), (red(), "ERROR")] {
        let pypi = limits();
        let q = quality(&Change::new(&diff), &pypi, build);
        assert!(q.starts_with(&format!("GATE: {word}\n")));
        assert!(!q.contains("## Failed conditions"));
        let first = if word == "ERROR" { 2 } else { 1 };
        assert!(
            q.contains(&format!(
                "\n{first}. The change as a whole: warning diff-budget: \
            The change touches 16 files, over the workflow's max_files of 15, and 640 lines (640 added, 0 removed), \
            over its max_lines of 600. Say in your report why it cannot be smaller.\n"
            )),
            "{q}"
        );
        assert!(q.contains(&format!(
            "\n{}. `src/m0.py (the whole file)` warning unexpected-paths: the spec mentions \
            neither this path nor its file name.\n",
            first + 1
        )));
        assert!(q.contains(&format!("\n{}. `src/m9.py (the whole file)`", first + 16)));
        assert!(pypi.state().asked.is_empty());
    }
}

#[test]
fn a_change_the_checks_find_nothing_in_keeps_the_builds_file() {
    let built = ok().quality.unwrap();
    assert_eq!(quality(&Change::new(""), &limits(), ok()), built);
}

#[test]
fn slow_pypi_stops_asking_at_the_rounds_deadline_and_keeps_the_heartbeat_between_asks() {
    let pypi = FakePyPI::with(&[
        ("limits", ProjectJson::default()),
        ("rich", ProjectJson::default()),
        ("attrs", ProjectJson::default()),
    ]);
    let change =
        adding(&["limits", "rich", "attrs"]).spec("# Throttle\n\nUse limits, rich and attrs, in pyproject.toml.\n");
    // Each request takes half the round's time, so the third would start too late; in_time beats the heartbeat.
    let beats = RefCell::new(Vec::new());
    let in_time = || {
        let asked = pypi.state().asked.len();
        beats.borrow_mut().push(asked);
        asked < 2
    };
    let pipeline = default_workflow(&Prompts::default());
    let index = PyPI::new(Arc::clone(&pypi) as Arc<dyn Http>);
    let q = change
        .run(&in_time, |c| judge_run(&ok().quality.unwrap(), &orchestrator_checks(&pipeline, &index), c).unwrap());
    assert_eq!(pypi.state().asked, ["limits", "rich"]);
    assert_eq!(*beats.borrow(), [0, 1, 2], "a beat before each ask");
    assert!(
        q.contains(
            "unchecked: PyPI could not be asked about `attrs`: the quality round had too little time left to ask;"
        ),
        "{q}"
    );
    assert!(q.starts_with("GATE: ERROR\n"));
}

#[test]
fn the_spec_may_be_the_runs_task() {
    let q = quality(&adding(&["limits"]).spec("limit requests with limits in pyproject.toml"), &limits(), ok());
    assert!(q.starts_with("GATE: OK\n"), "{q}");
    assert!(q.contains("new-dependencies added: `limits`, which the spec names"));
}
