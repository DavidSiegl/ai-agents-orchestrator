import json
import os
import socket
import subprocess
import unittest
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import orchestrator
from orchestrator import (
    APPROVE, CHANGES_REQUESTED, HEARTBEAT_SECONDS, STALE_SECONDS, Herdr, HerdrError, Host,
    OrchestratorError, RunState, RunTakenOver, Workflow, branch_name, main, parse_args, parse_verdict,
    pr_body, spec_title,
)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def result(obj):
    return completed(json.dumps({"id": "cli:x", "result": obj}))


def no_processes(argv, **kw):
    raise AssertionError(f"FakeHost ran a real process: {argv}")


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

    def read(self, path):
        return self.files.get(path)

    def write(self, path, text):
        self.writes.append(path)
        self.files[path] = text
        if not path.startswith("/proj/.orchestrator/"):
            self.changed.add(path)

    def git_head(self, cwd):
        return self.head

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
        return completed()

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
        self.lost_sessions = set()  # sessions `claude --resume` cannot find, so the agent exits at once
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

    def start_agent(self, name, pane, agent_args):
        self.calls.append(("start", name, pane, tuple(agent_args)))
        args = list(agent_args)
        session = args[args.index("--resume") + 1] if "--resume" in args else f"session-{len(self.calls)}"
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


def make_workflow(script, host=None, max_rounds=3, state=None, **kw):
    """A workflow on fakes; pass `state` to resume a saved run instead of starting a new one."""
    host = host or FakeHost()
    state = state or RunState("20260929-120000-a1b2c3", "add a rate limiter", "/proj", None)
    herdr = FakeHerdr(host, state, script)
    notes = []
    clock = FakeClock()
    wf = Workflow(herdr, host, state, notify=lambda t, b: notes.append(t),
                  max_rounds=max_rounds, sleep=clock.sleep, clock=clock, wallclock=clock, **kw)
    return wf, herdr, host, notes


spec_turn = writes(lambda s: s.spec_path, "# Add a token-bucket rate limiter\n\n## Goal\nlimit requests")


def build_turn(n):
    def turn(prompt, state, host):
        host.write("/proj/limiter.py", f"version {n}")
        return writes(lambda s: s.build_path(n), f"report {n}")(prompt, state, host)
    return turn


def review_turn(n, verdict):
    return writes(lambda s: s.review_path(n), f"**VERDICT: {verdict}**\n1. finding")


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

    def test_review_without_verdict_aborts(self):
        wf, *_ = make_workflow({
            "spec": [spec_turn],
            "build": [build_turn(1)],
            "review": [writes(lambda s: s.review_path(1), "looks fine")],
        })

        with self.assertRaisesRegex(OrchestratorError, "does not start with a VERDICT line"):
            wf.run()

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
        }, agent_args=["--permission-mode", "auto"])
        wf.run()
        args = {c[3] for c in herdr.calls if c[0] == "start"}
        self.assertEqual(args, {("--permission-mode", "auto")})

    def start_args(self, **kw):
        wf, herdr, *_ = make_workflow({
            "spec": [spec_turn], "build": [build_turn(1)], "review": [review_turn(1, APPROVE)],
        }, **kw)
        wf.run()
        return {c[1].split("-")[0]: c[3] for c in herdr.calls if c[0] == "start"}

    def test_no_models_adds_no_model_arg(self):
        self.assertEqual(self.start_args(), {"spec": (), "build": (), "review": ()})

    def test_each_role_gets_its_own_model(self):
        args = self.start_args(agent_args=["--permission-mode", "auto"],
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

        with self.assertRaises(HerdrError) as cm:
            Herdr(run=run).call("agent", "prompt", "x", "y")
        self.assertEqual(cm.exception.code, "agent_blocked")

    def test_plain_text_error(self):
        run = MagicMock(return_value=completed(stderr="unknown flag", returncode=2))
        with self.assertRaisesRegex(HerdrError, "exit_2: unknown flag"):
            Herdr(run=run).call("agent", "bogus")

    def test_status_is_none_after_exit(self):
        err = json.dumps({"error": {"code": "agent_not_found", "message": "gone"}})
        run = MagicMock(return_value=completed(stderr=err, returncode=1))
        self.assertIsNone(Herdr(run=run).status("spec-x"))

    def test_start_agent_passes_claude_args_after_separator(self):
        run = MagicMock(return_value=result({}))
        Herdr(run=run).start_agent("build-x", "w1:p2", ["--permission-mode", "auto"])
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--kind") + 1], "claude")
        self.assertEqual(argv[-3:], ["--", "--permission-mode", "auto"])

    def test_start_agent_blocked_at_startup(self):
        err = json.dumps({"error": {"code": "agent_not_ready", "message": "blocked during startup"}})
        run = MagicMock(return_value=completed(stderr=err, returncode=1))
        self.assertFalse(Herdr(run=run).start_agent("spec-x", "w1:p1", []))

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
        with self.assertRaisesRegex(OrchestratorError, "no saved herdr machine named nope"):
            Herdr("nope", run=run).ssh_target()

    def test_missing_binary(self):
        run = MagicMock(side_effect=FileNotFoundError())
        with self.assertRaisesRegex(OrchestratorError, "not on PATH"):
            Herdr(run=run).call("agent", "list")


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
        with self.assertRaisesRegex(OrchestratorError, "Permission denied"):
            Host(run=run).read("/x/spec.md")

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
        with self.assertRaisesRegex(OrchestratorError, "corrupt run state"):
            Host(run=run).run_states("/x")

    def test_real_filesystem_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            host = Host()
            path = f"{d}/nested/file.md"
            self.assertIsNone(host.read(path))
            host.write(path, "hello")
            self.assertEqual(host.read(path), "hello")
            self.assertEqual(host.resolve_dir(d), host.check(["sh", "-c", "cd -- \"$1\" && pwd", "_", d]).strip())


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
    def test_permission_mode_reaches_claude(self, _resolve, _run, init):
        with patch("builtins.print"):
            self.assertEqual(main(["run", "task", "--permission-mode", "auto"]), 0)
        self.assertEqual(init.call_args.kwargs["agent_args"], ["--permission-mode", "auto"])

    @patch.object(Workflow, "__init__", return_value=None)
    @patch.object(Workflow, "run", return_value=APPROVE)
    @patch.object(Host, "resolve_dir", return_value="/proj")
    @patch.dict("os.environ", {"HERDR_ENV": "1"})
    def test_models_reach_the_workflow(self, _resolve, _run, init):
        with patch("builtins.print"):
            self.assertEqual(main(["run", "task", "--permission-mode", "auto",
                                   "--model", "sonnet", "--review-model", "opus"]), 0)
        self.assertEqual(init.call_args.kwargs["models"],
                         {"spec": "sonnet", "build": "sonnet", "review": "opus"})
        self.assertEqual(init.call_args.kwargs["agent_args"], ["--permission-mode", "auto"])

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
        state = saved_run("review", 1, agents=("spec", "build", "review"), prompted="review-1.md",
                          error="review-1.md does not start with a VERDICT line")
        wf, herdr, host, _ = resume(state, {"review": [review_turn(1, APPROVE)]},
                                    alive=["review"], files={lambda s: s.review_path(1): "looks fine"})

        with self.assertRaisesRegex(OrchestratorError, "review-1.md does not start with a VERDICT line; fix it"):
            wf.run()
        saved = json.loads(host.files[f"{state.dir}/state.json"])
        self.assertIsNone(saved["prompted"])
        self.assertNotIn(("prompt", "review-a1b2c3"), herdr.calls)

        del host.files[state.review_path(1)]
        wf2, herdr2, *_ = resume(RunState.from_dict(saved), {"review": [review_turn(1, APPROVE)]},
                                 alive=["review"], host=host)
        self.assertEqual(wf2.run(), APPROVE)
        self.assertEqual(herdr2.calls.count(("prompt", "review-a1b2c3")), 1)
        self.assertIsNone(wf2.state.error)

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
        self.assertEqual(host.git_calls[:3], [("rev-parse", "-q", "--verify", "MERGE_HEAD"), ("merge", "--abort"),
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
                "max_rounds": 3, "turn_timeout": 600, "agent_args": ["--permission-mode", "auto"],
                "models": {"build": "sonnet"}, **kw}

    def resumable(self, argv, saved, age=10**4, alive=True):
        args = parse_args(["resume", *argv])
        return orchestrator.resumable_state([(age, saved)], args, "/proj", "here", lambda pid: alive)

    def test_saved_settings_are_kept(self):
        state = self.resumable(["a1b2c3"], self.saved())
        self.assertEqual((state.max_rounds, state.turn_timeout, state.agent_args, state.models),
                         (3, 600, ["--permission-mode", "auto"], {"build": "sonnet"}))

    def test_flags_override_saved_settings(self):
        state = self.resumable(["a1b2c3", "--max-rounds", "5", "--timeout", "60",
                                "--permission-mode", "acceptEdits", "--review-model", "opus"], self.saved())
        self.assertEqual((state.max_rounds, state.turn_timeout, state.agent_args, state.models),
                         (5, 60, ["--permission-mode", "acceptEdits"], {"build": "sonnet", "review": "opus"}))

    def test_max_rounds_below_the_saved_round(self):
        with self.assertRaisesRegex(OrchestratorError, "already in round 2"):
            self.resumable(["a1b2c3", "--max-rounds", "1"], self.saved())

    def test_live_run_needs_force(self):
        saved = self.saved(owner=ME)
        with self.assertRaisesRegex(OrchestratorError, "looks alive .*pass --force"):
            self.resumable(["a1b2c3"], saved, age=30)
        self.assertEqual(self.resumable(["a1b2c3", "--force"], saved, age=30).run_id, saved["run_id"])

    def test_machine_of_the_resume_is_saved(self):
        state = self.resumable(["a1b2c3", "--machine", "remote", "--cwd", "~/p"], self.saved(machine=None))
        self.assertEqual(state.machine, "remote")


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
        with self.assertRaises(HerdrError):
            Herdr(run=self.not_found("server_unavailable")).pane_exists("wA:p1")


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


if __name__ == "__main__":
    unittest.main()
