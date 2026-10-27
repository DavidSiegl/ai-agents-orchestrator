"""Settings and guards every test file shares: Hypothesis's profiles, and no network.

The guard: no test may open a network connection. The orchestrator's network clients take an injected `urlopen`
(`CI`, `PyPI`), so a test that reaches for the real network is a bug, and fails with `NetworkUsed`, naming the
address. Only this process's sockets are guarded; a child process the test starts is not.
"""

import ipaddress
import os
import socket

import pytest
from hypothesis import settings

# How many examples each property test tries. `default` keeps a local `uv run pytest` fast; the Jenkinsfile's Test
# stage sets HYPOTHESIS_PROFILE=ci for ten times as many. No deadline: the tests that run git or sh for each example
# take tens of milliseconds an example, and their speed is not what they test.
settings.register_profile("default", max_examples=25, deadline=None)
settings.register_profile("ci", max_examples=250, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

# The real calls, taken before any test can replace them, which a guarded call hands on to when it is allowed.
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_create_connection = socket.create_connection


def pytest_configure(config):
    """Register the marker that lets a test past the network guard."""
    config.addinivalue_line(
        "markers",
        "loopback: the test talks to a server it started itself on a loopback address, such as a RunServer or "
        "`orchestrator.py serve`, whose HTTP over real sockets is what it tests. Connections to any other address "
        "still fail. Loopback is not allowed to every test: the Jenkins and SonarQube a developer runs on this "
        "machine listen there too.")


class NetworkUsed(RuntimeError):
    """A test opened a network connection the guard does not allow.

    Not an OSError, so that code under test which takes an OSError for an unreachable server, such as
    `CI._request`, cannot turn it into an ordinary failure and let the test pass.
    """


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
