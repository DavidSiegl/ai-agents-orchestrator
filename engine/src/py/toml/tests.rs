use super::*;
use crate::py::json::Json;
use crate::test_support::oracle;

#[test]
fn values_are_written_as_python_writes_what_tomllib_reads() {
    for case in oracle("checks.json").get("toml_values").and_then(Json::as_array).unwrap() {
        let text = |k: &str| case.get(k).and_then(Json::as_str).unwrap().to_string();
        let literal = text("literal");
        let value = loads(&format!("v = {literal}")).unwrap().remove("v").unwrap();
        assert_eq!(python_type(&value), text("type"), "{literal}");
        assert_eq!(str(&value), text("str"), "{literal}");
        assert_eq!(repr(&value), text("repr"), "{literal}");
        let dumped = match json_dumps(&value) {
            Ok(text) => Json::Str(text),
            Err(e) => Json::Object([("error", Json::from(e))].into_iter().collect()),
        };
        assert_eq!(&dumped, case.get("json").unwrap(), "{literal}");
    }
}

#[test]
fn text_that_is_not_toml_says_where() {
    assert_eq!(loads("a = 1\n[b\n").unwrap_err().split(" (at ").nth(1), Some("line 2, column 3)"));
    assert!(loads("a = 1\nb").unwrap_err().contains("(at line 2, column 2)"));
    assert_eq!(loads("\"é\" = \"x\"\n").map(|t| t.len()), Ok(1));
}

#[test]
fn an_instant_counts_from_1970_in_utc_to_the_microsecond() {
    let at = |text: &str| match loads(&format!("v = {text}")).unwrap().remove("v").unwrap() {
        Value::Datetime(d) => instant(&d),
        _ => unreachable!(),
    };
    assert_eq!(at("1970-01-01T00:00:00Z"), Some((0, 0)));
    assert_eq!(at("1970-01-01T01:00:00.0000009+01:00"), Some((0, 0)));
    assert_eq!(at("2026-10-03T06:00:00.25+02:00"), Some((1_791_000_000, 250_000)));
    assert_eq!(at("1979-05-27T07:32:00"), None);
}
