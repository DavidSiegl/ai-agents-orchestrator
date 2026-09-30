# ai-agents-orchestrator

A role-based handoff workflow for **Claude Code** sessions running in [herdr](https://herdr.dev):

```
Spec Collector ──spec.md──▶ Builder ──build-N.md──▶ Reviewer ──review-N.md──▶ APPROVE
                               ▲                                   │
                               └──────── CHANGES_REQUESTED ────────┘
```

Each role is a separate interactive Claude Code session in its own herdr pane, so no
role judges its own work, and you can watch or step into any of them. The agents can
run on this machine or on a saved herdr machine such as `slave0`.

## Requirements

- herdr 0.9+ with the Claude integration installed where the agents run (`herdr integration install claude`)
- `claude` on `PATH` where the agents run
- Python 3.13+, standard library only; [uv](https://github.com/astral-sh/uv) only for the tests
- For `--machine`: the machine saved in herdr (`herdr machine list`) and non-interactive SSH to its target

## Usage

```bash
# From a herdr pane, on the project in the current directory
python orchestrator.py run "add a token-bucket rate limiter to the API client"

# Agents on slave0, driven from anywhere; --cwd is a path on slave0
python orchestrator.py run "add a token-bucket rate limiter" --machine slave0 --cwd ~/GitHub/myproject

# Runs recorded for a project, and whether each one is still running
python orchestrator.py list --machine slave0 --cwd ~/GitHub/myproject

# Continue a run whose orchestrator has stopped, by its run id or the six-character key at its end
python orchestrator.py resume e292fb --machine slave0 --cwd ~/GitHub/myproject
```

| Flag | Description |
|---|---|
| `--machine NAME` | Saved herdr machine to run the agents on. Requires `--cwd`. |
| `--cwd PATH` | Project directory, on the machine if `--machine` is given. Default: current directory. |
| `--max-rounds N` | Review rounds before giving up (default 3). |
| `--timeout SECONDS` | How long one Builder or Reviewer turn may take (default 1800). The interview has no limit. |
| `--permission-mode MODE` | Claude Code permission mode for every role, e.g. `auto` or `acceptEdits`. |
| `--model MODEL` | Claude model for every role, e.g. `sonnet` or `claude-opus-5-5`. Passed to `claude` unchecked. Default: Claude Code's own. |
| `--spec-model MODEL` | Claude model for the Spec Collector. Overrides `--model`. |
| `--build-model MODEL` | Claude model for the Builder. Overrides `--model`. |
| `--review-model MODEL` | Claude model for the Reviewer. Overrides `--model`. |
| `--force` | `resume` only: take over a run that still looks alive. |

A run saves its settings. `resume` takes the same flags as `run`, and a flag given to `resume` overrides the saved
value; one left out keeps it.

Exit status: `0` approved, `3` changes still requested after the last round, `1` error, `130` interrupted.

## How a run works

1. **Workspace.** The run gets its own herdr workspace. Its panes are named after the roles.
2. **Spec Collector.** The collector's pane is focused and a notification tells you it is waiting. Answer its
   questions in that pane. Once you approve the spec, it writes `spec.md`, which hands the work on.
3. **Builder.** It implements the spec in a pane split to the right, verifies the change, and writes
   `build-N.md`. It does not commit.
4. **Reviewer.** A fresh session in a pane below the Builder checks the change (`git diff` against the commit
   the run started from, plus untracked files) against the spec, and writes `review-N.md`. The first line of
   the review is `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Requested changes go back to the Builder,
   and the Reviewer checks again.

Everything a run writes stays in `<project>/.orchestrator/runs/<run-id>/`: the handoff files and
`state.json`, which records the phase, round, panes, verdict and any error. `.orchestrator/` ignores itself
through its own `.gitignore`, so it never shows up in the diff under review. The workspace is left open when
the run ends, so you can read the sessions; close it in herdr when you are done.

### Resuming a run

The agents and the handoff files outlive the orchestrator: Ctrl-C, a dropped SSH session or a crash stops only
the process that drives them. `resume` picks the run up where `state.json` says it stopped:

- A handoff file written while no orchestrator was watching is taken as it is, without prompting the role again.
- A role whose agent still runs is reused. If it was already prompted for its current file, it is only waited on.
- An agent that exited is relaunched in its pane with `claude --resume`, keeping its conversation. When its
  session is gone, a fresh one starts with a prompt that points it at the earlier rounds, and a Spec Collector's
  interview starts over.
- A closed pane is split again from a surviving one, and a closed workspace is replaced by a new one.
- The diff under review stays against the commit the run started from, even if you commit in between.
- An empty handoff file or a review without a verdict stops the resume with the file's name: fix it, or delete it
  to have the role write it again.

Resuming a finished run does nothing but print its verdict.

### Stale runs

While a run is going, its orchestrator rewrites `state.json` at least once a minute and records its host and
pid there. `list` shows each unfinished run as `running` or `stale`. A run is stale when `state.json` is more
than five minutes old, measured by the clock of the machine that holds it, or when its orchestrator ran on this
host and its pid is gone. A stale run's line ends with the `resume` command for it. `resume` refuses a run
that is still running unless you pass `--force`, and an orchestrator whose run is taken over stops at its next
save. Ctrl-C is recorded as the error `interrupted`.

[ROADMAP.md](ROADMAP.md) has the design behind both, and what else is planned.

### When the orchestrator needs you

A role's turn ends when it writes its handoff file. herdr's `idle` and `done` states don't mean the turn is
over: Claude Code ends a turn while a background task it started is still running, and resumes when that
task finishes. So the orchestrator polls for the file. Meanwhile, it sends a herdr notification when a role:

- is **blocked** on a permission prompt, a question, or a startup dialog such as folder trust;
- has sat **idle for 3 minutes** without writing its file (not for the Spec Collector, which waits on you by design).

Notifications appear in the herdr where the orchestrator runs. Answer in the named pane, and the run
continues.

### Running on slave0

With `--machine`, every herdr command is forwarded with `herdr --machine`, and file and `git` access runs
over SSH to the target saved for that machine. The orchestrator itself can run here (inside herdr or not),
or on slave0 directly: copy `orchestrator.py` over and run it with the system `python3` from a herdr pane
there, without `--machine`.

## Running tests

```bash
uv run pytest
```
