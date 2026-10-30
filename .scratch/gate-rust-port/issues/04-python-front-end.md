# 04 — The Python front end on tree-sitter-python

Status: ready-for-agent
Blocked by: 02

## What

In `crates/lang-python`, tree-sitter-python to the model:

- **What counts as Python.** A `.py` file, or a script whose `#!` line names python (`_is_python_source`,
  `_was_python_source`, `gate.py:4801`–`4850`). Decoding follows a BOM or coding cookie (`_decode_python`,
  `gate.py:1548`).
- **Functions and complexity.** Their spans start at their decorators, and scoring follows `cognitive_complexity` and
  its helpers (`gate.py:5107`–`5280`), with calls resolved through scopes (`_Scope`, `gate.py:5279`–`5430`).
- **Tests.**
  - test functions and classes (`_python_tests`)
  - removed tests
  - skips (`skipIf`, `pytest.mark.skip`)
  - asserts (`_python_assertions`)
  - swallowed errors (`_swallows`)
  - changed literals, with constants followed (`_PythonConstants`, `gate.py:1739`–`1845`)
- **Bindings for vague-names**, and the names fixed by an override (`_bound_names`, `_fixed_by_override`,
  `gate.py:5519`–`5560`).
- **Suppression patterns**: `# noqa`, `# type: ignore`, `# pragma: no cover`, and the rest `_suppressing_lines` reads
  (`gate.py:1236`).

Where tree-sitter's tree differs from `ast`'s in a way that changes a score, list it in `docs/`, as in 02. Known
candidates: decorators, `match` statements, and comprehension scopes.

- **Port the Python cases of `tests/test_gate.py`** for test-integrity, cognitive-complexity and vague-names, with
  `tests/data/s3776_at_84e87e7.py.txt` as a fixture.

## Verification

- `cargo nextest run --workspace` passes.
- `cargo xtask compare` on the Python fixtures, and on `gate.py`'s own history (the last 50 commits of
  `octoflow-gate`), agrees for the three parser checks, apart from the listed differences.
