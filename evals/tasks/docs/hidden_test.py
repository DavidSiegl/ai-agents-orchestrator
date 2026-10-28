"""The hidden acceptance test of the eval task `docs`: kvstore's README usage and docstrings, its code unchanged
(task.md). evals/acceptance.py says how it is run."""

import ast
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # evals/, for acceptance.py
from acceptance import Acceptance, main

MODULES = ["kvstore/__init__.py", "kvstore/store.py", "kvstore/__main__.py"]
COMMANDS = ["get", "set", "delete", "list"]


def without_docstrings(source: str) -> str:
    """The syntax tree of source, dumped, without the docstring of the module or of any class or function."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) \
                and ast.get_docstring(node, clean=False) is not None:
            node.body = node.body[1:] or [ast.Pass()]
    return ast.dump(tree)


class Docs(Acceptance):
    """The README documents every command, every function has a docstring, and no statement changed."""

    def usage(self) -> str:
        """The text of README.md's `## Usage` section, up to the next heading of its level or above; "" without one."""
        lines, fenced = [], False
        for line in pathlib.Path(self.repo, "README.md").read_text().splitlines(keepends=True):
            # A line starting with # inside a fenced block, such as a shell comment, is not a heading.
            heading = not fenced and re.match(r"#{1,2} ", line)
            if line.startswith("```"):
                fenced = not fenced
            if heading and lines:
                break
            if lines or (heading and re.match(r"## Usage\s*$", line)):
                lines.append(line)
        return "".join(lines[1:])

    def check_code_unchanged(self):
        """Each module is as the task found it but for docstrings."""
        for module in MODULES:
            with self.subTest(module=module):
                after = pathlib.Path(self.repo, module).read_text()
                self.assertEqual(without_docstrings(after), without_docstrings(self.original(module)))

    def check_every_function_has_a_docstring(self):
        """store.py's and __main__.py's functions, and __main__.py itself, have docstrings."""
        for module in ("kvstore/store.py", "kvstore/__main__.py"):
            tree = ast.parse(pathlib.Path(self.repo, module).read_text())
            self.assertTrue((ast.get_docstring(tree) or "").strip(), f"{module} has no module docstring")
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    with self.subTest(function=f"{module}:{node.name}"):
                        self.assertTrue((ast.get_docstring(node) or "").strip())

    def check_usage_shows_each_command(self):
        """The usage has an example of each command in a fenced block, and names --file and its default."""
        section = self.usage()
        self.assertTrue(section, "README.md has no ## Usage section")
        fenced = "\n".join(re.findall(r"^```[^\n]*\n(.*?)^```", section, re.MULTILINE | re.DOTALL))
        for command in COMMANDS:
            with self.subTest(command=command):
                self.assertRegex(fenced, rf"kvstore\b[^\n]*\s{command}\b")
        self.assertIn("--file", section)
        self.assertIn("store.json", section)

    def check_usage_says_a_missing_key_exits_1(self):
        """The usage says a missing key exits with status 1."""
        self.assertRegex(self.usage(), r"(?is)(exit|status)[^.]*\b1\b")


if __name__ == "__main__":
    sys.exit(main(Docs))
