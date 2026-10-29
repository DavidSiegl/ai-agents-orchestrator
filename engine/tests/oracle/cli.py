"""Write engine/tests/oracle/cli.json: the commands orchestrator.py's parse_args has, and the flags each command's
--help names, which the Rust engine's help must name too (rust-engine ticket 09's Verification compares the sets,
not the layout). Run `uv run python engine/tests/oracle/cli.py` to rewrite it."""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
import orchestrator  # found through the path above

OUT = os.path.join(os.path.dirname(__file__), "cli.json")

# The commands the Rust engine does not port: the tk GUI (the rust-engine spec's Decisions).
NOT_PORTED = ("gui",)

# A flag as help names it, in its usage or its text: a long flag, or -h.
FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z-]*[a-z]|-h)(?![\w-])")


def help_text(argv: list[str]) -> str:
    """What parse_args prints for argv, a --help, with no workflow files configured and no line wrapped, which could
    split a flag at its hyphen."""
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as config, \
            patch.dict("os.environ", {"XDG_CONFIG_HOME": config, "COLUMNS": "100000"}), \
            contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
        orchestrator.parse_args(argv)
    return out.getvalue()


def build() -> dict:
    """The oracle: the commands, in the order the top-level usage lists them, and by command but NOT_PORTED's (and
    "" for the program itself), the sorted flags its --help names."""
    top = help_text(["--help"])
    commands = re.search(r"\{([a-z,-]+)\}", top).group(1).split(",")
    flags = {"": sorted(set(FLAG.findall(top)))}
    for command in commands:
        if command not in NOT_PORTED:
            flags[command] = sorted(set(FLAG.findall(help_text([command, "--help"]))))
    return {"commands": commands, "flags": flags}


def text(oracle: dict) -> str:
    """The file's content for the oracle build() gives."""
    return json.dumps(oracle, indent=1) + "\n"


def main() -> None:
    """Write the oracle to OUT."""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text(build()))


if __name__ == "__main__":
    main()
