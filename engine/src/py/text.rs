//! Python's str methods that orchestrator.py's text handling relies on, with Python's character classes
//! (unicode_tables), so that a line, a blank or a digit is what it was to the Python engine.

use super::unicode_tables::{ALNUM, DECIMAL, PRINTABLE, SPACE};

fn in_table(table: &[(u32, u32)], c: char) -> bool {
    let c = c as u32;
    table
        .binary_search_by(|&(lo, hi)| {
            if hi < c {
                std::cmp::Ordering::Less
            } else if lo > c {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// Whether c is a blank to Python: str.isspace, re's `\s`, what str.strip removes.
pub fn is_space(c: char) -> bool {
    in_table(SPACE, c)
}

/// The value of c as a decimal digit to Python (str.isdecimal, re's `\d`, int()), or None for any other character.
pub fn decimal_value(c: char) -> Option<u32> {
    let code = c as u32;
    DECIMAL
        .iter()
        .find(|&&(lo, hi)| lo <= code && code <= hi)
        .map(|&(lo, _)| (code - lo) % 10)
}

/// Whether c is a word character of Python's re, `\w`: str.isalnum, or `_`.
pub fn is_word(c: char) -> bool {
    c == '_' || in_table(ALNUM, c)
}

/// Whether repr() writes c as it is (str.isprintable).
pub fn is_printable(c: char) -> bool {
    in_table(PRINTABLE, c)
}

/// s without the blanks (is_space) at its ends: str.strip().
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// s without the blanks at its end: str.rstrip().
pub fn rstrip(s: &str) -> &str {
    s.trim_end_matches(is_space)
}

/// s without the blanks at its start: str.lstrip().
pub fn lstrip(s: &str) -> &str {
    s.trim_start_matches(is_space)
}

/// text with each "\r\n" and each lone "\r" made "\n", as Python's text mode reads a process's output.
pub fn universal_newlines(text: &str) -> String {
    if !text.contains('\r') {
        return text.to_string();
    }
    text.replace("\r\n", "\n").replace('\r', "\n")
}

/// The characters that end a line for str.splitlines, besides "\r\n", which ends one as a pair.
pub const LINE_BREAKS: [char; 10] = ['\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\u{85}', '\u{2028}', '\u{2029}'];

/// The lines of s, as str.splitlines() gives them: without their line ends, a line ending at any of LINE_BREAKS or
/// at "\r\n", and no empty line after a last line end; none for an empty s.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if !LINE_BREAKS.contains(&c) {
            continue;
        }
        lines.push(&s[start..i]);
        let mut end = i + c.len_utf8();
        if c == '\r' {
            if let Some(&(j, '\n')) = chars.peek() {
                chars.next();
                end = j + 1;
            }
        }
        start = end;
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// path split before its extension, as os.path.splitext: the extension is from the last dot of the last component
/// on, unless every character before that dot in the component is a dot too, as in ".bashrc"; else it is empty.
pub fn splitext(path: &str) -> (&str, &str) {
    let name_start = path.rfind('/').map_or(0, |i| i + 1);
    if let Some(dot) = path[name_start..].rfind('.') {
        let dot = name_start + dot;
        if path[name_start..dot].chars().any(|c| c != '.') {
            return (&path[..dot], &path[dot..]);
        }
    }
    (path, "")
}

/// The last component of path, as os.path.basename.
pub fn basename(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

/// s with the first letter of each run of ASCII letters in upper case and the others in lower case, as str.title()
/// writes text that is all ASCII. A character that is not ASCII is left as it is and ends the run, where str.title()
/// would take a letter beyond ASCII into it; the engine titles only names of ASCII letters, digits, - and _.
pub fn title_ascii(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_word = false;
    for c in s.chars() {
        if c.is_ascii_alphabetic() {
            out.push(if in_word { c.to_ascii_lowercase() } else { c.to_ascii_uppercase() });
            in_word = true;
        } else {
            out.push(c);
            in_word = false;
        }
    }
    out
}

/// What Python's UnicodeDecodeError says of bytes that are not UTF-8, as e found them: `'utf-8' codec can't decode
/// byte 0xff in position 2: invalid start byte`, or, for a sequence of more than one byte that ends too soon,
/// `bytes in position 0-1`: the longest start of a sequence, as Python and Rust both take it.
pub fn utf8_error(bytes: &[u8], e: &std::str::Utf8Error) -> String {
    let at = e.valid_up_to();
    let len = e.error_len().unwrap_or(bytes.len() - at);
    let what = if len > 1 {
        format!("bytes in position {at}-{}", at + len - 1)
    } else {
        format!("byte 0x{:02x} in position {at}", bytes[at])
    };
    let reason = match e.error_len() {
        None => "unexpected end of data",
        // A byte that may start a sequence, followed by one that cannot go on with it.
        Some(_) if (0xc2..=0xf4).contains(&bytes[at]) => "invalid continuation byte",
        Some(_) => "invalid start byte",
    };
    format!("'utf-8' codec can't decode {what}: {reason}")
}

/// s as Python's repr() writes a str: in single quotes, or in double quotes when s holds a single quote and no
/// double one, with the backslash, the quote, "\t", "\n", "\r" and every character that is not printable escaped.
pub fn repr(s: &str) -> String {
    quoted(s, false)
}

/// s as Python's ascii() writes a str: repr, with every character beyond ASCII escaped too.
pub fn ascii(s: &str) -> String {
    quoted(s, true)
}

fn quoted(s: &str, ascii_only: bool) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            _ if c == quote => {
                out.push('\\');
                out.push(c);
            }
            _ if c.is_ascii() && (' '..='~').contains(&c) => out.push(c),
            _ if !c.is_ascii() && !ascii_only && is_printable(c) => out.push(c),
            _ => {
                let code = c as u32;
                if code <= 0xff {
                    out.push_str(&format!("\\x{code:02x}"));
                } else if code <= 0xffff {
                    out.push_str(&format!("\\u{code:04x}"));
                } else {
                    out.push_str(&format!("\\U{code:08x}"));
                }
            }
        }
    }
    out.push(quote);
    out
}

/// argv as one line of sh, each word quoted as shlex.quote quotes it, as shlex.join writes it: a word of nothing but
/// ASCII letters, digits and `_@%+=:,./-` as it is, any other in single quotes, a quote inside as `'"'"'`.
pub fn shlex_join<S: AsRef<str>>(argv: &[S]) -> String {
    argv.iter().map(|word| shlex_quote(word.as_ref())).collect::<Vec<_>>().join(" ")
}

fn shlex_quote(word: &str) -> String {
    if word.is_empty() {
        return "''".to_string();
    }
    if word.chars().all(|c| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c)) {
        return word.to_string();
    }
    format!("'{}'", word.replace('\'', "'\"'\"'"))
}

/// The one of possibilities closest to word, as difflib.get_close_matches(word, possibilities, n=1) picks it: the
/// highest SequenceMatcher ratio of at least 0.6, the greater string on a tie; None when none comes that close.
pub fn close_match<'a, I>(word: &str, possibilities: I) -> Option<&'a str>
where
    I: IntoIterator<Item = &'a str>,
{
    let b: Vec<char> = word.chars().collect();
    let mut best: Option<(f64, &'a str)> = None;
    for x in possibilities {
        let a: Vec<char> = x.chars().collect();
        let score = ratio(&a, &b);
        if score < 0.6 {
            continue;
        }
        if best.is_none_or(|(s, y)| score > s || (score == s && x > y)) {
            best = Some((score, x));
        }
    }
    best.map(|(_, x)| x)
}

/// difflib.SequenceMatcher(None, a, b).ratio(): twice the characters of the matching blocks over the length of both.
/// Its autojunk heuristic applies only from 200 characters of b on, which no word this takes comes near, and is
/// left out.
fn ratio(a: &[char], b: &[char]) -> f64 {
    let total = a.len() + b.len();
    if total == 0 {
        return 1.0;
    }
    2.0 * matching_characters(a, b) as f64 / total as f64
}

/// How many characters the matching blocks of a and b hold, as SequenceMatcher.get_matching_blocks finds them: the
/// longest match, earliest in a and then in b, then the same on each side of it.
fn matching_characters(a: &[char], b: &[char]) -> usize {
    let mut queue = vec![(0, a.len(), 0, b.len())];
    let mut matched = 0;
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = longest_match(a, b, alo, ahi, blo, bhi);
        if k == 0 {
            continue;
        }
        matched += k;
        if alo < i && blo < j {
            queue.push((alo, i, blo, j));
        }
        if i + k < ahi && j + k < bhi {
            queue.push((i + k, ahi, j + k, bhi));
        }
    }
    matched
}

/// SequenceMatcher.find_longest_match without junk: (i, j, k) such that a[i..i+k] == b[j..j+k] is a longest match
/// within the bounds, with the smallest i and then the smallest j.
fn longest_match(a: &[char], b: &[char], alo: usize, ahi: usize, blo: usize, bhi: usize) -> (usize, usize, usize) {
    let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0);
    // run[j] is the length of the match ending at a[i-1] and b[j-1], for the previous i.
    let mut run = vec![0usize; b.len() + 1];
    for (i, ac) in a.iter().enumerate().take(ahi).skip(alo) {
        let mut next = vec![0usize; b.len() + 1];
        for j in blo..bhi {
            if *ac != b[j] {
                continue;
            }
            let k = run[j] + 1;
            next[j + 1] = k;
            if k > bestsize {
                besti = i + 1 - k;
                bestj = j + 1 - k;
                bestsize = k;
            }
        }
        run = next;
    }
    (besti, bestj, bestsize)
}

/// text, or its first n - 1 characters and an ellipsis when it is longer than n characters.
pub fn shortened(text: &str, n: usize) -> String {
    if text.chars().count() <= n {
        return text.to_string();
    }
    let mut out: String = text.chars().take(n - 1).collect();
    out.push('…');
    out
}

#[cfg(test)]
mod tests;
