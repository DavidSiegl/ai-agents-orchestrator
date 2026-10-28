//! URLs as urllib.parse writes their parts, so that every URL the engine asks for is the one the Python engine asked
//! for: the same path segments, query and form, byte for byte.

/// s with every byte of its UTF-8 that is not a letter, a digit or one of `_.-~` written as `%XX`, in upper case:
/// urllib.parse.quote(s, safe=""), a path segment or a query value that cannot end the part it is in.
pub fn quote(s: &str) -> String {
    escaped(s, false)
}

/// The pairs as a query string or a form body: each name and value quoted as [`quote`] quotes, but a blank written as
/// `+`, joined by `=` and `&` in the order given, as urllib.parse.urlencode writes a dict.
pub fn urlencode(pairs: &[(&str, &str)]) -> String {
    pairs.iter().map(|(name, value)| format!("{}={}", escaped(name, true), escaped(value, true))).collect::<Vec<_>>().join("&")
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

#[cfg(test)]
mod tests;
