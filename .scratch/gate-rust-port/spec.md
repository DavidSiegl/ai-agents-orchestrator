# Spec: the gate in Rust, with a front end per language

Status: ready-for-agent (per ticket; see each)
Date: 2026-10-30
Lives in: `octoflow-gate`. It is written here until gate-repo-split/02 moves the gate, then moves with it as that
repo's `.scratch/gate-rust-port/`.

## Goal

The gate judges Python and Rust projects today, and other languages later (the user, 2026-10-30). Today it is a
5,705-line stdlib Python program (`gate.py`) plus a Rust reader whose JSON it walks (`ci/gate-rust`). The target is
one Rust binary, `gate`:

- Each check is written once, against a model that every language provides.
- Each language is a front end plus adapters for its tools.
- Adding a language adds one crate and changes no check.

## Decisions (the user's, 2026-10-30)

- **tree-sitter parses every language.** syn and `ci/gate-rust` go. A tree with error nodes counts as "could not
  read", which the gate reports as a not-run finding, as it does today for a file gate-rust cannot parse.
- The gate keeps judging Python projects. It runs the judged project's own Python for pytest and mutmut.
- Octoflow runs a pinned release (gate-repo-split).

## Must not break

- **The CLI.**
  - `--base`, `--tests` (repeatable), `--coverage` (repeatable), `--out`, `--mutation-budget`, `--base-run-budget`
    and `--test-contexts` keep their meaning (`gate.py:5624`–`5668`).
  - `--config` takes a `gate.toml` (below), and also a `pyproject.toml` with the tables gate.py reads, for one release.
  - `--rust-syntax` is accepted and ignored for one release, then removed.
- **Exit statuses.** 0 for OK, 1 for ERROR, 2 for could not judge (writing nothing), and 130 for interrupted
  (`gate.py:11`–`13`, `:105`–`107`).
- **The quality file format**, byte for byte on the golden files, since the engine reads it
  (`engine/src/quality.rs`). The format gets a document of its own, `docs/quality-file.md`, with a version. A change
  to it is a new version, which octoflow takes by raising its pin and its vendored golden files.
- **The checks and whether each fails the gate** (`gate.py:5561`–`5580`): test-integrity and cognitive-complexity
  fail; flaky-tests, surviving-mutants, passes-at-base and vague-names are to weigh. The limits stay the same:
  `COGNITIVE_COMPLEXITY_LIMIT = 15` (`gate.py:4440`), `FLAKY_SEEDS` (`:86`), and `VAGUE_NAME_WORDS` (`:5427`).
- **What a check finds, on the fixtures.** Where tree-sitter makes the Rust gate score or read something differently
  from gate.py's `ast` or gate-rust's syn, the difference is listed in the ticket and in `docs/`, and the fixture's
  expectation is changed on purpose. It never changes silently.

## Architecture

```
crates/gate/        the binary: CLI, config, the diff and changed lines, budgets, running checks,
                    the quality file, linters, coverage (Cobertura, LCOV), JUnit, suppressions, stray line ends
crates/model/       what the checks read: per file, functions (span, control-flow tree for complexity,
                    calls), bindings (name, span, fixed-by-override), tests (span, asserts, skips,
                    expected literals, swallowed errors); whether a path is test code
crates/checks/      one module per check, over model and the adapters' traits
crates/lang-rust/   tree-sitter-rust → model; nextest (TestRunner), cargo-mutants (Mutator), clippy (Linter)
crates/lang-python/ tree-sitter-python → model; pytest (TestRunner, with flaky reruns), mutmut (Mutator),
                    ruff and mypy (Linter); the pytest plugin, embedded and written out per run
fixtures/           a small repository per language and case, built by the tests with git; golden quality files
```

**The adapter traits.**

```rust
/// Runs a language's tests in a tree, as its project runs them.
trait TestRunner {
    /// The tests a change touches, by id, in a copy of tree (passes-at-base, flaky-tests).
    fn list(&self, tree: &Path, files: &[TestFile]) -> Result<Vec<TestId>, NotRun>;
    /// Runs ids under seed (None: the runner's default order) until deadline; outcomes by id.
    fn run(&self, tree: &Path, ids: &[TestId], seed: Option<u64>, deadline: Instant) -> Result<Outcomes, NotRun>;
}
/// Mutation-tests the changed functions of a copy of the tree, within a budget.
trait Mutator {
    fn survivors(&self, copy: &Path, functions: &[ChangedFunction], deadline: Instant) -> Result<Survivors, NotRun>;
}
```

A check that a language's adapters do not support is not run for that language, and says so. This is how
flaky-tests already stands for Rust (`TODO.md:79`–`81`).

**Linters are configured, not coded**, generalizing `GATE_TOOLS` (`gate.py:610`–`640`). Each `[[linter]]` in
`gate.toml` gives:
- the file globs it checks
- the argv, with `{files}` and `{rules}`
- the regex its output is read by, with named groups `path`, `line`, `rule`, `message` and `severity`
- the line-end characters it counts

clippy stays coded in `lang-rust`, since it runs per workspace (`gate.py:543`–`608`).

**`gate.toml`**, read from the judged main as the rules are today:

```toml
[tests]
python = ["tests/**", "**/test_*.py", "**/*_test.py"]   # closes gate-split/04 (configurable test files)
rust = ["**/tests/**", "**/tests.rs"]                    # plus inline #[cfg(test)], always
[budgets]
mutation = 900
base_run = 600
[[linter]]
name = "ruff"
# ...
```

## Dependencies (raise any beyond these with the user)

These were approved with the parser decision: `tree-sitter`, `tree-sitter-rust` and `tree-sitter-python`.

These need the user's approval in ticket 01, each with what it replaces:

| Crate | Replaces |
|---|---|
| `toml` + `serde` | `tomllib` |
| `serde_json` | `json` |
| `regex` | `re`, for the linter patterns |
| `quick-xml` | `xml.etree`, for JUnit and Cobertura |
| `nix` | `signal` and `os.killpg`, for process groups |
| `globset` | `fnmatch` |
| `proptest` (dev) | hypothesis, for the diff parsers' property tests (`tests/test_properties.py`) |

## Verification of the whole

- `cargo xtask compare REPO REV..REV` runs gate.py (the last Python release) and the Rust gate on each commit of a
  range, and diffs their quality files.
- Before the cutover (ticket 07) it runs over the last 50 main commits of octoflow and of `octoflow-gate`, plus every
  fixture. Each remaining difference is one of the documented ones.

## Tickets

1. `01-core-and-generic-checks` — ready-for-agent
2. `02-model-and-rust-front-end` — ready-for-agent, blocked by 01
3. `03-rust-adapters` — ready-for-agent, blocked by 02
4. `04-python-front-end` — ready-for-agent, blocked by 02
5. `05-python-adapters` — ready-for-agent, blocked by 04
6. `06-shadow-run-in-octoflow` — ready-for-human, blocked by 03, 05
7. `07-cutover` — ready-for-human, blocked by 06
