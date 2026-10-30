# 05 — Prompt templates filled by a `{name}` filler, not a `str.format` port

Status: ready-for-agent
Blocked by: 02

## What

`engine/src/py/format.rs` (586 lines) ports Python's `str.format`: conversions, format specs, `.attribute` and
`[index]`, and automatic and manual numbering. The templates use none of it. `examples/workflows/*.toml` and the
built-in prompts use only `{name}`.

- **A filler in `crate::prompts`.** It fills `{name}` from the run's values and turns `{{` and `}}` into literal
  braces. Any other use of a brace is an error naming the template, the placeholder and its position: a conversion
  (`!r`), a format spec (`:>10`), an attribute, an index, a positional field or an unknown name. The workflow file is
  checked when it is read, as `Pipeline::step_problem` checks templates now.
- **This closes `TODO.md:85`–`92`** ("A prompt template's `.attribute` is an error in the Rust engine"): it is now
  defined to be an error, with a message. Move the entry to Fixed.
- **`prompt_parity.rs`** (`engine/src/prompt_parity.rs:13`) parses with the new filler.
- **Delete** `py/format.rs` and its oracle cases.
- **Release notes.** One line: placeholders take no conversions or format specs.

## Verification

- `cargo nextest run --workspace` passes, including `prompt_parity` against the e2e recordings, unchanged.
- New tests: each refused brace form gives its message. `{{x}}` fills as `{x}`.
- Every file under `examples/workflows/` loads.
