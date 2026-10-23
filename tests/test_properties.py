"""Property-based tests of the parsers that read what another program wrote: git's diffs and quoted paths, an env
file a shell also sources, and the first line of a handoff file.

Hypothesis generates the inputs, as many as tests/conftest.py's profile says. Where another program defines the
right answer, that program is the oracle: those tests say so, and run real git or sh in a temporary directory,
never FakeHost.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from hypothesis import example, given
from hypothesis import strategies as st

from orchestrator import (
    APPROVE,
    CHANGES_REQUESTED,
    GATE_BUILD_FAILED,
    GATE_ERROR,
    GATE_OK,
    Host,
    OrchestratorError,
    _unquote,
    changed_lines,
    ci_credentials,
    parse_gate,
    parse_verdict,
    quality_report,
)

# git without the user's or the system's configuration, either of which could change what a diff looks like.
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}

# Every character str.splitlines ends a line at. Only "\n" ends one for git and sh.
LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


def git(cwd: str, *args: str) -> str:
    """git's output with its line ends as git wrote them. The calling test patches os.environ with GIT_ENV."""
    return Host().git(cwd, *args, verbatim=True)


def text_without(chars: str, **kw) -> st.SearchStrategy[str]:
    """Any text, surrogates aside, without chars."""
    return st.text(st.characters(exclude_categories=("Cs",), exclude_characters=chars + "\0"), **kw)


# ---------------------------------------------------------------------------
# git: changed_lines and _unquote
# ---------------------------------------------------------------------------

# File names that git quotes, or that split wrongly in a naive parser: spaces, quotes, backslashes, tabs, newlines,
# control characters and non-ASCII, besides anything else. Never "/" or NUL, which no file name holds, nor .git*,
# which changes how git treats the tree.
file_names = st.text(st.one_of(st.sampled_from(list(' "\\\t\né\x01-')),
                               st.characters(exclude_categories=("Cs",), exclude_characters="/\0")),
                     min_size=1, max_size=12).filter(
    lambda n: n not in (".", "..", "sub dir") and not n.startswith(".git") and len(n.encode()) <= 255)
paths = st.tuples(st.sampled_from(["", "sub dir/"]), file_names).map("".join)

# A line is a few parts from a small vocabulary, so trees share lines and git has real hunks to find. The vocabulary
# has the line breaks git does not split at, and text that looks like the diff's own header lines.
line_parts = st.sampled_from(["x", "y", " ", "\r", "\x0c", "\x85", "\u2028", "@@ -1 +1,9 @@",
                              "diff --git a/q b/q", "+++ b/q", "--- a/q", "\\ No newline at end of file"])
contents = st.tuples(st.lists(st.lists(line_parts, max_size=3).map("".join), max_size=6), st.booleans()).map(
    lambda t: "\n".join(t[0]) + ("\n" if t[0] and t[1] else ""))


@st.composite
def changes(draw) -> tuple[dict[str, str], dict[str, str]]:
    """(base, new): two trees as {path: content}, the new one the base with files kept, edited, deleted and renamed
    (with or without an edit), and files added."""
    base = draw(st.dictionaries(paths, contents, max_size=4))
    new: dict[str, str] = {}
    for path, content in base.items():
        fate = draw(st.sampled_from(["keep", "edit", "delete", "rename"]))
        if fate == "keep":
            new[path] = content
        elif fate == "edit":
            new[path] = draw(contents)
        elif fate == "rename":
            to = draw(paths.filter(lambda p: p not in base and p not in new))
            new[to] = draw(st.one_of(st.just(content), contents))
    for path, content in draw(st.dictionaries(paths, contents, max_size=3)).items():
        if path not in base:
            new.setdefault(path, content)
    return base, new


def commit_tree(repo: str, tree: dict[str, str]) -> str:
    """Commit tree as the repository's whole content, replacing what it held; returns the commit's hash."""
    for entry in os.listdir(repo):
        if entry != ".git":
            full = os.path.join(repo, entry)
            shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
    for path, content in tree.items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "tree")
    return git(repo, "rev-parse", "HEAD").strip()


def file_lines(content: str) -> list[str]:
    """The lines git numbers in content: split at "\\n" alone, a final one ending the last line."""
    return content.removesuffix("\n").split("\n") if content else []


def added_counts(repo: str, base: str, new: str) -> dict[str, int]:
    """The number of lines `git diff --numstat` says each file in new gains, by its path in new."""
    fields = git(repo, "-c", "core.quotePath=false", "diff", "--numstat", "-z", "-M", base, new).split("\0")
    counts, i = {}, 0
    # Each entry is "added<TAB>deleted<TAB>path"; a rename's path is empty, its old and new paths the next two fields.
    while i < len(fields) - 1:
        added, _, path = fields[i].split("\t", 2)
        if path:
            i += 1
        else:
            path, i = fields[i + 2], i + 3
        counts[path] = int(added)
    return counts


def is_subsequence(short: list[str], long: list[str]) -> bool:
    rest = iter(long)
    return all(x in rest for x in short)


class TestChangedLines(unittest.TestCase):
    """changed_lines on what Host.change_diff gets from real git for two generated trees. Runs git."""

    @given(changes())
    # An added line holding a lone carriage return, then text like a hunk header: read as a header, it added nine
    # lines to a one-line file.
    @example(({"00": "", "000": "", "0000": ""}, {"00": "", "000": "", "0000": "", "0": "\r@@ -1 +1,9 @@"}))
    def test_names_each_changed_file_and_exactly_its_added_lines(self, change):
        base, new = change
        with tempfile.TemporaryDirectory() as repo, patch.dict(os.environ, GIT_ENV):
            git(repo, "init", "-q")
            base_commit, new_commit = commit_tree(repo, base), commit_tree(repo, new)
            got = changed_lines(Host().change_diff(repo, base_commit, new_commit))
            counts = added_counts(repo, base_commit, new_commit)
        self.assertEqual(set(got), {path for path, content in new.items() if base.get(path) != content})
        for path, added in got.items():
            lines = file_lines(new[path])
            self.assertLessEqual(added, set(range(1, len(lines) + 1)), path)
            self.assertEqual(len(added), counts[path], path)
            # Whichever lines git chose to call added, the others must be a file's lines at the base, in order: the
            # one it was renamed from or, for a file new to the tree, none.
            kept = [line for n, line in enumerate(lines, 1) if n not in added]
            self.assertTrue(not kept or any(is_subsequence(kept, file_lines(c)) for c in base.values()), path)


class TestUnquote(unittest.TestCase):
    """_unquote against git's own quoting of a file name, with core.quotePath on and off. Runs git."""

    @given(file_names)
    @example("\x80")  # quoted as two octal escapes, one per byte of its UTF-8, which became two characters
    def test_undoes_gits_quoting(self, name):
        with tempfile.TemporaryDirectory() as repo, patch.dict(os.environ, GIT_ENV):
            git(repo, "init", "-q")
            with open(os.path.join(repo, name), "w"):
                pass
            git(repo, "add", "-A")
            for quote_path in ("true", "false"):
                listed = git(repo, "-c", f"core.quotePath={quote_path}", "ls-files").removesuffix("\n")
                self.assertEqual(_unquote(listed), name, f"core.quotePath={quote_path}: {listed}")


# ---------------------------------------------------------------------------
# The env file: ci_credentials, so parse_env_file as the file reaches it
# ---------------------------------------------------------------------------

# Names under one prefix, so that none is a variable the shell sets or refuses itself: PWD, IFS, BASH_*.
env_names = st.from_regex(r"V[A-Za-z0-9_]{0,8}", fullmatch=True)
# A value in each form the documentation allows that a shell reads literally: unquoted from characters no shell
# expands, single-quoted, and double-quoted without what expands inside double quotes. Any other line break than
# "\n" may be in it; a carriage return may not, which parse_env_file refuses (test_refuses_a_carriage_return).
env_values = st.one_of(
    st.from_regex(r"[A-Za-z0-9_./:@%+,=#-]*", fullmatch=True),
    text_without("'\n\r").map(lambda v: f"'{v}'"),
    text_without('"\\$`\n\r').map(lambda v: f'"{v}"'),
)
padding = st.from_regex(r"[ \t]*", fullmatch=True)


@st.composite
def env_files(draw) -> tuple[str, set[str]]:
    """(text, names): an env file of blank lines, comments and assignments, with or without `export `, and the names
    it assigns."""
    lines, names = [], set()
    for kind in draw(st.lists(st.sampled_from(["blank", "comment", "assignment"]), max_size=8)):
        if kind == "blank":
            lines.append(draw(padding))
        elif kind == "comment":
            lines.append(draw(padding) + "#" + draw(text_without("\n")))
        else:
            name = draw(env_names)
            names.add(name)
            lines.append(draw(padding) + draw(st.sampled_from(["", "export "])) + name + "=" + draw(env_values)
                         + draw(padding))
    return "\n".join(lines) + "\n", names


def write_env_file(directory: str, text: str) -> str:
    """The path of a ci.env in directory holding text as it is, mode 600 as ci_credentials demands."""
    path = os.path.join(directory, "ci.env")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


def sh_exports(path: str, names: set[str]) -> dict[str, str]:
    """names as a POSIX shell has them after sourcing the file at path with `set -a`. The shell inherits the
    environment, which cannot matter: the file assigns every one of names."""
    proc = Host().run(["sh", "-c", 'set -a; . "$1"; exec env -0', "sh", path], verbatim=True)
    if proc.returncode != 0:
        raise AssertionError(f"sh could not source {path}: {proc.stderr}")
    exported = dict(entry.partition("=")[::2] for entry in proc.stdout.split("\0") if entry)
    return {name: exported[name] for name in names}


class TestEnvFile(unittest.TestCase):
    """ci_credentials reads an env file as sh sources it. Runs sh."""

    @given(env_files())
    # Line breaks str.splitlines knows and sh does not: a comment that went on as an assignment, and values that
    # were refused.
    @example(("# note\x85V=evil\n", set()))
    @example(("V='a\x0cb'\n", {"V"}))
    @example(("V='a\u2028b'\n", {"V"}))
    def test_reads_what_sh_exports(self, file):
        text, names = file
        with tempfile.TemporaryDirectory() as d:
            path = write_env_file(d, text)
            self.assertEqual(ci_credentials({}, path), sh_exports(path, names))

    @given(padding, env_names, env_values, st.sampled_from(["\r", "\r\n"]))
    def test_refuses_a_carriage_return(self, indent, name, value, line_end):
        with tempfile.TemporaryDirectory() as d:
            path = write_env_file(d, f"{indent}{name}={value}{line_end}")
            with self.assertRaisesRegex(OrchestratorError, ":1: a carriage return"):
                ci_credentials({}, path)


# ---------------------------------------------------------------------------
# The first line of a handoff file: parse_gate and parse_verdict
# ---------------------------------------------------------------------------

GATES = (GATE_OK, GATE_ERROR, GATE_BUILD_FAILED)
VERDICTS = (APPROVE, CHANGES_REQUESTED)
blank_lines = st.lists(padding).map(lambda ls: "".join(line + "\n" for line in ls))
emphasis = st.sampled_from([("", ""), ("**", "**"), ("_", "_"), ("`", "`"), ("# ", ""), ("> ", "")])
# What may follow the word on its line: nothing, or text after a space.
after_word = st.one_of(st.just(""), text_without(LINE_BREAKS).map(lambda t: " " + t))
non_blank_line = text_without(LINE_BREAKS, min_size=1).filter(str.strip)


class TestFirstLineParsers(unittest.TestCase):
    """parse_gate and parse_verdict read their word from the first non-blank line, and only from there."""

    @given(st.text())
    def test_never_raise_and_return_only_their_words(self, text):
        self.assertIn(parse_gate(text), (*GATES, None))
        self.assertIn(parse_verdict(text), (*VERDICTS, None))

    @given(blank_lines, emphasis, st.sampled_from(GATES), st.sampled_from(VERDICTS), after_word, st.text())
    @example("", ("_", "_"), GATE_OK, APPROVE, "", "")  # the closing underscore read as the word going on
    def test_read_the_word_on_the_first_non_blank_line(self, blanks, marks, gate, verdict, after, rest):
        before, behind = marks
        self.assertEqual(parse_gate(f"{blanks}{before}GATE: {gate}{behind}{after}\n{rest}"), gate)
        self.assertEqual(parse_verdict(f"{blanks}{before}VERDICT: {verdict}{behind}{after}\n{rest}"), verdict)

    @given(blank_lines, non_blank_line, st.text())
    def test_ignore_every_line_below_the_first_non_blank_one(self, blanks, first, rest):
        self.assertEqual(parse_gate(f"{blanks}{first}\n{rest}"), parse_gate(first))
        self.assertEqual(parse_verdict(f"{blanks}{first}\n{rest}"), parse_verdict(first))

    @given(st.sampled_from(GATES), st.text())
    def test_read_back_the_gate_quality_report_writes(self, gate, intro):
        self.assertEqual(parse_gate(quality_report(gate, intro)), gate)
