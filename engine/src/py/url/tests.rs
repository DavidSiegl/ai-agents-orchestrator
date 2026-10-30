use super::*;
use crate::py::json::Json;

#[test]
fn quote_keeps_only_the_unreserved_characters() {
    assert_eq!(quote("a b/c?d=e&f~g_h.i-j"), "a%20b%2Fc%3Fd%3De%26f~g_h.i-j");
    assert_eq!(quote("Folder é"), "Folder%20%C3%A9");
    assert_eq!(quote(""), "");
}

#[test]
fn urlencode_writes_a_blank_as_a_plus() {
    assert_eq!(urlencode(&[("tree", "building,result")]), "tree=building%2Cresult");
    assert_eq!(urlencode(&[("GIT_REF", "a b+c"), ("x y", "")]), "GIT_REF=a+b%2Bc&x+y=");
    assert_eq!(urlencode(&[]), "");
}

/// The strings of an oracle case's list.
fn strings(v: &Json) -> Vec<String> {
    v.as_array().expect("a list").iter().map(|p| p.as_str().expect("a string").to_string()).collect()
}

#[test]
fn urlsplit_splits_as_python_does() {
    for case in crate::py::oracle("urlsplit") {
        let case = case.as_array().unwrap();
        let url = case[0].as_str().unwrap();
        let got = urlsplit(url);
        assert_eq!(vec![got.scheme, got.netloc, got.path, got.query, got.fragment], strings(&case[1]), "{url:?}");
    }
}

#[test]
fn parse_qsl_reads_as_python_does() {
    for case in crate::py::oracle("parse_qsl") {
        let case = case.as_array().unwrap();
        let query = case[0].as_str().unwrap();
        let pairs: Vec<(String, String)> = case[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| {
                let p = strings(p);
                (p[0].clone(), p[1].clone())
            })
            .collect();
        assert_eq!(parse_qsl(query), pairs, "{query:?}");
    }
}
