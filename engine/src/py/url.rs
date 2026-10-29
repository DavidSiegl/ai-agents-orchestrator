//! URLs as urllib.parse writes and reads their parts: every URL the engine asks for is the one the Python engine asked
//! for, the same path segments, query and form, byte for byte ([`quote`], [`urlencode`]), and a request serve answers
//! is read as Python's http.server reads it ([`urlsplit`], [`parse_qsl`]).

/// s with every byte of its UTF-8 that is not a letter, a digit or one of `_.-~` written as `%XX`, in upper case:
/// urllib.parse.quote(s, safe=""), a path segment or a query value that cannot end the part it is in.
pub fn quote(s: &str) -> String {
    escaped(s, false)
}

/// The pairs as a query string or a form body: each name and value quoted as [`quote`] quotes, but a blank written as
/// `+`, joined by `=` and `&` in the order given, as urllib.parse.urlencode writes a dict.
pub fn urlencode(pairs: &[(&str, &str)]) -> String {
    pairs
        .iter()
        .map(|(name, value)| format!("{}={}", escaped(name, true), escaped(value, true)))
        .collect::<Vec<_>>()
        .join("&")
}

fn escaped(s: &str, plus_for_blank: bool) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => out.push(char::from(b)),
            b' ' if plus_for_blank => out.push('+'),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// The parts of a URL as urllib.parse.urlsplit splits it, each a slice of the URL as it came, its % escapes kept.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SplitUrl {
    /// Before the first `:`, when that is a scheme (a letter, then letters, digits, `+`, `-` and `.`), in lower case;
    /// empty when there is none.
    pub scheme: String,
    /// After a `//` that starts what follows the scheme, up to the first `/`, `?` or `#`; empty without the `//`.
    pub netloc: String,
    /// What is left before the first `?` and `#`.
    pub path: String,
    /// Between the first `?` and the first `#` after it; empty without a `?`.
    pub query: String,
    /// After the first `#`; empty without one.
    pub fragment: String,
}

/// url split as urllib.parse.urlsplit(url) splits it: leading C0 controls and blanks dropped, and every tab, CR and
/// LF removed, first. Not ported: the ValueError Python raises for a netloc with one square bracket and no other, or
/// with characters NFKC normalization turns into a delimiter; such a netloc is kept as it is.
pub fn urlsplit(url: &str) -> SplitUrl {
    let url: String = url.trim_start_matches(|c: char| c <= ' ').chars().filter(|c| !matches!(c, '\t' | '\r' | '\n')).collect();
    let mut rest = url.as_str();
    let mut scheme = String::new();
    if let Some(i) = rest.find(':') {
        let candidate = &rest[..i];
        let is_scheme = candidate.starts_with(|c: char| c.is_ascii_alphabetic())
            && candidate.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'));
        if is_scheme {
            scheme = candidate.to_ascii_lowercase();
            rest = &rest[i + 1..];
        }
    }
    let mut netloc = "";
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        (netloc, rest) = after.split_at(end);
    }
    let (rest, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    SplitUrl {
        scheme,
        netloc: netloc.to_string(),
        path: path.to_string(),
        query: query.to_string(),
        fragment: fragment.to_string(),
    }
}

/// The name and value pairs of a query string, in order, as urllib.parse.parse_qsl(query, keep_blank_values=True)
/// reads them: split at each `&`, empty pieces skipped, a piece without `=` a name with an empty value, each name and
/// value unquoted by [`unquote_plus`].
pub fn parse_qsl(query: &str) -> Vec<(String, String)> {
    query
        .split('&')
        .filter(|piece| !piece.is_empty())
        .map(|piece| {
            let (name, value) = piece.split_once('=').unwrap_or((piece, ""));
            (unquote_plus(name), unquote_plus(value))
        })
        .collect()
}

/// s with each `+` a blank and each `%XX` (two hex digits, either case) the byte it names, the bytes then read as
/// UTF-8 with each invalid sequence U+FFFD, as urllib.parse.unquote_plus(s) reads it; a `%` without two hex digits
/// after it stays as it is. Characters beyond ASCII are kept as they are, as Python keeps them.
pub fn unquote_plus(s: &str) -> String {
    let s = s.replace('+', " ");
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let hex = |b: u8| char::from(b).to_digit(16);
        match (bytes[i], bytes.get(i + 1).copied().and_then(hex), bytes.get(i + 2).copied().and_then(hex)) {
            (b'%', Some(high), Some(low)) => {
                // Two hex digits make a value below 256.
                out.push(u8::try_from(high * 16 + low).unwrap_or(u8::MAX));
                i += 3;
            }
            (b, _, _) => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[cfg(test)]
mod tests;
