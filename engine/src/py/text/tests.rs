use super::*;
use crate::py::json::Json;
use crate::py::oracle;

fn s(v: &Json) -> &str {
    v.as_str().expect("a string")
}

fn strings(v: &Json) -> Vec<&str> {
    v.as_array().expect("a list").iter().map(s).collect()
}

#[test]
fn splitlines_breaks_where_python_does() {
    for case in oracle("splitlines") {
        let case = case.as_array().unwrap();
        assert_eq!(splitlines(s(&case[0])), strings(&case[1]), "{:?}", s(&case[0]));
    }
}

#[test]
fn strip_takes_pythons_blanks() {
    for case in oracle("strip") {
        let case = case.as_array().unwrap();
        assert_eq!(strip(s(&case[0])), s(&case[1]), "{:?}", s(&case[0]));
        assert_eq!(lstrip(s(&case[0])), s(&case[2]), "{:?}", s(&case[0]));
    }
}

#[test]
fn splitext_splits_where_python_does() {
    for case in oracle("splitext") {
        let case = case.as_array().unwrap();
        let (stem, ext) = splitext(s(&case[0]));
        assert_eq!(vec![stem, ext], strings(&case[1]), "{:?}", s(&case[0]));
    }
}

#[test]
fn repr_and_ascii_write_what_python_does() {
    for case in oracle("repr") {
        let case = case.as_array().unwrap();
        assert_eq!(repr(s(&case[0])), s(&case[1]));
        assert_eq!(ascii(s(&case[0])), s(&case[2]));
    }
}

#[test]
fn close_match_picks_what_difflib_does() {
    for case in oracle("close_match") {
        let case = case.as_array().unwrap();
        let keys = strings(&case[1]);
        assert_eq!(close_match(s(&case[0]), keys.iter().copied()), case[2].as_str(), "{:?}", s(&case[0]));
    }
}

#[test]
fn decimal_digits_are_pythons() {
    assert_eq!(decimal_value('7'), Some(7));
    assert_eq!(decimal_value('\u{0663}'), Some(3));
    assert_eq!(decimal_value('\u{1d7d8}'), Some(0));
    assert_eq!(decimal_value('\u{00b2}'), None);
    assert_eq!(decimal_value('x'), None);
}

#[test]
fn word_characters_are_pythons() {
    assert!(is_word('_') && is_word('a') && is_word('7') && is_word('é') && is_word('\u{00b2}'));
    assert!(!is_word('-') && !is_word(' ') && !is_word('*') && !is_word('\u{0301}'));
}

#[test]
fn title_capitalises_each_run_of_letters() {
    assert_eq!(title_ascii("test writer"), "Test Writer");
    assert_eq!(title_ascii("a1b2 XY"), "A1B2 Xy");
    assert_eq!(basename("a/b/c.toml"), "c.toml");
    assert_eq!(basename("c"), "c");
}
