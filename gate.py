"""
The quality gate: judge the change a git working tree makes since a commit, and write the quality file.

    python gate.py --base REV [--config PYPROJECT] [--tests JUNIT]... [--coverage XML]... [--out FILE]
                   [--mutation-budget SECONDS] [--base-run-budget SECONDS]

It judges the working tree of the git repository that holds the current directory: the linters (GATE_TOOLS) over
the files the repository tracks, keeping the findings on the lines the change adds since --base, and the checks
gate_checks lists, such as weakened tests, flaky tests and surviving mutants. `python gate.py --help` gives each
option. Exit status 0 when the quality file's first line is GATE: OK, 1 for GATE:
ERROR, EXIT_GATE_UNJUDGED (2) when it could not judge the change, having written nothing; argparse's own 2 for
bad arguments, and EXIT_INTERRUPTED (130) for Ctrl-C, likewise writing nothing.

The gate imports nothing but the standard library, so this one file is the whole program, to copy into another
project or take from a commit with `git show REV:gate.py`. What it runs it takes from the project it judges: the
linters from PATH, and pytest, pytest-randomly and mutmut through the Python that runs this file (sys.executable).
So run it with that project's Python, in which its dev tools are installed, such as `uv run python gate.py`.

docs/quality-gate.md says what the gate judges and why, and, under "The quality file as a contract", which parts
of the file's format the orchestrator reads back. The orchestrator imports nothing from this module: it keeps its
own copy of those parts, and TestQualityFileContract checks that the two agree.
"""

import argparse
import ast
import bisect
import collections
import contextlib
import difflib
import fnmatch
import functools
import io
import itertools
import json
import math
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import tokenize
import tomllib
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

# Files a change may edit that alter how Jenkins and the gate judge it.
BUILD_CONFIG_FILES = ("Jenkinsfile", "pyproject.toml", "uv.lock")
# The names of the files, anywhere in the tree, that clippy takes its rules or its toolchain from, which the change
# judged supplies whatever --config says: a manifest's [lints], clippy's configuration, the toolchain that brings
# clippy, and, under a `.cargo` directory, cargo's configuration, whose rustflags can set lint levels.
CLIPPY_CONFIG_NAMES = ("Cargo.toml", "clippy.toml", ".clippy.toml", "rust-toolchain", "rust-toolchain.toml")
CARGO_CONFIG_NAMES = ("config", "config.toml")
# The most findings the quality file lists, issues and findings to weigh numbered as one list, and the most failing
# tests; the rest are counted.
QUALITY_ISSUE_LIMIT = 50
# The words of the quality file's first line, the gate line.
GATE_OK = "OK"
GATE_ERROR = "ERROR"
# Coverage of new code below GATE_MIN_COVERAGE percent fails the gate once a change has GATE_MIN_NEW_LINES or more
# new lines to cover; a smaller change is not judged on coverage.
GATE_MIN_COVERAGE = 80.0
GATE_MIN_NEW_LINES = 20
# Seconds each process a linter runs may take before the gate counts the linter as failed: its one run for most,
# and for clippy each of the cargo commands Clippy says.
GATE_TOOL_TIMEOUT = 600
# Seconds a linter, pytest or mutmut run the gate started is read for after it exited or was killed
# (_run_in_session), at most: what a process outside its group, still holding its output, prints after that is lost.
TOOL_KILL_WAIT = 5
# The flaky-tests check reruns the tests a change adds or edits once under each of these pytest-randomly seeds,
# each pytest run, the collection before them included, within FLAKY_RUN_TIMEOUT seconds; so it adds at most
# (1 + len(FLAKY_SEEDS)) * FLAKY_RUN_TIMEOUT seconds to the gate, well inside a build's QUALITY_TIMEOUT.
FLAKY_SEEDS = (1, 2, 3, 4, 5)
FLAKY_RUN_TIMEOUT = 30
# The surviving-mutants check lets mutmut run for `gate.py --mutation-budget` seconds, MUTATION_BUDGET by default, then
# stops it: SIGINT, MUTMUT_STOP_GRACE seconds to exit, a kill, and TOOL_KILL_WAIT seconds to read what it printed.
# Listing its results and showing what a few survivors of each function change, about a third of a second each, then
# get MUTMUT_REPORT_TIMEOUT seconds in all, each call's stop included. So mutmut adds at most the budget and 75 seconds
# to the gate, and copying the tree a few more: with the flaky-tests check's 180, passes-at-base's BASE_RUN_BUDGET and
# the test run, about 990 of a quality round's QUALITY_TIMEOUT, leaving the rest to the queue and the install.
MUTATION_BUDGET = 600
MUTMUT_STOP_GRACE = 10
MUTMUT_REPORT_TIMEOUT = 60
# The passes-at-base check runs the changed tests at the base for `gate.py --base-run-budget` seconds, this by default.
BASE_RUN_BUDGET = 120
# The gate's exit status when it could not judge the change, and so wrote no quality file.
EXIT_GATE_UNJUDGED = 2
# Its exit status when Ctrl-C stopped it, before it wrote the quality file: 128 + SIGINT, as a shell reports it.
EXIT_INTERRUPTED = 130


class GateError(Exception):
    """A failure that stops the gate judging a change, so it writes no quality file; the message is shown to the
    user."""


def tool_env() -> dict[str, str]:
    """The environment for every process the gate starts, git, a linter, pytest or mutmut: its own, without the CI
    credentials the orchestrator reads (JENKINS_USER and JENKINS_TOKEN, and JENKINS_URL with them) and without git's
    switches for how it reads every pathspec.

    So a test the change brings, which the flaky-tests check reruns or mutmut runs, never inherits the tokens when
    the gate runs where they are set. And the gate's pathspecs are written in git's default syntax, which each switch
    breaks without an error: under GIT_LITERAL_PATHSPECS the linters' globs and GateChange.from_tree's exclude match
    nothing, under GIT_NOGLOB_PATHSPECS the globs match nothing, under GIT_GLOB_PATHSPECS "*.py" matches only the top
    level's files, and under GIT_ICASE_PATHSPECS the exclude leaves out a Mutants/ that is not mutmut's.
    """
    left_out = ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN", "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS",
                "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS")
    return {k: v for k, v in os.environ.items() if k not in left_out}


def _git_run(cwd: str, *args: str, timeout: float = 60, verbatim: bool = False) -> subprocess.CompletedProcess:
    """git args in cwd, with tool_env, for a command whose failure the caller interprets: its exit status, standard
    output and standard error, as text.

    The output is decoded strictly in the locale's encoding, each line end made "\n". verbatim keeps it as git
    printed it, for output that carries paths or file contents: line ends stay as they are, and bytes the file
    system's encoding cannot decode become lone surrogates, as os.fsdecode makes them, so a path in it opens the file
    it names. Raises GateError when git cannot be started, runs longer than timeout seconds, or, without verbatim,
    prints what the locale's encoding cannot decode.
    """
    argv = ["git", "-C", cwd, *args]
    try:
        if not verbatim:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=tool_env(),  # noqa: S603
                                  check=False)
        # Bytes, decoded here: text mode cannot leave line ends alone.
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, env=tool_env(), check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GateError(f"{shlex.join(argv)}: {e}") from e
    except UnicodeDecodeError as e:
        raise GateError(f"{shlex.join(argv)}: printed what is not {e.encoding.upper()}: {e}") from e
    return subprocess.CompletedProcess(proc.args, proc.returncode, os.fsdecode(proc.stdout), os.fsdecode(proc.stderr))


def _git(cwd: str, *args: str, timeout: float = 60, verbatim: bool = False) -> str:
    """The standard output of git args in cwd; timeout and verbatim as for _git_run. Raises GateError as _git_run
    does, and when git exits with a status other than 0."""
    proc = _git_run(cwd, *args, timeout=timeout, verbatim=verbatim)
    if proc.returncode != 0:
        raise GateError(f"{shlex.join(['git', '-C', cwd, *args])} failed: {proc.stderr.strip()}")
    return proc.stdout


def _change_diff(root: str, base: str, pathspecs: tuple[str, ...] = ()) -> str:
    """`git diff -U0` from base to the working tree's tracked files, in the one shape changed_lines parses, whatever
    the user's git config. With pathspecs, git's, only the files they match: a rename from a file they leave out is
    an added file.

    It is verbatim, as _git_run says: a line that is not UTF-8, such as Latin-1 text or a binary file that git diffs
    as text, is kept rather than refused, so a change to such a file is judged like any other, and only git's "\n"
    ends a line, so nothing inside an added line can pass for a header. Raises GateError as _git does.
    """
    return _git(root, "-c", "core.quotePath=false", "diff", "-U0", "-M", "--no-color", "--no-ext-diff",
                "--src-prefix=a/", "--dst-prefix=b/", base, "--", *pathspecs, verbatim=True)


def _changed_files(root: str, base: str, pathspecs: tuple[str, ...] = ()) -> list[tuple[str | None, str | None]]:
    """(path at base, path in the working tree) of each tracked file changed since base, a rename paired as
    _change_diff pairs it; None on the side where the file does not exist. pathspecs are as for _change_diff. The
    paths are verbatim, as _git_run says, so each opens the file it names. Raises GateError as _git does."""
    fields = _git(root, "diff", "--name-status", "-z", "-M", "--no-ext-diff", base, "--", *pathspecs,
                  verbatim=True).split("\0")
    out: list[tuple[str | None, str | None]] = []
    i = 0
    # -z gives each entry as its status, then one path, or two for a rename; the list ends with an empty field.
    while i + 1 < len(fields):
        status = fields[i]
        if status.startswith("R"):
            out.append((fields[i + 1], fields[i + 2]))
            i += 3
            continue
        path = fields[i + 1]
        out.append((None, path) if status == "A" else (path, None) if status == "D" else (path, path))
        i += 2
    return out


def changed_lines(diff: str) -> dict[str, set[int]]:
    """The lines each file gains in a `git diff -U0`, by its path in the new tree.

    A file the diff touches without adding a line, such as a pure rename, one that only loses lines
    or an empty new file, maps to an empty set; a deleted file is left out. Only `diff --git` and
    `@@` lines are read inside a hunk, since an added line can itself start with `+++ `. Only "\n" ends a
    line, as in git's output: an added line can hold a carriage return, a form feed or U+2028.
    """
    return _hunk_positions(diff, _hunk_lines)


def _removal_points(diff: str) -> dict[str, set[int]]:
    """For each file of a `git diff -U0`, by its path in the new tree, the new tree's lines after which a hunk that
    only removes lines removed them, 0 for the top of the file. The files are changed_lines'; one without such a
    hunk maps to an empty set."""
    return _hunk_positions(diff, _hunk_removal_point)


def _hunk_positions(diff: str, positions: Callable[[str], Iterable[int]]) -> dict[str, set[int]]:
    """The new tree's lines that positions gives for each `@@` line of each file of a `git diff -U0`, by its path in
    the new tree, the files as changed_lines says."""
    out: dict[str, set[int]] = {}
    path, in_hunk = None, False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            path, in_hunk = _note_path(out, _diff_git_path(line[len("diff --git "):])), False
        elif line.startswith("@@"):
            in_hunk = True
            if path is not None:
                out[path].update(positions(line))
        elif not in_hunk:
            path = _header_path(out, path, line)
    return out


def _note_path(out: dict[str, set[int]], path: str | None) -> str | None:
    """path, entered in out with no lines yet unless it is there or None."""
    if path is not None:
        out.setdefault(path, set())
    return path


def _hunk_lines(line: str) -> range:
    """The new tree's lines that a `@@` line's hunk adds."""
    m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
    if not m:
        return range(0)
    start = int(m.group(1))
    return range(start, start + (1 if m.group(2) is None else int(m.group(2))))


def _hunk_removal_point(line: str) -> list[int]:
    """The new tree's line after which a `@@` line's hunk removes lines when it adds none, which git gives as the
    hunk's new start with a count of 0; [] for a hunk that adds lines."""
    m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+),0 @@", line)
    return [int(m.group(1))] if m else []


def _header_path(out: dict[str, set[int]], path: str | None, line: str) -> str | None:
    """The file the diff is about after a header line outside a hunk, kept in out as changed_lines says."""
    if line.startswith("deleted file mode") and path is not None:
        out.pop(path, None)
        return None
    if line.startswith("rename to "):
        return _note_path(out, _unquote(line[len("rename to "):]))
    if line.startswith("+++ "):
        # git ends the name with a tab when it holds a space.
        name = _unquote(line[len("+++ "):].removesuffix("\t"))
        return _note_path(out, None if name == "/dev/null" else name.removeprefix("b/"))
    return path


def _diff_git_path(names: str) -> str | None:
    """The path of `a/<path> b/<path>`; None when the two differ, as for a rename, which later lines name."""
    if m := re.fullmatch(r'("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")', names):
        a, b = _unquote(m.group(1)), _unquote(m.group(2))
    else:
        half = (len(names) - 1) // 2
        a, b = names[:half], names[half + 1:]
    if a.startswith("a/") and b.startswith("b/") and a[2:] == b[2:]:
        return b[2:]
    return None


def _unquote(name: str) -> str:
    """A path as git quotes it when it holds a quote, a backslash or a control character, or, with core.quotePath
    on, a byte above 0x7f; any other name as it is. The path comes back as os.fsdecode makes it."""
    if len(name) < 2 or not (name.startswith('"') and name.endswith('"')):
        return name
    escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", '"': '"', "\\": "\\"}
    # An octal escape is one byte of the name, so a character above 0x7f is a run of them: gather the bytes and
    # decode them together.
    raw = bytearray()
    for m in re.finditer(r'\\([0-3][0-7]{2}|.)|[^\\]+|\\', name[1:-1], re.DOTALL):
        if m.group(1) is None:
            raw += os.fsencode(m.group(0))
        elif len(m.group(1)) == 3:
            raw.append(int(m.group(1), 8))
        else:
            raw += os.fsencode(escapes.get(m.group(1), m.group(1)))
    return os.fsdecode(bytes(raw))


# The headings of the quality file's sections that the orchestrator reads back, besides its numbered findings, to
# tell whether a quality round failed as the one before it did; the format's home, as the block's opening says.
FAILED_CONDITIONS = "## Failed conditions"
FAILING_TESTS = "## Failing tests"


def quality_report(gate: str, intro: str, *, conditions: list[str] | tuple = (), issues=(), to_weigh=(),
                   elsewhere: int = 0, tests: list[tuple[str, str]] | None = None,
                   coverage: str | None = None, suppressions: list[tuple[str, int, str]] | tuple = (),
                   edited=(), rules_from: str | None = None, mutmut_state=(), clippy_config=()) -> str:
    """The quality file: the gate, GATE_OK or GATE_ERROR, on its first line, then what the Builder has to answer.

    conditions are the failed ones, each a sentence. issues, the findings that fail the gate, and to_weigh,
    the ones that do not, are {path, line, severity, rule, message} as tool_findings gives them, line None
    for one about the whole file and path None for one about the whole change. Both are listed in the order
    given and numbered as one list, issues first, at most QUALITY_ISSUE_LIMIT in all; the rest are counted.
    elsewhere counts the linter findings left out for not being on lines the change added. tests are the
    (name, detail) of the failing tests of the test report the gate was given, None for no tests section, as when
    it was given none. coverage is the sentence about coverage of new code, None for no such section.
    suppressions are the (path, line, text) of lint suppressions the change added. edited are the
    BUILD_CONFIG_FILES the change edits; rules_from names the file the gate took its rules from when that was not the
    edited pyproject.toml. mutmut_state are the files the change adds or edits under mutmut's state, as
    mutmut_state_files gives them. clippy_config are the files the change edits that clippy takes its rules or its
    toolchain from, as clippy_config_files gives them.
    """
    out = [f"GATE: {gate}", "", intro]
    if conditions:
        out += ["", FAILED_CONDITIONS, "", *(one_line(f"- {c}") for c in conditions)]
    out += _issues_section(issues, elsewhere)
    if to_weigh:
        out += _to_weigh_section(to_weigh, after=min(len(issues), QUALITY_ISSUE_LIMIT))
    if tests is not None:
        out += _tests_section(tests)
    if coverage is not None:
        out += ["", "## Coverage on new code", "", coverage]
    if suppressions:
        out += ["", "## Suppressions added", "",
                *(one_line(f"- `{path}:{line}` `{text}`") for path, line, text in suppressions)]
    if edited or mutmut_state or clippy_config:
        out += _config_section(edited, rules_from, mutmut_state, clippy_config)
    return "\n".join(out) + "\n"


# The sections of quality_report after its first, each as lines that start with a blank one.

def on_changed_lines(issues, changed: dict[str, set[int]]) -> list[dict]:
    """The issues on lines the change added, and the lineless ones on files it touched, by path and line."""
    return sorted((i for i in issues if i["path"] in changed
                   and (i["line"] is None or i["line"] in changed[i["path"]])),
                  key=lambda i: (i["path"], i["line"] or 0))


def _issues_section(issues, elsewhere: int) -> list[str]:
    out = ["", "## Issues on changed lines", ""]
    out += [_numbered(k, i) for k, i in enumerate(issues[:QUALITY_ISSUE_LIMIT], 1)] or ["None."]
    if (more := len(issues) - QUALITY_ISSUE_LIMIT) > 0:
        out += ["", f"{more} more issues on changed lines are not listed; fix the ones above first."]
    if elsewhere:
        out += ["", (f"{elsewhere} other open issues in the project are not on lines this change added, "
                     "so they are not listed.")]
    return out


# Leads the findings to weigh; the orchestrator reads the numbered findings after it as ones that do not fail the gate.
TO_WEIGH_NOTE = "These do not fail the gate. Fix each, or say in your report why it should stand."


def _to_weigh_section(to_weigh, after: int) -> list[str]:
    """The findings to weigh, numbered on from after, the last issue's number, within QUALITY_ISSUE_LIMIT."""
    out = ["", "## Findings to weigh", ""]
    room = QUALITY_ISSUE_LIMIT - after
    if room <= 0:
        return out + [f"{len(to_weigh)} findings to weigh are not listed; answer the issues above first."]
    out += [TO_WEIGH_NOTE, "",
            *(_numbered(k, i) for k, i in enumerate(to_weigh[:room], after + 1))]
    if (more := len(to_weigh) - room) > 0:
        out += ["", f"{more} more findings to weigh are not listed."]
    return out


# Each character that ends a line in some reader of the quality file, a Markdown renderer or Python's
# str.splitlines, mapped to the escape one_line writes it as: \xNN, or \uNNNN past \xff, the convention by which
# judge_change also writes a byte that is not UTF-8.
_LINE_BREAK_ESCAPES = str.maketrans({c: f"\\x{ord(c):02x}" if ord(c) <= 0xFF else f"\\u{ord(c):04x}"
                                     for c in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"})


def one_line(text: str) -> str:
    """text as a quality file writes it within one of its lines, such as one item of a list or a path named in a
    sentence: each character that ends a line in some reader written as an escape (_LINE_BREAK_ESCAPES), anything
    else as it is. The orchestrator reads a quality file back line by line, so the escape is what it reads, the
    same in every round."""
    return text.translate(_LINE_BREAK_ESCAPES)


def _numbered(k: int, finding: dict) -> str:
    if finding["path"] is None:
        where = "The change as a whole:"
    elif finding["line"] is None:
        where = f"`{finding['path']} (the whole file)`"
    else:
        where = f"`{finding['path']}:{finding['line']}`"
    return one_line(f"{k}. {where} {finding['severity']} {finding['rule']}: {finding['message']}")


def _tests_section(tests: list[tuple[str, str]]) -> list[str]:
    out = ["", FAILING_TESTS, ""]
    out += [one_line(f"- `{name}`: {detail.strip().splitlines()[0] if detail.strip() else 'failed'}")
            for name, detail in tests[:QUALITY_ISSUE_LIMIT]] or ["None."]
    if len(tests) > QUALITY_ISSUE_LIMIT:
        out.append(f"- and {len(tests) - QUALITY_ISSUE_LIMIT} more.")
    return out


def _config_section(edited, rules_from: str | None, mutmut_state, clippy_config) -> list[str]:
    out = ["", "## Build configuration", ""]
    if "Jenkinsfile" in edited:
        out.append("- The change edits `Jenkinsfile`. The quality job reads it from `main`, "
                   "so this analysis ran without the edit.")
    if "pyproject.toml" in edited:
        out.append(f"- The change edits `pyproject.toml`. The gate took its rules from {rules_from}, so the edit "
                   f"did not change how the change was judged." if rules_from else
                   "- The change edits `pyproject.toml`, which holds the gate's rules; the change was judged by "
                   "the edited rules.")
    if "uv.lock" in edited:
        out.append("- The change edits `uv.lock`, which pins the versions of the linters the gate ran.")
    if clippy_config:
        named = _series([f"`{f}`" for f in clippy_config], limit=5)
        out.append(f"- The change edits {named}, which clippy takes its rules or its toolchain from; it judged the "
                   f"change by the edited ones.")
    if mutmut_state:
        named = _series([f"`{one_line(f)}`" for f in mutmut_state], limit=5)
        out.append(f"- The change adds or edits {named} under "
                   f"`{_MUTMUT_STATE}/`, where mutmut keeps its state. The gate's checks leave them out: none selects "
                   f"a test or a function in them or counts them as a change to the code, and the surviving-mutants "
                   f"check runs mutmut without them. The linters read them as they read any tracked file.")
    return [one_line(line) for line in out]


def edited_config(diff: str, changed: dict[str, set[int]]) -> list[str]:
    """The BUILD_CONFIG_FILES the diff changes, deletes or renames away."""
    touched = _touched_paths(diff, changed)
    return [f for f in BUILD_CONFIG_FILES if f in touched]


def clippy_config_files(diff: str, changed: dict[str, set[int]]) -> list[str]:
    """The files clippy takes its rules or its toolchain from, by CLIPPY_CONFIG_NAMES and CARGO_CONFIG_NAMES, that
    the diff changes, deletes or renames away, in path order."""
    def configures_clippy(path: str) -> bool:
        name = os.path.basename(path)
        return name in CLIPPY_CONFIG_NAMES or (name in CARGO_CONFIG_NAMES
                                               and os.path.basename(os.path.dirname(path)) == ".cargo")
    return sorted(f for f in _touched_paths(diff, changed) if configures_clippy(f))


def _touched_paths(diff: str, changed: dict[str, set[int]]) -> set[str]:
    """The paths diff, of changed as changed_lines gives it, changes, deletes or renames away."""
    return set(changed) | set(re.findall(r"^(?:--- a/|rename from )(.+?)\t?$", diff, re.MULTILINE))


def mutmut_state_files(changed: dict[str, set[int]]) -> list[str]:
    """The files a change adds or edits under mutmut's state, _MUTMUT_STATE, by path, of changed as changed_lines
    gives it: the files GateChange.from_tree leaves out of what the checks see."""
    return sorted(f for f in changed if f == _MUTMUT_STATE or f.startswith(f"{_MUTMUT_STATE}/"))


@dataclass(frozen=True)
class GateTool:
    """A linter the gate runs: which tracked files it reads, and, in a subclass's run, how it runs over them and
    reads its findings."""
    name: str
    # git pathspecs of the tracked files it reads; the gate does not run a tool that none of them match.
    pathspecs: tuple[str, ...]
    # The characters besides "\n" at which the tool ends a line of a file it reads and git does not; a "\r" counts
    # only where no "\n" follows it. Below one, the tool's line numbers are not git's, or it stops reading, so the
    # gate refuses a change below one (stray_line_ends).
    line_ends: tuple[str, ...]

    def run(self, root: str, rules: str, files: list[str], tracked: set[str]) -> tuple[list[dict], list[str]]:
        """The linter's findings over files, the tracked files its pathspecs match, in the repository whose top level
        is root, and the failed conditions it caused, each a sentence, none when it ran as it should.

        rules is the pyproject.toml whose rules decide, tracked every file git tracks, as _git_run's verbatim output
        names them. The findings are {path, line, severity, rule, message} as tool_findings gives them, wherever they
        are; the gate keeps those on added lines. A tool that is missing, fails or times out is a condition, never an
        exception."""
        raise NotImplementedError


@dataclass(frozen=True)
class TextTool(GateTool):
    """A linter that prints a finding a line as `path:line[:col]: detail`, with detail matching its pattern: groups
    rule and message, and severity where the tool grades its findings."""
    # The command line, given the pyproject.toml whose rules decide and the tracked files the pathspecs match.
    command: Callable[[str, list[str]], list[str]] = field(kw_only=True)
    detail: re.Pattern = field(kw_only=True)
    # Exit status 0 means it found nothing, this one that it found something, any other that it failed.
    findings_status: int = field(default=1, kw_only=True)

    def run(self, root: str, rules: str, files: list[str], tracked: set[str]) -> tuple[list[dict], list[str]]:
        """As GateTool.run says, by one run of command in root."""
        proc = _tool_process(self.name, self.command(rules, files), root)
        if isinstance(proc, str):
            return [], [proc]
        found = tool_findings(self, proc.stdout, root, tracked)
        if proc.returncode == 0 or (proc.returncode == self.findings_status and found):
            return found, []
        said = next((line.strip() for line in (proc.stderr + proc.stdout).split("\n") if line.strip()), "")
        if proc.returncode == self.findings_status:
            return [], [f"`{self.name}` reported findings, but none the gate could read: {said[:200]}"]
        return [], [f"`{self.name}` failed with exit status {proc.returncode}: {said[:200]}"]


@dataclass(frozen=True)
class Clippy(GateTool):
    """cargo clippy over every Cargo workspace that a manifest the repository tracks belongs to, all its targets
    checked, tests and examples included.

    Which lints are on is each workspace's own choice, the [lints] tables of its manifests and its clippy.toml: the
    pyproject.toml the gate is given holds none of clippy's. The gate caps every lint of the workspace's own crates
    at warn (rustc's --cap-lints), so that a lint at deny or forbid, which would fail its crate's build and stop
    cargo before the crates after it, is a finding like the rest: every finding is a warning, and the gate keeps
    those on added lines. The cap also lets an allow override a forbid, which rustc otherwise refuses (E0453), so
    such an allow silences its lint here; it is listed under the suppressions added only where its added line holds
    the allow itself (TODO.md). A build that still
    fails is a failed condition, naming the compiler's first error in a
    tracked file, wherever it is, else cargo's own: a change that deletes a function an old line calls breaks the
    build on no line it added.

    The cargo commands it runs, each within GATE_TOOL_TIMEOUT: `cargo clippy --version`, whose failure is clippy
    missing; `cargo locate-project` for each tracked Cargo.toml; and `cargo clippy` in each workspace, which builds
    what it needs to (target/ in the workspace) and may fetch the crates it uses. That last runs with --locked when
    git tracks the workspace's Cargo.lock, so that it judges the versions the change commits and never rewrites the
    file; without one, cargo writes an untracked Cargo.lock beside the manifest.
    """

    def run(self, root: str, rules: str, files: list[str], tracked: set[str]) -> tuple[list[dict], list[str]]:
        """As GateTool.run says; files and rules play no part, since clippy reads crates by their manifests."""
        proc = _tool_process(self.name, ["cargo", "clippy", "--version"], root)
        if isinstance(proc, str):
            return [], [proc]
        if proc.returncode != 0:
            return [], [(f"`{self.name}` is not installed: `cargo clippy --version` failed with exit status "
                         f"{proc.returncode}: {_said(proc)}")]
        manifests, problems = _cargo_workspaces(root, tracked)
        found: list[dict] = []
        for manifest in manifests:
            lints, problem = self._lint_workspace(root, manifest, tracked)
            found += lints
            if problem:
                problems.append(problem)
        # --all-targets checks a library twice, as itself and as its tests, and clippy reports a lint for each.
        return [dict(t) for t in dict.fromkeys(tuple(f.items()) for f in found)], problems

    def _lint_workspace(self, root: str, manifest: str, tracked: set[str]) -> tuple[list[dict], str | None]:
        """The lints clippy finds in the workspace whose root manifest is manifest, relative to root, and the failed
        condition, None when it compiled and clippy ran as it should."""
        workspace = os.path.dirname(manifest)
        locked = ["--locked"] if os.path.join(workspace, "Cargo.lock") in tracked else []
        proc = _tool_process(self.name, ["cargo", "clippy", "--workspace", "--all-targets", "--message-format=json",
                                         *locked, "--", "--cap-lints", "warn"], os.path.join(root, workspace))
        if isinstance(proc, str):
            return [], proc
        lossy = _lossy_names(tracked)
        found = [f for f in (_rustc_diagnostic(line, workspace, root, tracked, lossy)
                             for line in proc.stdout.split("\n")) if f is not None]
        # Capped at warn, a lint never fails the build, so each error is the compiler's.
        lints = [f for f in found if f["severity"] != "error"]
        if proc.returncode == 0:
            return lints, None
        if errors := [f for f in found if f["severity"] == "error"]:
            return lints, (f"`{self.name}` could not compile the workspace of `{manifest}`: "
                           f"`{errors[0]['path']}:{errors[0]['line']}` error {errors[0]['rule']}: "
                           f"{errors[0]['message']}")
        said = next((s.strip() for s in proc.stderr.split("\n") if s.startswith("error")), _said(proc))
        return lints, f"`{self.name}` failed with exit status {proc.returncode} in `{manifest}`: {said[:200]}"


GATE_TOOLS = (
    TextTool("ruff", ("*.py",), ("\r",),
             command=lambda rules, files: ["ruff", "check", "--config", rules, "--output-format", "concise",
                                           "--no-cache", *files],
             detail=re.compile(r"(?P<rule>[\w-]+):? (?:\[\*\] )?(?P<message>.+)")),
    # Which files mypy checks is one of its rules, [tool.mypy] files. A note elaborates an error; it is not one.
    TextTool("mypy", ("*.py",), ("\r",),
             command=lambda rules, files: ["mypy", "--config-file", rules, "--show-column-numbers",
                                           "--no-error-summary", "--cache-dir", os.devnull],
             detail=re.compile(r"(?P<severity>error): (?P<message>.+?)(?:  \[(?P<rule>[\w-]+)\])?")),
    TextTool("pymarkdown", ("*.md",), ("\r",),
             command=lambda rules, files: ["pymarkdown", "--config", rules, "--strict-config", "scan", *files],
             detail=re.compile(r"(?P<rule>MD\d+): (?P<message>.+)")),
    # shellcheck and actionlint have no table in pyproject.toml, so they run on their defaults: --norc and an empty
    # config file keep a .shellcheckrc or .github/actionlint.yaml, which a change could add, from loosening them.
    TextTool("shellcheck", ("*.sh",), ("\r",),
             command=lambda rules, files: ["shellcheck", "--norc", "-f", "gcc", *files],
             detail=re.compile(r"(?P<severity>error|warning|note): (?P<message>.+) \[(?P<rule>SC\d+)\]")),
    # Its YAML parser, ported from libyaml, also ends a line at NEL, U+2028 and U+2029, although YAML 1.2 does not
    # (https://yaml.org/spec/1.2.2/#54-line-break-characters). Observed with actionlint 1.7.12 (go.yaml.in/yaml/v4
    # v4.0.0-rc.3); the tests stub actionlint, so recheck this on an upgrade.
    TextTool("actionlint", (".github/workflows/*.yml", ".github/workflows/*.yaml"), ("\r", "\x85", "\u2028", "\u2029"),
             command=lambda rules, files: ["actionlint", "-config-file", os.devnull, *files],
             detail=re.compile(r"(?P<message>.+) \[(?P<rule>[\w-]+)\]")),
    # rustc numbers lines at "\n" alone, though Rust counts a lone "\r", NEL, U+2028 and U+2029 as whitespace:
    # observed with clippy 0.1.99 (rustc 1.99.1), whose finding below a line holding all four was on git's line.
    Clippy("clippy", ("*.rs",), ()),
)


@dataclass(frozen=True)
class GateChange:
    """The change the gate judges, as each GateCheck is given it; from_tree makes one."""
    root: str  # the repository's top level; its working tree is the change's new side
    base: str  # the commit the change is judged against, a full sha
    diff: str  # _change_diff from base to the working tree, of the files the checks see
    changed: dict[str, set[int]]  # changed_lines(diff)
    files: list[tuple[str | None, str | None]]  # _changed_files from base, of the files the checks see
    # The path of the pyproject.toml whose rules judge the change, as judge_change passes it: `gate.py --config`'s, else
    # the tree's own. None, which only a caller other than judge_change passes, also means the tree's own.
    rules: str | None = None

    @classmethod
    def from_tree(cls, root: str, base: str, rules: str | None = None) -> "GateChange":
        """The change from base, a full sha, to the working tree of the repository whose top level is root, judged by
        rules as the rules field says.

        The checks see the change as if git tracked nothing under mutmut's state, _MUTMUT_STATE, on either side: no
        check selects a test, a function or a changed line there, or counts an edit there as a change to the code. A
        file moved out of it is an added file, and one moved into it a deleted one. That is the one place mutmut's
        state is left out; the linters and the build-configuration listing see it as any other tracked file.

        Raises GateError when git cannot diff the tree against base."""
        pathspecs = (f":(top,exclude,literal){_MUTMUT_STATE}",)
        diff = _change_diff(root, base, pathspecs)
        return cls(root, base, diff, changed_lines(diff), _changed_files(root, base, pathspecs), rules)


@dataclass(frozen=True)
class GateCheck:
    """A check of the change beside the linters, which judges what the change did rather than how the tree
    reads, such as the tests it weakened.

    find returns its findings as {path, line, severity, rule, message}, like tool_findings's, but with rule
    the check's own, which the gate prefixes with name ("" for none), and path None for one about the change
    as a whole; the gate replaces its severity with what it does with the finding (judged). They are listed
    wherever they are, unlike linter findings, which count only on added lines. A check that cannot do its work
    returns a finding saying so, never an empty list, so that it is not read as having found nothing, and never
    under a weighed rule. It must not raise: an exception ends the gate with a traceback and no quality file.
    """
    name: str
    # True: each finding fails the gate, numbered with the linters' issues, but for those of weighed rules. False:
    # each is a finding to weigh, numbered after them, which the Builder answers and the Reviewer judges but which
    # leaves the gate as it is.
    fails: bool
    find: Callable[[GateChange], list[dict]]
    # The rules of a failing check whose findings are weighed instead, as the rule find gives them, without the
    # check's name: what a spec can ask for and the gate cannot read the spec to tell.
    weighed: frozenset[str] = frozenset()

    def judged(self, finding: dict) -> dict:
        """finding with its rule prefixed with name, and its severity what the gate does with it: "error" when it
        fails the gate, "warning" when it is weighed."""
        fails = self.fails and finding["rule"] not in self.weighed
        rule = f"{self.name} {finding['rule']}" if finding["rule"] else self.name
        return {**finding, "rule": rule, "severity": "error" if fails else "warning"}


# A finding's location, before the tool's own part; lineless when the tool names only the file.
_FINDING_AT = re.compile(r"(?P<path>[^:]+):(?:(?P<line>\d+):(?:\d+:)?)? (?P<detail>.+)")

# Comments that silence a linter or exempt lines from coverage, which the quality file lists for the Reviewer; and in
# a Rust file, the attributes that silence a lint: an allow or expect inside one attribute's brackets, also inside a
# cfg_attr, as in `#![cfg_attr(test, allow(...))]`, or alone on a line, as rustfmt wraps a long cfg_attr's.
_SUPPRESSION = re.compile(r"#.*\b(?:noqa|type:\s*ignore|shellcheck\s+disable|pragma:\s*no\s*(?:cover|branch|mutate))\b"
                          r"|<!--\s*pyml\s+disable")
_RUST_SUPPRESSION = re.compile(r"#!?\[[^\]]*\b(?:allow|expect)\s*\(|^\s*(?:allow|expect)\s*\(")


@dataclass(frozen=True)
class NewCoverage:
    """Coverage of the lines a change added: of those lines, how many are coverable and covered, and of the
    branch outcomes on them, how many are taken."""
    lines: int  # added lines the coverage report can cover
    covered_lines: int
    conditions: int  # branch outcomes on those lines
    covered_conditions: int

    def percent(self) -> float:
        """(covered conditions + covered lines) / (conditions + lines), in percent; 100 with nothing to cover."""
        total = self.lines + self.conditions
        return 100.0 * (self.covered_lines + self.covered_conditions) / total if total else 100.0


def judge_change(cwd: str, base: str, checks: tuple[GateCheck, ...], *, rules: str | None = None,
                 coverage: Sequence[str] = (), tests: Sequence[str] = ()) -> str:
    """The quality file for the change from base to the working tree of the git repository at cwd.

    Runs each GATE_TOOLS linter whose files the repository tracks over those files, with the rules of the pyproject.toml
    at rules, or the tree's own, and lists the findings on lines the change added and the lineless ones on files it
    touched. Then runs each of checks, such as gate_checks gives, on the change as GateChange.from_tree gives it, and
    lists what it finds, as an issue or as a finding to weigh as GateCheck.judged says. The gate is ERROR when there is
    such a linter finding or a check's finding judged an error, when a linter is missing, fails or prints nothing
    readable, when a file a linter reads and the change adds lines to holds a line end that git does not count, as
    stray_line_ends finds, when one of tests, JUnit reports of the tree's tests such as pytest's --junitxml and
    cargo-nextest's, has a failing test, or when coverage, Cobertura reports of the tree such as pytest-cov's
    coverage.xml and cargo-llvm-cov's, merged as new_coverage says, puts new code below GATE_MIN_COVERAGE percent on
    GATE_MIN_NEW_LINES or more new lines to cover; else OK. With no tests, or no coverage, the quality file has no
    section on them. Suppressions the change added are listed, not counted against it. Paths are absolute or relative
    to the process's working directory. A file name that is not UTF-8 appears in the quality file with each byte it
    cannot decode as `\\xNN`.

    Raises GateError when it cannot judge: cwd is not in a git work tree, base names no commit, there
    is no rules file, a test or coverage report cannot be read, or git prints what it cannot decode outside
    the paths and diffs it reads verbatim, such as a work tree whose own path is not UTF-8.
    """
    proc = _git_run(cwd, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise GateError(f"{cwd} is not in a git work tree")
    root = proc.stdout.strip()
    proc = _git_run(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if proc.returncode != 0:
        raise GateError(f"{base} names no commit in {root}")
    base_sha = proc.stdout.strip()
    rules_path = os.path.abspath(rules) if rules else os.path.join(root, "pyproject.toml")
    if not os.path.isfile(rules_path):
        raise GateError(f"no rules: {rules_path} is not a file")

    diff = _change_diff(root, base_sha)
    changed = changed_lines(diff)
    tracked = {f for f in _git(root, "ls-files", "-z", verbatim=True).split("\0") if f}
    conditions, findings = [], []
    reads: list[tuple[GateTool, list[str]]] = []  # each linter, with the tracked files it reads
    for tool in GATE_TOOLS:
        files = [f for f in _git(root, "ls-files", "-z", "--", *tool.pathspecs, verbatim=True).split("\0")
                 if f]
        reads.append((tool, files))
        if files:
            found, problems = tool.run(root, rules_path, files, tracked)
            findings += found
            conditions += problems
    issues = on_changed_lines(findings, changed)
    elsewhere = len(findings) - len(issues)
    if issues:
        conditions.insert(0, f"{len(issues)} findings on lines the change added; each is listed below.")
    conditions += [f"`{path}:{n}` holds `{ascii(end)[1:-1]}`, which a linter that reads the file ends a line at and "
                   f"git does not. Such a linter numbers every later line past git's, or stops reading there; "
                   f"either way the gate cannot judge its findings below it. Remove it, end the line there, or "
                   f"write it as an escape in a string."
                   for path, n, end in stray_line_ends(root, changed, reads)]
    to_weigh: list[dict] = []
    change = GateChange.from_tree(root, base_sha, rules_path)
    for check in checks:
        found = [check.judged(f) for f in check.find(change)]
        errors = [f for f in found if f["severity"] == "error"]
        to_weigh += [f for f in found if f["severity"] == "warning"]
        if errors:
            issues += errors
            conditions.append(_check_condition(check.name, len(errors)))

    failing: list[tuple[str, str]] | None = None
    if tests:
        failing = [failed for report in tests for failed in failing_tests(os.path.abspath(report))]
        if failing:
            conditions.append(f"{len(failing)} tests fail; each is listed below.")

    covered, coverage_sentence = None, None
    if coverage:
        covered = new_coverage([os.path.abspath(report) for report in coverage], root, changed)
        coverage_sentence = _coverage_sentence(covered, _unmeasured_files(changed))
        if covered.lines >= GATE_MIN_NEW_LINES and covered.percent() < GATE_MIN_COVERAGE:
            conditions.append(f"Coverage of new code is {_floor_tenth(covered.percent())}%; the gate wants at "
                              f"least {GATE_MIN_COVERAGE:g}% once a change has {GATE_MIN_NEW_LINES} or more new "
                              f"lines to cover.")

    head = _git(root, "rev-parse", "HEAD").strip()
    intro = (f"`orchestrator.py gate` judged the working tree at `{head}` against the base `{base_sha}`"
             + (f", by the rules in `{rules}`." if rules else "."))
    text = quality_report(GATE_ERROR if conditions else GATE_OK, intro, conditions=conditions, issues=issues,
                          to_weigh=to_weigh, elsewhere=elsewhere, tests=failing, coverage=coverage_sentence,
                          suppressions=added_suppressions(root, changed), edited=edited_config(diff, changed),
                          clippy_config=clippy_config_files(diff, changed),
                          mutmut_state=mutmut_state_files(changed),
                          rules_from=f"`{rules}`" if rules else None)
    # Paths and linter output come verbatim, so a name that is not UTF-8 holds lone surrogates, which no file or
    # terminal can take: give their bytes back, then write each byte that is not UTF-8 as \xNN.
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _tool_process(name: str, argv: list[str], cwd: str) -> subprocess.CompletedProcess | str:
    """argv run in cwd for the linter name, with tool_env, as _run_in_session runs it, within GATE_TOOL_TIMEOUT
    seconds: the finished process, whatever its exit status, its output decoded so that the paths it prints match
    git's and keep their line ends; or, when it could not be started or did not finish, the failed condition, a
    sentence naming the linter. A linter stopped at the timeout is killed with every process it started, such as
    the rustc processes of cargo clippy."""
    try:
        proc, stopped = _run_in_session(argv, cwd, tool_env(), timeout=GATE_TOOL_TIMEOUT)
    except FileNotFoundError:
        program = "it" if argv[0] == name else f"`{argv[0]}`"
        return f"`{name}` is not installed: {program} is not on PATH."
    except OSError as e:
        return f"`{name}` could not be started: {e}."
    if stopped:
        return f"`{name}` did not finish within {GATE_TOOL_TIMEOUT} seconds."
    return proc


def _cargo_workspaces(root: str, tracked: set[str]) -> tuple[list[str], list[str]]:
    """The root manifest of each Cargo workspace that a tracked Cargo.toml belongs to, relative to root, the
    repository's top level, in path order; and a failed condition, naming clippy, for each manifest whose workspace
    cargo cannot tell."""
    roots, problems = set(), []
    for manifest in sorted(f for f in tracked if os.path.basename(f) == "Cargo.toml"):
        proc = _tool_process("clippy", ["cargo", "locate-project", "--workspace", "--message-format", "plain",
                                        "--manifest-path", manifest], root)
        if isinstance(proc, str):
            problems.append(proc)
        elif proc.returncode != 0:
            problems.append(f"`clippy` could not find the workspace of `{manifest}`: {_said(proc)}")
        else:
            roots.add(os.path.relpath(proc.stdout.strip(), root))
    return sorted(roots), problems


def _rustc_diagnostic(line: str, workspace: str, root: str, tracked: set[str],
                      lossy: dict[str, str | None]) -> dict | None:
    """The finding in line, one line of `cargo --message-format=json`'s output, at the compiler's level, "error" or
    "warning", and with the lint's name or the error's code as its rule; None for any other line, such as a note, a
    summary with no place in the code, or a finding in a file git does not track.

    workspace is the workspace's directory relative to root, which the paths cargo prints are relative to; tracked
    and lossy as tool_findings uses them. A finding inside a macro's definition is placed where the macro was used,
    at the first expansion that is in a tracked file."""
    try:
        record = json.loads(line)
        message = record["message"] if record.get("reason") == "compiler-message" else None
        if message is None or message["level"] not in ("error", "warning"):
            return None
        span = next((s for s in message["spans"] if s["is_primary"]), None)
        path = None
        while span is not None and (path := _tracked_name(os.path.join(workspace, span["file_name"]), root, tracked,
                                                          lossy)) is None:
            span = (span.get("expansion") or {}).get("span")
        if span is None:
            return None
        code = (message.get("code") or {}).get("code")
        return {"path": path, "line": span["line_start"], "severity": message["level"],
                "rule": f"clippy {code}" if code else "clippy", "message": message["message"]}
    # Anything that is not a record of the shape https://doc.rust-lang.org/cargo/reference/external-tools.html
    # gives, such as a line a build script printed, holds no finding.
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
        return None


def _tracked_name(shown: str, root: str, tracked: set[str], lossy: dict[str, str | None]) -> str | None:
    """The tracked file that shown, a path a tool printed, absolute or relative to root, names; None when it names
    none. lossy is _lossy_names(tracked), for a tool that prints a name that is not UTF-8 lossily."""
    relative = os.path.relpath(os.path.join(root, shown), root)
    return relative if relative in tracked else lossy.get(relative)


def tool_findings(tool: TextTool, output: str, root: str, tracked: set[str]) -> list[dict]:
    """The findings in what the tool printed, as the {path, line, severity, rule, message} the quality file lists.

    A line that is not a finding on a tracked file, such as a summary, is skipped. Paths become relative to root,
    the repository's top level; rule starts with the tool's name, so the Builder knows which tool said it.
    output is decoded as os.fsdecode decodes, line ends untouched: only "\n" ends a line, so a name that holds a
    carriage return, U+2028 or another line separator stays whole. tracked and output hold a name that is not
    UTF-8 as os.fsdecode does; the tool may print it so, or with U+FFFD for each byte that is not, as ruff does,
    which names the one tracked file it can stand for.
    """
    lossy = _lossy_names(tracked)
    out = []
    for text in output.split("\n"):
        # Spaces, tabs and the \r of a \r\n only: other whitespace, such as U+2028, can be part of a name.
        at = _FINDING_AT.fullmatch(text.strip(" \t\r"))
        if not at:
            continue
        path = _tracked_name(at["path"], root, tracked, lossy)
        detail = tool.detail.fullmatch(at["detail"])
        if path is None or not detail:
            continue
        out.append({"path": path, "line": int(at["line"]) if at["line"] else None,
                    "severity": detail.groupdict().get("severity") or "error",
                    "rule": f"{tool.name} {detail['rule']}" if detail["rule"] else tool.name,
                    "message": detail["message"].strip()})
    return out


def _lossy_names(tracked: set[str]) -> dict[str, str | None]:
    """Each name of tracked that is not UTF-8, as os.fsdecode holds it, by how a tool that prints U+FFFD for each
    byte that is not shows it; None for a name so shown that two tracked names share, which then stands for neither."""
    out: dict[str, str | None] = {}
    for name in tracked:
        if (shown := os.fsencode(name).decode("utf-8", "replace")) != name:
            out[shown] = None if shown in out else name
    return out


def failing_tests(report: str) -> list[tuple[str, str]]:
    """(test, what failed) for each failing or erroring test case of a JUnit XML report, such as pytest's
    --junitxml. Raises GateError when the report cannot be read or parsed."""
    return [(name, detail) for name, outcome, detail in junit_outcomes(report) if outcome == "failed"]


def junit_outcomes(report: str) -> list[tuple[str, str, str]]:
    """(test, outcome, what failed) for each test case of a JUnit XML report, such as pytest's --junitxml, test
    named classname.name. outcome is "failed" for a failure or an error, with its message; else, with "",
    "xfailed" for a test that failed as an xfail mark expected (pytest's <skipped type="pytest.xfail">), "skipped"
    for any other skip, or "passed". Raises GateError when the report cannot be read or parsed."""
    try:
        # As for new_coverage: the build's own test run wrote it, and expat refuses the S314 attacks.
        tree = ElementTree.parse(report)  # noqa: S314
    except (OSError, ElementTree.ParseError) as e:
        raise GateError(f"cannot read the test report {report}: {e}") from e
    out = []
    for case in tree.iter("testcase"):
        name = f"{case.get('classname', '')}.{case.get('name', '')}".strip(".")
        failed = next((e for e in (case.find("failure"), case.find("error")) if e is not None), None)
        if failed is not None:
            out.append((name, "failed", failed.get("message") or failed.text or ""))
        elif (skipped := case.find("skipped")) is not None:
            out.append((name, "xfailed" if skipped.get("type") == "pytest.xfail" else "skipped", ""))
        else:
            out.append((name, "passed", ""))
    return out


def new_coverage(reports: Iterable[str], root: str, changed: dict[str, set[int]]) -> NewCoverage:
    """Coverage of the changed lines, from Cobertura reports such as pytest-cov's coverage.xml and cargo-llvm-cov's
    --cobertura output, merged by path and line: a line is coverable when any report lists it, and covered when any
    report covers it; its branch outcomes are the most any report counts, and the most any report covers.

    Each report's paths are relative to its <source> directories, a relative source to the current directory, or to
    root when it lists none; an absolute path stands as it is. So cargo-llvm-cov's report, whose source is its Cargo
    workspace's directory, maps to the repository's paths as long as the tree is where the report was written. A
    changed line no report lists is not coverable, like a comment. Raises GateError when a report cannot be read or
    parsed.
    """
    merged: dict[tuple[str, int], tuple[bool, int, int]] = {}  # (path, line): (covered, covered outcomes, outcomes)
    for report in reports:
        for at, hit, taken, outcomes in _changed_line_coverage(report, root, changed):
            was_hit, was_taken, was_outcomes = merged.get(at, (False, 0, 0))
            merged[at] = (was_hit or hit, max(was_taken, taken), max(was_outcomes, outcomes))
    return NewCoverage(lines=len(merged), covered_lines=sum(hit for hit, _, _ in merged.values()),
                       conditions=sum(outcomes for _, _, outcomes in merged.values()),
                       covered_conditions=sum(taken for _, taken, _ in merged.values()))


def _changed_line_coverage(report: str, root: str,
                           changed: dict[str, set[int]]) -> Iterator[tuple[tuple[str, int], bool, int, int]]:
    """((path, line), covered, covered branch outcomes, branch outcomes) for each entry of the Cobertura report at
    report on a changed line, as new_coverage reads it; a line with no branch data has 0 of 0 outcomes. Raises
    GateError when the report cannot be read or parsed."""
    try:
        # The report comes from the build's own test run, and the expat Python 3.13 bundles refuses the entity
        # expansion attacks S314 is about (https://docs.python.org/3/library/xml.html#xml-vulnerabilities).
        tree = ElementTree.parse(report)  # noqa: S314
    except (OSError, ElementTree.ParseError) as e:
        raise GateError(f"cannot read the coverage report {report}: {e}") from e
    sources = [s.text.strip() for s in tree.iterfind("sources/source") if s.text and s.text.strip()] or [root]
    real_root = os.path.realpath(root)
    for cls in tree.iterfind(".//class"):
        candidates = (os.path.relpath(os.path.realpath(os.path.join(source, cls.get("filename", ""))), real_root)
                      for source in sources)
        path = next((c for c in candidates if c in changed), None)
        if path is None:
            continue
        for line in cls.iterfind("lines/line"):
            try:
                n = int(line.get("number", ""))
                if n not in changed[path]:
                    continue
                hit = int(line.get("hits", "0")) > 0
                if line.get("branch") == "true" and (m := re.search(r"\((\d+)/(\d+)\)",
                                                                     line.get("condition-coverage", ""))):
                    yield (path, n), hit, int(m.group(1)), int(m.group(2))
                else:
                    yield (path, n), hit, 0, 0
            except ValueError as e:
                raise GateError(f"the coverage report {report} has a line it cannot read: {e}") from e


def _coverage_sentence(c: NewCoverage, unmeasured: list[str]) -> str:
    """The quality file's coverage section for c: how much of the new code is covered, then, by name, the changed
    files in unmeasured, whose names coverage.py cannot record, so that c leaves their lines out of both counts."""
    if not c.lines:
        sentence = "No new lines to cover."
    else:
        branches = f", counting their {c.conditions} branch outcomes" if c.conditions else ""
        sentence = (f"{_floor_tenth(c.percent())}% of {c.lines} new lines to cover{branches}; "
                    f"{c.lines - c.covered_lines} lines are not covered.")
    if unmeasured:
        named = _series([f"`{one_line(path)}`" for path in unmeasured], limit=5)
        sentence += f"\n\nNot measured: {named}; coverage.py records no data for a file whose name is not UTF-8."
    return sentence


def _unmeasured_files(changed: dict[str, set[int]]) -> list[str]:
    """The Python files changed, by changed_lines, adds lines to whose names are not UTF-8 (they hold lone
    surrogates, as git's verbatim output decodes them), in order: coverage.py 7.14 collects no data for such a file."""
    return sorted(path for path, added in changed.items() if added and path.endswith(".py") and not _is_utf8(path))


def _is_utf8(name: str) -> bool:
    """Whether name, decoded with os.fsdecode, came from UTF-8 bytes: it holds no lone surrogate."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _floor_tenth(percent: float) -> str:
    """percent to one decimal, rounded down, so a value just below a threshold never prints as the threshold."""
    return f"{math.floor(percent * 10) / 10:.1f}"


def added_suppressions(root: str, changed: dict[str, set[int]]) -> list[tuple[str, int, str]]:
    """The (path, line, text) of each added line, in the working tree at root, that silences a linter or coverage.
    Raises GateError when a file the change adds lines to cannot be read."""
    out = []
    for path, added, lines in _files_with_added_lines(root, changed):
        silencing = (_SUPPRESSION, _RUST_SUPPRESSION) if path.endswith(".rs") else (_SUPPRESSION,)
        for n in sorted(a for a in added if a <= len(lines)):
            text = lines[n - 1].decode("utf-8", "replace")
            if any(p.search(text) for p in silencing):
                out.append((path, n, text.strip()[:160]))
    return out


def stray_line_ends(root: str, changed: dict[str, set[int]],
                    reads: list[tuple[GateTool, list[str]]]) -> list[tuple[str, int, str]]:
    """(path, line, the character) for each file in changed that a linter reads and whose working tree at root
    holds one of that linter's GateTool.line_ends at or before the last line the change added: the first line that
    holds one, whichever commit added it. reads pairs each linter with the tracked files it reads. Raises
    GateError when such a file cannot be read.
    """
    line_ends: dict[str, set[str]] = {}
    for tool, files in reads:
        for f in files:
            line_ends.setdefault(f, set()).update(tool.line_ends)
    out = []
    for path, added, lines in _files_with_added_lines(root, {p: a for p, a in changed.items() if p in line_ends}):
        ends = [(c, c.encode()) for c in sorted(line_ends[path])]
        found = ((n, c) for n, line in enumerate(lines[:max(added)], 1) for c, raw in ends
                 if raw in line.removesuffix(b"\r"))
        if (first := next(found, None)) is not None:
            out.append((path, *first))
    return out


def _files_with_added_lines(root: str, changed: dict[str, set[int]]) -> Iterator[tuple[str, set[int], list[bytes]]]:
    """(path, the lines changed says the change added to it, its lines) for each file in changed with added lines,
    in path order, as the working tree at root has it.

    The lines are numbered as git numbers them: split at b"\n" alone, each without it, so line n is lines[n - 1].
    A symlink's one line is the name it points to, which is what git diffs, not its target's text. A directory,
    such as a submodule, whose commit is what changed, is left out. Raises GateError when a file cannot be
    read.
    """
    for path, added in sorted(changed.items()):
        if not added:
            continue
        full = os.path.join(root, path)
        try:
            if os.path.islink(full):
                text = os.fsencode(os.readlink(full))
            else:
                with open(full, "rb") as f:
                    text = f.read()
        except IsADirectoryError:
            continue
        except OSError as e:
            raise GateError(f"cannot read {path}, which the change edits: {e}") from e
        yield path, added, text.split(b"\n")


# Test integrity: the tests a change removed or weakened, which no linter sees and which make a gate easier to pass.

@dataclass(frozen=True)
class ChangedTestFile:
    """A test file the change touches, as it was at the base and as the working tree has it."""
    old_path: str | None  # None for a file the change adds
    new_path: str | None  # None for a file the change deletes
    before: str  # the text at the base; "" for an added file
    after: str  # the text in the working tree; "" for a deleted file
    added: set[int]  # the lines of after that the change added


@dataclass(frozen=True)
class WeakenedTestsDetector:
    """How the test-integrity check reads one language's tests."""
    language: str
    # Whether a repository path is one of the language's test files, and whether it is the language's at all.
    is_test: Callable[[str], bool]
    is_source: Callable[[str], bool]
    # The findings on every one of the language's test files the change touched, as GateCheck.find returns them,
    # given whether the change also edits the language's code outside its tests.
    find: Callable[[list[ChangedTestFile], bool], list[dict]]


def weakened_tests(change: GateChange) -> list[dict]:
    """The test-integrity check's find: in each WEAKENED_TESTS_DETECTORS language, the tests the change removed,
    skipped, stripped of assertions or wrapped in a swallowed error, and the expected values it changed together
    with the code under test. The change leaves out mutmut's state, as GateChange.from_tree says."""
    out: list[dict] = []
    for detector in WEAKENED_TESTS_DETECTORS:
        files, unreadable = [], []
        for old, new in change.files:
            if not any(p is not None and detector.is_test(p) for p in (old, new)):
                continue
            try:
                files.append(ChangedTestFile(
                    old, new, before=_git(change.root, "show", f"{change.base}:{old}") if old else "",
                    after=_read_text(os.path.join(change.root, new)) if new else "",
                    added=change.changed.get(new, set()) if new else set()))
            except (GateError, OSError, UnicodeDecodeError) as e:
                unreadable.append(_finding(new or old, None, "unreadable",
                                           f"could not be read, so its tests were not checked: {e}", severity="error"))
        code_changed = any(detector.is_source(p) and not detector.is_test(p)
                           for pair in change.files for p in pair if p)
        out += unreadable + (detector.find(files, code_changed) if files else [])
    return out


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _finding(path: str | None, line: int | None, rule: str, message: str, severity: str = "warning") -> dict:
    """A finding as GateCheck.find returns it, its severity as the check grades it; GateCheck.judged sets the one the
    gate lists."""
    return {"path": path, "line": line, "severity": severity, "rule": rule, "message": message}


def _in_scratch_directory(rule: str, purpose: str, run: Callable[[str], list[dict]]) -> list[dict]:
    """The findings of run(tmp), tmp a new empty directory only the gate's account can enter, which is removed
    afterwards whatever run does; purpose says what it is for, as a clause such as "to rerun the changed tests in".
    When the directory cannot be made, run is not called, and the one finding is of rule, saying so; when it cannot
    be removed, a not-removed finding naming it follows run's."""
    try:
        tmp = tempfile.mkdtemp(prefix="gate-")
    except OSError as e:
        return [_finding(None, None, rule,
                         f"could not make a directory {purpose}: {e}; nothing ran, so the check judged nothing.")]
    out: list[dict] = []
    try:
        out = run(tmp)
    finally:
        try:
            shutil.rmtree(tmp)
        except OSError as e:
            out.append(_finding(None, None, "not-removed", f"could not remove the directory {purpose}, `{tmp}`: {e}"))
    return out


def _python_test_file(path: str) -> bool:
    """pytest's and unittest's usual places: under a tests directory, test_*.py or *_test.py."""
    *dirs, name = path.split("/")
    return name.endswith(".py") and ("tests" in dirs or name.startswith("test_") or name.endswith("_test.py"))


def _body_fingerprint(definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """What a function's or class's body does, without its name, where it is, its comments or its layout: the same
    text for two bodies whose syntax trees are equal, so that a test or a function moved or renamed matches its old
    self."""
    return _tree_fingerprint(ast.Module(body=definition.body, type_ignores=[]))


def _tree_fingerprint(root: ast.AST, *, blank_literals: bool = False) -> str:
    """The syntax tree at root as text, without positions: the same text for two equal trees, and different text for
    two that differ. With blank_literals, each constant's value is left out, so trees that differ only in their literals
    give the same text."""
    # Not ast.dump or copy.deepcopy, which recurse as deep as the tree goes and overflow the stack on valid code, such
    # as a sum of a few thousand terms. ast.walk goes breadth first with a queue; each node's type, and each field with
    # every item of a list in its place, a node standing as "@" and coming later in the walk, fix the tree's shape.
    parts = []
    for node in ast.walk(root):
        parts.append(type(node).__name__)
        for field_name, value in ast.iter_fields(node):
            if blank_literals and isinstance(node, ast.Constant) and field_name == "value":
                continue
            items = value if isinstance(value, list) else [value]
            parts.append(f"{field_name}{'[]' if isinstance(value, list) else ''}="
                         + ",".join(_fingerprint_value(item) for item in items))
    return "\0".join(parts)


def _fingerprint_value(value: object) -> str:
    """value, a field of a syntax node or an item of one, as _tree_fingerprint writes it: "@" for a node, else its
    type and value, in a form that cannot fail. An int is in hex, since repr refuses one past sys.get_int_max_str_digits
    decimal digits, which a hex literal can hold."""
    if isinstance(value, ast.AST):
        return "@"
    return f"{type(value).__name__}:{hex(value) if isinstance(value, int) else repr(value)}"


@dataclass(frozen=True)
class _PythonTest:
    """A test function or method, or a class holding tests, in one side of a ChangedTestFile."""
    file: int  # the ChangedTestFile's index in the list find was given
    name: str  # its qualified name in the module: test_x, TestX or TestX.test_x
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef

    def key(self) -> tuple[int, str]:
        return self.file, self.name

    def body(self) -> str:
        """What it does, without its name or where it is, so that a test renamed or moved matches its old self."""
        return _body_fingerprint(self.node)


# The last part of a name that skips a test or expects it to fail, in unittest and pytest.
_SKIP_NAMES = frozenset({"skip", "skipIf", "skipUnless", "skipif", "skipTest", "SkipTest", "xfail",
                         "expectedFailure"})


def python_weakened_tests(files: list[ChangedTestFile], code_changed: bool) -> list[dict]:
    """WeakenedTestsDetector.find for Python: tests removed (renamed or moved ones match their old selves by
    body), skips and swallowed errors on added lines, fewer assertions in a test, and, when code_changed, an
    assertion whose literals changed while the rest of it stayed, or a constant of its file, as _PythonConstants
    reads them, that a kept test's assertions name and the change edited, as _changed_constants says."""
    out: list[dict] = []
    before: list[_PythonTest] = []
    after: list[_PythonTest] = []
    # Each parsed file's syntax trees, by its index in files: at the base, and in the working tree.
    trees: dict[int, tuple[ast.Module, ast.Module]] = {}
    for k, f in enumerate(files):
        new_tree = _parse_python(f.after)
        if new_tree is None:
            out.append(_finding(f.new_path, None, "unparsable",
                                "does not parse as Python, so its tests were not checked.", severity="error"))
            continue
        # A base that does not parse has no tests to lose.
        base_tree = _parse_python(f.before) or ast.Module(body=[], type_ignores=[])
        before += _python_tests(k, base_tree)
        after += _python_tests(k, new_tree)
        trees[k] = base_tree, new_tree
        out += _python_added_weakenings(f.new_path, new_tree, f.added) if f.new_path else []
    out += _python_removed_tests(files, before, after)
    # A file's constants at the base and now, read when a test of it is first compared.
    constants = functools.cache(lambda k: (_PythonConstants.of(trees[k][0], files[k].old_path),
                                           _PythonConstants.of(trees[k][1], files[k].new_path)))
    at_base = {t.key(): t for t in before}
    for t in after:
        if isinstance(t.node, ast.ClassDef) or t.key() not in at_base:
            continue
        was, now = _python_assertions(at_base[t.key()].node), _python_assertions(t.node)
        path = files[t.file].new_path
        if len(now) < len(was):
            out.append(_finding(path, t.node.lineno, "fewer-assertions",
                                f"`{t.name}` has {len(now)} assertions, {len(was)} at the base.", severity="error"))
        if code_changed:
            out += [_finding(path, b.lineno, "expected-value-changed",
                             f"`{t.name}`: `{_code(a)}` became `{_code(b)}`, "
                             f"in a change that also edits the code under test.", severity="error")
                    for a, b in _changed_literals(was, now)]
            out += _changed_constants(path, t.name, (at_base[t.key()].node, was), (t.node, now), *constants(t.file))
    return sorted(out, key=lambda f: (f["path"] or "", f["line"] or 0))


def _parse_python(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # ValueError: a null byte. RecursionError and MemoryError: the parser's own limits on nesting.
        return None


def _decode_python(raw: bytes) -> tuple[str, str]:
    """raw, the bytes of a Python source file, as text, decoded as Python decodes them: by a byte order mark or a PEP
    263 coding cookie, else as UTF-8, with the line endings left as they are; and the encoding that took. Raises
    SyntaxError, and only that, when raw cannot be decoded so: its cookie names an encoding that is unknown, not a text
    encoding, such as rot13, or one that decodes nothing, such as undefined, or it holds bytes the encoding cannot
    decode."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        return raw.decode(encoding), encoding
    except (LookupError, UnicodeError) as e:
        raise SyntaxError(f"cannot be decoded as its coding cookie or UTF-8 says: {e}") from e


@dataclass(frozen=True)
class _ChangedFunction:
    """A top-level function or a method of a top-level class, the unit mutmut mutates and pytest runs as a test, as
    _python_units reads it from the working tree; _changed_python_units gives those the change touched."""
    path: str
    name: str  # f, or C.f for a method
    line: int  # the line of its def
    first: int  # the line it starts on, its decorators included
    source: tuple[str, ...]  # its lines, from first on, as the working tree has them
    # Each of its decorators as ast.unparse writes it, without its @; None for one too deep or too long for it to write,
    # as _unparsed says.
    decorators: tuple[str | None, ...] = ()
    # The last line of the statement before it in its module or class, else its class's first line, else 0.
    floor: int = 0

    @property
    def last(self) -> int:
        """The line it ends on."""
        return self.first + len(self.source) - 1


def _adds_to(f: _ChangedFunction, added: set[int], removed: set[int]) -> bool:
    """Whether the change added a line to f, decorators included, as _changed_python_units's touched; lines it only
    removed do not count."""
    return any(f.first <= n <= f.last for n in added)


def _changed_python_units(change: GateChange, in_scope: Callable[[str], bool],
                          touched: Callable[[_ChangedFunction, set[int], set[int]], bool] = _adds_to,
                          ) -> tuple[list[_ChangedFunction], list[tuple[str, str]]]:
    """The top-level functions and methods of top-level classes that the change touched, in the Python files it adds
    lines to or removes lines from whose paths in_scope accepts, as _parsed_changed_files gives those files:
    touched(unit, added, removed) says so, with the file's _ChangedPythonFile.added and .removed. And (path, why) for
    each such file that could not be read or parsed, with _ChangedPythonFile.why."""
    functions, unreadable = [], []
    for changed in _parsed_changed_files(change, lambda path, added, removed: bool(added or removed) and in_scope(path)):
        if changed.tree is None:
            unreadable.append((changed.path, changed.why))
        else:
            functions += [unit for unit in _python_units(changed.path, changed.text, changed.tree)
                          if touched(unit, changed.added, changed.removed)]
    return functions, unreadable


def _python_units(path: str, text: str, tree: ast.Module) -> list[_ChangedFunction]:
    """Each top-level function and method of a top-level class of tree, the module at path whose source is text,
    as a _ChangedFunction, whether or not the change touched it; top-level ones first, then each class's."""
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    lines = text.split("\n")
    out = []
    # Each block with the line that opens it, and the qualified-name prefix of its functions.
    for block, opener, prefix in [(tree.body, 0, ""),
                                  *((c.body, c.lineno, f"{c.name}.") for c in tree.body
                                    if isinstance(c, ast.ClassDef))]:
        # The last line of the statement before each statement of the block, or the line that opens the block.
        ends = [opener, *(stmt.end_lineno or stmt.lineno for stmt in block)]
        for unit, floor in zip(block, ends, strict=False):
            if isinstance(unit, defs):
                first = min([unit.lineno, *(d.lineno for d in unit.decorator_list)])
                out.append(_ChangedFunction(path, prefix + unit.name, unit.lineno, first,
                                            tuple(lines[first - 1:unit.end_lineno or unit.lineno]),
                                            tuple(_unparsed(d)[0] or None for d in unit.decorator_list), floor))
    return out


def _python_tests(k: int, tree: ast.Module) -> list[_PythonTest]:
    """The module-level test functions, the classes holding test methods or named Test*, and their test methods."""
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    out = []
    for node in tree.body:
        if isinstance(node, defs) and node.name.startswith("test"):
            out.append(_PythonTest(k, node.name, node))
        elif isinstance(node, ast.ClassDef):
            methods = [_PythonTest(k, f"{node.name}.{m.name}", m) for m in node.body
                       if isinstance(m, defs) and m.name.startswith("test")]
            if methods or node.name.startswith("Test"):
                out += [_PythonTest(k, node.name, node), *methods]
    return out


def _python_removed_tests(files: list[ChangedTestFile], before: list[_PythonTest],
                          after: list[_PythonTest]) -> list[dict]:
    """One finding per test the change removed, a class with every test in it counting as one, and a deleted
    file as one; a test whose body reappears under another name or in another file is not removed."""
    now = {t.key() for t in after}
    was = {t.key() for t in before}
    bodies = collections.Counter(t.body() for t in after if t.key() not in was)
    removed = []
    for t in before:
        if t.key() in now:
            continue
        if bodies[t.body()]:
            bodies[t.body()] -= 1
        else:
            removed.append(t)
    gone = {t.key() for t in removed}
    out = []
    for k, f in enumerate(files):
        mine = [t for t in removed if t.file == k]
        tests = [t for t in mine if not isinstance(t.node, ast.ClassDef)]
        if f.new_path is None and mine:
            out.append(_finding(f.old_path, None, "removed-test",
                                f"deleted, with its {len(tests)} tests: {_names(tests)}.", severity="error"))
            continue
        # A class whose tests are all removed is one finding; a class that keeps or moved any is not.
        whole = {t.name for t in mine if isinstance(t.node, ast.ClassDef)
                 and all((k, m.name) in gone for m in before if m.file == k and m.name.startswith(f"{t.name}."))}
        for t in mine:
            cls = t.name.split(".")[0]
            if t.name in whole:
                n = sum(1 for m in tests if m.name.startswith(f"{t.name}."))
                out.append(_finding(f.old_path, t.node.lineno, "removed-test",
                                    f"`{t.name}` and its {n} tests are gone; this was line "
                                    f"{t.node.lineno} at the base.", severity="error"))
            elif cls not in whole and not isinstance(t.node, ast.ClassDef):
                out.append(_finding(f.old_path, t.node.lineno, "removed-test",
                                    f"`{t.name}` is gone; this was line {t.node.lineno} at the base.",
                                    severity="error"))
    return out


def _names(tests: list[_PythonTest], limit: int = 5) -> str:
    shown = ", ".join(f"`{t.name}`" for t in tests[:limit])
    return shown + (f" and {len(tests) - limit} more" if len(tests) > limit else "")


def _python_added_weakenings(path: str, tree: ast.Module, added: set[int]) -> list[dict]:
    """The skips and swallowed errors on the lines the change added to a test file."""
    out = []
    seen = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line not in added:
            continue
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if name in _SKIP_NAMES and (line, name) not in seen:
            seen.add((line, name))
            out.append(_finding(path, line, "skip", f"`{_code(node)}` added: it skips a "
                                                    f"test or expects it to fail.", severity="error"))
        elif isinstance(node, ast.ExceptHandler) and _swallows(node):
            out.append(_finding(path, line, "swallowed-error",
                                f"`{_code(node)}` added in a test: "
                                f"it hides the failure it catches.", severity="error"))
    return out


def _swallows(handler: ast.ExceptHandler) -> bool:
    """A bare except, or one catching Exception or BaseException that does nothing."""
    if handler.type is None:
        return True
    catches_all = isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException")
    return catches_all and all(isinstance(s, ast.Pass)
                               or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in handler.body)


def _python_assertions(test: ast.AST) -> list[ast.Assert | ast.Call]:
    """A test's assertions in source order: assert statements, self.assert* calls and pytest.raises calls."""
    out: list[ast.Assert | ast.Call] = []
    for n in ast.walk(test):
        if isinstance(n, ast.Assert) or (
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                and ((n.func.value.id == "self" and n.func.attr.startswith("assert"))
                     or (n.func.value.id == "pytest" and n.func.attr == "raises"))):
            out.append(n)
    return sorted(out, key=lambda n: (n.lineno, n.col_offset))


def _changed_literals(was: list[ast.Assert | ast.Call],
                      now: list[ast.Assert | ast.Call]) -> list[tuple[ast.Assert | ast.Call, ast.Assert | ast.Call]]:
    """The (before, after) pairs of assertions that kept their shape but not their literals, the assertions of
    each side aligned by shape."""
    blocks = difflib.SequenceMatcher(None, [_shape(a) for a in was], [_shape(b) for b in now],
                                     autojunk=False).get_matching_blocks()
    return [(was[i + d], now[j + d]) for i, j, n in blocks for d in range(n)
            if _literals(was[i + d]) != _literals(now[j + d])]


@dataclass(frozen=True)
class _PythonConstant:
    """A module-level constant of a test file, as _PythonConstants reads it from one side of a ChangedTestFile."""
    line: int  # the line its assignment starts on
    fingerprint: str  # the expression assigned to it, as _tree_fingerprint writes it


@dataclass(frozen=True)
class _PythonConstants:
    """The module-level constants of one side of a test file, and the dotted names its tests can reach it by.

    A constant is a name that one module-level Assign or AnnAssign binds to an expression holding no call, and that
    nothing else in the module's own scope binds, as _scope_bindings counts. A test reads one only where it binds
    the name nowhere, as asserted says: shadowing is not resolved. Out of scope: a constant imported from another
    module or computed by a call, whose value the file does not spell out; one built from other constants, whose
    edits it does not see; one reached through a local or a helper rather than named in an assertion; one whose name
    the test also binds; and a fixture's return value."""
    by_name: dict[str, _PythonConstant]
    # The dotted names a test can reach the module itself by, to name a constant as module.NAME: the module's dotted
    # name and each suffix of it that ends at the file's own name, and the name a top-level import statement of one
    # binds when nothing else in the module binds it.
    module: frozenset[str]

    @classmethod
    def of(cls, tree: ast.Module, path: str | None) -> "_PythonConstants":
        """The constants of tree, the test module at path, its dotted name taken from path; a path of None, a side
        the change has no file on, leaves only the bare names to reach them by."""
        bindings = _scope_bindings(tree.body)
        by_name: dict[str, _PythonConstant] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, expression = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target, expression = stmt.target, stmt.value
            else:
                continue
            if (isinstance(target, ast.Name) and bindings[target.id] == 1
                    and not any(isinstance(n, ast.Call) for n in ast.walk(expression))):
                by_name[target.id] = _PythonConstant(stmt.lineno, _tree_fingerprint(expression))
        return cls(by_name, _module_spellings(tree, path, bindings))

    def asserted(self, test: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
                 assertions: list[ast.Assert | ast.Call]) -> dict[str, int]:
        """Each name that assertions, test's as _python_assertions gives them, read as the module's, bare or as
        module.NAME, to the line of the first that does, whether or not it is one of the constants. A name test binds
        anywhere, as _names_bound_in says, is never the module's, bare or as module.NAME's first part."""
        own = _names_bound_in(test)
        out: dict[str, int] = {}
        for assertion in assertions:
            for n in ast.walk(assertion):
                if isinstance(n, ast.Name) and n.id not in own:
                    out.setdefault(n.id, assertion.lineno)
                elif isinstance(n, ast.Attribute):
                    root, dotted = _dotted_name(n.value) or (None, "")
                    if root is not None and root.id not in own and dotted in self.module:
                        out.setdefault(n.attr, assertion.lineno)
        return out


def _module_spellings(tree: ast.Module, path: str | None, bindings: collections.Counter[str]) -> frozenset[str]:
    """The dotted names a test of tree, the module at path, can reach the module itself by, as
    _PythonConstants.module holds them, given bindings, the module scope's as _scope_bindings counts them; none for a
    path of None."""
    parts = os.path.splitext(path)[0].split("/") if path else []
    spellings = {".".join(parts[i:]) for i in range(len(parts))}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            spellings |= {a.asname for a in stmt.names if a.asname and a.name in spellings}
        elif isinstance(stmt, ast.ImportFrom) and stmt.module and not stmt.level:
            spellings |= {a.asname or a.name for a in stmt.names if f"{stmt.module}.{a.name}" in spellings}
    return frozenset(n for n in spellings if "." in n or bindings[n] <= 1)


def _changed_constants(path: str | None, name: str,
                       was: tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, list[ast.Assert | ast.Call]],
                       now: tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, list[ast.Assert | ast.Call]],
                       base: _PythonConstants, current: _PythonConstants) -> list[dict]:
    """The expected-value-changed findings, as GateCheck.find returns them, on the test called name that the change
    kept in the file at path, for the constants its assertions name. was and now are the test at the base and in
    the working tree, each as its def and its assertions as _python_assertions gives them; base and current are its
    file's constants on the two sides. One finding per constant of the base that the assertions
    of both sides name and that is now either no constant or one whose expression differs, other than in layout or
    comments; it is at the first assertion now naming it."""
    at_base = base.asserted(*was)
    out = []
    for constant, line in current.asserted(*now).items():
        if constant not in at_base or constant not in base.by_name:
            continue
        defined = current.by_name.get(constant)
        if defined is None:
            where = f"line {base.by_name[constant].line} at the base"
            what = "is no longer a constant of the file that this check can read"
        elif defined.fingerprint != base.by_name[constant].fingerprint:
            where, what = f"line {defined.line}", "changed"
        else:
            continue
        out.append(_finding(path, line, "expected-value-changed",
                            f"`{constant}` ({where}), which `{name}` names in its assertions, {what}, "
                            f"in a change that also edits the code under test.", severity="error"))
    return out


# The nodes that open a scope of their own inside a module or a function.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
           ast.GeneratorExp)


def _scope_bindings(nodes: Iterable[ast.AST]) -> collections.Counter[str]:
    """How many times the scope whose code is nodes, a module's or a function's statements and parameters, binds each
    name, a binding being what _BINDING_NAME says. A def's or class's name counts, but what it binds inside does not,
    nor what a lambda or a comprehension binds, a := in a comprehension included, though Python binds that in the
    enclosing scope; an attribute assigned to binds no name."""
    out: collections.Counter[str] = collections.Counter()
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if not isinstance(node, ast.Attribute):
            name = _BINDING_NAME.get(type(node), lambda node: None)(node)
            if name:
                out[name] += 1
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))
    return out


def _dotted_name(node: ast.AST) -> tuple[ast.Name, str] | None:
    """When node is a Name or a chain of attributes on one, that Name and node as a dotted name, such as a.b.c; else
    None."""
    attrs = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    return (node, ".".join([node.id, *reversed(attrs)])) if isinstance(node, ast.Name) else None


def _names_bound_in(node: ast.AST) -> set[str]:
    """Every name node binds anywhere inside it, whatever the scope: by assignment of any kind, as a parameter of it
    or of a def or lambda in it, by import, as a for, with, except, comprehension or match target, by def or class,
    or by naming it in global, nonlocal or del. An attribute assigned to binds no name."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Del):
            out.add(n.id)
        elif not isinstance(n, ast.Attribute) and (name := _BINDING_NAME.get(type(n), lambda n: None)(n)):
            out.add(name)
    return out


def _shape(node: ast.AST) -> str:
    """node with every literal blanked, so that two assertions differing only in literals have one shape."""
    return _tree_fingerprint(node, blank_literals=True)


def _literals(node: ast.AST) -> list:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)]


def _code(node: ast.AST) -> str:
    """node as Python source, for a finding's message: its first line, clipped; or a note that it is too deep or too
    long to print, as _unparsed says."""
    text, why = _unparsed(node)
    if why:
        return f"(code at line {getattr(node, 'lineno', '?')}, {why} to print)"
    return _clip(text.splitlines()[0] if text else text)


def _unparsed(node: ast.AST) -> tuple[str, str]:
    """node as Python source, as ast.unparse writes it, and ""; or "" and why it cannot be written, "too deep" or "too
    long". ast.unparse recurses as deep as the tree goes, and so overflows the stack on valid code such as a sum of a
    few thousand terms; and it writes an int with repr, which refuses one of more than sys.get_int_max_str_digits()
    decimal digits."""
    try:
        return ast.unparse(node), ""
    except RecursionError:
        return "", "too deep"
    except ValueError:
        return "", "too long"


def _clip(text: str, n: int = 160) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


WEAKENED_TESTS_DETECTORS = (
    WeakenedTestsDetector("python", is_test=_python_test_file, is_source=lambda path: path.endswith(".py"),
                          find=python_weakened_tests),
)


# Flaky tests: the tests a change adds or edits that pass in one order and fail in another, which the build's one
# green run does not show.

def flaky_tests(change: GateChange) -> list[dict]:
    """The flaky-tests check's find: reruns the Python tests the change adds or removes lines of, decorators
    included, once under each of FLAKY_SEEDS with pytest-randomly, and reports at its def each test whose outcome,
    failed, xfailed (failed as its xfail mark expects) or passed, differs between seeds, naming the seeds of each. A
    test that failed under every seed that judged it, at least two, is not flaky, but is a finding of its own: the
    build may fail it too, which the failing-tests section lists, or it may need what the copy below lacks, so that
    the reruns could not judge it. One that xfails under every seed is not reported. The change leaves out mutmut's
    state, as GateChange.from_tree says, so no test there is rerun.

    The tests run in a temporary copy of the files git tracks, so that what the project's addopts write, such as a
    coverage report, does not replace the build's own; a test that needs an untracked file, such as a build product,
    cannot find it there. A run that skips a test, or does not report it, as when the project's addopts stop at the
    first failure, neither passed nor failed it. A test that is not mixed, which fewer than two runs passed or
    failed, is reported as one that too few runs judged, whether runs skipped it or the reruns stopped early; when
    no run told anything, the finding about the run that went wrong stands for them all.

    pytest decides which of those defs are tests, so a helper named test_* that it does not collect is not rerun.
    A changed path that is a symlink, a submodule or a special file holds no test the change edits, and is not read
    (_is_link_or_special): no test is rerun through a symlink, whose target is rerun only as a file the change edits.
    pytest and pytest-randomly must be importable by the gate's own interpreter, as a `uv run` of the project makes
    them. A pytest run that cannot start, takes longer than FLAKY_RUN_TIMEOUT, ends in a usage or collection error
    or writes no readable report ends the reruns of the tests under its configuration with a finding saying so, after
    what the runs before it showed, and the tests under other configurations are rerun still; a changed test file
    that cannot be read or parsed, and a copy that cannot be made, are findings too; a copy that cannot be removed
    afterwards is a finding of its own, beside the results. Each run has FLAKY_RUN_TIMEOUT of its own, so each
    configuration the changed tests fall under adds its runs to the check's time.
    """
    tests, unreadable = _changed_python_tests(change)
    out = [_finding(path, None, "not-rerun", f"{why}, so its tests were not rerun.") for path, why in unreadable]
    if tests:
        out += _in_scratch_directory("not-rerun", "to rerun the changed tests in",
                                     lambda tmp: _rerun_findings(change.root, tmp, tests))
    return sorted(out, key=lambda f: (f["path"] or "", f["line"] or 0))


def _rerun_findings(root: str, tmp: str, tests: dict[tuple[str, str], int]) -> list[dict]:
    """flaky_tests' findings on tests, as _changed_python_tests gives them, rerun in a copy of root's tracked files
    that it makes in tmp, an empty directory, beside the runs' reports: once for each pytest configuration the
    changed test files take (_pytest_groups), so that a group that cannot run leaves the others' findings."""
    tree = os.path.join(tmp, "tree")
    try:
        _copy_tracked(root, tree)
    except (OSError, GateError) as e:
        return [_finding(None, None, "not-rerun", f"could not copy the tracked files to rerun the changed "
                                                  f"tests in: {e}; the changed tests were not rerun.")]
    try:
        groups = _pytest_groups(tree, tmp, {path for path, _ in tests})
    except OSError as e:
        return [_finding(None, None, "not-rerun", f"could not write an empty pytest configuration for the "
                                                  f"reruns: {e}; the changed tests were not rerun.")]
    out = []
    for n, (group, label) in enumerate(zip(groups, _labels(groups), strict=True)):
        reports = os.path.join(tmp, f"reports-{n}")
        try:
            os.mkdir(reports)
        except OSError as e:
            out.append(_finding(None, None, "not-rerun", f"could not make a directory for the reruns' reports: {e}; "
                                                         f"{label or 'the changed tests'} were not rerun."))
            continue
        out += [group.in_tree(f) for f in _group_rerun_findings(group, group.own(tests), reports, label)]
    return out


def _group_rerun_findings(group: "_PytestGroup", tests: dict[tuple[str, str], int], reports: str,
                          label: str | None) -> list[dict]:
    """_rerun_findings' findings on tests, keyed by their paths from group's directory, as pytest names them there,
    rerun by group's configuration, with the runs' reports in reports, an empty directory; their paths are from
    group's directory too. label names the group's tests in a finding about all of them, as _PytestGroup.label
    does; None when they are all the change's tests."""
    ids, problem = _collect_python_tests(group.setup, tests)
    reruns = _Reruns.run(group.setup, ids, reports)
    problem = problem or reruns.problem
    out = []
    for i in reruns.outcomes:
        path, names, _ = _node_parts(i)
        if judged := reruns.judge(i):
            out.append(_finding(path, tests[(path, ".".join(names))], *judged))
    if problem:
        out.append(_finding(None, None, "not-rerun", f"{problem}; " + (
            f"only the reruns{f' of {label}' if label else ''} under {_seeds(reruns.seeds)} were judged."
            if reruns.seeds else f"{label or 'the changed tests'} were not rerun.")))
    return out


@dataclass(frozen=True)
class _Reruns:
    """What flaky_tests' reruns of a set of tests, one under each of FLAKY_SEEDS in turn, told of each test."""
    outcomes: dict[str, dict[int, str]]  # each test's outcome by the seed of a run that reported it passed or failed
    seeds: list[int]  # the seeds of the runs that told something, in the order they ran
    stopped: int | None  # the seed whose run went wrong and ended the reruns; None when every run told something
    problem: str | None  # what went wrong with the run under stopped, a clause; None with it

    @classmethod
    def run(cls, setup: "_PytestSetup", ids: list[str], reports: str) -> "_Reruns":
        """Rerun the tests of node ids ids by setup, once under each of FLAKY_SEEDS, until a run goes wrong, writing
        the runs' reports in reports. For no ids, no run."""
        outcomes: dict[str, dict[int, str]] = {i: {} for i in ids}
        seeds: list[int] = []
        for seed in FLAKY_SEEDS if ids else ():
            reported, problem = _rerun_python_tests(setup, ids, seed, os.path.join(reports, f"seed-{seed}.xml"))
            if problem:
                return cls(outcomes, seeds, seed, problem)
            seeds.append(seed)
            for i, outcome in reported.items():
                if outcome != "skipped":
                    outcomes[i][seed] = outcome
        return cls(outcomes, seeds, None, None)

    def judge(self, test_id: str) -> tuple[str, str] | None:
        """The rule and message of flaky_tests' finding on the test of node id test_id, one of outcomes', by what the
        reruns told of it; None when they show nothing to report."""
        _, names, params = _node_parts(test_id)
        name = ".".join(names) + params
        by_seed = self.outcomes[test_id]
        # What it did under which seeds, for each outcome it had.
        did = [f"{_OUTCOME_VERBS[o]} under {_seeds(under)}" for o in _OUTCOME_VERBS
               if (under := [s for s in self.seeds if by_seed.get(s) == o])]
        unjudged = [s for s in self.seeds if s not in by_seed]
        if len(did) > 1:
            return "mixed-results", (
                f"`{name}` {_series(did)}"
                + (f", and was skipped or not run under {_seeds(unjudged)}" if unjudged else "")
                + ": it depends on the order the tests run in, or on state or randomness that one run does not share "
                  "with another.")
        if self.seeds and len(by_seed) < 2:
            # Without a run that told something, the finding about the run already says no test was rerun.
            return "not-rerun", (
                f"`{name}` " + (f"{did[0]} only" if did else "neither passed nor failed under any seed")
                + (f"; the reports of {_seeds(unjudged)} have it skipped or not at all" if unjudged else "")
                + (f"; the reruns stopped when the run under seed {self.stopped} went wrong" if self.stopped else "")
                + ", so too few runs tell whether it is flaky.")
        if len(by_seed) >= 2 and set(by_seed.values()) == {"failed"}:
            # Not flaky, but the gate cannot tell a test the build fails too from one that needs what the copy lacks.
            return "failed-every-run", (
                f"`{name}` failed under {_seeds(list(by_seed))}, every rerun"
                + (f" that did not skip it or leave it out ({_seeds(unjudged)} did)" if unjudged else "")
                + ", which ran in a copy of the tracked files. If the build failed it too, the failing tests list it. "
                  "If the build passed it, it needs something the copy lacks, such as git's metadata, an untracked or "
                  "ignored file or a build product, so whether it is flaky could not be judged here.")
        return None


def _pytest_inifile(tree: str) -> str | None:
    """The file in the top directory of tree that pytest 9 takes its configuration from when run there: the first of
    pytest.toml, .pytest.toml, pytest.ini and .pytest.ini, a pyproject.toml with a [tool.pytest] table, a tox.ini
    with a [pytest] section and a setup.cfg with [tool:pytest], as pytest's locate_config orders them. None when it
    has none. A candidate that cannot be read or parsed is taken, so that pytest reports what is wrong with it."""
    for name in ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"):
        path = os.path.join(tree, name)
        if not os.path.isfile(path):
            continue
        if name in ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini"):
            return path
        try:
            text = _read_text(path)
            if name == "pyproject.toml":
                # An empty [tool.pytest] table is not one; an empty [tool.pytest.ini_options] is, and fills it.
                if tomllib.loads(text).get("tool", {}).get("pytest"):
                    return path
            elif {"tox.ini": "pytest", "setup.cfg": "tool:pytest"}[name] in _ini_sections(text):
                return path
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return path
    return None


@dataclass(frozen=True)
class _PytestSetup:
    """Where, and by what configuration, the gate runs pytest in a copy of a tree: for _pytest."""
    root: str  # pytest's rootdir, the configuration's directory, from which its node ids and reports name paths
    inifile: str  # the configuration file, given with -c, so that pytest looks for none above the copy
    # The copy's top directory: where pytest runs, as the gate always ran it, so that `python -m` puts it on the
    # path; and above which no conftest.py is imported (--confcutdir).
    top: str

    @property
    def where(self) -> str:
        """root, by its path in the copy: "" for the top."""
        where = os.path.relpath(self.root, self.top)
        return "" if where == "." else where

    def argument(self, name: str) -> str:
        """name, a test file's path or a node id from root, as pytest is to be given it, run from top."""
        return os.path.join(self.where, name) if self.where else name


@dataclass(frozen=True)
class _PytestGroup:
    """Changed test files that take one pytest configuration, as _pytest_groups finds them."""
    setup: _PytestSetup  # how to run them, rooted at the configuration's directory
    paths: frozenset[str]  # the files, by their paths in the copy

    def own(self, tests: dict[tuple[str, str], int]) -> dict[tuple[str, str], int]:
        """Those of tests, keyed by (path in the copy, qualified name), whose files are the group's, keyed by their
        paths from the group's directory, as pytest names them run there."""
        where = self.setup.where
        return {(os.path.relpath(path, where) if where else path, name): line
                for (path, name), line in tests.items() if path in self.paths}

    def in_tree(self, finding: dict) -> dict:
        """finding, its path one from the group's directory or None, with its path in the copy instead."""
        path, where = finding["path"], self.setup.where
        return {**finding, "path": os.path.join(where, path) if path and where else path}

    def label(self) -> str:
        """The group's tests, as a finding about all of them names them when the change's tests fall in more than
        one group: by their configuration file's path in the copy."""
        # The empty configuration is the one file given with -c that lies outside the copy (_empty_pytest_ini).
        if os.path.commonpath([self.setup.inifile, self.setup.top]) != self.setup.top:
            return "the changed tests with no pytest configuration"
        return f"the changed tests configured by {os.path.relpath(self.setup.inifile, self.setup.top)}"


def _labels(groups: list[_PytestGroup]) -> list[str | None]:
    """How a finding about all of each of groups' tests names them, as _PytestGroup.label does when there is more
    than one group; None for each when there is one, whose tests are all the changed tests."""
    return [group.label() if len(groups) > 1 else None for group in groups]


def _pytest_groups(tree: str, tmp: str, paths: set[str]) -> list[_PytestGroup]:
    """paths, test files by their paths in tree, a copy of a tree, grouped by the pytest configuration pytest takes
    for each: the one _pytest_inifile finds in the file's directory, else in the nearest directory above it that has
    one, up to tree's top and never above it, as pytest 9's locate_config walks up from the files it is given (its
    _pytest/config/findpaths.py). In the order of their directories. Files with none share the empty configuration
    _empty_pytest_ini writes in tmp. Raises OSError as that does."""
    seen: dict[str, str | None] = {}  # each directory read, by its path in tree, and the configuration it holds

    def inifile_in(where: str) -> str | None:
        if where not in seen:
            seen[where] = _pytest_inifile(os.path.join(tree, where))
        return seen[where]

    found: dict[str, tuple[str | None, set[str]]] = {}
    for path in sorted(paths):
        where = os.path.dirname(path)
        while (project_ini := inifile_in(where)) is None and where:
            where = os.path.dirname(where)
        found.setdefault(where if project_ini else "", (project_ini, set()))[1].add(path)
    return [_PytestGroup(_PytestSetup(os.path.join(tree, where) if where else tree,
                                      project_ini or _empty_pytest_ini(tmp), tree), frozenset(members))
            for where, (project_ini, members) in sorted(found.items())]


def _pytest_config(tree: str, tmp: str) -> str:
    """The pytest configuration to run all the tests of tree, a copy of a tree, by from its top, as surviving-mutants
    does: its own, as _pytest_inifile finds it, else the empty configuration _empty_pytest_ini writes in tmp. Raises
    OSError as that does."""
    return _pytest_inifile(tree) or _empty_pytest_ini(tmp)


def _empty_pytest_ini(tmp: str) -> str:
    """The path of an empty pytest.ini that this writes in tmp, the private directory beside a copy, where none is
    yet, so that pytest, given it with -c, does not look for a configuration above the copy, where another account
    may write. Raises OSError when it cannot be written, or tmp has one already."""
    inifile = os.path.join(tmp, "pytest.ini")
    with open(inifile, "x", encoding="utf-8"):
        pass
    return inifile


def _ini_sections(text: str) -> list[str]:
    """The names of an ini file's sections, as its [name] lines give them."""
    return re.findall(r"^\[\s*([^]\n]*?)\s*]", text, re.MULTILINE)


def _copy_tracked(root: str, dest: str) -> None:
    """Copy the files git tracks in the working tree at root, as they are there, to the same paths under dest, a
    directory it makes, with its parents. A symbolic link is copied as a link and a submodule as its directory; a
    tracked file missing from the working tree is left out. Raises GateError when git cannot list the files,
    and OSError when dest exists or a file cannot be copied; dest may then hold part of the copy."""
    names = sorted({n for n in _git(root, "ls-files", "-z", verbatim=True).split("\0") if n})
    os.makedirs(dest)
    for name in names:
        src, dst = os.path.join(root, name), os.path.join(dest, name)
        if not os.path.lexists(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)


def _changed_python_tests(change: GateChange) -> tuple[dict[tuple[str, str], int], list[tuple[str, str]]]:
    """The test functions and methods the change added lines to or removed lines from, decorators included, in the
    test files it left in the working tree: (path, qualified name as _python_tests gives it) to the line of its def.
    And (path, why) for each such file that could not be read or parsed, as _changed_python_units gives them. Which
    lines count is as _edits_test says."""
    functions, unreadable = _changed_python_units(change, _python_test_file, _edits_test)
    return {(f.path, f.name): f.line for f in functions if f.name.rpartition(".")[2].startswith("test")}, unreadable


def _edits_test(f: _ChangedFunction, added: set[int], removed: set[int]) -> bool:
    """Whether the change added lines to f, a test, or removed lines from it, as _changed_python_units's touched.
    Its lines run from the line after the statement before it, f.floor, to its last: what lies above its def, such
    as its decorators and comments, may have changed it, and a decorator replaced or removed leaves no line of the
    def itself changed. Lines removed right after its last line count too, though they may have been the blank lines
    below it."""
    return any(f.floor < n <= f.last for n in added) or any(f.floor <= n <= f.last for n in removed)


def _node_parts(node_id: str) -> tuple[str, list[str], str]:
    """The path, the names after it and the parameters of a pytest node id, such as `tests/t.py`, ["TestX", "test_y"]
    and "[a::b]" for `tests/t.py::TestX::test_y[a::b]`; "" for a node without parameters. As pytest's JUnit report
    splits it: the parameters start at the first "[", and only the "::" before them separate names. ".".join of the
    names is the qualified name _python_tests gives the test."""
    head, bracket, params = node_id.partition("[")
    path, *names = head.split("::")
    return path, names, bracket + params


def _collect_python_tests(setup: _PytestSetup, tests: dict[tuple[str, str], int]) -> tuple[list[str], str | None]:
    """The pytest node ids of tests, keyed by their paths from setup's root, one per parameter set, collected by
    setup, and the problem, a clause, when pytest could not collect them; then the ids are []."""
    files = sorted({path for path, _ in tests})
    # Exit status 5: pytest collected no test from the files, as for a file of helpers named test_*.
    # Without pytest-randomly, which only shuffles the list, and cannot hash a node id that is not UTF-8.
    proc, problem = _pytest(setup, "collecting the changed tests",
                            ["-p", "no:randomly", "--collect-only", *map(setup.argument, files)], ok=(0, 5),
                            timeout=FLAKY_RUN_TIMEOUT)
    if problem or proc is None:
        return [], problem
    return _listed_tests(proc, "collecting the changed tests", tests)


def _listed_tests(proc: subprocess.CompletedProcess, doing: str,
                  tests: dict[tuple[str, str], int]) -> tuple[list[str], str | None]:
    """The node ids, one per parameter set and sorted, of those of tests, keyed as _changed_python_tests keys them,
    that proc, a `pytest --collect-only` run by _pytest, listed. And the problem, a clause naming doing, when it exited
    with status 0 but listed no node id in a form the gate can read; then the ids are []."""
    files = {path for path, _ in tests}
    # At verbosity -1 pytest lists one node id a line, unindented, before its summary; a warnings summary repeats
    # some, indented or not. A collection that found tests but listed none in that form cannot be judged.
    ids = {line for line in proc.stdout.split("\n") if "::" in line and _node_parts(line)[0] in files}
    if proc.returncode == 0 and not ids:
        return [], f"pytest, {doing}, printed no node id the gate could read: {_said(proc)}"
    wanted = []
    for i in sorted(ids):
        path, names, _ = _node_parts(i)
        if (path, ".".join(names)) in tests:
            wanted.append(i)
    return wanted, None


def _rerun_python_tests(setup: _PytestSetup, ids: list[str], seed: int,
                        report: str) -> tuple[dict[str, str], str | None]:
    """The outcome, as junit_outcomes gives it, of each of ids that pytest reported when it ran them by setup, in the
    order pytest-randomly's seed gives, writing its JUnit report to report; a
    test reported more than once takes the first of failed, xfailed and passed that any of its cases has, else
    skipped. And the problem, a clause, when that run told nothing, then with {}."""
    doing = f"rerunning them under seed {seed}"
    proc, problem = _pytest(setup, doing,
                            ["-p", "randomly", f"--randomly-seed={seed}", f"--junitxml={report}",
                             *map(setup.argument, ids)],
                            timeout=FLAKY_RUN_TIMEOUT)
    if problem or proc is None:
        return {}, problem
    if not os.path.isfile(report):
        return {}, f"pytest, {doing}, wrote no test report: {_said(proc)}"
    try:
        cases = junit_outcomes(report)
    except GateError as e:
        return {}, f"pytest, {doing}, wrote a report the gate cannot read: {e}"
    by_case = {_junit_case(i): i for i in ids}
    rank = ("skipped", "passed", "xfailed", "failed")
    out: dict[str, str] = {}
    for name, outcome, _ in cases:
        if (i := by_case.get(name)) is not None and rank.index(outcome) >= rank.index(out.get(i, "skipped")):
            out[i] = outcome
    return out, None


def _junit_case(node_id: str) -> str:
    """How junit_outcomes names the test of a pytest node id: pytest's JUnit classname, the path dotted without .py
    and the classes after it, then a dot and the test's name with its parameters, as pytest's mangle_test_address
    makes them."""
    path, names, params = _node_parts(node_id)
    return ".".join([re.sub(r"\.py$", "", path).replace("/", "."), *names]) + params


def _run_in_session(argv: list[str], cwd: str, env: dict[str, str], *, timeout: float,
                    soft_stop: tuple[signal.Signals, float] | None = None) -> tuple[subprocess.CompletedProcess, bool]:
    """argv run at cwd with env as its whole environment and no input, in a session of its own, and whether it was
    stopped for running longer than timeout seconds.

    Stopped, it is killed, or with soft_stop, a (signal, grace) pair, first sent that signal and given grace seconds
    to exit; called on the main thread, it starts with that signal's default disposition even where this process
    ignores it (_not_ignored_in_children). Once it has exited, every process left in its process group, such as a
    server it started, is killed too, and what was printed is read until no process holds the output open, for at
    most TOOL_KILL_WAIT seconds more: only one outside the group, in a session of its own, can still hold it, and
    what that prints later is lost. So the call returns within timeout + grace + TOOL_KILL_WAIT seconds, the process
    reaped unless it could not exit within them. The output is decoded as _git_run's verbatim output is, so that
    paths in it keep their bytes; the exit status is -SIGKILL when the kill is the last thing known of it. Raises
    OSError when argv cannot be started.
    """
    # The callers' fixed commands, over names their tools listed; no shell reads them.
    with _not_ignored_in_children(soft_stop[0] if soft_stop else None):
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,  # noqa: S603
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    # Both, by the PIPEs above: standard output, then standard error.
    streams = [stream for stream in (proc.stdout, proc.stderr) if stream is not None]
    printed: dict[int, list[bytes]] = {stream.fileno(): [] for stream in streams}
    stopped = False
    try:
        with selectors.DefaultSelector() as pipes:
            for fd in printed:
                os.set_blocking(fd, False)
                pipes.register(fd, selectors.EVENT_READ)
            # Until the process exits, read what it prints, so that a full pipe never blocks it, polling for its exit
            # every _TOOL_POLL seconds; at the deadline stop it, and once a soft stop's grace has run out too, kill it.
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    if soft_stop and not stopped:
                        proc.send_signal(soft_stop[0])
                        deadline = time.monotonic() + soft_stop[1]
                        stopped = True
                        continue
                    stopped = True
                    break
                _read_ready(pipes, printed, min(_TOOL_POLL, deadline - time.monotonic()))
            _kill_group(proc)
            # What the group printed before the kill is in the pipes, ending once nothing holds them open.
            end = time.monotonic() + TOOL_KILL_WAIT
            while pipes.get_map() and (left := end - time.monotonic()) > 0:
                _read_ready(pipes, printed, left)
            # Killed a moment ago, the process may not have been reaped yet; what is left of the read's time is its
            # to exit in (a wait of 0 still reaps one that has), and one still there after it is left to a later Popen.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=max(0.0, end - time.monotonic()))
    finally:
        _kill_group(proc)
        for stream in streams:
            stream.close()
    returncode = proc.poll()
    out, err = (os.fsdecode(b"".join(chunks)) for chunks in printed.values())
    return subprocess.CompletedProcess(argv, -signal.SIGKILL if returncode is None else returncode, out, err), stopped


# Seconds between two of _run_in_session's checks of whether its process has exited, so the most it waits on past
# the exit before it reads the rest of the output.
_TOOL_POLL = 0.05


def _read_ready(pipes: selectors.BaseSelector, printed: dict[int, list[bytes]], timeout: float) -> None:
    """Appends to printed, by file descriptor, what each of pipes, non-blocking, has to read within timeout seconds,
    and unregisters a pipe at its end."""
    for key, _ in pipes.select(max(0.0, timeout)):
        try:
            chunk = os.read(key.fd, 65536)
        except BlockingIOError:
            continue
        if chunk:
            printed[key.fd].append(chunk)
        else:
            pipes.unregister(key.fd)


@contextlib.contextmanager
def _not_ignored_in_children(sig: signal.Signals | None) -> Iterator[None]:
    """While open, a process this one starts gets sig with its default disposition, also when this process ignores
    it, as a command a Jenkins sh step runs ignores SIGINT; afterwards this process treats sig as it did before. The
    disposition is the process's, so a child another thread starts meanwhile gets it too. An
    ignored signal stays ignored across exec, and a Python started so never raises KeyboardInterrupt, so a soft stop
    by it would reach a tool that ignores it. Meanwhile sig is caught and dropped here, as ignoring it would. Only the
    main thread can set a handler, so elsewhere, and for sig None, nothing changes."""
    if (sig is None or signal.getsignal(sig) != signal.SIG_IGN
            or threading.current_thread() is not threading.main_thread()):
        yield
        return
    # A handler is reset to the default on exec, which ignoring is not.
    signal.signal(sig, lambda *_: None)
    try:
        yield
    finally:
        signal.signal(sig, signal.SIG_IGN)


def _kill_group(proc: subprocess.Popen) -> None:
    """Kills every process in proc's process group, if any is left. macOS refuses with EPERM a group of zombies."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


class _PytestStopped(subprocess.CompletedProcess):
    """What a pytest run that _pytest stopped at its timeout printed until then; its returncode is the kill's."""


def _pytest(setup: _PytestSetup, doing: str, args: list[str], *, timeout: float, ok: tuple[int, ...] = (0, 1),
            env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess | None, str | None]:
    """pytest run at verbosity -1, for test cases too, in the gate's own interpreter at setup's root, without its
    cache, with setup's configuration and args, taking no configuration from anywhere else and no conftest.py from
    above setup's top, and env's
    variables beside tool_env's; its output unbuffered, so that what it printed before a kill is whole. And the
    problem, a clause naming doing, when it could not start, took longer than timeout seconds or exited with a
    status outside ok (by default 0, all passed, and 1, some failed); then the run is None when pytest did not start,
    a _PytestStopped when timeout stopped it, and else the run that exited so. pytest runs, is stopped and is read
    as _run_in_session runs a tool, killed at once. pytest writes a name that is not UTF-8 as its bytes, so the node
    ids in its output hold paths as git's do, and only "\n" ends a line. pytest-randomly 5.0.0 cannot shuffle such a
    name: under -p randomly pytest stops with an internal error."""
    # --verbosity sets the level outright, and pytest reads the project's addopts and PYTEST_ADDOPTS before the
    # command line, so a -v or -q of theirs cannot change what the collection prints. The list of collected tests
    # follows verbosity_test_cases where the project's configuration sets it, and -o overrides that; in pytest 9.0
    # no other option changes the list.
    # Without -c pytest looks for its configuration in every directory above the test files, and the copy is in a
    # temporary directory another account may write to, along with a conftest.py beside it: -c ends that search,
    # and --confcutdir keeps conftest.py files above the copy from being imported.
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-c", setup.inifile, "--rootdir", setup.root,
            "--confcutdir", setup.top, "--verbosity=-1", "-o", "verbosity_test_cases=-1", *args]
    try:
        proc, stopped = _run_in_session(argv, setup.top, {**tool_env(), **(env or {}), "PYTHONUNBUFFERED": "1",
                                                          "PYTHONIOENCODING": "utf-8:surrogateescape"},
                                        timeout=timeout)
    except OSError as e:
        return None, f"could not start pytest, {doing}: {e}"
    if stopped:
        return (_PytestStopped(argv, proc.returncode, proc.stdout, proc.stderr),
                f"pytest, {doing}, did not finish within {timeout:g} seconds")
    if proc.returncode not in ok:
        return proc, f"pytest, {doing}, exited with status {proc.returncode}: {_said(proc)}"
    return proc, None


def _said(proc: subprocess.CompletedProcess) -> str:
    """What a tool that went wrong, such as pytest or mutmut, said about it, in a line: the exception of a pytest
    internal error, else the exception that ended it when it printed a traceback, else the first line of its
    standard error, else the last line of its output. Only "\n" ends a line, as in the verbatim output of _git_run,
    so a path in it stays whole."""
    err = [line.strip() for line in proc.stderr.split("\n") if line.strip()]
    out = [line.strip() for line in proc.stdout.split("\n") if line.strip()]
    internal = [line.removeprefix("INTERNALERROR>").strip() for line in out if line.startswith("INTERNALERROR>")]
    return _clip(internal[-1] if internal else err[-1] if err and "Traceback" in proc.stderr else err[0] if err
                 else out[-1] if out else "nothing")


# How a mixed-results finding says what a test did under some seeds, by its outcome as junit_outcomes gives it; a
# skip judges nothing, so it has none.
_OUTCOME_VERBS = {"failed": "failed", "xfailed": "failed as its xfail mark expects", "passed": "passed"}


def _seeds(seeds: list[int]) -> str:
    """'seed 3', 'seeds 2 and 4' or 'seeds 1, 3 and 5'."""
    return f"seed{'s' if len(seeds) > 1 else ''} {_series([str(s) for s in seeds])}"


def _series(items: list[str], limit: int | None = None) -> str:
    """items as a list in prose: "a", "a and b" or "a, b and c"; "" for none. With limit, at most that many items are
    named, then how many are not, as "a, b and 3 more"."""
    if limit is not None and len(items) > limit:
        items = [*items[:limit], f"{len(items) - limit} more"]
    return " and ".join([", ".join(items[:-1]), items[-1]]) if len(items) > 1 else "".join(items)


# Tests that pass at the base: a test the change adds or edits that passes without the rest of the change, so that
# it cannot be what shows the change works, though coverage counts the lines it runs.

def passes_at_base(change: GateChange, *, budget: float) -> list[dict]:
    """The passes-at-base check's find: runs the Python tests the change adds or edits, as flaky_tests picks them,
    against the base's code, and reports at its def each one that passed there. Runs only when the change edits Python
    source outside the tests, as _edits_python_source says: a change to tests alone has no "without the change" to run
    them in, and one to docs alone none that a test could see.

    The tests run with pytest, in the gate's own interpreter, in the files git tracked at the base, as `git archive`
    writes them (so a path the base's .gitattributes marks export-ignore is missing there), each submodule filled from
    its own repository in the working tree at the commit the base records, or left empty, which a not-run finding names,
    with every file of the tests the change touches, conftest.py and data included, taken from the working tree instead,
    or removed where the change removed it. pytest first collects the changed test files there, then runs those of the
    changed tests it collected. A test passes there when pytest passed each of its cases that it did not skip, all its
    parameter sets included. One that fails or errors there tests the change, which is what the check expects, and gives
    nothing; so does one that pytest does not collect there: a test whose file cannot be imported at the base, or that
    lies under a conftest.py that fails to load there through a file the change edits, itself or one it imports,
    whatever it raises and whatever the test itself checks, and a test_* method of a class pytest does not take for a
    test class. The base's code is what the copy's import roots, its top and its src/, and its own configuration, such
    as pytest's pythonpath, import, ahead of what is installed into the gate's interpreter, an editable install of the
    project included. A run that still imports the project's code from the working tree, whether a changed test, a
    conftest.py or a helper imports it, as from a package an editable install finds outside those roots, judges none
    of its tests and is a not-run finding naming the package.

    The collections and runs together, one of each for every configuration the changed tests fall under, may take budget
    seconds; a stop there is a budget-reached finding saying when, and how many test cases the run had run, and none of
    that configuration's outcomes is reported; the configurations after it are not run, each a not-run finding saying
    so. A pytest that cannot start, stops before it collects, fails to load a conftest.py through no file the change
    edits, stops with another usage error, or writes no readable report, a changed test file that cannot be read or
    parsed, and a copy that cannot be made are not-run findings saying so; a copy that cannot be removed afterwards is a
    finding of its own.
    """
    if not _edits_python_source(change):
        return []
    tests, unreadable = _changed_python_tests(change)
    out = [_finding(path, None, "not-run", f"{why}, so its tests were not run at the base.")
           for path, why in unreadable]
    if tests:
        out += _in_scratch_directory("not-run", "to run the changed tests at the base in",
                                     lambda tmp: _base_run_findings(change, tmp, tests, budget))
    return sorted(out, key=lambda f: (f["path"] or "", f["line"] or 0))


def _edits_python_source(change: GateChange) -> bool:
    """Whether the change edits Python source outside the tests, as _beside_tests tells them: a file it leaves in the
    working tree that _is_python_source accepts, or a .py file it deletes."""
    return any((new is not None and not _beside_tests(new) and _is_python_source(change, new))
               or (new is None and old is not None and old.endswith(".py") and not _beside_tests(old))
               for old, new in change.files)


def _beside_tests(path: str) -> bool:
    """Whether a repository path belongs with the tests rather than with the code they test, as passes_at_base reads
    it: a Python test file (_python_test_file), a conftest.py, or any file under a tests directory, such as a fixture's
    data."""
    *dirs, name = path.split("/")
    return _python_test_file(path) or name == "conftest.py" or "tests" in dirs


@dataclass(frozen=True)
class _BaseCopy:
    """The copy of the base that one passes_at_base call runs the changed tests in, and what each of its pytest runs
    there shares, as _base_run_findings sets them up."""
    change: GateChange
    tree: str  # the copy's top, every group's top
    env: dict[str, str]  # the variables each pytest run takes beside tool_env's
    deadline: float  # the time.monotonic() time by which every collection and run must be done
    budget: float  # the seconds the check had for all of them, from when the copy was made, as findings name it


# The pytest plugin, a module of this name written beside the copy and put on the runs' path, by which a run at the
# base tells where the modules it imported came from. At the end of the session it writes, as JSON to the file
# --gate-imports-report names, each module in sys.modules whose __file__ lies under the directory --gate-imports-root
# names, by its name, to that file's path from there. Under pytest-xdist each worker, which imports the tests where
# the controller does not, writes its own, the file's name followed by "-" and the worker's id, as xdist gives the
# worker its config.workerinput; not PYTEST_XDIST_WORKER, which a pytest the gate runs from inside a worker of its own
# inherits. The file's directory is resolved, and the root, so that a link on the way to either changes nothing; the
# file itself is not, so that a module that is a link is named by the link. A namespace package has no __file__, and
# none is written.
_BASE_IMPORTS_PLUGIN = "_octoflow_gate_base_imports"
_BASE_IMPORTS_PLUGIN_SOURCE = """\
import json
import os
import sys


def pytest_addoption(parser):
    parser.addoption("--gate-imports-root")
    parser.addoption("--gate-imports-report")


def pytest_sessionfinish(session):
    root = os.path.realpath(session.config.getoption("gate_imports_root"))
    found = {}
    for name, module in list(sys.modules.items()):
        try:
            where = getattr(module, "__file__", None)
        except Exception:
            continue
        if isinstance(where, str):
            real = os.path.join(os.path.realpath(os.path.dirname(where)), os.path.basename(where))
            if real.startswith(root + os.sep):
                found[name] = os.path.relpath(real, root)
    report = session.config.getoption("gate_imports_report")
    worker = getattr(session.config, "workerinput", {}).get("workerid")
    with open(f"{report}-{worker}" if worker else report, "w", encoding="utf-8") as f:
        json.dump(found, f)
"""


def _base_run_findings(change: GateChange, tmp: str, tests: dict[tuple[str, str], int], budget: float) -> list[dict]:
    """passes_at_base's findings on tests, as _changed_python_tests gives them, collected and then run, within budget
    seconds for all of it, in a copy of the base that it makes in tmp, an empty directory, beside the runs' reports,
    their TMPDIR and the plugin _BASE_IMPORTS_PLUGIN names: once for each pytest configuration the changed test files
    take there (_pytest_groups), one after another while the budget lasts.

    The runs import the base's code from the copy's import roots, its top and its src/ when it has one, which are put
    on PYTHONPATH ahead of what was there: so they come before site-packages, and before what an editable install
    puts there, which would import the working tree's code."""
    tree, scratch, plugins = os.path.join(tmp, "tree"), os.path.join(tmp, "tmp"), os.path.join(tmp, "plugins")
    try:
        empty = _copy_base_with_tests(change, tree, os.path.join(tmp, "base.tar"))
        os.mkdir(scratch)
        os.mkdir(plugins)
        with open(os.path.join(plugins, f"{_BASE_IMPORTS_PLUGIN}.py"), "x", encoding="utf-8") as f:
            f.write(_BASE_IMPORTS_PLUGIN_SOURCE)
    except (OSError, tarfile.TarError, GateError) as e:
        return [_finding(None, None, "not-run", f"could not copy the base to run the changed tests in: {e}; the "
                                                f"changed tests were not run there.")]
    try:
        groups = _pytest_groups(tree, tmp, {path for path, _ in tests})
    except OSError as e:
        return [_finding(None, None, "not-run", f"could not write an empty pytest configuration for the run at the "
                                                f"base: {e}; the changed tests were not run there.")]
    src = os.path.join(tree, "src")
    roots = [tree, *([src] if os.path.isdir(src) else []), plugins]
    inherited = [os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []
    # No bytecode: a test that imports through a tracked link out of the tree would write it there.
    env = {"TMPDIR": scratch, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": os.pathsep.join([*roots, *inherited])}
    base_copy = _BaseCopy(change, tree, env, time.monotonic() + budget, budget)
    out: list[dict] = []
    if empty:
        noun, pronoun = ("a submodule", "it") if len(empty) == 1 else ("submodules", "them")
        named = "; ".join(f"`{path}`, {why}" for path, why in empty)
        out.append(_finding(None, None, "not-run", f"the copy of the base leaves {noun} empty, so a changed test "
                                                f"that imports from {pronoun}, if any does, was not judged: {named}."))
    for n, (group, label) in enumerate(zip(groups, _labels(groups), strict=True)):
        found, stopped = _base_group_findings(base_copy, group, group.own(tests), os.path.join(tmp, f"base-{n}"),
                                              label)
        out += [group.in_tree(f) for f in found]
        if stopped:
            out += [_finding(None, None, "not-run", f"the run at the base used its {budget:g} seconds "
                                                    f"(`--base-run-budget`) before it reached {later.label()}, so "
                                                    f"they were not run there.") for later in groups[n + 1:]]
            break
    return out


def _base_group_findings(base_copy: _BaseCopy, group: _PytestGroup, tests: dict[tuple[str, str], int], reports: str,
                         label: str | None) -> tuple[list[dict], bool]:
    """_base_run_findings' findings on tests, keyed by their paths from group's directory, as pytest names them
    there, collected and run in base_copy by group's configuration, writing the run's reports to paths that start
    with reports; their paths are from group's directory too. And whether base_copy's deadline stopped it, which
    leaves no time for another group. label names the group's tests in a finding about all of them, as
    _PytestGroup.label does; None when they are all the change's tests. When the run imported first-party code from
    the working tree, as _imported_from_working_tree tells, no test is judged, and one not-run finding names it."""
    which = label or "the changed tests"
    ids, stopped, problem = _collect_at_base(base_copy, group.setup, tests)
    if stopped:
        return [_base_run_budget_reached(base_copy.budget, f"while it collected {which}")], True
    if problem:
        return [_finding(None, None, "not-run", f"{problem}; {which} were not run there.")], False
    if not ids:
        return [], False
    left = base_copy.deadline - time.monotonic()
    if left <= 0:
        return [_base_run_budget_reached(base_copy.budget, f"once it had collected {which}, before it ran them")], True
    report, imports = f"{reports}.xml", f"{reports}-imports.json"
    proc, problem = _pytest(group.setup, "running the changed tests at the base",
                            ["-p", "no:randomly", "-p", _BASE_IMPORTS_PLUGIN,
                             f"--gate-imports-root={base_copy.change.root}", f"--gate-imports-report={imports}",
                             f"--junitxml={report}", *map(group.setup.argument, ids)],
                            timeout=left, env=base_copy.env)
    if isinstance(proc, _PytestStopped):
        ran = sum(len(m[1]) for m in re.finditer(r"^([.FEsxX]+) *(?:\[ *\d+%\])?$", proc.stdout, re.MULTILINE))
        of = f" of {label}" if label else ""
        when = f"after {ran} of the {len(ids)} test cases{of} it was given, before it reported them"
        return [_base_run_budget_reached(base_copy.budget, when)], True
    if problem or proc is None:
        return [_finding(None, None, "not-run", f"{problem}; {which} were not judged.")], False
    try:
        cases = junit_outcomes(report)
        outside = _imported_from_working_tree(base_copy, imports)
    except GateError as e:
        return [_finding(None, None, "not-run", f"pytest, running the changed tests at the base, wrote no report the "
                                                f"gate can read ({e}): {_said(proc)}")], False
    if outside:
        named = _series([f"`{name}` (`{path}`)" for name, path in outside], limit=5)
        return [_finding(None, None, "not-run", f"{which} imported {named} from the working tree, not from the copy "
                                                f"of the base, as from a project installed into the gate's "
                                                f"interpreter, so they were not judged.")], False
    return [_finding(path, tests[(path, name)], "passes-at-base",
                     f"`{name}` passes without the change, so it does not test it.")
            for path, name in _passed_tests(cases, ids)], False


def _passed_tests(cases: list[tuple[str, str, str]], ids: list[str]) -> list[tuple[str, str]]:
    """The tests, as (path, qualified name) and sorted, of which cases, a run's outcomes as junit_outcomes gives them,
    passed every case of ids, the node ids the run was given, that it did not skip; a test whose cases were all
    skipped is not one."""
    outcomes: dict[tuple[str, str], set[str]] = {}
    by_case = {_junit_case(i): i for i in ids}
    for case, outcome, _ in cases:
        if (i := by_case.get(case)) is not None and outcome != "skipped":
            path, names, _ = _node_parts(i)
            outcomes.setdefault((path, ".".join(names)), set()).add(outcome)
    return [test for test, seen in sorted(outcomes.items()) if seen == {"passed"}]


def _imported_from_working_tree(base_copy: _BaseCopy, imports: str) -> list[tuple[str, str]]:
    """The top-level modules under which a run at the base imported a module from the working tree's files of the
    project rather than from the copy, whichever of the changed tests, their conftest.py files or their helpers
    imported it, as the reports _BASE_IMPORTS_PLUGIN wrote at imports, and beside it for pytest-xdist's workers, tell
    them: each by its name and the path, from the working tree's top, of the file of the first such module in name
    order, itself or one below it, sorted. The project's files are those git tracks at the base, which the copy
    holds, and those the change adds; others under the top, such as a virtual environment's, are not the project's.
    Raises GateError when a report cannot be read, naming it."""
    folder, stem = os.path.split(imports)
    try:
        workers = sorted(name for name in os.listdir(folder) if name.startswith(f"{stem}-"))
    except OSError as e:
        raise GateError(f"{folder}: {e}") from e
    located: dict = {}
    for path in [imports, *(os.path.join(folder, name) for name in workers)]:
        try:
            with open(path, encoding="utf-8") as f:
                part = json.load(f)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise GateError(f"{path}: {e}") from e
        if not isinstance(part, dict):
            raise GateError(f"{path}: not a JSON object")
        located.update(part)
    added = {new for _, new in base_copy.change.files if new is not None}
    out: dict[str, str] = {}
    for name, path in sorted(located.items()):
        if isinstance(path, str) and (os.path.lexists(os.path.join(base_copy.tree, path)) or path in added):
            out.setdefault(name.partition(".")[0], path)
    return sorted(out.items())


def _collect_at_base(base_copy: _BaseCopy, setup: _PytestSetup,
                     tests: dict[tuple[str, str], int]) -> tuple[list[str], bool, str | None]:
    """The node ids, one per parameter set, of those of tests, keyed by their paths from setup's root, that pytest
    collects by setup in base_copy, with its env, by its deadline; whether the deadline stopped it; and the problem, a
    clause, when it went wrong otherwise.

    A test file that cannot be imported at the base gives no ids for the tests it covers and no problem: that is the
    expected outcome for a test of what the change adds. So does a test pytest does not collect, such as a test_*
    method of a class not named Test*, and each test under a conftest.py that fails to load there, whatever it raises,
    through a file the change edits: the conftest.py itself, a helper beside the tests that it imports, which the copy
    holds as the working tree has it, or source the copy holds as the base has it. Without the change's edit of that
    file, the conftest.py fails, so the failure is the change's. One that fails through no file the change edits is a
    problem: the base itself fails there. So is a pytest that stops before it collects, as for a plugin that cannot be
    imported."""
    files = sorted({path for path, _ in tests})
    doing = "collecting the changed tests at the base"
    # pytest loads the conftest.py files above every path it is given before it collects any, so one that fails stops
    # them all: the files under it are left out, and the others collected again, until none fails. Each round leaves
    # out at least the file that led pytest to the conftest.py.
    while files:
        # Exit status 1: some file could not be collected, which --continue-on-collection-errors lets the others
        # outlive; 4, a usage error, which a conftest.py that fails to load gives; 5: no test was collected.
        proc, problem = _pytest(setup, doing,
                                ["-p", "no:randomly", "--collect-only", "--continue-on-collection-errors",
                                 *map(setup.argument, files)],
                                timeout=base_copy.deadline - time.monotonic(), ok=(0, 1, 4, 5), env=base_copy.env)
        if isinstance(proc, _PytestStopped):
            return [], True, None
        if problem or proc is None:
            return [], False, problem
        # pytest exits 1 too when it stops before collecting, as for a plugin a conftest.py names that cannot be
        # imported; then it prints a traceback and no summary of what it collected.
        if proc.returncode == 1 and not re.search(r"^(?:no tests|\d+ tests?) collected\b", proc.stdout, re.MULTILINE):
            return [], False, f"pytest stopped before it collected the changed tests at the base: {_said(proc)}"
        if proc.returncode != 4:
            ids, problem = _listed_tests(proc, doing, tests)
            return ids, False, problem
        files, problem = _outside_failed_conftest(base_copy, setup, proc, files)
        if problem:
            return [], False, problem
    return [], False, None


def _outside_failed_conftest(base_copy: _BaseCopy, setup: _PytestSetup, proc: subprocess.CompletedProcess,
                             files: list[str]) -> tuple[list[str], str | None]:
    """Those of files, test files by their paths from setup's root, that are not under the conftest.py that stopped
    proc, their collection by _collect_at_base that exited with status 4, when it failed through a file the change
    edits (_failing_conftest); at least one is under it. Else [] and the problem, a clause: the conftest.py failed
    through no file the change edits, so the base itself fails there, it lies above none of files, or proc names
    none."""
    doing = "collecting the changed tests at the base"
    failed = _failing_conftest(proc, setup.top)
    if failed is None:
        return [], f"pytest, {doing}, exited with status 4: {_said(proc)}"
    conftest, error, through = failed
    if not through & base_copy.change.changed.keys():
        return [], (f"pytest, {doing}, could not load `{conftest}`, which fails through no file the change edits, so "
                    f"the base fails there: {error}")
    # One at the top, whose directory is "", covers every file.
    under = os.path.dirname(conftest)
    left = [path for path in files if under and not setup.argument(path).startswith(f"{under}/")]
    if left == files:
        return [], f"pytest, {doing}, could not load `{conftest}`, above none of the changed tests: {error}"
    return left, None


def _failing_conftest(proc: subprocess.CompletedProcess, top: str) -> tuple[str, str, frozenset[str]] | None:
    """The conftest.py that stopped proc, a pytest run in top that exited with status 4, as it loaded, whatever it
    raised: as pytest 9 words that (print_conftest_import_error in its _pytest/config/__init__.py), "ImportError while
    loading conftest '<path>'." for any exception, then the traceback, then the exception on lines marked "E". So, its
    path; the exception, as the first "E" line, or the conftest line itself when there is none; and the files the
    traceback passes through, the conftest.py among them. Paths are from top, each with its directory resolved, but
    not the file itself: a conftest.py that is a link is named by the link. None when proc names no such file."""
    said = proc.stdout + proc.stderr
    found = re.search(r"^ImportError while loading conftest '(.*)'\.$", said, re.MULTILINE)
    if found is None:
        return None
    raised = re.compile(r"^E\s+(\S.*)$", re.MULTILINE).search(said, found.end())
    traceback = said[found.end():raised.start() if raised else len(said)]
    conftest = _path_from(top, found[1])
    through = {conftest, *(_path_from(top, m[1]) for m in re.finditer(r"^(\S.*?):\d+: in ", traceback, re.MULTILINE))}
    return conftest, _clip(raised[1].strip() if raised else found[0]), frozenset(through)


def _path_from(top: str, path: str) -> str:
    """path, absolute or from top, as a repository path from top: its directory resolved, as top is, and its last
    part kept as it is."""
    folder = os.path.realpath(os.path.join(top, os.path.dirname(path)))
    return os.path.relpath(os.path.join(folder, os.path.basename(path)), os.path.realpath(top)).replace(os.sep, "/")


def _base_run_budget_reached(budget: float, when: str) -> dict:
    """passes_at_base's finding that its budget of budget seconds ran out when, a clause such as "while it collected
    the changed tests"."""
    return _finding(None, None, "budget-reached", f"pytest was stopped at {budget:g} seconds (`--base-run-budget`) "
                                                  f"{when}, so none of them was judged.")


def _copy_base_with_tests(change: GateChange, dest: str, archive: str) -> list[tuple[str, str]]:
    """Write the files git tracked at change.base to dest, a directory it makes, through a tar file at archive, then
    replace each file of the change's that _beside_tests accepts with the working tree's: removed at its path at the
    base, and copied, a symbolic link as a link, to its path in the working tree. A symbolic link is written as it is,
    wherever it points, and no file is written or removed through one: a link on the way to a replaced file is
    replaced by a directory, as the working tree has one there. A submodule holds the files of the commit the base
    records for it, as _unpack_commit writes them.

    Returns the submodules left empty, as _unpack_commit does. Raises GateError when git cannot list or write a
    commit, tarfile.TarError when an archive cannot be unpacked, and OSError when dest exists or a file cannot be
    written."""
    os.makedirs(dest)
    empty = _unpack_commit(change.root, change.base, dest, archive)
    # A path the base tracks has no link above it there.
    for old, _ in change.files:
        if old is not None and _beside_tests(old) and os.path.lexists(path := os.path.join(dest, old)):
            os.remove(path)
    for _, new in change.files:
        if new is not None and _beside_tests(new) and os.path.lexists(src := os.path.join(change.root, new)):
            dst = _unlinked_path(dest, new)
            # A directory the base had where the change has a file or a link: emptied by the removals above, or not.
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            elif os.path.lexists(dst):
                os.remove(dst)
            shutil.copy2(src, dst, follow_symlinks=False)
    return empty


def _unpack_commit(repo: str, commit: str, dest: str, archive: str, prefix: str = "") -> list[tuple[str, str]]:
    """Write the files git tracked at commit in the repository whose working tree's top is repo to dest, an existing
    directory, through a tar file at archive, which it overwrites; then fill each submodule's directory the same way,
    from the submodule's own repository in repo's working tree, at the commit that commit records for it, its own
    submodules too. A submodule that is not initialised there, or whose repository lacks that commit, is left an empty
    directory, as git archive leaves it; one that git archive leaves out, as for export-ignore, stays missing.

    Returns those, each by its path with prefix before it, and why, a clause such as "which is not initialised in the
    working tree". Raises GateError when git cannot list or write a commit, and tarfile.TarError when an archive
    cannot be unpacked."""
    _git(repo, "archive", "--format=tar", f"--output={archive}", commit, timeout=GATE_TOOL_TIMEOUT)
    with tarfile.open(archive) as tar:
        # "tar", not "data": a tracked link out of the tree is the repository's own; both refuse a member whose path
        # leads out of dest, through a link or otherwise.
        tar.extractall(dest, filter="tar")
    # Each entry is "<mode> <type> <object>\t<path>"; a submodule's mode is 160000 and its object the commit.
    listing = _git(repo, "ls-tree", "-r", "-z", commit, timeout=GATE_TOOL_TIMEOUT, verbatim=True)
    empty: list[tuple[str, str]] = []
    for entry in listing.split("\0"):
        meta, _, path = entry.partition("\t")
        # A submodule the commit's .gitattributes marks export-ignore is missing from the archive, as other paths are.
        if not meta.startswith("160000 ") or not os.path.isdir(os.path.join(dest, path)):
            continue
        recorded, name, sub = meta.split()[2], f"{prefix}{path}", os.path.join(repo, path)
        # The submodule's directory is the top of a repository of its own only once it is initialised: before, git
        # finds the superproject above it, and names the directory as a prefix within it.
        own = _git_run(sub, "rev-parse", "--show-prefix", timeout=GATE_TOOL_TIMEOUT, verbatim=True)
        if own.returncode != 0 or own.stdout.strip("\n"):
            empty.append((name, "which is not initialised in the working tree"))
        elif _git_run(sub, "cat-file", "-e", f"{recorded}^{{commit}}", timeout=GATE_TOOL_TIMEOUT).returncode != 0:
            why = f"whose repository in the working tree does not have the base's commit {recorded[:12]}"
            empty.append((name, why))
        else:
            empty += _unpack_commit(sub, recorded, os.path.join(dest, path), archive, f"{name}/")
    return empty


def _unlinked_path(dest: str, path: str) -> str:
    """dest/path, a repository path, after making each directory above it in dest a real one: a symbolic link there
    is replaced by an empty directory, and a missing one made. So a file written or removed at it stays in dest.
    Raises OSError when one cannot be."""
    at = dest
    for part in path.split("/")[:-1]:
        at = os.path.join(at, part)
        if os.path.islink(at):
            os.remove(at)
        if not os.path.isdir(at):
            os.mkdir(at)
    return os.path.join(at, path.rsplit("/", 1)[-1])


# Surviving mutants: the code a change adds that its tests run without checking what it does, which coverage counts
# as tested.
#
# mutmut 3 (3.8.0 here) is bounded to the functions the change edits like this. Its own selector, `mutmut run` with
# mutant names or globs such as `gate.x_flaky_tests__mutmut_*`, only picks which mutants run: it first makes a
# mutant of every function in its source_paths, which for orchestrator.py takes minutes and makes a 25 MB module the
# tests then crawl through. So mutmut runs on a copy of the tree, and in the copy every function and class that holds
# no changed function gets `# pragma: no mutate block` at the end of the line its header ends on. mutmut skips such a
# function whole: no mutants, and none of the call tracing that slows the tests. Appending a comment keeps every line
# where it was, and mutmut writes its state, mutants/, into the copy, so the working tree is never written to.
_MUTMUT_SKIP = "  # pragma: no mutate block"

# The directory in the tree mutmut runs in where it keeps its state: its copy of the code with the mutants, and each
# module's results in <path>.meta beside it. What a tree tracks there is no part of the change the checks see
# (GateChange.from_tree).
_MUTMUT_STATE = "mutants"

# How `mutmut results` words a mutant it did not try against a test: one the budget stopped it before, and one of a
# function no test calls.
_MUTMUT_UNTRIED = frozenset({"not checked", "check was interrupted by user", "no tests"})

# The line mutmut 3.8.0 prints, exiting with status 1 before it tries any mutant, when no test calls any function it
# mutated, or it made no mutant at all; the mutants it made are then left not checked. Only the whole line counts: the
# output of a failing test, which mutmut prints before its own, may hold its words.
_MUTMUT_NO_TESTS = re.compile(r"^Stopping early, because we could not find any test case for any mutant\. It seems "
                              r"that the selected tests do not cover any code that we mutated\.$", re.MULTILINE)

# How many of a function's surviving mutants its finding shows, in the order mutmut made them.
_SURVIVOR_EXAMPLES = 3


def surviving_mutants(change: GateChange, *, budget: float) -> list[dict]:
    """The surviving-mutants check's find: mutation-tests with mutmut each top-level function and method the change adds
    a line to in a Python file outside the tests. Reports at its def each such function that has mutants the tests did
    not notice, with how many of its mutants they were and what the first _SURVIVOR_EXAMPLES change, as a line before
    and after; and each such function that no test calls. The change leaves out mutmut's state, as
    GateChange.from_tree says, so no function there is mutated. A changed path that is a symlink, a submodule or a
    special file is not read to choose the functions (_is_link_or_special), so none of its functions is a changed
    one; a symlink's target has its functions chosen only as a file the change edits. mutmut's own walk of the copy
    still reads a symlink to a .py file, and makes mutants of it that no finding reports, once any changed function
    starts it.

    mutmut runs in the gate's own interpreter, as flaky_tests runs pytest, on a copy of the files git tracks. The copy
    leaves out what the tree tracks under mutmut's state, so mutmut starts without a cache. mutmut runs there by the
    [tool.mutmut] of change.rules, whatever the copy's says: its source_paths, only_mutate and do_not_mutate decide
    which files can be mutated, and a changed function outside them is a finding saying so; its test selection says
    which tests run. Each mutant is tried against the tests that called its function on a clean run. mutmut may run
    budget seconds; a run stopped there is a finding that says how many of the mutants it tried, beside what those
    showed. A mutmut that cannot start, fails, or whose results cannot be listed is a finding saying so, and so are
    rules that name no source_paths and a changed file that cannot be read or parsed.
    """
    functions, out = _changed_python_functions(change)
    if functions:
        out += _mutation_test(change, functions, budget)
    return sorted(out, key=lambda f: (f["path"] or "", f["line"] or 0))


def _mutation_test(change: GateChange, functions: list[_ChangedFunction], budget: float) -> list[dict]:
    """surviving_mutants's findings on functions, the change's changed functions: those mutmut does not mutate, then
    what a run of mutmut, by the rules' [tool.mutmut], of budget seconds found of the others."""
    rules = change.rules or os.path.join(change.root, "pyproject.toml")
    table, problem = _mutmut_table(rules)
    if problem:
        return [_mutation_not_run(problem)]
    out, mutated = [], []
    for f in functions:
        why = _outside_mutmut(f.path, table) or _unmutatable(f)
        if why:
            out.append(_finding(f.path, f.line, "not-mutated", f"`{f.name}` was not mutation-tested: {why}."))
        else:
            mutated.append(f)
    if not mutated:
        return out
    return out + _in_scratch_directory("not-run", "to run mutmut on a copy of the tree in",
                                       lambda tmp: _mutmut_on_copy(change.root, tmp, mutated, table, budget))


def _mutmut_on_copy(root: str, tmp: str, functions: list[_ChangedFunction], table: dict, budget: float) -> list[dict]:
    """Copies the files git tracks in root's working tree into tmp, an empty private directory, as _copy_tracked
    does, but for what it tracks under mutmut's state, _MUTMUT_STATE, marked for mutmut to skip all but
    functions, with table, the rules' [tool.mutmut], in place of its own; runs mutmut there on functions for
    budget seconds, its pytest runs taking no configuration or conftest.py from above the copy, and returns what
    it found."""
    tree_copy = os.path.join(tmp, "tree")
    added = table.get("pytest_add_cli_args", [])
    try:
        _copy_tracked(root, tree_copy)
        # mutmut takes what it finds under its state for its own cache, results included, so it starts without what
        # the tree tracks there.
        state = os.path.join(tree_copy, _MUTMUT_STATE)
        if os.path.isdir(state) and not os.path.islink(state):
            shutil.rmtree(state)
        elif os.path.lexists(state):
            os.remove(state)
        _mark_for_mutmut(tree_copy, {(f.path, f.name) for f in functions})
        # mutmut runs pytest in the copy's mutants/, with --rootdir=. and these after its own arguments. Without -c,
        # pytest looks for its configuration in each directory above, up to one that another account may write to
        # when the project has none; --confcutdir keeps it from importing a conftest.py from above mutants/. The
        # project's own configuration is read from mutmut's copy of it in mutants/, which also_copy makes, since
        # pytest takes paths in it, such as pythonpath, from where it is: there they name the mutated code.
        config = _pytest_config(tree_copy, tmp)
        mutants = os.path.join(tree_copy, _MUTMUT_STATE)
        also_copy = table.get("also_copy", [])
        if os.path.dirname(config) == tree_copy:
            also_copy = [*also_copy, os.path.basename(config)]
            config = os.path.join(mutants, os.path.basename(config))
        _use_mutmut_table(tree_copy, {**table, "also_copy": also_copy,
                                      "pytest_add_cli_args": [*added, "-c", config, "--confcutdir", mutants]})
        run, stopped = _run_mutmut(tree_copy, ["run"], budget)
    except (OSError, GateError) as e:
        return [_mutation_not_run(f"could not start mutmut: {e}")]
    except ValueError as e:
        return [_mutation_not_run(str(e))]
    return _mutmut_findings(tree_copy, functions, run, stopped, budget, table)


def _mutmut_findings(tree_copy: str, functions: list[_ChangedFunction], run: subprocess.CompletedProcess,
                     stopped: bool, budget: float, table: dict) -> list[dict]:
    """What the run of mutmut in tree_copy found of functions, as `mutmut results` and `mutmut show` tell it: run is
    how it ended, stopped whether the budget of budget seconds stopped it, and table the [tool.mutmut] it ran by."""
    no_tests = run.returncode != 0 and _MUTMUT_NO_TESTS.search(run.stdout) is not None
    if run.returncode != 0 and not stopped and not no_tests:
        return [_mutation_not_run(f"mutmut exited with status {run.returncode}: {_said_by_mutmut(run)}")]
    deadline = time.monotonic() + MUTMUT_REPORT_TIMEOUT
    statuses, cut_off, problem = _statuses_after_run(tree_copy, stopped, deadline)
    if problem:
        return [_mutation_not_run(problem)]
    if no_tests:
        statuses = dict.fromkeys(statuses, "no tests")
    mutants = _mutants_by_function(statuses, functions)
    # A function mutmut was given but listed no mutant of. mutmut saves a module's mutants, none or more, in
    # mutants/<path>.meta once it has made them all, so stopped by the budget it had not reached a module without one,
    # or the stop lost its results (_statuses_after_run).
    unlisted = [f for f in functions if f not in mutants]
    unreached = [f for f in unlisted
                 if stopped and not os.path.exists(os.path.join(tree_copy, _MUTMUT_STATE, f"{f.path}.meta"))]
    out = [_finding(f.path, f.line, "not-mutated",
                    f"`{f.name}` was not mutation-tested: mutmut made no mutant of it; {_why_no_mutant(f, table)}.")
           for f in unlisted if f not in unreached]
    for f, by_status in mutants.items():
        tried = sum(len(names) for status, names in by_status.items() if status not in _MUTMUT_UNTRIED)
        if by_status.get("survived"):
            out.append(_survivors(tree_copy, f, by_status["survived"], tried, deadline))
        if by_status.get("no tests"):
            out.append(_finding(f.path, f.line, "untested", f"no test calls `{f.name}`, so none of its "
                                                            f"{len(by_status['no tests'])} mutants can make one fail."))
    if stopped:
        out.append(_budget_reached(statuses, budget, unreached, cut_off))
    return out


def _statuses_after_run(tree_copy: str, stopped: bool, deadline: float) -> tuple[dict[str, str], list[str], str | None]:
    """_mutant_statuses's listing of the mutants a run of mutmut made in tree_copy, by deadline, a time.monotonic()
    time, with the paths of the modules whose results the run's stop lost, sorted; stopped is whether the budget
    stopped the run. Or {}, [] and the problem, a clause, when they could not be listed.

    The stop can land while mutmut rewrites a module's .meta, which `mutmut results` cannot read, so a stopped run's
    cut-off .meta files are removed from tree_copy first; their modules then list no mutant, as one the budget kept
    mutmut from does.
    """
    cut_off: list[str] = []
    if stopped:
        try:
            cut_off = _drop_cut_off_saves(tree_copy)
        except OSError as e:
            return {}, [], f"could not clear the results mutmut's stop cut off: {e}"
    statuses, problem = _mutant_statuses(tree_copy, deadline)
    if problem:
        lost = f" after the gate removed the cut-off results of {_series(cut_off, limit=5)}" if cut_off else ""
        return {}, [], problem + lost
    return statuses, cut_off, None


def _drop_cut_off_saves(tree_copy: str) -> list[str]:
    """Removes each of mutmut's saves of a module's results, mutants/<path>.py.meta, in tree_copy that does not parse
    as JSON, as one cut off mid-write leaves it, and returns the paths of their modules in the tree, sorted, as the
    gate names a changed file; [] when there is none. A save that cannot be opened is left as it is. Raises OSError
    when mutants/ cannot be searched or a save cannot be removed."""
    state = os.path.join(tree_copy, _MUTMUT_STATE)
    if not os.path.isdir(state):
        return []

    def refuse(e: OSError) -> None:
        raise e
    out = []
    for parent, _, names in os.walk(state, onerror=refuse):
        for name in names:
            if not name.endswith(".py.meta"):
                continue
            path = os.path.join(parent, name)
            try:
                with open(path, "rb") as f:
                    json.loads(f.read())
            except ValueError:
                os.remove(path)
                out.append(os.path.relpath(path, state).replace(os.sep, "/").removesuffix(".meta"))
            except OSError:
                # Not a cut-off save. mutmut reads a missing one as no results (SourceFileMutationData.load), and
                # fails to list on any other, which is then the finding.
                pass
    return sorted(out)


def _mutants_by_function(statuses: dict[str, str], functions: list[_ChangedFunction],
                         ) -> dict[_ChangedFunction, dict[str, list[str]]]:
    """The mutants of statuses, mutmut's status of each mutant by its name, that belong to each of functions, by
    status, in the order statuses lists them, which for `mutmut results` is the order mutmut made them in; a function
    without one has no key, and a mutant of no such function is left out."""
    by_prefix = {_mutant_prefix(f): f for f in functions}
    out: dict[_ChangedFunction, dict[str, list[str]]] = {}
    for mutant, status in statuses.items():
        owner = by_prefix.get(_prefix_of(mutant))
        if owner is not None:
            out.setdefault(owner, {}).setdefault(status, []).append(mutant)
    return out


def _budget_reached(statuses: dict[str, str], budget: float, unreached: list[_ChangedFunction],
                    cut_off: list[str]) -> dict:
    """The finding that the budget of budget seconds stopped mutmut, having made the mutants of statuses, mutmut's
    status of each by its name, and none yet of unreached, the functions it was given in modules it had not reached;
    of those, the ones in cut_off, paths of modules whose results the stop lost mid-save, are said to be so."""
    tried = sum(1 for status in statuses.values() if status not in _MUTMUT_UNTRIED)

    def named(fs: list[_ChangedFunction]) -> str:
        return _series([f"`{f.name}` in {f.path}" for f in fs], limit=5)
    unmade = [f for f in unreached if f.path not in cut_off]
    lost = [f for f in unreached if f.path in cut_off]
    clauses = [f"mutmut made no mutant of {named(unmade)} before the budget ran out"] if unmade else []
    if lost:
        clauses.append(f"the stop cut off mutmut's save of its results for {named(lost)}, so they count as not reached")
    return _finding(None, None, "budget-reached", (
        f"budget reached after {tried} of {len(statuses)} mutants" if statuses
        else "budget reached with none of the mutants mutmut made left to list" if lost
        else "budget reached before mutmut had made its mutants")
        + f": mutmut was stopped at {budget:g} seconds (`--mutation-budget`), so the mutants it did not try may "
          f"survive too"
        + "".join(f"; {c}" for c in clauses) + ".")


def _why_no_mutant(f: _ChangedFunction, table: dict) -> str:
    """Why mutmut 3.8.0 made no mutant of f, a function it was given, as a clause: the one cause the gate can tell
    cheaply, a `# pragma: no mutate block` where its header ends or table's do_not_mutate_patterns matching each
    statement in it, else every cause there can be."""
    text = textwrap.dedent("\n".join(f.source))
    tree = _parse_python(text)
    unit = tree.body[0] if tree is not None and tree.body else None
    if not isinstance(unit, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _EVERY_CAUSE
    ends, comments = _logical_line_ends(text)
    # The one form of mutmut's pragmas that the gate recognises and names as the cause: `block` in the comment of
    # the line its header ends on, with the body on the lines below (pragma_handling.py, _visit_compound_header). A
    # block pragma on a line of its own above the def or atop its body, one on its class, or a start/end pair around
    # it keep it out too, and get _EVERY_CAUSE, which names a pragma among the causes.
    header = ends[bisect.bisect_left(ends, unit.lineno)] if ends and ends[-1] >= unit.lineno else None
    if header is not None and unit.body[0].lineno > header and _mutmut_pragma(comments.get(header, "")) == "block":
        return "a `# pragma: no mutate block` where its header ends keeps it out"
    patterns = table.get("do_not_mutate_patterns", [])
    if patterns:
        # mutmut skips every expression that starts on a line a pattern matches; a statement with a block of its own
        # holds lines of other statements, which this does not look into.
        body = unit.body[1:] if ast.get_docstring(unit) is not None else unit.body
        starts = [f.source[stmt.lineno - 1] for stmt in body if not hasattr(stmt, "body")]
        with contextlib.suppress(re.error):
            if body and len(starts) == len(body) and all(any(re.search(p, line) for p in patterns) for line in starts):
                return "the rules' do_not_mutate_patterns match each statement in it"
    return _EVERY_CAUSE


# Why mutmut made no mutant of a function it was given, when the gate cannot tell which cause it was.
_EVERY_CAUSE = ("either it has nothing mutmut mutates, the rules' do_not_mutate_patterns or mutate_only_covered_lines "
                "leave its lines out, a `# pragma: no mutate` keeps them out, or the gate could not read mutmut's "
                "names for its mutants")


def _mutmut_pragma(comment: str) -> str | None:
    """What a comment says to mutmut 3.8.0, as its _parse_pragma_token reads it: "block", "start", "end" or "bare"
    for a `# pragma: no mutate` comment of each form; None for any other comment."""
    if "# pragma:" not in comment or "no mutate" not in comment:
        return None
    words = comment.partition("no mutate")[2].strip().lstrip(": ").split(",", 1)[0].split()
    return words[0] if words and words[0] in ("block", "start", "end") else "bare"


def _logical_line_ends(text: str) -> tuple[list[int], dict[int, str]]:
    """The lines of Python source text on which a logical line ends, in order, and each line's comment by its
    number; ([], {}) when text does not tokenize. A def's or a class's header ends on the first of those lines on or
    after the line of its def or class, whatever brackets or decorators it spans."""
    ends, comments = [], {}
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if t.type == tokenize.NEWLINE:
                ends.append(t.start[0])
            elif t.type == tokenize.COMMENT:
                comments[t.start[0]] = t.string
    except (tokenize.TokenError, SyntaxError):
        return [], {}
    return ends, comments


def _changed_python_functions(change: GateChange) -> tuple[list[_ChangedFunction], list[dict]]:
    """The functions mutmut mutates as one that the change added lines to, decorators included, in the Python files
    outside the tests that it left in the working tree; and a finding for each such file that could not be read or
    parsed."""
    functions, unreadable = _changed_python_units(change, lambda path: not _python_test_file(path))
    return functions, [_finding(path, None, "not-run", f"{why}, so its functions were not mutation-tested.")
                       for path, why in unreadable]


def _mutmut_table(rules: str) -> tuple[dict, str | None]:
    """The [tool.mutmut] table of the pyproject.toml at rules, {} when it has none; or {} and the problem, a clause,
    when it cannot be read, names no source_paths, without which what mutmut mutates cannot be told, or holds an
    only_mutate, do_not_mutate, do_not_mutate_patterns, also_copy or pytest_add_cli_args that is not a list of
    strings."""
    try:
        with open(rules, "rb") as f:
            table = tomllib.load(f).get("tool", {}).get("mutmut", {})
    except (OSError, tomllib.TOMLDecodeError, AttributeError) as e:
        return {}, f"could not read [tool.mutmut] in {rules}: {e}"
    sources = (table.get("source_paths") or table.get("paths_to_mutate")) if isinstance(table, dict) else None
    if not isinstance(sources, list) or not sources or not all(isinstance(p, str) for p in sources):
        return {}, (f"[tool.mutmut] in {rules} names no source_paths as a list of paths, so what mutmut mutates is "
                    f"unknown")
    # The other keys the gate reads or adds to.
    for key in ("only_mutate", "do_not_mutate", "do_not_mutate_patterns", "also_copy", "pytest_add_cli_args"):
        setting = table.get(key, [])
        if not isinstance(setting, list) or not all(isinstance(p, str) for p in setting):
            return {}, f"[tool.mutmut] {key} in {rules} is not a list of strings"
    return table, None


def _outside_mutmut(path: str, table: dict) -> str | None:
    """Why mutmut, set up by table, a [tool.mutmut] with source_paths, mutates nothing in the file at path, relative to
    the tree's top; None when it may. As mutmut 3.8.0 decides it, by walking source_paths, then Config.should_mutate's
    only_mutate and do_not_mutate globs."""
    sources: list[str] = table.get("source_paths") or table.get("paths_to_mutate") or []
    roots = [os.path.normpath(p) for p in sources]
    if not any(r == "." or path == r or path.startswith(f"{r}/") for r in roots):
        return f"it is outside [tool.mutmut] source_paths ({', '.join(sources)})"
    only = table.get("only_mutate", [])
    if only and not any(fnmatch.fnmatch(path, g) for g in only):
        return f"[tool.mutmut] only_mutate ({', '.join(only)}) leaves it out"
    excluded = [g for g in table.get("do_not_mutate", []) if fnmatch.fnmatch(path, g)]
    return f"[tool.mutmut] do_not_mutate excludes it ({', '.join(excluded)})" if excluded else None


def _unmutatable(f: _ChangedFunction) -> str | None:
    """Why mutmut 3.8.0 makes no mutant of f whatever it holds, as its file_mutation.py decides; None when it may."""
    name = f.name.rpartition(".")[2]
    if name in ("__new__", "__getattribute__", "__setattr__"):
        return f"mutmut never mutates `{name}`"
    if f.decorators and f.decorators not in (("staticmethod",), ("classmethod",)):
        return "mutmut does not mutate a decorated function, unless @staticmethod or @classmethod is its one decorator"
    return None


def _use_mutmut_table(tree_copy: str, table: dict) -> None:
    """Puts table in place of the [tool.mutmut] of tree_copy's pyproject.toml, leaving the rest of the file, the
    tests' own settings among it, as the tree has it. Raises OSError when the file cannot be read or written, and
    ValueError when the result does not read back as table, as when table holds a setting TOML's flat form cannot take
    or the file defines the table other than under a [tool.mutmut] header."""
    path = os.path.join(tree_copy, "pyproject.toml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    kept, inside = [], False
    for line in text.split("\n"):
        header = re.fullmatch(r"\s*\[\[?\s*([^\[\],=#]+?)\s*\]\]?\s*(?:#.*)?", line)
        if header:
            inside = header.group(1) == "tool.mutmut"
        if not inside:
            kept.append(line)
    # JSON's strings, numbers, booleans and arrays of them are TOML's too, and a quoted key is a key. What JSON
    # cannot write, such as a TOML date, cannot be put back.
    options = []
    for name, setting in table.items():
        try:
            options.append(f"{json.dumps(name)} = {json.dumps(setting)}\n")
        except (TypeError, ValueError) as e:
            raise ValueError(f"[tool.mutmut] {name} in the rules cannot be written back for mutmut: {e}") from e
    text = "\n".join(kept).rstrip("\n") + "\n\n[tool.mutmut]\n" + "".join(options)
    try:
        replaced = tomllib.loads(text).get("tool", {}).get("mutmut") == table
    except tomllib.TOMLDecodeError:
        replaced = False
    if not replaced:
        raise ValueError("the rules' [tool.mutmut] could not be put in place of the tree's in its pyproject.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _mark_for_mutmut(tree_copy: str, changed: set[tuple[str, str]]) -> None:
    """Marks for mutmut to skip each top-level function and class of the Python files in tree_copy, a copy of a tree,
    that is not, and holds no method, in changed: (path, name as _ChangedFunction gives it). A Python file that cannot
    be decoded or parsed, and a symbolic link, are left as they are. Raises OSError when a file cannot be read or
    written."""
    for top, _, names in os.walk(tree_copy):
        for name in names:
            path = os.path.join(top, name)
            if not name.endswith(".py") or os.path.islink(path):
                continue
            with open(path, "rb") as f:
                raw = f.read()
            try:
                text, encoding = _decode_python(raw)
            except SyntaxError:
                continue
            marked = _skip_unchanged(os.path.relpath(path, tree_copy).replace(os.sep, "/"), text, changed)
            if marked != text:
                with open(path, "wb") as f:
                    f.write(marked.encode(encoding))


def _skip_unchanged(path: str, text: str, changed: set[tuple[str, str]]) -> str:
    """text, the Python file at path, with _MUTMUT_SKIP on the line each header ends on of each top-level function and
    class that is not, and holds no method, in changed, and of each method or nested class beside one that is. text
    as it is when it does not parse."""
    tree = _parse_python(text)
    if tree is None:
        return text
    ends, _ = _logical_line_ends(text)
    if not ends:
        return text
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    skipped: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            inner = [m for m in node.body if isinstance(m, defs)]
            kept = [m for m in inner if (path, f"{node.name}.{m.name}") in changed]
            skipped += [m for m in inner if m not in kept] if kept else [node]
        elif isinstance(node, defs) and (path, node.name) not in changed:
            skipped.append(node)
    lines = text.split("\n")
    for node in skipped:
        i = ends[bisect.bisect_left(ends, node.lineno)] - 1
        line, cr = (lines[i][:-1], "\r") if lines[i].endswith("\r") else (lines[i], "")
        lines[i] = line + _MUTMUT_SKIP + cr
    return "\n".join(lines)


def _mutant_prefix(f: _ChangedFunction) -> str:
    """The name mutmut 3 gives f's mutants before their `__mutmut_<n>`: f's module, from its path as mutmut's
    get_mutant_name makes it, a dot, then x_<function> or xǁ<class>ǁ<method>, which holds no dot. _prefix_of reads it
    back from a mutant's name."""
    module = f.path.removesuffix(".py").replace("/", ".").removeprefix("src.")
    cls, _, name = f.name.rpartition(".")
    return f"{module}.{f'xǁ{cls}ǁ{name}' if cls else f'x_{name}'}".replace(".__init__.", ".")


def _prefix_of(mutant: str) -> str:
    """The part of a mutant's name, as `mutmut results` lists it, that _mutant_prefix gives its function: all before
    its last `__mutmut_`, since a function named _mutmut_x has one in its own name, as x__mutmut_x."""
    return mutant.rpartition("__mutmut_")[0]


def _run_mutmut(tree_copy: str, args: list[str], timeout: float) -> tuple[subprocess.CompletedProcess, bool]:
    """mutmut with args, run in the gate's own interpreter at tree_copy, and whether it was stopped at timeout seconds.

    It runs as _run_in_session runs a tool, stopped softly with SIGINT and MUTMUT_STOP_GRACE: on SIGINT mutmut stops
    trying mutants and keeps the results it has. So it returns within timeout + MUTMUT_STOP_GRACE + TOOL_KILL_WAIT
    seconds. Its temporary files, and those of the tests it runs, go into tree_copy's .tmp, so that what a test it
    killed left goes with it. Its mutant names and diffs hold paths and source lines, in which only "\n" ends a
    line. Raises OSError when it cannot be started.
    """
    # `mutmut`, its console script, runs this line. `python -m mutmut` would import its __main__ a second time in each
    # forked worker, which mutmut warns against.
    argv = [sys.executable, "-c", "from mutmut.__main__ import cli; cli()", *args]
    tmp = os.path.join(tree_copy, ".tmp")
    os.makedirs(tmp, exist_ok=True)
    return _run_in_session(argv, tree_copy, {**tool_env(), "TMPDIR": tmp}, timeout=timeout,
                           soft_stop=(signal.SIGINT, MUTMUT_STOP_GRACE))


def _time_left(deadline: float) -> float:
    """The seconds a mutmut run may be given that must end by deadline, a time.monotonic() time, stop included."""
    return deadline - time.monotonic() - MUTMUT_STOP_GRACE - TOOL_KILL_WAIT


def _mutant_statuses(tree_copy: str, deadline: float) -> tuple[dict[str, str], str | None]:
    """Each mutant mutmut made in tree_copy, by name, with its status, mutmut's word for what became of a mutant, as
    `mutmut results` words it: killed, survived, no tests, not checked and the like. Or {} and the problem, a clause,
    when they could not be listed by deadline, a time.monotonic() time that mutmut's stop is kept within."""
    try:
        proc, stopped = _run_mutmut(tree_copy, ["results", "--all", "true"], max(0.0, _time_left(deadline)))
    except OSError as e:
        return {}, f"could not start mutmut to list its results: {e}"
    if stopped:
        return {}, f"mutmut did not list its results within {MUTMUT_REPORT_TIMEOUT} seconds"
    if proc.returncode != 0:
        return {}, (f"mutmut could not list its results, exiting with status {proc.returncode}: "
                    f"{_said_by_mutmut(proc)}")
    return dict(re.findall(r"^\s*(\S+): (.+?)\s*$", proc.stdout, re.MULTILINE)), None


def _survivors(tree_copy: str, f: _ChangedFunction, survived: list[str], tried: int, deadline: float) -> dict:
    """The finding for f's survived mutants, of the tried ones mutmut ran against f's tests: how many, and what the
    first _SURVIVOR_EXAMPLES of them change, each as `mutmut show` gives it by deadline, a time.monotonic() time."""
    examples = [e for e in (_example(tree_copy, f, m, deadline) for m in survived[:_SURVIVOR_EXAMPLES]) if e]
    said = f"{len(survived)} of {tried} mutants of `{f.name}` survived its tests"
    return _finding(f.path, f.line, "survived", f"{said}: {'; '.join(examples)}." if examples
                    else f"{said}; mutmut did not show in time what they change.")


def _example(tree_copy: str, f: _ChangedFunction, mutant: str, deadline: float) -> str | None:
    """What mutant, a mutant of f, changes, as `line N, `before` became `after``; None when mutmut does not show it by
    deadline, a time.monotonic() time that mutmut's stop is kept within."""
    if _time_left(deadline) <= 0:
        return None
    try:
        proc, stopped = _run_mutmut(tree_copy, ["show", mutant], _time_left(deadline))
    except OSError:
        return None
    shown = None if stopped or proc.returncode != 0 else _mutation(proc.stdout)
    if shown is None:
        return None
    at, old, new = shown
    # at counts from the first line of the function's code as mutmut prints it, which takes in the comments right
    # above it, so the line is the nearest one that reads as the mutant's removed one.
    expected = f.first + at - 1
    hits = [f.first + k for k, line in enumerate(f.source) if line.strip() == old[0]]
    line = min(hits, key=lambda n: abs(n - expected), default=expected)
    return f"line {line}, `{_clip(' '.join(old))}` became `{_clip(' '.join(new))}`"


def _mutation(diff: str) -> tuple[int, list[str], list[str]] | None:
    """What the first hunk of `mutmut show`'s diff of a function and its mutant changes: the line of the function's code
    where it starts, 1 for the first, and the lines it removes and adds there, stripped; None when it removes none."""
    lines = diff.split("\n")
    hunk = next((i for i, line in enumerate(lines) if line.startswith("@@")), len(lines))
    m = re.match(r"@@ -(\d+)", lines[hunk]) if hunk < len(lines) else None
    if not m:
        return None
    n, at = int(m.group(1)), None
    old: list[str] = []
    new: list[str] = []
    for line in lines[hunk + 1:]:
        if line.startswith("@@"):
            break
        if line.startswith("+"):
            new.append(line[1:].strip())
            continue
        if line.startswith("-"):
            at = at or n
            old.append(line[1:].strip())
        n += 1
    return (at, old, new) if at else None


# A Python warning as the warnings module writes it to standard error: where, its category and message, then the
# source line it is about, indented.
_PYTHON_WARNING = re.compile(r"^\S.*:\d+: \w*Warning: .*\n(?:[ \t].*(?:\n|$))?", re.MULTILINE)


def _said_by_mutmut(proc: subprocess.CompletedProcess) -> str:
    """_said of a mutmut that went wrong, leaving out the Python warnings it writes to standard error, such as that a
    [tool.mutmut] key is deprecated, which come before its own message on standard output."""
    return _said(subprocess.CompletedProcess(proc.args, proc.returncode, proc.stdout,
                                             _PYTHON_WARNING.sub("", proc.stderr)))


def _mutation_not_run(problem: str) -> dict:
    return _finding(None, None, "not-run", f"{problem}; the change's functions were not mutation-tested.")


# Cognitive complexity: how hard a function is to follow, which a change raises when it piles branches into the
# function it edits rather than splitting it. ruff's C90 does not see that: it counts branches without their nesting,
# and reports at a def line, which a change that grows a function does not add.
#
# The score is G. Ann Campbell's, "Cognitive Complexity", SonarSource, 2017, version 1.7
# (https://www.sonarsource.com/docs/CognitiveComplexity.pdf), which SonarQube's rule S3776 implements. Its rules, as
# they apply in Python:
#
# - One point for each break in the linear flow: `if`, `elif`, `else`, a ternary `x if c else y`, `for`, `while`,
#   `except`, `match`, each sequence of like boolean operators, and a function that calls itself. Python has no
#   `goto` and no labelled `break` or `continue`, so the paper's points for those never apply; a plain `break` or
#   `continue` is free, as in the paper.
# - One more for each level of nesting it sits at, on `if`, a ternary, `for`, `while`, `except` and `match`; not on
#   `elif` and `else`, whose `if` has paid it.
# - The level rises by one in the bodies of `if`, `elif`, `else`, `for`, `while`, `except` and `match`'s cases, in
#   what a ternary holds, its condition included, and in a nested function or lambda. `try`, `finally` and `with`
#   add neither a point nor a level. A statement's condition, a loop's iterable and a nested function's decorators
#   and defaults are at the statement's level, outside its body.
#
# Where the paper leaves Python open, these are the choices. Sonar's own Python analyzer (sonar-python's
# CognitiveComplexityVisitor) is named where it chose otherwise:
#
# - `match` is the paper's `switch`: one point and its nesting for the whole statement, however many cases, which
#   nest their bodies. A case's guard adds only its own operators. Sonar's analyzer gives `match` no point.
# - The `else` of a loop or of a `try` counts as an `if`'s does, as in Sonar's analyzer: it runs on a condition the
#   reader must keep in mind, that no `break`, or no exception, came first.
# - A comprehension adds nothing, its `for` and `if` clauses included, as in Sonar's analyzer: it is the kind of
#   shorthand the paper exempts, a loop written as one expression and read left to right. What it holds, such as a
#   ternary or an `and`, counts as anywhere.
# - A nested function or lambda raises the level of its body, as the paper says of nested methods and lambdas, and
#   its points count toward the function it is in. Sonar's analyzer leaves a lambda's level alone, and a decorator's
#   wrapper function at its decorator's; this does neither. A class nested in a function raises nothing itself: its
#   methods are nested functions.
# - Boolean operators are read as written: `a and b or c` is the sequence `and`, `or`, two points, and an operand that
#   is itself an `and` or `or` continues its parent's sequence. The syntax tree keeps no parentheses, so
#   `a and (b and c)` is one sequence, one point, where Sonar's analyzer, which sees them, gives two. `not` starts a
#   sequence of its own.
# - Recursion is one point for each function that recurses, however often: the judged one when it calls itself or
#   lies on a cycle of calls through other judged functions of its module, as the paper counts each function in a
#   recursion cycle (_recursive_functions); and one nested in it that calls itself. Calls resolve by name, as Python's
#   scopes do, as _callees says. A call through an alias or into another module is not followed, nor a cycle through
#   a function nested in another. Sonar's analyzer counts no recursion.

# The cognitive-complexity check's limit: a function the change edits fails the gate above it, as with SonarQube's
# default for S3776.
COGNITIVE_COMPLEXITY_LIMIT = 15

# Where the check judges no function: the tests, as `per-file-ignores` in pyproject.toml exempts them from ruff's C901.
_COMPLEXITY_EXEMPT = "tests/"


@dataclass(frozen=True)
class _ScoredFunction:
    """A function the cognitive-complexity check judges as one, and its score: a function no other function holds,
    whether at the top of its module, in a class at any depth, or under a module-level statement such as an if or a
    try. A function nested in another is part of that one."""
    name: str  # its name qualified by its classes: f, C.f or C.D.f; a property's getter and setter share one
    first: int  # the line it starts on, its decorators included
    last: int  # the line it ends on
    line: int  # the line of its def
    score: int  # its cognitive_complexity, recursive as _recursive_functions tells it
    body: str  # what its body does, as _body_fingerprint gives it: equal for a function moved or renamed unchanged


def _scored_functions(tree: ast.Module) -> list[_ScoredFunction]:
    """Each function of tree that the cognitive-complexity check judges as one, scored, in the order they are
    written."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, str | None, frozenset[str]]] = []
    # The nodes whose statements are still to be read, each with the qualified-name prefix there, the qualified name
    # of the class a function there is a method of, and the type parameters of the classes around it. A stack rather
    # than recursion, as cognitive_complexity has.
    todo: list[tuple[ast.AST, str, str | None, frozenset[str]]] = [(tree, "", None, frozenset())]
    while todo:
        node, prefix, cls, typed = todo.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                found.append((prefix + child.name, child, cls, typed))
            elif isinstance(child, ast.ClassDef):
                todo.append((child, f"{prefix}{child.name}.", prefix + child.name, typed | _type_parameters(child)))
            elif isinstance(child, ast.stmt | ast.excepthandler | ast.match_case):
                todo.append((child, prefix, cls, typed))
    found.sort(key=lambda x: x[1].lineno)
    recursive = _recursive_functions([(node, cls, typed) for _, node, cls, typed in found])
    out = []
    for i, (name, node, _, _) in enumerate(found):
        first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        out.append(_ScoredFunction(name, first, node.end_lineno or node.lineno, node.lineno,
                                   cognitive_complexity(node, recursive=i in recursive), _body_fingerprint(node)))
    return out


def _recursive_functions(
        functions: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str | None, frozenset[str]]]) -> set[int]:
    """The indexes of those of functions that recurse: that call themselves, or lie on a cycle of calls through
    others of them, a strongly connected component of more than one in the graph of their calls to each other.
    functions are one module's, as _scored_functions finds them, each with the qualified name of the class it is a
    method of, or None, and the type parameters of the classes around it; calls resolve as _callees resolves them,
    and a call to anything else, a function nested in one of them included, is not followed."""
    by_key: dict[tuple[str | None, str], list[int]] = collections.defaultdict(list)
    for i, (node, cls, _) in enumerate(functions):
        by_key[(cls, node.name)].append(i)
    edges = [[j for key in _callees(node, cls, typed) for j in by_key.get(key, [])] for node, cls, typed in functions]
    return {i for component in _strong_components(edges) for i in component if len(component) > 1 or i in edges[i]}


def _strong_components(edges: list[list[int]]) -> list[list[int]]:
    """The strongly connected components of the graph whose vertices are edges' indexes, with an edge from v to each
    of edges[v]: each vertex in exactly one component, by Kosaraju's algorithm. Iterative, as cognitive_complexity's
    walk is, so that no graph is too deep for it."""
    reverse: list[list[int]] = [[] for _ in edges]
    for v, targets in enumerate(edges):
        for w in targets:
            reverse[w].append(v)
    seen: set[int] = set()
    return [_reach(reverse, v, seen) for v in reversed(_finish_order(edges)) if v not in seen]


def _finish_order(edges: list[list[int]]) -> list[int]:
    """Each vertex of the graph edges gives, as _strong_components says, once, in the order a depth-first search from
    each unvisited vertex in turn finishes it."""
    order: list[int] = []
    seen: set[int] = set()
    for root in range(len(edges)):
        if root in seen:
            continue
        seen.add(root)
        walk = [(root, iter(edges[root]))]
        while walk:
            v, rest = walk[-1]
            w = next((w for w in rest if w not in seen), None)
            if w is None:
                walk.pop()
                order.append(v)
            else:
                seen.add(w)
                walk.append((w, iter(edges[w])))
    return order


def _reach(edges: list[list[int]], v: int, seen: set[int]) -> list[int]:
    """v, which is not in seen, and each vertex the graph edges leads to from v through vertices not in seen; each is
    added to seen."""
    seen.add(v)
    out = [v]
    todo = [v]
    while todo:
        for w in edges[todo.pop()]:
            if w not in seen:
                seen.add(w)
                out.append(w)
                todo.append(w)
    return out


def too_complex_functions(change: GateChange) -> list[dict]:
    """The cognitive-complexity check's find: each function the change adds a line to, in a Python file outside
    _COMPLEXITY_EXEMPT, whose cognitive_complexity is over COGNITIVE_COMPLEXITY_LIMIT and higher than its base's, as
    _base_scores pairs them, or which has no base; at its def line, with both scores. A function over the limit that
    the change leaves no worse is not a finding: the gate judges the change, not the codebase. The functions judged
    are those _ScoredFunction describes.

    A changed file that cannot be read or parsed is a finding saying so, as GateCheck asks of a check that cannot do
    its work, and fails the gate as any other does. Files under mutmut's state are not changed files, as
    GateChange.from_tree says.
    """
    out, judged = _judged_files(change)
    if not any(_over_limit(functions, added) for functions, added in judged.values()):
        return out
    scores = _base_scores(change, judged)
    for path, (functions, added) in judged.items():
        for scored in _over_limit(functions, added):
            was = scores.get((path, scored.line))
            if was is None or scored.score > was:
                out.append(_over_limit_finding(path, scored, was))
    return sorted(out, key=lambda finding: (finding["path"] or "", finding["line"] or 0))


def _over_limit(functions: list[_ScoredFunction], added: set[int]) -> list[_ScoredFunction]:
    """Those of functions that score over COGNITIVE_COMPLEXITY_LIMIT and that the change adds a line to, decorators
    included; added is the lines it adds to their file."""
    return [scored for scored in functions if scored.score > COGNITIVE_COMPLEXITY_LIMIT
            and any(scored.first <= line <= scored.last for line in added)]


def _over_limit_finding(path: str, scored: _ScoredFunction, was: int | None) -> dict:
    """The finding for scored, a function of the working tree's file at path over the limit, whose base scored was, or
    which is new for was None."""
    return _finding(path, scored.line, "over-limit", (
        f"`{scored.name}`: cognitive complexity {scored.score}, "
        f"{'a new function' if was is None else f'{was} at the base'}; the limit is {COGNITIVE_COMPLEXITY_LIMIT}."),
        severity="error")


# The order in which _base_scores pairs a function with a base function of its body, each as (of its name, in its
# file's base): a function unchanged where it was, moved, renamed in place, renamed and moved.
_BODY_STEPS = ((True, True), (True, False), (False, True), (False, False))


def _base_scores(change: GateChange,
                 judged: dict[str, tuple[list[_ScoredFunction], set[int]]]) -> dict[tuple[str, int], int]:
    """The base score of each function of judged, _judged_files' functions by path, that has a base, by (path, def
    line); a function with no key is new. Only the change's own files are read.

    A function's base is a function of the base, each the base of one function at most, so that a copy never takes
    the score of what it copies. First a base function with its body, since an unchanged function is that function
    whatever its name: of its name in its file's base, under the file's old name when the change renamed it, then of
    its name elsewhere, as for a function moved; then of any name in its file's base, then elsewhere, as for one
    renamed. A base function lends to a function of another file only when its body is gone from its file, which
    now holds it fewer times than the base did: one still there, under any name, is the base of itself, and the
    other a copy. Failing a body, a function over the limit takes the highest-scoring base function of its name left
    in its file's base. A function of judged takes no base under _COMPLEXITY_EXEMPT, since the tests were never held
    to the limit; and a base that cannot be read or parsed has no functions.

    The functions of every other changed Python file, which the check does not judge, such as one under
    _COMPLEXITY_EXEMPT or a script whose name has no .py (_is_python_source), pair by body too, from any base, and in
    each step before the judged ones, so that one moved unchanged into such a file is still itself and lends its base
    to no copy, whatever the copy's name.

    The cost of bodies first: a copy of a function whose original the change edits takes the original's old score,
    as if the original were the new function under its name.
    """
    unjudged = {new: _working_functions(change, new)[0] or [] for _, new in change.files
                if new and new not in judged and _is_python_source(change, new)}
    working = {**unjudged, **{path: functions for path, (functions, _) in judged.items()}}
    old_paths = {new: old for old, new in change.files if new}
    homes = {path: old_paths.get(path, path) for path in working}
    pool = _BasePool({old: _base_functions(change, old) for old, new in change.files
                      if old and (new in working or _was_python_source(change, old))},
                     {old: working.get(new, []) if new else [] for old, new in change.files if old})
    for same_name, same_file in _BODY_STEPS:
        for path in [*unjudged, *judged]:
            pool.lend_by_body(path, working[path], homes[path], same_name=same_name, same_file=same_file,
                              judged=path in judged)
    for path, (functions, added) in judged.items():
        for scored in _over_limit(functions, added):
            pool.lend_by_name(path, scored, homes[path])
    return {(path, line): score for (path, line), score in pool.lent.items() if path in judged}


class _BasePool:
    """The functions of the change's files at the base, each of which lends its score to one function of the working
    tree at most, for _base_scores."""

    def __init__(self, bases: dict[str, list[_ScoredFunction]], now: dict[str, list[_ScoredFunction]]):
        """bases: the base functions of the change's files, by old path; now: the working tree's functions of the file
        each old path became, by old path, every one of bases' included, with none for a file the change deletes or
        that holds no Python now."""
        self._left = bases  # the base functions that have lent no score yet, by old path
        # (old path, body) of each body that the working tree's file holds fewer times than its base did: moved out of
        # it, edited or deleted, so that its base functions with that body may lend to functions of other files. The
        # count does not ask where the working tree's copies came from (TODO.md: "A body moved into a file ...").
        self._gone = {(old, body) for old, functions in bases.items()
                      for body, n in collections.Counter(g.body for g in functions).items()
                      if sum(f.body == body for f in now[old]) < n}
        self.lent: dict[tuple[str, int], int] = {}  # the score lent to each function, by (path, def line)

    def lend_by_body(self, path: str, functions: list[_ScoredFunction], home: str | None, *, same_name: bool,
                     same_file: bool, judged: bool) -> None:
        """Lend to each of functions, the working tree's in its file at path whose old path is home (None for a file
        the change adds), that has no score lent yet, the score of a base function left with its body: and its name
        for same_name, and in home's base for same_file, else in another file's, whose body is gone from it. judged
        says whether the check judges the file at path, whose functions then take no base under
        _COMPLEXITY_EXEMPT."""
        for scored in functions:
            if (path, scored.line) not in self.lent:
                found = self._twin(scored, home, same_name=same_name, same_file=same_file, judged=judged)
                if found is not None:
                    self._lend(path, scored, *found)

    def _twin(self, scored: _ScoredFunction, home: str | None, *, same_name: bool, same_file: bool,
              judged: bool) -> tuple[str, _ScoredFunction] | None:
        """The old path and the first base function left there that may lend to scored in lend_by_body's step, as its
        arguments of the same names say; None for no such function."""
        for old, left in self._left.items():
            if (old == home) != same_file or (judged and old.startswith(_COMPLEXITY_EXEMPT)):
                continue
            for base in left:
                if (base.body == scored.body and (base.name == scored.name or not same_name)
                        and (same_file or (old, base.body) in self._gone)):
                    return old, base
        return None

    def lend_by_name(self, path: str, scored: _ScoredFunction, home: str | None) -> None:
        """Lend to scored, a function of a judged file of the working tree at path whose old path is home, unless it
        has a score lent, the highest of the base functions of its name left in home's base, if that is outside
        _COMPLEXITY_EXEMPT."""
        if home is None or home.startswith(_COMPLEXITY_EXEMPT) or (path, scored.line) in self.lent:
            return
        namesakes = [g for g in self._left.get(home, []) if g.name == scored.name]
        if namesakes:
            self._lend(path, scored, home, max(namesakes, key=lambda g: g.score))

    def _lend(self, path: str, scored: _ScoredFunction, old: str, base: _ScoredFunction) -> None:
        self._left[old].remove(base)
        self.lent[(path, scored.line)] = base.score


def _judged_files(change: GateChange) -> tuple[list[dict], dict[str, tuple[list[_ScoredFunction], set[int]]]]:
    """A finding for each Python file outside _COMPLEXITY_EXEMPT that the change adds lines to and that cannot be read
    or parsed; and, by path, the scored functions of each of the others, with the lines the change adds to it."""
    out: list[dict] = []
    judged: dict[str, tuple[list[_ScoredFunction], set[int]]] = {}
    for changed in _parsed_changed_files(change, lambda path, added, removed: bool(added)
                                         and not path.startswith(_COMPLEXITY_EXEMPT)):
        if changed.tree is None:
            out.append(_finding(changed.path, None, "not-scored", f"{changed.why}, so its functions were not scored.",
                                severity="error"))
        else:
            judged[changed.path] = (_scored_functions(changed.tree), changed.added)
    return out, judged


@dataclass(frozen=True)
class _ChangedPythonFile:
    """A .py file of the change, as the working tree holds it and _parse_working_file reads it."""
    path: str
    added: set[int]  # the lines the change adds to it, from 1
    removed: set[int]  # the points it only removes lines at, as _removal_points gives them; empty when there are none
    text: str  # its source, decoded as Python decodes it; "" when it could not be read
    tree: ast.Module | None  # its syntax tree; None when it could not be read or does not parse
    why: str  # why tree is None, a clause such as "does not parse as Python"; "" when it is not None


def _parsed_changed_files(change: GateChange, wanted: Callable[[str, set[int], set[int]], bool]
                          = lambda path, added, removed: bool(added)) -> Iterator[_ChangedPythonFile]:
    """Each .py file the change touches that wanted(path, added, removed) accepts, by default each it adds lines to,
    read and parsed; the arguments are what the _ChangedPythonFile fields of those names hold. Only a regular file of
    the working tree is read: a symlink, a submodule, a FIFO or a device is left out whatever wanted says
    (_is_link_or_special). A file the change deletes is not among them, as changed_lines says, nor is one under
    mutmut's state, as GateChange.from_tree says."""
    removals = _removal_points(change.diff)
    for path, added in change.changed.items():
        removed = removals.get(path, set())
        if path.endswith(".py") and wanted(path, added, removed) and not _is_link_or_special(change, path):
            yield _ChangedPythonFile(path, added, removed, *_parse_working_file(change, path))


def _is_link_or_special(change: GateChange, path: str) -> bool:
    """Whether the working tree's path is there as anything but a regular file: a symlink, a submodule's directory,
    or a FIFO or device a symlink could name. The check reads none of them as code: a symlink's change is the name it
    points to, and a submodule's its commit; and reading what one points to could block, as on a FIFO, or never end,
    as on /dev/zero. False for a path that is not there, whose reading then fails as any other."""
    try:
        return not stat.S_ISREG(os.lstat(os.path.join(change.root, path)).st_mode)
    except OSError:
        return False


def _is_python_source(change: GateChange, path: str) -> bool:
    """Whether the working tree's path is a regular file of Python source, as _holds_python tells it. False for
    anything _is_link_or_special names, and for a file that cannot be read unless it is named *.py."""
    if _is_link_or_special(change, path):
        return False

    def first_line() -> bytes:
        try:
            with open(os.path.join(change.root, path), "rb") as source:
                return source.readline(_FIRST_LINE_BYTES)
        except OSError:
            return b""

    return _holds_python(path, first_line)


def _was_python_source(change: GateChange, path: str) -> bool:
    """Whether the change's base holds Python source at path, as _holds_python tells it: True for any path named
    *.py, without asking git; for another, False when the base holds no file there or git cannot read it."""

    def first_line() -> bytes:
        try:
            blob = _git(change.root, "cat-file", "blob", f"{change.base}:{path}", verbatim=True)
        except GateError:
            return b""
        return os.fsencode(blob)[:_FIRST_LINE_BYTES].split(b"\n", 1)[0]

    return _holds_python(path, first_line)


# How much of a file's first line _holds_python reads, in bytes: enough for any #! line.
_FIRST_LINE_BYTES = 256


def _holds_python(path: str, first_line: Callable[[], bytes]) -> bool:
    """Whether a file at path holds Python source: the path ends in .py, or the file's first line is a #! naming
    python. first_line gives that line, or its first _FIRST_LINE_BYTES bytes, and b"" for a file that cannot be read;
    it is called only when the name does not decide."""
    if path.endswith(".py"):
        return True
    first = first_line()
    return first.startswith(b"#!") and b"python" in first


def _working_functions(change: GateChange, path: str) -> tuple[list[_ScoredFunction] | None, str]:
    """The scored functions of the working tree's file at path, and ""; or None and why there are none, as
    _parse_working_file says."""
    _, tree, why = _parse_working_file(change, path)
    return (None, why) if tree is None else (_scored_functions(tree), "")


def _parse_working_file(change: GateChange, path: str) -> tuple[str, ast.Module | None, str]:
    """The source of the working tree's file at path, read as Python reads it, by its byte order mark or coding
    cookie, its syntax tree, and ""; or why there is no tree, a clause, after "" and None when the file could not be
    read, or after its source when it does not parse as Python."""
    try:
        with open(os.path.join(change.root, path), "rb") as source:
            text, _ = _decode_python(source.read())
    except (OSError, SyntaxError) as e:
        return "", None, f"could not be read ({e})"
    tree = _parse_python(text)
    return (text, None, "does not parse as Python") if tree is None else (text, tree, "")


def _base_functions(change: GateChange, path: str) -> list[_ScoredFunction]:
    """The functions the check judges as one in the file at path at the change's base, scored; none for a base that
    cannot be read or parsed, whose functions the change can then only be judged as adding."""
    try:
        # Verbatim, so that os.fsencode gives back the bytes git stores, which _decode_python decodes as Python would.
        tree = _parse_python(_decode_python(os.fsencode(
            _git(change.root, "cat-file", "blob", f"{change.base}:{path}", verbatim=True)))[0])
    except (GateError, SyntaxError):
        return []
    return _scored_functions(tree) if tree else []


# What a node holds, each with the nesting level it is scored at; None stands for a part the node goes without.
_Held = list[tuple[ast.AST | None, int]]
# A rule of cognitive_complexity's: the points a node of one type scores by itself at a nesting level, and _Held.
_CognitiveRule = Callable[[Any, int], tuple[int, _Held]]


def cognitive_complexity(function: ast.FunctionDef | ast.AsyncFunctionDef, *, recursive: bool = False) -> int:
    """function's cognitive complexity, by the rules above, the functions nested in it included. recursive says
    whether function itself recurses, which takes its module to tell (_recursive_functions); a function nested in it
    recurses when it calls itself. function's own decorators and defaults are not scored: they run where it is
    defined, not when it is called."""
    # A walk with a stack of its own rather than recursion: a syntax tree can be deeper than the interpreter lets a
    # recursive walk go, as a sum of a few hundred terms or an if with a thousand elifs are. Each node's points do not
    # depend on what it holds, so the order nodes are taken in does not matter. Each node goes with its nesting level
    # and the class whose body holds it directly, None outside one, which decides how a nested function recurses.
    points = int(recursive)
    todo: list[tuple[ast.AST | None, int, str | None]] = [(s, 0, None) for s in function.body]
    while todo:
        node, nesting, owner = todo.pop()
        if node is None:
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            # A function nested in the one judged, or a method of a class nested in it: its body one level deeper.
            points += int(_calls_itself(node, owner))
            outside = [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]
            todo += [*((o, nesting, owner) for o in outside), *((s, nesting + 1, None) for s in node.body)]
            continue
        if isinstance(node, ast.ClassDef):
            # A class nested in the one judged raises no level itself; its methods are nested functions.
            todo += [*((c, nesting, owner) for c in [*node.decorator_list, *node.bases, *node.keywords]),
                     *((s, nesting, node.name) for s in node.body)]
            continue
        rule = _COGNITIVE_RULES.get(type(node))
        if rule is None:
            todo += [(child, nesting, owner) for child in ast.iter_child_nodes(node)]
            continue
        own, held = rule(node, nesting)
        points += own
        todo += [(child, level, owner) for child, level in held]
    return points


def _cognitive_if(node: ast.If, nesting: int) -> tuple[int, _Held]:
    """An if, with the elifs and the else that follow it."""
    points = 1 + nesting
    held: _Held = []
    while True:
        held += [(node.test, nesting), *((s, nesting + 1) for s in node.body)]
        orelse = node.orelse
        # ast writes an elif as an else that holds just an if. That if starts in the elif's column; an if written
        # inside an else is indented further.
        if not (len(orelse) == 1 and isinstance(orelse[0], ast.If) and orelse[0].col_offset == node.col_offset):
            break
        node = orelse[0]
        points += 1
    return _with_else(points, held, orelse, nesting)


def _with_else(points: int, held: _Held, orelse: list[ast.stmt], nesting: int) -> tuple[int, _Held]:
    """points and held, with the else of an if, a loop or a try whose body is orelse: one point, and its body one level
    deeper; nothing for an empty orelse, which is no else."""
    if not orelse:
        return points, held
    return points + 1, held + [(s, nesting + 1) for s in orelse]


def _cognitive_loop(node: ast.For | ast.AsyncFor | ast.While, nesting: int) -> tuple[int, _Held]:
    head = [node.test] if isinstance(node, ast.While) else [node.target, node.iter]
    held: _Held = [*((h, nesting) for h in head), *((s, nesting + 1) for s in node.body)]
    return _with_else(1 + nesting, held, node.orelse, nesting)


def _cognitive_try(node: ast.Try | ast.TryStar, nesting: int) -> tuple[int, _Held]:
    held: _Held = [(s, nesting) for s in [*node.body, *node.finalbody]]
    for h in node.handlers:
        held += [(h.type, nesting), *((s, nesting + 1) for s in h.body)]
    return _with_else(len(node.handlers) * (1 + nesting), held, node.orelse, nesting)


def _cognitive_match(node: ast.Match, nesting: int) -> tuple[int, _Held]:
    # A case's pattern holds no operators and no branches; its guard and body are what can.
    return 1 + nesting, [(node.subject, nesting),
                         *((part, nesting + 1) for c in node.cases for part in [c.guard, *c.body])]


def _cognitive_ternary(node: ast.IfExp, nesting: int) -> tuple[int, _Held]:
    return 1 + nesting, [(node.test, nesting + 1), (node.body, nesting + 1), (node.orelse, nesting + 1)]


def _cognitive_boolean(node: ast.BoolOp, nesting: int) -> tuple[int, _Held]:
    """A sequence of boolean operators, its operands that are themselves and or or included: one point for its first
    operator and one for each that differs from the one before it."""
    ops, operands = _boolean_sequence(node)
    return 1 + sum(a is not b for a, b in itertools.pairwise(ops)), [(o, nesting) for o in operands]


def _boolean_sequence(node: ast.BoolOp) -> tuple[list[type[ast.boolop]], list[ast.expr]]:
    """The operators of node in the order they are written, those of an operand that is an and or or spliced in, and
    the operands that are neither."""
    ops: list[type[ast.boolop]] = []
    operands: list[ast.expr] = []
    # A stack of what is still to be read, the next item last: operands, and the operator types between them.
    todo: list[ast.expr | type[ast.boolop]] = [node]
    while todo:
        item = todo.pop()
        if isinstance(item, ast.BoolOp):
            written: list[ast.expr | type[ast.boolop]] = []
            for k, value in enumerate(item.values):
                written += [type(item.op), value] if k else [value]
            todo += reversed(written)
        elif isinstance(item, ast.expr):
            operands.append(item)
        else:
            ops.append(item)
    return ops, operands


def _cognitive_lambda(node: ast.Lambda, nesting: int) -> tuple[int, _Held]:
    return 0, [*((d, nesting) for d in [*node.args.defaults, *node.args.kw_defaults]), (node.body, nesting + 1)]


# The node types that cognitive_complexity scores by a rule of their own, beside the functions and classes nested in the
# judged one, which its walk takes itself; any other scores nothing itself, and what it holds is scored at its level.
_COGNITIVE_RULES: dict[type[ast.AST], _CognitiveRule] = {
    ast.If: _cognitive_if, ast.For: _cognitive_loop, ast.AsyncFor: _cognitive_loop, ast.While: _cognitive_loop,
    ast.Try: _cognitive_try, ast.TryStar: _cognitive_try, ast.Match: _cognitive_match, ast.IfExp: _cognitive_ternary,
    ast.BoolOp: _cognitive_boolean, ast.Lambda: _cognitive_lambda,
}


def _calls_itself(function: ast.FunctionDef | ast.AsyncFunctionDef, cls: str | None) -> bool:
    """Whether function's body calls function, as _callees resolves calls; cls is the name of the class function is a
    method of, None for one outside a class."""
    return (cls, function.name) in _callees(function, cls)


def _callees(function: ast.FunctionDef | ast.AsyncFunctionDef, cls: str | None,
             enclosing: frozenset[str] = frozenset()) -> set[tuple[str | None, str]]:
    """The functions of its module that function's body calls, the bodies of functions nested in it included, each as
    (the name of the class it is a method of, None outside a class; its name). cls is the name of the class function
    is a method of, or None, as the class names in the result are: qualified, as _scored_functions gives them.
    enclosing are the names the scopes around function bind for its body: the type parameters of the classes it is a
    method of.

    Calls resolve by name, as Python's scopes do (_Scope): f() calls the module's f unless a scope the call is read in
    binds f, as a parameter, a local, a nested function or a type parameter, and declares it neither global nor
    nonlocal; C.m() the method m of the module's class C, under the same proviso for C; and self.m() or cls.m() the
    method m of cls, or in a class nested in function, of that class, which is no class of the module. A call of
    anything else, such as an alias or an imported module's function, resolves to no function here."""
    calls: list[tuple[ast.expr, _Scope, str | None]] = []
    # Each node still to be read, with the scope it is read in and the class that self and cls name there. The
    # scopes' names are all known only once the walk is done, as Python fixes them at compile time, so the calls are
    # resolved after it.
    around = _Scope(None, "annotation", set(enclosing)) if enclosing else None
    root = _Scope(_Scope.of_type_parameters(function, around), "function", _parameters(function.args))
    todo: list[tuple[ast.AST, _Scope, str | None]] = [(s, root, cls) for s in function.body]
    while todo:
        node, scope, owner = todo.pop()
        if isinstance(node, ast.Call):
            calls.append((node.func, scope, owner))
        scope.bind(node)
        todo += _in_scopes(node, scope, owner, function.name)
    return {key for callee, scope, owner in calls if (key := _callee(callee, scope, owner)) is not None}


def _callee(callee: ast.expr, scope: "_Scope", owner: str | None) -> tuple[str | None, str] | None:
    """The function of the module that a call of callee, read in scope where self and cls name the class owner, calls,
    as _callees gives it; None for a call of anything else."""
    if isinstance(callee, ast.Name):
        return (None, callee.id) if scope.means_module(callee.id) else None
    if not (isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name)):
        return None
    if owner is not None and callee.value.id in ("self", "cls"):
        return owner, callee.attr
    return (callee.value.id, callee.attr) if scope.means_module(callee.value.id) else None


@dataclass
class _Scope:
    """A scope _callees reads a function in, as Python's compiler sees it: the function's own; a function, lambda,
    comprehension, class body or type-parameter list nested in it; or, around the function's own, its type-parameter
    list and those of the classes it is a method of. It tells what a bare name read there means
    (https://docs.python.org/3/reference/executionmodel.html#resolution-of-names)."""
    parent: "_Scope | None"  # the scope this one is nested in; None for the outermost, beyond which is the module
    # "function" for a function's or a lambda's; "class"; "comprehension"; or "annotation" for a type-parameter
    # list's, PEP 695's annotation scope, which unlike a function's sees the names of a class body it is directly in
    kind: str
    bound: set[str] = field(default_factory=set)  # the names it binds, and so means by itself
    globals: set[str] = field(default_factory=set)  # the names it declares global
    nonlocals: set[str] = field(default_factory=set)  # the names it declares nonlocal

    @classmethod
    def of_type_parameters(cls, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef,
                           parent: "_Scope | None") -> "_Scope | None":
        """The scope node's PEP 695 type parameters open inside parent, in which its annotations, bases and the
        parameters' bounds are read and its body's scope is nested; parent itself for a node with none."""
        names = _type_parameters(node)
        return cls(parent, "annotation", names) if names else parent

    def bind(self, node: ast.AST) -> None:
        """Take in what node, read in this scope, binds or declares there; nothing for any other node. A nested
        function's or class's own scope is _in_scopes'."""
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            self.bound.add(node.id)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            self._around_comprehensions().bound.add(node.target.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            self.bound.add(node.name)
        elif isinstance(node, ast.alias):
            self.bound.add((node.asname or node.name).partition(".")[0])
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            self.bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            self.bound.add(node.rest)
        elif isinstance(node, ast.Global):
            self.globals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            self.nonlocals.update(node.names)

    def _around_comprehensions(self) -> "_Scope":
        """This scope, or for a comprehension's, the nearest one around it that is no comprehension's: where an
        assignment expression read here binds its target (PEP 572)."""
        scope = self
        while scope.kind == "comprehension" and scope.parent is not None:
            scope = scope.parent
        return scope

    def means_module(self, name: str) -> bool:
        """Whether name, read in this scope, means the module's name rather than one bound in a scope of the chain:
        no scope from here out to the outermost binds it, unless one on the way declares it global. A class body
        counts only for a name read in it, or read in an annotation scope directly inside it, not in a scope nested
        in that. Valid once _callees has read the whole function."""
        scope: _Scope | None = self
        inner: _Scope | None = None  # the scope the walk came from, None at self
        while scope is not None:
            if name in scope.globals:
                return True
            sees_class = inner is None or (inner is self and inner.kind == "annotation")
            if name in scope.bound and name not in scope.nonlocals and (scope.kind != "class" or sees_class):
                return False
            scope, inner = scope.parent, scope
        return True


def _parameters(args: ast.arguments) -> set[str]:
    """The names args binds in its function's own scope; what its defaults and annotations hold is read outside."""
    return {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg] if a is not None}


# The nodes that open a scope of their own as a function's body does.
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _in_scopes(node: ast.AST, scope: _Scope, owner: str | None, function: str) -> list[tuple[ast.AST, _Scope, str | None]]:
    """What node holds, each with the scope it is read in and the class self and cls name there, for _callees, as
    Python reads it: a nested function, lambda, class and comprehension as _function_parts, _class_parts and
    _comprehension_parts say; an assignment expression's value, its target being _Scope.bind's; anything else in
    scope, as node is. function is the judged function's name, which marks a class nested in it as none of the
    module's."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        return _function_parts(node, scope, owner)
    if isinstance(node, ast.ClassDef):
        return _class_parts(node, scope, owner, f"<{node.name} in {function}>")
    if isinstance(node, _COMPREHENSIONS):
        return _comprehension_parts(node, scope, owner)
    if isinstance(node, ast.NamedExpr):
        return [(node.value, scope, owner)]
    return [(child, scope, owner) for child in ast.iter_child_nodes(node)]


def _function_parts(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, scope: _Scope,
                    owner: str | None) -> list[tuple[ast.AST, _Scope, str | None]]:
    """For _in_scopes: a nested function's or lambda's decorators and defaults, read in scope; its annotations and
    its type parameters' bounds, in its type parameters' scope; its body, in its own scope inside that."""
    args = node.args
    outside = [*getattr(node, "decorator_list", []), *args.defaults, *(d for d in args.kw_defaults if d is not None)]
    annotated = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    annotations = [a.annotation for a in annotated if a is not None and a.annotation is not None]
    annotations += [r for r in [getattr(node, "returns", None)] if r is not None] + _type_parameter_parts(node)
    typed = _Scope.of_type_parameters(node, scope) or scope
    inner = _Scope(typed, "function", _parameters(args))
    body = node.body if isinstance(node.body, list) else [node.body]
    return [*((o, scope, owner) for o in outside), *((a, typed, owner) for a in annotations),
            *((s, inner, owner) for s in body)]


def _class_parts(node: ast.ClassDef, scope: _Scope, owner: str | None,
                 marker: str) -> list[tuple[ast.AST, _Scope, str | None]]:
    """For _in_scopes: a nested class's decorators, read in scope; its bases, keywords and its type parameters'
    bounds, in its type parameters' scope; its body, in a class scope inside that, where self and cls name marker."""
    typed = _Scope.of_type_parameters(node, scope) or scope
    members = _Scope(typed, "class")
    inside = [*node.bases, *node.keywords, *_type_parameter_parts(node)]
    return [*((d, scope, owner) for d in node.decorator_list), *((x, typed, owner) for x in inside),
            *((s, members, marker) for s in node.body)]


def _comprehension_parts(node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp, scope: _Scope,
                         owner: str | None) -> list[tuple[ast.AST, _Scope, str | None]]:
    """For _in_scopes: a comprehension's first iterable, read in scope, as Python evaluates it before the
    comprehension starts; everything else, its loop targets included, in the comprehension's own scope."""
    inner = _Scope(scope, "comprehension")
    first, *rest = node.generators
    elements: list[ast.expr] = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
    return [(first.iter, scope, owner), *((x, inner, owner) for x in [first.target, *first.ifs, *rest, *elements])]


def _type_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef) -> set[str]:
    """The names of node's PEP 695 type parameters, as in def f[T](): none for a lambda, which has none."""
    return {t.name for t in getattr(node, "type_params", [])
            if isinstance(t, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)}


def _type_parameter_parts(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef) -> list[ast.expr]:
    """The bounds and defaults of node's PEP 695 type parameters, as in def f[T: int = bool](), which are read in the
    type parameters' own scope."""
    parts = [getattr(t, attr, None) for t in getattr(node, "type_params", []) for attr in ("bound", "default_value")]
    return [p for p in parts if p is not None]


# Vague names: names that fit anything, which tell a reader nothing about what they hold.

# The words the coding rules the agents are given ("Names" in the user's ~/.claude/CLAUDE.md) forbid as names that fit
# anything; vague_names finds a name that is one, or ends in one after an underscore, in any case. "result" is not
# here: the rules allow it for what a function returns, and telling that use from the others needs more than ast.
VAGUE_NAME_WORDS = frozenset({"data", "info", "value", "status", "handle", "manager"})


def vague_names(change: GateChange) -> list[dict]:
    """The vague-names check's find: each name that a line the change added to a Python file binds and that
    VAGUE_NAME_WORDS forbids, once per name and file, at the first such line; a binding on a line the change did not
    add is never reported. A name is bound as _bound_names says. A changed file that cannot be read or parsed is a
    finding saying so; the files are those _parsed_changed_files gives."""
    out = []
    for changed in _parsed_changed_files(change):
        path, added, tree = changed.path, changed.added, changed.tree
        if tree is None:
            out.append(_finding(path, None, "not-read", f"{changed.why}, so its names were not read."))
            continue
        # Each vague name's first binding on an added line, as (line, column).
        first: dict[str, tuple[int, int]] = {}
        for name, at in _bound_names(tree):
            if at[0] in added and name.lower().rpartition("_")[2] in VAGUE_NAME_WORDS:
                first[name] = min(first.get(name, at), at)
        out += [_finding(path, line, "vague-name", f"`{name}` could name anything; name it for what it holds.")
                for name, (line, _) in sorted(first.items(), key=lambda item: item[1])]
    return sorted(out, key=lambda f: (f["path"] or "", f["line"] or 0))


def _bound_names(tree: ast.Module) -> Iterator[tuple[str, tuple[int, int]]]:
    """Each name tree binds, with where: the line, from 1, and the column, from 0, of what binds it, such as its def.
    A name is bound by an assignment (augmented, annotated or :=, to a name or to an attribute of self or cls), as a
    parameter, a def or class, a for or comprehension target, a with's or an except's as, an import, a match pattern's
    capture, or a type parameter. global and nonlocal bind nothing new. A function marked @override, as PEP 698
    marks one whose name and signature its base class fixes, binds neither its name nor its parameters here."""
    fixed = _fixed_by_override(tree)
    for node in ast.walk(tree):
        name = _BINDING_NAME.get(type(node), lambda node: None)(node)
        # Every node _BINDING_NAME names is one of these, which carry where they are.
        if name and id(node) not in fixed and isinstance(node, (ast.stmt, ast.expr, ast.arg, ast.alias,
                                                                ast.excepthandler, ast.pattern, ast.type_param)):
            yield name, (node.lineno, node.col_offset)


# The name each kind of node that binds one binds, or None where that node binds none, as for a Name it only reads.
_BINDING_NAME: dict[type, Callable[[Any], str | None]] = {
    **dict.fromkeys((ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler, ast.MatchAs,
                     ast.MatchStar, ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple), lambda node: node.name),
    ast.arg: lambda node: node.arg,
    # `import a.b` binds a; `from m import *` binds names the module chooses.
    ast.alias: lambda node: None if node.name == "*" else node.asname or node.name.partition(".")[0],
    ast.Name: lambda node: node.id if isinstance(node.ctx, ast.Store) else None,
    ast.Attribute: lambda node: node.attr if isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name)
    and node.value.id in ("self", "cls") else None,
    ast.MatchMapping: lambda node: node.rest,
}


def _fixed_by_override(tree: ast.Module) -> set[int]:
    """The id()s of the defs in tree marked @override (typing's, or any decorator named override) and of their
    parameters: the names their base class fixes."""
    fixed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                (d.id if isinstance(d, ast.Name) else d.attr if isinstance(d, ast.Attribute) else "") == "override"
                for d in node.decorator_list):
            a = node.args
            fixed |= {id(node), *map(id, [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg])}
    return fixed


def gate_checks(mutation_budget: float = MUTATION_BUDGET,
                base_run_budget: float = BASE_RUN_BUDGET) -> tuple[GateCheck, ...]:
    """The checks judge_change runs after the linters, in this order, the surviving-mutants check letting mutmut run
    for mutation_budget seconds and the passes-at-base check letting pytest run for base_run_budget."""
    return (
        # Removing, reshaping or re-expecting a test is what a spec can ask for; skipping one or swallowing its error
        # only hides a failure.
        GateCheck("test-integrity", fails=True, find=weakened_tests,
                  weighed=frozenset({"removed-test", "fewer-assertions", "expected-value-changed"})),
        GateCheck("cognitive-complexity", fails=True, find=too_complex_functions),
        GateCheck("flaky-tests", fails=False, find=flaky_tests),
        GateCheck("surviving-mutants", fails=False, find=functools.partial(surviving_mutants, budget=mutation_budget)),
        GateCheck("passes-at-base", fails=False, find=functools.partial(passes_at_base, budget=base_run_budget)),
        GateCheck("vague-names", fails=False, find=vague_names),
    )


def _check_condition(name: str, n: int) -> str:
    """The failed condition of a check with n findings that fail the gate."""
    return f"{n} findings of the `{name}` check; each is listed below."


def gate_command(base: str, rules: str | None, coverage: Sequence[str], tests: Sequence[str], out: str | None,
                 mutation_budget: float, base_run_budget: float) -> int:
    """Write the quality file judge_change gives for the current directory, with gate_checks's checks, to out or
    standard output.

    Returns 0 for GATE: OK and 1 for GATE: ERROR; EXIT_GATE_UNJUDGED, having written nothing, when the change
    could not be judged or out could not be written.
    """
    try:
        text = judge_change(os.getcwd(), base, gate_checks(mutation_budget, base_run_budget), rules=rules,
                            coverage=coverage, tests=tests)
        if out:
            write_quality_file(out, text)
    except GateError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_GATE_UNJUDGED
    # quality_report's first line: the gate's own words, with no emphasis to tolerate.
    gate = text.split("\n", 1)[0].removeprefix("GATE: ")
    if out:
        print(f"GATE: {gate}, in {out}", file=sys.stderr)
    else:
        print(text, end="")
    return 0 if gate == GATE_OK else 1


def seconds_arg(text: str) -> int:
    """A time budget given on the command line, in whole seconds, at least 1."""
    try:
        seconds = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number of seconds, not {text!r}") from None
    if seconds < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {seconds}")
    return seconds


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The gate's command-line arguments argv, sys.argv[1:] by default, parsed: one attribute per option, as
    gate_command's parameters name them. Bad arguments and --help end the process through argparse's SystemExit,
    status 2 and 0."""
    parser = argparse.ArgumentParser(prog="gate.py")
    parser.description = (
        f"Run ruff, mypy, pymarkdown, shellcheck, actionlint and clippy over the files the repository tracks and "
        f"write the quality file: GATE: OK or GATE: ERROR on its first line, then the failed conditions, the findings "
        f"on lines added since --base and the checks' errors, the findings to weigh, failing tests, coverage of new "
        f"code, suppressions added and edited build configuration. Any finding on an added line or a check's error "
        f"fails the gate, and so do a linter that is missing or fails, a Cargo workspace that does not compile, a "
        f"failing test, and coverage below "
        f"{GATE_MIN_COVERAGE:g}% on {GATE_MIN_NEW_LINES} or more new lines to cover. Exit status 0 for OK, 1 for "
        f"ERROR, {EXIT_GATE_UNJUDGED} when it could not judge, writing no quality file.")
    parser.add_argument("--base", required=True, metavar="REV", help="the commit the change is measured from")
    parser.add_argument("--config", metavar="PYPROJECT",
                        help="take the linters' rules from this pyproject.toml instead of the tree's own")
    parser.add_argument("--coverage", metavar="XML", action="append", default=[],
                        help="a Cobertura coverage report of the tree, such as pytest-cov's coverage.xml or "
                             "cargo-llvm-cov's --cobertura output; given more than once, the reports are merged by "
                             "path and line, a line covered when any report covers it; without it, coverage is not "
                             "judged")
    parser.add_argument("--tests", metavar="XML", action="append", default=[],
                        help="a JUnit report of the tree's tests, such as pytest's --junitxml or cargo-nextest's; "
                             "given more than once, a failing test in any of them counts; without it, tests are not "
                             "judged")
    parser.add_argument("--out", metavar="FILE", help="write the quality file here instead of to standard output")
    parser.add_argument("--mutation-budget", type=seconds_arg, default=MUTATION_BUDGET, metavar="SECONDS",
                        help=f"stop mutmut, which mutation-tests the functions the change edits, after this long, "
                             f"and report how many of its mutants it tried; default {MUTATION_BUDGET}")
    parser.add_argument("--base-run-budget", type=seconds_arg, default=BASE_RUN_BUDGET, metavar="SECONDS",
                        help=f"stop the run of the changed tests against the base's code, which finds tests that "
                             f"pass without the change, after this long; default {BASE_RUN_BUDGET}")
    return parser.parse_args(argv)


def write_quality_file(path: str, text: str) -> None:
    """Make text, line endings as they are, the contents of the file at path.

    Written aside and renamed into place, so a failed write leaves the old file, not half a new one. A symlink at
    path is followed, so the link stays and its target changes, and an existing file keeps its permissions. Raises
    GateError when it cannot be written.
    """
    real = os.path.realpath(path)
    tmp = f"{real}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        with contextlib.suppress(FileNotFoundError):
            shutil.copymode(real, tmp)
        os.replace(tmp, real)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise GateError(f"cannot write {path}: {e}") from e


def main(argv: list[str] | None = None) -> int:
    """Judge the change in the current directory with the command-line arguments argv, sys.argv[1:] by default, as
    the module docstring says, and return the exit status: 0, 1 or EXIT_GATE_UNJUDGED as gate_command returns it,
    or EXIT_INTERRUPTED, saying so on standard error, when Ctrl-C stopped it. Bad arguments and --help end the
    process, as parse_args says."""
    args = parse_args(argv)
    try:
        return gate_command(args.base, args.config, args.coverage, args.tests, args.out, args.mutation_budget,
                            args.base_run_budget)
    except KeyboardInterrupt:
        # write_quality_file renames the whole file into place last, so out holds what it held before.
        print("interrupted; no quality file was written", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
