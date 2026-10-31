//! The hidden acceptance test of the eval task `bugfix`: `parse_duration` of several units (task.md). `cargo xtask
//! eval` copies it into the clone's tests/ after the run and runs it with the crate's own tests (xtask/src/eval.rs).

use std::path::Path;

use durations::{format_duration, parse_duration};

#[test]
fn several_units() {
    let cases =
        [("1h30m", 5400), ("2h5m10s", 7510), ("10s1h", 3610), ("1m1s", 61), (" 1h0m5s ", 3605), ("12m30s", 750)];
    for (text, seconds) in cases {
        assert_eq!(parse_duration(text).ok(), Some(seconds), "{text:?}");
    }
}

#[test]
fn one_unit() {
    let parsed: Vec<_> = ["45s", "2m", "3h", "0s"].map(|text| parse_duration(text).ok()).to_vec();
    assert_eq!(parsed, [Some(45), Some(120), Some(10800), Some(0)]);
}

#[test]
fn round_trip() {
    for seconds in (0..4000).step_by(37).chain([86399, 90061]) {
        assert_eq!(parse_duration(&format_duration(seconds)).ok(), Some(seconds), "{seconds}");
    }
}

#[test]
fn still_rejected() {
    for text in ["", " ", "5", "h", "1x", "1h-", "1h30", "m30", "1.5h", "-1h"] {
        assert!(parse_duration(text).is_err(), "{text:?} was read as a duration");
    }
}

#[test]
fn format_unchanged() {
    let formatted = [0, 59, 61, 3600, 5400, 3605, 7505].map(format_duration);
    assert_eq!(formatted, ["0s", "59s", "1m1s", "1h", "1h30m", "1h5s", "2h5m5s"]);
}

/// Whether text, a string literal's content, is a duration of more than one unit, such as `1h30m`.
fn several(text: &str) -> bool {
    let text = text.trim();
    let units = text.chars().filter(|c| "hms".contains(*c)).count();
    units > 1 && text.chars().all(|c| c.is_ascii_digit() || "hms".contains(c))
}

#[test]
fn a_regression_test() {
    let tests = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests");
    let found = std::fs::read_dir(&tests)
        .expect("tests/ lists")
        .map(|entry| entry.expect("an entry of tests/ reads").path())
        .filter(|path| path.file_name().is_some_and(|name| name != "hidden_test.rs"))
        .filter_map(|path| std::fs::read_to_string(path).ok())
        .any(|text| text.split("parse_duration(\"").skip(1).any(|rest| rest.split('"').next().is_some_and(several)));
    assert!(found, "no test in tests/ calls parse_duration with a duration of several units");
}
