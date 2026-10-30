# 08 — The eval runner as `cargo xtask`, with Rust toy repositories

Status: ready-for-agent
Blocked by: rust-engine-11 merged into main

Running evals starts real agents, so a human runs the verification trial. Writing the code is unattended work.

## What

- **`xtask/`.** A workspace member (`publish = false`, `[lints] workspace = true`), with
  `.cargo/config.toml`'s `[alias] xtask = "run -p xtask --"`.
- **`cargo xtask eval [--tasks a,b] [--repeat N] [--config NAME] [--keep] [-- RUN_ARGS...]`** ports `evals/run.py`
  (422 lines) with the same behaviour and exit statuses (`evals/run.py:1`–`25`):
  - it builds the engine first, or uses `EVAL_ENGINE`
  - it refuses `--no-pr`, `--spec`, `--cwd`, `--machine` and `--worktree` among RUN_ARGS
  - it reads `metrics.json`
  - it keeps results where run.py keeps them
- **`cargo xtask bundles [--check]`** ports `evals/make_bundles.py`: the fixed author, committer, dates and message,
  and `--check` comparing commit ids.
- **The five tasks as Rust crates**: `bugfix`, `feature`, `refactor`, `docs`, `no-dependency`. Each toy repo is a
  small library crate with its own tests. Its hidden test is `hidden_test.rs`, which the runner copies into the clone's
  `tests/` and runs as `cargo test --test hidden_test` after the run. Exit status 0 accepts. The tasks' `task.md` is
  rewritten for Rust, with the same intent each Python task had.
- **`no-dependency`** asks the agent to add no new crate. The engine's new-dependencies check reads only Python
  manifests (`engine/src/checks/dependencies.rs:1`–`3`). Until it reads Cargo (gate-rust-checks ticket 07,
  `TODO.md:79`), the hidden test itself checks `Cargo.toml` and `Cargo.lock` for added packages.
- **Delete** `evals/run.py`, `make_bundles.py`, `acceptance.py`, `tasks/*/hidden_test.py`, the Python toy repos,
  `tests/test_evals.py` and `tests/fake_engine.py`.
- **Port `test_evals.py`'s tests** (484 lines) to xtask's own tests, with a fake engine as a `[[bin]]` of xtask.
- **`docs/design.md`, Evals**: the commands, and a note that the numbers start a new series on this date.

## Verification

- `cargo nextest run -p xtask` passes.
- `cargo xtask bundles --check` exits 0.
- Manual: `cargo xtask eval --tasks bugfix -- --workflow quick` from a herdr pane completes one trial and prints its
  numbers.
