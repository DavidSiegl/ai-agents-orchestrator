//! What differs between two runs of one scenario, and so between a recording and its replay: `Normalizer`.
//!
//! The rust-engine spec's Parity section names it: times, run ids, pids and hashes, to which a world adds its own
//! directory and the host's name. Everything else in a record is compared as it is.
//!
//! The recordings were normalized by tests/parity/normalize.py's regular expressions. Each is a matcher here, which
//! finds what its expression found, scanning as Python's re does: at each position, the first alternative that
//! matches, the longest the expression allows, and the next search from the end of a match. Characters are classed
//! as Python's str patterns class them: a word character is alphanumeric or `_`, a digit is an ASCII one (no
//! recording holds another), and white space is char::is_whitespace's or one of U+001C to U+001F.

use serde_json::{Map, Value};

use crate::pyjson;

/// The JSON keys whose numbers are times measured by a clock: a turn's or a run's seconds, a state's age, a file's
/// modification time. In order, as the expression that finds them in JSON text tries them.
const TIMED_KEYS: [&str; 3] = ["heartbeat_age", "mtime", "seconds"];
/// The JSON keys whose strings word a time measured by a clock, such as run_health's "beat 3s ago".
const TIMED_TEXT_KEYS: [&str; 1] = ["health_reason"];

/// Replaces what differs between runs of a scenario with placeholders that are the same in every run.
///
/// Runs, their keys and git objects are numbered in the order they first appear in what `learn` is given, so a record
/// normalizes the same however its random ids came out: `<run:1>` and `<key:1>`, `<sha:3>`. A pid registered with
/// `pid` becomes `<pid:N>`, N being the number of the engine invocation it is a process of, and any other pid under a
/// "pid" key `<pid:?>`. Literal strings registered with `literal`, such as the world's directory, become their
/// placeholder.
pub struct Normalizer {
    /// Each literal and its placeholder, longest literal first, the host's name first among those as long.
    literals: Vec<(String, String)>,
    /// Each run id learnt and its placeholder, in the order learnt.
    runs: Vec<(String, String)>,
    /// Each run's key, the run id's last six hex digits, and its placeholder, in the order learnt.
    keys: Vec<(String, String)>,
    /// Each full git object name learnt and its placeholder, in the order learnt.
    shas: Vec<(String, String)>,
    /// Each pid registered and its placeholder, in the order first registered.
    pids: Vec<(i64, String)>,
}

impl Default for Normalizer {
    fn default() -> Normalizer {
        Normalizer::new()
    }
}

impl Normalizer {
    /// A normalizer that knows only the host's name, as `<host>`.
    pub fn new() -> Normalizer {
        let host = nix::unistd::gethostname().map(|h| h.to_string_lossy().into_owned()).expect("the host's name");
        Normalizer { literals: vec![(host, "<host>".into())], runs: vec![], keys: vec![], shas: vec![], pids: vec![] }
    }

    /// Write placeholder wherever text appears; a longer text registered is replaced before a shorter one.
    pub fn literal(&mut self, text: &str, placeholder: &str) {
        self.literals.push((text.into(), placeholder.into()));
        // Stable, as Python's sort: of literals as long, the one registered first is replaced first.
        self.literals.sort_by_key(|(literal, _)| std::cmp::Reverse(literal.chars().count()));
    }

    /// Write `<pid:n>` for pid, a process of engine invocation n, as a number in JSON and as a word in text. A pid
    /// registered again keeps its place among the others, with the new placeholder.
    pub fn pid(&mut self, pid: i64, n: usize) {
        let placeholder = format!("<pid:{n}>");
        match self.pids.iter_mut().find(|(p, _)| *p == pid) {
            Some(entry) => entry.1 = placeholder,
            None => self.pids.push((pid, placeholder)),
        }
    }

    /// Number the runs and git objects in record in the order they appear in its JSON text, after those learnt before.
    pub fn learn(&mut self, record: &Value) {
        let text: Vec<char> = pyjson::dumps(record).chars().collect();
        for (start, end) in find_all(&text, run_id_at) {
            let id: String = text[start..end].iter().collect();
            if !self.runs.iter().any(|(r, _)| *r == id) {
                let n = self.runs.len() + 1;
                self.keys.push((id[id.len() - 6..].to_string(), format!("<key:{n}>")));
                self.runs.push((id, format!("<run:{n}>")));
            }
        }
        for (start, end) in find_all(&text, |t, i| hex_run_at(t, i, |n| n == 40 || n == 64)) {
            let sha: String = text[start..end].iter().collect();
            if !self.shas.iter().any(|(s, _)| *s == sha) {
                let n = self.shas.len() + 1;
                self.shas.push((sha, format!("<sha:{n}>")));
            }
        }
    }

    /// s with every placeholder written in; with timed, also each duration, for text that words the clock's times,
    /// such as summary.md's turns table.
    pub fn text(&self, s: &str, timed: bool) -> String {
        let mut s = s.to_string();
        for (literal, placeholder) in &self.literals {
            s = s.replace(literal.as_str(), placeholder);
        }
        s = substitute(&s, run_id_at, |id| lookup(&self.runs, id).unwrap_or("<run:?>").to_string());
        for (key, placeholder) in &self.keys {
            s = replace_unbordered(&s, key, placeholder, |c| is_hex(Some(c)));
        }
        s = substitute(&s, |t, i| hex_run_at(t, i, |n| n == 40 || n == 64), |sha| {
            lookup(&self.shas, sha).unwrap_or("<sha:?>").to_string()
        });
        s = substitute(&s, |t, i| hex_run_at(t, i, |n| (7..=39).contains(&n)), |short| self.abbreviated(short));
        s = substitute(&s, time_at, |_| "<time>".into());
        s = substitute(&s, timed_json_at, |m| {
            let key = &m[1..m.find("\":").expect("a timed key")];
            format!("\"{key}\": \"<seconds>\"")
        });
        s = substitute(&s, pid_json_at, |m| {
            let pid: Option<i64> = m["\"pid\": ".len()..].parse().ok();
            let placeholder = pid.and_then(|p| self.pids.iter().find(|(q, _)| *q == p)).map_or("<pid:?>", |(_, h)| h);
            format!("\"pid\": \"{placeholder}\"")
        });
        for (pid, placeholder) in &self.pids {
            let pid = pid.to_string();
            s = replace_unbordered(&s, &pid, placeholder, is_word);
        }
        if timed {
            s = substitute(&s, duration_at, |_| "<duration>".into());
        }
        s
    }

    /// The placeholder of the one known object whose name short abbreviates; short as it is for none or several.
    fn abbreviated(&self, short: &str) -> String {
        let mut found: Vec<&str> = self.shas.iter().filter(|(sha, _)| sha.starts_with(short)).map(|(_, p)| p.as_str()).collect();
        found.dedup();
        match found.as_slice() {
            [one] => one.to_string(),
            _ => short.to_string(),
        }
    }

    /// node with text() applied to every string and key in it, and the numbers under TIMED_KEYS and the pids under
    /// "pid" replaced; with timed, every string is taken as timed. key is the key node is under, for the recursion.
    pub fn json(&self, node: &Value, timed: bool, key: Option<&str>) -> Value {
        match node {
            Value::Object(map) => {
                let mut out = Map::new();
                for (k, v) in map {
                    out.insert(self.text(k, false), self.json(v, timed, Some(k)));
                }
                Value::Object(out)
            }
            Value::Array(items) => Value::Array(items.iter().map(|v| self.json(v, timed, key)).collect()),
            Value::String(s) => Value::String(self.text(s, timed || key.is_some_and(|k| TIMED_TEXT_KEYS.contains(&k)))),
            Value::Number(n) if n.is_i64() || n.is_u64() => {
                if key.is_some_and(|k| TIMED_KEYS.contains(&k)) {
                    return "<seconds>".into();
                }
                if key == Some("pid") {
                    let found = n.as_i64().and_then(|p| self.pids.iter().find(|(q, _)| *q == p));
                    return found.map_or("<pid:?>", |(_, h)| h.as_str()).into();
                }
                node.clone()
            }
            other => other.clone(),
        }
    }
}

/// The content of text for a human, a normalized log, error or table, without its layout or wording: the
/// placeholders, URLs, paths, upper-case words such as verdicts, identifiers with an underscore, and words with digits
/// it holds, sorted, so that a reworded or relaid message with the same facts compares equal.
pub fn facts(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let strip: &[char] = &['.', ',', ';', ':', '(', ')', '`', '\'', '"'];
    let mut found: Vec<String> = find_all(&chars, fact_at)
        .into_iter()
        .map(|(start, end)| chars[start..end].iter().collect::<String>().trim_matches(strip).to_string())
        .collect();
    found.sort();
    found
}

/// Where the match at text's i ends, or None; as one of facts' alternatives tries, in order: a placeholder, a URL, a
/// path, an upper-case word, an identifier with an underscore, and a word with a digit.
fn fact_at(t: &[char], i: usize) -> Option<usize> {
    placeholder_at(t, i)
        .or_else(|| url_at(t, i))
        .or_else(|| path_at(t, i))
        .or_else(|| upper_word_at(t, i))
        .or_else(|| underscored_at(t, i))
        .or_else(|| digit_word_at(t, i))
}

/// `<[a-z]+(?::[0-9?]+)?>`
fn placeholder_at(t: &[char], i: usize) -> Option<usize> {
    if t.get(i) != Some(&'<') {
        return None;
    }
    let letters = span(t, i + 1, |c| c.is_ascii_lowercase());
    if letters == i + 1 {
        return None;
    }
    if t.get(letters) == Some(&':') {
        let n = span(t, letters + 1, |c| c.is_ascii_digit() || c == '?');
        if n > letters + 1 && t.get(n) == Some(&'>') {
            return Some(n + 1);
        }
    }
    (t.get(letters) == Some(&'>')).then_some(letters + 1)
}

/// `https?://\S+`
fn url_at(t: &[char], i: usize) -> Option<usize> {
    let scheme = if starts_with(t, i, "https://") {
        8
    } else if starts_with(t, i, "http://") {
        7
    } else {
        return None;
    };
    let end = span(t, i + scheme, |c| !is_space(c));
    (end > i + scheme).then_some(end)
}

/// `\S*/\S+`: the non-space run from i, where a `/` in it has a character after it.
fn path_at(t: &[char], i: usize) -> Option<usize> {
    let end = span(t, i, |c| !is_space(c));
    (i..end.saturating_sub(1)).any(|p| t[p] == '/').then_some(end)
}

/// `\b[A-Z][A-Z_]{2,}\b`
fn upper_word_at(t: &[char], i: usize) -> Option<usize> {
    if !t.get(i).is_some_and(char::is_ascii_uppercase) || !boundary(t, i) {
        return None;
    }
    let end = span(t, i + 1, |c| c.is_ascii_uppercase() || c == '_');
    (end >= i + 3 && boundary(t, end)).then_some(end)
}

/// `\b\w+_\w+\b`: a word from i with a `_` that has a word character on each side.
fn underscored_at(t: &[char], i: usize) -> Option<usize> {
    if !t.get(i).is_some_and(|c| is_word(*c)) || !boundary(t, i) {
        return None;
    }
    let end = span(t, i, is_word);
    (i + 1..end.saturating_sub(1)).any(|p| t[p] == '_').then_some(end)
}

/// `\b\S*\d\S*\b`: from a boundary at i, the longest stretch of the non-space run there that has a digit and ends
/// at a boundary.
fn digit_word_at(t: &[char], i: usize) -> Option<usize> {
    if !boundary(t, i) {
        return None;
    }
    let end = span(t, i, |c| !is_space(c));
    let digit = (i..end).find(|&p| t[p].is_ascii_digit())?;
    (digit + 1..=end).rev().find(|&j| boundary(t, j))
}

/// `\b\d{8}-\d{6}-([0-9a-f]{6})\b`: a run id as new_run_id makes it, the time it started and a random key.
fn run_id_at(t: &[char], i: usize) -> Option<usize> {
    if !boundary(t, i) {
        return None;
    }
    pattern_at(t, i, "dddddddd-dddddd-xxxxxx").filter(|&end| boundary(t, end))
}

/// Where pattern ends when it matches t at i, one character of t for each of its own: `d` an ASCII digit, `x` a hex
/// digit of [0-9a-f], `T` a `T` or a space, and any other character itself.
fn pattern_at(t: &[char], i: usize, pattern: &str) -> Option<usize> {
    let fits = |class: char, c: char| match class {
        'd' => c.is_ascii_digit(),
        'x' => is_hex(Some(c)),
        'T' => c == 'T' || c == ' ',
        literal => c == literal,
    };
    let n = pattern.chars().count();
    let matched = pattern.chars().enumerate().all(|(k, class)| t.get(i + k).is_some_and(|c| fits(class, *c)));
    matched.then_some(i + n)
}

/// `(?<![0-9a-f])[0-9a-f]{40}(?:[0-9a-f]{24})?(?![0-9a-f])` and its abbreviation, `[0-9a-f]{7,39}` so bordered: the
/// whole run of hex digits from i, if its length is one length allows.
fn hex_run_at(t: &[char], i: usize, length: impl Fn(usize) -> bool) -> Option<usize> {
    if i > 0 && is_hex(t.get(i - 1).copied()) {
        return None;
    }
    let end = span(t, i, |c| is_hex(Some(c)));
    (end > i && length(end - i)).then_some(end)
}

/// `\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?`: a time as iso_utc writes it, and as
/// turns_table shows it.
fn time_at(t: &[char], i: usize) -> Option<usize> {
    if !boundary(t, i) {
        return None;
    }
    let mut end = pattern_at(t, i, "dddd-dd-ddTdd:dd:dd")?;
    if t.get(end) == Some(&'.') {
        let digits = span(t, end + 1, |c| c.is_ascii_digit());
        if digits > end + 1 {
            end = digits;
        }
    }
    let offset = match t.get(end) {
        Some('Z') => Some(end + 1),
        // `:?` is greedy: with the colon first, else without it.
        Some('+' | '-') => pattern_at(t, end + 1, "dd:dd").or_else(|| pattern_at(t, end + 1, "dddd")),
        _ => None,
    };
    Some(offset.unwrap_or(end))
}

/// `"(heartbeat_age|mtime|seconds)": \d+`: a clock's number in JSON text, such as metrics.json's.
fn timed_json_at(t: &[char], i: usize) -> Option<usize> {
    TIMED_KEYS.iter().find_map(|key| {
        let head = format!("\"{key}\": ");
        starts_with(t, i, &head).then(|| span(t, i + head.chars().count(), |c| c.is_ascii_digit()))
            .filter(|&end| end > i + head.chars().count())
    })
}

/// `"pid": \d+`: a pid in JSON text.
fn pid_json_at(t: &[char], i: usize) -> Option<usize> {
    let head = "\"pid\": ";
    if !starts_with(t, i, head) {
        return None;
    }
    let end = span(t, i + head.len(), |c| c.is_ascii_digit());
    (end > i + head.len()).then_some(end)
}

/// `\b(?:\d+h ?\d{2}m|\d+m(?: \d{2}s)?|\d+s)\b`: a duration as format_duration and format_age write it: 5s, 2m 05s,
/// 1h 02m, 2m, 1h2m.
fn duration_at(t: &[char], i: usize) -> Option<usize> {
    if !boundary(t, i) {
        return None;
    }
    let digits = span(t, i, |c| c.is_ascii_digit());
    if digits == i {
        return None;
    }
    let two_digits_then = |p: usize, unit: char| {
        (t.get(p).is_some_and(char::is_ascii_digit) && t.get(p + 1).is_some_and(char::is_ascii_digit) && t.get(p + 2) == Some(&unit))
            .then_some(p + 3)
    };
    let at_boundary = |end: Option<usize>| end.filter(|&e| boundary(t, e));
    let hours = || {
        if t.get(digits) != Some(&'h') {
            return None;
        }
        let p = if t.get(digits + 1) == Some(&' ') { digits + 2 } else { digits + 1 };
        // ` ?` is greedy: without the space, the minutes would have to start at it.
        at_boundary(two_digits_then(p, 'm'))
    };
    let minutes = || {
        if t.get(digits) != Some(&'m') {
            return None;
        }
        let seconds = if t.get(digits + 1) == Some(&' ') { at_boundary(two_digits_then(digits + 2, 's')) } else { None };
        seconds.or_else(|| at_boundary(Some(digits + 1)))
    };
    let seconds = || (t.get(digits) == Some(&'s')).then_some(digits + 1).filter(|&e| boundary(t, e));
    hours().or_else(minutes).or_else(seconds)
}

/// Every match in t of the matcher at, which gives where a match at i ends, as (start, end), left to right, each
/// search starting where the last match ended.
fn find_all(t: &[char], at: impl Fn(&[char], usize) -> Option<usize>) -> Vec<(usize, usize)> {
    let mut found = Vec::new();
    let mut i = 0;
    while i < t.len() {
        match at(t, i) {
            Some(end) if end > i => {
                found.push((i, end));
                i = end;
            }
            _ => i += 1,
        }
    }
    found
}

/// s with each match of at replaced by what with gives for it.
fn substitute(s: &str, at: impl Fn(&[char], usize) -> Option<usize>, with: impl Fn(&str) -> String) -> String {
    let t: Vec<char> = s.chars().collect();
    let mut out = String::new();
    let mut last = 0;
    for (start, end) in find_all(&t, at) {
        out.extend(&t[last..start]);
        out.push_str(&with(&t[start..end].iter().collect::<String>()));
        last = end;
    }
    out.extend(&t[last..]);
    out
}

/// s with each occurrence of word that no character border(c) holds for touches replaced by placeholder:
/// `(?<!X)word(?!X)`, X being the class border describes.
fn replace_unbordered(s: &str, word: &str, placeholder: &str, border: impl Fn(char) -> bool) -> String {
    let n = word.chars().count();
    let w: Vec<char> = word.chars().collect();
    substitute(s, |t, i| {
        let fits = t.get(i..i + n) == Some(&w[..]);
        let before = i > 0 && border(t[i - 1]);
        let after = t.get(i + n).is_some_and(|c| border(*c));
        (fits && !before && !after).then_some(i + n)
    }, |_| placeholder.to_string())
}

fn lookup<'a>(table: &'a [(String, String)], key: &str) -> Option<&'a str> {
    table.iter().find(|(k, _)| k == key).map(|(_, p)| p.as_str())
}

/// The end of the run of characters from i that class holds for.
fn span(t: &[char], i: usize, class: impl Fn(char) -> bool) -> usize {
    let mut j = i;
    while j < t.len() && class(t[j]) {
        j += 1;
    }
    j
}

fn starts_with(t: &[char], i: usize, prefix: &str) -> bool {
    (i..).zip(prefix.chars()).all(|(j, c)| t.get(j) == Some(&c))
}

/// Whether `\b` holds at i: a word character on one side of it and none on the other, the ends counting as none.
fn boundary(t: &[char], i: usize) -> bool {
    let before = i > 0 && is_word(t[i - 1]);
    let after = t.get(i).is_some_and(|c| is_word(*c));
    before != after
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn is_hex(c: Option<char>) -> bool {
    c.is_some_and(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
}

fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

#[cfg(test)]
mod tests;
