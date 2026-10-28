"""Write engine/tests/oracle/checks.json: what orchestrator.py's checks give for the cases the Rust engine's checks
tests replay: judge_run of tests/test_orchestrator.py's TestJudgeRun quality files, written by gate.py's
quality_report, and of every quality file a parity recording holds; the manifests python_dependencies reads; the
requirements, spec names and lines that name a package; and TOML values as Python writes them.

A recorded quality file (tests/parity/recorded/) is the build's quality.md that the scenario's Jenkins served, with
judge_run's additions and then the line naming the build under its GATE line. The generator rebuilds judge_run's input
from the scenario (tests/parity/scenarios.py, played on a stub that keeps only what Jenkins and PyPI serve) and the
change from the recorded repository, diffed by the Python engine's Host on real git; and it checks that judge_run
gives the recorded file, so a stale reconstruction fails here rather than in the Rust tests. Run
`uv run python engine/tests/oracle/checks.py` to rewrite it."""

import contextlib
import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "tests", "parity")]
import orchestrator as o  # found through the path above
import scenarios  # found through the path above
import test_orchestrator as t  # found through the path above

OUT = os.path.join(os.path.dirname(__file__), "checks.json")
# The time every change is judged at: 2026-10-23, TestOrchestratorChecks' TODAY.
NOW = t.TODAY


def finding(path, line, rule, message="m", severity="error") -> dict:
    return {"path": path, "line": line, "severity": severity, "rule": rule, "message": message}


def judge_run_cases() -> list[dict]:
    """TestJudgeRun's and TestQualityFileContract's quality files and checks, and a few more, with judge_run's
    output."""
    f = finding
    contract = t.TestQualityFileContract()
    cases = [
        (t.quality_report("ERROR", "intro", conditions=["Coverage of new code is 50%."], issues=[f("a.py", 5, "ruff F401")],
                          to_weigh=[f("t.py", 5, "flaky-tests mixed-results")], tests=[]),
         [("new-dependencies", [f("pyproject.toml", 5, "not-in-spec")], []), ("diff-budget", [], [f(None, None, "")])]),
        (t.quality_report("OK", "intro", tests=[]),
         [("c", [f("pyproject.toml", 5, "r")], []), ("d", [f("pyproject.toml", 5, "s")] * 2, [])]),
        (t.quality_report("OK", "intro", tests=[]), [("c", [], [])]),
        (t.quality_report("OK", "intro"), [("c", [], [f("pyproject.toml", 5, "r")] * 60)]),
        (t.quality_report("ERROR", "intro", issues=[f("a.py", 5, "ruff F401")] * 60),
         [("c", [f("pyproject.toml", 5, "r")] * 2, [f("pyproject.toml", 5, "w")] * 3)]),
        (t.quality_report("ERROR", "intro", issues=[f("a.py", 5, "ruff F401")] * 48),
         [("c", [f("pyproject.toml", 5, "r")] * 2, [f("pyproject.toml", 5, "w")] * 3)]),
        (contract.full_report("OK", 0)[0],
         [("new-dependencies", [f("pyproject.toml", 5, "not-in-spec", "names a package at 7")],
           [f(None, None, "over-budget", severity="warning")])]),
        ("GATE: OK\n\nintro\n", [("n", [f("c\nd .py", 1, "flaky x", "`c\nd .py::t` failed")], [])]),
        ("\n\n**GATE: OK**\n\nintro\n\n## Failed conditions\n\n- one\n- two\n\n## Issues\n\n٣. x\n12 y\n",
         [("c", [f("a.py", None, "r", "whole file")], [f(None, None, "", "the change as a whole")])]),
        ("## GATE: OK\n## Findings\n\n7. `a.py:1` error x: y", [("c", [f("a.py", 2, "")], [])]),
        ("GATE: OKAY\n\n## Failed conditions\n", [("c", [f("a.py", 2, "r")], [])]),
        ("", [("c", [f(None, None, "r")], [])]),
    ]
    out = []
    for quality, checks in cases:
        made = tuple(o.OrchestratorCheck(name, lambda change, i=issues, w=weigh: o.CheckFindings(list(i), list(w)))
                     for name, issues, weigh in checks)
        out.append({"quality": quality,
                    "checks": [{"name": n, "issues": i, "to_weigh": w} for n, i, w in checks],
                    "out": o.judge_run(quality, made, t.run_change(""))})
    return out


class ServedOnly:
    """A World for a scenario to script, which keeps only what its Jenkins builds archive and what its PyPI knows."""

    def __init__(self):
        self.qualities: list[str | None] = []
        self.pypi: dict[str, dict] = {}

    def jenkins_build(self, result="SUCCESS", quality="GATE: OK\n", **kw):
        self.qualities.append(quality)

    def pypi_project(self, name, answer):
        self.pypi[name] = answer

    def __getattr__(self, name):
        return lambda *a, **kw: ""


def served(name: str) -> ServedOnly:
    """What scenario name's Jenkins and PyPI serve; the stub's answers to anything else may end it early, after the
    scenario scripted them."""
    world = ServedOnly()
    with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
        scenarios.SCENARIOS[name](world)
    return world


def commit_files(repository: dict) -> dict[str, dict[str, str]]:
    """The files of each commit of a recorded repository, by sha, each built from its parent's by its changes."""
    commits = {c["sha"]: c for c in repository["commits"]}
    out: dict[str, dict[str, str]] = {}

    def files(sha: str) -> dict[str, str]:
        if sha not in out:
            c = commits[sha]
            tree = dict(files(c["parents"][0])) if c["parents"] else {}
            for change in c["changes"]:
                status, *paths = change.split("\t")
                if status.startswith(("D", "R")):
                    tree.pop(paths[0], None)
            tree.update(c["files"])
            out[sha] = tree
        return out[sha]

    for sha in commits:
        files(sha)
    return out


def git(cwd: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def diffed(base: dict[str, str], new: dict[str, str]) -> tuple[str, list[list]]:
    """The Python engine's change_diff, as text, and changed_line_counts of base's files becoming new's, on real
    git."""
    with tempfile.TemporaryDirectory() as tmp:
        git(tmp, "init", "-q")
        shas = []
        for tree in (base, new):
            for path in git(tmp, "ls-files").splitlines():
                os.remove(os.path.join(tmp, path))
            for path, text in tree.items():
                os.makedirs(os.path.dirname(os.path.join(tmp, path)) or tmp, exist_ok=True)
                with open(os.path.join(tmp, path), "w", encoding="utf-8", newline="") as f:
                    f.write(text)
            git(tmp, "add", "-A")
            git(tmp, "-c", "user.name=o", "-c", "user.email=o@x", "commit", "-q", "--allow-empty", "-m", "c")
            shas.append(git(tmp, "rev-parse", "HEAD").strip())
        host = o.Host()
        diff = os.fsdecode(host.change_diff(tmp, shas[0], shas[1]))
        counts = [[f.base_path, f.path, f.added, f.removed] for f in host.changed_line_counts(tmp, shas[0], shas[1])]
    return diff, counts


def recorded_cases() -> list[dict]:
    """judge_run of each quality file a parity scenario's run recorded from a build's quality.md."""
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "tests", "parity", "recorded", "*.json"))):
        with open(path, encoding="utf-8") as f:
            recording = json.load(f)
        name = os.path.splitext(os.path.basename(path))[0]
        quality_files = {p: text for p, text in recording["run_files"].items()
                         if os.path.basename(p).startswith("quality-") and "\nJenkins build #" in text
                         and " judged snapshot `" in text}
        if not quality_files:
            continue
        world = served(name)
        files = commit_files(recording["repositories"]["project"])
        for file, text in sorted(quality_files.items()):
            run_dir = os.path.dirname(file)
            state = json.loads(recording["run_files"][f"{run_dir}/state.json"])
            spec = recording["run_files"].get(f"{run_dir}/spec.md") or state["task"]
            lines = text.split("\n")
            at = next(i for i, x in enumerate(lines) if x.strip())
            line = lines[at + 2]
            judged = "\n".join([*lines[:at + 1], *lines[at + 3:]])
            assert o._under_gate_line(judged, line) == text, file
            number = int(line.split("#", 1)[1].split(" ", 1)[0])
            quality = world.qualities[number - 1]
            branch = recording["repositories"]["project"]["refs"][f"refs/heads/{state['branch']}"]
            base_files, new_files = files[state["base"]], files[branch]
            diff, counts = diffed(base_files, new_files)
            pipeline = o.saved_pipeline(state)
            pypi = t.FakePyPI(**world.pypi)
            change = o.RunChange(spec, "base", "new", {p: set(ls) for p, ls in o.added_lines(diff).items()},
                                 [o.ChangedFile(*c) for c in counts],
                                 lambda commit, p: {"base": base_files, "new": new_files}[commit].get(p), NOW,
                                 lambda: True)
            result = o.judge_run(quality, o.orchestrator_checks(pipeline, o.PyPI(urlopen=pypi)), change)
            assert result == judged, f"{name} {file}: judge_run does not give the recorded file:\n{result}\n---\n{judged}"
            out.append({"from": f"{name} {file}", "quality": quality, "spec": spec, "base_files": base_files,
                        "new_files": new_files, "diff": diff, "counts": counts, "max_files": pipeline.max_files,
                        "max_lines": pipeline.max_lines, "pypi": world.pypi, "out": judged})
    return out


# Manifests python_dependencies reads: (pyproject.toml, uv.lock), None for a file that is not there.
MANIFESTS = [
    (t.pyproject_with("limits>=3", "Foo.Bar[extra] >=1; python_version>'3'", "baz @ https://example.com/baz.whl"),
     None),
    ('[project]\nname = "My_Proj"\ndependencies = ["x"]\n[project.optional-dependencies]\nall = ["my-proj[a]"]\n'
     '[dependency-groups]\ndev = ["pytest", {include-group = "lint"}]\nlint = ["ruff==0.5"]\n', None),
    ('[project]\nname = "p"\ndependencies = ["a", "b", "c", "d", "e", "f", "g", "h"]\n'
     '[[tool.uv.index]]\nname = "corp"\nurl = "https://corp.example/simple/"\n'
     '[[tool.uv.index]]\nname = "pypi"\nurl = "https://pypi.org/simple/"\n'
     '[[tool.uv.index]]\nname = 3\n'
     '[tool.uv.sources]\na = { index = "corp" }\nb = { index = "pypi" }\nc = { index = "nameless" }\n'
     'd = [{ git = "https://g/d", marker = "x" }, { path = "y" }]\ne = { path = 5 }\n'
     'f = { mirror = 1.5, n = [true, "é", -0.0, inf] }\ng = { workspace = true }\n'
     'h = { index = 3 }\n', None),
    ('[tool.uv.sources]\nf = { at = 1979-05-27T07:32:00Z }\n', None),
    ('[project]\ndependencies = ["a"]\n[tool.uv.sources]\na = { index = ["x"] }\n', None),
    ('[[tool.uv.index]]\nname = ["x"]\n[tool.uv.sources]\na = { path = "p" }\n', None),
    ('[project]\ndependencies = ["a", "b"]\n[[tool.uv.index]]\nname = 1\nurl = "https://one/simple"\n'
     '[tool.uv.sources]\na = { index = true }\nb = { index = 1.0 }\n', None),
    ('[project]\nname = "p"\ndependencies = ["a"]\n[tool.uv.sources]\na = { mirror = { x = 1 } }\n', None),
    ('[project]\nname = "p"\ndependencies = ["a"]\n[[tool.uv.index]]\nurl = "https://m/simple"\ndefault = true\n',
     'version = 1\n[[package]]\nname = "a"\nsource = { registry = "https://pypi.org/simple" }\n'
     '[[package]]\nname = "B_c"\nsource = { git = "https://g/b?rev=1#abc" }\n'
     '[[package]]\nname = "d"\nsource = { directory = "../d" }\n'
     '[[package]]\nname = "p"\nsource = { editable = "." }\n'
     '[[package]]\nname = "w"\nsource = { virtual = "w" }\n'
     '[[package]]\nname = "u"\nsource = { url = "https://u/u.whl" }\n'
     '[[package]]\nname = "o"\nsource = { other = 1979-05-27 }\n'
     '[[package]]\nname = "nosource"\n'),
    ('[project]\nname = "p"\ndependencies = ["a"]\n', 'version = 1\n[[package]]\nname = "a"\nsource = { x = 07:32:00 }\n'),
    ("[project\n", "[[package\n"),
    ('[project]\ndependencies = "x"\n', '[[package]]\nversion = 1\n'),
    ('[project]\ndependencies = ["=x"]\n', 'package = 5\n'),
    ('[project]\ndependencies = [5]\n', None),
    ('[project]\ndependencies = [1979-05-27]\n', None),
    ('[project]\nname = 5\ndependencies = ["x"]\n', None),
    ('project = 5\n', None),
    ('[project]\noptional-dependencies = 5\n', None),
    ('[project]\n[project.optional-dependencies]\nx = "y"\n', None),
    ('dependency-groups = []\n', None),
    ('tool = 1\n', None),
    ('[tool]\nuv = []\n', None),
    ('[tool.uv]\nindex = 5\n', None),
    ('[tool.uv]\nindex = [5]\n', None),
    ('[tool.uv]\nsources = 5\n', None),
    ('[tool.uv.sources]\nx = 5\n', None),
    ('[tool.uv.sources]\nx = []\n', None),
    ('[tool.uv.sources]\nx = { url = "u", at = 07:32:00.5 }\n[project]\ndependencies = ["x"]\n', None),
    ('[project]\ndependencies = ["  x  [a, b]  @  https://x/x.whl  ; python_version > \'3\'", "y [z", "w@", "v.-"]\n',
     None),
    (None, None),
]


def source(s: "o.PackageSource | None"):
    return None if s is None else [s.kind, s.where]


def dependency_cases() -> list[dict]:
    """python_dependencies of each of MANIFESTS."""
    out = []
    for pyproject, lock in MANIFESTS:
        files = {"pyproject.toml": pyproject, "uv.lock": lock}
        deps = o.python_dependencies(lambda p: files.get(p))
        out.append({"pyproject": pyproject, "lock": lock,
                    "direct": [[k, source(s)] for k, s in deps.direct.items()],
                    "locked": [[k, source(s)] for k, s in deps.locked.items()],
                    "unreadable": [[p, why] for p, why in deps.unreadable.items()]})
    return out


REQUIREMENTS = ["limits>=3", "Foo.Bar[extra] >=1", "baz @ https://x/baz.whl", " x", "x.", "x-._y", "=x", "",
                "a[b", "a [b] @ u", "a@", "a @  ", "a　@　u v", "é", "a[b]c@d", "a@b@c"]
SPECS = ["Use `Requests`. Add pytest_randomly, and ruff>=0.5 to the dev group.", "", "zope.interface and zope-",
         "naïve x_y.Z... -a 9"]
LINE_NAMING = [
    ('dependencies = [\n    "zope.interface>=5",\n    "zope",\n]', [2, 3], "zope"),
    ('dependencies = [\n    "zope.interface>=5",\n    "zope",\n]', [2, 3], "zope-interface"),
    ('x = ["Pytest_Randomly", "pytest-randomly"]', [1], "pytest-randomly"),
    ('x = ["mypytest", "pytest-x", "pytest"]', [1], "pytest"),
    ('x = ["a"]', [2, 1], "a"),
    ('x = ["a_"]\ny = ["a_b"]', [1, 2], "a"),
    ('é = "ab"', [1], "ab"),
]
TOML_VALUES = ['"text"', "'it\"s'", "\"it's\"", '"tab\\t\\u00e9"', "5", "-0", "2.5", "1e300", "inf", "-inf", "nan", "-0.0",
               "true", "false", "[1, 'a', [true]]", "[]", "{ k = 1, 'q\"' = \"a\" }", "{}",
               "1979-05-27T07:32:00Z", "1979-05-27T07:32:00.999999+01:30", "1979-05-27T07:32:05-07:00",
               "1979-05-27T00:00:00", "1979-05-27T07:32:00.5", "1979-05-27", "07:32:00", "07:32:05.25", "00:00:00"]


def toml_value(literal: str) -> dict:
    value = tomllib.loads(f"v = {literal}")["v"]
    try:
        dumped = json.dumps(value)
    except TypeError as e:
        dumped = {"error": str(e)}
    return {"literal": literal, "type": type(value).__name__, "str": str(value), "repr": repr(value), "json": dumped}


def requirement(text: str):
    try:
        return list(o._requirement(text))
    except ValueError as e:
        return {"error": str(e)}


def build() -> dict:
    """The oracle."""
    return {
        "judge_run": judge_run_cases(),
        "recorded": recorded_cases(),
        "dependencies": dependency_cases(),
        "requirements": [[r, requirement(r)] for r in REQUIREMENTS],
        "package_keys": [[n, o.package_key(n)] for n in ["Foo.Bar", "a-_.b", "ÉTÉ_x", "ΣΑΣ", "x--"]],
        "spec_names": [[s, sorted(o.spec_names(s))] for s in SPECS],
        "line_naming": [[text, lines, key, o._line_naming(text, set(lines), key)] for text, lines, key in LINE_NAMING],
        "toml_values": [toml_value(v) for v in TOML_VALUES],
    }


def text(oracle: dict) -> str:
    """The file's content for the oracle build() gives."""
    return json.dumps(oracle, indent=1) + "\n"


def main() -> None:
    """Write the oracle to OUT."""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text(build()))


if __name__ == "__main__":
    main()
