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
    let Some(offset) = chars[start..].iter().position(|&c| c == '{' || c == '}') else {
        *pos = chars.len();
        return Ok(Chunk { literal: chars[start..].iter().collect(), field: None });
    };
    let brace_at = start + offset;
    let brace = chars[brace_at];
    *pos = brace_at + 1;
    if chars.get(*pos) == Some(&brace) {
        // A doubled brace: the literal keeps one of them, and no field follows.
        *pos += 1;
        return Ok(Chunk { literal: chars[start..=brace_at].iter().collect(), field: None });
    }
    if brace == '}' {
        return Err(FormatError::value("Single '}' encountered in format string"));
    }
    if *pos >= chars.len() {
        return Err(FormatError::value("Single '{' encountered in format string"));
    }
    let literal = chars[start..brace_at].iter().collect();
    let field = parse_field(chars, pos)?;
    Ok(Chunk { literal, field: Some(field) })
}

/// The field that starts at chars[*pos], just after its `{`, read up to and past its `}` (parse_field in CPython's
/// Objects/stringlib/unicode_format.h).
fn parse_field(chars: &[char], pos: &mut usize) -> Result<Field, FormatError> {
    let (name, name_end) = field_name_at(chars, pos)?;
    let (conversion, end) = match name_end {
        '!' => {
            let (conversion, end) = conversion_at(chars, pos)?;
            (Some(conversion), end)
        }
        _ => (None, Some(name_end)),
    };
    let spec = if end == Some('}') { String::new() } else { format_spec_at(chars, pos)? };
    Ok(Field { name, spec, conversion })
}

/// The field name at chars[*pos], read up to and past the `}`, `:` or `!` that ends it, and that character. A `[`
/// hides those up to the next `]`, so that `{a[:]}` is named `a[:]`. ValueError for a `{` in the name, or a template
/// that ends before the name does.
fn field_name_at(chars: &[char], pos: &mut usize) -> Result<(String, char), FormatError> {
    let start = *pos;
    while *pos < chars.len() {
        let c = chars[*pos];
        *pos += 1;
        match c {
            '{' => return Err(FormatError::value("unexpected '{' in field name")),
            '[' => {
                while *pos < chars.len() && chars[*pos] != ']' {
                    *pos += 1;
                }
            }
            '}' | ':' | '!' => return Ok((chars[start..*pos - 1].iter().collect(), c)),
            _ => {}
        }
    }
    Err(FormatError::value("expected '}' before end of string"))
}

/// The conversion at chars[*pos], just after its `!`, and the `}` or `:` after it, both read past; None for the
/// latter when the template ends after the conversion. ValueError when the template ends at the `!`, or another
/// character follows the conversion.
fn conversion_at(chars: &[char], pos: &mut usize) -> Result<(char, Option<char>), FormatError> {
    let Some(&conversion) = chars.get(*pos) else {
        return Err(FormatError::value("end of string while looking for conversion specifier"));
    };
    *pos += 1;
    let end = chars.get(*pos).copied();
    *pos += usize::from(end.is_some());
    match end {
        None | Some('}' | ':') => Ok((conversion, end)),
        Some(_) => Err(FormatError::value("expected ':' after conversion specifier")),
    }
}

/// The format spec at chars[*pos], read up to and past the `}` that closes its field, with its nested fields as
/// written. ValueError when the template ends first.
fn format_spec_at(chars: &[char], pos: &mut usize) -> Result<String, FormatError> {
    let start = *pos;
    let mut depth = 1;
    while *pos < chars.len() {
        let c = chars[*pos];
        *pos += 1;
        if c == '{' {
            depth += 1;
        } else if c == '}' {
            depth -= 1;
            if depth == 0 {
                return Ok(chars[start..*pos - 1].iter().collect());
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
    /// Numbered automatically (`{}`), the next such field to get this index.
    Automatic(usize),
    Manual,
}

impl Numbering {
    /// The index of the next field numbered automatically (`{}`), from 0, which it uses up; ValueError once a field
    /// has been numbered by hand.
    fn next_automatic(&mut self) -> Result<usize, FormatError> {
        let n = match self {
            Numbering::Manual => {
                return Err(FormatError::value(
                    "cannot switch from manual field specification to automatic field numbering",
                ))
            }
            Numbering::Unset => 0,
            Numbering::Automatic(n) => *n,
        };
        *self = Numbering::Automatic(n + 1);
        Ok(n)
    }

    /// Records a field numbered by hand (`{0}`); ValueError once a field has been numbered automatically.
    fn number_manually(&mut self) -> Result<(), FormatError> {
        if let Numbering::Automatic(_) = self {
            return Err(FormatError::value(
                "cannot switch from automatic field numbering to manual field specification",
            ));
        }
        *self = Numbering::Manual;
        Ok(())
    }
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
/// str (get_field_object in CPython).
fn field_value(
    name: &str,
    values: &dyn Fn(&str) -> Option<String>,
    numbering: &mut Numbering,
) -> Result<String, FormatError> {
    let chars: Vec<char> = name.chars().collect();
    let first_end = chars.iter().position(|&c| c == '.' || c == '[').unwrap_or(chars.len());
    let first: String = chars[..first_end].iter().collect();
    let index = if first.is_empty() {
        Some(numbering.next_automatic()?)
    } else if let Some(n) = decimal(&chars[..first_end])? {
        numbering.number_manually()?;
        Some(n)
    } else {
        None
    };
    if let Some(n) = index {
        return Err(FormatError {
            kind: ErrorKind::IndexError,
            message: format!("Replacement index {n} out of range for positional args tuple"),
        });
    }
    let mut value = values(&first).ok_or_else(|| FormatError { kind: ErrorKind::KeyError, message: repr(&first) })?;
    let mut pos = first_end;
    while pos < chars.len() {
        value = accessor_at(&chars, &mut pos)?.apply_to(&value)?;
    }
    Ok(value)
}

/// A part of a field name after its first, which looks something up in the value before it.
enum Accessor<'a> {
    /// `.attribute`: the attribute's name, never empty.
    Attribute(&'a [char]),
    /// `[index]`: the index as written between the brackets, never empty.
    Index(&'a [char]),
}

impl Accessor<'_> {
    /// What this looks up in value, a str: the character at a decimal index. AttributeError for any attribute,
    /// IndexError for an index past value's end, TypeError for an index that is not decimal, and ValueError for one
    /// with too many digits.
    fn apply_to(&self, value: &str) -> Result<String, FormatError> {
        let index = match *self {
            Accessor::Attribute(name) => {
                return Err(FormatError {
                    kind: ErrorKind::AttributeError,
                    message: format!("'str' object has no attribute {}", repr(&name.iter().collect::<String>())),
                })
            }
            Accessor::Index(index) => index,
        };
        match decimal(index)? {
            Some(n) => value.chars().nth(n).map(String::from).ok_or(FormatError {
                kind: ErrorKind::IndexError,
                message: "string index out of range".to_string(),
            }),
            None => Err(FormatError {
                kind: ErrorKind::TypeError,
                message: "string indices must be integers, not 'str'".to_string(),
            }),
        }
    }
}

/// The `.attribute` or `[index]` at chars[*pos], which must be its `.` or `[`, read past (FieldNameIterator in
/// CPython). ValueError for an empty one, an index without its `]`, or an index followed by anything but a `.` or
/// `[`.
fn accessor_at<'a>(chars: &'a [char], pos: &mut usize) -> Result<Accessor<'a>, FormatError> {
    let opener = chars[*pos];
    *pos += 1;
    let start = *pos;
    if opener == '.' {
        while *pos < chars.len() && chars[*pos] != '.' && chars[*pos] != '[' {
            *pos += 1;
        }
        if *pos == start {
            return Err(FormatError::value("Empty attribute in format string"));
        }
        return Ok(Accessor::Attribute(&chars[start..*pos]));
    }
    // opener is '[': an index, up to its ']'.
    while *pos < chars.len() && chars[*pos] != ']' {
        *pos += 1;
    }
    if *pos >= chars.len() {
        return Err(FormatError::value("Missing ']' in format string"));
    }
    if *pos == start {
        return Err(FormatError::value("Empty attribute in format string"));
    }
    let index = &chars[start..*pos];
    *pos += 1;
    if *pos < chars.len() && chars[*pos] != '.' && chars[*pos] != '[' {
        return Err(FormatError::value("Only '.' or '[' may follow ']' in format field specifier"));
    }
    Ok(Accessor::Index(index))
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
    let spec = FormatSpec::parse(spec)?;
    spec.check_for_str()?;
    Ok(spec.lay_out(value))
}

/// A standard format spec as CPython's parse_internal_render_format_spec reads it for a str, before the checks a str
/// makes of it.
struct FormatSpec {
    /// The character padding to the width: as given, else `0` for the zero padding of old, else a space.
    fill: char,
    /// `<`, `>`, `^` or `=`; `<` where none is given, as for a str.
    align: char,
    /// `+`, `-` or a space, if given.
    sign: Option<char>,
    /// Whether `z` is given.
    no_negative_zero: bool,
    /// Whether `#` is given.
    alternate: bool,
    /// The minimum width in characters; 0 where none is given.
    width: usize,
    /// The precision, a str's maximum width in characters, if given.
    precision: Option<usize>,
    /// The presentation type, `s` where none is given.
    presentation: char,
}

impl FormatSpec {
    /// spec, which must not be empty, read; the ValueError Python raises for both groupings, a `.` without a
    /// precision, more than one character after the precision, a grouping the presentation type does not take, or a
    /// width or precision with too many digits.
    fn parse(spec: &str) -> Result<FormatSpec, FormatError> {
        let chars: Vec<char> = spec.chars().collect();
        let mut pos = 0;
        let (fill, align) = fill_and_align_at(&chars, &mut pos);
        let sign = chars.get(pos).copied().filter(|c| matches!(c, '+' | '-' | ' '));
        pos += usize::from(sign.is_some());
        let no_negative_zero = chars.get(pos) == Some(&'z');
        pos += usize::from(no_negative_zero);
        let alternate = chars.get(pos) == Some(&'#');
        pos += usize::from(alternate);
        // The zero padding of old: a fill of 0, aligned as a str is by default, to the left.
        let zero_padded = fill.is_none() && chars.get(pos) == Some(&'0');
        pos += usize::from(zero_padded);
        let fill = fill.unwrap_or(if zero_padded { '0' } else { ' ' });
        let width = digits_at(&chars, &mut pos)?.unwrap_or(0);
        let grouping = grouping_at(&chars, &mut pos)?;
        let precision = precision_at(&chars, &mut pos)?;
        if chars.len() - pos > 1 {
            return Err(FormatError::value(format!("Invalid format specifier '{spec}' for object of type 'str'")));
        }
        let presentation = chars.get(pos).copied().unwrap_or('s');
        if let Some(g) = grouping.filter(|&g| !groups_digits(presentation, g)) {
            return Err(FormatError::value(format!("Cannot specify '{g}' with {}.", code_text(presentation))));
        }
        Ok(FormatSpec { fill, align, sign, no_negative_zero, alternate, width, precision, presentation })
    }

    /// Ok when a str may be formatted with this spec: its presentation type is `s`, and it has none of a sign, `z`,
    /// `#` and `=` alignment, which only numbers take. Otherwise the ValueError Python raises for the first of those
    /// it has.
    fn check_for_str(&self) -> Result<(), FormatError> {
        if self.presentation != 's' {
            return Err(FormatError::value(format!(
                "Unknown format code {} for object of type 'str'",
                code_text(self.presentation)
            )));
        }
        if let Some(sign) = self.sign {
            return Err(FormatError::value(if sign == ' ' {
                "Space not allowed in string format specifier"
            } else {
                "Sign not allowed in string format specifier"
            }));
        }
        if self.no_negative_zero {
            return Err(FormatError::value("Negative zero coercion (z) not allowed in string format specifier"));
        }
        if self.alternate {
            return Err(FormatError::value("Alternate form (#) not allowed in string format specifier"));
        }
        if self.align == '=' {
            return Err(FormatError::value("'=' alignment not allowed in string format specifier"));
        }
        Ok(())
    }

    /// value cut to the precision, then padded with the fill to the width on the side the alignment leaves, `=` as
    /// `<`.
    fn lay_out(&self, value: &str) -> String {
        let mut text: Vec<char> = value.chars().collect();
        if let Some(p) = self.precision {
            text.truncate(p);
        }
        let pad = self.width.saturating_sub(text.len());
        let left = match self.align {
            '>' => pad,
            '^' => pad / 2,
            _ => 0,
        };
        let mut out = String::new();
        out.extend(std::iter::repeat_n(self.fill, left));
        out.extend(text);
        out.extend(std::iter::repeat_n(self.fill, pad - left));
        out
    }
}

/// The [[fill]align] of a format spec at chars[*pos], read past: the fill, None where none is given, and the
/// alignment, `<` where none is.
fn fill_and_align_at(chars: &[char], pos: &mut usize) -> (Option<char>, char) {
    let is_align = |c: Option<&char>| matches!(c, Some('<' | '>' | '=' | '^'));
    if is_align(chars.get(*pos + 1)) {
        *pos += 2;
        (Some(chars[*pos - 2]), chars[*pos - 1])
    } else if is_align(chars.get(*pos)) {
        *pos += 1;
        (None, chars[*pos - 1])
    } else {
        (None, '<')
    }
}

/// The grouping of a format spec's integer digits at chars[*pos], `,` or `_`, read past; None where neither is.
/// ValueError when the other follows it.
fn grouping_at(chars: &[char], pos: &mut usize) -> Result<Option<char>, FormatError> {
    let grouping = chars.get(*pos).copied().filter(|c| matches!(c, ',' | '_'));
    *pos += usize::from(grouping.is_some());
    match (grouping, chars.get(*pos)) {
        (Some(','), Some('_')) | (Some('_'), Some(',')) => Err(FormatError::value("Cannot specify both ',' and '_'.")),
        _ => Ok(grouping),
    }
}

/// The precision of a format spec at chars[*pos], its `.` and digits, read past with the grouping of the fraction's
/// digits after it, which no str uses; None where no `.` is. ValueError when no digit follows the `.`, or too many do.
fn precision_at(chars: &[char], pos: &mut usize) -> Result<Option<usize>, FormatError> {
    if chars.get(*pos) != Some(&'.') {
        return Ok(None);
    }
    *pos += 1;
    let Some(precision) = digits_at(chars, pos)? else {
        return Err(FormatError::value("Format specifier missing precision"));
    };
    if matches!(chars.get(*pos), Some(',' | '_')) {
        *pos += 1;
    }
    Ok(Some(precision))
}

/// Whether presentation type presentation groups its digits with grouping, `,` or `_`. No type a str has does: a str
/// given a grouping fails on its type then.
fn groups_digits(presentation: char, grouping: char) -> bool {
    matches!(presentation, 'd' | 'e' | 'f' | 'g' | 'E' | 'G' | '%' | 'F')
        || (grouping == '_' && matches!(presentation, 'b' | 'o' | 'x' | 'X'))
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
