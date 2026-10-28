"""Crash the orchestrator right after a save of state.json, resume the run as `resume --force` does, and check that it
ends as an uninterrupted run does: the same files, the same state.json less its heartbeat and owner, and each side
effect on the world once.

A crash is `Crash`, raised by `Workflow._save` right after it wrote state.json. It derives from BaseException, so no
`except` clause in `Workflow.run` sees it and nothing is released or saved: as with SIGKILL, the run is left as that
save left it. The resume is `resumed_state`, which main calls for `resume --force`, and a new `Workflow` on the same
fakes, which play the world that outlives the orchestrator: the project (FakeHost), herdr and its agents
(FakeHerdr), Jenkins (FakeCI) and time (FakeClock).

`TestCrashAfterEverySave` crashes once, after save k for every k of each scenario. `CrashesAndResumes` crashes at
points Hypothesis draws, as often as it draws them, a resume included.

What the parity harness (tests/parity) covers instead: it kills the engine as a program, with SIGKILL at any moment, so
also between two saves, on real git; but only at the points where a scenario holds it, one per phase. This harness
crashes after every save but only after a save, and only on the fakes, so a bug in how Host drives git, gh or herdr is
not its to find (docs/architecture.md, Tests).
"""

import json
import unittest
from dataclasses import dataclass

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
    METRICS_FILE,
    SUMMARY_FILE,
    AgentSettings,
    Clocks,
    RunState,
    Workflow,
)


class Crash(BaseException):
    """The orchestrator's process died: like SIGKILL, no except clause in Workflow.run sees it."""


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


SCENARIOS = (
    Scenario("approved"),
    Scenario("changes_then_approved", verdicts=(CHANGES_REQUESTED, APPROVE)),
    Scenario("gate_fails_then_passes", gate=(RED, {})),
    # The ticket's `tdd`, dropped since (9838e3f): product-team's QA Engineer writes the tests first, and they are
    # locked, as tdd's were.
    Scenario("product_team", workflow_file=f"{EXAMPLES}/product-team.toml"),
    Scenario("no_pr", pull_request=False),
)


class SessionHerdr(FakeHerdr):
    """FakeHerdr with a fresh agent's session named after the agent rather than counted from herdr's calls, so that
    a call a resume repeats, which may be allowed (repeatable), does not rename every later session."""

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
class Outcome:
    """What a run leaves behind. files: every file FakeHost holds, state.json aside. state: state.json, less
    heartbeat_at and owner, which only say which orchestrator wrote it when. effects: by kind, each effect on the
    world in order (World.effects)."""
    files: dict[str, str]
    state: dict
    effects: dict[str, list]


class World:
    """One scenario's run and the world it runs in, which a crash leaves as it was: the fakes, and agents that keep
    working while no orchestrator runs.

    crashes holds the state.json each crash left, in order; saves counts every save of every orchestrator so far;
    finished is true once an orchestrator ran the run to its end.
    """

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.clock = FakeClock()
        self.host = FakeHost()
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
        self.saves = 0
        # Whether an orchestrator has run it already, so that the next one resumes it.
        self.started = False
        self.finished = False

    def run(self, crash_after: int | None = None) -> bool:
        """Start the run, or once started resume it as `resume --force` does, and return whether it finished; with
        crash_after, it crashes right after that many saves, unless it finishes first.

        Raises AssertionError when the run fails, and OrchestratorError when resume refuses it.
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
        save, saves = workflow._save, 0

        def save_then_crash():
            nonlocal saves
            save()
            saves += 1
            self.saves += 1
            if saves == crash_after:
                self.crashes.append(self._saved_state())
                raise Crash
        workflow._save = save_then_crash
        try:
            verdict = workflow.run()
        except Crash:
            return False
        if verdict != self.scenario.verdicts[-1]:
            raise AssertionError(f"the run ended {verdict}, not {self.scenario.verdicts[-1]}")
        self.finished = True
        return True

    def outcome(self) -> Outcome:
        """What the run has left behind so far."""
        files = dict(self.host.files)
        del files[STATE_PATH]
        state = self._saved_state()
        del state["heartbeat_at"], state["owner"]
        return Outcome(files, state, self.effects())

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


def repeatable(kind: str, effect) -> bool:
    """Whether the effect may happen again right after itself, because the code documents it as idempotent or as
    done by every orchestrator that gets there. Any other repeat is a failure."""
    match kind, effect:
        # The snapshot's push to its quality branch: "--force replaces what a failed attempt left"
        # (docs/quality-gate.md, Snapshotting the uncommitted tree).
        case "git", (_, ("push", "--quiet", "--force", "origin", refspec)) if f":refs/heads/{CI_REF_PREFIX}" in refspec:
            return True
        # _check_repo fast-forwards the base branch at each start in round 0 (docs/architecture.md, The phase machine).
        case "git", (_, ("merge", "--ff-only", *_)):
            return True
        # A snapshot moves no ref (docs/quality-gate.md, Snapshotting the uncommitted tree). A lock's snapshot, and the
        # restore of locked files: "a resume before the run moves on does it again, harmlessly" (Workflow._hold_locks).
        case "snapshot" | "restore", _:
            return True
        # announce(how) "runs once the agent is ready" (Workflow._turn), in each orchestrator that takes the turn: a
        # resumed interview tells the human again that the Spec Collector waits, and focuses its pane.
        case "herdr", ("focus", _):
            return True
        case "notification", (title, _) if title.endswith(" is waiting for you"):
            return True
    return False


def collapsed(effects: dict[str, list]) -> dict[str, list]:
    """effects without each repeatable effect that only repeats the one before it."""
    return {kind: [e for i, e in enumerate(es) if not (i and e == es[i - 1] and repeatable(kind, e))]
            for kind, es in effects.items()}


def expected(uninterrupted: Outcome, crashes: list[dict]) -> Outcome:
    """The outcome of a run that crashed where each of crashes, the state.json each left, says, as the Python engine
    ends it: uninterrupted's, but for two known gaps (TODO.md, Open issues), each pinned here so that a fix fails the
    test and removes it.

    - A crash after `Workflow.run` saved phase done: a resume finds the run done and returns at once, so summary.md
      and metrics.json are never written, the human is never told the run finished, the workspace stays open, and
      the turns keep the tokens counted before the crash.
    - A crash after an agent's start was saved and before its session was: a resume finds the agent alive and
      prompts it without asking herdr for its session, so the record of the agent's next turn has none.
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
    return Outcome(files, state, effects)


_uninterrupted: dict[str, tuple[Outcome, int]] = {}


def uninterrupted(scenario: Scenario) -> tuple[Outcome, int]:
    """The scenario's outcome when nothing crashes, and how many saves it takes."""
    if scenario.name not in _uninterrupted:
        world = World(scenario)
        world.run()
        _uninterrupted[scenario.name] = (world.outcome(), world.saves)
    return _uninterrupted[scenario.name]


def assert_ends_as(world: World, want: Outcome) -> None:
    """Raise AssertionError, naming what differs, unless world's run ended with want's files and state, and with its
    effects but for the repeats `repeatable` allows."""
    got = world.outcome()
    assert got.files == want.files, "the files differ"
    assert got.state == want.state, "state.json differs"
    got_effects, want_effects = collapsed(got.effects), collapsed(want.effects)
    for kind in want_effects:
        assert got_effects[kind] == want_effects[kind], f"the {kind} effects differ"


class TestCrashAfterEverySave(unittest.TestCase):
    """For each scenario and each k: crash after save k, resume once, and end as the uninterrupted run does."""

    def crash_after_every_save(self, scenario: Scenario) -> None:
        reference, saves = uninterrupted(scenario)
        for k in range(1, saves + 1):
            with self.subTest(k=k):
                world = World(scenario)
                self.assertFalse(world.run(crash_after=k))
                self.assertTrue(world.run())
                assert_ends_as(world, expected(reference, world.crashes))

    def test_approved_in_round_1(self):
        self.crash_after_every_save(SCENARIOS[0])

    def test_changes_requested_then_approved(self):
        self.crash_after_every_save(SCENARIOS[1])

    def test_quality_gate_fails_then_passes(self):
        self.crash_after_every_save(SCENARIOS[2])

    def test_product_team_with_locked_tests(self):
        self.crash_after_every_save(SCENARIOS[3])

    def test_no_pull_request(self):
        self.crash_after_every_save(SCENARIOS[4])

    def test_every_scenario_saves_more_than_its_turns(self):
        # A heartbeat during each agent's turn is what lets a crash land while an agent works.
        for scenario in SCENARIOS:
            reference, saves = uninterrupted(scenario)
            self.assertGreater(saves, 2 * len(reference.state["turns"]), scenario.name)


class CrashesAndResumes(RuleBasedStateMachine):
    """A run crashed after a drawn number of saves, resumed, and crashed again as often as drawn, a crash during a
    resume included; at the end resumed until it finishes.

    After each step, the effects so far are a beginning of the uninterrupted run's; at the end, the run ended as
    `expected` says.
    """

    @initialize(scenario=st.sampled_from(SCENARIOS))
    def start(self, scenario):
        self.world = World(scenario)
        self.reference, _ = uninterrupted(scenario)

    # Half the most saves a scenario takes, or less: TestCrashAfterEverySave covers one crash at each save already, so
    # the draws favour a run crashed several times. A run that finishes stays finished: Hypothesis needs a rule it can
    # always take.
    @rule(k=st.integers(min_value=1, max_value=20))
    def crash_after(self, k):
        if not self.world.finished:
            self.world.run(crash_after=k)

    @invariant()
    def no_effect_repeated(self):
        got = collapsed(self.world.effects())
        want = collapsed(expected(self.reference, self.world.crashes).effects)
        for kind, effects in got.items():
            assert effects == want[kind][:len(effects)], (kind, effects, want[kind])

    def teardown(self):
        world = getattr(self, "world", None)
        if world is None:
            return
        while not world.finished:
            world.run()
        assert_ends_as(world, expected(self.reference, world.crashes))


TestCrashesAndResumes = CrashesAndResumes.TestCase
TestCrashesAndResumes.settings = settings(stateful_step_count=8)
