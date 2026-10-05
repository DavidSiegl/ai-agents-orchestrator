# ai-agents-orchestrator

A role-based handoff workflow for **Claude Code** sessions running in [herdr](https://herdr.dev):

```
Spec Collector ──spec.md──▶ Builder ──build-N.md──▶ Reviewer ──review-N.md──▶ APPROVE ──▶ pull request
                               ▲                                   │
                               └──────── CHANGES_REQUESTED ────────┘
```

With `--quality-gate`, a SonarQube analysis through Jenkins sits between the Builder and the Reviewer, and
sends its findings back to the Builder first.

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
- For `--quality-gate`: a Jenkins quality job and SonarQube set up as in [quality gate](docs/quality-gate.md),
  their credentials in `~/.config/ai-agents-orchestrator/ci.env` or the environment, and a git checkout with an
  `origin`

## Install

Each [release](https://github.com/DavidSiegl/ai-agents-orchestrator/releases) has `orchestrator.pyz`, an
executable of `orchestrator.py` that needs only Python 3.13+:

```bash
curl -fLO https://github.com/DavidSiegl/ai-agents-orchestrator/releases/latest/download/orchestrator.pyz
curl -fLO https://github.com/DavidSiegl/ai-agents-orchestrator/releases/latest/download/orchestrator.pyz.sha256
sha256sum -c orchestrator.pyz.sha256 && chmod +x orchestrator.pyz
./orchestrator.pyz run "add a token-bucket rate limiter to the API client"
```

It takes the same commands and flags as `python orchestrator.py` in a checkout, which the examples below use.

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

# With the SonarQube quality gate after each Builder turn; credentials from ~/.config/ai-agents-orchestrator/ci.env
python orchestrator.py run --quality-gate AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality "add a token-bucket rate limiter"
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
| `--workflow NAME` | `run` only: the workflow the run goes through (default `default`, the run described below). `--help` lists the choices; a resumed run keeps its workflow. A role of another workflow without its own `--ROLE-model` flag gets `--model`. |
| `--no-pr` | `run` only: leave the change uncommitted and the workspace open instead of opening a pull request. Works outside git. |
| `--quality-gate JOB` | `run` only: after each Builder turn, analyse the change with this Jenkins job, by its full name with folders, and SonarQube, and send the findings back to the Builder before the Reviewer. Needs `JENKINS_URL`, `JENKINS_USER`, `JENKINS_TOKEN`, `SONAR_HOST_URL` and `SONAR_TOKEN`, from the environment or `~/.config/ai-agents-orchestrator/ci.env`. |
| `--max-quality-rounds N` | With the gate: SonarQube analyses per review round before the Reviewer gets the change anyway (default 3). |
| `--force` | `resume` only: take over a run that still looks alive. |

A run saves its settings. `resume` takes the same flags as `run` except `--no-pr`, `--quality-gate` and
`--workflow`, and a flag given to `resume` overrides the saved value; one left out keeps it.

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
4. **Quality gate**, with `--quality-gate`. The orchestrator snapshots the working tree, pushes it to a
   throwaway branch `orchestrator-ci/<id>-N-qQ`, and has the Jenkins job analyse it into the run's own
   SonarQube project, after analysing the base once per run. It writes `quality-N-Q.md`, which starts with
   `GATE: OK`, `GATE: ERROR` or `GATE: BUILD_FAILED` and lists the issues on the lines the change added. If the
   gate does not pass, the Builder answers it in `build-N-qQ.md` and the change is analysed again, up to
   `--max-quality-rounds` times; then it goes to the Reviewer either way, who is told whether the gate passed.
   The throwaway branches and the project are deleted when the run finishes; see
   [quality gate](docs/quality-gate.md).
5. **Reviewer.** A fresh session checks the change against the spec and writes `review-N.md`, which starts with
   `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`; requested changes go back to the Builder.
6. **Pull request.** The change is committed as one commit. If the base branch on `origin` has moved on since,
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
