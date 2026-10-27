# Quality gate

How Jenkins judges this repository with `orchestrator.py gate`, and how `orchestrator.py run --quality-gate` uses
it: the [gate](#the-gate) itself, the [Jenkins setup](#jenkins-setup), the
[credentials and permissions](#credentials-and-permissions) the orchestrator needs, the
[orchestrator loop](#orchestrator-loop), and the [orchestrator's own checks](#the-orchestrators-checks), which
need the run's spec.

## The gate

`orchestrator.py gate --base REV` judges the change from `REV` to the working tree and writes the quality file:

```bash
uv run pytest --cov=orchestrator --cov-branch --cov-report=xml --junitxml=test-results.xml
uv run python orchestrator.py gate --base main --tests test-results.xml --coverage coverage.xml --out quality.md
```

It runs five linters over the files git tracks, each over the files it reads, and keeps only the findings on
lines the change adds, and the lineless ones on files it touches. Findings on older lines are counted, never
listed and never held against the change, so the gate judges the change rather than the codebase.

| Linter | Files | Rules |
|---|---|---|
| ruff | `*.py` | `[tool.ruff.lint]` in `pyproject.toml` |
| mypy | `[tool.mypy] files`, `orchestrator.py` | `[tool.mypy]` |
| pymarkdown | `*.md` | `[tool.pymarkdown]` |
| shellcheck | `*.sh` | its defaults |
| actionlint | `.github/workflows/*.yml` | its defaults |

ruff's rules include the coding rules the agents are given as prose that a linter can apply, so that every harness
gets them, every time: a docstring on every public module, class, method and function (`D100`-`D104`, `D106`,
presence only, since the repository's docstrings are prose rather than a convention; `tests/**` is exempt). A blind
`except Exception` (`BLE001`) is in ruff 0.17's default rules, which `extend-select` adds to and a `select` would
drop; an `except` that does nothing (`S110`, `S112`) and `subprocess.run` without `check` (`PLW1510`) were already
selected. `D105` and `D107` are not, since they fire on nearly all of `main`: its one magic method, and 8 of its 10
`__init__`s, each documented by its class's docstring. Which rule to mechanize next is a question for the run
metrics: the findings that recur.

ruff, mypy and pymarkdownlnt are in uv's `dev` group, so `uv run` puts them on `PATH`; shellcheck and actionlint
come from the Jenkins agent image. The orchestrator itself stays stdlib-only: the gate runs them as processes.

After the linters come the checks `gate_checks` gives, which judge what the change did rather than how the tree
reads. Each is a `GateCheck` that either fails the gate or only reports: a failing check's findings are issues,
like the linters', listed as `error`, and a reporting check's are findings to weigh, listed as `warning`, which the
Builder answers and the Reviewer judges without the gate failing. A failing check may name rules it weighs instead
(`GateCheck.weighed`), as `test-integrity` does for what a spec can ask for. Either kind is listed wherever it is,
not only on added lines, and names the check that raised it. A check that cannot run says so as one of its
findings, so a failing check that cannot run fails the gate.

Every check sees the change without what the tree tracks under `mutants/`, mutmut's state (`GateChange.from_tree`):
none selects a test, a function or a changed line there, or counts an edit there as a change to the code, and a file
moved out of it counts as added. The linters and the build configuration listing see those files as any other.

| Check | Fails? | Finds |
|---|---|---|
| `test-integrity` | yes, but for removed tests, fewer assertions and changed expected values | tests the change weakened, in test files (`tests/**`, `test_*.py`, `*_test.py`) |
| `cognitive-complexity` | yes | functions the change edits or adds that it takes over a cognitive complexity of 15 |
| `flaky-tests` | no | tests the change adds or edits that pass in one order and fail in another |
| `surviving-mutants` | no | changes to the functions the change edits that no test notices |
| `passes-at-base` | no | tests the change adds or edits that pass without the rest of the change |
| `vague-names` | no | names the change binds that fit anything, such as `data` or `raw_info` |

**Test integrity** compares each test file the change touches, outside `mutants/` as above, with its base, so a
change cannot weaken its tests unseen. Each of these is a finding:

- `removed-test`: a test function, method or class is gone, at its line at the base. A class whose tests all
  went is one finding, and so is a deleted file. A test that reappears with the same body under another name or
  in another file was renamed or moved, not removed. A test removed together with the code it tested is still a
  finding. It is one to weigh: the gate cannot see the spec, so the Builder says why in its report and the
  Reviewer judges;
- `skip`: a `skip`, `skipIf`, `skipUnless`, `skipTest`, `SkipTest`, `xfail` or `expectedFailure` on an added
  line;
- `fewer-assertions`: a test with fewer `assert` statements, `self.assert*` calls and `pytest.raises` calls than at
  the base. It is one to weigh, like `removed-test`: a test the spec reshapes may need fewer;
- `swallowed-error`: a bare `except:`, or an `except Exception:` that does nothing, on an added line;
- `expected-value-changed`: an assertion whose literals changed while the rest of it stayed, in a change that also
  edits code outside the tests: the "update the expected value to match the output" move. The finding names the
  assertion before and after. An expected value held in a constant counts too: when a kept test's assertions name a
  constant of its own file at the base and still name it, bare or as `module.NAME` (the module by its dotted name or
  the alias a module-level import gives it), and the constant's expression changed other than in layout, or it is
  no longer a constant, that is one finding per constant and test, at the test's first assertion naming it, naming
  the constant and its line. A name the test binds itself, in its body or a def, lambda or comprehension inside it, is
  not the module's. A constant is a module-level assignment of one name, bound nowhere else in the module's scope, to
  an expression with no call in it. One the change adds, one imported from another module or computed by a call, one
  reached through a local or a helper, an edit to another constant one is built from, and a fixture's return value do
  not count. It is one to weigh, like `removed-test`, since the spec may ask for the new value.

It reads Python only; another language is a `WeakenedTestsDetector` beside the Python one in
`WEAKENED_TESTS_DETECTORS`.

**Cognitive complexity** scores the functions the change edits, so that a change cannot pile branches into the
function it is editing rather than split it. A function counts when the change adds a line to it, decorators
included, in a `.py` file outside `tests/`, which is exempt as it is from ruff's `C901`: any function that no other
function holds, a method of a nested class and a function under a module-level `if` or `try` included. A function
nested in another is part of that one. Files are read as Python reads them, by their byte order mark or coding
cookie. Its score is G. Ann Campbell's [cognitive complexity](https://www.sonarsource.com/docs/CognitiveComplexity.pdf),
which SonarQube's rule S3776 measured: a point for each `if`, `elif`, `else`, ternary, loop, `except`, `match`,
sequence of like boolean operators and recursion, and one more for each level of nesting on those that nest.
Recursion is a function that calls itself, or one on a cycle of calls through other functions of its module, each of
which then scores the point. Calls are followed by name, as Python's scopes resolve them, so a call to a parameter
or a local of the same name is not one to the module's function; not through an alias, nor into another module. The
comment above `COGNITIVE_COMPLEXITY_LIMIT` in `orchestrator.py` says how each Python construct maps to the paper's
rules. Each of these is a finding:

- `over-limit`: a function over 15, at its `def` line, when it is new or the change raised its score, as
  "`_poll`: cognitive complexity 19, 14 at the base; the limit is 15". A function already over 15 that the change
  leaves no worse is not a finding, since the gate judges the change. Its base is a function of the base, and each
  base function is the base of one function at most:
  - first, one with the same body, since an unchanged function is that function whatever its name: of its name in
    its file's base, then of its name in the base of another Python file the change touches, a file it deletes
    included, which counts as Python by its `.py` or its `#!` line at the base, then of any name in its file's base,
    then elsewhere. So a function moved to another module, renamed or made a method, but otherwise
    unchanged, keeps its score, and a new function given its old name is new. Its score itself can rise with the
    move: two functions that call each other from two modules score no recursion, and moved into one they each
    score its point. Of a function moved unchanged and a
    copy of it, the one that kept the name takes it, and a function moved unchanged into a file the check does not
    judge, such as one under `tests/` or a script with no `.py` whose `#!` line names python, takes its own before
    any copy can, whatever the copy's name. A function takes the base of a function in another file only when that
    one's body is gone from it, so an unchanged copy takes nothing from an original still in its place, under its
    name or another; an original edited in place is gone by its body, which is the cost below;
  - failing that, the highest-scoring function of its name left in its file's base, under the file's old name,
    whatever that name, when the change renamed it. So with a name defined more than once, as a property's getter and
    setter or `@singledispatch` registrations named `_` are, adding or removing one never makes a function the change
    left no worse a finding; the cost is that a rise below a higher-scoring namesake goes unseen;
  - failing that, the function is new: a copy whose original is still in place, a function moved and edited, and a
    function whose base is under `tests/`, which was never held to the limit.

  Bodies pairing first has a cost: a copy of a function whose original the change edits keeps the old score and
  passes, as if it were the original renamed and the edited one new, even when the edit takes the original below the
  limit, and wherever the copy is, in a file the check does not judge too. `TODO.md` holds it.
- `not-scored`: a changed file that could not be read or parsed. Like any finding of the check, it fails the gate: a
  check that cannot do its work says so rather than pass. A base that cannot be read or parsed is not a finding: its
  file's functions are judged as new, as `test-integrity` takes a base that does not parse to have no tests.

ruff's `C90` stays beside it. It counts branches without their nesting, so it misses a deeply nested function that
cognitive complexity catches, but it still catches a flat function of many branches, which cognitive complexity scores
lower. It reports at the `def` line, and the gate keeps a linter's finding only on a line the change adds: when a change
grows an existing function, only this check reports it. A function whose `def` line the change adds, such as a new
one, and that both flag, is two findings at its `def`.

**Flaky tests** reruns the tests the change adds or edits, so a test that passed the build's one run by luck of
the order is caught. A test counts when the change adds or removes a line of it in a test file outside `mutants/`, as
above, from the line after the statement before it to its last: its decorators and the comments above its `def`
count, since a decorator replaced or removed changes the test, and so do lines removed right below it. pytest
decides which of those are tests. It runs them five times, alone, under pytest-randomly with seeds 1 to 5
(`-p randomly -p no:cacheprovider --randomly-seed=N`), each run in the gate's own interpreter (`python -m pytest`,
so the project's environment that `uv run` gives, whatever `pytest` is on `PATH`) and within 30 seconds. They run in
a temporary copy of the files git tracks, so a report the project's `addopts` write, such as `coverage.xml`, lands in
the copy and the build's own stays as the gate reads it; a test that needs an untracked file, such as a build
product, does not find it there. Each changed test file takes the configuration pytest would find for it: the first
in its own directory, else in the nearest one above it, up to the copy's top and never above it, in pytest's order
within a directory (`pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`, then `pyproject.toml`, `tox.ini` and
`setup.cfg` with a pytest table or section). The tests are rerun once for each configuration found, given with `-c`
and with `--rootdir` its directory, or with an empty configuration for files under none; pytest runs from the copy's
top, and `--confcutdir` is the copy. So a `tests/pytest.ini` or `pkg/pyproject.toml` configures the tests under it,
and no `pytest.ini` or `conftest.py` that another account put above the temporary directory is read. A group that
cannot run is a finding about its tests alone:

- `mixed-results`: a test that failed under some seeds and passed under the others, at its `def`, naming the seeds
  of each. It depends on the order of the tests, on state another test leaves, or on randomness pytest-randomly
  seeds. An xfail counts as an outcome of its own, so a test that xfails under every seed is consistent, and one
  that xfails under some seeds and passes or xpasses under others is mixed. A run that skips a test, or leaves it
  out of its report (an `addopts` with `--maxfail`, say), neither passed nor failed it;
- `failed-every-run`: a test that failed under every seed that judged it, at least two, in the copy. It is not
  flaky. If the build failed it too, the failing tests list it; if the build passed it, it needs something the copy
  lacks, such as git's metadata, an untracked or ignored file or a build product, and the reruns could not judge it.
  The gate does not compare the two, so the finding says both;
- `not-rerun`: the reruns could not judge the tests: pytest could not start, took longer than 30 seconds, ended in a
  usage or collection error, or collected tests without listing their ids; a changed test file could not be read or
  parsed; or the temporary copy could not be made. After a run that went wrong, the finding says which earlier runs
  were judged, and the reruns stop there. A test that is not mixed and that fewer than two runs passed or failed is
  one of these too, at its `def`, once any run has told something. pytest runs at `--verbosity=-1`, which a `-v`
  or `-q` in the project's `addopts` or `PYTEST_ADDOPTS` does not change, nor its `verbosity_test_cases`;
- `not-removed`: the copy could not be removed afterwards; the reruns' results stand.

A change to a helper or fixture outside the tests' `def`s reruns nothing. The check adds at most six pytest runs of
30 seconds to the gate, the collection included, and only when the change touches a test. pytest-randomly is in the
`dev` group, so every `uv run pytest` shuffles the tests too, printing the seed it used; `-p no:randomly` turns it
off, and `--randomly-seed=N` repeats an order.

**Surviving mutants** mutation-tests the functions the change edits, so that code its tests run without checking what
it does is found; coverage counts such code as tested. A function counts when the change adds a line to it, decorators
included: a top-level function, or a method of a top-level class, in a Python file outside the test files and, as
above, `mutants/`. mutmut, in the `dev` group, makes small changes to each, one at a time - an operator
swapped, a number or a string altered, an argument dropped - and runs each of these mutants against the tests that
call the function:

- `survived`: a function with mutants no test failed on, at its `def`: how many of its mutants survived out of how many
  were tried, and what the first three change, each as a line before and after. No test checks what those lines do;
- `untested`: a changed function that no test calls, at its `def`, with how many mutants it has;
- `budget-reached`: mutmut ran out of time, as "budget reached after N of M mutants", N counting the mutants it tried
  against a test, as the `survived` counts do. What the N showed is listed; the others may survive too. It also names,
  with their files, the changed functions in modules mutmut had not reached when it was stopped: it saves a module's
  mutants, none or more, in `mutants/<path>.meta` once it has made them all, and theirs had none;
- `not-mutated`: a changed function mutmut does not mutate, at its `def`, with the reason: its file is outside
  `[tool.mutmut]`'s `source_paths`, or left out by `only_mutate` or `do_not_mutate`; it is decorated, other than with
  `@staticmethod` or `@classmethod` alone; or it is `__new__`, `__getattribute__` or `__setattr__`. And a function
  mutmut was given but made no mutant of, with the cause where the gate can tell it, a `# pragma: no mutate block` on
  the line its header ends on, the one form of mutmut's pragmas the gate recognises and names as the cause, or
  `do_not_mutate_patterns` matching each statement in it, else every cause there can be: it has nothing mutmut
  mutates, `do_not_mutate_patterns` or `mutate_only_covered_lines` leave its lines out, a `# pragma: no mutate` keeps
  them out, or the gate could not read mutmut's names for its mutants. A block pragma on a line of its own above the
  `def` or atop its body, one on its class, and a `start`/`end` pair around it keep a function out too, and get that
  list;
- `not-run`: mutmut could not start, failed, as it does when a test fails, or left results the gate could not read; the
  rules name no `source_paths`, or give a key the gate reads or writes a value of another shape; the directory to copy
  the tree into could not be made; or a changed file could not be read or parsed;
- `not-removed`: the copy mutmut ran in could not be removed afterwards, named so that it can be; the findings stand.

mutmut runs in the gate's own interpreter, on a copy of the files git tracks, so the working tree stays as it was; its
state, `mutants/`, goes into the copy too. Its own selector, mutant names such as
`orchestrator.x_judge_change__mutmut_*`, picks which mutants run only after mutmut has made a mutant of every function,
which takes minutes for `orchestrator.py`. So in the copy, each function and class the change does not touch gets
mutmut's `# pragma: no mutate block` at the end of its header, and mutmut skips it. The rules' `[tool.mutmut]` says
what can be mutated (`source_paths`, `only_mutate`, `do_not_mutate`), which tests run
(`pytest_add_cli_args_test_selection`), what the tests read beside the code (`also_copy`), and which lines are left
alone (`do_not_mutate_patterns`); see [whose rules judge a change](#whose-rules-judge-a-change). Python files are read
as Python reads them, by a byte order mark or a coding cookie. mutmut runs pytest in the copy's `mutants/`, where it
puts the mutated code. Its runs get, with `-c`, the configuration of the copy's top directory, in the order the
reruns look in a directory: the project's own, which `also_copy` puts in `mutants/` so that paths in it, such as
`pythonpath`, name the mutated code, or an empty one; and
`--confcutdir` is `mutants/`. Both are added to the table's `pytest_add_cli_args`, so these runs too read no
`pytest.ini` or `conftest.py` from above the copy. In a repository without mutmut, a change to a function gets a
`not-run` finding.

`--mutation-budget SECONDS`, 600 by default, bounds mutmut's run. Stopping it takes at most 15 seconds more: SIGINT,
10 seconds to exit, a kill of its process group, and 5 seconds to read what it printed. Listing its results and showing
the survivors then take at most 60 seconds, each call's stop included. So the check takes at most the budget and 75
seconds, and the few it takes to copy the tree. With the flaky-tests check's three minutes, passes-at-base's two and
the test run, a quality build stays inside the orchestrator's `QUALITY_TIMEOUT` of 1200 seconds when Jenkins starts
it without a wait in the queue.

This repository's table keeps mutmut off argparse's help texts, which no test reads word for word:
`do_not_mutate_patterns = ['\bhelp=']`. mutmut matches each pattern against each source line and skips every expression
that starts on a matching line, with all it holds, so a help text's later lines are skipped too, and so is a whole
`add_argument(...)` call that starts on its `help=` line. A change to a help text alone makes no mutant.

**Passes at base** runs the tests the change adds or edits, picked as flaky-tests picks them, against the base's code,
since a test that passes without the change cannot be what shows the change works: coverage counts the lines it runs,
surviving-mutants sees only the functions the change edits, and test integrity only tests getting weaker. For a bug
fix, a test that fails at the base is the proof the bug was reproduced. The check runs only when the change edits
Python source outside the tests: a `.py` file, or a script whose `#!` names python, that is not a test file (as for
test integrity), a `conftest.py` or under a `tests` directory. A change to the tests alone, or to the tests and the
docs, has no "without the change" a test could see. The base is the files git tracked at `--base`, as `git archive`
writes them, with every one of the change's files that belong with the tests (those three kinds) taken from the
working tree, or removed where the change removed it, so the changed tests keep their fixtures and data. A submodule
holds the files of the commit the base records for it, from its own repository in the working tree; one that is not
initialised there, or lacks that commit, is left empty, and a `not-run` finding names it. pytest, in the gate's
interpreter, with the configurations flaky-tests' reruns get, found the same way in the base, once for each, and the
copy's own `TMPDIR`, first collects the changed test files with `--continue-on-collection-errors`, then runs those of
the changed tests it collected, all within the one time limit:

- `passes-at-base`: a test that passed at the base, each of its parameter sets that pytest did not skip included, at
  its `def`. A test that fails or errors there tests the change and gives nothing; so does one pytest does not collect
  there: a test whose file cannot be imported at the base, or that lies under a `conftest.py` that fails to load there
  through a file the change edits, the `conftest.py` itself or a helper it imports, whatever it raises, typically
  because it reaches for what the change adds (such a `conftest.py` hides only the tests under it, and the others are
  collected in one more run), and a `test_*` method of a class pytest does not take for a test class. A refactor that
  keeps behaviour gives these findings rightly: the Builder says it is one, and the Reviewer judges;
- `budget-reached`: `--base-run-budget SECONDS`, 120 by default, shared by the collection and the run, ran out; the
  finding says when, and how many of its test cases the run had run. pytest runs in a session of its own, and is
  killed with every process it started;
- `not-run`: pytest could not start, stopped before it collected (as for a plugin a `conftest.py` names that cannot be
  imported), failed to load a `conftest.py` through no file the change edits, as the base itself fails there, stopped
  with another usage error, or wrote no readable report; the run imported the project's code from the working tree
  (below); a submodule was left empty (above); a changed test file could not be read or parsed; or the copy could not
  be made;
- `not-removed`: the copy could not be removed afterwards; the findings stand.

The base's code is what the copy's import roots, its top and its `src/` when it has one, and its configuration, such
as `[tool.pytest.ini_options] pythonpath`, put on the path. The roots go on `PYTHONPATH`, ahead of site-packages and of
what an editable install puts there, so a `src/` layout installed editable into the gate's interpreter is judged at
its base. A project whose code the run still imports from the working tree cannot be judged: one installed editable
from a directory other than those roots, or through an import hook placed ahead of Python's own path search. The run
writes where each module it imported came from, and one that took a project file of the working tree's, whether for a
changed test, a `conftest.py` or a helper, is one `not-run` finding naming the packages, with no test judged.

**Vague names** lists the names a change binds that tell a reader nothing about what they hold: `data`, `info`,
`value`, `status`, `handle` and `manager` (`VAGUE_NAME_WORDS`), or a name ending in one after an underscore, such as
`raw_info`, in any case. A name counts where a line the change adds binds it: an assignment to a name or to an
attribute of `self` or `cls`, a parameter, a `def` or `class`, a `for` or comprehension target, a `with`'s or an
`except`'s `as`, an import, a `match` pattern's capture, or a type parameter. Each is a `vague-name` finding once per
file, at its first such line; a binding on a line the change did not add is never reported, though an added line
binding the same name is. A function marked `@override` (PEP 698) is skipped, name and parameters, since its base
class fixes them; an override without the mark is reported (see `TODO.md`).
`result` is not on the list, since the rules allow it for what a function returns. A changed file that cannot be read
or parsed is a `not-read` finding.

The gate is `GATE: ERROR` when any of these holds, and `GATE: OK` otherwise:

- a finding on a line the change adds, or a finding of a failing check that it does not weigh;
- a linter that is missing, fails, or exits "found something" with nothing the gate can read. A gate that cannot
  run a check never reports it as passed;
- a file a linter reads, by the pathspecs in the table above, with a character that linter ends a line at and git does
  not, on any line up to the last one the change adds to it, whichever commit put it there. For every linter that is a
  carriage return not followed by a newline; actionlint also ends a line at NEL (U+0085), U+2028 and U+2029, though YAML
  1.2 does not (see the comment on its `GateTool.line_ends`). Each linter declares its own set. ruff, mypy, pymarkdown
  and actionlint number every later line of the file past git's, and shellcheck stops reading there. Either way the gate
  cannot judge their findings below it. The condition names the file, the first such line and the character. A file no
  linter reads, such as a text file with old Mac line ends, is not held to this;
- with `--tests`, a failing test in that JUnit report;
- with `--coverage`, coverage of new code below 80%, once the change has 20 or more new lines to cover. Coverage
  of new code is (covered branch outcomes + covered lines) / (branch outcomes + coverable lines), over the added
  lines the Cobertura report lists; a smaller change is not judged on coverage, since a few lines swing it too far.

Exit status 0 means `GATE: OK`, 1 `GATE: ERROR`, and 2 that the gate could not judge (`REV` names no commit, not a
git tree, no rules file, an unreadable report), in which case it writes nothing.

A change to a file whose contents are not UTF-8, such as a Latin-1 line or a binary file that git diffs as text,
is judged like any other. The gate reads the diff, git's file lists and the linters' output as they were printed,
keeping the bytes it cannot decode, and only a newline ends a line of them. So a carriage return or form feed
inside an added line cannot pass for a diff header that would move the lines after it to another file, and a file
name that holds one keeps its linter findings. In the contents of a file a linter reads, a character that linter
ends a line at is still the failed condition above. A test file that `test-integrity` cannot decode is an
`unreadable` finding of that check, since its tests cannot be compared.

A file whose name is not UTF-8 is read the same way, and the quality file writes each byte it cannot decode as
`\xNN`. By the same convention it writes a character that ends a line in some reader, such as `\r`, `\x85` or
U+2028, as `\x0d`, `\x85` or `\u2028`, wherever it appears in a list item or a name, so that each item stays on
its line and reads the same in every round; `one_line` in `orchestrator.py` is where that convention lives. Not
every tool can read such a name:

- ruff prints it with U+FFFD for each such byte, which the gate matches to the tracked file, so ruff's findings
  on it are listed as usual.
- mypy stops with `INTERNAL ERROR` (exit status 2) whenever the file is among its `[tool.mypy] files`, and
  pymarkdown stops with `Configuration Error: 'utf-8' codec can't encode character` when it has a finding in such
  a `.md` file. Either is a failed condition quoting the tool's first line, which need not name the file; mypy's
  comes back on every change until the file has a UTF-8 name.
- coverage.py collects no data for such a file, so its added lines count as not coverable; the quality file names
  each such `.py` file the change adds lines to under the coverage sentence, as not measured.
- `flaky-tests` collects the tests of such a test file, but pytest-randomly stops with an internal error,
  `UnicodeEncodeError`, when it shuffles them, so a change to one of them is a `not-rerun` finding quoting it.

Any other git output the gate cannot decode, such as a work tree whose own path is not UTF-8, leaves the change
unjudged (exit 2).

The quality file starts with the `GATE:` line, then has the failed conditions, the issues (the findings on added
lines, then the failing checks' that they do not weigh), the findings to weigh, the failing tests, coverage of new code, the suppressions
the change adds, and the build configuration it edits. Issues and findings to weigh are numbered as one list, issues
first, at most 50 in all, then a count:

- **Suppressions added** (`# noqa`, `# type: ignore`, `# shellcheck disable`, `<!-- pyml disable`,
  `# pragma: no cover`, `# pragma: no mutate`) are listed, not failed, so the Reviewer weighs each one. A
  `# pragma: no cover` is listed because it takes lines out of the coverage condition, and a `# pragma: no mutate`
  because it takes them out of the surviving-mutants check.
- **Build configuration**: an edit to `Jenkinsfile`, `pyproject.toml` or `uv.lock` is noted, since each can
  change how the change was judged, and so is each file the change adds or edits under `mutants/`, where mutmut keeps
  its state: the checks leave them out, as [the gate](#the-gate) says, and the surviving-mutants check runs mutmut
  without them, though the linters read them as they read any tracked file.

### Whose rules judge a change

Every linter gets its rules from one `pyproject.toml`: the tree's own, or the one `--config` names. A quality
build passes `main`'s, and runs `main`'s `orchestrator.py`, so a change cannot loosen the rules it is judged by.
shellcheck and actionlint have no table there; they run on their defaults, with `--norc` and an empty config file
so that a `.shellcheckrc` or `.github/actionlint.yaml` added to the tree cannot loosen them either. What a
snapshot still controls is visible in its quality file: tool versions through `uv.lock` (noted under build
configuration), and inline suppressions (listed). shellcheck and actionlint versions live in the agent image,
outside this repository.

The surviving-mutants check takes `[tool.mutmut]` from the same file, so a change cannot tell mutmut to leave its code
alone. In the copy mutmut runs on, that table replaces the tree's own, and only that table: the rest of the tree's
`pyproject.toml`, such as `[tool.pytest.ini_options]`, is how its tests run, which a change may need to alter. Without
`--config`, the tree's table judges it, as the linters' do. What the tree holds under `mutants/`, mutmut's own state
and cache of results, is left out of that copy, so mutmut starts afresh and a change cannot mark its mutants killed;
a change that adds or edits files there is listed under build configuration.

The rule sets were tuned until `main` gave a short list of real findings: ruff's defaults plus bugbear,
simplifications, pyupgrade, bandit, cyclomatic complexity (`C90`, `max-complexity = 15`; nesting is the
`cognitive-complexity` check's) and pylint's errors and warnings, with the fake tokens, paths and fixtures of `tests/`
exempt; mypy at its default strictness on `orchestrator.py`; pymarkdown without the line-length and inline-HTML rules.
Only lines a change adds count, so what `main` already has does not block.

## Jenkins setup

One `Jenkinsfile` serves two jobs. Every build installs, tests with branch coverage of `orchestrator`, checks the
desktop app, runs the gate and archives `quality.md`; a pull request or `main` build then builds the Linux binary
and desktop app, which a quality build skips (the `Jenkinsfile`'s Package stage says why), and a build of `main`
that gets that far also releases. A build of `main` whose tree a pull request build already judged skips the
tests, the desktop app's checks and the gate (see
[A build of main judged at its pull request](#a-build-of-main-judged-at-its-pull-request)).

### What Jenkins needs

- **Plugins**: Pipeline, Git, JUnit, Workspace Cleanup, Credentials Binding and Email Extension, and GitHub Branch
  Source for the multibranch job. Email Extension sends with Jenkins's own SMTP settings.
- **An agent labelled `untrusted`**, which the `Jenkinsfile` asks for. Builds run code the agents wrote, so the
  agent must hold nothing an agent should not have: in the homelab it is a container on the agents' own box,
  restricted to this folder's jobs, while the agents that deploy accept no job from this folder. A label alone
  does not enforce that - a pushed branch's `Jenkinsfile` can ask for any label - so the restriction sits on
  the nodes, with the Job Restrictions plugin.
- **`uv`, `git`, `shellcheck` and `actionlint` on the agent**: the Install stage runs `uv sync --frozen`, and `uv`
  brings the Python of `.python-version`; the gate fails a build that lacks a linter.
- **Node 20.19 or later and npm, Rust 1.90 or later, and Tauri 2's system packages on the agent**, for the UI
  stage's checks of the desktop app and the Package stage's `tauri build` of it. Debian 13's `nodejs` and `npm`
  packages do for the frontend (`ui/.npmrc` makes their npm 9.2 resolve the tree), but its Rust 1.85 is older than
  the shell's locked Tauri crates need; the packages are `libwebkit2gtk-4.1-dev`, `build-essential`, `pkg-config`,
  `libxdo-dev`, `libssl-dev` and `file` ([Tauri's prerequisites](https://v2.tauri.app/start/prerequisites/)). A
  clean `cargo test` takes about 100 s on 2 CPUs; `CARGO_HOME` is kept in the workspace, so nothing is cached
  between builds. Package bundles the `.deb` with `dpkg-deb`, and the `.AppImage` with `linuxdeploy`, which
  `tauri build` downloads from GitHub each build.
- **`gh` on the agent, and a GitHub token** for the Release stage: a Secret text credential with the ID
  `GitHub-Agents`, holding a token with *Contents: read and write* on this repository. It is the agents' own
  `gh` token, so when that is rotated the credential must be too, or every build of `main` fails at Release.
  Only builds of `main` use it, for Release and to read pull request statuses.
- **A GitHub App of Jenkins's own, as the checkout credential**: both jobs check out with `GitHub-Agents-scm`,
  a GitHub Branch Source *GitHub App* credential, and Jenkins reports each pull request's
  `continuous-integration/jenkins/pr-head` status with it. The App needs *Contents: read*, *Pull requests:
  read* and *Commit statuses: read and write* on this repository - without the last the build passes and the
  required check waits forever, the log saying `Could not update commit status ... 403`. Its login,
  `<app-slug>[bot]`, is `PR_STATUS_CREATOR` in the `Jenkinsfile`, and the ruleset on `main` requires the
  `pr-head` check from that App, not from any source. The agents' token cannot post as the App, so a green
  required check, and the status a build of `main` trusts, show that Jenkins set it. With the agents' token as
  the checkout credential instead, as before, the agents could post that status themselves.
  Keep the credentials in this folder's own store, not the global one: any build can bind any credential it
  can see by ID, and a pushed branch's `Jenkinsfile` is a build.

### The two jobs

Both load the `Jenkinsfile` with "Pipeline script from SCM". The Checkout stage needs that: it runs
`checkout scm`, or fetches `GIT_REF` from `scm`'s remote.

- **The ordinary job**, a Multibranch Pipeline, builds `main` and every pull request. Its branch source needs:
  - **Discover branches** set to *Exclude branches that are also filed as PRs*, so an `orchestrator/*` branch
    is built once, as its pull request.
  - **Filter by name (with wildcards)** excluding `orchestrator-ci/*`. The orchestrator pushes its snapshots
    there for the quality job and deletes them afterwards; the multibranch job would otherwise build each one
    as a branch of its own.
- **The quality job**, here `AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality`: a plain Pipeline job that
  loads the `Jenkinsfile` from `main`. It takes any ref as a parameter, keeps quality builds out of the ordinary
  job's history, and a change to the `Jenkinsfile` cannot alter how that same change is judged.

Jenkins learns a Jenkinsfile's `parameters` only by running it, so a new job needs one build, by hand and with an
empty `GIT_REF`, before it accepts `buildWithParameters`.

### What the Gate stage judges

| Build | `--base` | Gate and rules |
|---|---|---|
| Quality build (`GIT_REF` set) | `HEAD^`: the snapshot's only parent is the run's base | `main`'s `orchestrator.py` and `pyproject.toml`, from the `origin/main` commit its description names (below) |
| Pull request (`CHANGE_TARGET` set) | the merge base with the target branch, fetched first | its own, `Jenkinsfile` included (below) |
| `main` | the last successful build's commit, else `HEAD^1` | its own; none when judged at its pull request (below) |

Before Install runs any code of the change, a quality build's Checkout stage sets its build description to
`judged tree=<sha> parent=<sha> main=<sha>`, exactly: full lowercase shas, one space apart. `tree` is the
snapshot's tree, `parent` its only parent, the run's base, and `main` the `origin/main` commit fetched then,
whose gate and rules the Gate stage judges with. `orchestrator.py judged-in-quality` reads it to decide whether a
pull request's head is a tree such a build already judged. A build restarted from a later stage skips Checkout,
records nothing and so is never trusted; its Gate stage refuses to run without the recorded commit.

A pull request is judged by the gate and rules it brings, and most from a branch of this repository merge once
Jenkins's App reports `pr-head` green, with no human in between (`.github/workflows/automerge.yml` says which).
The App's status is honest about the build, but the build ran the pull request's own `Jenkinsfile` and gate. So
whoever can push a branch here, the agents included, can change the rules that branch's merge is judged by, and
an agent's pull request, the orchestrator's own included, can reach `main` without a human. A fork's pull
request still waits for one.

The repository is public, so the stage fetches `main` or the target branch without credentials. `GIT_REF` and
the base reach the shell as environment variables, never as Groovy-built shell code, and `GIT_REF` is otherwise
interpolated only into the checkout's branch spec.

### A build of main judged at its pull request

A merge whose branch was up to date with `main` leaves `main` with the tree its pull request build judged: the
same code, tests, `Jenkinsfile`, gate and rules. The "Judged at PR" stage runs `ci/judged-at-pr.sh`, and when it
finds that build, `main`'s build skips Test and Gate, archives no `quality.md`, and goes on to Package and
Release. The script's header says what counts as judged; in short, a `pr-head` success that Jenkins's App posted
on a commit with `HEAD`'s tree that contains `HEAD^1`, with `HEAD^1` built successfully as `main`. A commit
that was never built as `main`, such as one of several a rebase merge adds, or one after a failed build of
`main`, is judged in full, so the lines of a failed build are never let through by a later skip. So is every
merge whose branch was behind `main`: its merged tree was never built.

A status counts only from Jenkins's own GitHub identity because anything a pull request build writes, an archived
file included, comes from code the agents control: the `Jenkinsfile` and tests of every commit pushed to the
pull request run unreviewed. Only the status Jenkins itself posts for a commit says that this commit's build,
with this commit's `Jenkinsfile`, passed.

### What each kind of build reports

The Test stage does not stop at a failing test, so the gate reports failing tests together with the findings; it
stops only when pytest itself could not run. The gate's exit status then decides:

- **A quality build** ends UNSTABLE on `GATE: ERROR`, SUCCESS on `GATE: OK`. FAILURE means the change never got
  as far as the gate, and its quality file is `GATE: BUILD_FAILED`.
- **A pull request or `main`** fails on `GATE: ERROR`, so a red gate blocks the merge and `main`'s release.
- **Any build** fails when the gate could not judge.

The multibranch job mails a failed build of `main` or a pull request, with the log attached, and the first build
that passes after one. The quality job mails nothing: its result goes to the orchestrator run that asked for it.

A build of `main` that passes then releases the version in `pyproject.toml`: if GitHub has no release
`v<version>` yet, `gh release create` makes one, with the tag at the commit built, notes generated from the pull
requests merged since the last release, and `orchestrator.pyz` with its `.sha256` attached. The `.pyz` is
`orchestrator.py` packed as a zipapp, built and started once with `--help` before the upload. To release, raise
`version` in the pull request; a merge that leaves it alone releases nothing. A release that already exists is
skipped whole, so its files are never replaced.

## Credentials and permissions

The orchestrator talks to Jenkins itself, over HTTP from the machine it runs on, with these credentials:

| Variable | What it holds |
|---|---|
| `JENKINS_URL` | Jenkins's base URL, e.g. `https://jenkins.example/` |
| `JENKINS_USER`, `JENKINS_TOKEN` | the technical user `agents` and an API token of it |

Put them in `~/.config/ai-agents-orchestrator/ci.env` (`$XDG_CONFIG_HOME/ai-agents-orchestrator/ci.env` if that
is set), one `NAME=value` per line, and `chmod 600` it:

```bash
JENKINS_URL=https://jenkins.example/
JENKINS_USER=agents
JENKINS_TOKEN=…
```

The orchestrator reads the file itself, on `run` and `resume`, so no wrapper is needed. Comments, blank lines,
a comment after a value, and an `export` before a name are allowed, so the same file can also be sourced by a
shell, and it means the same to both. A value is quoted, or unquoted without blanks, quotes or shell syntax; a
line that sh would read otherwise is refused, and so is a file with Windows line ends, since sh would keep each
carriage return in its value.
A variable set in the environment wins over the file, and the file is read only when one is missing. The
orchestrator refuses a file that others may read.

Never export them from `~/.bashrc` or a direnv `.envrc`: every agent's shell would have them. Values from the
file stay inside the orchestrator process and are never put into its environment.

Jenkins's user `agents` needs Overall/Read, and Job/Read and Job/Build on the `AI-Agents-Orchestrator` folder.

The token travels only in the `Authorization` header, never in a URL, so no error message, `state.json` or log
can carry it; text copied from Jenkins into a quality file has it replaced with `****`. Every process the
orchestrator starts (herdr, and through `Host` git, ssh and gh) gets its environment without these three
variables, so the agents cannot inherit them.

**Known limitation**: the agents run as the same OS user as the orchestrator, so they can still read `ci.env`.
Stripping the variables from subprocesses prevents inheritance, not access.

## Orchestrator loop

With `--quality-gate JOB`, every Builder turn is followed by a quality round, in round 1 and in each round after
`CHANGES_REQUESTED`: Builder → quality gate → (if it does not pass, back to the Builder) → Reviewer.

```text
build ──▶ quality ──▶ review
  ▲          │
  └── not OK, quality round < --max-quality-rounds, and not stuck
```

A quality round snapshots the working tree, pushes the snapshot to a throwaway branch on `origin`, has the
quality job judge it, and writes `quality-<n>-<q>.md`. The gate passes when the file says `GATE: OK`. Otherwise
the Builder gets the quality file and answers it in `build-<n>-q<q>.md`, and the next quality round judges that.
After `--max-quality-rounds` rounds (default 3) the change goes to the Reviewer anyway, whose prompt names the
last quality file as unresolved findings. Three rounds are at most three builds and two fix turns, and each
review round has its own budget. Without `--quality-gate`, a run behaves as it did before.

The loop also stops early when it is stuck: when quality round Q fails on just what round Q-1 failed on, the
Builder's turn between them fixed none of it, and another turn is unlikely to. `gate_failures` reads what failed
from the quality file: its failed conditions, by their words; its failing tests; and each numbered finding before
the findings to weigh, by `finding_fingerprint`, a hash of its path, rule and message with the digits taken out, so
a finding whose line moved is the same finding. When round Q's failures are not empty and are round Q-1's, no more
and no fewer (`GateFailures.repeats`), the change goes to the Reviewer, whose prompt ends with `QUALITY_STUCK_NOTE`,
and the log says why. A build that failed with nothing listed has no failures to compare, so it is never stuck by
this rule; a coverage condition is, while its percentage stays the same.

### Snapshotting the uncommitted tree

The Builder leaves its work uncommitted, and Jenkins can only fetch commits. `Host.snapshot(cwd, parent,
message)` commits the working tree through a temporary index, in one `sh -c` script: it copies the real index,
runs `git add -A` and `git write-tree` on the copy, and `git commit-tree` with `state.base` as the parent. No ref
moves, and the index, HEAD and `git status` stay as they were. `git add -A` honours `.gitignore`, and
`.orchestrator/` ignores itself, so the snapshot holds what the pull request's commit would. Its only parent
being the base is what lets the Gate stage measure the whole change against `HEAD^`.

The snapshot is pushed with `--force` to `orchestrator-ci/<key>-<n>-q<q>`; `--force` replaces what a failed
attempt left. A finished round deletes its branch, and `done` deletes any the run left.

### The calls

`CI` makes them with `urllib.request` over an injected `urlopen`, so the orchestrator stays stdlib-only and the
tests replace Jenkins. Jenkins takes HTTP Basic `JENKINS_USER:JENKINS_TOKEN` (no crumb with an API token).
Requests time out after `HTTP_TIMEOUT = 30` seconds. The job's full name may include folders: each `/` in it
becomes `/job/` in its URL, so `AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality` is
`<JENKINS_URL>/job/AI-Agents-Orchestrator/job/py-ai-agents-orchestrator-quality/` (`<job>` below).

- **Preflight**, before the interview: the three variables are set, and
  `<job>/api/json?tree=property[parameterDefinitions[name]]` lists `GIT_REF`. The directory must be a git
  repository, and a `--no-pr` run also needs a clean tree and an `origin`.
- **Per quality round**: (1) POST `<job>/buildWithParameters` with `GIT_REF`, the queue item in `Location`; (2)
  poll `<JENKINS_URL>/queue/item/<id>/api/json` for `executable.number`; (3) poll
  `<job>/<number>/api/json?tree=building,result`; (4) `<job>/<number>/artifact/quality.md`. Only for a build
  without one: (5) `<job>/<number>/testReport/api/json`, and (6) the last 60 lines of
  `<job>/<number>/consoleText` when no test failed.

**Once a build ends**, live or resumed: with `quality.md`, whatever the result, that file is the round's, with
[the orchestrator's checks](#the-orchestrators-checks) added and a line under its `GATE:` line naming the build,
the snapshot and the round. Without it and ABORTED, or cancelled in
the queue, nothing judged the change: the run fails with `ci` cut back to `ref` and `sha`, and the Builder gets no
fix turn. Without it otherwise: `GATE: BUILD_FAILED`, with the failing tests or the console's end, and a fix turn.
A build cut back that way is recorded in `ci.rejected`, so a resume builds the snapshot again instead of adopting
that build.

**When Jenkins fails**, rather than the change: a request that got no answer, or a 5xx, is retried every
`CI_POLL_SECONDS = 10` until `QUALITY_TIMEOUT = 1200` seconds after the round started; the run then fails, naming
the step and keeping `ci`, and a resume gets a fresh timeout. Any other answer, such as 401, 403 or 404, fails the
run at once, naming the step, method, URL and status. A 404 on the trigger means the job was deleted. The poll
loops call `_heartbeat`, so a long build does not make the run look stale.

### The orchestrator's checks

Some checks need the run's spec, which lives in the run directory (`.orchestrator/`, ignored, so never in a
snapshot) and so never reaches Jenkins. The orchestrator runs these itself, in each quality round, after it reads
the build's `quality.md` and before it writes `quality-<n>-<q>.md`; a build that ended before its Gate stage gets
none of them. They run only with `--quality-gate`. The spec is the contract step's file, `spec.md` in the default
workflow, or the run's task for a workflow without one. Each check is an `OrchestratorCheck` in
`orchestrator_checks`, and judges the change from the run's base to the round's snapshot:

| Check | Fails? | Finds |
|---|---|---|
| `new-dependencies` | yes | packages the change adds that the spec does not name or PyPI does not know |
| `diff-budget` | no | a change over the workflow's `max_files` or `max_lines` |
| `unexpected-paths` | no | changed paths the spec never mentions |

**New dependencies** reads the project's manifests at the repository's top level only, `pyproject.toml` and the
`uv.lock` beside it, when the change touches either: a manifest anywhere else, such as a test fixture, is not the
project's. It reads them in the snapshot and at the base, both at the top level: a file the change moves there, such
as a fixture's `pyproject.toml`, was not the project's at the base, so what it lists is new. The direct
dependencies are `pyproject.toml`'s `[project] dependencies` and `optional-dependencies` and its
`[dependency-groups]`, the project itself left out, so `all = ["myproj[a,b]"]` adds nothing; the locked packages are
`uv.lock`'s, the project and its workspace members left out. A package is new when the snapshot's files name it and
the base's do not. Names compare as PyPI compares them (PEP 503), and the spec names a package when it holds the name as
a word, in any case, so `pytest_randomly` names `pytest-randomly` and `pytest-randomly` does not name `pytest`. The
spec was agreed with the human, so naming a package there is the approval.

Where a package comes from decides whether PyPI is asked about it. A direct dependency's source is its
`[tool.uv.sources]` entry (`workspace`, `path`, `git`, `url` or `index`), else its PEP 508 URL (`name @ url`), else
the `[[tool.uv.index]]` marked `default = true`, else the registry `uv.lock` has it from; a locked package's is its
`source` in `uv.lock`. Only a package from PyPI's own index (`https://pypi.org/simple`) is looked up, at
`https://pypi.org/pypi/<name>/json`, with a timeout of 15 seconds (`PYPI_TIMEOUT`). A package from another index is
never looked up on pypi.org, so a name someone squatted there cannot lend it pypi.org's metadata: that is the
dependency-confusion case, and the finding names the index instead. Such a package is therefore never `not-on-pypi`;
only the spec rule can fail it.

- `not-in-spec` (fails): a new direct dependency the spec does not name, with where it comes from, unless it is a
  member of the project's workspace (`{ workspace = true }`): the project's own code, not a package from outside;
- `not-on-pypi` (fails): a package from PyPI that PyPI answers 404 for. That is the slopsquatting case: a name an
  agent made up, which anyone could register;
- `unchecked` (fails): the packages from PyPI that PyPI was not asked about, because a request got no usable answer
  (no answer, a timeout, an HTTP error other than 404, an answer that is not PyPI's JSON), or because the quality
  round had too little time left. PyPI is asked no more after the first such request, and none is started with less
  than 60 seconds (`SLOW_STEP_MARGIN`) of the round's `QUALITY_TIMEOUT` left. The run's heartbeat is kept between
  requests, so a long list of new packages cannot make a live run look stale. A package that could not be checked is
  never reported as passed, so the next quality round asks again;
- `unreadable` (fails): a manifest in the change that is not UTF-8 TOML, or not in the shape above. One that cannot
  be read at the base is a finding to weigh instead, since the Builder did not write it, and so is one that cannot be
  read in the change because it could not at the base either. Each manifest is judged by its own two sides, so a
  `pyproject.toml` the change breaks fails even beside a `uv.lock` broken all along. Either way that ecosystem's new
  dependencies are not checked;
- `added` (reports): a new direct dependency the spec names, or a workspace member, with its first release date, its
  latest version and its date, and its license, or the path, repository or URL it comes from. `young` instead when its
  first release is less than 14 days old (`YOUNG_PACKAGE_DAYS`), or it has no release files: few have used it yet.
  `other-index` instead for one from an index other than PyPI's, naming the index, and `unknown-source` for one whose
  `[tool.uv.sources]` entry has a shape this check does not know (none of `workspace`, `path`, `git`, `url` or
  `index`), quoting the entry: where it comes from is not checked, so it is reported rather than passed in silence;
- `pulled-in` (reports): one finding per `uv.lock`, listing with the same facts the packages it alone adds, those the
  new dependencies pull in. The spec cannot be expected to name them;
- `source-changed` (reports): a dependency the project already had, direct or locked, that the change takes from
  another source, naming both: `requests` moved from PyPI to `{ index = "evil" }` is the dependency-confusion move
  without a new name. A new default index moves every dependency without a source of its own.

**Diff budget** counts the files the change touches and the lines it adds and removes, from `git diff --numstat -M`
(`Host.changed_line_counts`); a rename counts as one file, with the lines it changes, and a binary file as a file of
no lines. Over either limit it is one finding about the change as a whole, naming each count over its limit; within
both it reports nothing, so the counts of a change of usual size do not take a number the Builder must answer. The
limits are workflow keys, a whole number of at least 1 each, in a workflow file next to `description`:

```toml
max_files = 15   # the defaults
max_lines = 600
```

They belong to the workflow rather than to the quality-gated step, because the change a quality round judges runs
from the run's base, so it holds every editing step's work, a Test Writer's as well as the Builder's.

**Unexpected paths** is a finding for each path the change touches, or deletes, that the spec mentions neither as a
whole nor by its file name. A rename is judged at its new path only. A path or name counts as mentioned only where it
appears whole in the spec, as a plain substring: naming `docs/` does not cover `docs/x.md`.

The findings go into the quality file as the gate's do, with `{path, line, severity, rule, message}` and the rule
prefixed by the check's name, in one last section, `## The orchestrator's checks`, which holds both the failing ones
and those to weigh. They are numbered on from the build's last number, failing ones first, then the findings to weigh
under the same lead sentence as the gate's. The limit of 50 is the whole file's: they are listed only while the file
holds fewer, and the rest are counted. A check with failing findings is a failed condition, added to the build's, and
turns `GATE: OK` into `GATE: ERROR`. Findings to weigh never change the gate. The Builder answers them all by number,
as it answers the gate's.

### What goes to the Builder

`QUALITY_FIX_PROMPT` names the quality file and the Builder's next report, `build-<n>-q<q>.md`, whose findings it
answers by number. A Builder in a fresh session gets `BUILD_PROMPT`, `REBUILD_NOTE` with every earlier report, and
`QUALITY_FIX_PROMPT`. The Reviewer's prompt ends with `QUALITY_PASSED_NOTE`, `QUALITY_UNRESOLVED_NOTE` or
`QUALITY_STUCK_NOTE`, naming the round's last quality file, and points at the Builder's last report of the round.
The pull request's description includes the last quality file.

A snapshot in which gitleaks, by the base's rules, finds a secret or an added comment telling it to allow a line is not
pushed, and no build is triggered. Its quality file is `GATE: LEAK`, saying why, with the findings numbered under
`## Secrets found`: each one's `file:line`, rule, commit and fingerprint, never the secret, which only gitleaks'
redacted report `leaks-snapshot.json` in the run's directory describes. The Builder gets `QUALITY_FIX_PROMPT` as for
any other gate, and the round counts toward `--max-quality-rounds`; a round that finds the same secrets as the one
before it, as many of each rule in each file, stops the loop early as any repeated failure does. A finding that is not a secret
passes only once the human lists it as `file:rule:line` (the fingerprint without its commit, which is the round's
snapshot) in `.gitleaksignore` on the base branch, and only in a run started after that: a run keeps the rules of
its base. A secret in a commit the snapshot does not add, the run's base or a commit below it that origin lacks,
fails the run instead: those are the human's commits. So does one in the branch at publish.

### Run state and resume

`state.json` holds `quality_job` (None for a run without the gate, as for one saved before it),
`max_quality_rounds`, `quality_round` and `ci`, `{ref, sha, queue_url, build}`, saved key by key, with `rejected`
as above. Phase `build` with q = 0 goes to `quality` q = 1; `quality` q goes to `review` on `OK` or at q ≥
`max_quality_rounds`, else to the fix turn, `build` with the same q, then `quality` q + 1; `CHANGES_REQUESTED`
goes to `build`, round n + 1, q = 0. `list` shows the phase `quality` and, in `build` and `quality`, the quality
round.

A resume in `quality` takes the furthest step the state allows: an existing quality file (one without a valid
`GATE:` line is refused, as an empty handoff file is); `ci.build` (when the job has discarded that build, the run
fails, cut back to `ci.ref`, so the next resume builds it again); `ci.queue_url` (when Jenkins has forgotten the
queue item, the build with that `GIT_REF` among the job's last 20); `ci.ref` alone, where a build with that
`GIT_REF` is adopted before a new one is triggered; otherwise snapshot, push and trigger. A state saved while
quality rounds went through SonarQube loads too: its project and baseline are dropped, its `ci.ce_task` is
ignored, and its build, which archived no `quality.md`, counts as one that ended before the gate.

### CLI

`run` takes `--quality-gate JOB` and `--max-quality-rounds N`; `resume` takes `--max-quality-rounds N`.
`--max-quality-rounds` below 1 is refused, and so is it on `run` without `--quality-gate` or on `resume` of a run
without the gate. `resume` refuses a limit below `quality_round` in phase `quality`, or up to it in `build` with
q ≥ 1, where the Builder is answering quality round q. `resume` also takes `--quality-gate JOB` or
`--no-quality-gate`, below; `--max-quality-rounds` goes with the first and is a usage error with the second.

`judged-in-quality --quality-gate JOB --target BRANCH` exits 0 when a quality build of `JOB` already judged
`HEAD` as a pull request's build into `BRANCH` would: among the builds Jenkins keeps, it ended SUCCESS, and its
description (above) names `HEAD`'s tree, the merge base of `HEAD` and `origin/BRANCH` as its parent, and a `main`
commit at which `Jenkinsfile`, `orchestrator.py`, `pyproject.toml`, `uv.lock` and `.python-version` are as in
`HEAD` and at that merge base. Otherwise, and on any error, such as Jenkins unreachable, an HTTP error or a
commit it cannot fetch, it exits 1 with one line on standard error naming the condition the closest build fails,
or what stopped it asking. It needs the same `JENKINS_*` credentials as `--quality-gate`. No build calls it yet.

### Turning the gate on or off on resume

`turn_quality_gate_on` or `turn_quality_gate_off` applies the flag to the saved state before the run goes on; a refusal exits 1 and leaves
`state.json` as it was.

`resume --quality-gate JOB` saves `JOB` as `quality_job`, after the preflight: in round 0, `_check_gate`, as for
`run --quality-gate`; later, an `origin`, gitleaks, the credentials and `CI.preflight`, before any turn. Where the gate
starts depends on where the run is:

- in round 0, or in `build`: that Builder turn is followed by quality round 1;
- in `review`, with `review-<n>.md` neither written, prompted for, asked again for its verdict nor waiting on a
  BLOCKED answer: phase `quality`, q = 1, so the Builder's report is judged before the Reviewer sees it, and a
  failure goes back to the Builder as usual;
- otherwise, a Reviewer already prompted included: at the Builder's next turn.

It is refused in `publish` or `done`, even with the run's own job, after `--no-quality-gate` (the gate cannot come
back), for a run that has another job (the same job is a no-op), in a workflow without a quality-gated step, and for
a `--no-pr` run in the checkout itself past round 0: its tree was never required to be clean, and a snapshot takes
the whole tree.

`resume --no-quality-gate` deletes the run's `orchestrator-ci/<key>-*` branches on `origin`, a failure only
logged, without cancelling a Jenkins build in progress, and saves `quality_job` null, `quality_gate_disabled` true
and `ci` empty. A run in `quality` goes on at `review`, dropping an analysis that has not finished; a Builder in
its fix turn writes `build-<n>-q<q>.md` and goes straight to `review`, with `quality_round` past the quality round
no build ever judges. The Reviewer reads the Builder's latest report of the round. Its prompt ends with
`QUALITY_PASSED_NOTE`, `QUALITY_UNRESOLVED_NOTE` or `QUALITY_STUCK_NOTE` for the round's last quality file that is
written; with `QUALITY_OFF_NOTE`, naming the round's quality files, when the Builder changed the code after it; and
with nothing when the round has none. The pull request carries that file. When no quality file judges the code it
publishes, because the Builder changed the code after the last one or the gate was turned off before any analysis
finished, it says so in a note and cites the run's last quality file from any round, if there is one, as `Quality
gate (round R, before the last change)`, its `Orchestrator-Gate` trailer ending `, then turned off`. A workflow
without a verdict step ends `FINISHED`. `show` prints the job on a `quality` line, or `off (turned off by resume)`.

### Edge cases

- **Concurrent runs** share only Jenkins's executors; `QUALITY_TIMEOUT` allows for a queue.
- **Two runs in one checkout**: a snapshot takes every change in the tree, so it would take the other run's
  too. A run refuses to start or resume while another run in its checkout is live, so the two never overlap.
  A `run --worktree` run snapshots its own worktree, which holds only its change, so worktree runs go in
  parallel in one checkout, beside each other and beside one run in the checkout itself.
- **No change**: the gate passes, and `_publish` refuses the empty change.
- **Cleanup**: a failed run keeps the branch in flight; `done` deletes the run's `orchestrator-ci/<key>-*`
  branches, and a failed delete is only logged.
- **Reachability under `--machine`**: HTTP runs on the orchestrator's machine and git on the agents', meeting at
  `origin`. If only the agents' machine reaches Jenkins, the calls would need to go through `Host`, with curl
  taking its config on stdin.
