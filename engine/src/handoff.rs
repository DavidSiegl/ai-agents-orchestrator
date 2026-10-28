//! What the engine reads in the first line of a handoff file: a review's verdict, a report's BLOCKED question, and,
//! through the same reader, a quality file's gate word (crate::quality).

use crate::constants::{APPROVE, BLOCKED, CHANGES_REQUESTED, NO_QUESTION};
use crate::py::text::{is_space, is_word, lstrip, splitlines, strip};

/// Where `label:`, blanks, then one of words stand in a line: the earliest such place, as Python's re.search finds
/// `({label}:\s*)({words})(?=_*(?!\w))`. The word must end there: a word character after it continues it into another
/// word, unless it is one of the underscores that close emphasis, as in `_VERDICT: APPROVE_`.
///
/// Every reader and rewriter of a verdict or gate line matches through this, so they agree on which lines hold a word.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LabelledWord<'w> {
    /// Byte offsets into the line: where `label:` starts, and where the word starts and ends.
    pub start: usize,
    pub word_start: usize,
    pub end: usize,
    /// The one of words it is.
    pub word: &'w str,
}

/// The first LabelledWord of line; None when it has none.
pub fn labelled_word<'w>(line: &str, label: &str, words: &[&'w str]) -> Option<LabelledWord<'w>> {
    let marker = format!("{label}:");
    let mut from = 0;
    while let Some(found) = line[from..].find(&marker) {
        let start = from + found;
        let after = start + marker.len();
        let word_start = after + (line[after..].len() - line[after..].trim_start_matches(is_space).len());
        let rest = &line[word_start..];
        for word in words {
            if !rest.starts_with(word) {
                continue;
            }
            let end = word_start + word.len();
            let behind = line[end..].trim_start_matches('_');
            if !behind.chars().next().is_some_and(is_word) {
                return Some(LabelledWord { start, word_start, end, word });
            }
        }
        from = start + line[start..].chars().next().map_or(1, char::len_utf8);
    }
    None
}

/// The one of words that follows `label:` on text's first non-blank line, Markdown emphasis around them tolerated;
/// None when that line has none of them, or when text is blank.
pub fn first_line_word<'w>(text: &str, label: &str, words: &[&'w str]) -> Option<&'w str> {
    let line = splitlines(text).into_iter().find(|line| !strip(line).is_empty())?;
    labelled_word(line, label, words).map(|found| found.word)
}

/// The verdict on the review's first non-blank line, APPROVE or CHANGES_REQUESTED, tolerating Markdown emphasis
/// around it; None for any other review.
pub fn parse_verdict(review: &str) -> Option<&'static str> {
    first_line_word(review, "VERDICT", &[APPROVE, CHANGES_REQUESTED])
}

/// The question of a report whose first non-blank line starts with BLOCKED:, without the blanks around it, or
/// NO_QUESTION when nothing follows; None for any other report.
pub fn parse_blocked(report: &str) -> Option<String> {
    let line = splitlines(report).into_iter().find(|line| !strip(line).is_empty())?;
    let question = lstrip(line).strip_prefix(BLOCKED)?;
    let question = strip(question);
    Some(if question.is_empty() { NO_QUESTION } else { question }.to_string())
}

#[cfg(test)]
mod tests;
