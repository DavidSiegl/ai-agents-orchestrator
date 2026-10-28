//! The verdict step's proofs (Pipeline::proof_command): the records of their runs (RunState::proofs) and the section
//! of a review that says what they showed.

use crate::constants::PROOF_COPIES_DIR;
use crate::py::json::{Json, Map};
use crate::py::text::decimal_value;

/// The heading of the section Workflow::judge_proofs appends to a review, which a Reviewer's own section is unlikely
/// to take.
pub const PROOFS_HEADING: &str = "## Proofs the orchestrator ran";
/// Seconds one proof may run.
pub const PROOF_TIMEOUT: u64 = 300;
/// How many lines of its output the review quotes.
pub const PROOF_TAIL_LINES: usize = 10;
/// A shell script that removes the directory "$1" names and everything under it, if it exists, failing as rm does.
/// Write permission is given back first, as far as chmod can: a proof may leave a directory it made read-only, as a
/// test of a permission error does.
pub const REMOVE_TREE_SCRIPT: &str = r#"{ [ ! -e "$1" ] || chmod -R u+rwX -- "$1" 2>/dev/null; }; rm -rf -- "$1""#;

/// The number of the finding a proof file is for: the first number in its name, as in test_3_rate.py; None for a name
/// without one.
pub fn proof_finding(file: &str) -> Option<u64> {
    let start = file.find(|c| decimal_value(c).is_some())?;
    let mut n: u64 = 0;
    for c in file[start..].chars() {
        let Some(d) = decimal_value(c) else { break };
        n = n.saturating_mul(10).saturating_add(d.into());
    }
    Some(n)
}

/// The round whose verdict step wrote the proof a RunState::proofs record is a run of: its written, or for a record
/// saved before there was one, the round it ran in.
pub fn proof_written(proof: &Map<Json>) -> Option<i128> {
    proof.get("written").or_else(|| proof.get("round")).and_then(Json::as_int)
}

/// Whether the run a RunState::proofs record holds passed: exited 0, neither failing nor erroring.
pub fn proof_passed(proof: &Map<Json>) -> bool {
    proof.get("said").and_then(Json::as_str).is_some_and(|said| proof_outcome(said) == "it passes")
}

/// How a proof exited, from a record's said: "it fails", "it passes" or how it errored.
pub fn proof_outcome(said: &str) -> &str {
    said.split('\n').next().unwrap_or_default().trim_end_matches(':')
}

/// The lines a proof printed last, from a record's said, fenced and indented under its list item; none when it
/// printed nothing.
pub fn proof_output(said: &str) -> Vec<String> {
    let Some((_, printed)) = said.split_once('\n') else { return Vec::new() };
    let mut out = vec![String::new(), "  ````".to_string()];
    out.extend(printed.split('\n').map(|line| format!("  {line}")));
    out.push("  ````".to_string());
    out
}

/// The section Workflow::judge_proofs appends to a review: for each of proofs, the round's records in
/// RunState::proofs, whether it demonstrates its finding, and the review's numbered findings left without one, which
/// stand as unproven. command ran each in cwd.
pub fn proofs_section(command: &str, cwd: &str, review: &str, proofs: &[Map<Json>]) -> String {
    let mut out = vec![
        PROOFS_HEADING.to_string(),
        String::new(),
        format!(
            "The orchestrator ran each proof with `{command}` in {cwd}, from a copy of its directory under \
             `{cwd}/{PROOF_COPIES_DIR}/`, removed after each run; a path to a proof in its output is the copy's. A \
             proof that fails demonstrates its finding; one that passes or errors does not."
        ),
    ];
    for p in proofs {
        let text = |key: &str| p.get(key).and_then(Json::as_str).unwrap_or_default();
        let said = text("said");
        let finding = match p.get("finding") {
            None | Some(Json::Null) => "no finding".to_string(),
            Some(n) => format!("finding {}", n.dumps()),
        };
        let demonstrated = if p.get("demonstrated").is_some_and(Json::truthy) { "demonstrated" } else { "not demonstrated" };
        out.push(String::new());
        out.push(format!("- `{}` ({finding}): {demonstrated}, {}.", text("file"), proof_outcome(said)));
        out.extend(proof_output(said));
    }
    let proven: Vec<Option<i128>> = proofs.iter().map(|p| p.get("finding").and_then(Json::as_int)).collect();
    let unproven: Vec<String> = numbered_findings(review)
        .into_iter()
        .filter(|k| !proven.contains(&Some(*k)))
        .map(|k| k.to_string())
        .collect();
    if !unproven.is_empty() {
        out.push(String::new());
        out.push(format!("Findings without a proof, unproven: {}.", unproven.join(", ")));
    }
    out.join("\n") + "\n"
}

/// The number of each line of review that starts with one and ". ", as re.findall(r"^(\d+)\. ", review,
/// re.MULTILINE) finds them: lines end at "\n" alone, and the digits are Python's decimal ones.
fn numbered_findings(review: &str) -> Vec<i128> {
    review
        .split('\n')
        .filter_map(|line| {
            let digits: Vec<u32> = line.chars().map_while(decimal_value).collect();
            let rest = &line[line.char_indices().nth(digits.len()).map_or(line.len(), |(i, _)| i)..];
            (!digits.is_empty() && rest.starts_with(". "))
                .then(|| digits.iter().fold(0i128, |n, d| n.saturating_mul(10).saturating_add((*d).into())))
        })
        .collect()
}

#[cfg(test)]
mod tests;
