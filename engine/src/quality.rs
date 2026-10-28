//! The engine's copy of what it needs of the quality file's format: reading the gate's word and what failed it, and
//! writing the quality files only the engine writes (a build that failed, a snapshot that holds secrets). The gate,
//! which writes the file, is the home of that format: gate.py's quality_report and its sections, which
//! docs/quality-gate.md's "The quality file as a contract" describes. engine/tests/oracle/quality.py holds this copy to
//! the Python engine's reading of the gate's files.

use std::fmt;

use sha2::{Digest, Sha256};

use crate::constants::CONSOLE_TAIL_LINES;
use crate::handoff::first_line_word;
use crate::py::json::Map;
use crate::py::text::{decimal_value, is_space, splitlines, strip};

/// The words of the gate line, the file's first non-blank one. Only the engine writes BUILD_FAILED, for a build that
/// ended before its Gate stage (Workflow::quality_report).
pub const QUALITY_OK: &str = "OK";
pub const QUALITY_ERROR: &str = "ERROR";
pub const QUALITY_BUILD_FAILED: &str = "BUILD_FAILED";
/// Only the engine writes LEAK too: gitleaks found a secret in the quality round's snapshot, so it was not pushed and
/// Jenkins did not judge it (leak_report).
pub const QUALITY_LEAK: &str = "LEAK";
/// The headings of the sections gate_failures reads, besides its numbered findings.
pub const QUALITY_FAILED_CONDITIONS: &str = "## Failed conditions";
pub const QUALITY_FAILING_TESTS: &str = "## Failing tests";
/// leak_report's section, numbering each Leak as its Display gives it.
pub const QUALITY_SECRETS_FOUND: &str = "## Secrets found";
/// Leads the findings to weigh, in a section of the gate's or the orchestrator's checks'.
pub const QUALITY_TO_WEIGH_NOTE: &str = "These do not fail the gate. Fix each, or say in your report why it should stand.";
/// The most findings a quality file lists, numbered as one list, and the most failing tests; the rest are counted.
pub const QUALITY_FINDINGS_LIMIT: usize = 50;

/// Each character that ends a line in some reader of the quality file, which one_line escapes as gate.py's
/// _LINE_BREAK_ESCAPES writes it, so a list item the engine adds reads back as the one line the gate's would.
const LINE_BREAKS: [char; 10] = ['\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\u{85}', '\u{2028}', '\u{2029}'];

/// text as a quality file writes it within one of its lines, such as one item of a list: each character that ends a
/// line in some reader written as an escape, `\x` and two hex digits up to U+00FF, `\u` and four beyond, anything
/// else as it is.
pub fn one_line(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if !LINE_BREAKS.contains(&c) {
            out.push(c);
        } else if (c as u32) <= 0xff {
            out.push_str(&format!("\\x{:02x}", c as u32));
        } else {
            out.push_str(&format!("\\u{:04x}", c as u32));
        }
    }
    out
}

/// The gate on the quality file's first non-blank line, one of the QUALITY_ words, tolerating Markdown emphasis
/// around it; None for a file without one.
pub fn parse_gate(quality: &str) -> Option<&'static str> {
    first_line_word(quality, "GATE", &[QUALITY_OK, QUALITY_ERROR, QUALITY_BUILD_FAILED, QUALITY_LEAK])
}

/// A secret gitleaks found in a commit a push would publish, or a `gitleaks:allow` comment the push would add. It
/// holds where the secret is, never the secret: nothing built from a Leak can carry it into a log, a prompt, a pull
/// request or state.json.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Leak {
    /// gitleaks' RuleID, or "gitleaks-allow" for the comment.
    pub rule: String,
    /// The path in that commit.
    pub file: String,
    /// Its line there, from 1.
    pub line: u64,
    /// The full sha of the commit that adds it.
    pub commit: String,
    /// gitleaks' Fingerprint, commit:file:rule:line, which a .gitleaksignore at the base can list.
    pub fingerprint: String,
}

impl fmt::Display for Leak {
    /// Where it is, its rule, the commit's first 12 characters and its fingerprint.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let commit: String = self.commit.chars().take(12).collect();
        write!(f, "`{}:{}` {} in {commit} (fingerprint `{}`)", self.file, self.line, self.rule, self.fingerprint)
    }
}

/// The quality file of a round whose snapshot holds secrets: QUALITY_LEAK, intro, and the leaks numbered, each where
/// it is, its rule and its fingerprint, never the secret.
pub fn leak_report(leaks: &[Leak], intro: &str) -> String {
    let mut lines = vec![format!("GATE: {QUALITY_LEAK}"), String::new(), intro.to_string(), String::new()];
    lines.push(QUALITY_SECRETS_FOUND.to_string());
    lines.push(String::new());
    lines.extend(leaks.iter().enumerate().map(|(k, leak)| one_line(&format!("{}. {leak}", k + 1))));
    lines.join("\n") + "\n"
}

/// The quality file of a build that ended before its Gate stage, so that nothing judged the change: GATE:
/// BUILD_FAILED, then intro, then a section of tests, the (name, detail) of the failing tests Jenkins reported, None
/// when it has no test report, and one of console, the end of the build's console, unless it is empty.
pub fn build_failed_report(intro: &str, tests: Option<&[(String, String)]>, console: &str) -> String {
    let mut out: Vec<String> = [&format!("GATE: {QUALITY_BUILD_FAILED}"), "", intro, "", QUALITY_FAILING_TESTS, ""]
        .iter()
        .map(|s| s.to_string())
        .collect();
    match tests {
        None => out.push("Jenkins has no test report for this build.".into()),
        Some(tests) => {
            for (name, detail) in tests.iter().take(QUALITY_FINDINGS_LIMIT) {
                let detail = strip(detail);
                let first = splitlines(detail).first().copied().filter(|_| !detail.is_empty()).unwrap_or("failed");
                out.push(one_line(&format!("- `{name}`: {first}")));
            }
            if tests.is_empty() {
                out.push("None.".into());
            }
            if tests.len() > QUALITY_FINDINGS_LIMIT {
                out.push(format!("- and {} more.", tests.len() - QUALITY_FINDINGS_LIMIT));
            }
        }
    }
    if !console.is_empty() {
        out.push(String::new());
        out.push(format!("## The last {CONSOLE_TAIL_LINES} lines of the build's console"));
        out.extend(["", "````", console, "````"].map(String::from));
    }
    out.join("\n") + "\n"
}

/// What a quality file says failed the gate, in a form two quality rounds can be compared by.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct GateFailures {
    /// The failed conditions, as the file words them.
    pub conditions: Vec<String>,
    /// By check, the first word of a finding's rule, or "tests" for a failing test: finding_fingerprint of each
    /// finding that fails the gate, and of each failing test, in the file's order. Findings to weigh are left out. A
    /// secret, under "gitleaks", is finding_fingerprint(path, "gitleaks <rule>", "") with "-<k>" added for the k-th
    /// secret of that rule in that file, its line and commit left out, so that two rounds compare by how many each
    /// has.
    pub fingerprints: Map<Vec<String>>,
}

impl GateFailures {
    /// Whether these failures are the earlier ones again: something failed, nothing that failed before was fixed,
    /// and nothing new failed. A finding counts as the same wherever it moved (finding_fingerprint), a condition only
    /// when its words are.
    pub fn repeats(&self, earlier: &GateFailures) -> bool {
        let now = self.all();
        !now.is_empty() && now == earlier.all()
    }

    fn all(&self) -> std::collections::BTreeSet<&str> {
        let findings = self.fingerprints.values().flatten();
        self.conditions.iter().chain(findings).map(String::as_str).collect()
    }
}

/// A short hash that names a finding however its line moves: the first 16 hex digits of the SHA-256 of its path
/// (empty for a finding about the whole change), its rule and its message without decimal digits, since a line
/// number in it moves too, joined by NULs.
pub fn finding_fingerprint(path: Option<&str>, rule: &str, message: &str) -> String {
    let message: String = message.chars().filter(|c| decimal_value(*c).is_none()).collect();
    let key = [path.unwrap_or(""), rule, &message].join("\0");
    let digest = Sha256::digest(key.as_bytes());
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// What the quality file, as the gate, judge_run and leak_report write it, says failed the gate: its failed
/// conditions, the numbered findings before a QUALITY_TO_WEIGH_NOTE in each section, its failing tests, and its
/// secrets under the check "gitleaks", each as the k-th secret of its rule in its file, wherever its line moved and
/// whichever snapshot held it, so that a round which removes one of a file's two keys of a kind is not the same
/// again.
pub fn gate_failures(quality: &str) -> GateFailures {
    let mut failures = GateFailures::default();
    let mut secrets: Map<usize> = Map::new();
    let (mut section, mut weighing) = ("", false);
    let add = |failures: &mut GateFailures, check: &str, fingerprint: String| match failures.fingerprints.get_mut(check) {
        Some(list) => list.push(fingerprint),
        None => failures.fingerprints.insert(check, vec![fingerprint]),
    };
    for line in quality.split('\n') {
        if line.starts_with("## ") {
            (section, weighing) = (line, false);
        } else if line == QUALITY_TO_WEIGH_NOTE {
            weighing = true;
        } else if section == QUALITY_FAILED_CONDITIONS && line.starts_with("- ") {
            failures.conditions.push(line[2..].to_string());
        } else if let (true, Some(name)) = (section == QUALITY_FAILING_TESTS, failing_test(line)) {
            add(&mut failures, "tests", finding_fingerprint(Some(name), "tests", ""));
        } else if let (true, Some((path, rule))) = (section == QUALITY_SECRETS_FOUND, secret_found(line)) {
            let rule = format!("gitleaks {rule}");
            let key = format!("{path}\0{rule}");
            let count = secrets.get(&key).copied().unwrap_or(0) + 1;
            secrets.insert(key, count);
            add(&mut failures, "gitleaks", format!("{}-{count}", finding_fingerprint(Some(path), &rule, "")));
        } else if let (false, Some(finding)) = (weighing, numbered_finding(line)) {
            let check = finding.rule.split(' ').next().unwrap_or_default();
            add(&mut failures, check, finding_fingerprint(finding.path, finding.rule, finding.message));
        }
    }
    failures
}

/// A numbered finding of a quality file, as gate.py's _numbered writes it.
struct NumberedFinding<'a> {
    path: Option<&'a str>,
    rule: &'a str,
    message: &'a str,
}

/// line as a numbered finding, where, then severity, rule and message: what Python's
/// `\d+\. (?:`(?P<path>.+?)(?::\d+| \(the whole file\))`|The change as a whole:) \S+ (?P<rule>.+?): (?P<message>.*)`
/// matches in all of it, the path as short as the rest allows; None for any other line.
fn numbered_finding(line: &str) -> Option<NumberedFinding<'_>> {
    let rest = number_and_dot(line)?;
    if let Some(rest) = rest.strip_prefix("The change as a whole:") {
        return severity_rule_message(rest).map(|(rule, message)| NumberedFinding { path: None, rule, message });
    }
    let quoted = rest.strip_prefix('`')?;
    // The shortest path first: each place the path may end, in order.
    for (end, _) in quoted.char_indices().skip(1).chain([(quoted.len(), ' ')]) {
        let (path, after) = quoted.split_at(end);
        if path.contains('\n') {
            return None;
        }
        let closed = after
            .strip_prefix(':')
            .and_then(|digits| {
                let n = digits.len() - digits.trim_start_matches(|c| decimal_value(c).is_some()).len();
                (n > 0).then(|| &digits[n..])
            })
            .or_else(|| after.strip_prefix(" (the whole file)"))
            .and_then(|tail| tail.strip_prefix('`'));
        if let Some((rule, message)) = closed.and_then(severity_rule_message) {
            return Some(NumberedFinding { path: Some(path), rule, message });
        }
    }
    None
}

/// The rest of line after `\d+\. `, the digits Python's decimal ones; None when it does not start so.
fn number_and_dot(line: &str) -> Option<&str> {
    let digits = line.len() - line.trim_start_matches(|c| decimal_value(c).is_some()).len();
    (digits > 0).then(|| line[digits..].strip_prefix(". ")).flatten()
}

/// The rule and message of ` \S+ (?P<rule>.+?): (?P<message>.*)` matched in all of text: a blank, the severity, a
/// blank, then the rule up to the first ": " after its first character, and the message.
fn severity_rule_message(text: &str) -> Option<(&str, &str)> {
    let text = text.strip_prefix(' ')?;
    let severity = text.len() - text.trim_start_matches(|c: char| !is_space(c)).len();
    if severity == 0 {
        return None;
    }
    let rest = text[severity..].strip_prefix(' ')?;
    let first = rest.chars().next().filter(|c| *c != '\n')?;
    let at = rest[first.len_utf8()..].find(": ")? + first.len_utf8();
    let (rule, message) = (&rest[..at], &rest[at + 2..]);
    (!rule.contains('\n') && !message.contains('\n')).then_some((rule, message))
}

/// The test's name in a failing test's line, `- `(?P<name>.+)`: .*` matched in all of it, the name as long as the
/// rest allows; None for any other line.
fn failing_test(line: &str) -> Option<&str> {
    let rest = line.strip_prefix("- `")?;
    let end = rest.rfind("`: ").filter(|&end| end > 0)?;
    (!line.contains('\n')).then_some(&rest[..end])
}

/// The path and rule of a numbered Leak under QUALITY_SECRETS_FOUND, `\d+\. `(?P<path>.+):\d+` (?P<rule>\S+) in \S+
/// \(fingerprint `.*`\)` matched in all of it, the path as long as the rest allows; None for any other line. Its
/// commit and fingerprint name the round's snapshot, new in each round.
fn secret_found(line: &str) -> Option<(&str, &str)> {
    if line.contains('\n') {
        return None;
    }
    let quoted = number_and_dot(line)?.strip_prefix('`')?;
    // The longest path first: each ":" it may end at, from the last.
    let ends: Vec<usize> = quoted.match_indices(':').map(|(i, _)| i).filter(|&i| i > 0).collect();
    for &end in ends.iter().rev() {
        let digits = &quoted[end + 1..];
        let n = digits.len() - digits.trim_start_matches(|c| decimal_value(c).is_some()).len();
        let Some(tail) = (n > 0).then(|| digits[n..].strip_prefix("` ")).flatten() else { continue };
        let rule_len = tail.len() - tail.trim_start_matches(|c: char| !is_space(c)).len();
        let Some(commit) = (rule_len > 0).then(|| tail[rule_len..].strip_prefix(" in ")).flatten() else { continue };
        let commit_len = commit.len() - commit.trim_start_matches(|c: char| !is_space(c)).len();
        let fingerprint = &commit[commit_len..];
        if commit_len > 0
            && fingerprint.len() >= " (fingerprint ``)".len()
            && fingerprint.starts_with(" (fingerprint `")
            && fingerprint.ends_with("`)")
        {
            return Some((&quoted[..end], &tail[..rule_len]));
        }
    }
    None
}

#[cfg(test)]
mod tests;
