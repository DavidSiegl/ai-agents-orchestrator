# 03 — Replace `crate::py::text` and the Unicode tables with std

Status: ready-for-agent
Blocked by: 02

## What

`engine/src/py/text.rs` (314 lines) ports `str.splitlines`, `strip`, `os.path.splitext`, `repr`,
`difflib.get_close_matches`, universal newlines and Python's character classes, backed by `unicode_tables.rs` (1,605
generated lines) from `engine/tools/unicode_tables.py`. About 19 modules use it
(`grep -rn 'py::text' engine/src`).

- **Replace each use** with the plain Rust equivalent: `str::lines`, `trim`, `Path::extension`, `char::is_whitespace`
  and `is_alphanumeric`, and `{:?}` where a human reads the output.
- **`get_close_matches`** (the "did you mean" of unknown roles and steps) becomes a small edit-distance helper in the
  module that needs it. Use Levenshtein, under a cut-off documented there.
- **`repr`** in user-facing messages becomes plain quoting with `"..."`. Re-record the e2e scenarios whose facts
  change, and review the diff.
- **Delete** `py/text.rs`, `py/unicode_tables.rs`, `engine/tools/unicode_tables.py`, and the `text` cases of
  `engine/tests/oracle/python.json`.
- **`TODO.md:176`–`210`** lists where the Rust engine reads input "otherwise than the Python engine". Drop the clauses
  this ticket makes moot (`\d`, `isspace`). Record the new behaviour where a reader would look: the doc comments of
  the functions.

## Verification

- `cargo nextest run --workspace` passes.
- The re-recorded scenarios' diff is reviewed in the pull request, each kind of change named in its description.
- `cargo clippy --workspace --all-targets` is clean.
- `git ls-files engine/tools` is empty.
