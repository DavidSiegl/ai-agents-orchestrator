# ai-agents-orchestrator

A role-based handoff workflow for **Claude Code** sessions running in [herdr](https://herdr.dev):

```
Spec Collector ──spec.md──▶ Builder ──build-N.md──▶ Reviewer ──review-N.md──▶ APPROVE ──▶ pull request
                               ▲                                   │
                               └──────── CHANGES_REQUESTED ────────┘
```

Each role is a separate interactive Claude Code session in its own herdr pane, so no role judges its own
work, and you can watch or step into any of them. The agents run on this machine or on another one saved in
herdr. Each feature is built on its own branch and ends as a pull request on GitHub, where you review it.

## Requirements

- herdr 0.9+ with the Claude integration installed where the agents run (`herdr integration install claude`)
- `claude` on `PATH` where the agents run
- Where the agents run, unless you use `--no-pr`: a git checkout with an `origin` it can push to, and
  [`gh`](https://cli.github.com) logged in (`gh auth status`)
- Python 3.13+, standard library only; [uv](https://github.com/astral-sh/uv) only for the tests
- For `--machine`: the machine saved in herdr and non-interactive SSH to its target; see
  [Running the agents on another machine](docs/design.md#running-the-agents-on-another-machine)

## Usage

```bash
# From a herdr pane, on the project in the current directory
python orchestrator.py run "add a token-bucket rate limiter to the API client"

# Agents on a machine saved in herdr, driven from anywhere; --cwd is a path on that machine
python orchestrator.py run "add a token-bucket rate limiter" --machine <machine> --cwd ~/GitHub/myproject

# Runs recorded for a project, and whether each one is still running
python orchestrator.py list --machine <machine> --cwd ~/GitHub/myproject

# Continue a stopped run, by its run id or the six-character key at its end
python orchestrator.py resume e292fb --machine <machine> --cwd ~/GitHub/myproject
```

| Flag | Description |
|---|---|
| `--machine NAME` | Saved herdr machine to run the agents on. Requires `--cwd`. |
| `--cwd PATH` | Project directory, on the machine if `--machine` is given. Default: current directory. |
| `--max-rounds N` | Review rounds before giving up (default 3). |
| `--timeout SECONDS` | How long one Builder or Reviewer turn may take (default 1800). The interview has no limit. |
| `--permission-mode MODE` | Claude Code permission mode for every role, e.g. `auto` or `acceptEdits`. |
| `--model MODEL` | Claude model for every role, e.g. `sonnet`. Default: Claude Code's own. |
| `--spec-model MODEL` | Claude model for the Spec Collector. Overrides `--model`. |
| `--build-model MODEL` | Claude model for the Builder. Overrides `--model`. |
| `--review-model MODEL` | Claude model for the Reviewer. Overrides `--model`. |
| `--no-pr` | `run` only: leave the change uncommitted and the workspace open instead of opening a pull request. Works outside git. |
| `--force` | `resume` only: take over a run that still looks alive. |

A run saves its settings. `resume` takes the same flags as `run` except `--no-pr`, and a flag given to `resume`
overrides the saved value; one left out keeps it.

Exit status: `0` approved, `3` changes still requested after the last round (the pull request is a draft), `4`
approved but the pull request conflicts with its base branch (it is a draft), `1` error, `130` interrupted.

## How a run works

1. **Workspace.** The run gets its own herdr workspace, with a pane per role. The project must be on a branch
   with a clean working tree; the pull request targets that branch. That branch is fetched from `origin` and
   fast-forwarded, so the Spec Collector reads current code. If that fails, for example offline or because the
   local branch has diverged from `origin`'s, the run stops before the interview.
2. **Spec Collector.** A notification tells you it is waiting. Answer its questions in its pane; once you
   approve the spec, it writes `spec.md`.
3. **Builder.** The orchestrator fetches and fast-forwards the base branch again, since the interview can take
   hours, and creates the branch `orchestrator/<spec title>-<id>` from it. The Builder implements the spec
   there, verifies it, and writes `build-N.md` without committing.
4. **Reviewer.** A fresh session checks the change against the spec and writes `review-N.md`, which starts with
   `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`; requested changes go back to the Builder.
5. **Pull request.** The change is committed as one commit. If the base branch on `origin` has moved on since,
   it is merged in with a merge commit. The branch is pushed to `origin` and opened as a pull request with `gh`,
   a draft if changes were still requested after the last round. If the merge conflicts, it is aborted, the
   branch is pushed without it, and the pull request is a draft whose description starts with a warning listing
   the conflicting files; you resolve them. The project goes back to its starting branch, and the workspace is
   closed.

Everything a run writes stays in `<project>/.orchestrator/runs/<run-id>/`: the handoff files and `state.json`,
which records the phase, round, panes, branch, pull request, verdict and any error. `.orchestrator/` ignores
itself, so it never shows up in the diff or the commit. A failed run keeps its workspace open and stays on its
branch; close the workspace in herdr when done.

`resume` continues a run whose orchestrator stopped where `state.json` says, reusing or relaunching its agents
([Resuming a run](docs/design.md#resuming-a-run)). `list` marks a run `stale` after five minutes without a
heartbeat ([Stale runs](docs/design.md#stale-runs)). You get a herdr notification when a role is blocked or idle
for 3 minutes ([When the orchestrator needs you](docs/design.md#when-the-orchestrator-needs-you)).
See also [design decisions](docs/design.md), [architecture](docs/architecture.md), [roadmap](docs/roadmap.md)
and [quality gate](docs/quality-gate.md).

## Running tests

```bash
uv run pytest
```

[TESTING.md](TESTING.md) plans the testing methods beyond these unit tests, and the order to adopt them in.
