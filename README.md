<p align="center"><img src="docs/logo.svg" alt="An octopus holding a terminal, a robot and a chat bubble" width="200"></p>

# ai-agents-orchestrator

A role-based handoff workflow for coding agents running in [herdr](https://herdr.dev), each role in the agent
of your choice: [Claude Code, Codex, Gemini CLI, opencode or pi](#five-coding-agents-in-any-mix). The default
workflow:

```
Spec Collector ──spec.md──▶ Builder ──build-N.md──▶ Reviewer ──review-N.md──▶ APPROVE ──▶ pull request
                               ▲                                   │
                               └──────── CHANGES_REQUESTED ────────┘
```

With `--quality-gate`, a SonarQube analysis through Jenkins sits between the Builder and the Reviewer, and
sends its findings back to the Builder first.

Each role is a separate interactive agent session in its own herdr pane, so no role judges its own
work, and you can watch or step into any of them. The agents run on this machine or on another one saved in
herdr. Each feature is built on its own branch and ends as a pull request on GitHub, where you review it.

## Five coding agents, in any mix

Every role runs in the agent harness you pick for it, so the agent that reviews a change need not be the one
that wrote it:

| Harness | `--agent` | Models | `--permission-mode` it understands |
|---|---|---|---|
| [Claude Code](https://github.com/anthropics/claude-code) | `claude`, the default | Anthropic's Claude | every mode, as it is |
| [Codex CLI](https://github.com/openai/codex) | `codex` | OpenAI's GPT | `default`, `acceptEdits`, `bypassPermissions`, `plan` |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `gemini` | Google's Gemini | `default`, `acceptEdits`, `bypassPermissions` |
| [opencode](https://github.com/sst/opencode) | `opencode` | any provider it is set up for | none; it starts with its own default |
| [pi](https://github.com/badlogic/pi-mono) | `pi` | any provider it is set up for | none; it starts with its own default |

- **A second opinion from another model.** `--role-agent review=codex` has Claude Code write the change and
  Codex review it, so the review does not share the builder's blind spots.
- **One flag for every role, or one role at a time.** `--agent gemini` moves the whole run; `--role-agent
  ROLE=KIND`, repeatable, moves one role; a [workflow file](#your-own-workflows) can pin `agent = "codex"` to a
  role. Models work the same way, with `--model`, `--build-model` or `--role-model ROLE=MODEL`.
- **One permission vocabulary.** `--permission-mode` takes Claude Code's mode names and gives each harness its
  own equivalent: `acceptEdits` is `--full-auto` for Codex and `--approval-mode auto_edit` for Gemini CLI. The log
  says when a harness has none.
- **Sessions that survive.** An agent that exits is relaunched into its saved session, with its harness's own
  resume syntax. Switch a role's harness on `resume`, and its next agent starts fresh in the new one.
- **You answer the Spec Collector in its pane**, whichever harness it runs in.

```bash
python orchestrator.py run "add a token-bucket rate limiter" --role-agent review=codex --role-agent spec=gemini
```

Each harness a run uses needs its CLI and its herdr integration where the agents run; see
[Requirements](#requirements) and, for the precedence and the exact flags, [Harnesses](#harnesses).

## Requirements

- herdr 0.9+ where the agents run, with the integration of each harness a run uses installed there
  (`herdr integration install <kind>`, e.g. `herdr integration install claude`)
- The CLI of each harness a run uses on `PATH` where the agents run: `claude` by default, and `codex`, `gemini`,
  `opencode` or `pi` for a role that runs in one of those
- Where the agents run, unless you use `--no-pr`: a git checkout with an `origin` it can push to, and
  [`gh`](https://cli.github.com) logged in (`gh auth status`); with `--worktree`, a branch checked out and an
  `origin` even with `--no-pr`
- Python 3.13+, standard library only, unless you use a [binary](#install), which brings its own; tkinter for the
  [GUI](#gui); [uv](https://github.com/astral-sh/uv) only for the tests and the build
- For `--machine`: the machine saved in herdr and non-interactive SSH to its target; see
  [Running the agents on another machine](docs/design.md#running-the-agents-on-another-machine)
- For `--quality-gate`: a Jenkins quality job and SonarQube set up as in [quality gate](docs/quality-gate.md),
  their credentials in `~/.config/ai-agents-orchestrator/ci.env` or the environment, and a git checkout with an
  `origin`

## Install

Each [release](https://github.com/DavidSiegl/ai-agents-orchestrator/releases) has a single-file executable for
Linux x86_64 and for Apple Silicon macOS, which needs no Python, and each with its `.sha256`:

```bash
# Linux x86_64; on an Apple Silicon Mac, orchestrator-macos-arm64 and `shasum -a 256 -c`
curl -fLO https://github.com/DavidSiegl/ai-agents-orchestrator/releases/latest/download/orchestrator-linux-x86_64
curl -fLO https://github.com/DavidSiegl/ai-agents-orchestrator/releases/latest/download/orchestrator-linux-x86_64.sha256
sha256sum -c orchestrator-linux-x86_64.sha256 && chmod +x orchestrator-linux-x86_64
./orchestrator-linux-x86_64 run "add a token-bucket rate limiter to the API client"
```

The macOS binary is not signed or notarized. Downloaded with a browser, macOS refuses to open it the first time:
right-click it and choose Open once, or run `xattr -d com.apple.quarantine orchestrator-macos-arm64`. A file
`curl` downloads is not quarantined, so it opens straight away.

Everywhere else, use `orchestrator.pyz`, an executable of `orchestrator.py` that needs Python 3.13+, and tkinter
for the GUI:

```bash
curl -fLO https://github.com/DavidSiegl/ai-agents-orchestrator/releases/latest/download/orchestrator.pyz
curl -fLO https://github.com/DavidSiegl/ai-agents-orchestrator/releases/latest/download/orchestrator.pyz.sha256
sha256sum -c orchestrator.pyz.sha256 && chmod +x orchestrator.pyz
./orchestrator.pyz run "add a token-bucket rate limiter to the API client"
```

Both take the same commands and flags as `python orchestrator.py` in a checkout, which the examples below use.

### GUI

Run without arguments, or with `gui`, the orchestrator opens a window where a display is available: always on
macOS, and on Linux when `DISPLAY` or `WAYLAND_DISPLAY` is set; otherwise it prints its usage. In the
window you fill in a run (the task, the project folder, and optionally a machine, the workflow, the harness for
every role, the model, the permission mode, `--no-pr` and a quality-gate job; per-role harnesses and models stay
on the command line), see the project's runs as `list` shows them, resume one, and follow the output of the
run, which Stop interrupts as Ctrl-C would. The window starts the same `run` or `resume` command you would type,
one at a time, and shows it at the top of the output. The window wears the octopus logo's dark navy: its
header names the selected workflow's roles, each run's status is in color, and the output is in a terminal-style
pane.

The role agents still run in herdr, so open `herdr` in a terminal to answer the Spec Collector. As on the
command line, a run without a machine has to start from a herdr pane: start the window from one, or give a
machine. Launched from a file manager instead of a shell, the binary sees only that session's `PATH`, so
`herdr`, `git`, `gh` and the CLI of each harness a run uses must be on it.

The text is set in Roboto where it is installed, and in the platform's font otherwise. Zoom it from 75% to 200%
with the − and + buttons at the top right, with Ctrl (⌘ on macOS) and +, − or 0, or with Ctrl and the mouse
wheel; the percentage between the buttons sets it back to 100%.

A Python without tkinter makes `gui` exit with status 1 and the command to install it, such as
`apt install python3-tk` or `brew install python-tk@3.13`; the binaries include it.

## Usage

```bash
# From a herdr pane, on the project in the current directory
python orchestrator.py run "add a token-bucket rate limiter to the API client"

# Agents on a machine saved in herdr, driven from anywhere; --cwd is a path on that machine
python orchestrator.py run "add a token-bucket rate limiter" --machine <machine> --cwd ~/GitHub/myproject

# Runs recorded for a project, and whether each one is still running
python orchestrator.py list --machine <machine> --cwd ~/GitHub/myproject

# One run in full: its state, branch and pull request, agents, handoff files and last review
python orchestrator.py show e292fb --machine <machine> --cwd ~/GitHub/myproject

# Delete the branches of runs whose pull request is merged, or closed 14 days ago; --dry-run only prints
python orchestrator.py prune --dry-run --machine <machine> --cwd ~/GitHub/myproject

# Continue a stopped run, by its run id or the six-character key at its end
python orchestrator.py resume e292fb --machine <machine> --cwd ~/GitHub/myproject

# Done with a failed or finished run: close its workspace, and a failed --worktree run's worktree and branch
python orchestrator.py close e292fb --machine <machine> --cwd ~/GitHub/myproject

# Another workflow: one of yours by name, or a workflow file anywhere; `workflows` lists them
python orchestrator.py run --workflow tdd "add a token-bucket rate limiter"
python orchestrator.py run --workflow-file examples/workflows/quick.toml "fix the off-by-one in the pager"

# In a git worktree of the run's own, so the checkout stays yours and several runs can work at once
python orchestrator.py run --worktree "add a token-bucket rate limiter"

# Skip the interview: a spec you already have is the contract, and the Builder starts on it
python orchestrator.py run --spec docs/specs/rate-limiter.md

# With the SonarQube quality gate after each Builder turn; credentials from ~/.config/ai-agents-orchestrator/ci.env
python orchestrator.py run --quality-gate AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality "add a token-bucket rate limiter"

# The window to start, list and resume runs in; no arguments open it too where there is a display
python orchestrator.py gui
```

| Flag | Description |
|---|---|
| `--machine NAME` | Saved herdr machine to run the agents on. Requires `--cwd`. |
| `--cwd PATH` | Project directory, on the machine if `--machine` is given. Default: current directory. |
| `--max-rounds N` | Review rounds before giving up (default 3). |
| `--timeout SECONDS` | How long one Builder or Reviewer turn may take (default 1800). The interview has no limit. |
| `--agent KIND` | The [harness](#harnesses) every role runs in: `claude`, `codex`, `gemini`, `opencode` or `pi`. Overrides the `agent` of a role in the workflow file. Default: that, else `claude`. |
| `--role-agent ROLE=KIND` | The harness of one role of the workflow, by its key, e.g. `review=codex`. Overrides `--agent`; repeatable. |
| `--permission-mode MODE` | Permission mode for every role, by Claude Code's names, e.g. `auto` or `acceptEdits`; each other harness gets its [equivalent](#harnesses), if it has one. |
| `--model MODEL` | Model for every role, e.g. `sonnet`, passed to its harness as `--model MODEL` unchanged. Default: the workflow file's model for the role, else the harness's own. |
| `--spec-model MODEL` | Model for the Spec Collector. Overrides `--model`. |
| `--build-model MODEL` | Model for the Builder. Overrides `--model`. |
| `--review-model MODEL` | Model for the Reviewer. Overrides `--model`. |
| `--role-model ROLE=MODEL` | Model for one role of the workflow, by its key, e.g. `tests=sonnet`. Overrides `--model`; repeatable. |
| `--workflow NAME` | `run` only: the workflow the run goes through: `default`, the run described below, or one of [your own](#your-own-workflows) in `~/.config/ai-agents-orchestrator/workflows/`. `--help` lists the choices; a resumed run keeps its workflow. |
| `--workflow-file FILE` | `run` only: like `--workflow`, for the workflow file at `FILE`, such as one kept in the project. |
| `--spec FILE` | `run` only: skip the interview. `FILE`, read on this machine (a relative path is from the current directory, not `--cwd`), is copied into the run as the handoff file of the workflow's first step, `spec.md` in `default`, and the run starts at the step after it. The task is then optional: without one it is the spec's `# ` title, or else `FILE`'s name. Needs a workflow whose first step writes the spec, such as `default` or `tdd`, not `quick`. |
| `--no-pr` | `run` only: leave the change uncommitted and the workspace open instead of opening a pull request. Works outside git. |
| `--worktree` | `run` only: work in a git worktree of the run's own, `.orchestrator/worktrees/<key>`, checked out from `origin`'s version of the branch the project is on, instead of in the project's checkout, which then need not be clean and stays yours to use. Needs a branch checked out and an `origin`, also with `--no-pr`. See [Worktree runs](#worktree-runs). |
| `--quality-gate JOB` | `run` only: after each Builder turn, analyse the change with this Jenkins job, by its full name with folders, and SonarQube, and send the findings back to the Builder before the Reviewer. Needs `JENKINS_URL`, `JENKINS_USER`, `JENKINS_TOKEN`, `SONAR_HOST_URL` and `SONAR_TOKEN`, from the environment or `~/.config/ai-agents-orchestrator/ci.env`. |
| `--max-quality-rounds N` | With the gate: SonarQube analyses per review round before the Reviewer gets the change anyway (default 3). |
| `--force` | `resume` only: take over a run that still looks alive. |
| `--dry-run` | `prune` only: print what would become of each branch, and delete and record nothing. |

A run saves its settings. `resume` takes the same flags as `run` except `--no-pr`, `--worktree`, `--quality-gate`,
`--workflow`, `--workflow-file` and `--spec`, and a flag given to `resume` overrides the saved value; one left out keeps it.
A harness changed by `resume` applies to each role's next agent; one still running keeps its own.

### Harnesses

A role's harness is herdr's `--kind` for its agent: `claude` (Claude Code, the default), `codex`, `gemini`,
`opencode` or `pi`. The precedence is `--role-agent`, then `--agent`, then the role's `agent` in the workflow file,
then `claude`. A model name is passed as it is, never translated between harnesses, so give each role a model its
harness knows.

`--permission-mode` takes Claude Code's mode names, and each harness gets its own equivalent:

| `--permission-mode` | claude | gemini | codex | pi, opencode |
|---|---|---|---|---|
| `default` | `--permission-mode default` | `--approval-mode default` | (nothing) | dropped |
| `acceptEdits` | `--permission-mode acceptEdits` | `--approval-mode auto_edit` | `--full-auto` | dropped |
| `bypassPermissions` | `--permission-mode bypassPermissions` | `--approval-mode yolo` | `--dangerously-bypass-approvals-and-sandbox` | dropped |
| `plan` | `--permission-mode plan` | dropped | `--sandbox read-only` | dropped |
| `auto`, `dontAsk`, any other | passed through | dropped | dropped | dropped |

A dropped mode is not passed to the agent, which then starts with its own default, and the log says so once per
role. An agent that exited is relaunched into its saved session with its harness's own syntax: `--resume <id>` for
claude and gemini, `--session <id>` for pi and opencode, and `codex resume <id>`. If the role's harness has changed
since, the agent starts a fresh session instead.

### Exit status

| Status | Meaning |
|---|---|
| `0` | Approved, or `FINISHED` for a workflow without a verdict step. |
| `3` | Changes still requested after the last round, or, for a workflow without a verdict step, `QUALITY_GATE_FAILED`: the quality gate still failed after the last quality round. Either way the pull request is a draft. |
| `4` | Approved or finished, but the pull request conflicts with its base branch, so it is a draft. |
| `1` | Error. |
| `130` | Interrupted. |

## How a run works

1. **Workspace.** The run gets its own herdr workspace, with a pane per role. The project must be on a branch
   with a clean working tree; the pull request targets that branch. That branch is fetched from `origin` and
   fast-forwarded, so the Spec Collector reads current code. If that fails, for example offline or because the
   local branch has diverged from `origin`'s, the run stops before the interview. A `--worktree` run instead
   fetches that branch and checks `origin`'s version of it out, on a detached HEAD, in its own worktree
   `.orchestrator/worktrees/<key>`, where its panes open; the checkout need not be clean and is never switched
   or fast-forwarded. Before all that, a run that
   will open a pull request deletes the branches of earlier runs whose pull request is done, as `prune` does,
   skipping any it kept before; a failure there is logged and the run goes on.
2. **Spec Collector.** A notification tells you it is waiting. Answer its questions in its pane; once you
   approve the spec, it writes `spec.md`. With `--spec FILE` there is no interview: `FILE` is copied to
   `spec.md`, no Spec Collector starts, and the Builder takes the root pane.
3. **Builder.** The orchestrator fetches and fast-forwards the base branch again, since the interview can take
   hours, and creates the branch `orchestrator/<spec title>-<id>` from it; a `--worktree` run creates it in its
   worktree, from `origin`'s base branch. The Builder implements the spec there, verifies it, and writes
   `build-N.md` without committing.
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
   closed; a `--worktree` run removes its worktree instead, and leaves the checkout alone. The branch stays,
   locally and on `origin`, until a later run's sweep or `prune` deletes it.

One run at a time per checkout: runs in one project directory share its working tree, so a run, `--no-pr`
or not, refuses to start or resume while another run there is `running` (see `list`). A stale, finished or
failed run does not count. A `--worktree` run has a working tree of its own, so it neither refuses nor is
refused by any other run. To run in parallel, use `--worktree`, or give each run its own clone.

Everything a run writes stays in `<project>/.orchestrator/runs/<run-id>/`: the handoff files and `state.json`,
which records the phase, round, panes, branch, pull request, verdict and any error. `.orchestrator/` ignores
itself, so it never shows up in the diff or the commit. A failed run keeps its workspace open and stays on its
branch, and a `--no-pr` run keeps its workspace open too. `close` closes it when you are done, and records that
in `state.json`; `resume` then refuses the run. A failed or stale `--worktree` run also loses its worktree,
uncommitted changes included, which `close` lists first, and its branch, unless the branch has commits beyond the
run's base. A finished run keeps its worktree, and an in-place run its checkout and branch. `close` refuses a run
that is `running`. `list` ends a closed run's line with what `close` did, in place of `stale` and its `resume`
command ([Closing a run](docs/design.md#closing-a-run)).

`resume` continues a run whose orchestrator stopped where `state.json` says, reusing or relaunching its agents
([Resuming a run](docs/design.md#resuming-a-run)). It refuses while another branch than the run's is checked
out, and names the one to check out. `list` marks a run `stale` after five minutes without a
heartbeat ([Stale runs](docs/design.md#stale-runs)). `show` prints one run in full, without opening
`state.json`: its `list` line and whole task, its workflow, branch and pull request, each role's agent and pane,
the paths of its handoff files, the last review, what `prune` did with its branch, and what `close` did. `prune` deletes the
branch of each run whose pull request is merged, or closed for 14 days, locally and on `origin`, and prints a
line per run. It keeps a side that has commits the pull request lacks, and a local branch checked out in any
worktree; a later `prune` looks at kept branches again
([Deleting run branches](docs/design.md#deleting-run-branches)). You get a herdr notification when a role is blocked or idle
for 3 minutes ([When the orchestrator needs you](docs/design.md#when-the-orchestrator-needs-you)).
See also [design decisions](docs/design.md), [architecture](docs/architecture.md), [roadmap](docs/roadmap.md)
and [quality gate](docs/quality-gate.md).

### Worktree runs

A `--worktree` run's worktree holds what git tracks, as `origin` has it. Untracked and ignored files in the
project, such as a gitignored `CLAUDE.md` or a `.venv`, are not in it; Claude Code still reads the project's
`CLAUDE.md`, since the worktree is inside the project and Claude Code reads the `CLAUDE.md` of each
parent directory too. The handoff files stay in the project's run
directory, outside the worktree: claude and codex get it with `--add-dir`, gemini with `--include-directories`.
opencode and pi have no such flag and may ask before writing a handoff file there; herdr's notification that the
agent is blocked brings you in to allow it.

The worktree is removed once the pull request is open. A `--no-pr` run leaves its change uncommitted in the
worktree, whose path it logs and `show` prints, and a failed run keeps its worktree, as an in-place run keeps its
branch. A `--worktree` run whose worktree is gone cannot be resumed, unless its pull request is already open.
`close` removes a failed run's worktree, discarding its changes. A finished `--no-pr` run's worktree holds the
change, so `close` keeps it and prints its path; remove it by `git worktree remove .orchestrator/worktrees/<key>`
in the project once you have taken the change.

## Your own workflows

The roles, their prompts and their order are a workflow. Besides the built-in `default`, you can add your own
without touching the code: a TOML file in `~/.config/ai-agents-orchestrator/workflows/`, named after the
workflow, or a file anywhere passed with `--workflow-file`. `python orchestrator.py workflows` lists them and
shows why one does not load; `python orchestrator.py workflows default > ~/.config/ai-agents-orchestrator/workflows/mine.toml`
gives you the default's full definition to edit. [`examples/workflows/`](examples/workflows) has three more:
`spec-build-review` (the built-in `default` as a file, to copy and edit), `quick` (Builder ⇄ Reviewer, the task
as the contract) and `tdd` (a Test Writer before the Builder).

```toml
description = "Spec Collector -> Test Writer -> Builder <-> Reviewer"

[roles]                    # optional: labels, default models and harnesses
tests = { label = "Test Writer", model = "sonnet" }
review = { agent = "codex" }

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
change; with `--quality-gate`, a gate that still failed after the last quality round ends it `QUALITY_GATE_FAILED`
instead, a draft, as under [Exit status](#exit-status)), and the flags `human_paced`, `edits`, `quality_gated` and
`fresh_repeats_again` (a fresh session gets `again` too). `use = "spec"`, `"build"` or `"review"` starts from that
default step and overrides only the keys you give. Prompts fill in `{task}`, `{cwd}`, `{n}`, `{change}`, `{path}`
(the step's own file), and for every step `X`: `{X_path}`, `{prev_X_path}` and `{earlier_X_paths}`; write a literal
brace as `{{` or `}}`. A role you do not list in `[roles]`, or list without a label, keeps the default workflow's
label for that key (`build` is "Builder"), and is otherwise labelled after its key (`test_writer` is "Test
Writer"). The panes follow the steps: the first role to take a turn gets the root pane, the next one a split to its
right, and each further one a split below. A role's `model` is its default, and any model flag overrides it;
likewise its `agent`, the [harness](#harnesses) it runs in, one of `claude`, `codex`, `gemini`, `opencode` and
`pi`, which `--agent` and `--role-agent` override. The file is checked when it loads, and an error names the step
and key. A run saves its workflow's definition, so editing or deleting the file does not change a run already
started. See [Workflows](docs/architecture.md#workflows) for the rules.

## Running tests

```bash
uv run pytest
```

`packaging/build-binary.sh` builds the binary for the machine it runs on into `dist/`, with PyInstaller from the
`build` dependency group, and smoke-tests it; see [Tooling](docs/architecture.md#tooling).
