"""A stand-in for the engine evals/run.py runs, for tests/test_evals.py: a program that plays one scripted run.

The script is the JSON file FAKE_ENGINE_PLAN names:

- "files": paths, relative to --cwd, and the text to write there, as the agents would;
- "exit": the run's exit status;
- "run_id": the name of the run directory to make under .orchestrator/runs, or null for none;
- "metrics": what to write as that directory's metrics.json, or null for none.

Each invocation appends a JSON line to the file FAKE_ENGINE_LOG names: its arguments, and for `run`, what the agents
could see: every path under --cwd but .git's, origin's URL, and the spec's path and text.
"""

import json
import os
import subprocess
import sys


def what_the_agents_see(cwd: str, spec: str) -> dict:
    """What the agents of a run in cwd with the spec at spec could see there, as the log's entry gives it."""
    paths = []
    for d, dirs, names in os.walk(cwd):
        dirs[:] = [x for x in dirs if x != ".git"]
        paths += [os.path.relpath(os.path.join(d, name), cwd) for name in dirs + names]
    origin = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],  # noqa: S603, S607 - git on PATH
                            capture_output=True, text=True, check=True).stdout.strip()
    with open(spec, encoding="utf-8") as f:
        return {"paths": sorted(paths), "origin": origin, "spec": spec, "spec_text": f.read()}


def main(argv: list[str]) -> int:
    with open(os.environ["FAKE_ENGINE_PLAN"], encoding="utf-8") as f:
        plan = json.load(f)
    entry: dict = {"argv": argv}
    if argv[0] == "run":
        cwd, spec = argv[argv.index("--cwd") + 1], argv[argv.index("--spec") + 1]
        entry.update(what_the_agents_see(cwd, spec))
        for path, text in plan.get("files", {}).items():
            full = os.path.join(cwd, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(text)
        if plan.get("run_id"):
            run_dir = os.path.join(cwd, ".orchestrator", "runs", plan["run_id"])
            os.makedirs(run_dir)
            if plan.get("metrics") is not None:
                with open(os.path.join(run_dir, "metrics.json"), "w", encoding="utf-8") as f:
                    json.dump(plan["metrics"], f)
    with open(os.environ["FAKE_ENGINE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return plan.get("exit", 0) if argv[0] == "run" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
