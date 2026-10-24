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
  `_changed_python_functions`. The cognitive-complexity check has a walker of its own (`_scored_functions`), so it
  is not affected.
- **A run the budget stops can leave mutmut's results unreadable.** mutmut 3.8.0 saves a module's results by
  rewriting `mutants/<path>.meta` in place after each mutant (`SourceFileMutationData.save`, `open(..., "w")` then
  `json.dump`), so the SIGINT at the budget, or the kill after `MUTMUT_STOP_GRACE`, can land mid-write. `mutmut results`
  then exits with a `JSONDecodeError`, and `_mutmut_findings` reports `not-run`, "mutmut could not list its results",
  without the `budget-reached` finding or anything the run had shown. Seen once in another branch's gate run. Closes
  with the stop waiting for a save to end, or with the gate reading what remains and saying the budget stopped it.
- **Committed mutmut output is read as the change's own code by the linters.** The checks leave `mutants/` out, by the
  user's decision of 2026-10-24 (review round 9, F33): `GateChange.from_tree` diffs the tree without it, so no check
  (test-integrity, flaky-tests, surviving-mutants, cognitive-complexity) selects a test, a function or a changed line
  there or counts an edit there as a change to the code, and the build configuration listing still names such files.
  That filter does not reach the linters: `judge_change` runs them over every tracked file their pathspecs match and
  keeps their findings on its own, unfiltered `changed_lines`, so a change that commits `mutants/calc.py`, mutmut's
  mutated copy with its trampolines, has its added lines there linted, and their findings can fail the gate. The same
  unfiltered lines feed the suppressions list, the stray-line-end refusal and the coverage condition; whether coverage
  counts such files was not traced. Whether the linters should read them, leave them out, or the gate refuse them is
  open. Closes with that decision.
- **The cognitive-complexity check reads and parses changed files in a loop of its own.** `_judged_files` and
  `_working_functions` repeat `_changed_python_units`' reading, decoding and unreadable-file handling, because that
  walker yields only top-level functions and methods of top-level classes, the units mutmut names, while the check
  judges every function that no other function holds (`_scored_functions`). The two copies of the except clause
  have to be kept alike, as a coding cookie naming an odd codec has shown twice. Closes with `_changed_python_units`
  taking the walker as a parameter, or handing its callers the parsed trees.
- **A changed `.py` symlink is read through to its target by the other Python checks.** `_changed_python_units`
  opens every changed `.py` path, and `open()` follows a symlink, so a change that adds `x.py` as a symlink to a FIFO
  blocks the surviving-mutants and flaky-tests selection, and one to `/dev/zero` reads without end. The
  cognitive-complexity check skips such paths (`_is_link_or_special`, review round 5 of ticket 06, R5-F1). Found by
  reading the code, not reproduced. Closes with `_changed_python_units` skipping what `_is_link_or_special` names.
- **A function moved out of a Python script the change deletes is judged as new.** The cognitive-complexity check
  reads the base of a changed file whose old name has no `.py` only when the file is still Python now
  (`_base_scores`: `new in working`), so the functions of a deleted `bin/tool` lend no score, and one moved unchanged
  from it into `lib.py` is reported as new. Closes with the base read whenever its first line is a `#!` naming
  python, as `_is_python_source` asks of the working tree.
- **A deep decorator can crash test-integrity and surviving-mutants.** `_changed_python_units` writes each
  decorator of a changed function with `ast.unparse(d)`, which recurses as deep as the expression goes, so a function
  decorated with a deep expression, such as a sum of a few thousand terms, can raise `RecursionError` out of the
  check, which GateCheck says must not raise; an int of more than 4300 digits in one raises `ValueError` the same
  way. Raised by review round 5 of ticket 06 (R5-F5), confirmed at the node only, not end to end; the code is
  surviving-mutants'. `_code` already survives both for findings' messages. Closes with the decorators compared by
  `_body_fingerprint`-like fingerprints rather than their text, or the unparse guarded as `_code` guards it.
- **Cognitive complexity scores direct recursion only.** Campbell's paper gives a point to each function in a
  recursion cycle; `_calls_itself` sees a function calling itself by name, or a method as `self.m`, `cls.m` or
  `C.m`, but not a cycle through other functions, nor a call through an alias. A function that recurses through a
  helper scores a point less than the paper says. Closes with a call graph of the module, scored per cycle.
- **A copy of a function whose original the change edits keeps the old score and passes.** The cognitive-complexity
  check pairs identical bodies before names (`_base_scores`), so that a function renamed unchanged keeps its score
  and a new function under its old name is new. A change that edits `big` in place and adds `copied`, a copy of
  `big`'s old body, has the same shape: `copied` takes `big`'s score and passes, and the edited `big` is judged as
  new, so nothing is reported when the edit takes `big` below the limit. Decided by the user in review round 4 of
  ticket 06. Closes with a way to tell a copy from a rename, such as git's copy detection (`git diff -C`) on the
  change's files.
