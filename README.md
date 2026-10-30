<p align="center"><img src="docs/logo.svg" alt="An octopus holding a terminal, a robot and a chat bubble" width="200"></p>

<p align="center">
  <a href="https://github.com/DavidSiegl/octoflow-ai/releases/latest"><img src="https://img.shields.io/github/v/release/DavidSiegl/octoflow-ai" alt="Latest release"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/python/required-version-toml?tomlFilePath=https://raw.githubusercontent.com/DavidSiegl/octoflow-ai/main/pyproject.toml" alt="Python version"></a>
  <a href="https://github.com/DavidSiegl/octoflow-ai/actions/workflows/macos-binary.yml"><img src="https://img.shields.io/github/actions/workflow/status/DavidSiegl/octoflow-ai/macos-binary.yml?label=macOS%20binary" alt="macOS binary build"></a>
  <a href="https://github.com/DavidSiegl/octoflow-ai/commits/main"><img src="https://img.shields.io/github/last-commit/DavidSiegl/octoflow-ai" alt="Last commit"></a>
  <a href="https://ko-fi.com/davidsiegl"><img src="https://img.shields.io/badge/Ko--fi-support%20me-FF5E5B?logo=kofi&logoColor=white" alt="Support me on Ko-fi"></a>
</p>

# Octoflow AI

Coding agents in relay, no role judging its own work: a handoff workflow running in [herdr](https://herdr.dev),
each role in the agent of your choice: [Claude Code, Codex, Gemini CLI, opencode or pi](#five-coding-agents-in-any-mix).
The default workflow:

```text
Spec Collector ──spec.md──▶ Builder ──build-N.md──▶ Reviewer ──review-N.md──▶ APPROVE ──▶ pull request
                               ▲                                   │
                               └──────── CHANGES_REQUESTED ────────┘
```

Each role is a separate interactive agent session in its own herdr pane, so you can watch or step into any of
them. The agents run on this machine or on another one saved in herdr. Each feature is built on its own branch
and ends as a pull request on GitHub, where you review it. With `--quality-gate`, a Jenkins build judges the
Builder's change (linters, tests, coverage of new code) before the Reviewer sees it
([quality gate](docs/quality-gate.md)).

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
  ROLE=KIND` moves one role; a [workflow file](#your-own-workflows) can pin `agent = "codex"` to a role. Models
  work the same way, with `--model` or `--role-model ROLE=MODEL`.
- **One permission vocabulary.** `--permission-mode` takes Claude Code's mode names and gives each harness its
  own equivalent ([Harnesses](#harnesses)).
- **Sessions that survive.** An agent that exits is relaunched into its saved session, with its harness's own
  resume syntax.

```bash
orchestrator run "add a token-bucket rate limiter" --role-agent review=codex --role-agent spec=gemini
```

## Requirements

`orchestrator doctor` checks these, here and where the agents run, and says how to fix each one that
is missing.

- herdr 0.9+ where the agents run, with the integration of each harness a run uses
  (`herdr integration install <kind>`, e.g. `herdr integration install claude`)
- The CLI of each harness a run uses on `PATH` where the agents run: `claude` by default
- Unless you use `--no-pr`: a git checkout with an `origin` it can push to, and [`gh`](https://cli.github.com)
  logged in; with `--worktree`, a branch checked out and an `origin` even with `--no-pr`
- For any run that pushes: [gitleaks](https://github.com/gitleaks/gitleaks#installing) 8.19+ and git 2.36+.
  Every push is scanned for secrets first, by the base's `.gitleaks.toml` and `.gitleaksignore`, and refused on
  a finding
- Nothing for the orchestrator itself, a single [binary](#install); to build it from a checkout, Rust by
  `rust-toolchain.toml`; [uv](https://github.com/astral-sh/uv) and Python 3.13+ only for `gate.py` and the tests
- For `--machine`: the machine saved in herdr and non-interactive SSH to it, which the orchestrator's
  commands share one connection to (OpenSSH's `ControlMaster`); see
  [Running the agents on another machine](docs/design.md#running-the-agents-on-another-machine)
- For `--quality-gate`: a Jenkins quality job set up as in [quality gate](docs/quality-gate.md), its
  credentials in `~/.config/ai-agents-orchestrator/ci.env` or the environment, and a git checkout with an
  `origin`, also with `--no-pr`

## Install

Each [release](https://github.com/DavidSiegl/octoflow-ai/releases) has a single-file executable for Linux
x86_64 and for Apple Silicon macOS, each with its `.sha256`:

```bash
# Linux x86_64; on an Apple Silicon Mac, octoflow-macos-arm64 and `shasum -a 256 -c`
curl -fLO https://github.com/DavidSiegl/octoflow-ai/releases/latest/download/octoflow-linux-x86_64
curl -fLO https://github.com/DavidSiegl/octoflow-ai/releases/latest/download/octoflow-linux-x86_64.sha256
sha256sum -c octoflow-linux-x86_64.sha256 && chmod +x octoflow-linux-x86_64
./octoflow-linux-x86_64 run "add a token-bucket rate limiter to the API client"
```

The macOS binary is not signed or notarized. Downloaded with a browser, it is quarantined: right-click it and
choose Open once, or run `xattr -d com.apple.quarantine octoflow-macos-arm64`. A file `curl` downloads opens
straight away.

Everywhere else, build it from a checkout with cargo, by the toolchain `rust-toolchain.toml` pins:

```bash
cargo build --release --locked -p octoflow-engine   # target/release/orchestrator
```

The examples below call it `orchestrator`: put the binary on your `PATH` under that name. Releases before 0.49.0
were the Python engine, `orchestrator.py`, which also came as `octoflow.pyz`; the Rust engine takes the same
commands and flags, and resumes the runs the Python engine started.

The tk window that `gui` opened went with the Python engine; the [desktop app](#desktop-app) is the window now.

### Desktop app

A desktop app shows your runs: one project's runs, their handoff files and their agents, live. It starts no runs,
and changes nothing of them but what you type to an agent. It is a Tauri 2 app in [`ui/`](ui). Each release has it
for Linux x86_64, as `octoflow-desktop-linux-x86_64.deb` and `.AppImage`, and for Apple Silicon macOS, as
`octoflow-desktop-macos-arm64.dmg`, each with its `.sha256` and the orchestrator inside, so it needs no
checkout. The macOS app is signed ad hoc only, not notarized, so macOS refuses to open it the
first time when a browser downloaded it, as it does the binary.

- **Overview:** each run's task, its progress through the workflow, its round, the current role, whether it is
  running or stale, the question it is blocked on, its verdict and its pull request.
- **Run detail:** the files in a run's directory, the newest or the one you pick rendered as Markdown, updated
  as they are written.
- **Agents:** a tab for each agent the run has started, showing the last lines of its herdr pane, read again every
  two seconds, with a box to type a message to it and keys (Esc, Enter, ↑, ↓, Ctrl+C), as you would in the pane:
  to answer a question it asked, or stop it. The window reaches each agent in the herdr session its run was
  started or last resumed in, wherever the window was opened, in herdr or not. A run an earlier version saved did
  not record its session; its agents are reached in the one the herdr CLI picks where the window was opened: its
  herdr pane's, else `HERDR_SESSION`'s, else the default one.
- **Project bar:** a project folder and, for agents on another machine, a saved herdr machine. The app reopens
  the project you opened last.

Each download is the CLI too, with the same commands and flags; started without arguments, the app opens its
window:

- **AppImage:** the file itself, e.g. `./octoflow-desktop-linux-x86_64.AppImage run "..."` after `chmod +x`.
- **`.deb`:** it installs the orchestrator as `/usr/bin/octoflow`.
- **macOS:** link the orchestrator inside the app onto your `PATH`:

  ```bash
  ln -s /Applications/Octoflow.app/Contents/MacOS/octoflow /usr/local/bin/octoflow
  ```

  If macOS refuses to run it, the app was downloaded with a browser: open it once with right-click and Open, or
  run `xattr -dr com.apple.quarantine /Applications/Octoflow.app`. Not yet checked on a real Mac.

- **Headless machines and CI:** the plain binaries in [Install](#install), which need neither WebKit nor FUSE.

To run the app from a checkout, it needs Node 20.19+ with npm, and Rust with
[Tauri 2's system packages](https://v2.tauri.app/start/prerequisites/). On Debian or Ubuntu:

```bash
sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file libxdo-dev libssl-dev \
  libayatana-appindicator3-dev librsvg2-dev pkg-config
cd ui
npm ci
npm run tauri dev     # the window, rebuilt as you edit
npm run tauri build   # an optimized build, ../target/release/octoflow-ui
npm test              # the frontend's tests; npm run typecheck and npm run lint check it too
cargo test -p octoflow-ui     # the shell's, which start and stop a real serve
```

The app starts `orchestrator serve` beside it, the checkout's own `target/debug/orchestrator` (build it first with
`cargo build -p octoflow-engine`), and stops it when its window closes; set `ORCHESTRATOR_BIN` to serve another
binary. `serve` works without the app too: it prints one JSON line,
`{"url": "http://127.0.0.1:<port>", "token": "..."}`, and serves the project's runs read-only on that port until
Ctrl-C or SIGTERM. Its endpoints and security checks are in [serve](docs/architecture.md#serve).

## Usage

```bash
# Check what a run needs, and print a fix for each missing piece; exit status 1 if any is missing
orchestrator doctor --machine <machine> --cwd ~/GitHub/myproject

# From a herdr pane, on the project in the current directory
orchestrator run "add a token-bucket rate limiter to the API client"

# Agents on a machine saved in herdr, driven from anywhere; --cwd is a path on that machine
orchestrator run "add a token-bucket rate limiter" --machine <machine> --cwd ~/GitHub/myproject

# In a git worktree of the run's own, so the checkout stays yours and several runs can work at once
orchestrator run --worktree "add a token-bucket rate limiter"

# Skip the interview: a spec you already have is the contract
orchestrator run --spec docs/specs/rate-limiter.md

# Another workflow: one of yours by name, or a workflow file anywhere; `workflows` lists them
orchestrator run --workflow product-team "add a token-bucket rate limiter"
orchestrator run --workflow-file examples/workflows/quick.toml "fix the off-by-one in the pager"

# With the quality gate after each Builder turn
orchestrator run --quality-gate AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality "add a token-bucket rate limiter"

# Run every ready spec under .scratch/<slug>/spec.md, one pull request each; see Draining specs
orchestrator drain --worktree

# A project's runs and whether each is still running; --json for scripts, --pr-state adds each PR's state
orchestrator list --machine <machine> --cwd ~/GitHub/myproject

# One run in full, by its run id or the six-character key at its end
orchestrator show e292fb

# Continue a stopped run
orchestrator resume e292fb

# Done with a failed or finished run: close its workspace, and a failed --worktree run's worktree and branch
orchestrator close e292fb

# After you resolved a pull request's conflict and pushed: drop its warning, and its draft if it was approved
orchestrator refresh e292fb

# Delete the branches of runs whose pull request is merged, or closed 14 days ago
orchestrator prune --dry-run

# Every finished run's metrics.json, one JSON line each: outcome, roles, turns with times and tokens
orchestrator metrics | jq -s 'map(.rounds) | add / length'

# Judge the change since a commit as the quality gate does; exit status 0 for GATE: OK, 1 for GATE: ERROR
uv run python gate.py --base main --coverage coverage.xml --out quality.md
```

`--machine` and `--cwd` work the same way for every command, and `doctor` checks a project only when given
`--cwd`.

| Flag | Description |
|---|---|
| `--machine NAME` | Saved herdr machine to run the agents on. Requires `--cwd`. |
| `--cwd PATH` | Project directory, on the machine if `--machine` is given. Default: current directory. |
| `--max-rounds N` | Review rounds before giving up (default 3). |
| `--timeout SECONDS` | How long one Builder or Reviewer turn may take (default 1800). The interview has no limit. |
| `--agent KIND` | The [harness](#harnesses) every role runs in. Overrides a role's `agent` in the workflow file. |
| `--role-agent ROLE=KIND` | The harness of one role, by its key, e.g. `review=codex`. Overrides `--agent`; repeatable. |
| `--permission-mode MODE` | Permission mode for every role, by Claude Code's names; each other harness gets its [equivalent](#harnesses). |
| `--model MODEL` | Model for every role, passed to its harness unchanged. Default: the workflow file's model for the role, else the harness's own. |
| `--spec-model`, `--build-model`, `--review-model MODEL` | Model for the Spec Collector, Builder or Reviewer. Overrides `--model`. |
| `--role-model ROLE=MODEL` | Model for one role, by its key, e.g. `tests=sonnet`. Overrides `--model`; repeatable. |
| `--workflow NAME` | `run` and `drain`: `default`, or one of [your own](#your-own-workflows). A resumed run keeps its workflow. |
| `--workflow-file FILE` | `run` and `drain`: like `--workflow`, for the workflow file at `FILE`. |
| `--spec FILE` | `run` only: skip the interview. `FILE` is read on this machine, a relative path from the current directory, not `--cwd`. It becomes the first step's handoff file, and the run starts at the step after it. Without a task, the task is the spec's first line, when that is a level-one heading, or else `FILE`'s name. Needs a workflow whose first step writes the spec, such as `default` or `product-team`. |
| `--no-pr` | `run` only: leave the change uncommitted and the workspace open instead of opening a pull request. Works outside git. |
| `--worktree` | `run` and `drain`: work in a git worktree of the run's own; see [Worktree runs](#worktree-runs). |
| `--quality-gate JOB` | `run`, `drain` and `resume`: after each Builder turn, have this Jenkins job, by its full name with folders, judge the change. Needs `JENKINS_URL`, `JENKINS_USER` and `JENKINS_TOKEN`, from the environment or `~/.config/ai-agents-orchestrator/ci.env`. |
| `--no-quality-gate` | `resume` only: turn the run's quality gate off for good. |
| `--max-quality-rounds N` | With the gate: quality builds per review round before the Reviewer gets the change anyway (default 3). |
| `--force` | `resume` only: take over a run that still looks alive. |
| `--dry-run` | `prune` and `drain`: print what would happen, and change nothing. |

A run saves its settings. `resume` takes the same flags as `run` except `--no-pr`, `--worktree`, `--workflow`,
`--workflow-file` and `--spec`; a flag given to `resume` overrides the saved value. Turning the quality gate on or
off on `resume` has rules of its own; see
[quality gate](docs/quality-gate.md#turning-the-gate-on-or-off-on-resume).

### Harnesses

A role's harness is herdr's `--kind` for its agent. The precedence is `--role-agent`, then `--agent`, then the
role's `agent` in the workflow file, then `claude`. A model name is passed as it is, never translated, so give
each role a model its harness knows.

| `--permission-mode` | claude | gemini | codex | pi, opencode |
|---|---|---|---|---|
| `default` | `--permission-mode default` | `--approval-mode default` | (nothing) | dropped |
| `acceptEdits` | `--permission-mode acceptEdits` | `--approval-mode auto_edit` | `--full-auto` | dropped |
| `bypassPermissions` | `--permission-mode bypassPermissions` | `--approval-mode yolo` | `--dangerously-bypass-approvals-and-sandbox` | dropped |
| `plan` | `--permission-mode plan` | dropped | `--sandbox read-only` | dropped |
| `auto`, `dontAsk`, any other | passed through | dropped | dropped | dropped |

A dropped mode is not passed, so the agent starts with its own default, and the log says so once per role. An
agent that exited is relaunched into its saved session: `--resume <id>` for claude and gemini, `--session <id>`
for pi and opencode, and `codex resume <id>`. A role whose harness changed since starts a fresh session.

### Exit status

| Status | Meaning |
|---|---|
| `0` | Approved, or `FINISHED` for a workflow without a verdict step. |
| `3` | Changes still requested after the last round, or `QUALITY_GATE_FAILED` for a workflow without a verdict step. The pull request is a draft. |
| `4` | Approved or finished, but the pull request conflicts with its base branch, so it is a draft. |
| `5` | `drain`: the specs left wait on blockers that are not `done`, or were deleted or emptied while it ran. `refresh`: a pull request still conflicts, or GitHub has not yet computed whether it does. |
| `1` | Error. |
| `130` | Interrupted. |

`drain` exits with the status of the run that stopped it, `0` once no spec is `ready-for-agent`, or `5`.

## How a run works

1. **Workspace.** The run gets its own herdr workspace, with a pane per role. The project must be on a branch
   with a clean working tree; the pull request targets that branch. The branch is fetched from `origin` and
   fast-forwarded, and if that fails, for example offline or because it has diverged, the run stops before the
   interview. A `--worktree` run checks `origin`'s version of the branch out in its own worktree instead. A run that
   will open a pull request first deletes the branches of earlier runs whose pull request is done, as `prune` does.
2. **Spec Collector.** A notification tells you it is waiting. Answer its questions in its pane; once you approve
   the spec, it writes `spec.md`. With `--spec`, there is no interview.
3. **Builder.** The base branch is fast-forwarded again, and the branch `orchestrator/<spec title>-<id>` is
   created from it. The Builder implements the spec, verifies it, and writes `build-N.md` without committing. If
   it needs a decision from you, it writes `BLOCKED: <question>` instead, and the run notifies you and waits for
   your answer in its pane ([When the orchestrator needs you](docs/design.md#when-the-orchestrator-needs-you)).
4. **Quality gate**, with `--quality-gate`. A snapshot of the working tree is pushed to a throwaway branch and
   judged by the Jenkins job. If the gate does not pass, the Builder answers its findings, up to
   `--max-quality-rounds` times; then the change goes to the Reviewer either way, who is told whether the gate
   passed. A snapshot holding a secret is never pushed. See [quality gate](docs/quality-gate.md).
5. **Reviewer.** A fresh session checks the change against the spec and writes `review-N.md`, which starts with
   `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`; requested changes go back to the Builder.
6. **Pull request.** The change is committed as one commit, the base branch is merged in if it moved on, and the
   branch is opened as a pull request with `gh`. It is a draft if changes were still requested after the last
   round, or if the merge conflicts; then you resolve the conflict and run `refresh`
   ([Refreshing a conflicted pull request](docs/design.md#refreshing-a-conflicted-pull-request)). The workspace
   is closed, and the branch stays until a later run's sweep or `prune` deletes it
   ([Deleting run branches](docs/design.md#deleting-run-branches)).
7. **Summary.** Whenever the run ends, it writes `summary.md`: the outcome, the pull request, each role's harness
   and model, and a table of the turns with their durations, which the pull request gets too. A finished run also
   writes `metrics.json`, which `metrics` prints
   ([Turn timings and the summary](docs/design.md#turn-timings-and-the-summary)).

Everything a run writes stays in `<project>/.orchestrator/runs/<run-id>/`, which git ignores. Runs in one checkout
share its working tree, so a run refuses to start or resume while another run there is `running`; use
`--worktree`, or a clone per run, to run in parallel.

A failed run keeps its workspace open and stays on its branch, and so does a `--no-pr` run. `resume` continues a
stopped run where `state.json` says ([Resuming a run](docs/design.md#resuming-a-run)), and `close` cleans up after
one you are done with ([Closing a run](docs/design.md#closing-a-run)). `list` marks a run `stale` after five
minutes without a heartbeat ([Stale runs](docs/design.md#stale-runs)).

See also [design decisions](docs/design.md), [architecture](docs/architecture.md), [roadmap](docs/roadmap.md)
and [quality gate](docs/quality-gate.md).

### Worktree runs

A `--worktree` run works in `.orchestrator/worktrees/<key>`, checked out from `origin`'s version of the project's
branch, so the project's checkout need not be clean and stays yours to use. The worktree holds what git tracks.
Untracked and ignored files, such as a `.venv`, are not in it, though Claude Code still reads the project's
`CLAUDE.md` from the parent directory. The handoff files stay in the project's run directory. claude, codex and
gemini are given access to it; opencode and pi may ask before writing there, and herdr notifies you to allow it.

The worktree is removed once the pull request is open. A failed or `--no-pr` run keeps its worktree, whose path
`show` prints. `close` removes a failed run's worktree, discarding its changes, but keeps a finished `--no-pr`
run's, which holds the change. A `--worktree` run whose worktree is gone cannot be resumed, unless its pull
request is already open.

## Draining specs

`drain [DIR]` works through a directory of specs, `.scratch/` by default. Each `DIR/<slug>/spec.md` is a
candidate, and drain runs the ready ones one after another, in slug order, each as `run --spec` would, each
ending in its own pull request. You merge the pull requests and run `drain` again until every spec is done.

Drain reads and writes only the spec's header, the lines before its first `##` heading:

```markdown
# Add a token-bucket rate limiter

Status: ready-for-agent
Blocked by: retry-policy, http-client/spec.md
Run: 20261024-231412-d64cd0
PR: https://github.com/o/r/pull/7
```

- `Status:` moves from `ready-for-agent` to `in-review` while the run's pull request is open, and to `done` once it
  has merged. A run that does not approve, conflicts or fails leaves its spec `ready-for-human`. Any other status
  is left alone.
- `Blocked by:` lists the specs that must be `done` first, by slug or by a path ending in `<slug>/spec.md`. A
  missing blocker, or blockers in a cycle, stop drain before it runs anything.
- `Run:` and `PR:` are written by drain.

Drain stops rather than polls: a spec whose blocker's pull request is still open waits for the next drain. If
the specs directory is inside the checkout, ignore it in git, or use `--worktree`, since the header edits would
otherwise dirty the tree. See [Draining specs](docs/design.md#draining-specs) for what a drain does in order and
how it treats specs you edit while it runs.

## Your own workflows

The roles, their prompts and their order are a workflow. Besides the built-in `default`, add your own as a TOML
file in `~/.config/ai-agents-orchestrator/workflows/`, named after the workflow, or anywhere with
`--workflow-file`. `orchestrator workflows` lists them and says why one does not load, and
`workflows default` prints the default's full definition to start from. [`examples/workflows/`](examples/workflows)
has three more: `spec-build-review` (the default as a file), `quick` (Builder ⇄ Reviewer, the task as the
contract) and `product-team` (Product Manager → Architect → QA Engineer → Engineer ⇄ Tech Lead: requirements in
business terms, a design against the code, and acceptance tests written and locked before the code).

```toml
# An abridged product-team.toml: the Architect's design before the default Builder and Reviewer.
description = "Product Manager -> Architect -> Engineer <-> Tech Lead"

[roles]                    # optional: labels, default models and harnesses
spec = "Product Manager"
design = { label = "Architect", model = "sonnet" }
review = { label = "Tech Lead", agent = "codex" }

[[steps]]
use = "spec"               # a step of the default workflow, as it is

[[steps]]
id = "design"              # also the role, unless role = "..." says otherwise
file = "design.md"         # the handoff file that ends the turn; {n} in it makes one per round
prompt = """
You are the Architect. Design how to meet the requirements in {spec_path} in the code in {cwd}; change no file.
As your last step, write the design to {design_path} in a single write."""

[[steps]]
use = "build"              # the default Builder step, with only its first prompt replaced
edits = true               # as it already is: the run's branch is created before the first editing step
prompt = "... the design in {design_path} ... write a report to {build_path} in a single write."

[[steps]]
use = "review"
```

| Step key | Meaning |
|---|---|
| `id`, `role` | The step's id, and the role that takes it, the id by default. |
| `file` | The handoff file in the run directory that ends the turn; `{n}` in it makes one per round. |
| `prompt`, `again`, `fresh_note` | The first prompt, the prompt in a later round, and the note for a fresh session in a later round. |
| `use` | `"spec"`, `"build"` or `"review"`: start from that default step and override only the keys given. |
| `loop_to` | Makes this the verdict step, which sends `CHANGES_REQUESTED` back to the step it names. |
| `edits`, `human_paced`, `quality_gated` | The step edits the code, waits on you, or is the one the quality gate follows. |
| `locks` | The first editing step locks the files it changes, as `product-team`'s QA Engineer does; later turns' changes to them are undone. |
| `fresh_repeats_again` | A fresh session in a later round gets `again` too. |

A workflow without a verdict step ends `FINISHED`, and its pull request says no agent reviewed the change. A
workflow's `proof_command`, such as `"python -m pytest -q {proofs}"`, runs the tests the verdict step wrote to back
its findings. Prompts fill in `{task}`, `{cwd}`, `{n}`, `{change}`, `{path}`, `{proofs_dir}`, and for every step
`X`, `{X_path}`, `{prev_X_path}` and `{earlier_X_paths}`; write a literal brace as `{{` or `}}`. The file is
checked when it loads, and a run saves its workflow, so editing the file does not change a run already started.
[Workflows](docs/architecture.md#workflows) has the full rules.

## Prompt files

To change only some of the texts the agents get, put `NAME.md` in `~/.config/ai-agents-orchestrator/prompts/`,
where `NAME` is the text's name in the engine's `engine/src/prompts.rs`. Its content, less its trailing newline, is sent in its
place, with exactly the placeholders of the text it replaces.

| File | Sent |
|---|---|
| `SPEC_PROMPT.md`, `BUILD_PROMPT.md`, `REVIEW_PROMPT.md` | the default workflow's first prompt to each role |
| `FIX_PROMPT.md`, `RECHECK_PROMPT.md` | the Builder's and the Reviewer's prompt in a later round |
| `REBUILD_NOTE.md`, `REREVIEW_NOTE.md` | to a fresh Builder or Reviewer session in a later round |
| `QUALITY_FIX_PROMPT.md` | to the quality-gated step when the gate did not pass |
| `QUALITY_PASSED_NOTE.md`, `QUALITY_UNRESOLVED_NOTE.md`, `QUALITY_STUCK_NOTE.md`, `QUALITY_OFF_NOTE.md` | at the end of the verdict step's prompt, on how the quality gate ended |
| `LOCKED_NOTE.md`, `PROOFS_NOTE.md`, `CARRIED_PROOFS_NOTE.md` | on restored locked files, and on the Reviewer's failing proofs |
| `BLOCKED_NOTE.md`, `BLOCKED_PROMPT.md` | at the end of every prompt of a step that can block, and once its report asked the human |
| `CONTINUE_PROMPT.md`, `RETRY_VERDICT_PROMPT.md` | to a session restarted mid-turn, and after a review without a verdict line |

`workflows` lists the prompt files in effect. A bad file stops `run` or `drain` before any agent starts; see
[Prompt files](docs/design.md#prompt-files).

## Running tests

```bash
cargo test --workspace    # the engine's and the desktop shell's
uv run pytest             # gate.py's, the docs', and the parity scenarios against the engine's binary
uv run pytest --cov=gate --cov-branch --cov-report=xml && \
    uv run python gate.py --base main --coverage coverage.xml
```

The parity scenarios (`tests/parity`) replay what the Python engine did, recorded before it was removed, against
the Rust engine, which `uv run pytest` builds with cargo. `tests/test_properties.py` holds property-based tests of
the gate: 25 inputs per test by default, 250 with `HYPOTHESIS_PROFILE=ci`, as Jenkins runs them. A change to a
workflow step's prompt or to the quality file's wording fails a test against its golden file in `tests/golden/`; `UPDATE_GOLDEN=1 uv run pytest` rewrites them, and the
change is reviewed as their diff (see [Tests](docs/architecture.md#tests)). `packaging/build-binary.sh` builds and
smoke-tests the binary for the machine it runs on; see [Tooling](docs/architecture.md#tooling).

Before a release that changes a prompt, a workflow or how a harness is run, run the eval set by hand, from a herdr
pane: `uv run python evals/run.py` gives real agents five small tasks and judges each by a test they never see
([Evals](docs/design.md#evals)).

## License

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or [MIT license](LICENSE-MIT), at your
option. Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in this
project by you, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms
or conditions.

The binaries and the desktop app contain Rust crates under their own licenses, whose texts each release has as
`octoflow-third-party-licenses.html`; the desktop app has the same file among its resources as
`THIRD-PARTY-LICENSES.html`. `packaging/build-binary.sh` writes it with
[cargo-about](https://github.com/EmbarkStudios/cargo-about), by `packaging/about.toml`, which lists the licenses a
dependency may be under: a dependency under any other fails the build.
