use super::*;

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
