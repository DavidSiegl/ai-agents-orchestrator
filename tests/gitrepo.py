"""RepositoryTest, the base of the test cases that run orchestrator on a git repository of their own."""

import os
import tempfile
import unittest

from orchestrator import Host


class RepositoryTest(unittest.TestCase):
    """A test case with a fresh git repository, on branch main, removed after the test.

    setUp makes the repository, configured to commit as `t <t@example.com>` unsigned whatever the user's own git
    configuration says, and commits BASE_FILES to it as "base" unless BASE_FILES is empty. A subclass that needs more
    calls this setUp first. Its attributes, for the subclass and its tests:

    - repo: the absolute path of the repository's top level.
    - outside: an empty directory that is not inside the repository, for what a test keeps out of the tree, such as
      stub tools, a remote or a rules file; removed with the repository.
    - host: the Host the helpers run git through, for tests that call its other methods on repo.
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
        self.host = Host()
        self.host.git(tmp.name, "init", "--quiet", "-b", "main", self.repo)
        for key, setting in [("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git("config", key, setting)
        self.base = self.commit(self.BASE_FILES, "base") if self.BASE_FILES else None

    def git(self, *args):
        """The standard output of git args, run in the repository. Raises OrchestratorError when git fails."""
        return self.host.git(self.repo, *args)

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
        those made before the call included, with message; returns the new commit's sha. Raises OrchestratorError
        when git fails, as it does when there is nothing to commit."""
        for name, text in files.items():
            self.put(name, text)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD").strip()
