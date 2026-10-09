import base64
import io
import itertools
import json
import os
import re
import socket
import subprocess
import shlex
import struct
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.parse
from dataclasses import asdict
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import orchestrator
from orchestrator import (
    APPROVE, CHANGES_REQUESTED, CI, HEARTBEAT_SECONDS, STALE_SECONDS, CIError, Herdr, HerdrError, Host,
    OrchestratorError, RunState, RunTakenOver, Workflow, branch_name, changed_lines, main, parse_args,
    parse_gate, parse_verdict, pr_body, quality_report, spec_title,
)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def result(obj):
    return completed(json.dumps({"id": "cli:x", "result": obj}))


def no_processes(argv, **kw):
    raise AssertionError(f"FakeHost ran a real process: {argv}")


HOST_RUN_STATES = Host.run_states


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
    - `has_origin`: whether the checkout has an origin at all.
    - `ages`: the seconds since each run's state.json was written, by run id; 0 for one not listed.

    For prune, which also reads `remote`:

    - `branches`: the local branches besides the checked-out one, by name, with their commit.
    - `tracking`: the branches origin/<name> exists for.
    - `worktrees`: the linked worktrees' branches, by path; /proj has `branch` checked out.
    - `ancestors`: (commit, of) pairs where commit is an ancestor of of.
    - `ahead`: by branch, the commits it has beyond the commit rev-list --count compares it to.
    - `missing_commits`: commits this clone lacks.
    - `pull_requests`: what `gh pr view` prints, by URL: a dict, or a completed process for a failure.
    """

    def __init__(self, head="abc123", branch="main"):
        super().__init__(run=no_processes)
        self.files = {}
        self.head = head
        self.writes = []  # every path written, in order
        self.branch = branch
        self.changed = set()
        self.git_calls = []
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
        self.gh_calls = []
        self.mtime_kept = []  # every path written with keep_mtime

    def snapshot(self, cwd, parent, message):
        self.snapshots.append((parent, message))
        return f"snap{len(self.snapshots)}"

    def read(self, path):
        return self.files.get(path)

    def write(self, path, text, keep_mtime=False):
        self.writes.append(path)
        if keep_mtime:
            self.mtime_kept.append(path)
        self.files[path] = text
        if not path.startswith("/proj/.orchestrator/"):
            self.changed.add(path)

    def rename(self, path, new_path):
        self.files[new_path] = self.files.pop(path)

    def git_head(self, cwd):
        return self.head

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

    def git_run(self, cwd, *args, timeout=60):
        self.git_calls.append(args)
        match args:
            case ("rev-parse", "--abbrev-ref", "HEAD"):
                return completed(self.branch + "\n")
            case ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
                return completed(returncode=0 if self.merge_head else 1)
            case ("status", "--porcelain"):
                return completed("".join(f"?? {p}\n" for p in sorted(self.changed)))
            case ("switch", "-c", name) | ("switch", "--quiet", name):
                self.branch = name
            case ("commit", *_):
                self.changed.clear()
                self.head += "+commit"
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
                return completed("".join(f"worktree {path}\nHEAD {'0' * 40}\nbranch refs/heads/{b}\n\n"
                                         for path, b in trees.items()))
        return completed()

    def gh_run(self, cwd, *args):
        self.gh_calls.append(args)
        match args:
            case ("pr", "view", url, "--json", "state,headRefOid,closedAt"):
                pr = self.pull_requests[url]
                return pr if isinstance(pr, subprocess.CompletedProcess) else completed(json.dumps(pr))
        raise AssertionError(f"FakeHost got an unscripted gh command: {args}")

    def create_pr(self, cwd, base, head, title, body, draft):
        self.prs.append({"base": base, "head": head, "title": title, "body": body, "draft": draft})
        return "https://github.com/o/r/pull/7"


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

    def _role(self, name):
        return name.split("-", 1)[0]

    def create_workspace(self, cwd, label):
        self.calls.append(("workspace", label))
        self.workspaces.add("w1")
        return "w1", self._pane()

    def _pane(self):
        self.panes += 1
        pane = f"w1:p{self.panes}"
        self.live_panes.add(pane)
        return pane

    def split(self, pane, direction, cwd):
        self.calls.append(("split", pane, direction))
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
        self.assertEqual(host.git_calls[push - 2:push],
                         [self.FETCH, ("merge-base", "--is-ancestor", "origin/main", "HEAD")])
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
        body = pr_body(state, "spec", "r", "VERDICT: APPROVE")
        self.assertEqual(body.split("\n\n", 1)[0],
                         "> [!WARNING]\n> This branch conflicts with `main`, so this is a draft. "
                         "Merge `main` into it and resolve the conflicts in:\n> - `a.py`\n> - `docs/b.md`")

    def test_no_conflicts_no_warning(self):
        state = RunState("r", "t", "/proj", verdict=APPROVE, round=1, base_branch="main")
        self.assertTrue(pr_body(state, "spec", "r", "VERDICT: APPROVE").startswith("Opened by"))

    def test_long_sections_are_truncated(self):
        state = RunState("r", "t", "/proj", verdict=APPROVE, round=1)
        body = pr_body(state, "x" * (orchestrator.PR_SECTION_LIMIT * 2), "r", "VERDICT: APPROVE")
        self.assertLess(len(body), 65536)
        self.assertIn("truncated", body)


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

    def test_rename_over_ssh(self):
        run = MagicMock(return_value=completed())
        Host("remote-host", run=run).rename("/x/review-1.md", "/x/review 1.rejected.md")
        self.assertEqual(run.call_args.args[0],
                         ["ssh", "-o", "BatchMode=yes", "remote-host", "mv -f -- /x/review-1.md '/x/review 1.rejected.md'"])

    def test_rename_failure_raises(self):
        host = Host(run=MagicMock(return_value=completed(stderr="No such file or directory", returncode=1)))
        with self.assertRaisesRegex(OrchestratorError, "No such file or directory"):
            host.rename("/x/review-1.md", "/x/review-1.rejected.md")


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
SONAR = "https://sonar.example"
JENKINS_TOKEN = "jenkins-secret-token"
SONAR_TOKEN = "sonar-secret-token"
CREDENTIALS = {"JENKINS_URL": f"{JENKINS}/", "JENKINS_USER": "agents", "JENKINS_TOKEN": JENKINS_TOKEN,
               "SONAR_HOST_URL": SONAR, "SONAR_TOKEN": SONAR_TOKEN}
JOB = "AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality"
JOB_PATH = "/job/AI-Agents-Orchestrator/job/py-ai-agents-orchestrator-quality/"
PROJECT = "py-ai-agents-orchestrator-a1b2c3"
BASE_REF = "orchestrator-ci/a1b2c3-base"
REF_1_1 = "orchestrator-ci/a1b2c3-1-q1"


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


def outcome(gate="OK", *, report=True, result=None, task="SUCCESS", issues=(), conditions=(), tests=(),
            test_report=True, console="", coverage=None, polls=2):
    """What one Jenkins build does, and the SonarQube analysis it makes.

    report: whether it archives report-task.txt; result defaults to what the Jenkinsfile ends with.
    task: how its SonarQube task ends. tests: (className, name, errorDetails) of each failing test.
    polls: how many polls still see it building.
    """
    if result is None:
        result = ("SUCCESS" if gate == "OK" else "UNSTABLE") if report else "FAILURE"
    return dict(gate=gate, report=report, result=result, task=task, issues=list(issues),
                conditions=list(conditions), tests=list(tests), test_report=test_report, console=console,
                polls=polls,
                coverage=coverage or {"new_coverage": "87.5", "new_lines_to_cover": "8", "new_uncovered_lines": "1"})


RED = dict(gate="ERROR", conditions=[{"status": "ERROR", "metricKey": "new_coverage", "comparator": "LT",
                                      "errorThreshold": "80", "actualValue": "50.0"}])


def issue(path, line=None, message="fix this", severity="MAJOR", rule="python:S1"):
    out = {"component": f"{PROJECT}:{path}" if path else PROJECT, "message": message, "rule": rule,
           "severity": severity}
    if line is not None:
        out["line"] = line
    return out


class FakeCI:
    """Jenkins and SonarQube behind urlopen; each triggered build plays the next scripted outcome.

    - `base`: the outcome of a build of the base; `outcomes`: those of the change's builds in order, OK once
      they run out.
    - `down`: the servers, "jenkins" or "sonar", that do not answer.
    - `fail`: {(method, path): status} for requests answered with an HTTP error.
    - `job_gone`: the job was deleted; `forgotten`: Jenkins forgot its queue items.

    Every request is recorded as (method, url, form) in `requests`, and by a short name in `names`.
    """

    def __init__(self):
        self.requests, self.names, self.auth = [], [], []
        self.parameters = list(orchestrator.JOB_PARAMETERS)
        self.base = outcome()
        self.outcomes = []
        self.down = set()
        self.fail = {}
        self.job_gone = False
        self.forgotten = False
        self.builds = {}
        self.queue = {}
        self.tasks = {}
        self.projects = {orchestrator.SONAR_PROJECT}
        self.current = None  # the outcome whose issues and measures the project shows
        self.number = 40

    def add_build(self, ref, version, out):
        """A build as Jenkins has it; returns its number n, whose SonarQube task is task-<n>."""
        self.number += 1
        n = self.number
        self.builds[n] = {"ref": ref, "version": version, "left": out["polls"], **out}
        if out["report"]:
            self.tasks[f"task-{n}"] = {"left": 1, "status": out["task"], "analysis": f"analysis-{n}", "out": out}
        return n

    def triggered(self, version):
        return [b for b in self.builds.values() if b["version"] == version]

    def jenkins_requests(self):
        return [url for _, url, _ in self.requests if url.startswith(JENKINS)]

    def __call__(self, req, timeout=None):
        assert timeout == orchestrator.HTTP_TIMEOUT
        method, url = req.get_method(), req.full_url
        form = dict(urllib.parse.parse_qsl(req.data.decode())) if req.data else {}
        self.requests.append((method, url, form))
        self.auth.append(req.get_header("Authorization"))
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        server = "jenkins" if url.startswith(JENKINS) else "sonar"
        if server in self.down:
            # A reason that quotes a token, to show it never gets any further.
            self.names.append(f"{server} down")
            raise urllib.error.URLError(f"[Errno 111] Connection refused (token {JENKINS_TOKEN})")
        if (method, parts.path) in self.fail:
            self.names.append(f"{method} {parts.path} failing")
            return self.error(url, self.fail[(method, parts.path)])
        name, answer = (self.jenkins if server == "jenkins" else self.sonar)(method, parts.path, query, form)
        self.names.append(name)
        if isinstance(answer, int):
            return self.error(url, answer)
        if isinstance(answer, FakeResponse):
            return answer
        return FakeResponse(b"" if answer is None else json.dumps(answer))

    def error(self, url, status):
        body = json.dumps({"errors": [{"msg": f"refused with {status}"}]}).encode()
        raise urllib.error.HTTPError(url, status, "Error", {}, io.BytesIO(body))

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
            version = form["SONAR_PROJECT_VERSION"]
            out = self.base if version == "base" else (self.outcomes.pop(0) if self.outcomes else outcome())
            n = self.add_build(form["GIT_REF"], version, out)
            self.queue[900 + n] = {"left": 1, "number": n}
            return f"trigger {version}", FakeResponse(headers={"Location": f"{JENKINS}/queue/item/{900 + n}/"})
        n, _, what = rest.partition("/")
        build = self.builds.get(int(n)) if n.isdigit() else None
        if build is None:
            return f"{method} {path}", 404
        if what == "api/json":
            if build["left"]:
                build["left"] -= 1
                return "build", {"building": True, "result": None}
            return "build", {"building": False, "result": build["result"]}
        if what == "artifact/.scannerwork/report-task.txt":
            if not build["report"]:
                return "report", 404
            return "report", FakeResponse(f"projectKey={PROJECT}\nceTaskId=task-{n}\n")
        if what == "testReport/api/json":
            if not build["test_report"]:
                return "tests", 404
            cases = [{"className": c, "name": t, "status": "FAILED", "errorDetails": e} for c, t, e in build["tests"]]
            return "tests", {"suites": [{"cases": [*cases, {"className": "t", "name": "ok", "status": "PASSED"}]}]}
        if what == "consoleText":
            return "console", FakeResponse(build["console"])
        return f"{method} {path}", 404

    def sonar(self, method, path, query, form):
        match path:
            case "/api/authentication/validate":
                return "validate", {"valid": True}
            case "/api/qualitygates/get_by_project":
                return "template gate", {"qualityGate": {"name": "Sonar way", "default": True}}
            case "/api/projects/create":
                if form["project"] in self.projects:
                    return "create", 400
                self.projects.add(form["project"])
                return "create", {"project": {"key": form["project"]}}
            case "/api/components/show":
                return "show", {"component": {}} if query["component"] in self.projects else 404
            case "/api/qualitygates/select":
                return "select gate", None
            case "/api/new_code_periods/set":
                return f"new code {form['type']}", None
            case "/api/ce/task":
                task = self.tasks[query["id"]]
                if task["left"]:
                    task["left"] -= 1
                    return "task", {"task": {"id": query["id"], "status": "IN_PROGRESS"}}
                answer = {"id": query["id"], "status": task["status"]}
                if task["status"] == "SUCCESS":
                    answer["analysisId"] = task["analysis"]
                else:
                    answer["errorMessage"] = f"the report was rejected for {SONAR_TOKEN}"
                return "task", {"task": answer}
            case "/api/qualitygates/project_status":
                self.current = next(t["out"] for t in self.tasks.values() if t["analysis"] == query["analysisId"])
                return "gate", {"projectStatus": {"status": self.current["gate"],
                                                  "conditions": self.current["conditions"]}}
            case "/api/issues/search":
                found = self.current["issues"]
                size, page = int(query["ps"]), int(query["p"])
                return "issues", {"paging": {"pageIndex": page, "pageSize": size, "total": len(found)},
                                  "issues": found[(page - 1) * size:page * size]}
            case "/api/measures/component":
                measures = [{"metric": k, "period": {"index": 1, "value": v}}
                            for k, v in self.current["coverage"].items()]
                return "coverage", {"component": {"key": query["component"], "measures": measures}}
            case "/api/projects/delete":
                self.projects.discard(form["project"])
                return "delete project", None
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
    """A run saved in quality round q of round 1, its project and baseline in place."""
    kw.setdefault("quality_baseline", "analysis-base")
    return saved_run("quality", 1, agents=("spec", "build"), quality_job=JOB, quality_round=q,
                     quality_project=PROJECT, ci=ci or {}, **kw)


def resume_gated(state, script, fake, **kw):
    """resume with the gate on fake; the ref in flight, if any, is on origin."""
    host = kw.pop("host", None) or FakeHost()
    if state.ci.get("ref"):
        host.remote.setdefault(state.ci["ref"], state.ci["sha"])
    return resume(state, script, ci=ci_for(fake), max_quality_rounds=state.max_quality_rounds, host=host,
                  files={lambda s: s.build_path(1): "report 1", **kw.pop("files", {})}, **kw)


def leaks(text):
    return [t for t in (JENKINS_TOKEN, SONAR_TOKEN) if t in text]


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

        self.assertEqual(collapsed(fake.names), [
            "parameters", "validate",
            "template gate", "create", "select gate", "new code PREVIOUS_VERSION",
            "trigger base", "queue", "build", "report", "task",
            "trigger change", "queue", "build", "report", "task", "gate", "issues", "coverage", "tests",
            "delete project"])
        triggers = [form for method, url, form in fake.requests if url.endswith("/buildWithParameters")]
        self.assertEqual(triggers, [
            {"GIT_REF": BASE_REF, "SONAR_PROJECT_KEY": PROJECT, "SONAR_PROJECT_VERSION": "base"},
            {"GIT_REF": REF_1_1, "SONAR_PROJECT_KEY": PROJECT, "SONAR_PROJECT_VERSION": "change"}])
        self.assertIn(("push", "--quiet", "--force", "origin", f"abc123:refs/heads/{BASE_REF}"), host.git_calls)
        self.assertIn(("push", "--quiet", "--force", "origin", f"snap1:refs/heads/{REF_1_1}"), host.git_calls)
        self.assertEqual(host.snapshots[0][0], "abc123")
        state = wf.state
        self.assertTrue(host.files[state.quality_path(1, 1)].startswith("GATE: OK\n"))
        self.assertIn(orchestrator.QUALITY_PASSED_NOTE.format(quality_path=state.quality_path(1, 1)), seen[0])
        self.assertIn(f"The Builder's report is in {state.build_path(1)}", seen[0])
        self.assertIn("<summary>Quality gate (round 1)</summary>", host.prs[0]["body"])
        # done: the refs and the run's project are gone.
        self.assertEqual(host.remote, {})
        self.assertNotIn(PROJECT, fake.projects)
        saved = json.loads(host.files[f"{state.dir}/state.json"])
        self.assertEqual((saved["phase"], saved["quality_project"], saved["ci"]), ("done", None, {}))
        self.assertEqual(saved["quality_baseline"], "analysis-41")

    def test_credentials_travel_only_in_the_authorization_header(self):
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        wf.run()

        import base64
        jenkins = "Basic " + base64.b64encode(f"agents:{JENKINS_TOKEN}".encode()).decode()
        sonar = "Basic " + base64.b64encode(f"{SONAR_TOKEN}:".encode()).decode()
        for (method, url, form), auth in zip(fake.requests, fake.auth):
            self.assertEqual(auth, jenkins if url.startswith(JENKINS) else sonar)
            self.assertFalse(leaks(url + json.dumps(form)))
        for path, text in host.files.items():
            self.assertFalse(leaks(text), path)

    def test_jenkins_requests_go_under_the_jobs_folder(self):
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        wf.run()

        job = f"{JENKINS}{JOB_PATH}"
        urls = fake.jenkins_requests()
        # The queue items Jenkins names in Location are its own, outside any job.
        self.assertEqual([u for u in urls if not u.startswith(job)],
                         [u for u in urls if u.startswith(f"{JENKINS}/queue/item/")])
        self.assertTrue(any(u.startswith(job) for u in urls))

    def test_gate_fails_then_passes(self):
        host = FakeHost()
        host.diff = ("diff --git a/limiter.py b/limiter.py\n--- a/limiter.py\n+++ b/limiter.py\n"
                     "@@ -2 +2,2 @@\n-old\n+new\n+newer\n"
                     "diff --git a/new_module.py b/new_module.py\nnew file mode 100644\n--- /dev/null\n"
                     "+++ b/new_module.py\n@@ -0,0 +1 @@\n+x\n")
        fake = FakeCI()
        fake.outcomes = [outcome(**RED, issues=[
            issue("limiter.py", 9, "on a line the change did not add"),
            issue("limiter.py", 3, "on an added line"),
            issue("new_module.py", 1, "in a file the Builder created"),
            issue("limiter.py", None, "on a touched file"),
            issue("other.py", None, "on an untouched file"),
            issue(None, None, "on the project"),
        ]), outcome()]
        builds, reviews = [], []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), recording(fix_turn(1, 1), builds)],
            "review": [recording(review_turn(1, APPROVE), reviews)]}, fake=fake, host=host)

        self.assertEqual(wf.run(), APPROVE)

        state = wf.state
        quality = host.files[state.quality_path(1, 1)]
        self.assertTrue(quality.startswith("GATE: ERROR\n"))
        self.assertIn("1. `limiter.py (the whole file)` MAJOR python:S1: on a touched file\n"
                      "2. `limiter.py:3` MAJOR python:S1: on an added line\n"
                      "3. `new_module.py:1` MAJOR python:S1: in a file the Builder created\n", quality)
        self.assertIn("3 other open issues in the project are not on lines this change added", quality)
        self.assertNotIn("did not add", quality)
        self.assertIn("`new_coverage` is 50.0; the gate wants at least 80.", quality)
        self.assertEqual(builds, [orchestrator.QUALITY_FIX_PROMPT.format(
            quality_path=state.quality_path(1, 1), report_path=state.quality_build_path(1, 1))])
        self.assertIn(f"The Builder's report is in {state.quality_build_path(1, 1)}", reviews[0])
        self.assertIn(orchestrator.QUALITY_PASSED_NOTE.format(quality_path=state.quality_path(1, 2)), reviews[0])
        self.assertTrue(host.files[state.quality_path(1, 2)].startswith("GATE: OK"))
        self.assertEqual([b["ref"] for b in fake.triggered("change")], [REF_1_1, "orchestrator-ci/a1b2c3-1-q2"])
        self.assertEqual(len(fake.triggered("base")), 1)

    def test_rounds_run_out(self):
        fake = FakeCI()
        fake.outcomes = [outcome(**RED), outcome(**RED)]
        reviews = []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), fix_turn(1, 1)],
            "review": [recording(review_turn(1, APPROVE), reviews)]}, fake=fake, max_quality_rounds=2)

        self.assertEqual(wf.run(), APPROVE)

        self.assertEqual(herdr.calls.count(("prompt", "build-a1b2c3")), 2)
        state = wf.state
        self.assertIn(orchestrator.QUALITY_UNRESOLVED_NOTE.format(quality_path=state.quality_path(1, 2)), reviews[0])
        self.assertIn(f"The Builder's report is in {state.quality_build_path(1, 1)}", reviews[0])
        self.assertTrue(host.prs[0]["body"].count("GATE: ERROR"), 1)

    def test_a_later_round_has_its_own_quality_rounds_and_no_new_baseline(self):
        reviews = []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), build_turn(2)],
            "review": [review_turn(1, CHANGES_REQUESTED), recording(review_turn(2, APPROVE), reviews)]})

        self.assertEqual(wf.run(), APPROVE)

        self.assertIn(("push", "--quiet", "--force", "origin", "snap2:refs/heads/orchestrator-ci/a1b2c3-2-q1"),
                      host.git_calls)
        self.assertEqual(len(fake.triggered("base")), 1)
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
        env = {k: v for k, v in CREDENTIALS.items() if k not in ("JENKINS_USER", "SONAR_TOKEN")}
        fake = self.preflight_fails("the quality gate needs JENKINS_USER, SONAR_TOKEN in", env=env)
        self.assertEqual(fake.requests, [])

    def test_unreachable_jenkins_fails_before_the_interview(self):
        fake = FakeCI()
        fake.down = {"jenkins"}
        self.preflight_fails("Jenkins at JENKINS_URL failed the preflight: GET .*Connection refused", fake=fake)

    def test_unreachable_sonarqube_fails_before_the_interview(self):
        fake = FakeCI()
        fake.down = {"sonar"}
        self.preflight_fails("SonarQube at SONAR_HOST_URL failed the preflight", fake=fake)

    def test_job_without_the_parameters_fails_before_the_interview(self):
        fake = FakeCI()
        fake.parameters = ["GIT_REF"]
        self.preflight_fails("lacks the parameters SONAR_PROJECT_KEY, SONAR_PROJECT_VERSION; build it once by hand",
                             fake=fake)

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
        self.assertNotIn(PROJECT, fake.projects)

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
            if fake.triggered("change") and not down_at:
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
        saved_text = host.files[f"{wf.state.dir}/state.json"]
        saved = json.loads(saved_text)
        self.assertEqual(saved["ci"], {"ref": REF_1_1, "sha": "snap1", "queue_url": f"{JENKINS}/queue/item/942/"})
        self.assertEqual(saved["phase"], "quality")
        for path, text in host.files.items():
            self.assertFalse(leaks(text), path)
        # A failed run keeps its project and the ref in flight, for a resume.
        self.assertIn(PROJECT, fake.projects)
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
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        self.assertLessEqual(max(gaps), HEARTBEAT_SECONDS + orchestrator.CI_POLL_SECONDS)

    def test_build_failed_before_the_analysis_goes_back_to_the_builder(self):
        fake = FakeCI()
        console = "\n".join(f"line {i}" for i in range(100)) + f"\nSONAR_TOKEN={SONAR_TOKEN}\nSyntaxError: bad"
        fake.outcomes = [outcome(report=False, console=console, test_report=False), outcome()]
        builds = []
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), recording(fix_turn(1, 1), builds)],
            "review": [review_turn(1, APPROVE)]}, fake=fake)

        self.assertEqual(wf.run(), APPROVE)
        quality = host.files[wf.state.quality_path(1, 1)]
        self.assertTrue(quality.startswith("GATE: BUILD_FAILED\n"))
        self.assertIn("ended FAILURE before the SonarQube analysis", quality)
        self.assertIn("SONAR_TOKEN=****\nSyntaxError: bad", quality)
        self.assertNotIn("line 41\n", quality)
        self.assertIn("````\nline 42\n", quality)
        self.assertFalse(leaks(quality))
        self.assertIn(wf.state.quality_path(1, 1), builds[0])
        # Only the second change build was analysed.
        self.assertEqual(fake.names.count("gate"), 1)

    def test_build_failed_with_failing_tests_has_no_console(self):
        fake = FakeCI()
        fake.outcomes = [outcome(report=False, console="x", tests=[("tests.t", "test_a", "AssertionError: 1 != 2")])]
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1), fix_turn(1, 1)], "review": [review_turn(1, APPROVE)]},
            fake=fake)

        wf.run()
        quality = host.files[wf.state.quality_path(1, 1)]
        self.assertIn("- `tests.t.test_a`: AssertionError: 1 != 2", quality)
        self.assertNotIn("console", quality)
        self.assertNotIn("console", fake.names)

    def test_base_build_failed_fails_the_run(self):
        fake = FakeCI()
        fake.base = outcome(report=False)
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)]}, fake=fake)

        with self.assertRaisesRegex(OrchestratorError, "build #41 of the base commit abc123 ended FAILURE"):
            wf.run()
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual(saved["ci"], {"ref": BASE_REF, "sha": "abc123", "rejected": [41]})
        self.assertIsNone(saved["quality_baseline"])

        # Once the base builds, a resume triggers a new build rather than adopting the failed one.
        fake.base = outcome()
        wf2, herdr2, *_ = resume_gated(RunState.from_dict(saved), {"review": [review_turn(1, APPROVE)]}, fake,
                                       host=host)
        self.assertEqual(wf2.run(), APPROVE)
        self.assertEqual(len(fake.triggered("base")), 2)

    def test_aborted_build_without_a_report_fails_the_run(self):
        fake = FakeCI()
        fake.outcomes = [outcome(report=False, result="ABORTED")]
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)]}, fake=fake)

        with self.assertRaisesRegex(OrchestratorError, f"build #42 of {REF_1_1} was aborted before"):
            wf.run()
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual(saved["ci"], {"ref": REF_1_1, "sha": "snap1", "rejected": [42]})
        self.assertNotIn(wf.state.quality_path(1, 1), host.files)
        self.assertEqual(herdr.calls.count(("prompt", "build-a1b2c3")), 1)

    def test_report_is_followed_whatever_the_result(self):
        for result in ("FAILURE", "ABORTED"):
            with self.subTest(result=result):
                fake = FakeCI()
                fake.outcomes = [outcome(result=result)]
                wf, herdr, host, fake, _ = gated({
                    "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, fake=fake)
                self.assertEqual(wf.run(), APPROVE)
                self.assertTrue(host.files[wf.state.quality_path(1, 1)].startswith("GATE: OK"))

    def test_failed_sonarqube_task_fails_the_run(self):
        fake = FakeCI()
        fake.outcomes = [outcome(task="FAILED")]
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)]}, fake=fake)

        with self.assertRaisesRegex(OrchestratorError, "SonarQube task task-42 of Jenkins build #42 ended FAILED: "
                                    r"the report was rejected for \*\*\*\*"):
            wf.run()
        saved = json.loads(host.files[f"{wf.state.dir}/state.json"])
        self.assertEqual(saved["ci"], {"ref": REF_1_1, "sha": "snap1", "rejected": [42]})

    def test_queue_item_cancelled_fails_the_run(self):
        fake = FakeCI()
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)]}, fake=fake)
        orig = fake.jenkins

        def cancelled(method, path, query, form):
            if path.startswith("/queue/item/") and fake.triggered("change"):
                return "queue", {"cancelled": True}
            return orig(method, path, query, form)
        fake.jenkins = cancelled

        with self.assertRaisesRegex(OrchestratorError, "cancelled in Jenkins's queue"):
            wf.run()
        self.assertEqual(json.loads(host.files[f"{wf.state.dir}/state.json"])["ci"], {"ref": REF_1_1, "sha": "snap1"})

    def test_failed_cleanup_is_only_logged(self):
        fake = FakeCI()
        fake.fail[("POST", "/api/projects/delete")] = 403
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]}, fake=fake)
        host.remote["orchestrator-ci/a1b2c3-1-q7"] = "left-over"
        host.remote["orchestrator-ci/ffffff-1-q1"] = "another-run"

        with patch("sys.stderr") as err:
            self.assertEqual(wf.run(), APPROVE)
        self.assertIn("could not delete SonarQube project", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertEqual(wf.state.phase, "done")
        self.assertEqual(wf.state.quality_project, PROJECT)
        self.assertEqual(host.remote, {"orchestrator-ci/ffffff-1-q1": "another-run"})


    def test_refused_request_mid_round_fails_at_once_naming_the_step(self):
        fake = FakeCI()
        fake.fail[("GET", "/api/issues/search")] = 403
        wf, herdr, host, fake, _ = gated({"spec": [spec_turn], "build": [build_turn(1)]}, fake=fake)

        with self.assertRaisesRegex(OrchestratorError, f"quality gate: reading the issues of {PROJECT}: "
                                    r"GET .*/api/issues/search\?.*: HTTP 403 \(refused with 403\)"):
            wf.run()
        self.assertLess(wf.clock.now, 200)
        self.assertEqual(json.loads(host.files[f"{wf.state.dir}/state.json"])["ci"]["ce_task"], "task-42")

    def test_cleanup_without_origin_listing_is_only_logged(self):
        wf, herdr, host, fake, _ = gated({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)]})
        host.remote_branches = MagicMock(side_effect=OrchestratorError("ssh: connection reset"))

        with patch("sys.stderr") as err:
            self.assertEqual(wf.run(), APPROVE)
        self.assertIn("could not list the orchestrator-ci/ branches", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertNotIn(PROJECT, fake.projects)


class TestQualityResume(unittest.TestCase):
    def test_resume_with_a_build_only_polls_it(self):
        fake = FakeCI()
        n = fake.add_build(REF_1_1, "change", outcome())
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/", "build": n})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        self.assertFalse(host.snapshots)
        self.assertFalse([c for c in host.git_calls if c[:3] == ("push", "--quiet", "--force")])
        self.assertNotIn("trigger change", fake.names)
        self.assertNotIn("queue", fake.names)
        self.assertEqual(collapsed(fake.names)[:3], ["build", "report", "task"])
        self.assertIn(f"`snap9`", host.files[state.quality_path(1, 1)])

    def test_resume_with_a_task_asks_jenkins_only_for_the_test_report(self):
        fake = FakeCI()
        n = fake.add_build(REF_1_1, "change", outcome())
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/",
                                  "build": n, "ce_task": f"task-{n}"})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.jenkins_requests(),
                         [f"{JENKINS}{JOB_PATH}{n}/testReport/api/json?tree="
                          "suites%5Bcases%5BclassName%2Cname%2Cstatus%2CerrorDetails%5D%5D"])

    def test_resume_with_only_the_ref_adopts_its_build(self):
        fake = FakeCI()
        n = fake.add_build(REF_1_1, "change", outcome())
        fake.add_build("orchestrator-ci/a1b2c3-1-q2", "change", outcome())
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9"})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn("trigger change", fake.names)
        self.assertFalse(host.snapshots)
        self.assertIn(f"Jenkins build #{n}", host.files[state.quality_path(1, 1)])

    def test_resume_with_only_the_ref_and_no_build_triggers_one(self):
        fake = FakeCI()
        state = quality_state(ci={"ref": REF_1_1, "sha": "snap9"})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(collapsed(fake.names)[:2], ["find", "trigger change"])
        self.assertFalse(host.snapshots)

    def test_forgotten_queue_item_finds_its_build(self):
        fake = FakeCI()
        n = fake.add_build(REF_1_1, "change", outcome())
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

    def test_a_leftover_ref_of_another_round_is_dropped(self):
        fake = FakeCI()
        state = quality_state(q=2, ci={"ref": REF_1_1, "sha": "snap9", "queue_url": f"{JENKINS}/queue/item/1/"})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake,
                                          files={lambda s: s.quality_build_path(1, 1): "report 1 q1"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertNotIn(REF_1_1, host.remote)
        self.assertEqual([b["ref"] for b in fake.triggered("change")], ["orchestrator-ci/a1b2c3-1-q2"])

    def test_resume_with_the_quality_file_makes_no_request(self):
        fake = FakeCI()
        state = quality_state()
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake,
                                          files={lambda s: s.quality_path(1, 1): "GATE: OK\n"})
        fake.fail[("POST", "/api/projects/delete")] = 500  # done's cleanup is not the round's

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.names, ["POST /api/projects/delete failing"])

    def test_quality_file_without_a_gate_is_refused(self):
        fake = FakeCI()
        state = quality_state()
        wf, *_ = resume_gated(state, {}, fake, files={lambda s: s.quality_path(1, 1): "all good\n"})

        with self.assertRaisesRegex(OrchestratorError, "quality-1-1.md does not start with a GATE line; fix it"):
            wf.run()
        self.assertEqual(fake.requests, [])

    def test_resume_mid_baseline_continues_it(self):
        fake = FakeCI()
        n = fake.add_build(BASE_REF, "base", outcome())
        state = quality_state(quality_baseline=None,
                              ci={"ref": BASE_REF, "sha": "abc123", "queue_url": f"{JENKINS}/queue/item/1/",
                                  "build": n})
        wf, herdr, host, _ = resume_gated(state, {"review": [review_turn(1, APPROVE)]}, fake)

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(fake.triggered("base"), [fake.builds[n]])
        self.assertEqual(wf.state.quality_baseline, f"analysis-{n}")
        self.assertEqual(len(fake.triggered("change")), 1)

    def test_build_ends_alike_live_and_resumed(self):
        cases = [
            ("no report", outcome(report=False), "GATE: BUILD_FAILED"),
            ("report after FAILURE", outcome(result="FAILURE"), "GATE: OK"),
            ("aborted, no report", outcome(report=False, result="ABORTED"), "was aborted before"),
        ]
        for name, out, expected in cases:
            with self.subTest(name):
                fake = FakeCI()
                n = fake.add_build(REF_1_1, "change", out)
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

    def test_resumed_base_build_failed_fails_the_run(self):
        fake = FakeCI()
        n = fake.add_build(BASE_REF, "base", outcome(report=False))
        state = quality_state(quality_baseline=None,
                              ci={"ref": BASE_REF, "sha": "abc123", "queue_url": f"{JENKINS}/queue/item/1/",
                                  "build": n})
        wf, *_ = resume_gated(state, {}, fake)
        with self.assertRaisesRegex(OrchestratorError, "of the base commit abc123 ended FAILURE"):
            wf.run()

    def test_fresh_builder_answering_a_quality_round_is_briefed(self):
        fake = FakeCI()
        seen = []
        state = saved_run("build", 1, agents=("spec", "build", "review"), quality_job=JOB, quality_round=1,
                          quality_project=PROJECT, quality_baseline="analysis-base", prompted="build-1-q1.md")
        wf, herdr, host, _ = resume_gated(state, {"build": [recording(fix_turn(1, 1), seen)],
                                                  "review": [review_turn(1, APPROVE)]}, fake,
                                          alive=["review"], files={lambda s: s.quality_path(1, 1): "GATE: ERROR\n"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertIn("You are the Builder", seen[0])
        self.assertIn(f"you are a fresh session. An earlier Builder session did the earlier turns; its changes are "
                      f"already in the working tree, and its reports are {state.build_path(1)}.", seen[0])
        self.assertTrue(seen[0].endswith(orchestrator.QUALITY_FIX_PROMPT.format(
            quality_path=state.quality_path(1, 1), report_path=state.quality_build_path(1, 1))))
        self.assertEqual(wf.state.quality_round, 2)

    def test_fresh_builder_in_a_later_round_hears_of_every_earlier_report(self):
        fake = FakeCI()
        seen = []
        state = saved_run("build", 2, agents=("spec", "build", "review"), quality_job=JOB,
                          quality_project=PROJECT, quality_baseline="analysis-base")
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

    def test_build_config_edits(self):
        diff = ("diff --git a/Jenkinsfile b/Jenkinsfile\n--- a/Jenkinsfile\n+++ b/Jenkinsfile\n@@ -1 +1 @@\n-a\n+b\n"
                "diff --git a/sonar-project.properties b/sonar-project.properties\ndeleted file mode 100644\n"
                "--- a/sonar-project.properties\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        self.assertEqual(orchestrator.edited_config(diff, changed_lines(diff)),
                         ["Jenkinsfile", "sonar-project.properties"])
        self.assertEqual(orchestrator.edited_config("", {}), [])


class TestQualityReport(unittest.TestCase):
    def test_gate_line(self):
        self.assertEqual(parse_gate("\n**GATE: BUILD_FAILED**\n"), "BUILD_FAILED")
        self.assertEqual(parse_gate("GATE: OK"), "OK")
        self.assertIsNone(parse_gate("GATE: WARN"))
        self.assertIsNone(parse_gate("Findings\nGATE: OK"))
        self.assertIsNone(parse_gate(""))

    def issues(self, n):
        return [{"path": "f.py", "line": i, "severity": "HIGH", "rule": "r", "message": f"m{i}"} for i in range(1, n + 1)]

    def test_numbers_at_most_fifty_issues(self):
        text = quality_report("ERROR", "intro", {"f.py": set(range(1, 61))}, issues=self.issues(70), tests=[])
        self.assertTrue(text.startswith("GATE: ERROR\n\nintro\n"))
        self.assertIn("50. `f.py:50` HIGH r: m50\n", text)
        self.assertNotIn("51. ", text)
        self.assertIn("10 more issues on changed lines are not listed", text)
        self.assertIn("10 other open issues in the project", text)

    def test_tests_coverage_and_console(self):
        text = quality_report("BUILD_FAILED", "intro", {}, tests=None, console="last\nlines")
        self.assertIn("Jenkins has no test report for this build.", text)
        self.assertIn("````\nlast\nlines\n````", text)
        self.assertNotIn("Issues on changed lines", text)
        text = quality_report("OK", "intro", {}, tests=[("t.T.test_x", "AssertionError\ntraceback")],
                              coverage={"new_coverage": "62.5", "new_lines_to_cover": "40", "new_uncovered_lines": "15"})
        self.assertIn("- `t.T.test_x`: AssertionError\n", text)
        self.assertIn("62.5% of 40 new lines to cover; 15 are not covered.", text)
        self.assertIn("None.", text)
        self.assertIn("No new lines to cover.", quality_report("OK", "i", {}, tests=[], coverage={}))

    def test_failing_tests_beyond_the_limit_are_counted(self):
        tests = [(f"t.T.test_{k}", "") for k in range(orchestrator.QUALITY_ISSUE_LIMIT + 3)]
        text = quality_report("ERROR", "intro", {}, tests=tests)
        self.assertIn("- `t.T.test_0`: failed\n", text)
        self.assertIn("- and 3 more.\n", text)
        self.assertNotIn(f"test_{orchestrator.QUALITY_ISSUE_LIMIT}`", text)

    def test_build_config_edits_are_named(self):
        text = quality_report("OK", "i", {}, tests=[], edited=["Jenkinsfile", "sonar-project.properties"])
        self.assertIn("The change edits `Jenkinsfile`. The quality job reads it from `main`", text)
        self.assertIn("The change edits `sonar-project.properties`. The scanner read the edited file", text)
        for only, other in (("Jenkinsfile", "sonar-project.properties"), ("sonar-project.properties", "Jenkinsfile")):
            with self.subTest(only=only):
                text = quality_report("OK", "i", {}, tests=[], edited=[only])
                self.assertIn(f"The change edits `{only}`", text)
                self.assertNotIn(f"The change edits `{other}`", text)

    def test_tokens_are_masked(self):
        ci = CI(JOB, env=CREDENTIALS)
        self.assertEqual(ci.mask(f"a {JENKINS_TOKEN} b {SONAR_TOKEN}"), "a **** b ****")
        self.assertEqual(CI(JOB, env={}).mask("text"), "text")


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
        env = {"JENKINS_USER": "from-env", "SONAR_TOKEN": ""}
        self.assertEqual(orchestrator.ci_credentials(env, self.path),
                         {**{k: f"file-{k}" for k in orchestrator.CI_ENV}, "JENKINS_USER": "from-env"})

    def test_the_file_is_not_read_when_the_environment_has_everything(self):
        self.write("garbage", mode=0o644)
        self.assertEqual(orchestrator.ci_credentials(CREDENTIALS, self.path), CREDENTIALS)

    def test_a_missing_file_leaves_the_environment(self):
        self.assertEqual(orchestrator.ci_credentials({"JENKINS_URL": JENKINS}, self.path), {"JENKINS_URL": JENKINS})

    def test_a_file_others_may_read_is_refused(self):
        self.write(f"SONAR_TOKEN={SONAR_TOKEN}\n", mode=0o640)
        with self.assertRaisesRegex(OrchestratorError, f"others may read it; run: chmod 600 {self.path}"):
            orchestrator.ci_credentials({}, self.path)

    def test_shell_syntax(self):
        self.write("# Jenkins\n\nexport JENKINS_URL=https://jenkins.example/\n"
                   f"  JENKINS_TOKEN = '{JENKINS_TOKEN}'\nSONAR_TOKEN=old\nSONAR_TOKEN=\"{SONAR_TOKEN}\"\n")
        self.assertEqual(orchestrator.ci_credentials({}, self.path),
                         {"JENKINS_URL": "https://jenkins.example/", "JENKINS_TOKEN": JENKINS_TOKEN,
                          "SONAR_TOKEN": SONAR_TOKEN})

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
        ci, urlopen = self.ci(FakeResponse(json.dumps(params)), FakeResponse('{"valid": true}'))
        ci.preflight()

        import base64
        jenkins, sonar = [c.args[0] for c in urlopen.call_args_list]
        self.assertEqual(jenkins.get_header("Authorization"),
                         "Basic " + base64.b64encode(f"agents:{JENKINS_TOKEN}".encode()).decode())
        self.assertEqual(sonar.get_header("Authorization"),
                         "Basic " + base64.b64encode(f"{SONAR_TOKEN}:".encode()).decode())
        # Kept from a redirect to another server.
        self.assertNotIn("Authorization", jenkins.headers)
        for req in (jenkins, sonar):
            self.assertFalse(leaks(req.full_url))
        self.assertEqual(sonar.full_url, f"{SONAR}/api/authentication/validate")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], orchestrator.HTTP_TIMEOUT)

    def test_errors_name_method_url_and_status(self):
        ci, _ = self.ci(self.http_error(403, b'{"errors": [{"msg": "Insufficient privileges"}]}'))
        with self.assertRaises(CIError) as cm:
            ci.delete_project(PROJECT)
        self.assertEqual(str(cm.exception), f"POST {SONAR}/api/projects/delete: HTTP 403 (Insufficient privileges)")
        self.assertEqual(cm.exception.status, 403)
        self.assertFalse(cm.exception.transient)

    def test_network_errors_are_transient_and_masked(self):
        ci, _ = self.ci(urllib.error.URLError(f"refused {SONAR_TOKEN}"), TimeoutError("timed out"),
                        self.http_error(503))
        for _ in range(3):
            with self.assertRaises(CIError) as cm:
                ci.build_result(7)
            self.assertTrue(cm.exception.transient)
            self.assertFalse(leaks(str(cm.exception)))
            self.assertIn(f"GET {JENKINS}{JOB_PATH}7/api/json?tree=building%2Cresult", str(cm.exception))

    def test_trigger_returns_the_queue_item(self):
        ci, urlopen = self.ci(FakeResponse(headers={"Location": "http://internal:8080/queue/item/77/"}))
        self.assertEqual(ci.trigger(REF_1_1, PROJECT, "change"), f"{JENKINS}/queue/item/77/")
        req = urlopen.call_args.args[0]
        self.assertEqual((req.get_method(), req.full_url), ("POST", f"{JENKINS}{JOB_PATH}buildWithParameters"))
        self.assertEqual(dict(urllib.parse.parse_qsl(req.data.decode())),
                         {"GIT_REF": REF_1_1, "SONAR_PROJECT_KEY": PROJECT, "SONAR_PROJECT_VERSION": "change"})

    def test_trigger_on_a_deleted_job_is_not_retried(self):
        ci, _ = self.ci(self.http_error(404))
        with self.assertRaises(OrchestratorError) as cm:
            ci.trigger(REF_1_1, PROJECT, "change")
        self.assertNotIsInstance(cm.exception, CIError)
        self.assertRegex(str(cm.exception), f"Jenkins job {JOB} is gone: POST .*buildWithParameters: HTTP 404")

    def test_missing_credentials_are_named_before_any_request(self):
        ci, urlopen = self.ci(env={"JENKINS_URL": JENKINS})
        with self.assertRaisesRegex(OrchestratorError,
                                    "needs JENKINS_USER, JENKINS_TOKEN, SONAR_HOST_URL, SONAR_TOKEN in"):
            ci.build_result(1)
        urlopen.assert_not_called()

    def test_issues_page_by_page(self):
        page1 = {"paging": {"total": 3}, "issues": [
            {"component": f"{PROJECT}:a.py", "line": 3, "rule": "r1", "message": "m1", "severity": "MINOR",
             "impacts": [{"softwareQuality": "MAINTAINABILITY", "severity": "LOW"},
                         {"softwareQuality": "RELIABILITY", "severity": "HIGH"}]},
            {"component": PROJECT, "rule": "r2", "message": "m2", "severity": "MAJOR"}]}
        page2 = {"paging": {"total": 3}, "issues": [
            {"component": f"{PROJECT}:dir/b.py", "rule": "r3", "message": "m3", "severity": "CRITICAL", "impacts": []}]}
        ci, urlopen = self.ci(FakeResponse(json.dumps(page1)), FakeResponse(json.dumps(page2)))

        self.assertEqual(ci.issues(PROJECT), [
            {"path": "a.py", "line": 3, "severity": "HIGH", "rule": "r1", "message": "m1"},
            {"path": None, "line": None, "severity": "MAJOR", "rule": "r2", "message": "m2"},
            {"path": "dir/b.py", "line": None, "severity": "CRITICAL", "rule": "r3", "message": "m3"}])
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(urlopen.call_args.args[0].full_url).query))
        self.assertEqual(query, {"components": PROJECT, "resolved": "false", "ps": "500", "p": "2"})

    def test_coverage_in_either_shape(self):
        body = {"component": {"measures": [{"metric": "new_coverage", "period": {"value": "50.0"}},
                                           {"metric": "new_lines_to_cover", "value": "4"},
                                           {"metric": "new_uncovered_lines", "periods": [{"value": "2"}]}]}}
        ci, _ = self.ci(FakeResponse(json.dumps(body)))
        self.assertEqual(ci.coverage(PROJECT),
                         {"new_coverage": "50.0", "new_lines_to_cover": "4", "new_uncovered_lines": "2"})

    def test_missing_artifact_and_test_report(self):
        ci, _ = self.ci(self.http_error(404), self.http_error(404))
        self.assertIsNone(ci.report_task(3))
        self.assertIsNone(ci.failed_tests(3))

    def test_malformed_answers(self):
        ci, _ = self.ci(FakeResponse("<html>proxy error</html>"), FakeResponse("[1]"), FakeResponse(),
                        FakeResponse("projectKey=x\n"), FakeResponse('{"qualityGate": null}'))
        for call in (lambda: ci.build_result(1), lambda: ci.build_result(1)):
            with self.assertRaisesRegex(CIError, "the answer is not") as cm:
                call()
            self.assertTrue(cm.exception.transient)
        with self.assertRaisesRegex(OrchestratorError, "named no queue item"):
            ci.trigger(REF_1_1, PROJECT, "change")
        self.assertIsNone(ci.report_task(1))
        with self.assertRaisesRegex(OrchestratorError, "names no quality gate for py-ai-agents-orchestrator"):
            ci.create_project(PROJECT, "py-ai-agents-orchestrator")

    def test_create_project_refused_is_raised(self):
        ci, _ = self.ci(FakeResponse('{"qualityGate": {"name": "Sonar way"}}'), self.http_error(400),
                        self.http_error(404))
        with self.assertRaisesRegex(CIError, "projects/create: HTTP 400"):
            ci.create_project(PROJECT, "py-ai-agents-orchestrator")

    def test_create_project_adopts_one_already_there(self):
        ci, urlopen = self.ci(FakeResponse('{"qualityGate": {"name": "Sonar way"}}'), self.http_error(400),
                              FakeResponse('{"component": {}}'), FakeResponse(), FakeResponse())
        ci.create_project(PROJECT, "py-ai-agents-orchestrator")
        select = urlopen.call_args_list[3].args[0]
        self.assertEqual(dict(urllib.parse.parse_qsl(select.data.decode())),
                         {"gateName": "Sonar way", "projectKey": PROJECT})


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


class TestQualityCLI(unittest.TestCase):
    def refused(self, argv):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(argv)

    def test_flag_checks(self):
        self.refused(["run", "t", "--quality-gate", JOB, "--max-quality-rounds", "0"])
        self.refused(["run", "t", "--max-quality-rounds", "2"])
        self.refused(["run", "t", "--quality-gate", "/folder/"])
        self.refused(["resume", "a1b2c3", "--quality-gate", JOB])
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

    def test_state_saved_before_the_gate_has_none(self):
        saved = asdict(saved_run("review", 1))
        for key in ("quality_job", "max_quality_rounds", "quality_round", "quality_project", "quality_baseline", "ci"):
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
# Since then, only SPEC_PROMPT's "Separate Claude Code sessions" became "Separate agent sessions".
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
        f'As your last step, write a report to {D}/build-1.md in a single write; it hands the work to the Reviewer: the files you changed and why, how you verified the change (commands and a summary of their results), and any acceptance criterion you did not meet, with the reason.'),
    'quality_fix_1_1': (
        f'The SonarQube quality gate did not pass on your change; the findings are in {D}/quality-1-1.md. Fix each numbered issue, the failed conditions, the failing tests and a failed build, or explain in your report why one should stand. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-1-q1.md in a single write, in the same shape as before, answering each numbered issue by its number.'),
    'review_1_passed': (
        f'You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did not write this change. Judge it only against the spec in {D}/spec.md and the code itself.\n'
        '\n'
        'The change: `git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\n'
        f"The Builder's report is in {D}/build-1-q1.md. Treat its claims as unverified: check them, and run the verification yourself. Do not modify any file other than your review.\n"
        '\n'
        f'As your last step, write your review to {D}/review-1.md in a single write. Its first line must be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered findings, each with file:line, what is wrong, and which acceptance criterion it violates or what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. Style preferences are not defects.\n'
        '\n'
        f'The SonarQube quality gate passed on this change; its report is in {D}/quality-1-2.md.'),
    'fix_2': (
        f'The Reviewer requested changes; the findings are in {D}/review-1.md. Fix each finding, or explain in your report why it is wrong. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2.md in a single write, in the same shape as before, answering each finding by its number.'),
    'quality_fix_2_1': (
        f'The SonarQube quality gate did not pass on your change; the findings are in {D}/quality-2-1.md. Fix each numbered issue, the failed conditions, the failing tests and a failed build, or explain in your report why one should stand. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2-q1.md in a single write, in the same shape as before, answering each numbered issue by its number.'),
    'recheck_2_unresolved': (
        f"The Builder has answered your review; the new report is in {D}/build-2-q1.md. Review the change again (`git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists) against the spec in {D}/spec.md and your previous findings, checking the Builder's claims rather than trusting them. As your last step, write the review to {D}/review-2.md in a single write, with the same first-line verdict and numbered findings as before.\n"
        '\n'
        f"The SonarQube quality gate still did not pass after the Builder's last quality round; its unresolved findings are in {D}/quality-2-2.md. Weigh them as you would your own."),
    'review_1_unresolved': (
        f'You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). You did not write this change. Judge it only against the spec in {D}/spec.md and the code itself.\n'
        '\n'
        'The change: `git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists\n'
        f"The Builder's report is in {D}/build-1-q1.md. Treat its claims as unverified: check them, and run the verification yourself. Do not modify any file other than your review.\n"
        '\n'
        f'As your last step, write your review to {D}/review-1.md in a single write. Its first line must be exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list numbered findings, each with file:line, what is wrong, and which acceptance criterion it violates or what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. Style preferences are not defects.\n'
        '\n'
        f"The SonarQube quality gate still did not pass after the Builder's last quality round; its unresolved findings are in {D}/quality-1-2.md. Weigh them as you would your own."),
    'recheck_2_passed': (
        f"The Builder has answered your review; the new report is in {D}/build-2.md. Review the change again (`git diff abc123` in /proj, plus the untracked files `git status --porcelain` lists) against the spec in {D}/spec.md and your previous findings, checking the Builder's claims rather than trusting them. As your last step, write the review to {D}/review-2.md in a single write, with the same first-line verdict and numbered findings as before.\n"
        '\n'
        f'The SonarQube quality gate passed on this change; its report is in {D}/quality-2-1.md.'),
    'fresh_build_2': (
        f'You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The spec in {D}/spec.md was agreed with the human by a separate session; it is your contract.\n'
        '\n'
        "Implement it in /proj, following the conventions of the surrounding code. Verify the change the way the spec's Verification section says, and run the tests. Do not commit, push or switch branches; leave the changes in the working tree for the Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your report.\n"
        '\n'
        f'As your last step, write a report to {D}/build-2.md in a single write; it hands the work to the Reviewer: the files you changed and why, how you verified the change (commands and a summary of their results), and any acceptance criterion you did not meet, with the reason.\n'
        '\n'
        f'This is round 2, and you are a fresh session. An earlier Builder session did the earlier turns; its changes are already in the working tree, and its reports are {D}/build-1.md.\n'
        '\n'
        f'The Reviewer requested changes; the findings are in {D}/review-1.md. Fix each finding, or explain in your report why it is wrong. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2.md in a single write, in the same shape as before, answering each finding by its number.'),
    'fresh_build_2_after_quality_rounds': (
        f'You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The spec in {D}/spec.md was agreed with the human by a separate session; it is your contract.\n'
        '\n'
        "Implement it in /proj, following the conventions of the surrounding code. Verify the change the way the spec's Verification section says, and run the tests. Do not commit, push or switch branches; leave the changes in the working tree for the Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your report.\n"
        '\n'
        f'As your last step, write a report to {D}/build-2.md in a single write; it hands the work to the Reviewer: the files you changed and why, how you verified the change (commands and a summary of their results), and any acceptance criterion you did not meet, with the reason.\n'
        '\n'
        f'This is round 2, and you are a fresh session. An earlier Builder session did the earlier turns; its changes are already in the working tree, and its reports are {D}/build-1.md, {D}/build-1-q1.md, {D}/build-1-q2.md.\n'
        '\n'
        f'The Reviewer requested changes; the findings are in {D}/review-1.md. Fix each finding, or explain in your report why it is wrong. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2.md in a single write, in the same shape as before, answering each finding by its number.'),
    'fresh_quality_fix_2_1': (
        f'You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). The spec in {D}/spec.md was agreed with the human by a separate session; it is your contract.\n'
        '\n'
        "Implement it in /proj, following the conventions of the surrounding code. Verify the change the way the spec's Verification section says, and run the tests. Do not commit, push or switch branches; leave the changes in the working tree for the Reviewer. If the spec is wrong or cannot be met, do not deviate silently: say so in your report.\n"
        '\n'
        f'As your last step, write a report to {D}/build-2-q1.md in a single write; it hands the work to the Reviewer: the files you changed and why, how you verified the change (commands and a summary of their results), and any acceptance criterion you did not meet, with the reason.\n'
        '\n'
        f'This is round 2, and you are a fresh session. An earlier Builder session did the earlier turns; its changes are already in the working tree, and its reports are {D}/build-1.md, {D}/build-1-q1.md, {D}/build-2.md.\n'
        '\n'
        f'The SonarQube quality gate did not pass on your change; the findings are in {D}/quality-2-1.md. Fix each numbered issue, the failed conditions, the failing tests and a failed build, or explain in your report why one should stand. Re-run the verification. Do not commit, push or switch branches. As your last step, write a new report to {D}/build-2-q1.md in a single write, in the same shape as before, answering each numbered issue by its number.'),
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
        f'The SonarQube quality gate passed on this change; its report is in {D}/quality-2-1.md.\n'
        '\n'
        f'This is round 2, and you are a fresh session. The earlier reviews of this change are {D}/review-1.md; check that the Builder has answered each of their findings.'),
    'continue_build_2': (
        f'Your session was restarted in the middle of this turn. Continue where you left off; the turn still ends when you write {D}/build-2.md in a single write.'),
}
GOLDEN_PR_BODY = (
    'Opened by ai-agents-orchestrator run `20260929-120000-a1b2c3`. Reviewer verdict after 2 rounds: **APPROVE**.\n'
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
    'SonarQube analysed snapshot `snap4` (round 2, quality round 2 of 2) in Jenkins build #45, against the base `abc123`: quality gate ERROR.\n'
    '\n'
    '## Failed conditions\n'
    '\n'
    '- `new_coverage` is 50.0; the gate wants at least 80.\n'
    '\n'
    '## Issues on changed lines\n'
    '\n'
    'None.\n'
    '\n'
    '## Failing tests\n'
    '\n'
    'None.\n'
    '\n'
    '## Coverage on new code\n'
    '\n'
    '87.5% of 8 new lines to cover; 1 are not covered.\n'
    '\n'
    '</details>\n'
    '\n'
    '<details>\n'
    '<summary>Review (round 2)</summary>\n'
    '\n'
    '**VERDICT: APPROVE**\n'
    '1. finding\n'
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
            [red, outcome(), red, red], [build_turn(1), fix_turn(1, 1), build_turn(2), fix_turn(2, 1)],
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
                                   "review-2.md"])
        self.assertEqual([c for c in herdr.calls if c[0] in ("rename", "split")], [
            ("rename", "w1:p1", "Spec Collector"), ("split", "w1:p1", "right"), ("rename", "w1:p2", "Builder"),
            ("split", "w1:p2", "down"), ("rename", "w1:p3", "Reviewer")])
        self.assertEqual((host.prs[0]["title"], host.prs[0]["body"]), ("Add a token-bucket rate limiter", GOLDEN_PR_BODY))

    def test_gate_runs_out_then_passes(self):
        red = outcome(**RED)
        seen, *_ = self.two_gated_rounds(
            [red, red, outcome()], [build_turn(1), fix_turn(1, 1), build_turn(2)],
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
        state = saved_run("build", 2, agents=("spec", "build", "review"), quality_job=JOB,
                          quality_project=PROJECT, quality_baseline="analysis-base")
        wf, *_ = resume_gated(state, {"build": [recording(build_turn(2), seen)], "review": [review_turn(2, APPROVE)]},
                              FakeCI(), alive=["review"], files={lambda s: s.quality_build_path(1, 1): "r",
                                                                 lambda s: s.quality_build_path(1, 2): "r"})
        wf.run()
        self.assertEqual(seen, [GOLDEN["fresh_build_2_after_quality_rounds"]])

    def test_fresh_builder_answering_a_quality_round_in_round_two(self):
        seen = []
        state = saved_run("build", 2, agents=("spec", "build", "review"), quality_job=JOB, quality_round=1,
                          quality_project=PROJECT, quality_baseline="analysis-base", prompted="build-2-q1.md")
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
        state = saved_run("review", 2, agents=("spec", "build", "review"), quality_job=JOB, quality_round=1,
                          quality_project=PROJECT, quality_baseline="analysis-base")
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
        self.assertEqual(seen, [f"Build add a rate limiter in /proj; report to {D}/build-1.md.",
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
        self.assertEqual(seen, [f"Build {D}/spec.md against {D}/tests.md; report to {D}/build-1.md.",
                                f"Fix {D}/review-1.md; report to {D}/build-2.md."])
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
            f"Implement add a rate limiter; report to {D}/impl-1.md.",
            orchestrator.QUALITY_FIX_PROMPT.format(quality_path=f"{D}/quality-1-1.md", report_path=f"{D}/impl-1-q1.md")])
        self.assertEqual(reviews, [f"Review {CHANGE} with {D}/impl-1-q1.md; write {D}/review-1.md.\n\n" +
                                   orchestrator.QUALITY_PASSED_NOTE.format(quality_path=f"{D}/quality-1-2.md")])
        written = [p.rsplit("/", 1)[-1] for p in host.writes if p.startswith(D) and not p.endswith("state.json")]
        self.assertEqual(written, ["impl-1.md", "quality-1-1.md", "impl-1-q1.md", "quality-1-2.md", "review-1.md"])
        self.assertTrue(host.files[f"{D}/quality-1-1.md"].startswith("GATE: ERROR\n"))
        self.assertTrue(host.files[f"{D}/quality-1-2.md"].startswith("GATE: OK\n"))
        self.assertEqual([b["ref"] for b in fake.triggered("change")], [REF_1_1, "orchestrator-ci/a1b2c3-1-q2"])
        body = host.prs[0]["body"]
        self.assertIn("<summary>Builder report (round 1)</summary>\n\nimpl 1 q1", body)
        self.assertIn("<summary>Quality gate (round 1)</summary>\n\nGATE: OK", body)
        self.assertEqual(host.prs[0]["title"], "add a rate limiter")

    def test_fresh_gated_step_answering_a_quality_round(self):
        seen = []
        state = saved_run("impl", 1, agents=(), quality_job=JOB, quality_round=1, quality_project=PROJECT,
                          quality_baseline="analysis-base", prompted="impl-1-q1.md", workflow="impl")
        state.agents = {"impl": {"name": "impl-a1b2c3", "pane": "w1:p1"}}
        wf, herdr, host, _ = resume_gated(state, {"impl": [recording(impl_turn(1, 1), seen)],
                                                  "review": [review_turn(1, APPROVE)]}, FakeCI(), pipeline=IMPL,
                                          files={lambda s: f"{s.dir}/impl-1.md": "impl 1",
                                                 lambda s: s.quality_path(1, 1): "GATE: ERROR\n"})

        self.assertEqual(wf.run(), APPROVE)
        self.assertEqual(seen, [f"Implement add a rate limiter; report to {D}/impl-1-q1.md.\n\n"
                                f"Earlier reports: {D}/impl-1.md.\n\n" +
                                orchestrator.QUALITY_FIX_PROMPT.format(quality_path=f"{D}/quality-1-1.md",
                                                                       report_path=f"{D}/impl-1-q1.md")])
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
        quick = orchestrator.load_workflow_file(f"{EXAMPLES}/quick.toml")
        self.assertEqual(orchestrator.workflow_shape(quick), "build -> review (back to build)")
        tdd = orchestrator.load_workflow_file(f"{EXAMPLES}/tdd.toml")
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
        self.assertEqual((tests.role, tests.file, tests.edits), ("tests", "tests.md", True))

    def test_default_round_trips_through_its_file(self):
        default = orchestrator.DEFAULT_WORKFLOW
        text = orchestrator.workflow_toml(default)
        self.assertEqual(orchestrator.parse_workflow("default", tomllib.loads(text)), default)

    def test_any_text_round_trips(self):
        odd = ["ends in a quote '", "has ''' inside\nand a newline", "tab\there", "del\x7f", "ünïcode 🦀\n",
               "\nstarts with a newline", 'back\\slash "quoted"\n']
        p = Pipeline("odd", {"b": "Bob's \"pane\""}, tuple(
            Step(f"s{i}", "b", f"s{i}.md", text + " {path}") for i, text in enumerate(odd)),
            models={"b": "m"}, description="with ''' and '")
        self.assertEqual(orchestrator.parse_workflow("odd", tomllib.loads(orchestrator.workflow_toml(p))), p)

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
        """))
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
        """))
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
        for name in ("state.json", "quality-{n}-1.md", "../b-{n}.md", "/tmp/b-{n}.md", ".."):
            cases.append((f"step build: handoff file {name} must be a plain file name, and not state.json or "
                          f"quality-*, which the run writes itself", "[[steps]]\n" + step.replace("b-{n}.md", name)))
        for message, text in cases:
            with self.subTest(message=message):
                path = self.file("bad", text)
                with self.assertRaises(OrchestratorError) as e:
                    orchestrator.load_workflow_file(path)
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
            orchestrator.load_workflow_file(path)

    def test_a_name_that_cannot_be_a_workflows(self):
        path = self.file("my flow", f"[[steps]]\nuse = \"spec\"\n")
        with self.assertRaisesRegex(OrchestratorError, "a workflow's name, its file's, may use only letters"):
            orchestrator.load_workflow_file(path)

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
            orchestrator.load_workflow_file("/nope/x.toml")

    def test_files_are_offered_by_name(self):
        with open(f"{EXAMPLES}/tdd.toml") as f:
            self.file("tdd", f.read())
        self.file("broken", "nonsense\n")
        with open(os.path.join(self.workflows, "notes.txt"), "w") as f:
            f.write("not a workflow")
        self.assertEqual(orchestrator.workflow_names(), ["broken", "default", "tdd"])
        self.assertEqual(orchestrator.named_workflow("tdd").name, "tdd")
        with self.assertRaisesRegex(OrchestratorError, "unknown workflow nope; there are broken, default, tdd"):
            orchestrator.named_workflow("nope")

    def test_a_run_keeps_its_workflow_when_the_file_is_gone(self):
        with open(f"{EXAMPLES}/quick.toml") as f:
            quick = orchestrator.load_workflow_file(self.file("quick", f.read()))

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
        self.assertEqual(kw["pipeline"], orchestrator.load_workflow_file(f"{EXAMPLES}/tdd.toml"))
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
        tdd = orchestrator.named_workflow("tdd")
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
                self.assertEqual(orchestrator.parse_workflow("tdd", tomllib.loads(out.getvalue())),
                                 orchestrator.named_workflow("tdd"))


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
        self.assertTrue(pr["body"].startswith(
            "Opened by ai-agents-orchestrator run `20260929-120000-a1b2c3`. **FINISHED**: its workflow has no review "
            "step, so no agent reviewed this change.\n\n<details open>\n<summary>Spec</summary>"))
        self.assertIn("<summary>Builder report (round 1)</summary>\n\nsolo report", pr["body"])
        self.assertNotIn("<summary>Review", pr["body"])
        commit = next(c for c in host.git_calls if c[0] == "commit")
        self.assertEqual(commit[-1], "Orchestrator run 20260929-120000-a1b2c3: FINISHED, unreviewed.")
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
        self.assertTrue(pr["body"].startswith(
            "Opened by ai-agents-orchestrator run `20260929-120000-a1b2c3`. **QUALITY_GATE_FAILED**: its workflow has "
            "no review step, and the SonarQube quality gate still did not pass after the last quality round, so this "
            "is a draft.\n\n<details open>"))
        self.assertNotIn("Reviewer", pr["body"])
        self.assertIn("<summary>Quality gate (round 1)</summary>\n\nGATE: ERROR", pr["body"])
        commit = next(c for c in host.git_calls if c[0] == "commit")
        self.assertEqual(commit[-1], "Orchestrator run 20260929-120000-a1b2c3: QUALITY_GATE_FAILED, unreviewed.")
        self.assertIn("Run finished: QUALITY_GATE_FAILED", notes)

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


class TestWorkflowFileAgents(unittest.TestCase):
    def test_parsed_and_round_tripped(self):
        p = orchestrator.parse_workflow("mine", tomllib.loads(
            'roles.review = { agent = "codex", model = "o3" }\nroles.build = { label = "Implementer", agent = "pi" }\n'
            '[[steps]]\nuse = "spec"\n[[steps]]\nuse = "build"\n[[steps]]\nuse = "review"\n'))
        self.assertEqual(p.agents, {"review": "codex", "build": "pi"})
        definition = orchestrator.workflow_definition(p)
        self.assertEqual(definition["roles"], {"spec": {"label": "Spec Collector"},
                                               "build": {"label": "Implementer", "agent": "pi"},
                                               "review": {"label": "Reviewer", "model": "o3", "agent": "codex"}})
        text = orchestrator.workflow_toml(p)
        self.assertIn('[roles.review]\nlabel = "Reviewer"\nmodel = "o3"\nagent = "codex"\n', text)
        self.assertEqual(orchestrator.parse_workflow("mine", tomllib.loads(text)), p)
        self.assertEqual(orchestrator.parse_workflow("mine", definition), p)

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
        self.assertEqual(orchestrator.run_pipeline("coded", wf.state.workflow_definition), p)

    def test_invalid_agents_name_the_role(self):
        steps = '[[steps]]\nuse = "spec"\n[[steps]]\nuse = "build"\n[[steps]]\nuse = "review"\n'
        self.assertEqual(orchestrator.parse_workflow("mine", tomllib.loads(steps)).steps,
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
                orchestrator.parse_workflow("mine", data)
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
        return [p for p in self.files if p.startswith(f"{run_dir}/")]


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
            self.assertEqual(host.run_files(run_dir), [f"{run_dir}/{n}" for n in ("c.md", "a.md", "b.md", "d.md")])

    def test_missing_directory(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Host().run_files(f"{d}/nosuch"), [])

    def test_one_call_over_ssh(self):
        run = MagicMock(return_value=completed("5 /r/b.md\0" "5 /r/a b.md\0" "3 /r/c.md\0"))
        self.assertEqual(Host("remote-host", run=run).run_files("/r"), ["/r/c.md", "/r/a b.md", "/r/b.md"])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:4], ["ssh", "-o", "BatchMode=yes", "remote-host"])
        self.assertIn("stat -c %Y", run.call_args.args[0][4])
        self.assertIn("stat -f %m", run.call_args.args[0][4])

    def test_failure_raises(self):
        host = Host(run=MagicMock(return_value=completed(stderr="Permission denied", returncode=1)))
        with self.assertRaisesRegex(OrchestratorError, "Permission denied"):
            host.run_files("/r")


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
        self.assertEqual(seen, [f"Build {D}/plan.md; report to {D}/build-1.md."])
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
        example = orchestrator.load_workflow_file(f"{EXAMPLES}/spec-build-review.toml")
        self.assertEqual(orchestrator.workflow_definition(example),
                         orchestrator.workflow_definition(orchestrator.DEFAULT_WORKFLOW))


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


NEW_RUN = "20261009-120000-c0ffee"


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


if __name__ == "__main__":
    unittest.main()
