"""Golden files: generated text held to a reviewed copy of it under tests/golden/, so that a change to its wording
fails a test and is reviewed as the diff of that copy.

`UPDATE_GOLDEN=1 uv run pytest` rewrites the copies a run compares with what the code now writes; read their diff
before committing it. docs/architecture.md (Tests) says what has golden files and what the parity harness pins
instead.
"""

import difflib
import os
import re

# Where the golden files are, one <name>.txt per name given to assert_golden.
GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")


def assert_golden(name: str, text: str) -> None:
    """Pass when text equals the golden file tests/golden/<name>.txt, byte for byte as UTF-8, line ends included.

    Otherwise raise AssertionError naming the file, with a unified diff from the file to text, or saying the file is
    missing. With UPDATE_GOLDEN=1 in the environment, write text to the file instead, creating it if need be, and
    pass. name is a file name without its extension; text is any str.
    """
    path = os.path.join(GOLDEN_DIR, f"{name}.txt")
    if os.environ.get("UPDATE_GOLDEN") == "1":
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return
    try:
        with open(path, encoding="utf-8", newline="") as f:
            want = f.read()
    except FileNotFoundError:
        raise AssertionError(f"no golden file {path}; write it with UPDATE_GOLDEN=1 and review it") from None
    if text == want:
        return
    # A last line without its line end gets diff's marker, so that it does not run into the next line of the diff.
    diff = "".join(line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n"
                   for line in difflib.unified_diff(_lines(want), _lines(text),
                                                    fromfile=path, tofile=f"{path} (as written now)"))
    raise AssertionError(f"{path} differs from the text written now; if the change is meant, rewrite it with "
                         f"UPDATE_GOLDEN=1 and review its diff:\n{diff}")


def _lines(text: str) -> list[str]:
    """text's lines, each with its "\\n", the last without one if text does not end in one. Only "\\n" ends a line,
    as in the file on disk; str.splitlines would split at "\\r" or U+2028 too, which a quality file can hold."""
    return re.findall(r"[^\n]*\n|[^\n]+\Z", text)
