# Architecture

An overview of how the orchestrator is built, for someone about to read or change the code. For usage see
[README.md](README.md); for the design rationale behind resume and stale-run detection, and the backlog, see
[ROADMAP.md](ROADMAP.md).

This file names code by symbol, not by line, so that it stays true as lines move.

## The three roles

```
Spec Collector ──spec.md──▶ Builder ──build-N.md──▶ Reviewer ──review-N.md──▶ APPROVE ──▶ publish
                               ▲                                   │
                               └──────── CHANGES_REQUESTED ────────┘
```

Each role is a separate interactive Claude Code session, run as a herdr agent in its own pane of the run's
herdr workspace. No role judges its own work, and the human can watch or step into any of them.

- The **Spec Collector** interviews the human and writes `spec.md`. Its first line is a `# ` title, which
  becomes the branch name, the commit subject and the pull request title.
- The **Builder** implements the spec and writes `build-N.md` for round N. It does not commit.
- The **Reviewer** checks the change against the spec and writes `review-N.md`. Its first line is
  `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Requested changes send the run back to the Builder for
  round N + 1, up to `max_rounds`.

The roles hand off only through these Markdown files, never through scraped terminal output. Every prompt
asks for its file "in a single write", so a file that exists is taken as complete.

## `orchestrator.py`

The whole program is one standard-library module, in these sections.

### Constants

`RUNS_DIR`, the defaults for the run settings (`DEFAULT_TURN_TIMEOUT`, `DEFAULT_MAX_ROUNDS`), the polling and
liveness intervals (`AGENT_START_TIMEOUT_MS`, `POLL_SECONDS`, `STALL_SECONDS`, `HEARTBEAT_SECONDS`,
`STALE_SECONDS`), the pull request settings (`NETWORK_TIMEOUT`, `BRANCH_PREFIX`, `REMOTE`, `PR_SECTION_LIMIT`),
the verdicts (`APPROVE`, `CHANGES_REQUESTED`), `ROLE_LABELS` (which also fixes the set of roles and the
per-role `--<role>-model` flags), and the exit codes (`EXIT_ERROR`, `EXIT_CHANGES_REQUESTED`,
`EXIT_INTERRUPTED`).

`OrchestratorError` is the one exception that ends a run with a message for the human. `HerdrError` adds
herdr's error code, which callers match on (`agent_not_found`, `agent_not_ready`, `timeout`, …).

### Role prompts

- `SPEC_PROMPT`, `BUILD_PROMPT` and `REVIEW_PROMPT` start each role.
- `FIX_PROMPT` (Builder) and `RECHECK_PROMPT` (Reviewer) are the follow-ups for round 2 onwards, sent to the
  same session.
- `CONTINUE_PROMPT` goes to a role relaunched into its saved Claude Code session after it had already been
  prompted for the current file.
- `REBUILD_NOTE` and `REREVIEW_NOTE` are appended for a Builder or Reviewer that starts a fresh session in a
  later round, since it has not seen the earlier rounds. `Workflow._build_prompts` and
  `Workflow._review_prompts` put these together.

### `Herdr`

A thin wrapper around the `herdr` CLI. `Herdr.call` runs one command and returns its `result` object;
`_herdr_error` turns a failed command into a `HerdrError`. With a machine, every command is prefixed with
`--machine NAME`; `Herdr.ssh_target` looks the machine up in `herdr machine list --json`, which always runs
locally.

The methods map one-to-one onto herdr commands: `create_workspace`, `split`, `rename_pane`, `start_agent`,
`prompt`, `wait`, `agent`, `status`, `workspace_exists`, `pane_exists`, `focus`, `close_workspace`, `notify`.
`agent` returns None for an agent that has exited, and `status` is built on it. `start_agent` returns False
when the agent is blocked on a startup dialog such as folder trust. `agent_session` reads the Claude Code
session id out of an agent record.

### `Host`

The filesystem and git checkout the agents work in: this machine, or, with an SSH target, the saved herdr
machine. `Host.run` wraps every command in `ssh -o BatchMode=yes TARGET` when there is a target, so a
password prompt fails fast instead of hanging.

- `read` returns None for a missing file (`MISSING_FILE_STATUS`); `write` writes to a temporary file and
  renames it into place, so a concurrent reader never sees half a `state.json`.
- `resolve_dir` expands `~` on the host, `git_head` and `git` run git, and `create_pr` runs `gh pr create`
  in the project directory.
- `run_states` returns every run's `state.json` with its age in seconds, measured by the host's own clock.

### `RunState` and helpers

`RunState` is what a run has done so far, saved as `state.json`. `RunState.from_dict` loads a saved state,
dropping unknown keys and letting the defaults fill fields that did not exist when it was written. `key` is
the six-hex suffix of the run id; it names the agents (`{role}-{key}`), labels the workspace and ends the
branch name. `dir`, `spec_path`, `build_path` and `review_path` give the run's files.

The helpers are pure functions:

- `new_run_id`: `YYYYMMDD-HHMMSS-<key>`.
- `parse_verdict`: the verdict on the review's first non-blank line, tolerating Markdown emphasis.
- `spec_title`: the `# ` heading on the spec's first non-blank line.
- `branch_name`: `orchestrator/<slug of the title>-<key>`.
- `pr_body`: the pull request description, holding the spec, the last build report and the last review,
  each cut to `PR_SECTION_LIMIT`.

### `Workflow`

Drives one run. It takes its collaborators as arguments (`herdr`, `host`, `notify`, `sleep`, `clock`,
`wallclock`), which is what lets the tests run it on fakes. The main methods:

- `run`: the phase machine below.
- `_claim` / `_release`: take ownership of the run, and give it up with the reason it stopped.
- `_check_repo`, `_require_clean`: refuse, before the interview, a project that could not become a pull
  request.
- `_prepare`: reuse the saved workspace or open a new one.
- `_collect_spec`, `_switch_to_branch`, `_build_and_review`, `_publish`, `_close_workspace`: the phases.
- `_turn`: one role's turn, from prompt to handoff file (see [Turns](#turns)).
- `_agent`, `_pane_for`, `_start`, `_wait_for_startup`, `_note_session`: getting a role's agent ready.
- `_save`, `_write_state`, `_heartbeat`: writing `state.json` (see [Heartbeat, owner and
  takeover](#heartbeat-owner-and-takeover)).

### CLI

- `parse_args`: the `run`, `resume` and `list` subcommands. `--machine` and `--cwd` come from a shared
  `target` parent parser; the run settings come from a `settings` parent parser, where an unset flag is None
  so that `resume` can tell it from a default.
- `role_models`: the model each role starts with, from `--model` and `--spec-model`, `--build-model`,
  `--review-model`.
- `run_health`: whether an unfinished run is `RUNNING` or `STALE`, and why.
- `print_runs`: the `list` output, one line per run plus its task, with a `resume` command for a stale run
  (`resume_command`).
- `find_run`: a run by its full id or its key.
- `resumable_state`: the saved state of the run to resume, with the flags given to `resume` applied, refusing
  a run that still looks alive unless `--force` is given.
- `main`: builds `Herdr` and `Host`, dispatches the subcommand, and maps the outcome to an exit code.
  `notify_locally` sends notifications to the herdr the orchestrator runs beside, where the human is watching.

## The phase machine

```
spec ──▶ build ⇄ review ──▶ publish ──▶ done
```

`Workflow.run` is re-entrant: it starts from the saved `phase` and `round`, and a new run is simply a resume
from `("spec", 0)` with no workspace. So `resume` and `run` share one code path, and every step first looks
for what an earlier orchestrator, or a role working while none was watching, already did.

| Phase | What happens | `RunState` fields recorded |
|---|---|---|
| (start) | `_claim` clears `error` and takes ownership. With `pull_request`, `_check_repo` checks the project before the interview. `_prepare` opens the workspace. | `owner`, `heartbeat_at`, `base_branch`, `workspace_id`, `root_pane` |
| `spec` | `_collect_spec` runs the interview, which has no deadline. Then `base` is taken from HEAD, `_switch_to_branch` creates the branch (with `pull_request`), and the run moves to build round 1. | `agents` (with `session`), `prompted`, `base`, `branch`, `phase`, `round` |
| `build` | `_build_and_review`: the Builder's turn for round N, then the phase becomes `review`. | `prompted`, `phase` |
| `review` | The Reviewer's turn; `parse_verdict` reads the verdict. APPROVE or the last round ends the loop; otherwise build round N + 1. | `verdict`, `phase`, `round` |
| `publish` | Only with `pull_request`. `_publish` commits the working tree, pushes the branch, opens the pull request (a draft unless approved), and switches back to `base_branch`. Each step is skipped when the saved state shows it done. | `pr_url` |
| `done` | `owner` is cleared, the human is notified, and with `pull_request` the workspace is closed. Resuming a `done` run only returns its verdict. | `owner` |

`base` is taken only once, at the step from spec to build. Every later phase, resumed or not, diffs against
it, even if the human commits the Builder's work in the meantime.

A failure anywhere goes through `_release`, which records it in `error` and clears `owner`; Ctrl-C is
recorded as `error = "interrupted"`. A failed run keeps its workspace and its branch, and `resume` clears
`error` and carries on at the saved phase.

`_switch_to_branch` runs only in the spec phase, so a resume in a later phase does not check which branch is
checked out; see Known issues in [ROADMAP.md](ROADMAP.md).

## Turns

`Workflow._turn` returns the handoff file that ends a role's turn. First it checks whether the file already
exists, because the role may have written it while no orchestrator was watching. An empty file, or a review
without a verdict, stops the run through `_reject`, which also clears `prompted`, so that a resume after the
human deletes the file prompts the role again.

Otherwise `_agent` makes sure the role's agent is running and says how it came to be ready:

| | Agent | Prompted with |
|---|---|---|
| `NEW` | The role's first agent in this run. | The role's prompt, or the fresh-session variant. |
| `ALIVE` | Still running from before. | Its prompt, unless `prompted` already names this file; then it is only waited on. |
| `RESUMED` | Had exited; relaunched with `claude --resume SESSION` from the saved `session`. | Its prompt, or `CONTINUE_PROMPT` if it was already prompted. |
| `RESTARTED` | Had exited and its session is gone; relaunched in a fresh session. | The fresh-session variant, with `REBUILD_NOTE` or `REREVIEW_NOTE`. |

`_pane_for` reuses the role's old pane if it survives, and otherwise splits one that does: the Builder to the
right of the root pane, the Reviewer below the Builder. `_prepare` opens a new workspace if the saved one is
gone.

After the prompt is delivered, `prompted` is saved, and `_turn` polls every `POLL_SECONDS` until the file
appears. **A turn ends when the handoff file appears, not when herdr reports the agent idle**: Claude Code
ends a turn while a background task it started is still running, and resumes when the task finishes, so
`idle` or `done` can come mid-work. While polling, `_turn` notifies the human once when the agent is
`blocked`, and once when a Builder or Reviewer has been idle for `STALL_SECONDS` without writing its file.
The Builder and the Reviewer have a deadline of `turn_timeout`; the Spec Collector has none. An agent that
exits without writing its file ends the run.

## The run directory

```
<project>/.orchestrator/
├── .gitignore            # "*": the directory ignores itself
└── runs/<run-id>/
    ├── state.json        # RunState
    ├── spec.md
    ├── build-1.md
    ├── review-1.md
    └── …
```

`_claim` writes `.orchestrator/.gitignore` when it is missing. It keeps the run files out of `git status`, so
the Reviewer's diff holds only the Builder's changes and the commit in `_publish` holds nothing else. The
directory lives in the project, on the agents' machine, so every access goes through `Host`.

## Heartbeat, owner and takeover

While a run is going, its orchestrator is recorded in `owner` (`host`, `pid`, `started_at`), and
`_write_state` stamps `heartbeat_at` from the wall clock on every write. `_heartbeat`, called from `_turn`'s
poll loop and between the bounded waits of `_wait_for_startup`, saves `state.json` at least every
`HEARTBEAT_SECONDS`. A failed heartbeat is logged and retried a full interval later; a failed phase save ends
the run.

`_save` re-reads `state.json` first. If `owner` there names another process, because a `resume --force` has
taken the run over, it raises `RunTakenOver`, and this orchestrator stops without saving anything, so it
cannot overwrite the new owner's state. `_claim` writes without that check: the caller has already decided
that any earlier owner is gone.

`run_health` decides, for a run that is neither `done` nor failed:

1. The owner ran on this host, its pid is gone (`pid_alive`), and the heartbeat is more than
   `HEARTBEAT_SECONDS` old: `STALE`.
2. `state.json` is more than `STALE_SECONDS` old: `STALE`.
3. Otherwise: `RUNNING`.

The age comes from `Host.run_states`, measured by the clock of the machine that holds the file, so clock skew
between machines cannot shift it. `print_runs` shows the result, and `resumable_state` refuses a `RUNNING` run
unless `--force` is given.

## Local and `--machine` operation

Without `--machine`, the orchestrator must run in a herdr pane (`HERDR_ENV=1`); otherwise herdr commands would
target whichever session is focused. `Host` runs commands locally.

With `--machine NAME`, `Herdr` forwards every command with `herdr --machine NAME`, and `Host` runs every file
and git command over `ssh -o BatchMode=yes` to the target that `Herdr.ssh_target` finds for that machine.
`Host` commands have a 60 s limit, or `NETWORK_TIMEOUT` for `git push` and `gh`. Notifications always go to the local
herdr, through `notify_locally`.

## Tests

`tests/test_orchestrator.py` runs the workflow on in-memory fakes, injected through `Workflow`'s arguments:

- `FakeHost`: an in-memory filesystem and git checkout. It records writes, git calls and pull requests, and
  counts files written outside `.orchestrator/` as uncommitted changes.
- `FakeHerdr`: plays each role from a script, one callable per turn, which writes the handoff file and returns
  the agent's status. It tracks live agents, sessions, workspaces and panes, so tests can make any of them
  disappear.
- `FakeClock`: time that passes only when the workflow sleeps; its hooks play the world meanwhile (a human
  answering, a file appearing, a takeover).

`make_workflow` builds a `Workflow` on these fakes, and `saved_run` and `resume` build one on a saved state as
an earlier orchestrator would have left it. `spec_turn`, `build_turn`, `review_turn`, `writes`, `idle` and
`recording` are the scripted turns.

| Test class | Covers |
|---|---|
| `TestParseVerdict` | `parse_verdict`. |
| `TestWorkflow` | New runs end to end: approval, the review loop, `max_rounds`, blocked and idle notifications, startup dialogs, exits, timeouts, invalid handoff files, a project outside git, agent arguments and per-role models. |
| `TestPullRequest` | The branch, commit, push and pull request; drafts; a dirty tree, a detached HEAD, no changes; closing the workspace; `--no-pr`. |
| `TestPullRequestText` | `spec_title`, `branch_name`, `pr_body`. |
| `TestHerdr` | `Herdr` against a mocked `subprocess.run`: results, errors, `--machine` forwarding, `start_agent`, `wait`, `ssh_target`. |
| `TestHost` | `Host`: SSH wrapping, `read`, `write`, `create_pr`, `git_head`, `run_states`, and a real-filesystem round trip. |
| `TestCLI` | `parse_args`, `role_models`, and `main` for `run` and `list`. |
| `TestResume` | `Workflow.run` on saved states: handoff files written meanwhile, `prompted`, resumed and fresh sessions, recovery prompts, `done`, invalid reviews, lost workspaces and panes, `RunState.from_dict`. |
| `TestResumePullRequest` | Resume and pull requests: runs saved before pull requests, resumed `publish` phases, `--no-pr` saved with the run. |
| `TestHeartbeat` | Heartbeats, `owner`, bounded startup waits, failed heartbeats, `RunTakenOver`, Ctrl-C, released runs. |
| `TestRunHealth` | `run_health` and `print_runs`. |
| `TestFindRun` | `find_run`. |
| `TestResumableState` | `resumable_state`: saved settings, overriding flags, `--force`, the resume's `--machine`. |
| `TestResumeCLI` | `main` for `resume`, and the resume hint after Ctrl-C. |
| `TestHerdrLookups` | `Herdr.agent`, `agent_session`, `workspace_exists`, `pane_exists`. |
| `TestAtomicWrite` | `Host.write` and `Host.run_states` on a real filesystem. |

## Tooling

- `pyproject.toml`: no runtime dependencies, standard library only, Python 3.13+, so `orchestrator.py` runs
  with a system `python3` without uv. The `dev` dependency group holds pytest and pytest-cov for
  `uv run pytest`; `pythonpath = ["."]` lets the tests import the module.
- `Jenkinsfile`: `uv sync --frozen`, then `uv run pytest` with coverage (`coverage.xml`) and JUnit results
  (`test-results.xml`), then a SonarQube analysis that reads both, then a quality gate that fails the build.
- `sonar-project.properties`: the SonarQube project key.
