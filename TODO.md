# TODO

What is known to be open in `orchestrator.py`, and why. An entry leaves `Open issues` only for `Fixed`, with what
was done.

## Open issues

- **A test file whose name is not UTF-8 is never rerun by `flaky-tests`.** pytest-randomly 5.0.0 hashes each node
  id as strict UTF-8 (`pytest_randomly._crc32`, called from `pytest_runtest_setup` and its neighbours) and stops
  with an internal error, exit status 3. `_pytest` already lets pytest print such an id
  (`PYTHONIOENCODING=utf-8:surrogateescape`), and the collection runs with `-p no:randomly`, so the id is found;
  each rerun then crashes, and `flaky_tests` reports `not-rerun` quoting the `UnicodeEncodeError`. Raised by the
  cross-review of `flaky-tests` (F10, 2026-10-23). Closes when pytest-randomly hashes the id's bytes, or when the
  reruns can seed such a test without it.
- **Coverage of a file whose name is not UTF-8 is never measured.** coverage.py 7.14 collects no data for it, so
  `new_coverage` finds none of its added lines in `coverage.xml` and counts them as not coverable: the coverage
  condition can pass a change whose new code there is untested. `docs/quality-gate.md` states it. Closes with a
  coverage.py that records such files, or with the gate naming them as unmeasured.
- **The reruns' time limit is thin for the check's own tests.** `FLAKY_RUN_TIMEOUT` is 30 s a run. On
  `flaky-tests`' own change each run took about 9 s on an idle laptop and about twice that under load, since many of
  `TestFlakyTests`' tests start nested pytest runs. A run over the limit makes `flaky_tests` report `not-rerun`,
  not a wrong verdict. Not measured on the Jenkins agent. Closes with a measured limit, or a lighter
  `TestFlakyTests`.
- **A pytest configuration below the copy's top directory is not used, so its tests are never rerun.**
  `_pytest_inifile` looks only in the top directory of `flaky_tests`' copy, and `_pytest` passes what it finds with
  `-c`, or an empty `pytest.ini`. A project whose pytest configuration is in a subdirectory (`pkg/pyproject.toml`,
  `tests/pytest.ini`) loses that configuration's `pythonpath`, `addopts`, plugins and markers, so every change to
  its tests gets `not-rerun` (typically a collection error) and no test is rerun. Before `-c`, pytest's
  `locate_config` walked up from the changed test files and found it, but it also walked past the copy into
  `TMPDIR` (review F12). Raised by the cross-review of `flaky-tests` (F13, 2026-10-23). Closes when each changed
  test's configuration is found by walking from its directory up to the copy's top and no further, in
  `locate_config`'s order, and the reruns run once per configuration found.
- **A file name that holds a line separator breaks its list item in the quality file.** `quality_report` writes
  names as they are, so `\r` or U+2028 in one shows as a line break in some renderers. The finding is still
  matched to the right file. Closes with names escaped when the quality file is written.
- **The mutation budget's default is not measured on the Jenkins agent.** `MUTATION_BUDGET` is 600 s, which fits
  `QUALITY_TIMEOUT` by the arithmetic beside it (budget + 75 s, flaky-tests' 180 s, the test run). mutmut runs one
  worker per CPU: on this branch's own change it made 1702 mutants in 221 s on 24 cores, so an agent with fewer cores
  ends at the budget, reported as `budget-reached`. Closes with a real quality build's timings, and the default
  lowered if the build does not fit.
- **`parse_args` still yields many survivors after `do_not_mutate_patterns = ['\bhelp=']`.** Its `description=` texts
  and flag names are mutated, and few tests check them word for word: 105 of 305 mutants survived on this branch's
  change. Reported per function, so it is one finding. Closes with a decision to add `'\bdescription='` to the
  pattern, or to leave it.
- **Mutation-testing the code that runs mutmut leaves processes behind.** When mutmut stops a mutant at its own
  timeout it kills the worker, and a real mutmut that `TestSurvivingMutants` started in its own session lives on as
  an orphan group of one process per CPU. It only happens when a change edits that code. Jenkins' process-tree
  killer should end them with the build; not checked. Closes with a check on a PR build, or with that test run
  without its own session under mutmut.
- **The surviving-mutants check selects functions by added lines only.** A function whose edit only removes lines,
  or changes only the comments or blank lines above it, is not mutation-tested, unlike flaky-tests' selection
  (`_edits_test`). Kept as the ticket states it. Closes with a decision to pass `_edits_test`-like counting to
  `_changed_python_functions`, which the cognitive-complexity check also uses.
- **A run the budget stops can leave mutmut's results unreadable.** mutmut 3.8.0 saves a module's results by
  rewriting `mutants/<path>.meta` in place after each mutant (`SourceFileMutationData.save`, `open(..., "w")` then
  `json.dump`), so the SIGINT at the budget, or the kill after `MUTMUT_STOP_GRACE`, can land mid-write. `mutmut results`
  then exits with a `JSONDecodeError`, and `_mutmut_findings` reports `not-run`, "mutmut could not list its results",
  without the `budget-reached` finding or anything the run had shown. Seen once in another branch's gate run. Closes
  with the stop waiting for a save to end, or with the gate reading what remains and saying the budget stopped it.
