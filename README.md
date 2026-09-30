# ai-agents-orchestrator

A role-based handoff workflow for **Claude Code** sessions running in [herdr](https://herdr.dev):

```
Spec Collector ──spec.md──▶ Builder ──build-N.md──▶ Reviewer ──review-N.md──▶ APPROVE ──▶ pull request
                               ▲                                   │
                               └──────── CHANGES_REQUESTED ────────┘
```

Each role is a separate interactive Claude Code session in its own herdr pane, so no
role judges its own work, and you can watch or step into any of them. The agents can
run on this machine or on another machine saved in herdr.

Each feature is built on its own branch and ends as a pull request on GitHub, where you
review it. The run's herdr panes are closed once the pull request is open.

## Requirements

- herdr 0.9+ with the Claude integration installed where the agents run (`herdr integration install claude`)
- `claude` on `PATH` where the agents run
- Where the agents run: a git checkout with a remote named `origin` it can push to, and [`gh`](https://cli.github.com)
  logged in (`gh auth status`). Not needed with `--no-pr`.
- Python 3.13+, standard library only; [uv](https://github.com/astral-sh/uv) only for the tests
- For `--machine`: the machine saved in herdr and non-interactive SSH to its target; see
  [Running the agents on another machine](#running-the-agents-on-another-machine)

## Usage

```bash
# From a herdr pane, on the project in the current directory
python orchestrator.py run "add a token-bucket rate limiter to the API client"

# Agents on a machine saved in herdr, driven from anywhere; --cwd is a path on that machine
python orchestrator.py run "add a token-bucket rate limiter" --machine <machine> --cwd ~/GitHub/myproject

# Runs recorded for a project, and whether each one is still running
python orchestrator.py list --machine <machine> --cwd ~/GitHub/myproject

# Continue a run whose orchestrator has stopped, by its run id or the six-character key at its end
python orchestrator.py resume e292fb --machine <machine> --cwd ~/GitHub/myproject
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
| `--no-pr` | `run` only: leave the change uncommitted in the working tree and the workspace open, instead of opening a pull request. Works outside git. |
| `--force` | `resume` only: take over a run that still looks alive. |

A run saves its settings. `resume` takes the same flags as `run` except `--no-pr`, and a flag given to `resume`
overrides the saved value; one left out keeps it.

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
- A run stopped while opening its pull request picks up after the last step it finished: it does not commit
  twice or open a second pull request.
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
[ARCHITECTURE.md](ARCHITECTURE.md) gives an overview of the code.

### When the orchestrator needs you

A role's turn ends when it writes its handoff file. herdr's `idle` and `done` states don't mean the turn is
over: Claude Code ends a turn while a background task it started is still running, and resumes when that
task finishes. So the orchestrator polls for the file. Meanwhile, it sends a herdr notification when a role:

- is **blocked** on a permission prompt, a question, or a startup dialog such as folder trust;
- has sat **idle for 3 minutes** without writing its file (not for the Spec Collector, which waits on you by design).

Notifications appear in the herdr where the orchestrator runs. Answer in the named pane, and the run
continues.

### Running the agents on another machine

The agents can run on another machine that you reach over SSH and save in herdr. Steps 1–3 run on the
machine you start the orchestrator from; step 4 is about the remote machine.

1. **Save the machine** in herdr, under a label of your choice:

   ```bash
   herdr machine add --label <machine> <ssh-target>
   ```

2. **Check it** with `herdr machine list`. `--machine` matches the machine's label or its id, and the
   machine must be enabled; otherwise the run stops with an error.
3. **Check that SSH works non-interactively.** File and `git` access runs over `ssh -o BatchMode=yes`, so
   this must succeed without prompting for a password, passphrase or host key:

   ```bash
   ssh -o BatchMode=yes <ssh-target> true
   ```

4. **Prepare the remote machine.** It needs:
   - herdr 0.9+ with the Claude integration (`herdr integration install claude`)
   - `claude` on `PATH`
   - the git checkout of the project, with an `origin` it can push to
   - `gh` logged in (`gh auth status`), unless you use `--no-pr`

With `--machine`, every herdr command is forwarded with `herdr --machine`, and file and `git` access runs
over SSH to the target saved for that machine. The orchestrator itself can run on your local machine
(inside herdr or not), or on the remote machine directly: copy `orchestrator.py` there and run it with the
system `python3` (3.13+) from a herdr pane on the remote machine, without `--machine`.

## Running tests

```bash
uv run pytest
```
