# 09 — Remove the Python toolchain from octoflow

Status: needs-info
Blocked by: 02, 06, 07, 08, gate-repo-split/05

Question for the user: what lints the Markdown once octoflow has no Python environment? The gate runs pymarkdown
(`gate.py:620`–`622`) with the rules in `pyproject.toml`'s `[tool.pymarkdown]` (`pyproject.toml:85`–`89`). Options:

- pymarkdown, installed on the agents as a tool (`uv tool install` or pipx), outside the tree
- a Rust Markdown linter, a new tool for the gate to know about
- no Markdown linting

The answer decides where the gate's rules live once `pyproject.toml` is gone: a `gate.toml`, which gate-rust-port
defines, or a `.pymarkdown.json` beside an interim gate.

## What

- **Delete** `pyproject.toml`, `uv.lock`, `.python-version`, `tests/` (`conftest.py`, `guard_errors.py`,
  `test_invariants.py` and `golden.py` stay only if a Python test is left, and none should be), and `.gitignore`'s
  Python entries.
- **`test_invariants.py`'s remaining rules** move to Rust tests or to the place they guard:
  - The engine's version is the release's (`:71`): the Release stage reads `engine/Cargo.toml`'s version.
  - No test sleeps for real (`:91`): drop it, or a clippy `disallowed_methods` on `std::thread::sleep` in tests, with
    reason.
- **Jenkinsfile.**
  - No `uv sync` and no pytest step.
  - `PROJECT_VERSION` from `engine/Cargo.toml` via `cargo metadata` (`Jenkinsfile:154`–`157`).
  - The Gate stage runs the pinned gate with `python3`, the system's, until gate-rust-port's release, and its rules
    from the file the question above decides.
  - `--tests test-results.xml` and `--coverage coverage.xml` go.
- **Engine.** `JUDGING_FILES` drops `pyproject.toml`, `uv.lock` and `.python-version`, and gains the rules file.
- **README.** The requirements (`README.md:70`) and the development section (`README.md:436`–`446`) name cargo and npm
  only.

## Verification

- `git ls-files '*.py' pyproject.toml uv.lock .python-version` is empty.
- `cargo nextest run --workspace` passes, and `npm test --prefix ui` passes.
- The pull request's Jenkins build is green, with a Gate stage that judged it.
