"""Build each eval task's `repo.bundle`, the toy repository its agents start from, out of the files this script holds.

    uv run python evals/make_bundles.py           # write evals/tasks/<name>/repo.bundle for every task in TOY_REPOS
    uv run python evals/make_bundles.py --check   # exit 1, naming them, if a bundle is not of the files here

The toy repositories live here as text, not as files under evals/, so that neither pytest nor the quality gate takes
their tests, or their deliberate bug, for this repository's own. A bundle holds one commit, on `main`, whose id
depends only on these files: the author, committer, dates and message are fixed and the user's git configuration is
not read. So --check compares commit ids, not bytes, which another git version may pack differently; a change to a
toy repository is reviewed here, and its bundle rebuilt with this script.
"""

import argparse
import os
import subprocess
import sys
import tempfile

TASKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks")
BUNDLE_NAME = "repo.bundle"
BRANCH = "main"

# Who made, and when, the commit of every toy repository, so that its id is the same on every machine.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "Octoflow Evals", "GIT_AUTHOR_EMAIL": "evals@example.com",
    "GIT_COMMITTER_NAME": "Octoflow Evals", "GIT_COMMITTER_EMAIL": "evals@example.com",
    "GIT_AUTHOR_DATE": "2026-10-28T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-10-28T00:00:00+00:00",
    # No global or system configuration: a user's commit.gpgsign, hooks path or default branch would change the commit.
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

_GITIGNORE = "__pycache__/\n*.pyc\n"


def _pyproject(name: str, description: str) -> str:
    return (f'[project]\nname = "{name}"\nversion = "0.1.0"\ndescription = "{description}"\n'
            f'requires-python = ">=3.11"\ndependencies = []\n')


# Each task's toy repository, by the task's name (its directory under evals/tasks/): its files' paths and text.
TOY_REPOS: dict[str, dict[str, str]] = {
    # A feature: top_words and `--top N`, on a word counter.
    "feature": {
        ".gitignore": _GITIGNORE,
        "pyproject.toml": _pyproject("tally", "Word counts of a text"),
        "README.md": "# tally\n\nWord counts of a text.\n\n```sh\npython -m tally < book.txt\n```\n\n"
                     "Prints a line per word, its count and the word, most frequent first.\n\n"
                     "Run the tests with `python -m unittest`.\n",
        "tally/__init__.py": '"""Word counts of a text."""\n\nfrom .counter import count_words, words\n\n'
                             '__all__ = ["count_words", "words"]\n',
        "tally/counter.py": '''\
"""Counting the words of a text."""

import re

WORD = re.compile(r"[a-z']+")


def words(text):
    """The words of text, lowercased, in order: runs of letters and apostrophes."""
    return WORD.findall(text.lower())


def count_words(text):
    """How often each word of text occurs, as a dict from word to count."""
    counts = {}
    for word in words(text):
        counts[word] = counts.get(word, 0) + 1
    return counts
''',
        "tally/__main__.py": '''\
"""Print the word counts of standard input, a line each: the count, a space and the word, most frequent first,
and words as frequent in alphabetical order."""

import sys

from .counter import count_words


def main():
    counts = count_words(sys.stdin.read())
    for word, n in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(n, word)
    return 0


if __name__ == "__main__":
    sys.exit(main())
''',
        "tests/__init__.py": "",
        "tests/test_counter.py": '''\
import unittest

from tally import count_words, words


class TestCounter(unittest.TestCase):
    def test_words_are_lowercased(self):
        self.assertEqual(words("The cat, the HAT."), ["the", "cat", "the", "hat"])

    def test_count_words(self):
        self.assertEqual(count_words("a b a"), {"a": 2, "b": 1})

    def test_no_words(self):
        self.assertEqual(count_words(" ,. "), {})
''',
    },
    # A bug with a reproducer: parse_duration does not reset its number after a unit.
    "bugfix": {
        ".gitignore": _GITIGNORE,
        "pyproject.toml": _pyproject("durations", "Durations written as 1h30m"),
        "README.md": "# durations\n\nDurations written as `1h30m`, `45s` or `2h5m10s`: `parse_duration` reads one, "
                     "`format_duration` writes one.\n\nRun the tests with `python -m unittest`.\n",
        "durations/__init__.py": '"""Durations written as 1h30m, 45s or 2h5m10s."""\n\n'
                                 "from .duration import format_duration, parse_duration\n\n"
                                 '__all__ = ["format_duration", "parse_duration"]\n',
        "durations/duration.py": '''\
"""Reading and writing durations such as 1h30m."""

UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """The seconds a duration such as "1h30m" stands for: one or more numbers, each followed by a unit, h, m or s.
    Surrounding whitespace is ignored. Raises ValueError for anything else, the empty string included."""
    text = text.strip()
    if not text or text[-1] not in UNITS:
        raise ValueError(f"not a duration: {text!r}")
    total, digits = 0, ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif ch in UNITS and digits:
            total += int(digits) * UNITS[ch]
        else:
            raise ValueError(f"not a duration: {text!r}")
    return total


def format_duration(seconds):
    """seconds, a non-negative int, written as parse_duration reads it: hours, minutes and seconds, each only when
    not 0, and "0s" for 0."""
    if seconds < 0:
        raise ValueError(f"a negative duration: {seconds}")
    parts = []
    for unit, size in UNITS.items():
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n}{unit}")
    return "".join(parts) or "0s"
''',
        "tests/__init__.py": "",
        "tests/test_duration.py": '''\
import unittest

from durations import format_duration, parse_duration


class TestParse(unittest.TestCase):
    def test_one_unit(self):
        self.assertEqual(parse_duration("45s"), 45)
        self.assertEqual(parse_duration("2m"), 120)
        self.assertEqual(parse_duration(" 3h "), 10800)

    def test_not_a_duration(self):
        for text in ("", "5", "h", "1x", "1h-"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)


class TestFormat(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(5400), "1h30m")
        self.assertEqual(format_duration(3605), "1h5s")
''',
    },
    # A refactor that must keep behaviour: two renderers that repeat each other's formatting and rounding.
    "refactor": {
        ".gitignore": _GITIGNORE,
        "pyproject.toml": _pyproject("invoice", "Plain-text invoices and credit notes"),
        "README.md": "# invoice\n\nPlain-text invoices and credit notes: `render_invoice` and `render_credit_note`."
                     "\n\nRun the tests with `python -m unittest`.\n",
        "invoice/__init__.py": '"""Plain-text invoices and credit notes."""\n\n'
                               "from .render import render_credit_note, render_invoice\n\n"
                               '__all__ = ["render_credit_note", "render_invoice"]\n',
        "invoice/render.py": '''\
"""Rendering invoices and credit notes as plain text."""

from decimal import ROUND_HALF_UP, Decimal


def render_invoice(customer, items, tax_rate):
    """The invoice for customer, as text: a line per item with its name, quantity, unit price and amount, then the
    subtotal, the tax and the total. items is a list of (name, quantity, unit price), quantity an int and the price
    a Decimal; tax_rate is a Decimal fraction, such as Decimal("0.2") for 20%. Amounts are rounded to the cent, half
    up, a line's before it is added to the subtotal."""
    lines = [f"Invoice for {customer}", ""]
    subtotal = Decimal(0)
    for name, quantity, price in items:
        amount = (price * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        subtotal += amount
        unit = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        lines.append(f"{name[:20]:<20} {quantity:>4} x {unit:>9} = {amount:>10}")
    tax = (subtotal * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    percent = str(tax_rate * 100)
    if "." in percent:
        percent = percent.rstrip("0").rstrip(".")
    lines.append("")
    lines.append(f"{'Subtotal':<36} {subtotal:>10}")
    lines.append(f"{'Tax ' + percent + '%':<36} {tax:>10}")
    lines.append(f"{'Total':<36} {subtotal + tax:>10}")
    return "\\n".join(lines) + "\\n"


def render_credit_note(customer, items, tax_rate):
    """The credit note for customer, as text: as render_invoice's invoice for the same items, headed "Credit note
    for", with every amount, the unit prices included, negative."""
    lines = [f"Credit note for {customer}", ""]
    subtotal = Decimal(0)
    for name, quantity, price in items:
        amount = -(price * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        subtotal += amount
        unit = -price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        lines.append(f"{name[:20]:<20} {quantity:>4} x {unit:>9} = {amount:>10}")
    tax = (subtotal * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    percent = str(tax_rate * 100)
    if "." in percent:
        percent = percent.rstrip("0").rstrip(".")
    lines.append("")
    lines.append(f"{'Subtotal':<36} {subtotal:>10}")
    lines.append(f"{'Tax ' + percent + '%':<36} {tax:>10}")
    lines.append(f"{'Total':<36} {subtotal + tax:>10}")
    return "\\n".join(lines) + "\\n"
''',
        "tests/__init__.py": "",
        "tests/test_render.py": '''\
import unittest
from decimal import Decimal

from invoice import render_credit_note, render_invoice

ITEMS = [("Widget", 3, Decimal("2.50")), ("Gadget", 1, Decimal("10"))]


class TestRender(unittest.TestCase):
    def test_invoice_total(self):
        text = render_invoice("ACME", ITEMS, Decimal("0.2"))
        self.assertTrue(text.startswith("Invoice for ACME\\n"))
        self.assertIn("Tax 20%", text)
        self.assertTrue(text.endswith("     21.00\\n"))

    def test_credit_note_is_negative(self):
        text = render_credit_note("ACME", ITEMS, Decimal("0.2"))
        self.assertTrue(text.startswith("Credit note for ACME\\n"))
        self.assertTrue(text.endswith("    -21.00\\n"))
''',
    },
    # A docs change: a README without usage, and functions without docstrings; the code must not change.
    "docs": {
        ".gitignore": _GITIGNORE + "store.json\n",
        "pyproject.toml": _pyproject("kvstore", "A key-value store in a JSON file"),
        "README.md": "# kvstore\n\nA key-value store in a JSON file.\n",
        "kvstore/__init__.py": '"""A key-value store in a JSON file."""\n\n'
                               "from .store import delete, get, keys, put\n\n"
                               '__all__ = ["delete", "get", "keys", "put"]\n',
        "kvstore/store.py": '''\
"""A key-value store kept in a JSON file: one object, from string keys to string values."""

import json
import os


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\\n")
    os.replace(tmp, path)


def get(path, key):
    return _load(path).get(key)


def put(path, key, value):
    data = _load(path)
    data[key] = value
    _save(path, data)


def delete(path, key):
    data = _load(path)
    if key not in data:
        return False
    del data[key]
    _save(path, data)
    return True


def keys(path):
    return sorted(_load(path))
''',
        "kvstore/__main__.py": '''\
import argparse
import sys

from . import store


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kvstore")
    parser.add_argument("--file", default="store.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("get").add_argument("key")
    put = commands.add_parser("set")
    put.add_argument("key")
    put.add_argument("value")
    commands.add_parser("delete").add_argument("key")
    commands.add_parser("list")
    args = parser.parse_args(argv)
    if args.command == "get":
        value = store.get(args.file, args.key)
        if value is None:
            print(f"no key {args.key}", file=sys.stderr)
            return 1
        print(value)
    elif args.command == "set":
        store.put(args.file, args.key, args.value)
    elif args.command == "delete":
        if not store.delete(args.file, args.key):
            print(f"no key {args.key}", file=sys.stderr)
            return 1
    else:
        for key in store.keys(args.file):
            print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
''',
        "tests/__init__.py": "",
        "tests/test_store.py": '''\
import os
import tempfile
import unittest

from kvstore import delete, get, keys, put
from kvstore.__main__ import main


class TestStore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "store.json")

    def test_put_get_delete(self):
        put(self.path, "a", "1")
        self.assertEqual(get(self.path, "a"), "1")
        self.assertEqual(keys(self.path), ["a"])
        self.assertTrue(delete(self.path, "a"))
        self.assertFalse(delete(self.path, "a"))
        self.assertIsNone(get(self.path, "a"))

    def test_get_of_a_missing_key_exits_1(self):
        self.assertEqual(main(["--file", self.path, "get", "nope"]), 1)
''',
    },
    # A task whose spec forbids a dependency: aligned tables, which tabulate or rich would make.
    "no-dependency": {
        ".gitignore": _GITIGNORE,
        "pyproject.toml": _pyproject("report", "CSV files, read and written again"),
        "README.md": "# report\n\nReads a CSV file and writes it again, normalized:\n\n"
                     "```sh\npython -m report data.csv\n```\n\nRun the tests with `python -m unittest`.\n",
        "report/__init__.py": '"""CSV files, read and written again."""\n\n'
                              "from .csvfile import read_rows, write_csv\n\n"
                              '__all__ = ["read_rows", "write_csv"]\n',
        "report/csvfile.py": '''\
"""Reading and writing CSV files."""

import csv
import io


def read_rows(path):
    """The header and the rows of the CSV file at path: (a list of column names, a list of lists of cells)."""
    with open(path, newline="", encoding="utf-8") as f:
        header, *rows = list(csv.reader(f))
    return header, rows


def write_csv(header, rows):
    """header and rows as CSV text, with "\\\\n" line endings."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()
''',
        "report/__main__.py": '''\
"""Write a CSV file to standard output again, in the format --format names."""

import argparse
import sys

from .csvfile import read_rows, write_csv


def main(argv=None):
    parser = argparse.ArgumentParser(prog="report")
    parser.add_argument("--format", choices=["csv"], default="csv")
    parser.add_argument("file")
    args = parser.parse_args(argv)
    header, rows = read_rows(args.file)
    sys.stdout.write(write_csv(header, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
''',
        "tests/__init__.py": "",
        "tests/test_csvfile.py": '''\
import os
import tempfile
import unittest

from report import read_rows, write_csv


class TestCsv(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "data.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("name,qty\\r\\napple,3\\r\\n")
            header, rows = read_rows(path)
        self.assertEqual((header, rows), (["name", "qty"], [["apple", "3"]]))
        self.assertEqual(write_csv(header, rows), "name,qty\\napple,3\\n")
''',
    },
}


def _git(cwd: str, *args: str) -> str:
    """The output of git args, run in cwd with the fixed identity and dates; GitError when it fails."""
    argv = ["git", "-C", cwd, *args]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False,  # noqa: S603
                              env={**os.environ, **_COMMIT_ENV})
    except OSError as e:
        raise GitError(f"could not run git {args[0]}: {e}") from e
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} in {cwd} failed: {proc.stderr.strip()}")
    return proc.stdout


class GitError(Exception):
    """A git command these functions run failed, or could not start; the message says which and why."""


def make_bundle(files: dict[str, str], bundle: str, message: str = "The starting point") -> str:
    """Write bundle, a git bundle of one commit on main holding files (paths to text) and nothing else, its HEAD on
    main, and return the commit's id, which depends only on files and message. An existing bundle is replaced.
    Raises GitError when git fails and OSError when a file cannot be written."""
    with tempfile.TemporaryDirectory(prefix="toy-repo-") as repo:
        _git(repo, "init", "--quiet", f"--initial-branch={BRANCH}")
        for path, text in files.items():
            full = os.path.join(repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        _git(repo, "add", "--all")
        _git(repo, "commit", "--quiet", "--no-verify", "-m", message)
        _git(repo, "bundle", "create", "--quiet", os.path.abspath(bundle), "HEAD", BRANCH)
        return _git(repo, "rev-parse", "HEAD").strip()


def bundle_commit(bundle: str) -> str:
    """The id of the commit main points to in bundle; GitError when bundle is not a readable bundle with a main."""
    heads = _git(os.path.dirname(os.path.abspath(bundle)), "bundle", "list-heads", os.path.abspath(bundle))
    for line in heads.splitlines():
        commit, _, ref = line.partition(" ")
        if ref == f"refs/heads/{BRANCH}":
            return commit
    raise GitError(f"{bundle} has no {BRANCH} branch")


def _message(name: str) -> str:
    return f"The starting point of the eval task {name}"


def main(argv: list[str] | None = None) -> int:
    """Write every task's bundle, or with --check compare each with TOY_REPOS; 1 if a bundle differs or is missing,
    or git fails."""
    parser = argparse.ArgumentParser(description="Build the eval tasks' repo.bundle files from TOY_REPOS.")
    parser.add_argument("--check", action="store_true",
                        help="change nothing; exit 1 if a bundle is missing or not of TOY_REPOS's files")
    args = parser.parse_args(argv)
    stale = []
    try:
        for name, files in TOY_REPOS.items():
            bundle = os.path.join(TASKS_DIR, name, BUNDLE_NAME)
            if args.check:
                with tempfile.TemporaryDirectory(prefix="toy-bundle-") as tmp:
                    expected = make_bundle(files, os.path.join(tmp, BUNDLE_NAME), _message(name))
                if not os.path.exists(bundle) or bundle_commit(bundle) != expected:
                    stale.append(bundle)
            else:
                os.makedirs(os.path.dirname(bundle), exist_ok=True)
                print(f"{bundle}: {make_bundle(files, bundle, _message(name))}")
    except (GitError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for bundle in stale:
        print(f"{bundle} is not of TOY_REPOS's files: run evals/make_bundles.py", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
