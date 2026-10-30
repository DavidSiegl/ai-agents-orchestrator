"""The hidden acceptance test of the eval task `feature`: top_words and `python -m tally --top N` (task.md).
evals/acceptance.py says how it is run."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # evals/, for acceptance.py
from acceptance import Acceptance, main

TEXT = "the cat and the hat and THE bat\n"


class Feature(Acceptance):
    """top_words, and --top on the command line."""

    def check_top_words(self):
        """top_words orders by count, then alphabetically, and gives [] for n <= 0."""
        from tally import top_words
        self.assertEqual(top_words(TEXT, 2), [("the", 3), ("and", 2)])
        self.assertEqual(top_words(TEXT, 3), [("the", 3), ("and", 2), ("bat", 1)])
        self.assertEqual(top_words(TEXT, 99), [("the", 3), ("and", 2), ("bat", 1), ("cat", 1), ("hat", 1)])
        self.assertEqual(top_words(TEXT, 0), [])
        self.assertEqual(top_words(TEXT, -1), [])
        self.assertEqual(top_words("", 3), [])

    def check_the_command_line_takes_top(self):
        """--top N prints the first N lines."""
        proc = self.python("-m", "tally", "--top", "2", stdin=TEXT)
        self.assertEqual((proc.returncode, proc.stdout), (0, "3 the\n2 and\n"), proc.stderr)

    def check_without_top_nothing_changed(self):
        """Without --top, every word, as before."""
        proc = self.python("-m", "tally", stdin=TEXT)
        self.assertEqual((proc.returncode, proc.stdout), (0, "3 the\n2 and\n1 bat\n1 cat\n1 hat\n"), proc.stderr)

    def check_a_bad_top_is_a_usage_error(self):
        """--top of anything but a positive integer exits 2 with a message."""
        for bad in ("0", "-2", "x"):
            with self.subTest(top=bad):
                proc = self.python("-m", "tally", "--top", bad, stdin=TEXT)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertTrue(proc.stderr.strip())

    def check_a_test_names_top_words(self):
        """The repository's tests test top_words."""
        named = [p for p in pathlib.Path(self.repo, "tests").rglob("*.py") if "top_words" in p.read_text()]
        self.assertTrue(named, "no test in tests/ names top_words")


if __name__ == "__main__":
    sys.exit(main(Feature))
