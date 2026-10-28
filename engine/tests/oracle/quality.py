"""Write engine/tests/oracle/quality.json: quality files and what the Python engine reads in each (parse_gate and
gate_failures), and what its quality-file writers write (leak_report, build_failed_report, one_line,
finding_fingerprint), for the Rust engine's tests to compare with.

The quality files: gate.py's golden ones (tests/golden/), the ones the parity scenarios' Jenkins served
(tests/parity/recorded/), and the ones tests/test_orchestrator.py's TestQualityFileContract and TestGateFailures write
with gate.py's quality_report and orchestrator.py's judge_run. Run `uv run python engine/tests/oracle/quality.py` to
rewrite it."""

import dataclasses
import glob
import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import test_orchestrator as t  # found through the path above

import gate  # found through the path above
import orchestrator as o  # found through the path above

OUT = os.path.join(os.path.dirname(__file__), "quality.json")


def contract_files() -> list[tuple[str, str]]:
    """TestQualityFileContract's quality files: every section the gate writes, and judge_run's amendments."""
    contract = t.TestQualityFileContract()
    out = []
    for word, n in ((gate.GATE_OK, 0), (gate.GATE_ERROR, 3), (gate.GATE_ERROR, gate.QUALITY_ISSUE_LIMIT + 2)):
        out.append((f"full_report({word}, {n})", contract.full_report(word, n)[0]))
    text = contract.full_report(gate.GATE_OK, 0)[0]
    found = contract.finding("pyproject.toml", 5, "not-in-spec", "names a package at 7")
    weigh = contract.finding(None, None, "over-budget", severity="warning")
    check = o.OrchestratorCheck("new-dependencies", lambda change: o.CheckFindings([found], [weigh]))
    out.append(("judge_run amended", o.judge_run(text, (check,), t.run_change(""))))
    return out


def gate_failures_files() -> list[tuple[str, str]]:
    """TestGateFailures's quality files."""
    out = []
    for percent in ("50.0", "60.0"):
        out.append((f"coverage {percent}", t.quality_report("ERROR", "judged.",
                                                            conditions=[f"Coverage of new code is {percent}%."])))
    issue = {"path": "a.py", "line": 3, "severity": "error", "rule": "ruff F401", "message": "`os` unused"}
    whole = {"path": "b.py", "line": None, "severity": "error", "rule": "mypy", "message": "bad: 2 errors"}
    weigh = {"path": "a.py", "line": 4, "severity": "warning", "rule": "flaky-tests failed", "message": "x"}
    quality = t.quality_report("ERROR", "judged.", conditions=["1 tests fail; each is listed below."],
                               issues=[issue, whole], to_weigh=[weigh],
                               tests=[("tests.test_a.T.test_x", "AssertionError: 1 != 2")])
    checked = {"path": None, "line": None, "severity": "error", "rule": "", "message": "too big: 900 lines"}
    quality = o.judge_run(quality, (
        o.OrchestratorCheck("diff-budget", lambda c: o.CheckFindings([checked], [weigh])),), SimpleNamespace())
    out.append(("findings, tests and findings to weigh", quality))
    out.append(("ok", t.quality_report("OK", "judged.")))
    return out


LEAKS = [
    t.LEAK,
    dataclasses.replace(t.LEAK, line=9, commit="snap2", fingerprint="snap2:limiter.py:github-pat:9"),
    dataclasses.replace(t.LEAK, rule="aws-access-token", file="dir/a\nb.py"),
    dataclasses.replace(t.LEAK, commit="snap1"),
]


def leak_reports() -> list[dict]:
    """leak_report of some lists of leaks."""
    cases = [[LEAKS[0]], [LEAKS[3], LEAKS[1]], [LEAKS[1], LEAKS[0], LEAKS[2]], []]
    return [{"leaks": [dataclasses.asdict(leak) for leak in leaks], "intro": "found.",
             "text": o.leak_report(leaks, "found.")} for leaks in cases]


def build_failed() -> list[dict]:
    """build_failed_report of TestBuildFailedReport's cases and a few more."""
    cases = [("intro", None, "last\nlines"), ("intro", [("t.T.test_x", "AssertionError\ntraceback")], ""),
             ("intro", [], ""), ("intro", [(f"t.T.test_{k}", "") for k in range(o.QUALITY_FINDINGS_LIMIT + 3)], ""),
             ("two\nlines", [("a\nb", "  \n\x85 detail more"), ("c", " \t")], "x")]
    return [{"intro": intro, "tests": tests, "console": console, "text": o.build_failed_report(intro, tests, console)}
            for intro, tests, console in cases]


def quality_files() -> list[dict]:
    """Each quality file, with what parse_gate and gate_failures read in it."""
    files = []
    for path in sorted(glob.glob(os.path.join(ROOT, "tests", "golden", "*.txt"))):
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        if "GATE:" in text:
            files.append((os.path.relpath(path, ROOT), text))
    for path in sorted(glob.glob(os.path.join(ROOT, "tests", "parity", "recorded", "*.json"))):
        with open(path, encoding="utf-8") as f:
            run_files = json.load(f)["run_files"]
        files += [(f"{os.path.basename(path)} {name}", text) for name, text in run_files.items()
                  if os.path.basename(name).startswith("quality-")]
    files += contract_files() + gate_failures_files()
    files += [(f"leak_report {k}", case["text"]) for k, case in enumerate(leak_reports())]
    files += [(f"build_failed_report {k}", case["text"]) for k, case in enumerate(build_failed())]
    out, seen = [], set()
    for where, text in files:
        if text in seen:
            continue
        seen.add(text)
        failures = o.gate_failures(text)
        out.append({"from": where, "text": text, "gate": o.parse_gate(text), "conditions": failures.conditions,
                    "fingerprints": failures.fingerprints})
    return out


def build() -> dict:
    """The oracle."""
    return {
        "files": quality_files(),
        "leak_reports": leak_reports(),
        "build_failed": build_failed(),
        "one_line": [[s, o.one_line(s)] for s in ["plain", "a\nb\rc\x0bd\x0ce\x1cf\x1dg\x1eh\x85i j k",
                                                  "tab\tstays", "é😀"]],
        "fingerprints": [[path, rule, message, o.finding_fingerprint(path, rule, message)] for path, rule, message in [
            ("a.py", "ruff C901", "too complex (16 > 15)"), (None, "x", "y"), ("é/ü.py", "r", "٣ and 3 and ²"),
            ("", "", ""), ("a\x00b", "rule", "m")]],
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
