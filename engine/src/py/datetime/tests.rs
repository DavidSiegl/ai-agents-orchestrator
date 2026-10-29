use super::*;
use crate::py::json::Json;
use crate::test_support::oracle;

#[test]
fn every_time_reads_as_python_s_fromisoformat_reads_it() {
    let cases = oracle("harness.json").get("fromisoformat").and_then(Json::as_array).cloned().unwrap();
    // harness.py's ZONE, which Python read the times without an offset in.
    let zone = TimeZone::posix("CET-1CEST,M3.5.0,M10.5.0/3").unwrap();
    assert!(cases.len() > 3000);
    let mut differ = vec![];
    for case in &cases {
        let [iso, read] = &case.as_array().unwrap()[..] else { panic!("{case:?}") };
        let iso = iso.as_str().unwrap();
        let ours = IsoTime::parse(iso).and_then(|time| time.timestamp_in(&zone));
        let same = match (read.get("ok"), read.get("error").and_then(Json::as_str)) {
            (Some(Json::Float(seconds)), _) => ours == Ok(*seconds),
            // timestamp()'s refusal of a time fromisoformat reads, whose message is CPython 3.14's; a refusal by
            // fromisoformat itself is compared as one (TODO.md, "The Rust engine's fromisoformat words some errors
            // as CPython 3.13 does").
            (_, Some(error)) if IsoTime::parse(iso).is_ok() => ours.as_ref().err().map(String::as_str) == Some(error),
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

#[test]
fn a_time_is_written_as_python_s_time_strftime_writes_it_in_each_zone() {
    let cases = oracle("harness.json").get("strftime_local").and_then(Json::as_array).cloned().unwrap();
    assert!(cases.len() > 20);
    for case in &cases {
        let [zone, seconds, written] = &case.as_array().unwrap()[..] else { panic!("{case:?}") };
        let zone = TimeZone::posix(zone.as_str().unwrap()).unwrap();
        let seconds = i64::try_from(seconds.as_int().unwrap()).unwrap();
        assert_eq!(strftime_in("%Y%m%d-%H%M%S", seconds, &zone).as_deref(), Ok(written.as_str().unwrap()), "{case:?}");
    }
    assert!(strftime_in("%Y", i64::MAX, &TimeZone::UTC).is_err());
}
