//! gate-rust run as gate.py runs it, over the fixtures in tests/fixtures, one per part of the schema
//! (docs/quality-gate.md, "Reading Rust: the gate-rust helper"), and over files made here that no fixture can be.

use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

use serde_json::{json, Value};

/// gate-rust run over `paths`, with what it printed and its exit status.
fn run(paths: &[&Path]) -> Output {
    let out = Command::new(env!("CARGO_BIN_EXE_gate-rust")).args(paths).output();
    out.expect("gate-rust could not be started")
}

/// The documents gate-rust printed for `paths`, one per line, having exited 0.
fn documents(paths: &[&Path]) -> Vec<Value> {
    let out = run(paths);
    assert!(out.status.success(), "gate-rust failed: {out:?}");
    let text = String::from_utf8(out.stdout).expect("gate-rust printed what is not UTF-8");
    text.lines().map(|line| serde_json::from_str(line).expect("a line that is not JSON")).collect()
}

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures").join(name)
}

/// The document of the fixture called `name`.
fn read(name: &str) -> Value {
    let [doc] = documents(&[&fixture(name)]).try_into().expect("one document per file");
    doc
}

/// A file called `name`, holding `bytes`, in a directory of this test's own.
fn scratch_file(test: &str, name: &str, bytes: &[u8]) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(test);
    std::fs::create_dir_all(&dir).expect("cannot make the scratch directory");
    let path = dir.join(name);
    std::fs::write(&path, bytes).expect("cannot write the scratch file");
    path
}

/// The one `fn` called `name` in `doc`.
fn function<'a>(doc: &'a Value, name: &str) -> &'a Value {
    let found: Vec<&Value> = doc["fns"].as_array().unwrap().iter().filter(|f| f["name"] == name).collect();
    assert_eq!(found.len(), 1, "fns called {name}: {found:?}");
    found[0]
}

/// The (name, line, attrs_line, end) of each of `doc`'s fns, in order.
fn spans(doc: &Value) -> Vec<(String, u64, u64, u64)> {
    doc["fns"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| {
            let n = |key: &str| f[key].as_u64().unwrap();
            (f["name"].as_str().unwrap().to_owned(), n("line"), n("attrs_line"), n("end"))
        })
        .collect()
}

#[test]
fn every_fn_with_a_body_or_a_place_for_one_is_listed_with_its_lines() {
    let doc = read("fns.rs");
    assert_eq!(doc["schema"], 1);
    let expected = [
        ("free", 5, 3, 5),
        ("method", 10, 10, 12),
        ("nested", 11, 11, 11),
        ("declared", 16, 16, 16),
        ("defaulted", 17, 17, 18),
        ("fmt", 22, 22, 27),
        ("in_closure", 24, 24, 24),
        ("in_const", 31, 31, 31),
        ("spread", 35, 35, 37),
        ("loop", 40, 40, 40),
    ];
    let expected: Vec<_> = expected.iter().map(|&(n, l, a, e)| (n.to_owned(), l, a, e)).collect();
    // Neither the extern block's abs nor the macro's made.
    assert_eq!(spans(&doc), expected);
}

#[test]
fn a_fn_names_the_items_that_hold_it_outermost_first() {
    let doc = read("fns.rs");
    assert_eq!(function(&doc, "free")["holders"], json!([]));
    assert_eq!(function(&doc, "method")["holders"], json!([{"kind": "impl", "name": "Holder", "trait": null}]));
    assert_eq!(
        function(&doc, "nested")["holders"],
        json!([{"kind": "impl", "name": "Holder", "trait": null}, {"kind": "fn", "name": "method"}])
    );
    assert_eq!(function(&doc, "declared")["holders"], json!([{"kind": "trait", "name": "Shape"}]));
    assert_eq!(
        function(&doc, "in_closure")["holders"],
        json!([{"kind": "impl", "name": "Holder", "trait": "std :: fmt :: Debug"}, {"kind": "fn", "name": "fmt"}])
    );
    assert_eq!(function(&doc, "in_const")["holders"], json!([{"kind": "const", "name": "TABLE"}]));
    assert_eq!(function(&doc, "spread")["holders"], json!([{"kind": "mod", "name": "inner"}]));
    assert_eq!(doc["mods"], json!([{"name": "inner", "line": 34, "attrs_line": 34, "end": 38, "cfg_test": false}]));
}

#[test]
fn a_fn_lists_its_attributes_as_written_but_its_doc_comments() {
    let doc = read("test_attrs.rs");
    assert_eq!(function(&doc, "passes")["attrs"], json!([{"path": "test", "text": "#[test]", "line": 5}]));
    let weighed = function(&doc, "weighed");
    assert_eq!(
        weighed["attrs"],
        json!([
            {"path": "test", "text": "#[test]", "line": 9},
            {"path": "ignore", "text": "#[ignore = \"slow\"]", "line": 10},
            {"path": "should_panic", "text": "#[should_panic(expected = \"boom\")]", "line": 11},
            {"path": "cfg", "text": "#[cfg(windows)]", "line": 12},
        ])
    );
    assert_eq!((weighed["attrs_line"].as_u64(), weighed["line"].as_u64()), (Some(8), Some(13)));
    assert_eq!(function(&doc, "awaits")["attrs"], json!([{"path": "tokio::test", "text": "#[tokio::test]", "line": 15}]));
}

#[test]
fn a_module_is_cfg_test_when_its_cfg_holds_only_under_test() {
    let doc = read("test_attrs.rs");
    let mods: Vec<(&str, bool)> = doc["mods"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| (m["name"].as_str().unwrap(), m["cfg_test"].as_bool().unwrap()))
        .collect();
    // A cfg inside the module counts as one above it; any() and not() can hold without test, and cfg_attr is no
    // cfg; a declared module is in another file.
    assert_eq!(
        mods,
        [
            ("plain", true),
            ("deeper", false),
            ("unix_tests", true),
            ("inner_cfg", true),
            ("any_test", false),
            ("not_test", false),
            ("attr_only", false),
        ]
    );
    assert_eq!(doc["mods"][0], json!({"name": "plain", "line": 4, "attrs_line": 3, "end": 19, "cfg_test": true}));
}

#[test]
fn each_assertion_macro_is_listed_with_its_text_shape_and_literals() {
    let doc = read("asserts.rs");
    let asserts = function(&doc, "asserts")["asserts"].as_array().unwrap();
    let names: Vec<(&str, u64)> =
        asserts.iter().map(|a| (a["name"].as_str().unwrap(), a["line"].as_u64().unwrap())).collect();
    // Not tokio::select!'s, whose tokens are no expressions, nor println!, nor the nested fn's.
    assert_eq!(
        names,
        [
            ("assert", 4),
            ("assert_eq", 5),
            ("assert_ne", 6),
            ("debug_assert", 7),
            ("debug_assert_eq", 8),
            ("debug_assert_ne", 9),
            ("assert_eq", 13),
            ("assert", 14),
            ("panic", 15),
            ("assert", 16),
            ("assert_eq", 17),
        ]
    );
    assert_eq!(
        asserts[1],
        json!({
            "name": "assert_eq",
            "line": 5,
            "text": "assert_eq!(count, -2, \"count {}\", r#\"raw \"quoted\"\"#)",
            "shape": "count , - $lit , $lit , $lit",
            "literals": ["2", "\"count {}\"", "r#\"raw \"quoted\"\"#"],
        })
    );
    assert_eq!(asserts[2]["literals"], json!(["b'x'"]));
    assert_eq!(asserts[5]["text"], "debug_assert_ne!(\n        list,\n        vec![1, 2],\n    )");
    assert_eq!(asserts[5]["shape"], "list , vec ! [$lit , $lit] ,");
    assert_eq!(asserts[6]["text"], "pretty_assertions::assert_eq!(left, \"right\")");
    assert_eq!(asserts[7]["shape"], "matches ! (found , Some ($lit) | None)");
    assert_eq!(asserts[8]["literals"], json!(["\"never {}\"", "'c'"]));
    assert_eq!(function(&doc, "helper")["asserts"][0]["text"], "assert!(nested_own)");
}

#[test]
fn the_values_a_body_throws_away_are_listed_by_how() {
    let doc = read("discards.rs");
    assert_eq!(
        function(&doc, "discards")["discards"],
        json!([
            {"kind": "let", "line": 4, "text": "std::panic::catch_unwind(|| run())"},
            {"kind": "let", "line": 5, "text": "fallible()"},
            {"kind": "assign", "line": 6, "text": "parse(\"1\")"},
            {"kind": "ok", "line": 7, "text": "fallible()"},
        ])
    );
    assert_eq!(function(&doc, "helper")["discards"], json!([{"kind": "let", "line": 12, "text": "own()"}]));
}

#[test]
fn each_kind_of_binding_is_listed_but_names_that_start_upper_case_in_a_pattern() {
    let doc = read("bindings.rs");
    let bindings: Vec<(&str, &str, u64)> = doc["bindings"]
        .as_array()
        .unwrap()
        .iter()
        .map(|b| (b["kind"].as_str().unwrap(), b["name"].as_str().unwrap(), b["line"].as_u64().unwrap()))
        .collect();
    assert_eq!(
        bindings,
        [
            ("use-as", "Map", 3),
            ("struct", "Point", 6),
            ("enum", "Shape", 7),
            ("union", "Bits", 8),
            ("trait", "Draw", 9),
            ("const", "SIDES", 9),
            ("type", "Pen", 9),
            ("fn", "draw", 9),
            ("param", "canvas", 9),
            ("type", "Alias", 10),
            ("const", "LIMIT", 11),
            ("static", "COUNTER", 12),
            ("const", "SIDES", 15),
            ("type", "Pen", 16),
            ("fn", "draw", 17),
            ("param", "ref", 17),
            ("fn", "bind", 20),
            ("param", "x", 20),
            ("param", "renamed", 20),
            ("param", "count", 20),
            ("let", "first", 21),
            ("let", "second", 21),
            ("let", "else_bound", 22),
            ("for", "index", 24),
            ("for", "item", 24),
            ("match", "captured", 26),
            ("if-let", "guarded", 26),
            ("if-let", "conditional", 30),
            ("if-let", "chained", 30),
            ("while-let", "looped", 31),
            ("let", "apply", 32),
            ("closure-param", "arg", 32),
            ("closure-param", "left", 32),
            ("closure-param", "right", 32),
            ("if-let", "scrutinee", 33),
            ("let", "inner", 33),
        ]
    );
    assert_eq!(function(&doc, "bind")["holders"], json!([]));
    let draws: Vec<&Value> = doc["fns"].as_array().unwrap().iter().filter(|f| f["name"] == "draw").collect();
    assert_eq!(draws[1]["holders"], json!([{"kind": "impl", "name": "Point", "trait": "Draw"}]));
}

#[test]
fn two_bodies_of_the_same_tokens_have_one_digest_whatever_their_layout() {
    let doc = read("digest.rs");
    let digest = |name: &str| function(&doc, name)["body_digest"].clone();
    assert_eq!(digest("original"), digest("relaid"));
    assert_ne!(digest("original"), digest("edited"));
    assert_eq!(digest("original").as_str().map(str::len), Some(16));
    assert_eq!(digest("declared"), Value::Null);
}

#[test]
fn a_file_that_does_not_parse_is_an_error_document_at_its_line() {
    assert_eq!(read("broken.rs"), json!({"schema": 1, "error": {"message": "expected an expression", "line": 6}}));
}

#[test]
fn a_file_that_ends_early_is_an_error_document_at_its_line_or_none() {
    // A brace left open is the lexer's error, at the brace; an item cut short is the parser's, at no place.
    let open = scratch_file("ends_early", "open.rs", b"fn f() {}\nfn g() {\n    let x = 1;\n");
    let cut = scratch_file("ends_early", "cut.rs", b"fn f() {}\nfn g()\n");
    let docs = documents(&[&open, &cut]);
    assert_eq!(docs[0], json!({"schema": 1, "error": {"message": "cannot parse string into token stream", "line": 2}}));
    assert_eq!(
        docs[1],
        json!({"schema": 1, "error": {"message": "unexpected end of input, expected curly braces", "line": null}})
    );
}

#[test]
fn a_file_that_cannot_be_read_or_is_not_utf8_is_an_error_document_with_no_line() {
    let latin1 = scratch_file("not_utf8", "latin1.rs", b"// caf\xe9\nfn f() {}\n");
    let missing = latin1.with_file_name("missing.rs");
    let docs = documents(&[&latin1, &missing]);
    assert_eq!(docs[0]["error"]["line"], Value::Null);
    assert!(docs[0]["error"]["message"].as_str().unwrap().starts_with("is not UTF-8: "), "{}", docs[0]);
    assert_eq!(docs[1]["error"]["line"], Value::Null);
    assert!(docs[1]["error"]["message"].as_str().unwrap().starts_with("could not be read: "), "{}", docs[1]);
}

#[test]
fn lines_are_counted_at_a_newline_alone() {
    // A lone carriage return is whitespace inside a comment, and a line separator is a character like any other.
    let path = scratch_file("newline_alone", "ends.rs", "// a\rb\u{2028}c\r\nfn f() {}\n".as_bytes());
    let [doc] = documents(&[&path]).try_into().unwrap();
    assert_eq!(spans(&doc), [("f".to_owned(), 2, 2, 2)]);
}

#[test]
fn the_documents_come_in_the_order_of_the_files_and_none_for_no_file() {
    let docs = documents(&[&fixture("broken.rs"), &fixture("digest.rs"), &fixture("broken.rs")]);
    assert_eq!(docs.len(), 3);
    assert!(docs[0]["error"].is_object() && docs[2]["error"].is_object());
    assert_eq!(docs[1]["fns"][0]["name"], "original");
    let out = run(&[]);
    assert!(out.status.success() && out.stdout.is_empty(), "{out:?}");
}

#[cfg(target_os = "linux")]
#[test]
fn output_that_cannot_be_written_exits_1_saying_so() {
    let full = std::fs::File::create("/dev/full").expect("Linux has /dev/full");
    let out = Command::new(env!("CARGO_BIN_EXE_gate-rust"))
        .arg(fixture("fns.rs"))
        .stdout(Stdio::from(full))
        .stderr(Stdio::piped())
        .output()
        .expect("gate-rust could not be started");
    assert_eq!(out.status.code(), Some(1));
    let said = String::from_utf8_lossy(&out.stderr);
    assert!(said.starts_with("gate-rust: cannot write to standard output: "), "{said}");
}
