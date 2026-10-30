//! The texts a run publishes its change with: its branch's name, its commit's note and trailers, and its pull
//! request's body, all from its RunState and handoff files.

use crate::constants::{
    APPROVE, BRANCH_PREFIX, DEFAULT_AGENT, FINISHED, PR_SECTION_LIMIT, QUALITY_GATE_FAILED, SUCCEEDED,
};
use crate::py::text::{is_space, splitlines, strip};
use crate::quality::parse_gate;
use crate::state::RunState;
use crate::summary::turns_table;

/// The `# ` heading on the spec's first non-blank line, without the blanks around it; None when that line is no
/// such heading, or the spec is blank.
pub fn spec_title(spec: &str) -> Option<String> {
    let line = strip(splitlines(spec).into_iter().find(|line| !strip(line).is_empty())?);
    let rest = line.strip_prefix('#')?;
    let title = rest.trim_start_matches(is_space);
    (title.len() < rest.len() && !title.is_empty()).then(|| strip(title).to_string())
}

/// The branch a run publishes its change on: BRANCH_PREFIX, the title lower-cased with each run of characters other
/// than a-z and 0-9 as one "-", cut to 40 characters and without a "-" at its ends, then "-" and the run's key; the
/// key alone when the title leaves nothing.
pub fn branch_name(title: &str, run_id: &str) -> String {
    let mut slug = String::new();
    for c in title.to_lowercase().chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            slug.push(c);
        } else if !slug.ends_with('-') {
            slug.push('-');
        }
    }
    let slug: String = slug.chars().take(40).collect();
    let slug = slug.trim_matches('-');
    let key = run_id.rsplit('-').next().unwrap_or(run_id);
    if slug.is_empty() {
        format!("{BRANCH_PREFIX}{key}")
    } else {
        format!("{BRANCH_PREFIX}{slug}-{key}")
    }
}

/// A run's verdict as Python's str() writes it in a text: None when it has none.
fn verdict_text(state: &RunState) -> &str {
    state.verdict.as_deref().unwrap_or("None")
}

/// How the run ended, for its pull request, and why it is a draft if it is one for that.
pub fn verdict_line(state: &RunState) -> String {
    match state.verdict.as_deref() {
        Some(FINISHED) => format!("**{FINISHED}**: its workflow has no review step, so no agent reviewed this change."),
        Some(QUALITY_GATE_FAILED) => format!(
            "**{QUALITY_GATE_FAILED}**: its workflow has no review step, and the quality gate still did not pass after \
             the last quality round, so this is a draft."
        ),
        verdict => {
            let n = state.round;
            let mut line = format!(
                "Reviewer verdict after {n} round{}: **{}**.",
                if n != 1 { "s" } else { "" },
                verdict_text(state)
            );
            if verdict != Some(APPROVE) {
                line.push_str("\n\nThe Reviewer still requested changes after the last round, so this is a draft.");
            }
            line
        }
    }
}

/// How the run ended, for its commit message.
pub fn commit_note(state: &RunState) -> String {
    match state.verdict.as_deref() {
        Some(FINISHED | QUALITY_GATE_FAILED) => {
            format!("Orchestrator run {}: {}, unreviewed.", state.run_id, verdict_text(state))
        }
        _ => format!(
            "Orchestrator run {}: {} after {} review round(s).",
            state.run_id,
            verdict_text(state),
            state.round
        ),
    }
}

/// A quality file of a run, as its commit and pull request cite it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct QualityFile {
    /// The review round it belongs to.
    pub round: u32,
    pub quality_round: u32,
    pub text: String,
}

/// What the quality gate said of the change a run publishes, for its commit trailers and pull request (provenance,
/// pr_body); Workflow::gate_evidence decides it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct GateEvidence {
    /// The quality file they cite: the final round's last one, or for an unjudged change the run's last one from any
    /// round; None when there is none to cite.
    pub last: Option<QualityFile>,
    /// True when resume --no-quality-gate turned the gate off and no quality file judges the published code
    /// (Workflow::unjudged): last, if any, judged code the Builder has changed since.
    pub unjudged: bool,
}

/// The git trailers of the run's commit, (key, value) in order: Orchestrator-Run, Orchestrator-Workflow, an
/// Orchestrator-Role per role whose agent ran, in the order they first started, as role=harness/model, the model
/// "default" where none was set, and with a quality file to cite Orchestrator-Gate, how its quality round ended,
/// with ", then turned off" for an unjudged change.
///
/// A role that ran in two harnesses over resumes names the last. docs/design.md says what each trailer means.
pub fn provenance(state: &RunState, quality: &GateEvidence) -> Vec<(String, String)> {
    let s = state;
    let mut trailers = vec![
        ("Orchestrator-Run".to_string(), s.run_id.clone()),
        ("Orchestrator-Workflow".to_string(), s.workflow.clone()),
    ];
    for (role, agent) in s.agents.iter() {
        let kind = agent.get("kind").map_or(DEFAULT_AGENT, String::as_str);
        let model = s.models.get(role).filter(|m| !m.is_empty()).map_or("default", String::as_str);
        trailers.push(("Orchestrator-Role".to_string(), format!("{role}={kind}/{model}")));
    }
    if let Some(last) = &quality.last {
        let q = last.quality_round;
        let rounds = format!("{q} quality round{}", if q != 1 { "s" } else { "" });
        let off = if quality.unjudged { ", then turned off" } else { "" };
        let gate = parse_gate(&last.text).unwrap_or("?");
        trailers.push(("Orchestrator-Gate".to_string(), format!("{gate} after {rounds}{off}")));
    }
    trailers
}

/// The paragraph that opens the body of a pull request opened over conflicts with base_branch in the files
/// conflicts, which must not be empty; without_conflict_warning takes it out again. A base branch that is None reads
/// as "None", as in the Python engine.
pub fn conflict_warning(base_branch: Option<&str>, conflicts: &[String]) -> String {
    let base = base_branch.unwrap_or("None");
    let files: Vec<String> = conflicts.iter().map(|f| format!("> - `{f}`")).collect();
    format!(
        "> [!WARNING]\n> This branch conflicts with `{base}`, so this is a draft. Merge `{base}` into it and resolve \
         the conflicts in:\n{}",
        files.join("\n")
    )
}

/// The length of the line end at body[at..], "\n" or "\r\n"; None where none is.
fn line_end(body: &str, at: usize) -> Option<usize> {
    let rest = body.get(at..)?;
    if rest.starts_with("\r\n") {
        Some(2)
    } else if rest.starts_with('\n') {
        Some(1)
    } else {
        None
    }
}

/// The end of the warning's lines matched at body[at..], each line ending in "\n" or "\r\n" as GitHub's editor
/// leaves them; None when they do not stand there.
fn block_end(body: &str, at: usize, lines: &[&str]) -> Option<usize> {
    let mut pos = at;
    for (i, line) in lines.iter().enumerate() {
        if i > 0 {
            pos += line_end(body, pos)?;
        }
        if !body.get(pos..)?.starts_with(line) {
            return None;
        }
        pos += line.len();
    }
    Some(pos)
}

/// Whether body[at..] is all line ends that `\r?\n?\Z` takes: nothing, "\n", "\r" or "\r\n".
fn ends_body(body: &str, at: usize) -> bool {
    matches!(body.get(at..), Some("" | "\n" | "\r" | "\r\n"))
}

/// The length of the blank line body[at..] starts with: two line ends as line_end takes them; None where none is.
fn blank_line(body: &str, at: usize) -> Option<usize> {
    line_end(body, at).and_then(|a| line_end(body, at + a).map(|b| a + b))
}

/// body without the warning's lines and the blank line after them, or without them and the rest when they end the
/// body, where they stand at body[at..] as a paragraph of their own: at the start or after a blank line. None when
/// they do not stand there so.
fn without_own_paragraph(body: &str, at: usize, lines: &[&str]) -> Option<String> {
    let opens_paragraph = at == 0 || body[..at].ends_with("\n\n") || body[..at].ends_with("\n\r\n");
    if !opens_paragraph {
        return None;
    }
    let end = block_end(body, at, lines)?;
    if let Some(blank) = blank_line(body, end) {
        return Some(format!("{}{}", &body[..at], &body[end + blank..]));
    }
    ends_body(body, end).then(|| body[..at].to_string())
}

/// body without the blank line at body[at..] and the warning's lines after it, where those lines end the body; None
/// when they do not stand there so.
fn without_closing_paragraph(body: &str, at: usize, lines: &[&str]) -> Option<String> {
    let blank = blank_line(body, at)?;
    let end = block_end(body, at + blank, lines)?;
    ends_body(body, end).then(|| format!("{}{}", &body[..at], &body[end..]))
}

/// body with its first conflict_warning paragraph for base_branch and conflicts removed, and the blank line after it,
/// or before it when it ends the body; body unchanged when it has no such paragraph. Its lines may end in \r\n, as
/// GitHub's editor leaves them, and the paragraph's own lines must be as conflict_warning wrote them.
pub fn without_conflict_warning(body: &str, base_branch: Option<&str>, conflicts: &[String]) -> String {
    let warning = conflict_warning(base_branch, conflicts);
    let lines: Vec<&str> = warning.split('\n').collect();
    // The earliest place either form of the paragraph stands, the paragraph of its own first where both do.
    (0..=body.len())
        .filter(|&i| body.is_char_boundary(i))
        .find_map(|at| without_own_paragraph(body, at, &lines).or_else(|| without_closing_paragraph(body, at, &lines)))
        .unwrap_or_else(|| body.to_string())
}

/// text as a collapsible section of the pull request, open or closed, cut at PR_SECTION_LIMIT characters with a
/// note that it was.
fn details(summary: &str, text: &str, open: bool) -> String {
    let cut;
    let text = if text.chars().count() > PR_SECTION_LIMIT {
        let kept: String = text.chars().take(PR_SECTION_LIMIT).collect();
        cut = format!("{kept}\n\n*(truncated; the full file is in the run directory)*");
        cut.as_str()
    } else {
        text
    };
    format!(
        "<details{}>\n<summary>{summary}</summary>\n\n{}\n\n</details>",
        if open { " open" } else { "" },
        strip(text)
    )
}

/// The pull request's body: the conflict warning and the note on a gate turned off where they apply, how the run
/// ended, its provenance as a table, the spec, the last report, the last quality file, the review and the run's
/// timings. quality is what the gate said of the change, as provenance takes it.
pub fn pr_body(state: &RunState, spec: &str, report: &str, review: &str, quality: &GateEvidence) -> String {
    let s = state;
    let approved = s.verdict.as_deref().is_some_and(|v| SUCCEEDED.contains(&v));
    let head = format!("Opened by Octoflow AI run `{}`. {}", s.run_id, verdict_line(s));
    let mut table = vec!["| Commit trailer | Value |".to_string(), "| --- | --- |".to_string()];
    table.extend(provenance(s, quality).into_iter().map(|(key, said)| format!("| {key} | `{said}` |")));
    let mut parts = Vec::new();
    if !s.conflicts.is_empty() {
        parts.push(conflict_warning(s.base_branch.as_deref(), &s.conflicts));
    }
    if quality.unjudged {
        let when = if quality.last.is_some() {
            "after its last analysis, and the Builder changed the code since, so nothing judged the final change"
        } else {
            "before any analysis finished, so nothing judged the change"
        };
        parts.push(format!("> [!NOTE]\n> The quality gate was turned off (`resume --no-quality-gate`) {when}."));
    }
    parts.push(head);
    parts.push(table.join("\n"));
    parts.push(details("Spec", spec, true));
    parts.push(details(&format!("Builder report (round {})", s.round), report, false));
    if let Some(last) = &quality.last {
        let label = if quality.unjudged {
            format!("round {}, before the last change", last.round)
        } else {
            format!("round {}", last.round)
        };
        parts.push(details(&format!("Quality gate ({label})"), &last.text, false));
    }
    if !matches!(s.verdict.as_deref(), Some(FINISHED | QUALITY_GATE_FAILED)) {
        parts.push(details(&format!("Review (round {})", s.round), review, !approved));
    }
    if !s.turns.is_empty() {
        parts.push(details("Run timings", &turns_table(&s.turns), false));
    }
    parts.join("\n\n") + "\n"
}

#[cfg(test)]
mod tests;
