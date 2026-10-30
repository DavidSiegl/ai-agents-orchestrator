# 04 — `serde_json` and `jiff` in place of `py::json` and `py::datetime`, with a state.json format marker

Status: ready-for-agent
Blocked by: 02

Changes what state.json, summary.md and metrics.json hold. By the user's ruling, runs from earlier releases are
refused, not migrated (spec.md, Decisions).

## What

- **`py::json`** (584 lines: `Json`, `Map`, Python's `json.dumps` spacing and float formatting) becomes
  `serde_json::Value` and `serde_json::Map`. Writing is `serde_json::to_string_pretty`. Where a type is known, use a
  typed `#[derive(Serialize, Deserialize)]` struct, starting with `RunState` (`engine/src/state.rs:123`–`240`): its
  hand-written `from_saved` and `to_json` become serde's.
- **`py::datetime`** (437 lines: CPython 3.14's `fromisoformat`, `strftime`) becomes `jiff`. Times are written as RFC
  3339 with offset. The local-time and UTC rules of the run-id name (TODO.md, Fixed, "The Rust engine named a new run
  by the time in UTC") stay as they are.
- **The format marker.** state.json gains `"format": 2`. A state.json without it, which is every one an earlier
  release wrote, is refused by `RunState::from_saved` with: "run <id> was saved by an octoflow before <version>;
  finish or close it with that release, or remove .orchestrator/runs/<id>". `list` shows such a run as unreadable, with
  the same hint, rather than failing as a whole. `prune` and `close` still work on it, since they only need the
  directory.
- **Delete** `py/json.rs`, `py/datetime.rs`, and their cases in `engine/tests/oracle/python.json`. Drop the
  `float_roundtrip` comment's Python reason (`engine/Cargo.toml:23`–`24`), keeping the feature only if a test needs
  it.
- **Re-record** the e2e scenarios. Their recordings hold state.json, which now differs in spacing, floats and times.
- **Release notes.** The release's `docs/releases/v<version>.md` gets "Runs do not carry over" in place of
  0.49.0's "Runs carry over".

## Verification

- `cargo nextest run --workspace` passes.
- A new test: a state.json without `format` is refused with that message. `list` shows it as unreadable, and `prune`
  removes it.
- The re-recorded diff is reviewed.
- Manual: `orchestrator resume` on a run directory copied from a 0.49 run prints the refusal and exits non-zero.
