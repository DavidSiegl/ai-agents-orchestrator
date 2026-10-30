//! The orchestrator's checks: what a quality round runs itself after the build's gate, because they need what the
//! build never sees, the run's spec and its workflow (docs/quality-gate.md, "The orchestrator's checks"), and how
//! judge_run adds what they find to the build's quality file.

use std::collections::{BTreeMap, BTreeSet};

use crate::constants::{CRATES_IO_TIMEOUT, PYPI_TIMEOUT};
use crate::error::OrchestratorError;
use crate::handoff::labelled_word;
use crate::host::ChangedFile;
use crate::pipeline::Pipeline;
use crate::py::text::{basename, decimal_value, strip};
use crate::package_index::PackageIndexes;
use crate::quality::{
    one_line, QUALITY_ERROR, QUALITY_FAILED_CONDITIONS, QUALITY_FINDINGS_LIMIT, QUALITY_OK, QUALITY_TO_WEIGH_NOTE,
};

pub mod dependencies;

pub use dependencies::new_dependencies;

/// Seconds of a quality round's QUALITY_TIMEOUT that an orchestrator check leaves unused: it starts no slow step,
/// such as a request to a package index, with less left. A request can take PYPI_TIMEOUT, or CRATES_IO_TIMEOUT, more
/// than once, to connect and to read.
pub const SLOW_STEP_MARGIN: u64 = 4 * if PYPI_TIMEOUT > CRATES_IO_TIMEOUT { PYPI_TIMEOUT } else { CRATES_IO_TIMEOUT };

/// Reads a file of the change: (commit, path) to the file at path, relative to the repository's top level, as
/// commit has it, its bytes as git stores them; None where it has none. Errors: git fails on the agents' machine.
pub type FileAt<'a> = dyn Fn(&str, &str) -> Result<Option<Vec<u8>>, OrchestratorError> + 'a;

/// The change a quality round judges, as each OrchestratorCheck is given it: what the build's gate saw, and what only
/// the run has.
pub struct RunChange<'a> {
    /// The run's spec: its contract step's file, or its task for a workflow without one.
    pub spec: String,
    /// The run's base commit.
    pub base: String,
    /// The snapshot the round judges, whose only parent is base.
    pub commit: String,
    /// The lines each file gains, by path: crate::patch::added_lines of Host::change_diff from base to commit.
    pub changed: BTreeMap<String, BTreeSet<u64>>,
    /// Every file the change touches: Host::changed_line_counts from base to commit.
    pub files: Vec<ChangedFile>,
    /// The files of base and commit, as Host::file_at reads them.
    pub file_at: &'a FileAt<'a>,
    /// When the round judges it, in seconds since the epoch.
    pub now: f64,
    /// Called before each step of a check that may be slow, such as a request to a package index: it keeps the run's heartbeat
    /// going, and is false once the quality round has too little time left to start one (SLOW_STEP_MARGIN).
    pub in_time: &'a dyn Fn() -> bool,
}

/// How much a finding weighs: an error fails the gate, a warning is only reported.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Severity {
    Error,
    Warning,
}

impl Severity {
    /// The word a quality file writes.
    pub fn as_str(self) -> &'static str {
        match self {
            Severity::Error => "error",
            Severity::Warning => "warning",
        }
    }
}

/// A finding of an orchestrator check, of the shape of the gate's checks' findings (gate.py's _finding).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Finding {
    /// The file it is about; None for one about the change as a whole.
    pub path: Option<String>,
    /// The line of path it is about, from 1; None for one about the whole file.
    pub line: Option<u64>,
    pub severity: Severity,
    /// The check's own rule, such as not-in-spec; "" for a check with a single rule. judge_run prefixes it with the
    /// check's name.
    pub rule: String,
    pub message: String,
}

impl Finding {
    /// A finding that is only reported.
    pub fn warning(path: Option<&str>, line: Option<u64>, rule: &str, message: String) -> Finding {
        Finding { path: path.map(str::to_string), line, severity: Severity::Warning, rule: rule.to_string(), message }
    }

    /// A finding that fails the gate.
    pub fn error(path: Option<&str>, line: Option<u64>, rule: &str, message: String) -> Finding {
        Finding { severity: Severity::Error, ..Finding::warning(path, line, rule, message) }
    }

    /// The finding as the k-th of the quality file's numbered list, as gate.py's _numbered writes it and
    /// crate::quality::gate_failures reads it.
    fn numbered(&self, k: u128) -> String {
        let place = match (&self.path, self.line) {
            (None, _) => "The change as a whole:".to_string(),
            (Some(path), None) => format!("`{path} (the whole file)`"),
            (Some(path), Some(line)) => format!("`{path}:{line}`"),
        };
        one_line(&format!("{k}. {place} {} {}: {}", self.severity.as_str(), self.rule, self.message))
    }
}

/// What an OrchestratorCheck found.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CheckFindings {
    /// The findings that fail the gate.
    pub issues: Vec<Finding>,
    /// Those that do not.
    pub to_weigh: Vec<Finding>,
}

/// What a check finds in a change; its only error is RunChange::file_at's.
pub type Find<'a> = dyn Fn(&RunChange) -> Result<CheckFindings, OrchestratorError> + 'a;

/// A check the orchestrator runs itself, in each quality round after the build's gate. find returns what it found
/// in a change; a check that cannot do its work says so in a finding, never by finding nothing. Its only error is
/// RunChange::file_at's, which fails the round as a Jenkins failure does.
pub struct OrchestratorCheck<'a> {
    /// Its name, which prefixes the rule of each of its findings in the quality file.
    pub name: &'static str,
    pub find: Box<Find<'a>>,
}

/// The checks a quality round of a run of pipeline runs, in this order: the new dependencies, which it asks indexes
/// about; the change's size against the workflow's max_files and max_lines; and its paths against the spec.
pub fn orchestrator_checks<'a>(pipeline: &Pipeline, indexes: &'a PackageIndexes) -> Vec<OrchestratorCheck<'a>> {
    let (max_files, max_lines) = (pipeline.max_files(), pipeline.max_lines());
    vec![
        OrchestratorCheck { name: "new-dependencies", find: Box::new(move |change| new_dependencies(change, indexes)) },
        OrchestratorCheck {
            name: "diff-budget",
            find: Box::new(move |change| Ok(diff_budget(change, max_files, max_lines))),
        },
        OrchestratorCheck { name: "unexpected-paths", find: Box::new(|change| Ok(unexpected_paths(change))) },
    ]
}

/// The quality file the build's gate wrote, quality, with what checks find in change added.
///
/// A check with failing findings is a failed condition, which turns `GATE: OK` into `GATE: ERROR`. Every finding is
/// listed in a section of their own at the end, failing ones first, numbered on from the file's last number while
/// the file lists at most QUALITY_FINDINGS_LIMIT in all; the rest are counted. quality comes back as it is when they
/// find nothing. Errors: a check's.
pub fn judge_run(quality: &str, checks: &[OrchestratorCheck], change: &RunChange) -> Result<String, OrchestratorError> {
    let (mut conditions, mut issues, mut to_weigh) = (Vec::new(), Vec::new(), Vec::new());
    for check in checks {
        let found = (check.find)(change)?;
        if !found.issues.is_empty() {
            conditions.push(format!(
                "{} findings of the `{}` check; each is listed below.",
                found.issues.len(),
                check.name
            ));
        }
        issues.extend(found.issues.into_iter().map(|f| named(check.name, f)));
        to_weigh.extend(found.to_weigh.into_iter().map(|f| named(check.name, f)));
    }
    if issues.is_empty() && to_weigh.is_empty() {
        return Ok(quality.to_string());
    }
    let quality = if conditions.is_empty() { quality.to_string() } else { failed_by(quality, &conditions) };
    let after = last_number(&quality);
    Ok(format!("{}\n{}\n", quality.trim_end_matches('\n'), orchestrator_section(&issues, &to_weigh, after).join("\n")))
}

/// finding with its rule prefixed by the check's name.
fn named(name: &str, finding: Finding) -> Finding {
    let rule = if finding.rule.is_empty() { name.to_string() } else { format!("{name} {}", finding.rule) };
    Finding { rule, ..finding }
}

/// The largest number a line of quality starts with, followed by ". ", in any script's decimal digits; 0 for none.
fn last_number(quality: &str) -> u128 {
    quality
        .split('\n')
        .filter_map(|line| {
            let digits: Vec<u32> = line.chars().map_while(decimal_value).collect();
            let rest = line.chars().skip(digits.len()).take(2).collect::<String>();
            (!digits.is_empty() && rest == ". ")
                .then(|| digits.iter().fold(0u128, |n, &d| n.saturating_mul(10).saturating_add(u128::from(d))))
        })
        .max()
        .unwrap_or(0)
}

/// quality with GATE: ERROR, and conditions last among its failed conditions, a section it gains, before its first
/// other one below the gate line, when it has none. The gate line stays the first non-blank one, even when it is
/// itself a `## ` heading, so crate::quality::parse_gate reads ERROR from the result.
fn failed_by(quality: &str, conditions: &[String]) -> String {
    let mut lines: Vec<String> = quality.split('\n').map(str::to_string).collect();
    let at = lines.iter().position(|line| !strip(line).is_empty()).unwrap_or(0);
    if let Some(found) = labelled_word(&lines[at], "GATE", &[QUALITY_OK]) {
        lines[at].replace_range(found.word_start..found.end, QUALITY_ERROR);
    }
    let bullets = conditions.iter().map(|c| one_line(&format!("- {c}")));
    match lines.iter().position(|line| line == QUALITY_FAILED_CONDITIONS) {
        Some(heading) => {
            let mut end = (heading + 2).min(lines.len());
            while end < lines.len() && lines[end].starts_with("- ") {
                end += 1;
            }
            lines.splice(end..end, bullets);
        }
        None => {
            let first = (at + 1..lines.len()).find(|&i| lines[i].starts_with("## ")).unwrap_or(lines.len());
            let section = [QUALITY_FAILED_CONDITIONS.to_string(), String::new()]
                .into_iter()
                .chain(bullets)
                .chain([String::new()]);
            lines.splice(first..first, section);
        }
    }
    lines.join("\n")
}

/// judge_run's section, numbered on from after, as lines that start with a blank one.
fn orchestrator_section(issues: &[Finding], to_weigh: &[Finding], after: u128) -> Vec<String> {
    let mut out = vec![
        String::new(),
        "## The orchestrator's checks".to_string(),
        String::new(),
        "The orchestrator ran these itself after the build, with what the build does not see: the run's spec and its \
         workflow."
            .to_string(),
    ];
    // The limit is the whole file's, the build's findings included.
    let room = usize::try_from((QUALITY_FINDINGS_LIMIT as u128).saturating_sub(after)).unwrap_or(0);
    let listed_issues = &issues[..issues.len().min(room)];
    let listed_weigh = &to_weigh[..to_weigh.len().min(room - listed_issues.len())];
    let mut k = after;
    if !listed_issues.is_empty() {
        out.push(String::new());
        for finding in listed_issues {
            k += 1;
            out.push(finding.numbered(k));
        }
    }
    if !listed_weigh.is_empty() {
        out.extend([String::new(), QUALITY_TO_WEIGH_NOTE.to_string(), String::new()]);
        for finding in listed_weigh {
            k += 1;
            out.push(finding.numbered(k));
        }
    }
    let more = issues.len() + to_weigh.len() - listed_issues.len() - listed_weigh.len();
    if more > 0 {
        out.extend([String::new(), format!("{more} more findings of the orchestrator's checks are not listed.")]);
    }
    out
}

/// A finding to weigh when the change touches more than max_files files, or adds and removes more than max_lines
/// lines between them; none otherwise.
pub fn diff_budget(change: &RunChange, max_files: i64, max_lines: i64) -> CheckFindings {
    let added: u64 = change.files.iter().map(|f| f.added).sum();
    let removed: u64 = change.files.iter().map(|f| f.removed).sum();
    let mut over = Vec::new();
    if i128::try_from(change.files.len()).unwrap_or(i128::MAX) > i128::from(max_files) {
        over.push(format!("{} files, over the workflow's max_files of {max_files}", change.files.len()));
    }
    if i128::from(added + removed) > i128::from(max_lines) {
        let whose = if over.is_empty() { "the workflow's" } else { "its" };
        over.push(format!(
            "{} lines ({added} added, {removed} removed), over {whose} max_lines of {max_lines}",
            added + removed
        ));
    }
    if over.is_empty() {
        return CheckFindings::default();
    }
    let message = format!("The change touches {}. Say in your report why it cannot be smaller.", over.join(", and "));
    CheckFindings { issues: Vec::new(), to_weigh: vec![Finding::warning(None, None, "", message)] }
}

/// A finding to weigh for each path the change touches that the spec mentions neither as a whole nor by its file
/// name. A rename is judged at its new path only, and a path or name counts as mentioned only where it appears
/// whole in the spec, as a plain substring: naming a directory does not cover the files in it.
pub fn unexpected_paths(change: &RunChange) -> CheckFindings {
    let paths: BTreeSet<&str> = change.files.iter().map(|f| f.path.as_str()).collect();
    let to_weigh = paths
        .into_iter()
        .filter(|path| !change.spec.contains(path) && !change.spec.contains(basename(path)))
        .map(|path| {
            Finding::warning(Some(path), None, "", "the spec mentions neither this path nor its file name.".to_string())
        })
        .collect();
    CheckFindings { issues: Vec::new(), to_weigh }
}

#[cfg(test)]
pub(crate) mod tests;
