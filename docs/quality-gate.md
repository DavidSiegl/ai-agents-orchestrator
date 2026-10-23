# Quality gate

How Jenkins judges this repository with `orchestrator.py gate`, and how `orchestrator.py run --quality-gate` uses
it: the [gate](#the-gate) itself, the [Jenkins setup](#jenkins-setup), the
[credentials and permissions](#credentials-and-permissions) the orchestrator needs, and the
[orchestrator loop](#orchestrator-loop).

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

ruff, mypy and pymarkdownlnt are in uv's `dev` group, so `uv run` puts them on `PATH`; shellcheck and actionlint
come from the Jenkins agent image. The orchestrator itself stays stdlib-only: the gate runs them as processes.

After the linters come the checks `gate_checks` gives, which judge what the change did rather than how the tree
reads. Each is a `GateCheck` that either fails the gate or only reports: a failing check's findings are issues,
like the linters', and a reporting check's are findings to weigh, which the Builder answers and the Reviewer
judges without the gate failing. Either kind is listed wherever it is, not only on added lines, and names the
check that raised it. A check that cannot run says so as one of its findings, so a failing check that cannot
run fails the gate.

Every check sees the change without what the tree tracks under `mutants/`, mutmut's state (`GateChange.from_tree`):
none selects a test, a function or a changed line there, or counts an edit there as a change to the code, and a file
moved out of it counts as added. The linters and the build configuration listing see those files as any other.

| Check | Fails? | Finds |
|---|---|---|
| `test-integrity` | yes | tests the change weakened, in test files (`tests/**`, `test_*.py`, `*_test.py`) |
| `cognitive-complexity` | yes | functions the change edits or adds that it takes over a cognitive complexity of 15 |
| `flaky-tests` | no | tests the change adds or edits that pass in one order and fail in another |
| `surviving-mutants` | no | changes to the functions the change edits that no test notices |

**Test integrity** compares each test file the change touches, outside `mutants/` as above, with its base, so a
change cannot pass by weakening its tests. Each of these is a finding:

- `removed-test`: a test function, method or class is gone, at its line at the base. A class whose tests all
  went is one finding, and so is a deleted file. A test that reappears with the same body under another name or
  in another file was renamed or moved, not removed. A test removed together with the code it tested is still a
  finding: the gate cannot see the spec, so the Builder says why in its report and the Reviewer judges;
- `skip`: a `skip`, `skipIf`, `skipUnless`, `skipTest`, `SkipTest`, `xfail` or `expectedFailure` on an added
  line;
- `fewer-assertions`: a test with fewer `assert` statements, `self.assert*` calls and `pytest.raises` calls than at
  the base;
- `swallowed-error`: a bare `except:`, or an `except Exception:` that does nothing, on an added line;
- `expected-value-changed`: an assertion whose literals changed while the rest of it stayed, in a change that also
  edits code outside the tests: the "update the expected value to match the output" move. The finding names the
  assertion before and after.

It reads Python only; another language is a `WeakenedTestsDetector` beside the Python one in
`WEAKENED_TESTS_DETECTORS`.

**Cognitive complexity** scores the functions the change edits, so that a change cannot pile branches into the
function it is editing rather than split it. A function counts when the change adds a line to it, decorators
included: a top-level function, or a method of a top-level class, in a Python file outside the test files; a function
nested in it is part of it. Its score is G. Ann Campbell's
[cognitive complexity](https://www.sonarsource.com/docs/CognitiveComplexity.pdf), which SonarQube's rule S3776
measured: a point for each `if`, `elif`, `else`, ternary, loop, `except`, `match`, sequence of like boolean operators
and call of a function to itself, and one more for each level of nesting on those that nest. The comment above
`COGNITIVE_COMPLEXITY_LIMIT` in `orchestrator.py` says how each Python construct maps to the paper's rules. Each of
these is a finding:

- `over-limit`: a function over 15, at its `def` line, when it is new or the change raised its score, as
  "`_poll`: cognitive complexity 19, 14 at the base; the limit is 15". Its base is the function of the same name and
  class in the file's base, under the file's old name when the change renamed it. A function already over 15 that the
  change leaves no worse is not a finding, since the gate judges the change;
- `not-scored`: a changed file that could not be read or parsed, or the base of one with a function over 15.

ruff's `C90` stays beside it. It counts branches without their nesting, so it misses a deeply nested function that
cognitive complexity catches, and it reports at the `def` line, which a change that grows an existing function does not
add; but it still catches a flat function of many branches, which cognitive complexity scores lower. A function both
flag is two findings at its `def`.

**Flaky tests** reruns the tests the change adds or edits, so a test that passed the build's one run by luck of
the order is caught. A test counts when the change adds or removes a line of it in a test file outside `mutants/`, as
above, from the line after the statement before it to its last: its decorators and the comments above its `def`
count, since a decorator replaced or removed changes the test, and so do lines removed right below it. pytest
decides which of those are tests. It runs them five times, alone, under pytest-randomly with seeds 1 to 5
(`-p randomly -p no:cacheprovider --randomly-seed=N`), each run in the gate's own interpreter (`python -m pytest`,
so the project's environment that `uv run` gives, whatever `pytest` is on `PATH`) and within 30 seconds. They run in
a temporary copy of the files git tracks, so a report the project's `addopts` write, such as `coverage.xml`, lands in
the copy and the build's own stays as the gate reads it; a test that needs an untracked file, such as a build
product, does not find it there. pytest takes its configuration only from the copy's top directory, in pytest's
order (`pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`, then `pyproject.toml`, `tox.ini` and `setup.cfg`
with a pytest table or section), given with `-c`, or an empty one when it has none; `--confcutdir` is the copy. So
no `pytest.ini` or `conftest.py` that another account put above the temporary directory is read. The top directory
is where a build that runs pytest there takes its configuration. A configuration in a subdirectory, such as
`pkg/pyproject.toml` or `tests/pytest.ini`, is not used: the reruns get the top directory's, or the empty one when
the top has none, and a project that needs the subdirectory's usually gets `not-rerun` (see `TODO.md`):

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
- `cleanup`: the copy mutmut ran in could not be removed afterwards, named so that it can be.

mutmut runs in the gate's own interpreter, on a copy of the files git tracks, so the working tree stays as it was; its
state, `mutants/`, goes into the copy too. Its own selector, mutant names such as
`orchestrator.x_judge_change__mutmut_*`, picks which mutants run only after mutmut has made a mutant of every function,
which takes minutes for `orchestrator.py`. So in the copy, each function and class the change does not touch gets
mutmut's `# pragma: no mutate block` at the end of its header, and mutmut skips it. The rules' `[tool.mutmut]` says
what can be mutated (`source_paths`, `only_mutate`, `do_not_mutate`), which tests run
(`pytest_add_cli_args_test_selection`), what the tests read beside the code (`also_copy`), and which lines are left
alone (`do_not_mutate_patterns`); see [whose rules judge a change](#whose-rules-judge-a-change). Python files are read
as Python reads them, by a byte order mark or a coding cookie. mutmut runs pytest in the copy's `mutants/`, where it
puts the mutated code. Its runs get the configuration flaky-tests' reruns get, with `-c`: the project's own, which
`also_copy` puts in `mutants/` so that paths in it, such as `pythonpath`, name the mutated code, or an empty one; and
`--confcutdir` is `mutants/`. Both are added to the table's `pytest_add_cli_args`, so these runs too read no
`pytest.ini` or `conftest.py` from above the copy. In a repository without mutmut, a change to a function gets a
`not-run` finding.

`--mutation-budget SECONDS`, 600 by default, bounds mutmut's run. Stopping it takes at most 15 seconds more: SIGINT,
10 seconds to exit, a kill of its process group, and 5 seconds to read what it printed. Listing its results and showing
the survivors then take at most 60 seconds, each call's stop included. So the check takes at most the budget and 75
seconds, and the few it takes to copy the tree. With the flaky-tests check's three minutes and the test run, a quality
build stays inside the orchestrator's `QUALITY_TIMEOUT` of 1200 seconds.

This repository's table keeps mutmut off argparse's help texts, which no test reads word for word:
`do_not_mutate_patterns = ['\bhelp=']`. mutmut matches each pattern against each source line and skips every expression
that starts on a matching line, with all it holds, so a help text's later lines are skipped too, and so is a whole
`add_argument(...)` call that starts on its `help=` line. A change to a help text alone makes no mutant.

The gate is `GATE: ERROR` when any of these holds, and `GATE: OK` otherwise:

- a finding on a line the change adds, or a finding of a failing check;
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
`\xNN`. Not every tool can read such a name:

- ruff prints it with U+FFFD for each such byte, which the gate matches to the tracked file, so ruff's findings
  on it are listed as usual.
- mypy stops with `INTERNAL ERROR` (exit status 2) whenever the file is among its `[tool.mypy] files`, and
  pymarkdown stops with `Configuration Error: 'utf-8' codec can't encode character` when it has a finding in such
  a `.md` file. Either is a failed condition quoting the tool's first line, which need not name the file; mypy's
  comes back on every change until the file has a UTF-8 name.
- coverage.py collects no data for such a file, so its added lines count as not coverable.
- `flaky-tests` collects the tests of such a test file, but pytest-randomly stops with an internal error,
  `UnicodeEncodeError`, when it shuffles them, so a change to one of them is a `not-rerun` finding quoting it.

Any other git output the gate cannot decode, such as a work tree whose own path is not UTF-8, leaves the change
unjudged (exit 2).

The quality file starts with the `GATE:` line, then has the failed conditions, the issues (the findings on added
lines, then the failing checks'), the findings to weigh, the failing tests, coverage of new code, the suppressions
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

One `Jenkinsfile` serves two jobs. Every build installs, tests with branch coverage of `orchestrator`, runs the
gate and archives `quality.md`, and builds the Linux binary; a build of `main` that gets that far also releases.

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
- **`gh` on the agent, and a GitHub token** for the Release stage: a Secret text credential with the ID
  `GitHub-Agents`, holding a token with *Contents: read and write* on this repository. It is the agents' own
  `gh` token, so when that is rotated the credential must be too, or every build of `main` fails at Release.
  Only builds of `main` use it.
- **A checkout credential of the same token**: GitHub Branch Source and Git need a Username with password
  credential, so both jobs check out with `GitHub-Agents-scm` (username and the agents' token as password).
  Jenkins also reports each pull request's `continuous-integration/jenkins/pr-head` status with it, so the
  token needs *Commit statuses: read and write* - without it the build passes and the required check waits
  forever, the log saying `Could not update commit status ... 403`. The agents can therefore post that
  status themselves too: the required check shows that a status was set, not that Jenkins set it.
  Keep both credentials in this folder's own store, not the global one: any build can bind any credential it
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
| Quality build (`GIT_REF` set) | `HEAD^`: the snapshot's only parent is the run's base | `main`'s `orchestrator.py` and `pyproject.toml`, from `origin/main` |
| Pull request (`CHANGE_TARGET` set) | the merge base with the target branch, fetched first | its own: a human reviews the pull request, `Jenkinsfile` included |
| `main` | the last successful build's commit, else `HEAD^1` | its own |

The repository is public, so the stage fetches `main` or the target branch without credentials. `GIT_REF` and
the base reach the shell as environment variables, never as Groovy-built shell code, and `GIT_REF` is otherwise
interpolated only into the checkout's branch spec.

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
quotes and `export ` are allowed, so the same file can also be sourced by a shell. A variable set in the
environment wins over the file, and the file is read only when one is missing. The orchestrator refuses a file
that others may read.

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
  └── not OK, quality round < --max-quality-rounds
```

A quality round snapshots the working tree, pushes the snapshot to a throwaway branch on `origin`, has the
quality job judge it, and writes `quality-<n>-<q>.md`. The gate passes when the file says `GATE: OK`. Otherwise
the Builder gets the quality file and answers it in `build-<n>-q<q>.md`, and the next quality round judges that.
After `--max-quality-rounds` rounds (default 3) the change goes to the Reviewer anyway, whose prompt names the
last quality file as unresolved findings. Three rounds are at most three builds and two fix turns, and each
review round has its own budget. Without `--quality-gate`, a run behaves as it did before.

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

**Once a build ends**, live or resumed: with `quality.md`, whatever the result, that file is the round's, with a
line under its `GATE:` line naming the build, the snapshot and the round. Without it and ABORTED, or cancelled in
the queue, nothing judged the change: the run fails with `ci` cut back to `ref` and `sha`, and the Builder gets no
fix turn. Without it otherwise: `GATE: BUILD_FAILED`, with the failing tests or the console's end, and a fix turn.
A build cut back that way is recorded in `ci.rejected`, so a resume builds the snapshot again instead of adopting
that build.

**When Jenkins fails**, rather than the change: a request that got no answer, or a 5xx, is retried every
`CI_POLL_SECONDS = 10` until `QUALITY_TIMEOUT = 1200` seconds after the round started; the run then fails, naming
the step and keeping `ci`, and a resume gets a fresh timeout. Any other answer, such as 401, 403 or 404, fails the
run at once, naming the step, method, URL and status. A 404 on the trigger means the job was deleted. The poll
loops call `_heartbeat`, so a long build does not make the run look stale.

### What goes to the Builder

`QUALITY_FIX_PROMPT` names the quality file and the Builder's next report, `build-<n>-q<q>.md`, whose findings it
answers by number. A Builder in a fresh session gets `BUILD_PROMPT`, `REBUILD_NOTE` with every earlier report,
and `QUALITY_FIX_PROMPT`. The Reviewer's prompt ends with `QUALITY_PASSED_NOTE` or `QUALITY_UNRESOLVED_NOTE`,
naming the round's last quality file, and points at the Builder's last report of the round. The pull request's
description includes the last quality file.

### Run state and resume

`state.json` holds `quality_job` (None for a run without the gate, as for one saved before it),
`max_quality_rounds`, `quality_round` and `ci`, `{ref, sha, queue_url, build}`, saved key by key, with `rejected`
as above. Phase `build` with q = 0 goes to `quality` q = 1; `quality` q goes to `review` on `OK` or at q ≥
`max_quality_rounds`, else to the fix turn, `build` with the same q, then `quality` q + 1; `CHANGES_REQUESTED`
goes to `build`, round n + 1, q = 0. `list` shows the phase `quality` and, in `build` and `quality`, the quality
round.

A resume in `quality` takes the furthest step the state allows: an existing quality file (one without a valid
`GATE:` line is refused, as an empty handoff file is); `ci.build`; `ci.queue_url` (when Jenkins has forgotten the
queue item, the build with that `GIT_REF` among the job's last 20); `ci.ref` alone, where a build with that
`GIT_REF` is adopted before a new one is triggered; otherwise snapshot, push and trigger. A state saved while
quality rounds went through SonarQube loads too: its project and baseline are dropped, its `ci.ce_task` is
ignored, and its build, which archived no `quality.md`, counts as one that ended before the gate.

### CLI

`run` takes `--quality-gate JOB` and `--max-quality-rounds N`; `resume` takes `--max-quality-rounds N`.
`--max-quality-rounds` below 1 is refused, and so is it on `run` without `--quality-gate` or on `resume` of a run
without the gate. `resume` refuses a limit below `quality_round` in phase `quality`, or up to it in `build` with
q ≥ 1, where the Builder is answering quality round q. Turning the gate on or off in the middle of a run is
not supported; see the [roadmap](roadmap.md).

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
