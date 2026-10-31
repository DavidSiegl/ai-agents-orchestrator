//! The engine's end-to-end scenarios: the orchestrator program run, as a human runs it, against fakes of herdr, gh
//! and ssh, each scenario's record compared with its recording in `recorded/`.
//!
//! The scenarios and the world they run in are this package's test, `tests/e2e/`. What the test shares with the
//! fakes, which run as programs of their own (`src/bin/`), is here: the ledger they keep together (`ledger`), the
//! fakes themselves (`fakes`), and the normalizer that makes two runs of a scenario compare equal (`normalize`).
//!
//! The recordings were made by the Python harness this replaces (tests/parity), and this harness reads and writes
//! them byte for byte as it did, through the engine's `py::json`.

pub mod fakes;
pub mod ledger;
pub mod normalize;
pub mod procs;
pub mod scratch;

use std::fs;
use std::path::{Path, PathBuf};

use octoflow_engine::py::json::{Json, Map};

/// A JSON object of pairs, in their order.
pub fn object<'a>(pairs: impl IntoIterator<Item = (&'a str, Json)>) -> Json {
    Json::Object(pairs.into_iter().collect())
}

/// A JSON array of strings.
pub fn strings<S: AsRef<str>>(items: &[S]) -> Json {
    Json::Array(items.iter().map(|s| Json::Str(s.as_ref().to_string())).collect())
}

/// The object map holds at key, inserted empty first when map has no such key; panics when it holds something else,
/// which no ledger or script of this harness's writing does.
pub fn object_at<'a>(map: &'a mut Map<Json>, key: &str) -> &'a mut Map<Json> {
    if !map.contains_key(key) {
        map.insert(key, Json::Object(Map::new()));
    }
    match map.get_mut(key) {
        Some(Json::Object(inner)) => inner,
        _ => panic!("{key} is not an object"),
    }
}

/// The array map holds at key, inserted empty first when map has no such key; panics when it holds something else.
pub fn array_at<'a>(map: &'a mut Map<Json>, key: &str) -> &'a mut Vec<Json> {
    if !map.contains_key(key) {
        map.insert(key, Json::Array(Vec::new()));
    }
    match map.get_mut(key) {
        Some(Json::Array(items)) => items,
        _ => panic!("{key} is not an array"),
    }
}

/// Whether the array map holds at key has the string s; false when it has no such key.
pub fn holds(map: &Map<Json>, key: &str, s: &str) -> bool {
    map.get(key).and_then(Json::as_array).is_some_and(|items| items.iter().any(|i| i.as_str() == Some(s)))
}

/// The value at key of the object json, which must be one and have it; panics naming key otherwise.
pub fn at<'a>(json: &'a Json, key: &str) -> &'a Json {
    json.get(key).unwrap_or_else(|| panic!("no {key} in {}", json.dumps()))
}

/// The string at key of the object json; panics naming key when it has none.
pub fn str_at<'a>(json: &'a Json, key: &str) -> &'a str {
    at(json, key).as_str().unwrap_or_else(|| panic!("{key} is not a string in {}", json.dumps()))
}

/// Where the engine keeps the runs of the project at project, a directory per run named `<run id>-<key>`.
pub fn runs_dir(project: &Path) -> PathBuf {
    project.join(".orchestrator").join("runs")
}

/// The directory of the project's run whose key is key. Panics, naming the directory, when runs/ cannot be listed or
/// holds no run of that key, or more than one.
pub fn run_dir(project: &Path, key: &str) -> PathBuf {
    let runs = runs_dir(project);
    let entries = fs::read_dir(&runs).unwrap_or_else(|e| panic!("listing {}: {e}", runs.display()));
    let found: Vec<String> = entries
        .map(|e| e.unwrap_or_else(|e| panic!("listing {}: {e}", runs.display())))
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.ends_with(&format!("-{key}")))
        .collect();
    match found.as_slice() {
        [one] => runs.join(one),
        _ => panic!("no single run directory for key {key} in {}: {found:?}", runs.display()),
    }
}
