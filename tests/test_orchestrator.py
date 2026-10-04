import io
import json
import os
import socket
import subprocess
import unittest
import urllib.error
import urllib.parse
from dataclasses import asdict
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

    def snapshot(self, cwd, parent, message):
        self.snapshots.append((parent, message))
        return f"snap{len(self.snapshots)}"

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

    def test_build_config_edits_are_named(self):
        text = quality_report("OK", "i", {}, tests=[], edited=["Jenkinsfile", "sonar-project.properties"])
        self.assertIn("The change edits `Jenkinsfile`. The quality job reads it from `main`", text)
        self.assertIn("The change edits `sonar-project.properties`. The scanner read the edited file", text)

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
            with self.assertRaisesRegex(OrchestratorError, f"environment or in {self.path}$"):
                CI(JOB).check_credentials()


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

        self.assertEqual([self.git("rev-parse", "HEAD"), self.git("ls-files", "--stage"),
                          self.git("status", "--porcelain")], before)
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
        with self.assertRaisesRegex(OrchestratorError, "has no quality gate"):
            self.resumable(["--max-quality-rounds", "2"], asdict(saved_run("build", 1)))

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


if __name__ == "__main__":
    unittest.main()
