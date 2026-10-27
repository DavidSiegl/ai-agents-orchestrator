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
snapshot of the change is judged by a Jenkins job running the quality gate,
gate.py, and a gate that does not pass goes back to the Builder before the
Reviewer sees the change. In another workflow, its quality-gated step takes the Builder's place.
HTTP to Jenkins goes through CI, from the orchestrator's machine.

The gui command, the default with no arguments where there is a display, is a
window over the same CLI: it starts run and resume as child processes.
"""

import argparse
import atexit
import base64
import collections
import contextlib
import difflib
import functools
import hashlib
import http.client
import http.server
import json
import os
import posixpath
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import string
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any, NamedTuple

RUNS_DIR = ".orchestrator/runs"
# Where run --worktree puts each run's worktree, named by the run's key.
WORKTREES_DIR = ".orchestrator/worktrees"
# Where, in a run's working tree, a proof is copied to for its run (Workflow._run_proof), under the round that wrote it.
PROOF_COPIES_DIR = ".orchestrator/proofs"
DEFAULT_TURN_TIMEOUT = 1800
DEFAULT_MAX_ROUNDS = 3
AGENT_START_TIMEOUT_MS = 60_000
POLL_SECONDS = 3
# How long a Builder or Reviewer may sit idle without its handoff file before the human is told.
STALL_SECONDS = 180
# How long one attempt waits for an agent to take a prompt: herdr reports it working or blocked, or its handoff
# file appears. An agent reported idle before its input takes text, as behind a startup dialog, drops the prompt.
PROMPT_DELIVERY_SECONDS = 30
# A live orchestrator rewrites state.json at least this often, so `list` can tell a live run from a dead one.
HEARTBEAT_SECONDS = 60
# A run whose state.json is older than this is stale: five missed beats, which rides out a slow SSH hop
# or a Host.run that hits its 60 s limit.
STALE_SECONDS = 300
# git fetch, git push and gh talk to the network; everything else Host runs is local to the machine.
NETWORK_TIMEOUT = 300
# Seconds gitleaks may take to scan what one push publishes, which is local work.
GITLEAKS_TIMEOUT = 300
# What the preflight tells a user whose machine lacks gitleaks with its `git` subcommand (8.19 and later).
GITLEAKS_INSTALL = ("install gitleaks 8.19 or later where the agents run (https://github.com/gitleaks/gitleaks#installing; "
                    "on Arch, `pacman -S gitleaks`)")
# The comment that makes gitleaks skip its line. Spelled in two halves so that a change moving the line that holds it
# is not refused by the push's own check for added allow comments (Host._allow_comments).
GITLEAKS_ALLOW = "gitleaks" ":allow"
# How many `[extend] path` steps gitleaks follows from its --config: the files one and two steps away are read, and an
# [extend] in the second is not (maxExtendDepth in gitleaks' config/config.go; 8.30.1 checked). A gitleaks that
# follows more steps finds no copy of the third file in Host._write_base_rules' directory, and fails to load.
GITLEAKS_EXTEND_DEPTH = 2
# The namespace that marks a branch as a run's, locally and on origin; prune touches no branch outside it.
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
# The first line of a report whose agent cannot go on without the human (parse_blocked).
BLOCKED = "BLOCKED:"
# How a BLOCKED line with nothing after the colon shows its question.
NO_QUESTION = "(no question given)"

# The quality gate. Requests time out below STALE_SECONDS, so a hung one cannot make a live run look stale.
DEFAULT_MAX_QUALITY_ROUNDS = 3
# A workflow's max_files and max_lines when it sets none (Pipeline).
DEFAULT_MAX_FILES = 15
DEFAULT_MAX_LINES = 600
# The workflow keys that hold them; a workflow file sets each to a whole number of at least 1.
BUDGET_KEYS = ("max_files", "max_lines")
HTTP_TIMEOUT = 30
# Seconds each blocking step of a request to PyPI may take, to connect or to read, so one request can take it more
# than once. What bounds the new-dependencies check as a whole is the quality round's deadline (SLOW_STEP_MARGIN).
PYPI_TIMEOUT = 15
CI_POLL_SECONDS = 10
# How long one quality round may wait on Jenkins, failed requests retried, before the run fails.
QUALITY_TIMEOUT = 1200
# The orchestrator's own environment variables that hold the credentials; no process it starts inherits them.
CI_ENV = ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN")
CI_TOKENS = ("JENKINS_TOKEN",)
# git's switches for how it reads every pathspec, which no process the orchestrator starts inherits: its pathspecs are
# written in git's default syntax, and each switch changes what one matches without an error, as under ICASE a
# `:(literal)` path such as _top_literal's matches a file whose name differs in case. The gate leaves them out of
# its own processes' environment for its own pathspecs, as gate.py's tool_env says.
GIT_PATHSPEC_ENV = ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS")
JOB_PARAMETERS = ("GIT_REF",)
# Snapshots are pushed to orchestrator-ci/<key>-<round>-q<quality round>.
CI_REF_PREFIX = "orchestrator-ci/"
# A run's local refs are under refs/orchestrator/<key>/ (RunState.refs_prefix).
LOCAL_REFS_PREFIX = "refs/orchestrator/"
# How much of a build's console the quality file of a build that failed before its Gate stage shows.
CONSOLE_TAIL_LINES = 60

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
# drain ran every spec it could, and specs remain whose blockers are not done, such as a pull request still open.
EXIT_WAITING = 5
EXIT_INTERRUPTED = 130


class OrchestratorError(Exception):
    """A failure that ends the run; the message is shown to the user."""


@dataclass(frozen=True)
class Leak:
    """A secret gitleaks found in a commit a push would publish, or a `gitleaks:allow` comment the push would add.
    It holds where the secret is, never the secret: nothing built from a Leak can carry it into a log, a prompt, a
    pull request or state.json."""
    rule: str  # gitleaks' RuleID, or "gitleaks-allow" for the comment
    file: str  # the path in that commit
    line: int  # its line there, from 1
    commit: str  # the full sha of the commit that adds it
    fingerprint: str  # gitleaks' Fingerprint, commit:file:rule:line, which a .gitleaksignore at the base can list

    def __str__(self) -> str:
        return f"`{self.file}:{self.line}` {self.rule} in {self.commit[:12]} (fingerprint `{self.fingerprint}`)"


class LeaksFound(OrchestratorError):
    """A push refused because what it would publish holds a secret, as Host.refuse_leaks raises it."""

    def __init__(self, leaks: list[Leak], report_path: str):
        self.leaks = leaks
        super().__init__(f"{self.found()} in what the push would publish, so nothing was pushed "
                         f"(report: {report_path}):\n" + "\n".join(f"- {leak}" for leak in leaks))

    def found(self) -> str:
        """How many secrets gitleaks found, as a clause: "gitleaks found 2 secrets"."""
        n = len(self.leaks)
        return f"gitleaks found {n} secret{'s' if n != 1 else ''}"


class HerdrError(OrchestratorError):
    def __init__(self, code: str, message: str):
        super().__init__(f"herdr {code}: {message}")
        self.code = code


def child_env() -> dict[str, str]:
    """The environment for every process the orchestrator starts: its own, without the CI credentials and without
    GIT_PATHSPEC_ENV.

    So the agents, which herdr, git, ssh and gh start or reach, never inherit the tokens. They can
    still read a file the tokens came from, as the same user. And the git the orchestrator runs reads each pathspec
    in its default syntax, whatever switch the user exported. An agent does not inherit the switches either, but its
    pane's shell reads the user's startup files and takes any they set. Over SSH this is the local ssh process's
    environment; the remote git reads the remote shell's.
    """
    return {k: v for k, v in os.environ.items() if k not in CI_ENV and k not in GIT_PATHSPEC_ENV}


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
        # newline="": parse_env_file must see a carriage return to refuse it.
        with open(path, encoding="utf-8", newline="") as f:
            if os.fstat(f.fileno()).st_mode & 0o077:
                raise OrchestratorError(f"{path} holds tokens but others may read it; run: chmod 600 {path}")
            text = f.read()
    except FileNotFoundError:
        return found
    except (OSError, UnicodeDecodeError) as e:
        raise OrchestratorError(f"could not read {path}: {e}") from e
    return {**parse_env_file(text, path), **found}


# An assignment line of an env file, blanks at its ends removed, in the forms sh reads literally: `export ` or
# not, NAME=, then a value that is single-quoted, double-quoted without what expands inside double quotes, or
# unquoted without blanks, quotes, a ~ (expanded at the start and after a ":"), a { (brace-expanded in an
# argument of export) or what sh treats as syntax there; then nothing, or blanks and a comment. No value holds a
# NUL, which sh drops. Group 1 is the name, 2 to 4 the value in its form.
_ENV_ASSIGNMENT = re.compile(r"""(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)="""
                             r"""(?:'([^'\x00]*)'|"([^"\\$`\x00]*)"|([^ \t'"\\$`;&|<>()~{\x00]*))"""
                             r"""(?:[ \t]+(?:#.*)?)?""", re.DOTALL)


def parse_env_file(text: str, path: str) -> dict[str, str]:
    """The NAME=value lines of text, with the quotes, `export ` and comments that a file also sourced by a shell has.

    Whatever it accepts means the same to sh: a line ends at "\n" alone, only spaces and tabs are blanks, and a
    value is quoted, or unquoted without blanks, quotes or shell syntax. Raises OrchestratorError, naming path and
    line but never its content, for a line in any other form, which sh would read otherwise or not at all, and
    for a carriage return outside a comment, which a file with Windows line ends puts at the end of every value.
    """
    values = {}
    for i, raw in enumerate(text.split("\n"), 1):
        line = raw.strip(" \t")
        if not line or line.startswith("#"):
            continue
        if "\r" in line:
            raise OrchestratorError(f"{path}:{i}: a carriage return; save the file with Unix line ends")
        m = _ENV_ASSIGNMENT.fullmatch(line)
        if not m:
            raise OrchestratorError(f"{path}:{i}: expected NAME=value")
        values[m.group(1)] = next(v for v in m.group(2, 3, 4) if v is not None)
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

# Appended at run time to every prompt of a step that can block (Step.can_block), so workflow definitions,
# and what `workflows` prints, never contain it.
BLOCKED_NOTE = """\
If you cannot continue without a decision from the human, write the report with a first line \
`BLOCKED: <your question>` instead."""

# For an agent whose report was BLOCKED, while the human answers it in the agent's pane.
BLOCKED_PROMPT = """\
Your report asked the human a question and has been moved to {blocked_path}. \
The human has been notified and will answer you in this pane. \
Once the question is resolved, finish the work and write {path} again in a single write."""

# Appended for a Builder or Reviewer that starts a fresh session in a later round: it has not seen the earlier ones.
REBUILD_NOTE = """\
This is round {n}, and you are a fresh session. An earlier Builder session did the earlier turns; \
its changes are already in the working tree, and its reports are {earlier_build_paths}."""

REREVIEW_NOTE = """\
This is round {n}, and you are a fresh session. The earlier reviews of this change are {earlier_review_paths}; \
check that the Builder has answered each of their findings."""

QUALITY_FIX_PROMPT = """\
The quality gate did not pass on your change; the findings are in {quality_path}. \
Fix each numbered issue, the failed conditions, the failing tests and a failed build, \
or explain in your report why one should stand. Answer each numbered finding to weigh the same way; \
those never fail the gate, but the Reviewer reads your answers. Re-run the verification. \
Do not commit, push or switch branches. As your last step, write a new report to {report_path} in a single write, \
in the same shape as before, answering each numbered issue and finding to weigh by its number."""

# Appended to the Reviewer's prompt in a run with the quality gate.
QUALITY_PASSED_NOTE = """\
The quality gate passed on this change; its report is in {quality_path}."""

QUALITY_UNRESOLVED_NOTE = """\
The quality gate still did not pass after the Builder's last quality round; \
its unresolved findings are in {quality_path}. Weigh them as you would your own."""

# QUALITY_UNRESOLVED_NOTE for a quality loop that stopped because a round failed on just what the one before it had.
QUALITY_STUCK_NOTE = """\
The quality gate still did not pass, and its last quality round failed on just what the round before it \
failed on, so the Builder was not asked again; its unresolved findings are in {quality_path}. \
Weigh them as you would your own."""

# In place of the notes above when the round's change went unjudged (Workflow._unjudged) and the round has a quality
# file: resume --no-quality-gate turned the gate off after it, and the Builder changed the code since. The pull
# request says so too (pr_body).
QUALITY_OFF_NOTE = """\
The quality gate was turned off before the Builder's latest change was analysed, so nothing judged that change; \
the round's earlier quality files are {quality_paths}, and the Builder's reports since answer them."""

# Appended to the prompt of the step a review loops back to, when tests the Reviewer wrote fail against the change.
PROOFS_NOTE = """\
The Reviewer backed findings with tests, and these do not pass against your change: {proofs}. \
Make each of them pass, without changing it; you may move one into the project as a regression test. \
The orchestrator runs each from a copy of its directory under the working tree's `""" + PROOF_COPIES_DIR + """/`, \
so that the working tree's test configuration applies; to run one yourself, copy it there or into the project, not \
where it lies. If one is wrong, say so in your report instead."""

# Appended to the verdict step's prompt, after the quality and locks notes, when proofs carried from an earlier round
# (Workflow._carried_proofs) were run again against the round's change (Workflow._rerun_proofs); proofs is a list item
# per proof, as proofs_section writes them.
CARRIED_PROOFS_NOTE = """\
Proofs that failed against the change in an earlier round were run again against this round's change, with \
`{command}` in {cwd}, each from a copy of its directory under `{cwd}/""" + PROOF_COPIES_DIR + """/`, removed \
after each run; a path to a proof in their output is the copy's:

{proofs}

Weigh a proof that still fails, or now errors, as a finding that stands, unless a report shows the proof \
itself is wrong."""

# Appended to the verdict step's prompt when a turn of the round changed files that an earlier step locked.
LOCKED_NOTE = """\
This round changed files that step {steps} locked: {paths}. The orchestrator restored them, \
so the change you review does not hold those edits; they are in {diff_paths}. \
If a report says a locked file is wrong, weigh that claim against the diff."""


@dataclass(frozen=True)
class Prompts:
    """The texts a run sends agents besides a workflow file's own prompts: the default workflow's steps, the
    follow-ups and the notes. Each field defaults to the constant of its name above, and a prompt file of that name
    replaces it (load_prompts), so a text added here is overridable at once.

    Each is a str.format template whose placeholders are the built-in text's; BLOCKED_NOTE has none.
    """
    SPEC_PROMPT: str = SPEC_PROMPT
    BUILD_PROMPT: str = BUILD_PROMPT
    FIX_PROMPT: str = FIX_PROMPT
    REVIEW_PROMPT: str = REVIEW_PROMPT
    RECHECK_PROMPT: str = RECHECK_PROMPT
    CONTINUE_PROMPT: str = CONTINUE_PROMPT
    RETRY_VERDICT_PROMPT: str = RETRY_VERDICT_PROMPT
    BLOCKED_NOTE: str = BLOCKED_NOTE
    BLOCKED_PROMPT: str = BLOCKED_PROMPT
    REBUILD_NOTE: str = REBUILD_NOTE
    REREVIEW_NOTE: str = REREVIEW_NOTE
    QUALITY_FIX_PROMPT: str = QUALITY_FIX_PROMPT
    QUALITY_PASSED_NOTE: str = QUALITY_PASSED_NOTE
    QUALITY_UNRESOLVED_NOTE: str = QUALITY_UNRESOLVED_NOTE
    QUALITY_STUCK_NOTE: str = QUALITY_STUCK_NOTE
    QUALITY_OFF_NOTE: str = QUALITY_OFF_NOTE
    PROOFS_NOTE: str = PROOFS_NOTE
    CARRIED_PROOFS_NOTE: str = CARRIED_PROOFS_NOTE
    LOCKED_NOTE: str = LOCKED_NOTE

    @property
    def overrides(self) -> dict[str, str]:
        """Name -> text of each field that differs from the built-in text, in declaration order; Prompts(**overrides)
        gives these prompts back."""
        return {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) != f.default}


# The prompts with no prompt file: what a run without any, or saved before them, sends.
BUILT_IN_PROMPTS = Prompts()


def prompts_dir() -> str:
    """Where a new run finds its prompt files, NAME.md for the field NAME of Prompts, on the orchestrator's machine."""
    return os.path.join(config_dir(), "prompts")


def prompt_files() -> dict[str, str]:
    """Name -> path of each .md file in prompts_dir(), unread, in name order; none without the directory. Other files
    are not prompt files, as an editor's backup is not."""
    try:
        names = os.listdir(prompts_dir())
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise OrchestratorError(f"cannot list {prompts_dir()}: {e.strerror}") from e
    return {stem: os.path.join(prompts_dir(), name) for name in sorted(names)
            for stem, ext in [os.path.splitext(name)] if ext == ".md"}


def load_prompts() -> Prompts:
    """The prompts a new run starts with: the built-in texts, each one a prompt file names replaced by the file's
    content, less one trailing newline.

    Raises OrchestratorError, naming the file, for one that names no field of Prompts, cannot be read, is not UTF-8,
    or uses other placeholders than the built-in text (prompt_problem).
    """
    names = {f.name for f in fields(Prompts)}
    overrides = {}
    for name, path in prompt_files().items():
        if name not in names:
            close = difflib.get_close_matches(name, names, n=1)
            hint = f"did you mean {close[0]}.md?" if close else f"the names are {', '.join(sorted(names))}"
            raise OrchestratorError(f"{path}: {name} names no built-in prompt; {hint}")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().removesuffix("\n")
        except OSError as e:
            raise OrchestratorError(f"cannot read prompt file {path}: {e.strerror}") from e
        except UnicodeDecodeError as e:
            raise OrchestratorError(f"{path}: not UTF-8 text: {e}") from e
        if problem := prompt_problem(name, text):
            raise OrchestratorError(f"{path}: {problem}")
        overrides[name] = text
    return Prompts(**overrides)


def prompt_problem(name: str, text: str) -> str | None:
    """What stops text from replacing the built-in prompt name, a field of Prompts, or None: a placeholder the
    built-in text lacks or a missing one it has, or a brace, format spec or conversion that cannot be filled in."""
    built_in = getattr(BUILT_IN_PROMPTS, name)
    try:
        used = set(_fields_of(text))
    except ValueError as e:
        return f"{e}; write a literal brace as {{{{ or }}}}"
    wanted = set(_fields_of(built_in))
    takes = f"{name} takes {', '.join(f'{{{p}}}' for p in sorted(wanted))}" if wanted else f"{name} takes none"
    if extra := used - wanted:
        return f"placeholder {{{min(extra)}}} is not one of the built-in text's; {takes}"
    if missing := wanted - used:
        return f"placeholder {{{min(missing)}}} is missing; {takes}"
    try:
        text.format(**dict.fromkeys(wanted, ""))
    except (ValueError, KeyError, IndexError, AttributeError) as e:
        return f"cannot be filled in: {type(e).__name__}: {e}"
    return None


# ---------------------------------------------------------------------------
# Workflow definitions
# ---------------------------------------------------------------------------

# Run phases that are no step's: the quality loop after a quality-gated step's turn, and the run's last two.
QUALITY = "quality"
PUBLISH = "publish"
DONE = "done"

# The placeholders a step's prompts may use besides the per-step paths (see Step).
PROMPT_FIELDS = {"task", "cwd", "n", "change", "approve", "changes", "path", "proofs_dir"}
# The per-step paths: (prefix, suffix) around a step's id.
PATH_FIELDS = (("", "_path"), ("prev_", "_path"), ("earlier_", "_paths"))


@dataclass(frozen=True)
class Step:
    """One role's turn in a workflow, which ends when the role writes its handoff file.

    A prompt is formatted with PROMPT_FIELDS, where n is the round, path the turn's own file and
    proofs_dir the round's directory for the verdict step's proofs (Pipeline.proof_command), and
    with three paths for every step X of the workflow: X_path in round n,
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
    # Locks the change as it stands when each of this editing step's turns ends: every file it then changes from the
    # base. A later turn of another editing step that changes one has it restored, and the verdict step hears of it
    # (Workflow._hold_locks).
    locks: bool = False

    @property
    def per_round(self) -> bool:
        return "n" in _fields_of(self.file)

    @property
    def can_block(self) -> bool:
        """Whether a BLOCKED report pauses the turn for the human. A human-paced step has the human at hand
        already, and the verdict step's file must start with its verdict."""
        return not self.human_paced and self.loop_to is None

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
    # The size of change past which a quality round asks the Builder why (diff_budget): files touched, and lines
    # added plus lines removed. The workflow's, not the gated step's: the change a quality round judges runs from the
    # run's base to the snapshot, so it holds every editing step's work, not only the gated step's.
    max_files: int = DEFAULT_MAX_FILES
    max_lines: int = DEFAULT_MAX_LINES
    # A shell command, with {proofs} for the path of a copy of one proof file inside the working tree
    # (Workflow._run_proof), that runs the proof in the working tree and exits 1 when the proof fails, as pytest does.
    # With it, each proof the verdict step writes in proofs_dir with a review that requests changes is run, and the
    # review gains the outcomes (Workflow._judge_proofs). None runs none.
    proof_command: str | None = None

    def __post_init__(self):
        if problem := self._problem():
            raise ValueError(f"workflow {self.name}: {problem}")

    def _problem(self) -> str | None:
        """What makes the definition unusable, or None; the first problem found, in the order of the checks."""
        if not self.steps:
            return "has no steps"
        checks = (self._name_problem, self._role_problem, self._uniqueness_problem, self._step_problem,
                  self._collision_problem, self._loop_problem, self._budget_problem, self._lock_problem,
                  self._proof_problem)
        return next((problem for check in checks if (problem := check())), None)

    def _budget_problem(self) -> str | None:
        for key in BUDGET_KEYS:
            if getattr(self, key) < 1:
                return f"{key} must be at least 1"
        return None

    def _proof_problem(self) -> str | None:
        if self.proof_command is None:
            return None
        if self.verdict_step is None:
            return "proof_command runs the verdict step's proofs, and the workflow has no verdict step"
        try:
            names = _fields_of(self.proof_command)
        except ValueError as e:
            return f"proof_command: {e}; write a literal brace as {{{{ or }}}}"
        if names != ["proofs"]:
            return "proof_command must name the proof file once, as {proofs}, and no other placeholder"
        return None

    def _lock_problem(self) -> str | None:
        """A locking step that is not the first editing step, or that the verdict loop runs again. Host.lock locks
        what the change holds when the step's turn ends, which is only the step's own work when no editing turn
        came before it."""
        for st in (st for st in self.steps if st.locks):
            if not st.edits:
                return f"step {st.id} locks the files it changes, so it must edit"
            if st is not self.first_edit:
                return f"step {st.id} locks the files it changes, so it must be the first editing step"
            verdict = self.verdict_step
            ids = [x.id for x in self.steps]
            if verdict and verdict.loop_to in ids and ids.index(verdict.loop_to) <= ids.index(st.id):
                return f"step {st.id} locks the files it changes, so the verdict loop must not run it again"
        return None

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


# Written into the run directory whenever the run ends: what it did, and where its time went.
SUMMARY_FILE = "summary.md"
# Written into the run directory when the run is done: run_metrics, for the metrics command.
METRICS_FILE = "metrics.json"
# Files the run writes into its directory itself, beside the steps' handoff files.
RUN_FILE = re.compile(r"state\.json|summary\.md|metrics\.json|quality-.*|locked-.*|proofs-.*")


def _fill_problem(step: Step, known: set[str]) -> str | None:
    """What stops the step's file or prompts from being filled in at run time, which a bad format spec,
    conversion or nested field would, though its placeholders are all known."""
    # A plain {n}, so that _name_tokens knows every name it can give.
    if any(spec or conversion for _, field, spec, conversion in string.Formatter().parse(step.file) if field):
        return f"handoff file {step.file} may use {{n}} only as it is, with no format spec or conversion"
    name = step.file.format(n=1)
    if "/" in name or name in ("", ".", "..") or RUN_FILE.fullmatch(name):
        return f"handoff file {step.file} must be a plain file name, and not state.json, {SUMMARY_FILE}, " \
               f"{METRICS_FILE}, quality-*, locked-* or proofs-*, which the run writes itself"
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


def default_workflow(prompts: Prompts) -> Pipeline:
    """The default workflow, its steps worded by prompts."""
    return Pipeline("default", dict(ROLE_LABELS), (
        Step("spec", "spec", "spec.md", prompts.SPEC_PROMPT, human_paced=True),
        Step("build", "build", "build-{n}.md", prompts.BUILD_PROMPT, again=prompts.FIX_PROMPT,
             fresh_note=prompts.REBUILD_NOTE, fresh_repeats_again=True, edits=True, quality_gated=True),
        Step("review", "review", "review-{n}.md", prompts.REVIEW_PROMPT, again=prompts.RECHECK_PROMPT,
             fresh_note=prompts.REREVIEW_NOTE, loop_to="build"),
    ))


DEFAULT_WORKFLOW = default_workflow(BUILT_IN_PROMPTS)

# The workflows `run --workflow` offers, by name, worded by the built-in prompts (find_workflow words them by others).
WORKFLOWS = {w.name: w for w in (DEFAULT_WORKFLOW,)}


def find_workflow(name: str, prompts: Prompts) -> Pipeline:
    """The built-in workflow of that name, worded by prompts; OrchestratorError when there is none."""
    if name == DEFAULT_WORKFLOW.name:
        # The one built-in workflow whose steps take their texts from Prompts.
        return default_workflow(prompts)
    try:
        return WORKFLOWS[name]
    except KeyError:
        raise OrchestratorError(f"unknown workflow {name}; this orchestrator has "
                                f"{', '.join(sorted(WORKFLOWS))}") from None


def is_built_in(p: Pipeline, prompts: Prompts) -> bool:
    """Whether p is the built-in workflow of its name as prompts word it, which a run need not save the definition of:
    its name and prompts give it back (run_pipeline)."""
    return p.name in WORKFLOWS and find_workflow(p.name, prompts) == p


# ---------------------------------------------------------------------------
# Workflow files
# ---------------------------------------------------------------------------

# A workflow file is TOML holding a workflow's definition, named after the file:
#
#   description = "..."                      # optional, shown by `workflows`
#   max_files = 15                           # optional: the change's budget (Pipeline), 15 files and 600 lines
#   max_lines = 600                          # when left out
#   proof_command = "python -m pytest {proofs}"   # optional: runs the Reviewer's proofs (Pipeline)
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

WORKFLOW_KEYS = {"description", "roles", "steps", "proof_command", *BUDGET_KEYS}
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


def named_workflow(name: str, prompts: Prompts) -> Pipeline:
    """The built-in workflow of that name, or else the one in workflows_dir(), worded by prompts as load_workflow_file
    words one."""
    if name in WORKFLOWS:
        return find_workflow(name, prompts)
    if path := workflow_files().get(name):
        return load_workflow_file(path, prompts)
    raise OrchestratorError(f"unknown workflow {name}; there are {', '.join(workflow_names())}")


def load_workflow_file(path: str, prompts: Prompts) -> Pipeline:
    """The workflow the file at path defines, a step that uses one of the default workflow's taking its texts from
    prompts; OrchestratorError, naming the file, when it cannot be read or defines none."""
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
        return parse_workflow(name, data, prompts)
    except ValueError as e:
        raise OrchestratorError(f"{path}: {e}") from e


def parse_workflow(name: str, definition: dict, prompts: Prompts) -> Pipeline:
    """The workflow a definition describes, a step with a use taking the fields of that step of the default workflow
    as prompts words it; ValueError names what is wrong with it."""
    _check_keys(definition, WORKFLOW_KEYS, "the file")
    raw_steps = definition.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("it has no [[steps]]")
    default = default_workflow(prompts)
    steps = [_parse_step(i, raw, default) for i, raw in enumerate(raw_steps, 1)]
    raw_roles = definition.get("roles", {})
    if not isinstance(raw_roles, dict):
        raise ValueError("roles must be a table")
    declared = {role: _parse_role(role, raw) for role, raw in raw_roles.items()}
    # The panes follow the steps; a declared role no step uses goes last, for Pipeline to reject.
    order = dict.fromkeys([st["role"] for st in steps] + list(declared))
    labels = {role: declared.get(role, {}).get("label") or _default_label(role) for role in order}
    models = {role: table["model"] for role, table in declared.items() if table.get("model")}
    agents = {role: table["agent"] for role, table in declared.items() if table.get("agent")}
    description = definition.get("description", "")
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    proof_command = definition.get("proof_command")
    if proof_command is not None and not isinstance(proof_command, str):
        raise ValueError("proof_command must be a string")
    budget = {key: definition[key] for key in BUDGET_KEYS if key in definition}
    for key, value in budget.items():
        # type(), not isinstance: true is an int to isinstance, but no number of lines.
        if type(value) is not int:
            raise ValueError(f"{key} must be a whole number")
    return Pipeline(name, labels, tuple(Step(**st) for st in steps), models=models, description=description,
                    agents=agents, proof_command=proof_command, **budget)


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


def _parse_step(i: int, raw, default: Pipeline) -> dict:
    """Step's arguments from step i of a definition, its use resolved against default."""
    if not isinstance(raw, dict):
        raise ValueError(f"step {i} must be a table")
    where = f"step {i}" + (f" ({raw['id']})" if isinstance(raw.get("id"), str) else "")
    _check_keys(raw, STEP_KEYS | {"use"}, where)
    step = _used_step(raw, where, default) | {k: v for k, v in raw.items() if k != "use"}
    step.setdefault("role", step.get("id"))
    for key in ("id", "file", "prompt"):
        if key not in step:
            raise ValueError(f"{where}: {key} is missing")
    for key, value in step.items():
        _check_step_value(key, value, where)
    return step


def _used_step(raw: dict, where: str, default: Pipeline) -> dict:
    """The fields of the step of default, the default workflow, that raw's use names; none without a use."""
    if "use" not in raw:
        return {}
    used = default.step(raw["use"]) if isinstance(raw["use"], str) else None
    if used is None:
        raise ValueError(f"{where}: use names no step of the default workflow; it has "
                         f"{', '.join(st.id for st in default.steps)}")
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
    budget = {f.name: getattr(p, f.name) for f in fields(Pipeline)
              if f.name in BUDGET_KEYS and getattr(p, f.name) != f.default}
    return {**({"description": p.description} if p.description else {}), **budget,
            **({"proof_command": p.proof_command} if p.proof_command else {}),
            "roles": roles, "steps": [step_fields(st) for st in p.steps]}


def workflow_toml(p: Pipeline) -> str:
    """The workflow as a workflow file."""
    d = workflow_definition(p)
    lines = [f"description = {_toml_string(d['description'])}"] if "description" in d else []
    lines += [f"{key} = {d[key]}" for key in BUDGET_KEYS if key in d]
    lines += [f"proof_command = {_toml_string(d['proof_command'])}"] if "proof_command" in d else []
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


def run_pipeline(name: str, definition: dict | None, prompts: Prompts) -> Pipeline:
    """A run's workflow: the definition saved with it, or the built-in workflow of its name worded by the run's
    prompts."""
    if definition is None:
        return find_workflow(name, prompts)
    try:
        # A saved definition is canonical, with no use for prompts to word.
        return parse_workflow(name, definition, prompts)
    except ValueError as e:
        raise OrchestratorError(f"the definition of workflow {name} saved with the run is invalid: {e}") from e


def saved_prompts(overrides: dict[str, str], run_id: str) -> Prompts:
    """The prompts a run started with, from the overrides saved with it (RunState.prompt_overrides); OrchestratorError
    when they name a prompt this orchestrator lacks."""
    if unknown := set(overrides) - {f.name for f in fields(Prompts)}:
        raise OrchestratorError(f"run state {run_id} replaces prompt {min(unknown)}, which this orchestrator lacks")
    return Prompts(**overrides)


def saved_pipeline(saved: dict) -> Pipeline | None:
    """The workflow of a saved state, or None when this orchestrator cannot tell."""
    try:
        return run_pipeline(saved.get("workflow") or DEFAULT_WORKFLOW.name, saved.get("workflow_definition"),
                            saved_prompts(saved.get("prompt_overrides") or {}, saved.get("run_id", "?")))
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
    """The harness's arguments for a --permission-mode, as a new list the caller may extend, or None when it has
    no equivalent."""
    if kind == "claude":
        return ["--permission-mode", mode]
    args = PERMISSION_ARGS[kind].get(mode)
    return None if args is None else list(args)


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
# Runs a command in a directory: gh, which finds the repository from its working directory and has no -C, and
# gitleaks, which reads an `[extend] path` from its working directory.
IN_DIR = 'cd -- "$1" && shift && exec "$@"'


def _top_literal(paths: list[str]) -> list[str]:
    """git pathspecs that each match exactly one of paths, relative to the repository's top level, whatever directory
    git runs in, and with no glob in them."""
    return [f":(top,literal){p}" for p in paths]


def _shortened(text: str, n: int = 160) -> str:
    """text, or its first n - 1 characters and an ellipsis when it is longer than n."""
    return text if len(text) <= n else text[:n - 1] + "…"


class GhUnusable(OrchestratorError):
    """gh is missing or logged out where the project is, so no gh call can succeed."""


class ChangedFile(NamedTuple):
    """A file a change touches, as Host.changed_line_counts gives it."""
    base_path: str  # its path at the base: path, unless git paired the file with another as a rename
    path: str  # its path in the change; for a file the change adds or deletes, the side without it has no file there
    added: int  # lines; 0 for a binary file
    removed: int


class RunFile(NamedTuple):
    """A file in a run directory, as Host.run_files lists it."""
    path: str  # the run directory's path, a slash and the file's name
    mtime: int  # when it was last modified, in seconds since the epoch by its host's clock


# How long, in seconds, an SSH master connection (ssh_sharing_options) outlives the last command that used it.
SSH_CONTROL_PERSIST_SECONDS = 60
# How often, in seconds, an SSH master asks its server whether the connection is alive, and how many asks left
# unanswered end it: so one that died without a reset ends within 30 s, and the next command opens a new one,
# rather than waiting on it until TCP gives up, while the commands keep it from ever idling out.
SSH_ALIVE_INTERVAL_SECONDS = 10
SSH_ALIVE_COUNT = 3
# The longest control socket path ssh can bind, in bytes: a Unix socket's path is at most 103 on macOS (sun_path
# holds 104 with its NUL; 108 on Linux), and ssh binds a temporary name 17 bytes longer before renaming it.
_SSH_SOCKET_PATH_MAX = 103 - 17
# The name of a control directory: this prefix, then the 8 characters mkdtemp adds.
_SSH_DIR_PREFIX = "octoflow-ssh-"


class _SshControl:
    """This process's SSH control sockets, for ssh_sharing_options."""

    def __init__(self) -> None:
        self.dir: str | None = None  # the directory, made by this process, they are in; None while there is none
        self.options: list[str] = []  # ssh_sharing_options' answer while dir is that directory


_SSH_CONTROL = _SshControl()


def ssh_sharing_options() -> list[str]:
    """The ssh options that let this process's commands to one target share a single connection (ssh_config(5),
    ControlMaster), which ends SSH_CONTROL_PERSIST_SECONDS after its last command, or once it stops answering
    (SSH_ALIVE_INTERVAL_SECONDS). Its socket is in a directory only the user can use, under $XDG_RUNTIME_DIR, else
    /tmp, made on the first call, made again when it is gone or no longer the user's alone, and removed when the
    process exits.

    An empty list when no such directory with a socket path ssh accepts can be made: ssh would fail the command,
    not connect on its own (ssh_config(5), ControlPath), so the command runs unshared."""
    control = _SSH_CONTROL
    if control.dir is None or not _private_dir(control.dir):
        control.dir, control.options = None, []
        # /tmp rather than tempfile's default, whose path on macOS leaves no room for the socket's; mkdtemp's
        # directory there is private and unguessable, and _private_dir sees that it stays so.
        for parent in (os.environ.get("XDG_RUNTIME_DIR"), "/tmp"):  # noqa: S108
            if made := _new_ssh_control_dir(parent):
                control.dir = made
                control.options = ["-o", "ControlMaster=auto", "-o", f"ControlPersist={SSH_CONTROL_PERSIST_SECONDS}",
                                   "-o", f"ControlPath={made}/%C",
                                   "-o", f"ServerAliveInterval={SSH_ALIVE_INTERVAL_SECONDS}",
                                   "-o", f"ServerAliveCountMax={SSH_ALIVE_COUNT}"]
                break
    return control.options


def _new_ssh_control_dir(parent: str | None) -> str | None:
    """A new directory for SSH control sockets in parent, only the user's, its removal at exit registered; None when
    parent is unset, would give a socket path ssh rejects, too long or with a character its option parser takes
    apart, or the directory cannot be made."""
    if not parent or any(c.isspace() or c in "%\"'\\" for c in parent):
        return None
    # The socket's path: parent, the directory's name, and %C, the hash of the connection's endpoints, 40 hex digits.
    if len(os.fsencode(parent)) + 1 + len(_SSH_DIR_PREFIX) + 8 + 1 + 40 > _SSH_SOCKET_PATH_MAX:
        return None
    try:
        made = tempfile.mkdtemp(prefix=_SSH_DIR_PREFIX, dir=parent)
    except OSError:
        return None
    atexit.register(shutil.rmtree, made, True)
    return made


def _private_dir(path: str) -> bool:
    """Whether path is still a directory, not a symlink, that is the user's and no one else's (mode 0700): one this
    process made may have been removed, and in /tmp another user, who sees its name in ps, may make one so named."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700


class Host:
    """Runs commands on the machine where the agents run: locally, or over SSH, one shared connection per target
    (ssh_sharing_options)."""

    def __init__(self, ssh_target: str | None = None, run=subprocess.run):
        self.ssh_target = ssh_target
        self._run = run

    def over_ssh(self, ssh_target: str) -> "Host":
        """A Host that runs its commands on ssh_target over SSH, starting them as this one does."""
        return Host(ssh_target, self._run)

    def run(self, argv: list[str], stdin: str | None = None, timeout: float = 60, *,
            verbatim: bool = False) -> subprocess.CompletedProcess:
        """argv's exit status, standard output and standard error, as text, run here or over SSH on ssh_target.

        stdin, when given, is its standard input. The output is decoded strictly in the locale's encoding, each
        line end made "\n", which suits output that is parsed, such as JSON or a state file. verbatim keeps it as
        the command printed it, for output that carries paths or file contents: line ends stay as they are, and
        bytes the file system's encoding cannot decode become lone surrogates, as os.fsdecode makes them, so a
        path in it opens the file it names. Raises OrchestratorError when the command cannot be started, runs
        longer than timeout seconds, or, without verbatim, prints what the locale's encoding cannot decode.
        """
        if self.ssh_target:
            # BatchMode fails fast instead of hanging on a password prompt nobody can see.
            argv = ["ssh", "-o", "BatchMode=yes", *ssh_sharing_options(), self.ssh_target, shlex.join(argv)]
        try:
            if not verbatim:
                return self._run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, env=child_env())
            # Bytes, decoded here: text mode cannot leave line ends alone.
            proc = self._run(argv, input=None if stdin is None else os.fsencode(stdin), capture_output=True,
                             timeout=timeout, env=child_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OrchestratorError(f"{shlex.join(argv)}: {e}") from e
        except UnicodeDecodeError as e:
            raise OrchestratorError(f"{shlex.join(argv)}: printed what is not {e.encoding.upper()}: {e}") from e
        return subprocess.CompletedProcess(proc.args, proc.returncode, os.fsdecode(proc.stdout),
                                           os.fsdecode(proc.stderr))

    def check(self, argv: list[str], stdin: str | None = None, timeout: float = 60) -> str:
        return self.checked(argv, self.run(argv, stdin, timeout))

    def checked(self, argv: list[str], proc: subprocess.CompletedProcess) -> str:
        """proc's standard output, proc being argv run on this host; raises OrchestratorError naming argv, the SSH
        target if any, and its standard error, when it exited with a status other than 0."""
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

    # now and mtime read this host's clock, the one that stamps the handoff files, so a turn's start and end
    # come from one clock and skew between the machines involved cannot distort its duration.
    def now(self) -> int:
        """The time on this host, in seconds since the epoch."""
        return self._seconds(self.check(["date", "+%s"]), "date +%s")

    def mtime(self, path: str) -> int:
        """When the file was last modified, in seconds since the epoch."""
        # stat -c is GNU, stat -f is BSD.
        script = 'stat -c %Y -- "$1" 2>/dev/null || stat -f %m -- "$1"'
        return self._seconds(self.check(["sh", "-c", script, "_", path]), f"stat of {path}")

    @staticmethod
    def _seconds(out: str, what: str) -> int:
        try:
            return int(out.strip())
        except ValueError as e:
            raise OrchestratorError(f"{what} printed {out.strip()!r}, not a time") from e

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

    def git(self, cwd: str, *args: str, timeout: float = 60, verbatim: bool = False) -> str:
        """The standard output of git args in cwd; timeout and verbatim as for run. Raises OrchestratorError when
        git exits with a status other than 0."""
        return self.checked(["git", "-C", cwd, *args], self.git_run(cwd, *args, timeout=timeout, verbatim=verbatim))

    def git_run(self, cwd: str, *args: str, timeout: float = 60,
                verbatim: bool = False) -> subprocess.CompletedProcess:
        """A git command whose failure the caller interprets; git uses exit status 1 for answers such as "no".
        timeout and verbatim are as for run."""
        return self.run(["git", "-C", cwd, *args], timeout=timeout, verbatim=verbatim)

    def _git_test(self, cwd: str, *args: str) -> bool:
        """Whether a yes-or-no git command answers yes: status 0 is yes, 1 is no, anything else an error."""
        proc = self.git_run(cwd, *args)
        if proc.returncode not in (0, 1):
            self.checked(["git", "-C", cwd, *args], proc)
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

    # Locked files (Step.locks). Their paths are relative to the repository's top level, verbatim as Host.run says,
    # whatever directory of the repository cwd is.

    def lock(self, cwd: str, base: str, ref: str) -> list[str]:
        """Snapshot the working tree on top of base, store the commit under ref, replacing what ref held, and return
        the paths whose files the snapshot changes from base: added, edited or deleted, a rename as its two paths.

        ref is a local ref, which keeps the commit from git gc; nothing pushes it.
        """
        sha = self.snapshot(cwd, base, f"locked files, {ref}")
        self.git(cwd, "update-ref", ref, sha)
        return self._paths_between(cwd, base, sha)

    def locked_changes(self, cwd: str, ref: str, paths: list[str]) -> tuple[list[str], str]:
        """Of paths, the ones whose file in the working tree, untracked files included, differs from ref's, a file
        missing on one side counting as different; and the diff from ref to the working tree over them, as text
        for a reader. ([], "") when none differs. Reads only: ref, the tree and the index stay as they are."""
        if not paths:
            return [], ""
        sha = self.snapshot(cwd, ref, f"the working tree against {ref}")
        differ = self._paths_between(cwd, ref, sha, paths)
        if not differ:
            return [], ""
        return differ, self.git(cwd, "diff", "--no-color", "--no-ext-diff", "--no-renames", ref, sha, "--",
                                *_top_literal(differ), verbatim=True)

    def restore(self, cwd: str, ref: str, paths: list[str]) -> None:
        """Make each of paths in the working tree what it is in ref: written as ref has it, or deleted where ref has
        none. The index stays as it is."""
        held = set(self.git(cwd, "ls-tree", "-r", "--name-only", "-z", "--full-tree", ref, "--", *_top_literal(paths),
                            verbatim=True).split("\0")) if paths else set()
        if kept := [p for p in paths if p in held]:
            self.git(cwd, "restore", f"--source={ref}", "--worktree", "--", *_top_literal(kept))
        # git restore refuses a path that neither ref nor the index has, so a file ref lacks is removed here.
        if gone := [p for p in paths if p not in held]:
            top = self.git(cwd, "rev-parse", "--show-toplevel", verbatim=True).rstrip("\n")
            self.check(["rm", "-f", "--", *(f"{top}/{p}" for p in gone)])

    def delete_refs(self, cwd: str, prefix: str) -> None:
        """Delete every local ref whose name starts with prefix, such as refs/orchestrator/<key>/; none is no error."""
        for ref in self.git(cwd, "for-each-ref", "--format=%(refname)", prefix).split():
            self.git(cwd, "update-ref", "-d", ref)

    def _paths_between(self, cwd: str, a: str, b: str, paths: list[str] | None = None) -> list[str]:
        """The paths whose files differ between commits a and b, a rename as its two paths; with paths, only those."""
        names = self.git(cwd, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", a, b, "--",
                         *_top_literal(paths or []), verbatim=True)
        return [p for p in names.split("\0") if p]

    def change_diff(self, cwd: str, base: str, commit: str | None = None) -> str:
        """`git diff -U0` from base to commit, or to the working tree's tracked files without one, in the one shape
        added_lines parses, whatever the user's git config.

        It is verbatim, as Host.run says: a line that is not UTF-8, such as Latin-1 text or a binary file that git
        diffs as text, is kept rather than refused, so a change to such a file is judged like any other, and only
        git's "\n" ends a line, so nothing inside an added line can pass for a header.
        """
        return self.git(cwd, "-c", "core.quotePath=false", "diff", "-U0", "-M", "--no-color", "--no-ext-diff",
                        "--src-prefix=a/", "--dst-prefix=b/", base, *([commit] if commit else []), "--", verbatim=True)

    def file_at(self, cwd: str, commit: str, path: str) -> str | None:
        """The file at path, relative to the repository's top level and taken literally, as commit has it, verbatim
        as Host.run says, so os.fsencode gives back the bytes git stores; None where commit has no file there. A
        symbolic link gives the path it points to, as git stores it. Raises OrchestratorError when git cannot read
        commit, or cwd is not in a git repository."""
        found = self._blob_at(cwd, commit, path)
        return None if found is None else found[0]

    def _blob_at(self, cwd: str, commit: str, path: str) -> tuple[str, bool] | None:
        """What file_at gives for path, and whether commit has a symbolic link there, whose text is then the path it
        points to; None where file_at gives None. Raises as file_at."""
        # Each entry is `<mode> <type> <object>\t<path>`; a directory's type is tree, a symbolic link's mode 120000.
        # A path ending in "/", or none, lists a directory's entries instead, so only an entry at path itself counts.
        entry = self.git(cwd, "ls-tree", "--full-tree", "-z", commit, "--", *_top_literal([path]),
                         verbatim=True).split("\0")[0]
        fields, _, listed = entry.partition("\t")
        mode, kind, blob = (fields.split(" ") + ["", "", ""])[:3]
        if kind != "blob" or listed != path:
            return None
        return self.git(cwd, "cat-file", "blob", blob, verbatim=True), mode == "120000"

    def _base_rules_file(self, cwd: str, base: str, name: str) -> str | None:
        """base's file at name, one the secret scan takes its rules from, as file_at gives it; None where base has
        none. Raises OrchestratorError, failing closed as _base_rules_refused says, when base has a symbolic link
        there, whose target this does not follow, and as file_at."""
        found = self._blob_at(cwd, base, name)
        if found and found[1]:
            raise _base_rules_refused(base, f"{name} is a symbolic link in base, and only a file is read there")
        return found[0] if found else None

    def changed_line_counts(self, cwd: str, base: str, commit: str) -> list[ChangedFile]:
        """Each file the change from base to commit touches, a rename paired as change_diff pairs it, with the lines it
        adds and removes, from `git diff --numstat`. The paths are verbatim, as Host.run says."""
        numstat = self.git(cwd, "diff", "--numstat", "-z", "-M", "--no-ext-diff", base, commit,
                           verbatim=True).split("\0")
        out = []
        i = 0
        # -z gives each entry as `added<TAB>removed<TAB>path`, or for a rename `added<TAB>removed<TAB>` and then the
        # old and new paths as fields of their own; a binary file counts "-". The list ends with an empty field.
        while i < len(numstat) and numstat[i]:
            added, removed, path = numstat[i].split("\t", 2)
            if path:
                base_path, i = path, i + 1
            else:
                base_path, path, i = numstat[i + 1], numstat[i + 2], i + 3
            out.append(ChangedFile(base_path, path, 0 if added == "-" else int(added),
                                   0 if removed == "-" else int(removed)))
        return out

    def push_ref(self, cwd: str, commit: str, branch: str, *, base: str, report_path: str) -> None:
        """Push commit to origin's branch, replacing what is there, once scan_unpublished finds no secret in what it
        would publish; base and report_path are as scan_unpublished takes them. Raises LeaksFound with what it found,
        or OrchestratorError when the scan cannot judge or the push fails; nothing is pushed then."""
        self.refuse_leaks(cwd, commit, base=base, report_path=report_path)
        # --force replaces what a failed attempt left under the same name.
        self.git(cwd, "push", "--quiet", "--force", REMOTE, f"{commit}:refs/heads/{branch}", timeout=NETWORK_TIMEOUT)

    def push_branch(self, cwd: str, branch: str, *, base: str, report_path: str) -> None:
        """Push the local branch to origin's branch of that name and track it, once scan_unpublished finds no secret
        in what its commit would publish; base and report_path are as scan_unpublished takes them. Raises as
        push_ref."""
        commit = self.git(cwd, "rev-parse", "--verify", f"refs/heads/{branch}").strip()
        self.refuse_leaks(cwd, commit, base=base, report_path=report_path)
        self.git(cwd, "push", "--quiet", "--set-upstream", REMOTE, branch, timeout=NETWORK_TIMEOUT)

    def gitleaks_problem(self) -> str | None:
        """Why gitleaks with its `git` subcommand (8.19 and later), which scan_unpublished runs, cannot run here, or
        None when it can. Its help, not its version, is asked: a distribution's build may print no version number."""
        try:
            proc = self.run(["gitleaks", "git", "--help"])
        except OrchestratorError as e:
            return f"gitleaks cannot be started ({e})"
        return None if proc.returncode == 0 else "`gitleaks git` does not run"

    def check_gitleaks(self) -> None:
        """Raise OrchestratorError, saying how to install it, when gitleaks_problem finds a problem."""
        problem = self.gitleaks_problem()
        if problem:
            raise OrchestratorError(f"this run pushes, and every push is scanned for secrets first, but {problem}: "
                                    f"{GITLEAKS_INSTALL}")

    def refuse_leaks(self, cwd: str, commit: str, *, base: str, report_path: str) -> None:
        """Raise LeaksFound when scan_unpublished finds any in what a push of commit would publish, and
        OrchestratorError when it cannot judge, as scan_unpublished says, which also says what it writes beside
        report_path."""
        leaks = self.scan_unpublished(cwd, commit, base, report_path)
        if leaks:
            raise LeaksFound(leaks, report_path)

    def _write_base_rules(self, cwd: str, base: str, rules: str) -> str:
        """Make the directory rules hold base's gitleaks rules, for a gitleaks run with rules as its working
        directory, and return the path to give it as --config, in rules: base's .gitleaks.toml, or gitleaks' defaults
        where base has none. Each file it extends with `[extend] path`, and each one that file extends in turn, as
        far as GITLEAKS_EXTEND_DEPTH says gitleaks reads, is written from base at that path under rules: gitleaks
        reads such a path from its working directory, and base's authors write it from the repository's top level, so
        every file gitleaks reads is base's. Whatever rules held before is removed.

        Raises OrchestratorError, failing closed and naming the file, when a file in the chain is refused by
        _gitleaks_extended_file or _base_rules_file, or extends what is no file in base or a file already in the
        chain; and when git cannot read base."""
        self.check(["rm", "-rf", "--", rules])
        chain = [".gitleaks.toml"]

        def refused(why: str) -> OrchestratorError:
            return _base_rules_refused(base, f"{chain[-1]} {why}")
        config, spelled = f"{rules}/{chain[0]}", chain[0]
        text = self._base_rules_file(cwd, base, chain[0])
        if text is None:
            text = "[extend]\nuseDefault = true\n"
        while True:
            self.write(f"{rules}/{spelled}", text)
            if len(chain) > GITLEAKS_EXTEND_DEPTH:
                return config
            try:
                extended = _gitleaks_extended_file(text)
            except ValueError as e:
                raise refused(str(e)) from e
            if extended is None:
                return config
            # gitleaks opens the path as spelled, and the copy is written so; every directory on the way is one this
            # makes, so the path reaches that copy, which in base is at the path's normal form.
            name = posixpath.normpath(extended)
            if name in chain:
                raise refused(f"extends {extended!r}, which comes back round: {' -> '.join([*chain, name])}")
            text = self._base_rules_file(cwd, base, name)
            if text is None:
                raise refused(f"extends {extended!r}, which is no file in base")
            chain.append(name)
            spelled = extended

    def scan_unpublished(self, cwd: str, commit: str, base: str, report_path: str) -> list[Leak]:
        """The secrets gitleaks finds, and the `gitleaks:allow` comments added, in the commits a push of commit would
        publish: those reachable from it and from no origin ref, as git log's `<commit> --not --remotes=origin`
        gives them. A merge's own content is what it adds beyond git's automatic merge of its parents (git's
        remerge diff): a conflict's resolution or an edit made in the merge counts, what a parent already had does
        not. git gives no remerge diff for a merge of three or more parents, so a range holding one is refused. A
        remote-tracking ref for a branch origin has since deleted or rewritten narrows the range: what only it reaches
        was public once, and is not scanned again.

        The rules come from base, a commit before the change: its .gitleaks.toml (else gitleaks' defaults), each file
        it extends with `[extend] path`, in turn, and its .gitleaksignore (else none), and nothing the change holds, in
        the working tree or committed, so that the change cannot allowlist itself. Every file git takes for text by its
        content is diffed as text, with no textconv, whatever any .gitattributes says, as _scan_git_env sets git up;
        one whose first 8000 bytes hold a NUL is binary to git and scanned by neither gitleaks nor the comment check
        (see TODO.md);
        gitleaks honours a `gitleaks:allow` comment silently, so each one an added line holds is a Leak of its own.
        gitleaks runs with --redact and writes its JSON report to report_path, a path in the run's directory, beside
        the .gitleaksignore copy, the rules directory _write_base_rules fills and the empty directory this makes there.

        Raises OrchestratorError, failing closed, when git cannot read base or the range, base's rules cannot be taken
        whole from base as _write_base_rules says or its .gitleaksignore is a symbolic link, the range holds an
        octopus merge, or gitleaks cannot be started, exits other than 0 or 1, writes no report or one that is not a
        list of findings, or exits 1 with an empty one: gitleaks also exits 1 when it cannot load its config, writing
        nothing. git runs where cwd is, as every Host
        command does, and gitleaks in the directory of rules."""
        directory = report_path.rsplit("/", 1)[0]
        rules, ignore, source = (f"{directory}/gitleaks{name}" for name in ("-rules", "ignore", "-source"))
        config = self._write_base_rules(cwd, base, rules)
        self.write(ignore, self._base_rules_file(cwd, base, ".gitleaksignore") or "")
        # gitleaks also reads <source>/.gitleaksignore, which in the working tree the change could write: it scans
        # from an empty directory, the repository named by GIT_DIR. And a report an earlier scan left would be read
        # as this one's.
        self.check(["sh", "-c", 'rm -rf -- "$1" "$2" && mkdir -p -- "$1"', "_", source, report_path])
        git_dir = self.git(cwd, "rev-parse", "--absolute-git-dir").strip()
        where = f" on {self.ssh_target}" if self.ssh_target else ""
        failed = f"gitleaks could not scan what the push would publish{where}, so nothing was pushed"
        octopus = self.git(cwd, "rev-list", "--min-parents=3", commit, "--not", f"--remotes={REMOTE}",
                           timeout=GITLEAKS_TIMEOUT).split()
        if octopus:
            raise OrchestratorError(f"{failed}: merge {octopus[0][:12]} has three or more parents, and git shows no "
                                    f"diff of what such a merge adds; merge its branches one at a time")
        log_opts = f"--diff-merges=remerge --no-textconv {commit} --not --remotes={REMOTE}"
        git_env = _scan_git_env(git_dir, self.git(cwd, "hash-object", "-t", "tree", os.devnull).strip())
        proc = self.run(["sh", "-c", IN_DIR, "_", rules,
                         *git_env, "gitleaks", "git", f"--log-opts={log_opts}", "--redact",
                         "--no-banner", "--no-color", "--exit-code", "1", "--report-format", "json", "--report-path",
                         report_path, "--config", config, "--gitleaks-ignore-path", ignore, source],
                        timeout=GITLEAKS_TIMEOUT)
        said = _shortened(proc.stderr.strip().rsplit("\n", 1)[-1] or "nothing")
        if proc.returncode not in (0, 1):
            raise OrchestratorError(f"{failed}: it exited with status {proc.returncode}: {said}")
        text = self.read(report_path)
        try:
            findings = json.loads(text) if text is not None else None
            leaks = [Leak(f["RuleID"], f["File"], int(f["StartLine"]), f["Commit"], f["Fingerprint"])
                     for f in findings] if isinstance(findings, list) else None
        except (ValueError, TypeError, KeyError) as e:
            raise OrchestratorError(f"{failed}: its report {report_path} cannot be read: {e}") from e
        if leaks is None or (proc.returncode == 1 and not leaks):
            raise OrchestratorError(f"{failed}: it exited with status {proc.returncode} and wrote "
                                    f"{'no report' if text is None else 'no finding'}: {said}")
        return leaks + self._allow_comments(git_env, source, log_opts)

    def _allow_comments(self, git_env: list[str], source: str, log_opts: str) -> list[Leak]:
        """A Leak for each line holding `gitleaks:allow` that the commits log_opts selects add, as git log -p shows
        them with log_opts, read as change_diff's diffs are. git runs as gitleaks does, in source under git_env, as
        _scan_git_env gives it, so both read the same patch."""
        argv = [*git_env, "git", "-C", source, "-c", "core.quotePath=false", "log", "-p", "-U0", "--no-color",
                "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", "--format=%x00%H", *log_opts.split()]
        out = self.checked(argv, self.run(argv, timeout=GITLEAKS_TIMEOUT, verbatim=True))
        leaks = []
        # Each commit's patch follows its sha on a line of its own that starts with a NUL; a NUL inside a line of a
        # patch does not start one.
        for block in ("\n" + out).split("\n\x00")[1:]:
            commit, _, patch = block.partition("\n")
            leaks += [Leak("gitleaks-allow", path, line, commit, f"{commit}:{path}:gitleaks-allow:{line}")
                      for path, lines in added_lines(patch).items() for line, text in lines.items()
                      if GITLEAKS_ALLOW in text]
        return leaks

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
        return self.checked(["git", "-C", cwd, *argv], proc).strip()

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

    def remove_worktree(self, cwd: str, path: str, force: bool = False) -> None:
        """Remove the worktree at path; without force, git refuses one with changes it would lose."""
        self.git(cwd, "worktree", "remove", *(["--force"] if force else []), path)

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
        return self._pr_view(cwd, url, "state,headRefOid,closedAt", lambda pr: (
            isinstance(pr["state"], str) and re.fullmatch(r"[0-9a-f]{40,64}", pr["headRefOid"])
            and (pr["closedAt"] is None or isinstance(pr["closedAt"], str))))

    def pull_request_merge(self, cwd: str, url: str) -> dict:
        """The pull request's state (OPEN, CLOSED or MERGED), isDraft, mergeable (MERGEABLE, CONFLICTING, or UNKNOWN
        while GitHub has not computed it) and body, as gh reports them. Raises as pull_request does."""
        return self._pr_view(cwd, url, "state,isDraft,mergeable,body", lambda pr: (
            isinstance(pr["state"], str) and isinstance(pr["isDraft"], bool)
            and pr["mergeable"] in ("MERGEABLE", "CONFLICTING", "UNKNOWN") and isinstance(pr["body"], str)))

    def _pr_view(self, cwd: str, url: str, json_fields: str, valid: Callable[[dict], object]) -> dict:
        """gh pr view's JSON object of json_fields, comma-separated, for the pull request at url, once valid finds it
        truthy. Raises GhUnusable when gh is missing or logged out, and OrchestratorError when it fails otherwise or
        prints what valid rejects, or raises KeyError or TypeError for."""
        args = ["pr", "view", url, "--json", json_fields]
        proc = self.gh_run(cwd, *args)
        if proc.returncode in GH_UNUSABLE_STATUS:
            raise GhUnusable(f"gh {shlex.join(args)} failed with status {proc.returncode}: "
                             f"{(proc.stderr or proc.stdout).strip()}")
        out = self.checked(["gh", *args], proc)
        try:
            pr = json.loads(out)
            fields_ok = valid(pr)
        except (ValueError, KeyError, TypeError) as e:
            raise OrchestratorError(f"gh pr view {url} printed {out.strip()!r}: {e}") from e
        if not fields_ok:
            raise OrchestratorError(f"gh pr view {url} printed {out.strip()!r}, not a pull request's state")
        return pr

    def edit_pr_body(self, cwd: str, url: str, body: str) -> None:
        """Make body the description of the pull request at url, with gh."""
        argv = ["gh", "pr", "edit", url, "--body-file", "-"]
        self.check(["sh", "-c", IN_DIR, "_", cwd, *argv], stdin=body, timeout=NETWORK_TIMEOUT)

    def mark_pr_ready(self, cwd: str, url: str) -> None:
        """Take the pull request at url out of draft, with gh."""
        self.check(["sh", "-c", IN_DIR, "_", cwd, "gh", "pr", "ready", url], timeout=NETWORK_TIMEOUT)

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

    def session_log(self, kind: str, session: str) -> str | None:
        """The log in which the harness kind keeps a session's token usage (SESSION_LOGS), found in the home directory
        here; None for a harness without one, a session id that is not a plain name, or no such file. Verbatim, as
        Host.run says."""
        pattern = SESSION_LOGS.get(kind)
        if pattern is None or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
            return None
        script = (f'cd -- "$HOME" || exit 1; for f in $1; do [ -f "$f" ] && exec cat -- "$f"; done; '
                  f'exit {MISSING_FILE_STATUS}')
        proc = self.run(["sh", "-c", script, "_", pattern.format(session=session)], verbatim=True)
        if proc.returncode == MISSING_FILE_STATUS:
            return None
        return self.checked(["sh", "-c", script], proc)

    def run_metrics(self, cwd: str) -> list[dict]:
        """The metrics.json of each run of the project at cwd that has one, oldest run first."""
        # Each file is one line; echo ends one that lacks its line end.
        script = (f'for f in "$1"/{RUNS_DIR}/*/{METRICS_FILE}; do [ -f "$f" ] || continue; cat -- "$f" && echo; done; '
                  'true')
        records = []
        for line in self.check(["sh", "-c", script, "_", cwd]).splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError as e:
                raise OrchestratorError(f"corrupt {METRICS_FILE} under {cwd}/{RUNS_DIR}: {e}") from e
        return records

    def run_files(self, run_dir: str) -> list[RunFile]:
        """The files in a run directory, oldest first, except state.json and write's temp files."""
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
        return [RunFile(path, mtime) for mtime, path in sorted(files)]


# ---------------------------------------------------------------------------
# CI: Jenkins, for the quality gate
# ---------------------------------------------------------------------------

class CIError(OrchestratorError):
    """A request to Jenkins that failed; status is the HTTP status, None when no answer came."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        # No answer, or the server's own trouble, may pass; a 4xx answer will not change by asking again.
        return self.status is None or self.status >= 500


@dataclass(frozen=True)
class QualityBuild:
    """A build of the quality job, as Jenkins lists it."""
    number: int
    result: str | None  # such as SUCCESS, UNSTABLE, FAILURE or ABORTED; None while it runs
    description: str | None  # as the build set it, verbatim; None when it set none


class CI:
    """Jenkins's quality job, over HTTP from the orchestrator's own machine.

    The credentials come from the orchestrator's environment or ci_env_path() and travel only in the
    Authorization header, never in a URL, so no error, log or state.json can carry them. Errors name the
    method, the URL and the status, and text from Jenkins goes through mask before it is shown or saved.
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
            raise CIError(self.mask(f"{method} {url}: HTTP {e.code}"), e.code) from e
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

    def preflight(self) -> None:
        """Fail before the interview if the gate could not run: the credentials, Jenkins and the job's parameters."""
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

    def trigger(self, ref: str) -> str:
        """Queue a build of the branch ref, which runs the gate on it; returns the queue item's URL."""
        url = f"{self.job_url}buildWithParameters"
        params = {"GIT_REF": ref}
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

    def retained_builds(self) -> list[QualityBuild]:
        """The job's builds that Jenkins still keeps, newest first, the newest 100 at most.

        Raises OrchestratorError, before asking, when credentials are missing; CIError when Jenkins gives no answer,
        an HTTP error, or an answer that is not a list of builds.
        """
        url = f"{self.job_url}api/json?{urllib.parse.urlencode({'tree': 'builds[number,result,description]{0,100}'})}"
        builds = self._jenkins_json(url).get("builds")
        # type() rather than isinstance: a JSON true is a bool, which is an int.
        if not isinstance(builds, list) or not all(
                isinstance(b, dict) and type(b.get("number")) is int and isinstance(b.get("result"), str | None)
                and isinstance(b.get("description"), str | None) for b in builds):
            raise CIError(self.mask(f"GET {url}: the answer is not the job's builds"))
        return [QualityBuild(b["number"], b.get("result"), b.get("description")) for b in builds]

    def build_result(self, number: int) -> str | None:
        """The finished build's result, such as SUCCESS, UNSTABLE, FAILURE or ABORTED; None while it runs."""
        build = self._jenkins_json(self._build_url(number, "api/json", "building,result"))
        return None if build.get("building") else build.get("result")

    def quality_file(self, number: int) -> str | None:
        """The quality file the build's Gate stage archived, quality.md; None when the build archived none."""
        try:
            body, _ = self._jenkins("GET", self._build_url(number, "artifact/quality.md"))
        except CIError as e:
            if e.status == 404:
                return None
            raise
        return body.decode(errors="replace")

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


class PyPIError(OrchestratorError):
    """PyPI gave no answer the orchestrator could use: none at all, an HTTP error other than 404, or something that
    is not its JSON."""


@dataclass(frozen=True)
class PyPIProject:
    """What PyPI knows of a project."""
    first_release: datetime | None  # the upload of its earliest file, in UTC; None when no release has a file
    version: str  # its latest version
    latest_release: datetime | None  # the upload of that version's earliest file, in UTC; None when it has none
    # Its license expression, else the first line of its license field, else its license classifier's last part;
    # None when it states none.
    license: str | None


class PyPI:
    """The Python Package Index's JSON API (https://docs.pypi.org/api/json/), over HTTPS from the orchestrator's own
    machine, through urlopen, which the tests replace."""

    def __init__(self, urlopen=None):
        self._urlopen = urlopen or urllib.request.urlopen

    def project(self, name: str) -> PyPIProject | None:
        """What PyPI knows of the project name, None when it has no such project (HTTP 404). Raises PyPIError, naming
        the URL, when it gives no usable answer within PYPI_TIMEOUT seconds."""
        url = f"https://pypi.org/pypi/{urllib.parse.quote(name, safe='')}/json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with self._urlopen(req, timeout=PYPI_TIMEOUT) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise PyPIError(f"GET {url}: HTTP {e.code}") from e
        # URLError, a timeout and a dropped connection are all OSErrors.
        except (OSError, http.client.HTTPException) as e:
            reason = e.reason if isinstance(e, urllib.error.URLError) else e
            raise PyPIError(f"GET {url}: {reason}") from e
        try:
            return _pypi_project(json.loads(body))
        # JSON of another shape raises any of these; JSONDecodeError and UnicodeDecodeError are ValueErrors.
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            raise PyPIError(f"GET {url}: the answer is not PyPI's JSON: {e}") from e


def _pypi_project(answer) -> PyPIProject:
    """The project of PyPI's JSON answer, parsed; ValueError, TypeError, AttributeError or KeyError for JSON of
    another shape."""
    info = answer["info"] if isinstance(answer, dict) else None
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise TypeError("it has no info.version")
    first = _first_upload(f for files in (answer.get("releases") or {}).values() for f in files or [])
    return PyPIProject(first, info["version"], _first_upload(answer.get("urls") or []), _license(info))


def _first_upload(files: Iterable[dict]) -> datetime | None:
    """The earliest upload_time_iso_8601 of PyPI's files, None for none."""
    times = [datetime.fromisoformat(f["upload_time_iso_8601"]) for f in files]
    return min(times, default=None)


def _license(info: dict) -> str | None:
    if expression := (info.get("license_expression") or "").strip():
        return expression
    if text := (info.get("license") or "").strip():
        return _shortened(text.splitlines()[0], 80)
    classifiers = [c for c in info.get("classifiers") or [] if c.startswith("License :: ")]
    return classifiers[0].rsplit(" :: ", 1)[1] if classifiers else None


# The files that decide how a change is judged: the stages, the gate, its rules, the versions of its tools and the
# Python they run on. judged_in_quality trusts a quality build only when none of them differs.
JUDGING_FILES = ("Jenkinsfile", "gate.py", "orchestrator.py", "pyproject.toml", "uv.lock", ".python-version")
# A quality build's description, in the format the comment above the Jenkinsfile's Checkout stage defines; a sha is
# SHA-1's 40 hex digits or SHA-256's 64.
_SHA = r"[0-9a-f]{40}(?:[0-9a-f]{24})?"
JUDGED_DESCRIPTION = re.compile(rf"judged tree=(?P<tree>{_SHA}) parent=(?P<parent>{_SHA}) main=(?P<main>{_SHA})")


def judged_in_quality_command(job: str, target: str) -> int:
    """Say whether a build of the Jenkins quality job job already judged the current directory's HEAD, as
    judged_in_quality decides it for a pull request into the branch target.

    Returns 0, naming the build on standard output, when one did. Otherwise returns EXIT_ERROR, with one line on
    standard error saying why: the condition the closest build fails, or what stopped the question being answered.
    No error returns 0.
    """
    try:
        number = judged_in_quality(Host(), os.getcwd(), CI(job), target)
    # OSError: such as os.getcwd's, for a working directory that is gone.
    except (OrchestratorError, OSError) as e:
        print(f"judged-in-quality: {' '.join(str(e).splitlines())}", file=sys.stderr)
        return EXIT_ERROR
    print(f"judged-in-quality: build #{number} of {job} judged HEAD's tree from its merge base with {target}")
    return 0


def judged_in_quality(host: Host, root: str, ci: CI, target: str) -> int:
    """The number of a build of ci's job that judged the HEAD of the checkout at root on host as a pull request's
    build into the branch target would, so that build need not judge it again.

    Such a build is among ci.retained_builds and meets each of these conditions, in this order: its description is
    in JUDGED_DESCRIPTION's format; it names HEAD's tree; it names as parent the merge base of HEAD and REMOTE's
    target; it ended SUCCESS; and each of JUDGING_FILES is the same, or absent alike, at the main commit it names as
    in HEAD and at that merge base.

    Fetches target from REMOTE into refs/remotes/REMOTE/target, and a build's main commit when root lacks it.
    Raises OrchestratorError when no build meets them all, naming the first condition that the closest build fails:
    the newest of those that meet the most conditions in a row. Raises it too for a target that is not a branch
    name, a HEAD with no merge base or one whose JUDGING_FILES differ from it, missing credentials, and when git
    fails or a main commit cannot be fetched, as host.git says; CIError as ci.retained_builds does.
    """
    # A leading "-" is refused as `check-ref-format --branch` refuses it; --branch itself is not used, since it
    # turns @{-N} into the branch checked out N switches ago.
    if target.startswith("-") or host.git_run(root, "check-ref-format", f"refs/heads/{target}").returncode != 0:
        raise OrchestratorError(f"{target!r} is not a branch name")
    tracking = f"refs/remotes/{REMOTE}/{target}"
    host.git(root, "fetch", "--quiet", REMOTE, f"+refs/heads/{target}:{tracking}")
    tree = host.git(root, "rev-parse", "--verify", "HEAD^{tree}").strip()
    merge_base = host.git_run(root, "merge-base", "HEAD", tracking)
    if merge_base.returncode != 0:
        # merge-base exits 1, printing nothing, when the commits have no common ancestor.
        raise OrchestratorError(f"HEAD has no merge base with {REMOTE}/{target}: "
                                f"{merge_base.stderr.strip() or 'no common ancestor'}")
    base = merge_base.stdout.strip()
    if changed := _judging_changes(host, root, base, "HEAD"):
        raise OrchestratorError(f"HEAD changes {', '.join(changed)} since its merge base {base}, which decide how "
                                f"it is judged")
    builds = ci.retained_builds()
    if not builds:
        raise OrchestratorError(f"the quality job {ci.job} has no builds")
    refusals = []
    for build in builds:
        met, why = _build_trust(host, root, build, tree, base)
        if why is None:
            return build.number
        refusals.append((met, why))
    # max keeps the first of equals, and the builds are newest first. A description is text from Jenkins, of any
    # length, so it is masked before the cut, which could split a token.
    raise OrchestratorError(_shortened(ci.mask(max(refusals, key=lambda r: r[0])[1]), 400))


def _build_trust(host: Host, root: str, build: QualityBuild, tree: str, base: str) -> tuple[int, str | None]:
    """How many of judged_in_quality's conditions build meets in a row, for HEAD's tree and merge base base, and
    why it fails the first it does not, None when it meets them all. The reason quotes the build's description
    whole, unmasked. Fetches the build's main commit, as judged_in_quality says, only when it meets the conditions
    before that one. Raises OrchestratorError as host.git does."""
    m = JUDGED_DESCRIPTION.fullmatch(build.description or "")
    if not m:
        return 0, (f"build #{build.number}'s description is not `judged tree=<sha> parent=<sha> main=<sha>`: "
                   f"{build.description!r}")
    if m["tree"] != tree:
        return 1, f"build #{build.number} judged the tree {m['tree']}, not HEAD's {tree}"
    if m["parent"] != base:
        return 2, f"build #{build.number} judged from {m['parent']}, not from HEAD's merge base {base}"
    if build.result != "SUCCESS":
        return 3, f"build #{build.number} {'is still running' if build.result is None else 'ended ' + build.result}"
    if host.git_run(root, "cat-file", "-e", f"{m['main']}^{{commit}}").returncode != 0:
        host.git(root, "fetch", "--quiet", REMOTE, m["main"])
    if changed := _judging_changes(host, root, base, m["main"]):
        return 4, (f"build #{build.number} judged by main at {m['main']}, where {', '.join(changed)} differ from "
                   f"HEAD's")
    return 5, None


def _judging_changes(host: Host, root: str, a: str, b: str) -> list[str]:
    """Those of JUDGING_FILES whose file differs between commits a and b, its mode included, or that only one of
    them has. Raises OrchestratorError as host.git does."""
    # --no-relative: under the user's diff.relative, a diff run in a subdirectory lists none of the top's files.
    names = host.git(root, "diff", "--name-only", "-z", "--no-relative", "--no-renames", "--no-ext-diff", a, b,
                     "--", *_top_literal(list(JUDGING_FILES)), verbatim=True)
    return [p for p in names.split("\0") if p]


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
    # The files that conflicted with the base branch when the pull request was opened, as a draft; empty again once
    # refresh found the pull request merging cleanly, merged or closed (refresh_conflict).
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
    # True once resume --no-quality-gate turned the gate off, for good: quality_job is None then, and the quality
    # files written until then stay the run's (turn_quality_gate_off).
    quality_gate_disabled: bool = False
    max_quality_rounds: int = DEFAULT_MAX_QUALITY_ROUNDS
    # The quality round the round is in: 0 until the Builder's first report, then q from quality round q on. A
    # quality-fix turn that ends after resume --no-quality-gate leaves it one past the last quality round, which never
    # runs.
    quality_round: int = 0
    # The quality build in flight, {ref, sha, queue_url, build}, each saved as soon as it is known.
    # rejected lists builds of ref that ended without a verdict, so a resume does not adopt them again.
    ci: dict = field(default_factory=dict)
    # The name of the run's workflow; a run saved before workflows existed ran the default.
    workflow: str = DEFAULT_WORKFLOW.name
    # The workflow_definition of one that is not built in, so a resume does not depend on its file.
    workflow_definition: dict | None = None
    # Prompts.overrides of the prompts the run started with (load_prompts), so a resume does not depend on the prompt
    # files either; empty for the built-in texts, as for a run saved before prompt files.
    prompt_overrides: dict[str, str] = field(default_factory=dict)
    # What prune did with branch once the pull request was done: "deleted", or "kept: <why>". Written by
    # prune_branches, never by the run itself.
    branch_cleanup: str | None = None
    # A run --worktree run's own git worktree, by its absolute path, from the run's start on: round 0 creates it
    # there. None for a run in the project's checkout, cwd. cwd stays the project, where the run directory is.
    worktree: str | None = None
    # What close did, one line such as "workspace closed; worktree deleted; branch deleted". Written by close_run,
    # never by the run itself; a closed run is not resumed.
    closed: str | None = None
    # One record per handoff file, in the order the turns started (Workflow._turn_record). A turn's record opens
    # when its prompt is delivered, or a quality analysis starts, and closes when its file is found. Absent from a
    # run saved before them.
    turns: list[dict] = field(default_factory=list)
    # One record per quality round judged, in order: {"round", "quality_round", "gate", "conditions",
    # "fingerprints_by_check"}, the last two as gate_failures reads them from its quality file. Fingerprints, never
    # the findings' text, so that state.json stays small.
    quality_rounds: list[dict] = field(default_factory=list)
    # The BLOCKED report the current turn waits on the human for, {"file", "question", "k"}: file is the handoff
    # file's basename, and the report was moved to its k-th blocked name (Workflow._blocked_path). None otherwise.
    blocked: dict | None = None
    # Per locking step (Step.locks), by id: the paths its last turn locked, as Host.lock gives them. Their locked
    # versions are in the commit at lock_ref(step id).
    locked: dict[str, list[str]] = field(default_factory=dict)
    # One record per editing turn that changed locked files, {"round", "file", "paths"}, in the order of the turns:
    # file is the basename of the diff of what the turn changed in them, in the run directory, and paths are the
    # files, by the id of the step that locked them. The files were restored once the record was saved.
    restored: list[dict] = field(default_factory=list)
    # One record per run of a proof the verdict step wrote with a review that requested changes, {"round", "written",
    # "file", "finding", "demonstrated", "said"}, in the order they ran: round is the round it ran in, written the
    # round whose verdict step wrote it, file its basename in proofs_dir(written), finding the first number in that
    # name, demonstrated whether it failed against the change, and said how it exited and the last PROOF_TAIL_LINES
    # lines it printed. A proof first runs in the round that wrote it (Workflow._judge_proofs), then again in every
    # later round until it does not fail (Workflow._rerun_proofs). A record saved before written existed is a first
    # run: proof_written reads it.
    proofs: list[dict] = field(default_factory=list)

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
    def prompts(self) -> Prompts:
        """The prompts the run sends: those it started with (saved_prompts)."""
        return saved_prompts(self.prompt_overrides, self.run_id)

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

    @property
    def refs_prefix(self) -> str:
        """Where the run keeps its local refs, which nothing pushes and done deletes."""
        return f"{LOCAL_REFS_PREFIX}{self.key}/"

    def proofs_dir(self, n: int) -> str:
        """Where the verdict step of round n writes its proofs (Pipeline.proof_command)."""
        return f"{self.dir}/proofs-{n}"

    def lock_ref(self, step_id: str) -> str:
        """The local ref of the commit that holds the files the step locked."""
        return f"{self.refs_prefix}locked/{step_id}"

    # The default workflow's handoff files; the quality files are every workflow's.
    @property
    def spec_path(self) -> str:
        return f"{self.dir}/spec.md"

    def build_path(self, n: int) -> str:
        return f"{self.dir}/build-{n}.md"

    def review_path(self, n: int) -> str:
        return f"{self.dir}/review-{n}.md"

    def leaks_path(self, push: str) -> str:
        """Where gitleaks' report on the push named push ("snapshot" or "publish") goes; each scan replaces it."""
        return f"{self.dir}/leaks-{push}.json"

    def quality_path(self, n: int, q: int) -> str:
        return f"{self.dir}/quality-{n}-{q}.md"

    def quality_build_path(self, n: int, q: int) -> str:
        """The Builder's answer to quality-<n>-<q>.md."""
        return f"{self.dir}/build-{n}-q{q}.md"

    def builder_report_path(self, n: int, q: int) -> str:
        """The report of the Builder's turn in round n that answers quality round q, or none when q is 0."""
        return self.quality_build_path(n, q) if q else self.build_path(n)

    @property
    def had_quality_gate(self) -> bool:
        """Whether the run has the quality gate, or had it until resume turned it off: whether it may have quality
        files."""
        return self.quality_job is not None or self.quality_gate_disabled

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


def _labelled_word(label: str, words: tuple[str, ...]) -> str:
    """A regex for `label:`, blanks, then one of words: group 1 is `label:` with the blanks, group 2 the word.

    Every reader and rewriter of a verdict or gate line matches through this, so they agree on which lines hold a
    word. The word must end there: a word character after it continues it into another word, unless it is one of
    the underscores that close emphasis, as in `_VERDICT: APPROVE_`.
    """
    return rf"({label}:\s*)({'|'.join(map(re.escape, words))})(?=_*(?!\w))"


def _first_line_word(text: str, label: str, words: tuple[str, ...]) -> str | None:
    """The one of words that follows `label:` on text's first non-blank line, Markdown emphasis around them
    tolerated; None when that line has none of them, or when text is blank."""
    pattern = _labelled_word(label, words)
    for line in text.splitlines():
        if line.strip():
            m = re.search(pattern, line)
            return m.group(2) if m else None
    return None


def parse_verdict(review: str) -> str | None:
    """The verdict on the review's first non-blank line, tolerating Markdown emphasis around it."""
    return _first_line_word(review, "VERDICT", (APPROVE, CHANGES_REQUESTED))


def parse_blocked(report: str) -> str | None:
    """The question of a report whose first non-blank line starts with BLOCKED:, or None for any other report."""
    for line in report.splitlines():
        if line.strip():
            if not line.lstrip().startswith(BLOCKED):
                return None
            return line.lstrip().removeprefix(BLOCKED).strip() or NO_QUESTION
    return None


# The engine's copy of what it needs of the quality file's format. The gate, which writes the file, is the home of
# that format: gate.py's quality_report and its sections, which docs/quality-gate.md's "The quality file as a
# contract" describes. TestQualityFileContract checks that the two agree.
# The words of the gate line, the file's first non-blank one. Only the engine writes BUILD_FAILED, for a build that
# ended before its Gate stage (Workflow._quality_report).
QUALITY_OK = "OK"
QUALITY_ERROR = "ERROR"
QUALITY_BUILD_FAILED = "BUILD_FAILED"
# Only the engine writes LEAK too: gitleaks found a secret in the quality round's snapshot, so it was not pushed and
# Jenkins did not judge it (leak_report).
QUALITY_LEAK = "LEAK"
# The headings of the sections gate_failures reads, besides its numbered findings.
QUALITY_FAILED_CONDITIONS = "## Failed conditions"
QUALITY_FAILING_TESTS = "## Failing tests"
# leak_report's section, numbering each Leak as its str gives it.
QUALITY_SECRETS_FOUND = "## Secrets found"
# Leads the findings to weigh, in a section of the gate's or the orchestrator's checks'.
QUALITY_TO_WEIGH_NOTE = "These do not fail the gate. Fix each, or say in your report why it should stand."
# The most findings a quality file lists, numbered as one list, and the most failing tests; the rest are counted.
QUALITY_FINDINGS_LIMIT = 50
# Each character that ends a line in some reader of the quality file, mapped to its escape, as gate.py's
# _LINE_BREAK_ESCAPES writes it, so a list item the engine adds reads back as the one line the gate's would.
_QUALITY_LINE_BREAK_ESCAPES = str.maketrans({c: f"\\x{ord(c):02x}" if ord(c) <= 0xFF else f"\\u{ord(c):04x}"
                                             for c in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"})


def one_line(text: str) -> str:
    """text as a quality file writes it within one of its lines, such as one item of a list: each character that
    ends a line in some reader written as an escape (_QUALITY_LINE_BREAK_ESCAPES), anything else as it is."""
    return text.translate(_QUALITY_LINE_BREAK_ESCAPES)


def parse_gate(quality: str) -> str | None:
    """The gate on the quality file's first non-blank line, tolerating Markdown emphasis around it."""
    return _first_line_word(quality, "GATE", (QUALITY_OK, QUALITY_ERROR, QUALITY_BUILD_FAILED, QUALITY_LEAK))


def _under_gate_line(quality: str, line: str) -> str:
    """The quality file with line as a paragraph of its own right after its GATE line, the first non-blank one."""
    lines = quality.split("\n")
    at = next((i for i, x in enumerate(lines) if x.strip()), len(lines) - 1)
    return "\n".join([*lines[:at + 1], "", line, *lines[at + 1:]])


def leak_report(leaks: list[Leak], intro: str) -> str:
    """The quality file of a round whose snapshot holds secrets: QUALITY_LEAK, intro, and the leaks numbered, each
    where it is, its rule and its fingerprint, never the secret."""
    return "\n".join([f"GATE: {QUALITY_LEAK}", "", intro, "", QUALITY_SECRETS_FOUND, "",
                      *(one_line(f"{k}. {leak}") for k, leak in enumerate(leaks, 1))]) + "\n"


class GateFailures(NamedTuple):
    """What a quality file says failed the gate, in a form two quality rounds can be compared by."""
    conditions: list[str]  # the failed conditions, as the file words them
    # By check, the first word of a finding's rule, or "tests" for a failing test: finding_fingerprint of each
    # finding that fails the gate, and of each failing test, in the file's order. Findings to weigh are left out.
    # A secret, under "gitleaks", is finding_fingerprint(path, "gitleaks <rule>", "") with "-<k>" added for the k-th
    # secret of that rule in that file, its line and commit left out, so that two rounds compare by how many each has.
    fingerprints: dict[str, list[str]]

    def repeats(self, earlier: "GateFailures") -> bool:
        """Whether these failures are the earlier ones again: something failed, nothing that failed before was
        fixed, and nothing new failed. A finding counts as the same wherever it moved (finding_fingerprint), a
        condition only when its words are."""
        now = self._all()
        return bool(now) and now == earlier._all()

    def _all(self) -> set[str]:
        return {*self.conditions, *(f for fs in self.fingerprints.values() for f in fs)}


def finding_fingerprint(path: str | None, rule: str, message: str) -> str:
    """A short hash that names a finding however its line moves: its path, its rule and its message without
    digits, since a line number in it moves too. A finding about the whole change has no path."""
    key = "\0".join([path or "", rule, re.sub(r"\d", "", message)])
    return hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:16]


# A numbered finding as _numbered_finding writes it: where, then severity, rule and message.
_NUMBERED_FINDING = re.compile(r"\d+\. (?:`(?P<path>.+?)(?::\d+| \(the whole file\))`|The change as a whole:) "
                               r"\S+ (?P<rule>.+?): (?P<message>.*)")
_FAILING_TEST = re.compile(r"- `(?P<name>.+)`: .*")
# A numbered Leak under QUALITY_SECRETS_FOUND; its commit and fingerprint name the round's snapshot, new in each round.
_SECRET_FOUND = re.compile(r"\d+\. `(?P<path>.+):\d+` (?P<rule>\S+) in \S+ \(fingerprint `.*`\)")


def gate_failures(quality: str) -> GateFailures:
    """What the quality file, as the gate, judge_run and leak_report write it, says failed the gate: its failed
    conditions, the numbered findings before a QUALITY_TO_WEIGH_NOTE in each section, its failing tests, and its
    secrets under the check "gitleaks", each as the k-th secret of its rule in its file, wherever its line moved and
    whichever snapshot held it, so that a round which removes one of a file's two keys of a kind is not the same
    again."""
    conditions: list[str] = []
    fingerprints: dict[str, list[str]] = {}
    secrets: collections.Counter[tuple[str, str]] = collections.Counter()
    section, weighing = "", False
    for line in quality.split("\n"):
        if line.startswith("## "):
            section, weighing = line, False
        elif line == QUALITY_TO_WEIGH_NOTE:
            weighing = True
        elif section == QUALITY_FAILED_CONDITIONS and line.startswith("- "):
            conditions.append(line[2:])
        elif section == QUALITY_FAILING_TESTS and (test := _FAILING_TEST.fullmatch(line)):
            fingerprints.setdefault("tests", []).append(finding_fingerprint(test["name"], "tests", ""))
        elif section == QUALITY_SECRETS_FOUND and (secret := _SECRET_FOUND.fullmatch(line)):
            key = (secret["path"], f"gitleaks {secret['rule']}")
            secrets[key] += 1
            fingerprints.setdefault("gitleaks", []).append(f"{finding_fingerprint(*key, '')}-{secrets[key]}")
        elif not weighing and (finding := _NUMBERED_FINDING.fullmatch(line)):
            fingerprints.setdefault(finding["rule"].split(" ", 1)[0], []).append(
                finding_fingerprint(finding["path"], finding["rule"], finding["message"]))
    return GateFailures(conditions, fingerprints)


def build_failed_report(intro: str, tests: list[tuple[str, str]] | None, console: str) -> str:
    """The quality file of a build that ended before its Gate stage, so that nothing judged the change: GATE:
    BUILD_FAILED, then intro, then a section of tests, the (name, detail) of the failing tests Jenkins reported, None
    when it has no test report, and one of console, the end of the build's console, unless it is empty."""
    out = [f"GATE: {QUALITY_BUILD_FAILED}", "", intro, "", QUALITY_FAILING_TESTS, ""]
    if tests is None:
        out.append("Jenkins has no test report for this build.")
    else:
        out += [one_line(f"- `{name}`: {detail.strip().splitlines()[0] if detail.strip() else 'failed'}")
                for name, detail in tests[:QUALITY_FINDINGS_LIMIT]] or ["None."]
        if len(tests) > QUALITY_FINDINGS_LIMIT:
            out.append(f"- and {len(tests) - QUALITY_FINDINGS_LIMIT} more.")
    if console:
        out += ["", f"## The last {CONSOLE_TAIL_LINES} lines of the build's console", "", "````", console, "````"]
    return "\n".join(out) + "\n"


def _scan_git_env(git_dir: str, empty_tree: str) -> list[str]:
    """The `env` prefix under which the secret scan runs git, and gitleaks, which runs git: the repository at
    git_dir, with no tree's attributes, so that no .gitattributes the change commits or the base holds decides how
    git diffs a file; git diffs as text every file it takes for text by its content. empty_tree is the repository's
    empty tree. $GIT_DIR/info/attributes and core.attributesFile still apply, which only the repository's or the
    user's own files set, never a change.

    GIT_DIR, from a directory outside the working tree, leaves out the working tree's .gitattributes. attr.tree goes
    in as GIT_CONFIG_COUNT sets config, so that it reaches the git gitleaks starts and overrides the user's.
    GIT_WORK_TREE, GIT_ATTR_SOURCE and GIT_CONFIG_PARAMETERS (which `git -c` exports, read after GIT_CONFIG_COUNT)
    would bring a tree's attributes back, and are unset."""
    return ["env", "-u", "GIT_WORK_TREE", "-u", "GIT_ATTR_SOURCE", "-u", "GIT_CONFIG_PARAMETERS",
            f"GIT_DIR={git_dir}", "GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=attr.tree", f"GIT_CONFIG_VALUE_0={empty_tree}"]


def _base_rules_refused(base: str, why: str) -> OrchestratorError:
    """The error the secret scan fails closed with when it cannot take its rules from base, a commit, as gitleaks
    would read them; why names the file and says what is wrong with it, and follows "its"."""
    return OrchestratorError(f"nothing was pushed: the secret scan takes its rules from base {base[:12]}, but its {why}")


def _gitleaks_extended_file(config: str) -> str | None:
    """The path of the file that config, a gitleaks config's text, extends with `[extend] path`, as config spells it,
    from the repository's top level, from which a gitleaks run there reads it; None where config extends none (no
    [extend], no path, or an empty one). gitleaks reads its config's keys whatever their case, so `[Extend] Path`
    counts too.

    Raises ValueError, saying why in words that follow the config file's name, when config is not TOML, gives [extend]
    or its path twice in letters that differ only in case, or one that is not a table or a string, or the path is
    absolute, holds a NUL, or leaves the repository (`..`)."""
    try:
        parsed = tomllib.loads(config)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"is not TOML: {e}") from e
    extend = _gitleaks_key(parsed, "extend", dict)
    path = _gitleaks_key(extend, "path", str) if extend else None
    if not path:
        return None
    name = posixpath.normpath(path)
    if posixpath.isabs(path):
        why = "is an absolute path"
    elif "\0" in path:
        why = "holds a NUL"
    elif name == ".." or name.startswith("../"):
        why = "leaves the repository"
    elif name == ".":
        why = "is the top level, no file"
    else:
        return path
    raise ValueError(f"extends {path!r}, which {why}")


def _gitleaks_key(table: dict, key: str, kind: type) -> Any:
    """table's entry for key, a lower-case key of a gitleaks config, in whatever case table spells it, as gitleaks
    reads it; None where table has none. Raises ValueError, saying why as _gitleaks_extended_file's errors do, when
    table spells key more than one way or its entry is not of kind (dict for a table, str for a string)."""
    found = [v for k, v in table.items() if k.lower() == key]
    if len(found) > 1:
        raise ValueError(f"gives {key} {len(found)} times, in letters that differ only in case")
    if found and not isinstance(found[0], kind):
        raise ValueError(f"gives a {key} that is not a {'table' if kind is dict else 'string'}")
    return found[0] if found else None


def added_lines(diff: str) -> dict[str, dict[int, str]]:
    """For each file a `git diff -U0` touches, by its path in the new tree, the lines it gains: each one's text
    without the +, by its line there. A file the diff touches without adding a line, such as a pure rename, one that
    only loses lines or an empty new file, maps to an empty dict; a deleted file is left out. Only `diff --git` and
    `@@` lines are read inside a hunk, since an added line can itself start with `+++ `. Only "\n" ends a line, as
    in git's output: an added line can hold a carriage return, a form feed or U+2028.

    The engine's own reading of Host.change_diff's shape; the gate's changed_lines reads the same shape and is the
    home of how, so a fix to one belongs in both.
    """
    out: dict[str, dict[int, str]] = {}
    path, line, in_hunk = None, 0, False
    for text in diff.split("\n"):
        if text.startswith("diff --git "):
            path, in_hunk = _patch_git_path(text[len("diff --git "):]), False
        elif text.startswith("@@"):
            in_hunk = True
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", text)
            line = int(m.group(1)) if m else 0
        elif not in_hunk:
            path = _patch_header_path(out, path, text)
        elif text.startswith("+") and path is not None:
            out[path][line] = text[1:]
            line += 1
        if path is not None:
            out.setdefault(path, {})
    return out


def _patch_header_path(out: dict[str, dict[int, str]], path: str | None, text: str) -> str | None:
    """The file the diff is about after text, a header line outside a hunk, path before it; a deleted file is
    dropped from out, as added_lines says."""
    if text.startswith("deleted file mode") and path is not None:
        out.pop(path, None)
        return None
    if text.startswith("rename to "):
        return _patch_unquote(text[len("rename to "):])
    if text.startswith("+++ "):
        # git ends the name with a tab when it holds a space.
        name = _patch_unquote(text[len("+++ "):].removesuffix("\t"))
        return None if name == "/dev/null" else name.removeprefix("b/")
    return path


def _patch_git_path(names: str) -> str | None:
    """The path of `a/<path> b/<path>`; None when the two differ, as for a rename, which later lines name."""
    if m := re.fullmatch(r'("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")', names):
        a, b = _patch_unquote(m.group(1)), _patch_unquote(m.group(2))
    else:
        half = (len(names) - 1) // 2
        a, b = names[:half], names[half + 1:]
    if a.startswith("a/") and b.startswith("b/") and a[2:] == b[2:]:
        return b[2:]
    return None


def _patch_unquote(name: str) -> str:
    """A path as git quotes it when it holds a quote, a backslash or a control character, or, with core.quotePath
    on, a byte above 0x7f; any other name as it is. The path comes back as os.fsdecode makes it."""
    if len(name) < 2 or not (name.startswith('"') and name.endswith('"')):
        return name
    escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", '"': '"', "\\": "\\"}
    # An octal escape is one byte of the name, so a character above 0x7f is a run of them: gather the bytes and
    # decode them together.
    raw = bytearray()
    for m in re.finditer(r'\\([0-3][0-7]{2}|.)|[^\\]+|\\', name[1:-1], re.DOTALL):
        if m.group(1) is None:
            raw += os.fsencode(m.group(0))
        elif len(m.group(1)) == 3:
            raw.append(int(m.group(1), 8))
        else:
            raw += os.fsencode(escapes.get(m.group(1), m.group(1)))
    return os.fsdecode(bytes(raw))


# ---------------------------------------------------------------------------
# Orchestrator checks: the quality round's own, after the build's gate
# ---------------------------------------------------------------------------

# A new dependency whose first release is fewer days old than this is flagged: few have used it yet, and a name
# registered for the occasion is the slopsquatting move.
YOUNG_PACKAGE_DAYS = 14
# Seconds of a quality round's QUALITY_TIMEOUT that an orchestrator check leaves unused: it starts no slow step, such
# as a request to PyPI, with less left. A request can take PYPI_TIMEOUT more than once, to connect and to read.
SLOW_STEP_MARGIN = 4 * PYPI_TIMEOUT


@dataclass(frozen=True)
class RunChange:
    """The change a quality round judges, as each OrchestratorCheck is given it: what the build's gate saw, and what
    only the run has."""
    spec: str  # the run's spec: its contract step's file, or its task for a workflow without one
    base: str  # the run's base commit
    commit: str  # the snapshot the round judges, whose only parent is base
    changed: dict[str, set[int]]  # added_lines of Host.change_diff from base to commit: the lines each file gains
    files: list[ChangedFile]  # Host.changed_line_counts from base to commit: every file the change touches
    # (commit, path) -> the file at path, relative to the repository's top level, as commit has it, verbatim as
    # Host.file_at gives it; None where it has none. Raises OrchestratorError when git fails on the agents' machine.
    file_at: Callable[[str, str], str | None]
    now: float  # when the round judges it, in seconds since the epoch
    # Called before each step of a check that may be slow, such as a request to PyPI: it keeps the run's heartbeat
    # going, and is False once the quality round has too little time left to start one (SLOW_STEP_MARGIN).
    in_time: Callable[[], bool]


class CheckFindings(NamedTuple):
    """What an OrchestratorCheck found, each finding {path, line, severity, rule, message} as GateCheck's."""
    issues: list[dict]  # the findings that fail the gate
    to_weigh: list[dict]  # those that do not


@dataclass(frozen=True)
class OrchestratorCheck:
    """A check the orchestrator runs itself, in each quality round after the build's gate, because it needs what the
    build never sees: the run's spec and its workflow. The first, fixed form of roadmap gap I.

    find returns what it found; a finding's rule is the check's own, which judge_run prefixes with name ("" for
    none), and its path None for one about the change as a whole. A check that cannot do its work says so in a
    finding, never by finding nothing. It raises nothing but OrchestratorError, from RunChange.file_at, which fails
    the round as a Jenkins failure does.
    """
    name: str
    find: Callable[[RunChange], CheckFindings]


def orchestrator_checks(pipeline: Pipeline, pypi: "PyPI") -> tuple[OrchestratorCheck, ...]:
    """The checks a quality round of a run of pipeline runs, in this order: the new dependencies, which it asks pypi
    about, and the change's size against the workflow's max_files and max_lines, and its paths against the spec."""
    return (
        OrchestratorCheck("new-dependencies", functools.partial(new_dependencies, pypi=pypi)),
        OrchestratorCheck("diff-budget", functools.partial(diff_budget, max_files=pipeline.max_files,
                                                           max_lines=pipeline.max_lines)),
        OrchestratorCheck("unexpected-paths", unexpected_paths),
    )


def judge_run(quality: str, checks: tuple[OrchestratorCheck, ...], change: RunChange) -> str:
    """The quality file the build's gate wrote, quality, with what checks find in change added.

    A check with failing findings is a failed condition, which turns GATE: OK into GATE: ERROR. Every finding is
    listed in a section of their own at the end, failing ones first, numbered on from the file's last number while
    the file lists at most QUALITY_FINDINGS_LIMIT in all; the rest are counted. quality comes back as it is when they
    find nothing.
    """
    conditions, issues, to_weigh = [], [], []
    for check in checks:
        found, weigh = (_named_findings(check.name, part) for part in check.find(change))
        if found:
            issues += found
            conditions.append(_orchestrator_condition(check.name, len(found)))
        to_weigh += weigh
    if not issues and not to_weigh:
        return quality
    if conditions:
        quality = _failed_by(quality, conditions)
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", quality, re.MULTILINE)]
    return quality.rstrip("\n") + "\n" + "\n".join(_orchestrator_section(issues, to_weigh, max(numbers, default=0))) \
        + "\n"


def _named_findings(name: str, findings: list[dict]) -> list[dict]:
    """findings with their rules prefixed by the check's name."""
    return [{**f, "rule": f"{name} {f['rule']}" if f["rule"] else name} for f in findings]


def _orchestrator_condition(name: str, n: int) -> str:
    """The failed condition of an orchestrator check that fails the gate with n findings, worded as the gate's
    _check_condition words its own checks'."""
    return f"{n} findings of the `{name}` check; each is listed below."


def _failed_by(quality: str, conditions: list[str]) -> str:
    """quality with GATE: ERROR, and conditions last among its failed conditions, a section it gains, before its
    first other one below the gate line, when it has none. The gate line stays the first non-blank one, even when
    it is itself a `## ` heading, so parse_gate reads ERROR from the result."""
    lines = quality.split("\n")
    at = next((i for i, x in enumerate(lines) if x.strip()), 0)
    lines[at] = re.sub(_labelled_word("GATE", (QUALITY_OK,)), rf"\g<1>{QUALITY_ERROR}", lines[at], count=1)
    bullets = [one_line(f"- {c}") for c in conditions]
    if QUALITY_FAILED_CONDITIONS in lines:
        end = lines.index(QUALITY_FAILED_CONDITIONS) + 2
        while end < len(lines) and lines[end].startswith("- "):
            end += 1
        lines[end:end] = bullets
    else:
        first = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        lines[first:first] = [QUALITY_FAILED_CONDITIONS, "", *bullets, ""]
    return "\n".join(lines)


def _orchestrator_section(issues: list[dict], to_weigh: list[dict], after: int) -> list[str]:
    """judge_run's section, numbered on from after, as lines that start with a blank one."""
    out = ["", "## The orchestrator's checks", "",
           ("The orchestrator ran these itself after the build, with what the build does not see: the run's spec and "
            "its workflow.")]
    # The limit is the whole file's, the build's findings included.
    room = max(QUALITY_FINDINGS_LIMIT - after, 0)
    listed_issues = issues[:room]
    listed_weigh = to_weigh[:room - len(listed_issues)]
    if listed_issues:
        out += ["", *(_numbered_finding(k, f) for k, f in enumerate(listed_issues, after + 1))]
    if listed_weigh:
        out += ["", QUALITY_TO_WEIGH_NOTE, "",
                *(_numbered_finding(k, f) for k, f in enumerate(listed_weigh, after + len(listed_issues) + 1))]
    if more := len(issues) + len(to_weigh) - len(listed_issues) - len(listed_weigh):
        out += ["", f"{more} more findings of the orchestrator's checks are not listed."]
    return out


def _numbered_finding(k: int, finding: dict) -> str:
    """The finding as the k-th of the quality file's numbered list, as the gate's _numbered writes it and
    _NUMBERED_FINDING reads it."""
    if finding["path"] is None:
        where = "The change as a whole:"
    elif finding["line"] is None:
        where = f"`{finding['path']} (the whole file)`"
    else:
        where = f"`{finding['path']}:{finding['line']}`"
    return one_line(f"{k}. {where} {finding['severity']} {finding['rule']}: {finding['message']}")


def _check_finding(path: str | None, line: int | None, rule: str, message: str, severity: str = "warning") -> dict:
    """A finding as OrchestratorCheck.find returns it, of the gate's checks' shape (the gate's _finding): a check
    gives warnings for what it only reports, and errors for what fails the gate."""
    return {"path": path, "line": line, "severity": severity, "rule": rule, "message": message}


# -- New dependencies ------------------------------------------------------

# PyPI's index as uv.lock and [[tool.uv.index]] name it; a package from any other index is not PyPI's to vouch for.
PYPI_INDEX_URL = "https://pypi.org/simple"


@dataclass(frozen=True)
class PackageSource:
    """Where a package comes from when it is not PyPI, as uv's sources name it."""
    # workspace, path, git, url or index; unknown for one uv names in a way this reader does not know.
    kind: str
    # The path, repository, URL or index URL, as the manifest gives it; for unknown, the whole entry, as TOML.
    where: str

    def describe(self) -> str:
        """Where it comes from, as a finding says it."""
        if self.kind == "workspace":
            return "a member of the project's workspace, not from PyPI"
        if self.kind == "index":
            return f"from the index {self.where}, not checked against PyPI"
        if self.kind == "unknown":
            return (f"from the uv source {self.where}, which this check does not know, so where it comes from is not "
                    f"checked")
        place = {"path": "the path ", "git": "the repository "}.get(self.kind, "")
        return f"from {place}{self.where}, not from PyPI"


@dataclass(frozen=True)
class Dependencies:
    """What a project's manifests say it depends on, each package by package_key with its PackageSource, None for
    PyPI."""
    direct: dict[str, PackageSource | None]  # the project's own choice, which the spec must name
    locked: dict[str, PackageSource | None]  # every package its lock file pins, the project and its workspace aside
    # Path -> why, for each manifest that cannot be read; what direct and locked say then comes from the others only.
    unreadable: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DependencyEcosystem:
    """A kind of project's dependency manifests, at the repository's top level, and how to read them. Manifests
    anywhere else, such as test fixtures, are not the project's."""
    manifest: str  # the file that lists the project's own dependencies
    lock_file: str  # the file beside it that pins every package they pull in
    # The project's Dependencies, given (path) -> that file's text, None where there is none; each manifest it cannot
    # read is in their unreadable, and the others are still read.
    read: Callable[[Callable[[str], str | None]], Dependencies]


def package_key(name: str) -> str:
    """A Python package's name as PyPI compares names (PEP 503): lower case, each run of -, _ and . one -."""
    return re.sub(r"[-_.]+", "-", name).lower()


def python_dependencies(read: Callable[[str], str | None]) -> Dependencies:
    """DependencyEcosystem.read of a Python project with uv.

    Its direct dependencies are pyproject.toml's [project] dependencies and optional-dependencies and its
    [dependency-groups], an {include-group} entry and the project itself left out. A dependency's source is its
    [tool.uv.sources] entry, else its PEP 508 URL, else the [[tool.uv.index]] marked default, else the index uv.lock
    has it from. The locked packages are uv.lock's, the project and its workspace members left out.
    """
    unreadable = {}
    locked: dict[str, PackageSource | None] = {}
    direct: dict[str, PackageSource | None] = {}
    try:
        locked = _uv_lock_packages(_toml_manifest(read, "uv.lock"))
    except (ValueError, TypeError) as e:
        unreadable["uv.lock"] = str(e)
    try:
        direct = _pyproject_dependencies(_toml_manifest(read, "pyproject.toml"))
    except (ValueError, TypeError) as e:
        unreadable["pyproject.toml"] = str(e)
    return Dependencies({key: source or locked.get(key) for key, source in direct.items()}, locked, unreadable)


def _toml_manifest(read: Callable[[str], str | None], path: str) -> dict:
    """The table of the TOML file at path, read verbatim as Host.file_at gives it; {} where there is none. ValueError
    for one that is not UTF-8, as TOML must be, or not TOML."""
    text = read(path)
    if text is None:
        return {}
    try:
        return tomllib.loads(os.fsencode(text).decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ValueError(f"it is not UTF-8: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"it is not TOML: {e}") from e


def _pyproject_dependencies(pyproject: dict) -> dict[str, PackageSource | None]:
    """python_dependencies's direct dependencies of a pyproject.toml, parsed; ValueError or TypeError, saying why, for
    one of another shape."""
    project = _toml_table(pyproject, "project")
    uv = _toml_table(_toml_table(pyproject, "tool"), "uv")
    sources = _uv_sources(uv)
    default = next((_index_source(i.get("url")) for i in _uv_indexes(uv) if i.get("default") is True), None)
    own = package_key(project.get("name") or "")
    out: dict[str, PackageSource | None] = {}
    for where, requirements in _requirement_lists(project, pyproject):
        if not isinstance(requirements, list):
            raise TypeError(f"{where} is not a list")
        for key, url in (_requirement(r) for r in requirements if not isinstance(r, dict)):
            if key != own:
                out[key] = sources.get(key) or (PackageSource("url", url) if url else default)
    return out


def _requirement_lists(project: dict, pyproject: dict) -> list[tuple[str, object]]:
    """(where, its list) of each list of requirements in a pyproject.toml, parsed, whose [project] table is project."""
    return [("[project] dependencies", project.get("dependencies", [])),
            *((f"[project.optional-dependencies] {k}", v)
              for k, v in _toml_table(project, "optional-dependencies").items()),
            *((f"[dependency-groups] {k}", v) for k, v in _toml_table(pyproject, "dependency-groups").items())]


def _uv_indexes(uv: dict) -> list[dict]:
    """[tool.uv]'s [[index]] tables."""
    indexes = uv.get("index", [])
    if not isinstance(indexes, list) or not all(isinstance(i, dict) for i in indexes):
        raise TypeError("[[tool.uv.index]] is not a list of tables")
    return indexes


def _uv_sources(uv: dict) -> dict[str, PackageSource | None]:
    """The PackageSource of each package [tool.uv.sources] names, by package_key; None for one from PyPI's index. Of
    a package with several sources, each for some platforms, the first."""
    indexes = {i.get("name"): i.get("url") for i in _uv_indexes(uv)}
    out = {}
    for name, entry in _toml_table(uv, "sources").items():
        first = entry[0] if isinstance(entry, list) and entry else entry
        if not isinstance(first, dict):
            raise TypeError(f"[tool.uv.sources] {name} is not a table")
        out[package_key(name)] = _uv_source(first, indexes)
    return out


def _uv_source(entry: dict, indexes: dict) -> PackageSource | None:
    """The PackageSource of a [tool.uv.sources] entry, given the indexes' URLs by name."""
    if entry.get("workspace") is True:
        return PackageSource("workspace", "")
    if "index" in entry:
        return _index_source(indexes.get(entry["index"]) or entry["index"])
    kind = next((k for k in ("path", "git", "url") if k in entry), None)
    if kind is None:
        return _unknown_source(entry)
    return PackageSource(kind, str(entry[kind]))


def _unknown_source(table: dict) -> PackageSource:
    """The PackageSource of a source table of a shape this reader does not know, written as an inline table."""
    return PackageSource("unknown", "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in table.items()) + " }")


def _index_source(url) -> PackageSource | None:
    """The PackageSource of a package from the index at url; None for PyPI's."""
    return None if str(url).rstrip("/") == PYPI_INDEX_URL else PackageSource("index", str(url))


def _uv_lock_packages(lock: dict) -> dict[str, PackageSource | None]:
    """python_dependencies's locked packages of a uv.lock, parsed; TypeError for one of another shape."""
    packages = lock.get("package", [])
    if not isinstance(packages, list) or not all(isinstance(p, dict) and isinstance(p.get("name"), str)
                                                 and isinstance(p.get("source", {}), dict) for p in packages):
        raise TypeError("[[package]] is not a list of tables with a name")
    # editable and virtual: the project itself and its workspace members.
    return {package_key(p["name"]): _lock_source(p.get("source", {})) for p in packages
            if not {"editable", "virtual"} & p.get("source", {}).keys()}


def _lock_source(source: dict) -> PackageSource | None:
    """The PackageSource of a uv.lock package's source table."""
    if "registry" in source:
        return _index_source(source["registry"])
    kind = next((k for k in ("path", "directory", "git", "url") if k in source), None)
    if kind is None:
        return _unknown_source(source)
    return PackageSource("path" if kind == "directory" else kind, str(source[kind]))


def _toml_table(parent: dict, key: str) -> dict:
    """The table key of the TOML table parent, {} when it has none; TypeError when key is not a table."""
    table = parent.get(key, {})
    if not isinstance(table, dict):
        raise TypeError(f"{key} is not a table")
    return table


# A PEP 508 requirement's name, extras and direct URL: `name[extra] @ url ; marker`, or a version after the name.
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\]\s*)?(?:@\s*(\S+))?")


def _requirement(requirement) -> tuple[str, str | None]:
    """(package_key, direct URL or None) of a PEP 508 requirement; ValueError for something else."""
    m = _REQUIREMENT.match(requirement) if isinstance(requirement, str) else None
    if not m:
        raise ValueError(f"{requirement!r} is not a requirement")
    return package_key(m.group(1)), m.group(2)


# The ecosystems whose new packages the new-dependencies check judges. Python's so far, whose index is PyPI; another
# ecosystem's needs its own index too.
DEPENDENCY_ECOSYSTEMS = (DependencyEcosystem("pyproject.toml", "uv.lock", python_dependencies),)


@dataclass(frozen=True)
class _NewPackage:
    """A package a project depends on after the change and not before it."""
    key: str  # package_key
    path: str  # the manifest that names it: its ecosystem's manifest for a direct one, else its lock file
    line: int | None  # the first line the change added to it that names the package; None when none does
    direct: bool  # one of Dependencies.direct; else a package the lock file alone adds
    source: PackageSource | None  # None for PyPI


def new_dependencies(change: RunChange, *, pypi: "PyPI") -> CheckFindings:
    """The packages that the change adds to a DEPENDENCY_ECOSYSTEMS project, each from PyPI asked of pypi.

    Fails a direct dependency the spec does not name (spec_names), unless it is a member of the project's workspace;
    a package PyPI does not know; those PyPI could not be asked about; and a manifest in the change that cannot be
    read. Reports a direct dependency the spec names, or a workspace member, with where it comes from: what PyPI knows
    of it, flagged when its first release is under YOUNG_PACKAGE_DAYS days old, or its source, other-index for an
    index other than PyPI's; the packages a lock file alone adds, those the direct ones pull in, in one finding per
    lock file; a dependency of the base's that the change takes from another source (source-changed); and a
    manifest that cannot be read at the base, which leaves its ecosystem unchecked. PyPI is asked
    only while change.in_time(), and no more after the first request it gives no usable answer to.
    """
    packages, issues, to_weigh = _new_packages(change)
    known, failure = _ask_pypi(packages, pypi, change.in_time)
    named = spec_names(change.spec)
    pulled_in: dict[str, list[str]] = {}
    for p in packages:
        if not p.direct and (p.source or known.get(p.key)):
            pulled_in.setdefault(p.path, []).append(f"`{p.key}` ({_whence(p, known, change.now)})")
        elif judged := _package_finding(p, named, known, change.now):
            fails, finding = judged
            (issues if fails else to_weigh).append(finding)
    for path, described in pulled_in.items():
        to_weigh.append(_check_finding(path, None, "pulled-in", (
            f"{len(described)} new packages that the change's dependencies pull in: {', '.join(described)}.")))
    if unasked := list(dict.fromkeys(p.key for p in packages if not p.source and p.key not in known)):
        issues.append(_check_finding(None, None, "unchecked", (
            f"PyPI could not be asked about {', '.join(f'`{k}`' for k in unasked)}: {failure}; whether they exist is "
            f"not known."), severity="error"))
    return CheckFindings(issues, to_weigh)


def _package_finding(p: _NewPackage, named: set[str], known: dict[str, PyPIProject | None],
                     now: float) -> tuple[bool, dict] | None:
    """Whether new_dependencies's finding on package p fails the gate, and the finding; None for a package of a lock
    file that comes from elsewhere than PyPI or that PyPI knows, which is described with the others the lock file
    adds, or one that PyPI was not asked about, which is unchecked."""
    member = p.source is not None and p.source.kind == "workspace"
    if p.direct and p.key not in named and not member:
        return True, _check_finding(p.path, p.line, "not-in-spec", (
            f"`{p.key}` is a new dependency that the spec does not name{_whence(p, known, now, '; ')}."),
            severity="error")
    if p.key in known and known[p.key] is None:
        return True, _check_finding(p.path, p.line, "not-on-pypi", (
            f"PyPI does not know `{p.key}`: the name may be mistyped, or name a package that does not exist yet, "
            f"which anyone could register (slopsquatting)."), severity="error")
    if p.direct and (p.source or p.key in known):
        return False, _check_finding(p.path, p.line, _added_rule(p, known.get(p.key), now), (
            f"`{p.key}`{', which the spec names' if p.key in named else ''}: {_whence(p, known, now)}."))
    return None


def _added_rule(p: _NewPackage, project: PyPIProject | None, now: float) -> str:
    """The rule of a direct dependency new_dependencies only reports."""
    if rule := {"index": "other-index", "unknown": "unknown-source"}.get(p.source.kind if p.source else ""):
        return rule
    return "young" if project and _young(project, now) else "added"


def spec_names(spec: str) -> set[str]:
    """The package_keys of the words in spec that could be a package's name, so a spec names a package when it holds
    the name as a word, in any case and with -, _ or . in any of their places; a longer name holding it does not."""
    return {package_key(w.rstrip("._-")) for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec)}


def _new_packages(change: RunChange) -> tuple[list[_NewPackage], list[dict], list[dict]]:
    """The packages the change adds to each DEPENDENCY_ECOSYSTEMS project whose manifest or lock file it touches,
    direct ones first; and an unreadable finding for each manifest that cannot be read, which leaves its project
    unchecked: failing when the change made it so, and to weigh when it cannot be read at the base either."""
    touched = {f.path for f in change.files} | {f.base_path for f in change.files}
    found: list[_NewPackage] = []
    issues, to_weigh = [], []

    # Read once, for the ecosystem and for the lines that name its packages.
    @functools.cache
    def new_text(path: str) -> str | None:
        return change.file_at(change.commit, path)

    for ecosystem in DEPENDENCY_ECOSYSTEMS:
        if ecosystem.manifest not in touched and ecosystem.lock_file not in touched:
            continue
        # Both sides at the top level, where the project's manifests are: a file the change moves there was not the
        # project's at the base.
        after = ecosystem.read(new_text)
        before = ecosystem.read(lambda path: change.file_at(change.base, path))
        # Each manifest by its own two sides: the Builder broke only what the base could read.
        issues += [_unreadable(path, why, "in the change", "error") for path, why in after.unreadable.items()
                   if path not in before.unreadable]
        to_weigh += [_unreadable(path, why, "at the base", "warning") for path, why in before.unreadable.items()]
        if not after.unreadable and not before.unreadable:
            added = change.changed.get(ecosystem.manifest, set())
            new, moved = _compare_dependencies(ecosystem, before, after, functools.partial(
                _line_naming, new_text(ecosystem.manifest) or "", added))
            found += new
            to_weigh += moved
    found.sort(key=lambda p: not p.direct)
    return found, issues, to_weigh


def _compare_dependencies(ecosystem: DependencyEcosystem, before: Dependencies, after: Dependencies,
                          line_naming: Callable[[str], int | None]) -> tuple[list[_NewPackage], list[dict]]:
    """The packages the project depends on after and not before, a package of its direct dependencies counted as one
    of them only; and a source-changed finding to weigh for each that it depended on before from another source.
    line_naming gives the added line of the manifest that names a package, by its key."""
    lock_only = {key: source for key, source in after.locked.items() if key not in after.direct}
    new = [_NewPackage(key, ecosystem.manifest, line_naming(key), True, source)
           for key, source in after.direct.items() if key not in before.direct]
    new += [_NewPackage(key, ecosystem.lock_file, None, False, source)
            for key, source in lock_only.items() if key not in before.locked]
    moved = []
    for path, was, now, line in ((ecosystem.manifest, before.direct, after.direct, line_naming),
                                 (ecosystem.lock_file, before.locked, lock_only, lambda key: None)):
        moved += [_check_finding(path, line(key), "source-changed", (
                      f"`{key}`: at the base, {_source_text(was[key])}; in the change, {_source_text(source)}."))
                  for key, source in now.items() if key in was and was[key] != source]
    return new, moved


def _source_text(source: PackageSource | None) -> str:
    return "from PyPI" if source is None else source.describe()


def _unreadable(path: str, why: str, where: str, severity: str) -> dict:
    return _check_finding(path, None, "unreadable", (f"`{path}` cannot be read {where}: {why}; the change's new "
                                                f"dependencies were not checked."), severity=severity)


def _line_naming(text: str, added: set[int], key: str) -> int | None:
    """The first of the added lines of text that names the package key, as spec_names reads a name: a ., - or _ that
    goes on into more of a name makes it another name, as zope.interface is not zope."""
    pattern = re.compile(r"(?<![A-Za-z0-9._-])" + "[-_.]+".join(map(re.escape, key.split("-")))
                         + r"(?![-_.]*[A-Za-z0-9])", re.IGNORECASE)
    lines = text.split("\n")
    return next((n for n in sorted(added) if n <= len(lines) and pattern.search(lines[n - 1])), None)


def _ask_pypi(packages: list[_NewPackage], pypi: "PyPI",
              in_time: Callable[[], bool]) -> tuple[dict[str, PyPIProject | None], str | None]:
    """What pypi knows of each package from PyPI, by key, None for one it does not know; and why the rest were not
    asked: in_time() said no before a request, or pypi gave no usable answer to one, which leaves that one and every
    later one out."""
    known: dict[str, PyPIProject | None] = {}
    for p in packages:
        if p.source is None and p.key not in known:
            if not in_time():
                return known, "the quality round had too little time left to ask"
            try:
                known[p.key] = pypi.project(p.key)
            except PyPIError as e:
                return known, str(e)
    return known, None


def _whence(p: _NewPackage, known: dict[str, PyPIProject | None], now: float, lead: str = "") -> str:
    """Where package p comes from, after lead: its source, or what PyPI knows of it; "" when PyPI was not asked."""
    if p.source:
        return f"{lead}{p.source.describe()}"
    if p.key not in known:
        return ""
    project = known[p.key]
    if project is None:
        return f"{lead}PyPI does not know it either"
    since = f"since {project.first_release:%Y-%m-%d}" if project.first_release else "with no release files"
    latest = f" of {project.latest_release:%Y-%m-%d}" if project.latest_release else ""
    young = f"; it is younger than {YOUNG_PACKAGE_DAYS} days, so few have used it yet" if _young(project, now) else ""
    return f"{lead}on PyPI {since}, latest {project.version}{latest}, license {project.license or 'not stated'}{young}"


def _young(project: PyPIProject, now: float) -> bool:
    """Whether the project's first release is under YOUNG_PACKAGE_DAYS days before now; one with none is."""
    return project.first_release is None or now - project.first_release.timestamp() < YOUNG_PACKAGE_DAYS * 86400


# -- Diff budget and unexpected paths --------------------------------------


def diff_budget(change: RunChange, *, max_files: int, max_lines: int) -> CheckFindings:
    """A finding to weigh when the change touches more than max_files files, or adds and removes more than max_lines
    lines between them; none otherwise."""
    added, removed = sum(f.added for f in change.files), sum(f.removed for f in change.files)
    over = []
    if len(change.files) > max_files:
        over.append(f"{len(change.files)} files, over the workflow's max_files of {max_files}")
    if added + removed > max_lines:
        whose = "its" if over else "the workflow's"
        over.append(f"{added + removed} lines ({added} added, {removed} removed), over {whose} max_lines of {max_lines}")
    if not over:
        return CheckFindings([], [])
    return CheckFindings([], [_check_finding(None, None, "", (
        f"The change touches {', and '.join(over)}. Say in your report why it cannot be smaller."))])


def unexpected_paths(change: RunChange) -> CheckFindings:
    """A finding to weigh for each path the change touches that the spec mentions neither as a whole nor by its file
    name. A rename is judged at its new path only, and a path or name counts as mentioned only where it appears whole
    in the spec, as a plain substring: naming a directory does not cover the files in it."""
    return CheckFindings([], [
        _check_finding(path, None, "", "the spec mentions neither this path nor its file name.")
        for path in sorted({f.path for f in change.files})
        if path not in change.spec and posixpath.basename(path) not in change.spec])


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
        return (f"**{QUALITY_GATE_FAILED}**: its workflow has no review step, and the quality gate still "
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


class QualityFile(NamedTuple):
    """A quality file of a run, as its commit and pull request cite it."""
    round: int  # the review round it belongs to
    quality_round: int
    text: str


class GateEvidence(NamedTuple):
    """What the quality gate said of the change a run publishes, for its commit trailers and pull request
    (provenance, pr_body); Workflow._gate_evidence decides it."""
    # The quality file they cite: the final round's last one, or for an unjudged change the run's last one from any
    # round; None when there is none to cite.
    last: QualityFile | None = None
    # True when resume --no-quality-gate turned the gate off and no quality file judges the published code
    # (Workflow._unjudged): last, if any, judged code the Builder has changed since.
    unjudged: bool = False


def provenance(state: "RunState", quality: GateEvidence) -> list[tuple[str, str]]:
    """The git trailers of the run's commit, (key, value) in order: Orchestrator-Run, Orchestrator-Workflow, an
    Orchestrator-Role per role whose agent ran, in the order they first started, as role=harness/model, the model
    "default" where none was set, and with a quality file to cite Orchestrator-Gate, how its quality round ended,
    with ", then turned off" for an unjudged change.

    A role that ran in two harnesses over resumes names the last. docs/design.md says what each trailer means.
    """
    s = state
    trailers = [("Orchestrator-Run", s.run_id), ("Orchestrator-Workflow", s.workflow)]
    trailers += [("Orchestrator-Role", f"{role}={agent.get('kind', DEFAULT_AGENT)}/{s.models.get(role) or 'default'}")
                 for role, agent in s.agents.items()]
    if (last := quality.last) is not None:
        q = last.quality_round
        rounds = f"{q} quality round{'s' if q != 1 else ''}"
        off = ", then turned off" if quality.unjudged else ""
        trailers.append(("Orchestrator-Gate", f"{parse_gate(last.text) or '?'} after {rounds}{off}"))
    return trailers


def conflict_warning(base_branch: str | None, conflicts: list[str]) -> str:
    """The paragraph that opens the body of a pull request opened over conflicts with base_branch in the files
    conflicts, which must not be empty; without_conflict_warning takes it out again."""
    files = "\n".join(f"> - `{f}`" for f in conflicts)
    return (f"> [!WARNING]\n> This branch conflicts with `{base_branch}`, so this is a draft. "
            f"Merge `{base_branch}` into it and resolve the conflicts in:\n{files}")


def without_conflict_warning(body: str, base_branch: str | None, conflicts: list[str]) -> str:
    """body with its first conflict_warning paragraph for base_branch and conflicts removed, and the blank line after
    it, or before it when it ends the body; body unchanged when it has no such paragraph. Its lines may end in \r\n,
    as GitHub's editor leaves them, and the paragraph's own lines must be as conflict_warning wrote them."""
    block = r"\r?\n".join(re.escape(line) for line in conflict_warning(base_branch, conflicts).split("\n"))
    paragraph = (rf"(?:\A|(?<=\n\n)|(?<=\n\r\n)){block}(?:\r?\n\r?\n|\r?\n?\Z)"
                 rf"|\r?\n\r?\n{block}(?=\r?\n?\Z)")
    return re.sub(paragraph, "", body, count=1)


def pr_body(state: "RunState", spec: str, report: str, review: str, quality: GateEvidence) -> str:
    """The pull request's body; quality is what the gate said of the change, as provenance takes it."""
    approved = state.verdict in SUCCEEDED
    head = f"Opened by Octoflow AI run `{state.run_id}`. {verdict_line(state)}"
    trailers = "\n".join(["| Commit trailer | Value |", "| --- | --- |",
                          *(f"| {key} | `{said}` |" for key, said in provenance(state, quality))])
    warning = [conflict_warning(state.base_branch, state.conflicts)] if state.conflicts else []
    note, section = [], []
    if quality.unjudged:
        when = ("after its last analysis, and the Builder changed the code since, so nothing judged the final change"
                if quality.last else "before any analysis finished, so nothing judged the change")
        note = [f"> [!NOTE]\n> The quality gate was turned off (`resume --no-quality-gate`) {when}."]
    if (last := quality.last) is not None:
        label = f"round {last.round}, before the last change" if quality.unjudged else f"round {last.round}"
        section = [_details(f"Quality gate ({label})", last.text)]
    return "\n\n".join([
        *warning,
        *note,
        head,
        trailers,
        _details("Spec", spec, open_=True),
        _details(f"Builder report (round {state.round})", report),
        *section,
        *([] if state.verdict in (FINISHED, QUALITY_GATE_FAILED) else
          [_details(f"Review (round {state.round})", review, open_=not approved)]),
        *([_details("Run timings", turns_table(state.turns))] if state.turns else []),
    ]) + "\n"


# -- Turn records: RunState.turns, as summary.md, the pull request and show print them ------------------------

def iso_utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def epoch_seconds(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def turn_cells(t: dict) -> dict[str, str]:
    """A turn record's values as the tables show them. A start or duration that is not known shows as ?: a file
    written before turns were recorded, or a turn still open. A quality analysis runs in no harness."""
    harness = t.get("agent")
    return {
        "step": t["step"],
        "role": t["role"],
        "round": f"{t['round']} q{t['quality_round']}" if t.get("quality_round") else str(t["round"]),
        "harness": harness or "-",
        "model": t.get("model") or ("default" if harness else "-"),
        "start": t["started_at"].replace("T", " ").removesuffix("+00:00") if t.get("started_at") else "?",
        "duration": "?" if t.get("seconds") is None else format_duration(t["seconds"]),
        "file": t["file"],
    }


def turns_table(turns: list[dict]) -> str:
    """The turns as a Markdown table, the human-paced ones marked: their time is the human's as much as the agent's."""
    head = ["step", "role", "round", "harness", "model", "start (UTC)", "duration", "file"]
    rows = []
    for t in turns:
        cells = turn_cells(t)
        if t.get("human_paced"):
            cells["step"] += " (human-paced)"
        rows.append([cells[k] for k in ("step", "role", "round", "harness", "model", "start", "duration", "file")])
    return "\n".join("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |"
                     for row in [head, ["---"] * len(head), *rows])


def turn_lines(turns: list[dict]) -> list[str]:
    """The turns as show prints them, a line each, in columns."""
    keys = ("step", "round", "harness", "start", "duration", "file")
    rows = [turn_cells(t) for t in turns]
    widths = {k: max((len(r[k]) for r in rows), default=0) for k in keys}
    return ["  ".join(f"{r[k]:<{widths[k]}}" for k in keys).rstrip() for r in rows]


def run_summary(state: "RunState", pipeline: Pipeline, rounds: list[str], now: int) -> str:
    """summary.md of a run that ended at now: how it turned out, who played each role, and where its time went.

    rounds holds a line per round, with what its handoff files say (Workflow._round_results).
    """
    s = state
    outcome = f"error: {s.error}" if s.error else s.verdict or "unfinished"
    starts = [epoch_seconds(t["started_at"]) for t in s.turns if t.get("started_at")]
    wall = format_duration(now - min(starts)) if starts else "?"
    roles = []
    for role, label in pipeline.roles.items():
        harness = (s.agents.get(role) or {}).get("kind") or s.agent_kinds.get(role, DEFAULT_AGENT)
        roles.append(f"| {label} ({role}) | {harness} | {s.models.get(role) or 'default'} |")
    lines = [
        f"# Run {s.run_id}",
        "",
        f"- **Workflow:** {s.workflow}",
        f"- **Outcome:** {outcome}",
        f"- **Pull request:** {s.pr_url or 'none'}",
        f"- **Branch:** {f'`{s.branch}`' if s.branch else 'none'}",
        f"- **Wall time:** {wall}, from the first turn's start to {iso_utc(now)}",
        "",
        "## Task",
        "",
        s.task.strip(),
        "",
        "## Roles",
        "",
        "| role | harness | model |",
        "| --- | --- | --- |",
        *roles,
        "",
        "## Turns",
        "",
        turns_table(s.turns) if s.turns else "No turns were recorded.",
    ]
    if rounds:
        lines += ["", "## Rounds", "", *(f"- {line}" for line in rounds)]
    return "\n".join(lines) + "\n"


# -- Run metrics: metrics.json, which the metrics command prints ---------------------------------------------

# The version of run_metrics' shape, raised when a key changes its meaning or goes.
METRICS_VERSION = 1

# Where each harness that records the tokens of a session on disk keeps that record, as a glob relative to the home
# directory of the machine the agents run on, {session} standing for the session id herdr reports. A harness that is
# not here records none the orchestrator reads, and its turns' tokens are "unknown" (docs/design.md, Token usage).
SESSION_LOGS = {"claude": ".claude/projects/*/{session}.jsonl", "pi": ".pi/agent/sessions/*/*_{session}.jsonl"}


def session_usage(kind: str, session_log: str) -> list[tuple[float, dict[str, int]]]:
    """The token usage a harness's session log, as SESSION_LOGS finds it, records: (when, in seconds since the epoch;
    {"input", "output", "cache_read", "cache_write"}) for each answer of the model, oldest first. A line that is not a
    record of one is skipped. Claude Code writes one line per content block of an answer, each with the answer's
    usage, so each answer counts once, at its first line."""
    out, seen = [], set()
    for line in session_log.split("\n"):
        try:
            entry = json.loads(line)
            message = entry["message"]
            usage = message["usage"]
            when = datetime.fromisoformat(entry["timestamp"]).timestamp()
            if kind == "claude":
                if entry.get("type") != "assistant" or message.get("id") in seen:
                    continue
                seen.add(message.get("id"))
                tokens = {"input": usage["input_tokens"], "output": usage["output_tokens"],
                          "cache_read": usage.get("cache_read_input_tokens", 0),
                          "cache_write": usage.get("cache_creation_input_tokens", 0)}
            else:
                if message.get("role") != "assistant":
                    continue
                tokens = {"input": usage["input"], "output": usage["output"], "cache_read": usage.get("cacheRead", 0),
                          "cache_write": usage.get("cacheWrite", 0)}
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if all(type(v) is int for v in tokens.values()):
            out.append((when, tokens))
    return out


def turn_tokens(turn: dict, usage: list[tuple[float, dict[str, int]]] | None) -> dict[str, int] | str:
    """The tokens of the answers the turn's agent gave from the turn's start to the second its handoff file was
    written, summed, from usage, session_usage of the session it ran in throughout; "unknown" when there is no such
    usage, or the turn's session or either end is not known, or not a time."""
    if usage is None or not turn.get("session") or not turn.get("started_at") or not turn.get("ended_at"):
        return "unknown"
    try:
        start, end = epoch_seconds(turn["started_at"]), epoch_seconds(turn["ended_at"]) + 1
    except (ValueError, TypeError):
        return "unknown"
    total = dict.fromkeys(("input", "output", "cache_read", "cache_write"), 0)
    for when, tokens in usage:
        if start <= when < end:
            for k, v in tokens.items():
                total[k] += v
    return total


def run_metrics(state: "RunState", now: int) -> dict:
    """metrics.json of a run that is done, at now: one object of the run's outcome, its roles, its turns with their
    times and tokens, and its quality rounds with their fingerprints, never their findings' text. docs/design.md
    gives every key."""
    s = state
    starts = [epoch_seconds(t["started_at"]) for t in s.turns if t.get("started_at")]
    turns = [{"step": t["step"], "role": t["role"], "harness": t.get("agent"), "model": t.get("model"),
              "round": t["round"], "quality_round": t.get("quality_round", 0),
              "human_paced": bool(t.get("human_paced")), "started_at": t.get("started_at"),
              "ended_at": t.get("ended_at"), "seconds": t.get("seconds"),
              "tokens": (t.get("tokens") or "unknown") if t.get("agent") else None}
             for t in s.turns]
    return {
        "version": METRICS_VERSION, "run_id": s.run_id, "workflow": s.workflow,
        "outcome": s.verdict or ("error" if s.error else "unfinished"), "rounds": s.round,
        "max_rounds": s.max_rounds, "quality_gate": s.had_quality_gate,
        "max_quality_rounds": s.max_quality_rounds if s.had_quality_gate else None, "pull_request": s.pr_url,
        "started_at": iso_utc(min(starts)) if starts else None, "ended_at": iso_utc(now),
        "seconds": now - min(starts) if starts else None,
        "roles": {role: {"harness": a.get("kind", DEFAULT_AGENT), "model": s.models.get(role)}
                  for role, a in s.agents.items()},
        "turns": turns, "quality_rounds": s.quality_rounds,
        "restored_locked_files": sum(len(ps) for r in s.restored for ps in r["paths"].values()),
    }


# -- Proofs: the verdict step's failing tests (Pipeline.proof_command) --------------------------------------

# The heading of the section _judge_proofs appends to a review, which a Reviewer's own section is unlikely to take.
PROOFS_HEADING = "## Proofs the orchestrator ran"
# Seconds one proof may run, and how many lines of its output the review quotes.
PROOF_TIMEOUT = 300
PROOF_TAIL_LINES = 10
# A shell script that removes the directory "$1" names and everything under it, if it exists, failing as rm does.
# Write permission is given back first, as far as chmod can: a proof may leave a directory it made read-only, as a
# test of a permission error does.
REMOVE_TREE_SCRIPT = '{ [ ! -e "$1" ] || chmod -R u+rwX -- "$1" 2>/dev/null; }; rm -rf -- "$1"'


def _proof_finding(file: str) -> int | None:
    """The number of the finding a proof file is for: the first number in its name, as in test_3_rate.py."""
    m = re.search(r"\d+", file)
    return int(m.group()) if m else None


def proof_written(proof: dict) -> int:
    """The round whose verdict step wrote the proof a RunState.proofs record is a run of."""
    return proof.get("written", proof["round"])


def _proof_passed(proof: dict) -> bool:
    """Whether the run a RunState.proofs record holds passed: exited 0, neither failing nor erroring."""
    return _proof_outcome(proof["said"]) == "it passes"


def _proof_outcome(said: str) -> str:
    """How a proof exited, from a record's said: "it fails", "it passes" or how it errored."""
    return said.split("\n", 1)[0].rstrip(":")


def _proof_output(said: str) -> list[str]:
    """The lines a proof printed last, from a record's said, fenced and indented under its list item; none when it
    printed nothing."""
    if "\n" not in said:
        return []
    return ["", "  ````", *(f"  {line}" for line in said.split("\n")[1:]), "  ````"]


def proofs_section(command: str, cwd: str, review: str, proofs: list[dict]) -> str:
    """The section _judge_proofs appends to a review: for each of proofs, the round's records in RunState.proofs,
    whether it demonstrates its finding, and the review's numbered findings left without one, which stand as
    unproven."""
    out = [PROOFS_HEADING, "", (f"The orchestrator ran each proof with `{command}` in {cwd}, from a copy of its "
                                f"directory under `{cwd}/{PROOF_COPIES_DIR}/`, removed after each run; a path to a "
                                "proof in its output is the copy's. A proof that fails demonstrates its finding; one "
                                "that passes or errors does not.")]
    for p in proofs:
        file, failed, said = p["file"], p["demonstrated"], p["said"]
        number = p["finding"]
        finding = f"finding {number}" if number is not None else "no finding"
        demonstrated = "demonstrated" if failed else "not demonstrated"
        out += ["", f"- `{file}` ({finding}): {demonstrated}, {_proof_outcome(said)}.", *_proof_output(said)]
    proven = {p["finding"] for p in proofs}
    unproven = [int(k) for k in re.findall(r"^(\d+)\. ", review, re.MULTILINE) if int(k) not in proven]
    if unproven:
        out += ["", f"Findings without a proof, unproven: {', '.join(map(str, unproven))}."]
    return "\n".join(out) + "\n"


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
                 spec: SpecFile | None = None,
                 pypi: PyPI | None = None):
        """pipeline is the run's workflow; without it, the one the state names.

        spec, from run --spec, is written as the contract step's file before the run starts, for a state
        that starts at the step after it (seeded_start). pypi is what the quality rounds' new-dependencies check
        asks; PyPI() without it.
        """
        self.herdr = herdr
        self.host = host
        self.state = state
        self.notify = notify
        self.ci = ci
        self.spec = spec
        self.pypi = pypi or PyPI()
        # Every prompt a workflow sends that its definition does not word: the follow-ups and notes.
        self.prompts = prompts = state.prompts
        self.pipeline = pipeline = pipeline or run_pipeline(state.workflow, state.workflow_definition, prompts)
        if ci and pipeline.gated is None:
            raise OrchestratorError(f"workflow {pipeline.name} has no quality-gated step, "
                                    f"so the quality gate does not apply")
        # Publishing commits on the run's branch, which only the first editing step creates.
        if pull_request and pipeline.first_edit is None:
            raise OrchestratorError(f"workflow {pipeline.name} has no editing step, "
                                    f"so it cannot end in a pull request; pass --no-pr")
        state.workflow = pipeline.name
        state.workflow_definition = None if is_built_in(pipeline, prompts) else workflow_definition(pipeline)
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
            self._clean_up_locks()
        except RunTakenOver:
            raise
        except OrchestratorError as e:
            self._release(str(e))
            raise
        except KeyboardInterrupt:
            self._release("interrupted")
            raise

        s.phase = DONE
        # Saved first: the run is done whatever counting the tokens meets.
        self._save()
        self._count_tokens()
        s.owner = None
        self._save()
        self._write_summary()
        self._write_metrics()
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
            self._check_before_interview()
        else:
            if s.worktree:
                self._check_worktree()
            self._check_branch()
            if self.ci and s.phase in (self.pipeline.gated.id, QUALITY):
                # Before a turn of the gated step that may take half an hour, not after it.
                self.ci.check_credentials()

    @contextlib.contextmanager
    def _rewrite_on_leak(self) -> Iterator[tuple[str, str]]:
        """The run's base and branch, for a scan of the branch at publish. A LeaksFound raised inside becomes the
        error that ends the run, still in phase publish: the commit holding the secret is already on the local
        branch, so fixing the working tree is not enough, and the error says how to rewrite it before a resume."""
        s = self.state
        if s.base is None or s.branch is None:
            raise OrchestratorError("a publish before the run has a base and a branch")
        try:
            yield s.base, s.branch
        except LeaksFound as e:
            raise OrchestratorError(
                f"{e}\nThose commits are on {s.branch} in {s.work_dir} and not on {REMOTE}. Rewrite them without the "
                f"secret, for example with `git reset --soft {s.base}` there, remove it from the files, and resume the "
                f"run: it stays in phase {PUBLISH} and commits again.") from e

    def _check_before_interview(self) -> None:
        """Open the run's worktree, if it has one, and fail if the run could not push what it makes, run its quality
        gate or open its pull request, before the Spec Collector's first turn."""
        s = self.state
        if s.worktree:
            self._open_worktree()
        if s.pull_request or self.ci:
            self.host.check_gitleaks()
        if self.ci:
            self._check_gate()
        if s.pull_request and not s.worktree:
            self._check_repo()

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
        self._ignore_orchestrator_dir(s.cwd)
        if self.spec:
            self._seed_spec()
        s.error = None
        s.owner = self.me
        # Not _save: the caller has already decided that any earlier owner is gone.
        self._write_state()

    def _ignore_orchestrator_dir(self, tree: str) -> None:
        """Make git ignore everything under the .orchestrator directory of the working tree at tree, by a .gitignore
        of `*` there, unless it has a .gitignore already. Keeps run files and proof copies out of `git status`, so
        the Reviewer sees only the Builder's changes and the commit holds nothing else."""
        ignore = f"{tree}/.orchestrator/.gitignore"
        if self.host.read(ignore) is None:
            self.host.write(ignore, "*\n")

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
            if isinstance(e, RunTakenOver):
                return  # the run is another orchestrator's now, and so is its summary
        self._write_summary()

    def _write_summary(self) -> None:
        """Write summary.md for a run that just ended; a failure costs only the summary, so it is only logged."""
        s = self.state
        try:
            self.host.write(f"{s.dir}/{SUMMARY_FILE}",
                            run_summary(s, self.pipeline, self._round_results(), self.host.now()))
        except OrchestratorError as e:
            log(f"could not write {SUMMARY_FILE}: {e}")

    def _write_metrics(self) -> None:
        """Write metrics.json for a run that is done; as for the summary, a failure is only logged."""
        s = self.state
        try:
            self.host.write(f"{s.dir}/{METRICS_FILE}", json.dumps(run_metrics(s, self.host.now())) + "\n")
        except OrchestratorError as e:
            log(f"could not write {METRICS_FILE}: {e}")

    def _count_tokens(self) -> None:
        """Give each agent turn the tokens its harness recorded for it (turn_tokens), "unknown" where it recorded
        none the orchestrator can read. A session log that cannot be read costs only its turns' counts."""
        usage: dict[tuple[str, str], list[tuple[float, dict[str, int]]] | None] = {}
        for t in self.state.turns:
            if not t.get("agent"):
                continue
            key = (t["agent"], t.get("session") or "")
            if key not in usage:
                usage[key] = None
                try:
                    if key[1] and (session_log := self.host.session_log(*key)) is not None:
                        usage[key] = session_usage(key[0], session_log)
                except OrchestratorError as e:
                    log(f"could not read the {key[0]} session log of {key[1]}: {e}")
            t["tokens"] = turn_tokens(t, usage[key])

    def _round_results(self) -> list[str]:
        """Per round, its quality gates and its verdict, as its handoff files say."""
        s, verdict_step = self.state, self.pipeline.verdict_step
        lines = []
        for n in range(1, s.round + 1):
            parts = []
            gates, q = [], 1
            while s.had_quality_gate and (text := self.host.read(s.quality_path(n, q))) is not None:
                gates.append(parse_gate(text) or "?")
                q += 1
            if gates:
                parts.append(f"quality gate {', '.join(gates)}")
            if verdict_step and (text := self.host.read(verdict_step.path(s.dir, n))) is not None:
                parts.append(f"{verdict_step.id} {parse_verdict(text) or '?'}")
            lines.append(f"Round {n}: {'; '.join(parts) or 'no verdict'}")
        return lines

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
            require_origin(self.host, s.cwd)
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
        if step.loop_to is not None:
            self._rerun_proofs(step, n)
        out = self._step_turn(step, n)
        if step.edits:
            # Before the quality round, so the gate judges the locked files.
            self._hold_locks(step, n)
        if step.quality_gated and (self.ci or s.quality_round):
            # The report goes to the next quality round. A round whose gate resume turned off since its last
            # quality round never analyses it, and the next step reads the report all the same (Step.last_path).
            s.quality_round += 1
            if self.ci:
                s.phase = QUALITY
                self._save()
                return False
        if step.loop_to is None:
            return True
        s.verdict = parse_verdict(out)  # _turn accepts no verdict file without one
        log(f"[{step.role}] round {n}: {s.verdict}")
        if s.verdict == CHANGES_REQUESTED and self.pipeline.proof_command:
            self._judge_proofs(step, n, out)
        if s.verdict != APPROVE and n < s.max_rounds:
            s.quality_round = 0
            self._goto(self.pipeline.step(step.loop_to), n + 1)
            return False
        return True

    def _hold_locks(self, step: Step, n: int) -> None:
        """After the editing step's turn in round n: restore the files other steps locked that the turn changed, then
        lock the step's own files if it locks them. A resume before the run moves on does it again, harmlessly."""
        s = self.state
        file = f"locked-{os.path.splitext(os.path.basename(self._own_path(step, n)))[0]}.diff"
        record = next((r for r in s.restored if r["file"] == file), None)
        if record is None:
            record = self._locked_changes(step, n, file)
        # A saved record is restored again: an orchestrator may have stopped before or while restoring it.
        for locker, paths in (record or {}).get("paths", {}).items():
            self.host.restore(s.work_dir, s.lock_ref(locker), paths)
            log(f"[{step.role}] restored {', '.join(paths)}, which step {locker} locked; the changes are in {file}")
        if step.locks:
            if s.base is None:
                raise OrchestratorError("a lock before the run has a base")
            s.locked[step.id] = self.host.lock(s.work_dir, s.base, s.lock_ref(step.id))
            self._save()
            log(f"[{step.role}] locked {len(s.locked[step.id])} files for the steps after it")

    def _locked_changes(self, step: Step, n: int, file: str) -> dict | None:
        """The record of what the step's turn in round n changed in other steps' locked files, saved with its diff
        written to file; None when it changed none of them."""
        s = self.state
        paths: dict[str, list[str]] = {}
        diffs = []
        for locker, locked in s.locked.items():
            if locker == step.id:
                continue
            differ, diff = self.host.locked_changes(s.work_dir, s.lock_ref(locker), locked)
            if differ:
                paths[locker] = differ
                diffs.append(diff)
        if not paths:
            return None
        self.host.write(f"{s.dir}/{file}", "".join(diffs))
        record = {"round": n, "file": file, "paths": paths}
        s.restored.append(record)
        self._save()
        return record

    def _locks_note(self, n: int) -> str:
        """What the verdict step hears of the locked files restored in round n; "" when none was."""
        s = self.state
        records = [r for r in s.restored if r["round"] == n]
        if not records:
            return ""
        paths = sorted({p for r in records for ps in r["paths"].values() for p in ps})
        lockers = sorted({locker for r in records for locker in r["paths"]})
        return "\n\n" + self.prompts.LOCKED_NOTE.format(
            paths=", ".join(f"`{p}`" for p in paths), steps=", ".join(lockers),
            diff_paths=", ".join(f"{s.dir}/{r['file']}" for r in records))

    def _clean_up_locks(self) -> None:
        """At the end of a run, delete its local refs; a failure is only logged."""
        s = self.state
        if not s.locked:
            return
        try:
            self.host.delete_refs(s.cwd, s.refs_prefix)
        except OrchestratorError as e:
            log(f"could not delete the refs under {s.refs_prefix}: {e}")

    def _judge_proofs(self, step: Step, n: int, review: str) -> None:
        """Run each proof the verdict step wrote in round n with the workflow's proof_command, record which fail, and
        append their outcomes to its review. A resume runs none that were recorded, and appends them once."""
        s, command = self.state, self.pipeline.proof_command
        if command is None:
            return
        records = [p for p in s.proofs if p["round"] == n and proof_written(p) == n]
        if not records:
            for f in sorted(os.path.basename(f.path) for f in self.host.run_files(s.proofs_dir(n))):
                failed, said = self._run_proof(command, n, f)
                records.append({"round": n, "written": n, "file": f, "finding": _proof_finding(f),
                                "demonstrated": failed, "said": said})
            if not records:
                return
            s.proofs += records
            self._save()
            log(f"[{step.role}] {sum(p['demonstrated'] for p in records)} of {len(records)} proofs fail against the "
                f"change")
        if PROOFS_HEADING not in review.split("\n"):
            self.host.write(step.path(s.dir, n),
                            review.rstrip("\n") + "\n\n" + proofs_section(command, s.work_dir, review, records))

    def _rerun_proofs(self, step: Step, n: int) -> None:
        """Before the verdict step's turn of round n, run each proof carried into it from an earlier round
        (_carried_proofs) again against the change, and record the outcomes in RunState.proofs. A resume runs none
        once round n's reruns are recorded."""
        s, command = self.state, self.pipeline.proof_command
        if command is None or self._reruns(n):
            return
        records = []
        for p in self._carried_proofs(n):
            failed, said = self._run_proof(command, proof_written(p), p["file"])
            records.append({"round": n, "written": proof_written(p), "file": p["file"], "finding": p["finding"],
                            "demonstrated": failed, "said": said})
        if not records:
            return
        s.proofs += records
        self._save()
        log(f"[{step.role}] {sum(p['demonstrated'] for p in records)} of {len(records)} proofs from earlier rounds "
            f"still fail against the change")

    def _carried_proofs(self, n: int) -> list[dict]:
        """The latest record before round n of each proof carried into it, in the order the proofs first ran: one
        that failed when last run, or that a rerun found erroring rather than passing. A proof whose first run, in the
        round that wrote it, errored is not carried: it never demonstrated its finding."""
        latest: dict[tuple[int, str], dict] = {}
        for p in self.state.proofs:
            if p["round"] < n:
                latest[proof_written(p), p["file"]] = p
        return [p for p in latest.values()
                if p["demonstrated"] or (proof_written(p) < p["round"] and not _proof_passed(p))]

    def _reruns(self, n: int) -> list[dict]:
        """The records of the proofs carried into round n and run again before its verdict step's turn."""
        return [p for p in self.state.proofs if p["round"] == n and proof_written(p) < n]

    def _proof_path(self, proof: dict) -> str:
        """Where the proof a RunState.proofs record is a run of lies: in the proofs directory of the round that wrote
        it."""
        return f"{self.state.proofs_dir(proof_written(proof))}/{proof['file']}"

    def _carried_proofs_note(self, n: int) -> str:
        """What the verdict step hears in round n of the proofs from earlier rounds run again before its turn; ""
        when none was."""
        s, command = self.state, self.pipeline.proof_command
        records = self._reruns(n)
        if command is None or not records:
            return ""
        items = []
        for p in records:
            passed = _proof_passed(p)
            how = "still fails" if p["demonstrated"] else "now passes" if passed else "no longer fails but errors"
            output = [] if passed else _proof_output(p["said"])
            said = "" if passed else f", {_proof_outcome(p['said'])}"
            items.append("\n".join([f"- `{self._proof_path(p)}`: {how}{said}.", *output]))
        return "\n\n" + self.prompts.CARRIED_PROOFS_NOTE.format(command=command, cwd=s.work_dir, proofs="\n\n".join(items))

    def _run_proof(self, command: str, written: int, file: str) -> tuple[bool, str]:
        """Whether the proof file that round written's verdict step wrote fails against the working tree, as command's
        exit status 1 says, and what it printed last: its output's end, how else it exited, or why it could not run.

        The proof runs from a copy of proofs_dir(written), with the files beside it, at
        <work_dir>/PROOF_COPIES_DIR/<written>: command gets the copy's path, and runs in the working tree's top
        directory. A tool that looks upwards from the proof for its configuration, as pytest does for its rootdir,
        conftest.py files and pythonpath, so meets the working tree's before any of the checkout's, which holds the
        run directory and encloses a run's worktree. The proof's output names the copy. The working tree's
        .orchestrator directory is made ignored first, so no commit holds the copy; PROOF_COPIES_DIR is emptied
        before the copy is made and removed after the proof, so what a proof writes beside itself goes with it.
        """
        s = self.state
        copies = f"{s.work_dir}/{PROOF_COPIES_DIR}"
        copy = f"{copies}/{written}"
        cmd = command.format(proofs=shlex.quote(f"{copy}/{file}"))
        # GNU timeout runs the proof in a process group of its own and kills all of it, so nothing a proof started
        # goes on writing to the tree; where there is none, Host.run's limit stops only the shell.
        script = ('cd -- "$1" || exit 125; command -v timeout >/dev/null 2>&1 && exec timeout -k 10 "$3" sh -c "$2"; '
                  'eval "$2"')
        try:
            self._ignore_orchestrator_dir(s.work_dir)
            # Emptied first: an orchestrator stopped during a proof leaves its copy behind. A proof file that is gone
            # from its directory, as one the Builder moved into the project is, is not run on a missing path.
            prepare = ('[ -f "$3/$4" ] || { echo "$3/$4 is gone" >&2; exit 1; }; '
                       f'{REMOVE_TREE_SCRIPT} && mkdir -p -- "$2" && cp -R -- "$3/." "$2"')
            self.host.check(["sh", "-c", prepare, "_", copies, copy, s.proofs_dir(written), file])
            proc = self.host.run(["sh", "-c", script, "_", s.work_dir, cmd, str(PROOF_TIMEOUT)],
                                 timeout=PROOF_TIMEOUT + 30, verbatim=True)
        except OrchestratorError as e:
            return False, f"it did not run: {e}"
        finally:
            self._remove_proof_copies(copies)
        tail = "\n".join((proc.stdout + proc.stderr).rstrip().split("\n")[-PROOF_TAIL_LINES:])
        how = {0: "it passes", 1: "it fails", 124: f"it errors, running out of its {PROOF_TIMEOUT} s"}.get(
            proc.returncode, f"it errors, with exit status {proc.returncode}")
        return proc.returncode == 1, f"{how}:\n{tail}" if tail else how

    def _remove_proof_copies(self, copies: str) -> None:
        """Remove the directory copies and everything under it; a failure is only logged, since the next proof's run
        empties it again and git ignores it meanwhile."""
        try:
            self.host.check(["sh", "-c", REMOVE_TREE_SCRIPT, "_", copies])
        except OrchestratorError as e:
            log(f"could not remove the proof copies in {copies}: {e}")

    def _proofs_note(self, step: Step, n: int) -> str:
        """What the step hears in round n of the proofs carried into it (_carried_proofs); "" when it is not the step
        the verdict step loops back to, or none is carried."""
        verdict = self.pipeline.verdict_step
        if verdict is None or verdict.loop_to != step.id:
            return ""
        failing = [self._proof_path(p) for p in self._carried_proofs(n)]
        if not failing:
            return ""
        return "\n\n" + self.prompts.PROOFS_NOTE.format(proofs=", ".join(failing))

    def _judge_quality(self, n: int) -> bool:
        """The current quality round of round n; False when its gate sends the change back to the gated step: when it
        failed, and not on just what the quality round before failed on (_stuck)."""
        s, gated = self.state, self.pipeline.gated
        if gated is None:
            raise OrchestratorError(f"a quality round in workflow {self.pipeline.name}, which has no quality-gated step")
        q = s.quality_round
        gate = self._quality(n, q)
        # As for review rounds: a saved round beyond a lowered limit ends here too.
        if gate == QUALITY_OK or q >= s.max_quality_rounds:
            return True
        if self._stuck(n, q):
            log(f"[quality] round {n}, quality round {q} failed on just what quality round {q - 1} failed on; "
                f"going on rather than back to {gated.id}")
            return True
        s.phase = gated.id
        self._save()
        return False

    def _stuck(self, n: int, q: int) -> bool:
        """Whether quality round q of round n failed on the failures of quality round q-1 again (GateFailures.repeats):
        the gated step's turn between them fixed none of them."""
        if q < 2:
            return False
        before, after = (self.host.read(self.state.quality_path(n, k)) for k in (q - 1, q))
        return before is not None and after is not None and gate_failures(after).repeats(gate_failures(before))

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
            return self._turn(step.role, text, path, s.turn_timeout, fresh_text=fresh, verdict=verdict,
                              blocks=step.can_block)
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
        before it; each ends with BLOCKED_NOTE when the step can block."""
        text, fresh = self._step_prompts(step, n)
        if not step.can_block:
            return text, fresh
        # Formatted with nothing, as each prompt is a template (Prompts): an override may write a brace as {{.
        note = self.prompts.BLOCKED_NOTE.format()
        return f"{text}\n\n{note}", fresh and f"{fresh}\n\n{note}"

    def _step_prompts(self, step: Step, n: int) -> tuple[str, str | None]:
        """_prompts as the step's definition words them."""
        s = self.state
        q = s.quality_round if step.quality_gated else 0
        fill = {name: self._placeholder(name, step, n) for name in step.placeholders()}
        quality = (self._quality_note(n) + self._locks_note(n) + self._carried_proofs_note(n)
                   if step.loop_to is not None else "")
        first = step.prompt.format(**fill) + quality
        if q:
            follow = self.prompts.QUALITY_FIX_PROMPT.format(quality_path=s.quality_path(n, q),
                                                            report_path=self._own_path(step, n))
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
        # Last, so a fresh session hears it too, whatever its prompt repeats.
        proofs = "" if q else self._proofs_note(step, n)
        return follow + proofs, "\n\n".join(fresh) + proofs

    def _placeholder(self, name: str, step: Step, n: int) -> str:
        """The value of a prompt placeholder (see Step) in the step's turn in round n."""
        s = self.state
        fixed = {"task": s.task, "cwd": s.work_dir, "n": str(n), "approve": APPROVE, "changes": CHANGES_REQUESTED,
                 "proofs_dir": s.proofs_dir(n)}
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
        while s.had_quality_gate and step.quality_gated and \
                self.host.read(step.path(s.dir, n, len(reports))) is not None:
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
            quality = self._gate_evidence(s.round)
            if self.host.git(wd, "status", "--porcelain").strip():
                self.host.git(wd, "add", "--all")
                trailers = [arg for key, said in provenance(s, quality) for arg in ("--trailer", f"{key}: {said}")]
                self.host.git(wd, "commit", "--quiet", "-m", title, "-m", commit_note(s), *trailers)
            elif self.host.git_head(wd) == s.base:
                raise OrchestratorError(f"the Builder changed no files; there is nothing to commit on {s.branch}")
            # Scanned before the merge too, so that rewriting the run's commits never has to undo a merge.
            with self._rewrite_on_leak() as (base, branch):
                self.host.refuse_leaks(wd, f"refs/heads/{branch}", base=base, report_path=s.leaks_path("publish"))
            # The base moved on while the run built and reviewed. A conflict is left to the human:
            # the branch is pushed without the merge and the pull request says what conflicts.
            s.conflicts = self.host.merge_upstream(wd, s.base_branch)
            self._save()
            if s.conflicts:
                log(f"{s.branch} conflicts with {REMOTE}/{s.base_branch} in {', '.join(s.conflicts)}; "
                    f"the pull request will be a draft")
            with self._rewrite_on_leak() as (base, branch):
                self.host.push_branch(wd, branch, base=base, report_path=s.leaks_path("publish"))
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
        """What the verdict step hears of round n's last quality file: passed, or the findings left, or for an unjudged
        change (_unjudged), QUALITY_OFF_NOTE; "" without one."""
        s = self.state
        last = self._last_quality(n)
        if last is None:
            return ""
        q, text = last
        if self._unjudged(last):
            return "\n\n" + self.prompts.QUALITY_OFF_NOTE.format(
                quality_paths=", ".join(s.quality_path(n, k) for k in range(1, q + 1)))
        if parse_gate(text) == QUALITY_OK:
            note = self.prompts.QUALITY_PASSED_NOTE
        else:
            note = self.prompts.QUALITY_STUCK_NOTE if self._stuck(n, q) else self.prompts.QUALITY_UNRESOLVED_NOTE
        return "\n\n" + note.format(quality_path=s.quality_path(n, q))

    def _last_quality(self, n: int) -> tuple[int, str] | None:
        """Round n's last quality file that is written, as (its quality round, its text); None when there is none.

        It is the current quality round's, except when resume --no-quality-gate turned the gate off before that
        round's analysis finished, or in the gated step's turn after it (Workflow._take_turn): the one before then.
        """
        s = self.state
        for q in (s.quality_round, s.quality_round - 1):
            if q > 0 and (text := self.host.read(s.quality_path(n, q))) is not None:
                return q, text
        return None

    def _unjudged(self, last: tuple[int, str] | None) -> bool:
        """Whether the current round's change went unjudged: resume --no-quality-gate turned the gate off, and last,
        the round's last quality file as _last_quality gives it, is older than the Builder's latest report in the
        round, or is None: the gate was turned off before the round's first analysis finished, or in an earlier
        round."""
        s = self.state
        return s.quality_gate_disabled and (last is None or last[0] < s.quality_round)

    def _gate_evidence(self, n: int) -> GateEvidence:
        """What the quality gate said of round n's change, the run's final one: round n's last quality file, or for
        an unjudged change (_unjudged) the run's last one from any round."""
        s = self.state
        last = self._last_quality(n)
        unjudged = self._unjudged(last)
        if last is not None or not unjudged:
            return GateEvidence(None if last is None else QualityFile(n, *last), unjudged)
        # Round n has none, and quality_round no longer says how far an earlier round got: its quality files are
        # numbered from 1 without gaps, so its last is the one before the first missing.
        for m in range(n - 1, 0, -1):
            found, q = None, 1
            while (text := self.host.read(s.quality_path(m, q))) is not None:
                found, q = QualityFile(m, q, text), q + 1
            if found is not None:
                return GateEvidence(found, unjudged=True)
        return GateEvidence(unjudged=True)

    def _gate_passed(self, n: int) -> bool:
        """Whether round n's last quality round passed the gate; True for a run without one."""
        s = self.state
        if not self.ci or not s.quality_round:
            return True
        # With the gate on, the quality loop has written the current quality round's file by now; one missing fails.
        last = self._last_quality(n)
        return last is not None and last[0] == s.quality_round and parse_gate(last[1]) == QUALITY_OK

    # -- The quality gate --------------------------------------------------

    @property
    def _jenkins(self) -> CI:
        """The run's CI. Only a run with the quality gate has one, and only its quality rounds ask for it."""
        if self.ci is None:
            raise OrchestratorError("a quality round in a run without the quality gate")
        return self.ci

    def _quality(self, n: int, q: int) -> str:
        """Quality round q of round n: have Jenkins judge the change, write quality-<n>-<q>.md, and return its gate.
        A snapshot gitleaks finds a secret in is not pushed, and its quality file is QUALITY_LEAK.

        Each step first looks for what an earlier orchestrator already did, furthest first: the
        quality file; then, in _analyse, the chain in ci.
        """
        s = self.state
        path = s.quality_path(n, q)
        ref = f"{CI_REF_PREFIX}{s.key}-{n}-q{q}"
        if (text := self.host.read(path)) is not None:
            log(f"[quality] {os.path.basename(path)} is already written")
        else:
            if not self._open_turn(os.path.basename(path)):
                s.turns.append(self._turn_record(QUALITY, os.path.basename(path), self.host.now()))
                self._save()
            # Per round, and afresh on a resume.
            deadline = self.clock() + QUALITY_TIMEOUT
            message = f"Orchestrator run {s.run_id}: round {n}, quality round {q}"
            base = s.base
            if base is None:
                raise OrchestratorError("a quality round before the run has a base")
            try:
                build, result, quality = self._analyse(
                    ref, deadline, base, lambda: self.host.snapshot(s.work_dir, base, message))
            except LeaksFound as e:
                text = self._leak_report(n, q, e)
            else:
                text = self._jenkins.mask(self._quality_report(n, q, build, result, quality, deadline))
            self.host.write(path, text)
        self._end_turn(QUALITY, path)
        gate = parse_gate(text)
        if gate is None:
            self._reject(path, "does not start with a GATE line")
        if not any(r["round"] == n and r["quality_round"] == q for r in s.quality_rounds):
            failures = gate_failures(text)
            s.quality_rounds.append({"round": n, "quality_round": q, "gate": gate, "conditions": failures.conditions,
                                     "fingerprints_by_check": failures.fingerprints})
        log(f"[quality] round {n}, quality round {q}: {gate}")
        self._finish_analysis(ref)
        return gate

    def _analyse(self, ref: str, deadline: float, base: str, commit) -> tuple[int, str | None, str | None]:
        """Have Jenkins judge a commit; returns (build, its result, the quality file it archived), as soon as the
        build has archived its quality file or has ended, whichever comes first.

        The result is None when the build was still running the stages after its Gate stage: they cannot change the
        verdict, which the quality file already holds, so the round does not wait for them. It is never None
        without a quality file.

        commit() makes the commit, whose only parent is base, which is pushed to the branch ref once gitleaks, by base's
        rules, finds no secret in it or in the commits below it that origin lacks; on a secret, nothing is pushed or
        triggered and ci is left empty, and it raises LeaksFound when every secret is in that commit, else
        OrchestratorError, since the others are the human's. The quality file is None when
        the build ended before its Gate stage. Each link of the chain is saved as soon as it is
        known, so a resume picks it up from the furthest one; a ce_task a SonarQube-era orchestrator
        saved is ignored, and its build, which archived no quality file, counts as one that failed.
        """
        s = self.state
        fresh = s.ci.get("ref") != ref
        if fresh:
            if s.ci.get("ref"):
                delete_ci_ref(self.host, s.cwd, s.ci["ref"])
                s.ci = {}
            sha = commit()
            try:
                self.host.push_ref(s.work_dir, sha, ref, base=base, report_path=s.leaks_path("snapshot"))
            except LeaksFound as e:
                # The snapshot's only parent is base, so a secret in any other commit is in base or below it: the
                # human's history, which the Builder cannot rewrite.
                if any(leak.commit != sha for leak in e.leaks):
                    raise OrchestratorError(
                        f"{e}\nThose not in snapshot {sha[:12]} are in the run's base {base[:12]} or below it, which "
                        f"{REMOTE} does not have: the human's commits, not the Builder's to fix. Rewrite them without "
                        f"the secret, or list a finding that is not one in .gitleaksignore there, and start a new "
                        f"run.") from e
                raise
            s.ci = {"ref": ref, "sha": sha}
            self._save()
        if "build" not in s.ci:
            self._start_build(ref, deadline, adopt=not fresh)
        number = s.ci["build"]
        result, quality = self._poll(f"waiting for Jenkins build #{number} of {ref} to archive quality.md or end",
                                     deadline, self._gate_or_end(ref, number))
        if quality is None and result == "ABORTED":
            self._cut_back(number)
            raise OrchestratorError(f"Jenkins build #{number} of {ref} was aborted before the quality gate ran, so "
                                    f"nothing judged the change; resume to build it again")
        return number, result, quality

    def _gate_or_end(self, ref: str, number: int):
        """A poll for _poll: (result, quality file) of build number, which ci records as the build of ref, once it
        has ended, or (None, quality file) once two reads of the file it archived while it still runs agree and hold
        a whole non-blank line; None until then.

        Each call asks for the result (_build_result, and its errors) and then the file, two requests at most.
        """
        # The last read of the file while the build ran; None before one found it.
        earlier: str | None = None

        def poll() -> tuple[str | None, str | None] | None:
            nonlocal earlier
            result = self._build_result(ref, number)
            quality = self._jenkins.quality_file(number)
            if result is not None:
                return result, quality
            # Jenkins serves an artifact while archiveArtifacts is still copying it into place, so one read of a
            # running build's file may be cut short. Two reads that agree, at least a poll interval apart, and hold a
            # whole non-blank line, as the GATE line is, are taken as whole: so an empty copy or one cut inside its
            # first line is not, while a whole first line that is no GATE line is, for _quality to report. A
            # heuristic, which a copy stalled that long past its first line and then resumed defeats (TODO.md). A
            # build that has ended has finished archiving, so one read then is enough.
            if quality is not None and quality == earlier and any(line.strip() for line in quality.split("\n")[:-1]):
                return None, quality
            earlier = quality
            return None

        return poll

    def _start_build(self, ref: str, deadline: float, adopt: bool) -> None:
        """Record in ci the build of ref: one triggered now, or with adopt one an earlier orchestrator triggered."""
        s = self.state
        if "queue_url" not in s.ci:
            # The ref names one round of one run, so a build of it is this round's, triggered by an
            # orchestrator that stopped before it recorded the queue item.
            found = self._once(f"looking for a Jenkins build of {ref}", deadline,
                               lambda: self._jenkins.find_build(ref, s.ci.get("rejected", ()))) if adopt else None
            if found is not None:
                log(f"[quality] adopting Jenkins build #{found} of {ref}")
                s.ci["build"] = found
                self._save()
                return
            s.ci["queue_url"] = self._once(f"triggering Jenkins job {self._jenkins.job} for {ref}", deadline,
                                           lambda: self._jenkins.trigger(ref))
            self._save()
        s.ci["build"] = self._poll(f"waiting for Jenkins to start the build of {ref}", deadline,
                                   lambda: self._queued_build(ref))
        self._save()

    def _queued_build(self, ref: str) -> int | None:
        s = self.state
        queue_url = s.ci["queue_url"]
        try:
            item = self._jenkins.queue_item(queue_url)
        except CIError as e:
            if e.status != 404:
                raise
            # Jenkins forgets a queue item some minutes after its build started.
            found = self._jenkins.find_build(ref, s.ci.get("rejected", ()))
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

    def _build_result(self, ref: str, number: int) -> str | None:
        """build_result of number, which ci records as the build of ref; a build Jenkins no longer has fails the run,
        cut back so that a resume builds ref again."""
        try:
            return self._jenkins.build_result(number)
        except CIError as e:
            if e.status != 404:
                raise
            # The Jenkinsfile's buildDiscarder drops the job's oldest builds, which a run stopped mid-round and resumed
            # much later can still name.
            self._cut_back()
            raise OrchestratorError(f"Jenkins build #{number} of {ref} is gone, discarded by the job; resume to "
                                    f"build it again") from e

    def _cut_back(self, rejected: int | None = None) -> None:
        """Keep only the pushed commit in ci, so a resume builds it anew, never adopting the rejected build."""
        ci = self.state.ci
        skip = [*ci.get("rejected", []), *([rejected] if rejected is not None else [])]
        self.state.ci = {"ref": ci["ref"], "sha": ci["sha"], **({"rejected": skip} if skip else {})}

    def _quality_report(self, n: int, q: int, build: int, result: str | None, quality: str | None,
                        deadline: float) -> str:
        """The quality file of the round: the build's own, naming the build under its GATE line; or, for a build
        that ended before its Gate stage, a BUILD_FAILED one with the failing tests or the console's end.

        result and quality are as _analyse returns them: result None for a build still running after its Gate stage.
        """
        s = self.state
        sha = s.ci["sha"]
        where = self._where(n, q)
        if quality is not None:
            how_it_stood = "was still running when its quality.md was read" if result is None else f"ended {result}"
            return _under_gate_line(self._judged_by_orchestrator(quality, sha, deadline),
                                    f"Jenkins build #{build} judged snapshot `{sha}` ({where}) and {how_it_stood}.")
        tests = self._once(f"reading the test report of Jenkins build #{build}", deadline,
                           lambda: self._jenkins.failed_tests(build))
        console = "" if tests else self._once(f"reading the console of Jenkins build #{build}", deadline,
                                              lambda: self._jenkins.console_tail(build))
        intro = (f"Jenkins build #{build} of snapshot `{sha}` ({where}) ended {result} before the quality gate "
                 f"ran, so nothing judged the change. Make it build first.")
        return build_failed_report(intro, tests, console)

    def _where(self, n: int, q: int) -> str:
        """Quality round q of round n, as a quality file's intro names it."""
        return f"round {n}, quality round {q} of {self.state.max_quality_rounds}"

    def _leak_report(self, n: int, q: int, found: LeaksFound) -> str:
        """The quality file of quality round q of round n, whose snapshot was not pushed for the secrets found."""
        intro = (f"{found.found()} in the snapshot of {self._where(n, q)}, so it was not pushed and Jenkins did not "
                 f"judge the change. Take each secret below out of the change; a comment telling gitleaks to allow "
                 f"its line counts as one too. If one is not a secret, say so in your report: only the human can let "
                 f"it through, and not in this run, which keeps the rules of its base: listed as `file:rule:line` in "
                 f".gitleaksignore on the base branch, it passes in a run started after that.")
        return leak_report(found.leaks, intro)

    def _judged_by_orchestrator(self, quality: str, sha: str, deadline: float) -> str:
        """The build's quality file with what orchestrator_checks find in snapshot sha added (judge_run), their slow
        steps kept to the round's deadline, a clock() time."""
        s = self.state
        base = s.base
        if base is None:
            raise OrchestratorError("a quality round before the run has a base")
        # A workflow without a contract step has only its task to say what the change is for.
        spec = self._last_text(self.pipeline.contract, 0) or s.task
        added = added_lines(self.host.change_diff(s.work_dir, base, sha))
        change = RunChange(spec, base, sha, {path: set(lines) for path, lines in added.items()},
                           self.host.changed_line_counts(s.work_dir, base, sha),
                           functools.partial(self.host.file_at, s.work_dir), self.wallclock(),
                           functools.partial(self._in_time, deadline))
        return judge_run(quality, orchestrator_checks(self.pipeline, self.pypi), change)

    def _in_time(self, deadline: float) -> bool:
        """RunChange.in_time for a round that ends at deadline, a clock() time."""
        self._heartbeat()
        return self.clock() < deadline - SLOW_STEP_MARGIN

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
            # Jenkins has checked the ref out by now, though the build may still run the stages after its Gate stage
            # (_analyse); the Jenkinsfile's Checkout comment says why that is enough. A leftover is only clutter,
            # which the run's end tries again.
            delete_ci_ref(self.host, s.cwd, ref)
            s.ci = {}
        self._save()

    def _clean_up_quality(self) -> None:
        """At the end of a run that has or had the quality gate, delete its refs on origin, again for one whose gate
        resume turned off, in case deleting them then failed; a failure is only logged."""
        if self.state.had_quality_gate:
            delete_ci_refs(self.host, self.state)

    def _turn(self, role: str, text: str, path: str, timeout: int | None, *, fresh_text: str | None = None,
              watch_stalls: bool = True, announce=None, verdict: bool = False, blocks: bool = False) -> str:
        """Return the handoff file that ends the role's turn, prompting the role only if it still owes it.

        fresh_text replaces text for an agent in a new session, which has not seen the role's
        earlier turns. announce(how) runs once the agent is ready, before any prompt. A verdict
        file without a VERDICT first line is asked for once more (_retry) before it ends the run.
        With blocks, a BLOCKED report waits for the human (_block) as often as it comes.
        """
        if verdict:
            return self._verdict_turn(role, text, path, timeout, fresh_text, watch_stalls, announce)
        s = self.state
        if blocks and s.blocked is not None and s.blocked["file"] == os.path.basename(path):
            # A resume in the middle of a block.
            self._finish_block(path, s.blocked)
            out = self._unblocked(role, path, s.blocked)
        else:
            out = self._written(role, text, path, timeout, fresh_text, watch_stalls, announce)
            if blocks and (question := parse_blocked(out)) is not None:
                out = self._unblocked(role, path, self._block(role, path, question))
        if not out.strip():
            self._reject(path, f"was written empty by the {self.pipeline.roles[role]}")
        return out

    def _verdict_turn(self, role: str, text: str, path: str, timeout: int | None, fresh_text: str | None,
                      watch_stalls: bool, announce) -> str:
        """_turn for the verdict step: its file, once it starts with a VERDICT line."""
        s = self.state
        file = os.path.basename(path)
        if s.retrying == file:
            text = fresh_text = self._retry_prompt(path)
        while not parse_verdict(out := self._written(role, text, path, timeout, fresh_text, watch_stalls, announce)):
            self._retry(role, path, out)
            text = fresh_text = self._retry_prompt(path)
        if s.retrying == file:
            s.retrying = None
        return out

    def _unblocked(self, role: str, path: str, block: dict) -> str:
        """The report of a blocked turn once it is no longer BLOCKED, block being the state's record of the block it
        is in; each that still is waits again (_block)."""
        while (question := parse_blocked(out := self._block_answer(role, path, block))) is not None:
            block = self._block(role, path, question)
        self.state.blocked = None
        self._save()
        return out

    def _written(self, role: str, text: str, path: str, timeout: int | None, fresh_text: str | None,
                 watch_stalls: bool, announce) -> str:
        """The handoff file as the role wrote it, before _turn judges it."""
        s = self.state
        file = os.path.basename(path)
        # First, because the role may have written it while no orchestrator was watching.
        if (out := self.host.read(path)) is not None:
            log(f"[{role}] {file} is already written")
            self._end_turn(role, path)
            return out

        how = self._agent(role)
        agent = s.agents[role]
        if announce:
            announce(how)
        log(f"[{role}] working in pane {agent['pane']}")
        if (message := self._prompt_for(how, file, text, fresh_text, path)) is not None:
            # A resumed or restarted turn keeps its record and start. Read before the prompt goes out, so that
            # no agent can write its file before its turn started.
            started = None if self._open_turn(file) else self.host.now()
            self._deliver(role, path, message)
            # Recorded only once the prompt is taken. Dying between sending and confirming costs one duplicate
            # prompt, which the role answers by writing the same file again.
            s.prompted = file
            if started is not None:
                s.turns.append(self._turn_record(role, file, started))
            self._save()
        out = self._await_handoff(role, path, timeout, watch_stalls)
        log(f"[{role}] wrote {file}")
        self._end_turn(role, path)
        return out

    def _deliver(self, role: str, path: str, message: str) -> None:
        """Prompt the role's agent with message until it takes it (_taken), sending it at most twice.

        Raises OrchestratorError when the agent takes neither prompt, naming what a resume does about it, or exits
        meanwhile; HerdrError as Herdr does.
        """
        name = self.state.agents[role]["name"]
        self.herdr.prompt(name, message)
        if self._taken(role, path):
            return
        log(f"[{role}] did not take its prompt within {PROMPT_DELIVERY_SECONDS}s; sending it again")
        self.herdr.prompt(name, message)
        if not self._taken(role, path):
            label, pane = self.pipeline.roles[role], self.state.agents[role]["pane"]
            raise OrchestratorError(f"the {label} did not take its prompt to write {path}, sent twice; see pane "
                                    f"{pane}, and resume to send it again")

    def _taken(self, role: str, path: str) -> bool:
        """Whether the role's agent took the prompt just sent, within PROMPT_DELIVERY_SECONDS: herdr reports it
        working or blocked, or path, its handoff file, exists. idle and done do not count, since an agent shows
        them before taking a prompt too.

        Raises OrchestratorError when the agent exits without writing path; HerdrError as Herdr.wait does.
        """
        name = self.state.agents[role]["name"]
        # First, for a turn so quick that the agent is done before the wait begins.
        if self.host.read(path) is not None:
            return True
        try:
            self.herdr.wait(name, PROMPT_DELIVERY_SECONDS * 1000, until=("working", "blocked"))
            return True
        except HerdrError as e:
            # herdr's wait on an agent that has exited fails with agent_not_found, which is told apart below.
            if e.code not in ("timeout", "agent_not_found"):
                raise
        if self.host.read(path) is not None:
            return True
        if self.herdr.agent(name) is None:
            raise OrchestratorError(f"the {self.pipeline.roles[role]} exited without writing {path}")
        return False

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
        # The turn's record, which _written closed, goes with the file, whose mtime the rename kept; the retry opens
        # a record of its own.
        next(t for t in reversed(s.turns) if t["file"] == file)["file"] = os.path.basename(rejected)
        self._save()
        log(f"[{role}] {file} {why}; moved it to {os.path.basename(rejected)}, prompting once more")

    def _block(self, role: str, path: str, question: str) -> dict:
        """Move a BLOCKED report aside to its next blocked name, and record the block, so that the turn waits for
        the human's answer and then for the report again. Returns the record, RunState.blocked."""
        s, file = self.state, os.path.basename(path)
        k = s.blocked["k"] + 1 if s.blocked and s.blocked["file"] == file else 1
        blocked = self._blocked_path(path, k)
        # Everything is saved before the rename, which a resume finishes (_finish_block): dying at any point leaves
        # the report to be judged again, as block k once more, or the wait under way, never a report lost or
        # counted twice. No prompt counts as delivered, so even an agent that is alive gets BLOCKED_PROMPT.
        s.prompted = None
        s.blocked = {"file": file, "question": question, "k": k}
        # The turn's record, which _written closed, goes with the file, as in _retry; the wait opens a record of
        # its own, human-paced (_turn_record).
        next(t for t in reversed(s.turns) if t["file"] == file)["file"] = os.path.basename(blocked)
        self._save()
        self.host.rename(path, blocked)
        log(f"[{role}] {file} is blocked: {question}; moved it to {os.path.basename(blocked)}")
        return s.blocked

    def _finish_block(self, path: str, block: dict) -> None:
        """Make the rename of the block, RunState.blocked, that the orchestrator saved but may have died before
        making."""
        blocked = self._blocked_path(path, block["k"])
        if self.host.read(blocked) is None and self.host.read(path) is not None:
            self.host.rename(path, blocked)

    def _block_answer(self, role: str, path: str, block: dict) -> str:
        """The role's report once the human has answered the BLOCKED question of block, RunState.blocked, in its
        pane.

        As for the interview: no turn timeout and no stall notice, since the agent waits for the human. A resumed
        wait announces itself again, and prompts as _prompt_for does, with BLOCKED_PROMPT in place of the step's.
        """
        s = self.state
        prompt = self.prompts.BLOCKED_PROMPT.format(path=path, blocked_path=self._blocked_path(path, block["k"]))

        def announce(_how: str) -> None:
            self.herdr.focus(s.agents[role]["name"])
            self._ask_human(role, f"is blocked: {block['question']}")

        return self._written(role, prompt, path, None, prompt, False, announce)

    @staticmethod
    def _blocked_path(path: str, k: int) -> str:
        stem, ext = os.path.splitext(path)
        return f"{stem}.blocked-{k}{ext}"

    @staticmethod
    def _rejected_path(path: str) -> str:
        stem, ext = os.path.splitext(path)
        return f"{stem}.rejected{ext}"

    def _retry_prompt(self, path: str) -> str:
        return self.prompts.RETRY_VERDICT_PROMPT.format(path=path, rejected_path=self._rejected_path(path),
                                                        approve=APPROVE, changes=CHANGES_REQUESTED)

    def _prompt_for(self, how: str, file: str, text: str, fresh_text: str | None, path: str) -> str | None:
        """What to prompt an agent that is ready as how says with; None when it only has to be waited for."""
        if how in (NEW, RESTARTED):
            return fresh_text or text
        if self.state.prompted != file:
            return text
        if how == RESUMED:
            return self.prompts.CONTINUE_PROMPT.format(path=path)
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
        # and neither does a retry or a block: the deleted file is asked for with the step's own prompt.
        self.state.prompted = self.state.retrying = self.state.blocked = None
        raise OrchestratorError(f"{path} {why}; fix it, or delete it to have it written again, then resume")

    def _open_turn(self, file: str) -> dict | None:
        """The record of the turn under way that writes file; None when there is none."""
        return next((t for t in self.state.turns if t["file"] == file and t["ended_at"] is None), None)

    def _turn_record(self, role: str, file: str, started: int | None) -> dict:
        """An open record of the current phase's turn that writes file: the current step's, or with role QUALITY
        a quality analysis, which runs no agent. started is None when the turn's start is not known."""
        s = self.state
        step = self.pipeline.step(s.phase)
        agent = model = session = None
        if step:
            agent = (s.agents.get(role) or {}).get("kind") or s.agent_kinds.get(role, DEFAULT_AGENT)
            model = s.models.get(role)
            session = (s.agents.get(role) or {}).get("session")
        return {"step": s.phase, "role": role, "round": s.round,
                "quality_round": s.quality_round if step is None or step.quality_gated else 0,
                # The agent's session for the whole turn, None when that is not known: _end_turn clears it for a
                # turn that ended in another session than it started in.
                # The tokens of the turn, turn_tokens counts them once the run is done; None until then, and always
                # for a quality analysis.
                "file": file, "agent": agent, "model": model, "session": session, "tokens": None,
                # A blocked turn's wait for the human's answer, and the rewrite after it, is paced by the human too.
                "human_paced": bool(step and step.human_paced) or bool(s.blocked and s.blocked["file"] == file),
                "started_at": None if started is None else iso_utc(started), "ended_at": None, "seconds": None}

    def _end_turn(self, role: str, path: str) -> None:
        """Close the record of the turn that wrote path, found written, at the file's mtime: the turn ended then,
        even if no orchestrator was watching. A file whose turn has no record, as one written before turns were
        recorded, gets one with no start."""
        s, file = self.state, os.path.basename(path)
        record = self._open_turn(file)
        if record is None:
            if any(t["file"] == file for t in s.turns):
                return  # closed already, by an orchestrator that stopped before the run moved on
            record = self._turn_record(role, file, None)
            s.turns.append(record)
        if record.get("session") != (s.agents.get(role) or {}).get("session"):
            record["session"] = None
        ended = self.host.mtime(path)
        record["ended_at"] = iso_utc(ended)
        if record["started_at"] is not None:
            record["seconds"] = ended - epoch_seconds(record["started_at"])
        self._save()

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
        return iso_utc(self.wallclock())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# The RUN argument of every command that takes one; find_run looks it up.
RUN_REF_HELP = "the run id, or the six-character key at its end"


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
                          help=f"with --quality-gate: quality builds per review round before the Reviewer "
                               f"gets the change anyway (default {DEFAULT_MAX_QUALITY_ROUNDS})")

    # What a run is started with, for run and for each of drain's runs. Not resume's: a resumed run keeps its
    # workflow and where it works (its worktree or the checkout), and switches its quality gate with flags of its own.
    starts = argparse.ArgumentParser(add_help=False)
    which = starts.add_mutually_exclusive_group()
    which.add_argument("--workflow", choices=offered_workflows(), default=DEFAULT_WORKFLOW.name,
                       help=f"the roles and handoffs the run goes through: a built-in workflow or one in "
                            f"{workflows_dir()} (default {DEFAULT_WORKFLOW.name}); see the workflows command. "
                            f"With --spec, and for drain, its first step must write the spec")
    which.add_argument("--workflow-file", metavar="FILE",
                       help="like --workflow, for the workflow file at FILE, on this machine")
    starts.add_argument("--worktree", action="store_true",
                        help=f"build in a git worktree of the run's own, {WORKTREES_DIR}/<key>, from {REMOTE}'s "
                             f"version of the checked-out branch, instead of in the checkout itself, which then "
                             f"need not be clean and stays free to use. Needs a branch checked out and an {REMOTE}. "
                             f"The worktree is removed once the pull request is open; when the run fails, or "
                             f"for run --no-pr, it stays")
    starts.add_argument("--quality-gate", metavar="JOB",
                        help="after each Builder turn, have this Jenkins job (its full name, folders included) "
                             "judge the change with `gate.py`, and send the findings back to the "
                             "Builder; needs JENKINS_URL, JENKINS_USER and JENKINS_TOKEN, "
                             "from the environment or ~/.config/ai-agents-orchestrator/ci.env")

    run = sub.add_parser("run", parents=[target, settings, starts], help="run the handoff workflow for a task")
    run.add_argument("task", nargs="?",
                     help="what to build, as you would tell the Spec Collector; optional with --spec, "
                          "which then takes the spec's title")
    # Run only: a resumed run already has its spec.
    run.add_argument("--spec", metavar="FILE",
                     help="skip the interview: copy the spec at FILE, on this machine, into the run as the "
                          "first step's file, and start at the step after it. Needs a workflow whose first "
                          "step writes the spec, as default's does")
    # Run only: a resumed run may already be on its own branch, so whether it ends in a pull request is fixed.
    run.add_argument("--no-pr", action="store_true",
                     help="leave the change uncommitted in the working tree and the workspace open, "
                          "instead of opening a pull request")

    resume = sub.add_parser(
        "resume", parents=[target, settings], help="continue a run whose orchestrator has stopped",
        description="Continue a run whose orchestrator has stopped. A settings flag overrides what the run "
                    "was started with; an unset one keeps it, not the default shown.")
    resume.add_argument("run_ref", metavar="RUN", help=RUN_REF_HELP)
    resume.add_argument("--force", action="store_true", help="take over a run that still looks alive")
    # Hidden and refused below: argparse would otherwise take --spec for an abbreviation of --spec-model.
    resume.add_argument("--spec", nargs="?", const="", help=argparse.SUPPRESS)
    switch = resume.add_mutually_exclusive_group()
    switch.add_argument("--quality-gate", metavar="JOB",
                        help="turn the quality gate on with this Jenkins job, as run --quality-gate has it, for a "
                             "run started without it; a Builder report not yet judged is judged before the "
                             "Reviewer starts, and otherwise the gate starts at the Builder's next turn")
    switch.add_argument("--no-quality-gate", action="store_true",
                        help="turn the run's quality gate off for good, deleting its refs on origin; an analysis "
                             "under way is dropped, and the Reviewer is told what the gate last found")

    listing = sub.add_parser(
        "list", parents=[target], help="list the runs in the project directory",
        description="List the runs in the project directory: each run's id, phase, round and outcome, and its task.")
    listing.add_argument("--json", action="store_true",
                         help=f"print one JSON document instead, {{\"version\": {RUN_RECORD_VERSION}, \"runs\": [...]}}, "
                              "with a record of each run's state")
    listing.add_argument("--pr-state", action="store_true",
                         help="with --json, ask gh for the state of each run's pull request, one call per run")

    sub.add_parser(
        "metrics", parents=[target], help="print the metrics of every run that is done, a JSON line each",
        description=f"Print the {METRICS_FILE} of every run in the project directory that is done, one JSON object "
                    f"per line, oldest run first, for jq or a spreadsheet: its outcome, roles, turns with their times "
                    f"and tokens, and quality rounds. A run that is not done has none and is left out.")

    show = sub.add_parser(
        "show", parents=[target], help="print one run in full: its state, files and last review",
        description="Print one run: its line as list shows it and its whole task, its workflow, branch and "
                    "pull request, each role's agent, the files in its run directory, and the last review.")
    show.add_argument("run_ref", metavar="RUN", help=RUN_REF_HELP)

    prune = sub.add_parser(
        "prune", parents=[target], help="delete the branches of runs whose pull request is done",
        description=f"Delete the branch of each run whose pull request is merged, or closed for "
                    f"{CLOSED_BRANCH_GRACE // 86400} days, locally and on {REMOTE}, and print what became of each. "
                    f"A branch with commits its pull request lacks, or one checked out, is kept. Every run "
                    f"that opens a pull request does this first, except for branches kept before.")
    prune.add_argument("--dry-run", action="store_true", help="print what would be deleted, and delete nothing")

    drain = sub.add_parser(
        "drain", parents=[target, settings, starts], help="run every ready spec in a directory, one pull request each",
        description=f"Run each DIR/<slug>/spec.md whose header says {SPEC_READY} and whose blockers are "
                    f"{SPEC_DONE}, one after another, as run --spec runs it, and record each in the spec's header: "
                    f"{SPEC_IN_REVIEW} with its pull request, or {SPEC_FOR_HUMAN}, which stops the drain. First "
                    f"it marks {SPEC_DONE} each spec whose pull request has merged, and refreshes the runs of "
                    f"{SPEC_IN_REVIEW} and {SPEC_FOR_HUMAN} specs as the refresh command does, moving a "
                    f"{SPEC_FOR_HUMAN} spec whose approved pull request no longer conflicts to {SPEC_IN_REVIEW}. "
                    f"Exits 0 once no spec is {SPEC_READY}, and {EXIT_WAITING} when the ones left have blockers that are not {SPEC_DONE}.")
    drain.add_argument("dir", nargs="?", default=DRAIN_DIR, metavar="DIR",
                       help=f"the directory of specs, on this machine; default {DRAIN_DIR}")
    drain.add_argument("--dry-run", action="store_true",
                       help="print the changes to the specs' status and the specs it would run, in order, and "
                            "write, run and delete nothing")
    # Hidden and refused below, as for resume: argparse would otherwise take --spec for --spec-model.
    drain.add_argument("--spec", nargs="?", const="", help=argparse.SUPPRESS)
    # Each run drain starts is a run --spec run that opens a pull request: new_state reads these.
    drain.set_defaults(task=None, no_pr=False)

    close = sub.add_parser(
        "close", parents=[target], help="clean up after a finished or failed run: its workspace, worktree and branch",
        description="Clean up after a run that is finished, failed or stale, and record it in the run's state, so "
                    "that resume refuses it. Closes the run's herdr workspace. A failed or stale --worktree run "
                    "also loses its worktree, uncommitted changes included, which are listed first, and its "
                    "branch, unless that has commits beyond the run's base. A finished run keeps its worktree; "
                    "an in-place run keeps its checkout and branch. A running run is refused.")
    close.add_argument("run_ref", metavar="RUN", help=RUN_REF_HELP)

    refresh = sub.add_parser(
        "refresh", parents=[target], help="clear a resolved conflict from a run's pull request",
        description=f"For a finished run whose pull request was opened as a draft over conflicts with its base, ask "
                    f"gh whether it merges cleanly now. If it does, remove the conflict warning from its "
                    f"description, keeping every other edit, mark it ready for review if the run approved or "
                    f"finished, and forget the conflicts in the run's state; a pull request merged or closed only "
                    f"has its run's conflicts forgotten. Without RUN, every finished run with a pull request and "
                    f"conflicts. Prints a line per run. Exit status 0 when each conflict is cleared or forgotten, "
                    f"{EXIT_WAITING} when a pull request still conflicts or GitHub has not computed whether it does.")
    refresh.add_argument("run_ref", nargs="?", metavar="RUN", help=f"{RUN_REF_HELP}; default: every such run")

    workflows = sub.add_parser(
        "workflows", help="list the workflows, or print one as a workflow file",
        description=f"Without WORKFLOW, list the workflows `run --workflow` offers: the built-in ones and "
                    f"the workflow files in {workflows_dir()}. With it, print that workflow, or the file "
                    f"at that path, as a workflow file, a starting point for your own.")
    workflows.add_argument("workflow", nargs="?", metavar="WORKFLOW", help="a workflow's name, or a file's path")

    judged = sub.add_parser(
        "judged-in-quality", help="exit 0 when a quality build already judged HEAD as its pull request's build would",
        description=f"Exit 0 when a build of the quality job JOB already judged HEAD as a pull request's build "
                    f"into BRANCH would, so that build need not judge it again; docs/quality-gate.md says when. "
                    f"Fetches BRANCH, and the main commit a build names. Otherwise, and on any error, exit "
                    f"{EXIT_ERROR}, saying why in one line on standard error. Needs JENKINS_URL, JENKINS_USER and "
                    f"JENKINS_TOKEN, from the environment or {ci_env_path()}.")
    judged.add_argument("--quality-gate", required=True, metavar="JOB",
                        help="the quality job's full name, folders included")
    judged.add_argument("--target", required=True, metavar="BRANCH",
                        help="the branch the pull request would merge into")

    sub.add_parser(
        "doctor", parents=[target], help="check what a run needs, and say what to run for each that is missing",
        description=f"Check what a run needs where the agents run, on this machine or with --machine on that one: "
                    f"herdr {dotted(HERDR_MIN_VERSION)} or later and a way to reach it, each harness's "
                    f"CLI and herdr integration, git {dotted(GIT_MIN_VERSION)} or later, gh logged in, "
                    f"gitleaks, with --cwd a git checkout with an {REMOTE}, and tkinter for the GUI. Prints a line "
                    f"each, ok, missing, or warn for one only some runs need, with a fix under each that is not ok. "
                    f"Installs and changes nothing. Exit status 0 when nothing is missing, {EXIT_ERROR} otherwise.")

    gui = sub.add_parser(
        "gui", help="open a window to start, list and resume runs; the default with no arguments where there "
                    "is a display")
    gui.add_argument("--smoke-test", action="store_true", help="open the window and close it at once")

    serve = sub.add_parser(
        "serve", help="serve the runs of any project, read-only, over HTTP on 127.0.0.1, for the desktop app",
        description="Serve the runs of any project, local or on a saved herdr machine, read-only: the run records "
                    "list --json prints, each run's files and their contents, and a stream of events as they "
                    "change. Prints one JSON line, {\"url\": ..., \"token\": ...}, then serves on a free port of "
                    "127.0.0.1 until SIGINT or SIGTERM, and exits 0. Every request needs the token.")
    serve.add_argument("--origin", action="append", default=[], metavar="ORIGIN",
                       help="also answer requests from this origin, such as the desktop app's webview, "
                            "tauri://localhost; repeatable")

    args = p.parse_args(argv)
    check_spec_args(args, run, resume, drain)
    if getattr(args, "pr_state", False) and not args.json:
        listing.error("--pr-state needs --json")
    check_flag_values(args, p)
    return args


def check_flag_values(args: argparse.Namespace, p: argparse.ArgumentParser) -> None:
    """Exit with a usage error from p, the top-level parser, for flag values that argparse's types cannot rule out:
    --machine without --cwd, a --max-rounds or --max-quality-rounds below 1, a run given
    --max-quality-rounds without --quality-gate, a resume given it with --no-quality-gate, and a --quality-gate that
    is not a Jenkins job's full name. args are any command's; a flag its command does not take is not checked."""
    if getattr(args, "machine", None) and not args.cwd:
        p.error("--cwd is required with --machine")
    if getattr(args, "max_rounds", None) is not None and args.max_rounds < 1:
        p.error("--max-rounds must be at least 1")
    if getattr(args, "max_quality_rounds", None) is not None:
        if args.max_quality_rounds < 1:
            p.error("--max-quality-rounds must be at least 1")
        if args.command in ("run", "drain") and not args.quality_gate:
            p.error("--max-quality-rounds needs --quality-gate")
        if getattr(args, "no_quality_gate", False):
            p.error("--max-quality-rounds does not apply with --no-quality-gate")
    if getattr(args, "quality_gate", None) is not None and not re.fullmatch(r"[^/]+(/[^/]+)*", args.quality_gate):
        p.error("--quality-gate takes a Jenkins job's full name, such as folder/job")


def check_spec_args(args: argparse.Namespace, run: argparse.ArgumentParser,
                    resume: argparse.ArgumentParser, drain: argparse.ArgumentParser) -> None:
    """Exit with a usage error for a run with neither a task nor --spec, or a resume or drain given --spec."""
    if args.command == "run" and args.task is None and args.spec is None:
        run.error("a task or --spec FILE is needed")
    if args.command == "resume" and args.spec is not None:
        resume.error("unrecognized arguments: --spec; a resumed run keeps the spec it has")
    if args.command == "drain" and args.spec is not None:
        drain.error("unrecognized arguments: --spec; drain runs the specs in DIR")


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


def interrupted_note(state: RunState | None, target: list[str]) -> str:
    """What to tell the human whose Ctrl-C stopped the orchestrator, with the command that resumes the run that
    state describes; state is None for an interrupt before the run's state existed, and the note names no command."""
    hint = f"; resume with: {resume_command(state.run_id, target)}" if state else ""
    return f"interrupted; the role agents keep running in herdr{hint}"


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
# The waiting reason, and outcome, of a running run whose turn waits on a BLOCKED report (run_record).
BLOCKED_STATUS = "blocked"


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


def saved_quality_gate(saved: dict) -> str | None:
    """The quality gate of the run saved as saved, as show prints it: its job, or that resume turned it off; None
    for a run that never had one."""
    if saved.get("quality_gate_disabled"):
        return "off (turned off by resume)"
    return saved.get("quality_job")


def show_run(host: Host, cwd: str, ref: str, local_host: str, pid_alive, target: list[str]) -> None:
    """Print one run in full: its list line and task, workflow, branch, agents, turns, files and last review."""
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
                         ("quality", saved_quality_gate(s)),
                         ("conflicts", ", ".join(s.get("conflicts") or [])), ("workspace", s.get("workspace_id")),
                         ("closed", s.get("closed"))):
        if value:
            print(f"{label:<9}  {value}")
    agents = s.get("agents") or {}
    width = max((len(role) for role in agents), default=0)
    print_section("agents", [f"{role:<{width}}  {a.get('kind') or DEFAULT_AGENT:<8}  {a.get('pane', '')}"
                             for role, a in agents.items()])
    print_section("turns", turn_lines(s.get("turns") or []))
    print_section("files", [f.path for f in host.run_files(run_dir)])
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


# The version of the run records `list --json` prints, raised when a key is removed or changes its meaning.
RUN_RECORD_VERSION = 1


def run_record(s: dict, age: int, local_host: str, pid_alive, target: list[str]) -> dict:
    """A run's saved state as `list --json` prints it, and as the run list and `show` word it (run_rows).

    s is the state as Host.run_states loaded it, age the seconds since its state.json was written, and
    local_host, pid_alive and target are run_health's and resume_command's. Reads nothing beyond s; a state
    saved by an older version, or one whose workflow this orchestrator cannot load, still makes a record.

    The record is JSON-ready, and has every key below; null means the key does not apply to the run:
    - id, key, task (the whole task), workflow: the run's id, its key (as resume takes it), task and workflow.
    - steps: the workflow's steps, each {"id", "role"}, in order; null when the workflow cannot be loaded.
    - phase, round: as saved. quality_round: the quality round while the run is in the quality loop or its
      gated step, once it has one; null otherwise.
    - role, pane: the current step's role and that role's pane; null in quality, publish or done, or when the
      workflow cannot be loaded, and pane also before the role's agent is started.
    - health, health_reason: RUNNING or STALE and why, as run_health has them; null for a finished or failed run.
      heartbeat_age is age.
    - waiting: what the run waits on the human for, {"reason", "detail"}, else null. Reasons, first match wins:
      "blocked" (detail: the question) while a running run's turn waits on a BLOCKED report; "interview"
      (detail: the role) while a running run's current step is human-paced and its last turn has not ended;
      "stale" (detail: the resume command) for a stale run that is not closed.
    - verdict, error, closed, branch, worktree: as saved.
    - pr: null without a pull request, else {"url", "state", "error"}; state and error are null here, and
      fill_pr_states sets one of them.
    - turns: the turn records as saved (Workflow._turn_record), [] for a run saved before them.
    - resume_command: the command that resumes a stale run that is not closed; null otherwise.
    """
    pipeline = saved_pipeline(s)
    step = pipeline.step(s["phase"]) if pipeline else None
    role = step.role if step else None
    health = run_health(s, age, local_host, pid_alive)
    resume = resume_command(s["run_id"], target) if health and health[0] == STALE and not s.get("closed") else None
    # A workflow this orchestrator lacks has no gated step known, so only phase quality has a quality round then.
    gated = pipeline.gated.id if pipeline and pipeline.gated else QUALITY
    turns = s.get("turns") or []
    last_turn = next((t for t in reversed(turns) if t.get("step") == s["phase"]), None)
    waiting = None
    if health and health[0] == RUNNING and s.get("blocked"):
        waiting = {"reason": BLOCKED_STATUS, "detail": s["blocked"]["question"]}
    elif (health and health[0] == RUNNING and step and step.human_paced and last_turn
          and last_turn.get("ended_at") is None):
        waiting = {"reason": "interview", "detail": role}
    elif resume:
        waiting = {"reason": STALE, "detail": resume}
    return {
        "id": s["run_id"], "key": s["run_id"].rsplit("-", 1)[-1], "task": s["task"],
        "workflow": s.get("workflow") or DEFAULT_WORKFLOW.name,
        "steps": [{"id": st.id, "role": st.role} for st in pipeline.steps] if pipeline else None,
        "phase": s["phase"], "round": s["round"],
        "quality_round": s.get("quality_round") if s.get("quality_round") and s["phase"] in (QUALITY, gated) else None,
        "role": role, "pane": (s.get("agents") or {}).get(role, {}).get("pane") if role else None,
        "health": health[0] if health else None, "health_reason": health[1] if health else None,
        "heartbeat_age": age, "waiting": waiting,
        "verdict": s.get("verdict"), "error": s.get("error"), "closed": s.get("closed"),
        "branch": s.get("branch"), "worktree": s.get("worktree"),
        "pr": {"url": s["pr_url"], "state": None, "error": None} if s.get("pr_url") else None,
        "turns": turns, "resume_command": resume,
    }


def fill_pr_states(host: Host, cwd: str, records: list[dict]) -> None:
    """Set pr.state of each run record with a pull request to gh's OPEN, CLOSED or MERGED, one gh pr view each,
    run in the project directory cwd. A run whose lookup fails gets the failure's message in pr.error instead."""
    for record in records:
        if pr := record["pr"]:
            try:
                pr["state"] = host.pull_request(cwd, pr["url"])["state"]
            except OrchestratorError as e:  # GhUnusable too: each run says why its state is missing
                pr["error"] = str(e)


def run_rows(runs: list[tuple[int, dict]], local_host: str, pid_alive,
             target: list[str]) -> list[tuple[str, str, str, str, str]]:
    """Each run as `list` shows it, in the GUI too: its id, phase, round, outcome and task."""
    rows = []
    for age, s in runs:
        record = run_record(s, age, local_host, pid_alive, target)
        rows.append((record["id"], record["phase"], run_round(record), run_outcome(record), record["task"][:100]))
    return rows


def run_outcome(record: dict) -> str:
    """A run record's outcome as `list` words it: its error, verdict, waiting reason or health, then what it has
    besides, its closing, pull request and a workflow other than the default."""
    waiting = record["waiting"] or {}
    if record["error"]:
        outcome = f"error: {record['error']}"
    elif record["health"] is None:
        outcome = record["verdict"] or ""
    elif record["closed"]:
        # Stale by its heartbeat, but resume refuses it, so neither its staleness nor the command is worth showing.
        outcome = ""
    elif waiting.get("reason") == BLOCKED_STATUS:
        outcome = f"{BLOCKED_STATUS}: {waiting['detail']}"
        if record["pane"]:
            outcome += f" (pane {record['pane']})"
    else:
        outcome = f"{record['health']}: {record['health_reason']}"
        if record["resume_command"]:
            outcome += f"; resume: {record['resume_command']}"
    if record["closed"]:
        outcome = "; ".join(part for part in (outcome, f"closed: {record['closed']}") if part)
    if record["pr"]:
        outcome += f"  {record['pr']['url']}"
    if record["workflow"] != DEFAULT_WORKFLOW.name:
        outcome += f"  [{record['workflow']} workflow]"
    return outcome


def run_round(record: dict) -> str:
    """A run record's round, and its quality round when it has one."""
    rnd = f"round {record['round']}"
    if record["quality_round"]:
        rnd += f" q{record['quality_round']}"
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
    # Not even with --force: close may have deleted its worktree and branch.
    if state.closed:
        raise OrchestratorError(f"run {state.run_id} was closed; start a new run")
    pipeline = run_pipeline(state.workflow, state.workflow_definition, state.prompts)
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
    resume_quality_limit(state, pipeline, args)
    # The herdr this resume addresses: the saved one may name the same herdr reached another way.
    state.machine = args.machine
    return state


def resume_quality_limit(state: RunState, pipeline: Pipeline, args: argparse.Namespace) -> None:
    """Apply resume's --max-quality-rounds to state, of workflow pipeline; raise OrchestratorError for a run without
    the gate, unless args turn it on, or for a limit the run is already past. Without the flag, the saved limit is
    checked the same way."""
    if args.max_quality_rounds is not None:
        if not state.quality_job and not args.quality_gate:
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


def require_origin(host: Host, cwd: str) -> None:
    """Raise OrchestratorError unless the checkout at cwd has an origin, which the quality gate pushes snapshots to."""
    if host.git_run(cwd, "remote", "get-url", REMOTE).returncode != 0:
        raise OrchestratorError(f"the quality gate pushes snapshots to {REMOTE}, and {cwd} has no {REMOTE}")


def turn_quality_gate_on(state: RunState, pipeline: Pipeline, host: Host, ci: CI) -> None:
    """Turn the quality gate of the run in state, of workflow pipeline, on with ci's job, as resume --quality-gate
    does; state is changed in memory only, for the Workflow to save. The run's own job again is a no-op.

    From round 1 on, the checkout must have an origin, gitleaks must run, and the job must pass CI.preflight; round 0
    is left to the Workflow's own preflight, as for run --quality-gate. A run in the step after the gated one, whose
    turn has not started, goes to quality round 1 first; any other starts the gate at the gated step's next turn.

    Raises OrchestratorError, with state unchanged, in phase done or publish (even with the run's own job), after
    turn_quality_gate_off, for a run with another job, in a workflow without a quality-gated step, for an in-place
    --no-pr run past round 0, whose working tree may hold the human's own changes that a snapshot would take, and for
    a failed preflight.
    """
    s = state
    gated = _gate_can_start(s, pipeline, ci.job)
    if gated is None:
        log(f"run {s.run_id} already has quality gate job {ci.job}")
        return
    if s.round:
        require_origin(host, s.cwd)
        # Every quality snapshot is scanned before it is pushed (Host.push_ref); round 0's preflight checks it.
        host.check_gitleaks()
        ci.preflight()
    s.quality_job = ci.job
    following = pipeline.after(gated)
    if following and s.phase == following.id:
        file = os.path.basename(following.path(s.dir, s.round))
        # A turn that was prompted, asked again for its verdict, or is waiting on its BLOCKED answer has seen the
        # change; only one with none of these has not.
        started = {s.prompted, s.retrying, (s.blocked or {}).get("file")}
        if host.read(f"{s.dir}/{file}") is None and file not in started:
            s.phase, s.quality_round = QUALITY, 1
    log(f"turned the quality gate of run {s.run_id} on, with job {ci.job}")


def _gate_can_start(state: RunState, pipeline: Pipeline, job: str) -> Step | None:
    """The quality-gated step of pipeline, if the run in state may turn the gate on with job; None when it already
    has job, which needs nothing done; OrchestratorError saying why not otherwise (turn_quality_gate_on)."""
    s, gated = state, pipeline.gated
    if s.phase in (DONE, PUBLISH):
        why = f"is in phase {s.phase}, past its last turn"
    elif s.quality_job == job:
        return None
    elif s.quality_gate_disabled:
        why = "had its quality gate turned off with --no-quality-gate, which cannot be undone"
    elif s.quality_job:
        why = f"has quality gate job {s.quality_job}, and cannot switch to {job}"
    elif gated is None:
        why = f"is of workflow {pipeline.name}, which has no quality-gated step"
    elif s.round and not s.pull_request and not s.worktree:
        why = (f"is a --no-pr run in {s.cwd} itself, whose working tree was never required to be clean, and a "
               f"quality snapshot would take your own changes too")
    else:
        return gated
    raise OrchestratorError(f"run {s.run_id} {why}, so its quality gate cannot be turned on")


def turn_quality_gate_off(state: RunState, pipeline: Pipeline, host: Host) -> None:
    """Turn the quality gate of the run in state, of workflow pipeline, off for good, as resume --no-quality-gate
    does; state is changed in memory only, for the Workflow to save.

    The run's refs on origin are deleted, a failure only logged, and a build in progress is left to run. A run in
    phase quality goes on at the step after the gated one, dropping an analysis that has not finished; the verdict
    step then hears of the round's quality files as Workflow._quality_note says.

    Raises OrchestratorError, with state unchanged, for a run without the gate or one that is done.
    """
    s, gated = state, pipeline.gated
    if s.phase == DONE or not s.quality_job:
        why = "is done" if s.phase == DONE else "has no quality gate"
        raise OrchestratorError(f"run {s.run_id} {why}, so --no-quality-gate does not apply")
    delete_ci_refs(host, s)
    s.quality_job, s.quality_gate_disabled = None, True
    if s.phase == QUALITY and gated:
        # An analysis that has not finished has an open record, which no quality file will ever close.
        s.turns = [t for t in s.turns if t["step"] != QUALITY or t["ended_at"] is not None]
        following = pipeline.after(gated)
        s.phase = following.id if following else PUBLISH
    log(f"turned the quality gate of run {s.run_id} off")


def delete_ci_refs(host: Host, state: RunState) -> None:
    """Delete the run's quality snapshot refs on origin and forget its build in flight (state.ci); a failure is only
    logged."""
    try:
        refs = host.remote_branches(state.cwd, f"{CI_REF_PREFIX}{state.key}-")
    except OrchestratorError as e:
        log(f"could not list the {CI_REF_PREFIX} branches on {REMOTE}: {e}")
        refs = []
    for ref in refs:
        delete_ci_ref(host, state.cwd, ref)
    state.ci = {}


def delete_ci_ref(host: Host, cwd: str, ref: str) -> None:
    """Delete the branch ref on cwd's origin; a failure is only logged."""
    try:
        host.delete_remote_branch(cwd, ref)
    except OrchestratorError as e:
        log(f"could not delete {ref} on {REMOTE}: {e}")


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
        record_in_state(host, f"{cwd}/{RUNS_DIR}/{state.run_id}/state.json", saved,
                        "branch_cleanup", run.cleanup, "its branch was pruned")


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


def record_in_state(host: Host, path: str, saved: dict, key: str, recorded: object, meanwhile: str) -> None:
    """Set key to recorded, as JSON, in the run's state.json, unless the run has saved since it was read as saved.

    For what prune, close and refresh write about a run they do not drive. The file keeps its modification time,
    which is the run's heartbeat: a stale run must not look live.
    """
    text = host.read(path)
    try:
        unchanged = text is not None and json.loads(text) == saved
    except ValueError as e:
        raise OrchestratorError(f"corrupt {path}: {e}") from e
    if not unchanged:
        raise OrchestratorError(f"{path} changed while {meanwhile}; {key} is not recorded")
    host.write(path, json.dumps({**saved, key: recorded}) + "\n", keep_mtime=True)


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


def close_run(herdr: Herdr, host: Host, cwd: str, ref: str, local_host: str, pid_alive) -> str:
    """Clean up after a run that nothing drives any more, record what was done as its closed, and return that.

    Every run loses its workspace. A failed or stale --worktree run also loses its worktree, changes and all,
    and its branch if that holds no commit beyond the run's base: the Builder never commits, so such a branch
    has nothing to lose. A finished run's worktree is a --no-pr run's change, and an in-place run's checkout
    and branch are the human's to sort out, so both stay. Each step skips what is already gone, so a close
    that failed part way can be run again; closed is recorded only once every step has succeeded.
    """
    age, saved = find_run(host.run_states(cwd), ref, cwd)
    state = RunState.from_dict(saved)
    if state.closed:
        print(f"run {state.run_id} is already closed: {state.closed}")
        return state.closed
    health = run_health(saved, age, local_host, pid_alive)
    if health and health[0] == RUNNING:
        raise OrchestratorError(f"run {state.run_id} is still running ({health[1]}); stop it before closing it")
    failed = bool(state.error) or health is not None
    done = [close_run_workspace(herdr, state.workspace_id)]
    if state.worktree and failed:
        done += discard_run_worktree(host, cwd, state)
    elif state.worktree and host.is_worktree(cwd, state.worktree):
        done.append(f"worktree kept: {state.worktree}")
    closed = "; ".join(done)
    record_in_state(host, f"{cwd}/{RUNS_DIR}/{state.run_id}/state.json", saved, "closed", closed,
                    "the run was closed")
    print(f"closed run {state.run_id}: {closed}")
    return closed


def close_run_workspace(herdr: Herdr, workspace: str) -> str:
    if workspace and herdr.workspace_exists(workspace):
        herdr.close_workspace(workspace)
        print(f"closed workspace {workspace}")
        return "workspace closed"
    return "workspace already gone"


def discard_run_worktree(host: Host, cwd: str, state: RunState) -> list[str]:
    """Force-remove a failed run's worktree of cwd, after listing the changes that go with it, then its branch."""
    done = []
    if host.is_worktree(cwd, state.worktree):
        if changes := host.git(state.worktree, "status", "--porcelain").splitlines():
            print(f"discarding in {state.worktree}:")
            for line in changes:
                print(f"    {line}")
        host.remove_worktree(cwd, state.worktree, force=True)
        print(f"deleted worktree {state.worktree}")
        done.append("worktree deleted")
    else:
        done.append("worktree already gone")
    # Even with the worktree gone: a close that failed at the branch must find it again.
    if state.branch and host.branch_commit(cwd, state.branch) is not None:
        done.append(discard_run_branch(host, cwd, state))
    return done


def discard_run_branch(host: Host, cwd: str, state: RunState) -> str:
    if state.base is None:
        why = "the run has no base to compare it with"
    elif n := host.commits_beyond(cwd, state.base, state.branch):
        why = f"{n} commit{'s' if n != 1 else ''} beyond base"
    else:
        host.delete_branch(cwd, state.branch)
        print(f"deleted branch {state.branch}")
        return "branch deleted"
    print(f"kept branch {state.branch}: {why}")
    return f"branch kept: {why}"


# ---------------------------------------------------------------------------
# Refreshing a pull request opened over conflicts
# ---------------------------------------------------------------------------

@dataclass
class ConflictCheck:
    """What gh says now of the pull request a run opened over conflicts with its base (has_conflict), and so what
    refresh_conflict does about it."""
    run_id: str
    url: str
    pr_state: str  # OPEN, CLOSED or MERGED
    # MERGEABLE, CONFLICTING, or UNKNOWN while GitHub has not computed it; it decides nothing for a pull request
    # that is not OPEN.
    mergeable: str
    # The pull request's body with the run's conflict_warning removed; None when it is not OPEN and MERGEABLE, or
    # has no such warning left to remove.
    body: str | None
    # True when clearing the conflict takes the pull request out of draft: it is OPEN, MERGEABLE and a draft, and the
    # run's verdict is one of SUCCEEDED.
    ready: bool

    @property
    def clears(self) -> bool:
        """Whether the run's conflicts are forgotten: its pull request merges cleanly, or is merged or closed."""
        return self.pr_state != "OPEN" or self.mergeable == "MERGEABLE"

    @property
    def line(self) -> str:
        """What refresh prints for the run."""
        if self.pr_state != "OPEN":
            return f"{self.run_id}: {self.pr_state.lower()}, conflict forgotten"
        if self.mergeable == "MERGEABLE":
            return f"{self.run_id}: conflict cleared{', ready for review' if self.ready else ''}  {self.url}"
        if self.mergeable == "CONFLICTING":
            return f"{self.run_id}: still conflicts  {self.url}"
        return f"{self.run_id}: mergeability not known yet, try again  {self.url}"


def has_conflict(saved: dict) -> bool:
    """Whether the run saved as saved is done, with a pull request it opened over conflicts it still records."""
    return saved.get("phase") == DONE and bool(saved.get("pr_url")) and bool(saved.get("conflicts"))


def refresh_conflict(host: Host, cwd: str, saved: dict, dry_run: bool = False) -> ConflictCheck:
    """Ask gh about the pull request of the run saved as saved, which has_conflict, and, unless dry_run, act on what
    it says: for one that merges cleanly now, remove the conflict warning from its body, every other edit to the
    body kept, take it out of draft if the run succeeded, and record no conflicts in the run's state; for one merged
    or closed, only that record. One that still conflicts, or whose mergeability GitHub has not computed yet, is left
    as it is. Returns what gh said, and so what was done.

    cwd is the project, where gh runs and the run's state is. Raises OrchestratorError, naming the run, when gh
    fails or the state changed since it was read as saved (record_in_state).
    """
    run_id, url = saved["run_id"], saved["pr_url"]
    try:
        pr = host.pull_request_merge(cwd, url)
        clean = pr["state"] == "OPEN" and pr["mergeable"] == "MERGEABLE"
        body = without_conflict_warning(pr["body"], saved.get("base_branch"), saved["conflicts"]) if clean else None
        check = ConflictCheck(run_id, url, pr["state"], pr["mergeable"], body if body != pr["body"] else None,
                              clean and pr["isDraft"] and saved.get("verdict") in SUCCEEDED)
        if dry_run or not check.clears:
            return check
        if check.body is not None:
            host.edit_pr_body(cwd, url, check.body)
        if check.ready:
            host.mark_pr_ready(cwd, url)
        record_in_state(host, f"{cwd}/{RUNS_DIR}/{run_id}/state.json", saved, "conflicts", [],
                        "its pull request was refreshed")
    except OrchestratorError as e:
        raise OrchestratorError(f"run {run_id}: {e}") from e
    return check


def refresh_command(host: Host, cwd: str, ref: str | None) -> int:
    """Refresh the run ref names (find_run), or with ref None every run in cwd that has_conflict, in run id order,
    printing a line each (ConflictCheck.line). Returns 0 when each conflict was forgotten, or there was none to
    refresh, and EXIT_WAITING when any pull request still conflicts or is not known to merge yet. Raises
    OrchestratorError for a ref that names no run, or one that is not done, and as refresh_conflict does."""
    states = host.run_states(cwd)
    runs = [saved for _, saved in states]
    if ref is not None:
        _, saved = find_run(states, ref, cwd)
        if saved.get("phase") != DONE:
            raise OrchestratorError(f"run {saved['run_id']} is not done; refresh only looks at a finished run's "
                                    f"pull request")
        if not has_conflict(saved):
            print(f"{saved['run_id']}: no conflict to clear")
            return 0
        runs = [saved]
    waiting = False
    for saved in sorted((s for s in runs if has_conflict(s)), key=lambda s: s["run_id"]):
        check = refresh_conflict(host, cwd, saved)
        print(check.line)
        waiting = waiting or not check.clears
    return EXIT_WAITING if waiting else 0


# ---------------------------------------------------------------------------
# Draining specs
# ---------------------------------------------------------------------------

# Where drain looks for specs when given no DIR, relative to the current directory.
DRAIN_DIR = ".scratch"
# The values of a spec's Status: line that drain acts on. A spec moves from SPEC_READY to SPEC_IN_REVIEW, its pull
# request open, to SPEC_DONE, merged; or to SPEC_FOR_HUMAN, which drain leaves only for SPEC_IN_REVIEW, once a
# succeeded run's conflict is cleared (refreshed_headers). Drain leaves a spec with any other value, or none, alone.
SPEC_READY = "ready-for-agent"
SPEC_IN_REVIEW = "in-review"
SPEC_DONE = "done"
SPEC_FOR_HUMAN = "ready-for-human"
# The keys of the spec header's lines that drain reads: the lines before the first `## ` heading, each
# `<key>: <entry>`. It writes all but BLOCKED_BY_KEY. Blocked by: is a comma-separated list of the specs that must be
# SPEC_DONE first, each by its slug or a path ending in <slug>/spec.md; Run: names the run drain started for the
# spec, and PR: that run's pull request.
STATUS_KEY = "Status"
BLOCKED_BY_KEY = "Blocked by"
RUN_KEY = "Run"
PR_KEY = "PR"


def _lines(text: str) -> list[str]:
    """text split after each \\n, every line keeping its ending; "".join of them is text."""
    return re.findall(r"[^\n]*\n|[^\n]+\Z", text)


def _header_end(lines: list[str]) -> int:
    return next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))


def _keyed_text(line: str, key: str) -> str | None:
    m = re.match(rf"{re.escape(key)}:\s*(.*)$", line.rstrip("\r\n"))
    return m.group(1).strip() if m else None


def header_field(text: str, key: str) -> str | None:
    """The entry on the spec header's first key line, blanks at its ends removed; None when the header has none."""
    lines = _lines(text)
    found = (_keyed_text(line, key) for line in lines[:_header_end(lines)])
    return next((t for t in found if t is not None), None)


def with_header_field(text: str, key: str, entry: str) -> str:
    """text with the header's first key line made `<key>: <entry>`, or with that line added directly below its
    Status: line when it has none. Every other line, and every line's ending, stays as it was; an added line ends as
    the Status: line does, and below a Status: line that ends the file unterminated, ends the file so instead.
    Raises OrchestratorError for a header with neither line."""
    lines = _lines(text)
    status_at = None
    for i, line in enumerate(lines[:_header_end(lines)]):
        ending = line[len(line.rstrip("\r\n")):]
        if _keyed_text(line, key) is not None:
            lines[i] = f"{key}: {entry}{ending}"
            return "".join(lines)
        if status_at is None and _keyed_text(line, STATUS_KEY) is not None:
            status_at = i
    if status_at is None:
        raise OrchestratorError(f"the spec has no {STATUS_KEY}: line to add {key}: below")
    status_line = lines[status_at]
    ending = status_line[len(status_line.rstrip("\r\n")):]
    if ending:
        lines.insert(status_at + 1, f"{key}: {entry}{ending}")
    else:
        # The Status: line ends the file without a line ending: the new line takes its place as the last.
        lines[status_at] += "\r\n" if "\r\n" in text else "\n"
        lines.append(f"{key}: {entry}")
    return "".join(lines)


# A Blocked by: value that names no blocker, as the specs and tickets here write it.
NO_BLOCKERS = {"-", "none"}


def blocker_slugs(line: str | None) -> list[str]:
    """The slugs a Blocked by: line's value names, in order; none for a spec without the line, or whose value is
    one of NO_BLOCKERS."""
    if (line or "").strip().lower() in NO_BLOCKERS:
        return []
    entries = [e.strip() for e in (line or "").split(",")]
    return [os.path.basename(os.path.dirname(e)) if e.endswith("/spec.md") else e for e in entries if e]


def replace_local_file(path: str, text: str) -> None:
    """Make text, line endings as they are, the contents of the file at path on this machine.

    Written aside and renamed into place, so a failed write leaves the old file, not half a new one. A symlink at
    path is followed, so the link stays and its target changes, and an existing file keeps its permissions. Raises
    OrchestratorError when it cannot be written.
    """
    real = os.path.realpath(path)
    tmp = f"{real}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        with contextlib.suppress(FileNotFoundError):
            shutil.copymode(real, tmp)
        os.replace(tmp, real)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise OrchestratorError(f"cannot write {path}: {e}") from e


@dataclass
class QueuedSpec:
    """A spec that drain found, DIR/<slug>/spec.md, with what drain has done to it."""
    slug: str
    # Its path as drain found it, and its text as drain last read it, with drain's changes to its header since, line
    # endings as in the file.
    file: SpecFile
    blockers: list[str]  # the slugs on its Blocked by: line, each one of a spec drain found
    was: str | None  # its status as drain first read it
    # True once drain has changed its header, or would have but for --dry-run.
    changed: bool = False
    # Why the spec is gone: drain could not read it again (read_spec_file's error), deleted or emptied while drain
    # ran, so it is neither run nor done, and file stays as drain last read it. None while it can be read.
    gone: str | None = None

    @property
    def status_as_read(self) -> str | None:
        """Its Status: line's value as drain last read or set it, which a gone spec keeps; None without one."""
        return header_field(self.file.text, STATUS_KEY)

    @property
    def status(self) -> str | None:
        """Its Status: line's value now; None without one, or once it is gone."""
        return None if self.gone else self.status_as_read

    @property
    def run(self) -> str | None:
        """The run id on its Run: line; None without one, or once it is gone."""
        return None if self.gone else header_field(self.file.text, RUN_KEY)

    @property
    def pr(self) -> str | None:
        """The pull request's URL on its PR: line; None without one, or once it is gone."""
        return None if self.gone else header_field(self.file.text, PR_KEY)

    def reread(self) -> bool:
        """Take the file as it is on disk now, so an edit made while drain runs is kept, not overwritten, and return
        True; or return False, the spec gone, when it cannot be read. A gone spec read again is no longer gone. Its
        blockers stay as drain first read them."""
        try:
            self.file = read_spec_file(self.file.path)
        except OrchestratorError as e:
            self.gone = str(e)
            return False
        self.gone = None
        return True

    def update(self, entries: dict[str, str], write: bool = True) -> bool:
        """Set each of the header's lines that entries names, by key (with_header_field), and, with write, save the
        file as it is on disk now with those lines changed; return True. With write, a spec found gone is left as it
        is: it logs the entries it does not record and returns False. Raises OrchestratorError as with_header_field
        and replace_local_file do."""
        if write and not self.reread():
            log(f"{self.slug}: {self.gone}; not recording {', '.join(f'{k}: {v}' for k, v in entries.items())}")
            return False
        text = self.file.text
        for key, entry in entries.items():
            text = with_header_field(text, key, entry)
        if write:
            replace_local_file(self.file.path, text)
        self.file = SpecFile(self.file.path, text)
        self.changed = True
        return True

    def change_line(self) -> str:
        """The line drain prints for a spec it changed: its slug, old and new status, `gone` for a gone one, and
        pull request."""
        line = f"{self.slug}: {self.was} -> {'gone' if self.gone else self.status}"
        return f"{line}  {self.pr}" if self.pr else line


def read_queue(spec_dir: str) -> list[QueuedSpec]:
    """Every spec_dir/<slug>/spec.md on this machine, ordered by slug.

    Raises OrchestratorError when spec_dir cannot be listed, a spec cannot be read (read_spec_file), a Blocked by:
    line names a slug that has no spec.md, or the blockers form a cycle; the message names the specs.
    """
    try:
        names = sorted(os.listdir(spec_dir))
    except OSError as e:
        raise OrchestratorError(f"cannot list the specs in {spec_dir}: {e.strerror}") from e
    specs = {}
    for name in names:
        path = os.path.join(spec_dir, name, "spec.md")
        if os.path.isfile(path):
            file = read_spec_file(path)
            specs[name] = QueuedSpec(name, file, blocker_slugs(header_field(file.text, BLOCKED_BY_KEY)),
                                     header_field(file.text, STATUS_KEY))
    for spec in specs.values():
        if missing := [b for b in spec.blockers if b not in specs]:
            raise OrchestratorError(f"spec {spec.slug} is blocked by {', '.join(missing)}, which has no spec.md "
                                    f"in {spec_dir}")
    if cycle := blocker_cycle({slug: spec.blockers for slug, spec in specs.items()}):
        raise OrchestratorError(f"the specs' blockers form a cycle: {' -> '.join(cycle)}")
    return list(specs.values())


def blocker_cycle(blockers: dict[str, list[str]]) -> list[str] | None:
    """A cycle among the specs, each by its slug with the slugs that block it, as the slugs along it with the first
    again at the end; None when there is none. Every blocker must be a key of blockers."""
    acyclic = set()

    def cycle_from(slug: str, path: list[str]) -> list[str] | None:
        if slug in path:
            return path[path.index(slug):] + [slug]
        if slug in acyclic:
            return None
        for b in blockers[slug]:
            if found := cycle_from(b, [*path, slug]):
                return found
        acyclic.add(slug)
        return None

    return next((c for slug in blockers if (c := cycle_from(slug, []))), None)


def refreshed_headers(specs: list[QueuedSpec], host: Host, cwd: str, target: list[str],
                      local_host: str, pid_alive, dry_run: bool = False) -> list[tuple[QueuedSpec, dict[str, str]]]:
    """The header lines to set, by spec, for what has become of the pull requests and runs that earlier drains left
    behind, one pair for each spec that has any, each followed to where it settles in this pass (settled_header), so
    that a pull request already merged or closed frees the spec's dependants in this drain. It refreshes a run's
    conflict and logs what refresh_conflict did; with dry_run, it does nothing of that, and gives the header lines as
    though it had.

    cwd is the project, where gh asks about pull requests and the runs are; target is the --machine and --cwd flags
    as given, for a resume command. Raises OrchestratorError as Host.pull_request, refresh_conflict and
    finished_run_header do.
    """
    # Read once, and only when a spec's run is to be looked at.
    statuses = (SPEC_READY, SPEC_IN_REVIEW, SPEC_FOR_HUMAN)
    runs = host.run_states(cwd) if any(spec.run and spec.status in statuses for spec in specs) else []
    settle = functools.partial(settled_header, runs=runs, prs=_PullRequestStates(host, cwd), target=target,
                               local_host=local_host, pid_alive=pid_alive, dry_run=dry_run)
    return [(spec, entries) for spec in specs if (entries := settle(spec))]


class _PullRequestStates:
    """The states gh gives pull requests in one refreshed_headers pass, OPEN, CLOSED or MERGED by URL: each asked of
    gh once at most, or taken from what a conflict refresh was told (learn)."""

    def __init__(self, host: Host, cwd: str):
        self.host, self.cwd = host, cwd  # whom to ask, and in which project, as refreshed_headers says
        self._states: dict[str, str] = {}

    def learn(self, url: str, state: str) -> None:
        """Take state as the pull request's at url for the rest of the pass, as gh gave it another way."""
        self._states[url] = state

    def known(self, url: str) -> str | None:
        """The pull request's state at url, if this pass has heard it; None, asking no one, if not."""
        return self._states.get(url)

    def state(self, url: str) -> str:
        """The pull request's state at url, asking gh unless this pass has heard it. Raises OrchestratorError as
        Host.pull_request does."""
        if url not in self._states:
            self._states[url] = self.host.pull_request(self.cwd, url)["state"]
        return self._states[url]


def settled_header(spec: QueuedSpec, *, runs: list[tuple[int, dict]], prs: _PullRequestStates, target: list[str],
                   local_host: str, pid_alive, dry_run: bool) -> dict[str, str]:
    """The header lines to set for spec, empty for none, from these steps in this order, each on the status the one
    before it left:

    1. a SPEC_IN_REVIEW or SPEC_FOR_HUMAN spec's run, its conflict refreshed (conflict_header), which can make a
       SPEC_FOR_HUMAN spec SPEC_IN_REVIEW with its pull request;
    2. a SPEC_READY spec's finished run on its Run: line, which an interrupted drain leaves (finished_run_header):
       SPEC_IN_REVIEW with its pull request, or SPEC_FOR_HUMAN;
    3. a spec now SPEC_IN_REVIEW with a pull request, or SPEC_FOR_HUMAN with one whose state step 1 heard: SPEC_DONE
       once it is merged, SPEC_FOR_HUMAN once it is closed (pull_request_header).

    gh hears of each pull request once a pass (prs). runs are Host.run_states's; the rest is as refreshed_headers
    says. Raises OrchestratorError as those steps do.
    """
    entries: dict[str, str] = {}
    if spec.status in (SPEC_IN_REVIEW, SPEC_FOR_HUMAN):
        entries |= conflict_header(spec, runs, prs, dry_run)
    elif spec.status == SPEC_READY and spec.run:
        entries |= finished_run_header(spec, spec.run, runs, prs.cwd, target, local_host, pid_alive)
    status, pr = entries.get(STATUS_KEY, spec.status), entries.get(PR_KEY, spec.pr)
    if pr and (status == SPEC_IN_REVIEW or (status == SPEC_FOR_HUMAN and prs.known(pr))):
        settled = pull_request_header(prs.state(pr))
        if settled.get(STATUS_KEY, status) != status:
            entries |= settled
    return entries


def conflict_header(spec: QueuedSpec, runs: list[tuple[int, dict]], prs: _PullRequestStates,
                    dry_run: bool) -> dict[str, str]:
    """Refresh the run on the spec's Run: line, if it has_conflict, as refreshed_headers says, telling prs the state
    gh gave its pull request, and return the header lines that sets: SPEC_IN_REVIEW and the pull request for a
    SPEC_FOR_HUMAN spec whose succeeded run's pull request is open and no longer conflicts; none otherwise, and for a
    run not among runs, Host.run_states's."""
    saved = next((s for _, s in runs if s.get("run_id") == spec.run), None)
    if saved is None or not has_conflict(saved):
        return {}
    check = refresh_conflict(prs.host, prs.cwd, saved, dry_run)
    if not dry_run:
        log(check.line)
    prs.learn(check.url, check.pr_state)
    cleared_open = check.pr_state == "OPEN" and check.clears
    if spec.status == SPEC_FOR_HUMAN and cleared_open and saved.get("verdict") in SUCCEEDED:
        return {STATUS_KEY: SPEC_IN_REVIEW, PR_KEY: check.url}
    return {}


def pull_request_header(state: str) -> dict[str, str]:
    """The header lines to set for a spec whose pull request is in state, as gh gives it: SPEC_DONE for MERGED,
    SPEC_FOR_HUMAN for CLOSED; none for OPEN."""
    if state == "MERGED":
        return {STATUS_KEY: SPEC_DONE}
    if state == "CLOSED":
        return {STATUS_KEY: SPEC_FOR_HUMAN}
    return {}


def finished_run_header(spec: QueuedSpec, run_id: str, runs: list[tuple[int, dict]], cwd: str, target: list[str],
                        local_host: str, pid_alive) -> dict[str, str]:
    """The header lines to set for the finished run run_id, on the spec's Run: line: SPEC_IN_REVIEW and its pull
    request for one that succeeded without conflicts, SPEC_FOR_HUMAN otherwise, with its pull request if it has one.

    runs are Host.run_states's. Raises OrchestratorError for a run that is not finished: running, stale, failed,
    closed or not among runs; the message names the spec, the run and what to do about it.
    """
    saved = next(((age, s) for age, s in runs if s.get("run_id") == run_id), None)
    if saved is None or saved[1].get("phase") != DONE:
        why, remedy = unfinished_run(spec, run_id, saved, cwd, target, local_host, pid_alive)
        raise OrchestratorError(f"spec {spec.slug}: run {run_id}, on its {RUN_KEY}: line, {why}; {remedy}")
    s = saved[1]
    pr = s.get("pr_url")
    if s.get("verdict") in SUCCEEDED and not s.get("conflicts") and pr:
        return {STATUS_KEY: SPEC_IN_REVIEW, PR_KEY: pr}
    return {STATUS_KEY: SPEC_FOR_HUMAN, **({PR_KEY: pr} if pr else {})}


def unfinished_run(spec: QueuedSpec, run_id: str, saved: tuple[int, dict] | None, cwd: str, target: list[str],
                   local_host: str, pid_alive) -> tuple[str, str]:
    """Why the run run_id on the spec's Run: line is not finished, and what the human can do about it, as two
    clauses for finished_run_header's error. saved is the run's age and state as Host.run_states gives them, None
    when there is no such run; a saved run must not be done."""
    start_over = f"remove the {RUN_KEY}: line from {spec.file.path} to start the spec over"
    if saved is None:
        return f"is not under {cwd}/{RUNS_DIR}", start_over
    age, s = saved
    if s.get("closed"):
        return f"was closed ({s['closed']})", start_over
    # None for a run that is not done: it failed.
    health = run_health(s, age, local_host, pid_alive)
    if health is None:
        why = f"failed: {s['error']}"
    elif health[0] == RUNNING:
        return f"is {health[0]} ({health[1]})", f"wait for it to finish, or {start_over}"
    else:
        why = f"is {health[0]} ({health[1]})"
    return why, f"resume it with `{resume_command(run_id, target)}`, or {start_over}"


def startable(spec: QueuedSpec, by_slug: dict[str, QueuedSpec]) -> bool:
    """Whether drain runs the spec: it is SPEC_READY and each of its blockers, by_slug, is SPEC_DONE."""
    return spec.status == SPEC_READY and all(by_slug[b].status == SPEC_DONE for b in spec.blockers)


def drain_command(args: argparse.Namespace, herdr: Herdr, host: Host, cwd: str) -> int:
    """Run the specs in args.dir that are ready, one after another, recording each in its header, and return the
    exit status: 0 once no spec is SPEC_READY, EXIT_WAITING when those left are blocked or gone (drain_end), or the
    status of the run that stopped the drain. With args.dry_run, print what it would change and run, and change nothing.

    cwd is the project the runs are in. Raises OrchestratorError, before any run starts, for a workflow that cannot
    take a spec (seeded_start), and as read_queue and refreshed_headers do.
    """
    seeded_start(flagged_workflow(args, load_prompts()))
    specs = read_queue(args.dir)
    for spec, entries in refreshed_headers(specs, host, cwd, target_args(args), socket.gethostname(), pid_alive,
                                           args.dry_run):
        spec.update(entries, write=not args.dry_run)
    by_slug = {spec.slug: spec for spec in specs}
    if args.dry_run:
        print_spec_changes(specs)
        order = [spec.slug for spec in specs if startable(spec, by_slug)]
        print(f"would run, in order: {', '.join(order)}" if order else "would run no spec")
        return 0
    sweep_branches(host, cwd)
    exit_code = None
    try:
        while exit_code is None and (ready := next_startable(specs, by_slug)):
            exit_code = drain_spec(args, herdr, host, cwd, ready)
    finally:
        print_spec_changes(specs)
    return exit_code if exit_code is not None else drain_end(specs, by_slug)


def next_startable(specs: list[QueuedSpec], by_slug: dict[str, QueuedSpec]) -> QueuedSpec | None:
    """The first of specs drain runs next (startable), each read again from disk first, so a status the human
    changed while an earlier spec ran counts; None when there is none. Logs each spec that has become gone."""
    for spec in specs:
        was_gone = spec.gone
        if not spec.reread() and not was_gone:
            log(f"{spec.slug}: {spec.gone}; {'not running it' if spec.status_as_read == SPEC_READY else 'gone'}")
    return next((s for s in specs if startable(s, by_slug)), None)


def drain_spec(args: argparse.Namespace, herdr: Herdr, host: Host, cwd: str, spec: QueuedSpec) -> int | None:
    """Run the spec as run --spec would with args's flags, and record the run and how it ended in its header.

    Returns None when the run succeeded without conflicts, and the spec is SPEC_IN_REVIEW or gone, or when the spec
    is found gone before the run starts, which it then does not; otherwise the exit status that stops the drain,
    having printed why. A header write to a gone spec is logged instead (QueuedSpec.update). Raises
    OrchestratorError when the run's state cannot be made (new_state) or the spec cannot be written.
    """
    given = spec.file
    state, pipeline = new_state(args, cwd, given)
    if not spec.update({RUN_KEY: state.run_id}):
        return None
    try:
        verdict = run_workflow(herdr, host, state, pipeline, given)
    except RunTakenOver as e:
        # Another orchestrator drives the run now; the next drain finds it by the Run: line and records its end.
        print(f"error: {e}", file=sys.stderr)
        print(f"{spec.slug} stays {SPEC_READY}; run drain again once that run has ended", file=sys.stderr)
        return EXIT_ERROR
    except OrchestratorError as e:
        # Printed first, so the run's error and its resume command survive a header write that fails too.
        print(f"error: {e}", file=sys.stderr)
        print(f"resume its run with: {resume_command(state.run_id, target_args(args))}", file=sys.stderr)
        if spec.update({STATUS_KEY: SPEC_FOR_HUMAN}):
            print(f"{spec.slug} is {SPEC_FOR_HUMAN}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(interrupted_note(state, target_args(args)), file=sys.stderr)
        return EXIT_INTERRUPTED
    exit_code = finish(verdict, state, pipeline)
    spec.update({STATUS_KEY: SPEC_IN_REVIEW if exit_code == 0 else SPEC_FOR_HUMAN,
                 **({PR_KEY: state.pr_url} if state.pr_url else {})})
    return None if exit_code == 0 else exit_code


def print_spec_changes(specs: list[QueuedSpec]) -> None:
    """Print the change_line of each spec drain changed, in their order."""
    for spec in specs:
        if spec.changed:
            print(spec.change_line())


def drain_end(specs: list[QueuedSpec], by_slug: dict[str, QueuedSpec]) -> int:
    """Print what is left once drain can start no spec, and return its exit status: 0 when no spec was SPEC_READY as
    drain last read it, else EXIT_WAITING. Such a spec is left blocked, or gone."""
    left = [spec for spec in specs if spec.status_as_read == SPEC_READY]
    if not left:
        print("all specs cleared")
        return 0
    for spec in left:
        if spec.gone:
            print(f"{spec.slug}: gone ({spec.gone})")
            continue
        print(f"{spec.slug} waits on:")
        for b in spec.blockers:
            blocker = by_slug[b]
            if blocker.gone:
                print(f"    {b}: gone ({blocker.gone})")
            else:
                print(f"    {b}: {blocker.status or 'no status'}" + (f"  {blocker.pr}" if blocker.pr else ""))
    return EXIT_WAITING


def print_workflows() -> None:
    """Print each workflow `run --workflow` offers, then the prompt files a new run would start with."""
    for name in sorted(WORKFLOWS):
        print_workflow(name, "built in", WORKFLOWS[name])
    for name, path in workflow_files().items():
        try:
            # The shape and description only, which no prompt file changes.
            print_workflow(name, path, load_workflow_file(path, BUILT_IN_PROMPTS))
        except OrchestratorError as e:
            print(f"{name}  {path}\n    error: {str(e).removeprefix(f'{path}: ')}")
    print(f"\nAdd one as {workflows_dir()}/NAME.toml; `orchestrator.py workflows {DEFAULT_WORKFLOW.name}` "
          f"prints the default as a file to start from.")
    print_prompt_overrides()


def print_prompt_overrides() -> None:
    """Print the prompt files a new run would start with, one line each, or why it would not start; nothing without
    any."""
    try:
        overrides = load_prompts().overrides
    except OrchestratorError as e:
        print(f"\nPrompt files in {prompts_dir()}:\n    error: {e}")
        return
    if not overrides:
        return
    print(f"\nPrompt files in {prompts_dir()}, in place of the built-in texts:")
    for name in overrides:
        print(f"    {name}  {os.path.join(prompts_dir(), name)}.md")


def print_workflow(name: str, source: str, p: Pipeline) -> None:
    print(f"{name}  {source}")
    print(f"    {workflow_shape(p)}")
    if p.description:
        print(f"    {p.description}")


def workflow_arg(ref: str) -> Pipeline:
    """The workflow a name or a path names, worded by the prompt files as a new run would be: a path has a / or ends in
    .toml."""
    prompts = load_prompts()
    if "/" in ref or ref.endswith(".toml"):
        return load_workflow_file(ref, prompts)
    return named_workflow(ref, prompts)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    state = None
    try:
        exit_code = herdrless_command(args)
        if exit_code is not None:
            return exit_code
        herdr, host = connect(args.machine)
        cwd = host.resolve_dir(args.cwd or os.getcwd())
        if args.command in RUNS_COMMANDS:
            runs_command(args, herdr, host, cwd)
            return 0
        if args.command == "drain":
            return drain_command(args, herdr, host, cwd)
        if args.command == "refresh":
            return refresh_command(host, cwd, args.run_ref)
        spec = read_spec_file(args.spec) if args.command == "run" and args.spec is not None else None
        # Only a run that opens a pull request: a --no-pr run's project may have no gh.
        if args.command == "run" and not args.no_pr:
            sweep_branches(host, cwd)
        state, pipeline = new_state(args, cwd, spec) if args.command == "run" else resumed_state(args, host, cwd)
        verdict = run_workflow(herdr, host, state, pipeline, spec)
    except OrchestratorError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(interrupted_note(state, target_args(args)), file=sys.stderr)
        return EXIT_INTERRUPTED
    return finish(verdict, state, pipeline)


def run_workflow(herdr: Herdr, host: Host, state: RunState, pipeline: Pipeline, spec: SpecFile | None) -> str:
    """Drive the run state describes through pipeline, by the settings saved in state, and return its verdict.

    spec is the file a new run --spec run starts from; None for any other run. Raises OrchestratorError and
    KeyboardInterrupt as Workflow.run does, once it has released the run.
    """
    workflow = Workflow(
        herdr, host, state, notify=notify_locally,
        max_rounds=state.max_rounds, turn_timeout=state.turn_timeout,
        agents=AgentSettings(state.permission_mode, state.models, state.agent_kinds),
        pull_request=state.pull_request,
        ci=CI(state.quality_job) if state.quality_job else None,
        max_quality_rounds=state.max_quality_rounds, pipeline=pipeline, spec=spec)
    return workflow.run()


def herdrless_command(args: argparse.Namespace) -> int | None:
    """Run args's command if it is one that needs no herdr pane, and return its exit status; None for any other
    command, which main runs after connect. Raises OrchestratorError as the command does."""
    # Workflows are on this machine, so listing them needs no herdr.
    if args.command == "workflows":
        workflows_command(args.workflow)
        return 0
    if args.command == "gui":
        return gui_command(args.smoke_test)
    if args.command == "serve":
        return serve_command(args.origin)
    # Not being in a herdr pane is one of the things doctor reports.
    if args.command == "doctor":
        return doctor_command(args.machine, args.cwd)
    if args.command == "judged-in-quality":
        return judged_in_quality_command(args.quality_gate, args.target)
    return None


# The commands about a project's runs, which start none.
RUNS_COMMANDS = ("list", "show", "prune", "close", "metrics")


def runs_command(args: argparse.Namespace, herdr: Herdr, host: Host, cwd: str) -> None:
    local_host = socket.gethostname()
    if args.command == "list" and args.json:
        records = [run_record(s, age, local_host, pid_alive, target_args(args)) for age, s in host.run_states(cwd)]
        if args.pr_state:
            fill_pr_states(host, cwd, records)
        print(json.dumps({"version": RUN_RECORD_VERSION, "runs": records}, indent=2))
    elif args.command == "list":
        print_runs(host.run_states(cwd), local_host, pid_alive, target_args(args))
    elif args.command == "show":
        show_run(host, cwd, args.run_ref, local_host, pid_alive, target_args(args))
    elif args.command == "close":
        close_run(herdr, host, cwd, args.run_ref, local_host, pid_alive)
    elif args.command == "metrics":
        for record in host.run_metrics(cwd):
            print(json.dumps(record))
    else:
        print_pruned(prune_branches(host, cwd, time.time, local_host, pid_alive, dry_run=args.dry_run, revisit=True))


def workflows_command(ref: str | None) -> None:
    if ref:
        print(workflow_toml(workflow_arg(ref)), end="")
    else:
        print_workflows()


def in_herdr_pane() -> bool:
    """Whether this process runs in a pane herdr manages, which a run without a machine needs: without a machine,
    herdr commands target whichever session is focused, and only such a pane knows it is talking to its own."""
    return os.environ.get("HERDR_ENV") == "1"


def connect(machine: str | None) -> tuple[Herdr, Host]:
    """The herdr the agents run in, and the host the project is on."""
    herdr = Herdr(machine)
    if machine:
        return herdr, Host(herdr.ssh_target())
    if not in_herdr_pane():
        raise OrchestratorError("not inside a herdr pane; run from herdr, or pass --machine")
    return herdr, Host()


# ---------------------------------------------------------------------------
# doctor: what a run needs, checked before the first run fails on it
# ---------------------------------------------------------------------------

# How much a failed Check matters, as README's Requirements lists them: no run starts without a REQUIRED one, no run
# opens its pull request without a NEEDED_FOR_PR one, and an OPTIONAL one only some runs need.
REQUIRED = "required"
NEEDED_FOR_PR = "needed-for-pr"
OPTIONAL = "optional"
# The oldest herdr and git a run works with, by the numbers their --version prints.
HERDR_MIN_VERSION = (0, 9)
GIT_MIN_VERSION = (2, 36)


def dotted(version: tuple[int, ...]) -> str:
    """A version as it is written, such as 0.9 for (0, 9)."""
    return ".".join(map(str, version))


@dataclass(frozen=True)
class Check:
    """One thing a run needs, as check_setup found it."""
    name: str  # what was checked, such as "herdr" or "codex integration"
    level: str  # REQUIRED, NEEDED_FOR_PR or OPTIONAL
    ok: bool
    # What was found, such as a version or a path; when not ok, why, or for a skipped check the check it needs.
    detail: str
    fix: str = ""  # a command to run or one sentence; empty when ok; for a skipped check, a pointer to its cause


def doctor_command(machine: str | None, cwd: str | None) -> int:
    """Print check_setup's checks where the agents run, this machine or the saved herdr machine, and of the project
    at cwd when given, as report_checks does, and return its exit status."""
    return report_checks(check_setup(Host(), Herdr(machine), cwd))


def report_checks(checks: list[Check]) -> int:
    """Print a line for each check, `ok`, `missing`, or `warn` for an OPTIONAL one that failed, then its name and
    detail, with its fix on an indented line under it when it has one. Returns 0 when every REQUIRED and
    NEEDED_FOR_PR check is ok, EXIT_ERROR otherwise."""
    for c in checks:
        word = "ok" if c.ok else "warn" if c.level == OPTIONAL else "missing"
        print(f"{word:<8}{c.name}: {c.detail}")
        if c.fix:
            print(f"{'':<8}fix: {c.fix}")
    return 0 if all(c.ok for c in checks if c.level != OPTIONAL) else EXIT_ERROR


class _Unmet(OrchestratorError):
    """A requirement a check found unmet: the message is what it found; fix, when given, replaces the check's own."""

    def __init__(self, detail: str, fix: str | None = None):
        super().__init__(detail)
        self.fix = fix


def check_setup(host: Host, herdr: Herdr, cwd: str | None = None) -> list[Check]:
    """A Check of each thing a run needs, in this order: herdr, reaching herdr, with herdr.machine herdr on that
    machine, each harness's CLI and herdr integration, git, gh, gitleaks, with cwd the project's git checkout and its
    origin, and tkinter.

    host runs commands on this machine, where the orchestrator runs and whose herdr a run drives. Without
    herdr.machine the agents run here too, and herdr is reached from a herdr pane. With it, this herdr must find the
    machine saved and enabled, and every later check but tkinter's runs on its SSH target, through a Host that starts
    commands as host does. cwd is the project's directory where the agents run, a leading ~ meaning the home directory
    there, as for a run; without it the project is not checked. Only reads: it installs, logs in and
    writes nothing. A check that fails, or cannot run or times out, has ok False and the error as its detail. A check
    that needs an earlier one that failed is skipped: ok False, with a detail naming that one. So one failure does not
    stop the rest, and check_setup raises nothing.
    """
    checks: list[Check] = []

    def add(*args, **kwargs) -> Check:
        checks.append(check := _check_unless_skipped(*args, **kwargs))
        return check

    install_herdr = f"install herdr {dotted(HERDR_MIN_VERSION)} or later, or update it with `herdr update`"
    found = add("herdr", REQUIRED, install_herdr, lambda: _tool_version(host, "herdr", HERDR_MIN_VERSION))
    reach_host, reach = _reach_herdr(host, herdr, found)
    checks.append(reach)
    # On a machine every check but tkinter's needs reaching it; reach_host is only used once it is.
    on_machine = reach if herdr.machine else None
    agents = reach_host or host
    if herdr.machine:
        found = add(f"herdr on {herdr.machine}", REQUIRED, install_herdr,
                    lambda: _tool_version(agents, "herdr", HERDR_MIN_VERSION), on_machine)
    integrations = _IntegrationStatus(agents)
    for kind in AGENT_KINDS:
        level = REQUIRED if kind == DEFAULT_AGENT else OPTIONAL
        note = f"needed only for a role that runs in {kind}" if level == OPTIONAL else ""
        add(f"{kind} CLI", level, f"install the {kind} CLI where the agents run",
            functools.partial(_on_path, agents, kind), on_machine, note)
        add(f"{kind} integration", level, f"herdr integration install {kind}",
            functools.partial(integrations.current, kind), found, note)
    git = add("git", REQUIRED, f"install git {dotted(GIT_MIN_VERSION)} or later",
              lambda: _tool_version(agents, "git", GIT_MIN_VERSION), on_machine)
    add("gh", NEEDED_FOR_PR, "install gh (https://cli.github.com)", lambda: _gh_logged_in(agents), on_machine)
    add("gitleaks", NEEDED_FOR_PR, GITLEAKS_INSTALL, lambda: _gitleaks_runs(agents), on_machine)
    if cwd is not None:
        checkout = add("git checkout", NEEDED_FOR_PR,
                       "pass --cwd a git checkout, or make one with git clone or git init",
                       lambda: _checkout(agents, cwd), git)
        add("origin", NEEDED_FOR_PR, f"git remote add {REMOTE} <url>",
            lambda: agents.git(checkout.detail, "remote", "get-url", REMOTE).strip(), checkout)
    checks.append(_tkinter_check())
    return checks


def _check_unless_skipped(name: str, level: str, fix: str, probe: Callable[[], str], needs: Check | None = None,
                          note: str = "") -> Check:
    """_run_check's Check of probe, or, when needs is given and not ok, a skipped Check naming it without running
    probe; note is added to the detail of a Check that is not ok."""
    check = _skipped(name, level, needs) if needs is not None and not needs.ok else _run_check(name, level, fix, probe)
    if not check.ok and note:
        check = Check(name, level, False, f"{check.detail}; {note}", check.fix)
    return check


def _skipped(name: str, level: str, needs: Check) -> Check:
    """The Check of name, skipped because needs is not ok: not ok itself, its fix pointing at needs."""
    return Check(name, level, False, f"skipped: needs {needs.name}", f"fix {needs.name} first")


def _run_check(name: str, level: str, fix: str, probe: Callable[[], str]) -> Check:
    """The Check of probe, which returns the detail when the requirement is met and raises OrchestratorError, such as
    _Unmet, when it is not or cannot be checked; fix is the Check's when the error brings none."""
    try:
        return Check(name, level, True, probe())
    except OrchestratorError as e:
        return Check(name, level, False, str(e), getattr(e, "fix", None) or fix)


def _reach_herdr(host: Host, herdr: Herdr, local_herdr: Check) -> tuple[Host | None, Check]:
    """The Host the agents run on, None when it cannot be reached, and the Check of reaching herdr: as connect
    needs it, from a herdr pane without herdr.machine; with it, the machine saved and enabled, and its SSH target
    answering non-interactive SSH, which is skipped when local_herdr, the Check of herdr here, is not ok."""
    name = "reaching herdr"
    if herdr.machine and not local_herdr.ok:
        return None, _skipped(name, REQUIRED, local_herdr)
    if not herdr.machine:
        if in_herdr_pane():
            return host, Check(name, REQUIRED, True, "inside a herdr pane")
        return host, Check(name, REQUIRED, False, "not inside a herdr pane", "run from a herdr pane, or pass --machine")
    try:
        target = herdr.ssh_target()
        remote = host.over_ssh(target)
        remote.check(["true"])
    except OrchestratorError as e:
        return None, Check(name, REQUIRED, False, str(e), f"save herdr machine {herdr.machine} and enable it, with "
                                                          f"non-interactive SSH to its target")
    return remote, Check(name, REQUIRED, True, f"herdr machine {herdr.machine}, over SSH to {target}")


def _on_path(host: Host, command: str) -> str:
    """Where command is on host's PATH; raises _Unmet when it is not there."""
    proc = host.run(["sh", "-c", 'command -v -- "$1"', "_", command])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise _Unmet(f"{command} is not on PATH")
    return proc.stdout.strip()


# The exit status a POSIX shell gives a command it cannot find, as the shell an SSH command runs in does.
COMMAND_NOT_FOUND_EXIT = 127


def _run_tool(host: Host, argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    """host.run(argv, timeout=timeout); raises the _Unmet _on_path raises when argv[0] is not on host's PATH: it
    cannot be started here, or over SSH the remote shell exits COMMAND_NOT_FOUND_EXIT. Raises OrchestratorError as
    Host.run does for any other failure to run it."""
    try:
        proc = host.run(argv, timeout=timeout)
    except OrchestratorError as e:
        if isinstance(e.__cause__, FileNotFoundError):
            raise _Unmet(f"{argv[0]} is not on PATH") from e
        raise
    if host.ssh_target and proc.returncode == COMMAND_NOT_FOUND_EXIT:
        raise _Unmet(f"{argv[0]} is not on PATH")
    return proc


def _tool_version(host: Host, command: str, minimum: tuple[int, ...]) -> str:
    """What `command --version` prints, once it is on host's PATH and the first version number it prints is minimum
    or later; raises _Unmet when it is missing, prints no version or an older one, and OrchestratorError when it
    fails."""
    argv = [command, "--version"]
    said = host.checked(argv, _run_tool(host, argv)).strip()
    number = re.search(r"\d+(?:\.\d+)+", said)
    if number is None:
        raise _Unmet(f"{command} --version prints no version number: {said!r}")
    if tuple(int(n) for n in number.group().split(".")) < minimum:
        raise _Unmet(f"{command} {number.group()} is older than {dotted(minimum)}")
    return said


class _IntegrationStatus:
    """The states `herdr integration status` gives the harnesses on a host, asked once, when first needed."""

    def __init__(self, host: Host):
        self._host = host
        self._states: dict[str, str] | _Unmet | None = None  # by harness kind; or why herdr could not be asked

    def current(self, kind: str) -> str:
        """The state of kind's integration when it is current, such as "current (v10)"; raises _Unmet when it is in
        any other, or not listed, and _Unmet with herdr's fix when herdr cannot be asked."""
        if self._states is None:
            try:
                self._states = integration_states(self._host.check(["herdr", "integration", "status"]))
            except OrchestratorError as e:
                self._states = _Unmet(str(e), "update herdr with `herdr update`, then check that "
                                              "`herdr integration status` runs")
        if isinstance(self._states, OrchestratorError):
            raise self._states
        state = self._states.get(kind)
        if state is None:
            raise _Unmet(f"herdr integration status does not list {kind}")
        if not state.startswith("current"):
            raise _Unmet(state)
        return state


def integration_states(listing: str) -> dict[str, str]:
    """Each harness's integration state, by its kind, from `herdr integration status`, whose lines read
    `<kind>: current (vN) (<path>)`, `<kind>: not installed (<path>)` or the like for another state, and a kind may
    carry a note, as `letta (experimental): ...`. The state is what follows the colon, the path left out."""
    states = {}
    for line in listing.splitlines():
        if m := re.fullmatch(r"(\S+)[^:]*: (.*?)(?: \([^()]*\))?", line.strip()):
            states[m.group(1)] = m.group(2)
    return states


def _gh_logged_in(host: Host) -> str:
    """That gh is on host's PATH and logged in; raises _Unmet with the fix for what is not."""
    proc = _run_tool(host, ["gh", "auth", "status"], timeout=NETWORK_TIMEOUT)
    if proc.returncode != 0:
        said = " ".join(line.strip() for line in (proc.stderr.strip() or proc.stdout.strip()).splitlines()[:4])
        raise _Unmet(f"gh auth status fails{': ' + said if said else ''}", "gh auth login")
    return "logged in"


def _gitleaks_runs(host: Host) -> str:
    """That `gitleaks git` runs on host, as the pre-push check asks it; raises _Unmet with why it does not."""
    problem = host.gitleaks_problem()
    if problem:
        raise _Unmet(problem)
    return "`gitleaks git` runs"


def _checkout(host: Host, cwd: str) -> str:
    """The top level of the git checkout cwd is in on host, a leading ~ in cwd being host's home as Host.resolve_dir
    reads it; raises _Unmet when cwd is no directory there, or in no checkout."""
    try:
        path = host.resolve_dir(cwd)
    except OrchestratorError as e:
        raise _Unmet(f"{cwd} is not a directory: {e}") from e
    proc = host.git_run(path, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        said = proc.stderr.strip().splitlines()
        raise _Unmet(f"{cwd} is not in a git checkout{': ' + said[0] if said else ''}")
    return proc.stdout.strip()


def _tkinter_check() -> Check:
    """Whether this Python, where the orchestrator runs, has tkinter, which only the GUI needs."""
    # Imported here, as gui_command does, so the CLI and the tests run on a Python without tkinter.
    try:
        import tkinter
    except ImportError:
        return Check("tkinter", OPTIONAL, False, "this Python lacks it; needed only for the GUI",
                     tk_install_hint(sys.platform, sys.version_info))
    return Check("tkinter", OPTIONAL, True, f"Tk {tkinter.TkVersion}")


def new_state(args: argparse.Namespace, cwd: str, spec: SpecFile | None = None) -> tuple[RunState, Pipeline]:
    """A new run's state from the run command's flags, and its workflow; with spec, from --spec, the run
    starts after the contract step. Its prompts are the prompt files' as they are now (load_prompts); raises
    OrchestratorError as load_prompts does, and for a workflow that cannot be found or loaded."""
    prompts = load_prompts()
    pipeline = flagged_workflow(args, prompts)
    first = seeded_start(pipeline) if spec else pipeline.steps[0]
    # parse_args leaves the task out only with --spec.
    task = args.task if args.task is not None else spec.task
    state = RunState(new_run_id(), task, cwd, args.machine, phase=first.id, workflow=pipeline.name,
                     prompt_overrides=prompts.overrides)
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


def flagged_workflow(args: argparse.Namespace, prompts: Prompts) -> Pipeline:
    """The workflow run's or drain's --workflow-file or --workflow names, worded by prompts."""
    if args.workflow_file:
        return load_workflow_file(args.workflow_file, prompts)
    return named_workflow(args.workflow, prompts)


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
    """The state of the run args.run_ref names among cwd's runs, as resumable_state finds it, and its workflow, with
    the quality gate turned on or off as args.quality_gate or args.no_quality_gate asks (turn_quality_gate_on and
    _off: a CI preflight, and deleting the run's refs on origin). Changes nothing on disk; raises OrchestratorError
    when the run cannot be resumed or its gate cannot be switched."""
    state = resumable_state(host.run_states(cwd), args, cwd, socket.gethostname(), pid_alive)
    pipeline = run_pipeline(state.workflow, state.workflow_definition, state.prompts)
    if args.quality_gate:
        turn_quality_gate_on(state, pipeline, host, CI(args.quality_gate))
    elif args.no_quality_gate:
        turn_quality_gate_off(state, pipeline, host)
    return state, pipeline


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
    if status.startswith((CHANGES_REQUESTED, STALE, BLOCKED_STATUS)):
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
    """The runs `list` shows for the project."""
    host, cwd = project_host(machine, cwd)
    return host.run_states(cwd)


def project_host(machine: str, cwd: str) -> tuple[Host, str]:
    """The Host that reaches the project, on the saved herdr machine or, with machine empty, here, and the project's
    absolute directory on it; cwd may start with ~.

    Without connect's check for a herdr pane, which guards the herdr commands a run sends: reading a project sends
    none, so a window started from a file manager reads it too. Raises OrchestratorError when the machine is not
    saved or enabled, or the directory cannot be entered.
    """
    host = Host(Herdr(machine).ssh_target()) if machine else Host()
    return host, host.resolve_dir(cwd)


@contextlib.contextmanager
def _not_ignored_in_children(sig: signal.Signals | None) -> Iterator[None]:
    """While open, a process this one starts gets sig with its default disposition, also when this process ignores
    it, as a command a Jenkins sh step runs ignores SIGINT; afterwards this process treats sig as it did before. The
    disposition is the process's, so a child another thread starts meanwhile, such as the GUI's ssh, gets it too. An
    ignored signal stays ignored across exec, and a Python started so never raises KeyboardInterrupt, so a soft stop
    by it would reach a tool that ignores it. Meanwhile sig is caught and dropped here, as ignoring it would. Only the
    main thread can set a handler, so elsewhere, and for sig None, nothing changes. gate.py has its own copy, for the
    tools it starts."""
    if (sig is None or signal.getsignal(sig) != signal.SIG_IGN
            or threading.current_thread() is not threading.main_thread()):
        yield
        return
    # A handler is reset to the default on exec, which ignoring is not.
    signal.signal(sig, lambda *_: None)
    try:
        yield
    finally:
        signal.signal(sig, signal.SIG_IGN)


class RunProcess:
    """A CLI command the GUI started, its stdout and stderr read together on a thread."""

    def __init__(self, argv: list[str], popen=subprocess.Popen):
        # A session of its own, so Stop can signal its process group as Ctrl-C in a terminal does; and SIGINT not
        # ignored there, which a GUI started as a background job of a non-interactive shell would pass on (the
        # with below, not the session, is what keeps it). Unbuffered, so the output arrives as it is printed rather
        # than when the run ends.
        with _not_ignored_in_children(signal.SIGINT):
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
        root.title("Octoflow AI")
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
        ttk.Label(h, text="Octoflow AI", style="Title.TLabel").grid(row=0, column=1, sticky="sw")
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
            # The roles only, which no prompt file changes.
            workflow = named_workflow(self.vars["workflow"].get(), BUILT_IN_PROMPTS)
            line, style = role_chain(workflow), PIPELINE_LABEL
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


# ---------------------------------------------------------------------------
# serve: a project's runs, read-only, over HTTP for the desktop app
# ---------------------------------------------------------------------------

# How often an event stream reads its project's runs again, in seconds. A read of a local project starts a few short
# processes; on a --machine, each of them is an SSH round trip.
SERVE_POLL_SECONDS = 1
SERVE_REMOTE_POLL_SECONDS = 5
# The keys of a run record that grow with every read of it while nothing happens; a change in them alone is no event.
# The client ages heartbeat_age itself from when it got the record.
AGING_RECORD_KEYS = ("heartbeat_age", "health_reason")


class RunPathRefused(OrchestratorError):
    """A run id or file name given to serve that is not one plain name in the run directory, or names state.json."""


class ProjectRuns:
    """One project's runs and their files, as serve's endpoints give them.

    host reaches the project, whose absolute directory on it is cwd. target is the --machine and --cwd flags as the
    client gave them (target_args), for the records' resume commands.
    """

    def __init__(self, host: Host, cwd: str, target: list[str]):
        self.host = host
        self.cwd = cwd
        self.target = target

    @property
    def remote(self) -> bool:
        """Whether the project is on another machine, read over SSH."""
        return bool(self.host.ssh_target)

    def records(self) -> list[dict]:
        """Each run's run_record, as `list --json` prints them. Raises OrchestratorError when the runs cannot be
        read."""
        return [run_record(s, age, socket.gethostname(), pid_alive, self.target)
                for age, s in self.host.run_states(self.cwd)]

    def files(self, run_id: str) -> list[dict]:
        """The run's files, oldest first, each {"name", "mtime"} with mtime in seconds since the epoch; [] for a run
        without a directory. Raises RunPathRefused for a run id that is not a plain name, and OrchestratorError when
        the directory cannot be listed."""
        return [{"name": posixpath.basename(f.path), "mtime": f.mtime} for f in self.host.run_files(self._dir(run_id))]

    def read(self, run_id: str, name: str) -> str | None:
        """The text of the run's file name, None when there is no such file. Raises RunPathRefused, having read
        nothing, for a run id or name that is not a plain name or for state.json, and OrchestratorError when the file
        cannot be read."""
        if name == "state.json":
            raise RunPathRefused("state.json is not a handoff file")
        return self.host.read(f"{self._dir(run_id)}/{plain_name(name)}")

    def _dir(self, run_id: str) -> str:
        return f"{self.cwd}/{RUNS_DIR}/{plain_name(run_id)}"


def plain_name(name: str) -> str:
    """name, if it names an entry of a directory itself: not empty, . or .., and without a slash or NUL. Raises
    RunPathRefused otherwise."""
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise RunPathRefused(f"{name!r} is not a name in the run directory")
    return name


class RunWatch:
    """What one event stream has told its client about a project's runs, and the events that bring it up to date."""

    def __init__(self, project: ProjectRuns):
        self.project = project
        # By run id, the run's record without AGING_RECORD_KEYS and its files, as last sent.
        self._sent: dict[str, tuple[dict, list[dict]]] = {}
        # The error the last event of kind problem reported; None when there was none, or it cleared.
        self._problem: str | None = None

    def poll(self) -> list[tuple[str, dict]]:
        """The events since the last poll, each as (kind, payload):

        - ("record", {"run", "record"}): the run's run_record, for a run that appeared or whose record changed other
          than in AGING_RECORD_KEYS; record is null for a run whose state.json is gone.
        - ("files", {"run", "files"}): the run's files, as ProjectRuns.files lists them, when they changed.
        - ("problem", {"error"}): the project could not be read, and why; error is null once it can be again.

        The first poll sends every run's record and files. A finished or failed run's files are read again only
        when its record changes: nothing drives it.
        """
        try:
            events = self._changes()
        except OrchestratorError as e:
            return self._problem_events(str(e))
        return self._problem_events(None) + events

    def _changes(self) -> list[tuple[str, dict]]:
        events = []
        sent = {}
        for record in self.project.records():
            run_id = record["id"]
            key = {k: v for k, v in record.items() if k not in AGING_RECORD_KEYS}
            old_key, old_files = self._sent.get(run_id, (None, None))
            if key != old_key:
                events.append(("record", {"run": run_id, "record": record}))
            files = old_files if old_files is not None and key == old_key and record["health"] is None \
                else self.project.files(run_id)
            if files != old_files:
                events.append(("files", {"run": run_id, "files": files}))
            sent[run_id] = (key, files)
        events += [("record", {"run": run_id, "record": None}) for run_id in sorted(self._sent.keys() - sent.keys())]
        self._sent = sent
        return events

    def _problem_events(self, error: str | None) -> list[tuple[str, dict]]:
        if error == self._problem:
            return []
        self._problem = error
        return [("problem", {"error": error})]


def sse_event(kind: str, payload: dict) -> str:
    """One Server-Sent Event of the kind, its payload as the one line of JSON in its data field."""
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"


class RunServer(http.server.ThreadingHTTPServer):
    """serve's HTTP server, on a free port of 127.0.0.1, with its token; RunRequestHandler answers its requests.

    origins are the Origin headers besides its own that it answers and allows by CORS: the desktop app's webview.
    connect gives the Host that reaches a project and the project's absolute directory, from the machine (empty for
    this one) and directory a request names, as project_host does; each pair is connected once. Raises OSError when
    no port can be bound.
    """

    daemon_threads = True  # an event stream never ends by itself, so shutting down cannot wait for it

    def __init__(self, origins: Iterable[str] = (),
                 connect: Callable[[str, str], tuple[Host, str]] = project_host):
        super().__init__(("127.0.0.1", 0), RunRequestHandler)
        port = self.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self.token = secrets.token_urlsafe(32)
        # The Host headers a request may carry: anything else came through a name that only resolves here, as a DNS
        # rebinding page's does.
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", *origins}
        # Set when the server shuts down, which ends every event stream.
        self.stopping = threading.Event()
        self._connect = connect
        self._projects: dict[tuple[str, str], ProjectRuns] = {}
        self._projects_lock = threading.Lock()

    def project(self, machine: str, cwd: str) -> ProjectRuns:
        """The project in directory cwd, which may start with ~, on the saved herdr machine, or here with machine
        empty. Raises OrchestratorError when it cannot be reached."""
        with self._projects_lock:
            project = self._projects.get((machine, cwd))
        if project is None:
            host, path = self._connect(machine, cwd)
            project = ProjectRuns(host, path, target_args(argparse.Namespace(machine=machine, cwd=cwd)))
            with self._projects_lock:
                self._projects[machine, cwd] = project
        return project


class RunRequestHandler(http.server.BaseHTTPRequestHandler):
    """The endpoints of a RunServer, each a GET of JSON naming the project by ?cwd= and an optional ?machine=:

    - /runs: {"version", "runs"}, as `list --json` prints it.
    - /files?run=ID: {"run", "files"}, the run's files as ProjectRuns.files lists them.
    - /file?run=ID&name=NAME: {"run", "name", "text"}; 403 for a name outside the run directory or state.json,
      404 for no such file.
    - /events: a text/event-stream of RunWatch's events, polled every SERVE_POLL_SECONDS, every
      SERVE_REMOTE_POLL_SECONDS for a --machine project, until the client goes or the server stops.

    Every request needs the server's token as an `Authorization: Bearer` header or, for an EventSource, which cannot
    set one, as ?token=; else 401. A Host header other than the server's own address, or an Origin other than its own
    or one of its origins, gets 403. 400 is a missing parameter, 502 a project that could not be read, with
    {"error"}.
    """

    server: RunServer

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Log nothing for a request answered: the app polls every second. Errors are still logged, to standard
        error."""

    def do_OPTIONS(self) -> None:
        """Answer a CORS preflight that passes the Host and Origin checks with 204, allowing GET with an
        Authorization header. A browser sends no Authorization header with a preflight, so there is no token to
        check."""
        if self._refused():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._allow_origin()
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        """Answer a GET of one of the endpoints, as the class docstring lists them."""
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
        if self._refused():
            return
        if not self._has_token(query.get("token", "")):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "the token is missing or wrong"})
            return
        # Each endpoint, with the parameters it needs besides cwd.
        endpoints = {"/runs": (self._runs, ()), "/files": (self._files, ("run",)),
                     "/file": (self._file, ("run", "name")), "/events": (self._events, ())}
        if url.path not in endpoints:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"no endpoint {url.path}"})
            return
        endpoint, params = endpoints[url.path]
        if missing := [name for name in ("cwd", *params) if name not in query]:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"missing parameter {', '.join(missing)}"})
            return
        try:
            endpoint(self.server.project(query.get("machine", ""), query["cwd"]), query)
        except RunPathRefused as e:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": str(e)})
        except OrchestratorError as e:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})

    def _runs(self, project: ProjectRuns, query: dict[str, str]) -> None:
        self._send_json(HTTPStatus.OK, {"version": RUN_RECORD_VERSION, "runs": project.records()})

    def _files(self, project: ProjectRuns, query: dict[str, str]) -> None:
        self._send_json(HTTPStatus.OK, {"run": query["run"], "files": project.files(query["run"])})

    def _file(self, project: ProjectRuns, query: dict[str, str]) -> None:
        text = project.read(query["run"], query["name"])
        if text is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"no file {query['name']} in run {query['run']}"})
        else:
            self._send_json(HTTPStatus.OK, {"run": query["run"], "name": query["name"], "text": text})

    def _events(self, project: ProjectRuns, query: dict[str, str]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self._allow_origin()
        self.end_headers()
        watch = RunWatch(project)
        interval = SERVE_REMOTE_POLL_SECONDS if project.remote else SERVE_POLL_SECONDS
        while not self.server.stopping.is_set():
            # A comment when nothing changed, so a client that went away fails the write and ends the stream.
            chunk = "".join(sse_event(kind, payload) for kind, payload in watch.poll()) or ":\n\n"
            try:
                self.wfile.write(chunk.encode())
                self.wfile.flush()
            except OSError:
                return
            self.server.stopping.wait(interval)

    def _refused(self) -> bool:
        """Whether the request came through another name than the server's address, or from a foreign origin;
        answered with 403 if so."""
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in self.server.hosts or (origin is not None and origin not in self.server.origins):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "the Host or Origin header is not this server's"})
            return True
        return False

    def _has_token(self, query_token: str) -> bool:
        # As bytes: compare_digest refuses a str with a non-ASCII character, which a client can send in either place.
        # surrogatepass encodes any str, lone surrogates too.
        def digest(text: str) -> bytes:
            return text.encode("utf-8", "surrogatepass")

        token = self.server.token
        header = self.headers.get("Authorization", "")
        return (secrets.compare_digest(digest(header), digest(f"Bearer {token}"))
                or secrets.compare_digest(digest(query_token), digest(token)))

    def _allow_origin(self) -> None:
        # Only after _refused, so an Origin here is one the server answers.
        if origin := self.headers.get("Origin"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send_json(self, code: HTTPStatus, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if code != HTTPStatus.FORBIDDEN:
            self._allow_origin()
        if code == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(payload)


def serve_command(origins: list[str]) -> int:
    """Start a RunServer that also answers origins, print {"url", "token"} as one JSON line, and serve until SIGINT or
    SIGTERM; returns 0 then. Returns EXIT_ERROR when no port can be bound, or when the line cannot be printed, as when
    whoever started serve has stopped reading its standard output: nobody could reach the server then."""
    try:
        server = RunServer(origins)
    except OSError as e:
        print(f"error: cannot serve on 127.0.0.1: {e}", file=sys.stderr)
        return EXIT_ERROR
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    # Serving before the line is printed: shutdown waits for serve_forever to stop, so it must have started.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        try:
            print(json.dumps({"url": server.url, "token": server.token}), flush=True)
        except OSError as e:
            print(f"error: cannot print the server's address: {e}", file=sys.stderr)
            # The line stays in stdout's buffer, and the flush at exit would fail again; see
            # https://docs.python.org/3/library/signal.html#note-on-sigpipe
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            return EXIT_ERROR
        stop.wait()
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
