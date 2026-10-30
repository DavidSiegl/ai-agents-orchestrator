"""Run the eval set: give each task under evals/tasks/ to the engine, judge what its agents made by a hidden test,
and print and keep the numbers. docs/design.md, Evals, says when to run it and what the numbers mean.

    uv run python evals/run.py [--tasks a,b] [--repeat N] [--config NAME] [--keep] [-- RUN_ARGS...]

Run it from a herdr pane, as `orchestrator run`: every trial is a real run, with real agents. RUN_ARGS, after
`--`, are given to the engine's `run` unchanged, such as `--workflow quick --model sonnet`; the runner sets
`--no-pr`, `--spec` and `--cwd` itself, and refuses those, `--machine` and `--worktree` there.

The engine is the command line `EVAL_ENGINE` gives, as tests/parity's PARITY_ENGINE does, else this checkout's own,
built first (`cargo build --locked -p octoflow-engine`), so that a trial never runs a binary older than the commit it
is recorded under. It is run as a program, through `run` and
`close`, and must keep a run's files as the orchestrator does: the runner finds the run under
`.orchestrator/runs/<run id>/` in the clone, and its numbers in `metrics.json` there (docs/design.md, Run metrics).

A task is a directory evals/tasks/<name>/ holding:

- task.md, the spec the run starts from, as `run --spec` takes one: a workflow whose first step writes the spec,
  such as `default`, skips its interview;
- repo.bundle, a git bundle of the toy repository at its starting commit, on main (evals/make_bundles.py);
- hidden_test.py, the acceptance test: run as `python -I -B hidden_test.py REPO`, from outside REPO, after the run;
  exit status 0 accepts the change. It may read REPO's origin/main, the starting commit, which the run never moves.

Exit status: 0 when every trial ran, whatever its tests said; 1 for an error of the runner's own, such as an unknown
task or a bundle that cannot be cloned; 2 for a usage error; 130 when interrupted, after the trials before.
"""

import argparse
import dataclasses
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_DIR = os.path.join(ROOT, "evals", "tasks")
# Where the result lines go, relative to the checkout's top, and there.
RESULTS_SUBDIR = os.path.join("evals", "results")
RESULTS_DIR = os.path.join(ROOT, RESULTS_SUBDIR)
# The version of a result line's shape (Eval.record), raised when a key changes its meaning or goes.
RECORD_VERSION = 1
# Where a run keeps its files in the project it runs on, one directory per run named by its run id.
RUNS_DIR = os.path.join(".orchestrator", "runs")
# Seconds a hidden test may take, and how many of the last lines of its output a result line keeps.
HIDDEN_TEST_TIMEOUT = 600
HIDDEN_TEST_TAIL_LINES = 15
# The engine's run flags the runner sets, or whose run would not leave its change in the clone, which it tests.
OWN_FLAGS = ("--no-pr", "--spec", "--cwd", "--machine", "--worktree")
EXIT_INTERRUPTED = 130


class EvalError(Exception):
    """A trial could not be set up or judged for a reason of the runner's own, not the agents': the message says
    what was being done and why it failed."""


@dataclasses.dataclass(frozen=True)
class Task:
    """An eval task: the directory evals/tasks/<name>/ holding its task.md, repo.bundle and hidden_test.py."""
    name: str
    dir: str

    @property
    def spec(self) -> str:
        """Its task.md."""
        return os.path.join(self.dir, "task.md")

    @property
    def bundle(self) -> str:
        """Its repo.bundle."""
        return os.path.join(self.dir, "repo.bundle")

    @property
    def hidden_test(self) -> str:
        """Its hidden_test.py."""
        return os.path.join(self.dir, "hidden_test.py")


@dataclasses.dataclass
class Trial:
    """What one run of one task came to. Each field that metrics.json gives is None when the run wrote none, as a
    run that failed or was interrupted does not."""
    task: str
    repeat: int  # which run of the task in this eval, from 1
    passed: bool  # whether the hidden test accepted what the run left in the clone
    engine_exit: int  # the exit status of the engine's run
    seconds: float  # wall time of the engine's run, measured by the runner
    hidden_test_output: str  # the last HIDDEN_TEST_TAIL_LINES lines of the hidden test's output
    run_id: str | None = None  # None when the engine made no run directory
    outcome: str | None = None  # metrics.json's: the verdict, "error" or "unfinished"
    rounds: int | None = None
    quality_rounds: int | None = None  # how many quality builds judged the change, over every round
    # The four token counts (input, output, cache_read, cache_write) summed over every agent turn; None also when a
    # single turn's are unknown, so that a sum never stands for less than the whole run.
    tokens: dict[str, int] | None = None


def load_tasks(names: list[str] | None, tasks_dir: str = TASKS_DIR) -> list[Task]:
    """The tasks named, in that order, or every task in tasks_dir in name order when names is None. Raises
    EvalError for a name with no task directory, or one missing a file a task needs."""
    if names is None:
        try:
            names = sorted(e for e in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, e)))
        except OSError as e:
            raise EvalError(f"could not list the tasks in {tasks_dir}: {e}") from e
    tasks = [Task(name, os.path.join(tasks_dir, name)) for name in names]
    for task in tasks:
        missing = [os.path.basename(p) for p in (task.spec, task.bundle, task.hidden_test) if not os.path.isfile(p)]
        if missing:
            raise EvalError(f"task {task.name}: {', '.join(missing)} missing in {task.dir}")
    return tasks


# How a result line names the engine a trial ran without EVAL_ENGINE: this checkout's own build.
DEFAULT_ENGINE = "this checkout's build"


def engine_command() -> list[str]:
    """The engine every trial runs, as EVAL_ENGINE gives it, else this checkout's own, as built_engine builds it."""
    if command := os.environ.get("EVAL_ENGINE"):
        return shlex.split(command)
    return [built_engine()]


def built_engine() -> str:
    """The path of this checkout's orchestrator binary, `cargo build --locked -p octoflow-engine --bin orchestrator`
    run first, wherever CARGO_TARGET_DIR puts it. Raises EvalError when cargo is missing, fails, or reports no such
    binary."""
    cargo = shutil.which("cargo")
    if cargo is None:
        raise EvalError("cargo, which builds the engine, is not on PATH; set EVAL_ENGINE to run another")
    built = subprocess.run([cargo, "build", "--locked", "--quiet", "-p", "octoflow-engine", "--bin", "orchestrator",  # noqa: S603
                            "--message-format=json"], cwd=ROOT, capture_output=True, text=True, check=False)
    if built.returncode != 0:
        raise EvalError(f"cargo could not build the engine: {built.stderr.strip()}")
    paths = [m["executable"] for m in map(json.loads, built.stdout.splitlines())
             if m.get("reason") == "compiler-artifact" and m.get("executable")]
    if len(paths) != 1:
        raise EvalError(f"cargo reported {len(paths)} binaries for the engine, not one")
    return paths[0]


def config_name(run_args: list[str]) -> str:
    """A name for the configuration run_args give, for the results file's: "default" for none, else the arguments'
    letters and digits, lowercased, in runs joined by "-", at most 60 characters."""
    words = re.findall(r"[a-z0-9]+", " ".join(run_args).lower())
    return "-".join(words)[:60].strip("-") or "default"


def run_trial(task: Task, repeat: int, engine: list[str], run_args: list[str], keep: bool = False) -> Trial:
    """Run task once: clone its bundle into a directory of its own under the temporary directory, have the engine
    run its spec there with run_args and without a pull request, close the run's workspace, run the hidden test on
    what the run left, and remove the directory unless keep, saying where it is when kept.

    Neither the hidden test nor the task's directory is reachable from the clone: the spec is a copy beside it, and
    its origin a copy of the bundle. The engine's and the hidden test's output go to this process's standard error
    and into the result, respectively. Raises EvalError when the bundle cannot be cloned or the hidden test cannot be
    started; KeyboardInterrupt, once the engine has stopped, when interrupted, leaving the directory for a resume.
    """
    work = tempfile.mkdtemp(prefix=f"octoflow-eval-{task.name}-")
    repo = os.path.join(work, "repo")
    interrupted = False
    try:
        spec = _isolated_inputs(task, work, repo)
        started = time.monotonic()
        engine_exit = _run_engine([*engine, "run", "--no-pr", "--spec", spec, "--cwd", repo, *run_args])
        seconds = round(time.monotonic() - started, 1)
        known = _run_metrics(repo)
        if known["run_id"]:
            _close(engine, known["run_id"], repo)
        else:
            print(f"warning: the engine left no run directory under {RUNS_DIR} in {repo}", file=sys.stderr)
        passed, output = _run_hidden_test(task, repo, work)
        return Trial(task.name, repeat, passed, engine_exit, seconds, output, **known)
    except KeyboardInterrupt:
        interrupted = True
        print(f"interrupted: the run's clone stays in {repo}", file=sys.stderr)
        raise
    finally:
        if keep:
            print(f"{task.name} #{repeat}: kept in {work}", file=sys.stderr)
        elif not interrupted:
            shutil.rmtree(work, ignore_errors=True)


def _isolated_inputs(task: Task, work: str, repo: str) -> str:
    """Clone task's bundle into repo, its origin a copy of the bundle in work, and copy its spec into work; return
    the copy's path. Raises EvalError when either fails."""
    bundle = os.path.join(work, "origin.bundle")
    spec = os.path.join(work, "task.md")
    try:
        shutil.copyfile(task.bundle, bundle)
        shutil.copyfile(task.spec, spec)
        proc = _subprocess(["git", "clone", "--quiet", bundle, repo])
    except OSError as e:
        raise EvalError(f"task {task.name}: could not set up its clone in {work}: {e}") from e
    if proc.returncode != 0:
        raise EvalError(f"task {task.name}: could not clone {task.bundle}: {proc.stderr.strip()}")
    return spec


def _subprocess(argv: list[str], cwd: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    """argv run in cwd, its output captured as text, whatever its exit status; OSError when it cannot start and
    TimeoutExpired, after killing it, when it runs over timeout seconds."""
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,  # noqa: S603
                          check=False)


def _run_engine(argv: list[str]) -> int:
    """The exit status of argv, run with its output on this process's standard error. An interrupt reaches
    the engine too, in the same process group, so it is waited for before the KeyboardInterrupt goes on: it saves
    its run's state as it stops."""
    try:
        # Standard output stays the table's: the engine's goes where its log does, to file descriptor 2.
        proc = subprocess.Popen(argv, stdout=2)  # noqa: S603 - the engine EVAL_ENGINE names
    except OSError as e:
        raise EvalError(f"could not start the engine, {shlex.join(argv[:2])}: {e}") from e
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.wait()
        raise


def _run_hidden_test(task: Task, repo: str, work: str) -> tuple[bool, str]:
    """Whether task's hidden test accepts repo, and the last lines of its output; run from work, outside repo.
    A hidden test that runs over HIDDEN_TEST_TIMEOUT fails. Raises EvalError when it cannot be started."""
    try:
        proc = _subprocess([sys.executable, "-I", "-B", task.hidden_test, repo], cwd=work, timeout=HIDDEN_TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"the hidden test ran over {HIDDEN_TEST_TIMEOUT}s"
    except OSError as e:
        raise EvalError(f"task {task.name}: could not run its hidden test: {e}") from e
    lines = (proc.stdout + proc.stderr).rstrip("\n").split("\n")
    return proc.returncode == 0, "\n".join(lines[-HIDDEN_TEST_TAIL_LINES:])


def _run_metrics(repo: str) -> dict:
    """The Trial fields a run in repo gives: its run id, the name of the one run directory there, and what its
    metrics.json says (docs/design.md, Run metrics); None for each that a missing or unreadable directory or file
    leaves unknown."""
    known: dict = {"run_id": None, "outcome": None, "rounds": None, "quality_rounds": None, "tokens": None}
    try:
        run_ids = sorted(os.listdir(os.path.join(repo, RUNS_DIR)))
    except OSError:
        return known
    if not run_ids:
        return known
    known["run_id"] = run_ids[-1]
    try:
        with open(os.path.join(repo, RUNS_DIR, known["run_id"], "metrics.json"), encoding="utf-8") as f:
            metrics = json.load(f)
    except (OSError, ValueError):
        return known
    known["outcome"], known["rounds"] = metrics.get("outcome"), metrics.get("rounds")
    known["quality_rounds"] = len(metrics.get("quality_rounds") or [])
    counts = [t["tokens"] for t in metrics.get("turns", []) if t.get("harness")]
    if counts and all(isinstance(c, dict) for c in counts):
        known["tokens"] = {k: sum(c.get(k, 0) for c in counts) for k in ("input", "output", "cache_read", "cache_write")}
    return known


def _close(engine: list[str], run_id: str, repo: str) -> None:
    """Close the run's herdr workspace, which a --no-pr run leaves open; a failure is only reported."""
    try:
        proc = _subprocess([*engine, "close", run_id, "--cwd", repo])
    except OSError as e:
        print(f"warning: could not close run {run_id}: {e}", file=sys.stderr)
        return
    if proc.returncode != 0:
        print(f"warning: could not close run {run_id}: {proc.stderr.strip()}", file=sys.stderr)


@dataclasses.dataclass(frozen=True)
class Eval:
    """One invocation of the runner: what its result lines share, and the file they go to."""
    date: str  # the day it started, YYYY-MM-DD
    config: str  # the name of its configuration, config_name's or --config's
    run_args: list[str]  # what the engine's run was given beyond the runner's own flags
    engine: str  # EVAL_ENGINE, or DEFAULT_ENGINE for this checkout's
    commit: str  # this checkout's HEAD, "-dirty" after it with tracked changes: what the default engine ran with

    @property
    def results_path(self) -> str:
        """evals/results/<date>-<config>.jsonl, where its result lines go."""
        return os.path.join(RESULTS_DIR, f"{self.date}-{self.config}.jsonl")

    def record(self, trial: Trial) -> dict:
        """The result line of trial: this eval's fields, then trial's. docs/design.md, Evals, gives every key."""
        return {"version": RECORD_VERSION, **dataclasses.asdict(self), **dataclasses.asdict(trial)}


def append_records(path: str, records: list[dict]) -> None:
    """Append records to the JSON-lines file at path, one line each, making its directory; OSError if it cannot."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)


def table(trials: list[Trial]) -> str:
    """A table of trials, a row per task in the order they first appear and one for them all: the runs, how many
    passed, and the means of rounds, quality rounds, wall time and tokens (all four counts summed), each over the
    trials that know it, "?" when none does; a mean over fewer than all the row's runs says over how many."""
    groups: dict[str, list[Trial]] = {}
    for t in trials:
        groups.setdefault(t.task, []).append(t)
    if len(groups) > 1:
        groups["all"] = trials
    head = ["task", "runs", "passed", "rounds", "quality rounds", "wall time", "tokens"]
    rows = [head] + [[name, str(len(ts)), f"{sum(t.passed for t in ts)}/{len(ts)}",
                      _mean([t.rounds for t in ts], _tenths), _mean([t.quality_rounds for t in ts], _tenths),
                      _mean([t.seconds for t in ts], _duration),
                      _mean([sum(t.tokens.values()) if t.tokens else None for t in ts], _count)]
                     for name, ts in groups.items()]
    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    return "".join("  ".join(cell.ljust(w) if i == 0 else cell.rjust(w)
                             for i, (cell, w) in enumerate(zip(row, widths, strict=True))).rstrip() + "\n"
                   for row in rows)


def _mean(values: list[float | None], show: Callable[[float], str]) -> str:
    """The mean of the values that are not None, as show writes it; "?" when all are None, and " (k/n)" after it
    when only k of the n are known."""
    known = [v for v in values if v is not None]
    if not known:
        return "?"
    text = show(sum(known) / len(known))
    return text if len(known) == len(values) else f"{text} ({len(known)}/{len(values)})"


def _tenths(x: float) -> str:
    return f"{x:.1f}"


def _duration(seconds: float) -> str:
    minutes, s = divmod(round(seconds), 60)
    return f"{minutes}m{s:02d}s"


def _count(n: float) -> str:
    for size, unit in ((1e6, "M"), (1e3, "k")):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


def _head_commit() -> str:
    """This checkout's HEAD, the prompts and workflows the default engine runs with, followed by "-dirty" when a file
    git tracks differs from it, so that numbers of an uncommitted edit are not credited to the commit without it;
    "unknown" when git cannot say."""
    try:
        head = _subprocess(["git", "-C", ROOT, "rev-parse", "HEAD"])
        # Not the results files, which an eval of earlier the same day appended to.
        changed = _subprocess(["git", "-C", ROOT, "diff", "--quiet", "HEAD", "--", ".",
                               f":(exclude){RESULTS_SUBDIR}"])
    except OSError:
        return "unknown"
    if head.returncode != 0 or changed.returncode not in (0, 1):
        return "unknown"
    return head.stdout.strip() + ("-dirty" if changed.returncode == 1 else "")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """The runner's arguments; RUN_ARGS, those after `--`, as run_args. Exits with status 2 for a usage error."""
    parser = argparse.ArgumentParser(
        prog="evals/run.py", usage="%(prog)s [--tasks a,b] [--repeat N] [--config NAME] [--keep] [-- RUN_ARGS...]",
        description="Run the eval set's tasks through the engine and judge each by its hidden test.")
    parser.add_argument("--tasks", help="the tasks to run, by name, comma-separated; default: every task")
    parser.add_argument("--repeat", type=int, default=1, help="runs of each task (default 1)")
    parser.add_argument("--config", help="the name of the results file's configuration; default: from RUN_ARGS")
    parser.add_argument("--keep", action="store_true", help="keep each trial's clone, and say where")
    own, run_args = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    args = parser.parse_args(own)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    # By prefix too: the engine's argparse takes --workt for --worktree.
    if refused := [a for a in run_args if _abbreviates_own_flag(a.split("=", 1)[0])]:
        parser.error(f"RUN_ARGS cannot hold {', '.join(refused)}: the runner sets or refuses {', '.join(OWN_FLAGS)}")
    args.run_args = run_args
    return args


def _abbreviates_own_flag(arg: str) -> bool:
    """Whether arg is one of OWN_FLAGS, or the start of one that argparse would take for it."""
    return arg.startswith("--") and len(arg) > 2 and any(flag.startswith(arg) for flag in OWN_FLAGS)


def main(argv: list[str] | None = None) -> int:
    """Run the eval set as the module docstring says, print its table, and append its result lines to
    evals/results/<date>-<config>.jsonl as each trial ends; the exit status is the module docstring's."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    evaluation = Eval(datetime.date.today().isoformat(), args.config or config_name(args.run_args), args.run_args,
                      os.environ.get("EVAL_ENGINE") or DEFAULT_ENGINE, _head_commit())
    trials: list[Trial] = []
    exit_code = 0
    try:
        engine = engine_command()
        tasks = load_tasks(args.tasks.split(",") if args.tasks else None)
        for task in tasks:
            for repeat in range(1, args.repeat + 1):
                trial = run_trial(task, repeat, engine, args.run_args, keep=args.keep)
                trials.append(trial)
                append_records(evaluation.results_path, [evaluation.record(trial)])
    except (EvalError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        exit_code = EXIT_INTERRUPTED
    if trials:
        print(table(trials), end="")
        print(f"results appended to {os.path.relpath(evaluation.results_path)}")
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
