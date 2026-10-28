//! TOML as tomllib reads it into Python values, for the manifests the new-dependencies check reads
//! (crate::checks::dependencies): its errors, and what Python's str(), repr() and json.dumps() write of each value.

use ::toml::value::{Datetime, Offset};
use ::toml::{Table, Value};

use super::json::Json;
use super::text::repr as str_repr;

/// The table text holds, as tomllib.loads reads it; for text that is not TOML, why not, as [`syntax_error`] words
/// it. The toml crate reads TOML 1.1, some of which tomllib before Python 3.15 refuses.
pub fn loads(text: &str) -> Result<Table, String> {
    text.parse::<Table>().map_err(|e| syntax_error(text, &e))
}

/// Why text is not TOML, as tomllib words it: the parser's message and `(at line L, column C)`, where it found the
/// problem, the line and the column counted from 1, the column in characters.
pub fn syntax_error(text: &str, e: &::toml::de::Error) -> String {
    let message = e.message().trim_end();
    match e.span() {
        Some(span) => {
            let before = &text[..span.start.min(text.len())];
            let line = before.matches('\n').count() + 1;
            let column = before.rsplit('\n').next().unwrap_or(before).chars().count() + 1;
            format!("{message} (at line {line}, column {column})")
        }
        None => message.to_string(),
    }
}

/// The name of the Python type tomllib reads value as.
pub fn type_name(value: &Value) -> &'static str {
    match value {
        Value::String(_) => "str",
        Value::Integer(_) => "int",
        Value::Float(_) => "float",
        Value::Boolean(_) => "bool",
        Value::Datetime(d) => match (d.date, d.time) {
            (Some(_), Some(_)) => "datetime",
            (Some(_), None) => "date",
            _ => "time",
        },
        Value::Array(_) => "list",
        Value::Table(_) => "dict",
    }
}

/// value as Python's str() writes it: a string as it is, a datetime, date or time in ISO 8601 with a blank between
/// the date and the time (`1979-05-27 07:32:00+00:00`), anything else as repr.
pub fn str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Datetime(d) => datetime_str(d),
        other => repr(other),
    }
}

/// A TOML datetime as Python's str writes what tomllib reads it as.
fn datetime_str(d: &Datetime) -> String {
    let date = d.date.map(|date| format!("{:04}-{:02}-{:02}", date.year, date.month, date.day));
    let clock = d.time.map(|t| {
        let micro = t.nanosecond.unwrap_or(0) / 1000;
        let fraction = if micro == 0 { String::new() } else { format!(".{micro:06}") };
        format!("{:02}:{:02}:{:02}{fraction}", t.hour, t.minute, t.second.unwrap_or(0))
    });
    let zone = match d.offset {
        None => String::new(),
        Some(Offset::Z) => "+00:00".to_string(),
        Some(Offset::Custom { minutes }) => {
            let sign = if minutes < 0 { '-' } else { '+' };
            format!("{sign}{:02}:{:02}", minutes.unsigned_abs() / 60, minutes.unsigned_abs() % 60)
        }
    };
    match (date, clock) {
        (Some(date), Some(clock)) => format!("{date} {clock}{zone}"),
        (Some(date), None) => date,
        (None, clock) => clock.unwrap_or_default(),
    }
}

/// value as Python's repr() writes it: `'text'`, `5`, `2.5`, `True`, `[1, 'a']`, `{'k': 1}`, or a datetime's
/// `datetime.date(2020, 1, 31)`.
pub fn repr(value: &Value) -> String {
    match value {
        Value::String(s) => str_repr(s),
        Value::Integer(n) => n.to_string(),
        Value::Float(x) => Json::Float(*x).python_str(),
        Value::Boolean(b) => if *b { "True" } else { "False" }.to_string(),
        Value::Datetime(d) => datetime_repr(d),
        Value::Array(items) => format!("[{}]", items.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Table(table) => {
            format!(
                "{{{}}}",
                table.iter().map(|(k, v)| format!("{}: {}", str_repr(k), repr(v))).collect::<Vec<_>>().join(", ")
            )
        }
    }
}

/// value as json.dumps writes it; for a datetime, date or time anywhere in it, json.dumps's TypeError's message.
pub fn json_dumps(value: &Value) -> Result<String, String> {
    to_json(value).map(|json| json.dumps())
}

fn to_json(value: &Value) -> Result<Json, String> {
    Ok(match value {
        Value::String(s) => Json::Str(s.clone()),
        Value::Integer(n) => Json::Int((*n).into()),
        Value::Float(x) => Json::Float(*x),
        Value::Boolean(b) => Json::Bool(*b),
        Value::Datetime(_) => return Err(format!("Object of type {} is not JSON serializable", type_name(value))),
        Value::Array(items) => Json::Array(items.iter().map(to_json).collect::<Result<_, _>>()?),
        Value::Table(table) => {
            Json::Object(table.iter().map(|(k, v)| Ok((k.clone(), to_json(v)?))).collect::<Result<_, String>>()?)
        }
    })
}

/// A TOML datetime as Python's repr writes what tomllib reads it as: datetime.datetime, datetime.date or
/// datetime.time, a second and a microsecond only when not 0, and its offset as a datetime.timezone.
fn datetime_repr(d: &Datetime) -> String {
    let clock = d.time.map(|t| {
        let (second, micro) = (t.second.unwrap_or(0), t.nanosecond.unwrap_or(0) / 1000);
        let mut fields = vec![t.hour.to_string(), t.minute.to_string()];
        if second != 0 || micro != 0 {
            fields.push(second.to_string());
        }
        if micro != 0 {
            fields.push(micro.to_string());
        }
        fields.join(", ")
    });
    let date = d.date.map(|date| format!("{}, {}, {}", date.year, date.month, date.day));
    match (date, clock) {
        (Some(date), Some(clock)) => {
            let zone = match d.offset {
                None => String::new(),
                Some(Offset::Z) | Some(Offset::Custom { minutes: 0 }) => ", tzinfo=datetime.timezone.utc".to_string(),
                Some(Offset::Custom { minutes }) => {
                    let seconds = i64::from(minutes) * 60;
                    let delta = if seconds < 0 {
                        format!("days=-1, seconds={}", 86_400 + seconds)
                    } else {
                        format!("seconds={seconds}")
                    };
                    format!(", tzinfo=datetime.timezone(datetime.timedelta({delta}))")
                }
            };
            format!("datetime.datetime({date}, {clock}{zone})")
        }
        (Some(date), None) => format!("datetime.date({date})"),
        (None, clock) => format!("datetime.time({})", clock.unwrap_or_default()),
    }
}

#[cfg(test)]
mod tests;
