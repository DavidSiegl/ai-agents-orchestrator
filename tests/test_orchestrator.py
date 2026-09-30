import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import orchestrator
from orchestrator import (
    APPROVE, CHANGES_REQUESTED, Herdr, HerdrError, Host, OrchestratorError,
    RunState, Workflow, branch_name, main, parse_args, parse_verdict, pr_body, spec_title,
)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def result(obj):
    return completed(json.dumps({"id": "cli:x", "result": obj}))


class FakeHost:
    """An in-memory filesystem and git checkout standing in for the machine the agents run on.

    Files written outside .orchestrator/ count as uncommitted changes until a commit.
    """

    def __init__(self, head="abc123", branch="main"):
        self.files = {}
        self.head = head
        self.branch = branch
        self.changed = set()
        self.git_calls = []
        self.prs = []

    def read(self, path):
        return self.files.get(path)

    def write(self, path, text):
        self.files[path] = text
        if not path.startswith("/proj/.orchestrator/"):
            self.changed.add(path)

    def git_head(self, cwd):
        return self.head

    def git(self, cwd, *args, timeout=60):
        self.git_calls.append(args)
        match args:
            case ("rev-parse", "--abbrev-ref", "HEAD"):
                return self.branch + "\n"
            case ("status", "--porcelain"):
                return "".join(f"?? {p}\n" for p in sorted(self.changed))
            case ("switch", "-c", name) | ("switch", "--quiet", name):
                self.branch = name
            case ("commit", *_):
                self.changed.clear()
        return ""

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
        self.statuses = {}
        self.waits = []
        self.blocked_at_start = set()

    def _role(self, name):
        return name.split("-", 1)[0]

    def create_workspace(self, cwd, label):
        self.calls.append(("workspace", label))
        return "w1", self._pane()

    def _pane(self):
        self.panes += 1
        return f"w1:p{self.panes}"

    def split(self, pane, direction, cwd):
        self.calls.append(("split", pane, direction))
        return self._pane()

    def rename_pane(self, pane, label):
        self.calls.append(("rename", pane, label))

    def start_agent(self, name, pane, agent_args):
        self.calls.append(("start", name, pane, tuple(agent_args)))
        self.statuses[name] = "idle"
        return self._role(name) not in self.blocked_at_start

    def prompt(self, name, text):
        self.calls.append(("prompt", name))
        self.statuses[name] = self.script[self._role(name)].pop(0)(text, self.state, self.host)

    def wait(self, name, timeout_ms, until=()):
        self.waits.append(until)
        return "done"

    def status(self, name):
        return self.statuses.get(name)

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


def make_workflow(script, host=None, max_rounds=3, **kw):
    host = host or FakeHost()
    state = RunState("20260929-120000-a1b2c3", "add a rate limiter", "/proj", None)
    herdr = FakeHerdr(host, state, script)
    notes = []
    clock = FakeClock()
    wf = Workflow(herdr, host, state, notify=lambda t, b: notes.append(t),
                  max_rounds=max_rounds, sleep=clock.sleep, clock=clock, **kw)
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

        with self.assertRaisesRegex(OrchestratorError, "Builder wrote an empty"):
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

    def test_long_sections_are_truncated(self):
        state = RunState("r", "t", "/proj", verdict=APPROVE, round=1)
        body = pr_body(state, "x" * (orchestrator.PR_SECTION_LIMIT * 2), "r", "VERDICT: APPROVE")
        self.assertLess(len(body), 65536)
        self.assertIn("truncated", body)


class TestHerdr(unittest.TestCase):
    def test_call_returns_result_and_forwards_machine(self):
        run = MagicMock(return_value=result({"agent": {"agent_status": "idle"}}))

        self.assertEqual(Herdr("slave0", run=run).status("build-x"), "idle")
        self.assertEqual(run.call_args.args[0],
                         ["herdr", "--machine", "slave0", "agent", "get", "build-x"])

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
        profiles = [{"id": "5d45", "label": "slave0", "target": "ai-agents", "enabled": True}]
        run = MagicMock(return_value=completed(json.dumps(profiles)))

        self.assertEqual(Herdr("slave0", run=run).ssh_target(), "ai-agents")
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
        run = MagicMock(return_value=completed("/home/agent/proj\n"))
        cwd = Host("ai-agents", run=run).resolve_dir("~/proj")

        self.assertEqual(cwd, "/home/agent/proj")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["ssh", "-o", "BatchMode=yes", "ai-agents"])
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
        url = Host("ai-agents", run=run).create_pr("/proj", "main", "orchestrator/x", "Title", "body", draft=True)

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
        out = '{"run_id": "a"}\n\n{"run_id": "b"}\n'
        run = MagicMock(return_value=completed(out))
        self.assertEqual([s["run_id"] for s in Host(run=run).run_states("/x")], ["a", "b"])

    def test_real_filesystem_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            host = Host()
            path = f"{d}/nested/file.md"
            self.assertIsNone(host.read(path))
            host.write(path, "hello")
            self.assertEqual(host.read(path), "hello")
            self.assertEqual(host.resolve_dir(d), host.check(["sh", "-c", "cd -- \"$1\" && pwd", "_", d]).strip())


class TestCLI(unittest.TestCase):
    def test_machine_requires_cwd(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_args(["run", "task", "--machine", "slave0"])

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
    @patch.object(Host, "resolve_dir", return_value="/home/agent/proj")
    @patch.object(Herdr, "ssh_target", return_value="ai-agents")
    def test_list_on_machine_uses_ssh_host(self, _target, _resolve, _states):
        with patch("builtins.print") as out:
            self.assertEqual(main(["list", "--machine", "slave0", "--cwd", "~/proj"]), 0)
        out.assert_called_with("No runs.")


if __name__ == "__main__":
    unittest.main()
