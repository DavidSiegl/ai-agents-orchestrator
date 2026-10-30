# 01 — The `gate` binary's core and its language-neutral checks

Status: ready-for-agent
Blocked by: gate-repo-split/02; the user's approval of the dependencies in spec.md's table

## What

A Cargo workspace in `octoflow-gate`, beside `gate.py`, with `crates/gate`:

- **CLI and exit statuses**, as spec.md's "Must not break" lists. Ctrl-C kills the process group of whatever the gate
  started and exits 130, as `_kill_group` and `EXIT_INTERRUPTED` do (`gate.py:2695`, `:107`).
- **The diff.** `_change_diff`, `changed_lines`, `_removed_lines`, `_hunk_*`, `_unquote` and `_diff_git_path`
  (`gate.py:170`–`330`), with their property tests ported to proptest from `tests/test_properties.py`, git still the
  oracle.
- **The quality file.** `quality_report` and its sections (`gate.py:332`–`456`), held to the golden files byte for
  byte.
- **Config.** `gate.toml` (spec.md), and the `pyproject.toml` compatibility reader for one release.
- **Linters** from `[[linter]]`. shellcheck, actionlint and pymarkdown are configured in the gate repo's own
  `gate.toml`.
- **Coverage.** Cobertura (`new_coverage`, `_changed_line_coverage`, `gate.py:1127`–`1222`) and LCOV, with
  `GATE_MIN_COVERAGE` and `GATE_MIN_NEW_LINES`.
- **JUnit.** `failing_tests`, `junit_outcomes` (`gate.py:1098`–`1125`).
- **Suppressions and stray line ends** (`gate.py:1225`–`1310`). The suppression patterns become per-language data,
  which 02 and 04 provide.
- **Golden files and edited build config.** The golden-file finding of test-integrity (`gate.py:1365`–`1395`), and
  the edited build-config section (`gate.py:439`–`488`).
- **`cargo xtask compare`** (spec.md), so each later ticket checks itself with it.

The Python checks keep running from `gate.py` until 05. In this ticket the Rust binary judges only what it ports.

## Verification

- `cargo nextest run --workspace` passes.
- The golden-file tests reproduce `tests/golden/gate_*.txt` and `quality_report_*.txt` unchanged.
- `cargo xtask compare` on the fixtures agrees with gate.py for every finding of the parts ported here.
- `cargo clippy --workspace --all-targets -- -D warnings` is clean.
