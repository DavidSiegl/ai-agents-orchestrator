# Architecture

A map of the code for someone about to change it. Usage is in the [README](../README.md), and the reasons behind
resume and stale-run detection are in [design.md](design.md). Code is named by symbol, not line, so this stays
true as lines move.

## `orchestrator.py`

One standard-library module, in these sections:

| Section | What it holds |
|---|---|
| Constants | Defaults (`DEFAULT_MAX_ROUNDS`, `DEFAULT_TURN_TIMEOUT`, `DEFAULT_MAX_QUALITY_ROUNDS`), intervals (`POLL_SECONDS`, `STALL_SECONDS`, `HEARTBEAT_SECONDS`, `STALE_SECONDS`, and for the quality gate `HTTP_TIMEOUT`, `CI_POLL_SECONDS`, `QUALITY_TIMEOUT`), `BRANCH_PREFIX` with `CLOSED_BRANCH_GRACE` (how long `prune` waits after a pull request is closed), `CI_REF_PREFIX`, `CI_ENV` (the credential variables), `GIT_PATHSPEC_ENV` (git's pathspec-syntax switches), `ROLE_LABELS` (the default workflow's roles, and so the `--<role>-model` flags), `AGENT_KINDS` (the harnesses a role can run in, herdr's `--kind`) with `DEFAULT_AGENT` and the exit codes. `OrchestratorError` ends a run with a message; `HerdrError` carries herdr's error code. `child_env` is the environment of every process the engine starts: its own without `CI_ENV` and `GIT_PATHSPEC_ENV`, which the agents' shells do not inherit, though they read the user's startup files. The gate's processes get the gate's own copy, `tool_env`. `ci_credentials` fills what the environment lacks of `CI_ENV` from the file at `ci_env_path` in `config_dir`, parsed by `parse_env_file`, without putting it into the environment. |
| Role prompts | `SPEC_PROMPT`, `BUILD_PROMPT` and `REVIEW_PROMPT` start each role; `FIX_PROMPT` and `RECHECK_PROMPT` follow up in later rounds; `QUALITY_FIX_PROMPT` sends a quality file to the Builder; `CONTINUE_PROMPT` goes to a relaunched session; `REBUILD_NOTE` and `REREVIEW_NOTE` brief a fresh session on earlier turns; `QUALITY_PASSED_NOTE`, `QUALITY_UNRESOLVED_NOTE` and `QUALITY_STUCK_NOTE` end the Reviewer's prompt in a run with the gate, and `LOCKED_NOTE` when locked files were restored. `Prompts` holds every one of these texts by its constant's name, `BUILT_IN_PROMPTS` the constants themselves; `load_prompts` replaces each that a prompt file `NAME.md` in `prompts_dir` names (`prompt_files` lists them), refusing one whose placeholders differ from the built-in text's (`prompt_problem`). A run saves `Prompts.overrides` in `RunState.prompt_overrides`, so a resume sends the texts it started with (`RunState.prompts`, `saved_prompts`); `Workflow` reads every follow-up and note from its `prompts`. |
| Workflow definitions | `Step` and `Pipeline`, the reserved phases `QUALITY`, `PUBLISH` and `DONE`, `default_workflow`, which builds the default workflow from a `Prompts`, `DEFAULT_WORKFLOW` built from the built-in ones, the `WORKFLOWS` registry and `find_workflow`, which words the default by a run's prompts (`is_built_in` tells whether a run need not save its definition); see [Workflows](#workflows). |
| Workflow files | `parse_workflow` turns a definition (TOML, or one saved in `state.json`) into a `Pipeline`, resolving `use` against the default workflow as the run's prompts word it and filling in labels; `load_workflow_file` reads one, `workflow_files` and `named_workflow` find them in `workflows_dir`, and `workflow_definition` and `workflow_toml` write one back. `run_pipeline` is a run's workflow: its saved definition, else the built-in one of its name. |
| Agent harnesses | `AgentSettings` is how each role's agent starts: the permission mode, and per role its model and harness; `Workflow` takes one and saves it in `state.json`. `launch_args` builds the arguments an agent starts with in its harness: `permission_args` translates `--permission-mode` through `PERMISSION_ARGS` (claude takes every mode as it is; `None` means the harness has no equivalent), `RESUME_ARGS` names each harness's way into a saved session (codex's `resume` is a subcommand, so it goes first), the model goes after it as `--model`, and `ADD_DIR_ARGS` grants a worktree run's agents its run directory last (claude and codex `--add-dir`, gemini `--include-directories`; opencode and pi have no such flag). |
| `Herdr` | A thin wrapper around the `herdr` CLI, one method per command, forwarding `--machine` when one is given. `start_agent` takes the harness as herdr's `--kind`. |
| `Host` | The project's filesystem and git checkout, run locally or over `ssh -o BatchMode=yes`. `write` is atomic; `run_states` ages each `state.json` by the host's clock; `fast_forward`, `merge_upstream` and `abort_merge` keep the branch up to date with `origin`. For the quality gate, `snapshot` commits the working tree through a temporary index without moving a ref, `push_ref`, `delete_remote_branch` and `remote_branches` handle the throwaway branches, and `change_diff` is the `git diff -U0` that `added_lines` parses. For `prune`, `pull_request` reads a pull request's state, head commit and closing time with `gh` (`gh_run` runs gh in the project directory, and `GhUnusable` means gh is missing or logged out), `remote_branch_commit` and `delete_remote_branch_at` read and delete `origin`'s branch under a lease, `delete_tracking_ref` drops `origin/<branch>`, `branch_commit`, `has_commit`, `is_ancestor`, `commits_beyond` and `delete_branch` decide on and delete the local branch, and `checked_out_branches` parses `git worktree list --porcelain`. For `run --worktree`, `add_worktree` checks a commit out on a detached HEAD in a new worktree, `is_worktree` tells whether a path is one of the repository's worktrees and still there (by its physical path, as git lists it), and `remove_worktree` removes one without `--force`. `write` with `keep_mtime` keeps a `state.json`'s age. |
| `CI` | Jenkins's quality job over HTTP, from the orchestrator's machine, one method per call, over an injected `urlopen`. The credentials come from the environment or `ci.env` and go only into the `Authorization` header; `mask` replaces the token in text from Jenkins. `CIError` carries the HTTP status, and `transient` says whether a retry may help. |
| `RunState` and helpers | `RunState` is `state.json`; it saves `permission_mode` as given and each role's harness in `agent_kinds`, and `from_dict` reads a 0.3.0 state's `agent_args` as its permission mode. `worktree` is a `--worktree` run's worktree, set from the start (`None` in place, as for a state saved before it); `work_dir` is that, or else `cwd`, and is where the agents work and every git step of the run runs, while `cwd` stays the project, which holds the run directory (`dir`) and `.orchestrator/.gitignore`. The pure helpers are `parse_verdict`, `spec_title`, `branch_name`, `pr_body` (with `verdict_line`; `commit_note` for the commit message), and for the quality file `added_lines`, `build_failed_report`, `leak_report`, `parse_gate` and `gate_failures`; `changed_lines`, `edited_config` and `quality_report` are `gate.py`'s. |
| `Workflow` | Drives one run through its `pipeline`. Its collaborators (`herdr`, `host`, `ci`, `notify`, and `clocks`, a `Clocks` of `sleep`, `monotonic` and `wall`) are arguments, which is what lets the tests use fakes. |
| CLI | `parse_args` (which turns no arguments into `gui` when `gui_by_default` finds a display), `offered_workflows`, `role_models` (with `role_model_arg`), `role_agents` (with `role_agent_arg`), `print_workflows` (with `print_prompt_overrides`, the prompt files a new run would take), `workflow_arg`, `run_health`, `print_runs` (with `run_rows`, `run_outcome` and `run_round`, which format the `run_record` of each run that `list --json` prints, and `fill_pr_states`, which adds `--pr-state`'s pull request states to those records), `find_run`, `resumable_state`, `prune_branches` (with `is_prune_candidate`, `prune_run`, `pull_request_pending`, `prune_remote`, `prune_local` and `record_in_state`, returning a `PrunedRun` per run), which `prune` prints with `print_pruned` and `run` calls first through `sweep_branches`, which logs `sweep_summary`, `close_run` (with `close_run_workspace`, `discard_run_worktree`, `discard_run_branch` and `record_in_state`), and `main` with `herdrless_command` (`workflows`, `gui`, `serve`, `judged-in-quality` and `doctor`, which need no herdr pane; `judged_in_quality_command` asks `judged_in_quality` whether a build of the quality job, from `CI.retained_builds`, already judged `HEAD`; `doctor_command` prints with `report_checks` the `Check` of each requirement that `check_setup` makes, through `Host` and `Herdr` and so on `--machine` too, a failed one never stopping the rest), `runs_command` (`list`, `show`, `prune` and `close`), `workflows_command`, `connect`, `new_state` (with `flagged_workflow`, also `drain`'s), `resumed_state` and `finish`. |
| GUI | `gui_command` and `RunWindow`, a tkinter window over the CLI; see [GUI](#gui). |
| serve | `serve_command`, `RunServer` with `RunRequestHandler`, `ProjectRuns` and `RunWatch`: a project's runs, read-only over HTTP, for the desktop app in `ui/`; see [serve](#serve). |

## `gate.py`

The quality gate, a standard-library program of its own: `python gate.py --base REV` judges the change in the
working tree it runs in and writes the quality file ([quality gate](quality-gate.md)). The orchestrator never
imports it; Jenkins runs it, and the orchestrator reads the file it writes, with its own copy of the parts of the
format it needs ([the quality file as a contract](quality-gate.md#the-quality-file-as-a-contract)). In order:
the tunables (`QUALITY_ISSUE_LIMIT`, `GATE_MIN_COVERAGE`, `MUTATION_BUDGET`, ...); its own git helpers (`_git`,
`_git_run`) and `GateError`; `quality_report`, which writes the file; the linters (`GATE_TOOLS`), `GateChange`,
`GateCheck` and `judge_change`, which runs them; the checks `gate_checks` lists, from `weakened_tests` to
`vague_names`; and `gate_command`, `parse_args` and `main`.

## The phase machine

```
spec ──▶ build ⇄ review ──▶ publish ──▶ done
          ⇅
       quality   (with --quality-gate)
```

That is the default workflow's; in general a run's `phase` is the id of its current step, or `quality`,
`publish` or `done`. `Workflow.run` starts from the saved `phase` and `round`; a new run is a resume from the
first step. Before anything else, `_check_start` checks that the run can finish: `_check_alone` refuses a run
in the checkout while another one there is live (a worktree run neither refuses nor counts); in round 0 (before
its first editing step), a worktree run's `_open_worktree` checks for a branch and an `origin`, fetches the base
branch and adds the worktree at `origin`'s (or finds it from before), `_check_gate` runs `CI.preflight` and
checks the working tree, and for an in-place run `_check_repo` fast-forwards the base branch; in a later round,
a worktree run's `_check_worktree` refuses a run whose worktree is gone unless `pr_url` is set, and a resume
that starts in the gated step or in `quality` checks the CI credentials. Then `_walk` takes the steps in
order, in `work_dir`:

- Each step's turn is `_take_turn` and `_step_turn`, with the prompts `_prompts` fills in through
  `_placeholder`. A human-paced step, the default's `spec`, has no timeout or stall notice and focuses its pane.
- `_goto` makes the next step current and saves it. Before the first editing step, the default's `build`,
  `_switch_to_branch` fast-forwards the base branch again and creates the branch (in a worktree run, from
  `origin`'s base branch fetched again, without touching the checkout), `base` is taken from HEAD and round 1
  starts.
- `quality`, after each turn of the quality-gated step when the gate is on: `_quality` writes `quality-N-Q.md`.
  `_analyse` snapshots, pushes and has Jenkins judge the change, saving each link of the chain in `ci` as it
  learns it, so a resume continues from the furthest one, and the round's quality file is the `quality.md` the
  build archived. `_poll` retries a failed request until `QUALITY_TIMEOUT` and keeps the heartbeat going.
  A gate that does not pass goes back to the gated step, whose answer is `build-N-qQ.md` in the default.
- The verdict step, the default's `review`, records `verdict`; `CHANGES_REQUESTED` goes back to its loop target
  in the next round until `max_rounds`, resetting `quality_round`. A workflow without a verdict step records
  `FINISHED` once its last step is done, which counts as `APPROVE` (`SUCCEEDED`) for the exit status and the
  draft, while `verdict_line` and `commit_note` say that no agent reviewed the change. With the gate, a last
  quality round that did not pass (`_gate_passed`) records `QUALITY_GATE_FAILED` instead, which does not count.
- `publish`, only with a pull request: `_publish` commits, merges `origin`'s base branch in (recording
  `conflicts` and aborting when it conflicts), pushes and records `pr_url`, skipping any step already done.
  Then `_leave_work_dir` checks the base branch out again, or in a worktree run removes the worktree, only
  logging a failure.
- `done`: `_clean_up_quality` deletes the run's throwaway branches, `owner` is cleared,
  and with a pull request `_close_workspace` closes the workspace.

A failure goes through `_release`, which records `error`. Each role's turn is `_turn`: it takes a handoff file that
already exists, and otherwise gets the agent ready through `_agent` (`NEW`, `ALIVE`, `RESUMED` or `RESTARTED`; an
exited agent is resumed into its saved session only if its record's `kind` is the role's harness now, and otherwise
starts fresh), prompts it with what `_prompt_for` picks, and polls for the file in `_await_handoff`, where
`_tell_human` notifies the human of a blocked or stalled agent once. A verdict step's file without a `VERDICT` first
line goes to `_retry` (in `_verdict_turn`), which moves it aside and, once per file (`retried`), prompts the role
again with `RETRY_VERDICT_PROMPT`; `retrying` names the file while that turn is under way, so a resume sends the
retry prompt, not the step's own. A file of a step that can block (`Step.can_block`: neither human-paced nor the
verdict step) whose first line is `BLOCKED:` (`parse_blocked`) goes to `_block`, which saves `blocked` and then
moves it aside to `.blocked-K`; `_unblocked` then has `_block_answer` notify the human and wait for the file again,
as often as it comes back `BLOCKED:`, with `BLOCKED_PROMPT`, no timeout and no stall notice, and a resume with
`blocked` set finishes an undone move (`_finish_block`) and goes back into that wait. `_prompts` appends
`BLOCKED_NOTE` to every prompt of such a step. `_save` checks the owner before every write and raises `RunTakenOver`
after a takeover; `_heartbeat` keeps `state.json` fresh while a turn runs. `_written` and `_quality` open a turn's
record in `turns` (`_turn_record`, started by `Host.now`) and `_end_turn` closes it at the handoff file's
`Host.mtime`; both read the project host's clock. The end of `run` and `_release` write `summary.md` (`run_summary`,
with `turns_table`), except after a takeover; `pr_body` adds the same table and `show` prints `turn_lines`. A run
that is done first counts each agent turn's tokens (`_count_tokens`: `Host.session_log` reads the session's log
where `SESSION_LOGS` says its harness keeps one, and `turn_tokens` sums the answers within the turn), then writes
`metrics.json` (`run_metrics`), with the quality rounds `_quality` records in `quality_rounds` by `gate_failures`;
the `metrics` command prints every run's (`Host.run_metrics`).

## Workflows

A workflow is data: a `Pipeline` with a name, its roles in pane order (role key to pane label) and a tuple of
`Step`s, and optionally a `proof_command`. A step names its id (also its phase), its role, its handoff file (fixed
like `spec.md`, or one per round like `build-{n}.md`), a first prompt, a later-round prompt (`again`) and a note for
a fresh session in a later round (`fresh_note`), and the flags `human_paced`, `edits`, `loop_to` (which makes it the
verdict step), `quality_gated` and `locks`. Prompts take `{task}`, `{cwd}`, `{n}`, `{change}`, `{path}`,
`{proofs_dir}` and, for every step `X`, `{X_path}`, `{prev_X_path}` and `{earlier_X_paths}`. `Pipeline` validates
itself when constructed and raises `ValueError` naming the problem, such as a duplicate id, a loop target that does
not come earlier, two verdict loops, two gated steps or an unknown placeholder.

The quality-gated step is the one the gate follows: its answer to quality round Q inserts `-qQ` before its file's
extension, other steps' prompts name its last report of the round, and `QUALITY_PASSED_NOTE` or
`QUALITY_UNRESOLVED_NOTE` ends the verdict step's prompt. The contract step, a first step with a fixed file that
does not edit, titles the branch and the pull request; without one the task does. The pull request body holds the
commit's provenance trailers as a table (`provenance`), the contract, the last editing step's last report, the
last quality file and the verdict file.

A locking step (`locks`: the first editing step, which the verdict loop does not run again, so that what it locks is
its own work) locks the change as it stands when each of its turns ends: `Host.lock` snapshots the tree into the
local ref `refs/orchestrator/<key>/locked/<step>`, which nothing pushes and `done` deletes, and `RunState.locked`
saves the paths that differ from the base. After every other editing turn, `Workflow._hold_locks` writes the turn's
changes to those paths to `locked-<turn's file>.diff` in the run directory, saves a record of them in
`RunState.restored`, and then restores the paths from the ref (`Host.restore`, the working tree only, a path the ref
lacks deleted); a resume restores a saved record again. That happens before the quality round, so the gate judges
the locked files, and `LOCKED_NOTE` tells the verdict step which files of its round were restored and where the
diffs are.

A workflow with a `proof_command`, such as `python -m pytest -q {proofs}`, has its verdict step back findings with
tests in `{proofs_dir}`, `RunState.proofs_dir(n)` in the run directory, so they never enter the snapshot or the
commit. After a review that requests changes, `Workflow._judge_proofs` runs each proof file with the command in the
working tree (`_run_proof`: exit status 1 is a failing proof, which demonstrates the finding its name's first number
names; 0 passes and anything else errors, neither demonstrating), saves the outcomes in `RunState.proofs`, and
appends `proofs_section` under `PROOFS_HEADING` to the review; a resume runs no proof of a round that has records,
and appends the section only where the review lacks it. A proof runs from a copy of its directory inside the tree
it judges, `<work_dir>/.orchestrator/proofs/<round that wrote it>/`, so that pytest, looking upwards from it for its
configuration, meets the working tree's first (`_run_proof` has the details); the review and the notes name the proof
where the verdict step wrote it, and say where it ran. A proof runs under GNU `timeout` where there is one, which
stops its whole process group. A demonstrated proof is run again in every later round until it passes:
`Workflow._rerun_proofs`, right before the verdict step's turn and so after the editing steps' turns and the quality
loop, runs each carried proof (`_carried_proofs`: its last run failed, or a rerun found it erroring) from the proofs
directory of the round that wrote it, and records the outcome in `RunState.proofs` with the round it ran in and, as
`written`, the round that wrote it (`proof_written` reads a record saved before there was one as a first run); a
resume runs no proof again once the round's reruns are recorded. `CARRIED_PROOFS_NOTE`, after the quality and locks
notes, tells the verdict step which proofs still fail or now error, with their last lines, and which now pass, and
asks it to weigh one that does not pass as a finding that stands unless a report shows the proof itself is wrong;
the reruns leave the reviews themselves alone. `PROOFS_NOTE` ends the next round's prompt of the step the review
loops back to, naming every carried proof, from whichever round wrote it.

`WORKFLOWS` holds only `DEFAULT_WORKFLOW`, worded by the built-in prompts; a run with prompt files gets it from
`find_workflow`, worded by its own. Other workflows are TOML files: `run --workflow NAME` finds
`NAME.toml` in `workflows_dir` (`~/.config/ai-agents-orchestrator/workflows/`), and `run --workflow-file` takes
one by path; the README's [Your own workflows](../README.md#your-own-workflows) gives the format. A file's
step can `use` a default step and override some of its fields; a role without a label keeps the default
workflow's label for its key, or else is named after the key; a role's `model` sits under every model flag,
and its `agent` (`Pipeline.agents`, one of `AGENT_KINDS`) under `--agent` and `--role-agent`. A
file cannot take a built-in workflow's name. `Pipeline` also rejects a prompt or file that could not be filled
in at run time (`_fill_problem`), and a handoff file outside the run directory or named like the run's own
`state.json`, `summary.md`, `metrics.json`, `quality-*` and `locked-*` files, and two steps whose files can share a name in some round (`Step.name_forms`,
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

## serve

`serve_command` starts a `RunServer`, a stdlib `ThreadingHTTPServer` on a free port of `127.0.0.1`, prints
`{"url", "token"}` as one line, and serves from a thread until SIGINT or SIGTERM sets its stop event; it then
sets `RunServer.stopping`, which ends every event stream, shuts the server down and returns 0. The thread starts
before the line is printed, because `shutdown` waits for `serve_forever` to stop, so it must have started. A line
that cannot be printed (the reader of stdout is gone) returns `EXIT_ERROR`, with stdout pointed at `/dev/null` so
the flush at exit cannot fail again. Like `gui` and
`doctor`, `main` runs it through `herdrless_command`, before `connect`, so it needs no herdr pane. Everything it
serves is read-only.

- **Projects.** Each request names its project by `cwd` and an optional `machine`, as the window's fields do.
  `RunServer.project` connects each pair once through `project_host`, which `project_runs` also uses: the
  `Host` that reaches the machine, and the directory resolved on it. It keeps them as a `ProjectRuns`, with
  `target_args` of the pair for the records' resume commands.
- **Endpoints.** Every request names the project by `?cwd=` and, optionally, `?machine=`, and needs the token:

  | Endpoint | Gives |
  |---|---|
  | `/runs` | `{"version": 1, "runs": [...]}`, `list --json`'s document (`run_record` of each of `Host.run_states`) |
  | `/files?run=ID` | the files in the run's directory (`Host.run_files`), oldest first, each `{"name", "mtime"}` |
  | `/file?run=ID&name=NAME` | `{"run", "name", "text"}` for one of them, read through `Host.read` |
  | `/events` | a `text/event-stream` of `record`, `files` and `problem` events; see Events below |

  `RunRequestHandler`'s docstring is the authority on them.
- **Events.** Each `/events` stream has its own `RunWatch`. It polls `run_states` every `SERVE_POLL_SECONDS`,
  and for a `--machine` project every `SERVE_REMOTE_POLL_SECONDS`, and compares what it reads with what the stream
  last sent. A run whose record changed, other than in `AGING_RECORD_KEYS`, gets a `record` event, and one whose
  files changed a `files` event; a run whose `state.json` is gone gets a `record` event with `null`. The first
  poll sends every run. An unfinished run's files are read on every poll, a finished or failed run's only when
  its record changes. A project that cannot be read gives one `problem` event, and another with `null` once it
  can. A stream with nothing to send writes an SSE comment instead, so a client that has gone is noticed at the
  next poll.
- **Security.** Any local user can connect to a port on `127.0.0.1`, and any web page can ask the browser to.
  So `RunRequestHandler` checks every request, in this order. `_refused` answers 403 to a `Host` header that is
  not `127.0.0.1:<port>` or `localhost:<port>`, which is how a DNS-rebinding page's requests arrive. It also
  answers 403 to an `Origin` that is not the server's own or one given with `--origin`. The Tauri shell passes
  its webview's origin: `tauri://localhost`, or Vite's dev server under `tauri dev`. `_has_token` then wants
  the token from `secrets.token_urlsafe`, compared in constant time as UTF-8 bytes, since `compare_digest`
  refuses a non-ASCII `str`. It may come as `Authorization: Bearer` or,
  for an `EventSource`, which cannot set headers, as `?token=`; without it the answer is 401. A CORS preflight
  (`OPTIONS`) carries no token, so it gets only the `Host` and `Origin` checks, and a 204 that allows `GET`
  with `Authorization`. `Access-Control-Allow-Origin` echoes only an origin that passed. `ProjectRuns` lets a
  run id or file name be only a plain name (`plain_name`: no `/`, not `.` or `..`) and refuses `state.json`.
  It raises `RunPathRefused` before anything is read, which the handler answers with 403.

## The desktop app: `ui/`

A Vite + React + TypeScript frontend in `ui/src/`, in a Tauri 2 shell in `ui/src-tauri/` (`create-tauri-app`'s
layout), with no Python of its own.

- **Shell.** `src-tauri/src/lib.rs` starts the sidecar in `setup`: `serve --origin <the webview's origin>` of
  the orchestrator `Orchestrator::locate` picks, `python3 $ORCHESTRATOR_PY`, else the binary beside the app's
  executable that a release bundles (`BUNDLED_NAME`), else `python3` with the orchestrator.py of the checkout the
  app was built in. It reads the first line and keeps the URL, the token and the checkout's directory, null for
  the bundled binary, or why it failed, for the `server` command. With no checkout and no project opened before,
  the frontend asks for a project folder. It kills the sidecar on `RunEvent::Exit`. The webview's CSP allows
  connections only to itself, Tauri's IPC and `http://127.0.0.1`.
- **CLI.** Started with arguments, `main.rs` runs none of that: before GTK or WebKit start, `lib.rs`'s `cli` builds
  the same orchestrator's command with the arguments appended, and `Cli::exec` replaces the app's process with it,
  so the terminal, the exit status and the signals sent to the app are the CLI's; a user's directory that is gone,
  or an exec that fails, prints why and exits 1. So the AppImage is the CLI too, but for signals when it runs
  without FUSE: then the process its caller started is the AppImage's runtime, which passes on none (TODO.md). On
  macOS, a lone `-psn_` argument, which a Finder launch may pass, opens the window. Windows, which builds no app and
  no console, has no CLI.
- **The user's shell.** `Orchestrator::command`, which the sidecar and the CLI are both built from, starts the
  orchestrator in `UserShell`'s environment and directory, not the app's. Only an AppImage makes them differ: its
  `AppRun` points path lists, `PYTHONHOME` and GTK's variables into the bundle and starts the app in
  `$APPDIR/usr`, and so does an extracted one's `squashfs-root/AppRun`. `UserShell` drops the bundle's entries and
  variables (its constants list them), naming the bundle by `$APPDIR` and by what that resolves to, and changes to
  the directory the AppImage was started from, `OWD`, or `PWD` where an extracted AppImage sets no `OWD`, so the
  orchestrator, and every process it starts through `child_env`, sees the user's. The app's own process keeps the
  bundle's, which its GTK needs.
- **Frontend.** `api.ts` types the protocol: `RunRecord` mirrors `run_record`, and `Api` builds the URLs and
  reads `/file`. `hooks.ts`'s `useRunStream` opens the `EventSource` and folds its events into a `RunsState`
  with `runs.ts`'s `applyEvent`. Every opening starts from no runs, since the server sends every run again.
  `runs.ts` also holds `progress` (the stages of a run's progress bar, with `quality` after the step it judges)
  and the wording helpers, which follow `format_age` and `run_round`. A `RunEntry` keeps when its record arrived,
  so `heartbeatAge` ticks with `useNow` between events. The components are `ProjectBar`, `RunsOverview`,
  `RunDetail`, `StepProgress` and `HandoffFile`, which renders a file with `react-markdown` and `remark-gfm`,
  raw HTML skipped, and fetches it again whenever its mtime changes.
- **Tests.** Vitest with React Testing Library in jsdom: `App.test.tsx` replays `test/recorded-stream.json`
  through `test/fakes.ts`'s `FakeEventSource`, with `fetch` stubbed. The recording is the events a real
  `serve` sent over a scripted run. `runs.test.ts` covers the pure helpers. `cargo test` in `src-tauri/` starts
  and stops a real sidecar.

## Tests

`tests/test_gate.py` covers `gate.py`: each check on a real git repository in a temporary directory, the quality
file `quality_report` writes, and `TestGate`, the program itself, with stub linters on `PATH`. Tests that read
what the gate writes with the orchestrator's reader, such as `TestQualityFileContract`, import both modules and stay
in `tests/test_orchestrator.py`, as do `tests/test_properties.py`'s, which hold both modules' copies to one oracle.

`tests/test_orchestrator.py` runs `Workflow` on in-memory fakes: `FakeHost` (filesystem and git, played one git
command at a time so `Host`'s own git steps run on it, with origin scripted by its attributes), `FakeHerdr`
(scripted roles, one callable per turn), `FakeCI` (Jenkins behind `urlopen`, each triggered build
playing a scripted `outcome`) and `FakeClock` (time that passes only when the workflow sleeps).
`make_workflow` builds a new run on them; `saved_run` and `resume` build one from a saved state. Test classes
are named after what they cover, e.g. `TestWorkflow`, `TestPullRequest`, `TestResume`, `TestHeartbeat`,
`TestRunHealth`, `TestQualityGate`, `TestQualityResume` and `TestOtherWorkflows`; `TestDefaultWorkflowPrompts`
compares the default's prompts with those recorded from commit `d6bb100`, except for `SPEC_PROMPT`'s
"Separate agent sessions"; `TestHarnesses`, `TestHarnessFlags` and `TestWorkflowFileAgents` cover the harnesses,
on `FakeHerdr`, which records each agent's kind in `kinds` and finds a resumed session by its harness's syntax; `gated` and `resume_gated` build a run with the gate
on a `FakeCI`. `TestHostGit` and `TestHostSnapshot` run on real git in a temporary directory. `TestRunWindow`
and `TestGuiCommand` drive the GUI on fakes of the tkinter modules (`fake_ui`, `fake_tkinter`, with `FakeRoot`
running `after` callbacks on `tick`), and `TestRunProcess` runs real child processes. `TestServeEndpoints`,
`TestServeSecurity` and `TestServeEventsOnAMachine` run a real `RunServer` on a free port over `ServeHost`, a
`FakeHost` with run files. `TestServeEvents` runs one over real temporary directories, read with `urllib`.
`TestRunWatch` covers the polling on its own, and `TestServeCommand` runs `serve` as a process of its own.

`tests/parity/` is the parity harness, the oracle the Rust engine is judged by: it runs the engine as a program, with
nothing of it imported. Each scenario in `scenarios.py` scripts a `World` (`world.py`): a git project on `main` with
a bare `origin` beside it, the fake herdr, gh and ssh of `fakes.py` first on `PATH` and the real gitleaks after them
(every scenario is skipped where there is none), and Jenkins, and PyPI behind an HTTPS proxy, served from the test
process. It runs
`orchestrator.py` there, or the program `PARITY_ENGINE` names, killing and resuming it where the scenario says,
and the world records, normalized by `normalize.py` (times, run ids, pids, hashes, its own paths and ports): each
invocation's exit status, the output programs read whole and other output only as its facts; the commands herdr
and gh got and the requests Jenkins and PyPI got, in order, with `state.json` wherever it changed between them; the
run directory's files; and both repositories' refs, commits and working trees. `test_parity.py` fails a scenario
whose record differs from `recorded/<scenario>.json`, naming where; `PARITY_RECORD=1` writes the recordings
instead, from the Python engine only.

## Tooling

- `pyproject.toml`: no runtime dependencies, Python 3.13+; pytest, pytest-cov and Hypothesis in the `dev` group, PyInstaller
  in the `build` group.
- `ui/package.json`: the desktop app's npm scripts, `dev`, `build`, `test`, `typecheck`, `lint` and `tauri`. Its
  `.npmrc` sets `legacy-peer-deps`, because npm 9.2 as Debian ships it cannot resolve Vitest's optional peers.
  Jenkins runs the frontend's `typecheck`, `lint` and `test`, and the shell's `cargo test`.
- `packaging/build-desktop.sh`: builds the desktop app with `tauri build`, the binary `build-binary.sh` built
  copied in as its `externalBin` sidecar and `pyproject.toml`'s version, through a release config the script
  writes and merges over `tauri.conf.json`, which bundles nothing. It writes
  `dist/octoflow-desktop-<os>-<arch>` `.deb` and `.AppImage` on Linux, `.dmg` on macOS (signed ad hoc),
  each with its `.sha256`, and refuses a bundle without the sidecar. On Linux it then runs the AppImage as the
  CLI from a temporary directory: `--help` and `workflows` exit 0, and the orchestrator `serve` execs starts in
  that directory with no `$APPDIR` entry in `LD_LIBRARY_PATH` and no `PYTHONHOME`, read from its `/proc` entry.
- `packaging/build-binary.sh`: builds `dist/octoflow-<os>-<arch>` (`linux-x86_64`, `macos-arm64`), a
  PyInstaller one-file binary with Python and tkinter inside, and its `.sha256`, then smoke-tests it: `--help`,
  `workflows` lists `default`, and `gui --smoke-test` where there is a display. On Linux it builds with the
  system `python3` (`python3-tk` installed) in `build/binary-venv`, not uv's Python, whose Tk lacks Xft and so
  draws every font in one fixed bitmap size; it refuses a Tk without libXft.
- `Jenkinsfile`: `uv sync --frozen`, `uv run pytest` with branch coverage of `orchestrator` and `gate`, a UI stage
  that runs `ui/`'s `npm ci`, `typecheck`, `lint` and `test` and `ui/src-tauri/`'s `cargo test --locked`, failing
  the build before the gate, a Gate stage that runs `gate.py` and archives `quality.md` (see
  [quality gate](quality-gate.md)), and a Package stage that runs `packaging/build-binary.sh` and
  `packaging/build-desktop.sh` on every build but a quality build (`GIT_REF` set).
  On `main` a Release stage publishes a new `pyproject.toml` version as a GitHub release, with
  `orchestrator.py` attached as the zipapp
  `octoflow.pyz`, the Linux binary and the Linux desktop app, each with its `.sha256`, in one
  `gh release create`. Its parameter `GIT_REF` builds
  another branch; such a build never releases.
- `.github/workflows/automerge.yml`: keeps auto-merge on for a pull request into `main` from a branch of this
  repository, so it merges with a merge commit once Jenkins' required check passes; its header lists the pull
  requests it leaves to a human.
- `.github/workflows/macos-binary.yml`: Jenkins has no Mac, so it runs `packaging/build-binary.sh` and
  `packaging/build-desktop.sh` on `macos-latest`. When a release is published, or by hand for an existing tag, a
  second job uploads `octoflow-macos-arm64`, `octoflow-desktop-macos-arm64.dmg` and their `.sha256` to that release
  with `gh release upload --clobber`; with only `contents: write`, it never creates a release or a tag. For a pull
  request that touches the packaging, the UI, the code or the Python they build with, it only builds, with
  `contents: read`, as an advisory check. Everything else stays in Jenkins.

How to set Jenkins up for it is in [quality-gate.md](quality-gate.md).
