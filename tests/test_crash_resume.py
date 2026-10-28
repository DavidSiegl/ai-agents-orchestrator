"""Crash the orchestrator right after a write of state.json, resume the run as `resume --force` does, and check that it
ends as an uninterrupted run does: the same files, the same state.json less its heartbeat and owner, and each side
effect on the world once.

A crash is `Killed`, raised by the world's host right after it wrote state.json, whether `Workflow._save` or
`Workflow._claim` wrote it. It derives from BaseException, so no `except` clause in `Workflow.run` sees it and nothing
is released or saved: as with SIGKILL, the run is left as that write left it. The resume is `resumed_state`, which main
calls for `resume --force`, and a new `Workflow` on the same fakes, which play the world that outlives the orchestrator:
the project (FakeHost), herdr and its agents (FakeHerdr), Jenkins (FakeCI) and time (FakeClock).

`TestCrashAfterEveryWrite` crashes once, after write k for every k of each scenario. `CrashesAndResumes` crashes at
points Hypothesis draws, as often as it draws them, a resume included.

What the parity harness (tests/parity) covers instead: it kills the engine as a program, with SIGKILL at any moment, so
also between two writes, on real git; but only at the points where a scenario holds it, one per phase. This harness
crashes after every write of state.json but only there, and only on the fakes, so a bug in how Host drives git, gh or
herdr is not its to find (docs/architecture.md, Tests).
"""

import json
import unittest
from dataclasses import dataclass, field

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
from test_orchestrator import (
    EXAMPLES,
    JOB,
    RED,
    FakeCI,
    FakeClock,
    FakeHerdr,
    FakeHost,
    Killed,
    ci_for,
    outcome,
)

import orchestrator
from orchestrator import (
    APPROVE,
    BUILT_IN_PROMPTS,
    CHANGES_REQUESTED,
    CI_REF_PREFIX,
    DONE,
    HEARTBEAT_SECONDS,
    METRICS_FILE,
    SUMMARY_FILE,
    AgentSettings,
    Clocks,
    RunState,
    Workflow,
)

RUN_ID = "20260929-120000-a1b2c3"
RUN_KEY = RUN_ID.rsplit("-", 1)[1]
RUN_DIR = RunState(RUN_ID, "", "/proj", None).dir
STATE_PATH = f"{RUN_DIR}/state.json"
# How long each agent works on a prompt before its handoff file appears: past HEARTBEAT_SECONDS, so every turn has a
# heartbeat save to crash after while the agent works.
TURN_SECONDS = 90


@dataclass(frozen=True)
class Scenario:
    """A run to crash: what its Reviewer answers each round, and the flags it starts with.

    verdicts: the verdict step's answer in round n is verdicts[n - 1]. gate: the outcome() arguments of each
    quality build in turn, None for a run without the quality gate. workflow_file: an example workflow's path, None
    for the default workflow.
    """
    name: str
    verdicts: tuple[str, ...] = (APPROVE,)
    pull_request: bool = True
    gate: tuple[dict, ...] | None = None
    workflow_file: str | None = None


SCENARIOS = {s.name: s for s in (
    Scenario("approved"),
    Scenario("changes_then_approved", verdicts=(CHANGES_REQUESTED, APPROVE)),
    Scenario("gate_fails_then_passes", gate=(RED, {})),
    # The ticket's `tdd`, dropped since (9838e3f): product-team's QA Engineer writes the tests first, and they are
    # locked, as tdd's were.
    Scenario("product_team", workflow_file=f"{EXAMPLES}/product-team.toml"),
    Scenario("no_pr", pull_request=False),
)}


class CrashingHost(FakeHost):
    """FakeHost on which the orchestrator dies (Killed) right after the write of state.json numbered crash_at, counting
    from the first; None never. state_writes counts the writes of state.json so far."""

    def __init__(self):
        super().__init__()
        self.crash_at: int | None = None
        self.state_writes = 0

    def write(self, path, text, keep_mtime=False):
        super().write(path, text, keep_mtime)
        if path == STATE_PATH:
            self.state_writes += 1
            if self.state_writes == self.crash_at:
                raise Killed("state.json written")


class SessionHerdr(FakeHerdr):
    """FakeHerdr with a fresh agent's session named after the agent rather than counted from herdr's calls, so that
    a call a resume repeats does not rename every later session."""

    def start_agent(self, name, pane, kind, agent_args):
        fresh = orchestrator.RESUME_ARGS[kind] not in agent_args
        ready = super().start_agent(name, pane, kind, agent_args)
        if fresh and name in self.sessions:
            self.sessions[name] = f"session-{name}"
        return ready


class EveryPrompt:
    """A FakeHerdr script entry for a role that answers every prompt with the same turn: an agent prompted twice
    works twice, as a real one would."""

    def __init__(self, turn):
        self.turn = turn

    def pop(self, _index):
        return self.turn


@dataclass
class Aftermath:
    """What a run leaves behind. files: every file FakeHost holds, state.json aside. state: state.json, less
    heartbeat_at and owner, which only say which orchestrator wrote it when. effects: by kind, each effect on the
    world in order (World.effects). announce_repeats: how many times the interview's announce may come again, a known
    gap (`expected`); 0 for a run that must announce it once."""
    files: dict[str, str]
    state: dict
    effects: dict[str, list]
    announce_repeats: int = field(default=0)


class World:
    """One scenario's run and the world it runs in, which a crash leaves as it was: the fakes, and agents that keep
    working while no orchestrator runs.

    crashes holds the state.json each crash left, in order; finished is true once an orchestrator ran the run to its
    end.
    """

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.clock = FakeClock()
        self.host = CrashingHost()
        self.host.clock = self.clock
        self.jenkins = FakeCI()
        self.jenkins.outcomes = [outcome(**o) for o in scenario.gate or ()]
        self.pipeline = (orchestrator.load_workflow_file(scenario.workflow_file, BUILT_IN_PROMPTS)
                         if scenario.workflow_file else orchestrator.DEFAULT_WORKFLOW)
        self.state = RunState(RUN_ID, "add a rate limiter", "/proj", None, phase=self.pipeline.steps[0].id,
                              workflow=self.pipeline.name, pull_request=scenario.pull_request,
                              quality_job=JOB if scenario.gate is not None else None)
        self.herdr = SessionHerdr(self.host, self.state,
                                  {role: EveryPrompt(self._turn(role)) for role in self.pipeline.roles})
        self.notes = []  # (title, body) of each notification to the human
        # (when, agent, {path: text}) of each answer an agent is still working on.
        self._work = []
        self.clock.hooks.append(self._agents_work)
        self.crashes = []
        # Whether an orchestrator has run it already, so that the next one resumes it.
        self.started = False
        self.finished = False

    @property
    def state_writes(self) -> int:
        """How many times every orchestrator so far wrote state.json."""
        return self.host.state_writes

    def run(self, crash_after: int | None = None) -> bool:
        """Start the run, or once started resume it as `resume --force` does, and return whether it finished; with
        crash_after, it crashes right after that many writes of state.json, unless it finishes first.

        Raises OrchestratorError when the run fails or resume refuses it, and AssertionError when the run ends with
        another verdict than the scenario's last.
        """
        if self.started:
            args = orchestrator.parse_args(["resume", "--force", RUN_KEY])
            state, pipeline = orchestrator.resumed_state(args, self.host, "/proj")
        else:
            state, pipeline = self.state, self.pipeline
        self.started = True
        # The agents' turns read the run's paths from the state they are given.
        self.herdr.state = state
        # As run_workflow builds it, on the fakes.
        workflow = Workflow(
            self.herdr, self.host, state, notify=lambda title, body: self.notes.append((title, body)),
            max_rounds=state.max_rounds, turn_timeout=state.turn_timeout,
            agents=AgentSettings(state.permission_mode, state.models, state.agent_kinds),
            pull_request=state.pull_request, ci=ci_for(self.jenkins) if state.quality_job else None,
            max_quality_rounds=state.max_quality_rounds, pipeline=pipeline,
            clocks=Clocks(self.clock.sleep, self.clock, self.clock))
        self.host.crash_at = None if crash_after is None else self.host.state_writes + crash_after
        try:
            verdict = workflow.run()
        except Killed:
            self.crashes.append(self._saved_state())
            return False
        finally:
            self.host.crash_at = None
        if verdict != self.scenario.verdicts[-1]:
            raise AssertionError(f"the run ended {verdict}, not {self.scenario.verdicts[-1]}")
        self.finished = True
        return True

    def aftermath(self) -> Aftermath:
        """What the run has left behind so far."""
        files = dict(self.host.files)
        del files[STATE_PATH]
        state = self._saved_state()
        del state["heartbeat_at"], state["owner"]
        return Aftermath(files, state, self.effects())

    def effects(self) -> dict[str, list]:
        """Each effect the run had on the world so far, by kind, in order. Reads and checks are not effects: what
        the orchestrator only looks at, it may look at again."""
        return {
            "prompt": list(self.herdr.prompts),
            "herdr": [call for call in self.herdr.calls if call[0] != "prompt"],
            "git": [(cwd, args) for cwd, args in self.host.git_log
                    if args[0] in ("commit", "push", "switch", "merge", "worktree", "branch", "update-ref")],
            "snapshot": list(self.host.snapshots),
            "restore": list(self.host.restores),
            "pull request": list(self.host.prs) + list(self.host.pr_edits),
            "jenkins": [form["GIT_REF"] for method, url, form in self.jenkins.requests
                        if method == "POST" and url.endswith("/buildWithParameters")],
            "notification": list(self.notes),
        }

    def _saved_state(self) -> dict:
        return json.loads(self.host.files[STATE_PATH])

    def _turn(self, role: str):
        """The role's answer to a prompt: after TURN_SECONDS, its handoff file for the step and round the run is in,
        and an edit of the project if the role edits it."""
        def turn(prompt, state, host):
            step = self.pipeline.step(state.phase)
            n, q = state.round, state.quality_round if step.quality_gated else 0
            if step.loop_to is not None:
                text = f"VERDICT: {self.scenario.verdicts[n - 1]}\n1. finding"
            elif step is self.pipeline.contract:
                text = "# Add a token-bucket rate limiter\n\n## Goal\nlimit requests"
            else:
                text = f"{step.id} {n}.{q}"
            files = {step.path(state.dir, n, q): text}
            if role == "build":
                files["/proj/limiter.py"] = f"version {n}.{q}"
            elif role == "tests":
                files["/proj/test_limiter.py"] = "assert limit() == 3"
            self._work.append((self.clock() + TURN_SECONDS, f"{role}-{state.key}", files))
            return "working"
        return turn

    def _agents_work(self) -> None:
        """Write each answer whose time has come, and show its agent done."""
        due = [w for w in self._work if w[0] <= self.clock()]
        self._work = [w for w in self._work if w[0] > self.clock()]
        for _, agent, files in due:
            for path, text in files.items():
                self.host.write(path, text)
            self.herdr.statuses[agent] = "done"


def idempotent(kind: str, effect) -> bool:
    """Whether the code documents the effect as harmless to repeat, so that it may happen again right after itself;
    any other repeat is a failure, or a known gap that `expected` names."""
    match kind, effect:
        # The snapshot's push to its quality branch: "--force replaces what a failed attempt left"
        # (docs/quality-gate.md, Snapshotting the uncommitted tree). The ticket names it; no crash after a write of
        # state.json repeats it, since the save that records the push follows it at once.
        case "git", (_, ("push", "--quiet", "--force", "origin", refspec)) if f":refs/heads/{CI_REF_PREFIX}" in refspec:
            return True
        # _check_repo fast-forwards the base branch at each start in round 0 (docs/architecture.md, The phase machine).
        case "git", (_, ("merge", "--ff-only", *_)):
            return True
        # A snapshot moves no ref (docs/quality-gate.md, Snapshotting the uncommitted tree); a lock's snapshot, "a
        # resume before the run moves on does it again, harmlessly" (Workflow._hold_locks).
        case "snapshot", _:
            return True
    return False


def announces(kind: str, effect) -> bool:
    """Whether the effect is part of the announce of a human-paced turn (Workflow._step_turn): focusing the agent's
    pane, and telling the human it waits for them."""
    match kind, effect:
        case "herdr", ("focus", _):
            return True
        case "notification", (title, _) if title.endswith(" is waiting for you"):
            return True
    return False


def without_repeats(effects: dict[str, list], may_repeat) -> tuple[dict[str, list], int]:
    """effects without each effect that may_repeat(kind, effect) and that only repeats the one before it, and the
    most such repeats dropped of any one kind."""
    kept, most = {}, 0
    for kind, es in effects.items():
        kept[kind] = [e for i, e in enumerate(es) if not (i and e == es[i - 1] and may_repeat(kind, e))]
        most = max(most, len(es) - len(kept[kind]))
    return kept, most


def expected(uninterrupted: Aftermath, crashes: list[dict]) -> Aftermath:
    """What a run that crashed where each of crashes, the state.json each left, says leaves behind in the Python
    engine: uninterrupted's, but for three known gaps (TODO.md, Open issues), each pinned here.

    - A crash after `Workflow.run` saved phase done: a resume finds the run done and returns at once, so summary.md
      and metrics.json are never written, the human is never told the run finished, the workspace stays open, and
      the turns keep the tokens counted before the crash.
    - A crash after an agent's start was saved and before its session was: a resume finds the agent alive and
      prompts it without asking herdr for its session, so the record of the agent's next turn has none. SessionHerdr
      reports a session as soon as an agent starts, so an agent record without one is always that crash.
    - Each orchestrator that takes the interview's turn announces it again (Workflow._step_turn): the human is told
      once more that the interview waits, and its pane is focused, up to once per resume.
    """
    files, state = dict(uninterrupted.files), json.loads(json.dumps(uninterrupted.state))
    effects = {kind: list(es) for kind, es in uninterrupted.effects.items()}
    for crashed in crashes:
        if crashed["phase"] == DONE:
            files.pop(f"{RUN_DIR}/{SUMMARY_FILE}", None)
            files.pop(f"{RUN_DIR}/{METRICS_FILE}", None)
            effects["notification"] = [n for n in effects["notification"] if not n[0].startswith("Run finished")]
            effects["herdr"] = [c for c in effects["herdr"] if c[0] != "close"]
            state["turns"] = crashed["turns"]
            break
        ended = {t["file"] for t in crashed["turns"] if t["ended_at"]}
        for role, agent in crashed["agents"].items():
            if "session" not in agent:
                turn = next(t for t in state["turns"] if t["role"] == role and t["file"] not in ended)
                turn["session"] = None
    return Aftermath(files, state, effects, announce_repeats=len(crashes))


def ends_as_problems(got: Aftermath, want: Aftermath) -> list[str]:
    """What differs between got and want, as a line each; [] when nothing does. Repeats `idempotent` allows are no
    difference, nor up to want.announce_repeats repeats of the announce."""
    problems = []
    if got.files != want.files:
        problems.append(f"the files differ: {sorted(set(got.files.items()) ^ set(want.files.items()))}")
    if got.state != want.state:
        problems.append("state.json differs: " + ", ".join(k for k in want.state if got.state.get(k) != want.state[k]))
    got_effects, _ = without_repeats(got.effects, idempotent)
    want_effects, _ = without_repeats(want.effects, idempotent)
    got_effects, repeats = without_repeats(got_effects, announces)
    if repeats > want.announce_repeats:
        problems.append(f"the announce came {repeats} more times, at most {want.announce_repeats} expected")
    for kind in want_effects:
        if got_effects[kind] != want_effects[kind]:
            problems.append(f"the {kind} effects differ: {got_effects[kind]} != {want_effects[kind]}")
    return problems


# By scenario name, its aftermath when nothing crashes and how many writes of state.json that takes. Shared by every
# test and Hypothesis example, so no caller may change what it holds: `expected` copies before it edits.
_uninterrupted: dict[str, tuple[Aftermath, int]] = {}


def uninterrupted(scenario: Scenario) -> tuple[Aftermath, int]:
    """The scenario's aftermath when nothing crashes, and how many writes of state.json it takes."""
    if scenario.name not in _uninterrupted:
        world = World(scenario)
        world.run()
        _uninterrupted[scenario.name] = (world.aftermath(), world.state_writes)
    return _uninterrupted[scenario.name]


class TestCrashAfterEveryWrite(unittest.TestCase):
    """For each scenario and each k: crash after write k of state.json, resume once, and end as `expected` says."""

    def crash_after_every_write(self, name: str) -> None:
        scenario = SCENARIOS[name]
        reference, writes = uninterrupted(scenario)
        for k in range(1, writes + 1):
            with self.subTest(k=k):
                world = World(scenario)
                self.assertFalse(world.run(crash_after=k))
                self.assertTrue(world.run())
                self.assertEqual(ends_as_problems(world.aftermath(), expected(reference, world.crashes)), [])

    def test_approved_in_round_1(self):
        self.crash_after_every_write("approved")

    def test_changes_requested_then_approved(self):
        self.crash_after_every_write("changes_then_approved")

    def test_quality_gate_fails_then_passes(self):
        self.crash_after_every_write("gate_fails_then_passes")

    def test_product_team_with_locked_tests(self):
        self.crash_after_every_write("product_team")

    def test_no_pull_request(self):
        self.crash_after_every_write("no_pr")

    def test_agents_work_past_a_heartbeat(self):
        # A heartbeat save during each agent's turn is what lets a crash land while an agent works.
        self.assertGreater(TURN_SECONDS, HEARTBEAT_SECONDS)

    def test_a_repeated_prompt_is_a_difference(self):
        reference, _ = uninterrupted(SCENARIOS["approved"])
        again = Aftermath(reference.files, reference.state,
                          {**reference.effects, "prompt": [*reference.effects["prompt"], reference.effects["prompt"][-1]]})
        self.assertEqual(len(ends_as_problems(again, expected(reference, []))), 1)


class CrashesAndResumes(RuleBasedStateMachine):
    """A run crashed after a drawn number of writes of state.json, resumed, and crashed again as often as drawn, a
    crash during a resume included; at the end resumed until it finishes.

    After each step, the effects so far are a beginning of the uninterrupted run's; at the end, the run ended as
    `expected` says.
    """

    @initialize(name=st.sampled_from(sorted(SCENARIOS)))
    def start(self, name):
        self.world = World(SCENARIOS[name])
        self.reference, _ = uninterrupted(SCENARIOS[name])

    # Half the most writes a scenario takes, or less: TestCrashAfterEveryWrite covers one crash after each write
    # already, so the draws favour a run crashed several times. A run that finishes stays finished: Hypothesis needs a
    # rule it can always take.
    @rule(k=st.integers(min_value=1, max_value=24))
    def crash_after(self, k):
        if not self.world.finished:
            self.world.run(crash_after=k)

    @invariant()
    def effects_so_far_begin_the_expected(self):
        want = expected(self.reference, self.world.crashes)
        got_effects, _ = without_repeats(without_repeats(self.world.effects(), idempotent)[0], announces)
        want_effects, _ = without_repeats(without_repeats(want.effects, idempotent)[0], announces)
        for kind, effects in got_effects.items():
            assert effects == want_effects[kind][:len(effects)], (kind, effects, want_effects[kind])

    def teardown(self):
        world = getattr(self, "world", None)
        if world is None:
            return
        while not world.finished:
            world.run()
        problems = ends_as_problems(world.aftermath(), expected(self.reference, world.crashes))
        assert not problems, problems


TestCrashesAndResumes = CrashesAndResumes.TestCase
TestCrashesAndResumes.settings = settings(stateful_step_count=8)
