# 06 — Shadow run: octoflow's builds run both gates

Status: ready-for-human
Blocked by: 03, 05

Changes octoflow's CI, and a human reads the comparison.

## What

- Release the gate with both programs in its tarball: `gate.py` as the judge, and `gate`, the Rust binary.
- Octoflow raises its pin to that release (gate-repo-split, "The pin").
- Octoflow's Gate stage runs `gate.py` as the judge, as before, then the Rust `gate` on the same inputs. It writes
  `quality-rust.md` and a `quality-diff.txt`, both archived. The Rust gate's exit status and differences never change
  the build's result.
- Run the same in the gate repo's own builds.

## Verification

- Over two weeks of builds, or 30 builds, whichever is later, each `quality-diff.txt` is empty or shows only
  differences documented in `docs/`. Each that isn't gets a ticket.
