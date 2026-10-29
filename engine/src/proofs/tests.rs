use super::*;
use crate::test_support::helper_cases;
use crate::workflow_file::saved_pipeline;

#[test]
fn every_recorded_proof_run_is_reported_as_the_python_engine_does() {
    let mut compared = 0;
    for case in helper_cases() {
        let Some(expected) = case.expected.get("proofs_section").and_then(Json::as_str) else { continue };
        let pipeline = saved_pipeline(&case.saved).unwrap();
        let s = &case.state;
        let proofs: Vec<ProofRun> = s.proofs.iter().map(|p| ProofRun::read(p).unwrap()).collect();
        let section = proofs_section(pipeline.proof_command().unwrap_or(""), s.work_dir(), &case.review, &proofs);
        assert_eq!(section, expected);
        compared += 1;
    }
    assert!(compared > 0, "no recorded run has proofs");
}

fn record(text: &str) -> Map<Json> {
    Json::parse(text).unwrap().as_object().unwrap().clone()
}

fn run(text: &str) -> ProofRun {
    ProofRun::read(&record(text)).unwrap()
}

#[test]
fn a_proofs_section_names_each_proof_and_the_findings_left_unproven() {
    let proofs = [
        run(r#"{"round": 1, "written": 1, "file": "test_1_rate.py", "finding": 1, "demonstrated": true, "said": "it fails:\nE   assert 2 == 3\nlast"}"#),
        run(r#"{"round": 1, "file": "check.sh", "finding": null, "demonstrated": false, "said": "it did not run: gone"}"#),
    ];
    let review = "VERDICT: CHANGES_REQUESTED\n1. rate\n2. burst\n 3. indented\n\u{0663}. arabic\n10.no blank\n";
    let section = proofs_section("sh {proofs}", "/proj", review, &proofs);
    assert_eq!(
        section,
        "## Proofs the orchestrator ran\n\nThe orchestrator ran each proof with `sh {proofs}` in /proj, from a copy of its \
         directory under `/proj/.orchestrator/proofs/`, removed after each run; a path to a proof in its output is the \
         copy's. A proof that fails demonstrates its finding; one that passes or errors does not.\n\n\
         - `test_1_rate.py` (finding 1): demonstrated, it fails.\n\n  ````\n  E   assert 2 == 3\n  last\n  ````\n\n\
         - `check.sh` (no finding): not demonstrated, it did not run: gone.\n\n\
         Findings without a proof, unproven: 2, 3.\n"
    );
    assert_eq!((proofs[0].written, proofs[1].written), (1, 1));
    assert!(!proofs[0].passed());
    assert!(run(r#"{"round": 2, "written": 1, "file": "t", "finding": null, "demonstrated": false, "said": "it passes"}"#).passed());
}

#[test]
fn a_proof_run_is_read_and_written_as_the_python_engine_saves_it() {
    // Written last: a record saved before there was one reads as a first run, and is written with it.
    let old = r#"{"round": 2, "file": "test_3.sh", "finding": 3, "demonstrated": true, "said": "it fails"}"#;
    let read = run(old);
    assert_eq!(read.written, 2);
    assert_eq!(
        Json::Object(read.record()).dumps(),
        r#"{"round": 2, "written": 2, "file": "test_3.sh", "finding": 3, "demonstrated": true, "said": "it fails"}"#
    );
    let refused = [
        (r#"{"written": 1}"#, "a proof run saved in state.json has no round"),
        (r#"{"round": -1}"#, "a proof run saved in state.json has round -1, not a round"),
        (r#"{"round": 1, "file": "t", "finding": "3"}"#, "a proof run saved in state.json has finding \"3\", not a number"),
        (
            r#"{"round": 1, "file": "t", "finding": 3, "demonstrated": 1}"#,
            "a proof run saved in state.json has demonstrated 1, not true or false",
        ),
        (r#"{"round": 1, "file": 2, "finding": null, "demonstrated": true}"#, "a proof run saved in state.json has file 2, not a string"),
    ];
    for (text, error) in refused {
        assert_eq!(ProofRun::read(&record(text)).unwrap_err().0, error, "{text}");
    }
}

#[test]
fn a_proofs_finding_is_the_first_number_in_its_name() {
    assert_eq!(proof_finding("test_3_rate.py"), Some(3));
    assert_eq!(proof_finding("test_12x4.py"), Some(12));
    assert_eq!(proof_finding("check.sh"), None);
    assert_eq!(proof_outcome("it fails:\nline"), "it fails");
    assert!(proof_output("it passes").is_empty());
    assert!(REMOVE_TREE_SCRIPT.starts_with("{ [ ! -e \"$1\" ]") && PROOF_TIMEOUT == 300 && PROOF_TAIL_LINES == 10);
}
