# 05 — The Python adapters: pytest, mutmut, ruff, mypy

Status: ready-for-agent
Blocked by: 04

## What

In `crates/lang-python`:

- **Which Python.** The judged project's Python: `--python PATH`, else `python3` on PATH. This replaces
  `sys.executable` (`gate.py:16`–`18`). A Python without pytest is a not-run finding, as today.
- **The pytest plugin.** Today the gate imports itself into pytest (`pytest_addoption` and `pytest_sessionfinish`,
  `gate.py:2852`–`2875`). The plugin becomes a Python file of about 20 lines, embedded in the binary with
  `include_str!`. Each run writes it into its scratch directory and puts it on `PYTHONPATH` with `-p`. It is the only
  Python in the gate's source, and it is linted by the gate's own ruff run.
- **`TestRunner` over pytest**, porting:
  - flaky-tests (`gate.py:2217`–`2770`): groups by inifile, reruns under `FLAKY_SEEDS` with pytest-randomly, a
    process group per run (`_run_in_session`)
  - Python's passes-at-base (`gate.py:2773`–`3125`): the base copy, imports from the working tree, the failed
    conftest
- **`Mutator` over mutmut** (`gate.py:3474`–`4176`):
  - the narrowed table, and the copy marking unchanged functions `do_not_mutate`
  - `--test-contexts` read from coverage's SQLite. rusqlite would be a new dependency, so ask the user, or run the
    judged Python's `coverage` to export JSON instead.
  - the budget, statuses after the run, and dropping saves that were cut off
- **ruff and mypy** as `[[linter]]` entries in a default `gate.toml` for Python projects.
- **Port the matching tests** of `tests/test_gate.py`. After this ticket, `tests/test_gate.py` has nothing left that
  the Rust tests do not cover; it is deleted in 07.

## Verification

- `cargo nextest run --workspace` passes on a machine with a Python that has pytest, pytest-randomly and mutmut.
- `cargo xtask compare` over the fixtures and the last 50 `octoflow-gate` commits: every quality file agrees with
  gate.py's, apart from the listed differences.
