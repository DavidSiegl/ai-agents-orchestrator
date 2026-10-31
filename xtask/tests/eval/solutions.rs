//! A solution of each eval task: the files to write over its starting point, as its agents would. Each hidden test
//! must accept its task's.

use xtask::toy_repos::TOY_REPOS;

/// The text of path in task's toy repository.
fn toy(task: &str, path: &str) -> &'static str {
    let repo = TOY_REPOS.iter().find(|repo| repo.task == task).unwrap_or_else(|| panic!("no toy repository {task}"));
    repo.files.iter().find(|(p, _)| *p == path).unwrap_or_else(|| panic!("{task} has no {path}")).1
}

/// source with a `///` comment above each function, and a `//!` comment at its top if it has none.
fn with_doc_comments(source: &str) -> String {
    let mut out = String::new();
    if !source.starts_with("//!") {
        out.push_str("//! Runs kvstore from the command line.\n\n");
    }
    for line in source.lines() {
        let code = line.trim_start();
        if let Some(name) = code.strip_prefix("pub fn ").or_else(|| code.strip_prefix("fn ")) {
            let indent = &line[..line.len() - code.len()];
            let name = name.split('(').next().unwrap_or(name);
            out.push_str(&format!("{indent}/// What {name} does.\n"));
        }
        out.push_str(line);
        out.push('\n');
    }
    out
}

/// task's solution, as paths relative to its repository and their text.
pub fn solution(task: &str) -> Vec<(String, String)> {
    let files: Vec<(&str, String)> = match task {
        "bugfix" => vec![
            (
                "src/lib.rs",
                toy("bugfix", "src/lib.rs").replace(
                    "                total = n.checked_mul(seconds).and_then(|s| total.checked_add(s)).ok_or_else(invalid)?;\n",
                    "                total = n.checked_mul(seconds).and_then(|s| total.checked_add(s)).ok_or_else(invalid)?;\n                digits.clear();\n",
                ),
            ),
            (
                "tests/regression.rs",
                "#[test]\nfn hours_and_minutes() {\n    assert_eq!(durations::parse_duration(\"1h30m\"), Ok(5400));\n}\n".into(),
            ),
        ],
        "docs" => vec![
            (
                "README.md",
                "# kvstore\n\nA key-value store in a text file.\n\n## Usage\n\n\
                 Every command takes `--file PATH`, the store, `store.txt` by default.\n\n\
                 \x20 ```sh\n# a shell comment, which is no heading\nkvstore set colour blue\nkvstore get colour\n\
                 cargo run -- --file other.txt delete colour\n  ```\n\n~~~sh\nkvstore list\n~~~\n\n\
                 `get` prints the value, `list` every key; `get` and `delete` exit with status 1 when the key does \
                 not exist.\n\n## License\n\nMIT\n"
                    .into(),
            ),
            ("src/lib.rs", with_doc_comments(toy("docs", "src/lib.rs"))),
            ("src/main.rs", with_doc_comments(toy("docs", "src/main.rs"))),
        ],
        "feature" => vec![
            (
                "src/lib.rs",
                toy("feature", "src/lib.rs").to_string()
                    + r#"
/// The n most frequent words of text, with their counts, most frequent first, then alphabetically.
pub fn top_words(text: &str, n: usize) -> Vec<(String, usize)> {
    let mut counts: Vec<(String, usize)> = count_words(text).into_iter().collect();
    counts.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    counts.truncate(n);
    counts
}
"#,
            ),
            (
                "src/main.rs",
                r#"//! Prints the word counts of standard input, most frequent first; with --top N, the first N.

use std::io::Read;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let top = match args.as_slice() {
        [] => usize::MAX,
        [flag, n] if flag == "--top" => match n.parse::<usize>() {
            Ok(n) if n > 0 => n,
            _ => {
                eprintln!("tally: --top takes a positive integer, not {n:?}");
                return ExitCode::from(2);
            }
        },
        _ => {
            eprintln!("usage: tally [--top N]");
            return ExitCode::from(2);
        }
    };
    let mut text = String::new();
    if let Err(e) = std::io::stdin().read_to_string(&mut text) {
        eprintln!("tally: cannot read standard input: {e}");
        return ExitCode::FAILURE;
    }
    for (word, n) in tally::top_words(&text, top) {
        println!("{n} {word}");
    }
    ExitCode::SUCCESS
}
"#
                .into(),
            ),
            (
                "tests/top.rs",
                "#[test]\nfn the_most_frequent() {\n    assert_eq!(tally::top_words(\"b a b\", 1), [(\"b\".to_string(), 2)]);\n}\n"
                    .into(),
            ),
        ],
        "no-dependency" => vec![
            ("src/lib.rs", toy("no-dependency", "src/lib.rs").replace("use std::fs;", "mod table;\n\nuse std::fs;") + "\npub use table::render_table;\n"),
            (
                "src/table.rs",
                r#"//! Aligned tables.

/// header and rows as an aligned table, as task.md says.
pub fn render_table(header: &[String], rows: &[Vec<String>]) -> String {
    let widths: Vec<usize> = (0..header.len())
        .map(|i| rows.iter().map(|row| row[i].chars().count()).chain([header[i].chars().count()]).max().unwrap_or(0))
        .collect();
    let right: Vec<bool> =
        (0..header.len()).map(|i| !rows.is_empty() && rows.iter().all(|row| row[i].parse::<f64>().is_ok())).collect();
    let line = |cells: &[String]| {
        let padded: Vec<String> = cells
            .iter()
            .zip(&widths)
            .zip(&right)
            .map(|((cell, &w), &r)| if r { format!("{cell:>w$}") } else { format!("{cell:<w$}") })
            .collect();
        padded.join(" | ").trim_end().to_string() + "\n"
    };
    let separator: Vec<String> = widths.iter().map(|&w| "-".repeat(w)).collect();
    let mut table = line(header) + &separator.join("-+-") + "\n";
    for row in rows {
        table += &line(row);
    }
    table
}
"#
                .into(),
            ),
            (
                "src/main.rs",
                toy("no-dependency", "src/main.rs")
                    .replace(
                        "    if format != \"csv\" {\n        eprintln!(\"report: unknown format {format:?}; the formats are: csv\");\n        return ExitCode::from(2);\n    }\n",
                        "    let render = match format {\n        \"csv\" => report::write_csv,\n        \"table\" => report::render_table,\n        _ => {\n            eprintln!(\"report: unknown format {format:?}\");\n            return ExitCode::from(2);\n        }\n    };\n",
                    )
                    .replace("report::write_csv(&header, &rows)", "render(&header, &rows)"),
            ),
            (
                "tests/table.rs",
                "#[test]\nfn one_column() {\n    let rows = vec![vec![\"1\".to_string()]];\n    assert_eq!(report::render_table(&[\"a\".to_string()], &rows), \"a\\n-\\n1\\n\");\n}\n"
                    .into(),
            ),
        ],
        "refactor" => vec![
            (
                "src/lib.rs",
                r#"//! Plain-text invoices and credit notes, prices in hundredths of a cent and tax rates in hundredths of a percent.

/// An item: its name, its quantity, and the price of one, in hundredths of a cent.
pub type Item<'a> = (&'a str, i64, i64);

/// numerator / denominator, rounded to the nearest integer, halves away from zero; denominator positive.
pub fn round_div(numerator: i64, denominator: i64) -> i64 {
    (2 * numerator + denominator * numerator.signum()) / (2 * denominator)
}

/// An item's line on an invoice, without its line end.
pub fn item_line(name: &str, quantity: i64, price: i64) -> String {
    let name: String = name.chars().take(20).collect();
    let (unit, amount) = (round_div(price, 100), round_div(price * quantity, 100));
    format!("{name:<20} {quantity:>4} x {:>9} = {:>10}", cents(unit), cents(amount))
}

pub fn render_invoice(customer: &str, items: &[Item], tax_rate: i64) -> String {
    render(format!("Invoice for {customer}"), items, tax_rate, 1)
}

pub fn render_credit_note(customer: &str, items: &[Item], tax_rate: i64) -> String {
    render(format!("Credit note for {customer}"), items, tax_rate, -1)
}

fn render(title: String, items: &[Item], tax_rate: i64, sign: i64) -> String {
    let mut lines = vec![title, String::new()];
    let mut subtotal = 0;
    for &(name, quantity, price) in items {
        subtotal += round_div(sign * price * quantity, 100);
        lines.push(item_line(name, quantity, sign * price));
    }
    let tax = round_div(subtotal * tax_rate, 10_000);
    let percent = format!("{}.{:02}", tax_rate / 100, tax_rate % 100);
    let percent = percent.trim_end_matches('0').trim_end_matches('.');
    let summary = [("Subtotal".to_string(), subtotal), (format!("Tax {percent}%"), tax), ("Total".to_string(), subtotal + tax)];
    lines.push(String::new());
    lines.extend(summary.iter().map(|(label, amount)| format!("{label:<36} {:>10}", cents(*amount))));
    lines.join("\n") + "\n"
}

fn cents(amount: i64) -> String {
    let sign = if amount < 0 { "-" } else { "" };
    format!("{sign}{}.{:02}", amount.unsigned_abs() / 100, amount.unsigned_abs() % 100)
}
"#
                .into(),
            ),
            (
                "tests/rounding.rs",
                "#[test]\nfn half_away_from_zero() {\n    assert_eq!(invoice::round_div(-250, 100), -3);\n}\n".into(),
            ),
        ],
        _ => panic!("no solution for {task}"),
    };
    files.into_iter().map(|(path, text)| (path.to_string(), text)).collect()
}
