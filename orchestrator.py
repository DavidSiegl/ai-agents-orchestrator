"""
Role-based handoff orchestrator for Claude Code in herdr:

    Spec Collector -> Builder -> Reviewer  (review findings loop back to the Builder)

Each role is a separate interactive Claude Code session in its own herdr pane,
so no role judges its own work and the human can watch or step into any of
them. Roles hand off through Markdown files in the run directory, never
through scraped terminal output.

The agents may run on a saved herdr machine (--machine). The run directory
then lives on that machine, so every file and git access goes through Host,
which runs commands locally or over SSH.
"""

import argparse
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone

RUNS_DIR = ".orchestrator/runs"
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

APPROVE = "APPROVE"
CHANGES_REQUESTED = "CHANGES_REQUESTED"

ROLE_LABELS = {"spec": "Spec Collector", "build": "Builder", "review": "Reviewer"}

EXIT_ERROR = 1
EXIT_CHANGES_REQUESTED = 3
EXIT_INTERRUPTED = 130


class OrchestratorError(Exception):
    """A failure that ends the run; the message is shown to the user."""


class HerdrError(OrchestratorError):
    def __init__(self, code: str, message: str):
        super().__init__(f"herdr {code}: {message}")
        self.code = code


# ---------------------------------------------------------------------------
# Role prompts
# ---------------------------------------------------------------------------

SPEC_PROMPT = """\
You are the Spec Collector, the first of three roles (Spec Collector -> Builder -> Reviewer). \
Separate Claude Code sessions play the Builder and the Reviewer; they will know only what you write down. \
A human is at this terminal and answers you directly.

Task from the human:
{task}

Interview the human until the requirements are unambiguous: the goal, what is in and out of scope, \
testable acceptance criteria, constraints, and how the result will be verified. \
Read the code in {cwd} first so your questions are grounded and you can cite the files the change touches. \
Ask a few questions at a time. Do not write or change any code.

When the human approves the spec, write it in a single write to {spec_path} as Markdown with these sections: \
Goal, Scope, Non-goals, Acceptance criteria (a numbered list, each one checkable), Relevant code (file:line), Verification. \
Writing that file hands the work to the Builder, so write it only after the human approves it."""

BUILD_PROMPT = """\
You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). \
The spec in {spec_path} was agreed with the human by a separate session; it is your contract.

Implement it in {cwd}, following the conventions of the surrounding code. \
Verify the change the way the spec's Verification section says, and run the tests. \
Do not commit or push; leave the changes in the working tree for the Reviewer. \
If the spec is wrong or cannot be met, do not deviate silently: say so in your report.

As your last step, write a report to {report_path} in a single write; it hands the work to the Reviewer: the files you changed and why, \
how you verified the change (commands and a summary of their results), \
and any acceptance criterion you did not meet, with the reason."""

FIX_PROMPT = """\
The Reviewer requested changes; the findings are in {review_path}. \
Fix each finding, or explain in your report why it is wrong. Re-run the verification. \
Do not commit or push. As your last step, write a new report to {report_path} in a single write, in the same shape as before, \
answering each finding by its number."""

REVIEW_PROMPT = """\
You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). \
You did not write this change. Judge it only against the spec in {spec_path} and the code itself.

The change: {change}
The Builder's report is in {report_path}. Treat its claims as unverified: check them, and run the verification yourself. \
Do not modify any file other than your review.

As your last step, write your review to {review_path} in a single write. Its first line must be exactly `VERDICT: {approve}` or `VERDICT: {changes}`. \
Then list numbered findings, each with file:line, what is wrong, and which acceptance criterion it violates \
or what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. \
Style preferences are not defects."""

RECHECK_PROMPT = """\
The Builder has answered your review; the new report is in {report_path}. \
Review the change again ({change}) against the spec in {spec_path} and your previous findings, \
checking the Builder's claims rather than trusting them. \
As your last step, write the review to {review_path} in a single write, with the same first-line verdict and numbered findings as before."""

# For a role relaunched into its saved Claude Code session after it had already been prompted.
CONTINUE_PROMPT = """\
Your session was restarted in the middle of this turn. Continue where you left off; \
the turn still ends when you write {path} in a single write."""

# Appended for a Builder or Reviewer that starts a fresh session in a later round: it has not seen the earlier ones.
REBUILD_NOTE = """\
This is round {n}, and you are a fresh session. An earlier Builder session did the previous rounds; \
its changes are already in the working tree, and its reports are {reports}."""

REREVIEW_NOTE = """\
This is round {n}, and you are a fresh session. The earlier reviews of this change are {reviews}; \
check that the Builder has answered each of their findings."""


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
            proc = self._run(["herdr", *args], capture_output=True, text=True, timeout=limit)
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

    def start_agent(self, name: str, pane: str, agent_args: list[str]) -> bool:
        """Start Claude Code in the pane. False means it is blocked on a startup dialog."""
        args = ["agent", "start", name, "--kind", "claude", "--pane", pane,
                "--timeout", str(AGENT_START_TIMEOUT_MS)]
        if agent_args:
            args += ["--", *agent_args]
        try:
            self.call(*args, limit=AGENT_START_TIMEOUT_MS / 1000 + 60)
        except HerdrError as e:
            # Such as the folder-trust question in a directory Claude Code has not seen.
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

    def notify(self, title: str, body: str) -> None:
        self.call("notification", "show", title, "--body", body, "--sound", "request")


def agent_session(agent: dict) -> str | None:
    """The Claude Code session id herdr reports for an agent, once it knows it."""
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


class Host:
    """Runs commands on the machine where the agents run: locally, or over SSH."""

    def __init__(self, ssh_target: str | None = None, run=subprocess.run):
        self.ssh_target = ssh_target
        self._run = run

    def run(self, argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        if self.ssh_target:
            # BatchMode fails fast instead of hanging on a password prompt nobody can see.
            argv = ["ssh", "-o", "BatchMode=yes", self.ssh_target, shlex.join(argv)]
        try:
            return self._run(argv, input=stdin, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OrchestratorError(f"{shlex.join(argv)}: {e}") from e

    def check(self, argv: list[str], stdin: str | None = None) -> str:
        proc = self.run(argv, stdin)
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

    def write(self, path: str, text: str) -> None:
        # Written aside and renamed into place, so a concurrent `list` never reads half a state.json.
        script = ('t="$1.tmp.$$"; mkdir -p -- "$(dirname -- "$1")" && cat > "$t" && mv -f -- "$t" "$1" '
                  '|| { rm -f -- "$t"; exit 1; }')
        self.check(["sh", "-c", script, "_", path], stdin=text)

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
    phase: str = "spec"
    round: int = 0
    workspace_id: str = ""
    # The Spec Collector's pane, which the Builder's pane is split from.
    root_pane: str = ""
    base: str | None = None
    verdict: str | None = None
    error: str | None = None
    # Per role: the agent's name, its pane and, once herdr reports it, its Claude Code session id.
    agents: dict[str, dict[str, str]] = field(default_factory=dict)
    max_rounds: int = DEFAULT_MAX_ROUNDS
    turn_timeout: int = DEFAULT_TURN_TIMEOUT
    agent_args: list[str] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)
    # Basename of the handoff file whose prompt was delivered, so a resume does not prompt for it again.
    prompted: str | None = None
    # The orchestrator process driving the run, {"host", "pid", "started_at"}; None while none does.
    owner: dict | None = None
    heartbeat_at: str | None = None

    @classmethod
    def from_dict(cls, saved: dict) -> "RunState":
        """A saved state, including one written before some of these fields existed."""
        known = {f.name for f in fields(cls)}
        try:
            state = cls(**{k: v for k, v in saved.items() if k in known})
        except TypeError as e:
            raise OrchestratorError(f"run state {saved.get('run_id', '?')} is incomplete: {e}") from e
        if not state.root_pane and "spec" in state.agents:
            state.root_pane = state.agents["spec"]["pane"]
        return state

    @property
    def key(self) -> str:
        """The run id's random suffix, which names its agents and labels its workspace."""
        return self.run_id.rsplit("-", 1)[-1]

    @property
    def dir(self) -> str:
        return f"{self.cwd}/{RUNS_DIR}/{self.run_id}"

    @property
    def spec_path(self) -> str:
        return f"{self.dir}/spec.md"

    def build_path(self, n: int) -> str:
        return f"{self.dir}/build-{n}.md"

    def review_path(self, n: int) -> str:
        return f"{self.dir}/review-{n}.md"


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def parse_verdict(review: str) -> str | None:
    """The verdict on the review's first non-blank line, tolerating Markdown emphasis around it."""
    for line in review.splitlines():
        if line.strip():
            m = re.search(rf"VERDICT:\s*({APPROVE}|{CHANGES_REQUESTED})\b", line)
            return m.group(1) if m else None
    return None


def log(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


class RunTakenOver(OrchestratorError):
    """Another orchestrator process owns the run now, so this one stops without saving."""


# How a role's agent came to be ready for a turn, which decides what it is prompted with.
NEW = "new"              # the role's first agent in this run
ALIVE = "alive"          # still running from before
RESUMED = "resumed"      # had exited; relaunched into its saved Claude Code session
RESTARTED = "restarted"  # had exited; relaunched in a fresh session that has lost its earlier turns


class Workflow:
    """Drives one run through Spec Collector -> Builder -> Reviewer in a herdr workspace.

    A role's turn ends when it writes its handoff file, not when herdr reports it
    settled: Claude Code ends a turn while a background task it started is still
    running and resumes when the task completes, so idle or done can come mid-work.

    The same code starts a new run and resumes an interrupted one. A new run is a
    resume from phase spec with no workspace; every step first looks for what an
    earlier orchestrator, or a role working while none was watching, already did.
    """

    def __init__(self, herdr: Herdr, host: Host, state: RunState, *,
                 notify, max_rounds: int = DEFAULT_MAX_ROUNDS,
                 turn_timeout: int = DEFAULT_TURN_TIMEOUT,
                 agent_args: list[str] | None = None,
                 models: dict[str, str] | None = None,
                 sleep=time.sleep, clock=time.monotonic, wallclock=time.time):
        self.herdr = herdr
        self.host = host
        self.state = state
        self.notify = notify
        # Saved with the run, so a resume starts from them.
        state.max_rounds = max_rounds
        state.turn_timeout = turn_timeout
        state.agent_args = agent_args or []
        state.models = models or {}
        self.sleep = sleep
        self.clock = clock
        self.wallclock = wallclock
        self.me = {"host": socket.gethostname(), "pid": os.getpid(), "started_at": self._timestamp()}
        self._last_write = 0.0

    def run(self) -> str:
        """Run every phase not yet done and return the final verdict."""
        s = self.state
        if s.phase == "done":
            return s.verdict
        try:
            self._claim()
            self._prepare()
            if s.phase == "spec":
                self._collect_spec()
                # Only here, before the first build: every later phase, resumed or not, diffs
                # against this base, even if the human commits the Builder's work meanwhile.
                s.base = self.host.git_head(s.cwd)
                s.phase, s.round = "build", 1
                self._save()
            return self._build_and_review()
        except RunTakenOver:
            raise
        except OrchestratorError as e:
            self._release(str(e))
            raise
        except KeyboardInterrupt:
            self._release("interrupted")
            raise

    def _claim(self) -> None:
        s = self.state
        ignore = f"{s.cwd}/.orchestrator/.gitignore"
        if self.host.read(ignore) is None:
            # Keeps run files out of `git status`, so the Reviewer sees only the Builder's changes.
            self.host.write(ignore, "*\n")
        s.error = None
        s.owner = self.me
        # Not _save: the caller has already decided that any earlier owner is gone.
        self._write_state()

    def _release(self, error: str) -> None:
        """Record why the run stopped and that no process drives it now, as far as saving still works."""
        self.state.error = error
        self.state.owner = None
        try:
            self._save()
        except OrchestratorError as e:
            log(f"could not record the failure in state.json: {e}")

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
        s.workspace_id, s.root_pane = self.herdr.create_workspace(s.cwd, label)
        log(f"run {s.run_id} in herdr workspace {s.workspace_id}")
        self._save()

    def _collect_spec(self) -> None:
        s = self.state

        def announce(how: str) -> None:
            name, pane = s.agents["spec"]["name"], s.agents["spec"]["pane"]
            self.herdr.focus(name)
            what = "restarted; the interview starts over" if how == RESTARTED else "is waiting for you"
            self.notify(f"Spec Collector {what}", f"pane {pane}: {s.task}")
            log(f"[spec] answer the Spec Collector in pane {pane}")

        # The interview is paced by the human, so it has no deadline, and an idle
        # collector is normal: it is waiting for the human's reply.
        self._turn("spec", SPEC_PROMPT.format(task=s.task, cwd=s.cwd, spec_path=s.spec_path),
                   s.spec_path, timeout=None, watch_stalls=False, announce=announce)

    def _build_and_review(self) -> str:
        s = self.state
        while True:
            n = s.round
            if s.phase == "build":
                text, fresh = self._build_prompts(n)
                self._turn("build", text, s.build_path(n), s.turn_timeout, fresh_text=fresh)
                s.phase = "review"
                self._save()
            text, fresh = self._review_prompts(n)
            review = self._turn("review", text, s.review_path(n), s.turn_timeout, fresh_text=fresh)
            s.verdict = parse_verdict(review)
            if s.verdict is None:
                self._reject(s.review_path(n), "does not start with a VERDICT line")
            log(f"[review] round {n}: {s.verdict}")
            if s.verdict == APPROVE or n >= s.max_rounds:
                break
            s.phase, s.round = "build", n + 1
            self._save()

        s.phase = "done"
        s.owner = None
        self._save()
        self.notify(f"Run finished: {s.verdict}", s.task)
        return s.verdict

    def _build_prompts(self, n: int) -> tuple[str, str | None]:
        """The Builder's prompt for round n, and the one for a fresh session that has not seen rounds before n."""
        s = self.state
        first = BUILD_PROMPT.format(spec_path=s.spec_path, cwd=s.cwd, report_path=s.build_path(n))
        if n == 1:
            return first, None
        fix = FIX_PROMPT.format(review_path=s.review_path(n - 1), report_path=s.build_path(n))
        note = REBUILD_NOTE.format(n=n, reports=", ".join(s.build_path(i) for i in range(1, n)))
        return fix, f"{first}\n\n{note}\n\n{fix}"

    def _review_prompts(self, n: int) -> tuple[str, str | None]:
        """The Reviewer's prompt for round n, and the one for a fresh session that has not seen rounds before n."""
        s = self.state
        fill = dict(spec_path=s.spec_path, change=self._change_description(),
                    report_path=s.build_path(n), review_path=s.review_path(n),
                    approve=APPROVE, changes=CHANGES_REQUESTED)
        first = REVIEW_PROMPT.format(**fill)
        if n == 1:
            return first, None
        note = REREVIEW_NOTE.format(n=n, reviews=", ".join(s.review_path(i) for i in range(1, n)))
        return RECHECK_PROMPT.format(**fill), f"{first}\n\n{note}"

    def _turn(self, role: str, text: str, path: str, timeout: int | None, *,
              fresh_text: str | None = None, watch_stalls: bool = True, announce=None) -> str:
        """Return the handoff file that ends the role's turn, prompting the role only if it still owes it.

        fresh_text replaces text for an agent in a new session, which has not seen the role's
        earlier turns. announce(how) runs once the agent is ready, before any prompt.
        """
        s = self.state
        label = ROLE_LABELS[role]
        file = os.path.basename(path)
        # First, because the role may have written it while no orchestrator was watching.
        if (out := self._handoff(label, path)) is not None:
            log(f"[{role}] {file} is already written")
            return out

        how = self._agent(role)
        agent = s.agents[role]
        if announce:
            announce(how)
        log(f"[{role}] working in pane {agent['pane']}")
        if how in (NEW, RESTARTED):
            message = fresh_text or text
        elif s.prompted != file:
            message = text
        elif how == RESUMED:
            message = CONTINUE_PROMPT.format(path=path)
        else:
            message = None  # alive and already prompted for this file: only wait for it
        if message is not None:
            self.herdr.prompt(agent["name"], message)
            # Recorded only once the prompt is delivered. Dying in between costs one duplicate
            # prompt, which the role answers by writing the same file again.
            s.prompted = file
            self._save()

        deadline = None if timeout is None else self.clock() + timeout
        quiet_since = None
        told = None  # what the human was last told about this turn
        while (out := self._handoff(label, path)) is None:
            record = self.herdr.agent(agent["name"])
            if record is None:
                raise OrchestratorError(f"the {label} exited without writing {path}")
            self._note_session(role, record)
            status = record["agent_status"]
            now = self.clock()
            if deadline is not None and now > deadline:
                raise OrchestratorError(
                    f"the {label} did not write {path} within {timeout}s; see pane {agent['pane']}")

            if status in ("idle", "done"):
                if quiet_since is None:
                    quiet_since = now
            else:
                quiet_since = None
            stalled = watch_stalls and quiet_since is not None and now - quiet_since >= STALL_SECONDS
            if status == "blocked" and told != "blocked":
                self._ask_human(role, "needs your answer")
                told = "blocked"
            elif stalled and told != "stalled":
                self._ask_human(role, f"is idle without writing {file}")
                told = "stalled"
            elif status == "working":
                told = None
            self._heartbeat()
            self.sleep(POLL_SECONDS)

        log(f"[{role}] wrote {file}")
        return out

    def _handoff(self, label: str, path: str) -> str | None:
        """The handoff file, or None while it is not written; an empty one ends the run."""
        out = self.host.read(path)
        if out is not None and not out.strip():
            self._reject(path, f"was written empty by the {label}")
        return out

    def _reject(self, path: str, why: str) -> None:
        # Resume does not judge a role's output; the human fixes the file or deletes it,
        # and a deleted file is asked for again, so the prompt no longer counts as delivered.
        self.state.prompted = None
        raise OrchestratorError(f"{path} {why}; fix it, or delete it to have it written again, then resume")

    def _agent(self, role: str) -> str:
        """Make sure the role's agent is running, and say how: NEW, ALIVE, RESUMED or RESTARTED."""
        known = self.state.agents.get(role)
        if known and self.herdr.status(known["name"]) is not None:
            return ALIVE
        pane = self._pane_for(role)
        if session := (known or {}).get("session"):
            # Keeps the conversation: for the Spec Collector that is the interview itself.
            try:
                if self._start(role, pane, ["--resume", session]):
                    return RESUMED
            except HerdrError as e:
                log(f"[{role}] could not resume session {session}: {e}")
            log(f"[{role}] session {session} is gone; starting a fresh one")
        self._start(role, pane)
        return RESTARTED if known else NEW

    def _pane_for(self, role: str) -> str:
        """The pane for the role's next agent: its old one if that survives, else a new split."""
        s = self.state
        old = (s.agents.get(role) or {}).get("pane") or (s.root_pane if role == "spec" else "")
        if old and self.herdr.pane_exists(old):
            return old
        # The layout: the Builder to the right of the root pane, the Reviewer below the Builder.
        if role == "review":
            parent, direction = (s.agents.get("build") or {}).get("pane"), "down"
        else:
            parent, direction = s.root_pane, "right"
        survivors = [parent, s.root_pane, *(a["pane"] for a in s.agents.values())]
        for pane in dict.fromkeys(p for p in survivors if p and p != old):
            if self.herdr.pane_exists(pane):
                return self.herdr.split(pane, direction, s.cwd)
        raise OrchestratorError(f"no pane of run {s.run_id} is left in herdr workspace {s.workspace_id}")

    def _start(self, role: str, pane: str, extra_args: list[str] | None = None) -> bool:
        """Start the role's agent in the pane. False means it exited at once."""
        s = self.state
        name = f"{role}-{s.key}"
        args = [*s.agent_args, *(extra_args or [])]
        if model := s.models.get(role):
            args += ["--model", model]
        self.herdr.rename_pane(pane, ROLE_LABELS[role])
        ready = self.herdr.start_agent(name, pane, args)
        s.agents[role] = {"name": name, "pane": pane}
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
        self.notify(f"{ROLE_LABELS[role]} {what}", f"pane {pane}")
        log(f"[{role}] {what} (pane {pane})")

    def _change_description(self) -> str:
        if self.state.base:
            return (f"`git diff {self.state.base}` in {self.state.cwd}, "
                    f"plus the untracked files `git status --porcelain` lists")
        return f"the files the Builder's report lists, in {self.state.cwd} (not a git repository)"

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
        description="Spec Collector -> Builder -> Reviewer handoff between Claude Code sessions in herdr.")
    sub = p.add_subparsers(dest="command", required=True)

    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--machine", help="saved herdr machine to run the agents on, e.g. slave0")
    target.add_argument("--cwd", help="project directory (on the machine, if --machine is given)")

    # Unset flags are None: run fills in the defaults, resume keeps what the run was started with.
    settings = argparse.ArgumentParser(add_help=False)
    settings.add_argument("--max-rounds", type=int,
                          help=f"review rounds before giving up (default {DEFAULT_MAX_ROUNDS})")
    settings.add_argument("--timeout", type=int,
                          help=f"seconds a Builder or Reviewer turn may take (default {DEFAULT_TURN_TIMEOUT})")
    settings.add_argument("--permission-mode",
                          help="Claude Code permission mode for every role, e.g. auto or acceptEdits")
    settings.add_argument("--model", help="Claude model for every role, e.g. sonnet or opus")
    for role in ROLE_LABELS:
        settings.add_argument(f"--{role}-model", metavar="MODEL",
                              help=f"Claude model for the {ROLE_LABELS[role]}; overrides --model")

    run = sub.add_parser("run", parents=[target, settings], help="run the handoff workflow for a task")
    run.add_argument("task", help="what to build, as you would tell the Spec Collector")

    resume = sub.add_parser(
        "resume", parents=[target, settings], help="continue a run whose orchestrator has stopped",
        description="Continue a run whose orchestrator has stopped. A settings flag overrides what the run "
                    "was started with; an unset one keeps it, not the default shown.")
    resume.add_argument("run_ref", metavar="RUN", help="the run id, or the six-character key at its end")
    resume.add_argument("--force", action="store_true", help="take over a run that still looks alive")

    sub.add_parser("list", parents=[target], help="list the runs in the project directory")

    args = p.parse_args(argv)
    if args.machine and not args.cwd:
        p.error("--cwd is required with --machine")
    if getattr(args, "max_rounds", None) is not None and args.max_rounds < 1:
        p.error("--max-rounds must be at least 1")
    return args


def role_models(args: argparse.Namespace) -> dict[str, str]:
    """The model each role starts with; a role without one keeps Claude Code's default."""
    models = {role: getattr(args, f"{role}_model") or args.model for role in ROLE_LABELS}
    return {role: m for role, m in models.items() if m}


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


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, under another user
    return True


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
    for age, s in runs:
        health = run_health(s, age, local_host, pid_alive)
        if s.get("error"):
            outcome = f"error: {s['error']}"
        elif health is None:
            outcome = s.get("verdict") or ""
        else:
            outcome = f"{health[0]}: {health[1]}"
            if health[0] == STALE:
                outcome += f"; resume: {resume_command(s['run_id'], target)}"
        print(f"{s['run_id']}  {s['phase']:<6}  round {s['round']}  {outcome}")
        print(f"    {s['task'][:100]}")


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
    health = run_health(saved, age, local_host, pid_alive)
    if health and health[0] == RUNNING and not args.force:
        raise OrchestratorError(
            f"run {state.run_id} looks alive ({health[1]}); pass --force to take it over")
    if args.max_rounds is not None:
        state.max_rounds = args.max_rounds
    if args.timeout is not None:
        state.turn_timeout = args.timeout
    if args.permission_mode:
        state.agent_args = ["--permission-mode", args.permission_mode]
    state.models = {**state.models, **role_models(args)}
    if state.phase != "done" and state.max_rounds < state.round:
        raise OrchestratorError(f"run {state.run_id} is already in round {state.round}; "
                                f"--max-rounds {state.max_rounds} is too low")
    # The herdr this resume addresses: the saved one may name the same herdr reached another way.
    state.machine = args.machine
    return state


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    state = None
    try:
        herdr = Herdr(args.machine)
        if args.machine:
            host = Host(herdr.ssh_target())
        elif os.environ.get("HERDR_ENV") != "1":
            # Without a machine, herdr commands target whichever session is focused;
            # only a pane herdr manages knows it is talking to its own session.
            raise OrchestratorError("not inside a herdr pane; run from herdr, or pass --machine")
        else:
            host = Host()
        cwd = host.resolve_dir(args.cwd or os.getcwd())

        if args.command == "list":
            print_runs(host.run_states(cwd), socket.gethostname(), pid_alive, target_args(args))
            return 0

        if args.command == "run":
            state = RunState(new_run_id(), args.task, cwd, args.machine)
            state.max_rounds = args.max_rounds or DEFAULT_MAX_ROUNDS
            state.turn_timeout = args.timeout or DEFAULT_TURN_TIMEOUT
            state.agent_args = ["--permission-mode", args.permission_mode] if args.permission_mode else []
            state.models = role_models(args)
        else:
            state = resumable_state(host.run_states(cwd), args, cwd, socket.gethostname(), pid_alive)
        workflow = Workflow(
            herdr, host, state, notify=notify_locally,
            max_rounds=state.max_rounds, turn_timeout=state.turn_timeout,
            agent_args=state.agent_args, models=state.models)
        verdict = workflow.run()
    except OrchestratorError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        hint = f"; resume with: {resume_command(state.run_id, target_args(args))}" if state else ""
        print(f"interrupted; the role agents keep running in herdr{hint}", file=sys.stderr)
        return EXIT_INTERRUPTED

    print(f"{verdict}: {state.review_path(state.round)}")
    return 0 if verdict == APPROVE else EXIT_CHANGES_REQUESTED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
