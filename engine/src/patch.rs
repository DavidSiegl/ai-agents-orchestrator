//! Reading a `git diff -U0` in the one shape Host::change_diff gives it: added_lines.

use std::collections::BTreeMap;

use crate::error::OrchestratorError;
use crate::py::json::Map;
use crate::py::text::repr;

/// The lines a file gains, by their line in the new tree, from 1, each one's bytes without the "+".
pub type AddedLines = BTreeMap<u64, Vec<u8>>;

/// For each file diff touches, by its path in the new tree and in the order the diff names them, the lines it gains:
/// added_lines of orchestrator.py. A file the diff touches without adding a line, such as a pure rename, one that only
/// loses lines or an empty new file, maps to no lines; a deleted file is left out. Only `diff --git` and `@@` lines
/// are read inside a hunk, since an added line can itself start with `+++ `. Only "\n" ends a line, as in git's
/// output: an added line can hold a carriage return, a form feed or U+2028, and bytes that are not UTF-8.
///
/// The gate's changed_lines (gate.py) reads the same shape and is the home of how, so a fix to one belongs in both.
/// Errors: a path, as git writes it or once its quoting is undone, that is not UTF-8, which Python reads.
pub fn added_lines(diff: &[u8]) -> Result<Map<AddedLines>, OrchestratorError> {
    let mut out: Map<AddedLines> = Map::new();
    let mut path: Option<String> = None;
    let (mut line, mut in_hunk) = (0u64, false);
    for text in diff.split(|&b| b == b'\n') {
        if let Some(names) = text.strip_prefix(b"diff --git ") {
            path = git_path(names)?;
            in_hunk = false;
        } else if text.starts_with(b"@@") {
            in_hunk = true;
            line = hunk_start(text).unwrap_or(0);
        } else if !in_hunk {
            path = header_path(&mut out, path, text)?;
        } else if let (Some(added), Some(p)) = (text.strip_prefix(b"+"), &path) {
            out.get_mut(p).expect("a file is in out from its first line on").insert(line, added.to_vec());
            line += 1;
        }
        if let Some(p) = &path {
            if !out.contains_key(p) {
                out.insert(p.clone(), AddedLines::new());
            }
        }
    }
    Ok(out)
}

/// The line a hunk header `@@ -<old>[,<n>] +<new>...` starts the new side at; None for a header not in that shape.
fn hunk_start(header: &[u8]) -> Option<u64> {
    let rest = header.strip_prefix(b"@@ -")?;
    let old = rest.iter().take_while(|b| b.is_ascii_digit()).count();
    if old == 0 {
        return None;
    }
    let mut rest = &rest[old..];
    if let Some(after) = rest.strip_prefix(b",") {
        let n = after.iter().take_while(|b| b.is_ascii_digit()).count();
        if n == 0 {
            return None;
        }
        rest = &after[n..];
    }
    let new = rest.strip_prefix(b" +")?;
    let digits = new.iter().take_while(|b| b.is_ascii_digit()).count();
    std::str::from_utf8(&new[..digits]).ok()?.parse().ok()
}

/// The file the diff is about after text, a header line outside a hunk, path before it; a deleted file is dropped
/// from out, as added_lines says.
fn header_path(
    out: &mut Map<AddedLines>,
    path: Option<String>,
    text: &[u8],
) -> Result<Option<String>, OrchestratorError> {
    if text.starts_with(b"deleted file mode") {
        if let Some(p) = path {
            out.remove(&p);
            return Ok(None);
        }
    }
    if let Some(name) = text.strip_prefix(b"rename to ") {
        return utf8_path(unquote(name)).map(Some);
    }
    if let Some(name) = text.strip_prefix(b"+++ ") {
        // git ends the name with a tab when it holds a space.
        let name = utf8_path(unquote(name.strip_suffix(b"\t").unwrap_or(name)))?;
        if name == "/dev/null" {
            return Ok(None);
        }
        return Ok(Some(name.strip_prefix("b/").map(String::from).unwrap_or(name)));
    }
    Ok(path)
}

/// The path of `a/<path> b/<path>`; None when the two differ, as for a rename, which later lines name.
fn git_path(names: &[u8]) -> Result<Option<String>, OrchestratorError> {
    let (a, b) = match quoted_pair(names) {
        Some((a, b)) => (unquote(a), unquote(b)),
        None => {
            let half = names.len().saturating_sub(1) / 2;
            (names[..half].to_vec(), names.get(half + 1..).unwrap_or_default().to_vec())
        }
    };
    match (a.strip_prefix(b"a/"), b.strip_prefix(b"b/")) {
        (Some(a), Some(b)) if a == b => utf8_path(b.to_vec()).map(Some),
        _ => Ok(None),
    }
}

/// The two names of `"<a>" "<b>"`, each quoted as git quotes a name, quotes included; None for names in any other
/// form.
fn quoted_pair(names: &[u8]) -> Option<(&[u8], &[u8])> {
    let first = quoted_len(names)?;
    let rest = names[first..].strip_prefix(b" ")?;
    (quoted_len(rest)? == rest.len()).then(|| (&names[..first], rest))
}

/// The length of the quoted name text starts with, its quotes included: a quote, then anything but a quote or a
/// backslash, or a backslash and the character after it, up to the closing quote; None when text starts with none.
fn quoted_len(text: &[u8]) -> Option<usize> {
    if text.first() != Some(&b'"') {
        return None;
    }
    let mut i = 1;
    while i < text.len() {
        match text[i] {
            b'"' => return Some(i + 1),
            // Python's `.` takes any character but "\n" after the backslash.
            b'\\' if text.get(i + 1).is_some_and(|&c| c != b'\n') => i += 1 + utf8_len(text[i + 1]),
            b'\\' => return None,
            _ => i += 1,
        }
    }
    None
}

/// How many bytes the UTF-8 sequence whose first byte is first holds; 1 for a byte that starts none.
fn utf8_len(first: u8) -> usize {
    match first {
        0xc0..=0xdf => 2,
        0xe0..=0xef => 3,
        0xf0..=0xf7 => 4,
        _ => 1,
    }
}

/// A path as git quotes it when it holds a quote, a backslash or a control character, or, with core.quotePath on, a
/// byte above 0x7f, as the bytes it names; any other name as it is.
fn unquote(name: &[u8]) -> Vec<u8> {
    if name.len() < 2 || name[0] != b'"' || name[name.len() - 1] != b'"' {
        return name.to_vec();
    }
    let inner = &name[1..name.len() - 1];
    let mut raw = Vec::with_capacity(inner.len());
    let mut i = 0;
    while i < inner.len() {
        if inner[i] != b'\\' || i + 1 == inner.len() {
            raw.push(inner[i]);
            i += 1;
            continue;
        }
        let octal = inner.get(i + 1..i + 4).filter(|d| {
            (b'0'..=b'3').contains(&d[0]) && d[1..].iter().all(|c| (b'0'..=b'7').contains(c))
        });
        if let Some(digits) = octal {
            raw.push((digits[0] - b'0') * 64 + (digits[1] - b'0') * 8 + (digits[2] - b'0'));
            i += 4;
            continue;
        }
        let escaped = match inner[i + 1] {
            b'a' => b'\x07',
            b'b' => b'\x08',
            b'f' => b'\x0c',
            b'n' => b'\n',
            b'r' => b'\r',
            b't' => b'\t',
            b'v' => b'\x0b',
            // Any other character, the quote and the backslash among them, stands for itself.
            _ => {
                let len = utf8_len(inner[i + 1]).min(inner.len() - i - 1);
                raw.extend_from_slice(&inner[i + 1..i + 1 + len]);
                i += 1 + len;
                continue;
            }
        };
        raw.push(escaped);
        i += 2;
    }
    raw
}

/// bytes, a path, as text; refused when it is not UTF-8, which the Python engine reads as os.fsdecode decodes it
/// (TODO.md, "The Rust engine reads some input ... otherwise").
fn utf8_path(bytes: Vec<u8>) -> Result<String, OrchestratorError> {
    String::from_utf8(bytes).map_err(|e| {
        let shown = String::from_utf8_lossy(e.as_bytes()).into_owned();
        OrchestratorError(format!("the diff names a path that is not UTF-8: {}", repr(&shown)))
    })
}

#[cfg(test)]
mod tests;
