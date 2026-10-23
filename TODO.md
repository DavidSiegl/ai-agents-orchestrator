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
