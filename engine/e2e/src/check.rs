//! A scenario run against the engine and compared with its recording: `Bench::check`.
//!
//! A scenario's record (World::record) must equal recorded/<name>.json; a difference fails the test, naming where the
//! two first part. The recordings are the Python engine's, the oracle the Rust engine was ported against, made before
//! the switch removed it (rust-engine ticket 11). `E2E_RECORD=1 cargo nextest run -p octoflow-e2e` writes recordings
//! instead (Bench::new says when that is right).

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::dirs::{self, Scratch};
use crate::pyjson;
use crate::world::{Fakes, World};

/// Where scenarios are checked: the fakes every world puts on the engine's PATH, the recordings a record is held to,
/// where the worlds are made, and whether a check writes its recording instead of comparing with it.
pub struct Bench {
    pub fakes: Fakes,
    /// The directory of the recordings, <scenario>.json each.
    pub recordings: PathBuf,
    /// The directory each scenario's world is made in, a directory of its own below it.
    pub worlds: PathBuf,
    /// True means a check writes its scenario's recording, rather than comparing the record with it.
    pub writes: bool,
}

impl Bench {
    /// The bench of this checkout: its recordings, engine/e2e/recorded/; worlds under TMPDIR; and recordings written
    /// rather than compared when E2E_RECORD is 1, for a scenario added since the Python engine went, which has no
    /// Python recording to hold the Rust engine to, so that the diff of what it recorded is read as a review of the
    /// engine's behaviour before it is committed.
    pub fn new(fakes: Fakes) -> Bench {
        Bench {
            fakes,
            recordings: Path::new(env!("CARGO_MANIFEST_DIR")).join("recorded"),
            worlds: dirs::temp(),
            writes: std::env::var("E2E_RECORD").as_deref() == Ok("1"),
        }
    }

    /// Run scenario, named name, in a fresh world, and compare what it recorded with its recording, or write the
    /// recording where the bench writes. Panics, failing the test, at a difference or a missing recording. The world's
    /// directory is removed when the scenario passes and kept for a look when it fails.
    pub fn check(&self, name: &str, scenario: impl FnOnce(&mut World)) {
        let world = Scratch::kept_on_failure(&self.worlds, name);
        let root = world.path();
        let got = {
            let mut w = World::new(root, &self.fakes);
            scenario(&mut w);
            w.record()
        };
        let path = self.recordings.join(format!("{name}.json"));
        if self.writes {
            let text = pyjson::dumps_recording(&got) + "\n";
            fs::write(&path, text).unwrap_or_else(|e| panic!("writing {}: {e}", path.display()));
        } else {
            let text = fs::read_to_string(&path).unwrap_or_else(|e| {
                panic!("scenario {name} has no recording {} ({e}); make it with E2E_RECORD=1, and review what it records", path.display())
            });
            let want: Value = serde_json::from_str(&text).unwrap_or_else(|e| panic!("{} is no JSON: {e}", path.display()));
            if let Some(difference) = first_difference(&want, &got, "$") {
                panic!("scenario {name} differs from its recording at {difference}\n(its world is kept in {})", root.display());
            }
        }
    }
}

/// Where got first differs from want, two JSON values, as a JSON path and the two sides there; None when they are
/// equal, the order of an object's keys included. For strings, the line on which they part.
pub fn first_difference(want: &Value, got: &Value, path: &str) -> Option<String> {
    match (want, got) {
        (Value::Object(w), Value::Object(g)) => object_difference(w, g, path),
        (Value::Array(w), Value::Array(g)) => array_difference(w, g, path),
        (Value::String(w), Value::String(g)) if w != g => Some(line_difference(w, g, path)),
        (w, g) if w == g => None,
        (w, g) => Some(format!("{path}: recorded {}, got {}", short(w, 300), short(g, 300))),
    }
}

/// tree's JSON text, cut after n characters.
fn short(tree: &Value, n: usize) -> String {
    pyjson::dumps(tree).chars().take(n).collect()
}

/// first_difference of two objects: a key only one has, the first value under a key that differs, or their keys'
/// order, in that order.
fn object_difference(want: &Map<String, Value>, got: &Map<String, Value>, path: &str) -> Option<String> {
    for key in want.keys().chain(got.keys().filter(|k| !want.contains_key(*k))) {
        let (Some(a), Some(b)) = (want.get(key), got.get(key)) else {
            let side = if want.contains_key(key) { "recorded" } else { "in this run" };
            return Some(format!("{path}: key {key:?} only {side}"));
        };
        if let Some(found) = first_difference(a, b, &format!("{path}[{key:?}]")) {
            return Some(found);
        }
    }
    let (wk, gk): (Vec<_>, Vec<_>) = (want.keys().collect(), got.keys().collect());
    (wk != gk).then(|| format!("{path}: keys in another order: recorded {wk:?}, got {gk:?}"))
}

/// first_difference of two arrays: the first item that differs, else their lengths and the first item one has more.
fn array_difference(want: &[Value], got: &[Value], path: &str) -> Option<String> {
    let found = want.iter().zip(got).enumerate().find_map(|(i, (a, b))| first_difference(a, b, &format!("{path}[{i}]")));
    if found.is_some() || want.len() == got.len() {
        return found;
    }
    let (side, extra) = if want.len() > got.len() { ("recorded", &want[got.len()]) } else { ("got", &got[want.len()]) };
    Some(format!("{path}: recorded {} items, got {}; first unmatched ({side}): {}", want.len(), got.len(), short(extra, 500)))
}

/// Where two strings that differ part: the line, and each side's text of it.
fn line_difference(want: &str, got: &str, path: &str) -> String {
    let (a, b): (Vec<&str>, Vec<&str>) = (want.split('\n').collect(), got.split('\n').collect());
    let i = a.iter().zip(&b).position(|(x, y)| x != y).unwrap_or(a.len().min(b.len()));
    let line = |l: &[&str]| l.get(i).map_or("(no line)".to_string(), |s| format!("{s:?}"));
    format!("{path}, line {}:\n  recorded: {}\n  got:      {}", i + 1, line(&a), line(&b))
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
        assert!(first_difference(&json!({"a": 1}), &json!({}), "$").unwrap().contains("key \"a\" only recorded"));
        assert!(first_difference(&json!({}), &json!({"b": 1}), "$").unwrap().contains("key \"b\" only in this run"));
        let found = first_difference(&json!([1]), &json!([1, 2]), "$").unwrap();
        assert!(found.contains("recorded 1 items, got 2; first unmatched (got): 2"), "{found}");
        let found = first_difference(&json!([1, 3]), &json!([1]), "$").unwrap();
        assert!(found.contains("recorded 2 items, got 1; first unmatched (recorded): 3"), "{found}");
        assert!(first_difference(&json!(1), &json!("1"), "$").unwrap().contains("recorded 1, got \"1\""));
    }
}
