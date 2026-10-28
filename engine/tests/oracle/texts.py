"""Write engine/tests/oracle/texts.json: orchestrator.py's constants and built-in prompts, by name, which the Rust
engine's tests hold its own to byte for byte. Run `uv run python engine/tests/oracle/texts.py` to rewrite it."""

import dataclasses
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
import orchestrator  # found through the path above

OUT = os.path.join(os.path.dirname(__file__), "texts.json")

# The constants of orchestrator.py's top section (docs/architecture.md, "Constants"), which engine/src/constants.rs
# ports, and the prompts' own.
CONSTANTS = ["RUNS_DIR", "WORKTREES_DIR", "PROOF_COPIES_DIR", "DEFAULT_TURN_TIMEOUT", "DEFAULT_MAX_ROUNDS",
             "AGENT_START_TIMEOUT_MS", "POLL_SECONDS", "STALL_SECONDS", "PROMPT_DELIVERY_SECONDS",
             "HEARTBEAT_SECONDS", "STALE_SECONDS", "NETWORK_TIMEOUT", "GITLEAKS_TIMEOUT", "GITLEAKS_INSTALL",
             "GITLEAKS_ALLOW", "GITLEAKS_EXTEND_DEPTH", "BRANCH_PREFIX", "REMOTE", "CLOSED_BRANCH_GRACE",
             "PR_SECTION_LIMIT", "APPROVE", "CHANGES_REQUESTED", "FINISHED", "QUALITY_GATE_FAILED", "SUCCEEDED",
             "BLOCKED", "NO_QUESTION", "DEFAULT_MAX_QUALITY_ROUNDS", "DEFAULT_MAX_FILES", "DEFAULT_MAX_LINES",
             "BUDGET_KEYS", "HTTP_TIMEOUT", "PYPI_TIMEOUT", "CI_POLL_SECONDS", "QUALITY_TIMEOUT", "CI_ENV",
             "CI_TOKENS", "GIT_PATHSPEC_ENV", "JOB_PARAMETERS", "CI_REF_PREFIX", "LOCAL_REFS_PREFIX",
             "CONSOLE_TAIL_LINES", "ROLE_LABELS", "AGENT_KINDS", "DEFAULT_AGENT", "EXIT_ERROR",
             "EXIT_CHANGES_REQUESTED", "EXIT_CONFLICT", "EXIT_WAITING", "EXIT_INTERRUPTED"]


def build() -> dict:
    """The oracle: the constants and the built-in prompts, by name."""
    return {
        "constants": {name: getattr(orchestrator, name) for name in CONSTANTS},
        "prompts": {f.name: getattr(orchestrator.BUILT_IN_PROMPTS, f.name)
                    for f in dataclasses.fields(orchestrator.Prompts)},
    }


def text(oracle: dict) -> str:
    """The file's content for the oracle build() gives."""
    return json.dumps(oracle, indent=1) + "\n"


def main() -> None:
    """Write the oracle to OUT."""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text(build()))


if __name__ == "__main__":
    main()
