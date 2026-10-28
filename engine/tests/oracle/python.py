"""Write engine/tests/oracle/python.json: what Python 3.14, the engine's Python, gives for the str methods, format
strings, difflib matches, shlex.join and json.dumps that engine/src/py ports, for cases the Rust tests replay against
the port.

Run `uv run python engine/tests/oracle/python.py` after changing a case; tests/test_rust_oracle.py fails while the
file is not what this writes."""

import difflib
import json
import os
import shlex
import string

OUT = os.path.join(os.path.dirname(__file__), "python.json")

LINES = ["", "a", "a\n", "a\nb", "a\r\nb\r\n", "a\rb", "\n\n", "a\x0bb\x0cc\x1cd\x1de\x1ef\x85g\u2028h\u2029i",
         "a\r\r\nb", "\r", "x\n\ny", "a\x1fb"]
BLANKS = ["  a  ", "\t\na\r\n", "\x1c\x1d\x1e\x1fa\x1f", "\xa0a\u3000", "\u200ba\u200b", "\x85a\u2028", "", "   "]
PATHS = ["a.md", "build-{n}.md", ".bashrc", "..x", "a..b", "dir.d/file", "dir/.hidden.toml", "a.", "noext",
         "/x/y.tar.gz", "...", "a/b.c/d"]
STRINGS = ["", "plain", "it's", 'say "hi"', "both ' and \"", "back\\slash", "tab\tnl\ncr\r", "\x00\x1f\x7f",
           "\x80\x9f\xa0\xad", "é ü ß", "\u200b\u2028\ufeff", "😀", "\U000e0001", "\ud7ff\ue000"]
KEY_SETS = {
    "workflow": ["description", "roles", "steps", "proof_command", "max_files", "max_lines"],
    "role": ["label", "model", "agent"],
    "step": ["id", "role", "file", "prompt", "again", "fresh_note", "fresh_repeats_again", "human_paced", "edits",
             "loop_to", "quality_gated", "locks", "use"],
}
WORDS = ["step", "rolse", "prompts", "proof_comand", "lable", "modle", "agnet", "edit", "loopto", "zzz", "idd",
         "fresh", "quality-gated", "locked", "max_line", "", "x"]
TEMPLATES = ["{a!x}", "{a!}", "{a!r", "{a!rx}", "{a!r}", "{a!a}", "{a!s:>6}", "{a:d}", "{a:+}", "{a: }", "{a:z}",
             "{a:#}", "{a:=5}", "{a:05}", "{a:,}", "{a:_}", "{a:,_}", "{a:_,}", "{a:_x}", "{a:,x}", "{a:,\x01}",
             "{a:.}", "{a:ss}", "{a:.2s}", "{a:x^7}", "{a:^8}", "{a:>8}", "{a:<8}", "{a:*<2}", "{a:{b}}",
             "{a:{b:{c}}}", "{}", "{0}", "{a.b}", "{a[0]}", "{a[9]}", "{a[x]}", "{a[}", "{a[0]x}", "{a.}", "{a[]}",
             "{", "}", "a}b", "{a", "{a:", "{a:{", "{{}}", "{a:9999999999999999999999}", "{a:.5,}", "{a:.5_}",
             "{a:>.1}", "{a: >5}", "{\u0661}", "{a:\u0661\u0662}", "{{a}}", "{a:{}}", "{d}", "{a}{}", "{}{0}",
             "{0}{}", "{a:\x01}", "{a:é}", "{a:\x7f}", "{a:,\x7f}", "{a! }", "{a!é}", "{a!\x01}", "{a!\x7f}", "{a!~}",
             "{q} }", "{a}{q}{", "{q:{}}", "{a{b}}", "plain", "", "x{a}y{b}z", "{a[0][0]}", "{a[0].x}",
             "{a:{b}{c}}", "{a:}", "{a!}}", "{a:}}}", "{a}}", "{e!r}", "{e!a}", "{a:0>4}", "{a:00}", "{a:<05}"]
SHELL_WORDS = ["a@b%c+d=e:f,g.h/i-j_k", "~", "*", "!", "-", "\u0661", "a b"]
ARGV = ["sh", "-c", 'cd -- "$1" && pwd', "_", "/my proj", "it's"]
VALUES = {"a": "hey", "b": "3", "c": "x", "e": "it's é\u200b\n"}
JSON_VALUES = [None, True, False, 0, -7, 2**63, -2**63, 1.0, -0.0, 0.5, 1e16, 1e15, 1.5e-5, 0.0001, 123.456, 1e300,
               2.5e-300, 1 / 3, "", "a\"b\\c", "\n\r\t\b\f\x00\x1f\x7f", "é😀\u2028", [], {}, [1, [2, {}]],
               {"z": 1, "a": [True, None], "é": {"k": "v"}}]


def outcome(call):
    """{"ok": what call returns}, or {"error": "Name: message"} of the exception it raises."""
    try:
        return {"ok": call()}
    except Exception as e:  # noqa: BLE001 - the oracle records any exception, by its name and message
        return {"error": f"{type(e).__name__}: {e}"}


def chunks(template):
    """string.Formatter().parse(template) as lists."""
    return [[literal, name, spec, conversion] for literal, name, spec, conversion in string.Formatter().parse(template)]


def build() -> dict:
    """The oracle: each kind of call with its cases and Python's answers."""
    return {
        "splitlines": [[s, s.splitlines()] for s in LINES],
        "strip": [[s, s.strip(), s.lstrip()] for s in BLANKS],
        "splitext": [[p, list(os.path.splitext(p))] for p in PATHS],
        "repr": [[s, repr(s), ascii(s)] for s in STRINGS],
        "close_match": [[word, keys, next(iter(difflib.get_close_matches(word, keys, n=1)), None)]
                        for keys in KEY_SETS.values() for word in WORDS],
        "format": [[t, outcome(lambda t=t: t.format(**VALUES))] for t in TEMPLATES],
        "parse": [[t, outcome(lambda t=t: chunks(t))] for t in TEMPLATES],
        "shlex_join": [[[s], shlex.join([s])] for s in [*STRINGS, *WORDS, *PATHS, *SHELL_WORDS]]
        + [[ARGV, shlex.join(ARGV)]],
        "json": [[json.dumps(v, ensure_ascii=False), json.dumps(v)] for v in JSON_VALUES],
        "values": VALUES,
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
