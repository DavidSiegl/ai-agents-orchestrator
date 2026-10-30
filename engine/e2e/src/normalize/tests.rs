//! The normalizer against tests/parity/normalize.py's: oracle.json holds what Python's Normalizer and facts gave for the
//! texts here, with the same literals, pids and learnt record, made by running them on those inputs.

use serde_json::{json, Value};

use super::*;

fn oracle() -> Value {
    serde_json::from_str(include_str!("oracle.json")).expect("oracle.json is JSON")
}

/// The normalizer oracle.json's cases were made with: two literals, three pids (the first registered twice), and
/// oracle.json's record learnt.
fn normalizer(oracle: &Value) -> Normalizer {
    let mut n = Normalizer::new();
    n.literal("/w/orld", "<world>");
    n.literal("/w", "<short>");
    n.pid(4242, 1);
    n.pid(77, 2);
    n.pid(4242, 3);
    n.learn(&oracle["json"][0]);
    n
}

#[test]
fn text_writes_placeholders_as_python_did() {
    let oracle = oracle();
    let n = normalizer(&oracle);
    for case in oracle["texts"].as_array().expect("texts") {
        let text = case[0].as_str().expect("a text");
        assert_eq!(n.text(text, false), case[1].as_str().expect("untimed"), "{text}");
        assert_eq!(n.text(text, true), case[2].as_str().expect("timed"), "{text}");
    }
}

#[test]
fn json_replaces_timed_numbers_and_pids_as_python_did() {
    let oracle = oracle();
    let n = normalizer(&oracle);
    assert_eq!(n.json(&oracle["json_in"], false, None), oracle["json"][1]);
    assert_eq!(n.json(&json!({"a": "5s"}), true, None), oracle["json"][2]);
}

#[test]
fn facts_are_found_as_python_found_them() {
    for case in oracle()["facts"].as_array().expect("facts") {
        let want: Vec<String> = serde_json::from_value(case[1].clone()).expect("a list of facts");
        assert_eq!(facts(case[0].as_str().expect("a text")), want);
    }
}

#[test]
fn the_host_is_a_placeholder() {
    let host = nix::unistd::gethostname().expect("the host's name").to_string_lossy().into_owned();
    assert_eq!(Normalizer::new().text(&format!("on {host}."), false), "on <host>.");
}
