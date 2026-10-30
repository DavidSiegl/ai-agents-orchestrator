//! What differs between two runs of one scenario, and so between a recording and its replay: `Normalizer`.
//!
//! Times, run ids, pids and git object names, the world's directory and the host's name. Everything else in a record
//! is compared as it is.
//!
//! The recordings were normalized by tests/parity/normalize.py's regular expressions, which this module matches by
//! hand, with their semantics: `\b` is a change between a word character (a letter, a digit or `_`, as Python's
//! Unicode `\w`) and any other, `\d` an ASCII digit (the records hold no other), and hex is `[0-9a-f]`.

use octoflow_engine::py::json::{Json, Map};

/// The JSON keys whose numbers are times measured by a clock: a turn's or a run's seconds, a state's age, a file's
/// modification time. Sorted, as normalize.py's alternation of them is.
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
    /// Each literal and its placeholder, the longest literal first.
    literals: Vec<(String, String)>,
    /// A run id to its placeholder, in the order learnt.
    runs: Map<String>,
    /// A run's key, the six hex digits that end its id, to its placeholder.
    keys: Map<String>,
    /// A git object name to its placeholder.
    shas: Map<String>,
    /// A pid to its placeholder, in the order registered; a pid registered again keeps its place.
    pids: Vec<(i64, String)>,
}

impl Normalizer {
    /// A normalizer that knows only the host's name, as `<host>`. Panics when the host's name cannot be read.
    pub fn new() -> Normalizer {
        let host = nix::unistd::gethostname().unwrap_or_else(|e| panic!("reading the host's name: {e}"));
        let mut n = Normalizer {
            literals: Vec::new(),
            runs: Map::new(),
            keys: Map::new(),
            shas: Map::new(),
            pids: Vec::new(),
        };
        n.literal(&host.to_string_lossy(), "<host>");
        n
    }

    /// Write placeholder wherever text appears; a longer text registered is replaced before a shorter one.
    pub fn literal(&mut self, text: &str, placeholder: &str) {
        self.literals.push((text.to_string(), placeholder.to_string()));
        self.literals.sort_by_key(|(text, _)| std::cmp::Reverse(text.chars().count()));
    }

    /// Write `<pid:n>` for pid, a process of engine invocation n, as a number in JSON and as a word in text.
    pub fn pid(&mut self, pid: i64, n: usize) {
        let placeholder = format!("<pid:{n}>");
        match self.pids.iter_mut().find(|(p, _)| *p == pid) {
            Some(entry) => entry.1 = placeholder,
            None => self.pids.push((pid, placeholder)),
        }
    }

    /// Number the runs and git objects in record in the order they appear in its json.dumps text, after those learnt
    /// before.
    pub fn learn(&mut self, record: &Json) {
        let text: Vec<char> = record.dumps().chars().collect();
        let mut i = 0;
        while i < text.len() {
            match run_id(&text, i) {
                Some(end) => {
                    let id: String = text[i..end].iter().collect();
                    if !self.runs.contains_key(&id) {
                        let n = self.runs.len() + 1;
                        self.keys.insert(&id[id.len() - 6..], format!("<key:{n}>"));
                        self.runs.insert(id, format!("<run:{n}>"));
                    }
                    i = end;
                }
                None => i += 1,
            }
        }
        let mut i = 0;
        while i < text.len() {
            match sha(&text, i) {
                Some(end) => {
                    let name: String = text[i..end].iter().collect();
                    if !self.shas.contains_key(&name) {
                        let placeholder = format!("<sha:{}>", self.shas.len() + 1);
                        self.shas.insert(name, placeholder);
                    }
                    i = end;
                }
                None => i += 1,
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
        s = substitute(&s, |c, i| {
            let end = run_id(c, i)?;
            let id: String = c[i..end].iter().collect();
            Some((end, self.runs.get(&id).cloned().unwrap_or_else(|| "<run:?>".to_string())))
        });
        for (key, placeholder) in self.keys.iter() {
            let key: Vec<char> = key.chars().collect();
            s = substitute(&s, |c, i| {
                let end = i + key.len();
                let found = c.get(i..end) == Some(&key[..]) && !hex_at(c, i.wrapping_sub(1)) && !hex_at(c, end);
                found.then(|| (end, placeholder.clone()))
            });
        }
        s = substitute(&s, |c, i| {
            let end = sha(c, i)?;
            let name: String = c[i..end].iter().collect();
            Some((end, self.shas.get(&name).cloned().unwrap_or_else(|| "<sha:?>".to_string())))
        });
        s = substitute(&s, |c, i| {
            let end = hex_run(c, i).filter(|end| (7..=39).contains(&(end - i)))?;
            let prefix: String = c[i..end].iter().collect();
            let mut found = self.shas.iter().filter(|(name, _)| name.starts_with(&prefix));
            let placeholder = match (found.next(), found.next()) {
                (Some((_, p)), None) => p.clone(),
                _ => prefix,
            };
            Some((end, placeholder))
        });
        s = substitute(&s, |c, i| time(c, i).map(|end| (end, "<time>".to_string())));
        s = substitute(&s, |c, i| {
            TIMED_KEYS.iter().find_map(|key| {
                let end = json_number(c, i, key)?;
                Some((end, format!("\"{key}\": \"<seconds>\"")))
            })
        });
        s = substitute(&s, |c, i| {
            let end = json_number(c, i, "pid")?;
            let digits: String = c[i + "\"pid\": ".len()..end].iter().collect();
            let pid = digits.parse::<i64>().ok().and_then(|p| self.pid_placeholder(p));
            Some((end, format!("\"pid\": \"{}\"", pid.unwrap_or("<pid:?>"))))
        });
        for (pid, placeholder) in &self.pids {
            let digits: Vec<char> = pid.to_string().chars().collect();
            s = substitute(&s, |c, i| {
                let end = i + digits.len();
                let found = c.get(i..end) == Some(&digits[..]) && boundary(c, i) && boundary(c, end);
                found.then(|| (end, placeholder.clone()))
            });
        }
        if timed {
            s = substitute(&s, |c, i| duration(c, i).map(|end| (end, "<duration>".to_string())));
        }
        s
    }

    fn pid_placeholder(&self, pid: i64) -> Option<&str> {
        self.pids.iter().find(|(p, _)| *p == pid).map(|(_, placeholder)| placeholder.as_str())
    }

    /// node with text() applied to every string and key in it, and the ints under TIMED_KEYS and the pids under "pid"
    /// replaced; with timed, every string is taken as timed. key is the key node is under, if any.
    pub fn json(&self, node: &Json, timed: bool, key: Option<&str>) -> Json {
        match node {
            Json::Object(map) => {
                Json::Object(map.iter().map(|(k, v)| (self.text(k, false), self.json(v, timed, Some(k)))).collect())
            }
            Json::Array(items) => Json::Array(items.iter().map(|v| self.json(v, timed, key)).collect()),
            Json::Str(s) => Json::Str(self.text(s, timed || key.is_some_and(|k| TIMED_TEXT_KEYS.contains(&k)))),
            Json::Int(_) if key.is_some_and(|k| TIMED_KEYS.contains(&k)) => Json::from("<seconds>"),
            Json::Int(n) if key == Some("pid") => {
                let pid = i64::try_from(*n).ok().and_then(|p| self.pid_placeholder(p));
                Json::from(pid.unwrap_or("<pid:?>"))
            }
            other => other.clone(),
        }
    }
}

impl Default for Normalizer {
    fn default() -> Self {
        Normalizer::new()
    }
}

/// The content of text for a human, a normalized log, error or table, without its layout or wording: the
/// placeholders, URLs, paths, upper-case words such as verdicts, identifiers with an underscore, and words with digits
/// it holds, sorted, so that a reworded or relaid message with the same facts compares equal.
pub fn facts(text: &str) -> Vec<String> {
    let c: Vec<char> = text.chars().collect();
    let mut found = Vec::new();
    let mut i = 0;
    while i < c.len() {
        let end = placeholder(&c, i)
            .or_else(|| url(&c, i))
            .or_else(|| path(&c, i))
            .or_else(|| upper_word(&c, i))
            .or_else(|| identifier(&c, i))
            .or_else(|| with_digit(&c, i));
        match end {
            Some(end) => {
                let fact: String = c[i..end].iter().collect();
                found.push(fact.trim_matches(|ch| ".,;:()`'\"".contains(ch)).to_string());
                i = end;
            }
            None => i += 1,
        }
    }
    found.sort();
    found
}

/// s with each match of a matcher replaced, scanning from the left as re.sub does: the matcher is given s's characters
/// and a position, and answers the end of a match that starts there and its replacement, or None.
fn substitute(s: &str, matcher: impl Fn(&[char], usize) -> Option<(usize, String)>) -> String {
    let c: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < c.len() {
        match matcher(&c, i) {
            Some((end, replacement)) => {
                out.push_str(&replacement);
                i = end;
            }
            None => {
                out.push(c[i]);
                i += 1;
            }
        }
    }
    out
}

fn is_word(ch: char) -> bool {
    ch.is_alphanumeric() || ch == '_'
}

fn is_hex(ch: char) -> bool {
    ch.is_ascii_digit() || ('a'..='f').contains(&ch)
}

/// Whether c has a hex digit at i; false beyond either end (i being usize::MAX for the position before the first).
fn hex_at(c: &[char], i: usize) -> bool {
    c.get(i).copied().is_some_and(is_hex)
}

/// Whether `\b` holds at i: between a word character and another, the ends counting as other.
fn boundary(c: &[char], i: usize) -> bool {
    let before = i.checked_sub(1).and_then(|j| c.get(j)).copied().is_some_and(is_word);
    let after = c.get(i).copied().is_some_and(is_word);
    before != after
}

/// The end of the run of `n` ASCII digits at i; None when fewer are there.
fn digits(c: &[char], i: usize, n: usize) -> Option<usize> {
    c.get(i..i + n).filter(|run| run.iter().all(char::is_ascii_digit)).map(|_| i + n)
}

/// The end of the longest run of ASCII digits at i; None for none.
fn digit_run(c: &[char], i: usize) -> Option<usize> {
    let n = c.get(i..)?.iter().take_while(|ch| ch.is_ascii_digit()).count();
    (n > 0).then_some(i + n)
}

/// The end of ch at i.
fn char_at(c: &[char], i: usize, ch: char) -> Option<usize> {
    (c.get(i) == Some(&ch)).then_some(i + 1)
}

/// The end of the whole run of hex digits that starts at i, when one does (c has no hex digit just before i).
fn hex_run(c: &[char], i: usize) -> Option<usize> {
    if hex_at(c, i.wrapping_sub(1)) || !hex_at(c, i) {
        return None;
    }
    Some(i + c[i..].iter().take_while(|&&ch| is_hex(ch)).count())
}

/// A run id as new_run_id makes it, at i: `\b\d{8}-\d{6}-[0-9a-f]{6}\b`, the time it started and a random key.
fn run_id(c: &[char], i: usize) -> Option<usize> {
    if !boundary(c, i) {
        return None;
    }
    let end = digits(c, i, 8)?;
    let end = char_at(c, end, '-')?;
    let end = digits(c, end, 6)?;
    let end = char_at(c, end, '-')?;
    let end = c.get(end..end + 6).filter(|key| key.iter().all(|&ch| is_hex(ch))).map(|_| end + 6)?;
    boundary(c, end).then_some(end)
}

/// A git object name at i: a whole run of SHA-1's 40 hex digits or SHA-256's 64.
fn sha(c: &[char], i: usize) -> Option<usize> {
    hex_run(c, i).filter(|end| [40, 64].contains(&(end - i)))
}

/// A time at i as iso_utc writes it, or turns_table shows it with a space for the T and no offset:
/// `\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?`.
fn time(c: &[char], i: usize) -> Option<usize> {
    if !boundary(c, i) {
        return None;
    }
    let mut end = digits(c, i, 4)?;
    for (sep, n) in [('-', 2), ('-', 2)] {
        end = digits(c, char_at(c, end, sep)?, n)?;
    }
    end = char_at(c, end, 'T').or_else(|| char_at(c, end, ' '))?;
    end = digits(c, end, 2)?;
    for sep in [':', ':'] {
        end = digits(c, char_at(c, end, sep)?, 2)?;
    }
    if let Some(fraction) = char_at(c, end, '.').and_then(|e| digit_run(c, e)) {
        end = fraction;
    }
    let offset = char_at(c, end, 'Z').or_else(|| {
        let e = char_at(c, end, '+').or_else(|| char_at(c, end, '-'))?;
        let e = digits(c, e, 2)?;
        digits(c, char_at(c, e, ':').unwrap_or(e), 2)
    });
    Some(offset.unwrap_or(end))
}

/// `"key": ` and the digits after it, at i, as JSON text such as metrics.json's holds a number: their end.
fn json_number(c: &[char], i: usize, key: &str) -> Option<usize> {
    let head: Vec<char> = format!("\"{key}\": ").chars().collect();
    (c.get(i..i + head.len()) == Some(&head[..])).then_some(())?;
    digit_run(c, i + head.len())
}

/// A duration at i as format_duration and format_age write it, `5s`, `2m 05s`, `1h 02m`, `2m` or `1h2m`:
/// `\b(?:\d+h ?\d{2}m|\d+m(?: \d{2}s)?|\d+s)\b`.
fn duration(c: &[char], i: usize) -> Option<usize> {
    if !boundary(c, i) {
        return None;
    }
    let number = digit_run(c, i)?;
    let hours = char_at(c, number, 'h').and_then(|e| {
        let e = char_at(c, e, ' ').unwrap_or(e);
        char_at(c, digits(c, e, 2)?, 'm')
    });
    if let Some(end) = hours.filter(|&end| boundary(c, end)) {
        return Some(end);
    }
    if let Some(minutes) = char_at(c, number, 'm') {
        let seconds = char_at(c, minutes, ' ').and_then(|e| char_at(c, digits(c, e, 2)?, 's'));
        if let Some(end) = seconds.filter(|&end| boundary(c, end)) {
            return Some(end);
        }
        if boundary(c, minutes) {
            return Some(minutes);
        }
    }
    char_at(c, number, 's').filter(|&end| boundary(c, end))
}

/// The end of the run of characters at i that are not whitespace.
fn non_space_end(c: &[char], i: usize) -> usize {
    i + c[i..].iter().take_while(|ch| !ch.is_whitespace()).count()
}

/// A placeholder at i: `<[a-z]+(?::[0-9?]+)?>`.
fn placeholder(c: &[char], i: usize) -> Option<usize> {
    let start = char_at(c, i, '<')?;
    let name = start + c[start..].iter().take_while(|ch| ch.is_ascii_lowercase()).count();
    if name == start {
        return None;
    }
    let after = char_at(c, name, ':').and_then(|e| {
        let n = c[e..].iter().take_while(|&&ch| ch.is_ascii_digit() || ch == '?').count();
        char_at(c, e + n, '>').filter(|_| n > 0)
    });
    after.or_else(|| char_at(c, name, '>'))
}

/// A URL at i: `https?://\S+`.
fn url(c: &[char], i: usize) -> Option<usize> {
    let rest: String = c.get(i..i + 8)?.iter().collect();
    let scheme = ["https://", "http://"].iter().find(|s| rest.starts_with(*s))?;
    let end = non_space_end(c, i + scheme.len());
    (end > i + scheme.len()).then_some(end)
}

/// A path at i: `\S*/\S+`, a run of non-space characters from i with a / that has one after it.
fn path(c: &[char], i: usize) -> Option<usize> {
    let end = non_space_end(c, i);
    c[i..end.saturating_sub(1).max(i)].contains(&'/').then_some(end)
}

/// An upper-case word at i, such as a verdict: `\b[A-Z][A-Z_]{2,}\b`.
fn upper_word(c: &[char], i: usize) -> Option<usize> {
    if !boundary(c, i) || !c[i].is_ascii_uppercase() {
        return None;
    }
    let n = c[i + 1..].iter().take_while(|&&ch| ch.is_ascii_uppercase() || ch == '_').count();
    (n >= 2 && boundary(c, i + 1 + n)).then_some(i + 1 + n)
}

/// An identifier with an underscore at i: `\b\w+_\w+\b`, a whole word with a _ neither first nor last.
fn identifier(c: &[char], i: usize) -> Option<usize> {
    if !boundary(c, i) || !is_word(c[i]) {
        return None;
    }
    let end = i + c[i..].iter().take_while(|&&ch| is_word(ch)).count();
    c[i + 1..end.saturating_sub(1).max(i + 1)].contains(&'_').then_some(end)
}

/// A word with a digit at i: `\b\S*\d\S*\b`, through the last position within the run of non-space characters that
/// follows its first digit and is a word boundary, as the regular expression's backtracking finds it.
fn with_digit(c: &[char], i: usize) -> Option<usize> {
    if !boundary(c, i) {
        return None;
    }
    let end = non_space_end(c, i);
    let first_digit = i + c[i..end].iter().position(char::is_ascii_digit)?;
    (first_digit + 1..=end).rev().find(|&e| boundary(c, e))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn normalizer() -> Normalizer {
        let mut n = Normalizer::new();
        n.learn(&Json::from("20260101-120000-abc123 and 0123456789abcdef0123456789abcdef01234567"));
        n
    }

    #[test]
    fn runs_keys_and_shas_are_numbered_as_learnt() {
        let n = normalizer();
        let text = n.text("run 20260101-120000-abc123 (key abc123), commit 0123456789abcdef0123456789abcdef01234567", false);
        assert_eq!(text, "run <run:1> (key <key:1>), commit <sha:1>");
        assert_eq!(n.text("20991231-000000-ffffff", false), "<run:?>");
    }

    #[test]
    fn an_abbreviated_sha_is_named_only_when_one_object_has_it() {
        let n = normalizer();
        assert_eq!(n.text("at 0123456 then", false), "at <sha:1> then");
        assert_eq!(n.text("at 012345 then", false), "at 012345 then");
        assert_eq!(n.text("at 1234567 then", false), "at 1234567 then");
    }

    #[test]
    fn times_seconds_and_pids_are_replaced() {
        let mut n = Normalizer::new();
        n.pid(4242, 2);
        let text = r#"at 2026-01-02T03:04:05.123+00:00, "seconds": 12, "pid": 4242, "pid": 7, pid 4242 x42420"#;
        assert_eq!(
            n.text(text, false),
            r#"at <time>, "seconds": "<seconds>", "pid": "<pid:2>", "pid": "<pid:?>", pid <pid:2> x42420"#
        );
    }

    #[test]
    fn durations_are_replaced_only_in_timed_text() {
        let n = Normalizer::new();
        // 1h2m is no duration: the pattern takes two digits of minutes after the hours.
        let text = "took 5s, 2m 05s, 1h 02m, 2m, 1h2m and 3ms";
        assert_eq!(n.text(text, false), text);
        assert_eq!(n.text(text, true), "took <duration>, <duration>, <duration>, <duration>, 1h2m and 3ms");
    }

    #[test]
    fn json_replaces_timed_ints_pids_and_keys() {
        let mut n = Normalizer::new();
        n.literal("/w", "<world>");
        n.pid(10, 1);
        let node = Json::Object(Map::from_iter([
            ("/w/x", Json::from("/w/y")),
            ("seconds", Json::from(3)),
            ("pid", Json::from(10)),
            ("health_reason", Json::from("beat 3s ago")),
            ("n", Json::from(3)),
        ]));
        let got = n.json(&node, false, None).dumps();
        assert_eq!(
            got,
            r#"{"<world>/x": "<world>/y", "seconds": "<seconds>", "pid": "<pid:1>", "health_reason": "beat <duration> ago", "n": 3}"#
        );
    }

    #[test]
    fn facts_are_the_placeholders_paths_words_and_numbers_sorted() {
        let got = facts("run <run:1> failed: VERDICT_X in src/calc.py (see https://x.example/a), exit 3; build_path ok.");
        assert_eq!(got, ["3", "<run:1>", "VERDICT_X", "build_path", "https://x.example/a", "src/calc.py"]);
    }
}
