"""The quality gate, gate.py: each of its checks, the quality file it writes, and the program over a real git
repository.

The tests that read what the gate writes with the orchestrator's own reader, or compare the two modules' copies of
a piece of the quality file's format, are in tests/test_orchestrator.py and tests/test_properties.py.
"""

import ast
import contextlib
import io
import json
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
import threading
import time
import tomllib
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from coverage import CoverageData
from gitrepo import RepositoryTest
from golden import assert_golden

import gate
from gate import changed_lines, quality_report


class TestChangedLines(unittest.TestCase):
    def test_added_modified_and_deleted_files(self):
        diff = ("diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n"
                "+++ b/new.py\n@@ -0,0 +1,3 @@\n+a\n+b\n+c\n"
                "diff --git a/mod.py b/mod.py\nindex 1..2 100644\n--- a/mod.py\n+++ b/mod.py\n"
                "@@ -3 +3 @@ def f():\n-x\n+y\n@@ -10,2 +10,0 @@\n-p\n-q\n@@ -20,0 +18,2 @@\n+r\n+s\n"
                "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\nindex 1..0\n--- a/gone.py\n+++ /dev/null\n"
                "@@ -1 +0,0 @@\n-z\n")
        self.assertEqual(changed_lines(diff), {"new.py": {1, 2, 3}, "mod.py": {3, 18, 19}})

    def test_a_hunk_header_it_cannot_read_adds_no_lines(self):
        diff = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ garbled @@\n+x\n"
        self.assertEqual(changed_lines(diff), {"m.py": set()})

    def test_renames(self):
        diff = ("diff --git a/old name.py b/new name.py\nsimilarity index 100%\nrename from old name.py\n"
                "rename to new name.py\n"
                "diff --git a/a.py b/b.py\nsimilarity index 80%\nrename from a.py\nrename to b.py\n"
                "--- a/a.py\n+++ b/b.py\n@@ -4,0 +5 @@\n+added\n")
        self.assertEqual(changed_lines(diff), {"new name.py": set(), "b.py": {5}})

    def test_a_file_that_only_loses_lines_is_touched(self):
        diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -5,2 +4,0 @@\n-a\n-b\n"
        self.assertEqual(changed_lines(diff), {"f.py": set()})

    def test_empty_new_file_and_names_with_spaces(self):
        diff = ("diff --git a/my file.py b/my file.py\nnew file mode 100644\nindex 0000000..e69de29\n"
                "diff --git a/x y.py b/x y.py\n--- a/x y.py\t\n+++ b/x y.py\t\n@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual(changed_lines(diff), {"my file.py": set(), "x y.py": {1}})

    def test_quoted_names(self):
        diff = ('diff --git "a/tab\\there.py" "b/tab\\there.py"\n--- "a/tab\\there.py"\n+++ "b/tab\\there.py"\n'
                "@@ -0,0 +1 @@\n+x\n")
        self.assertEqual(changed_lines(diff), {"tab\there.py": {1}})

    def test_hunk_lines_that_look_like_headers(self):
        diff = ("diff --git a/f.md b/f.md\n--- a/f.md\n+++ b/f.md\n@@ -1,0 +2,3 @@\n"
                "+++ b/evil.md\n+--- a/x\n+rename to y\n")
        self.assertEqual(changed_lines(diff), {"f.md": {2, 3, 4}})

    def test_only_a_newline_ends_a_line(self):
        # Each of these ends a line for str.splitlines, which would read the rest of the added line as a header.
        for sep in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
            with self.subTest(sep=repr(sep)):
                diff = (f"diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -3,0 +4,3 @@\n"
                        f"+x = 1{sep}diff --git a/g.py b/g.py{sep}@@ -5,0 +6,2 @@\n+y\n+z\n")
                self.assertEqual(changed_lines(diff), {"f.py": {4, 5, 6}})

    def test_build_config_edits(self):
        diff = ("diff --git a/Jenkinsfile b/Jenkinsfile\n--- a/Jenkinsfile\n+++ b/Jenkinsfile\n@@ -1 +1 @@\n-a\n+b\n"
                "diff --git a/uv.lock b/uv.lock\ndeleted file mode 100644\n"
                "--- a/uv.lock\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        self.assertEqual(gate.edited_config(diff, changed_lines(diff)), ["Jenkinsfile", "uv.lock"])
        self.assertEqual(gate.edited_config("", {}), [])

    def test_clippy_config_edits_anywhere_in_the_tree(self):
        diff = ("diff --git a/ui/Cargo.toml b/ui/Cargo.toml\n--- a/ui/Cargo.toml\n+++ b/ui/Cargo.toml\n@@ -1 +1 @@\n"
                "-a\n+b\ndiff --git a/ui/.cargo/config.toml b/ui/.cargo/config.toml\ndeleted file mode 100644\n"
                "--- a/ui/.cargo/config.toml\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
                "diff --git a/clippy.toml b/clippy.toml\nnew file mode 100644\n--- /dev/null\n+++ b/clippy.toml\n"
                "@@ -0,0 +1 @@\n+y\ndiff --git a/docs/config.toml b/docs/config.toml\n--- a/docs/config.toml\n"
                "+++ b/docs/config.toml\n@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual(gate.clippy_config_files(diff, changed_lines(diff)),
                         ["clippy.toml", "ui/.cargo/config.toml", "ui/Cargo.toml"])
        self.assertEqual(gate.edited_config(diff, changed_lines(diff)), [])


class TestQualityReport(unittest.TestCase):
    def issues(self, n):
        return [{"path": "f.py", "line": i, "severity": "HIGH", "rule": "r", "message": f"m{i}"} for i in range(1, n + 1)]

    def test_numbers_at_most_fifty_issues(self):
        text = quality_report("ERROR", "intro", issues=self.issues(60), elsewhere=10, tests=[])
        self.assertTrue(text.startswith("GATE: ERROR\n\nintro\n"))
        self.assertIn("50. `f.py:50` HIGH r: m50\n", text)
        self.assertNotIn("51. ", text)
        self.assertIn("10 more issues on changed lines are not listed", text)
        self.assertIn("10 other open issues in the project", text)

    def finding(self, path, line, rule="flaky test_x"):
        return {"path": path, "line": line, "severity": "warning", "rule": rule, "message": "m"}

    def test_findings_to_weigh_alone_leave_the_gate_ok(self):
        text = quality_report("OK", "intro", to_weigh=[self.finding("t.py", 3), self.finding("t.py", None),
                                                        self.finding(None, None, "mutants budget")])
        self.assertTrue(text.startswith("GATE: OK\n"))
        self.assertIn("## Issues on changed lines\n\nNone.\n", text)
        self.assertIn("## Findings to weigh\n\nThese do not fail the gate. Fix each, or say in your report why it "
                      "should stand.\n\n1. `t.py:3` warning flaky test_x: m\n"
                      "2. `t.py (the whole file)` warning flaky test_x: m\n"
                      "3. The change as a whole: warning mutants budget: m\n", text)

    def test_findings_to_weigh_are_numbered_after_the_issues(self):
        text = quality_report("ERROR", "intro", issues=self.issues(2), to_weigh=[self.finding("t.py", 9)])
        self.assertLess(text.index("2. `f.py:2` HIGH r: m2"), text.index("## Findings to weigh"))
        self.assertIn("3. `t.py:9` warning flaky test_x: m\n", text)

    def test_the_limit_takes_issues_first_then_findings_to_weigh(self):
        limit = gate.QUALITY_ISSUE_LIMIT
        text = quality_report("ERROR", "intro", issues=self.issues(limit - 2),
                              to_weigh=[self.finding("t.py", n) for n in range(1, 6)])
        self.assertIn(f"{limit}. `t.py:2` ", text)
        self.assertNotIn(f"{limit + 1}. ", text)
        self.assertIn("3 more findings to weigh are not listed.", text)
        text = quality_report("ERROR", "intro", issues=self.issues(limit + 1), to_weigh=[self.finding("t.py", 1)])
        self.assertIn("## Findings to weigh\n\n1 findings to weigh are not listed; answer the issues above first.\n",
                      text)
        self.assertNotIn("`t.py:1`", text)

    def test_no_findings_to_weigh_no_section(self):
        self.assertNotIn("Findings to weigh", quality_report("OK", "intro"))
        self.assertNotIn("Findings to weigh", quality_report("ERROR", "intro", issues=self.issues(1)))

    def test_tests_and_coverage(self):
        # The console's end is the engine's to write (TestBuildFailedReport): the gate's file never has one.
        text = quality_report("OK", "intro", tests=[("t.T.test_x", "AssertionError\ntraceback")],
                              coverage="62.5% of 40 new lines to cover.")
        self.assertIn("- `t.T.test_x`: AssertionError\n", text)
        self.assertIn("## Coverage on new code\n\n62.5% of 40 new lines to cover.\n", text)
        self.assertIn("None.", text)
        self.assertNotIn("console", text)
        self.assertNotIn("Jenkins has no test report", text)
        self.assertNotIn("## Failing tests", quality_report("OK", "intro", tests=None))
        self.assertIn("## Failing tests\n\nNone.\n", quality_report("OK", "intro", tests=[]))

    def test_sections_left_out_by_default(self):
        text = quality_report("OK", "intro")
        self.assertNotIn("## Failing tests", text)
        self.assertNotIn("## Coverage on new code", text)
        self.assertNotIn("## Suppressions added", text)
        self.assertNotIn("## Failed conditions", text)

    def test_conditions_and_suppressions_are_listed(self):
        text = quality_report("ERROR", "intro", conditions=["`ruff` is not installed."],
                              suppressions=[("f.py", 3, "x = 1  # noqa: F401")])
        self.assertIn("## Failed conditions\n\n- `ruff` is not installed.\n", text)
        self.assertIn("## Suppressions added\n\n- `f.py:3` `x = 1  # noqa: F401`\n", text)

    def test_failing_tests_beyond_the_limit_are_counted(self):
        tests = [(f"t.T.test_{k}", "") for k in range(gate.QUALITY_ISSUE_LIMIT + 3)]
        text = quality_report("ERROR", "intro", tests=tests)
        self.assertIn("- `t.T.test_0`: failed\n", text)
        self.assertIn("- and 3 more.\n", text)
        self.assertNotIn(f"test_{gate.QUALITY_ISSUE_LIMIT}`", text)

    def test_build_config_edits_are_named(self):
        text = quality_report("OK", "i", tests=[], edited=["Jenkinsfile", "uv.lock"])
        self.assertIn("The change edits `Jenkinsfile`. The quality job reads it from `main`", text)
        for only, other in (("Jenkinsfile", "uv.lock"), ("uv.lock", "Jenkinsfile")):
            with self.subTest(only=only):
                text = quality_report("OK", "i", tests=[], edited=[only])
                self.assertIn(f"The change edits `{only}`", text)
                self.assertNotIn(f"The change edits `{other}`", text)

    def test_gate_config_edits_are_named(self):
        text = quality_report("OK", "i", edited=["pyproject.toml", "uv.lock"])
        self.assertIn("The change edits `pyproject.toml`, which holds the gate's rules; the change was judged by "
                      "the edited rules.", text)
        self.assertIn("The change edits `uv.lock`, which pins the versions of the linters the gate ran.", text)
        text = quality_report("OK", "i", edited=["pyproject.toml"], rules_from="`main`'s `pyproject.toml`")
        self.assertIn("The gate took its rules from `main`'s `pyproject.toml`, so the edit did not change", text)

    def test_clippy_config_edits_are_named_together(self):
        text = quality_report("OK", "i", edited=["uv.lock"], clippy_config=["clippy.toml", "ui/Cargo.toml"])
        self.assertIn("- The change edits `clippy.toml` and `ui/Cargo.toml`, which clippy takes its rules or its "
                      "toolchain from; it judged the change by the edited ones.\n", text)

    def test_files_under_mutmut_s_state_are_named_apart_from_build_config(self):
        text = quality_report("OK", "i", mutmut_state=["mutants/calc.py", "mutants/calc.py.meta"])
        self.assertIn("## Build configuration\n\n- The change adds or edits `mutants/calc.py` and "
                      "`mutants/calc.py.meta` under `mutants/`, where mutmut keeps its state. The gate's checks leave "
                      "them out: none selects a test or a function in them or counts them as a change to the code, "
                      "and the surviving-mutants check runs mutmut without them. The linters read them as they read "
                      "any tracked file.\n", text)
        self.assertNotIn("The change edits", text)

    # The whole file, for each gate word and each way quality_report words a section, held to its golden file
    # (tests/golden.py), so that a change to its wording fails here and is reviewed as the golden file's diff. The
    # intro, the conditions and the coverage sentence are judge_change's: TestGate's golden files pin those.

    def test_an_ok_file_with_nothing_to_answer_is_golden(self):
        assert_golden("quality_report_ok", quality_report("OK", "The intro."))

    def test_an_ok_file_with_findings_to_weigh_is_golden(self):
        text = quality_report("OK", "The intro.", tests=[], coverage="85.0% of 20 new lines to cover; 3 lines are "
                              "not covered.", to_weigh=[self.finding("t.py", 3), self.finding("t.py", None),
                                                        self.finding(None, None, "mutants budget")],
                              edited=["pyproject.toml"], rules_from="`main`'s `pyproject.toml`")
        assert_golden("quality_report_ok_to_weigh", text)

    def test_an_error_file_with_every_section_is_golden(self):
        text = quality_report(
            "ERROR", "The intro.",
            conditions=["2 findings on lines the change added; each is listed below.", "`ruff` is not installed."],
            issues=self.issues(2), to_weigh=[self.finding("t.py", 9)], elsewhere=3,
            tests=[("t.T.test_x", "AssertionError: 3 != 2\ntraceback"), ("t.T.test_y", "")],
            coverage="50.0% of 20 new lines to cover; 10 lines are not covered.",
            suppressions=[("f.py", 3, "x = 1  # noqa: F401"), ("g\rh.py", 1, "# noqa")],
            edited=["Jenkinsfile", "pyproject.toml", "uv.lock"],
            clippy_config=[f"c{k}/Cargo.toml" for k in range(7)], mutmut_state=["mutants/calc.py"])
        assert_golden("quality_report_error", text)

    def test_an_error_file_whose_findings_to_weigh_are_cut_is_golden(self):
        text = quality_report("ERROR", "The intro.", issues=self.issues(gate.QUALITY_ISSUE_LIMIT - 1),
                              to_weigh=[self.finding("t.py", n) for n in range(1, 4)],
                              clippy_config=["clippy.toml", "ui/Cargo.toml"])
        assert_golden("quality_report_error_to_weigh_cut", text)

    def test_an_error_file_over_the_limit_is_golden(self):
        limit = gate.QUALITY_ISSUE_LIMIT
        text = quality_report("ERROR", "The intro.", issues=self.issues(limit + 2),
                              to_weigh=[self.finding("t.py", 1), self.finding("t.py", 2)],
                              tests=[(f"t.T.test_{k}", "") for k in range(limit + 1)])
        assert_golden("quality_report_error_over_limit", text)


class TestGate(RepositoryTest):
    """`gate.py` over a real git repository, with stub linters on PATH that print what each one does."""

    BASE_FILES = {"pyproject.toml": "[tool.ruff]\n", "f.py": "".join(f"line{n} = {n}\n" for n in range(1, 4))}

    def setUp(self):
        super().setUp()
        self.bin, self.stubs = (os.path.join(self.outside, d) for d in ("bin", "stubs"))
        for d in (self.bin, self.stubs):
            os.mkdir(d)
        for tool in ("ruff", "mypy", "pymarkdown", "shellcheck", "actionlint"):
            self.stub(tool)
        env = patch.dict(os.environ, {"PATH": f"{self.bin}:{os.path.dirname(shutil.which('git'))}",
                                      "STUB_DIR": self.stubs})
        env.start()
        self.addCleanup(env.stop)

    def stub(self, tool):
        """A stub tool that records its arguments and prints $STUB_DIR/<tool>.out and .err, exiting with .status."""
        path = os.path.join(self.bin, tool)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nd="$STUB_DIR/{tool}"\nprintf "%s\\n" "$@" > "$d.argv"\n'
                    f'[ -f "$d.out" ] && cat "$d.out"\n[ -f "$d.err" ] && cat "$d.err" >&2\n'
                    f'exit "$(cat "$d.status" 2>/dev/null || echo 0)"\n')
        os.chmod(path, 0o700)

    def says(self, tool, out="", status=1, err=""):
        for ext, text in (("out", out), ("err", err), ("status", str(status))):
            with open(os.path.join(self.stubs, f"{tool}.{ext}"), "w") as f:
                f.write(text)

    def argv(self, tool):
        with open(os.path.join(self.stubs, f"{tool}.argv")) as f:
            return f.read().splitlines()

    def gate(self, *args, base="HEAD"):
        """The exit status, standard output and standard error of `gate.py --base BASE ARGS` in the
        repo."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = gate.main(["--base", base, *args])
        return status, out.getvalue(), err.getvalue()

    def add_lines(self, n, start=4):
        with open(os.path.join(self.repo, "f.py"), "a") as f:
            f.write("".join(f"new{k} = {k}\n" for k in range(start, start + n)))

    def test_ruff_reads_every_file_whatever_syntax_the_environment_gives_git_s_pathspecs(self):
        self.put("sub/g.py", "g = 1\n")
        self.git("add", "sub/g.py")
        self.add_lines(1)
        for name in ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS"):
            with self.subTest(name), patch.dict("os.environ", {name: "1"}):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(self.stubs, "ruff.argv"))
                self.gate()
                self.assertTrue({"f.py", "sub/g.py"} <= set(self.argv("ruff")), self.argv("ruff"))

    def test_a_clean_change_passes(self):
        self.add_lines(1)
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("GATE: OK\n"))
        self.assertNotIn("## Failed conditions", out)

    def test_only_findings_on_added_lines_count(self):
        self.add_lines(1)
        self.says("ruff", "f.py:2:1: F401 `os` imported but unused\n"
                          "f.py:4:7: B023 Function definition does not bind loop variable `x`\nFound 2 errors.\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertTrue(out.startswith("GATE: ERROR\n"))
        self.assertIn("- 1 findings on lines the change added; each is listed below.\n", out)
        self.assertIn("1. `f.py:4` error ruff B023: Function definition does not bind loop variable `x`\n", out)
        self.assertNotIn("f.py:2", out)
        self.assertIn("1 other open issues in the project", out)

    def test_a_finding_on_an_old_line_alone_passes(self):
        self.add_lines(1)
        self.says("ruff", "f.py:2:1: F401 `os` imported but unused\n")
        self.assertEqual(self.gate()[0], 0)

    def test_a_missing_tool_fails_naming_it(self):
        self.put("run.sh", "#!/bin/sh\n")
        self.git("add", "run.sh")
        os.remove(os.path.join(self.bin, "shellcheck"))
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `shellcheck` is not installed: it is not on PATH.\n", out)

    def test_only_tracked_files_are_linted(self):
        self.put("run.sh", "#!/bin/sh\n")
        os.remove(os.path.join(self.bin, "shellcheck"))
        self.assertEqual(self.gate()[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.stubs, "actionlint.argv")))

    def test_a_failing_or_unreadable_tool_fails_the_gate(self):
        self.put("README.md", "# r\n")
        self.git("add", "README.md")
        self.says("ruff", status=2, err="ruff failed\n  Cause: invalid config\n")
        self.says("pymarkdown", "WARNING:pymarkdown.main:Specified configuration file does not exist.\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `ruff` failed with exit status 2: ruff failed\n", out)
        self.assertIn("- `pymarkdown` reported findings, but none the gate could read: WARNING:pymarkdown", out)

    def test_a_line_that_is_not_utf8_counts_like_any_other(self):
        with open(os.path.join(self.repo, "f.py"), "ab") as f:
            f.write("name = 'Größe'\n".encode("latin-1"))
        self.says("ruff", "f.py:4:1: E501 Line too long (130 > 120)\n")
        status, out, _ = self.gate("--out", "quality.md")
        self.assertEqual(status, 1)
        with open(os.path.join(self.repo, "quality.md"), encoding="utf-8") as f:
            self.assertIn("1. `f.py:4` error ruff E501: Line too long (130 > 120)\n", f.read())

    def test_an_added_binary_file_is_judged(self):
        # git diffs the first as text, since it has no NUL byte, and the second as binary.
        for name, raw in (("logo.png", b"\x89PNG\r\n\x1a\n\xff\xd8\xff\xe0\n"), ("blob.bin", b"\x00\xff\xfe\n")):
            with open(os.path.join(self.repo, name), "wb") as f:
                f.write(raw)
            self.git("add", name)
        status, _, err = self.gate("--out", "quality.md")
        self.assertEqual((status, err), (0, "GATE: OK, in quality.md\n"))

    def test_a_carriage_return_in_an_added_line_does_not_end_it(self):
        with open(os.path.join(self.repo, "f.py"), "ab") as f:
            f.write(b"x = 1\rdiff --git a/g.py b/g.py\r@@ -5,0 +6,2 @@\ny = 2\nz = 3\n")
        base = self.git("rev-parse", "HEAD").strip()
        self.assertEqual(changed_lines(self.host.change_diff(self.repo, base)), {"f.py": {4, 5, 6}})

    def test_a_carriage_return_inside_an_added_python_line_fails_the_gate(self):
        # Python ends a line at the lone \r and git does not, so ruff's line 6 is git's line 5, the second added
        # line. Without the condition, the finding sits on no added line and the gate passes. A \r\n is one end.
        with open(os.path.join(self.repo, "f.py"), "ab") as f:
            f.write(b"crlf = 1\r\npass\rimport os\n")
        self.says("ruff", "f.py:6:8: F401 [*] `os` imported but unused\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `f.py:5` holds `\\r`, which a linter that reads the file ends a line at", out)
        self.assertNotIn("`f.py:4`", out)
        with self.assertRaisesRegex(gate.GateError, "cannot read gone.py, which the change edits"):
            gate.stray_line_ends(self.repo, {"gone.py": {1}}, [(gate.GATE_TOOLS[0], ["gone.py"])])

    def test_a_carriage_return_in_a_file_no_linter_reads_passes(self):
        self.put("notes.txt", "a\rb\n")
        with open(os.path.join(self.repo, "data.bin"), "wb") as f:
            f.write(b"\x01\r\x02\n")
        self.git("add", "notes.txt", "data.bin")
        self.git("commit", "--quiet", "-m", "carriage returns")
        self.put("notes.txt", "a\rb\nc\n")
        with open(os.path.join(self.repo, "data.bin"), "ab") as f:
            f.write(b"\x03\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertNotIn("`notes.txt:", out)
        self.assertNotIn("`data.bin:", out)

    def test_a_carriage_return_above_an_added_line_fails_the_gate_in_any_file_a_linter_reads(self):
        # The \r lines are the base's, and d.md is pymarkdown's: each linter's line after them is one past git's.
        self.put("f.py", "a = 1\rb = 2\n")
        self.put("d.md", "# T\n\nok\r more text\n")
        self.git("add", "d.md")
        self.git("commit", "--quiet", "-am", "carriage returns")
        self.put("f.py", "a = 1\rb = 2\nimport os\n")
        self.put("d.md", "# T\n\nok\r more text\n\n# T\n")
        self.says("ruff", "f.py:3:8: F401 [*] `os` imported but unused\n")
        self.says("pymarkdown", "d.md:6:1: MD025: Multiple top-level headings in the same document\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `f.py:1` holds `\\r`, which a linter that reads the file ends a line at", out)
        self.assertIn("- `d.md:3` holds `\\r`, which a linter that reads the file ends a line at", out)

    def test_a_line_separator_above_an_added_line_fails_the_gate_only_where_a_linter_ends_a_line_at_it(self):
        # actionlint's YAML parser ends a line at U+2028, so its line 9 is git's line 8; ruff numbers like git.
        workflow = ("on: push\njobs:\n  j:\n    runs-on: x\n    steps:\n      # c\u2028# d\n"
                    "      - run: echo hi\n")
        self.put(".github/workflows/w.yml", workflow)
        self.put("f.py", "# c\u2028# d\n")
        self.git("add", ".github")
        self.git("commit", "--quiet", "-am", "line separators")
        self.put(".github/workflows/w.yml", workflow + "      - run: echo ${{ github.nope }}\n")
        self.put("f.py", "# c\u2028# d\nx = 1\n")
        self.says("actionlint", '.github/workflows/w.yml:9:23: property "nope" is not defined [expression]\n')
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- `.github/workflows/w.yml:6` holds `\\u2028`, which a linter that reads the file ends a "
                      "line at", out)
        self.assertNotIn("`f.py:", out)

    def test_a_suppression_after_a_form_feed_is_listed_at_gits_line(self):
        self.put("f.py", "a = 1\x0c\n")
        self.git("commit", "--quiet", "-am", "form feed")
        self.put("f.py", "a = 1\x0c\nimport os  # noqa: F401\n")
        self.assertIn("- `f.py:2` `import os  # noqa: F401`", self.gate()[1])

    def test_a_symlink_to_a_directory_is_judged(self):
        self.put("pkg/m.py", "x = 1\n")
        self.git("add", "pkg")
        self.git("commit", "--quiet", "-m", "pkg")
        os.symlink("pkg", os.path.join(self.repo, "lib.py"))
        self.git("add", "lib.py")
        status, out, _ = self.gate()
        self.assertIn(status, (0, 1))
        self.assertTrue(out.startswith("GATE: "))

    def test_a_submodule_is_judged(self):
        os.mkdir(os.path.join(self.repo, "sub.py"))
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.git('rev-parse', 'HEAD').strip()},sub.py")
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("GATE: OK\n"))

    def test_a_symlink_is_read_as_git_diffs_it_its_target_name(self):
        self.put("target.py", "x = 1  # noqa: E501\n")
        self.git("add", "target.py")
        self.git("commit", "--quiet", "-m", "target")
        os.symlink("target.py", os.path.join(self.repo, "link.py"))
        self.git("add", "link.py")
        self.assertNotIn("## Suppressions added", self.gate()[1])

    def test_a_file_name_that_holds_a_line_separator_keeps_its_findings(self):
        # Each ends a line for str.splitlines, and the \r one for text mode as well; the tool's line ends at "\n".
        names = ["a\x85b.py", "a b.py", "a\x1cb.py", "a\rb.py"]
        for name in names:
            self.put(name, "x = 1\n")
            self.git("add", name)
        self.says("ruff", "".join(f"{name}:1:1: F401 `os` imported but unused\n" for name in names))
        status, _, _ = self.gate("--out", "quality.md")
        self.assertEqual(status, 1)
        with open(os.path.join(self.repo, "quality.md"), encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertIn("- 4 findings on lines the change added", text)
        for name in names:
            self.assertIn(f"`{gate.one_line(name)}:1` error ruff F401", text)

    def test_a_file_name_that_is_not_utf8_is_judged(self):
        name = os.fsdecode(b"f\xe9.py")
        self.put(name, "a = 1\n")
        self.git("add", name)
        self.git("commit", "--quiet", "-m", "latin-1 name")
        with open(os.path.join(self.repo, name), "a") as f:
            f.write("b = 2  # noqa: E501\n")
        # ruff prints U+FFFD for each byte of the name that is not UTF-8; a tool may also print the bytes themselves.
        for printed in (b"f\xe9.py", "f�.py".encode()):
            with self.subTest(printed=printed):
                with open(os.path.join(self.stubs, "ruff.out"), "wb") as f:
                    f.write(printed + b":2:1: E501 Line too long (130 > 120)\n")
                status, _, _ = self.gate("--out", "quality.md")
                self.assertEqual(status, 1)
                # Strict UTF-8: the name's undecodable byte is written as an escape.
                with open(os.path.join(self.repo, "quality.md"), encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("1. `f\\xe9.py:2` error ruff E501: Line too long (130 > 120)\n", text)
                self.assertNotIn("none the gate could read", text)
                self.assertIn("f\\xe9.py:2", text.split("## Suppressions", 1)[1])

    def test_an_unresolvable_base_writes_nothing(self):
        out = os.path.join(self.repo, "quality.md")
        status, stdout, err = self.gate("--base", "no-such-rev", "--out", out)
        self.assertEqual(status, gate.EXIT_GATE_UNJUDGED)
        self.assertIn("no-such-rev names no commit", err)
        self.assertEqual(stdout, "")
        self.assertFalse(os.path.exists(out))

    def test_outside_a_git_tree_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(gate.main(["--base", "HEAD"]), gate.EXIT_GATE_UNJUDGED)
        self.assertIn("is not in a git work tree", err.getvalue())

    def test_out_gets_the_file_and_stderr_the_gate(self):
        self.add_lines(1)
        status, stdout, err = self.gate("--out", "quality.md")
        self.assertEqual((status, stdout), (0, ""))
        self.assertEqual(err, "GATE: OK, in quality.md\n")
        with open(os.path.join(self.repo, "quality.md")) as f:
            self.assertTrue(f.read().startswith("GATE: OK\n"))

    def coverage(self, lines):
        """A Cobertura report of f.py: lines maps a line number to its hits, or to (hits, "covered/total" branches)."""
        rows = "".join(f'<line number="{n}" hits="{h[0] if isinstance(h, tuple) else h}"'
                       + (f' branch="true" condition-coverage="50% ({h[1]})"' if isinstance(h, tuple) else "")
                       + "/>" for n, h in lines.items())
        self.put("coverage.xml", f'<?xml version="1.0" ?><coverage><sources><source>{self.repo}</source></sources>'
                                 f'<packages><package name="."><classes><class name="f.py" filename="f.py"><lines>'
                                 f'{rows}</lines></class></classes></package></packages></coverage>')
        return os.path.join(self.repo, "coverage.xml")

    def test_coverage_below_the_minimum_fails_from_twenty_new_lines(self):
        self.add_lines(20)
        xml = self.coverage({n: int(n < 14) for n in range(1, 24)})
        status, out, _ = self.gate("--coverage", xml)
        self.assertEqual(status, 1)
        self.assertIn("- Coverage of new code is 50.0%; the gate wants at least 80% once a change has 20 or more", out)
        self.assertIn("50.0% of 20 new lines to cover; 10 lines are not covered.", out)

    def test_coverage_is_not_judged_on_nineteen_new_lines(self):
        self.add_lines(19)
        status, out, _ = self.gate("--coverage", self.coverage({n: 0 for n in range(1, 23)}))
        self.assertEqual(status, 0)
        self.assertIn("0.0% of 19 new lines to cover; 19 lines are not covered.", out)

    def test_coverage_counts_branch_outcomes_too(self):
        self.add_lines(2)
        _, out, _ = self.gate("--coverage", self.coverage({4: (1, "1/2"), 5: 0, 2: 1}))
        # (1 covered condition + 1 covered line) / (2 conditions + 2 lines); line 2 is not new.
        self.assertIn("50.0% of 2 new lines to cover, counting their 2 branch outcomes; 1 lines are not covered.", out)

    def test_coverage_names_a_file_it_cannot_measure_and_judges_without_it(self):
        name = os.fsdecode(b"m\xff.py")
        self.put(name, "".join(f"m{k} = {k}\n" for k in range(30)))
        self.git("add", name)
        self.add_lines(1)
        status, _, _ = self.gate("--coverage", self.coverage({4: 1}), "--out", "quality.md")
        # 30 uncovered lines would fail the gate; coverage.py has no entry for them, so only f.py's line counts.
        self.assertEqual(status, 0)
        with open(os.path.join(self.repo, "quality.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("100.0% of 1 new lines to cover; 0 lines are not covered.\n\nNot measured: `m\\xff.py`; "
                      "coverage.py records no data for a file whose name is not UTF-8.", text)

    def rust_coverage(self, lines, filename="src/lib.rs"):
        """A Cobertura report of ui/src-tauri/src/lib.rs as cargo-llvm-cov writes it, run in that Cargo workspace:
        filename relative to the workspace, which is the report's source, or absolute. lines maps a line number to
        its hits."""
        rows = "".join(f'<line branch="false" hits="{h}" number="{n}"/>' for n, h in lines.items())
        self.put("coverage-rust.xml",
                 f'<?xml version="1.0" ?><coverage><sources><source>{self.repo}/ui/src-tauri</source></sources>'
                 f'<packages><package name="src"><classes><class filename="{filename}" name="src.lib.rs"><lines>'
                 f'{rows}</lines></class></classes></package></packages></coverage>')
        return os.path.join(self.repo, "coverage-rust.xml")

    def add_rust_lines(self, n):
        """Add a tracked ui/src-tauri/src/lib.rs of n lines, which clippy, a stub here, finds nothing in: with no
        Cargo.toml tracked there is no workspace to lint."""
        self.stub("cargo")
        self.put("ui/src-tauri/src/lib.rs", "".join(f"pub const C{k}: u8 = {k};\n" for k in range(1, n + 1)))
        self.git("add", "ui/src-tauri/src/lib.rs")

    def test_coverage_reports_given_twice_are_merged_and_an_uncovered_rust_line_counts(self):
        self.add_lines(10)
        self.add_rust_lines(10)
        python = self.coverage({n: 1 for n in range(4, 14)})
        rust = self.rust_coverage({n: int(n <= 2) for n in range(1, 11)})
        status, out, _ = self.gate("--coverage", python)
        self.assertEqual(status, 0)
        self.assertIn("100.0% of 10 new lines to cover; 0 lines are not covered.", out)
        status, out, _ = self.gate("--coverage", python, "--coverage", rust)
        self.assertEqual(status, 1)
        # (10 Python lines + 2 Rust lines covered) / 20 new lines.
        self.assertIn("- Coverage of new code is 60.0%; the gate wants at least 80%", out)
        self.assertIn("60.0% of 20 new lines to cover; 8 lines are not covered.", out)

    def test_an_absolute_path_in_a_coverage_report_stands_as_it_is(self):
        self.add_rust_lines(2)
        rust = self.rust_coverage({1: 1, 2: 0}, filename=os.path.join(self.repo, "ui/src-tauri/src/lib.rs"))
        _, out, _ = self.gate("--coverage", rust)
        self.assertIn("50.0% of 2 new lines to cover; 1 lines are not covered.", out)

    def test_a_line_two_coverage_reports_list_counts_once_and_is_covered_if_either_covers_it(self):
        self.add_lines(2)
        os.rename(self.coverage({4: 1, 5: (1, "1/2")}), os.path.join(self.repo, "first.xml"))
        second = self.coverage({4: 0, 5: (0, "0/2")})
        _, out, _ = self.gate("--coverage", "first.xml", "--coverage", second)
        # (2 covered lines + 1 covered outcome) / (2 lines + 2 outcomes).
        self.assertIn("75.0% of 2 new lines to cover, counting their 2 branch outcomes; 0 lines are not covered.", out)

    def test_a_coverage_entry_it_cannot_read_on_a_line_the_change_did_not_add_is_skipped(self):
        self.add_lines(1)
        status, out, _ = self.gate("--coverage", self.coverage({2: "1.0", 4: 1}))
        self.assertEqual(status, 0)
        self.assertIn("100.0% of 1 new lines to cover; 0 lines are not covered.", out)
        _, _, err = self.gate("--coverage", self.coverage({4: "1.0"}))
        self.assertIn("has a line it cannot read", err)

    def test_an_unreadable_coverage_report_writes_nothing(self):
        self.add_lines(1)
        self.put("coverage.xml", "<coverage")
        status, stdout, err = self.gate("--coverage", "coverage.xml")
        self.assertEqual((status, stdout), (gate.EXIT_GATE_UNJUDGED, ""))
        self.assertIn("cannot read the coverage report", err)

    def junit(self, *failing):
        """A pytest --junitxml report with one passing test and each of failing as (name, message)."""
        cases = "".join(f'<testcase classname="tests.T" name="{n}"><failure message="{m}">trace</failure></testcase>'
                        for n, m in failing)
        self.put("results.xml", f'<?xml version="1.0"?><testsuites><testsuite name="pytest">'
                                f'<testcase classname="tests.T" name="test_ok"/>{cases}</testsuite></testsuites>')
        return "results.xml"

    def test_failing_tests_fail_the_gate_and_are_listed(self):
        self.add_lines(1)
        status, out, _ = self.gate("--tests", self.junit(("test_limit", "AssertionError: 3 != 2")))
        self.assertEqual(status, 1)
        self.assertIn("- 1 tests fail; each is listed below.\n", out)
        self.assertIn("## Failing tests\n\n- `tests.T.test_limit`: AssertionError: 3 != 2\n", out)

    def test_a_failing_test_in_any_of_several_test_reports_fails_the_gate(self):
        self.add_lines(1)
        python = self.junit(("test_limit", "AssertionError: 3 != 2"))
        # cargo-nextest's report names a test's binary as its classname.
        self.put("rust.xml", '<?xml version="1.0"?><testsuites name="nextest-run"><testsuite name="octoflow-ui">'
                             '<testcase name="tests::serves" classname="octoflow-ui"><failure message="panicked">'
                             'trace</failure></testcase></testsuite></testsuites>')
        status, out, _ = self.gate("--tests", "rust.xml", "--tests", python)
        self.assertEqual(status, 1)
        self.assertIn("- 2 tests fail; each is listed below.\n", out)
        self.assertIn("## Failing tests\n\n- `octoflow-ui.tests::serves`: panicked\n"
                      "- `tests.T.test_limit`: AssertionError: 3 != 2\n", out)

    def test_passing_tests_are_said_to_pass(self):
        self.add_lines(1)
        status, out, _ = self.gate("--tests", self.junit())
        self.assertEqual(status, 0)
        self.assertIn("## Failing tests\n\nNone.\n", out)

    def test_an_unreadable_test_report_writes_nothing(self):
        self.put("results.xml", "<testsuites")
        status, stdout, err = self.gate("--tests", "results.xml")
        self.assertEqual((status, stdout), (gate.EXIT_GATE_UNJUDGED, ""))
        self.assertIn("cannot read the test report", err)

    def test_added_suppressions_are_listed_not_failed(self):
        with open(os.path.join(self.repo, "f.py"), "a") as f:
            f.write("import os  # noqa: F401\n")
        self.put("run.sh", "# shellcheck disable=SC2086\n")
        self.git("add", "run.sh")
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertIn("## Suppressions added\n\n- `f.py:4` `import os  # noqa: F401`\n"
                      "- `run.sh:1` `# shellcheck disable=SC2086`\n", out)

    def checks(self, *checks):
        """Run the gate with checks in place of gate_checks's."""
        p = patch.object(gate, "gate_checks", lambda mutation_budget, base_run_budget, test_contexts: checks)
        p.start()
        self.addCleanup(p.stop)

    def test_a_failing_check_fails_the_gate_wherever_its_findings_are(self):
        seen = []

        def find(change):
            seen.append(change)
            # Line 2 is not one the change added; a check's findings are listed anyway.
            return [{"path": "f.py", "line": 2, "severity": "error", "rule": "removed-test", "message": "test_a"},
                    {"path": None, "line": None, "severity": "error", "rule": "", "message": "could not run"}]
        self.checks(gate.GateCheck("integrity", fails=True, find=find))
        self.add_lines(1)
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- 2 findings of the `integrity` check; each is listed below.\n", out)
        self.assertIn("1. `f.py:2` error integrity removed-test: test_a\n"
                      "2. The change as a whole: error integrity: could not run\n", out)
        [change] = seen
        self.assertEqual((change.root, change.base, change.changed),
                         (os.path.realpath(self.repo), self.git("rev-parse", "HEAD").strip(), {"f.py": {4}}))
        self.assertIn("+new4 = 4", change.diff)

    def test_a_reporting_check_is_weighed_after_the_issues_and_passes(self):
        self.checks(gate.GateCheck("flaky", fails=False, find=lambda change: [
            {"path": "f.py", "line": 4, "severity": "warning", "rule": "seeds 3 5", "message": "test_b"}]))
        self.add_lines(1)
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertIn("## Findings to weigh\n\nThese do not fail the gate. Fix each, or say in your report why it "
                      "should stand.\n\n1. `f.py:4` warning flaky seeds 3 5: test_b\n", out)
        self.says("ruff", "f.py:4:1: F401 `os` imported but unused\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("1. `f.py:4` error ruff F401: `os` imported but unused\n", out)
        self.assertIn("2. `f.py:4` warning flaky seeds 3 5: test_b\n", out)

    def test_a_removed_test_is_weighed_and_a_skip_fails_the_gate(self):
        self.put("tests/test_f.py", "def test_one():\n    assert 1\n\n\ndef test_two():\n    assert 2\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "tests")
        self.put("tests/test_f.py", "def test_one():\n    assert 1\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertIn("## Findings to weigh\n\n", out)
        self.assertIn("1. `tests/test_f.py:5` warning test-integrity removed-test: ", out)
        self.put("tests/test_f.py", "import pytest\n\n\n@pytest.mark.skip\ndef test_one():\n    assert 1\n")
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        self.assertIn("- 1 findings of the `test-integrity` check; each is listed below.\n", out)
        self.assertIn("1. `tests/test_f.py:4` error test-integrity skip: ", out)
        self.assertIn("2. `tests/test_f.py:5` warning test-integrity removed-test: ", out)

    def test_a_failing_check_weighs_its_weighed_rules(self):
        weighed = frozenset({"removed-test"})
        self.checks(gate.GateCheck("integrity", fails=True, weighed=weighed, find=lambda _: [
            {"path": "f.py", "line": 2, "severity": "error", "rule": "removed-test", "message": "test_a"},
            {"path": "f.py", "line": 3, "severity": "warning", "rule": "skip", "message": "test_b"}]))
        self.add_lines(1)
        status, out, _ = self.gate()
        self.assertEqual(status, 1)
        # The gate, not find, says what each finding does: a failing rule fails whatever severity find gave.
        self.assertIn("- 1 findings of the `integrity` check; each is listed below.\n", out)
        self.assertIn("1. `f.py:3` error integrity skip: test_b\n", out)
        self.assertIn("2. `f.py:2` warning integrity removed-test: test_a\n", out)

    def test_a_check_with_nothing_to_say_adds_nothing(self):
        self.checks(gate.GateCheck("quiet", fails=True, find=lambda change: []),
                    gate.GateCheck("hush", fails=False, find=lambda change: []))
        self.add_lines(1)
        status, out, _ = self.gate()
        self.assertEqual(status, 0)
        self.assertNotIn("quiet", out)
        self.assertNotIn("Findings to weigh", out)

    def test_mutation_budget_bounds_the_surviving_mutants_check(self):
        budgets = []

        def find(change, budget, test_contexts):
            budgets.append(budget)
            return []
        self.add_lines(1)
        with patch.object(gate, "surviving_mutants", find):
            self.gate()
            self.gate("--mutation-budget", "45")
        self.assertEqual(budgets, [gate.MUTATION_BUDGET, 45])
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            self.gate("--mutation-budget", "0")

    def test_test_contexts_names_the_data_file_the_surviving_mutants_check_narrows_mutmut_s_tests_by(self):
        files = []

        def find(change, budget, test_contexts):
            files.append(test_contexts)
            return []
        self.add_lines(1)
        with patch.object(gate, "surviving_mutants", find):
            self.gate()
            self.gate("--test-contexts", ".coverage")
        self.assertEqual(files, [None, ".coverage"])

    def test_the_checks_are_told_whose_rules_judge_the_change(self):
        seen = []
        self.checks(gate.GateCheck("rules", fails=False, find=lambda change: seen.append(change.rules) or []))
        main_rules = os.path.join(self.stubs, "main.toml")
        with open(main_rules, "w") as f:
            f.write("[tool.ruff]\n")
        self.gate()
        self.gate("--config", main_rules)
        self.assertEqual(seen, [os.path.join(self.repo, "pyproject.toml"), main_rules])

    def test_config_replaces_the_tree_s_rules(self):
        self.put("pyproject.toml", "[tool.ruff]\nline-length = 1\n")
        self.gate()
        self.assertIn(os.path.join(self.repo, "pyproject.toml"), self.argv("ruff"))
        main_rules = os.path.join(self.stubs, "main.toml")
        with open(main_rules, "w") as f:
            f.write("[tool.ruff]\n")
        _, out, _ = self.gate("--config", main_rules)
        for tool in ("ruff", "mypy", "pymarkdown"):
            with self.subTest(tool=tool):
                if tool != "pymarkdown":
                    self.assertIn(main_rules, self.argv(tool))
        self.assertIn(f"by the rules in `{main_rules}`.", out)
        self.assertIn(f"The gate took its rules from `{main_rules}`, so the edit did not change", out)

    def test_no_rules_file_writes_nothing(self):
        status, _, err = self.gate("--config", "missing.toml")
        self.assertEqual(status, gate.EXIT_GATE_UNJUDGED)
        self.assertIn("is not a file", err)

    def test_ctrl_c_writes_nothing_and_exits_interrupted(self):
        out = os.path.join(self.outside, "quality.md")
        with open(out, "w") as f:
            f.write("GATE: OK\n")
        with patch.object(gate, "judge_change", side_effect=KeyboardInterrupt):
            status, stdout, err = self.gate("--out", out)
        self.assertEqual((status, stdout), (gate.EXIT_INTERRUPTED, ""))
        self.assertIn("interrupted", err)
        with open(out) as f:
            self.assertEqual(f.read(), "GATE: OK\n")

    # The whole quality file the gate writes, held to its golden file (tests/golden.py), so that judge_change's
    # sentences are pinned too: the intro, the conditions these changes meet (linter findings, a failing check,
    # failing tests, coverage) and the coverage section. TestQualityReport's golden files pin the rest of the layout.

    def golden_run(self, *args, base="HEAD"):
        """gate's standard output for ARGS against base, with the base commit's sha written as <base> and HEAD's as
        <head>, <head> where they are one, and test-integrity as the only check: the other checks run pytest or
        mutmut, whose words are the host's."""
        self.checks(*(c for c in gate.gate_checks() if c.name == "test-integrity"))
        shas = {self.git("rev-parse", base).strip(): "<base>", self.git("rev-parse", "HEAD").strip(): "<head>"}
        _, out, _ = self.gate(*args, base=base)
        for sha, name in shas.items():
            out = out.replace(sha, name)
        return out

    def test_the_file_of_a_passing_change_is_golden(self):
        name = os.fsdecode(b"m\xff.py")
        self.put(name, "m = 1\n")
        self.git("add", name)
        self.add_lines(1)
        out = self.golden_run("--tests", self.junit(), "--coverage", self.coverage({4: 1}))
        assert_golden("gate_ok", out)

    def test_the_file_of_a_failing_change_is_golden(self):
        base = self.commit({"tests/test_f.py": "def test_one():\n    assert 1\n\n\ndef test_two():\n    assert 2\n",
                            "uv.lock": "version = 1\n"}, "tests")
        self.commit({"README.md": "Read me.\n"}, "head")
        self.put("tests/test_f.py", "import pytest\n\n\n@pytest.mark.skip\ndef test_one():\n    assert 1\n")
        self.put("uv.lock", "version = 2\n")
        self.add_lines(19)
        with open(os.path.join(self.repo, "f.py"), "a") as f:
            f.write("import os  # noqa: F401\n")
        self.says("ruff", "f.py:2:1: F401 `os` imported but unused\n"
                          "f.py:4:7: B023 Function definition does not bind loop variable `x`\nFound 2 errors.\n")
        out = self.golden_run("--tests", self.junit(("test_limit", "AssertionError: 3 != 2")),
                              "--coverage", self.coverage({n: int(n < 14) for n in range(1, 24)}), base=base)
        assert_golden("gate_error", out)


class TestClippy(RepositoryTest):
    """`gate.py` with the real cargo clippy, over a crate of no dependencies in a subdirectory of the repository, as
    `ui/src-tauri/` is in this one. cargo runs offline, so it never reaches for the registry."""

    LIB = "pub fn one() -> i32 {\n    1\n}\n"
    BASE_FILES = {"pyproject.toml": "[tool.ruff]\n",
                  "crate/Cargo.toml": '[package]\nname = "tiny"\nversion = "0.1.0"\nedition = "2021"\n',
                  "crate/src/lib.rs": LIB}

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"CARGO_NET_OFFLINE": "true"})
        env.start()
        self.addCleanup(env.stop)

    def gate(self):
        """The exit status and standard output of `gate.py --base HEAD` in the repository."""
        out = io.StringIO()
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            status = gate.main(["--base", "HEAD"])
        return status, out.getvalue()

    def stub_cargo(self, script):
        """Put a cargo that runs the sh script on PATH, ahead of the real one."""
        bin_dir = os.path.join(self.outside, "bin")
        os.mkdir(bin_dir)
        with open(os.path.join(bin_dir, "cargo"), "w") as f:
            f.write(f"#!/bin/sh\n{script}\n")
        os.chmod(os.path.join(bin_dir, "cargo"), 0o700)
        env = patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_lint_on_an_added_line_is_an_issue_listed_once(self):
        # --all-targets checks the library twice, as itself and as its tests, and clippy reports the lint for each.
        self.put("crate/src/lib.rs", self.LIB + "pub fn two() -> i32 {\n    return 2;\n}\n")
        status, out = self.gate()
        self.assertEqual(status, 1, out)
        self.assertIn("- 1 findings on lines the change added; each is listed below.\n", out)
        self.assertIn("1. `crate/src/lib.rs:5` warning clippy clippy::needless_return: unneeded `return` "
                      "statement\n", out)
        self.assertEqual(out.count("needless_return"), 1)

    def test_a_lint_on_an_old_line_counts_as_elsewhere(self):
        two = "pub fn two() -> i32 {\n    return 2;\n}\n"
        self.commit({"crate/src/lib.rs": two}, "a lint")
        self.put("crate/src/lib.rs", two + self.LIB)
        status, out = self.gate()
        self.assertEqual(status, 0, out)
        self.assertIn("1 other open issues in the project", out)

    def test_a_compile_error_on_an_old_line_fails_the_gate_naming_the_first_error(self):
        two = "pub fn two() -> i32 {\n    one() + 1\n}\n"
        self.commit({"crate/src/lib.rs": self.LIB + two}, "a caller")
        self.put("crate/src/lib.rs", two)
        status, out = self.gate()
        self.assertEqual(status, 1, out)
        self.assertIn("- `clippy` could not compile the workspace of `crate/Cargo.toml`: `crate/src/lib.rs:2` error "
                      "clippy E0425: cannot find function `one` in this scope\n", out)
        self.assertIn("## Issues on changed lines\n\nNone.\n", out)

    def test_a_lint_at_deny_in_one_crate_hides_neither_a_compile_error_in_the_next_nor_its_own_lints(self):
        # b uses a, so cargo checks a first; a lint at deny there would fail a's build and leave b unchecked.
        self.commit({"crate/Cargo.toml": '[workspace]\nmembers = ["a", "b"]\nresolver = "2"\n',
                     "crate/a/Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                           '[lints.clippy]\nneedless_return = "deny"\n',
                     "crate/a/src/lib.rs": "pub fn two() -> i32 {\n    return 2;\n}\n",
                     "crate/b/Cargo.toml": '[package]\nname = "b"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                           '[dependencies]\na = { path = "../a" }\n',
                     "crate/b/src/lib.rs": "pub fn three() -> i32 {\n    a::two() + 1\n}\n"}, "a workspace")
        self.git("rm", "--quiet", "crate/src/lib.rs")
        self.put("crate/b/src/lib.rs", "pub fn three() -> i32 {\n    a::two() + one\n}\n")
        status, out = self.gate()
        self.assertEqual(status, 1, out)
        self.assertIn("- `clippy` could not compile the workspace of `crate/Cargo.toml`: `crate/b/src/lib.rs:2` error "
                      "clippy E0425: cannot find value `one` in this scope\n", out)
        self.assertIn("1 other open issues in the project", out)

    def test_a_tracked_cargo_lock_is_held_to_not_rewritten(self):
        lock = '# This file is automatically @generated by Cargo.\nversion = 4\n\n[[package]]\nname = "tiny"\n' \
               'version = "0.1.0"\n'
        self.commit({"crate/Cargo.lock": lock}, "a lock")
        self.edit("crate/Cargo.toml", 'version = "0.1.0"', 'version = "0.2.0"')
        status, out = self.gate()
        self.assertEqual(status, 1, out)
        self.assertIn("- `clippy` failed with exit status 101 in `crate/Cargo.toml`: error: cannot update the lock "
                      "file", out)
        with open(os.path.join(self.repo, "crate/Cargo.lock")) as f:
            self.assertEqual(f.read(), lock)

    def test_a_cargo_past_the_timeout_is_killed_with_the_processes_it_started(self):
        child = os.path.join(self.outside, "child")
        self.stub_cargo(f'case "$1 $2" in\n'
                        f'"clippy --version") exit 0 ;;\n'
                        f'"locate-project "*) echo "$PWD/crate/Cargo.toml" ;;\n'
                        f'*) sleep 60 & echo $! > "{child}"; wait ;;\n'
                        f'esac')
        with patch.object(gate, "GATE_TOOL_TIMEOUT", 1):
            status, out = self.gate()
        self.assertEqual(status, 1, out)
        self.assertIn("- `clippy` did not finish within 1 seconds.\n", out)
        with open(child) as f:
            pid = f.read().strip()
        # Killed, the sleep is gone, or a zombie until this container's init reaps it.
        with contextlib.suppress(FileNotFoundError), open(f"/proc/{pid}/stat") as f:
            self.assertEqual(f.read().rpartition(")")[2].split()[0], "Z")

    def test_a_repository_without_rust_files_runs_no_cargo(self):
        self.git("rm", "--quiet", "crate/src/lib.rs")
        self.stub_cargo(f'echo ran > "{self.outside}/cargo-ran"; exit 1')
        status, out = self.gate()
        self.assertEqual(status, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "cargo-ran")))

    def test_a_cargo_without_clippy_fails_naming_it(self):
        self.stub_cargo('echo "error: no such command: \\`clippy\\`" >&2; exit 101')
        status, out = self.gate()
        self.assertEqual(status, 1, out)
        self.assertIn("- `clippy` is not installed: `cargo clippy --version` failed with exit status 101: error: no "
                      "such command: `clippy`\n", out)

    def test_an_added_allow_attribute_is_listed_as_a_suppression_in_a_rust_file_alone(self):
        self.put("crate/src/lib.rs", "#![cfg_attr(not(test), allow(clippy::all))]\n" + self.LIB
                 + "#[allow(clippy::needless_return)]\npub fn two() -> i32 {\n    return 2;\n}\n"
                 + '#[cfg_attr(\n    not(test),\n    allow(clippy::needless_return)\n)]\npub fn three() {}\n'
                 + '#[test] fn parses() { "1".parse::<i32>().expect("ok"); }\n')
        self.put("crate/notes.txt", "Write `#[allow(lint, reason = \"...\")]`.\n")
        self.git("add", "crate/notes.txt")
        status, out = self.gate()
        self.assertEqual(status, 0, out)
        self.assertTrue(out.endswith("## Suppressions added\n\n"
                                     "- `crate/src/lib.rs:1` `#![cfg_attr(not(test), allow(clippy::all))]`\n"
                                     "- `crate/src/lib.rs:5` `#[allow(clippy::needless_return)]`\n"
                                     "- `crate/src/lib.rs:11` `allow(clippy::needless_return)`\n"), out)

    def test_a_build_that_fails_without_a_compile_error_fails_naming_cargo_s_error(self):
        self.put("crate/build.rs", 'fn main() {\n    panic!("no");\n}\n')
        self.git("add", "crate/build.rs")
        status, out = self.gate()
        self.assertEqual(status, 1, out)
        self.assertIn("- `clippy` failed with exit status 101 in `crate/Cargo.toml`: error: failed to run custom build "
                      "command for `tiny v0.1.0", out)

    def test_a_manifest_cargo_cannot_read_fails_naming_it_and_the_rest_are_linted(self):
        self.put("broken/Cargo.toml", "[package\n")
        self.git("add", "broken/Cargo.toml")
        self.put("crate/src/lib.rs", self.LIB + "pub fn two() -> i32 {\n    return 2;\n}\n")
        status, out = self.gate()
        self.assertEqual(status, 1, out)
        self.assertIn("- `clippy` could not find the workspace of `broken/Cargo.toml`: error: ", out)
        self.assertIn("`crate/src/lib.rs:5` warning clippy clippy::needless_return", out)


class TestTestIntegrityCheck(unittest.TestCase):
    def test_it_fails_the_gate_but_weighs_what_a_spec_can_ask_for(self):
        [check] = [c for c in gate.gate_checks() if c.name == "test-integrity"]
        self.assertEqual((check.fails, check.find), (True, gate.weakened_tests))
        self.assertEqual(check.weighed, {"removed-test", "fewer-assertions", "expected-value-changed"})


class TestWeakenedTests(RepositoryTest):
    """The test-integrity check over a real git repository: a base commit, then the change in its working tree."""

    BASE_FILES = {
        "tests/test_calc.py": (
            "import unittest\n"                                # 1
            "\n"                                               # 2
            "from calc import add\n"                           # 3
            "\n"                                               # 4
            "\n"                                               # 5
            "class TestAdd(unittest.TestCase):\n"              # 6
            "    def test_small(self):\n"                      # 7
            "        self.assertEqual(add(1, 2), 3)\n"         # 8
            "        self.assertEqual(add(0, 0), 0)\n"         # 9
            "\n"                                               # 10
            "    def test_large(self):\n"                      # 11
            "        assert add(10, 20) == 30\n"               # 12
            "\n"                                               # 13
            "\n"                                               # 14
            "class TestNegative(unittest.TestCase):\n"         # 15
            "    def test_one(self):\n"                        # 16
            "        assert add(-1, -1) == -2\n"               # 17
            "\n"                                               # 18
            "    def test_two(self):\n"                        # 19
            "        with self.assertRaises(TypeError):\n"     # 20
            "            add(None, 1)\n"                       # 21
            "\n"                                               # 22
            "\n"                                               # 23
            "def test_function():\n"                           # 24
            "    assert add(2, 2) == 4\n"                      # 25
        ),
        "tests/test_other.py": "def test_a():\n    assert True\n\n\ndef test_b():\n    assert 1\n",
        "calc.py": "def add(a, b):\n    return a + b\n",
    }

    def found(self):
        """(path, line, rule) of each finding on the working tree's change, and the findings themselves."""
        base = self.git("rev-parse", "HEAD").strip()
        found = gate.weakened_tests(gate.GateChange.from_tree(self.repo, base))
        return [(f["path"], f["line"], f["rule"]) for f in found], found

    def test_an_unchanged_tree_has_no_findings(self):
        self.assertEqual(self.found()[0], [])

    def test_a_removed_test_is_found_at_its_line_at_the_base(self):
        self.edit("tests/test_calc.py", "    def test_large(self):\n        assert add(10, 20) == 30\n\n", "")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 11, "removed-test")])
        self.assertIn("`TestAdd.test_large`", found[0]["message"])
        self.assertIn("line 11 at the base", found[0]["message"])

    def test_a_removed_class_is_one_finding_with_its_tests(self):
        self.edit("tests/test_calc.py", self.BASE_FILES["tests/test_calc.py"][
            self.BASE_FILES["tests/test_calc.py"].index("class TestNegative"):
            self.BASE_FILES["tests/test_calc.py"].index("def test_function")], "")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 15, "removed-test")])
        self.assertIn("`TestNegative` and its 2 tests", found[0]["message"])

    def test_a_deleted_test_file_is_one_finding(self):
        os.remove(os.path.join(self.repo, "tests/test_other.py"))
        where, found = self.found()
        self.assertEqual(where, [("tests/test_other.py", None, "removed-test")])
        self.assertIn("2 tests", found[0]["message"])

    def test_a_renamed_test_with_the_same_body_is_not_removed(self):
        self.edit("tests/test_calc.py", "def test_large(self):", "def test_big(self):")
        self.assertEqual(self.found()[0], [])

    def test_a_renamed_test_deeper_than_python_s_recursion_limit_is_matched(self):
        # A sum of 3000 terms parses, but a recursive walk of it, as ast.dump makes, overflows the stack.
        deep = "def test_deep():\n    assert " + " + ".join(["1"] * 3000) + " == 3000\n"
        self.put("tests/test_deep.py", deep)
        self.git("add", "tests/test_deep.py")
        self.git("commit", "--quiet", "-m", "deep")
        self.edit("tests/test_deep.py", "def test_deep():", "def test_deeper():")
        self.assertEqual(self.found()[0], [])

    def test_a_test_deeper_than_python_s_recursion_limit_is_compared_with_its_base(self):
        # Changing a literal while the code changes too compares the test's assertions by their shape.
        deep = "def test_deep():\n    x = 1\n    assert x + " + " + ".join(["1"] * 3000) + " == 3001\n"
        self.put("tests/test_deep.py", deep)
        self.git("add", "tests/test_deep.py")
        self.git("commit", "--quiet", "-m", "deep")
        self.edit("tests/test_deep.py", "    x = 1\n", "    x = 2\n")
        self.edit("calc.py", "return a + b", "return b + a")
        self.assertEqual(self.found()[0], [])

    def test_a_deep_assertion_whose_expected_value_changes_is_reported_without_its_text(self):
        deep = "def test_deep():\n    assert " + " + ".join(["1"] * 3000) + " == 3001\n"
        self.put("tests/test_deep.py", deep)
        self.git("add", "tests/test_deep.py")
        self.git("commit", "--quiet", "-m", "deep")
        self.edit("tests/test_deep.py", "== 3001", "== 3002")
        self.edit("calc.py", "return a + b", "return b + a")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_deep.py", 2, "expected-value-changed")])
        self.assertIn("too deep to print", found[0]["message"])

    def test_an_assertion_with_an_int_too_long_to_print_is_reported_without_its_text(self):
        # ast.unparse writes an int with repr, which refuses one of more than 4300 decimal digits.
        self.put("tests/test_big.py", "def test_big():\n    assert 1 == 0x" + "f" * 4000 + "\n")
        self.git("add", "tests/test_big.py")
        self.git("commit", "--quiet", "-m", "big")
        self.edit("tests/test_big.py", "assert 1 ==", "assert 2 ==")
        self.edit("calc.py", "return a + b", "return b + a")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_big.py", 2, "expected-value-changed")])
        self.assertIn("(code at line 2, too long to print)", found[0]["message"])

    def test_tests_moved_to_a_renamed_file_are_not_removed(self):
        self.git("mv", "tests/test_other.py", "tests/test_moved.py")
        self.edit("tests/test_moved.py", "def test_b():", "def test_bee():")
        self.assertEqual(self.found()[0], [])

    def test_tests_moved_into_another_file_are_not_removed(self):
        os.remove(os.path.join(self.repo, "tests/test_other.py"))
        self.put("tests/test_new.py", "def test_a():\n    assert True\n\n\ndef test_b():\n    assert 1\n")
        self.git("add", "-A")
        self.assertEqual(self.found()[0], [])

    def test_added_skips_are_found_at_their_lines(self):
        self.edit("tests/test_calc.py", "    def test_small(self):\n",
                  "    @unittest.skip('later')\n    def test_small(self):\n")                     # line 7
        self.edit("tests/test_calc.py", "    def test_one(self):\n        assert",
                  "    def test_one(self):\n        self.skipTest('flaky')\n        assert")     # line 18
        self.edit("tests/test_other.py", "def test_b():\n",
                  "import pytest\n\n\n@pytest.mark.xfail\ndef test_b():\n    pytest.skip('no')\n")
        where, _ = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 7, "skip"), ("tests/test_calc.py", 18, "skip"),
                                 ("tests/test_other.py", 8, "skip"), ("tests/test_other.py", 10, "skip")])

    def test_skips_already_at_the_base_are_not_found(self):
        self.put("tests/test_skipped.py", "import pytest\n\n\n@pytest.mark.skipif(True, reason='x')\n"
                                          "def test_x():\n    assert 1\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "skipped")
        self.edit("tests/test_skipped.py", "assert 1", "assert 2")
        self.assertEqual(self.found()[0], [])

    def test_fewer_assertions_are_found_at_the_test(self):
        self.edit("tests/test_calc.py", "        self.assertEqual(add(0, 0), 0)\n", "")
        self.edit("tests/test_calc.py", "        with self.assertRaises(TypeError):\n            add(None, 1)\n",
                  "        add(None, 1)\n")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 7, "fewer-assertions"),
                                 ("tests/test_calc.py", 18, "fewer-assertions")])
        self.assertIn("`TestAdd.test_small` has 1 assertions, 2 at the base", found[0]["message"])

    def test_more_assertions_and_new_tests_are_not_found(self):
        self.edit("tests/test_calc.py", "        assert add(10, 20) == 30\n",
                  "        assert add(10, 20) == 30\n        assert add(20, 10) == 30\n")
        self.edit("tests/test_other.py", "def test_a():", "def test_new():\n    assert 3\n\n\ndef test_a():")
        self.assertEqual(self.found()[0], [])

    def test_swallowed_errors_are_found(self):
        self.edit("tests/test_calc.py", "        assert add(-1, -1) == -2\n",
                  "        try:\n            assert add(-1, -1) == -2\n        except:\n            pass\n")
        self.edit("tests/test_calc.py", "    assert add(2, 2) == 4\n",
                  "    try:\n        assert add(2, 2) == 4\n    except Exception:\n        pass\n"
                  "    try:\n        add(1, 1)\n    except ValueError:\n        pass\n")
        where, _ = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 19, "swallowed-error"),
                                 ("tests/test_calc.py", 30, "swallowed-error")])

    def test_an_expected_value_changed_with_the_code_is_reported_as_the_pair(self):
        self.edit("calc.py", "return a + b", "return a + b + 1")
        self.edit("tests/test_calc.py", "assert add(10, 20) == 30", "assert add(10, 20) == 31")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_calc.py", 12, "expected-value-changed")])
        self.assertIn("`assert add(10, 20) == 30` became `assert add(10, 20) == 31`", found[0]["message"])

    def test_an_expected_value_changed_alone_is_not_found(self):
        self.edit("tests/test_calc.py", "assert add(10, 20) == 30", "assert add(10, 20) == 31")
        self.assertEqual(self.found()[0], [])

    # A test file whose test asserts against a module-level constant: the expected value held outside the assertion.
    GOLDEN_TESTS = (
        "import unittest\n"                                       # 1
        "\n"                                                      # 2
        "from calc import add\n"                                  # 3
        "\n"                                                      # 4
        'GOLDEN = "a"\n'                                          # 5
        "\n"                                                      # 6
        "\n"                                                      # 7
        "class TestGolden(unittest.TestCase):\n"                  # 8
        "    def test_add(self):\n"                               # 9
        '        self.assertEqual(add("", "a"), GOLDEN)\n'        # 10
    )

    def commit_golden(self, tests=GOLDEN_TESTS):
        """Commit tests as tests/test_golden.py, on top of the base."""
        self.commit({"tests/test_golden.py": tests}, "golden")

    def test_an_edited_constant_an_assertion_names_is_an_expected_value_changed(self):
        self.commit_golden()
        self.edit("calc.py", "return a + b", "return b + a")
        self.edit("tests/test_golden.py", 'GOLDEN = "a"', 'GOLDEN = "b"')
        where, found = self.found()
        self.assertEqual(where, [("tests/test_golden.py", 10, "expected-value-changed")])
        self.assertIn("`GOLDEN` (line 5), which `TestGolden.test_add` names in its assertions, changed,",
                      found[0]["message"])

    def test_an_edited_constant_named_through_the_test_module_is_an_expected_value_changed(self):
        self.commit_golden(self.GOLDEN_TESTS.replace(", GOLDEN)", ", test_golden.GOLDEN)"))
        self.edit("calc.py", "return a + b", "return b + a")
        self.edit("tests/test_golden.py", 'GOLDEN = "a"', 'GOLDEN = "b"')
        where, found = self.found()
        self.assertEqual(where, [("tests/test_golden.py", 10, "expected-value-changed")])
        self.assertIn("`GOLDEN` (line 5)", found[0]["message"])

    def test_an_edited_annotated_constant_is_an_expected_value_changed(self):
        self.commit_golden(self.GOLDEN_TESTS.replace('GOLDEN = "a"', 'GOLDEN: str = "a"'))
        self.edit("calc.py", "return a + b", "return b + a")
        self.edit("tests/test_golden.py", 'GOLDEN: str = "a"', 'GOLDEN: str = "b"')
        self.assertEqual(self.found()[0], [("tests/test_golden.py", 10, "expected-value-changed")])

    def test_an_edited_constant_without_a_code_change_is_not_found(self):
        self.commit_golden()
        self.edit("tests/test_golden.py", 'GOLDEN = "a"', 'GOLDEN = "b"')
        self.assertEqual(self.found()[0], [])

    def test_a_constant_added_with_its_test_is_not_found(self):
        self.edit("calc.py", "return a + b", "return b + a")
        self.put("tests/test_golden.py", self.GOLDEN_TESTS)
        self.assertEqual(self.found()[0], [])

    def test_a_constant_added_for_a_kept_test_is_not_found(self):
        self.commit_golden(self.GOLDEN_TESTS.replace(", GOLDEN)", ', "a")').replace('GOLDEN = "a"\n', ""))
        self.edit("calc.py", "return a + b", "return b + a")
        self.put("tests/test_golden.py", self.GOLDEN_TESTS.replace('"a"\n', '"b"\n'))
        self.assertEqual(self.found()[0], [])

    def test_an_edited_constant_computed_by_a_call_is_not_found(self):
        self.commit_golden(self.GOLDEN_TESTS.replace('GOLDEN = "a"', 'GOLDEN = str("a")'))
        self.edit("calc.py", "return a + b", "return b + a")
        self.edit("tests/test_golden.py", 'GOLDEN = str("a")', 'GOLDEN = str("b")')
        self.assertEqual(self.found()[0], [])

    def test_a_local_of_the_constant_s_name_is_not_the_constant(self):
        self.commit_golden(self.GOLDEN_TESTS.replace("        self.assertEqual",
                                                     '        GOLDEN = "a"\n        self.assertEqual'))
        self.edit("calc.py", "return a + b", "return b + a")
        self.edit("tests/test_golden.py", 'GOLDEN = "a"\n\n', 'GOLDEN = "b"\n\n')
        self.assertEqual(self.found()[0], [])

    def test_two_tests_asserting_against_one_edited_constant_are_one_finding_each(self):
        self.commit_golden(self.GOLDEN_TESTS + (
            "\n"                                                  # 11
            "    def test_again(self):\n"                         # 12
            '        self.assertEqual(add("a", ""), GOLDEN)\n'    # 13
            '        self.assertNotEqual(add("", "").upper(), GOLDEN)\n'  # 14
        ))
        self.edit("calc.py", "return a + b", "return b + a")
        self.edit("tests/test_golden.py", 'GOLDEN = "a"', 'GOLDEN = "b"')
        where, found = self.found()
        self.assertEqual(where, [("tests/test_golden.py", 10, "expected-value-changed"),
                                 ("tests/test_golden.py", 13, "expected-value-changed")])
        self.assertIn("`TestGolden.test_again` names in its assertions", found[1]["message"])

    def found_after_golden_edit(self, old, new, tests=GOLDEN_TESTS):
        """found()'s answer once tests is committed as tests/test_golden.py and the change then edits the code and
        replaces old with new in that file."""
        self.commit_golden(tests)
        self.edit("calc.py", "return a + b", "return b + a")
        self.edit("tests/test_golden.py", old, new)
        return self.found()

    def test_a_constant_bound_again_is_an_expected_value_changed(self):
        where, found = self.found_after_golden_edit('GOLDEN = "a"\n', 'GOLDEN = "a"\nGOLDEN = "b"\n')
        self.assertEqual(where, [("tests/test_golden.py", 11, "expected-value-changed")])
        self.assertIn("`GOLDEN` (line 5 at the base), which `TestGolden.test_add` names in its assertions, is no "
                      "longer a constant", found[0]["message"])

    def test_a_constant_now_computed_by_a_call_is_an_expected_value_changed(self):
        where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = str("b")')
        self.assertEqual(where, [("tests/test_golden.py", 10, "expected-value-changed")])

    def test_a_function_s_local_of_the_constant_s_name_leaves_it_a_constant(self):
        where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = "b"', self.GOLDEN_TESTS + (
            "\n\ntry:\n    import json\nexcept ImportError:\n    def helper():\n        GOLDEN = 1\n"))
        self.assertEqual(where, [("tests/test_golden.py", 10, "expected-value-changed")])

    def test_an_except_s_name_in_the_test_is_not_the_constant(self):
        where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = "b"', self.GOLDEN_TESTS.replace(
            '        self.assertEqual(add("", "a"), GOLDEN)\n',
            "        try:\n            add(None, 1)\n        except TypeError as GOLDEN:\n"
            '            self.assertIn("x", str(GOLDEN))\n'))
        self.assertEqual(where, [])

    def test_a_constant_s_sign_or_type_changed_is_an_expected_value_changed(self):
        for old, new in [("-5", "5"), ("1", "True"), ('("a",)', '"a"')]:
            with self.subTest(old=old, new=new):
                self.setUp()
                where, _ = self.found_after_golden_edit(f"GOLDEN = {old}\n", f"GOLDEN = {new}\n",
                                                        self.GOLDEN_TESTS.replace('GOLDEN = "a"', f"GOLDEN = {old}"))
                self.assertEqual(where, [("tests/test_golden.py", 10, "expected-value-changed")])

    def test_a_constant_named_through_the_module_s_package_or_an_alias_is_an_expected_value_changed(self):
        for spelled, imported in [("tests.test_golden", "\n"), ("tg", "import tests.test_golden as tg\n"),
                                  ("tg", "from tests import test_golden as tg\n")]:
            with self.subTest(spelled=spelled, imported=imported):
                self.setUp()
                tests = self.GOLDEN_TESTS.replace(", GOLDEN)", f", {spelled}.GOLDEN)").replace(
                    "from calc import add\n\n", "from calc import add\n" + imported)
                where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = "b"', tests)
                self.assertEqual(where, [("tests/test_golden.py", 10, "expected-value-changed")])

    def test_a_name_an_inner_scope_of_the_test_binds_is_not_the_constant(self):
        for inner in ["        def check(GOLDEN):\n            assert add('', 'a') == GOLDEN\n\n        check('a')\n",
                      "        assert all(GOLDEN for GOLDEN in ['a'])\n",
                      "        assert [GOLDEN for GOLDEN in ['a']]\n",
                      "        assert (lambda GOLDEN: GOLDEN)('a')\n"]:
            with self.subTest(inner=inner):
                self.setUp()
                where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = "b"', self.GOLDEN_TESTS.replace(
                    '        self.assertEqual(add("", "a"), GOLDEN)\n', inner))
                self.assertEqual(where, [])

    def test_an_attribute_of_a_local_named_as_the_module_is_not_the_constant(self):
        for spelled in ["tg", "test_golden", "tests"]:
            with self.subTest(spelled=spelled):
                self.setUp()
                tests = self.GOLDEN_TESTS.replace("from calc import add\n\n", "from calc import add\n"
                                                  "import tests.test_golden as tg\n").replace(
                    "    def test_add(self):\n", f"    def test_add(self, {spelled}=None):\n").replace(
                    ", GOLDEN)", f", {spelled}.GOLDEN)" if spelled != "tests" else ", tests.test_golden.GOLDEN)")
                where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = "b"', tests)
                self.assertEqual(where, [])

    def test_a_constant_whose_name_the_test_binds_anywhere_is_not_checked(self):
        # Shadowing is not resolved: any binding of the name in the test, at any depth, leaves the constant out.
        for inner in [("        class F:\n            GOLDEN = 1\n\n            def m(self):\n"
                       '                assert add("", "a") == GOLDEN\n\n        F().m()\n'),
                      '        assert (lambda GOLDEN=GOLDEN: add("", "a") == GOLDEN)()\n',
                      "        assert all(GOLDEN for GOLDEN in [GOLDEN])\n",
                      '        global GOLDEN\n        assert add("", "a") == GOLDEN\n',
                      '        assert add("", "a") == GOLDEN\n        del GOLDEN\n',
                      '        from calc import add as GOLDEN\n        assert add("", "a") == GOLDEN\n',
                      # Review 54, F15: an inlined comprehension's target beside a read of the module's name.
                      '        x = GOLDEN\n        assert [GOLDEN for GOLDEN in "b"] and x\n']:
            with self.subTest(inner=inner):
                self.setUp()
                where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = "b"', self.GOLDEN_TESTS.replace(
                    '        self.assertEqual(add("", "a"), GOLDEN)\n', inner))
                self.assertEqual(where, [])

    def test_a_constant_read_inside_nested_generators_or_lambdas_is_found(self):
        # Review 54, F14: a scope nested in a same-named scope's first iterable or default.
        for inner in ['        assert all(GOLDEN == x for x in (y for y in "a"))\n',
                      '        assert all(x for x in (y == GOLDEN for y in "a"))\n',
                      '        assert (lambda f=(lambda: 0): add("", "a") == GOLDEN)()\n']:
            with self.subTest(inner=inner):
                self.setUp()
                where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = "b"', self.GOLDEN_TESTS.replace(
                    '        self.assertEqual(add("", "a"), GOLDEN)\n', inner))
                self.assertEqual(where, [("tests/test_golden.py", 10, "expected-value-changed")])

    def test_the_module_imported_inside_the_test_is_not_checked(self):
        # The import binds the root of module.NAME in the test, so, as for any binding there, nothing is checked.
        for lines in [["import tests.test_golden", 'self.assertEqual(add("", "a"), tests.test_golden.GOLDEN)'],
                      ["from tests import test_golden", 'self.assertEqual(add("", "a"), test_golden.GOLDEN)'],
                      ["import tests.test_golden as tg", "", "def check():",
                       '    assert add("", "a") == tg.GOLDEN', "", "check()"],
                      # Review 54, F16: a parameter an import in the test sometimes rebinds.
                      ["if tg:", "    import tests.test_golden as tg", 'self.assertEqual(add("", "a"), tg.GOLDEN)']]:
            with self.subTest(lines=lines):
                self.setUp()
                where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = "b"', self.GOLDEN_TESTS.replace(
                    "    def test_add(self):\n", "    def test_add(self, tg=None):\n").replace(
                    '        self.assertEqual(add("", "a"), GOLDEN)\n', "".join(f"        {x}\n" for x in lines)))
                self.assertEqual(where, [])

    def test_an_unchanged_constant_with_a_code_change_is_not_found(self):
        where, _ = self.found_after_golden_edit("from calc import add\n", "from calc import add  # the code\n")
        self.assertEqual(where, [])

    def test_a_test_with_type_parameters_reads_the_module_s_constant(self):
        where, _ = self.found_after_golden_edit('GOLDEN = "a"', 'GOLDEN = "b"', self.GOLDEN_TESTS.replace(
            "    def test_add(self):", "    def test_add[T](self):"))
        self.assertEqual(where, [("tests/test_golden.py", 10, "expected-value-changed")])

    def commit_mutmut_state(self):
        """Commit what mutmut leaves in mutants/ after a run: its copy of the code and of the tests beside it."""
        for name, text in self.BASE_FILES.items():
            self.put(f"mutants/{name}", text)
        self.git("add", "mutants")
        self.git("commit", "--quiet", "-m", "mutmut's state")

    def test_a_removed_test_is_found_whatever_syntax_the_environment_gives_git_s_pathspecs(self):
        self.put("Mutants/test_case.py", "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n")
        self.git("add", "Mutants")
        self.git("commit", "--quiet", "-m", "a directory that is not mutmut's")
        self.edit("Mutants/test_case.py", "def test_b():\n    assert 2\n", "")
        for name in ("GIT_LITERAL_PATHSPECS", "GIT_ICASE_PATHSPECS"):
            with self.subTest(name), patch.dict("os.environ", {name: "1"}):
                self.assertEqual(self.found()[0], [("Mutants/test_case.py", 5, "removed-test")])

    def test_removing_mutmut_s_state_removes_no_test(self):
        self.commit_mutmut_state()
        self.git("rm", "-r", "--quiet", "mutants")
        self.assertEqual(self.found()[0], [])

    def test_an_edit_to_mutmut_s_state_is_no_change_to_the_code(self):
        self.commit_mutmut_state()
        self.edit("mutants/calc.py", "return a + b", "return a + b + 1")
        self.edit("tests/test_calc.py", "assert add(10, 20) == 30", "assert add(10, 20) == 31")
        self.assertEqual(self.found()[0], [])

    def test_a_test_moved_out_of_mutmut_s_state_is_a_new_test(self):
        # Seen without mutants/, the move adds the file, so the skip at its base is on an added line.
        self.put("mutants/tests/test_skipped.py", "import pytest\n\n\n@pytest.mark.skip\ndef test_x():\n    assert 1\n")
        self.git("add", "mutants")
        self.git("commit", "--quiet", "-m", "mutmut's state")
        self.git("mv", "mutants/tests/test_skipped.py", "tests/test_skipped.py")
        self.assertEqual(self.found()[0], [("tests/test_skipped.py", 4, "skip")])

    def test_a_test_file_that_does_not_parse_says_so(self):
        self.edit("tests/test_other.py", "def test_b():", "def test_b(:")
        where, _ = self.found()
        self.assertEqual(where, [("tests/test_other.py", None, "unparsable")])

    def test_a_test_file_that_cannot_be_read_says_so(self):
        with open(os.path.join(self.repo, "tests/test_other.py"), "wb") as f:
            f.write(b"def test_a():\n    assert '\xff'\n")
        where, found = self.found()
        self.assertEqual(where, [("tests/test_other.py", None, "unreadable")])
        self.assertIn("so its tests were not checked", found[0]["message"])

    def test_a_removed_helper_class_is_not_a_test(self):
        self.put("tests/test_help.py", "class Helper:\n    def make(self):\n        return 1\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "helper")
        self.put("tests/test_help.py", "")
        self.assertEqual(self.found()[0], [])

    def test_only_test_files_are_read(self):
        self.put("helpers.py", "def test_helper():\n    assert 1\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "helper")
        os.remove(os.path.join(self.repo, "helpers.py"))
        self.put("pkg/sub_test.py", "def test_x():\n    pass\n")
        self.git("add", "-A")
        self.assertEqual(self.found()[0], [])

    def test_changed_files_pairs_each_side(self):
        self.git("mv", "tests/test_other.py", "tests/test_renamed.py")
        os.remove(os.path.join(self.repo, "calc.py"))
        self.put("new.py", "x = 1\n")
        self.git("add", "new.py")
        self.edit("tests/test_calc.py", "import unittest\n", "import unittest  # edited\n")
        self.assertEqual(sorted(gate._changed_files(self.repo, "HEAD"), key=str),
                         sorted([("calc.py", None), (None, "new.py"), ("tests/test_calc.py", "tests/test_calc.py"),
                                 ("tests/test_other.py", "tests/test_renamed.py")], key=str))


class TestRunInSession(unittest.TestCase):
    """_run_in_session, which runs the gate's pytest and mutmut, on shell commands."""

    def run_sh(self, code, *, timeout=10, soft_stop=None):
        return gate._run_in_session(["sh", "-c", code], tempfile.gettempdir(), dict(os.environ),
                                            timeout=timeout, soft_stop=soft_stop)

    def test_a_tool_that_finishes_returns_its_status_and_output(self):
        proc, stopped = self.run_sh("echo out; echo err >&2; exit 3")
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr, stopped), (3, "out\n", "err\n", False))

    def test_a_tool_that_exits_on_sigint_returns_what_it_printed(self):
        # Ignored, as in a Jenkins sh step, which starts its command as a background job of a non-interactive shell.
        for here in (signal.SIG_IGN, signal.default_int_handler):
            with self.subTest(sigint=here):
                before = signal.signal(signal.SIGINT, here)
                try:
                    proc, stopped = self.run_sh("trap 'echo bye; exit 5' INT; echo hi; while :; do sleep 0.1; done",
                                                timeout=0.5, soft_stop=(signal.SIGINT, 5))
                    self.assertEqual(signal.getsignal(signal.SIGINT), here)
                finally:
                    signal.signal(signal.SIGINT, before)
                self.assertEqual((proc.returncode, proc.stdout, stopped), (5, "hi\nbye\n", True))

    def test_a_tool_that_ignores_sigint_is_killed_after_the_grace(self):
        started = time.monotonic()
        proc, stopped = self.run_sh("trap '' INT; echo hi; exec sleep 30", timeout=0.3, soft_stop=(signal.SIGINT, 0.3))
        self.assertLess(time.monotonic() - started, 0.6 + gate.TOOL_KILL_WAIT)
        self.assertEqual((proc.returncode, proc.stdout, stopped), (-signal.SIGKILL, "hi\n", True))

    def test_a_tool_that_prints_more_than_a_pipe_holds_is_not_blocked(self):
        proc, stopped = self.run_sh("head -c 1000000 /dev/zero | tr '\\0' x", timeout=5)
        self.assertEqual((proc.returncode, len(proc.stdout), stopped), (0, 1000000, False))

    def test_a_tool_that_cannot_start_raises_oserror(self):
        with self.assertRaises(OSError):
            gate._run_in_session(["/nonexistent/tool"], tempfile.gettempdir(), {}, timeout=1)

    def test_an_ignored_soft_stop_signal_stays_ignored_here_when_the_tool_cannot_start(self):
        before = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            with self.assertRaises(OSError):
                gate._run_in_session(["/nonexistent/tool"], tempfile.gettempdir(), {}, timeout=1,
                                             soft_stop=(signal.SIGINT, 1))
            self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGINT, before)


class TestFlakyTests(RepositoryTest):
    """The flaky-tests check over a real git repository, rerunning its tests with this suite's own pytest."""

    BASE = (
        "import os\n"                                      # 1
        "import unittest\n"                                # 2
        "\n"                                               # 3
        "# test_write_<k> adds k; test_read_<k> passes only before it.\n"  # 4
        "WRITTEN = set()\n"                                # 5
        "# A character for each run of TestRuns.test_alternates in this checkout.\n"  # 6
        "RUNS = os.path.join(os.path.dirname(__file__), 'runs')\n"  # 7
        "\n"                                               # 8
        "\n"                                               # 9
        "def test_old_reads():\n"                          # 10
        "    assert 0 not in WRITTEN\n"                    # 11
        "\n"                                               # 12
        "\n"                                               # 13
        "class TestRuns(unittest.TestCase):\n"             # 14
        "    def test_alternates(self):\n"                 # 15
        "        self.skipTest('not yet')\n"               # 16
        "        with open(RUNS, 'a') as f:\n"             # 17
        "            f.write('x')\n"                       # 18
        "        with open(RUNS) as f:\n"                  # 19
        "            self.assertEqual(len(f.read()) % 2, 1)\n"  # 20
    )
    # The reader and writer pairs change_the_tests adds.
    PAIRS = 8

    BASE_FILES = {"tests/test_state.py": BASE, "calc.py": "def add(a, b):\n    return a + b\n"}

    def found(self):
        base = self.git("rev-parse", "HEAD").strip()
        return gate.flaky_tests(gate.GateChange.from_tree(self.repo, base))

    def change_the_tests(self, extra=""):
        """Edit TestRuns.test_alternates to fail on every second run, whatever the order, and add PAIRS writers and
        readers, a stable test, a broken one and extra; returns the new text of the test file."""
        text = self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)")
        for k in range(self.PAIRS):
            text += (f"\n\ndef test_write_{k}():\n    WRITTEN.add({k})\n"
                     f"\n\ndef test_read_{k}():\n    assert {k} not in WRITTEN\n")
        text += ("\n\ndef test_stable():\n    assert WRITTEN == WRITTEN\n"
                 "\n\ndef test_broken():\n    assert False\n" + extra)
        self.put("tests/test_state.py", text)
        return text

    @staticmethod
    def line_of(text, definition):
        return text.splitlines().index(definition) + 1

    def test_a_server_a_test_leaves_holding_the_output_does_not_hold_up_the_reruns(self):
        # Under -s a test's processes inherit pytest's own output; this one outlives every run, in a session of its own.
        pids = os.path.join(self.outside, "servers")

        def started_servers():
            with contextlib.suppress(FileNotFoundError), open(pids) as f:
                return [int(pid) for pid in f.read().split()]
            return []

        def kill():
            for pid in started_servers():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(FileNotFoundError):
                os.remove(pids)
        self.addCleanup(kill)
        self.put("pytest.ini", "[pytest]\naddopts = -s\n")
        self.git("add", "pytest.ini")
        self.git("commit", "--quiet", "-m", "-s")
        self.put("tests/test_server.py", (
            "import subprocess\nimport sys\n\n\ndef test_starts_a_server():\n"
            "    server = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], "
            "start_new_session=True)\n"
            f"    with open({pids!r}, 'a') as f:\n        f.write(f'{{server.pid}}\\n')\n"))
        self.git("add", "tests/test_server.py")
        started = time.monotonic()
        with patch.object(gate, "TOOL_KILL_WAIT", 0.5):
            found = self.found()
        self.assertLess(time.monotonic() - started, gate.FLAKY_RUN_TIMEOUT)
        self.assertEqual(found, [])
        # One server for each seed's run, so the reruns did happen.
        self.assertEqual(len(started_servers()), len(gate.FLAKY_SEEDS))

    @staticmethod
    def alternates():
        """The finding's message on TestRuns.test_alternates, which fails on the second and fourth run."""
        s = gate.FLAKY_SEEDS
        return (f"`TestRuns.test_alternates` failed under seeds {s[1]} and {s[3]} and passed under seeds {s[0]}, "
                f"{s[2]} and {s[4]}: it depends on the order the tests run in, or on state or randomness that one run "
                f"does not share with another.")

    def test_an_order_dependent_test_is_reported_with_the_seeds_it_failed_under(self):
        text = self.change_the_tests()
        found = self.found()
        by_line = {f["line"]: f for f in found}
        self.assertEqual({(f["path"], f["severity"]) for f in found}, {("tests/test_state.py", "warning")})
        # test_alternates fails on the second and fourth run, in any order of the tests.
        self.assertEqual(by_line.pop(15)["message"], self.alternates())
        # test_broken fails under every seed: not flaky, but reported, since the copy may be what it lacks.
        self.assertEqual(by_line.pop(self.line_of(text, "def test_broken():"))["rule"], "failed-every-run")
        # Each seed orders each pair one way or the other. Five seeds that split none of eight independent pairs
        # have a chance of 16 ** -8 under a uniform shuffle, so some reader is reported whatever pytest-randomly's
        # version. No writer is, nor test_stable. test_old_reads depends on test_write_0 as test_read_0 does, but
        # the change left it alone, so it is not rerun.
        self.assertEqual({f["rule"] for f in by_line.values()}, {"mixed-results"})
        readers = {self.line_of(text, f"def test_read_{k}():"): k for k in range(self.PAIRS)}
        self.assertTrue(by_line)
        self.assertLessEqual(set(by_line), set(readers))
        for line, f in by_line.items():
            m = re.fullmatch(rf"`test_read_{readers[line]}` failed under seeds? ([\d, and]+) and passed under seeds? "
                             r"([\d, and]+): .+", f["message"])
            self.assertIsNotNone(m, f["message"])
            failed, passed = ({int(s) for s in re.findall(r"\d+", g)} for g in m.groups())
            self.assertTrue(failed and passed)
            self.assertEqual(failed | passed, set(gate.FLAKY_SEEDS))

    def test_the_project_s_verbosity_does_not_hide_the_tests(self):
        self.change_the_tests()
        # Two seeds are enough for test_alternates to fail under one, and keep this test quick.
        seeds = patch.object(gate, "FLAKY_SEEDS", (1, 2))
        for ini, env in (("addopts = -v", ""), ("addopts = -qq", ""), ("", "-q"), ("verbosity_test_cases = 2", "")):
            with self.subTest(ini=ini, env=env), seeds, patch.dict(os.environ, {"PYTEST_ADDOPTS": env}):
                self.put("pytest.ini", f"[pytest]\n{ini}\n")
                self.git("add", "pytest.ini")
                self.assertIn("`TestRuns.test_alternates` failed under seed 2 and passed under seed 1: ",
                              "\n".join(f["message"] for f in self.found()))

    def fake_python(self, script):
        """Make the gate's interpreter a shell script that runs script, whatever it is asked."""
        fake = os.path.join(self.repo, "fake-python")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\n{script}\n")
        os.chmod(fake, 0o700)
        p = patch.object(gate.sys, "executable", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_a_collection_the_gate_cannot_read_is_a_finding(self):
        self.change_the_tests()
        self.fake_python("printf 'tests/test_state.py: 20\\n\\n20 tests collected\\n'")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertIn("printed no node id the gate could read: 20 tests collected", found[0]["message"])

    def test_a_test_missing_from_a_run_s_report_is_not_counted_as_passed(self):
        # --maxfail=1 ends each run at its first failure, so of four tests that always fail, each run reports one.
        # None is flaky; some must be reported under fewer than two seeds, as five runs report five failures in all.
        # One that two or more runs failed is reported as failing under every seed that judged it.
        self.put("pytest.ini", "[pytest]\naddopts = --maxfail=1\n")
        self.git("add", "pytest.ini")
        text = self.BASE + "".join(f"\n\ndef test_fails_{k}():\n    assert False\n" for k in range(4))
        self.put("tests/test_state.py", text)
        found = self.found()
        self.assertIn("not-rerun", {f["rule"] for f in found})
        self.assertLessEqual({f["rule"] for f in found}, {"not-rerun", "failed-every-run"})
        lines = {self.line_of(text, f"def test_fails_{k}():") for k in range(4)}
        for f in (f for f in found if f["rule"] == "not-rerun"):
            self.assertIn(f["line"], lines)
            self.assertRegex(f["message"], r"^`test_fails_\d` (failed under seed \d only|neither passed nor failed "
                                           r"under any seed); the reports of seeds? [\d, and]+ have it skipped or not "
                                           r"at all, so too few runs tell whether it is flaky\.$")

    def test_a_test_skipped_under_some_seeds_is_judged_by_the_others(self):
        # Skipped on the third run, failing on the second and fourth: the third is neither a pass nor a failure.
        text = self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)").replace(
            "            self.assertEqual(len(f.read()) % 2, 1)\n",
            "            runs = len(f.read())\n"
            "        if runs == 3:\n"
            "            self.skipTest('the third run')\n"
            "        self.assertEqual(runs % 2, 1)\n")
        self.put("tests/test_state.py", text)
        s = gate.FLAKY_SEEDS
        self.assertEqual([f["message"] for f in self.found()], [(
            f"`TestRuns.test_alternates` failed under seeds {s[1]} and {s[3]} and passed under seeds {s[0]} and "
            f"{s[4]}, and was skipped or not run under seed {s[2]}: it depends on the order the tests run in, or on "
            f"state or randomness that one run does not share with another.")])

    def test_a_test_that_only_lost_lines_is_rerun(self):
        self.put("tests/test_state.py", self.BASE.replace("        self.skipTest('not yet')\n", ""))
        self.assertEqual([(f["line"], f["message"]) for f in self.found()], [(15, self.alternates())])

    def test_a_decorator_removed_above_the_others_reruns_its_test(self):
        decorated = self.BASE.replace("        self.skipTest('not yet')\n", "").replace(
            "    def test_alternates(self):\n",
            "    @unittest.skip('not yet')\n    @unittest.expectedFailure\n    def test_alternates(self):\n")
        self.put("tests/test_state.py", decorated)
        self.git("commit", "--quiet", "-am", "decorated")
        self.put("tests/test_state.py", decorated.replace("    @unittest.skip('not yet')\n", "").replace(
            "    @unittest.expectedFailure\n", ""))
        # Two seeds are enough for test_alternates to fail under one, and keep this test quick.
        with patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            self.assertEqual([(f["line"], f["rule"]) for f in self.found()], [(15, "mixed-results")])

    def test_a_test_file_whose_name_holds_a_line_end_is_rerun(self):
        # A carriage return breaks text mode's reading of pytest's list of node ids, and U+2028 str.splitlines'.
        old = "tests/test_state.py"
        for name in ("tests/test_st\u2028ate.py", "tests/test_st\rate.py"):
            with self.subTest(name=name), patch.object(gate, "FLAKY_SEEDS", (1, 2)):
                self.git("mv", old, name)
                self.git("commit", "--quiet", "-m", "renamed")
                old = name
                self.put(name, self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)"))
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()],
                                 [(name, 15, "mixed-results")])
                self.git("checkout", "--", name)

    # A module-level test like TestRuns.test_alternates, failing on every second run; line 9 is its def.
    ALTERNATING = ("import os\n"
                   "\n"
                   "import pytest\n"
                   "\n"
                   "RUNS = os.path.join(os.path.dirname(__file__), 'runs')\n"
                   "\n"
                   "\n"
                   "@pytest.mark.parametrize('x', ['a::b'])\n"
                   "def test_p(x):\n"
                   "    with open(RUNS, 'a') as f:\n"
                   "        f.write('x')\n"
                   "    with open(RUNS) as f:\n"
                   "        assert len(f.read()) % 2 == 1\n")

    def test_a_parameter_id_that_holds_colons_is_judged(self):
        self.put("tests/test_p.py", self.ALTERNATING)
        self.git("add", "tests/test_p.py")
        with patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("tests/test_p.py", 9, "mixed-results")])
        self.assertTrue(found[0]["message"].startswith("`test_p[a::b]` failed under seed 2 and passed under seed 1: "))

    def decorated(self, old_above, new_above):
        """The lines of test_r's def found on a change from old_above to new_above, the lines above it."""
        head = "import os\n\nimport pytest\n\nRUNS = os.path.join(os.path.dirname(__file__), 'runs')\n\n\n"
        body = ("def test_r():\n    with open(RUNS, 'a') as f:\n        f.write('x')\n"
                "    with open(RUNS) as f:\n        assert len(f.read()) % 2 == 1\n")
        self.put("tests/test_r.py", head + old_above + body)
        self.git("add", "tests/test_r.py")
        self.git("commit", "--quiet", "-m", "test_r")
        self.put("tests/test_r.py", head + new_above + body)
        with patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            return [(f["path"], f["line"], f["rule"]) for f in self.found()]

    def test_a_decorator_replaced_by_a_comment_reruns_its_test(self):
        self.assertEqual(self.decorated("@pytest.mark.skip(reason='alternates, #12')\n", "# Was skipped for #12.\n"),
                         [("tests/test_r.py", 9, "mixed-results")])

    def test_a_decorator_removed_above_a_comment_reruns_its_test(self):
        self.assertEqual(self.decorated("@pytest.mark.skip(reason='#12')\n# Alternates between runs.\n",
                                        "# Alternates between runs.\n"),
                         [("tests/test_r.py", 9, "mixed-results")])

    def test_a_test_that_xfails_under_every_seed_is_consistent(self):
        old = "import pytest\n\n\n@pytest.mark.xfail(strict=True, reason='bug 12')\ndef test_a():\n    assert 1 == 2\n"
        self.put("tests/test_x.py", old)
        self.git("add", "tests/test_x.py")
        self.git("commit", "--quiet", "-m", "xfail")
        self.put("tests/test_x.py", old.replace("1 == 2", "1 == 3"))
        self.assertEqual(self.found(), [])

    def test_a_test_that_xfails_under_some_seeds_and_passes_under_others_is_mixed(self):
        self.assertEqual(self.decorated("", "@pytest.mark.xfail(reason='#12')\n"),
                         [("tests/test_r.py", 9, "mixed-results")])
        with patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            [found] = self.found()
        self.assertTrue(found["message"].startswith("`test_r` failed as its xfail mark expects under seed 2 and passed "
                                                    "under seed 1: "), found["message"])

    def test_a_test_file_whose_name_is_not_utf8_is_rerun(self):
        name = os.fsdecode(b"tests/test_\xff.py")
        self.git("mv", "tests/test_state.py", name)
        self.git("commit", "--quiet", "-m", "renamed")
        self.put(name, self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)"))
        with patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            found = self.found()
        if [f["rule"] for f in found] == ["not-rerun"]:
            # pytest collects and lists the test, but pytest-randomly 5.0.0 hashes each node id as strict UTF-8
            # (pytest_randomly._crc32), so the first rerun stops with an internal error, which the finding quotes.
            self.assertRegex(found[0]["message"], r"^pytest, rerunning them under seed 1, exited with status 3: "
                                                  r"UnicodeEncodeError: .*surrogates not allowed; the changed tests "
                                                  r"were not rerun\.$")
        else:
            self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(name, 15, "mixed-results")])

    def test_no_config_or_conftest_above_the_copy_is_loaded(self):
        # Another account can write the directory the copy is made in; pytest must not take its pytest.ini, which
        # would make that directory confcutdir, nor so import its conftest.py.
        shared = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, shared)
        pwned = os.path.join(shared, "PWNED")
        with open(os.path.join(shared, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        with open(os.path.join(shared, "conftest.py"), "w") as f:
            f.write(f"with open({pwned!r}, 'a') as f:\n    f.write('conftest ran\\n')\n")
        self.put("tests/test_state.py", self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)"))
        with patch.object(tempfile, "tempdir", shared), patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            self.assertEqual([(f["line"], f["rule"]) for f in self.found()], [(15, "mixed-results")])
        self.assertFalse(os.path.exists(pwned))

    def test_the_project_s_pyproject_toml_configures_the_reruns(self):
        # --maxfail=1 in the project's own table leaves each of four always failing tests judged by few runs.
        text = self.BASE + "".join(f"\n\ndef test_fails_{k}():\n    assert False\n" for k in range(4))
        for table in ('[tool.pytest.ini_options]\naddopts = "--maxfail=1"\n',
                      '[tool.pytest]\naddopts = ["--maxfail=1"]\n'):
            with self.subTest(table=table):
                self.put("pyproject.toml", f"[project]\nname = 'p'\n\n{table}")
                self.git("add", "pyproject.toml")
                self.put("tests/test_state.py", text)
                # Without the table, each of them would fail under every seed, and none would be judged too seldom.
                self.assertIn("not-rerun", {f["rule"] for f in self.found()})

    def test_the_project_s_pytest_config_is_found_as_pytest_finds_it(self):
        cases = [
            ({}, None),
            ({"pyproject.toml": "[project]\nname = 'p'\n", "tox.ini": "[tox]\n", "setup.cfg": "[metadata]\n"}, None),
            ({"pyproject.toml": "[tool.pytest]\n"}, None),
            ({"pyproject.toml": "[tool.pytest.ini_options]\n", "pytest.ini": ""}, "pytest.ini"),
            ({"pyproject.toml": "[tool.pytest.ini_options]\n", "tox.ini": "[pytest]\n"}, "pyproject.toml"),
            ({"pyproject.toml": "[tool.pytest]\naddopts = []\n"}, "pyproject.toml"),
            ({"tox.ini": "[pytest]\n", "setup.cfg": "[tool:pytest]\n"}, "tox.ini"),
            ({"tox.ini": "[tool:pytest]\n", "setup.cfg": "[tool:pytest]\n"}, "setup.cfg"),
            ({".pytest.ini": "", "pytest.toml": ""}, "pytest.toml"),
            ({"pyproject.toml": "not toml ["}, "pyproject.toml"),
        ]
        for files, expected in cases:
            with self.subTest(files=files), tempfile.TemporaryDirectory() as tree:
                for name, text in files.items():
                    with open(os.path.join(tree, name), "w") as f:
                        f.write(text)
                found = gate._pytest_inifile(tree)
                self.assertEqual(found and os.path.basename(found), expected)

    def test_a_configuration_below_the_top_configures_the_tests_under_it(self):
        # tests/pytest.ini puts tests/src on the path, which the changed test needs to import; and the alternating
        # test from BASE shows it was rerun and judged.
        self.put("tests/pytest.ini", "[pytest]\npythonpath = src\n")
        self.put("tests/src/helper.py", "VALUE = 1\n")
        self.git("add", "tests/pytest.ini", "tests/src/helper.py")
        self.git("commit", "--quiet", "-m", "nested config")
        self.put("tests/test_state.py", "import helper\n" + self.BASE.replace("self.skipTest('not yet')",
                                                                          "self.assertEqual(helper.VALUE, 1)"))
        with patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [("tests/test_state.py", 16, "mixed-results")])

    def test_tests_under_two_configurations_are_rerun_once_by_each(self):
        for name, text in (("a/pyproject.toml", "[tool.pytest.ini_options]\n"), ("b/tox.ini", "[pytest]\n")):
            self.put(name, text)
            self.put(f"{os.path.dirname(name)}/test_{name[0]}.py", "def test_old():\n    assert True\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "two configs")
        for where in ("a", "b"):
            self.put(f"{where}/test_{where}.py", "def test_old():\n    assert True\n\n\ndef test_new():\n    assert True\n")
        runs = []
        real = gate._pytest

        def recording(setup, doing, args, **kwargs):
            runs.append((os.path.relpath(setup.root, setup.top), os.path.relpath(setup.inifile, setup.top), doing))
            return real(setup, doing, args, **kwargs)
        with patch.object(gate, "_pytest", recording), patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            self.assertEqual(self.found(), [])
        self.assertEqual(sorted({r[:2] for r in runs}), [("a", "a/pyproject.toml"), ("b", "b/tox.ini")])
        self.assertEqual(sorted(r[0] for r in runs if r[2] == "collecting the changed tests"), ["a", "b"])

    def test_a_group_s_label_names_its_configuration_or_its_lack_of_one(self):
        top = "/w/tree"

        def group(inifile):
            return gate._PytestGroup(gate._PytestSetup(top, inifile, top), frozenset())
        self.assertEqual(group("/w/pytest.ini").label(), "the changed tests with no pytest configuration")
        self.assertEqual(group("/w/tree/..ci/pytest.ini").label(), "the changed tests configured by ..ci/pytest.ini")
        self.assertEqual(group("/w/tree/b/tox.ini").label(), "the changed tests configured by b/tox.ini")

    def failing_in_the_copy(self, base, changed, extra=None):
        """The findings on tests/test_g.py changed from base to changed, with extra files beside it untracked."""
        self.put("tests/test_g.py", base)
        self.git("add", "tests/test_g.py")
        self.git("commit", "--quiet", "-m", "test_g")
        for name, text in (extra or {}).items():
            self.put(name, text)
        self.put("tests/test_g.py", changed)
        with patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            return self.found()

    def test_a_test_that_needs_git_s_metadata_fails_every_run_in_the_copy(self):
        test = ("import os\nimport subprocess\n\n\ndef test_head():\n"
                "    subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, cwd=os.path.dirname(__file__))\n")
        [found] = self.failing_in_the_copy(test.replace("check=True", "check=False"), test)
        self.assertEqual((found["path"], found["line"], found["severity"], found["rule"]),
                         ("tests/test_g.py", 5, "warning", "failed-every-run"))
        self.assertEqual(found["message"],
                         "`test_head` failed under seeds 1 and 2, every rerun, which ran in a copy of the tracked "
                         "files. If the build failed it too, the failing tests list it. If the build passed it, it "
                         "needs something the copy lacks, such as git's metadata, an untracked or ignored file or a "
                         "build product, so whether it is flaky could not be judged here.")

    def test_a_test_that_needs_an_ignored_file_fails_every_run_in_the_copy(self):
        test = ("import os\n\n\ndef test_settings():\n"
                "    assert os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'settings.local'))\n")
        found = self.failing_in_the_copy(test.replace("assert ", "assert True or "), test,
                                         {".gitignore": "settings.local\n", "settings.local": "x = 1\n"})
        self.assertEqual([(f["line"], f["rule"]) for f in found], [(4, "failed-every-run")])

    def stopped_at(self, seed, extra):
        """The findings on the change that adds extra to the test file, when the rerun under seed times out."""
        self.put("tests/test_state.py", self.BASE + extra)
        rerun = gate._rerun_python_tests

        def times_out(setup, ids, s, report):
            if s == seed:
                return {}, f"pytest, rerunning them under seed {seed}, did not finish within 30 seconds"
            return rerun(setup, ids, s, report)
        with patch.object(gate, "_rerun_python_tests", times_out):
            return {(f["line"], f["rule"]): f["message"] for f in self.found()}

    def test_a_test_one_run_failed_before_the_next_went_wrong_is_not_failing_every_run(self):
        found = self.stopped_at(2, "\n\ndef test_one():\n    assert False\n")
        self.assertEqual(found, {
            (23, "not-rerun"): "`test_one` failed under seed 1 only; the reruns stopped when the run under seed 2 "
                               "went wrong, so too few runs tell whether it is flaky.",
            (None, "not-rerun"): "pytest, rerunning them under seed 2, did not finish within 30 seconds; only the "
                                 "reruns under seed 1 were judged."})

    def test_a_test_the_runs_before_a_stop_skipped_is_judged_by_too_few(self):
        found = self.stopped_at(3, "\n\nimport pytest\n\n\ndef test_skipped():\n    pytest.skip('x')\n"
                                   "\n\ndef test_fails():\n    assert False\n")
        self.assertEqual(set(found), {(None, "not-rerun"), (26, "not-rerun"), (30, "failed-every-run")})
        self.assertEqual(found[(26, "not-rerun")],
                         "`test_skipped` neither passed nor failed under any seed; the reports of seeds 1 and 2 have "
                         "it skipped or not at all; the reruns stopped when the run under seed 3 went wrong, so too "
                         "few runs tell whether it is flaky.")

    def test_a_change_to_no_test_reruns_nothing(self):
        self.put("calc.py", "def add(a, b):\n    return b + a\n")
        self.put("tests/test_state.py", self.BASE.replace("import unittest\n", "import unittest  # edited\n"))
        with patch.object(gate, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def test_mutmut_s_copy_of_a_test_is_not_rerun(self):
        # mutmut copies the tests into mutants/ beside the mutated code; committed, they are its state, not tests.
        self.put("mutants/tests/test_state.py", self.BASE + "\n\ndef test_new():\n    assert True\n")
        self.git("add", "mutants/tests/test_state.py")
        with patch.object(gate, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def test_a_rerun_that_cannot_start_is_a_finding(self):
        self.change_the_tests()
        with patch.object(gate.sys, "executable", os.path.join(self.repo, "no-python")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertIn("could not start pytest", found[0]["message"])

    def test_a_rerun_that_times_out_is_a_finding(self):
        self.change_the_tests()
        self.fake_python("exec sleep 30")
        with patch.object(gate, "FLAKY_RUN_TIMEOUT", 1):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertIn("did not finish within 1 seconds", found[0]["message"])

    def test_no_room_for_the_reruns_is_a_finding(self):
        self.change_the_tests()
        with patch.object(gate.tempfile, "mkdtemp", side_effect=OSError(28, "No space left on device")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"], f["message"]) for f in found], [(
            None, None, "not-rerun", ("could not make a directory to rerun the changed tests in: [Errno 28] No space "
                                      "left on device; nothing ran, so the check judged nothing."))])

    def test_the_reruns_leave_the_build_s_reports_alone(self):
        # The project's addopts write a coverage report, as the build's own run did before the gate reads it.
        self.put("pytest.ini", "[pytest]\naddopts = --cov=calc --cov-report=xml\n")
        self.git("add", "pytest.ini")
        self.git("commit", "--quiet", "-m", "coverage")
        self.put("coverage.xml", "the build's report\n")
        self.put("tests/test_state.py", self.BASE.replace("self.skipTest('not yet')", "self.assertIsNotNone(RUNS)"))
        with patch.object(gate, "FLAKY_SEEDS", (1, 2)):
            self.assertEqual([(f["line"], f["rule"]) for f in self.found()], [(15, "mixed-results")])
        with open(os.path.join(self.repo, "coverage.xml")) as f:
            self.assertEqual(f.read(), "the build's report\n")
        self.assertEqual(sorted(os.listdir(self.repo)), [".git", "calc.py", "coverage.xml", "pytest.ini", "tests"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.repo, "tests"))), ["test_state.py"])

    def test_tracked_files_that_cannot_be_copied_are_a_finding(self):
        self.change_the_tests()
        with patch.object(gate.shutil, "copy2", side_effect=PermissionError(13, "Permission denied")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertRegex(found[0]["message"], r"^could not copy the tracked files to rerun the changed tests in: .*"
                                              r"Permission denied.*; the changed tests were not rerun\.$")

    def test_copy_tracked_copies_what_git_tracks_as_it_is(self):
        self.put("tests/data/a.txt", "a\n")
        os.symlink("data/a.txt", os.path.join(self.repo, "tests", "link"))
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "data")
        self.put("tests/data/a.txt", "edited\n")
        self.put("untracked.txt", "x\n")
        os.remove(os.path.join(self.repo, "calc.py"))
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "copy")
            gate._copy_tracked(self.repo, dest)
            found = sorted(os.path.relpath(os.path.join(d, f), dest) for d, _, fs in os.walk(dest) for f in fs)
            self.assertEqual(found, ["tests/data/a.txt", "tests/link", "tests/test_state.py"])
            self.assertEqual(os.readlink(os.path.join(dest, "tests", "link")), "data/a.txt")
            with open(os.path.join(dest, "tests", "data", "a.txt")) as f:
                self.assertEqual(f.read(), "edited\n")

    def test_a_rerun_directory_left_behind_is_a_finding_beside_the_results(self):
        self.change_the_tests()
        rmtree = shutil.rmtree

        def refuse(path):
            self.addCleanup(rmtree, path)
            raise PermissionError(13, "Permission denied", path)
        with patch.object(gate, "FLAKY_SEEDS", (1, 2)), patch.object(gate.shutil, "rmtree", refuse):
            found = self.found()
        self.assertIn((15, "mixed-results"), [(f["line"], f["rule"]) for f in found])
        [left] = [f for f in found if f["rule"] == "not-removed"]
        self.assertEqual((left["path"], left["line"]), (None, None))
        self.assertRegex(left["message"], r"^could not remove the directory to rerun the changed tests in, `.+`: "
                                          r".*Permission denied")

    def test_a_test_file_that_does_not_collect_is_a_finding(self):
        self.change_the_tests()
        with open(os.path.join(self.repo, "tests/test_state.py"), "a") as f:
            f.write("\nimport no_such_module\n")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-rerun")])
        self.assertIn("exited with status 2", found[0]["message"])

    def test_an_unparsable_test_file_is_a_finding(self):
        self.put("tests/test_state.py", self.BASE + "def test_new(:\n")
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()],
                         [("tests/test_state.py", None, "not-rerun")])

    def test_it_reports_without_failing_the_gate(self):
        [check] = [c for c in gate.gate_checks(gate.MUTATION_BUDGET) if c.name == "flaky-tests"]
        self.assertEqual((check.fails, check.find), (False, gate.flaky_tests))


class TestPassesAtBase(RepositoryTest):
    """The passes-at-base check over a real git repository, running its tests with this suite's own pytest."""

    # add is wrong at the base; mul is right, and stays so.
    CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
    TESTS = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"

    BASE_FILES = {"calc.py": CALC, "tests/test_calc.py": TESTS, "pytest.ini": "[pytest]\npythonpath = .\n"}

    def found(self, budget=gate.BASE_RUN_BUDGET):
        """The check's findings on the working tree, every file in it tracked."""
        base = self.git("rev-parse", "HEAD").strip()
        self.git("add", "-A")
        return gate.passes_at_base(gate.GateChange.from_tree(self.repo, base), budget=budget)

    def fix_add(self, tests=""):
        """Fix add, and add test_add, which fails on the old add, then tests."""
        self.put("calc.py", self.CALC.replace("a - b", "a + b"))
        self.put("tests/test_calc.py", self.TESTS.replace("import mul", "import add, mul")
                 + "\n\ndef test_add():\n    assert add(2, 2) == 4\n" + tests)

    def test_no_room_for_the_run_at_the_base_is_a_finding(self):
        self.fix_add()
        with patch.object(gate.tempfile, "mkdtemp", side_effect=OSError(28, "No space left on device")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"], f["message"]) for f in found], [(
            None, None, "not-run", ("could not make a directory to run the changed tests at the base in: [Errno 28] "
                                    "No space left on device; nothing ran, so the check judged nothing."))])

    def test_a_base_run_directory_left_behind_is_a_finding_beside_the_results(self):
        self.fix_add()
        rmtree = shutil.rmtree

        def refuse(path):
            self.addCleanup(rmtree, path)
            raise PermissionError(13, "Permission denied", path)
        with patch.object(gate.shutil, "rmtree", refuse):
            found = self.found()
        [left] = found
        self.assertEqual((left["path"], left["line"], left["rule"]), (None, None, "not-removed"))
        self.assertRegex(left["message"], r"^could not remove the directory to run the changed tests at the base in, "
                                          r"`.+`: .*Permission denied")

    def test_a_test_that_fails_without_the_fix_gives_nothing(self):
        self.fix_add()
        self.assertEqual(self.found(), [])

    def test_a_configuration_below_the_top_configures_the_run_at_the_base(self):
        # The base's own configuration is tests/pytest.ini, whose pythonpath puts tests/src on the path for helper.
        self.put("tests/pytest.ini", "[pytest]\npythonpath = src\n")
        self.put("tests/src/helper.py", "ONE = 1\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "nested config")
        self.fix_add("\n\ndef test_mul_again():\n    from helper import ONE\n    assert mul(ONE, ONE) == 1\n")
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()],
                         [("tests/test_calc.py", 12, "passes-at-base")])

    def test_a_test_of_code_the_change_left_alone_passes_at_the_base(self):
        # The fixture comes from a conftest.py the change adds, so the test runs at the base only with it overlaid.
        self.put("tests/conftest.py", "import pytest\n\n\n@pytest.fixture\ndef three():\n    return 3\n")
        self.fix_add("\n\nimport pytest\n\n\n@pytest.mark.parametrize('n', [1, 2])\n"
                     "def test_mul_again(n, three):\n    assert mul(n, three) == 3 * n\n")
        self.git("add", "-A")
        status = self.git("status", "--porcelain")
        found = self.found()
        with open(os.path.join(self.repo, "tests/test_calc.py")) as f:
            line = f.read().splitlines().index("def test_mul_again(n, three):") + 1
        self.assertEqual(found, [{"path": "tests/test_calc.py", "line": line, "severity": "warning",
                                  "rule": "passes-at-base",
                                  "message": "`test_mul_again` passes without the change, so it does not test it."}])
        self.assertEqual(self.git("status", "--porcelain"), status)

    def test_a_test_of_a_function_the_change_adds_gives_nothing(self):
        # At the base, the test file cannot import sub, so pytest collects nothing from it.
        self.put("calc.py", self.CALC + "\n\ndef sub(a, b):\n    return a - b\n")
        self.put("tests/test_sub.py", "from calc import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")
        self.assertEqual(self.found(), [])

    def installed_editable(self, where, finder=False):
        """Move calc.py to where/calc.py, a directory of the repository, with a pytest.ini that puts nothing on the
        path, commit that as the base, and make the gate's interpreter a new virtual environment's Python into which
        the project is installed editable, as setuptools and hatchling write an editable install: a .pth file there
        puts the working tree's where on the path, or with finder, imports a module that appends to sys.meta_path a
        finder of calc in the working tree's where. The environment sees this suite's packages, pytest among them,
        through another .pth file. Making it takes the standard library alone, without pip, and no network: the
        suite's own environment has neither pip nor setuptools to make a real editable install with."""
        venv = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, venv)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv],  # noqa: S603
                       check=True, capture_output=True, timeout=120)
        python = os.path.join(venv, "bin", "python")
        site_dir = subprocess.run([python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],  # noqa: S603
                                  check=True, capture_output=True, text=True, timeout=60).stdout.strip()
        with open(os.path.join(site_dir, "suite.pth"), "w") as f:
            f.write(sysconfig.get_path("purelib") + "\n")
        calc = os.path.join(self.repo, where, "calc.py")
        with open(os.path.join(site_dir, "__editable___calc_finder.py"), "w") as f:
            f.write("import importlib.util\nimport sys\n\n\nclass Finder:\n    @classmethod\n"
                    "    def find_spec(cls, name, path=None, target=None):\n"
                    "        if name != 'calc':\n            return None\n"
                    f"        return importlib.util.spec_from_file_location(name, {calc!r})\n"
                    "\n\ndef install():\n    sys.meta_path.append(Finder)\n")
        with open(os.path.join(site_dir, "__editable__.calc-0.pth"), "w") as f:
            f.write("import __editable___calc_finder; __editable___calc_finder.install()\n" if finder
                    else os.path.dirname(calc) + "\n")
        os.makedirs(os.path.join(self.repo, where))
        self.git("mv", "calc.py", f"{where}/calc.py")
        self.put("pytest.ini", "[pytest]\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", f"{where} layout")
        p = patch.object(gate.sys, "executable", python)
        p.start()
        self.addCleanup(p.stop)

    def add_sub(self, where):
        """Add sub to where/calc.py, and test_sub, which calc.sub at the base cannot pass, to tests/test_calc.py."""
        self.put(f"{where}/calc.py", self.CALC + "\n\ndef sub(a, b):\n    return a - b\n")
        self.put("tests/test_calc.py", "import calc\n\n" + self.TESTS
                 + "\n\ndef test_sub():\n    assert calc.sub(3, 1) == 2\n")

    def test_an_editable_install_of_a_src_layout_imports_the_base(self):
        # The run at the base puts the copy's src/ on the path, ahead of the installed working tree's.
        self.installed_editable("src")
        self.add_sub("src")
        self.assertEqual(self.found(), [])

    def test_a_namespace_package_partly_in_the_working_tree_is_not_taken_for_one_imported_from_it(self):
        # ns has no __init__.py, so its __path__ holds the copy's src/ns and, through the editable install, the
        # working tree's; its module comes from the copy.
        self.installed_editable("src")
        self.put("src/ns/helper.py", "ONE = 1\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "namespace package")
        self.add_sub("src")
        self.put("tests/test_calc.py", "from ns import helper\n\n" + self.TESTS
                 + "\n\ndef test_mul_one():\n    assert mul(helper.ONE, 1) == 1\n")
        self.assertEqual([(f["rule"], f["message"].split("`")[1]) for f in self.found()],
                         [("passes-at-base", "test_mul_one")])

    def test_a_module_a_namespace_package_finds_in_the_working_tree_cannot_be_judged(self):
        # The change adds ns/extra.py, which the copy lacks; ns's __path__ finds it in the working tree's src/ns.
        self.installed_editable("src")
        self.put("src/ns/helper.py", "ONE = 1\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "namespace package")
        self.put("src/calc.py", self.CALC.replace("a - b", "a + b"))
        self.put("src/ns/extra.py", "TWO = 2\n")
        self.put("tests/test_calc.py", "from ns import extra\n\n" + self.TESTS
                 + "\n\ndef test_mul_two():\n    assert mul(extra.TWO, 1) == 2\n")
        self.assertEqual([(f["rule"], "`ns` (`src/ns/extra.py`)" in f["message"]) for f in self.found()],
                         [("not-run", True)])

    def test_a_package_only_a_conftest_imports_from_the_working_tree_cannot_be_judged(self):
        # The changed test reaches calc only through a fixture of the conftest.py, which imports it from lib/.
        self.installed_editable("lib")
        self.put("tests/conftest.py", "import pytest\n\n\n@pytest.fixture\ndef calc_module():\n"
                                      "    import calc\n    return calc\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "fixture")
        self.put("lib/calc.py", self.CALC + "\n\ndef sub(a, b):\n    return a - b\n")
        self.put("tests/test_sub.py", "def test_sub(calc_module):\n    assert calc_module.sub(3, 1) == 2\n")
        self.assertEqual([(f["rule"], "`calc` (`lib/calc.py`)" in f["message"]) for f in self.found()],
                         [("not-run", True)])

    def test_an_editable_install_by_an_import_finder_imports_the_base(self):
        # The finder comes after Python's own path search, which finds calc in the copy's src/ first.
        self.installed_editable("src", finder=True)
        self.add_sub("src")
        self.assertEqual(self.found(), [])

    def test_a_package_an_import_finder_takes_from_the_working_tree_cannot_be_judged(self):
        self.installed_editable("lib", finder=True)
        self.add_sub("lib")
        self.assertEqual([(f["rule"], "`calc` (`lib/calc.py`)" in f["message"]) for f in self.found()],
                         [("not-run", True)])

    def test_a_package_imported_from_the_working_tree_cannot_be_judged(self):
        # lib/ is no import root of the copy's, so calc comes from the working tree, through the editable install.
        self.installed_editable("lib")
        self.add_sub("lib")
        self.assertEqual([(f["path"], f["line"], f["rule"], f["message"]) for f in self.found()], [(
            None, None, "not-run", ("the changed tests imported `calc` (`lib/calc.py`) from the working tree, not "
                                    "from the copy of the base, as from a project installed into the gate's "
                                    "interpreter, so they were not judged."))])

    def test_a_change_to_the_tests_alone_is_not_run(self):
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_zero():\n    assert mul(0, 5) == 0\n")
        with patch.object(gate, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def fake_pytest(self, collect, run):
        """Make the gate's interpreter a shell script that runs collect for a --collect-only and run otherwise."""
        fake = os.path.join(self.repo, "fake-python")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\ncase "$*" in *--collect-only*) {collect};; *) {run};; esac\n')
        os.chmod(fake, 0o700)
        p = patch.object(gate.sys, "executable", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_a_budget_spent_on_one_configuration_says_the_later_ones_were_not_run(self):
        for where in ("a", "b"):
            self.put(f"{where}/pytest.ini", "[pytest]\n")
            self.put(f"{where}/test_{where}.py", "def test_old():\n    assert True\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "two configs")
        self.put("calc.py", self.CALC.replace("a - b", "a + b"))
        for where in ("a", "b"):
            self.put(f"{where}/test_{where}.py", "def test_old():\n    assert True\n\n\ndef test_new():\n    pass\n")
        self.fake_pytest("exec sleep 30", "exit 0")
        found = self.found(budget=1)
        self.assertEqual([(f["rule"], f["message"]) for f in found], [
            ("budget-reached", ("pytest was stopped at 1 seconds (`--base-run-budget`) while it collected the changed "
                                "tests configured by a/pytest.ini, so none of them was judged.")),
            ("not-run", ("the run at the base used its 1 seconds (`--base-run-budget`) before it reached the changed "
                         "tests configured by b/pytest.ini, so they were not run there."))])

    def test_a_run_past_the_budget_says_how_many_test_cases_ran(self):
        self.fix_add("\n\ndef test_more():\n    assert add(1, 1) == 2\n")
        # The sleep in the background holds pytest's output open too: only killing its process group ends the run.
        self.fake_pytest("printf 'tests/test_calc.py::test_add\\ntests/test_calc.py::test_more\\n'",
                         "printf '.'; sleep 30 & exec sleep 30")
        start = time.monotonic()
        found = self.found(budget=1)
        self.assertLess(time.monotonic() - start, gate.TOOL_KILL_WAIT)
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "budget-reached")])
        self.assertEqual(found[0]["message"], "pytest was stopped at 1 seconds (`--base-run-budget`) after 1 of the 2 "
                                              "test cases it was given, before it reported them, so none of them was "
                                              "judged.")

    def test_a_collection_past_the_budget_is_a_finding(self):
        self.fix_add()
        self.fake_pytest("exec sleep 30", "exit 0")
        self.assertEqual([f["message"] for f in self.found(budget=1)], [(
            "pytest was stopped at 1 seconds (`--base-run-budget`) while it collected the changed tests, so none of "
            "them was judged.")])

    def test_a_test_pytest_does_not_collect_leaves_the_others_judged(self):
        # pytest does not collect a test_* method of a class not named Test*; asked for it by id, it exits 4.
        self.fix_add("\n\nclass CalcMixin:\n    def test_x(self):\n        assert True\n"
                     "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([(f["rule"], f["message"].split("`")[1]) for f in self.found()],
                         [("passes-at-base", "test_mul_one")])

    def test_a_conftest_that_cannot_be_imported_at_the_base_gives_nothing(self):
        self.put("calc.py", self.CALC.replace("a - b", "a + b") + "\n\nTWO = 2\n")
        self.put("tests/conftest.py", "import pytest\n\nfrom calc import TWO\n\n\n@pytest.fixture\n"
                                      "def two():\n    return TWO\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_two(two):\n    assert mul(two, 1) == 2\n")
        self.assertEqual(self.found(), [])

    def test_a_conftest_that_cannot_be_imported_hides_only_the_tests_under_it(self):
        self.put("calc.py", self.CALC.replace("a - b", "a + b") + "\n\nTWO = 2\n")
        self.put("tests/b/conftest.py", "from calc import TWO\n")
        self.put("tests/b/test_two.py", "from calc import mul\n\n\ndef test_two():\n    assert mul(2, 1) == 2\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([(f["path"], f["message"].split("`")[1]) for f in self.found()],
                         [("tests/test_calc.py", "test_mul_one")])

    def test_where_the_run_at_the_base_imported_from_is_read_from_its_reports(self):
        tree = os.path.join(self.repo, "copy")
        self.put("copy/lib/calc.py", "")
        self.put("new/sub.py", "")
        self.git("add", "-A")
        change = gate.GateChange.from_tree(self.repo, self.git("rev-parse", "HEAD").strip())
        base_copy = gate._BaseCopy(change, tree, {}, 0.0, 1.0)
        reports = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, reports)
        report = os.path.join(reports, "base-0-imports.json")

        def write(path, text):
            with open(path, "w") as f:
                f.write(text)
        # Tracked at the base, as the copy shows, or added by the change: the project's, named by its top-level
        # module; a virtual environment's under the top is not. A pytest-xdist worker's report beside the
        # controller's counts too.
        write(report, '{"calc": "lib/calc.py", "new.sub": "new/sub.py", "pytest": ".venv/lib/pytest/__init__.py"}')
        write(f"{report}-gw0", '{"w": "lib/calc.py"}')
        self.assertEqual(gate._imported_from_working_tree(base_copy, report),
                         [("calc", "lib/calc.py"), ("new", "new/sub.py"), ("w", "lib/calc.py")])
        # A report that cannot be read is named, the controller's or a worker's.
        for bad in (report, f"{report}-gw0"):
            for text in ("[]", "{"):
                with self.subTest(bad=bad, text=text):
                    write(report, "{}")
                    write(f"{report}-gw0", "{}")
                    write(bad, text)
                    with self.assertRaisesRegex(gate.GateError, f"^{re.escape(bad)}: "):
                        gate._imported_from_working_tree(base_copy, report)
        os.remove(report)
        with self.assertRaisesRegex(gate.GateError, f"^{re.escape(report)}: "):
            gate._imported_from_working_tree(base_copy, report)

    def test_the_imports_plugin_writes_a_worker_s_report_beside_the_controller_s(self):
        plugin = ModuleType("plugin")
        exec(gate._BASE_IMPORTS_PLUGIN_SOURCE, plugin.__dict__)  # noqa: S102
        reports = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, reports)
        report = os.path.join(reports, "imports.json")
        options = {"gate_imports_root": os.path.dirname(gate.__file__), "gate_imports_report": report}
        # A pytest the gate runs inside an xdist worker of its own inherits PYTEST_XDIST_WORKER, but is no worker.
        with patch.dict(os.environ, {"PYTEST_XDIST_WORKER": "gw7"}):
            for config in (SimpleNamespace(getoption=options.get),
                           SimpleNamespace(getoption=options.get, workerinput={"workerid": "gw1"})):
                plugin.pytest_sessionfinish(SimpleNamespace(config=config))
        self.assertEqual(sorted(os.listdir(reports)), ["imports.json", "imports.json-gw1"])
        with open(report) as f:
            self.assertEqual(json.load(f)["gate"], "gate.py")

    def test_a_usage_error_without_a_conftest_is_a_finding(self):
        self.fix_add()
        self.fake_pytest("echo 'ERROR: usage: no such option'; exit 4", "exit 0")
        self.assertEqual([(f["rule"], f["message"]) for f in self.found()], [(
            "not-run", ("pytest, collecting the changed tests at the base, exited with status 4: ERROR: usage: no such "
                        "option; the changed tests were not run there."))])

    def test_a_conftest_the_change_does_not_edit_failing_is_a_finding(self):
        self.put("calc.py", self.CALC.replace("a - b", "a + b"))
        self.put("tests/conftest.py", "raise RuntimeError('broken')\n")
        self.git("add", "tests/conftest.py")
        self.git("commit", "--quiet", "-m", "broken conftest")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([(f["path"], f["rule"], f["message"]) for f in self.found()], [(
            None, "not-run", ("pytest, collecting the changed tests at the base, could not load `tests/conftest.py`, "
                              "which fails through no file the change edits, so the base fails there: RuntimeError: "
                              "broken; the changed tests were not run there."))])

    def test_a_conftest_failing_through_a_helper_the_change_edits_hides_only_the_tests_under_it(self):
        # The conftest.py is the base's, but the helper it imports is the working tree's, which needs calc.TWO.
        self.put("tests/b/conftest.py", "import helpers\n")
        self.put("tests/b/helpers.py", "")
        self.git("add", "tests/b")
        self.git("commit", "--quiet", "-m", "helpers")
        self.put("calc.py", self.CALC.replace("a - b", "a + b") + "\n\nTWO = 2\n")
        self.put("tests/b/helpers.py", "import calc\n\nTWO = calc.TWO\n")
        self.put("tests/b/test_two.py", "from calc import mul\n\n\ndef test_two():\n    assert mul(2, 1) == 2\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([(f["path"], f["message"].split("`")[1]) for f in self.found()],
                         [("tests/test_calc.py", "test_mul_one")])

    def test_a_conftest_that_is_a_link_the_change_edits_is_named_by_the_link(self):
        # Neither file the link may point at is the change's; the change points it at the one that needs calc.TWO.
        self.put("shared/plain.py", "")
        self.put("shared/two.py", "import calc\n\nTWO = calc.TWO\n")
        os.makedirs(os.path.join(self.repo, "tests/b"))
        os.symlink("../../shared/plain.py", os.path.join(self.repo, "tests/b/conftest.py"))
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "linked conftest")
        self.put("calc.py", self.CALC.replace("a - b", "a + b") + "\n\nTWO = 2\n")
        os.remove(os.path.join(self.repo, "tests/b/conftest.py"))
        os.symlink("../../shared/two.py", os.path.join(self.repo, "tests/b/conftest.py"))
        self.put("tests/b/test_two.py", "from calc import mul\n\n\ndef test_two():\n    assert mul(2, 1) == 2\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([(f["path"], f["message"].split("`")[1]) for f in self.found()],
                         [("tests/test_calc.py", "test_mul_one")])

    def test_a_conftest_the_change_edits_failing_otherwise_hides_only_the_tests_under_it(self):
        # At the base, calc has no TWO, so the conftest.py the change edits raises AttributeError there.
        self.put("tests/b/conftest.py", "import calc\n")
        self.git("add", "tests/b/conftest.py")
        self.git("commit", "--quiet", "-m", "conftest")
        self.put("calc.py", self.CALC.replace("a - b", "a + b") + "\n\nTWO = 2\n")
        self.put("tests/b/conftest.py", "import calc\n\nTWO = calc.TWO\n")
        self.put("tests/b/test_two.py", "from calc import mul\n\n\ndef test_two():\n    assert mul(2, 1) == 2\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([(f["path"], f["message"].split("`")[1]) for f in self.found()],
                         [("tests/test_calc.py", "test_mul_one")])

    def test_a_failing_root_conftest_costs_one_collection(self):
        self.put("calc.py", self.CALC.replace("a - b", "a + b") + "\n\nTWO = 2\n")
        self.put("conftest.py", "import calc\n\nTWO = calc.TWO\n")
        for name in ("a", "b", "c"):
            self.put(f"tests/test_{name}.py", "from calc import mul\n\n\ndef test_one():\n    assert mul(1, 1) == 1\n")
        with patch.object(gate, "_pytest", wraps=gate._pytest) as pytest_run:
            self.assertEqual(self.found(), [])
        self.assertLessEqual(pytest_run.call_count, 2)

    def test_a_failing_conftest_above_none_of_the_changed_tests_is_a_finding(self):
        # Real pytest fails only at a conftest.py above a file it was given; a fake one names another, which ends
        # the collection rather than repeating it.
        self.put("elsewhere/conftest.py", "")
        self.fix_add()
        self.fake_pytest("echo \"ImportError while loading conftest 'elsewhere/conftest.py'.\"; "
                         "echo 'E   AttributeError: x'; exit 4", "exit 0")
        self.assertEqual([(f["rule"], f["message"]) for f in self.found()], [(
            "not-run", ("pytest, collecting the changed tests at the base, could not load `elsewhere/conftest.py`, "
                        "above none of the changed tests: AttributeError: x; the changed tests were not run there."))])

    def add_submodule(self):
        """Commit, as the base, a submodule at vendor/lib whose helper.py defines ONE, on pytest's path; then fix add,
        and add test_mul_again, which imports ONE from it and passes at the base."""
        lib = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, lib)
        self.host.git(lib, "init", "--quiet", "-b", "main")
        with open(os.path.join(lib, "helper.py"), "w") as f:
            f.write("ONE = 1\n")
        self.host.git(lib, "add", "helper.py")
        self.host.git(lib, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                      "commit", "--quiet", "-m", "lib")
        self.git("-c", "protocol.file.allow=always", "submodule", "--quiet", "add", lib, "vendor/lib")
        self.put("pytest.ini", "[pytest]\npythonpath = . vendor/lib\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "submodule")
        self.fix_add("\n\ndef test_mul_again():\n    from helper import ONE\n    assert mul(ONE, ONE) == 1\n")

    def test_a_test_importing_from_a_submodule_is_judged_at_the_base(self):
        self.add_submodule()
        self.assertEqual([(f["rule"], f["message"].split("`")[1]) for f in self.found()],
                         [("passes-at-base", "test_mul_again")])

    def test_a_submodule_not_initialised_is_one_finding(self):
        self.add_submodule()
        self.git("submodule", "--quiet", "deinit", "--force", "vendor/lib")
        self.assertEqual([(f["path"], f["rule"], f["message"]) for f in self.found()], [(
            None, "not-run", ("the copy of the base leaves a submodule empty, so a changed test that imports from "
                              "it, if any does, was not judged: `vendor/lib`, which is not initialised in the working "
                              "tree."))])

    def test_a_submodule_marked_export_ignore_stays_out_of_the_copy(self):
        self.add_submodule()
        self.put(".gitattributes", "vendor/lib export-ignore\n")
        self.git("add", ".gitattributes")
        self.git("commit", "--quiet", "-m", "export-ignore")
        self.assertEqual(self.found(), [])

    def test_a_submodule_without_the_base_s_commit_is_one_finding(self):
        self.add_submodule()
        # The base records a commit that the submodule's repository in the working tree never had.
        self.git("update-index", "--cacheinfo", f"160000,{'1' * 40},vendor/lib")
        self.git("commit", "--quiet", "-m", "unknown submodule commit")
        self.assertEqual([(f["path"], f["rule"], f["message"]) for f in self.found()], [(
            None, "not-run", ("the copy of the base leaves a submodule empty, so a changed test that imports from "
                              "it, if any does, was not judged: `vendor/lib`, whose repository in the working tree "
                              "does not have the base's commit 111111111111."))])

    def test_nothing_is_written_through_a_link_the_change_replaces(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        self.git("rm", "--quiet", "-r", "tests")
        os.symlink(outside, os.path.join(self.repo, "tests"))
        self.git("add", "tests")
        self.git("commit", "--quiet", "-m", "tests elsewhere")
        os.remove(os.path.join(self.repo, "tests"))
        self.fix_add("\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        # The whole test file is new to the change, so test_mul is changed too.
        self.assertEqual([f["message"].split("`")[1] for f in self.found()], ["test_mul", "test_mul_one"])
        self.assertEqual(os.listdir(outside), [])

    def test_a_pytest_that_stops_before_it_collects_is_a_finding(self):
        # pytest exits 1 for a plugin it cannot import, as for collection errors, but collects nothing.
        self.put("calc.py", self.CALC.replace("a - b", "a + b"))
        self.put("conftest.py", "pytest_plugins = ['calc_plugin']\n")
        self.put("calc_plugin.py", "")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        found = self.found()
        self.assertEqual([(f["path"], f["rule"]) for f in found], [(None, "not-run")])
        self.assertIn("pytest stopped before it collected the changed tests at the base: ", found[0]["message"])
        self.assertIn("calc_plugin", found[0]["message"])

    def test_a_directory_the_change_makes_a_link_is_replaced(self):
        self.put("tests/data/x.json", "{}\n")
        self.git("add", "tests/data")
        self.git("commit", "--quiet", "-m", "data")
        self.git("rm", "--quiet", "-r", "tests/data")
        self.put("fixtures/x.json", "{}\n")
        os.symlink("../fixtures", os.path.join(self.repo, "tests/data"))
        self.fix_add("\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([f["message"].split("`")[1] for f in self.found()], ["test_mul_one"])

    def test_a_change_to_docs_and_tests_is_not_run(self):
        self.put("TODO.md", "- nothing\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_zero():\n    assert mul(0, 5) == 0\n")
        with patch.object(gate, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def test_a_tracked_link_out_of_the_tree_does_not_stop_the_copy(self):
        os.symlink("/etc/hostname", os.path.join(self.repo, "host"))
        self.git("add", "host")
        self.git("commit", "--quiet", "-m", "link")
        self.fix_add("\n\ndef test_mul_one():\n    assert mul(1, 1) == 1\n")
        self.assertEqual([f["rule"] for f in self.found()], ["passes-at-base"])

    def test_a_run_that_cannot_start_is_a_finding(self):
        self.fix_add()
        with patch.object(gate.sys, "executable", os.path.join(self.repo, "no-python")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("could not start pytest", found[0]["message"])

    def test_an_unparsable_test_file_is_a_finding(self):
        self.put("calc.py", self.CALC.replace("a - b", "a + b"))
        self.put("tests/test_calc.py", self.TESTS + "def test_new(:\n")
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()],
                         [("tests/test_calc.py", None, "not-run")])

    def test_a_change_beside_the_tests_alone_is_not_run(self):
        # A conftest.py anywhere and any file under tests/ go with the tests, not with the code they test.
        self.put("conftest.py", "")
        self.put("tests/data/input.txt", "2 3\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_mul_zero():\n    assert mul(0, 5) == 0\n")
        with patch.object(gate, "_pytest") as pytest_run:
            self.assertEqual(self.found(), [])
        pytest_run.assert_not_called()

    def test_its_budget_is_a_whole_number_of_seconds_from_1(self):
        self.assertEqual(gate.parse_args(["--base", "main", "--base-run-budget", "7"]).base_run_budget, 7)
        self.assertEqual(gate.parse_args(["--base", "main"]).base_run_budget, gate.BASE_RUN_BUDGET)
        for flag in ("--base-run-budget", "--mutation-budget"):
            for value, error in (("0", "must be at least 1, not 0"), ("1.5", "expected a whole number of seconds")):
                with self.subTest(flag=flag, value=value), patch("sys.stderr", new_callable=io.StringIO) as err:
                    with self.assertRaises(SystemExit):
                        gate.parse_args(["--base", "main", flag, value])
                    self.assertIn(f"argument {flag}: {error}", err.getvalue())

    def test_it_reports_without_failing_the_gate_within_its_budget(self):
        with patch.object(gate, "passes_at_base", return_value=[]) as find:
            [check] = [c for c in gate.gate_checks(base_run_budget=42) if c.name == "passes-at-base"]
            check.find("change")
        self.assertFalse(check.fails)
        find.assert_called_once_with("change", budget=42)


class TestVagueNames(RepositoryTest):
    """The vague-names check over a real git repository: a base commit, then the change in its working tree."""

    BASE = "data = 1\n\n\ndef old(value):\n    return value\n"

    BASE_FILES = {"calc.py": BASE}

    def found(self):
        """(path, line, rule, message) of each finding on the working tree's change, every file in it tracked."""
        self.git("add", "-A")
        change = gate.GateChange.from_tree(self.repo, self.git("rev-parse", "HEAD").strip())
        return [(f["path"], f["line"], f["rule"], f["message"]) for f in gate.vague_names(change)]

    def test_each_vague_name_an_added_line_binds_is_found_once_at_its_first_binding(self):
        added = textwrap.dedent("""\


            class InfoManager:
                def __init__(self, raw_info, *, metadata=None, **user_data):
                    self.handle = raw_info
                    self.result = metadata


            def load(path):
                result = []
                for line_value in open(path):
                    with line_value as status:
                        data = status
                        data = data
                        result.append(data)
                return result
            """)
        self.put("calc.py", self.BASE + added)
        base = len(self.BASE.splitlines())
        found = [(path, line - base, rule, message) for path, line, rule, message in self.found()]
        self.assertEqual([(line, message.split("`")[1]) for _, line, _, message in found], [
            (4, "raw_info"), (4, "user_data"), (5, "handle"), (11, "line_value"), (12, "status"), (13, "data")])
        self.assertEqual({(path, rule) for path, _, rule, _ in found}, {("calc.py", "vague-name")})
        self.assertEqual(found[0][3], "`raw_info` could name anything; name it for what it holds.")

    def test_every_python_file_is_read_and_no_other(self):
        # notes.txt sorts before calc.py in the diff, so a check that stopped at it would not read calc.py.
        self.put("a_notes.txt", "data = 1\n")
        self.put("calc.py", self.BASE + "\n\nclass C:\n    @classmethod\n    def make(cls):\n        cls.manager = 1\n")
        self.assertEqual([(path, message.split("`")[1]) for path, _, _, message in self.found()],
                         [("calc.py", "manager")])

    def test_imports_excepts_matches_and_type_parameters_bind_names(self):
        self.put("calc.py", self.BASE + textwrap.dedent("""\


            import json as raw_data
            from os import path as status


            def parse[T_value](text: str) -> None:
                try:
                    match json.loads(text):
                        case {"k": handle, **rest_info}:
                            pass
                        case [*all_status]:
                            pass
                except ValueError as info:
                    raise
            """))
        self.assertEqual([message.split("`")[1] for _, _, _, message in self.found()],
                         # A mapping pattern's **rest is placed at the pattern's start, before the captures in it.
                         ["raw_data", "status", "T_value", "rest_info", "handle", "all_status", "info"])

    def test_an_override_s_name_and_parameters_are_its_base_s(self):
        self.put("calc.py", self.BASE + textwrap.dedent("""\


            from typing import override


            class Handler(Base):
                @override
                def handle(self, raw_data):
                    status = raw_data
                    return status
            """))
        self.assertEqual([message.split("`")[1] for _, _, _, message in self.found()], ["status"])

    def test_a_name_on_a_line_the_change_left_alone_is_not_found(self):
        self.put("calc.py", self.BASE.replace("return value", "return value + 1"))
        self.assertEqual(self.found(), [])

    def test_a_class_or_function_named_by_a_vague_word_is_found(self):
        self.put("tests/test_calc.py", "class Manager:\n    pass\n\n\ndef handle():\n    pass\n")
        self.assertEqual([(path, line) for path, line, _, _ in self.found()],
                         [("tests/test_calc.py", 1), ("tests/test_calc.py", 5)])

    def test_an_unparsable_file_is_a_finding(self):
        self.put("calc.py", self.BASE + "def broken(:\n")
        [(path, line, rule, message)] = self.found()
        self.assertEqual((path, line, rule), ("calc.py", None, "not-read"))
        self.assertEqual(message, "does not parse as Python, so its names were not read.")

    def test_it_reports_without_failing_the_gate(self):
        [check] = [c for c in gate.gate_checks() if c.name == "vague-names"]
        self.assertEqual((check.fails, check.find), (False, gate.vague_names))


class TestSurvivingMutants(RepositoryTest):
    """The surviving-mutants check over a real git repository, with this suite's own mutmut, or a fake one."""

    BASE = (
        "def checked(a, b):\n"          # 1
        "    return b + a\n"            # 2
        "\n"                            # 3
        "\n"                            # 4
        "def unchecked(a, b):\n"        # 5
        "    return b * a\n"            # 6
        "\n"                            # 7
        "\n"                            # 8
        "def untouched(a, b):\n"        # 9
        "    return a - b\n"            # 10
        "\n"                            # 11
        "\n"                            # 12
        "def orphan():\n"               # 13
        "    return 1\n"                # 14
        "\n"                            # 15
        "\n"                            # 16
        "class Box:\n"                  # 17
        "    def size(self):\n"         # 18
        "        return 3\n"            # 19
        "\n"                            # 20
        "    def other(self):\n"        # 21
        "        return 4\n"            # 22
        "\n"                            # 23
        "\n"                            # 24
        "def describe(parser):\n"       # 25
        "    parser.add_argument('--count', help='how many'\n"   # 26
        "                                     ' to make')\n"    # 27
    )
    # The repository's own [tool.mutmut] do_not_mutate_patterns, which keep mutmut off argparse's help texts.
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml"), "rb") as f:
        PATTERNS = tomllib.load(f)["tool"]["mutmut"].get("do_not_mutate_patterns", [])
    # Each test calls one function; only test_checked looks at what it returns.
    TESTS = (
        "import calc\n\n\n"
        "def test_checked():\n    assert calc.checked(2, 3) == 5\n\n\n"
        "def test_unchecked():\n    calc.unchecked(2, 3)\n\n\n"
        "def test_untouched():\n    calc.untouched(2, 3)\n\n\n"
        "def test_box():\n    calc.Box().size()\n    calc.Box().other()\n"
    )

    BASE_FILES = {
        "pyproject.toml": ('[tool.mutmut]\nsource_paths = ["calc.py"]\n'
                           'pytest_add_cli_args_test_selection = ["tests/"]\n'
                           f'do_not_mutate_patterns = {json.dumps(PATTERNS)}\n'),
        "calc.py": BASE,
        "tests/test_calc.py": TESTS,
    }

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.outside, "bin")
        os.mkdir(self.bin)

    def found(self, budget=60, rules=None, test_contexts=None):
        base = self.git("rev-parse", "HEAD").strip()
        return gate.surviving_mutants(gate.GateChange.from_tree(self.repo, base, rules), budget=budget,
                                      test_contexts=test_contexts)

    def rules(self, table):
        """A rules file outside the tree, as `gate --config` names one, with table as its [tool.mutmut]."""
        path = os.path.join(self.bin, "rules.toml")
        with open(path, "w") as f:
            f.write("[tool.mutmut]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in table.items()))
        return path

    # How change_the_functions edits each function it edits.
    EDITS = {"checked": ("b + a", "a + b"), "unchecked": ("b * a", "a * b"), "orphan": ("return 1", "return 2"),
             "Box.size": ("return 3", "return 5")}

    def change_the_functions(self, *names):
        """Edit the functions names, by default checked, unchecked, orphan and Box.size, leaving untouched and
        Box.other as they were."""
        text = self.BASE
        for name in names or self.EDITS:
            text = text.replace(*self.EDITS[name])
        self.put("calc.py", text)

    def fake_mutmut(self, run, results="", show=None, listing=None):
        """Make the gate's interpreter a fake that answers `mutmut run` with the shell code run, `results` by
        printing results, or with the shell code listing, and `show NAME` by printing show[NAME]."""
        python = os.path.join(self.bin, "python")
        shows = "".join(f"{shlex.quote(name)}) printf %s {shlex.quote(diff)};;\n"
                        for name, diff in (show or {}).items())
        # The gate starts mutmut as `python -c CODE ARGS`, so the command is the third argument.
        with open(python, "w") as f:
            f.write(f'#!/bin/sh\ncase "$3" in\nrun) {run};;\n'
                    f'results) {listing or "printf %s " + shlex.quote(results)};;\n'
                    f'show) case "$4" in\n{shows}esac;;\nesac\n')
        os.chmod(python, 0o700)
        p = patch.object(gate.sys, "executable", python)
        p.start()
        self.addCleanup(p.stop)

    def test_a_mutant_the_tests_do_not_notice_is_reported_and_a_killed_one_is_not(self):
        self.change_the_functions()
        made, found = self.found_and_made()
        # untouched and Box.other are as weakly tested as unchecked, but the change left them alone, so mutmut made
        # no mutant of them.
        self.assertEqual({re.sub(r"__mutmut_\d+$", "", m) for m in made},
                         {"calc.x_checked", "calc.x_unchecked", "calc.x_orphan", "calc.xǁBoxǁsize"})
        # unchecked's test never looks at what it returns, nor does Box.size's; checked's does.
        self.assertEqual([(f["path"], f["line"], f["severity"], f["rule"]) for f in found],
                         [("calc.py", 5, "warning", "survived"), ("calc.py", 13, "warning", "untested"),
                          ("calc.py", 18, "warning", "survived")])
        self.assertEqual(found[0]["message"], "1 of 1 mutants of `unchecked` survived its tests: line 6, "
                                              "`return a * b` became `return a / b`.")
        self.assertIn("`orphan`", found[1]["message"])
        self.assertIn("1 of 1 mutants of `Box.size` survived its tests: line 19, `return 5` became ",
                      found[2]["message"])
        self.assertEqual(self.git("status", "--porcelain", "--ignored"), " M calc.py\n")

    def test_a_source_file_with_a_byte_order_mark_is_read_as_python_reads_it(self):
        self.change_the_functions()
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            text = f.read()
        with open(os.path.join(self.repo, "calc.py"), "w", encoding="utf-8-sig") as f:
            f.write(text)
        made, found = self.found_and_made()
        # untouched and Box.other are marked to be skipped in the copy as in a file without the mark.
        self.assertEqual({re.sub(r"__mutmut_\d+$", "", m) for m in made},
                         {"calc.x_checked", "calc.x_unchecked", "calc.x_orphan", "calc.xǁBoxǁsize"})
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [("calc.py", 5, "survived"), ("calc.py", 13, "untested"), ("calc.py", 18, "survived")])

    def found_and_made(self, budget=60):
        """found(budget), and the names of the mutants mutmut made."""
        made, list_results = [], gate._mutant_statuses

        def results(*args):
            statuses, problem = list_results(*args)
            made.extend(statuses)
            return statuses, problem
        with patch.object(gate, "_mutant_statuses", results):
            return made, self.found(budget)

    def test_the_rules_mutmut_table_judges_the_change_not_the_tree_s(self):
        self.change_the_functions()
        # The change tells mutmut to leave every line alone; the rules it is judged by do not.
        with open(os.path.join(self.repo, "pyproject.toml"), "a") as f:
            f.write("do_not_mutate_patterns = ['.']\n")
        rules = self.rules({"source_paths": ["calc.py"], "pytest_add_cli_args_test_selection": ["tests/"]})
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found(rules=rules)],
                         [("calc.py", 5, "survived"), ("calc.py", 13, "untested"), ("calc.py", 18, "survived")])

    def test_a_function_outside_source_paths_is_not_mutated_and_said_so(self):
        self.put("tools/extra.py", "def extra():\n    return 1\n")
        self.git("add", "tools/extra.py")
        self.fake_mutmut(f"touch {self.bin}/ran")
        found = self.found()
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("tools/extra.py", 1, "not-mutated")])
        self.assertIn("`extra`", found[0]["message"])
        self.assertIn("outside [tool.mutmut] source_paths", found[0]["message"])

    def test_a_file_do_not_mutate_names_is_not_mutated_and_said_so(self):
        self.put("calc.py", self.BASE.replace("return 1", "return 2"))
        self.fake_mutmut(f"touch {self.bin}/ran")
        rules = self.rules({"source_paths": ["."], "do_not_mutate": ["calc*"]})
        found = self.found(rules=rules)
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 13, "not-mutated")])
        self.assertIn("do_not_mutate", found[0]["message"])

    def test_rules_that_name_no_source_paths_are_a_finding(self):
        self.change_the_functions()
        self.fake_mutmut(f"touch {self.bin}/ran")
        found = self.found(rules=self.rules({"also_copy": ["docs/"]}))
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("source_paths", found[0]["message"])

    def test_a_function_mutmut_does_not_mutate_is_said_so(self):
        # mutmut 3.8.0 mutates no decorated function but one decorated with @staticmethod or @classmethod alone, and
        # never __new__, __setattr__ or __getattribute__.
        text = (self.BASE + "\n\n"
                "import functools\n\n\n"
                "@functools.cache\n"
                "def cached(a):\n    return a + 1\n\n\n"
                "class Made:\n"
                "    def __new__(cls):\n        return super().__new__(cls)\n\n"
                "    @staticmethod\n"
                "    def make():\n        return 1\n")
        self.put("calc.py", text)
        lines = text.split("\n")
        # mutmut is run for Made.make, the one function it mutates, and lists its mutant.
        self.fake_mutmut(f"touch {self.bin}/ran", results="    calc.xǁMadeǁmake__mutmut_1: killed\n")
        found = self.found()
        self.assertTrue(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [("calc.py", lines.index("def cached(a):") + 1, "not-mutated"),
                          ("calc.py", lines.index("    def __new__(cls):") + 1, "not-mutated")])
        self.assertIn("`cached` was not mutation-tested: mutmut does not mutate a decorated function",
                      found[0]["message"])
        self.assertIn("`Made.__new__` was not mutation-tested: mutmut never mutates `__new__`", found[1]["message"])

    def test_a_change_to_a_decorated_function_alone_runs_nothing_and_says_so(self):
        self.put("calc.py", self.BASE + "\n\n@property\ndef shown(self):\n    return 1\n")
        self.fake_mutmut(f"touch {self.bin}/ran")
        self.assertEqual([(f["path"], f["rule"]) for f in self.found()], [("calc.py", "not-mutated")])
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))

    def test_the_budget_counts_tried_mutants_as_each_function_does(self):
        self.change_the_functions("checked", "orphan", "unchecked")
        self.fake_mutmut("exec sleep 30", results="    calc.x_checked__mutmut_1: killed\n"
                                                  "    calc.x_orphan__mutmut_1: no tests\n"
                                                  "    calc.x_unchecked__mutmut_1: not checked\n")
        found = self.found(budget=1)
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "budget-reached"), ("calc.py", 13, "untested")])
        self.assertTrue(found[0]["message"].startswith("budget reached after 1 of 3 mutants"), found[0]["message"])

    def test_a_directory_for_the_copy_that_cannot_be_made_is_a_finding(self):
        self.change_the_functions()
        with patch.object(gate.tempfile, "mkdtemp", side_effect=OSError(28, "No space left on device")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"], f["message"]) for f in found], [(
            None, None, "not-run", ("could not make a directory to run mutmut on a copy of the tree in: [Errno 28] "
                                    "No space left on device; nothing ran, so the check judged nothing."))])

    def test_a_copy_that_cannot_be_removed_is_a_finding_beside_the_others(self):
        self.change_the_functions("orphan")
        self.fake_mutmut("exit 0", results="    calc.x_orphan__mutmut_1: no tests\n")
        rmtree = shutil.rmtree
        with patch.object(gate.shutil, "rmtree", side_effect=OSError(16, "Device or resource busy")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "not-removed"), ("calc.py", 13, "untested")])
        self.assertRegex(found[0]["message"], r"^could not remove the directory to run mutmut on a copy of the tree "
                                              r"in, `.+`: .*Device or resource busy")
        left = re.search(r"`([^`]+)`", found[0]["message"]).group(1)
        self.assertTrue(os.path.isdir(left))
        rmtree(left)

    def helper_holding_the_output(self):
        """Shell code that starts a process in a session of its own, which keeps the fake mutmut's output open after
        mutmut's process group is killed, and that the test kills when it ends."""
        pid = os.path.join(self.bin, "helper.pid")

        def kill():
            with contextlib.suppress(FileNotFoundError, ProcessLookupError), open(pid) as f:
                os.kill(int(f.read()), signal.SIGKILL)
        self.addCleanup(kill)
        code = f"import os, time; os.setsid(); open({pid!r}, 'w').write(str(os.getpid())); time.sleep(30)"
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} &"

    def test_a_run_whose_output_outlives_it_ends_within_the_stated_bound(self):
        self.change_the_functions()
        # mutmut ignores SIGINT here, so it is killed after the grace, and a helper keeps its output open after that.
        self.fake_mutmut(f"trap '' INT; {self.helper_holding_the_output()} exec sleep 30")
        started = time.monotonic()
        with patch.multiple(gate, MUTMUT_STOP_GRACE=0.5, TOOL_KILL_WAIT=0.5):
            found = self.found(budget=1)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual([f["rule"] for f in found], ["budget-reached"])

    def test_the_report_ends_within_its_time_whatever_mutmut_does(self):
        self.change_the_functions()
        self.fake_mutmut("exit 0", listing="trap '' INT; exec sleep 30")
        started = time.monotonic()
        # Without its stop counted in, the listing would get all 4 seconds, and then the grace on top.
        with patch.multiple(gate, MUTMUT_REPORT_TIMEOUT=4, MUTMUT_STOP_GRACE=1.5, TOOL_KILL_WAIT=0.5):
            found = self.found()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([f["rule"] for f in found], ["not-run"])

    def test_a_pytest_configuration_above_the_copy_is_not_read(self):
        # The project has no pytest configuration of its own, so pytest under mutmut would look for one in every
        # directory above the copy, where another account may plant one, and a conftest.py beside it.
        self.change_the_functions()
        above, ran = os.path.join(self.bin, "tmp"), os.path.join(self.bin, "planted-ran")
        os.mkdir(above)
        with open(os.path.join(above, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        with open(os.path.join(above, "conftest.py"), "w") as f:
            f.write(f"open({ran!r}, 'w').close()\n")
        with patch.object(gate.tempfile, "tempdir", above):
            found = self.found()
        self.assertFalse(os.path.exists(ran))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [("calc.py", 5, "survived"), ("calc.py", 13, "untested"), ("calc.py", 18, "survived")])

    def test_the_project_s_own_pytest_configuration_still_applies(self):
        self.change_the_functions()
        # Its addopts run only test_checked, so no test calls the other changed functions. Its pythonpath, relative to
        # the configuration's directory, must name mutmut's copy of the code, or checked's mutants go untested too.
        with open(os.path.join(self.repo, "pyproject.toml"), "a") as f:
            f.write('\n[tool.pytest.ini_options]\naddopts = "-k test_checked"\npythonpath = ["."]\n')
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()],
                         [("calc.py", 5, "untested"), ("calc.py", 13, "untested"), ("calc.py", 18, "untested")])

    def test_a_mutmut_table_of_the_wrong_shape_is_a_finding_and_leaves_nothing_behind(self):
        self.change_the_functions()
        tables = [("only_mutate", "only_mutate = 5"), ("only_mutate", 'only_mutate = "calc.py"'),
                  ("do_not_mutate", "do_not_mutate = [1]"), ("when", "when = 1979-05-27")]
        for key, line in tables:
            with self.subTest(line=line):
                tmp = tempfile.mkdtemp(dir=self.bin)
                self.fake_mutmut(f"touch {self.bin}/ran")
                rules = os.path.join(self.bin, "rules.toml")
                with open(rules, "w") as f:
                    f.write(f'[tool.mutmut]\nsource_paths = ["calc.py"]\n{line}\n')
                with patch.object(gate.tempfile, "tempdir", tmp):
                    found = self.found(rules=rules)
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
                self.assertIn(f"[tool.mutmut] {key}", found[0]["message"])
                self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
                self.assertEqual(os.listdir(tmp), [])

    def test_a_failing_test_that_prints_mutmut_s_words_is_not_taken_for_mutmut(self):
        # The clean run fails, and mutmut prints the test's output before its own "failed to collect stats".
        self.change_the_functions("unchecked")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_loud():\n    calc.unchecked(1, 1)\n"
                                                  "    print('could not find any test case for any mutant')\n"
                                                  "    assert False\n")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("failed to collect stats", found[0]["message"])

    def test_a_failed_run_is_named_by_mutmut_s_message_not_its_config_warning(self):
        # paths_to_mutate makes mutmut warn that it is deprecated, on standard error, before the failing test stops it.
        self.change_the_functions("unchecked")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_failing():\n    assert False\n")
        found = self.found(rules=self.rules({"paths_to_mutate": ["calc.py"],
                                             "pytest_add_cli_args_test_selection": ["tests/"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("exited with status 1: failed to collect stats. runner returned 1", found[0]["message"])

    def test_a_file_whose_coding_cookie_names_no_text_encoding_is_not_read(self):
        # Python finds rot13, a codec of str to str, and undefined, which refuses to decode anything, by name, but
        # decodes no source with either.
        for codec in ("rot13", "undefined"):
            with self.subTest(codec=codec, file="changed"):
                self.put(f"tools/{codec}.py", f"# coding: {codec}\n")
                self.git("add", f"tools/{codec}.py")
                self.git("commit", "--quiet", "-m", codec)
                self.put("calc.py", self.BASE)
                self.put(f"tools/{codec}.py", f"# coding: {codec}\nqrs s():\n    erghea 1\n")
                found = self.found()
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                                 [(f"tools/{codec}.py", None, "not-run")])
                self.assertIn("could not be read", found[0]["message"])
            with self.subTest(codec=codec, file="copied"):
                self.put(f"tools/{codec}.py", f"# coding: {codec}\n")
                self.change_the_functions("checked")
                self.fake_mutmut("exit 0", results="    calc.x_checked__mutmut_1: killed\n")
                self.assertEqual(self.found(), [])
                self.git("checkout", "--quiet", "--", ".")

    def test_a_function_whose_file_the_budget_kept_mutmut_from_is_part_of_the_budget(self):
        # mutmut makes and saves mutants file by file, so a run stopped while making them lists only some files'.
        self.put("more.py", "def g(a):\n    return a + 1\n")
        self.git("add", "more.py")
        self.git("commit", "--quiet", "-m", "more")
        self.put("more.py", "def g(a):\n    return a + 2\n")
        self.change_the_functions("checked")
        self.fake_mutmut("exec sleep 30", results="    calc.x_checked__mutmut_1: not checked\n")
        found = self.found(budget=1, rules=self.rules({"source_paths": ["calc.py", "more.py"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "budget-reached")])
        self.assertIn("mutmut made no mutant of `g` in more.py before the budget ran out", found[0]["message"])

    def test_a_pragma_mutmut_does_not_honour_is_not_given_as_the_cause(self):
        # A bare `no mutate` on the def keeps only that line out; under mutate_only_covered_lines the function no test
        # calls gets no mutant for that reason instead.
        self.put("calc.py", self.BASE.replace("def orphan():", "def orphan():  # pragma: no mutate")
                 .replace("return 1", "return 2"))
        found = self.found(rules=self.rules({"source_paths": ["calc.py"], "mutate_only_covered_lines": True,
                                             "pytest_add_cli_args_test_selection": ["tests/"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 13, "not-mutated")])
        self.assertNotIn("on its def keeps it out", found[0]["message"])
        self.assertIn("either it has nothing mutmut mutates", found[0]["message"])

    def test_a_block_pragma_where_a_long_header_ends_is_given_as_the_cause(self):
        self.put("calc.py", self.BASE.replace("def unchecked(a, b):",
                                              "def unchecked(\n        a, b):  # pragma: no mutate block"))
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 5, "not-mutated")])
        self.assertIn("a `# pragma: no mutate block` where its header ends keeps it out", found[0]["message"])

    def test_a_function_in_a_file_mutmut_listed_mutants_of_keeps_its_cause_after_a_budget_stop(self):
        # The fake saves mutants/calc.py.meta, as mutmut does once it has made calc.py's mutants, which is how the
        # gate knows calc.py was reached.
        self.put("calc.py", self.BASE.replace("b + a", "a + b")
                 .replace("def unchecked(a, b):", "def unchecked(a, b):  # pragma: no mutate block"))
        self.fake_mutmut("mkdir -p mutants; echo {} > mutants/calc.py.meta; exec sleep 30",
                         results="    calc.x_checked__mutmut_1: not checked\n")
        found = self.found(budget=1)
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "budget-reached"), ("calc.py", 5, "not-mutated")])
        self.assertNotIn("before the budget ran out", found[0]["message"])
        self.assertIn("a `# pragma: no mutate block` where its header ends keeps it out", found[1]["message"])

    def test_a_module_mutmut_saved_without_mutants_was_reached(self):
        # mutmut saves a module's .meta once it has made its mutants, none or more.
        self.put("more.py", "def g(a):\n    return a + 1\n")
        self.git("add", "more.py")
        self.git("commit", "--quiet", "-m", "more")
        self.put("more.py", "def g(a):  # pragma: no mutate block\n    return a + 2\n")
        self.change_the_functions("checked")
        self.fake_mutmut("mkdir -p mutants; for m in calc more; do echo {} > mutants/$m.py.meta; done; exec sleep 30",
                         results="    calc.x_checked__mutmut_1: not checked\n")
        found = self.found(budget=1, rules=self.rules({"source_paths": ["calc.py", "more.py"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "budget-reached"), ("more.py", 1, "not-mutated")])
        self.assertNotIn("before the budget ran out", found[0]["message"])
        self.assertIn("a `# pragma: no mutate block` where its header ends keeps it out", found[1]["message"])

    def test_mutmut_s_state_in_the_change_is_left_out_and_listed(self):
        self.change_the_functions("unchecked")
        # The .meta mutmut writes for this change, kept from a first run, with every mutant marked killed.
        kept, run = os.path.join(self.bin, "calc.py.meta"), gate._mutmut_findings

        def keep(tree_copy, *args):
            shutil.copy(os.path.join(tree_copy, "mutants", "calc.py.meta"), kept)
            return run(tree_copy, *args)
        with patch.object(gate, "_mutmut_findings", keep):
            self.assertEqual([f["rule"] for f in self.found()], ["survived"])
        with open(kept) as f:
            meta = json.load(f)
        meta["exit_code_by_key"] = dict.fromkeys(meta["exit_code_by_key"], 1)
        self.put("mutants/calc.py.meta", json.dumps(meta))
        self.git("add", "mutants/calc.py.meta")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 5, "survived")])
        diff = self.host.change_diff(self.repo, self.git("rev-parse", "HEAD").strip())
        self.assertEqual(gate.edited_config(diff, changed_lines(diff)), [])
        self.assertEqual(gate.mutmut_state_files(changed_lines(diff)), ["mutants/calc.py.meta"])

    def test_a_function_under_mutmut_s_state_is_not_selected(self):
        self.put("mutants/calc.py", "def x_checked__mutmut_1(a, b):\n    return a - b\n")
        self.git("add", "mutants/calc.py")
        self.fake_mutmut(f"touch {self.bin}/ran")
        found = self.found(rules=self.rules({"source_paths": ["."]}))
        self.assertEqual(found, [])
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))

    def test_unreached_functions_are_named_with_their_files(self):
        for name in ("a.py", "b.py"):
            self.put(name, "def run():\n    return 1\n")
            self.git("add", name)
        self.git("commit", "--quiet", "-m", "run twice")
        for name in ("a.py", "b.py"):
            self.put(name, "def run():\n    return 2\n")
        self.fake_mutmut("exec sleep 30")
        found = self.found(budget=1, rules=self.rules({"source_paths": ["a.py", "b.py"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "budget-reached")])
        self.assertIn("mutmut made no mutant of `run` in a.py and `run` in b.py before the budget ran out",
                      found[0]["message"])

    def test_a_save_the_stop_cut_off_loses_that_module_s_results_alone(self):
        # mutmut rewrites a module's .meta in place after each mutant, so the stop can leave one cut off. The run
        # below is a real one, ended as one the budget stopped would be, with more.py's .meta cut off at half.
        self.put("more.py", "def g(a):\n    return a + 1\n")
        self.git("add", "more.py")
        self.git("commit", "--quiet", "-m", "more")
        self.put("more.py", "def g(a):\n    return a + 2\n")
        self.change_the_functions()
        run_mutmut = gate._run_mutmut

        def stopped_mid_save(tree_copy, args, timeout):
            run, stopped = run_mutmut(tree_copy, args, timeout)
            if args != ["run"]:
                return run, stopped
            meta = os.path.join(tree_copy, "mutants", "more.py.meta")
            with open(meta, "rb") as f:
                saved = f.read()
            with open(meta, "wb") as f:
                f.write(saved[:len(saved) // 2])
            return run, True
        with patch.object(gate, "_run_mutmut", stopped_mid_save):
            found = self.found(rules=self.rules({"source_paths": ["calc.py", "more.py"],
                                                 "pytest_add_cli_args_test_selection": ["tests/"]}))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "budget-reached"), ("calc.py", 5, "survived"), ("calc.py", 13, "untested"),
                          ("calc.py", 18, "survived")])
        self.assertIn("the stop cut off mutmut's save of its results for `g` in more.py, so they count as not "
                      "reached", found[0]["message"])
        self.assertNotIn("before the budget ran out", found[0]["message"])

    # A fake `mutmut results` that fails while mutants/calc.py.meta is there, as mutmut's does on one cut off, and
    # lists nothing once it is gone.
    LISTS_WITHOUT_CALC_META = "[ ! -e mutants/calc.py.meta ]"
    CUTS_OFF_CALC_META = "mkdir -p mutants; printf '{\"exit' > mutants/calc.py.meta"

    def test_a_listing_that_fails_once_the_cut_off_save_is_removed_is_not_run_and_says_so(self):
        self.change_the_functions("checked")
        listed = os.path.join(self.bin, "listed")
        self.fake_mutmut(f"{self.CUTS_OFF_CALC_META}; exec sleep 30",
                         listing=f"echo >> {listed}; echo broken >&2; exit 1")
        found = self.found(budget=1)
        self.assertEqual([f["rule"] for f in found], ["not-run"])
        self.assertIn("mutmut could not list its results", found[0]["message"])
        self.assertIn("broken", found[0]["message"])
        self.assertIn("after the gate removed the cut-off results of calc.py", found[0]["message"])
        with open(listed) as f:
            self.assertEqual(f.read(), "\n")

    def test_a_meta_that_cannot_be_opened_after_a_stop_is_left_to_mutmut(self):
        self.change_the_functions("checked")
        # mutmut reads a .meta that is not there as no results; this one is a dangling link, which the listing needs.
        self.fake_mutmut("mkdir -p mutants; ln -s nowhere mutants/calc.py.meta; exec sleep 30",
                         listing="[ -L mutants/calc.py.meta ] && printf '    calc.x_checked__mutmut_1: killed\\n'")
        found = self.found(budget=1)
        self.assertEqual([f["rule"] for f in found], ["budget-reached"])
        self.assertTrue(found[0]["message"].startswith("budget reached after 1 of 1 mutants:"), found[0]["message"])

    def test_a_cut_off_save_after_a_stop_is_dropped_and_the_rest_listed(self):
        self.change_the_functions("checked")
        # notes.meta is the project's own file, which mutmut copies into mutants/ as it copies every file it finds.
        self.fake_mutmut(f"{self.CUTS_OFF_CALC_META}; echo notes > mutants/notes.meta; exec sleep 30",
                         listing=f"[ -e mutants/notes.meta ] && {self.LISTS_WITHOUT_CALC_META}")
        found = self.found(budget=1)
        self.assertEqual([f["rule"] for f in found], ["budget-reached"])
        self.assertEqual(found[0]["message"],
                         "budget reached with none of the mutants mutmut made left to list: mutmut was stopped at 1 "
                         "seconds (`--mutation-budget`), so the mutants it did not try may survive too; the stop cut "
                         "off mutmut's save of its results for `checked` in calc.py, so they count as not reached.")

    def test_a_cut_off_save_without_a_stop_is_not_dropped(self):
        self.change_the_functions("checked")
        self.fake_mutmut(self.CUTS_OFF_CALC_META, listing=self.LISTS_WITHOUT_CALC_META)
        found = self.found()
        self.assertEqual([f["rule"] for f in found], ["not-run"])
        self.assertIn("mutmut could not list its results", found[0]["message"])

    def test_a_cut_off_save_that_cannot_be_removed_is_not_run_and_says_why(self):
        self.change_the_functions("checked")
        self.fake_mutmut(f"{self.CUTS_OFF_CALC_META}; exec sleep 30", listing=self.LISTS_WITHOUT_CALC_META)
        remove = os.remove

        def refuse_meta(path, *args, **kwargs):
            if str(path).endswith(".meta"):
                raise PermissionError(13, "Permission denied", path)
            return remove(path, *args, **kwargs)
        with patch.object(gate.os, "remove", refuse_meta):
            found = self.found(budget=1)
        self.assertEqual([f["rule"] for f in found], ["not-run"])
        self.assertIn("could not clear the results mutmut's stop cut off", found[0]["message"])
        self.assertIn("Permission denied", found[0]["message"])

    def test_a_state_that_cannot_be_searched_after_a_stop_is_not_run_and_says_why(self):
        self.change_the_functions("checked")
        self.fake_mutmut(f"{self.CUTS_OFF_CALC_META}; exec sleep 30", listing=self.LISTS_WITHOUT_CALC_META)
        walk = os.walk

        def refuse_mutants(top, *args, onerror=None, **kwargs):
            if os.path.basename(top) == "mutants" and onerror is not None:
                onerror(PermissionError(13, "Permission denied", top))
                return iter(())
            return walk(top, *args, onerror=onerror, **kwargs)
        with patch.object(gate.os, "walk", refuse_mutants):
            found = self.found(budget=1)
        self.assertEqual([f["rule"] for f in found], ["not-run"])
        self.assertIn("could not clear the results mutmut's stop cut off", found[0]["message"])
        self.assertIn("Permission denied", found[0]["message"])

    def test_a_series_reads_as_a_list_in_prose(self):
        series = gate._series
        self.assertEqual([series([]), series(["a"]), series(["a", "b"]), series(["a", "b", "c"])],
                         ["", "a", "a and b", "a, b and c"])
        self.assertEqual(series(["a", "b", "c", "d"], limit=2), "a, b and 2 more")
        self.assertEqual(series(["a", "b"], limit=2), "a and b")

    def test_a_change_to_a_help_text_alone_makes_no_mutant(self):
        # The second line of the help text does not say help=, but its string starts on the line that does.
        self.put("calc.py", self.BASE.replace(" to make", " to build"))
        made, found = self.found_and_made()
        self.assertEqual(made, [])
        # mutmut made no mutant of describe, and the finding says why rather than nothing.
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 25, "not-mutated")])
        self.assertEqual(found[0]["message"], "`describe` was not mutation-tested: mutmut made no mutant of it; the "
                                              "rules' do_not_mutate_patterns match each statement in it.")

    def test_a_function_a_mutmut_pragma_opts_out_is_said_so_and_its_pragma_listed(self):
        text = self.BASE.replace("def unchecked(a, b):", "def unchecked(a, b):  # pragma: no mutate block")
        self.put("calc.py", text)
        made, found = self.found_and_made()
        self.assertEqual(made, [])
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 5, "not-mutated")])
        self.assertEqual(found[0]["message"], "`unchecked` was not mutation-tested: mutmut made no mutant of it; a "
                                              "`# pragma: no mutate block` where its header ends keeps it out.")
        diff = self.host.change_diff(self.repo, self.git("rev-parse", "HEAD").strip())
        self.assertEqual(gate.added_suppressions(self.repo, changed_lines(diff)),
                         [("calc.py", 5, "def unchecked(a, b):  # pragma: no mutate block")])

    def test_a_change_to_untested_functions_alone_is_reported_as_untested(self):
        # mutmut stops early when no test calls any function it mutated.
        self.put("calc.py", self.BASE.replace("return 1", "return 2"))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in self.found()], [("calc.py", 13, "untested")])

    def test_a_change_to_no_function_runs_nothing(self):
        self.put("calc.py", self.BASE + "\nLIMIT = 3\n")
        self.put("tests/test_calc.py", self.TESTS + "\n\ndef test_more():\n    calc.checked(1, 1)\n")
        self.fake_mutmut(f"touch {self.bin}/ran")
        self.assertEqual(self.found(), [])
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))

    def test_a_run_stopped_by_the_budget_says_so_with_what_it_found(self):
        self.change_the_functions("checked", "unchecked")
        self.fake_mutmut("exec sleep 30",
                         results="    calc.x_checked__mutmut_1: killed\n    calc.x_unchecked__mutmut_1: survived\n"
                                 "    calc.x_unchecked__mutmut_2: not checked\n",
                         show={"calc.x_unchecked__mutmut_1": self.unchecked_mutant("a / b")})
        started = time.monotonic()
        found = self.found(budget=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(None, None, "budget-reached"), ("calc.py", 5, "survived")])
        self.assertTrue(found[0]["message"].startswith("budget reached after 2 of 3 mutants"), found[0]["message"])
        self.assertTrue(found[1]["message"].startswith("1 of 1 mutants of `unchecked`"), found[1]["message"])

    @staticmethod
    def unchecked_mutant(new):
        """What `mutmut show` prints for a mutant of unchecked that returns new."""
        return (f"# a mutant: survived\n--- calc.py\n+++ calc.py\n@@ -1,2 +1,2 @@\n def unchecked(a, b):\n"
                f"-    return a * b\n+    return {new}\n")

    def test_a_run_that_fails_is_a_finding(self):
        self.change_the_functions()
        self.fake_mutmut("echo 'failed to collect stats. runner returned 1'; exit 1")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("exited with status 1: failed to collect stats. runner returned 1", found[0]["message"])

    def test_a_project_without_mutmut_gets_a_finding_naming_the_import_error(self):
        self.change_the_functions()
        self.fake_mutmut("echo 'Traceback (most recent call last):' >&2; "
                         "echo \"ModuleNotFoundError: No module named 'mutmut'\" >&2; exit 1")
        found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("exited with status 1: ModuleNotFoundError: No module named 'mutmut'", found[0]["message"])

    def test_the_survivors_of_a_function_are_one_finding_with_examples(self):
        self.change_the_functions("unchecked")
        self.fake_mutmut("exit 0", results="    calc.x_unchecked__mutmut_1: survived\n"
                                           "    calc.x_unchecked__mutmut_2: killed\n"
                                           "    calc.x_unchecked__mutmut_3: survived\n",
                         show={"calc.x_unchecked__mutmut_1": self.unchecked_mutant("a / b"),
                               "calc.x_unchecked__mutmut_3": self.unchecked_mutant("None")})
        self.assertEqual(self.found(), [{
            "path": "calc.py", "line": 5, "severity": "warning", "rule": "survived",
            "message": "2 of 3 mutants of `unchecked` survived its tests: line 6, `return a * b` became "
                       "`return a / b`; line 6, `return a * b` became `return None`."}])

    def test_an_example_keeps_a_line_that_holds_a_line_separator_whole(self):
        # Only "\n" ends a line of mutmut's output, as of git's; U+2028 in a string is part of the line.
        self.put("calc.py", self.BASE.replace("return b * a", "return 'a\u2028b'"))
        self.fake_mutmut("exit 0", results="    calc.x_unchecked__mutmut_1: survived\n",
                         show={"calc.x_unchecked__mutmut_1": self.unchecked_mutant("'XXa\u2028bXX'").replace(
                             "return a * b", "return 'a\u2028b'")})
        [finding] = self.found()
        self.assertEqual(finding["message"], "1 of 1 mutants of `unchecked` survived its tests: line 6, "
                                             "`return 'a\u2028b'` became `return 'XXa\u2028bXX'`.")

    def test_a_function_s_first_three_survivors_are_its_examples(self):
        self.change_the_functions("unchecked")
        names = [f"calc.x_unchecked__mutmut_{n}" for n in range(1, 5)]
        self.fake_mutmut("exit 0", results="".join(f"    {name}: survived\n" for name in names),
                         show={name: self.unchecked_mutant(f"{n}") for n, name in enumerate(names)})
        [finding] = self.found()
        self.assertTrue(finding["message"].startswith("4 of 4 mutants of `unchecked` survived its tests: "),
                        finding["message"])
        self.assertEqual(re.findall(r"became `return (\d)`", finding["message"]), ["0", "1", "2"])

    def test_a_mutmut_that_cannot_start_is_a_finding(self):
        self.change_the_functions()
        with patch.object(gate.sys, "executable", os.path.join(self.repo, "no-python")):
            found = self.found()
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-run")])
        self.assertIn("could not start mutmut", found[0]["message"])

    def test_an_unparsable_source_file_is_a_finding(self):
        self.put("calc.py", self.BASE + "def broken(:\n")
        self.fake_mutmut(f"touch {self.bin}/ran")
        found = self.found()
        self.assertFalse(os.path.exists(os.path.join(self.bin, "ran")))
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", None, "not-run")])

    def test_it_reports_without_failing_the_gate_within_its_budget(self):
        with patch.object(gate, "surviving_mutants", return_value=[]) as find:
            [check] = [c for c in gate.gate_checks(42) if c.name == "surviving-mutants"]
            check.find("change")
        self.assertFalse(check.fails)
        find.assert_called_once_with("change", budget=42, test_contexts=None)

    def contexts(self, lines_by_test, path="calc.py", name="test"):
        """A new coverage.py data file outside the tree, name.coverage, as `pytest --cov-context=test` writes one,
        recording each test of lines_by_test, a node id, running its lines of the file at path; its path."""
        data_file = os.path.join(self.bin, f"{name}.coverage")
        data = CoverageData(basename=data_file)
        for node_id, lines in lines_by_test.items():
            data.set_context(f"{node_id}|run")
            data.add_lines({os.path.realpath(os.path.join(self.repo, path)): lines})
        data.write()
        return data_file

    def selections(self):
        """The test selection of each [tool.mutmut] the check starts mutmut by, as a list it fills in; mutmut runs
        as it would."""
        selections, run = [], gate._mutmut_on_copy

        def on_copy(root, tmp, functions, table, budget):
            selections.append(table.get("pytest_add_cli_args_test_selection"))
            return run(root, tmp, functions, table, budget)
        p = patch.object(gate, "_mutmut_on_copy", on_copy)
        p.start()
        self.addCleanup(p.stop)
        return selections

    def test_mutmut_runs_only_the_tests_the_contexts_record_reaching_the_changed_functions(self):
        self.change_the_functions("checked", "unchecked")
        selections = self.selections()
        # A parity test reaches checked too, but the rules leave tests/parity out, as this repository's do.
        data_file = self.contexts({"tests/test_calc.py::test_checked": [1, 2],
                                   "tests/test_calc.py::test_unchecked": [5, 6],
                                   "tests/test_calc.py::test_untouched": [9, 10],
                                   "tests/parity/test_p.py::test_p": [2]})
        rules = self.rules({"source_paths": ["calc.py"], "pytest_add_cli_args_test_selection": ["tests/"],
                            "pytest_add_cli_args": ["--ignore=tests/parity"]})
        found = self.found(rules=rules, test_contexts=data_file)
        self.assertEqual(selections, [["tests/test_calc.py::test_checked", "tests/test_calc.py::test_unchecked"]])
        # The same verdicts as with the whole selection: unchecked's test never looks at what it returns.
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [("calc.py", 5, "survived")])

    def test_without_test_contexts_mutmut_runs_the_whole_selection(self):
        self.change_the_functions("checked")
        selections = self.selections()
        self.assertEqual(self.found(), [])
        self.assertEqual(selections, [["tests/"]])

    def test_functions_no_recorded_test_reaches_are_untested_without_mutmut(self):
        self.change_the_functions("checked", "orphan")
        selections = self.selections()
        data_file = self.contexts({"tests/test_calc.py::test_untouched": [10]})
        found = self.found(test_contexts=data_file)
        self.assertEqual(selections, [])
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [("calc.py", 1, "untested"), ("calc.py", 13, "untested")])
        self.assertIn("no test of [tool.mutmut]'s selection runs `checked`", found[0]["message"])

    def test_contexts_that_cannot_tell_the_tests_leave_mutmut_the_whole_selection_and_say_why(self):
        under_recorded = {f"tests/test_calc.py::test_{i}": [2 if i == 0 else 30 + i] for i in range(11)}
        cases = {
            # Eleven tests, none of the file's lines recorded for more than one: as the sys.monitoring core writes it.
            "records 11 tests but none of its lines for more than 1": self.contexts(under_recorded, name="under"),
            "did not measure `calc.py`": self.contexts({"tests/test_calc.py::test_checked": [2]}, path="other.py",
                                                       name="other"),
            "no such file": os.path.join(self.bin, "missing.coverage"),
        }
        self.change_the_functions("checked")
        selections = self.selections()
        for why, data_file in cases.items():
            with self.subTest(why):
                selections.clear()
                found = self.found(test_contexts=data_file)
                self.assertEqual(selections, [["tests/"]])
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found], [(None, None, "not-narrowed")])
                self.assertIn(why, found[0]["message"])


# The seven functions S3776 flagged, as orchestrator.py had them before 84e87e7 split them.
S3776_FUNCTIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "s3776_at_84e87e7.py.txt")


def score(source, cls=None):
    """The cognitive complexity of the first function in source, or of its class cls's first method, as the check
    scores it in its module."""
    tree = ast.parse(textwrap.dedent(source))
    owner = tree if cls is None else next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    function = next(n for n in owner.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    name = function.name if cls is None else f"{cls}.{function.name}"
    return next(f.score for f in gate._scored_functions(tree) if f.name == name)


class TestCognitiveComplexity(unittest.TestCase):
    """cognitive_complexity, after G. Ann Campbell's paper, in its mapping to Python. Each expected score is
    added up in the comments, one increment per line."""

    def test_loops_and_ifs_add_one_and_their_nesting(self):
        # The paper's sumOfPrimes, whose labelled `continue OUT` Python writes as a loop's else.
        self.assertEqual(score("""
            def sum_of_primes(top):
                total = 0
                for i in range(1, top + 1):  # +1
                    for j in range(2, i):    # +2
                        if i % j == 0:       # +3
                            break
                    else:                    # +1
                        total += i
                return total
        """), 7)

    def test_elif_and_else_add_one_and_nest_their_bodies(self):
        self.assertEqual(score("""
            def f(a):
                if a == 1:      # +1
                    return 1
                elif a == 2:    # +1
                    if a:       # +2
                        return 2
                else:           # +1
                    if a:       # +2
                        return 3
                return 0
        """), 7)

    def test_a_while_and_its_else(self):
        self.assertEqual(score("""
            def f(n):
                while n:      # +1
                    n -= 1
                else:         # +1
                    n = 0
                return n
        """), 2)

    def test_match_adds_one_for_all_its_cases_and_nests_them(self):
        # The paper's getWords: a switch is one increment, however many cases it has.
        self.assertEqual(score("""
            def get_words(n):
                match n:              # +1
                    case 1:
                        return "one"
                    case 2 if n > 0:  # a guard adds nothing of its own
                        return "a couple"
                    case _:
                        if n:         # +2
                            return "lots"
        """), 3)

    def test_except_adds_one_and_its_nesting_while_try_finally_and_with_add_nothing(self):
        self.assertEqual(score("""
            def f(path):
                try:
                    with open(path) as f:  # neither try nor with nests
                        if f:              # +1
                            pass
                except OSError:            # +1
                    if path:               # +2
                        pass
                except ValueError:         # +1
                    pass
                else:                      # +1
                    pass
                finally:
                    if path:               # +1
                        pass
        """), 7)

    def test_each_sequence_of_like_boolean_operators_adds_one(self):
        self.assertEqual(score("""
            def f(a, b, c, d):
                if a and b and c:       # +1 if, +1 and
                    pass
                if a and b or c and d:  # +1 if, +1 and, +1 or, +1 and
                    pass
                x = a or b and c or d   # +1 or, +1 and, +1 or
                y = a or not (b or c)   # +1 or, +1 or: not starts a sequence of its own
                z = (1 if a else 0) or b and c  # +1 or, +1 and, +1 ternary
                return a and (b and c)  # +1: the parentheses do not show in the syntax tree
        """), 15)

    def test_except_and_match_take_their_nesting_too(self):
        self.assertEqual(score("""
            def f(xs):
                for x in xs:            # +1
                    try:
                        pass
                    except ValueError:  # +2
                        pass
                    match x:            # +2
                        case _:
                            pass
        """), 5)

    def test_a_ternary_adds_one_and_its_nesting_and_nests_what_it_holds(self):
        self.assertEqual(score("""
            def f(a, b):
                for x in a:                          # +1
                    y = 1 if x else (2 if b else 3)  # +2, +3
                return y
        """), 6)

    def test_what_a_statement_reads_before_its_body_counts_at_the_statement_s_level(self):
        self.assertEqual(score("""
            def f(xs, key=lambda x: 1 if x else 0):  # the judged function's own defaults count for nothing
                for x in (xs if xs else []):         # +1 for, +1 ternary
                    while (x if x else 0):           # +2 while, +2 ternary
                        if (x if x else 0):          # +3 if, +3 ternary
                            x -= 1
                        elif x:                      # +1
                            x += 1
                match (xs if xs else None):          # +1 match, +1 ternary
                    case _:
                        pass
                try:
                    pass
                except (ValueError if xs else TypeError):  # +1 except, +1 ternary
                    pass
                def g(y=(1 if xs else 2)):           # +1 ternary
                    return y
                h = lambda y=(1 if xs else 2): y     # +1 ternary
                return g, h
        """), 19)

    def test_comprehensions_add_nothing_and_lambdas_and_nested_functions_nest(self):
        self.assertEqual(score("""
            def f(xs):
                ys = [x for x in xs if x for y in x if not y]
                key = lambda x: 1 if x else 2          # +2
                def inner(x):
                    if x:                              # +2
                        return [y for y in x if y and x]  # +1
                    return inner(x - 1)                # +1, recursion
                class Box:
                    def size(self):
                        return 3 if self else 4        # +2
                return sorted(ys, key=key), inner, Box
        """), 8)

    def test_a_function_that_calls_itself_adds_one(self):
        self.assertEqual(score("""
            def walk(node):
                if node is None:                      # +1
                    return 0
                return walk(node.left) + walk(node.right)  # +1, once
        """), 2)

    def test_a_method_calls_itself_through_self_cls_or_its_class(self):
        source = """
            class Tree:
                def depth(self, node):
                    return max(self.depth(c) for c in node)  # +1
                def size(self, node):
                    return Tree.size(self, node)            # +1
                def leaves(self, node):
                    return leaves(node)                     # a module-level function, not this method
                @classmethod
                def build(cls, n):
                    return cls.build(n - 1)                 # +1
        """
        self.assertEqual(score(source, "Tree"), 1)
        self.assertEqual(self.module_scores(source), {"Tree.depth": 1, "Tree.size": 1, "Tree.leaves": 0, "Tree.build": 1})

    def module_scores(self, source):
        """The score of each function of source that the check judges as one, by its qualified name."""
        return {f.name: f.score for f in gate._scored_functions(ast.parse(textwrap.dedent(source)))}

    def test_each_function_in_a_cycle_of_two_scores_the_recursion_point(self):
        self.assertEqual(self.module_scores("""
            def even(n):
                return n == 0 or odd(n - 1)   # +1 or, +1 cycle
            def odd(n):
                return n != 0 and even(n - 1)  # +1 and, +1 cycle
            def unrelated(n):
                return even(n)
        """), {"even": 2, "odd": 2, "unrelated": 0})

    def test_each_function_in_a_cycle_of_three_scores_the_recursion_point_and_one_leading_into_it_none(self):
        self.assertEqual(self.module_scores("""
            def a(x):
                return b(x)
            def b(x):
                return c(x)
            def c(x):
                return a(x)
            def enter(x):
                return a(x)
        """), {"a": 1, "b": 1, "c": 1, "enter": 0})

    def test_methods_in_a_cycle_through_self_score_the_recursion_point(self):
        self.assertEqual(self.module_scores("""
            class Parser:
                def x(self, s):
                    return self.y(s)
                def y(self, s):
                    return Parser.x(self, s)
                def z(self, s):
                    return self.x(s)
        """), {"Parser.x": 1, "Parser.y": 1, "Parser.z": 0})

    def test_a_cycle_through_an_alias_is_not_found(self):
        self.assertEqual(self.module_scores("""
            def a(x):
                f = b
                return f(x)
            def b(x):
                return a(x)
        """), {"a": 0, "b": 0})

    def test_a_call_to_a_name_the_function_binds_is_not_a_call_to_the_module_s_function(self):
        self.assertEqual(self.module_scores("""
            def a(b):
                return b()            # the parameter
            def b():
                return a(len)
            def c():
                def d():
                    return 1
                return d()            # the nested function
            def d():
                return c()
            def e(items):
                f = sorted
                return f(items)       # the local
            def f(items):
                return e(items)
        """), {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0, "f": 0})

    def test_every_way_of_binding_a_name_shadows_the_module_s_function_but_global(self):
        self.assertEqual(self.module_scores("""
            def a(x):
                import b
                from c import d as e
                try:
                    pass
                except ValueError as f:
                    pass
                match x:
                    case [*g]:
                        pass
                    case {**h}:
                        pass
                    case i:
                        pass
                return b() + e() + f() + g() + h() + i()
            def b(): return a(0)
            def e(): return a(0)
            def f(): return a(0)
            def g(): return a(0)
            def h(): return a(0)
            def i(): return a(0)
            def j():
                global k
                k = wrap(k)
                return k()            # the module's k, which it has just rebound
            def k(): return j()
        """), {"a": 2, "b": 0, "e": 0, "f": 0, "g": 0, "h": 0, "i": 0, "j": 1, "k": 1})  # a: except, match

    def test_a_name_bound_only_in_a_nested_scope_leaves_the_function_s_own_call_to_the_module(self):
        self.assertEqual(self.module_scores("""
            def f(x):
                items = [f for f in x]     # the comprehension's own f
                return f(x[1:])            # +1, recursion
            def walk(node):
                key = lambda walk: walk.name
                return walk(node.child)    # +1, recursion
            def g(x):
                def inner():
                    h = 1
                    return h
                return h(x)                # +1, the cycle with h
            def h(x):
                return g(x)
        """), {"f": 1, "walk": 1, "g": 1, "h": 1})

    def test_a_type_parameter_is_not_the_module_s_function(self):
        self.assertEqual(self.module_scores("""
            def T():
                return build(0)
            def build[T](x):
                return T(x)
            class Box[U]:
                def get(self):
                    return U()            # the class's type parameter
            def U():
                return Box.get(None)
        """), {"T": 0, "build": 0, "Box.get": 0, "U": 0})

    def test_a_type_parameter_list_in_a_class_body_sees_the_class_s_names(self):
        self.assertEqual(self.module_scores("""
            def f():
                class C:
                    def f(): return 1
                    def m[T: f()](self): pass     # C's f, as PEP 695 reads a bound
                return C
            def g():
                class C:
                    g = object
                    class D[T](g()): pass         # C's g, as PEP 695 reads a base
                return C
            def h():
                class C:
                    def h(): return 1
                    def m[T](self):
                        return h()                # the module's h: a method's body does not see C's names
                return C
        """), {"f": 0, "g": 0, "h": 1})

    def test_an_assignment_expression_in_a_comprehension_binds_in_the_function(self):
        self.assertEqual(self.module_scores("""
            def f(handlers, x):
                if any((g := h) for h in handlers):   # +1 if
                    return g(x)                       # the g the walrus bound
            def g(x):
                return f([], x)
        """), {"f": 1, "g": 0})

    def test_what_python_reads_outside_a_nested_scope_is_read_in_the_function(self):
        self.assertEqual(self.module_scores("""
            def a(x):
                return [a for a in a(x[1:])]          # +1, the first iterable is read in a's scope
            def b(x, key=lambda b: b):
                return b(x[1:])                       # +1, the lambda's b is the lambda's alone
            def c(x):
                def inner(y: c(x) = 0):               # +1, an annotation is read in c's scope
                    pass
                return inner
            def d():
                class C[T: d()]:                      # +1, a bound is read in the type parameters' scope
                    pass
                return C
            def e():
                @e()                                  # +1, a decorator is read outside the type parameters
                class C[e]:
                    pass
                return C
        """), {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1})

    def test_a_global_or_nonlocal_in_a_nested_scope_leaves_the_outer_binding_alone(self):
        self.assertEqual(self.module_scores("""
            def f():
                g = make()
                def reset():
                    nonlocal g
                    g = make()
                return g()
            def g():
                return f()
            def k():
                return m()
            def m():
                global k
                def inner(k):
                    return k()
                return inner
        """), {"f": 0, "g": 0, "k": 0, "m": 0})

    def test_self_in_a_class_nested_in_a_method_is_that_class_s(self):
        self.assertEqual(self.module_scores("""
            class C:
                def x(self):
                    class D:
                        def z(self):
                            return self.y()
                    return D
                def y(self):
                    return self.x()
        """), {"C.x": 0, "C.y": 0})

    def test_methods_of_two_nested_classes_of_one_name_are_told_apart(self):
        self.assertEqual(self.module_scores("""
            class A:
                class Meta:
                    def x(self):
                        return self.y()
            class B:
                class Meta:
                    def y(self):
                        return self.x()
        """), {"A.Meta.x": 0, "B.Meta.y": 0})

    def test_a_function_that_calls_itself_and_lies_on_a_cycle_scores_the_point_once(self):
        self.assertEqual(self.module_scores("""
            def walk(node):
                return walk(node.left) + visit(node)
            def visit(node):
                return walk(node.right)
            def alone(n):
                return alone(n - 1)
        """), {"walk": 1, "visit": 1, "alone": 1})

    def test_a_method_of_a_class_nested_in_the_function_recurses_as_a_method_does(self):
        self.assertEqual(score("""
            def outer():
                class K:
                    def m(self):
                        return m()       # a module-level m, not this method
                return K
        """), 0)
        self.assertEqual(score("""
            def outer():
                class K:
                    def m(self):
                        if self:
                            return self.m()  # +1 recursion
                        return K.m(self)
                return K
        """), 3)  # the if, nested in m: +2

    def test_code_deeper_than_python_s_recursion_limit_is_scored(self):
        self.assertEqual(score(LONG_SUM), 0)
        self.assertEqual(score(LONG_ELIF), 1001)  # the if, and one for each elif

    def fingerprint(self, source):
        return gate._body_fingerprint(ast.parse(textwrap.dedent(source)).body[0])

    def test_a_body_fingerprint_keeps_where_each_absent_part_sits(self):
        # Dict.keys holds None for a ** entry, and kw_defaults None for a keyword-only argument without a default.
        self.assertNotEqual(self.fingerprint("def f(a, b, c):\n    return {**a, b: c}\n"),
                            self.fingerprint("def f(a, b, c):\n    return {b: a, **c}\n"))
        self.assertNotEqual(self.fingerprint("def f():\n    def g(*, a=1, b):\n        pass\n"),
                            self.fingerprint("def f():\n    def g(*, a, b=1):\n        pass\n"))

    def test_a_body_fingerprint_takes_an_integer_too_long_to_print_in_decimal(self):
        # 4000 hex digits are about 4800 decimal ones, past the 4300 that int's repr allows.
        huge = "def f():\n    return 0x" + "f" * 4000 + "\n"
        self.assertNotEqual(self.fingerprint(huge), self.fingerprint(huge.replace("0xf", "0xe", 1)))

    def test_the_seven_functions_sonarqube_flagged_score_over_the_limit(self):
        with open(S3776_FUNCTIONS) as f:
            tree = ast.parse(f.read())
        scores = {f.name.rpartition(".")[2]: f.score for f in gate._scored_functions(tree)}
        self.assertEqual(sorted(scores), sorted(["main", "_turn", "_poll", "quality_report", "changed_lines",
                                                 "_shared_name", "_parse_step"]))
        for name, n in scores.items():
            self.assertGreater(n, gate.COGNITIVE_COMPLEXITY_LIMIT, name)


# Valid Python whose syntax tree is deeper than the interpreter's recursion limit lets a recursive walk go: a sum of 350
# terms, and an if with 1000 elifs.
LONG_SUM = "def long_sum(a):\n    return " + " + ".join(["a"] * 350) + "\n"


LONG_ELIF = ("def long_elif(x):\n    if x == 0:\n        pass\n"
             + "".join(f"    elif x == {k}:\n        pass\n" for k in range(1, 1001)))


def flat(name, n, indent=""):
    """A function of n ifs one after the other, so of cognitive complexity n, which returns at its last line."""
    body = "".join(f"{indent}    if x == '{name} {k}':\n{indent}        x += {k}\n" for k in range(n))
    return f"{indent}def {name}(x):\n{body}{indent}    return x\n"


class TestTooComplexFunctions(RepositoryTest):
    """The cognitive-complexity check over a real git repository: a base commit, then the change in its working tree."""

    BASE = flat("grow", 14) + "\n\n" + flat("big", 20) + "\n\n" + flat("small", 3)

    BASE_FILES = {"calc.py": BASE}

    def change(self):
        return gate.GateChange.from_tree(self.repo, self.git("rev-parse", "HEAD").strip())

    def found(self):
        """(path, line, rule) of each finding on the working tree's change, and the findings themselves."""
        found = gate.too_complex_functions(self.change())
        return [(f["path"], f["line"], f["rule"]) for f in found], found

    def def_line(self, name, path="calc.py"):
        with open(os.path.join(self.repo, path)) as f:
            return next(n for n, line in enumerate(f, 1) if line.lstrip().startswith(f"def {name}("))

    def nest_under(self, name, k):
        """Add an if inside the kth if of calc.py's function name: 2 more for it, at nesting 1."""
        self.edit("calc.py", f"    if x == '{name} {k}':\n        x += {k}\n",
                  f"    if x == '{name} {k}':\n        x += {k}\n        if x:\n            x -= 1\n")

    def test_an_unchanged_tree_has_no_findings(self):
        self.assertEqual(self.found()[0], [])

    def test_nesting_added_inside_a_function_is_found_at_its_def_line_which_the_change_does_not_add(self):
        self.nest_under("grow", 13)  # its last if: 14 becomes 16
        where, found = self.found()
        self.assertEqual(where, [("calc.py", 1, "over-limit")])
        self.assertNotIn(1, self.change().changed["calc.py"])
        self.assertEqual(found[0]["message"], "`grow`: cognitive complexity 16, 14 at the base; the limit is 15.")
        self.assertEqual(found[0]["severity"], "error")

    def test_a_function_over_the_limit_whose_score_the_change_leaves_alone_is_not_found(self):
        self.edit("calc.py", "        x += 19\n", "        x += 190\n")  # big's last if, still 20
        self.assertEqual(self.found()[0], [])

    def test_a_function_over_the_limit_whose_score_the_change_raises_is_found(self):
        self.nest_under("big", 19)  # its last if: 20 becomes 22
        where, found = self.found()
        self.assertEqual(where, [("calc.py", self.def_line("big"), "over-limit")])
        self.assertEqual(found[0]["message"], "`big`: cognitive complexity 22, 20 at the base; the limit is 15.")

    def test_a_score_that_rises_to_the_limit_is_not_found(self):
        self.edit("calc.py", "    if x == 'grow 13':\n", "    if x == 'grow 13' and x:\n")  # grow: 14 becomes 15
        self.assertEqual(self.found()[0], [])

    def test_a_new_function_over_the_limit_is_found_and_one_at_it_is_not(self):
        self.put("calc.py", self.BASE + "\n\n" + flat("new_big", 16) + "\n\n" + flat("new_ok", 15))
        where, found = self.found()
        self.assertEqual(where, [("calc.py", self.def_line("new_big"), "over-limit")])
        self.assertEqual(found[0]["message"], "`new_big`: cognitive complexity 16, a new function; the limit is 15.")

    def test_a_method_is_named_and_scored_with_its_class(self):
        # 15 ifs, and a call of itself through self: 16.
        self.put("shapes.py", "class Box:\n" + flat("size", 15, indent="    ").replace("return x", "return self.size(x)"))
        self.git("add", "shapes.py")
        where, found = self.found()
        self.assertEqual(where, [("shapes.py", 2, "over-limit")])
        self.assertTrue(found[0]["message"].startswith("`Box.size`: cognitive complexity 16, a new function"))

    def test_a_method_is_scored_with_its_class_at_the_base_too(self):
        # 15 ifs, and a call of itself through self: 16 at the base and after an edit that adds no point.
        self.put("shapes.py", "class Box:\n" + flat("size", 15, indent="    ").replace("return x", "return self.size(x)"))
        self.git("add", "shapes.py")
        self.git("commit", "--quiet", "-m", "box")
        self.edit("shapes.py", "x += 3\n", "x += 30\n")
        self.assertEqual(self.found()[0], [])

    def test_an_unchanged_function_moved_and_renamed_is_compared_with_its_old_self(self):
        big = flat("big", 20)
        self.edit("calc.py", "\n\n" + big, "")
        self.put("other.py", big.replace("def big(", "def huge("))
        self.git("add", "other.py")
        self.assertEqual(self.found()[0], [])

    def test_an_unchanged_function_moved_out_of_a_deleted_file_is_compared_with_its_old_self(self):
        self.git("rm", "--quiet", "calc.py")
        # Lines enough of its own that git does not take other.py for calc.py renamed.
        self.put("other.py", "".join(f"CONSTANT_{k} = {k}\n" for k in range(200)) + "\n\n" + flat("big", 20))
        self.git("add", "other.py")
        self.assertIn(("calc.py", None), gate._changed_files(self.repo, self.git("rev-parse", "HEAD").strip()))
        self.assertEqual(self.found()[0], [])

    def test_an_unchanged_function_made_a_method_in_its_file_is_compared_with_its_old_self(self):
        big = flat("big", 20)
        self.edit("calc.py", big, "class Box:\n" + flat("big", 20, indent="    "))
        self.assertEqual(self.found()[0], [])

    def test_a_copy_of_a_function_over_the_limit_is_new_wherever_it_goes(self):
        copy = flat("big", 20).replace("def big(", "def copied(")
        new = ["`copied`: cognitive complexity 20, a new function; the limit is 15."]
        with self.subTest("beside the original, in its file"):
            self.put("calc.py", self.BASE + "\n\n" + copy)
            self.assertEqual([f["message"] for f in self.found()[1]], new)
        self.put("calc.py", self.BASE)
        self.put("other.py", copy)
        self.git("add", "other.py")
        with self.subTest("in another file, the original's file untouched"):
            self.assertEqual([f["message"] for f in self.found()[1]], new)
        with self.subTest("in another file, the original's file edited elsewhere"):
            self.edit("calc.py", "'small 2':\n        x += 2\n", "'small 2':\n        x += 20\n")
            self.assertEqual([f["message"] for f in self.found()[1]], new)

    def test_a_function_moved_and_made_more_complex_is_judged_as_new(self):
        big = flat("big", 20)
        self.edit("calc.py", "\n\n" + big, "")
        self.put("other.py", big.replace("        x += 19\n", "        x += 19\n        if x:\n            x -= 1\n"))
        self.git("add", "other.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`big`: cognitive complexity 22, a new function; the limit is 15."])

    def test_a_file_renamed_from_any_name_is_compared_with_its_base(self):
        self.commit({"bin/tool": flat("helper", 20)}, "base")
        self.git("mv", "bin/tool", "bin/tool.py")
        self.edit("bin/tool.py", "x += 3\n", "x += 30\n")  # still 20
        self.assertEqual(self.found()[0], [])

    def test_a_function_moved_unchanged_out_of_a_deleted_python_script_keeps_its_base(self):
        self.commit({"bin/tool": "#!/usr/bin/env python3\n" + flat("helper", 20)}, "base")
        self.git("rm", "--quiet", "bin/tool")
        self.put("calc.py", self.BASE + "\n\n" + flat("helper", 20))  # an edit, so git sees no rename
        self.assertEqual(self.found()[0], [])
        # Into a new file, which git may pair with the deleted script as its rename.
        self.put("calc.py", self.BASE)
        self.put("lib.py", flat("helper", 20))
        self.git("add", "lib.py")
        self.assertEqual(self.found()[0], [])

    def test_a_deleted_script_that_is_not_python_lends_no_base(self):
        self.commit({"bin/tool": "#!/bin/sh\n" + flat("helper", 20)}, "base")
        self.git("rm", "--quiet", "bin/tool")
        self.put("calc.py", self.BASE + "\n\n" + flat("helper", 20))  # an edit, so git sees no rename
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`helper`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_out_of_tests_is_judged_as_new_as_tests_were_never_held_to_the_limit(self):
        self.commit({"tests/helpers.py": flat("helper", 20) + "\n\n" + flat("other_helper", 20)}, "base")
        os.makedirs(os.path.join(self.repo, "lib"))
        self.git("mv", "tests/helpers.py", "lib/helpers.py")  # renamed, with both functions
        self.edit("lib/helpers.py", "'helper 3':\n        x += 3\n", "'helper 3':\n        x += 30\n")  # still 20
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`helper`: cognitive complexity 20, a new function; the limit is 15."])
        self.git("commit", "--quiet", "-am", "renamed")
        self.put("lib/helpers.py", flat("helper", 20))
        self.put("lib/more.py", flat("other_helper", 20))  # moved out of the old test file's lines
        self.git("add", "lib/more.py")
        self.assertEqual(self.found()[0], [])  # lib/helpers.py's base is lib/, now held to the limit

    def test_a_function_moved_unchanged_out_of_tests_is_judged_as_new(self):
        self.commit({"tests/helpers.py": "X = 0\n\n\n" + flat("helper", 20)}, "base")
        self.put("tests/helpers.py", "X = 0\n")
        self.put("lib.py", flat("helper", 20))
        self.git("add", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`helper`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_moved_unchanged_into_tests_keeps_its_base_from_a_copy_elsewhere(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("tests/helpers.py", big)  # the original, moved where nothing is judged
        self.put("lib.py", big.replace("def big(", "def copied("))
        self.git("add", "tests/helpers.py", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`copied`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_moved_unchanged_where_nothing_is_judged_keeps_its_base_from_a_copy_of_its_name(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("tests/helpers.py", big)  # the original, moved where nothing is judged
        self.put("lib.py", big)  # a copy under the same name
        self.git("add", "tests/helpers.py", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`big`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_moved_unchanged_into_a_python_script_keeps_its_base_from_a_copy_elsewhere(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("bin/tool", "#!/usr/bin/env python3\n" + big)  # the original, in a script with no .py
        self.put("lib.py", big.replace("def big(", "def copied("))
        self.git("add", "bin/tool", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`copied`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_s_text_moved_into_a_file_that_is_not_python_leaves_its_base_to_its_copy(self):
        big = flat("big", 20)
        for name, first in [("notes.txt", "# python, kept for reference\n"), ("bin/run", "#!/bin/sh\n")]:
            with self.subTest(name):
                self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
                self.put(name, first + big)
                self.put("lib.py", big.replace("def big(", "def copied("))  # the only function with big's body
                self.git("add", name, "lib.py")
                self.assertEqual(self.found()[0], [])
                self.git("rm", "--quiet", "--cached", name)
                os.remove(os.path.join(self.repo, name))

    def test_a_function_in_a_file_the_check_does_not_judge_keeps_its_own_base_from_a_moved_twin(self):
        big = flat("big", 20)
        self.commit({"tests/helpers.py": "X = 0\n\n\n" + big}, "base")
        self.edit("tests/helpers.py", "X = 0\n", "X = 1\n")
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("lib.py", big)  # calc.py's big, moved unchanged
        self.git("add", "lib.py")
        self.assertEqual(self.found()[0], [])

    def test_a_changed_symlink_is_not_read_so_one_to_a_fifo_cannot_block_the_check(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        fifo = os.path.join(elsewhere.name, "pipe")
        os.mkfifo(fifo)
        os.symlink(fifo, os.path.join(self.repo, "x.py"))
        self.git("add", "x.py")
        self.nest_under("big", 19)
        where = []
        worker = threading.Thread(target=lambda: where.extend(self.found()[0]), daemon=True)
        worker.start()
        worker.join(10)
        if worker.is_alive():
            # Open the FIFO's other end and close it, so that the read blocked on it ends and the suite goes on.
            with contextlib.suppress(OSError):
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(10)
            self.fail("the check read the FIFO a changed symlink points to")
        self.assertEqual(where, [("calc.py", self.def_line("big"), "over-limit")])

    def test_a_module_renamed_with_a_symlink_left_under_its_old_name_keeps_its_functions_bases(self):
        self.git("mv", "calc.py", "core.py")
        os.symlink("core.py", os.path.join(self.repo, "calc.py"))
        self.git("add", "calc.py")
        self.assertEqual(self.found()[0], [])

    def test_a_changed_file_gone_before_it_is_read_could_not_be_read(self):
        self.nest_under("big", 19)
        change = self.change()
        os.remove(os.path.join(self.repo, "calc.py"))
        found = gate.too_complex_functions(change)
        self.assertEqual([(f["path"], f["rule"]) for f in found], [("calc.py", "not-scored")])
        self.assertTrue(found[0]["message"].startswith("could not be read ("), found[0]["message"])

    def test_an_unchanged_copy_in_a_file_the_check_does_not_judge_takes_no_base_from_its_original(self):
        big = flat("big", 20)
        self.commit({"tests/helpers.py": "X = 0\n\n\n" + big}, "base")
        self.edit("calc.py", "'big 3':\n        x += 3\n", "'big 3':\n        x += 30\n")  # edited in place: still 20
        self.edit("tests/helpers.py", "X = 0\n", "X = 1\n")  # the copy at the base, in a file edited elsewhere
        self.assertEqual(self.found()[0], [])

    def test_an_unchanged_copy_in_a_file_the_check_does_not_judge_takes_no_base_from_its_original_renamed(self):
        self.put("tests/helpers.py", flat("big", 20))
        self.git("add", "tests/helpers.py")
        self.edit("calc.py", "def big(", "def legacy_big(")  # renamed in place, its body unchanged
        self.assertEqual(self.found()[0], [])

    def test_a_copy_of_its_name_takes_no_base_from_an_original_renamed_in_place(self):
        big = flat("big", 20)
        self.edit("calc.py", "def big(", "def legacy_big(")  # renamed in place, its body unchanged
        self.put("lib.py", big)
        self.git("add", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`big`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_function_moved_unchanged_keeps_its_base_from_a_new_function_under_its_old_name(self):
        new_big = flat("big", 18).replace("'big ", "'new big ")
        for where in ["lib.py", "tests/helpers.py", "bin/tool"]:
            with self.subTest(where):
                self.put("calc.py", self.BASE.replace(flat("big", 20), new_big))
                self.put(where, ("#!/usr/bin/env python3\n" if where == "bin/tool" else "") + flat("big", 20))
                self.git("add", where)
                self.assertEqual([f["message"] for f in self.found()[1]],
                                 ["`big`: cognitive complexity 18, a new function; the limit is 15."])
                self.git("rm", "--quiet", "--cached", where)
                os.remove(os.path.join(self.repo, where))

    def test_a_function_moved_unchanged_out_of_a_file_that_keeps_its_name_keeps_its_base_from_a_copy(self):
        # Two functions named _, as @singledispatch registrations are: the one scoring 20 moves where nothing is
        # judged, the other stays, and a copy of the first is added elsewhere.
        moved, stays = flat("_", 20).replace("'_ ", "'moved "), flat("_", 3).replace("'_ ", "'stays ")
        self.commit({"disp.py": moved + "\n\n" + stays}, "base")
        self.put("disp.py", stays)
        self.put("tests/helpers.py", moved)
        self.put("lib.py", moved)
        self.git("add", "tests/helpers.py", "lib.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`_`: cognitive complexity 20, a new function; the limit is 15."])
        self.assertEqual(self.found()[0][0][0], "lib.py")

    def test_a_function_moved_unchanged_out_of_a_file_that_keeps_its_twin_keeps_its_base(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE + "\n\n" + big.replace("def big(", "def twin("))
        self.git("commit", "--quiet", "-am", "twin")
        self.edit("calc.py", "\n\n" + big.replace("def big(", "def twin("), "")
        self.put("lib.py", big.replace("def big(", "def twin("))  # twin moved unchanged; big stays as it was
        self.git("add", "lib.py")
        self.assertEqual(self.found()[0], [])

    def test_a_copy_of_a_function_the_change_edits_takes_its_base_as_bodies_first_decides(self):
        # The cost TODO.md records ("A copy of a function whose original the change edits keeps the old score and
        # passes"): the copy is taken for the original moved unchanged, and the edited original for a new function.
        big = flat("big", 20)
        self.edit("calc.py", "'big 3':\n        x += 3\n", "'big 3':\n        x += 30\n")  # edited in place: still 20
        for where in ["lib.py", "tests/helpers.py"]:
            with self.subTest(where):
                self.put(where, big)
                self.git("add", where)
                self.assertEqual([f["message"] for f in self.found()[1]],
                                 ["`big`: cognitive complexity 20, a new function; the limit is 15."])
                self.assertEqual(self.found()[0][0][0], "calc.py")
                self.git("rm", "--quiet", "--cached", where)
                os.remove(os.path.join(self.repo, where))

    def test_a_function_whose_decorator_the_change_adds_is_compared_with_its_unchanged_self(self):
        self.edit("calc.py", "def big(x):\n", "@staticmethod\ndef big(x):\n")  # touched, its body as it was
        self.assertEqual(self.found()[0], [])

    def test_a_function_renamed_unchanged_keeps_its_base_from_a_new_function_under_its_old_name(self):
        big = flat("big", 20)
        self.put("calc.py", self.BASE.replace(big, big.replace("def big(", "def legacy_big(")) + "\n\n"
                 + flat("big", 16).replace("'big ", "'new big "))
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`big`: cognitive complexity 16, a new function; the limit is 15."])

    def test_one_base_function_is_the_base_of_one_function_only(self):
        big = flat("big", 20)
        copy = big.replace("def big(", "def copied(")
        # The original moved unchanged, and a copy of it elsewhere.
        self.put("calc.py", self.BASE.replace("\n\n" + big, ""))
        self.put("other.py", big)
        self.put("third.py", copy)
        self.git("add", "other.py", "third.py")
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`copied`: cognitive complexity 20, a new function; the limit is 15."])
        # The copy in a file read before the original's new one, so that only the name decides.
        os.rename(os.path.join(self.repo, "third.py"), os.path.join(self.repo, "a_copy.py"))
        self.git("add", "-A", "third.py", "a_copy.py")
        self.assertEqual(self.found()[0], [("a_copy.py", 1, "over-limit")])

    def test_a_name_defined_twice_is_compared_with_the_same_definition_at_the_base(self):
        # A property's getter, scoring 21, then its setter, which scores nothing, under the same name.
        getter = flat("x", 21, indent="    ").replace("    def x(x):\n", "    @property\n    def x(self):\n")
        self.put("props.py", "class C:\n" + getter + "\n    @x.setter\n    def x(self, value):\n        pass\n")
        self.git("add", "props.py")
        self.git("commit", "--quiet", "-m", "property")
        self.edit("props.py", "x += 3\n", "x += 30\n")  # the getter's, still 21
        self.assertEqual(self.found()[0], [])
        self.edit("props.py", "x += 30\n", "x += 30\n        if x:\n            x -= 1\n")  # 21 becomes 22
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`C.x`: cognitive complexity 22, 21 at the base; the limit is 15."])

    # A function named _ that does nothing, as one @singledispatch registration or @overload stub can be.
    TRIVIAL = "def _(a):\n    return a\n\n\n"

    def test_a_namesake_removed_above_a_function_leaves_it_compared_with_its_own_base(self):
        self.commit({"disp.py": self.TRIVIAL + flat("_", 20)}, "base")
        self.put("disp.py", flat("_", 20).replace("x += 3\n", "x += 30\n"))  # the trivial one gone; still 20
        self.assertEqual(self.found()[0], [])

    def test_a_namesake_added_above_a_function_leaves_it_compared_with_its_own_base(self):
        self.commit({"disp.py": flat("_", 20)}, "base")
        self.put("disp.py", self.TRIVIAL + flat("_", 20).replace("x += 3\n", "x += 30\n"))  # still 20
        self.assertEqual(self.found()[0], [])

    def test_a_rise_of_a_function_whose_name_others_share_is_found(self):
        self.commit({"disp.py": flat("_", 20)}, "base")
        self.put("disp.py", flat("_", 20).replace("        x += 19\n", "        x += 19\n        if x:\n            x -= 1\n"))
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`_`: cognitive complexity 22, 20 at the base; the limit is 15."])

    def test_only_the_tests_directory_is_exempt_as_from_ruff_s_c901(self):
        self.put("tests/test_calc.py", flat("test_big", 16))
        self.put("tests/unit/helpers.py", flat("helper", 16))
        self.put("test_top.py", flat("test_big", 16))
        self.put("src/test_utils.py", flat("util", 16))
        self.git("add", "tests", "test_top.py", "src")
        self.assertEqual(self.found()[0], [("src/test_utils.py", 1, "over-limit"), ("test_top.py", 1, "over-limit")])

    def test_methods_of_nested_classes_and_functions_under_module_level_statements_are_judged(self):
        self.put("more.py", (
            "class Outer:\n"
            "    class Inner:\n"
            + flat("method", 16, indent="        ")
            + "if True:\n"
            + flat("under_if", 16, indent="    ")
            + "try:\n"
            "    import json\n"
            "except ImportError:\n"
            + flat("under_except", 16, indent="    ")))
        self.git("add", "more.py")
        where, found = self.found()
        self.assertEqual(where, [("more.py", self.def_line(name, "more.py"), "over-limit")
                                 for name in ("method", "under_if", "under_except")])
        self.assertEqual([f["message"].split(":")[0] for f in found],
                         ["`Outer.Inner.method`", "`under_if`", "`under_except`"])

    def test_a_renamed_file_is_compared_with_its_base_under_its_old_name(self):
        self.git("mv", "calc.py", "lib.py")
        self.edit("lib.py", "        x += 19\n", "        x += 190\n")
        self.assertEqual(self.found()[0], [])

    def test_an_unparsable_file_is_a_finding(self):
        self.put("calc.py", self.BASE + "def broken(:\n")
        where, found = self.found()
        self.assertEqual(where, [("calc.py", None, "not-scored")])
        self.assertEqual(found[0]["message"], "does not parse as Python, so its functions were not scored.")

    def test_files_that_cannot_be_read_or_parsed_are_findings_and_the_others_are_still_judged(self):
        self.put("a_unknown.py", "# -*- coding: nonsense -*-\n")  # an encoding Python does not know
        self.put("b_broken.py", "def broken(:\n")
        self.put("0_notes.txt", "not Python\n")
        self.git("add", "a_unknown.py", "b_broken.py", "0_notes.txt")
        self.nest_under("big", 19)
        where, found = self.found()
        self.assertEqual(where, [("a_unknown.py", None, "not-scored"), ("b_broken.py", None, "not-scored"),
                                 ("calc.py", self.def_line("big"), "over-limit")])
        self.assertTrue(found[0]["message"].startswith("could not be read ("), found[0]["message"])

    def test_a_base_that_cannot_be_read_leaves_its_functions_judged_as_new(self):
        self.nest_under("big", 19)
        git = gate._git

        def no_blob(cwd, *args, **kw):
            if "cat-file" in args:
                raise gate.GateError("git cat-file failed")
            return git(cwd, *args, **kw)

        with patch.object(gate, "_git", no_blob):
            where, found = self.found()
        self.assertEqual(where, [("calc.py", self.def_line("big"), "over-limit")])
        self.assertEqual(found[0]["message"], "`big`: cognitive complexity 22, a new function; the limit is 15.")

    def test_a_function_in_mutmut_s_state_is_not_judged(self):
        self.put("mutants/calc.py", flat("x_big__mutmut_1", 20) + "\n")
        self.git("add", "mutants/calc.py")
        self.nest_under("big", 19)
        where, _ = self.found()
        self.assertEqual(where, [("calc.py", self.def_line("big"), "over-limit")])

    def put_bytes(self, name, data):
        with open(os.path.join(self.repo, name), "wb") as f:
            f.write(data)

    def test_a_file_with_a_byte_order_mark_or_a_coding_cookie_is_judged_as_python_reads_it(self):
        self.put_bytes("bom.py", b"\xef\xbb\xbf" + flat("marked", 16).encode())
        self.put_bytes("latin.py", b"# -*- coding: latin-1 -*-\nCAFE = 'caf\xe9'\n" + flat("declared", 16).encode())
        self.git("add", "bom.py", "latin.py")
        self.assertEqual(self.found()[0], [("bom.py", 1, "over-limit"), ("latin.py", 3, "over-limit")])

    def test_a_file_whose_coding_cookie_names_no_text_encoding_could_not_be_read(self):
        # rot13 is a codec, but not one that decodes bytes to text; Python refuses such a file.
        self.put("rot.py", "# coding: rot13\n" + flat("rotated", 16))
        self.git("add", "rot.py")
        where, found = self.found()
        self.assertEqual(where, [("rot.py", None, "not-scored")])
        self.assertTrue(found[0]["message"].startswith("could not be read ("), found[0]["message"])

    def test_a_base_whose_coding_cookie_names_no_text_encoding_leaves_its_functions_judged_as_new(self):
        self.put("rot.py", "# coding: rot13\n" + flat("rotated", 20))
        self.git("add", "rot.py")
        self.git("commit", "--quiet", "-m", "rot13")
        self.put("rot.py", flat("rotated", 20).replace("x += 3\n", "x += 30\n"))
        self.assertEqual([f["message"] for f in self.found()[1]],
                         ["`rotated`: cognitive complexity 20, a new function; the limit is 15."])

    def test_a_base_with_a_byte_order_mark_or_a_coding_cookie_is_compared_as_python_reads_it(self):
        bom, cookie = b"\xef\xbb\xbf", b"# -*- coding: latin-1 -*-\nCAFE = 'caf\xe9'\n"
        self.put_bytes("bom.py", bom + flat("marked", 20).encode())
        self.put_bytes("latin.py", cookie + flat("declared", 20).encode())
        self.git("add", "bom.py", "latin.py")
        self.git("commit", "--quiet", "-m", "encodings")
        # Edits that leave both at 20.
        self.put_bytes("bom.py", bom + flat("marked", 20).replace("x += 3\n", "x += 30\n").encode())
        self.put_bytes("latin.py", cookie + flat("declared", 20).replace("x += 3\n", "x += 30\n").encode())
        self.assertEqual(self.found()[0], [])

    def test_a_base_that_does_not_parse_leaves_its_functions_judged_as_new(self):
        self.put("broken.py", "def big(:\n")
        self.git("add", "broken.py")
        self.git("commit", "--quiet", "-m", "broken")
        self.put("broken.py", flat("big", 16))
        where, found = self.found()
        self.assertEqual(where, [("broken.py", 1, "over-limit")])
        self.assertEqual(found[0]["message"], "`big`: cognitive complexity 16, a new function; the limit is 15.")

    def test_code_deeper_than_python_s_recursion_limit_is_judged_in_the_change_and_at_the_base(self):
        self.put("deep.py", LONG_SUM + "\n\n" + LONG_ELIF)
        self.git("add", "deep.py")
        self.assertEqual(self.found()[0], [("deep.py", self.def_line("long_elif", "deep.py"), "over-limit")])
        self.git("commit", "--quiet", "-m", "deep")
        self.edit("deep.py", "    elif x == 1000:\n        pass\n",
                  "    elif x == 1000:\n        pass\n    else:\n        pass\n")  # 1001 becomes 1002
        found = self.found()[1]
        self.assertEqual([f["message"] for f in found],
                         ["`long_elif`: cognitive complexity 1002, 1001 at the base; the limit is 15."])

    def test_an_integer_too_long_to_print_in_decimal_is_judged_like_any_code(self):
        self.put("huge.py", "def h():\n    return 0x" + "f" * 4000 + "\n\n\n" + flat("big_one", 16))
        self.git("add", "huge.py")
        self.assertEqual(self.found()[0], [("huge.py", 5, "over-limit")])

    def test_it_fails_the_gate(self):
        [check] = [c for c in gate.gate_checks(gate.MUTATION_BUDGET) if c.name == "cognitive-complexity"]
        self.assertEqual((check.fails, check.find), (True, gate.too_complex_functions))


class TestChangedPythonFiles(RepositoryTest):
    """How flaky-tests, surviving-mutants and cognitive complexity read the Python files a change touches, end to end
    through each check over a real git repository."""

    # d is a decorator that takes an argument, for the decorators below.
    CALC = "def d(_):\n    return lambda f: f\n\n\ndef add(a, b):\n    return a + b\n"

    BASE_FILES = {
        "pyproject.toml": '[tool.mutmut]\nsource_paths = ["."]\n',
        "calc.py": CALC,
        "tests/test_calc.py": "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n",
    }

    def change(self):
        return gate.GateChange.from_tree(self.repo, self.git("rev-parse", "HEAD").strip())

    @staticmethod
    @contextlib.contextmanager
    def bounded(seconds):
        """A context in which a check that blocks or reads without end fails the test, rather than hanging the suite
        or taking the host's memory: SIGALRM after seconds interrupts an open() blocked on a FIFO, and on Linux an
        address space limited to 1 GiB above the process's present size turns an endless read, as of /dev/zero, into
        MemoryError. Elsewhere the address space is left as it is: macOS has no /proc and does not enforce
        RLIMIT_AS."""
        def expired(signum, frame):
            raise AssertionError(f"the check ran longer than {seconds} seconds")

        limits = resource.getrlimit(resource.RLIMIT_AS)
        cap = limits[0]
        if sys.platform == "linux":
            with open("/proc/self/statm") as f:
                size = int(f.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
            cap = size + 2 ** 30 if limits[1] == resource.RLIM_INFINITY else min(size + 2 ** 30, limits[1])
        previous = signal.signal(signal.SIGALRM, expired)
        resource.setrlimit(resource.RLIMIT_AS, (cap, limits[1]))
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            resource.setrlimit(resource.RLIMIT_AS, limits)
            signal.signal(signal.SIGALRM, previous)

    def test_a_changed_symlink_to_a_fifo_or_dev_zero_is_not_read_by_the_checks_that_rerun_or_mutate(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        fifo = os.path.join(elsewhere.name, "pipe")
        os.mkfifo(fifo)
        # /dev/zero only where bounded() can bound the endless read a regression would make of it.
        for target in (fifo, "/dev/zero") if sys.platform == "linux" else (fifo,):
            with self.subTest(target=target):
                for name in ("x.py", "tests/test_x.py"):
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(os.path.join(self.repo, name))
                    os.symlink(target, os.path.join(self.repo, name))
                self.git("add", "x.py", "tests/test_x.py")
                with self.bounded(20):
                    self.assertEqual(gate.flaky_tests(self.change()), [])
                    self.assertEqual(gate.surviving_mutants(self.change(), budget=60), [])

    def test_a_coding_cookie_naming_an_unknown_codec_is_the_same_reason_in_every_check(self):
        source = "# -*- coding: nonsense -*-\ndef test_x():\n    pass\n"
        self.put("x.py", source)
        self.put("tests/test_x.py", source)
        self.git("add", "x.py", "tests/test_x.py")
        change = self.change()
        found = [*gate.flaky_tests(change), *gate.surviving_mutants(change, budget=60),
                 *gate.too_complex_functions(change)]
        self.assertEqual([(f["path"], f["rule"]) for f in found],
                         [("tests/test_x.py", "not-rerun"), ("x.py", "not-run"), ("x.py", "not-scored")])
        reasons = {f["message"].partition(", so ")[0] for f in found}
        self.assertEqual(len(reasons), 1, reasons)
        self.assertTrue(reasons.pop().startswith("could not be read ("))

    # Decorators that ast.unparse cannot write: a sum too deep for its recursion, and an int, as hexadecimal, of
    # more than sys.get_int_max_str_digits() decimal digits, which it writes in decimal.
    DEEP_DECORATORS = {"deep": "@d(" + " + ".join(["1"] * 5000) + ")", "long": "@d(0x" + "f" * 4200 + ")"}

    def test_a_changed_test_with_a_decorator_too_deep_or_too_long_to_write_is_still_rerun(self):
        for kind, decorator in self.DEEP_DECORATORS.items():
            with self.subTest(kind=kind), patch.object(gate, "FLAKY_SEEDS", (1, 2)):
                self.put("tests/test_calc.py", (
                    "import calc\n\n\ndef d(_):\n    return lambda f: f\n\n\n"
                    f"{decorator}\ndef test_add():\n    assert calc.add(2, 3) == 6\n"))
                found = gate.flaky_tests(self.change())
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                                 [("tests/test_calc.py", 9, "failed-every-run")])

    def test_a_changed_function_with_a_decorator_too_deep_or_too_long_to_write_is_not_mutated(self):
        for kind, decorator in self.DEEP_DECORATORS.items():
            with self.subTest(kind=kind):
                self.put("calc.py", self.CALC.replace("def add(a, b):\n    return a + b",
                                                      f"{decorator}\ndef add(a, b):\n    return b + a"))
                found = gate.surviving_mutants(self.change(), budget=60)
                self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                                 [("calc.py", 6, "not-mutated")])
                self.assertIn("mutmut does not mutate a decorated function", found[0]["message"])


class TestToolFindings(unittest.TestCase):
    """What each linter really prints, as captured from it, read into the quality file's findings."""

    TRACKED = {"orchestrator.py", "README.md", "a.sh", ".github/workflows/w.yml"}

    def read(self, name, output):
        tool = next(t for t in gate.GATE_TOOLS if t.name == name)
        return gate.tool_findings(tool, output, "/repo", self.TRACKED)

    def test_ruff(self):
        self.assertEqual(self.read("ruff", "orchestrator.py:595:18: F402 Import `field` from line 55 shadowed\n"
                                           "orchestrator.py:1:8: invalid-syntax: Expected an expression\nFound 2 errors.\n"),
                         [{"path": "orchestrator.py", "line": 595, "severity": "error", "rule": "ruff F402",
                           "message": "Import `field` from line 55 shadowed"},
                          {"path": "orchestrator.py", "line": 1, "severity": "error", "rule": "ruff invalid-syntax",
                           "message": "Expected an expression"}])

    def test_mypy_drops_notes(self):
        self.assertEqual(self.read("mypy", 'orchestrator.py:594:5: error: Need type annotation for "tokens"  '
                                           '[var-annotated]\norchestrator.py:594:5: note: See https://x\n'),
                         [{"path": "orchestrator.py", "line": 594, "severity": "error", "rule": "mypy var-annotated",
                           "message": 'Need type annotation for "tokens"'}])

    def test_a_name_ruff_prints_lossily_is_not_guessed_between_two_files(self):
        ruff = next(t for t in gate.GATE_TOOLS if t.name == "ruff")
        tracked = {os.fsdecode(b"a\xe9.py"), os.fsdecode(b"a\xe8.py")}
        self.assertEqual(gate.tool_findings(ruff, "a�.py:1:8: F401 `os` imported but unused\n",
                                                    "/repo", tracked), [])

    def test_pymarkdown_with_absolute_paths(self):
        self.assertEqual(self.read("pymarkdown", "/repo/README.md:3:1: MD040: Fenced code blocks should have a "
                                                 "language specified (fenced-code-language)\n"),
                         [{"path": "README.md", "line": 3, "severity": "error", "rule": "pymarkdown MD040",
                           "message": "Fenced code blocks should have a language specified (fenced-code-language)"}])

    def test_shellcheck(self):
        self.assertEqual([(f["line"], f["severity"], f["rule"]) for f in self.read("shellcheck",
                         "a.sh:2:6: note: Double quote to prevent globbing and word splitting. [SC2086]\n"
                         "a.sh:3:1: warning: Use 'cd ... || exit' in case cd fails. [SC2164]\n")],
                         [(2, "note", "shellcheck SC2086"), (3, "warning", "shellcheck SC2164")])

    def test_actionlint_skips_its_snippets(self):
        found = self.read("actionlint", '.github/workflows/w.yml:6:23: "github.event.issue.title" is potentially '
                                        'untrusted. [expression]\n  |\n6 |       - run: echo ${{ x }}\n  |   ^~~\n')
        self.assertEqual([(f["path"], f["line"], f["rule"]) for f in found],
                         [(".github/workflows/w.yml", 6, "actionlint expression")])

    def test_untracked_paths_are_skipped(self):
        self.assertEqual(self.read("ruff", "build/x.py:1:1: F401 unused\n"), [])

    def test_clippy_places_a_finding_in_a_macro_s_definition_where_the_macro_was_used(self):
        def span(file_name, line, expansion=None):
            return {"file_name": file_name, "line_start": line, "is_primary": True,
                    "expansion": expansion and {"span": expansion}}
        registry = "/home/u/.cargo/registry/src/index/serde-1.0.0/src/lib.rs"
        record = {"reason": "compiler-message",
                  "message": {"level": "warning", "code": {"code": "clippy::needless_return"},
                              "message": "unneeded `return` statement",
                              "spans": [span(registry, 90, span("src/main.rs", 3))]}}
        self.assertEqual(gate._rustc_diagnostic(json.dumps(record), "ui", "/repo", {"ui/src/main.rs"}, {}),
                         {"path": "ui/src/main.rs", "line": 3, "severity": "warning",
                          "rule": "clippy clippy::needless_return", "message": "unneeded `return` statement"})
        record["message"]["spans"] = [span(registry, 90)]
        self.assertIsNone(gate._rustc_diagnostic(json.dumps(record), "ui", "/repo", {"ui/src/main.rs"}, {}))

    def test_clippy_s_other_output_holds_no_finding(self):
        for line in ("", "not json", "[1]", '{"reason": "build-finished", "success": false}',
                     json.dumps({"reason": "compiler-message", "message": {
                         "level": "error", "code": None, "spans": [], "message": "aborting due to 1 previous error"}})):
            with self.subTest(line=line):
                self.assertIsNone(gate._rustc_diagnostic(line, "ui", "/repo", {"ui/src/main.rs"}, {}))
