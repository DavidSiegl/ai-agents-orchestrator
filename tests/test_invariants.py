"""Rules the docs state and no example test would notice breaking, one test each.

Each test's failure message names the document that states its rule.
"""

import ast
import io
import json
import pathlib
import sys
import time
import tomllib
import unittest

import conftest

import orchestrator
from orchestrator import CI

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "orchestrator.py"

TOKEN = "tok-3f9a1c"
JENKINS = "http://jenkins.example:8080"


def imported_modules(tree: ast.AST) -> set[str]:
    """The top-level name of every module an import statement anywhere in tree names; relative imports left out."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return names


def authorization_setters(tree: ast.AST) -> list[tuple[str, str]]:
    """(qualified name of the enclosing function, call or "dict") for each place in tree that sets a request header
    named Authorization, in any case: an add_header, add_unredirected_header or putheader call naming it first, or
    a dict literal with it as a key, such as Request's headers."""
    setters = []

    def visit(node, scope):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            scope = f"{scope}.{node.name}" if scope else node.name
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("add_header", "add_unredirected_header", "putheader")
                and node.args and _is_authorization(node.args[0])):
            setters.append((scope, node.func.attr))
        if isinstance(node, ast.Dict) and any(k is not None and _is_authorization(k) for k in node.keys):
            setters.append((scope, "dict"))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, "")
    return setters


def _is_authorization(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower() == "authorization"


class AnyJenkins:
    """urlopen for a Jenkins that gives every request one answer, enough for each of CI's methods to finish, and
    records each Request in `requests`."""

    BODY = json.dumps({
        "property": [{"parameterDefinitions": [{"name": p} for p in orchestrator.JOB_PARAMETERS]}],
        "builds": [], "building": False, "result": "SUCCESS", "suites": [],
    }).encode()

    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return Answer(self.BODY, {"Location": f"{JENKINS}/queue/item/7/"})


class Answer(io.BytesIO):
    """A urlopen response: a body, and headers as a dict."""

    def __init__(self, body: bytes, headers: dict):
        super().__init__(body)
        self.headers = headers


# One call of each public member of CI. A member added to CI without a line here fails the credentials test, so
# that its requests are checked too.
CI_CALLS = {
    "job_url": lambda ci: ci.job_url,
    "mask": lambda ci: ci.mask(f"a {TOKEN}"),
    "check_credentials": lambda ci: ci.check_credentials(),
    "preflight": lambda ci: ci.preflight(),
    "trigger": lambda ci: ci.trigger("orchestrator-ci/a1b2c3-1-q1"),
    "queue_item": lambda ci: ci.queue_item(f"{JENKINS}/queue/item/7/"),
    "find_build": lambda ci: ci.find_build("orchestrator-ci/a1b2c3-1-q1"),
    "retained_builds": lambda ci: ci.retained_builds(),
    "build_result": lambda ci: ci.build_result(41),
    "quality_file": lambda ci: ci.quality_file(41),
    "failed_tests": lambda ci: ci.failed_tests(41),
    "console_tail": lambda ci: ci.console_tail(41),
}


class TestInvariants(unittest.TestCase):
    def test_the_orchestrator_imports_only_the_standard_library(self):
        others = imported_modules(ast.parse(SOURCE.read_text())) - set(sys.stdlib_module_names)
        self.assertEqual(others, set(), "orchestrator.py is stdlib-only, so that it runs on a system python3 without "
                                        "uv (pyproject.toml, [project]; docs/ui-options.md, C1)")

    def test_the_project_has_no_runtime_dependencies(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            project = tomllib.load(f)["project"]
        self.assertEqual(project["dependencies"], [], "the orchestrator has no runtime dependencies "
                                                      "(pyproject.toml, [project]; docs/ui-options.md, C1)")

    def test_credentials_travel_only_in_an_unredirected_header(self):
        rule = ("the token travels only in the Authorization header, never in a URL, and only to the server asked "
                "(docs/quality-gate.md, Credentials and permissions)")
        self.assertEqual(authorization_setters(ast.parse(SOURCE.read_text())),
                         [("CI._request", "add_unredirected_header")], rule)
        self.assertEqual(set(CI_CALLS), {name for name in vars(CI) if not name.startswith("_")},
                         "CI_CALLS calls each public member of CI once")
        jenkins = AnyJenkins()
        ci = CI("Folder/quality", env={"JENKINS_URL": JENKINS, "JENKINS_USER": "agents", "JENKINS_TOKEN": TOKEN},
                urlopen=jenkins)
        for name, call in CI_CALLS.items():
            with self.subTest(member=name):
                call(ci)
        self.assertGreaterEqual(len(jenkins.requests), len(CI_CALLS) - 3)  # job_url, mask, check_credentials ask none
        for req in jenkins.requests:
            with self.subTest(url=req.full_url):
                self.assertNotIn(TOKEN, req.full_url, rule)
                self.assertNotIn(TOKEN.encode(), req.data or b"", rule)
                self.assertIn("Authorization", req.unredirected_hdrs, rule)
                self.assertNotIn("Authorization", req.headers, rule)

    def test_no_test_sleeps_for_real(self):
        with self.assertRaises(conftest.RealSleep, msg="tests run on FakeClock, time that passes only when the "
                                                       "workflow sleeps (docs/architecture.md, Tests)"):
            time.sleep(0)
