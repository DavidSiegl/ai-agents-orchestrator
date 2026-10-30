# Spec: the quality gate in a repository of its own

Status: ready-for-human
Date: 2026-10-30
Decided by the user: split now, before the Rust rewrite (gate-rust-port), and not "once a second project uses it"
(superseding `TODO.md:28`–`31`, 2026-10-24). Octoflow's CI runs a **pinned release** of the gate.

## Why

The gate will judge Python projects, Rust projects and later other languages, for octoflow and for other projects.
Octoflow itself is to become Rust plus the desktop UI's TypeScript (spec octoflow-without-python). Keeping the gate in
this tree keeps Python, pytest, mutmut, hypothesis, ruff and mypy here for one program that is not octoflow's engine.

The boundary already exists, so the split is a move, not a redesign:

- The engine imports nothing from the gate; the only contract is the quality file (`gate.py:21`–`23`;
  `engine/src/quality.rs:1`–`5`; docs/quality-gate.md, "The quality file as a contract").
- The engine holds that contract to the gate's golden files (`engine/src/quality/tests.rs:198`–`208`, reading
  `tests/golden/gate_*.txt`).
- Jenkins already treats the gate as an outside program taken from a trusted tree: `git show $JUDGED_MAIN:gate.py`
  (`Jenkinsfile:281`–`291`), and the gate-rust helper built from the judged main (`Jenkinsfile:116`–`152`).

## What moves to `octoflow-gate`

With their git history (`git filter-repo --path ...`):

| Path here | Notes |
|---|---|
| `gate.py` | the gate, unchanged |
| `ci/gate-rust/` | the Rust reader, its own crate and `Cargo.lock` already (`Cargo.toml:7`–`9` excludes it) |
| `tests/test_gate.py`, `tests/test_properties.py`, `tests/gitrepo.py` | the gate's tests (`test_properties.py:1` tests the gate's diff parsers) |
| `tests/golden/gate_*.txt`, `tests/golden/quality_report_*.txt`, `tests/golden.py`, `tests/test_golden.py` | the quality file's golden files and their helper |
| `tests/data/s3776_at_84e87e7.py.txt` | cognitive-complexity data |
| the gate's half of `tests/conftest.py`, `tests/guard_errors.py` | the no-sleep and no-network guards (`tests/conftest.py:11`–`12`, `44`–`48`) |
| `tests/test_invariants.py:60`–`69`, `79`–`90` | stdlib-only, no runtime dependencies, the C tracer, no Authorization header |
| `docs/quality-gate.md` | becomes the gate repo's docs; octoflow keeps a short page (below) |
| `pyproject.toml`'s `[dependency-groups] dev`, `[tool.mutmut]`, `[tool.coverage.run]`, `[tool.ruff]`, `[tool.mypy]` for `gate.py` | the gate's own dev setup (`pyproject.toml:18`–`85`) |
| the gate's entries of `TODO.md` | "The gate judges Rust as it judges Python", "Test code outside the places the spec names", the macro and pairing entries (`TODO.md:70`–`84`, `115`–`148`), and gate-split/04 (configurable test files, `TODO.md:28`–`31`) |

The Install stage's cargo-mutants (`ci/install-tools.sh:10`) is the gate's tool for judging Rust: it stays installed
on octoflow's agents, since the gate runs it on octoflow's changes.

## What stays in octoflow

- The engine's reader of the quality file (`engine/src/quality.rs`) and its contract test, now reading a vendored
  copy of the pinned release's golden files: `engine/tests/data/quality/<name>.txt`, replaced whenever the pin moves.
- The Jenkinsfile's Gate stage, running the pinned gate; the rules octoflow is judged by (`pyproject.toml`'s ruff,
  mypy and pymarkdown tables for now, a `gate.toml` once gate-rust-port has one).
- A short `docs/quality-gate.md`: how to set up the quality job, which gate version octoflow pins and where, how the
  engine reads the quality file. It links to the gate repo for what the gate judges.
- `engine/src/patch.rs:18` and `engine/src/checks.rs:69`–`94` copy gate.py's diff reading and finding format ("a fix
  to one belongs in both"). Their comments then name the gate repo and its version instead of `gate.py`.

## The pin

A tracked file `ci/gate.pin`:

```
version = "0.1.0"
sha256 = "<sha256 of octoflow-gate-0.1.0-linux-x86_64.tar.gz>"
```

- A release of the gate is `octoflow-gate-<version>-linux-x86_64.tar.gz` on its GitHub release `v<version>`. It holds
  `gate.py`, the `gate-rust` binary built for linux x86_64, and the golden files. Its sha256 is published beside it.
- `ci/fetch-gate.sh PIN DIR` downloads the asset the pin names, checks its sha256 and unpacks it into DIR, failing
  with a message naming what failed. It works like `fetch_tool` in `ci/install-tools.sh:26`–`33`.
- **A quality build takes the pin from the judged main**: `git show "$JUDGED_MAIN:ci/gate.pin"`, as it takes `gate.py`
  now (`Jenkinsfile:281`–`291`). Any other build takes the pin from its own tree. So a change cannot pick the gate
  that judges it, and a change to the pin is judged by the previous pin's gate.
- The rules still come from the judged main (`Jenkinsfile:288`).
- The engine's `JUDGING_FILES` (`engine/src/cli/judged.rs:13`) becomes `["Jenkinsfile", "ci/gate.pin", <the rules
  file>, ...]`. It no longer lists `gate.py` or `orchestrator.py`, which are gone from the tree. Otherwise a quality
  build of another gate version counts as having judged the tree.

## What must not break

- A quality build, a pull request build and a main build each still write `quality.md`, with the same exit-status
  handling (`Jenkinsfile:328`–`334`), and a quality build is still judged by main's gate and main's rules.
- `judged-in-quality` (`engine/src/cli/judged.rs`) still refuses a build whose judging files differ.
- A build whose `ci/fetch-gate.sh` fails fails as "could not judge" (exit 2), never as a pass, and writes no
  `quality.md`, as a main without gate.py does now (`Jenkinsfile:283`–`285`).
- The engine's quality-file contract test stays green, now against the vendored golden files.

## Cutover and rollback

- **Cutover.** Until a main commit carries `ci/gate.pin`, a quality build judged by an older main still uses
  `git show $JUDGED_MAIN:gate.py`. The Jenkinsfile accepts both for that window: the pin if the judged main has one,
  else main's `gate.py` (ticket 04). Ticket 05 drops the fallback once main has the pin.
- **Rollback.** Revert ticket 04's commit, which restores `git show $JUDGED_MAIN:gate.py`. `gate.py` stays in this tree
  until ticket 05, so the rollback needs nothing from the gate repo.
- **Irreversible.** Creating the GitHub repository and publishing releases. Both are ready-for-human.

## Lane

- **Gate repo.** The layout of today's files, unchanged: `gate.py` at the top, `gate-rust/` as its own crate. The
  Rust rewrite (gate-rust-port) reshapes the repo; this spec only moves it.
- **CI for the gate repo.** A Jenkins multibranch job, as octoflow's, with a Jenkinsfile of its own. Its Test stage
  runs pytest and `cargo test` of gate-rust. Its Gate stage judges each change with the previous release (the pin
  scheme above, applied to itself). Until the first release exists, it judges with main's `gate.py`, as octoflow does
  today.

## Tickets

1. `01-create-the-repository` — ready-for-human
2. `02-move-with-history` — ready-for-agent, blocked by 01
3. `03-release-pipeline` — ready-for-human, blocked by 02
4. `04-octoflow-runs-the-pinned-gate` — ready-for-agent, blocked by 03
5. `05-remove-the-gate-from-octoflow` — ready-for-agent, blocked by 04

Blocked overall by rust-engine-11 merging into main (142 commits ahead of `origin/main` today). The move starts from
main, so it should hold the switch's gate.
