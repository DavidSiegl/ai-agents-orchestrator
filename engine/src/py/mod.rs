//! What the port needs of Python's own semantics, so that the engine reads and writes text as orchestrator.py did:
//! str's methods and character classes (text), format strings (format), JSON (json), TOML's values (toml), the
//! parts of a URL (url) and ISO 8601 times (datetime).

pub mod datetime;
pub mod format;
pub mod json;
pub mod text;
pub mod toml;
pub mod url;
mod unicode_tables;

/// What engine/tests/oracle/python.py recorded from Python under key, which the tests of this module replay.
#[cfg(test)]
pub(crate) fn recorded(key: &str) -> json::Json {
    let all = json::Json::parse(include_str!("../../tests/oracle/python.json")).expect("python.json is JSON");
    all.get(key).cloned().unwrap_or_else(|| panic!("python.json has no {key}"))
}

/// The cases engine/tests/oracle/python.py recorded from Python for one kind of call.
#[cfg(test)]
pub(crate) fn oracle(kind: &str) -> Vec<json::Json> {
    recorded(kind).as_array().cloned().unwrap_or_else(|| panic!("python.json's {kind} is no list"))
}
