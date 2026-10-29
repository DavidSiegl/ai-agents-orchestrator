//! What the engine needs of Python's datetime and time: the time an ISO 8601 string names, as datetime.fromisoformat
//! reads it and timestamp() and strftime's date give it, and a time as time.strftime writes it in the machine's zone.
//!
//! The machine's zone is jiff's TimeZone::system(), which reads it as the C library does for Python: the TZ variable
//! (a zone's name, a file or a POSIX rule; empty is UTC), else /etc/localtime, a zone's rules coming from
//! /usr/share/zoneinfo; and UTC where neither names one that can be read, as glibc takes it.

use jiff::tz::TimeZone;
use jiff::Timestamp;

use super::text::repr;

/// A time as `datetime.fromisoformat` reads it in CPython 3.14, which .python-version pins for the repository's tests
/// and harness.json's answers; 3.13 reads a few malformed times otherwise (TODO.md, "The Rust engine reads some input
/// no run writes otherwise").
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IsoTime {
    /// Microseconds since 1970-01-01T00:00 of its date and time as they are written, hour 24 already turned into the
    /// next day's midnight.
    local: i128,
    /// Its offset from UTC in microseconds, east positive; None for a time written without one.
    offset: Option<i128>,
}

impl IsoTime {
    /// The time iso names, read in every form 3.14's fromisoformat reads (a date as YYYY-MM-DD, YYYYMMDD or an ISO
    /// week date, then any one character and a time of hours, minutes and seconds with or without colons, a fraction
    /// of a second after `.` or `,` of which the first six digits count, hour 24 as the next day's midnight, and Z or
    /// an offset of hours, minutes, seconds and a fraction), with its quirks. Errors: what fromisoformat raises for
    /// iso, as its message says.
    pub fn parse(iso: &str) -> Result<IsoTime, String> {
        fromisoformat(iso).map(|(local, offset)| IsoTime { local, offset })
    }

    /// Seconds since the epoch, with their fraction, as `timestamp()` gives them: a time without an offset is the
    /// machine's local time (the module's doc says how its zone is found). Errors: a time without an offset within a
    /// day of the years 1 to 9999's ends, which CPython 3.14's timestamp() refuses, as its message says.
    pub fn timestamp(self) -> Result<f64, String> {
        // The machine's zone is read only for a time that needs it; UTC stands in for one that has its offset.
        let zone = if self.offset.is_some() { TimeZone::UTC } else { TimeZone::system() };
        self.timestamp_in(&zone)
    }

    /// timestamp(), a time without an offset being a time in zone; errors as its.
    fn timestamp_in(self, zone: &TimeZone) -> Result<f64, String> {
        match self.offset {
            Some(offset) => Ok(seconds_of_micros(self.local - offset)),
            None => {
                // As CPython's datetime_timestamp adds them: whole seconds, then the microseconds as a fraction.
                let (seconds, micros) = (self.local.div_euclid(1_000_000), self.local.rem_euclid(1_000_000));
                // Years 1 to 10000 (hour 24 of 9999-12-31) are far inside an i64 of seconds.
                Ok(local_to_seconds(seconds as i64, zone)? as f64 + micros as f64 / 1e6)
            }
        }
    }

    /// Its date as written, in its own offset, as `f"{time:%Y-%m-%d}"` writes it: 2026-10-29 for
    /// 2026-10-28T24:00:00Z.
    pub fn date(self) -> String {
        let (year, month, day) = civil_from_days(i64::try_from(self.local.div_euclid(86_400_000_000)).expect("years 1 to 9999 fit"));
        format!("{year:04}-{month:02}-{day:02}")
    }
}

/// The seconds since the epoch at which zone's clocks show the time shown, written as seconds since 1970-01-01T00:00
/// of its date and time, solved as CPython's local_to_seconds solves it for fold 0 (Modules/_datetimemodule.c, 3.14):
/// at a time the clocks show twice, the first; at one they skip, the time the offset before the change gives (PEP
/// 495). Errors: a time whose solving reaches a year beyond 1 to 9999, which CPython 3.14 refuses, as its message
/// says.
fn local_to_seconds(shown: i64, zone: &TimeZone) -> Result<i64, String> {
    /// CPython's max_fold_seconds: the farthest apart two times its clocks show alike can be.
    const MAX_FOLD_SECONDS: i64 = 24 * 3600;
    // What zone's clocks show at u, as CPython's local() gives it.
    let local = |u: i64| -> Result<i64, String> {
        // jiff holds no time after 9999-12-30T22:00Z, which CPython's local() reaches for a time near the end of 9999;
        // the offset there is taken as the one at that bound, which holds unless a zone's rule changes its clocks in
        // the last 26 hours of 9999, as no tzdb zone does.
        let at = Timestamp::from_second(u.clamp(Timestamp::MIN.as_second(), Timestamp::MAX.as_second()))
            .map_err(|e| e.to_string())?;
        let lt = u + i64::from(zone.to_offset(at).seconds());
        let (year, _, _) = civil_from_days(lt.div_euclid(86_400));
        if (1..=9999).contains(&year) {
            Ok(lt)
        } else {
            Err(format!("year must be in 1..9999, not {year}"))
        }
    };
    let t = shown;
    let a = local(t)? - t;
    let u1 = t - a;
    let t1 = local(u1)?;
    let b = if t1 == t {
        // One solution; an earlier one is the one fold 0 wants, if there is one.
        let u2 = u1 - MAX_FOLD_SECONDS;
        let b = local(u2)? - u2;
        if a == b {
            return Ok(u1);
        }
        b
    } else {
        t1 - u1
    };
    let u2 = t - b;
    if local(u2)? == t {
        return Ok(u2);
    }
    if t1 == t {
        return Ok(u1);
    }
    // Neither offset solves it: t is in a gap.
    Ok(u1.max(u2))
}

/// The time seconds since the epoch name, as `time.strftime(format, time.localtime(seconds))` writes it in the
/// machine's zone (the module's doc says how it is found), for a format of %Y, %m, %d, %H, %M, %S and other text and
/// a year from 1000 to 9999: 20261029-142233 for `%Y%m%d-%H%M%S`. Errors: seconds is beyond jiff's years -9999 to
/// 9999, or format holds a conversion jiff does not know, as jiff's message says.
pub fn strftime_local(format: &str, seconds: i64) -> Result<String, String> {
    strftime_in(format, seconds, &TimeZone::system())
}

/// strftime_local in zone.
fn strftime_in(format: &str, seconds: i64, zone: &TimeZone) -> Result<String, String> {
    let at = Timestamp::from_second(seconds).map_err(|e| e.to_string())?;
    jiff::fmt::strtime::format(format, zone.to_datetime(at)).map_err(|e| e.to_string())
}

/// `IsoTime::parse(iso)?.timestamp()`: the seconds since the epoch of the time iso names, as
/// `datetime.fromisoformat(iso).timestamp()` gives them. Errors: as IsoTime::parse's and IsoTime::timestamp's.
pub fn fromisoformat_timestamp(iso: &str) -> Result<f64, String> {
    IsoTime::parse(iso)?.timestamp()
}

/// The time iso names, as CPython's datetime_fromisoformat reads it (Modules/_datetimemodule.c, 3.14): microseconds
/// since 1970-01-01T00:00 of its date and time as they are written, and its offset from UTC in microseconds, None for a
/// time without one. Errors: as IsoTime::parse's.
fn fromisoformat(iso: &str) -> Result<(i128, Option<i128>), String> {
    let invalid = || format!("Invalid isoformat string: {}", repr(iso));
    if iso.chars().count() < 7 {
        return Err(invalid());
    }
    let s = Terminated(iso.as_bytes());
    let len = iso.len();
    let sep = separator_location(&s, len).ok_or_else(invalid)?;
    let (mut year, mut month, mut day) = date(&s, sep).ok_or_else(invalid)?;
    let ([mut hour, minute, second, micro], offset) = if len > sep {
        // The separator is any one character, its length read from its first byte as C reads it.
        let lead = s.at(sep);
        let width = match lead {
            _ if lead & 0x80 == 0 => 1,
            _ if lead & 0xf0 == 0xe0 => 3,
            _ if lead & 0xf0 == 0xf0 => 4,
            _ => 2,
        };
        if sep + width > len {
            return Err(invalid());
        }
        time(&s, sep + width, len).ok_or_else(invalid)?
    } else {
        ([0; 4], None)
    };
    if offset.is_some_and(|offset: i128| offset.abs() >= 86_400_000_000) {
        return Err("offset must be a timedelta strictly between -timedelta(hours=24) and timedelta(hours=24)."
            .to_string());
    }
    if hour == 24 && (1..=12).contains(&month) && day <= days_in_month(year, month) {
        if [minute, second, micro] != [0; 3] {
            return Err("minute, second, and microsecond must be 0 when hour is 24".to_string());
        }
        hour = 0;
        (year, month, day) = next_day(year, month, day);
    }
    if !(1..=9999).contains(&year) {
        return Err(format!("year {year} is out of range"));
    }
    if !(1..=12).contains(&month) {
        return Err("month must be in 1..12".to_string());
    }
    if day < 1 || day > days_in_month(year, month) {
        return Err("day is out of range for month".to_string());
    }
    for (name, value, max) in [("hour", hour, 23), ("minute", minute, 59), ("second", second, 59)] {
        if value > max {
            return Err(format!("{name} must be in 0..{max}, not {value}"));
        }
    }
    let seconds = days_from_civil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second;
    Ok((i128::from(seconds) * 1_000_000 + i128::from(micro), offset))
}

/// A string's bytes as C reads them: followed by a NUL, which every read past the end gives.
struct Terminated<'a>(&'a [u8]);

impl Terminated<'_> {
    /// The byte at i; 0 at the end and past it.
    fn at(&self, i: usize) -> u8 {
        self.0.get(i).copied().unwrap_or(0)
    }

    /// The number the n ASCII digits at i are; None when any of them is not one (C's parse_digits).
    fn digits(&self, i: usize, n: usize) -> Option<i64> {
        (i..i + n).try_fold(0, |n, k| self.at(k).is_ascii_digit().then(|| n * 10 + i64::from(self.at(k) - b'0')))
    }
}

/// Where the date of s, len bytes long, ends and the separator is, as _find_isoformat_datetime_separator finds it;
/// None where it finds no date (it returns -1).
fn separator_location(s: &Terminated, len: usize) -> Option<usize> {
    if len == 7 {
        return Some(7);
    }
    if s.at(4) == b'-' {
        if s.at(5) != b'W' {
            return Some(10);
        }
        // YYYY-Www, or YYYY-Www-D.
        if len < 8 {
            return None;
        }
        if len > 8 && s.at(8) == b'-' {
            if len == 9 {
                return None;
            }
            return Some(if len > 10 && s.at(10).is_ascii_digit() { 8 } else { 10 });
        }
        return Some(8);
    }
    if s.at(4) == b'W' {
        // YYYYWww or YYYYWwwD: as many digits as follow decide.
        let end = (7..len).find(|&i| !s.at(i).is_ascii_digit()).unwrap_or(len.max(7));
        if end < 9 {
            return Some(end);
        }
        return Some(if end % 2 == 0 { 7 } else { 8 });
    }
    // YYYYMMDD.
    Some(8)
}

/// The year, month and day of the date of s that ends at len, as parse_isoformat_date reads it, unchecked; None when
/// it is not of a date's form. A week date is turned into its day of the Gregorian calendar.
fn date(s: &Terminated, len: usize) -> Option<(i64, i64, i64)> {
    let year = s.digits(0, 4)?;
    let dashed = s.at(4) == b'-';
    let mut p = 4 + usize::from(dashed);
    let dash = |p: usize| !dashed || s.at(p) == b'-';
    if s.at(p) == b'W' {
        let week = s.digits(p + 1, 2)?;
        p += 3;
        let mut weekday = 1;
        if p < len {
            if !dash(p) {
                return None;
            }
            weekday = s.digits(p + usize::from(dashed), 1)?;
        }
        return iso_week_date(year, week, weekday);
    }
    let month = s.digits(p, 2)?;
    p += 2;
    if !dash(p) {
        return None;
    }
    let day = s.digits(p + usize::from(dashed), 2)?;
    Some((year, month, day))
}

/// The Gregorian date of day weekday (1 Monday) of ISO week week of year, as iso_to_ymd gives it; None for a year,
/// week or weekday out of range.
fn iso_week_date(year: i64, week: i64, weekday: i64) -> Option<(i64, i64, i64)> {
    if !(1..=9999).contains(&year) || !(1..=7).contains(&weekday) {
        return None;
    }
    // Days since the epoch of 1 January, and its weekday, 0 for Monday (1970-01-01 was a Thursday).
    let january_1 = days_from_civil(year, 1, 1);
    let first_weekday = (january_1 + 3).rem_euclid(7);
    let leap = days_in_month(year, 2) == 29;
    // A year has a 53rd week when it starts on a Thursday, or on a Wednesday in a leap year.
    if !(1..=52).contains(&week) && !(week == 53 && (first_weekday == 3 || (first_weekday == 2 && leap))) {
        return None;
    }
    // Week 1 is the week with the year's first Thursday; its Monday may be in the year before.
    let week_1_monday = january_1 - first_weekday + if first_weekday > 3 { 7 } else { 0 };
    Some(civil_from_days(week_1_monday + (week - 1) * 7 + (weekday - 1)))
}

/// The hour, minute, second and microsecond of the time of s from start to end, and its offset from UTC in
/// microseconds, as parse_isoformat_time reads them, unchecked; None when it is not of a time's form.
fn time(s: &Terminated, start: usize, end: usize) -> Option<([i64; 4], Option<i128>)> {
    // The offset starts at the first Z, + or -; C's do-while looks at start even when the time is empty.
    let mut tz = start;
    while !matches!(s.at(tz), b'Z' | b'+' | b'-') {
        tz += 1;
        if tz >= end {
            break;
        }
    }
    let (clock, more) = hh_mm_ss_ff(s, start, tz)?;
    if tz == end {
        return (!more).then_some((clock, None));
    }
    if s.at(tz) == b'Z' {
        return (s.at(tz + 1) == 0).then_some((clock, Some(0)));
    }
    let sign = if s.at(tz) == b'-' { -1 } else { 1 };
    let ([h, m, sec, us], more) = hh_mm_ss_ff(s, tz + 1, end)?;
    if more {
        return None;
    }
    Some((clock, Some(sign * (i128::from(h * 3600 + m * 60 + sec) * 1_000_000 + i128::from(us)))))
}

/// The hours, minutes, seconds and microseconds of HH[:?MM[:?SS[{.,}f+]]] at s from start to end, as
/// parse_hh_mm_ss_ff reads them, unchecked, and whether what follows them is not the end of s; None when they are
/// not of that form.
fn hh_mm_ss_ff(s: &Terminated, start: usize, end: usize) -> Option<([i64; 4], bool)> {
    let mut parts = [0; 4];
    let mut p = start;
    let mut colons = true;
    for i in 0..3 {
        parts[i] = s.digits(p, 2)?;
        let c = s.at(p + 2);
        p += 3;
        if i == 0 {
            colons = c == b':';
        }
        if c == b'.' || c == b',' {
            // A fraction of the seconds only, with a digit.
            if i < 2 || p >= end {
                return None;
            }
            break;
        } else if p >= end {
            return Some((parts, c != 0));
        } else if colons && c == b':' {
            if i == 2 {
                return None;
            }
        } else if !colons {
            p -= 1;
        } else {
            return None;
        }
    }
    // The fraction: up to six digits count, the rest are skipped.
    let used = (end - p).min(6);
    parts[3] = s.digits(p, used)? * 10_i64.pow(6 - used as u32);
    p += used;
    while s.at(p).is_ascii_digit() {
        p += 1;
    }
    Some((parts, s.at(p) != 0))
}

/// micros microseconds in seconds, as the f64 nearest to micros / 10^6, ties to even, as Python's true division of
/// two ints (timedelta.total_seconds) rounds it.
fn seconds_of_micros(micros: i128) -> f64 {
    let quotient = micros as f64 / 1e6;
    if micros.unsigned_abs() < 1 << 53 {
        // micros is a double as it is, and the division rounds once.
        return quotient;
    }
    // Else micros was rounded first; the nearest is quotient or a neighbour of it. Each is m * 2^e, e above -32 for
    // any time datetime can hold, so micros / 10^6 - m * 2^e, times 10^6 * 2^32, is an integer.
    let distance = |c: f64| -> i128 {
        let bits = c.abs().to_bits();
        let (exponent, fraction) = ((bits >> 52) as i32, bits & ((1 << 52) - 1));
        let (m, e) = (i128::from(fraction | 1 << 52), exponent - 1075);
        let m = if c < 0.0 { -m } else { m };
        (micros << 32) - m * 1_000_000 * (1_i128 << (e + 32))
    };
    let candidates = [quotient.next_down(), quotient, quotient.next_up()];
    let even = |c: &f64| c.to_bits() & 1 == 0;
    *candidates.iter().min_by_key(|c| (distance(**c).unsigned_abs(), !even(c))).expect("three candidates")
}

/// The date after year-month-day.
fn next_day(year: i64, month: i64, day: i64) -> (i64, i64, i64) {
    if day < days_in_month(year, month) {
        (year, month, day + 1)
    } else if month < 12 {
        (year, month + 1, 1)
    } else {
        (year + 1, 1, 1)
    }
}

/// How many days month has in year, of the proleptic Gregorian calendar.
fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// Days since 1970-01-01 of a date of the proleptic Gregorian calendar (Howard Hinnant's days_from_civil).
pub(crate) fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The date of a day since 1970-01-01 (Howard Hinnant's civil_from_days).
pub(crate) fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(month <= 2), month, day)
}

#[cfg(test)]
mod tests;
