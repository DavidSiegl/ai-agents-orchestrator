//! What the engine's unit tests share: a seeded generator of test inputs, in place of Python's Hypothesis, and a
//! temporary directory.

use std::path::PathBuf;

/// A deterministic stream of pseudo-random numbers (xorshift64*), so that a generated case that fails fails again.
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }

    pub fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// A number below n, which must not be 0.
    pub fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }

    pub fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[self.below(items.len())]
    }

    /// Text of up to max characters, none of them in excluded nor a NUL: mostly ASCII, with the characters that
    /// Python's and sh's notions of a line, a blank and a word disagree on mixed in.
    pub fn text(&mut self, max: usize, excluded: &str) -> String {
        const ODD: &[char] = &[
            '\n', '\r', '\t', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x1f', '\u{85}', '\u{a0}', '\u{2028}',
            '\u{2029}', '\u{3000}', '\u{200b}', 'é', 'ß', '😀', '\u{0301}', '\u{0663}', '_', '#', '=', '\'', '"', '\\',
            '$', '`', '~', '{', '}', ':', '*', '>',
        ];
        let len = self.below(max + 1);
        let mut out = String::new();
        while out.chars().count() < len {
            let c = if self.below(3) == 0 {
                *self.pick(ODD)
            } else {
                char::from(b' ' + self.below(95) as u8)
            };
            if c != '\0' && !excluded.contains(c) {
                out.push(c);
            }
        }
        out
    }
}

/// A directory of its own under the system's temporary directory, removed with everything in it when dropped.
pub struct TempDir(PathBuf);

impl TempDir {
    pub fn new() -> Self {
        use std::sync::atomic::{AtomicU64, Ordering};
        static COUNT: AtomicU64 = AtomicU64::new(0);
        let n = COUNT.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("octoflow-engine-test-{}-{n}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("a temporary directory");
        TempDir(dir)
    }

    /// The directory's path, as a str.
    pub fn path(&self) -> String {
        self.0.to_str().expect("a UTF-8 path").to_string()
    }

    /// The path of name in the directory, as a str.
    pub fn join(&self, name: &str) -> String {
        self.0.join(name).to_str().expect("a UTF-8 path").to_string()
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        if let Err(e) = std::fs::remove_dir_all(&self.0) {
            eprintln!("could not remove {}: {e}", self.0.display());
        }
    }
}

/// What starts a command with SIGINT at its default, so that a shell can trap it: one that the tests' process ignores,
/// as Jenkins's does, stays ignored in what it starts, as in Python, and a shell cannot trap a signal ignored on entry.
pub const HEARS_SIGINT: [&str; 2] = ["env", "--default-signal=INT"];

/// A shell script for a program the engine runs, such as a fake herdr, that runs body with SIGINT at its default,
/// whatever the tests' process does with it: it starts itself again through HEARS_SIGINT, once, with its arguments.
pub fn hearing_sigint(body: &str) -> String {
    format!(
        "#!/bin/sh\n[ -n \"$HEARS_SIGINT\" ] || {{ HEARS_SIGINT=1; export HEARS_SIGINT; exec {} \"$0\" \"$@\"; }}\n{body}",
        HEARS_SIGINT.join(" ")
    )
}

/// How long the Ctrl-C tests wait for a command to get somewhere or a process to end, before they fail.
const PATIENCE: std::time::Duration = std::time::Duration::from_secs(20);

/// Raise interrupt, from a thread of its own, once path exists: the human's Ctrl-C while a command that writes path
/// as it starts runs. The thread panics, failing the join, when path is not written within PATIENCE.
pub fn raise_once_written(path: &str, interrupt: &crate::interrupt::Interrupt) -> std::thread::JoinHandle<()> {
    let (path, interrupt) = (path.to_string(), interrupt.clone());
    std::thread::spawn(move || {
        assert!(eventually(|| std::fs::metadata(&path).is_ok_and(|m| m.len() > 0)), "{path} was not written");
        interrupt.raise();
    })
}

/// Whether the process whose pid path holds, as `echo $$` wrote it, has ended (gone, or a zombie no one reaps yet)
/// within PATIENCE: on Linux, as /proc shows it.
pub fn ended(path: &str) -> bool {
    let pid = std::fs::read_to_string(path).expect("a pid file").trim().to_string();
    eventually(|| match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        // The state follows the parenthesized command name.
        Ok(stat) => stat.rsplit_once(") ").is_some_and(|(_, rest)| rest.starts_with('Z')),
        Err(_) => true,
    })
}

/// Whether done holds within PATIENCE, asked every 10 ms.
pub fn eventually(mut done: impl FnMut() -> bool) -> bool {
    let deadline = std::time::Instant::now() + PATIENCE;
    while std::time::Instant::now() < deadline {
        if done() {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    done()
}

/// The repository's examples/workflows/.
pub const EXAMPLES: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../examples/workflows");

/// The bytes a string of hex digits stands for, as an oracle file writes bytes that are not UTF-8.
pub fn unhex(hex: &str) -> Vec<u8> {
    (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).expect("hex digits")).collect()
}

/// An oracle file of engine/tests/oracle/, which its generator there wrote from the Python engine, parsed.
pub fn oracle(file: &str) -> crate::py::json::Json {
    let text = match file {
        "texts.json" => include_str!("../tests/oracle/texts.json"),
        "workflows.json" => include_str!("../tests/oracle/workflows.json"),
        "states.json" => include_str!("../tests/oracle/states.json"),
        "quality.json" => include_str!("../tests/oracle/quality.json"),
        "helpers.json" => include_str!("../tests/oracle/helpers.json"),
        "host.json" => include_str!("../tests/oracle/host.json"),
        "checks.json" => include_str!("../tests/oracle/checks.json"),
        "harness.json" => include_str!("../tests/oracle/harness.json"),
        "cli.json" => include_str!("../tests/oracle/cli.json"),
        _ => panic!("no oracle file {file}"),
    };
    crate::py::json::Json::parse(text).unwrap_or_else(|e| panic!("{file}: {e}"))
}

// -- The workflows tests/test_orchestrator.py defines, by its names -------------------------------------------------

use crate::pipeline::{Pipeline, PipelineSettings, Step};
use crate::py::json::Map;

/// A role map of (key, label) pairs, in pane order.
pub fn roles(pairs: &[(&str, &str)]) -> Map<String> {
    pairs.iter().map(|(k, v)| (*k, v.to_string())).collect()
}

/// Step::new, with again set.
pub fn again(mut st: Step, text: &str) -> Step {
    st.again = Some(text.to_string());
    st
}

/// No spec step: the task is the Builder's contract.
pub fn quick() -> Pipeline {
    let mut build = again(Step::new("build", "build", "build-{n}.md", "Build {task} in {cwd}; report to {build_path}."),
        "Fix {prev_review_path}; report to {build_path}.");
    build.edits = true;
    let mut review = again(Step::new("review", "review", "review-{n}.md", "Review {change} against {task}; write {review_path}."),
        "Recheck {build_path}; write {review_path}.");
    review.loop_to = Some("build".into());
    Pipeline::new("quick", roles(&[("build", "Builder"), ("review", "Reviewer")]), vec![build, review]).unwrap()
}

/// Four roles: the tests are written once, and the review loop goes back to the Builder only.
pub fn four_steps() -> Vec<Step> {
    let mut spec = Step::new("spec", "spec", "spec.md", "Interview about {task}; write {spec_path}.");
    spec.human_paced = true;
    let mut tests = Step::new("tests", "tests", "tests.md", "Write tests for {spec_path}; report to {tests_path}.");
    tests.edits = true;
    let mut build = again(Step::new("build", "build", "build-{n}.md", "Build {spec_path} against {tests_path}; report to {build_path}."),
        "Fix {prev_review_path}; report to {build_path}.");
    build.fresh_note = Some("Earlier reports: {earlier_build_paths}.".into());
    build.fresh_repeats_again = true;
    build.edits = true;
    let mut review = again(Step::new("review", "review", "review-{n}.md", "Review {change}; write {review_path}."),
        "Recheck; write {review_path}.");
    review.loop_to = Some("build".into());
    vec![spec, tests, build, review]
}

pub fn four_roles() -> Map<String> {
    roles(&[("spec", "Spec Collector"), ("tests", "Test Writer"), ("build", "Builder"), ("review", "Reviewer")])
}

pub fn four() -> Pipeline {
    Pipeline::new("four", four_roles(), four_steps()).unwrap()
}

/// FOUR with its Test Writer locking the tests it writes.
pub fn locking() -> Pipeline {
    let steps = four_steps().into_iter().map(|mut st| { st.locks = st.id == "tests"; st }).collect();
    Pipeline::new("locking", four_roles(), steps).unwrap()
}

/// The quality-gated step is not called build.
pub fn impl_() -> Pipeline {
    let mut imp = again(Step::new("impl", "impl", "impl-{n}.md", "Implement {task}; report to {impl_path}."),
        "Fix {prev_review_path}; report to {impl_path}.");
    imp.fresh_note = Some("Earlier reports: {earlier_impl_paths}.".into());
    imp.edits = true;
    imp.quality_gated = true;
    let mut review = again(Step::new("review", "review", "review-{n}.md", "Review {change} with {impl_path}; write {path}."),
        "Recheck {impl_path}; write {review_path}.");
    review.loop_to = Some("impl".into());
    Pipeline::new("impl", roles(&[("impl", "Implementer"), ("review", "Reviewer")]), vec![imp, review]).unwrap()
}

/// No verdict step: the run finishes when the Builder's one turn is done.
pub fn solo() -> Pipeline {
    let mut build = Step::new("build", "build", "build.md", "Build {task} in {cwd}; report to {path}.");
    build.edits = true;
    Pipeline::new("solo", roles(&[("build", "Builder")]), vec![build]).unwrap()
}

/// p with other settings, validated again, as dataclasses.replace makes one.
pub fn replaced(p: &Pipeline, change: impl FnOnce(&mut PipelineSettings)) -> Result<Pipeline, String> {
    let mut settings = p.settings().clone();
    change(&mut settings);
    Pipeline::with(p.name(), p.roles().clone(), p.steps().to_vec(), settings)
}

/// Each recording of the e2e scenarios, engine/e2e/recorded/<scenario>.json, by scenario, in name order.
pub fn recordings() -> Vec<(String, crate::py::json::Json)> {
    let dir = concat!(env!("CARGO_MANIFEST_DIR"), "/e2e/recorded");
    let mut paths: Vec<_> = std::fs::read_dir(dir).expect("the recordings").map(|e| e.expect("an entry").path()).collect();
    paths.sort();
    paths
        .into_iter()
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .map(|p| {
            let text = std::fs::read_to_string(&p).expect("a recording");
            let name = p.file_stem().and_then(|s| s.to_str()).expect("a name").to_string();
            let recording = crate::py::json::Json::parse(&text).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
            (name, recording)
        })
        .collect()
}

/// A run of helpers.json's cases with the inputs engine/tests/oracle/helpers.py gives its helpers, read from its
/// recording as that script reads them, and the outputs it recorded.
pub struct HelperCase {
    /// The case: its recording, run directory and conflicts, and the Python engine's outputs by helper.
    pub expected: crate::py::json::Json,
    pub saved: Map<crate::py::json::Json>,
    pub state: crate::state::RunState,
    pub spec: String,
    pub report: String,
    pub review: String,
    pub evidence: crate::pull_request::GateEvidence,
    pub now: i64,
}

/// helpers.json's cases, their inputs read from the recordings.
pub fn helper_cases() -> Vec<HelperCase> {
    use crate::py::json::Json;
    let oracle = oracle("helpers.json");
    let epoch = oracle.get("epoch").and_then(Json::as_int).unwrap() as i64;
    let now = oracle.get("now").and_then(Json::as_int).unwrap() as i64;
    let recordings = recordings();
    let mut out = Vec::new();
    for case in oracle.get("cases").and_then(Json::as_array).unwrap() {
        let field = |key: &str| case.get(key).and_then(Json::as_str).unwrap().to_string();
        let (recording, run_dir) = (field("recording"), field("run_dir"));
        let name = recording.strip_suffix(".json").unwrap();
        let files = recordings.iter().find(|(n, _)| n == name).unwrap().1.get("run_files").unwrap().as_object().unwrap().clone();
        let file = |name: &str| files.get(&format!("{run_dir}{name}")).and_then(Json::as_str).map(String::from);
        let text = denormalized(&file("state.json").unwrap(), epoch);
        let Json::Object(mut saved) = Json::parse(&text).unwrap() else { panic!("a state") };
        let conflicts = case.get("conflicts").and_then(Json::as_array).unwrap();
        if !conflicts.is_empty() {
            saved.insert("conflicts", Json::Array(conflicts.clone()));
        }
        let state = crate::state::RunState::from_saved(&saved).unwrap();
        // helpers.py's last: the last of the run directory's files whose name matches, by name.
        let last = |matches: &dyn Fn(&str) -> bool| {
            let mut names: Vec<&str> = files.keys().filter_map(|n| n.strip_prefix(run_dir.as_str())).filter(|n| matches(n)).collect();
            names.sort();
            names.last().map(|n| (n.to_string(), file(n).unwrap()))
        };
        let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
        let body = |n: &str, prefix: &str| n.strip_prefix(prefix).and_then(|n| n.strip_suffix(".md")).map(String::from);
        let spec = last(&|n| n == "spec.md").map(|(_, t)| t).filter(|t| !t.is_empty()).unwrap_or(state.task.clone());
        let is_report = |n: &str| body(n, "build-").is_some_and(|b| match b.split_once("-q") {
            Some((round, q)) => digits(round) && digits(q),
            None => digits(&b),
        });
        let report = last(&is_report).map_or(String::new(), |(_, t)| t);
        let review = last(&|n| body(n, "review-").is_some_and(|b| digits(&b))).map_or(String::new(), |(_, t)| t);
        let is_quality = |n: &str| body(n, "quality-").is_some_and(|b| b.split_once('-').is_some_and(|(r, q)| digits(r) && digits(q)));
        let quality = last(&is_quality).map(|(n, text)| {
            let b = body(&n, "quality-").unwrap();
            let (round, q) = b.split_once('-').unwrap();
            crate::pull_request::QualityFile { round: round.parse().unwrap(), quality_round: q.parse().unwrap(), text }
        });
        let evidence = crate::pull_request::GateEvidence { last: quality, unjudged: state.quality_gate_disabled };
        out.push(HelperCase { expected: case.clone(), saved, state, spec, report, review, evidence, now });
    }
    out
}

/// helpers.py's denormalized: text, a recording's state.json, with each "<time>" string in it a time as
/// crate::summary::iso_utc writes it, the first at epoch (Unix seconds) and each a minute after the one before, and
/// each "<seconds>" string a count, 5 and then 7 more each time.
fn denormalized(text: &str, epoch: i64) -> String {
    use crate::py::json::Json;
    let mut out = String::new();
    let (mut times, mut counts) = (0, 5);
    let mut rest = text;
    while let Some(at) = rest.find('"') {
        out.push_str(&rest[..at]);
        rest = &rest[at..];
        if let Some(after) = rest.strip_prefix("\"<time>\"") {
            out.push_str(&Json::from(crate::summary::iso_utc(epoch + 60 * times)).dumps());
            times += 1;
            rest = after;
        } else if let Some(after) = rest.strip_prefix("\"<seconds>\"") {
            out.push_str(&counts.to_string());
            counts += 7;
            rest = after;
        } else {
            out.push('"');
            rest = &rest[1..];
        }
    }
    out.push_str(rest);
    out
}

/// The summary of the default workflow approved in round 1, as TestTurnTimings has it: crate::summary's tests
/// make it of a state, the Workflow's of a whole run.
pub const GOLDEN_SUMMARY: &str = "\
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

/// The JSON object text holds, as a saved record reads; panics on text that is not one.
pub fn object(text: &str) -> Map<crate::py::json::Json> {
    match crate::py::json::Json::parse(text) {
        Ok(crate::py::json::Json::Object(map)) => map,
        other => panic!("not a JSON object: {other:?}"),
    }
}

/// resumable_state for `resume a1b2c3` with flags over one run, saved as saved 10^4 s ago, in /proj, on host "here",
/// whose pids are all dead: Python's resumable helpers.
pub fn resumable(flags: &[&str], saved: &Map<crate::py::json::Json>) -> Result<crate::state::RunState, crate::error::OrchestratorError> {
    let argv = ["resume", "a1b2c3"].iter().chain(flags).map(std::ffi::OsString::from);
    let Ok(crate::cli::Command::Resume(args)) = crate::cli::parse_args(argv, &crate::env::ConfigDir::at("/nowhere")) else {
        panic!("not a resume: {flags:?}")
    };
    let runs = [(10_000, crate::py::json::Json::Object(saved.clone()))];
    crate::cli::resumable_state(&runs, &args, "/proj", "here", &|_| false).map(|(state, _)| state)
}
