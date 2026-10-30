# 03 — Release the gate as a versioned tarball

Status: ready-for-human
Blocked by: 02

Publishes outward: it creates GitHub releases.

## What

In `octoflow-gate`, a Release stage modelled on octoflow's (`Jenkinsfile:359`–`400`). A merge into main that raises
`pyproject.toml`'s version releases `v<version>` with:

- `octoflow-gate-<version>-linux-x86_64.tar.gz`, which holds:
  - `gate.py`
  - `gate-rust`, `cargo build --release --locked` of `gate-rust/`
  - `golden/`, the quality file's golden files (`gate_*.txt`, `quality_report_*.txt`)
- `octoflow-gate-<version>-linux-x86_64.tar.gz.sha256`, which holds the asset's sha256.

A smoke step before the release unpacks the tarball into an empty directory and checks:
- `python3 gate.py --help` exits 0
- `./gate-rust < fixture.rs` emits JSON of the schema `gate-rust/tests/schema.rs` checks

The gate's own Gate stage then judges with the previous release, fetched as octoflow will fetch it (spec.md, "The
pin"), with `ci/gate.pin` in the gate repo.

## Verification

- Release `v0.1.0` exists with both assets.
- `sha256sum -c` of the downloaded asset against the `.sha256` file passes.
- A build after the release shows the Gate stage running the released `gate.py`, not main's.
