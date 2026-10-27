"""Contract tests: the orchestrator's CI class, with the real urllib, against a live Jenkins quality job.

The other tests replace Jenkins by FakeCI and FakeResponse (tests/test_orchestrator.py). This one runs the chain of
calls a quality round makes against the real job and checks that every answer the orchestrator reads has the shape
those fakes give it. A field the live answer shapes differently is a drift: fix the fake to match Jenkins.

It is marked contract, which a normal run deselects (tests/conftest.py), and needs four variables, named apart from
the orchestrator's own (CI_ENV) so that a developer's ci.env never turns it on; a missing one fails it, rather than
skips it:

    CONTRACT_JENKINS_URL=http://cicdbox0.lan:8084 CONTRACT_JENKINS_USER=agents CONTRACT_JENKINS_TOKEN=<API token> \\
        CONTRACT_JOB=Octoflow-AI/py-octoflow-ai-quality uv run pytest -m contract tests/contract

Each run triggers one real build of the job with GIT_REF=main, waiting for it up to QUALITY_TIMEOUT, as a quality
round does. It pushes nothing, and a build with GIT_REF set never reaches the Release stage (Jenkinsfile).
"""

import json
import os
import re
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pytest

import orchestrator

# The variable each of the orchestrator's CI_ENV takes its value from here.
CONTRACT_ENV = {"JENKINS_URL": "CONTRACT_JENKINS_URL", "JENKINS_USER": "CONTRACT_JENKINS_USER",
                "JENKINS_TOKEN": "CONTRACT_JENKINS_TOKEN"}
# The job's full name, folders included, as --quality-job takes it.
CONTRACT_JOB = "CONTRACT_JOB"
# A branch every quality build can check out, whose build pushes nothing.
REF = "main"
# Jenkins's build results (hudson.model.Result).
RESULTS = ("SUCCESS", "UNSTABLE", "FAILURE", "NOT_BUILT", "ABORTED")
# A test case's status in Jenkins's test report (hudson.tasks.junit.CaseResult.Status).
CASE_STATUSES = ("PASSED", "SKIPPED", "FAILED", "FIXED", "REGRESSION")


@dataclass(frozen=True)
class Answer:
    """One answer Jenkins gave: an HTTP error's too, whose body is then b""."""
    method: str  # the request's
    path: str  # the URL's path, without its query
    http_status: int
    body: bytes


class Replayed:
    """A response whose body was already read, which reads it again, for CI to take as urlopen's."""

    def __init__(self, body: bytes, headers):
        self.body = body
        self.headers = headers

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingUrlopen:
    """urllib.request.urlopen, which keeps each answer it gets in answers, in order."""

    def __init__(self):
        self.answers: list[Answer] = []

    def __call__(self, req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the URL is under CONTRACT_JENKINS_URL
                body, headers, http_status = resp.read(), resp.headers, resp.status
        except urllib.error.HTTPError as e:
            self.answers.append(Answer(req.get_method(), path, e.code, b""))
            raise
        self.answers.append(Answer(req.get_method(), path, http_status, body))
        return Replayed(body, headers)

    def json(self, path: str) -> list[dict]:
        """The JSON bodies of the 2xx answers to GETs of a URL path that path, a regular expression, matches at its
        end, in order."""
        return [json.loads(a.body) for a in self.answers
                if a.method == "GET" and re.search(f"{path}$", a.path) and 200 <= a.http_status < 300]


def contract_env() -> tuple[str, dict[str, str]]:
    """The job and the credentials from the CONTRACT_* variables; fails the test naming each one unset."""
    names = [*CONTRACT_ENV.values(), CONTRACT_JOB]
    if missing := [n for n in names if not os.environ.get(n)]:
        pytest.fail(f"the contract tests need {', '.join(missing)} set (tests/contract/test_jenkins.py)")
    return os.environ[CONTRACT_JOB], {k: os.environ[v] for k, v in CONTRACT_ENV.items()}


@pytest.mark.contract
@pytest.mark.real_sleep
class TestJenkinsContract(unittest.TestCase):

    def setUp(self):
        self.assertEqual(set(CONTRACT_ENV), set(orchestrator.CI_ENV),
                         "CONTRACT_ENV must give every variable of the orchestrator's CI_ENV")
        job, env = contract_env()
        self.jenkins_url = env["JENKINS_URL"].rstrip("/")
        self.urlopen = RecordingUrlopen()
        self.ci = orchestrator.CI(job, env=env, urlopen=self.urlopen)
        self.deadline = time.monotonic() + orchestrator.QUALITY_TIMEOUT

    def poll(self, step: str, call):
        """call() every CI_POLL_SECONDS until it returns something other than None, and that; fails the test, naming
        step, once QUALITY_TIMEOUT has passed since the test began."""
        while (out := call()) is None:
            if time.monotonic() >= self.deadline:
                self.fail(f"{step}: no answer within {orchestrator.QUALITY_TIMEOUT}s")
            time.sleep(orchestrator.CI_POLL_SECONDS)
        return out

    def test_one_quality_build_of_main(self):
        # Preflight: the job lists JOB_PARAMETERS, or this raises.
        self.ci.preflight()

        # Trigger: the queue item, from the Location header (FakeCI's "trigger").
        queue_url = self.ci.trigger(REF)
        self.assertRegex(queue_url, rf"^{re.escape(self.jenkins_url)}/queue/item/\d+/$")

        # The queue item until it names its build: executable.number once the build started, and cancelled, false
        # unless it never will (FakeCI's "queue").
        number = self.poll("waiting for the build to start",
                           lambda: (self.ci.queue_item(queue_url).get("executable") or {}).get("number"))
        self.assertIsInstance(number, int)
        items = self.urlopen.json(r"/queue/item/\d+/api/json")
        for item in items:
            self.assertIsInstance(item.get("cancelled", False), bool, item)
            self.assertIsInstance(item.get("executable") or {}, dict, item)
        self.assertFalse(items[-1].get("cancelled"), items[-1])

        # find_build: the job's last builds with their parameters, newest first, ours among them (FakeCI's "find").
        self.assertEqual(self.ci.find_build(REF), number)

        # The build until it ends: building, a boolean, and result, null while it runs (FakeCI's "build").
        result = self.poll(f"waiting for build #{number} to end", lambda: self.ci.build_result(number))
        self.assertIn(result, RESULTS)
        for build in self.urlopen.json(rf"/job/[^/]+/{number}/api/json"):
            self.assertIsInstance(build.get("building"), bool, build)
            self.assertIn(build.get("result"), (None, *RESULTS), build)
        # A build whose Gate stage ran: its tests ran before it, and the result says it judged.
        judged = result in ("SUCCESS", "UNSTABLE")

        # The quality file the Gate stage archived, its first line the GATE line (FakeCI's "quality").
        quality = self.ci.quality_file(number)
        if judged:
            self.assertIsInstance(quality, str)
            self.assertRegex(quality.split("\n", 1)[0], r"^GATE: \w+$")
        else:
            self.assertIsInstance(quality, str | None)

        # The test report: suites of cases, each with className, name, status and errorDetails (FakeCI's "tests").
        failed = self.ci.failed_tests(number)
        if judged:
            self.assertIsInstance(failed, list)
        for test, error in failed or []:
            self.assertIsInstance(test, str)
            self.assertIsInstance(error, str)
        for report in self.urlopen.json(rf"/job/[^/]+/{number}/testReport/api/json"):
            cases = [c for suite in report["suites"] for c in suite["cases"]]
            self.assertTrue(cases, "the test report has no cases")
            for case in cases:
                self.assertIsInstance(case.get("className"), str, case)
                self.assertIsInstance(case.get("name"), str, case)
                self.assertIn(case.get("status"), CASE_STATUSES, case)
                self.assertIsInstance(case.get("errorDetails"), str | None, case)

        # The job's retained builds, which judged_in_quality reads: ours, with its result (FakeCI's "builds").
        retained = {b.number: b for b in self.ci.retained_builds()}
        self.assertIn(number, retained)
        self.assertEqual(retained[number].result, result)

        # The console's end, which a BUILD_FAILED quality file quotes: its last line, Jenkins's own, after the
        # Timestamper plugin's time (FakeCI's "console").
        self.assertRegex(self.ci.console_tail(number).split("\n")[-1], rf"^(\[[^]]+\] )?Finished: {result}$")
