"""Settings and guards every test file shares: Hypothesis's profiles, the repository's top directory, no network,
and no real sleeping.

The guards, each a fixture every test runs under, and each for this process only, not for a child process a test
starts:

- No test may open a network connection. The orchestrator's network clients take an injected `urlopen` (`CI`,
  `PyPI`), so a test that reaches for the real network is a bug, and fails with `NetworkUsed`, naming the address.
- No test may wait in time.sleep. A Workflow takes its clocks as an argument (`Clocks`), and the tests give it
  `FakeClock`, time that passes only when the workflow sleeps; a real sleep makes the suite slow, and a test that
  depends on how long one takes flaky. The test code and orchestrator.py are guarded, the standard library is not:
  subprocess sleeps while it waits for a child with a timeout, which is how the orchestrator runs its processes.
  The sleep of a `Clocks()` built with its defaults, which a Workflow given no clocks uses, is guarded too.
"""

import ipaddress
import os
import pathlib
import socket
import sys
import time

import pytest
from guard_errors import NetworkUsed, RealSleep
from hypothesis import HealthCheck, settings

import orchestrator

# How many examples each property test tries. `default` keeps a local `uv run pytest` fast; the Jenkinsfile's Test
# stage sets HYPOTHESIS_PROFILE=ci for ten times as many. No deadline: the tests that run git or sh for each example
# take tens of milliseconds an example, and their speed is not what they test.
# differing_executors is off because the gate's mutmut runs the tests more than once in one process, and unittest
# makes a new TestCase for each run, so the second calls each @given method on another self. The check guards against
# state kept on self from one example to the next, and the @given methods of tests/test_properties.py keep none: they
# read self only to assert.
_UNCHECKED = [HealthCheck.differing_executors]
settings.register_profile("default", max_examples=25, deadline=None, suppress_health_check=_UNCHECKED)
settings.register_profile("ci", max_examples=250, deadline=None, suppress_health_check=_UNCHECKED)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

# The real calls, taken before any test can replace them, which a guarded call hands on to when it is allowed.
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_create_connection = socket.create_connection
_sleep = time.sleep

# The top directory of the tree the tests run in, and import orchestrator.py and gate.py from: the repository, or
# under mutmut its copy of it, mutants/ at the repository's top (mutmut's fixed name; gate.py's _MUTMUT_STATE).
_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The repository's top directory, whose files the tests read as text: the docs, and orchestrator.py and gate.py
# unmutated. Under mutmut, which runs the tests in _ROOT = mutants/, that is the directory above, the one mutmut copied
# this file from: mutmut's copy of the code holds every mutant beside each function it mutates, and mutants/ holds only
# what [tool.mutmut]'s source_paths and also_copy (pyproject.toml) and mutmut's own defaults (tests/, pyproject.toml,
# the lock files) name, so not README.md. A checkout that is itself named mutants has no tests/conftest.py above it.
_IN_MUTMUT_COPY = _ROOT.name == "mutants" and (_ROOT.parent / "tests" / "conftest.py").is_file()
REPO = _ROOT.parent if _IN_MUTMUT_COPY else _ROOT

# The code whose time.sleep calls the sleep guard refuses: the tests, and the orchestrator they test.
_GUARDED_TESTS = _ROOT / "tests"
_GUARDED_SOURCE = _ROOT / "orchestrator.py"


def pytest_configure(config):
    """Register the markers that let a test past a guard."""
    config.addinivalue_line(
        "markers",
        "loopback: the test talks to a server it started itself on a loopback address, such as a RunServer or "
        "`orchestrator.py serve`, whose HTTP over real sockets is what it tests. Connections to any other address "
        "still fail. Loopback is not allowed to every test: the Jenkins and SonarQube a developer runs on this "
        "machine listen there too.")
    config.addinivalue_line(
        "markers",
        "real_sleep: the test waits on something outside this process's control, a child process or a server "
        "thread, by polling it with time.sleep: what it tests is how the orchestrator behaves with real processes "
        "and real time, which FakeClock cannot stand in for. Keep such waits short and bounded by a deadline.")


def _is_loopback(address) -> bool:
    """Whether a socket address, a (host, port, ...) tuple, names this machine's loopback interface."""
    host = address[0] if isinstance(address, tuple) else address
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).partition("%")[0]).is_loopback
    except ValueError:  # a host name other than localhost
        return False


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """For the length of each test, make socket.socket.connect, connect_ex and socket.create_connection raise
    NetworkUsed for any address but a Unix socket's or, in a test marked loopback, a loopback address."""
    loopback = request.node.get_closest_marker("loopback") is not None

    def check(family, address):
        if family == socket.AF_UNIX or (loopback and _is_loopback(address)):
            return
        allowed = "only to a loopback address" if loopback else "to no address; see the loopback marker"
        raise NetworkUsed(f"the test connected to {address!r}, and tests connect {allowed} (tests/conftest.py)")

    def connect(sock, address):
        check(sock.family, address)
        return _connect(sock, address)

    def connect_ex(sock, address):
        check(sock.family, address)
        return _connect_ex(sock, address)

    # Checked before the real one resolves the host name, so that no DNS query goes out either.
    def create_connection(address, *args, **kwargs):
        check(socket.AF_INET, address)
        return _create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)


@pytest.fixture(autouse=True)
def no_real_sleep(request, monkeypatch):
    """For the length of each test not marked real_sleep, make time.sleep, and the sleep of a Clocks built with its
    defaults, raise RealSleep when called from a test file or orchestrator.py; called from anywhere else, such as
    the standard library, it sleeps."""
    if request.node.get_closest_marker("real_sleep") is not None:
        return

    def sleep(seconds):
        caller = pathlib.Path(sys._getframe(1).f_code.co_filename).resolve()
        if caller == _GUARDED_SOURCE or caller.is_relative_to(_GUARDED_TESTS):
            raise RealSleep(f"time.sleep({seconds!r}) in {caller.name}: tests run on FakeClock; a test that must wait "
                            f"on a real process is marked real_sleep (tests/conftest.py)")
        _sleep(seconds)

    monkeypatch.setattr(time, "sleep", sleep)
    # Clocks took the real time.sleep as its default when orchestrator.py was imported, out of reach of the patch.
    defaults = orchestrator.Clocks.__init__.__defaults__
    monkeypatch.setattr(orchestrator.Clocks.__init__, "__defaults__",
                        tuple(sleep if default is _sleep else default for default in defaults))
