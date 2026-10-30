# 04 — Octoflow's Jenkins runs the pinned gate release

Status: ready-for-agent
Blocked by: 03

Changes how every octoflow build is judged, so a human reviews the pull request. The change itself is reversible:
revert the commit.

## What

- **`ci/gate.pin`**, naming `v0.1.0` and its sha256 (spec.md, "The pin").
- **`ci/fetch-gate.sh PIN DIR`**: downloads, checks the sha256 and unpacks into DIR. A missing pin, a failed download
  or a hash mismatch exits non-zero, naming which. It uses the same pattern as `fetch_tool`
  (`ci/install-tools.sh:26`–`33`).
- **Gate stage** (`Jenkinsfile:270`–`327`):
  - A quality build reads the pin from `$JUDGED_MAIN`. Any other build reads it from its own tree.
  - The stage fetches into `$WORKSPACE_TMP/gate` and runs `$WORKSPACE_TMP/gate/gate.py` with
    `--rust-syntax $WORKSPACE_TMP/gate/gate-rust`.
  - **Cutover fallback.** If the judged main has no `ci/gate.pin`, run `git show $JUDGED_MAIN:gate.py` as today. A
    comment says which ticket removes this (05).
  - A failed fetch fails the build as "could not judge" (exit 2), writing no `quality.md`.
- **Install stage.** Stop building gate-rust from the judged tree (`Jenkinsfile:116`–`152`) when the pin is used. The
  build stays only for the fallback. Keep the `cargo check` of the tree's own `ci/gate-rust` until 05 deletes it.
- **Compatibility shims.** The `grep -q -- '"--test-contexts"'` and `'"--rust-syntax"'` probes
  (`Jenkinsfile:316`–`323`) stay only on the fallback path. The pinned release defines both flags.
- **The engine.**
  - `JUDGING_FILES` (`engine/src/cli/judged.rs:13`) gains `ci/gate.pin`. It keeps `gate.py` while the fallback exists.
  - Update its doc comment and `judged`'s tests (`engine/src/cli/judged/tests.rs`).
- **Contract test.**
  - Vendor the release's `golden/` into `engine/tests/data/quality/`.
  - `gate_golden` (`engine/src/quality/tests.rs:201`) reads from there.
  - The data directory's comment names the pin as the version those files are from.

## Verification

- `cargo nextest run -p octoflow-engine` passes, including `judged`'s and `quality`'s tests.
- `shellcheck ci/fetch-gate.sh` is clean, and the gate's shellcheck run finds nothing on it.
- Manual: a pull request build of this change runs the pinned gate. Its log shows the fetch and sha256 check, and its
  `quality.md` is the same as the previous build's for an unchanged tree.
- Manual: a quality build of a snapshot made from a main without the pin takes the fallback path.
