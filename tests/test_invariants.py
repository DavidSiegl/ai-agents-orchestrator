"""Rules the docs state and no example test would notice breaking, one test each.

Each test's failure message names the document that states its rule.
"""

import ast
import re
import sys
import time
import tomllib
import unittest

import conftest
from guard_errors import RealSleep

ROOT = conftest.REPO
GATE_SOURCE = ROOT / "gate.py"


def imported_modules(tree: ast.AST) -> set[str]:
    """The top-level name of every module an import statement anywhere in tree names; relative imports left out."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return names


def authorization_uses(tree: ast.AST) -> list[tuple[str, str]]:
    """(qualified name of the enclosing function, how it is used) for each place in tree that could set an
    Authorization header: the string "Authorization", in any case, as the argument of a call (by the called
    method's name, such as "add_unredirected_header" or "get") or anywhere else ("literal", which a constant
    holding the name or a dict key is); a keyword argument of that name ("keyword"); and any name of urllib's
    authentication handlers and password managers ("auth handler")."""
    uses = []

    def visit(node, scope, call):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            scope = f"{scope}.{node.name}" if scope else node.name
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower() == "authorization":
            uses.append((scope, call or "literal"))
        elif isinstance(node, ast.keyword) and (node.arg or "").lower() == "authorization":
            uses.append((scope, "keyword"))
        elif re.search(r"AuthHandler|PasswordMgr", getattr(node, "id", "") or getattr(node, "attr", "")):
            uses.append((scope, "auth handler"))
        for child in ast.iter_child_nodes(node):
            # A call is the use only of its own arguments; what is nested deeper in them is judged on its own.
            arg_of = None
            if isinstance(node, ast.Call) and child in node.args:
                arg_of = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            visit(child, scope, arg_of)

    visit(tree, "", None)
    return uses


class TestInvariants(unittest.TestCase):
    def test_the_gate_imports_only_the_standard_library(self):
        others = imported_modules(ast.parse(GATE_SOURCE.read_text())) - set(sys.stdlib_module_names)
        self.assertEqual(others, set(), "gate.py is stdlib-only, so that the one file is the gate, for another project "
                                        "to run (docs/quality-gate.md; docs/ui-options.md, C1)")

    def test_the_project_has_no_runtime_dependencies(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            project = tomllib.load(f)["project"]
        self.assertEqual(project["dependencies"], [], "gate.py has no runtime dependencies "
                                                      "(pyproject.toml, [project]; docs/ui-options.md, C1)")

    def test_the_engine_s_version_is_the_release_s(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            release = tomllib.load(f)["project"]["version"]
        with open(ROOT / "engine" / "Cargo.toml", "rb") as f:
            engine = tomllib.load(f)["package"]["version"]
        self.assertEqual(engine, release, "the engine's version is the release's, which the Jenkinsfile's Release "
                                          "stage reads from pyproject.toml (docs/architecture.md, Tooling)")

    def test_coverage_records_with_the_c_tracer(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            run = tomllib.load(f)["tool"]["coverage"]["run"]
        self.assertEqual(run.get("core"), "ctrace", "under another core, `--cov-context=test` can record each line for "
                                                    "its first tests alone, and gate.py --test-contexts then has mutmut "
                                                    "leave out tests (pyproject.toml, [tool.coverage.run])")

    def test_the_gate_sets_no_authorization_header(self):
        rule = ("gate.py sends no credentials; only the engine talks to Jenkins, its token in the Authorization header "
                "alone (docs/quality-gate.md, Credentials and permissions)")
        self.assertEqual(authorization_uses(ast.parse(GATE_SOURCE.read_text())), [], rule)

    def test_no_test_sleeps_for_real(self):
        with self.assertRaises(RealSleep, msg="a test that must wait on a real process is marked real_sleep "
                                              "(tests/conftest.py)"):
            time.sleep(0)
