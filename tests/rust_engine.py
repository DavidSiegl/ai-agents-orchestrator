"""The Rust engine as the tests run it: this checkout's orchestrator binary, built with cargo."""

import functools
import json
import shutil
import subprocess

import conftest

# The repository's top directory, where the Cargo workspace is: under mutmut too, whose copy of the tree holds no
# engine/ (conftest.REPO).
ROOT = conftest.REPO


@functools.cache
def binary() -> str:
    """The path of this checkout's orchestrator binary, `cargo build --locked -p octoflow-engine --bin orchestrator`
    run once per test run, wherever CARGO_TARGET_DIR puts it. Raises AssertionError when cargo is missing or the build
    fails, so that a machine without cargo fails the tests that run the engine rather than skipping them."""
    cargo = shutil.which("cargo")
    if cargo is None:
        raise AssertionError("cargo, which builds the engine, is not on PATH")
    built = subprocess.run([cargo, "build", "--locked", "--quiet", "-p", "octoflow-engine", "--bin", "orchestrator",  # noqa: S603
                            "--message-format=json"], cwd=ROOT, capture_output=True, text=True, check=False)
    if built.returncode != 0:
        raise AssertionError(f"cargo could not build the engine: {built.stderr}")
    [path] = [m["executable"] for m in map(json.loads, built.stdout.splitlines())
              if m.get("reason") == "compiler-artifact" and m.get("executable")]
    return path
