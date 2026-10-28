use super::*;
use crate::quality::{parse_gate, QUALITY_BUILD_FAILED, QUALITY_ERROR, QUALITY_LEAK, QUALITY_OK};
use crate::test_support::Rng;

#[test]
fn verdicts() {
    assert_eq!(parse_verdict("VERDICT: APPROVE\n"), Some(APPROVE));
    assert_eq!(parse_verdict("\n\n# VERDICT: CHANGES_REQUESTED\n1. x"), Some(CHANGES_REQUESTED));
    assert_eq!(parse_verdict("Looks good.\nVERDICT: APPROVE"), None);
    assert_eq!(parse_verdict(""), None);
    assert_eq!(parse_verdict("VERDICT: APPROVED"), None);
    assert_eq!(parse_verdict("_VERDICT: APPROVE_"), Some(APPROVE));
    assert_eq!(parse_verdict("VERDICT: APPROVE_X VERDICT: CHANGES_REQUESTED"), Some(CHANGES_REQUESTED));
}

#[test]
fn blocked_questions() {
    let question = Some("which database?".to_string());
    let no_question = Some(NO_QUESTION.to_string());
    let cases = [
        ("BLOCKED: which database?\nmore", question.clone()),
        ("\n\n  BLOCKED:which database?  \n", question.clone()),
        ("BLOCKED:", no_question.clone()),
        ("BLOCKED:   \nwhy", no_question),
        ("blocked: which database?", None),
        ("Done.\nBLOCKED: which database?", None),
        ("**BLOCKED:** which database?", None),
        ("", None),
        ("\n \n", None),
        ("\u{1c}\n\u{a0}BLOCKED: which database?\u{2028}more", question),
    ];
    for (text, expected) in cases {
        assert_eq!(parse_blocked(text), expected, "{text:?}");
    }
}

#[test]
fn a_labelled_word_says_where_it_stands() {
    let found = labelled_word("x GATE:  OK_ y", "GATE", &[QUALITY_OK]).unwrap();
    assert_eq!((found.start, found.word_start, found.end, found.word), (2, 9, 11, "OK"));
    assert_eq!(labelled_word("GATE:é OK", "GATE", &[QUALITY_OK]), None);
}

// -- tests/test_properties.py's TestFirstLineParsers, with a seeded generator in place of Hypothesis ---------------

const GATES: [&str; 4] = [QUALITY_OK, QUALITY_ERROR, QUALITY_BUILD_FAILED, QUALITY_LEAK];
const VERDICTS: [&str; 2] = [APPROVE, CHANGES_REQUESTED];
const LINE_BREAKS: &str = "\n\r\x0b\x0c\x1c\x1d\x1e\u{85}\u{2028}\u{2029}";
const EMPHASIS: [(&str, &str); 7] = [("", ""), ("**", "**"), ("_", "_"), ("`", "`"), ("# ", ""), ("## ", ""), ("> ", "")];

fn blank_lines(rng: &mut Rng) -> String {
    (0..rng.below(3)).map(|_| format!("{}\n", rng.text(2, &format!("{LINE_BREAKS}abcdefghijklmnopqrstuvwxyz")).replace(|c: char| !c.is_whitespace(), " "))).collect()
}

fn after_word(rng: &mut Rng) -> String {
    if rng.below(2) == 0 { String::new() } else { format!(" {}", rng.text(8, LINE_BREAKS)) }
}

#[test]
fn never_panic_and_return_only_their_words() {
    let mut rng = Rng::new(10);
    for _ in 0..500 {
        let text = rng.text(30, "");
        assert!(parse_gate(&text).is_none_or(|g| GATES.contains(&g)));
        assert!(parse_verdict(&text).is_none_or(|v| VERDICTS.contains(&v)));
    }
}

#[test]
fn read_the_word_on_the_first_non_blank_line() {
    let mut rng = Rng::new(11);
    for k in 0..300 {
        let (blanks, (before, behind)) = if k == 0 { (String::new(), ("_", "_")) } else { (blank_lines(&mut rng), *rng.pick(&EMPHASIS)) };
        let (gate, verdict) = (*rng.pick(&GATES), *rng.pick(&VERDICTS));
        let (after, rest) = if k == 0 { (String::new(), String::new()) } else { (after_word(&mut rng), rng.text(20, "")) };
        assert_eq!(parse_gate(&format!("{blanks}{before}GATE: {gate}{behind}{after}\n{rest}")), Some(gate));
        assert_eq!(parse_verdict(&format!("{blanks}{before}VERDICT: {verdict}{behind}{after}\n{rest}")), Some(verdict));
    }
}

#[test]
fn refuse_a_word_that_goes_on() {
    let mut rng = Rng::new(12);
    for k in 0..300 {
        let going_on = if k == 0 {
            "D_WITH_CHANGES".to_string()
        } else {
            let alnum: Vec<char> = ('a'..='z').chain('A'..='Z').chain('0'..='9').collect();
            format!("{}{}{}", "_".repeat(rng.below(3)), rng.pick(&alnum), rng.text(8, LINE_BREAKS))
        };
        if going_on.contains("GATE:") || going_on.contains("VERDICT:") {
            continue;
        }
        let (blanks, rest) = (blank_lines(&mut rng), rng.text(20, ""));
        let (gate, verdict) = (*rng.pick(&GATES), *rng.pick(&VERDICTS));
        assert_eq!(parse_gate(&format!("{blanks}GATE: {gate}{going_on}\n{rest}")), None, "{going_on:?}");
        assert_eq!(parse_verdict(&format!("{blanks}VERDICT: {verdict}{going_on}\n{rest}")), None, "{going_on:?}");
    }
}

#[test]
fn ignore_every_line_below_the_first_non_blank_one() {
    let mut rng = Rng::new(13);
    for _ in 0..300 {
        let first = rng.text(20, LINE_BREAKS);
        if strip(&first).is_empty() {
            continue;
        }
        let (blanks, rest) = (blank_lines(&mut rng), format!("GATE: OK VERDICT: APPROVE {}", rng.text(20, "")));
        assert_eq!(parse_gate(&format!("{blanks}{first}\n{rest}")), parse_gate(&first));
        assert_eq!(parse_verdict(&format!("{blanks}{first}\n{rest}")), parse_verdict(&first));
    }
}
