"""assert_golden, the golden-file helper of tests/golden.py, on golden files in a temporary directory."""

import os
import tempfile
import unittest
from unittest.mock import patch

import golden
from golden import assert_golden


class TestAssertGolden(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.join(tmp.name, "golden")
        for p in (patch.object(golden, "GOLDEN_DIR", self.dir), patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("UPDATE_GOLDEN", None)

    def read(self, name):
        with open(os.path.join(self.dir, f"{name}.txt"), encoding="utf-8", newline="") as f:
            return f.read()

    def test_update_writes_the_file_and_the_next_run_passes(self):
        os.environ["UPDATE_GOLDEN"] = "1"
        assert_golden("g", "one\r\ntwo\n")
        self.assertEqual(self.read("g"), "one\r\ntwo\n")
        del os.environ["UPDATE_GOLDEN"]
        assert_golden("g", "one\r\ntwo\n")

    def test_a_difference_fails_with_a_diff_naming_the_file(self):
        os.environ["UPDATE_GOLDEN"] = "1"
        assert_golden("g", "the gate\npassed\n")
        del os.environ["UPDATE_GOLDEN"]
        with self.assertRaises(AssertionError) as raised:
            assert_golden("g", "the gate\nfailed")
        message = str(raised.exception)
        self.assertIn(f"{os.path.join(self.dir, 'g.txt')} differs from the text written now", message)
        self.assertIn("UPDATE_GOLDEN=1", message)
        self.assertIn(" the gate\n-passed\n+failed\n\\ No newline at end of file\n", message)
        self.assertEqual(self.read("g"), "the gate\npassed\n")

    def test_a_line_end_alone_is_a_difference(self):
        os.environ["UPDATE_GOLDEN"] = "1"
        assert_golden("g", "a\n")
        del os.environ["UPDATE_GOLDEN"]
        with self.assertRaises(AssertionError):
            assert_golden("g", "a\r\n")

    def test_the_diff_ends_a_line_only_at_a_newline(self):
        os.environ["UPDATE_GOLDEN"] = "1"
        assert_golden("g", "a\rb c\n")
        del os.environ["UPDATE_GOLDEN"]
        with self.assertRaises(AssertionError) as raised:
            assert_golden("g", "a\rb d\n")
        self.assertIn("\n-a\rb c\n+a\rb d\n", str(raised.exception))
        self.assertNotIn("No newline", str(raised.exception))

    def test_a_missing_file_fails_naming_it(self):
        with self.assertRaises(AssertionError) as raised:
            assert_golden("absent", "text")
        self.assertIn(os.path.join(self.dir, "absent.txt"), str(raised.exception))
        self.assertFalse(os.path.exists(self.dir))
