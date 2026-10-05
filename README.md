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

# Another workflow: one of yours by name, or a workflow file anywhere; `workflows` lists them
python orchestrator.py run --workflow tdd "add a token-bucket rate limiter"
python orchestrator.py run --workflow-file examples/workflows/quick.toml "fix the off-by-one in the pager"

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
| `--model MODEL` | Claude model for every role, e.g. `sonnet`. Default: the workflow file's model for the role, else Claude Code's own. |
| `--spec-model MODEL` | Claude model for the Spec Collector. Overrides `--model`. |
| `--build-model MODEL` | Claude model for the Builder. Overrides `--model`. |
| `--review-model MODEL` | Claude model for the Reviewer. Overrides `--model`. |
| `--workflow NAME` | `run` only: the workflow the run goes through: `default`, the run described below, or one of [your own](#your-own-workflows) in `~/.config/ai-agents-orchestrator/workflows/`. `--help` lists the choices; a resumed run keeps its workflow. |
| `--workflow-file FILE` | `run` only: like `--workflow`, for the workflow file at `FILE`, such as one kept in the project. |
| `--role-model ROLE=MODEL` | Claude model for one role of the workflow, by its key, e.g. `tests=sonnet`. Overrides `--model`; repeatable. |
| `--no-pr` | `run` only: leave the change uncommitted and the workspace open instead of opening a pull request. Works outside git. |
| `--quality-gate JOB` | `run` only: after each Builder turn, analyse the change with this Jenkins job, by its full name with folders, and SonarQube, and send the findings back to the Builder before the Reviewer. Needs `JENKINS_URL`, `JENKINS_USER`, `JENKINS_TOKEN`, `SONAR_HOST_URL` and `SONAR_TOKEN`, from the environment or `~/.config/ai-agents-orchestrator/ci.env`. |
| `--max-quality-rounds N` | With the gate: SonarQube analyses per review round before the Reviewer gets the change anyway (default 3). |
| `--force` | `resume` only: take over a run that still looks alive. |

A run saves its settings. `resume` takes the same flags as `run` except `--no-pr`, `--quality-gate`,
`--workflow` and `--workflow-file`, and a flag given to `resume` overrides the saved value; one left out keeps it.

Exit status: `0` approved, or `FINISHED` for a workflow without a verdict step; `3` changes still requested after
the last round (the pull request is a draft); `4` approved or finished, but the pull request conflicts with its base
branch (it is a draft); `1` error; `130` interrupted.

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

## Your own workflows

The roles, their prompts and their order are a workflow. Besides the built-in `default`, you can add your own
without touching the code: a TOML file in `~/.config/ai-agents-orchestrator/workflows/`, named after the
workflow, or a file anywhere passed with `--workflow-file`. `python orchestrator.py workflows` lists them and
shows why one does not load; `python orchestrator.py workflows default > ~/.config/ai-agents-orchestrator/workflows/mine.toml`
gives you the default's full definition to edit. [`examples/workflows/`](examples/workflows) has two more:
`quick` (Builder ⇄ Reviewer, the task as the contract) and `tdd` (a Test Writer before the Builder).

```toml
description = "Spec Collector -> Test Writer -> Builder <-> Reviewer"

[roles]                    # optional: labels and default models
tests = { label = "Test Writer", model = "sonnet" }

[[steps]]
use = "spec"               # a step of the default workflow, as it is

[[steps]]
id = "tests"               # also the role, unless role = "..." says otherwise
file = "tests.md"          # the handoff file that ends the turn; {n} in it makes one per round
edits = true               # the run's branch is created before the first editing step
prompt = """
You are the Test Writer. Write failing tests for the spec in {spec_path} in {cwd}.
As your last step, write a report to {tests_path} in a single write."""

[[steps]]
use = "build"              # the default Builder step, with only its first prompt replaced
prompt = "... the tests in {tests_path} ... write a report to {build_path} in a single write."

[[steps]]
use = "review"
```

A step takes `id`, `role`, `file` (a plain file name in the run directory, which no other step's file can match in
any round), `prompt`, and optionally `again` (its prompt in a later round), `fresh_note` (for a fresh session in a
later round), `loop_to`, which makes it the verdict step that sends `CHANGES_REQUESTED` back to the step it names
(a workflow without one ends `FINISHED` when its last step is done, and its pull request says no agent reviewed the
change), and the flags `human_paced`, `edits`, `quality_gated` and `fresh_repeats_again` (a fresh session gets
`again` too). `use = "spec"`, `"build"` or `"review"` starts from that default step and overrides only the keys you
give. Prompts fill in `{task}`, `{cwd}`, `{n}`, `{change}`, `{path}` (the step's own file), and for every step `X`:
`{X_path}`, `{prev_X_path}` and `{earlier_X_paths}`; write a literal brace as `{{` or `}}`. A role you do not list
in `[roles]`, or list without a label, keeps the default workflow's label for that key (`build` is "Builder"), and
is otherwise labelled after its key (`test_writer` is "Test Writer"). The panes follow the steps: the first role to
take a turn gets the root pane, the next one a split to its right, and each further one a split below. A role's
`model` is its default, and any model flag overrides it. The file is checked when it loads, and an error names the
step and key. A run saves its workflow's definition, so editing or deleting the file does not change a run already
started. See [Workflows](docs/architecture.md#workflows) for the rules.

## Running tests

```bash
uv run pytest
```

[TESTING.md](TESTING.md) plans the testing methods beyond these unit tests, and the order to adopt them in.
