//! The e2e scenarios (scenarios.rs), each run against the engine and compared with its recording in `recorded/`.
//!
//! A scenario's record (World::record) must equal `recorded/<name>.json`; a difference fails the test, naming where the
//! two first part. `E2E_RECORD=1` writes recordings instead: for a scenario added since, or an intended change of the
//! engine's behaviour, so read the diff of what it recorded as a review of that behaviour before committing it.
//!
//! The engine is E2E_ENGINE, the path of an orchestrator binary, when set, such as the build Jenkins's Test stage
//! made for coverage; else this checkout's, built with cargo (engine::binary). Linux only, as the engine's other
//! process tests.

mod compare;
mod engine;
mod scenarios;
mod world;

use std::path::PathBuf;

use octoflow_engine::py::json::{unicode_string, Json};

/// Where the recordings are.
fn recorded() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("recorded")
}

/// Run scenario name in a world of its own and compare its record with its recording, or write the recording under
/// E2E_RECORD=1.
fn check(name: &str, scenario: fn(&mut world::World)) {
    let mut w = world::World::new(name, &engine::binary());
    scenario(&mut w);
    let got = w.record();
    let path = recorded().join(format!("{name}.json"));
    if std::env::var("E2E_RECORD").as_deref() == Ok("1") {
        let mut text = String::new();
        write_recording(&got, 0, &mut text);
        text.push('\n');
        std::fs::write(&path, text).unwrap_or_else(|e| panic!("writing {}: {e}", path.display()));
        return;
    }
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!("scenario {name} has no recording at {} ({e}); make it with E2E_RECORD=1, and review what it records", path.display())
    });
    let want = Json::parse(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    if let Some(difference) = compare::first_difference(&want, &got, "$") {
        panic!("scenario {name} differs from its recording at {difference}");
    }
}

/// Write json at out's end as the Python harness wrote a recording, `json.dump(json, f, indent=1, ensure_ascii=False)`:
/// as py::json's dumps_indented(1), but with every character beyond ASCII as it is, not escaped. level is how many
/// arrays and objects hold json.
fn write_recording(json: &Json, level: usize, out: &mut String) {
    let (open, between, close) = (format!("\n{}", " ".repeat(level + 1)), ",", format!("\n{}", " ".repeat(level)));
    match json {
        Json::Str(s) => out.push_str(&unicode_string(s)),
        Json::Array(items) if !items.is_empty() => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                out.push_str(if i == 0 { "" } else { between });
                out.push_str(&open);
                write_recording(item, level + 1, out);
            }
            out.push_str(&close);
            out.push(']');
        }
        Json::Object(map) if !map.is_empty() => {
            out.push('{');
            for (i, (key, value)) in map.iter().enumerate() {
                out.push_str(if i == 0 { "" } else { between });
                out.push_str(&open);
                out.push_str(&unicode_string(key));
                out.push_str(": ");
                write_recording(value, level + 1, out);
            }
            out.push_str(&close);
            out.push('}');
        }
        // A number, a constant, or an empty array or object, which dumps writes as json.dump does.
        other => out.push_str(&other.dumps()),
    }
}

#[test]
fn a_recording_keeps_characters_beyond_ascii_as_they_are() {
    let record = Json::parse(r#"{"a": ["é \u2014", {}, []], "b": {"n": 1.5, "x": null}}"#).expect("JSON");
    let mut text = String::new();
    write_recording(&record, 0, &mut text);
    assert_eq!(text, "{\n \"a\": [\n  \"é \u{2014}\",\n  {},\n  []\n ],\n \"b\": {\n  \"n\": 1.5,\n  \"x\": null\n }\n}");
}

/// A #[test] for each scenario, by its name in scenarios.rs.
macro_rules! scenarios {
    ($($name:ident),* $(,)?) => {
        $(
            #[test]
            fn $name() {
                check(stringify!($name), scenarios::$name);
            }
        )*
    };
}

scenarios!(
    agent_exits,
    blocked_build_answered,
    branches_pruned,
    conflict_refreshed,
    default_approved,
    default_changes_then_approved,
    default_max_rounds,
    doctor_checks,
    failed_run_closed,
    finished_run_closed,
    locks_restored,
    machine_run,
    no_pr_run,
    prompt_file_run,
    proofs_run,
    publish_with_conflict,
    runs_listed_and_shown,
    spec_file_run,
    specs_drained,
    takeover,
    verdict_retried,
    workflow_product_team,
    workflow_quick,
    workflow_spec_build_review,
    workflows_listed,
    worktree_run,
);
