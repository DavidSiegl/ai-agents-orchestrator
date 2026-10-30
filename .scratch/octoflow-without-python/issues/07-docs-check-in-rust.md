# 07 — The docs check as a Rust test

Status: needs-info
Blocked by: 02

Question for the user: may the test read engine/src's names through **syn**, as a new dev-dependency of the e2e
package? It is a dependency of the gate-rust helper today, so not new to the project, but new to the workspace. The
alternative is to keep matching names by pattern, the open issue `TODO.md:110`–`114`.

## What

Port `tests/test_docs.py` (343 lines) to `engine/e2e/tests/docs.rs`:

- Each command the docs run takes the flags they give it (`test_docs.py:248`), read from the binary's `--help`.
- The README's flag table gives each flag the commands that take it (`:262`), and every flag is documented (`:288`).
- The names the docs give exist in the engine (`:297`). This reads names through syn if approved, and closes
  `TODO.md:110`–`114`.
- Every relative link resolves to a file, and its anchor to a heading (`:323`). The anchors are computed as GitHub
  does; port the Python function.
- Delete `tests/test_docs.py`.

## Verification

- `cargo nextest run -p octoflow-e2e docs` passes.
- Breaking one link, removing one flag from the README, and naming one missing fn each fail the test with
  `file:line: what`.
