"""Write engine/tests/oracle/workflows.json: every workflow the repository defines, as the TOML a workflow file holds,
with what the Python engine makes of it: its canonical definition (workflow_definition), its file (workflow_toml) and
its shape, or the error that refuses it. The Rust engine's tests parse each and compare.

The workflows: examples/workflows/, the ones the parity scenarios write (tests/parity/scenarios.py), the ones
tests/test_orchestrator.py defines, and the default. Run `uv run python engine/tests/oracle/workflows.py` to rewrite
it."""

import ast
import glob
import json
import os
import sys
import tomllib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import test_orchestrator  # found through the path above

import orchestrator  # found through the path above

OUT = os.path.join(os.path.dirname(__file__), "workflows.json")

# The workflows tests/test_orchestrator.py defines as Pipelines, by their names there.
TEST_WORKFLOWS = ["QUICK", "FOUR", "IMPL", "LOCKING", "SOLO"]


def scenario_files() -> list[tuple[str, str]]:
    """(name, text) of each .toml file a parity scenario writes with a literal text, by w.write's first argument."""
    path = os.path.join(ROOT, "tests", "parity", "scenarios.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    constants = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                 for target in node.targets if isinstance(target, ast.Name)}
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "write"
                and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)):
            continue
        where, text = node.args
        if not str(where.value).endswith(".toml"):
            continue
        if isinstance(text, ast.Name) and text.id in constants:
            text = constants[text.id]
        try:
            found.append((os.path.splitext(os.path.basename(where.value))[0], ast.literal_eval(text)))
        except ValueError:
            continue  # a file's text read at run time, such as an example's
    return found


def sources() -> list[tuple[str, str, str]]:
    """(name, where it comes from, TOML text) of every workflow."""
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "examples", "workflows", "*.toml"))):
        with open(path, encoding="utf-8") as f:
            out.append((os.path.splitext(os.path.basename(path))[0], os.path.relpath(path, ROOT), f.read()))
    out += [(name, "tests/parity/scenarios.py", text) for name, text in scenario_files()]
    for name in TEST_WORKFLOWS:
        p = getattr(test_orchestrator, name)
        out.append((p.name, f"tests/test_orchestrator.py {name}", orchestrator.workflow_toml(p)))
    out.append(("default", "orchestrator.DEFAULT_WORKFLOW",
                orchestrator.workflow_toml(orchestrator.DEFAULT_WORKFLOW)))
    return out


def outcome(name: str, text: str) -> dict:
    """What the Python engine makes of the workflow file text named name."""
    try:
        p = orchestrator.parse_workflow(name, tomllib.loads(text), orchestrator.BUILT_IN_PROMPTS)
    except ValueError as e:
        return {"error": str(e)}
    return {"definition": json.dumps(orchestrator.workflow_definition(p)), "toml": orchestrator.workflow_toml(p),
            "shape": orchestrator.workflow_shape(p),
            "built_in": orchestrator.is_built_in(p, orchestrator.BUILT_IN_PROMPTS)}


def build() -> list:
    """The oracle: each workflow with its source and the Python engine's outcome."""
    return [{"name": name, "from": where, "toml": text, "outcome": outcome(name, text)}
            for name, where, text in sources()]


def text(oracle: list) -> str:
    """The file's content for the oracle build() gives."""
    return json.dumps(oracle, indent=1) + "\n"


def main() -> None:
    """Write the oracle to OUT."""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text(build()))


if __name__ == "__main__":
    main()
