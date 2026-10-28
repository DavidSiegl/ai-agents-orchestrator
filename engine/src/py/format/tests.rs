use super::*;
use crate::py::json::Json;
use crate::py::{oracle, recorded};

/// The keyword arguments python.py formats each template with.
fn values(name: &str) -> Option<String> {
    recorded("values").get(name).and_then(Json::as_str).map(String::from)
}

fn outcome<T>(result: Result<T, FormatError>, ok: impl Fn(T) -> Json) -> Json {
    let mut map = crate::py::json::Map::new();
    match result {
        Ok(v) => map.insert("ok", ok(v)),
        Err(e) => map.insert("error", Json::Str(e.named())),
    }
    Json::Object(map)
}

#[test]
fn format_fills_in_and_fails_as_python_does() {
    for case in oracle("format") {
        let case = case.as_array().unwrap();
        let template = case[0].as_str().unwrap();
        let got = outcome(format(template, &values), Json::Str);
        assert_eq!(got.dumps(), case[1].dumps(), "{template:?}");
    }
}

#[test]
fn parse_reads_as_formatter_parse_does() {
    for case in oracle("parse") {
        let case = case.as_array().unwrap();
        let template = case[0].as_str().unwrap();
        let got = outcome(parse(template), |chunks| {
            Json::Array(
                chunks
                    .into_iter()
                    .map(|c| {
                        let field = c.field;
                        Json::Array(vec![
                            Json::Str(c.literal),
                            field.as_ref().map(|f| f.name.clone()).into(),
                            field.as_ref().map(|f| f.spec.clone()).into(),
                            field.as_ref().and_then(|f| f.conversion).map(String::from).into(),
                        ])
                    })
                    .collect(),
            )
        });
        assert_eq!(got.dumps(), case[1].dumps(), "{template:?}");
    }
}

#[test]
fn field_names_lists_each_field_in_order() {
    assert_eq!(field_names("{a} {b:>{c}} {a!r} {{d}}").unwrap(), ["a", "b", "a"]);
    assert_eq!(field_names("{").unwrap_err().named(), "ValueError: Single '{' encountered in format string");
}
