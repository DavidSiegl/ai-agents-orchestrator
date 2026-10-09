# Architecture

A map of the code for someone about to change it. Usage is in the [README](../README.md), and the reasons behind
resume and stale-run detection are in [design.md](design.md). Code is named by symbol, not line, so this stays
true as lines move.

## `orchestrator.py`

One standard-library module, in these sections:

| Section | What it holds |
|---|---|
| Constants | Defaults (`DEFAULT_MAX_ROUNDS`, `DEFAULT_TURN_TIMEOUT`, `DEFAULT_MAX_QUALITY_ROUNDS`), intervals (`POLL_SECONDS`, `STALL_SECONDS`, `HEARTBEAT_SECONDS`, `STALE_SECONDS`, and for the quality gate `HTTP_TIMEOUT`, `CI_POLL_SECONDS`, `QUALITY_TIMEOUT`), `BRANCH_PREFIX` with `CLOSED_BRANCH_GRACE` (how long `prune` waits after a pull request is closed), `CI_REF_PREFIX`, `CI_ENV` (the credential variables), `ROLE_LABELS` (the default workflow's roles, and so the `--<role>-model` flags), `AGENT_KINDS` (the harnesses a role can run in, herdr's `--kind`) with `DEFAULT_AGENT` and the exit codes. `OrchestratorError` ends a run with a message; `HerdrError` carries herdr's error code. `child_env` is the environment of every process the orchestrator starts: its own without `CI_ENV`. `ci_credentials` fills what the environment lacks of `CI_ENV` from the file at `ci_env_path` in `config_dir`, parsed by `parse_env_file`, without putting it into the environment. |
| Role prompts | `SPEC_PROMPT`, `BUILD_PROMPT` and `REVIEW_PROMPT` start each role; `FIX_PROMPT` and `RECHECK_PROMPT` follow up in later rounds; `QUALITY_FIX_PROMPT` sends a quality file to the Builder; `CONTINUE_PROMPT` goes to a relaunched session; `REBUILD_NOTE` and `REREVIEW_NOTE` brief a fresh session on earlier turns; `QUALITY_PASSED_NOTE` and `QUALITY_UNRESOLVED_NOTE` end the Reviewer's prompt in a run with the gate. |
| Workflow definitions | `Step` and `Pipeline`, the reserved phases `QUALITY`, `PUBLISH` and `DONE`, `DEFAULT_WORKFLOW` built from the role prompts, the `WORKFLOWS` registry and `find_workflow`; see [Workflows](#workflows). |
| Workflow files | `parse_workflow` turns a definition (TOML, or one saved in `state.json`) into a `Pipeline`, resolving `use` and filling in labels; `load_workflow_file` reads one, `workflow_files` and `named_workflow` find them in `workflows_dir`, and `workflow_definition` and `workflow_toml` write one back. `run_pipeline` is a run's workflow: its saved definition, else the built-in one of its name. |
| Agent harnesses | `AgentSettings` is how each role's agent starts: the permission mode, and per role its model and harness; `Workflow` takes one and saves it in `state.json`. `launch_args` builds the arguments an agent starts with in its harness: `permission_args` translates `--permission-mode` through `PERMISSION_ARGS` (claude takes every mode as it is; `None` means the harness has no equivalent), `RESUME_ARGS` names each harness's way into a saved session (codex's `resume` is a subcommand, so it goes first), and the model goes last as `--model`. |
| `Herdr` | A thin wrapper around the `herdr` CLI, one method per command, forwarding `--machine` when one is given. `start_agent` takes the harness as herdr's `--kind`. |
| `Host` | The project's filesystem and git checkout, run locally or over `ssh -o BatchMode=yes`. `write` is atomic; `run_states` ages each `state.json` by the host's clock; `fast_forward`, `merge_upstream` and `abort_merge` keep the branch up to date with `origin`. For the quality gate, `snapshot` commits the working tree through a temporary index without moving a ref, `push_ref`, `delete_remote_branch` and `remote_branches` handle the throwaway branches, and `change_diff` is the `git diff -U0` that `changed_lines` parses. For `prune`, `pull_request` reads a pull request's state, head commit and closing time with `gh` (`gh_run` runs gh in the project directory, and `GhUnusable` means gh is missing or logged out), `remote_branch_commit` and `delete_remote_branch_at` read and delete `origin`'s branch under a lease, `delete_tracking_ref` drops `origin/<branch>`, `branch_commit`, `has_commit`, `is_ancestor`, `commits_beyond` and `delete_branch` decide on and delete the local branch, and `checked_out_branches` parses `git worktree list --porcelain`. `write` with `keep_mtime` keeps a `state.json`'s age. |
| `CI` | Jenkins's quality job and SonarQube over HTTP, from the orchestrator's machine, one method per call, over an injected `urlopen`. The credentials come from the environment or `ci.env` and go only into the `Authorization` header; `mask` replaces the tokens in text from either server. `CIError` carries the HTTP status, and `transient` says whether a retry may help. |
| `RunState` and helpers | `RunState` is `state.json`; it saves `permission_mode` as given and each role's harness in `agent_kinds`, and `from_dict` reads a 0.3.0 state's `agent_args` as its permission mode. The pure helpers are `parse_verdict`, `parse_gate`, `spec_title`, `branch_name`, `pr_body` (with `verdict_line`; `commit_note` for the commit message), and for the quality file `changed_lines`, `edited_config`, `issue_severity` and `quality_report`. |
| `Workflow` | Drives one run through its `pipeline`. Its collaborators (`herdr`, `host`, `ci`, `notify`, and `clocks`, a `Clocks` of `sleep`, `monotonic` and `wall`) are arguments, which is what lets the tests use fakes. |
| CLI | `parse_args` (which turns no arguments into `gui` when `gui_by_default` finds a display), `offered_workflows`, `role_models` (with `role_model_arg`), `role_agents` (with `role_agent_arg`), `print_workflows`, `workflow_arg`, `run_health`, `print_runs` (with `run_rows`, `run_outcome` and `run_round`), `find_run`, `resumable_state`, `prune_branches` (with `is_prune_candidate`, `prune_run`, `pull_request_pending`, `prune_remote`, `prune_local` and `record_cleanup`, returning a `PrunedRun` per run), which `prune` prints with `print_pruned` and `run` calls first through `sweep_branches`, which logs `sweep_summary`, and `main` with `runs_command` (`list`, `show` and `prune`), `workflows_command`, `connect`, `new_state`, `resumed_state` and `finish`. |
| GUI | `gui_command` and `RunWindow`, a tkinter window over the CLI; see [GUI](#gui). |

## The phase machine

```
spec ──▶ build ⇄ review ──▶ publish ──▶ done
          ⇅
       quality   (with --quality-gate)
```

That is the default workflow's; in general a run's `phase` is the id of its current step, or `quality`,
`publish` or `done`. `Workflow.run` starts from the saved `phase` and `round`; a new run is a resume from the
first step. Before anything else, `_check_start` checks that the run can finish: in round 0 (before its first
editing step), `_check_gate` runs `CI.preflight` and checks the checkout, and `_check_repo` fast-forwards the
base branch; a resume that starts in the gated step or in `quality` checks the CI credentials instead. Then
`_walk` takes the steps in order:

- Each step's turn is `_take_turn` and `_step_turn`, with the prompts `_prompts` fills in through
  `_placeholder`. A human-paced step, the default's `spec`, has no timeout or stall notice and focuses its pane.
- `_goto` makes the next step current and saves it. Before the first editing step, the default's `build`,
  `_switch_to_branch` fast-forwards the base branch again and creates the branch, `base` is taken from HEAD and
  round 1 starts.
- `quality`, after each turn of the quality-gated step when the gate is on: `_quality` writes `quality-N-Q.md`.
  It creates the run's SonarQube project and analyses the base once per run, then `_analyse` snapshots, pushes
  and has Jenkins analyse the change, saving each id of the chain in `ci` as it learns it, so a resume continues
  from the furthest one. `_poll` retries a failed request until `QUALITY_TIMEOUT` and keeps the heartbeat going.
  A gate that does not pass goes back to the gated step, whose answer is `build-N-qQ.md` in the default.
- The verdict step, the default's `review`, records `verdict`; `CHANGES_REQUESTED` goes back to its loop target
  in the next round until `max_rounds`, resetting `quality_round`. A workflow without a verdict step records
  `FINISHED` once its last step is done, which counts as `APPROVE` (`SUCCEEDED`) for the exit status and the
  draft, while `verdict_line` and `commit_note` say that no agent reviewed the change. With the gate, a last
  quality round that did not pass (`_gate_passed`) records `QUALITY_GATE_FAILED` instead, which does not count.
- `publish`, only with a pull request: `_publish` commits, merges `origin`'s base branch in (recording
  `conflicts` and aborting when it conflicts), pushes and records `pr_url`, skipping any step already done.
- `done`: `_clean_up_quality` deletes the run's throwaway branches and SonarQube project, `owner` is cleared,
  and with a pull request `_close_workspace` closes the workspace.

A failure goes through `_release`, which records `error`. Each role's turn is `_turn`: it takes a handoff file
that already exists, and otherwise gets the agent ready through `_agent` (`NEW`, `ALIVE`, `RESUMED` or
`RESTARTED`; an exited agent is resumed into its saved session only if its record's `kind` is the role's
harness now, and otherwise starts fresh), prompts it with what `_prompt_for` picks, and polls for the file in `_await_handoff`, where
`_tell_human` notifies the human of a blocked or stalled agent once. A verdict step's file without a `VERDICT`
first line goes to `_retry`, which moves it aside and, once per file (`retried`), prompts the role again with
`RETRY_VERDICT_PROMPT`; `retrying` names the file while that turn is under way, so a resume sends the retry prompt,
not the step's own. `_save` checks the owner before every write and raises
`RunTakenOver` after a takeover; `_heartbeat` keeps `state.json` fresh while a turn runs.

## Workflows

A workflow is data: a `Pipeline` with a name, its roles in pane order (role key to pane label) and a tuple of
`Step`s. A step names its id (also its phase), its role, its handoff file (fixed like `spec.md`, or one per
round like `build-{n}.md`), a first prompt, a later-round prompt (`again`) and a note for a fresh session in a
later round (`fresh_note`), and the flags `human_paced`, `edits`, `loop_to` (which makes it the verdict step)
and `quality_gated`. Prompts take `{task}`, `{cwd}`, `{n}`, `{change}`, `{path}` and, for every step `X`,
`{X_path}`, `{prev_X_path}` and `{earlier_X_paths}`. `Pipeline` validates itself when constructed and raises
`ValueError` naming the problem, such as a duplicate id, a loop target that does not come earlier, two verdict
loops, two gated steps or an unknown placeholder.

The quality-gated step is the one the gate follows: its answer to quality round Q inserts `-qQ` before its
file's extension, other steps' prompts name its last report of the round, and `QUALITY_PASSED_NOTE` or
`QUALITY_UNRESOLVED_NOTE` ends the verdict step's prompt. The contract step, a first step with a fixed file
that does not edit, titles the branch and the pull request; without one the task does. The pull request body
holds the contract, the last editing step's last report, the last quality file and the verdict file.

`WORKFLOWS` holds only `DEFAULT_WORKFLOW`. Other workflows are TOML files: `run --workflow NAME` finds
`NAME.toml` in `workflows_dir` (`~/.config/ai-agents-orchestrator/workflows/`), and `run --workflow-file` takes
one by path; the README's [Your own workflows](../README.md#your-own-workflows) gives the format. A file's
step can `use` a default step and override some of its fields; a role without a label keeps the default
workflow's label for its key, or else is named after the key; a role's `model` sits under every model flag,
and its `agent` (`Pipeline.agents`, one of `AGENT_KINDS`) under `--agent` and `--role-agent`. A
file cannot take a built-in workflow's name. `Pipeline` also rejects a prompt or file that could not be filled
in at run time (`_fill_problem`), and a handoff file outside the run directory or named like the run's own
`state.json` and `quality-*` files, and two steps whose files can share a name in some round (`Step.name_forms`,
`_shared_name` and `_collision_problem`, an exact check). `state.json` saves
the workflow's name and, for one that is not built in, its canonical definition (`workflow_definition`), which
a resume rebuilds the `Pipeline` from, so editing or deleting the file does not change a started run. One saved
before workflows existed loads as `default`. Panes: the first role takes the root pane, the second
splits right of it and each further one splits down from the one before; a workflow file's roles are in the
order its steps first use them. The tests define three more workflows
that run on this engine: Builder ⇄ Reviewer with the task as the contract, Spec Collector → Test Writer →
Builder ⇄ Reviewer, and an Implementer ⇄ Reviewer whose gated step writes `impl-N.md`.

## GUI

`gui_command` imports tkinter only when it runs, so the CLI and the tests work without tkinter or a display;
without tkinter it exits 1 with `tk_install_hint`, and without a display with Tk's error. `RunWindow` is the
window, given the tkinter modules (`tk`, `ttk`, `filedialog`, `messagebox`) as `ui`, so the tests drive it on
fakes:

- The form is a `RunForm`; `run_args` turns it into the `run` command's arguments, the task last after `--`,
  and `resume_args` builds `resume <run id>` with the `target_args` of the project the list shows.
- A command runs as a `RunProcess`, a child of `self_command` (the PyInstaller binary when frozen, else the
  Python running the `.pyz` or `orchestrator.py`) in a session of its own, its stdout and stderr read on a
  thread. Stop and a confirmed close call `interrupt`, which sends SIGINT to its process group as Ctrl-C does,
  so the run ends through `main`'s `KeyboardInterrupt` handler: status 130 and the resume hint. One runs at a
  time.
- The run list is `run_rows`, what `print_runs` prints, of `project_runs`: the runs `list` reads, without
  `connect`'s herdr-pane check, since listing sends no herdr command. It is read on a background thread.
- Tk is used only from its own thread: `_poll`, every `GUI_POLL_MS`, takes up the output and the listings.

## Tests

`tests/test_orchestrator.py` runs `Workflow` on in-memory fakes: `FakeHost` (filesystem and git, played one git
command at a time so `Host`'s own git steps run on it, with origin scripted by its attributes), `FakeHerdr`
(scripted roles, one callable per turn), `FakeCI` (Jenkins and SonarQube behind `urlopen`, each triggered build
playing a scripted `outcome`) and `FakeClock` (time that passes only when the workflow sleeps).
`make_workflow` builds a new run on them; `saved_run` and `resume` build one from a saved state. Test classes
are named after what they cover, e.g. `TestWorkflow`, `TestPullRequest`, `TestResume`, `TestHeartbeat`,
`TestRunHealth`, `TestQualityGate`, `TestQualityResume` and `TestOtherWorkflows`; `TestDefaultWorkflowPrompts`
compares the default's prompts with those recorded from commit `d6bb100`, except for `SPEC_PROMPT`'s
"Separate agent sessions"; `TestHarnesses`, `TestHarnessFlags` and `TestWorkflowFileAgents` cover the harnesses,
on `FakeHerdr`, which records each agent's kind in `kinds` and finds a resumed session by its harness's syntax; `gated` and `resume_gated` build a run with the gate
on a `FakeCI`. `TestHostGit` and `TestHostSnapshot` run on real git in a temporary directory. `TestRunWindow`
and `TestGuiCommand` drive the GUI on fakes of the tkinter modules (`fake_ui`, `fake_tkinter`, with `FakeRoot`
running `after` callbacks on `tick`), and `TestRunProcess` runs real child processes.

## Tooling

- `pyproject.toml`: no runtime dependencies, Python 3.13+; pytest and pytest-cov in the `dev` group, PyInstaller
  in the `build` group.
- `packaging/build-binary.sh`: builds `dist/orchestrator-<os>-<arch>` (`linux-x86_64`, `macos-arm64`), a
  PyInstaller one-file binary with Python and tkinter inside, and its `.sha256`, then smoke-tests it: `--help`,
  `workflows` lists `default`, and `gui --smoke-test` where there is a display. On Linux it builds with the
  system `python3` (`python3-tk` installed) in `build/binary-venv`, not uv's Python, whose Tk lacks Xft and so
  draws every font in one fixed bitmap size; it refuses a Tk without libXft.
- `Jenkinsfile`: `uv sync --frozen`, `uv run pytest` with branch coverage of `orchestrator`, a Package stage that
  runs `packaging/build-binary.sh` on every build but a quality build, and on `main` or in a quality build a
  SonarQube analysis and a quality gate. On `main` a Release stage publishes a new `pyproject.toml` version as a
  GitHub release, with `orchestrator.py` attached as the zipapp `orchestrator.pyz` and the Linux binary, each
  with its `.sha256`, in one `gh release create`. Its parameters `GIT_REF`, `SONAR_PROJECT_KEY` and
  `SONAR_PROJECT_VERSION` are for quality builds: with `SONAR_PROJECT_KEY` set, a red gate does not abort the
  pipeline but marks it UNSTABLE.
- `.github/workflows/macos-binary.yml`, the only GitHub Action: Jenkins has no Mac, so when a release is
  published, or by hand for an existing tag, it runs `packaging/build-binary.sh` on `macos-latest` and uploads
  `orchestrator-macos-arm64` and its `.sha256` to that release with `gh release upload --clobber`. With only
  `contents: write`, it never creates a release or a tag; everything else stays in Jenkins.
- `sonar-project.properties`: the SonarQube project key, the sources (`orchestrator.py`) and the tests (`tests`).

How to set Jenkins up for it is in [quality-gate.md](quality-gate.md).
