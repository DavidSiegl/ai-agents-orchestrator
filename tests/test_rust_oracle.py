"""The Python engine's answers that the Rust engine's tests replay (engine/tests/oracle/): each file there is what
its generator, the .py of the same name, writes now, so that a Rust test never checks against a stale oracle."""

import glob
import importlib.util
import os
import unittest

ORACLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine", "tests", "oracle")


def generator(path: str):
    """The generator module at path, imported under its own name."""
    spec = importlib.util.spec_from_file_location(os.path.splitext(os.path.basename(path))[0], path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestRustOracle(unittest.TestCase):
    def test_each_oracle_file_is_what_its_generator_writes(self):
        scripts = sorted(glob.glob(os.path.join(ORACLE_DIR, "*.py")))
        self.assertTrue(scripts, f"no generator in {ORACLE_DIR}")
        for script in scripts:
            with self.subTest(script=os.path.basename(script)):
                module = generator(script)
                with open(module.OUT, encoding="utf-8") as f:
                    written = f.read()
                self.assertEqual(written, module.text(module.build()),
                                 f"{module.OUT} is stale; run: uv run python {os.path.relpath(script)}")
