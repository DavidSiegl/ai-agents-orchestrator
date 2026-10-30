//! A scenario run against the engine and compared with its recording in recorded/: `check`.
//!
//! A scenario's record (World::record) must equal recorded/<name>.json; a difference fails the test, naming where the
//! two first part. The recordings are the Python engine's, the oracle the Rust engine was ported against, made before
//! the switch removed it (rust-engine ticket 11). `E2E_RECORD=1 cargo nextest run -p octoflow-e2e` writes recordings
//! instead: for a scenario added since, which has no Python recording to hold the Rust engine to, so read the diff of
//! what it recorded as a review of the engine's behaviour before committing it.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::pyjson;
use crate::world::{Fakes, World};

/// The directory of the recordings, recorded/<scenario>.json.
pub fn recorded() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("recorded")
}

/// Run scenario, named name, in a fresh world whose fakes are fakes, and compare what it recorded with its recording,
/// or with E2E_RECORD=1 write the recording. Panics, failing the test, at a difference or a missing recording. The
/// world's directory, under TMPDIR, is removed when the scenario passes and kept for a look when it fails.
pub fn check(name: &str, fakes: &Fakes, scenario: impl FnOnce(&mut World)) {
    let base = std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, PathBuf::from);
    let root = base.join(format!("octoflow-e2e-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).unwrap_or_else(|e| panic!("making {}: {e}", root.display()));
    let got = {
        let mut world = World::new(&root, fakes);
        scenario(&mut world);
        world.record()
    };
    let path = recorded().join(format!("{name}.json"));
    if std::env::var("E2E_RECORD").as_deref() == Ok("1") {
        fs::write(&path, pyjson::dumps_recording(&got) + "\n").unwrap_or_else(|e| panic!("writing {}: {e}", path.display()));
    } else {
        let text = fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("scenario {name} has no recording {} ({e}); make it with E2E_RECORD=1, and review what it records", path.display()));
        let want: Value = serde_json::from_str(&text).unwrap_or_else(|e| panic!("{} is no JSON: {e}", path.display()));
        if let Some(difference) = first_difference(&want, &got, "$") {
            panic!("scenario {name} differs from its recording at {difference}\n(its world is kept in {})", root.display());
        }
    }
    let _ = fs::remove_dir_all(&root);
}

/// Where got first differs from want, two JSON values, as a JSON path and the two sides there; None when they are
/// equal, the order of an object's keys included. For strings, the line on which they part.
pub fn first_difference(want: &Value, got: &Value, path: &str) -> Option<String> {
    let short = |v: &Value, n: usize| pyjson::dumps(v).chars().take(n).collect::<String>();
    match (want, got) {
        (Value::Object(w), Value::Object(g)) => {
            for key in w.keys().chain(g.keys().filter(|k| !w.contains_key(*k))) {
                let (Some(a), Some(b)) = (w.get(key), g.get(key)) else {
                    let side = if w.contains_key(key) { "recorded" } else { "in this run" };
                    return Some(format!("{path}: key {key:?} only {side}"));
                };
                if let Some(found) = first_difference(a, b, &format!("{path}[{key:?}]")) {
                    return Some(found);
                }
            }
            let (wk, gk): (Vec<_>, Vec<_>) = (w.keys().collect(), g.keys().collect());
            (wk != gk).then(|| format!("{path}: keys in another order: recorded {wk:?}, got {gk:?}"))
        }
        (Value::Array(w), Value::Array(g)) => {
            for (i, (a, b)) in w.iter().zip(g).enumerate() {
                if let Some(found) = first_difference(a, b, &format!("{path}[{i}]")) {
                    return Some(found);
                }
            }
            (w.len() != g.len()).then(|| {
                let (side, extra) = if w.len() > g.len() { ("recorded", &w[g.len()]) } else { ("got", &g[w.len()]) };
                format!("{path}: recorded {} items, got {}; first unmatched ({side}): {}", w.len(), g.len(), short(extra, 500))
            })
        }
        (Value::String(w), Value::String(g)) if w != g => {
            let (a, b): (Vec<&str>, Vec<&str>) = (w.split('\n').collect(), g.split('\n').collect());
            let i = a.iter().zip(&b).position(|(x, y)| x != y).unwrap_or(a.len().min(b.len()));
            let line = |l: &[&str]| l.get(i).map_or("(no line)".to_string(), |s| format!("{s:?}"));
            Some(format!("{path}, line {}:\n  recorded: {}\n  got:      {}", i + 1, line(&a), line(&b)))
        }
        (w, g) if w == g => None,
        (w, g) => Some(format!("{path}: recorded {}, got {}", short(w, 300), short(g, 300))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn equal_values_have_no_difference() {
        assert_eq!(first_difference(&json!({"a": [1, "x"]}), &json!({"a": [1, "x"]}), "$"), None);
    }

    #[test]
    fn a_difference_is_named_by_its_path_and_line() {
        let found = first_difference(&json!({"a": ["x\ny"]}), &json!({"a": ["x\nz"]}), "$").unwrap();
        assert_eq!(found, "$[\"a\"][0], line 2:\n  recorded: \"y\"\n  got:      \"z\"");
    }

    #[test]
    fn keys_in_another_order_differ() {
        let found = first_difference(&json!({"a": 1, "b": 2}), &json!({"b": 2, "a": 1}), "$").unwrap();
        assert!(found.contains("keys in another order"), "{found}");
    }

    #[test]
    fn missing_keys_and_items_differ() {
        assert!(first_difference(&json!({"a": 1}), &json!({}), "$").unwrap().contains("only recorded"));
        assert!(first_difference(&json!([1]), &json!([1, 2]), "$").unwrap().contains("recorded 1 items, got 2"));
        assert!(first_difference(&json!(1), &json!("1"), "$").unwrap().contains("recorded 1, got \"1\""));
    }
}
