"""What differs between two runs of one parity scenario, and so between a recording and its replay: `Normalizer`.

The spec's Parity section names it: times, run ids, pids and hashes, to which a world adds its own directory, the
ports of its servers, serve's token and the host's name. Everything else in a record is compared as it is.
"""

import json
import re
import socket

# A run id as new_run_id makes it: the time it started and a random key of six hex digits.
RUN_ID = re.compile(r"\b\d{8}-\d{6}-([0-9a-f]{6})\b")
# A git object name, SHA-1's 40 hex digits or SHA-256's 64; and an abbreviation of one, as messages shorten them.
SHA = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?:[0-9a-f]{24})?(?![0-9a-f])")
ABBREVIATED_SHA = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,39}(?![0-9a-f])")
# A time as iso_utc writes it, and as turns_table shows it, with a space for the T and no offset.
TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
# A duration as format_duration and format_age write it: 5s, 2m 05s, 1h 02m, 2m, 1h2m.
DURATION = re.compile(r"\b(?:\d+h ?\d{2}m|\d+m(?: \d{2}s)?|\d+s)\b")
# The JSON keys whose numbers are times measured by a clock: a turn's or a run's seconds, a state's age, a file's
# modification time.
TIMED_KEYS = frozenset({"seconds", "heartbeat_age", "mtime"})
# A number under one of TIMED_KEYS, or a pid, in JSON text such as metrics.json's, kept as text so that its layout
# is compared too.
TIMED_JSON = re.compile(rf'"({"|".join(sorted(TIMED_KEYS))})": \d+')
PID_JSON = re.compile(r'"pid": (\d+)')
# The JSON keys whose strings word a time measured by a clock, such as run_health's "beat 3s ago".
TIMED_TEXT_KEYS = frozenset({"health_reason"})


class Normalizer:
    """Replaces what differs between runs of a scenario with placeholders that are the same in every run.

    Runs, their keys and git objects are numbered in the order they first appear in what `learn` is given, so a
    record normalizes the same however its random ids came out: `<run:1>` and `<key:1>`, `<sha:3>`. A pid of an
    engine invocation becomes `<pid:N>`, N being the invocation's number. Literal strings registered with
    `literal`, such as the world's directory, become their placeholder.
    """

    def __init__(self):
        self._literals: list[tuple[str, str]] = [(socket.gethostname(), "<host>")]
        self._runs: dict[str, str] = {}  # a run id to its placeholder; keyed by its key in _keys
        self._keys: dict[str, str] = {}
        self._shas: dict[str, str] = {}
        self._pids: dict[int, str] = {}

    def literal(self, text: str, placeholder: str) -> None:
        """Write placeholder wherever text appears; a longer text registered is replaced before a shorter one."""
        self._literals.append((text, placeholder))
        self._literals.sort(key=lambda pair: -len(pair[0]))

    def pid(self, pid: int, n: int) -> None:
        """Write `<pid:n>` for pid, the pid of engine invocation n, as a number in JSON and as a word in text."""
        self._pids[pid] = f"<pid:{n}>"

    def learn(self, record) -> None:
        """Number the runs and git objects in record, anything JSON-ready, in the order they appear in it, after
        those learnt before."""
        text = json.dumps(record)
        for m in RUN_ID.finditer(text):
            if m.group(0) not in self._runs:
                n = len(self._runs) + 1
                self._runs[m.group(0)] = f"<run:{n}>"
                self._keys[m.group(1)] = f"<key:{n}>"
        for m in SHA.finditer(text):
            self._shas.setdefault(m.group(0), f"<sha:{len(self._shas) + 1}>")

    def text(self, s: str, timed: bool = False) -> str:
        """s with every placeholder written in; with timed, also each duration, for text that words the clock's
        times, such as summary.md's turns table."""
        for literal, placeholder in self._literals:
            s = s.replace(literal, placeholder)
        s = RUN_ID.sub(lambda m: self._runs.get(m.group(0), "<run:?>"), s)
        for key, placeholder in self._keys.items():
            s = re.sub(rf"(?<![0-9a-f]){key}(?![0-9a-f])", placeholder, s)
        s = SHA.sub(lambda m: self._shas.get(m.group(0), "<sha:?>"), s)
        s = ABBREVIATED_SHA.sub(self._abbreviated, s)
        s = TIME.sub("<time>", s)
        s = TIMED_JSON.sub(r'"\1": "<seconds>"', s)
        s = PID_JSON.sub(lambda m: f'"pid": "{self._pids.get(int(m.group(1)), "<pid:?>")}"', s)
        for pid, placeholder in self._pids.items():
            s = re.sub(rf"\b{pid}\b", placeholder, s)
        return DURATION.sub("<duration>", s) if timed else s

    def _abbreviated(self, m: re.Match) -> str:
        """The placeholder of the one known object whose name m abbreviates; m as it is for none or several."""
        found = {p for sha, p in self._shas.items() if sha.startswith(m.group(0))}
        return found.pop() if len(found) == 1 else m.group(0)

    def json(self, node, timed: bool = False, key: str | None = None):
        """node, any JSON-ready value, with text() applied to every string and key in it, and the numbers under
        TIMED_KEYS and the pids under "pid" replaced; with timed, every string is taken as timed. key is the
        key node is under, for the recursion."""
        if isinstance(node, dict):
            return {self.text(k): self.json(v, timed, k) for k, v in node.items()}
        if isinstance(node, list):
            return [self.json(v, timed, key) for v in node]
        if isinstance(node, str):
            return self.text(node, timed or key in TIMED_TEXT_KEYS)
        if isinstance(node, int) and not isinstance(node, bool):
            if key in TIMED_KEYS:
                return "<seconds>"
            if key == "pid":
                return self._pids.get(node, "<pid:?>")
        return node


def facts(text: str) -> list[str]:
    """The content of text for a human, a normalized log, error or table, without its layout or wording: the
    placeholders, URLs, paths, upper-case words such as verdicts, identifiers with an underscore, and words with
    digits it holds, sorted, so that a reworded or relaid message with the same facts compares equal."""
    found = re.findall(r"<[a-z]+(?::[0-9?]+)?>|https?://\S+|\S*/\S+|\b[A-Z][A-Z_]{2,}\b|\b\w+_\w+\b|\b\S*\d\S*\b", text)
    return sorted(f.strip(".,;:()`'\"") for f in found)
