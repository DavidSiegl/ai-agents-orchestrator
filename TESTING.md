# Testing beyond the unit tests

This is a plan, not a test suite: it lists the testing methods that would catch bugs the unit tests in
`tests/test_orchestrator.py` miss, and the order to adopt them in. Nothing here is implemented yet. Each
method is separate work, picked up after this document has been reviewed. The snippets are sketches to show
the shape of a test. They have not been committed or wired into anything.

## Where the tests stand

- `uv run pytest` runs 121 tests in about 0.2 s. They are `unittest.TestCase` classes run by pytest.
- Most of them drive `Workflow` (`orchestrator.py:526`) through fakes: `FakeHost`
  (`tests/test_orchestrator.py:25`), `FakeHerdr` (`tests/test_orchestrator.py:70`) and `FakeClock`
  (`tests/test_orchestrator.py:167`), assembled by `make_workflow` (`tests/test_orchestrator.py:183`).
  The fakes make the suite fast, but they are also where it is blind: a bug in how the code uses git, the
  shell or herdr is invisible as long as the fake agrees with the code.
- Only two tests touch the real filesystem through the real `Host`: `test_real_filesystem_roundtrip`
  (`tests/test_orchestrator.py:658`) and `TestAtomicWrite` (`tests/test_orchestrator.py:1315`).
- Coverage, statements and branches together, is 94% for `orchestrator.py`.

**Constraint: the orchestrator stays stdlib-only.** It runs with a system `python3` 3.13+ without uv
(`pyproject.toml` says so beside `dependencies = []`). Every tool proposed below goes in the `dev`
dependency group of `pyproject.toml`, next to `pytest` and `pytest-cov`, and nothing is imported by
`orchestrator.py` itself.

Each entry below has the same parts: **Catches** (what kind of bug it would find in this project),
**Targets**, **Tool**, **Run**, **Cost** and **Sketch**.

## 1. Property-based tests (Hypothesis)

**Catches.** Edge cases in the pure functions that turn free text into names, titles and verdicts. The
unit tests check one or two hand-picked inputs each. Hypothesis generates hundreds and shrinks a failure
to the smallest input that causes it. Two things were found while preparing this document, so the first
run would surface them:

- `RunState.from_dict` raises a bare `KeyError`, not `OrchestratorError`, for a saved `spec` agent
  without a `pane` key (`orchestrator.py:439`).
- The round trip is not exact when `root_pane` is empty and a `spec` agent exists, because `from_dict`
  fills `root_pane` in on purpose (`orchestrator.py:438`). The property has to generate a non-empty
  `root_pane`, or state that exception.

**Targets and properties.**

- `branch_name` (`orchestrator.py:484`): for any title and any run id shaped like `new_run_id`'s
  (`orchestrator.py:462`), the result starts with `orchestrator/` (`BRANCH_PREFIX`, `orchestrator.py:47`)
  and ends with the run key. The slug between them is at most 40 characters of `[a-z0-9-]`, with no
  leading or trailing `-`. `git check-ref-format --branch` accepts the whole name.
- `parse_verdict` (`orchestrator.py:466`) and `spec_title` (`orchestrator.py:475`): they never raise, and
  only the first non-blank line decides. Prepending blank lines or appending any text after that line
  leaves the result unchanged.
- `pr_body` (`orchestrator.py:496`): for any spec, report and review text, the body stays under GitHub's
  65536-character limit. This is what `PR_SECTION_LIMIT` (`orchestrator.py:50`) and `_details`
  (`orchestrator.py:490`) exist for; `test_long_sections_are_truncated`
  (`tests/test_orchestrator.py:526`) checks it for one input only.
- `RunState` (`orchestrator.py:399`): `asdict` → `json.dumps` → `json.loads` → `RunState.from_dict`
  (`orchestrator.py:431`) gives back an equal state, and unknown keys in the saved dict are ignored.
- `run_health` (`orchestrator.py:1074`): a run whose phase is `done` or whose `error` is set gives
  `None`. Without either, `age > STALE_SECONDS` (`orchestrator.py:44`) gives `STALE`, whatever the owner
  and the pid say.
- `resume_command` (`orchestrator.py:1040`): `shlex.split` of the command gives back
  `["orchestrator.py", "resume", <key>, *target]` for any target arguments, quotes and spaces included.

Later candidates: `format_age` (`orchestrator.py:1062`) is monotonic, and `find_run`
(`orchestrator.py:1114`) returns the full-id match over a key match.

**Tool.** Hypothesis; adds `hypothesis` to the `dev` group.

**Run.** `uv run pytest tests/test_properties.py`, and as part of `uv run pytest`.

**Cost.** Setup is half a day for the properties above. The suite takes about 2–3 s longer at
Hypothesis's default of 100 examples per property; the `git check-ref-format` property spawns one process
per example and is the slowest.

**Sketch.**

```python
import json
import re
import shlex
import subprocess
import unittest
from dataclasses import asdict

from hypothesis import given, strategies as st

from orchestrator import (BRANCH_PREFIX, STALE, STALE_SECONDS, RunState, branch_name, parse_verdict,
                          pr_body, resume_command, run_health, spec_title)

keys = st.text("0123456789abcdef", min_size=6, max_size=6)
run_ids = keys.map(lambda k: f"20261003-105143-{k}")  # the shape new_run_id makes
one_line = st.text().filter(lambda s: s.strip() and len(s.splitlines()) == 1)


class TestProperties(unittest.TestCase):
    @given(st.text(), run_ids)
    def test_branch_name(self, title, run_id):
        name = branch_name(title, run_id)
        key = run_id.rsplit("-", 1)[-1]
        self.assertTrue(name.startswith(BRANCH_PREFIX) and name.endswith(key))
        slug = name.removeprefix(BRANCH_PREFIX).removesuffix(key).removesuffix("-")
        self.assertRegex(slug, r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?)?$")
        self.assertLessEqual(len(slug), 40)
        subprocess.run(["git", "check-ref-format", "--branch", name], check=True, capture_output=True)

    @given(one_line, st.text())
    def test_first_non_blank_line_decides(self, first, rest):
        for parse in (parse_verdict, spec_title):
            self.assertEqual(parse(f"\n \n{first}\n{rest}"), parse(first))

    @given(st.text(), st.text(), st.text(), run_ids, st.integers(0, 99),
           st.sampled_from(["APPROVE", "CHANGES_REQUESTED"]))
    def test_pr_body_fits_github(self, spec, report, review, run_id, rnd, verdict):
        state = RunState(run_id, "task", "/proj", round=rnd, verdict=verdict)
        self.assertLess(len(pr_body(state, spec, report, review)), 65536)

    @given(st.builds(RunState, run_id=run_ids, task=st.text(), cwd=st.text(), root_pane=st.text(min_size=1),
                     agents=st.dictionaries(st.sampled_from(["spec", "build", "review"]),
                                            st.fixed_dictionaries({"name": st.text(), "pane": st.text()}))))
    def test_run_state_round_trips(self, state):
        saved = json.loads(json.dumps(asdict(state)))
        self.assertEqual(RunState.from_dict({**saved, "field_from_a_later_version": 1}), state)

    @given(st.sampled_from(["spec", "build", "review", "publish", "done"]), st.one_of(st.none(), st.text()),
           st.integers(0, 10**6))
    def test_run_health(self, phase, error, age):
        health = run_health({"phase": phase, "error": error}, age, "here", lambda pid: True)
        if phase == "done" or error:
            self.assertIsNone(health)
        elif age > STALE_SECONDS:
            self.assertEqual(health[0], STALE)

    @given(run_ids, st.lists(st.text()))
    def test_resume_command_round_trips(self, run_id, target):
        self.assertEqual(shlex.split(resume_command(run_id, target)),
                         ["orchestrator.py", "resume", run_id.rsplit("-", 1)[-1], *target])
```

## 2. Stateful crash-and-resume tests

**Catches.** A resume that does a step twice or skips one. Resuming is the orchestrator's hardest promise:
a run can stop after any side effect, and `Workflow.run` (`orchestrator.py:566`) must pick it up from
`state.json` without committing twice, opening a second pull request or prompting a role for a file it
already wrote. The resume tests in `TestResume` and `TestResumePullRequest`
(`tests/test_orchestrator.py:975`) each start from one hand-built saved state; nothing checks the states
in between.

A deterministic version of this test, run while preparing this document, already found one gap. The
orchestrator saves `pr_url` only after `create_pr` returns (`orchestrator.py:741`, then `_save` at
`orchestrator.py:743`). A crash between the two makes the resume open a second pull request; on GitHub,
`gh` refuses a second pull request for the same head, so the resumed run fails instead. Fixing that (for
example by asking `gh` for an open pull request on the branch first) is separate work.

**Targets.** `Workflow.run` and every step that saves state or has a side effect: `_claim`
(`orchestrator.py:608`), `_switch_to_branch` (`orchestrator.py:679`), `_turn` and the `prompted` marker
(`orchestrator.py:767`, `orchestrator.py:799`), `_publish` (`orchestrator.py:720`), `_save`
(`orchestrator.py:938`) and `_heartbeat` (`orchestrator.py:956`).

**Invariants**, checked after the run finally completes:

- it never commits twice: at most one `commit` in `FakeHost.git_calls`;
- it never opens a second pull request: `len(host.prs) <= 1`;
- the final verdict equals the verdict of the same script run without interruption;
- `round <= max_rounds` (the state machine below checks it after every rule, not only at the end).

**How.** The entry reuses the fakes in `tests/test_orchestrator.py`: `FakeHost`, `FakeHerdr`, `FakeClock`,
`spec_turn`, `build_turn` and `review_turn`. A crash is an exception class derived from `BaseException`, so
the `except OrchestratorError` and `except KeyboardInterrupt` clauses in `Workflow.run` do not see it and
nothing is released or saved, as with `SIGKILL`. Wrappers around `FakeHost.write`, `FakeHost.git`,
`FakeHost.create_pr` and `FakeHerdr.prompt` count side effects and raise it after the k-th one. The resume
loads `state.json` from `host.files` with `RunState.from_dict` and runs a new `Workflow` on the same fakes.
Two gaps in the fakes need handling, in the test or in the fakes themselves:

- `FakeHost.git_head` returns a fixed `head` (`tests/test_orchestrator.py:49`), and a commit does not move
  it (`tests/test_orchestrator.py:61`). A resume after a commit then fails with "the Builder changed no
  files". The test's `commit` has to move `head`, as `test_publish_resumed_after_the_commit_does_not_commit_again`
  (`tests/test_orchestrator.py:1010`) does by hand.
- A crash between delivering a prompt and saving `prompted` costs one duplicate prompt, by design. The
  scripted turns must therefore follow `state.round` instead of being popped once per round.

There are two levels, the cheaper one first:

- **Crash after step k, for every k.** Run once uninterrupted to count the side effects (44 for a
  two-round run that ends in a pull request), then once per k with a crash after step k followed by a
  resume. It is deterministic, has no dependency, and is exhaustive for one crash.
- **Hypothesis `RuleBasedStateMachine`.** Rules crash at a drawn step, resume, let another orchestrator
  take the run over (which must end in `RunTakenOver`), or make a role exit. The script draws each round's
  verdict and `max_rounds`. Invariants are checked after every rule. This covers several crashes in one
  run, and crashes during a resume.

**Tool.** None for the crash-after-k loop. The state machine uses Hypothesis, the same `dev` dependency as
entry 1.

**Run.** `uv run pytest tests/test_crash_resume.py`

**Cost.** One day for the crash-after-k loop, including the two fake changes; it runs in well under a
second (about 50 short runs on fakes). Two more days for the state machine, which adds a few seconds at a
fixed example budget.

**Sketch** of the crash-after-k loop:

```python
import json
import unittest

from orchestrator import APPROVE, CHANGES_REQUESTED, RunState, Workflow
from test_orchestrator import FakeClock, FakeHerdr, FakeHost, build_turn, review_turn, spec_turn

RUN_ID = "20260929-120000-a1b2c3"


class Crash(BaseException):
    """Like SIGKILL: no except clause in Workflow.run sees it, so nothing is released or saved."""


# The turns follow the round, so the duplicate prompt a crash can cause rewrites the same file.
def build(prompt, state, host):
    return build_turn(state.round)(prompt, state, host)


def review(prompt, state, host):
    verdict = APPROVE if state.round == 2 else CHANGES_REQUESTED
    return review_turn(state.round, verdict)(prompt, state, host)


def crash_after(k, steps, obj, name):
    real = getattr(obj, name)

    def step(*args, **kw):
        out = real(*args, **kw)
        steps.append(name)
        if len(steps) == k:
            raise Crash
        return out
    setattr(obj, name, step)


def finish(k=None):
    """Run with a crash after side effect k, then resume from state.json until the run completes."""
    host = FakeHost()
    git = host.git

    def git_that_commits(cwd, *args, timeout=60):
        if args[0] == "commit":
            host.head = "c0ffee"  # FakeHost.git leaves HEAD where it was
        return git(cwd, *args, timeout=timeout)
    host.git = git_that_commits
    herdr = FakeHerdr(host, None, {"spec": [spec_turn] * 9, "build": [build] * 9, "review": [review] * 9})
    steps = []
    for obj, name in ((host, "write"), (host, "git"), (host, "create_pr"), (herdr, "prompt")):
        crash_after(k, steps, obj, name)
    path = f"{RunState(RUN_ID, '', '/proj').dir}/state.json"
    while True:
        saved = host.files.get(path)
        state = RunState.from_dict(json.loads(saved)) if saved else RunState(RUN_ID, "add a rate limiter", "/proj")
        herdr.state = state
        clock = FakeClock()
        wf = Workflow(herdr, host, state, notify=lambda title, body: None, max_rounds=3,
                      sleep=clock.sleep, clock=clock, wallclock=clock)
        try:
            wf.run()
            return wf.state, host, len(steps)
        except Crash:
            continue


class TestCrashAndResume(unittest.TestCase):
    def test_every_single_crash_point(self):
        expected, _, total = finish()
        for k in range(1, total + 1):
            with self.subTest(k=k):
                state, host, _ = finish(k)
                self.assertEqual(state.verdict, expected.verdict)
                self.assertLessEqual(state.round, state.max_rounds)
                self.assertLessEqual(sum(c[0] == "commit" for c in host.git_calls), 1)
                self.assertLessEqual(len(host.prs), 1)  # fails today for the crash right after create_pr
```

## 3. Mutation testing

**Catches.** Tests that run code without checking what it does. A mutation tool changes one thing in
`orchestrator.py` at a time (a `<` to `<=`, a `True` to `False`, a dropped call) and runs the tests; a
mutant no test fails on ("survives") marks behaviour no test pins down. The trial run below found such
survivors in `_turn` (`orchestrator.py:767`): `s.prompted = file` replaced by `s.prompted = None`, and the
stall check's `>= STALL_SECONDS` turned into `>`. In `run_health` (`orchestrator.py:1074`),
`age > STALE_SECONDS` turned into `>=` survives too: no test sits on the boundary.

**Targets.** All of `orchestrator.py`, with the most attention on `Workflow` (`orchestrator.py:526`),
`run_health` (`orchestrator.py:1074`) and `resumable_state` (`orchestrator.py:1127`).

**Tool.** mutmut 3 (recommended over cosmic-ray, which needs a separate session database and more setup);
adds `mutmut` to the `dev` group, with this in `pyproject.toml`:

```toml
[tool.mutmut]
source_paths = ["orchestrator.py"]
pytest_add_cli_args_test_selection = ["tests/"]
```

**Run.** `uv run mutmut run`, then `uv run mutmut results` to list the surviving mutants,
`uv run mutmut show <mutant>` to see one as a diff, and `uv run mutmut browse` to step through them.
mutmut works in a `mutants/` directory, which goes into `.gitignore`.

**Cost.** The current suite runs in under a second (121 tests), which is what makes mutation testing
affordable here at all. A trial run of mutmut 3.8 on a scratch copy of the repo made 1,997 mutants and
took 76 s on one machine. 1,384 were killed, 466 survived, 142 hit code no test runs and 5 timed out.
Setup is an hour; working through the survivors the first time is a day or two.

The survivors cluster where the fakes are blind. 47 are in `_publish` (`orchestrator.py:720`): `git add`
renamed to `XXaddXX`, the `cwd` passed to `git` replaced by `None`, the commit's second `-m` dropped. All of
them pass because `FakeHost.git` ignores its `cwd` and the `add` call. Real-git tests (entry 4) kill them.
Most of the rest are in argv strings and help texts: `parse_args` (`orchestrator.py:977`), `main`
(`orchestrator.py:1151`) and the `Herdr` wrappers. mutmut 3 mutates only code inside functions, so the
module-level role prompts are not mutated; golden tests (entry 7) cover those.

**How to act on survivors.** Run it by hand, not in CI: it is too slow to repeat on every build, and its
result is a list to read, not a pass or fail. Before a release or after a large change, go through the
survivors:

- A survivor in logic (a comparison, a condition, a dropped call or argument) is a missing assertion: write
  the test that kills it, then rerun that mutant with `uv run mutmut run <mutant>`.
- A survivor in a log message or a help text is noise: mark the line `# pragma: no mutate`, or leave it.
- A survivor that changes nothing observable (an equivalent mutant) is accepted and left alone.
- "No tests" mutants are uncovered code; they show up in the coverage report too (entry 6).

**Sketch.** None beyond the configuration above: mutation testing needs no test code of its own.

## 4. Real-git integration tests

**Catches.** Bugs in how the orchestrator drives git, which `FakeHost.git` (`tests/test_orchestrator.py:52`)
cannot show. The fake records each call and acts on only a few: it answers `rev-parse --abbrev-ref HEAD`
and `status --porcelain`, and switches its branch name. It ignores `push` and `add` entirely. A `commit`
only clears its list of changed files: it creates no commit and does not move `HEAD`, so nothing checks
what the commit contains. `status --porcelain` lists only untracked files, and nothing checks that
`.orchestrator/` really stays out of the commit or that the push reaches `origin`. A wrong git argument,
an `add` that picks up run files, or a branch that never reaches `origin` would all pass the unit tests.

**Targets.** In `Workflow._switch_to_branch` (`orchestrator.py:679`):

- reading the current branch with `rev-parse --abbrev-ref HEAD` (`orchestrator.py:684`), and skipping the
  switch when the run is already on its branch;
- `_require_clean` (`orchestrator.py:641`) on a tree the human touched during the interview;
- `git switch -c <branch>` (`orchestrator.py:687`) from the commit the run started on, with a name real git
  accepts.

In `Workflow._publish` (`orchestrator.py:720`):

- `status --porcelain` (`orchestrator.py:732`), with the run files hidden by `.orchestrator/.gitignore`,
  which `_claim` writes (`orchestrator.py:608`);
- `add --all` (`orchestrator.py:733`) and `commit` with the spec title and the run line
  (`orchestrator.py:734`): one commit holding exactly the Builder's files;
- the `git_head == base` check (`orchestrator.py:736`) when nothing changed, and on a resume after the
  commit;
- `push --set-upstream origin <branch>` (`orchestrator.py:738`) to a bare `origin`;
- `create_pr` (`orchestrator.py:741`), through `Host.create_pr`'s `cd`-and-`exec` snippet
  (`orchestrator.py:364`), with `gh` stubbed;
- `switch --quiet <base_branch>` back (`orchestrator.py:746`), leaving the tree clean.

**How.** Each test makes a temporary directory with a bare repository as `origin` and a clone with one
commit on `main`. It sets `user.name` and `user.email` in the clone, so the test does not depend on the
machine's git identity. A stub `gh` script on a `PATH` set with `patch.dict(os.environ, ...)` records its
arguments and its stdin and prints a URL. The workflow runs with the real `Host()` and a `FakeHerdr` whose
turns write real files under `state.cwd`, through `make_workflow(..., host=Host(), state=...)`.

**Tool.** None: git is already needed, and the `gh` stub is a shell script. The CI agent needs `git` on
`PATH`, which an agent that checks the repository out with command-line git already has.

**Run.** `uv run pytest tests/test_real_git.py`

**Cost.** One to two days, most of it the fixture. Each test makes a few git processes; about 20 tests add
1–3 s.

**Sketch.**

```python
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import APPROVE, Host, RunState
from test_orchestrator import make_workflow, review_turn, spec_turn, writes

GH_STUB = """#!/bin/sh
printf '%s\\n' "$*" > "$GH_LOG" && cat > "$GH_LOG.body" && echo https://github.com/o/r/pull/1
"""


class TestRealGit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = self.dir = tmp.name
        self.cwd = f"{d}/proj"
        self.git(d, "init", "--quiet", "--bare", "--initial-branch=main", "origin.git")
        self.git(d, "clone", "--quiet", "origin.git", "proj")
        self.git(self.cwd, "config", "user.name", "Test")
        self.git(self.cwd, "config", "user.email", "test@example.com")
        self.git(self.cwd, "commit", "--quiet", "--allow-empty", "-m", "initial")
        self.git(self.cwd, "push", "--quiet", "origin", "main")
        os.mkdir(f"{d}/bin")
        with open(f"{d}/bin/gh", "w") as f:
            f.write(GH_STUB)
        os.chmod(f"{d}/bin/gh", 0o755)
        env = patch.dict(os.environ, {"PATH": f"{d}/bin:{os.environ['PATH']}", "GH_LOG": f"{d}/gh.log"})
        env.start()
        self.addCleanup(env.stop)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True).stdout

    def test_approved_change_is_one_commit_on_origin(self):
        def build(prompt, state, host):
            host.write(f"{state.cwd}/limiter.py", "version 1")
            return writes(lambda s: s.build_path(1), "report 1")(prompt, state, host)

        state = RunState("20260929-120000-a1b2c3", "add a rate limiter", self.cwd, None)
        wf, *_ = make_workflow({"spec": [spec_turn], "build": [build], "review": [review_turn(1, APPROVE)]},
                               host=Host(), state=state)

        self.assertEqual(wf.run(), APPROVE)
        branch = wf.state.branch
        self.assertEqual(self.git(self.cwd, "branch", "--show-current").strip(), "main")
        self.assertEqual(self.git(self.cwd, "status", "--porcelain"), "")
        files = self.git(self.cwd, "show", "--name-only", "--format=", branch).split()
        self.assertEqual(files, ["limiter.py"])  # nothing under .orchestrator/
        self.assertEqual(self.git(f"{self.dir}/origin.git", "rev-parse", branch),
                         self.git(self.cwd, "rev-parse", branch))
```

## 5. Tests of the embedded shell snippets

**Catches.** Quoting and portability bugs in the `sh -c` scripts that `Host` runs, locally or over SSH.
The unit tests in `TestHost` (`tests/test_orchestrator.py:608`) replace `subprocess.run` with a mock, so
these scripts never run there. Three things can go wrong in them:

- **Awkward paths.** Paths with spaces, a leading `-`, a newline, `$`, `*`, quotes or a backslash. A trial
  run while preparing this document ran `write`, `read`, `resolve_dir` and `run_states` on such paths
  under both `dash` and `bash`, and all of them passed. The tests would keep it that way.
- **The environment.** One real finding: with `CDPATH` set, `cd -- "$1"` on a relative path prints the
  directory it found, so `resolve_dir`'s `cd -- "$1" && pwd` (`orchestrator.py:346`) prints it twice, and
  `resolve_dir` returns two lines. Both `dash` and `bash` do this. It matters for a relative `--cwd`.
- **Shells and platforms.** `sh` is `dash` on Debian and `bash` on others. `run_states` falls back to BSD
  `stat -f` (`orchestrator.py:379`). Over SSH the whole command line is `shlex.join`ed
  (`orchestrator.py:312`) and parsed by the remote user's login shell before `sh -c` runs.

**Targets.** In `Host` (`orchestrator.py:301`): `read` (`orchestrator.py:325`), `write`
(`orchestrator.py:335`), `resolve_dir` (`orchestrator.py:341`), `create_pr` (`orchestrator.py:357`),
`run_states` (`orchestrator.py:371`), and the SSH wrapping in `run` (`orchestrator.py:308`). The existing
real-filesystem tests, `test_real_filesystem_roundtrip` (`tests/test_orchestrator.py:658`) and
`TestAtomicWrite` (`tests/test_orchestrator.py:1315`), are the starting point.

**How.**

- Run each snippet through the real `Host` against a temporary directory, once per awkward name.
- Run each one under each shell. Pass `Host(run=...)` a `run` that replaces `argv[0] == "sh"` with `dash`
  or `bash` before calling `subprocess.run`; skip a shell that is not installed.
- Simulate the SSH path by running the string `Host("x").run` would send to `ssh` under `bash -c` and
  `dash -c` locally, which stands in for the remote login shell.
- Run the BSD branch of `run_states` with a `stat` shim on `PATH` that rejects `-c`.
- Lint every snippet. Capture the scripts with a recording `run` (the same way the tests in `TestHost`
  capture the argv) and pipe each into `shellcheck -s sh -S warning -`. The scripts stay in
  `orchestrator.py` as the single source; nothing is copied into a `.sh` file. Today all of them are clean
  at warning level. `write`'s `A && B || C` raises the informational SC2015, which is intended there: the
  cleanup should run when any step fails.

**Tool.** shellcheck through the `shellcheck-py` package, added to the `dev` group, which ships the
binary, so no system package is needed. `dash` and `bash` are system shells.

**Run.** `uv run pytest tests/test_shell.py`

**Cost.** One day. The tests start roughly 100 short shell processes and add about 1 s; shellcheck adds
well under a second.

**Sketch.**

```python
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import RUNS_DIR, Host

AWKWARD = ["with space", "-leading", "new\nline", "dollar$HOME", "star*", "quote'\"", "back\\slash"]


def host_under(shell):
    def run(argv, **kw):
        return subprocess.run([shell, *argv[1:]] if argv[0] == "sh" else argv, **kw)
    return Host(run=run)


class TestShellSnippets(unittest.TestCase):
    def test_awkward_paths_in_each_shell(self):
        for shell in ("dash", "bash"):
            if not shutil.which(shell):
                continue
            host = host_under(shell)
            for name in AWKWARD:
                with self.subTest(shell=shell, name=name), tempfile.TemporaryDirectory() as d:
                    cwd = os.path.join(d, name)
                    os.mkdir(cwd)
                    path = f"{cwd}/{RUNS_DIR}/r-abc/state.json"
                    host.write(path, '{"run_id": "r-abc"}\n')
                    self.assertEqual(host.read(path), '{"run_id": "r-abc"}\n')
                    self.assertEqual(host.resolve_dir(cwd), os.path.realpath(cwd))
                    [(_, state)] = host.run_states(cwd)
                    self.assertEqual(state["run_id"], "r-abc")

    def test_resolve_dir_ignores_cdpath(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(f"{d}/sub")
            with patch.dict(os.environ, {"CDPATH": d}):
                self.assertEqual(Host().resolve_dir("sub"), f"{os.path.realpath(d)}/sub")  # fails today
```

## 6. Branch coverage with a fail-under threshold

**Catches.** New code that lands with no test, and branches no test takes, such as the error path of an
`if`. The Jenkins Test stage already measures statement coverage for SonarQube, but nothing fails a build
locally or in the Test stage when coverage drops. Today's report, run with `--cov-branch`, shows 760
statements with 45 missed and 224 branches with 13 partly taken: 94% together.

**Targets.** The uncovered code today: the error paths of `Herdr.call`, `Herdr._exec` and
`Herdr.ssh_target` (`orchestrator.py:160`, `orchestrator.py:174`, `orchestrator.py:185`), `Host.run`'s
`OSError` and timeout path (`orchestrator.py:308`), `notify_locally` (`orchestrator.py:1044`) and
`pid_alive` (`orchestrator.py:1052`). The threshold then guards all of `orchestrator.py`.

**Tool.** None: `pytest-cov` is already in the `dev` group. Configuration in `pyproject.toml`:

```toml
[tool.coverage.run]
branch = true
source = ["orchestrator"]

[tool.coverage.report]
fail_under = 92
show_missing = true
```

The threshold starts two points below today's figure and is raised as entries 1–5 add tests. Measure
`orchestrator` only: the Jenkinsfile's `--cov=.` also counts the test files, which inflates the figure.

**Run.** `uv run pytest --cov=orchestrator --cov-branch --cov-report=term-missing`

**Cost.** An hour. Coverage adds about 0.2 s to the suite.

**Sketch.** Configuration only, shown above: there is no test code to write for this method.

## 7. Golden tests for the role prompts and the PR body

**Catches.** Unintended changes to the text the roles and the human read. The prompts are the contract
between roles: `REVIEW_PROMPT` (`orchestrator.py:115`) tells the Reviewer to put
`VERDICT: APPROVE` on the first line, which `parse_verdict` (`orchestrator.py:466`) depends on. An edit that
drops "in a single write" or garbles the verdict line passes every unit test today: they check only
fragments, such as "git diff abc123" in `test_review_prompt_names_the_base_commit` or "You are the
Builder". The tests that reach round 2 do format `FIX_PROMPT` (`orchestrator.py:109`), `RECHECK_PROMPT`
(`orchestrator.py:128`), `REBUILD_NOTE` (`orchestrator.py:140`) and `REREVIEW_NOTE` (`orchestrator.py:144`),
so a broken placeholder raises there, but a prompt that names the wrong file (say the previous round's
report instead of this round's) passes. The mutation trial (entry 3) confirms it: filling `REVIEW_PROMPT`'s
`approve` with `None`, so the Reviewer is told to write `VERDICT: None`, or the Builder's `report_path`
with `None`, leaves the whole suite green.

**Targets.** `SPEC_PROMPT` (`orchestrator.py:76`), `BUILD_PROMPT` (`orchestrator.py:96`), `FIX_PROMPT`,
`REVIEW_PROMPT`, `RECHECK_PROMPT`, `CONTINUE_PROMPT` (`orchestrator.py:135`), `REBUILD_NOTE` and
`REREVIEW_NOTE`, rendered the way the workflow renders them: through `_build_prompts`
(`orchestrator.py:710`) and `_review_prompts` (`orchestrator.py:755`) for rounds 1 and 2, fresh and
continuing sessions. Also `pr_body` (`orchestrator.py:496`) for an approved run and a draft.

**How.** Each rendering is compared with a file under `tests/golden/`. Running with `UPDATE_GOLDEN=1`
rewrites the files, and the diff of the golden files then shows in review exactly how the prompts
changed. Next to the snapshot, one contract assertion: the verdict line the Reviewer is told to write
parses, `parse_verdict(f"VERDICT: {APPROVE}") == APPROVE`.

**Tool.** None: plain files and `assertEqual`. (syrupy would do the same as a pytest plugin, but adds a
dependency for little gain.)

**Run.** `uv run pytest tests/test_golden.py`; after an intended change,
`UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and review the diff.

**Cost.** Half a day. Runtime is negligible. Every intended prompt change now also updates a golden file,
which is the point.

**Sketch.**

```python
import os
import unittest

from orchestrator import APPROVE, parse_verdict, pr_body
from test_orchestrator import make_workflow, saved_run

GOLDEN = os.path.join(os.path.dirname(__file__), "golden")


class TestGolden(unittest.TestCase):
    def check(self, name, text):
        path = os.path.join(GOLDEN, name)
        if os.environ.get("UPDATE_GOLDEN"):
            with open(path, "w") as f:
                f.write(text)
        with open(path) as f:
            self.assertEqual(f.read(), text)

    def test_prompts(self):
        wf, *_ = make_workflow({}, state=saved_run("build", 1))
        for n in (1, 2):
            text, fresh = wf._build_prompts(n)
            self.check(f"build-{n}.txt", f"{text}\n---\n{fresh}")
            text, fresh = wf._review_prompts(n)
            self.check(f"review-{n}.txt", f"{text}\n---\n{fresh}")

    def test_pr_body(self):
        state = saved_run("publish", 2, verdict=APPROVE)
        self.check("pr-body.md", pr_body(state, "# Title\n\n## Goal\nx", "report", "VERDICT: APPROVE"))

    def test_review_prompt_asks_for_a_verdict_that_parses(self):
        self.assertEqual(parse_verdict(f"VERDICT: {APPROVE}"), APPROVE)
```

## 8. Static analysis (mypy, ruff)

**Catches.** `None` reaching code that expects a string, and plain mistakes such as unused names. The code
is fully annotated, so a type checker costs little to add. mypy on `orchestrator.py` today reports 6
errors, all the same kind:

- `run` returns `s.verdict`, typed `str | None`, as `str` (`orchestrator.py:570`, `orchestrator.py:606`).
- `_publish` passes `s.branch` and `s.base_branch`, typed `str | None`, to `git` and `create_pr`
  (`orchestrator.py:738`, `orchestrator.py:741`, `orchestrator.py:746`).

These hold by an invariant the types do not state: a run with `pull_request` set has a branch by the time
it publishes. If a hand-edited or old `state.json` broke it, `None` would reach `subprocess.run` as a
`TypeError`. That is not an `OrchestratorError`, so `Workflow.run` would not record the failure, and the
run would look alive until it went stale. Fixing them means a check that raises `OrchestratorError`, or an
`assert` that states the invariant.

ruff with `E, F, W, B, UP` and a 120-column line length reports only 7 over-long lines today; its default
rule set adds about 20 style findings, mostly unused unpacked variables in the tests.

**Targets.** All of `orchestrator.py` for mypy; `orchestrator.py` and `tests/` for ruff.

**Tool.** mypy and ruff; adds `mypy` and `ruff` to the `dev` group. mypy is recommended over pyright,
which needs Node.js; both would find the errors above.

**Run.** `uv run mypy orchestrator.py` and `uv run ruff check`.

**Cost.** Half a day to fix the 6 mypy errors and settle the ruff rules. Each tool takes a few seconds.

**Sketch.** Configuration only:

```toml
[tool.mypy]
files = ["orchestrator.py"]

[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "W", "B", "UP"]
```

## 9. End-to-end with real herdr and a stub `claude`

**Catches.** Drift between the orchestrator and the herdr CLI: a renamed flag, a changed JSON shape, a new
error code. `FakeHerdr` (`tests/test_orchestrator.py:70`) cannot see any of that. The argv tests in
`TestHerdr` pin down what the orchestrator sends, but not that herdr still accepts it.

**Targets.** `Herdr` (`orchestrator.py:153`), above all `start_agent` (`orchestrator.py:210`) and the
statuses `_turn` (`orchestrator.py:767`) polls.

**Verdict: not worth it now.** A stub `claude` would have to pass for Claude Code, and that part is not
under this project's control:

- `herdr agent start` succeeds only when "the expected agent was detected in the same terminal and is
  ready for input" (`herdr agent start --help`, herdr 0.9.1).
- The statuses (`idle`, `working`, `blocked`, `done`) and the session id the orchestrator relies on come
  from herdr's Claude integration hooks. The stub would have to reproduce them, which ties the test to
  herdr internals instead of its CLI.
- It needs a running herdr server and a terminal on the Jenkins agent, and every step waits on real
  polling, so it would be slow and flaky.

Instead, a manual smoke run with the real `claude` before a release, on a throwaway repository, catches
the same drift at a fraction of the cost. Revisit if herdr ships a documented test agent kind.

**Tool.** None: herdr and Claude Code are installed on the machine, not Python dependencies.

**Run.** The manual smoke run, from a herdr pane, sketched below.

**Cost.** For the automated version: several days, and tens of seconds to minutes per run. For the manual
smoke run: about 15 minutes per release.

**Sketch** of the manual smoke run:

```bash
cd "$(mktemp -d)" && git init -q && git commit -q --allow-empty -m initial
python ~/GitHub/ai-agents-orchestrator/orchestrator.py run "add a hello.txt saying hi" --no-pr --max-rounds 1
# Ctrl-C while the Builder works, then:
python ~/GitHub/ai-agents-orchestrator/orchestrator.py list
python ~/GitHub/ai-agents-orchestrator/orchestrator.py resume <key>
```

## CI

The Jenkinsfile's Test stage runs `uv run pytest --cov=. --cov-report=xml:coverage.xml
--junitxml=test-results.xml`. Recommendations:

- **Hypothesis, the stateful tests, the real-git tests and the shell-snippet tests run in that same
  `uv run pytest` stage.** pytest discovers them under `tests/` without a change to the command. Together
  they add a few seconds. They need only what the agent already has (`git`, `sh`) and what `uv sync
  --frozen` installs (Hypothesis, shellcheck-py).
- **Hypothesis is deterministic in CI.** A `tests/conftest.py` registers a `ci` profile, and the
  Jenkinsfile's `environment` block sets `HYPOTHESIS_PROFILE = 'ci'`:

  ```python
  import os
  from hypothesis import settings

  settings.register_profile("ci", max_examples=200, derandomize=True, deadline=None, print_blob=True)
  settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
  ```

  `derandomize` gives the same examples on every build, so a red build is reproducible and never flaky.
  `deadline=None` keeps a slow agent from failing a property on time alone. `print_blob` prints what is
  needed to replay a failure locally. On a laptop the default profile stays random, which is where new
  failures are found. `.hypothesis/` goes into `.gitignore`. CI keeps no example database, since
  `cleanWs()` wipes it.
- **Branch coverage** joins the same command: `--cov=orchestrator --cov-branch`, with `fail_under` from
  `pyproject.toml` failing the stage before SonarQube runs.
- **Static analysis** can be a short `Lint` stage before Test (`uv run ruff check && uv run mypy
  orchestrator.py`) once its 6 errors are fixed.
- **Mutation testing stays out of CI.** It is the documented manual command `uv run mutmut run` (entry 3).
- **The end-to-end smoke run stays manual** (entry 9).

## Adoption order

1. **Property-based tests (Hypothesis).** The cheapest method with real findings: half a day, a few
   seconds of runtime, and it already turns up the `from_dict` `KeyError`. It also brings in the
   dependency entry 2 reuses.
2. **Stateful crash-and-resume tests.** Resume is the feature most likely to go wrong silently, and the
   crash-after-k loop already found the duplicate pull request window. Start with the deterministic loop;
   the state machine follows.
3. **Real-git integration tests.** They close the largest blind spot in the fakes, the publish path that
   ends every run, for one to two days of work and a few seconds per build.
4. **Shell-snippet tests.** These scripts also run on remote machines, so a quoting bug costs a run there.
   One day, and it already found the `CDPATH` bug in `resolve_dir`.
5. **Static analysis.** Half a day to clear 6 mypy errors that hide an unreported failure mode. After that,
   it guards every change for free.
6. **Branch coverage threshold.** An hour of configuration. It stops coverage from sliding but finds
   nothing new, so it comes after the methods that raise coverage.
7. **Golden tests.** Half a day. They protect the prompts, which change rarely and deliberately; their
   main value is a reviewable diff when they do.
8. **Mutation testing.** The most informative audit of the suite, but each run takes over a minute and
   grows with the suite, and the first triage takes a day or two. It pays off most once entries 1–7 exist, so it measures them too.
9. **End-to-end with real herdr.** Several days for a slow, flaky test of an interface owned by herdr. The
   manual smoke run per release covers the same risk.
