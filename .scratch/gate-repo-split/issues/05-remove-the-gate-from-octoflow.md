# 05 — Remove the gate's files from octoflow

Status: ready-for-agent
Blocked by: 04 merged into main (main carries `ci/gate.pin`, so no quality build needs the fallback any more)

## What

- **Delete** what spec.md's table moves: `gate.py`, `ci/gate-rust/`, `tests/test_gate.py`, `tests/test_properties.py`,
  `tests/gitrepo.py` if nothing else imports it, the gate's golden files, `tests/data/s3776_at_84e87e7.py.txt`, and
  the gate's tests in `tests/test_invariants.py`.
- **`Cargo.toml`.** Drop `exclude = ["ci/gate-rust"]` (`Cargo.toml:7`–`9`).
- **`pyproject.toml`.**
  - Drop `[tool.mutmut]`, since there's no Python source left for the gate to mutate, `[tool.coverage.run]`, and the
    gate-only dev dependencies.
  - Keep the rule tables octoflow is judged by. Those leave with spec octoflow-without-python.
  - The Test stage's `--cov=gate --cov-context=test` (`Jenkinsfile:227`) and the Gate stage's `--test-contexts` go.
- **Jenkinsfile.**
  - Drop the cutover fallback and its shims (ticket 04).
  - Drop the Install stage's gate-rust build and `cargo check`.
- **Engine.**
  - `JUDGING_FILES` drops `gate.py` and `orchestrator.py`.
  - The comments citing `gate.py` (`engine/src/patch.rs:18`, `engine/src/checks.rs:69`–`94`, `engine/src/quality.rs:1`–`5`, `:36`, `:208`, `engine/src/constants.rs:99`, `engine/src/cli/args.rs:594`) name the gate repo and the pin instead.
- **Docs.**
  - `docs/quality-gate.md` shrinks to octoflow's side: the quality job, the pin and how to raise it, and the engine's
    reading of the quality file. It links to the gate repo for the rest.
  - `README.md:70`, `:208`–`209` and `:438`–`445`, and `docs/architecture.md:7`–`16`, are updated to match.
  - `tests/test_docs.py` checks the links still resolve.
- **`TODO.md`.** The moved entries become one line under Fixed: "The gate moved to octoflow-gate", with the date and
  a pointer to the gate repo's TODO.md.
- **Release notes.** Add a line to the next `docs/releases/v<version>.md`.

## Verification

- `cargo nextest run --workspace` passes.
- `uv run pytest` passes, with only the docs, evals and parity tests left.
- `grep -rn 'gate\.py\|gate-rust' --exclude-dir=target --exclude-dir=.venv .` finds only release notes, the history
  in TODO.md, and the docs that name the gate repo.
- The pull request's Jenkins build is green, and its Gate stage ran the pinned release.
