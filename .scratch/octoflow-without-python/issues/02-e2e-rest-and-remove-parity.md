# 02 — The remaining e2e scenarios, then remove the Python harness

Status: ready-for-agent
Blocked by: 01

## What

- **Port the rest.**
  - `kill_in_*`, `product_team_killed`, `resume_after_agent_exit`.
  - `quality_*`, `judged_in_quality`, `gate_turned_*_by_resume`. These need the fake Jenkins and the real gitleaks.
  - `serve_endpoints_and_events`.
- **Kills.** Kill the engine's process group with `nix::sys::signal::killpg`, as `engine/tests/program.rs` does.
- **Delete** `tests/parity/` and `tests/rust_engine.py`. In `tests/test_docs.py`, read the binary's help through the
  path `cargo build` prints, until ticket 07 replaces that file.
- **Jenkinsfile.**
  - The Test stage runs the e2e package in its nextest run, with the instrumented engine
    (`Jenkinsfile:192`–`230`).
  - The quality-build skip condition (`Jenkinsfile:219`–`224`) becomes a nextest filter excluding `octoflow-e2e`
    under the same `git diff --quiet` paths, with `tests/parity` replaced by `engine/e2e`.
  - `PARITY_ENGINE` and the `LLVM_PROFILE_FILE` workaround move to `E2E_ENGINE`.
- **Rename everywhere.** "parity scenarios" becomes "e2e scenarios" in `docs/architecture.md` (Tests), README,
  `TODO.md:32`–`62`, `engine/tests/program.rs:1`–`3`, `engine/src/prompt_parity.rs:1`–`6`, and
  `ci/install-tools.sh:6`–`7`.
- **The parity TODO entry.** `TODO.md:32` becomes "The e2e scenarios: what they do not record". Drop the "recorded
  from the Python engine" closings, since none can be: each gap now closes by recording the Rust engine, reviewed.

## Verification

- `cargo nextest run --workspace` passes all 43 scenarios against unchanged recordings.
- The pull request's Jenkins build is green, and its JUnit report lists the 43.
- `git ls-files tests/parity` is empty.
