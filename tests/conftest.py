"""Settings every test file shares: Hypothesis's profiles."""

import os

from hypothesis import settings

# How many examples each property test tries. `default` keeps a local `uv run pytest` fast; the Jenkinsfile's Test
# stage sets HYPOTHESIS_PROFILE=ci for ten times as many. No deadline: the tests that run git or sh for each example
# take tens of milliseconds an example, and their speed is not what they test.
settings.register_profile("default", max_examples=25, deadline=None)
settings.register_profile("ci", max_examples=250, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
