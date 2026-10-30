//! JSON text as Python's json module writes it, which the recordings were made with: the spacing of json.dumps, its
//! escapes with and without ensure_ascii, and json.dump(indent=1, ensure_ascii=False) for a recording itself. Floats
//! are written as serde_json writes them, which can differ from Python's repr; no recording holds one.

use serde_json::Value;

/// value as json.dumps(value) writes it: ", " between items, ": " after keys, and every character outside printable
/// ASCII escaped.
pub fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write(value, &mut out, None, 0, true);
    out
}

/// value as json.dump(value, indent=1, ensure_ascii=False) writes a recording: each item of a non-empty array or
/// object on a line of its own, indented a space a level, and characters beyond ASCII as they are.
pub fn dumps_recording(value: &Value) -> String {
    let mut out = String::new();
    write(value, &mut out, Some(1), 0, false);
    out
}

/// value as json.dumps(value, sort_keys=True) writes it: dumps, with every object's keys in order. Two values give
/// the same text exactly when they are equal, whatever order their keys were written in.
pub fn dumps_sorted(value: &Value) -> String {
    dumps(&sorted(value))
}

fn sorted(value: &Value) -> Value {
    match value {
        Value::Array(items) => Value::Array(items.iter().map(sorted).collect()),
        Value::Object(map) => {
            let mut entries: Vec<_> = map.iter().collect();
            entries.sort_by(|a, b| a.0.cmp(b.0));
            Value::Object(entries.into_iter().map(|(k, v)| (k.clone(), sorted(v))).collect())
        }
        other => other.clone(),
    }
}

/// Write value at out's end: indent spaces a level (None for one line), level being how many arrays and objects hold
/// it, ascii as json.dumps's ensure_ascii.
fn write(value: &Value, out: &mut String, indent: Option<usize>, level: usize, ascii: bool) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_string(s, out, ascii),
        Value::Array(items) => {
            write_items(out, ('[', ']'), items.len(), indent, level, |i, out| {
                write(&items[i], out, indent, level + 1, ascii);
            });
        }
        Value::Object(map) => {
            let entries: Vec<_> = map.iter().collect();
            write_items(out, ('{', '}'), entries.len(), indent, level, |i, out| {
                write_string(entries[i].0, out, ascii);
                out.push_str(": ");
                write(entries[i].1, out, indent, level + 1, ascii);
            });
        }
    }
}

/// Write n items between brackets, each by write_item given its index, as write lays them out.
fn write_items(
    out: &mut String,
    brackets: (char, char),
    n: usize,
    indent: Option<usize>,
    level: usize,
    mut write_item: impl FnMut(usize, &mut String),
) {
    out.push(brackets.0);
    if n == 0 {
        out.push(brackets.1);
        return;
    }
    let (separator, close) = match indent {
        Some(k) => (format!(",\n{}", " ".repeat(k * (level + 1))), format!("\n{}", " ".repeat(k * level))),
        None => (", ".to_string(), String::new()),
    };
    if let Some(k) = indent {
        out.push('\n');
        out.push_str(&" ".repeat(k * (level + 1)));
    }
    for i in 0..n {
        if i > 0 {
            out.push_str(&separator);
        }
        write_item(i, out);
    }
    out.push_str(&close);
    out.push(brackets.1);
}

/// Write s as a JSON string: `"` and `\` escaped, the five control characters JSON names by a letter so, every other
/// one below space as `\u00XX`; with ascii also every character from DEL up, beyond U+FFFF as a surrogate pair.
fn write_string(s: &str, out: &mut String, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if c < ' ' || (ascii && c > '~') => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn dumps_spaces_and_escapes_as_python() {
        let value = json!({"a": [1, "é\n\u{1b}", null, true], "b": {}, "c": [], "d": "🙂"});
        assert_eq!(
            dumps(&value),
            "{\"a\": [1, \"\\u00e9\\n\\u001b\", null, true], \"b\": {}, \"c\": [], \"d\": \"\\ud83d\\ude42\"}"
        );
    }

    #[test]
    fn a_recording_is_indented_by_a_space_and_keeps_unicode() {
        let value = json!({"a": [1, {"x": "é"}], "b": [], "c": {}});
        assert_eq!(
            dumps_recording(&value),
            "{\n \"a\": [\n  1,\n  {\n   \"x\": \"é\"\n  }\n ],\n \"b\": [],\n \"c\": {}\n}"
        );
    }

    #[test]
    fn sorted_text_ignores_the_order_keys_were_written_in() {
        assert_eq!(dumps_sorted(&json!({"b": 1, "a": {"d": 2, "c": 3}})), r#"{"a": {"c": 3, "d": 2}, "b": 1}"#);
    }
}
