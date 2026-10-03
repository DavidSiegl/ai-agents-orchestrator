# Architecture

A map of the code for someone about to change it. Usage is in the [README](../README.md), and the reasons behind
resume and stale-run detection are in [design.md](design.md). Code is named by symbol, not line, so this stays
true as lines move.

## `orchestrator.py`

One standard-library module, in these sections:

| Section | What it holds |
|---|---|
| Constants | Defaults (`DEFAULT_MAX_ROUNDS`, `DEFAULT_TURN_TIMEOUT`), intervals (`POLL_SECONDS`, `STALL_SECONDS`, `HEARTBEAT_SECONDS`, `STALE_SECONDS`), `BRANCH_PREFIX`, `ROLE_LABELS` (the set of roles, and so the `--<role>-model` flags) and the exit codes. `OrchestratorError` ends a run with a message; `HerdrError` carries herdr's error code. |
| Role prompts | `SPEC_PROMPT`, `BUILD_PROMPT` and `REVIEW_PROMPT` start each role; `FIX_PROMPT` and `RECHECK_PROMPT` follow up in later rounds; `CONTINUE_PROMPT` goes to a relaunched session; `REBUILD_NOTE` and `REREVIEW_NOTE` brief a fresh session on earlier rounds. |
| `Herdr` | A thin wrapper around the `herdr` CLI, one method per command, forwarding `--machine` when one is given. |
| `Host` | The project's filesystem and git checkout, run locally or over `ssh -o BatchMode=yes`. `write` is atomic; `run_states` ages each `state.json` by the host's clock; `fast_forward`, `merge_upstream` and `abort_merge` keep the branch up to date with `origin`. |
| `RunState` and helpers | `RunState` is `state.json`. The pure helpers are `parse_verdict`, `spec_title`, `branch_name` and `pr_body`. |
| `Workflow` | Drives one run. Its collaborators (`herdr`, `host`, `notify`, `sleep`, `clock`, `wallclock`) are arguments, which is what lets the tests use fakes. |
| CLI | `parse_args`, `role_models`, `run_health`, `print_runs`, `find_run`, `resumable_state` and `main`. |

## The phase machine

```
spec ──▶ build ⇄ review ──▶ publish ──▶ done
```

`Workflow.run` starts from the saved `phase` and `round`; a new run is a resume from `spec`. Each phase is one
method:

- `spec`: `_check_repo` fast-forwards the base branch before the interview, then `_collect_spec`, then
  `_switch_to_branch` fast-forwards it again and creates the branch, and `base` is taken from HEAD.
- `build` and `review`: `_build_and_review` alternates the two until APPROVE or the last round, recording
  `round` and `verdict`.
- `publish`, only with a pull request: `_publish` commits, merges `origin`'s base branch in (recording
  `conflicts` and aborting when it conflicts), pushes and records `pr_url`, skipping any step already done.
- `done`: `owner` is cleared, and with a pull request `_close_workspace` closes the workspace.

A failure goes through `_release`, which records `error`. Each role's turn is `_turn`: it takes a handoff file
that already exists, and otherwise gets the agent ready through `_agent` (`NEW`, `ALIVE`, `RESUMED` or
`RESTARTED`), prompts it and polls for the file. `_save` checks the owner before every write and raises
`RunTakenOver` after a takeover; `_heartbeat` keeps `state.json` fresh while a turn runs.

## Tests

`tests/test_orchestrator.py` runs `Workflow` on in-memory fakes: `FakeHost` (filesystem and git, played one git
command at a time so `Host`'s own git steps run on it, with origin scripted by its attributes), `FakeHerdr`
(scripted roles, one callable per turn) and `FakeClock` (time that passes only when the workflow sleeps).
`make_workflow` builds a new run on them; `saved_run` and `resume` build one from a saved state. Test classes
are named after what they cover, e.g. `TestWorkflow`, `TestPullRequest`, `TestResume`, `TestHeartbeat` and
`TestRunHealth`.

## Tooling

- `pyproject.toml`: no runtime dependencies, Python 3.13+; pytest and pytest-cov in the `dev` group.
- `Jenkinsfile`: `uv sync --frozen`, `uv run pytest` with branch coverage of `orchestrator`, a SonarQube
  analysis and a quality gate. Its parameters `GIT_REF`, `SONAR_PROJECT_KEY` and `SONAR_PROJECT_VERSION` are for
  quality builds: with `SONAR_PROJECT_KEY` set, a red gate does not abort the pipeline but marks it UNSTABLE.
- `sonar-project.properties`: the SonarQube project key, the sources (`orchestrator.py`) and the tests (`tests`).

How to set Jenkins up for it is in [quality-gate.md](quality-gate.md).
