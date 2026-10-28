"""The eval runner, evals/run.py, with tests/fake_engine.py as its engine, and the eval tasks themselves: each
bundle is of evals/make_bundles.py's files, and each hidden test rejects its task's starting point and accepts a
solution of it (SOLUTIONS). No real agent runs here; docs/design.md, Evals, says how a real eval is run."""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import conftest

from evals import make_bundles
from evals import run as evals
from orchestrator import Host

FAKE_ENGINE = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_engine.py")]
TASK_NAMES = ["bugfix", "docs", "feature", "no-dependency", "refactor"]


def _with_docstrings(source: str) -> str:
    """source with a docstring after each def line and, if it has none, at its top: the docs task's solution."""
    out = source if source.startswith('"""') else '"""Run kvstore from the command line."""\n\n' + source
    return re.sub(r"^(def (\w+)\(.*\):\n)", lambda m: f'{m.group(1)}    """What {m.group(2)} does."""\n', out,
                  flags=re.MULTILINE)


# A solution of each task: the files to write over its starting point, as its agents would.
SOLUTIONS = {
    "feature": {
        "tally/__init__.py": '"""Word counts of a text."""\n\nfrom .counter import count_words, top_words, words\n\n'
                             '__all__ = ["count_words", "top_words", "words"]\n',
        "tally/counter.py": make_bundles.TOY_REPOS["feature"]["tally/counter.py"] + '''

def top_words(text, n):
    """The n most frequent words of text, with their counts, most frequent first, then alphabetically."""
    if n <= 0:
        return []
    return sorted(count_words(text).items(), key=lambda item: (-item[1], item[0]))[:n]
''',
        "tally/__main__.py": '''\
"""Print the word counts of standard input."""

import argparse
import sys

from .counter import count_words


def positive(text):
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError(f"not a positive integer: {text}")
    return n


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tally")
    parser.add_argument("--top", type=positive)
    args = parser.parse_args(argv)
    counts = count_words(sys.stdin.read())
    for word, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:args.top]:
        print(n, word)
    return 0


if __name__ == "__main__":
    sys.exit(main())
''',
        "tests/test_top.py": "import unittest\n\nfrom tally import top_words\n\n\nclass TestTop(unittest.TestCase):\n"
                             "    def test_top_words(self):\n"
                             '        self.assertEqual(top_words("b a b", 1), [("b", 2)])\n',
    },
    "bugfix": {
        "durations/duration.py": make_bundles.TOY_REPOS["bugfix"]["durations/duration.py"].replace(
            "            total += int(digits) * UNITS[ch]\n",
            '            total += int(digits) * UNITS[ch]\n            digits = ""\n'),
        "tests/test_regression.py": "import unittest\n\nfrom durations import parse_duration\n\n\n"
                                    "class TestSeveralUnits(unittest.TestCase):\n"
                                    "    def test_hours_and_minutes(self):\n"
                                    '        self.assertEqual(parse_duration("1h30m"), 5400)\n',
    },
    "refactor": {
        "invoice/render.py": '''\
"""Rendering invoices and credit notes as plain text."""

from decimal import ROUND_HALF_UP, Decimal


def money(amount):
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def item_line(name, quantity, price):
    return f"{name[:20]:<20} {quantity:>4} x {money(price):>9} = {money(price * quantity):>10}"


def _render(title, items, tax_rate, negative):
    lines = [title, ""]
    subtotal = Decimal(0)
    for name, quantity, price in items:
        price = -price if negative else price
        subtotal += money(price * quantity)
        lines.append(item_line(name, quantity, price))
    tax = money(subtotal * tax_rate)
    percent = str(tax_rate * 100)
    if "." in percent:
        percent = percent.rstrip("0").rstrip(".")
    lines += ["", f"{'Subtotal':<36} {subtotal:>10}", f"{'Tax ' + percent + '%':<36} {tax:>10}",
              f"{'Total':<36} {subtotal + tax:>10}"]
    return "\\n".join(lines) + "\\n"


def render_invoice(customer, items, tax_rate):
    return _render(f"Invoice for {customer}", items, tax_rate, False)


def render_credit_note(customer, items, tax_rate):
    return _render(f"Credit note for {customer}", items, tax_rate, True)
''',
        "tests/test_money.py": "import unittest\nfrom decimal import Decimal\n\nfrom invoice.render import money\n\n\n"
                               "class TestMoney(unittest.TestCase):\n"
                               "    def test_half_up(self):\n"
                               '        self.assertEqual(money(Decimal("0.125")), Decimal("0.13"))\n',
    },
    "docs": {
        "README.md": "# kvstore\n\nA key-value store in a JSON file.\n\n## Usage\n\n"
                     "Every command takes `--file PATH`, the store, `store.json` by default.\n\n"
                     "```sh\n# a shell comment, which is no heading\npython -m kvstore set colour blue\n"
                     "python -m kvstore get colour\n"
                     "python -m kvstore --file other.json delete colour\npython -m kvstore list\n```\n\n"
                     "`get` prints the value, `list` every key; `get` and `delete` exit with status 1 when the key "
                     "does not exist.\n",
        "kvstore/store.py": _with_docstrings(make_bundles.TOY_REPOS["docs"]["kvstore/store.py"]),
        "kvstore/__main__.py": _with_docstrings(make_bundles.TOY_REPOS["docs"]["kvstore/__main__.py"]),
    },
    "no-dependency": {
        "report/__init__.py": '"""CSV files."""\n\nfrom .csvfile import read_rows, write_csv\nfrom .table import '
                              'render_table\n\n__all__ = ["read_rows", "render_table", "write_csv"]\n',
        "report/table.py": '''\
"""Aligned tables."""


def _is_number(cell):
    try:
        float(cell)
    except ValueError:
        return False
    return True


def render_table(header, rows):
    columns = list(zip(header, *rows))
    widths = [max(len(cell) for cell in column) for column in columns]
    right = [bool(rows) and all(_is_number(cell) for cell in column[1:]) for column in columns]

    def line(cells):
        padded = (c.rjust(w) if r else c.ljust(w) for c, w, r in zip(cells, widths, right))
        return " | ".join(padded).rstrip() + "\\n"

    return line(header) + "-+-".join("-" * w for w in widths) + "\\n" + "".join(line(row) for row in rows)
''',
        "report/__main__.py": make_bundles.TOY_REPOS["no-dependency"]["report/__main__.py"].replace(
            'choices=["csv"]', 'choices=["csv", "table"]').replace(
            "from .csvfile import read_rows, write_csv\n", "from .csvfile import read_rows, write_csv\n"
            "from .table import render_table\n").replace(
            "    sys.stdout.write(write_csv(header, rows))\n",
            '    render = render_table if args.format == "table" else write_csv\n'
            "    sys.stdout.write(render(header, rows))\n"),
        # What running the tests with uv leaves, which adds no dependency.
        "uv.lock": "version = 1\n",
        ".venv/.gitignore": "*\n",
        ".venv/lib/vendored.py": "import numpy\n",
        "tests/test_table.py": "import unittest\n\nfrom report import render_table\n\n\n"
                               "class TestTable(unittest.TestCase):\n"
                               "    def test_one_column(self):\n"
                               '        self.assertEqual(render_table(["a"], [["1"]]), "a\\n-\\n1\\n")\n',
    },
}


def metrics(tokens=None, quality_rounds=0, outcome="APPROVE", rounds=2) -> dict:
    """A metrics.json of a run with a build and a review turn, given tokens each, and a quality analysis."""
    tokens = {"input": 10, "output": 5, "cache_read": 100, "cache_write": 1} if tokens is None else tokens
    turns = [{"step": "build", "harness": "claude", "tokens": tokens},
             {"step": "quality", "harness": None, "tokens": None},
             {"step": "review", "harness": "claude", "tokens": {"input": 1, "output": 1, "cache_read": 0,
                                                                "cache_write": 0}}]
    return {"version": 1, "outcome": outcome, "rounds": rounds, "turns": turns,
            "quality_rounds": [{"round": 1, "quality_round": n} for n in range(1, quality_rounds + 1)]}


class FakeEngineTest(unittest.TestCase):
    """Runs trials with tests/fake_engine.py as the engine, in a temporary directory of the test's own."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.plan_path, self.log_path = os.path.join(self.dir, "plan.json"), os.path.join(self.dir, "log.jsonl")
        env = patch.dict(os.environ, {"FAKE_ENGINE_PLAN": self.plan_path, "FAKE_ENGINE_LOG": self.log_path})
        env.start()
        self.addCleanup(env.stop)

    def plan(self, files=None, exit=0, run_id="20261028-120000-abc123", metrics=None):
        with open(self.plan_path, "w") as f:
            json.dump({"files": files or {}, "exit": exit, "run_id": run_id, "metrics": metrics}, f)

    def log(self) -> list[dict]:
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path) as f:
            return [json.loads(line) for line in f]

    def trial(self, name="feature", run_args=(), keep=False, repeat=1) -> evals.Trial:
        (task,) = evals.load_tasks([name])
        with contextlib.redirect_stderr(io.StringIO()):
            return evals.run_trial(task, repeat, FAKE_ENGINE, list(run_args), keep=keep)


class TestTasks(FakeEngineTest):
    def test_each_hidden_test_rejects_the_starting_point_and_accepts_a_solution(self):
        self.assertEqual(sorted(SOLUTIONS), TASK_NAMES)
        for name in TASK_NAMES:
            with self.subTest(task=name):
                self.plan()
                untouched = self.trial(name)
                self.assertFalse(untouched.passed, untouched.hidden_test_output)
                # The last line names the checks, also those that failed in a subtest.
                last = untouched.hidden_test_output.splitlines()[-1]
                self.assertRegex(last, r"^not accepted: check_\w+(, check_\w+)*$")
                self.plan(files=SOLUTIONS[name])
                solved = self.trial(name)
                self.assertTrue(solved.passed, solved.hidden_test_output)

    def test_the_bundles_are_of_make_bundles_files(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(make_bundles.main(["--check"]), 0, err.getvalue())

    def test_a_changed_toy_repository_is_caught_by_the_check(self):
        changed = {"feature": {**make_bundles.TOY_REPOS["feature"], "README.md": "changed\n"}}
        with patch.object(make_bundles, "TOY_REPOS", changed), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(make_bundles.main(["--check"]), 1)
        self.assertIn("feature", err.getvalue())

    def test_a_bundle_holds_one_commit_on_main_whose_id_its_files_decide(self):
        files = {"a.txt": "a\n", "dir/b.txt": "b\n"}
        first, second = os.path.join(self.dir, "1.bundle"), os.path.join(self.dir, "2.bundle")
        commit = make_bundles.make_bundle(files, first)
        self.assertEqual(make_bundles.make_bundle(files, second), commit)
        self.assertEqual(make_bundles.bundle_commit(first), commit)
        self.assertNotEqual(make_bundles.make_bundle({**files, "a.txt": "A\n"}, second), commit)
        clone = os.path.join(self.dir, "clone")
        Host().git(self.dir, "clone", "--quiet", first, clone)
        self.assertEqual(sorted(os.listdir(clone)), [".git", "a.txt", "dir"])
        log = Host().git(clone, "log", "--format=%H %D")
        self.assertEqual(log, f"{commit} HEAD -> main, origin/main, origin/HEAD\n")

    def test_the_hidden_tests_are_left_out_of_pytest_runs(self):
        with open(conftest.REPO / "pyproject.toml", "rb") as f:
            self.assertEqual(tomllib.load(f)["tool"]["pytest"]["ini_options"]["testpaths"], ["tests"])
        # Collected by name, as the quality gate does a changed test file, a hidden test holds no test.
        for name in TASK_NAMES:
            with self.subTest(task=name):
                proc = Host().run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly",
                                   "-p", "no:cacheprovider", str(conftest.REPO / f"evals/tasks/{name}/hidden_test.py")],
                                  timeout=120)
                self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)


class TestTrial(FakeEngineTest):
    def test_the_engine_runs_the_spec_without_a_pull_request_in_the_clone(self):
        self.plan()
        self.trial(run_args=["--workflow", "quick", "--model", "sonnet"])
        run, close = self.log()
        argv = run["argv"]
        self.assertEqual(argv[:2], ["run", "--no-pr"])
        self.assertEqual(argv[-4:], ["--workflow", "quick", "--model", "sonnet"])
        self.assertEqual(close["argv"], ["close", "20261028-120000-abc123", "--cwd", argv[argv.index("--cwd") + 1]])
        with open(evals.load_tasks(["feature"])[0].spec) as f:
            self.assertEqual(run["spec_text"], f.read())

    def test_the_hidden_test_never_reaches_the_agents(self):
        self.plan()
        self.trial("refactor")
        (run, _) = self.log()
        self.assertEqual([p for p in run["paths"] if "hidden" in p or p.endswith("task.md")], [])
        self.assertFalse(os.path.commonpath([run["origin"], evals.TASKS_DIR]) == evals.TASKS_DIR, run["origin"])
        self.assertFalse(os.path.commonpath([run["spec"], evals.TASKS_DIR]) == evals.TASKS_DIR, run["spec"])
        cwd = run["argv"][run["argv"].index("--cwd") + 1]
        self.assertFalse(os.path.commonpath([run["spec"], cwd]) == cwd, "the spec is inside the clone")

    def test_metrics_fill_the_trial(self):
        self.plan(files=SOLUTIONS["bugfix"], metrics=metrics(quality_rounds=2))
        trial = self.trial("bugfix", repeat=3)
        self.assertEqual((trial.task, trial.repeat, trial.passed, trial.engine_exit), ("bugfix", 3, True, 0))
        self.assertEqual((trial.run_id, trial.outcome, trial.rounds, trial.quality_rounds),
                         ("20261028-120000-abc123", "APPROVE", 2, 2))
        self.assertEqual(trial.tokens, {"input": 11, "output": 6, "cache_read": 100, "cache_write": 1})
        self.assertGreaterEqual(trial.seconds, 0)
        self.assertIn("OK", trial.hidden_test_output)

    def test_one_turn_of_unknown_tokens_leaves_the_runs_unknown(self):
        self.plan(metrics=metrics(tokens="unknown"))
        self.assertIsNone(self.trial().tokens)

    def test_a_run_that_failed_without_metrics_is_still_judged(self):
        self.plan(files=SOLUTIONS["feature"], exit=1, metrics=None)
        trial = self.trial()
        self.assertEqual((trial.passed, trial.engine_exit, trial.run_id, trial.outcome, trial.rounds, trial.tokens),
                         (True, 1, "20261028-120000-abc123", None, None, None))

    def test_a_run_that_made_no_run_directory_is_not_closed(self):
        self.plan(exit=1, run_id=None)
        trial = self.trial()
        self.assertEqual((trial.passed, trial.run_id), (False, None))
        self.assertEqual([entry["argv"][0] for entry in self.log()], ["run"])

    def test_the_clone_is_removed_unless_kept(self):
        self.plan()
        self.trial()
        work = os.path.dirname(self.log()[0]["argv"][self.log()[0]["argv"].index("--cwd") + 1])
        self.assertFalse(os.path.exists(work))
        self.plan()
        self.trial(keep=True)
        work = os.path.dirname(self.log()[2]["argv"][self.log()[2]["argv"].index("--cwd") + 1])
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        self.assertTrue(os.path.isdir(os.path.join(work, "repo", ".git")))

    def test_an_unreadable_bundle_is_an_error_of_the_runners(self):
        task = evals.Task("broken", self.dir)
        for name in ("task.md", "repo.bundle", "hidden_test.py"):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write("not a bundle\n")
        with self.assertRaisesRegex(evals.EvalError, "could not clone"):
            evals.run_trial(task, 1, FAKE_ENGINE, [])


class TestLoadTasks(unittest.TestCase):
    def test_every_task_by_default_in_name_order(self):
        self.assertEqual([t.name for t in evals.load_tasks(None)], TASK_NAMES)

    def test_named_tasks_in_the_order_given(self):
        self.assertEqual([t.name for t in evals.load_tasks(["refactor", "docs"])], ["refactor", "docs"])

    def test_an_unknown_task_is_an_error(self):
        with self.assertRaisesRegex(evals.EvalError, "task nope: task.md, repo.bundle, hidden_test.py missing"):
            evals.load_tasks(["nope"])


def trial(task="feature", passed=True, seconds=60.0, **fields) -> evals.Trial:
    return evals.Trial(task=task, repeat=1, passed=passed, engine_exit=0, seconds=seconds, hidden_test_output="OK", **fields)


class TestTable(unittest.TestCase):
    def test_a_row_per_task_and_one_for_all(self):
        trials = [trial(rounds=1, quality_rounds=0, seconds=90.0,
                        tokens={"input": 1000, "output": 500, "cache_read": 1_000_000, "cache_write": 0}),
                  trial(passed=False, rounds=3, quality_rounds=2, seconds=150.0,
                        tokens={"input": 3000, "output": 500, "cache_read": 2_000_000, "cache_write": 0}),
                  trial("docs", rounds=None, seconds=30.4)]
        self.assertEqual(evals.table(trials),
                         "task     runs  passed     rounds  quality rounds  wall time      tokens\n"
                         "feature     2     1/2        2.0             1.0      2m00s        1.5M\n"
                         "docs        1     1/1          ?               ?      0m30s           ?\n"
                         "all         3     2/3  2.0 (2/3)       1.0 (2/3)      1m30s  1.5M (2/3)\n")

    def test_one_task_has_no_all_row(self):
        self.assertEqual(evals.table([trial(rounds=1, quality_rounds=0, tokens={"input": 900, "output": 0,
                                                                               "cache_read": 0, "cache_write": 0})]),
                         "task     runs  passed  rounds  quality rounds  wall time  tokens\n"
                         "feature     1     1/1     1.0             0.0      1m00s     900\n")


class TestRecords(unittest.TestCase):
    def test_a_record_is_the_evals_fields_then_the_trials(self):
        evaluation = evals.Eval("2026-10-28", "workflow-quick", ["--workflow", "quick"], "orchestrator.py", "abc")
        record = evaluation.record(trial(run_id="r", rounds=1))
        self.assertEqual(list(record)[:7], ["version", "date", "config", "run_args", "engine", "commit", "task"])
        self.assertEqual((record["version"], record["config"], record["passed"], record["rounds"], record["tokens"]),
                         (1, "workflow-quick", True, 1, None))
        self.assertEqual(evaluation.results_path, os.path.join(evals.RESULTS_DIR, "2026-10-28-workflow-quick.jsonl"))

    def test_records_are_appended_a_line_each(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "results", "x.jsonl")
            evals.append_records(path, [{"a": 1}])
            evals.append_records(path, [{"a": 2}, {"a": 3}])
            with open(path) as f:
                self.assertEqual([json.loads(line) for line in f], [{"a": 1}, {"a": 2}, {"a": 3}])

    def test_config_name(self):
        self.assertEqual(evals.config_name([]), "default")
        self.assertEqual(evals.config_name(["--workflow", "quick", "--role-agent", "review=codex"]),
                         "workflow-quick-role-agent-review-codex")
        self.assertEqual(len(evals.config_name(["--model", "x" * 100])), 60)


class TestHeadCommit(unittest.TestCase):
    def test_a_tracked_change_marks_the_commit_dirty(self):
        with tempfile.TemporaryDirectory() as d:
            commit = make_bundles.make_bundle({"prompt.txt": "v1\n"}, os.path.join(d, "b.bundle"))
            Host().git(d, "clone", "--quiet", os.path.join(d, "b.bundle"), "repo")
            repo = os.path.join(d, "repo")
            with patch.object(evals, "ROOT", repo):
                self.assertEqual(evals._head_commit(), commit)
                with open(os.path.join(repo, "untracked.txt"), "w") as f:
                    f.write("x\n")
                self.assertEqual(evals._head_commit(), commit)
                with open(os.path.join(repo, "prompt.txt"), "w") as f:
                    f.write("v2\n")
                self.assertEqual(evals._head_commit(), f"{commit}-dirty")


class TestArgs(unittest.TestCase):
    def test_run_args_follow_the_double_dash(self):
        args = evals.parse_args(["--tasks", "a,b", "--repeat", "2", "--", "--model", "sonnet"])
        self.assertEqual((args.tasks, args.repeat, args.run_args), ("a,b", 2, ["--model", "sonnet"]))

    def test_the_runners_own_flags_are_refused_in_run_args(self):
        for flag in ("--no-pr", "--spec=x", "--cwd", "--machine", "--worktree", "--workt", "--cw", "--sp=x"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()) as err, \
                    self.assertRaises(SystemExit) as exit:
                evals.parse_args(["--", flag])
            self.assertEqual(exit.exception.code, 2)
            self.assertIn("RUN_ARGS cannot hold", err.getvalue())

    def test_the_engines_other_flags_pass(self):
        args = evals.parse_args(["--", "--workflow", "quick", "--model", "sonnet", "--max-rounds", "2", "--", "x"])
        self.assertEqual(args.run_args, ["--workflow", "quick", "--model", "sonnet", "--max-rounds", "2", "--", "x"])

    def test_repeat_is_at_least_one(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            evals.parse_args(["--repeat", "0"])

    def test_the_engine_is_eval_engine_else_this_checkouts_orchestrator(self):
        with patch.dict(os.environ, {"EVAL_ENGINE": "target/debug/octoflow --flag 'a b'"}):
            self.assertEqual(evals.engine_command(), ["target/debug/octoflow", "--flag", "a b"])
        with patch.dict(os.environ, {"EVAL_ENGINE": ""}):
            self.assertEqual(evals.engine_command(), [sys.executable, os.path.join(evals.ROOT, "orchestrator.py")])


class TestMain(FakeEngineTest):
    def test_main_prints_the_table_and_appends_a_line_per_trial(self):
        self.plan(files=SOLUTIONS["feature"], metrics=metrics())
        results = os.path.join(self.dir, "results")
        out = io.StringIO()
        with patch.object(evals, "RESULTS_DIR", results), patch.object(evals, "engine_command", lambda: FAKE_ENGINE), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(evals.main(["--tasks", "feature", "--repeat", "2", "--", "--model", "sonnet"]), 0)
        self.assertRegex(out.getvalue(), r"(?m)^feature +2 +2/2 +2\.0 +0\.0 +0m0\ds +118$")
        (name,) = os.listdir(results)
        self.assertRegex(name, r"^\d{4}-\d\d-\d\d-model-sonnet\.jsonl$")
        with open(os.path.join(results, name)) as f:
            records = [json.loads(line) for line in f]
        self.assertEqual([(r["task"], r["repeat"], r["passed"], r["run_args"]) for r in records],
                         [("feature", 1, True, ["--model", "sonnet"]), ("feature", 2, True, ["--model", "sonnet"])])

    def test_an_unknown_task_exits_1_and_writes_nothing(self):
        results = os.path.join(self.dir, "results")
        err = io.StringIO()
        with patch.object(evals, "RESULTS_DIR", results), contextlib.redirect_stderr(err):
            self.assertEqual(evals.main(["--tasks", "nope"]), 1)
        self.assertIn("error: task nope", err.getvalue())
        self.assertFalse(os.path.exists(results))
