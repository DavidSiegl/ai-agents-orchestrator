"""ci/version-raised.sh, which fails a pull request build into main that does not raise the release version: run on
a repository of its own whose origin's main holds a version, against a branch that changes it."""

import os
import subprocess

from gitrepo import RepositoryTest, git

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "ci", "version-raised.sh")


def pyproject(version: str) -> str:
    """A pyproject.toml whose [project] has version, after a table whose own version line must not be taken for it."""
    return f'[tool.other]\nversion = "9.9.9"\n\n[project]\nname = "p"\nversion = "{version}"\n\n[tool.x]\nversion = "8"\n'


class TestVersionRaised(RepositoryTest):
    BASE_FILES = {"pyproject.toml": pyproject("0.9.0")}

    def setUp(self):
        super().setUp()
        origin = os.path.join(self.outside, "origin.git")
        git(self.outside, "init", "--quiet", "--bare", origin)
        self.git("remote", "add", "origin", origin)
        self.git("push", "--quiet", "origin", "main")
        self.git("switch", "--quiet", "-c", "change")

    def check(self, version: str) -> subprocess.CompletedProcess:
        """The script's run with HEAD's pyproject.toml at version, in a commit that changes another file too, so that
        it commits when the version is main's, as a pull request build into main runs it."""
        self.commit({"pyproject.toml": pyproject(version), "change.txt": version}, f"version {version}")
        return subprocess.run(["sh", SCRIPT], cwd=self.repo, env={**os.environ, "CHANGE_TARGET": "main"},  # noqa: S603, S607
                              capture_output=True, text=True, check=False)

    def test_a_raised_version_passes_compared_number_by_number(self):
        for version in ("0.9.1", "0.10.0", "1.0.0"):
            with self.subTest(version):
                proc = self.check(version)
                self.assertEqual((proc.returncode, proc.stderr), (0, ""))

    def test_a_kept_or_lowered_version_fails_naming_both(self):
        for version in ("0.9.0", "0.8.9"):
            with self.subTest(version):
                proc = self.check(version)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(f"version is {version}, not greater than main's 0.9.0", proc.stderr)

    def test_a_pyproject_without_a_project_version_fails(self):
        self.commit({"pyproject.toml": "[project]\nname = \"p\"\n"}, "no version")
        proc = subprocess.run(["sh", SCRIPT], cwd=self.repo, env={**os.environ, "CHANGE_TARGET": "main"},  # noqa: S603, S607
                              capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no [project] version in pyproject.toml", proc.stderr)
