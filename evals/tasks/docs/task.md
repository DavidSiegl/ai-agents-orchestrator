# Document kvstore: its usage and its functions

## Goal

`kvstore`'s README says what it is but not how to use it, and the functions in `src/lib.rs` and `src/main.rs` have no
doc comments. Document both. This is a documentation change: the code must not change.

## Requirements

- A `## Usage` section in `README.md` that documents each command of `kvstore`: `get`, `set`, `delete` and `list`,
  each with an example command line in a fenced code block (as `kvstore ...` or `cargo run -- ...`), and the `--file`
  option and its default.
- It says what each command prints, and that `get` and `delete` exit with status 1 when the key does not exist.
- A `///` doc comment on every function in `src/lib.rs` and `src/main.rs`, the private ones included, and a `//!`
  comment on `src/main.rs`, saying what it does, its arguments and its return value.
- No change to any line of code: only comments are added, and the tests still pass with `cargo test`.
