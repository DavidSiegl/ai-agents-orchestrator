"""Write engine/tests/oracle/harness.json: what orchestrator.py makes of the agent harnesses and their session logs
for the cases the Rust engine's tests replay: the arguments an agent starts with (launch_args) for every harness,
permission mode, model, session and extra directory, each mode's permission_args, the token usage session_usage
reads from sample Claude Code and pi session logs (given as hex, since they hold bytes that are not UTF-8), the
tokens turn_tokens sums for turns over them, and the time datetime.fromisoformat reads from ISO strings, which the
logs' and the turns' times are. Run `uv run python engine/tests/oracle/harness.py` to rewrite it.

A time without an offset is local time to Python and UTC to the Rust engine (TODO.md, "The Rust engine reads some
input no run writes otherwise than the Python engine"), so fromisoformat's answer for one is recorded as UTC's, and
no log or turn here has one."""

import json
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
import orchestrator as o  # found through the path above

OUT = os.path.join(os.path.dirname(__file__), "harness.json")

# Every mode --permission-mode takes, and one it does not; None and "" pass none.
MODES = [None, "", "default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan", "nope"]
MODELS = [None, "", "m"]
SESSIONS = [None, "", "s-1"]
DIRS = [None, "", "/proj/.orchestrator/runs/r"]

# 2026-10-03T04:00:00+00:00, as the Python tests' FakeHost reads its clock.
EPOCH = 1_791_000_000


def at(seconds: float, suffix: str = "Z") -> str:
    """The time EPOCH + seconds as a session log writes it, with suffix for its offset."""
    return datetime.fromtimestamp(EPOCH + seconds, timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", suffix)


def line(**entry) -> str:
    return json.dumps(entry)


def claude(seconds, message_id, usage, kind="assistant", **extra) -> str:
    return line(type=kind, timestamp=at(seconds), message={"id": message_id, "usage": usage, **extra})


def tokens(i, o_, read=None, write=None) -> dict:
    usage = {"input_tokens": i, "output_tokens": o_}
    if read is not None:
        usage["cache_read_input_tokens"] = read
    if write is not None:
        usage["cache_creation_input_tokens"] = write
    return usage


def pi(seconds, usage, role="assistant", suffix="Z") -> str:
    return line(type="message", timestamp=at(seconds, suffix), message={"role": role, "usage": usage})


# Claude Code session logs: (name, the log's bytes).
CLAUDE_LOGS = [
    ("the Python tests' log", "\n".join([
        line(type="user", timestamp=at(0), message={"role": "user"}),
        claude(50, "msg_0", tokens(10, 20, 7, 1)), claude(50, "msg_0", tokens(10, 20, 7, 1)),
        claude(90, "msg_1", tokens(1, 2, 7, 1)), claude(90, "msg_1", tokens(1, 2, 7, 1)),
        claude(500, "msg_2", tokens(9, 9, 7, 1))]).encode() + b"\n"),
    ("cache counts missing, a user line with usage, a summary line", "\n".join([
        claude(10, "a", tokens(3, 4)), claude(11, "u", tokens(100, 100), kind="user"),
        line(type="summary", summary="x", leafUuid="y"), claude(12, "b", tokens(5, 6, 0, 2))]).encode()),
    ("counts that are not ints", "\n".join([
        claude(1, "f", tokens(1.0, 2)), claude(2, "t", tokens(True, 2)), claude(3, "s", tokens("1", 2)),
        claude(4, "n", tokens(None, 2)), claude(5, "c", tokens(1, 2, 3.5)), claude(6, "ok", tokens(-1, 0, 0, 0)),
        claude(7, "big", tokens(2**62, 2**62, 2**62, 2**62))]).encode()),
    ("a line whose counts fail marks its id seen", "\n".join([
        claude(1, "m", {"input_tokens": 1}), claude(2, "m", tokens(5, 5)),
        claude(3, "l", [1, 2]), claude(4, "l", tokens(6, 6)),
        claude(5, "v", tokens(1.5, 1)), claude(6, "v", tokens(7, 7))]).encode()),
    ("a line that fails before its id is read does not", "\n".join([
        line(type="assistant", timestamp="garbled", message={"id": "g", "usage": tokens(1, 1)}),
        claude(2, "g", tokens(8, 8)),
        line(type="assistant", message={"id": "h", "usage": tokens(1, 1)}), claude(3, "h", tokens(9, 9)),
        line(type="assistant", timestamp=5, message={"id": "i", "usage": tokens(1, 1)}),
        claude(4, "i", tokens(10, 10))]).encode()),
    ("ids Python's set takes as one", "\n".join([
        claude(1, 1, tokens(1, 1)), claude(2, 1.0, tokens(2, 2)), claude(3, True, tokens(3, 3)),
        claude(4, None, tokens(4, 4)), line(type="assistant", timestamp=at(5), message={"usage": tokens(5, 5)}),
        claude(6, 0, tokens(6, 6)), claude(7, False, tokens(7, 7)), claude(8, -0.0, tokens(8, 8)),
        claude(9, "1", tokens(9, 9)), claude(10, 2.5, tokens(10, 10)), claude(11, 2.5, tokens(11, 11)),
        claude(12, [1], tokens(12, 12)), claude(13, {"a": 1}, tokens(13, 13)), claude(14, [1], tokens(14, 14))])
     .encode()),
    ("entries and messages of other types", "\n".join([
        "[1, 2]", '"text"', "3", "null", line(type="assistant", timestamp=at(1), message="m"),
        line(type="assistant", timestamp=at(2), message=["usage"]),
        line(type="assistant", timestamp=at(3), message={"id": "x", "usage": "u"}),
        line(type="assistant", timestamp=at(4), message={"id": "x", "usage": tokens(4, 4)}),
        line(type="assistant", timestamp=at(5), message={"id": "y"}),
        line(type=["assistant"], timestamp=at(6), message={"id": "z", "usage": tokens(6, 6)}),
        claude(7, "z", tokens(7, 7))]).encode()),
    ("line ends, blanks, a BOM and lines that are not JSON", b"\r\n".join([
        claude(1, "crlf", tokens(1, 1)).encode(), b"", b"   ", b"not json",
        b"\xef\xbb\xbf" + claude(2, "bom", tokens(2, 2)).encode(),
        b"  " + claude(3, "padded", tokens(3, 3)).encode() + b"\t ",
        claude(4, "end", tokens(4, 4)).encode()[:-1]]) + b"\r\n" + claude(5, "last", tokens(5, 5)).encode()),
    ("lone surrogates and bytes that are not UTF-8", b"\n".join([
        claude(1, "lone", tokens(1, 1), content=[{"text": "cut \ud83d"}]).encode(),
        claude(2, "pair", tokens(2, 2), content=[{"text": "whole \U0001F600"}]).encode(),
        claude(3, "low", tokens(3, 3), content=[{"text": "\udc00\\ud800"}]).encode(),
        claude(4, "bytes", tokens(4, 4), content="CONTENT").encode().replace(b"CONTENT", b"\xff\xfe raw"),
        b"\xff" + claude(5, "outside", tokens(5, 5)).encode(),
        claude(6, "escaped", tokens(6, 6), content="\\ud800 \\\\ud800 \\\\\\ud800").encode()])),
    ("times in each form", "\n".join([
        line(type="assistant", timestamp=t, message={"id": f"t{k}", "usage": tokens(k, k)}) for k, t in enumerate([
            at(1), at(2, "+00:00"), at(3, "+02:00"), at(4, "-05:30"), "2026-10-03T04:00:06Z",
            "2026-10-03T04:00:07.123456789Z", "2026-10-03 04:00:08,5Z", "20261003T040009Z", "2026-W40-6T04:00:10Z",
            "2026-10-03T24:00:00Z", "2026-10-03T04:00:61Z", "2026-02-30T00:00:00Z", "2026-10-03T04:00:11+25:00",
            "2026-10-03T04:00:12.Z", "2026-10-03T04:00:13+00", "2026-10-03T04Z"])]).encode()),
    ("a large log", "\n".join(claude(k, f"msg_{k // 3}", tokens(k, 2 * k, 3, k % 5)) for k in range(600)).encode()),
]

PI_LOGS = [
    ("the Python tests' log", "\n".join([
        pi(130, {"input": 5, "output": 6, "cacheRead": 2}), pi(140, {"input": 100, "output": 100}, role="user"),
        "not json"]).encode()),
    ("each answer counts, cache counts, other roles and types", "\n".join([
        pi(1, {"input": 1, "output": 1, "cacheRead": 1, "cacheWrite": 1}), pi(1, {"input": 1, "output": 1}),
        pi(2, {"input": 2, "output": 2.0}), pi(3, {"input": 3, "output": 3, "cacheWrite": False}),
        pi(4, {"input": 4}), pi(5, {"input": 5, "output": 5}, role=None),
        pi(6, {"input": 6, "output": 6}, suffix="+01:00"),
        line(type="message", timestamp=at(7), message={"role": "assistant", "usage": {"input_tokens": 7}}),
        line(type="assistant", timestamp=at(8), message={"role": "assistant", "usage": {"input": 8, "output": 8}}),
        line(timestamp=at(9), message={"role": "assistant", "usage": ["input"]})]).encode()),
]

# Turns as state.json records them, each summed over each Claude Code log above.
TURNS = [
    {"agent": "claude", "session": "s", "started_at": o.iso_utc(EPOCH + 0), "ended_at": o.iso_utc(EPOCH + 20)},
    {"agent": "claude", "session": "s", "started_at": o.iso_utc(EPOCH + 0), "ended_at": o.iso_utc(EPOCH + 10)},
    {"agent": "claude", "session": "s", "started_at": o.iso_utc(EPOCH + 0), "ended_at": o.iso_utc(EPOCH + 9)},
    {"agent": "claude", "session": "s", "started_at": o.iso_utc(EPOCH + 50), "ended_at": o.iso_utc(EPOCH + 90)},
    {"agent": "claude", "session": "s", "started_at": o.iso_utc(EPOCH + 51), "ended_at": o.iso_utc(EPOCH + 1000)},
    {"agent": "claude", "session": "s", "started_at": "2026-10-03T06:00:00+02:00", "ended_at": "2026-10-03T04:01:40Z"},
    {"agent": "claude", "session": "s", "started_at": None, "ended_at": o.iso_utc(EPOCH)},
    {"agent": "claude", "session": "s", "started_at": o.iso_utc(EPOCH), "ended_at": None},
    {"agent": "claude", "session": None, "started_at": o.iso_utc(EPOCH), "ended_at": o.iso_utc(EPOCH + 9)},
    {"agent": "claude", "session": "", "started_at": o.iso_utc(EPOCH), "ended_at": o.iso_utc(EPOCH + 9)},
    {"agent": "claude", "started_at": o.iso_utc(EPOCH), "ended_at": o.iso_utc(EPOCH + 9)},
    {"agent": "claude", "session": 7, "started_at": o.iso_utc(EPOCH), "ended_at": o.iso_utc(EPOCH + 9)},
    {"agent": "claude", "session": "s", "started_at": "garbled", "ended_at": o.iso_utc(EPOCH + 9)},
    {"agent": "claude", "session": "s", "started_at": 5, "ended_at": o.iso_utc(EPOCH + 9)},
    {"agent": "claude", "session": "s", "started_at": "", "ended_at": o.iso_utc(EPOCH + 9)},
    {"agent": "claude", "session": "s", "started_at": o.iso_utc(EPOCH - 10**6), "ended_at": o.iso_utc(EPOCH + 10**6)},
]

# ISO strings, as datetime.fromisoformat reads them: the forms a harness or the engine writes, each other form it
# takes, and near misses of each.
TIMES = [
    "2026-10-28T12:00:00.250Z", "2026-10-28T12:00:00+00:00", "2026-10-28T12:00:00.123456+00:00", "2026-10-28",
    "2026-10-28T12:00:00", "2026-10-28 12:00:00", "2026-10-28T12:00:00.1234567Z", "2026-10-28T12:00:00.1Z",
    "2026-10-28T12:00:00,5Z", "20261028T120000Z", "20261028T1200", "2026-W43-3", "2026W433", "2026-W43",
    "2026W43", "2026-W53-1", "2020-W53-7", "2015-W53-1", "2026-W00-1", "2026-W43-8", "2026-W43-0", "2026W43-3",
    "2026-W433", "2026-W43-", "2026-W43-3T12:00Z", "2026W433T12Z", "2026-10-28T12Z", "2026-10-28T12:00:00+0100",
    "2026-10-28T12:00:00+01", "2026-10-28T12:00:00+01:00:30", "2026-10-28T12:00:00.5+01:00:30.5",
    "2026-10-28T12:00:00+010030.5", "2026-10-28T12:00:00+23:59", "2026-10-28T12:00:00+24:00",
    "2026-10-28T12:00:00-23:59:59.999999", "2026-10-28T12:00:00+01:99", "2026-10-28T12:00:00+1",
    "2026-10-28T12:00:00+011", "2026-10-28T12:00:00+", "2026-10-28T24:00:00Z", "2026-12-31T24:00",
    "2026-02-28T24:00", "2024-02-28T24:00", "2026-02-29T24:00", "2026-10-28T24:00:01", "2026-10-28T24:00:00.000001",
    "2026-10-28T25:00", "2026-10-28T12:60", "2026-10-28T12:00:60Z", "2026-10-28T12:00:00z",
    "2026-10-28X12:00:00Z", "2026-10-28é12:00", "2026-10-28T1200Z", "2026-10-28T12:0000", "2026-10-28T1200:00",
    "2026-10-28T12:00.5", "2026-10-28T12.5", "2026-10-28T12:00:00.Z", "2026-10-28T12:00:00.", "2026-10-28T12:00:00.x",
    "2026-10-28T1", "2026-10-28T", "2026-10-28T12:00:00Z+01:00", "2026-10-28T12:00:00-01:00Z",
    "2026-10-28T12:00:00+01:00-02:00", "2026-10-28T12-01", "2026-10-28T12:00Z05", "2026-10-28T12:00:00 ",
    " 2026-10-28", "+2026-10-28", "2026-1-28", "2026-10-8", "2026-10-28T12:00:00.000000001",
    "2026-13-01", "2026-00-01", "2026-02-29", "2024-02-29", "0000-01-01", "0001-01-01T00:00:00+01:00",
    "9999-12-31T23:59:59.999999", "9999-12-31T24:00", "1969-12-31T23:59:59.5Z", "1960-01-01T00:00:00.000001Z",
    "202610-28", "2026-1028", "20261028", "2026102", "2026-10", "2026W4", "", "2026", "garbled", "２０２６-10-28",
    "2026-10-28T１２:00", "2026-10-28T12:00:00.５", "2026-10-28T12:00:00+0１:00",
]


def fuzzed_times(n: int) -> list[str]:
    """n strings each one of the TIMES fromisoformat reads with one to three edits (a character deleted, or a piece of
    an ISO time put in its place or before it), so that the parsers are compared beyond the cases above, near a valid
    time."""
    pieces = ["2026", "10", "28", "-", "W", "43", "3", "T", " ", "12", ":", "00", "5", ".", ",", "123456789",
              "Z", "+", "01", "24", "60", "0", "é", ""]
    rng = random.Random(6)
    valid = [iso for iso in TIMES if "ok" in timestamp(iso)]
    out = []
    for _ in range(n):
        iso = rng.choice(valid)
        for _ in range(rng.randint(1, 3)):
            at, piece = rng.randint(0, len(iso)), rng.choice(pieces)
            iso = iso[:at] + piece + iso[at + rng.randint(0, 1):]
        out.append(iso)
    return out


def timestamp(iso: str) -> dict:
    """{"ok": the time fromisoformat reads, as timestamp() gives it}, or {"error": what it raises}."""
    try:
        read = datetime.fromisoformat(iso)
        return {"ok": (read if read.tzinfo else read.replace(tzinfo=timezone.utc)).timestamp()}
    except ValueError as e:
        return {"error": str(e)}


def usage(kind: str, log: bytes) -> list:
    return [[when, tokens] for when, tokens in o.session_usage(kind, os.fsdecode(log))]


def build() -> dict:
    """The oracle: each kind of call with its cases and Python's answers."""
    kinds = list(o.AGENT_KINDS)
    claude_usage = [o.session_usage("claude", os.fsdecode(log)) for _, log in CLAUDE_LOGS]
    return {
        "constants": {name: getattr(o, name) for name in [
            "AGENT_KINDS", "DEFAULT_AGENT", "PERMISSION_ARGS", "RESUME_ARGS", "ADD_DIR_ARGS", "SESSION_LOGS"]},
        "permission_args": [[kind, mode, o.permission_args(kind, mode)] for kind in kinds for mode in MODES if mode],
        "launch_args": [[kind, mode, model, session, extra, o.launch_args(kind, mode, model, session, extra)]
                        for kind in kinds for mode in MODES for model in MODELS for session in SESSIONS
                        for extra in DIRS],
        "session_usage": [[kind, name, log.hex(), usage(kind, log)]
                          for kind, logs in (("claude", CLAUDE_LOGS), ("pi", PI_LOGS)) for name, log in logs],
        "turn_tokens": [[turn, [o.turn_tokens(turn, u) for u in claude_usage] + [o.turn_tokens(turn, None)]]
                        for turn in TURNS],
        "fromisoformat": [[iso, timestamp(iso)] for iso in TIMES + fuzzed_times(3000)],
    }


def text(oracle: dict) -> str:
    """The file's content for the oracle build() gives."""
    return json.dumps(oracle, indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    """Write the oracle to OUT."""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text(build()))


if __name__ == "__main__":
    main()
