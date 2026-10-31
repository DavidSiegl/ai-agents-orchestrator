# One home for the invoice's rounding and formatting

## Goal

`render_invoice` and `render_credit_note` in `src/lib.rs` repeat each other: the rounding to the cent, the item line,
the tax percentage and the summary lines are written out twice, and the rounding six times. Refactor so that each is
written once. The output must not change.

## Requirements

- One function in `src/lib.rs`, `pub fn round_div(numerator: i64, denominator: i64) -> i64`, divides, rounding to
  the nearest integer and halves away from zero, for a positive `denominator`; every rounding in `src/lib.rs` goes
  through it, and `src/lib.rs` calls `signum` nowhere else.
- One function, `pub fn item_line(name: &str, quantity: i64, price: i64) -> String`, returns an item's line as both
  renderers print it, without its line ending, for the price as given in hundredths of a cent (the credit note passes
  its negated price).
- `render_invoice` and `render_credit_note` keep their names, arguments and output, byte for byte, for every input
  they take today, an empty list of items included.
- The existing tests in `tests/render.rs` pass unchanged; add tests for `round_div` and `item_line`. Run them with
  `cargo test`.

## Out of scope

Any change to the output's format, or to the crate's other names.
