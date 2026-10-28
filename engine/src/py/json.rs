//! JSON as orchestrator.py reads and writes it: a value whose objects keep their keys in the order they came, as a
//! Python dict does, read by json.loads and written by json.dumps with its defaults, so that state.json and
//! metrics.json come out byte for byte as the Python engine wrote them. TOML read into a Json keeps the order the
//! toml crate gives, which is its keys sorted.

use std::fmt;

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};

/// A map that keeps its keys in insertion order, as a Python dict: inserting a key it has replaces the value in
/// place. Two maps are equal when they hold the same keys with equal values, in any order, as two dicts are.
#[derive(Clone, Debug)]
pub struct Map<V> {
    entries: Vec<(String, V)>,
}

impl<V> Default for Map<V> {
    fn default() -> Self {
        Map { entries: Vec::new() }
    }
}

impl<V> Map<V> {
    pub fn new() -> Self {
        Map::default()
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn get(&self, key: &str) -> Option<&V> {
        self.entries.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    pub fn get_mut(&mut self, key: &str) -> Option<&mut V> {
        self.entries.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    pub fn contains_key(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    /// Sets key to value: in its place when the map has key, else last.
    pub fn insert(&mut self, key: impl Into<String>, value: V) {
        let key = key.into();
        match self.get_mut(&key) {
            Some(v) => *v = value,
            None => self.entries.push((key, value)),
        }
    }

    /// The value of key, taken out of the map; None when it has none.
    pub fn remove(&mut self, key: &str) -> Option<V> {
        let i = self.entries.iter().position(|(k, _)| k == key)?;
        Some(self.entries.remove(i).1)
    }

    pub fn keys(&self) -> impl Iterator<Item = &str> {
        self.entries.iter().map(|(k, _)| k.as_str())
    }

    pub fn values(&self) -> impl Iterator<Item = &V> {
        self.entries.iter().map(|(_, v)| v)
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, &V)> {
        self.entries.iter().map(|(k, v)| (k.as_str(), v))
    }
}

impl<V: PartialEq> PartialEq for Map<V> {
    fn eq(&self, other: &Self) -> bool {
        self.len() == other.len() && self.iter().all(|(k, v)| other.get(k) == Some(v))
    }
}

impl<K: Into<String>, V> FromIterator<(K, V)> for Map<V> {
    fn from_iter<I: IntoIterator<Item = (K, V)>>(iter: I) -> Self {
        let mut map = Map::new();
        for (k, v) in iter {
            map.insert(k, v);
        }
        map
    }
}

impl<V> IntoIterator for Map<V> {
    type Item = (String, V);
    type IntoIter = std::vec::IntoIter<(String, V)>;

    fn into_iter(self) -> Self::IntoIter {
        self.entries.into_iter()
    }
}

/// A JSON value as Python holds one: an int apart from a float, and objects as Maps.
#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    Object(Map<Json>),
}

impl Json {
    /// The value text holds, as json.loads reads it; the parser's error for text that is not JSON. Unlike
    /// json.loads, it refuses NaN and Infinity, which no file of the engine's holds.
    pub fn parse(text: &str) -> Result<Json, String> {
        serde_json::from_str(text).map_err(|e| e.to_string())
    }

    /// The value as json.dumps(value) writes it: ", " and ": " between items and keys, every character outside
    /// printable ASCII escaped, and a float as Python's repr writes it.
    pub fn dumps(&self) -> String {
        let mut out = String::new();
        self.write(&mut out);
        out
    }

    fn write(&self, out: &mut String) {
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Int(n) => out.push_str(&n.to_string()),
            Json::Float(x) => out.push_str(&float_text(*x)),
            Json::Str(s) => write_string(s, out),
            Json::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    item.write(out);
                }
                out.push(']');
            }
            Json::Object(map) => {
                out.push('{');
                for (i, (k, v)) in map.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    write_string(k, out);
                    out.push_str(": ");
                    v.write(out);
                }
                out.push('}');
            }
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    /// The int this is; None for anything else, a bool and a float included.
    pub fn as_int(&self) -> Option<i128> {
        match self {
            Json::Int(n) => Some(*n),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Array(items) => Some(items),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&Map<Json>> {
        match self {
            Json::Object(map) => Some(map),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Json::Null)
    }

    /// The value at key of an object; None for a key it lacks, or a value that is not an object.
    pub fn get(&self, key: &str) -> Option<&Json> {
        self.as_object().and_then(|map| map.get(key))
    }

    /// Whether Python takes the value as true: not null, false, 0, 0.0, or an empty string, list or object.
    pub fn truthy(&self) -> bool {
        match self {
            Json::Null => false,
            Json::Bool(b) => *b,
            Json::Int(n) => *n != 0,
            Json::Float(x) => *x != 0.0,
            Json::Str(s) => !s.is_empty(),
            Json::Array(items) => !items.is_empty(),
            Json::Object(map) => !map.is_empty(),
        }
    }
}

impl From<&str> for Json {
    fn from(s: &str) -> Self {
        Json::Str(s.to_string())
    }
}

impl From<String> for Json {
    fn from(s: String) -> Self {
        Json::Str(s)
    }
}

impl From<bool> for Json {
    fn from(b: bool) -> Self {
        Json::Bool(b)
    }
}

impl From<i64> for Json {
    fn from(n: i64) -> Self {
        Json::Int(n.into())
    }
}

impl<T: Into<Json>> From<Option<T>> for Json {
    fn from(value: Option<T>) -> Self {
        value.map_or(Json::Null, Into::into)
    }
}

/// s as a JSON string with json.dumps's ensure_ascii: `"` and `\` escaped, "\n", "\r", "\t", "\b" and "\f" by
/// their letters, every other character outside space to `~` as `\u` and four hex digits, a surrogate pair beyond
/// U+FFFF.
fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

/// x as Python's float.__repr__ writes it, which json.dumps uses: the shortest digits that read back as x, in
/// positional notation for exponents from -4 to 15 with at least one digit after the point, else in scientific
/// notation with a sign and at least two digits in the exponent; NaN and Infinity as json.dumps writes them.
fn float_text(x: f64) -> String {
    if x.is_nan() {
        return "NaN".to_string();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity" } else { "-Infinity" }.to_string();
    }
    // Rust's {:e} gives the shortest round-trip digits, as d.ddde<exp>.
    let sci = format!("{x:e}");
    let (mantissa, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exp) {
        let point = exp + 1;
        let body = if point <= 0 {
            format!("0.{}{}", "0".repeat(point.unsigned_abs() as usize), digits)
        } else if point as usize >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            format!("{}.{}", &digits[..point as usize], &digits[point as usize..])
        };
        format!("{sign}{body}")
    } else {
        let rest = if digits.len() > 1 { format!(".{}", &digits[1..]) } else { String::new() };
        let exp_sign = if exp < 0 { '-' } else { '+' };
        format!("{sign}{}{rest}e{exp_sign}{:02}", &digits[..1], exp.unsigned_abs())
    }
}

impl<'de> Deserialize<'de> for Json {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Json, D::Error> {
        deserializer.deserialize_any(JsonVisitor)
    }
}

struct JsonVisitor;

impl<'de> Visitor<'de> for JsonVisitor {
    type Value = Json;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a JSON value")
    }

    fn visit_bool<E: de::Error>(self, b: bool) -> Result<Json, E> {
        Ok(Json::Bool(b))
    }

    fn visit_i64<E: de::Error>(self, n: i64) -> Result<Json, E> {
        Ok(Json::Int(n.into()))
    }

    fn visit_u64<E: de::Error>(self, n: u64) -> Result<Json, E> {
        Ok(Json::Int(n.into()))
    }

    fn visit_f64<E: de::Error>(self, x: f64) -> Result<Json, E> {
        Ok(Json::Float(x))
    }

    fn visit_str<E: de::Error>(self, s: &str) -> Result<Json, E> {
        Ok(Json::Str(s.to_string()))
    }

    fn visit_string<E: de::Error>(self, s: String) -> Result<Json, E> {
        Ok(Json::Str(s))
    }

    fn visit_unit<E: de::Error>(self) -> Result<Json, E> {
        Ok(Json::Null)
    }

    fn visit_none<E: de::Error>(self) -> Result<Json, E> {
        Ok(Json::Null)
    }

    fn visit_some<D: Deserializer<'de>>(self, deserializer: D) -> Result<Json, D::Error> {
        Json::deserialize(deserializer)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Json, A::Error> {
        let mut items = Vec::new();
        while let Some(item) = seq.next_element()? {
            items.push(item);
        }
        Ok(Json::Array(items))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Json, A::Error> {
        let mut map = Map::new();
        while let Some((k, v)) = access.next_entry::<String, Json>()? {
            map.insert(k, v);
        }
        Ok(Json::Object(map))
    }
}

#[cfg(test)]
mod tests;
