# TODO

What is known to be open in `orchestrator.py`, and why. An entry leaves `Open issues` only for `Fixed`, with what
was done.

## Open issues

- **The macOS builds are first tried on a published release.** `.github/workflows/macos-binary.yml` runs only on
  `release: published` or by hand for an existing tag, so a change that breaks the macOS binary or
  `packaging/build-desktop.sh` on macOS merges green, and the release then lacks those assets until it is fixed and
  the workflow is run again for the tag. Found adding the desktop app to releases (2026-10-24), whose `.dmg` had
  never been built before the first release with it. Closes with a macOS build of pull requests that touch
  `packaging/` or `ui/`, or a decision that a rerun after the release is enough.
- **The frontend's tests fail on Node 25 and later.** Node's own `localStorage` global, which is undefined without
  `--localstorage-file`, hides jsdom's, so `ui/src/test/setup.ts`'s `localStorage.clear()` throws and every test
  fails. Jenkins's Node 20 is unaffected; locally `NODE_OPTIONS=--no-experimental-webstorage npm test` passes.
  Found on Node 26 (2026-10-24). Closes with tests that pass on both, e.g. jsdom's storage installed in
  `setup.ts`, or `engines` in `ui/package.json` that rules the newer Node out.

- **A pull request opened over a conflict stays a draft with its warning after the conflict is resolved.** When
  `Host.merge_upstream` finds conflicts with the base branch, `pr_body` opens the body with a `[!WARNING]` naming
  the conflicting files and `create_pr` opens it as a draft; nothing ever edits the body or marks it ready, and
  `RunState.conflicts` keeps the files. So once someone merges the base in and pushes, the pull request still says
  it conflicts and stays a draft until a human edits it by hand. Met on PR #61 (2026-10-24): resolved and pushed,
  it still showed the warning and the draft. Closes when the orchestrator, or a command of its, notices that the
  head merges cleanly and drops the warning and the conflict-only draft.
- **A proof that failed is not run again in the next round.** `Workflow._judge_proofs` runs only the proofs the
  verdict step wrote in its own round, so a Builder told to make round N's failing proofs pass (`PROOFS_NOTE`) is
  checked only if the Reviewer writes them again; one that still fails can be approved. Raised by the code review of
  loop-guards (2026-10-24). Closes with round N's demonstrated proofs run again after the Builder's turn of round
  N+1 and their outcomes put before the Reviewer.
- **Proofs run after the last round's review too.** No Builder turn follows to read them; only the draft pull
  request's review shows them, and each may take `PROOF_TIMEOUT`. Kept: a demonstrated defect is what the human
  most needs to see in a draft. Raised by the same review.
- **A proof in a worktree run finds the checkout's pytest configuration.** The run directory, and with it
  `RunState.proofs_dir`, is under the project checkout, not the run's worktree, so `adversarial.toml`'s
  `python -m pytest {proofs}` takes its rootdir and `conftest.py` from the checkout while importing the code from the
  worktree it runs in. Found while writing loop-guards ticket 05; not reproduced. Closes with `_run_proof` passing
  the working tree as pytest's rootdir and conftest root, or the proofs copied into a scratch directory of the
  worktree for their run.
- **`test-integrity` does not see an expected value held outside the assertion.** `expected-value-changed` compares
  each assertion's literals (`_changed_literals`), so editing a golden constant the assertion names, such as the
  tests' `GOLDEN_PR_BODY` or `GOLDEN_SUMMARY`, changes what is expected without a finding, while the same edit inside
  the assertion is one. Found while adding the provenance trailers to the pull request body (loop-guards ticket 03).
  Closes with the assertions' names resolved to module-level constants the change edits.
- **A run that never reaches `done` keeps its local refs.** A locking step's commit stays under
  `refs/orchestrator/<key>/locked/` (`RunState.lock_ref`) until the run ends `done` (`Workflow._clean_up_locks`);
  `close` and `prune` do not delete them, so a failed run's locked snapshots are never collected by `git gc`. Closes
  with `close` and `prune` deleting `RunState.refs_prefix` as they delete the run's branch.
- **An agent's pull request can reach `main` on rules it wrote itself, with no human.** Jenkins's App reports
  `continuous-integration/jenkins/pr-head`, which the agents' token cannot post, but a pull request build runs
  the pull request's own `Jenkinsfile` and gate (`docs/quality-gate.md`, What the Gate stage judges), and since
  `automerge.yml` a pull request into `main` from a branch outside `orchestrator/` merges once that status is
  green. Raised by the cross-review of `pr-automerge` (F5, 2026-10-24): "A branch the agents push under any other
  name, once a pull request is open on it, merges into `main` (and so into a release) without a human". Closes
  with an approving review required on `main` that the agents cannot give (Homelab `.scratch/agent-identity`,
  ticket 03), or a Jenkinsfile and gate a pull request build takes from `main` rather than from the pull request.
- **A pull request moved off `main` can merge unchecked before `automerge.yml` turns auto-merge off.** No ruleset
  requires a check outside `main`, so once a pull request with auto-merge on targets another branch and has no
  conflict, GitHub merges it at once, racing the run that would turn auto-merge off; and a conflict cleared from
  the base side starts no run. Raised by the cross-review of `pr-automerge` (R3-1(b), 2026-10-24): "After the push
  that resolves the conflict, the PR is CLEAN against a base with no required check, so GitHub merges it at once
  and unchecked", "In (b) that run still races GitHub's immediate merge. A conflict cleared by a change to the base
  rather than the head raises no pull-request event at all." Closes with a ruleset that requires the check on
  every branch, or one that restricts merges outside `main`.
- **The secret scan does not look inside a file git takes for binary.** With no attributes, git still calls a file
  binary when its first 8000 bytes hold a NUL, and `git log -p` prints "Binary files ... differ" for it, to gitleaks
  and to `Host._allow_comments` alike, so a secret or a `gitleaks:allow` in such a file is pushed unscanned. gitleaks'
  own `git` mode has the same blind spot. Raised by the cross-review of `push-secret-scan` (F15, 2026-10-24). Closes
  with a decision in the spec whether binary content is in scope, and, if it is, a `--text` diff or a scan of the
  blobs themselves (`gitleaks dir` over the pushed files).
- **A base `.gitleaks.toml` that extends another file reads that file from outside the base.** `scan_unpublished`
  writes the base's `.gitleaks.toml` into the run directory and passes it with `--config`; an `[extend] path = ...`
  in it is resolved by gitleaks against its working directory, not against the base, so a file the change can edit
  may supply rules. Raised by the cross-review of `push-secret-scan` (F6, 2026-10-24): "a base config using
  `[extend] path = ...` resolves that path outside base (process cwd), which the change may control". Closes by
  writing each extended file from the base beside the config and rewriting the path, or refusing such a config.
- **A process a test leaves holding pytest's output keeps a finished run waiting until its timeout.** `_pytest`'s
  `communicate` reads to the end of pytest's output, so a server a test started that inherits it keeps a run that has
  finished going until the timeout, reported then as `budget-reached` (passes-at-base) or `not-rerun` (flaky-tests).
  It was so before `_pytest` ran pytest in its own session. Raised by the cross-review of `passes-at-base` (round 2,
  2026-10-24). Closes when `_pytest` waits for pytest itself, then kills its group and reads what is buffered.
- **`_pytest` and `_run_mutmut` copy one stop-and-kill sequence.** Both start their tool in a session of its own,
  kill its process group on a timeout and in `finally`, and read what is left with `_abandon`; the copies had already
  drifted in which errors `killpg` may raise. Raised by the same review. Closes with one helper that runs a command in
  its own session and stops it, which both use, mutmut's SIGINT grace an option of it.
- **`vague-names` does not split CamelCase.** `vague_names` matches a whole name or its last `_`-separated word,
  so `user_data` is found but a class `InfoManager` or `UserData` is not. Closes, if the run metrics show such names
  recur, with the last CamelCase word matched too.
- **Nine test classes copy the same git-repository fixture.** `TestFlakyTests`, `TestPassesAtBase`, `TestVagueNames`,
  `TestTooComplexFunctions` and five more each define a `setUp` that makes a repository, configures it and commits a
  base, most with the same `git` and `put` helpers; eight of the copies bind the vague `value` that `vague-names` now
  reports on added lines, and `TestVagueNames`' binds `setting`. Raised again by the cross-review of `written-rules`
  (F8, 2026-10-24). Closes with one mixin or base class those tests share.
- **`vague-names` reports a name an undecorated override cannot change.** A `def handle(self)` in a
  `socketserver.BaseRequestHandler` subclass, or a parameter the overridden signature names, has no fix; only a
  method marked `@override` is skipped (`_bound_names`). Raised by the cross-review of `written-rules` (F6,
  2026-10-24): "names fixed by a parent class or framework are reported, though they cannot be renamed". Closes, if
  the run metrics show it is common, with methods whose class has a base from outside the repository left out.
- **`passes-at-base` reads every changed test as passing in a project installed into the gate's interpreter.**
  `_base_run_findings` runs pytest in the gate's interpreter, so the base's code is whatever the copy's
  configuration puts on `sys.path` (`pythonpath = ["."]` here). An editable install, as a `src/` layout under
  `uv sync` has, resolves the package to the working tree first, so the base run tests the change and every changed
  test is reported. This repository is not installed (no `[build-system]`), so it is unaffected. Closes when the run
  at the base puts the copy's own package directories before site-packages, or names such a project as one it cannot
  judge.
- **A test file that cannot be imported at the base hides all its changed tests from `passes-at-base`.** A
  module-level import of something the change adds makes pytest's collection of the whole file fail at the base,
  which `_collect_at_base` takes as the expected outcome for every changed test in it, including ones that would pass
  there. Accepted: the check reports only, and splitting the file's tests from its imports would mean rewriting
  them. Closes, if its run metrics show these files are common, by importing such names inside the tests that use
  them, or by running each test with the failing import stubbed.
- **A `conftest.py` that fails at the base other than by `ImportError` makes `passes-at-base` not-run.** One that
  reaches a name the change adds by attribute (`import calc; calc.TWO`, an `AttributeError`) stops pytest with exit 4
  as an `ImportError` does, but `_conftest_import_failed` accepts only `ImportError` and `ModuleNotFoundError`, so
  the whole check is `not-run`, where a test file failing the same way gives nothing. Loud, not wrong. Raised by the
  cross-review of `passes-at-base` (round 3, 2026-10-24). Closes by telling a conftest.py the change edits from one
  it does not, and taking any failure of the first as the expected outcome.
- **The fallback collection runs one pytest per changed test file.** When a `conftest.py` cannot be imported at the
  base, `_collect_at_base` collects each changed file on its own, each paying pytest's start-up from the shared
  budget, and a failing root `conftest.py` makes every file repeat the failure. Raised by the same review. Closes by
  reading the failing conftest's path from pytest's output and leaving out only the files under it, in one more run.
- **A submodule is empty in `passes-at-base`'s copy of the base.** `git archive` leaves a submodule's directory
  empty, so a changed test that imports from one fails to import at the base and gives nothing, though it might pass
  there; the check misses it. Raised by the cross-review of `passes-at-base` (R1-5, 2026-10-24): "`git archive` omits
  submodule contents, so a test that imports from a submodule fails import at the base and is silently 'nothing'".
  Closes when `_copy_base_with_tests` checks out each submodule at the commit the base records for it.
- **Three gate checks repeat the make-a-scratch-directory, run, remove-it skeleton.** `flaky_tests`,
  `_mutation_test` and `passes_at_base` each call `tempfile.mkdtemp`, word their own finding when it fails, and word
  another when `shutil.rmtree` fails (`not-removed` twice, `cleanup` once). The cross-review of `passes-at-base`
  (R1-8, 2026-10-24) raised it as "a third copy of the scratch-directory skeleton ... with reworded messages". Closes
  with one helper that takes the check's rule and what it was doing, and the three findings worded one way, with the
  flaky-tests and surviving-mutants tests that quote today's wording.
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
- **The reruns' time limit is too short for slow tests on the Jenkins agent.** `FLAKY_RUN_TIMEOUT` is 30 s a run. On
  `flaky-tests`' own change each run took about 9 s on an idle laptop and about twice that under load. Measured on
  Jenkins on 2026-10-24 (PR #50, surviving-mutants' change): the first rerun did not finish within 30 s, so the check
  reported `not-rerun` and judged no test; the tests that change edits start mutmut, and rerunning their four test
  classes once takes about 50 s on the laptop. A limit that covers them (about 90 s, five seeds) would push the quality
  build past `QUALITY_TIMEOUT` beside the mutation budget, so the user kept 30 s (2026-10-24). `not-rerun` is a finding
  to weigh, never a wrong verdict. Closes with one time budget the reruns and mutmut share, or lighter tests for the
  gate's own checks.
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
- **The mutation budget leaves most of a large change's mutants untried.** `MUTATION_BUDGET` is 600 s. Measured on
  Jenkins on 2026-10-24: on PR #50 (surviving-mutants' change) mutmut tried 756 of 3079 mutants and the Gate stage took
  683 s, the build about 850 s; on PR #51, 677 of 801 in a 638 s Gate stage. That fits `QUALITY_TIMEOUT` (1200 s) with
  about 350 s to spare, but not after a wait in Jenkins' queue, which counts against a quality round (PR #52 waited
  about 860 s behind the others). The user kept 600 s (2026-10-24): a stopped run says `budget-reached`, never "none
  survived". Closes with the shared time budget above, or mutants chosen by the changed lines rather than whole
  functions.
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
  unfiltered lines feed the suppressions list, the stray-line-end refusal and the coverage condition; coverage counts
  such files only when the project's coverage measures them, since `new_coverage` reads only the files the Cobertura
  report lists. Whether the linters should read them, leave them out, or the gate refuse them is open. Closes with that
  decision.
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
- **A deep decorator can crash flaky-tests and surviving-mutants.** `_changed_python_units`, which both select
  through (`_changed_python_tests`, `_changed_python_functions`), writes each decorator of a changed function with
  `ast.unparse(d)`, which recurses as deep as the expression goes, so a function decorated with a deep expression,
  such as a sum of a few thousand terms, can raise `RecursionError` out of the check, which GateCheck says must not
  raise; an int of more than 4300 digits in one raises `ValueError` the same way. Raised by review round 5 of ticket
  06 (R5-F5), its check named right in round 6 (R6-F3); confirmed at the node only, not end to end; the code is
  surviving-mutants'. `_code` already survives both for findings' messages. Closes with the decorators compared by
  `_body_fingerprint`-like fingerprints rather than their text, or the unparse guarded as `_code` guards it.
- **A Python script with no `.py` is never judged by the cognitive-complexity check.** `_judged_files` judges only
  paths ending in `.py`, while the pairing reads a script whose `#!` line names python as Python
  (`_is_python_source`), so a function moved unchanged into `bin/tool` keeps its base, but one the change grows
  there, or writes new over the limit, is not a finding. `docs/quality-gate.md` says "a `.py` file". Raised by the
  spec-conformance pass of review round 6 of ticket 06; ticket 06's "the Python functions the change touches" can be
  read either way. Closes with `_judged_files` taking `_is_python_source`, or a decision that `.py` is the scope.
- **Cognitive complexity scores direct recursion only.** Campbell's paper gives a point to each function in a
  recursion cycle; `_calls_itself` sees a function calling itself by name, or a method as `self.m`, `cls.m` or
  `C.m`, but not a cycle through other functions, nor a call through an alias. A function that recurses through a
  helper scores a point less than the paper says. Closes with a call graph of the module, scored per cycle.
- **A body moved into a file hides a function with that body moved out of it, which is then judged as new.** The
  cognitive-complexity check takes a base function as gone from its file when the file now holds its body fewer times
  than the base did (`_BasePool._gone`), without asking where the working tree's copies came from. So when `calc.py`'s
  `big` moves unchanged to `lib.py` and `other.py`'s `dup`, with the same body, moves unchanged into `calc.py`, `dup`
  takes `big`'s base in `calc.py` and `big` is "a new function": two unchanged moves fail the gate (review 01 round 7,
  R7-F1, a regression from the round-6 rule; deferred by the user, 2026-10-24, as rare: it needs two over-limit
  functions with one body in two of the change's files). Closes with a count that pairs each working copy with the
  base it came from, so an arrival does not make up for a departure.
- **A copy of a function whose original the change edits keeps the old score and passes.** The cognitive-complexity
  check pairs identical bodies before names (`_base_scores`), so that a function renamed unchanged keeps its score
  and a new function under its old name is new. A change that edits `big` in place and adds `copied`, a copy of
  `big`'s old body, has the same shape: `copied` takes `big`'s score and passes, and the edited `big` is judged as
  new, so nothing is reported when the edit takes `big` below the limit. The same holds for a copy in a file the
  check does not judge, such as one under `tests/`, since a function moved unchanged there must keep its base from
  a new function under its old name (review round 6, R6-F1). Decided by the user in review round 4 of ticket 06.
  Closes with a way to tell a copy from a rename, such as git's copy detection (`git diff -C`) on the change's
  files.
