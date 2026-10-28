"""The hidden acceptance test of the eval task `bugfix`: parse_duration of several units (task.md).
evals/acceptance.py says how it is run."""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # evals/, for acceptance.py
from acceptance import Acceptance, main


class Bugfix(Acceptance):
    """parse_duration adds up every unit, rejects what it rejected, and has a regression test."""

    def check_several_units(self):
        """Durations of several units, in any order."""
        from durations import parse_duration
        cases = {"1h30m": 5400, "2h5m10s": 7510, "10s1h": 3610, "1m1s": 61, " 1h0m5s ": 3605, "12m30s": 750}
        for text, seconds in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), seconds)

    def check_one_unit(self):
        """Durations of one unit, as before."""
        from durations import parse_duration
        self.assertEqual([parse_duration(t) for t in ("45s", "2m", "3h", "0s")], [45, 120, 10800, 0])

    def check_round_trip(self):
        """parse_duration reads what format_duration writes."""
        from durations import format_duration, parse_duration
        for seconds in [*range(0, 4000, 37), 86399, 90061]:
            with self.subTest(seconds=seconds):
                self.assertEqual(parse_duration(format_duration(seconds)), seconds)

    def check_still_rejected(self):
        """What is not a duration raises ValueError."""
        from durations import parse_duration
        for text in ("", " ", "5", "h", "1x", "1h-", "1h30", "m30", "1.5h", "-1h"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)

    def check_format_unchanged(self):
        """format_duration writes as before."""
        from durations import format_duration
        self.assertEqual([format_duration(s) for s in (0, 59, 61, 3600, 5400, 3605, 7505)],
                         ["0s", "59s", "1m1s", "1h", "1h30m", "1h5s", "2h5m5s"])

    def check_a_regression_test(self):
        """The repository's tests parse a duration of several units."""
        several = re.compile(r"""["']\s*\d+[hms]\d+[hms]""")
        named = [p for p in pathlib.Path(self.repo, "tests").rglob("*.py") if several.search(p.read_text())]
        self.assertTrue(named, "no test in tests/ parses a duration of several units")


if __name__ == "__main__":
    sys.exit(main(Bugfix))
