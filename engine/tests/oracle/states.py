"""Write engine/tests/oracle/states.json: what the Python engine writes back after reading a state.json
(RunState.from_dict, then asdict and json.dumps as Workflow._save does), for every state.json the parity recordings
hold (tests/parity/recorded/) and a few older states. The Rust engine's tests read each into its RunState and write it
back.

A recorded state the Python engine writes back unchanged is only counted, since the Rust tests read the recordings
themselves; one it changes, and each older state, is listed with what it writes back. Run
`uv run python engine/tests/oracle/states.py` to rewrite it."""

import dataclasses
import glob
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
import orchestrator  # found through the path above

OUT = os.path.join(os.path.dirname(__file__), "states.json")

# States saved by older engines, which a newer one still reads: 0.3.0's agent_args in place of permission_mode, and
# one saved before workflows, prompt files, turns and the other later fields existed.
OLDER = [
    {"run_id": "20260101-000000-abc123", "task": "t", "cwd": "/p", "phase": "build", "round": 1,
     "agents": {"spec": {"name": "spec-abc123", "pane": "w1:p1"}}, "agent_args": ["--permission-mode", "acceptEdits"]},
    {"run_id": "20260101-000000-abc124", "task": "t", "cwd": "/p", "agent_args": []},
    {"run_id": "20260101-000000-abc125", "task": "t", "cwd": "/p", "agent_args": ["--model", "x"]},
    {"run_id": "20260101-000000-abc126", "task": "t"},
    {"run_id": "r-abc", "task": "t", "cwd": "/p", "workflow": "default", "root_pane": "",
     "agents": {"review": {"name": "review-abc", "pane": "w9:p2"}, "spec": {"name": "spec-abc", "pane": "w9:p1"}}},
]


def saved_states(recording) -> list[str]:
    """The state.json texts a recording holds: each one the ledger saw, as json.dumps writes it, and the run files'."""
    found = []

    def walk(x) -> None:
        if isinstance(x, dict):
            if str(x.get("path", "")).endswith("state.json") and isinstance(x.get("saved"), dict):
                found.append(json.dumps(x["saved"]) + "\n")
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(recording["events"])
    found += [text for path, text in recording["run_files"].items() if path.endswith("state.json")]
    return found


def written_back(text: str) -> dict:
    """What the Python engine writes after reading text: {"text": ...}, or {"error": ...} when it refuses it."""
    try:
        state = orchestrator.RunState.from_dict(json.loads(text))
    except orchestrator.OrchestratorError as e:
        return {"error": str(e)}
    return {"text": json.dumps(dataclasses.asdict(state)) + "\n"}


def recorded_states() -> list[str]:
    """Each distinct state.json text of the recordings, in the order of their files and within each."""
    texts = []
    for path in sorted(glob.glob(os.path.join(ROOT, "tests/parity/recorded/*.json"))):
        with open(path, encoding="utf-8") as f:
            texts += saved_states(json.load(f))
    return list(dict.fromkeys(texts))


def build() -> dict:
    """The oracle: how many distinct states the recordings hold, and each state the Python engine writes back
    otherwise than it was, with what it writes back."""
    recorded = recorded_states()
    texts = recorded + [json.dumps(state) + "\n" for state in OLDER]
    changed = []
    for text in texts:
        back = written_back(text)
        if back.get("text") != text:
            changed.append({"text": text, "back": back})
    return {"recorded": len(recorded), "changed": changed}


def text(oracle: dict) -> str:
    """The file's content for the oracle build() gives."""
    return json.dumps(oracle, indent=1) + "\n"


def main() -> None:
    """Write the oracle to OUT."""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text(build()))


if __name__ == "__main__":
    main()
