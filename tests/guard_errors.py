"""The errors the guards in conftest.py raise, for a test to catch.

They live here, not in conftest.py, so that a test module sees the class the guard raises however many times pytest
runs in one process, as mutmut runs it: pytest imports conftest.py afresh for each run
(`PytestPluginManager._importconftest` drops the old one from sys.modules), while it imports this module, like a test
module, once.
"""


class NetworkUsed(RuntimeError):
    """A test opened a network connection the guard does not allow.

    Not an OSError, so that code under test which takes an OSError for an unreachable server, such as
    `CI._request`, cannot turn it into an ordinary failure and let the test pass.
    """


class RealSleep(RuntimeError):
    """A test, or the orchestrator code it runs, called time.sleep without the real_sleep marker."""
