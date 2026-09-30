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
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field

RUNS_DIR = ".orchestrator/runs"
DEFAULT_TURN_TIMEOUT = 1800
DEFAULT_MAX_ROUNDS = 3
AGENT_START_TIMEOUT_MS = 60_000
POLL_SECONDS = 3
# How long a Builder or Reviewer may sit idle without its handoff file before the human is told.
STALL_SECONDS = 180

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

    def status(self, name: str) -> str | None:
        """The agent's lifecycle status, or None once it has exited."""
        try:
            return self.call("agent", "get", name)["agent"]["agent_status"]
        except HerdrError as e:
            if e.code == "agent_not_found":
                return None
            raise

    def focus(self, name: str) -> None:
        self.call("agent", "focus", name)

    def notify(self, title: str, body: str) -> None:
        self.call("notification", "show", title, "--body", body, "--sound", "request")


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
        self.check(["sh", "-c", 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"', "_", path], stdin=text)

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

    def run_states(self, cwd: str) -> list[dict]:
        script = f'for f in "$1"/{RUNS_DIR}/*/state.json; do [ -f "$f" ] && cat -- "$f" && echo; done; true'
        out = self.check(["sh", "-c", script, "_", cwd])
        states = []
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                states.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise OrchestratorError(f"corrupt run state under {cwd}/{RUNS_DIR}: {e}") from e
        return states


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
    base: str | None = None
    verdict: str | None = None
    error: str | None = None
    agents: dict[str, dict[str, str]] = field(default_factory=dict)

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


class Workflow:
    """Drives one run through Spec Collector -> Builder -> Reviewer in a herdr workspace.

    A role's turn ends when it writes its handoff file, not when herdr reports it
    settled: Claude Code ends a turn while a background task it started is still
    running and resumes when the task completes, so idle or done can come mid-work.
    """

    def __init__(self, herdr: Herdr, host: Host, state: RunState, *,
                 notify, max_rounds: int = DEFAULT_MAX_ROUNDS,
                 turn_timeout: int = DEFAULT_TURN_TIMEOUT,
                 agent_args: list[str] | None = None,
                 models: dict[str, str] | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        self.herdr = herdr
        self.host = host
        self.state = state
        self.notify = notify
        self.max_rounds = max_rounds
        self.turn_timeout = turn_timeout
        self.agent_args = agent_args or []
        self.models = models or {}
        self.sleep = sleep
        self.clock = clock

    def run(self) -> str:
        """Run every phase and return the final verdict."""
        try:
            root = self._prepare()
            self._collect_spec(root)
            return self._build_and_review(root)
        except OrchestratorError as e:
            self.state.error = str(e)
            self._save_after_error()
            raise

    def _prepare(self) -> str:
        ignore = f"{self.state.cwd}/.orchestrator/.gitignore"
        if self.host.read(ignore) is None:
            # Keeps run files out of `git status`, so the Reviewer sees only the Builder's changes.
            self.host.write(ignore, "*\n")
        label = f"{self.state.run_id[-6:]} {self.state.task}"[:40]
        self.state.workspace_id, root = self.herdr.create_workspace(self.state.cwd, label)
        log(f"run {self.state.run_id} in herdr workspace {self.state.workspace_id}")
        self._save()
        return root

    def _collect_spec(self, pane: str) -> None:
        s = self.state
        name = self._start("spec", pane)
        self.herdr.focus(name)
        self.notify("Spec Collector is waiting for you", f"pane {pane}: {s.task}")
        log(f"[spec] answer the Spec Collector in pane {pane}")
        # The interview is paced by the human, so it has no deadline, and an idle
        # collector is normal: it is waiting for the human's reply.
        self._turn("spec", SPEC_PROMPT.format(task=s.task, cwd=s.cwd, spec_path=s.spec_path),
                   s.spec_path, timeout=None, watch_stalls=False)

    def _build_and_review(self, root: str) -> str:
        s = self.state
        s.base = self.host.git_head(s.cwd)
        s.phase, s.round = "build", 1
        self._save()

        build_pane = self.herdr.split(root, "right", s.cwd)
        self._start("build", build_pane)
        self._turn("build", BUILD_PROMPT.format(
            spec_path=s.spec_path, cwd=s.cwd, report_path=s.build_path(1)), s.build_path(1), self.turn_timeout)

        review_pane = self.herdr.split(build_pane, "down", s.cwd)
        self._start("review", review_pane)
        for n in range(1, self.max_rounds + 1):
            s.phase, s.round = "review", n
            self._save()
            template = REVIEW_PROMPT if n == 1 else RECHECK_PROMPT
            review = self._turn("review", template.format(
                spec_path=s.spec_path, change=self._change_description(),
                report_path=s.build_path(n), review_path=s.review_path(n),
                approve=APPROVE, changes=CHANGES_REQUESTED), s.review_path(n), self.turn_timeout)
            s.verdict = parse_verdict(review)
            if s.verdict is None:
                raise OrchestratorError(f"{s.review_path(n)} does not start with a VERDICT line")
            log(f"[review] round {n}: {s.verdict}")
            if s.verdict == APPROVE or n == self.max_rounds:
                break

            s.phase, s.round = "build", n + 1
            self._save()
            self._turn("build", FIX_PROMPT.format(
                review_path=s.review_path(n), report_path=s.build_path(n + 1)), s.build_path(n + 1), self.turn_timeout)

        s.phase = "done"
        self._save()
        self.notify(f"Run finished: {s.verdict}", s.task)
        return s.verdict

    def _start(self, role: str, pane: str) -> str:
        key = self.state.run_id.rsplit("-", 1)[-1]
        name = f"{role}-{key}"
        self.herdr.rename_pane(pane, ROLE_LABELS[role])
        args = self.agent_args
        if model := self.models.get(role):
            args = [*args, "--model", model]
        ready = self.herdr.start_agent(name, pane, args)
        self.state.agents[role] = {"name": name, "pane": pane}
        self._save()
        if not ready:
            self._ask_human(role, "needs your answer")
            # The human decides how long a startup dialog takes, as with the interview.
            self.herdr.wait(name, None, until=("idle", "done"))
        return name

    def _turn(self, role: str, text: str, path: str, timeout: int | None,
              watch_stalls: bool = True) -> str:
        """Prompt a role and return the handoff file that ends its turn."""
        agent = self.state.agents[role]
        label = ROLE_LABELS[role]
        log(f"[{role}] working in pane {agent['pane']}")
        self.herdr.prompt(agent["name"], text)

        deadline = None if timeout is None else self.clock() + timeout
        quiet_since = None
        told = None  # what the human was last told about this turn
        while (out := self.host.read(path)) is None:
            status = self.herdr.status(agent["name"])
            if status is None:
                raise OrchestratorError(f"the {label} exited without writing {path}")
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
                self._ask_human(role, f"is idle without writing {os.path.basename(path)}")
                told = "stalled"
            elif status == "working":
                told = None
            self.sleep(POLL_SECONDS)

        if not out.strip():
            raise OrchestratorError(f"the {label} wrote an empty {path}")
        log(f"[{role}] wrote {os.path.basename(path)}")
        return out

    def _ask_human(self, role: str, what: str) -> None:
        pane = self.state.agents[role]["pane"]
        self.notify(f"{ROLE_LABELS[role]} {what}", f"pane {pane}")
        log(f"[{role}] {what} (pane {pane})")

    def _change_description(self) -> str:
        if self.state.base:
            return (f"`git diff {self.state.base}` in {self.state.cwd}, "
                    f"plus the untracked files `git status --porcelain` lists")
        return f"the files the Builder's report lists, in {self.state.cwd} (not a git repository)"

    def _save(self) -> None:
        self.host.write(f"{self.state.dir}/state.json", json.dumps(asdict(self.state)) + "\n")

    def _save_after_error(self) -> None:
        try:
            self._save()
        except OrchestratorError as e:
            log(f"could not record the failure in state.json: {e}")


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

    run = sub.add_parser("run", parents=[target], help="run the handoff workflow for a task")
    run.add_argument("task", help="what to build, as you would tell the Spec Collector")
    run.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                     help=f"review rounds before giving up (default {DEFAULT_MAX_ROUNDS})")
    run.add_argument("--timeout", type=int, default=DEFAULT_TURN_TIMEOUT,
                     help=f"seconds a Builder or Reviewer turn may take (default {DEFAULT_TURN_TIMEOUT})")
    run.add_argument("--permission-mode",
                     help="Claude Code permission mode for every role, e.g. auto or acceptEdits")
    run.add_argument("--model", help="Claude model for every role, e.g. sonnet or opus")
    for role in ROLE_LABELS:
        run.add_argument(f"--{role}-model", metavar="MODEL",
                         help=f"Claude model for the {ROLE_LABELS[role]}; overrides --model")

    sub.add_parser("list", parents=[target], help="list the runs in the project directory")

    args = p.parse_args(argv)
    if args.machine and not args.cwd:
        p.error("--cwd is required with --machine")
    if args.command == "run" and args.max_rounds < 1:
        p.error("--max-rounds must be at least 1")
    return args


def role_models(args: argparse.Namespace) -> dict[str, str]:
    """The model each role starts with; a role without one keeps Claude Code's default."""
    models = {role: getattr(args, f"{role}_model") or args.model for role in ROLE_LABELS}
    return {role: m for role, m in models.items() if m}


def notify_locally(title: str, body: str) -> None:
    """Notify the herdr this orchestrator runs beside, where the human is watching."""
    try:
        Herdr().notify(title, body)
    except OrchestratorError as e:
        log(f"notification failed: {e}")


def print_runs(states: list[dict]) -> None:
    if not states:
        print("No runs.")
        return
    for s in states:
        outcome = f"error: {s['error']}" if s.get("error") else s.get("verdict") or ""
        print(f"{s['run_id']}  {s['phase']:<6}  round {s['round']}  {outcome}")
        print(f"    {s['task'][:100]}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
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
            print_runs(host.run_states(cwd))
            return 0

        state = RunState(new_run_id(), args.task, cwd, args.machine)
        workflow = Workflow(
            herdr, host, state, notify=notify_locally,
            max_rounds=args.max_rounds, turn_timeout=args.timeout,
            agent_args=["--permission-mode", args.permission_mode] if args.permission_mode else [],
            models=role_models(args))
        verdict = workflow.run()
    except OrchestratorError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted; the role agents keep running in herdr", file=sys.stderr)
        return EXIT_INTERRUPTED

    print(f"{verdict}: {state.review_path(state.round)}")
    return 0 if verdict == APPROVE else EXIT_CHANGES_REQUESTED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
