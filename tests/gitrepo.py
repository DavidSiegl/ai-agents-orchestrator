"""RepositoryTest, the base of the test cases that run the gate on a git repository of their own, and git, which
runs git for them."""

import os
import subprocess
import tempfile
import unittest


def git(cwd, *args):
    """The standard output of git args, run in cwd, as text. Raises RuntimeError, with git's standard error, when git
    exits with a status other than 0."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603, S607
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd} failed: {proc.stderr.strip()}")
    return proc.stdout


class RepositoryTest(unittest.TestCase):
    """A test case with a fresh git repository, on branch main, removed after the test.

    setUp makes the repository, configured to commit as `t <t@example.com>` unsigned whatever the user's own git
    configuration says, and commits BASE_FILES to it as "base" unless BASE_FILES is empty. A subclass that needs more
    calls this setUp first. Its attributes, for the subclass and its tests:

    - repo: the absolute path of the repository's top level.
    - outside: an empty directory that is not inside the repository, for what a test keeps out of the tree, such as
      stub tools, a remote or a rules file; removed with the repository.
    - base: the sha of the commit setUp made of BASE_FILES, or None when BASE_FILES is empty.
    """

    # The files of the base commit: each path, relative to the repository, to its text. Empty: setUp commits nothing,
    # and the repository has no HEAD until a test commits.
    BASE_FILES: dict[str, str] = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, self.outside = (os.path.join(tmp.name, d) for d in ("repo", "outside"))
        os.mkdir(self.outside)
        git(tmp.name, "init", "--quiet", "-b", "main", self.repo)
        for key, setting in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, setting)
        self.base = self.commit(self.BASE_FILES, "base") if self.BASE_FILES else None

    def git(self, *args):
        """The standard output of git args, run in the repository. Raises RuntimeError when git fails."""
        return git(self.repo, *args)

    def put(self, name, text):
        """Write text to the file name, a path relative to the repository, replacing it, and making its directories
        if they are missing. Stages nothing."""
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def edit(self, name, old, new):
        """Replace old with new in the file name, as put names it; fails the test unless old occurs there exactly
        once."""
        with open(os.path.join(self.repo, name)) as f:
            text = f.read()
        self.assertEqual(text.count(old), 1, old)
        self.put(name, text.replace(old, new))

    def commit(self, files, message):
        """Put files, a mapping of path to text as put takes them, then commit every change in the working tree,
        those made before the call included, with message; returns the new commit's sha. Raises RuntimeError
        when git fails, as it does when there is nothing to commit."""
        for name, text in files.items():
            self.put(name, text)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD").strip()
