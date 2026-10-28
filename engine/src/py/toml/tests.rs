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
