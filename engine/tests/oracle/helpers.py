"""Write engine/tests/oracle/helpers.json: what the Python engine's text helpers make of each run the parity scenarios
leave behind (tests/parity/recorded/, its run directory's state.json and handoff files): the commit note, trailers and
pull request body, summary.md, metrics.json, the turn tables and the proofs section. The Rust engine's tests compute
each from the same inputs and compare.

The recorded times and durations are normalized (tests/parity/normalize.py); each <time> and <seconds> is given a
value of its own here, in order (denormalized), so that the helpers that compute with them can. The handoff files
are each run's last of their kind, by name (inputs). A few runs are also given with conflicts, so that the pull
request's warning is written. The file holds the outputs; the Rust tests read the inputs from the recordings
themselves, as these functions do. Run `uv run python engine/tests/oracle/helpers.py` to rewrite it."""

import glob
import itertools
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
import orchestrator as o  # found through the path above

OUT = os.path.join(os.path.dirname(__file__), "helpers.json")

# 2026-10-03T04:00:00+00:00, when the first recorded time is taken to be.
EPOCH = 1_791_000_000
NOW = EPOCH + 7 * 3600


def denormalized(text: str) -> str:
    """text with each "<time>" a time a minute after the one before, from EPOCH, and each "<seconds>" a count."""
    times, counts = itertools.count(), itertools.count(5, 7)
    text = re.sub(r'"<time>"', lambda _: json.dumps(o.iso_utc(EPOCH + 60 * next(times))), text)
    return re.sub(r'"<seconds>"', lambda _: str(next(counts)), text)


def last(files: dict, run_dir: str, pattern: str) -> str | None:
    """The text of the last file of the run directory whose name matches pattern, by name; None for none."""
    names = sorted(n for n in files if n.startswith(run_dir) and re.fullmatch(pattern, n[len(run_dir):]))
    return files[names[-1]] if names else None


def inputs(files: dict, run_dir: str, conflicts: list[str]) -> tuple:
    """The run in run_dir as the helpers take it: (saved state, state, spec, report, review, gate evidence)."""
    saved = json.loads(denormalized(files[f"{run_dir}state.json"]))
    saved["conflicts"] = conflicts or saved["conflicts"]
    state = o.RunState.from_dict(saved)
    spec = last(files, run_dir, r"spec\.md") or state.task
    report = last(files, run_dir, r"build-\d+(-q\d+)?\.md") or ""
    review = last(files, run_dir, r"review-\d+\.md") or ""
    quality_names = sorted(n for n in files if re.fullmatch(rf"{re.escape(run_dir)}quality-\d+-\d+\.md", n))
    quality = None
    if quality_names:
        n, q = map(int, re.findall(r"\d+", quality_names[-1][len(run_dir):]))
        quality = o.QualityFile(n, q, files[quality_names[-1]])
    return saved, state, spec, report, review, o.GateEvidence(quality, state.quality_gate_disabled)


def case(recording: str, files: dict, run_dir: str, conflicts: list[str]) -> dict:
    """The helpers' outputs for the run in run_dir of the recording, with conflicts."""
    saved, state, spec, report, review, evidence = inputs(files, run_dir, conflicts)
    pipeline = o.saved_pipeline(saved)
    rounds = [f"Round {k}: as recorded" for k in range(1, state.round + 1)]
    return {
        "recording": recording, "run_dir": run_dir, "conflicts": conflicts,
        "spec_title": o.spec_title(spec),
        "branch_name": o.branch_name(o.spec_title(spec) or state.task, state.run_id),
        "verdict_line": o.verdict_line(state),
        "commit_note": o.commit_note(state),
        "provenance": o.provenance(state, evidence),
        "pr_body": o.pr_body(state, spec, report, review, evidence),
        "turn_lines": o.turn_lines(state.turns),
        "run_summary": None if pipeline is None else o.run_summary(state, pipeline, rounds, NOW),
        "run_metrics": json.dumps(o.run_metrics(state, NOW)),
        "proofs_section": None if not state.proofs or pipeline is None else o.proofs_section(
            pipeline.proof_command or "", state.work_dir, review, state.proofs),
    }


# The runs also given with conflicts, by recording.
WITH_CONFLICTS = ["default_approved.json", "quality_passes.json", "workflow_quick.json"]


def build() -> dict:
    """The oracle: the times the inputs are given, and a case per run of each recording."""
    cases = []
    for path in sorted(glob.glob(os.path.join(ROOT, "tests", "parity", "recorded", "*.json"))):
        recording = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            files = json.load(f)["run_files"]
        for state_path in sorted(n for n in files if n.endswith("/state.json")):
            run_dir = state_path.removesuffix("state.json")
            cases.append(case(recording, files, run_dir, []))
            if recording in WITH_CONFLICTS:
                cases.append(case(recording, files, run_dir, ["a.py", "docs/b|c.md"]))
    return {"epoch": EPOCH, "now": NOW, "cases": cases}


def text(oracle: dict) -> str:
    """The file's content for the oracle build() gives."""
    return json.dumps(oracle, indent=1) + "\n"


def main() -> None:
    """Write the oracle to OUT."""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text(build()))


if __name__ == "__main__":
    main()
