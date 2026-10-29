"""The parity scenarios: each a function that scripts a World and runs the engine in it, registered in SCENARIOS.

A scenario says only what happens: what each role's agent writes when prompted, what Jenkins, PyPI and gh answer,
and which engine commands run, in which order, with which stopped midway. What the engine did in return is the
world's record, compared with the recording by test_parity.py. A new scenario is a function here decorated with
@scenario, recorded once from the Python engine (test_parity.py says how).
"""

import http.client
import json
import signal
import threading
import time
import urllib.parse
from collections.abc import Callable

from world import JOB, ROOT, World

# Every scenario, by name: its function, given a fresh world.
SCENARIOS: dict[str, Callable[[World], None]] = {}


def scenario(fn: Callable[[World], None]) -> Callable[[World], None]:
    """Register fn as the scenario of its name."""
    SCENARIOS[fn.__name__] = fn
    return fn


# -- What the agents write ------------------------------------------------------

SPEC = "# Add a subtract function\n\nAdd `subtract(a, b)` to `src/calc.py`, returning a - b, with a test.\n"
CALC_WITH_SUBTRACT = ('def add(a, b):\n    """The sum of a and b."""\n    return a + b\n\n\n'
                      'def subtract(a, b):\n    """a minus b."""\n    return a - b\n')
TEST_WITH_SUBTRACT = ("from calc import add, subtract\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                      "def test_subtract():\n    assert subtract(3, 2) == 1\n")


def writes(file: str, text: str, *, edit: dict | None = None, hold: str | None = None, **more) -> dict:
    """The action of an agent that makes edit's changes to the working tree, then writes text to file in the run
    directory; with hold, only once released (fakes.perform)."""
    return {"write": {file: text}, "edit": edit or {}, **({"hold": hold} if hold else {}), **more}


def spec_written(**kw) -> dict:
    return writes("spec.md", SPEC, **kw)


def built(n: int = 1, q: int = 0, **kw) -> dict:
    """The Builder's report of round n, answering quality round q when q is set, having added subtract."""
    file = f"build-{n}-q{q}.md" if q else f"build-{n}.md"
    edit = kw.pop("edit", {"src/calc.py": CALC_WITH_SUBTRACT, "tests/test_calc.py": TEST_WITH_SUBTRACT})
    return writes(file, f"Added subtract and its test (round {n}). `pytest` passes.\n", edit=edit, **kw)


def reviewed(n: int, verdict: str = "APPROVE", findings: str = "None.", **kw) -> dict:
    return writes(f"review-{n}.md", f"VERDICT: {verdict}\n\n{findings}\n", **kw)


# -- The default workflow ------------------------------------------------------------


@scenario
def default_approved(w: World) -> None:
    """The default workflow, approved in its first round, ending in a pull request."""
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--", "Add a subtract function")


@scenario
def default_changes_then_approved(w: World) -> None:
    """The Reviewer requests changes in round 1 and approves round 2's fix."""
    w.agent("spec", spec_written())
    w.agent("build", built(1, edit={"src/calc.py": CALC_WITH_SUBTRACT}), built(2))
    w.agent("review", reviewed(1, "CHANGES_REQUESTED", "1. tests/test_calc.py:1: subtract has no test."),
            reviewed(2))
    w.run("run", "--", "Add a subtract function")


@scenario
def default_max_rounds(w: World) -> None:
    """Changes requested in every round until --max-rounds: a draft pull request, exit status 3."""
    w.agent("spec", spec_written())
    w.agent("build", built(1), built(2))
    w.agent("review", *(reviewed(n, "CHANGES_REQUESTED", f"1. src/calc.py:5: still wrong in round {n}.")
                        for n in (1, 2)))
    w.run("run", "--max-rounds", "2", "--", "Add a subtract function")


@scenario
def blocked_build_answered(w: World) -> None:
    """The Builder's first report is BLOCKED: the human is asked, and the Builder's next report ends the turn."""
    w.agent("spec", spec_written())
    w.agent("build", writes("build-1.md", "BLOCKED: Should subtract accept floats too?\n"), built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--", "Add a subtract function")


@scenario
def verdict_retried(w: World) -> None:
    """A review without a VERDICT line is moved aside and asked for once more."""
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", writes("review-1.md", "Looks right to me.\n"), reviewed(1))
    w.run("run", "--", "Add a subtract function")


@scenario
def no_pr_run(w: World) -> None:
    """run --no-pr: the change stays uncommitted in the checkout and the workspace stays open."""
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--no-pr", "--", "Add a subtract function")


@scenario
def agent_exits(w: World) -> None:
    """The Builder exits without writing its report: the run fails, keeping its workspace and branch."""
    w.agent("spec", spec_written())
    w.agent("build", {"exit": True})
    w.run("run", "--", "Add a subtract function")


# -- The other workflows ------------------------------------------------------------

EXAMPLES = f"{ROOT}/examples/workflows"


@scenario
def workflow_quick(w: World) -> None:
    """examples/workflows/quick.toml by --workflow-file: Builder and Reviewer, the task as the contract."""
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--workflow-file", f"{EXAMPLES}/quick.toml", "--", "Add a subtract function")


@scenario
def workflow_spec_build_review(w: World) -> None:
    """examples/workflows/spec-build-review.toml installed as a workflow file and run by --workflow, with
    --role-model and --agent."""
    with open(f"{EXAMPLES}/spec-build-review.toml") as f:
        w.write("config/ai-agents-orchestrator/workflows/sbr.toml", f.read())
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--workflow", "sbr", "--agent", "codex", "--role-model", "review=opus", "--permission-mode",
          "acceptEdits", "--", "Add a subtract function")


TESTS_LOCKED = TEST_WITH_SUBTRACT
TESTS_WEAKENED = "from calc import add, subtract\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def product_team(w: World, engineer_edits: dict) -> None:
    """examples/workflows/product-team.toml, approved in round 1, its Engineer making engineer_edits."""
    w.agent("spec", spec_written())
    w.agent("design", writes("design.md", "# Design\n\nAdd subtract beside add in src/calc.py.\n"))
    w.agent("tests", writes("tests.md", "Acceptance test: tests/test_calc.py::test_subtract.\n",
                            edit={"tests/test_calc.py": TESTS_LOCKED}))
    w.agent("build", built(1, edit=engineer_edits))
    w.agent("review", reviewed(1))
    w.run("run", "--workflow-file", f"{EXAMPLES}/product-team.toml", "--", "Add a subtract function")


@scenario
def workflow_product_team(w: World) -> None:
    """examples/workflows/product-team.toml: five roles, the QA Engineer's tests locked, the Engineer leaving them."""
    product_team(w, {"src/calc.py": CALC_WITH_SUBTRACT})


@scenario
def locks_restored(w: World) -> None:
    """The product team's Engineer weakens the locked acceptance test: it is restored, its diff kept, and the Tech
    Lead told."""
    product_team(w, {"src/calc.py": CALC_WITH_SUBTRACT, "tests/test_calc.py": TESTS_WEAKENED})


PROOF_WORKFLOW = '''description = "Builder <-> Reviewer, the Reviewer's findings backed by proofs"
proof_command = "sh {proofs}"

[roles]
build = "Builder"
review = "Reviewer"

[[steps]]
use = "build"
prompt = "Implement {task} in {cwd}. As your last step, write a report to {build_path}."

[[steps]]
use = "review"
prompt = """Review {change} against the task, {task}. Back each finding with a proof in {proofs_dir}. \\
As your last step, write {review_path}, its first line VERDICT: {approve} or VERDICT: {changes}."""
again = "Review the change again; the Builder's report is {build_path}. Write {review_path} as before."
fresh_note = "You are a fresh session; the earlier reviews are {earlier_review_paths}."
'''
CALC_WRONG = CALC_WITH_SUBTRACT.replace("return a - b", "return b - a")
PROOF = "grep -q 'return a - b' src/calc.py || exit 1\n"


@scenario
def proofs_run(w: World) -> None:
    """A workflow with a proof_command: round 1's review backs its finding with a proof that fails, the proof is run
    again before round 2's review and passes."""
    path = w.write("workflows/proofs.toml", PROOF_WORKFLOW)
    w.agent("build", built(1, edit={"src/calc.py": CALC_WRONG}), built(2))
    review = "VERDICT: CHANGES_REQUESTED\n\n1. src/calc.py:7: the sign is wrong.\n"
    w.agent("review", {"write": {"proofs-1/test_1_sign.sh": PROOF, "review-1.md": review}}, reviewed(2))
    w.run("run", "--workflow-file", path, "--", "Add a subtract function")


# -- The quality gate ----------------------------------------------------------------

GATED = ("run", "--quality-gate", JOB)
QUALITY_FAILED = ("GATE: ERROR\n\n## Failed conditions\n\n- 1 findings of the `ruff` check; each is listed below.\n\n"
                  "## Findings\n\n1. `src/calc.py:7` error ruff F821: undefined name `c`\n")
PYPROJECT_WITH_ATTRS = '[project]\nname = "parity-project"\nversion = "0.1.0"\ndependencies = ["attrs>=25"]\n'
ATTRS = {"info": {"version": "25.3.0", "license_expression": "MIT"},
         "releases": {"15.0.0": [{"upload_time_iso_8601": "2015-01-29T00:00:00Z"}],
                      "25.3.0": [{"upload_time_iso_8601": "2025-03-13T00:00:00Z"}]},
         "urls": [{"upload_time_iso_8601": "2025-03-13T00:00:00Z"}]}


@scenario
def quality_passes(w: World) -> None:
    """--quality-gate: the first quality round passes; the Builder added a dependency the spec names, which the
    orchestrator's checks look up on PyPI."""
    w.jenkins_build("SUCCESS", "GATE: OK\n\nEvery condition holds.\n")
    w.pypi_project("attrs", ATTRS)
    w.agent("spec", writes("spec.md", SPEC + "\nUse attrs for the result type.\n"))
    w.agent("build", built(1, edit={"src/calc.py": CALC_WITH_SUBTRACT, "pyproject.toml": PYPROJECT_WITH_ATTRS}))
    w.agent("review", reviewed(1))
    w.run(*GATED, "--", "Add a subtract function")


@scenario
def quality_fails_then_passes(w: World) -> None:
    """Quality round 1 fails, the Builder answers it, and quality round 2 passes."""
    w.jenkins_build("UNSTABLE", QUALITY_FAILED)
    w.jenkins_build("SUCCESS", "GATE: OK\n")
    w.agent("spec", spec_written())
    w.agent("build", built(1), built(1, 1))
    w.agent("review", reviewed(1))
    w.run(*GATED, "--", "Add a subtract function")


@scenario
def quality_stuck(w: World) -> None:
    """Quality rounds 1 and 2 fail on the same finding: the loop stops and the Reviewer is told it is stuck."""
    w.jenkins_build("UNSTABLE", QUALITY_FAILED)
    w.jenkins_build("UNSTABLE", QUALITY_FAILED)
    w.agent("spec", spec_written())
    w.agent("build", built(1), built(1, 1))
    w.agent("review", reviewed(1, "CHANGES_REQUESTED", "1. src/calc.py:7: the gate still fails."), built(2))
    w.run(*GATED, "--max-rounds", "1", "--", "Add a subtract function")


@scenario
def quality_build_failed(w: World) -> None:
    """The build ends FAILURE before its Gate stage, once with a test report and once without, until
    --max-quality-rounds; the Reviewer gets the change anyway."""
    w.jenkins_build("FAILURE", None, tests=[("tests.test_calc.test_subtract", "AssertionError: 1 != -1")])
    w.jenkins_build("FAILURE", None, console="Started\n+ uv sync --frozen\nerror: lock file out of date\nFinished")
    w.agent("spec", spec_written())
    w.agent("build", built(1), built(1, 1))
    w.agent("review", reviewed(1))
    w.run(*GATED, "--max-quality-rounds", "2", "--", "Add a subtract function")


@scenario
def quality_workflow_without_review(w: World) -> None:
    """A workflow without a verdict step whose last quality round fails: QUALITY_GATE_FAILED, exit status 3."""
    path = w.write("workflows/solo.toml", 'description = "Builder alone"\n\n[roles]\nbuild = "Builder"\n\n'
                   '[[steps]]\nuse = "build"\nprompt = "Implement {task} in {cwd}; report to {build_path}."\n'
                   'again = "Do it again; report to {build_path}."\nfresh_note = "You are a fresh session."\n')
    w.jenkins_build("UNSTABLE", QUALITY_FAILED)
    w.agent("build", built(1))
    w.run(*GATED, "--max-quality-rounds", "1", "--workflow-file", path, "--", "Add a subtract function")


# -- Where the run works -------------------------------------------------------------


@scenario
def worktree_run(w: World) -> None:
    """run --worktree: the run builds in a worktree of its own from origin's main, which is removed once the pull
    request is open; the checkout itself, dirty, is left alone."""
    w.write("project/notes.txt", "the human's own uncommitted work\n")
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--worktree", "--", "Add a subtract function")


@scenario
def machine_run(w: World) -> None:
    """run --machine: herdr is addressed by the machine, and every file and git command goes over ssh, which the
    fake runs here."""
    w.machine("box")
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--machine", "box", "--cwd", w.project, "--", "Add a subtract function")


@scenario
def publish_with_conflict(w: World) -> None:
    """origin's main changes the same lines while the run reviews: publishing finds the conflict, pushes the branch
    without the merge and opens a draft pull request, exit status 4."""
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1, hold="review"))
    run = w.start("run", "--", "Add a subtract function")
    w.wait_reached("review")
    w.push_to_origin({"src/calc.py": 'def add(a, b):\n    """a plus b."""\n    return b + a\n\n\n'
                                     'def subtract(a, b):\n    return -(b - a)\n'}, "Add subtract upstream")
    w.release("review")
    run.wait()


# -- Stopped and resumed ------------------------------------------------------------


def killed_and_resumed(w: World, hold: str, *args: str) -> None:
    """Run the engine with args until it reaches hold, kill it there, then let hold go and resume the run with
    --force, which a run whose orchestrator died a moment ago needs: its heartbeat is still fresh."""
    run = w.start(*args)
    w.wait_reached(hold)
    run.kill()
    w.release(hold)
    w.run("resume", "--force", w.run_key())


@scenario
def kill_in_spec(w: World) -> None:
    """Killed while the Spec Collector interviews; resumed, the interview's file is found written."""
    w.agent("spec", spec_written(hold="spec"))
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    killed_and_resumed(w, "spec", "run", "--", "Add a subtract function")


@scenario
def kill_in_build(w: World) -> None:
    """Killed while the Builder works."""
    w.agent("spec", spec_written())
    w.agent("build", built(1, hold="build"))
    w.agent("review", reviewed(1))
    killed_and_resumed(w, "build", "run", "--", "Add a subtract function")


@scenario
def kill_in_review(w: World) -> None:
    """Killed while the Reviewer works, in round 2."""
    w.agent("spec", spec_written())
    w.agent("build", built(1), built(2))
    w.agent("review", reviewed(1, "CHANGES_REQUESTED", "1. src/calc.py:5: no docstring."), reviewed(2, hold="review"))
    killed_and_resumed(w, "review", "run", "--", "Add a subtract function")


@scenario
def kill_in_quality(w: World) -> None:
    """Killed while Jenkins builds the quality round's snapshot; resumed, the run polls the build it recorded."""
    w.jenkins_build("SUCCESS", "GATE: OK\n", hold="quality")
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    killed_and_resumed(w, "quality", *GATED, "--", "Add a subtract function")


@scenario
def kill_in_publish(w: World) -> None:
    """Killed while gh opens the pull request, after the commit and the push; resumed, publishing finishes."""
    w.hold_pr_create("publish")
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    killed_and_resumed(w, "publish", "run", "--", "Add a subtract function")


def product_team_killed(w: World, hold: str) -> None:
    """examples/workflows/product-team.toml killed in step hold, design or tests, and resumed."""
    w.agent("spec", spec_written())
    w.agent("design", writes("design.md", "# Design\n\nAdd subtract beside add in src/calc.py.\n",
                             hold="design" if hold == "design" else None))
    w.agent("tests", writes("tests.md", "Acceptance test: tests/test_calc.py::test_subtract.\n",
                            edit={"tests/test_calc.py": TESTS_LOCKED}, hold="tests" if hold == "tests" else None))
    w.agent("build", built(1, edit={"src/calc.py": CALC_WITH_SUBTRACT}))
    w.agent("review", reviewed(1))
    killed_and_resumed(w, hold, "run", "--workflow-file", f"{EXAMPLES}/product-team.toml", "--",
                       "Add a subtract function")


@scenario
def kill_in_design(w: World) -> None:
    """The product team's run killed while the Architect works, before the run has a branch."""
    product_team_killed(w, "design")


@scenario
def kill_in_tests(w: World) -> None:
    """The product team's run killed while the QA Engineer writes the locked tests, its first editing step."""
    product_team_killed(w, "tests")


@scenario
def resume_after_agent_exit(w: World) -> None:
    """The Builder exits without its report and the run fails; resume restarts it in its saved session, which it
    is prompted to continue in."""
    w.agent("spec", spec_written())
    w.agent("build", {"exit": True}, built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--", "Add a subtract function")
    w.run("resume", w.run_key())


@scenario
def takeover(w: World) -> None:
    """resume --force while the run's orchestrator is alive: the new one takes the run over, and the old one stops
    at its next save without saving."""
    w.agent("spec", spec_written())
    w.agent("build", built(1, hold="build"))
    # Held, so that the second still owns the run when the first next looks, rather than having finished it.
    w.agent("review", reviewed(1, hold="review"))
    first = w.start("run", "--", "Add a subtract function")
    w.wait_reached("build")
    second = w.start("resume", "--force", w.run_key())
    w.wait_for(lambda: (w.run_state(w.run_key()).get("owner") or {}).get("pid") == second.process.pid,
               "the second orchestrator to claim the run")
    # The second has claimed it; once it waits on the Builder too, let the Builder finish.
    w.wait_for(lambda: any(e.get("by") == "2" and e.get("herdr", [])[:2] == ["agent", "get"]
                           for e in w.ledger.events()), "the second orchestrator to wait on the Builder")
    w.release("build")
    first.wait()
    w.wait_reached("review")
    w.release("review")
    second.wait()


# -- The commands about runs ------------------------------------------------------------


def approved_run(w: World) -> None:
    """A default run approved in round 1, its pull request open."""
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--", "Add a subtract function")


@scenario
def runs_listed_and_shown(w: World) -> None:
    """list, list --json with --pr-state, show and metrics, over a finished run and a failed one."""
    approved_run(w)
    w.agent("spec", {"exit": True})
    w.run("run", "--", "Add a multiply function")
    w.run("list")
    w.set_pr(1, state="MERGED", closedAt="2026-01-02T00:00:00Z")
    w.run("list", "--json", "--pr-state")
    w.run("show", w.run_key(1))
    w.run("show", w.run_key(2))
    w.run("metrics")


@scenario
def branches_pruned(w: World) -> None:
    """prune once the pull request is merged: --dry-run first, then for real, locally and on origin; then a new run's
    sweep, which finds nothing left."""
    approved_run(w)
    w.run("prune")
    w.set_pr(1, state="MERGED", closedAt="2026-01-02T00:00:00Z")
    w.run("prune", "--dry-run")
    w.run("prune")
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--no-pr", "--", "Add a subtract function again")


@scenario
def failed_run_closed(w: World) -> None:
    """close of a failed --worktree run: its workspace closed, its worktree and its branch, holding no commit, deleted;
    then resume refuses it, and a second close says it is closed."""
    w.agent("spec", spec_written())
    w.agent("build", writes("build-1.md", "", edit={"src/calc.py": CALC_WITH_SUBTRACT}, exit=True))
    w.run("run", "--worktree", "--", "Add a subtract function")
    key = w.run_key()
    w.run("close", key)
    w.run("resume", key)
    w.run("close", key)


@scenario
def finished_run_closed(w: World) -> None:
    """close of a finished --no-pr run: the workspace is closed and the checkout keeps the change."""
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--no-pr", "--", "Add a subtract function")
    w.run("close", w.run_key())


@scenario
def workflows_listed(w: World) -> None:
    """workflows: the list, with a workflow file and a prompt file installed; the default printed as a file; an
    example by its path; and a workflow file that does not load."""
    with open(f"{EXAMPLES}/quick.toml") as f:
        w.write("config/ai-agents-orchestrator/workflows/quick.toml", f.read())
    w.write("config/ai-agents-orchestrator/prompts/BUILD_PROMPT.md",
            "Build {spec_path} in {cwd}; follow the conventions. Report to {build_path}.\n")
    w.run("workflows")
    w.run("workflows", "default")
    w.run("workflows", f"{EXAMPLES}/product-team.toml")
    w.run("workflows", w.write("workflows/broken.toml", '[[steps]]\nuse = "nothing"\n'))


@scenario
def prompt_file_run(w: World) -> None:
    """A run with a prompt file: the Builder gets its text, the run saves it, and the commit says so."""
    w.write("config/ai-agents-orchestrator/prompts/BUILD_PROMPT.md",
            "Build what {spec_path} says, in {cwd}. As your last step, report to {build_path}.\n")
    approved_run(w)


@scenario
def conflict_refreshed(w: World) -> None:
    """refresh after a pull request opened over conflicts: first GitHub still finds it conflicting (exit status 5),
    then mergeable, so the warning goes from its description, it is marked ready, and the run forgets its conflicts."""
    publish_with_conflict(w)
    w.set_pr(1, mergeable="CONFLICTING")
    w.run("refresh")
    w.set_pr(1, mergeable="MERGEABLE")
    w.run("refresh", w.run_key())
    w.note("pull request", w.pr(1))


# -- serve ------------------------------------------------------------------------------


class EventStream:
    """serve's /events, read on a thread of its own: each event and each keep-alive comment as it arrives."""

    def __init__(self, url: str, path: str):
        """url: the server's, as serve printed it; path: the stream's path with its query."""
        parts = urllib.parse.urlsplit(url)
        self._conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=60)
        self._conn.request("GET", path)
        self.response = self._conn.getresponse()
        # Each item read so far, in order: (when it arrived, by time.monotonic, and the event's kind and payload, or
        # None for a comment).
        self.items: list[tuple[float, tuple[str, dict] | None]] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        kind, payload = None, None
        try:
            for raw in self.response:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    kind = line[len("event: "):]
                elif line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                elif line == ":":
                    self._add(None)
                elif line == "" and kind is not None:
                    self._add((kind, payload))
                    kind, payload = None, None
        # AttributeError: close() racing a read of a chunked response, which the Rust engine's serve sends an
        # HTTP/1.1 client, leaves http.client peeking at a connection it has set to None.
        except (OSError, ValueError, AttributeError, http.client.HTTPException):
            return

    def _add(self, item) -> None:
        with self._lock:
            self.items.append((time.monotonic(), item))

    def settled_since(self, t: float) -> bool:
        """Whether a poll that began after t has sent nothing new: an item arrived after t, and a comment after
        that one, so the comment's poll started after t."""
        with self._lock:
            after = [item for when, item in self.items if when > t]
        return len(after) >= 2 and after[-1] is None

    def events(self) -> list[tuple[str, dict]]:
        with self._lock:
            return [item for _, item in self.items if item is not None]

    def close(self) -> None:
        self._conn.close()


def serve_request(url: str, token: str | None, path: str, method: str = "GET", **headers: str) -> dict:
    """One request to serve, as {"request", "status", "headers", "body"}: the headers a client reads (content type,
    CORS, authentication), the body as served, its files sorted by name where it lists a run's files, whose order
    is by modification time and so by the clock."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
    if token:
        headers.setdefault("Authorization", f"Bearer {token}")
    conn.request(method, path, headers=headers)
    response = conn.getresponse()
    body = response.read().decode()
    conn.close()
    if path.startswith("/files") and response.status == 200:
        files = json.loads(body)
        files["files"].sort(key=lambda f: f["name"])
        body = files
    kept = ("Content-Type", "Access-Control-Allow-Origin", "Access-Control-Allow-Methods",
            "Access-Control-Allow-Headers", "Vary", "WWW-Authenticate", "Cache-Control")
    return {"request": f"{method} {path}", "status": response.status,
            "headers": {k: response.headers[k] for k in kept if response.headers[k] is not None}, "body": body}


def last_by_run(events: list[tuple[str, dict]]) -> dict:
    """Of events, the last record and the last files of each run, and the last problem: what a client that folds
    the stream, as the desktop app does, holds after them. Files are sorted by name, as serve_request sorts them.
    An event of any other kind is kept whole, in order, under "other"."""
    out: dict = {"records": {}, "files": {}}
    for kind, payload in events:
        if kind == "record":
            out["records"][payload["run"]] = payload["record"]
        elif kind == "files":
            out["files"][payload["run"]] = sorted(payload["files"], key=lambda f: f["name"])
        elif kind == "problem":
            out["problem"] = payload["error"]
        else:
            out.setdefault("other", []).append([kind, payload])
    return out


@scenario
def serve_endpoints_and_events(w: World) -> None:
    """serve: its endpoints and security checks, and an event stream over a run, read at each point where the run
    waits on an agent, and once it is done."""
    serve = w.start("serve", "--origin", "tauri://localhost")
    w.wait_for(lambda: serve.stdout.endswith("\n"), "serve to print its address")
    first = json.loads(serve.stdout)
    url, token = first["url"], first["token"]
    w.normalizer.literal(token, "<token>")
    w.normalizer.literal(url.removeprefix("http://"), "<serve>")
    cwd = urllib.parse.quote(w.project)
    host = urllib.parse.urlsplit(url).netloc
    requests = [
        serve_request(url, None, f"/runs?cwd={cwd}"),
        serve_request(url, token, f"/runs?cwd={cwd}", Host=f"evil.example:{url.rsplit(':', 1)[1]}"),
        serve_request(url, token, f"/runs?cwd={cwd}", Origin="https://evil.example"),
        serve_request(url, None, f"/runs?cwd={cwd}", "OPTIONS", Origin="tauri://localhost"),
        serve_request(url, token, f"/runs?cwd={cwd}", Origin="tauri://localhost"),
        serve_request(url, None, f"/runs?cwd={cwd}&token={token}", Host=host),
        serve_request(url, token, "/runs"),
        serve_request(url, token, f"/nothing?cwd={cwd}"),
    ]
    w.agent("spec", spec_written(hold="spec"))
    w.agent("build", built(1, hold="build"))
    w.agent("review", reviewed(1, hold="review"))
    stream = EventStream(url, f"/events?cwd={cwd}&token={token}")
    checkpoints = []
    run = w.start("run", "--", "Add a subtract function")
    for hold in ("spec", "build", "review"):
        w.wait_reached(hold)
        t = time.monotonic()
        w.wait_for(lambda t=t: stream.settled_since(t), f"the event stream to settle at {hold}")
        checkpoints.append({"waiting on": hold, **last_by_run(stream.events())})
        w.release(hold)
    run.wait()
    t = time.monotonic()
    w.wait_for(lambda: stream.settled_since(t), "the event stream to settle at the end")
    checkpoints.append({"waiting on": None, **last_by_run(stream.events())})
    stream.close()
    run_id = w.run_state(w.run_key())["run_id"]
    requests += [
        serve_request(url, token, f"/runs?cwd={cwd}"),
        serve_request(url, token, f"/files?cwd={cwd}&run={run_id}"),
        serve_request(url, token, f"/file?cwd={cwd}&run={run_id}&name=spec.md"),
        serve_request(url, token, f"/file?cwd={cwd}&run={run_id}&name=state.json"),
        serve_request(url, token, f"/file?cwd={cwd}&run={run_id}&name=..%2Fx"),
        serve_request(url, token, f"/file?cwd={cwd}&run={run_id}&name=nothing.md"),
        serve_request(url, token, f"/files?cwd={cwd}"),
    ]
    serve.process.send_signal(signal.SIGTERM)
    serve.wait()
    w.note("requests", requests)
    w.note("event stream", {"status": stream.response.status,
                            "content type": stream.response.headers["Content-Type"], "checkpoints": checkpoints})


# -- The other commands -----------------------------------------------------------------

DRAIN_SPEC = """# {title}

Status: {status}
{blocked}
## Goal

{goal}
"""


@scenario
def specs_drained(w: World) -> None:
    """drain over two specs, the second blocked by the first: --dry-run, then a run of the first, which ends in
    review, leaving the second waiting (exit status 5); the specs' headers are noted."""
    specs = {"a-subtract": DRAIN_SPEC.format(title="Add a subtract function", status="ready-for-agent", blocked="",
                                             goal="Add `subtract(a, b)` to src/calc.py."),
             "b-multiply": DRAIN_SPEC.format(title="Add a multiply function", status="ready-for-agent",
                                             blocked="Blocked by: a-subtract\n", goal="Add `multiply(a, b)`.")}
    for slug, text in specs.items():
        w.write(f"specs/{slug}/spec.md", text)
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("drain", "--dry-run", f"{w.root}/specs")
    w.run("drain", f"{w.root}/specs")
    w.set_pr(1, state="MERGED", closedAt="2026-01-02T00:00:00Z")
    w.run("drain", "--dry-run", f"{w.root}/specs")
    notes = {}
    for slug in specs:
        with open(f"{w.root}/specs/{slug}/spec.md") as f:
            notes[slug] = f.read()
    w.note("specs", notes)


@scenario
def judged_in_quality(w: World) -> None:
    """judged-in-quality on a branch whose tree a retained quality build judged: refused while only a build of
    another tree is there, then found; and refused for a target that is no branch name."""
    w.git(w.project, "switch", "--quiet", "-c", "feature")
    w.write("project/src/calc.py", CALC_WITH_SUBTRACT)
    w.git(w.project, "commit", "--quiet", "-am", "Add subtract")
    tree = w.git(w.project, "rev-parse", "HEAD^{tree}").strip()
    base = w.git(w.project, "rev-parse", "main").strip()
    other = w.git(w.project, "rev-parse", "main^{tree}").strip()
    w.retained_build(5, "SUCCESS", f"judged tree={other} parent={base} main={base}")
    w.run("judged-in-quality", "--quality-gate", JOB, "--target", "main")
    w.retained_build(6, "SUCCESS", f"judged tree={tree} parent={base} main={base}")
    w.run("judged-in-quality", "--quality-gate", JOB, "--target", "main")
    w.run("judged-in-quality", "--quality-gate", JOB, "--target", "no..branch")


@scenario
def doctor_checks(w: World) -> None:
    """doctor here, with --cwd, and on a machine: what it asks herdr and gh, and its exit status. Its lines are
    noted without the versions of git and Tk, which are the host's."""
    w.machine("box")
    w.run("doctor", output_recorded=False)
    w.run("doctor", "--cwd", w.project, output_recorded=False)
    w.run("doctor", "--machine", "box", "--cwd", w.project, output_recorded=False)
    w.run("doctor", "--machine", "nowhere", "--cwd", w.project, output_recorded=False)
    w.note("lines", [host_free(inv.stdout) for inv in w.invocations])


def host_free(report: str) -> list[str]:
    """doctor's lines without those of the git and tkinter checks, and their fixes, which say what the host has."""
    lines, skipping = [], False
    for line in report.splitlines():
        if not line.startswith(" "):
            skipping = line.split(":")[0].split()[-1] in ("git", "tkinter")
        if not skipping:
            lines.append(line)
    return lines


@scenario
def spec_file_run(w: World) -> None:
    """run --spec: the spec is copied in as spec.md, the interview skipped, and the task taken from its title."""
    path = w.write("specs/subtract.md", SPEC)
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    w.run("run", "--spec", path)


@scenario
def gate_turned_on_by_resume(w: World) -> None:
    """A run started without the quality gate, killed in its build, resumed with --quality-gate: the Builder's
    report is judged before the Reviewer starts."""
    w.jenkins_build("SUCCESS", "GATE: OK\n")
    w.agent("spec", spec_written())
    w.agent("build", built(1, hold="build"))
    w.agent("review", reviewed(1))
    run = w.start("run", "--", "Add a subtract function")
    w.wait_reached("build")
    run.kill()
    w.release("build")
    w.run("resume", "--force", "--quality-gate", JOB, w.run_key())


@scenario
def gate_turned_off_by_resume(w: World) -> None:
    """A run killed while Jenkins builds its first quality round, resumed with --no-quality-gate: its refs on origin
    go, and the Reviewer is told nothing judged the change."""
    w.jenkins_build("SUCCESS", "GATE: OK\n", hold="quality")
    w.agent("spec", spec_written())
    w.agent("build", built(1))
    w.agent("review", reviewed(1))
    run = w.start(*GATED, "--", "Add a subtract function")
    w.wait_reached("quality")
    run.kill()
    w.run("resume", "--force", "--no-quality-gate", w.run_key())
