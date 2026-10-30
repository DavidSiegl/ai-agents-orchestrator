"""A parity world: a project with an origin, fakes of everything the engine talks to, and the engine run in it.

`World` is what a scenario (scenarios.py) is written against. It makes, in a temporary directory, a git project
whose `origin` is a bare repository beside it, puts the fake herdr, gh and ssh of fakes.py first on PATH, and
serves Jenkins and PyPI over HTTP from this process; the scenario scripts each of them, runs the engine as a program
with `run` or `start`, and `record` then gives what the run did, normalized (normalize.py), for test_parity.py to
compare with the scenario's recording.

The engine is `PARITY_ENGINE`, a command line, when set, else this checkout's Rust engine, its orchestrator binary
built with cargo (tests/rust_engine.py). The recordings are the Python engine's, made before the switch to Rust
removed it.
"""

import contextlib
import http.server
import json
import os
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Self

import fakes
import rust_engine
from normalize import Normalizer, facts

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The Jenkins job the quality-gate scenarios run, by its full name, and the credentials the engine sends it.
JOB = "parity/quality"
JENKINS_USER, JENKINS_TOKEN = "parity", "parity-token"
# Who the world's commits are by: the harness's own, and the engine's, through the git configuration it is given.
GIT_NAME, GIT_EMAIL = "Parity", "parity@example.com"
# The date of every commit the harness makes, so that the project's own history has the same names in every run.
GIT_DATE = "2026-01-01T00:00:00+00:00"
# How long one engine invocation may take before the scenario fails, and a wait for a hold or a condition, seconds.
INVOCATION_TIMEOUT = 120
WAIT_TIMEOUT = 60
WAIT_POLL_SECONDS = 0.05
# The project every world starts with, committed to origin's main: a Python package with a test, as the gate and
# the orchestrator's checks expect to find one.
PROJECT_FILES = {
    "README.md": "# Parity project\n\nThe project a parity scenario changes.\n",
    "pyproject.toml": '[project]\nname = "parity-project"\nversion = "0.1.0"\ndependencies = []\n',
    "src/calc.py": 'def add(a, b):\n    """The sum of a and b."""\n    return a + b\n',
    "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
}


def engine_command() -> list[str]:
    """The engine a world runs, as PARITY_ENGINE gives it, else this checkout's (rust_engine.binary)."""
    if command := os.environ.get("PARITY_ENGINE"):
        return shlex.split(command)
    return [rust_engine.binary()]


@dataclass
class Invocation:
    """One run of the engine in a world: its arguments, its process and, once it ended, its exit status."""
    n: int  # its number in the world, from 1, which its fakes log as PARITY_INVOCATION
    args: list[str]  # the engine's arguments, without the engine's own command
    process: subprocess.Popen
    out_path: str  # where its standard output and error go, the error at out_path + ".err"
    output_recorded: bool  # whether the record holds its output (World.start)
    killed: bool = False  # whether the scenario killed it, rather than letting it end
    exit: int | None = None

    def wait(self, timeout: float = INVOCATION_TIMEOUT) -> int:
        """Its exit status, once it has ended; kills it and fails the scenario, with its error output, when it runs
        longer than timeout seconds."""
        try:
            self.exit = self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise AssertionError(
                f"engine {shlex.join(self.args)} ran over {timeout}s:\n{self.stderr[-3000:]}") from None
        return self.exit

    def kill(self) -> None:
        """Kill it with SIGKILL, and every process it started, as a crash or a lost machine would stop it: its
        process group, and each process below it in groups of their own, as the Rust engine starts every command
        (engine/src/host/process.rs), which a kill of the engine's group alone would leave running. Each is stopped
        first, until a listing finds none new, so that none starts another between the listing and the kill."""
        stopped: set[int] = set()
        try:
            while True:
                # The engine's group, then what the listing finds below it that is not stopped yet.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGSTOP)
                below = fakes.descendants(self.process.pid) - {self.process.pid} - stopped
                if not below:
                    break
                for pid in below:
                    # Each a process of the engine's, or one that ended meanwhile.
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGSTOP)
                stopped |= below
        finally:
            # Also when a listing fails: nothing is left stopped.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            for pid in stopped:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.process.wait()
        self.killed = True

    @property
    def stdout(self) -> str:
        with open(self.out_path) as f:
            return f.read()

    @property
    def stderr(self) -> str:
        with open(self.out_path + ".err") as f:
            return f.read()


@dataclass
class _Servers:
    """The world's HTTP servers, each on a free port of 127.0.0.1, serving from a thread of this process."""
    jenkins: http.server.ThreadingHTTPServer
    pypi: http.server.ThreadingHTTPServer
    threads: list[threading.Thread] = field(default_factory=list)


class World:
    """A temporary world a scenario runs the engine in, and what it did there.

    The directory holds `project/` (the git checkout the engine runs in, on main, with origin at `origin.git/`),
    `home/` and `config/` (the engine's HOME and XDG_CONFIG_HOME, so that its configuration directory is
    `config/ai-agents-orchestrator/`), `bin/` (the fakes), `fakes/` (their ledger) and `out/` (each invocation's
    output). Use it as a context manager: leaving it stops the servers and kills any invocation still running.
    """

    def __init__(self, root: str, engines: list[list[str]] | None = None):
        """root: an empty directory the world may fill, removed by its owner afterwards. engines: the engine each
        invocation runs, as a command line, the n-th for invocation n and the last for every one after it, so that a
        run one engine started and was killed in is resumed by another; [engine_command()] when not given. Raises
        RuntimeError, which fails the scenario, where gitleaks or openssl is not on PATH or openssl cannot make the
        fake PyPI's certificate."""
        self.root = os.path.realpath(root)
        self.engines = engines or [engine_command()]
        self.project = os.path.join(self.root, "project")
        self.origin = os.path.join(self.root, "origin.git")
        self.config = os.path.join(self.root, "config", "ai-agents-orchestrator")
        # gitleaks is the real one, which the engine runs; openssl makes the fake PyPI's certificate. Why a missing one
        # fails the scenario rather than skipping it: TODO.md, "The parity harness: what it does not record".
        for tool in ("gitleaks", "openssl"):
            if shutil.which(tool) is None:
                raise RuntimeError(f"{tool} is not on PATH; the parity scenarios need it")
        self.normalizer = Normalizer()
        self.normalizer.literal(self.root, "<world>")
        # This checkout, where the example workflows a scenario runs are.
        self.normalizer.literal(ROOT, "<checkout>")
        self.invocations: list[Invocation] = []
        self.notes: dict[str, object] = {}
        self._script = {"project": self.project, "origin": self.origin, "agents": {}, "machines": [], "gh": {},
                        "jenkins": {"builds": []}, "pypi": {}}
        for d in ("home", "config/ai-agents-orchestrator", "bin", "fakes", "out", "tmp"):
            os.makedirs(os.path.join(self.root, d))
        self.ledger = fakes.Ledger(self.root)
        self._write_script()
        self._make_bin()
        self._make_repositories()
        self._servers = self._serve()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        for inv in self.invocations:
            if inv.process.poll() is None:
                inv.kill()
        for server in (self._servers.jenkins, self._servers.pypi):
            server.shutdown()
            server.server_close()

    # -- Scripting the world ------------------------------------------------

    def agent(self, role: str, *actions: dict) -> None:
        """Script the role's agent: each action answers one prompt, in order, after those scripted before (see
        fakes.perform for what an action holds). A prompt beyond the last makes the agent exit."""
        self._script["agents"].setdefault(role, []).extend(actions)
        self._write_script()

    def jenkins_build(self, result: str = "SUCCESS", quality: str | None = "GATE: OK\n", *,
                      tests: list[tuple[str, str]] | None = None, console: str = "", hold: str | None = None,
                      description: str | None = None) -> None:
        """Script the next build the quality job is asked for: how it ends (result), the quality.md it archives,
        None for one that ended before its Gate stage, its failing tests as (name, detail), None for no test report,
        and its console. With hold, it runs until release(hold), archiving nothing meanwhile."""
        self._script["jenkins"]["builds"].append(
            {"result": result, "quality": quality, "tests": tests, "console": console, "hold": hold,
             "description": description})
        self._write_script()

    def retained_build(self, number: int, result: str, description: str) -> None:
        """Give the quality job a build that Jenkins keeps from before the scenario, as judged-in-quality reads it."""
        self._script["jenkins"].setdefault("retained", []).append(
            {"number": number, "result": result, "description": description})
        self._write_script()

    def pypi_project(self, name: str, answer: dict) -> None:
        """Make PyPI answer GET /pypi/<name>/json with answer, its JSON API's document; any other name is a 404."""
        self._script["pypi"][name] = answer
        self._write_script()

    def machine(self, label: str) -> None:
        """Save a herdr machine of that label, reached over the fake ssh, which runs every command here."""
        self._script["machines"].append({"label": label, "id": f"id-{label}", "enabled": True,
                                         "target": f"parity@{label}"})
        self._write_script()

    def hold_pr_create(self, hold: str) -> None:
        """Make gh's next `pr create` wait until release(hold)."""
        self._script["gh"]["create_hold"] = hold
        self._write_script()

    def set_pr(self, n: int, **fields) -> None:
        """Change what gh says of pull request n, the n-th the engine opened: state (OPEN, CLOSED or MERGED),
        closedAt, mergeable, isDraft."""
        with self.ledger.change() as state:
            state["prs"][str(n)].update(fields)

    def pr(self, n: int) -> dict:
        """Pull request n as gh holds it: base, head, title, body, isDraft, state, closedAt, mergeable."""
        with self.ledger.change() as state:
            return dict(state["prs"][str(n)])

    def release(self, hold: str) -> None:
        """Let what waits on hold go on: an agent's action is done at herdr's next command, a build ends by its
        next poll and gh's pr create goes on."""
        with self.ledger.change() as state:
            state.setdefault("released", []).append(hold)

    def wait_reached(self, hold: str) -> None:
        """Wait until the engine waits on hold, at a point where it does nothing but poll until hold is released,
        so that what it did before is the same in every run: it polled the agent after prompting it, polled the
        build for its quality file, or started gh's pr create."""
        def reached():
            with self.ledger.change() as state:
                return hold in state.get("reached", [])
        self.wait_for(reached, f"hold {hold} to be reached")

    @staticmethod
    def wait_for(condition, what: str, timeout: float = WAIT_TIMEOUT) -> None:
        """Poll condition() until it is true; fails the scenario, naming what, after timeout seconds."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError(f"waited {timeout}s for {what}")
            time.sleep(WAIT_POLL_SECONDS)

    def write(self, path: str, text: str) -> str:
        """Write text to path, relative to the world's directory, making its directories; returns its full path."""
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        return full

    def push_to_origin(self, files: dict[str, str], message: str, branch: str = "main") -> None:
        """Commit files, path to text, on origin's branch, as another developer's push would, without touching the
        project's checkout."""
        clone = os.path.join(self.root, "tmp", "pusher")
        if not os.path.exists(clone):
            self.git(self.root, "clone", "--quiet", self.origin, clone)
        self.git(clone, "fetch", "--quiet", "origin")
        self.git(clone, "checkout", "--quiet", "-B", branch, f"origin/{branch}")
        for path, text in files.items():
            full = os.path.join(clone, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        self.git(clone, "add", "--all")
        self.git(clone, "commit", "--quiet", "-m", message)
        self.git(clone, "push", "--quiet", "origin", branch)

    def git(self, cwd: str, *args: str) -> str:
        """git args in cwd, as the harness, with the world's identity and fixed dates; its output. Fails the
        scenario when git fails."""
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,  # noqa: S603, S607
                              env=self._env(None), check=False)
        if proc.returncode != 0:
            raise AssertionError(f"git {shlex.join(args)} in {cwd} failed: {proc.stderr}")
        return proc.stdout

    def note(self, key: str, observed) -> None:
        """Add observed, anything JSON-ready, to the record under notes/key, normalized with the rest: what a
        scenario observed itself, such as serve's answers."""
        self.notes[key] = observed

    # -- Running the engine ---------------------------------------------------

    def start(self, *args: str, cwd: str | None = None, output_recorded: bool = True) -> Invocation:
        """Start the engine with args in the project (or cwd), in a process group of its own, and return at once.
        Without output_recorded, the record keeps only its exit status: for output that differs from host to host,
        such as doctor's versions of git and Tk, which the scenario notes what it needs of."""
        n = len(self.invocations) + 1
        out = os.path.join(self.root, "out", str(n))
        with open(out, "w") as stdout, open(out + ".err", "w") as stderr:
            process = subprocess.Popen(  # noqa: S603 - the engine under test, with the scenario's arguments
                [*self.engines[min(n, len(self.engines)) - 1], *args], cwd=cwd or self.project, env=self._env(n), stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, start_new_session=True)
        self.normalizer.pid(process.pid, n)
        with self.ledger.change() as state:
            state.setdefault("engines", {})[str(n)] = process.pid
        inv = Invocation(n, list(args), process, out, output_recorded)
        self.invocations.append(inv)
        return inv

    def run(self, *args: str, cwd: str | None = None, output_recorded: bool = True) -> Invocation:
        """Run the engine with args, as start does, and wait for it to end."""
        inv = self.start(*args, cwd=cwd, output_recorded=output_recorded)
        inv.wait()
        return inv

    def run_key(self, n: int = 1) -> str:
        """The key of the n-th run the engine started in the project, by the order of their first herdr workspace;
        fails the scenario when there is none."""
        commands = [e["herdr"][2:] if e["herdr"][:1] == ["--machine"] else e["herdr"]
                    for e in self.ledger.events() if "herdr" in e]
        keys = [c[c.index("--label") + 1].split()[0] for c in commands if c[:2] == ["workspace", "create"]]
        if len(keys) < n:
            raise AssertionError(f"no run {n} in the project; workspaces were made for {keys}")
        return keys[n - 1]

    def run_state(self, key: str) -> dict:
        """The saved state of the run with that key, as state.json holds it now."""
        runs = os.path.join(self.project, ".orchestrator", "runs")
        (name,) = [d for d in os.listdir(runs) if d.endswith(f"-{key}")]
        with open(os.path.join(runs, name, "state.json")) as f:
            return json.load(f)

    def _env(self, n: int | None) -> dict[str, str]:
        """The environment of engine invocation n, or of the harness's own git for None: built afresh, so that
        nothing of the developer's own configuration reaches the engine."""
        env = {
            "PATH": f"{os.path.join(self.root, 'bin')}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": os.path.join(self.root, "home"),
            "XDG_CONFIG_HOME": os.path.join(self.root, "config"),
            "TMPDIR": os.path.join(self.root, "tmp"),
            "LC_ALL": "C.UTF-8", "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_GLOBAL": os.path.join(self.root, "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
        }
        if n is None:
            return {**env, "GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE}
        env.update({"HERDR_ENV": "1", "PARITY_INVOCATION": str(n),
                    "JENKINS_URL": self._url(self._servers.jenkins), "JENKINS_USER": JENKINS_USER,
                    "JENKINS_TOKEN": JENKINS_TOKEN, "no_proxy": "127.0.0.1,localhost"})
        env["https_proxy"] = self._url(self._servers.pypi)
        env["SSL_CERT_FILE"] = os.path.join(self.root, "pypi-ca.pem")
        return env

    # -- Making the world -------------------------------------------------------

    def _write_script(self) -> None:
        path = os.path.join(self.root, "script.json")
        with open(path + ".tmp", "w") as f:
            json.dump(self._script, f)
        os.replace(path + ".tmp", path)

    def _make_bin(self) -> None:
        """Shims of the fakes in bin/, and the harnesses' CLIs."""
        programs = ["herdr", "gh", "ssh"]
        shims = {program: (f"exec {shlex.quote(sys.executable)} -I -S "
                           f"{shlex.quote(os.path.join(os.path.dirname(__file__), 'fakes.py'))} "
                           f"{shlex.quote(self.root)} {program} \"$@\"") for program in programs}
        # The harnesses' CLIs, which doctor looks for on PATH and herdr would start: nothing runs them here.
        shims.update({kind: "exit 0" for kind in fakes.AGENT_KINDS})
        for program, body in shims.items():
            path = os.path.join(self.root, "bin", program)
            with open(path, "w") as f:
                f.write(f"#!/bin/sh\n{body}\n")
            os.chmod(path, 0o755)  # noqa: S103 - a program the engine runs, in the world's own directory

    def _make_repositories(self) -> None:
        with open(os.path.join(self.root, "gitconfig"), "w") as f:
            f.write(f"[user]\n\tname = {GIT_NAME}\n\temail = {GIT_EMAIL}\n[commit]\n\tgpgsign = false\n"
                    "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n")
        self.git(self.root, "init", "--quiet", "--bare", "-b", "main", self.origin)
        self.git(self.root, "init", "--quiet", "-b", "main", self.project)
        for path, text in PROJECT_FILES.items():
            full = os.path.join(self.project, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        self.git(self.project, "add", "--all")
        self.git(self.project, "commit", "--quiet", "-m", "Start the parity project")
        self.git(self.project, "remote", "add", "origin", self.origin)
        self.git(self.project, "push", "--quiet", "--set-upstream", "origin", "main")

    def _serve(self) -> _Servers:
        self._make_certificate()
        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls.load_cert_chain(os.path.join(self.root, "pypi-cert.pem"), os.path.join(self.root, "pypi-key.pem"))
        jenkins = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _JenkinsHandler)
        jenkins.world = self
        try:
            pypi = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        except OSError:
            # __exit__ never runs for a world __init__ failed to make.
            jenkins.server_close()
            raise
        pypi.world = self
        pypi.tls = tls
        servers = _Servers(jenkins, pypi)
        for server in (jenkins, pypi):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.threads.append(thread)
        self.normalizer.literal(self._url(jenkins), "<jenkins>")
        return servers

    def _make_certificate(self) -> None:
        """Make pypi.org's certificate for the PyPI server, signed by a certificate authority of the world's own, which
        the engine trusts by SSL_CERT_FILE: a leaf under a CA, as PyPI's is, since rustls refuses a server certificate
        that is itself a CA's, as a self-signed one is (CaUsedAsEndEntity). Raises RuntimeError, naming openssl,
        where it fails."""
        def path(name: str) -> str:
            return os.path.join(self.root, name)
        key = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "30"]
        commands = [
            ["openssl", "req", "-x509", *key, "-keyout", path("pypi-ca-key.pem"), "-out", path("pypi-ca.pem"),
             "-subj", "/CN=Parity CA", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign"],
            ["openssl", "req", "-x509", *key, "-CA", path("pypi-ca.pem"), "-CAkey", path("pypi-ca-key.pem"),
             "-keyout", path("pypi-key.pem"), "-out", path("pypi-cert.pem"), "-subj", "/CN=pypi.org",
             "-addext", "subjectAltName=DNS:pypi.org", "-addext", "basicConstraints=critical,CA:FALSE"],
        ]
        for command in commands:
            # openssl as found on PATH by World.__init__'s check.
            proc = subprocess.run(command, capture_output=True, check=False)  # noqa: S603
            if proc.returncode != 0:
                raise RuntimeError(f"openssl could not make the fake PyPI's certificate, exit status "
                                   f"{proc.returncode}: {proc.stderr.decode(errors='replace').strip()}")

    @staticmethod
    def _url(server: http.server.HTTPServer) -> str:
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def log_request(self, event: dict) -> None:
        """Log a request one of the world's servers received, by the latest invocation, the one whose requests
        these are in every scenario."""
        n = len(self.invocations)
        pids = fakes.descendants(self.invocations[-1].process.pid) if self.invocations else set()
        with self.ledger.change() as state:
            self.ledger.log(state, event, str(n), pids)

    # -- The record -------------------------------------------------------------

    def record(self) -> dict:
        """What the scenario did, normalized: each invocation's arguments, exit status and output; per invocation,
        the commands herdr and gh got and the requests Jenkins and PyPI got, in order, with state.json wherever it
        changed between them; the run directories' files; both repositories' refs, commits and working trees; and
        the scenario's notes. Output read by programs is kept whole, a human's only as its facts (normalize.facts).
        """
        raw = {
            "invocations": [self._invocation_record(inv) for inv in self.invocations],
            "events": self._events(),
            "run_files": self._run_files(),
            "repositories": {"project": self._repository(self.project), "origin": self._repository(self.origin)},
            "notes": self.notes,
        }
        self.normalizer.learn(raw)
        return self._normalized(raw)

    def _normalized(self, raw: dict) -> dict:
        n = self.normalizer
        out = n.json(raw)
        for inv in out["invocations"]:
            for stream in ("stdout", "stderr"):
                if f"{stream}_facts" in inv:
                    inv[f"{stream}_facts"] = facts(n.text(inv.pop(f"{stream}_facts"), timed=True))
        # Texts that word the clock's times, as the turns tables do.
        for by in raw["events"]:
            for raw_event, event in zip(raw["events"][by], out["events"][n.text(by)], strict=True):
                if "stdin" in raw_event:
                    event["stdin"] = n.text(raw_event["stdin"], timed=True)
        for path, text in raw["run_files"].items():
            if path.endswith("summary.md"):
                out["run_files"][n.text(path)] = n.text(text, timed=True)
        # What a scenario notes, such as a pull request's body, may word the clock's times too.
        out["notes"] = n.json(raw["notes"], timed=True)
        return out

    def _invocation_record(self, inv: Invocation) -> dict:
        record = {"args": inv.args, "exit": "killed" if inv.killed else inv.exit}
        if not inv.output_recorded:
            return record
        if _machine_read(inv.args):
            record["stdout"] = inv.stdout
        else:
            record["stdout_facts"] = inv.stdout
        record["stderr_facts"] = inv.stderr
        return record

    def _events(self) -> dict[str, list]:
        """The ledger's events by invocation, each state.json parsed, and polls that repeated collapsed into one."""
        by: dict[str, list] = {}
        for e in self.ledger.events():
            event = dict(e)
            who = event.pop("by")
            if "state" in event:
                event["state"] = {"path": event["state"]["path"], "saved": json.loads(event["state"]["text"])}
                # The fakes log a state only while its owner, if any, is a process of the invocation logging it.
                if (owner := event["state"]["saved"].get("owner")) and who.isdigit():
                    self.normalizer.pid(owner["pid"], int(who))
            by.setdefault(who, []).append(event)
        return {who: _collapsed(events) for who, events in by.items()}

    def _run_files(self) -> dict[str, str]:
        """Every file under the project's .orchestrator, but the worktrees, by its path there, in path order."""
        top = os.path.join(self.project, ".orchestrator")
        files = {}
        for dirpath, dirnames, filenames in os.walk(top):
            if dirpath == top:
                dirnames[:] = [d for d in dirnames if d != "worktrees"]
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                with open(path, errors="replace") as f:
                    files[os.path.relpath(path, top)] = f.read()
        return files

    def _repository(self, repo: str) -> dict:
        """The repository's refs, every commit they reach, from the refs in name order and each commit's parents in
        order, and for a repository with a working tree, its worktrees' status and changed files."""
        lines = self.git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
        refs = dict(line.split(" ", 1) for line in lines)
        head = self.git(repo, "symbolic-ref", "--quiet", "HEAD").strip() if self._is_symbolic(repo) else "detached"
        commits, todo = {}, [sha for _, sha in sorted(refs.items())]
        while todo:
            sha = todo.pop(0)
            if sha in commits or self.git(repo, "cat-file", "-t", sha).strip() != "commit":
                continue
            commits[sha] = self._commit(repo, sha)
            todo = commits[sha]["parents"] + todo
        out = {"head": head, "refs": refs, "commits": list(commits.values())}
        if not repo.endswith(".git"):
            out["worktrees"] = self._worktrees(repo)
        return out

    def _is_symbolic(self, repo: str) -> bool:
        return subprocess.run(["git", "-C", repo, "symbolic-ref", "--quiet", "HEAD"],  # noqa: S603, S607
                              capture_output=True, check=False).returncode == 0

    def _commit(self, repo: str, sha: str) -> dict:
        parents = self.git(repo, "show", "--no-patch", "--format=%P", sha).split()
        message = self.git(repo, "show", "--no-patch", "--format=%an <%ae>%n%B", sha)
        author, _, body = message.partition("\n")
        changes = self.git(repo, "diff-tree", "--root", "--no-commit-id", "-r", "--name-status", "-m",
                           "--first-parent", sha).splitlines()
        files = {}
        for line in changes:
            change, path = line.split("\t", 1)
            if change != "D":
                files[path] = self.git(repo, "show", f"{sha}:{path}")
        return {"sha": sha, "parents": parents, "author": author, "message": body, "changes": changes,
                "files": files}

    def _worktrees(self, repo: str) -> list[dict]:
        """Each worktree of repo, its path, HEAD, status and the text of each file the status names."""
        out, tree = [], {}
        for line in self.git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
            if line:
                name, _, text = line.partition(" ")
                tree[name] = text
                continue
            if tree:
                path = tree["worktree"]
                porcelain = self.git(path, "status", "--porcelain", "--untracked-files=all").splitlines()
                changed = {}
                for entry in porcelain:
                    name = entry[3:]
                    full = os.path.join(path, name)
                    if os.path.isfile(full) and not name.startswith(".orchestrator/"):
                        with open(full, errors="replace") as f:
                            changed[name] = f.read()
                out.append({"path": path, "branch": tree.get("branch", "detached"), "status": porcelain,
                            "changed": changed})
                tree = {}
        return out


def _machine_read(args: list[str]) -> bool:
    """Whether the engine's standard output for args is read by a program, so that it must come out byte for byte
    (spec, Parity): a run's or resume's last line, list --json, metrics, serve's first line, and a workflow printed
    as a file."""
    command = next((a for a in args if not a.startswith("-")), "")
    return (command in ("run", "resume", "metrics", "serve") or (command == "list" and "--json" in args)
            or (command == "workflows" and len([a for a in args if not a.startswith("-")]) > 1))


def _collapsed(events: list) -> list:
    """events with each run of events that repeats the run just before it dropped, for runs of up to four: a poll
    the engine repeats while it waits comes out once, however long the wait took."""
    out = list(events)
    changed = True
    while changed:
        changed = False
        for k in range(1, 5):
            i = 0
            while i + 2 * k <= len(out):
                if out[i:i + k] == out[i + k:i + 2 * k]:
                    del out[i + k:i + 2 * k]
                    changed = True
                else:
                    i += 1
    return out


class _JenkinsHandler(http.server.BaseHTTPRequestHandler):
    """Jenkins's REST API for the job JOB, as the engine's CI class uses it, from the world's scripted builds.

    The retained builds keep their numbers; the triggered ones follow the highest of them, in the order they were
    triggered, from 1 where there are none. Queue item k is build k's. Each request is logged, with its form,
    before it is answered. Its JSON answers have the fields and types of the live Jenkins's, as FakeCI's do
    (tests/contract); the texts, such as a console, and how many cases a test report holds are each scenario's own."""

    server: http.server.ThreadingHTTPServer

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._answer("GET", None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._answer("POST", urllib.parse.parse_qs(self.rfile.read(length).decode()))

    def _answer(self, method: str, form: dict | None) -> None:
        world: World = self.server.world
        world.log_request({"jenkins": f"{method} {self.path}", **({"form": form} if form is not None else {})})
        if not self.headers.get("Authorization", "").startswith("Basic "):
            return self._send(401, b"no credentials")
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        prefix = "".join(f"/job/{part}" for part in JOB.split("/")) + "/"
        if url.path.startswith("/queue/item/"):
            return self._json({"cancelled": False, "executable": {"number": int(url.path.split("/")[3])}})
        if not url.path.startswith(prefix):
            return self._send(404, b"no such job")
        rest = url.path[len(prefix):]
        with world.ledger.change() as state:
            builds = self._builds(world, state)
            if rest == "buildWithParameters" and method == "POST":
                state.setdefault("triggered", []).append(form["GIT_REF"][0])
                n = max(self._builds(world, state))
                host, port = self.server.server_address[:2]
                return self._send(201, b"", {"Location": f"http://{host}:{port}/queue/item/{n}/"})
            if rest == "api/json":
                return self._job(query.get("tree", [""])[0], builds)
            number, _, what = rest.partition("/")
            build = builds.get(int(number)) if number.isdigit() else None
            if build is None:
                return self._send(404, b"no such build")
            held = build.get("hold") and build["hold"] not in state.get("released", [])
            # The last request of the engine's poll of a running build: it sleeps and polls again after it.
            if held and what == "artifact/quality.md" and build["hold"] not in state.setdefault("reached", []):
                state["reached"].append(build["hold"])
        if what == "api/json":
            return self._json({"building": bool(held), "result": None if held else build["result"]})
        if what == "artifact/quality.md" and build.get("quality") is not None and not held:
            return self._send(200, build["quality"].encode())
        if what == "testReport/api/json" and build.get("tests") is not None:
            return self._json({"suites": [{"cases": [
                {"className": name.rsplit(".", 1)[0], "name": name.rsplit(".", 1)[-1], "status": "FAILED",
                 "errorDetails": detail} for name, detail in build["tests"]]}]})
        if what == "consoleText":
            return self._send(200, build.get("console", "").encode())
        return self._send(404, b"not found")

    @staticmethod
    def _builds(world: World, state: dict) -> dict[int, dict]:
        """The job's builds by number: the retained ones, then those triggered so far, each its scripted outcome
        with its GIT_REF."""
        script = world.ledger.script()["jenkins"]
        builds = {b["number"]: {**b, "ref": None} for b in script.get("retained", [])}
        first = max(builds, default=0) + 1
        for k, ref in enumerate(state.get("triggered", [])):
            outcome = script["builds"][k] if k < len(script["builds"]) else {"result": "FAILURE", "quality": None}
            builds[first + k] = {**outcome, "number": first + k, "ref": ref}
        return builds

    def _job(self, tree: str, builds: dict[int, dict]) -> None:
        newest = sorted(builds.values(), key=lambda b: -b["number"])
        if "parameterDefinitions" in tree:
            return self._json({"property": [{"parameterDefinitions": [{"name": "GIT_REF"}]}]})
        if "actions" in tree:
            return self._json({"builds": [{"number": b["number"], "actions": [
                {"parameters": [{"name": "GIT_REF", "value": b["ref"]}]}]} for b in newest]})
        return self._json({"builds": [{"number": b["number"], "result": b["result"],
                                       "description": b.get("description")} for b in newest]})

    def _json(self, body: dict) -> None:
        self._send(200, json.dumps(body).encode(), {"Content-Type": "application/json"})

    def _send(self, code: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """The HTTPS proxy the engine's https_proxy names: it takes a CONNECT to pypi.org:443 and answers the request
    inside, over TLS with the world's certificate, as PyPI's JSON API would (_PyPIHandler)."""

    server: http.server.ThreadingHTTPServer

    def log_message(self, format, *args):
        pass

    def do_CONNECT(self):
        if self.path != "pypi.org:443":
            self.send_error(403, "the parity proxy reaches only pypi.org")
            return
        self.send_response(200)
        self.end_headers()
        try:
            tls = self.server.tls.wrap_socket(self.connection, server_side=True)
        except ssl.SSLError:
            self.close_connection = True
            return
        _PyPIHandler(tls, self.client_address, self.server)
        self.close_connection = True


class _PyPIHandler(http.server.BaseHTTPRequestHandler):
    """PyPI's GET /pypi/<name>/json, from the world's pypi_project answers; a 404 for any other project."""

    server: http.server.ThreadingHTTPServer

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        world: World = self.server.world
        world.log_request({"pypi": f"GET {self.path}"})
        name = urllib.parse.unquote(self.path.split("/")[2]) if self.path.startswith("/pypi/") else ""
        answer = world.ledger.script()["pypi"].get(name)
        body = json.dumps(answer).encode() if answer is not None else b"not found"
        self.send_response(200 if answer is not None else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
