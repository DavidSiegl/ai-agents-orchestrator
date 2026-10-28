use super::*;
use crate::py::oracle;

#[test]
fn dumps_writes_what_json_dumps_does() {
    for case in oracle("json") {
        let case = case.as_array().unwrap();
        let value = Json::parse(case[0].as_str().unwrap()).unwrap();
        assert_eq!(value.dumps(), case[1].as_str().unwrap());
    }
}

#[test]
fn objects_keep_their_keys_in_order_and_a_repeated_key_its_first_place() {
    let value = Json::parse(r#"{"b": 1, "a": 2, "b": 3}"#).unwrap();
    assert_eq!(value.dumps(), r#"{"b": 3, "a": 2}"#);
}

#[test]
fn maps_compare_as_dicts_do() {
    let a: Map<i64> = [("x", 1), ("y", 2)].into_iter().collect();
    let b: Map<i64> = [("y", 2), ("x", 1)].into_iter().collect();
    let c: Map<i64> = [("y", 2)].into_iter().collect();
    assert_eq!(a, b);
    assert_ne!(a, c);
    let mut d = a.clone();
    assert_eq!(d.remove("x"), Some(1));
    assert_eq!(d.remove("x"), None);
    assert_eq!(d.keys().collect::<Vec<_>>(), ["y"]);
}

#[test]
fn truthiness_is_pythons() {
    for falsy in ["null", "false", "0", "0.0", "\"\"", "[]", "{}"] {
        assert!(!Json::parse(falsy).unwrap().truthy(), "{falsy}");
    }
    for truthy in ["true", "1", "0.5", "\"x\"", "[0]", "{\"a\": null}"] {
        assert!(Json::parse(truthy).unwrap().truthy(), "{truthy}");
    }
}

#[test]
fn floats_read_as_python_reads_them_and_wide_integers_are_refused() {
    for text in ["26.010854494457302", "0.1", "1e-07", "123456789.12345679", "-0.0"] {
        assert_eq!(Json::parse(text).unwrap().dumps(), text, "{text}");
    }
    assert_eq!(Json::parse("[18446744073709551615, -9223372036854775808]").unwrap().dumps(), "[18446744073709551615, -9223372036854775808]");
    assert_eq!(Json::parse("18446744073709551616").unwrap_err(), "the integer 18446744073709551616 is beyond 64 bits");
    assert!(Json::parse("[-9223372036854775809]").is_err());
    assert!(Json::parse(r#"{"a\"1": "99999999999999999999", "b": 1.5e300}"#).is_ok());
}

#[test]
fn text_that_is_not_json_is_refused() {
    assert!(Json::parse("{").is_err());
    assert!(Json::parse("[1] x").is_err());
    assert!(Json::parse("NaN").is_err());
}

#[test]
fn a_lone_surrogate_reads_as_the_replacement_character() {
    let read = |text: &str| Json::parse_lossy(text).map(|j| j.as_str().map(String::from));
    // Halves without their pair, high or low, at the end or before another character.
    assert_eq!(read(r#""cut \ud83d""#), Ok(Some("cut \u{fffd}".to_string())));
    assert_eq!(read(r#""\udc00\ud800x""#), Ok(Some("\u{fffd}\u{fffd}x".to_string())));
    assert_eq!(read(r#""\ud83dA""#), Ok(Some("\u{fffd}A".to_string())));
    // A pair, an escaped backslash before u, and an escape outside any string are left as they are.
    assert_eq!(read(r#""😀""#), Ok(Some("😀".to_string())));
    assert_eq!(read(r#""\\ud800""#), Ok(Some("\\ud800".to_string())));
    assert!(read(r#"\ud800"#).is_err());
    // Strict parse refuses what parse_lossy reads, and both read text without escapes alike.
    assert!(Json::parse(r#""\ud800""#).is_err());
    assert_eq!(Json::parse_lossy(r#"{"a": [1, "é"]}"#), Json::parse(r#"{"a": [1, "é"]}"#));
}
