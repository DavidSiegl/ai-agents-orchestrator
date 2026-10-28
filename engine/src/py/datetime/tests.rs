use super::*;
use crate::py::json::Json;
use crate::test_support::oracle;

#[test]
fn every_time_reads_as_python_s_fromisoformat_reads_it() {
    let cases = oracle("harness.json").get("fromisoformat").and_then(Json::as_array).cloned().unwrap();
    assert!(cases.len() > 3000);
    let mut differ = vec![];
    for case in &cases {
        let [iso, read] = &case.as_array().unwrap()[..] else { panic!("{case:?}") };
        let iso = iso.as_str().unwrap();
        let ours = fromisoformat_timestamp(iso);
        let same = match read.get("ok") {
            Some(Json::Float(seconds)) => ours == Ok(*seconds),
            _ => ours.is_err(),
        };
        if !same {
            differ.push(format!("{iso:?}: Python {}, the engine {ours:?}", read.dumps()));
        }
    }
    assert!(differ.is_empty(), "{} of {} differ:\n{}", differ.len(), cases.len(), differ.join("\n"));
}

#[test]
fn an_error_names_the_string_or_the_field_out_of_range() {
    assert_eq!(fromisoformat_timestamp("garbled"), Err("Invalid isoformat string: 'garbled'".to_string()));
    assert_eq!(fromisoformat_timestamp("2026-10-28T12:00:60"), Err("second must be in 0..59, not 60".to_string()));
    assert_eq!(
        fromisoformat_timestamp("2026-10-28T24:00:01"),
        Err("minute, second, and microsecond must be 0 when hour is 24".to_string())
    );
}
