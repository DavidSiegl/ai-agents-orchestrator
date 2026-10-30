# 07 — Cutover: the Rust gate judges, gate.py goes

Status: ready-for-human
Blocked by: 06

Changes how every build of octoflow and of the gate is judged, and publishes a release.

## What

In `octoflow-gate`:

- **Delete** `gate.py`, `tests/` (Python), `pyproject.toml` (keeping the embedded plugin's ruff rules in `gate.toml`),
  `uv.lock` and `gate-rust/`.
- **Drop** the `pyproject.toml` compatibility of `--config` and the ignored `--rust-syntax`.
- **Release** a major version. Its tarball holds `gate` and the golden files. The release notes list the documented
  differences from gate.py.
- **The gate repo's Jenkinsfile** has no uv and no pytest.

In octoflow:

- **Raise the pin.** The Gate stage runs `gate` with octoflow's `gate.toml`. This settles octoflow-without-python/09's
  question of where the rules live.
- **Vendor** the release's golden files.
- **`JUDGING_FILES`** names `gate.toml`.

Rollback: lower the pin to the shadow release, whose `gate.py` still judges. Keep that release's assets published.

## Verification

- The first octoflow pull request build after the pin change is green, with `quality.md` written by `gate`.
- `git ls-files '*.py'` in `octoflow-gate` lists only the embedded plugin and the Python fixtures.
