//! Python's format strings, str.format's language, as orchestrator.py fills in a prompt, a handoff file's name or a
//! proof command: every value is a str, given by keyword. What is filled in, and every error with its exception's
//! name and message, is what Python 3.14 gives, so that a template the Python engine refused is refused here with
//! the same problem named. https://docs.python.org/3/library/string.html#format-string-syntax
//!
//! Positional fields ({} and {0}) have no value, as str.format(**kwargs) has none, and fail as Python's do.

use std::fmt;

use super::text::{ascii, decimal_value, repr};

/// The exception a format string raises in Python, by its class name.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorKind {
    ValueError,
    KeyError,
    IndexError,
    AttributeError,
    TypeError,
}

/// Why a template cannot be parsed or filled in: Python's exception, and its message as str(exception) gives it,
/// which for a KeyError is the key's repr.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FormatError {
    pub kind: ErrorKind,
    pub message: String,
}

impl FormatError {
    fn value(message: impl Into<String>) -> Self {
        FormatError { kind: ErrorKind::ValueError, message: message.into() }
    }
}

impl fmt::Display for FormatError {
    /// The message alone, as str(exception) gives it.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl FormatError {
    /// "Name: message", as orchestrator.py writes `f"{type(e).__name__}: {e}"`.
    pub fn named(&self) -> String {
        format!("{:?}: {}", self.kind, self.message)
    }
}

/// A replacement field of a template: what string.Formatter().parse gives for it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Field {
    /// Everything before the conversion and the format spec, such as `task`, `a.b` or `x[0]`; empty for `{}`.
    pub name: String,
    /// The format spec as written, nested fields unexpanded; empty when there is none.
    pub spec: String,
    /// The character after `!`, if any.
    pub conversion: Option<char>,
}

/// One item of string.Formatter().parse: the literal text before a field, its `{{` and `}}` undoubled, and the field,
/// if one follows. A doubled brace ends an item, as it does in Python.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Chunk {
    pub literal: String,
    pub field: Option<Field>,
}

/// The template as string.Formatter().parse reads it; the ValueError Python raises for a lone brace, an unclosed
/// field or a bad conversion.
pub fn parse(template: &str) -> Result<Vec<Chunk>, FormatError> {
    chunks(template).collect()
}

/// The template's chunks one by one, as Python's MarkupIterator reads them: an error ends them, after the chunks
/// before it, so that a template filled in chunk by chunk fails on the first problem in its order.
fn chunks(template: &str) -> impl Iterator<Item = Result<Chunk, FormatError>> {
    let chars: Vec<char> = template.chars().collect();
    let mut pos = 0;
    let mut failed = false;
    std::iter::from_fn(move || {
        if failed || pos >= chars.len() {
            return None;
        }
        let chunk = next_chunk(&chars, &mut pos);
        failed = chunk.is_err();
        Some(chunk)
    })
}

/// The chunk that starts at chars[*pos], read past.
fn next_chunk(chars: &[char], pos: &mut usize) -> Result<Chunk, FormatError> {
    let start = *pos;
    let mut brace = None;
    while *pos < chars.len() {
        let c = chars[*pos];
        *pos += 1;
        if c == '{' || c == '}' {
            brace = Some(c);
            break;
        }
    }
    let at_end = *pos >= chars.len();
    let mut literal_end = *pos;
    let mut field_follows = brace.is_some();
    if let Some(c) = brace {
        if c == '}' && (at_end || chars[*pos] != '}') {
            return Err(FormatError::value("Single '}' encountered in format string"));
        }
        if c == '{' && at_end {
            return Err(FormatError::value("Single '{' encountered in format string"));
        }
        if chars[*pos] == c {
            // A doubled brace: the literal keeps one of them, and no field follows.
            *pos += 1;
            field_follows = false;
        } else {
            literal_end -= 1;
        }
    }
    let literal: String = chars[start..literal_end].iter().collect();
    let field = if field_follows { Some(parse_field(chars, pos)?) } else { None };
    Ok(Chunk { literal, field })
}

/// The field that starts at chars[*pos], just after its `{`, read up to and past its `}` (parse_field in CPython's
/// Objects/stringlib/unicode_format.h).
fn parse_field(chars: &[char], pos: &mut usize) -> Result<Field, FormatError> {
    let name_start = *pos;
    let mut last = '\0';
    while *pos < chars.len() {
        last = chars[*pos];
        *pos += 1;
        match last {
            '{' => return Err(FormatError::value("unexpected '{' in field name")),
            '[' => {
                while *pos < chars.len() && chars[*pos] != ']' {
                    *pos += 1;
                }
                continue;
            }
            '}' | ':' | '!' => break,
            _ => continue,
        }
    }
    let name: String = chars[name_start..*pos - 1].iter().collect();
    if last != '!' && last != ':' {
        if last != '}' {
            return Err(FormatError::value("expected '}' before end of string"));
        }
        return Ok(Field { name, spec: String::new(), conversion: None });
    }
    let mut conversion = None;
    if last == '!' {
        if *pos >= chars.len() {
            return Err(FormatError::value("end of string while looking for conversion specifier"));
        }
        conversion = Some(chars[*pos]);
        *pos += 1;
        if *pos < chars.len() {
            let c = chars[*pos];
            *pos += 1;
            if c == '}' {
                return Ok(Field { name, spec: String::new(), conversion });
            }
            if c != ':' {
                return Err(FormatError::value("expected ':' after conversion specifier"));
            }
        }
    }
    let spec_start = *pos;
    let mut depth = 1;
    while *pos < chars.len() {
        let c = chars[*pos];
        *pos += 1;
        if c == '{' {
            depth += 1;
        } else if c == '}' {
            depth -= 1;
            if depth == 0 {
                let spec = chars[spec_start..*pos - 1].iter().collect();
                return Ok(Field { name, spec, conversion });
            }
        }
    }
    Err(FormatError::value("unmatched '{' in format spec"))
}

/// The names of the template's fields, in order, as `[name for _, name, _, _ in Formatter().parse(t) if name is not
/// None]` gives them: a name used twice is there twice, and the fields nested in a format spec are not.
pub fn field_names(template: &str) -> Result<Vec<String>, FormatError> {
    Ok(parse(template)?.into_iter().filter_map(|chunk| chunk.field.map(|f| f.name)).collect())
}

/// template.format(**values), where values gives each keyword's str, or None for a keyword it lacks; otherwise the
/// first error Python raises, filling the template in from its start.
pub fn format(template: &str, values: &dyn Fn(&str) -> Option<String>) -> Result<String, FormatError> {
    let mut numbering = Numbering::Unset;
    build(template, values, 2, &mut numbering)
}

/// How a template numbers its positional fields, which it may not mix.
enum Numbering {
    Unset,
    Automatic(usize),
    Manual,
}

/// build_string of CPython's unicode_format.h: depth is how many levels of nested format specs may still be
/// expanded.
fn build(
    template: &str,
    values: &dyn Fn(&str) -> Option<String>,
    depth: u32,
    numbering: &mut Numbering,
) -> Result<String, FormatError> {
    if depth == 0 {
        return Err(FormatError::value("Max string recursion exceeded"));
    }
    let mut out = String::new();
    for chunk in chunks(template) {
        let chunk = chunk?;
        out.push_str(&chunk.literal);
        let Some(field) = chunk.field else { continue };
        let mut value = field_value(&field.name, values, numbering)?;
        value = match field.conversion {
            None | Some('s') => value,
            Some('r') => repr(&value),
            Some('a') => ascii(&value),
            Some(c) => return Err(FormatError::value(format!("Unknown conversion specifier {}", char_text(c)))),
        };
        let spec = if field.spec.contains('{') {
            build(&field.spec, values, depth - 1, numbering)?
        } else {
            field.spec
        };
        out.push_str(&format_str(&value, &spec)?);
    }
    Ok(out)
}

/// The value a field name gives: its first part looked up, then each `.attribute` and `[index]` after it applied to a
/// str (get_field_object and FieldNameIterator in CPython).
fn field_value(
    name: &str,
    values: &dyn Fn(&str) -> Option<String>,
    numbering: &mut Numbering,
) -> Result<String, FormatError> {
    let chars: Vec<char> = name.chars().collect();
    let first_end = chars.iter().position(|&c| c == '.' || c == '[').unwrap_or(chars.len());
    let first: String = chars[..first_end].iter().collect();
    let index = if first.is_empty() {
        let n = match numbering {
            Numbering::Manual => {
                return Err(FormatError::value(
                    "cannot switch from manual field specification to automatic field numbering",
                ))
            }
            Numbering::Unset => 0,
            Numbering::Automatic(n) => *n,
        };
        *numbering = Numbering::Automatic(n + 1);
        Some(n)
    } else if let Some(n) = decimal(&chars[..first_end])? {
        if let Numbering::Automatic(_) = numbering {
            return Err(FormatError::value(
                "cannot switch from automatic field numbering to manual field specification",
            ));
        }
        *numbering = Numbering::Manual;
        Some(n)
    } else {
        None
    };
    let mut value = match index {
        Some(n) => {
            return Err(FormatError {
                kind: ErrorKind::IndexError,
                message: format!("Replacement index {n} out of range for positional args tuple"),
            })
        }
        None => values(&first).ok_or_else(|| FormatError { kind: ErrorKind::KeyError, message: repr(&first) })?,
    };
    let mut pos = first_end;
    while pos < chars.len() {
        let c = chars[pos];
        pos += 1;
        if c == '.' {
            let start = pos;
            while pos < chars.len() && chars[pos] != '.' && chars[pos] != '[' {
                pos += 1;
            }
            if pos == start {
                return Err(FormatError::value("Empty attribute in format string"));
            }
            let attribute: String = chars[start..pos].iter().collect();
            return Err(FormatError {
                kind: ErrorKind::AttributeError,
                message: format!("'str' object has no attribute {}", repr(&attribute)),
            });
        }
        // c is '[': an index, up to its ']'.
        let start = pos;
        while pos < chars.len() && chars[pos] != ']' {
            pos += 1;
        }
        if pos >= chars.len() {
            return Err(FormatError::value("Missing ']' in format string"));
        }
        if pos == start {
            return Err(FormatError::value("Empty attribute in format string"));
        }
        let key = &chars[start..pos];
        pos += 1;
        if pos < chars.len() && chars[pos] != '.' && chars[pos] != '[' {
            return Err(FormatError::value("Only '.' or '[' may follow ']' in format field specifier"));
        }
        value = match decimal(key)? {
            Some(n) => value.chars().nth(n).map(String::from).ok_or(FormatError {
                kind: ErrorKind::IndexError,
                message: "string index out of range".to_string(),
            })?,
            None => {
                return Err(FormatError {
                    kind: ErrorKind::TypeError,
                    message: "string indices must be integers, not 'str'".to_string(),
                })
            }
        };
    }
    Ok(value)
}

/// The number that digits spell when every one is a decimal digit (decimal_value), as CPython's get_integer reads a
/// field's index or a spec's width; None when one is not, or there are none.
fn decimal(digits: &[char]) -> Result<Option<usize>, FormatError> {
    if digits.is_empty() {
        return Ok(None);
    }
    let mut n: usize = 0;
    for &c in digits {
        let Some(d) = decimal_value(c) else { return Ok(None) };
        n = n
            .checked_mul(10)
            .and_then(|n| n.checked_add(d as usize))
            .filter(|&n| n <= isize::MAX as usize)
            .ok_or_else(|| FormatError::value("Too many decimal digits in format string"))?;
    }
    Ok(Some(n))
}

/// format(value, spec) for a str: the standard format spec, [[fill]align][sign][z][#][0][width][grouping][.precision
/// [grouping]][type], with the checks a str makes of it.
fn format_str(value: &str, spec: &str) -> Result<String, FormatError> {
    if spec.is_empty() {
        return Ok(value.to_string());
    }
    let chars: Vec<char> = spec.chars().collect();
    let is_align = |c: char| matches!(c, '<' | '>' | '=' | '^');
    let mut pos = 0;
    let (mut fill, mut align) = (' ', '<');
    let mut fill_given = false;
    if chars.len() >= 2 && is_align(chars[1]) {
        (fill, align) = (chars[0], chars[1]);
        fill_given = true;
        pos = 2;
    } else if is_align(chars[0]) {
        align = chars[0];
        pos = 1;
    }
    let sign = chars.get(pos).copied().filter(|c| matches!(c, '+' | '-' | ' '));
    pos += usize::from(sign.is_some());
    let no_negative_zero = chars.get(pos) == Some(&'z');
    pos += usize::from(no_negative_zero);
    let alternate = chars.get(pos) == Some(&'#');
    pos += usize::from(alternate);
    if !fill_given && chars.get(pos) == Some(&'0') {
        // The zero padding of old: a fill of 0, aligned as a str is by default, to the left.
        fill = '0';
        pos += 1;
    }
    let width = digits_at(&chars, &mut pos)?;
    let mut grouping = None;
    if chars.get(pos) == Some(&',') {
        grouping = Some(',');
        pos += 1;
    }
    if chars.get(pos) == Some(&'_') {
        if grouping.is_some() {
            return Err(FormatError::value("Cannot specify both ',' and '_'."));
        }
        grouping = Some('_');
        pos += 1;
    }
    if chars.get(pos) == Some(&',') && grouping == Some('_') {
        return Err(FormatError::value("Cannot specify both ',' and '_'."));
    }
    let mut precision = None;
    if chars.get(pos) == Some(&'.') {
        pos += 1;
        precision = digits_at(&chars, &mut pos)?;
        if precision.is_none() {
            return Err(FormatError::value("Format specifier missing precision"));
        }
        // The grouping of the fraction's digits, which no str uses.
        if matches!(chars.get(pos), Some(',') | Some('_')) {
            pos += 1;
        }
    }
    if chars.len() - pos > 1 {
        let rest: String = chars.iter().collect();
        return Err(FormatError::value(format!("Invalid format specifier '{rest}' for object of type 'str'")));
    }
    let kind = chars.get(pos).copied().unwrap_or('s');
    if let Some(g) = grouping {
        // The presentation types that group digits, which no str has: a str fails on its type then.
        let groups = matches!(kind, 'd' | 'e' | 'f' | 'g' | 'E' | 'G' | '%' | 'F')
            || (g == '_' && matches!(kind, 'b' | 'o' | 'x' | 'X'));
        if !groups {
            return Err(FormatError::value(format!("Cannot specify '{g}' with {}.", code_text(kind))));
        }
    }
    if kind != 's' {
        return Err(FormatError::value(format!(
            "Unknown format code {} for object of type 'str'",
            code_text(kind)
        )));
    }
    if let Some(sign) = sign {
        return Err(FormatError::value(if sign == ' ' {
            "Space not allowed in string format specifier"
        } else {
            "Sign not allowed in string format specifier"
        }));
    }
    if no_negative_zero {
        return Err(FormatError::value("Negative zero coercion (z) not allowed in string format specifier"));
    }
    if alternate {
        return Err(FormatError::value("Alternate form (#) not allowed in string format specifier"));
    }
    if align == '=' {
        return Err(FormatError::value("'=' alignment not allowed in string format specifier"));
    }
    let mut text: Vec<char> = value.chars().collect();
    if let Some(p) = precision {
        text.truncate(p);
    }
    let width = width.unwrap_or(0);
    let pad = width.saturating_sub(text.len());
    let left = match align {
        '>' => pad,
        '^' => pad / 2,
        _ => 0,
    };
    let mut out = String::new();
    out.extend(std::iter::repeat_n(fill, left));
    out.extend(text);
    out.extend(std::iter::repeat_n(fill, pad - left));
    Ok(out)
}

/// A presentation type as CPython's messages quote it: the character in quotes where it is printable ASCII, else its
/// code as `'\x..'`.
fn code_text(kind: char) -> String {
    let code = kind as u32;
    if code > 32 && code < 128 {
        format!("'{kind}'")
    } else {
        format!("'\\x{code:x}'")
    }
}

/// A conversion character as CPython's error writes it: as it is where it is printable ASCII, else `\x` and its code
/// in hex digits, as few as it takes. (Unlike a presentation type's, DEL is written as its code.)
fn char_text(c: char) -> String {
    let code = c as u32;
    if code > 32 && code < 127 {
        c.to_string()
    } else {
        format!("\\x{code:x}")
    }
}

/// The number written at chars[*pos] in decimal digits, read past; None where no digit is.
fn digits_at(chars: &[char], pos: &mut usize) -> Result<Option<usize>, FormatError> {
    let start = *pos;
    while *pos < chars.len() && decimal_value(chars[*pos]).is_some() {
        *pos += 1;
    }
    decimal(&chars[start..*pos])
}

#[cfg(test)]
mod tests;
