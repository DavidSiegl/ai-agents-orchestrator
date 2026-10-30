# 01 — The e2e harness in Rust, with the run and workflow scenarios

Status: ready-for-agent
Blocked by: rust-engine-11 merged into main

## What

A new workspace member, `engine/e2e/` (package `octoflow-e2e`, `publish = false`, `[lints] workspace = true`):

- **Fakes.** `[[bin]]` targets `fake-herdr`, `fake-gh` and `fake-ssh`, ported from `tests/parity/fakes.py`, with the
  same script format (`WORLD/script.json`) and the same ledger (`WORLD/fakes/state.json`, `events.jsonl` under one
  lock). The lock is `fcntl`'s in Python; here it is `nix::fcntl::flock`, since nix is in the workspace already
  (`engine/Cargo.toml:21`).
- **`World`**, ported from `tests/parity/world.py`: the project and its bare origin, the fakes first on PATH, and
  Jenkins and PyPI served by `tiny_http` from the test process. It runs the engine binary (`CARGO_BIN_EXE_orchestrator`
  through a dev-dependency on `octoflow-engine`, or `E2E_ENGINE` when set, replacing `PARITY_ENGINE`).
- **`record` and `normalize`**, ported from `world.py` and `normalize.py`. Their output must be byte-identical to
  today's recordings.
- **The recordings** move from `tests/parity/recorded/` to `engine/e2e/recorded/`. Update `test_support::recordings`
  (`engine/src/test_support.rs:246`–`247`).
- **`E2E_RECORD=1`** writes recordings, as `PARITY_RECORD=1` does (`tests/parity/test_parity.py:5`–`7`).
- **Scenarios.** In this ticket, those without kills, quality or serve: `default_*`, `blocked_build_answered`,
  `verdict_retried`, `no_pr_run`, `agent_exits`, `workflow_*`, `locks_restored`, `proofs_run`, `worktree_run`,
  `machine_run`, `publish_with_conflict`, `runs_listed_and_shown`, `branches_pruned`, `*_run_closed`,
  `workflows_listed`, `prompt_file_run`, `spec_file_run`, `conflict_refreshed`, `specs_drained`, `doctor_checks`,
  `takeover`.
- The Python harness keeps running all 43 until ticket 02. Both pass on the same recordings.

## Verification

- `cargo nextest run -p octoflow-e2e` passes every scenario this ticket ports, against the unchanged recordings.
- `git diff --stat` shows no change under the recordings, other than their move.
- `uv run pytest tests/parity` still passes.
