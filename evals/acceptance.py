"""What the eval tasks' hidden tests share: the repository they judge, running a command in it, and their main.

A hidden test (evals/run.py says how the runner runs one) subclasses Acceptance with methods named check_*, which
neither pytest nor unittest's default loader takes for tests, so that this repository's own test runs leave the hidden
tests alone, and ends with

    if __name__ == "__main__":
        sys.exit(main(TheSubclass))
"""

import os
import subprocess
import sys
import unittest


class Acceptance(unittest.TestCase):
    """The checks of one task's hidden test, run on the repository main was given. Every task's includes
    check_the_repository_tests_pass."""

    repo = ""  # the absolute path of the repository being judged, which main sets

    def in_repo(self, *argv: str, stdin: str = "", timeout: float = 300) -> subprocess.CompletedProcess:
        """argv run in the repository, given stdin, with its output captured as text; it fails the check when it
        cannot start or takes over timeout seconds, but not by its exit status, which the caller checks."""
        try:
            return subprocess.run(argv, cwd=self.repo, input=stdin, capture_output=True, text=True,  # noqa: S603
                                  timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.fail(f"{' '.join(argv)}: {e}")

    def python(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        """python args, on the Python running the hidden test, in the repository, as in_repo runs a command."""
        return self.in_repo(sys.executable, "-B", *args, stdin=stdin)

    def original(self, path: str) -> str:
        """The text of the file at path, relative to the repository, as the task found it: origin/main's, which a
        --no-pr run never moves."""
        proc = self.in_repo("git", "show", f"origin/main:{path}", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def check_the_repository_tests_pass(self):
        """The repository's own tests, `python -m unittest`, pass after the change."""
        proc = self.python("-m", "unittest")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])


def main(case: type[Acceptance], argv: list[str] | None = None) -> int:
    """Run case's check_* methods on the repository argv names, `hidden_test.py REPO`, its code importable; print
    each check's outcome, then the checks that did not pass, and return the exit status: 0 when all passed, 1 when
    one did not, 2 for a usage error."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not os.path.isdir(args[0]):
        print("usage: hidden_test.py REPO, a directory", file=sys.stderr)
        return 2
    Acceptance.repo = os.path.abspath(args[0])
    sys.path.insert(0, Acceptance.repo)
    loader = unittest.TestLoader()
    loader.testMethodPrefix = "check"
    result = unittest.TextTestRunner(verbosity=2).run(loader.loadTestsFromTestCase(case))
    if result.wasSuccessful():
        return 0
    # Last, so that the tail of the output the runner keeps names every check that did not pass.
    # A failure inside a subTest is reported with the subtest, whose test_case is the check.
    failed = sorted({getattr(test, "test_case", test)._testMethodName for test, _ in result.failures + result.errors})
    print(f"not accepted: {', '.join(failed)}", file=sys.stderr)
    return 1
