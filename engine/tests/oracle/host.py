"""Write engine/tests/oracle/host.json: what orchestrator.py's Host and the helpers it reads git's output and
gitleaks' configs with give for the cases the Rust engine's host tests replay: added_lines of diffs, the
`[extend] path` a gitleaks config names, the normal form of a path, the wording of a found leak, of a timed-out
command and of a shortened line, and Host's constants. Run `uv run python engine/tests/oracle/host.py` to rewrite
it."""

import json
import os
import posixpath
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
import orchestrator  # found through the path above

OUT = os.path.join(os.path.dirname(__file__), "host.json")

# TestChangedLines' diffs, then the ways a path can be quoted and a hunk header can read.
DIFFS = [
    "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -5,2 +4,0 @@\n-a\n-b\n",
    ("diff --git a/my file.py b/my file.py\nnew file mode 100644\nindex 0000000..e69de29\n"
     "diff --git a/x y.py b/x y.py\n--- a/x y.py\t\n+++ b/x y.py\t\n@@ -1 +1 @@\n-a\n+b\n"),
    ('diff --git "a/tab\\there.py" "b/tab\\there.py"\n--- "a/tab\\there.py"\n+++ "b/tab\\there.py"\n'
     "@@ -0,0 +1 @@\n+x\n"),
    "diff --git a/f.md b/f.md\n--- a/f.md\n+++ b/f.md\n@@ -1,0 +2,3 @@\n+++ b/evil.md\n+--- a/x\n+rename to y\n",
    ("diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n"
     "--- a/old.py\n+++ b/new.py\n@@ -2,0 +3 @@\n+z = 1\r\n"
     "diff --git a/uv.lock b/uv.lock\ndeleted file mode 100644\n--- a/uv.lock\n+++ /dev/null\n@@ -1 +0,0 @@\n"
     "-x\n"),
    ('diff --git "a/\\303\\251t\\303\\251.py" "b/\\303\\251t\\303\\251.py"\n--- /dev/null\n'
     '+++ "b/\\303\\251t\\303\\251.py"\n@@ -0,0 +1,2 @@\n+a\n+b\n'),
    ('diff --git "a/q\\"uote\\\\back\\a\\b\\f\\n\\r\\v\\z" "b/q\\"uote\\\\back\\a\\b\\f\\n\\r\\v\\z"\n'
     '+++ "b/q\\"uote\\\\back\\a\\b\\f\\n\\r\\v\\z"\n@@ -1 +7 @@\n+x\n'),
    'diff --git "a/x" b/x\n+++ "b/end\\"\n@@ -1 +1 @@\n+y\n',
    "diff --git a/é b/é\n--- a/é\n+++ b/é\n@@ -1 +1 @@\n+ü \x0c\n",
    "diff --git a/f b/f\n@@ -x +1 @@\n+a\n@@ -1,y +2 @@\n+b\n@@ -1 2 @@\n+c\n@@ +4 @@\n+d\n",
    "+orphan\ndiff --git a/one b/two\n@@ -1 +1 @@\n+none\n--- a/f\n",
    "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +3 @@\n+a\ndiff --git a/f b/f\n@@ -9 +9 @@\n+b\n",
    "diff --git \n",
    "",
]

# gitleaks configs, as _gitleaks_extended_file reads them.
CONFIGS = [
    "", "title = 'x'\n", "[extend]\nuseDefault = true\n", "[extend]\npath = 'rules/extra.toml'\n",
    "[Extend]\nPath = 'rules/two.toml'\n", "[extend]\npath = ''\n", "[extend]\npath = '/etc/x.toml'\n",
    "[extend]\npath = 'rules/../../x.toml'\n", "[extend]\npath = '..'\n", "[extend]\npath = 'rules/..'\n",
    "[extend]\npath = '.'\n", '[extend]\npath = "a\\u0000.toml"\n', "[extend]\npath = 1\n", "extend = 1\n",
    "[extend]\npath = 'a.toml'\nPath = 'b.toml'\n", "[extend]\n[EXTEND]\n", "[extend\n", "[extend]\npath = 'tmp/../one.toml'\n",
    "[extend]\npath = './x.toml'\n", "[extend]\npath = 'a//b/./c.toml'\n",
]

# Paths, as _write_base_rules takes an extended file's to its normal form with posixpath.normpath.
PATHS = ["", ".", "..", "a", "a/", "a//b", "./a", "a/./b", "a/../b", "../a", "a/../../b", "../../a/..", "/a/../..",
         "//a", "///a/b/", "/..", "a/b/../../..", "tmp/../one.toml", "rules/..", "a/.."]

LEAKS = [orchestrator.Leak("github-pat", "cfg.py", 1, "c0ffee" * 6 + "c0ff", "c0ffee:cfg.py:github-pat:1"),
         orchestrator.Leak("gitleaks-allow", "a b.py", 12, "ab" * 20, "x:a b.py:gitleaks-allow:12")]


def outcome(call):
    """{"ok": what call returns}, or {"error": the message of the ValueError it raises}."""
    try:
        return {"ok": call()}
    except ValueError as e:
        return {"error": str(e)}


def build() -> dict:
    """The oracle: each kind of call with its cases and Python's answers."""
    return {
        "added_lines": [[d, orchestrator.added_lines(d)] for d in DIFFS],
        "extended_file": [[c, outcome(lambda c=c: orchestrator._gitleaks_extended_file(c))] for c in CONFIGS],
        "leaks": [[str(leak) for leak in LEAKS], str(orchestrator.LeaksFound(LEAKS, "/r/leaks.json")),
                  str(orchestrator.LeaksFound(LEAKS[:1], "/r/leaks.json"))],
        "timeout": str(subprocess.TimeoutExpired(["sh", "-c", "it's", "_", "é\n"], 60)),
        "shortened": [[t, orchestrator._shortened(t)] for t in ["", "x" * 160, "x" * 161, "é" * 200]],
        "normpath": [[p, posixpath.normpath(p)] for p in PATHS],
        "base_rules_refused": str(orchestrator._base_rules_refused("ab" * 20, ".gitleaks.toml is a symbolic link")),
        "constants": {name: getattr(orchestrator, name) for name in [
            "MISSING_FILE_STATUS", "GH_UNUSABLE_STATUS", "IN_DIR", "SSH_CONTROL_PERSIST_SECONDS",
            "SSH_ALIVE_INTERVAL_SECONDS", "SSH_ALIVE_COUNT", "SESSION_LOGS"]},
    }


def text(oracle: dict) -> str:
    """The file's content for the oracle build() gives."""
    return json.dumps(oracle, indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    """Write the oracle to OUT."""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text(build()))


if __name__ == "__main__":
    main()
