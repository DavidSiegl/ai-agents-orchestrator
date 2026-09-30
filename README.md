# ai-agents-orchestrator

A role-based handoff workflow for **Claude Code** sessions running in [herdr](https://herdr.dev):

```
Spec Collector ──spec.md──▶ Builder ──build-N.md──▶ Reviewer ──review-N.md──▶ APPROVE ──▶ pull request
                               ▲                                   │
                               └──────── CHANGES_REQUESTED ────────┘
```

Each role is a separate interactive Claude Code session in its own herdr pane, so no
role judges its own work, and you can watch or step into any of them. The agents can
run on this machine or on a saved herdr machine such as `slave0`.

Each feature is built on its own branch and ends as a pull request on GitHub, where you
review it. The run's herdr panes are closed once the pull request is open.

## Requirements

- herdr 0.9+ with the Claude integration installed where the agents run (`herdr integration install claude`)
- `claude` on `PATH` where the agents run
- Where the agents run: a git checkout with a remote named `origin` it can push to, and [`gh`](https://cli.github.com)
  logged in (`gh auth status`). Not needed with `--no-pr`.
- Python 3.13+, standard library only; [uv](https://github.com/astral-sh/uv) only for the tests
- For `--machine`: the machine saved in herdr (`herdr machine list`) and non-interactive SSH to its target

## Usage

```bash
# From a herdr pane, on the project in the current directory
python orchestrator.py run "add a token-bucket rate limiter to the API client"

# Agents on slave0, driven from anywhere; --cwd is a path on slave0
python orchestrator.py run "add a token-bucket rate limiter" --machine slave0 --cwd ~/GitHub/myproject

# Runs recorded for a project
python orchestrator.py list --machine slave0 --cwd ~/GitHub/myproject
```

| Flag | Description |
|---|---|
| `--machine NAME` | Saved herdr machine to run the agents on. Requires `--cwd`. |
| `--cwd PATH` | Project directory, on the machine if `--machine` is given. Default: current directory. |
| `--max-rounds N` | Review rounds before giving up (default 3). |
| `--timeout SECONDS` | How long one Builder or Reviewer turn may take (default 1800). The interview has no limit. |
| `--permission-mode MODE` | Claude Code permission mode for every role, e.g. `auto` or `acceptEdits`. |
| `--no-pr` | Leave the change uncommitted in the working tree and the workspace open, instead of opening a pull request. Works outside git. |

Exit status: `0` approved, `3` changes still requested after the last round (the pull request is a draft), `1` error, `130` interrupted.

## How a run works

1. **Workspace.** The run gets its own herdr workspace. Its panes are named after the roles. The project must
   be on a branch (not a detached HEAD) with a clean working tree; that branch is what the pull request targets.
2. **Spec Collector.** The collector's pane is focused and a notification tells you it is waiting. Answer its
   questions in that pane. Once you approve the spec, it writes `spec.md`, which hands the work on.
3. **Builder.** The orchestrator creates the branch `orchestrator/<spec title>-<id>` from the current commit.
   The Builder implements the spec on it in a pane split to the right, verifies the change, and writes
   `build-N.md`. It does not commit.
4. **Reviewer.** A fresh session in a pane below the Builder checks the change (`git diff` against the commit
   the run started from, plus untracked files) against the spec, and writes `review-N.md`. The first line of
   the review is `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Requested changes go back to the Builder,
   and the Reviewer checks again.
5. **Pull request.** The orchestrator commits the change as one commit, titled with the spec's `#` heading,
   pushes the branch to `origin`, and opens a pull request with `gh`. Its description holds the spec, the
   last build report and the last review. If the Reviewer still requests changes after the last round, the
   pull request is a draft. The project is switched back to the branch the run started on, and the run's
   herdr workspace is closed.

Everything a run writes stays in `<project>/.orchestrator/runs/<run-id>/`: the handoff files and
`state.json`, which records the phase, round, panes, branch, pull request, verdict and any error.
`.orchestrator/` ignores itself through its own `.gitignore`, so it never shows up in the diff under review
or in the commit. A run that fails keeps its workspace open and stays on its branch, so you can see what
happened; close the workspace in herdr when you are done.

A run cannot yet be resumed once its orchestrator process is gone; [ROADMAP.md](ROADMAP.md) has the design
for that and what else is planned.

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
