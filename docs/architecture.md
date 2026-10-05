# Architecture

A map of the code for someone about to change it. Usage is in the [README](../README.md), and the reasons behind
resume and stale-run detection are in [design.md](design.md). Code is named by symbol, not line, so this stays
true as lines move.

## `orchestrator.py`

One standard-library module, in these sections:

| Section | What it holds |
|---|---|
| Constants | Defaults (`DEFAULT_MAX_ROUNDS`, `DEFAULT_TURN_TIMEOUT`, `DEFAULT_MAX_QUALITY_ROUNDS`), intervals (`POLL_SECONDS`, `STALL_SECONDS`, `HEARTBEAT_SECONDS`, `STALE_SECONDS`, and for the quality gate `HTTP_TIMEOUT`, `CI_POLL_SECONDS`, `QUALITY_TIMEOUT`), `BRANCH_PREFIX`, `CI_REF_PREFIX`, `CI_ENV` (the credential variables), `ROLE_LABELS` (the set of roles, and so the `--<role>-model` flags) and the exit codes. `OrchestratorError` ends a run with a message; `HerdrError` carries herdr's error code. `child_env` is the environment of every process the orchestrator starts: its own without `CI_ENV`. `ci_credentials` fills what the environment lacks of `CI_ENV` from the file at `ci_env_path`, parsed by `parse_env_file`, without putting it into the environment. |
| Role prompts | `SPEC_PROMPT`, `BUILD_PROMPT` and `REVIEW_PROMPT` start each role; `FIX_PROMPT` and `RECHECK_PROMPT` follow up in later rounds; `QUALITY_FIX_PROMPT` sends a quality file to the Builder; `CONTINUE_PROMPT` goes to a relaunched session; `REBUILD_NOTE` and `REREVIEW_NOTE` brief a fresh session on earlier turns; `QUALITY_PASSED_NOTE` and `QUALITY_UNRESOLVED_NOTE` end the Reviewer's prompt in a run with the gate. |
| `Herdr` | A thin wrapper around the `herdr` CLI, one method per command, forwarding `--machine` when one is given. |
| `Host` | The project's filesystem and git checkout, run locally or over `ssh -o BatchMode=yes`. `write` is atomic; `run_states` ages each `state.json` by the host's clock; `fast_forward`, `merge_upstream` and `abort_merge` keep the branch up to date with `origin`. For the quality gate, `snapshot` commits the working tree through a temporary index without moving a ref, `push_ref`, `delete_remote_branch` and `remote_branches` handle the throwaway branches, and `change_diff` is the `git diff -U0` that `changed_lines` parses. |
| `CI` | Jenkins's quality job and SonarQube over HTTP, from the orchestrator's machine, one method per call, over an injected `urlopen`. The credentials come from the environment or `ci.env` and go only into the `Authorization` header; `mask` replaces the tokens in text from either server. `CIError` carries the HTTP status, and `transient` says whether a retry may help. |
| `RunState` and helpers | `RunState` is `state.json`. The pure helpers are `parse_verdict`, `parse_gate`, `spec_title`, `branch_name`, `pr_body`, and for the quality file `changed_lines`, `edited_config`, `issue_severity` and `quality_report`. |
| `Workflow` | Drives one run. Its collaborators (`herdr`, `host`, `ci`, `notify`, `sleep`, `clock`, `wallclock`) are arguments, which is what lets the tests use fakes. |
| CLI | `parse_args`, `role_models`, `run_health`, `print_runs`, `find_run`, `resumable_state` and `main`. |

## The phase machine

```
spec ──▶ build ⇄ review ──▶ publish ──▶ done
          ⇅
       quality   (with --quality-gate)
```

`Workflow.run` starts from the saved `phase` and `round`; a new run is a resume from `spec`. Each phase is one
method:

- `spec`: with the gate, `_check_gate` runs `CI.preflight` and checks the checkout; `_check_repo`
  fast-forwards the base branch before the interview, then `_collect_spec`, then `_switch_to_branch`
  fast-forwards it again and creates the branch, and `base` is taken from HEAD.
- `build`, `quality` and `review`: `_build_and_review` alternates them until APPROVE or the last round,
  recording `round`, `quality_round` and `verdict`. The Builder writes `build-N.md`, and in a fix turn
  `build-N-qQ.md`.
- `quality`, only with the gate: `_quality` writes `quality-N-Q.md`. It creates the run's SonarQube project and
  analyses the base once per run, then `_analyse` snapshots, pushes and has Jenkins analyse the change, saving
  each id of the chain in `ci` as it learns it, so a resume continues from the furthest one. `_poll` retries a
  failed request until `QUALITY_TIMEOUT` and keeps the heartbeat going.
- `publish`, only with a pull request: `_publish` commits, merges `origin`'s base branch in (recording
  `conflicts` and aborting when it conflicts), pushes and records `pr_url`, skipping any step already done.
- `done`: `_clean_up_quality` deletes the run's throwaway branches and SonarQube project, `owner` is cleared,
  and with a pull request `_close_workspace` closes the workspace.

A failure goes through `_release`, which records `error`. Each role's turn is `_turn`: it takes a handoff file
that already exists, and otherwise gets the agent ready through `_agent` (`NEW`, `ALIVE`, `RESUMED` or
`RESTARTED`), prompts it and polls for the file. `_save` checks the owner before every write and raises
`RunTakenOver` after a takeover; `_heartbeat` keeps `state.json` fresh while a turn runs.

## Tests

`tests/test_orchestrator.py` runs `Workflow` on in-memory fakes: `FakeHost` (filesystem and git, played one git
command at a time so `Host`'s own git steps run on it, with origin scripted by its attributes), `FakeHerdr`
(scripted roles, one callable per turn), `FakeCI` (Jenkins and SonarQube behind `urlopen`, each triggered build
playing a scripted `outcome`) and `FakeClock` (time that passes only when the workflow sleeps).
`make_workflow` builds a new run on them; `saved_run` and `resume` build one from a saved state. Test classes
are named after what they cover, e.g. `TestWorkflow`, `TestPullRequest`, `TestResume`, `TestHeartbeat`,
`TestRunHealth`, `TestQualityGate` and `TestQualityResume`; `gated` and `resume_gated` build a run with the gate
on a `FakeCI`. `TestHostGit` and `TestHostSnapshot` run on real git in a temporary directory.

## Tooling

- `pyproject.toml`: no runtime dependencies, Python 3.13+; pytest and pytest-cov in the `dev` group.
- `Jenkinsfile`: `uv sync --frozen`, `uv run pytest` with branch coverage of `orchestrator`, and on `main` or
  in a quality build a SonarQube analysis and a quality gate. Its parameters `GIT_REF`, `SONAR_PROJECT_KEY` and
  `SONAR_PROJECT_VERSION` are for quality builds: with `SONAR_PROJECT_KEY` set, a red gate does not abort the
  pipeline but marks it UNSTABLE.
- `sonar-project.properties`: the SonarQube project key, the sources (`orchestrator.py`) and the tests (`tests`).

How to set Jenkins up for it is in [quality-gate.md](quality-gate.md).
