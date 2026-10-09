"""
Role-based handoff orchestrator for coding agents in herdr:

    Spec Collector -> Builder -> Reviewer  (review findings loop back to the Builder)

Each role is a separate interactive agent session in its own herdr pane, so no
role judges its own work and the human can watch or step into any of them.
Each role runs in the agent harness of its choice (AGENT_KINDS), Claude Code by
default, so that, say, Claude Code builds and Codex reviews. Roles hand off
through Markdown files in the run directory, never through scraped terminal
output. That sequence is the default workflow; a workflow is data (a Pipeline
of Steps), and `run --workflow` picks one from WORKFLOWS.

By default the change is built on a new branch, committed, pushed and opened
as a pull request once the review loop ends, and the run's herdr workspace is
closed: the human reviews on GitHub, not in the panes.

The agents may run on a saved herdr machine (--machine). The run directory
then lives on that machine, so every file and git access goes through Host,
which runs commands locally or over SSH.

With --quality-gate, every Builder turn is followed by a quality round: a
snapshot of the change is analysed by a Jenkins job into SonarQube, and a gate
that does not pass goes back to the Builder before the Reviewer sees the change.
In another workflow, its quality-gated step takes the Builder's place.
HTTP to Jenkins and SonarQube goes through CI, from the orchestrator's machine.

The gui command, the default with no arguments where there is a display, is a
window over the same CLI: it starts run and resume as child processes.
"""

import argparse
import base64
import collections
import difflib
import http.client
import json
import os
import queue
import re
import secrets
import shlex
import signal
import socket
import string
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from types import SimpleNamespace

RUNS_DIR = ".orchestrator/runs"
# Where run --worktree puts each run's worktree, named by the run's key.
WORKTREES_DIR = ".orchestrator/worktrees"
DEFAULT_TURN_TIMEOUT = 1800
DEFAULT_MAX_ROUNDS = 3
AGENT_START_TIMEOUT_MS = 60_000
POLL_SECONDS = 3
# How long a Builder or Reviewer may sit idle without its handoff file before the human is told.
STALL_SECONDS = 180
# A live orchestrator rewrites state.json at least this often, so `list` can tell a live run from a dead one.
HEARTBEAT_SECONDS = 60
# A run whose state.json is older than this is stale: five missed beats, which rides out a slow SSH hop
# or a Host.run that hits its 60 s limit.
STALE_SECONDS = 300
# git fetch, git push and gh talk to the network; everything else Host runs is local to the machine.
NETWORK_TIMEOUT = 300
BRANCH_PREFIX = "orchestrator/"
REMOTE = "origin"
# prune deletes a run's branch once its pull request is merged, or closed this long: time to reopen it.
CLOSED_BRANCH_GRACE = 14 * 24 * 3600
# GitHub rejects pull request bodies over 65536 characters; this leaves room for the frame.
PR_SECTION_LIMIT = 20_000

APPROVE = "APPROVE"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
# The outcomes of a run whose workflow has no verdict step, once its last step is done. FINISHED counts as
# approved: the run did all it was asked to, though no agent judged the change. With the quality gate,
# QUALITY_GATE_FAILED says it still failed after the last quality round, and no agent weighed that.
FINISHED = "FINISHED"
QUALITY_GATE_FAILED = "QUALITY_GATE_FAILED"
SUCCEEDED = (APPROVE, FINISHED)

# The quality gate. Requests time out below STALE_SECONDS, so a hung one cannot make a live run look stale.
DEFAULT_MAX_QUALITY_ROUNDS = 3
HTTP_TIMEOUT = 30
CI_POLL_SECONDS = 10
# How long one quality round may wait on Jenkins and SonarQube, failed requests retried, before the run fails.
QUALITY_TIMEOUT = 1200
# The orchestrator's own environment variables that hold the credentials; no process it starts inherits them.
CI_ENV = ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN", "SONAR_HOST_URL", "SONAR_TOKEN")
CI_TOKENS = ("JENKINS_TOKEN", "SONAR_TOKEN")
JOB_PARAMETERS = ("GIT_REF", "SONAR_PROJECT_KEY", "SONAR_PROJECT_VERSION")
# The SonarQube project whose quality gate each run's own project, SONAR_PROJECT-<key>, copies.
SONAR_PROJECT = "py-ai-agents-orchestrator"
# Snapshots are pushed to orchestrator-ci/<key>-<round>-q<quality round>, the base to orchestrator-ci/<key>-base.
CI_REF_PREFIX = "orchestrator-ci/"
# Files a change may edit that alter how Jenkins and the scanner judge it.
BUILD_CONFIG_FILES = ("Jenkinsfile", "sonar-project.properties")
QUALITY_ISSUE_LIMIT = 50
CONSOLE_TAIL_LINES = 60
ISSUE_PAGE_SIZE = 500
# SonarQube serves at most this many issues of one search.
ISSUE_SEARCH_LIMIT = 10_000
COVERAGE_METRICS = ("new_coverage", "new_lines_to_cover", "new_uncovered_lines")

GATE_OK = "OK"
GATE_ERROR = "ERROR"
GATE_BUILD_FAILED = "BUILD_FAILED"

# The default workflow's roles. Each has its own --ROLE-model flag; another workflow's roles take --model.
ROLE_LABELS = {"spec": "Spec Collector", "build": "Builder", "review": "Reviewer"}

# The agent harnesses a role can run in, by herdr's --kind. Each needs its herdr integration and its CLI
# where the agents run.
AGENT_KINDS = ("claude", "codex", "gemini", "opencode", "pi")
DEFAULT_AGENT = "claude"

EXIT_ERROR = 1
EXIT_CHANGES_REQUESTED = 3
# Approved, but the pull request conflicts with its base, so it was opened as a draft.
EXIT_CONFLICT = 4
EXIT_INTERRUPTED = 130


class OrchestratorError(Exception):
    """A failure that ends the run; the message is shown to the user."""


class HerdrError(OrchestratorError):
    def __init__(self, code: str, message: str):
        super().__init__(f"herdr {code}: {message}")
        self.code = code


def child_env() -> dict[str, str]:
    """The environment for every process the orchestrator starts: its own, without the CI credentials.

    So the agents, which herdr, git, ssh and gh start or reach, never inherit the tokens. They can
    still read a file the tokens came from, as the same user.
    """
    return {k: v for k, v in os.environ.items() if k not in CI_ENV}


def config_dir() -> str:
    """The orchestrator's own configuration directory, on the machine it runs on."""
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(config):
        config = os.path.expanduser("~/.config")
    return os.path.join(config, "ai-agents-orchestrator")


def ci_env_path() -> str:
    """The file the quality gate's credentials come from when the environment lacks them."""
    return os.path.join(config_dir(), "ci.env")


def ci_credentials(environ, path: str) -> dict[str, str]:
    """CI_ENV from environ, each one it lacks taken from the file at path.

    The file's values stay in the returned dict and never enter os.environ, so child_env needs no
    help to keep them from the agents. It is read only when something is missing, and refused when
    others may read it: it holds two tokens.
    """
    found = {k: environ[k] for k in CI_ENV if environ.get(k)}
    if len(found) == len(CI_ENV):
        return found
    try:
        with open(path, encoding="utf-8") as f:
            if os.fstat(f.fileno()).st_mode & 0o077:
                raise OrchestratorError(f"{path} holds tokens but others may read it; run: chmod 600 {path}")
            text = f.read()
    except FileNotFoundError:
        return found
    except (OSError, UnicodeDecodeError) as e:
        raise OrchestratorError(f"could not read {path}: {e}") from e
    return {**parse_env_file(text, path), **found}


def parse_env_file(text: str, path: str) -> dict[str, str]:
    """The NAME=value lines of text, with the quotes and `export ` that a file also sourced by a shell has."""
    values = {}
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.removeprefix("export ").partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_]\w*", name, re.ASCII):
            raise OrchestratorError(f"{path}:{i}: expected NAME=value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name] = value
    return values


# ---------------------------------------------------------------------------
# Role prompts
# ---------------------------------------------------------------------------

SPEC_PROMPT = """\
You are the Spec Collector, the first of three roles (Spec Collector -> Builder -> Reviewer). \
Separate agent sessions play the Builder and the Reviewer; they will know only what you write down. \
A human is at this terminal and answers you directly.

Task from the human:
{task}

Interview the human until the requirements are unambiguous: the goal, what is in and out of scope, \
testable acceptance criteria, constraints, and how the result will be verified. \
Read the code in {cwd} first so your questions are grounded and you can cite the files the change touches. \
Ask a few questions at a time. Do not write or change any code.

When the human approves the spec, write it in a single write to {spec_path} as Markdown. \
Its first line is a `# ` heading: a short imperative title for the change, under 70 characters; \
it becomes the commit subject and the pull request title. \
Then these `##` sections: \
Goal, Scope, Non-goals, Acceptance criteria (a numbered list, each one checkable), Relevant code (file:line), Verification. \
Writing that file hands the work to the Builder, so write it only after the human approves it."""

BUILD_PROMPT = """\
You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). \
The spec in {spec_path} was agreed with the human by a separate session; it is your contract.

Implement it in {cwd}, following the conventions of the surrounding code. \
Verify the change the way the spec's Verification section says, and run the tests. \
Do not commit, push or switch branches; leave the changes in the working tree for the Reviewer. \
If the spec is wrong or cannot be met, do not deviate silently: say so in your report.

As your last step, write a report to {build_path} in a single write; it hands the work to the Reviewer: the files you changed and why, \
how you verified the change (commands and a summary of their results), \
and any acceptance criterion you did not meet, with the reason."""

FIX_PROMPT = """\
The Reviewer requested changes; the findings are in {prev_review_path}. \
Fix each finding, or explain in your report why it is wrong. Re-run the verification. \
Do not commit, push or switch branches. As your last step, write a new report to {build_path} in a single write, in the same shape as before, \
answering each finding by its number."""

REVIEW_PROMPT = """\
You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). \
You did not write this change. Judge it only against the spec in {spec_path} and the code itself.

The change: {change}
The Builder's report is in {build_path}. Treat its claims as unverified: check them, and run the verification yourself. \
Do not modify any file other than your review.

As your last step, write your review to {review_path} in a single write. Its first line must be exactly `VERDICT: {approve}` or `VERDICT: {changes}`. \
Then list numbered findings, each with file:line, what is wrong, and which acceptance criterion it violates \
or what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. \
Style preferences are not defects."""

RECHECK_PROMPT = """\
The Builder has answered your review; the new report is in {build_path}. \
Review the change again ({change}) against the spec in {spec_path} and your previous findings, \
checking the Builder's claims rather than trusting them. \
As your last step, write the review to {review_path} in a single write, with the same first-line verdict and numbered findings as before."""

# For a role relaunched into its saved agent session after it had already been prompted.
CONTINUE_PROMPT = """\
Your session was restarted in the middle of this turn. Continue where you left off; \
the turn still ends when you write {path} in a single write."""

# For a verdict step whose file had no VERDICT first line. Self-contained, so a fresh session can answer it too.
RETRY_VERDICT_PROMPT = """\
The file {path} was rejected: it was empty, or its first non-blank line was not a verdict line. \
It has been moved to {rejected_path}; reuse its findings, if it has any. \
Write the file again to {path} in a single write. Its first line must be exactly `VERDICT: {approve}` \
or `VERDICT: {changes}`, followed by the numbered findings."""

# Appended for a Builder or Reviewer that starts a fresh session in a later round: it has not seen the earlier ones.
REBUILD_NOTE = """\
This is round {n}, and you are a fresh session. An earlier Builder session did the earlier turns; \
its changes are already in the working tree, and its reports are {earlier_build_paths}."""

REREVIEW_NOTE = """\
This is round {n}, and you are a fresh session. The earlier reviews of this change are {earlier_review_paths}; \
check that the Builder has answered each of their findings."""

QUALITY_FIX_PROMPT = """\
The SonarQube quality gate did not pass on your change; the findings are in {quality_path}. \
Fix each numbered issue, the failed conditions, the failing tests and a failed build, \
or explain in your report why one should stand. Re-run the verification. \
Do not commit, push or switch branches. As your last step, write a new report to {report_path} in a single write, \
in the same shape as before, answering each numbered issue by its number."""

# Appended to the Reviewer's prompt in a run with the quality gate.
QUALITY_PASSED_NOTE = """\
The SonarQube quality gate passed on this change; its report is in {quality_path}."""

QUALITY_UNRESOLVED_NOTE = """\
The SonarQube quality gate still did not pass after the Builder's last quality round; \
its unresolved findings are in {quality_path}. Weigh them as you would your own."""


# ---------------------------------------------------------------------------
# Workflow definitions
# ---------------------------------------------------------------------------

# Run phases that are no step's: the quality loop after a quality-gated step's turn, and the run's last two.
QUALITY = "quality"
PUBLISH = "publish"
DONE = "done"

# The placeholders a step's prompts may use besides the per-step paths (see Step).
PROMPT_FIELDS = {"task", "cwd", "n", "change", "approve", "changes", "path"}
# The per-step paths: (prefix, suffix) around a step's id.
PATH_FIELDS = (("", "_path"), ("prev_", "_path"), ("earlier_", "_paths"))


@dataclass(frozen=True)
class Step:
    """One role's turn in a workflow, which ends when the role writes its handoff file.

    A prompt is formatted with PROMPT_FIELDS, where n is the round and path the turn's own
    file, and with three paths for every step X of the workflow: X_path in round n,
    prev_X_path in round n-1, and earlier_X_paths, rounds 1 to n-1 joined with commas.

    A quality-gated step writes more than one file a round: the answer to quality round q
    inserts -q<q> before the extension. So for such an X, X_path is the file its current turn
    writes in its own prompts and its last one of round n in the others', prev_X_path is its
    last one of round n-1, and earlier_X_paths lists every one before its current turn.
    """
    id: str  # also the run's phase while the step is current
    role: str  # names the agent, and keys its pane, label and model
    file: str  # the handoff file's name; a {n} in it makes one file per round
    prompt: str  # round 1, or a step outside the review loop
    # A later round, for an agent that has seen the earlier ones; None repeats prompt.
    again: str | None = None
    # A fresh session in a later round gets prompt, then this note on the turns it has not seen,
    # then again when fresh_repeats_again: when again asks for work that prompt does not.
    fresh_note: str | None = None
    fresh_repeats_again: bool = False
    # Paced by the human: no turn timeout, no stall notice, and the pane is focused once the agent is ready.
    human_paced: bool = False
    # Changes the working tree; the run's base commit and branch are fixed just before the first such step.
    edits: bool = False
    # Makes this the verdict step: its file starts with a VERDICT line, and CHANGES_REQUESTED goes
    # back to the step with this id in round n+1, until max_rounds.
    loop_to: str | None = None
    # With --quality-gate, each turn is followed by the quality loop, which sends a gate that does
    # not pass back to this step with QUALITY_FIX_PROMPT, and the verdict step hears how it ended.
    quality_gated: bool = False

    @property
    def per_round(self) -> bool:
        return "n" in _fields_of(self.file)

    def path(self, run_dir: str, n: int, q: int = 0) -> str:
        """The handoff file of round n; with q, the answer to quality round q."""
        return f"{run_dir}/{self.name(n, q)}"

    def name(self, n: int, q: int = 0) -> str:
        name = self.file.format(n=n)
        if q:
            stem, ext = os.path.splitext(name)
            name = f"{stem}-q{q}{ext}"
        return name

    def name_forms(self) -> list[list[str | None]]:
        """The step's file names as _name_tokens: its file's, and for the gated step its quality answers'."""
        forms = [_name_tokens(self.file)]
        if self.quality_gated:
            # As name() does: -q<q> before the extension, where the template splits at the same dot as
            # the filled-in name, since a round number has none.
            stem, ext = os.path.splitext(self.file)
            forms.append(_name_tokens(stem) + ["-", "q", None] + _name_tokens(ext))
        return forms

    def last_path(self, run_dir: str, n: int, quality_round: int) -> str:
        """The step's last file of round n, once the round's quality rounds, if any, are over."""
        return self.path(run_dir, n, max(quality_round - 1, 0) if self.quality_gated else 0)

    def templates(self) -> list[str]:
        return [t for t in (self.prompt, self.again, self.fresh_note) if t is not None]

    def placeholders(self) -> set[str]:
        return {name for t in self.templates() for name in _fields_of(t)}


@dataclass(frozen=True)
class Pipeline:
    """A named workflow: roles that hand off through files, one step after another.

    A run is in round 0 until its first editing step, round 1 from there, and one round
    more each time the verdict step loops back.
    """
    name: str
    # Role key -> pane label. The order lays out the panes: the first role takes the workspace's
    # root pane, the second a split to its right, and each further one a split below the one before
    # (Workflow._pane_order, which reorders them for a run that skips its contract step).
    roles: dict[str, str]
    steps: tuple[Step, ...]
    # Role key -> the model its agent starts with when no model flag names one.
    models: dict[str, str] = field(default_factory=dict)
    description: str = ""
    # Role key -> the harness its agent runs in when no agent flag names one; else DEFAULT_AGENT.
    agents: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if problem := self._problem():
            raise ValueError(f"workflow {self.name}: {problem}")

    def _problem(self) -> str | None:
        """What makes the definition unusable, or None; the first problem found, in the order of the checks."""
        if not self.steps:
            return "has no steps"
        checks = (self._name_problem, self._role_problem, self._uniqueness_problem, self._step_problem,
                  self._collision_problem, self._loop_problem)
        return next((problem for check in checks if (problem := check())), None)

    def _name_problem(self) -> str | None:
        ids, files = [st.id for st in self.steps], [st.file for st in self.steps]
        for kind, names in (("step id", ids), ("handoff file", files)):
            if dup := next((x for x in names if names.count(x) > 1), None):
                return f"duplicate {kind} {dup}"
        if reserved := {QUALITY, PUBLISH, DONE} & set(ids):
            return f"step id {min(reserved)} is reserved for a phase of the run's own"
        return None

    def _role_problem(self) -> str | None:
        for role, label in self.roles.items():
            if not label:
                return f"role {role} has no label"
            if role not in {st.role for st in self.steps}:
                return f"role {role} has no step"
        for what, table in (("a model", self.models), ("an agent", self.agents)):
            if unknown := set(table) - set(self.roles):
                return f"{what} is set for role {min(unknown)}, which is not one of the workflow's roles"
        for role, kind in self.agents.items():
            if kind not in AGENT_KINDS:
                return f"role {role}: agent {kind} is not a harness this orchestrator supports; " \
                       f"it takes {', '.join(AGENT_KINDS)}"
        return None

    def _uniqueness_problem(self) -> str | None:
        """More than one verdict loop or quality-gated step, or a gated step that cannot be one."""
        loops = [st for st in self.steps if st.loop_to is not None]
        if len(loops) > 1:
            return f"steps {', '.join(st.id for st in loops)} each loop back; only one verdict loop is supported"
        gated = [st for st in self.steps if st.quality_gated]
        if len(gated) > 1:
            return f"steps {', '.join(st.id for st in gated)} are each quality-gated; only one may be"
        for st in gated:
            if not st.edits:
                return f"step {st.id} is quality-gated, so it must edit"
            if not st.per_round:
                return f"step {st.id} is quality-gated, so its handoff file needs {{n}}"
            if st.loop_to is not None:
                return f"step {st.id} is quality-gated, so it cannot be the verdict step"
        return None

    def _step_problem(self) -> str | None:
        names = [name for name, _, _ in self.path_placeholders()]
        if dup := next((x for x in names if names.count(x) > 1), None):
            return f"prompt placeholder {dup} names the files of two steps"
        known = PROMPT_FIELDS | set(names)
        first_edit = next((i for i, st in enumerate(self.steps) if st.edits), len(self.steps))
        for i, st in enumerate(self.steps):
            if st.role not in self.roles:
                return f"step {st.id}: role {st.role} has no label"
            if set(_fields_of(st.file)) - {"n"}:
                return f"step {st.id}: handoff file {st.file} may use only {{n}}"
            if st.per_round and i < first_edit:
                # Round 0 lasts until the first editing step, so such a file would be numbered 0.
                return f"step {st.id}: a per-round handoff file needs an editing step at or before it"
            if unknown := st.placeholders() - known:
                return f"step {st.id}: unknown prompt placeholder {min(unknown)}"
            if problem := _fill_problem(st, known):
                return f"step {st.id}: {problem}"
        return None

    def _collision_problem(self) -> str | None:
        """Two steps whose files can have the same name: the later turn would find its file already written."""
        for i, st in enumerate(self.steps):
            for other in self.steps[i + 1:]:
                if (name := _shared_step_name(st, other)) is not None:
                    return f"steps {st.id} and {other.id} can both write {name}"
        return None

    def _loop_problem(self) -> str | None:
        verdict = self.verdict_step
        if verdict is None:
            return None
        ids = [st.id for st in self.steps]
        if verdict.loop_to not in ids:
            return f"step {verdict.id} loops back to {verdict.loop_to}, which is not a step"
        target, end = ids.index(verdict.loop_to), ids.index(verdict.id)
        if target >= end:
            return f"step {verdict.id} loops back to {verdict.loop_to}, which does not come before it"
        # A later round finds a fixed file already written and would skip the step.
        if fixed := next((st for st in self.steps[target:end + 1] if not st.per_round), None):
            return f"step {fixed.id} is in the review loop, so its handoff file needs {{n}}"
        return None

    def path_placeholders(self) -> list[tuple[str, str, Step]]:
        """Each per-step path placeholder, with its prefix and the step whose file it names."""
        return [(f"{pre}{st.id}{post}", pre, st) for st in self.steps for pre, post in PATH_FIELDS]

    def step(self, step_id: str) -> Step | None:
        return next((st for st in self.steps if st.id == step_id), None)

    def after(self, step: Step) -> Step | None:
        i = self.steps.index(step) + 1
        return self.steps[i] if i < len(self.steps) else None

    @property
    def first_edit(self) -> Step | None:
        return next((st for st in self.steps if st.edits), None)

    @property
    def last_edit(self) -> Step | None:
        return next((st for st in reversed(self.steps) if st.edits), None)

    @property
    def verdict_step(self) -> Step | None:
        return next((st for st in self.steps if st.loop_to is not None), None)

    @property
    def gated(self) -> Step | None:
        return next((st for st in self.steps if st.quality_gated), None)

    @property
    def contract(self) -> Step | None:
        """The step whose file is the change's spec, titling its branch and pull request; None leaves the task."""
        first = self.steps[0]
        return None if first.edits or first.per_round else first

    @property
    def result(self) -> Step:
        """The step whose file the run ends on."""
        return self.verdict_step or self.steps[-1]


# Files the run writes into its directory itself, beside the steps' handoff files.
RUN_FILE = re.compile(r"state\.json|quality-.*")


def _fill_problem(step: Step, known: set[str]) -> str | None:
    """What stops the step's file or prompts from being filled in at run time, which a bad format spec,
    conversion or nested field would, though its placeholders are all known."""
    # A plain {n}, so that _name_tokens knows every name it can give.
    if any(spec or conversion for _, field, spec, conversion in string.Formatter().parse(step.file) if field):
        return f"handoff file {step.file} may use {{n}} only as it is, with no format spec or conversion"
    name = step.file.format(n=1)
    if "/" in name or name in ("", ".", "..") or RUN_FILE.fullmatch(name):
        return f"handoff file {step.file} must be a plain file name, and not state.json or quality-*, " \
               f"which the run writes itself"
    # Every value a prompt is filled in with is a string.
    values = dict.fromkeys(known, "")
    for field_name in ("prompt", "again", "fresh_note"):
        if (template := getattr(step, field_name)) is None:
            continue
        try:
            template.format(**values)
        except (ValueError, KeyError, IndexError, AttributeError) as e:
            return f"{field_name} cannot be filled in: {type(e).__name__}: {e}"
    return None


def _name_tokens(template: str) -> list[str | None]:
    """A file template as its characters, with None for each {n}: a round or quality round number."""
    tokens = []
    for literal, field, _, _ in string.Formatter().parse(template):
        tokens += literal
        if field is not None:
            tokens.append(None)
    return tokens


def _shared_name(a: list[str | None], b: list[str | None]) -> str | None:
    """The shortest name that both token lists give, or None when they share none.

    Each list is matched by a small automaton whose states are (position, inside a number), a number
    being [1-9][0-9]*, as rounds are. A breadth-first walk over pairs of state sets, one character at a
    time, finds a name both accept, so the answer is exact: no sampling of rounds. The one approximation
    is a template that repeats {n}, whose numbers are taken as independent; it can only reject more.
    """
    alphabet = sorted({t for t in a + b if t is not None} | set("0123456789"))
    start = (_token_closure({(0, False)}),) * 2
    # Each pair of state sets reached, with the name that first reached it.
    reached = {start: ""}
    queue = collections.deque([start])
    while queue:
        pair = queue.popleft()
        if (len(a), False) in pair[0] and (len(b), False) in pair[1]:
            return reached[pair]
        for c in alphabet:
            nxt = (_token_advance(pair[0], a, c), _token_advance(pair[1], b, c))
            if nxt[0] and nxt[1] and nxt not in reached:
                reached[nxt] = reached[pair] + c
                queue.append(nxt)
    return None


def _shared_step_name(st: Step, other: Step) -> str | None:
    """The shortest name that files of both steps can have, or None."""
    return next((name for a in st.name_forms() for b in other.name_forms()
                 if (name := _shared_name(a, b)) is not None), None)


def _token_closure(states: set) -> frozenset:
    """The automaton states of _shared_name, with each number also ended: one may end after any digit."""
    return frozenset(states | {(i + 1, False) for i, inside in states if inside})


def _token_advance(states: frozenset, tokens: list[str | None], c: str) -> frozenset:
    return _token_closure({nxt for state in states if (nxt := _token_step(state, tokens, c)) is not None})


def _token_step(state: tuple[int, bool], tokens: list[str | None], c: str) -> tuple[int, bool] | None:
    """Where one state goes on the character c, or None when c ends it."""
    i, inside = state
    if inside:
        return state if c.isdigit() else None
    if i == len(tokens):
        return None
    if tokens[i] is None:
        # A number starts with a digit other than 0.
        return (i, True) if c in "123456789" else None
    return (i + 1, False) if tokens[i] == c else None


def _fields_of(template: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]


DEFAULT_WORKFLOW = Pipeline("default", dict(ROLE_LABELS), (
    Step("spec", "spec", "spec.md", SPEC_PROMPT, human_paced=True),
    Step("build", "build", "build-{n}.md", BUILD_PROMPT, again=FIX_PROMPT,
         fresh_note=REBUILD_NOTE, fresh_repeats_again=True, edits=True, quality_gated=True),
    Step("review", "review", "review-{n}.md", REVIEW_PROMPT, again=RECHECK_PROMPT,
         fresh_note=REREVIEW_NOTE, loop_to="build"),
))

# The workflows `run --workflow` offers, by name.
WORKFLOWS = {w.name: w for w in (DEFAULT_WORKFLOW,)}


def find_workflow(name: str) -> Pipeline:
    try:
        return WORKFLOWS[name]
    except KeyError:
        raise OrchestratorError(f"unknown workflow {name}; this orchestrator has "
                                f"{', '.join(sorted(WORKFLOWS))}") from None


# ---------------------------------------------------------------------------
# Workflow files
# ---------------------------------------------------------------------------

# A workflow file is TOML holding a workflow's definition, named after the file:
#
#   description = "..."                      # optional, shown by `workflows`
#   [roles]                                  # optional
#   build = "Builder"                        # a role's pane label,
#   tests = { label = "Test Writer", model = "sonnet", agent = "codex" }   # or its label, default model
#                                                                         # and harness (AGENT_KINDS)
#   [[steps]]                                # one table per step, in order
#   use = "build"                            # optional: start from that step of the default workflow
#   id = "build"                             # and Step's fields; role defaults to id
#
# A role the [roles] table leaves out, or gives no label, is labelled after the default workflow's
# role of that key, or else after the key itself. The panes are laid out in the order the steps
# first use the roles. workflow_definition writes the canonical form, which
# such a file may also be: every role with its label, and every step with its fields.

WORKFLOW_KEYS = {"description", "roles", "steps"}
ROLE_KEYS = {"label", "model", "agent"}
STEP_KEYS = {f.name for f in fields(Step)}
STEP_FLAGS = {f.name for f in fields(Step) if f.default is False}
# Role keys name agents and panes, and step ids name phases and prompt placeholders.
NAME_PATTERN = r"[A-Za-z0-9_-]+"


def workflows_dir() -> str:
    """Where `run --workflow NAME` finds NAME.toml, on the orchestrator's machine."""
    return os.path.join(config_dir(), "workflows")


def workflow_files() -> dict[str, str]:
    """Name -> path of each workflow file in workflows_dir(), unparsed; none without the directory."""
    try:
        names = os.listdir(workflows_dir())
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise OrchestratorError(f"cannot list {workflows_dir()}: {e.strerror}") from e
    return {stem: os.path.join(workflows_dir(), name) for name in sorted(names)
            for stem, ext in [os.path.splitext(name)] if ext == ".toml"}


def workflow_names() -> list[str]:
    return sorted(set(WORKFLOWS) | set(workflow_files()))


def named_workflow(name: str) -> Pipeline:
    """The built-in workflow of that name, or else the one in workflows_dir()."""
    if name in WORKFLOWS:
        return WORKFLOWS[name]
    if path := workflow_files().get(name):
        return load_workflow_file(path)
    raise OrchestratorError(f"unknown workflow {name}; there are {', '.join(workflow_names())}")


def load_workflow_file(path: str) -> Pipeline:
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise OrchestratorError(f"cannot read workflow file {path}: {e.strerror}") from e
    except tomllib.TOMLDecodeError as e:
        raise OrchestratorError(f"{path}: {e}") from e
    if name in WORKFLOWS:
        raise OrchestratorError(f"{path}: workflow {name} is built in; give the file another name")
    if not re.fullmatch(NAME_PATTERN, name):
        raise OrchestratorError(f"{path}: a workflow's name, its file's, may use only letters, digits, - and _")
    try:
        return parse_workflow(name, data)
    except ValueError as e:
        raise OrchestratorError(f"{path}: {e}") from e


def parse_workflow(name: str, data: dict) -> Pipeline:
    """The workflow a definition describes; ValueError names what is wrong with it."""
    _check_keys(data, WORKFLOW_KEYS, "the file")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("it has no [[steps]]")
    steps = [_parse_step(i, raw) for i, raw in enumerate(raw_steps, 1)]
    raw_roles = data.get("roles", {})
    if not isinstance(raw_roles, dict):
        raise ValueError("roles must be a table")
    declared = {role: _parse_role(role, raw) for role, raw in raw_roles.items()}
    # The panes follow the steps; a declared role no step uses goes last, for Pipeline to reject.
    order = dict.fromkeys([st["role"] for st in steps] + list(declared))
    labels = {role: declared.get(role, {}).get("label") or _default_label(role) for role in order}
    models = {role: table["model"] for role, table in declared.items() if table.get("model")}
    agents = {role: table["agent"] for role, table in declared.items() if table.get("agent")}
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    return Pipeline(name, labels, tuple(Step(**st) for st in steps), models=models, description=description,
                    agents=agents)


def _parse_role(role: str, raw) -> dict[str, str]:
    """The ROLE_KEYS a role's entry in [roles] sets."""
    where = f"role {role}"
    if not re.fullmatch(NAME_PATTERN, role):
        raise ValueError(f"{where}: a role key may use only letters, digits, - and _")
    if isinstance(raw, str):
        return {"label": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: give a label, or a table with label, model and agent")
    _check_keys(raw, ROLE_KEYS, where)
    for key in ROLE_KEYS & raw.keys():
        if not isinstance(raw[key], str):
            raise ValueError(f"{where}: {key} must be a string")
    return raw


def _parse_step(i: int, raw) -> dict:
    """Step's arguments from step i of a definition, its use resolved."""
    if not isinstance(raw, dict):
        raise ValueError(f"step {i} must be a table")
    where = f"step {i}" + (f" ({raw['id']})" if isinstance(raw.get("id"), str) else "")
    _check_keys(raw, STEP_KEYS | {"use"}, where)
    step = _used_step(raw, where) | {k: v for k, v in raw.items() if k != "use"}
    step.setdefault("role", step.get("id"))
    for key in ("id", "file", "prompt"):
        if key not in step:
            raise ValueError(f"{where}: {key} is missing")
    for key, value in step.items():
        _check_step_value(key, value, where)
    return step


def _used_step(raw: dict, where: str) -> dict:
    """The fields of the default step that raw's use names; none without a use."""
    if "use" not in raw:
        return {}
    used = DEFAULT_WORKFLOW.step(raw["use"]) if isinstance(raw["use"], str) else None
    if used is None:
        raise ValueError(f"{where}: use names no step of the default workflow; it has "
                         f"{', '.join(st.id for st in DEFAULT_WORKFLOW.steps)}")
    return step_fields(used)


def _check_step_value(key: str, value, where: str) -> None:
    if key in STEP_FLAGS:
        if not isinstance(value, bool):
            raise ValueError(f"{where}: {key} must be true or false")
        return
    if not isinstance(value, str):
        raise ValueError(f"{where}: {key} must be a string")
    if key in ("id", "role") and not re.fullmatch(NAME_PATTERN, value):
        raise ValueError(f"{where}: {key} may use only letters, digits, - and _")
    try:
        _fields_of(value)
    except ValueError as e:
        raise ValueError(f"{where}: {key}: {e}; write a literal brace as {{{{ or }}}}") from None


def _check_keys(table: dict, known: set[str], where: str) -> None:
    for key in table:
        if key not in known:
            close = difflib.get_close_matches(key, known, n=1)
            hint = f"did you mean {close[0]}?" if close else f"it takes {', '.join(sorted(known))}"
            raise ValueError(f"{where}: unknown key {key}; {hint}")


def _default_label(role: str) -> str:
    return DEFAULT_WORKFLOW.roles.get(role) or re.sub(r"[-_]+", " ", role).title()


def step_fields(step: Step) -> dict:
    """The step's fields that differ from their defaults, in declaration order."""
    return {f.name: getattr(step, f.name) for f in fields(Step) if getattr(step, f.name) != f.default}


def workflow_definition(p: Pipeline) -> dict:
    """The canonical definition of a workflow, which parse_workflow turns back into it."""
    roles = {role: {"label": label, **{key: table[role] for key, table in (("model", p.models), ("agent", p.agents))
                                       if role in table}}
             for role, label in p.roles.items()}
    return {**({"description": p.description} if p.description else {}),
            "roles": roles, "steps": [step_fields(st) for st in p.steps]}


def workflow_toml(p: Pipeline) -> str:
    """The workflow as a workflow file."""
    d = workflow_definition(p)
    lines = [f"description = {_toml_string(d['description'])}"] if "description" in d else []
    for role, table in d["roles"].items():
        lines += ["", f"[roles.{role}]", *(f"{k} = {_toml_string(v)}" for k, v in table.items())]
    for st in d["steps"]:
        lines += ["", "[[steps]]"]
        lines += [f"{k} = {str(v).lower() if isinstance(v, bool) else _toml_string(v)}" for k, v in st.items()]
    return "\n".join(lines) + "\n"


def _toml_string(s: str) -> str:
    # A multi-line literal string keeps a prompt readable, but cannot hold ''' or end in a quote,
    # and holds no control characters besides tab and newline.
    if "\n" in s and "'''" not in s and not s.endswith("'") and \
            all(c in "\t\n" or " " <= c != "\x7f" for c in s):
        return f"'''\n{s}'''"
    # JSON's escapes are TOML's too, except that TOML also forbids a raw DEL.
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def workflow_shape(p: Pipeline) -> str:
    """The workflow's steps in order, e.g. spec -> build -> review (back to build)."""
    return " -> ".join(st.id + (f" (back to {st.loop_to})" if st.loop_to else "") for st in p.steps)


def run_pipeline(name: str, definition: dict | None) -> Pipeline:
    """A run's workflow: the definition saved with it, or the built-in workflow of its name."""
    if definition is None:
        return find_workflow(name)
    try:
        return parse_workflow(name, definition)
    except ValueError as e:
        raise OrchestratorError(f"the definition of workflow {name} saved with the run is invalid: {e}") from e


def saved_pipeline(saved: dict) -> Pipeline | None:
    """The workflow of a saved state, or None when this orchestrator cannot tell."""
    try:
        return run_pipeline(saved.get("workflow") or DEFAULT_WORKFLOW.name, saved.get("workflow_definition"))
    except OrchestratorError:
        return None


# ---------------------------------------------------------------------------
# Agent harnesses
# ---------------------------------------------------------------------------

# --permission-mode takes Claude Code's mode names, which claude gets as they are. These are the other
# harnesses' equivalents; a mode a harness's table lacks has none, and is not passed to it.
PERMISSION_ARGS = {
    "gemini": {"default": ["--approval-mode", "default"], "acceptEdits": ["--approval-mode", "auto_edit"],
               "bypassPermissions": ["--approval-mode", "yolo"]},
    # Codex asks before acting by default, so its default needs no flag.
    "codex": {"default": [], "acceptEdits": ["--full-auto"],
              "bypassPermissions": ["--dangerously-bypass-approvals-and-sandbox"], "plan": ["--sandbox", "read-only"]},
    "opencode": {},
    "pi": {},
}

# How each harness resumes a saved session; codex's is a subcommand, so it has to come first.
RESUME_ARGS = {"claude": "--resume", "gemini": "--resume", "pi": "--session", "opencode": "--session",
               "codex": "resume"}

# How each harness grants an agent a directory outside its working directory: a worktree run's handoff files
# are in the project's run directory, not the worktree. opencode and pi have no such flag.
ADD_DIR_ARGS = {"claude": "--add-dir", "codex": "--add-dir", "gemini": "--include-directories"}


@dataclass(frozen=True)
class AgentSettings:
    """How each role's agent starts: the permission mode, and by role its model and its harness (kinds).

    A role missing from models starts on its harness's default model, and one missing from kinds in DEFAULT_AGENT.
    """
    permission_mode: str | None = None
    models: dict[str, str] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)


def permission_args(kind: str, mode: str) -> list[str] | None:
    """The harness's arguments for a --permission-mode, or None when it has no equivalent."""
    if kind == "claude":
        return ["--permission-mode", mode]
    return PERMISSION_ARGS[kind].get(mode)


def launch_args(kind: str, permission_mode: str | None, model: str | None, session: str | None,
                extra_dir: str | None = None) -> list[str]:
    """The arguments a role's agent starts with in the harness: into its saved session, if given one, and with
    access to extra_dir where the harness can grant it."""
    args = (permission_args(kind, permission_mode) or []) if permission_mode else []
    if session:
        resume = [RESUME_ARGS[kind], session]
        args = resume + args if kind == "codex" else args + resume
    if model:
        args += ["--model", model]
    if extra_dir and kind in ADD_DIR_ARGS:
        args += [ADD_DIR_ARGS[kind], extra_dir]
    return args


# ---------------------------------------------------------------------------
# herdr
# ---------------------------------------------------------------------------

class Herdr:
    """The herdr CLI, forwarded to a saved SSH machine when one is given."""

    def __init__(self, machine: str | None = None, run=subprocess.run):
        self.machine = machine
        self._run = run

    def call(self, *args: str, limit: float | None = 120) -> dict:
        """Run one API-backed command and return its `result` object.

        `limit` bounds the CLI process in seconds; None lets it run as long as
        herdr does. Commands that wait pass herdr their own --timeout, and the
        limit only guards against a hung CLI.
        """
        prefix = ["--machine", self.machine] if self.machine else []
        out = self._exec(prefix + list(args), limit)
        try:
            return json.loads(out)["result"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise HerdrError("bad_response", f"{' '.join(args[:2])}: {e}") from e

    def _exec(self, args: list[str], limit: float | None = 120) -> str:
        try:
            proc = self._run(["herdr", *args], capture_output=True, text=True, timeout=limit, env=child_env())
        except FileNotFoundError as e:
            raise OrchestratorError("herdr is not on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise HerdrError("timeout", f"herdr {' '.join(args[:3])} did not return") from e
        if proc.returncode != 0:
            raise _herdr_error(proc)
        return proc.stdout

    def ssh_target(self) -> str:
        """The SSH target of the saved machine, for reaching its filesystem."""
        try:
            profiles = json.loads(self._exec(["machine", "list", "--json"]))
        except json.JSONDecodeError as e:
            raise HerdrError("bad_response", f"machine list: {e}") from e
        for p in profiles:
            if self.machine in (p.get("label"), p.get("id")):
                if not p.get("enabled"):
                    raise OrchestratorError(f"herdr machine {self.machine} is disabled")
                return p["target"]
        raise OrchestratorError(f"no saved herdr machine named {self.machine}")

    def create_workspace(self, cwd: str, label: str) -> tuple[str, str]:
        """Create a workspace; returns (workspace id, root pane id)."""
        r = self.call("workspace", "create", "--cwd", cwd, "--label", label, "--no-focus")
        return r["workspace"]["workspace_id"], r["root_pane"]["pane_id"]

    def split(self, pane: str, direction: str, cwd: str) -> str:
        r = self.call("pane", "split", pane, "--direction", direction, "--cwd", cwd, "--no-focus")
        return r["pane"]["pane_id"]

    def rename_pane(self, pane: str, label: str) -> None:
        self.call("pane", "rename", pane, label)

    def start_agent(self, name: str, pane: str, kind: str, agent_args: list[str]) -> bool:
        """Start an agent of the harness kind in the pane. False means it is blocked on a startup dialog."""
        args = ["agent", "start", name, "--kind", kind, "--pane", pane,
                "--timeout", str(AGENT_START_TIMEOUT_MS)]
        if agent_args:
            args += ["--", *agent_args]
        try:
            self.call(*args, limit=AGENT_START_TIMEOUT_MS / 1000 + 60)
        except HerdrError as e:
            # Such as Claude Code's folder-trust question in a directory it has not seen.
            # herdr keeps the name bound, so the agent can still be waited on.
            if e.code == "agent_not_ready":
                return False
            raise
        return True

    def prompt(self, name: str, text: str) -> None:
        self.call("agent", "prompt", name, text)

    def wait(self, name: str, timeout_ms: int | None, until: tuple[str, ...] = ()) -> str:
        args = ["agent", "wait", name]
        if timeout_ms is not None:
            args += ["--timeout", str(timeout_ms)]
        for status in until:
            args += ["--until", status]
        limit = None if timeout_ms is None else timeout_ms / 1000 + 60
        return self.call(*args, limit=limit)["agent"]["agent_status"]

    def agent(self, name: str) -> dict | None:
        """herdr's record of the agent, or None once it has exited."""
        try:
            return self.call("agent", "get", name)["agent"]
        except HerdrError as e:
            if e.code == "agent_not_found":
                return None
            raise

    def status(self, name: str) -> str | None:
        """The agent's lifecycle status, or None once it has exited."""
        agent = self.agent(name)
        return None if agent is None else agent["agent_status"]

    def workspace_exists(self, workspace: str) -> bool:
        return self._exists("workspace_not_found", "workspace", "get", workspace)

    def pane_exists(self, pane: str) -> bool:
        return self._exists("pane_not_found", "pane", "get", pane)

    def _exists(self, missing_code: str, *args: str) -> bool:
        try:
            self.call(*args)
        except HerdrError as e:
            if e.code == missing_code:
                return False
            raise
        return True

    def focus(self, name: str) -> None:
        self.call("agent", "focus", name)

    def close_workspace(self, workspace: str) -> None:
        self.call("workspace", "close", workspace)

    def notify(self, title: str, body: str) -> None:
        self.call("notification", "show", title, "--body", body, "--sound", "request")


def agent_session(agent: dict) -> str | None:
    """The session id herdr reports for an agent, once it knows it."""
    return (agent.get("agent_session") or {}).get("value")


def _herdr_error(proc: subprocess.CompletedProcess) -> HerdrError:
    # Server errors arrive as {"error": {"code", "message"}}; syntax errors (exit 2) as plain text.
    for stream in (proc.stderr, proc.stdout):
        try:
            err = json.loads(stream)["error"]
            return HerdrError(err["code"], err["message"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    text = (proc.stderr or proc.stdout).strip()
    return HerdrError(f"exit_{proc.returncode}", text)


# ---------------------------------------------------------------------------
# Host: the filesystem the agents work in
# ---------------------------------------------------------------------------

MISSING_FILE_STATUS = 3
# gh's exit status when it is not logged in, and sh's when gh is not installed: no gh call can succeed then.
GH_UNUSABLE_STATUS = (4, 127)
# Runs a command in a directory, for gh, which finds the repository from its working directory and has no -C.
IN_DIR = 'cd -- "$1" && shift && exec "$@"'


class GhUnusable(OrchestratorError):
    """gh is missing or logged out where the project is, so no gh call can succeed."""


class Host:
    """Runs commands on the machine where the agents run: locally, or over SSH."""

    def __init__(self, ssh_target: str | None = None, run=subprocess.run):
        self.ssh_target = ssh_target
        self._run = run

    def run(self, argv: list[str], stdin: str | None = None,
            timeout: float = 60) -> subprocess.CompletedProcess:
        if self.ssh_target:
            # BatchMode fails fast instead of hanging on a password prompt nobody can see.
            argv = ["ssh", "-o", "BatchMode=yes", self.ssh_target, shlex.join(argv)]
        try:
            return self._run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, env=child_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OrchestratorError(f"{shlex.join(argv)}: {e}") from e

    def check(self, argv: list[str], stdin: str | None = None, timeout: float = 60) -> str:
        return self._checked(argv, self.run(argv, stdin, timeout))

    def _checked(self, argv: list[str], proc: subprocess.CompletedProcess) -> str:
        if proc.returncode != 0:
            where = f" on {self.ssh_target}" if self.ssh_target else ""
            raise OrchestratorError(f"{shlex.join(argv)} failed{where}: {proc.stderr.strip()}")
        return proc.stdout

    def read(self, path: str) -> str | None:
        """The file's contents, or None when it does not exist yet."""
        script = f'[ -f "$1" ] || exit {MISSING_FILE_STATUS}; cat -- "$1"'
        proc = self.run(["sh", "-c", script, "_", path])
        if proc.returncode == MISSING_FILE_STATUS:
            return None
        if proc.returncode != 0:
            raise OrchestratorError(f"reading {path}: {proc.stderr.strip()}")
        return proc.stdout

    def write(self, path: str, text: str, keep_mtime: bool = False) -> None:
        """Replace the file's contents; with keep_mtime, the existing file keeps its modification time.

        keep_mtime is for a state.json written by someone other than its run: its age is the run's heartbeat.
        """
        # Written aside and renamed into place, so a concurrent `list` never reads half a state.json.
        keep = ' && touch -r "$1" -- "$t"' if keep_mtime else ""
        script = (f't="$1.tmp.$$"; mkdir -p -- "$(dirname -- "$1")" && cat > "$t"{keep} && mv -f -- "$t" "$1" '
                  '|| { rm -f -- "$t"; exit 1; }')
        self.check(["sh", "-c", script, "_", path], stdin=text)

    def rename(self, path: str, new_path: str) -> None:
        self.check(["mv", "-f", "--", path, new_path])

    def resolve_dir(self, path: str) -> str:
        """Absolute form of a directory path, expanding a leading ~ on the host."""
        if path == "~" or path.startswith("~/"):
            script, arg = 'cd -- "$HOME$1" && pwd', path[1:]
        else:
            script, arg = 'cd -- "$1" && pwd', path
        return self.check(["sh", "-c", script, "_", arg]).strip()

    def git_head(self, cwd: str) -> str | None:
        """HEAD's commit, or None when cwd is not a git repository with a commit."""
        proc = self.run(["git", "-C", cwd, "rev-parse", "--verify", "HEAD"])
        return proc.stdout.strip() if proc.returncode == 0 else None

    def git(self, cwd: str, *args: str, timeout: float = 60) -> str:
        return self._checked(["git", "-C", cwd, *args], self.git_run(cwd, *args, timeout=timeout))

    def git_run(self, cwd: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        """A git command whose failure the caller interprets; git uses exit status 1 for answers such as "no"."""
        return self.run(["git", "-C", cwd, *args], timeout=timeout)

    def _git_test(self, cwd: str, *args: str) -> bool:
        """Whether a yes-or-no git command answers yes: status 0 is yes, 1 is no, anything else an error."""
        proc = self.git_run(cwd, *args)
        if proc.returncode not in (0, 1):
            self._checked(["git", "-C", cwd, *args], proc)
        return proc.returncode == 0

    def fetch(self, cwd: str, branch: str) -> str:
        """Fetch the branch from origin and return its remote-tracking ref."""
        try:
            self.git(cwd, "fetch", "--quiet", REMOTE, branch, timeout=NETWORK_TIMEOUT)
        except OrchestratorError as e:
            raise OrchestratorError(f"could not fetch {branch} from {REMOTE}: {e}") from e
        return f"{REMOTE}/{branch}"

    def fast_forward(self, cwd: str, branch: str) -> None:
        """Bring the checked-out branch up to origin's. One only ahead of origin's is left as it is."""
        upstream = self.fetch(cwd, branch)
        argv = ["merge", "--ff-only", "--quiet", upstream]
        proc = self.git_run(cwd, *argv)
        if proc.returncode != 0:
            if self._git_test(cwd, "merge-base", "--is-ancestor", "HEAD", upstream):
                why = f"{branch} cannot be fast-forwarded to {upstream}"
            else:
                why = f"{branch} has diverged from {upstream}"
            raise OrchestratorError(f"{why}; reconcile it first. "
                                    f"git {shlex.join(argv)} failed: {proc.stderr.strip()}")

    def merge_upstream(self, cwd: str, branch: str) -> list[str]:
        """Merge origin's latest branch into HEAD with a merge commit, and return the files that conflict.

        Nothing is merged when HEAD already contains it, so a repeated call merges once. A
        conflicting merge is aborted, leaving HEAD and the working tree as they were.
        """
        upstream = self.fetch(cwd, branch)
        if self._git_test(cwd, "merge-base", "--is-ancestor", upstream, "HEAD"):
            return []
        merge = self.git_run(cwd, "merge", "--no-edit", "--quiet", upstream)
        if merge.returncode == 0:
            return []
        # -z: paths with unusual characters come unquoted.
        conflicts = [p for p in self.git(cwd, "diff", "--name-only", "-z", "--diff-filter=U").split("\0") if p]
        self.abort_merge(cwd)
        if not conflicts:
            raise OrchestratorError(f"git merge {upstream} failed in {cwd}: {merge.stderr.strip()}")
        return conflicts

    def abort_merge(self, cwd: str) -> bool:
        """Abort a merge in progress, if there is one; True when there was."""
        if not self._git_test(cwd, "rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return False
        self.git(cwd, "merge", "--abort")
        return True

    def snapshot(self, cwd: str, parent: str, message: str) -> str:
        """Commit the working tree as it is, untracked files included, on top of parent; returns the commit.

        It goes through a copy of the index, so no ref moves and the index stays as it was. `git add -A`
        honours .gitignore, which keeps .orchestrator/ out, so the snapshot holds what committing the
        change would. One sh -c script, because git -C cannot pass GIT_INDEX_FILE.
        """
        script = ('cd -- "$1" || exit 1; i=$(git rev-parse --git-path index) || exit 1; t=$(mktemp) || exit 1; '
                  'trap \'rm -f -- "$t"\' EXIT; '
                  # A repository whose index was never written: git starts the copy from nothing.
                  'if [ -f "$i" ]; then cp -- "$i" "$t" || exit 1; else rm -f -- "$t"; fi; '
                  'GIT_INDEX_FILE=$t git add -A && tree=$(GIT_INDEX_FILE=$t git write-tree) && '
                  'git commit-tree "$tree" -p "$2" -m "$3"')
        sha = self.check(["sh", "-c", script, "_", cwd, parent, message]).strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise OrchestratorError(f"snapshot of {cwd}: git commit-tree printed {sha!r}, not a commit")
        return sha

    def change_diff(self, cwd: str, base: str, commit: str) -> str:
        """`git diff -U0` from base to commit, in the one shape changed_lines parses, whatever the user's git config."""
        return self.git(cwd, "-c", "core.quotePath=false", "diff", "-U0", "-M", "--no-color", "--no-ext-diff",
                        "--src-prefix=a/", "--dst-prefix=b/", base, commit)

    def push_ref(self, cwd: str, commit: str, branch: str) -> None:
        # --force replaces what a failed attempt left under the same name.
        self.git(cwd, "push", "--quiet", "--force", REMOTE, f"{commit}:refs/heads/{branch}", timeout=NETWORK_TIMEOUT)

    def delete_remote_branch(self, cwd: str, branch: str) -> None:
        self.git(cwd, "push", "--quiet", REMOTE, "--delete", f"refs/heads/{branch}", timeout=NETWORK_TIMEOUT)

    def remote_branches(self, cwd: str, prefix: str) -> list[str]:
        """origin's branches whose names start with prefix."""
        out = self.git(cwd, "ls-remote", "--heads", REMOTE, timeout=NETWORK_TIMEOUT)
        refs = [line.split("\t", 1)[1] for line in out.splitlines() if "\t" in line]
        return [r.removeprefix("refs/heads/") for r in refs if r.startswith(f"refs/heads/{prefix}")]

    def remote_branch_commit(self, cwd: str, branch: str) -> str | None:
        """The commit origin's branch is at; None when origin has no such branch."""
        ref = f"refs/heads/{branch}"
        out = self.git(cwd, "ls-remote", REMOTE, ref, timeout=NETWORK_TIMEOUT)
        # ls-remote matches its pattern against the end of each ref's name, so the exact name is picked out.
        for line in out.splitlines():
            commit, _, name = line.partition("\t")
            if name == ref:
                return commit
        return None

    def delete_remote_branch_at(self, cwd: str, branch: str, commit: str) -> None:
        """Delete origin's branch while it is at commit; the lease fails the push if anyone pushed since."""
        ref = f"refs/heads/{branch}"
        self.git(cwd, "push", "--quiet", f"--force-with-lease={ref}:{commit}", REMOTE, "--delete", ref,
                 timeout=NETWORK_TIMEOUT)

    def delete_tracking_ref(self, cwd: str, branch: str) -> None:
        """Drop origin/<branch>, which outlives the branch on origin until a fetch prunes it; none is fine."""
        self.git(cwd, "update-ref", "-d", f"refs/remotes/{REMOTE}/{branch}")

    def branch_commit(self, cwd: str, branch: str) -> str | None:
        """The commit the local branch is at; None when there is no such branch."""
        argv = ["rev-parse", "-q", "--verify", f"refs/heads/{branch}"]
        proc = self.git_run(cwd, *argv)
        if proc.returncode == 1:
            return None
        return self._checked(["git", "-C", cwd, *argv], proc).strip()

    def has_commit(self, cwd: str, commit: str) -> bool:
        return self._git_test(cwd, "rev-parse", "-q", "--verify", f"{commit}^{{commit}}")

    def is_ancestor(self, cwd: str, commit: str, of: str) -> bool:
        """Whether commit is of or one of its ancestors."""
        return self._git_test(cwd, "merge-base", "--is-ancestor", commit, of)

    def commits_beyond(self, cwd: str, commit: str, branch: str) -> int:
        """How many commits the local branch has that commit lacks."""
        out = self.git(cwd, "rev-list", "--count", f"{commit}..refs/heads/{branch}").strip()
        if not out.isdigit():
            raise OrchestratorError(f"git rev-list --count {commit}..refs/heads/{branch} printed {out!r}, not a count")
        return int(out)

    def delete_branch(self, cwd: str, branch: str) -> None:
        # -D: -d checks the branch against HEAD or its upstream, not its pull request, so it refuses a squash merge.
        self.git(cwd, "branch", "--quiet", "-D", branch)

    def add_worktree(self, cwd: str, path: str, commit: str) -> None:
        """Check commit out, on a detached HEAD, in a new worktree of cwd's repository at path."""
        self.git(cwd, "worktree", "add", "--detach", path, commit)

    def is_worktree(self, cwd: str, path: str) -> bool:
        """Whether path is a worktree of cwd's repository, registered and still there."""
        # git lists a worktree by its physical path, which differs from path when path goes through a symlink.
        proc = self.run(["sh", "-c", 'cd -P -- "$1" && pwd -P', "_", path])
        if proc.returncode != 0:
            return False
        out = self.git(cwd, "worktree", "list", "--porcelain")
        # One record per worktree, separated by a blank line; "prunable" marks one whose directory is gone.
        records = [r.splitlines() for r in out.split("\n\n")]
        return any(r and r[0] == f"worktree {proc.stdout.strip()}" and not any(x.startswith("prunable") for x in r)
                   for r in records)

    def remove_worktree(self, cwd: str, path: str) -> None:
        """Remove the worktree at path; without --force, git refuses one with changes it would lose."""
        self.git(cwd, "worktree", "remove", path)

    def checked_out_branches(self, cwd: str) -> dict[str, str]:
        """The branches checked out in the repository's worktrees, the main checkout's included, with each path."""
        branches, path = {}, ""
        for line in self.git(cwd, "worktree", "list", "--porcelain").splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                path = value
            elif key == "branch":
                branches[value.removeprefix("refs/heads/")] = path
        return branches

    def gh_run(self, cwd: str, *args: str) -> subprocess.CompletedProcess:
        """A gh command in cwd whose failure the caller interprets."""
        return self.run(["sh", "-c", IN_DIR, "_", cwd, "gh", *args], timeout=NETWORK_TIMEOUT)

    def pull_request(self, cwd: str, url: str) -> dict:
        """The pull request's state (OPEN, CLOSED or MERGED), headRefOid and closedAt, as gh reports them."""
        args = ["pr", "view", url, "--json", "state,headRefOid,closedAt"]
        proc = self.gh_run(cwd, *args)
        if proc.returncode in GH_UNUSABLE_STATUS:
            raise GhUnusable(f"gh {shlex.join(args)} failed with status {proc.returncode}: "
                             f"{(proc.stderr or proc.stdout).strip()}")
        out = self._checked(["gh", *args], proc)
        try:
            pr = json.loads(out)
            fields_ok = (isinstance(pr["state"], str) and re.fullmatch(r"[0-9a-f]{40,64}", pr["headRefOid"])
                         and (pr["closedAt"] is None or isinstance(pr["closedAt"], str)))
        except (ValueError, KeyError, TypeError) as e:
            raise OrchestratorError(f"gh pr view {url} printed {out.strip()!r}: {e}") from e
        if not fields_ok:
            raise OrchestratorError(f"gh pr view {url} printed {out.strip()!r}, not a pull request's state")
        return pr

    def create_pr(self, cwd: str, base: str, head: str, title: str, body: str, draft: bool) -> str:
        """Open a pull request with gh and return its URL."""
        argv = ["gh", "pr", "create", "--base", base, "--head", head,
                "--title", title, "--body-file", "-"]
        if draft:
            argv.append("--draft")
        out = self.check(["sh", "-c", IN_DIR, "_", cwd, *argv], stdin=body, timeout=NETWORK_TIMEOUT)
        lines = out.split()
        if not lines:
            raise OrchestratorError(f"gh pr create printed no URL for {head}")
        return lines[-1]

    def run_states(self, cwd: str) -> list[tuple[int, dict]]:
        """Each run's saved state, with the seconds since its state.json was last written.

        The age is measured by this host's own clock, the one that stamped the file,
        so clock skew between the machines involved cannot shift it.
        """
        # stat -c is GNU, stat -f is BSD.
        script = (f'now=$(date +%s) || exit 1; for f in "$1"/{RUNS_DIR}/*/state.json; do [ -f "$f" ] || continue; '
                  'm=$(stat -c %Y -- "$f" 2>/dev/null || stat -f %m -- "$f") || exit 1; '
                  'printf "%s " "$((now - m))"; cat -- "$f"; echo; done; true')
        out = self.check(["sh", "-c", script, "_", cwd])
        runs = []
        for line in out.splitlines():
            if not line.strip():
                continue
            age, _, text = line.partition(" ")
            try:
                runs.append((max(0, int(age)), json.loads(text)))
            except ValueError as e:  # JSONDecodeError is a ValueError
                raise OrchestratorError(f"corrupt run state under {cwd}/{RUNS_DIR}: {e}") from e
        return runs

    def run_files(self, run_dir: str) -> list[str]:
        """The paths of the files in a run directory, oldest first, except state.json and write's temp files."""
        # Each entry is "<mtime> <path>" ended by a NUL, which no path holds. stat -c is GNU, stat -f is BSD.
        script = ('for f in "$1"/*; do [ -f "$f" ] || continue; '
                  'case "${f##*/}" in state.json|*.tmp.*) continue;; esac; '
                  'm=$(stat -c %Y -- "$f" 2>/dev/null || stat -f %m -- "$f") || exit 1; '
                  'printf "%s %s\\0" "$m" "$f"; done; true')
        out = self.check(["sh", "-c", script, "_", run_dir])
        files = []
        for entry in out.split("\0"):
            if not entry:
                continue
            mtime, _, path = entry.partition(" ")
            try:
                files.append((int(mtime), path))
            except ValueError as e:
                raise OrchestratorError(f"listing {run_dir}: stat printed {mtime!r}, not a time") from e
        return [path for _, path in sorted(files)]


# ---------------------------------------------------------------------------
# CI: Jenkins and SonarQube, for the quality gate
# ---------------------------------------------------------------------------

class CIError(OrchestratorError):
    """A request to Jenkins or SonarQube that failed; status is the HTTP status, None when no answer came."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        # No answer, or the server's own trouble, may pass; a 4xx answer will not change by asking again.
        return self.status is None or self.status >= 500


class CI:
    """Jenkins's quality job and SonarQube, over HTTP from the orchestrator's own machine.

    The credentials come from the orchestrator's environment or ci_env_path() and travel only in the
    Authorization header, never in a URL, so no error, log or state.json can carry them. Errors name the
    method, the URL and the status, and text from either server goes through mask before it is shown or saved.
    """

    def __init__(self, job: str, env=None, urlopen=None):
        self.job = job
        env = ci_credentials(os.environ, ci_env_path()) if env is None else env
        self._env = {k: env.get(k) or "" for k in CI_ENV}
        self._urlopen = urlopen or urllib.request.urlopen

    @property
    def job_url(self) -> str:
        # A job in a folder is folder/job in Jenkins's full name and /job/folder/job/job/ in its URL.
        path = "".join(f"/job/{urllib.parse.quote(part, safe='')}" for part in self.job.split("/"))
        return f"{self._base('JENKINS_URL')}{path}/"

    def _base(self, name: str) -> str:
        return self._env[name].rstrip("/")

    def mask(self, text: str) -> str:
        """text with each token replaced by ****."""
        for name in CI_TOKENS:
            if token := self._env[name]:
                text = text.replace(token, "****")
        return text

    def check_credentials(self) -> None:
        if missing := [k for k in CI_ENV if not self._env[k]]:
            raise OrchestratorError(f"the quality gate needs {', '.join(missing)} in the orchestrator's environment "
                                    f"or in {ci_env_path()}")

    def _request(self, method: str, url: str, user: str, password: str,
                 form: dict | None = None) -> tuple[bytes, dict]:
        """The body and headers of a 2xx answer; CIError otherwise."""
        self.check_credentials()
        data = None if form is None else urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(url, data=data, method=method)
        auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        # Unredirected: a redirect to another server does not take the credentials along.
        req.add_unredirected_header("Authorization", f"Basic {auth}")
        try:
            with self._urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            raise CIError(self.mask(f"{method} {url}: HTTP {e.code}{_server_message(e)}"), e.code) from e
        # URLError, a timeout and a dropped connection are all OSErrors.
        except (OSError, http.client.HTTPException) as e:
            reason = e.reason if isinstance(e, urllib.error.URLError) else e
            raise CIError(self.mask(f"{method} {url}: {reason}")) from e

    def _json(self, method: str, url: str, body: bytes) -> dict:
        try:
            out = json.loads(body)
        except ValueError as e:  # JSONDecodeError and UnicodeDecodeError
            raise CIError(self.mask(f"{method} {url}: the answer is not JSON: {e}")) from e
        if not isinstance(out, dict):
            raise CIError(self.mask(f"{method} {url}: the answer is not a JSON object"))
        return out

    def _jenkins(self, method: str, url: str, form: dict | None = None) -> tuple[bytes, dict]:
        return self._request(method, url, self._env["JENKINS_USER"], self._env["JENKINS_TOKEN"], form)

    def _jenkins_json(self, url: str) -> dict:
        body, _ = self._jenkins("GET", url)
        return self._json("GET", url, body)

    def _build_url(self, number: int, path: str, tree: str | None = None) -> str:
        query = f"?{urllib.parse.urlencode({'tree': tree})}" if tree else ""
        return f"{self.job_url}{number}/{path}{query}"

    def _sonar(self, method: str, path: str, params: dict | None = None) -> dict:
        """SonarQube's JSON answer, {} for an empty one. A GET sends params in the query, a POST as a form."""
        url = f"{self._base('SONAR_HOST_URL')}{path}"
        if method == "GET" and params:
            url += f"?{urllib.parse.urlencode(params)}"
        form = (params or {}) if method == "POST" else None
        # SonarQube takes a token as the user name, with an empty password.
        body, _ = self._request(method, url, self._env["SONAR_TOKEN"], "", form)
        return self._json(method, url, body) if body.strip() else {}

    def preflight(self) -> None:
        """Fail before the interview if the gate could not run: the credentials, both servers and the job's parameters."""
        self.check_credentials()
        url = f"{self.job_url}api/json?{urllib.parse.urlencode({'tree': 'property[parameterDefinitions[name]]'})}"
        try:
            job = self._jenkins_json(url)
        except CIError as e:
            if e.status == 404:
                raise OrchestratorError(f"there is no Jenkins job {self.job} at JENKINS_URL: {e}") from e
            raise OrchestratorError(f"Jenkins at JENKINS_URL failed the preflight: {e}") from e
        defined = {d.get("name") for p in job.get("property") or [] if isinstance(p, dict)
                   for d in p.get("parameterDefinitions") or []}
        if missing := [p for p in JOB_PARAMETERS if p not in defined]:
            raise OrchestratorError(
                f"Jenkins job {self.job} lacks the parameters {', '.join(missing)}; build it once by hand with "
                "empty parameters, so Jenkins learns them from the Jenkinsfile")
        try:
            valid = self._sonar("GET", "/api/authentication/validate").get("valid")
        except CIError as e:
            raise OrchestratorError(f"SonarQube at SONAR_HOST_URL failed the preflight: {e}") from e
        if valid is not True:
            raise OrchestratorError("SonarQube at SONAR_HOST_URL does not accept SONAR_TOKEN")

    def create_project(self, key: str, template: str) -> None:
        """Create the run's project, with the template project's quality gate and the previous version as new code."""
        gate = self._sonar("GET", "/api/qualitygates/get_by_project", {"project": template}).get("qualityGate")
        if not isinstance(gate, dict) or not gate.get("name"):
            raise OrchestratorError(f"SonarQube names no quality gate for {template}")
        try:
            self._sonar("POST", "/api/projects/create", {"project": key, "name": key})
        except CIError as e:
            # An attempt whose answer was lost, or an earlier orchestrator, already created it.
            if e.status != 400 or not self._project_exists(key):
                raise
        self._sonar("POST", "/api/qualitygates/select", {"gateName": gate["name"], "projectKey": key})
        self._sonar("POST", "/api/new_code_periods/set", {"project": key, "type": "PREVIOUS_VERSION"})

    def _project_exists(self, key: str) -> bool:
        try:
            self._sonar("GET", "/api/components/show", {"component": key})
        except CIError as e:
            if e.status == 404:
                return False
            raise
        return True

    def delete_project(self, key: str) -> None:
        self._sonar("POST", "/api/projects/delete", {"project": key})

    def trigger(self, ref: str, project: str, version: str) -> str:
        """Queue a build of the branch ref that analyses it into project as version; returns the queue item's URL."""
        url = f"{self.job_url}buildWithParameters"
        params = {"GIT_REF": ref, "SONAR_PROJECT_KEY": project, "SONAR_PROJECT_VERSION": version}
        try:
            _, headers = self._jenkins("POST", url, params)
        except CIError as e:
            if e.status == 404:
                raise OrchestratorError(f"Jenkins job {self.job} is gone: {e}") from e
            raise
        m = re.search(r"/queue/item/(\d+)/?$", headers.get("Location") or "")
        if not m:
            raise OrchestratorError(f"POST {url} named no queue item")
        # Rebuilt from JENKINS_URL: behind a proxy, Location may name a host only Jenkins itself can reach.
        return f"{self._base('JENKINS_URL')}/queue/item/{m.group(1)}/"

    def queue_item(self, queue_url: str) -> dict:
        """The queue item: its build is executable.number once one started; cancelled is true if it never will."""
        return self._jenkins_json(f"{queue_url}api/json")

    def find_build(self, ref: str, skip=()) -> int | None:
        """The newest of the job's last 20 builds whose GIT_REF is ref, leaving out the numbers in skip."""
        tree = "builds[number,actions[parameters[name,value]]]{0,20}"
        for build in self._jenkins_json(f"{self.job_url}api/json?{urllib.parse.urlencode({'tree': tree})}"
                                        ).get("builds") or []:
            params = {p.get("name"): p.get("value")
                      for a in build.get("actions") or [] if isinstance(a, dict) for p in a.get("parameters") or []}
            number = build.get("number")
            if params.get("GIT_REF") == ref and isinstance(number, int) and number not in skip:
                return number
        return None

    def build_result(self, number: int) -> str | None:
        """The finished build's result, such as SUCCESS, UNSTABLE, FAILURE or ABORTED; None while it runs."""
        build = self._jenkins_json(self._build_url(number, "api/json", "building,result"))
        return None if build.get("building") else build.get("result")

    def report_task(self, number: int) -> str | None:
        """The SonarQube task id in the build's archived report-task.txt; None when the build archived none."""
        try:
            body, _ = self._jenkins("GET", self._build_url(number, "artifact/.scannerwork/report-task.txt"))
        except CIError as e:
            if e.status == 404:
                return None
            raise
        m = re.search(r"^ceTaskId=(\S+)", body.decode(errors="replace"), re.M)
        return m.group(1) if m else None

    def failed_tests(self, number: int) -> list[tuple[str, str]] | None:
        """(test, error) for each failing test of the build; None when the build has no test report."""
        tree = "suites[cases[className,name,status,errorDetails]]"
        try:
            report = self._jenkins_json(self._build_url(number, "testReport/api/json", tree))
        except CIError as e:
            if e.status == 404:
                return None
            raise
        return [(f"{c.get('className')}.{c.get('name')}", c.get("errorDetails") or "")
                for suite in report.get("suites") or [] for c in suite.get("cases") or []
                if c.get("status") in ("FAILED", "REGRESSION")]

    def console_tail(self, number: int) -> str:
        body, _ = self._jenkins("GET", self._build_url(number, "consoleText"))
        return "\n".join(body.decode(errors="replace").splitlines()[-CONSOLE_TAIL_LINES:])

    def ce_task(self, task: str) -> dict:
        """The SonarQube task: its status, and its analysisId once the status is SUCCESS."""
        return self._sonar("GET", "/api/ce/task", {"id": task}).get("task") or {}

    def gate_status(self, analysis: str) -> dict:
        """The analysis's quality gate: its status and its conditions."""
        return self._sonar("GET", "/api/qualitygates/project_status", {"analysisId": analysis}).get("projectStatus") or {}

    def issues(self, project: str) -> list[dict]:
        """The project's open issues as {path, line, severity, rule, message}; path is None for the project's own."""
        found, page = [], 1
        while True:
            out = self._sonar("GET", "/api/issues/search", {"components": project, "resolved": "false",
                                                             "ps": ISSUE_PAGE_SIZE, "p": page})
            batch = out.get("issues") or []
            found += batch
            total = (out.get("paging") or {}).get("total", out.get("total", 0))
            if not batch or len(found) >= min(total, ISSUE_SEARCH_LIMIT):
                break
            page += 1
        prefix = f"{project}:"
        return [{"path": i["component"][len(prefix):] if str(i.get("component", "")).startswith(prefix) else None,
                 "line": i.get("line"), "severity": issue_severity(i),
                 "rule": i.get("rule") or "", "message": i.get("message") or ""} for i in found]

    def coverage(self, project: str) -> dict[str, str]:
        """The coverage measures of new code, by metric."""
        out = self._sonar("GET", "/api/measures/component",
                          {"component": project, "metricKeys": ",".join(COVERAGE_METRICS)})
        values = {}
        for m in (out.get("component") or {}).get("measures") or []:
            # A new-code measure is a value, or in older versions a period's or the first of its periods.
            periods = m.get("periods") or [{}]
            value = m.get("value") or (m.get("period") or {}).get("value") or periods[0].get("value")
            if value is not None:
                values[m.get("metric")] = value
        return values


def _server_message(e: urllib.error.HTTPError) -> str:
    """SonarQube's reason for refusing a request, from its {"errors": [{"msg"}]} body; "" for anything else."""
    try:
        errors = json.loads(e.read()).get("errors") or []
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return ""
    msgs = [x["msg"] for x in errors if isinstance(x, dict) and isinstance(x.get("msg"), str)]
    return f" ({'; '.join(msgs)})" if msgs else ""


IMPACT_SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "BLOCKER")


def issue_severity(issue: dict) -> str:
    """The issue's highest impact severity, or its older single severity when it lists no impacts."""
    impacts = [i.get("severity") for i in issue.get("impacts") or []
               if isinstance(i, dict) and i.get("severity") in IMPACT_SEVERITIES]
    if impacts:
        return max(impacts, key=IMPACT_SEVERITIES.index)
    return issue.get("severity") or "UNKNOWN"


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@dataclass
class RunState:
    """What a run has done so far, saved as state.json in its run directory."""
    run_id: str
    task: str
    cwd: str
    machine: str | None = None
    # The current step's id, or quality, publish or done. main starts a run at its workflow's first step.
    phase: str = "spec"
    round: int = 0
    workspace_id: str = ""
    # The first role's pane, which the second role's pane is split from.
    root_pane: str = ""
    base: str | None = None
    base_branch: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    verdict: str | None = None
    # The files that conflicted with the base branch when the pull request was opened, as a draft.
    conflicts: list[str] = field(default_factory=list)
    error: str | None = None
    # Per role: the agent's name, its pane, the harness it runs in (kind) and, once herdr reports it,
    # its session id. A record without a kind is a claude agent's, saved before there were others.
    agents: dict[str, dict[str, str]] = field(default_factory=dict)
    max_rounds: int = DEFAULT_MAX_ROUNDS
    turn_timeout: int = DEFAULT_TURN_TIMEOUT
    # As --permission-mode names it; each harness's agent gets its own equivalent (permission_args).
    permission_mode: str | None = None
    models: dict[str, str] = field(default_factory=dict)
    # Per role: the harness its next agent starts in. A role missing here runs in DEFAULT_AGENT.
    agent_kinds: dict[str, str] = field(default_factory=dict)
    # False by default because a run saved before pull requests existed never switched to its own branch.
    pull_request: bool = False
    # Basename of the handoff file whose prompt was delivered, so a resume does not prompt for it again.
    prompted: str | None = None
    # Basenames of the verdict files whose one retry was used, and the one whose retry turn is under way, so a
    # resume neither grants a second retry nor sends the step's own prompt in place of the retry prompt.
    retried: list[str] = field(default_factory=list)
    retrying: str | None = None
    # The orchestrator process driving the run, {"host", "pid", "started_at"}; None while none does.
    owner: dict | None = None
    heartbeat_at: str | None = None
    # The Jenkins job that runs the quality gate; None for a run without the gate, as for one saved before it.
    quality_job: str | None = None
    max_quality_rounds: int = DEFAULT_MAX_QUALITY_ROUNDS
    # The quality round the round is in: 0 until the Builder's first report, then q from quality round q on.
    quality_round: int = 0
    # The run's SonarQube project, once created and configured, and the analysis of the base it is judged against.
    quality_project: str | None = None
    quality_baseline: str | None = None
    # The analysis in flight, {ref, sha, queue_url, build, ce_task}, each saved as soon as it is known.
    # rejected lists builds of ref that ended without a verdict, so a resume does not adopt them again.
    ci: dict = field(default_factory=dict)
    # The name of the run's workflow; a run saved before workflows existed ran the default.
    workflow: str = DEFAULT_WORKFLOW.name
    # The workflow_definition of one that is not built in, so a resume does not depend on its file.
    workflow_definition: dict | None = None
    # What prune did with branch once the pull request was done: "deleted", or "kept: <why>". Written by
    # prune_branches, never by the run itself.
    branch_cleanup: str | None = None
    # A run --worktree run's own git worktree, by its absolute path, from the run's start on: round 0 creates it
    # there. None for a run in the project's checkout, cwd. cwd stays the project, where the run directory is.
    worktree: str | None = None

    @classmethod
    def from_dict(cls, saved: dict) -> "RunState":
        """A saved state, including one written before some of these fields existed."""
        known = {f.name for f in fields(cls)}
        try:
            state = cls(**{k: v for k, v in saved.items() if k in known})
        except TypeError as e:
            raise OrchestratorError(f"run state {saved.get('run_id', '?')} is incomplete: {e}") from e
        if "permission_mode" not in saved:
            state.permission_mode = _saved_permission_mode(saved)
        pipeline = saved_pipeline(saved)
        first = next(iter(pipeline.roles)) if pipeline else None
        if not state.root_pane and first in state.agents:
            state.root_pane = state.agents[first]["pane"]
        return state

    @property
    def key(self) -> str:
        """The run id's random suffix, which names its agents and labels its workspace."""
        return self.run_id.rsplit("-", 1)[-1]

    @property
    def dir(self) -> str:
        return f"{self.cwd}/{RUNS_DIR}/{self.run_id}"

    @property
    def work_dir(self) -> str:
        """Where the agents work and the run's git steps run: its worktree, or else the project's checkout."""
        return self.worktree or self.cwd

    # The default workflow's handoff files; the quality files are every workflow's.
    @property
    def spec_path(self) -> str:
        return f"{self.dir}/spec.md"

    def build_path(self, n: int) -> str:
        return f"{self.dir}/build-{n}.md"

    def review_path(self, n: int) -> str:
        return f"{self.dir}/review-{n}.md"

    def quality_path(self, n: int, q: int) -> str:
        return f"{self.dir}/quality-{n}-{q}.md"

    def quality_build_path(self, n: int, q: int) -> str:
        """The Builder's answer to quality-<n>-<q>.md."""
        return f"{self.dir}/build-{n}-q{q}.md"

    def builder_report_path(self, n: int, q: int) -> str:
        """The report of the Builder's turn in round n that answers quality round q, or none when q is 0."""
        return self.quality_build_path(n, q) if q else self.build_path(n)

    def last_report_path(self, n: int) -> str:
        """The Builder's last report in round n, once its quality rounds are over: the one the Reviewer reads."""
        return self.builder_report_path(n, max(self.quality_round - 1, 0))


def _saved_permission_mode(saved: dict) -> str | None:
    """The permission mode of a state saved by 0.3.0, which kept it as Claude Code's agent_args."""
    args = saved.get("agent_args") or []
    if not args:
        return None
    if len(args) != 2 or args[0] != "--permission-mode":
        raise OrchestratorError(f"run state {saved.get('run_id', '?')} has agent_args {args}, "
                                f"which name no permission mode")
    return args[1]


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def parse_verdict(review: str) -> str | None:
    """The verdict on the review's first non-blank line, tolerating Markdown emphasis around it."""
    for line in review.splitlines():
        if line.strip():
            m = re.search(rf"VERDICT:\s*({APPROVE}|{CHANGES_REQUESTED})\b", line)
            return m.group(1) if m else None
    return None


def parse_gate(quality: str) -> str | None:
    """The gate on the quality file's first non-blank line, tolerating Markdown emphasis around it."""
    for line in quality.splitlines():
        if line.strip():
            m = re.search(rf"GATE:\s*({GATE_OK}|{GATE_ERROR}|{GATE_BUILD_FAILED})\b", line)
            return m.group(1) if m else None
    return None


def changed_lines(diff: str) -> dict[str, set[int]]:
    """The lines each file gains in a `git diff -U0`, by its path in the new tree.

    A file the diff touches without adding a line, such as a pure rename, one that only loses lines
    or an empty new file, maps to an empty set; a deleted file is left out. Only `diff --git` and
    `@@` lines are read inside a hunk, since an added line can itself start with `+++ `.
    """
    out: dict[str, set[int]] = {}
    path, in_hunk = None, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path, in_hunk = _note_path(out, _diff_git_path(line[len("diff --git "):])), False
        elif line.startswith("@@"):
            in_hunk = True
            if path is not None:
                out[path].update(_hunk_lines(line))
        elif not in_hunk:
            path = _header_path(out, path, line)
    return out


def _note_path(out: dict[str, set[int]], path: str | None) -> str | None:
    """path, entered in out with no lines yet unless it is there or None."""
    if path is not None:
        out.setdefault(path, set())
    return path


def _hunk_lines(line: str) -> range:
    """The new tree's lines that a `@@` line's hunk adds."""
    m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
    if not m:
        return range(0)
    start = int(m.group(1))
    return range(start, start + (1 if m.group(2) is None else int(m.group(2))))


def _header_path(out: dict[str, set[int]], path: str | None, line: str) -> str | None:
    """The file the diff is about after a header line outside a hunk, kept in out as changed_lines says."""
    if line.startswith("deleted file mode") and path is not None:
        out.pop(path, None)
        return None
    if line.startswith("rename to "):
        return _note_path(out, _unquote(line[len("rename to "):]))
    if line.startswith("+++ "):
        # git ends the name with a tab when it holds a space.
        name = _unquote(line[len("+++ "):].removesuffix("\t"))
        return _note_path(out, None if name == "/dev/null" else name.removeprefix("b/"))
    return path


def _diff_git_path(names: str) -> str | None:
    """The path of `a/<path> b/<path>`; None when the two differ, as for a rename, which later lines name."""
    if m := re.fullmatch(r'("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")', names):
        a, b = _unquote(m.group(1)), _unquote(m.group(2))
    else:
        half = (len(names) - 1) // 2
        a, b = names[:half], names[half + 1:]
    if a.startswith("a/") and b.startswith("b/") and a[2:] == b[2:]:
        return b[2:]
    return None


def _unquote(name: str) -> str:
    """A path as git quotes it when it holds a quote, a backslash or a control character."""
    if len(name) < 2 or not (name.startswith('"') and name.endswith('"')):
        return name
    escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", '"': '"', "\\": "\\"}
    return re.sub(r'\\([0-7]{3}|.)',
                  lambda m: chr(int(m.group(1), 8)) if len(m.group(1)) == 3 else escapes.get(m.group(1), m.group(1)),
                  name[1:-1])


def quality_report(gate: str, intro: str, changed: dict[str, set[int]], *, conditions=(), issues=(),
                   tests: list[tuple[str, str]] | None = None, coverage: dict[str, str] | None = None,
                   console: str = "", edited=()) -> str:
    """The quality file: the gate on its first line, then what the Builder has to answer.

    Only the issues on lines the change added, and the lineless ones on files it touched, are
    numbered, at most QUALITY_ISSUE_LIMIT of them; the rest are counted.
    """
    out = [f"GATE: {gate}", "", intro]
    if conditions:
        out += _conditions_section(conditions)
    if gate != GATE_BUILD_FAILED:
        out += _issues_section(issues, changed)
    out += _tests_section(tests)
    if coverage is not None:
        out += _coverage_section(coverage)
    if console:
        out += ["", f"## The last {CONSOLE_TAIL_LINES} lines of the build's console", "", "````", console, "````"]
    if edited:
        out += _config_section(edited)
    return "\n".join(out) + "\n"


# The sections of quality_report after its first, each as lines that start with a blank one.

def _conditions_section(conditions) -> list[str]:
    out = ["", "## Failed conditions", ""]
    for c in conditions:
        wants = "at least" if c.get("comparator") == "LT" else "at most"
        out.append(f"- `{c.get('metricKey')}` is {c.get('actualValue')}; the gate wants {wants} "
                   f"{c.get('errorThreshold')}.")
    return out


def _issues_section(issues, changed: dict[str, set[int]]) -> list[str]:
    on_change = sorted((i for i in issues if i["path"] in changed
                        and (i["line"] is None or i["line"] in changed[i["path"]])),
                       key=lambda i: (i["path"], i["line"] or 0))
    out = ["", "## Issues on changed lines", ""]
    for k, i in enumerate(on_change[:QUALITY_ISSUE_LIMIT], 1):
        where = f"{i['path']}:{i['line']}" if i["line"] is not None else f"{i['path']} (the whole file)"
        out.append(f"{k}. `{where}` {i['severity']} {i['rule']}: {i['message']}")
    if not on_change:
        out.append("None.")
    if (more := len(on_change) - QUALITY_ISSUE_LIMIT) > 0:
        out += ["", f"{more} more issues on changed lines are not listed; fix the ones above first."]
    if others := len(issues) - len(on_change):
        out += ["", f"{others} other open issues in the project are not on lines this change added, "
                    "so they are not listed."]
    return out


def _tests_section(tests: list[tuple[str, str]] | None) -> list[str]:
    out = ["", "## Failing tests", ""]
    if tests is None:
        return out + ["Jenkins has no test report for this build."]
    out += [f"- `{name}`: {detail.strip().splitlines()[0] if detail.strip() else 'failed'}"
            for name, detail in tests[:QUALITY_ISSUE_LIMIT]] or ["None."]
    if len(tests) > QUALITY_ISSUE_LIMIT:
        out.append(f"- and {len(tests) - QUALITY_ISSUE_LIMIT} more.")
    return out


def _coverage_section(coverage: dict[str, str]) -> list[str]:
    to_cover = coverage.get("new_lines_to_cover")
    if to_cover in (None, "0"):
        return ["", "## Coverage on new code", "", "No new lines to cover."]
    return ["", "## Coverage on new code", "",
            f"{coverage.get('new_coverage', '?')}% of {to_cover} new lines to cover; "
            f"{coverage.get('new_uncovered_lines', '?')} are not covered."]


def _config_section(edited) -> list[str]:
    out = ["", "## Build configuration", ""]
    if "Jenkinsfile" in edited:
        out.append("- The change edits `Jenkinsfile`. The quality job reads it from `main`, "
                   "so this analysis ran without the edit.")
    if "sonar-project.properties" in edited:
        out.append("- The change edits `sonar-project.properties`. The scanner read the edited file "
                   "from the snapshot.")
    return out


def edited_config(diff: str, changed: dict[str, set[int]]) -> list[str]:
    """The BUILD_CONFIG_FILES the diff changes, deletes or renames away."""
    return [f for f in BUILD_CONFIG_FILES
            if f in changed or re.search(rf"^(--- a/|rename from ){re.escape(f)}\t?$", diff, re.M)]


def spec_title(spec: str) -> str | None:
    """The `# ` heading on the spec's first non-blank line."""
    for line in spec.splitlines():
        if line.strip():
            m = re.match(r"#\s+(\S.*)", line.strip())
            return m.group(1).strip() if m else None
    return None


def branch_name(title: str, run_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())[:40].strip("-")
    key = run_id.rsplit("-", 1)[-1]
    return f"{BRANCH_PREFIX}{slug}-{key}" if slug else f"{BRANCH_PREFIX}{key}"


@dataclass(frozen=True)
class SpecFile:
    """A spec given with run --spec, read on the orchestrator's machine: it becomes the contract step's file."""
    path: str  # as given, for the log
    text: str

    @property
    def task(self) -> str:
        """The task of a run given none: the spec's title, or else the file's name without its extension."""
        return spec_title(self.text) or os.path.splitext(os.path.basename(self.path))[0]


def read_spec_file(path: str) -> SpecFile:
    try:
        # newline="" keeps the file's line endings, so the run gets it unchanged.
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError as e:
        raise OrchestratorError(f"cannot read spec file {path}: {e.strerror}") from e
    except UnicodeDecodeError as e:
        raise OrchestratorError(f"spec file {path} is not UTF-8 text: {e}") from e
    if not text.strip():
        raise OrchestratorError(f"spec file {path} is empty")
    return SpecFile(path, text)


def _details(summary: str, text: str, open_: bool = False) -> str:
    if len(text) > PR_SECTION_LIMIT:
        text = text[:PR_SECTION_LIMIT] + "\n\n*(truncated; the full file is in the run directory)*"
    return f"<details{' open' if open_ else ''}>\n<summary>{summary}</summary>\n\n{text.strip()}\n\n</details>"


def verdict_line(state: "RunState") -> str:
    """How the run ended, for its pull request, and why it is a draft if it is one for that."""
    if state.verdict == FINISHED:
        return f"**{FINISHED}**: its workflow has no review step, so no agent reviewed this change."
    if state.verdict == QUALITY_GATE_FAILED:
        return (f"**{QUALITY_GATE_FAILED}**: its workflow has no review step, and the SonarQube quality gate still "
                f"did not pass after the last quality round, so this is a draft.")
    line = f"Reviewer verdict after {state.round} round{'s' if state.round != 1 else ''}: **{state.verdict}**."
    if state.verdict != APPROVE:
        line += "\n\nThe Reviewer still requested changes after the last round, so this is a draft."
    return line


def commit_note(state: "RunState") -> str:
    """How the run ended, for its commit message."""
    if state.verdict in (FINISHED, QUALITY_GATE_FAILED):
        return f"Orchestrator run {state.run_id}: {state.verdict}, unreviewed."
    return f"Orchestrator run {state.run_id}: {state.verdict} after {state.round} review round(s)."


def pr_body(state: "RunState", spec: str, report: str, review: str, quality: str | None = None) -> str:
    approved = state.verdict in SUCCEEDED
    head = f"Opened by ai-agents-orchestrator run `{state.run_id}`. {verdict_line(state)}"
    warning = []
    if state.conflicts:
        files = "\n".join(f"> - `{f}`" for f in state.conflicts)
        warning = [f"> [!WARNING]\n> This branch conflicts with `{state.base_branch}`, so this is a draft. "
                   f"Merge `{state.base_branch}` into it and resolve the conflicts in:\n{files}"]
    return "\n\n".join([
        *warning,
        head,
        _details("Spec", spec, open_=True),
        _details(f"Builder report (round {state.round})", report),
        *([_details(f"Quality gate (round {state.round})", quality)] if quality else []),
        *([] if state.verdict in (FINISHED, QUALITY_GATE_FAILED) else
          [_details(f"Review (round {state.round})", review, open_=not approved)]),
    ]) + "\n"


def log(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, under another user
    return True


class RunTakenOver(OrchestratorError):
    """Another orchestrator process owns the run now, so this one stops without saving."""


# How a role's agent came to be ready for a turn, which decides what it is prompted with.
NEW = "new"              # the role's first agent in this run
ALIVE = "alive"          # still running from before
RESUMED = "resumed"      # had exited; relaunched into its saved agent session
RESTARTED = "restarted"  # had exited; relaunched in a fresh session that has lost its earlier turns


@dataclass(frozen=True)
class Clocks:
    """The time a Workflow keeps: passed in, so the tests can run one on a fake that sleeps instantly."""
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic  # for deadlines and stalls
    wall: Callable[[], float] = time.time  # for timestamps in state.json


class Workflow:
    """Drives one run through the steps of its workflow in a herdr workspace.

    A role's turn ends when it writes its handoff file, not when herdr reports it
    settled: an agent can end a turn while a background task it started is still
    running and resume when the task completes, as Claude Code does, so idle or done
    can come mid-work.

    The same code starts a new run and resumes an interrupted one. A new run is a
    resume from the first step with no workspace; every step first looks for what an
    earlier orchestrator, or a role working while none was watching, already did.

    With pull_request, the editing steps work on a new branch, and the finished change
    is committed, pushed and opened as a pull request against the branch the run
    started on, which is checked out again afterwards. The workspace is then
    closed. A run that fails keeps its workspace and branch, to see what happened.

    With a worktree, the agents and every git step of the run work there instead
    of in the project's checkout (work_dir), which the run then never touches; the
    worktree is removed once the pull request is open, and kept otherwise.

    With ci, each turn of the quality-gated step is followed by a quality round (phase
    quality) that analyses a snapshot of the change; a gate that does not pass goes back
    to that step, up to max_quality_rounds analyses per round, before the next step.
    """

    # Whether a process on this host exists, for judging the other runs in the checkout. A class attribute,
    # not a parameter, which __init__ has enough of; a test sets its own on the instance.
    pid_alive = staticmethod(pid_alive)

    def __init__(self, herdr: Herdr, host: Host, state: RunState, *,
                 notify, max_rounds: int = DEFAULT_MAX_ROUNDS,
                 turn_timeout: int = DEFAULT_TURN_TIMEOUT,
                 agents: AgentSettings | None = None,
                 pull_request: bool = True,
                 ci: CI | None = None,
                 max_quality_rounds: int = DEFAULT_MAX_QUALITY_ROUNDS,
                 pipeline: Pipeline | None = None,
                 clocks: Clocks | None = None,
                 spec: SpecFile | None = None):
        """pipeline is the run's workflow; without it, the one the state names.

        spec, from run --spec, is written as the contract step's file before the run starts, for a state
        that starts at the step after it (seeded_start).
        """
        self.herdr = herdr
        self.host = host
        self.state = state
        self.notify = notify
        self.ci = ci
        self.spec = spec
        self.pipeline = pipeline = pipeline or run_pipeline(state.workflow, state.workflow_definition)
        if ci and pipeline.gated is None:
            raise OrchestratorError(f"workflow {pipeline.name} has no quality-gated step, "
                                    f"so the quality gate does not apply")
        # Publishing commits on the run's branch, which only the first editing step creates.
        if pull_request and pipeline.first_edit is None:
            raise OrchestratorError(f"workflow {pipeline.name} has no editing step, "
                                    f"so it cannot end in a pull request; pass --no-pr")
        state.workflow = pipeline.name
        state.workflow_definition = None if WORKFLOWS.get(pipeline.name) is pipeline else workflow_definition(pipeline)
        # Saved with the run, so a resume starts from them.
        state.max_rounds = max_rounds
        state.turn_timeout = turn_timeout
        agents = agents or AgentSettings()
        state.permission_mode = agents.permission_mode
        state.models = dict(agents.models)
        state.agent_kinds = {role: agents.kinds.get(role, DEFAULT_AGENT) for role in pipeline.roles}
        state.pull_request = pull_request
        state.quality_job = ci.job if ci else None
        state.max_quality_rounds = max_quality_rounds
        clocks = clocks or Clocks()
        self.sleep = clocks.sleep
        self.clock = clocks.monotonic
        self.wallclock = clocks.wall
        self.me = {"host": socket.gethostname(), "pid": os.getpid(), "started_at": self._timestamp()}
        self._last_write = 0.0
        # The roles whose harness has no equivalent of the permission mode and that the log has said so for.
        self._told_dropped = set()

    def run(self) -> str:
        """Run every phase not yet done and return the final verdict; for a workflow without one, FINISHED,
        or QUALITY_GATE_FAILED when the gate still failed."""
        s = self.state
        if s.phase == DONE:
            return s.verdict
        self._check_phase()
        try:
            self._claim()
            self._check_start()
            # Publishing needs no agent, so a resume there opens no workspace only to close it.
            if s.phase != PUBLISH:
                self._prepare()
            self._walk()
            if self.pipeline.verdict_step is None:
                s.verdict = FINISHED if self._gate_passed(s.round) else QUALITY_GATE_FAILED
            if s.pull_request:
                self._publish()
            self._clean_up_quality()
        except RunTakenOver:
            raise
        except OrchestratorError as e:
            self._release(str(e))
            raise
        except KeyboardInterrupt:
            self._release("interrupted")
            raise

        s.phase = DONE
        s.owner = None
        self._save()
        if s.worktree and not s.pull_request:
            log(f"the change is in worktree {s.worktree}")
        self.notify(f"Run finished: {s.verdict}", s.pr_url or s.task)
        if s.pull_request:
            self._close_workspace()
        return s.verdict

    def _check_phase(self) -> None:
        s, p = self.state, self.pipeline
        phases = {st.id for st in p.steps} | {PUBLISH}
        if p.gated:
            phases.add(QUALITY)
        if s.phase not in phases:
            raise OrchestratorError(f"run {s.run_id} is in phase {s.phase}, "
                                    f"which workflow {p.name} has no step for")

    def _check_start(self) -> None:
        """Fail before the next turn if the run could not finish."""
        s = self.state
        self._check_alone()
        # Round 0: the run has not reached its first editing step, so it has no base or branch yet.
        if s.round == 0:
            if s.worktree:
                self._open_worktree()
            if self.ci:
                self._check_gate()
            if s.pull_request and not s.worktree:
                self._check_repo()
        else:
            if s.worktree:
                self._check_worktree()
            self._check_branch()
            if self.ci and s.phase in (self.pipeline.gated.id, QUALITY):
                # Before a turn of the gated step that may take half an hour, not after it.
                self.ci.check_credentials()

    def _check_alone(self) -> None:
        """Fail if another run in this checkout is live: the two would share one working tree.

        A worktree run shares no working tree, neither with another worktree run nor with the run in the
        checkout. After _claim, so of two runs that start together each sees the other's state.json and both
        stop, rather than both going on.
        """
        s = self.state
        if s.worktree:
            return
        for age, other in self.host.run_states(s.cwd):
            if other.get("run_id") == s.run_id or other.get("worktree"):
                continue
            health = run_health(other, age, self.me["host"], self.pid_alive)
            if health and health[0] == RUNNING:
                raise OrchestratorError(f"run {other['run_id']} is running in {s.cwd} ({health[1]}); "
                                        f"one run at a time per checkout: wait for it, stop it, "
                                        f"or use another clone")

    def _claim(self) -> None:
        s = self.state
        ignore = f"{s.cwd}/.orchestrator/.gitignore"
        if self.host.read(ignore) is None:
            # Keeps run files out of `git status`, so the Reviewer sees only the Builder's changes
            # and the commit holds nothing else.
            self.host.write(ignore, "*\n")
        if self.spec:
            self._seed_spec()
        s.error = None
        s.owner = self.me
        # Not _save: the caller has already decided that any earlier owner is gone.
        self._write_state()

    def _seed_spec(self) -> None:
        """Write the spec given with run --spec as the contract step's file.

        After .gitignore, which keeps it out of the clean-tree check, and before the first state.json, which
        starts the run after the contract step: a resume of that run must find the file.
        """
        contract = self.pipeline.contract
        path = contract.path(self.state.dir, 0)
        self.host.write(path, self.spec.text)
        skipped = "interview" if contract.human_paced else "turn"
        log(f"[{contract.role}] {os.path.basename(path)} is a copy of {self.spec.path}; "
            f"skipping the {self.pipeline.roles[contract.role]}'s {skipped}")

    def _release(self, error: str) -> None:
        """Record why the run stopped and that no process drives it now, as far as saving still works."""
        self.state.error = self.ci.mask(error) if self.ci else error
        self.state.owner = None
        try:
            self._save()
        except OrchestratorError as e:
            log(f"could not record the failure in state.json: {e}")

    def _check_repo(self) -> None:
        """Fail before the interview if the change could not become a pull request."""
        s = self.state
        if self.host.git_head(s.cwd) is None:
            raise OrchestratorError(f"{s.cwd} is not a git repository with a commit; pass --no-pr")
        branch = self.host.git(s.cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch == "HEAD":
            raise OrchestratorError(f"{s.cwd} is on a detached HEAD; check out the branch the pull request targets")
        # Kept once recorded: a run resumed after it switched to its own branch still targets the first one.
        s.base_branch = s.base_branch or branch
        self._require_clean()
        # So the Spec Collector reads current code. A resume already on the run's own branch
        # leaves it to _switch_to_branch.
        if branch == s.base_branch:
            self.host.fast_forward(s.cwd, s.base_branch)

    def _open_worktree(self) -> None:
        """Before the interview, check out origin's base branch, detached, in the run's worktree.

        The base branch is the one checked out in the project. The checkout itself is left as it is, so it
        need not be clean or up to date, and the human can go on working in it.
        """
        s = self.state
        if self.host.git_head(s.cwd) is None:
            raise OrchestratorError(f"{s.cwd} is not a git repository with a commit, which --worktree needs")
        branch = self.host.git(s.cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch == "HEAD":
            raise OrchestratorError(f"{s.cwd} is on a detached HEAD; --worktree bases the run on the checked-out "
                                    f"branch, so check one out")
        if self.host.git_run(s.cwd, "remote", "get-url", REMOTE).returncode != 0:
            raise OrchestratorError(f"--worktree starts the run from {REMOTE}'s branch, and {s.cwd} has no {REMOTE}")
        # Kept once recorded, as _check_repo keeps it.
        s.base_branch = s.base_branch or branch
        # A resume before the first editing step: the run's worktree holds nothing it needs to keep.
        if self.host.is_worktree(s.cwd, s.worktree):
            log(f"run {s.run_id} resumes in worktree {s.worktree}")
            return
        self.host.add_worktree(s.cwd, s.worktree, self.host.fetch(s.cwd, s.base_branch))
        log(f"run {s.run_id} in worktree {s.worktree}, at {REMOTE}/{s.base_branch}")
        self._save()

    def _check_worktree(self) -> None:
        """Fail if the run's worktree, which holds its change, is gone; unless the pull request has the change."""
        s = self.state
        if s.pr_url or self.host.is_worktree(s.cwd, s.worktree):
            return
        raise OrchestratorError(f"the worktree of run {s.run_id}, {s.worktree}, is gone or no longer a git "
                                f"worktree, and with it the run's change; the run cannot go on")

    def _check_branch(self) -> None:
        """Fail if HEAD is not the run's branch, which holds the Builder's uncommitted change until publishing.

        Otherwise the commit lands on whatever the human checked out, and the run's branch is pushed without it.
        """
        s = self.state
        if not s.pull_request or not s.branch or s.pr_url:
            return
        current = self.host.git(s.work_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current != s.branch:
            # A detached HEAD reads as "HEAD", so it never matches.
            raise OrchestratorError(f"{s.work_dir} is on {current}, not {s.branch}, which holds this run's change; "
                                    f"check out {s.branch} and resume")

    def _check_gate(self) -> None:
        """Fail before the interview if the quality gate could not run."""
        s = self.state
        if self.host.git_head(s.cwd) is None:
            raise OrchestratorError(f"the quality gate needs a git repository, and {s.cwd} is not one with a commit")
        # _check_repo checks both for a pull-request run, and a worktree run's worktree starts out clean.
        if not s.pull_request:
            # A snapshot takes every change in the tree, so the tree must start with none.
            self._require_clean()
            if self.host.git_run(s.cwd, "remote", "get-url", REMOTE).returncode != 0:
                raise OrchestratorError(f"the quality gate pushes snapshots to {REMOTE}, and {s.cwd} has no {REMOTE}")
        self.ci.preflight()

    def _require_clean(self) -> None:
        # The commit takes every change in the working tree, so it must hold only the Builder's.
        work_dir = self.state.work_dir
        dirty = self.host.git(work_dir, "status", "--porcelain")
        if dirty.strip():
            raise OrchestratorError(
                f"{work_dir} has uncommitted changes; commit or stash them first:\n{dirty.rstrip()}")

    def _prepare(self) -> None:
        s = self.state
        if s.workspace_id:
            if self.herdr.workspace_exists(s.workspace_id):
                log(f"run {s.run_id} resumes in herdr workspace {s.workspace_id}")
                return
            # The handoff files are the whole contract between roles, so a new workspace loses nothing
            # a role needs; its panes went with the old one.
            log(f"herdr workspace {s.workspace_id} is gone; opening a new one")
            for agent in s.agents.values():
                agent["pane"] = ""
        label = f"{s.key} {s.task}"[:40]
        s.workspace_id, s.root_pane = self.herdr.create_workspace(s.work_dir, label)
        log(f"run {s.run_id} in herdr workspace {s.workspace_id}")
        self._save()

    def _walk(self) -> None:
        """Take the turn of the current step and of each one after it, looping back as the verdict says."""
        s, p = self.state, self.pipeline
        if s.phase == PUBLISH:
            return
        if s.round == 0 and (step := p.step(s.phase)).edits:
            self._goto(step, 0)  # a run whose first step edits
        while True:
            n = s.round
            if s.phase == QUALITY:
                step, finished = p.gated, self._judge_quality(n)
            else:
                step = p.step(s.phase)
                finished = self._take_turn(step, n)
            if not finished:
                continue
            if (following := p.after(step)) is None:
                return
            self._goto(following, n)

    def _take_turn(self, step: Step, n: int) -> bool:
        """The step's turn in round n; False when the run goes on elsewhere than the next step."""
        s = self.state
        out = self._step_turn(step, n)
        if step.quality_gated and self.ci:
            s.phase, s.quality_round = QUALITY, s.quality_round + 1
            self._save()
            return False
        if step.loop_to is None:
            return True
        s.verdict = parse_verdict(out)  # _turn accepts no verdict file without one
        log(f"[{step.role}] round {n}: {s.verdict}")
        if s.verdict != APPROVE and n < s.max_rounds:
            s.quality_round = 0
            self._goto(self.pipeline.step(step.loop_to), n + 1)
            return False
        return True

    def _judge_quality(self, n: int) -> bool:
        """The current quality round of round n; False when its gate sends the change back to the gated step."""
        s = self.state
        gate = self._quality(n, s.quality_round)
        # As for review rounds: a saved round beyond a lowered limit ends here too.
        if gate != GATE_OK and s.quality_round < s.max_quality_rounds:
            s.phase = self.pipeline.gated.id
            self._save()
            return False
        return True

    def _goto(self, step: Step, n: int) -> None:
        """Make step the current one, in round n, and save that; the first editing step starts round 1."""
        s = self.state
        if n == 0 and step.edits:
            if s.pull_request:
                self._switch_to_branch()
            # Only here, before the first editing step and after the base branch was fast-forwarded:
            # every later step, resumed or not, diffs against this base, even if the human commits
            # the Builder's work meanwhile.
            s.base = self.host.git_head(s.work_dir)
            n = 1
        s.phase, s.round = step.id, n
        self._save()

    def _step_turn(self, step: Step, n: int) -> str:
        s = self.state
        text, fresh = self._prompts(step, n)
        path = self._own_path(step, n)
        verdict = step.loop_to is not None
        if not step.human_paced:
            return self._turn(step.role, text, path, s.turn_timeout, fresh_text=fresh, verdict=verdict)
        label = self.pipeline.roles[step.role]

        def announce(how: str) -> None:
            name, pane = s.agents[step.role]["name"], s.agents[step.role]["pane"]
            self.herdr.focus(name)
            what = "restarted; the interview starts over" if how == RESTARTED else "is waiting for you"
            self.notify(f"{label} {what}", f"pane {pane}: {s.task}")
            log(f"[{step.role}] answer the {label} in pane {pane}")

        # Paced by the human, so the turn has no deadline, and an idle agent is normal:
        # it is waiting for the human's reply.
        return self._turn(step.role, text, path, timeout=None, fresh_text=fresh,
                          watch_stalls=False, announce=announce, verdict=verdict)

    def _own_path(self, step: Step, n: int) -> str:
        """The file the step's turn in round n writes: for the gated step, its answer to the quality round if any."""
        return step.path(self.state.dir, n, self.state.quality_round if step.quality_gated else 0)

    def _prompts(self, step: Step, n: int) -> tuple[str, str | None]:
        """The step's prompt for its turn in round n, and the one for a fresh session that has not seen the turns
        before it."""
        s = self.state
        q = s.quality_round if step.quality_gated else 0
        fill = {name: self._placeholder(name, step, n) for name in step.placeholders()}
        quality = self._quality_note(n) if step.loop_to is not None else ""
        first = step.prompt.format(**fill) + quality
        if q:
            follow = QUALITY_FIX_PROMPT.format(quality_path=s.quality_path(n, q), report_path=self._own_path(step, n))
        elif n > 1:
            follow = (step.again or step.prompt).format(**fill) + quality
        else:
            return first, None
        fresh = [first]
        if step.fresh_note:
            fresh.append(step.fresh_note.format(**fill))
        # A quality fix always asks for work that the first prompt does not.
        if q or step.fresh_repeats_again:
            fresh.append(follow)
        return follow, "\n\n".join(fresh)

    def _placeholder(self, name: str, step: Step, n: int) -> str:
        """The value of a prompt placeholder (see Step) in the step's turn in round n."""
        s = self.state
        fixed = {"task": s.task, "cwd": s.work_dir, "n": str(n), "approve": APPROVE, "changes": CHANGES_REQUESTED}
        if name in fixed:
            return fixed[name]
        if name == "change":
            return self._change_description()
        if name == "path":
            return self._own_path(step, n)
        prefix, other = next((pre, st) for x, pre, st in self.pipeline.path_placeholders() if x == name)
        if prefix == "prev_":
            return self._reports(other, n - 1)[-1]
        if prefix == "earlier_":
            reports = [r for i in range(1, n) for r in self._reports(other, i)]
            if other is step and step.quality_gated:
                reports += [step.path(s.dir, n, j) for j in range(s.quality_round)]
            return ", ".join(reports)
        return self._own_path(step, n) if other is step else other.last_path(s.dir, n, s.quality_round)

    def _reports(self, step: Step, n: int) -> list[str]:
        """The step's files of round n, oldest first; for the gated step, its answers to quality rounds too."""
        s = self.state
        reports = [step.path(s.dir, n)]
        # How many quality rounds an earlier round took is recorded only by the files it left.
        while self.ci and step.quality_gated and self.host.read(step.path(s.dir, n, len(reports))) is not None:
            reports.append(step.path(s.dir, n, len(reports)))
        return reports

    def _switch_to_branch(self) -> None:
        s = self.state
        spec = self._last_text(self.pipeline.contract, 0)
        s.branch = branch_name(spec_title(spec) or s.task, s.run_id)
        # Already there when an earlier orchestrator stopped between the switch and saving the next phase.
        current = self.host.git(s.work_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current != s.branch:
            # Checked again: the human may have touched the tree during the interview.
            self._require_clean()
            # Again, because the interview can take hours: the branch starts from origin's latest base.
            if s.worktree:
                # From origin's base itself: the worktree's detached HEAD is where it was before the interview,
                # and the project's own base branch is the human's.
                self.host.git(s.work_dir, "switch", "-c", s.branch, self.host.fetch(s.work_dir, s.base_branch))
            else:
                if current != s.base_branch:
                    raise OrchestratorError(f"{s.cwd} is on {current}, not {s.base_branch}, which the pull request "
                                            f"targets; check out {s.base_branch} and resume")
                self.host.fast_forward(s.cwd, s.base_branch)
                self.host.git(s.cwd, "switch", "-c", s.branch)
        log(f"building on branch {s.branch}")

    def _publish(self) -> None:
        """Commit the change, merge the latest base into it, push its branch and open a pull request for it.

        Each step first checks whether an earlier orchestrator already took it, so a resume
        in this phase finishes the job instead of committing, merging or opening a second pull request.
        """
        s, p = self.state, self.pipeline
        s.phase = PUBLISH
        self._save()
        if not s.pr_url:
            wd = s.work_dir
            # Again: the human may have switched branches during a turn.
            self._check_branch()
            # An earlier orchestrator stopped mid-merge: committing now would commit the conflict markers.
            if self.host.abort_merge(wd):
                log(f"aborted the merge an earlier orchestrator left in {wd}")
            spec = self._last_text(p.contract, 0)
            title = (spec_title(spec) or s.task.strip().split("\n", 1)[0] or s.run_id)[:72]
            if self.host.git(wd, "status", "--porcelain").strip():
                self.host.git(wd, "add", "--all")
                self.host.git(wd, "commit", "--quiet", "-m", title, "-m", commit_note(s))
            elif self.host.git_head(wd) == s.base:
                raise OrchestratorError(f"the Builder changed no files; there is nothing to commit on {s.branch}")
            # The base moved on while the run built and reviewed. A conflict is left to the human:
            # the branch is pushed without the merge and the pull request says what conflicts.
            s.conflicts = self.host.merge_upstream(wd, s.base_branch)
            self._save()
            if s.conflicts:
                log(f"{s.branch} conflicts with {REMOTE}/{s.base_branch} in {', '.join(s.conflicts)}; "
                    f"the pull request will be a draft")
            self.host.git(wd, "push", "--quiet", "--set-upstream", REMOTE, s.branch, timeout=NETWORK_TIMEOUT)
            quality = self.host.read(s.quality_path(s.round, s.quality_round)) if s.quality_round else None
            report = self._last_text(p.last_edit, s.round)
            verdict = self._last_text(p.verdict_step, s.round)
            body = pr_body(s, spec, report, verdict, quality)
            s.pr_url = self.host.create_pr(wd, s.base_branch, s.branch, title, body,
                                           draft=s.verdict not in SUCCEEDED or bool(s.conflicts))
            self._save()
            log(f"opened {s.pr_url}")
            if s.conflicts:
                self.notify(f"Pull request conflicts with {s.base_branch}", s.pr_url)
        self._leave_work_dir()

    def _leave_work_dir(self) -> None:
        """Once the pull request has the change: check the base branch out again, or remove the run's worktree.

        The base branch leaves the checkout where the human had it, so the next run branches from there too.
        The worktree goes without --force, which git refuses for one with untracked or modified files. _publish
        has committed them all, so a refusal means someone changed the worktree since; that, like any failure in
        removing it, is only logged. The run's branch stays either way.
        """
        s = self.state
        if not s.worktree:
            self.host.git(s.cwd, "switch", "--quiet", s.base_branch)
            return
        try:
            if self.host.is_worktree(s.cwd, s.worktree):
                self.host.remove_worktree(s.cwd, s.worktree)
                log(f"removed worktree {s.worktree}")
        except OrchestratorError as e:
            log(f"could not remove worktree {s.worktree}: {e}")

    def _last_text(self, step: Step | None, n: int) -> str:
        """The step's last file of round n, or "" for no step or no file."""
        if step is None:
            return ""
        return self.host.read(step.last_path(self.state.dir, n, self.state.quality_round)) or ""

    def _close_workspace(self) -> None:
        # The pull request carries the change now; a failure here leaves only clutter behind.
        try:
            self.herdr.close_workspace(self.state.workspace_id)
        except OrchestratorError as e:
            log(f"could not close herdr workspace {self.state.workspace_id}: {e}")

    def _quality_note(self, n: int) -> str:
        """What the verdict step hears of round n's last quality round: passed, or the findings left; "" without one."""
        s = self.state
        if not self.ci or not s.quality_round:
            return ""
        note = QUALITY_PASSED_NOTE if self._gate_passed(n) else QUALITY_UNRESOLVED_NOTE
        return "\n\n" + note.format(quality_path=s.quality_path(n, s.quality_round))

    def _gate_passed(self, n: int) -> bool:
        """Whether round n's last quality round passed the gate; True for a run without one."""
        s = self.state
        if not self.ci or not s.quality_round:
            return True
        text = self.host.read(s.quality_path(n, s.quality_round))
        return text is not None and parse_gate(text) == GATE_OK

    # -- The quality gate --------------------------------------------------

    def _quality(self, n: int, q: int) -> str:
        """Quality round q of round n: analyse the change, write quality-<n>-<q>.md, and return its gate.

        Each step first looks for what an earlier orchestrator already did, furthest first: the
        quality file; then the project and the base's analysis; then, in _analyse, the chain in ci.
        """
        s = self.state
        path = s.quality_path(n, q)
        ref = f"{CI_REF_PREFIX}{s.key}-{n}-q{q}"
        if (text := self.host.read(path)) is not None:
            log(f"[quality] {os.path.basename(path)} is already written")
        else:
            # Per round, and afresh on a resume.
            deadline = self.clock() + QUALITY_TIMEOUT
            self._quality_project(deadline)
            self._quality_baseline(deadline)
            message = f"Orchestrator run {s.run_id}: round {n}, quality round {q}"
            build, result, analysis = self._analyse(
                ref, "change", deadline, lambda: self.host.snapshot(s.work_dir, s.base, message))
            text = self.ci.mask(self._quality_report(n, q, build, result, analysis, deadline))
            self.host.write(path, text)
        gate = parse_gate(text)
        if gate is None:
            self._reject(path, "does not start with a GATE line")
        log(f"[quality] round {n}, quality round {q}: {gate}")
        self._finish_analysis(ref)
        return gate

    def _quality_project(self, deadline: float) -> None:
        s = self.state
        if s.quality_project:
            return
        key = f"{SONAR_PROJECT}-{s.key}"
        self._once(f"creating SonarQube project {key}", deadline, lambda: self.ci.create_project(key, SONAR_PROJECT))
        s.quality_project = key
        self._save()

    def _quality_baseline(self, deadline: float) -> None:
        """Analyse the base once per run, as version base, so new code is only what the change adds."""
        s = self.state
        if s.quality_baseline:
            return
        ref = f"{CI_REF_PREFIX}{s.key}-base"
        build, result, analysis = self._analyse(ref, "base", deadline, lambda: s.base)
        if analysis is None:
            self._cut_back(build)
            raise OrchestratorError(
                f"Jenkins build #{build} of the base commit {s.base} ended {result} before the SonarQube analysis, "
                f"so there is no baseline to judge the change against; see the build, then resume")
        s.quality_baseline = analysis
        self._finish_analysis(ref)

    def _analyse(self, ref: str, version: str, deadline: float, commit) -> tuple[int, str | None, str | None]:
        """Have Jenkins analyse a commit into the run's project; returns (build, its result, analysis id).

        commit() makes the commit, which is pushed to the branch ref. The analysis id is None when
        the build ended before the analysis; the result is None when a resume found the analysis
        already under way. Each link of the chain is saved as soon as it is known, so a resume
        picks it up from the furthest one.
        """
        s = self.state
        fresh = s.ci.get("ref") != ref
        if fresh:
            if s.ci.get("ref"):
                self._delete_ci_ref(s.ci["ref"])
            sha = commit()
            self.host.push_ref(s.work_dir, sha, ref)
            s.ci = {"ref": ref, "sha": sha}
            self._save()
        result = None
        if "ce_task" not in s.ci:
            if "build" not in s.ci:
                self._start_build(ref, version, deadline, adopt=not fresh)
            number = s.ci["build"]
            result = self._poll(f"waiting for Jenkins build #{number} of {ref}", deadline,
                                lambda: self.ci.build_result(number))
            task = self._once(f"reading report-task.txt of Jenkins build #{number}", deadline,
                              lambda: self.ci.report_task(number))
            if task is None:
                if result == "ABORTED":
                    self._cut_back(number)
                    raise OrchestratorError(
                        f"Jenkins build #{number} of {ref} was aborted before the SonarQube analysis, so nothing "
                        f"judged the change; resume to build it again")
                return number, result, None
            s.ci["ce_task"] = task
            self._save()
        task, number = s.ci["ce_task"], s.ci["build"]
        analysis = self._poll(f"waiting for SonarQube task {task} of Jenkins build #{number}", deadline,
                              lambda: self._analysis_id(task))
        return number, result, analysis

    def _start_build(self, ref: str, version: str, deadline: float, adopt: bool) -> None:
        """Record in ci the build of ref: one triggered now, or with adopt one an earlier orchestrator triggered."""
        s = self.state
        if "queue_url" not in s.ci:
            # The ref names one round of one run, so a build of it is this round's, triggered by an
            # orchestrator that stopped before it recorded the queue item.
            found = self._once(f"looking for a Jenkins build of {ref}", deadline,
                               lambda: self.ci.find_build(ref, s.ci.get("rejected", ()))) if adopt else None
            if found is not None:
                log(f"[quality] adopting Jenkins build #{found} of {ref}")
                s.ci["build"] = found
                self._save()
                return
            s.ci["queue_url"] = self._once(f"triggering Jenkins job {self.ci.job} for {ref}", deadline,
                                           lambda: self.ci.trigger(ref, s.quality_project, version))
            self._save()
        s.ci["build"] = self._poll(f"waiting for Jenkins to start the build of {ref}", deadline,
                                   lambda: self._queued_build(ref))
        self._save()

    def _queued_build(self, ref: str) -> int | None:
        s = self.state
        queue_url = s.ci["queue_url"]
        try:
            item = self.ci.queue_item(queue_url)
        except CIError as e:
            if e.status != 404:
                raise
            # Jenkins forgets a queue item some minutes after its build started.
            found = self.ci.find_build(ref, s.ci.get("rejected", ()))
            if found is None:
                self._cut_back()
                raise OrchestratorError(f"Jenkins forgot queue item {queue_url}, and none of the job's last 20 "
                                        f"builds is of {ref}; resume to trigger a new one") from e
            return found
        if item.get("cancelled"):
            self._cut_back()
            raise OrchestratorError(f"the build of {ref} was cancelled in Jenkins's queue, so nothing judged the "
                                    f"change; resume to trigger a new one")
        return (item.get("executable") or {}).get("number")

    def _analysis_id(self, task: str) -> str | None:
        """The analysis the SonarQube task made, None while it runs."""
        t = self.ci.ce_task(task)
        status = t.get("status")
        if status == "SUCCESS":
            if not t.get("analysisId"):
                raise OrchestratorError(f"SonarQube task {task} succeeded without an analysis")
            return t["analysisId"]
        if status in ("FAILED", "CANCELED"):
            number = self.state.ci.get("build")
            self._cut_back(number)
            raise OrchestratorError(self.ci.mask(
                f"SonarQube task {task} of Jenkins build #{number} ended {status}: {t.get('errorMessage') or ''}; "
                f"resume to build it again"))
        return None

    def _cut_back(self, rejected: int | None = None) -> None:
        """Keep only the pushed commit in ci, so a resume builds it anew, never adopting the rejected build."""
        ci = self.state.ci
        skip = [*ci.get("rejected", []), *([rejected] if rejected is not None else [])]
        self.state.ci = {"ref": ci["ref"], "sha": ci["sha"], **({"rejected": skip} if skip else {})}

    def _quality_report(self, n: int, q: int, build: int, result: str | None, analysis: str | None,
                        deadline: float) -> str:
        s = self.state
        sha = s.ci["sha"]
        diff = self.host.change_diff(s.work_dir, s.base, sha)
        changed = changed_lines(diff)
        edited = edited_config(diff, changed)
        where = f"round {n}, quality round {q} of {s.max_quality_rounds}"
        tests_step = f"reading the test report of Jenkins build #{build}"
        if analysis is None:
            tests = self._once(tests_step, deadline, lambda: self.ci.failed_tests(build))
            console = "" if tests else self._once(f"reading the console of Jenkins build #{build}", deadline,
                                                  lambda: self.ci.console_tail(build))
            intro = (f"Jenkins build #{build} of snapshot `{sha}` ({where}) ended {result} before the SonarQube "
                     f"analysis, so nothing judged the change. Make it build first.")
            return quality_report(GATE_BUILD_FAILED, intro, changed, tests=tests, console=console, edited=edited)
        status = self._once(f"reading the quality gate of analysis {analysis}", deadline,
                            lambda: self.ci.gate_status(analysis))
        issues = self._once(f"reading the issues of {s.quality_project}", deadline,
                            lambda: self.ci.issues(s.quality_project))
        coverage = self._once(f"reading the coverage of {s.quality_project}", deadline,
                              lambda: self.ci.coverage(s.quality_project))
        tests = self._once(tests_step, deadline, lambda: self.ci.failed_tests(build))
        sonar = status.get("status")
        intro = (f"SonarQube analysed snapshot `{sha}` ({where}) in Jenkins build #{build}, "
                 f"against the base `{s.base}`: quality gate {sonar}.")
        failed = [c for c in status.get("conditions") or [] if c.get("status") == "ERROR"]
        return quality_report(GATE_OK if sonar == "OK" else GATE_ERROR, intro, changed, conditions=failed,
                              issues=issues, tests=tests, coverage=coverage, edited=edited)

    def _poll(self, step: str, deadline: float, poll):
        """Call poll() every CI_POLL_SECONDS until it returns something other than None, and return that.

        A failure that may pass, such as Jenkins being unreachable, is retried until the round's
        deadline; any other fails the run at once. Either way ci keeps what is known so far.
        """
        failing = None
        while True:
            out, failing = self._poll_once(step, poll, failing)
            if out is not None:
                return out
            if self.clock() >= deadline:
                why = f"; last error: {failing}" if failing else ""
                raise OrchestratorError(f"quality gate: {step} did not finish within {QUALITY_TIMEOUT}s of the "
                                        f"round's start{why}; resume to go on")
            self._heartbeat()
            self.sleep(CI_POLL_SECONDS)

    @staticmethod
    def _poll_once(step: str, poll, failing: CIError | None) -> tuple:
        """poll()'s result, None when it failed in a way that may pass, and the failure that stands after it."""
        try:
            out = poll()
        except CIError as e:
            if not e.transient:
                raise OrchestratorError(f"quality gate: {step}: {e}") from e
            if failing is None:
                log(f"[quality] {step}: {e}; retrying")
            return None, e
        if failing is not None:
            log(f"[quality] {step}: answered again")
        return out, None

    def _once(self, step: str, deadline: float, call):
        """call()'s result, retried as _poll retries; None is a result here too."""
        return self._poll(step, deadline, lambda: (call(),))[0]

    def _finish_analysis(self, ref: str) -> None:
        s = self.state
        if s.ci.get("ref") == ref:
            self._delete_ci_ref(ref)
            s.ci = {}
        self._save()

    def _delete_ci_ref(self, ref: str) -> None:
        # Jenkins has checked the ref out by now; a leftover is only clutter, which `done` tries again.
        try:
            self.host.delete_remote_branch(self.state.cwd, ref)
        except OrchestratorError as e:
            log(f"could not delete {ref} on {REMOTE}: {e}")

    def _clean_up_quality(self) -> None:
        """At the end of a run, delete its refs on origin and its SonarQube project; a failure is only logged."""
        s = self.state
        if not self.ci:
            return
        try:
            refs = self.host.remote_branches(s.cwd, f"{CI_REF_PREFIX}{s.key}-")
        except OrchestratorError as e:
            log(f"could not list the {CI_REF_PREFIX} branches on {REMOTE}: {e}")
            refs = []
        for ref in refs:
            self._delete_ci_ref(ref)
        s.ci = {}
        if s.quality_project:
            try:
                self.ci.delete_project(s.quality_project)
            except OrchestratorError as e:
                log(f"could not delete SonarQube project {s.quality_project}: {e}")
            else:
                s.quality_project = None

    def _turn(self, role: str, text: str, path: str, timeout: int | None, *,
              fresh_text: str | None = None, watch_stalls: bool = True, announce=None, verdict: bool = False) -> str:
        """Return the handoff file that ends the role's turn, prompting the role only if it still owes it.

        fresh_text replaces text for an agent in a new session, which has not seen the role's
        earlier turns. announce(how) runs once the agent is ready, before any prompt. A verdict
        file without a VERDICT first line is asked for once more (_retry) before it ends the run.
        """
        s = self.state
        file = os.path.basename(path)
        if s.retrying == file:
            text = fresh_text = self._retry_prompt(path)
        while True:
            out = self._written(role, text, path, timeout, fresh_text, watch_stalls, announce)
            if not verdict:
                if not out.strip():
                    self._reject(path, f"was written empty by the {self.pipeline.roles[role]}")
                return out
            if parse_verdict(out):
                if s.retrying == file:
                    s.retrying = None
                return out
            self._retry(role, path, out)
            text = fresh_text = self._retry_prompt(path)

    def _written(self, role: str, text: str, path: str, timeout: int | None, fresh_text: str | None,
                 watch_stalls: bool, announce) -> str:
        """The handoff file as the role wrote it, before _turn judges it."""
        s = self.state
        file = os.path.basename(path)
        # First, because the role may have written it while no orchestrator was watching.
        if (out := self.host.read(path)) is not None:
            log(f"[{role}] {file} is already written")
            return out

        how = self._agent(role)
        agent = s.agents[role]
        if announce:
            announce(how)
        log(f"[{role}] working in pane {agent['pane']}")
        if (message := self._prompt_for(how, file, text, fresh_text, path)) is not None:
            self.herdr.prompt(agent["name"], message)
            # Recorded only once the prompt is delivered. Dying in between costs one duplicate
            # prompt, which the role answers by writing the same file again.
            s.prompted = file
            self._save()
        out = self._await_handoff(role, path, timeout, watch_stalls)
        log(f"[{role}] wrote {file}")
        return out

    def _retry(self, role: str, path: str, out: str) -> None:
        """Move a malformed verdict file aside so its role writes it again; end the run if its retry is used."""
        s = self.state
        file = os.path.basename(path)
        why = f"was written empty by the {self.pipeline.roles[role]}" if not out.strip() \
            else "does not start with a VERDICT line"
        if file in s.retried:
            self._reject(path, f"{why}, and its one retry was already used")
        rejected = self._rejected_path(path)
        # Saved around the rename, so dying at any point leaves the malformed file with its retry unused, no file
        # and no prompt counted as delivered, or the retry under way; never a used retry the role was not asked for.
        s.prompted = None
        self._save()
        self.host.rename(path, rejected)
        s.retried.append(file)
        s.retrying = file
        self._save()
        log(f"[{role}] {file} {why}; moved it to {os.path.basename(rejected)}, prompting once more")

    @staticmethod
    def _rejected_path(path: str) -> str:
        stem, ext = os.path.splitext(path)
        return f"{stem}.rejected{ext}"

    def _retry_prompt(self, path: str) -> str:
        return RETRY_VERDICT_PROMPT.format(path=path, rejected_path=self._rejected_path(path),
                                           approve=APPROVE, changes=CHANGES_REQUESTED)

    def _prompt_for(self, how: str, file: str, text: str, fresh_text: str | None, path: str) -> str | None:
        """What to prompt an agent that is ready as how says with; None when it only has to be waited for."""
        if how in (NEW, RESTARTED):
            return fresh_text or text
        if self.state.prompted != file:
            return text
        if how == RESUMED:
            return CONTINUE_PROMPT.format(path=path)
        return None  # alive and already prompted for this file

    def _await_handoff(self, role: str, path: str, timeout: int | None, watch_stalls: bool) -> str:
        """Poll until the role writes path, telling the human when it is blocked or stalled."""
        label, agent, file = self.pipeline.roles[role], self.state.agents[role], os.path.basename(path)
        deadline = None if timeout is None else self.clock() + timeout
        quiet_since = None
        told = None  # what the human was last told about this turn
        while (out := self.host.read(path)) is None:
            record = self.herdr.agent(agent["name"])
            if record is None:
                raise OrchestratorError(f"the {label} exited without writing {path}")
            self._note_session(role, record)
            status = record["agent_status"]
            now = self.clock()
            if deadline is not None and now > deadline:
                raise OrchestratorError(
                    f"the {label} did not write {path} within {timeout}s; see pane {agent['pane']}")
            if status not in ("idle", "done"):
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            stalled = watch_stalls and quiet_since is not None and now - quiet_since >= STALL_SECONDS
            told = self._tell_human(role, status, stalled, told, file)
            self._heartbeat()
            self.sleep(POLL_SECONDS)
        return out

    def _tell_human(self, role: str, status: str, stalled: bool, told: str | None, file: str) -> str | None:
        """Notify the human of a blocked or stalled agent once; returns what they have been told now."""
        if status == "blocked" and told != "blocked":
            self._ask_human(role, "needs your answer")
            return "blocked"
        if stalled and told != "stalled":
            self._ask_human(role, f"is idle without writing {file}")
            return "stalled"
        return None if status == "working" else told

    def _reject(self, path: str, why: str) -> None:
        # Resume does not judge a role's output; the human fixes the file or deletes it,
        # and a deleted file is asked for again, so the prompt no longer counts as delivered,
        # and neither does a retry: the deleted file is asked for with the step's own prompt.
        self.state.prompted = self.state.retrying = None
        raise OrchestratorError(f"{path} {why}; fix it, or delete it to have it written again, then resume")

    def _agent(self, role: str) -> str:
        """Make sure the role's agent is running, and say how: NEW, ALIVE, RESUMED or RESTARTED."""
        known = self.state.agents.get(role)
        if known and self.herdr.status(known["name"]) is not None:
            return ALIVE
        pane = self._pane_for(role)
        session, kind = (known or {}).get("session"), (known or {}).get("kind", DEFAULT_AGENT)
        if session and kind != self.state.agent_kinds[role]:
            log(f"[{role}] session {session} is {kind}'s, and the role runs in {self.state.agent_kinds[role]} now; "
                f"starting a fresh one")
        elif session:
            # Keeps the conversation: for the Spec Collector that is the interview itself.
            try:
                if self._start(role, pane, session):
                    return RESUMED
            except HerdrError as e:
                log(f"[{role}] could not resume session {session}: {e}")
            log(f"[{role}] session {session} is gone; starting a fresh one")
        self._start(role, pane)
        return RESTARTED if known else NEW

    def _pane_for(self, role: str) -> str:
        """The pane for the role's next agent: its old one if that survives, else a new split."""
        s = self.state
        roles = self._pane_order(role)
        i = roles.index(role)
        old = (s.agents.get(role) or {}).get("pane") or (s.root_pane if i == 0 else "")
        if old and self.herdr.pane_exists(old):
            return old
        # The layout: the second role to the right of the root pane, each further one below the one before.
        if i >= 2:
            parent, direction = (s.agents.get(roles[i - 1]) or {}).get("pane"), "down"
        else:
            parent, direction = s.root_pane, "right"
        survivors = [parent, s.root_pane, *(a["pane"] for a in s.agents.values())]
        for pane in dict.fromkeys(p for p in survivors if p and p != old):
            if self.herdr.pane_exists(pane):
                return self.herdr.split(pane, direction, s.work_dir)
        raise OrchestratorError(f"no pane of run {s.run_id} is left in herdr workspace {s.workspace_id}")

    def _pane_order(self, role: str) -> list[str]:
        """The roles in the order their panes are laid out, the root pane going to the first.

        That is the workflow's order, unless the run skipped its contract step (run --spec): then the first
        role to take a turn leads, and the contract's role, which may still have a later step, comes last.
        s.agents keeps the order the roles' agents first started in, and the role asking now is the first
        when none has.
        """
        roles = list(self.pipeline.roles)
        contract = self.pipeline.contract
        first = next(iter(self.state.agents), role)
        if contract is None or first == contract.role:
            return roles
        return [first, *(r for r in roles if r not in (first, contract.role)), contract.role]

    def _start(self, role: str, pane: str, session: str | None = None) -> bool:
        """Start the role's agent in the pane, in its saved session if given one. False means it exited at once."""
        s = self.state
        name, kind = f"{role}-{s.key}", s.agent_kinds[role]
        mode = s.permission_mode
        if mode and permission_args(kind, mode) is None and role not in self._told_dropped:
            self._told_dropped.add(role)
            log(f"[{role}] {kind} has no equivalent of --permission-mode {mode}; starting it without one")
        self.herdr.rename_pane(pane, self.pipeline.roles[role])
        # The handoff files are outside a worktree run's working directory.
        extra_dir = s.dir if s.worktree else None
        args = launch_args(kind, mode, s.models.get(role), session, extra_dir)
        ready = self.herdr.start_agent(name, pane, kind, args)
        s.agents[role] = {"name": name, "pane": pane, "kind": kind}
        self._save()
        if not ready:
            self._ask_human(role, "needs your answer")
            self._wait_for_startup(name)
        record = self.herdr.agent(name)
        if record is None:
            return False
        self._note_session(role, record)
        return True

    def _wait_for_startup(self, name: str) -> None:
        # The human decides how long a startup dialog takes, as with the interview, so there is
        # no deadline; the wait is cut into steps so the heartbeat keeps the run from looking stale.
        while True:
            try:
                self.herdr.wait(name, HEARTBEAT_SECONDS * 1000, until=("idle", "done"))
                return
            except HerdrError as e:
                if e.code != "timeout":
                    raise
            self._heartbeat()

    def _note_session(self, role: str, record: dict) -> None:
        # Saved as soon as herdr reports it, so an agent that exits later can be resumed into it.
        agent = self.state.agents[role]
        session = agent_session(record)
        if session and agent.get("session") != session:
            agent["session"] = session
            self._save()

    def _ask_human(self, role: str, what: str) -> None:
        pane = self.state.agents[role]["pane"]
        self.notify(f"{self.pipeline.roles[role]} {what}", f"pane {pane}")
        log(f"[{role}] {what} (pane {pane})")

    def _change_description(self) -> str:
        s = self.state
        if s.base:
            return f"`git diff {s.base}` in {s.work_dir}, plus the untracked files `git status --porcelain` lists"
        return f"the files the Builder's report lists, in {s.work_dir} (not a git repository)"

    @property
    def _state_path(self) -> str:
        return f"{self.state.dir}/state.json"

    def _save(self) -> None:
        """Write state.json, unless another orchestrator has taken the run over since this one claimed it."""
        saved = self.host.read(self._state_path)
        if saved is not None:
            try:
                owner = json.loads(saved).get("owner")
            except (json.JSONDecodeError, AttributeError) as e:
                raise OrchestratorError(f"corrupt {self._state_path}: {e}") from e
            if owner != self.me:
                who = f"pid {owner['pid']} on {owner['host']}" if owner else "another orchestrator"
                raise RunTakenOver(f"run {self.state.run_id} was taken over by {who}; stopping without saving")
        self._write_state()

    def _write_state(self) -> None:
        self.state.heartbeat_at = self._timestamp()
        self.host.write(self._state_path, json.dumps(asdict(self.state)) + "\n")
        self._last_write = self.clock()

    def _heartbeat(self) -> None:
        if self.clock() - self._last_write < HEARTBEAT_SECONDS:
            return
        try:
            self._save()
        except RunTakenOver:
            raise
        except OrchestratorError as e:
            # A run looks stale only after several missed beats, so one failure costs nothing;
            # the next try comes a full interval later rather than on every poll.
            log(f"heartbeat failed: {e}")
            self._last_write = self.clock()

    def _timestamp(self) -> str:
        return datetime.fromtimestamp(self.wallclock(), timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="orchestrator.py",
        description="Role-based handoff between agent sessions in herdr, each role in the harness of its "
                    "choice; by default Spec Collector -> Builder -> Reviewer, all in Claude Code.")
    sub = p.add_subparsers(dest="command", required=True)
    if not argv and gui_by_default(sys.platform, os.environ):
        argv = ["gui"]

    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--machine", help="saved herdr machine to run the agents on")
    target.add_argument("--cwd", help="project directory (on the machine, if --machine is given)")

    # Unset flags are None: run fills in the defaults, resume keeps what the run was started with.
    settings = argparse.ArgumentParser(add_help=False)
    settings.add_argument("--max-rounds", type=int,
                          help=f"review rounds before giving up (default {DEFAULT_MAX_ROUNDS})")
    settings.add_argument("--timeout", type=int,
                          help=f"seconds a Builder or Reviewer turn may take (default {DEFAULT_TURN_TIMEOUT})")
    settings.add_argument("--permission-mode",
                          help="permission mode for every role, by Claude Code's names, e.g. auto or acceptEdits. "
                               "claude takes any as it is. gemini gets --approval-mode default, auto_edit or yolo "
                               "for default, acceptEdits or bypassPermissions; codex nothing for default, "
                               "--full-auto for acceptEdits, --dangerously-bypass-approvals-and-sandbox for "
                               "bypassPermissions and --sandbox read-only for plan. Any other mode, and every mode "
                               "for opencode and pi, is not passed, and the log says so")
    settings.add_argument("--model", help="model for every role, passed to its harness as --model, "
                                          "e.g. sonnet or claude-opus-5-5")
    for role in ROLE_LABELS:
        settings.add_argument(f"--{role}-model", metavar="MODEL",
                              help=f"model for the {ROLE_LABELS[role]}; overrides --model")
    settings.add_argument("--role-model", metavar="ROLE=MODEL", action="append", type=role_model_arg,
                          help="model for one role of the workflow, by its key; overrides --model and "
                               "the workflow's own model for it; repeatable")
    settings.add_argument("--agent", metavar="KIND", choices=AGENT_KINDS,
                          help=f"agent harness for every role, one of {', '.join(AGENT_KINDS)}; overrides the "
                               f"agent key of a role in the workflow file (default {DEFAULT_AGENT}). Each harness a "
                               f"run uses needs its CLI on PATH and `herdr integration install KIND` where the agents "
                               f"run")
    settings.add_argument("--role-agent", metavar="ROLE=KIND", action="append", type=role_agent_arg,
                          help="agent harness for one role of the workflow, by its key; overrides --agent; "
                               "repeatable")
    settings.add_argument("--max-quality-rounds", type=int, metavar="N",
                          help=f"with --quality-gate: SonarQube analyses per review round before the Reviewer "
                               f"gets the change anyway (default {DEFAULT_MAX_QUALITY_ROUNDS})")

    run = sub.add_parser("run", parents=[target, settings], help="run the handoff workflow for a task")
    run.add_argument("task", nargs="?",
                     help="what to build, as you would tell the Spec Collector; optional with --spec, "
                          "which then takes the spec's title")
    # Run only: a resumed run already has its spec.
    run.add_argument("--spec", metavar="FILE",
                     help="skip the interview: copy the spec at FILE, on this machine, into the run as the "
                          "first step's file, and start at the step after it. Needs a workflow whose first "
                          "step writes the spec, as default's does")
    # Run only: a resumed run keeps the workflow it was started with.
    which = run.add_mutually_exclusive_group()
    which.add_argument("--workflow", choices=offered_workflows(), default=DEFAULT_WORKFLOW.name,
                       help=f"the roles and handoffs the run goes through: a built-in workflow or one in "
                            f"{workflows_dir()} (default {DEFAULT_WORKFLOW.name}); see the workflows command")
    which.add_argument("--workflow-file", metavar="FILE",
                       help="like --workflow, for the workflow file at FILE, on this machine")
    # Run only: a resumed run may already be on its own branch, so whether it ends in a pull request is fixed.
    run.add_argument("--no-pr", action="store_true",
                     help="leave the change uncommitted in the working tree and the workspace open, "
                          "instead of opening a pull request")
    # Run only: a run keeps where it works, its worktree or the checkout.
    run.add_argument("--worktree", action="store_true",
                     help=f"build in a git worktree of the run's own, {WORKTREES_DIR}/<key>, from {REMOTE}'s "
                          f"version of the checked-out branch, instead of in the checkout itself, which then "
                          f"need not be clean and stays free to use. Needs a branch checked out and an {REMOTE}. "
                          f"The worktree is removed once the pull request is open; with --no-pr, or when the "
                          f"run fails, it stays")
    # Run only: turning the gate on or off in the middle of a run is not supported yet.
    run.add_argument("--quality-gate", metavar="JOB",
                     help="after each Builder turn, analyse the change with this Jenkins job (its full name, "
                          "folders included) and SonarQube, and send the findings back to the Builder; "
                          "needs JENKINS_URL, JENKINS_USER, JENKINS_TOKEN, SONAR_HOST_URL and SONAR_TOKEN, "
                          "from the environment or ~/.config/ai-agents-orchestrator/ci.env")

    resume = sub.add_parser(
        "resume", parents=[target, settings], help="continue a run whose orchestrator has stopped",
        description="Continue a run whose orchestrator has stopped. A settings flag overrides what the run "
                    "was started with; an unset one keeps it, not the default shown.")
    resume.add_argument("run_ref", metavar="RUN", help="the run id, or the six-character key at its end")
    resume.add_argument("--force", action="store_true", help="take over a run that still looks alive")
    # Hidden and refused below: argparse would otherwise take --spec for an abbreviation of --spec-model.
    resume.add_argument("--spec", nargs="?", const="", help=argparse.SUPPRESS)

    sub.add_parser("list", parents=[target], help="list the runs in the project directory")

    show = sub.add_parser(
        "show", parents=[target], help="print one run in full: its state, files and last review",
        description="Print one run: its line as list shows it and its whole task, its workflow, branch and "
                    "pull request, each role's agent, the files in its run directory, and the last review.")
    show.add_argument("run_ref", metavar="RUN", help="the run id, or the six-character key at its end")

    prune = sub.add_parser(
        "prune", parents=[target], help="delete the branches of runs whose pull request is done",
        description=f"Delete the branch of each run whose pull request is merged, or closed for "
                    f"{CLOSED_BRANCH_GRACE // 86400} days, locally and on {REMOTE}, and print what became of each. "
                    f"A branch with commits its pull request lacks, or one checked out, is kept. Every run "
                    f"that opens a pull request does this first, except for branches kept before.")
    prune.add_argument("--dry-run", action="store_true", help="print what would be deleted, and delete nothing")

    workflows = sub.add_parser(
        "workflows", help="list the workflows, or print one as a workflow file",
        description=f"Without WORKFLOW, list the workflows `run --workflow` offers: the built-in ones and "
                    f"the workflow files in {workflows_dir()}. With it, print that workflow, or the file "
                    f"at that path, as a workflow file, a starting point for your own.")
    workflows.add_argument("workflow", nargs="?", metavar="WORKFLOW", help="a workflow's name, or a file's path")

    gui = sub.add_parser(
        "gui", help="open a window to start, list and resume runs; the default with no arguments where there "
                    "is a display")
    gui.add_argument("--smoke-test", action="store_true", help="open the window and close it at once")

    args = p.parse_args(argv)
    check_spec_args(args, run, resume)
    if getattr(args, "machine", None) and not args.cwd:
        p.error("--cwd is required with --machine")
    if getattr(args, "max_rounds", None) is not None and args.max_rounds < 1:
        p.error("--max-rounds must be at least 1")
    if getattr(args, "max_quality_rounds", None) is not None:
        if args.max_quality_rounds < 1:
            p.error("--max-quality-rounds must be at least 1")
        if args.command == "run" and not args.quality_gate:
            p.error("--max-quality-rounds needs --quality-gate")
    if getattr(args, "quality_gate", None) is not None and not re.fullmatch(r"[^/]+(/[^/]+)*", args.quality_gate):
        p.error("--quality-gate takes a Jenkins job's full name, such as folder/job")
    return args


def check_spec_args(args: argparse.Namespace, run: argparse.ArgumentParser,
                    resume: argparse.ArgumentParser) -> None:
    """Exit with a usage error for a run with neither a task nor --spec, or a resume given --spec."""
    if args.command == "run" and args.task is None and args.spec is None:
        run.error("a task or --spec FILE is needed")
    if args.command == "resume" and args.spec is not None:
        resume.error("unrecognized arguments: --spec; a resumed run keeps the spec it has")


def offered_workflows() -> list[str]:
    """The workflows `run --workflow` takes."""
    try:
        return workflow_names()
    except OrchestratorError:
        return sorted(WORKFLOWS)  # the workflows command shows why the directory cannot be listed


def role_model_arg(text: str) -> tuple[str, str]:
    role, sep, model = text.partition("=")
    if not (sep and role and model):
        raise argparse.ArgumentTypeError(f"expected ROLE=MODEL, not {text!r}")
    return role, model


def role_agent_arg(text: str) -> tuple[str, str]:
    role, sep, kind = text.partition("=")
    if not (sep and role) or kind not in AGENT_KINDS:
        raise argparse.ArgumentTypeError(f"expected ROLE=KIND, where KIND is one of {', '.join(AGENT_KINDS)}; "
                                         f"not {text!r}")
    return role, kind


def _named_roles(flag: str, pairs, roles) -> dict[str, str]:
    """The ROLE=VALUE pairs of a flag, by role, each role one of the workflow's."""
    named = dict(pairs or [])
    if unknown := set(named) - set(roles):
        raise OrchestratorError(f"{flag} names role {min(unknown)}, which the workflow does not have; "
                                f"its roles are {', '.join(roles)}")
    return named


def role_models(args: argparse.Namespace, roles=ROLE_LABELS) -> dict[str, str]:
    """The model each role starts with by the flags; a role without one keeps its workflow's, or its harness's.

    --role-model names any role; only the default workflow's roles have a --ROLE-model flag too.
    """
    named = _named_roles("--role-model", args.role_model, roles)
    models = {role: named.get(role) or (getattr(args, f"{role}_model") if role in ROLE_LABELS else None)
              or args.model for role in roles}
    return {role: m for role, m in models.items() if m}


def role_agents(args: argparse.Namespace, roles) -> dict[str, str]:
    """The harness each role runs in by the flags; a role without one keeps its workflow's, or the saved one."""
    named = _named_roles("--role-agent", args.role_agent, roles)
    agents = {role: named.get(role) or args.agent for role in roles}
    return {role: kind for role, kind in agents.items() if kind}


def target_args(args: argparse.Namespace) -> list[str]:
    """The --machine and --cwd flags as the human gave them."""
    out = []
    if args.machine:
        out += ["--machine", args.machine]
    if args.cwd:
        out += ["--cwd", args.cwd]
    return out


def resume_command(run_id: str, target: list[str]) -> str:
    return shlex.join(["orchestrator.py", "resume", run_id.rsplit("-", 1)[-1], *target])


def notify_locally(title: str, body: str) -> None:
    """Notify the herdr this orchestrator runs beside, where the human is watching."""
    try:
        Herdr().notify(title, body)
    except OrchestratorError as e:
        log(f"notification failed: {e}")


def format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60}m"


RUNNING = "running"
STALE = "stale"


def run_health(state: dict, age: int, local_host: str, pid_alive) -> tuple[str, str] | None:
    """(RUNNING or STALE, why) for an unfinished run; None for a finished or failed one, which nothing drives.

    age is the seconds since state.json was written, which a live owner does every
    HEARTBEAT_SECONDS. The owner's pid can be checked only on its own host. Host names
    can collide, so a dead pid counts only once a beat is late too; a live pid proves
    nothing, since pids are reused, so the age still applies to it.
    """
    if state.get("phase") == "done" or state.get("error"):
        return None
    owner = state.get("owner")
    if owner and owner.get("host") == local_host and age > HEARTBEAT_SECONDS and not pid_alive(owner["pid"]):
        return STALE, f"orchestrator pid {owner['pid']} exited"
    if age > STALE_SECONDS:
        return STALE, f"no heartbeat for {format_age(age)}"
    if owner:
        return RUNNING, f"pid {owner['pid']} on {owner['host']}, beat {format_age(age)} ago"
    return RUNNING, f"beat {format_age(age)} ago"


def print_runs(runs: list[tuple[int, dict]], local_host: str, pid_alive, target: list[str]) -> None:
    if not runs:
        print("No runs.")
        return
    for run_id, phase, rnd, outcome, task in run_rows(runs, local_host, pid_alive, target):
        print(run_line(run_id, phase, rnd, outcome))
        print(f"    {task}")


def run_line(run_id: str, phase: str, rnd: str, outcome: str) -> str:
    return f"{run_id}  {phase:<7}  {rnd}  {outcome}"


def show_run(host: Host, cwd: str, ref: str, local_host: str, pid_alive, target: list[str]) -> None:
    """Print one run in full: its list line and task, workflow, branch, agents, files and last review."""
    age, s = find_run(host.run_states(cwd), ref, cwd)
    run_id, phase, rnd, outcome, _ = run_rows([(age, s)], local_host, pid_alive, target)[0]
    print(run_line(run_id, phase, rnd, outcome))
    print(f"    {s['task']}")
    # The directory state.json was found in, which outlives a project moved since the run saved its cwd.
    run_dir = f"{cwd}/{RUNS_DIR}/{run_id}"
    pipeline = saved_pipeline(s)
    workflow = s.get("workflow") or DEFAULT_WORKFLOW.name
    print()
    if pipeline:
        print(f"workflow   {workflow}: {workflow_shape(pipeline)}")
    else:
        print(f"workflow   {workflow}: this orchestrator cannot load it")
    for label, value in (("worktree", s.get("worktree")), ("branch", s.get("branch")),
                         ("base", s.get("base_branch")), ("pr", s.get("pr_url")),
                         ("cleanup", s.get("branch_cleanup")),
                         ("conflicts", ", ".join(s.get("conflicts") or [])), ("workspace", s.get("workspace_id"))):
        if value:
            print(f"{label:<9}  {value}")
    agents = s.get("agents") or {}
    width = max((len(role) for role in agents), default=0)
    print_section("agents", [f"{role:<{width}}  {a.get('kind') or DEFAULT_AGENT:<8}  {a.get('pane', '')}"
                             for role, a in agents.items()])
    print_section("files", host.run_files(run_dir))
    review = last_review(host, run_dir, pipeline, s.get("round", 0)) if pipeline else None
    if review:
        path, text = review
        print(f"\nlast review: {path}")
        print(text, end="" if text.endswith("\n") else "\n")


def print_section(title: str, lines: list[str]) -> None:
    """A heading and its lines, indented; nothing when there are no lines."""
    if lines:
        print(f"\n{title}")
        for line in lines:
            print(f"    {line}")


def last_review(host: Host, run_dir: str, pipeline: Pipeline, rnd: int) -> tuple[str, str] | None:
    """The latest round's verdict file that exists, and its text without the VERDICT line; None without one."""
    step = pipeline.verdict_step
    if step is None:
        return None
    for n in range(rnd, 0, -1):
        path = step.path(run_dir, n)
        text = host.read(path)
        if text is None:
            continue
        if parse_verdict(text):
            lines = text.splitlines(keepends=True)
            first = next(i for i, line in enumerate(lines) if line.strip())
            text = "".join(lines[:first] + lines[first + 1:])
        return path, text
    return None


def run_rows(runs: list[tuple[int, dict]], local_host: str, pid_alive,
             target: list[str]) -> list[tuple[str, str, str, str, str]]:
    """Each run as `list` shows it, in the GUI too: its id, phase, round, outcome and task."""
    return [(s["run_id"], s["phase"], run_round(s), run_outcome(s, age, local_host, pid_alive, target),
             s["task"][:100]) for age, s in runs]


def run_outcome(s: dict, age: int, local_host: str, pid_alive, target: list[str]) -> str:
    health = run_health(s, age, local_host, pid_alive)
    if s.get("error"):
        outcome = f"error: {s['error']}"
    elif health is None:
        outcome = s.get("verdict") or ""
    else:
        outcome = f"{health[0]}: {health[1]}"
        if health[0] == STALE:
            outcome += f"; resume: {resume_command(s['run_id'], target)}"
    if s.get("pr_url"):
        outcome += f"  {s['pr_url']}"
    if (workflow := s.get("workflow", DEFAULT_WORKFLOW.name)) != DEFAULT_WORKFLOW.name:
        outcome += f"  [{workflow} workflow]"
    return outcome


def run_round(s: dict) -> str:
    """The run's round, and its quality round while it is in the quality loop or the gated step."""
    rnd = f"round {s['round']}"
    # A workflow this orchestrator lacks has no gated step known, so only phase quality shows the quality round.
    pipeline = saved_pipeline(s)
    gated = pipeline.gated.id if pipeline and pipeline.gated else QUALITY
    if s.get("quality_round") and s["phase"] in (QUALITY, gated):
        rnd += f" q{s['quality_round']}"
    return rnd


def find_run(runs: list[tuple[int, dict]], ref: str, cwd: str) -> tuple[int, dict]:
    """The run named by its full id or by its key."""
    matches = [r for r in runs if r[1].get("run_id") == ref]
    if not matches:
        matches = [r for r in runs if str(r[1].get("run_id", "")).rsplit("-", 1)[-1] == ref]
    if not matches:
        raise OrchestratorError(f"no run {ref} under {cwd}/{RUNS_DIR}")
    if len(matches) > 1:
        ids = ", ".join(r[1]["run_id"] for r in matches)
        raise OrchestratorError(f"{ref} matches several runs ({ids}); give the full run id")
    return matches[0]


def resumable_state(runs: list[tuple[int, dict]], args: argparse.Namespace, cwd: str,
                    local_host: str, pid_alive) -> RunState:
    """The saved state of the run to resume, with the flags given to resume applied."""
    age, saved = find_run(runs, args.run_ref, cwd)
    state = RunState.from_dict(saved)
    pipeline = run_pipeline(state.workflow, state.workflow_definition)
    health = run_health(saved, age, local_host, pid_alive)
    if health and health[0] == RUNNING and not args.force:
        raise OrchestratorError(
            f"run {state.run_id} looks alive ({health[1]}); pass --force to take it over")
    if args.max_rounds is not None:
        state.max_rounds = args.max_rounds
    if args.timeout is not None:
        state.turn_timeout = args.timeout
    if args.permission_mode:
        state.permission_mode = args.permission_mode
    state.models = {**state.models, **role_models(args, pipeline.roles)}
    # Taken up by each role's next agent; one still running keeps its harness.
    state.agent_kinds = {**state.agent_kinds, **role_agents(args, pipeline.roles)}
    if state.phase != DONE and state.max_rounds < state.round:
        raise OrchestratorError(f"run {state.run_id} is already in round {state.round}; "
                                f"--max-rounds {state.max_rounds} is too low")
    if args.max_quality_rounds is not None:
        if not state.quality_job:
            raise OrchestratorError(f"run {state.run_id} has no quality gate, so --max-quality-rounds does not apply")
        state.max_quality_rounds = args.max_quality_rounds
    q = state.quality_round
    # In quality, round q is analysed whatever the limit; in the gated step with q >= 1, it is
    # answering quality round q, and only a round q + 1 would judge that answer.
    gated = pipeline.gated
    if (state.phase == QUALITY and state.max_quality_rounds < q) or \
            (gated and state.phase == gated.id and q >= 1 and state.max_quality_rounds <= q):
        raise OrchestratorError(f"run {state.run_id} is already in quality round {q} of its {state.phase} phase; "
                                f"--max-quality-rounds {state.max_quality_rounds} is too low")
    # The herdr this resume addresses: the saved one may name the same herdr reached another way.
    state.machine = args.machine
    return state


BRANCH_DELETED = "deleted"
BRANCH_KEPT = "kept: "


@dataclass
class PrunedRun:
    """What prune_branches did with one run's branch, locally and on origin, and what it recorded for it."""
    run_id: str
    branch: str
    local: str = ""
    remote: str = ""
    # Why nothing was decided: the pull request is not done yet, or the run failed. Nothing is recorded then.
    skipped: str | None = None
    # The run's branch_cleanup: written, or what would be in a dry run.
    cleanup: str | None = None

    @property
    def line(self) -> str:
        """The run as prune prints it."""
        what = f"skipped: {self.skipped}" if self.skipped else f"local {self.local}, remote {self.remote}"
        return f"{self.run_id}  {self.branch}  {what}"


def prune_branches(host: Host, cwd: str, wall: Callable[[], float], local_host: str, pid_alive, *,
                   dry_run: bool = False, revisit: bool = False) -> list[PrunedRun]:
    """Delete the branches of the runs in cwd whose pull request is merged, or closed for CLOSED_BRANCH_GRACE.

    A branch is deleted only when its pull request's head commit holds all of it: a local branch at or
    behind that commit, and origin's at it, under a lease. GitHub keeps refs/pull/<n>/head, so every deleted
    branch can be restored from its pull request (docs/design.md). Only runs with no branch_cleanup yet
    are candidates, and with revisit the runs whose branch was kept too. A failure on one run is logged and
    skips that run; gh being unusable ends the sweep.
    """
    runs = [s for age, s in host.run_states(cwd) if is_prune_candidate(s, age, local_host, pid_alive, revisit)]
    if not runs:
        return []
    checked_out = host.checked_out_branches(cwd)
    pruned = []
    for saved in runs:
        run = PrunedRun(saved["run_id"], saved["branch"])
        try:
            prune_run(host, cwd, saved, run, wall(), checked_out, dry_run)
        except GhUnusable as e:
            log(f"stopped pruning run branches: {e}")
            break
        except OrchestratorError as e:
            log(f"could not prune the branch of run {run.run_id}: {e}")
            run.skipped = f"failed: {e}"
        pruned.append(run)
    return pruned


def is_prune_candidate(s: dict, age: int, local_host: str, pid_alive, revisit: bool) -> bool:
    """Whether prune may touch the run's branch: one it made and opened a pull request for, and that no
    orchestrator drives now. A run without a pull request may hold the Builder's uncommitted work."""
    branch, cleanup = s.get("branch"), s.get("branch_cleanup")
    if not (isinstance(branch, str) and branch.startswith(BRANCH_PREFIX) and s.get("pr_url")):
        return False
    if cleanup and not (revisit and cleanup.startswith(BRANCH_KEPT)):
        return False
    health = run_health(s, age, local_host, pid_alive)
    return not (health and health[0] == RUNNING)


def prune_run(host: Host, cwd: str, saved: dict, run: PrunedRun, now: float, checked_out: dict[str, str],
              dry_run: bool) -> None:
    """Decide each side of one run's branch, delete what may go and record the result in its state.json."""
    state = RunState.from_dict(saved)
    number = state.pr_url.rstrip("/").rsplit("/", 1)[-1]
    pr = host.pull_request(cwd, state.pr_url)
    if why := pull_request_pending(pr, number, now):
        run.skipped = why
        return
    head = pr["headRefOid"]
    # Each side is decided on its own, so keeping one does not keep the other.
    run.remote, remote_kept = prune_remote(host, cwd, state.branch, head, number, dry_run)
    run.local, local_kept = prune_local(host, cwd, state.branch, head, number, checked_out, dry_run)
    kept = [why for why in (local_kept, remote_kept) if why]
    run.cleanup = BRANCH_KEPT + "; ".join(kept) if kept else BRANCH_DELETED
    if not dry_run:
        record_cleanup(host, f"{cwd}/{RUNS_DIR}/{state.run_id}/state.json", saved, run.cleanup)


def pull_request_pending(pr: dict, number: str, now: float) -> str | None:
    """Why the pull request's branch stays for now; None once it is merged, or closed for CLOSED_BRANCH_GRACE."""
    if pr["state"] == "MERGED":
        return None
    if pr["state"] != "CLOSED":
        return f"PR #{number} is {pr['state'].lower()}"
    try:
        closed = datetime.fromisoformat(pr["closedAt"]).timestamp()
    except (TypeError, ValueError) as e:
        raise OrchestratorError(f"PR #{number} is closed at {pr['closedAt']!r}, which is not a time") from e
    if now - closed > CLOSED_BRANCH_GRACE:
        return None
    return f"PR #{number} was closed less than {CLOSED_BRANCH_GRACE // 86400} days ago"


def prune_remote(host: Host, cwd: str, branch: str, head: str, number: str,
                 dry_run: bool) -> tuple[str, str | None]:
    """What became of origin's branch, and why it was kept, if it was."""
    tip = host.remote_branch_commit(cwd, branch)
    if tip is None:
        if not dry_run:
            host.delete_tracking_ref(cwd, branch)
        return "gone", None
    if tip != head:
        why = f"remote has commits beyond PR #{number}"
        return f"{BRANCH_KEPT}{why}", why
    if not dry_run:
        host.delete_remote_branch_at(cwd, branch, head)
    return deletion(dry_run), None


def prune_local(host: Host, cwd: str, branch: str, head: str, number: str, checked_out: dict[str, str],
                dry_run: bool) -> tuple[str, str | None]:
    """What became of the local branch, and why it was kept, if it was."""
    if branch in checked_out:
        why = f"checked out in {checked_out[branch]}"
        return f"{BRANCH_KEPT}{why}", why
    tip = host.branch_commit(cwd, branch)
    if tip is None:
        return "gone", None
    if tip != head:
        if not host.has_commit(cwd, head):
            why = f"PR #{number}'s head {head[:12]} is not in this clone; fetch it to decide"
            return f"{BRANCH_KEPT}{why}", why
        if not host.is_ancestor(cwd, tip, head):
            n = host.commits_beyond(cwd, head, branch)
            why = f"local has {n} commit{'s' if n != 1 else ''} beyond PR #{number}"
            return f"{BRANCH_KEPT}{why}", why
    if not dry_run:
        host.delete_branch(cwd, branch)
    return deletion(dry_run), None


def deletion(dry_run: bool) -> str:
    return "would be deleted" if dry_run else BRANCH_DELETED


def record_cleanup(host: Host, path: str, saved: dict, cleanup: str) -> None:
    """Add branch_cleanup to the run's state.json, unless the run has saved since it was read.

    The file keeps its modification time, which is the run's heartbeat: a stale run must not look live.
    """
    text = host.read(path)
    try:
        unchanged = text is not None and json.loads(text) == saved
    except ValueError as e:
        raise OrchestratorError(f"corrupt {path}: {e}") from e
    if not unchanged:
        raise OrchestratorError(f"{path} changed while its branch was pruned; branch_cleanup is not recorded")
    host.write(path, json.dumps({**saved, "branch_cleanup": cleanup}) + "\n", keep_mtime=True)


def print_pruned(pruned: list[PrunedRun]) -> None:
    if not pruned:
        print("No run branches to prune.")
    for run in pruned:
        print(run.line)


def sweep_summary(pruned: list[PrunedRun]) -> str | None:
    """The one line a run logs about the branches of earlier runs it pruned; None when it did nothing."""
    deleted = sum(run.cleanup == BRANCH_DELETED and BRANCH_DELETED in (run.local, run.remote) for run in pruned)
    kept = sum(bool(run.cleanup and run.cleanup.startswith(BRANCH_KEPT)) for run in pruned)
    parts = []
    if deleted:
        parts.append(f"deleted {branches(deleted)} of earlier runs")
    if kept:
        parts.append(f"kept {kept}, see prune" if deleted else f"kept {branches(kept)} of earlier runs, see prune")
    return "; ".join(parts) or None


def branches(n: int) -> str:
    return f"{n} branch" if n == 1 else f"{n} branches"


def sweep_branches(host: Host, cwd: str) -> None:
    """Prune the branches of earlier runs, as a new run starts; nothing that goes wrong stops the run."""
    try:
        pruned = prune_branches(host, cwd, time.time, socket.gethostname(), pid_alive)
    except OrchestratorError as e:
        log(f"could not prune the branches of earlier runs: {e}")
        return
    if line := sweep_summary(pruned):
        log(line)


def print_workflows() -> None:
    for name in sorted(WORKFLOWS):
        print_workflow(name, "built in", WORKFLOWS[name])
    for name, path in workflow_files().items():
        try:
            print_workflow(name, path, load_workflow_file(path))
        except OrchestratorError as e:
            print(f"{name}  {path}\n    error: {str(e).removeprefix(f'{path}: ')}")
    print(f"\nAdd one as {workflows_dir()}/NAME.toml; `orchestrator.py workflows {DEFAULT_WORKFLOW.name}` "
          f"prints the default as a file to start from.")


def print_workflow(name: str, source: str, p: Pipeline) -> None:
    print(f"{name}  {source}")
    print(f"    {workflow_shape(p)}")
    if p.description:
        print(f"    {p.description}")


def workflow_arg(ref: str) -> Pipeline:
    """The workflow a name or a path names: a path has a / or ends in .toml."""
    if "/" in ref or ref.endswith(".toml"):
        return load_workflow_file(ref)
    return named_workflow(ref)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    state = None
    try:
        # Workflows are on this machine, so listing them needs no herdr.
        if args.command == "workflows":
            workflows_command(args.workflow)
            return 0
        if args.command == "gui":
            return gui_command(args.smoke_test)
        herdr, host = connect(args.machine)
        cwd = host.resolve_dir(args.cwd or os.getcwd())
        if args.command in RUNS_COMMANDS:
            runs_command(args, host, cwd)
            return 0
        spec = read_spec_file(args.spec) if args.command == "run" and args.spec is not None else None
        # Only a run that opens a pull request: a --no-pr run's project may have no gh.
        if args.command == "run" and not args.no_pr:
            sweep_branches(host, cwd)
        state, pipeline = new_state(args, cwd, spec) if args.command == "run" else resumed_state(args, host, cwd)
        workflow = Workflow(
            herdr, host, state, notify=notify_locally,
            max_rounds=state.max_rounds, turn_timeout=state.turn_timeout,
            agents=AgentSettings(state.permission_mode, state.models, state.agent_kinds),
            pull_request=state.pull_request,
            ci=CI(state.quality_job) if state.quality_job else None,
            max_quality_rounds=state.max_quality_rounds, pipeline=pipeline, spec=spec)
        verdict = workflow.run()
    except OrchestratorError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        hint = f"; resume with: {resume_command(state.run_id, target_args(args))}" if state else ""
        print(f"interrupted; the role agents keep running in herdr{hint}", file=sys.stderr)
        return EXIT_INTERRUPTED
    return finish(verdict, state, pipeline)


# The commands about a project's runs, which start none.
RUNS_COMMANDS = ("list", "show", "prune")


def runs_command(args: argparse.Namespace, host: Host, cwd: str) -> None:
    local_host = socket.gethostname()
    if args.command == "list":
        print_runs(host.run_states(cwd), local_host, pid_alive, target_args(args))
    elif args.command == "show":
        show_run(host, cwd, args.run_ref, local_host, pid_alive, target_args(args))
    else:
        print_pruned(prune_branches(host, cwd, time.time, local_host, pid_alive, dry_run=args.dry_run, revisit=True))


def workflows_command(ref: str | None) -> None:
    if ref:
        print(workflow_toml(workflow_arg(ref)), end="")
    else:
        print_workflows()


def connect(machine: str | None) -> tuple[Herdr, Host]:
    """The herdr the agents run in, and the host the project is on."""
    herdr = Herdr(machine)
    if machine:
        return herdr, Host(herdr.ssh_target())
    if os.environ.get("HERDR_ENV") != "1":
        # Without a machine, herdr commands target whichever session is focused;
        # only a pane herdr manages knows it is talking to its own session.
        raise OrchestratorError("not inside a herdr pane; run from herdr, or pass --machine")
    return herdr, Host()


def new_state(args: argparse.Namespace, cwd: str, spec: SpecFile | None = None) -> tuple[RunState, Pipeline]:
    """A new run's state from the run command's flags, and its workflow; with spec, from --spec, the run
    starts after the contract step."""
    pipeline = load_workflow_file(args.workflow_file) if args.workflow_file else named_workflow(args.workflow)
    first = seeded_start(pipeline) if spec else pipeline.steps[0]
    # parse_args leaves the task out only with --spec.
    task = args.task if args.task is not None else spec.task
    state = RunState(new_run_id(), task, cwd, args.machine, phase=first.id, workflow=pipeline.name)
    state.max_rounds = args.max_rounds or DEFAULT_MAX_ROUNDS
    state.turn_timeout = args.timeout or DEFAULT_TURN_TIMEOUT
    state.permission_mode = args.permission_mode
    state.models = {**pipeline.models, **role_models(args, pipeline.roles)}
    state.agent_kinds = {**pipeline.agents, **role_agents(args, pipeline.roles)}
    state.pull_request = not args.no_pr
    # Its path from the start, so round 0 creates the worktree there.
    state.worktree = f"{cwd}/{WORKTREES_DIR}/{state.key}" if args.worktree else None
    state.quality_job = args.quality_gate
    state.max_quality_rounds = args.max_quality_rounds or DEFAULT_MAX_QUALITY_ROUNDS
    return state, pipeline


def seeded_start(p: Pipeline) -> Step:
    """The step a run given its spec with --spec starts at: the one after the contract step."""
    if p.contract is None:
        raise OrchestratorError(f"workflow {p.name} starts with step {p.steps[0].id}, which edits or writes a "
                                f"file per round; --spec needs a first step that writes the spec")
    if (following := p.after(p.contract)) is None:
        raise OrchestratorError(f"workflow {p.name} has no step after {p.contract.id}, so with --spec "
                                f"it has nothing to do")
    return following


def resumed_state(args: argparse.Namespace, host: Host, cwd: str) -> tuple[RunState, Pipeline]:
    state = resumable_state(host.run_states(cwd), args, cwd, socket.gethostname(), pid_alive)
    return state, run_pipeline(state.workflow, state.workflow_definition)


def finish(verdict: str, state: RunState, pipeline: Pipeline) -> int:
    """Print how the run ended and return its exit status."""
    conflict = f" (a draft: it conflicts with {state.base_branch})" if state.conflicts else ""
    result = pipeline.result.last_path(state.dir, state.round, state.quality_round)
    print(f"{verdict}: {state.pr_url or result}{conflict}")
    if verdict not in SUCCEEDED:
        return EXIT_CHANGES_REQUESTED
    return EXIT_CONFLICT if state.conflicts else 0


# ---------------------------------------------------------------------------
# GUI: a window over the CLI
# ---------------------------------------------------------------------------

# How often the window takes up the running command's output and a finished listing of the runs.
GUI_POLL_MS = 100
# gui --smoke-test keeps the window open this long, so it is drawn before it closes.
SMOKE_TEST_MS = 500
# Offered in the form; it takes any other mode as typed, as --permission-mode does.
PERMISSION_MODES = ("default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan")
HERDR_NOTE = ("The role agents run in herdr: open herdr in a terminal to answer the Spec Collector. "
              "Without a machine, start this window from a herdr pane, as you would the CLI.")

# The window's palette: the colors of docs/logo.svg, with lighter and darker navies for depth. Every text
# color has a contrast of at least 4.5:1 (WCAG AA) on each background it is drawn on, which the tests check.
NAVY = "#1E2A38"         # the window and its header; entries and the run list
NAVY_LIGHT = "#283A4E"   # the section cards, and disabled buttons
NAVY_DARK = "#131B25"    # the output pane
NAVY_RAISED = "#34495F"  # buttons, column headings, borders
NAVY_HOVER = "#3D546C"   # a button under the pointer
PALE_MINT = "#E3F1EF"    # text
MUTED_MINT = "#A3B8BA"   # secondary and disabled text
CORAL = "#EE7B5E"        # Start run, failed runs
CORAL_LIGHT = "#F4987F"  # Start run under the pointer
TEAL = "#2A9D8F"         # focus
MINT = "#7FE0D2"         # selection, approved runs, the echoed command
AMBER = "#F2B33D"        # runs that need a look
# The window's typeface, given to Tk's named fonts for proportional text, which every widget but the output
# pane draws with. Tk cannot load a font file, so where Roboto is not installed the platform's font stays. The
# output pane keeps TkFixedFont, a monospace one, so it still reads as a terminal.
GUI_FONT_FAMILY = "Roboto"
GUI_TEXT_FONTS = ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont", "TkCaptionFont",
                  "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont")
# The zoom levels, as factors of the fonts' sizes at start; Ctrl or Cmd with +, - and 0 steps through them.
ZOOM_LEVELS = (0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)
# Ctrl and the wheel, by event, each with its step; ZOOM_WHEEL_SECONDS is the least time between two of its steps.
# The wheel is Button-4 and -5 on X11, and MouseWheel elsewhere, whose delta's sign says which way (tkinter
# gives the others a delta of 0). Its size is no measure across platforms and Tk versions, and a trackpad
# sends one swipe as a burst of small ones, so time, not the delta, keeps a swipe from running to either end.
# The time is the orchestrator's own clock, not the event's, which wraps around and which no Tk promises to fill.
ZOOM_WHEEL = {"<Control-Button-4>": 1, "<Control-Button-5>": -1, "<Control-MouseWheel>": 1}
ZOOM_WHEEL_SECONDS = 0.25
# The ttk styles the window switches between or sets on a widget, besides defining them.
ACCENT_BUTTON = "Accent.TButton"
PIPELINE_LABEL = "Pipeline.TLabel"
MUTED_LABEL = "Muted.TLabel"

# docs/logo-64.png, docs/logo.svg rendered once for Tk 8.6, which loads no SVG. Regenerated with:
#   uv run --no-project --with resvg-py==0.5.0 python -c "import pathlib, resvg_py; pathlib.Path('docs/logo-64.png')
#   .write_bytes(bytes(resvg_py.svg_to_bytes(svg_path='docs/logo.svg', width=64, height=64)))"
#   base64 -w 110 docs/logo-64.png
LOGO_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAPe0lEQVR4nOVbCXRU1Rn+Zs2eyRABWQIBCfsmUFBU1uoBPEdja4FqKbHYYkUWjy"
    "LWBQKiB1xa0GLluIAcsWIVg6cixcpWZAnIHlkCGpKYjUD2bdb+/515k/fevDeZgQD29DtnMnnv3Xfv///3v/927xjxfw4zrhEKaipGG7xI9dJH"
    "3PBijKKBATvElwF5XvqkJNh34hrAgKuEwtqKwV437hWMGlTMhgsvCYUEYzBhU+d4+xFcBbSqACq8FUm11ZhOszyPZjIVrQjqM4/6XBGfiPftBn"
    "slWgmtIoDihopUpwOLaMbSicgkXEWQICqJ6rUWK1Z2iLHn4QpxxQIoqKpYRF+ZuMZgQbBGpNjsi/m636pV8Yhxz0/yxr36zYwZNeH2Y8Rlgoza"
    "mPzKih9wHZhn+DUtk2nIq6kcZ4h3vRllNR6oNNa8NSYzM2zjflkawLPu9ngyTcbI5GcsLYQ55wCMZYXi2pR/Vny7u/QQ3552neHq9zN42ndGpN"
    "h4NueDd44fMHlhWn8iY84X4b4XkQDYyFVWuLPWnTw6usbZiEcGDEeUuQVhNzbA8u1OWA6SQW9qQDjwRsXAOWwMnENHA9Exuu1K6+pgJA7axsaJ"
    "6/yaqn19O9kmRmIkwxaAsPBV2L4lL3ew0+PGDTGxyK+uxpTe/XXfMR/cCes3X4bNuBosCMdtE+EaNjroWWF1FT4+c4KWggGTe/VHJ3IPvpdwJN"
    "6GseEKISwBSMxT68H1ZO7//O0euMgKzb55BJKjY4NfoFm3bvsMlhP70Rpw9h8Bx7j7AtpQ53TiraPZeGLYbeL6tYO78eigEYixWHwvRCCEFo2F"
    "nHm+jiX/89wtY1DvciFWS/2J+ZiP3qB1/mPwo/o61FSUIzG5LaK0BKcDFqSJ7EbD1NlCCHHEqMPtxKX6euLVS/97mplnEK1MM9HeohBa1ICCyo"
    "rtkURyMWtf1mSe4SLtqa2qQLzNDjMJMlJ42nVCQ8ZT4n+n243Vxw7yXcwcOBwWkyn4BYokU5LsY0P1GVIAZO0z6WsRwoT1643C4AUI9niIaSes"
    "UVGIFJKwLFHRiEuwBe6zYXSM/0UEPWExxQqZeg91/Rj7eUTAvCn3WBDzl0p/RFV5iWAkUjTRUnI2NaKBYms5eAweKwIs8vOiCV0BUCKzBhGAjZ"
    "4cPINeEgKDGYkUMXEJiIqNR4I9ucWx1GDhf7xuHYp+LBbXoXjRFACrfiTJjPn4fhirLinuWUl1mQEDBUsxkouKAEZ6L5GY1zKWPBaPqYd1b62E"
    "vfQTvL1sAS6UlXPUmOpfzsF9qW9wYoMIVJ8hV305mIEbOqQg2h+otCbMOdm6z2LjElFFoYcLFnIaPtdJXnuunzdlP+obpLmZastYU1OLs2d/QO"
    "6Zc6itrUOPHt0wavRI8YzDWz2rfzXBYTSPrRU2T54+A795YD9mzX4YCQk+4XPu4HJgHv07T95WIQD2+Tu2Hfklq01xcSkOHzqG3NzvhQDkSEiI"
    "DwjAdPY4rhd4bL28oU1yGyTZbIp7pAXTicdMeWwgBJDab+Rgr9e7ZlC/iYMDnbezw12mbb1ZIIdIOEOGDAwkNNcDPLbTFwxiw0dZWPPeesVkPT"
    "yjebKZ1peWPZ9kNMRn0OUK6b6wAR6vJ50iqsGP9EnGtNuNSB9mhGVIz5CDl5CGCCIKrqMA/GOztr6+cjUef3QGju3divM5exWfzZ++j9KSMny8"
    "IYt4xVx5H4olsL24FqdPevxXoeN4HtSYn4vrDaahuLwBnTreiBm/nYonn12Kf2Q1Z8MpnTpg9evLMGPaFPzzq+3CI3C9UqoxCg0gj7NDfN8+Eu"
    "16dQtrYLYP4eJ0VRMOEpE1Tk+LbbkNt+V3IgELYG/2Iez+/ix+teYlca9f+s/R83f3YcmyFejbu2cg7KW4IF16z6cB5ugjcDQif99hDHvofrQ5"
    "fgbndmbD2aAfwLBXCEf9Fx4qxefnq8RM3GiPw5PtGtDLph0aM9MP/6eQhODGLT+7GV1Ly/CnnqFdqKAhppP4vzON4Sy7hG0v/E1c5/57D0qOnc"
    "a0CXcpX5KV5IUA8o7sqOw7fDxqyytRXVgCS1w0ZX0WjB0/Gt1TuyC1S2fMeUoZGrCxcTQ5ECqlWX+uUjAvBPH0PNw1bhR9JmHDyLaa7R/fVySY"
    "70eztWHtm/gkazM+3/g27ukSXiDFQs5atxqF/giQkZgYL/pj7QjAgNEKAXCs/NgjT6HE7UD59wUo2H8UA/v3QbeuKTj3w3ls/XqX5oAXL1FmB3"
    "0U1TkD/69Y9S72HTiE06XsWbQFUFTva59z6gyWLF+BfdmHcYfBiXBw6rRPG1kI/FFj67ZdiI1v1ibmmTZfdggB8I5NWs+bcJispIRjJ06Kjx4m"
    "T0lHx1gLQqFjnO/5ay8+jyeefUEwlmAx6bbnZxPunoDComK8u26DuDdpYLuQYxgoaUob2B1xxNyUjFm4dfiQoDasEVu+3ok5c/8QuEcxgSjfm/"
    "0XqWlp3YNenE+DP3hTEnKdFmxrNwBdeqaBBdWhQ3tfJ1TuCgV+l5fA4uUrAgwuGdpetz0/W/jFFrEMGMNuiBV9hIKXCiQcmL2xarlwczv3ZiOO"
    "TPustg70jvbgq2ozTkV1xDPPPh4I3hg06RzzZAXc4KS776R1XUehbq1gcJC3Gj2/86l+msWJ1E5mOGQd6MJkhjHOR7SnrhIbxnUVVr2oPo4Yig"
    "mpNWM7xFP7KNGe23F7dX9wuzTfZZrnzpsp/rduXk9VJF+ucGeiC6Mm3g7XgBGa7/kE4Nu/w5Sp6c0PONv6TtYw9zgcqpfdKT2COozqMRxGa7Sv"
    "WyK26Ww2McJXzdXdnMom9EtSeoLv6F5fuseM39PFJyQD9cP9GUw+Mj0NNdTfASUNPQYE0cC0hgER9erWA6RavQSu7KoLEZ4uaaJyGwARKhEbCh"
    "cbXbh7ax425lWLz6R/5aGiyd3iewarTyMk8NjqXIBpVFeh1bz4XpbZAC14bcnwJLaBsZryfKrfGakMbia1cqcNVLRzUsna+s0W/0guNJ7eA7Pd"
    "Z4Xd1RfgdQTHEqNujMOANtF474wv11g/NgV2a7Bx5HdZg0yJPq/hqihWLAGnRrmcaTQmJpG21IvUlnlgXvQQcmvHRRsfohEz36mbUC3r5g9F5T"
    "dAxNAx8CTYm18iAl3lBeKjxbwEZviJ/jeIjxbzErgPqT858zwmjx0Al+KJNqaRaWWa5TwEwQCREZr9Fzvob5A4eQALbW54qivhqD4s7okSdUGu"
    "TxNoUGP1RRhrIq/5XSl4zOisd2iGk0WpnFVfqko5TvpoFTtMciEpIXKB0AuWOuYNiagvPwzcMrWnsPPSBRhpq+t6g9NhE/zhOC1Tps1d2lyckW"
    "+m6EEsAT6WoteA3UfTxAeaX6D1ZUy4qkcALgtME9MmgWnWc30Mr0GmAXwmh6yiLrgjj62Nz9jl5ui2c/YZCvO5HBgckVeBw4GTGaKoTfLxcngu"
    "lYkPu2bHbROEhwoFg98GBMp/VDX1IgxwHU4qg3EYyoLhHRseUL0xwmiY+ph4FvfyXOX96fOFC7NQNBnwIvDFFo2/ni3+19plkmaW6wD8TKx72i"
    "Yz5Z+D12qFlzTBOXJCi1vstFkieJc5VeyUZ0l64I61OjdUXQxinplh5rUKJ1If6mDKUN1cXm+8bwYJ4RWFX2d7JAlcmuXYlU83tykpELahIWO+"
    "vvtjXv1odoP+Y2qXBdKE6M/eDbrdNOkBzebMQABRSiMl319gBrT6iP7or4GgjDVSHfjwtfmEftlczmtAAAYTsvTa82CsjqzGMasXB0WEvFOjVl"
    "Xew5NmQF04kUePWtrETElgd+vsr/TlzCALXMQkZG+0QvJQxVo5rwEBiBqZF+eDOiJmeTCJQZ4hMdv+YIiJUJ8DkA42BAZsDB2acrQmh1qYjkkP"
    "BgmBwePG/P0NzcqUQssUxOG8/MyhIhLkU1fq9nr7cEZe82S8tA5BNJLhk/tf9d6BV6X2amKNGgzpCUEPWmEyQ82jQgC0o7SWvEyVooFqz4/BDF"
    "i3fyaOv6jBVlqu1mLnSNWHO02Zwak1Qq/WyEJgw6gWoJo2pkHLADJvzKP8nkIAYsfEoG8LJIjMUGON8Qypgw/17PNsq4lTC4QFxl5FC2wT6mcu"
    "Er5evvZF2Evjs/bpBUA8++oTI0GhMEWUmc4mceJT7Cu5KN82h7H9pXdwQZ2ba6mxIvOU3iMr7pTZEQlHjpzAt/sPo/yi1NZfyaI8qU2+G3d0b0"
    "RvjaITz74lSjn7Yhz1DT5+SkERrxNRBm4itTPqnPkRHbPKkatSp8mic942V72nnu3AfVoGRpnr4iRMLYBTJ3NxYPcB3H/vRFitwZWlJocTH36y"
    "CRazGTf1SFU849nXOlqre0SmoLIij552DRBE6918PDswS6zKrIJsbHgGxU6tLbnZ+JHljyWXKffR8jM+QYSQyseuXqJkSBbP094lli5+FS8vWg"
    "CbTb9MXlJ6AS/+ZRUWPCPbBCbLn5JkT9Vqr58NmpABD7ZLlzwbgRlhtyaz8nJXKBFt3bMlKEBp4uxMByxE9XJjD+RizaKx+DxgUVEp4qn6yzX+"
    "TzdtxvML5mL+c0vxytLn8MLylXho2mR065KCkqKyYF50oFsQ4Zo5fS3WfChjnjVD7gqZaCEQVbrMzLWUoKhdlwh4KOeXYLMlCEHwZscI2jli9O"
    "mVBltiAn33QGKC73l8omI3abGfF02Ec0xuR6gcQRGH64DtBFvulnJzRtTGd4KMLtsHLmws37gLC5+chTZ2/XS8oLAIq977AI/NE3sAmyjpSQ81"
    "XosC8B+UZCEM0nquzvK0wL5by0hqN9Y/aOmhKnF2UzSSx9+L4ePGkdFzYNfuZu1zOl34cttO/P6PGSyko/E2jLnig5KMUEKI1glFGaE8RCiwQe"
    "WEJ5Rm5afdgsyvjmDY8JsD92yJiRg15lZi3h4W84xID0sHCUGPWPbrTTTzl3P0XeqXU99Q54/q5iwLXlZehM08I+Lj8iSELLVNYGI5L2BN8FKB"
    "xEW+XhQjw1jzIUHLwUzJGAdFWlpWP3OhOqrcFJ+IjKtyXF6OSI/QtgY4TmBBcIDE2sahsCpQCnkkVrdfXCbE8VM3hZayYOm6gFN48vOhXF0otM"
    "aPpjL9P5Oz4RqCY3v/j6YycQVotZ/N8SFEIirjagtCYtxsxdqfxM/m5GAjWVeNDNaIVl8apOrMOOfzP7kfTmrB/9PZdP/We4vVZk34KtX809ms"
    "/4mfzoYCG00+luI/mcEYLG1R+zcqBYO8Y8ObFpdr1CLFNRPATxX/Bcpb1UE/evGSAAAAAElFTkSuQmCC"
)


def gui_by_default(platform: str, environ) -> bool:
    """Whether no arguments open the GUI: always on macOS, elsewhere when there is a display."""
    return platform == "darwin" or bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def zoom_keys(platform: str) -> dict[str, int]:
    """The key events that zoom the window, each with its step: 1 larger, -1 smaller, 0 back to 100%.

    Key-0, not 0: Tk reads <Control-0> as a click of mouse button 0. Ctrl-+ arrives as Shift-plus on most
    layouts, which <Control-Key-plus> still matches; Ctrl-= is the same key without Shift.
    """
    mod = "Command" if platform == "darwin" else "Control"
    keys = {"plus": 1, "equal": 1, "KP_Add": 1, "minus": -1, "KP_Subtract": -1, "0": 0}
    return {f"<{mod}-Key-{key}>": step for key, step in keys.items()}


def tk_install_hint(platform: str, version) -> str:
    if platform == "darwin":
        return f"brew install python-tk@{version[0]}.{version[1]}"
    return "apt install python3-tk, or dnf install python3-tkinter"


def self_command() -> list[str]:
    """How to start this program again: the PyInstaller binary, the .pyz or orchestrator.py."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    here = os.path.abspath(__file__)
    # In a zipapp, __file__ lies inside the archive, which is a file.
    archive = os.path.dirname(here)
    return [sys.executable, archive if os.path.isfile(archive) else here]


def role_chain(pipeline: Pipeline) -> str:
    """The workflow's roles in the order its steps first use them, as the window's header shows them."""
    return " → ".join(pipeline.roles[role] for role in dict.fromkeys(step.role for step in pipeline.steps))


def status_tag(status: str) -> str | None:
    """The run list's color for a Status as run_outcome words it: ok, warn, error, or None for the text's own."""
    if status.startswith(APPROVE):
        return "ok"
    if status.startswith((CHANGES_REQUESTED, STALE)):
        return "warn"
    if status.startswith("error"):
        return "error"
    return None


def text_colors(background: str) -> dict:
    """A tk.Text's options in the palette: it is no ttk widget, so no style reaches it."""
    return {"background": background, "foreground": PALE_MINT, "insertbackground": MINT, "selectbackground": MINT,
            "selectforeground": NAVY, "inactiveselectbackground": MUTED_MINT, "relief": "flat", "borderwidth": 0,
            "highlightthickness": 1, "highlightbackground": NAVY_RAISED, "highlightcolor": TEAL, "padx": 8,
            "pady": 6}


def _derived_font(font, weight: str, scale: float = 1):
    """A copy of a Tk named font; Tk deletes it once Python drops it, so keep it referenced."""
    copy = font.copy()
    # A negative size is in pixels, a positive one in points; scaling keeps the sign.
    copy.configure(weight=weight, size=round(font.cget("size") * scale))
    return copy


@dataclass
class RunForm:
    """The GUI's run form, as typed. Per-role agents and models are CLI-only."""
    task: str = ""
    cwd: str = ""
    machine: str = ""
    workflow: str = ""
    agent: str = ""
    model: str = ""
    permission_mode: str = ""
    no_pr: bool = False
    quality_gate: str = ""

    def stripped(self) -> "RunForm":
        return RunForm(**{f.name: v.strip() if isinstance(v := getattr(self, f.name), str) else v
                          for f in fields(self)})


def run_args(form: RunForm) -> list[str]:
    """The arguments of the `run` command the form stands for, as one would type them."""
    form = form.stripped()
    if not form.task:
        raise OrchestratorError("the task is empty")
    if not form.cwd:
        raise OrchestratorError("choose a project folder")
    args = ["run", *target_args(form)]
    for flag, value in (("--workflow", form.workflow), ("--agent", form.agent), ("--model", form.model),
                        ("--permission-mode", form.permission_mode), ("--quality-gate", form.quality_gate)):
        if value:
            args += [flag, value]
    if form.no_pr:
        args.append("--no-pr")
    # The task last, after --, so one that starts with - is not taken for a flag.
    return [*args, "--", form.task]


def resume_args(run_id: str, form: RunForm) -> list[str]:
    """The arguments of `resume` for a run in the list of the form's project."""
    return ["resume", run_id, *target_args(form.stripped())]


def project_runs(machine: str, cwd: str) -> list[tuple[int, dict]]:
    """The runs `list` shows for the project.

    Without connect's check for a herdr pane, which guards the herdr commands a run sends: listing a
    local project sends none, so a GUI started from a file manager lists it too.
    """
    host = Host(Herdr(machine).ssh_target()) if machine else Host()
    return host.run_states(host.resolve_dir(cwd))


class RunProcess:
    """A CLI command the GUI started, its stdout and stderr read together on a thread."""

    def __init__(self, argv: list[str], popen=subprocess.Popen):
        # An ignored SIGINT stays ignored across exec, and a Python started so never raises KeyboardInterrupt:
        # in a GUI started that way, as a non-interactive shell starts a background job, Stop would do nothing.
        # exec resets a handler to the default, so handling it here lets the command take Ctrl-C again.
        if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        # A session of its own, so Stop can signal its process group as Ctrl-C in a terminal does.
        # Unbuffered, so the output arrives as it is printed rather than when the run ends.
        self.proc = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace", start_new_session=True,
                          env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self._lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def read(self) -> tuple[str, int | None]:
        """The output since the last read, and the exit status once the output has ended."""
        out = []
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                return "".join(out), None
            if line is None:
                return "".join(out), self.proc.wait()
            out.append(line)

    def interrupt(self, killpg=os.killpg) -> None:
        """Ctrl-C: SIGINT to the command's process group, which ends a run with its resume hint and status 130."""
        if self.proc.poll() is None:
            try:
                killpg(self.proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass  # it has just exited


def _in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class RunWindow:
    """The GUI: a form that starts a run, the project's runs with Resume, and the running command's output.

    Every run goes through the CLI as a child process, one at a time, so a run started here is the one
    typed into a terminal. ui holds the tkinter modules: tk, ttk, font, filedialog and messagebox. Listing
    the runs may go over SSH, so it runs in the background; Tk is touched only from its own thread, which
    takes up the results in _poll.
    """

    def __init__(self, root, ui, start=RunProcess, list_runs=project_runs, background=_in_thread):
        self.root, self.ui = root, ui
        self._start, self._list_runs, self._background = start, list_runs, background
        self.process = None
        self._closing = False
        self._closed = False
        self._listed = None  # the form whose project the run list shows
        self._listings = queue.Queue()
        root.title("ai-agents-orchestrator")
        root.protocol("WM_DELETE_WINDOW", self.close)
        # The header and the form keep their size; at the window's least, the run list and the output still
        # show a few lines each.
        root.minsize(760, 760)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1, minsize=120)
        root.rowconfigure(3, weight=2, minsize=140)
        tk = ui.tk
        # Kept on the window: Tk frees an image as soon as Python drops it.
        self.logo = tk.PhotoImage(master=root, data=LOGO_PNG)
        root.iconphoto(True, self.logo)
        self._theme()
        self.vars = {name: tk.StringVar(root) for name in
                     ("cwd", "machine", "workflow", "agent", "model", "permission_mode", "quality_gate",
                      "runs_note", "pipeline", "zoom")}
        self.vars["workflow"].set(DEFAULT_WORKFLOW.name)
        self.vars["zoom"].set("100%")
        self.vars["no_pr"] = tk.BooleanVar(root)
        self._header()
        self.vars["workflow"].trace_add("write", lambda *_: self._show_pipeline())
        self._show_pipeline()
        self._form_frame()
        self._runs_frame()
        self._log_frame()
        ui.ttk.Label(root, text=HERDR_NOTE, wraplength=720, style=MUTED_LABEL).grid(
            row=4, column=0, sticky="w", padx=16, pady=(0, 12))
        self._set_running(False)
        for event, step in zoom_keys(sys.platform).items():
            root.bind(event, lambda _, step=step: self.zoom(step))
        self._wheel_zoomed = None  # when the wheel last zoomed, by time.monotonic()
        for event, step in ZOOM_WHEEL.items():
            root.bind(event, lambda e, step=step: self._wheel_zoom(e, step if e.delta >= 0 else -step))
        root.after(GUI_POLL_MS, self._poll)

    def _theme(self) -> None:
        """Style every widget the window uses in the palette.

        On clam everywhere, since it takes the colors on every platform; macOS's aqua ignores them.
        """
        root, ttk, font = self.root, self.ui.ttk, self.ui.font
        # Before the copies below, so the section and title headings get the family too.
        if GUI_FONT_FAMILY in font.families(root):
            for name in GUI_TEXT_FONTS:
                font.nametofont(name, root=root).configure(family=GUI_FONT_FAMILY)
        heading = font.nametofont("TkHeadingFont", root=root)
        self.fonts = {"section": _derived_font(heading, "bold"), "title": _derived_font(heading, "bold", 1.5),
                      "command": _derived_font(font.nametofont("TkFixedFont", root=root), "bold")}
        # Each font the window draws with, and its size at 100%, which every zoom level scales from.
        named = [font.nametofont(name, root=root) for name in (*GUI_TEXT_FONTS, "TkFixedFont")]
        self._sizes = [(f, f.cget("size")) for f in (*named, *self.fonts.values())]
        self._zoom = ZOOM_LEVELS.index(1)
        linespace = font.nametofont("TkDefaultFont", root=root).metrics("linespace")
        root.configure(background=NAVY)
        # A combobox's drop-down list is a plain Listbox, which only the option database reaches.
        for option, value in (("background", NAVY), ("foreground", PALE_MINT), ("selectBackground", MINT),
                              ("selectForeground", NAVY), ("font", "TkDefaultFont")):
            root.option_add(f"*TCombobox*Listbox.{option}", value)
        self.style = style = ttk.Style(root)
        style.theme_use("clam")

        def flat(color: str) -> dict:
            """clam's gradient and border colors, all one color: no bevel."""
            return {"background": color, "bordercolor": color, "lightcolor": color, "darkcolor": color}

        def states(disabled: str, active: str) -> list:
            return [("disabled", disabled), ("pressed", active), ("active", active)]

        style.configure(".", background=NAVY_LIGHT, foreground=PALE_MINT, fieldbackground=NAVY,
                        bordercolor=NAVY_RAISED, lightcolor=NAVY_LIGHT, darkcolor=NAVY_LIGHT, troughcolor=NAVY_DARK,
                        selectbackground=MINT, selectforeground=NAVY, insertcolor=MINT, focuscolor=TEAL,
                        arrowcolor=PALE_MINT, font="TkDefaultFont")
        style.map(".", foreground=[("disabled", MUTED_MINT)])
        style.configure("TFrame", background=NAVY)
        style.configure("Card.TFrame", background=NAVY_LIGHT)
        style.configure("Rule.TFrame", background=TEAL)
        style.configure("TLabel", background=NAVY_LIGHT, foreground=PALE_MINT)
        style.configure("Header.TLabel", background=NAVY, foreground=PALE_MINT)
        style.configure("Title.TLabel", background=NAVY, foreground=PALE_MINT, font=self.fonts["title"])
        style.configure(PIPELINE_LABEL, background=NAVY, foreground=MINT)
        style.configure("Section.TLabel", background=NAVY, foreground=PALE_MINT, font=self.fonts["section"])
        style.configure(MUTED_LABEL, background=NAVY, foreground=MUTED_MINT)
        style.configure("TButton", **flat(NAVY_RAISED), foreground=PALE_MINT, padding=(12, 4))
        for option in ("background", "lightcolor", "darkcolor"):
            style.map("TButton", **{option: states(NAVY_LIGHT, NAVY_HOVER)})
        style.map("TButton", foreground=[("disabled", MUTED_MINT)],
                  bordercolor=[("disabled", NAVY_RAISED), ("focus", TEAL), ("active", TEAL)])
        style.configure(ACCENT_BUTTON, **flat(CORAL), foreground=NAVY, focuscolor=NAVY, padding=(16, 4))
        for option in ("background", "lightcolor", "darkcolor"):
            style.map(ACCENT_BUTTON, **{option: states(NAVY_LIGHT, CORAL_LIGHT)})
        style.map(ACCENT_BUTTON, foreground=[("disabled", MUTED_MINT)],
                  bordercolor=[("disabled", NAVY_RAISED), ("focus", PALE_MINT)])
        for widget in ("TEntry", "TCombobox"):
            style.configure(widget, fieldbackground=NAVY, foreground=PALE_MINT, insertcolor=MINT,
                            bordercolor=NAVY_RAISED, lightcolor=NAVY, darkcolor=NAVY, padding=4)
            style.map(widget, bordercolor=[("focus", TEAL), ("active", TEAL)], lightcolor=[("focus", TEAL)],
                      fieldbackground=[("disabled", NAVY_LIGHT)])
        # A read-only combobox selects its text while focused, and clam paints the field the selection's color.
        style.configure("TCombobox", background=NAVY_RAISED)
        style.map("TCombobox", background=states(NAVY_LIGHT, NAVY_HOVER), arrowcolor=[("disabled", MUTED_MINT)],
                  fieldbackground=[("disabled", NAVY_LIGHT), ("readonly", "focus", NAVY_RAISED), ("readonly", NAVY)],
                  foreground=[("disabled", MUTED_MINT), ("readonly", PALE_MINT)],
                  selectbackground=[("readonly", NAVY_RAISED)], selectforeground=[("readonly", PALE_MINT)])
        style.configure("TCheckbutton", background=NAVY_LIGHT, foreground=PALE_MINT, indicatorbackground=NAVY,
                        indicatorforeground=MINT, upperbordercolor=NAVY_RAISED, lowerbordercolor=NAVY_RAISED)
        style.map("TCheckbutton", background=[("active", NAVY_LIGHT)],
                  indicatorbackground=[("disabled", NAVY_LIGHT), ("pressed", NAVY_RAISED), ("active", NAVY_HOVER)],
                  upperbordercolor=[("focus", TEAL)], lowerbordercolor=[("focus", TEAL)])
        style.configure("Treeview", background=NAVY, fieldbackground=NAVY, foreground=PALE_MINT,
                        bordercolor=NAVY_RAISED, lightcolor=NAVY, darkcolor=NAVY, rowheight=linespace + 8)
        # Only the selected state: a map entry for unselected rows would outweigh the status tags' colors.
        style.map("Treeview", background=[("selected", MINT)], foreground=[("selected", NAVY)])
        style.configure("Treeview.Heading", **flat(NAVY_RAISED), foreground=PALE_MINT, relief="flat",
                        font="TkHeadingFont", padding=6)
        style.map("Treeview.Heading", background=[("active", NAVY_HOVER)])
        style.configure("TScrollbar", **flat(NAVY_RAISED), troughcolor=NAVY_DARK, arrowcolor=PALE_MINT,
                        gripcount=0)
        style.map("TScrollbar", background=[("pressed", TEAL), ("active", TEAL)])

    def _header(self) -> None:
        """The logo, the name, and the role chain of the workflow the form has selected."""
        ttk = self.ui.ttk
        h = ttk.Frame(self.root)
        h.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 12))
        h.columnconfigure(1, weight=1)
        ttk.Label(h, image=self.logo, style="Header.TLabel").grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ttk.Label(h, text="ai-agents-orchestrator", style="Title.TLabel").grid(row=0, column=1, sticky="sw")
        # Wrapped, so that a long error, such as a workflow file's path, does not widen the window.
        self.pipeline_label = ttk.Label(h, textvariable=self.vars["pipeline"], style=PIPELINE_LABEL,
                                        wraplength=640)
        self.pipeline_label.grid(row=1, column=1, sticky="nw")
        z = ttk.Frame(h)
        z.grid(row=0, column=2, rowspan=2, sticky="ne")
        self.zoom_buttons = {step: ttk.Button(z, text=text, width=width, command=lambda step=step: self.zoom(step))
                             for step, text, width in ((-1, "−", 2), (0, None, 5), (1, "+", 2))}
        self.zoom_buttons[0].configure(textvariable=self.vars["zoom"])
        for column, step in enumerate((-1, 0, 1)):
            self.zoom_buttons[step].grid(row=0, column=column, padx=(0 if column == 0 else 4, 0))
        ttk.Frame(h, height=2, style="Rule.TFrame").grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))

    def zoom(self, step: int) -> None:
        """Make every font a zoom level larger (1) or smaller (-1), or with 0 its size at start."""
        i = ZOOM_LEVELS.index(1) if step == 0 else min(max(self._zoom + step, 0), len(ZOOM_LEVELS) - 1)
        self._zoom, factor = i, ZOOM_LEVELS[i]
        for f, size in self._sizes:
            # A negative size is in pixels, a positive one in points; scaling keeps the sign.
            f.configure(size=round(size * factor))
        # The run list's rows are as tall as configured, not as their text, so they follow by hand.
        linespace = self.ui.font.nametofont("TkDefaultFont", root=self.root).metrics("linespace")
        self.style.configure("Treeview", rowheight=linespace + 8)
        for name, width in self._column_widths.items():
            self.runs.column(name, width=round(width * factor))
        self.vars["zoom"].set(f"{round(factor * 100)}%")
        self.zoom_buttons[-1].configure(state="normal" if i > 0 else "disabled")
        self.zoom_buttons[1].configure(state="normal" if i < len(ZOOM_LEVELS) - 1 else "disabled")

    def _wheel_zoom(self, event, step: int) -> None:
        now = time.monotonic()
        if self._wheel_zoomed is not None and now - self._wheel_zoomed < ZOOM_WHEEL_SECONDS:
            return
        self._wheel_zoomed = now
        self.zoom(step)

    def _show_pipeline(self) -> None:
        # Static, from the workflow's definition: the window shows no run's live phase.
        try:
            line, style = role_chain(named_workflow(self.vars["workflow"].get())), PIPELINE_LABEL
        except OrchestratorError as e:
            line, style = f"error: {e}", MUTED_LABEL
        self.vars["pipeline"].set(line)
        self.pipeline_label.configure(style=style)

    def _section(self, row: int, title: str):
        """A bold heading over a card; returns the card, which holds the section's widgets."""
        ttk = self.ui.ttk
        s = ttk.Frame(self.root)
        s.grid(row=row, column=0, sticky="nsew", padx=16, pady=(0, 12))
        s.columnconfigure(0, weight=1)
        s.rowconfigure(1, weight=1)
        ttk.Label(s, text=title, style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        card = ttk.Frame(s, style="Card.TFrame", padding=12)
        card.grid(row=1, column=0, sticky="nsew")
        return card

    def _form_frame(self) -> None:
        ttk, v = self.ui.ttk, self.vars
        f = self._section(1, "New run")
        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)
        ttk.Label(f, text="Task").grid(row=0, column=0, sticky="nw", padx=6, pady=4)
        self.task = self.ui.tk.Text(f, height=4, width=80, wrap="word", font="TkDefaultFont", **text_colors(NAVY))
        self.task.grid(row=0, column=1, columnspan=4, sticky="nsew", padx=6, pady=4)
        ttk.Label(f, text="Project folder").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        cwd = ttk.Entry(f, textvariable=v["cwd"])
        cwd.grid(row=1, column=1, columnspan=3, sticky="ew", padx=6, pady=4)
        cwd.bind("<Return>", lambda _: self.refresh())
        ttk.Button(f, text="Choose…", command=self.choose_folder).grid(row=1, column=4, padx=6, pady=4)
        fields_ = (("Machine", ttk.Entry(f, textvariable=v["machine"])),
                   ("Workflow", ttk.Combobox(f, textvariable=v["workflow"], values=offered_workflows(),
                                             state="readonly")),
                   ("Agent", ttk.Combobox(f, textvariable=v["agent"], values=("", *AGENT_KINDS),
                                          state="readonly")),
                   ("Model", ttk.Entry(f, textvariable=v["model"])),
                   ("Permission mode", ttk.Combobox(f, textvariable=v["permission_mode"],
                                                    values=("", *PERMISSION_MODES))),
                   ("Quality gate job", ttk.Entry(f, textvariable=v["quality_gate"])))
        for i, (label, widget) in enumerate(fields_):
            row, col = 2 + i // 2, i % 2 * 2
            ttk.Label(f, text=label).grid(row=row, column=col, sticky="w", padx=6, pady=4)
            widget.grid(row=row, column=col + 1, sticky="ew", padx=6, pady=4)
        fields_[0][1].bind("<Return>", lambda _: self.refresh())
        ttk.Checkbutton(f, text="No pull request (--no-pr)", variable=v["no_pr"]).grid(
            row=5, column=1, sticky="w", padx=6, pady=4)
        self.start_button = ttk.Button(f, text="Start run", command=self.start_run, style=ACCENT_BUTTON)
        self.start_button.grid(row=5, column=4, sticky="e", padx=6, pady=(8, 4))

    def _runs_frame(self) -> None:
        ttk = self.ui.ttk
        f = self._section(2, "Runs of the project")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        stretching = ("outcome", "task")
        columns = (("run", "Run", 190), ("phase", "Phase", 70), ("round", "Round", 70),
                   ("outcome", "Status", 300), ("task", "Task", 300))
        self.runs = ttk.Treeview(f, columns=[c[0] for c in columns], show="headings", height=5,
                                 selectmode="browse")
        for name, heading, width in columns:
            self.runs.heading(name, text=heading)
            self.runs.column(name, width=width, stretch=name in stretching)
        # The fixed columns are pixels, so zoom() scales them with the text, or a run id no longer fits its column
        # from 150%. Not the stretching ones: they take whatever room is left, and scaled, they would widen the
        # window past a laptop's screen.
        self._column_widths = {name: width for name, _, width in columns if name not in stretching}
        for tag, color in (("ok", MINT), ("warn", AMBER), ("error", CORAL)):
            self.runs.tag_configure(tag, foreground=color)
        self.runs.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=6, pady=4)
        ttk.Label(f, textvariable=self.vars["runs_note"]).grid(row=1, column=0, sticky="w", padx=6)
        ttk.Button(f, text="Refresh", command=self.refresh).grid(row=1, column=1, padx=6, pady=(8, 0))
        self.resume_button = ttk.Button(f, text="Resume", command=self.resume_run)
        self.resume_button.grid(row=1, column=2, padx=6, pady=(8, 0))

    def _log_frame(self) -> None:
        ttk = self.ui.ttk
        f = self._section(3, "Output")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self.log = self.ui.tk.Text(f, height=14, wrap="char", state="disabled", font="TkFixedFont",
                                   **text_colors(NAVY_DARK))
        self.log.tag_configure("command", foreground=MINT, font=self.fonts["command"])
        self.log.tag_configure("success", foreground=MINT)
        self.log.tag_configure("failure", foreground=CORAL)
        self.log.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=4)
        bar = ttk.Scrollbar(f, orient="vertical", command=self.log.yview)
        bar.grid(row=0, column=1, sticky="ns", pady=4)
        self.log.configure(yscrollcommand=bar.set)
        self.stop_button = ttk.Button(f, text="Stop", command=self.stop)
        self.stop_button.grid(row=1, column=0, columnspan=2, sticky="e", padx=6, pady=(8, 0))

    def form(self) -> RunForm:
        v = self.vars
        return RunForm(task=self.task.get("1.0", "end-1c"), cwd=v["cwd"].get(), machine=v["machine"].get(),
                       workflow=v["workflow"].get(), agent=v["agent"].get(), model=v["model"].get(),
                       permission_mode=v["permission_mode"].get(), no_pr=v["no_pr"].get(),
                       quality_gate=v["quality_gate"].get())

    def choose_folder(self) -> None:
        path = self.ui.filedialog.askdirectory(parent=self.root, mustexist=True,
                                               initialdir=self.vars["cwd"].get() or os.path.expanduser("~"))
        if path:
            self.vars["cwd"].set(path)
            self.refresh()

    def start_run(self) -> None:
        try:
            args = run_args(self.form())
        except OrchestratorError as e:
            self.ui.messagebox.showerror("Cannot start the run", str(e), parent=self.root)
            return
        self._launch(args)

    def resume_run(self) -> None:
        selected = self.runs.selection()
        if not selected or self._listed is None:
            self.ui.messagebox.showerror("Cannot resume", "Select a run in the list first.", parent=self.root)
            return
        self._launch(resume_args(selected[0], self._listed))

    def _launch(self, args: list[str]) -> None:
        if self.process:
            return
        self._append(f"$ {shlex.join(['orchestrator.py', *args])}\n", "command")
        try:
            self.process = self._start([*self_command(), *args])
        except OSError as e:
            self._append(f"could not start it: {e}\n")
            return
        self._set_running(True)

    def stop(self) -> None:
        if self.process:
            self.process.interrupt()

    def close(self) -> None:
        """Close the window; during a run, once the human confirms, after interrupting it as Stop does."""
        if self.process is None:
            self._destroy()
            return
        if self._closing:
            return
        if self.ui.messagebox.askokcancel(
                "Stop the run?", "A run is going. Closing the window interrupts it, as Ctrl-C would; "
                                 "you can resume it later.", parent=self.root):
            self._closing = True
            self.stop()

    def refresh(self) -> None:
        """List the runs of the form's project, in the background."""
        form = self.form().stripped()
        if not form.cwd:
            return
        self.vars["runs_note"].set("Listing the runs…")

        def fetch():
            try:
                rows = run_rows(self._list_runs(form.machine, form.cwd), socket.gethostname(), pid_alive,
                                target_args(form))
            except OrchestratorError as e:
                rows = e
            self._listings.put((form, rows))

        self._background(fetch)

    def _poll(self) -> None:
        if self.process:
            self._take_output()
        while not self._listings.empty():
            self._show_runs(*self._listings.get_nowait())
        if not self._closed:
            self.root.after(GUI_POLL_MS, self._poll)

    def _take_output(self) -> None:
        out, status = self.process.read()
        if out:
            self._append(out)
        if status is None:
            return
        self.process = None
        self._append(f"[exited with status {status}]\n", "success" if status == 0 else "failure")
        self._set_running(False)
        if self._closing:
            self._destroy()
        else:
            self.refresh()

    def _show_runs(self, form: RunForm, rows) -> None:
        self.runs.delete(*self.runs.get_children())
        if isinstance(rows, OrchestratorError):
            self._listed = None
            self.vars["runs_note"].set(f"error: {rows}")
            return
        self._listed = form
        for row in rows:
            tag = status_tag(row[3])
            self.runs.insert("", "end", iid=row[0], values=row, tags=(tag,) if tag else ())
        self.vars["runs_note"].set("" if rows else "No runs.")

    def _append(self, text: str, *tags: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, *tags)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        idle = "disabled" if running else "normal"
        self.start_button.configure(state=idle)
        self.resume_button.configure(state=idle)
        self.stop_button.configure(state="normal" if running else "disabled")

    def _destroy(self) -> None:
        self._closed = True
        self.root.destroy()


def gui_command(smoke_test: bool) -> int:
    # Imported here, so the CLI and the tests run on a Python without tkinter, and without a display.
    try:
        import tkinter
        from tkinter import filedialog, font, messagebox, ttk
    except ImportError:
        print(f"error: the GUI needs tkinter, which this Python lacks; install it, e.g. with "
              f"{tk_install_hint(sys.platform, sys.version_info)}", file=sys.stderr)
        return EXIT_ERROR
    try:
        root = tkinter.Tk()
    except tkinter.TclError as e:
        print(f"error: cannot open the GUI: {e}", file=sys.stderr)
        return EXIT_ERROR
    window = RunWindow(root, SimpleNamespace(tk=tkinter, ttk=ttk, font=font, filedialog=filedialog,
                                             messagebox=messagebox))
    if smoke_test:
        root.after(SMOKE_TEST_MS, window.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
