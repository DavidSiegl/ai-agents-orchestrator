"""The hidden acceptance test of the eval task `no-dependency`: render_table and `--format table`, with no
dependency added (task.md). evals/acceptance.py says how it is run."""

import ast
import pathlib
import sys
import tempfile
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # evals/, for acceptance.py
from acceptance import Acceptance, main

HEADER = ["name", "qty", "price"]
ROWS = [["apple", "3", "1.50"], ["banana", "12", "0.25"]]
TABLE = "name   | qty | price\n-------+-----+------\napple  |   3 |  1.50\nbanana |  12 |  0.25\n"
# Files that list a project's dependencies, beside pyproject.toml. Not a lock file, which a tool such as uv writes
# of pyproject.toml's dependencies, none or not.
DEPENDENCY_LISTS = ("setup.py", "setup.cfg", "Pipfile", "environment.yml")


def imported(path: pathlib.Path) -> set[str]:
    """The top-level names of the modules the Python file at path imports, relative imports left out."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add(node.module or "")
    return {name.partition(".")[0] for name in names}


class NoDependency(Acceptance):
    """render_table and --format table work, and the project depends on nothing."""

    def check_render_table(self):
        """The task's example."""
        from report import render_table
        self.assertEqual(render_table(HEADER, ROWS), TABLE)

    def check_alignment_rules(self):
        """Numbers right, the rest left; widths from headers; no trailing space; no rows."""
        from report import render_table
        self.assertEqual(render_table(["id", "a long header"], [["1", "x"], ["n/a", ""]]),
                         "id  | a long header\n----+--------------\n1   | x\nn/a |\n")
        self.assertEqual(render_table(["n"], [["1e3"], ["-2.5"]]), "   n\n----\n 1e3\n-2.5\n")
        self.assertEqual(render_table(["only", "header"], []), "only | header\n-----+-------\n")

    def check_the_command_line(self):
        """--format table prints the table; the default is CSV, as before."""
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d, "data.csv")
            path.write_text("name,qty,price\napple,3,1.50\nbanana,12,0.25\n")
            table = self.python("-m", "report", "--format", "table", str(path))
            csv = self.python("-m", "report", str(path))
        self.assertEqual((table.returncode, table.stdout), (0, TABLE), table.stderr)
        self.assertEqual((csv.returncode, csv.stdout), (0, "name,qty,price\napple,3,1.50\nbanana,12,0.25\n"))

    def check_no_dependency(self):
        """pyproject.toml lists none, no other file lists any, and the code imports only the standard library."""
        project = tomllib.loads(pathlib.Path(self.repo, "pyproject.toml").read_text())
        self.assertEqual(project["project"].get("dependencies"), [])
        self.assertNotIn("optional-dependencies", project["project"])
        self.assertNotIn("dependency-groups", project)
        files = self.project_files()
        lists = [f for f in files if "/" not in f and (f.startswith("requirements") or f in DEPENDENCY_LISTS)]
        self.assertEqual(lists, [])
        for path in (f for f in files if f.endswith(".py")):
            with self.subTest(path=path):
                imports = imported(pathlib.Path(self.repo, path))
                self.assertLessEqual(imports - set(sys.stdlib_module_names), {"report", "tests"})

    def project_files(self) -> list[str]:
        """The project's files, committed or not, as paths relative to it; not what git ignores, such as a virtual
        environment uv made or the run's own directory."""
        proc = self.in_repo("git", "ls-files", "--cached", "--others", "--exclude-standard", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.splitlines()


if __name__ == "__main__":
    sys.exit(main(NoDependency))
