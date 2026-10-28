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
fn python_str_is_str_of_what_json_loads_read() {
    let value = Json::parse(r#"[null, true, false, 1, 2.5, 1e300, "it's", {"k": [], "q\"": "a"}]"#).unwrap();
    assert_eq!(value.python_str(), r#"[None, True, False, 1, 2.5, 1e+300, "it's", {'k': [], 'q"': 'a'}]"#);
    assert_eq!(Json::from("plain").python_str(), "plain");
    assert_eq!(Json::Float(f64::INFINITY).python_str(), "inf");
}
