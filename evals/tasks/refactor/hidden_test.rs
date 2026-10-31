//! The hidden acceptance test of the eval task `refactor`: `round_div` and `item_line` in src/lib.rs, its output
//! unchanged (task.md). `cargo xtask eval` copies it into the clone's tests/ after the run and runs it with the crate's
//! own tests (xtask/src/eval.rs).

use std::path::Path;
use std::process::Command;

use invoice::{item_line, render_credit_note, render_invoice, round_div};

/// src/lib.rs as the task found it, which the renderers' output is compared with. xtask's tests check that it is the
/// toy repository's (xtask/src/toy_repos.rs, REFACTOR_LIB).
#[allow(dead_code)]
mod original {
    //! Plain-text invoices and credit notes.
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
}

const ITEMS: [&[(&str, i64, i64)]; 4] = [
    &[],
    &[("Widget", 3, 25_000), ("Gadget", 1, 100_000)],
    &[("A name longer than twenty characters", 12, 1_250), ("Half cent", 3, 3_350)],
    &[("Bulk", 1000, 12_345_678), ("Free", 2, 0)],
];
const RATES: [i64; 5] = [0, 2_000, 750, 1_900, 10_000];

/// The text of the file at path, relative to the crate, as the task found it: origin/main's, which a run without a
/// pull request never moves.
fn original_file(path: &str) -> String {
    let output = Command::new("git")
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .args(["show", &format!("origin/main:{path}")])
        .output()
        .expect("git runs");
    assert!(output.status.success(), "git show: {}", String::from_utf8_lossy(&output.stderr));
    String::from_utf8(output.stdout).expect("the file is UTF-8")
}

#[test]
fn output_unchanged() {
    for items in ITEMS {
        for rate in RATES {
            assert_eq!(render_invoice("ACME", items, rate), original::render_invoice("ACME", items, rate));
            assert_eq!(render_credit_note("ACME", items, rate), original::render_credit_note("ACME", items, rate));
        }
    }
}

#[test]
fn round_div_rounds_halves_away_from_zero() {
    let cases = [(250, 100, 3), (-250, 100, -3), (249, 100, 2), (-5, 10, -1), (0, 7, 0), (2_675, 1_000, 3)];
    for (numerator, denominator, rounded) in cases {
        assert_eq!(round_div(numerator, denominator), rounded, "{numerator} / {denominator}");
    }
}

#[test]
fn item_line_is_an_items_line() {
    assert_eq!(item_line("Widget", 3, 25_000), "Widget                  3 x      2.50 =       7.50");
    assert_eq!(item_line("Widget", 3, -25_000), "Widget                  3 x     -2.50 =      -7.50");
}

#[test]
fn rounding_has_one_home() {
    let lib =
        std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lib.rs")).expect("src/lib.rs reads");
    let calls: usize = lib.lines().map(|line| line.split("//").next().unwrap_or("").matches("signum").count()).sum();
    assert_eq!(calls, 1, "src/lib.rs should call signum once, in round_div");
}

#[test]
fn the_existing_tests_unchanged() {
    let now = std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/render.rs"));
    assert_eq!(now.expect("tests/render.rs reads"), original_file("tests/render.rs"));
}
