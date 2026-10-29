"""The parity scenarios, each run against the engine and compared with its recording in recorded/.

A scenario's record (World.record) must equal recorded/<name>.json; a difference fails the test, naming where the
two first part. `PARITY_RECORD=1 uv run pytest tests/parity` writes the recordings instead, from the engine
PARITY_ENGINE names (world.engine_command); record only from the Python engine, the oracle, and read the diff of
what changed before committing it.

With PARITY_ENGINE set, each scenario that kills a run and resumes it (SWITCHED) also runs with the engines switched
at the kill, the Python engine first and PARITY_ENGINE's after it and the reverse, against the same recording: a run
one engine leaves resumes in the other as in itself (the rust-engine spec's Decisions, "Runs carry over").

With PARITY_COMMANDS set to commands of the engine, comma-separated, such as `run,resume`, a scenario whose recording
invokes another command is left out, for an engine that has only those (the Rust engine, until its CLI is whole); one
not recorded yet runs, and fails for want of its recording.
"""

import json
import os

import pytest
from scenarios import SCENARIOS
from world import World, engine_command, python_engine

RECORDED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recorded")


def first_difference(want, got, path: str = "$") -> str | None:
    """Where got first differs from want, two JSON-ready values, as a JSON path and the two sides there; None when
    they are equal. For strings, the line on which they part, with the lines around it."""
    if type(want) is not type(got):
        return f"{path}: recorded {json.dumps(want)[:300]}, got {json.dumps(got)[:300]}"
    if isinstance(want, dict):
        for key in list(want) + [k for k in got if k not in want]:
            if key not in got or key not in want:
                return f"{path}: key {key!r} only {'recorded' if key in want else 'in this run'}"
            if (found := first_difference(want[key], got[key], f"{path}[{key!r}]")) is not None:
                return found
        if list(want) != list(got):
            return f"{path}: keys in another order: recorded {list(want)}, got {list(got)}"
        return None
    if isinstance(want, list):
        for i, (a, b) in enumerate(zip(want, got, strict=False)):
            if (found := first_difference(a, b, f"{path}[{i}]")) is not None:
                return found
        if len(want) != len(got):
            extra = want[len(got):] if len(want) > len(got) else got[len(want):]
            return (f"{path}: recorded {len(want)} items, got {len(got)}; first unmatched "
                    f"({'recorded' if len(want) > len(got) else 'got'}): {json.dumps(extra[0])[:500]}")
        return None
    if want == got:
        return None
    if isinstance(want, str):
        a, b = want.split("\n"), got.split("\n")
        i = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
        return (f"{path}, line {i + 1}:\n  recorded: {a[i] if i < len(a) else '(no line)'!r}\n"
                f"  got:      {b[i] if i < len(b) else '(no line)'!r}")
    return f"{path}: recorded {want!r}, got {got!r}"


# The scenarios whose run is killed in a phase and resumed by a second invocation, with or without its quality gate.
SWITCHED = sorted(name for name in SCENARIOS if name.startswith(("kill_in_", "gate_turned_")))


def recorded_commands(name: str) -> set[str] | None:
    """The commands scenario name's recording invokes the engine with: each invocation's first argument; None for a
    scenario not recorded yet, which test_scenario records or fails as with every command."""
    try:
        with open(os.path.join(RECORDED, f"{name}.json")) as f:
            recording = json.load(f)
    except FileNotFoundError:
        return None
    return {invocation["args"][0] for invocation in recording["invocations"]}


def runs() -> list:
    """test_scenario's parameters: each scenario on engine_command, as (name, None); with PARITY_ENGINE set, also each
    of SWITCHED with the engines switched at the kill, as (name, engines), engines being World's. With
    PARITY_COMMANDS set, without the scenarios whose recording invokes the engine with another command."""
    names = sorted(SCENARIOS)
    if commands := os.environ.get("PARITY_COMMANDS"):
        names = [name for name in names
                 if (recorded := recorded_commands(name)) is None or recorded <= set(commands.split(","))]
    out = [pytest.param(name, None, id=name) for name in names]
    if os.environ.get("PARITY_ENGINE"):
        python, other = python_engine(), engine_command()
        for name in [name for name in SWITCHED if name in names]:
            out.append(pytest.param(name, [python, other], id=f"{name}-python-then-engine"))
            out.append(pytest.param(name, [other, python], id=f"{name}-engine-then-python"))
    return out


@pytest.mark.loopback
@pytest.mark.real_sleep
@pytest.mark.parametrize(("name", "engines"), runs())
def test_scenario(name, engines, tmp_path):
    with World(str(tmp_path), engines) as world:
        SCENARIOS[name](world)
        got = world.record()
    path = os.path.join(RECORDED, f"{name}.json")
    if os.environ.get("PARITY_RECORD") == "1":
        if os.environ.get("PARITY_ENGINE") or engines:
            pytest.fail("PARITY_RECORD records the oracle, the Python engine; unset PARITY_ENGINE to record")
        os.makedirs(RECORDED, exist_ok=True)
        with open(path, "w") as f:
            json.dump(got, f, indent=1, ensure_ascii=False)
            f.write("\n")
        return
    if not os.path.exists(path):
        pytest.fail(f"scenario {name} has no recording; make it with PARITY_RECORD=1 from the Python engine")
    with open(path) as f:
        want = json.load(f)
    if (difference := first_difference(want, got)) is not None:
        pytest.fail(f"scenario {name} differs from its recording at {difference}")
