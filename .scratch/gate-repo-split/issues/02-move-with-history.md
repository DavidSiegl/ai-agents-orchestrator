# 02 — Move the gate's files into `octoflow-gate`, with their history

Status: ready-for-agent
Blocked by: 01, and rust-engine-11 merged into main

## What

From a fresh clone of octoflow's main, run `git filter-repo` keeping the paths spec.md's table lists, then push to
the empty `octoflow-gate` as `main`. Then, in that repository and as ordinary commits after the history:

- **Layout.** `gate.py` at the top. `ci/gate-rust/` moved to `gate-rust/`, and every reference to `ci/gate-rust`
  updated: `gate.py`'s `--rust-syntax` help and docstring (`gate.py:18`–`19`), `tests/test_gate.py`'s `gate_rust()`
  (`tests/test_gate.py:57`) and `[tool.mutmut] also_copy` (`pyproject.toml:71`).
- **`pyproject.toml`.** Only the gate's tables (spec.md). Version `0.1.0`. The project name `octoflow-gate`.
- **`tests/conftest.py`.** Only what the gate's tests use; drop the engine's and the parity harness's parts.
- **Docs.** `docs/quality-gate.md` becomes `README.md` plus `docs/`. Drop octoflow's own parts: the Jenkins quality
  job, the engine's quality rounds and `--quality-gate`. Those stay in octoflow's page (ticket 05).
- **`TODO.md`.** The gate's entries, moved from octoflow's. Each keeps its history line ("Found by ..."), and gains a
  note that it moved from octoflow on this date.
- **Jenkinsfile.** Install: uv sync, `install-tools.sh` (cargo-nextest, cargo-llvm-cov, cargo-mutants), and a build of
  `gate-rust`. Test: `uv run pytest` with coverage and `cargo test --manifest-path gate-rust/Cargo.toml`. Gate:
  `uv run python gate.py` from main's tree, as octoflow's Gate stage does now (`Jenkinsfile:263`–`340`), until ticket
  03's first release exists.
- **Also copy** `ci/install-tools.sh`'s fetch helper, as `ci/install-tools.sh` of the gate repo with only the tools
  its own CI needs.

Nothing is deleted from octoflow in this ticket; ticket 05 does that.

## Verification

In `octoflow-gate`:

- `uv run pytest` passes with the same number of tests as octoflow's `tests/test_gate.py`, `test_properties.py`,
  `test_golden.py` and the moved `test_invariants.py` tests pass on main.
- `cargo test --manifest-path gate-rust/Cargo.toml` passes.
- `uv run python gate.py --base HEAD~1 --out quality.md` exits 0 on the repo's own last commit.
- `git log --follow gate.py` reaches the gate split's commits (gate-split, 2026-10-24).
- The first Jenkins build of main is green.
