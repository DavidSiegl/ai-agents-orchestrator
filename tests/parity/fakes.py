"""The programs a parity world puts on the engine's PATH in place of herdr, gh and ssh, and its shared ledger.

Run as `python fakes.py WORLD PROGRAM ARGS...`, where WORLD is the world's directory and PROGRAM is herdr, gh, ssh
or gitleaks; the world's bin directory holds a shim of each name that runs this. Each plays its part of the world
from the scenario's script, `WORLD/script.json`, which `World` in world.py writes:

- herdr keeps workspaces, panes and agents, and plays each role's agent from `script["agents"][role]`, one action
  per prompt (`perform` says what an action holds). `--machine M` is taken off the front and the machine must be
  one of `script["machines"]`.
- gh answers `pr create`, `pr view`, `pr edit`, `pr ready` and `auth status` from the pull requests it has
  opened, which `World.set_pr` changes between invocations; a head commit is read from the origin repository.
- ssh runs its command locally with sh, so that `--machine` reaches this machine's files through the engine's
  remote code.
- gitleaks, put on PATH only where no real gitleaks is installed, answers `git --help` and writes an empty report,
  as the real one does for a scan that finds nothing.

Every herdr and gh command, and each state.json a command finds changed, goes into the ledger, `WORLD/fakes/`:
the fakes' own state (`state.json`) and the event log (`events.jsonl`), each read and written under one lock
(`Ledger`). Imported by world.py for `Ledger`, which the world's in-process servers share; this module imports
nothing of the engine, so that it runs whichever engine the world drives.
"""

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time

# The ids herdr's answers carry, in herdr's JSON shape; the engine reads only these keys.
PR_URL = "https://github.com/parity/project/pull/{}"
# How often a fake waiting on a hold looks for its release, in seconds.
HOLD_POLL_SECONDS = 0.05
# herdr's version as `herdr --version` prints it; doctor needs 0.9 or later.
HERDR_VERSION = "herdr 0.9.4"
# The harnesses an agent can run in, as herdr's --kind takes them: each has a current integration, and a CLI on
# PATH that World puts there and nothing runs.
AGENT_KINDS = ("claude", "codex", "gemini", "opencode", "pi")


class Ledger:
    """A world's shared record, `WORLD/fakes/`: the fakes' state and the event log, each change under one lock.

    The state is one JSON object, which `change` hands its caller to read and modify. Its keys, each created by the
    first writer that needs it: workspaces, panes and agents (herdr's), prs (gh's), consumed (per role, how many
    of its scripted actions have been taken), held (per hold name, the action waiting for it), reached and released
    (hold names), last_states (per state.json path, its text as last logged, heartbeat left out) and next (per kind
    of id, the next number).
    """

    def __init__(self, world: str):
        """world: the world's directory, whose `fakes/` directory must exist."""
        self.dir = os.path.join(world, "fakes")
        self.world = world

    @contextlib.contextmanager
    def change(self):
        """The state, locked against every other process of the world until the block ends, then saved."""
        with open(os.path.join(self.dir, "lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = os.path.join(self.dir, "state.json")
            try:
                with open(path) as f:
                    state = json.load(f)
            except FileNotFoundError:
                state = {}
            yield state
            with open(path + ".tmp", "w") as f:
                json.dump(state, f)
            os.replace(path + ".tmp", path)

    def log(self, state: dict, event: dict, by: str, engine_pids: set[int]) -> None:
        """Append event to the log, by invocation `by`, after a `state` event for each run whose state.json
        changed since it was last logged and is the run of one of engine_pids or of none (see `_changed_states`).
        Call inside `change`, whose state is `state`."""
        lines = [{"by": by, "state": s} for s in _changed_states(state, self.script()["project"], engine_pids)]
        lines.append({"by": by, **event})
        with open(os.path.join(self.dir, "events.jsonl"), "a") as f:
            f.writelines(json.dumps(line) + "\n" for line in lines)

    def events(self) -> list[dict]:
        """Every event logged so far, in order."""
        try:
            with open(os.path.join(self.dir, "events.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def script(self) -> dict:
        """The scenario's script as `World` last wrote it."""
        with open(os.path.join(self.world, "script.json")) as f:
            return json.load(f)


def _changed_states(state: dict, project: str, engine_pids: set[int]) -> list[dict]:
    """The state.json of each run in project whose text changed since it was last logged, as {"path", "text"}, in
    the order of their paths, and records them as logged in state.

    A run counts only while engine_pids holds its owner's pid, or it has no owner: another engine process that
    talks to the world meanwhile, as a taken-over run's old orchestrator does, sees state.json change at times only
    the scheduler decides. heartbeat_at is left out of the comparison: it changes with time alone."""
    runs = os.path.join(project, ".orchestrator", "runs")
    try:
        names = sorted(os.listdir(runs))
    except FileNotFoundError:
        return []
    last = state.setdefault("last_states", {})
    changed = []
    for name in names:
        path = os.path.join(runs, name, "state.json")
        try:
            with open(path) as f:
                text = f.read()
            saved = json.loads(text)
        except (FileNotFoundError, ValueError):
            continue
        owner = saved.get("owner")
        if owner and owner.get("pid") not in engine_pids:
            continue
        saved.pop("heartbeat_at", None)
        key = json.dumps(saved, sort_keys=True)
        if last.get(path) != key:
            last[path] = key
            changed.append({"path": path, "text": text})
    return changed


def ancestors() -> set[int]:
    """The pids of this process's ancestors: the engine that started it is one of them, whether directly, through
    sh or through the fake ssh."""
    pids, pid = set(), os.getppid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                # The command name, field 2, is in parentheses and may hold spaces; the parent is the second field
                # after it.
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],  # noqa: S603, S607 - ps on PATH, as sh finds it
                                 capture_output=True, text=True, check=False)
            try:
                pid = int(out.stdout.strip())
            except ValueError:
                break
    return pids


def invocation() -> str:
    """The engine invocation this process serves, as World numbers them in PARITY_INVOCATION; "?" outside one."""
    return os.environ.get("PARITY_INVOCATION", "?")


# -- herdr ------------------------------------------------------------------


class HerdrFailure(Exception):
    """A herdr error answer: herdr prints {"error": {"code", "message"}} and exits 1."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def herdr(ledger: Ledger, args: list[str]) -> int:
    """Answer one herdr command as herdr would, logging it; returns the exit status."""
    script = ledger.script()
    with ledger.change() as state:
        ledger.log(state, {"herdr": args}, invocation(), ancestors())
        _perform_released(ledger, state)
        command = args
        if args[:1] == ["--machine"]:
            if not any(args[1] in (m["label"], m["id"]) for m in script.get("machines", [])):
                return _herdr_error(HerdrFailure("machine_not_found", f"no machine {args[1]}"))
            command = args[2:]
        if command[:1] == ["--version"]:
            print(HERDR_VERSION)
            return 0
        if command[:2] == ["integration", "status"]:
            print("\n".join(f"{kind}: current (v10) (/parity/{kind})" for kind in AGENT_KINDS))
            return 0
        if command[:3] == ["machine", "list", "--json"]:
            print(json.dumps(script.get("machines", [])))
            return 0
        try:
            result = _herdr_command(ledger, state, command)
        except HerdrFailure as e:
            return _herdr_error(e)
    print(json.dumps({"result": result}))
    return 0


def _herdr_error(e: HerdrFailure) -> int:
    print(json.dumps({"error": {"code": e.code, "message": str(e)}}), file=sys.stderr)
    return 1


def _next_id(state: dict, kind: str) -> int:
    """The next number of kind, from 1."""
    counters = state.setdefault("next", {})
    counters[kind] = counters.get(kind, 0) + 1
    return counters[kind]


def _herdr_command(ledger: Ledger, state: dict, args: list[str]) -> dict:
    """The `result` of one API-backed herdr command, its effects made in state; HerdrFailure for herdr's errors."""
    workspaces = state.setdefault("workspaces", {})
    panes = state.setdefault("panes", {})
    agents = state.setdefault("agents", {})
    what, rest = tuple(args[:2]), args[2:]
    if what == ("workspace", "create"):
        w = f"w{_next_id(state, 'workspace')}"
        pane = f"p{_next_id(state, 'pane')}"
        workspaces[w] = {"label": _flag(rest, "--label"), "cwd": _flag(rest, "--cwd")}
        panes[pane] = {"workspace": w, "label": ""}
        return {"workspace": {"workspace_id": w}, "root_pane": {"pane_id": pane}}
    if what == ("workspace", "get"):
        if rest[0] not in workspaces:
            raise HerdrFailure("workspace_not_found", f"no workspace {rest[0]}")
        return {"workspace": {"workspace_id": rest[0]}}
    if what == ("workspace", "close"):
        if workspaces.pop(rest[0], None) is None:
            raise HerdrFailure("workspace_not_found", f"no workspace {rest[0]}")
        for p in [p for p, v in panes.items() if v["workspace"] == rest[0]]:
            del panes[p]
        for name in [n for n, a in agents.items() if a["pane"] not in panes]:
            del agents[name]
        return {}
    if what == ("pane", "split"):
        parent = _pane(panes, rest[0])
        pane = f"p{_next_id(state, 'pane')}"
        panes[pane] = {"workspace": parent["workspace"], "label": ""}
        return {"pane": {"pane_id": pane}}
    if what == ("pane", "rename"):
        _pane(panes, rest[0])["label"] = rest[1]
        return {}
    if what == ("pane", "get"):
        _pane(panes, rest[0])
        return {"pane": {"pane_id": rest[0]}}
    if what == ("agent", "start"):
        name, pane = rest[0], _flag(rest, "--pane")
        _pane(panes, pane)
        agents[name] = {"pane": pane, "kind": _flag(rest, "--kind"), "status": "idle",
                        "session": f"session-{_next_id(state, 'session')}"}
        return {"agent": _agent_record(name, agents[name])}
    if what == ("agent", "get"):
        agent = _agent(agents, rest[0])
        # The engine's first poll of an agent whose action is held: the engine is waiting for it now, and does
        # nothing but poll again until the hold is released.
        for hold, entry in state.get("held", {}).items():
            if entry["name"] == rest[0] and hold not in state.setdefault("reached", []):
                state["reached"].append(hold)
        return {"agent": _agent_record(rest[0], agent)}
    if what == ("agent", "wait"):
        agent = _agent(agents, rest[0])
        until = [rest[i + 1] for i, a in enumerate(rest) if a == "--until"]
        if until and agent["status"] not in until:
            raise HerdrFailure("timeout", f"agent {rest[0]} did not reach {', '.join(until)}")
        return {"agent": _agent_record(rest[0], agent)}
    if what == ("agent", "prompt"):
        _prompted(ledger, state, rest[0], _agent(agents, rest[0]))
        return {}
    if what == ("agent", "focus"):
        _agent(agents, rest[0])
        return {}
    if what == ("notification", "show"):
        return {}
    raise HerdrFailure("unknown_command", f"the fake herdr has no command {' '.join(args)}")


def _flag(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _pane(panes: dict, pane: str) -> dict:
    if pane not in panes:
        raise HerdrFailure("pane_not_found", f"no pane {pane}")
    return panes[pane]


def _agent(agents: dict, name: str) -> dict:
    if name not in agents:
        raise HerdrFailure("agent_not_found", f"no agent {name}")
    return agents[name]


def _agent_record(name: str, agent: dict) -> dict:
    return {"name": name, "pane_id": agent["pane"], "agent_status": agent["status"],
            "agent_session": {"value": agent["session"]}}


def _prompted(ledger: Ledger, state: dict, name: str, agent: dict) -> None:
    """Play the role's next scripted action for the prompt just sent to agent name: performed now, or held until
    World.release names its hold, the hold being reached at the engine's first `agent get` of the agent after it.
    A prompt the script has no action for makes the agent exit, which the engine reports, rather than leave the
    scenario waiting."""
    role = name.rsplit("-", 1)[0]
    consumed = state.setdefault("consumed", {})
    actions = ledger.script()["agents"].get(role, [])
    k = consumed.get(role, 0)
    if k >= len(actions):
        del state["agents"][name]
        return
    consumed[role] = k + 1
    action = actions[k]
    if (hold := action.get("hold")) and hold not in state.get("released", []):
        agent["status"] = "working"
        state.setdefault("held", {})[hold] = {"name": name, "action": action}
        return
    perform(ledger, state, name, action)


def _perform_released(ledger: Ledger, state: dict) -> None:
    """Perform each held action whose hold has been released since."""
    held = state.get("held", {})
    for hold in [h for h in held if h in state.get("released", [])]:
        entry = held.pop(hold)
        if entry["name"] in state.get("agents", {}):
            perform(ledger, state, entry["name"], entry["action"])


def perform(ledger: Ledger, state: dict, name: str, action: dict) -> None:
    """Do what the agent name's action says, as its agent would in the turn the prompt starts.

    An action is a dict of any of: "edit", {path relative to the run's working directory (its worktree, else the
    project): text, or None to delete the file}; "write", {path relative to the run directory: text}, written after
    the edits, so that the handoff file, which ends the turn, comes last; "exit", true for an agent that exits
    afterwards; "status", the agent's status afterwards, default idle; and "hold", which World.release takes."""
    agent = state["agents"][name]
    run_dir = _run_dir(ledger.script()["project"], name.rsplit("-", 1)[1])
    with open(os.path.join(run_dir, "state.json")) as f:
        saved = json.load(f)
    work_dir = saved.get("worktree") or saved["cwd"]
    for path, text in action.get("edit", {}).items():
        _put(os.path.join(work_dir, path), text)
    for path, text in action.get("write", {}).items():
        _put(os.path.join(run_dir, path), text)
    agent["status"] = action.get("status", "idle")
    if action.get("exit"):
        del state["agents"][name]


def _run_dir(project: str, key: str) -> str:
    """The run directory of the run whose key is key."""
    runs = os.path.join(project, ".orchestrator", "runs")
    found = [d for d in os.listdir(runs) if d.endswith(f"-{key}")]
    if len(found) != 1:
        raise RuntimeError(f"no single run directory for key {key} in {runs}: {found}")
    return os.path.join(runs, found[0])


def _put(path: str, text: str | None) -> None:
    if text is None:
        os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


# -- gh ---------------------------------------------------------------------


def gh(ledger: Ledger, args: list[str]) -> int:
    """Answer one gh command as gh would, logging it with its standard input; returns the exit status.

    `script["gh"]["create_hold"]`, when set, holds `pr create` until World.release names it: the fake waits, so
    that the engine can be stopped in the middle of publishing."""
    if args[:2] == ["auth", "status"]:
        print("github.com\n  Logged in to github.com account parity")
        return 0
    stdin = sys.stdin.read() if "--body-file" in args else None
    with ledger.change() as state:
        ledger.log(state, {"gh": args, **({"stdin": stdin} if stdin is not None else {})}, invocation(), ancestors())
        hold = ledger.script().get("gh", {}).get("create_hold") if args[:2] == ["pr", "create"] else None
        if hold:
            state.setdefault("reached", []).append(hold)
    while hold:
        with ledger.change() as state:
            if hold in state.get("released", []):
                break
        time.sleep(HOLD_POLL_SECONDS)
    with ledger.change() as state:
        prs = state.setdefault("prs", {})
        if args[:2] == ["pr", "create"]:
            n = _next_id(state, "pr")
            prs[str(n)] = {"base": _flag(args, "--base"), "head": _flag(args, "--head"),
                           "title": _flag(args, "--title"), "body": stdin, "isDraft": "--draft" in args,
                           "state": "OPEN", "closedAt": None, "mergeable": "MERGEABLE"}
            print(PR_URL.format(n))
            return 0
        if args[:2] in (["pr", "view"], ["pr", "edit"], ["pr", "ready"]):
            pr = prs.get(args[2].rstrip("/").rsplit("/", 1)[-1])
            if pr is None:
                print(f"no pull requests found for {args[2]}", file=sys.stderr)
                return 1
            if args[1] == "edit":
                pr["body"] = stdin
            elif args[1] == "ready":
                pr["isDraft"] = False
            else:
                view = {**pr, "headRefOid": _origin_commit(ledger.script()["origin"], pr["head"])}
                print(json.dumps({k: view[k] for k in _flag(args, "--json").split(",")}))
            return 0
    print(f"the fake gh has no command {' '.join(args)}", file=sys.stderr)
    return 1


def _origin_commit(origin: str, branch: str) -> str:
    """The commit of branch in the origin repository, or 40 zeros when it has none: gh still names the head that
    the pull request last had."""
    out = subprocess.run(["git", "-C", origin, "rev-parse", "--verify", "--quiet",  # noqa: S603, S607 - as gh's git
                          f"refs/heads/{branch}"], capture_output=True, text=True, check=False)
    return out.stdout.strip() or "0" * 40


# -- ssh and gitleaks ---------------------------------------------------------


def ssh(args: list[str]) -> int:
    """Run ssh's command, the argument after its options and target, with sh on this machine; its exit status."""
    i = 0
    while args[i].startswith("-"):
        i += 2 if args[i] in ("-o", "-p", "-i", "-l", "-F") else 1
    # The command the engine sent, run as sshd's login shell would run it.
    return subprocess.run(["sh", "-c", args[i + 1]], check=False).returncode  # noqa: S603, S607


def gitleaks(args: list[str]) -> int:
    """`gitleaks git --help`, and a scan that finds nothing, writing the empty report a real one writes."""
    if "--help" in args:
        print("Usage: gitleaks git [flags] [repo]")
        return 0
    with open(_flag(args, "--report-path"), "w") as f:
        f.write("[]\n")
    return 0


def main(argv: list[str]) -> int:
    world, program, args = argv[1], argv[2], argv[3:]
    ledger = Ledger(world)
    if program == "herdr":
        return herdr(ledger, args)
    if program == "gh":
        return gh(ledger, args)
    if program == "ssh":
        return ssh(args)
    if program == "gitleaks":
        return gitleaks(args)
    print(f"fakes.py: no fake {program}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
