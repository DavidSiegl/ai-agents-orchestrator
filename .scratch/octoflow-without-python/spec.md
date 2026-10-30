# Spec: octoflow without Python

Status: ready-for-agent (per ticket; see each)
Date: 2026-10-30

## Goal

Octoflow's tree holds Rust and the desktop UI's TypeScript, and nothing else but shell glue, the Jenkinsfile and
data. The user wants a clean, stringent codebase with few languages and dependencies. Once the gate has moved out
(spec gate-repo-split), the Python left is test harnesses, eval tooling and a code generator. The engine also carries
a port of Python's own semantics, `engine/src/py/` (3,905 lines). That port existed so the Rust engine would match
the Python one byte for byte, and the Python engine is gone (rust-engine ticket 11).

## Decisions (the user's, 2026-10-30)

- **No compatibility with runs from earlier releases.** The release that changes state.json's format refuses older
  state files with a clear message. The release notes say to finish or close runs before upgrading. This supersedes
  "Runs carry over" (`docs/releases/v0.49.0.md`, "Runs carry over") for that release onward.
- **The eval tasks become Rust toy repositories.** Their numbers start a new series.
- The e2e scenarios (today's parity scenarios) stay, ported to Rust. They are the only test of a run from start to
  verdict (`engine/tests/program.rs:1`–`3`).

## What goes, and what replaces it

| Now | Replacement | Ticket |
|---|---|---|
| `tests/parity/` (2,260 lines of Python: `world.py`, `fakes.py`, `scenarios.py`, `normalize.py`, `test_parity.py`), `tests/rust_engine.py` | a Rust e2e test package; the recordings kept as snapshots | 01, 02 |
| `engine/src/py/text.rs`, `unicode_tables.rs`, `engine/tools/unicode_tables.py` | std's `str` methods, `char::is_whitespace` and similar | 03 |
| `engine/src/py/json.rs`, `py/datetime.rs` | `serde_json::Value`, `jiff` (both already dependencies, `engine/Cargo.toml:16`, `:24`) | 04 |
| `engine/src/py/format.rs` | a template filler for `{name}` and `{{` / `}}` | 05 |
| `engine/src/py/toml.rs`, `py/url.rs` | the `toml` crate directly (a dependency already, `engine/Cargo.toml:41`), and a few plain helpers | 06 |
| `engine/tests/oracle/python.json` and the other Python-recorded oracles that only `py/` replays | nothing | 03–06 |
| `tests/test_docs.py` | a Rust integration test | 07 |
| `evals/run.py`, `make_bundles.py`, `acceptance.py`, `tasks/*/hidden_test.py`, `tests/test_evals.py`, `tests/fake_engine.py` | `cargo xtask eval`, `cargo xtask bundles`, and Rust toy repos | 08 |
| `pyproject.toml`, `uv.lock`, `.python-version`, the Jenkinsfile's `uv` | nothing | 09 |

`engine/src/prompt_parity.rs` (prompts checked against the recordings) stays. It reads the recordings from their new
place.

## What must not break

- **The 43 scenarios' recordings.** Tickets 01–02 port them without changing them. Tickets 03–06 may change them, but
  only by re-recording, with the diff reviewed in the same pull request, and each change explained there: wording,
  float or time formatting, key order.
- **The prompts agents get.** `examples/workflows/*.toml` use only `{name}` placeholders (`spec_path`, `build_path`,
  `cwd`, `n`, ...). Ticket 05 must keep filling those exactly. A placeholder with a conversion, format spec or
  attribute becomes an error naming the placeholder. Today it is Python's `str.format` semantics, and `.attribute` is
  an open bug (`TODO.md:85`–`92`).
- **Users' workflow files.** These live outside the tree and may use the same features. The release notes name the
  change (ticket 05).
- **The quality file contract**, which the gate repo now owns.
- **Every CI stage**: Test, Gate, Package, Release.

## Failure halfway

Each ticket is a release-neutral refactor, except 04 and 05. 04 changes state.json's bytes, and 05 changes template
errors. For those two:

- **04 adds a format marker.** state.json gains `"format": 2`, and the engine refuses a state.json without it: "run
  `<id>` was saved by an octoflow before 0.50; finish or close it with that release". That keeps a half-upgraded
  machine from mis-reading an old run. Rollback: install the previous release, which still reads every run it wrote.
  A run the new release saved, the previous release does not refuse: it keeps an unknown key such as `format`
  (`RunState::from_saved`, `engine/src/state.rs:240`) and may misread the rest. So the release notes say to finish
  runs before rolling back as well.
- **The release that ships 04** says so in `docs/releases/v<version>.md`, under "Runs do not carry over".

## Lane

- **The e2e tests** go in a workspace member of their own, `engine/e2e/` (package `octoflow-e2e`). Its fake herdr, gh
  and ssh are `[[bin]]` targets: a test finds them through `CARGO_BIN_EXE_*`, which Cargo sets only for binaries of
  the test's own package, so they cannot live in `octoflow-engine`'s `tests/`. The fake Jenkins and PyPI run on
  `tiny_http` in the test process, as `engine/src/http.rs`'s tests do already (`engine/Cargo.toml:36`–`38`).
- **Repo chores** go in `xtask/`, the Cargo convention for project scripts in Rust. It is a workspace member, run as
  `cargo xtask <cmd>` through an alias in `.cargo/config.toml`.
- **New dependencies.** None planned beyond what the engine has. Where a ticket finds it needs one (syn for ticket
  07; possibly `tempfile`), the user approves it in the ticket, as each of `engine/Cargo.toml`'s dependencies records
  ("Approved by the user for ...").

## Order

01 → 02 (e2e in Rust) can start at once, beside the gate split. 03–06 come after 02, so each re-recording runs on
the Rust harness. 07 and 08 are independent. 09 is last, blocked by everything here and by gate-repo-split/05. The
Python gate needs only a system `python3`, not this tree's `uv` environment, until gate-rust-port replaces it.

Blocked overall by rust-engine-11 merging into main.
