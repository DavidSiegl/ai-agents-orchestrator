//! The eval tasks' toy repositories, as text: each task's starting point, which `cargo xtask bundles` commits into its
//! `repo.bundle` (crate::bundles).
//!
//! They live here as strings, not as files under evals/, so that neither Cargo, which would find a package inside
//! the workspace that is not a member of it, nor the quality gate, which would judge their code and their deliberate
//! bug as this repository's own, takes them for the repository's. A change to one is reviewed here, and its bundle
//! rebuilt with `cargo xtask bundles`.
//!
//! Each is a library crate with no dependency, tested by `cargo test` from its `tests/`. Prices, durations and counts
//! are integers, so that no task turns on a float's rounding.

/// A toy repository's Cargo.toml: a package of the name and description given, without dependencies.
macro_rules! cargo_toml {
    ($name:literal, $description:literal) => {
        concat!(
            "[package]\nname = \"",
            $name,
            "\"\nversion = \"0.1.0\"\ndescription = \"",
            $description,
            "\"\nedition = \"2021\"\npublish = false\n\n[dependencies]\n"
        )
    };
}

/// Every toy repository's .gitignore: Cargo's build directory.
const GITIGNORE: &str = "/target\n";

/// A toy repository: the name of its task (its directory under evals/tasks/), and its files' paths and text.
pub struct ToyRepo {
    pub task: &'static str,
    pub files: &'static [(&'static str, &'static str)],
}

/// Every task's toy repository, in task name order.
pub const TOY_REPOS: &[ToyRepo] = &[
    // A bug with a reproducer: parse_duration does not reset its number after a unit.
    ToyRepo {
        task: "bugfix",
        files: &[
            (".gitignore", GITIGNORE),
            ("Cargo.toml", cargo_toml!("durations", "Durations written as 1h30m")),
            (
                "README.md",
                "# durations\n\nDurations written as `1h30m`, `45s` or `2h5m10s`: `parse_duration` reads one, \
                 `format_duration` writes one.\n\nRun the tests with `cargo test`.\n",
            ),
            ("src/lib.rs", BUGFIX_LIB),
            ("tests/duration.rs", BUGFIX_TESTS),
        ],
    },
    // A docs change: a README without usage, and functions without doc comments; the code must not change.
    ToyRepo {
        task: "docs",
        files: &[
            (".gitignore", "/target\nstore.txt\n"),
            ("Cargo.toml", cargo_toml!("kvstore", "A key-value store in a text file")),
            ("README.md", "# kvstore\n\nA key-value store in a text file.\n"),
            ("src/lib.rs", DOCS_LIB),
            ("src/main.rs", DOCS_MAIN),
            ("tests/store.rs", DOCS_TESTS),
        ],
    },
    // A feature: top_words and `--top N`, on a word counter.
    ToyRepo {
        task: "feature",
        files: &[
            (".gitignore", GITIGNORE),
            ("Cargo.toml", cargo_toml!("tally", "Word counts of a text")),
            (
                "README.md",
                "# tally\n\nWord counts of a text.\n\n```sh\ncargo run < book.txt\n```\n\n\
                 Prints a line per word, its count and the word, most frequent first.\n\n\
                 Run the tests with `cargo test`.\n",
            ),
            ("src/lib.rs", FEATURE_LIB),
            ("src/main.rs", FEATURE_MAIN),
            ("tests/counter.rs", FEATURE_TESTS),
        ],
    },
    // A task whose spec forbids a dependency: aligned tables, which a crate such as comfy-table would make.
    ToyRepo {
        task: "no-dependency",
        files: &[
            (".gitignore", GITIGNORE),
            ("Cargo.toml", cargo_toml!("report", "CSV files, read and written again")),
            (
                "README.md",
                "# report\n\nReads a CSV file and writes it again, normalized:\n\n\
                 ```sh\ncargo run -- data.csv\n```\n\nRun the tests with `cargo test`.\n",
            ),
            ("src/lib.rs", NO_DEPENDENCY_LIB),
            ("src/main.rs", NO_DEPENDENCY_MAIN),
            ("tests/csv.rs", NO_DEPENDENCY_TESTS),
        ],
    },
    // A refactor that must keep behaviour: two renderers that repeat each other's formatting and rounding.
    ToyRepo {
        task: "refactor",
        files: &[
            (".gitignore", GITIGNORE),
            ("Cargo.toml", cargo_toml!("invoice", "Plain-text invoices and credit notes")),
            (
                "README.md",
                "# invoice\n\nPlain-text invoices and credit notes: `render_invoice` and `render_credit_note`.\n\n\
                 Run the tests with `cargo test`.\n",
            ),
            ("src/lib.rs", REFACTOR_LIB),
            ("tests/render.rs", REFACTOR_TESTS),
        ],
    },
];

const BUGFIX_LIB: &str = r##"//! Durations written as `1h30m`, `45s` or `2h5m10s`: [`parse_duration`] reads one,
//! [`format_duration`] writes one.

use std::fmt;

/// The units a duration is written in, largest first, each with its length in seconds.
const UNITS: [(char, u64); 3] = [('h', 3600), ('m', 60), ('s', 1)];

/// The text [`parse_duration`] could not read as a duration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotADuration(pub String);

impl fmt::Display for NotADuration {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "not a duration: {:?}", self.0)
    }
}

impl std::error::Error for NotADuration {}

/// The seconds a duration such as `1h30m` stands for: one or more numbers, each followed by a unit, `h`, `m` or `s`.
/// Surrounding whitespace is ignored. Anything else, the empty string included, is [`NotADuration`].
pub fn parse_duration(text: &str) -> Result<u64, NotADuration> {
    let invalid = || NotADuration(text.to_string());
    let trimmed = text.trim();
    if !trimmed.ends_with(|c| unit_seconds(c).is_some()) {
        return Err(invalid());
    }
    let mut total: u64 = 0;
    let mut digits = String::new();
    for c in trimmed.chars() {
        if c.is_ascii_digit() {
            digits.push(c);
            continue;
        }
        match unit_seconds(c) {
            Some(seconds) if !digits.is_empty() => {
                let n: u64 = digits.parse().map_err(|_| invalid())?;
                total = n.checked_mul(seconds).and_then(|s| total.checked_add(s)).ok_or_else(invalid)?;
            }
            _ => return Err(invalid()),
        }
    }
    Ok(total)
}

/// `seconds` written as [`parse_duration`] reads it: hours, minutes and seconds, each only when not 0, and `0s` for 0.
pub fn format_duration(seconds: u64) -> String {
    let mut left = seconds;
    let mut text = String::new();
    for (unit, size) in UNITS {
        let n = left / size;
        left %= size;
        if n > 0 {
            text.push_str(&format!("{n}{unit}"));
        }
    }
    if text.is_empty() {
        "0s".to_string()
    } else {
        text
    }
}

/// The seconds in one of `unit`, or None when it is not one of [`UNITS`].
fn unit_seconds(unit: char) -> Option<u64> {
    UNITS.iter().find(|&&(u, _)| u == unit).map(|&(_, seconds)| seconds)
}
"##;

const BUGFIX_TESTS: &str = r##"use durations::{format_duration, parse_duration};

#[test]
fn one_unit() {
    assert_eq!(parse_duration("45s"), Ok(45));
    assert_eq!(parse_duration("2m"), Ok(120));
    assert_eq!(parse_duration(" 3h "), Ok(10800));
}

#[test]
fn not_a_duration() {
    for text in ["", "5", "h", "1x", "1h-"] {
        assert!(parse_duration(text).is_err(), "{text:?} was read as a duration");
    }
}

#[test]
fn format() {
    assert_eq!(format_duration(0), "0s");
    assert_eq!(format_duration(5400), "1h30m");
    assert_eq!(format_duration(3605), "1h5s");
}
"##;

const DOCS_LIB: &str = r##"//! A key-value store kept in a text file: a line per key, the key, a tab and the value,
//! in key order.

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, ErrorKind};
use std::path::Path;

fn load(path: &Path) -> io::Result<BTreeMap<String, String>> {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Err(e) => return Err(e),
    };
    Ok(text
        .lines()
        .filter_map(|line| line.split_once('\t'))
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect())
}

fn save(path: &Path, entries: &BTreeMap<String, String>) -> io::Result<()> {
    let text: String = entries.iter().map(|(key, value)| format!("{key}\t{value}\n")).collect();
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, text)?;
    fs::rename(&tmp, path)
}

pub fn get(path: &Path, key: &str) -> io::Result<Option<String>> {
    Ok(load(path)?.remove(key))
}

pub fn put(path: &Path, key: &str, value: &str) -> io::Result<()> {
    let mut entries = load(path)?;
    entries.insert(key.to_string(), value.to_string());
    save(path, &entries)
}

pub fn delete(path: &Path, key: &str) -> io::Result<bool> {
    let mut entries = load(path)?;
    if entries.remove(key).is_none() {
        return Ok(false);
    }
    save(path, &entries)?;
    Ok(true)
}

pub fn keys(path: &Path) -> io::Result<Vec<String>> {
    Ok(load(path)?.into_keys().collect())
}
"##;

const DOCS_MAIN: &str = r##"use std::path::{Path, PathBuf};
use std::process::ExitCode;

fn usage() -> ExitCode {
    eprintln!("usage: kvstore [--file PATH] (get KEY | set KEY VALUE | delete KEY | list)");
    ExitCode::from(2)
}

fn run(file: &Path, command: &[String]) -> std::io::Result<ExitCode> {
    match command {
        [name, key] if name == "get" => match kvstore::get(file, key)? {
            Some(value) => println!("{value}"),
            None => {
                eprintln!("no key {key}");
                return Ok(ExitCode::from(1));
            }
        },
        [name, key, value] if name == "set" => kvstore::put(file, key, value)?,
        [name, key] if name == "delete" => {
            if !kvstore::delete(file, key)? {
                eprintln!("no key {key}");
                return Ok(ExitCode::from(1));
            }
        }
        [name] if name == "list" => {
            for key in kvstore::keys(file)? {
                println!("{key}");
            }
        }
        _ => return Ok(usage()),
    }
    Ok(ExitCode::SUCCESS)
}

fn main() -> ExitCode {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut file = PathBuf::from("store.txt");
    if args.first().is_some_and(|arg| arg == "--file") {
        if args.len() < 2 {
            return usage();
        }
        file = PathBuf::from(args.remove(1));
        args.remove(0);
    }
    match run(&file, &args) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("kvstore: {}: {e}", file.display());
            ExitCode::FAILURE
        }
    }
}
"##;

const DOCS_TESTS: &str = r##"use std::fs;
use std::path::PathBuf;
use std::process::Command;

use kvstore::{delete, get, keys, put};

fn store(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("kvstore-test-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir.join("store.txt")
}

#[test]
fn put_get_delete() {
    let path = store("put");
    put(&path, "a", "1").unwrap();
    assert_eq!(get(&path, "a").unwrap(), Some("1".to_string()));
    assert_eq!(keys(&path).unwrap(), ["a"]);
    assert!(delete(&path, "a").unwrap());
    assert!(!delete(&path, "a").unwrap());
    assert_eq!(get(&path, "a").unwrap(), None);
}

#[test]
fn get_of_a_missing_key_exits_1() {
    let path = store("missing");
    let status = Command::new(env!("CARGO_BIN_EXE_kvstore")).arg("--file").arg(&path).args(["get", "nope"]).status();
    assert_eq!(status.unwrap().code(), Some(1));
}
"##;

const FEATURE_LIB: &str = r##"//! Word counts of a text.

use std::collections::HashMap;

/// The words of `text`, lowercased, in order: runs of the letters a to z and apostrophes.
pub fn words(text: &str) -> Vec<String> {
    text.to_lowercase()
        .split(|c: char| !(c.is_ascii_lowercase() || c == '\''))
        .filter(|word| !word.is_empty())
        .map(str::to_string)
        .collect()
}

/// How often each word of `text` occurs, by word.
pub fn count_words(text: &str) -> HashMap<String, usize> {
    let mut counts = HashMap::new();
    for word in words(text) {
        *counts.entry(word).or_insert(0) += 1;
    }
    counts
}
"##;

const FEATURE_MAIN: &str = r##"//! Prints the word counts of standard input, a line each: the count, a space and the word,
//! most frequent first, and words as frequent in alphabetical order.

use std::io::Read;
use std::process::ExitCode;

fn main() -> ExitCode {
    let mut text = String::new();
    if let Err(e) = std::io::stdin().read_to_string(&mut text) {
        eprintln!("tally: cannot read standard input: {e}");
        return ExitCode::FAILURE;
    }
    let mut counts: Vec<(String, usize)> = tally::count_words(&text).into_iter().collect();
    counts.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    for (word, n) in counts {
        println!("{n} {word}");
    }
    ExitCode::SUCCESS
}
"##;

const FEATURE_TESTS: &str = r##"use std::collections::HashMap;

use tally::{count_words, words};

#[test]
fn words_are_lowercased() {
    assert_eq!(words("The cat, the HAT."), ["the", "cat", "the", "hat"]);
}

#[test]
fn count_words_counts_each_word() {
    assert_eq!(count_words("a b a"), HashMap::from([("a".to_string(), 2), ("b".to_string(), 1)]));
}

#[test]
fn no_words() {
    assert!(count_words(" ,. ").is_empty());
}
"##;

const NO_DEPENDENCY_LIB: &str = r##"//! CSV files, read and written again.

use std::fs;
use std::io;
use std::path::Path;

/// The header and the rows of the CSV file at `path`: its first line's cells, then each other line's. Cells are
/// separated by commas, lines end with `\n` or `\r\n`, and no cell is quoted.
pub fn read_rows(path: &Path) -> io::Result<(Vec<String>, Vec<Vec<String>>)> {
    let text = fs::read_to_string(path)?;
    let mut lines = text.lines().map(|line| line.split(',').map(str::to_string).collect::<Vec<_>>());
    let header = lines.next().unwrap_or_default();
    Ok((header, lines.collect()))
}

/// `header` and `rows` as CSV text, a line each, ending with `\n`.
pub fn write_csv(header: &[String], rows: &[Vec<String>]) -> String {
    std::iter::once(header).chain(rows.iter().map(Vec::as_slice)).map(|cells| cells.join(",") + "\n").collect()
}
"##;

const NO_DEPENDENCY_MAIN: &str = r##"//! Writes a CSV file to standard output again, in the format `--format` names:
//! `report [--format csv] FILE`.

use std::path::Path;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (format, file) = match args.as_slice() {
        [file] => ("csv", file),
        [flag, format, file] if flag == "--format" => (format.as_str(), file),
        _ => {
            eprintln!("usage: report [--format csv] FILE");
            return ExitCode::from(2);
        }
    };
    if format != "csv" {
        eprintln!("report: unknown format {format:?}; the formats are: csv");
        return ExitCode::from(2);
    }
    match report::read_rows(Path::new(file)) {
        Ok((header, rows)) => {
            print!("{}", report::write_csv(&header, &rows));
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("report: {file}: {e}");
            ExitCode::FAILURE
        }
    }
}
"##;

const NO_DEPENDENCY_TESTS: &str = r##"use std::fs;

use report::{read_rows, write_csv};

#[test]
fn round_trip() {
    let dir = std::env::temp_dir().join(format!("report-test-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join("data.csv");
    fs::write(&path, "name,qty\r\napple,3\r\n").unwrap();
    let (header, rows) = read_rows(&path).unwrap();
    fs::remove_dir_all(&dir).unwrap();
    assert_eq!(header, ["name", "qty"]);
    assert_eq!(rows, [["apple", "3"]]);
    assert_eq!(write_csv(&header, &rows), "name,qty\napple,3\n");
}
"##;

/// The refactor task's starting src/lib.rs. Its hidden test holds a copy, as its module `original`, to compare the
/// renderers' output with (xtask's tests/bundles.rs checks that the copy is this).
pub const REFACTOR_LIB: &str = r##"//! Plain-text invoices and credit notes.
//!
//! Prices are in hundredths of a cent (`25_000` is 2.50), the amounts on an invoice in cents, and tax rates in
//! hundredths of a percent (`2_000` is 20%).

/// An item: its name, its quantity, and the price of one, in hundredths of a cent.
pub type Item<'a> = (&'a str, i64, i64);

/// The invoice for `customer`, as text: a line per item with its name, quantity, unit price and amount, then the
/// subtotal, the tax at `tax_rate` and the total. Amounts are rounded to the cent, half away from zero, a line's
/// before it is added to the subtotal.
pub fn render_invoice(customer: &str, items: &[Item], tax_rate: i64) -> String {
    let mut lines = vec![format!("Invoice for {customer}"), String::new()];
    let mut subtotal = 0;
    for &(name, quantity, price) in items {
        let exact = price * quantity;
        let amount = (2 * exact + 100 * exact.signum()) / 200;
        subtotal += amount;
        let unit = (2 * price + 100 * price.signum()) / 200;
        let name: String = name.chars().take(20).collect();
        lines.push(format!("{name:<20} {quantity:>4} x {:>9} = {:>10}", cents(unit), cents(amount)));
    }
    let exact = subtotal * tax_rate;
    let tax = (2 * exact + 10_000 * exact.signum()) / 20_000;
    let percent = format!("{}.{:02}", tax_rate / 100, tax_rate % 100);
    let percent = percent.trim_end_matches('0').trim_end_matches('.');
    lines.push(String::new());
    lines.push(format!("{:<36} {:>10}", "Subtotal", cents(subtotal)));
    lines.push(format!("{:<36} {:>10}", format!("Tax {percent}%"), cents(tax)));
    lines.push(format!("{:<36} {:>10}", "Total", cents(subtotal + tax)));
    lines.join("\n") + "\n"
}

/// The credit note for `customer`, as text: as [`render_invoice`]'s invoice for the same items, headed "Credit note
/// for", with every amount, the unit prices included, negative.
pub fn render_credit_note(customer: &str, items: &[Item], tax_rate: i64) -> String {
    let mut lines = vec![format!("Credit note for {customer}"), String::new()];
    let mut subtotal = 0;
    for &(name, quantity, price) in items {
        let exact = price * quantity;
        let amount = -((2 * exact + 100 * exact.signum()) / 200);
        subtotal += amount;
        let unit = -((2 * price + 100 * price.signum()) / 200);
        let name: String = name.chars().take(20).collect();
        lines.push(format!("{name:<20} {quantity:>4} x {:>9} = {:>10}", cents(unit), cents(amount)));
    }
    let exact = subtotal * tax_rate;
    let tax = (2 * exact + 10_000 * exact.signum()) / 20_000;
    let percent = format!("{}.{:02}", tax_rate / 100, tax_rate % 100);
    let percent = percent.trim_end_matches('0').trim_end_matches('.');
    lines.push(String::new());
    lines.push(format!("{:<36} {:>10}", "Subtotal", cents(subtotal)));
    lines.push(format!("{:<36} {:>10}", format!("Tax {percent}%"), cents(tax)));
    lines.push(format!("{:<36} {:>10}", "Total", cents(subtotal + tax)));
    lines.join("\n") + "\n"
}

/// `amount` cents written in units, with two decimals, such as `-7.50`.
fn cents(amount: i64) -> String {
    let sign = if amount < 0 { "-" } else { "" };
    format!("{sign}{}.{:02}", amount.unsigned_abs() / 100, amount.unsigned_abs() % 100)
}
"##;

const REFACTOR_TESTS: &str = r##"use invoice::{render_credit_note, render_invoice};

const ITEMS: [(&str, i64, i64); 2] = [("Widget", 3, 25_000), ("Gadget", 1, 100_000)];

#[test]
fn invoice_total() {
    let text = render_invoice("ACME", &ITEMS, 2_000);
    assert!(text.starts_with("Invoice for ACME\n"), "{text}");
    assert!(text.contains("Tax 20%"), "{text}");
    assert!(text.ends_with("     21.00\n"), "{text}");
}

#[test]
fn credit_note_is_negative() {
    let text = render_credit_note("ACME", &ITEMS, 2_000);
    assert!(text.starts_with("Credit note for ACME\n"), "{text}");
    assert!(text.ends_with("    -21.00\n"), "{text}");
}
"##;
