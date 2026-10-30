# 06 — Remove what is left of `crate::py`: `toml` and `url`

Status: ready-for-agent
Blocked by: 03, 04, 05

## What

- **`py/toml.rs`** (234 lines) makes TOML read as `tomllib` reads it: Python's `repr`, `json.dumps` and truthiness of
  values, for `crate::checks::dependencies` (`engine/src/checks/dependencies.rs:13`). Use the `toml` crate's `Value`
  directly, and word the check's messages in TOML terms rather than Python's.
- **`py/url.rs`** (121 lines). Keep `quote`, `urlsplit` and `parse_qsl` as plain helpers in `crate::http` and
  `crate::serve`, their docs no longer citing urllib. Add no `url` crate.
- **Delete** `engine/src/py/`, `engine/tests/oracle/python.json`, and the `#[cfg(test)] recorded` and `oracle` helpers
  (`engine/src/py/mod.rs:13`–`24`).
- **`docs/architecture.md:28`** (the `py` row) goes.
- **The engine's name.** While the hints are being touched: they still say `orchestrator.py` (`TODO.md:93`–`101`). Make
  them name the binary as it was invoked, re-record, and move the entry to Fixed.

## Verification

- `cargo nextest run --workspace` passes.
- `grep -rn 'crate::py\|orchestrator\.py' engine/src` finds nothing.
- The re-recorded diff is reviewed.
