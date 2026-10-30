# 01 — Create the `octoflow-gate` repository and its Jenkins job

Status: ready-for-human

Outward-facing: it creates a GitHub repository and a Jenkins job.

## What

- Create `DavidSiegl/octoflow-gate` on GitHub, with the same visibility as `octoflow-ai`. Leave it empty: ticket 02
  pushes its history.
- Install the Claude GitHub App on it if agents should push there.
- Create a Jenkins multibranch job for it on the agent pool octoflow's job uses. The job has the same tools
  available: uv, Python 3.13+, rustup with clippy and llvm-tools, and `ci/install-tools.sh`'s tools.

## Verification

- `git ls-remote https://github.com/DavidSiegl/octoflow-gate` answers, with no refs.
- The Jenkins job shows up and scans the empty repository without error.
