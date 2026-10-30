"""Property-based tests of the gate's parsers of what git wrote: its diffs and its quoted paths.

Hypothesis generates the inputs, as many as tests/conftest.py's profile says. git is the oracle: the tests run real
git in a temporary directory.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hypothesis import example, given
from hypothesis import strategies as st

from gate import _unquote, changed_lines

# git without the user's or the system's configuration, either of which could change what a diff looks like.
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}

# Every character str.splitlines ends a line at. Only "\n" ends one for git and sh.
LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


def git(cwd: str, *args: str) -> str:
    """git's output with its line ends as git wrote them, and each byte the file system's encoding cannot decode as a
    lone surrogate, as os.fsdecode makes it. Raises CalledProcessError when git fails. The calling test patches
    os.environ with GIT_ENV."""
    return os.fsdecode(subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout)  # noqa: S603, S607


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
    """changed_lines on what `git diff -U0`, run as the gate runs it (gate._change_diff), gets from real git for two
    generated trees. Runs git."""

    @given(changes())
    # An added line holding a lone carriage return, then text like a hunk header: read as a header, it added nine
    # lines to a one-line file.
    @example(({"00": "", "000": "", "0000": ""}, {"00": "", "000": "", "0000": "", "0": "\r@@ -1 +1,9 @@"}))
    def test_names_each_changed_file_and_exactly_its_added_lines(self, change):
        base, new = change
        with tempfile.TemporaryDirectory() as repo, patch.dict(os.environ, GIT_ENV):
            git(repo, "init", "-q")
            base_commit, new_commit = commit_tree(repo, base), commit_tree(repo, new)
            diff = git(repo, "-c", "core.quotePath=false", "diff", "-U0", "-M", "--no-color", "--no-ext-diff",
                       "--src-prefix=a/", "--dst-prefix=b/", base_commit, new_commit, "--")
            got = changed_lines(diff)
            counts = added_counts(repo, base_commit, new_commit)
        self.assertEqual(set(got), {path for path, content in new.items() if base.get(path) != content})
        for path, added in got.items():
            lines = file_lines(new[path])
            self.assertLessEqual(added, set(range(1, len(lines) + 1)), path)
            self.assertEqual(len(added), counts[path], path)
            # Whichever lines git chose to call added, the others must be the file's own lines at the base, in
            # order: those at its path, or, for a path new to the tree, those of a file the change deleted, which
            # git may pair with it as a rename.
            kept = [line for n, line in enumerate(lines, 1) if n not in added]
            before = [base[path]] if path in base else [c for p, c in base.items() if p not in new]
            self.assertTrue(not kept or any(is_subsequence(kept, file_lines(c)) for c in before), path)


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
