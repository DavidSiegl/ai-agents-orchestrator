# 03 — The Rust adapters: nextest, cargo-mutants, clippy

Status: ready-for-agent
Blocked by: 02

## What

In `crates/lang-rust`:

- **`TestRunner` over cargo-nextest**, porting `_rust_passes_at_base` and its helpers (`gate.py:3127`–`3357`):
  - the base copied with the change's tests (`_copy_base_with_tests`, `_unpack_commit`, `gate.py:3358`–`3473`, which
    move to `crates/gate` as language-neutral)
  - targets found from each path
  - cargo's errors read uncoloured
  - the budget shared with Python's half
- **`Mutator` over cargo-mutants** (`gate.py:4177`–`4440`):
  - the copy, uncoloured, without the CI's credentials
  - stopped with SIGINT
  - workspaces in turn, `--in-diff`, outcomes read
  - the budget left after mutmut's half
- **clippy** (`gate.py:543`–`608`): per workspace, its config files the change edits (`CLIPPY_CONFIG_NAMES`,
  `CARGO_CONFIG_NAMES`, `gate.py:64`–`68`), and `_cargo_home_in`.
- **Port the matching tests of `tests/test_gate.py`** (`TestRustPassesAtBase`, the cargo-mutants and clippy classes).
  Like today's, they fail rather than skip without cargo-nextest or cargo-mutants (commit `4fc88ea`).

## Verification

- `cargo nextest run --workspace` passes on a machine with cargo-nextest and cargo-mutants.
- `cargo xtask compare` on the Rust fixtures and the last 20 octoflow main commits agrees for passes-at-base,
  surviving-mutants and clippy.
