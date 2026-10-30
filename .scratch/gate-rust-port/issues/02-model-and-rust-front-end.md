# 02 — The model, the checks over it, and the Rust front end on tree-sitter

Status: ready-for-agent
Blocked by: 01

## What

- **`crates/model`.** The types spec.md lists. They are the gate-rust helper's JSON schema
  (`ci/gate-rust/tests/schema.rs`; docs/quality-gate.md, "Reading Rust: the gate-rust helper"), generalized so that
  nothing in them is Rust's.
- **`crates/checks`** for the parser-only checks:
  - cognitive-complexity, including recursion through strongly connected components and base-score pairing
    (`gate.py:4443`–`4760`, `:4899`–`5105`)
  - vague-names (`gate.py:5433`–`5520`)
  - test-integrity's removed tests, skips, weakened asserts, changed expected literals and swallowed errors
    (`gate.py:2003`–`2215` for Rust)
  - the Python side of each comes in 04, over the same code
- **`crates/lang-rust`.** tree-sitter-rust to the model, replacing `ci/gate-rust/src/main.rs`:
  - test spans for `tests/`, `tests.rs` and inline `#[cfg(test)]` (`gate.py:1926`–`1960`)
  - `cfg` and `cfg_attr` skips
  - asserts by macro name
  - the scoring of `if`, `match`, `loop`, `while`, `for`, `let ... else`, `?`, closures, nested fns and boolean
    sequences
  - The open entries on macros (`TODO.md:126`–`133`) and pairing (`TODO.md:134`–`137`) are decided in this ticket.
    With tree-sitter, a macro body is a token tree, so the gate either skips it or parses it as expressions. Record
    which in docs and `TODO.md`.
- **Port the Rust tests of `tests/test_gate.py`** (`TestRustFiles`, `TestRustWeakenedTests`, and the complexity and
  vague-names Rust cases) as Rust tests over fixtures.
- **Delete** `ci/gate-rust` once `gate.py` no longer reads it, which is in 07. Until then it stays for the Python gate.

## Verification

- `cargo nextest run --workspace` passes.
- `cargo xtask compare` on the Rust fixtures agrees with gate.py + gate-rust, apart from the differences this ticket
  lists in `docs/`.
- Re-score the engine as of today: `cargo xtask compare` on octoflow at rust-engine-11's head lists the same
  functions over the limit as gate.py (none, after `c98c616`), or each difference is explained.
