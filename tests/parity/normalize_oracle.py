"""Write what normalize.py gives for the inputs here, as JSON, to standard output: the oracle the e2e harness's
normalizer is held to (engine/e2e/src/normalize.rs, its oracle tests). Make it again after a change to the inputs:

    python3 tests/parity/normalize_oracle.py > engine/e2e/src/normalize/oracle.json

The normalizer it uses: without the host's name, so that the oracle holds on any host; two literals, one inside the
other; three pids, the first registered twice; RECORD learnt. The Rust tests make the same one, so a change to either
side's setup is a change to both."""

import json
import sys

from normalize import Normalizer, facts

TEXTS = [
    "run 20260101-120000-abc123 in /w/orld/project and /w/x, key abc123, xabc123 abc123g",
    "commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1 aaaaaaa aaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef 01234567 00000000000000000000000000000000000000000",
    "at 2026-01-02T03:04:05Z and 2026-01-02 03:04:05.123+01:00 and 2026-01-02T03:04:05+0100 and 2026-01-02T03:04:05+01:0 and x2026-01-02T03:04:05",
    '{"seconds": 12, "mtime": 3.5, "heartbeat_age": 7, "pid": 4242, "pid": 99}',
    "pid 4242 and 77 and 424242 and a77",
    "took 5s, 2m 05s, 1h 02m, 2m, 1h2m, 12m 5s, 3h 120m, 7ms, x5s, 5s1",
]
RECORD = {
    "x": [
        "run 20260101-120000-abc123 at aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
        {
            "y": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef and 20260102-000000-0f0f0f"
        },
        "\n20260103-000000-111111",
    ]
}
JSON_IN = {
    "pid": 4242,
    "seconds": 3,
    "health_reason": "beat 3s ago",
    "list": [77, True, "20260101-120000-abc123"],
    "other": 5,
    "pid2": {"pid": 1},
}
FACTS = [
    "<run:1> failed: see https://x.org/a?b=1, at /tmp/x/y and a/ or /b; VERDICT: APPROVE NO_PR AB ABC_ D_E (exit 3) foo_bar _x x_ round-2 v0.9.4 <pid:?> <x> <sha:12> <a:> é5 5é ok.",
    "x1 1x a1b-c (2) -5 path/ /p //",
]

n = Normalizer()
n._literals.clear()
n.literal("/w/orld", "<world>")
n.literal("/w", "<short>")
n.pid(4242, 1)
n.pid(77, 2)
n.pid(4242, 3)
n.learn(RECORD)
oracle = {
    "texts": [[t, n.text(t), n.text(t, timed=True)] for t in TEXTS],
    "json": [RECORD, n.json(JSON_IN), n.json({"a": "5s"}, timed=True)],
    "json_in": JSON_IN,
    "facts": [[t, facts(t)] for t in FACTS],
}
json.dump(oracle, sys.stdout, indent=1, ensure_ascii=False)
print()
