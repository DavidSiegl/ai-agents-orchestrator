import ast
import base64
import contextlib
import copy
import http.client
import io
import itertools
import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, replace
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import orchestrator
from orchestrator import (
    APPROVE,
    BUILT_IN_PROMPTS,
    CHANGES_REQUESTED,
    CI,
    HEARTBEAT_SECONDS,
    STALE_SECONDS,
    CIError,
    Herdr,
    HerdrError,
    Host,
    OrchestratorError,
    RunState,
    RunTakenOver,
    Workflow,
    branch_name,
    changed_lines,
    main,
    parse_args,
    parse_gate,
    parse_verdict,
    pr_body,
    quality_report,
    spec_title,
)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def result(obj):
    return completed(json.dumps({"id": "cli:x", "result": obj}))


def no_processes(argv, **kw):
    raise AssertionError(f"FakeHost ran a real process: {argv}")


HOST_RUN_STATES = Host.run_states
# What a FakeHost's clock reads when the workflow's clock reads 0: far from it, so a time taken from the wrong
# clock shows.
HOST_EPOCH = 1_791_000_000


class FakeHost(Host):
    """An in-memory filesystem and git checkout standing in for the machine the agents run on.

    Files written outside .orchestrator/ count as uncommitted changes until a commit. Git is
    played at the level of single commands, so Host's own fetch, fast-forward and merge steps
    run on it. A test scripts origin with these attributes:

    - `upstream`: the commit origin's branch is at when HEAD lacks it; None when HEAD has it all.
    - `fetch_error`: the stderr of a failing fetch, e.g. offline or no such branch on origin.
    - `diverged`: the local branch and origin's each have commits the other lacks.
    - `conflicts`: the files a merge of `upstream` conflicts in.
    - `merge_head`: a merge is in progress, as one that conflicted and was never aborted leaves it.
    - `remote`: origin's branches that `push --force` created, by name; `ls-remote` lists them.
    - `diff`: what `git diff -U0` from the base to a snapshot prints.
    - `committed`: the text of each file in a commit, by (commit, path); file_at reads it, and records each read in
      `file_reads`.
    - `line_counts`: what changed_line_counts gives; None gives each file changed_lines finds in `diff`, under its
      own path, with the lines it adds and none removed.
    - `has_origin`: whether the checkout has an origin at all.
    - `ages`: the seconds since each run's state.json was written, by run id; 0 for one not listed.
    - `clock`: a FakeClock, which make_workflow sets to the workflow's. The host's own time is HOST_EPOCH later:
      now() reads it, and write() stamps each file's mtime with it. A file put straight into `files` has no mtime
      of its own, so mtime() gives the time now; `mtimes` sets one.

    For prune, which also reads `remote`:

    - `branches`: the local branches besides the checked-out one, by name, with their commit.
    - `tracking`: the branches origin/<name> exists for.
    - `worktrees`: the linked worktrees' branches, by path, "HEAD" for a detached one; /proj has `branch` checked
      out. git in a worktree sees its own branch, and the changed files under its path as its own changes.
    - `ancestors`: (commit, of) pairs where commit is an ancestor of of.
    - `ahead`: by branch, the commits it has beyond the commit rev-list --count compares it to.
    - `missing_commits`: commits this clone lacks.
    - `pull_requests`: what `gh pr view` prints, by URL: a dict, or a completed process for a failure. Both of
      Host's views print the whole dict.
    - `pr_edits`: each pull request edit refresh made, ("body", url, body) or ("ready", url), in order.
    - `remove_error`: the stderr of a failing `git worktree remove`.

    Each snapshot keeps the changed files' text in `trees`, which lock, locked_changes and restore play the locked
    files on; `refs` holds the refs lock made, and `restores` records each restore.

    `session_logs` holds each agent session's log, by (harness, session id), for the turns' tokens.

    For the secret scan before each push:

    - `leaks`: what scan_unpublished finds, as Leaks; every scan finds them all. `scans` records (commit, base,
      report) of each scan.
    - `gitleaks_missing`: check_gitleaks fails as on a machine without gitleaks.
    """

    def __init__(self, head="abc123", branch="main"):
        super().__init__(run=no_processes)
        self.files = {}
        self.head = head
        self.writes = []  # every path written, in order
        self.branch = branch
        self.changed = set()
        self.git_calls = []
        self.git_log = []  # (cwd, args) of each git call
        self.prs = []
        self.upstream = None
        self.fetch_error = None
        self.diverged = False
        self.conflicts = []
        self.merge_head = False
        self.snapshots = []  # (parent, message) of each snapshot, whose commit is snap<n>
        self.remote = {}
        self.diff = ""
        self.has_origin = True
        self.ages = {}
        self.branches = {}
        self.tracking = set()
        self.worktrees = {}
        self.ancestors = set()
        self.ahead = {}
        self.missing_commits = set()
        self.pull_requests = {}
        self.pr_edits = []
        self.remove_error = None
        self.gh_calls = []
        self.mtime_kept = []  # every path written with keep_mtime
        self.clock = None
        self.mtimes = {}
        self.leaks = []
        self.scans = []
        self.gitleaks_missing = False
        self.committed = {}
        self.file_reads = []
        self.line_counts = None
        self.trees = {}  # by snapshot: the text of each changed file then, None for a deleted one
        self.refs = {}  # the local refs lock made, with their snapshot
        self.restores = []  # (ref, paths) of each restore
        self.session_logs = {}  # by (harness, session): what session_log reads

    def scan_unpublished(self, cwd, commit, base, report_path):
        self.scans.append((commit, base, report_path))
        if self.leaks:
            # As a gitleaks run without --redact would write it: the report is the one place the secret may be.
            self.files[report_path] = json.dumps([{"Secret": FAKE_TOKEN, "File": leak.file} for leak in self.leaks])
        return list(self.leaks)

    def check_gitleaks(self):
        if self.gitleaks_missing:
            raise OrchestratorError(f"this run pushes, and every push is scanned for secrets first: "
                                    f"{orchestrator.GITLEAKS_INSTALL}")

    def now(self):
        return HOST_EPOCH + int(self.clock() if self.clock else 0)

    def mtime(self, path):
        return self.mtimes.get(path, self.now())

    def snapshot(self, cwd, parent, message):
        self.snapshots.append((parent, message))
        sha = f"snap{len(self.snapshots)}"
        self.trees[sha] = {p: self.files.get(p) for p in self._changed_in(cwd)}
        return sha

    def delete(self, path):
        """Delete a file of the working tree, as an agent may."""
        self.files.pop(path, None)
        self.changed.add(path)

    def lock(self, cwd, base, ref):
        sha = self.snapshot(cwd, base, f"locked files, {ref}")
        self.refs[ref] = sha
        return sorted(p.removeprefix(f"{cwd}/") for p in self.trees[sha])

    def locked_changes(self, cwd, ref, paths):
        tree = self.trees[self.refs[ref]]
        differ = [p for p in paths if self.files.get(f"{cwd}/{p}") != tree[f"{cwd}/{p}"]]
        return differ, "".join(f"--- {p}: {tree[f'{cwd}/{p}']}\n+++ {p}: {self.files.get(f'{cwd}/{p}')}\n"
                               for p in differ)

    def restore(self, cwd, ref, paths):
        self.restores.append((ref, list(paths)))
        tree = self.trees[self.refs[ref]]
        for p in paths:
            if tree[f"{cwd}/{p}"] is None:
                self.files.pop(f"{cwd}/{p}", None)
            else:
                self.files[f"{cwd}/{p}"] = tree[f"{cwd}/{p}"]

    def session_log(self, kind, session):
        return self.session_logs.get((kind, session))

    def delete_refs(self, cwd, prefix):
        self.refs = {ref: sha for ref, sha in self.refs.items() if not ref.startswith(prefix)}

    def read(self, path):
        return self.files.get(path)

    def write(self, path, text, keep_mtime=False):
        self.writes.append(path)
        if keep_mtime:
            self.mtime_kept.append(path)
        self.files[path] = text
        if not keep_mtime:
            self.mtimes[path] = self.now()
        if not path.startswith("/proj/.orchestrator/") or path.startswith(f"/proj/{orchestrator.WORKTREES_DIR}/"):
            self.changed.add(path)

    def rename(self, path, new_path):
        self.files[new_path] = self.files.pop(path)
        if path in self.mtimes:
            self.mtimes[new_path] = self.mtimes.pop(path)

    def git_head(self, cwd):
        return self.head

    def file_at(self, cwd, commit, path):
        self.file_reads.append((commit, path))
        return self.committed.get((commit, path))

    def changed_line_counts(self, cwd, base, commit):
        if self.line_counts is not None:
            return self.line_counts
        return touched(self.diff)

    def run_states(self, cwd):
        # A test that patches Host.run_states, for main to find the run to resume, decides it here too.
        if Host.run_states is not HOST_RUN_STATES:
            return super().run_states(cwd)
        runs = f"{cwd}/{orchestrator.RUNS_DIR}/"
        states = []
        for path, text in self.files.items():
            run_id, _, name = path.removeprefix(runs).partition("/")
            if path.startswith(runs) and name == "state.json":
                states.append((self.ages.get(run_id, 0), json.loads(text)))
        return states

    def is_worktree(self, cwd, path):
        return path in self.worktrees

    def _changed_in(self, cwd):
        """The changed files in the worktree at cwd, or else in the checkout: those outside every worktree."""
        if cwd in self.worktrees:
            return {p for p in self.changed if p.startswith(f"{cwd}/")}
        return {p for p in self.changed if not any(p.startswith(f"{w}/") for w in self.worktrees)}

    def git_run(self, cwd, *args, timeout=60, verbatim=False):
        self.git_calls.append(args)
        self.git_log.append((cwd, args))
        if cwd in self.worktrees:
            match args:
                case ("rev-parse", "--abbrev-ref", "HEAD"):
                    return completed(self.worktrees[cwd] + "\n")
                case ("switch", "-c", name, _):
                    self.worktrees[cwd] = name
                    return completed()
                case ("status", "--porcelain") | ("commit", *_):
                    changed = self._changed_in(cwd)
                    if args[0] == "status":
                        return completed("".join(f"?? {p}\n" for p in sorted(changed)))
                    self.changed -= changed
                    self.head += "+commit"
                    return completed()
        match args:
            case ("rev-parse", "--abbrev-ref", "HEAD"):
                return completed(self.branch + "\n")
            case ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
                return completed(returncode=0 if self.merge_head else 1)
            case ("status", "--porcelain"):
                return completed("".join(f"?? {p}\n" for p in sorted(self._changed_in(cwd))))
            case ("switch", "-c", name) | ("switch", "--quiet", name):
                self.branch = name
            case ("commit", *_):
                self.changed -= self._changed_in(cwd)
                self.head += "+commit"
            case ("worktree", "add", "--detach", path, _):
                self.worktrees[path] = "HEAD"
            case ("worktree", "remove", path) | ("worktree", "remove", "--force", path):
                if self.remove_error:
                    return completed(stderr=self.remove_error, returncode=128)
                del self.worktrees[path]
                self.changed = {p for p in self.changed if not p.startswith(f"{path}/")}
            case ("fetch", "--quiet", "origin", _) if self.fetch_error:
                return completed(stderr=self.fetch_error, returncode=128)
            case ("merge-base", "--is-ancestor", "HEAD", _):
                return completed(returncode=1 if self.diverged else 0)
            case ("merge-base", "--is-ancestor", _, "HEAD"):
                return completed(returncode=0 if self.upstream is None and not self.diverged else 1)
            case ("merge", "--ff-only", "--quiet", _):
                if self.diverged:
                    return completed(stderr="fatal: Not possible to fast-forward, aborting.", returncode=128)
                self.head, self.upstream = self.upstream or self.head, None
            case ("merge", "--no-edit", "--quiet", _):
                if self.conflicts:
                    self.merge_head = True
                    self.changed |= {f"/proj/{f}" for f in self.conflicts}
                    return completed(f"CONFLICT (content): Merge conflict in {self.conflicts[0]}", returncode=1)
                self.head, self.upstream = f"merge-{self.upstream}", None
            case ("diff", "--name-only", "-z", "--diff-filter=U"):
                return completed("".join(f"{f}\0" for f in self.conflicts) if self.merge_head else "")
            case ("merge", "--abort"):
                self.merge_head = False
                self.changed -= {f"/proj/{f}" for f in self.conflicts}
            case ("push", "--quiet", "--force", "origin", refspec):
                commit, ref = refspec.split(":")
                self.remote[ref.removeprefix("refs/heads/")] = commit
            case ("push", "--quiet", "origin", "--delete", ref):
                if self.remote.pop(ref.removeprefix("refs/heads/"), None) is None:
                    return completed(stderr=f"error: unable to delete '{ref}': remote ref does not exist",
                                     returncode=1)
            case ("ls-remote", "--heads", "origin"):
                return completed("".join(f"{c}\trefs/heads/{b}\n" for b, c in self.remote.items()))
            case ("-c", "core.quotePath=false", "diff", *_):
                return completed(self.diff)
            case ("remote", "get-url", "origin"):
                if not self.has_origin:
                    return completed(stderr="error: No such remote 'origin'", returncode=2)
                return completed("git@github.com:o/r.git\n")
            case ("ls-remote", "origin", ref):
                b = ref.removeprefix("refs/heads/")
                return completed(f"{self.remote[b]}\t{ref}\n" if b in self.remote else "")
            case ("push", "--quiet", lease, "origin", "--delete", ref) if lease.startswith("--force-with-lease="):
                b = ref.removeprefix("refs/heads/")
                if lease != f"--force-with-lease={ref}:{self.remote.get(b)}":
                    return completed(stderr=f" ! [rejected]        {b} (stale info)", returncode=1)
                del self.remote[b]
                self.tracking.discard(b)
            case ("update-ref", "-d", ref):
                self.tracking.discard(ref.removeprefix("refs/remotes/origin/"))
            case ("rev-parse", "-q", "--verify", ref) if ref.startswith("refs/heads/"):
                b = ref.removeprefix("refs/heads/")
                return completed(f"{self.branches[b]}\n") if b in self.branches else completed(returncode=1)
            case ("rev-parse", "-q", "--verify", obj) if obj.endswith("^{commit}"):
                return completed(returncode=1 if obj.removesuffix("^{commit}") in self.missing_commits else 0)
            case ("merge-base", "--is-ancestor", commit, of):
                return completed(returncode=0 if commit == of or (commit, of) in self.ancestors else 1)
            case ("rev-list", "--count", commits):
                return completed(f"{self.ahead[commits.partition('..refs/heads/')[2]]}\n")
            case ("branch", "--quiet", "-D", b):
                del self.branches[b]
            case ("worktree", "list", "--porcelain"):
                trees = {"/proj": self.branch, **self.worktrees}
                return completed("".join(f"worktree {path}\nHEAD {'0' * 40}\n"
                                         f"{'detached' if b == 'HEAD' else f'branch refs/heads/{b}'}\n\n"
                                         for path, b in trees.items()))
        return completed()

    def gh_run(self, cwd, *args):
        self.gh_calls.append(args)
        match args:
            case ("pr", "view", url, "--json", "state,headRefOid,closedAt" | "state,isDraft,mergeable,body"):
                pr = self.pull_requests[url]
                return pr if isinstance(pr, subprocess.CompletedProcess) else completed(json.dumps(pr))
        raise AssertionError(f"FakeHost got an unscripted gh command: {args}")

    def create_pr(self, cwd, base, head, title, body, draft):
        self.prs.append({"base": base, "head": head, "title": title, "body": body, "draft": draft})
        return "https://github.com/o/r/pull/7"

    def edit_pr_body(self, cwd, url, body):
        self.pr_edits.append(("body", url, body))

    def mark_pr_ready(self, cwd, url):
        self.pr_edits.append(("ready", url))


class FakeHerdr:
    """Plays each role by writing its handoff file when prompted.

    `script[role]` is a list of callables, one per turn of that role; each
    receives the prompt, may write files, and returns the status the agent
    shows afterwards.
    """

    def __init__(self, host, state, script):
        self.host = host
        self.state = state
        self.script = script
        self.calls = []
        self.panes = 0
        self.statuses = {}  # the agents alive, by name; an exited agent has no entry
        self.sessions = {}
        self.lost_sessions = set()  # sessions a harness cannot resume, so the agent exits at once
        self.kinds = []  # (name, kind) of each agent started, in order
        self.workspaces = set()
        self.live_panes = set()
        self.waits = []
        self.blocked_at_start = set()
        self.cwds = []  # (what, cwd) of each workspace created and pane split

    def _role(self, name):
        return name.split("-", 1)[0]

    def create_workspace(self, cwd, label):
        self.calls.append(("workspace", label))
        self.cwds.append(("workspace", cwd))
        self.workspaces.add("w1")
        return "w1", self._pane()

    def _pane(self):
        self.panes += 1
        pane = f"w1:p{self.panes}"
        self.live_panes.add(pane)
        return pane

    def split(self, pane, direction, cwd):
        self.calls.append(("split", pane, direction))
        self.cwds.append(("split", cwd))
        return self._pane()

    def rename_pane(self, pane, label):
        self.calls.append(("rename", pane, label))

    def start_agent(self, name, pane, kind, agent_args):
        self.calls.append(("start", name, pane, tuple(agent_args)))
        self.kinds.append((name, kind))
        session = resumed_session(kind, list(agent_args)) or f"session-{len(self.calls)}"
        if session in self.lost_sessions:
            return True
        self.statuses[name] = "idle"
        self.sessions[name] = session
        return self._role(name) not in self.blocked_at_start

    def prompt(self, name, text):
        self.calls.append(("prompt", name))
        self.statuses[name] = self.script[self._role(name)].pop(0)(text, self.state, self.host)

    def wait(self, name, timeout_ms, until=()):
        self.waits.append(until)
        return "done"

    def agent(self, name):
        if name not in self.statuses:
            return None
        record = {"agent_status": self.statuses[name]}
        if name in self.sessions:
            record["agent_session"] = {"value": self.sessions[name]}
        return record

    def status(self, name):
        return self.statuses.get(name)

    def workspace_exists(self, workspace):
        return workspace in self.workspaces

    def pane_exists(self, pane):
        return pane in self.live_panes

    def focus(self, name):
        self.calls.append(("focus", name))

    def close_workspace(self, workspace):
        self.calls.append(("close", workspace))
        self.workspaces.discard(workspace)


def resumed_session(kind, args):
    """The session a harness's arguments resume, as each harness takes it; None for a fresh one."""
    flag = orchestrator.RESUME_ARGS[kind]
    if kind == "codex":
        return args[1] if args[:1] == [flag] else None
    return args[args.index(flag) + 1] if flag in args else None


def writes(path_of, text, status="done"):
    """A turn that writes `text` to the path the state names, then settles."""
    def turn(prompt, state, host):
        host.write(path_of(state), text)
        return status
    return turn


def idle(prompt, state, host):
    return "done"


class FakeClock:
    """Time that passes only when the workflow sleeps; hooks play the world meanwhile."""

    def __init__(self):
        self.now = 0.0
        self.hooks = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for hook in self.hooks:
            hook()


def make_workflow(script, host=None, max_rounds=3, state=None, *, permission_mode=None, models=None, agent_kinds=None,
                  **kw):
    """A workflow on fakes; pass `state` to resume a saved run instead of starting a new one.

    permission_mode, models and agent_kinds make up the Workflow's AgentSettings.
    """
    host = host or FakeHost()
    state = state or RunState("20260929-120000-a1b2c3", "add a rate limiter", "/proj", None)
    herdr = FakeHerdr(host, state, script)
    notes = []
    clock = FakeClock()
    if host.clock is None:
        host.clock = clock
    agents = orchestrator.AgentSettings(permission_mode, models or {}, agent_kinds or {})
    wf = Workflow(herdr, host, state, notify=lambda t, b: notes.append(t), max_rounds=max_rounds, agents=agents,
                  clocks=orchestrator.Clocks(clock.sleep, clock, clock), **kw)
    return wf, herdr, host, notes


spec_turn = writes(lambda s: s.spec_path, "# Add a token-bucket rate limiter\n\n## Goal\nlimit requests")


def build_turn(n):
    def turn(prompt, state, host):
        host.write("/proj/limiter.py", f"version {n}")
        return writes(lambda s: s.build_path(n), f"report {n}")(prompt, state, host)
    return turn


def review_turn(n, verdict):
    return writes(lambda s: s.review_path(n), f"**VERDICT: {verdict}**\n1. finding")


def malformed_review(n, text="looks fine"):
    return writes(lambda s: s.review_path(n), text)


# What every prompt of a step that can block ends with.
NOTE = "\n\n" + orchestrator.BLOCKED_NOTE


def retry_prompt(path):
    return orchestrator.RETRY_VERDICT_PROMPT.format(
        path=path, rejected_path=path.removesuffix(".md") + ".rejected.md", approve=APPROVE, changes=CHANGES_REQUESTED)


class TestParseVerdict(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_verdict("VERDICT: APPROVE\n"), APPROVE)

    def test_markdown_emphasis_and_leading_blank_lines(self):
        self.assertEqual(parse_verdict("\n\n# VERDICT: CHANGES_REQUESTED\n1. x"), CHANGES_REQUESTED)

    def test_verdict_not_on_first_line_is_rejected(self):
        self.assertIsNone(parse_verdict("Looks good.\nVERDICT: APPROVE"))

    def test_empty(self):
        self.assertIsNone(parse_verdict(""))


class TestWorkflow(unittest.TestCase):
    def test_approved_first_round(self):
        wf, herdr, host, notes = make_workflow({
            "spec": [spec_turn],
            "build": [build_turn(1)],
            "review": [review_turn(1, APPROVE)],
        })

        self.assertEqual(wf.run(), APPROVE)

        starts = [c[1] for c in herdr.calls if c[0] == "start"]
        self.assertEqual(starts, ["spec-a1b2c3", "build-a1b2c3", "review-a1b2c3"])
        self.assertIn(("split", "w1:p1", "right"), herdr.calls)
        self.assertIn(("split", "w1:p2", "down"), herdr.calls)
        self.assertIn(("focus", "spec-a1b2c3"), herdr.calls)
        self.assertEqual(host.files["/proj/.orchestrator/.gitignore"], "*\n")
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual((saved["phase"], saved["verdict"], saved["base"]), ("done", APPROVE, "abc123"))
        self.assertEqual(notes[-1], f"Run finished: {APPROVE}")

    def test_review_prompt_names_the_base_commit(self):
        seen = []

        def review(prompt, state, host):
            seen.append(prompt)
            return review_turn(1, APPROVE)(prompt, state, host)

        wf, *_ = make_workflow({"spec": [spec_turn], "build": [build_turn(1)], "review": [review]})
        wf.run()
        self.assertIn("git diff abc123", seen[0])

    def test_changes_requested_loop_back_to_builder(self):
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn],
            "build": [build_turn(1), build_turn(2)],
            "review": [review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)],
        })

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.round, 2)
        prompts = [c[1] for c in herdr.calls if c[0] == "prompt"]
        self.assertEqual(prompts, ["spec-a1b2c3", "build-a1b2c3", "review-a1b2c3",
                                   "build-a1b2c3", "review-a1b2c3"])

    def test_stops_after_max_rounds(self):
        wf, *_ = make_workflow({
            "spec": [spec_turn],
            "build": [build_turn(1), build_turn(2)],
            "review": [review_turn(1, CHANGES_REQUESTED), review_turn(2, CHANGES_REQUESTED)],
        }, max_rounds=2)

        self.assertEqual(wf.run(), CHANGES_REQUESTED)
        self.assertEqual(wf.state.phase, "done")

    def test_blocked_turn_notifies_once_then_waits_for_the_file(self):
        wf, herdr, host, notes = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [lambda p, st, h: "blocked"],
        })
        polls = []

        def human_answers_on_third_poll():
            polls.append(1)
            if len(polls) == 3:
                herdr.statuses["review-a1b2c3"] = "working"
                host.write(wf.state.review_path(1), "VERDICT: APPROVE")
        wf.clock.hooks.append(human_answers_on_third_poll)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(notes.count("Reviewer needs your answer"), 1)

    def test_settled_status_is_not_the_end_of_a_turn(self):
        # Claude Code reports done while a background task it started runs on.
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "build": [lambda p, st, h: "done"], "review": [review_turn(1, APPROVE)],
        }, pull_request=False)
        wf.clock.hooks.append(lambda: host.write(wf.state.build_path(1), "report"))

        self.assertEqual(wf.run(), APPROVE)

    def test_idle_without_handoff_notifies_the_human(self):
        wf, herdr, host, notes = make_workflow({
            "spec": [spec_turn], "build": [idle], "review": [review_turn(1, APPROVE)],
        }, pull_request=False)

        def writes_after_stall():
            if wf.clock.now > orchestrator.STALL_SECONDS + 10:
                host.write(wf.state.build_path(1), "report")
        wf.clock.hooks.append(writes_after_stall)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(notes.count("Builder is idle without writing build-1.md"), 1)

    def test_idle_spec_collector_is_not_a_stall(self):
        wf, herdr, host, notes = make_workflow({
            "spec": [idle], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        })

        def human_approves_late():
            if wf.clock.now > 10 * orchestrator.STALL_SECONDS:
                host.write(wf.state.spec_path, "spec")
        wf.clock.hooks.append(human_approves_late)

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse([n for n in notes if "idle" in n])

    def test_startup_dialog_waits_for_the_human(self):
        wf, herdr, _, notes = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        })
        herdr.blocked_at_start = {"spec"}

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(notes[0], "Spec Collector needs your answer")
        self.assertEqual(herdr.waits, [("idle", "done")])

    def test_spec_collector_exiting_aborts(self):
        wf, herdr, host, _ = make_workflow({"spec": [idle]})
        wf.clock.hooks.append(herdr.statuses.clear)

        with self.assertRaisesRegex(OrchestratorError, "Spec Collector exited without writing"):
            wf.run()
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertIn("exited without writing", saved["error"])

    def test_builder_timeout(self):
        wf, *_ = make_workflow({"spec": [spec_turn], "build": [idle]}, turn_timeout=60)

        with self.assertRaisesRegex(OrchestratorError, "Builder did not write .*build-1.md within 60s"):
            wf.run()

    def test_empty_handoff_aborts(self):
        wf, *_ = make_workflow({"spec": [spec_turn], "build": [writes(lambda s: s.build_path(1), "\n")]})

        with self.assertRaisesRegex(OrchestratorError, "build-1.md was written empty by the Builder"):
            wf.run()

    def test_review_without_verdict_is_asked_for_once_more(self):
        seen = []
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn],
            "build": [build_turn(1)],
            "review": [recording(malformed_review(1), seen), recording(review_turn(1, APPROVE), seen)],
        })

        with patch.object(orchestrator, "log") as log:
            self.assertEqual(wf.run(), APPROVE)
        path = wf.state.review_path(1)
        rejected = f"{wf.state.dir}/review-1.rejected.md"
        self.assertEqual(seen[1], retry_prompt(path))
        self.assertIn(path, seen[1])
        self.assertIn(rejected, seen[1])
        self.assertIn(f"`VERDICT: {APPROVE}`", seen[1])
        self.assertIn(f"`VERDICT: {CHANGES_REQUESTED}`", seen[1])
        self.assertEqual(herdr.calls.count(("prompt", "review-a1b2c3")), 2)
        self.assertEqual(host.files[rejected], "looks fine")
        self.assertEqual(parse_verdict(host.files[path]), APPROVE)
        retries = [c.args[0] for c in log.call_args_list if "prompting once more" in c.args[0]]
        self.assertEqual(retries, ["[review] review-1.md does not start with a VERDICT line; "
                                   "moved it to review-1.rejected.md, prompting once more"])
        # The run goes on as if the first review had been valid: a pull request, and the retry spent.
        self.assertEqual(len(host.prs), 1)
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual((saved["phase"], saved["round"], saved["verdict"]), ("done", 1, APPROVE))
        self.assertEqual((saved["retried"], saved["retrying"]), (["review-1.md"], None))

    def test_empty_review_is_asked_for_once_more(self):
        seen = []
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn],
            "build": [build_turn(1)],
            "review": [malformed_review(1, " \n\n"), recording(review_turn(1, CHANGES_REQUESTED), seen)],
        }, max_rounds=1)

        self.assertEqual(wf.run(), CHANGES_REQUESTED)
        self.assertEqual(seen, [retry_prompt(wf.state.review_path(1))])
        self.assertEqual(host.files[f"{wf.state.dir}/review-1.rejected.md"], " \n\n")

    def test_review_malformed_twice_aborts(self):
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn],
            "build": [build_turn(1)],
            "review": [malformed_review(1), malformed_review(1, "still no verdict")],
        })

        with self.assertRaisesRegex(OrchestratorError, "review-1.md does not start with a VERDICT line, and its one "
                                    "retry was already used; fix it, or delete it to have it written again, "
                                    "then resume$"):
            wf.run()
        self.assertEqual(host.files[f"{wf.state.dir}/review-1.rejected.md"], "looks fine")
        self.assertEqual(host.files[wf.state.review_path(1)], "still no verdict")
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual((saved["retried"], saved["retrying"], saved["prompted"]), (["review-1.md"], None, None))

        # A resume does not grant a second retry.
        wf2, herdr2, *_ = resume(RunState.from_dict(saved), {"review": []}, alive=["review"], host=host)
        with self.assertRaisesRegex(OrchestratorError, "its one retry was already used"):
            wf2.run()
        self.assertNotIn(("prompt", "review-a1b2c3"), herdr2.calls)

    def test_empty_review_after_a_retry_aborts(self):
        wf, *_ = make_workflow({
            "spec": [spec_turn],
            "build": [build_turn(1)],
            "review": [malformed_review(1), malformed_review(1, "\n")],
        })

        with self.assertRaisesRegex(OrchestratorError, "review-1.md was written empty by the Reviewer, and its one "
                                    "retry was already used; fix it"):
            wf.run()

    def test_each_round_has_its_own_retry(self):
        seen = []
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn],
            "build": [build_turn(1), build_turn(2)],
            "review": [malformed_review(1), review_turn(1, CHANGES_REQUESTED),
                       recording(malformed_review(2), seen), recording(review_turn(2, APPROVE), seen)],
        }, max_rounds=2)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen[1], retry_prompt(wf.state.review_path(2)))
        self.assertIn(f"{wf.state.dir}/review-1.rejected.md", host.files)
        self.assertIn(f"{wf.state.dir}/review-2.rejected.md", host.files)
        # The retries used no review round: two rounds were enough under --max-rounds 2.
        self.assertEqual((wf.state.round, wf.state.retried), (2, ["review-1.md", "review-2.md"]))

    def test_not_a_git_repo(self):
        seen = []

        def review(prompt, state, host):
            seen.append(prompt)
            return review_turn(1, APPROVE)(prompt, state, host)

        wf, *_ = make_workflow({"spec": [spec_turn], "build": [build_turn(1)], "review": [review]},
                               host=FakeHost(head=None), pull_request=False)
        wf.run()
        self.assertIn("not a git repository", seen[0])

    def test_agent_args_reach_every_role(self):
        wf, herdr, *_ = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        }, permission_mode="auto")
        wf.run()
        args = {c[3] for c in herdr.calls if c[0] == "start"}
        self.assertEqual(args, {("--permission-mode", "auto")})
        self.assertEqual({kind for _, kind in herdr.kinds}, {"claude"})

    def start_args(self, **kw):
        wf, herdr, *_ = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        }, **kw)
        wf.run()
        return {c[1].split("-")[0]: c[3] for c in herdr.calls if c[0] == "start"}

    def test_no_models_adds_no_model_arg(self):
        self.assertEqual(self.start_args(), {"spec": (), "build": (), "review": ()})

    def test_each_role_gets_its_own_model(self):
        args = self.start_args(permission_mode="auto",
                               models={"spec": "sonnet", "review": "claude-opus-5-5"})
        self.assertEqual(args, {
            "spec": ("--permission-mode", "auto", "--model", "sonnet"),
            "build": ("--permission-mode", "auto"),
            "review": ("--permission-mode", "auto", "--model", "claude-opus-5-5"),
        })


class TestPullRequest(unittest.TestCase):
    def script(self, *verdicts):
        n = len(verdicts)
        return {
            "spec": [spec_turn],
            "build": [build_turn(i) for i in range(1, n + 1)],
            "review": [review_turn(i, v) for i, v in enumerate(verdicts, 1)],
        }

    def test_approved_change_becomes_a_pull_request(self):
        wf, herdr, host, notes = make_workflow(self.script(APPROVE))

        self.assertEqual(wf.run(), APPROVE)

        branch = "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3"
        self.assertIn(("switch", "-c", branch), host.git_calls)
        commit = next(c for c in host.git_calls if c[0] == "commit")
        self.assertEqual(commit[commit.index("-m") + 1], "Add a token-bucket rate limiter")
        self.assertIn(("push", "--quiet", "--set-upstream", "origin", branch), host.git_calls)
        pr = host.prs[0]
        self.assertEqual((pr["base"], pr["head"], pr["draft"]), ("main", branch, False))
        self.assertIn("## Goal", pr["body"])
        self.assertEqual(host.branch, "main")
        self.assertEqual(herdr.calls[-1], ("close", "w1"))
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual((saved["phase"], saved["pr_url"]), ("done", "https://github.com/o/r/pull/7"))

    def test_builder_works_on_the_branch(self):
        wf, herdr, host, _ = make_workflow(self.script(APPROVE))
        branches = []
        build = wf.herdr.script["build"][0]

        def build_on_branch(prompt, state, h):
            branches.append(h.branch)
            return build(prompt, state, h)
        wf.herdr.script["build"][0] = build_on_branch
        wf.run()
        self.assertEqual(branches, ["orchestrator/add-a-token-bucket-rate-limiter-a1b2c3"])

    def test_changes_still_requested_opens_a_draft(self):
        wf, herdr, host, _ = make_workflow(self.script(CHANGES_REQUESTED, CHANGES_REQUESTED), max_rounds=2)

        self.assertEqual(wf.run(), CHANGES_REQUESTED)
        self.assertTrue(host.prs[0]["draft"])
        self.assertIn("<details open>\n<summary>Review (round 2)</summary>", host.prs[0]["body"])
        self.assertIn(("close", "w1"), herdr.calls)

    def test_dirty_tree_fails_before_the_interview(self):
        host = FakeHost()
        host.changed.add("/proj/wip.py")
        wf, herdr, *_ = make_workflow({"spec": []}, host=host)

        with self.assertRaisesRegex(OrchestratorError, "uncommitted changes"):
            wf.run()
        self.assertFalse(herdr.calls)

    def test_tree_touched_during_interview_fails_before_branching(self):
        def spec_and_wip(prompt, state, host):
            host.write("/proj/wip.py", "x")
            return spec_turn(prompt, state, host)
        wf, _, host, _ = make_workflow({"spec": [spec_and_wip]})

        with self.assertRaisesRegex(OrchestratorError, "uncommitted changes"):
            wf.run()
        self.assertEqual(host.branch, "main")

    def test_detached_head_is_refused(self):
        wf, *_ = make_workflow({"spec": []}, host=FakeHost(branch="HEAD"))
        with self.assertRaisesRegex(OrchestratorError, "detached HEAD"):
            wf.run()

    def test_not_a_git_repo_needs_no_pr(self):
        wf, *_ = make_workflow({"spec": []}, host=FakeHost(head=None))
        with self.assertRaisesRegex(OrchestratorError, "--no-pr"):
            wf.run()

    def test_no_changes_keeps_the_workspace_open(self):
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn],
            "build": [writes(lambda s: s.build_path(1), "nothing to do")],
            "review": [review_turn(1, APPROVE)],
        })

        with self.assertRaisesRegex(OrchestratorError, "changed no files"):
            wf.run()
        self.assertFalse(host.prs)
        self.assertNotIn(("close", "w1"), herdr.calls)

    def test_close_failure_does_not_fail_the_run(self):
        wf, herdr, *_ = make_workflow(self.script(APPROVE))
        herdr.close_workspace = MagicMock(side_effect=HerdrError("workspace_not_found", "gone"))
        self.assertEqual(wf.run(), APPROVE)

    def test_no_pr_leaves_changes_and_workspace(self):
        wf, herdr, host, _ = make_workflow(self.script(APPROVE), pull_request=False)

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse(host.git_calls)
        self.assertFalse(host.prs)
        self.assertNotIn(("close", "w1"), herdr.calls)
        self.assertEqual(host.changed, {"/proj/limiter.py"})

    def test_no_pr_neither_fetches_nor_merges_even_when_origin_moved(self):
        host = FakeHost()
        host.upstream = "u1"
        wf, *_ = make_workflow(self.script(APPROVE), host=host, pull_request=False)

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse(host.git_calls)
        self.assertEqual(wf.state.base, "abc123")


class TestUpToDateBase(unittest.TestCase):
    """The base branch is brought up to origin's before the interview and the branch, and merged in before the push."""

    BRANCH = "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3"
    FETCH = ("fetch", "--quiet", "origin", "main")
    FAST_FORWARD = ("merge", "--ff-only", "--quiet", "origin/main")
    MERGE = ("merge", "--no-edit", "--quiet", "origin/main")

    def script(self, build=None, verdict=APPROVE, spec=spec_turn, review=None):
        return {"spec": [spec], "build": [build or build_turn(1)], "review": [review or review_turn(1, verdict)]}

    def test_base_is_fast_forwarded_before_the_interview(self):
        host = FakeHost()
        host.upstream = "u1"
        at_interview = []

        def spec(prompt, state, h):
            at_interview.append((list(h.git_calls), h.head))
            return spec_turn(prompt, state, h)
        wf, *_ = make_workflow(self.script(spec=spec), host=host)

        self.assertEqual(wf.run(), APPROVE)
        calls, head = at_interview[0]
        self.assertEqual(calls[calls.index(self.FETCH) + 1], self.FAST_FORWARD)
        self.assertEqual(head, "u1")

    def test_fetch_failure_stops_before_the_interview(self):
        host = FakeHost()
        host.fetch_error = "fatal: couldn't find remote ref main"
        wf, herdr, *_ = make_workflow({"spec": []}, host=host)

        with self.assertRaisesRegex(OrchestratorError, "could not fetch main from origin: "
                                    "git -C /proj fetch --quiet origin main failed: fatal: couldn't find"):
            wf.run()
        self.assertFalse(herdr.calls)
        self.assertIn("could not fetch main", json.loads(host.files[f"{wf.state.dir}/state.json"])["error"])

    def test_diverged_base_stops_before_the_interview(self):
        host = FakeHost()
        host.diverged = True
        wf, herdr, *_ = make_workflow({"spec": []}, host=host)

        with self.assertRaisesRegex(OrchestratorError, "main has diverged from origin/main; reconcile it first. "
                                    "git merge --ff-only --quiet origin/main failed"):
            wf.run()
        self.assertFalse(herdr.calls)

    def test_base_is_fast_forwarded_again_before_branching(self):
        def spec_while_origin_moves(prompt, state, h):
            h.upstream = "u2"
            return spec_turn(prompt, state, h)
        seen = []
        wf, _, host, _ = make_workflow(self.script(spec=spec_while_origin_moves,
                                                   review=recording(review_turn(1, APPROVE), seen)))

        self.assertEqual(wf.run(), APPROVE)
        switch = host.git_calls.index(("switch", "-c", self.BRANCH))
        self.assertEqual(host.git_calls[switch - 2:switch], [self.FETCH, self.FAST_FORWARD])
        self.assertEqual(wf.state.base, "u2")
        self.assertIn("git diff u2", seen[0])

    def test_switching_away_from_the_base_during_the_interview_is_refused(self):
        def spec_and_switch(prompt, state, h):
            h.branch = "wip"
            return spec_turn(prompt, state, h)
        wf, _, host, _ = make_workflow(self.script(spec=spec_and_switch))

        with self.assertRaisesRegex(OrchestratorError, "is on wip, not main"):
            wf.run()
        self.assertNotIn(("switch", "-c", self.BRANCH), host.git_calls)

    def test_moved_base_is_merged_before_the_push(self):
        def build_while_origin_moves(prompt, state, h):
            h.upstream = "u3"
            return build_turn(1)(prompt, state, h)
        wf, _, host, notes = make_workflow(self.script(build=build_while_origin_moves))

        self.assertEqual(wf.run(), APPROVE)
        calls = host.git_calls
        commit = next(i for i, c in enumerate(calls) if c[0] == "commit")
        push = calls.index(("push", "--quiet", "--set-upstream", "origin", self.BRANCH))
        self.assertLess(commit, calls.index(self.MERGE))
        self.assertLess(calls.index(self.MERGE), push)
        self.assertEqual(host.head, "merge-u3")
        self.assertFalse(host.prs[0]["draft"])
        self.assertFalse(host.prs[0]["body"].startswith("> [!WARNING]"))
        self.assertEqual(json.loads(host.files[f"{wf.state.dir}/state.json"])["conflicts"], [])
        self.assertFalse([n for n in notes if "conflicts" in n])

    def test_base_already_merged_is_not_merged_again(self):
        wf, _, host, _ = make_workflow(self.script())

        self.assertEqual(wf.run(), APPROVE)
        push = host.git_calls.index(("push", "--quiet", "--set-upstream", "origin", self.BRANCH))
        # Then the branch's commit is read, for the secret scan before the push.
        self.assertEqual(host.git_calls[push - 3:push],
                         [self.FETCH, ("merge-base", "--is-ancestor", "origin/main", "HEAD"),
                          ("rev-parse", "--verify", f"refs/heads/{self.BRANCH}")])
        self.assertNotIn(self.MERGE, host.git_calls)

    def conflicting(self, verdict=APPROVE):
        def build_while_origin_conflicts(prompt, state, h):
            h.upstream, h.conflicts = "u3", ["limiter.py", "docs/api.md"]
            return build_turn(1)(prompt, state, h)
        wf, herdr, host, notes = make_workflow(self.script(build=build_while_origin_conflicts, verdict=verdict),
                                                 max_rounds=1)
        self.assertEqual(wf.run(), verdict)
        return wf, host, notes

    def test_conflict_is_aborted_and_opens_a_draft_that_says_so(self):
        wf, host, notes = self.conflicting()

        calls = host.git_calls
        push = calls.index(("push", "--quiet", "--set-upstream", "origin", self.BRANCH))
        self.assertLess(calls.index(self.MERGE), calls.index(("merge", "--abort")))
        self.assertLess(calls.index(("merge", "--abort")), push)
        self.assertEqual(host.head, "abc123+commit")  # pushed without the merge
        self.assertFalse(host.merge_head)
        pr = host.prs[0]
        self.assertTrue(pr["draft"])
        warning = pr["body"].split("\n\n", 1)[0]
        self.assertTrue(warning.startswith("> [!WARNING]"))
        for name in ("`main`", "`limiter.py`", "`docs/api.md`"):
            self.assertIn(name, warning)
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual(saved["conflicts"], ["limiter.py", "docs/api.md"])
        self.assertIn("Pull request conflicts with main", notes)

    def test_conflict_with_changes_requested_is_a_draft_too(self):
        wf, host, _ = self.conflicting(CHANGES_REQUESTED)
        self.assertTrue(host.prs[0]["draft"])
        self.assertEqual(wf.state.conflicts, ["limiter.py", "docs/api.md"])

    def test_fetch_failure_at_publish_stops_and_resume_retries(self):
        def build_then_offline(prompt, state, h):
            h.fetch_error = "fatal: unable to access 'https://github.com/o/r/': Could not resolve host"
            return build_turn(1)(prompt, state, h)
        wf, herdr, host, _ = make_workflow(self.script(build=build_then_offline), max_rounds=1)

        with self.assertRaisesRegex(OrchestratorError, "could not fetch main from origin"):
            wf.run()
        self.assertFalse(host.prs)
        self.assertFalse([c for c in host.git_calls if c[0] == "push"])
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual(saved["phase"], "publish")

        host.fetch_error = None
        wf2, *_ = resume(RunState.from_dict(saved), {}, host=host)
        self.assertEqual(wf2.run(), APPROVE)
        self.assertEqual(len(host.prs), 1)
        self.assertEqual(len([c for c in host.git_calls if c[0] == "commit"]), 1)




class TestPullRequestText(unittest.TestCase):
    def test_spec_title(self):
        self.assertEqual(spec_title("\n# Add a limiter \n## Goal"), "Add a limiter")

    def test_spec_without_title(self):
        self.assertIsNone(spec_title("## Goal\n# Late title"))

    def test_branch_name(self):
        self.assertEqual(branch_name("Add a Token-Bucket limiter!", "20260929-120000-a1b2c3"),
                         "orchestrator/add-a-token-bucket-limiter-a1b2c3")

    def test_branch_name_without_letters(self):
        self.assertEqual(branch_name("???", "20260929-120000-a1b2c3"), "orchestrator/a1b2c3")

    def test_conflict_warning_comes_first(self):
        state = RunState("r", "t", "/proj", verdict=APPROVE, round=1, base_branch="main",
                         conflicts=["a.py", "docs/b.md"])
        body = pr_body(state, "spec", "r", "VERDICT: APPROVE", orchestrator.GateEvidence())
        self.assertEqual(body.split("\n\n", 1)[0],
                         "> [!WARNING]\n> This branch conflicts with `main`, so this is a draft. "
                         "Merge `main` into it and resolve the conflicts in:\n> - `a.py`\n> - `docs/b.md`")

    def test_the_conflict_warning_is_removed_and_nothing_else(self):
        warning = orchestrator.conflict_warning("main", ["a.py", "docs/b.md"])
        cases = [
            (f"{warning}\n\nOpened by run r.\n", "Opened by run r.\n"),
            (f"A note.\n\n{warning}\n\nOpened by run r.\n", "A note.\n\nOpened by run r.\n"),
            (f"Opened by run r.\n\n{warning}\n", "Opened by run r.\n"),
            (f"{warning}\n\nOpened by run r.\n".replace("\n", "\r\n"), "Opened by run r.\r\n"),
            (warning, ""),
            ("Opened by run r.\n", "Opened by run r.\n"),
            # Not the run's own warning: another base, other files, or edited.
            (f"{orchestrator.conflict_warning('dev', ['a.py', 'docs/b.md'])}\n\nx", None),
            (f"{orchestrator.conflict_warning('main', ['a.py'])}\n\nx", None),
            (f"{warning} Fixed it.\n\nx", None),
            (f"Text{warning}\n\nx", None),
        ]
        for body, without in cases:
            with self.subTest(body=body):
                self.assertEqual(orchestrator.without_conflict_warning(body, "main", ["a.py", "docs/b.md"]),
                                 body if without is None else without)

    def test_the_pull_request_body_without_its_warning_is_the_body_without_conflicts(self):
        fields = {"verdict": APPROVE, "round": 1, "base_branch": "main"}
        conflicted = RunState("r", "t", "/proj", conflicts=["a.py"], **fields)
        clean = RunState("r", "t", "/proj", **fields)
        body = pr_body(conflicted, "spec", "r", "VERDICT: APPROVE", orchestrator.GateEvidence())
        self.assertEqual(orchestrator.without_conflict_warning(body, "main", ["a.py"]),
                         pr_body(clean, "spec", "r", "VERDICT: APPROVE", orchestrator.GateEvidence()))

    def test_no_conflicts_no_warning(self):
        state = RunState("r", "t", "/proj", verdict=APPROVE, round=1, base_branch="main")
        body = pr_body(state, "spec", "r", "VERDICT: APPROVE", orchestrator.GateEvidence())
        self.assertTrue(body.startswith("Opened by"))

    def test_long_sections_are_truncated(self):
        state = RunState("r", "t", "/proj", verdict=APPROVE, round=1)
        body = pr_body(state, "x" * (orchestrator.PR_SECTION_LIMIT * 2), "r", "VERDICT: APPROVE",
                       orchestrator.GateEvidence())
        self.assertLess(len(body), 65536)
        self.assertIn("truncated", body)


class TestProvenance(unittest.TestCase):
    BRANCH = "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3"

    def test_a_role_run_in_two_harnesses_names_the_one_that_finished(self):
        state = saved_run("build", 1, agents=("spec", "build"), pull_request=True, base_branch="main",
                          branch=self.BRANCH, models={"build": "gpt-5"})
        state.agents["build"]["kind"] = "codex"
        wf, _, host, _ = resume(state, {"build": [build_turn(1)], "review": [review_turn(1, APPROVE)]},
                                host=FakeHost(branch=self.BRANCH), agent_kinds={"build": "claude"},
                                models={"build": "opus"})
        self.assertEqual(wf.run(), APPROVE)
        commit = next(c for c in host.git_calls if c[0] == "commit")
        self.assertEqual(commit[6:], ("--trailer", "Orchestrator-Run: 20260929-120000-a1b2c3",
                                      "--trailer", "Orchestrator-Workflow: default",
                                      "--trailer", "Orchestrator-Role: spec=claude/default",
                                      "--trailer", "Orchestrator-Role: build=claude/opus",
                                      "--trailer", "Orchestrator-Role: review=claude/default"))

    def test_the_gate_trailer_says_how_the_last_quality_round_ended(self):
        state = saved_run("publish", 2, agents=("build",), quality_job=JOB, quality_round=1)
        self.assertEqual(orchestrator.provenance(state, gate_evidence(2, 1, "GATE: OK\n"))[-1],
                         ("Orchestrator-Gate", "OK after 1 quality round"))
        self.assertEqual(orchestrator.provenance(state, gate_evidence(2, 3, "garbled"))[-1],
                         ("Orchestrator-Gate", "? after 3 quality rounds"))
        self.assertEqual([k for k, _ in orchestrator.provenance(state, orchestrator.GateEvidence())],
                         ["Orchestrator-Run", "Orchestrator-Workflow", "Orchestrator-Role"])

    def test_the_gate_trailer_says_when_the_gate_was_turned_off_after_it(self):
        state = saved_run("publish", 2, agents=("build",), quality_round=0, quality_gate_disabled=True)
        self.assertEqual(orchestrator.provenance(state, gate_evidence(1, 1, "GATE: ERROR\n", unjudged=True))[-1],
                         ("Orchestrator-Gate", "ERROR after 1 quality round, then turned off"))
        self.assertEqual([k for k, _ in orchestrator.provenance(state, orchestrator.GateEvidence(unjudged=True))],
                         ["Orchestrator-Run", "Orchestrator-Workflow", "Orchestrator-Role"])

    def test_git_reads_them_as_trailers(self):
        with tempfile.TemporaryDirectory() as d:
            host = Host()
            host.check(["git", "init", "--quiet", d])
            for key, setting in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
                host.git(d, "config", key, setting)
            state = saved_run("publish", 1, agents=("spec", "build", "review"), quality_job=JOB, quality_round=2,
                              verdict=APPROVE, models={"review": "gpt-5"})
            state.agents["review"]["kind"] = "codex"
            trailers = [arg for key, said in orchestrator.provenance(state, gate_evidence(1, 2, "GATE: OK\n"))
                        for arg in ("--trailer", f"{key}: {said}")]
            host.git(d, "commit", "--quiet", "--allow-empty", "-m", "Add a limiter", "-m",
                     orchestrator.commit_note(state), *trailers)
            message = host.git(d, "log", "-1", "--format=%B")
            self.assertEqual(host.check(["git", "interpret-trailers", "--parse"], stdin=message),
                             "Orchestrator-Run: 20260929-120000-a1b2c3\nOrchestrator-Workflow: default\n"
                             "Orchestrator-Role: spec=claude/default\nOrchestrator-Role: build=claude/default\n"
                             "Orchestrator-Role: review=codex/gpt-5\nOrchestrator-Gate: OK after 2 quality rounds\n")
            self.assertEqual(host.git(d, "log", "-1", "--format=%(trailers:key=Orchestrator-Role,valueonly)"),
                             "spec=claude/default\nbuild=claude/default\nreview=codex/gpt-5\n\n")
            self.assertIn(orchestrator.commit_note(state), message)


class TestHerdr(unittest.TestCase):
    def test_call_returns_result_and_forwards_machine(self):
        run = MagicMock(return_value=result({"agent": {"agent_status": "idle"}}))

        self.assertEqual(Herdr("remote", run=run).status("build-x"), "idle")
        self.assertEqual(run.call_args.args[0],
                         ["herdr", "--machine", "remote", "agent", "get", "build-x"])

    def test_prompt_does_not_wait(self):
        run = MagicMock(return_value=result({"type": "agent_prompted"}))
        Herdr(run=run).prompt("spec-x", "hi")
        self.assertEqual(run.call_args.args[0], ["herdr", "agent", "prompt", "spec-x", "hi"])

    def test_error_json_raises_with_code(self):
        err = json.dumps({"error": {"code": "agent_blocked", "message": "waiting"}})
        run = MagicMock(return_value=completed(stderr=err, returncode=1))

        herdr = Herdr(run=run)
        with self.assertRaises(HerdrError) as cm:
            herdr.call("agent", "prompt", "x", "y")
        self.assertEqual(cm.exception.code, "agent_blocked")

    def test_plain_text_error(self):
        run = MagicMock(return_value=completed(stderr="unknown flag", returncode=2))
        herdr = Herdr(run=run)
        with self.assertRaisesRegex(HerdrError, "exit_2: unknown flag"):
            herdr.call("agent", "bogus")

    def test_status_is_none_after_exit(self):
        err = json.dumps({"error": {"code": "agent_not_found", "message": "gone"}})
        run = MagicMock(return_value=completed(stderr=err, returncode=1))
        self.assertIsNone(Herdr(run=run).status("spec-x"))

    def test_start_agent_passes_the_harness_args_after_separator(self):
        run = MagicMock(return_value=result({}))
        Herdr(run=run).start_agent("build-x", "w1:p2", "claude", ["--permission-mode", "auto"])
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--kind") + 1], "claude")
        self.assertEqual(argv[-3:], ["--", "--permission-mode", "auto"])
        Herdr(run=run).start_agent("review-x", "w1:p3", "codex", [])
        self.assertEqual(run.call_args.args[0], ["herdr", "agent", "start", "review-x", "--kind", "codex",
                                                 "--pane", "w1:p3", "--timeout", "60000"])

    def test_start_agent_blocked_at_startup(self):
        err = json.dumps({"error": {"code": "agent_not_ready", "message": "blocked during startup"}})
        run = MagicMock(return_value=completed(stderr=err, returncode=1))
        self.assertFalse(Herdr(run=run).start_agent("spec-x", "w1:p1", "claude", []))

    def test_wait_passes_each_until(self):
        run = MagicMock(return_value=result({"agent": {"agent_status": "idle"}}))
        Herdr(run=run).wait("x", 1000, until=("working", "idle"))
        argv = run.call_args.args[0]
        self.assertEqual(argv.count("--until"), 2)
        self.assertEqual(run.call_args.kwargs["timeout"], 61)

    def test_unbounded_wait_has_no_process_limit(self):
        run = MagicMock(return_value=result({"agent": {"agent_status": "idle"}}))
        Herdr(run=run).wait("x", None, until=("idle",))
        self.assertNotIn("--timeout", run.call_args.args[0])
        self.assertIsNone(run.call_args.kwargs["timeout"])

    def test_ssh_target_by_label(self):
        profiles = [{"id": "5d45", "label": "remote", "target": "remote-host", "enabled": True}]
        run = MagicMock(return_value=completed(json.dumps(profiles)))

        self.assertEqual(Herdr("remote", run=run).ssh_target(), "remote-host")
        # Machine management is local; forwarding it would be rejected by herdr.
        self.assertEqual(run.call_args.args[0], ["herdr", "machine", "list", "--json"])

    def test_ssh_target_unknown_machine(self):
        run = MagicMock(return_value=completed("[]"))
        herdr = Herdr("nope", run=run)
        with self.assertRaisesRegex(OrchestratorError, "no saved herdr machine named nope"):
            herdr.ssh_target()

    def test_missing_binary(self):
        run = MagicMock(side_effect=FileNotFoundError())
        herdr = Herdr(run=run)
        with self.assertRaisesRegex(OrchestratorError, "not on PATH"):
            herdr.call("agent", "list")


class TestHost(unittest.TestCase):
    def test_ssh_wraps_command(self):
        run = MagicMock(return_value=completed("/home/user/proj\n"))
        cwd = Host("remote-host", run=run).resolve_dir("~/proj")

        self.assertEqual(cwd, "/home/user/proj")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["ssh", "-o", "BatchMode=yes", "remote-host"])
        self.assertIn('"$HOME$1"', argv[4])
        self.assertTrue(argv[4].endswith(" _ /proj"))

    def test_read_missing_file(self):
        run = MagicMock(return_value=completed(returncode=orchestrator.MISSING_FILE_STATUS))
        self.assertIsNone(Host(run=run).read("/x/spec.md"))

    def test_read_failure_raises(self):
        run = MagicMock(return_value=completed(stderr="Permission denied", returncode=1))
        host = Host(run=run)
        with self.assertRaisesRegex(OrchestratorError, "Permission denied"):
            host.read("/x/spec.md")

    def test_write_sends_text_on_stdin(self):
        run = MagicMock(return_value=completed())
        Host(run=run).write("/x/state.json", "{}")
        self.assertEqual(run.call_args.kwargs["input"], "{}")

    def test_create_pr_runs_gh_in_the_project(self):
        run = MagicMock(return_value=completed("Creating pull request\nhttps://github.com/o/r/pull/7\n"))
        url = Host("remote-host", run=run).create_pr("/proj", "main", "orchestrator/x", "Title", "body", draft=True)

        self.assertEqual(url, "https://github.com/o/r/pull/7")
        remote = run.call_args.args[0][4]
        self.assertIn("_ /proj gh pr create --base main --head orchestrator/x", remote)
        self.assertTrue(remote.endswith("--body-file - --draft"))
        self.assertEqual(run.call_args.kwargs["input"], "body")
        self.assertEqual(run.call_args.kwargs["timeout"], orchestrator.NETWORK_TIMEOUT)

    def test_git_head_outside_repo(self):
        run = MagicMock(return_value=completed(returncode=128))
        self.assertIsNone(Host(run=run).git_head("/x"))

    def test_run_states(self):
        out = '12 {"run_id": "a"}\n\n-1 {"run_id": "b"}\n\n'
        run = MagicMock(return_value=completed(out))
        self.assertEqual(Host(run=run).run_states("/x"), [(12, {"run_id": "a"}), (0, {"run_id": "b"})])

    def test_run_states_corrupt(self):
        run = MagicMock(return_value=completed('5 {"run_id": \n'))
        host = Host(run=run)
        with self.assertRaisesRegex(OrchestratorError, "corrupt run state"):
            host.run_states("/x")

    def test_real_filesystem_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            host = Host()
            path = f"{d}/nested/file.md"
            self.assertIsNone(host.read(path))
            host.write(path, "hello")
            self.assertEqual(host.read(path), "hello")
            self.assertEqual(host.resolve_dir(d), host.check(["sh", "-c", "cd -- \"$1\" && pwd", "_", d]).strip())
            host.rename(path, f"{d}/nested/file.rejected.md")
            self.assertIsNone(host.read(path))
            self.assertEqual(host.read(f"{d}/nested/file.rejected.md"), "hello")

    def test_now_and_mtime_on_the_real_host(self):
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/review 1.md"
            host = Host()
            host.write(path, "x")
            os.utime(path, (1_700_000_000, 1_700_000_000))
            self.assertEqual(host.mtime(path), 1_700_000_000)
            self.assertLessEqual(abs(host.now() - time.time()), 5)
            with self.assertRaisesRegex(OrchestratorError, "stat"):
                host.mtime(f"{d}/missing.md")

    def test_now_and_mtime_over_ssh(self):
        run = MagicMock(return_value=completed("1791000000\n"))
        host = Host("remote-host", run=run)
        self.assertEqual(host.now(), 1_791_000_000)
        self.assertEqual(run.call_args.args[0], ["ssh", "-o", "BatchMode=yes", "remote-host", "date +%s"])
        self.assertEqual(host.mtime("/x/review-1.md"), 1_791_000_000)
        self.assertEqual(run.call_args.args[0][:4], ["ssh", "-o", "BatchMode=yes", "remote-host"])
        self.assertTrue(run.call_args.args[0][4].endswith(" _ /x/review-1.md"))

    def test_a_time_that_is_no_number_raises(self):
        host = Host(run=MagicMock(return_value=completed("soon\n")))
        with self.assertRaisesRegex(OrchestratorError, "date \\+%s printed 'soon', not a time"):
            host.now()

    def test_rename_over_ssh(self):
        run = MagicMock(return_value=completed())
        Host("remote-host", run=run).rename("/x/review-1.md", "/x/review 1.rejected.md")
        self.assertEqual(run.call_args.args[0],
                         ["ssh", "-o", "BatchMode=yes", "remote-host", "mv -f -- /x/review-1.md '/x/review 1.rejected.md'"])

    def test_rename_failure_raises(self):
        host = Host(run=MagicMock(return_value=completed(stderr="No such file or directory", returncode=1)))
        with self.assertRaisesRegex(OrchestratorError, "No such file or directory"):
            host.rename("/x/review-1.md", "/x/review-1.rejected.md")

    def test_output_that_is_not_utf8_raises_naming_the_command(self):
        with self.assertRaisesRegex(OrchestratorError, r"^printf '\\351': printed what is not UTF-8"):
            Host().run(["printf", "\\351"])
        self.assertEqual(Host().run(["printf", "\\351"], verbatim=True).stdout, os.fsdecode(b"\xe9"))


class TestHostGit(unittest.TestCase):
    """Host's fetch, fast-forward and merge steps on real git: a bare origin, our checkout A and someone else's B."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.host = Host()
        self.sh("git", "init", "--quiet", "--bare", "-b", "main", f"{self.dir}/origin.git")
        self.a, self.b = self.clone("a"), self.clone("b")
        self.commit(self.b, "app.py", "one\n")
        self.sh("git", "-C", self.b, "push", "--quiet", "origin", "main")
        self.sh("git", "-C", self.a, "pull", "--quiet", "origin", "main")

    def sh(self, *argv):
        return self.host.check(list(argv))

    def clone(self, name):
        path = f"{self.dir}/{name}"
        self.sh("git", "clone", "--quiet", f"{self.dir}/origin.git", path)
        for key, value in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.sh("git", "-C", path, "config", key, value)
        return path

    def commit(self, repo, name, text):
        with open(f"{repo}/{name}", "w") as f:
            f.write(text)
        self.sh("git", "-C", repo, "add", name)
        self.sh("git", "-C", repo, "commit", "--quiet", "-m", f"change {name}")
        return self.head(repo)

    def head(self, repo):
        return self.host.git_head(repo)

    def push_from_b(self, name, text):
        upstream = self.commit(self.b, name, text)
        self.sh("git", "-C", self.b, "push", "--quiet", "origin", "main")
        return upstream

    def test_file_at_reads_a_file_as_a_commit_has_it(self):
        first = self.head(self.a)
        os.makedirs(f"{self.a}/sub")
        second = self.commit(self.a, "sub/lib.py", "two\n")
        self.assertEqual(self.host.file_at(self.a, second, "sub/lib.py"), "two\n")
        self.assertEqual(self.host.file_at(f"{self.a}/sub", second, "sub/lib.py"), "two\n")
        self.assertIsNone(self.host.file_at(self.a, first, "sub/lib.py"))
        self.assertIsNone(self.host.file_at(self.a, second, "sub"))
        self.assertEqual(self.host.file_at(self.a, first, "app.py"), "one\n")
        with self.assertRaises(OrchestratorError):
            self.host.file_at(self.a, "0" * 40, "app.py")

    def test_changed_line_counts_pair_renames_and_count_binary_files_as_none(self):
        self.commit(self.a, "gone.py", "x\ny\n")
        base = self.commit(self.a, "lib.py", "".join(f"line {i}\n" for i in range(20)))
        self.sh("git", "-C", self.a, "rm", "--quiet", "gone.py")
        os.makedirs(f"{self.a}/sub dir")
        self.sh("git", "-C", self.a, "mv", "lib.py", "sub dir/lib.py")
        with open(f"{self.a}/sub dir/lib.py", "a") as f:
            f.write("added\n")
        with open(f"{self.a}/app.py", "w") as f:
            f.write("two\n")
        with open(f"{self.a}/logo.png", "wb") as f:
            f.write(b"\x00\x01\x02")
        self.sh("git", "-C", self.a, "add", "-A")
        self.sh("git", "-C", self.a, "commit", "--quiet", "-m", "change")
        counts = self.host.changed_line_counts(self.a, base, self.head(self.a))
        self.assertEqual(sorted(counts), [
            orchestrator.ChangedFile("app.py", "app.py", 1, 1),
            orchestrator.ChangedFile("gone.py", "gone.py", 0, 2),
            orchestrator.ChangedFile("lib.py", "sub dir/lib.py", 1, 0),
            orchestrator.ChangedFile("logo.png", "logo.png", 0, 0)])

    def test_fast_forward_moves_the_base_to_origins(self):
        upstream = self.push_from_b("lib.py", "new\n")
        self.host.fast_forward(self.a, "main")
        self.assertEqual(self.head(self.a), upstream)

    def test_fast_forward_leaves_a_base_ahead_of_origin(self):
        ahead = self.commit(self.a, "local.py", "mine\n")
        self.host.fast_forward(self.a, "main")
        self.assertEqual(self.head(self.a), ahead)

    def test_fast_forward_refuses_a_diverged_base(self):
        self.push_from_b("lib.py", "theirs\n")
        mine = self.commit(self.a, "local.py", "mine\n")
        with self.assertRaisesRegex(OrchestratorError, "main has diverged from origin/main; reconcile it first. "
                                    "git merge --ff-only --quiet origin/main failed"):
            self.host.fast_forward(self.a, "main")
        self.assertEqual(self.head(self.a), mine)

    def test_fetch_of_a_branch_origin_lacks(self):
        with self.assertRaisesRegex(OrchestratorError, "could not fetch develop from origin: "
                                    "git -C .* fetch --quiet origin develop failed"):
            self.host.fast_forward(self.a, "develop")

    def feature_branch(self):
        self.sh("git", "-C", self.a, "switch", "--quiet", "-c", "feature")
        return self.commit(self.a, "app.py", "one\nfeature\n")

    def test_merge_of_a_moved_base_is_a_merge_commit(self):
        mine = self.feature_branch()
        upstream = self.push_from_b("lib.py", "new\n")

        self.assertEqual(self.host.merge_upstream(self.a, "main"), [])
        parents = self.sh("git", "-C", self.a, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
        self.assertEqual(parents, [mine, upstream])

    def test_merge_is_skipped_when_the_base_is_in_head(self):
        self.feature_branch()
        self.push_from_b("lib.py", "new\n")
        self.host.merge_upstream(self.a, "main")
        merged = self.head(self.a)

        self.assertEqual(self.host.merge_upstream(self.a, "main"), [])
        self.assertEqual(self.head(self.a), merged)

    def test_conflicting_merge_is_aborted_and_reported(self):
        mine = self.feature_branch()
        self.push_from_b("app.py", "one\ntheirs\n")

        self.assertEqual(self.host.merge_upstream(self.a, "main"), ["app.py"])
        self.assertEqual(self.head(self.a), mine)
        self.assertEqual(self.sh("git", "-C", self.a, "status", "--porcelain"), "")
        self.assertFalse(self.host.abort_merge(self.a))

    def test_abort_merge_ends_a_half_done_merge(self):
        mine = self.feature_branch()
        self.push_from_b("app.py", "one\ntheirs\n")
        self.sh("git", "-C", self.a, "fetch", "--quiet", "origin", "main")
        self.assertEqual(self.host.git_run(self.a, "merge", "origin/main").returncode, 1)

        self.assertTrue(self.host.abort_merge(self.a))
        self.assertEqual(self.head(self.a), mine)
        self.assertEqual(self.sh("git", "-C", self.a, "status", "--porcelain"), "")

    def pushed_run_branch(self):
        """A run branch with one commit, pushed from A; returns its commit."""
        self.sh("git", "-C", self.a, "switch", "--quiet", "-c", "orchestrator/x-a1b2c3")
        head = self.commit(self.a, "x.py", "x\n")
        self.sh("git", "-C", self.a, "push", "--quiet", "origin", "orchestrator/x-a1b2c3")
        self.sh("git", "-C", self.a, "switch", "--quiet", "main")
        return head

    def test_lease_guarded_delete_at_the_expected_commit(self):
        head = self.pushed_run_branch()
        self.assertEqual(self.host.remote_branch_commit(self.a, "orchestrator/x-a1b2c3"), head)
        self.host.delete_remote_branch_at(self.a, "orchestrator/x-a1b2c3", head)
        self.assertIsNone(self.host.remote_branch_commit(self.a, "orchestrator/x-a1b2c3"))
        self.assertEqual(self.sh("git", "-C", self.a, "for-each-ref", "refs/remotes/origin/orchestrator/"), "")

    def test_lease_guarded_delete_refuses_a_branch_that_moved(self):
        head = self.pushed_run_branch()
        self.sh("git", "-C", self.b, "fetch", "--quiet", "origin", "orchestrator/x-a1b2c3:orchestrator/x-a1b2c3")
        self.sh("git", "-C", self.b, "switch", "--quiet", "orchestrator/x-a1b2c3")
        moved = self.commit(self.b, "x.py", "x\ntheirs\n")
        self.sh("git", "-C", self.b, "push", "--quiet", "origin", "orchestrator/x-a1b2c3")

        with self.assertRaisesRegex(OrchestratorError, "(?s)--force-with-lease=refs/heads/orchestrator/x-a1b2c3:"
                                                       f"{head} origin --delete .* failed: .*stale info"):
            self.host.delete_remote_branch_at(self.a, "orchestrator/x-a1b2c3", head)
        self.assertEqual(self.host.remote_branch_commit(self.a, "orchestrator/x-a1b2c3"), moved)

    def test_remote_branch_commit_is_the_exact_name(self):
        self.pushed_run_branch()
        self.assertIsNone(self.host.remote_branch_commit(self.a, "x-a1b2c3"))

    def test_checked_out_branches_of_every_worktree(self):
        wt = f"{self.dir}/wt"
        self.sh("git", "-C", self.a, "worktree", "add", "--quiet", "-b", "orchestrator/y-a1b2c3", wt)
        self.assertEqual(self.host.checked_out_branches(self.a),
                         {"main": os.path.realpath(self.a), "orchestrator/y-a1b2c3": os.path.realpath(wt)})
        self.assertEqual(self.host.checked_out_branches(wt), self.host.checked_out_branches(self.a))

    def test_local_branch_steps(self):
        head = self.pushed_run_branch()
        older = self.sh("git", "-C", self.a, "rev-parse", "main").strip()
        self.assertEqual(self.host.branch_commit(self.a, "orchestrator/x-a1b2c3"), head)
        self.assertIsNone(self.host.branch_commit(self.a, "orchestrator/none"))
        self.assertTrue(self.host.is_ancestor(self.a, older, head))
        self.assertFalse(self.host.is_ancestor(self.a, head, older))
        self.assertEqual(self.host.commits_beyond(self.a, older, "orchestrator/x-a1b2c3"), 1)
        self.assertTrue(self.host.has_commit(self.a, head))
        self.assertFalse(self.host.has_commit(self.a, "f" * 40))
        self.host.delete_tracking_ref(self.a, "orchestrator/x-a1b2c3")
        self.host.delete_tracking_ref(self.a, "orchestrator/x-a1b2c3")
        self.assertEqual(self.sh("git", "-C", self.a, "for-each-ref", "refs/remotes/origin/orchestrator/"), "")
        self.host.delete_branch(self.a, "orchestrator/x-a1b2c3")
        self.assertIsNone(self.host.branch_commit(self.a, "orchestrator/x-a1b2c3"))

    def test_prune_on_real_git(self):
        """A squash-merged run branch: local behind the PR head, origin at it."""
        self.sh("git", "-C", self.a, "switch", "--quiet", "-c", "orchestrator/x-a1b2c3")
        behind = self.commit(self.a, "x.py", "x\n")
        head = self.commit(self.a, "x.py", "x\ny\n")
        self.sh("git", "-C", self.a, "push", "--quiet", "origin", "orchestrator/x-a1b2c3")
        self.sh("git", "-C", self.a, "reset", "--quiet", "--hard", behind)
        self.sh("git", "-C", self.a, "switch", "--quiet", "main")
        state = RunState("20261001-120000-a1b2c3", "x", self.a, None, phase="done", verdict=APPROVE,
                         branch="orchestrator/x-a1b2c3", pr_url="https://github.com/o/r/pull/7")
        self.host.write(f"{state.dir}/state.json", json.dumps(asdict(state)) + "\n")
        gh = {"state": "MERGED", "headRefOid": head, "closedAt": "2026-10-01T12:00:00Z"}
        self.host.gh_run = lambda cwd, *args: completed(json.dumps(gh))

        with patch("sys.stderr"):
            [run] = orchestrator.prune_branches(self.host, self.a, time.time, "here", lambda pid: False)
        self.assertEqual((run.local, run.remote, run.cleanup), ("deleted", "deleted", "deleted"))
        self.assertEqual(self.sh("git", "-C", self.a, "for-each-ref", "refs/heads/orchestrator/",
                                 "refs/remotes/origin/orchestrator/"), "")
        self.assertIsNone(self.host.remote_branch_commit(self.a, "orchestrator/x-a1b2c3"))
        self.assertEqual(json.loads(self.host.read(f"{state.dir}/state.json"))["branch_cleanup"], "deleted")

    def test_close_on_real_git(self):
        """A failed --worktree run: its dirty worktree is removed by force, and its branch, at base, deleted."""
        os.makedirs(f"{self.a}/.orchestrator")
        with open(f"{self.a}/.orchestrator/.gitignore", "w") as f:
            f.write("*\n")
        base = self.head(self.a)
        wt = f"{self.a}/{orchestrator.WORKTREES_DIR}/a1b2c3"
        self.host.add_worktree(self.a, wt, base)
        self.sh("git", "-C", wt, "switch", "--quiet", "-c", "orchestrator/x-a1b2c3")
        with open(f"{wt}/app.py", "w") as f:
            f.write("changed\n")
        with open(f"{wt}/new.py", "w") as f:
            f.write("new\n")
        state = RunState("20261001-120000-a1b2c3", "x", self.a, None, phase="build", round=1, workspace_id="w1",
                         base=base, base_branch="main", branch="orchestrator/x-a1b2c3", error="interrupted",
                         worktree=wt)
        path = f"{state.dir}/state.json"
        self.host.write(path, json.dumps(asdict(state)) + "\n")
        os.utime(path, (1_000_000_000, 1_000_000_000))
        herdr = MagicMock()
        herdr.workspace_exists.return_value = False

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            closed = orchestrator.close_run(herdr, self.host, self.a, "a1b2c3", "here", lambda pid: False)
        self.assertEqual(closed, "workspace already gone; worktree deleted; branch deleted")
        self.assertIn(f"discarding in {wt}:\n     M app.py\n    ?? new.py\n", out.getvalue())
        self.assertFalse(os.path.exists(wt))
        self.assertNotIn(wt, self.sh("git", "-C", self.a, "worktree", "list", "--porcelain"))
        self.assertIsNone(self.host.branch_commit(self.a, "orchestrator/x-a1b2c3"))
        self.assertEqual(json.loads(self.host.read(path))["closed"], closed)
        self.assertEqual(os.stat(path).st_mtime, 1_000_000_000)
        herdr.close_workspace.assert_not_called()


    def test_worktree_steps(self):
        os.makedirs(f"{self.a}/.orchestrator")
        with open(f"{self.a}/.orchestrator/.gitignore", "w") as f:
            f.write("*\n")
        wt = f"{self.a}/.orchestrator/worktrees/a1b2c3"
        upstream = self.push_from_b("lib.py", "new\n")
        self.assertFalse(self.host.is_worktree(self.a, wt))

        self.host.add_worktree(self.a, wt, self.host.fetch(self.a, "main"))
        self.assertTrue(self.host.is_worktree(self.a, wt))
        self.assertEqual(self.head(wt), upstream)
        self.assertEqual(self.sh("git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD\n")
        self.assertEqual(self.sh("git", "-C", self.a, "status", "--porcelain"), "")
        self.assertFalse(self.host.is_worktree(self.a, self.b))  # another clone's checkout
        self.assertFalse(self.host.is_worktree(self.a, f"{self.dir}/nowhere"))

        with open(f"{wt}/new.py", "w") as f:
            f.write("x\n")
        with self.assertRaisesRegex(OrchestratorError, "worktree remove .* failed: .*untracked"):
            self.host.remove_worktree(self.a, wt)
        os.remove(f"{wt}/new.py")
        self.host.remove_worktree(self.a, wt)
        self.assertFalse(os.path.exists(wt))
        self.assertFalse(self.host.is_worktree(self.a, wt))

    def test_a_deleted_worktree_is_no_worktree(self):
        wt = f"{self.dir}/wt"
        self.host.add_worktree(self.a, wt, "HEAD")
        shutil.rmtree(wt)
        self.assertIn(f"worktree {wt}", self.sh("git", "-C", self.a, "worktree", "list", "--porcelain"))
        self.assertFalse(self.host.is_worktree(self.a, wt))

    def test_worktree_through_a_symlink(self):
        os.symlink(self.a, f"{self.dir}/link")
        wt = f"{self.dir}/link/wt"
        self.host.add_worktree(f"{self.dir}/link", wt, "HEAD")
        self.assertTrue(self.host.is_worktree(f"{self.dir}/link", wt))
        self.assertTrue(self.host.is_worktree(self.a, f"{self.a}/wt"))

    def test_snapshot_in_a_worktree_holds_only_its_change(self):
        base = self.head(self.a)
        wt = f"{self.dir}/wt"
        self.host.add_worktree(self.a, wt, base)
        with open(f"{self.a}/app.py", "w") as f:
            f.write("one\nchecked out\n")
        with open(f"{self.a}/mine.py", "w") as f:
            f.write("mine\n")
        with open(f"{wt}/feature.py", "w") as f:
            f.write("feature\n")

        sha = self.host.snapshot(wt, base, "snapshot")
        self.assertEqual(self.sh("git", "-C", wt, "diff", "--name-only", base, sha), "feature.py\n")
        self.assertEqual(changed_lines(self.host.change_diff(wt, base, sha)), {"feature.py": {1}})
        self.assertEqual(self.sh("git", "-C", self.a, "status", "--porcelain"), " M app.py\n?? mine.py\n")

    def put(self, name, text=None):
        """Write a file of checkout A, or delete it with no text."""
        path = f"{self.a}/{name}"
        if text is None:
            os.remove(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def read(self, name):
        with open(f"{self.a}/{name}") as f:
            return f.read()

    def test_locked_files_are_restored_and_the_index_is_left_alone(self):
        self.commit(self.a, "gone.py", "old\n")
        self.commit(self.a, "removed.py", "old\n")
        base = self.head(self.a)
        ref = "refs/orchestrator/a1b2c3/locked/tests"
        self.put("tests/test_[x].py", "assert f()\n")
        self.put("app.py", "one\ntested\n")
        self.put("gone.py")
        # Deleted from the index too, so that git restore would know it from neither side.
        self.sh("git", "-C", self.a, "rm", "--quiet", "removed.py")
        self.assertEqual(self.host.lock(self.a, base, ref), ["app.py", "gone.py", "removed.py", "tests/test_[x].py"])
        locked = self.host.lock(self.a, base, ref)

        # A later step edits one, deletes another, brings back the third and changes a file nobody locked.
        self.put("tests/test_[x].py")
        self.put("app.py", "one\nweakened\n")
        self.put("gone.py", "back\n")
        self.put("removed.py", "back\n")
        self.put("feature.py", "new\n")
        os.makedirs(f"{self.a}/sub")
        differ, diff = self.host.locked_changes(f"{self.a}/sub", ref, locked)
        self.assertEqual(differ, ["app.py", "gone.py", "removed.py", "tests/test_[x].py"])
        self.assertIn("-tested\n+weakened\n", diff)
        self.assertIn("deleted file mode", diff)
        porcelain = self.sh("git", "-C", self.a, "status", "--porcelain")

        self.host.restore(f"{self.a}/sub", ref, differ)
        self.assertEqual((self.read("tests/test_[x].py"), self.read("app.py"), self.read("feature.py")),
                         ("assert f()\n", "one\ntested\n", "new\n"))
        self.assertFalse(os.path.exists(f"{self.a}/gone.py"))
        self.assertFalse(os.path.exists(f"{self.a}/removed.py"))
        self.assertEqual(self.host.locked_changes(self.a, ref, locked), ([], ""))
        # The index holds only the deletion staged above.
        self.assertEqual(self.sh("git", "-C", self.a, "diff", "--cached", "--name-only"), "removed.py\n")
        self.assertNotEqual(self.sh("git", "-C", self.a, "status", "--porcelain"), porcelain)

        other = "refs/orchestrator/d4e5f6/locked/tests"
        self.sh("git", "-C", self.a, "update-ref", other, base)
        self.host.delete_refs(self.a, "refs/orchestrator/a1b2c3/")
        self.assertEqual(self.sh("git", "-C", self.a, "for-each-ref", "--format=%(refname)", "refs/orchestrator/"),
                         f"{other}\n")
        self.host.delete_refs(self.a, "refs/orchestrator/a1b2c3/")


class TestCLI(unittest.TestCase):
    def test_machine_requires_cwd(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["run", "task", "--machine", "remote"])

    def test_max_rounds_must_be_positive(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["run", "task", "--max-rounds", "0"])

    @patch.dict("os.environ", {"HERDR_ENV": ""})
    def test_refuses_outside_herdr(self):
        with patch("sys.stderr") as err:
            self.assertEqual(main(["list"]), orchestrator.EXIT_ERROR)
        self.assertIn("not inside a herdr pane", "".join(c.args[0] for c in err.write.call_args_list))

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_permission_mode_reaches_the_workflow(self, _resolve, _run, init):
        with patch("builtins.print"):
            self.assertEqual(main(["run", "task", "--permission-mode", "auto"]), 0)
        self.assertEqual(init.call_args.kwargs["agents"].permission_mode, "auto")

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_models_reach_the_workflow(self, _resolve, _run, init):
        with patch("builtins.print"):
            self.assertEqual(main(["run", "task", "--permission-mode", "auto",
                                   "--model", "sonnet", "--review-model", "opus"]), 0)
        self.assertEqual(init.call_args.kwargs["agents"].models,
                         {"spec": "sonnet", "build": "sonnet", "review": "opus"})
        self.assertEqual(init.call_args.kwargs["agents"].permission_mode, "auto")

    def test_role_models(self):
        cases = [
            ([], {}),
            (["--model", "claude-opus-5-5"],
             {"spec": "claude-opus-5-5", "build": "claude-opus-5-5", "review": "claude-opus-5-5"}),
            (["--model", "sonnet", "--review-model", "opus"],
             {"spec": "sonnet", "build": "sonnet", "review": "opus"}),
            (["--build-model", "sonnet"], {"build": "sonnet"}),
        ]
        for flags, models in cases:
            with self.subTest(flags=flags):
                self.assertEqual(orchestrator.role_models(parse_args(["run", "task", *flags])), models)

    def test_model_flags_are_run_only(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["list", "--model", "sonnet"])

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_no_pr_flag(self, _resolve, _run, init):
        with patch("builtins.print"):
            main(["run", "task"])
            main(["run", "task", "--no-pr"])
        self.assertEqual([c.kwargs["pull_request"] for c in init.call_args_list], [True, False])

    @patch.object(Workflow, "run", return_value=CHANGES_REQUESTED)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_run_exit_code_when_changes_remain(self, _resolve, _run):
        with patch("builtins.print"):
            self.assertEqual(main(["run", "task"]), orchestrator.EXIT_CHANGES_REQUESTED)

    @patch.object(Host, "run_states", return_value=[])
    @patch.object(Host, "resolve_dir", return_value="/home/user/proj")
    @patch.object(Herdr, "ssh_target", return_value="remote-host")
    def test_list_on_machine_uses_ssh_host(self, _target, _resolve, _states):
        with patch("builtins.print") as out:
            self.assertEqual(main(["list", "--machine", "remote", "--cwd", "~/proj"]), 0)
        out.assert_called_with("No runs.")


def saved_run(phase, rnd, *, agents=("spec",), files=(), **kw):
    """A run as an earlier orchestrator left it, in workspace w1 whose root pane is w1:p1."""
    panes = {"spec": "w1:p1", "build": "w1:p2", "review": "w1:p3"}
    state = RunState("20260929-120000-a1b2c3", "add a rate limiter", "/proj", None,
                     phase=phase, round=rnd, workspace_id="w1", root_pane="w1:p1",
                     base="abc123" if phase != "spec" else None,
                     agents={r: {"name": f"{r}-a1b2c3", "pane": panes[r]} for r in agents}, **kw)
    return state


def resume(state, script, *, alive=(), sessions=None, files=None, host=None, **kw):
    """make_workflow on a saved run: `alive` names the roles whose agents still run."""
    host = host or FakeHost()
    # As main does: a resumed run keeps the choice it was started with.
    kw.setdefault("pull_request", state.pull_request)
    wf, herdr, host, notes = make_workflow(script, host=host, state=state, **kw)
    herdr.workspaces.add("w1")
    herdr.live_panes.update(a["pane"] for a in state.agents.values())
    herdr.live_panes.add("w1:p1")
    herdr.panes = 3
    for role in alive:
        herdr.statuses[f"{role}-a1b2c3"] = "working"
    for role, session in (sessions or {}).items():
        state.agents[role]["session"] = session
    for path_of, text in (files or {}).items():
        host.files[path_of(state)] = text
    return wf, herdr, host, notes


def recording(turn, seen):
    def wrapped(prompt, state, host):
        seen.append(prompt)
        return turn(prompt, state, host)
    return wrapped


class TestResume(unittest.TestCase):
    def test_spec_written_while_orchestrator_was_gone(self):
        # Run e292fb: the collector wrote spec.md and exited after the orchestrator died.
        saved = {"run_id": "20260929-120000-a1b2c3", "task": "add a rate limiter", "cwd": "/proj",
                 "machine": "remote", "phase": "spec", "round": 0, "workspace_id": "w1", "base": None,
                 "verdict": None, "error": None, "agents": {"spec": {"name": "spec-a1b2c3", "pane": "w1:p1"}}}
        wf, herdr, host, _ = resume(RunState.from_dict(saved), {
            "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        }, files={lambda s: s.spec_path: "# Goal\nx"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(("prompt", "spec-a1b2c3"), herdr.calls)
        self.assertFalse([c for c in herdr.calls if c[0] in ("workspace", "focus")])
        self.assertIn(("split", "w1:p1", "right"), herdr.calls)
        self.assertEqual(wf.state.base, "abc123")

    def test_live_prompted_collector_is_only_waited_on(self):
        wf, herdr, host, notes = resume(saved_run("spec", 0, prompted="spec.md"), {
            "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        }, alive=["spec"])
        wf.clock.hooks.append(lambda: host.write(wf.state.spec_path, "# Goal\nx"))

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(("prompt", "spec-a1b2c3"), herdr.calls)
        self.assertNotIn("start", [c[0] for c in herdr.calls if c[1] == "spec-a1b2c3"])
        self.assertIn(("focus", "spec-a1b2c3"), herdr.calls)
        self.assertEqual(notes[0], "Spec Collector is waiting for you")

    def test_live_collector_never_prompted_is_prompted_once(self):
        wf, herdr, *_ = resume(saved_run("spec", 0), {
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        }, alive=["spec"])

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(herdr.calls.count(("prompt", "spec-a1b2c3")), 1)

    def test_collector_without_session_restarts_the_interview(self):
        wf, herdr, host, notes = resume(saved_run("spec", 0, prompted="spec.md"), {
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        })

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn(("start", "spec-a1b2c3", "w1:p1", ()), herdr.calls)
        self.assertEqual(notes[0], "Spec Collector restarted; the interview starts over")

    def test_exited_builder_resumes_its_session_and_continues(self):
        seen = []
        wf, herdr, *_ = resume(saved_run("build", 2, agents=("spec", "build", "review"), prompted="build-2.md"), {
            "build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)],
        }, alive=["review"], sessions={"build": "s-build"})

        self.assertEqual(wf.run(), APPROVE)
        starts = [c for c in herdr.calls if c[0] == "start"]
        self.assertEqual(starts, [("start", "build-a1b2c3", "w1:p2", ("--resume", "s-build"))])
        self.assertEqual(seen, [orchestrator.CONTINUE_PROMPT.format(path=wf.state.build_path(2))])

    def test_resumed_builder_not_yet_prompted_gets_its_normal_prompt(self):
        seen = []
        wf, *_ = resume(saved_run("build", 2, agents=("spec", "build", "review"), prompted="review-1.md"), {
            "build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)],
        }, alive=["review"], sessions={"build": "s-build"})

        wf.run()
        self.assertTrue(seen[0].startswith("The Reviewer requested changes"))

    def test_exited_builder_without_session_gets_a_recovery_prompt(self):
        seen = []
        wf, herdr, *_ = resume(saved_run("build", 2, agents=("spec", "build", "review"), prompted="build-2.md"), {
            "build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)],
        }, alive=["review"])

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn(("start", "build-a1b2c3", "w1:p2", ()), herdr.calls)
        self.assertIn("You are the Builder", seen[0])
        self.assertIn("This is round 2, and you are a fresh session", seen[0])
        self.assertIn(wf.state.review_path(1), seen[0])

    def test_lost_session_falls_back_to_a_fresh_one(self):
        seen = []
        wf, herdr, *_ = resume(saved_run("build", 2, agents=("spec", "build", "review")), {
            "build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)],
        }, alive=["review"], sessions={"build": "s-gone"})
        herdr.lost_sessions.add("s-gone")

        self.assertEqual(wf.run(), APPROVE)
        starts = [c[3] for c in herdr.calls if c[0] == "start"]
        self.assertEqual(starts, [("--resume", "s-gone"), ()])
        self.assertIn("You are the Builder", seen[0])
        self.assertNotEqual(wf.state.agents["build"]["session"], "s-gone")

    def test_fresh_reviewer_in_a_later_round_is_told_about_earlier_reviews(self):
        seen = []
        wf, *_ = resume(saved_run("review", 2, agents=("spec", "build", "review")), {
            "review": [recording(review_turn(2, APPROVE), seen)],
        }, files={lambda s: s.build_path(2): "report 2"})

        wf.run()
        self.assertIn("You are the Reviewer", seen[0])
        self.assertIn(f"earlier reviews of this change are {wf.state.review_path(1)}", seen[0])

    def test_review_already_written_is_not_asked_for(self):
        wf, herdr, *_ = resume(saved_run("review", 1, agents=("spec", "build", "review")), {},
                               files={lambda s: s.review_path(1): "VERDICT: APPROVE"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse([c for c in herdr.calls if c[0] in ("prompt", "start")])
        self.assertEqual(wf.state.phase, "done")

    def test_base_comes_from_the_saved_state(self):
        seen = []
        wf, herdr, *_ = resume(saved_run("build", 1), {
            "build": [build_turn(1)], "review": [recording(review_turn(1, APPROVE), seen)],
        }, host=FakeHost(head="moved"))

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn("git diff abc123", seen[0])
        # A crash before the Builder was recorded: it is started in a split of the root pane.
        self.assertIn(("split", "w1:p1", "right"), herdr.calls)

    def test_done_is_a_no_op(self):
        state = saved_run("done", 2, agents=("spec", "build", "review"), verdict=CHANGES_REQUESTED)
        wf, herdr, host, _ = resume(state, {})

        self.assertEqual(wf.run(), CHANGES_REQUESTED)
        self.assertEqual(herdr.calls, [])
        self.assertEqual(host.writes, [])

    def test_invalid_review_is_refused_until_deleted(self):
        # As a run that stopped because its retry was used left it.
        state = saved_run("review", 1, agents=("spec", "build", "review"), retried=["review-1.md"],
                          error="review-1.md does not start with a VERDICT line, and its one retry was already used")
        wf, herdr, host, _ = resume(state, {"review": [review_turn(1, APPROVE)]},
                                    alive=["review"], files={lambda s: s.review_path(1): "looks fine"})

        with self.assertRaisesRegex(OrchestratorError, "review-1.md does not start with a VERDICT line, and its one "
                                    "retry was already used; fix it"):
            wf.run()
        saved = json.loads(host.files[f"{state.dir}/state.json"])
        self.assertIsNone(saved["prompted"])
        self.assertNotIn(("prompt", "review-a1b2c3"), herdr.calls)

        del host.files[state.review_path(1)]
        seen = []
        wf2, herdr2, *_ = resume(RunState.from_dict(saved), {"review": [recording(review_turn(1, APPROVE), seen)]},
                                 alive=["review"], host=host)
        self.assertEqual(wf2.run(), APPROVE)
        self.assertEqual(herdr2.calls.count(("prompt", "review-a1b2c3")), 1)
        self.assertTrue(seen[0].startswith("You are the Reviewer"))
        self.assertIsNone(wf2.state.error)

    def test_malformed_review_found_on_resume_is_retried(self):
        # Saved by a version without retries: state.json has neither retried nor retrying.
        saved = asdict(saved_run("review", 1, agents=("spec", "build", "review"), prompted="review-1.md"))
        del saved["retried"], saved["retrying"]
        seen = []
        wf, herdr, host, _ = resume(RunState.from_dict(saved), {"review": [recording(review_turn(1, APPROVE), seen)]},
                                    alive=["review"], files={lambda s: s.review_path(1): "looks fine"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen, [retry_prompt(wf.state.review_path(1))])
        self.assertEqual(host.files[f"{wf.state.dir}/review-1.rejected.md"], "looks fine")
        self.assertEqual(wf.state.retried, ["review-1.md"])

    def retrying_run(self):
        """A run that stopped after the Reviewer was given the retry prompt, before it wrote review-1.md again."""
        state = saved_run("review", 1, agents=("spec", "build", "review"), prompted="review-1.md",
                          retried=["review-1.md"], retrying="review-1.md")
        return state, {lambda s: f"{s.dir}/review-1.rejected.md": "looks fine"}

    def test_live_reviewer_mid_retry_is_only_waited_for(self):
        state, files = self.retrying_run()
        wf, herdr, host, _ = resume(state, {"review": []}, alive=["review"], files=files)
        wf.clock.hooks.append(lambda: host.write(state.review_path(1), f"VERDICT: {APPROVE}\n"))

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(("prompt", "review-a1b2c3"), herdr.calls)
        self.assertIsNone(wf.state.retrying)

    def test_resumed_reviewer_mid_retry_continues_it(self):
        state, files = self.retrying_run()
        seen = []
        wf, *_ = resume(state, {"review": [recording(review_turn(1, APPROVE), seen)]},
                        sessions={"review": "s-review"}, files=files)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen, [orchestrator.CONTINUE_PROMPT.format(path=state.review_path(1))])

    def test_fresh_reviewer_mid_retry_gets_the_retry_prompt_and_no_second_retry(self):
        state, files = self.retrying_run()
        seen = []
        wf, herdr, host, _ = resume(state, {"review": [recording(malformed_review(1, "again"), seen)]}, files=files)

        with self.assertRaisesRegex(OrchestratorError, "its one retry was already used"):
            wf.run()
        self.assertEqual(seen, [retry_prompt(state.review_path(1))])
        self.assertEqual(host.files[f"{state.dir}/review-1.rejected.md"], "looks fine")
        self.assertEqual(host.files[state.review_path(1)], "again")

    def test_reviewer_not_yet_given_the_retry_prompt_gets_it(self):
        # Stopped between saving the retry and delivering its prompt.
        state, files = self.retrying_run()
        state.prompted = None
        seen = []
        wf, *_ = resume(state, {"review": [recording(review_turn(1, APPROVE), seen)]}, alive=["review"], files=files)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen, [retry_prompt(state.review_path(1))])

    def test_workspace_gone_opens_a_new_one(self):
        state = saved_run("build", 1, agents=("spec", "build"))
        state.workspace_id = "w0"
        wf, herdr, host, _ = resume(state, {"build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        herdr.workspaces.clear()
        herdr.live_panes.clear()
        herdr.panes = 0

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(len([c for c in herdr.calls if c[0] == "workspace"]), 1)
        saved = json.loads(host.files[f"{state.dir}/state.json"])
        self.assertEqual((saved["workspace_id"], saved["root_pane"]), ("w1", "w1:p1"))
        self.assertIn(("split", "w1:p1", "right"), herdr.calls)
        self.assertEqual([c[1] for c in herdr.calls if c[0] == "start"], ["build-a1b2c3", "review-a1b2c3"])

    def test_pane_gone_splits_a_surviving_one(self):
        wf, herdr, *_ = resume(saved_run("review", 1, agents=("spec", "build", "review")),
                               {"review": [review_turn(1, APPROVE)]},
                               files={lambda s: s.build_path(1): "report"})
        herdr.live_panes -= {"w1:p2", "w1:p3"}

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn(("split", "w1:p1", "down"), herdr.calls)

    def test_recorded_reviewer_is_not_started_twice(self):
        # _start("review") saves before the review loop does, so round 1 can be saved as build with a reviewer.
        wf, herdr, *_ = resume(saved_run("build", 1, agents=("spec", "build", "review")),
                               {"review": [review_turn(1, APPROVE)]},
                               alive=["build", "review"], files={lambda s: s.build_path(1): "report"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse([c for c in herdr.calls if c[0] == "start"])
        self.assertEqual(herdr.calls.count(("prompt", "review-a1b2c3")), 1)

    def test_from_dict_ignores_unknown_keys_and_fills_new_ones(self):
        state = RunState.from_dict({"run_id": "r-abc", "task": "t", "cwd": "/p", "later_field": 1,
                                    "agents": {"spec": {"name": "spec-abc", "pane": "w9:p1"}}})
        self.assertEqual((state.root_pane, state.prompted, state.owner), ("w9:p1", None, None))
        self.assertEqual(state.max_rounds, orchestrator.DEFAULT_MAX_ROUNDS)

    def test_from_dict_incomplete(self):
        with self.assertRaisesRegex(OrchestratorError, "run state r-abc is incomplete"):
            RunState.from_dict({"run_id": "r-abc"})


class TestResumePullRequest(unittest.TestCase):
    BRANCH = "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3"
    ON_BRANCH = ("rev-parse", "--abbrev-ref", "HEAD")
    # The git commands that would change the tree, the branch or origin.
    MUTATING = ("add", "commit", "merge", "push", "switch")

    def test_run_saved_before_pull_requests_opens_none(self):
        saved = asdict(saved_run("review", 1, agents=("spec", "build", "review")))
        del saved["pull_request"]
        wf, herdr, host, _ = resume(RunState.from_dict(saved), {"review": [review_turn(1, APPROVE)]})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(host.prs, [])
        self.assertNotIn(("close", "w1"), herdr.calls)

    def test_resumed_review_ends_in_a_pull_request(self):
        state = saved_run("review", 1, agents=("spec", "build", "review"), pull_request=True,
                          base_branch="main", branch=self.BRANCH)
        host = FakeHost(branch=self.BRANCH)
        host.changed.add("/proj/limiter.py")
        wf, herdr, host, _ = resume(state, {"review": [review_turn(1, APPROVE)]}, host=host)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual([(p["base"], p["head"]) for p in host.prs], [("main", self.BRANCH)])
        self.assertEqual(host.branch, "main")

    def test_spec_resumed_after_switching_keeps_the_base_branch(self):
        # The earlier orchestrator stopped between `git switch -c` and saving the build phase.
        state = saved_run("spec", 0, pull_request=True, base_branch="main")
        host = FakeHost(branch=self.BRANCH)
        wf, herdr, host, _ = resume(state, {
            "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        }, host=host, files={lambda s: s.spec_path: "# Add a token-bucket rate limiter\n"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(("switch", "-c", self.BRANCH), host.git_calls)
        self.assertEqual(host.prs[0]["base"], "main")
        # Neither before the interview nor before branching: the only fetch is the one at publish.
        self.assertNotIn(TestUpToDateBase.FAST_FORWARD, host.git_calls)
        self.assertEqual(host.git_calls.count(TestUpToDateBase.FETCH), 1)

    def test_publish_resumed_after_the_commit_does_not_commit_again(self):
        state = saved_run("publish", 1, agents=("spec", "build", "review"), pull_request=True,
                          base_branch="main", branch=self.BRANCH, verdict=APPROVE)
        wf, herdr, host, _ = resume(state, {}, host=FakeHost(head="def456", branch=self.BRANCH))

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse([c for c in host.git_calls if c[0] == "commit"])
        self.assertIn(("push", "--quiet", "--set-upstream", "origin", self.BRANCH), host.git_calls)
        self.assertEqual(len(host.prs), 1)
        self.assertNotIn("workspace", [c[0] for c in herdr.calls])
        # Whether HEAD is the Builder's commit or the merge commit after it, origin's base is in it.
        self.assertNotIn(TestUpToDateBase.MERGE, host.git_calls)

    def test_publish_resumed_mid_merge_aborts_it_before_committing(self):
        state = saved_run("publish", 1, agents=("spec", "build", "review"), pull_request=True,
                          base_branch="main", branch=self.BRANCH, verdict=APPROVE)
        host = FakeHost(head="def456", branch=self.BRANCH)
        host.upstream, host.conflicts, host.merge_head = "u3", ["limiter.py"], True
        host.changed = {"/proj/limiter.py"}  # the conflict markers
        wf, herdr, host, _ = resume(state, {}, host=host)

        self.assertEqual(wf.run(), APPROVE)
        # The branch is checked at the start and again in _publish, before anything touches the tree.
        self.assertEqual(host.git_calls[:5], [self.ON_BRANCH, self.ON_BRANCH,
                                              ("rev-parse", "-q", "--verify", "MERGE_HEAD"), ("merge", "--abort"),
                                              ("status", "--porcelain")])
        self.assertFalse([c for c in host.git_calls if c[0] in ("add", "commit")])
        # The merge is tried again, conflicts again, and is aborted again.
        self.assertEqual(host.git_calls.count(("merge", "--abort")), 2)
        self.assertTrue(host.prs[0]["draft"])
        self.assertEqual(wf.state.conflicts, ["limiter.py"])

    def test_publish_resumed_after_the_pull_request_only_switches_back(self):
        state = saved_run("publish", 1, agents=("spec", "build", "review"), pull_request=True,
                          base_branch="main", branch=self.BRANCH, verdict=APPROVE,
                          pr_url="https://github.com/o/r/pull/7")
        wf, herdr, host, _ = resume(state, {}, host=FakeHost(branch=self.BRANCH))

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(host.prs, [])
        self.assertEqual(host.git_calls, [("switch", "--quiet", "main")])
        self.assertEqual(wf.state.phase, "done")

    def pr_run(self, phase, **kw):
        return saved_run(phase, 1, agents=("spec", "build", "review"), pull_request=True,
                         base_branch="main", branch=self.BRANCH, **kw)

    def assert_refused(self, wf, host, current):
        """The run fails naming both branches, state.json records it as resumable, and nothing in git_calls
        changed the tree, the branch or origin."""
        with self.assertRaises(OrchestratorError) as raised:
            wf.run()
        message = str(raised.exception)
        self.assertEqual(message, f"/proj is on {current}, not {self.BRANCH}, which holds this run's change; "
                                  f"check out {self.BRANCH} and resume")
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual((saved["error"], saved["owner"]), (message, None))
        self.assertFalse([c for c in host.git_calls if c[0] in self.MUTATING])
        self.assertEqual(host.prs, [])
        return saved

    def test_resume_on_another_branch_fails_before_any_turn(self):
        for current in ("main", "HEAD"):  # HEAD: detached
            for phase in ("build", "review", "quality"):
                with self.subTest(current=current, phase=phase):
                    host = FakeHost(branch=current)
                    host.changed.add("/proj/limiter.py")
                    fake = FakeCI()
                    if phase == "quality":
                        state = quality_state(pull_request=True, base_branch="main", branch=self.BRANCH)
                        wf, herdr, *_ = resume_gated(state, {"build": [fix_turn(1, 1)]}, fake, host=host)
                    else:
                        wf, herdr, *_ = resume(self.pr_run(phase), {
                            "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
                        }, host=host, ci=ci_for(fake))
                    saved = self.assert_refused(wf, host, current)
                    self.assertEqual(saved["phase"], phase)
                    self.assertEqual(herdr.calls, [])
                    self.assertEqual(fake.requests, [])
                    self.assertEqual(host.git_calls, [self.ON_BRANCH])

    def test_publish_resumed_on_another_branch_touches_nothing(self):
        host = FakeHost(head="def456", branch="main")
        host.changed, host.merge_head = {"/proj/limiter.py"}, True
        wf, herdr, *_ = resume(self.pr_run("publish", verdict=APPROVE), {}, host=host)

        self.assert_refused(wf, host, "main")
        self.assertEqual(host.git_calls, [self.ON_BRANCH])
        self.assertTrue(host.merge_head)

    def test_branch_switched_during_a_turn_is_refused_before_the_commit(self):
        def switching(turn):
            def wrapped(prompt, state, host):
                host.branch = "main"
                # From here on, git_calls holds only what ran after the switch.
                host.git_calls.clear()
                return turn(prompt, state, host)
            return wrapped

        for role in ("build", "review"):
            with self.subTest(role=role):
                script = {"spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}
                script[role] = [switching(script[role][0])]
                wf, herdr, host, _ = make_workflow(script)

                saved = self.assert_refused(wf, host, "main")
                self.assertEqual(saved["phase"], "publish")
                self.assertEqual(host.git_calls, [self.ON_BRANCH])

                host.branch = self.BRANCH
                wf2, *_ = resume(RunState.from_dict(saved), {}, host=host)
                self.assertEqual(wf2.run(), APPROVE)
                self.assertEqual([(p["base"], p["head"]) for p in host.prs], [("main", self.BRANCH)])
                self.assertEqual(len([c for c in host.git_calls if c[0] == "commit"]), 1)
                self.assertIsNone(wf2.state.error)

    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_cli_exits_1_on_another_branch(self, _resolve):
        state = self.pr_run("review")
        host = FakeHost(branch="main")
        herdr = FakeHerdr(host, state, {})
        with patch.object(Host, "run_states", return_value=[(10**4, asdict(state))]), \
                patch("orchestrator.connect", return_value=(herdr, host)), patch("sys.stderr") as err:
            self.assertEqual(main(["resume", "a1b2c3"]), orchestrator.EXIT_ERROR)
        self.assertIn(f"check out {self.BRANCH} and resume", "".join(c.args[0] for c in err.write.call_args_list))
        saved = json.loads(host.files[f"{state.dir}/state.json"])
        self.assertIn("check out", saved["error"])
        self.assertIsNone(saved["owner"])
        self.assertEqual(herdr.calls, [])

    def test_runs_without_a_branch_to_guard_are_not_checked(self):
        cases = {
            "no-pr": (saved_run("build", 1, agents=("spec", "build", "review")),
                      {"build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}),
            "pr_url set": (self.pr_run("publish", verdict=APPROVE, pr_url="https://github.com/o/r/pull/7"), {}),
        }
        for name, (state, script) in cases.items():
            with self.subTest(name):
                wf, herdr, host, _ = resume(state, script, host=FakeHost(branch="feature"))
                self.assertEqual(wf.run(), APPROVE)
                self.assertNotIn(self.ON_BRANCH, host.git_calls)

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_no_pr_is_saved_with_the_run(self, _resolve, _run, init):
        with patch("builtins.print"):
            main(["run", "task", "--no-pr"])
        self.assertFalse(init.call_args.args[2].pull_request)
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["resume", "c0ffee", "--no-pr"])

class TestHeartbeat(unittest.TestCase):
    def state_writes(self, host, state):
        return host.writes.count(f"{state.dir}/state.json")

    def test_heartbeat_during_a_long_interview(self):
        wf, herdr, host, _ = make_workflow({"spec": [idle]})
        path = f"{wf.state.dir}/state.json"
        beats = []

        def watch():
            beats.append(json.loads(host.files[path])["heartbeat_at"])
            if wf.clock.now > 10 * orchestrator.STALL_SECONDS:
                raise KeyboardInterrupt
        wf.clock.hooks.append(watch)

        with self.assertRaises(KeyboardInterrupt):
            wf.run()
        # About one write a minute over the 30-minute interview, not one per 3 s poll.
        interview_writes = self.state_writes(host, wf.state)
        minutes = 10 * orchestrator.STALL_SECONDS / HEARTBEAT_SECONDS
        self.assertLess(abs(interview_writes - minutes), 8)
        self.assertGreater(len(set(beats)), minutes - 2)

    def test_owner_is_recorded_and_released(self):
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        })
        seen = []
        herdr.script["build"] = [recording(build_turn(1), seen)]
        owners = []
        orig = herdr.prompt

        def prompt(name, text):
            owners.append(json.loads(host.files[f"{wf.state.dir}/state.json"])["owner"])
            orig(name, text)
        herdr.prompt = prompt

        wf.run()
        self.assertEqual(owners[0]["pid"], os.getpid())
        self.assertEqual(owners[0]["host"], socket.gethostname())
        self.assertIsNone(json.loads(host.files[f"{wf.state.dir}/state.json"])["owner"])

    def test_startup_dialog_waits_in_steps_with_heartbeats(self):
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        })
        herdr.blocked_at_start = {"spec"}
        waits = []

        def wait(name, timeout_ms, until=()):
            waits.append(timeout_ms)
            wf.clock.now += timeout_ms / 1000
            if len(waits) < 4:
                raise HerdrError("timeout", "timed out waiting for agent status")
            return "idle"
        herdr.wait = wait

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(waits, [HEARTBEAT_SECONDS * 1000] * 4)
        beats = [p for p in host.writes if p.endswith("state.json")]
        self.assertGreaterEqual(len(beats), 3 + 5)  # three during the dialog, on top of the phase saves

    def test_failed_heartbeat_is_not_fatal(self):
        # No pull request: the Builder here writes its report behind the flaky write and changes no files.
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "build": [idle], "review": [review_turn(1, APPROVE)],
        }, pull_request=False)
        failures = []
        orig = host.write

        def flaky(path, text):
            if path.endswith("state.json") and wf.clock.now > 100 and not failures:
                failures.append(path)
                raise OrchestratorError("ssh: connection reset")
            orig(path, text)
        host.write = flaky
        wf.clock.hooks.append(lambda: wf.clock.now > 200 and host.files.setdefault(wf.state.build_path(1), "r"))

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(len(failures), 1)

    def test_takeover_stops_without_saving(self):
        wf, herdr, host, _ = make_workflow({"spec": [spec_turn], "build": [idle]})
        path = f"{wf.state.dir}/state.json"
        other = {"host": "laptop", "pid": 4242, "started_at": "2026-09-30T07:00:00+00:00"}

        def take_over():
            if wf.clock.now > 30 and json.loads(host.files[path])["owner"] != other:
                host.files[path] = json.dumps({**json.loads(host.files[path]), "owner": other})
        wf.clock.hooks.append(take_over)

        with self.assertRaisesRegex(RunTakenOver, "taken over by pid 4242 on laptop"):
            wf.run()
        saved = json.loads(host.files[path])
        self.assertEqual(saved["owner"], other)
        self.assertIsNone(saved["error"])

    def test_interrupt_is_recorded(self):
        def interrupted(prompt, state, host):
            raise KeyboardInterrupt
        wf, herdr, host, _ = make_workflow({"spec": [spec_turn], "build": [interrupted]})

        with self.assertRaises(KeyboardInterrupt):
            wf.run()
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual((saved["error"], saved["owner"]), ("interrupted", None))

    def test_failure_releases_the_run(self):
        wf, herdr, host, _ = make_workflow({"spec": [spec_turn], "build": [idle]}, turn_timeout=60)
        with self.assertRaises(OrchestratorError):
            wf.run()
        self.assertIsNone(json.loads(host.files[f"{wf.state.dir}/state.json"])["owner"])


def run_record(phase="build", *, owner=None, error=None, verdict=None):
    return {"run_id": "20260930-070000-c0ffee", "task": "add a rate limiter", "phase": phase, "round": 1,
            "owner": owner, "error": error, "verdict": verdict}


ME = {"host": "here", "pid": 4242, "started_at": "2026-09-30T07:00:00+00:00"}


class TestRunHealth(unittest.TestCase):
    def health(self, record, age, alive=True):
        return orchestrator.run_health(record, age, "here", lambda pid: alive)

    def test_fresh_heartbeat_is_running(self):
        self.assertEqual(self.health(run_record(owner=ME), 40), ("running", "pid 4242 on here, beat 40s ago"))

    def test_old_heartbeat_is_stale(self):
        self.assertEqual(self.health(run_record(owner=ME), STALE_SECONDS + 420),
                         ("stale", "no heartbeat for 12m"))

    def test_dead_local_pid_is_stale_once_a_beat_is_late(self):
        record = run_record(owner=ME)
        self.assertEqual(self.health(record, HEARTBEAT_SECONDS + 1, alive=False)[0], "stale")
        self.assertEqual(self.health(record, 10, alive=False)[0], "running")

    def test_pid_on_another_host_is_not_checked(self):
        record = run_record(owner={**ME, "host": "laptop"})
        self.assertEqual(self.health(record, 120, alive=False)[0], "running")

    def test_run_from_before_owners_existed(self):
        self.assertEqual(self.health(run_record(phase="spec"), 7 * 3600 + 720),
                         ("stale", "no heartbeat for 7h12m"))

    def test_finished_and_failed_runs_are_never_stale(self):
        self.assertIsNone(self.health(run_record(phase="done", verdict=APPROVE), 10**6))
        self.assertIsNone(self.health(run_record(error="interrupted"), 10**6))

    def test_print_runs(self):
        runs = [
            (STALE_SECONDS + 1, run_record(phase="spec")),
            (40, run_record(owner=ME)),
            (10**6, run_record(phase="done", verdict=APPROVE)),
            (10**6, run_record(error="interrupted")),
        ]
        with patch("builtins.print") as out:
            orchestrator.print_runs(runs, "here", lambda pid: True, ["--machine", "remote", "--cwd", "~/proj"])
        lines = [c.args[0] for c in out.call_args_list][::2]
        self.assertEqual(lines, [
            "20260930-070000-c0ffee  spec     round 1  stale: no heartbeat for 5m; "
            "resume: orchestrator.py resume c0ffee --machine remote --cwd '~/proj'",
            "20260930-070000-c0ffee  build    round 1  running: pid 4242 on here, beat 40s ago",
            f"20260930-070000-c0ffee  done     round 1  {APPROVE}",
            "20260930-070000-c0ffee  build    round 1  error: interrupted",
        ])

    def test_print_closed_runs(self):
        closed = "workspace closed; worktree deleted"
        runs = [
            (STALE_SECONDS + 1, {**run_record(phase="build"), "closed": closed}),
            (10**6, {**run_record(phase="done", verdict=APPROVE), "closed": "workspace closed"}),
            (10**6, {**run_record(phase="done", verdict=None), "closed": "workspace already gone"}),
            (10**6, {**run_record(error="interrupted"), "closed": closed}),
        ]
        with patch("builtins.print") as out:
            orchestrator.print_runs(runs, "here", lambda pid: True, [])
        lines = [c.args[0] for c in out.call_args_list][::2]
        self.assertEqual(lines, [
            f"20260930-070000-c0ffee  build    round 1  closed: {closed}",
            f"20260930-070000-c0ffee  done     round 1  {APPROVE}; closed: workspace closed",
            "20260930-070000-c0ffee  done     round 1  closed: workspace already gone",
            f"20260930-070000-c0ffee  build    round 1  error: interrupted; closed: {closed}",
        ])


RUN_RECORD_KEYS = {"id", "key", "task", "workflow", "steps", "phase", "round", "quality_round", "role", "pane",
                   "health", "health_reason", "heartbeat_age", "waiting", "verdict", "error", "closed", "branch",
                   "worktree", "pr", "turns", "resume_command"}
PR_URL = "https://github.com/o/r/pull/7"


def open_turn(step, role):
    return {"step": step, "role": role, "round": 1, "quality_round": 0, "file": f"{step}.md", "agent": "claude",
            "model": None, "human_paced": True, "started_at": "2026-09-30T07:00:00Z", "ended_at": None,
            "seconds": None}


class TestRunRecord(unittest.TestCase):
    """The record `list --json` prints, and the plain list and show lines formatted from it."""

    def record(self, state, age=40, alive=True, target=()):
        return orchestrator.run_record(state, age, "here", lambda pid: alive, list(target))

    def test_every_key_is_present(self):
        record = self.record({**run_record(owner=ME), "branch": "orchestrator/x", "pr_url": PR_URL})
        self.assertEqual(set(record), RUN_RECORD_KEYS)
        self.assertEqual(record["id"], "20260930-070000-c0ffee")
        self.assertEqual(record["key"], "c0ffee")
        self.assertEqual(record["workflow"], orchestrator.DEFAULT_WORKFLOW.name)
        self.assertEqual(record["steps"], [{"id": "spec", "role": "spec"}, {"id": "build", "role": "build"},
                                           {"id": "review", "role": "review"}])
        self.assertEqual((record["phase"], record["round"], record["role"]), ("build", 1, "build"))
        self.assertEqual((record["health"], record["health_reason"], record["heartbeat_age"]),
                         ("running", "pid 4242 on here, beat 40s ago", 40))
        self.assertEqual(record["pr"], {"url": PR_URL, "state": None, "error": None})
        self.assertEqual(record["branch"], "orchestrator/x")
        self.assertEqual(record["turns"], [])
        self.assertIsNone(record["waiting"])
        self.assertIsNone(record["resume_command"])
        json.dumps(record)

    def test_the_task_is_whole(self):
        task = "x" * 300
        self.assertEqual(self.record({**run_record(), "task": task})["task"], task)

    def test_a_state_from_before_the_newer_fields(self):
        record = self.record({"run_id": "20260930-070000-c0ffee", "task": "t", "phase": "review", "round": 2},
                             age=STALE_SECONDS + 1)
        self.assertEqual(set(record), RUN_RECORD_KEYS)
        self.assertEqual((record["turns"], record["quality_round"], record["closed"], record["worktree"]),
                         ([], None, None, None))
        self.assertEqual(record["waiting"], {"reason": "stale", "detail": "orchestrator.py resume c0ffee"})

    def test_a_workflow_this_orchestrator_cannot_load(self):
        record = self.record({**run_record(), "workflow": "gone", "quality_round": 2})
        self.assertEqual(set(record), RUN_RECORD_KEYS)
        self.assertEqual((record["workflow"], record["steps"], record["role"], record["pane"]),
                         ("gone", None, None, None))
        self.assertIsNone(record["quality_round"])
        self.assertEqual(self.record({**run_record(orchestrator.QUALITY), "workflow": "gone",
                                      "quality_round": 2})["quality_round"], 2)

    def test_no_role_outside_the_steps(self):
        for phase in (orchestrator.QUALITY, orchestrator.PUBLISH, orchestrator.DONE):
            with self.subTest(phase=phase):
                self.assertIsNone(self.record(run_record(phase))["role"])

    def test_quality_round_only_in_the_quality_loop_or_gated_step(self):
        for phase, expected in (("build", 2), (orchestrator.QUALITY, 2), ("review", None)):
            with self.subTest(phase=phase):
                self.assertEqual(self.record({**run_record(phase), "quality_round": 2})["quality_round"], expected)

    def test_blocked_waits_for_the_answer(self):
        state = {**run_record(owner=ME), "blocked": {"file": "build-1.md", "question": "which db?", "k": 1},
                 "agents": {"build": {"name": "build-c0ffee", "pane": "w1:p2"}},
                 "turns": [open_turn("spec", "spec")]}
        record = self.record(state)
        self.assertEqual(record["waiting"], {"reason": "blocked", "detail": "which db?"})
        self.assertEqual(record["pane"], "w1:p2")

    def test_an_interview_waits_while_the_human_paced_turn_is_open(self):
        state = {**run_record("spec", owner=ME), "turns": [open_turn("spec", "spec")]}
        self.assertEqual(self.record(state)["waiting"], {"reason": "interview", "detail": "spec"})
        ended = {**open_turn("spec", "spec"), "ended_at": "2026-09-30T07:05:00Z"}
        self.assertIsNone(self.record({**state, "turns": [ended]})["waiting"])
        self.assertIsNone(self.record({**run_record("spec", owner=ME)})["waiting"])
        # A Builder turn is not human-paced, open or not.
        self.assertIsNone(self.record({**run_record(owner=ME), "turns": [open_turn("build", "build")]})["waiting"])

    def test_a_stale_run_waits_to_be_resumed_unless_closed(self):
        stale = run_record(owner=ME)
        record = self.record(stale, age=STALE_SECONDS + 1, target=["--machine", "m", "--cwd", "~/p"])
        command = "orchestrator.py resume c0ffee --machine m --cwd '~/p'"
        self.assertEqual(record["waiting"], {"reason": "stale", "detail": command})
        self.assertEqual(record["resume_command"], command)
        closed = self.record({**stale, "closed": "workspace closed"}, age=STALE_SECONDS + 1)
        self.assertEqual(closed["health"], "stale")
        self.assertIsNone(closed["waiting"])
        self.assertIsNone(closed["resume_command"])

    def test_a_blocked_stale_run_waits_to_be_resumed(self):
        state = {**run_record(owner=ME), "blocked": {"file": "build-1.md", "question": "q", "k": 1}}
        self.assertEqual(self.record(state, age=STALE_SECONDS + 1)["waiting"]["reason"], "stale")

    def test_finished_and_failed_runs_wait_for_nothing(self):
        for state in (run_record("done", verdict=APPROVE), run_record(error="interrupted")):
            with self.subTest(state=state):
                record = self.record(state, age=10**6)
                self.assertEqual((record["health"], record["waiting"], record["resume_command"]), (None, None, None))

    def test_plain_lines_in_each_state(self):
        blocked = {**run_record(owner=ME), "blocked": {"file": "build-1.md", "question": "which db?", "k": 1},
                   "agents": {"build": {"name": "build-c0ffee", "pane": "w1:p2"}}}
        runs = [
            (40, run_record(owner=ME)),
            (STALE_SECONDS + 1, run_record(owner=ME)),
            (40, blocked),
            (STALE_SECONDS + 1, {**run_record(), "closed": "workspace closed"}),
            (10**6, run_record(error="interrupted")),
            (10**6, {**run_record("done", verdict=APPROVE), "pr_url": PR_URL}),
            (10**6, {**run_record("done", verdict=APPROVE), "workflow": "gone"}),
            (40, {**run_record(orchestrator.QUALITY, owner=ME), "quality_round": 2}),
        ]
        rows = orchestrator.run_rows(runs, "here", lambda pid: True, [])
        self.assertEqual([orchestrator.run_line(*row[:4]) for row in rows], [
            "20260930-070000-c0ffee  build    round 1  running: pid 4242 on here, beat 40s ago",
            "20260930-070000-c0ffee  build    round 1  stale: no heartbeat for 5m; resume: orchestrator.py resume c0ffee",
            "20260930-070000-c0ffee  build    round 1  blocked: which db? (pane w1:p2)",
            "20260930-070000-c0ffee  build    round 1  closed: workspace closed",
            "20260930-070000-c0ffee  build    round 1  error: interrupted",
            f"20260930-070000-c0ffee  done     round 1  {APPROVE}  {PR_URL}",
            f"20260930-070000-c0ffee  done     round 1  {APPROVE}  [gone workflow]",
            "20260930-070000-c0ffee  quality  round 1 q2  running: pid 4242 on here, beat 40s ago",
        ])

    def test_show_starts_with_the_list_line(self):
        host = FakeHost()
        state = {**run_record("done", verdict=APPROVE), "cwd": "/proj", "pr_url": PR_URL}
        host.files[f"/proj/{orchestrator.RUNS_DIR}/{state['run_id']}/state.json"] = json.dumps(state)
        with patch.object(host, "run_files", return_value=[]), patch("builtins.print") as out:
            orchestrator.show_run(host, "/proj", "c0ffee", "here", lambda pid: True, [])
        self.assertEqual(out.call_args_list[0].args[0],
                         f"20260930-070000-c0ffee  done     round 1  {APPROVE}  {PR_URL}")

    def test_pr_states_are_filled_per_run(self):
        host = FakeHost()
        unusable = f"{PR_URL}/unusable"
        broken = f"{PR_URL}/broken"
        host.pull_requests = {
            PR_URL: {"state": "MERGED", "headRefOid": "a" * 40, "closedAt": "2026-09-30T08:00:00Z"},
            unusable: completed(stderr="gh: not logged in", returncode=4),
            broken: completed(stdout="nonsense"),
        }
        records = [self.record({**run_record("done"), "pr_url": url}) for url in (PR_URL, unusable, broken)]
        records.append(self.record(run_record("done")))
        orchestrator.fill_pr_states(host, "/proj", records)
        self.assertEqual(records[0]["pr"], {"url": PR_URL, "state": "MERGED", "error": None})
        self.assertIsNone(records[1]["pr"]["state"])
        self.assertIn("not logged in", records[1]["pr"]["error"])
        self.assertIsNone(records[2]["pr"]["state"])
        self.assertIn("nonsense", records[2]["pr"]["error"])
        self.assertIsNone(records[3]["pr"])
        self.assertEqual(len(host.gh_calls), 3)


@patch.object(Host, "resolve_dir", return_value="/proj")
@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestListJSON(unittest.TestCase):
    def list_json(self, *flags, runs=()):
        with patch.object(Host, "run_states", return_value=list(runs)), \
                patch.object(Herdr, "_exec", side_effect=AssertionError("herdr was called")), \
                patch("builtins.print") as out:
            self.assertEqual(main(["list", "--json", *flags]), 0)
        self.assertEqual(out.call_count, 1)
        return json.loads(out.call_args.args[0])

    def test_no_runs(self, _resolve):
        self.assertEqual(self.list_json(), {"version": 1, "runs": []})

    def test_one_record_per_run_in_order(self, _resolve):
        runs = [(40, run_record(owner=ME)), (10**6, {**run_record("done", verdict=APPROVE), "run_id": "x-beef00"})]
        doc = self.list_json(runs=runs)
        self.assertEqual([r["key"] for r in doc["runs"]], ["c0ffee", "beef00"])
        self.assertEqual(doc["runs"][0], orchestrator.run_record(runs[0][1], 40, socket.gethostname(),
                                                                 orchestrator.pid_alive, []))

    def test_no_gh_without_pr_state(self, _resolve):
        with patch.object(Host, "pull_request", side_effect=AssertionError("gh was called")):
            doc = self.list_json(runs=[(10**6, {**run_record("done"), "pr_url": PR_URL})])
        self.assertEqual(doc["runs"][0]["pr"], {"url": PR_URL, "state": None, "error": None})

    def test_pr_state(self, _resolve):
        with patch.object(Host, "pull_request", return_value={"state": "OPEN"}) as pr:
            doc = self.list_json("--pr-state", runs=[(10**6, {**run_record("done"), "pr_url": PR_URL}),
                                                     (10**6, run_record("done"))])
        pr.assert_called_once_with("/proj", PR_URL)
        self.assertEqual(doc["runs"][0]["pr"]["state"], "OPEN")

    def test_unusable_gh_still_lists(self, _resolve):
        with patch.object(Host, "pull_request", side_effect=orchestrator.GhUnusable("gh is not installed")):
            doc = self.list_json("--pr-state", runs=[(10**6, {**run_record("done"), "pr_url": PR_URL})])
        self.assertEqual(doc["runs"][0]["pr"], {"url": PR_URL, "state": None, "error": "gh is not installed"})

    def test_pr_state_needs_json(self, _resolve):
        with self.assertRaises(SystemExit) as raised, patch("sys.stderr"):
            parse_args(["list", "--pr-state"])
        self.assertEqual(raised.exception.code, 2)

    @patch.object(Herdr, "ssh_target", return_value="remote-host")
    def test_on_a_machine(self, _target, _resolve):
        runs = [(STALE_SECONDS + 1, run_record(owner=ME))]
        with patch.object(Host, "run_states", return_value=runs) as states, \
                patch.object(Herdr, "_exec", side_effect=AssertionError("herdr was called")), \
                patch("builtins.print") as out:
            self.assertEqual(main(["list", "--json", "--machine", "remote", "--cwd", "~/proj"]), 0)
        self.assertEqual(states.call_args.args, ("/proj",))
        record = json.loads(out.call_args.args[0])["runs"][0]
        self.assertEqual(record["resume_command"], "orchestrator.py resume c0ffee --machine remote --cwd '~/proj'")


class TestPidAlive(unittest.TestCase):
    def test_own_process_is_alive(self):
        self.assertTrue(orchestrator.pid_alive(os.getpid()))

    def test_exited_process_is_not(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(orchestrator.pid_alive(3354482))

    def test_another_users_process_is_alive(self):
        with patch("os.kill", side_effect=PermissionError):
            self.assertTrue(orchestrator.pid_alive(1))


class TestFindRun(unittest.TestCase):
    runs = [(1, {"run_id": "20260930-070000-c0ffee"}), (2, {"run_id": "20260930-080000-beef00"}),
            (3, {"run_id": "20260929-080000-beef00"})]

    def test_by_key_or_full_id(self):
        self.assertEqual(orchestrator.find_run(self.runs, "c0ffee", "/p")[0], 1)
        self.assertEqual(orchestrator.find_run(self.runs, "20260929-080000-beef00", "/p")[0], 3)

    def test_ambiguous_key(self):
        with self.assertRaisesRegex(OrchestratorError, "beef00 matches several runs"):
            orchestrator.find_run(self.runs, "beef00", "/p")

    def test_no_match(self):
        with self.assertRaisesRegex(OrchestratorError, "no run abcdef under /p/.orchestrator/runs"):
            orchestrator.find_run(self.runs, "abcdef", "/p")


class TestResumableState(unittest.TestCase):
    def saved(self, **kw):
        return {**asdict(saved_run("build", 2, agents=("spec", "build"))),
                "max_rounds": 3, "turn_timeout": 600, "permission_mode": "auto",
                "models": {"build": "sonnet"}, **kw}

    def resumable(self, argv, saved, age=10**4, alive=True):
        args = parse_args(["resume", *argv])
        return orchestrator.resumable_state([(age, saved)], args, "/proj", "here", lambda pid: alive)

    def test_saved_settings_are_kept(self):
        state = self.resumable(["a1b2c3"], self.saved())
        self.assertEqual((state.max_rounds, state.turn_timeout, state.permission_mode, state.models),
                         (3, 600, "auto", {"build": "sonnet"}))

    def test_flags_override_saved_settings(self):
        state = self.resumable(["a1b2c3", "--max-rounds", "5", "--timeout", "60",
                                "--permission-mode", "acceptEdits", "--review-model", "opus"], self.saved())
        self.assertEqual((state.max_rounds, state.turn_timeout, state.permission_mode, state.models),
                         (5, 60, "acceptEdits", {"build": "sonnet", "review": "opus"}))

    def test_max_rounds_below_the_saved_round(self):
        saved = self.saved()
        with self.assertRaisesRegex(OrchestratorError, "already in round 2"):
            self.resumable(["a1b2c3", "--max-rounds", "1"], saved)

    def test_live_run_needs_force(self):
        saved = self.saved(owner=ME)
        with self.assertRaisesRegex(OrchestratorError, "looks alive .*pass --force"):
            self.resumable(["a1b2c3"], saved, age=30)
        self.assertEqual(self.resumable(["a1b2c3", "--force"], saved, age=30).run_id, saved["run_id"])

    def test_machine_of_the_resume_is_saved(self):
        state = self.resumable(["a1b2c3", "--machine", "remote", "--cwd", "~/p"], self.saved(machine=None))
        self.assertEqual(state.machine, "remote")


OTHER_ID = "20261009-220151-537935"
OTHER_DIR = f"/proj/.orchestrator/runs/{OTHER_ID}"


class TestOneRunPerCheckout(unittest.TestCase):
    """A run refuses to go on while another run in its checkout is live, since both would edit one tree."""

    def full_run(self):
        """A run's script, new each time since each turn is taken off it."""
        return {"spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}

    def host_with_other_run(self, age=16, **kw):
        """A FakeHost whose /proj holds run OTHER_ID, which this host's orchestrator pid 3354482 last saved age
        seconds ago."""
        host = FakeHost()
        owner = {"host": socket.gethostname(), "pid": 3354482, "started_at": "2026-10-09T22:01:51+00:00"}
        saved = {**asdict(RunState(OTHER_ID, "another task", "/proj", None, phase="build", round=1)),
                 "owner": owner, **kw}
        host.files[f"{OTHER_DIR}/state.json"] = json.dumps(saved)
        host.ages[OTHER_ID] = age
        return host

    def assert_refused(self, wf, host, beat="16s"):
        """The run fails naming the other run, records that in its own state.json, and starts no agent."""
        other = host.files[f"{OTHER_DIR}/state.json"]
        with self.assertRaises(OrchestratorError) as raised:
            wf.run()
        message = str(raised.exception)
        self.assertEqual(message, f"run {OTHER_ID} is running in /proj "
                                  f"(pid 3354482 on {socket.gethostname()}, beat {beat} ago); "
                                  f"one run at a time per checkout: wait for it, stop it, or use another clone")
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual((saved["error"], saved["owner"]), (message, None))
        self.assertEqual(wf.herdr.calls, [])
        self.assertEqual(host.files[f"{OTHER_DIR}/state.json"], other)

    def test_new_run_is_refused_while_another_runs(self):
        for pull_request in (True, False):
            with self.subTest(pull_request=pull_request):
                host = self.host_with_other_run()
                wf, *_ = make_workflow(self.full_run(), host=host, pull_request=pull_request)
                self.assert_refused(wf, host)

    def test_resume_is_refused_while_another_runs(self):
        for pull_request in (True, False):
            with self.subTest(pull_request=pull_request):
                host = self.host_with_other_run()
                state = saved_run("review", 2, agents=("spec", "build", "review"), pull_request=pull_request,
                                  base_branch="main", branch="orchestrator/x-a1b2c3")
                wf, *_ = resume(state, {"review": [review_turn(2, APPROVE)]}, host=host)
                self.assert_refused(wf, host)

    def test_runs_nothing_drives_do_not_block(self):
        alive, dead = (lambda pid: True), (lambda pid: False)
        cases = {
            "stale": (STALE_SECONDS + 1, {}, alive),
            "dead pid on this host": (HEARTBEAT_SECONDS + 1, {}, dead),
            "done": (0, {"phase": "done", "verdict": APPROVE, "owner": None}, alive),
            "failed": (0, {"error": "interrupted", "owner": None}, alive),
        }
        for name, (age, saved, pid_alive) in cases.items():
            with self.subTest(name):
                host = self.host_with_other_run(age, **saved)
                wf, *_ = make_workflow(self.full_run(), host=host)
                wf.pid_alive = pid_alive
                self.assertEqual(wf.run(), APPROVE)
                self.assertIsNone(wf.state.error)

    def test_live_pid_with_a_late_beat_blocks(self):
        host = self.host_with_other_run(HEARTBEAT_SECONDS + 1)
        wf, *_ = make_workflow(self.full_run(), host=host)
        wf.pid_alive = lambda pid: True
        self.assert_refused(wf, host, beat="1m")

    def test_two_runs_that_claim_together_both_refuse(self):
        host = FakeHost()
        first, *_ = make_workflow(self.full_run(), host=host,
                                  state=RunState("20261009-220151-aaaaaa", "first", "/proj", None))
        second, *_ = make_workflow(self.full_run(), host=host,
                                   state=RunState("20261009-220151-bbbbbb", "second", "/proj", None))
        list_runs = host.run_states
        reads = []

        def second_claims_and_checks_as_first_reads(cwd):
            reads.append(cwd)
            if len(reads) > 1:
                return list_runs(cwd)
            second._claim()
            runs = list_runs(cwd)
            with self.assertRaisesRegex(OrchestratorError, "run 20261009-220151-aaaaaa is running in /proj"):
                second.run()
            return runs
        host.run_states = second_claims_and_checks_as_first_reads

        with self.assertRaisesRegex(OrchestratorError, "run 20261009-220151-bbbbbb is running in /proj"):
            first.run()
        for wf in (first, second):
            saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
            self.assertIn("one run at a time per checkout", saved["error"])
            self.assertIsNone(saved["owner"])

    def force_resume(self, host):
        """resume --force of run a1b2c3, which looks alive: saved 30 s ago by a live pid on this host."""
        saved = saved_run("review", 1, agents=("spec", "build", "review"),
                          owner={"host": socket.gethostname(), "pid": 4242, "started_at": "x"})
        host.files[f"{saved.dir}/state.json"] = json.dumps(asdict(saved))
        host.ages[saved.run_id] = 30
        args = parse_args(["resume", "a1b2c3", "--force"])
        state = orchestrator.resumable_state(host.run_states("/proj"), args, "/proj", socket.gethostname(),
                                             lambda pid: True)
        wf, *_ = resume(state, {"review": [review_turn(1, APPROVE)]}, host=host)
        wf.pid_alive = lambda pid: True
        return wf

    def test_force_takes_over_the_run_itself(self):
        wf = self.force_resume(FakeHost())
        self.assertEqual(wf.run(), APPROVE)

    def test_force_does_not_override_another_live_run(self):
        host = self.host_with_other_run()
        self.assert_refused(self.force_resume(host), host)


    def test_worktree_runs_do_not_refuse_each_other(self):
        host = self.host_with_other_run(worktree=f"/proj/{orchestrator.WORKTREES_DIR}/537935")
        wf, *_ = worktree_run(host=host)
        wf.pid_alive = lambda pid: True
        self.assertEqual(wf.run(), APPROVE)

    def test_a_worktree_run_and_one_in_the_checkout_do_not_refuse_each_other(self):
        cases = {"in-place run, live worktree run": (self.host_with_other_run(worktree="/elsewhere"), make_workflow),
                 "worktree run, live in-place run": (self.host_with_other_run(), worktree_run)}
        for name, (host, workflow) in cases.items():
            with self.subTest(name):
                wf, *_ = workflow(self.full_run(), host=host)
                wf.pid_alive = lambda pid: True
                self.assertEqual(wf.run(), APPROVE)


WORKTREE = f"/proj/{orchestrator.WORKTREES_DIR}/a1b2c3"
RUN_BRANCH = "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3"


def worktree_build_turn(n):
    """build_turn, in the run's worktree."""
    def turn(prompt, state, host):
        host.write(f"{state.work_dir}/limiter.py", f"version {n}")
        return writes(lambda s: s.build_path(n), f"report {n}")(prompt, state, host)
    return turn


def worktree_run(script=None, host=None, **kw):
    """make_workflow for a new run --worktree run, by default one the Reviewer approves in round 1.

    Its Builder writes in the worktree, whatever the script says.
    """
    state = RunState("20260929-120000-a1b2c3", "add a rate limiter", "/proj", None, worktree=WORKTREE)
    script = script or {"spec": [spec_turn], "review": [review_turn(1, APPROVE)]}
    script["build"] = [worktree_build_turn(1)]
    return make_workflow(script, host=host, state=state, **kw)


def saved_worktree_run(phase, rnd, **kw):
    """A worktree run that an earlier orchestrator took to phase in round rnd, with its branch."""
    return saved_run(phase, rnd, agents=("spec", "build", "review"), worktree=WORKTREE, pull_request=True,
                     base_branch="main", branch=RUN_BRANCH, **kw)


class TestWorktreeRun(unittest.TestCase):
    """run --worktree: the run works in a git worktree of its own and leaves the project's checkout alone."""

    def test_a_run_flag(self):
        with self.assertRaises(SystemExit), patch("sys.stdout", new_callable=io.StringIO) as out:
            parse_args(["run", "--help"])
        self.assertIn("--worktree", out.getvalue())
        with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO) as err:
            parse_args(["resume", "a1b2c3", "--worktree"])
        self.assertIn("unrecognized arguments: --worktree", err.getvalue())

        state, _ = orchestrator.new_state(parse_args(["run", "task", "--worktree"]), "/proj")
        self.assertEqual(state.worktree, f"/proj/.orchestrator/worktrees/{state.key}")
        self.assertIsNone(orchestrator.new_state(parse_args(["run", "task"]), "/proj")[0].worktree)

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_flag_reaches_the_workflow(self, _resolve, _run, init):
        with patch("builtins.print"), patch.object(orchestrator, "sweep_branches"):
            self.assertEqual(main(["run", "task", "--worktree"]), 0)
        state = init.call_args.args[2]
        self.assertEqual(state.worktree, f"/proj/.orchestrator/worktrees/{state.key}")

    def test_dirty_checkout_on_another_branch_is_left_alone(self):
        host = FakeHost(branch="feature")
        host.changed.add("/proj/wip.py")
        host.upstream = "u1"  # so the base is merged into the run's branch before the push
        wf, _, host, _ = worktree_run(host=host)

        self.assertEqual(wf.run(), APPROVE)

        in_checkout = [args for cwd, args in host.git_log if cwd == "/proj"]
        self.assertFalse([a for a in in_checkout if a[0] in ("switch", "status", "add", "commit", "push")
                          or a[:2] == ("merge", "--ff-only")], in_checkout)
        in_worktree = [args for cwd, args in host.git_log if cwd == WORKTREE]
        self.assertIn(("switch", "-c", RUN_BRANCH, "origin/feature"), in_worktree)
        self.assertIn(("merge", "--no-edit", "--quiet", "origin/feature"), in_worktree)
        self.assertIn(("push", "--quiet", "--set-upstream", "origin", RUN_BRANCH), in_worktree)
        self.assertLessEqual({"status", "add", "commit"}, {a[0] for a in in_worktree})
        self.assertEqual((host.branch, host.changed), ("feature", {"/proj/wip.py"}))
        self.assertEqual((host.prs[0]["base"], host.prs[0]["head"]), ("feature", RUN_BRANCH))

    def test_worktree_is_added_from_origin_before_the_workspace_opens(self):
        wf, herdr, host, _ = worktree_run()
        at_workspace = []
        create = herdr.create_workspace

        def create_workspace(cwd, label):
            saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
            at_workspace.append((saved["worktree"], WORKTREE in host.worktrees))
            return create(cwd, label)
        herdr.create_workspace = create_workspace

        self.assertEqual(wf.run(), APPROVE)
        steps = [(cwd, args) for cwd, args in host.git_log if args[0] in ("fetch", "worktree")]
        self.assertEqual(steps[:2], [("/proj", ("fetch", "--quiet", "origin", "main")),
                                     ("/proj", ("worktree", "add", "--detach", WORKTREE, "origin/main"))])
        self.assertEqual(at_workspace, [(WORKTREE, True)])
        self.assertEqual(wf.state.base_branch, "main")

    def test_agents_work_in_the_worktree(self):
        seen = []
        wf, herdr, _, _ = worktree_run({"spec": [recording(spec_turn, seen)],
                                        "review": [recording(review_turn(1, APPROVE), seen)]})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(herdr.cwds, [("workspace", WORKTREE), ("split", WORKTREE), ("split", WORKTREE)])
        self.assertIn(f"Read the code in {WORKTREE} first", seen[0])
        self.assertIn(f"`git diff abc123` in {WORKTREE},", seen[1])

    def test_needs_a_repository_a_branch_and_an_origin(self):
        no_origin = FakeHost()
        no_origin.has_origin = False
        cases = [(FakeHost(head=None), "/proj is not a git repository with a commit, which --worktree needs"),
                 (FakeHost(branch="HEAD"), "/proj is on a detached HEAD"),
                 (no_origin, "/proj has no origin")]
        for host, message in cases:
            for pull_request in (True, False):
                with self.subTest(message, pull_request=pull_request):
                    wf, herdr, *_ = worktree_run(host=host, pull_request=pull_request)
                    with self.assertRaisesRegex(OrchestratorError, message):
                        wf.run()
                    self.assertEqual((herdr.calls, host.worktrees), ([], {}))

    def test_publishing_removes_the_worktree(self):
        wf, herdr, host, _ = worktree_run()
        removed = []
        remove = host.remove_worktree

        def remove_worktree(cwd, path):
            removed.append((cwd, path, json.loads(host.files[f"{wf.state.dir}/state.json"])["pr_url"]))
            remove(cwd, path)
        host.remove_worktree = remove_worktree

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(removed, [("/proj", WORKTREE, "https://github.com/o/r/pull/7")])
        self.assertIn(("/proj", ("worktree", "remove", WORKTREE)), host.git_log)
        self.assertEqual(host.worktrees, {})
        self.assertFalse([a for a in host.git_calls if a[:2] == ("switch", "--quiet")])
        self.assertEqual(herdr.calls[-1], ("close", "w1"))

    def test_a_failed_removal_is_only_logged(self):
        host = FakeHost()
        host.remove_error = f"fatal: '{WORKTREE}' contains modified or untracked files, use --force to delete it"
        wf, *_ = worktree_run(host=host)

        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.phase, "done")
        self.assertIn(f"could not remove worktree {WORKTREE}: ", err.getvalue())
        self.assertIn("use --force to delete it", err.getvalue())
        self.assertIn(WORKTREE, host.worktrees)

    def test_no_pr_keeps_the_worktree_with_the_change(self):
        wf, herdr, host, _ = worktree_run(pull_request=False)

        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(wf.run(), APPROVE)
        self.assertIn(f"the change is in worktree {WORKTREE}", err.getvalue())
        self.assertEqual(host.worktrees, {WORKTREE: "HEAD"})
        self.assertEqual(host.changed, {f"{WORKTREE}/limiter.py"})
        self.assertFalse([a for a in host.git_calls if a[0] in ("switch", "commit", "push")])
        self.assertNotIn(("close", "w1"), herdr.calls)

    def test_a_failed_run_keeps_the_worktree(self):
        wf, herdr, host, _ = worktree_run()
        herdr.script["build"] = [writes(lambda s: s.build_path(1), "nothing to do")]

        with self.assertRaisesRegex(OrchestratorError, "changed no files"):
            wf.run()
        self.assertEqual(host.worktrees, {WORKTREE: RUN_BRANCH})

    def test_resume_reuses_the_worktree(self):
        host = FakeHost()
        host.worktrees[WORKTREE] = RUN_BRANCH
        host.changed.add(f"{WORKTREE}/limiter.py")
        wf, _, host, _ = resume(saved_worktree_run("review", 1), {"review": [review_turn(1, APPROVE)]},
                                host=host, files={lambda s: s.build_path(1): "report 1"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse([a for a in host.git_calls if a[:2] == ("worktree", "add")])
        self.assertIn((WORKTREE, ("commit", "--quiet", "-m", "add a rate limiter", "-m",
                                  orchestrator.commit_note(wf.state))),
                      [(cwd, args[:6]) for cwd, args in host.git_log])

    def test_resume_before_the_first_edit_reuses_the_worktree(self):
        host = FakeHost()
        host.worktrees[WORKTREE] = "HEAD"
        state = saved_run("spec", 0, worktree=WORKTREE, pull_request=True)
        wf, *_ = resume(state, {"spec": [spec_turn], "build": [worktree_build_turn(1)],
                                "review": [review_turn(1, APPROVE)]}, host=host)

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse([a for a in host.git_calls if a[:2] == ("worktree", "add")])

    def test_resume_refuses_a_worktree_that_is_gone(self):
        for registered in ({}, {f"/proj/{orchestrator.WORKTREES_DIR}/ffffff": "HEAD"}):
            with self.subTest(registered=registered):
                host = FakeHost()
                host.worktrees.update(registered)
                wf, herdr, *_ = resume(saved_worktree_run("review", 1), {"review": []}, host=host)

                with self.assertRaisesRegex(OrchestratorError, f"the worktree of run {wf.state.run_id}, "
                                                               f"{WORKTREE}, is gone"):
                    wf.run()
                self.assertEqual(herdr.calls, [])
                self.assertFalse([a for a in host.git_calls if a[:2] == ("worktree", "add")])

    def test_resume_with_a_pull_request_finishes_without_the_worktree(self):
        state = saved_worktree_run("publish", 1, verdict=APPROVE, pr_url="https://github.com/o/r/pull/7")
        wf, herdr, host, _ = resume(state, {})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.phase, "done")
        self.assertFalse([a for a in host.git_calls if a[0] in ("worktree", "switch")])
        self.assertEqual(herdr.calls[-1], ("close", "w1"))

    def test_a_state_saved_without_worktree_resumes_in_place(self):
        saved = asdict(saved_run("review", 1, agents=("spec", "build", "review"), pull_request=True,
                                 base_branch="main", branch=RUN_BRANCH))
        del saved["worktree"]
        state = RunState.from_dict(saved)
        self.assertEqual((state.worktree, state.work_dir), (None, "/proj"))
        host = FakeHost(branch=RUN_BRANCH)
        host.changed.add("/proj/limiter.py")
        wf, *_ = resume(state, {"review": [review_turn(1, APPROVE)]}, host=host,
                        files={lambda s: s.build_path(1): "report 1"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual({cwd for cwd, _ in host.git_log}, {"/proj"})
        self.assertEqual(host.git_calls[-1], ("switch", "--quiet", "main"))


class TestResumeCLI(unittest.TestCase):
    def test_machine_requires_cwd(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["resume", "a1b2c3", "--machine", "m"])

    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_resume_runs_the_saved_state(self, _resolve, run):
        saved = asdict(saved_run("review", 1, agents=("spec", "build", "review")))
        with patch.object(Host, "run_states", return_value=[(10**4, saved)]), patch("builtins.print") as out:
            self.assertEqual(main(["resume", "a1b2c3"]), 0)
        run.assert_called_once()
        out.assert_called_with(f"{APPROVE}: {RunState.from_dict(saved).review_path(1)}")

    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_finished_run_exit_status(self, _resolve):
        url = "https://github.com/o/r/pull/7"
        cases = [
            (APPROVE, [], 0, f"{APPROVE}: {url}"),
            (APPROVE, ["a.py"], orchestrator.EXIT_CONFLICT, f"{APPROVE}: {url} (a draft: it conflicts with main)"),
            (CHANGES_REQUESTED, ["a.py"], orchestrator.EXIT_CHANGES_REQUESTED,
             f"{CHANGES_REQUESTED}: {url} (a draft: it conflicts with main)"),
        ]
        for verdict, conflicts, status, line in cases:
            with self.subTest(verdict=verdict, conflicts=conflicts):
                saved = asdict(saved_run("done", 1, agents=("spec", "build", "review"), pull_request=True,
                                         base_branch="main", verdict=verdict, pr_url=url, conflicts=conflicts))
                with patch.object(Host, "run_states", return_value=[(10**4, saved)]), \
                        patch("builtins.print") as out:
                    self.assertEqual(main(["resume", "a1b2c3"]), status)
                out.assert_called_with(line)

    @patch.object(Workflow, "run", side_effect=KeyboardInterrupt)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_interrupt_names_the_resume_command(self, _resolve, _run):
        with patch("sys.stderr") as err, patch("orchestrator.new_run_id", return_value="20260930-070000-c0ffee"):
            self.assertEqual(main(["run", "task"]), orchestrator.EXIT_INTERRUPTED)
        self.assertIn("resume with: orchestrator.py resume c0ffee", "".join(c.args[0] for c in err.write.call_args_list))


class TestHerdrLookups(unittest.TestCase):
    def not_found(self, code):
        return MagicMock(return_value=completed(stderr=json.dumps({"error": {"code": code, "message": "x"}}),
                                                returncode=1))

    def test_agent_record(self):
        record = {"agent_status": "idle", "agent_session": {"value": "abc"}}
        run = MagicMock(return_value=result({"agent": record}))
        self.assertEqual(orchestrator.agent_session(Herdr(run=run).agent("spec-x")), "abc")

    def test_workspace_and_pane_exist(self):
        run = MagicMock(return_value=result({}))
        self.assertTrue(Herdr(run=run).workspace_exists("wA"))
        self.assertEqual(run.call_args.args[0], ["herdr", "workspace", "get", "wA"])
        self.assertTrue(Herdr(run=run).pane_exists("wA:p1"))
        self.assertEqual(run.call_args.args[0], ["herdr", "pane", "get", "wA:p1"])

    def test_workspace_and_pane_gone(self):
        self.assertFalse(Herdr(run=self.not_found("workspace_not_found")).workspace_exists("wA"))
        self.assertFalse(Herdr(run=self.not_found("pane_not_found")).pane_exists("wA:p1"))

    def test_other_errors_raise(self):
        herdr = Herdr(run=self.not_found("server_unavailable"))
        with self.assertRaises(HerdrError):
            herdr.pane_exists("wA:p1")


class TestAtomicWrite(unittest.TestCase):
    def test_real_filesystem_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            host = Host()
            run_dir = f"{d}/{orchestrator.RUNS_DIR}/r-abc"
            host.write(f"{run_dir}/state.json", '{"run_id": "r-abc"}\n')
            host.write(f"{run_dir}/state.json", '{"run_id": "r-abc", "phase": "build"}\n')
            self.assertEqual(os.listdir(run_dir), ["state.json"])
            [(age, state)] = host.run_states(d)
            self.assertEqual(state["phase"], "build")
            self.assertLess(age, 5)

    def test_keep_mtime(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            host, path = Host(), f"{d}/state.json"
            host.write(path, "old\n")
            os.utime(path, (1_000_000_000, 1_000_000_000))
            host.write(path, "new\n", keep_mtime=True)
            self.assertEqual((host.read(path), os.stat(path).st_mtime), ("new\n", 1_000_000_000))
            self.assertEqual(os.listdir(d), ["state.json"])
            host.write(path, "newer\n")
            self.assertGreater(os.stat(path).st_mtime, 1_000_000_000)



# ---------------------------------------------------------------------------
# The quality gate
# ---------------------------------------------------------------------------

JENKINS = "https://jenkins.example"
JENKINS_TOKEN = "jenkins-secret-token"
CREDENTIALS = {"JENKINS_URL": f"{JENKINS}/", "JENKINS_USER": "agents", "JENKINS_TOKEN": JENKINS_TOKEN}
JOB = "AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality"
JOB_PATH = "/job/AI-Agents-Orchestrator/job/py-ai-agents-orchestrator-quality/"
REF_1_1 = "orchestrator-ci/a1b2c3-1-q1"

# The pull request's notes for a final change that resume --no-quality-gate left unjudged (pr_body).
OFF_AFTER_ANALYSIS = ("> [!NOTE]\n> The quality gate was turned off (`resume --no-quality-gate`) after its last "
                      "analysis, and the Builder changed the code since, so nothing judged the final change.")
OFF_BEFORE_ANALYSIS = ("> [!NOTE]\n> The quality gate was turned off (`resume --no-quality-gate`) before any "
                       "analysis finished, so nothing judged the change.")


def gate_evidence(n, q, text, unjudged=False):
    """GateEvidence citing quality-<n>-<q>.md, of the given text."""
    return orchestrator.GateEvidence(orchestrator.QualityFile(n, q, text), unjudged)


class FakeResponse:
    def __init__(self, body=b"", headers=None):
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def outcome(gate="OK", *, archived=True, result=None, findings=(), tests=(), test_report=True, console="", polls=2):
    """What one Jenkins build does.

    gate: the GATE of the quality.md its Gate stage archives; archived=False for a build that ended before that
    stage. result defaults to what the Jenkinsfile ends with. findings: the numbered lines of quality.md.
    tests: (className, name, errorDetails) of each failing test in Jenkins's test report. polls: how many polls
    still see it building.
    """
    if result is None:
        result = ("SUCCESS" if gate == "OK" else "UNSTABLE") if archived else "FAILURE"
    quality = None
    if archived:
        numbered = "\n".join(f"{k}. {f}" for k, f in enumerate(findings, 1)) or "None."
        quality = (f"GATE: {gate}\n\n`orchestrator.py gate` judged the working tree.\n\n"
                   f"## Issues on changed lines\n\n{numbered}\n")
    return dict(result=result, quality=quality, tests=list(tests), test_report=test_report, console=console,
                polls=polls)


RED = dict(gate="ERROR", findings=["`limiter.py:3` error ruff F401: `os` imported but unused"])
# A gate that fails on something else than RED.
RED2 = dict(gate="ERROR", findings=["`limiter.py:9` error mypy arg-type: Argument 1 has incompatible type"])


class FakeCI:
    """Jenkins behind urlopen; each triggered build plays the next scripted outcome, OK once they run out.

    - `down`: holds "jenkins" while Jenkins does not answer.
    - `fail`: {(method, path): status} for requests answered with an HTTP error.
    - `job_gone`: the job was deleted; `forgotten`: Jenkins forgot its queue items.

    Every request is recorded as (method, url, form) in `requests`, and by a short name in `names`.
    """

    def __init__(self):
        self.requests, self.names, self.auth = [], [], []
        self.parameters = list(orchestrator.JOB_PARAMETERS)
        self.outcomes = []
        self.down = set()
        self.fail = {}
        self.job_gone = False
        self.forgotten = False
        self.builds = {}
        self.queue = {}
        self.number = 40

    def add_build(self, ref, out):
        """A build of ref as Jenkins has it; returns its number."""
        self.number += 1
        self.builds[self.number] = {"ref": ref, "left": out["polls"], **out}
        return self.number

    def triggered(self):
        """The refs of the builds, in the order they were triggered or added."""
        return [b["ref"] for _, b in sorted(self.builds.items())]

    def __call__(self, req, timeout=None):
        assert timeout == orchestrator.HTTP_TIMEOUT
        method, url = req.get_method(), req.full_url
        form = dict(urllib.parse.parse_qsl(req.data.decode())) if req.data else {}
        self.requests.append((method, url, form))
        self.auth.append(req.get_header("Authorization"))
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if "jenkins" in self.down:
            # A reason that quotes a token, to show it never gets any further.
            self.names.append("jenkins down")
            raise urllib.error.URLError(f"[Errno 111] Connection refused (token {JENKINS_TOKEN})")
        if (method, parts.path) in self.fail:
            self.names.append(f"{method} {parts.path} failing")
            raise urllib.error.HTTPError(url, self.fail[(method, parts.path)], "Error", {}, io.BytesIO(b"<html>"))
        name, answer = self.jenkins(method, parts.path, query, form)
        self.names.append(name)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "Error", {}, io.BytesIO(b"<html>"))
        if isinstance(answer, FakeResponse):
            return answer
        return FakeResponse(json.dumps(answer))

    def jenkins(self, method, path, query, form):
        if path.startswith("/queue/item/"):
            item = int(path.split("/")[3])
            if self.forgotten or item not in self.queue:
                return "queue", 404
            waiting = self.queue[item]
            if waiting["left"]:
                waiting["left"] -= 1
                return "queue", {"why": "Waiting for next available executor"}
            return "queue", {"executable": {"number": waiting["number"]}}
        if not path.startswith(JOB_PATH) or self.job_gone:
            return f"{method} {path}", 404
        rest = path[len(JOB_PATH):]
        if rest == "api/json" and "property" in query.get("tree", ""):
            return "parameters", {"property": [{}, {"parameterDefinitions": [{"name": p} for p in self.parameters]}]}
        if rest == "api/json":
            builds = sorted(self.builds.items(), reverse=True)[:20]
            return "find", {"builds": [{"number": n, "actions": [{}, {"parameters": [
                {"name": "GIT_REF", "value": b["ref"]}]}]} for n, b in builds]}
        if rest == "buildWithParameters" and method == "POST":
            n = self.add_build(form["GIT_REF"], self.outcomes.pop(0) if self.outcomes else outcome())
            self.queue[900 + n] = {"left": 1, "number": n}
            return "trigger", FakeResponse(headers={"Location": f"{JENKINS}/queue/item/{900 + n}/"})
        n, _, what = rest.partition("/")
        build = self.builds.get(int(n)) if n.isdigit() else None
        if build is None:
            return f"{method} {path}", 404
        if what == "api/json":
            if build["left"]:
                build["left"] -= 1
                return "build", {"building": True, "result": None}
            return "build", {"building": False, "result": build["result"]}
        if what == "artifact/quality.md":
            return "quality", 404 if build["quality"] is None else FakeResponse(build["quality"])
        if what == "testReport/api/json":
            if not build["test_report"]:
                return "tests", 404
            cases = [{"className": c, "name": t, "status": "FAILED", "errorDetails": e} for c, t, e in build["tests"]]
            return "tests", {"suites": [{"cases": [*cases, {"className": "t", "name": "ok", "status": "PASSED"}]}]}
        if what == "consoleText":
            return "console", FakeResponse(build["console"])
        return f"{method} {path}", 404


def ci_for(fake, env=None):
    return CI(JOB, env=CREDENTIALS if env is None else env, urlopen=fake)


def gated(script, *, fake=None, env=None, **kw):
    """make_workflow with the quality gate on a FakeCI."""
    fake = fake or FakeCI()
    wf, herdr, host, notes = make_workflow(script, ci=ci_for(fake, env), **kw)
    return wf, herdr, host, fake, notes


def fix_turn(n, q):
    """The Builder's answer to quality-<n>-<q>.md."""
    def turn(prompt, state, host):
        host.write("/proj/limiter.py", f"version {n}.{q}")
        return writes(lambda s: s.quality_build_path(n, q), f"report {n} q{q}")(prompt, state, host)
    return turn


def collapsed(names):
    """The names without repeats in a row, which polls make."""
    return [n for i, n in enumerate(names) if i == 0 or n != names[i - 1]]


def quality_state(q=1, ci=None, **kw):
    """A run saved in quality round q of round 1."""
    return saved_run("quality", 1, agents=("spec", "build"), quality_job=JOB, quality_round=q, ci=ci or {}, **kw)


def resume_gated(state, script, fake, **kw):
    """resume with the gate on fake; the ref in flight, if any, is on origin."""
    host = kw.pop("host", None) or FakeHost()
    if state.ci.get("ref"):
        host.remote.setdefault(state.ci["ref"], state.ci["sha"])
    return resume(state, script, ci=ci_for(fake), max_quality_rounds=state.max_quality_rounds, host=host,
                  files={lambda s: s.build_path(1): "report 1", **kw.pop("files", {})}, **kw)


def leaks(text):
    return JENKINS_TOKEN in text


# A token-shaped string, built so that no scanner takes this file for a leak.
FAKE_TOKEN = "ghp_" + "Zq7" * 12

# The fake gitleaks: writes its argv and the config it was given beside itself, then acts as the file `mode` says.
FAKE_GITLEAKS = r'''#!PYTHON
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "argv"), "w") as f:
    json.dump(args, f)
with open(os.path.join(here, "env"), "w") as f:
    f.write("\n".join(f"{k}={v}" for k, v in sorted(os.environ.items()) if k.startswith("GIT_")))
mode = open(os.path.join(here, "mode")).read().strip()
if args[:2] == ["git", "--help"]:
    sys.exit(1 if mode == "old" else 0)
for flag, name in (("--config", "config-seen"), ("--gitleaks-ignore-path", "ignore-seen")):
    with open(args[args.index(flag) + 1]) as src, open(os.path.join(here, name), "w") as dst:
        dst.write(src.read())
report = args[args.index("--report-path") + 1]
commit = next(w for w in args[args.index("git") + 1].split("=", 1)[1].split() if not w.startswith("--"))
if mode == "leak":
    with open(report, "w") as f:
        json.dump([{"RuleID": "github-pat", "File": "cfg.py", "StartLine": 1, "Commit": commit,
                    "Fingerprint": f"{commit}:cfg.py:github-pat:1", "Secret": os.environ.get("TOKEN", ""),
                    "Match": "token = " + os.environ.get("TOKEN", "")}], f)
    sys.exit(1)
if mode == "clean":
    with open(report, "w") as f:
        f.write("[]")
    sys.exit(0)
sys.stderr.write("FTL unable to load gitleaks config\n")
sys.exit(2 if mode == "crash" else 1)
'''


class TestScanUnpublished(unittest.TestCase):
    """Host's scan before a push, over a real repository and a bare origin, with a fake gitleaks on PATH."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.repo, self.origin, self.bin = (os.path.join(self.tmp, d) for d in ("repo", "origin.git", "bin"))
        self.run_dir = os.path.join(self.tmp, "run")
        self.host = Host()
        os.makedirs(self.bin)
        self.host.git(self.tmp, "init", "--quiet", "--bare", self.origin)
        self.host.git(self.tmp, "init", "--quiet", "-b", "main", self.repo)
        for key, setting in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, setting)
        self.commit({"a.txt": "a\n"}, "base")
        self.git("remote", "add", "origin", self.origin)
        self.git("push", "--quiet", "origin", "main")
        self.base = self.git("rev-parse", "HEAD").strip()
        path = os.path.join(self.bin, "gitleaks")
        with open(path, "w") as f:
            f.write(FAKE_GITLEAKS.replace("PYTHON", sys.executable))
        os.chmod(path, 0o700)
        self.mode("clean")
        env = patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}", "TOKEN": FAKE_TOKEN})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args):
        return self.host.git(self.repo, *args)

    def commit(self, files, message):
        for name, text in files.items():
            with open(os.path.join(self.repo, name), "w") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def mode(self, mode):
        with open(os.path.join(self.bin, "mode"), "w") as f:
            f.write(mode)

    def seen(self, name):
        with open(os.path.join(self.bin, name)) as f:
            return f.read()

    def push(self, commit):
        self.host.push_ref(self.repo, commit, "ci-ref", base=self.base,
                           report_path=f"{self.run_dir}/leaks-snapshot.json")

    def on_origin(self):
        return self.host.git(self.origin, "for-each-ref", "--format=%(refname)").split()

    def test_a_clean_range_is_pushed_after_its_scan(self):
        commit = self.commit({"b.txt": "b\n"}, "clean")
        self.push(commit)
        self.assertIn("refs/heads/ci-ref", self.on_origin())
        argv = json.loads(self.seen("argv"))
        self.assertIn(f"--log-opts=--diff-merges=remerge --no-textconv {commit} --not --remotes=origin", argv)
        # Not the working tree: gitleaks would read its .gitleaksignore.
        self.assertEqual(argv[-1], f"{self.run_dir}/gitleaks-source")
        self.assertIn("--redact", argv)

    def test_a_secret_blocks_the_push_and_stays_out_of_the_error(self):
        commit = self.commit({"cfg.py": f"token = '{FAKE_TOKEN}'\n"}, "leak")
        self.mode("leak")
        with self.assertRaises(orchestrator.LeaksFound) as caught:
            self.push(commit)
        self.assertEqual(caught.exception.leaks, [orchestrator.Leak("github-pat", "cfg.py", 1, commit,
                                                                    f"{commit}:cfg.py:github-pat:1")])
        self.assertIn(f"`cfg.py:1` github-pat in {commit[:12]}", str(caught.exception))
        # The fake report carries the secret unredacted; nothing built from it does.
        self.assertNotIn(FAKE_TOKEN, str(caught.exception) + repr(caught.exception.leaks))
        self.assertNotIn("refs/heads/ci-ref", self.on_origin())

    def test_the_change_s_own_gitleaks_files_do_not_judge_it(self):
        commit = self.commit({".gitleaks.toml": "[allowlist]\npaths = ['cfg.py']\n",
                              ".gitleaksignore": "anything\n"}, "allowlist itself")
        self.push(commit)
        self.assertEqual((self.seen("config-seen"), self.seen("ignore-seen")), ("[extend]\nuseDefault = true\n", ""))
        # The base's own files are what judge a change.
        self.git("reset", "--quiet", "--hard", self.base)
        self.base = self.commit({".gitleaks.toml": "title = 'base rules'\n"}, "rules")
        self.git("push", "--quiet", "origin", "main")
        self.push(self.commit({".gitleaks.toml": "title = 'loosened'\n"}, "loosen"))
        self.assertEqual(self.seen("config-seen"), "title = 'base rules'\n")

    def test_an_added_gitleaks_allow_is_a_leak(self):
        allowed = self.commit({"c.py": "x = 1\ny = 2  # gitleaks:allow\n"}, "allow")
        with self.assertRaises(orchestrator.LeaksFound) as caught:
            self.push(allowed)
        self.assertEqual([(leak.rule, leak.file, leak.line, leak.commit) for leak in caught.exception.leaks],
                         [("gitleaks-allow", "c.py", 2, allowed)])
        # One already on origin publishes nothing new.
        self.git("push", "--quiet", "origin", "HEAD:main")
        self.push(self.commit({"d.txt": "d\n"}, "after"))

    def test_an_added_line_that_looks_like_a_header_is_still_read(self):
        commit = self.commit({"notes.md": "a\n++ token # gitleaks:allow\n"}, "header-like")
        with self.assertRaises(orchestrator.LeaksFound) as caught:
            self.push(commit)
        self.assertEqual([(leak.file, leak.line) for leak in caught.exception.leaks], [("notes.md", 2)])

    def test_a_merge_counts_what_it_adds_not_what_its_parents_had(self):
        # Upstream, already on origin, adds a gitleaks:allow; merging it publishes nothing new.
        self.git("switch", "--quiet", "-c", "upstream")
        self.commit({"up.py": "x = 1  # gitleaks:allow\n"}, "upstream")
        self.git("push", "--quiet", "origin", "upstream")
        self.git("switch", "--quiet", "main")
        self.commit({"mine.txt": "mine\n"}, "mine")
        self.git("merge", "--quiet", "--no-edit", "upstream")
        self.push(self.git("rev-parse", "HEAD").strip())
        # An edit made in a merge is the merge's own.
        self.git("switch", "--quiet", "-c", "side", "HEAD~1")
        self.commit({"side.txt": "side\n"}, "side")
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--no-commit", "side")
        merge = self.commit({"evil.py": "y = 2  # gitleaks:allow\n"}, "merge side")
        with self.assertRaises(orchestrator.LeaksFound) as caught:
            self.push(merge)
        self.assertEqual([(leak.file, leak.commit) for leak in caught.exception.leaks], [("evil.py", merge)])

    def test_an_octopus_merge_is_refused(self):
        # git gives no remerge diff for three parents, so what such a merge adds cannot be scanned.
        for name in ("s1", "s2"):
            self.git("switch", "--quiet", "-c", name, self.base)
            self.commit({f"{name}.txt": name}, name)
        self.git("switch", "--quiet", "main")
        self.commit({"mine.txt": "mine\n"}, "mine")
        self.git("merge", "--quiet", "--no-edit", "s1", "s2")
        with self.assertRaisesRegex(OrchestratorError, "three or more parents"):
            self.push(self.git("rev-parse", "HEAD").strip())
        self.assertNotIn("refs/heads/ci-ref", self.on_origin())

    def test_the_change_s_gitattributes_do_not_hide_an_allow(self):
        commit = self.commit({".gitattributes": "notes.md -diff\n", "notes.md": "x # gitleaks:allow\n"}, "hidden")
        with self.assertRaises(orchestrator.LeaksFound) as caught:
            self.push(commit)
        self.assertEqual([(leak.file, leak.line) for leak in caught.exception.leaks], [("notes.md", 1)])

    def test_committed_gitattributes_do_not_hide_an_allow_whatever_git_s_config(self):
        # attr.tree=HEAD, or GIT_ATTR_SOURCE, makes git read the attributes a commit holds: the change's.
        self.git("config", "attr.tree", "HEAD")
        commit = self.commit({".gitattributes": "notes.md -diff\n", "notes.md": "x # gitleaks:allow\n"}, "hidden")
        with patch.dict(os.environ, {"GIT_ATTR_SOURCE": commit}), self.assertRaises(orchestrator.LeaksFound) as caught:
            self.push(commit)
        self.assertEqual([(leak.file, leak.line) for leak in caught.exception.leaks], [("notes.md", 1)])
        # gitleaks gets the same git environment, which brings back no tree's attributes.
        env = dict(line.split("=", 1) for line in self.seen("env").splitlines())
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], self.git("hash-object", "-t", "tree", os.devnull).strip())
        self.assertFalse({"GIT_ATTR_SOURCE", "GIT_WORK_TREE", "GIT_CONFIG_PARAMETERS"} & set(env))

    def test_neither_the_base_s_attributes_nor_git_c_hide_an_allow(self):
        # A -diff the base has for its own reasons is no allowlist; git -c exports GIT_CONFIG_PARAMETERS.
        self.base = self.commit({".gitattributes": "package-lock.json -diff\n"}, "base attributes")
        self.git("push", "--quiet", "origin", "main")
        commit = self.commit({"package-lock.json": '{"t": "x"} // gitleaks:allow\n'}, "lock")
        with patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": "'attr.tree'='HEAD'"}), \
                self.assertRaises(orchestrator.LeaksFound) as caught:
            self.push(commit)
        self.assertEqual([leak.file for leak in caught.exception.leaks], ["package-lock.json"])

    def test_a_nul_inside_a_line_does_not_end_the_commit_s_patch(self):
        # git still diffs as text a file whose first 8000 bytes hold no NUL.
        commit = self.commit({"n.txt": "x" * 9000 + "\na\0b\ny = 2  # gitleaks:allow\n"}, "nul")
        with self.assertRaises(orchestrator.LeaksFound) as caught:
            self.push(commit)
        self.assertEqual([(leak.file, leak.line) for leak in caught.exception.leaks], [("n.txt", 3)])

    def test_a_base_git_cannot_read_blocks_the_push(self):
        commit = self.commit({"b.txt": "b\n"}, "clean")
        self.base = "0" * 40
        with self.assertRaises(OrchestratorError):
            self.push(commit)
        self.assertNotIn("refs/heads/ci-ref", self.on_origin())

    def test_a_gitleaks_that_cannot_judge_blocks_the_push(self):
        commit = self.commit({"b.txt": "b\n"}, "clean")
        for mode, status in (("crash", "status 2"), ("bad-config", "status 1 and wrote no report")):
            with self.subTest(mode=mode):
                self.mode(mode)
                with self.assertRaises(OrchestratorError) as caught:
                    self.push(commit)
                self.assertNotIsInstance(caught.exception, orchestrator.LeaksFound)
                self.assertIn(status, str(caught.exception))
                self.assertIn("nothing was pushed", str(caught.exception))
                self.assertNotIn("refs/heads/ci-ref", self.on_origin())

    def test_a_report_left_by_an_earlier_scan_is_not_read(self):
        commit = self.commit({"b.txt": "b\n"}, "clean")
        self.push(commit)
        self.mode("bad-config")
        with self.assertRaises(OrchestratorError):
            self.push(commit)

    def test_the_preflight_needs_gitleaks_git(self):
        self.host.check_gitleaks()
        self.mode("old")
        with self.assertRaisesRegex(OrchestratorError, "`gitleaks git` does not run: install gitleaks 8.19"):
            self.host.check_gitleaks()
        # No gitleaks at all.
        with patch.dict(os.environ, {"PATH": self.run_dir}), \
                self.assertRaisesRegex(OrchestratorError, "install gitleaks 8.19"):
            self.host.check_gitleaks()


LEAK = orchestrator.Leak("github-pat", "limiter.py", 3, "c0ffee" * 6 + "c0ff", "c0ffee:limiter.py:github-pat:3")


class TestSecretScanWorkflow(unittest.TestCase):
    """The scan before each of a run's pushes, on FakeHost's scripted findings."""

    def script(self):
        return {"spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}

    def test_a_secret_in_a_snapshot_ends_the_run_before_jenkins(self):
        host = FakeHost()
        host.leaks = [LEAK]
        wf, herdr, host, fake, _ = gated(self.script(), host=host)
        with self.assertRaisesRegex(orchestrator.LeaksFound, r"`limiter.py:3` github-pat"):
            wf.run()
        self.assertNotIn("trigger", fake.names)
        self.assertEqual(host.remote, {})
        self.assertEqual(host.scans, [("snap1", "abc123", f"{wf.state.dir}/leaks-snapshot.json")])

    def test_a_secret_on_the_branch_stops_publish_until_it_is_rewritten(self):
        host = FakeHost()
        host.leaks, host.upstream = [LEAK], "u3"
        wf, herdr, host, _ = make_workflow(self.script(), host=host)
        with self.assertRaises(OrchestratorError) as caught, patch("sys.stderr", new_callable=io.StringIO) as err:
            wf.run()
        self.assertIn(f"git reset --soft {wf.state.base}", str(caught.exception))
        self.assertIn("`limiter.py:3` github-pat", str(caught.exception))
        # Scanned before origin's base is merged in, so the rewrite never has to undo a merge.
        self.assertFalse([c for c in host.git_calls if c[0] == "push"])
        self.assertNotIn(TestUpToDateBase.MERGE, host.git_calls)
        self.assertEqual([scan[0] for scan in host.scans], [f"refs/heads/{wf.state.branch}"])
        # The report gitleaks wrote is the only place the secret is.
        self.assertNotIn(FAKE_TOKEN, err.getvalue() + str(caught.exception))
        self.assertEqual([path for path, text in host.files.items() if FAKE_TOKEN in text],
                         [wf.state.leaks_path("publish")])
        self.assertEqual((wf.state.phase, host.prs), ("publish", []))
        self.assertEqual(json.loads(host.files[f"{wf.state.dir}/state.json"])["phase"], "publish")
        # Rewritten, the branch passes the scan, and the resume pushes it.
        host.leaks = []
        wf, *_ = make_workflow({}, host=host, state=wf.state)
        self.assertEqual(wf.run(), APPROVE)
        self.assertIn(("push", "--quiet", "--set-upstream", "origin", wf.state.branch), host.git_calls)
        self.assertEqual(len(host.prs), 1)

    def test_a_run_that_pushes_needs_gitleaks_before_the_interview(self):
        for kw in ({}, {"pull_request": False, "ci": ci_for(FakeCI())}):
            with self.subTest(kw=list(kw)):
                host = FakeHost()
                host.gitleaks_missing = True
                wf, herdr, *_ = make_workflow(self.script(), host=host, **kw)
                with self.assertRaisesRegex(OrchestratorError, "install gitleaks 8.19"):
                    wf.run()
                self.assertNotIn("start", [c[0] for c in herdr.calls])

    def test_a_run_that_pushes_nothing_needs_no_gitleaks(self):
        host = FakeHost()
        host.gitleaks_missing = True
        wf, *_ = make_workflow(self.script(), host=host, pull_request=False)
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(host.scans, [])


class TestQualityGate(unittest.TestCase):
    def test_without_the_gate_no_request_is_made(self):
        fake = FakeCI()
        with patch("urllib.request.urlopen", fake):
            wf, herdr, host, _ = make_workflow({
                "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
            self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.requests, [])
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual((saved["quality_job"], saved["quality_round"], saved["ci"]), (None, 0, {}))
        self.assertFalse(host.snapshots)

    def test_gate_passes_first_time(self):
        seen = []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [recording(review_turn(1, APPROVE), seen)]})

        self.assertEqual(wf.run(), APPROVE)

        self.assertEqual(collapsed(fake.names), ["parameters", "trigger", "queue", "build", "quality"])
        triggers = [form for method, url, form in fake.requests if url.endswith("/buildWithParameters")]
        self.assertEqual(triggers, [{"GIT_REF": REF_1_1}])
        self.assertIn(("push", "--quiet", "--force", "origin", f"snap1:refs/heads/{REF_1_1}"), host.git_calls)
        self.assertEqual(host.snapshots[0][0], "abc123")
        state = wf.state
        self.assertEqual(host.files[state.quality_path(1, 1)], (
            "GATE: OK\n\nJenkins build #41 judged snapshot `snap1` (round 1, quality round 1 of 3) and ended SUCCESS.\n"
            "\n`orchestrator.py gate` judged the working tree.\n\n## Issues on changed lines\n\nNone.\n"))
        self.assertIn(orchestrator.QUALITY_PASSED_NOTE.format(quality_path=state.quality_path(1, 1)), seen[0])
        self.assertIn(f"The Builder's report is in {state.build_path(1)}", seen[0])
        self.assertIn("<summary>Quality gate (round 1)</summary>", host.prs[0]["body"])
        # done: the refs are gone.
        self.assertEqual(host.remote, {})
        saved = json.loads(host.files[f"{state.dir}/state.json"])
        self.assertEqual((saved["phase"], saved["ci"]), ("done", {}))

    def test_credentials_travel_only_in_the_authorization_header(self):
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        wf.run()

        jenkins = "Basic " + base64.b64encode(f"agents:{JENKINS_TOKEN}".encode()).decode()
        for (_, url, form), auth in zip(fake.requests, fake.auth, strict=True):
            self.assertEqual(auth, jenkins)
            self.assertFalse(leaks(url + json.dumps(form)))
        for path, text in host.files.items():
            self.assertFalse(leaks(text), path)

    def test_jenkins_requests_go_under_the_jobs_folder(self):
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        wf.run()

        job = f"{JENKINS}{JOB_PATH}"
        urls = [url for _, url, _ in fake.requests]
        # The queue items Jenkins names in Location are its own, outside any job.
        self.assertEqual([u for u in urls if not u.startswith(job)],
                         [u for u in urls if u.startswith(f"{JENKINS}/queue/item/")])
        self.assertTrue(any(u.startswith(job) for u in urls))

    def test_gate_fails_then_passes(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED), outcome()]
        builds, reviews = [], []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), recording(fix_turn(1, 1), builds)],
            "review": [recording(review_turn(1, APPROVE), reviews)]}, fake=fake)

        self.assertEqual(wf.run(), APPROVE)

        state = wf.state
        quality = host.files[state.quality_path(1, 1)]
        self.assertTrue(quality.startswith("GATE: ERROR\n\nJenkins build #41 judged snapshot `snap1` "
                                           "(round 1, quality round 1 of 3) and ended UNSTABLE.\n"))
        self.assertIn("1. `limiter.py:3` error ruff F401: `os` imported but unused\n", quality)
        self.assertEqual(builds, [orchestrator.QUALITY_FIX_PROMPT.format(
            quality_path=state.quality_path(1, 1), report_path=state.quality_build_path(1, 1)) + NOTE])
        self.assertIn(f"The Builder's report is in {state.quality_build_path(1, 1)}", reviews[0])
        self.assertIn(orchestrator.QUALITY_PASSED_NOTE.format(quality_path=state.quality_path(1, 2)), reviews[0])
        self.assertTrue(host.files[state.quality_path(1, 2)].startswith("GATE: OK"))
        self.assertEqual(fake.triggered(), [REF_1_1, "orchestrator-ci/a1b2c3-1-q2"])

    def test_rounds_run_out(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED), outcome(**RED2)]
        reviews = []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), fix_turn(1, 1)],
            "review": [recording(review_turn(1, APPROVE), reviews)]}, fake=fake, max_quality_rounds=2)

        self.assertEqual(wf.run(), APPROVE)

        self.assertEqual(herdr.calls.count(("prompt", "build-a1b2c3")), 2)
        state = wf.state
        self.assertIn(orchestrator.QUALITY_UNRESOLVED_NOTE.format(quality_path=state.quality_path(1, 2)), reviews[0])
        self.assertIn(f"The Builder's report is in {state.quality_build_path(1, 1)}", reviews[0])
        self.assertEqual(host.prs[0]["body"].count("GATE: ERROR"), 1)

    def test_a_later_round_has_its_own_quality_rounds(self):
        reviews = []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), build_turn(2)],
            "review": [review_turn(1, CHANGES_REQUESTED), recording(review_turn(2, APPROVE), reviews)]})

        self.assertEqual(wf.run(), APPROVE)

        self.assertIn(("push", "--quiet", "--force", "origin", "snap2:refs/heads/orchestrator-ci/a1b2c3-2-q1"),
                      host.git_calls)
        self.assertEqual(fake.triggered(), [REF_1_1, "orchestrator-ci/a1b2c3-2-q1"])
        self.assertIn(f"the new report is in {wf.state.build_path(2)}", reviews[0])
        self.assertIn(orchestrator.QUALITY_PASSED_NOTE.format(quality_path=wf.state.quality_path(2, 1)), reviews[0])

    def preflight_fails(self, pattern, *, fake=None, env=None, host=None, **kw):
        wf, herdr, host, fake, _ = gated({"spec": []}, fake=fake, env=env, host=host, **kw)
        with self.assertRaisesRegex(OrchestratorError, pattern) as cm:
            wf.run()
        self.assertEqual(herdr.calls, [])
        self.assertFalse(leaks(str(cm.exception)))
        self.assertRegex(json.loads(host.files[f"{wf.state.dir}/state.json"])["error"], pattern)
        return fake

    def test_missing_credentials_fail_before_the_interview(self):
        env = {k: v for k, v in CREDENTIALS.items() if k != "JENKINS_USER"}
        fake = self.preflight_fails("the quality gate needs JENKINS_USER in", env=env)
        self.assertEqual(fake.requests, [])

    def test_unreachable_jenkins_fails_before_the_interview(self):
        fake = FakeCI()
        fake.down = {"jenkins"}
        self.preflight_fails("Jenkins at JENKINS_URL failed the preflight: GET .*Connection refused", fake=fake)

    def test_job_without_the_parameter_fails_before_the_interview(self):
        fake = FakeCI()
        fake.parameters = []
        self.preflight_fails("lacks the parameters GIT_REF; build it once by hand", fake=fake)

    def test_missing_job_fails_before_the_interview(self):
        fake = FakeCI()
        fake.job_gone = True
        self.preflight_fails(f"there is no Jenkins job {JOB} at JENKINS_URL: .* HTTP 404", fake=fake)

    def test_not_a_git_repository_is_refused(self):
        for pull_request in (True, False):
            with self.subTest(pull_request=pull_request):
                fake = self.preflight_fails("the quality gate needs a git repository", host=FakeHost(head=None),
                                            pull_request=pull_request)
                self.assertEqual(fake.requests, [])

    def test_no_pr_needs_a_clean_tree_and_an_origin(self):
        host = FakeHost()
        host.changed.add("/proj/wip.py")
        self.preflight_fails("uncommitted changes", host=host, pull_request=False)
        host = FakeHost()
        host.has_origin = False
        self.preflight_fails("pushes snapshots to origin, and /proj has no origin", host=host, pull_request=False)

    def test_no_pr_pushes_snapshots_without_a_run_branch(self):
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]},
            pull_request=False)

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn(("push", "--quiet", "--force", "origin", f"snap1:refs/heads/{REF_1_1}"), host.git_calls)
        self.assertFalse([c for c in host.git_calls if c[0] in ("switch", "commit", "add")])
        self.assertEqual(host.changed, {"/proj/limiter.py"})
        self.assertEqual(host.remote, {})

    def test_job_deleted_mid_run_fails_at_once(self):
        fake = FakeCI()

        def spec_then_job_deleted(prompt, state, host):
            fake.job_gone = True
            return spec_turn(prompt, state, host)
        wf, *_ = gated({"spec": [spec_then_job_deleted], "build": [build_turn(1)]}, fake=fake)

        with self.assertRaisesRegex(OrchestratorError, f"Jenkins job {JOB} is gone: POST .*HTTP 404"):
            wf.run()
        self.assertLess(wf.clock.now, 60)

    def jenkins_down_after_the_change_is_triggered(self, fake, wf, back_after=None):
        down_at = []

        def flaky():
            if fake.triggered() and not down_at:
                fake.down.add("jenkins")
                down_at.append(wf.clock.now)
            elif back_after and down_at and wf.clock.now >= down_at[0] + back_after:
                fake.down.discard("jenkins")
        wf.clock.hooks.append(flaky)

    def test_jenkins_unreachable_mid_round_then_back(self):
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        self.jenkins_down_after_the_change_is_triggered(fake, wf, back_after=300)

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn("jenkins down", fake.names)
        self.assertTrue(host.files[wf.state.quality_path(1, 1)].startswith("GATE: OK"))

    def test_jenkins_unreachable_until_the_round_times_out(self):
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)]})
        self.jenkins_down_after_the_change_is_triggered(fake, wf)

        with self.assertRaisesRegex(OrchestratorError, "quality gate: waiting for Jenkins to start the build of "
                                    f"{REF_1_1} did not finish within 1200s") as cm:
            wf.run()
        self.assertIn("Connection refused (token ****)", str(cm.exception))
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual(saved["ci"], {"ref": REF_1_1, "sha": "snap1", "queue_url": f"{JENKINS}/queue/item/941/"})
        self.assertEqual(saved["phase"], "quality")
        for path, text in host.files.items():
            self.assertFalse(leaks(text), path)
        # A failed run keeps the ref in flight, for a resume.
        self.assertIn(REF_1_1, host.remote)

    def test_heartbeat_during_a_twenty_minute_build(self):
        fake = FakeCI()
        fake.outcomes = [outcome(polls=110)]
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, fake=fake)
        beats = []
        orig = host.write

        def write(path, text):
            if path.endswith("state.json"):
                beats.append(wf.clock.now)
            orig(path, text)
        host.write = write

        self.assertEqual(wf.run(), APPROVE)
        self.assertGreater(wf.clock.now, 1100)
        gaps = [b - a for a, b in itertools.pairwise(beats)]
        self.assertLessEqual(max(gaps), HEARTBEAT_SECONDS + orchestrator.CI_POLL_SECONDS)

    def test_build_failed_before_the_gate_goes_back_to_the_builder(self):
        fake = FakeCI()
        console = "\n".join(f"line {i}" for i in range(100)) + f"\nJENKINS_TOKEN={JENKINS_TOKEN}\nSyntaxError: bad"
        fake.outcomes = [outcome(archived=False, console=console, test_report=False), outcome()]
        builds = []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), recording(fix_turn(1, 1), builds)],
            "review": [review_turn(1, APPROVE)]}, fake=fake)

        self.assertEqual(wf.run(), APPROVE)
        quality = host.files[wf.state.quality_path(1, 1)]
        self.assertTrue(quality.startswith("GATE: BUILD_FAILED\n"))
        self.assertIn("Jenkins build #41 of snapshot `snap1` (round 1, quality round 1 of 3) ended FAILURE before the "
                      "quality gate ran", quality)
        self.assertIn("JENKINS_TOKEN=****\nSyntaxError: bad", quality)
        self.assertNotIn("line 41\n", quality)
        self.assertIn("````\nline 42\n", quality)
        self.assertFalse(leaks(quality))
        self.assertIn(wf.state.quality_path(1, 1), builds[0])
        self.assertTrue(host.files[wf.state.quality_path(1, 2)].startswith("GATE: OK"))

    def test_build_failed_with_failing_tests_has_no_console(self):
        fake = FakeCI()
        fake.outcomes = [outcome(archived=False, console="x", tests=[("tests.t", "test_a", "AssertionError: 1 != 2")])]
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), fix_turn(1, 1)], "review": [review_turn(1, APPROVE)]},
            fake=fake)

        wf.run()
        quality = host.files[wf.state.quality_path(1, 1)]
        self.assertIn("- `tests.t.test_a`: AssertionError: 1 != 2", quality)
        self.assertNotIn("console", quality)
        self.assertNotIn("console", fake.names)

    def test_aborted_build_without_a_quality_file_fails_the_run(self):
        fake = FakeCI()
        fake.outcomes = [outcome(archived=False, result="ABORTED")]
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)]}, fake=fake)

        with self.assertRaisesRegex(OrchestratorError, f"build #41 of {REF_1_1} was aborted before the quality gate"):
            wf.run()
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual(saved["ci"], {"ref": REF_1_1, "sha": "snap1", "rejected": [41]})
        self.assertNotIn(wf.state.quality_path(1, 1), host.files)
        self.assertEqual(herdr.calls.count(("prompt", "build-a1b2c3")), 1)

    def test_quality_file_is_followed_whatever_the_result(self):
        for result in ("FAILURE", "ABORTED"):
            with self.subTest(result=result):
                fake = FakeCI()
                fake.outcomes = [outcome(result=result)]
                wf, herdr, host, fake, _ = gated({
                    "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, fake=fake)
                self.assertEqual(wf.run(), APPROVE)
                self.assertTrue(host.files[wf.state.quality_path(1, 1)].startswith("GATE: OK"))

    def test_queue_item_cancelled_fails_the_run(self):
        fake = FakeCI()
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)]}, fake=fake)
        orig = fake.jenkins

        def cancelled(method, path, query, form):
            if path.startswith("/queue/item/") and fake.triggered():
                return "queue", {"cancelled": True}
            return orig(method, path, query, form)
        fake.jenkins = cancelled

        with self.assertRaisesRegex(OrchestratorError, "cancelled in Jenkins's queue"):
            wf.run()
        self.assertEqual(json.loads(host.files[f"{wf.state.dir}/state.json"])["ci"], {"ref": REF_1_1, "sha": "snap1"})

    def test_done_deletes_only_this_runs_refs(self):
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        host.remote["orchestrator-ci/a1b2c3-1-q7"] = "left-over"
        host.remote["orchestrator-ci/ffffff-1-q1"] = "another-run"

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.phase, "done")
        self.assertEqual(host.remote, {"orchestrator-ci/ffffff-1-q1": "another-run"})

    def test_refused_request_mid_round_fails_at_once_naming_the_step(self):
        fake = FakeCI()
        fake.fail[("GET", f"{JOB_PATH}41/artifact/quality.md")] = 403
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)]}, fake=fake)

        with self.assertRaisesRegex(OrchestratorError, "quality gate: reading quality.md of Jenkins build #41: "
                                    r"GET .*/41/artifact/quality.md: HTTP 403"):
            wf.run()
        self.assertLess(wf.clock.now, 200)
        self.assertEqual(json.loads(host.files[f"{wf.state.dir}/state.json"])["ci"]["build"], 41)

    def test_cleanup_without_origin_listing_is_only_logged(self):
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        host.remote_branches = MagicMock(side_effect=OrchestratorError("ssh: connection reset"))

        with patch("sys.stderr") as err:
            self.assertEqual(wf.run(), APPROVE)
        self.assertIn("could not list the orchestrator-ci/ branches", "".join(c.args[0] for c in err.write.call_args_list))


class TestStuckQualityLoop(unittest.TestCase):
    def loop(self, *outcomes, builds=3):
        fake = FakeCI()
        fake.outcomes = list(outcomes)
        reviews = []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), *(fix_turn(1, q) for q in range(1, builds))],
            "review": [recording(review_turn(1, APPROVE), reviews)]}, fake=fake)
        self.assertEqual(wf.run(), APPROVE)
        return wf, herdr, reviews

    def test_the_same_findings_again_go_to_review(self):
        wf, herdr, reviews = self.loop(outcome(**RED), outcome(**RED))
        self.assertEqual(herdr.calls.count(("prompt", "build-a1b2c3")), 2)
        self.assertEqual(wf.state.quality_round, 2)
        self.assertIn(orchestrator.QUALITY_STUCK_NOTE.format(quality_path=wf.state.quality_path(1, 2)), reviews[0])
        self.assertNotIn(orchestrator.QUALITY_UNRESOLVED_NOTE[:40], reviews[0])

    def test_a_fixed_finding_and_a_new_one_keep_the_loop_going(self):
        both = dict(gate="ERROR", findings=[*RED["findings"], *RED2["findings"]])
        new = dict(gate="ERROR", findings=[RED2["findings"][0], "`limiter.py:1` error ruff E501: Line too long"])
        wf, herdr, reviews = self.loop(outcome(**both), outcome(**new), outcome(**RED))
        self.assertEqual(herdr.calls.count(("prompt", "build-a1b2c3")), 3)
        self.assertIn(orchestrator.QUALITY_UNRESOLVED_NOTE.format(quality_path=wf.state.quality_path(1, 3)),
                      reviews[0])

    def test_a_fixed_finding_keeps_the_loop_going(self):
        both = dict(gate="ERROR", findings=[*RED["findings"], *RED2["findings"]])
        _, herdr, _ = self.loop(outcome(**both), outcome(**RED), outcome(**RED))
        self.assertEqual(herdr.calls.count(("prompt", "build-a1b2c3")), 3)

    def test_a_finding_on_a_moved_line_is_the_same(self):
        moved = dict(gate="ERROR", findings=["`limiter.py:12` error ruff F401: `os` imported but unused"])
        _, herdr, _ = self.loop(outcome(**RED), outcome(**moved))
        self.assertEqual(herdr.calls.count(("prompt", "build-a1b2c3")), 2)

    def test_a_build_that_failed_with_nothing_listed_is_not_stuck(self):
        failed = outcome("ERROR", archived=False, test_report=False, console="error: cannot install")
        _, herdr, reviews = self.loop(failed, failed, failed)
        self.assertEqual(herdr.calls.count(("prompt", "build-a1b2c3")), 3)


class TestGateFailures(unittest.TestCase):
    def test_a_coverage_condition_repeated_is_stuck_until_its_words_change(self):
        def report(percent):
            return quality_report("ERROR", "judged.", conditions=[f"Coverage of new code is {percent}%."])
        fifty, sixty = (orchestrator.gate_failures(report(p)) for p in ("50.0", "60.0"))
        self.assertTrue(fifty.repeats(orchestrator.gate_failures(report("50.0"))))
        self.assertFalse(sixty.repeats(fifty))
        self.assertEqual(fifty.fingerprints, {})

    def test_reads_failing_findings_and_tests_and_leaves_findings_to_weigh_out(self):
        issue = {"path": "a.py", "line": 3, "severity": "error", "rule": "ruff F401", "message": "`os` unused"}
        whole = {"path": "b.py", "line": None, "severity": "error", "rule": "mypy", "message": "bad: 2 errors"}
        weigh = {"path": "a.py", "line": 4, "severity": "warning", "rule": "flaky-tests failed", "message": "x"}
        quality = quality_report("ERROR", "judged.", conditions=["1 tests fail; each is listed below."],
                                 issues=[issue, whole], to_weigh=[weigh],
                                 tests=[("tests.test_a.T.test_x", "AssertionError: 1 != 2")])
        checked = {"path": None, "line": None, "severity": "error", "rule": "", "message": "too big: 900 lines"}
        change = SimpleNamespace()
        quality = orchestrator.judge_run(quality, (
            orchestrator.OrchestratorCheck("diff-budget", lambda c: orchestrator.CheckFindings([checked], [weigh])),
        ), change)

        failures = orchestrator.gate_failures(quality)

        fp = orchestrator.finding_fingerprint
        self.assertEqual(failures.conditions, ["1 tests fail; each is listed below.",
                                               "1 findings of the `diff-budget` check; each is listed below."])
        self.assertEqual(failures.fingerprints, {
            "ruff": [fp("a.py", "ruff F401", "`os` unused")], "mypy": [fp("b.py", "mypy", "bad: 2 errors")],
            "diff-budget": [fp(None, "diff-budget", "too big: 900 lines")],
            "tests": [fp("tests.test_a.T.test_x", "tests", "")]})

    def test_a_fingerprint_ignores_digits_and_keeps_paths_and_rules_apart(self):
        fp = orchestrator.finding_fingerprint
        self.assertEqual(fp("a.py", "ruff C901", "too complex (16 > 15)"),
                         fp("a.py", "ruff C901", "too complex (17 > 15)"))
        self.assertNotEqual(fp("a.py", "ruff C901", "m"), fp("b.py", "ruff C901", "m"))
        self.assertNotEqual(fp("a.py", "ruff C901", "m"), fp("a.py", "ruff E501", "m"))
        self.assertEqual(len(fp(None, "x", "y")), 16)

    def test_nothing_failed_repeats_nothing(self):
        ok = orchestrator.gate_failures(quality_report("OK", "judged."))
        self.assertEqual(ok, orchestrator.GateFailures([], {}))
        self.assertFalse(ok.repeats(ok))


class TestQualityResume(unittest.TestCase):
    def test_resume_with_a_build_only_polls_it(self):
        fake = FakeCI()
        n = fake.add_build(REF_1_1, outcome())
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/", "build": n})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse(host.snapshots)
        self.assertFalse([c for c in host.git_calls if c[:3] == ("push", "--quiet", "--force")])
        self.assertEqual(collapsed(fake.names), ["build", "quality"])
        self.assertIn("`snap9`", host.files[state.quality_path(1, 1)])

    def test_resume_of_a_state_saved_with_sonarqube(self):
        fake = FakeCI()
        # A SonarQube-era build of the snapshot: it archived no quality.md.
        n = fake.add_build(REF_1_1, outcome(archived=False, result="SUCCESS"))
        saved = asdict(quality_state(ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/",
                                         "build": n, "ce_task": f"task-{n}"}))
        saved.update(quality_project="py-ai-agents-orchestrator-a1b2c3", quality_baseline="analysis-base")
        builds = []
        wf, herdr, host, _ = resume_gated(RunState.from_dict(saved), {
            "build": [recording(fix_turn(1, 1), builds)], "review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        quality = host.files[wf.state.quality_path(1, 1)]
        self.assertTrue(quality.startswith("GATE: BUILD_FAILED\n"))
        self.assertIn(f"Jenkins build #{n} of snapshot `snap9` (round 1, quality round 1 of 3) ended SUCCESS before "
                      f"the quality gate ran", quality)
        self.assertEqual(len(builds), 1)
        self.assertTrue(host.files[wf.state.quality_path(1, 2)].startswith("GATE: OK"))
        self.assertNotIn("quality_project", json.loads(host.files[f"{wf.state.dir}/state.json"]))

    def test_resume_with_only_the_ref_adopts_its_build(self):
        fake = FakeCI()
        n = fake.add_build(REF_1_1, outcome())
        fake.add_build("orchestrator-ci/a1b2c3-1-q2", outcome())
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9"})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn("trigger", fake.names)
        self.assertFalse(host.snapshots)
        self.assertIn(f"Jenkins build #{n}", host.files[state.quality_path(1, 1)])

    def test_resume_with_only_the_ref_and_no_build_triggers_one(self):
        fake = FakeCI()
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9"})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(collapsed(fake.names)[:2], ["find", "trigger"])
        self.assertFalse(host.snapshots)

    def test_forgotten_queue_item_finds_its_build(self):
        fake = FakeCI()
        n = fake.add_build(REF_1_1, outcome())
        fake.forgotten = True
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/"})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(collapsed(fake.names)[:3], ["queue", "find", "build"])
        self.assertIn(f"Jenkins build #{n}", host.files[state.quality_path(1, 1)])

    def test_forgotten_queue_item_without_a_build_fails_the_run(self):
        fake = FakeCI()
        fake.forgotten = True
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/"})
        wf, herdr, host, _ = resume_gated(state, {}, fake)

        with self.assertRaisesRegex(OrchestratorError, "Jenkins forgot queue item .*none of the job's last 20"):
            wf.run()
        self.assertEqual(wf.state.ci, {"ref": REF_1_1, "sha": "snap9"})

    def test_discarded_build_is_cut_back_so_a_resume_builds_again(self):
        fake = FakeCI()
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/",
                                  "build": 41})
        wf, herdr, host, _ = resume_gated(state, {}, fake)

        with self.assertRaisesRegex(OrchestratorError, f"Jenkins build #41 of {REF_1_1} is gone"):
            wf.run()
        self.assertEqual(wf.state.ci, {"ref": REF_1_1, "sha": "snap9"})

        wf, herdr, host, _ = resume_gated(wf.state, {"review": [review_turn(1, APPROVE)]}, fake, host=host)
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.triggered(), [REF_1_1])

    def test_a_leftover_ref_of_another_round_is_dropped(self):
        fake = FakeCI()
        state = quality_state(q=2, ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/"})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake,
                                          files={lambda s: s.quality_build_path(1, 1): "report 1 q1"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(REF_1_1, host.remote)
        self.assertEqual(fake.triggered(), ["orchestrator-ci/a1b2c3-1-q2"])

    def test_resume_with_the_quality_file_makes_no_request(self):
        fake = FakeCI()
        state = quality_state()
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake,
                                          files={lambda s: s.quality_path(1, 1): "GATE: OK\n"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.requests, [])

    def test_quality_file_without_a_gate_is_refused(self):
        fake = FakeCI()
        state = quality_state()
        wf, *_ = resume_gated(state, {}, fake, files={lambda s: s.quality_path(1, 1): "all good\n"})

        with self.assertRaisesRegex(OrchestratorError, "quality-1-1.md does not start with a GATE line; fix it"):
            wf.run()
        self.assertEqual(fake.requests, [])

    def test_build_ends_alike_live_and_resumed(self):
        cases = [
            ("no quality file", outcome(archived=False), "GATE: BUILD_FAILED"),
            ("quality file after FAILURE", outcome(result="FAILURE"), "GATE: OK"),
            ("aborted, no quality file", outcome(archived=False, result="ABORTED"), "was aborted before"),
        ]
        for name, out, expected in cases:
            with self.subTest(name):
                fake = FakeCI()
                n = fake.add_build(REF_1_1, out)
                state = quality_state(ci={"ref": REF_1_1, "sha": "snap9",
                                          "queue_url": f"{JENKINS}/queue/item/1/", "build": n})
                wf, herdr, host, _ = resume_gated(state, {"build": [fix_turn(1, 1)],
                                                          "review": [review_turn(1, APPROVE)]}, fake)
                if expected.startswith("GATE"):
                    self.assertEqual(wf.run(), APPROVE)
                    self.assertTrue(host.files[state.quality_path(1, 1)].startswith(expected))
                else:
                    with self.assertRaisesRegex(OrchestratorError, expected):
                        wf.run()
                    self.assertEqual(wf.state.ci, {"ref": REF_1_1, "sha": "snap9", "rejected": [n]})

    def test_fresh_builder_answering_a_quality_round_is_briefed(self):
        fake = FakeCI()
        seen = []
        state = saved_run("build", 1, agents=("spec", "build", "review"), quality_job=JOB, quality_round=1,
                          prompted="build-1-q1.md")
        wf, herdr, host, _ = resume_gated(state, {"build": [recording(fix_turn(1, 1), seen)],
                                                  "review": [review_turn(1, APPROVE)]}, fake,
                                          alive=["review"], files={lambda s: s.quality_path(1, 1): "GATE: ERROR\n"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn("You are the Builder", seen[0])
        self.assertIn(f"you are a fresh session. An earlier Builder session did the earlier turns; its changes are "
                      f"already in the working tree, and its reports are {state.build_path(1)}.", seen[0])
        self.assertTrue(seen[0].endswith(orchestrator.QUALITY_FIX_PROMPT.format(
            quality_path=state.quality_path(1, 1), report_path=state.quality_build_path(1, 1)) + NOTE))
        self.assertEqual(wf.state.quality_round, 2)

    def test_fresh_builder_in_a_later_round_hears_of_every_earlier_report(self):
        fake = FakeCI()
        seen = []
        state = saved_run("build", 2, agents=("spec", "build", "review"), quality_job=JOB)
        wf, *_ = resume_gated(state, {"build": [recording(build_turn(2), seen)],
                                      "review": [review_turn(2, APPROVE)]}, fake, alive=["review"],
                              files={lambda s: s.quality_build_path(1, 1): "r", lambda s: s.quality_build_path(1, 2): "r"})

        self.assertEqual(wf.run(), APPROVE)
        reports = ", ".join([state.build_path(1), state.quality_build_path(1, 1), state.quality_build_path(1, 2)])
        self.assertIn(f"its reports are {reports}.", seen[0])

    def test_the_last_allowed_quality_round_goes_to_review(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED)]
        reviews = []
        state = quality_state(q=2, max_quality_rounds=2)
        wf, herdr, host, _ = resume_gated(state, {"review": [recording(review_turn(1, APPROVE), reviews)]}, fake,
                                          files={lambda s: s.quality_build_path(1, 1): "report 1 q1"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(("prompt", "build-a1b2c3"), herdr.calls)
        self.assertIn(orchestrator.QUALITY_UNRESOLVED_NOTE.format(quality_path=state.quality_path(1, 2)), reviews[0])

    def test_resume_checks_the_credentials_before_the_builder(self):
        fake = FakeCI()
        state = saved_run("build", 1, agents=("spec", "build"), quality_job=JOB)
        env = {k: v for k, v in CREDENTIALS.items() if k != "JENKINS_TOKEN"}
        wf, herdr, *_ = resume(state, {"build": [build_turn(1)]}, ci=ci_for(fake, env))

        with self.assertRaisesRegex(OrchestratorError, "needs JENKINS_TOKEN"):
            wf.run()
        self.assertNotIn(("prompt", "build-a1b2c3"), herdr.calls)


SWITCH_BRANCH = "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3"


def pr_run(phase, rnd, **kw):
    """saved_run for a run that ends in a pull request, on its own branch."""
    kw.setdefault("agents", ("spec", "build", "review"))
    return saved_run(phase, rnd, pull_request=True, base_branch="main", branch=SWITCH_BRANCH, **kw)


def pr_host():
    """A FakeHost on pr_run's branch, which already holds a commit of the Builder's."""
    return FakeHost(head="def456", branch=SWITCH_BRANCH)


def switch(state, ci, host=None, pipeline=None):
    """turn_quality_gate_on with ci, or turn_quality_gate_off with None, on the default workflow unless pipeline is
    given; returns the host, pr_host() by default."""
    host = host or pr_host()
    pipeline = pipeline or orchestrator.DEFAULT_WORKFLOW
    if ci is None:
        orchestrator.turn_quality_gate_off(state, pipeline, host)
    else:
        orchestrator.turn_quality_gate_on(state, pipeline, host, ci)
    return host


class TestQualityGateOnResume(unittest.TestCase):
    def test_on_in_the_builders_step_gates_its_turn(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED)]
        reviews = []
        state = pr_run("build", 1)
        host = switch(state, ci_for(fake))
        self.assertEqual(fake.names, ["parameters"])
        self.assertEqual((state.phase, state.quality_job, state.quality_round), ("build", JOB, 0))

        wf, herdr, host, _ = resume(state, {"build": [build_turn(1), fix_turn(1, 1)],
                                            "review": [recording(review_turn(1, APPROVE), reviews)]},
                                    ci=ci_for(fake), host=host)
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.triggered(), [REF_1_1, "orchestrator-ci/a1b2c3-1-q2"])
        self.assertIn(f"The Builder's report is in {state.quality_build_path(1, 1)}", reviews[0])
        self.assertIn(orchestrator.QUALITY_PASSED_NOTE.format(quality_path=state.quality_path(1, 2)), reviews[0])
        self.assertEqual(json.loads(host.files[f"{state.dir}/state.json"])["quality_job"], JOB)

    def test_on_in_the_reviewers_step_judges_the_builders_report_before_the_review(self):
        fake = FakeCI()
        state = pr_run("review", 1)
        host = switch(state, ci_for(fake))
        self.assertEqual((state.phase, state.quality_round), (orchestrator.QUALITY, 1))

        reviews = []
        wf, herdr, host, _ = resume(state, {"review": [recording(review_turn(1, APPROVE), reviews)]},
                                    ci=ci_for(fake), host=host, files={lambda s: s.build_path(1): "report 1"})
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.triggered(), [REF_1_1])
        self.assertEqual(host.snapshots[0][0], "abc123")
        self.assertEqual(len(reviews), 1)
        self.assertIn(f"The Builder's report is in {state.build_path(1)}", reviews[0])
        self.assertIn(orchestrator.QUALITY_PASSED_NOTE.format(quality_path=state.quality_path(1, 1)), reviews[0])

    def test_on_with_the_reviewer_prompted_gates_the_next_builder_turn(self):
        fake = FakeCI()
        state = pr_run("review", 1, prompted="review-1.md")
        host = switch(state, ci_for(fake))
        self.assertEqual((state.phase, state.quality_round), ("review", 0))

        wf, herdr, host, _ = resume(state, {"build": [build_turn(2)], "review": [review_turn(2, APPROVE)]},
                                    ci=ci_for(fake), host=host, alive=["review"],
                                    files={lambda s: s.build_path(1): "report 1",
                                           lambda s: s.review_path(1): f"VERDICT: {CHANGES_REQUESTED}\n1. x"})
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.triggered(), ["orchestrator-ci/a1b2c3-2-q1"])
        self.assertNotIn(state.quality_path(1, 1), host.files)

    def test_on_with_the_reviewer_retried_or_blocked_gates_the_next_builder_turn(self):
        # Both have seen the change: one is asked again for the verdict it wrote, the other waits on its answer.
        for name, kw in (("retried", {"retrying": "review-1.md"}),
                         ("blocked", {"blocked": {"file": "review-1.md", "question": "which?", "k": 1}})):
            with self.subTest(name):
                state = pr_run("review", 1, **kw)
                switch(state, ci_for(FakeCI()))
                self.assertEqual((state.phase, state.quality_round, state.quality_job), ("review", 0, JOB))

    def test_on_in_round_0_is_run_quality_gate(self):
        fake = FakeCI()
        state = saved_run("spec", 0, agents=(), pull_request=True)
        switch(state, ci_for(fake), host=FakeHost())
        self.assertEqual((fake.requests, state.quality_job), ([], JOB))

        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)],
                                          "review": [review_turn(1, APPROVE)]}, fake=fake, state=state)
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(collapsed(fake.names), ["parameters", "trigger", "queue", "build", "quality"])

        # And its preflight, before the interview.
        fake = FakeCI()
        fake.parameters = []
        state = saved_run("spec", 0, agents=(), pull_request=True)
        switch(state, ci_for(fake), host=FakeHost())
        wf, herdr, *_ = gated({"spec": [spec_turn]}, fake=fake, state=state)
        with self.assertRaisesRegex(OrchestratorError, "lacks the parameters"):
            wf.run()
        self.assertNotIn(("prompt", "spec-a1b2c3"), herdr.calls)

    def test_an_in_place_no_pr_run_can_turn_it_on_in_round_0_only(self):
        fake = FakeCI()
        state = saved_run("spec", 0, agents=())
        switch(state, ci_for(fake))
        self.assertEqual(state.quality_job, JOB)
        # Round 0 checks the tree is clean before the interview, as run --quality-gate does.
        host = FakeHost()
        host.write("/proj/mine.py", "my own change")
        wf, *_ = gated({"spec": [spec_turn]}, fake=fake, state=state, host=host, pull_request=False)
        with self.assertRaisesRegex(OrchestratorError, "uncommitted changes"):
            wf.run()

    def test_refusals_leave_the_state_as_it_was(self):
        cases = [
            ("done", pr_run("done", 1, verdict=APPROVE), "is in phase done, past its last turn"),
            ("publish", pr_run("publish", 1), "is in phase publish, past its last turn"),
            ("done, the same job", pr_run("done", 1, verdict=APPROVE, quality_job=JOB),
             "is in phase done, past its last turn"),
            ("turned off", pr_run("build", 1, quality_gate_disabled=True),
             "turned off with --no-quality-gate, which cannot be undone"),
            ("another job", pr_run("build", 1, quality_job="other/job"),
             f"has quality gate job other/job, and cannot switch to {JOB}"),
            ("in place", saved_run("build", 1, agents=("spec", "build")), "is a --no-pr run in /proj itself"),
        ]
        for name, state, why in cases:
            with self.subTest(name):
                fake = FakeCI()
                before = asdict(state)
                with self.assertRaisesRegex(OrchestratorError, why):
                    switch(state, ci_for(fake))
                self.assertEqual(asdict(state), before)
                self.assertEqual(fake.requests, [])

        state = saved_run("build", 1, agents=("build",), pull_request=True, workflow="solo")
        before = asdict(state)
        with self.assertRaisesRegex(OrchestratorError, "is of workflow solo, which has no quality-gated step, so its "
                                                       "quality gate cannot be turned on"):
            switch(state, ci_for(FakeCI()), pipeline=SOLO)
        self.assertEqual(asdict(state), before)

    def test_the_same_job_again_changes_nothing(self):
        fake = FakeCI()
        state = pr_run("review", 1, quality_job=JOB, quality_round=1)
        before = asdict(state)
        with patch("sys.stderr") as err:
            switch(state, ci_for(fake))
        self.assertEqual((asdict(state), fake.requests), (before, []))
        self.assertIn(f"already has quality gate job {JOB}", "".join(c.args[0] for c in err.write.call_args_list))

    def test_a_failed_preflight_refuses_before_any_turn(self):
        no_origin = pr_host()
        no_origin.has_origin = False
        no_gitleaks = pr_host()
        no_gitleaks.gitleaks_missing = True
        unknown = FakeCI()
        unknown.job_gone = True
        cases = [
            ("no credentials", FakeCI(), {}, None, "needs JENKINS_URL, JENKINS_USER, JENKINS_TOKEN"),
            ("no such job", unknown, None, None, f"there is no Jenkins job {JOB}"),
            ("no origin", FakeCI(), None, no_origin, "pushes snapshots to origin, and /proj has no origin"),
            ("no gitleaks", FakeCI(), None, no_gitleaks, "every push is scanned for secrets first"),
        ]
        for name, fake, env, host, why in cases:
            with self.subTest(name):
                state = pr_run("build", 1)
                before = asdict(state)
                with self.assertRaisesRegex(OrchestratorError, why):
                    switch(state, ci_for(fake, env), host=host)
                self.assertEqual(asdict(state), before)

    def test_the_cli_refuses_with_exit_1_and_leaves_state_json_alone(self):
        saved = asdict(saved_run("build", 1, agents=("spec", "build")))
        cases = [
            (["--quality-gate", JOB], "is a --no-pr run in /proj itself"),
            (["--no-quality-gate"], "has no quality gate, so --no-quality-gate does not apply"),
        ]
        for flags, why in cases:
            with self.subTest(flags[0]), patch.object(Host, "resolve_dir", return_value="/proj"), \
                    patch.dict("os.environ", {"HERDR_ENV": "1"}), \
                    patch.object(Host, "run_states", return_value=[(10**4, saved)]), \
                    patch.object(Host, "write") as write, patch.object(Workflow, "run") as run, \
                    patch("sys.stderr") as err:
                self.assertEqual(main(["resume", "a1b2c3", *flags]), orchestrator.EXIT_ERROR)
                run.assert_not_called()
                write.assert_not_called()
                self.assertIn(why, "".join(c.args[0] for c in err.write.call_args_list))

    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_the_cli_runs_the_workflow_with_the_new_job(self, _resolve, run):
        saved = asdict(pr_run("build", 1))
        fake = FakeCI()
        with patch.object(Host, "run_states", return_value=[(10**4, saved)]), \
                patch.object(Host, "git_run", return_value=completed()), \
                patch.object(Host, "check_gitleaks"), \
                patch("orchestrator.CI", side_effect=lambda job: CI(job, env=CREDENTIALS, urlopen=fake)) as ci, \
                patch.object(Workflow, "__init__", return_value=None) as init, patch("builtins.print"):
            main(["resume", "a1b2c3", "--quality-gate", JOB, "--max-quality-rounds", "2"])
        self.assertEqual(fake.names, ["parameters"])
        self.assertEqual([c.args for c in ci.call_args_list], [(JOB,), (JOB,)])
        state = init.call_args.args[2]
        self.assertEqual((state.quality_job, state.max_quality_rounds), (JOB, 2))


class TestQualityGateOffOnResume(unittest.TestCase):
    def test_off_in_quality_drops_the_analysis_and_goes_to_the_reviewer(self):
        ref_2 = "orchestrator-ci/a1b2c3-1-q2"
        state = pr_run("quality", 1, quality_job=JOB, quality_round=2, ci={"ref": ref_2, "sha": "snap9"},
                       turns=[{"step": orchestrator.QUALITY, "role": orchestrator.QUALITY, "round": 1,
                               "quality_round": 2, "file": "quality-1-2.md", "ended_at": None}])
        host = pr_host()
        host.remote = {REF_1_1: "snap1", ref_2: "snap9", "orchestrator-ci/ffffff-1-q1": "another-run"}
        switch(state, None, host=host)

        self.assertEqual(host.remote, {"orchestrator-ci/ffffff-1-q1": "another-run"})
        self.assertEqual((state.quality_job, state.quality_gate_disabled, state.ci, state.phase, state.turns),
                         (None, True, {}, "review", []))
        reviews = []
        wf, herdr, host, _ = resume(state, {"review": [recording(review_turn(1, APPROVE), reviews)]}, host=host,
                                    files={lambda s: s.build_path(1): "report 1",
                                           lambda s: s.quality_path(1, 1): "GATE: ERROR\n\n1. x\n",
                                           lambda s: s.quality_build_path(1, 1): "report 1 q1"})
        host.changed.add("/proj/limiter.py")
        self.assertEqual(wf.run(), APPROVE)
        self.assertIn(f"The Builder's report is in {state.quality_build_path(1, 1)}", reviews[0])
        self.assertIn(orchestrator.QUALITY_OFF_NOTE.format(quality_paths=state.quality_path(1, 1)), reviews[0])
        self.assertNotIn(orchestrator.QUALITY_UNRESOLVED_NOTE[:30], reviews[0])
        self.assertFalse(host.snapshots)
        body = host.prs[0]["body"]
        self.assertIn(f"{OFF_AFTER_ANALYSIS}\n\nOpened by", body)
        commit = next(c for c in host.git_calls if c[0] == "commit")
        self.assertEqual(commit[-2:], ("--trailer", "Orchestrator-Gate: ERROR after 1 quality round, then turned off"))

    def test_off_in_quality_tells_the_reviewer_what_the_round_last_found(self):
        cases = [
            ("first analysis unfinished", 1, {}, None, None),
            ("analysis finished, passed", 1, {lambda s: s.quality_path(1, 1): "GATE: OK\n"},
             orchestrator.QUALITY_PASSED_NOTE, 1),
            ("analysis finished, failed", 1, {lambda s: s.quality_path(1, 1): "GATE: ERROR\n\n1. x\n"},
             orchestrator.QUALITY_UNRESOLVED_NOTE, 1),
            ("stuck", 2, {lambda s: s.quality_path(1, 1): outcome(**RED)["quality"],
                          lambda s: s.quality_build_path(1, 1): "report 1 q1",
                          lambda s: s.quality_path(1, 2): outcome(**RED)["quality"]},
             orchestrator.QUALITY_STUCK_NOTE, 2),
        ]
        for name, q, files, note, noted in cases:
            with self.subTest(name):
                state = pr_run("quality", 1, quality_job=JOB, quality_round=q)
                host = switch(state, None)
                reviews = []
                wf, herdr, host, _ = resume(state, {"review": [recording(review_turn(1, APPROVE), reviews)]},
                                            host=host, files={lambda s: s.build_path(1): "report 1", **files})
                host.changed.add("/proj/limiter.py")
                self.assertEqual(wf.run(), APPROVE)
                self.assertIn(f"The Builder's report is in {state.last_report_path(1)}", reviews[0])
                body = host.prs[0]["body"]
                commit = next(c for c in host.git_calls if c[0] == "commit")
                if note is None:
                    self.assertNotIn("quality gate", reviews[0].lower())
                    self.assertNotIn("Quality gate", body)
                    self.assertIn(f"{OFF_BEFORE_ANALYSIS}\n\nOpened by", body)
                    self.assertNotIn("Orchestrator-Gate", " ".join(commit) + body)
                else:
                    self.assertIn(note.format(quality_path=state.quality_path(1, noted)), reviews[0])
                    self.assertIn("<summary>Quality gate (round 1)</summary>", body)
                    self.assertNotIn("[!NOTE]", body)
                    self.assertNotIn("then turned off", " ".join(commit) + body)
                    self.assertRegex(body, rf"\| Orchestrator-Gate \| `\w+ after {noted} quality rounds?` \|")

    def test_off_in_round_1_and_published_in_round_2_cites_round_1(self):
        state = pr_run("review", 1, quality_job=JOB, quality_round=2)
        host = switch(state, None)
        wf, herdr, host, _ = resume(state, {"review": [review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)],
                                            "build": [build_turn(2)]}, host=host,
                                    files={lambda s: s.build_path(1): "report 1",
                                           lambda s: s.quality_path(1, 1): "GATE: ERROR\n\n1. x\n",
                                           lambda s: s.quality_build_path(1, 1): "report 1 q1",
                                           lambda s: s.quality_path(1, 2): "GATE: OK\n"})
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual((wf.state.round, wf.state.quality_round), (2, 0))
        body = host.prs[0]["body"]
        self.assertIn(f"{OFF_AFTER_ANALYSIS}\n\nOpened by", body)
        self.assertIn("| Orchestrator-Gate | `OK after 2 quality rounds, then turned off` |", body)
        self.assertIn("<summary>Quality gate (round 1, before the last change)</summary>\n\nGATE: OK", body)
        commit = next(c for c in host.git_calls if c[0] == "commit")
        self.assertEqual(commit[-1], "Orchestrator-Gate: OK after 2 quality rounds, then turned off")

    def test_the_note_follows_a_conflict_warning(self):
        state = pr_run("quality", 1, quality_job=JOB, quality_round=1)
        host = switch(state, None)
        host.merge_upstream = MagicMock(return_value=["limiter.py"])
        wf, herdr, host, _ = resume(state, {"review": [review_turn(1, APPROVE)]}, host=host,
                                    files={lambda s: s.build_path(1): "report 1"})
        self.assertEqual(wf.run(), APPROVE)
        warning, note, head = host.prs[0]["body"].split("\n\n", 3)[:3]
        self.assertTrue(warning.startswith("> [!WARNING]"))
        self.assertEqual(note, OFF_BEFORE_ANALYSIS)
        self.assertTrue(head.startswith("Opened by"))

    def test_off_in_the_builders_quality_fix_goes_straight_to_the_reviewer(self):
        state = pr_run("build", 1, quality_job=JOB, quality_round=1, prompted="build-1-q1.md")
        host = switch(state, None)
        self.assertEqual((state.phase, state.quality_round), ("build", 1))

        reviews = []
        with patch("urllib.request.urlopen") as urlopen:
            wf, herdr, host, _ = resume(state, {"review": [recording(review_turn(1, APPROVE), reviews)]},
                                        host=host, alive=["build"],
                                        files={lambda s: s.build_path(1): "report 1",
                                               lambda s: s.quality_path(1, 1): "GATE: ERROR\n\n1. x\n",
                                               lambda s: s.quality_build_path(1, 1): "report 1 q1"})
            self.assertEqual(wf.run(), APPROVE)
        urlopen.assert_not_called()
        self.assertFalse(host.snapshots)
        self.assertIn(f"The Builder's report is in {state.quality_build_path(1, 1)}", reviews[0])
        self.assertIn(orchestrator.QUALITY_OFF_NOTE.format(quality_paths=state.quality_path(1, 1)), reviews[0])
        self.assertEqual(wf.state.quality_round, 2)

    def test_a_failed_cleanup_is_only_logged(self):
        state = pr_run("quality", 1, quality_job=JOB, quality_round=1)
        host = pr_host()
        host.remote_branches = MagicMock(side_effect=OrchestratorError("ssh: connection reset"))
        with patch("sys.stderr") as err:
            switch(state, None, host=host)
        self.assertIn("could not list the orchestrator-ci/ branches", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertEqual((state.quality_job, state.quality_gate_disabled, state.phase), (None, True, "review"))

    def test_a_failed_ref_deletion_is_only_logged(self):
        state = pr_run("review", 1, quality_job=JOB, quality_round=1)
        host = pr_host()
        host.remote = {REF_1_1: "snap1"}
        host.delete_remote_branch = MagicMock(side_effect=OrchestratorError("ssh: connection reset"))
        with patch("sys.stderr") as err:
            switch(state, None, host=host)
        self.assertIn(f"could not delete {REF_1_1} on origin", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertTrue(state.quality_gate_disabled)

    def test_refs_left_by_a_failed_deletion_are_deleted_at_the_end(self):
        state = pr_run("review", 1, quality_job=JOB, quality_round=1)
        host = pr_host()
        host.remote = {REF_1_1: "snap1"}
        failing = host.delete_remote_branch
        host.delete_remote_branch = MagicMock(side_effect=OrchestratorError("ssh: connection reset"))
        with patch("sys.stderr"):
            switch(state, None, host=host)
        host.delete_remote_branch = failing
        wf, herdr, host, _ = resume(state, {"review": [review_turn(1, APPROVE)]}, host=host,
                                    files={lambda s: s.build_path(1): "report 1"})
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(host.remote, {})

    def test_metrics_count_a_gate_turned_off_as_a_gate(self):
        state = pr_run("review", 1, quality_job=JOB, quality_round=1)
        switch(state, None)
        metrics = orchestrator.run_metrics(state, 10**9)
        self.assertEqual((metrics["quality_gate"], metrics["max_quality_rounds"]),
                         (True, orchestrator.DEFAULT_MAX_QUALITY_ROUNDS))

    def test_refused_without_a_gate_or_once_done(self):
        for name, state in [("no gate", pr_run("build", 1)), ("done", pr_run("done", 1, quality_job=JOB))]:
            with self.subTest(name):
                before = asdict(state)
                with self.assertRaisesRegex(OrchestratorError, "so --no-quality-gate does not apply"):
                    switch(state, None)
                self.assertEqual(asdict(state), before)

    def test_turned_off_after_a_failed_round_a_workflow_without_a_verdict_finishes(self):
        solo = Pipeline("gsolo", {"impl": "Implementer"}, (
            Step("impl", "impl", "impl-{n}.md", "Implement {task}; report to {path}.", edits=True, quality_gated=True),))
        state = saved_run("quality", 1, agents=(), pull_request=True, base_branch="main", branch=SWITCH_BRANCH,
                          workflow="gsolo", quality_job=JOB, quality_round=1)
        host = switch(state, None, host=pr_host(), pipeline=solo)
        self.assertEqual(state.phase, orchestrator.PUBLISH)

        wf, herdr, host, _ = resume(state, {}, host=host, pipeline=solo,
                                    files={lambda s: f"{s.dir}/impl-1.md": "impl 1",
                                           lambda s: s.quality_path(1, 1): "GATE: ERROR\n\n1. x\n"})
        self.assertEqual(wf.run(), orchestrator.FINISHED)
        self.assertFalse(host.prs[0]["draft"])
        self.assertNotIn("[!NOTE]", host.prs[0]["body"])
        with patch("builtins.print"):
            self.assertEqual(orchestrator.finish(orchestrator.FINISHED, wf.state, solo), 0)

    def test_turned_off_in_its_quality_fix_a_workflow_without_a_verdict_finishes_unjudged(self):
        solo = Pipeline("gsolo", {"impl": "Implementer"}, (
            Step("impl", "impl", "impl-{n}.md", "Implement {task}; report to {path}.", edits=True, quality_gated=True),))
        state = saved_run("impl", 1, agents=(), pull_request=True, base_branch="main", branch=SWITCH_BRANCH,
                          workflow="gsolo", quality_job=JOB, quality_round=1)
        host = switch(state, None, host=pr_host(), pipeline=solo)
        wf, herdr, host, _ = resume(state, {"impl": [impl_turn(1, 1)]}, host=host, pipeline=solo,
                                    files={lambda s: f"{s.dir}/impl-1.md": "impl 1",
                                           lambda s: s.quality_path(1, 1): "GATE: ERROR\n\n1. x\n"})
        self.assertEqual(wf.run(), orchestrator.FINISHED)
        self.assertEqual(wf.state.quality_round, 2)
        body = host.prs[0]["body"]
        self.assertFalse(host.prs[0]["draft"])
        self.assertIn(f"{OFF_AFTER_ANALYSIS}\n\nOpened by", body)
        self.assertIn("| Orchestrator-Gate | `ERROR after 1 quality round, then turned off` |", body)

    def test_round_results_keep_the_quality_files(self):
        state = pr_run("review", 1, quality_job=JOB, quality_round=1)
        host = switch(state, None)
        wf, herdr, host, _ = resume(state, {"review": [review_turn(1, APPROVE)]}, host=host,
                                    files={lambda s: s.build_path(1): "report 1",
                                           lambda s: s.quality_path(1, 1): "GATE: OK\n"})
        self.assertEqual(wf.run(), APPROVE)
        self.assertIn("Round 1: quality gate OK; review APPROVE", host.files[f"{state.dir}/summary.md"])

    def test_show_names_the_gate(self):
        cases = [("on", {"quality_job": JOB}, f"quality    {JOB}"),
                 ("off", {"quality_gate_disabled": True}, "quality    off (turned off by resume)"),
                 ("never", {}, None)]
        for name, fields_, line in cases:
            with self.subTest(name):
                host = FakeHost()
                state = {**run_record("build"), "cwd": "/proj", **fields_}
                host.files[f"/proj/{orchestrator.RUNS_DIR}/{state['run_id']}/state.json"] = json.dumps(state)
                with patch.object(host, "run_files", return_value=[]), patch("builtins.print") as out:
                    orchestrator.show_run(host, "/proj", "c0ffee", "here", lambda pid: True, [])
                printed = [c.args[0] for c in out.call_args_list if c.args]
                quality = [p for p in printed if p.startswith("quality ")]
                self.assertEqual(quality, [line] if line else [])

    def test_a_state_saved_before_the_switch_loads_with_the_gate_not_turned_off(self):
        saved = asdict(pr_run("build", 1, quality_job=JOB))
        del saved["quality_gate_disabled"]
        state = RunState.from_dict(saved)
        self.assertFalse(state.quality_gate_disabled)
        wf, herdr, host, fake, _ = gated({"build": [build_turn(1)], "review": [review_turn(1, APPROVE)]},
                                         state=state, host=pr_host())
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.triggered(), [REF_1_1])

PYPI_JSON = "https://pypi.org/pypi/{}/json"
# 2026-10-23, the day the young-package tests run on.
TODAY = 1_792_713_600


def pypi_project(first="2015-01-03", latest="2024-06-22", version="3.13.0", license="MIT", expression=None,
                 classifiers=()):
    """PyPI's JSON for a project with one early release and its latest, as https://docs.pypi.org/api/json/ has it."""
    def files(day):
        return [{"upload_time_iso_8601": f"{day}T10:00:00.123456Z"}, {"upload_time_iso_8601": f"{day}T11:00:00Z"}]
    return {"info": {"version": version, "license": license, "license_expression": expression,
                     "classifiers": list(classifiers)},
            "releases": {"0.1": files(first), "0.2": [], version: files(latest)}, "urls": files(latest)}


class FakePyPI:
    """PyPI behind urlopen: each project in `projects` answers its JSON, any other 404; `down` makes every request
    time out. `asked` lists the project names requested, in order. With `clock`, a FakeClock, each request takes
    `seconds` of its time, and is logged as ("ask", name) in `events`."""

    def __init__(self, **projects):
        self.projects = projects
        self.down = False
        self.asked = []
        self.clock, self.seconds, self.events = None, 0, []

    def __call__(self, req, timeout=None):
        assert timeout == orchestrator.PYPI_TIMEOUT
        url = req.full_url
        m = re.fullmatch(r"https://pypi\.org/pypi/([^/]+)/json", url)
        assert m, url
        self.asked.append(m.group(1))
        self.events.append(("ask", m.group(1)))
        if self.clock:
            self.clock.now += self.seconds
        if self.down:
            raise TimeoutError("timed out")
        if m.group(1) not in self.projects:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        return FakeResponse(json.dumps(self.projects[m.group(1)]))


def pyproject_with(*requirements):
    """A pyproject.toml depending on httpx, then on requirements, from line 5 on."""
    deps = "".join(f'    "{r}",\n' for r in ("httpx>=0.27", *requirements))
    return f'[project]\nname = "limiter"\ndependencies = [\n{deps}]\n'


def pyproject_diff(*requirements):
    """`git diff -U0` of pyproject_with() becoming pyproject_with(*requirements)."""
    added = "".join(f'+    "{r}",\n' for r in requirements)
    return ("diff --git a/pyproject.toml b/pyproject.toml\nindex 1111111..2222222 100644\n--- a/pyproject.toml\n"
            f"+++ b/pyproject.toml\n@@ -4,0 +5,{len(requirements)} @@ dependencies = [\n{added}")


def pyproject_files(*requirements, base="abc123", commit="snap1"):
    return {(base, "pyproject.toml"): pyproject_with(), (commit, "pyproject.toml"): pyproject_with(*requirements)}


SPEC_NAMING_LIMITS = ("# Add a token-bucket rate limiter\n\n## Goal\nThrottle the client with the `Limits` package, "
                      "declared in pyproject.toml.\n")


def touched(diff):
    """The ChangedFiles of the files changed_lines finds in diff, each under its own path, with the lines it adds and
    none removed."""
    return [orchestrator.ChangedFile(path, path, len(lines), 0) for path, lines in changed_lines(diff).items()]


def run_change(diff, files=None, spec=SPEC_NAMING_LIMITS, now=TODAY, counts=None, in_time=lambda: True):
    """The RunChange of a quality round from base to new; files holds each file's text by (commit, path), and counts
    the ChangedFiles, touched(diff) by default."""
    files = files or {}
    return orchestrator.RunChange(spec=spec, base="base", commit="new", changed=changed_lines(diff),
                                  files=touched(diff) if counts is None else counts,
                                  file_at=lambda commit, path: files.get((commit, path)), now=now, in_time=in_time)


def new_file_diff(path, lines):
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n"
            f"+++ b/{path}\n@@ -0,0 +1,{lines} @@\n" + "".join(f"+line {i}\n" for i in range(lines)))


class TestOrchestratorChecks(unittest.TestCase):
    """The checks the quality round runs itself after the build, with the run's spec: driven through a run."""

    def quality(self, diff, files=None, *, pypi=None, spec=SPEC_NAMING_LIMITS, build=None):
        """The quality file of a gated run's first quality round, whose snapshot has diff and files, and the PyPI it
        asked. build is the Jenkins build's outcome. The round runs on TODAY."""
        fake = FakeCI()
        fake.outcomes = [build or outcome()]
        pypi = pypi or FakePyPI(limits=pypi_project())
        host = FakeHost()
        host.diff = diff
        host.committed = files or {}
        wf, herdr, host, fake, _ = gated({
            "spec": [writes(lambda s: s.spec_path, spec)], "build": [build_turn(1)],
            "review": [review_turn(1, APPROVE)]}, fake=fake, host=host, max_quality_rounds=1,
            pypi=orchestrator.PyPI(urlopen=pypi))
        wf.wallclock = lambda: TODAY
        self.assertEqual(wf.run(), APPROVE)
        return host.files[wf.state.quality_path(1, 1)], pypi

    def test_a_dependency_the_spec_names_passes_with_its_metadata(self):
        quality, pypi = self.quality(pyproject_diff("limits>=3"), pyproject_files("limits>=3"))

        self.assertTrue(quality.startswith("GATE: OK\n"), quality)
        self.assertNotIn("## Failed conditions", quality)
        self.assertIn("\n## The orchestrator's checks\n", quality)
        self.assertIn("These do not fail the gate.", quality)
        self.assertIn("\n1. `pyproject.toml:5` warning new-dependencies added: `limits`, which the spec names: on PyPI "
                      "since 2015-01-03, latest 3.13.0 of 2024-06-22, license MIT.\n", quality)
        self.assertEqual(pypi.asked, ["limits"])

    def test_a_dependency_the_spec_does_not_name_fails(self):
        pypi = FakePyPI(requests=pypi_project(first="2011-02-14", version="2.32.3", license="Apache-2.0"))
        quality, _ = self.quality(pyproject_diff("requests>=2"), pyproject_files("requests>=2"), pypi=pypi)

        self.assertTrue(quality.startswith("GATE: ERROR\n"), quality)
        self.assertIn("## Failed conditions\n\n- 1 findings of the `new-dependencies` check; each is listed below.\n"
                      "\n## Issues on changed lines\n", quality)
        self.assertIn("\n1. `pyproject.toml:5` error new-dependencies not-in-spec: `requests` is a new dependency that "
                      "the spec does not name; on PyPI since 2011-02-14, latest 2.32.3 of 2024-06-22, license "
                      "Apache-2.0.\n", quality)
        self.assertEqual(parse_gate(quality), "ERROR")

    def test_a_failing_finding_is_numbered_after_the_builds_own(self):
        quality, _ = self.quality(pyproject_diff("requests>=2"), pyproject_files("requests>=2"), build=outcome(**RED))

        self.assertTrue(quality.startswith("GATE: ERROR\n"))
        self.assertIn("1. `limiter.py:3` error ruff F401", quality)
        self.assertIn("\n2. `pyproject.toml:5` error new-dependencies not-in-spec: `requests` is a new dependency "
                      "that the spec does not name; PyPI does not know it either.\n", quality)

    def test_a_dependency_pypi_does_not_know_fails(self):
        spec = "# Add a rate limiter\n\nUse the `limitz` package.\n"
        quality, pypi = self.quality(pyproject_diff("limitz"), pyproject_files("limitz"), spec=spec)

        self.assertTrue(quality.startswith("GATE: ERROR\n"))
        self.assertIn("1. `pyproject.toml:5` error new-dependencies not-on-pypi: PyPI does not know `limitz`: the name "
                      "may be mistyped, or name a package that does not exist yet, which anyone could register "
                      "(slopsquatting).\n", quality)
        self.assertEqual(pypi.asked, ["limitz"])

    def test_a_pypi_timeout_is_unchecked_never_passed(self):
        pypi = FakePyPI(limits=pypi_project())
        pypi.down = True
        quality, _ = self.quality(pyproject_diff("limits>=3"), pyproject_files("limits>=3"), pypi=pypi)

        self.assertTrue(quality.startswith("GATE: ERROR\n"))
        self.assertIn("1. The change as a whole: error new-dependencies unchecked: PyPI could not be asked about "
                      f"`limits`: GET {PYPI_JSON.format('limits')}: timed out; whether they exist is not known.\n",
                      quality)
        self.assertNotIn("which the spec names", quality)

    def test_a_dependency_younger_than_fourteen_days_is_flagged_not_failed(self):
        pypi = FakePyPI(limits=pypi_project(first="2026-10-20", latest="2026-10-21", version="0.2"))
        quality, _ = self.quality(pyproject_diff("limits"), pyproject_files("limits"), pypi=pypi)

        self.assertTrue(quality.startswith("GATE: OK\n"))
        self.assertIn("1. `pyproject.toml:5` warning new-dependencies young: `limits`, which the spec names: on PyPI "
                      "since 2026-10-20, latest 0.2 of 2026-10-21, license MIT; it is younger than 14 days, so few "
                      "have used it yet.\n", quality)

    def test_the_budget_and_unexpected_paths_are_reported_and_never_change_the_gate(self):
        diff = "".join(new_file_diff(f"src/m{i}.py", 40) for i in range(16))
        for build, gate in ((outcome(), "OK"), (outcome(**RED), "ERROR")):
            with self.subTest(gate=gate):
                quality, pypi = self.quality(diff, build=build)

                self.assertTrue(quality.startswith(f"GATE: {gate}\n"))
                self.assertNotIn("## Failed conditions", quality)
                first = 2 if gate == "ERROR" else 1
                self.assertIn(f"\n{first}. The change as a whole: warning diff-budget: The change touches 16 files, "
                              "over the workflow's max_files of 15, and 640 lines (640 added, 0 removed), over its "
                              "max_lines of 600. Say in your report why it cannot be smaller.\n", quality)
                self.assertIn(f"\n{first + 1}. `src/m0.py (the whole file)` warning unexpected-paths: the spec "
                              "mentions neither this path nor its file name.\n", quality)
                self.assertIn(f"\n{first + 16}. `src/m9.py (the whole file)`", quality)
                self.assertEqual(pypi.asked, [])

    def test_a_change_the_checks_find_nothing_in_keeps_the_builds_file(self):
        quality, pypi = self.quality("")
        self.assertEqual(quality, (
            "GATE: OK\n\nJenkins build #41 judged snapshot `snap1` (round 1, quality round 1 of 1) and ended SUCCESS.\n"
            "\n`orchestrator.py gate` judged the working tree.\n\n## Issues on changed lines\n\nNone.\n"))

    def test_a_build_that_failed_before_the_gate_is_not_checked(self):
        quality, pypi = self.quality(pyproject_diff("requests"), pyproject_files("requests"),
                                     build=outcome(archived=False, console="boom"))
        self.assertTrue(quality.startswith("GATE: BUILD_FAILED\n"))
        self.assertNotIn("orchestrator's checks", quality)
        self.assertEqual(pypi.asked, [])

    def test_slow_pypi_keeps_the_run_alive_and_stops_at_the_rounds_deadline(self):
        pypi = FakePyPI(limits=pypi_project(), rich=pypi_project(), attrs=pypi_project())
        host = FakeHost()
        host.diff = pyproject_diff("limits", "rich", "attrs")
        host.committed = pyproject_files("limits", "rich", "attrs")
        spec = "# Throttle\n\nUse limits, rich and attrs, in pyproject.toml.\n"
        wf, herdr, host, fake, _ = gated({
            "spec": [writes(lambda s: s.spec_path, spec)], "build": [build_turn(1), fix_turn(1, 1)],
            "review": [review_turn(1, APPROVE)]}, host=host, max_quality_rounds=1,
            pypi=orchestrator.PyPI(urlopen=pypi))
        wf.wallclock = lambda: TODAY
        # Each request takes half the round's time.
        pypi.clock, pypi.seconds = wf.clock, orchestrator.QUALITY_TIMEOUT / 2
        beat = wf._heartbeat

        def heartbeat():
            pypi.events.append("beat")
            beat()
        wf._heartbeat = heartbeat

        self.assertEqual(wf.run(), APPROVE)

        quality = host.files[wf.state.quality_path(1, 1)]
        self.assertEqual(pypi.asked, ["limits", "rich"])
        self.assertIn("unchecked: PyPI could not be asked about `attrs`: the quality round had too little time left to "
                      "ask;", quality)
        self.assertTrue(quality.startswith("GATE: ERROR\n"))
        asks = [i for i, e in enumerate(pypi.events) if e != "beat"]
        self.assertIn("beat", pypi.events[asks[0]:asks[1]])

    def test_without_the_quality_gate_none_of_this_runs(self):
        pypi = FakePyPI(limits=pypi_project())
        host = FakeHost()
        host.diff = pyproject_diff("requests")
        host.committed = pyproject_files("requests")
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, host=host,
            pypi=orchestrator.PyPI(urlopen=pypi))

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual((pypi.asked, host.file_reads), ([], []))
        self.assertFalse([p for p in host.files if "quality-" in p])

    def test_the_spec_is_the_runs_task_without_a_contract_step(self):
        pypi = FakePyPI(limits=pypi_project())
        host = FakeHost()
        host.diff = pyproject_diff("limits")
        host.committed = pyproject_files("limits")
        state = RunState("20260929-120000-a1b2c3", "limit requests with limits in pyproject.toml", "/proj", None,
                         phase="impl", workflow="impl")
        wf, herdr, host, fake, _ = gated({"impl": [impl_turn(1)], "review": [review_turn(1, APPROVE)]},
                                         host=host, state=state, pipeline=IMPL, pypi=orchestrator.PyPI(urlopen=pypi))
        wf.wallclock = lambda: TODAY

        self.assertEqual(wf.run(), APPROVE)
        quality = host.files[wf.state.quality_path(1, 1)]
        self.assertTrue(quality.startswith("GATE: OK\n"), quality)
        self.assertIn("new-dependencies added: `limits`, which the spec names", quality)


class TestNewDependencies(unittest.TestCase):
    """new_dependencies on a RunChange of its own, with PyPI faked."""

    def find(self, change, pypi):
        return orchestrator.new_dependencies(change, pypi=orchestrator.PyPI(urlopen=pypi))

    def test_the_spec_names_a_package_as_a_word_in_any_spelling(self):
        names = orchestrator.spec_names("Use `Requests`. Add pytest_randomly, and ruff>=0.5 to the dev group.")
        self.assertTrue({"requests", "pytest-randomly", "ruff"} <= names)
        self.assertNotIn("pytest", names)
        self.assertNotIn("request", names)

    def test_a_package_the_lock_alone_adds_is_pulled_in(self):
        lock_base = 'version = 1\n\n[[package]]\nname = "limiter"\nversion = "0.1"\nsource = { virtual = "." }\n'
        lock = lock_base + "".join(
            f'\n[[package]]\nname = "{n}"\nversion = "1.0"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
            for n in ("limits", "Deprecated", "packaging"))
        lock += '\n[[package]]\nname = "vendored"\nversion = "1.0"\nsource = { path = "vendor/x" }\n'
        diff = pyproject_diff("limits") + (
            "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n@@ -6,0 +7,20 @@\n")
        files = {**pyproject_files("limits", base="base", commit="new"), ("base", "uv.lock"): lock_base,
                 ("new", "uv.lock"): lock}
        mit = "License :: OSI Approved :: MIT License"
        pypi = FakePyPI(limits=pypi_project(), deprecated=pypi_project(first="2016-11-01", latest="2026-10-20",
                                                                        version="1.2.18", license=None,
                                                                        classifiers=[mit]),
                        packaging=pypi_project(license="", expression="Apache-2.0 OR BSD-2-Clause"))

        issues, to_weigh = self.find(run_change(diff, files), pypi)

        self.assertEqual(issues, [])
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in to_weigh],
                         [("pyproject.toml", 5, "added"), ("uv.lock", None, "pulled-in")])
        self.assertEqual(to_weigh[1]["message"], (
            "3 new packages that the change's dependencies pull in: `deprecated` (on PyPI since 2016-11-01, latest "
            "1.2.18 of 2026-10-20, license MIT License), `packaging` (on PyPI since 2015-01-03, latest 3.13.0 of "
            "2024-06-22, license Apache-2.0 OR BSD-2-Clause), `vendored` (from the path vendor/x, not from PyPI)."))
        self.assertEqual(pypi.asked, ["limits", "deprecated", "packaging"])

    def test_pypi_is_asked_no_more_once_it_failed(self):
        pypi = FakePyPI()
        pypi.down = True
        diff = pyproject_diff("limits", "requests")
        issues, to_weigh = self.find(run_change(diff, pyproject_files("limits", "requests", base="base",
                                                                      commit="new")), pypi)

        self.assertEqual(pypi.asked, ["limits"])
        self.assertEqual([(f["path"], f["rule"]) for f in issues],
                         [("pyproject.toml", "not-in-spec"), (None, "unchecked")])
        self.assertEqual(issues[0]["message"], "`requests` is a new dependency that the spec does not name.")
        self.assertIn("about `limits`, `requests`: GET", issues[1]["message"])
        self.assertEqual(to_weigh, [])

    def test_pypi_is_asked_only_while_the_round_has_time(self):
        pypi = FakePyPI(limits=pypi_project(), rich=pypi_project())
        answers = iter([True, False])
        change = run_change(pyproject_diff("limits", "rich", "attrs"),
                            pyproject_files("limits", "rich", "attrs", base="base", commit="new"),
                            spec="limits, rich and attrs", in_time=lambda: next(answers))

        issues, to_weigh = self.find(change, pypi)

        self.assertEqual(pypi.asked, ["limits"])
        self.assertEqual([f["rule"] for f in to_weigh], ["added"])
        self.assertEqual([(f["rule"], f["message"]) for f in issues], [
            ("unchecked", ("PyPI could not be asked about `rich`, `attrs`: the quality round had too little time "
                           "left to ask; whether they exist is not known."))])

    def test_a_dependency_is_found_on_the_line_that_names_it_whole(self):
        pypi = FakePyPI(**{"zope-interface": pypi_project(), "zope": pypi_project()})
        reqs = ("zope.interface>=5", "zope")
        _, to_weigh = self.find(run_change(pyproject_diff(*reqs), pyproject_files(*reqs, base="base", commit="new"),
                                           spec="zope and zope.interface"), pypi)
        self.assertEqual([(f["line"], f["message"][:16]) for f in to_weigh],
                         [(5, "`zope-interface`"), (6, "`zope`, which th")])

    def test_a_dependency_from_a_url_is_not_looked_up(self):
        url = "git+https://example.com/limits.git@v3"
        pypi = FakePyPI()
        issues, to_weigh = self.find(run_change(pyproject_diff(f"limits @ {url}"),
                                                pyproject_files(f"limits @ {url}", base="base", commit="new")), pypi)
        self.assertEqual((issues, pypi.asked), ([], []))
        self.assertEqual(to_weigh[0]["message"], f"`limits`, which the spec names: from {url}, not from PyPI.")

    def test_dependency_lists_of_every_kind_are_read_without_the_project_itself(self):
        text = textwrap.dedent("""\
            [project]
            name = "My_Proj"
            dependencies = ["Foo.Bar[extra] >=1; python_version>'3'", "baz @ https://example.com/baz.whl"]
            [project.optional-dependencies]
            docs = ["sphinx"]
            all = ["my-proj[docs]"]
            [dependency-groups]
            dev = ["pytest", {include-group = "lint"}, "my.proj[docs]"]
            lint = ["ruff==0.5"]
            [tool.uv]
            dev-dependencies = ["ignored"]
        """)
        self.assertEqual(orchestrator.python_dependencies({"pyproject.toml": text}.get).direct, {
            "foo-bar": None, "baz": orchestrator.PackageSource("url", "https://example.com/baz.whl"), "sphinx": None,
            "pytest": None, "ruff": None})

    def test_the_project_itself_in_an_extra_is_no_new_dependency(self):
        new = pyproject_with() + '[project.optional-dependencies]\nall = ["limiter[a,b]"]\n'
        pypi = FakePyPI()
        found = self.find(run_change("", {("base", "pyproject.toml"): pyproject_with(), ("new", "pyproject.toml"): new},
                                     counts=[orchestrator.ChangedFile("pyproject.toml", "pyproject.toml", 2, 0)]), pypi)
        self.assertEqual((found, pypi.asked), (([], []), []))

    def changed_manifests(self, base, new, lock_base=None, lock=None):
        """A RunChange whose pyproject.toml, and uv.lock when given, go from base to new."""
        files = {("base", "pyproject.toml"): base, ("new", "pyproject.toml"): new,
                 ("base", "uv.lock"): lock_base, ("new", "uv.lock"): lock}
        counts = [orchestrator.ChangedFile(p, p, 1, 0) for p in ("pyproject.toml", "uv.lock")]
        return run_change("", files, spec="Use `forked`, `wheel-pkg`, `corp-utils`, `corp-auth` and `rich`.",
                          counts=counts)

    def test_a_package_with_a_uv_source_is_not_looked_up_on_pypi(self):
        new = textwrap.dedent("""\
            [project]
            name = "limiter"
            dependencies = ["httpx>=0.27", "member", "local", "forked", "wheel-pkg"]
            [tool.uv.sources]
            member = { workspace = true }
            local = { path = "../local", editable = true }
            forked = [{ git = "https://github.com/x/forked", rev = "v1" }]
            wheel-pkg = { url = "https://example.com/wheel_pkg-1.0-py3-none-any.whl" }
        """)
        # Each name squatted on PyPI: none of their metadata may show.
        pypi = FakePyPI(**{n: pypi_project() for n in ("member", "local", "forked", "wheel-pkg")})

        issues, to_weigh = self.find(self.changed_manifests(pyproject_with(), new), pypi)

        self.assertEqual(pypi.asked, [])
        self.assertEqual([(f["rule"], f["message"]) for f in issues], [
            ("not-in-spec", ("`local` is a new dependency that the spec does not name; from the path ../local, not "
                             "from PyPI."))])
        self.assertEqual([(f["rule"], f["message"]) for f in to_weigh], [
            ("added", "`member`: a member of the project's workspace, not from PyPI."),
            ("added", ("`forked`, which the spec names: from the repository https://github.com/x/forked, not from "
                       "PyPI.")),
            ("added", ("`wheel-pkg`, which the spec names: from https://example.com/wheel_pkg-1.0-py3-none-any.whl, "
                       "not from PyPI."))])

    def test_a_package_from_another_index_is_never_checked_against_pypi(self):
        corp = "https://pypi.internal.example/simple"
        new = textwrap.dedent(f"""\
            [project]
            name = "limiter"
            dependencies = ["httpx>=0.27", "corp-utils", "corp-auth", "corp-secret"]
            [[tool.uv.index]]
            name = "corp"
            url = "{corp}"
            explicit = true
            [tool.uv.sources]
            corp-utils = {{ index = "corp" }}
            corp-secret = {{ index = "corp" }}
        """)

        def lock(*packages):
            return 'version = 1\n' + "".join(
                f'\n[[package]]\nname = "{n}"\nversion = "1.0"\nsource = {{ registry = "{url}" }}\n'
                for n, url in packages)
        lock_base = lock(("httpx", "https://pypi.org/simple"))
        lock_new = lock(("httpx", "https://pypi.org/simple"), ("corp-utils", corp), ("corp-auth", corp),
                        ("corp-secret", corp), ("corp-lib", corp))
        squatted = FakePyPI(**{n: pypi_project() for n in ("corp-utils", "corp-auth", "corp-secret", "corp-lib")})

        issues, to_weigh = self.find(self.changed_manifests(pyproject_with(), new, lock_base, lock_new), squatted)

        self.assertEqual(squatted.asked, [])
        whence = f"from the index {corp}, not checked against PyPI"
        self.assertEqual([(f["rule"], f["message"]) for f in issues], [
            ("not-in-spec", f"`corp-secret` is a new dependency that the spec does not name; {whence}.")])
        self.assertEqual([(f["path"], f["rule"], f["message"]) for f in to_weigh], [
            ("pyproject.toml", "other-index", f"`corp-utils`, which the spec names: {whence}."),
            ("pyproject.toml", "other-index", f"`corp-auth`, which the spec names: {whence}."),
            ("uv.lock", "pulled-in", f"1 new packages that the change's dependencies pull in: `corp-lib` ({whence}).")])
        self.assertNotIn("on PyPI since", json.dumps(issues + to_weigh))

    def test_a_dependency_moved_to_another_source_is_reported(self):
        evil = "https://evil.example/simple"
        new = pyproject_with("requests") + textwrap.dedent(f"""\
            [[tool.uv.index]]
            name = "evil"
            url = "{evil}"
            [tool.uv.sources]
            requests = {{ index = "evil" }}
        """)

        def lock(registry):
            return 'version = 1\n' + "".join(
                f'\n[[package]]\nname = "{n}"\nversion = "1.0"\nsource = {{ registry = "{url}" }}\n'
                for n, url in (("requests", registry), ("idna", registry), ("httpx", "https://pypi.org/simple")))
        files = {("base", "pyproject.toml"): pyproject_with("requests"), ("new", "pyproject.toml"): new,
                 ("base", "uv.lock"): lock("https://pypi.org/simple"), ("new", "uv.lock"): lock(evil)}
        diff = ("diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n+++ b/pyproject.toml\n"
                "@@ -6,0 +7,5 @@\n")
        pypi = FakePyPI()

        issues, to_weigh = self.find(run_change(diff, files), pypi)

        self.assertEqual((issues, pypi.asked), ([], []))
        whence = f"from the index {evil}, not checked against PyPI"
        self.assertEqual([(f["path"], f["line"], f["severity"], f["rule"], f["message"]) for f in to_weigh], [
            ("pyproject.toml", 11, "warning", "source-changed",
             f"`requests`: at the base, from PyPI; in the change, {whence}."),
            ("uv.lock", None, "warning", "source-changed", f"`idna`: at the base, from PyPI; in the change, {whence}.")])

    def test_a_uv_source_of_a_shape_this_check_does_not_know_is_reported(self):
        new = pyproject_with("forked", "local") + textwrap.dedent("""\
            [tool.uv.sources]
            forked = { mirror = "https://example.com/forked", tag = "v1" }
            local = { mirror = "../local" }
        """)
        pypi = FakePyPI(forked=pypi_project(), local=pypi_project())
        issues, to_weigh = self.find(self.changed_manifests(pyproject_with(), new), pypi)
        self.assertEqual(pypi.asked, [])
        self.assertEqual([(f["rule"], f["message"]) for f in to_weigh], [
            ("unknown-source", ('`forked`, which the spec names: from the uv source { mirror = '
                                '"https://example.com/forked", tag = "v1" }, which this check does not know, so where '
                                'it comes from is not checked.'))])
        self.assertEqual([(f["rule"], f["message"]) for f in issues], [
            ("not-in-spec", ('`local` is a new dependency that the spec does not name; from the uv source { mirror = '
                             '"../local" }, which this check does not know, so where it comes from is not checked.'))])

    def test_a_default_index_replaces_pypi(self):
        new = pyproject_with("rich") + '[[tool.uv.index]]\nname = "mirror"\nurl = "https://mirror.example/simple"\n' \
                                       "default = true\n"
        pypi = FakePyPI(rich=pypi_project())
        issues, to_weigh = self.find(self.changed_manifests(pyproject_with(), new), pypi)
        self.assertEqual((issues, pypi.asked), ([], []))
        whence = "from the index https://mirror.example/simple, not checked against PyPI"
        # The default index takes every dependency, httpx too.
        self.assertEqual([(f["rule"], f["message"]) for f in to_weigh], [
            ("source-changed", f"`httpx`: at the base, from PyPI; in the change, {whence}."),
            ("other-index", f"`rich`, which the spec names: {whence}.")])

    def test_only_the_manifests_at_the_top_level_count(self):
        files = {("base", "tests/fixtures/x/pyproject.toml"): pyproject_with(),
                 ("new", "tests/fixtures/x/pyproject.toml"): pyproject_with("made-up-package"),
                 ("new", "tests/fixtures/bad/uv.lock"): "[[package\n"}
        counts = [orchestrator.ChangedFile(p, p, 1, 0) for p in ("tests/fixtures/x/pyproject.toml",
                                                               "tests/fixtures/bad/uv.lock")]
        pypi = FakePyPI()
        self.assertEqual(self.find(run_change("", files, counts=counts), pypi), ([], []))
        self.assertEqual(pypi.asked, [])

    def test_a_manifest_broken_at_the_base_and_left_alone_is_weighed_not_failed(self):
        broken = "[[package\n"
        files = {("base", "pyproject.toml"): pyproject_with(), ("new", "pyproject.toml"): pyproject_with("rich"),
                 ("base", "uv.lock"): broken, ("new", "uv.lock"): broken}
        pypi = FakePyPI()
        issues, to_weigh = self.find(run_change(pyproject_diff("rich"), files), pypi)
        self.assertEqual((issues, pypi.asked), ([], []))
        self.assertEqual([(f["path"], f["severity"], f["rule"]) for f in to_weigh],
                         [("uv.lock", "warning", "unreadable")])
        self.assertTrue(to_weigh[0]["message"].startswith("`uv.lock` cannot be read at the base: it is not TOML: "))

    def test_each_manifest_is_judged_by_its_own_base(self):
        broken = "[[package\n"
        files = {("base", "pyproject.toml"): pyproject_with(), ("new", "pyproject.toml"): "[project\n",
                 ("base", "uv.lock"): broken, ("new", "uv.lock"): broken}
        pypi = FakePyPI()
        issues, to_weigh = self.find(run_change(pyproject_diff("rich"), files), pypi)
        self.assertEqual([(f["path"], f["severity"], f["rule"]) for f in issues],
                         [("pyproject.toml", "error", "unreadable")])
        self.assertIn("cannot be read in the change", issues[0]["message"])
        self.assertEqual([(f["path"], f["severity"], f["rule"]) for f in to_weigh],
                         [("uv.lock", "warning", "unreadable")])
        self.assertEqual(pypi.asked, [])

    def test_a_manifest_the_change_breaks_fails_whatever_the_base(self):
        files = {("base", "pyproject.toml"): "[project\n", ("new", "pyproject.toml"): "[project\n",
                 ("base", "uv.lock"): None, ("new", "uv.lock"): "[[package\n"}
        issues, to_weigh = self.find(run_change(pyproject_diff("rich"), files), FakePyPI())
        self.assertEqual(([(f["path"], f["rule"]) for f in issues], [(f["path"], f["rule"]) for f in to_weigh]),
                         ([("uv.lock", "unreadable")], [("pyproject.toml", "unreadable")]))
        self.assertIn("cannot be read in the change", issues[0]["message"])

    def test_a_manifest_unreadable_at_the_base_is_weighed_not_failed(self):
        files = {("base", "pyproject.toml"): "[project\n", ("new", "pyproject.toml"): pyproject_with("rich")}
        pypi = FakePyPI()
        issues, to_weigh = self.find(run_change(pyproject_diff("rich"), files), pypi)
        self.assertEqual((issues, pypi.asked), ([], []))
        self.assertEqual([(f["path"], f["line"], f["severity"], f["rule"]) for f in to_weigh],
                         [("pyproject.toml", None, "warning", "unreadable")])
        self.assertTrue(to_weigh[0]["message"].startswith("`pyproject.toml` cannot be read at the base: it is not "
                                                          "TOML: "), to_weigh[0]["message"])
        self.assertTrue(to_weigh[0]["message"].endswith("; the change's new dependencies were not checked."))

    def test_a_manifest_that_cannot_be_read_fails_unchecked(self):
        files = {("base", "pyproject.toml"): pyproject_with(), ("new", "pyproject.toml"): "[project\n"}
        for text, why in (("[project\n", "it is not TOML"), ('[project]\ndependencies = "x"\n', "not a list"),
                          ('[project]\ndependencies = ["=x"]\n', "is not a requirement"),
                          # As Host.file_at gives a file that is not UTF-8: TOML must be.
                          (os.fsdecode(b'[project]\ndependencies = ["\xff"]\n'), "codec can't decode byte 0xff")):
            with self.subTest(why=why):
                files[("new", "pyproject.toml")] = text
                issues, to_weigh = self.find(run_change(pyproject_diff("x"), files), FakePyPI())
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in issues],
                                 [("pyproject.toml", None, "unreadable")])
                self.assertIn(why, issues[0]["message"])
                self.assertTrue(issues[0]["message"].startswith("`pyproject.toml` cannot be read in the change: "))
                self.assertTrue(issues[0]["message"].endswith("; the change's new dependencies were not checked."))

    def test_a_deleted_or_untouched_manifest_adds_nothing(self):
        diff = ("diff --git a/pyproject.toml b/pyproject.toml\ndeleted file mode 100644\n--- a/pyproject.toml\n"
                "+++ /dev/null\n@@ -1,5 +0,0 @@\n")
        pypi = FakePyPI()
        deleted = [orchestrator.ChangedFile("pyproject.toml", "pyproject.toml", 0, 5)]
        self.assertEqual(self.find(run_change(diff, {("base", "pyproject.toml"): pyproject_with("x")},
                                              counts=deleted), pypi), ([], []))
        self.assertEqual(self.find(run_change(new_file_diff("app.py", 3)), pypi), ([], []))
        self.assertEqual(pypi.asked, [])

    def test_a_manifest_moved_out_of_the_top_level_adds_nothing(self):
        moved = pyproject_with("requests", "rich")
        files = {("base", "pyproject.toml"): moved, ("new", "backend/pyproject.toml"): moved}
        pypi = FakePyPI()
        found = self.find(run_change("", files, counts=[
            orchestrator.ChangedFile("pyproject.toml", "backend/pyproject.toml", 0, 0)]), pypi)
        self.assertEqual((found, pypi.asked), (([], []), []))

    def test_a_manifest_moved_to_the_top_level_brings_no_base_of_its_own(self):
        fixture = pyproject_with("evilpkg")
        files = {("base", "tests/fixtures/pyproject.toml"): fixture, ("new", "pyproject.toml"): fixture}
        issues, _ = self.find(run_change("", files, counts=[
            orchestrator.ChangedFile("tests/fixtures/pyproject.toml", "pyproject.toml", 0, 0)]), FakePyPI())
        self.assertEqual([(f["path"], f["rule"], f["message"][:9]) for f in issues],
                         [("pyproject.toml", "not-in-spec", "`httpx` i"), ("pyproject.toml", "not-in-spec", "`evilpkg`")])

    def test_a_new_manifest_adds_all_it_names(self):
        files = {("new", "pyproject.toml"): pyproject_with()}
        issues, _ = self.find(run_change(new_file_diff("pyproject.toml", 6), files), FakePyPI(
            httpx=pypi_project()))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in issues], [("pyproject.toml", 4, "not-in-spec")])


class TestPyPI(unittest.TestCase):
    def project(self, *answers):
        urlopen = MagicMock(side_effect=list(answers))
        return orchestrator.PyPI(urlopen=urlopen).project("limits"), urlopen

    def test_reads_dates_version_and_license(self):
        project, urlopen = self.project(FakeResponse(json.dumps(pypi_project())))
        self.assertEqual(project.first_release.isoformat(), "2015-01-03T10:00:00.123456+00:00")
        self.assertEqual(project.latest_release.isoformat(), "2024-06-22T10:00:00.123456+00:00")
        self.assertEqual((project.version, project.license), ("3.13.0", "MIT"))
        (req,) = [c.args[0] for c in urlopen.call_args_list]
        self.assertEqual(req.full_url, PYPI_JSON.format("limits"))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], orchestrator.PYPI_TIMEOUT)

    def test_a_license_is_the_expression_then_the_field_then_the_classifier(self):
        cases = [(dict(license="MIT", expression="Apache-2.0"), "Apache-2.0"),
                 (dict(license="BSD\n\nlong text of the licence"), "BSD"),
                 (dict(license="", classifiers=["Topic :: X", "License :: OSI Approved :: MIT License"]),
                  "MIT License"),
                 (dict(license=None), None)]
        for kw, license in cases:
            with self.subTest(license=license):
                project, _ = self.project(FakeResponse(json.dumps(pypi_project(**kw))))
                self.assertEqual(project.license, license)

    def test_a_project_without_files_has_no_release_dates(self):
        body = {"info": {"version": "0.0.1"}, "releases": {"0.0.1": []}, "urls": []}
        project, _ = self.project(FakeResponse(json.dumps(body)))
        self.assertEqual((project.first_release, project.latest_release), (None, None))

    def test_an_unknown_project_is_none(self):
        project, _ = self.project(urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b"")))
        self.assertIsNone(project)

    def test_any_other_failure_raises(self):
        url = PYPI_JSON.format("limits")
        for answer, message in [
                (urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO(b"")), f"GET {url}: HTTP 503"),
                (urllib.error.URLError("[Errno -3] name resolution"), f"GET {url}: [Errno -3] name resolution"),
                (TimeoutError("timed out"), f"GET {url}: timed out"),
                (FakeResponse(b"<html>"), f"GET {url}: the answer is not PyPI's JSON"),
                (FakeResponse(b"[]"), f"GET {url}: the answer is not PyPI's JSON"),
                (FakeResponse(b'{"info": {}}'), f"GET {url}: the answer is not PyPI's JSON")]:
            with self.subTest(message=message):
                with self.assertRaises(orchestrator.PyPIError) as cm:
                    self.project(answer)
                self.assertTrue(str(cm.exception).startswith(message), str(cm.exception))


class TestDiffBudget(unittest.TestCase):
    def test_the_budget_counts_files_and_lines_against_the_workflows(self):
        counts = [orchestrator.ChangedFile("a.py", "a.py", 10, 0), orchestrator.ChangedFile("b.py", "b.py", 10, 0),
                  orchestrator.ChangedFile("old.py", "c.py", 1, 5)]
        budget = orchestrator.diff_budget
        self.assertEqual(budget(run_change("", counts=counts), max_files=3, max_lines=26), ([], []))
        (finding,) = budget(run_change("", counts=counts), max_files=2, max_lines=26).to_weigh
        self.assertEqual(finding["message"], "The change touches 3 files, over the workflow's max_files of 2. Say in "
                                             "your report why it cannot be smaller.")
        (finding,) = budget(run_change("", counts=counts), max_files=3, max_lines=25).to_weigh
        self.assertEqual(finding["message"], "The change touches 26 lines (21 added, 5 removed), over the workflow's "
                                             "max_lines of 25. Say in your report why it cannot be smaller.")

    def test_a_path_the_spec_mentions_by_path_or_name_is_expected(self):
        diff = new_file_diff("src/limiter.py", 1) + new_file_diff("tests/test_limiter.py", 1) + new_file_diff(
            "README.md", 1)
        spec = "Add src/limiter.py, tested in test_limiter.py."
        issues, to_weigh = orchestrator.unexpected_paths(run_change(diff, spec=spec))
        self.assertEqual(issues, [])
        self.assertEqual([f["path"] for f in to_weigh], ["README.md"])


class TestJudgeRun(unittest.TestCase):
    """judge_run adds the orchestrator checks' findings to a quality file the gate wrote."""

    def finding(self, rule, path="pyproject.toml"):
        return {"path": path, "line": 5, "severity": "error", "rule": rule, "message": "m"}

    def check(self, name, issues=(), to_weigh=()):
        return orchestrator.OrchestratorCheck(name, lambda change: orchestrator.CheckFindings(list(issues),
                                                                                            list(to_weigh)))

    def test_findings_follow_the_files_own_and_fail_it_as_a_condition(self):
        built = quality_report(
            "ERROR", "intro", conditions=["Coverage of new code is 50%."],
            issues=[self.finding("ruff F401", "a.py")], to_weigh=[self.finding("flaky-tests mixed-results", "t.py")],
            tests=[])
        checks = (self.check("new-dependencies", issues=[self.finding("not-in-spec")]),
                  self.check("diff-budget", to_weigh=[self.finding("", None)]))

        text = orchestrator.judge_run(built, checks, run_change(""))

        self.assertTrue(text.startswith("GATE: ERROR\n\nintro\n\n## Failed conditions\n\n- Coverage of new code is "
                                        "50%.\n- 1 findings of the `new-dependencies` check; each is listed below.\n"
                                        "\n## Issues on changed lines\n"), text)
        self.assertTrue(text.endswith(
            "## Failing tests\n\nNone.\n\n## The orchestrator's checks\n\n"
            "The orchestrator ran these itself after the build, with what the build does not see: the run's spec and "
            "its workflow.\n\n"
            "3. `pyproject.toml:5` error new-dependencies not-in-spec: m\n\n"
            "These do not fail the gate. Fix each, or say in your report why it should stand.\n\n"
            "4. The change as a whole: error diff-budget: m\n"), text)
        self.assertEqual(parse_gate(text), "ERROR")

    def test_a_failing_finding_turns_ok_into_error(self):
        built = quality_report("OK", "intro", tests=[])
        checks = (self.check("c", issues=[self.finding("r")]), self.check("d", issues=[self.finding("s")] * 2))
        text = orchestrator.judge_run(built, checks, run_change(""))
        self.assertTrue(text.startswith("GATE: ERROR\n\nintro\n\n## Failed conditions\n\n- 1 findings of the `c` "
                                        "check; each is listed below.\n- 2 findings of the `d` check; each is listed "
                                        "below.\n\n## Issues on changed lines\n\nNone.\n"), text)
        self.assertTrue(text.endswith("\n1. `pyproject.toml:5` error c r: m\n2. `pyproject.toml:5` error d s: m\n"
                                      "3. `pyproject.toml:5` error d s: m\n"), text)

    def test_nothing_found_leaves_the_file_as_it_is(self):
        built = quality_report("OK", "intro", tests=[])
        self.assertEqual(orchestrator.judge_run(built, (self.check("c"),), run_change("")), built)

    def test_at_most_fifty_are_listed(self):
        built = quality_report("OK", "intro")
        many = [self.finding("r")] * 60
        text = orchestrator.judge_run(built, (self.check("c", to_weigh=many),), run_change(""))
        self.assertIn("\n50. `pyproject.toml:5` error c r: m\n", text)
        self.assertNotIn("\n51. ", text)
        self.assertTrue(text.endswith("\n10 more findings of the orchestrator's checks are not listed.\n"))

    def test_the_limit_of_fifty_is_the_whole_files(self):
        checks = (self.check("c", issues=[self.finding("r")] * 2, to_weigh=[self.finding("w")] * 3),)
        # The build's file lists its first 50 of 60 issues, or all of its 48.
        for issues, shown in ((60, 0), (48, 2)):
            with self.subTest(issues=issues):
                built = quality_report("ERROR", "intro", issues=[self.finding("ruff F401", "a.py")] * issues)
                text = orchestrator.judge_run(built, checks, run_change(""))
                section = text[text.index("## The orchestrator's checks"):]
                self.assertEqual(re.findall(r"^(\d+)\. ", section, re.MULTILINE), [str(n) for n in range(49, 51)][:shown])
                self.assertTrue(section.endswith(f"\n{5 - shown} more findings of the orchestrator's checks are not "
                                                 "listed.\n"), section)
                self.assertIn("- 2 findings of the `c` check; each is listed below.", text)

    def test_checks_come_from_the_workflow(self):
        p = Pipeline("budget", QUICK.roles, QUICK.steps, max_files=3, max_lines=40)
        checks = orchestrator.orchestrator_checks(p, orchestrator.PyPI(urlopen=FakePyPI()))
        self.assertEqual([c.name for c in checks], ["new-dependencies", "diff-budget", "unexpected-paths"])
        (finding,) = checks[1].find(run_change(new_file_diff("README.md", 41))).to_weigh
        self.assertIn("over the workflow's max_lines of 40", finding["message"])


class TestQualityFileContract(unittest.TestCase):
    """The quality file is the one interface between the gate and the engine: what the gate's quality_report writes,
    over every section it can write, the engine's parse_gate and gate_failures read back, and judge_run's
    amendments leave a file they still read. Each side keeps its own copy of the format, which this checks agree."""

    def finding(self, path, line, rule, message="m", severity="error"):
        return {"path": path, "line": line, "severity": severity, "rule": rule, "message": message}

    def full_report(self, gate, n_issues=3):
        """A quality file with every section quality_report can write, and the inputs it was written from."""
        conditions = ["`ruff` is not installed: it is not on PATH.", "2 tests fail; each is listed below."]
        kinds = [("a.py", 3), ("b.py", None), (None, None)]
        issues = [self.finding(*kinds[k % 3], f"ruff F{k}", f"m{k} at 1{k}") for k in range(n_issues)]
        to_weigh = [self.finding("t.py", 9, "flaky-tests mixed-results", severity="warning"),
                    self.finding(None, None, "surviving-mutants budget-reached", severity="warning")]
        tests = [("t.T.test_a", "AssertionError: 1 != 2\ntraceback"), ("t.T.test_b", "")]
        text = quality_report(gate, "intro", conditions=conditions, issues=issues, to_weigh=to_weigh, elsewhere=4,
                              tests=tests, coverage="62.5% of 40 new lines to cover.",
                              suppressions=[("a.py", 3, "x = 1  # noqa: F401")],
                              edited=list(orchestrator.BUILD_CONFIG_FILES), rules_from="`main.toml`",
                              mutmut_state=["mutants/a.py.meta"])
        return text, conditions, issues, tests

    def expected_fingerprints(self, issues, tests):
        out: dict[str, list[str]] = {}
        for i in issues[:orchestrator.QUALITY_ISSUE_LIMIT]:
            out.setdefault(i["rule"].split(" ", 1)[0], []).append(
                orchestrator.finding_fingerprint(i["path"], i["rule"], i["message"]))
        out["tests"] = [orchestrator.finding_fingerprint(name, "tests", "") for name, _ in tests]
        return out

    def test_the_engine_reads_every_section_the_gate_writes(self):
        for gate, n_issues in ((orchestrator.GATE_OK, 0), (orchestrator.GATE_ERROR, 3),
                               (orchestrator.GATE_ERROR, orchestrator.QUALITY_ISSUE_LIMIT + 2)):
            with self.subTest(gate=gate, n_issues=n_issues):
                text, conditions, issues, tests = self.full_report(gate, n_issues)
                for heading in ("## Failed conditions", "## Issues on changed lines", "## Findings to weigh",
                                "## Failing tests", "## Coverage on new code", "## Suppressions added",
                                "## Build configuration"):
                    self.assertIn(f"\n{heading}\n", text)
                self.assertEqual(parse_gate(text), gate)
                failures = orchestrator.gate_failures(text)
                self.assertEqual(failures.conditions, conditions)
                self.assertEqual(failures.fingerprints, self.expected_fingerprints(issues, tests))

    def test_judge_run_s_amendments_leave_a_file_the_engine_reads(self):
        text, conditions, issues, tests = self.full_report(orchestrator.GATE_OK, 0)
        found = self.finding("pyproject.toml", 5, "not-in-spec", "names a package at 7")
        weigh = self.finding(None, None, "over-budget", severity="warning")
        check = orchestrator.OrchestratorCheck(
            "new-dependencies", lambda change: orchestrator.CheckFindings([found], [weigh]))
        amended = orchestrator.judge_run(text, (check,), run_change(""))
        self.assertEqual(parse_gate(amended), orchestrator.QUALITY_ERROR)
        failures = orchestrator.gate_failures(amended)
        self.assertEqual(failures.conditions,
                         [*conditions, "1 findings of the `new-dependencies` check; each is listed below."])
        expected = self.expected_fingerprints(issues, tests)
        expected["new-dependencies"] = [orchestrator.finding_fingerprint(
            "pyproject.toml", "new-dependencies not-in-spec", "names a package at 7")]
        self.assertEqual(failures.fingerprints, expected)

    def test_the_engine_s_copies_match_the_gate_s(self):
        self.assertEqual((orchestrator.QUALITY_OK, orchestrator.QUALITY_ERROR),
                         (orchestrator.GATE_OK, orchestrator.GATE_ERROR))
        self.assertEqual(orchestrator.QUALITY_FAILED_CONDITIONS, orchestrator.FAILED_CONDITIONS)
        self.assertEqual(orchestrator.QUALITY_FAILING_TESTS, orchestrator.FAILING_TESTS)
        self.assertEqual(orchestrator.QUALITY_TO_WEIGH_NOTE, orchestrator.TO_WEIGH_NOTE)
        self.assertEqual(orchestrator.QUALITY_FINDINGS_LIMIT, orchestrator.QUALITY_ISSUE_LIMIT)
        self.assertEqual(orchestrator._orchestrator_condition("c", 2), orchestrator._check_condition("c", 2))
        for path, line in (("a.py", 3), ("a.py", None), (None, None)):
            f = orchestrator._check_finding(path, line, "r", "m", severity="error")
            self.assertEqual(f, orchestrator._finding(path, line, "r", "m", severity="error"))
            self.assertEqual(orchestrator._numbered_finding(7, f), orchestrator._numbered(7, f))
        text = "x" * 200
        self.assertEqual(orchestrator._shortened(text), orchestrator._clip(text))
        self.assertEqual(orchestrator._shortened(text, 80), orchestrator._clip(text, 80))


class TestChangedLines(unittest.TestCase):
    def test_added_modified_and_deleted_files(self):
        diff = ("diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n"
                "+++ b/new.py\n@@ -0,0 +1,3 @@\n+a\n+b\n+c\n"
                "diff --git a/mod.py b/mod.py\nindex 1..2 100644\n--- a/mod.py\n+++ b/mod.py\n"
                "@@ -3 +3 @@ def f():\n-x\n+y\n@@ -10,2 +10,0 @@\n-p\n-q\n@@ -20,0 +18,2 @@\n+r\n+s\n"
                "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\nindex 1..0\n--- a/gone.py\n+++ /dev/null\n"
                "@@ -1 +0,0 @@\n-z\n")
        self.assertEqual(changed_lines(diff), {"new.py": {1, 2, 3}, "mod.py": {3, 18, 19}})

    def test_a_hunk_header_it_cannot_read_adds_no_lines(self):
        diff = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ garbled @@\n+x\n"
        self.assertEqual(changed_lines(diff), {"m.py": set()})

    def test_renames(self):
        diff = ("diff --git a/old name.py b/new name.py\nsimilarity index 100%\nrename from old name.py\n"
                "rename to new name.py\n"
                "diff --git a/a.py b/b.py\nsimilarity index 80%\nrename from a.py\nrename to b.py\n"
                "--- a/a.py\n+++ b/b.py\n@@ -4,0 +5 @@\n+added\n")
        self.assertEqual(changed_lines(diff), {"new name.py": set(), "b.py": {5}})

    def test_a_file_that_only_loses_lines_is_touched(self):
        diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -5,2 +4,0 @@\n-a\n-b\n"
        self.assertEqual(changed_lines(diff), {"f.py": set()})

    def test_empty_new_file_and_names_with_spaces(self):
        diff = ("diff --git a/my file.py b/my file.py\nnew file mode 100644\nindex 0000000..e69de29\n"
                "diff --git a/x y.py b/x y.py\n--- a/x y.py\t\n+++ b/x y.py\t\n@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual(changed_lines(diff), {"my file.py": set(), "x y.py": {1}})

    def test_quoted_names(self):
        diff = ('diff --git "a/tab\\there.py" "b/tab\\there.py"\n--- "a/tab\\there.py"\n+++ "b/tab\\there.py"\n'
                "@@ -0,0 +1 @@\n+x\n")
        self.assertEqual(changed_lines(diff), {"tab\there.py": {1}})

    def test_hunk_lines_that_look_like_headers(self):
        diff = ("diff --git a/f.md b/f.md\n--- a/f.md\n+++ b/f.md\n@@ -1,0 +2,3 @@\n"
                "+++ b/evil.md\n+--- a/x\n+rename to y\n")
        self.assertEqual(changed_lines(diff), {"f.md": {2, 3, 4}})

    def test_only_a_newline_ends_a_line(self):
        # Each of these ends a line for str.splitlines, which would read the rest of the added line as a header.
        for sep in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
            with self.subTest(sep=repr(sep)):
                diff = (f"diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -3,0 +4,3 @@\n"
                        f"+x = 1{sep}diff --git a/g.py b/g.py{sep}@@ -5,0 +6,2 @@\n+y\n+z\n")
                self.assertEqual(changed_lines(diff), {"f.py": {4, 5, 6}})

    def test_added_lines_the_engine_s_reads_each_diff_as_changed_lines_does(self):
        diffs = [
            "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -5,2 +4,0 @@\n-a\n-b\n",
            ("diff --git a/my file.py b/my file.py\nnew file mode 100644\nindex 0000000..e69de29\n"
             "diff --git a/x y.py b/x y.py\n--- a/x y.py\t\n+++ b/x y.py\t\n@@ -1 +1 @@\n-a\n+b\n"),
            ('diff --git "a/tab\\there.py" "b/tab\\there.py"\n--- "a/tab\\there.py"\n+++ "b/tab\\there.py"\n'
             "@@ -0,0 +1 @@\n+x\n"),
            "diff --git a/f.md b/f.md\n--- a/f.md\n+++ b/f.md\n@@ -1,0 +2,3 @@\n+++ b/evil.md\n+--- a/x\n+rename to y\n",
            ("diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n"
             "--- a/old.py\n+++ b/new.py\n@@ -2,0 +3 @@\n+z = 1\r\n"
             "diff --git a/uv.lock b/uv.lock\ndeleted file mode 100644\n--- a/uv.lock\n+++ /dev/null\n@@ -1 +0,0 @@\n"
             "-x\n"),
        ]
        for diff in diffs:
            with self.subTest(diff=diff):
                added = orchestrator.added_lines(diff)
                self.assertEqual({path: set(lines) for path, lines in added.items()}, changed_lines(diff))
        self.assertEqual(orchestrator.added_lines(diffs[3]),
                         {"f.md": {2: "++ b/evil.md", 3: "--- a/x", 4: "rename to y"}})
        self.assertEqual(orchestrator.added_lines(diffs[4]), {"new.py": {3: "z = 1\r"}})

    def test_build_config_edits(self):
        diff = ("diff --git a/Jenkinsfile b/Jenkinsfile\n--- a/Jenkinsfile\n+++ b/Jenkinsfile\n@@ -1 +1 @@\n-a\n+b\n"
                "diff --git a/uv.lock b/uv.lock\ndeleted file mode 100644\n"
                "--- a/uv.lock\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        self.assertEqual(orchestrator.edited_config(diff, changed_lines(diff)), ["Jenkinsfile", "uv.lock"])
        self.assertEqual(orchestrator.edited_config("", {}), [])


class TestBuildFailedReport(unittest.TestCase):
    """build_failed_report, the engine's quality file for a build that ended before its Gate stage."""

    def test_no_test_report_and_the_console(self):
        text = orchestrator.build_failed_report("intro", None, "last\nlines")
        self.assertTrue(text.startswith("GATE: BUILD_FAILED\n\nintro\n"))
        self.assertIn("## Failing tests\n\nJenkins has no test report for this build.\n", text)
        self.assertIn(f"## The last {orchestrator.CONSOLE_TAIL_LINES} lines of the build's console\n\n"
                      "````\nlast\nlines\n````\n", text)
        self.assertNotIn("Issues on changed lines", text)
        self.assertNotIn("Findings to weigh", text)

    def test_failing_tests_without_a_console(self):
        text = orchestrator.build_failed_report("intro", [("t.T.test_x", "AssertionError\ntraceback")], "")
        self.assertIn("## Failing tests\n\n- `t.T.test_x`: AssertionError\n", text)
        self.assertNotIn("console", text)
        self.assertIn("## Failing tests\n\nNone.\n", orchestrator.build_failed_report("intro", [], ""))

    def test_failing_tests_beyond_the_limit_are_counted(self):
        tests = [(f"t.T.test_{k}", "") for k in range(orchestrator.QUALITY_FINDINGS_LIMIT + 3)]
        text = orchestrator.build_failed_report("intro", tests, "")
        self.assertIn("- `t.T.test_0`: failed\n", text)
        self.assertIn("- and 3 more.\n", text)
        self.assertNotIn(f"test_{orchestrator.QUALITY_FINDINGS_LIMIT}`", text)


class TestQualityReport(unittest.TestCase):
    def test_the_fix_prompt_asks_for_findings_to_weigh_by_number(self):
        prompt = orchestrator.QUALITY_FIX_PROMPT
        self.assertIn("finding to weigh", prompt)
        self.assertIn("never fail the gate", prompt)

    def test_gate_line(self):
        self.assertEqual(parse_gate("\n**GATE: BUILD_FAILED**\n"), "BUILD_FAILED")
        self.assertEqual(parse_gate("GATE: OK"), "OK")
        self.assertIsNone(parse_gate("GATE: WARN"))
        self.assertIsNone(parse_gate("Findings\nGATE: OK"))
        self.assertIsNone(parse_gate(""))

    def issues(self, n):
        return [{"path": "f.py", "line": i, "severity": "HIGH", "rule": "r", "message": f"m{i}"} for i in range(1, n + 1)]

    def test_numbers_at_most_fifty_issues(self):
        text = quality_report("ERROR", "intro", issues=self.issues(60), elsewhere=10, tests=[])
        self.assertTrue(text.startswith("GATE: ERROR\n\nintro\n"))
        self.assertIn("50. `f.py:50` HIGH r: m50\n", text)
        self.assertNotIn("51. ", text)
        self.assertIn("10 more issues on changed lines are not listed", text)
        self.assertIn("10 other open issues in the project", text)

    def finding(self, path, line, rule="flaky test_x"):
        return {"path": path, "line": line, "severity": "warning", "rule": rule, "message": "m"}

    def test_findings_to_weigh_alone_leave_the_gate_ok(self):
        text = quality_report("OK", "intro", to_weigh=[self.finding("t.py", 3), self.finding("t.py", None),
                                                        self.finding(None, None, "mutants budget")])
        self.assertTrue(text.startswith("GATE: OK\n"))
        self.assertIn("## Issues on changed lines\n\nNone.\n", text)
        self.assertIn("## Findings to weigh\n\nThese do not fail the gate. Fix each, or say in your report why it "
                      "should stand.\n\n1. `t.py:3` warning flaky test_x: m\n"
                      "2. `t.py (the whole file)` warning flaky test_x: m\n"
                      "3. The change as a whole: warning mutants budget: m\n", text)

    def test_findings_to_weigh_are_numbered_after_the_issues(self):
        text = quality_report("ERROR", "intro", issues=self.issues(2), to_weigh=[self.finding("t.py", 9)])
        self.assertLess(text.index("2. `f.py:2` HIGH r: m2"), text.index("## Findings to weigh"))
        self.assertIn("3. `t.py:9` warning flaky test_x: m\n", text)

    def test_the_limit_takes_issues_first_then_findings_to_weigh(self):
        limit = orchestrator.QUALITY_ISSUE_LIMIT
        text = quality_report("ERROR", "intro", issues=self.issues(limit - 2),
                              to_weigh=[self.finding("t.py", n) for n in range(1, 6)])
        self.assertIn(f"{limit}. `t.py:2` ", text)
        self.assertNotIn(f"{limit + 1}. ", text)
        self.assertIn("3 more findings to weigh are not listed.", text)
        text = quality_report("ERROR", "intro", issues=self.issues(limit + 1), to_weigh=[self.finding("t.py", 1)])
        self.assertIn("## Findings to weigh\n\n1 findings to weigh are not listed; answer the issues above first.\n",
                      text)
        self.assertNotIn("`t.py:1`", text)

    def test_no_findings_to_weigh_no_section(self):
        self.assertNotIn("Findings to weigh", quality_report("OK", "intro"))
        self.assertNotIn("Findings to weigh", quality_report("ERROR", "intro", issues=self.issues(1)))

    def test_tests_and_coverage(self):
        # The console's end is the engine's to write (TestBuildFailedReport): the gate's file never has one.
        text = quality_report("OK", "intro", tests=[("t.T.test_x", "AssertionError\ntraceback")],
                              coverage="62.5% of 40 new lines to cover.")
        self.assertIn("- `t.T.test_x`: AssertionError\n", text)
        self.assertIn("## Coverage on new code\n\n62.5% of 40 new lines to cover.\n", text)
        self.assertIn("None.", text)
        self.assertNotIn("console", text)
        self.assertNotIn("Jenkins has no test report", text)
        self.assertNotIn("## Failing tests", quality_report("OK", "intro", tests=None))
        self.assertIn("## Failing tests\n\nNone.\n", quality_report("OK", "intro", tests=[]))

    def test_sections_left_out_by_default(self):
        text = quality_report("OK", "intro")
        self.assertNotIn("## Failing tests", text)
        self.assertNotIn("## Coverage on new code", text)
        self.assertNotIn("## Suppressions added", text)
        self.assertNotIn("## Failed conditions", text)

    def test_conditions_and_suppressions_are_listed(self):
        text = quality_report("ERROR", "intro", conditions=["`ruff` is not installed."],
                              suppressions=[("f.py", 3, "x = 1  # noqa: F401")])
        self.assertIn("## Failed conditions\n\n- `ruff` is not installed.\n", text)
        self.assertIn("## Suppressions added\n\n- `f.py:3` `x = 1  # noqa: F401`\n", text)

    def test_failing_tests_beyond_the_limit_are_counted(self):
        tests = [(f"t.T.test_{k}", "") for k in range(orchestrator.QUALITY_ISSUE_LIMIT + 3)]
        text = quality_report("ERROR", "intro", tests=tests)
        self.assertIn("- `t.T.test_0`: failed\n", text)
        self.assertIn("- and 3 more.\n", text)
        self.assertNotIn(f"test_{orchestrator.QUALITY_ISSUE_LIMIT}`", text)

    def test_build_config_edits_are_named(self):
        text = quality_report("OK", "i", tests=[], edited=["Jenkinsfile", "uv.lock"])
        self.assertIn("The change edits `Jenkinsfile`. The quality job reads it from `main`", text)
        for only, other in (("Jenkinsfile", "uv.lock"), ("uv.lock", "Jenkinsfile")):
            with self.subTest(only=only):
                text = quality_report("OK", "i", tests=[], edited=[only])
                self.assertIn(f"The change edits `{only}`", text)
                self.assertNotIn(f"The change edits `{other}`", text)

    def test_gate_config_edits_are_named(self):
        text = quality_report("OK", "i", edited=["pyproject.toml", "uv.lock"])
        self.assertIn("The change edits `pyproject.toml`, which holds the gate's rules; the change was judged by "
                      "the edited rules.", text)
        self.assertIn("The change edits `uv.lock`, which pins the versions of the linters the gate ran.", text)
        text = quality_report("OK", "i", edited=["pyproject.toml"], rules_from="`main`'s `pyproject.toml`")
        self.assertIn("The gate took its rules from `main`'s `pyproject.toml`, so the edit did not change", text)

    def test_files_under_mutmut_s_state_are_named_apart_from_build_config(self):
        text = quality_report("OK", "i", mutmut_state=["mutants/calc.py", "mutants/calc.py.meta"])
        self.assertIn("## Build configuration\n\n- The change adds or edits `mutants/calc.py` and "
                      "`mutants/calc.py.meta` under `mutants/`, where mutmut keeps its state. The gate's checks leave "
                      "them out: none selects a test or a function in them or counts them as a change to the code, "
                      "and the surviving-mutants check runs mutmut without them. The linters read them as they read "
                      "any tracked file.\n", text)
        self.assertNotIn("The change edits", text)

    def test_tokens_are_masked(self):
        ci = CI(JOB, env=CREDENTIALS)
        self.assertEqual(ci.mask(f"a {JENKINS_TOKEN} b {JENKINS_TOKEN}"), "a **** b ****")
        self.assertEqual(CI(JOB, env={}).mask("text"), "text")


class TestGate(unittest.TestCase):
    """`orchestrator.py gate` over a real git repository, with stub linters on PATH that print what each one does."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, self.bin, self.stubs = (os.path.join(tmp.name, d) for d in ("repo", "bin", "stubs"))
        for d in (self.repo, self.bin, self.stubs):
            os.mkdir(d)
        for tool in ("ruff", "mypy", "pymarkdown", "shellcheck", "actionlint"):
            self.stub(tool)
        env = patch.dict(os.environ, {"PATH": f"{self.bin}:{os.path.dirname(shutil.which('git'))}",
                                      "STUB_DIR": self.stubs})
        env.start()
        self.addCleanup(env.stop)
        self.host = Host()
        self.git("init", "--quiet", "-b", "main")
        for key, value in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, value)
        self.put("pyproject.toml", "[tool.ruff]\n")
        self.put("f.py", "".join(f"line{n} = {n}\n" for n in range(1, 4)))
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "base")

    def stub(self, tool):
        """A stub tool that records its arguments and prints $STUB_DIR/<tool>.out and .err, exiting with .status."""
        path = os.path.join(self.bin, tool)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nd="$STUB_DIR/{tool}"\nprintf "%s\\n" "$@" > "$d.argv"\n'
                    f'[ -f "$d.out" ] && cat "$d.out"\n[ -f "$d.err" ] && cat "$d.err" >&2\n'
                    f'exit "$(cat "$d.status" 2>/dev/null || echo 0)"\n')
        os.chmod(path, 0o700)

    def says(self, tool, out="", status=1, err=""):
        for ext, text in (("out", out), ("err", err), ("status", str(status))):
            with open(os.path.join(self.stubs, f"{tool}.{ext}"), "w") as f:
                f.write(text)

    def argv(self, tool):
        with open(os.path.join(self.stubs, f"{tool}.argv")) as f:
            return f.read().splitlines()

    def git(self, *args):
        return self.host.git(self.repo, *args)

    def put(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def gate(self, *args):
        """The exit status, standard output and standard error of `orchestrator.py gate --base HEAD ARGS` in the
        repo."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(["gate", "--base", "HEAD", *args])
        return status, out.getvalue(), err.getvalue()

    def add_lines(self, n, start=4):
        with open(os.path.join(self.repo, "f.py"), "a") as f:
            f.write("".join(f"new{k} = {k}\n" for k in range(start, start + n)))

    def test_ruff_reads_every_file_whatever_syntax_the_environment_gives_git_s_pathspecs(self):
        self.put("sub/g.py", "g = 1\n")
        self.git("add", "sub/g.py")
        self.add_lines(1)
        for name in ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS"):
            with self.subTest(name), patch.dict("os.environ", {name: "1"}):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(self.stubs, "ruff.argv"))
                self.gate()
                self.assertTrue({"f.py", "sub/g.py"} <= set(self.argv("ruff")), self.argv("ruff"))

    def test_a_clean_change_passes(self):
        self.add_lines(1)
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("GATE: OK\n"))
        self.assertNotIn("## Failed conditions", out)

    def test_only_findings_on_added_lines_count(self):
        self.add_lines(1)
        self.says("ruff", "f.py:2:1: F401 `os` imported but unused\n"
                          "f.py:4:7: B023 Function definition does not bind loop variable `x`\nFound 2 errors.\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertTrue(out.startswith("GATE: ERROR\n"))
        self.assertIn("- 1 findings on lines the change added; each is listed below.\n", out)
        self.assertIn("1. `f.py:4` error ruff B023: Function definition does not bind loop variable `x`\n", out)
        self.assertNotIn("f.py:2", out)
        self.assertIn("1 other open issues in the project", out)

    def test_a_finding_on_an_old_line_alone_passes(self):
        self.add_lines(1)
        self.says("ruff", "f.py:2:1: F401 `os` imported but unused\n")
        self.assertEqual(self.gate()[0], 0)

    def test_a_missing_tool_fails_naming_it(self):
        self.put("run.sh", "#!/bin/sh\n")
        self.git("add", "run.sh")
        os.remove(os.path.join(self.bin, "shellcheck"))
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `shellcheck` is not installed: it is not on PATH.\n", out)

    def test_only_tracked_files_are_linted(self):
        self.put("run.sh", "#!/bin/sh\n")
        os.remove(os.path.join(self.bin, "shellcheck"))
        self.assertEqual(self.gate()[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.stubs, "actionlint.argv")))

    def test_a_failing_or_unreadable_tool_fails_the_gate(self):
        self.put("README.md", "# r\n")
        self.git("add", "README.md")
        self.says("ruff", status=2, err="ruff failed\n  Cause: invalid config\n")
        self.says("pymarkdown", "WARNING:pymarkdown.main:Specified configuration file does not exist.\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `ruff` failed with exit status 2: ruff failed\n", out)
        self.assertIn("- `pymarkdown` reported findings, but none the gate could read: WARNING:pymarkdown", out)

    def test_a_line_that_is_not_utf8_counts_like_any_other(self):
        with open(os.path.join(self.repo, "f.py"), "ab") as f:
            f.write("name = 'Größe'\n".encode("latin-1"))
        self.says("ruff", "f.py:4:1: E501 Line too long (130 > 120)\n")
        status, out, _ = self.gate("--out", "quality.md")
        self.assertEqual(status, 1)
        with open(os.path.join(self.repo, "quality.md"), encoding="utf-8") as f:
            self.assertIn("1. `f.py:4` error ruff E501: Line too long (130 > 120)\n", f.read())

    def test_an_added_binary_file_is_judged(self):
        # git diffs the first as text, since it has no NUL byte, and the second as binary.
        for name, raw in (("logo.png", b"\x89PNG\r\n\x1a\n\xff\xd8\xff\xe0\n"), ("blob.bin", b"\x00\xff\xfe\n")):
            with open(os.path.join(self.repo, name), "wb") as f:
                f.write(raw)
            self.git("add", name)
        status, _, err = self.gate("--out", "quality.md")
        self.assertEqual((status, err), (0, "GATE: OK, in quality.md\n"))

    def test_a_carriage_return_in_an_added_line_does_not_end_it(self):
        with open(os.path.join(self.repo, "f.py"), "ab") as f:
            f.write(b"x = 1\rdiff --git a/g.py b/g.py\r@@ -5,0 +6,2 @@\ny = 2\nz = 3\n")
        base = self.git("rev-parse", "HEAD").strip()
        self.assertEqual(changed_lines(self.host.change_diff(self.repo, base)), {"f.py": {4, 5, 6}})

    def test_a_carriage_return_inside_an_added_python_line_fails_the_gate(self):
        # Python ends a line at the lone \r and git does not, so ruff's line 6 is git's line 5, the second added
        # line. Without the condition, the finding sits on no added line and the gate passes. A \r\n is one end.
        with open(os.path.join(self.repo, "f.py"), "ab") as f:
            f.write(b"crlf = 1\r\npass\rimport os\n")
        self.says("ruff", "f.py:6:8: F401 [*] `os` imported but unused\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `f.py:5` holds `\\r`, which a linter that reads the file ends a line at", out)
        self.assertNotIn("`f.py:4`", out)
        with self.assertRaisesRegex(orchestrator.GateError, "cannot read gone.py, which the change edits"):
            orchestrator.stray_line_ends(self.repo, {"gone.py": {1}}, [(orchestrator.GATE_TOOLS[0], ["gone.py"])])

    def test_a_carriage_return_in_a_file_no_linter_reads_passes(self):
        self.put("notes.txt", "a\rb\n")
        with open(os.path.join(self.repo, "data.bin"), "wb") as f:
            f.write(b"\x01\r\x02\n")
        self.git("add", "notes.txt", "data.bin")
        self.git("commit", "--quiet", "-m", "carriage returns")
        self.put("notes.txt", "a\rb\nc\n")
        with open(os.path.join(self.repo, "data.bin"), "ab") as f:
            f.write(b"\x03\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertNotIn("`notes.txt:", out)
        self.assertNotIn("`data.bin:", out)

    def test_a_carriage_return_above_an_added_line_fails_the_gate_in_any_file_a_linter_reads(self):
        # The \r lines are the base's, and d.md is pymarkdown's: each linter's line after them is one past git's.
        self.put("f.py", "a = 1\rb = 2\n")
        self.put("d.md", "# T\n\nok\r more text\n")
        self.git("add", "d.md")
        self.git("commit", "--quiet", "-am", "carriage returns")
        self.put("f.py", "a = 1\rb = 2\nimport os\n")
        self.put("d.md", "# T\n\nok\r more text\n\n# T\n")
        self.says("ruff", "f.py:3:8: F401 [*] `os` imported but unused\n")
        self.says("pymarkdown", "d.md:6:1: MD025: Multiple top-level headings in the same document\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `f.py:1` holds `\\r`, which a linter that reads the file ends a line at", out)
        self.assertIn("- `d.md:3` holds `\\r`, which a linter that reads the file ends a line at", out)

    def test_a_line_separator_above_an_added_line_fails_the_gate_only_where_a_linter_ends_a_line_at_it(self):
        # actionlint's YAML parser ends a line at U+2028, so its line 9 is git's line 8; ruff numbers like git.
        workflow = ("on: push\njobs:\n  j:\n    runs-on: x\n    steps:\n      # c\u2028# d\n"
                    "      - run: echo hi\n")
        self.put(".github/workflows/w.yml", workflow)
        self.put("f.py", "# c\u2028# d\n")
        self.git("add", ".github")
        self.git("commit", "--quiet", "-am", "line separators")
        self.put(".github/workflows/w.yml", workflow + "      - run: echo ${{ github.nope }}\n")
        self.put("f.py", "# c\u2028# d\nx = 1\n")
        self.says("actionlint", '.github/workflows/w.yml:9:23: property "nope" is not defined [expression]\n')
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `.github/workflows/w.yml:6` holds `\\u2028`, which a linter that reads the file ends a "
                      "line at", out)
        self.assertNotIn("`f.py:", out)

    def test_a_suppression_after_a_form_feed_is_listed_at_gits_line(self):
        self.put("f.py", "a = 1\x0c\n")
        self.git("commit", "--quiet", "-am", "form feed")
        self.put("f.py", "a = 1\x0c\nimport os  # noqa: F401\n")
        self.assertIn("- `f.py:2` `import os  # noqa: F401`", self.gate()[1])

    def test_a_symlink_to_a_directory_is_judged(self):
        self.put("pkg/m.py", "x = 1\n")
        self.git("add", "pkg")
        self.git("commit", "--quiet", "-m", "pkg")
        os.symlink("pkg", os.path.join(self.repo, "lib.py"))
        self.git("add", "lib.py")
        status, out, _ = self.gate()
        self.assertIn(status, (0, 1))
        self.assertTrue(out.startswith("GATE: "))

    def test_a_submodule_is_judged(self):
        os.mkdir(os.path.join(self.repo, "sub.py"))
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.git('rev-parse', 'HEAD').strip()},sub.py")
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("GATE: OK\n"))

    def test_a_symlink_is_read_as_git_diffs_it_its_target_name(self):
        self.put("target.py", "x = 1  # noqa: E501\n")
        self.git("add", "target.py")
        self.git("commit", "--quiet", "-m", "target")
        os.symlink("target.py", os.path.join(self.repo, "link.py"))
        self.git("add", "link.py")
        self.assertNotIn("## Suppressions added", self.gate()[1])

    def test_a_file_name_that_holds_a_line_separator_keeps_its_findings(self):
        # Each ends a line for str.splitlines, and the \r one for text mode as well; the tool's line ends at "\n".
        names = ["a\x85b.py", "a b.py", "a\x1cb.py", "a\rb.py"]
        for name in names:
            self.put(name, "x = 1\n")
            self.git("add", name)
        self.says("ruff", "".join(f"{name}:1:1: F401 `os` imported but unused\n" for name in names))
        status, _, _ = self.gate("--out", "quality.md")
        self.assertEqual(status, 1)
        with open(os.path.join(self.repo, "quality.md"), encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertIn("- 4 findings on lines the change added", text)
        for name in names:
            self.assertIn(f"`{name}:1` error ruff F401", text)

    def test_a_file_name_that_is_not_utf8_is_judged(self):
        name = os.fsdecode(b"f\xe9.py")
        self.put(name, "a = 1\n")
        self.git("add", name)
        self.git("commit", "--quiet", "-m", "latin-1 name")
        with open(os.path.join(self.repo, name), "a") as f:
            f.write("b = 2  # noqa: E501\n")
        # ruff prints U+FFFD for each byte of the name that is not UTF-8; a tool may also print the bytes themselves.
        for printed in (b"f\xe9.py", "f�.py".encode()):
            with self.subTest(printed=printed):
                with open(os.path.join(self.stubs, "ruff.out"), "wb") as f:
                    f.write(printed + b":2:1: E501 Line too long (130 > 120)\n")
                status, _, _ = self.gate("--out", "quality.md")
                self.assertEqual(status, 1)
                # Strict UTF-8: the name's undecodable byte is written as an escape.
                with open(os.path.join(self.repo, "quality.md"), encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("1. `f\\xe9.py:2` error ruff E501: Line too long (130 > 120)\n", text)
                self.assertNotIn("none the gate could read", text)
                self.assertIn("f\\xe9.py:2", text.split("## Suppressions", 1)[1])

    def test_an_unresolvable_base_writes_nothing(self):
        out = os.path.join(self.repo, "quality.md")
        status, stdout, err = self.gate("--base", "no-such-rev", "--out", out)
        self.assertEqual(status, orchestrator.EXIT_GATE_UNJUDGED)
        self.assertIn("no-such-rev names no commit", err)
        self.assertEqual(stdout, "")
        self.assertFalse(os.path.exists(out))

    def test_outside_a_git_tree_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["gate", "--base", "HEAD"]), orchestrator.EXIT_GATE_UNJUDGED)
        self.assertIn("is not in a git work tree", err.getvalue())

    def test_out_gets_the_file_and_stderr_the_gate(self):
        self.add_lines(1)
        status, stdout, err = self.gate("--out", "quality.md")
        self.assertEqual((status, stdout), (0, ""))
        self.assertEqual(err, "GATE: OK, in quality.md\n")
        with open(os.path.join(self.repo, "quality.md")) as f:
            self.assertTrue(f.read().startswith("GATE: OK\n"))

    def coverage(self, lines):
        """A Cobertura report of f.py: lines maps a line number to its hits, or to (hits, "covered/total" branches)."""
        rows = "".join(f'<line number="{n}" hits="{h[0] if isinstance(h, tuple) else h}"'
                       + (f' branch="true" condition-coverage="50% ({h[1]})"' if isinstance(h, tuple) else "")
                       + "/>" for n, h in lines.items())
        self.put("coverage.xml", f'<?xml version="1.0" ?><coverage><sources><source>{self.repo}</source></sources>'
                                 f'<packages><package name="."><classes><class name="f.py" filename="f.py"><lines>'
                                 f'{rows}</lines></class></classes></package></packages></coverage>')
        return os.path.join(self.repo, "coverage.xml")

    def test_coverage_below_the_minimum_fails_from_twenty_new_lines(self):
        self.add_lines(20)
        xml = self.coverage({n: int(n < 14) for n in range(1, 24)})
        status, out, _ = self.gate("--coverage", xml)
        self.assertEqual(status, 1)
        self.assertIn("- Coverage of new code is 50.0%; the gate wants at least 80% once a change has 20 or more", out)
        self.assertIn("50.0% of 20 new lines to cover; 10 lines are not covered.", out)

    def test_coverage_is_not_judged_on_nineteen_new_lines(self):
        self.add_lines(19)
        status, out, _ = self.gate("--coverage", self.coverage({n: 0 for n in range(1, 23)}))
        self.assertEqual(status, 0)
        self.assertIn("0.0% of 19 new lines to cover; 19 lines are not covered.", out)

    def test_coverage_counts_branch_outcomes_too(self):
        self.add_lines(2)
        _, out, _ = self.gate("--coverage", self.coverage({4: (1, "1/2"), 5: 0, 2: 1}))
        # (1 covered condition + 1 covered line) / (2 conditions + 2 lines); line 2 is not new.
        self.assertIn("50.0% of 2 new lines to cover, counting their 2 branch outcomes; 1 lines are not covered.", out)

    def test_an_unreadable_coverage_report_writes_nothing(self):
        self.add_lines(1)
        self.put("coverage.xml", "<coverage")
        status, stdout, err = self.gate("--coverage", "coverage.xml")
        self.assertEqual((status, stdout), (orchestrator.EXIT_GATE_UNJUDGED, ""))
        self.assertIn("cannot read the coverage report", err)

    def junit(self, *failing):
        """A pytest --junitxml report with one passing test and each of failing as (name, message)."""
        cases = "".join(f'<testcase classname="tests.T" name="{n}"><failure message="{m}">trace</failure></testcase>'
                        for n, m in failing)
        self.put("results.xml", f'<?xml version="1.0"?><testsuites><testsuite name="pytest">'
                                f'<testcase classname="tests.T" name="test_ok"/>{cases}</testsuite></testsuites>')
        return "results.xml"

    def test_failing_tests_fail_the_gate_and_are_listed(self):
        self.add_lines(1)
        status, out, _ = self.gate("--tests", self.junit(("test_limit", "AssertionError: 3 != 2")))
        self.assertEqual(status, 1)
        self.assertIn("- 1 tests fail; each is listed below.\n", out)
        self.assertIn("## Failing tests\n\n- `tests.T.test_limit`: AssertionError: 3 != 2\n", out)

    def test_passing_tests_are_said_to_pass(self):
        self.add_lines(1)
        status, out, _ = self.gate("--tests", self.junit())
        self.assertEqual(status, 0)
        self.assertIn("## Failing tests\n\nNone.\n", out)

    def test_an_unreadable_test_report_writes_nothing(self):
        self.put("results.xml", "<testsuites")
        status, stdout, err = self.gate("--tests", "results.xml")
        self.assertEqual((status, stdout), (orchestrator.EXIT_GATE_UNJUDGED, ""))
        self.assertIn("cannot read the test report", err)

    def test_added_suppressions_are_listed_not_failed(self):
        with open(os.path.join(self.repo, "f.py"), "a") as f:
            f.write("import os  # noqa: F401\n")
        self.put("run.sh", "# shellcheck disable=SC2086\n")
        self.git("add", "run.sh")
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertIn("## Suppressions added\n\n- `f.py:4` `import os  # noqa: F401`\n"
                      "- `run.sh:1` `# shellcheck disable=SC2086`\n", out)

    def checks(self, *checks):
        """Run the gate with checks in place of gate_checks's."""
        p = patch.object(orchestrator, "gate_checks", lambda mutation_budget, base_run_budget: checks)
        p.start()
        self.addCleanup(p.stop)

    def test_a_failing_check_fails_the_gate_wherever_its_findings_are(self):
        seen = []

        def find(change):
            seen.append(change)
            # Line 2 is not one the change added; a check's findings are listed anyway.
            return [{"path": "f.py", "line": 2, "severity": "error", "rule": "removed-test", "message": "test_a"},
                    {"path": None, "line": None, "severity": "error", "rule": "", "message": "could not run"}]
        self.checks(orchestrator.GateCheck("integrity", fails=True, find=find))
        self.add_lines(1)
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- 2 findings of the `integrity` check; each is listed below.\n", out)
        self.assertIn("1. `f.py:2` error integrity removed-test: test_a\n"
                      "2. The change as a whole: error integrity: could not run\n", out)
        [change] = seen
        self.assertEqual((change.root, change.base, change.changed),
                         (os.path.realpath(self.repo), self.git("rev-parse", "HEAD").strip(), {"f.py": {4}}))
        self.assertIn("+new4 = 4", change.diff)

    def test_a_reporting_check_is_weighed_after_the_issues_and_passes(self):
        self.checks(orchestrator.GateCheck("flaky", fails=False, find=lambda change: [
            {"path": "f.py", "line": 4, "severity": "warning", "rule": "seeds 3 5", "message": "test_b"}]))
        self.add_lines(1)
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertIn("## Findings to weigh\n\nThese do not fail the gate. Fix each, or say in your report why it "
                      "should stand.\n\n1. `f.py:4` warning flaky seeds 3 5: test_b\n", out)
        self.says("ruff", "f.py:4:1: F401 `os` imported but unused\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("1. `f.py:4` error ruff F401: `os` imported but unused\n", out)
        self.assertIn("2. `f.py:4` warning flaky seeds 3 5: test_b\n", out)

    def test_a_removed_test_fails_the_gate(self):
        self.put("tests/test_f.py", "def test_one():\n    assert 1\n\n\ndef test_two():\n    assert 2\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "tests")
        self.put("tests/test_f.py", "def test_one():\n    assert 1\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- 1 findings of the `test-integrity` check; each is listed below.\n", out)
        self.assertIn("1. `tests/test_f.py:5` error test-integrity removed-test: ", out)

    def test_a_check_with_nothing_to_say_adds_nothing(self):
        self.checks(orchestrator.GateCheck("quiet", fails=True, find=lambda change: []),
                    orchestrator.GateCheck("hush", fails=False, find=lambda change: []))
        self.add_lines(1)
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertNotIn("quiet", out)
        self.assertNotIn("Findings to weigh", out)

    def test_mutation_budget_bounds_the_surviving_mutants_check(self):
        budgets = []

        def find(change, budget):
            budgets.append(budget)
            return []
        self.add_lines(1)
        with patch.object(orchestrator, "surviving_mutants", find):
            self.gate()
            self.gate("--mutation-budget", "45")
        self.assertEqual(budgets, [orchestrator.MUTATION_BUDGET, 45])
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            self.gate("--mutation-budget", "0")

    def test_the_checks_are_told_whose_rules_judge_the_change(self):
        seen = []
        self.checks(orchestrator.GateCheck("rules", fails=False, find=lambda change: seen.append(change.rules) or []))
        main_rules = os.path.join(self.stubs, "main.toml")
        with open(main_rules, "w") as f:
            f.write("[tool.ruff]\n")
        self.gate()
        self.gate("--config", main_rules)
        self.assertEqual(seen, [os.path.join(self.repo, "pyproject.toml"), main_rules])

    def test_config_replaces_the_tree_s_rules(self):
        self.put("pyproject.toml", "[tool.ruff]\nline-length = 1\n")
        self.gate()
        self.assertIn(os.path.join(self.repo, "pyproject.toml"), self.argv("ruff"))
        main_rules = os.path.join(self.stubs, "main.toml")
        with open(main_rules, "w") as f:
            f.write("[tool.ruff]\n")
        _, out, _ = self.gate("--config", main_rules)
        for tool in ("ruff", "mypy", "pymarkdown"):
            with self.subTest(tool=tool):
                if tool != "pymarkdown":
                    self.assertIn(main_rules, self.argv(tool))
        self.assertIn(f"by the rules in `{main_rules}`.", out)
        self.assertIn(f"The gate took its rules from `{main_rules}`, so the edit did not change", out)

    def test_no_rules_file_writes_nothing(self):
        status, _, err = self.gate("--config", "missing.toml")
        self.assertEqual(status, orchestrator.EXIT_GATE_UNJUDGED)
        self.assertIn("is not a file", err)


class TestWeakenedTests(unittest.TestCase):
    """The test-integrity check over a real git repository: a base commit, then the change in its working tree."""

    BASE = {
        "tests/test_calc.py": (
            "import unittest\n"                                # 1
            "\n"                                               # 2
            "from calc import add\n"                           # 3
            "\n"                                               # 4
            "\n"                                               # 5
            "class TestAdd(unittest.TestCase):\n"              # 6
            "    def test_small(self):\n"                      # 7
            "        self.assertEqual(add(1, 2), 3)\n"         # 8
            "        self.assertEqual(add(0, 0), 0)\n"         # 9
            "\n"                                               # 10
            "    def test_large(self):\n"                      # 11
            "        assert add(10, 20) == 30\n"               # 12
            "\n"                                               # 13
            "\n"                                               # 14
            "class TestNegative(unittest.TestCase):\n"         # 15
            "    def test_one(self):\n"                        # 16
            "        assert add(-1, -1) == -2\n"               # 17
            "\n"                                               # 18
            "    def test_two(self):\n"                        # 19
            "        with self.assertRaises(TypeError):\n"     # 20
            "            add(None, 1)\n"                       # 21
            "\n"                                               # 22
            "\n"                                               # 23
            "def test_function():\n"                           # 24
            "    assert add(2, 2) == 4\n"                      # 25
        ),
        "tests/test_other.py": "def test_a():\n    assert True\n\n\ndef test_b():\n    assert 1\n",
        "calc.py": "def add(a, b):\n    return a + b\n",
    }

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.host = Host()
        self.git("init", "--quiet", "-b", "main")
        for key, value in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, value)
        for name, text in self.BASE.items():
            self.put(name, text)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "base")

    def git(self, *args):
        return self.host.git(self.repo, *args)

    def put(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def edit(self, name, old, new):
        with open(os.path.join(self.repo, name)) as f:
            text = f.read()
        self.assertEqual(text.count(old), 1, old)
        self.put(name, text.replace(old, new))

    def found(self):
        """(path, line, rule) of each finding on the working tree's change, and the findings themselves."""
        base = self.git("rev-parse", "HEAD").strip()
        found = orchestrator.weakened_tests(orchestrator.GateChange.from_tree(self.repo, base))
        return [(f["path"], f["line"], f["rule"]) for f in found], found

    def test_an_unchanged_tree_has_no_findings(self):
        self.assertEqual(self.found()[0], [])

    def test_a_removed_test_is_found_at_its_line_at_the_base(self):
        self.edit("tests/test_calc.py", "    def test_large(self):\n        assert add(10, 20) == 30\n\n", "")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 11, "removed-test")])
        self.assertIn("`TestAdd.test_large`", found[0]["message"])
        self.assertIn("line 11 at the base", found[0]["message"])

    def test_a_removed_class_is_one_finding_with_its_tests(self):
        self.edit("tests/test_calc.py", self.BASE["tests/test_calc.py"][
            self.BASE["tests/test_calc.py"].index("class TestNegative"):
            self.BASE["tests/test_calc.py"].index("def test_function")], "")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 15, "removed-test")])
        self.assertIn("`TestNegative` and its 2 tests", found[0]["message"])

    def test_a_deleted_test_file_is_one_finding(self):
        os.remove(os.path.join(self.repo, "tests/test_other.py"))
        where, found = self.found()
        self.assertEqual(where, [("tests/test_other.py", None, "removed-test")])
        self.assertIn("2 tests", found[0]["message"])

    def test_a_renamed_test_with_the_same_body_is_not_removed(self):
        self.edit("tests/test_calc.py", "def test_large(self):", "def test_big(self):")
        self.assertEqual(self.found()[0], [])

    def test_a_renamed_test_deeper_than_python_s_recursion_limit_is_matched(self):
        # A sum of 3000 terms parses, but a recursive walk of it, as ast.dump makes, overflows the stack.
        deep = "def test_deep():\n    assert " + " + ".join(["1"] * 3000) + " == 3000\n"
        self.put("tests/test_deep.py", deep)
        self.git("add", "tests/test_deep.py")
        self.git("commit", "--quiet", "-m", "deep")
        self.edit("tests/test_deep.py", "def test_deep():", "def test_deeper():")
        self.assertEqual(self.found()[0], [])

    def test_a_test_deeper_than_python_s_recursion_limit_is_compared_with_its_base(self):
        # Changing a literal while the code changes too compares the test's assertions by their shape.
        deep = "def test_deep():\n    x = 1\n    assert x + " + " + ".join(["1"] * 3000) + " == 3001\n"
        self.put("tests/test_deep.py", deep)
        self.git("add", "tests/test_deep.py")
        self.git("commit", "--quiet", "-m", "deep")
        self.edit("tests/test_deep.py", "    x = 1\n", "    x = 2\n")
        self.edit("calc.py", "return a + b", "return b + a")
        self.assertEqual(self.found()[0], [])

    def test_a_deep_assertion_whose_expected_value_changes_is_reported_without_its_text(self):
        deep = "def test_deep():\n    assert " + " + ".join(["1"] * 3000) + " == 3001\n"
        self.put("tests/test_deep.py", deep)
        self.git("add", "tests/test_deep.py")
        self.git("commit", "--quiet", "-m", "deep")
        self.edit("tests/test_deep.py", "== 3001", "== 3002")
        self.edit("calc.py", "return a + b", "return b + a")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_deep.py", 2, "expected-value-changed")])
        self.assertIn("too deep to print", found[0]["message"])

    def test_an_assertion_with_an_int_too_long_to_print_is_reported_without_its_text(self):
        # ast.unparse writes an int with repr, which refuses one of more than 4300 decimal digits.
        self.put("tests/test_big.py", "def test_big():\n    assert 1 == 0x" + "f" * 4000 + "\n")
        self.git("add", "tests/test_big.py")
        self.git("commit", "--quiet", "-m", "big")
        self.edit("tests/test_big.py", "assert 1 ==", "assert 2 ==")
        self.edit("calc.py", "return a + b", "return b + a")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_big.py", 2, "expected-value-changed")])
        self.assertIn("(code at line 2, too long to print)", found[0]["message"])

    def test_tests_moved_to_a_renamed_file_are_not_removed(self):
        self.git("mv", "tests/test_other.py", "tests/test_moved.py")
        self.edit("tests/test_moved.py", "def test_b():", "def test_bee():")
        self.assertEqual(self.found()[0], [])

    def test_tests_moved_into_another_file_are_not_removed(self):
        os.remove(os.path.join(self.repo, "tests/test_other.py"))
        self.put("tests/test_new.py", "def test_a():\n    assert True\n\n\ndef test_b():\n    assert 1\n")
        self.git("add", "-A")
        self.assertEqual(self.found()[0], [])

    def test_added_skips_are_found_at_their_lines(self):
        self.edit("tests/test_calc.py", "    def test_small(self):\n",
                  "    @unittest.skip('later')\n    def test_small(self):\n")                     # line 7
        self.edit("tests/test_calc.py", "    def test_one(self):\n        assert",
                  "    def test_one(self):\n        self.skipTest('flaky')\n        assert")     # line 18
        self.edit("tests/test_other.py", "def test_b():\n",
                  "import pytest\n\n\n@pytest.mark.xfail\ndef test_b():\n    pytest.skip('no')\n")
        where, _ = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 7, "skip"), ("tests/test_calc.py", 18, "skip"),
                                 ("tests/test_other.py", 8, "skip"), ("tests/test_other.py", 10, "skip")])

    def test_skips_already_at_the_base_are_not_found(self):
        self.put("tests/test_skipped.py", "import pytest\n\n\n@pytest.mark.skipif(True, reason='x')\n"
                                          "def test_x():\n    assert 1\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "skipped")
        self.edit("tests/test_skipped.py", "assert 1", "assert 2")
        self.assertEqual(self.found()[0], [])

    def test_fewer_assertions_are_found_at_the_test(self):
        self.edit("tests/test_calc.py", "        self.assertEqual(add(0, 0), 0)\n", "")
        self.edit("tests/test_calc.py", "        with self.assertRaises(TypeError):\n            add(None, 1)\n",
                  "        add(None, 1)\n")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 7, "fewer-assertions"),
                                 ("tests/test_calc.py", 18, "fewer-assertions")])
        self.assertIn("`TestAdd.test_small` has 1 assertions, 2 at the base", found[0]["message"])

    def test_more_assertions_and_new_tests_are_not_found(self):
        self.edit("tests/test_calc.py", "        assert add(10, 20) == 30\n",
                  "        assert add(10, 20) == 30\n        assert add(20, 10) == 30\n")
        self.edit("tests/test_other.py", "def test_a():", "def test_new():\n    assert 3\n\n\ndef test_a():")
        self.assertEqual(self.found()[0], [])

    def test_swallowed_errors_are_found(self):
        self.edit("tests/test_calc.py", "        assert add(-1, -1) == -2\n",
                  "        try:\n            assert add(-1, -1) == -2\n        except:\n            pass\n")
        self.edit("tests/test_calc.py", "    assert add(2, 2) == 4\n",
                  "    try:\n        assert add(2, 2) == 4\n    except Exception:\n        pass\n"
                  "    try:\n        add(1, 1)\n    except ValueError:\n        pass\n")
        where, _ = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 19, "swallowed-error"),
                                 ("tests/test_calc.py", 30, "swallowed-error")])

    def test_an_expected_value_changed_with_the_code_is_reported_as_the_pair(self):
        self.edit("calc.py", "return a + b", "return a + b + 1")
        self.edit("tests/test_calc.py", "assert add(10, 20) == 30", "assert add(10, 20) == 31")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 12, "expected-value-changed")])
        self.assertIn("`assert add(10, 20) == 30` became `assert add(10, 20) == 31`", found[0]["message"])

    def test_an_expected_value_changed_alone_is_not_found(self):
        self.edit("tests/test_calc.py", "assert add(10, 20) == 30", "assert add(10, 20) == 31")
        self.assertEqual(self.found()[0], [])

    def commit_mutmut_state(self):
        """Commit what mutmut leaves in mutants/ after a run: its copy of the code and of the tests beside it."""
        for name, text in self.BASE.items():
            self.put(f"mutants/{name}", text)
        self.git("add", "mutants")
        self.git("commit", "--quiet", "-m", "mutmut's state")

    def test_a_removed_test_is_found_whatever_syntax_the_environment_gives_git_s_pathspecs(self):
        self.put("Mutants/test_case.py", "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n")
        self.git("add", "Mutants")
        self.git("commit", "--quiet", "-m", "a directory that is not mutmut's")
        self.edit("Mutants/test_case.py", "def test_b():\n    assert 2\n", "")
        for name in ("GIT_LITERAL_PATHSPECS", "GIT_ICASE_PATHSPECS"):
            with self.subTest(name), patch.dict("os.environ", {name: "1"}):
                self.assertEqual(self.found()[0], [("Mutants/test_case.py", 5, "removed-test")])

    def test_removing_mutmut_s_state_removes_no_test(self):
        self.commit_mutmut_state()
        self.git("rm", "-r", "--quiet", "mutants")
        self.assertEqual(self.found()[0], [])

    def test_an_edit_to_mutmut_s_state_is_no_change_to_the_code(self):
        self.commit_mutmut_state()
        self.edit("mutants/calc.py", "return a + b", "return a + b + 1")
        self.edit("tests/test_calc.py", "assert add(10, 20) == 30", "assert add(10, 20) == 31")
        self.assertEqual(self.found()[0], [])

    def test_a_test_moved_out_of_mutmut_s_state_is_a_new_test(self):
        # Seen without mutants/, the move adds the file, so the skip at its base is on an added line.
        self.put("mutants/tests/test_skipped.py", "import pytest\n\n\n@pytest.mark.skip\ndef test_x():\n    assert 1\n")
        self.git("add", "mutants")
        self.git("commit", "--quiet", "-m", "mutmut's state")
        self.git("mv", "mutants/tests/test_skipped.py", "tests/test_skipped.py")
        self.assertEqual(self.found()[0], [("tests/test_skipped.py", 4, "skip")])

    def test_a_test_file_that_does_not_parse_says_so(self):
        self.edit("tests/test_other.py", "def test_b():", "def test_b(:")
        where, _ = self.found()
        self.assertEqual(where, [("tests/test_other.py", None, "unparsable")])

    def test_a_test_file_that_cannot_be_read_says_so(self):
        with open(os.path.join(self.repo, "tests/test_other.py"), "wb") as f:
            f.write(b"def test_a():\n    assert '\xff'\n")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_other.py", None, "unreadable")])
        self.assertIn("so its tests were not checked", found[0]["message"])

    def test_a_removed_helper_class_is_not_a_test(self):
        self.put("tests/test_help.py", "class Helper:\n    def make(self):\n        return 1\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "helper")
        self.put("tests/test_help.py", "")
        self.assertEqual(self.found()[0], [])

    def test_only_test_files_are_read(self):
        self.put("helpers.py", "def test_helper():\n    assert 1\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "helper")
        os.remove(os.path.join(self.repo, "helpers.py"))
        self.put("pkg/sub_test.py", "def test_x():\n    pass\n")
        self.git("add", "-A")
        self.assertEqual(self.found()[0], [])

    def test_changed_files_pairs_each_side(self):
        self.git("mv", "tests/test_other.py", "tests/test_renamed.py")
        os.remove(os.path.join(self.repo, "calc.py"))
        self.put("new.py", "x = 1\n")
        self.git("add", "new.py")
        self.edit("tests/test_calc.py", "import unittest\n", "import unittest  # edited\n")
        self.assertEqual(sorted(orchestrator._changed_files(self.repo, "HEAD"), key=str),
                         sorted([("calc.py", None), (None, "new.py"), ("tests/test_calc.py", "tests/test_calc.py"),
                                 ("tests/test_other.py", "tests/test_renamed.py")], key=str))


class TestFlakyTests(unittest.TestCase):
    """The flaky-tests check over a real git repository, rerunning its tests with this suite's own pytest."""

    BASE = (
        "import os\n"                                      # 1
        "import unittest\n"                                # 2
        "\n"                                               # 3
        "# test_write_<k> adds k; test_read_<k> passes only before it.\n"  # 4
        "WRITTEN = set()\n"                                # 5
        "# A character for each run of TestRuns.test_alternates in this checkout.\n"  # 6
        "RUNS = os.path.join(os.path.dirname(__file__), 'runs')\n"  # 7
        "\n"                                               # 8
        "\n"                                               # 9
        "def test_old_reads():\n"                          # 10
        "    assert 0 not in WRITTEN\n"                    # 11
        "\n"                                               # 12
        "\n"                                               # 13
        "class TestRuns(unittest.TestCase):\n"             # 14
        "    def test_alternates(self):\n"                 # 15
        "        self.skipTest('not yet')\n"               # 16
        "        with open(RUNS, 'a') as f:\n"             # 17
        "            f.write('x')\n"                       # 18
        "        with open(RUNS) as f:\n"                  # 19
        "            self.assertEqual(len(f.read()) % 2, 1)\n"  # 20
    )
    # The reader and writer pairs change_the_tests adds.
    PAIRS = 8

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.host = Host()
        self.git("init", "--quiet", "-b", "main")
        for key, value in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, value)
        self.put("tests/test_state.py", self.BASE)
        self.put("calc.py", "def add(a, b):\n    return a + b\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "base")

    def git(self, *args):
        return self.host.git(self.repo, *args)

    def put(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def found(self):
        base = self.git("rev-parse", "HEAD").strip()
        return orchestrator.flaky_tests(orchestrator.GateChange.from_tree(self.repo, base))

    def change_the_tests(self, extra=""):
        """Edit TestRuns.test_alternates to fail on every second run, whatever the order, and add PAIRS writers and
        readers, a stable test, a broken one and extra; returns the new text of the test file."""
        text = self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)")
        for k in range(self.PAIRS):
            text += (f"\n\ndef test_write_{k}():\n    WRITTEN.add({k})\n"
                     f"\n\ndef test_read_{k}():\n    assert {k} not in WRITTEN\n")
        text += ("\n\ndef test_stable():\n    assert WRITTEN == WRITTEN\n"
                 "\n\ndef test_broken():\n    assert False\n" + extra)
        self.put("tests/test_state.py", text)
        return text

    @staticmethod
    def line_of(text, definition):
        return text.splitlines().index(definition) + 1

    @staticmethod
    def alternates():
        """The finding's message on TestRuns.test_alternates, which fails on the second and fourth run."""
        s = orchestrator.FLAKY_SEEDS
        return (f"`TestRuns.test_alternates` failed under seeds {s[1]} and {s[3]} and passed under seeds {s[0]}, "
                f"{s[2]} and {s[4]}: it depends on the order the tests run in, or on state or randomness that one run "
                f"does not share with another.")

    def test_an_order_dependent_test_is_reported_with_the_seeds_it_failed_under(self):
        text = self.change_the_tests()
        found = self.found()
        by_line = {f["line"]: f for f in found}
        self.assertEqual({(f["path"], f["severity"]) for f in found}, {("tests/test_state.py", "warning")})
        # test_alternates fails on the second and fourth run, in any order of the tests.
        self.assertEqual(by_line.pop(15)["message"], self.alternates())
        # test_broken fails under every seed: not flaky, but reported, since the copy may be what it lacks.
        self.assertEqual(by_line.pop(self.line_of(text, "def test_broken():"))["rule"], "failed-every-run")
        # Each seed orders each pair one way or the other. Five seeds that split none of eight independent pairs
        # have a chance of 16 ** -8 under a uniform shuffle, so some reader is reported whatever pytest-randomly's
        # version. No writer is, nor test_stable. test_old_reads depends on test_write_0 as test_read_0 does, but
        # the change left it alone, so it is not rerun.
        self.assertEqual({f["rule"] for f in by_line.values()}, {"mixed-results"})
        readers = {self.line_of(text, f"def test_read_{k}():"): k for k in range(self.PAIRS)}
        self.assertTrue(by_line)
        self.assertLessEqual(set(by_line), set(readers))
        for line, f in by_line.items():
            m = re.fullmatch(rf"`test_read_{readers[line]}` failed under seeds? ([\d, and]+) and passed under seeds? "
                             r"([\d, and]+): .+", f["message"])
            self.assertIsNotNone(m, f["message"])
            failed, passed = ({int(s) for s in re.findall(r"\d+", g)} for g in m.groups())
            self.assertTrue(failed and passed)
            self.assertEqual(failed | passed, set(orchestrator.FLAKY_SEEDS))

    def test_the_project_s_verbosity_does_not_hide_the_tests(self):
        self.change_the_tests()
        # Two seeds are enough for test_alternates to fail under one, and keep this test quick.
        seeds = patch.object(orchestrator, "FLAKY_SEEDS", (1, 2))
        for ini, env in (("addopts = -v", ""), ("addopts = -qq", ""), ("", "-q"), ("verbosity_test_cases = 2", "")):
            with self.subTest(ini=ini, env=env), seeds, patch.dict(os.environ, {"PYTEST_ADDOPTS": env}):
                self.put("pytest.ini", f"[pytest]\n{ini}\n")
                self.git("add", "pytest.ini")
                self.assertIn("`TestRuns.test_alternates` failed under seed 2 and passed under seed 1: ",
                              "\n".join(f["message"] for f in self.found()))

    def fake_python(self, script):
        """Make the gate's interpreter a shell script that runs script, whatever it is asked."""
        fake = os.path.join(self.repo, "fake-python")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\n{script}\n")
        os.chmod(fake, 0o700)
        p = patch.object(orchestrator.sys, "executable", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_a_collection_the_gate_cannot_read_is_a_finding(self):
        self.change_the_tests()
        self.fake_python("printf 'tests/test_state.py: 20\\n\\n20 tests collected\\n'")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertIn("printed no node id the gate could read: 20 tests collected", found[0]["message"])

    def test_a_test_missing_from_a_run_s_report_is_not_counted_as_passed(self):
        # --maxfail=1 ends each run at its first failure, so of four tests that always fail, each run reports one.
        # None is flaky; some must be reported under fewer than two seeds, as five runs report five failures in all.
        # One that two or more runs failed is reported as failing under every seed that judged it.
        self.put("pytest.ini", "[pytest]\naddopts = --maxfail=1\n")
        self.git("add", "pytest.ini")
        text = self.BASE + "".join(f"\n\ndef test_fails_{k}():\n    assert False\n" for k in range(4))
        self.put("tests/test_state.py", text)
        found = self.found()
        self.assertIn("not-rerun", {f["rule"] for f in found})
        self.assertLessEqual({f["rule"] for f in found}, {"not-rerun", "failed-every-run"})
        lines = {self.line_of(text, f"def test_fails_{k}():") for k in range(4)}
        for f in (f for f in found if f["rule"] == "not-rerun"):
            self.assertIn(f["line"], lines)
            self.assertRegex(f["message"], r"^`test_fails_\d` (failed under seed \d only|neither passed nor failed "
                                           r"under any seed); the reports of seeds? [\d, and]+ have it skipped or not "
                                           r"at all, so too few runs tell whether it is flaky\.$")

    def test_a_test_skipped_under_some_seeds_is_judged_by_the_others(self):
        # Skipped on the third run, failing on the second and fourth: the third is neither a pass nor a failure.
        text = self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)").replace(
            "            self.assertEqual(len(f.read()) % 2, 1)\n",
            "            runs = len(f.read())\n"
            "        if runs == 3:\n"
            "            self.skipTest('the third run')\n"
            "        self.assertEqual(runs % 2, 1)\n")
        self.put("tests/test_state.py", text)
        s = orchestrator.FLAKY_SEEDS
        self.assertEqual([f["message"] for f in self.found()], [(
            f"`TestRuns.test_alternates` failed under seeds {s[1]} and {s[3]} and passed under seeds {s[0]} and "
            f"{s[4]}, and was skipped or not run under seed {s[2]}: it depends on the order the tests run in, or on "
            f"state or randomness that one run does not share with another.")])

    def test_a_test_that_only_lost_lines_is_rerun(self):
        self.put("tests/test_state.py", self.BASE.replace("        self.skipTest('not yet')\n", ""))
        self.assertEqual([(f["line"], f["message"]) for f in self.found()], [(15, self.alternates())])

    def test_a_decorator_removed_above_the_others_reruns_its_test(self):
        decorated = self.BASE.replace("        self.skipTest('not yet')\n", "").replace(
            "    def test_alternates(self):\n",
            "    @unittest.skip('not yet')\n    @unittest.expectedFailure\n    def test_alternates(self):\n")
        self.put("tests/test_state.py", decorated)
        self.git("commit", "--quiet", "-am", "decorated")
        self.put("tests/test_state.py", decorated.replace("    @unittest.skip('not yet')\n", "").replace(
            "    @unittest.expectedFailure\n", ""))
        # Two seeds are enough for test_alternates to fail under one, and keep this test quick.
        with patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)):
            self.assertEqual([(f["line"], f["rule"]) for f in self.found()], [(15, "mixed-results")])

    def test_a_test_file_whose_name_holds_a_line_end_is_rerun(self):
        # A carriage return breaks text mode's reading of pytest's list of node ids, and U+2028 str.splitlines'.
        old = "tests/test_state.py"
        for name in ("tests/test_st\u2028ate.py", "tests/test_st\rate.py"):
            with self.subTest(name=name), patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)):
                self.git("mv", old, name)
                self.git("commit", "--quiet", "-m", "renamed")
                old = name
                self.put(name, self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)"))
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()],
                                 [(name, 15, "mixed-results")])
                self.git("checkout", "--", name)

    # A module-level test like TestRuns.test_alternates, failing on every second run; line 9 is its def.
    ALTERNATING = ("import os\n"
                   "\n"
                   "import pytest\n"
                   "\n"
                   "RUNS = os.path.join(os.path.dirname(__file__), 'runs')\n"
                   "\n"
                   "\n"
                   "@pytest.mark.parametrize('x', ['a::b'])\n"
                   "def test_p(x):\n"
                   "    with open(RUNS, 'a') as f:\n"
                   "        f.write('x')\n"
                   "    with open(RUNS) as f:\n"
                   "        assert len(f.read()) % 2 == 1\n")

    def test_a_parameter_id_that_holds_colons_is_judged(self):
        self.put("tests/test_p.py", self.ALTERNATING)
        self.git("add", "tests/test_p.py")
        with patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("tests/test_p.py", 9, "mixed-results")])
        self.assertTrue(found[0]["message"].startswith("`test_p[a::b]` failed under seed 2 and passed under seed 1: "))

    def decorated(self, old_above, new_above):
        """The lines of test_r's def found on a change from old_above to new_above, the lines above it."""
        head = "import os\n\nimport pytest\n\nRUNS = os.path.join(os.path.dirname(__file__), 'runs')\n\n\n"
        body = ("def test_r():\n    with open(RUNS, 'a') as f:\n        f.write('x')\n"
                "    with open(RUNS) as f:\n        assert len(f.read()) % 2 == 1\n")
        self.put("tests/test_r.py", head + old_above + body)
        self.git("add", "tests/test_r.py")
        self.git("commit", "--quiet", "-m", "test_r")
        self.put("tests/test_r.py", head + new_above + body)
        with patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)):
            return [(f["path"], f["line"], f["rule"]) for f in self.found()]

    def test_a_decorator_replaced_by_a_comment_reruns_its_test(self):
        self.assertEqual(self.decorated("@pytest.mark.skip(reason='alternates, #12')\n", "# Was skipped for #12.\n"),
                         [("tests/test_r.py", 9, "mixed-results")])

    def test_a_decorator_removed_above_a_comment_reruns_its_test(self):
        self.assertEqual(self.decorated("@pytest.mark.skip(reason='#12')\n# Alternates between runs.\n",
                                        "# Alternates between runs.\n"),
                         [("tests/test_r.py", 9, "mixed-results")])

    def test_a_test_that_xfails_under_every_seed_is_consistent(self):
        old = "import pytest\n\n\n@pytest.mark.xfail(strict=True, reason='bug 12')\ndef test_a():\n    assert 1 == 2\n"
        self.put("tests/test_x.py", old)
        self.git("add", "tests/test_x.py")
        self.git("commit", "--quiet", "-m", "xfail")
        self.put("tests/test_x.py", old.replace("1 == 2", "1 == 3"))
        self.assertEqual(self.found(), [])

    def test_a_test_that_xfails_under_some_seeds_and_passes_under_others_is_mixed(self):
        self.assertEqual(self.decorated("", "@pytest.mark.xfail(reason='#12')\n"),
                         [("tests/test_r.py", 9, "mixed-results")])
        with patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)):
            [found] = self.found()
        self.assertTrue(found["message"].startswith("`test_r` failed as its xfail mark expects under seed 2 and passed "
                                                    "under seed 1: "), found["message"])

    def test_a_test_file_whose_name_is_not_utf8_is_rerun(self):
        name = os.fsdecode(b"tests/test_\xff.py")
        self.git("mv", "tests/test_state.py", name)
        self.git("commit", "--quiet", "-m", "renamed")
        self.put(name, self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)"))
        with patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)):
            found = self.found()
        if [f["rule"] for f in found] == ["not-rerun"]:
            # pytest collects and lists the test, but pytest-randomly 5.0.0 hashes each node id as strict UTF-8
            # (pytest_randomly._crc32), so the first rerun stops with an internal error, which the finding quotes.
            self.assertRegex(found[0]["message"], r"^pytest, rerunning them under seed 1, exited with status 3: "
                                                  r"UnicodeEncodeError: .*surrogates not allowed; the changed tests "
                                                  r"were not rerun\.$")
        else:
            self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(name, 15, "mixed-results")])

    def test_no_config_or_conftest_above_the_copy_is_loaded(self):
        # Another account can write the directory the copy is made in; pytest must not take its pytest.ini, which
        # would make that directory confcutdir, nor so import its conftest.py.
        shared = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, shared)
        pwned = os.path.join(shared, "PWNED")
        with open(os.path.join(shared, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        with open(os.path.join(shared, "conftest.py"), "w") as f:
            f.write(f"with open({pwned!r}, 'a') as f:\n    f.write('conftest ran\\n')\n")
        self.put("tests/test_state.py", self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)"))
        with patch.object(tempfile, "tempdir", shared), patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)):
            self.assertEqual([(f["line"], f["rule"]) for f in self.found()], [(15, "mixed-results")])
        self.assertFalse(os.path.exists(pwned))

    def test_the_project_s_pyproject_toml_configures_the_reruns(self):
        # --maxfail=1 in the project's own table leaves each of four always failing tests judged by few runs.
        text = self.BASE + "".join(f"\n\ndef test_fails_{k}():\n    assert False\n" for k in range(4))
        for table in ('[tool.pytest.ini_options]\naddopts = "--maxfail=1"\n',
                      '[tool.pytest]\naddopts = ["--maxfail=1"]\n'):
            with self.subTest(table=table):
                self.put("pyproject.toml", f"[project]\nname = 'p'\n\n{table}")
                self.git("add", "pyproject.toml")
                self.put("tests/test_state.py", text)
                # Without the table, each of them would fail under every seed, and none would be judged too seldom.
                self.assertIn("not-rerun", {f["rule"] for f in self.found()})

    def test_the_project_s_pytest_config_is_found_as_pytest_finds_it(self):
        cases = [
            ({}, None),
            ({"pyproject.toml": "[project]\nname = 'p'\n", "tox.ini": "[tox]\n", "setup.cfg": "[metadata]\n"}, None),
            ({"pyproject.toml": "[tool.pytest]\n"}, None),
            ({"pyproject.toml": "[tool.pytest.ini_options]\n", "pytest.ini": ""}, "pytest.ini"),
            ({"pyproject.toml": "[tool.pytest.ini_options]\n", "tox.ini": "[pytest]\n"}, "pyproject.toml"),
            ({"pyproject.toml": "[tool.pytest]\naddopts = []\n"}, "pyproject.toml"),
            ({"tox.ini": "[pytest]\n", "setup.cfg": "[tool:pytest]\n"}, "tox.ini"),
            ({"tox.ini": "[tool:pytest]\n", "setup.cfg": "[tool:pytest]\n"}, "setup.cfg"),
            ({".pytest.ini": "", "pytest.toml": ""}, "pytest.toml"),
            ({"pyproject.toml": "not toml ["}, "pyproject.toml"),
        ]
        for files, expected in cases:
            with self.subTest(files=files), tempfile.TemporaryDirectory() as tree:
                for name, text in files.items():
                    with open(os.path.join(tree, name), "w") as f:
                        f.write(text)
                found = orchestrator._pytest_inifile(tree)
                self.assertEqual(found and os.path.basename(found), expected)

    def failing_in_the_copy(self, base, changed, extra=None):
        """The findings on tests/test_g.py changed from base to changed, with extra files beside it untracked."""
        self.put("tests/test_g.py", base)
        self.git("add", "tests/test_g.py")
        self.git("commit", "--quiet", "-m", "test_g")
        for name, text in (extra or {}).items():
            self.put(name, text)
        self.put("tests/test_g.py", changed)
        with patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)):
            return self.found()

    def test_a_test_that_needs_git_s_metadata_fails_every_run_in_the_copy(self):
        test = ("import os\nimport subprocess\n\n\ndef test_head():\n"
                "    subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, cwd=os.path.dirname(__file__))\n")
        [found] = self.failing_in_the_copy(test.replace("check=True", "check=False"), test)
        self.assertEqual((found["path"], found["line"], found["severity"], found["rule"]),
                         ("tests/test_g.py", 5, "warning", "failed-every-run"))
        self.assertEqual(found["message"],
                         "`test_head` failed under seeds 1 and 2, every rerun, which ran in a copy of the tracked "
                         "files. If the build failed it too, the failing tests list it. If the build passed it, it "
                         "needs something the copy lacks, such as git's metadata, an untracked or ignored file or a "
                         "build product, so whether it is flaky could not be judged here.")

    def test_a_test_that_needs_an_ignored_file_fails_every_run_in_the_copy(self):
        test = ("import os\n\n\ndef test_settings():\n"
                "    assert os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'settings.local'))\n")
        found = self.failing_in_the_copy(test.replace("assert ", "assert True or "), test,
                                         {".gitignore": "settings.local\n", "settings.local": "x = 1\n"})
        self.assertEqual([(f["line"], f["rule"]) for f in found], [(4, "failed-every-run")])

    def stopped_at(self, seed, extra):
        """The findings on the change that adds extra to the test file, when the rerun under seed times out."""
        self.put("tests/test_state.py", self.BASE + extra)
        rerun = orchestrator._rerun_python_tests

        def times_out(tree, inifile, ids, s, report):
            if s == seed:
                return {}, f"pytest, rerunning them under seed {seed}, did not finish within 30 seconds"
            return rerun(tree, inifile, ids, s, report)
        with patch.object(orchestrator, "_rerun_python_tests", times_out):
            return {(f["line"], f["rule"]): f["message"] for f in self.found()}

    def test_a_test_one_run_failed_before_the_next_went_wrong_is_not_failing_every_run(self):
        found = self.stopped_at(2, "\n\ndef test_one():\n    assert False\n")
        self.assertEqual(found, {
            (23, "not-rerun"): "`test_one` failed under seed 1 only; the reruns stopped when the run under seed 2 "
                               "went wrong, so too few runs tell whether it is flaky.",
            (None, "not-rerun"): "pytest, rerunning them under seed 2, did not finish within 30 seconds; only the "
                                 "reruns under seed 1 were judged."})

    def test_a_test_the_runs_before_a_stop_skipped_is_judged_by_too_few(self):
        found = self.stopped_at(3, "\n\nimport pytest\n\n\ndef test_skipped():\n    pytest.skip('x')\n"
                                   "\n\ndef test_fails():\n    assert False\n")
        self.assertEqual(set(found), {(None, "not-rerun"), (26, "not-rerun"), (30, "failed-every-run")})
        self.assertEqual(found[(26, "not-rerun")],
                         "`test_skipped` neither passed nor failed under any seed; the reports of seeds 1 and 2 have "
                         "it skipped or not at all; the reruns stopped when the run under seed 3 went wrong, so too "
                         "few runs tell whether it is flaky.")

    def test_a_change_to_no_test_reruns_nothing(self):
        self.put("calc.py", "def add(a, b):\n    return b + a\n")
        self.put("tests/test_state.py", self.BASE.replace("import unittest\n", "import unittest  # edited\n"))
        with patch.object(orchestrator, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def test_mutmut_s_copy_of_a_test_is_not_rerun(self):
        # mutmut copies the tests into mutants/ beside the mutated code; committed, they are its state, not tests.
        self.put("mutants/tests/test_state.py", self.BASE + "\n\ndef test_new():\n    assert True\n")
        self.git("add", "mutants/tests/test_state.py")
        with patch.object(orchestrator, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def test_a_rerun_that_cannot_start_is_a_finding(self):
        self.change_the_tests()
        with patch.object(orchestrator.sys, "executable", os.path.join(self.repo, "no-python")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertIn("could not start pytest", found[0]["message"])

    def test_a_rerun_that_times_out_is_a_finding(self):
        self.change_the_tests()
        self.fake_python("exec sleep 30")
        with patch.object(orchestrator, "FLAKY_RUN_TIMEOUT", 1):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertIn("did not finish within 1 seconds", found[0]["message"])

    def test_no_room_for_the_reruns_is_a_finding(self):
        self.change_the_tests()
        with patch.object(tempfile, "tempdir", os.path.join(self.repo, "no-such-dir")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertRegex(found[0]["message"], r"^could not make a directory to rerun the changed tests in: .*No such "
                                              r"file.*; the changed tests were not rerun\.$")

    def test_the_reruns_leave_the_build_s_reports_alone(self):
        # The project's addopts write a coverage report, as the build's own run did before the gate reads it.
        self.put("pytest.ini", "[pytest]\naddopts = --cov=calc --cov-report=xml\n")
        self.git("add", "pytest.ini")
        self.git("commit", "--quiet", "-m", "coverage")
        self.put("coverage.xml", "the build's report\n")
        self.put("tests/test_state.py", self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)"))
        with patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)):
            self.assertEqual([(f["line"], f["rule"]) for f in self.found()], [(15, "mixed-results")])
        with open(os.path.join(self.repo, "coverage.xml")) as f:
            self.assertEqual(f.read(), "the build's report\n")
        self.assertEqual(sorted(os.listdir(self.repo)), [".git", "calc.py", "coverage.xml", "pytest.ini", "tests"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.repo, "tests"))), ["test_state.py"])

    def test_tracked_files_that_cannot_be_copied_are_a_finding(self):
        self.change_the_tests()
        with patch.object(orchestrator.shutil, "copy2", side_effect=PermissionError(13, "Permission denied")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertRegex(found[0]["message"], r"^could not copy the tracked files to rerun the changed tests in: .*"
                                              r"Permission denied.*; the changed tests were not rerun\.$")

    def test_copy_tracked_copies_what_git_tracks_as_it_is(self):
        self.put("tests/data/a.txt", "a\n")
        os.symlink("data/a.txt", os.path.join(self.repo, "tests", "link"))
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "data")
        self.put("tests/data/a.txt", "edited\n")
        self.put("untracked.txt", "x\n")
        os.remove(os.path.join(self.repo, "calc.py"))
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "copy")
            orchestrator._copy_tracked(self.repo, dest)
            found = sorted(os.path.relpath(os.path.join(d, f), dest) for d, _, fs in os.walk(dest) for f in fs)
            self.assertEqual(found, ["tests/data/a.txt", "tests/link", "tests/test_state.py"])
            self.assertEqual(os.readlink(os.path.join(dest, "tests", "link")), "data/a.txt")
            with open(os.path.join(dest, "tests", "data", "a.txt")) as f:
                self.assertEqual(f.read(), "edited\n")

    def test_a_rerun_directory_left_behind_is_a_finding_beside_the_results(self):
        self.change_the_tests()
        rmtree = shutil.rmtree

        def refuse(path):
            self.addCleanup(rmtree, path)
            raise PermissionError(13, "Permission denied", path)
        with patch.object(orchestrator, "FLAKY_SEEDS", (1, 2)), patch.object(orchestrator.shutil, "rmtree", refuse):
            found = self.found()
        self.assertIn((15, "mixed-results"), [(f["line"], f["rule"]) for f in found])
        [left] = [f for f in found if f["rule"] == "not-removed"]
        self.assertEqual((left["path"], left["line"]), (None, None))
        self.assertRegex(left["message"], r"^could not remove the directory the changed tests were rerun in, .+: "
                                          r".*Permission denied")

    def test_a_test_file_that_does_not_collect_is_a_finding(self):
        self.change_the_tests()
        with open(os.path.join(self.repo, "tests/test_state.py"), "a") as f:
            f.write("\nimport no_such_module\n")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertIn("exited with status 2", found[0]["message"])

    def test_an_unparsable_test_file_is_a_finding(self):
        self.put("tests/test_state.py", self.BASE + "def test_new(:\n")
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()],
                         [("tests/test_state.py", None, "not-rerun")])

    def test_it_reports_without_failing_the_gate(self):
        [check] = [c for c in orchestrator.gate_checks(orchestrator.MUTATION_BUDGET) if c.name == "flaky-tests"]
        self.assertEqual((check.fails, check.find), (False, orchestrator.flaky_tests))


class TestPassesAtBase(unittest.TestCase):
    """The passes-at-base check over a real git repository, running its tests with this suite's own pytest."""

    # add is wrong at the base; mul is right, and stays so.
    CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
    TESTS = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.host = Host()
        self.git("init", "--quiet", "-b", "main")
        for key, value in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, value)
        self.put("calc.py", self.CALC)
        self.put("tests/test_calc.py", self.TESTS)
        self.put("pytest.ini", "[pytest]\npythonpath = .\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "base")

    def git(self, *args):
        return self.host.git(self.repo, *args)

    def put(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def found(self, budget=orchestrator.BASE_RUN_BUDGET):
        """The check's findings on the working tree, every file in it tracked."""
        base = self.git("rev-parse", "HEAD").strip()
        self.git("add", "-A")
        return orchestrator.passes_at_base(orchestrator.GateChange.from_tree(self.repo, base), budget=budget)

    def fix_add(self, tests=""):
        """Fix add, and add test_add, which fails on the old add, then tests."""
        self.put("calc.py", self.CALC.replace("a - b", "a + b"))
        self.put("tests/test_calc.py", self.TESTS.replace("import mul", "import add, mul")
                 + "\n\ndef test_add():\n    assert add(2, 2) == 4\n" + tests)

    def test_a_test_that_fails_without_the_fix_gives_nothing(self):
        self.fix_add()
        self.assertEqual(self.found(), [])

    def test_a_test_of_code_the_change_left_alone_passes_at_the_base(self):
        # The fixture comes from a conftest.py the change adds, so the test runs at the base only with it overlaid.
        self.put("tests/conftest.py", "import pytest\n\n\n@pytest.fixture\ndef three():\n    return 3\n")
        self.fix_add("\n\nimport pytest\n\n\n@pytest.mark.parametrize('n', [1, 2])\n"
                     "def test_mul_again(n, three):\n    assert mul(n, three) == 3 * n\n")
        self.git("add", "-A")
        status = self.git("status", "--porcelain")
        found = self.found()
        with open(os.path.join(self.repo, "tests/test_calc.py")) as f:
            line = f.read().splitlines().index("def test_mul_again(n, three):") + 1
        self.assertEqual(found, [{"path": "tests/test_calc.py", "line": line, "severity": "warning",
                                  "rule": "passes-at-base",
                                  "message": "`test_mul_again` passes without the change, so it does not test it."}])
        self.assertEqual(self.git("status", "--porcelain"), status)

    def test_a_test_of_a_function_the_change_adds_gives_nothing(self):
        # At the base, the test file cannot import sub, so pytest collects nothing from it.
        self.put("calc.py", self.CALC + "\n\ndef sub(a, b):\n    return a - b\n")
        self.put("tests/test_sub.py", "from calc import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")
        self.assertEqual(self.found(), [])

    def test_a_change_to_the_tests_alone_is_not_run(self):
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_zero():\n    assert mul(0, 5) == 0\n")
        with patch.object(orchestrator, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def fake_pytest(self, collect, run):
        """Make the gate's interpreter a shell script that runs collect for a --collect-only and run otherwise."""
        fake = os.path.join(self.repo, "fake-python")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\ncase "$*" in *--collect-only*) {collect};; *) {run};; esac\n')
        os.chmod(fake, 0o700)
        p = patch.object(orchestrator.sys, "executable", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_a_run_past_the_budget_says_how_many_test_cases_ran(self):
        self.fix_add("\n\ndef test_more():\n    assert add(1, 1) == 2\n")
        # The sleep in the background holds pytest's output open too: only killing its process group ends the run.
        self.fake_pytest("printf 'tests/test_calc.py::test_add\\ntests/test_calc.py::test_more\\n'",
                         "printf '.'; sleep 30 & exec sleep 30")
        start = time.monotonic()
        found = self.found(budget=1)
        self.assertLess(time.monotonic() - start, orchestrator.MUTMUT_KILL_WAIT)
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "budget-reached")])
        self.assertEqual(found[0]["message"], "pytest was stopped at 1 seconds (`--base-run-budget`) after 1 of the 2 "
                                              "test cases it was given, before it reported them, so none of them was "
                                              "judged.")

    def test_a_collection_past_the_budget_is_a_finding(self):
        self.fix_add()
        self.fake_pytest("exec sleep 30", "exit 0")
        self.assertEqual([f["message"] for f in self.found(budget=1)], [(
            "pytest was stopped at 1 seconds (`--base-run-budget`) while it collected the changed tests, so none of "
            "them was judged.")])

    def test_a_test_pytest_does_not_collect_leaves_the_others_judged(self):
        # pytest does not collect a test_* method of a class not named Test*; asked for it by id, it exits 4.
        self.fix_add("\n\nclass CalcMixin:\n    def test_x(self):\n        assert True\n"
                     "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([(f["rule"], f["message"].split("`")[1]) for f in self.found()],
                         [("passes-at-base", "test_mul_one")])

    def test_a_conftest_that_cannot_be_imported_at_the_base_gives_nothing(self):
        self.put("calc.py", self.CALC.replace("a - b", "a + b") + "\n\nTWO = 2\n")
        self.put("tests/conftest.py", "import pytest\n\nfrom calc import TWO\n\n\n@pytest.fixture\n"
                                      "def two():\n    return TWO\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_two(two):\n    assert mul(two, 1) == 2\n")
        self.assertEqual(self.found(), [])

    def test_a_conftest_that_cannot_be_imported_hides_only_the_tests_under_it(self):
        self.put("calc.py", self.CALC.replace("a - b", "a + b") + "\n\nTWO = 2\n")
        self.put("tests/b/conftest.py", "from calc import TWO\n")
        self.put("tests/b/test_two.py", "from calc import mul\n\n\ndef test_two():\n    assert mul(2, 1) == 2\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([(f["path"], f["message"].split("`")[1]) for f in self.found()],
                         [("tests/test_calc.py", "test_mul_one")])

    def test_a_conftest_broken_otherwise_is_a_finding(self):
        self.put("calc.py", self.CALC.replace("a - b", "a + b"))
        self.put("tests/conftest.py", "raise RuntimeError('broken')\n")
        self.git("add", "tests/conftest.py")
        self.git("commit", "--quiet", "-m", "broken conftest")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([(f["rule"], "exited with status 4" in f["message"]) for f in self.found()],
                         [("not-run", True)])

    def test_nothing_is_written_through_a_link_the_change_replaces(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        self.git("rm", "--quiet", "-r", "tests")
        os.symlink(outside, os.path.join(self.repo, "tests"))
        self.git("add", "tests")
        self.git("commit", "--quiet", "-m", "tests elsewhere")
        os.remove(os.path.join(self.repo, "tests"))
        self.fix_add("\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        # The whole test file is new to the change, so test_mul is changed too.
        self.assertEqual([f["message"].split("`")[1] for f in self.found()], ["test_mul", "test_mul_one"])
        self.assertEqual(os.listdir(outside), [])

    def test_a_pytest_that_stops_before_it_collects_is_a_finding(self):
        # pytest exits 1 for a plugin it cannot import, as for collection errors, but collects nothing.
        self.put("calc.py", self.CALC.replace("a - b", "a + b"))
        self.put("conftest.py", "pytest_plugins = ['calc_plugin']\n")
        self.put("calc_plugin.py", "")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        found = self.found()
        self.assertEqual([(f["path"], f["rule"]) for f in found], [(None, "not-run")])
        self.assertIn("pytest stopped before it collected the changed tests at the base: ", found[0]["message"])
        self.assertIn("calc_plugin", found[0]["message"])

    def test_a_directory_the_change_makes_a_link_is_replaced(self):
        self.put("tests/data/x.json", "{}\n")
        self.git("add", "tests/data")
        self.git("commit", "--quiet", "-m", "data")
        self.git("rm", "--quiet", "-r", "tests/data")
        self.put("fixtures/x.json", "{}\n")
        os.symlink("../fixtures", os.path.join(self.repo, "tests/data"))
        self.fix_add("\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([f["message"].split("`")[1] for f in self.found()], ["test_mul_one"])

    def test_a_change_to_docs_and_tests_is_not_run(self):
        self.put("TODO.md", "- nothing\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_zero():\n    assert mul(0, 5) == 0\n")
        with patch.object(orchestrator, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def test_a_tracked_link_out_of_the_tree_does_not_stop_the_copy(self):
        os.symlink("/etc/hostname", os.path.join(self.repo, "host"))
        self.git("add", "host")
        self.git("commit", "--quiet", "-m", "link")
        self.fix_add("\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([f["rule"] for f in self.found()], ["passes-at-base"])

    def test_a_run_that_cannot_start_is_a_finding(self):
        self.fix_add()
        with patch.object(orchestrator.sys, "executable", os.path.join(self.repo, "no-python")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("could not start pytest", found[0]["message"])

    def test_an_unparsable_test_file_is_a_finding(self):
        self.put("calc.py", self.CALC.replace("a - b", "a + b"))
        self.put("tests/test_calc.py", self.TESTS + "def test_new(:\n")
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()],
                         [("tests/test_calc.py", None, "not-run")])

    def test_a_change_beside_the_tests_alone_is_not_run(self):
        # A conftest.py anywhere and any file under tests/ go with the tests, not with the code they test.
        self.put("conftest.py", "")
        self.put("tests/data/input.txt", "2 3\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_zero():\n    assert mul(0, 5) == 0\n")
        with patch.object(orchestrator, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def test_its_budget_is_a_whole_number_of_seconds_from_1(self):
        self.assertEqual(parse_args(["gate", "--base", "main", "--base-run-budget", "7"]).base_run_budget, 7)
        self.assertEqual(parse_args(["gate", "--base", "main"]).base_run_budget, orchestrator.BASE_RUN_BUDGET)
        for flag in ("--base-run-budget", "--mutation-budget"):
            for value, error in (("0", "must be at least 1, not 0"), ("1.5", "expected a whole number of seconds")):
                with self.subTest(flag=flag, value=value), patch("sys.stderr", new_callable=io.StringIO) as err:
                    with self.assertRaises(SystemExit):
                        parse_args(["gate", "--base", "main", flag, value])
                    self.assertIn(f"argument {flag}: {error}", err.getvalue())

    def test_it_reports_without_failing_the_gate_within_its_budget(self):
        with patch.object(orchestrator, "passes_at_base", return_value=[]) as find:
            [check] = [c for c in orchestrator.gate_checks(base_run_budget=42) if c.name == "passes-at-base"]
            check.find("change")
        self.assertFalse(check.fails)
        find.assert_called_once_with("change", budget=42)


class TestWrittenRules(unittest.TestCase):
    """The coding rules the agents are given as prose, as this repository's ruff rules state them."""

    PYPROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")

    def ruff(self, name, text):
        """(line, rule code) of each finding the real ruff reports on text, saved as name in a copy of this
        repository's pyproject.toml's directory, run there with a relative path as the gate runs it: ruff resolves
        per-file-ignores against its working directory."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shutil.copy(self.PYPROJECT, tmp.name)
        os.makedirs(os.path.join(tmp.name, os.path.dirname(name)), exist_ok=True)
        with open(os.path.join(tmp.name, name), "w") as f:
            f.write(text)
        # A fixed command over a file this test wrote; no shell reads it.
        proc = subprocess.run([sys.executable, "-m", "ruff", "check", "--config", "pyproject.toml",  # noqa: S603
                               "--no-cache", "--output-format", "concise", name],
                              cwd=tmp.name, capture_output=True, text=True, check=False)
        self.assertNotIn("No module named", proc.stderr)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        return sorted((int(line), code) for line, code in re.findall(r":(\d+):\d+: ([A-Z]+\d+)", proc.stdout))

    STORE = ('"""A module."""\n\n\ndef load():\n    return 1\n\n\nclass Store:\n    def get(self):\n        return 1\n'
             '\n    def __init__(self):\n        self.n = 1\n\n    def __len__(self):\n        return 1\n'
             '\n\ndef _private():\n    return 1\n')

    def test_an_undocumented_public_function_class_or_method_is_found_at_its_def_outside_the_tests(self):
        self.assertEqual(self.ruff("store.py", self.STORE), [(4, "D103"), (8, "D101"), (9, "D102")])
        # The tests document themselves by their names.
        self.assertEqual(self.ruff("tests/test_store.py", self.STORE.replace('"""A module."""\n', "")), [])

    def test_a_blind_except_that_goes_on_is_found(self):
        # BLE001 comes from ruff's defaults, which extend-select keeps: this fails if the rules move to select.
        text = ('"""A module."""\n\n\ndef load():\n    """Load."""\n    try:\n        return 1\n'
                '    except Exception:\n        return None\n')
        self.assertEqual(self.ruff("store.py", text), [(8, "BLE001")])


class TestVagueNames(unittest.TestCase):
    """The vague-names check over a real git repository: a base commit, then the change in its working tree."""

    BASE = "data = 1\n\n\ndef old(value):\n    return value\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.host = Host()
        self.git("init", "--quiet", "-b", "main")
        for key, setting in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, setting)
        self.put("calc.py", self.BASE)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "base")

    def git(self, *args):
        return self.host.git(self.repo, *args)

    def put(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def found(self):
        """(path, line, rule, message) of each finding on the working tree's change, every file in it tracked."""
        self.git("add", "-A")
        change = orchestrator.GateChange.from_tree(self.repo, self.git("rev-parse", "HEAD").strip())
        return [(f["path"], f["line"], f["rule"], f["message"]) for f in orchestrator.vague_names(change)]

    def test_each_vague_name_an_added_line_binds_is_found_once_at_its_first_binding(self):
        added = textwrap.dedent("""\


            class InfoManager:
                def __init__(self, raw_info, *, metadata=None, **user_data):
                    self.handle = raw_info
                    self.result = metadata


            def load(path):
                result = []
                for line_value in open(path):
                    with line_value as status:
                        data = status
                        data = data
                        result.append(data)
                return result
            """)
        self.put("calc.py", self.BASE + added)
        base = len(self.BASE.splitlines())
        found = [(path, line - base, rule, message) for path, line, rule, message in self.found()]
        self.assertEqual([(line, message.split("`")[1]) for _, line, _, message in found], [
            (4, "raw_info"), (4, "user_data"), (5, "handle"), (11, "line_value"), (12, "status"), (13, "data")])
        self.assertEqual({(path, rule) for path, _, rule, _ in found}, {("calc.py", "vague-name")})
        self.assertEqual(found[0][3], "`raw_info` could name anything; name it for what it holds.")

    def test_every_python_file_is_read_and_no_other(self):
        # notes.txt sorts before calc.py in the diff, so a check that stopped at it would not read calc.py.
        self.put("a_notes.txt", "data = 1\n")
        self.put("calc.py", self.BASE + "\n\nclass C:\n    @classmethod\n    def make(cls):\n        cls.manager = 1\n")
        self.assertEqual([(path, message.split("`")[1]) for path, _, _, message in self.found()],
                         [("calc.py", "manager")])

    def test_imports_excepts_matches_and_type_parameters_bind_names(self):
        self.put("calc.py", self.BASE + textwrap.dedent("""\


            import json as raw_data
            from os import path as status


            def parse[T_value](text: str) -> None:
                try:
                    match json.loads(text):
                        case {"k": handle, **rest_info}:
                            pass
                        case [*all_status]:
                            pass
                except ValueError as info:
                    raise
            """))
        self.assertEqual([message.split("`")[1] for _, _, _, message in self.found()],
                         # A mapping pattern's **rest is placed at the pattern's start, before the captures in it.
                         ["raw_data", "status", "T_value", "rest_info", "handle", "all_status", "info"])

    def test_an_override_s_name_and_parameters_are_its_base_s(self):
        self.put("calc.py", self.BASE + textwrap.dedent("""\


            from typing import override


            class Handler(Base):
                @override
                def handle(self, raw_data):
                    status = raw_data
                    return status
            """))
        self.assertEqual([message.split("`")[1] for _, _, _, message in self.found()], ["status"])

    def test_a_name_on_a_line_the_change_left_alone_is_not_found(self):
        self.put("calc.py", self.BASE.replace("return value", "return value + 1"))
        self.assertEqual(self.found(), [])

    def test_a_class_or_function_named_by_a_vague_word_is_found(self):
        self.put("tests/test_calc.py", "class Manager:\n    pass\n\n\ndef handle():\n    pass\n")
        self.assertEqual([(path, line) for path, line, _, _ in self.found()],
                         [("tests/test_calc.py", 1), ("tests/test_calc.py", 5)])

    def test_an_unparsable_file_is_a_finding(self):
        self.put("calc.py", self.BASE + "def broken(:\n")
        [(path, line, rule, message)] = self.found()
        self.assertEqual((path, line, rule), ("calc.py", None, "not-read"))
        self.assertEqual(message, "does not parse as Python, so its names were not read.")

    def test_it_reports_without_failing_the_gate(self):
        [check] = [c for c in orchestrator.gate_checks() if c.name == "vague-names"]
        self.assertEqual((check.fails, check.find), (False, orchestrator.vague_names))


class TestSurvivingMutants(unittest.TestCase):
    """The surviving-mutants check over a real git repository, with this suite's own mutmut, or a fake one."""

    BASE = (
        "def checked(a, b):\n"          # 1
        "    return b + a\n"            # 2
        "\n"                            # 3
        "\n"                            # 4
        "def unchecked(a, b):\n"        # 5
        "    return b * a\n"            # 6
        "\n"                            # 7
        "\n"                            # 8
        "def untouched(a, b):\n"        # 9
        "    return a - b\n"            # 10
        "\n"                            # 11
        "\n"                            # 12
        "def orphan():\n"               # 13
        "    return 1\n"                # 14
        "\n"                            # 15
        "\n"                            # 16
        "class Box:\n"                  # 17
        "    def size(self):\n"         # 18
        "        return 3\n"            # 19
        "\n"                            # 20
        "    def other(self):\n"        # 21
        "        return 4\n"            # 22
        "\n"                            # 23
        "\n"                            # 24
        "def describe(parser):\n"       # 25
        "    parser.add_argument('--count', help='how many'\n"   # 26
        "                                     ' to make')\n"    # 27
    )
    # The repository's own [tool.mutmut] do_not_mutate_patterns, which keep mutmut off argparse's help texts.
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml"), "rb") as f:
        PATTERNS = tomllib.load(f)["tool"]["mutmut"].get("do_not_mutate_patterns", [])
    # Each test calls one function; only test_checked looks at what it returns.
    TESTS = (
        "import calc\n\n\n"
        "def test_checked():\n    assert calc.checked(2, 3) == 5\n\n\n"
        "def test_unchecked():\n    calc.unchecked(2, 3)\n\n\n"
        "def test_untouched():\n    calc.untouched(2, 3)\n\n\n"
        "def test_box():\n    calc.Box().size()\n    calc.Box().other()\n"
    )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, self.bin = (os.path.join(tmp.name, d) for d in ("repo", "bin"))
        os.mkdir(self.repo)
        os.mkdir(self.bin)
        self.host = Host()
        self.git("init", "--quiet", "-b", "main")
        for key, value in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, value)
        self.put("pyproject.toml", '[tool.mutmut]\nsource_paths = ["calc.py"]\n'
                                   'pytest_add_cli_args_test_selection = ["tests/"]\n'
                                   f'do_not_mutate_patterns = {json.dumps(self.PATTERNS)}\n')
        self.put("calc.py", self.BASE)
        self.put("tests/test_calc.py", self.TESTS)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "base")

    def git(self, *args):
        return self.host.git(self.repo, *args)

    def put(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def found(self, budget=60, rules=None):
        base = self.git("rev-parse", "HEAD").strip()
        return orchestrator.surviving_mutants(orchestrator.GateChange.from_tree(self.repo, base, rules), budget=budget)

    def rules(self, table):
        """A rules file outside the tree, as `gate --config` names one, with table as its [tool.mutmut]."""
        path = os.path.join(self.bin, "rules.toml")
        with open(path, "w") as f:
            f.write("[tool.mutmut]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in table.items()))
        return path

    # How change_the_functions edits each function it edits.
    EDITS = {"checked": ("b + a", "a + b"), "unchecked": ("b * a", "a * b"), "orphan": ("return 1", "return 2"),
             "Box.size": ("return 3", "return 5")}

    def change_the_functions(self, *names):
        """Edit the functions names, by default checked, unchecked, orphan and Box.size, leaving untouched and
        Box.other as they were."""
        text = self.BASE
        for name in names or self.EDITS:
            text = text.replace(*self.EDITS[name])
        self.put("calc.py", text)

    def fake_mutmut(self, run, results="", show=None, listing=None):
        """Make the gate's interpreter a fake that answers `mutmut run` with the shell code run, `results` by
        printing results, or with the shell code listing, and `show NAME` by printing show[NAME]."""
        python = os.path.join(self.bin, "python")
        shows = "".join(f"{shlex.quote(name)}) printf %s {shlex.quote(diff)};;\n"
                        for name, diff in (show or {}).items())
        # The gate starts mutmut as `python -c CODE ARGS`, so the command is the third argument.
        with open(python, "w") as f:
            f.write(f'#!/bin/sh\ncase "$3" in\nrun) {run};;\n'
                    f'results) {listing or "printf %s " + shlex.quote(results)};;\n'
                    f'show) case "$4" in\n{shows}esac;;\nesac\n')
        os.chmod(python, 0o700)
        p = patch.object(orchestrator.sys, "executable", python)
        p.start()
        self.addCleanup(p.stop)

    def test_a_mutant_the_tests_do_not_notice_is_reported_and_a_killed_one_is_not(self):
        self.change_the_functions()
        made, found = self.found_and_made()
        # untouched and Box.other are as weakly tested as unchecked, but the change left them alone, so mutmut made
        # no mutant of them.
        self.assertEqual({re.sub(r"__mutmut_\d+$", "", m) for m in made},
                         {"calc.x_checked", "calc.x_unchecked", "calc.x_orphan", "calc.xǁBoxǁsize"})
        # unchecked's test never looks at what it returns, nor does Box.size's; checked's does.
        self.assertEqual([(f["path"], f["line"], f["severity"], f["rule"]) for f in found],
                         [("calc.py", 5, "warning", "survived"), ("calc.py", 13, "warning", "untested"),
                          ("calc.py", 18, "warning", "survived")])
        self.assertEqual(found[0]["message"], "1 of 1 mutants of `unchecked` survived its tests: line 6, "
                                              "`return a * b` became `return a / b`.")
        self.assertIn("`orphan`", found[1]["message"])
        self.assertIn("1 of 1 mutants of `Box.size` survived its tests: line 19, `return 5` became ",
                      found[2]["message"])
        self.assertEqual(self.git("status", "--porcelain", "--ignored"), " M calc.py\n")

    def test_a_source_file_with_a_byte_order_mark_is_read_as_python_reads_it(self):
        self.change_the_functions()
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            text = f.read()
        with open(os.path.join(self.repo, "calc.py"), "w", encoding="utf-8-sig") as f:
            f.write(text)
        made, found = self.found_and_made()
        # untouched and Box.other are marked to be skipped in the copy as in a file without the mark.
        self.assertEqual({re.sub(r"__mutmut_\d+$", "", m) for m in made},
                         {"calc.x_checked", "calc.x_unchecked", "calc.x_orphan", "calc.xǁBoxǁsize"})
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [("calc.py", 5, "survived"), ("calc.py", 13, "untested"), ("calc.py", 18, "survived")])

    def found_and_made(self, budget=60):
        """found(budget), and the names of the mutants mutmut made."""
        made, list_results = [], orchestrator._mutant_statuses

        def results(*args):
            statuses, problem = list_results(*args)
            made.extend(statuses)
            return statuses, problem
        with patch.object(orchestrator, "_mutant_statuses", results):
            return made, self.found(budget)

    def test_the_rules_mutmut_table_judges_the_change_not_the_tree_s(self):
        self.change_the_functions()
        # The change tells mutmut to leave every line alone; the rules it is judged by do not.
        with open(os.path.join(self.repo, "pyproject.toml"), "a") as f:
            f.write("do_not_mutate_patterns = ['.']\n")
        rules = self.rules({"source_paths": ["calc.py"], "pytest_add_cli_args_test_selection": ["tests/"]})
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found(rules=rules)],
                         [("calc.py", 5, "survived"), ("calc.py", 13, "untested"), ("calc.py", 18, "survived")])

    def test_a_function_outside_source_paths_is_not_mutated_and_said_so(self):
        self.put("tools/extra.py", "def extra():\n    return 1\n")
        self.git("add", "tools/extra.py")
        self.fake_mutmut(f"touch {self.bin}/ran")
        found = self.found()
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("tools/extra.py", 1, "not-mutated")])
        self.assertIn("`extra`", found[0]["message"])
        self.assertIn("outside [tool.mutmut] source_paths", found[0]["message"])

    def test_a_file_do_not_mutate_names_is_not_mutated_and_said_so(self):
        self.put("calc.py", self.BASE.replace("return 1", "return 2"))
        self.fake_mutmut(f"touch {self.bin}/ran")
        rules = self.rules({"source_paths": ["."], "do_not_mutate": ["calc*"]})
        found = self.found(rules=rules)
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 13, "not-mutated")])
        self.assertIn("do_not_mutate", found[0]["message"])

    def test_rules_that_name_no_source_paths_are_a_finding(self):
        self.change_the_functions()
        self.fake_mutmut(f"touch {self.bin}/ran")
        found = self.found(rules=self.rules({"also_copy": ["docs/"]}))
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("source_paths", found[0]["message"])

    def test_a_function_mutmut_does_not_mutate_is_said_so(self):
        # mutmut 3.8.0 mutates no decorated function but one decorated with @staticmethod or @classmethod alone, and
        # never __new__, __setattr__ or __getattribute__.
        text = (self.BASE + "\n\n"
                "import functools\n\n\n"
                "@functools.cache\n"
                "def cached(a):\n    return a + 1\n\n\n"
                "class Made:\n"
                "    def __new__(cls):\n        return super().__new__(cls)\n\n"
                "    @staticmethod\n"
                "    def make():\n        return 1\n")
        self.put("calc.py", text)
        lines = text.split("\n")
        # mutmut is run for Made.make, the one function it mutates, and lists its mutant.
        self.fake_mutmut(f"touch {self.bin}/ran", results="    calc.xǁMadeǁmake__mutmut_1: killed\n")
        found = self.found()
        self.assertTrue(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [("calc.py", lines.index("def cached(a):") + 1, "not-mutated"),
                          ("calc.py", lines.index("    def __new__(cls):") + 1, "not-mutated")])
        self.assertIn("`cached` was not mutation-tested: mutmut does not mutate a decorated function",
                      found[0]["message"])
        self.assertIn("`Made.__new__` was not mutation-tested: mutmut never mutates `__new__`", found[1]["message"])

    def test_a_change_to_a_decorated_function_alone_runs_nothing_and_says_so(self):
        self.put("calc.py", self.BASE + "\n\n@property\ndef shown(self):\n    return 1\n")
        self.fake_mutmut(f"touch {self.bin}/ran")
        self.assertEqual([(f["path"], f["rule"]) for f in self.found()], [("calc.py", "not-mutated")])
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))

    def test_the_budget_counts_tried_mutants_as_each_function_does(self):
        self.change_the_functions("checked", "orphan", "unchecked")
        self.fake_mutmut("exec sleep 30", results="    calc.x_checked__mutmut_1: killed\n"
                                                  "    calc.x_orphan__mutmut_1: no tests\n"
                                                  "    calc.x_unchecked__mutmut_1: not checked\n")
        found = self.found(budget=1)
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "budget-reached"), ("calc.py", 13, "untested")])
        self.assertTrue(found[0]["message"].startswith("budget reached after 1 of 3 mutants"), found[0]["message"])

    def test_a_directory_for_the_copy_that_cannot_be_made_is_a_finding(self):
        self.change_the_functions()
        with patch.object(orchestrator.tempfile, "mkdtemp", side_effect=OSError(28, "No space left on device")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("No space left on device", found[0]["message"])

    def test_a_copy_that_cannot_be_removed_is_a_finding_beside_the_others(self):
        self.change_the_functions("orphan")
        self.fake_mutmut("exit 0", results="    calc.x_orphan__mutmut_1: no tests\n")
        rmtree = shutil.rmtree
        with patch.object(orchestrator.shutil, "rmtree", side_effect=OSError(16, "Device or resource busy")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "cleanup"), ("calc.py", 13, "untested")])
        self.assertIn("Device or resource busy", found[0]["message"])
        left = re.search(r"`([^`]+)`", found[0]["message"]).group(1)
        self.assertTrue(os.path.isdir(left))
        rmtree(left)

    def helper_holding_the_output(self):
        """Shell code that starts a process in a session of its own, which keeps the fake mutmut's output open after
        mutmut's process group is killed, and that the test kills when it ends."""
        pid = os.path.join(self.bin, "helper.pid")

        def kill():
            with contextlib.suppress(FileNotFoundError, ProcessLookupError), open(pid) as f:
                os.kill(int(f.read()), signal.SIGKILL)
        self.addCleanup(kill)
        code = f"import os, time; os.setsid(); open({pid!r}, 'w').write(str(os.getpid())); time.sleep(30)"
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} &"

    def test_a_run_whose_output_outlives_it_ends_within_the_stated_bound(self):
        self.change_the_functions()
        # mutmut ignores SIGINT here, so it is killed after the grace, and a helper keeps its output open after that.
        self.fake_mutmut(f"trap '' INT; {self.helper_holding_the_output()} exec sleep 30")
        started = time.monotonic()
        with patch.multiple(orchestrator, MUTMUT_STOP_GRACE=0.5, MUTMUT_KILL_WAIT=0.5):
            found = self.found(budget=1)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual([f["rule"] for f in found], ["budget-reached"])

    def test_the_report_ends_within_its_time_whatever_mutmut_does(self):
        self.change_the_functions()
        self.fake_mutmut("exit 0", listing="trap '' INT; exec sleep 30")
        started = time.monotonic()
        # Without its stop counted in, the listing would get all 4 seconds, and then the grace on top.
        with patch.multiple(orchestrator, MUTMUT_REPORT_TIMEOUT=4, MUTMUT_STOP_GRACE=1.5, MUTMUT_KILL_WAIT=0.5):
            found = self.found()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([f["rule"] for f in found], ["not-run"])

    def test_a_pytest_configuration_above_the_copy_is_not_read(self):
        # The project has no pytest configuration of its own, so pytest under mutmut would look for one in every
        # directory above the copy, where another account may plant one, and a conftest.py beside it.
        self.change_the_functions()
        above, ran = os.path.join(self.bin, "tmp"), os.path.join(self.bin, "planted-ran")
        os.mkdir(above)
        with open(os.path.join(above, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        with open(os.path.join(above, "conftest.py"), "w") as f:
            f.write(f"open({ran!r}, 'w').close()\n")
        with patch.object(orchestrator.tempfile, "tempdir", above):
            found = self.found()
        self.assertFalse(os.path.exists(ran))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [("calc.py", 5, "survived"), ("calc.py", 13, "untested"), ("calc.py", 18, "survived")])

    def test_the_project_s_own_pytest_configuration_still_applies(self):
        self.change_the_functions()
        # Its addopts run only test_checked, so no test calls the other changed functions. Its pythonpath, relative to
        # the configuration's directory, must name mutmut's copy of the code, or checked's mutants go untested too.
        with open(os.path.join(self.repo, "pyproject.toml"), "a") as f:
            f.write('\n[tool.pytest.ini_options]\naddopts = "-k test_checked"\npythonpath = ["."]\n')
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()],
                         [("calc.py", 5, "untested"), ("calc.py", 13, "untested"), ("calc.py", 18, "untested")])

    def test_a_mutmut_table_of_the_wrong_shape_is_a_finding_and_leaves_nothing_behind(self):
        self.change_the_functions()
        tables = [("only_mutate", "only_mutate = 5"), ("only_mutate", 'only_mutate = "calc.py"'),
                  ("do_not_mutate", "do_not_mutate = [1]"), ("when", "when = 1979-05-27")]
        for key, line in tables:
            with self.subTest(line=line):
                tmp = tempfile.mkdtemp(dir=self.bin)
                self.fake_mutmut(f"touch {self.bin}/ran")
                rules = os.path.join(self.bin, "rules.toml")
                with open(rules, "w") as f:
                    f.write(f'[tool.mutmut]\nsource_paths = ["calc.py"]\n{line}\n')
                with patch.object(orchestrator.tempfile, "tempdir", tmp):
                    found = self.found(rules=rules)
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
                self.assertIn(f"[tool.mutmut] {key}", found[0]["message"])
                self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
                self.assertEqual(os.listdir(tmp), [])

    def test_a_failing_test_that_prints_mutmut_s_words_is_not_taken_for_mutmut(self):
        # The clean run fails, and mutmut prints the test's output before its own "failed to collect stats".
        self.change_the_functions("unchecked")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_loud():\n    calc.unchecked(1, 1)\n"
                                                  "    print('could not find any test case for any mutant')\n"
                                                  "    assert False\n")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("failed to collect stats", found[0]["message"])

    def test_a_failed_run_is_named_by_mutmut_s_message_not_its_config_warning(self):
        # paths_to_mutate makes mutmut warn that it is deprecated, on standard error, before the failing test stops it.
        self.change_the_functions("unchecked")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_failing():\n    assert False\n")
        found = self.found(rules=self.rules({"paths_to_mutate": ["calc.py"],
                                             "pytest_add_cli_args_test_selection": ["tests/"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("exited with status 1: failed to collect stats. runner returned 1", found[0]["message"])

    def test_a_file_whose_coding_cookie_names_no_text_encoding_is_not_read(self):
        # Python finds rot13, a codec of str to str, and undefined, which refuses to decode anything, by name, but
        # decodes no source with either.
        for codec in ("rot13", "undefined"):
            with self.subTest(codec=codec, file="changed"):
                self.put(f"tools/{codec}.py", f"# coding: {codec}\n")
                self.git("add", f"tools/{codec}.py")
                self.git("commit", "--quiet", "-m", codec)
                self.put("calc.py", self.BASE)
                self.put(f"tools/{codec}.py", f"# coding: {codec}\nqrs s():\n    erghea 1\n")
                found = self.found()
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                                 [(f"tools/{codec}.py", None, "not-run")])
                self.assertIn("could not be read", found[0]["message"])
            with self.subTest(codec=codec, file="copied"):
                self.put(f"tools/{codec}.py", f"# coding: {codec}\n")
                self.change_the_functions("checked")
                self.fake_mutmut("exit 0", results="    calc.x_checked__mutmut_1: killed\n")
                self.assertEqual(self.found(), [])
                self.git("checkout", "--quiet", "--", ".")

    def test_a_function_whose_file_the_budget_kept_mutmut_from_is_part_of_the_budget(self):
        # mutmut makes and saves mutants file by file, so a run stopped while making them lists only some files'.
        self.put("more.py", "def g(a):\n    return a + 1\n")
        self.git("add", "more.py")
        self.git("commit", "--quiet", "-m", "more")
        self.put("more.py", "def g(a):\n    return a + 2\n")
        self.change_the_functions("checked")
        self.fake_mutmut("exec sleep 30", results="    calc.x_checked__mutmut_1: not checked\n")
        found = self.found(budget=1, rules=self.rules({"source_paths": ["calc.py", "more.py"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "budget-reached")])
        self.assertIn("mutmut made no mutant of `g` in more.py before the budget ran out", found[0]["message"])

    def test_a_pragma_mutmut_does_not_honour_is_not_given_as_the_cause(self):
        # A bare `no mutate` on the def keeps only that line out; under mutate_only_covered_lines the function no test
        # calls gets no mutant for that reason instead.
        self.put("calc.py", self.BASE.replace("def orphan():", "def orphan():  # pragma: no mutate")
                 .replace("return 1", "return 2"))
        found = self.found(rules=self.rules({"source_paths": ["calc.py"], "mutate_only_covered_lines": True,
                                             "pytest_add_cli_args_test_selection": ["tests/"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 13, "not-mutated")])
        self.assertNotIn("on its def keeps it out", found[0]["message"])
        self.assertIn("either it has nothing mutmut mutates", found[0]["message"])

    def test_a_block_pragma_where_a_long_header_ends_is_given_as_the_cause(self):
        self.put("calc.py", self.BASE.replace("def unchecked(a, b):",
                                              "def unchecked(\n        a, b):  # pragma: no mutate block"))
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 5, "not-mutated")])
        self.assertIn("a `# pragma: no mutate block` where its header ends keeps it out", found[0]["message"])

    def test_a_function_in_a_file_mutmut_listed_mutants_of_keeps_its_cause_after_a_budget_stop(self):
        # The fake saves mutants/calc.py.meta, as mutmut does once it has made calc.py's mutants, which is how the
        # gate knows calc.py was reached.
        self.put("calc.py", self.BASE.replace("b + a", "a + b")
                 .replace("def unchecked(a, b):", "def unchecked(a, b):  # pragma: no mutate block"))
        self.fake_mutmut("mkdir -p mutants; touch mutants/calc.py.meta; exec sleep 30",
                         results="    calc.x_checked__mutmut_1: not checked\n")
        found = self.found(budget=1)
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "budget-reached"), ("calc.py", 5, "not-mutated")])
        self.assertNotIn("before the budget ran out", found[0]["message"])
        self.assertIn("a `# pragma: no mutate block` where its header ends keeps it out", found[1]["message"])

    def test_a_module_mutmut_saved_without_mutants_was_reached(self):
        # mutmut saves a module's .meta once it has made its mutants, none or more.
        self.put("more.py", "def g(a):\n    return a + 1\n")
        self.git("add", "more.py")
        self.git("commit", "--quiet", "-m", "more")
        self.put("more.py", "def g(a):  # pragma: no mutate block\n    return a + 2\n")
        self.change_the_functions("checked")
        self.fake_mutmut("mkdir -p mutants; touch mutants/calc.py.meta mutants/more.py.meta; exec sleep 30",
                         results="    calc.x_checked__mutmut_1: not checked\n")
        found = self.found(budget=1, rules=self.rules({"source_paths": ["calc.py", "more.py"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "budget-reached"), ("more.py", 1, "not-mutated")])
        self.assertNotIn("before the budget ran out", found[0]["message"])
        self.assertIn("a `# pragma: no mutate block` where its header ends keeps it out", found[1]["message"])

    def test_mutmut_s_state_in_the_change_is_left_out_and_listed(self):
        self.change_the_functions("unchecked")
        # The .meta mutmut writes for this change, kept from a first run, with every mutant marked killed.
        kept, run = os.path.join(self.bin, "calc.py.meta"), orchestrator._mutmut_findings

        def keep(tree_copy, *args):
            shutil.copy(os.path.join(tree_copy, "mutants", "calc.py.meta"), kept)
            return run(tree_copy, *args)
        with patch.object(orchestrator, "_mutmut_findings", keep):
            self.assertEqual([f["rule"] for f in self.found()], ["survived"])
        with open(kept) as f:
            meta = json.load(f)
        meta["exit_code_by_key"] = dict.fromkeys(meta["exit_code_by_key"], 1)
        self.put("mutants/calc.py.meta", json.dumps(meta))
        self.git("add", "mutants/calc.py.meta")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 5, "survived")])
        diff = self.host.change_diff(self.repo, self.git("rev-parse", "HEAD").strip())
        self.assertEqual(orchestrator.edited_config(diff, changed_lines(diff)), [])
        self.assertEqual(orchestrator.mutmut_state_files(changed_lines(diff)), ["mutants/calc.py.meta"])

    def test_a_function_under_mutmut_s_state_is_not_selected(self):
        self.put("mutants/calc.py", "def x_checked__mutmut_1(a, b):\n    return a - b\n")
        self.git("add", "mutants/calc.py")
        self.fake_mutmut(f"touch {self.bin}/ran")
        found = self.found(rules=self.rules({"source_paths": ["."]}))
        self.assertEqual(found, [])
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))

    def test_unreached_functions_are_named_with_their_files(self):
        for name in ("a.py", "b.py"):
            self.put(name, "def run():\n    return 1\n")
            self.git("add", name)
        self.git("commit", "--quiet", "-m", "run twice")
        for name in ("a.py", "b.py"):
            self.put(name, "def run():\n    return 2\n")
        self.fake_mutmut("exec sleep 30")
        found = self.found(budget=1, rules=self.rules({"source_paths": ["a.py", "b.py"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "budget-reached")])
        self.assertIn("mutmut made no mutant of `run` in a.py and `run` in b.py before the budget ran out",
                      found[0]["message"])

    def test_a_series_reads_as_a_list_in_prose(self):
        series = orchestrator._series
        self.assertEqual([series([]), series(["a"]), series(["a", "b"]), series(["a", "b", "c"])],
                         ["", "a", "a and b", "a, b and c"])
        self.assertEqual(series(["a", "b", "c", "d"], limit=2), "a, b and 2 more")
        self.assertEqual(series(["a", "b"], limit=2), "a and b")

    def test_a_change_to_a_help_text_alone_makes_no_mutant(self):
        # The second line of the help text does not say help=, but its string starts on the line that does.
        self.put("calc.py", self.BASE.replace(" to make", " to build"))
        made, found = self.found_and_made()
        self.assertEqual(made, [])
        # mutmut made no mutant of describe, and the finding says why rather than nothing.
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 25, "not-mutated")])
        self.assertEqual(found[0]["message"], "`describe` was not mutation-tested: mutmut made no mutant of it; the "
                                              "rules' do_not_mutate_patterns match each statement in it.")

    def test_a_function_a_mutmut_pragma_opts_out_is_said_so_and_its_pragma_listed(self):
        text = self.BASE.replace("def unchecked(a, b):", "def unchecked(a, b):  # pragma: no mutate block")
        self.put("calc.py", text)
        made, found = self.found_and_made()
        self.assertEqual(made, [])
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 5, "not-mutated")])
        self.assertEqual(found[0]["message"], "`unchecked` was not mutation-tested: mutmut made no mutant of it; a "
                                              "`# pragma: no mutate block` where its header ends keeps it out.")
        diff = self.host.change_diff(self.repo, self.git("rev-parse", "HEAD").strip())
        self.assertEqual(orchestrator.added_suppressions(self.repo, changed_lines(diff)),
                         [("calc.py", 5, "def unchecked(a, b):  # pragma: no mutate block")])

    def test_a_change_to_untested_functions_alone_is_reported_as_untested(self):
        # mutmut stops early when no test calls any function it mutated.
        self.put("calc.py", self.BASE.replace("return 1", "return 2"))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()], [("calc.py", 13, "untested")])

    def test_a_change_to_no_function_runs_nothing(self):
        self.put("calc.py", self.BASE + "\nLIMIT = 3\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_more():\n    calc.checked(1, 1)\n")
        self.fake_mutmut(f"touch {self.bin}/ran")
        self.assertEqual(self.found(), [])
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))

    def test_a_run_stopped_by_the_budget_says_so_with_what_it_found(self):
        self.change_the_functions("checked", "unchecked")
        self.fake_mutmut("exec sleep 30",
                         results="    calc.x_checked__mutmut_1: killed\n    calc.x_unchecked__mutmut_1: survived\n"
                                 "    calc.x_unchecked__mutmut_2: not checked\n",
                         show={"calc.x_unchecked__mutmut_1": self.unchecked_mutant("a / b")})
        started = time.monotonic()
        found = self.found(budget=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "budget-reached"), ("calc.py", 5, "survived")])
        self.assertTrue(found[0]["message"].startswith("budget reached after 2 of 3 mutants"), found[0]["message"])
        self.assertTrue(found[1]["message"].startswith("1 of 1 mutants of `unchecked`"), found[1]["message"])

    @staticmethod
    def unchecked_mutant(new):
        """What `mutmut show` prints for a mutant of unchecked that returns new."""
        return (f"# a mutant: survived\n--- calc.py\n+++ calc.py\n@@ -1,2 +1,2 @@\n def unchecked(a, b):\n"
                f"-    return a * b\n+    return {new}\n")

    def test_a_run_that_fails_is_a_finding(self):
        self.change_the_functions()
        self.fake_mutmut("echo 'failed to collect stats. runner returned 1'; exit 1")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("exited with status 1: failed to collect stats. runner returned 1", found[0]["message"])

    def test_a_project_without_mutmut_gets_a_finding_naming_the_import_error(self):
        self.change_the_functions()
        self.fake_mutmut("echo 'Traceback (most recent call last):' >&2; "
                         "echo \"ModuleNotFoundError: No module named 'mutmut'\" >&2; exit 1")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("exited with status 1: ModuleNotFoundError: No module named 'mutmut'", found[0]["message"])

    def test_the_survivors_of_a_function_are_one_finding_with_examples(self):
        self.change_the_functions("unchecked")
        self.fake_mutmut("exit 0", results="    calc.x_unchecked__mutmut_1: survived\n"
                                           "    calc.x_unchecked__mutmut_2: killed\n"
                                           "    calc.x_unchecked__mutmut_3: survived\n",
                         show={"calc.x_unchecked__mutmut_1": self.unchecked_mutant("a / b"),
                               "calc.x_unchecked__mutmut_3": self.unchecked_mutant("None")})
        self.assertEqual(self.found(), [{
            "path": "calc.py", "line": 5, "severity": "warning", "rule": "survived",
            "message": "2 of 3 mutants of `unchecked` survived its tests: line 6, `return a * b` became "
                       "`return a / b`; line 6, `return a * b` became `return None`."}])

    def test_an_example_keeps_a_line_that_holds_a_line_separator_whole(self):
        # Only "\n" ends a line of mutmut's output, as of git's; U+2028 in a string is part of the line.
        self.put("calc.py", self.BASE.replace("return b * a", "return 'a\u2028b'"))
        self.fake_mutmut("exit 0", results="    calc.x_unchecked__mutmut_1: survived\n",
                         show={"calc.x_unchecked__mutmut_1": self.unchecked_mutant("'XXa\u2028bXX'").replace(
                             "return a * b", "return 'a\u2028b'")})
        [finding] = self.found()
        self.assertEqual(finding["message"], "1 of 1 mutants of `unchecked` survived its tests: line 6, "
                                             "`return 'a\u2028b'` became `return 'XXa\u2028bXX'`.")

    def test_a_function_s_first_three_survivors_are_its_examples(self):
        self.change_the_functions("unchecked")
        names = [f"calc.x_unchecked__mutmut_{n}" for n in range(1, 5)]
        self.fake_mutmut("exit 0", results="".join(f"    {name}: survived\n" for name in names),
                         show={name: self.unchecked_mutant(f"{n}") for n, name in enumerate(names)})
        [finding] = self.found()
        self.assertTrue(finding["message"].startswith("4 of 4 mutants of `unchecked` survived its tests: "),
                        finding["message"])
        self.assertEqual(re.findall(r"became `return (\d)`", finding["message"]), ["0", "1", "2"])

    def test_a_mutmut_that_cannot_start_is_a_finding(self):
        self.change_the_functions()
        with patch.object(orchestrator.sys, "executable", os.path.join(self.repo, "no-python")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("could not start mutmut", found[0]["message"])

    def test_an_unparsable_source_file_is_a_finding(self):
        self.put("calc.py", self.BASE + "def broken(:\n")
        self.fake_mutmut(f"touch {self.bin}/ran")
        found = self.found()
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", None, "not-run")])

    def test_it_reports_without_failing_the_gate_within_its_budget(self):
        with patch.object(orchestrator, "surviving_mutants", return_value=[]) as find:
            [check] = [c for c in orchestrator.gate_checks(42) if c.name == "surviving-mutants"]
            check.find("change")
        self.assertFalse(check.fails)
        find.assert_called_once_with("change", budget=42)


# The seven functions S3776 flagged, as orchestrator.py had them before 84e87e7 split them.
S3776_FUNCTIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "s3776_at_84e87e7.py.txt")


def score(source, cls=None):
    """The cognitive complexity of the first function in source, or of its class cls's first method."""
    tree = ast.parse(textwrap.dedent(source))
    if cls is not None:
        [owner] = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls]
        tree = owner
    function = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return orchestrator.cognitive_complexity(function, cls)


class TestCognitiveComplexity(unittest.TestCase):
    """cognitive_complexity, after G. Ann Campbell's paper, in its mapping to Python. Each expected score is
    added up in the comments, one increment per line."""

    def test_loops_and_ifs_add_one_and_their_nesting(self):
        # The paper's sumOfPrimes, whose labelled `continue OUT` Python writes as a loop's else.
        self.assertEqual(score("""
            def sum_of_primes(top):
                total = 0
                for i in range(1, top + 1):  # +1
                    for j in range(2, i):    # +2
                        if i % j == 0:       # +3
                            break
                    else:                    # +1
                        total += i
                return total
        """), 7)

    def test_elif_and_else_add_one_and_nest_their_bodies(self):
        self.assertEqual(score("""
            def f(a):
                if a == 1:      # +1
                    return 1
                elif a == 2:    # +1
                    if a:       # +2
                        return 2
                else:           # +1
                    if a:       # +2
                        return 3
                return 0
        """), 7)

    def test_a_while_and_its_else(self):
        self.assertEqual(score("""
            def f(n):
                while n:      # +1
                    n -= 1
                else:         # +1
                    n = 0
                return n
        """), 2)

    def test_match_adds_one_for_all_its_cases_and_nests_them(self):
        # The paper's getWords: a switch is one increment, however many cases it has.
        self.assertEqual(score("""
            def get_words(n):
                match n:              # +1
                    case 1:
                        return "one"
                    case 2 if n > 0:  # a guard adds nothing of its own
                        return "a couple"
                    case _:
                        if n:         # +2
                            return "lots"
        """), 3)

    def test_except_adds_one_and_its_nesting_while_try_finally_and_with_add_nothing(self):
        self.assertEqual(score("""
            def f(path):
                try:
                    with open(path) as f:  # neither try nor with nests
                        if f:              # +1
                            pass
                except OSError:            # +1
                    if path:               # +2
                        pass
                except ValueError:         # +1
                    pass
                else:                      # +1
                    pass
                finally:
                    if path:               # +1
                        pass
        """), 7)

    def test_each_sequence_of_like_boolean_operators_adds_one(self):
        self.assertEqual(score("""
            def f(a, b, c, d):
                if a and b and c:       # +1 if, +1 and
                    pass
                if a and b or c and d:  # +1 if, +1 and, +1 or, +1 and
                    pass
                x = a or b and c or d   # +1 or, +1 and, +1 or
                y = a or not (b or c)   # +1 or, +1 or: not starts a sequence of its own
                z = (1 if a else 0) or b and c  # +1 or, +1 and, +1 ternary
                return a and (b and c)  # +1: the parentheses do not show in the syntax tree
        """), 15)

    def test_except_and_match_take_their_nesting_too(self):
        self.assertEqual(score("""
            def f(xs):
                for x in xs:            # +1
                    try:
                        pass
                    except ValueError:  # +2
                        pass
                    match x:            # +2
                        case _:
                            pass
        """), 5)

    def test_a_ternary_adds_one_and_its_nesting_and_nests_what_it_holds(self):
        self.assertEqual(score("""
            def f(a, b):
                for x in a:                          # +1
                    y = 1 if x else (2 if b else 3)  # +2, +3
                return y
        """), 6)

    def test_what_a_statement_reads_before_its_body_counts_at_the_statement_s_level(self):
        self.assertEqual(score("""
            def f(xs, key=lambda x: 1 if x else 0):  # the judged function's own defaults count for nothing
                for x in (xs if xs else []):         # +1 for, +1 ternary
                    while (x if x else 0):           # +2 while, +2 ternary
                        if (x if x else 0):          # +3 if, +3 ternary
                            x -= 1
                        elif x:                      # +1
                            x += 1
                match (xs if xs else None):          # +1 match, +1 ternary
                    case _:
                        pass
                try:
                    pass
                except (ValueError if xs else TypeError):  # +1 except, +1 ternary
                    pass
                def g(y=(1 if xs else 2)):           # +1 ternary
                    return y
                h = lambda y=(1 if xs else 2): y     # +1 ternary
                return g, h
        """), 19)

    def test_comprehensions_add_nothing_and_lambdas_and_nested_functions_nest(self):
        self.assertEqual(score("""
            def f(xs):
                ys = [x for x in xs if x for y in x if not y]
                key = lambda x: 1 if x else 2          # +2
                def inner(x):
                    if x:                              # +2
                        return [y for y in x if y and x]  # +1
                    return inner(x - 1)                # +1, recursion
                class Box:
                    def size(self):
                        return 3 if self else 4        # +2
                return sorted(ys, key=key), inner, Box
        """), 8)

    def test_a_function_that_calls_itself_adds_one(self):
        self.assertEqual(score("""
            def walk(node):
                if node is None:                      # +1
                    return 0
                return walk(node.left) + walk(node.right)  # +1, once
        """), 2)

    def test_a_method_calls_itself_through_self_cls_or_its_class(self):
        source = """
            class Tree:
                def depth(self, node):
                    return max(self.depth(c) for c in node)  # +1
                def size(self, node):
                    return Tree.size(self, node)            # +1
                def leaves(self, node):
                    return leaves(node)                     # a module-level function, not this method
                @classmethod
                def build(cls, n):
                    return cls.build(n - 1)                 # +1
        """
        self.assertEqual(score(source, "Tree"), 1)
        tree = ast.parse(textwrap.dedent(source)).body[0]
        self.assertEqual([orchestrator.cognitive_complexity(m, "Tree") for m in tree.body], [1, 1, 0, 1])

    def test_a_method_of_a_class_nested_in_the_function_recurses_as_a_method_does(self):
        self.assertEqual(score("""
            def outer():
                class K:
                    def m(self):
                        return m()       # a module-level m, not this method
                return K
        """), 0)
        self.assertEqual(score("""
            def outer():
                class K:
                    def m(self):
                        if self:
                            return self.m()  # +1 recursion
                        return K.m(self)
                return K
        """), 3)  # the if, nested in m: +2

    def test_code_deeper_than_python_s_recursion_limit_is_scored(self):
        self.assertEqual(score(LONG_SUM), 0)
        self.assertEqual(score(LONG_ELIF), 1001)  # the if, and one for each elif

    def fingerprint(self, source):
        return orchestrator._body_fingerprint(ast.parse(textwrap.dedent(source)).body[0])

    def test_a_body_fingerprint_keeps_where_each_absent_part_sits(self):
        # Dict.keys holds None for a ** entry, and kw_defaults None for a keyword-only argument without a default.
        self.assertNotEqual(self.fingerprint("def f(a, b, c):\n    return {**a, b: c}\n"),
                            self.fingerprint("def f(a, b, c):\n    return {b: a, **c}\n"))
        self.assertNotEqual(self.fingerprint("def f():\n    def g(*, a=1, b):\n        pass\n"),
                            self.fingerprint("def f():\n    def g(*, a, b=1):\n        pass\n"))

    def test_a_body_fingerprint_takes_an_integer_too_long_to_print_in_decimal(self):
        # 4000 hex digits are about 4800 decimal ones, past the 4300 that int's repr allows.
        huge = "def f():\n    return 0x" + "f" * 4000 + "\n"
        self.assertNotEqual(self.fingerprint(huge), self.fingerprint(huge.replace("0xf", "0xe", 1)))

    def test_the_seven_functions_sonarqube_flagged_score_over_the_limit(self):
        with open(S3776_FUNCTIONS) as f:
            tree = ast.parse(f.read())
        scores = {}
        for node in tree.body:
            cls = node.name if isinstance(node, ast.ClassDef) else None
            for function in node.body if cls else [node]:
                scores[function.name] = orchestrator.cognitive_complexity(function, cls)
        self.assertEqual(sorted(scores), sorted(["main", "_turn", "_poll", "quality_report", "changed_lines",
                                                 "_shared_name", "_parse_step"]))
        for name, n in scores.items():
            self.assertGreater(n, orchestrator.COGNITIVE_COMPLEXITY_LIMIT, name)


# Valid Python whose syntax tree is deeper than the interpreter's recursion limit lets a recursive walk go: a sum of 350
# terms, and an if with 1000 elifs.
LONG_SUM = "def long_sum(a):\n    return " + " + ".join(["a"] * 350) + "\n"
LONG_ELIF = ("def long_elif(x):\n    if x == 0:\n        pass\n"
             + "".join(f"    elif x == {k}:\n        pass\n" for k in range(1, 1001)))


def flat(name, n, indent=""):
    """A function of n ifs one after the other, so of cognitive complexity n, which returns at its last line."""
    body = "".join(f"{indent}    if x == '{name} {k}':\n{indent}        x += {k}\n" for k in range(n))
    return f"{indent}def {name}(x):\n{body}{indent}    return x\n"


class TestTooComplexFunctions(unittest.TestCase):
    """The cognitive-complexity check over a real git repository: a base commit, then the change in its working tree."""

    BASE = flat("grow", 14) + "\n\n" + flat("big", 20) + "\n\n" + flat("small", 3)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.host = Host()
        self.git("init", "--quiet", "-b", "main")
        for key, value in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, value)
        self.put("calc.py", self.BASE)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "base")

    def git(self, *args):
        return self.host.git(self.repo, *args)

    def put(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def edit(self, name, old, new):
        with open(os.path.join(self.repo, name)) as f:
            text = f.read()
        self.assertEqual(text.count(old), 1, old)
        self.put(name, text.replace(old, new))

    def change(self):
        return orchestrator.GateChange.from_tree(self.repo, self.git("rev-parse", "HEAD").strip())

    def found(self):
        """(path, line, rule) of each finding on the working tree's change, and the findings themselves."""
        found = orchestrator.too_complex_functions(self.change())
        return [(f["path"], f["line"], f["rule"]) for f in found], found

    def def_line(self, name, path="calc.py"):
        with open(os.path.join(self.repo, path)) as f:
            return next(n for n, line in enumerate(f, 1) if line.lstrip().startswith(f"def {name}("))

    def nest_under(self, name, k):
        """Add an if inside the kth if of calc.py's function name: 2 more for it, at nesting 1."""
        self.edit("calc.py", f"    if x == '{name} {k}':\n        x += {k}\n",
                  f"    if x == '{name} {k}':\n        x += {k}\n        if x:\n            x -= 1\n")

    def test_an_unchanged_tree_has_no_findings(self):
        self.assertEqual(self.found()[0], [])

    def test_nesting_added_inside_a_function_is_found_at_its_def_line_which_the_change_does_not_add(self):
        self.nest_under("grow", 13)  # its last if: 14 becomes 16
        where, found = self.found()
        self.assertEqual(where, [("calc.py", 1, "over-limit")])
        self.assertNotIn(1, self.change().changed["calc.py"])
        self.assertEqual(found[0]["message"], "`grow`: cognitive complexity 16, 14 at the base; the limit is 15.")
        self.assertEqual(found[0]["severity"], "error")

    def test_a_function_over_the_limit_whose_score_the_change_leaves_alone_is_not_found(self):
        self.edit("calc.py", "        x += 19\n", "        x += 190\n")  # big's last if, still 20
        self.assertEqual(self.found()[0], [])

    def test_a_function_over_the_limit_whose_score_the_change_raises_is_found(self):
        self.nest_under("big", 19)  # its last if: 20 becomes 22
        where, found = self.found()
        self.assertEqual(where, [("calc.py", self.def_line("big"), "over-limit")])
        self.assertEqual(found[0]["message"], "`big`: cognitive complexity 22, 20 at the base; the limit is 15.")

    def test_a_score_that_rises_to_the_limit_is_not_found(self):
        self.edit("calc.py", "    if x == 'grow 13':\n", "    if x == 'grow 13' and x:\n")  # grow: 14 becomes 15
        self.assertEqual(self.found()[0], [])

    def test_a_new_function_over_the_limit_is_found_and_one_at_it_is_not(self):
        self.put("calc.py", self.BASE + "\n\n" + flat("new_big", 16) + "\n\n" + flat("new_ok", 15))
        where, found = self.found()
        self.assertEqual(where, [("calc.py", self.def_line("new_big"), "over-limit")])
        self.assertEqual(found[0]["message"], "`new_big`: cognitive complexity 16, a new function; the limit is 15.")

    def test_a_method_is_named_and_scored_with_its_class(self):
        # 15 ifs, and a call of itself through self: 16.
        self.put("shapes.py", "class Box:\n" + flat("size", 15, indent="    ").replace("return x", "return self.size(x)"))
        self.git("add", "shapes.py")
        where, found = self.found()
        self.assertEqual(where, [("shapes.py", 2, "over-limit")])
        self.assertTrue(found[0]["message"].startswith("`Box.size`: cognitive complexity 16, a new function"))

    def test_a_method_is_scored_with_its_class_at_the_base_too(self):
        # 15 ifs, and a call of itself through self: 16 at the base and after an edit that adds no point.
        self.put("shapes.py", "class Box:\n" + flat("size", 15, indent="    ").replace("return x", "return self.size(x)"))
        self.git("add", "shapes.py")
        self.git("commit", "--quiet", "-m", "box")
        self.edit("shapes.py", "x += 3\n", "x += 30\n")
        self.assertEqual(self.found()[0], [])

    def test_an_unchanged_function_moved_and_renamed_is_compared_with_its_old_self(self):
        big = flat("big", 20)
        self.edit("calc.py", "\n\n" + big, "")
        self.put("other.py", big.replace("def big(", "def huge("))
        self.git("add", "other.py")
        self.assertEqual(self.found()[0], [])

    def test_an_unchanged_function_moved_out_of_a_deleted_file_is_compared_with_its_old_self(self):
        self.git("rm", "--quiet", "calc.py")
        # Lines enough of its own that git does not take other.py for calc.py renamed.
        self.put("other.py", "".join(f"CONSTANT_{k} = {k}\n" for k in range(200)) + "\n\n" + flat("big", 20))
        self.git("add", "other.py")
        self.assertIn(("calc.py", None), orchestrator._changed_files(self.repo, self.git("rev-parse", "HEAD").strip()))
        self.assertEqual(self.found()[0], [])

    def test_an_unchanged_function_made_a_method_in_its_file_is_compared_with_its_old_self(self):
        big = flat("big", 20)
        self.edit("calc.py", big, "class Box:\n" + flat("big", 20, indent="    "))
        self.assertEqual(self.found()[0], [])

    def test_a_copy_of_a_function_over_the_limit_is_new_wherever_it_goes(self):
        copy = flat("big", 20).replace("def big(", "def copied(")
        new = ["`copied`: cognitive complexity 20, a new function; the limit is 15."]
        with self.subTest("beside the original, in its file"):
            self.put("calc.py", self.BASE + "\n\n" + copy)
            self.assertEqual([f["message"] for f in self.found()[1]], new)
        self.put("calc.py", self.BASE)
        self.put("other.py", copy)
        self.git("add", "other.py")
        with self.subTest("in another file, the original's file untouched"):
            self.assertEqual([f["message"] for f in self.found()[1]], new)
        with self.subTest("in another file, the original's file edited elsewhere"):
            self.edit("calc.py", "'small 2':\n        x += 2\n", "'small 2':\n        x += 20\n")
            self.assertEqual([f["message"] for f in self.found()[1]], new)

    def test_a_function_moved_and_made_more_complex_is_judged_as_new(self):
        big = flat("big", 20)
        self.edit("calc.py", "\n\n" + big, "")
        self.put("other.py", big.replace("        x += 19\n", "        x += 19\n        if x:\n            x -= 1\n"))
        self.git("add", "other.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`big`: cognitive complexity 22, a new function; the limit is 15."])

    def test_a_file_renamed_from_any_name_is_compared_with_its_base(self):
        self.commit_file("bin/tool", flat("helper", 20))
        self.git("mv", "bin/tool", "bin/tool.py")
        self.edit("bin/tool.py", "x += 3\n", "x += 30\n")  # still 20
        self.assertEqual(self.found()[0], [])

    def test_a_function_out_of_tests_is_judged_as_new_as_tests_were_never_held_to_the_limit(self):
        self.commit_file("tests/helpers.py", flat("helper", 20) + "\n\n" + flat("other_helper", 20))
        os.makedirs(os.path.join(self.repo, "lib"))
        self.git("mv", "tests/helpers.py", "lib/helpers.py")  # renamed, with both functions
        self.edit("lib/helpers.py", "'helper 3':\n        x += 3\n", "'helper 3':\n        x += 30\n")  # still 20
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`helper`: cognitive complexity 20, a new function; the limit is 15."])
        self.git("commit", "--quiet", "-am", "renamed")
        self.put("lib/helpers.py", flat("helper", 20))
        self.put("lib/more.py", flat("other_helper", 20))  # moved out of the old test file's lines
        self.git("add", "lib/more.py")
        self.assertEqual(self.found()[0], [])  # lib/helpers.py's base is lib/, now held to the limit

    def test_a_function_moved_unchanged_out_of_tests_is_judged_as_new(self):
        self.commit_file("tests/helpers.py", "X = 0\n\n\n" + flat("helper", 20))
        self.put("tests/helpers.py", "X = 0\n")
        self.put("lib.py", flat("helper", 20))
        self.git("add", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`helper`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_moved_unchanged_into_tests_keeps_its_base_from_a_copy_elsewhere(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("tests/helpers.py", big)  # the original, moved where nothing is judged
        self.put("lib.py", big.replace("def big(", "def copied("))
        self.git("add", "tests/helpers.py", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`copied`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_moved_unchanged_where_nothing_is_judged_keeps_its_base_from_a_copy_of_its_name(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("tests/helpers.py", big)  # the original, moved where nothing is judged
        self.put("lib.py", big)  # a copy under the same name
        self.git("add", "tests/helpers.py", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`big`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_moved_unchanged_into_a_python_script_keeps_its_base_from_a_copy_elsewhere(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("bin/tool", "#!/usr/bin/env python3\n" + big)  # the original, in a script with no .py
        self.put("lib.py", big.replace("def big(", "def copied("))
        self.git("add", "bin/tool", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`copied`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_s_text_moved_into_a_file_that_is_not_python_leaves_its_base_to_its_copy(self):
        big = flat("big", 20)
        for name, first in [("notes.txt", "# python, kept for reference\n"), ("bin/run", "#!/bin/sh\n")]:
            with self.subTest(name):
                self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
                self.put(name, first + big)
                self.put("lib.py", big.replace("def big(", "def copied("))  # the only function with big's body
                self.git("add", name, "lib.py")
                self.assertEqual(self.found()[0], [])
                self.git("rm", "--quiet", "--cached", name)
                os.remove(os.path.join(self.repo, name))

    def test_a_function_in_a_file_the_check_does_not_judge_keeps_its_own_base_from_a_moved_twin(self):
        big = flat("big", 20)
        self.commit_file("tests/helpers.py", "X = 0\n\n\n" + big)
        self.edit("tests/helpers.py", "X = 0\n", "X = 1\n")
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("lib.py", big)  # calc.py's big, moved unchanged
        self.git("add", "lib.py")
        self.assertEqual(self.found()[0], [])

    def test_a_changed_symlink_is_not_read_so_one_to_a_fifo_cannot_block_the_check(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        fifo = os.path.join(elsewhere.name, "pipe")
        os.mkfifo(fifo)
        os.symlink(fifo, os.path.join(self.repo, "x.py"))
        self.git("add", "x.py")
        self.nest_under("big", 19)
        where = []
        worker = threading.Thread(target=lambda: where.extend(self.found()[0]), daemon=True)
        worker.start()
        worker.join(10)
        if worker.is_alive():
            # Open the FIFO's other end and close it, so that the read blocked on it ends and the suite goes on.
            with contextlib.suppress(OSError):
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(10)
            self.fail("the check read the FIFO a changed symlink points to")
        self.assertEqual(where, [("calc.py", self.def_line("big"), "over-limit")])

    def test_a_module_renamed_with_a_symlink_left_under_its_old_name_keeps_its_functions_bases(self):
        self.git("mv", "calc.py", "core.py")
        os.symlink("core.py", os.path.join(self.repo, "calc.py"))
        self.git("add", "calc.py")
        self.assertEqual(self.found()[0], [])

    def test_a_changed_file_gone_before_it_is_read_could_not_be_read(self):
        self.nest_under("big", 19)
        change = self.change()
        os.remove(os.path.join(self.repo, "calc.py"))
        found = orchestrator.too_complex_functions(change)
        self.assertEqual([(f["path"], f["rule"]) for f in found], [("calc.py", "not-scored")])
        self.assertTrue(found[0]["message"].startswith("could not be read ("), found[0]["message"])

    def test_an_unchanged_copy_in_a_file_the_check_does_not_judge_takes_no_base_from_its_original(self):
        big = flat("big", 20)
        self.commit_file("tests/helpers.py", "X = 0\n\n\n" + big)
        self.edit("calc.py", "'big 3':\n        x += 3\n", "'big 3':\n        x += 30\n")  # edited in place: still 20
        self.edit("tests/helpers.py", "X = 0\n", "X = 1\n")  # the copy at the base, in a file edited elsewhere
        self.assertEqual(self.found()[0], [])

    def test_an_unchanged_copy_in_a_file_the_check_does_not_judge_takes_no_base_from_its_original_renamed(self):
        self.put("tests/helpers.py", flat("big", 20))
        self.git("add", "tests/helpers.py")
        self.edit("calc.py", "def big(", "def legacy_big(")  # renamed in place, its body unchanged
        self.assertEqual(self.found()[0], [])

    def test_a_copy_of_its_name_takes_no_base_from_an_original_renamed_in_place(self):
        big = flat("big", 20)
        self.edit("calc.py", "def big(", "def legacy_big(")  # renamed in place, its body unchanged
        self.put("lib.py", big)
        self.git("add", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`big`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_moved_unchanged_keeps_its_base_from_a_new_function_under_its_old_name(self):
        new_big = flat("big", 18).replace("'big ", "'new big ")
        for where in ["lib.py", "tests/helpers.py", "bin/tool"]:
            with self.subTest(where):
                self.put("calc.py", self.BASE.replace(flat("big", 20), new_big))
                self.put(where, ("#!/usr/bin/env python3\n" if where == "bin/tool" else "") + flat("big", 20))
                self.git("add", where)
                self.assertEqual([f["message"] for f in self.found()[1]],
                                 ["`big`: cognitive complexity 18, a new function; the limit is 15."])
                self.git("rm", "--quiet", "--cached", where)
                os.remove(os.path.join(self.repo, where))

    def test_a_function_moved_unchanged_out_of_a_file_that_keeps_its_name_keeps_its_base_from_a_copy(self):
        # Two functions named _, as @singledispatch registrations are: the one scoring 20 moves where nothing is
        # judged, the other stays, and a copy of the first is added elsewhere.
        moved, stays = flat("_", 20).replace("'_ ", "'moved "), flat("_", 3).replace("'_ ", "'stays ")
        self.commit_file("disp.py", moved + "\n\n" + stays)
        self.put("disp.py", stays)
        self.put("tests/helpers.py", moved)
        self.put("lib.py", moved)
        self.git("add", "tests/helpers.py", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`_`: cognitive complexity 20, a new function; the limit is 15."])
        self.assertEqual(self.found()[0][0][0], "lib.py")

    def test_a_function_moved_unchanged_out_of_a_file_that_keeps_its_twin_keeps_its_base(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE + "\n\n" + big.replace("def big(", "def twin("))
        self.git("commit", "--quiet", "-am", "twin")
        self.edit("calc.py", "\n\n" + big.replace("def big(", "def twin("), "")
        self.put("lib.py", big.replace("def big(", "def twin("))  # twin moved unchanged; big stays as it was
        self.git("add", "lib.py")
        self.assertEqual(self.found()[0], [])

    def test_a_copy_of_a_function_the_change_edits_takes_its_base_as_bodies_first_decides(self):
        # The cost TODO.md records ("A copy of a function whose original the change edits keeps the old score and
        # passes"): the copy is taken for the original moved unchanged, and the edited original for a new function.
        big = flat("big", 20)
        self.edit("calc.py", "'big 3':\n        x += 3\n", "'big 3':\n        x += 30\n")  # edited in place: still 20
        for where in ["lib.py", "tests/helpers.py"]:
            with self.subTest(where):
                self.put(where, big)
                self.git("add", where)
                self.assertEqual([f["message"] for f in self.found()[1]],
                                 ["`big`: cognitive complexity 20, a new function; the limit is 15."])
                self.assertEqual(self.found()[0][0][0], "calc.py")
                self.git("rm", "--quiet", "--cached", where)
                os.remove(os.path.join(self.repo, where))

    def test_a_function_whose_decorator_the_change_adds_is_compared_with_its_unchanged_self(self):
        self.edit("calc.py", "def big(x):\n", "@staticmethod\ndef big(x):\n")  # touched, its body as it was
        self.assertEqual(self.found()[0], [])

    def test_a_function_renamed_unchanged_keeps_its_base_from_a_new_function_under_its_old_name(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE.replace(big, big.replace("def big(", "def legacy_big(")) + "\n\n"
                 + flat("big", 16).replace("'big ", "'new big "))
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`big`: cognitive complexity 16, a new function; the limit is 15."])

    def test_one_base_function_is_the_base_of_one_function_only(self):
        big = flat("big", 20)
        copy = big.replace("def big(", "def copied(")
        # The original moved unchanged, and a copy of it elsewhere.
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("other.py", big)
        self.put("third.py", copy)
        self.git("add", "other.py", "third.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`copied`: cognitive complexity 20, a new function; the limit is 15."])
        # The copy in a file read before the original's new one, so that only the name decides.
        os.rename(os.path.join(self.repo, "third.py"), os.path.join(self.repo, "a_copy.py"))
        self.git("add", "-A", "third.py", "a_copy.py")
        self.assertEqual(self.found()[0], [("a_copy.py", 1, "over-limit")])

    def test_a_name_defined_twice_is_compared_with_the_same_definition_at_the_base(self):
        # A property's getter, scoring 21, then its setter, which scores nothing, under the same name.
        getter = flat("x", 21, indent="    ").replace("    def x(x):\n", "    @property\n    def x(self):\n")
        self.put("props.py", "class C:\n" + getter + "\n    @x.setter\n    def x(self, value):\n        pass\n")
        self.git("add", "props.py")
        self.git("commit", "--quiet", "-m", "property")
        self.edit("props.py", "x += 3\n", "x += 30\n")  # the getter's, still 21
        self.assertEqual(self.found()[0], [])
        self.edit("props.py", "x += 30\n", "x += 30\n        if x:\n            x -= 1\n")  # 21 becomes 22
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`C.x`: cognitive complexity 22, 21 at the base; the limit is 15."])

    # A function named _ that does nothing, as one @singledispatch registration or @overload stub can be.
    TRIVIAL = "def _(a):\n    return a\n\n\n"

    def commit_file(self, name, text):
        self.put(name, text)
        self.git("add", name)
        self.git("commit", "--quiet", "-m", name)

    def test_a_namesake_removed_above_a_function_leaves_it_compared_with_its_own_base(self):
        self.commit_file("disp.py", self.TRIVIAL + flat("_", 20))
        self.put("disp.py", flat("_", 20).replace("x += 3\n", "x += 30\n"))  # the trivial one gone; still 20
        self.assertEqual(self.found()[0], [])

    def test_a_namesake_added_above_a_function_leaves_it_compared_with_its_own_base(self):
        self.commit_file("disp.py", flat("_", 20))
        self.put("disp.py", self.TRIVIAL + flat("_", 20).replace("x += 3\n", "x += 30\n"))  # still 20
        self.assertEqual(self.found()[0], [])

    def test_a_rise_of_a_function_whose_name_others_share_is_found(self):
        self.commit_file("disp.py", flat("_", 20))
        self.put("disp.py", flat("_", 20).replace("        x += 19\n", "        x += 19\n        if x:\n            x -= 1\n"))
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`_`: cognitive complexity 22, 20 at the base; the limit is 15."])

    def test_only_the_tests_directory_is_exempt_as_from_ruff_s_c901(self):
        self.put("tests/test_calc.py", flat("test_big", 16))
        self.put("tests/unit/helpers.py", flat("helper", 16))
        self.put("test_top.py", flat("test_big", 16))
        self.put("src/test_utils.py", flat("util", 16))
        self.git("add", "tests", "test_top.py", "src")
        self.assertEqual(self.found()[0], [("src/test_utils.py", 1, "over-limit"), ("test_top.py", 1, "over-limit")])

    def test_methods_of_nested_classes_and_functions_under_module_level_statements_are_judged(self):
        self.put("more.py", (
            "class Outer:\n"
            "    class Inner:\n"
            + flat("method", 16, indent="        ")
            + "if True:\n"
            + flat("under_if", 16, indent="    ")
            + "try:\n"
            "    import json\n"
            "except ImportError:\n"
            + flat("under_except", 16, indent="    ")))
        self.git("add", "more.py")
        where, found = self.found()
        self.assertEqual(where, [("more.py", self.def_line(name, "more.py"), "over-limit")
                                 for name in ("method", "under_if", "under_except")])
        self.assertEqual([f["message"].split(":")[0] for f in found],
                         ["`Outer.Inner.method`", "`under_if`", "`under_except`"])

    def test_a_renamed_file_is_compared_with_its_base_under_its_old_name(self):
        self.git("mv", "calc.py", "lib.py")
        self.edit("lib.py", "        x += 19\n", "        x += 190\n")
        self.assertEqual(self.found()[0], [])

    def test_an_unparsable_file_is_a_finding(self):
        self.put("calc.py", self.BASE + "def broken(:\n")
        where, found = self.found()
        self.assertEqual(where, [("calc.py", None, "not-scored")])
        self.assertEqual(found[0]["message"], "does not parse as Python, so its functions were not scored.")

    def test_files_that_cannot_be_read_or_parsed_are_findings_and_the_others_are_still_judged(self):
        self.put("a_unknown.py", "# -*- coding: nonsense -*-\n")  # an encoding Python does not know
        self.put("b_broken.py", "def broken(:\n")
        self.put("0_notes.txt", "not Python\n")
        self.git("add", "a_unknown.py", "b_broken.py", "0_notes.txt")
        self.nest_under("big", 19)
        where, found = self.found()
        self.assertEqual(where, [("a_unknown.py", None, "not-scored"), ("b_broken.py", None, "not-scored"),
                                 ("calc.py", self.def_line("big"), "over-limit")])
        self.assertTrue(found[0]["message"].startswith("could not be read ("), found[0]["message"])

    def test_a_base_that_cannot_be_read_leaves_its_functions_judged_as_new(self):
        self.nest_under("big", 19)
        git = orchestrator._git

        def no_blob(cwd, *args, **kw):
            if "cat-file" in args:
                raise orchestrator.GateError("git cat-file failed")
            return git(cwd, *args, **kw)

        with patch.object(orchestrator, "_git", no_blob):
            where, found = self.found()
        self.assertEqual(where, [("calc.py", self.def_line("big"), "over-limit")])
        self.assertEqual(found[0]["message"], "`big`: cognitive complexity 22, a new function; the limit is 15.")

    def test_a_function_in_mutmut_s_state_is_not_judged(self):
        self.put("mutants/calc.py", flat("x_big__mutmut_1", 20) + "\n")
        self.git("add", "mutants/calc.py")
        self.nest_under("big", 19)
        where, _ = self.found()
        self.assertEqual(where, [("calc.py", self.def_line("big"), "over-limit")])

    def put_bytes(self, name, data):
        with open(os.path.join(self.repo, name), "wb") as f:
            f.write(data)

    def test_a_file_with_a_byte_order_mark_or_a_coding_cookie_is_judged_as_python_reads_it(self):
        self.put_bytes("bom.py", b"\xef\xbb\xbf" + flat("marked", 16).encode())
        self.put_bytes("latin.py", b"# -*- coding: latin-1 -*-\nCAFE = 'caf\xe9'\n" + flat("declared", 16).encode())
        self.git("add", "bom.py", "latin.py")
        self.assertEqual(self.found()[0], [("bom.py", 1, "over-limit"), ("latin.py", 3, "over-limit")])

    def test_a_file_whose_coding_cookie_names_no_text_encoding_could_not_be_read(self):
        # rot13 is a codec, but not one that decodes bytes to text; Python refuses such a file.
        self.put("rot.py", "# coding: rot13\n" + flat("rotated", 16))
        self.git("add", "rot.py")
        where, found = self.found()
        self.assertEqual(where, [("rot.py", None, "not-scored")])
        self.assertTrue(found[0]["message"].startswith("could not be read ("), found[0]["message"])

    def test_a_base_whose_coding_cookie_names_no_text_encoding_leaves_its_functions_judged_as_new(self):
        self.put("rot.py", "# coding: rot13\n" + flat("rotated", 20))
        self.git("add", "rot.py")
        self.git("commit", "--quiet", "-m", "rot13")
        self.put("rot.py", flat("rotated", 20).replace("x += 3\n", "x += 30\n"))
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`rotated`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_base_with_a_byte_order_mark_or_a_coding_cookie_is_compared_as_python_reads_it(self):
        bom, cookie = b"\xef\xbb\xbf", b"# -*- coding: latin-1 -*-\nCAFE = 'caf\xe9'\n"
        self.put_bytes("bom.py", bom + flat("marked", 20).encode())
        self.put_bytes("latin.py", cookie + flat("declared", 20).encode())
        self.git("add", "bom.py", "latin.py")
        self.git("commit", "--quiet", "-m", "encodings")
        # Edits that leave both at 20.
        self.put_bytes("bom.py", bom + flat("marked", 20).replace("x += 3\n", "x += 30\n").encode())
        self.put_bytes("latin.py", cookie + flat("declared", 20).replace("x += 3\n", "x += 30\n").encode())
        self.assertEqual(self.found()[0], [])

    def test_a_base_that_does_not_parse_leaves_its_functions_judged_as_new(self):
        self.put("broken.py", "def big(:\n")
        self.git("add", "broken.py")
        self.git("commit", "--quiet", "-m", "broken")
        self.put("broken.py", flat("big", 16))
        where, found = self.found()
        self.assertEqual(where, [("broken.py", 1, "over-limit")])
        self.assertEqual(found[0]["message"], "`big`: cognitive complexity 16, a new function; the limit is 15.")

    def test_code_deeper_than_python_s_recursion_limit_is_judged_in_the_change_and_at_the_base(self):
        self.put("deep.py", LONG_SUM + "\n\n" + LONG_ELIF)
        self.git("add", "deep.py")
        self.assertEqual(self.found()[0], [("deep.py", self.def_line("long_elif", "deep.py"), "over-limit")])
        self.git("commit", "--quiet", "-m", "deep")
        self.edit("deep.py", "    elif x == 1000:\n        pass\n",
                  "    elif x == 1000:\n        pass\n    else:\n        pass\n")  # 1001 becomes 1002
        found = self.found()[1]
        self.assertEqual([f["message"] for f in found],
                         ["`long_elif`: cognitive complexity 1002, 1001 at the base; the limit is 15."])

    def test_an_integer_too_long_to_print_in_decimal_is_judged_like_any_code(self):
        self.put("huge.py", "def h():\n    return 0x" + "f" * 4000 + "\n\n\n" + flat("big_one", 16))
        self.git("add", "huge.py")
        self.assertEqual(self.found()[0], [("huge.py", 5, "over-limit")])

    def test_it_fails_the_gate(self):
        [check] = [c for c in orchestrator.gate_checks(orchestrator.MUTATION_BUDGET) if c.name == "cognitive-complexity"]
        self.assertEqual((check.fails, check.find), (True, orchestrator.too_complex_functions))


class TestToolFindings(unittest.TestCase):
    """What each linter really prints, as captured from it, read into the quality file's findings."""

    TRACKED = {"orchestrator.py", "README.md", "a.sh", ".github/workflows/w.yml"}

    def read(self, name, output):
        tool = next(t for t in orchestrator.GATE_TOOLS if t.name == name)
        return orchestrator.tool_findings(tool, output, "/repo", self.TRACKED)

    def test_ruff(self):
        self.assertEqual(self.read("ruff", "orchestrator.py:595:18: F402 Import `field` from line 55 shadowed\n"
                                           "orchestrator.py:1:8: invalid-syntax: Expected an expression\nFound 2 errors.\n"),
                         [{"path": "orchestrator.py", "line": 595, "severity": "error", "rule": "ruff F402",
                           "message": "Import `field` from line 55 shadowed"},
                          {"path": "orchestrator.py", "line": 1, "severity": "error", "rule": "ruff invalid-syntax",
                           "message": "Expected an expression"}])

    def test_mypy_drops_notes(self):
        self.assertEqual(self.read("mypy", 'orchestrator.py:594:5: error: Need type annotation for "tokens"  '
                                           '[var-annotated]\norchestrator.py:594:5: note: See https://x\n'),
                         [{"path": "orchestrator.py", "line": 594, "severity": "error", "rule": "mypy var-annotated",
                           "message": 'Need type annotation for "tokens"'}])

    def test_a_name_ruff_prints_lossily_is_not_guessed_between_two_files(self):
        ruff = next(t for t in orchestrator.GATE_TOOLS if t.name == "ruff")
        tracked = {os.fsdecode(b"a\xe9.py"), os.fsdecode(b"a\xe8.py")}
        self.assertEqual(orchestrator.tool_findings(ruff, "a�.py:1:8: F401 `os` imported but unused\n",
                                                    "/repo", tracked), [])

    def test_pymarkdown_with_absolute_paths(self):
        self.assertEqual(self.read("pymarkdown", "/repo/README.md:3:1: MD040: Fenced code blocks should have a "
                                                 "language specified (fenced-code-language)\n"),
                         [{"path": "README.md", "line": 3, "severity": "error", "rule": "pymarkdown MD040",
                           "message": "Fenced code blocks should have a language specified (fenced-code-language)"}])

    def test_shellcheck(self):
        self.assertEqual([(f["line"], f["severity"], f["rule"]) for f in self.read("shellcheck",
                         "a.sh:2:6: note: Double quote to prevent globbing and word splitting. [SC2086]\n"
                         "a.sh:3:1: warning: Use 'cd ... || exit' in case cd fails. [SC2164]\n")],
                         [(2, "note", "shellcheck SC2086"), (3, "warning", "shellcheck SC2164")])

    def test_actionlint_skips_its_snippets(self):
        found = self.read("actionlint", '.github/workflows/w.yml:6:23: "github.event.issue.title" is potentially '
                                        'untrusted. [expression]\n  |\n6 |       - run: echo ${{ x }}\n  |   ^~~\n')
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(".github/workflows/w.yml", 6, "actionlint expression")])

    def test_untracked_paths_are_skipped(self):
        self.assertEqual(self.read("ruff", "build/x.py:1:1: F401 unused\n"), [])


class TestCIEnvFile(unittest.TestCase):
    """The credentials file the quality gate falls back on when the environment lacks a variable."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = f"{tmp.name}/ai-agents-orchestrator/ci.env"
        os.makedirs(os.path.dirname(self.path))

    def write(self, text, mode=0o600):
        with open(self.path, "w") as f:
            f.write(text)
        os.chmod(self.path, mode)

    def test_the_file_fills_what_the_environment_lacks(self):
        self.write("".join(f"{k}=file-{k}\n" for k in orchestrator.CI_ENV))
        env = {"JENKINS_USER": "from-env", "JENKINS_TOKEN": ""}
        self.assertEqual(orchestrator.ci_credentials(env, self.path),
                         {**{k: f"file-{k}" for k in orchestrator.CI_ENV}, "JENKINS_USER": "from-env"})

    def test_the_file_is_not_read_when_the_environment_has_everything(self):
        self.write("garbage", mode=0o644)
        self.assertEqual(orchestrator.ci_credentials(CREDENTIALS, self.path), CREDENTIALS)

    def test_a_missing_file_leaves_the_environment(self):
        self.assertEqual(orchestrator.ci_credentials({"JENKINS_URL": JENKINS}, self.path), {"JENKINS_URL": JENKINS})

    def test_a_file_others_may_read_is_refused(self):
        self.write(f"JENKINS_TOKEN={JENKINS_TOKEN}\n", mode=0o640)
        with self.assertRaisesRegex(OrchestratorError, f"others may read it; run: chmod 600 {self.path}"):
            orchestrator.ci_credentials({}, self.path)

    def test_shell_syntax(self):
        self.write("# Jenkins\n\nexport JENKINS_URL=https://jenkins.example/\n"
                   f"  JENKINS_USER='agents'  # the technical user\nJENKINS_TOKEN=old\nJENKINS_TOKEN=\"{JENKINS_TOKEN}\"\n")
        self.assertEqual(orchestrator.ci_credentials({}, self.path),
                         {"JENKINS_URL": "https://jenkins.example/", "JENKINS_USER": "agents",
                          "JENKINS_TOKEN": JENKINS_TOKEN})

    def test_a_malformed_line_is_named_without_its_content(self):
        self.write(f"JENKINS_URL={JENKINS}\n{JENKINS_TOKEN}\n")
        with self.assertRaises(OrchestratorError) as cm:
            orchestrator.ci_credentials({}, self.path)
        self.assertEqual(str(cm.exception), f"{self.path}:2: expected NAME=value")

    def test_path_follows_xdg_config_home(self):
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": "/xdg", "HOME": "/home/u"}):
            self.assertEqual(orchestrator.ci_env_path(), "/xdg/ai-agents-orchestrator/ci.env")
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": "relative", "HOME": "/home/u"}):
            self.assertEqual(orchestrator.ci_env_path(), "/home/u/.config/ai-agents-orchestrator/ci.env")

    def test_ci_reads_the_file_but_no_child_process_sees_it(self):
        self.write("".join(f"{k}={v}\n" for k, v in CREDENTIALS.items()))
        xdg = os.path.dirname(os.path.dirname(self.path))
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": xdg}):
            for k in orchestrator.CI_ENV:
                os.environ.pop(k, None)
            ci = CI(JOB)
            ci.check_credentials()
            self.assertEqual(ci.job_url, f"{JENKINS}{JOB_PATH}")
            self.assertFalse(set(orchestrator.CI_ENV) & set(os.environ))
            self.assertFalse(leaks(Host().check(["env"])))

    def test_missing_credentials_name_the_file(self):
        xdg = os.path.dirname(os.path.dirname(self.path))
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": xdg}):
            for k in orchestrator.CI_ENV:
                os.environ.pop(k, None)
            ci = CI(JOB)
            with self.assertRaisesRegex(OrchestratorError, f"environment or in {self.path}$"):
                ci.check_credentials()


class TestCI(unittest.TestCase):
    def ci(self, *answers, env=None):
        urlopen = MagicMock(side_effect=list(answers))
        return CI(JOB, env=CREDENTIALS if env is None else env, urlopen=urlopen), urlopen

    def http_error(self, status, body=b"<html>"):
        return urllib.error.HTTPError("u", status, "Error", {}, io.BytesIO(body))

    def test_job_url_maps_folders(self):
        self.assertEqual(CI(JOB, env=CREDENTIALS).job_url, f"{JENKINS}{JOB_PATH}")
        self.assertEqual(CI("a b", env=CREDENTIALS).job_url, f"{JENKINS}/job/a%20b/")

    def test_preflight_sends_basic_auth_in_the_header_only(self):
        params = {"property": [{"parameterDefinitions": [{"name": p} for p in orchestrator.JOB_PARAMETERS]}]}
        ci, urlopen = self.ci(FakeResponse(json.dumps(params)))
        ci.preflight()

        (req,) = [c.args[0] for c in urlopen.call_args_list]
        self.assertEqual(req.get_header("Authorization"),
                         "Basic " + base64.b64encode(f"agents:{JENKINS_TOKEN}".encode()).decode())
        # Kept from a redirect to another server.
        self.assertNotIn("Authorization", req.headers)
        self.assertFalse(leaks(req.full_url))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], orchestrator.HTTP_TIMEOUT)

    def test_errors_name_method_url_and_status(self):
        ci, _ = self.ci(self.http_error(403))
        with self.assertRaises(CIError) as cm:
            ci.build_result(7)
        self.assertEqual(str(cm.exception), f"GET {JENKINS}{JOB_PATH}7/api/json?tree=building%2Cresult: HTTP 403")
        self.assertEqual(cm.exception.status, 403)
        self.assertFalse(cm.exception.transient)

    def test_network_errors_are_transient_and_masked(self):
        ci, _ = self.ci(urllib.error.URLError(f"refused {JENKINS_TOKEN}"), TimeoutError("timed out"),
                        self.http_error(503))
        for _ in range(3):
            with self.assertRaises(CIError) as cm:
                ci.build_result(7)
            self.assertTrue(cm.exception.transient)
            self.assertFalse(leaks(str(cm.exception)))
            self.assertIn(f"GET {JENKINS}{JOB_PATH}7/api/json?tree=building%2Cresult", str(cm.exception))

    def test_trigger_returns_the_queue_item(self):
        ci, urlopen = self.ci(FakeResponse(headers={"Location": "http://internal:8080/queue/item/77/"}))
        self.assertEqual(ci.trigger(REF_1_1), f"{JENKINS}/queue/item/77/")
        req = urlopen.call_args.args[0]
        self.assertEqual((req.get_method(), req.full_url), ("POST", f"{JENKINS}{JOB_PATH}buildWithParameters"))
        self.assertEqual(dict(urllib.parse.parse_qsl(req.data.decode())), {"GIT_REF": REF_1_1})

    def test_trigger_on_a_deleted_job_is_not_retried(self):
        ci, _ = self.ci(self.http_error(404))
        with self.assertRaises(OrchestratorError) as cm:
            ci.trigger(REF_1_1)
        self.assertNotIsInstance(cm.exception, CIError)
        self.assertRegex(str(cm.exception), f"Jenkins job {JOB} is gone: POST .*buildWithParameters: HTTP 404")

    def test_missing_credentials_are_named_before_any_request(self):
        ci, urlopen = self.ci(env={"JENKINS_URL": JENKINS})
        with self.assertRaisesRegex(OrchestratorError, "needs JENKINS_USER, JENKINS_TOKEN in"):
            ci.build_result(1)
        urlopen.assert_not_called()

    def test_quality_file_is_the_archived_quality_md(self):
        ci, urlopen = self.ci(FakeResponse("GATE: OK\n\nintro\n"))
        self.assertEqual(ci.quality_file(3), "GATE: OK\n\nintro\n")
        self.assertEqual(urlopen.call_args.args[0].full_url, f"{JENKINS}{JOB_PATH}3/artifact/quality.md")

    def test_missing_artifact_and_test_report(self):
        ci, _ = self.ci(self.http_error(404), self.http_error(404))
        self.assertIsNone(ci.quality_file(3))
        self.assertIsNone(ci.failed_tests(3))

    def test_malformed_answers(self):
        ci, _ = self.ci(FakeResponse("<html>proxy error</html>"), FakeResponse("[1]"), FakeResponse())
        for call in (lambda: ci.build_result(1), lambda: ci.build_result(1)):
            with self.assertRaisesRegex(CIError, "the answer is not") as cm:
                call()
            self.assertTrue(cm.exception.transient)
        with self.assertRaisesRegex(OrchestratorError, "named no queue item"):
            ci.trigger(REF_1_1)


class TestHostSnapshot(unittest.TestCase):
    """Host.snapshot on real git."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.host = Host()
        self.git("init", "--quiet", "-b", "main")
        for key, value in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, value)

    def git(self, *args):
        return self.host.git(self.repo, *args)

    def put(self, name, text):
        path = f"{self.repo}/{name}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def test_snapshot_of_the_working_tree(self):
        self.put("keep.py", "a\n")
        self.put("mod.py", "1\n2\n")
        self.put("del.py", "x\n")
        self.put(".gitignore", "*.log\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "base")
        base = self.host.git_head(self.repo)
        self.put("mod.py", "1\ntwo\n3\n")
        os.remove(f"{self.repo}/del.py")
        self.put("untracked.py", "new\n")
        self.put("debug.log", "ignored\n")
        self.put(".orchestrator/.gitignore", "*\n")
        self.put(".orchestrator/runs/r/state.json", "{}\n")
        self.git("add", "mod.py")  # a staged change, to show the index is left as it was
        before = [self.git("rev-parse", "HEAD"), self.git("ls-files", "--stage"), self.git("status", "--porcelain")]

        sha = self.host.snapshot(self.repo, base, "snapshot")

        after = [self.git("rev-parse", "HEAD"), self.git("ls-files", "--stage"), self.git("status", "--porcelain")]
        self.assertEqual(after, before)
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", sha).split(),
                         [".gitignore", "keep.py", "mod.py", "untracked.py"])
        self.assertEqual(self.git("show", f"{sha}:mod.py"), "1\ntwo\n3\n")
        self.assertEqual(self.git("rev-parse", f"{sha}^"), self.git("rev-parse", base))
        self.assertEqual(self.git("log", "--format=%s", "-n", "1", sha).strip(), "snapshot")
        self.assertEqual(changed_lines(self.host.change_diff(self.repo, base, sha)),
                         {"mod.py": {2, 3}, "untracked.py": {1}})

    def test_snapshot_of_a_repository_without_an_index(self):
        self.git("commit", "--quiet", "--allow-empty", "-m", "empty")
        base = self.host.git_head(self.repo)
        self.put("first.py", "x\n")

        sha = self.host.snapshot(self.repo, base, "snapshot")
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", sha).split(), ["first.py"])


class TestChildEnvironment(unittest.TestCase):
    ENV = {**CREDENTIALS, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "KEEP_ME": "1"}

    def assert_scrubbed(self, env):
        self.assertFalse(set(orchestrator.CI_ENV) & set(env))
        self.assertEqual(env["KEEP_ME"], "1")

    @patch.dict("os.environ", ENV)
    def test_host_commands_get_no_credentials(self):
        run = MagicMock(return_value=completed())
        Host("remote-host", run=run).run(["git", "status"])
        self.assert_scrubbed(run.call_args.kwargs["env"])

    @patch.dict("os.environ", ENV)
    def test_herdr_gets_no_credentials(self):
        run = MagicMock(return_value=result({}))
        Herdr(run=run).call("agent", "list")
        self.assert_scrubbed(run.call_args.kwargs["env"])

    @patch.dict("os.environ", ENV)
    def test_a_real_process_sees_none(self):
        out = Host().check(["env"])
        self.assertIn("KEEP_ME=1", out)
        self.assertFalse([k for k in orchestrator.CI_ENV if f"{k}=" in out])
        self.assertFalse(leaks(out))

    @patch.dict("os.environ", {**ENV, **{k: "1" for k in orchestrator.GIT_PATHSPEC_ENV}})
    def test_the_gate_s_processes_get_no_credentials_and_no_pathspec_switches(self):
        # tool_env is the gate's own copy of child_env, which the gate cannot use; both leave out the same names.
        env = orchestrator.tool_env()
        self.assert_scrubbed(env)
        self.assertFalse(set(orchestrator.GIT_PATHSPEC_ENV) & set(env))
        self.assertEqual(env, orchestrator.child_env())


class TestQualityCLI(unittest.TestCase):
    def refused(self, argv):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(argv)

    def test_flag_checks(self):
        self.refused(["run", "t", "--quality-gate", JOB, "--max-quality-rounds", "0"])
        self.refused(["run", "t", "--max-quality-rounds", "2"])
        self.refused(["run", "t", "--quality-gate", "/folder/"])
        self.refused(["resume", "a1b2c3", "--quality-gate", "/folder/"])
        self.refused(["resume", "a1b2c3", "--quality-gate", JOB, "--no-quality-gate"])
        self.refused(["resume", "a1b2c3", "--no-quality-gate", "--max-quality-rounds", "2"])
        self.refused(["resume", "a1b2c3", "--max-quality-rounds", "0"])
        args = parse_args(["run", "t", "--quality-gate", JOB, "--max-quality-rounds", "2"])
        self.assertEqual((args.quality_gate, args.max_quality_rounds), (JOB, 2))

    def resumable(self, argv, saved):
        args = parse_args(["resume", "a1b2c3", *argv])
        return orchestrator.resumable_state([(10**4, saved)], args, "/proj", "here", lambda pid: False)

    def test_resume_limit_against_the_saved_quality_round(self):
        quality = asdict(quality_state(q=2))
        with self.assertRaisesRegex(OrchestratorError, "already in quality round 2 of its quality phase; "
                                    "--max-quality-rounds 1 is too low"):
            self.resumable(["--max-quality-rounds", "1"], quality)
        self.assertEqual(self.resumable(["--max-quality-rounds", "2"], quality).max_quality_rounds, 2)

        build = asdict(saved_run("build", 1, quality_job=JOB, quality_round=2))
        with self.assertRaisesRegex(OrchestratorError, "already in quality round 2 of its build phase"):
            self.resumable(["--max-quality-rounds", "2"], build)
        self.assertEqual(self.resumable(["--max-quality-rounds", "3"], build).max_quality_rounds, 3)
        # Round q = 0 has had no quality round yet.
        self.resumable(["--max-quality-rounds", "1"], asdict(saved_run("build", 2, quality_job=JOB)))

    def test_resume_limit_needs_the_gate(self):
        saved = asdict(saved_run("build", 1))
        with self.assertRaisesRegex(OrchestratorError, "has no quality gate"):
            self.resumable(["--max-quality-rounds", "2"], saved)
        # The gate this resume turns on is enough.
        self.assertEqual(self.resumable(["--quality-gate", JOB, "--max-quality-rounds", "2"], saved)
                         .max_quality_rounds, 2)

    def test_state_saved_before_the_gate_has_none(self):
        saved = asdict(saved_run("review", 1))
        for key in ("quality_job", "max_quality_rounds", "quality_round", "ci"):
            del saved[key]
        state = RunState.from_dict(saved)
        self.assertEqual((state.quality_job, state.quality_round, state.ci), (None, 0, {}))
        self.assertEqual(state.last_report_path(1), state.build_path(1))

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    # No real ci.env on this machine may decide the test.
    @patch.dict("os.environ", {"HERDR_ENV": "1", "XDG_CONFIG_HOME": "/nonexistent"})
    def test_gate_reaches_the_workflow(self, _resolve, _run, init):
        with patch("builtins.print"):
            main(["run", "task"])
            main(["run", "task", "--quality-gate", JOB, "--max-quality-rounds", "2"])
        plain, gated_ = init.call_args_list
        self.assertIsNone(plain.kwargs["ci"])
        self.assertEqual((gated_.kwargs["ci"].job, gated_.kwargs["max_quality_rounds"]), (JOB, 2))
        self.assertEqual(gated_.args[2].quality_job, JOB)

    def test_list_shows_the_quality_phase(self):
        record = {**run_record("quality"), "quality_round": 2}
        with patch("builtins.print") as out:
            orchestrator.print_runs([(10**6, record)], "here", lambda pid: True, [])
        self.assertTrue(out.call_args_list[0].args[0].startswith("20260930-070000-c0ffee  quality  round 1 q2  stale"))


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

D = "/proj/.orchestrator/runs/20260929-120000-a1b2c3"

# The default workflow's prompts and pull request body as commit d6bb100 sent them, recorded by
# running the scenarios of TestDefaultWorkflowPrompts on that commit's code.
# Since then, SPEC_PROMPT's "Separate Claude Code sessions" became "Separate agent sessions", and every prompt
# the Builder gets ends with BLOCKED_NOTE.
GOLDEN = {
    'spec': (
        'You are the Spec Collector, the first of three roles (Spec Collector -> Builder -> Reviewer). Separate agent sessions play the Builder and the Reviewer; they will know only what you write down. A human is at this terminal and answers you directly.\n'
        '\n'
        'Task from the human:\n'
        'add a rate limiter\n'
        '\n'
        'Interview the human until the requirements are unambiguous: the goal, what is in and out of scope, testable acceptance criteria, constraints, and how the result will be verified. Read the code in /proj first so your questions are grounded and you can cite the files the change touches. Ask a few questions at a time. Do not write or change any code.\n'
        '\n'
        f'When the human approves the spec, write it in a single write to {D}/spec.md as Markdown. Its first line is a `# ` heading: a short imperative title for the change, under 70 characters; it becomes the commit subject and the pull request title. Then these `##` sections: Goal, Scope, Non-goals, Acceptance criteria (a numbered list, each one checkable), Relevant code (file:line), Verification. Writing that file hands the work to the Builder, so write it only after the human approves it.'),
    'build_1': (
        f'You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The spec in {D}/spec.md was agreed with the human by a separate session; it is your contract.\n'
        '\n'
        "Implement it in /proj, following the conventions of the surrounding code. Verify the change the way the spec's Verification section says, and run the tests. Do not commit, push or switch branches; leave the changes in the working tree for the Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your report.\n"
        '\n'
        f'As your last step, write a report to {D}/build-1.md in a single write; it hands the work to the Reviewer: the files you changed and why, how you verified the change (commands and a summary of their results), and any acceptance criterion you did not meet, with the reason.'
        '\n'
        '\n'
        'If you cannot continue without a decision from the human, write the report with a first line `BLOCKED: <your question>` instead.'),
    'quality_fix_1_1': (
        f'The quality gate did not pass on your change; the findings are in {D}/quality-1-1.md. Fix each numbered issue, the failed conditions, the failing tests and a failed build, or explain in your report why one should stand. Answer each numbered finding to weigh the same way; those never fail the gate, but the Reviewer reads your answers. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-1-q1.md in a single write, in the same shape as before, answering each numbered issue and finding to weigh by its number.'
        '\n'
        '\n'
        'If you cannot continue without a decision from the human, write the report with a first line `BLOCKED: <your question>` instead.'),
    'review_1_passed': (
        f'You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did not write this change. Judge it only against the spec in {D}/spec.md and the code itself.\n'
        '\n'
        'The change: `git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\n'
        f"The Builder's report is in {D}/build-1-q1.md. Treat its claims as unverified: check them, and run the verification yourself. Do not modify any file other than your review.\n"
        '\n'
        f'As your last step, write your review to {D}/review-1.md in a single write. Its first line must be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered findings, each with file:line, what is wrong, and which acceptance criterion it violates or what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. Style preferences are not defects.\n'
        '\n'
        f'The quality gate passed on this change; its report is in {D}/quality-1-2.md.'),
    'fix_2': (
        f'The Reviewer requested changes; the findings are in {D}/review-1.md. Fix each finding, or explain in your report why it is wrong. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2.md in a single write, in the same shape as before, answering each finding by its number.'
        '\n'
        '\n'
        'If you cannot continue without a decision from the human, write the report with a first line `BLOCKED: <your question>` instead.'),
    'quality_fix_2_1': (
        f'The quality gate did not pass on your change; the findings are in {D}/quality-2-1.md. Fix each numbered issue, the failed conditions, the failing tests and a failed build, or explain in your report why one should stand. Answer each numbered finding to weigh the same way; those never fail the gate, but the Reviewer reads your answers. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2-q1.md in a single write, in the same shape as before, answering each numbered issue and finding to weigh by its number.'
        '\n'
        '\n'
        'If you cannot continue without a decision from the human, write the report with a first line `BLOCKED: <your question>` instead.'),
    'recheck_2_unresolved': (
        f"The Builder has answered your review; the new report is in {D}/build-2-q1.md. Review the change again (`git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists) against the spec in {D}/spec.md and your previous findings, checking the Builder's claims rather than trusting them. As your last step, write the review to {D}/review-2.md in a single write, with the same first-line verdict and numbered findings as before.\n"
        '\n'
        f"The quality gate still did not pass after the Builder's last quality round; its unresolved findings are in {D}/quality-2-2.md. Weigh them as you would your own."),
    'review_1_unresolved': (
        f'You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did not write this change. Judge it only against the spec in {D}/spec.md and the code itself.\n'
        '\n'
        'The change: `git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\n'
        f"The Builder's report is in {D}/build-1-q1.md. Treat its claims as unverified: check them, and run the verification yourself. Do not modify any file other than your review.\n"
        '\n'
        f'As your last step, write your review to {D}/review-1.md in a single write. Its first line must be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered findings, each with file:line, what is wrong, and which acceptance criterion it violates or what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. Style preferences are not defects.\n'
        '\n'
        f"The quality gate still did not pass after the Builder's last quality round; its unresolved findings are in {D}/quality-1-2.md. Weigh them as you would your own."),
    'recheck_2_passed': (
        f"The Builder has answered your review; the new report is in {D}/build-2.md. Review the change again (`git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists) against the spec in {D}/spec.md and your previous findings, checking the Builder's claims rather than trusting them. As your last step, write the review to {D}/review-2.md in a single write, with the same first-line verdict and numbered findings as before.\n"
        '\n'
        f'The quality gate passed on this change; its report is in {D}/quality-2-1.md.'),
    'fresh_build_2': (
        f'You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The spec in {D}/spec.md was agreed with the human by a separate session; it is your contract.\n'
        '\n'
        "Implement it in /proj, following the conventions of the surrounding code. Verify the change the way the spec's Verification section says, and run the tests. Do not commit, push or switch branches; leave the changes in the working tree for the Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your report.\n"
        '\n'
        f'As your last step, write a report to {D}/build-2.md in a single write; it hands the work to the Reviewer: the files you changed and why, how you verified the change (commands and a summary of their results), and any acceptance criterion you did not meet, with the reason.\n'
        '\n'
        f'This is round 2, and you are a fresh session. An earlier Builder session did the earlier turns; its changes are already in the working tree, and its reports are {D}/build-1.md.\n'
        '\n'
        f'The Reviewer requested changes; the findings are in {D}/review-1.md. Fix each finding, or explain in your report why it is wrong. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2.md in a single write, in the same shape as before, answering each finding by its number.'
        '\n'
        '\n'
        'If you cannot continue without a decision from the human, write the report with a first line `BLOCKED: <your question>` instead.'),
    'fresh_build_2_after_quality_rounds': (
        f'You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The spec in {D}/spec.md was agreed with the human by a separate session; it is your contract.\n'
        '\n'
        "Implement it in /proj, following the conventions of the surrounding code. Verify the change the way the spec's Verification section says, and run the tests. Do not commit, push or switch branches; leave the changes in the working tree for the Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your report.\n"
        '\n'
        f'As your last step, write a report to {D}/build-2.md in a single write; it hands the work to the Reviewer: the files you changed and why, how you verified the change (commands and a summary of their results), and any acceptance criterion you did not meet, with the reason.\n'
        '\n'
        f'This is round 2, and you are a fresh session. An earlier Builder session did the earlier turns; its changes are already in the working tree, and its reports are {D}/build-1.md, {D}/build-1-q1.md, {D}/build-1-q2.md.\n'
        '\n'
        f'The Reviewer requested changes; the findings are in {D}/review-1.md. Fix each finding, or explain in your report why it is wrong. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2.md in a single write, in the same shape as before, answering each finding by its number.'
        '\n'
        '\n'
        'If you cannot continue without a decision from the human, write the report with a first line `BLOCKED: <your question>` instead.'),
    'fresh_quality_fix_2_1': (
        f'You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The spec in {D}/spec.md was agreed with the human by a separate session; it is your contract.\n'
        '\n'
        "Implement it in /proj, following the conventions of the surrounding code. Verify the change the way the spec's Verification section says, and run the tests. Do not commit, push or switch branches; leave the changes in the working tree for the Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your report.\n"
        '\n'
        f'As your last step, write a report to {D}/build-2-q1.md in a single write; it hands the work to the Reviewer: the files you changed and why, how you verified the change (commands and a summary of their results), and any acceptance criterion you did not meet, with the reason.\n'
        '\n'
        f'This is round 2, and you are a fresh session. An earlier Builder session did the earlier turns; its changes are already in the working tree, and its reports are {D}/build-1.md, {D}/build-1-q1.md, {D}/build-2.md.\n'
        '\n'
        f'The quality gate did not pass on your change; the findings are in {D}/quality-2-1.md. Fix each numbered issue, the failed conditions, the failing tests and a failed build, or explain in your report why one should stand. Answer each numbered finding to weigh the same way; those never fail the gate, but the Reviewer reads your answers. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2-q1.md in a single write, in the same shape as before, answering each numbered issue and finding to weigh by its number.'
        '\n'
        '\n'
        'If you cannot continue without a decision from the human, write the report with a first line `BLOCKED: <your question>` instead.'),
    'fresh_review_2': (
        f'You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did not write this change. Judge it only against the spec in {D}/spec.md and the code itself.\n'
        '\n'
        'The change: `git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\n'
        f"The Builder's report is in {D}/build-2.md. Treat its claims as unverified: check them, and run the verification yourself. Do not modify any file other than your review.\n"
        '\n'
        f'As your last step, write your review to {D}/review-2.md in a single write. Its first line must be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered findings, each with file:line, what is wrong, and which acceptance criterion it violates or what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. Style preferences are not defects.\n'
        '\n'
        f'This is round 2, and you are a fresh session. The earlier reviews of this change are {D}/review-1.md; check that the Builder has answered each of their findings.'),
    'fresh_review_2_passed': (
        f'You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did not write this change. Judge it only against the spec in {D}/spec.md and the code itself.\n'
        '\n'
        'The change: `git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\n'
        f"The Builder's report is in {D}/build-2.md. Treat its claims as unverified: check them, and run the verification yourself. Do not modify any file other than your review.\n"
        '\n'
        f'As your last step, write your review to {D}/review-2.md in a single write. Its first line must be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered findings, each with file:line, what is wrong, and which acceptance criterion it violates or what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. Style preferences are not defects.\n'
        '\n'
        f'The quality gate passed on this change; its report is in {D}/quality-2-1.md.\n'
        '\n'
        f'This is round 2, and you are a fresh session. The earlier reviews of this change are {D}/review-1.md; check that the Builder has answered each of their findings.'),
    'continue_build_2': (
        f'Your session was restarted in the middle of this turn. Continue where you left off; the turn still ends when you write {D}/build-2.md in a single write.'),
}
GOLDEN_PR_BODY = (
    'Opened by ai-agents-orchestrator run `20260929-120000-a1b2c3`. Reviewer verdict after 2 rounds: **APPROVE**.\n'
    '\n'
    '| Commit trailer | Value |\n'
    '| --- | --- |\n'
    '| Orchestrator-Run | `20260929-120000-a1b2c3` |\n'
    '| Orchestrator-Workflow | `default` |\n'
    '| Orchestrator-Role | `spec=claude/default` |\n'
    '| Orchestrator-Role | `build=claude/default` |\n'
    '| Orchestrator-Role | `review=claude/default` |\n'
    '| Orchestrator-Gate | `ERROR after 2 quality rounds` |\n'
    '\n'
    '<details open>\n'
    '<summary>Spec</summary>\n'
    '\n'
    '# Add a token-bucket rate limiter\n'
    '\n'
    '## Goal\n'
    'limit requests\n'
    '\n'
    '</details>\n'
    '\n'
    '<details>\n'
    '<summary>Builder report (round 2)</summary>\n'
    '\n'
    'report 2 q1\n'
    '\n'
    '</details>\n'
    '\n'
    '<details>\n'
    '<summary>Quality gate (round 2)</summary>\n'
    '\n'
    'GATE: ERROR\n'
    '\n'
    'Jenkins build #44 judged snapshot `snap4` (round 2, quality round 2 of 2) and ended UNSTABLE.\n'
    '\n'
    '`orchestrator.py gate` judged the working tree.\n'
    '\n'
    '## Issues on changed lines\n'
    '\n'
    '1. `limiter.py:9` error mypy arg-type: Argument 1 has incompatible type\n'
    '\n'
    '</details>\n'
    '\n'
    '<details>\n'
    '<summary>Review (round 2)</summary>\n'
    '\n'
    '**VERDICT: APPROVE**\n'
    '1. finding\n'
    '\n'
    '</details>\n'
    '\n'
    '<details>\n'
    '<summary>Run timings</summary>\n'
    '\n'
    '| step | role | round | harness | model | start (UTC) | duration | file |\n'
    '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
    '| spec (human-paced) | spec | 0 | claude | default | 2026-10-03 04:00:00 | 0s | spec.md |\n'
    '| build | build | 1 | claude | default | 2026-10-03 04:00:00 | 0s | build-1.md |\n'
    '| quality | quality | 1 q1 | - | - | 2026-10-03 04:00:00 | 30s | quality-1-1.md |\n'
    '| build | build | 1 q1 | claude | default | 2026-10-03 04:00:30 | 0s | build-1-q1.md |\n'
    '| quality | quality | 1 q2 | - | - | 2026-10-03 04:00:30 | 30s | quality-1-2.md |\n'
    '| review | review | 1 | claude | default | 2026-10-03 04:01:00 | 0s | review-1.md |\n'
    '| build | build | 2 | claude | default | 2026-10-03 04:01:00 | 0s | build-2.md |\n'
    '| quality | quality | 2 q1 | - | - | 2026-10-03 04:01:00 | 30s | quality-2-1.md |\n'
    '| build | build | 2 q1 | claude | default | 2026-10-03 04:01:30 | 0s | build-2-q1.md |\n'
    '| quality | quality | 2 q2 | - | - | 2026-10-03 04:01:30 | 30s | quality-2-2.md |\n'
    '| review | review | 2 | claude | default | 2026-10-03 04:02:00 | 0s | review-2.md |\n'
    '\n'
    '</details>\n')


class TestDefaultWorkflowPrompts(unittest.TestCase):
    def two_gated_rounds(self, outcomes, build, review):
        """A run with the gate and two quality rounds a round; its prompts, saved phases and host, in order."""
        seen, phases = [], []
        fake = FakeCI()
        fake.outcomes = outcomes
        wf, herdr, host, fake, _ = gated({
            "spec": [recording(spec_turn, seen)], "build": [recording(b, seen) for b in build],
            "review": [recording(r, seen) for r in review]}, fake=fake, max_quality_rounds=2)
        orig = host.write

        def write(path, text):
            orig(path, text)
            if path.endswith("state.json"):
                saved = json.loads(text)
                phases.append((saved["phase"], saved["round"], saved["quality_round"]))
        host.write = write

        self.assertEqual(wf.run(), APPROVE)
        return seen, list(dict.fromkeys(phases)), herdr, host

    def test_gate_passes_after_a_fix_then_runs_out(self):
        red = outcome(**RED)
        seen, phases, herdr, host = self.two_gated_rounds(
            [red, outcome(), red, outcome(**RED2)], [build_turn(1), fix_turn(1, 1), build_turn(2), fix_turn(2, 1)],
            [review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)])

        self.assertEqual(seen, [GOLDEN[k] for k in ("spec", "build_1", "quality_fix_1_1", "review_1_passed", "fix_2",
                                                    "quality_fix_2_1", "recheck_2_unresolved")])
        self.assertEqual(phases, [
            ("spec", 0, 0), ("build", 1, 0), ("quality", 1, 1), ("build", 1, 1), ("quality", 1, 2), ("review", 1, 2),
            ("build", 2, 0), ("quality", 2, 1), ("build", 2, 1), ("quality", 2, 2), ("review", 2, 2),
            ("publish", 2, 2), ("done", 2, 2)])
        written = [p.rsplit("/", 1)[-1] for p in host.writes if p.startswith(D) and not p.endswith("state.json")]
        self.assertEqual(written, ["spec.md", "build-1.md", "quality-1-1.md", "build-1-q1.md", "quality-1-2.md",
                                   "review-1.md", "build-2.md", "quality-2-1.md", "build-2-q1.md", "quality-2-2.md",
                                   "review-2.md", "summary.md", "metrics.json"])
        self.assertEqual([c for c in herdr.calls if c[0] in ("rename", "split")], [
            ("rename", "w1:p1", "Spec Collector"), ("split", "w1:p1", "right"), ("rename", "w1:p2", "Builder"),
            ("split", "w1:p2", "down"), ("rename", "w1:p3", "Reviewer")])
        self.assertEqual((host.prs[0]["title"], host.prs[0]["body"]), ("Add a token-bucket rate limiter", GOLDEN_PR_BODY))

    def test_gate_runs_out_then_passes(self):
        red = outcome(**RED)
        seen, *_ = self.two_gated_rounds(
            [red, outcome(**RED2), outcome()], [build_turn(1), fix_turn(1, 1), build_turn(2)],
            [review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)])

        self.assertEqual(seen, [GOLDEN[k] for k in ("spec", "build_1", "quality_fix_1_1", "review_1_unresolved",
                                                    "fix_2", "recheck_2_passed")])

    def test_fresh_builder_in_round_two(self):
        seen = []
        wf, *_ = resume(saved_run("build", 2, agents=("spec", "build", "review"), prompted="build-2.md"), {
            "build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)],
        }, alive=["review"])
        wf.run()
        self.assertEqual(seen, [GOLDEN["fresh_build_2"]])

    def test_fresh_builder_in_round_two_after_quality_rounds(self):
        seen = []
        state = saved_run("build", 2, agents=("spec", "build", "review"), quality_job=JOB)
        wf, *_ = resume_gated(state, {"build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)]},
                              FakeCI(), alive=["review"], files={lambda s: s.quality_build_path(1, 1): "r",
                                                                 lambda s: s.quality_build_path(1, 2): "r"})
        wf.run()
        self.assertEqual(seen, [GOLDEN["fresh_build_2_after_quality_rounds"]])

    def test_fresh_builder_answering_a_quality_round_in_round_two(self):
        seen = []
        state = saved_run("build", 2, agents=("spec", "build", "review"), quality_job=JOB, quality_round=1, prompted="build-2-q1.md")
        wf, *_ = resume_gated(state, {"build": [recording(fix_turn(2, 1), seen)], "review": [review_turn(2, APPROVE)]},
                              FakeCI(), alive=["review"],
                              files={lambda s: s.quality_build_path(1, 1): "r", lambda s: s.build_path(2): "r",
                                     lambda s: s.quality_path(2, 1): "GATE: ERROR\n"})
        wf.run()
        self.assertEqual(seen, [GOLDEN["fresh_quality_fix_2_1"]])

    def test_fresh_reviewer_in_round_two(self):
        seen = []
        wf, *_ = resume(saved_run("review", 2, agents=("spec", "build", "review")), {
            "review": [recording(review_turn(2, APPROVE), seen)],
        }, files={lambda s: s.build_path(2): "report 2"})
        wf.run()
        self.assertEqual(seen, [GOLDEN["fresh_review_2"]])

    def test_fresh_reviewer_in_round_two_with_the_gate(self):
        seen = []
        state = saved_run("review", 2, agents=("spec", "build", "review"), quality_job=JOB, quality_round=1)
        wf, *_ = resume_gated(state, {"review": [recording(review_turn(2, APPROVE), seen)]}, FakeCI(),
                              files={lambda s: s.build_path(2): "r", lambda s: s.quality_path(2, 1): "GATE: OK\n"})
        wf.run()
        self.assertEqual(seen, [GOLDEN["fresh_review_2_passed"]])

    def test_continue_after_a_resumed_session(self):
        seen = []
        wf, *_ = resume(saved_run("build", 2, agents=("spec", "build", "review"), prompted="build-2.md"), {
            "build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)],
        }, alive=["review"], sessions={"build": "s-build"})
        wf.run()
        self.assertEqual(seen, [GOLDEN["continue_build_2"]])


Pipeline, Step = orchestrator.Pipeline, orchestrator.Step

# No spec step: the task is the Builder's contract.
QUICK = Pipeline("quick", {"build": "Builder", "review": "Reviewer"}, (
    Step("build", "build", "build-{n}.md", "Build {task} in {cwd}; report to {build_path}.",
         again="Fix {prev_review_path}; report to {build_path}.", edits=True),
    Step("review", "review", "review-{n}.md", "Review {change} against {task}; write {review_path}.",
         again="Recheck {build_path}; write {review_path}.", loop_to="build"),
))

# Four roles: the tests are written once, and the review loop goes back to the Builder only.
FOUR = Pipeline("four", {"spec": "Spec Collector", "tests": "Test Writer", "build": "Builder",
                         "review": "Reviewer"}, (
    Step("spec", "spec", "spec.md", "Interview about {task}; write {spec_path}.", human_paced=True),
    Step("tests", "tests", "tests.md", "Write tests for {spec_path}; report to {tests_path}.", edits=True),
    Step("build", "build", "build-{n}.md", "Build {spec_path} against {tests_path}; report to {build_path}.",
         again="Fix {prev_review_path}; report to {build_path}.",
         fresh_note="Earlier reports: {earlier_build_paths}.", fresh_repeats_again=True, edits=True),
    Step("review", "review", "review-{n}.md", "Review {change}; write {review_path}.",
         again="Recheck; write {review_path}.", loop_to="build"),
))

# The quality-gated step is not called build.
IMPL = Pipeline("impl", {"impl": "Implementer", "review": "Reviewer"}, (
    Step("impl", "impl", "impl-{n}.md", "Implement {task}; report to {impl_path}.",
         again="Fix {prev_review_path}; report to {impl_path}.", fresh_note="Earlier reports: {earlier_impl_paths}.",
         edits=True, quality_gated=True),
    Step("review", "review", "review-{n}.md", "Review {change} with {impl_path}; write {path}.",
         again="Recheck {impl_path}; write {review_path}.", loop_to="impl"),
))

CHANGE = "`git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists"

write_tests = writes(lambda s: f"{s.dir}/tests.md", "tests written")


def impl_turn(n, q=0):
    """The Implementer's report in round n, answering quality round q when q is not 0."""
    def turn(prompt, state, host):
        host.write("/proj/limiter.py", f"version {n}.{q}")
        return writes(lambda s: IMPL.steps[0].path(s.dir, n, q), f"impl {n} q{q}")(prompt, state, host)
    return turn


def new_run(pipeline):
    """A new run's state, as main makes it for the workflow."""
    return RunState("20260929-120000-a1b2c3", "add a rate limiter", "/proj", None,
                    phase=pipeline.steps[0].id, workflow=pipeline.name)


class TestWorkflowDefinitions(unittest.TestCase):
    def test_default_is_the_only_registered_workflow(self):
        self.assertEqual(list(orchestrator.WORKFLOWS), ["default"])

    def pipeline(self, *steps, roles=None):
        return Pipeline("bad", roles or {"build": "Builder", "review": "Reviewer"}, steps)

    def step(self, id, file, **kw):
        return Step(id, kw.pop("role", id), file, "go", **kw)

    def test_invalid_definitions(self):
        build, review = self.step("build", "a-{n}.md", edits=True), self.step("review", "b-{n}.md")
        cases = [
            ("duplicate step id build", [build, self.step("build", "b-{n}.md", role="review")]),
            ("duplicate handoff file a-{n}.md", [build, self.step("review", "a-{n}.md")]),
            ("step review loops back to nope, which is not a step",
             [build, self.step("review", "b-{n}.md", loop_to="nope")]),
            ("step build loops back to review, which does not come before it",
             [self.step("build", "a-{n}.md", edits=True, loop_to="review"), review]),
            ("step build loops back to build, which does not come before it",
             [self.step("build", "a-{n}.md", edits=True, loop_to="build"), review]),
            ("steps build, review each loop back; only one verdict loop is supported",
             [self.step("prep", "p-{n}.md", role="build", edits=True),
              self.step("build", "a-{n}.md", loop_to="prep"), self.step("review", "b-{n}.md", loop_to="build")]),
            ("steps build, review are each quality-gated; only one may be",
             [self.step("build", "a-{n}.md", edits=True, quality_gated=True),
              self.step("review", "b-{n}.md", edits=True, quality_gated=True)]),
            ("step build is quality-gated, so it must edit",
             [self.step("prep", "p-{n}.md", role="review", edits=True),
              self.step("build", "a-{n}.md", quality_gated=True)]),
            ("step build is quality-gated, so its handoff file needs {n}",
             [self.step("build", "a.md", edits=True, quality_gated=True), review]),
            ("step review is quality-gated, so it cannot be the verdict step",
             [build, self.step("review", "b-{n}.md", edits=True, quality_gated=True, loop_to="build")]),
            ("role review has no step", [build]),
            ("step review is in the review loop, so its handoff file needs {n}",
             [build, self.step("review", "b.md", loop_to="build")]),
            ("step build: a per-round handoff file needs an editing step at or before it",
             [self.step("build", "a-{n}.md"), self.step("review", "b-{n}.md", edits=True)]),
            ("step id done is reserved", [build, self.step("done", "b.md", role="review")]),
            ("step id quality is reserved", [build, self.step("quality", "b.md", role="review")]),
            ("has no steps", []),
            ("step build: handoff file a-{round}.md may use only {n}",
             [self.step("build", "a-{round}.md", edits=True), review]),
            ("prompt placeholder prev_review_path names the files of two steps",
             [build, self.step("review", "b-{n}.md"), self.step("prev_review", "c-{n}.md", role="review")]),
        ]
        for problem, steps in cases:
            with self.subTest(problem=problem):
                with self.assertRaisesRegex(ValueError, "^workflow bad: " + re.escape(problem)):
                    self.pipeline(*steps)

    def test_role_without_label(self):
        steps = (self.step("build", "a.md", edits=True), self.step("review", "b.md"))
        with self.assertRaisesRegex(ValueError, "workflow bad: step build: role build has no label"):
            self.pipeline(*steps, roles={"review": "Reviewer"})
        with self.assertRaisesRegex(ValueError, "workflow bad: role build has no label"):
            self.pipeline(*steps, roles={"build": "", "review": "Reviewer"})

    def test_unknown_placeholder(self):
        step = Step("build", "build", "a-{n}.md", "write {spec_path}", edits=True)
        with self.assertRaisesRegex(ValueError, "workflow bad: step build: unknown prompt placeholder spec_path"):
            Pipeline("bad", {"build": "Builder"}, (step,))

    def test_quality_fix_report_names(self):
        self.assertEqual(Step("b", "b", "build-{n}.md", "go").path(D, 2, 3), f"{D}/build-2-q3.md")
        self.assertEqual(Step("b", "b", "impl-{n}", "go").path(D, 1, 1), f"{D}/impl-1-q1")

    def test_the_gate_needs_a_gated_step(self):
        state = new_run(QUICK)
        with self.assertRaisesRegex(OrchestratorError, "workflow quick has no quality-gated step"):
            gated({}, state=state, pipeline=QUICK)

    def test_a_pull_request_needs_an_editing_step(self):
        talk = Pipeline("talk", {"spec": "Spec Collector"}, (Step("spec", "spec", "spec.md", "{task}"),))
        state = new_run(talk)
        with self.assertRaisesRegex(OrchestratorError, "workflow talk has no editing step, so it cannot end in a "
                                                       "pull request; pass --no-pr"):
            make_workflow({}, state=state, pipeline=talk)
        wf, *_ = make_workflow({"spec": [writes(lambda s: f"{s.dir}/spec.md", "notes")]}, state=new_run(talk),
                               pipeline=talk, pull_request=False)
        self.assertEqual(wf.run(), orchestrator.FINISHED)
        self.assertEqual(wf.state.phase, "done")


class TestOtherWorkflows(unittest.TestCase):
    def test_workflow_without_a_spec_step(self):
        seen, at_first_build = [], []

        def build(prompt, state, host):
            at_first_build.append((state.base, host.branch, state.round))
            return build_turn(1)(prompt, state, host)
        wf, herdr, host, _ = make_workflow({
            "build": [recording(build, seen)], "review": [recording(review_turn(1, APPROVE), seen)],
        }, state=new_run(QUICK), pipeline=QUICK)

        self.assertEqual(wf.run(), APPROVE)
        branch = "orchestrator/add-a-rate-limiter-a1b2c3"
        self.assertEqual(at_first_build, [("abc123", branch, 1)])
        self.assertEqual(seen, [f"Build add a rate limiter in /proj; report to {D}/build-1.md.{NOTE}",
                                f"Review {CHANGE} against add a rate limiter; write {D}/review-1.md."])
        self.assertEqual([c[1] for c in herdr.calls if c[0] == "start"], ["build-a1b2c3", "review-a1b2c3"])
        self.assertEqual([c for c in herdr.calls if c[0] == "split"], [("split", "w1:p1", "right")])
        self.assertFalse([c for c in herdr.calls if c[0] == "focus"])
        self.assertEqual((host.prs[0]["title"], host.prs[0]["head"]), ("add a rate limiter", branch))
        commit = next(c for c in host.git_calls if c[0] == "commit")
        self.assertEqual(commit[commit.index("-m") + 1], "add a rate limiter")
        saved = json.loads(host.files[f"{D}/state.json"])
        self.assertEqual((saved["workflow"], saved["phase"]), ("quick", "done"))

    def test_verdict_step_of_another_name_gets_one_retry(self):
        checked = Pipeline("checked", {"build": "Builder", "check": "Checker"}, (
            Step("build", "build", "build-{n}.md", "Build {task}; report to {build_path}.",
                 again="Fix {prev_check_path}; report to {build_path}.", edits=True),
            Step("check", "check", "check-{n}.md", "Check {change}; write {path}.", loop_to="build"),
        ))
        seen = []
        wf, herdr, host, _ = make_workflow({
            "build": [build_turn(1)],
            "check": [writes(lambda s: f"{s.dir}/check-1.md", "fine by me"),
                      recording(writes(lambda s: f"{s.dir}/check-1.md", f"VERDICT: {APPROVE}\n"), seen)],
        }, state=new_run(checked), pipeline=checked)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen, [retry_prompt(f"{D}/check-1.md")])
        self.assertEqual(host.files[f"{D}/check-1.rejected.md"], "fine by me")
        self.assertEqual(wf.state.retried, ["check-1.md"])

    def test_four_roles_loop_back_to_the_builder_only(self):
        seen = []
        wf, herdr, host, notes = make_workflow({
            "spec": [spec_turn], "tests": [write_tests],
            "build": [recording(build_turn(1), seen), recording(build_turn(2), seen)],
            "review": [review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)],
        }, state=new_run(FOUR), pipeline=FOUR)

        self.assertEqual(wf.run(), APPROVE)
        prompts = [c[1] for c in herdr.calls if c[0] == "prompt"]
        self.assertEqual(prompts, ["spec-a1b2c3", "tests-a1b2c3", "build-a1b2c3", "review-a1b2c3",
                                   "build-a1b2c3", "review-a1b2c3"])
        self.assertEqual([c for c in herdr.calls if c[0] == "split"],
                         [("split", "w1:p1", "right"), ("split", "w1:p2", "down"), ("split", "w1:p3", "down")])
        self.assertEqual([c[2] for c in herdr.calls if c[0] == "rename"],
                         ["Spec Collector", "Test Writer", "Builder", "Reviewer"])
        self.assertEqual(seen, [f"Build {D}/spec.md against {D}/tests.md; report to {D}/build-1.md.{NOTE}",
                                f"Fix {D}/review-1.md; report to {D}/build-2.md.{NOTE}"])
        self.assertEqual(notes[0], "Spec Collector is waiting for you")
        self.assertEqual(host.prs[0]["title"], "Add a token-bucket rate limiter")
        self.assertIn("<summary>Builder report (round 2)</summary>\n\nreport 2", host.prs[0]["body"])
        self.assertEqual(wf.state.round, 2)

    def test_interrupted_mid_loop_resumes_at_the_right_step(self):
        def build_then_interrupt(prompt, state, host):
            build_turn(2)(prompt, state, host)
            raise KeyboardInterrupt
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "tests": [write_tests], "build": [build_turn(1), build_then_interrupt],
            "review": [review_turn(1, CHANGES_REQUESTED)],
        }, state=new_run(FOUR), pipeline=FOUR)
        with self.assertRaises(KeyboardInterrupt):
            wf.run()
        saved = json.loads(host.files[f"{D}/state.json"])
        self.assertEqual((saved["workflow"], saved["phase"], saved["round"], saved["error"]),
                         ("four", "build", 2, "interrupted"))

        seen = []
        wf2, herdr2, *_ = resume(RunState.from_dict(saved), {"review": [recording(review_turn(2, APPROVE), seen)]},
                                 host=host, pipeline=FOUR)
        herdr2.panes = 4
        self.assertEqual(wf2.run(), APPROVE)
        self.assertEqual([c[1] for c in herdr2.calls if c[0] == "prompt"], ["review-a1b2c3"])
        self.assertEqual([c[1] for c in herdr2.calls if c[0] == "start"], ["review-a1b2c3"])
        self.assertEqual(seen, [f"Recheck; write {D}/review-2.md."])
        self.assertEqual((wf2.state.round, wf2.state.phase, wf2.state.verdict), (2, "done", APPROVE))

    def test_gated_step_with_another_name(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED), outcome()]
        impls, reviews = [], []
        wf, herdr, host, fake, _ = gated({
            "impl": [recording(impl_turn(1), impls), recording(impl_turn(1, 1), impls)],
            "review": [recording(review_turn(1, APPROVE), reviews)],
        }, fake=fake, state=new_run(IMPL), pipeline=IMPL)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(impls, [
            f"Implement add a rate limiter; report to {D}/impl-1.md.{NOTE}",
            orchestrator.QUALITY_FIX_PROMPT.format(quality_path=f"{D}/quality-1-1.md", report_path=f"{D}/impl-1-q1.md")
            + NOTE])
        self.assertEqual(reviews, [f"Review {CHANGE} with {D}/impl-1-q1.md; write {D}/review-1.md.\n\n" +
                                   orchestrator.QUALITY_PASSED_NOTE.format(quality_path=f"{D}/quality-1-2.md")])
        written = [p.rsplit("/", 1)[-1] for p in host.writes if p.startswith(D) and not p.endswith("state.json")]
        self.assertEqual(written, ["impl-1.md", "quality-1-1.md", "impl-1-q1.md", "quality-1-2.md", "review-1.md",
                                   "summary.md", "metrics.json"])
        self.assertTrue(host.files[f"{D}/quality-1-1.md"].startswith("GATE: ERROR\n"))
        self.assertTrue(host.files[f"{D}/quality-1-2.md"].startswith("GATE: OK\n"))
        self.assertEqual(fake.triggered(), [REF_1_1, "orchestrator-ci/a1b2c3-1-q2"])
        body = host.prs[0]["body"]
        self.assertIn("<summary>Builder report (round 1)</summary>\n\nimpl 1 q1", body)
        self.assertIn("<summary>Quality gate (round 1)</summary>\n\nGATE: OK", body)
        self.assertEqual(host.prs[0]["title"], "add a rate limiter")

    def test_fresh_gated_step_answering_a_quality_round(self):
        seen = []
        state = saved_run("impl", 1, agents=(), quality_job=JOB, quality_round=1, prompted="impl-1-q1.md", workflow="impl")
        state.agents = {"impl": {"name": "impl-a1b2c3", "pane": "w1:p1"}}
        wf, herdr, host, _ = resume_gated(state, {"impl": [recording(impl_turn(1, 1), seen)],
                                                  "review": [review_turn(1, APPROVE)]}, FakeCI(), pipeline=IMPL,
                                          files={lambda s: f"{s.dir}/impl-1.md": "impl 1",
                                                 lambda s: s.quality_path(1, 1): "GATE: ERROR\n"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen, [f"Implement add a rate limiter; report to {D}/impl-1-q1.md.\n\n"
                                f"Earlier reports: {D}/impl-1.md.\n\n" +
                                orchestrator.QUALITY_FIX_PROMPT.format(quality_path=f"{D}/quality-1-1.md",
                                                                       report_path=f"{D}/impl-1-q1.md") + NOTE])
        # The second role goes to the right of the first.
        self.assertEqual([c for c in herdr.calls if c[0] == "split"], [("split", "w1:p1", "right")])
        self.assertEqual(wf.state.quality_round, 2)

    def test_resumed_gated_step_checks_the_credentials_first(self):
        state = saved_run("impl", 1, agents=(), quality_job=JOB, workflow="impl")
        env = {k: v for k, v in CREDENTIALS.items() if k != "JENKINS_TOKEN"}
        wf, herdr, *_ = resume(state, {"impl": [impl_turn(1)]}, ci=ci_for(FakeCI(), env), pipeline=IMPL)

        with self.assertRaisesRegex(OrchestratorError, "needs JENKINS_TOKEN"):
            wf.run()
        self.assertFalse([c for c in herdr.calls if c[0] == "prompt"])

    def test_model_reaches_a_role_without_its_own_flag(self):
        models = orchestrator.role_models(parse_args(["run", "task", "--model", "X", "--build-model", "Y"]),
                                          FOUR.roles)
        self.assertEqual(models, {"spec": "X", "tests": "X", "build": "Y", "review": "X"})
        wf, herdr, *_ = make_workflow({
            "spec": [spec_turn], "tests": [write_tests], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        }, state=new_run(FOUR), pipeline=FOUR, models=models)
        wf.run()
        self.assertIn(("start", "tests-a1b2c3", "w1:p2", ("--model", "X")), herdr.calls)

    def test_phase_outside_the_workflow_is_refused(self):
        wf, *_ = make_workflow({}, state=new_run(FOUR), pipeline=QUICK)
        with self.assertRaisesRegex(OrchestratorError, "phase spec, which workflow quick has no step for"):
            wf.run()


LOCKING = Pipeline("locking", FOUR.roles, tuple(replace(st, locks=True) if st.id == "tests" else st
                                                for st in FOUR.steps))
TEST_FILE = "/proj/test_limiter.py"


def locked_tests_turn(prompt, state, host):
    host.write(TEST_FILE, "assert limit() == 3")
    return write_tests(prompt, state, host)


def build_touching_tests(n, test=""):
    """The Builder's turn in round n, which also rewrites the locked test, or deletes it for None."""
    def turn(prompt, state, host):
        if test is None:
            host.delete(TEST_FILE)
        else:
            host.write(TEST_FILE, test)
        return build_turn(n)(prompt, state, host)
    return turn


class TestLockedFiles(unittest.TestCase):
    def run_locking(self, *builds, reviews=None, **kw):
        seen = []
        reviews = reviews or [review_turn(1, APPROVE)]
        wf, herdr, host, notes = make_workflow({
            "spec": [spec_turn], "tests": [locked_tests_turn], "build": list(builds),
            "review": [recording(r, seen) for r in reviews],
        }, state=new_run(LOCKING), pipeline=LOCKING, **kw)
        return wf, host, seen

    def test_an_edited_locked_test_is_restored_and_the_reviewer_told(self):
        wf, host, seen = self.run_locking(build_touching_tests(1, "assert True"))

        self.assertEqual(wf.run(), APPROVE)

        ref = "refs/orchestrator/a1b2c3/locked/tests"
        self.assertEqual(host.restores, [(ref, ["test_limiter.py"])])
        self.assertEqual(host.files[TEST_FILE], "assert limit() == 3")
        # A file nobody locked keeps the Builder's change.
        self.assertEqual(host.files["/proj/limiter.py"], "version 1")
        self.assertEqual(host.files[f"{D}/locked-build-1.diff"],
                         "--- test_limiter.py: assert limit() == 3\n+++ test_limiter.py: assert True\n")
        self.assertEqual(wf.state.locked, {"tests": ["test_limiter.py"]})
        self.assertEqual(wf.state.restored,
                         [{"round": 1, "file": "locked-build-1.diff", "paths": {"tests": ["test_limiter.py"]}}])
        self.assertIn("\n\n" + orchestrator.LOCKED_NOTE.format(steps="tests", paths="`test_limiter.py`",
                                                               diff_paths=f"{D}/locked-build-1.diff"), seen[0])
        # done: the run's local refs are gone.
        self.assertEqual(host.refs, {})

    def test_a_deleted_locked_test_comes_back(self):
        wf, host, _ = self.run_locking(build_touching_tests(1, None))
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(host.files[TEST_FILE], "assert limit() == 3")
        self.assertIn("+++ test_limiter.py: None", host.files[f"{D}/locked-build-1.diff"])

    def test_untouched_locked_files_need_no_restore(self):
        wf, host, seen = self.run_locking(build_turn(1), build_touching_tests(2, "assert True"),
                                          reviews=[review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)])
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(host.restores, [("refs/orchestrator/a1b2c3/locked/tests", ["test_limiter.py"])])
        self.assertNotIn(f"{D}/locked-build-1.diff", host.files)
        self.assertNotIn("locked", seen[0])
        self.assertIn(f"{D}/locked-build-2.diff", seen[1])

    def test_a_workflow_without_locks_restores_nothing(self):
        wf, _, host, _ = make_workflow({
            "spec": [spec_turn], "tests": [locked_tests_turn], "build": [build_touching_tests(1, "assert True")],
            "review": [review_turn(1, APPROVE)],
        }, state=new_run(FOUR), pipeline=FOUR)
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual((host.files[TEST_FILE], host.restores, host.refs), ("assert True", [], {}))
        self.assertEqual((wf.state.locked, wf.state.restored), ({}, []))

    def test_a_resume_between_the_turn_and_the_restore_still_restores(self):
        def build_then_interrupt(prompt, state, host):
            build_touching_tests(1, "assert True")(prompt, state, host)
            raise KeyboardInterrupt
        wf, host, _ = self.run_locking(build_then_interrupt)
        with self.assertRaises(KeyboardInterrupt):
            wf.run()
        self.assertEqual(host.files[TEST_FILE], "assert True")
        saved = json.loads(host.files[f"{D}/state.json"])
        self.assertEqual((saved["phase"], saved["locked"], saved["restored"]),
                         ("build", {"tests": ["test_limiter.py"]}, []))

        wf2, *_ = resume(RunState.from_dict(saved), {"review": [review_turn(1, APPROVE)]}, host=host,
                         pipeline=LOCKING)
        self.assertEqual(wf2.run(), APPROVE)
        self.assertEqual(host.files[TEST_FILE], "assert limit() == 3")
        self.assertEqual([r["file"] for r in wf2.state.restored], ["locked-build-1.diff"])

    def test_a_record_saved_before_the_restore_is_restored_on_resume(self):
        state = saved_run("build", 1, agents=("spec", "build"), workflow="locking",
                          workflow_definition=orchestrator.workflow_definition(LOCKING),
                          locked={"tests": ["test_limiter.py"]},
                          restored=[{"round": 1, "file": "locked-build-1.diff", "paths": {"tests": ["test_limiter.py"]}}])
        host = FakeHost()
        host.refs["refs/orchestrator/a1b2c3/locked/tests"] = "snap0"
        host.trees["snap0"] = {TEST_FILE: "assert limit() == 3"}
        host.files[TEST_FILE] = "assert True"
        wf, *_ = resume(state, {"review": [review_turn(1, APPROVE)]}, host=host, pipeline=LOCKING,
                        files={lambda s: s.build_path(1): "report 1", lambda s: f"{s.dir}/tests.md": "tests"})
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(host.files[TEST_FILE], "assert limit() == 3")
        self.assertEqual(len(wf.state.restored), 1)
        self.assertNotIn(f"{D}/locked-build-1.diff", host.writes)

    def test_the_gate_judges_the_restored_tests(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED), outcome()]
        gated_build = replace(LOCKING.step("build"), quality_gated=True)
        pipeline = Pipeline("locking", LOCKING.roles, tuple(gated_build if st.id == "build" else st
                                                            for st in LOCKING.steps))

        def fix(prompt, state, host):
            host.write(TEST_FILE, "assert 1")
            return fix_turn(1, 1)(prompt, state, host)
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "tests": [locked_tests_turn], "build": [build_touching_tests(1, "assert True"), fix],
            "review": [review_turn(1, APPROVE)]}, fake=fake, state=new_run(pipeline), pipeline=pipeline)

        self.assertEqual(wf.run(), APPROVE)
        quality_snapshots = [sha for (_, message), sha in zip(host.snapshots, host.trees, strict=True)
                             if "quality round" in message]
        self.assertEqual([host.trees[sha][TEST_FILE] for sha in quality_snapshots],
                         ["assert limit() == 3", "assert limit() == 3"])
        self.assertEqual([r["file"] for r in wf.state.restored], ["locked-build-1.diff", "locked-build-1-q1.diff"])

    def test_only_an_editing_step_may_lock(self):
        with self.assertRaisesRegex(ValueError, "step review locks the files it changes, so it must edit"):
            Pipeline("bad", QUICK.roles, (QUICK.steps[0], replace(QUICK.steps[1], locks=True)))
        # Its lock would hold the earlier step's work too.
        with self.assertRaisesRegex(ValueError, "step build locks the files it changes, so it must be the first "
                                                "editing step"):
            Pipeline("bad", FOUR.roles, tuple(replace(st, locks=st.id == "build") for st in FOUR.steps))
        # A second turn of it would lock the Builder's work of the round before.
        with self.assertRaisesRegex(ValueError, "step tests locks the files it changes, so the verdict loop must not "
                                                "run it again"):
            Pipeline("bad", FOUR.roles, tuple(replace(st, locks=st.id == "tests", file="tests-{n}.md")
                                              if st.id == "tests" else
                                              replace(st, loop_to="tests") if st.id == "review" else st
                                              for st in FOUR.steps))
        step = 'id = "tests"\nfile = "t.md"\nprompt = "{path}"\nlocks = true\n'
        with self.assertRaisesRegex(ValueError, "step tests locks the files it changes, so it must edit"):
            orchestrator.parse_workflow("bad", tomllib.loads(f"[[steps]]\n{step}"), BUILT_IN_PROMPTS)
        with self.assertRaisesRegex(ValueError, "locks must be true or false"):
            orchestrator.parse_workflow("bad", tomllib.loads(f'[[steps]]\n{step}'.replace("true", '"yes"')), BUILT_IN_PROMPTS)

    def test_locks_round_trip_through_a_workflow_file(self):
        text = orchestrator.workflow_toml(LOCKING)
        self.assertIn("locks = true", text)
        self.assertEqual(orchestrator.parse_workflow("locking", tomllib.loads(text), BUILT_IN_PROMPTS), LOCKING)


# A proof run_proofs writes that fails until a Builder turn fixes it.
FIXABLE = object()


class TestProofs(unittest.TestCase):
    """The verdict step's proofs, run for real by sh in a scratch directory that stands in for the project."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proj = tmp.name
        self.pipeline = replace(QUICK, proof_command="sh {proofs}")

    def run_proofs(self, *reviews, builds=2, fixes=(), max_rounds=3, pipeline=None, state=None, host=None):
        """A quick run in the scratch project whose Reviewer writes, per round, a review and {proof file: its exit
        status, or FIXABLE}; returns the workflow, the host and the Builder's prompts, and keeps the Reviewer's in
        self.reviewed. fixes lists, per Builder turn from round 2 on, the FIXABLE proofs it makes pass."""
        seen, self.reviewed = [], []

        def review(n, verdict, proofs):
            def turn(prompt, state, host):
                for name, exit_status in proofs.items():
                    os.makedirs(state.proofs_dir(n), exist_ok=True)
                    with open(f"{state.proofs_dir(n)}/{name}", "w") as f:
                        f.write(f"echo checking {name}\n" + (f"test -e fixed-{name} || exit 1\n" if exit_status is FIXABLE
                                                            else f"exit {exit_status}\n"))
                return writes(lambda s: s.review_path(n), f"VERDICT: {verdict}\n1. a bug\n2. another\n3. more\n")(
                    prompt, state, host)
            return recording(turn, self.reviewed)

        def build(n):
            def turn(prompt, state, host):
                for name in (fixes[n - 2] if 2 <= n < len(fixes) + 2 else ()):
                    open(f"{self.proj}/fixed-{name}", "w").close()
                return build_turn(n)(prompt, state, host)
            return recording(turn, seen)
        pipeline = pipeline or self.pipeline
        state = state or RunState("20260929-120000-a1b2c3", "add a rate limiter", self.proj, None, phase="build",
                                  workflow=pipeline.name)
        host = host or FakeHost()
        host._run = subprocess.run
        wf, herdr, host, _ = make_workflow({
            "build": [build(n) for n in range(1, builds + 1)],
            "review": [review(n, *r) for n, r in enumerate(reviews, 1)]},
            host=host, max_rounds=max_rounds, state=state, pipeline=pipeline, pull_request=False)
        return wf, host, seen

    def test_a_failing_proof_demonstrates_its_finding_and_the_builder_must_make_it_pass(self):
        wf, host, seen = self.run_proofs((CHANGES_REQUESTED, {"test_1_bug.sh": 1, "test_2.sh": 0, "test_3.sh": 4}),
                                         (APPROVE, {}))
        self.assertEqual(wf.run(), APPROVE)

        state = wf.state
        review = host.files[state.review_path(1)]
        self.assertTrue(review.startswith("VERDICT: CHANGES_REQUESTED\n1. a bug\n"))
        self.assertIn(f"## Proofs the orchestrator ran\n\nThe orchestrator ran each proof with `sh {{proofs}}` in {self.proj}.", review)
        self.assertIn("- `test_1_bug.sh` (finding 1): demonstrated, it fails.\n\n  ````\n  checking test_1_bug.sh\n"
                      "  ````", review)
        self.assertIn("- `test_2.sh` (finding 2): not demonstrated, it passes.", review)
        self.assertIn("- `test_3.sh` (finding 3): not demonstrated, it errors, with exit status 4.", review)
        self.assertEqual([(p["file"], p["finding"], p["demonstrated"]) for p in state.proofs if p["round"] == 1],
                         [("test_1_bug.sh", 1, True), ("test_2.sh", 2, False), ("test_3.sh", 3, False)])
        self.assertEqual(state.proofs[0]["said"], "it fails:\nchecking test_1_bug.sh")
        self.assertIn("\n\n" + orchestrator.PROOFS_NOTE.format(proofs=f"{state.proofs_dir(1)}/test_1_bug.sh"), seen[1])
        self.assertNotIn("Reviewer backed", seen[0])

    def test_findings_without_a_proof_stand_as_unproven(self):
        wf, host, _ = self.run_proofs((CHANGES_REQUESTED, {"test_2.sh": 1}), (APPROVE, {}))
        wf.run()
        self.assertIn("Findings without a proof, unproven: 1, 3.", host.files[wf.state.review_path(1)])

    def test_no_proofs_directory_leaves_the_review_as_it_is(self):
        wf, host, seen = self.run_proofs((CHANGES_REQUESTED, {}), (APPROVE, {}))
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(host.files[wf.state.review_path(1)],
                         "VERDICT: CHANGES_REQUESTED\n1. a bug\n2. another\n3. more\n")
        self.assertEqual(wf.state.proofs, [])
        self.assertNotIn("Reviewer backed", seen[1])

    def test_an_approval_runs_no_proof(self):
        wf, host, _ = self.run_proofs((APPROVE, {"test_1.sh": 1}), builds=1)
        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(orchestrator.PROOFS_HEADING, host.files[wf.state.review_path(1)])
        self.assertEqual(wf.state.proofs, [])

    def test_a_workflow_without_proof_command_runs_none(self):
        wf, host, _ = self.run_proofs((CHANGES_REQUESTED, {"test_1.sh": 1}), (APPROVE, {}), pipeline=QUICK)
        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(orchestrator.PROOFS_HEADING, host.files[wf.state.review_path(1)])

    def resumed_in_review(self, review, proofs):
        state = RunState("20260929-120000-a1b2c3", "add a rate limiter", self.proj, None, phase="review", round=1,
                         workflow="quick", base="abc123", proofs=proofs)
        host = FakeHost()
        # Round 2's review runs the demonstrated proofs again, and finds no file.
        host._run = subprocess.run
        host.files[state.review_path(1)] = review
        host.files[state.build_path(1)] = "report 1"
        seen = []
        wf, *_ = make_workflow({"build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)]},
                               host=host, state=state, pipeline=self.pipeline, pull_request=False)
        self.assertEqual(wf.run(), APPROVE)
        return wf, host, seen

    def test_a_review_judged_before_a_resume_is_not_judged_again(self):
        judged = f"VERDICT: CHANGES_REQUESTED\n1. a bug\n\n{orchestrator.PROOFS_HEADING}\n\nalready judged\n"
        proof = {"round": 1, "file": "test_1.sh", "finding": 1, "demonstrated": True, "said": "it fails"}
        wf, host, seen = self.resumed_in_review(judged, [proof])
        self.assertEqual(host.files[wf.state.review_path(1)], judged)
        self.assertEqual([p for p in wf.state.proofs if p["round"] == 1], [proof])
        self.assertIn(orchestrator.PROOFS_NOTE.format(proofs=f"{wf.state.proofs_dir(1)}/test_1.sh"), seen[0])

    def test_proofs_recorded_before_a_resume_are_appended_without_running_again(self):
        # No proof file is on disk: running one again would fail to find it.
        proof = {"round": 1, "file": "test_1.sh", "finding": 1, "demonstrated": True, "said": "it fails"}
        wf, host, _ = self.resumed_in_review("VERDICT: CHANGES_REQUESTED\n1. a bug\n", [proof])
        self.assertIn("- `test_1.sh` (finding 1): demonstrated, it fails.", host.files[wf.state.review_path(1)])

    def carried_note(self, *items):
        """The note the Reviewer hears of proofs from earlier rounds run again, items being its list's items."""
        return "\n\n" + orchestrator.CARRIED_PROOFS_NOTE.format(command="sh {proofs}", cwd=self.proj,
                                                                 proofs="\n\n".join(items))

    def test_a_demonstrated_proof_runs_again_every_round_until_it_passes(self):
        wf, host, seen = self.run_proofs(
            (CHANGES_REQUESTED, {"test_1_bug.sh": FIXABLE, "test_2.sh": FIXABLE, "test_3.sh": 0}),
            (CHANGES_REQUESTED, {"test_4.sh": 1}), (CHANGES_REQUESTED, {}), (APPROVE, {}),
            builds=4, fixes=[["test_2.sh"], [], ["test_1_bug.sh"]], max_rounds=4)
        self.assertEqual(wf.run(), APPROVE)
        state = wf.state
        one, two = state.proofs_dir(1), state.proofs_dir(2)

        self.assertNotIn("run again", self.reviewed[0])
        self.assertTrue(self.reviewed[1].endswith(self.carried_note(
            f"- `{one}/test_1_bug.sh`: still fails, it fails.\n\n  ````\n  checking test_1_bug.sh\n  ````",
            f"- `{one}/test_2.sh`: now passes.")), self.reviewed[1])
        self.assertTrue(self.reviewed[2].endswith(self.carried_note(
            f"- `{one}/test_1_bug.sh`: still fails, it fails.\n\n  ````\n  checking test_1_bug.sh\n  ````",
            f"- `{two}/test_4.sh`: still fails, it fails.\n\n  ````\n  checking test_4.sh\n  ````")))
        self.assertIn(self.carried_note(f"- `{one}/test_1_bug.sh`: now passes.",
                                        f"- `{two}/test_4.sh`: still fails, it fails.\n\n  ````\n  checking test_4.sh\n  ````"), self.reviewed[3])
        self.assertEqual([(p["round"], p["written"], p["file"], p["demonstrated"]) for p in state.proofs], [
            (1, 1, "test_1_bug.sh", True), (1, 1, "test_2.sh", True), (1, 1, "test_3.sh", False),
            (2, 1, "test_1_bug.sh", True), (2, 1, "test_2.sh", False), (2, 2, "test_4.sh", True),
            (3, 1, "test_1_bug.sh", True), (3, 2, "test_4.sh", True),
            (4, 1, "test_1_bug.sh", False), (4, 2, "test_4.sh", True)])

        # The step the review loops back to hears every proof that still fails, from whichever round wrote it.
        self.assertIn(orchestrator.PROOFS_NOTE.format(proofs=f"{one}/test_1_bug.sh, {one}/test_2.sh"), seen[1])
        self.assertIn(orchestrator.PROOFS_NOTE.format(proofs=f"{one}/test_1_bug.sh, {two}/test_4.sh"), seen[2])
        self.assertIn(orchestrator.PROOFS_NOTE.format(proofs=f"{one}/test_1_bug.sh, {two}/test_4.sh"), seen[3])

        # Round 1's review is the only one with proofs of its own written into it; reruns leave the reviews alone.
        self.assertNotIn(orchestrator.PROOFS_HEADING, host.files[state.review_path(3)])
        self.assertNotIn("test_1_bug", host.files[state.review_path(2)])

    def test_a_round_with_no_proof_to_carry_prompts_the_reviewer_as_before(self):
        # Proofs that pass or error are not carried, so round 2's review is prompted as in a run without proofs.
        wf, _, _ = self.run_proofs((CHANGES_REQUESTED, {"test_1.sh": 0, "test_2.sh": 4}), (APPROVE, {}))
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual([p["round"] for p in wf.state.proofs], [1, 1])
        carried = self.reviewed
        wf, _, _ = self.run_proofs((CHANGES_REQUESTED, {}), (APPROVE, {}))
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(carried, self.reviewed)

    def test_reruns_recorded_before_a_resume_are_not_run_again_and_still_heard(self):
        # No proof file is on disk: running one again would record that it errors.
        first = {"round": 1, "file": "test_1.sh", "finding": 1, "demonstrated": True, "said": "it fails"}
        rerun = {"round": 2, "written": 1, "file": "test_1.sh", "finding": 1, "demonstrated": True,
                 "said": "it fails:\nstill broken"}
        state = RunState("20260929-120000-a1b2c3", "add a rate limiter", self.proj, None, phase="review", round=2,
                         workflow="quick", base="abc123", proofs=[first, rerun])
        host = FakeHost()
        for n in (1, 2):
            host.files[state.build_path(n)] = f"report {n}"
        host.files[state.review_path(1)] = f"VERDICT: CHANGES_REQUESTED\n1. a bug\n\n{orchestrator.PROOFS_HEADING}\n"
        reviewed = []
        wf, *_ = make_workflow({"review": [recording(review_turn(2, APPROVE), reviewed)]},
                               host=host, state=state, pipeline=self.pipeline, pull_request=False)
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.proofs, [first, rerun])
        self.assertIn(self.carried_note(f"- `{state.proofs_dir(1)}/test_1.sh`: still fails, it fails.\n\n"
                                        "  ````\n  still broken\n  ````"), reviewed[0])

    def test_a_proof_recorded_before_reruns_existed_runs_again(self):
        saved = {"run_id": "20260929-120000-a1b2c3", "task": "add a rate limiter", "cwd": self.proj,
                 "phase": "build", "round": 2, "workflow": "quick", "base": "abc123",
                 "proofs": [{"round": 1, "file": "test_1.sh", "finding": 1, "demonstrated": True, "said": "it fails"}]}
        state = RunState.from_dict(saved)
        os.makedirs(state.proofs_dir(1))
        with open(f"{state.proofs_dir(1)}/test_1.sh", "w") as f:
            f.write("exit 0\n")
        host = FakeHost()
        host._run = subprocess.run
        host.files[state.build_path(1)] = "report 1"
        host.files[state.review_path(1)] = f"VERDICT: CHANGES_REQUESTED\n1. a bug\n\n{orchestrator.PROOFS_HEADING}\n"
        seen, reviewed = [], []
        wf, *_ = make_workflow({"build": [recording(build_turn(2), seen)],
                                "review": [recording(review_turn(2, APPROVE), reviewed)]},
                               host=host, state=state, pipeline=self.pipeline, pull_request=False)
        self.assertEqual(wf.run(), APPROVE)
        self.assertIn(orchestrator.PROOFS_NOTE.format(proofs=f"{state.proofs_dir(1)}/test_1.sh"), seen[0])
        self.assertEqual(wf.state.proofs[1], {"round": 2, "written": 1, "file": "test_1.sh", "finding": 1,
                                              "demonstrated": False, "said": "it passes"})
        self.assertIn(self.carried_note(f"- `{state.proofs_dir(1)}/test_1.sh`: now passes."),
                      reviewed[0])

    def test_a_proof_that_errors_when_run_again_stays_carried(self):
        # A change that makes a demonstrated proof crash has not made it pass.
        first = {"round": 1, "written": 1, "file": "test_1.sh", "finding": 1, "demonstrated": True, "said": "it fails"}
        state = RunState("20260929-120000-a1b2c3", "add a rate limiter", self.proj, None, phase="build", round=2,
                         workflow="quick", base="abc123", proofs=[first])
        os.makedirs(state.proofs_dir(1))
        with open(f"{state.proofs_dir(1)}/test_1.sh", "w") as f:
            f.write("echo crashed\nexit 2\n")
        host = FakeHost()
        host._run = subprocess.run
        host.files[state.build_path(1)] = "report 1"
        host.files[state.review_path(1)] = f"VERDICT: CHANGES_REQUESTED\n1. a bug\n\n{orchestrator.PROOFS_HEADING}\n"
        seen, reviewed = [], []
        wf, *_ = make_workflow({"build": [recording(build_turn(n), seen) for n in (2, 3)],
                                "review": [recording(review_turn(2, CHANGES_REQUESTED), reviewed),
                                           recording(review_turn(3, APPROVE), reviewed)]},
                               host=host, state=state, pipeline=self.pipeline, pull_request=False)
        self.assertEqual(wf.run(), APPROVE)
        path = f"{state.proofs_dir(1)}/test_1.sh"
        for prompt in reviewed:
            self.assertIn(self.carried_note(f"- `{path}`: no longer fails but errors, it errors, with exit status 2."
                                            "\n\n  ````\n  crashed\n  ````"), prompt)
        self.assertIn(orchestrator.PROOFS_NOTE.format(proofs=path), seen[1])
        self.assertEqual([(p["round"], p["demonstrated"]) for p in wf.state.proofs], [(1, True), (2, False), (3, False)])

    def test_the_reviewers_own_proofs_heading_does_not_stop_the_run(self):
        wf, host, _ = self.run_proofs((CHANGES_REQUESTED, {"test_1.sh": 1}), (APPROVE, {}))
        real_read = host.read

        def read(path):
            text = real_read(path)
            if path == wf.state.review_path(1) and text and orchestrator.PROOFS_HEADING not in text:
                return text + "\n## Proofs\n\n1. test_1.sh\n"
            return text
        host.read = read
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual([p["demonstrated"] for p in wf.state.proofs if p["round"] == 1], [True])

    def test_a_proof_that_runs_too_long_is_stopped_with_what_it_started(self):
        with patch.object(orchestrator, "PROOF_TIMEOUT", 1):
            wf, host, _ = self.run_proofs((CHANGES_REQUESTED, {}), (APPROVE, {}))
            os.makedirs(wf.state.proofs_dir(1))
            with open(f"{wf.state.proofs_dir(1)}/test_1.sh", "w") as f:
                f.write(f"(sleep 3; echo leaked > {self.proj}/leaked) &\nsleep 30\n")
            failed, said = wf._run_proof("sh {proofs}", f"{wf.state.proofs_dir(1)}/test_1.sh")
        self.assertEqual((failed, said), (False, "it errors, running out of its 1 s"))
        time.sleep(4)
        self.assertFalse(os.path.exists(f"{self.proj}/leaked"))

    def test_proof_command_needs_a_verdict_step_and_names_the_proof_once(self):
        with self.assertRaisesRegex(ValueError, "proof_command runs the verdict step's proofs, and the workflow has no "
                                                "verdict step"):
            replace(SOLO, proof_command="sh {proofs}")
        for command in ("sh", "sh {proofs} {proofs}", "sh {proofs} {task}", "sh {proofs"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "proof_command"):
                replace(QUICK, proof_command=command)
        with self.assertRaisesRegex(ValueError, "proof_command must be a string"):
            orchestrator.parse_workflow("bad", {**orchestrator.workflow_definition(QUICK), "proof_command": 1}, BUILT_IN_PROMPTS)

    def test_proof_command_round_trips_through_a_workflow_file(self):
        text = orchestrator.workflow_toml(self.pipeline)
        self.assertIn('proof_command = "sh {proofs}"', text)
        self.assertEqual(orchestrator.parse_workflow("quick", tomllib.loads(text), BUILT_IN_PROMPTS), self.pipeline)
        self.assertNotIn("proof_command", orchestrator.workflow_toml(QUICK))

    def test_the_adversarial_example_loads(self):
        adversarial = orchestrator.load_workflow_file(f"{EXAMPLES}/adversarial.toml", BUILT_IN_PROMPTS)
        self.assertEqual(orchestrator.workflow_shape(adversarial), "spec -> build -> review (back to build)")
        self.assertIn("{proofs}", adversarial.proof_command)
        self.assertIn("{proofs_dir}", adversarial.step("review").prompt)
        self.assertIn("{proofs_dir}", adversarial.step("review").again)


class TestSavedBeforeWorkflows(unittest.TestCase):
    """state.json as commit d6bb100 wrote it, without a workflow field."""

    def old(self, state):
        saved = asdict(state)
        del saved["workflow"]
        return RunState.from_dict(saved)

    def test_each_phase_resumes_under_the_default(self):
        full = ("spec", "build", "review")
        cases = {
            "spec": (saved_run("spec", 0, pull_request=True),
                     {"spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, None),
            "build": (saved_run("build", 1, agents=full),
                      {"build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, None),
            "review": (saved_run("review", 2, agents=full),
                       {"review": [review_turn(2, APPROVE)]}, {lambda s: s.build_path(2): "report 2"}),
            "publish": (saved_run("publish", 1, agents=full, pull_request=True, base_branch="main",
                                  branch="orchestrator/x-a1b2c3", verdict=APPROVE), {}, None),
            "done": (saved_run("done", 1, agents=full, verdict=APPROVE), {}, None),
        }
        for phase, (state, script, files) in cases.items():
            with self.subTest(phase=phase):
                host = FakeHost(head="def456" if phase == "publish" else "abc123", branch=state.branch or "main")
                wf, *_ = resume(self.old(state), script, host=host, files=files)
                self.assertEqual(wf.state.workflow, "default")
                self.assertEqual(wf.run(), APPROVE)
                self.assertEqual(wf.state.phase, "done")

    def test_quality_resumes_under_the_default(self):
        reviews = []
        state = self.old(quality_state())
        wf, herdr, host, _ = resume_gated(state, {"review": [recording(review_turn(1, APPROVE), reviews)]}, FakeCI(),
                                          files={lambda s: s.quality_path(1, 1): "GATE: OK\n"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.workflow, "default")
        self.assertEqual(wf.state.phase, "done")
        self.assertIn(orchestrator.QUALITY_PASSED_NOTE.format(quality_path=state.quality_path(1, 1)), reviews[0])

    def test_root_pane_comes_from_the_workflows_first_role(self):
        saved = {"run_id": "r-abc", "task": "t", "cwd": "/p", "workflow": "quick",
                 "agents": {"review": {"name": "review-abc", "pane": "w9:p2"}, "build": {"name": "build-abc",
                                                                                     "pane": "w9:p1"}}}
        with patch.dict(orchestrator.WORKFLOWS, {"quick": QUICK}):
            self.assertEqual(RunState.from_dict(saved).root_pane, "w9:p1")
        self.assertEqual(RunState.from_dict(saved).root_pane, "")


def without_workflow_files(test):
    """Point the orchestrator's config directory at an empty one for the test; returns it."""
    config = tempfile.TemporaryDirectory()
    test.addCleanup(config.cleanup)
    env = patch.dict("os.environ", {"XDG_CONFIG_HOME": config.name})
    env.start()
    test.addCleanup(env.stop)
    return config.name


class TestWorkflowCLI(unittest.TestCase):
    def setUp(self):
        without_workflow_files(self)

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_default_workflow_is_the_default(self, _resolve, _run, init):
        with patch("builtins.print") as out, patch("orchestrator.new_run_id", return_value="20260930-070000-c0ffee"):
            self.assertEqual(main(["run", "task", "--model", "m"]), 0)
            self.assertEqual(main(["run", "task", "--model", "m", "--workflow", "default"]), 0)
        first, second = init.call_args_list
        self.assertEqual(first.kwargs, second.kwargs)
        self.assertEqual(asdict(first.args[2]), asdict(second.args[2]))
        self.assertEqual((first.args[2].workflow, first.args[2].phase), ("default", "spec"))
        self.assertEqual(out.call_args_list[0], out.call_args_list[1])

    def test_unknown_workflow_is_an_argparse_error(self):
        with self.assertRaises(SystemExit), patch("sys.stderr") as err:
            parse_args(["run", "task", "--workflow", "nope"])
        message = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertRegex(message, r"argument --workflow: invalid choice: 'nope' \(choose from '?default'?\)")

    def test_resume_has_no_workflow_flag(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["resume", "a1b2c3", "--workflow", "default"])

    def test_resuming_an_unknown_workflow(self):
        saved = {**asdict(saved_run("build", 1)), "workflow": "nope"}
        args = parse_args(["resume", "a1b2c3"])
        with self.assertRaisesRegex(OrchestratorError, "unknown workflow nope; this orchestrator has default"):
            orchestrator.resumable_state([(10**4, saved)], args, "/proj", "here", lambda pid: True)

    def test_resume_limit_against_the_quality_round_of_another_gated_step(self):
        saved = asdict(saved_run("impl", 1, agents=(), quality_job=JOB, quality_round=2, workflow="impl"))
        args = parse_args(["resume", "a1b2c3", "--max-quality-rounds", "2"])
        with patch.dict(orchestrator.WORKFLOWS, {"impl": IMPL}):
            with self.assertRaisesRegex(OrchestratorError, "already in quality round 2 of its impl phase"):
                orchestrator.resumable_state([(10**4, saved)], args, "/proj", "here", lambda pid: False)

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_registered_workflow_is_offered_and_started(self, _resolve, _run, init):
        with patch.dict(orchestrator.WORKFLOWS, {"quick": QUICK}), patch("builtins.print") as out:
            self.assertEqual(main(["run", "task", "--workflow", "quick", "--model", "X"]), 0)
        state = init.call_args.args[2]
        self.assertEqual((state.workflow, state.phase), ("quick", "build"))
        self.assertEqual(init.call_args.kwargs["agents"].models, {"build": "X", "review": "X"})
        out.assert_called_with(f"{APPROVE}: {state.dir}/review-0.md")

    def test_list_names_a_non_default_workflow(self):
        runs = [(10**6, run_record(phase="done", verdict=APPROVE)),
                (10**6, {**run_record(phase="done", verdict=APPROVE), "workflow": "default"}),
                (10**6, {**run_record(phase="done", verdict=APPROVE), "workflow": "quick"}),
                (10**6, {**run_record(phase="impl"), "quality_round": 2, "workflow": "impl"})]
        with patch("builtins.print") as out, patch.dict(orchestrator.WORKFLOWS, {"impl": IMPL}):
            orchestrator.print_runs(runs, "here", lambda pid: True, [])
        lines = [c.args[0] for c in out.call_args_list][::2]
        self.assertEqual(lines[:3], [
            f"20260930-070000-c0ffee  done     round 1  {APPROVE}",
            f"20260930-070000-c0ffee  done     round 1  {APPROVE}",
            f"20260930-070000-c0ffee  done     round 1  {APPROVE}  [quick workflow]",
        ])
        self.assertTrue(lines[3].startswith("20260930-070000-c0ffee  impl     round 1 q2  stale"))
        self.assertTrue(lines[3].endswith("  [impl workflow]"))


EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "workflows")


class TestWorkflowFiles(unittest.TestCase):
    def setUp(self):
        self.workflows = os.path.join(without_workflow_files(self), "ai-agents-orchestrator", "workflows")
        os.makedirs(self.workflows)

    def file(self, name, text):
        path = os.path.join(self.workflows, f"{name}.toml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_examples_load(self):
        default = orchestrator.DEFAULT_WORKFLOW
        quick = orchestrator.load_workflow_file(f"{EXAMPLES}/quick.toml", BUILT_IN_PROMPTS)
        self.assertEqual(orchestrator.workflow_shape(quick), "build -> review (back to build)")
        tdd = orchestrator.load_workflow_file(f"{EXAMPLES}/tdd.toml", BUILT_IN_PROMPTS)
        self.assertEqual(tdd.name, "tdd")
        self.assertEqual(tdd.roles, {"spec": "Spec Collector", "tests": "Test Writer", "build": "Builder",
                                     "review": "Reviewer"})
        self.assertEqual(tdd.models, {"tests": "sonnet"})
        spec, tests, build, review = tdd.steps
        self.assertEqual((spec, review), (default.step("spec"), default.step("review")))
        # use keeps every field the file does not set.
        self.assertNotEqual(build.prompt, default.step("build").prompt)
        self.assertEqual(Step(**{**orchestrator.step_fields(build), "prompt": default.step("build").prompt}),
                         default.step("build"))
        self.assertEqual((tests.role, tests.file, tests.edits, tests.locks), ("tests", "tests.md", True, True))

    def test_default_round_trips_through_its_file(self):
        default = orchestrator.DEFAULT_WORKFLOW
        text = orchestrator.workflow_toml(default)
        self.assertEqual(orchestrator.parse_workflow("default", tomllib.loads(text), BUILT_IN_PROMPTS), default)

    def test_the_change_budget_is_the_workflows(self):
        step = 'id = "build"\nfile = "b-{n}.md"\nprompt = "{path}"\nedits = true\n'
        p = orchestrator.load_workflow_file(self.file("small", f"max_files = 1\nmax_lines = 100\n[[steps]]\n{step}"), BUILT_IN_PROMPTS)
        self.assertEqual((p.max_files, p.max_lines), (1, 100))
        self.assertEqual(orchestrator.parse_workflow("small", tomllib.loads(orchestrator.workflow_toml(p)), BUILT_IN_PROMPTS), p)
        self.assertEqual(orchestrator.workflow_definition(p)["max_lines"], 100)
        default = orchestrator.DEFAULT_WORKFLOW
        self.assertEqual((default.max_files, default.max_lines), (15, 600))
        self.assertNotIn("max_files", orchestrator.workflow_definition(default))
        for value, message in (("0", "workflow bad: max_lines must be at least 1"),
                               ("-1", "workflow bad: max_lines must be at least 1"),
                               ('"3"', "max_lines must be a whole number"), ("true", "max_lines must be a whole number"),
                               ("1.5", "max_lines must be a whole number")):
            with self.subTest(value=value), self.assertRaisesRegex(OrchestratorError, message):
                orchestrator.load_workflow_file(self.file("bad", f"max_lines = {value}\n[[steps]]\n{step}"), BUILT_IN_PROMPTS)

    def test_any_text_round_trips(self):
        odd = ["ends in a quote '", "has ''' inside\nand a newline", "tab\there", "del\x7f", "ünïcode 🦀\n",
               "\nstarts with a newline", 'back\\slash "quoted"\n']
        p = Pipeline("odd", {"b": "Bob's \"pane\""}, tuple(
            Step(f"s{i}", "b", f"s{i}.md", text + " {path}") for i, text in enumerate(odd)),
            models={"b": "m"}, description="with ''' and '")
        self.assertEqual(orchestrator.parse_workflow("odd", tomllib.loads(orchestrator.workflow_toml(p)), BUILT_IN_PROMPTS), p)

    def test_roles_default_from_the_steps(self):
        p = orchestrator.load_workflow_file(self.file("mine", """
            [[steps]]
            id = "test_writer"
            file = "tests.md"
            prompt = "Write tests for {task}; report to {path}."
            edits = true

            [[steps]]
            use = "build"
            prompt = "Make {test_writer_path} pass; report to {build_path}."
            again = "Fix them; report to {build_path}."
            fresh_note = "Earlier: {earlier_build_paths}."
            quality_gated = false
        """), BUILT_IN_PROMPTS)
        self.assertEqual(p.roles, {"test_writer": "Test Writer", "build": "Builder"})
        self.assertEqual(p.steps[0].role, "test_writer")
        self.assertEqual((p.steps[1].edits, p.steps[1].quality_gated, p.steps[1].fresh_repeats_again),
                         (True, False, True))
        self.assertEqual(p.models, {})

    def test_panes_follow_the_steps_not_the_roles_table(self):
        p = orchestrator.load_workflow_file(self.file("mine", """
            roles.review = { model = "opus" }
            roles.build = "Implementer"
            [[steps]]
            use = "spec"
            [[steps]]
            use = "build"
            [[steps]]
            use = "review"
        """), BUILT_IN_PROMPTS)
        self.assertEqual(p.roles, {"spec": "Spec Collector", "build": "Implementer", "review": "Reviewer"})
        self.assertEqual(p.models, {"review": "opus"})

    def test_invalid_files_name_the_problem(self):
        step = 'id = "build"\nfile = "b-{n}.md"\nprompt = "{path}"\nedits = true\n'
        cases = [
            ("step 1 (build): unknown key edit; did you mean edits?", f"[[steps]]\n{step}edit = true\n"),
            ("the file: unknown key step; did you mean steps?", f"[[step]]\n{step}"),
            ("it has no [[steps]]", 'description = "x"\n'),
            ("step 1 (build): file is missing", '[[steps]]\nid = "build"\nprompt = "x"\n'),
            ("step 1 (build): use names no step of the default workflow; it has spec, build, review",
             f'[[steps]]\nuse = "test"\n{step}'),
            ("step 1 (build): edits must be true or false", "[[steps]]\n" + step.replace("true", '"yes"')),
            ("step 1 (build): prompt: Single '}' encountered in format string; write a literal brace as {{ or }}",
             '[[steps]]\nid = "build"\nfile = "b-{n}.md"\nprompt = "a } b"\nedits = true\n'),
            ("role build: give a label, or a table with label, model and agent",
             f"roles.build = 3\n[[steps]]\n{step}"),
            ("role build: unknown key modle; did you mean model?",
             f'roles.build = {{ modle = "x" }}\n[[steps]]\n{step}'),
            ("step 1 (a b): id may use only letters, digits, - and _",
             "[[steps]]\n" + step.replace('"build"', '"a b"')),
            ("workflow bad: duplicate step id build", f"[[steps]]\n{step}[[steps]]\n{step}"),
            ("workflow bad: role y has no step", f'[roles]\ny = {{ model = "m" }}\n[[steps]]\n{step}'),
            ("Expected '=' after a key", "this is not toml\n"),
            ("roles must be a table", f"roles = 3\n[[steps]]\n{step}"),
            ("description must be a string", f"description = 3\n[[steps]]\n{step}"),
            ("role a b: a role key may use only letters, digits, - and _", f'roles."a b" = "x"\n[[steps]]\n{step}'),
            ("role build: label must be a string", f"roles.build = {{ label = 3 }}\n[[steps]]\n{step}"),
            ("step 1 must be a table", "steps = [3]\n"),
            ("step 1 (build): prompt must be a string", "[[steps]]\n" + step.replace('"{path}"', "3")),
            # Placeholders that are all known, but that could not be filled in when the step comes up.
            ("step build: prompt cannot be filled in: ValueError: Unknown format code 'd'",
             "[[steps]]\n" + step.replace('"{path}"', '"{task:d}"')),
            ("step build: prompt cannot be filled in: ValueError: Unknown conversion specifier z",
             "[[steps]]\n" + step.replace('"{path}"', '"{task!z}"')),
            ("step build: again cannot be filled in: KeyError: 'nope'",
             f'[[steps]]\n{step}again = "{{task:{{nope}}}}"\n'),
        ]
        for name in ("b-{n!s}.md", "b-{n:d}.md", "b-{n:{x}}.md"):
            cases.append((f"step build: handoff file {name} may use {{n}} only as it is, with no format spec or "
                          f"conversion", "[[steps]]\n" + step.replace("b-{n}.md", name)))
        for name in ("state.json", "summary.md", "metrics.json", "quality-{n}-1.md", "proofs-{n}", "../b-{n}.md",
                     "/tmp/b-{n}.md", ".."):
            cases.append((f"step build: handoff file {name} must be a plain file name, and not state.json, "
                          f"summary.md, metrics.json, quality-*, locked-* or proofs-*, which the run writes itself",
                          "[[steps]]\n" + step.replace("b-{n}.md", name)))
        for message, text in cases:
            with self.subTest(message=message):
                path = self.file("bad", text)
                with self.assertRaises(OrchestratorError) as e:
                    orchestrator.load_workflow_file(path, BUILT_IN_PROMPTS)
                self.assertTrue(str(e.exception).startswith(f"{path}: "), str(e.exception))
                self.assertIn(message, str(e.exception))

    def test_a_saved_definition_that_cannot_be_filled_in_stops_the_resume(self):
        definition = orchestrator.workflow_definition(QUICK)
        definition["steps"][0]["prompt"] = "{task:d}"
        saved = {**asdict(saved_run("build", 1)), "workflow": "quick", "workflow_definition": definition}
        args = parse_args(["resume", "a1b2c3"])
        with self.assertRaisesRegex(OrchestratorError, "saved with the run is invalid: workflow quick: step build: "
                                                       "prompt cannot be filled in"):
            orchestrator.resumable_state([(10**4, saved)], args, "/proj", "here", lambda pid: True)

    def test_steps_whose_files_can_share_a_name(self):
        default = orchestrator.DEFAULT_WORKFLOW
        spec, build, review = default.steps
        b = Step("b", "b", "x{n}.md", "{path}", edits=True)
        cases = [
            ("steps spec and build can both write build-1.md",
             [Step("spec", "spec", "build-1.md", "{path}"), build, review]),
            ("steps build and review can both write build-1-q1.md",
             [spec, build, Step("review", "review", "build-{n}-q1.md", "{path}", loop_to="build")]),
            ("steps b and r can both write x1.md", [b, Step("r", "b", "x1.md", "{path}")]),
            # {n} in the extension: a quality answer goes before it, as b-q3.1.
            ("steps a and r can both write b-q3.1",
             [Step("a", "a", "b.{n}", "{path}", edits=True, quality_gated=True),
              Step("r", "r", "b-q3.{n}", "{path}", loop_to="a")]),
            ("steps a and z can both write b-q3.1",
             [Step("a", "a", "b.{n}", "{path}", edits=True, quality_gated=True), Step("z", "z", "b-q3.1", "{path}")]),
            # Rounds that differ give one name: round 3 of a and round 5 of r.
            ("steps a and r can both write x35.md",
             [Step("a", "a", "x{n}5.md", "{path}", edits=True), Step("r", "r", "x3{n}.md", "{path}", loop_to="a")]),
            ("steps a and r can both write x3-5.md",
             [Step("a", "a", "x{n}-5.md", "{path}", edits=True), Step("r", "r", "x3-{n}.md", "{path}", loop_to="a")]),
            ("steps a and r can both write 31.33",
             [Step("a", "a", "{n}.33", "{path}", edits=True), Step("r", "r", "31.{n}", "{path}", loop_to="a")]),
        ]
        for message, steps in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                Pipeline("bad", {st.role: st.role for st in steps}, tuple(steps))
        # The gated step's own answers, and names that only look alike, are not collisions.
        Pipeline("ok", {"b": "B"}, (Step("b", "b", "x{n}.md", "{path}", edits=True, quality_gated=True),
                                    Step("r", "b", "x{n}-r.md", "{path}", loop_to="b")))
        Pipeline("ok", {"b": "B"}, (Step("s", "b", "notes", "{path}"), Step("b", "b", "notes-{n}", "{path}",
                                                                            edits=True, quality_gated=True)))
        # A round number is never 0 and has no leading zero, and a digit beside {n} is fine on its own.
        Pipeline("ok", {"b": "B"}, (Step("s", "b", "x0.md", "{path}"), Step("b", "b", "x{n}.md", "{path}", edits=True),
                                    Step("r", "b", "x0{n}.md", "{path}", loop_to="b")))
        Pipeline("ok", {"b": "B"}, (Step("b", "b", "x-{n}.md", "{path}", edits=True),
                                    Step("r", "b", "x3{n}.md", "{path}", loop_to="b")))

    def test_a_model_for_a_role_not_in_the_workflow(self):
        steps = (Step("build", "build", "b.md", "{path}"),)
        with self.assertRaisesRegex(ValueError, "a model is set for role x, which is not one of the workflow's"):
            Pipeline("bad", {"build": "Builder"}, steps, models={"x": "m"})

    def test_a_file_cannot_replace_a_built_in_workflow(self):
        path = self.file("default", '[[steps]]\nuse = "spec"\n')
        with self.assertRaisesRegex(OrchestratorError, "workflow default is built in; give the file another name"):
            orchestrator.load_workflow_file(path, BUILT_IN_PROMPTS)

    def test_a_name_that_cannot_be_a_workflows(self):
        path = self.file("my flow", f"[[steps]]\nuse = \"spec\"\n")
        with self.assertRaisesRegex(OrchestratorError, "a workflow's name, its file's, may use only letters"):
            orchestrator.load_workflow_file(path, BUILT_IN_PROMPTS)

    def test_an_unlistable_workflows_directory(self):
        os.rmdir(self.workflows)
        with open(self.workflows, "w") as f:
            f.write("a file, not a directory")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["workflows"]), orchestrator.EXIT_ERROR)
        self.assertIn(f"error: cannot list {self.workflows}", err.getvalue())
        self.assertEqual(parse_args(["run", "task"]).workflow, "default")

    def test_a_missing_file(self):
        with self.assertRaisesRegex(OrchestratorError, "cannot read workflow file /nope/x.toml"):
            orchestrator.load_workflow_file("/nope/x.toml", BUILT_IN_PROMPTS)

    def test_files_are_offered_by_name(self):
        with open(f"{EXAMPLES}/tdd.toml") as f:
            self.file("tdd", f.read())
        self.file("broken", "nonsense\n")
        with open(os.path.join(self.workflows, "notes.txt"), "w") as f:
            f.write("not a workflow")
        self.assertEqual(orchestrator.workflow_names(), ["broken", "default", "tdd"])
        self.assertEqual(orchestrator.named_workflow("tdd", BUILT_IN_PROMPTS).name, "tdd")
        with self.assertRaisesRegex(OrchestratorError, "unknown workflow nope; there are broken, default, tdd"):
            orchestrator.named_workflow("nope", BUILT_IN_PROMPTS)

    def test_a_run_keeps_its_workflow_when_the_file_is_gone(self):
        with open(f"{EXAMPLES}/quick.toml") as f:
            quick = orchestrator.load_workflow_file(self.file("quick", f.read()), BUILT_IN_PROMPTS)

        def build_then_interrupt(prompt, state, host):
            build_turn(2)(prompt, state, host)
            raise KeyboardInterrupt
        wf, _, host, _ = make_workflow({
            "build": [build_turn(1), build_then_interrupt], "review": [review_turn(1, CHANGES_REQUESTED)],
        }, state=new_run(quick), pipeline=quick)
        with self.assertRaises(KeyboardInterrupt):
            wf.run()
        saved = json.loads(host.files[f"{D}/state.json"])
        self.assertEqual(saved["workflow_definition"], orchestrator.workflow_definition(quick))
        os.remove(os.path.join(self.workflows, "quick.toml"))

        seen = []
        state = RunState.from_dict(saved)
        self.assertEqual(state.root_pane, saved["agents"]["build"]["pane"])
        wf2, *_ = resume(state, {"review": [recording(review_turn(2, APPROVE), seen)]}, host=host)
        self.assertEqual(wf2.run(), APPROVE)
        self.assertEqual(wf2.pipeline, quick)
        self.assertTrue(seen[0].startswith(f"The Builder has answered your review; the new report is in "
                                           f"{D}/build-2.md."))

    def test_a_built_in_workflow_is_saved_by_name_only(self):
        wf, *_ = make_workflow({"spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]},
                               pull_request=False)
        wf.run()
        self.assertEqual((wf.state.workflow, wf.state.workflow_definition), ("default", None))

    def test_list_reads_the_gated_step_from_the_saved_definition(self):
        runs = [(10**6, {**run_record(phase="impl"), "quality_round": 2, "workflow": "impl",
                         "workflow_definition": orchestrator.workflow_definition(IMPL)})]
        with patch("builtins.print") as out:
            orchestrator.print_runs(runs, "here", lambda pid: True, [])
        self.assertTrue(out.call_args_list[0].args[0].startswith("20260930-070000-c0ffee  impl     round 1 q2"))

    def test_a_corrupt_saved_definition_is_an_error(self):
        saved = {**asdict(saved_run("build", 1)), "workflow": "quick", "workflow_definition": {"steps": []}}
        args = parse_args(["resume", "a1b2c3"])
        with self.assertRaisesRegex(OrchestratorError, r"the definition of workflow quick saved with the run is "
                                                       r"invalid: it has no \[\[steps\]\]"):
            orchestrator.resumable_state([(10**4, saved)], args, "/proj", "here", lambda pid: True)


@patch.object(Workflow, "__init__", return_value=None)
@patch.object(Workflow, "run", return_value=APPROVE)
@patch.object(Host, "resolve_dir", return_value="/proj")
@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestWorkflowFileCLI(unittest.TestCase):
    def setUp(self):
        workflows = os.path.join(without_workflow_files(self), "ai-agents-orchestrator", "workflows")
        os.makedirs(workflows)
        with open(f"{EXAMPLES}/tdd.toml") as src, open(f"{workflows}/tdd.toml", "w") as dst:
            dst.write(src.read())

    def started(self, init, *flags):
        with patch("builtins.print"):
            self.assertEqual(main(["run", "task", *flags]), 0)
        return init.call_args.args[2], init.call_args.kwargs

    def test_run_a_workflow_file_by_name(self, _resolve, _run, init):
        state, kw = self.started(init, "--workflow", "tdd")
        self.assertEqual((state.workflow, state.phase), ("tdd", "spec"))
        self.assertEqual(kw["pipeline"], orchestrator.load_workflow_file(f"{EXAMPLES}/tdd.toml", BUILT_IN_PROMPTS))
        self.assertEqual(kw["agents"].models, {"tests": "sonnet"})

    def test_run_a_workflow_file_by_path(self, _resolve, _run, init):
        state, kw = self.started(init, "--workflow-file", f"{EXAMPLES}/quick.toml")
        self.assertEqual((state.workflow, state.phase, kw["pipeline"].name), ("quick", "build", "quick"))

    def test_model_flags_override_the_files_models(self, _resolve, _run, init):
        _, kw = self.started(init, "--workflow", "tdd", "--model", "X")
        self.assertEqual(kw["agents"].models, {"spec": "X", "tests": "X", "build": "X", "review": "X"})
        _, kw = self.started(init, "--workflow", "tdd", "--role-model", "tests=haiku", "--role-model", "spec=opus",
                             "--build-model", "Y")
        self.assertEqual(kw["agents"].models, {"spec": "opus", "tests": "haiku", "build": "Y"})

    def test_role_model_for_a_role_the_workflow_lacks(self, _resolve, _run, init):
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["run", "task", "--role-model", "tests=haiku"]), orchestrator.EXIT_ERROR)
        self.assertIn("--role-model names role tests, which the workflow does not have; its roles are spec, "
                      "build, review", err.getvalue())
        init.assert_not_called()

    def test_bad_flags(self, _resolve, _run, init):
        for argv in (["run", "task", "--role-model", "tests"],
                     ["run", "task", "--workflow", "tdd", "--workflow-file", "x.toml"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), patch("sys.stderr"):
                parse_args(argv)

    def test_resume_takes_role_model(self, _resolve, _run, init):
        tdd = orchestrator.named_workflow("tdd", BUILT_IN_PROMPTS)
        saved = {**asdict(saved_run("build", 1, models={"tests": "sonnet"})), "workflow": "tdd",
                 "workflow_definition": orchestrator.workflow_definition(tdd)}
        args = parse_args(["resume", "a1b2c3", "--role-model", "review=opus"])
        state = orchestrator.resumable_state([(10**4, saved)], args, "/proj", "here", lambda pid: False)
        self.assertEqual(state.models, {"tests": "sonnet", "review": "opus"})

    def test_workflows_lists_them(self, _resolve, _run, init):
        workflows = orchestrator.workflows_dir()
        with open(f"{workflows}/broken.toml", "w") as f:
            f.write("[[steps]]\nid = 3\n")
        with patch.dict("os.environ", {"HERDR_ENV": ""}), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["workflows"]), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "default  built in",
            "    spec -> build -> review (back to build)",
            f"broken  {workflows}/broken.toml",
            "    error: step 1: file is missing",
            f"tdd  {workflows}/tdd.toml",
            "    spec -> tests -> build -> review (back to build)",
            "    Spec Collector -> Test Writer -> Builder <-> Reviewer: tests first, by another session",
            "",
            f"Add one as {workflows}/NAME.toml; `orchestrator.py workflows default` prints the default as a file "
            f"to start from.",
        ])

    def test_workflows_prints_one_as_a_file(self, _resolve, _run, init):
        for ref in ("tdd", f"{EXAMPLES}/tdd.toml"):
            with self.subTest(ref=ref), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(main(["workflows", ref]), 0)
                self.assertEqual(orchestrator.parse_workflow("tdd", tomllib.loads(out.getvalue()), BUILT_IN_PROMPTS),
                                 orchestrator.named_workflow("tdd", BUILT_IN_PROMPTS))


class TestPromptFiles(unittest.TestCase):
    """Prompt files in the config directory, in place of the built-in prompts."""

    def setUp(self):
        self.prompts = os.path.join(without_workflow_files(self), "ai-agents-orchestrator", "prompts")

    def write(self, name, text):
        """A prompt file; text as bytes is written as it is, a str as UTF-8 with the newline an editor ends it with."""
        os.makedirs(self.prompts, exist_ok=True)
        path = os.path.join(self.prompts, name)
        with open(path, "wb") as f:
            f.write(text if isinstance(text, bytes) else f"{text}\n".encode())
        return path

    def started(self, *flags):
        """A new run's state and workflow, as run with flags makes them."""
        with patch("orchestrator.new_run_id", return_value="20260929-120000-a1b2c3"):
            return orchestrator.new_state(parse_args(["run", "task", *flags]), "/proj")

    def test_a_build_prompt_file_words_the_builders_first_turn(self):
        self.write("BUILD_PROMPT.md", "Build {spec_path} in {cwd}; report to {build_path}.")
        state, pipeline = self.started()
        seen = []
        wf, *_ = make_workflow({"spec": [spec_turn], "build": [recording(build_turn(1), seen)],
                                "review": [review_turn(1, APPROVE)]}, state=state, pipeline=pipeline,
                               pull_request=False)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen, [f"Build {D}/spec.md in /proj; report to {D}/build-1.md.{NOTE}"])
        self.assertEqual(state.prompt_overrides, {"BUILD_PROMPT": "Build {spec_path} in {cwd}; report to {build_path}."})
        # Its name and the overrides give the workflow back, so the run saves no definition of it.
        self.assertIsNone(state.workflow_definition)

    def test_a_follow_up_file_reaches_the_turn_that_uses_it(self):
        self.write("FIX_PROMPT.md", "Fix {prev_review_path}; report to {build_path}.")
        state, pipeline = self.started()
        seen = []
        wf, *_ = make_workflow({"spec": [spec_turn], "build": [build_turn(1), recording(build_turn(2), seen)],
                                "review": [review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)]},
                               state=state, pipeline=pipeline, pull_request=False)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen, [f"Fix {D}/review-1.md; report to {D}/build-2.md.{NOTE}"])

    def test_a_note_file_reaches_the_turn_that_uses_it(self):
        self.write("QUALITY_PASSED_NOTE.md", "Gate passed: {quality_path}.")
        reviews = []
        state = quality_state(prompt_overrides=orchestrator.load_prompts().overrides)
        wf, *_ = resume_gated(state, {"review": [recording(review_turn(1, APPROVE), reviews)]}, FakeCI(),
                              files={lambda s: s.quality_path(1, 1): "GATE: OK\n"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertTrue(reviews[0].endswith(f"\n\nGate passed: {D}/quality-1-1.md."), reviews[0])
        self.assertNotIn("The quality gate passed", reviews[0])

    def test_a_note_file_reaches_every_workflow(self):
        self.write("BLOCKED_NOTE.md", "Stuck? Write `BLOCKED: <question>` {{and stop}}.")
        seen = []
        state = replace(new_run(QUICK), prompt_overrides=orchestrator.load_prompts().overrides)
        wf, *_ = make_workflow({"build": [recording(build_turn(1), seen)], "review": [review_turn(1, APPROVE)]},
                               state=state, pipeline=QUICK, pull_request=False)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen, [(f"Build add a rate limiter in /proj; report to {D}/build-1.md.\n\n"
                                 f"Stuck? Write `BLOCKED: <question>` {{and stop}}.")])

    def test_a_workflow_files_own_prompt_is_not_replaced(self):
        self.write("BUILD_PROMPT.md", "Build {spec_path} in {cwd}; report to {build_path}.")
        self.write("FIX_PROMPT.md", "Fix {prev_review_path}; report to {build_path}.")
        _, pipeline = self.started("--workflow-file", f"{EXAMPLES}/quick.toml")

        build = pipeline.step("build")
        # Its own prompt, and the again it takes from the default workflow's build step through use.
        self.assertTrue(build.prompt.startswith("You are the Builder, the first of two roles"), build.prompt)
        self.assertEqual(build.again, "Fix {prev_review_path}; report to {build_path}.")

    def test_bad_files_stop_run_before_any_agent(self):
        cases = [
            ("BUILDER_PROMPT.md", "Build {spec_path}.",
             "BUILDER_PROMPT names no built-in prompt; did you mean BUILD_PROMPT.md?"),
            ("README.md", "notes", "README names no built-in prompt; the names are BLOCKED_NOTE, BLOCKED_PROMPT, "),
            ("BUILD_PROMPT.md", "Build {spec_path} in {cwd} {nope}; report to {build_path}.",
             ("placeholder {nope} is not one of the built-in text's; BUILD_PROMPT takes {build_path}, {cwd}, "
              "{spec_path}")),
            ("BUILD_PROMPT.md", "Build {spec_path} in {cwd}.",
             "placeholder {build_path} is missing; BUILD_PROMPT takes {build_path}, {cwd}, {spec_path}"),
            ("BLOCKED_NOTE.md", "Ask {}.", "placeholder {} is not one of the built-in text's; BLOCKED_NOTE takes none"),
            ("CONTINUE_PROMPT.md", "Write {path!z}.", "cannot be filled in: ValueError: Unknown conversion specifier z"),
            ("CONTINUE_PROMPT.md", "Write {path}}.",
             "Single '}' encountered in format string; write a literal brace as {{ or }}"),
            ("FIX_PROMPT.md", "Fix \xe9".encode("latin-1"), "not UTF-8 text"),
        ]
        for name, text, message in cases:
            with self.subTest(name=name, message=message), \
                    patch.object(Workflow, "__init__", return_value=None) as init, \
                    patch.object(Host, "resolve_dir", return_value="/proj"), \
                    patch.dict("os.environ", {"HERDR_ENV": "1"}), \
                    patch("sys.stderr", new_callable=io.StringIO) as err:
                path = self.write(name, text)
                self.assertEqual(main(["run", "task"]), orchestrator.EXIT_ERROR)
                self.assertTrue(err.getvalue().startswith(f"error: {path}: {message}"), err.getvalue())
                init.assert_not_called()
                os.remove(path)

    def test_an_unreadable_file_names_it(self):
        # A directory, which no user can read as a file, root included.
        path = os.path.join(self.prompts, "FIX_PROMPT.md")
        os.makedirs(path)
        with self.assertRaisesRegex(OrchestratorError, f"cannot read prompt file {re.escape(path)}: Is a directory"):
            orchestrator.load_prompts()

    def test_a_bad_file_stops_drain_before_any_run(self):
        self.write("BUILD_PROMPT.md", "Build {nope}.")
        queue = tempfile.TemporaryDirectory()
        self.addCleanup(queue.cleanup)
        os.mkdir(os.path.join(queue.name, "a"))
        with open(os.path.join(queue.name, "a", "spec.md"), "w") as f:
            f.write(drain_spec_text("First"))
        with patch.object(orchestrator, "connect", return_value=(None, FakeHost())), \
                patch.object(Host, "resolve_dir", return_value="/proj"), \
                patch.object(Workflow, "__init__", return_value=None) as init, \
                patch("sys.stdout"), patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["drain", queue.name]), orchestrator.EXIT_ERROR)
        self.assertIn(f"{self.prompts}/BUILD_PROMPT.md: placeholder {{nope}}", err.getvalue())
        init.assert_not_called()

    def test_a_resume_keeps_the_prompts_the_run_started_with(self):
        for edit in ("edited", "deleted"):
            with self.subTest(edit=edit):
                path = self.write("FIX_PROMPT.md", "Fix {prev_review_path}; report to {build_path}.")
                state, pipeline = self.started()

                def review_then_interrupt(prompt, state, host):
                    raise KeyboardInterrupt
                wf, _, host, _ = make_workflow({"spec": [spec_turn], "build": [build_turn(1)],
                                                "review": [review_then_interrupt]},
                                               state=state, pipeline=pipeline, pull_request=False)
                with self.assertRaises(KeyboardInterrupt):
                    wf.run()
                if edit == "edited":
                    self.write("FIX_PROMPT.md", "Changed {prev_review_path}; report to {build_path}.")
                else:
                    os.remove(path)

                seen = []
                saved = json.loads(host.files[f"{D}/state.json"])
                wf2, *_ = resume(RunState.from_dict(saved), {
                    "review": [review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)],
                    "build": [recording(build_turn(2), seen)]}, host=host)
                self.assertEqual(wf2.run(), APPROVE)
                self.assertEqual(len(seen), 1)
                self.assertIn(f"Fix {D}/review-1.md; report to {D}/build-2.md.", seen[0])
                self.assertNotIn("Changed", seen[0])
                if os.path.exists(path):
                    os.remove(path)

    def test_a_state_saved_before_prompt_files_resumes_with_the_built_in_texts(self):
        self.write("FIX_PROMPT.md", "Fix {prev_review_path}; report to {build_path}.")
        saved = asdict(saved_run("build", 2, agents=("spec", "build", "review")))
        del saved["prompt_overrides"]
        seen = []
        state = RunState.from_dict(saved)
        wf, *_ = resume(state, {"build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)]},
                        files={lambda s: s.review_path(1): f"**VERDICT: {CHANGES_REQUESTED}**\n1. finding",
                               lambda s: s.build_path(1): "report 1"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(state.prompt_overrides, {})
        self.assertIn(orchestrator.FIX_PROMPT.format(prev_review_path=f"{D}/review-1.md",
                                                     build_path=f"{D}/build-2.md"), seen[0])

    def test_a_state_that_replaces_an_unknown_prompt(self):
        saved = {**asdict(saved_run("build", 1)), "prompt_overrides": {"LATER_NOTE": "x"}}
        with self.assertRaisesRegex(OrchestratorError, "run state 20260929-120000-a1b2c3 replaces prompt LATER_NOTE, "
                                                       "which this orchestrator lacks"):
            orchestrator.resumable_state([(10**4, saved)], parse_args(["resume", "a1b2c3"]), "/proj", "here",
                                         lambda pid: False)

    def test_without_prompt_files_nothing_changes(self):
        self.write("notes.txt", "not a prompt file")
        state, pipeline = self.started()
        self.assertEqual((state.prompt_overrides, pipeline), ({}, orchestrator.DEFAULT_WORKFLOW))
        self.assertEqual(orchestrator.load_prompts(), BUILT_IN_PROMPTS)

    def test_an_override_ends_with_one_newline_less(self):
        self.write("BLOCKED_NOTE.md", "Ask.\n")
        self.assertEqual(orchestrator.load_prompts().BLOCKED_NOTE, "Ask.\n")

    def test_workflows_lists_the_prompt_files(self):
        self.write("QUALITY_OFF_NOTE.md", "Off: {quality_paths}.")
        self.write("BUILD_PROMPT.md", "Build {spec_path} in {cwd}; report to {build_path}.")
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["workflows"]), 0)
        self.assertTrue(out.getvalue().endswith(
            f"\nPrompt files in {self.prompts}, in place of the built-in texts:\n"
            f"    BUILD_PROMPT  {self.prompts}/BUILD_PROMPT.md\n"
            f"    QUALITY_OFF_NOTE  {self.prompts}/QUALITY_OFF_NOTE.md\n"), out.getvalue())

    def test_workflows_says_why_the_prompt_files_would_stop_a_run(self):
        self.write("BUILD_PROMPT.md", "Build.")
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["workflows"]), 0)
        self.assertTrue(out.getvalue().endswith(
            f"\nPrompt files in {self.prompts}:\n    error: {self.prompts}/BUILD_PROMPT.md: placeholder {{build_path}} "
            f"is missing; BUILD_PROMPT takes {{build_path}}, {{cwd}}, {{spec_path}}\n"), out.getvalue())

    def test_workflows_prints_the_default_as_the_prompt_files_word_it(self):
        self.write("BUILD_PROMPT.md", "Build {spec_path} in {cwd}; report to {build_path}.")
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["workflows", "default"]), 0)
        default = orchestrator.parse_workflow("default", tomllib.loads(out.getvalue()), BUILT_IN_PROMPTS)
        self.assertEqual(default.step("build").prompt, "Build {spec_path} in {cwd}; report to {build_path}.")

    def test_every_built_in_prompt_is_overridable(self):
        names = {f.name for f in orchestrator.fields(orchestrator.Prompts)}
        constants = {k for k in vars(orchestrator) if re.fullmatch(r"[A-Z_]+_(PROMPT|NOTE)", k)}
        # Not sent to an agent: the quality file's and the window's.
        self.assertEqual(constants - names, {"TO_WEIGH_NOTE", "QUALITY_TO_WEIGH_NOTE", "HERDR_NOTE"})
        for name in names:
            self.assertEqual(getattr(BUILT_IN_PROMPTS, name), getattr(orchestrator, name))
            self.assertIsNone(orchestrator.prompt_problem(name, getattr(orchestrator, name)))


# No verdict step: the run finishes when the Builder's one turn is done.
SOLO = Pipeline("solo", {"build": "Builder"}, (
    Step("build", "build", "build.md", "Build {task} in {cwd}; report to {path}.", edits=True),))


def solo_turn(prompt, state, host):
    host.write("/proj/limiter.py", "version 1")
    return writes(lambda s: f"{s.dir}/build.md", "solo report")(prompt, state, host)


class TestWorkflowWithoutAVerdict(unittest.TestCase):
    def test_a_run_finishes_and_opens_a_ready_pull_request(self):
        wf, herdr, host, notes = make_workflow({"build": [solo_turn]}, state=new_run(SOLO), pipeline=SOLO)

        self.assertEqual(wf.run(), orchestrator.FINISHED)
        pr = host.prs[0]
        self.assertFalse(pr["draft"])
        # The head line, then the commit's trailers, then the spec.
        head, trailers, rest = pr["body"].split("\n\n", 2)
        self.assertEqual(head, "Opened by ai-agents-orchestrator run `20260929-120000-a1b2c3`. **FINISHED**: its "
                               "workflow has no review step, so no agent reviewed this change.")
        self.assertEqual(trailers, "| Commit trailer | Value |\n| --- | --- |\n"
                                   "| Orchestrator-Run | `20260929-120000-a1b2c3` |\n"
                                   "| Orchestrator-Workflow | `solo` |\n| Orchestrator-Role | `build=claude/default` |")
        self.assertTrue(rest.startswith("<details open>\n<summary>Spec</summary>"))
        self.assertIn("<summary>Builder report (round 1)</summary>\n\nsolo report", pr["body"])
        self.assertNotIn("<summary>Review", pr["body"])
        commit = next(c for c in host.git_calls if c[0] == "commit")
        self.assertEqual(commit[5:], ("Orchestrator run 20260929-120000-a1b2c3: FINISHED, unreviewed.",
                                      "--trailer", "Orchestrator-Run: 20260929-120000-a1b2c3",
                                      "--trailer", "Orchestrator-Workflow: solo",
                                      "--trailer", "Orchestrator-Role: build=claude/default"))
        self.assertIn("Run finished: FINISHED", notes)
        saved = json.loads(host.files[f"{D}/state.json"])
        self.assertEqual((saved["phase"], saved["verdict"]), ("done", "FINISHED"))

    def test_a_resume_at_publish_keeps_the_outcome(self):
        state = saved_run("publish", 1, agents=("build",), pull_request=True, base_branch="main",
                          branch="orchestrator/x-a1b2c3", verdict=orchestrator.FINISHED, workflow="solo")
        wf, _, host, _ = resume(state, {}, host=FakeHost(head="def456", branch=state.branch), pipeline=SOLO)
        self.assertEqual(wf.run(), orchestrator.FINISHED)
        self.assertFalse(host.prs[0]["draft"])

    def gated_solo(self, *outcomes):
        """A run of a one-step gated workflow, with up to two quality rounds playing these outcomes."""
        solo = Pipeline("gsolo", {"impl": "Implementer"}, (
            Step("impl", "impl", "impl-{n}.md", "Implement {task}; report to {path}.", edits=True, quality_gated=True),))
        fake = FakeCI()
        fake.outcomes = list(outcomes)
        wf, _, host, _, notes = gated({"impl": [impl_turn(1), impl_turn(1, 1)]}, fake=fake, state=new_run(solo),
                                      pipeline=solo, max_quality_rounds=2)
        return wf.run(), host, notes

    def test_a_gate_that_still_fails_is_not_finished(self):
        verdict, host, notes = self.gated_solo(outcome(**RED), outcome(**RED))

        self.assertEqual(verdict, orchestrator.QUALITY_GATE_FAILED)
        pr = host.prs[0]
        self.assertTrue(pr["draft"])
        head, trailers, rest = pr["body"].split("\n\n", 2)
        self.assertEqual(head, "Opened by ai-agents-orchestrator run `20260929-120000-a1b2c3`. "
                               "**QUALITY_GATE_FAILED**: its workflow has no review step, and the quality gate still "
                               "did not pass after the last quality round, so this is a draft.")
        self.assertIn("| Orchestrator-Gate | `ERROR after 2 quality rounds` |", trailers.split("\n"))
        self.assertTrue(rest.startswith("<details open>"))
        self.assertNotIn("Reviewer", pr["body"])
        self.assertIn("<summary>Quality gate (round 1)</summary>\n\nGATE: ERROR", pr["body"])
        commit = next(c for c in host.git_calls if c[0] == "commit")
        self.assertEqual(commit[5], "Orchestrator run 20260929-120000-a1b2c3: QUALITY_GATE_FAILED, unreviewed.")
        self.assertIn("Run finished: QUALITY_GATE_FAILED", notes)

    def test_a_missing_last_quality_file_fails_the_gate(self):
        # The quality round before passed, but the current one's file is not written: as before _gate_passed read
        # _last_quality, which falls back to the round before.
        state = quality_state(q=2)
        wf, _, host, _ = resume(state, {}, ci=ci_for(FakeCI()), files={lambda s: s.quality_path(1, 1): "GATE: OK\n"})
        self.assertFalse(wf._gate_passed(1))
        host.files[state.quality_path(1, 2)] = "GATE: OK\n"
        self.assertTrue(wf._gate_passed(1))

    def test_a_gate_that_passes_is_finished(self):
        verdict, host, _ = self.gated_solo(outcome(**RED), outcome())
        self.assertEqual(verdict, orchestrator.FINISHED)
        self.assertFalse(host.prs[0]["draft"])
        self.assertIn("<summary>Quality gate (round 1)</summary>\n\nGATE: OK", host.prs[0]["body"])

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=orchestrator.QUALITY_GATE_FAILED)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_a_failed_gate_exits_as_changes_requested(self, _resolve, _run, init):
        with patch("builtins.print"):
            self.assertEqual(main(["run", "task"]), orchestrator.EXIT_CHANGES_REQUESTED)

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=orchestrator.FINISHED)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_finished_exits_zero(self, _resolve, _run, init):
        config = without_workflow_files(self)
        path = os.path.join(config, "solo.toml")
        with open(path, "w") as f:
            f.write(orchestrator.workflow_toml(SOLO))
        with patch("builtins.print") as out:
            self.assertEqual(main(["run", "task", "--workflow-file", path]), 0)
        state = init.call_args.args[2]
        out.assert_called_with(f"FINISHED: {state.dir}/build.md")



KINDS = ("claude", "codex", "gemini", "opencode", "pi")
MODES = ("default", "acceptEdits", "bypassPermissions", "plan", "auto", "dontAsk")
# The spec's table of what --permission-mode becomes in each harness; None is dropped.
PERMISSIONS = {
    "claude": {mode: ("--permission-mode", mode) for mode in MODES},
    "gemini": {"default": ("--approval-mode", "default"), "acceptEdits": ("--approval-mode", "auto_edit"),
               "bypassPermissions": ("--approval-mode", "yolo"), "plan": None, "auto": None, "dontAsk": None},
    "codex": {"default": (), "acceptEdits": ("--full-auto",),
              "bypassPermissions": ("--dangerously-bypass-approvals-and-sandbox",),
              "plan": ("--sandbox", "read-only"), "auto": None, "dontAsk": None},
    "opencode": dict.fromkeys(MODES),
    "pi": dict.fromkeys(MODES),
}
FULL = ("spec", "build", "review")


def every_role(kind, roles=FULL):
    return dict.fromkeys(roles, kind)


class TestHarnesses(unittest.TestCase):
    def run_default(self, **kw):
        """A whole run of the default workflow; its starts as {role: (kind, args)} and what it logged."""
        wf, herdr, *_ = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, **kw)
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(wf.run(), APPROVE)
        args = {c[1].split("-")[0]: c[3] for c in herdr.calls if c[0] == "start"}
        return {name.split("-")[0]: (kind, args[name.split("-")[0]]) for name, kind in herdr.kinds}, err.getvalue()

    def test_a_new_agent_in_each_harness(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                starts, _ = self.run_default(agent_kinds=every_role(kind), models={"build": "m"})
                self.assertEqual(starts, {"spec": (kind, ()), "build": (kind, ("--model", "m")),
                                          "review": (kind, ())})

    def test_each_permission_mode_in_each_harness(self):
        for kind in KINDS:
            for mode in MODES:
                with self.subTest(kind=kind, mode=mode):
                    starts, logged = self.run_default(agent_kinds=every_role(kind), permission_mode=mode,
                                                      models={"review": "m"})
                    expected = PERMISSIONS[kind][mode] or ()
                    self.assertEqual(starts, {"spec": (kind, expected), "build": (kind, expected),
                                              "review": (kind, (*expected, "--model", "m"))})
                    dropped = [line.strip() for line in logged.splitlines() if "no equivalent" in line]
                    if PERMISSIONS[kind][mode] is None:
                        self.assertEqual(dropped, [f"[{role}] {kind} has no equivalent of --permission-mode {mode}; "
                                                   f"starting it without one" for role in FULL])
                    else:
                        self.assertEqual(dropped, [])

    def test_without_a_harness_every_role_is_claude_as_before(self):
        starts, logged = self.run_default(permission_mode="auto")
        self.assertEqual(starts, every_role(("claude", ("--permission-mode", "auto"))))
        self.assertNotIn("no equivalent", logged)

    def test_a_dropped_mode_is_logged_once_per_role(self):
        wf, herdr, *_ = resume(saved_run("build", 2, agents=FULL), {
            "build": [build_turn(2)], "review": [review_turn(2, APPROVE)]},
            alive=["review"], sessions={"build": "s-gone"}, agent_kinds=every_role("pi"), permission_mode="plan")
        wf.state.agents["build"]["kind"] = "pi"
        herdr.lost_sessions.add("s-gone")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(wf.run(), APPROVE)
        self.assertEqual([c[3] for c in herdr.calls if c[0] == "start"], [("--session", "s-gone"), ()])
        self.assertEqual(err.getvalue().count("no equivalent"), 1)

    def test_an_exited_agent_resumes_in_its_own_harness(self):
        resumed = {
            "claude": ("--permission-mode", "acceptEdits", "--resume", "s-build", "--model", "m"),
            "gemini": ("--approval-mode", "auto_edit", "--resume", "s-build", "--model", "m"),
            "codex": ("resume", "s-build", "--full-auto", "--model", "m"),
            "pi": ("--session", "s-build", "--model", "m"),
            "opencode": ("--session", "s-build", "--model", "m"),
        }
        for kind in KINDS:
            with self.subTest(kind=kind):
                seen = []
                wf, herdr, *_ = resume(saved_run("build", 2, agents=FULL, prompted="build-2.md"), {
                    "build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)]},
                    alive=["review"], sessions={"build": "s-build"}, agent_kinds=every_role(kind),
                    permission_mode="acceptEdits", models={"build": "m"})
                wf.state.agents["build"]["kind"] = kind
                with patch("sys.stderr"):
                    self.assertEqual(wf.run(), APPROVE)
                self.assertEqual([c for c in herdr.calls if c[0] == "start"],
                                 [("start", "build-a1b2c3", "w1:p2", resumed[kind])])
                self.assertEqual(herdr.kinds, [("build-a1b2c3", kind)])
                self.assertEqual(seen, [orchestrator.CONTINUE_PROMPT.format(path=wf.state.build_path(2))])
                self.assertEqual(wf.state.agents["build"]["kind"], kind)

    def test_a_changed_harness_starts_fresh(self):
        # The saved record without a kind is a claude agent's, as 0.3.0 saved it.
        for saved_kind, kind in ((None, "codex"), ("claude", "pi"), ("gemini", "claude")):
            with self.subTest(saved=saved_kind, now=kind):
                seen = []
                wf, herdr, *_ = resume(saved_run("build", 2, agents=FULL, prompted="build-2.md"), {
                    "build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)]},
                    alive=["review"], sessions={"build": "s-build"}, agent_kinds={"build": kind})
                if saved_kind:
                    wf.state.agents["build"]["kind"] = saved_kind
                with patch("sys.stderr") as err:
                    self.assertEqual(wf.run(), APPROVE)
                self.assertEqual([c for c in herdr.calls if c[0] == "start"],
                                 [("start", "build-a1b2c3", "w1:p2", ())])
                self.assertEqual(herdr.kinds, [("build-a1b2c3", kind)])
                self.assertIn("You are the Builder", seen[0])
                self.assertIn("This is round 2, and you are a fresh session", seen[0])
                logged = "".join(c.args[0] for c in err.write.call_args_list)
                self.assertIn(f"[build] session s-build is {saved_kind or 'claude'}'s, and the role runs in {kind} "
                              f"now; starting a fresh one", logged)

    def test_a_live_agent_keeps_its_harness(self):
        wf, herdr, *_ = resume(saved_run("review", 1, agents=FULL), {"review": [review_turn(1, APPROVE)]},
                               alive=["review"], files={lambda s: s.build_path(1): "report 1"},
                               agent_kinds=every_role("codex"))
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(herdr.kinds, [])
        self.assertIn(("prompt", "review-a1b2c3"), herdr.calls)

    def test_every_roles_harness_is_saved(self):
        wf, *_ = make_workflow({"spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]},
                               agent_kinds={"review": "pi"}, permission_mode="plan")
        wf.run()
        saved = json.loads(wf.host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual(saved["agent_kinds"], {"spec": "claude", "build": "claude", "review": "pi"})
        self.assertEqual(saved["permission_mode"], "plan")
        self.assertNotIn("agent_args", saved)
        self.assertEqual({role: a["kind"] for role, a in saved["agents"].items()},
                         {"spec": "claude", "build": "claude", "review": "pi"})


def v030_state(**kw):
    """state.json as 0.3.0 wrote it: Claude Code's agent_args, no harnesses, agent records without a kind."""
    saved = asdict(saved_run("build", 2, agents=FULL, prompted="build-2.md"))
    for key in ("permission_mode", "agent_kinds"):
        del saved[key]
    saved["agents"]["build"]["session"] = "s-build"
    return {**saved, "agent_args": ["--permission-mode", "acceptEdits"], **kw}


class TestHarnessFlags(unittest.TestCase):
    def setUp(self):
        workflows = os.path.join(without_workflow_files(self), "ai-agents-orchestrator", "workflows")
        os.makedirs(workflows)
        with open(f"{workflows}/coded.toml", "w") as f:
            f.write('roles.review = { agent = "codex" }\n[[steps]]\nuse = "spec"\n[[steps]]\nuse = "build"\n'
                    '[[steps]]\nuse = "review"\n')

    def kinds(self, *flags):
        """The harness of each role of a new run with these flags, once its Workflow fills in the default."""
        state, pipeline = orchestrator.new_state(parse_args(["run", "task", *flags]), "/proj")
        wf, *_ = make_workflow({}, state=state, pipeline=pipeline, agent_kinds=state.agent_kinds)
        return wf.state.agent_kinds

    def test_precedence(self):
        cases = [
            ([], every_role("claude")),
            (["--agent", "codex"], every_role("codex")),
            (["--agent", "gemini", "--role-agent", "review=pi"], {"spec": "gemini", "build": "gemini", "review": "pi"}),
            (["--role-agent", "build=opencode"], {"spec": "claude", "build": "opencode", "review": "claude"}),
            (["--workflow", "coded"], {"spec": "claude", "build": "claude", "review": "codex"}),
            (["--workflow", "coded", "--agent", "gemini"], every_role("gemini")),
            (["--workflow", "coded", "--role-agent", "review=pi"],
             {"spec": "claude", "build": "claude", "review": "pi"}),
            (["--workflow", "coded", "--agent", "pi", "--role-agent", "spec=claude"],
             {"spec": "claude", "build": "pi", "review": "pi"}),
        ]
        for flags, kinds in cases:
            with self.subTest(flags=flags):
                self.assertEqual(self.kinds(*flags), kinds)

    def test_role_agent_starts_that_role_in_its_harness(self):
        state, pipeline = orchestrator.new_state(
            parse_args(["run", "task", "--agent", "gemini", "--role-agent", "review=pi"]), "/proj")
        wf, herdr, *_ = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        }, state=state, pipeline=pipeline, agent_kinds=state.agent_kinds)
        wf.run()
        self.assertEqual([(name.split("-")[0], kind) for name, kind in herdr.kinds],
                         [("spec", "gemini"), ("build", "gemini"), ("review", "pi")])

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_flags_reach_the_workflow(self, _resolve, _run, init):
        with patch("builtins.print"):
            self.assertEqual(main(["run", "task", "--agent", "codex", "--role-agent", "spec=claude"]), 0)
        self.assertEqual(init.call_args.kwargs["agents"].kinds, {"spec": "claude", "build": "codex", "review": "codex"})

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_role_agent_for_a_role_the_workflow_lacks(self, _resolve, init):
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["run", "task", "--role-agent", "tests=pi"]), orchestrator.EXIT_ERROR)
        self.assertIn("--role-agent names role tests, which the workflow does not have; its roles are spec, "
                      "build, review", err.getvalue())
        init.assert_not_called()

    def test_unknown_kinds_are_argparse_errors(self):
        for argv in (["run", "task", "--agent", "cursor"], ["resume", "a1b2c3", "--agent", "cursor"],
                     ["run", "task", "--role-agent", "review=cursor"], ["resume", "a1b2c3", "--role-agent", "review"],
                     ["run", "task", "--role-agent", "=pi"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                    patch("sys.stderr", new_callable=io.StringIO) as err:
                parse_args(argv)
            self.assertRegex(err.getvalue(), r"claude'?, '?codex'?, '?gemini'?, '?opencode'?,? (and )?'?pi")

    def resumable(self, argv, saved):
        return orchestrator.resumable_state([(10**4, saved)], parse_args(["resume", "a1b2c3", *argv]), "/proj",
                                            "here", lambda pid: False)

    def test_resume_keeps_or_overrides_the_saved_harnesses(self):
        saved = {**asdict(saved_run("build", 2, agents=FULL)),
                 "agent_kinds": {"spec": "claude", "build": "codex", "review": "gemini"}}
        cases = [
            ([], {"spec": "claude", "build": "codex", "review": "gemini"}),
            (["--agent", "pi"], every_role("pi")),
            (["--role-agent", "review=opencode"], {"spec": "claude", "build": "codex", "review": "opencode"}),
        ]
        for flags, kinds in cases:
            with self.subTest(flags=flags):
                self.assertEqual(self.resumable(flags, saved).agent_kinds, kinds)

    def test_resume_with_a_new_harness_relaunches_in_it(self):
        state = self.resumable(["--agent", "pi"], {**v030_state(), "phase": "review"})
        wf, herdr, *_ = resume(state, {"review": [review_turn(2, APPROVE)]},
                               files={lambda s: s.build_path(2): "report 2"},
                               permission_mode=state.permission_mode, agent_kinds=state.agent_kinds)
        with patch("sys.stderr"):
            self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(herdr.kinds, [("review-a1b2c3", "pi")])

    def test_a_030_state_resumes_on_claude(self):
        state = self.resumable([], v030_state())
        self.assertEqual((state.permission_mode, state.agent_kinds), ("acceptEdits", {}))
        seen = []
        wf, herdr, host, _ = resume(state, {
            "build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)]},
            permission_mode=state.permission_mode, agent_kinds=state.agent_kinds, models=state.models)
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual([c[3] for c in herdr.calls if c[0] == "start"], [
            ("--permission-mode", "acceptEdits", "--resume", "s-build"), ("--permission-mode", "acceptEdits")])
        self.assertEqual(herdr.kinds, [("build-a1b2c3", "claude"), ("review-a1b2c3", "claude")])
        self.assertEqual(seen, [orchestrator.CONTINUE_PROMPT.format(path=wf.state.build_path(2))])
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual((saved["permission_mode"], saved["agent_kinds"]), ("acceptEdits", every_role("claude")))

    def test_a_030_state_without_a_permission_mode(self):
        self.assertIsNone(RunState.from_dict(v030_state(agent_args=[])).permission_mode)

    def test_unreadable_agent_args(self):
        saved = v030_state(agent_args=["--model", "x"])
        with self.assertRaisesRegex(OrchestratorError, r"has agent_args \['--model', 'x'\], which name no permission"):
            RunState.from_dict(saved)


    def test_launch_args_leave_the_permission_table_alone(self):
        # Without a session, codex's and gemini's arguments start as their table's list; extending that list in place
        # gave every later agent of that kind the earlier one's --model as well.
        table = copy.deepcopy(orchestrator.PERMISSION_ARGS)
        for kind in KINDS:
            with self.subTest(kind=kind):
                first = orchestrator.launch_args(kind, "acceptEdits", "m", None, "/run")
                self.assertEqual(orchestrator.launch_args(kind, "acceptEdits", "m", None, "/run"), first)
        self.assertEqual(orchestrator.PERMISSION_ARGS, table)

    def test_run_dir_granted_in_worktree_runs_only(self):
        d = "/proj/.orchestrator/runs/r"
        grants = {"claude": ["--add-dir", d], "codex": ["--add-dir", d], "gemini": ["--include-directories", d],
                  "opencode": [], "pi": []}
        for kind, grant in grants.items():
            with self.subTest(kind=kind):
                for args in ((kind, None, None, None), (kind, "acceptEdits", "m", "s1")):
                    self.assertEqual(orchestrator.launch_args(*args, d), orchestrator.launch_args(*args) + grant)
                self.assertEqual(orchestrator.launch_args(kind, None, None, None), [])

        wf, herdr, *_ = worktree_run()
        wf.run()
        self.assertEqual([c[3] for c in herdr.calls if c[0] == "start"], [("--add-dir", wf.state.dir)] * 3)
        wf, herdr, *_ = make_workflow({"spec": [spec_turn], "build": [build_turn(1)],
                                       "review": [review_turn(1, APPROVE)]})
        wf.run()
        self.assertEqual([c[3] for c in herdr.calls if c[0] == "start"], [()] * 3)


class TestWorkflowFileAgents(unittest.TestCase):
    def test_parsed_and_round_tripped(self):
        p = orchestrator.parse_workflow("mine", tomllib.loads(
            'roles.review = { agent = "codex", model = "o3" }\nroles.build = { label = "Implementer", agent = "pi" }\n'
            '[[steps]]\nuse = "spec"\n[[steps]]\nuse = "build"\n[[steps]]\nuse = "review"\n'), BUILT_IN_PROMPTS)
        self.assertEqual(p.agents, {"review": "codex", "build": "pi"})
        definition = orchestrator.workflow_definition(p)
        self.assertEqual(definition["roles"], {"spec": {"label": "Spec Collector"},
                                               "build": {"label": "Implementer", "agent": "pi"},
                                               "review": {"label": "Reviewer", "model": "o3", "agent": "codex"}})
        text = orchestrator.workflow_toml(p)
        self.assertIn('[roles.review]\nlabel = "Reviewer"\nmodel = "o3"\nagent = "codex"\n', text)
        self.assertEqual(orchestrator.parse_workflow("mine", tomllib.loads(text), BUILT_IN_PROMPTS), p)
        self.assertEqual(orchestrator.parse_workflow("mine", definition, BUILT_IN_PROMPTS), p)

    def test_workflows_prints_it(self):
        config = without_workflow_files(self)
        workflows = os.path.join(config, "ai-agents-orchestrator", "workflows")
        os.makedirs(workflows)
        with open(f"{workflows}/coded.toml", "w") as f:
            f.write('roles.review = { agent = "codex" }\n[[steps]]\nuse = "spec"\n[[steps]]\nuse = "build"\n'
                    '[[steps]]\nuse = "review"\n')
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["workflows", "coded"]), 0)
        self.assertIn('[roles.review]\nlabel = "Reviewer"\nagent = "codex"\n', out.getvalue())

    def test_saved_with_the_run(self):
        p = Pipeline("coded", dict(QUICK.roles), QUICK.steps, agents={"review": "codex"})
        wf, *_ = make_workflow({"build": [build_turn(1)], "review": [review_turn(1, APPROVE)]},
                               state=new_run(p), pipeline=p, pull_request=False)
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.workflow_definition["roles"]["review"], {"label": "Reviewer", "agent": "codex"})
        self.assertEqual(orchestrator.run_pipeline("coded", wf.state.workflow_definition, BUILT_IN_PROMPTS), p)

    def test_invalid_agents_name_the_role(self):
        steps = '[[steps]]\nuse = "spec"\n[[steps]]\nuse = "build"\n[[steps]]\nuse = "review"\n'
        self.assertEqual(orchestrator.parse_workflow("mine", tomllib.loads(steps), BUILT_IN_PROMPTS).steps,
                         orchestrator.DEFAULT_WORKFLOW.steps)
        cases = [
            ('roles.review = { agent = "cursor" }\n',
             "role review: agent cursor is not a harness this orchestrator supports; "
             "it takes claude, codex, gemini, opencode, pi"),
            ('roles.review = { agent = 3 }\n', "role review: agent must be a string"),
            ('roles.review = { agnet = "pi" }\n', "role review: unknown key agnet; did you mean agent?"),
        ]
        for text, message in cases:
            data = tomllib.loads(text + steps)
            with self.subTest(text=text), self.assertRaises(ValueError) as cm:
                orchestrator.parse_workflow("mine", data, BUILT_IN_PROMPTS)
            self.assertIn(message, str(cm.exception))
        roles = dict(QUICK.roles)
        with self.assertRaisesRegex(ValueError, "an agent is set for role x, which is not one of the workflow's roles"):
            Pipeline("bad", roles, QUICK.steps, agents={"x": "pi"})



# ---------------------------------------------------------------------------
# GUI: on fakes of the tkinter modules, so the suite needs neither tkinter nor a display
# ---------------------------------------------------------------------------

EVERY_OPTION = orchestrator.RunForm(
    task="add a rate limiter", cwd="~/proj", machine="remote", workflow="default", agent="codex",
    model="gpt-5", permission_mode="acceptEdits", no_pr=True, quality_gate="folder/job")


class TestRunArgs(unittest.TestCase):
    def test_every_option(self):
        self.assertEqual(orchestrator.run_args(EVERY_OPTION), [
            "run", "--machine", "remote", "--cwd", "~/proj", "--workflow", "default", "--agent", "codex",
            "--model", "gpt-5", "--permission-mode", "acceptEdits", "--quality-gate", "folder/job", "--no-pr",
            "--", "add a rate limiter"])

    def test_the_cli_reads_every_option_as_the_form_has_it(self):
        args = parse_args(orchestrator.run_args(EVERY_OPTION))
        self.assertEqual((args.command, args.task, args.machine, args.cwd, args.workflow, args.agent, args.model,
                          args.permission_mode, args.no_pr, args.quality_gate),
                         ("run", "add a rate limiter", "remote", "~/proj", "default", "codex", "gpt-5",
                          "acceptEdits", True, "folder/job"))

    def test_unset_options_are_left_out(self):
        form = orchestrator.RunForm(task="  fix it\n", cwd=" /proj ", machine=" ", model="", agent="")
        self.assertEqual(orchestrator.run_args(form), ["run", "--cwd", "/proj", "--", "fix it"])

    def test_a_task_that_looks_like_a_flag_stays_the_task(self):
        for task in ("-v is broken", "--help", "line one\nline two -- three"):
            with self.subTest(task=task):
                args = parse_args(orchestrator.run_args(orchestrator.RunForm(task=task, cwd="/proj")))
                self.assertEqual((args.task, args.cwd), (task, "/proj"))

    def test_task_and_folder_are_required(self):
        no_task, no_folder = orchestrator.RunForm(task=" \n", cwd="/proj"), orchestrator.RunForm(task="fix it", cwd=" ")
        with self.assertRaisesRegex(OrchestratorError, "the task is empty"):
            orchestrator.run_args(no_task)
        with self.assertRaisesRegex(OrchestratorError, "choose a project folder"):
            orchestrator.run_args(no_folder)

    def test_resume_takes_the_target_arguments(self):
        self.assertEqual(orchestrator.resume_args("20260930-070000-c0ffee", EVERY_OPTION),
                         ["resume", "20260930-070000-c0ffee", "--machine", "remote", "--cwd", "~/proj"])
        self.assertEqual(orchestrator.resume_args("c0ffee", orchestrator.RunForm(cwd="/proj ")),
                         ["resume", "c0ffee", "--cwd", "/proj"])


class TestGuiByDefault(unittest.TestCase):
    def test_display(self):
        cases = [("darwin", {}, True), ("linux", {"DISPLAY": ":0"}, True), ("linux", {"WAYLAND_DISPLAY": "w"}, True),
                 ("linux", {}, False), ("linux", {"DISPLAY": "", "WAYLAND_DISPLAY": ""}, False)]
        for platform, environ, expected in cases:
            with self.subTest(platform=platform, environ=environ):
                self.assertEqual(orchestrator.gui_by_default(platform, environ), expected)

    @patch.object(orchestrator.sys, "platform", "linux")
    def test_no_arguments_open_the_gui_with_a_display(self):
        with patch.dict("os.environ", {"DISPLAY": ":0"}):
            self.assertEqual(parse_args([]).command, "gui")

    @patch.object(orchestrator.sys, "platform", "linux")
    @patch.dict("os.environ", {"DISPLAY": "", "WAYLAND_DISPLAY": ""})
    def test_no_arguments_without_a_display_print_the_usage(self):
        with patch("sys.stderr", new_callable=io.StringIO) as err, self.assertRaises(SystemExit) as cm:
            parse_args([])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("usage: orchestrator.py", err.getvalue())

    @patch.object(orchestrator.sys, "platform", "darwin")
    def test_arguments_never_open_the_gui(self):
        self.assertEqual(parse_args(["workflows"]).command, "workflows")
        with patch("sys.stdout", new_callable=io.StringIO) as out, self.assertRaises(SystemExit) as cm:
            parse_args(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("gui", out.getvalue())

    @patch.object(orchestrator, "gui_command", return_value=0)
    def test_main_opens_the_gui_without_herdr(self, gui):
        with patch.dict("os.environ", {"HERDR_ENV": ""}):
            self.assertEqual(main(["gui", "--smoke-test"]), 0)
        gui.assert_called_once_with(True)


class TestSelfCommand(unittest.TestCase):
    def test_script(self):
        self.assertEqual(orchestrator.self_command(),
                         [orchestrator.sys.executable, os.path.abspath(orchestrator.__file__)])

    def test_zipapp(self):
        with tempfile.TemporaryDirectory() as d:
            pyz = os.path.join(d, "orchestrator.pyz")
            with open(pyz, "wb") as f:
                f.write(b"PK")
            with patch.object(orchestrator, "__file__", os.path.join(pyz, "orchestrator.py")):
                self.assertEqual(orchestrator.self_command(), [orchestrator.sys.executable, pyz])

    def test_pyinstaller_binary(self):
        with patch.object(orchestrator.sys, "frozen", True, create=True), \
                patch.object(orchestrator.sys, "executable", "/opt/orchestrator-linux-x86_64"):
            self.assertEqual(orchestrator.self_command(), ["/opt/orchestrator-linux-x86_64"])


class TestRunProcess(unittest.TestCase):
    """On real child processes."""

    def finish(self, process, until=None):
        out, deadline = "", time.monotonic() + 20
        while time.monotonic() < deadline:
            text, status = process.read()
            out += text
            if status is not None or (until and until in out):
                return out, status
            time.sleep(0.02)
        self.fail(f"the child did not finish; output so far: {out!r}")

    def test_stdout_and_stderr_then_the_status(self):
        p = orchestrator.RunProcess([orchestrator.sys.executable, "-c",
                                     "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
        out, status = self.finish(p)
        self.assertEqual((sorted(out.split()), status), (["err", "out"], 3))

    def interrupted(self):
        """The output and status of a child that waits for Ctrl-C, once interrupted."""
        child = ("import time\ntry:\n    print('ready')\n    time.sleep(60)\n"
                 "except KeyboardInterrupt:\n    print('interrupted')\n    raise SystemExit(130)\n")
        p = orchestrator.RunProcess([orchestrator.sys.executable, "-c", child])
        out, _ = self.finish(p, until="ready")
        p.interrupt()
        rest, status = self.finish(p)
        return out + rest, status

    def test_interrupt_is_ctrl_c(self):
        self.assertEqual(self.interrupted(), ("ready\ninterrupted\n", 130))

    def test_interrupt_where_sigint_is_ignored(self):
        # As in a background job of a non-interactive shell, such as a Jenkins sh step.
        before = orchestrator.signal.signal(orchestrator.signal.SIGINT, orchestrator.signal.SIG_IGN)
        try:
            self.assertEqual(self.interrupted(), ("ready\ninterrupted\n", 130))
        finally:
            orchestrator.signal.signal(orchestrator.signal.SIGINT, before)

    def test_started_in_its_own_session_unbuffered(self):
        popen = MagicMock()
        popen.return_value.stdout = io.StringIO("a\n")
        orchestrator.RunProcess(["orchestrator.py", "list"], popen=popen)
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual((kwargs["stderr"], kwargs["env"]["PYTHONUNBUFFERED"]), (subprocess.STDOUT, "1"))

    def test_interrupt_after_exit_does_nothing(self):
        popen = MagicMock()
        popen.return_value.stdout = io.StringIO("")
        p = orchestrator.RunProcess(["x"], popen=popen)
        killpg = MagicMock(side_effect=ProcessLookupError)
        popen.return_value.poll.return_value = None
        p.interrupt(killpg)  # exited between the poll and the signal
        killpg.assert_called_once_with(popen.return_value.pid, orchestrator.signal.SIGINT)
        popen.return_value.poll.return_value = 0
        p.interrupt(killpg)
        self.assertEqual(killpg.call_count, 1)


class FakeVar:
    def __init__(self, master=None, value=""):
        self.value, self.traces = value, []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for fn in self.traces:
            fn("PY_VAR0", "", "write")

    def trace_add(self, mode, fn):
        self.traces.append(fn)


class FakeBooleanVar(FakeVar):
    def __init__(self, master=None, value=False):
        super().__init__(master, value)


class FakeWidget:
    """A tkinter widget that records its options, children, tags and bindings, and accepts any layout call."""

    def __init__(self, master=None, **options):
        self.options = options
        self.bindings, self.children, self.tags = {}, [], {}
        if isinstance(master, FakeWidget):
            master.children.append(self)

    def configure(self, **options):
        self.options.update(options)

    def tag_configure(self, tag, **options):
        self.tags[tag] = options

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()

    def bind(self, event, handler):
        self.bindings[event] = handler

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def press(self):
        self.options["command"]()


class FakeText(FakeWidget):
    """Like Tk's Text, it ignores inserts while disabled. tagged holds each insert's text and tags."""

    text = ""

    def __init__(self, master=None, **options):
        super().__init__(master, **options)
        self.tagged = []

    def insert(self, index, text, *tags):
        if self.options.get("state") != "disabled":
            self.text += text
            self.tagged.append((text, tags))

    def get(self, start, end):
        return self.text


class FakeTreeview(FakeWidget):
    def __init__(self, master=None, **options):
        super().__init__(master, **options)
        self.rows, self.row_tags, self.selected, self.widths = {}, {}, (), {}

    def column(self, name, width=None, **options):
        self.widths[name] = width

    def insert(self, parent, index, iid, values, tags=()):
        self.rows[iid] = values
        self.row_tags[iid] = tags

    def delete(self, *iids):
        for iid in iids:
            del self.rows[iid]

    def get_children(self):
        return tuple(self.rows)

    def selection(self):
        return self.selected


class FakeRoot(FakeWidget):
    def __init__(self):
        super().__init__()
        self.pending, self.protocols, self.destroyed = [], {}, False
        self.icon, self.option_db = None, {}

    def after(self, ms, fn):
        self.pending.append(fn)

    def protocol(self, name, fn):
        self.protocols[name] = fn

    def destroy(self):
        self.destroyed = True

    def iconphoto(self, default, image):
        self.icon = (default, image)

    def option_add(self, pattern, value):
        self.option_db[pattern] = value

    def tick(self):
        """Run the callbacks due so far, as one pass of Tk's event loop would."""
        pending, self.pending = self.pending, []
        for fn in pending:
            fn()

    def mainloop(self):
        while self.pending and not self.destroyed:
            self.tick()


class FakeStyle:
    """ttk.Style, recording the theme and each style's options and state maps."""

    def __init__(self, master=None):
        self.theme, self.options, self.maps = None, {}, {}

    def theme_use(self, name):
        self.theme = name

    def configure(self, style, **options):
        self.options.setdefault(style, {}).update(options)

    def map(self, style, **options):
        self.maps.setdefault(style, {}).update(options)


class FakeFont:
    """A Tk named font, as tkinter.font.nametofont gives it."""

    def __init__(self, name, size=-12, family="DejaVu Sans"):
        self.name, self.options = name, {"family": family, "size": size, "weight": "normal"}

    def copy(self):
        return FakeFont(self.name, self.options["size"], self.options["family"])

    def configure(self, **options):
        self.options.update(options)

    def cget(self, option):
        return self.options[option]

    def metrics(self, option):
        return round(abs(self.options["size"]) * 1.25)  # the linespace: 15 at the default 12 pixels


class FakePhotoImage:
    def __init__(self, master=None, **options):
        self.options = options


# The ttk widget classes the window may create; a class of its own each, so a test can tell them apart.
TTK_WIDGETS = {name: type(name, (FakeWidget,), {}) for name in
               ("Frame", "Label", "Entry", "Combobox", "Button", "Checkbutton", "Scrollbar")}


def fake_ui(families=("DejaVu Sans",)):
    """The tkinter modules, faked, with the font families installed; styles holds each ttk.Style the window
    creates, and named_fonts Tk's named fonts, one object per name as in Tk."""
    styles, named_fonts = [], {}

    def style(master=None):
        styles.append(FakeStyle(master))
        return styles[-1]

    return SimpleNamespace(
        tk=SimpleNamespace(StringVar=FakeVar, BooleanVar=FakeBooleanVar, Text=FakeText, PhotoImage=FakePhotoImage),
        ttk=SimpleNamespace(**TTK_WIDGETS, Treeview=FakeTreeview, Style=style),
        font=SimpleNamespace(nametofont=lambda name, root=None: named_fonts.setdefault(name, FakeFont(name)),
                             families=lambda root=None, displayof=None: families),
        filedialog=MagicMock(), messagebox=MagicMock(), styles=styles, named_fonts=named_fonts)


class FakeProcess:
    """A started command; the test scripts its output and when it exits."""

    def __init__(self, argv):
        self.argv, self.out, self.status, self.interrupts = argv, "", None, 0

    def read(self):
        out, self.out = self.out, ""
        return out, self.status

    def interrupt(self):
        self.interrupts += 1


class TestRunWindow(unittest.TestCase):
    def setUp(self):
        self.root, self.ui, self.started, self.listed = FakeRoot(), fake_ui(), [], []
        self.runs = [(10**6, run_record(phase="done", verdict=APPROVE))]
        self.window = orchestrator.RunWindow(self.root, self.ui, start=self.start, list_runs=self.list_runs,
                                             background=lambda fn: fn())

    def start(self, argv):
        self.started.append(FakeProcess(argv))
        return self.started[-1]

    def list_runs(self, machine, cwd):
        self.listed.append((machine, cwd))
        if isinstance(self.runs, Exception):
            raise self.runs
        return self.runs

    def fill(self, **values):
        self.window.task.text = values.pop("task", "add a rate limiter")
        for name, value in values.items():
            self.window.vars[name].set(value)

    def log(self):
        return self.window.log.text

    def buttons(self):
        w = self.window
        return [b.options["state"] for b in (w.start_button, w.resume_button, w.stop_button)]

    def test_the_form_starts_the_cli_command(self):
        self.fill(**{k: v for k, v in asdict(EVERY_OPTION).items() if k != "task"})
        self.window.start_button.press()
        argv = orchestrator.run_args(EVERY_OPTION)
        self.assertEqual(self.started[0].argv, [*orchestrator.self_command(), *argv])
        self.assertEqual(self.log(), f"$ {shlex.join(['orchestrator.py', *argv])}\n")
        self.assertEqual(self.buttons(), ["disabled", "disabled", "normal"])

    def test_output_streams_into_the_log_until_the_command_exits(self):
        self.fill(cwd="/proj")
        self.window.start_run()
        self.started[0].out = "  spec: waiting\n"
        self.root.tick()
        self.assertTrue(self.log().endswith("  spec: waiting\n"))
        self.started[0].status = 0
        self.root.tick()
        self.assertTrue(self.log().endswith("  spec: waiting\n[exited with status 0]\n"))
        self.assertIsNone(self.window.process)
        self.assertEqual(self.buttons(), ["normal", "normal", "disabled"])
        self.assertEqual(self.listed, [("", "/proj")])  # the run list catches up with the run
        self.assertEqual(len(self.root.pending), 1)  # the poll goes on

    def test_one_run_at_a_time(self):
        self.fill(cwd="/proj")
        self.window.start_run()
        self.window.start_run()
        self.assertEqual(len(self.started), 1)

    def test_an_incomplete_form_starts_nothing(self):
        self.fill(task=" ", cwd="/proj")
        self.window.start_run()
        self.assertEqual(self.started, [])
        self.ui.messagebox.showerror.assert_called_once_with("Cannot start the run", "the task is empty",
                                                             parent=self.root)

    def test_a_command_that_cannot_start(self):
        self.window.task.text, self.window.vars["cwd"].value = "fix it", "/proj"
        self.window._start = MagicMock(side_effect=PermissionError("denied"))
        self.window.start_run()
        self.assertTrue(self.log().endswith("could not start it: denied\n"))
        self.assertEqual(self.buttons(), ["normal", "normal", "disabled"])

    def test_stop_interrupts(self):
        self.window.stop()  # nothing to stop
        self.fill(cwd="/proj")
        self.window.start_run()
        self.window.stop_button.press()
        self.assertEqual(self.started[0].interrupts, 1)

    def test_the_run_list_is_what_list_shows(self):
        self.runs = [(STALE_SECONDS + 1, run_record(phase="spec")),
                     (40, {**run_record(owner=ME), "run_id": "20260930-080000-beef00"})]
        self.fill(cwd=" ~/proj ", machine="remote")
        self.window.refresh()
        self.assertEqual(self.window.vars["runs_note"].get(), "Listing the runs…")
        self.root.tick()
        self.assertEqual(self.listed, [("remote", "~/proj")])
        expected = orchestrator.run_rows(self.runs, socket.gethostname(), orchestrator.pid_alive,
                                         ["--machine", "remote", "--cwd", "~/proj"])
        self.assertEqual(list(self.window.runs.rows.values()), expected)
        self.assertEqual(self.window.vars["runs_note"].get(), "")

    def test_no_runs_and_a_failed_listing(self):
        self.runs = []
        self.fill(cwd="/proj")
        self.window.refresh()
        self.root.tick()
        self.assertEqual(self.window.vars["runs_note"].get(), "No runs.")
        self.runs = OrchestratorError("cd: /proj: No such file or directory")
        self.window.refresh()
        self.root.tick()
        self.assertEqual(self.window.vars["runs_note"].get(), "error: cd: /proj: No such file or directory")
        self.window.runs.selected = ("20260930-070000-c0ffee",)
        self.window.resume_run()
        self.assertEqual(self.started, [])  # the list no longer stands for any project

    def test_refresh_needs_a_folder(self):
        self.window.refresh()
        self.root.tick()
        self.assertEqual(self.listed, [])

    def test_resume_takes_the_listed_project(self):
        self.fill(cwd="~/proj", machine="remote")
        self.window.refresh()
        self.root.tick()
        self.fill(cwd="/elsewhere", machine="")
        self.window.runs.selected = ("20260930-070000-c0ffee",)
        self.window.resume_button.press()
        self.assertEqual(self.started[0].argv, [*orchestrator.self_command(), "resume", "20260930-070000-c0ffee",
                                                "--machine", "remote", "--cwd", "~/proj"])

    def test_resume_needs_a_selected_run(self):
        self.window.resume_run()
        self.assertEqual(self.started, [])
        self.ui.messagebox.showerror.assert_called_once()

    def test_choose_folder_lists_its_runs(self):
        self.ui.filedialog.askdirectory.return_value = ""
        self.window.choose_folder()
        self.assertEqual(self.window.vars["cwd"].get(), "")
        self.ui.filedialog.askdirectory.return_value = "/home/me/proj"
        self.window.choose_folder()
        self.root.tick()
        self.assertEqual(self.listed, [("", "/home/me/proj")])

    def test_close_without_a_run(self):
        self.root.protocols["WM_DELETE_WINDOW"]()
        self.assertTrue(self.root.destroyed)
        self.root.tick()
        self.assertEqual(self.root.pending, [])  # no poll after the window is gone
        self.ui.messagebox.askokcancel.assert_not_called()

    def test_close_during_a_run_asks_first(self):
        self.fill(cwd="/proj")
        self.window.start_run()
        self.ui.messagebox.askokcancel.return_value = False
        self.window.close()
        self.assertEqual((self.started[0].interrupts, self.root.destroyed), (0, False))
        self.ui.messagebox.askokcancel.return_value = True
        self.window.close()
        self.window.close()  # asks once, interrupts once
        self.assertEqual(self.ui.messagebox.askokcancel.call_count, 2)
        self.assertEqual((self.started[0].interrupts, self.root.destroyed), (1, False))
        self.started[0].out, self.started[0].status = "interrupted; resume with: ...\n", 130
        self.root.tick()
        self.assertTrue(self.root.destroyed)
        self.assertEqual(self.listed, [])


DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")


def contrast(a, b):
    """WCAG's contrast ratio of two #RRGGBB colors."""
    def luminance(color):
        r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


PALETTE = {name: getattr(orchestrator, name) for name in
           ("NAVY", "NAVY_LIGHT", "NAVY_DARK", "NAVY_RAISED", "NAVY_HOVER", "PALE_MINT", "MUTED_MINT", "CORAL",
            "CORAL_LIGHT", "TEAL", "MINT", "AMBER")}


class TestGuiPalette(unittest.TestCase):
    def test_the_logos_colors(self):
        with open(os.path.join(DOCS, "logo.svg")) as f:
            svg = f.read().upper()
        for name in ("NAVY", "PALE_MINT", "CORAL", "TEAL", "MINT", "AMBER"):
            with self.subTest(name=name):
                self.assertIn(f'"{PALETTE[name]}"', svg)

    def test_text_is_legible_on_its_backgrounds(self):
        o = orchestrator
        pairs = {
            "text on the window, cards, fields and output": [(o.PALE_MINT, bg) for bg in
                                                             (o.NAVY, o.NAVY_LIGHT, o.NAVY_DARK)],
            "text on buttons and headings": [(o.PALE_MINT, o.NAVY_RAISED), (o.PALE_MINT, o.NAVY_HOVER)],
            "muted and disabled text": [(o.MUTED_MINT, o.NAVY), (o.MUTED_MINT, o.NAVY_LIGHT)],
            "Start run": [(o.NAVY, o.CORAL), (o.NAVY, o.CORAL_LIGHT)],
            "the header's role chain": [(o.MINT, o.NAVY)],
            "run statuses": [(color, o.NAVY) for color in (o.MINT, o.AMBER, o.CORAL)],
            "selected text": [(o.NAVY, o.MINT), (o.NAVY, o.MUTED_MINT)],
            "the output's command and exit lines": [(o.MINT, o.NAVY_DARK), (o.CORAL, o.NAVY_DARK)],
        }
        for use, colors in pairs.items():
            for fg, bg in colors:
                with self.subTest(use=use, fg=fg, bg=bg):
                    self.assertGreaterEqual(contrast(fg, bg), 4.5)

    def test_the_contrast_formula(self):
        self.assertAlmostEqual(contrast("#000000", "#FFFFFF"), 21)
        self.assertAlmostEqual(contrast("#777777", "#777777"), 1)


class TestGuiLogo(unittest.TestCase):
    def test_the_constant_is_the_committed_png(self):
        with open(os.path.join(DOCS, "logo-64.png"), "rb") as f:
            png = f.read()
        self.assertEqual(base64.b64decode(orchestrator.LOGO_PNG, validate=True), png)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(png[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", png[16:24]), (64, 64))


class TestStatusTag(unittest.TestCase):
    def test_each_status(self):
        url = "  https://github.com/o/r/pull/7"
        for status, tag in (("APPROVE", "ok"), (f"APPROVE{url}", "ok"), (f"APPROVE{url}  [quick workflow]", "ok"),
                            ("CHANGES_REQUESTED", "warn"), (f"CHANGES_REQUESTED{url}  [tdd workflow]", "warn"),
                            ("stale: no heartbeat for 6m; resume: orchestrator.py resume c0ffee", "warn"),
                            ("error: herdr: no such pane", "error"), ("error: boom  [quick workflow]", "error"),
                            ("running: pid 4242 on here", None), ("FINISHED", None), ("", None),
                            ("closed: workspace closed", None), ("error: interrupted; closed: workspace closed", "error"),
                            (f"APPROVE; closed: workspace closed{url}", "ok"),
                            ("[quick workflow]", None)):
            with self.subTest(status=status):
                self.assertEqual(orchestrator.status_tag(status), tag)


class TestRoleChain(unittest.TestCase):
    def test_in_the_order_the_steps_use_the_roles(self):
        self.assertEqual(orchestrator.role_chain(orchestrator.DEFAULT_WORKFLOW),
                         "Spec Collector → Builder → Reviewer")
        self.assertEqual(orchestrator.role_chain(QUICK), "Builder → Reviewer")
        roles = {"review": "Reviewer", "build": "Builder"}  # declared out of the steps' order
        self.assertEqual(orchestrator.role_chain(Pipeline("q", roles, QUICK.steps)), "Builder → Reviewer")


class TestRunWindowLook(unittest.TestCase):
    def setUp(self):
        self.workflows = os.path.join(without_workflow_files(self), "ai-agents-orchestrator", "workflows")
        os.makedirs(self.workflows)
        self.root, self.ui = FakeRoot(), fake_ui()
        self.started = []
        self.window = orchestrator.RunWindow(self.root, self.ui, start=self.start, list_runs=lambda m, c: [],
                                             background=lambda fn: fn())
        [self.style] = self.ui.styles

    def start(self, argv):
        self.started.append(FakeProcess(argv))
        return self.started[-1]

    def ttk_widgets(self):
        """Each ttk widget in the window, with the style it is drawn in."""
        classes = {cls: name for name, cls in vars(self.ui.ttk).items() if isinstance(cls, type)}
        for w in self.root.descendants():
            if (name := classes.get(type(w))) is not None:
                yield w, w.options.get("style") or (name if name == "Treeview" else f"T{name}")

    def test_every_widget_is_styled_on_clam(self):
        self.assertEqual(self.style.theme, "clam")
        styles = {style for _, style in self.ttk_widgets()}
        self.assertLessEqual({"TFrame", "Card.TFrame", "TLabel", "Section.TLabel", "TEntry", "TCombobox", "TButton",
                              "Accent.TButton", "TCheckbutton", "Treeview", "TScrollbar"}, styles)
        for style in styles | {"Treeview.Heading"}:
            with self.subTest(style=style):
                self.assertTrue({"background", "fieldbackground"} & self.style.options[style].keys())
        self.assertEqual(self.window.start_button.options["style"], "Accent.TButton")
        self.assertEqual(self.style.options["Accent.TButton"]["foreground"], orchestrator.NAVY)
        self.assertEqual(self.style.options["Accent.TButton"]["background"], orchestrator.CORAL)
        self.assertEqual(self.root.option_db["*TCombobox*Listbox.background"], orchestrator.NAVY)
        self.assertEqual(self.root.options["background"], orchestrator.NAVY)

    def test_every_color_is_from_the_palette(self):
        colors = set(PALETTE.values())
        values = [v for options in self.style.options.values() for v in options.values()]
        values += [spec[-1] for maps in self.style.maps.values() for specs in maps.values() for spec in specs]
        values += list(self.root.option_db.values())
        for w in (self.window.task, self.window.log, self.window.runs):
            values += list(w.options.values()) + [v for tag in w.tags.values() for v in tag.values()]
        hexes = [v for v in values if isinstance(v, str) and v.startswith("#")]
        self.assertTrue(hexes)
        self.assertEqual([v for v in hexes if v not in colors], [])

    def test_disabled_and_hovered_buttons_stay_legible(self):
        o = orchestrator
        for style in ("TButton", "Accent.TButton"):
            with self.subTest(style=style):
                maps = self.style.maps[style]
                self.assertEqual(dict(maps["background"])["disabled"], o.NAVY_LIGHT)
                self.assertEqual(dict(maps["foreground"])["disabled"], o.MUTED_MINT)
                self.assertEqual(maps["background"][0][0], "disabled")  # the first state that matches wins
        self.assertEqual(dict(self.style.maps["Accent.TButton"]["background"])["active"], o.CORAL_LIGHT)

    def test_the_logo_in_the_header_and_as_the_icon(self):
        logo = self.window.logo
        self.assertIsInstance(logo, FakePhotoImage)
        self.assertEqual(logo.options["data"], orchestrator.LOGO_PNG)
        self.assertEqual(self.root.icon, (True, logo))
        self.assertIn(logo, [w.options.get("image") for w, _ in self.ttk_widgets()])

    def header(self):
        return self.window.vars["pipeline"].get(), self.window.pipeline_label.options["style"]

    def test_the_header_names_the_selected_workflows_roles(self):
        texts = [w.options.get("text") for w, _ in self.ttk_widgets()]
        self.assertIn("ai-agents-orchestrator", texts)
        self.assertEqual(self.header(), ("Spec Collector → Builder → Reviewer", "Pipeline.TLabel"))
        with open(f"{EXAMPLES}/quick.toml") as src, open(f"{self.workflows}/quick.toml", "w") as dst:
            dst.write(src.read())
        self.window.vars["workflow"].set("quick")
        self.assertEqual(self.header(), ("Builder → Reviewer", "Pipeline.TLabel"))

    def test_an_unloadable_workflow_shows_its_error(self):
        with open(f"{self.workflows}/broken.toml", "w") as f:
            f.write("[[steps]]\nid = 3\n")
        self.window.vars["workflow"].set("broken")
        text, style = self.header()
        self.assertTrue(text.startswith(f"error: {self.workflows}/broken.toml: "), text)
        self.assertEqual(style, "Muted.TLabel")
        self.window.vars["workflow"].set("default")
        self.assertEqual(self.header(), ("Spec Collector → Builder → Reviewer", "Pipeline.TLabel"))

    def test_run_statuses_are_colored(self):
        o = orchestrator
        runs = self.window.runs
        self.assertEqual(runs.tags, {"ok": {"foreground": o.MINT}, "warn": {"foreground": o.AMBER},
                                     "error": {"foreground": o.CORAL}})
        rows = [("a", "done", "round 1", "APPROVE  https://github.com/o/r/pull/7", "t"),
                ("b", "review", "round 2", "running: pid 1 on here", "t"),
                ("c", "build", "round 1", "error: boom  [quick workflow]", "t")]
        self.window._show_runs(o.RunForm(cwd="/proj"), rows)
        self.assertEqual(list(runs.rows.values()), rows)
        self.assertEqual(runs.row_tags, {"a": ("ok",), "b": (), "c": ("error",)})

    def test_the_output_pane_is_a_terminal(self):
        o = orchestrator
        log = self.window.log
        self.assertEqual((log.options["font"], log.options["background"], log.options["foreground"]),
                         ("TkFixedFont", o.NAVY_DARK, o.PALE_MINT))
        self.assertEqual(log.tags["command"]["foreground"], o.MINT)
        self.assertEqual(log.tags["command"]["font"].options["weight"], "bold")
        self.assertEqual((log.tags["success"], log.tags["failure"]), ({"foreground": o.MINT},
                                                                      {"foreground": o.CORAL}))
        for status, tag in ((0, "success"), (130, "failure")):
            with self.subTest(status=status):
                log.tagged.clear()
                self.window.task.text, self.window.vars["cwd"].value = "fix it", "/proj"
                self.window.start_run()
                self.started[-1].out, self.started[-1].status = "working\n", status
                self.root.tick()
                self.assertEqual(log.tagged[0][1], ("command",))
                self.assertTrue(log.tagged[0][0].startswith("$ orchestrator.py run --cwd /proj"))
                self.assertEqual(log.tagged[1:], [("working\n", ()), (f"[exited with status {status}]\n", (tag,))])

    def test_fonts_are_tks_named_fonts(self):
        o = orchestrator
        fonts = self.window.fonts
        self.assertEqual({name: (f.name, f.options["weight"]) for name, f in fonts.items()},
                         {"section": ("TkHeadingFont", "bold"), "title": ("TkHeadingFont", "bold"),
                          "command": ("TkFixedFont", "bold")})
        self.assertEqual(fonts["title"].options["size"], -18)  # 1.5 times the heading's 12 pixels
        self.assertEqual(self.style.options["Section.TLabel"]["font"], fonts["section"])
        self.assertEqual(self.window.task.options["font"], "TkDefaultFont")

    def test_roboto_where_it_is_installed(self):
        ui = fake_ui(families=("DejaVu Sans", "Roboto"))
        window = orchestrator.RunWindow(FakeRoot(), ui, start=self.start, list_runs=lambda m, c: [],
                                        background=lambda fn: fn())
        families = {name: f.options["family"] for name, f in ui.named_fonts.items()}
        self.assertEqual({name: families[name] for name in orchestrator.GUI_TEXT_FONTS},
                         dict.fromkeys(orchestrator.GUI_TEXT_FONTS, "Roboto"))
        self.assertEqual(families["TkFixedFont"], "DejaVu Sans")  # the output pane stays monospace
        self.assertEqual({name: f.options["family"] for name, f in window.fonts.items()},
                         {"section": "Roboto", "title": "Roboto", "command": "DejaVu Sans"})

    def sizes(self):
        """Each font's size: Tk's named fonts by name, the window's own by its key."""
        return ({name: f.options["size"] for name, f in self.ui.named_fonts.items()} |
                {key: f.options["size"] for key, f in self.window.fonts.items()})

    def test_zoom_scales_every_font_and_the_run_lists_rows(self):
        at_start = self.sizes()
        self.assertEqual(self.style.options["Treeview"]["rowheight"], 15 + 8)
        self.window.zoom(1)
        self.assertEqual(self.sizes(), {name: round(size * 1.1) for name, size in at_start.items()})
        self.assertEqual((self.sizes()["TkDefaultFont"], self.sizes()["title"]), (-13, -20))
        self.assertEqual(self.style.options["Treeview"]["rowheight"], 16 + 8)  # 13 pixels' linespace
        self.assertEqual(self.window.vars["zoom"].get(), "110%")
        self.window.zoom(0)
        self.assertEqual(self.sizes(), at_start)
        self.assertEqual(self.window.vars["zoom"].get(), "100%")

    def test_zoom_widens_the_run_lists_fixed_columns_only(self):
        at_start = dict(self.window.runs.widths)
        self.assertEqual(at_start, {"run": 190, "phase": 70, "round": 70, "outcome": 300, "task": 300})
        for _ in range(3):
            self.window.zoom(1)
        self.assertEqual(self.window.vars["zoom"].get(), "150%")
        # Status and Task stretch into the room left, so the window asks for no more width than at 100%.
        self.assertEqual(self.window.runs.widths, {"run": 285, "phase": 105, "round": 105, "outcome": 300,
                                                   "task": 300})
        self.window.zoom(0)
        self.assertEqual(self.window.runs.widths, at_start)
        self.assertEqual(self.window.vars["zoom"].get(), "100%")

    def test_zoom_stops_at_either_end(self):
        buttons = self.window.zoom_buttons
        for _ in range(len(orchestrator.ZOOM_LEVELS)):
            buttons[1].press()
        self.assertEqual(self.window.vars["zoom"].get(), "200%")
        self.assertEqual(self.sizes()["TkDefaultFont"], -24)
        self.assertEqual((buttons[1].options["state"], buttons[-1].options["state"]), ("disabled", "normal"))
        for _ in range(len(orchestrator.ZOOM_LEVELS)):
            buttons[-1].press()
        self.assertEqual(self.window.vars["zoom"].get(), "75%")
        self.assertEqual((buttons[1].options["state"], buttons[-1].options["state"]), ("normal", "disabled"))
        buttons[0].press()
        self.assertEqual(self.window.vars["zoom"].get(), "100%")
        self.assertEqual(self.sizes()["TkDefaultFont"], -12)

    def test_zoom_by_keys_and_wheel(self):
        bindings = self.root.bindings
        mod = "Command" if orchestrator.sys.platform == "darwin" else "Control"
        clock = itertools.count()  # the wheel's events a second apart, so that each one steps
        for event, delta, zoom in (
                (f"<{mod}-Key-plus>", 0, "110%"), (f"<{mod}-Key-minus>", 0, "100%"),
                ("<Control-Button-4>", 0, "110%"), ("<Control-Button-5>", 0, "100%"),
                ("<Control-MouseWheel>", 120, "110%"), ("<Control-MouseWheel>", -120, "100%"),
                (f"<{mod}-Key-equal>", 0, "110%"), (f"<{mod}-Key-0>", 0, "100%")):
            with self.subTest(event=event, delta=delta), \
                    patch.object(orchestrator.time, "monotonic", return_value=next(clock)):
                bindings[event](SimpleNamespace(delta=delta))
                self.assertEqual(self.window.vars["zoom"].get(), zoom)

    def test_a_swipe_of_the_wheel_zooms_a_step_at_a_time(self):
        wheel = self.root.bindings["<Control-MouseWheel>"]

        def at(seconds, delta):
            with patch.object(orchestrator.time, "monotonic", return_value=seconds):
                wheel(SimpleNamespace(delta=delta))

        for i in range(10):  # a trackpad's swipe: small deltas, 20 ms apart
            at(100 + i * 0.02, 3)
        self.assertEqual(self.window.vars["zoom"].get(), "110%")
        at(100 + orchestrator.ZOOM_WHEEL_SECONDS, 3)
        self.assertEqual(self.window.vars["zoom"].get(), "125%")
        at(101, -3)
        self.assertEqual(self.window.vars["zoom"].get(), "110%")

    def test_zoom_keys(self):
        mac, linux = orchestrator.zoom_keys("darwin"), orchestrator.zoom_keys("linux")
        self.assertEqual((mac["<Command-Key-plus>"], mac["<Command-Key-minus>"], mac["<Command-Key-0>"]), (1, -1, 0))
        self.assertEqual((linux["<Control-Key-plus>"], linux["<Control-Key-minus>"], linux["<Control-Key-0>"]),
                         (1, -1, 0))
        self.assertFalse([e for e in mac if e.startswith("<Control-Key")])
        self.assertNotIn("<Control-0>", linux)  # Tk would read that as mouse button 0

    def test_the_platforms_font_without_roboto(self):
        self.assertEqual({f.options["family"] for f in self.ui.named_fonts.values()}, {"DejaVu Sans"})
        self.assertEqual({f.options["family"] for f in self.window.fonts.values()}, {"DejaVu Sans"})


class FakeTclError(Exception):
    pass


def fake_tkinter(root):
    ui = fake_ui()
    tk = ModuleType("tkinter")
    tk.Tk, tk.TclError = root, FakeTclError
    tk.StringVar, tk.BooleanVar, tk.Text, tk.PhotoImage = (ui.tk.StringVar, ui.tk.BooleanVar, ui.tk.Text,
                                                           ui.tk.PhotoImage)
    tk.ttk, tk.font, tk.filedialog, tk.messagebox = ui.ttk, ui.font, ui.filedialog, ui.messagebox
    return {"tkinter": tk, "tkinter.ttk": tk.ttk, "tkinter.font": tk.font, "tkinter.filedialog": tk.filedialog,
            "tkinter.messagebox": tk.messagebox}


class TestGuiCommand(unittest.TestCase):
    def test_smoke_test_opens_and_closes_the_window(self):
        root = FakeRoot()
        with patch.dict("sys.modules", fake_tkinter(lambda: root)):
            self.assertEqual(main(["gui", "--smoke-test"]), 0)
        self.assertTrue(root.destroyed)

    def test_the_window_stays_until_closed(self):
        root = FakeRoot()
        root.mainloop = MagicMock()
        with patch.dict("sys.modules", fake_tkinter(lambda: root)):
            self.assertEqual(main(["gui"]), 0)
        root.mainloop.assert_called_once()
        self.assertFalse(root.destroyed)

    def test_listing_runs_in_the_background(self):
        done = threading.Event()
        orchestrator._in_thread(done.set)
        self.assertTrue(done.wait(10))

    def test_without_tkinter(self):
        for platform, hint in (("linux", "apt install python3-tk"), ("darwin", "brew install python-tk@3.")):
            with self.subTest(platform=platform), patch.dict("sys.modules", {"tkinter": None}), \
                    patch.object(orchestrator.sys, "platform", platform), \
                    patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(main(["gui"]), orchestrator.EXIT_ERROR)
            self.assertIn("the GUI needs tkinter", err.getvalue())
            self.assertIn(hint, err.getvalue())

    def test_without_a_display(self):
        def no_display():
            raise FakeTclError("no display name and no $DISPLAY environment variable")

        with patch.dict("sys.modules", fake_tkinter(no_display)), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["gui"]), orchestrator.EXIT_ERROR)
        self.assertEqual(err.getvalue(),
                         "error: cannot open the GUI: no display name and no $DISPLAY environment variable\n")


class TestProjectRuns(unittest.TestCase):
    @patch.dict("os.environ", {"HERDR_ENV": ""})
    @patch.object(Host, "run_states", return_value=[])
    @patch.object(Host, "resolve_dir", return_value="/home/user/proj")
    def test_a_local_project_needs_no_herdr_pane(self, resolve_dir, run_states):
        self.assertEqual(orchestrator.project_runs("", "~/proj"), [])
        resolve_dir.assert_called_once_with("~/proj")
        run_states.assert_called_once_with("/home/user/proj")

    @patch.object(Host, "run_states", autospec=True, return_value=[])
    @patch.object(Host, "resolve_dir", return_value="/home/user/proj")
    @patch.object(Herdr, "ssh_target", return_value="remote-host")
    def test_a_machine_is_read_over_ssh(self, _target, _resolve, run_states):
        orchestrator.project_runs("remote", "~/proj")
        self.assertEqual(run_states.call_args.args[0].ssh_target, "remote-host")



# ---------------------------------------------------------------------------
# The show command
# ---------------------------------------------------------------------------

SHOW_ID = "20260929-120000-a1b2c3"
SHOW_DIR = f"/proj/.orchestrator/runs/{SHOW_ID}"
LONG_TASK = "add a token-bucket rate limiter to the API client " * 4


class ShowHost(FakeHost):
    """A FakeHost whose project /proj holds the saved runs in `states` and the run files in `files`."""

    def __init__(self, *states):
        super().__init__()
        self.states = [(10**6, s) for s in states]

    def resolve_dir(self, path):
        return "/proj"

    def run_states(self, cwd):
        return self.states

    def run_files(self, run_dir):
        # Written in mtime order, so the order of insertion is the age order.
        return [orchestrator.RunFile(p, 0) for p in self.files if p.startswith(f"{run_dir}/")]


def show_state(**kw):
    saved = {"run_id": SHOW_ID, "task": LONG_TASK, "cwd": "/proj", "phase": "done", "round": 1,
             "verdict": APPROVE, "workspace_id": "w1",
             "agents": {"spec": {"name": "spec-a1b2c3", "pane": "w1:p1"},
                        "build": {"name": "build-a1b2c3", "pane": "w1:p2", "kind": "codex"},
                        "review": {"name": "review-a1b2c3", "pane": "w1:p3", "kind": "claude"}}}
    return {**saved, **kw}


@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestShow(unittest.TestCase):
    def show(self, host, ref="a1b2c3", *flags):
        with patch.object(orchestrator, "connect", return_value=(None, host)), \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            code = main(["show", ref, *flags])
        return code, out.getvalue(), err.getvalue()

    def list_lines(self, host):
        with patch.object(orchestrator, "connect", return_value=(None, host)), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["list"]), 0)
        return out.getvalue().splitlines()

    def test_key_and_full_id_print_the_same(self):
        host = ShowHost(show_state(), show_state(run_id="20260929-130000-ffffff"))
        host.files[f"{SHOW_DIR}/review-1.md"] = f"VERDICT: {APPROVE}\n\nfine\n"
        by_key = self.show(host, "a1b2c3")
        self.assertEqual(by_key, self.show(host, SHOW_ID))
        self.assertEqual(by_key[0], 0)
        self.assertIn(SHOW_ID, by_key[1])
        self.assertNotIn("ffffff", by_key[1])

    def test_unknown_or_ambiguous_run(self):
        host = ShowHost(show_state(), show_state(run_id="20260930-120000-a1b2c3"))
        cases = [("nosuch", "error: no run nosuch under /proj/.orchestrator/runs\n"),
                 ("a1b2c3", (f"error: a1b2c3 matches several runs ({SHOW_ID}, 20260930-120000-a1b2c3); "
                             "give the full run id\n"))]
        for ref, message in cases:
            with self.subTest(ref=ref):
                self.assertEqual(self.show(host, ref), (orchestrator.EXIT_ERROR, "", message))

    def test_starts_with_the_list_line_and_the_whole_task(self):
        cases = [show_state(pr_url="https://github.com/o/r/pull/7"),
                 show_state(phase="build", owner=ME, verdict=None, workflow="nosuch"),
                 show_state(phase="review", verdict=None, error="interrupted")]
        for saved in cases:
            with self.subTest(phase=saved["phase"]):
                host = ShowHost(saved)
                _, out, _ = self.show(host)
                lines = out.splitlines()
                self.assertEqual(lines[0], self.list_lines(host)[0])
                self.assertEqual(lines[1], f"    {LONG_TASK}")
                self.assertGreater(len(LONG_TASK), 100)

    def test_branch_pull_request_and_conflicts(self):
        published = show_state(branch="orchestrator/x-a1b2c3", base_branch="main",
                               pr_url="https://github.com/o/r/pull/7", conflicts=["a.py", "b c.py"])
        _, out, _ = self.show(ShowHost(published))
        self.assertIn("\nbranch     orchestrator/x-a1b2c3\nbase       main\n"
                      "pr         https://github.com/o/r/pull/7\nconflicts  a.py, b c.py\nworkspace  w1\n", out)

        _, out, _ = self.show(ShowHost(show_state(pull_request=False)))
        for label in ("branch", "base", "pr", "conflicts"):
            self.assertNotRegex(out, rf"(?m)^{label} ")

    def test_branch_cleanup_once_set(self):
        published = show_state(branch="orchestrator/x-a1b2c3", pr_url="https://github.com/o/r/pull/7")
        _, out, _ = self.show(ShowHost({**published, "branch_cleanup": "kept: checked out in /proj"}))
        self.assertIn("pr         https://github.com/o/r/pull/7\ncleanup    kept: checked out in /proj\n", out)

        for saved in (published, {**published, "branch_cleanup": None}):
            _, out, _ = self.show(ShowHost(saved))
            self.assertNotRegex(out, r"(?m)^cleanup ")

    def test_closed_once_set(self):
        _, out, _ = self.show(ShowHost(show_state(closed="workspace closed; worktree deleted; branch deleted")))
        self.assertIn("\nworkspace  w1\nclosed     workspace closed; worktree deleted; branch deleted\n", out)
        for saved in (show_state(), show_state(closed=None)):
            _, out, _ = self.show(ShowHost(saved))
            self.assertNotRegex(out, r"(?m)^closed ")

    def test_turns_once_recorded(self):
        turns = [turn("spec", "spec.md", 0, 4000, rnd=0, human_paced=True),
                 turn("build", "build-1.md", 4000, 4600, agent="codex"),
                 turn("quality", "quality-1-1.md", 4600, 4650, q=1, agent=None),
                 turn("review", "review-1.md", None, 4700),
                 turn("build", "build-2.md", 4800, None, rnd=2, agent="codex")]
        _, out, _ = self.show(ShowHost(show_state(turns=turns)))
        self.assertIn("\nturns\n"
                      "    spec     0     claude  2026-10-03 04:00:00  1h 06m   spec.md\n"
                      "    build    1     codex   2026-10-03 05:06:40  10m 00s  build-1.md\n"
                      "    quality  1 q1  -       2026-10-03 05:16:40  50s      quality-1-1.md\n"
                      "    review   1     claude  ?                    ?        review-1.md\n"
                      "    build    2     codex   2026-10-03 05:20:00  ?        build-2.md\n", out)
        for saved in (show_state(), show_state(turns=[])):
            _, out, _ = self.show(ShowHost(saved))
            self.assertNotRegex(out, r"(?m)^turns")

    def test_worktree_of_a_worktree_run(self):
        _, out, _ = self.show(ShowHost(show_state(worktree=WORKTREE, branch="orchestrator/x-a1b2c3")))
        self.assertIn(f"\nworktree   {WORKTREE}\nbranch     orchestrator/x-a1b2c3\n", out)
        _, out, _ = self.show(ShowHost(show_state()))
        self.assertNotRegex(out, r"(?m)^worktree ")

    def test_every_agent_with_its_kind_and_pane(self):
        _, out, _ = self.show(ShowHost(show_state()))
        self.assertIn("\nagents\n"
                      "    spec    claude    w1:p1\n"
                      "    build   codex     w1:p2\n"
                      "    review  claude    w1:p3\n", out)

    def test_files_oldest_first(self):
        host = ShowHost(show_state())
        for name in ("spec.md", "build-1.md", "review-1.md"):
            host.files[f"{SHOW_DIR}/{name}"] = "x"
        _, out, _ = self.show(host)
        self.assertIn(f"\nfiles\n    {SHOW_DIR}/spec.md\n    {SHOW_DIR}/build-1.md\n    {SHOW_DIR}/review-1.md\n",
                      out)

    def test_the_last_review_in_a_later_round(self):
        host = ShowHost(show_state(phase="build", round=2, verdict=CHANGES_REQUESTED))
        host.files[f"{SHOW_DIR}/review-1.md"] = f"\n**VERDICT: {CHANGES_REQUESTED}**\n\n## Findings\n\n1. a bug\n"
        _, out, _ = self.show(host)
        self.assertTrue(out.endswith(f"\nlast review: {SHOW_DIR}/review-1.md\n\n\n## Findings\n\n1. a bug\n"))
        self.assertNotIn("VERDICT", out)

    def test_a_review_without_a_verdict_line_is_shown_whole(self):
        host = ShowHost(show_state(phase="review", round=1, verdict=None))
        host.files[f"{SHOW_DIR}/review-1.md"] = "half a review"
        _, out, _ = self.show(host)
        self.assertTrue(out.endswith(f"\nlast review: {SHOW_DIR}/review-1.md\nhalf a review\n"))

    def test_no_last_review(self):
        solo = orchestrator.workflow_definition(SOLO)
        cases = {"no verdict step": show_state(workflow="solo", workflow_definition=solo, verdict="FINISHED"),
                 "spec phase": show_state(phase="spec", round=0, verdict=None),
                 "unknown workflow": show_state(workflow="nosuch")}
        for why, saved in cases.items():
            with self.subTest(why):
                host = ShowHost(saved)
                for name in ("review-1.md", "build.md", "build-1.md"):
                    host.files[f"{SHOW_DIR}/{name}"] = f"VERDICT: {APPROVE}\n"
                code, out, _ = self.show(host)
                self.assertEqual(code, 0)
                self.assertNotIn("last review", out)
        _, out, _ = self.show(ShowHost(cases["unknown workflow"]))
        self.assertIn("\nworkflow   nosuch: this orchestrator cannot load it\n", out)
        _, out, _ = self.show(ShowHost(cases["no verdict step"]))
        self.assertIn("\nworkflow   solo: build\n", out)

    @patch.object(Herdr, "ssh_target", return_value="remote-host")
    def test_on_a_machine_everything_goes_over_ssh(self, _target):
        saved = show_state(phase="build", round=2, verdict=CHANGES_REQUESTED)
        review = f"{SHOW_DIR}/review-1.md"

        def remote(argv, **kw):
            self.assertEqual(argv[:4], ["ssh", "-o", "BatchMode=yes", "remote-host"])
            script, *args = shlex.split(argv[4])[2:]
            if "pwd" in script:
                return completed("/proj\n")
            if "state.json; do" in script:
                return completed(f"{STALE_SECONDS + 1} {json.dumps(saved)}\n")
            if "stat" in script:
                self.assertEqual(args, ["_", SHOW_DIR])
                return completed(f"7 {SHOW_DIR}/spec.md\0" f"9 {review}\0" f"8 {SHOW_DIR}/build-1.md\0")
            if args == ["_", review]:
                return completed(f"VERDICT: {CHANGES_REQUESTED}\nremote findings\n")
            return completed(returncode=orchestrator.MISSING_FILE_STATUS)

        # remote checks that every process the Host from connect starts is an ssh command.
        run = MagicMock(side_effect=remote)
        with patch.object(orchestrator, "Host", lambda target=None: Host(target, run=run)), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["show", "--machine", "M", "--cwd", "~/proj", "a1b2c3"]), 0)
        self.assertTrue(out.getvalue().endswith(
            f"\nfiles\n    {SHOW_DIR}/spec.md\n    {SHOW_DIR}/build-1.md\n    {review}\n"
            f"\nlast review: {review}\nremote findings\n"))
        self.assertIn("resume: orchestrator.py resume a1b2c3 --machine M --cwd '~/proj'", out.getvalue())
        reads = [shlex.split(c.args[0][4])[-1] for c in run.call_args_list
                 if f"|| exit {orchestrator.MISSING_FILE_STATUS}; cat" in c.args[0][4]]
        self.assertEqual(reads, [f"{SHOW_DIR}/review-2.md", review])

    def test_help(self):
        for argv, text in ((["--help"], "show"), (["show", "--help"], "RUN")):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as exit_, \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                parse_args(argv)
            self.assertEqual(exit_.exception.code, 0)
            self.assertIn(text, out.getvalue())
        self.assertIn("the run id, or the six-character key at its end", out.getvalue())


class TestRunFiles(unittest.TestCase):
    def test_real_directory_oldest_first(self):
        with tempfile.TemporaryDirectory() as d:
            host = Host()
            run_dir = f"{d}/{orchestrator.RUNS_DIR}/r-abc"
            # Name order is a, b, c, d; modification order is c, a, then b and d together.
            for name, mtime in (("b.md", 300), ("a.md", 200), ("c.md", 100), ("d.md", 300),
                                ("state.json", 50), ("state.json.tmp.123", 60), ("spec.md.tmp.9", 70)):
                host.write(f"{run_dir}/{name}", name)
                os.utime(f"{run_dir}/{name}", (mtime, mtime))
            os.mkdir(f"{run_dir}/sub")
            self.assertEqual(host.run_files(run_dir), [(f"{run_dir}/{n}", m) for n, m in
                                                       (("c.md", 100), ("a.md", 200), ("b.md", 300), ("d.md", 300))])

    def test_missing_directory(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Host().run_files(f"{d}/nosuch"), [])

    def test_one_call_over_ssh(self):
        run = MagicMock(return_value=completed("5 /r/b.md\0" "5 /r/a b.md\0" "3 /r/c.md\0"))
        self.assertEqual(Host("remote-host", run=run).run_files("/r"), [("/r/c.md", 3), ("/r/a b.md", 5), ("/r/b.md", 5)])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:4], ["ssh", "-o", "BatchMode=yes", "remote-host"])
        self.assertIn("stat -c %Y", run.call_args.args[0][4])
        self.assertIn("stat -f %m", run.call_args.args[0][4])

    def test_failure_raises(self):
        host = Host(run=MagicMock(return_value=completed(stderr="Permission denied", returncode=1)))
        with self.assertRaisesRegex(OrchestratorError, "Permission denied"):
            host.run_files("/r")

    def test_a_time_that_is_not_one(self):
        host = Host(run=MagicMock(return_value=completed("5 /r/a.md\0" "soon /r/b.md\0")))
        with self.assertRaises(OrchestratorError) as raised:
            host.run_files("/r")
        self.assertEqual(str(raised.exception), "listing /r: stat printed 'soon', not a time")


# ---------------------------------------------------------------------------
# run --spec
# ---------------------------------------------------------------------------

# CRLF and trailing blanks, which the run's copy keeps.
GIVEN_SPEC = "# Add a token-bucket rate limiter\r\n\r\n## Goal\r\nlimit requests  \n\n"
SPEC_SOURCE = "/home/me/spec.md"


def seeded_run(pipeline=orchestrator.DEFAULT_WORKFLOW):
    """A new run's state, as main makes it for the workflow with --spec."""
    return RunState("20260929-120000-a1b2c3", "add a rate limiter", "/proj", None,
                    phase=orchestrator.seeded_start(pipeline).id, workflow=pipeline.name)


class TestSeededSpec(unittest.TestCase):
    def run_seeded(self, script, text=GIVEN_SPEC, pipeline=orchestrator.DEFAULT_WORKFLOW, **kw):
        """Run a new run given its spec; also returns the first state.json it wrote, and its log."""
        wf, herdr, host, notes = make_workflow(script, state=seeded_run(pipeline), pipeline=pipeline,
                                               spec=orchestrator.SpecFile(SPEC_SOURCE, text), **kw)
        first_saved = []
        write = host.write

        def recording_write(path, text):
            if path.endswith("/state.json") and not first_saved:
                first_saved.append(json.loads(text))
            write(path, text)
        host.write = recording_write
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            verdict = wf.run()
        return verdict, wf, herdr, host, notes, first_saved[0], err.getvalue()

    def test_the_spec_is_the_contract_and_the_interview_is_skipped(self):
        seen = []
        verdict, wf, herdr, host, notes, first_saved, log = self.run_seeded({
            "build": [recording(build_turn(1), seen)], "review": [review_turn(1, APPROVE)],
        })

        self.assertEqual(verdict, APPROVE)
        self.assertEqual(host.files[f"{D}/spec.md"], GIVEN_SPEC)
        self.assertLess(host.writes.index(f"{D}/spec.md"), host.writes.index(f"{D}/state.json"))
        self.assertEqual((first_saved["phase"], first_saved["round"], first_saved["agents"]), ("build", 0, {}))
        self.assertIn(f"The spec in {D}/spec.md was agreed", seen[0])
        # No Spec Collector: no agent, no pane, no notification.
        self.assertEqual([c[1:3] for c in herdr.calls if c[0] == "start"],
                         [("build-a1b2c3", "w1:p1"), ("review-a1b2c3", "w1:p2")])
        self.assertEqual([c for c in herdr.calls if c[0] == "split"], [("split", "w1:p1", "right")])
        self.assertEqual(herdr.panes, 2)
        self.assertFalse([c for c in herdr.calls if c[0] == "focus"])
        self.assertFalse([n for n in notes if "waiting" in n])
        self.assertEqual(list(wf.state.agents), ["build", "review"])
        self.assertIn(f"[spec] spec.md is a copy of {SPEC_SOURCE}; skipping the Spec Collector's interview", log)
        self.assertNotIn("already written", log)
        branch = "orchestrator/add-a-token-bucket-rate-limiter-a1b2c3"
        self.assertEqual((host.prs[0]["title"], host.prs[0]["head"]), ("Add a token-bucket rate limiter", branch))

    def test_a_spec_without_a_title_titles_by_the_task(self):
        verdict, _, _, host, *_ = self.run_seeded(
            {"build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, text="Limit the requests.\n")

        self.assertEqual(verdict, APPROVE)
        self.assertEqual((host.prs[0]["title"], host.prs[0]["head"]),
                         ("add a rate limiter", "orchestrator/add-a-rate-limiter-a1b2c3"))

    def test_a_seeded_run_resumes_like_any_other(self):
        def interrupted(prompt, state, host):
            raise KeyboardInterrupt
        wf, herdr, host, _ = make_workflow({"build": [build_turn(1)], "review": [interrupted]},
                                           state=seeded_run(), spec=orchestrator.SpecFile(SPEC_SOURCE, GIVEN_SPEC))
        with patch("sys.stderr"), self.assertRaises(KeyboardInterrupt):
            wf.run()
        saved = RunState.from_dict(json.loads(host.files[f"{D}/state.json"]))
        self.assertEqual((saved.phase, saved.round, saved.error), ("review", 1, "interrupted"))

        # As main resumes it: no spec given, and here a new workspace, since herdr lost the old one.
        wf, herdr, host, _ = make_workflow({"review": [review_turn(1, APPROVE)]}, host=host, state=saved,
                                           pull_request=saved.pull_request)
        with patch("sys.stderr"):
            self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(host.files[f"{D}/spec.md"], GIVEN_SPEC)
        self.assertEqual([c[1:3] for c in herdr.calls if c[0] == "start"], [("review-a1b2c3", "w1:p2")])
        self.assertEqual([c for c in herdr.calls if c[0] == "split"], [("split", "w1:p1", "right")])
        self.assertEqual(host.prs[0]["title"], "Add a token-bucket rate limiter")

    def test_a_contract_role_with_a_later_step_lays_out_last(self):
        planned = Pipeline("planned", {"plan": "Planner", "build": "Builder"}, (
            Step("plan", "plan", "plan.md", "Plan {task}; write {path}."),
            Step("build", "build", "build-{n}.md", "Build {plan_path}; report to {build_path}.",
                 again="Fix {prev_check_path}; report to {build_path}.", edits=True),
            Step("check", "plan", "check-{n}.md", "Check {change} against {plan_path}; write {path}.",
                 loop_to="build"),
        ))
        seen = []
        verdict, _, herdr, _, _, _, log = self.run_seeded({
            "build": [recording(build_turn(1), seen)],
            "plan": [writes(lambda s: f"{s.dir}/check-1.md", f"VERDICT: {APPROVE}\n")],
        }, pipeline=planned)

        self.assertEqual(verdict, APPROVE)
        self.assertEqual(seen, [f"Build {D}/plan.md; report to {D}/build-1.md.{NOTE}"])
        self.assertEqual([c[1:3] for c in herdr.calls if c[0] == "start"],
                         [("build-a1b2c3", "w1:p1"), ("plan-a1b2c3", "w1:p2")])
        self.assertEqual([c for c in herdr.calls if c[0] == "split"], [("split", "w1:p1", "right")])
        self.assertIn(f"[plan] plan.md is a copy of {SPEC_SOURCE}; skipping the Planner's turn", log)

    def test_a_workflow_without_a_contract_step_cannot_take_a_spec(self):
        with self.assertRaisesRegex(OrchestratorError, r"^workflow quick starts with step build, which edits or "
                                                       r"writes a file per round; --spec needs a first step that "
                                                       r"writes the spec$"):
            orchestrator.seeded_start(QUICK)

    def test_a_workflow_with_only_a_contract_step_cannot_take_a_spec(self):
        solo = Pipeline("solo", {"spec": "Spec Collector"}, (Step("spec", "spec", "spec.md", "Write {path}."),))
        with self.assertRaisesRegex(OrchestratorError, "workflow solo has no step after spec, so with --spec"):
            orchestrator.seeded_start(solo)


@patch.object(Workflow, "__init__", return_value=None)
@patch.object(Workflow, "run", return_value=APPROVE)
@patch.object(Host, "resolve_dir", return_value="/proj")
@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestSpecCLI(unittest.TestCase):
    def setUp(self):
        without_workflow_files(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.spec = self.file("spec.md", GIVEN_SPEC)

    def file(self, name, text, mode="w"):
        path = os.path.join(self.dir, name)
        with open(path, mode, **({"newline": ""} if "b" not in mode else {})) as f:
            f.write(text)
        return path

    def started(self, init, *argv):
        with patch("builtins.print"):
            self.assertEqual(main(["run", *argv]), 0)
        return init.call_args.args[2], init.call_args.kwargs

    def refused(self, init, *argv):
        """The error message of a run that exits 1 before its Workflow, and so its workspace, exists."""
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["run", *argv]), orchestrator.EXIT_ERROR)
        init.assert_not_called()
        return err.getvalue()

    def test_the_spec_reaches_the_workflow(self, _resolve, _run, init):
        state, kw = self.started(init, "--spec", self.spec)
        self.assertEqual(kw["spec"], orchestrator.SpecFile(self.spec, GIVEN_SPEC))
        self.assertEqual((state.task, state.phase, state.round), ("Add a token-bucket rate limiter", "build", 0))

    def test_a_given_task_is_used_unchanged(self, _resolve, _run, init):
        state, _ = self.started(init, "  the task\nas typed", "--spec", self.spec)
        self.assertEqual(state.task, "  the task\nas typed")

    def test_an_untitled_spec_names_the_task_after_its_file(self, _resolve, _run, init):
        state, _ = self.started(init, "--spec", self.file("rate-limiter.v2.md", "Limit the requests.\n"))
        self.assertEqual(state.task, "rate-limiter.v2")

    def test_a_relative_path_is_the_shells_not_cwds(self, _resolve, _run, init):
        here = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, here)
        _, kw = self.started(init, "--spec", "spec.md", "--cwd", "/elsewhere")
        self.assertEqual(kw["spec"].text, GIVEN_SPEC)

    def test_without_spec_the_run_starts_at_the_first_step(self, _resolve, _run, init):
        state, kw = self.started(init, "task")
        self.assertEqual((state.phase, kw["spec"]), ("spec", None))

    @patch.object(Herdr, "ssh_target", return_value="remote-host")
    # No real ci.env on this machine may decide the --quality-gate case.
    @patch.dict("os.environ", {"XDG_CONFIG_HOME": "/nonexistent"})
    def test_spec_combines_with_the_other_run_flags(self, _target, _resolve, _run, init):
        example = f"{EXAMPLES}/spec-build-review.toml"
        cases = [
            (["--no-pr"], lambda st, kw: self.assertFalse(st.pull_request)),
            (["--quality-gate", JOB], lambda st, kw: self.assertEqual((st.quality_job, kw["ci"].job), (JOB, JOB))),
            (["--machine", "m", "--cwd", "~/p"], lambda st, kw: self.assertEqual(st.machine, "m")),
            (["--workflow", "default"], lambda st, kw: self.assertEqual(st.workflow, "default")),
            (["--workflow-file", example], lambda st, kw: self.assertEqual(st.workflow, "spec-build-review")),
        ]
        for flags, check in cases:
            with self.subTest(flags=flags):
                state, kw = self.started(init, "--spec", self.spec, *flags)
                self.assertEqual((state.phase, kw["spec"].text), ("build", GIVEN_SPEC))
                check(state, kw)

    def test_an_unreadable_spec_is_an_error(self, _resolve, _run, init):
        missing = os.path.join(self.dir, "nosuch.md")
        self.assertEqual(self.refused(init, "--spec", missing),
                         f"error: cannot read spec file {missing}: No such file or directory\n")
        self.assertEqual(self.refused(init, "--spec", self.dir),
                         f"error: cannot read spec file {self.dir}: Is a directory\n")
        binary = self.file("spec.bin", b"\xff\xfe# spec", "wb")
        self.assertIn(f"error: spec file {binary} is not UTF-8 text", self.refused(init, "--spec", binary))

    def test_an_empty_spec_is_an_error(self, _resolve, _run, init):
        for text in ("", " \n\t\r\n"):
            with self.subTest(text=text):
                path = self.file("blank.md", text)
                self.assertEqual(self.refused(init, "--spec", path), f"error: spec file {path} is empty\n")

    def test_a_workflow_without_a_contract_step_is_an_error(self, _resolve, _run, init):
        message = self.refused(init, "--spec", self.spec, "--workflow-file", f"{EXAMPLES}/quick.toml")
        self.assertIn("workflow quick starts with step build", message)
        self.assertIn("--spec needs a first step that writes the spec", message)

    def test_a_task_or_spec_is_needed(self, _resolve, _run, init):
        with self.assertRaises(SystemExit) as exit_, patch("sys.stderr", new_callable=io.StringIO) as err:
            parse_args(["run"])
        self.assertEqual(exit_.exception.code, 2)
        self.assertIn("orchestrator.py run: error: a task or --spec FILE is needed", err.getvalue())

    def test_resume_takes_no_spec(self, _resolve, _run, init):
        # Not even as an abbreviation of --spec-model, which argparse would otherwise make of it.
        for flags in (["--spec", "spec.md"], ["--spec"], [f"--spec={self.spec}"]):
            with self.subTest(flags=flags):
                with self.assertRaises(SystemExit) as exit_, patch("sys.stderr", new_callable=io.StringIO) as err:
                    parse_args(["resume", "a1b2c3", *flags])
                self.assertEqual(exit_.exception.code, 2)
                self.assertIn("unrecognized arguments: --spec", err.getvalue())
        self.assertEqual(parse_args(["resume", "a1b2c3", "--spec-model", "opus"]).spec_model, "opus")

    def test_run_help_lists_spec(self, _resolve, _run, init):
        with self.assertRaises(SystemExit), patch("sys.stdout", new_callable=io.StringIO) as out:
            parse_args(["run", "--help"])
        self.assertIn("--spec FILE", out.getvalue())
        self.assertEqual(parse_args(["run", "t", "--spec-model", "opus"]).spec_model, "opus")


class TestSpecBuildReviewExample(unittest.TestCase):
    def test_it_is_the_default_workflow(self):
        # Fails when a built-in prompt or step changes; regenerate the file with `workflows default`.
        example = orchestrator.load_workflow_file(f"{EXAMPLES}/spec-build-review.toml", BUILT_IN_PROMPTS)
        self.assertEqual(orchestrator.workflow_definition(example),
                         orchestrator.workflow_definition(orchestrator.DEFAULT_WORKFLOW))


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------

PR_A = "https://github.com/o/r/pull/1"
PR_B = "https://github.com/o/r/pull/2"


def drain_spec_text(title, status="ready-for-agent", *header, nl="\n"):
    """A spec as drain reads it: a title, a Status: line unless status is None, the further header lines, a body."""
    lines = [f"# {title}", ""]
    if status is not None:
        lines.append(f"Status: {status}")
    lines += [*header, "", "## Goal", "Status: in the body, which drain never reads", ""]
    return nl.join(lines)


def approved(pr=PR_A, conflicts=()):
    """A Workflow.run that opened the pull request pr and approved, as run_workflow's workflow."""
    def run(wf):
        wf.state.pr_url, wf.state.conflicts, wf.state.phase = pr, list(conflicts), orchestrator.DONE
        return APPROVE
    return run


def ended(verdict, pr=PR_A):
    def run(wf):
        wf.state.pr_url, wf.state.phase = pr, orchestrator.DONE
        return verdict
    return run


def raising(error):
    def run(wf):
        raise error
    return run


@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestDrain(unittest.TestCase):
    """drain through main, each run's Workflow.run played by a script; FakeHost is the project's host."""

    def setUp(self):
        without_workflow_files(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.host = FakeHost()
        self.runs = []  # each run's state as Workflow.run got it, and the spec it was given
        for p in (patch.object(orchestrator, "connect", return_value=(None, self.host)),
                  patch.object(Host, "resolve_dir", return_value="/proj")):
            p.start()
            self.addCleanup(p.stop)
        self.sweep = patch.object(orchestrator, "sweep_branches").start()
        self.addCleanup(patch.stopall)

    def spec(self, slug, text):
        os.makedirs(os.path.join(self.dir, slug), exist_ok=True)
        with open(self.path(slug), "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def path(self, slug):
        return os.path.join(self.dir, slug, "spec.md")

    def text(self, slug):
        with open(self.path(slug), encoding="utf-8", newline="") as f:
            return f.read()

    def header(self, slug):
        """The spec's Status:, Run: and PR: values, None for each it lacks."""
        text = self.text(slug)
        return tuple(orchestrator.header_field(text, k) for k in ("Status", "Run", "PR"))

    def drain(self, *outcomes, argv=()):
        """main's exit status for drain with argv, each run ending by the next of outcomes; and what it printed."""
        script = list(outcomes)

        def run(wf):
            self.runs.append((copy.deepcopy(wf.state), wf.spec))
            return script.pop(0)(wf)
        with patch.object(Workflow, "run", autospec=True, side_effect=run), \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            status = main(["drain", self.dir, *argv])
        self.assertEqual(script, [], "every scripted run ran")
        return status, out.getvalue(), err.getvalue()

    def saved_run(self, slug, run_id, age=0, **fields):
        """A run of the spec's, as an interrupted drain leaves it in the project's runs."""
        state = RunState(run_id, f"spec {slug}", "/proj", None, **fields)
        self.host.files[f"{state.dir}/state.json"] = json.dumps(asdict(state))
        self.host.ages[run_id] = age

    def test_independent_specs_run_in_slug_order(self):
        self.spec("b-second", drain_spec_text("Second"))
        self.spec("a-first", drain_spec_text("First"))

        status, out, _ = self.drain(approved(PR_A), approved(PR_B))

        self.assertEqual(status, 0)
        self.assertEqual([state.task for state, _ in self.runs], ["First", "Second"])
        a, b = (state for state, _ in self.runs)
        self.assertEqual(self.header("a-first"), ("in-review", a.run_id, PR_A))
        self.assertEqual(self.header("b-second"), ("in-review", b.run_id, PR_B))
        self.assertTrue(out.endswith(f"a-first: ready-for-agent -> in-review  {PR_A}\n"
                                     f"b-second: ready-for-agent -> in-review  {PR_B}\n"
                                     "all specs cleared\n"), out)
        self.sweep.assert_called_once_with(self.host, "/proj")

    def test_each_run_is_a_run_spec_run_of_its_spec(self):
        self.spec("a", drain_spec_text("First"))
        self.drain(approved())

        state, given = self.runs[0]
        self.assertEqual(given, orchestrator.SpecFile(self.path("a"), drain_spec_text("First")))
        self.assertEqual((state.phase, state.pull_request, state.cwd), ("build", True, "/proj"))

    def test_the_run_line_is_written_before_the_run(self):
        self.spec("a", drain_spec_text("First"))
        seen = []

        def run(wf):
            seen.append(self.header("a"))
            return approved()(wf)
        self.drain(run)

        self.assertEqual(seen, [("ready-for-agent", self.runs[0][0].run_id, None)])

    def test_an_edit_made_while_a_spec_runs_is_kept(self):
        self.spec("a", drain_spec_text("First"))
        self.spec("b", drain_spec_text("Second"))

        def run(wf):
            # The human adds to a's body, and holds b back, while a runs.
            with open(self.path("a"), "a", encoding="utf-8") as f:
                f.write("A note added during the run.\n")
            self.spec("b", drain_spec_text("Second", "needs-info"))
            return approved(PR_A)(wf)
        status, _, _ = self.drain(run)

        self.assertEqual(status, 0)
        self.assertEqual(len(self.runs), 1)
        self.assertTrue(self.text("a").endswith("A note added during the run.\n"))
        self.assertEqual(self.header("a")[::2], ("in-review", PR_A))
        self.assertEqual(self.text("b"), drain_spec_text("Second", "needs-info"))

    def gone(self, slug):
        """Why drain finds the spec deleted, as read_spec_file says it."""
        return f"cannot read spec file {self.path(slug)}: No such file or directory"

    def test_a_spec_deleted_while_another_runs_is_skipped(self):
        self.spec("a", drain_spec_text("First"))
        self.spec("b", drain_spec_text("Second"))
        self.spec("c", drain_spec_text("Third"))

        def run(wf):
            os.remove(self.path("b"))
            return approved(PR_A)(wf)
        status, out, err = self.drain(run, approved(PR_B))

        # b was ready-for-agent when drain last read it, so it is left, gone, and the drain does not end cleared.
        self.assertEqual(status, orchestrator.EXIT_WAITING)
        self.assertEqual([state.task for state, _ in self.runs], ["First", "Third"])
        self.assertIn("b: ", err)
        self.assertIn("not running it", err)
        self.assertEqual(err.count(f"b: {self.gone('b')}; not running it\n"), 1, err)
        self.assertNotIn("all specs cleared", out)
        self.assertTrue(out.endswith(f"c: ready-for-agent -> in-review  {PR_B}\nb: gone ({self.gone('b')})\n"), out)

    def test_the_running_spec_deleted_during_its_run_is_not_recorded(self):
        self.spec("a", drain_spec_text("First"))
        self.spec("b", drain_spec_text("Second"))

        def run(wf):
            os.remove(self.path("a"))
            return approved(PR_A)(wf)
        status, out, err = self.drain(run, approved(PR_B))

        self.assertEqual(status, orchestrator.EXIT_WAITING)
        self.assertEqual(len(self.runs), 2)
        self.assertFalse(os.path.exists(self.path("a")))
        self.assertIn(f"a: {self.gone('a')}; not recording Status: in-review, PR: {PR_A}\n", err)
        self.assertNotIn("not running it", err)
        self.assertEqual(self.header("b")[::2], ("in-review", PR_B))
        self.assertTrue(out.endswith(f"a: ready-for-agent -> gone\nb: ready-for-agent -> in-review  {PR_B}\n"
                                     f"a: gone ({self.gone('a')})\n"), out)

    def test_the_running_spec_emptied_during_a_failed_run_is_not_recorded(self):
        self.spec("a", drain_spec_text("First"))

        def run(wf):
            self.spec("a", "")
            raise OrchestratorError("the Builder's pane is gone")
        status, out, err = self.drain(run)

        run_id = self.runs[0][0].run_id
        why = f"spec file {self.path('a')} is empty"
        self.assertEqual(status, orchestrator.EXIT_ERROR)
        self.assertEqual(self.text("a"), "")
        self.assertIn("error: the Builder's pane is gone\n", err)
        self.assertIn(f"orchestrator.py resume {run_id.rsplit('-', 1)[-1]}", err)
        self.assertIn(f"a: {why}; not recording Status: ready-for-human\n", err)
        self.assertNotIn("a is ready-for-human", err)
        self.assertEqual(out, "a: ready-for-agent -> gone\n")

    def test_a_spec_deleted_before_its_run_line_is_written_is_not_run(self):
        self.spec("a", drain_spec_text("First"))
        reread = orchestrator.QueuedSpec.reread
        rereads = []

        def deleting_on_the_second(spec):
            # The first is next_startable's; the human deletes the spec just before drain_spec writes Run:.
            rereads.append(spec.slug)
            if len(rereads) == 2:
                os.remove(self.path("a"))
            return reread(spec)
        with patch.object(orchestrator.QueuedSpec, "reread", autospec=True, side_effect=deleting_on_the_second):
            status, out, err = self.drain()

        self.assertEqual(status, orchestrator.EXIT_WAITING)
        self.assertEqual(self.runs, [])
        self.assertRegex(err, rf"a: {re.escape(self.gone('a'))}; not recording Run: \d{{8}}-\d{{6}}-\w{{6}}\n")
        self.assertEqual(out, f"a: gone ({self.gone('a')})\n")

    def test_a_blocker_deleted_mid_drain_is_shown_gone_without_its_pull_request(self):
        self.spec("a", drain_spec_text("First"))
        self.spec("b", drain_spec_text("Second", "in-review", f"PR: {PR_B}"))
        self.spec("c", drain_spec_text("Third", "ready-for-agent", "Blocked by: b"))
        self.host.pull_requests[PR_B] = {"state": "OPEN", "headRefOid": "b" * 40, "closedAt": None}

        def run(wf):
            os.remove(self.path("b"))
            return approved(PR_A)(wf)
        status, out, err = self.drain(run)

        self.assertEqual(status, orchestrator.EXIT_WAITING)
        self.assertEqual(err.count(f"b: {self.gone('b')}; gone\n"), 1, err)
        self.assertNotIn("not running it", err)
        self.assertTrue(out.endswith(f"c waits on:\n    b: gone ({self.gone('b')})\n"), out)
        self.assertNotIn(PR_B, out)

    def test_a_done_spec_deleted_mid_drain_is_logged_once(self):
        self.spec("a", drain_spec_text("First"))
        self.spec("b", drain_spec_text("Second"))
        self.spec("c", drain_spec_text("Third", "done"))

        def run(wf):
            os.remove(self.path("c"))
            return approved(PR_A)(wf)
        status, out, err = self.drain(run, approved(PR_B))

        self.assertEqual(status, 0)
        self.assertEqual(err.count("c: "), 1, err)
        self.assertIn(f"c: {self.gone('c')}; gone\n", err)
        self.assertNotIn("not running it", err)
        self.assertTrue(out.endswith("all specs cleared\n"), out)

    def test_a_spec_emptied_then_restored_is_run(self):
        self.spec("a", drain_spec_text("First"))
        self.spec("b", drain_spec_text("Second"))
        self.spec("c", drain_spec_text("Third"))

        def emptying_b(wf):
            self.spec("b", "")
            return approved(PR_A)(wf)

        def restoring_b(wf):
            self.spec("b", drain_spec_text("Second"))
            return approved(PR_B)(wf)
        status, out, err = self.drain(emptying_b, restoring_b, approved(PR_B))

        self.assertEqual(status, 0)
        self.assertEqual([state.task for state, _ in self.runs], ["First", "Third", "Second"])
        self.assertEqual(err.count(f"b: spec file {self.path('b')} is empty; not running it\n"), 1, err)
        self.assertEqual(self.header("b")[::2], ("in-review", PR_B))
        self.assertTrue(out.endswith(f"b: ready-for-agent -> in-review  {PR_B}\nc: ready-for-agent -> in-review  "
                                     f"{PR_B}\nall specs cleared\n"), out)

    def test_a_gone_spec_change_line_ends_gone(self):
        spec = orchestrator.QueuedSpec("a", orchestrator.SpecFile(self.path("a"), drain_spec_text("A")), [],
                                       "ready-for-agent")
        spec.update({"Status": "in-review", "PR": PR_A}, write=False)
        self.assertFalse(spec.reread())
        self.assertEqual(spec.change_line(), "a: ready-for-agent -> gone")

    def test_a_blocked_by_line_naming_no_blocker(self):
        for line in ("Blocked by: -", "Blocked by: none", "Blocked by: None"):
            with self.subTest(line=line):
                self.runs.clear()
                self.spec("a", drain_spec_text("First", "ready-for-agent", line))
                status, _, _ = self.drain(approved())
                self.assertEqual((status, len(self.runs)), (0, 1))

    def test_a_run_taken_over_leaves_its_spec_for_the_next_drain(self):
        self.spec("a", drain_spec_text("First"))
        status, _, err = self.drain(raising(orchestrator.RunTakenOver("run x was taken over by host 1")))
        self.assertEqual(status, orchestrator.EXIT_ERROR)
        self.assertEqual(self.header("a")[:2], ("ready-for-agent", self.runs[0][0].run_id))
        self.assertIn("a stays ready-for-agent; run drain again once that run has ended", err)
        self.assertNotIn("resume its run", err)

    def test_a_symlinked_spec_stays_a_link_and_keeps_its_mode(self):
        target = os.path.join(self.dir, "elsewhere.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write(drain_spec_text("First"))
        os.chmod(target, 0o600)
        os.makedirs(os.path.join(self.dir, "a"))
        os.symlink(target, self.path("a"))

        self.drain(approved(PR_A))

        self.assertTrue(os.path.islink(self.path("a")))
        self.assertEqual(self.header("a")[::2], ("in-review", PR_A))
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

    def test_a_blocked_spec_waits_for_its_blockers_pull_request(self):
        self.spec("a", drain_spec_text("First"))
        self.spec("b", drain_spec_text("Second", "ready-for-agent", "Blocked by: a"))

        status, out, _ = self.drain(approved(PR_A))

        self.assertEqual(status, orchestrator.EXIT_WAITING)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.header("a")[::2], ("in-review", PR_A))
        self.assertEqual(self.header("b"), ("ready-for-agent", None, None))
        self.assertTrue(out.endswith(f"b waits on:\n    a: in-review  {PR_A}\n"), out)

    def test_the_next_drain_follows_the_blockers_pull_request(self):
        cases = [
            ("MERGED", [approved(PR_B)], 0, "done", "in-review"),
            ("OPEN", [], orchestrator.EXIT_WAITING, "in-review", "ready-for-agent"),
            ("CLOSED", [], orchestrator.EXIT_WAITING, "ready-for-human", "ready-for-agent"),
        ]
        for pr_state, outcomes, exit_code, a_after, b_after in cases:
            with self.subTest(pr_state=pr_state):
                self.runs.clear()
                self.spec("a", drain_spec_text("First", "in-review", "Run: 20261024-100000-aaaaaa", f"PR: {PR_A}"))
                self.spec("b", drain_spec_text("Second", "ready-for-agent", "Blocked by: a/spec.md"))
                self.host.pull_requests[PR_A] = {"state": pr_state, "headRefOid": "a" * 40, "closedAt": None}

                status, out, _ = self.drain(*outcomes)

                self.assertEqual(status, exit_code)
                self.assertEqual((self.header("a")[0], self.header("b")[0]), (a_after, b_after))
                self.assertEqual(len(self.runs), len(outcomes))
                if pr_state == "OPEN":
                    self.assertNotIn("->", out)
                else:
                    self.assertIn(f"a: in-review -> {a_after}  {PR_A}\n", out)

    def test_a_run_that_does_not_succeed_stops_the_drain(self):
        cases = [
            (ended(CHANGES_REQUESTED), orchestrator.EXIT_CHANGES_REQUESTED),
            (ended(orchestrator.QUALITY_GATE_FAILED), orchestrator.EXIT_CHANGES_REQUESTED),
            (approved(conflicts=["x.py"]), orchestrator.EXIT_CONFLICT),
        ]
        for outcome, exit_code in cases:
            with self.subTest(exit_code=exit_code):
                self.runs.clear()
                self.spec("a", drain_spec_text("First"))
                self.spec("b", drain_spec_text("Second"))

                status, out, _ = self.drain(outcome)

                self.assertEqual(status, exit_code)
                self.assertEqual(self.header("a"), ("ready-for-human", self.runs[0][0].run_id, PR_A))
                self.assertEqual(self.header("b"), ("ready-for-agent", None, None))
                self.assertIn(f"a: ready-for-agent -> ready-for-human  {PR_A}\n", out)
                self.assertNotIn("all specs cleared", out)

    def test_a_failed_run_stops_the_drain_with_its_resume_command(self):
        self.spec("a", drain_spec_text("First"))
        self.spec("b", drain_spec_text("Second"))

        status, out, err = self.drain(raising(OrchestratorError("the Builder's pane is gone")))

        run_id = self.runs[0][0].run_id
        self.assertEqual(status, orchestrator.EXIT_ERROR)
        self.assertEqual(self.header("a"), ("ready-for-human", run_id, None))
        self.assertEqual(self.header("b"), ("ready-for-agent", None, None))
        self.assertIn("error: the Builder's pane is gone\n", err)
        self.assertIn(f"orchestrator.py resume {run_id.rsplit('-', 1)[-1]}", err)
        self.assertEqual(out, "a: ready-for-agent -> ready-for-human\n")

    def test_an_interrupted_run_keeps_its_spec_ready_with_its_run(self):
        self.spec("a", drain_spec_text("First"))

        status, out, err = self.drain(raising(KeyboardInterrupt()), argv=["--machine", "m", "--cwd", "~/p"])

        run_id = self.runs[0][0].run_id
        self.assertEqual(status, orchestrator.EXIT_INTERRUPTED)
        self.assertEqual(self.header("a"), ("ready-for-agent", run_id, None))
        self.assertIn(f"resume with: orchestrator.py resume {run_id.rsplit('-', 1)[-1]} --machine m --cwd '~/p'",
                      err)

    def test_a_finished_run_on_the_run_line_is_taken_up_without_a_new_run(self):
        cases = [
            ({"verdict": APPROVE, "pr_url": PR_A}, ("in-review", PR_A)),
            ({"verdict": orchestrator.FINISHED, "pr_url": PR_A}, ("in-review", PR_A)),
            ({"verdict": APPROVE, "pr_url": PR_A, "conflicts": ["x.py"]}, ("ready-for-human", PR_A)),
            ({"verdict": CHANGES_REQUESTED, "pr_url": PR_A}, ("ready-for-human", PR_A)),
            ({"verdict": CHANGES_REQUESTED}, ("ready-for-human", None)),
        ]
        for fields, (spec_after, pr) in cases:
            with self.subTest(fields=fields):
                self.spec("a", drain_spec_text("First", "ready-for-agent", "Run: 20261024-100000-aaaaaa"))
                self.saved_run("a", "20261024-100000-aaaaaa", 10**4, phase="done", **fields)

                status, out, _ = self.drain()

                self.assertEqual(self.header("a"), (spec_after, "20261024-100000-aaaaaa", pr))
                self.assertEqual(status, 0)
                self.assertIn(f"a: ready-for-agent -> {spec_after}", out)

    def conflicted_spec(self, status="ready-for-human", verdict=APPROVE, **pr):
        """Spec a, of status, whose run with verdict opened PR_A over a conflict in x.py, and what gh says of PR_A:
        OPEN, a draft and MERGEABLE unless pr says otherwise, its body the warning and a line."""
        run_id = "20261024-100000-aaaaaa"
        self.spec("a", drain_spec_text("First", status, f"Run: {run_id}", f"PR: {PR_A}"))
        self.saved_run("a", run_id, 10**4, phase="done", verdict=verdict, pr_url=PR_A, base_branch="main",
                       conflicts=["x.py"])
        body = f"{orchestrator.conflict_warning('main', ['x.py'])}\n\nOpened by run {run_id}.\n"
        self.host.pull_requests[PR_A] = {"state": "OPEN", "isDraft": True, "mergeable": "MERGEABLE", "body": body,
                                         "headRefOid": "a" * 40, "closedAt": None, **pr}
        return run_id

    def run_conflicts(self, run_id):
        return json.loads(self.host.files[f"/proj/.orchestrator/runs/{run_id}/state.json"])["conflicts"]

    def test_a_spec_for_the_human_only_for_its_conflict_goes_to_review_once_refresh_clears_it(self):
        run_id = self.conflicted_spec()
        status, out, err = self.drain()
        self.assertEqual(status, 0)
        self.assertEqual(self.header("a"), ("in-review", run_id, PR_A))
        self.assertIn(f"a: ready-for-human -> in-review  {PR_A}\n", out)
        self.assertIn(f"{run_id}: conflict cleared, ready for review  {PR_A}\n", err)
        self.assertEqual(self.host.pr_edits, [("body", PR_A, f"Opened by run {run_id}.\n"), ("ready", PR_A)])
        self.assertEqual(self.run_conflicts(run_id), [])

    def test_a_spec_for_the_human_stays_when_its_run_did_not_succeed_or_still_conflicts(self):
        for verdict, pr, edits in ((CHANGES_REQUESTED, {}, ["body"]), (APPROVE, {"mergeable": "CONFLICTING"}, []),
                                   (APPROVE, {"state": "CLOSED"}, [])):
            with self.subTest(verdict=verdict, pr=pr):
                self.setUp()
                run_id = self.conflicted_spec(verdict=verdict, **pr)
                status, out, _ = self.drain()
                self.assertEqual(status, 0)
                self.assertEqual(self.header("a"), ("ready-for-human", run_id, PR_A))
                self.assertNotIn("a: ", out)
                self.assertEqual([e[0] for e in self.host.pr_edits], edits)

    def test_an_in_review_specs_run_is_refreshed_too(self):
        run_id = self.conflicted_spec("in-review")
        status, out, _ = self.drain()
        self.assertEqual(status, 0)
        self.assertEqual(self.header("a"), ("in-review", run_id, PR_A))
        self.assertEqual(self.run_conflicts(run_id), [])
        self.assertEqual(out, "all specs cleared\n")

    def test_a_dry_run_shows_the_move_and_refreshes_nothing(self):
        run_id = self.conflicted_spec()
        before = self.text("a")
        status, out, err = self.drain(argv=["--dry-run"])
        self.assertEqual(status, 0)
        self.assertIn(f"a: ready-for-human -> in-review  {PR_A}\n", out)
        self.assertNotIn("conflict cleared", err)
        self.assertEqual((self.text("a"), self.host.pr_edits, self.run_conflicts(run_id)), (before, [], ["x.py"]))

    def test_an_unfinished_run_on_the_run_line_stops_the_drain_before_any_run(self):
        run_id = "20261024-100000-aaaaaa"
        stale = HEARTBEAT_SECONDS + STALE_SECONDS
        cases = [
            ({"owner": {"host": "elsewhere", "pid": 1}}, 0, "is running (pid 1 on elsewhere, beat 0s ago)",
             "wait for it to finish, or remove the Run: line"),
            ({}, stale, "is stale (no heartbeat for", "resume it with `orchestrator.py resume aaaaaa`"),
            ({"error": "interrupted"}, stale, "failed: interrupted", "resume it with `orchestrator.py resume aaaaaa`"),
            ({"closed": "workspace closed"}, stale, "was closed (workspace closed)", "; remove the Run: line from"),
            (None, 0, "is not under /proj/.orchestrator/runs", "; remove the Run: line from"),
        ]
        for fields, age, why, remedy in cases:
            with self.subTest(why=why):
                self.host.files.clear()
                self.spec("a", drain_spec_text("First", "ready-for-agent", f"Run: {run_id}"))
                self.spec("b", drain_spec_text("Second"))
                if fields is not None:
                    self.saved_run("a", run_id, age, phase="build", **fields)
                before = self.text("a")

                status, out, err = self.drain()

                self.assertEqual(status, orchestrator.EXIT_ERROR)
                self.assertTrue(err.startswith(f"error: spec a: run {run_id}, on its Run: line, {why}"), err)
                self.assertIn(remedy, err)
                self.assertIn(f"{self.path('a')} to start the spec over", err)
                self.assertEqual((self.text("a"), self.header("b")), (before, ("ready-for-agent", None, None)))
                self.assertEqual(out, "")
                self.sweep.assert_not_called()

    def test_a_refresh_that_fails_changes_no_spec(self):
        self.spec("a", drain_spec_text("First", "in-review", f"PR: {PR_A}"))
        self.spec("b", drain_spec_text("Second", "ready-for-agent", "Run: 20261024-100000-bbbbbb"))
        self.host.pull_requests[PR_A] = {"state": "MERGED", "headRefOid": "a" * 40, "closedAt": None}
        before = self.text("a")

        status, _, err = self.drain()

        self.assertEqual(status, orchestrator.EXIT_ERROR)
        self.assertIn("error: spec b: run 20261024-100000-bbbbbb", err)
        self.assertEqual(self.text("a"), before)

    def test_bad_blockers_stop_the_drain_before_any_run(self):
        cases = [
            ({"a": "Blocked by: nosuch"}, "error: spec a is blocked by nosuch, which has no spec.md in"),
            ({"a": "Blocked by: b", "b": "Blocked by: c", "c": "Blocked by: b"},
             "error: the specs' blockers form a cycle: b -> c -> b\n"),
            ({"a": "Blocked by: a"}, "error: the specs' blockers form a cycle: a -> a\n"),
        ]
        for blockers, message in cases:
            with self.subTest(blockers=blockers):
                shutil.rmtree(self.dir)
                os.mkdir(self.dir)
                for slug in "abc":
                    header = [blockers[slug]] if slug in blockers else []
                    self.spec(slug, drain_spec_text(slug.upper(), "ready-for-agent", *header))

                status, out, err = self.drain()

                self.assertEqual(status, orchestrator.EXIT_ERROR)
                self.assertTrue(err.startswith(message), err)
                self.assertEqual(out, "")
                self.sweep.assert_not_called()

    def test_specs_with_any_other_status_are_left_alone(self):
        texts = {
            "a": drain_spec_text("A", None),
            "b": drain_spec_text("B", "needs-triage"),
            "c": drain_spec_text("C", "ready-for-human", f"PR: {PR_A}"),
            "d": drain_spec_text("D", "wontfix", "Run: 20261024-100000-dddddd"),
            "e": drain_spec_text("E", "in-review"),
            "f": drain_spec_text("F", "done"),
        }
        for slug, text in texts.items():
            self.spec(slug, text)
        os.makedirs(os.path.join(self.dir, "no-spec", "issues"))

        status, out, _ = self.drain()

        self.assertEqual((status, out), (0, "all specs cleared\n"))
        self.assertEqual({slug: self.text(slug) for slug in texts}, texts)
        self.assertEqual(self.host.gh_calls, [])

    def test_only_the_header_lines_drain_owns_change(self):
        for nl in ("\n", "\r\n"):
            with self.subTest(nl=nl):
                self.runs.clear()
                header = ["Owner: me", "Status:   ready-for-agent  ", "Blocked by:", "Run: stale"]
                text = drain_spec_text("First", None, *header, "## Notes", "PR: not in the header", nl=nl)
                self.spec("a", text)
                self.saved_run("a", "stale", phase="done", verdict=APPROVE, pr_url=PR_A)

                self.assertEqual(self.drain()[0], 0)

                want = drain_spec_text("First", None, "Owner: me", "Status: in-review", f"PR: {PR_A}", "Blocked by:",
                                       "Run: stale", "## Notes", "PR: not in the header", nl=nl)
                self.assertEqual(self.text("a"), want)

    def test_added_lines_go_directly_below_the_status_line(self):
        self.spec("a", "# First\r\nOwner: me\r\nStatus: ready-for-agent\r\nNote: x\r\n")
        self.drain(approved())

        run_id = self.runs[0][0].run_id
        self.assertEqual(self.text("a"),
                         f"# First\r\nOwner: me\r\nStatus: in-review\r\nPR: {PR_A}\r\nRun: {run_id}\r\nNote: x\r\n")

    def test_a_after_line_that_ends_the_file(self):
        self.assertEqual(orchestrator.with_header_field("# T\nStatus: done", "PR", PR_A),
                         f"# T\nStatus: done\nPR: {PR_A}")
        with self.assertRaisesRegex(OrchestratorError, "the spec has no Status: line to add PR: below"):
            orchestrator.with_header_field("# T\n## Status: done\n", "PR", PR_A)

    def test_a_spec_that_cannot_be_written_stops_the_drain_and_keeps_the_old_file(self):
        self.spec("a", drain_spec_text("First"))
        before = self.text("a")

        # Fails as a full disk would at the rename, once the new text is written aside; root ignores a chmod.
        with patch("os.replace", side_effect=OSError(28, "No space left on device")):
            status, _, err = self.drain()

        self.assertEqual(status, orchestrator.EXIT_ERROR)
        self.assertIn(f"error: cannot write {self.path('a')}: [Errno 28] No space left on device", err)
        self.assertEqual((self.text("a"), os.listdir(os.path.join(self.dir, "a"))), (before, ["spec.md"]))
        self.assertEqual(self.runs, [])

    def test_dry_run_changes_nothing_and_says_what_it_would_do(self):
        self.spec("a", drain_spec_text("A", "in-review", f"PR: {PR_A}"))
        self.spec("b", drain_spec_text("B", "ready-for-agent", "Blocked by: a"))
        self.spec("c", drain_spec_text("C", "ready-for-agent", "Run: 20261024-100000-cccccc"))
        self.spec("d", drain_spec_text("D"))
        self.spec("e", drain_spec_text("E", "ready-for-agent", "Blocked by: d"))
        self.host.pull_requests[PR_A] = {"state": "MERGED", "headRefOid": "a" * 40, "closedAt": None}
        self.saved_run("c", "20261024-100000-cccccc", phase="done", verdict=APPROVE, pr_url=PR_B)
        before = {slug: self.text(slug) for slug in "abcde"}

        status, out, _ = self.drain(argv=["--dry-run"])

        self.assertEqual(status, 0)
        self.assertEqual({slug: self.text(slug) for slug in "abcde"}, before)
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "a"))), ["spec.md"])
        self.assertEqual(out, f"a: in-review -> done  {PR_A}\nc: ready-for-agent -> in-review  {PR_B}\n"
                              "would run, in order: b, d\n")
        self.sweep.assert_not_called()

    def test_drain_takes_no_task_spec_or_no_pr(self):
        for argv in (["--no-pr"], ["task", "--spec", "spec.md"], ["--spec", "spec.md"], ["a", "b"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as exit_, patch("sys.stderr", new_callable=io.StringIO):
                    parse_args(["drain", *argv])
                self.assertEqual(exit_.exception.code, 2)
        self.assertEqual(parse_args(["drain", "--spec-model", "opus"]).spec_model, "opus")
        self.assertEqual(parse_args(["drain"]).dir, ".scratch")

    @patch.dict("os.environ", {"XDG_CONFIG_HOME": "/nonexistent"})
    def test_the_flags_reach_every_run(self):
        example = f"{EXAMPLES}/spec-build-review.toml"
        self.spec("a", drain_spec_text("A"))
        self.spec("b", drain_spec_text("B"))
        argv = ["--workflow-file", example, "--worktree", "--quality-gate", JOB, "--max-quality-rounds", "2",
                "--max-rounds", "5", "--timeout", "60", "--permission-mode", "auto", "--model", "m",
                "--review-model", "r", "--agent", "codex"]

        self.assertEqual(self.drain(approved(), approved(), argv=argv)[0], 0)

        for state, _ in self.runs:
            self.assertEqual((state.workflow, state.quality_job, state.max_quality_rounds, state.max_rounds,
                              state.turn_timeout, state.permission_mode),
                             ("spec-build-review", JOB, 2, 5, 60, "auto"))
            self.assertEqual(state.worktree, f"/proj/{orchestrator.WORKTREES_DIR}/{state.key}")
            self.assertEqual(state.models, {"spec": "m", "build": "m", "review": "r"})
            self.assertEqual(set(state.agent_kinds.values()), {"codex"})
        self.assertEqual(len(self.runs), 2)

    def test_a_workflow_that_cannot_take_a_spec_fails_before_any_run(self):
        self.spec("a", drain_spec_text("A"))

        status, _, err = self.drain(argv=["--workflow-file", f"{EXAMPLES}/quick.toml"])

        self.assertEqual(status, orchestrator.EXIT_ERROR)
        self.assertIn("--spec needs a first step that writes the spec", err)
        self.sweep.assert_not_called()
        self.assertEqual(self.header("a"), ("ready-for-agent", None, None))

    def test_a_relative_dir_is_the_shells(self):
        self.spec("a", drain_spec_text("A"))
        here = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, here)
        with patch.object(Workflow, "run", autospec=True, side_effect=approved()), patch("sys.stdout"):
            self.assertEqual(main(["drain", ".", "--cwd", "/elsewhere"]), 0)
        self.assertEqual(self.header("a")[0], "in-review")

    def test_a_missing_dir_is_an_error(self):
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["drain", os.path.join(self.dir, "nosuch")]), orchestrator.EXIT_ERROR)
        self.assertIn("error: cannot list the specs in", err.getvalue())


@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestDrainWorkflows(unittest.TestCase):
    """drain driving real Workflows through FakeHerdr and FakeHost, from spec to pull request."""

    def test_two_specs_end_as_two_pull_requests(self):
        without_workflow_files(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for slug in ("a", "b"):
            os.mkdir(os.path.join(tmp.name, slug))
            with open(os.path.join(tmp.name, slug, "spec.md"), "w") as f:
                f.write(drain_spec_text(f"Spec {slug}"))
        host = FakeHost()
        herdr = FakeHerdr(host, None, {"build": [build_turn(1), build_turn(1)],
                                       "review": [review_turn(1, APPROVE), review_turn(1, APPROVE)]})
        clock = FakeClock()
        host.clock = clock
        init = Workflow.__init__

        def on_fakes(wf, herdr, host, state, **kw):
            herdr.state = state  # the run FakeHerdr's turns play
            init(wf, herdr, host, state, clocks=orchestrator.Clocks(clock.sleep, clock, clock), **kw)
        with patch.object(orchestrator, "connect", return_value=(herdr, host)), \
                patch.object(Host, "resolve_dir", return_value="/proj"), \
                patch.object(Workflow, "__init__", autospec=True, side_effect=on_fakes), \
                patch.object(orchestrator, "notify_locally"), \
                patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr"):
            self.assertEqual(main(["drain", tmp.name]), 0)

        self.assertEqual([pr["title"] for pr in host.prs], ["Spec a", "Spec b"])
        self.assertTrue(out.getvalue().endswith("all specs cleared\n"))
        for slug in ("a", "b"):
            with open(os.path.join(tmp.name, slug, "spec.md")) as f:
                text = f.read()
            self.assertEqual(orchestrator.header_field(text, "Status"), "in-review")
            self.assertEqual(orchestrator.header_field(text, "PR"), "https://github.com/o/r/pull/7")


# ---------------------------------------------------------------------------
# Pruning run branches
# ---------------------------------------------------------------------------

PR_HEAD = "1" * 40
OLDER = "2" * 40
LATER = "3" * 40
NOW = 1_791_500_000.0  # 2026-10-09


def iso(seconds):
    return orchestrator.datetime.fromtimestamp(seconds, orchestrator.timezone.utc).isoformat().replace("+00:00", "Z")


def merged(head=PR_HEAD):
    return {"state": "MERGED", "headRefOid": head, "closedAt": iso(NOW - 86400)}


def closed(days_ago):
    return {"state": "CLOSED", "headRefOid": PR_HEAD, "closedAt": iso(NOW - days_ago * 86400)}


OPEN = {"state": "OPEN", "headRefOid": PR_HEAD, "closedAt": None}


def deleting_calls(host):
    """The git commands host ran that delete a branch or a ref."""
    return [c for c in host.git_calls if c[:1] == ("update-ref",) or c[:2] == ("branch", "--quiet") or "--delete" in c]


class PruneTest(unittest.TestCase):
    """Runs in /proj on a FakeHost, each with a pull request #<n> on branch orchestrator/task-<key>."""

    def setUp(self):
        self.host = FakeHost()

    def add_run(self, key, pr, *, n=7, local=PR_HEAD, remote=PR_HEAD, branch=None, **kw):
        """A finished run with key whose PR gh reports as pr; local and remote are where its branch is, None
        for nowhere. Returns its run id."""
        run_id = f"20261001-120000-{key}"
        branch = branch or f"orchestrator/task-{key}"
        url = f"https://github.com/o/r/pull/{n}"
        saved = {**asdict(RunState(run_id, "a task", "/proj", None, phase="done", verdict=APPROVE,
                                   pull_request=True, base_branch="main", branch=branch, pr_url=url)), **kw}
        self.host.files[self.state_path(run_id)] = json.dumps(saved) + "\n"
        self.host.pull_requests[url] = pr
        if local:
            self.host.branches[branch] = local
        if remote:
            self.host.remote[branch] = remote
        return run_id

    @staticmethod
    def state_path(run_id):
        return f"/proj/.orchestrator/runs/{run_id}/state.json"

    def saved(self, run_id):
        return json.loads(self.host.files[self.state_path(run_id)])

    def prune(self, **kw):
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            pruned = orchestrator.prune_branches(self.host, "/proj", lambda: NOW, "here", lambda pid: False, **kw)
        self.log = err.getvalue()
        return {run.run_id: run for run in pruned}


class TestPrune(PruneTest):
    def test_merged_at_the_pull_requests_head(self):
        run_id = self.add_run("aaaaaa", merged())
        self.host.tracking.add("orchestrator/task-aaaaaa")
        run = self.prune()[run_id]
        self.assertEqual((run.local, run.remote, run.cleanup), ("deleted", "deleted", "deleted"))
        self.assertEqual((self.host.branches, self.host.remote, self.host.tracking), ({}, {}, set()))
        self.assertEqual(self.saved(run_id)["branch_cleanup"], "deleted")
        self.assertEqual(self.host.mtime_kept, [self.state_path(run_id)])
        self.assertIn(("push", "--quiet", f"--force-with-lease=refs/heads/orchestrator/task-aaaaaa:{PR_HEAD}",
                       "origin", "--delete", "refs/heads/orchestrator/task-aaaaaa"), self.host.git_calls)
        self.assertEqual(self.host.gh_calls,
                         [("pr", "view", "https://github.com/o/r/pull/7", "--json", "state,headRefOid,closedAt")])

    def test_local_behind_the_pull_requests_head_is_deleted(self):
        run_id = self.add_run("aaaaaa", merged(), local=OLDER)
        self.host.ancestors.add((OLDER, PR_HEAD))
        run = self.prune()[run_id]
        self.assertEqual((run.local, run.cleanup), ("deleted", "deleted"))
        self.assertIn(("branch", "--quiet", "-D", "orchestrator/task-aaaaaa"), self.host.git_calls)

    def test_local_ahead_of_the_pull_requests_head_is_kept(self):
        run_id = self.add_run("aaaaaa", merged(), local=LATER)
        self.host.ahead["orchestrator/task-aaaaaa"] = 3
        run = self.prune()[run_id]
        self.assertEqual((run.local, run.remote), ("kept: local has 3 commits beyond PR #7", "deleted"))
        self.assertEqual(self.host.branches, {"orchestrator/task-aaaaaa": LATER})
        self.assertEqual(self.host.remote, {})
        self.assertEqual(self.saved(run_id)["branch_cleanup"], "kept: local has 3 commits beyond PR #7")
        self.assertIn(("rev-list", "--count", f"{PR_HEAD}..refs/heads/orchestrator/task-aaaaaa"), self.host.git_calls)

    def test_one_commit_beyond(self):
        run_id = self.add_run("aaaaaa", merged(), local=LATER)
        self.host.ahead["orchestrator/task-aaaaaa"] = 1
        self.assertEqual(self.prune()[run_id].local, "kept: local has 1 commit beyond PR #7")

    def test_remote_moved_past_the_pull_requests_head_is_kept(self):
        run_id = self.add_run("aaaaaa", merged(), remote=LATER)
        run = self.prune()[run_id]
        self.assertEqual((run.local, run.remote), ("deleted", "kept: remote has commits beyond PR #7"))
        self.assertEqual(self.host.remote, {"orchestrator/task-aaaaaa": LATER})
        self.assertEqual(self.saved(run_id)["branch_cleanup"], "kept: remote has commits beyond PR #7")
        self.assertFalse([c for c in self.host.git_calls if "--delete" in c])

    def test_remote_already_gone_drops_the_stale_tracking_ref(self):
        run_id = self.add_run("aaaaaa", merged(), remote=None)
        self.host.tracking.add("orchestrator/task-aaaaaa")
        run = self.prune()[run_id]
        self.assertEqual((run.local, run.remote, run.cleanup), ("deleted", "gone", "deleted"))
        self.assertEqual(self.host.tracking, set())
        self.assertIn(("update-ref", "-d", "refs/remotes/origin/orchestrator/task-aaaaaa"), self.host.git_calls)

    def test_checked_out_branch_is_kept(self):
        for where in ("/proj", "/proj-wt"):
            with self.subTest(where):
                self.setUp()
                run_id = self.add_run("aaaaaa", merged())
                if where == "/proj":
                    self.host.branch = "orchestrator/task-aaaaaa"
                else:
                    self.host.worktrees[where] = "orchestrator/task-aaaaaa"
                run = self.prune()[run_id]
                self.assertEqual((run.local, run.remote), (f"kept: checked out in {where}", "deleted"))
                self.assertIn("orchestrator/task-aaaaaa", self.host.branches)
                self.assertEqual(self.saved(run_id)["branch_cleanup"], f"kept: checked out in {where}")

    def test_both_sides_kept_records_both_reasons(self):
        run_id = self.add_run("aaaaaa", merged(), local=LATER, remote=LATER)
        self.host.ahead["orchestrator/task-aaaaaa"] = 2
        self.prune()
        self.assertEqual(self.saved(run_id)["branch_cleanup"],
                         "kept: local has 2 commits beyond PR #7; remote has commits beyond PR #7")

    def test_pull_request_head_missing_from_the_clone_keeps_local(self):
        run_id = self.add_run("aaaaaa", merged(), local=OLDER)
        self.host.missing_commits.add(PR_HEAD)
        run = self.prune()[run_id]
        self.assertEqual(run.local, f"kept: PR #7's head {PR_HEAD[:12]} is not in this clone; fetch it to decide")
        self.assertIn("orchestrator/task-aaaaaa", self.host.branches)

    def test_pull_requests_not_done_are_skipped(self):
        cases = {"closed 13 days ago": (closed(13), "PR #7 was closed less than 14 days ago"),
                 "open": (OPEN, "PR #7 is open")}
        for name, (pr, why) in cases.items():
            with self.subTest(name):
                self.setUp()
                run_id = self.add_run("aaaaaa", pr)
                before = self.host.files[self.state_path(run_id)]
                run = self.prune()[run_id]
                self.assertEqual(run.skipped, why)
                self.assertIsNone(run.cleanup)
                self.assertEqual(deleting_calls(self.host), [])
                self.assertEqual(self.host.writes, [])
                self.assertEqual(self.host.files[self.state_path(run_id)], before)
                self.assertEqual(run.line, f"{run_id}  orchestrator/task-aaaaaa  skipped: {why}")

    def test_closed_15_days_ago_is_deleted(self):
        run_id = self.add_run("aaaaaa", closed(15))
        run = self.prune()[run_id]
        self.assertEqual((run.local, run.remote, run.cleanup), ("deleted", "deleted", "deleted"))
        self.assertEqual((self.host.branches, self.host.remote), ({}, {}))

    def test_running_run_is_skipped(self):
        # Saved a moment ago by an orchestrator on another host: RUNNING, whatever its pid.
        self.add_run("aaaaaa", merged(), phase="build", verdict=None,
                     owner={"host": "elsewhere", "pid": 1, "started_at": "x"})
        self.assertEqual(self.prune(), {})
        self.assertEqual((self.host.gh_calls, deleting_calls(self.host)), ([], []))

    def test_stale_run_is_a_candidate_and_keeps_its_age(self):
        run_id = self.add_run("aaaaaa", merged(), phase="publish", verdict=None)
        self.host.ages[run_id] = STALE_SECONDS + 1
        self.assertEqual(self.prune()[run_id].cleanup, "deleted")
        self.assertEqual(self.host.mtime_kept, [self.state_path(run_id)])

    def test_branch_without_the_prefix_is_never_touched(self):
        self.add_run("aaaaaa", merged(), branch="feature/rate-limiter")
        self.add_run("bbbbbb", merged(), branch="main")
        self.assertEqual(self.prune(), {})
        self.assertEqual((self.host.gh_calls, deleting_calls(self.host)), ([], []))
        self.assertEqual(set(self.host.branches), {"feature/rate-limiter", "main"})

    def test_runs_without_a_pull_request_are_never_touched(self):
        run_id = self.add_run("aaaaaa", merged())
        saved = self.saved(run_id)
        saved["pr_url"] = None
        self.host.files[self.state_path(run_id)] = json.dumps(saved)
        self.assertEqual(self.prune(), {})
        self.assertEqual(self.host.gh_calls, [])

    def test_recorded_runs(self):
        done = self.add_run("aaaaaa", merged(), branch_cleanup="deleted")
        kept = self.add_run("bbbbbb", merged(), n=8, branch_cleanup="kept: checked out in /proj")
        self.assertEqual(self.prune(), {})
        self.assertEqual(self.host.gh_calls, [])

        pruned = self.prune(revisit=True)
        self.assertEqual(list(pruned), [kept])
        self.assertEqual(self.saved(kept)["branch_cleanup"], "deleted")
        self.assertEqual(self.saved(done)["branch_cleanup"], "deleted")

    def test_saved_before_prune_existed(self):
        run_id = self.add_run("aaaaaa", merged())
        saved = self.saved(run_id)
        del saved["branch_cleanup"]
        self.host.files[self.state_path(run_id)] = json.dumps(saved)
        self.assertIsNone(RunState.from_dict(saved).branch_cleanup)
        self.assertEqual(self.prune()[run_id].cleanup, "deleted")
        self.assertEqual(self.saved(run_id), {**saved, "branch_cleanup": "deleted"})

    def test_dry_run_deletes_and_records_nothing(self):
        run_id = self.add_run("aaaaaa", merged(), remote=None)
        self.host.tracking.add("orchestrator/task-aaaaaa")
        kept = self.add_run("bbbbbb", merged(), n=8, remote=LATER)
        pruned = self.prune(dry_run=True)
        self.assertEqual((pruned[run_id].local, pruned[run_id].remote), ("would be deleted", "gone"))
        self.assertEqual(pruned[kept].remote, "kept: remote has commits beyond PR #8")
        self.assertEqual((deleting_calls(self.host), self.host.writes), ([], []))
        self.assertEqual(len(self.host.branches), 2)
        self.assertEqual(self.host.tracking, {"orchestrator/task-aaaaaa"})

    def test_gh_failure_on_one_run_skips_only_that_run(self):
        failing = self.add_run("aaaaaa", completed(stderr="HTTP 502: Bad Gateway", returncode=1))
        fine = self.add_run("bbbbbb", merged(), n=8)
        pruned = self.prune()
        self.assertEqual(pruned[failing].skipped,
                         "failed: gh pr view https://github.com/o/r/pull/7 --json state,headRefOid,closedAt "
                         "failed: HTTP 502: Bad Gateway")
        self.assertIn(f"could not prune the branch of run {failing}: gh pr view", self.log)
        self.assertIsNone(self.saved(failing)["branch_cleanup"])
        self.assertIn("orchestrator/task-aaaaaa", self.host.branches)
        self.assertEqual(pruned[fine].cleanup, "deleted")

    def test_rejected_lease_skips_the_run(self):
        run_id = self.add_run("aaaaaa", merged())
        lease = self.host.git_run

        def moved_meanwhile(cwd, *args, **kw):
            if args[:1] == ("push",):
                self.host.remote["orchestrator/task-aaaaaa"] = LATER
            return lease(cwd, *args, **kw)
        self.host.git_run = moved_meanwhile
        run = self.prune()[run_id]
        self.assertIn("stale info", run.skipped)
        self.assertIn(f"could not prune the branch of run {run_id}", self.log)
        self.assertIsNone(self.saved(run_id)["branch_cleanup"])

    def test_unusable_gh_stops_the_sweep_after_one_line(self):
        for status, stderr in ((127, "sh: 1: exec: gh: not found"),
                               (4, "To get started with GitHub CLI, please run:  gh auth login")):
            with self.subTest(status):
                self.setUp()
                self.add_run("aaaaaa", completed(stderr=stderr, returncode=status))
                self.add_run("bbbbbb", completed(stderr=stderr, returncode=status), n=8)
                self.assertEqual(self.prune(), {})
                self.assertEqual(len(self.host.gh_calls), 1)
                self.assertEqual(self.log.count("\n"), 1)
                self.assertIn(f"stopped pruning run branches: gh pr view https://github.com/o/r/pull/7 --json "
                              f"state,headRefOid,closedAt failed with status {status}: {stderr}", self.log)
                self.assertEqual(deleting_calls(self.host), [])

    def test_unreadable_state_skips_the_run(self):
        run_id = self.add_run("aaaaaa", merged())
        saved = self.saved(run_id)
        del saved["task"]
        self.host.files[self.state_path(run_id)] = json.dumps(saved)
        self.assertIn("is incomplete", self.prune()[run_id].skipped)
        self.assertEqual(deleting_calls(self.host), [])

    def test_state_saved_meanwhile_is_not_overwritten(self):
        run_id = self.add_run("aaaaaa", merged())
        view = self.host.gh_run

        def resumed_meanwhile(cwd, *args):
            self.host.files[self.state_path(run_id)] = json.dumps({**self.saved(run_id), "phase": "done", "round": 2})
            return view(cwd, *args)
        self.host.gh_run = resumed_meanwhile
        self.assertIn("changed while its branch was pruned", self.prune()[run_id].skipped)
        self.assertEqual(self.saved(run_id)["round"], 2)
        self.assertIsNone(self.saved(run_id)["branch_cleanup"])


class TestHostPullRequest(unittest.TestCase):
    URL = "https://github.com/o/r/pull/7"

    def view(self, proc):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return proc
        pr = Host(run=run).pull_request("/my proj", self.URL)
        self.assertEqual(calls, [["sh", "-c", orchestrator.IN_DIR, "_", "/my proj",
                                  "gh", "pr", "view", self.URL, "--json", "state,headRefOid,closedAt"]])
        return pr

    def test_state(self):
        self.assertEqual(self.view(completed(json.dumps(merged()))), merged())
        self.assertEqual(self.view(completed(json.dumps(OPEN))), OPEN)

    def test_unexpected_output(self):
        for out in ("not json", "[]", json.dumps({"state": "MERGED"}), json.dumps({**OPEN, "headRefOid": "main"}),
                    json.dumps({**OPEN, "closedAt": 5})):
            proc = completed(out)
            with self.subTest(out), self.assertRaisesRegex(OrchestratorError, f"gh pr view {self.URL} printed"):
                self.view(proc)

    def test_unusable_gh(self):
        logged_out = completed(stderr="gh auth login\n", returncode=4)
        failing = completed(stderr="HTTP 502", returncode=1)
        with self.assertRaisesRegex(orchestrator.GhUnusable, "failed with status 4: gh auth login"):
            self.view(logged_out)
        with self.assertRaises(OrchestratorError) as raised:
            self.view(failing)
        self.assertNotIsInstance(raised.exception, orchestrator.GhUnusable)

    def test_closed_without_a_time(self):
        pr = {**closed(1), "closedAt": "yesterday"}
        with self.assertRaisesRegex(OrchestratorError, "PR #7 is closed at 'yesterday', which is not a time"):
            orchestrator.pull_request_pending(pr, "7", NOW)

    def merge_view(self, proc):
        run = MagicMock(return_value=proc)
        pr = Host(run=run).pull_request_merge("/my proj", self.URL)
        self.assertEqual(run.call_args.args[0], ["sh", "-c", orchestrator.IN_DIR, "_", "/my proj", "gh", "pr", "view",
                                                 self.URL, "--json", "state,isDraft,mergeable,body"])
        return pr

    def test_merge_view(self):
        pr = {"state": "OPEN", "isDraft": True, "mergeable": "MERGEABLE", "body": "text"}
        self.assertEqual(self.merge_view(completed(json.dumps(pr))), pr)
        for bad in ({**pr, "mergeable": "BLOCKED"}, {**pr, "isDraft": "yes"}, {**pr, "body": None},
                    {"state": "OPEN"}):
            with self.subTest(bad), self.assertRaisesRegex(OrchestratorError, f"gh pr view {self.URL} printed"):
                self.merge_view(completed(json.dumps(bad)))
        with self.assertRaises(orchestrator.GhUnusable):
            self.merge_view(completed(stderr="gh auth login\n", returncode=4))

    def test_edit_body_and_ready_run_gh_in_the_project(self):
        run = MagicMock(return_value=completed())
        host = Host("remote-host", run=run)
        host.edit_pr_body("/proj", self.URL, "new body")
        edit = run.call_args
        host.mark_pr_ready("/proj", self.URL)
        ready = run.call_args
        self.assertTrue(edit.args[0][4].endswith(f"_ /proj gh pr edit {self.URL} --body-file -"), edit.args[0])
        self.assertEqual(edit.kwargs["input"], "new body")
        self.assertTrue(ready.args[0][4].endswith(f"_ /proj gh pr ready {self.URL}"), ready.args[0])
        self.assertEqual({edit.kwargs["timeout"], ready.kwargs["timeout"]}, {orchestrator.NETWORK_TIMEOUT})

    def test_a_failed_edit_raises(self):
        run = MagicMock(return_value=completed(stderr="HTTP 422", returncode=1))
        with self.assertRaisesRegex(OrchestratorError, "gh pr ready .* failed: HTTP 422"):
            Host(run=run).mark_pr_ready("/proj", self.URL)


class TestSweepSummary(PruneTest):
    def sweep(self):
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            orchestrator.sweep_branches(self.host, "/proj")
        return err.getvalue()

    def test_one_line_when_it_deleted_or_kept(self):
        self.add_run("aaaaaa", merged())
        self.add_run("bbbbbb", merged(), n=8)
        self.add_run("cccccc", merged(), n=9, remote=LATER)
        self.assertEqual(self.sweep(), "  deleted 2 branches of earlier runs; kept 1, see prune\n")

    def test_single_branches(self):
        self.add_run("aaaaaa", merged())
        self.assertEqual(self.sweep(), "  deleted 1 branch of earlier runs\n")
        self.add_run("bbbbbb", merged(), n=8, remote=LATER)
        self.assertEqual(self.sweep(), "  kept 1 branch of earlier runs, see prune\n")

    def test_nothing_when_there_was_nothing_to_do(self):
        self.assertEqual(self.sweep(), "")
        self.add_run("aaaaaa", OPEN)
        self.add_run("bbbbbb", merged(), n=8, branch_cleanup="kept: checked out in /proj")
        # Both sides already gone: recorded, but nothing deleted.
        gone = self.add_run("cccccc", merged(), n=9, local=None, remote=None)
        self.assertEqual(self.sweep(), "")
        self.assertEqual(self.saved(gone)["branch_cleanup"], "deleted")

    def test_a_sweep_that_fails_is_one_line(self):
        self.add_run("aaaaaa", merged())
        self.host.checked_out_branches = MagicMock(side_effect=OrchestratorError("git worktree list failed"))
        self.assertEqual(self.sweep(), "  could not prune the branches of earlier runs: git worktree list failed\n")


@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestPruneCLI(PruneTest):
    def main(self, *argv):
        with patch.object(orchestrator, "connect", return_value=(None, self.host)), \
                patch.object(Host, "resolve_dir", return_value="/proj"), \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            code = orchestrator.main(list(argv))
        self.log = err.getvalue()
        return code, out.getvalue()

    def test_prints_one_line_per_candidate_run(self):
        deleted = self.add_run("aaaaaa", merged())
        kept = self.add_run("bbbbbb", merged(), n=8, local=LATER)
        self.host.ahead["orchestrator/task-bbbbbb"] = 3
        waiting = self.add_run("cccccc", OPEN, n=9)
        code, out = self.main("prune")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            f"{deleted}  orchestrator/task-aaaaaa  local deleted, remote deleted",
            f"{kept}  orchestrator/task-bbbbbb  local kept: local has 3 commits beyond PR #8, remote deleted",
            f"{waiting}  orchestrator/task-cccccc  skipped: PR #9 is open",
        ])

    def test_revisits_kept_branches(self):
        run_id = self.add_run("aaaaaa", merged(), branch_cleanup="kept: checked out in /proj")
        self.assertEqual(self.main("prune"),
                         (0, f"{run_id}  orchestrator/task-aaaaaa  local deleted, remote deleted\n"))

    def test_dry_run(self):
        run_id = self.add_run("aaaaaa", merged())
        code, out = self.main("prune", "--dry-run")
        self.assertEqual((code, out),
                         (0, f"{run_id}  orchestrator/task-aaaaaa  local would be deleted, remote would be deleted\n"))
        self.assertEqual((deleting_calls(self.host), self.host.writes), ([], []))

    def test_nothing_to_prune(self):
        self.assertEqual(self.main("prune"), (0, "No run branches to prune.\n"))

    def test_unusable_gh_is_not_an_error(self):
        self.add_run("aaaaaa", completed(stderr="exec: gh: not found", returncode=127))
        self.assertEqual(self.main("prune"), (0, "No run branches to prune.\n"))
        self.assertEqual(self.log.count("\n"), 1)

    def test_target_flags(self):
        args = parse_args(["prune", "--dry-run", "--machine", "m", "--cwd", "~/proj"])
        self.assertEqual((args.dry_run, args.machine, args.cwd), (True, "m", "~/proj"))
        self.assertFalse(parse_args(["prune"]).dry_run)
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["prune", "--machine", "m"])


CLOSE_ID = "20261001-120000-a1b2c3"
CLOSE_STATE = f"/proj/.orchestrator/runs/{CLOSE_ID}/state.json"
CLOSE_BRANCH = "orchestrator/x-a1b2c3"


@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestClose(unittest.TestCase):
    """close on a FakeHost and a FakeHerdr whose workspace w1 is open."""

    def setUp(self):
        self.host = FakeHost()
        self.herdr = FakeHerdr(self.host, None, {})
        self.herdr.workspaces.add("w1")

    def add_run(self, *, age=0, worktree=True, **kw):
        """A run in workspace w1 that failed, by default a --worktree run whose worktree holds a.py uncommitted
        and whose branch is at its base; returns its saved state."""
        state = RunState(CLOSE_ID, "a task", "/proj", None, phase="build", round=1, workspace_id="w1",
                         base="base1", base_branch="main", branch=CLOSE_BRANCH, error="interrupted",
                         worktree=WORKTREE if worktree else None)
        saved = {**asdict(state), **kw}
        self.host.files[CLOSE_STATE] = json.dumps(saved) + "\n"
        self.host.ages[CLOSE_ID] = age
        if worktree:
            self.host.worktrees[WORKTREE] = CLOSE_BRANCH
            self.host.changed.add(f"{WORKTREE}/a.py")
            self.host.branches[CLOSE_BRANCH] = "base1"
        else:
            self.host.branch = CLOSE_BRANCH
            self.host.changed.add("/proj/a.py")
        self.host.ahead[CLOSE_BRANCH] = 0
        return saved

    def close(self, *argv):
        with patch.object(orchestrator, "connect", return_value=(self.herdr, self.host)) as connect, \
                patch.object(Host, "resolve_dir", return_value="/proj"), \
                patch.object(socket, "gethostname", return_value="here"), \
                patch.object(orchestrator, "pid_alive", return_value=False), \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            code = main(["close", *(argv or ["a1b2c3"])])
        self.connect = connect
        self.err = err.getvalue()
        return code, out.getvalue()

    def saved(self):
        return json.loads(self.host.files[CLOSE_STATE])

    def project(self):
        """Everything of the project close might change: files, worktrees, branches and uncommitted changes."""
        return dict(self.host.files), dict(self.host.worktrees), dict(self.host.branches), set(self.host.changed)

    def assert_closed(self, line):
        self.assertEqual(self.saved()["closed"], line)
        self.assertEqual(self.host.mtime_kept, [CLOSE_STATE])

    def test_running_run_is_refused_untouched(self):
        self.add_run(error=None, owner=ME)
        before = self.project()
        code, out = self.close()
        self.assertEqual((code, out), (orchestrator.EXIT_ERROR, ""))
        self.assertEqual(self.err, f"error: run {CLOSE_ID} is still running (pid 4242 on here, beat 0s ago); "
                                   f"stop it before closing it\n")
        self.assertEqual((self.herdr.calls, self.host.git_calls, self.host.writes), ([], [], []))
        self.assertEqual(self.project(), before)

    def test_failed_worktree_run_loses_workspace_worktree_and_branch(self):
        self.add_run()
        code, out = self.close()
        self.assertEqual(code, 0)
        self.assertEqual(out, f"closed workspace w1\n"
                              f"discarding in {WORKTREE}:\n"
                              f"    ?? {WORKTREE}/a.py\n"
                              f"deleted worktree {WORKTREE}\n"
                              f"deleted branch {CLOSE_BRANCH}\n"
                              f"closed run {CLOSE_ID}: workspace closed; worktree deleted; branch deleted\n")
        self.assertEqual(self.herdr.calls, [("close", "w1")])
        self.assertIn(("/proj", ("worktree", "remove", "--force", WORKTREE)), self.host.git_log)
        self.assertEqual((self.host.worktrees, self.host.changed, self.host.branches), ({}, set(), {}))
        self.assert_closed("workspace closed; worktree deleted; branch deleted")

    def test_a_clean_worktree_lists_nothing_to_discard(self):
        self.add_run()
        self.host.changed.clear()
        _, out = self.close()
        self.assertNotIn("discarding", out)

    def test_branch_with_commits_beyond_base_is_kept(self):
        for n, why in ((1, "1 commit beyond base"), (3, "3 commits beyond base")):
            with self.subTest(n=n):
                self.setUp()
                self.add_run()
                self.host.ahead[CLOSE_BRANCH] = n
                code, out = self.close()
                self.assertEqual(code, 0)
                self.assertIn(f"kept branch {CLOSE_BRANCH}: {why}\n", out)
                self.assertIn(CLOSE_BRANCH, self.host.branches)
                self.assertEqual(self.host.worktrees, {})
                self.assert_closed(f"workspace closed; worktree deleted; branch kept: {why}")

    def test_branch_of_a_run_without_a_base_is_kept(self):
        self.add_run(base=None)
        self.close()
        self.assertIn(CLOSE_BRANCH, self.host.branches)
        self.assert_closed("workspace closed; worktree deleted; branch kept: the run has no base to compare it with")

    def test_stale_runs_are_cleaned_up_as_failed_ones(self):
        cases = {"no heartbeat": dict(age=STALE_SECONDS + 1),
                 "dead pid on this host": dict(age=HEARTBEAT_SECONDS + 1, owner=ME)}
        for why, kw in cases.items():
            with self.subTest(why):
                self.setUp()
                self.add_run(error=None, **kw)
                self.assertEqual(self.close()[0], 0)
                self.assertEqual(self.herdr.calls, [("close", "w1")])
                self.assertEqual((self.host.worktrees, self.host.branches), ({}, {}))
                self.assert_closed("workspace closed; worktree deleted; branch deleted")

    def test_failed_in_place_run_only_loses_its_workspace(self):
        self.add_run(worktree=False)
        code, out = self.close()
        self.assertEqual((code, out), (0, f"closed workspace w1\nclosed run {CLOSE_ID}: workspace closed\n"))
        self.assertEqual(self.herdr.calls, [("close", "w1")])
        self.assertEqual(self.host.git_calls, [])
        self.assertEqual((self.host.branch, self.host.changed), (CLOSE_BRANCH, {"/proj/a.py"}))
        self.assert_closed("workspace closed")

    def test_finished_no_pr_worktree_run_keeps_its_worktree(self):
        self.add_run(phase="done", error=None, verdict=APPROVE)
        code, out = self.close()
        self.assertEqual(code, 0)
        self.assertIn(f"worktree kept: {WORKTREE}", out)
        self.assertEqual(self.herdr.calls, [("close", "w1")])
        self.assertEqual(self.host.worktrees, {WORKTREE: CLOSE_BRANCH})
        self.assertEqual(self.host.changed, {f"{WORKTREE}/a.py"})
        self.assertIn(CLOSE_BRANCH, self.host.branches)
        self.assert_closed(f"workspace closed; worktree kept: {WORKTREE}")

    def test_finished_worktree_run_whose_worktree_went_with_its_pull_request(self):
        self.add_run(phase="done", error=None, verdict=APPROVE, pr_url="https://github.com/o/r/pull/7")
        self.host.worktrees.clear()
        self.close()
        self.assert_closed("workspace closed")

    def test_a_workspace_herdr_lacks_is_already_gone(self):
        for workspace in ("w1", ""):
            with self.subTest(workspace=workspace):
                self.setUp()
                self.herdr.workspaces.clear()
                self.add_run(workspace_id=workspace)
                code, out = self.close()
                self.assertEqual(code, 0)
                self.assertNotIn("closed workspace", out)
                self.assertEqual(self.herdr.calls, [])
                self.assert_closed("workspace already gone; worktree deleted; branch deleted")

    def test_a_worktree_already_gone_still_has_its_branch_deleted(self):
        self.add_run()
        self.host.worktrees.clear()
        self.close()
        self.assertEqual(self.host.branches, {})
        self.assert_closed("workspace closed; worktree already gone; branch deleted")
        self.setUp()
        self.add_run()
        self.host.worktrees.clear()
        self.host.branches.clear()
        self.close()
        self.assert_closed("workspace closed; worktree already gone")

    def test_second_close_only_prints_the_record(self):
        self.add_run()
        self.close()
        self.herdr.calls.clear()
        self.host.git_calls.clear()
        writes = list(self.host.writes)
        code, out = self.close()
        self.assertEqual((code, out), (0, f"run {CLOSE_ID} is already closed: "
                                          f"workspace closed; worktree deleted; branch deleted\n"))
        self.assertEqual((self.herdr.calls, self.host.git_calls, self.host.writes), ([], [], writes))

    def test_resume_refuses_a_closed_run_even_with_force(self):
        saved = {**asdict(saved_run("build", 1, agents=("spec", "build"))), "closed": "workspace closed"}
        for flags in ([], ["--force"]):
            args = parse_args(["resume", "a1b2c3", *flags])
            with self.subTest(flags=flags), self.assertRaisesRegex(
                    OrchestratorError, f"run {saved['run_id']} was closed; start a new run"):
                orchestrator.resumable_state([(0, saved)], args, "/proj", "here", lambda pid: True)

    def test_resume_of_a_closed_run_exits_1(self):
        self.add_run()
        self.close()
        for flags in ([], ["--force"]):
            with self.subTest(flags=flags), \
                    patch.object(orchestrator, "connect", return_value=(self.herdr, self.host)), \
                    patch.object(Host, "resolve_dir", return_value="/proj"), \
                    patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(main(["resume", "a1b2c3", *flags]), orchestrator.EXIT_ERROR)
            self.assertIn(f"run {CLOSE_ID} was closed; start a new run", err.getvalue())

    def test_state_saved_before_close_existed_loads_and_resumes(self):
        saved = asdict(saved_run("build", 1, agents=("spec", "build")))
        del saved["closed"]
        state = orchestrator.resumable_state([(10**4, saved)], parse_args(["resume", "a1b2c3"]), "/proj", "here",
                                             lambda pid: True)
        self.assertIsNone(state.closed)

    def test_state_saved_meanwhile_is_not_overwritten(self):
        self.add_run()
        close_workspace = self.herdr.close_workspace

        def resumed_meanwhile(workspace):
            close_workspace(workspace)
            self.host.files[CLOSE_STATE] = json.dumps({**self.saved(), "round": 2})
        self.herdr.close_workspace = resumed_meanwhile
        code, _ = self.close()
        self.assertEqual(code, orchestrator.EXIT_ERROR)
        self.assertEqual(self.err, f"error: {CLOSE_STATE} changed while the run was closed; closed is not recorded\n")
        self.assertEqual((self.saved()["round"], self.saved()["closed"]), (2, None))

    def test_herdr_failure_records_nothing(self):
        self.add_run()
        self.herdr.close_workspace = MagicMock(side_effect=HerdrError("server_error", "herdr is down"))
        code, _ = self.close()
        self.assertEqual(code, orchestrator.EXIT_ERROR)
        self.assertIn("herdr is down", self.err)
        self.assertEqual((self.host.git_calls, self.host.writes), ([], []))
        self.assertIsNone(self.saved()["closed"])

    def test_git_failure_records_nothing_and_a_second_close_finishes(self):
        self.add_run()
        self.host.remove_error = "fatal: cannot remove a locked working tree"
        code, _ = self.close()
        self.assertEqual(code, orchestrator.EXIT_ERROR)
        self.assertIn("cannot remove a locked working tree", self.err)
        self.assertEqual(self.host.writes, [])
        self.assertIsNone(self.saved()["closed"])
        self.assertIn(CLOSE_BRANCH, self.host.branches)

        self.host.remove_error = None
        self.assertEqual(self.close()[0], 0)
        self.assert_closed("workspace already gone; worktree deleted; branch deleted")

    def test_on_a_machine(self):
        self.add_run()
        self.assertEqual(self.close("a1b2c3", "--machine", "m", "--cwd", "~/proj")[0], 0)
        self.connect.assert_called_once_with("m")

    def test_args(self):
        args = parse_args(["close", "a1b2c3", "--machine", "m", "--cwd", "~/proj"])
        self.assertEqual((args.command, args.run_ref, args.machine, args.cwd), ("close", "a1b2c3", "m", "~/proj"))
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["close", "a1b2c3", "--machine", "m"])
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["close"])


NEW_RUN = "20261009-120000-c0ffee"


REFRESH_ID = "20261001-120000-a1b2c3"
REFRESH_URL = "https://github.com/o/r/pull/7"


class TestRefresh(unittest.TestCase):
    """refresh through main, on a FakeHost whose gh is scripted by pull_requests."""

    def setUp(self):
        self.host = FakeHost()
        for p in (patch.object(orchestrator, "connect", return_value=(None, self.host)),
                  patch.object(Host, "resolve_dir", return_value="/proj")):
            p.start()
            self.addCleanup(p.stop)

    def add_run(self, run_id=REFRESH_ID, url=REFRESH_URL, conflicts=("a.py",), body=None, **fields):
        """A done run whose pull request at url was opened over conflicts, and what gh says of that pull request:
        OPEN, a draft, MERGEABLE and with body, by default the one pr_body wrote, unless fields say otherwise.
        Returns the run's saved state."""
        state = RunState(run_id, "a task", "/proj", None, phase="done", round=1, base_branch="main",
                         branch="orchestrator/x", pr_url=url, verdict=fields.pop("verdict", APPROVE),
                         conflicts=list(conflicts))
        saved = asdict(state)
        self.host.files[f"{state.dir}/state.json"] = json.dumps(saved) + "\n"
        if body is None:
            body = pr_body(state, "spec", "report", "VERDICT: APPROVE", orchestrator.GateEvidence())
        self.host.pull_requests[url] = {"state": "OPEN", "isDraft": True, "mergeable": "MERGEABLE", "body": body,
                                        **fields}
        return saved

    def saved(self, run_id=REFRESH_ID):
        return json.loads(self.host.files[f"/proj/.orchestrator/runs/{run_id}/state.json"])

    def refresh(self, *argv):
        with patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            code = main(["refresh", *argv])
        self.err = err.getvalue()
        return code, out.getvalue()

    def clean_body(self, verdict=APPROVE):
        state = RunState(REFRESH_ID, "a task", "/proj", None, round=1, base_branch="main", verdict=verdict)
        return pr_body(state, "spec", "report", "VERDICT: APPROVE", orchestrator.GateEvidence())

    def test_a_cleanly_merging_approved_draft_loses_its_warning_and_is_marked_ready(self):
        self.add_run()
        code, out = self.refresh("a1b2c3")
        self.assertEqual(code, 0)
        self.assertEqual(out, f"{REFRESH_ID}: conflict cleared, ready for review  {REFRESH_URL}\n")
        self.assertEqual(self.host.pr_edits, [("body", REFRESH_URL, self.clean_body()), ("ready", REFRESH_URL)])
        self.assertEqual(self.saved()["conflicts"], [])
        self.assertEqual(self.host.mtime_kept, [f"/proj/.orchestrator/runs/{REFRESH_ID}/state.json"])

    def test_a_run_that_did_not_succeed_keeps_its_draft(self):
        self.add_run(verdict=CHANGES_REQUESTED)
        code, out = self.refresh(REFRESH_ID)
        self.assertEqual(code, 0)
        self.assertEqual(out, f"{REFRESH_ID}: conflict cleared  {REFRESH_URL}\n")
        self.assertEqual(self.host.pr_edits, [("body", REFRESH_URL, self.clean_body(CHANGES_REQUESTED))])
        self.assertEqual(self.saved()["conflicts"], [])

    def test_a_pull_request_already_out_of_draft_is_not_marked_again(self):
        self.add_run(isDraft=False)
        code, out = self.refresh(REFRESH_ID)
        self.assertEqual((code, out), (0, f"{REFRESH_ID}: conflict cleared  {REFRESH_URL}\n"))
        self.assertEqual([edit[0] for edit in self.host.pr_edits], ["body"])

    def test_a_pull_request_that_still_conflicts_or_is_not_known_is_left(self):
        for mergeable, line in (("CONFLICTING", "still conflicts"),
                                ("UNKNOWN", "mergeability not known yet, try again")):
            with self.subTest(mergeable):
                self.setUp()
                saved = self.add_run(mergeable=mergeable)
                code, out = self.refresh(REFRESH_ID)
                self.assertEqual(code, orchestrator.EXIT_WAITING)
                self.assertEqual(out, f"{REFRESH_ID}: {line}  {REFRESH_URL}\n")
                self.assertEqual((self.host.pr_edits, self.host.writes), ([], []))
                self.assertEqual(self.saved(), saved)

    def test_a_merged_or_closed_pull_request_only_forgets_the_conflict(self):
        for state in ("MERGED", "CLOSED"):
            with self.subTest(state):
                self.setUp()
                self.add_run(state=state)
                code, out = self.refresh(REFRESH_ID)
                self.assertEqual((code, out), (0, f"{REFRESH_ID}: {state.lower()}, conflict forgotten\n"))
                self.assertEqual(self.host.pr_edits, [])
                self.assertEqual(self.saved()["conflicts"], [])

    def test_human_edits_to_the_body_are_kept(self):
        warning = orchestrator.conflict_warning("main", ["a.py"])
        self.add_run(body=f"{warning}\n\nA human's note.\n\nOpened by run x.\n")
        self.refresh(REFRESH_ID)
        self.assertEqual(self.host.pr_edits[0], ("body", REFRESH_URL, "A human's note.\n\nOpened by run x.\n"))

    def test_a_body_whose_warning_is_gone_is_not_edited(self):
        for verdict, edits in ((APPROVE, [("ready", REFRESH_URL)]), (CHANGES_REQUESTED, [])):
            with self.subTest(verdict):
                self.setUp()
                self.add_run(body="Edited by hand.\n", verdict=verdict)
                code, _ = self.refresh(REFRESH_ID)
                self.assertEqual(code, 0)
                self.assertEqual(self.host.pr_edits, edits)
                self.assertEqual(self.saved()["conflicts"], [])

    def test_without_a_run_every_done_run_with_a_pull_request_and_conflicts_in_run_order(self):
        urls = {k: f"https://github.com/o/r/pull/{n}" for n, k in enumerate("abcdef", 1)}
        ids = {k: f"20261001-12000{n}-{k * 6}" for n, k in enumerate("abcdef", 1)}
        # Added out of order; d has no conflicts, e no pull request, f is not done.
        self.add_run(ids["c"], urls["c"], mergeable="CONFLICTING")
        self.add_run(ids["a"], urls["a"])
        self.add_run(ids["b"], urls["b"], state="MERGED")
        self.add_run(ids["d"], urls["d"], conflicts=())
        saved = self.add_run(ids["e"], urls["e"])
        self.host.files[f"/proj/.orchestrator/runs/{ids['e']}/state.json"] = json.dumps({**saved, "pr_url": None})
        saved = self.add_run(ids["f"], urls["f"])
        self.host.files[f"/proj/.orchestrator/runs/{ids['f']}/state.json"] = json.dumps({**saved, "phase": "review"})
        code, out = self.refresh()
        self.assertEqual(code, orchestrator.EXIT_WAITING)
        self.assertEqual(out, f"{ids['a']}: conflict cleared, ready for review  {urls['a']}\n"
                              f"{ids['b']}: merged, conflict forgotten\n"
                              f"{ids['c']}: still conflicts  {urls['c']}\n")
        self.assertEqual([call[2] for call in self.host.gh_calls], [urls["a"], urls["b"], urls["c"]])

    def test_without_a_run_and_nothing_to_refresh(self):
        self.add_run(conflicts=())
        self.assertEqual(self.refresh(), (0, ""))
        self.assertEqual(self.host.gh_calls, [])

    def test_a_named_run_without_conflicts_or_not_done(self):
        saved = self.add_run(conflicts=())
        self.assertEqual(self.refresh(REFRESH_ID), (0, f"{REFRESH_ID}: no conflict to clear\n"))
        self.host.files[f"/proj/.orchestrator/runs/{REFRESH_ID}/state.json"] = json.dumps({**saved, "phase": "build"})
        self.assertEqual(self.refresh(REFRESH_ID), (orchestrator.EXIT_ERROR, ""))
        self.assertIn(f"run {REFRESH_ID} is not done", self.err)
        self.assertEqual(self.refresh("ffffff"), (orchestrator.EXIT_ERROR, ""))
        self.assertEqual(self.host.gh_calls, [])

    def test_a_gh_failure_names_the_run(self):
        self.add_run()
        self.host.pull_requests[REFRESH_URL] = completed(stderr="HTTP 502", returncode=1)
        self.assertEqual(self.refresh(REFRESH_ID), (orchestrator.EXIT_ERROR, ""))
        self.assertRegex(self.err, rf"^error: run {REFRESH_ID}: gh pr view .* failed: HTTP 502\n$")

    def test_a_state_saved_meanwhile_is_not_overwritten(self):
        self.add_run()
        path = f"/proj/.orchestrator/runs/{REFRESH_ID}/state.json"
        mark_ready = self.host.mark_pr_ready

        def resumed_meanwhile(cwd, url):
            self.host.files[path] = json.dumps({**self.saved(), "round": 2})
            mark_ready(cwd, url)
        self.host.mark_pr_ready = resumed_meanwhile
        self.assertEqual(self.refresh(REFRESH_ID), (orchestrator.EXIT_ERROR, ""))
        self.assertIn(f"run {REFRESH_ID}: {path} changed while its pull request was refreshed", self.err)
        self.assertEqual(self.saved()["conflicts"], ["a.py"])

    def test_show_no_longer_lists_the_conflicts(self):
        self.add_run()
        with patch.object(orchestrator, "pid_alive", return_value=False), \
                patch.object(self.host, "run_files", return_value=[]), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            main(["show", REFRESH_ID])
            self.assertIn("conflicts  a.py\n", out.getvalue())
            self.refresh(REFRESH_ID)
            out.seek(0)
            out.truncate()
            main(["show", REFRESH_ID])
        self.assertNotIn("conflicts", out.getvalue())

    def test_args(self):
        args = parse_args(["refresh", "a1b2c3", "--machine", "m", "--cwd", "~/proj"])
        self.assertEqual((args.command, args.run_ref, args.machine, args.cwd), ("refresh", "a1b2c3", "m", "~/proj"))
        self.assertIsNone(parse_args(["refresh"]).run_ref)


@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestSweepOnRun(PruneTest):
    """The automatic sweep: main's run, before the new run's state and its interview."""

    def run_main(self, *argv, prune=None):
        """main(argv) on the fakes, the new run taking the default workflow's turns; returns its exit status
        and output. prune stands in for prune_branches, when given."""
        state = RunState(NEW_RUN, "task", "/proj", None)
        self.seen_at_interview = None

        def spec(prompt, s, host):
            self.seen_at_interview = (dict(host.branches), dict(host.remote))
            return spec_turn(prompt, s, host)
        herdr = FakeHerdr(self.host, state, {"spec": [spec], "build": [build_turn(1)],
                                             "review": [review_turn(1, APPROVE)]})
        sweep = patch.object(orchestrator, "prune_branches", prune) if prune else patch.dict({})
        with patch.object(orchestrator, "connect", return_value=(herdr, self.host)), \
                patch.object(Host, "resolve_dir", return_value="/proj"), \
                patch.object(orchestrator, "new_run_id", return_value=NEW_RUN), sweep, \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            code = orchestrator.main(list(argv))
        self.log = err.getvalue()
        return code, out.getvalue()

    def test_deletes_merged_branches_before_the_interview(self):
        earlier = self.add_run("aaaaaa", merged())
        self.assertEqual(self.run_main("run", "task")[0], 0)
        self.assertEqual(self.seen_at_interview[0], {})
        self.assertNotIn("orchestrator/task-aaaaaa", self.seen_at_interview[1])
        self.assertEqual(self.saved(earlier)["branch_cleanup"], "deleted")
        self.assertIn("  deleted 1 branch of earlier runs\n", self.log)

    def test_a_failing_sweep_leaves_the_run_as_it_was(self):
        self.add_run("aaaaaa", merged())
        expected = self.run_main("run", "task")

        self.setUp()
        self.add_run("aaaaaa", merged())
        failing = MagicMock(side_effect=OrchestratorError("ssh: connection reset"))
        self.assertEqual(self.run_main("run", "task", prune=failing), expected)
        failing.assert_called_once()
        self.assertIsNotNone(self.seen_at_interview)
        self.assertIn("could not prune the branches of earlier runs: ssh: connection reset", self.log)
        self.assertIn("orchestrator/task-aaaaaa", self.host.branches)

    @patch.object(Workflow, "run", return_value=APPROVE)
    def test_no_gh_for_resume_or_no_pr(self, _run):
        self.add_run("aaaaaa", merged())
        resumable = saved_run("review", 1, agents=("spec", "build", "review"))
        self.host.files[self.state_path(resumable.run_id)] = json.dumps(asdict(resumable))
        self.host.ages[resumable.run_id] = 10**4
        for argv in (["resume", "a1b2c3"], ["run", "task", "--no-pr"]):
            with self.subTest(argv[0]):
                self.host.gh_calls.clear()
                self.assertEqual(self.run_main(*argv)[0], 0)
                self.assertEqual(self.host.gh_calls, [])
        self.assertIn("orchestrator/task-aaaaaa", self.host.branches)

        self.run_main("run", "task")
        self.assertEqual(len(self.host.gh_calls), 1)


def taking(seconds, turn):
    """turn, its agent working for `seconds` of the host's time before it writes its file."""
    def slow(prompt, state, host):
        host.clock.now += seconds
        return turn(prompt, state, host)
    return slow


def working(prompt, state, host):
    """A turn whose agent works on and writes nothing, until the test writes its file."""
    return "working"


def at(seconds):
    """The time on a FakeHost whose clock reads `seconds`, as a turn record has it."""
    return orchestrator.iso_utc(HOST_EPOCH + seconds)


class Partly(dict):
    """A dict equal to any dict that holds all of its items, whatever else that holds."""

    def __eq__(self, other):
        return isinstance(other, dict) and all(k in other and other[k] == v for k, v in self.items())

    def __ne__(self, other):
        return not self == other

    __hash__ = None


def turn(step, file, start, end, *, role=None, rnd=1, q=0, agent="claude", model=None, human_paced=False, **more):
    """A turn record that started and ended at these host clock readings; None for one not known. It equals a record
    with any session and tokens, unless more gives them."""
    return Partly({"step": step, "role": role or step, "round": rnd, "quality_round": q, "file": file, "agent": agent,
                   "model": model, "human_paced": human_paced,
                   "started_at": None if start is None else at(start), "ended_at": None if end is None else at(end),
                   "seconds": None if start is None or end is None else end - start, **more})


GOLDEN_SUMMARY = """\
# Run 20260929-120000-a1b2c3

- **Workflow:** default
- **Outcome:** APPROVE
- **Pull request:** https://github.com/o/r/pull/7
- **Branch:** `orchestrator/add-a-token-bucket-rate-limiter-a1b2c3`
- **Wall time:** 35m 00s, from the first turn's start to 2026-10-03T04:35:00+00:00

## Task

add a rate limiter

## Roles

| role | harness | model |
| --- | --- | --- |
| Spec Collector (spec) | claude | default |
| Builder (build) | codex | default |
| Reviewer (review) | claude | opus |

## Turns

| step | role | round | harness | model | start (UTC) | duration | file |
| --- | --- | --- | --- | --- | --- | --- | --- |
| spec (human-paced) | spec | 0 | claude | default | 2026-10-03 04:00:00 | 10m 00s | spec.md |
| build | build | 1 | codex | default | 2026-10-03 04:10:00 | 20m 00s | build-1.md |
| review | review | 1 | claude | opus | 2026-10-03 04:30:00 | 5m 00s | review-1.md |

## Rounds

- Round 1: review APPROVE
"""


class TestTurnTimings(unittest.TestCase):
    def summary(self, host):
        return host.files.get(f"{D}/summary.md")

    def test_each_turn_of_an_approved_run_is_recorded_on_the_host_clock(self):
        wf, herdr, host, _ = make_workflow({
            "spec": [taking(600, spec_turn)], "build": [taking(1200, build_turn(1))],
            "review": [taking(300, review_turn(1, APPROVE))]}, models={"review": "opus"}, agent_kinds={"build": "codex"})

        self.assertEqual(wf.run(), APPROVE)
        expected = [turn("spec", "spec.md", 0, 600, rnd=0, human_paced=True),
                    turn("build", "build-1.md", 600, 1800, agent="codex"),
                    turn("review", "review-1.md", 1800, 2100, model="opus")]
        self.assertEqual(wf.state.turns, expected)
        saved = json.loads(host.files[f"{D}/state.json"])
        self.assertEqual(saved["turns"], expected)
        # The workflow's own clock, which stamps the heartbeat, is HOST_EPOCH behind the host's.
        self.assertEqual(saved["heartbeat_at"], "1970-01-01T00:35:00+00:00")
        self.assertEqual(self.summary(host), GOLDEN_SUMMARY)
        body = host.prs[0]["body"]
        timings = GOLDEN_SUMMARY.split("## Turns\n\n")[1].split("\n\n")[0]
        self.assertTrue(body.endswith(f"<details>\n<summary>Run timings</summary>\n\n{timings}\n\n</details>\n"))

    def test_the_end_is_the_files_mtime_even_when_no_orchestrator_watched(self):
        wf, herdr, host, _ = make_workflow({"spec": [spec_turn], "build": [working]})
        clock = wf.clock

        def interrupt():
            if clock.now >= 900:
                raise KeyboardInterrupt
        clock.hooks.append(interrupt)
        with patch("sys.stderr"), self.assertRaises(KeyboardInterrupt):
            wf.run()
        saved = RunState.from_dict(json.loads(host.files[f"{D}/state.json"]))
        self.assertEqual(saved.turns[-1], turn("build", "build-1.md", 0, None))
        self.assertIn("- **Outcome:** error: interrupted\n", self.summary(host))
        self.assertIn("| build | build | 1 | claude | default | 2026-10-03 04:00:00 | ? | build-1.md |\n",
                      self.summary(host))

        # The Builder finishes while no orchestrator runs, and one resumes the run much later.
        clock.hooks.clear()
        clock.now = 1200
        build_turn(1)("", saved, host)
        clock.now = 5000
        wf, herdr, host, _ = resume(saved, {"review": [taking(100, review_turn(1, APPROVE))]}, alive=["build"],
                                    host=host, pull_request=saved.pull_request)
        self.assertEqual(wf.run(), APPROVE)

        self.assertEqual([t for t in wf.state.turns if t["file"] == "build-1.md"],
                         [turn("build", "build-1.md", 0, 1200)])
        self.assertEqual(wf.state.turns[-1], turn("review", "review-1.md", 5000, 5100))
        summary = self.summary(host)
        self.assertIn("- **Outcome:** APPROVE\n", summary)
        self.assertIn("| build | build | 1 | claude | default | 2026-10-03 04:00:00 | 20m 00s | build-1.md |\n"
                      "| review | review | 1 | claude | default | 2026-10-03 05:23:20 | 1m 40s | review-1.md |\n",
                      summary)

    def test_a_restarted_agent_keeps_its_turns_record(self):
        state = saved_run("build", 2, agents=("spec", "build", "review"), prompted="build-2.md",
                          turns=[dict(turn("build", "build-2.md", -1000, None, rnd=2))])
        wf, herdr, host, _ = resume(state, {"build": [taking(50, build_turn(2))], "review": [review_turn(2, APPROVE)]},
                                    alive=["review"])

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn(("prompt", "build-a1b2c3"), herdr.calls)
        self.assertEqual([t["file"] for t in wf.state.turns], ["build-2.md", "review-2.md"])
        self.assertEqual(wf.state.turns[0], turn("build", "build-2.md", -1000, 50, rnd=2))

    def test_a_record_closed_before_the_run_moved_on_is_not_recorded_again(self):
        closed = turn("build", "build-1.md", -100, -50)
        state = saved_run("build", 1, agents=("spec", "build"), prompted="build-1.md", turns=[closed])
        wf, *_ = resume(state, {"review": [review_turn(1, APPROVE)]}, files={lambda s: s.build_path(1): "report"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.turns[:-1], [closed])
        self.assertEqual(wf.state.turns[-1]["file"], "review-1.md")

    def test_quality_rounds_and_answers_have_records_of_their_own(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED), outcome()]
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1), fix_turn(1, 1)],
                                          "review": [review_turn(1, APPROVE)]}, fake=fake)

        self.assertEqual(wf.run(), APPROVE)
        turns = wf.state.turns
        self.assertEqual([(t["file"], t["step"], t["role"], t["round"], t["quality_round"]) for t in turns], [
            ("spec.md", "spec", "spec", 0, 0), ("build-1.md", "build", "build", 1, 0),
            ("quality-1-1.md", "quality", "quality", 1, 1), ("build-1-q1.md", "build", "build", 1, 1),
            ("quality-1-2.md", "quality", "quality", 1, 2), ("review-1.md", "review", "review", 1, 0)])
        quality = [t for t in turns if t["role"] == "quality"]
        self.assertEqual({(t["agent"], t["model"], t["human_paced"]) for t in quality}, {(None, None, False)})
        for t in turns:
            self.assertLessEqual(t["started_at"], t["ended_at"])
            self.assertEqual(t["seconds"], orchestrator.epoch_seconds(t["ended_at"]) -
                             orchestrator.epoch_seconds(t["started_at"]))
        # Each quality round polls Jenkins, which takes the host's time too.
        self.assertGreater(quality[0]["seconds"], 0)
        self.assertIn("| quality | quality | 1 q1 | - | - |", self.summary(host))
        self.assertIn("- Round 1: quality gate ERROR, OK; review APPROVE\n", self.summary(host))

    def test_a_resumed_analysis_keeps_its_start(self):
        fake = FakeCI()
        n = fake.add_build(REF_1_1, outcome())
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/",
                                  "build": n},
                              turns=[dict(turn("quality", "quality-1-1.md", -500, None, q=1, agent=None))])
        wf, *_ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        # build-1.md was written before turns were recorded.
        self.assertEqual([t["file"] for t in wf.state.turns], ["quality-1-1.md", "review-1.md"])
        self.assertEqual(wf.state.turns[0]["started_at"], at(-500))

    def test_a_rejected_verdict_keeps_its_record_under_its_new_name(self):
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)],
            "review": [taking(60, malformed_review(1)), taking(30, review_turn(1, APPROVE))]})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.turns[2:], [turn("review", "review-1.rejected.md", 0, 60),
                                              turn("review", "review-1.md", 60, 90)])
        self.assertEqual(host.mtimes[f"{D}/review-1.rejected.md"], HOST_EPOCH + 60)

    def test_a_state_saved_before_turns_resumes_and_records_its_files_without_a_start(self):
        saved = asdict(saved_run("review", 1, agents=("spec", "build", "review"), prompted="review-1.md",
                                 pull_request=True, base_branch="main", branch="orchestrator/x-a1b2c3"))
        del saved["turns"]
        state = RunState.from_dict(saved)
        self.assertEqual(state.turns, [])
        wf, herdr, host, _ = resume(state, {}, alive=["review"], host=FakeHost(head="def456", branch=state.branch),
                                    files={lambda s: s.review_path(1): f"VERDICT: {APPROVE}\n"})
        host.mtimes[state.review_path(1)] = HOST_EPOCH - 60

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(wf.state.turns, [turn("review", "review-1.md", None, -60)])
        summary = self.summary(host)
        self.assertIn("- **Wall time:** ?, from the first turn's start", summary)
        self.assertIn("| review | review | 1 | claude | default | ? | ? | review-1.md |\n", summary)
        self.assertIn("| review | review | 1 | claude | default | ? | ? | review-1.md |", host.prs[0]["body"])

    def test_a_seeded_spec_has_no_record(self):
        wf, herdr, host, _ = make_workflow({"build": [build_turn(1)], "review": [review_turn(1, APPROVE)]},
                                           state=seeded_run(), spec=orchestrator.SpecFile(SPEC_SOURCE, GIVEN_SPEC))
        with patch("sys.stderr"):
            self.assertEqual(wf.run(), APPROVE)
        self.assertEqual([t["file"] for t in wf.state.turns], ["build-1.md", "review-1.md"])

    def test_every_end_writes_a_summary(self):
        def ended(script, **kw):
            wf, herdr, host, _ = make_workflow(script, **kw)
            with patch("sys.stderr"):
                try:
                    wf.run()
                except OrchestratorError:
                    pass
            return self.summary(host)

        cases = [
            ("changes requested", dict(script={"spec": [spec_turn], "build": [build_turn(1)],
                                               "review": [review_turn(1, CHANGES_REQUESTED)]}, max_rounds=1),
             ["- **Outcome:** CHANGES_REQUESTED\n", "- Round 1: review CHANGES_REQUESTED\n",
              "- **Pull request:** https://github.com/o/r/pull/7\n"]),
            ("finished", dict(script={"build": [solo_turn]}, state=new_run(SOLO), pipeline=SOLO, pull_request=False),
             ["- **Workflow:** solo\n", "- **Outcome:** FINISHED\n", "| Builder (build) | claude | default |\n",
              "- **Pull request:** none\n- **Branch:** none\n", "- Round 1: no verdict\n"]),
            ("failed", dict(script={"spec": [spec_turn], "build": [idle]}, turn_timeout=60),
             [f"- **Outcome:** error: the Builder did not write {D}/build-1.md within 60s; see pane w1:p2\n",
              "| build | build | 1 | claude | default | 2026-10-03 04:00:00 | ? | build-1.md |\n",
              "- Round 1: no verdict\n"]),
        ]
        for name, kw, parts in cases:
            with self.subTest(name):
                summary = ended(**kw)
                for part in parts:
                    self.assertIn(part, summary)

    def test_a_run_without_turns_says_so(self):
        state = saved_run("publish", 1, agents=("build",), base_branch="main", branch="orchestrator/x-a1b2c3",
                          verdict=orchestrator.FINISHED, workflow="solo")
        wf, _, host, _ = resume(state, {}, pipeline=SOLO)
        self.assertEqual(wf.run(), orchestrator.FINISHED)
        self.assertIn("## Turns\n\nNo turns were recorded.\n", self.summary(host))

    def test_a_run_taken_over_writes_no_summary(self):
        other = {"host": "laptop", "pid": 4242, "started_at": "2026-09-30T07:00:00+00:00"}
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                wf, herdr, host, _ = make_workflow({"spec": [spec_turn], "build": [idle]})
                path = f"{D}/state.json"

                def take_over():
                    if wf.clock.now > 30 and json.loads(host.files[path])["owner"] != other:
                        host.files[path] = json.dumps({**json.loads(host.files[path]), "owner": other})
                        if interrupted:
                            raise KeyboardInterrupt
                wf.clock.hooks.append(take_over)

                with patch("sys.stderr", new_callable=io.StringIO) as err, \
                        self.assertRaises(KeyboardInterrupt if interrupted else RunTakenOver):
                    wf.run()
                self.assertIsNone(self.summary(host))
                if interrupted:
                    self.assertIn("could not record the failure in state.json: run 20260929-120000-a1b2c3 was taken "
                                  "over by pid 4242 on laptop", err.getvalue())

    def test_a_failure_that_cannot_be_saved_still_writes_a_summary(self):
        wf, herdr, host, _ = make_workflow({"spec": [spec_turn], "build": [idle]}, turn_timeout=60)
        write = host.write

        def failing(path, text, keep_mtime=False):
            if path.endswith("/state.json") and json.loads(text)["error"]:
                raise OrchestratorError("ssh: connection reset")
            write(path, text, keep_mtime)
        host.write = failing

        with patch("sys.stderr", new_callable=io.StringIO) as err, \
                self.assertRaisesRegex(OrchestratorError, "did not write"):
            wf.run()
        self.assertIn("could not record the failure in state.json: ", err.getvalue())
        self.assertIn("- **Outcome:** error: the Builder did not write", self.summary(host))

    def test_a_summary_that_cannot_be_written_changes_no_outcome(self):
        def failing_summary(host):
            write = host.write

            def failing(path, text, keep_mtime=False):
                if path.endswith("/summary.md"):
                    raise OrchestratorError("disk full")
                write(path, text, keep_mtime)
            host.write = failing

        approved = FakeHost()
        failing_summary(approved)
        wf, *_ = make_workflow({"spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]},
                               host=approved)
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(wf.run(), APPROVE)
        self.assertIn("could not write summary.md: disk full", err.getvalue())
        self.assertEqual(json.loads(approved.files[f"{D}/state.json"])["phase"], "done")

        failed = FakeHost()
        failing_summary(failed)
        wf, *_ = make_workflow({"spec": [spec_turn], "build": [idle]}, host=failed, turn_timeout=60)
        with patch("sys.stderr", new_callable=io.StringIO) as err, \
                self.assertRaisesRegex(OrchestratorError, "did not write"):
            wf.run()
        self.assertIn("could not write summary.md: disk full", err.getvalue())

    def test_durations(self):
        for seconds, text in ((0, "0s"), (59, "59s"), (65, "1m 05s"), (3599, "59m 59s"), (3725, "1h 02m")):
            with self.subTest(seconds=seconds):
                self.assertEqual(orchestrator.format_duration(seconds), text)

    def test_a_pipe_in_a_cell_does_not_split_it(self):
        table = orchestrator.turns_table([turn("build", "a|b.md", 0, 5, model="x|y")])
        self.assertTrue(table.endswith("| build | build | 1 | claude | x\\|y | 2026-10-03 04:00:00 | 5s | a\\|b.md |"))



# ---------------------------------------------------------------------------
# Blocked turns
# ---------------------------------------------------------------------------

QUESTION = "which database?"
BUILD_1 = f"{D}/build-1.md"
BLOCKED_1 = f"{D}/build-1.blocked-1.md"


def blocked_report(path_of, question=QUESTION):
    """A turn whose agent cannot go on without the human: it writes a BLOCKED report and waits."""
    return writes(path_of, f"BLOCKED: {question}\n\nThe spec names no database.", status="idle")


def answered(path_of, text, after=10 * orchestrator.STALL_SECONDS):
    """A turn whose agent waits for the human's answer in its pane, idle, then finishes the work and writes text
    `after` seconds later."""
    def turn(prompt, state, host):
        due, done = host.clock.now + after, []

        def write():
            if not done and host.clock.now >= due:
                done.append(True)
                host.write("/proj/limiter.py", "answered")
                host.write(path_of(state), text)
        host.clock.hooks.append(write)
        return "idle"
    return turn


def blocked_prompt(path, k=1):
    stem, ext = os.path.splitext(path)
    return orchestrator.BLOCKED_PROMPT.format(path=path, blocked_path=f"{stem}.blocked-{k}{ext}")


def blocked_files(host):
    return sorted(p.rsplit("/", 1)[-1] for p in host.files if ".blocked-" in p)


def bounded(wf, limit=100 * orchestrator.STALL_SECONDS):
    """Fail, rather than poll forever, a run that is still waiting after `limit` seconds: no turn here takes that
    long, and a blocked turn has no timeout of its own."""
    def give_up():
        if wf.clock.now > limit:
            raise AssertionError(f"the run is still waiting after {limit}s")
    wf.clock.hooks.append(give_up)
    return wf


def claude_log(*answers):
    """A Claude Code session log: for each (host clock reading, input, output) an answer of the model, written as Claude
    Code writes it, one line per content block, each with the whole answer's usage."""
    lines = [json.dumps({"type": "user", "timestamp": at(0).replace("+00:00", "Z"), "message": {"role": "user"}})]
    for k, (seconds, tokens_in, tokens_out) in enumerate(answers):
        usage = {"input_tokens": tokens_in, "output_tokens": tokens_out, "cache_read_input_tokens": 7,
                 "cache_creation_input_tokens": 1}
        for _ in range(2):
            lines.append(json.dumps({"type": "assistant", "timestamp": at(seconds).replace("+00:00", ".250Z"),
                                     "message": {"id": f"msg_{k}", "usage": usage}}))
    return "\n".join(lines) + "\n"


class TestRunMetrics(unittest.TestCase):
    def test_a_gated_run_of_two_rounds_records_its_turns_and_quality_rounds(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED), outcome(), outcome(**RED), outcome(**RED)]
        wf, herdr, host, fake, _ = gated({
            "spec": [taking(600, spec_turn)], "build": [taking(100, build_turn(1)), taking(50, fix_turn(1, 1)),
                                                        taking(100, build_turn(2)), taking(50, fix_turn(2, 1))],
            "review": [taking(30, review_turn(1, CHANGES_REQUESTED)), taking(30, review_turn(2, APPROVE))]}, fake=fake)

        self.assertEqual(wf.run(), APPROVE)

        metrics = json.loads(host.files[f"{D}/metrics.json"])
        self.assertEqual(host.files[f"{D}/metrics.json"].count("\n"), 1)
        self.assertEqual([(t["step"], t["round"], t["quality_round"], t["harness"]) for t in metrics["turns"]], [
            ("spec", 0, 0, "claude"), ("build", 1, 0, "claude"), ("quality", 1, 1, None), ("build", 1, 1, "claude"),
            ("quality", 1, 2, None), ("review", 1, 0, "claude"), ("build", 2, 0, "claude"), ("quality", 2, 1, None),
            ("build", 2, 1, "claude"), ("quality", 2, 2, None), ("review", 2, 0, "claude")])
        self.assertEqual(metrics["turns"][0]["seconds"], 600)
        self.assertEqual(metrics["turns"][3], {"step": "build", "role": "build", "harness": "claude", "model": None,
                                               "round": 1, "quality_round": 1, "human_paced": False,
                                               "started_at": at(730), "ended_at": at(780), "seconds": 50,
                                               "tokens": "unknown"})
        self.assertEqual(metrics["turns"][2]["tokens"], None)
        self.assertEqual([t["tokens"] for t in metrics["turns"]][:3], ["unknown", "unknown", None])
        ruff = [orchestrator.finding_fingerprint("limiter.py", "ruff F401", "`os` imported but unused")]
        red, green = {"gate": "ERROR", "conditions": [], "fingerprints_by_check": {"ruff": ruff}}, \
            {"gate": "OK", "conditions": [], "fingerprints_by_check": {}}
        self.assertEqual(metrics["quality_rounds"], [
            {"round": 1, "quality_round": 1, **red}, {"round": 1, "quality_round": 2, **green},
            {"round": 2, "quality_round": 1, **red}, {"round": 2, "quality_round": 2, **red}])
        self.assertEqual({k: metrics[k] for k in ("version", "run_id", "workflow", "outcome", "rounds", "quality_gate",
                                                  "max_quality_rounds", "pull_request", "restored_locked_files")},
                         {"version": 1, "run_id": "20260929-120000-a1b2c3", "workflow": "default", "outcome": APPROVE,
                          "rounds": 2, "quality_gate": True, "max_quality_rounds": 3,
                          "pull_request": "https://github.com/o/r/pull/7", "restored_locked_files": 0})
        claude = {"harness": "claude", "model": None}
        self.assertEqual(metrics["roles"], {"spec": claude, "build": claude, "review": claude})
        self.assertEqual(metrics["started_at"], at(0))
        self.assertEqual(metrics["seconds"], orchestrator.epoch_seconds(metrics["ended_at"]) - HOST_EPOCH)
        self.assertEqual(json.loads(host.files[f"{D}/state.json"])["quality_rounds"], metrics["quality_rounds"])

    def test_a_resumed_quality_round_is_recorded_once(self):
        state = quality_state(quality_rounds=[{"round": 1, "quality_round": 1, "gate": "OK", "conditions": [],
                                               "fingerprints_by_check": {}}])
        wf, *_ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, FakeCI(),
                              files={lambda s: s.quality_path(1, 1): "GATE: OK\n"})
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(len(wf.state.quality_rounds), 1)

    def test_a_failed_run_writes_no_metrics(self):
        wf, herdr, host, _ = make_workflow({"spec": [spec_turn], "build": [lambda *a: "done"]}, turn_timeout=5)
        with patch("sys.stderr"), self.assertRaises(OrchestratorError):
            wf.run()
        self.assertIn(f"{D}/summary.md", host.files)
        self.assertNotIn(f"{D}/metrics.json", host.files)

    def test_tokens_come_from_the_session_the_turn_ran_in(self):
        host = FakeHost()
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "build": [taking(100, build_turn(1))],
            "review": [taking(60, review_turn(1, APPROVE))]}, host=host, agent_kinds={"review": "pi"})
        # Sessions are named after herdr's call count when each agent starts: spec, then build, then review.
        herdr_sessions = {}

        def logs():
            herdr_sessions.update({role: herdr.sessions[f"{role}-a1b2c3"] for role in ("build", "review")})
            host.session_logs[("claude", herdr_sessions["build"])] = claude_log((50, 10, 20), (90, 1, 2), (500, 9, 9))
            host.session_logs[("pi", herdr_sessions["review"])] = "\n".join([
                json.dumps({"type": "message", "timestamp": at(130).replace("+00:00", "Z"),
                            "message": {"role": "assistant", "usage": {"input": 5, "output": 6, "cacheRead": 2}}}),
                json.dumps({"type": "message", "timestamp": at(140).replace("+00:00", "Z"),
                            "message": {"role": "user", "usage": {"input": 100, "output": 100}}}),
                "not json"])
        real_publish = wf._publish
        wf._publish = lambda: (logs(), real_publish())

        self.assertEqual(wf.run(), APPROVE)
        tokens = {t["role"]: t["tokens"] for t in json.loads(host.files[f"{D}/metrics.json"])["turns"]}
        self.assertEqual(tokens, {"spec": "unknown", "build": {"input": 11, "output": 22, "cache_read": 14,
                                                               "cache_write": 2},
                                  "review": {"input": 5, "output": 6, "cache_read": 2, "cache_write": 0}})
        self.assertEqual([t["session"] for t in wf.state.turns][1:],
                         [herdr_sessions["build"], herdr_sessions["review"]])

    def test_a_turn_that_changed_sessions_has_unknown_tokens(self):
        state = saved_run("build", 1, agents=("spec", "build"), prompted="build-1.md")
        state.agents["build"]["session"] = "s-old"
        state.turns = [dict(turn("build", "build-1.md", -100, None, session="s-old", tokens=None))]
        wf, herdr, host, _ = resume(state, {"build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        herdr.lost_sessions.add("s-old")
        host.session_logs[("claude", "s-old")] = claude_log((-50, 1, 1))
        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual((wf.state.turns[0]["session"], wf.state.turns[0]["tokens"]), (None, "unknown"))

    def test_turn_tokens_needs_a_known_window_and_a_harness_it_reads(self):
        log = orchestrator.session_usage("claude", claude_log((10, 1, 1)))
        known = {"agent": "claude", "session": "s", "started_at": at(0), "ended_at": at(20)}
        self.assertEqual(orchestrator.turn_tokens(known, log),
                         {"input": 1, "output": 1, "cache_read": 7, "cache_write": 1})
        self.assertEqual(orchestrator.turn_tokens({**known, "ended_at": at(10)}, log)["input"], 1)
        self.assertEqual(orchestrator.turn_tokens({**known, "ended_at": at(9)}, log)["input"], 0)
        for unknown in ({"started_at": None}, {"ended_at": None}, {"session": None}, {"started_at": "garbled"}):
            self.assertEqual(orchestrator.turn_tokens({**known, **unknown}, log), "unknown")
        self.assertEqual(orchestrator.turn_tokens(known, None), "unknown")

    def test_session_log_finds_the_file_under_home(self):
        with tempfile.TemporaryDirectory() as home, patch.dict("os.environ", {"HOME": home}):
            os.makedirs(f"{home}/.claude/projects/-proj")
            with open(f"{home}/.claude/projects/-proj/abc-123.jsonl", "w") as f:
                f.write("log\n")
            self.assertEqual(Host().session_log("claude", "abc-123"), "log\n")
            self.assertIsNone(Host().session_log("claude", "other"))
            self.assertIsNone(Host().session_log("claude", "*"))
            self.assertIsNone(Host().session_log("codex", "abc-123"))

    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_the_metrics_command_prints_a_line_per_run_that_is_done(self):
        with tempfile.TemporaryDirectory() as proj:
            for run_id, metrics in (("1-aaaaaa", {"run_id": "1-aaaaaa", "outcome": APPROVE}), ("2-bbbbbb", None),
                                    ("3-cccccc", {"run_id": "3-cccccc", "outcome": "error"})):
                os.makedirs(f"{proj}/{orchestrator.RUNS_DIR}/{run_id}")
                if metrics:
                    with open(f"{proj}/{orchestrator.RUNS_DIR}/{run_id}/metrics.json", "w") as f:
                        f.write(json.dumps(metrics, indent=1) if run_id.startswith("3") else json.dumps(metrics))
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch("sys.stderr"):
                self.assertEqual(main(["metrics", "--cwd", proj]), orchestrator.EXIT_ERROR)
            # A file that is not one JSON line is corrupt; the run writes each as one.
            with open(f"{proj}/{orchestrator.RUNS_DIR}/3-cccccc/metrics.json", "w") as f:
                f.write(json.dumps({"run_id": "3-cccccc", "outcome": "error"}) + "\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["metrics", "--cwd", proj]), 0)
        self.assertEqual([json.loads(line) for line in out.getvalue().splitlines()],
                         [{"run_id": "1-aaaaaa", "outcome": APPROVE}, {"run_id": "3-cccccc", "outcome": "error"}])
        self.assertEqual(len(out.getvalue().splitlines()), 2)


class Killed(BaseException):
    """The orchestrator process dies here: no handler of the workflow's sees it, and it saves nothing more."""


class KillingHost(FakeHost):
    """A FakeHost whose orchestrator is killed at the first write or rename `kill_at(what, path, text)` picks."""

    def __init__(self, kill_at):
        super().__init__()
        self.kill_at = kill_at
        self.dead = False

    def _check(self, what, path, text=None):
        if self.dead or self.kill_at(what, path, text):
            self.dead = True
            raise Killed(what)

    def write(self, path, text, keep_mtime=False):
        self._check("write", path, text)
        super().write(path, text, keep_mtime)

    def rename(self, path, new_path):
        self._check("rename", path)
        super().rename(path, new_path)


def saving_a_block(what, path, text):
    return what == "write" and path.endswith("state.json") and json.loads(text).get("blocked") is not None


def renaming_a_report(what, path, text):
    return what == "rename" and path == BUILD_1


class TestParseBlocked(unittest.TestCase):
    def test_question(self):
        cases = [("BLOCKED: which database?\nmore", QUESTION), ("\n\n  BLOCKED:which database?  \n", QUESTION),
                 ("BLOCKED:", orchestrator.NO_QUESTION), ("BLOCKED:   \nwhy", orchestrator.NO_QUESTION),
                 ("blocked: which database?", None), ("Done.\nBLOCKED: which database?", None),
                 ("**BLOCKED:** which database?", None), ("", None), ("\n \n", None)]
        for text, question in cases:
            with self.subTest(text=text):
                self.assertEqual(orchestrator.parse_blocked(text), question)

    def test_which_steps_can_block(self):
        spec, build, review = orchestrator.DEFAULT_WORKFLOW.steps
        self.assertEqual((spec.can_block, build.can_block, review.can_block), (False, True, False))


class TestBlocked(unittest.TestCase):
    def run_blocked(self, build, review=None, **kw):
        """A default run whose Builder takes the turns in build; its notifications as (title, body), and the
        prompts the Builder and the Reviewer saw, with the state saved as the Reviewer got its prompt."""
        seen, reviews, at_review = [], [], []

        def review_turn_1(prompt, state, host):
            at_review.append(json.loads(host.files[f"{D}/state.json"]))
            return review_turn(1, APPROVE)(prompt, state, host)
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "build": [recording(b, seen) for b in build],
            "review": [recording(r, reviews) for r in (review or [review_turn_1])]}, **kw)
        notes = []
        wf.notify = lambda title, body: notes.append((title, body))
        return bounded(wf), herdr, host, notes, seen, reviews, at_review

    def test_a_blocked_report_waits_for_the_human_then_goes_to_the_reviewer(self):
        wf, herdr, host, notes, seen, reviews, at_review = self.run_blocked(
            [blocked_report(lambda s: BUILD_1), answered(lambda s: BUILD_1, "report 1")], turn_timeout=60)

        self.assertEqual(wf.run(), APPROVE)
        self.assertTrue(host.files[BLOCKED_1].startswith(f"BLOCKED: {QUESTION}\n"))
        self.assertEqual(host.files[BUILD_1], "report 1")
        self.assertEqual(blocked_files(host), ["build-1.blocked-1.md"])
        self.assertEqual(seen[1], blocked_prompt(BUILD_1))
        self.assertIn(("Builder is blocked: which database?", "pane w1:p2"), notes)
        # Focused, then prompted; the Reviewer only once the Builder has written its report again.
        prompts = [c for c in herdr.calls if c[0] in ("prompt", "focus")]
        self.assertEqual(prompts, [("focus", "spec-a1b2c3"), ("prompt", "spec-a1b2c3"), ("prompt", "build-a1b2c3"),
                                   ("focus", "build-a1b2c3"), ("prompt", "build-a1b2c3"),
                                   ("prompt", "review-a1b2c3")])
        # The wait outlasted --timeout and STALL_SECONDS, unremarked.
        self.assertGreaterEqual(wf.clock.now, 10 * orchestrator.STALL_SECONDS)
        self.assertFalse([t for t, _ in notes if "idle" in t])
        self.assertEqual((at_review[0]["blocked"], at_review[0]["round"]), (None, 1))
        self.assertIn(f"The Builder's report is in {BUILD_1}.", reviews[0])
        self.assertIsNone(wf.state.blocked)

    def test_the_wait_is_saved_and_listed(self):
        during = []

        def wait(prompt, state, host):
            during.append(json.loads(host.files[f"{D}/state.json"]))
            return answered(lambda s: BUILD_1, "report 1")(prompt, state, host)
        wf, *_ = self.run_blocked([blocked_report(lambda s: BUILD_1), wait])

        self.assertEqual(wf.run(), APPROVE)
        saved = during[0]
        self.assertEqual(saved["blocked"], {"file": "build-1.md", "question": QUESTION, "k": 1})
        self.assertIsNone(saved["prompted"])
        with patch("builtins.print") as out:
            orchestrator.print_runs([(40, saved)], saved["owner"]["host"], lambda pid: True, [])
        self.assertEqual(out.call_args_list[0].args[0],
                         f"{saved['run_id']}  build    round 1  blocked: {QUESTION} (pane w1:p2)")

    def test_a_question_left_out(self):
        wf, _, _, notes, *_ = self.run_blocked([blocked_report(lambda s: BUILD_1, question=""),
                                                answered(lambda s: BUILD_1, "report 1")])

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn(("Builder is blocked: (no question given)", "pane w1:p2"), notes)

    def test_blocking_again_waits_again(self):
        wf, herdr, host, notes, seen, *_ = self.run_blocked([
            blocked_report(lambda s: BUILD_1), answered(lambda s: BUILD_1, "BLOCKED: postgres or sqlite?"),
            answered(lambda s: BUILD_1, "report 1")])

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(blocked_files(host), ["build-1.blocked-1.md", "build-1.blocked-2.md"])
        self.assertEqual(host.files[f"{D}/build-1.blocked-2.md"], "BLOCKED: postgres or sqlite?")
        self.assertEqual(seen[1:], [blocked_prompt(BUILD_1), blocked_prompt(BUILD_1, 2)])
        self.assertEqual([t for t, _ in notes if "blocked" in t],
                         ["Builder is blocked: which database?", "Builder is blocked: postgres or sqlite?"])
        self.assertEqual(herdr.calls.count(("prompt", "review-a1b2c3")), 1)

    def test_each_rounds_report_has_its_own_count(self):
        wf, _, host, *_ = self.run_blocked(
            [blocked_report(lambda s: BUILD_1), answered(lambda s: BUILD_1, "report 1"),
             blocked_report(lambda s: s.build_path(2)), answered(lambda s: s.build_path(2), "report 2")],
            review=[review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE)])

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(blocked_files(host), ["build-1.blocked-1.md", "build-2.blocked-1.md"])
        self.assertEqual(wf.state.round, 2)

    def test_an_empty_report_after_a_block_aborts_and_ends_the_block(self):
        wf, _, host, *_ = self.run_blocked([blocked_report(lambda s: BUILD_1), answered(lambda s: BUILD_1, "\n")])

        with self.assertRaisesRegex(OrchestratorError, "build-1.md was written empty by the Builder"):
            wf.run()
        saved = json.loads(host.files[f"{D}/state.json"])
        self.assertEqual((saved["blocked"], saved["prompted"]), (None, None))

    def test_a_blocked_quality_answer_waits_then_goes_back_to_the_quality_round(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED), outcome()]
        seen, reviews, notes = [], [], []
        def q1(s):
            return s.quality_build_path(1, 1)
        wf, _, host, fake, _ = gated({
            "spec": [spec_turn],
            "build": [build_turn(1), recording(blocked_report(q1), seen), recording(answered(q1, "report 1 q1"), seen)],
            "review": [recording(review_turn(1, APPROVE), reviews)]}, fake=fake)
        wf.notify = lambda title, body: notes.append(title)
        bounded(wf)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(blocked_files(host), ["build-1-q1.blocked-1.md"])
        self.assertTrue(seen[0].endswith(NOTE))
        self.assertEqual(seen[1], blocked_prompt(f"{D}/build-1-q1.md"))
        self.assertIn("Builder is blocked: which database?", notes)
        self.assertTrue(host.files[f"{D}/quality-1-2.md"].startswith("GATE: OK\n"))
        self.assertIn(f"The Builder's report is in {D}/build-1-q1.md.", reviews[0])
        self.assertEqual((wf.state.round, wf.state.quality_round, wf.state.blocked), (1, 2, None))

    def test_a_step_from_a_workflow_file_can_block(self):
        tdd = orchestrator.load_workflow_file(f"{EXAMPLES}/tdd.toml", BUILT_IN_PROMPTS)
        def tests_md(s):
            return f"{s.dir}/tests.md"
        seen, specs, notes = [], [], []
        wf, _, host, _ = make_workflow({
            "spec": [recording(spec_turn, specs)],
            "tests": [recording(blocked_report(tests_md, "which test runner?"), seen),
                      recording(answered(tests_md, "tests written"), seen)],
            "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, state=new_run(tdd), pipeline=tdd)
        wf.notify = lambda title, body: notes.append((title, body))
        bounded(wf)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(blocked_files(host), ["tests.blocked-1.md"])
        self.assertTrue(seen[0].endswith(NOTE))
        self.assertEqual(seen[1], blocked_prompt(f"{D}/tests.md"))
        self.assertIn(("Test Writer is blocked: which test runner?", "pane w1:p2"), notes)
        self.assertNotIn("BLOCKED", specs[0])

    def test_a_blocked_verdict_file_gets_the_verdict_retry(self):
        wf, _, host, notes, _, reviews, _ = self.run_blocked(
            [build_turn(1)], review=[writes(lambda s: s.review_path(1), "BLOCKED: should I approve?"),
                                     review_turn(1, APPROVE)])

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(reviews[1], retry_prompt(wf.state.review_path(1)))
        self.assertEqual(host.files[f"{D}/review-1.rejected.md"], "BLOCKED: should I approve?")
        self.assertEqual(blocked_files(host), [])
        self.assertFalse([t for t, _ in notes if "blocked" in t])

    def test_the_note_is_in_every_prompt_of_the_steps_that_can_block_only(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED), outcome(), outcome()]
        prompts = {"spec": [], "build": [], "review": []}
        wf, *_ = gated({
            "spec": [recording(spec_turn, prompts["spec"])],
            "build": [recording(t, prompts["build"]) for t in (build_turn(1), fix_turn(1, 1), build_turn(2))],
            "review": [recording(t, prompts["review"])
                       for t in (malformed_review(1), review_turn(1, CHANGES_REQUESTED), review_turn(2, APPROVE))]},
            fake=fake)

        self.assertEqual(wf.run(), APPROVE)
        # First, quality fix and again; the fresh prompts are in GOLDEN.
        self.assertEqual(len(prompts["build"]), 3)
        for prompt in prompts["build"]:
            self.assertTrue(prompt.endswith(NOTE))
            self.assertEqual(prompt.count(orchestrator.BLOCKED_NOTE), 1)
        # The Reviewer's first, retry and again.
        self.assertEqual(len(prompts["review"]), 3)
        for prompt in prompts["spec"] + prompts["review"]:
            self.assertNotIn("BLOCKED", prompt)
        self.assertEqual(GOLDEN["fresh_build_2"].count(orchestrator.BLOCKED_NOTE), 1)

    def test_workflow_definitions_do_not_contain_the_note(self):
        default = orchestrator.DEFAULT_WORKFLOW
        self.assertNotIn("BLOCKED", orchestrator.workflow_toml(default))
        self.assertNotIn("BLOCKED", "".join(t for st in default.steps for t in st.templates()))

    def test_the_wait_and_the_rewrite_are_a_human_paced_turn(self):
        wf, _, host, *_ = self.run_blocked(
            [taking(600, blocked_report(lambda s: BUILD_1)), answered(lambda s: BUILD_1, "report 1")])

        self.assertEqual(wf.run(), APPROVE)
        end = host.mtimes[BUILD_1] - HOST_EPOCH
        self.assertEqual(wf.state.turns[1:3], [turn("build", "build-1.blocked-1.md", 0, 600),
                                               turn("build", "build-1.md", 600, end, human_paced=True)])
        self.assertEqual(host.mtimes[BLOCKED_1], HOST_EPOCH + 600)
        summary = host.files[f"{D}/summary.md"]
        self.assertIn("| build | build | 1 | claude | default | 2026-10-03 04:00:00 | 10m 00s | "
                      "build-1.blocked-1.md |\n| build (human-paced) | build | 1 | claude | default | "
                      "2026-10-03 04:10:00 |", summary)

    def test_a_state_saved_before_blocks_loads_and_resumes(self):
        saved = asdict(saved_run("build", 1, agents=("spec", "build"), prompted="build-1.md"))
        del saved["blocked"]
        state = RunState.from_dict(saved)
        self.assertIsNone(state.blocked)
        wf, herdr, *_ = resume(state, {"review": [review_turn(1, APPROVE)]}, alive=["build"])
        wf.clock.hooks.append(lambda: build_turn(1)("", state, wf.host))

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(("prompt", "build-a1b2c3"), herdr.calls)


class TestBlockedResume(unittest.TestCase):
    """The orchestrator dies at each point of a block, and a resume carries on from there."""

    def kill(self, host, *, at_focus=False, in_wait=False):
        """Run until the kill; the state.json it left."""
        wait = []

        def waits(prompt, state, h):
            wait.append(prompt)
            return "idle"
        wf, herdr, host, _ = make_workflow({
            "spec": [spec_turn], "build": [blocked_report(lambda s: BUILD_1), waits]}, host=host)
        if at_focus:
            def focus(name):
                if name == "build-a1b2c3":
                    host.dead = True
                    raise Killed("focus")
            herdr.focus = focus

        def die_waiting():
            if in_wait and wait:
                host.dead = True
                raise Killed("wait")
        wf.clock.hooks.append(die_waiting)
        bounded(wf)
        with self.assertRaises(Killed):
            wf.run()
        host.dead, host.kill_at = False, lambda *a: False
        return RunState.from_dict(json.loads(host.files[f"{D}/state.json"])), wait

    def resume_blocked(self, state, host, **kw):
        """Resume, the Builder writing its report once the wait is under way; what it was prompted with."""
        seen, notes = [], []
        wf, herdr, host, _ = resume(state, {"build": [recording(idle, seen)], "review": [review_turn(1, APPROVE)]},
                                    host=host, **kw)
        wf.notify = lambda title, body: notes.append((title, body))

        def answer():
            if blocked_files(host) and BUILD_1 not in host.files:
                build_turn(1)("", state, host)
        wf.clock.hooks.append(answer)
        self.assertEqual(bounded(wf).run(), APPROVE)
        self.assertEqual(blocked_files(host), ["build-1.blocked-1.md"])
        self.assertTrue(host.files[BLOCKED_1].startswith("BLOCKED: "))
        self.assertIn(("Builder is blocked: which database?", "pane w1:p2"), notes)
        self.assertIn(("focus", "build-a1b2c3"), herdr.calls)
        self.assertEqual([t["file"] for t in wf.state.turns if t["step"] == "build"],
                         ["build-1.blocked-1.md", "build-1.md"])
        self.assertTrue(wf.state.turns[2]["human_paced"])
        self.assertIsNone(wf.state.blocked)
        return seen

    def test_killed_before_saving_the_block(self):
        host = KillingHost(saving_a_block)
        state, _ = self.kill(host)
        self.assertIsNone(state.blocked)
        self.assertIn(BUILD_1, host.files)

        self.assertEqual(self.resume_blocked(state, host, alive=["build"]), [blocked_prompt(BUILD_1)])

    def test_killed_between_saving_the_block_and_the_rename(self):
        host = KillingHost(renaming_a_report)
        state, _ = self.kill(host)
        self.assertEqual(state.blocked, {"file": "build-1.md", "question": QUESTION, "k": 1})
        self.assertIn(BUILD_1, host.files)
        self.assertNotIn(BLOCKED_1, host.files)

        self.assertEqual(self.resume_blocked(state, host, alive=["build"]), [blocked_prompt(BUILD_1)])

    def test_killed_after_the_rename_before_the_prompt(self):
        host = KillingHost(lambda *a: False)
        state, wait = self.kill(host, at_focus=True)
        self.assertEqual((state.blocked["k"], state.prompted, wait), (1, None, []))
        self.assertNotIn(BUILD_1, host.files)

        self.assertEqual(self.resume_blocked(state, host, alive=["build"]), [blocked_prompt(BUILD_1)])

    def killed_waiting(self):
        host = KillingHost(lambda *a: False)
        state, wait = self.kill(host, in_wait=True)
        self.assertEqual((state.blocked["k"], state.prompted, wait), (1, "build-1.md", [blocked_prompt(BUILD_1)]))
        return state, host

    def test_killed_waiting_with_the_agent_alive_only_waits(self):
        self.assertEqual(self.resume_blocked(*self.killed_waiting(), alive=["build"]), [])

    def test_killed_waiting_with_the_agent_resumed_continues(self):
        state, host = self.killed_waiting()
        self.assertEqual(self.resume_blocked(state, host, sessions={"build": "s-build"}),
                         [orchestrator.CONTINUE_PROMPT.format(path=BUILD_1)])

    def test_killed_waiting_with_the_agent_restarted_gets_the_blocked_prompt(self):
        state, host = self.killed_waiting()
        del state.agents["build"]["session"]
        self.assertEqual(self.resume_blocked(state, host), [blocked_prompt(BUILD_1)])

    def test_a_report_written_while_no_orchestrator_watched_ends_the_wait(self):
        state, host = self.killed_waiting()
        build_turn(1)("", state, host)
        wf, herdr, host, notes = resume(state, {"review": [review_turn(1, APPROVE)]}, host=host, alive=["build"])

        self.assertEqual(bounded(wf).run(), APPROVE)
        self.assertEqual(notes, [f"Run finished: {APPROVE}"])
        self.assertNotIn(("prompt", "build-a1b2c3"), herdr.calls)
        self.assertEqual(blocked_files(host), ["build-1.blocked-1.md"])
        self.assertIsNone(wf.state.blocked)


BLOCK = {"file": "build-1.md", "question": QUESTION, "k": 1}


class TestBlockedListing(unittest.TestCase):

    def lines(self, *runs):
        with patch("builtins.print") as out:
            orchestrator.print_runs(list(runs), "here", lambda pid: True, [])
        return [c.args[0] for c in out.call_args_list][::2]

    def test_a_running_blocked_run_shows_its_question_and_pane(self):
        record = {**run_record(owner=ME), "blocked": BLOCK, "agents": {"build": {"pane": "w1:p2"}}}
        stale = {**record, "phase": "build"}
        self.assertEqual(self.lines((40, record), (STALE_SECONDS + 1, stale)), [
            f"20260930-070000-c0ffee  build    round 1  blocked: {QUESTION} (pane w1:p2)",
            "20260930-070000-c0ffee  build    round 1  stale: no heartbeat for 5m; resume: orchestrator.py resume c0ffee",
        ])

    def test_without_a_known_pane_only_the_question(self):
        unknown = {**run_record(owner=ME), "blocked": BLOCK, "workflow": "nosuch"}
        no_agent = {**run_record(owner=ME), "blocked": BLOCK}
        self.assertEqual(self.lines((40, unknown), (40, no_agent)), [
            f"20260930-070000-c0ffee  build    round 1  blocked: {QUESTION}  [nosuch workflow]",
            f"20260930-070000-c0ffee  build    round 1  blocked: {QUESTION}",
        ])

    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_show(self):
        host = ShowHost(show_state(phase="build", owner=ME, verdict=None, blocked=BLOCK))
        host.states = [(40, s) for _, s in host.states]
        with patch.object(orchestrator, "connect", return_value=(None, host)), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["show", "a1b2c3"]), 0)
        self.assertEqual(out.getvalue().splitlines()[0],
                         f"{SHOW_ID}  build    round 1  blocked: {QUESTION} (pane w1:p2)")

    def test_status_tag(self):
        self.assertEqual(orchestrator.status_tag(f"blocked: {QUESTION} (pane w1:p2)"), "warn")


# ---------------------------------------------------------------------------
# serve: a project's runs over HTTP, for the desktop app
# ---------------------------------------------------------------------------

SERVE_ID = "20261024-120000-5e7e00"
SERVE_RUN_DIR = f"/proj/.orchestrator/runs/{SERVE_ID}"
TAURI_ORIGIN = "tauri://localhost"
OPENER = urllib.request.build_opener()


def serve_state(**kw):
    return {"run_id": SERVE_ID, "task": "add a rate limiter", "phase": "build", "round": 1, **kw}


class ServeHost(FakeHost):
    """A FakeHost whose run_files lists the files under a run directory in `files`, by their `mtimes`, as Host's does,
    and which records the path of every read in `reads`."""

    def __init__(self, ssh_target=None):
        super().__init__()
        self.ssh_target = ssh_target
        self.reads = []

    def read(self, path):
        self.reads.append(path)
        return super().read(path)

    def run_files(self, run_dir):
        files = [(self.mtimes.get(p, 0), p) for p in self.files
                 if p.startswith(f"{run_dir}/") and p != f"{run_dir}/state.json"]
        return [orchestrator.RunFile(p, m) for m, p in sorted(files)]


class ServerTest(unittest.TestCase):
    """A RunServer on a free port, serving the projects connect gives, for as long as the test runs."""

    def start(self, connect, origins=()):
        server = orchestrator.RunServer(origins, connect=connect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.stopping.set)
        self.server = server
        return server

    def fake_project(self, host=None, origins=()):
        """Serve a ServeHost as /proj, on any machine; returns the host."""
        host = host or ServeHost()
        self.connects = []

        def connect(machine, cwd):
            self.connects.append((machine, cwd))
            return host, "/proj"

        self.start(connect, origins)
        return host

    def request(self, path, method="GET", token=True, **headers):
        """(HTTP status code, response headers, JSON body or else None) of a request to the server, with its token
        as a Bearer header unless token is False."""
        if token:
            headers.setdefault("Authorization", f"Bearer {self.server.token}")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        is_json = resp.headers.get_content_type() == "application/json"
        return resp.status, resp.headers, json.loads(body) if is_json else None

    def query(self, endpoint, **params):
        return f"{endpoint}?{urllib.parse.urlencode({'cwd': '~/proj', **params})}"

    def events(self, **params):
        """The server's event stream, read through urllib as an EventSource would, with the token in the URL. It
        ends when the server stops, at the end of the test."""
        url = f"{self.server.url}{self.query('/events', token=self.server.token, **params)}"
        return EventStream(OPENER.open(url, timeout=10))


class EventStream:
    """A text/event-stream response, its events parsed on a thread as they arrive."""

    def __init__(self, response):
        self.response = response
        self.headers = response.headers
        self.queue = queue.Queue()
        self.ended = threading.Event()  # set once the response has ended
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        kind, payload = None, None
        with contextlib.suppress(OSError, ValueError):
            for raw in self.response:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    kind = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    payload = json.loads(line.removeprefix("data: "))
                elif not line and kind:
                    self.queue.put((kind, payload))
                    kind, payload = None, None
        self.response.close()
        self.ended.set()

    def next(self, timeout=2):
        """The next event, as (kind, payload); fails the test when none comes within timeout seconds."""
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no event within {timeout}s") from None

    def take(self, n, timeout=2):
        return [self.next(timeout) for _ in range(n)]


class TestProjectHost(unittest.TestCase):
    @patch.object(Host, "resolve_dir", return_value="/home/user/proj")
    @patch.object(Herdr, "ssh_target", autospec=True, return_value="remote-host")
    def test_a_machine_is_reached_by_its_saved_target(self, target, resolve_dir):
        host, cwd = orchestrator.project_host("devbox", "~/proj")
        self.assertEqual((host.ssh_target, cwd), ("remote-host", "/home/user/proj"))
        self.assertEqual(target.call_args.args[0].machine, "devbox")
        resolve_dir.assert_called_once_with("~/proj")

    @patch.object(Host, "resolve_dir", return_value="/home/user/proj")
    @patch.object(Herdr, "ssh_target", side_effect=AssertionError("herdr was asked"))
    def test_no_machine_is_this_one(self, _target, _resolve_dir):
        host, cwd = orchestrator.project_host("", "~/proj")
        self.assertEqual((host.ssh_target, cwd), (None, "/home/user/proj"))


class TestServeEndpoints(ServerTest):
    def test_runs_are_list_json_records_on_a_machine(self):
        host = self.fake_project(ServeHost("remote-host"))
        host.files[f"{SERVE_RUN_DIR}/state.json"] = json.dumps(serve_state(owner=ME))
        host.ages[SERVE_ID] = STALE_SECONDS + 1
        code, _, doc = self.request(self.query("/runs", machine="remote"))
        self.assertEqual(code, 200)
        self.assertEqual(self.connects, [("remote", "~/proj")])
        with patch.object(Host, "run_states", return_value=host.run_states("/proj")), \
                patch.object(Host, "resolve_dir", return_value="/proj"), \
                patch.object(Herdr, "ssh_target", return_value="remote-host"), \
                patch.object(Herdr, "_exec", side_effect=AssertionError("herdr was called")), \
                patch("builtins.print") as out:
            self.assertEqual(main(["list", "--json", "--machine", "remote", "--cwd", "~/proj"]), 0)
        self.assertEqual(doc, json.loads(out.call_args.args[0]))
        self.assertEqual(doc["runs"][0]["resume_command"],
                         "orchestrator.py resume 5e7e00 --machine remote --cwd '~/proj'")

    def test_runs_are_list_json_records_locally(self):
        with tempfile.TemporaryDirectory() as d:
            self.start(orchestrator.project_host)
            Host().write(f"{d}/{orchestrator.RUNS_DIR}/{SERVE_ID}/state.json", json.dumps(serve_state()))
            Host().write(f"{d}/{orchestrator.RUNS_DIR}/x-beef00/state.json",
                         json.dumps({**serve_state(phase="done", verdict=APPROVE), "run_id": "x-beef00"}))
            code, _, doc = self.request(f"/runs?{urllib.parse.urlencode({'cwd': d})}")
            # list needs a herdr pane, which serve does not.
            with patch.dict("os.environ", {"HERDR_ENV": "1"}), patch("builtins.print") as out:
                self.assertEqual(main(["list", "--json", "--cwd", d]), 0)
        self.assertEqual(code, 200)
        listed = json.loads(out.call_args.args[0])
        # A second may pass between the two reads; everything else is the same.
        for record in doc["runs"] + listed["runs"]:
            record.pop("heartbeat_age")
            record.pop("health_reason")
        self.assertEqual(doc, listed)
        self.assertEqual(len(doc["runs"]), 2)

    def test_a_project_is_connected_once(self):
        self.fake_project()
        self.request(self.query("/runs"))
        self.request(self.query("/runs"))
        self.assertEqual(self.connects, [("", "~/proj")])

    def test_files_with_their_mtimes(self):
        host = self.fake_project()
        host.files[f"{SERVE_RUN_DIR}/state.json"] = json.dumps(serve_state())
        host.files[f"{SERVE_RUN_DIR}/spec.md"] = "# Spec\n"
        host.files[f"{SERVE_RUN_DIR}/build-1.md"] = "built"
        host.mtimes.update({f"{SERVE_RUN_DIR}/spec.md": 100, f"{SERVE_RUN_DIR}/build-1.md": 200})
        code, _, doc = self.request(self.query("/files", run=SERVE_ID))
        self.assertEqual((code, doc), (200, {"run": SERVE_ID, "files": [{"name": "spec.md", "mtime": 100},
                                                                         {"name": "build-1.md", "mtime": 200}]}))

    def test_file(self):
        host = self.fake_project()
        host.files[f"{SERVE_RUN_DIR}/spec.md"] = "# Spec\n"
        code, _, doc = self.request(self.query("/file", run=SERVE_ID, name="spec.md"))
        self.assertEqual((code, doc), (200, {"run": SERVE_ID, "name": "spec.md", "text": "# Spec\n"}))

    def test_missing_file(self):
        self.fake_project()
        code, _, doc = self.request(self.query("/file", run=SERVE_ID, name="review-1.md"))
        self.assertEqual(code, 404)
        self.assertIn("review-1.md", doc["error"])

    def test_paths_outside_the_run_directory_are_refused_unread(self):
        host = self.fake_project()
        host.files[f"{SERVE_RUN_DIR}/state.json"] = json.dumps(serve_state())
        host.files["/proj/secret"] = "secret"
        for run, name in ((SERVE_ID, "../../../secret"), (SERVE_ID, "/proj/secret"), (SERVE_ID, "state.json"),
                          (SERVE_ID, ".."), (SERVE_ID, "."), (SERVE_ID, ""), (SERVE_ID, "spec\0.md"),
                          ("..", "secret"), (".", "secret"), (f"{SERVE_ID}/..", "secret"),
                          ("/proj", "secret")):
            with self.subTest(run=run, name=name):
                code, _, doc = self.request(self.query("/file", run=run, name=name))
                self.assertEqual(code, 403)
                self.assertNotIn("text", doc)
                self.assertEqual(doc["error"], "state.json is not a handoff file" if name == "state.json"
                                 else f"{name if run == SERVE_ID else run!r} is not a name in the run directory")
        self.assertEqual(host.reads, [])

    def test_files_of_a_run_outside_the_runs_directory_are_refused(self):
        self.fake_project()
        code, _, _ = self.request(self.query("/files", run="../.."))
        self.assertEqual(code, 403)

    def test_missing_parameter(self):
        self.fake_project()
        for path in ("/runs", self.query("/file", run=SERVE_ID), self.query("/files")):
            with self.subTest(path=path):
                code, _, doc = self.request(path)
                self.assertEqual(code, 400)
                self.assertIn("missing parameter", doc["error"])

    def test_unknown_endpoint(self):
        self.fake_project()
        self.assertEqual(self.request(self.query("/state"))[0], 404)

    def test_an_unreadable_project_is_a_bad_gateway(self):
        def connect(machine, cwd):
            raise OrchestratorError("no saved herdr machine named nosuch")

        self.start(connect)
        code, _, doc = self.request(self.query("/runs", machine="nosuch"))
        self.assertEqual((code, doc), (502, {"error": "no saved herdr machine named nosuch"}))


class TestServeSecurity(ServerTest):
    def test_no_token(self):
        self.fake_project()
        code, headers, _ = self.request(self.query("/runs"), token=False)
        self.assertEqual(code, 401)
        self.assertEqual(headers["WWW-Authenticate"], "Bearer")

    def test_wrong_token(self):
        self.fake_project()
        for headers in ({"Authorization": "Bearer nope"}, {"Authorization": self.server.token}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(self.query("/runs"), token=False, **headers)[0], 401)
        self.assertEqual(self.request(self.query("/runs", token="nope"), token=False)[0], 401)

    def test_non_ascii_tokens(self):
        self.fake_project()
        # http.client sends a header's str as Latin-1, so these are é's UTF-8 bytes on the wire.
        for header in ("Bearer é".encode().decode("latin-1"), f"Bearer {self.server.token}é".encode().decode("latin-1")):
            with self.subTest(header=header):
                self.assertEqual(self.request(self.query("/runs"), token=False, Authorization=header)[0], 401)
        # é, an encoded lone surrogate, and a byte that is not UTF-8.
        for token in ("%C3%A9", "%ED%B2%80", "%FF"):
            with self.subTest(token=token):
                self.assertEqual(self.request(f"{self.query('/runs')}&token={token}", token=False)[0], 401)

    def test_token_in_the_query(self):
        self.fake_project()
        self.assertEqual(self.request(self.query("/runs", token=self.server.token), token=False)[0], 200)

    def test_tokens_differ_per_server(self):
        self.fake_project()
        other = orchestrator.RunServer()
        self.addCleanup(other.server_close)
        self.assertNotEqual(other.token, self.server.token)
        self.assertGreaterEqual(len(self.server.token), 32)

    def test_bound_to_loopback_only(self):
        self.fake_project()
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.server.url, f"http://127.0.0.1:{self.server.server_address[1]}")

    def test_foreign_host_header(self):
        self.fake_project()
        port = self.server.server_address[1]
        for host in ("evil.example", f"evil.example:{port}", "127.0.0.1", f"127.0.0.1:{port + 1}"):
            with self.subTest(host=host):
                code, _, doc = self.request(self.query("/runs"), Host=host)
                self.assertEqual(code, 403)
                self.assertNotIn("runs", doc)

    def test_own_host_headers(self):
        self.fake_project()
        port = self.server.server_address[1]
        for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            with self.subTest(host=host):
                self.assertEqual(self.request(self.query("/runs"), Host=host)[0], 200)

    def test_foreign_origin(self):
        self.fake_project(origins=[TAURI_ORIGIN])
        for origin in ("http://evil.example", "null", "http://localhost:1420"):
            with self.subTest(origin=origin):
                code, headers, _ = self.request(self.query("/runs"), Origin=origin)
                self.assertEqual(code, 403)
                self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_foreign_origin_even_without_a_token(self):
        self.fake_project()
        self.assertEqual(self.request(self.query("/runs"), token=False, Origin="http://evil.example")[0], 403)

    def test_the_webview_origin_is_allowed_by_cors(self):
        self.fake_project(origins=[TAURI_ORIGIN])
        code, headers, _ = self.request(self.query("/runs"), Origin=TAURI_ORIGIN)
        self.assertEqual(code, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], TAURI_ORIGIN)

    def test_own_origin_and_none(self):
        self.fake_project()
        code, headers, _ = self.request(self.query("/runs"))
        self.assertEqual(code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        port = self.server.server_address[1]
        self.assertEqual(self.request(self.query("/runs"), Origin=f"http://localhost:{port}")[0], 200)

    def test_preflight_of_the_webview(self):
        self.fake_project(origins=[TAURI_ORIGIN])
        code, headers, _ = self.request("/runs", method="OPTIONS", token=False, Origin=TAURI_ORIGIN)
        self.assertEqual(code, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], TAURI_ORIGIN)
        self.assertEqual(headers["Access-Control-Allow-Headers"], "Authorization")
        self.assertEqual(headers["Access-Control-Allow-Methods"], "GET")

    def test_preflight_of_a_foreign_origin(self):
        self.fake_project(origins=[TAURI_ORIGIN])
        code, headers, _ = self.request("/runs", method="OPTIONS", token=False, Origin="http://evil.example")
        self.assertEqual(code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_writes_are_not_served(self):
        self.fake_project()
        self.assertEqual(self.request(self.query("/runs"), method="POST")[0], 501)


class TestServeEvents(ServerTest):
    """The event stream against real run directories, on a real server, read through urllib."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.cwd = d.name
        self.runs = f"{d.name}/{orchestrator.RUNS_DIR}"
        self.start(orchestrator.project_host)

    def query(self, endpoint, **params):
        return f"{endpoint}?{urllib.parse.urlencode({'cwd': self.cwd, **params})}"

    def save(self, run_id, **kw):
        Host().write(f"{self.runs}/{run_id}/state.json", json.dumps({**serve_state(**kw), "run_id": run_id}))

    def assert_soon(self, stream, expected, started):
        """The next event is expected, and arrived within 2 s of started."""
        kind, payload = stream.next(timeout=3)
        self.assertLessEqual(time.monotonic() - started, 2)
        self.assertEqual((kind, {k: payload[k] for k in expected[1]}), expected)
        return payload

    def test_the_stream(self):
        self.save(SERVE_ID)
        Host().write(f"{self.runs}/{SERVE_ID}/spec.md", "# Spec\n")
        stream = self.events()
        self.assertEqual(stream.headers["Content-Type"], "text/event-stream")
        self.assertEqual(stream.headers["Cache-Control"], "no-store")
        (kind, record), (files_kind, files) = stream.take(2)
        self.assertEqual((kind, record["run"], record["record"]["phase"]), ("record", SERVE_ID, "build"))
        self.assertEqual((files_kind, [f["name"] for f in files["files"]]), ("files", ["spec.md"]))

        started = time.monotonic()
        self.save(SERVE_ID, phase="review")
        payload = self.assert_soon(stream, ("record", {"run": SERVE_ID}), started)
        self.assertEqual(payload["record"]["phase"], "review")

        started = time.monotonic()
        Host().write(f"{self.runs}/{SERVE_ID}/build-1.md", "built")
        payload = self.assert_soon(stream, ("files", {"run": SERVE_ID}), started)
        self.assertEqual([f["name"] for f in payload["files"]], ["spec.md", "build-1.md"])

        started = time.monotonic()
        self.save("20261024-130000-a11ce0")
        self.assert_soon(stream, ("record", {"run": "20261024-130000-a11ce0"}), started)
        self.assert_soon(stream, ("files", {"run": "20261024-130000-a11ce0", "files": []}), started)

        started = time.monotonic()
        shutil.rmtree(f"{self.runs}/{SERVE_ID}")
        self.assert_soon(stream, ("record", {"run": SERVE_ID, "record": None}), started)

    def test_nothing_while_nothing_changes(self):
        self.save(SERVE_ID)
        stream = self.events()
        stream.take(2)
        with self.assertRaises(AssertionError):
            stream.next(timeout=2.5)

    def test_an_unreadable_project_is_a_problem_event(self):
        os.makedirs(f"{self.runs}/{SERVE_ID}")
        with open(f"{self.runs}/{SERVE_ID}/state.json", "w") as f:
            f.write("{not json")
        stream = self.events()
        kind, payload = stream.next()
        self.assertEqual(kind, "problem")
        self.assertIn("corrupt run state", payload["error"])
        started = time.monotonic()
        self.save(SERVE_ID)
        self.assert_soon(stream, ("problem", {"error": None}), started)
        self.assert_soon(stream, ("record", {"run": SERVE_ID}), started)

    def test_the_stream_ends_when_the_server_stops(self):
        stream = self.events()
        self.server.stopping.set()
        self.assertTrue(stream.ended.wait(timeout=3))


class TestServeEventsOnAMachine(ServerTest):
    """The event stream of a --machine project, on FakeHost."""

    @patch.object(orchestrator, "SERVE_REMOTE_POLL_SECONDS", 0.1)
    @patch.object(orchestrator, "SERVE_POLL_SECONDS", 60)
    def test_changes_are_polled(self):
        host = self.fake_project(ServeHost("remote-host"))
        host.files[f"{SERVE_RUN_DIR}/state.json"] = json.dumps(serve_state())
        stream = self.events(machine="remote")
        self.assertEqual([kind for kind, _ in stream.take(2)], ["record", "files"])
        host.files[f"{SERVE_RUN_DIR}/review-1.md"] = "VERDICT: APPROVE\n"
        kind, payload = stream.next()
        self.assertEqual((kind, payload), ("files", {"run": SERVE_ID, "files": [{"name": "review-1.md", "mtime": 0}]}))
        del host.files[f"{SERVE_RUN_DIR}/state.json"]
        self.assertEqual(stream.next(), ("record", {"run": SERVE_ID, "record": None}))

    @patch.object(orchestrator, "SERVE_REMOTE_POLL_SECONDS", 60)
    @patch.object(orchestrator, "SERVE_POLL_SECONDS", 0.1)
    def test_a_machine_is_polled_less_often(self):
        host = self.fake_project(ServeHost("remote-host"))
        host.files[f"{SERVE_RUN_DIR}/state.json"] = json.dumps(serve_state())
        stream = self.events(machine="remote")
        # The first poll, whenever the handler gets to it, sends the run; only a change after it waits for the next.
        self.assertEqual([kind for kind, _ in stream.take(2)], ["record", "files"])
        host.files[f"{SERVE_RUN_DIR}/state.json"] = json.dumps(serve_state(phase="review"))
        with self.assertRaises(AssertionError):
            stream.next(timeout=1)


class TestRunWatch(unittest.TestCase):
    def setUp(self):
        self.host = ServeHost()
        self.watch = orchestrator.RunWatch(orchestrator.ProjectRuns(self.host, "/proj", []))

    def save(self, age=0, **kw):
        self.host.files[f"{SERVE_RUN_DIR}/state.json"] = json.dumps(serve_state(**kw))
        self.host.ages[SERVE_ID] = age

    def test_first_poll_sends_everything(self):
        self.save()
        self.host.files[f"{SERVE_RUN_DIR}/spec.md"] = "# Spec\n"
        events = self.watch.poll()
        self.assertEqual([(kind, payload["run"]) for kind, payload in events], [("record", SERVE_ID), ("files", SERVE_ID)])
        self.assertEqual(events[0][1]["record"]["phase"], "build")
        self.assertEqual(events[1][1]["files"], [{"name": "spec.md", "mtime": 0}])
        self.assertEqual(self.watch.poll(), [])

    def test_an_older_heartbeat_alone_is_no_event(self):
        self.save(age=10)
        self.watch.poll()
        self.save(age=50)
        self.assertEqual(self.watch.poll(), [])

    def test_a_run_turning_stale_is_an_event(self):
        self.save(age=10)
        self.watch.poll()
        self.save(age=STALE_SECONDS + 1)
        [(kind, payload)] = self.watch.poll()
        self.assertEqual((kind, payload["record"]["health"]), ("record", orchestrator.STALE))

    def test_a_finished_runs_files_are_read_again_only_when_it_changes(self):
        self.save(phase="done", verdict=APPROVE)
        self.watch.poll()
        self.host.files[f"{SERVE_RUN_DIR}/summary.md"] = "done"
        self.assertEqual(self.watch.poll(), [])
        self.save(phase="done", verdict=APPROVE, closed="2026-10-24T12:00:00Z")
        self.assertEqual([kind for kind, _ in self.watch.poll()], ["record", "files"])

    def test_a_run_of_this_host_whose_orchestrator_exited_is_stale(self):
        self.save(age=HEARTBEAT_SECONDS + 1, owner={"host": socket.gethostname(), "pid": 4242, "started_at": "x"})
        with patch.object(orchestrator, "pid_alive", return_value=False) as alive:
            [(kind, payload), _] = self.watch.poll()
        alive.assert_called_once_with(4242)
        self.assertEqual((payload["record"]["health"], payload["record"]["health_reason"]),
                         (orchestrator.STALE, "orchestrator pid 4242 exited"))

    def test_a_problem_is_told_once_and_its_end_too(self):
        with patch.object(self.host, "run_states", side_effect=OrchestratorError("ssh: connection refused")):
            self.assertEqual(self.watch.poll(), [("problem", {"error": "ssh: connection refused"})])
            self.assertEqual(self.watch.poll(), [])
        self.save()
        self.assertEqual([kind for kind, _ in self.watch.poll()], ["problem", "record", "files"])
        self.assertEqual(self.watch.poll(), [])


class TestServeCommand(unittest.TestCase):
    """orchestrator.py serve as a process of its own."""

    def serve(self):
        """The started serve, as a RunProcess, and its first line of output."""
        proc = orchestrator.RunProcess([sys.executable, orchestrator.__file__, "serve"])
        self.addCleanup(proc.proc.kill)
        out = ""
        deadline = time.monotonic() + 10
        while "\n" not in out and time.monotonic() < deadline:
            chunk, returncode = proc.read()
            self.assertIsNone(returncode, out)
            out += chunk
            time.sleep(0.05)
        line, _, rest = out.partition("\n")
        self.assertEqual(rest, "")
        return proc, json.loads(line)

    def test_one_json_line_then_serves_until_a_signal(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig):
                proc, line = self.serve()
                self.assertEqual(set(line), {"url", "token"})
                self.assertRegex(line["url"], r"^http://127\.0\.0\.1:\d+$")
                with tempfile.TemporaryDirectory() as d:
                    url = f"{line['url']}/runs?{urllib.parse.urlencode({'cwd': d, 'token': line['token']})}"
                    with OPENER.open(url, timeout=10) as resp:
                        self.assertEqual(json.load(resp), {"version": 1, "runs": []})
                proc.proc.send_signal(sig)
                self.assertEqual(proc.proc.wait(timeout=10), 0)
                # Nothing more on standard output or standard error, which RunProcess reads together.
                self.assertEqual(self.rest_of_output(proc), "")

    @staticmethod
    def rest_of_output(proc):
        out, returncode = proc.read()
        while returncode is None:
            time.sleep(0.05)
            chunk, returncode = proc.read()
            out += chunk
        return out

    def test_in_process(self):
        handlers = {}
        with patch("signal.signal", side_effect=handlers.setdefault), \
                patch("builtins.print") as out:
            thread = threading.Thread(target=lambda: handlers.setdefault("status", orchestrator.serve_command([])))
            thread.start()
            deadline = time.monotonic() + 10
            while not out.called and time.monotonic() < deadline:
                time.sleep(0.01)
            line = json.loads(out.call_args.args[0])
            with tempfile.TemporaryDirectory() as d:
                url = f"{line['url']}/runs?{urllib.parse.urlencode({'cwd': d, 'token': line['token']})}"
                with OPENER.open(url, timeout=10) as resp:
                    self.assertEqual(resp.status, 200)
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            thread.join(timeout=10)
        self.assertEqual(handlers["status"], 0)
        self.assertEqual(out.call_args.kwargs, {"flush": True})

    def test_a_closed_stdout_ends_it(self):
        # serve's standard output is a pipe whose reader has gone, as when the app quit before reading the line.
        script = textwrap.dedent(f"""
            import os, subprocess, sys
            r, w = os.pipe()
            os.close(r)
            proc = subprocess.Popen([sys.executable, {orchestrator.__file__!r}, "serve"], stdout=w,
                                    stderr=subprocess.PIPE, text=True)
            os.close(w)
            try:
                _, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                print("hung")
            else:
                print(proc.returncode)
                print(err, end="")
        """)
        out = Host().check([sys.executable, "-c", script], timeout=30)
        self.assertEqual(out, f"{orchestrator.EXIT_ERROR}\n"
                              "error: cannot print the server's address: [Errno 32] Broken pipe\n")

    def test_an_unprintable_line_ends_it(self):
        handlers = {}

        def broken_stdout(text, file=None, flush=False):
            if file is None:
                raise BrokenPipeError(32, "Broken pipe")
            file.write(f"{text}\n")

        with patch("signal.signal", side_effect=handlers.setdefault), patch("builtins.print", side_effect=broken_stdout), \
                patch("sys.stderr", new_callable=io.StringIO) as err, patch("os.open", return_value=99) as devnull, \
                patch("os.dup2") as dup2, patch("sys.stdout") as stdout:
            stdout.fileno.return_value = 1
            started = time.monotonic()
            self.assertEqual(orchestrator.serve_command([]), orchestrator.EXIT_ERROR)
        self.assertLess(time.monotonic() - started, 5)
        devnull.assert_called_once_with(os.devnull, os.O_WRONLY)
        dup2.assert_called_once_with(99, 1)
        self.assertEqual(err.getvalue(), "error: cannot print the server's address: [Errno 32] Broken pipe\n")

    def test_no_port(self):
        with patch.object(orchestrator, "RunServer", side_effect=OSError("Address already in use")), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(orchestrator.serve_command([]), orchestrator.EXIT_ERROR)
        self.assertEqual(err.getvalue(), "error: cannot serve on 127.0.0.1: Address already in use\n")

    def test_needs_no_herdr_pane(self):
        with patch.dict("os.environ", {"HERDR_ENV": ""}), \
                patch.object(orchestrator, "serve_command", return_value=0) as serve, \
                patch.object(orchestrator, "connect", side_effect=AssertionError("connect was called")):
            self.assertEqual(main(["serve", "--origin", TAURI_ORIGIN]), 0)
        serve.assert_called_once_with([TAURI_ORIGIN])


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

INTEGRATIONS_CURRENT = "".join(f"{kind}: current (v3) (/home/u/.{kind}/herdr-agent-state.sh)\n"
                               for kind in orchestrator.AGENT_KINDS) + "letta (experimental): not installed (/l)\n"


class FakeTools:
    """A machine's commands, as Host.run and Herdr's run start them, with every tool doctor checks present; a test
    changes paths or answers to take one away. Over SSH it answers for targets, and records each command's
    target in calls."""

    def __init__(self, targets=("me@box",)):
        self.paths = {name: f"/usr/bin/{name}" for name in (*orchestrator.AGENT_KINDS, "herdr", "git", "gh")}
        self.answers = {
            ("herdr", "--version"): completed("herdr 0.9.1\n"),
            ("herdr", "integration", "status"): completed(INTEGRATIONS_CURRENT),
            ("git", "--version"): completed("git version 2.47.3\n"),
            ("gh", "auth", "status"): completed("github.com\n  ✓ Logged in to github.com\n"),
            ("gitleaks", "git", "--help"): completed("Usage: gitleaks git [flags]\n"),
            ("git", "-C", "/proj", "rev-parse", "--show-toplevel"): completed("/proj\n"),
            ("git", "-C", "/proj", "remote", "get-url", "origin"): completed("git@github.com:me/proj.git\n"),
            ("git", "-C", "/home/u/proj", "rev-parse", "--show-toplevel"): completed("/home/u/proj\n"),
            ("git", "-C", "/home/u/proj", "remote", "get-url", "origin"): completed("git@github.com:me/p.git\n"),
            ("herdr", "machine", "list", "--json"): completed(json.dumps(
                [{"label": "box", "id": "m1", "enabled": True, "target": "me@box"},
                 {"label": "off", "id": "m2", "enabled": False, "target": "me@off"}])),
        }
        self.targets = targets
        self.dirs = {"/proj", "/home/u/proj"}  # the directories Host.resolve_dir finds
        self.calls = []  # (SSH target or None, argv)

    def __call__(self, argv, **kwargs):
        target = None
        if argv[0] == "ssh":
            target, argv = argv[3], shlex.split(argv[4])
            if target not in self.targets:
                return completed(stderr=f"ssh: connect to host {target}: Connection refused", returncode=255)
        self.calls.append((target, argv))
        if argv == ["true"]:
            return completed()
        if argv[:2] == ["sh", "-c"] and "command -v" in argv[2]:
            path = self.paths.get(argv[-1])
            return completed(f"{path}\n") if path else completed(returncode=1)
        if argv[:2] == ["sh", "-c"] and argv[2].endswith("&& pwd"):  # Host.resolve_dir, home being /home/u
            path = ("/home/u" if "$HOME" in argv[2] else "") + argv[-1]
            return completed(f"{path}\n") if path in self.dirs else completed(stderr="cd: no such dir", returncode=2)
        if argv[0] not in self.paths and argv[0] != "gitleaks":
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        answer = self.answers[tuple(argv)]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def with_tkinter():
    tk = ModuleType("tkinter")
    tk.TkVersion = 8.6
    return patch.dict("sys.modules", {"tkinter": tk})


@patch.dict("os.environ", {"HERDR_ENV": "1"})
class TestCheckSetup(unittest.TestCase):
    def check(self, tools, machine=None, cwd=None):
        with with_tkinter():
            checks = orchestrator.check_setup(Host(run=tools), Herdr(machine, run=tools), cwd)
        return {c.name: c for c in checks}

    def report(self, checks):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            doctor_exit = orchestrator.report_checks(list(checks.values()))
        return out.getvalue(), doctor_exit

    def test_all_present(self):
        checks = self.check(FakeTools(), cwd="/proj")
        kinds = orchestrator.AGENT_KINDS
        self.assertEqual(list(checks), [
            "herdr", "reaching herdr",
            *(f"{kind} {what}" for kind in kinds for what in ("CLI", "integration")),
            "git", "gh", "gitleaks", "git checkout", "origin", "tkinter"])
        self.assertEqual([c.name for c in checks.values() if not c.ok], [])
        self.assertEqual(checks["herdr"].detail, "herdr 0.9.1")
        self.assertEqual(checks["claude integration"].detail, "current (v3)")
        self.assertEqual(checks["origin"].detail, "git@github.com:me/proj.git")
        out, doctor_exit = self.report(checks)
        self.assertEqual(doctor_exit, 0)
        self.assertTrue(all(line.startswith("ok      ") for line in out.splitlines()), out)
        self.assertIn("ok      herdr: herdr 0.9.1\n", out)

    def test_levels(self):
        checks = self.check(FakeTools(), cwd="/proj")
        self.assertEqual({name: c.level for name, c in checks.items() if c.level != orchestrator.OPTIONAL}, {
            "herdr": "required", "reaching herdr": "required", "claude CLI": "required",
            "claude integration": "required", "git": "required", "gh": "needed-for-pr", "gitleaks": "needed-for-pr",
            "git checkout": "needed-for-pr", "origin": "needed-for-pr"})

    def test_without_herdr(self):
        tools = FakeTools()
        del tools.paths["herdr"]
        checks = self.check(tools)
        self.assertEqual(checks["herdr"], orchestrator.Check(
            "herdr", "required", False, "herdr is not on PATH",
            "install herdr 0.9 or later, or update it with `herdr update`"))
        self.assertEqual(checks["claude integration"].detail, "skipped: needs herdr")
        self.assertEqual(checks["codex integration"].detail,
                         "skipped: needs herdr; needed only for a role that runs in codex")
        self.assertTrue(checks["git"].ok and checks["gh"].ok and checks["claude CLI"].ok)
        self.assertNotIn((None, ["herdr", "integration", "status"]), tools.calls)
        out, doctor_exit = self.report(checks)
        self.assertEqual(doctor_exit, orchestrator.EXIT_ERROR)
        self.assertIn("missing herdr: herdr is not on PATH\n"
                      "        fix: install herdr 0.9 or later, or update it with `herdr update`\n", out)
        self.assertIn("missing claude integration: skipped: needs herdr\n        fix: fix herdr first\nok      codex CLI",
                      out)

    def test_old_herdr(self):
        tools = FakeTools()
        tools.answers["herdr", "--version"] = completed("herdr 0.8.4\n")
        herdr = self.check(tools)["herdr"]
        self.assertEqual((herdr.ok, herdr.detail), (False, "herdr 0.8.4 is older than 0.9"))
        for said, ok in (("herdr 0.9\n", True), ("herdr 0.10.0\n", True), ("herdr 1.0.0\n", True),
                         ("herdr dev\n", False)):
            with self.subTest(said=said):
                tools.answers["herdr", "--version"] = completed(said)
                self.assertEqual(self.check(tools)["herdr"].ok, ok)

    def test_integrations_not_installed(self):
        tools = FakeTools()
        tools.answers["herdr", "integration", "status"] = completed(
            INTEGRATIONS_CURRENT.replace("claude: current (v3)", "claude: not installed")
            .replace("codex: current (v3)", "codex: not installed")
            .replace("gemini: current (v3)", "gemini: outdated (v1)"))
        checks = self.check(tools, cwd="/proj")
        self.assertEqual(checks["claude integration"], orchestrator.Check(
            "claude integration", "required", False, "not installed", "herdr integration install claude"))
        self.assertEqual(checks["codex integration"], orchestrator.Check(
            "codex integration", "optional", False, "not installed; needed only for a role that runs in codex",
            "herdr integration install codex"))
        self.assertEqual(checks["gemini integration"].detail,
                         "outdated (v1); needed only for a role that runs in gemini")
        self.assertEqual([c.name for c in checks.values() if not c.ok],
                         ["claude integration", "codex integration", "gemini integration"])
        self.assertEqual(tools.calls.count((None, ["herdr", "integration", "status"])), 1)
        out, doctor_exit = self.report(checks)
        self.assertEqual(doctor_exit, orchestrator.EXIT_ERROR)
        self.assertIn("missing claude integration: not installed\n        fix: herdr integration install claude\n", out)
        self.assertIn("warn    codex integration: not installed;", out)

    def test_optional_failures_alone_pass(self):
        tools = FakeTools()
        del tools.paths["codex"]
        tools.answers["herdr", "integration", "status"] = completed(
            INTEGRATIONS_CURRENT.replace("pi: current", "pi: x"))
        checks = self.check(tools)
        self.assertEqual(checks["codex CLI"].fix, "install the codex CLI where the agents run")
        self.assertEqual(self.report(checks)[1], 0)

    def test_integration_unlisted_or_unasked(self):
        tools = FakeTools()
        tools.answers["herdr", "integration", "status"] = completed("claude: current (v3) (/c)\n")
        self.assertEqual(self.check(tools)["pi integration"].detail,
                         "herdr integration status does not list pi; needed only for a role that runs in pi")
        tools.answers["herdr", "integration", "status"] = completed(stderr="unknown command", returncode=2)
        checks = self.check(tools)
        self.assertIn("herdr integration status failed: unknown command", checks["claude integration"].detail)
        for kind in orchestrator.AGENT_KINDS:
            self.assertEqual(checks[f"{kind} integration"].fix,
                             "update herdr with `herdr update`, then check that `herdr integration status` runs")

    def test_integration_states(self):
        self.assertEqual(orchestrator.integration_states(
            "pi: current (v9) (/home/u/.pi/a.ts)\n"
            "codex: not installed (/home/u/.codex/a.sh)\n"
            "letta (experimental): not installed (/home/u/.letta/a.sh)\n"
            "gemini: outdated (v2, current is v3) (/g)\n"
            "\n"
            "noise\n"), {"pi": "current (v9)", "codex": "not installed", "letta": "not installed",
                         "gemini": "outdated (v2, current is v3)"})

    def test_git(self):
        tools = FakeTools()
        for said, ok in (("git version 2.36.0\n", True), ("git version 2.39.3 (Apple Git-145)\n", True),
                         ("git version 2.35.9\n", False)):
            with self.subTest(said=said):
                tools.answers["git", "--version"] = completed(said)
                self.assertEqual(self.check(tools)["git"].ok, ok)
        git = self.check(tools)["git"]
        self.assertEqual((git.detail, git.fix), ("git 2.35.9 is older than 2.36", "install git 2.36 or later"))
        del tools.paths["git"]
        checks = self.check(tools, cwd="/proj")
        self.assertEqual(checks["git"].detail, "git is not on PATH")
        self.assertEqual(checks["git checkout"].detail, "skipped: needs git")
        self.assertEqual(checks["origin"].detail, "skipped: needs git checkout")

    def test_gh_logged_out(self):
        tools = FakeTools()
        tools.answers["gh", "auth", "status"] = completed(
            stderr="You are not logged into any GitHub hosts. To log in, run: gh auth login\n", returncode=1)
        checks = self.check(tools)
        self.assertEqual(checks["gh"], orchestrator.Check(
            "gh", "needed-for-pr", False,
            "gh auth status fails: You are not logged into any GitHub hosts. To log in, run: gh auth login",
            "gh auth login"))
        self.assertTrue(checks["gitleaks"].ok and checks["tkinter"].ok)
        self.assertEqual(self.report(checks)[1], orchestrator.EXIT_ERROR)
        tools.answers["gh", "auth", "status"] = completed(
            "github.com\n  X Failed to log in to github.com using token (GH_TOKEN)\n"
            "  - The token in GH_TOKEN is invalid.\n", returncode=1)
        self.assertEqual(self.check(tools)["gh"].detail,
                         "gh auth status fails: github.com X Failed to log in to github.com using token (GH_TOKEN) "
                         "- The token in GH_TOKEN is invalid.")
        del tools.paths["gh"]
        gh = self.check(tools)["gh"]
        self.assertEqual((gh.detail, gh.fix), ("gh is not on PATH", "install gh (https://cli.github.com)"))

    def test_gh_times_out(self):
        tools = FakeTools()
        tools.answers["gh", "auth", "status"] = subprocess.TimeoutExpired(["gh"], orchestrator.NETWORK_TIMEOUT)
        checks = self.check(tools)
        self.assertFalse(checks["gh"].ok)
        self.assertIn("timed out", checks["gh"].detail)
        self.assertTrue(checks["gitleaks"].ok)

    def test_gitleaks(self):
        tools = FakeTools()
        tools.answers["gitleaks", "git", "--help"] = completed(stderr="unknown command \"git\"", returncode=1)
        checks = self.check(tools)
        self.assertEqual(checks["gitleaks"], orchestrator.Check(
            "gitleaks", "needed-for-pr", False, "`gitleaks git` does not run", orchestrator.GITLEAKS_INSTALL))
        self.assertIn("https://github.com/gitleaks/gitleaks#installing", checks["gitleaks"].fix)
        self.assertEqual(self.report(checks)[1], orchestrator.EXIT_ERROR)
        tools.answers["gitleaks", "git", "--help"] = FileNotFoundError(2, "No such file or directory", "gitleaks")
        self.assertTrue(self.check(tools)["gitleaks"].detail.startswith("gitleaks cannot be started ("))

    def test_not_a_checkout(self):
        tools = FakeTools()
        tools.answers["git", "-C", "/proj", "rev-parse", "--show-toplevel"] = completed(
            stderr="fatal: not a git repository (or any of the parent directories): .git\nmore\n", returncode=128)
        checks = self.check(tools, cwd="/proj")
        self.assertEqual(checks["git checkout"].detail,
                         "/proj is not in a git checkout: fatal: not a git repository (or any of the parent "
                         "directories): .git")
        self.assertEqual(checks["origin"], orchestrator.Check("origin", "needed-for-pr", False,
                                                              "skipped: needs git checkout", "fix git checkout first"))
        self.assertNotIn((None, ["git", "-C", "/proj", "remote", "get-url", "origin"]), tools.calls)

    def test_no_origin(self):
        tools = FakeTools()
        tools.answers["git", "-C", "/proj", "remote", "get-url", "origin"] = completed(
            stderr="error: No such remote 'origin'", returncode=2)
        origin = self.check(tools, cwd="/proj")["origin"]
        self.assertFalse(origin.ok)
        self.assertEqual(origin.fix, "git remote add origin <url>")

    def test_cwd_resolved_as_a_run_resolves_it(self):
        tools = FakeTools()
        checks = self.check(tools, machine="box", cwd="~/proj")
        self.assertEqual((checks["git checkout"].detail, checks["origin"].detail),
                         ("/home/u/proj", "git@github.com:me/p.git"))
        self.assertIn(("me@box", ["sh", "-c", 'cd -- "$HOME$1" && pwd', "_", "/proj"]), tools.calls)
        checkout = self.check(tools, cwd="/gone")["git checkout"]
        self.assertEqual((checkout.ok, checkout.detail.split(":")[0]), (False, "/gone is not a directory"))

    def test_without_cwd_the_project_is_not_checked(self):
        self.assertNotIn("git checkout", self.check(FakeTools()))

    @patch.dict("os.environ", {"HERDR_ENV": ""})
    def test_outside_a_herdr_pane(self):
        checks = self.check(FakeTools())
        self.assertEqual(checks["reaching herdr"], orchestrator.Check(
            "reaching herdr", "required", False, "not inside a herdr pane", "run from a herdr pane, or pass --machine"))
        self.assertEqual([c.name for c in checks.values() if not c.ok], ["reaching herdr"])
        out, doctor_exit = self.report(checks)
        self.assertEqual(doctor_exit, orchestrator.EXIT_ERROR)
        self.assertIn("missing reaching herdr: not inside a herdr pane\n"
                      "        fix: run from a herdr pane, or pass --machine\n", out)

    @patch.dict("os.environ", {"HERDR_ENV": ""})
    def test_on_a_machine(self):
        tools = FakeTools()
        checks = self.check(tools, machine="box", cwd="/proj")
        self.assertEqual([c.name for c in checks.values() if not c.ok], [])
        self.assertEqual(checks["reaching herdr"].detail, "herdr machine box, over SSH to me@box")
        self.assertEqual((checks["herdr"].detail, checks["herdr on box"].detail), ("herdr 0.9.1", "herdr 0.9.1"))
        # Only the herdr a run drives is checked here; everything else goes over SSH to the machine.
        local = [argv for target, argv in tools.calls if target is None]
        self.assertEqual(local, [["sh", "-c", 'command -v -- "$1"', "_", "herdr"], ["herdr", "--version"],
                                 ["herdr", "machine", "list", "--json"]])
        remote = {tuple(argv) for target, argv in tools.calls if target == "me@box"}
        for argv in (("herdr", "--version"), ("herdr", "integration", "status"), ("git", "--version"),
                     ("gh", "auth", "status"), ("gitleaks", "git", "--help"),
                     ("git", "-C", "/proj", "rev-parse", "--show-toplevel"),
                     ("sh", "-c", 'command -v -- "$1"', "_", "codex")):
            self.assertIn(argv, remote)

    def test_machine_not_saved_disabled_or_unreachable(self):
        tools = FakeTools(targets=())
        for machine, detail in (("nowhere", "no saved herdr machine named nowhere"),
                                ("off", "herdr machine off is disabled"),
                                ("box", "true failed on me@box: ssh: connect to host me@box: Connection refused")):
            with self.subTest(machine=machine):
                checks = self.check(tools, machine=machine, cwd="/proj")
                reach = checks["reaching herdr"]
                self.assertEqual((reach.ok, reach.detail), (False, detail))
                self.assertIn(machine, reach.fix)
                self.assertTrue(checks["herdr"].ok)
                self.assertEqual(checks["herdr on " + machine].detail, "skipped: needs reaching herdr")
                self.assertEqual(checks["gh"].detail, "skipped: needs reaching herdr")
                self.assertEqual(checks["origin"].detail, "skipped: needs git checkout")
                self.assertTrue(checks["tkinter"].ok)
                out, doctor_exit = self.report(checks)
                self.assertEqual(doctor_exit, orchestrator.EXIT_ERROR)
                self.assertIn(f"missing reaching herdr: {detail}\n", out)

    def test_machine_without_local_herdr(self):
        tools = FakeTools()
        del tools.paths["herdr"]
        checks = self.check(tools, machine="box")
        self.assertEqual((checks["herdr"].detail, checks["herdr"].fix),
                         ("herdr is not on PATH", "install herdr 0.9 or later, or update it with `herdr update`"))
        self.assertEqual((checks["reaching herdr"].detail, checks["reaching herdr"].fix),
                         ("skipped: needs herdr", "fix herdr first"))
        self.assertNotIn((None, ["herdr", "machine", "list", "--json"]), tools.calls)

    def test_machine_herdr_versions_checked_apart(self):
        # The herdr here drives the run and the one on the machine hosts its agents; each must be recent enough.
        for local, remote, old in (("0.8.4", "0.9.1", "herdr"), ("0.9.1", "0.8.4", "herdr on box")):
            with self.subTest(local=local, remote=remote):
                tools = FakeTools()
                versions = {None: f"herdr {local}\n", "me@box": f"herdr {remote}\n"}

                def answer(argv, run=tools.__call__, versions=versions, **kwargs):
                    if argv == ["herdr", "--version"] or argv[-1:] == ["herdr --version"]:
                        return completed(versions[None if argv[0] == "herdr" else argv[3]])
                    return run(argv, **kwargs)

                with with_tkinter():
                    checks = {c.name: c for c in orchestrator.check_setup(Host(run=answer), Herdr("box", run=answer))}
                self.assertEqual(checks[old].detail, "herdr 0.8.4 is older than 0.9")
                self.assertEqual([checks[n].ok for n in ("herdr", "reaching herdr", "herdr on box")],
                                 [False, False, False] if old == "herdr" else [True, True, False])

    def test_without_tkinter(self):
        for platform, hint in (("linux", "apt install python3-tk"), ("darwin", "brew install python-tk@")):
            with self.subTest(platform=platform), patch.dict("sys.modules", {"tkinter": None}), \
                    patch.object(sys, "platform", platform):
                checks = orchestrator.check_setup(Host(run=FakeTools()), Herdr(run=FakeTools()))
                tk = checks[-1]
                self.assertEqual((tk.name, tk.level, tk.ok), ("tkinter", "optional", False))
                self.assertIn(hint, tk.fix)
                with patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(orchestrator.report_checks(checks), 0)
                self.assertIn("warn    tkinter: this Python lacks it; needed only for the GUI\n", out.getvalue())

    def test_commands_get_the_child_env(self):
        tools = MagicMock(wraps=FakeTools())
        with patch.dict("os.environ", {"JENKINS_TOKEN": "secret"}), with_tkinter():
            orchestrator.check_setup(Host(run=tools), Herdr("box", run=tools), "/proj")
        self.assertTrue(tools.call_args_list)
        for call in tools.call_args_list:
            self.assertNotIn("JENKINS_TOKEN", call.kwargs["env"])


class TestDoctorCommand(unittest.TestCase):
    def test_needs_no_herdr_pane(self):
        checks = [orchestrator.Check("herdr", "required", True, "herdr 0.9.1")]
        with patch.dict("os.environ", {"HERDR_ENV": ""}), \
                patch.object(orchestrator, "check_setup", return_value=checks) as check, \
                patch.object(orchestrator, "connect", side_effect=AssertionError("connect was called")), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["doctor", "--machine", "box", "--cwd", "~/proj"]), 0)
        host, herdr, cwd = check.call_args.args
        self.assertEqual((host.ssh_target, herdr.machine, cwd), (None, "box", "~/proj"))
        self.assertEqual(out.getvalue(), "ok      herdr: herdr 0.9.1\n")

    def test_machine_needs_cwd(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["doctor", "--machine", "box"])
        self.assertIsNone(parse_args(["doctor"]).cwd)

    def test_report(self):
        checks = [orchestrator.Check("a", "required", True, "fine"),
                  orchestrator.Check("b", "optional", False, "gone", "get b"),
                  orchestrator.Check("c", "needed-for-pr", False, "skipped: needs a", "fix a first")]
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(orchestrator.report_checks(checks), orchestrator.EXIT_ERROR)
        self.assertEqual(out.getvalue(), "ok      a: fine\nwarn    b: gone\n        fix: get b\n"
                                         "missing c: skipped: needs a\n        fix: fix a first\n")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(orchestrator.report_checks(checks[:2]), 0)


if __name__ == "__main__":
    unittest.main()
