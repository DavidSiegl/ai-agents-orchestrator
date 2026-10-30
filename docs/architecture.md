# Architecture

A map of the code for someone about to change it. Usage is in the [README](../README.md), and the reasons behind
resume and stale-run detection are in [design.md](design.md). Code is named by symbol, not line, so this stays
true as lines move.

## `gate.py`

The quality gate, a standard-library program of its own: `python gate.py --base REV` judges the change in the
working tree it runs in and writes the quality file ([quality gate](quality-gate.md)). It is the one Python left:
the engine is Rust, and never runs it; Jenkins runs it, and the engine reads the file it writes, with its own copy of
the parts of the format it needs ([the quality file as a contract](quality-gate.md#the-quality-file-as-a-contract)). In order:
the tunables (`QUALITY_ISSUE_LIMIT`, `GATE_MIN_COVERAGE`, `MUTATION_BUDGET`, ...); its own git helpers (`_git`,
`_git_run`) and `GateError`; `quality_report`, which writes the file; the linters (`GATE_TOOLS`), `GateChange`,
`GateCheck` and `judge_change`, which runs them; the checks `gate_checks` lists, from `weakened_tests` to
`vague_names`; and `gate_command`, `parse_args` and `main`.

## `engine/`: the engine

The orchestrator, `octoflow-engine` in the top-level Cargo workspace: a library crate and the `orchestrator` binary
(`engine/src/main.rs`). It is the port of the Python engine, `orchestrator.py`, which it replaced in 0.49.0
(rust-engine ticket 11), and it reads and writes every file, prompt and run state as that engine did, so a run either
started resumes in the other. Where a doc comment names a Python function or class, it is the one the Rust code was
ported from. By module:

| Module | What it holds |
|---|---|
| `py` | What the port needs of Python itself: `text` (str's `splitlines`, `strip`, `os.path.splitext`, `repr`, `difflib.get_close_matches`, text mode's universal newlines, with Python's character classes in `unicode_tables`, which `engine/tools/unicode_tables.py` writes), `format` (`str.format`'s language, filling and failing as Python does), `json` (`Json`, a value whose objects keep their order, and `Map`, written as `json.dumps` writes it; `parse_lossy` reads a lone surrogate as U+FFFD), `toml` (TOML as `tomllib` reads it, and what `str`, `repr` and `json.dumps` write of its values), `url` (`urllib.parse`'s `quote`, `urlencode`, `urlsplit`, `parse_qsl` and `unquote_plus`) and `datetime` (`IsoTime`, a time as CPython's C `fromisoformat` reads it, quirks included, with `IsoTime::timestamp`, which takes one without an offset in the machine's zone, and its date; `fromisoformat_timestamp`, the two in one call; and `strftime_local`, `time.strftime` there; the zone is jiff's, read as the C library reads it). |
| `constants`, `error`, `env` | The constants, `OrchestratorError`, `child_env`, `ci_credentials` and `parse_env_file`; `ConfigDir` is `config_dir` and where its files are. |
| `prompts` | The built-in prompts, `Prompts` (by `Prompt`), `load_prompts`, `prompt_problem` and `saved_prompts`. |
| `pipeline`, `workflow_file` | `Step`, `Pipeline` with all its checks, `default_workflow`, `find_workflow`, `is_built_in`; `parse_workflow`, `load_workflow_file`, `workflow_files`, `named_workflow`, `workflow_definition`, `workflow_toml`, `run_pipeline` and `saved_pipeline`. |
| `state` | `RunState`, with the fields of a `state.json` it does not know kept, after its own. |
| `handoff`, `quality` | `parse_verdict` and `parse_blocked`; the quality-file reader, `parse_gate`, `gate_failures`, `finding_fingerprint`, and the engine's own quality files, `leak_report` and `build_failed_report`. |
| `host`, `patch` | `Host`, a trait whose every operation is a provided method built on running one command (`exec`), so a fake overrides only what it plays: `ProcessHost`, the machine reached by real processes, here or over `ssh -o BatchMode=yes` with `ssh_sharing_options`' shared connection, each command in a process group of its own, its output read to the end on threads of their own, and the whole group killed with SIGKILL when it runs out of time; the secret scan (`scan_unpublished`, finding `quality`'s `Leak`s, `LeaksFound`); `ChangedFile`, `RunFile`, the pull request views with `GhError::Unusable` for `GhUnusable`, and `PushError::Leaks` for `LeaksFound`. Output kept verbatim is bytes, and a path git prints is refused when it is not UTF-8. `FakeHost` (`host/fake.rs`, tests only) is the Python tests' `FakeHost`, git played one command at a time through `git_run`, its other commands real processes when a test sets `processes`, as the Python tests' `host._run = subprocess.run` makes them, its time read from a `FakeClock` there, the Python tests' `FakeClock`. `added_lines` reads `change_diff`'s diffs. |
| `harness`, `herdr` | The agent harnesses: `HARNESSES`, one row per harness of what the Python engine kept in four tables, its permission, resume and directory arguments and its session logs, found by `harness(kind)`, each `Harness` giving its `permission_args`, `launch_args` and, from its `SessionLog`, `session_usage`; `AgentSettings`. `Herdr`, a trait of the commands a run gives herdr (the CLI's argv, JSON and error codes are `ProcessHerdr`'s alone, so a fake plays agents, not the protocol), `HerdrError` with herdr's code, `Agent` (herdr's record, its session included), `Direction`; `ProcessHerdr` runs the CLI here as `ProcessHost` runs a command, forwarding `--machine`, and has the commands no `Workflow` gives: `ssh_target`, `notify` and `machine`, and the `AgentConsole` serve reads and types into a run's agents through (`read`, `submit`, `send_keys`), each named by an `AgentAddress`, its herdr name and its run's server. `HerdrServer` is which of this machine's herdr servers the CLI reaches, by `HERDR_SOCKET_PATH` and `HERDR_SESSION`: `Herdr::server` gives the one a run's agents are in, which the run saves as `RunState.herdr_server`. `FakeHerdr` (`herdr/fake.rs`, tests only) is the Python tests' `FakeHerdr`, a role's turns scripted as closures; a turn that raises the run's `Interrupt` fails its prompt, as Python's turn that raises KeyboardInterrupt. |
| `http`, `ci`, `pypi`, `crates_io`, `package_index` | `Http`, the one seam to Jenkins, PyPI and crates.io, as `urlopen` is the Python engine's: one request, its whole 2xx answer, or `HttpError` with the status or, for no answer, URLError's words; `HttpClient` sends it with ureq, through the proxy `http_proxy` or `https_proxy` names unless `no_proxy` names the host, trusting the certificates of the places `SSL_CERT_FILE` and `SSL_CERT_DIR` name, else of the system's as openssl-probe finds them, each read by rustls-native-certs (`root_certs`), and leaves an `Authorization` header behind on a redirect. `Jenkins` is the Python engine's `CI` class: each call of the quality job, its errors a `CIError`, `Request` (Python's `CIError`, with `status` and `transient`) or `Refused` (its `OrchestratorError`), with the credentials only in the Authorization header and Jenkins's text masked (`mask`); `PyPI` is Python's, and `CratesIo` asks crates.io as it asks PyPI, with a User-Agent naming the project; both are a `PackageIndex`, which answers what it knows of a package as an `IndexedPackage`, and `PackageIndexes` holds one of each for the new-dependencies check. The tests' `FakeJenkins` (`ci/fake.rs`), `FakePyPI` (`pypi/fake.rs`) and `FakeCratesIo` (`crates_io/fake.rs`) play the servers behind `Http` as `FakeCI` and `FakePyPI` play them behind `urlopen`, so every URL, answer and error of `Jenkins`, `PyPI` and `CratesIo` runs in them; `http/tests.rs` runs `HttpClient` against a local server. |
| `checks` | The orchestrator's checks: `RunChange`, `Finding`, `CheckFindings`, `OrchestratorCheck`, `orchestrator_checks`, `judge_run`, `diff_budget`, `unexpected_paths`, and in `checks::dependencies` `new_dependencies` with `DEPENDENCY_ECOSYSTEMS`, `python_dependencies` (pyproject.toml's lists, uv's indexes and sources, `uv.lock`), `cargo_dependencies` (a Cargo workspace's manifests and `Cargo.lock`), `PackageSource`, `package_key` and `spec_names`. |
| `workflow`, `runs`, `interrupt`, `spec_file` | `Workflow`, the phase machine (below): a run in the project's checkout or a worktree, blocked reports, a seeded spec, locked files (`workflow/locks.rs`), proofs (`workflow/proof_runs.rs`) and the quality gate's rounds (`workflow/quality_rounds.rs`: the snapshot pushed, Jenkins's build polled to its quality file, the orchestrator's checks added, the loop back to the gated step and the refs deleted): given its `Collaborators` (`Host`, `Herdr`, `Clocks`, `Human`, which is Python's `notify` and `log`, the `Interrupt`, `pid_alive`, and the quality job, `Option<&Jenkins>`, with the `PyPI` its checks ask) and `RunSettings`, `run` ends in a verdict or `Stopped` (`Failed`, `TakenOver` for `RunTakenOver`, `Interrupted` for a Ctrl-C). `run_health`, `pid_alive` and `local_host`; `Interrupt`, the Ctrl-C, which the Workflow checks at each pause and which stops the command a `ProcessHost` or `ProcessHerdr` waits on (its group gets SIGINT, then SIGKILL, in `run_process`), raised by the thread `by_signals` starts for SIGINT, SIGTERM and SIGHUP, each unless it was ignored at start, after which `interrupt::exit` ends the program by the first SIGTERM or SIGHUP, as the Python engine dies by it; `SystemClocks` and `LocalHuman` (`workflow/system.rs`), the real run's clocks, whose sleep a Ctrl-C ends, and human, Python's `notify_locally` and `log`; `SpecFile` and `read_spec_file`, run --spec's spec, which `RunSettings::spec` gives a run that starts at `Pipeline::seeded_start`. `workflow/fixture.rs` (tests only) is the Python tests' `make_workflow`, `saved_run` and `resume`, with `Told`, the human as the tests read it, and `host/fake.rs`'s `FakeClock` as the Workflow's `Clocks`. |
| `cli` | The `orchestrator` program (`engine/src/main.rs`, which ends by `interrupt::exit`): `main`, which calls `by_signals` once it has read the arguments, for every command but `serve`, and its commands (the Python engine's `gui`, its tk window, was not ported); `run_command` dispatches them, and `herdrless_command` those that need no herdr pane, `workflows`, `judged-in-quality` and `doctor`, given a `Local` (this machine's `Host`, a herdr maker, Jenkins and the current directory). `cli::args` is `parse_args` on clap (`Command`, with `Target`, `Settings` and `Starts`, the flag groups argparse's parent parsers share), `check_flag_values` and `check_spec_args`; `cli::start` is `new_state`, `resumable_state` (with `find_run` and `resume_quality_limit`) and `resumed_state`, which turns the gate on or off by `turn_quality_gate_on` and `_off` (`workflow/gate_switch.rs`); `Connected` is what a command is given once `connect` has reached herdr and the host, the Workflow's `Collaborators`, how to make a run's `Jenkins`, standard output and error, and the `RunWorkflow` that drives a run (`run_workflow`), so the tests run commands on the fixture's fakes and script runs as Python patches `Workflow.run`; `finish`, `resume_command` and the interrupted note end it. `cli::listing` is `list`'s and `show`'s (`run_record`, `run_rows`, `run_outcome`, `run_round`, `print_runs`, `fill_pr_states`, `show_run`); `cli::prune` is `prune_branches`, `pull_request_pending`, `sweep_summary` and `sweep_branches`, which a `run` that opens a pull request calls first; `cli::close` is `close_run`; `cli::workflows` is `workflows_command`; `cli::refresh` is `ConflictCheck`, `has_conflict`, `refresh_conflict` and `refresh_command`; `cli::drain` is `drain_command` and `refreshed_headers`, and `cli::drain::queue` the spec queue (`QueuedSpec`, `read_queue`, `blocker_cycle`, `header_field`, `with_header_field`, `replace_local_file`); `cli::judged` is `judged_in_quality`; `cli::doctor` is `check_setup` and `report_checks`, without Python's tkinter check. A Ctrl-C outside a Workflow fails the command it stops, which `main` takes for the interrupt (exit status 130). |
| `serve` | `serve`, on tiny_http (below): `serve_command`, `RunServer` (Python's `RunServer` and `RunRequestHandler`, a thread per request), `project_host` and the `Reached` project it gives; in `serve::runs` `ProjectRuns`, `AgentInput`, `plain_name`, `RunReadError` (`RunPathRefused`), `RunWatch` and its `Event`s (`sse_event`). |
| `pull_request`, `summary`, `proofs` | `spec_title`, `branch_name`, `verdict_line`, `commit_note`, `provenance`, `pr_body` and `without_conflict_warning`; `run_summary`, `run_metrics`, `turns_table`, `turn_lines`, `turn_tokens`, `format_duration`, `format_age`, `iso_utc` and `epoch_seconds`; `ProofRun`, a `RunState.proofs` record as the Workflow reads and writes it, and `proofs_section`. |

Its tests are each module's `<module>/tests.rs`, the Python tests of those parts ported case for case, and checks
against the Python engine as the oracle: `engine/tests/oracle/` holds what the Python engine gave for the engine's
inputs, each `.json` written by a generator that ran it, removed with it, and the parity recordings (`tests/parity/recorded/`) are read as they are: every `state.json` they hold
reads into `RunState` and writes back byte for byte, every workflow the repository defines parses to the Python
engine's definition, every recorded run's commit note, trailers, pull request body, `summary.md` and
`metrics.json` come out as the Python engine wrote them, and every prompt the scenarios recorded is made of the
engine's templates (`prompt_parity.rs`). `host/tests.rs` and `host/scan/tests.rs` run `Host` on real git in
temporary repositories, with gh, gitleaks and ssh played by scripts on the `Host`'s `PATH`, which `ProcessHost`
takes from the environment it is given rather than the process's. `harness/tests.rs` replays the Python engine's
launch arguments for every harness, permission mode, model, session and directory, and its token counts of sample
session logs; `herdr/tests.rs` replays every herdr command the parity scenarios recorded through `ProcessHerdr`, and
checks each recorded agent start against the launch arguments of the run's state as last saved before it.
`workflow/tests.rs` and its submodules, each holding one or more Python classes, run `Workflow` on `FakeHost`,
`FakeHerdr`, `FakeClock`, `FakeJenkins` and `FakePyPI`, the Python tests of it ported case for case.
`checks/tests.rs` runs `judge_run` on every quality file a parity recording holds, from the build's quality.md its
scenario's Jenkins served, and gives the recorded file.

## The phase machine

```
spec ──▶ build ⇄ review ──▶ publish ──▶ done
          ⇅
       quality   (with --quality-gate)
```

That is the default workflow's; in general a run's `phase` is the id of its current step, or `quality`,
`publish` or `done`. `Workflow::run` starts from the saved `phase` and `round`; a new run is a resume from the
first step. Before anything else, `check_start` checks that the run can finish: `check_alone` refuses a run
in the checkout while another one there is live (a worktree run neither refuses nor counts); in round 0 (before
its first editing step), a worktree run's `open_worktree` checks for a branch and an `origin`, fetches the base
branch and adds the worktree at `origin`'s (or finds it from before), `check_gate` runs `CI::preflight` and
checks the working tree, and for an in-place run `check_repo` fast-forwards the base branch; in a later round,
a worktree run's `check_worktree` refuses a run whose worktree is gone unless `pr_url` is set, and a resume
that starts in the gated step or in `quality` checks the CI credentials. Then `walk` takes the steps in
order, in `work_dir`:

- Each step's turn is `take_turn` and `step_turn`, with the prompts `prompts` fills in through
  `placeholder`. A human-paced step, the default's `spec`, has no timeout or stall notice and focuses its pane.
- `goto` makes the next step current and saves it. Before the first editing step, the default's `build`,
  `switch_to_branch` fast-forwards the base branch again and creates the branch (in a worktree run, from
  `origin`'s base branch fetched again, without touching the checkout), `base` is taken from HEAD and round 1
  starts.
- `quality`, after each turn of the quality-gated step when the gate is on: `quality` writes `quality-N-Q.md`.
  `analyse` snapshots, pushes and has Jenkins judge the change, saving each link of the chain in `ci` as it
  learns it, so a resume continues from the furthest one, and the round's quality file is the `quality.md` the
  build archived. `poll` retries a failed request until `QUALITY_TIMEOUT` and keeps the heartbeat going.
  A gate that does not pass goes back to the gated step, whose answer is `build-N-qQ.md` in the default.
- The verdict step, the default's `review`, records `verdict`; `CHANGES_REQUESTED` goes back to its loop target
  in the next round until `max_rounds`, resetting `quality_round`. A workflow without a verdict step records
  `FINISHED` once its last step is done, which counts as `APPROVE` (`SUCCEEDED`) for the exit status and the
  draft, while `verdict_line` and `commit_note` say that no agent reviewed the change. With the gate, a last
  quality round that did not pass (`gate_passed`) records `QUALITY_GATE_FAILED` instead, which does not count.
- `publish`, only with a pull request: `publish` commits, merges `origin`'s base branch in (recording
  `conflicts` and aborting when it conflicts), pushes and records `pr_url`, skipping any step already done.
  Then `leave_work_dir` checks the base branch out again, or in a worktree run removes the worktree, only
  logging a failure.
- `done`: `clean_up_quality` deletes the run's throwaway branches, `owner` is cleared,
  and with a pull request `close_workspace` closes the workspace.

A failure goes through `release`, which records `error`. Each role's turn is `turn`: it takes a handoff file that
already exists, and otherwise gets the agent ready through `agent` (`How`: `New`, `Alive`, `Resumed` or `Restarted`; an
exited agent is resumed into its saved session only if its record's `kind` is the role's harness now, and otherwise
starts fresh), prompts it with what `prompt_for` picks, and polls for the file in `await_handoff`, where
`tell_human` notifies the human of a blocked or stalled agent once. A verdict step's file without a `VERDICT` first
line goes to `retry` (in `verdict_turn`), which moves it aside and, once per file (`retried`), prompts the role
again with `RETRY_VERDICT_PROMPT`; `retrying` names the file while that turn is under way, so a resume sends the
retry prompt, not the step's own. A file of a step that can block (`Step::can_block`: neither human-paced nor the
verdict step) whose first line is `BLOCKED:` (`parse_blocked`) goes to `block`, which saves `blocked` and then
moves it aside to `.blocked-K`; `unblocked` then has `block_answer` notify the human and wait for the file again,
as often as it comes back `BLOCKED:`, with `BLOCKED_PROMPT`, no timeout and no stall notice, and a resume with
`blocked` set finishes an undone move (`finish_block`) and goes back into that wait. `prompts` appends
`BLOCKED_NOTE` to every prompt of such a step. `save` checks the owner before every write and stops the run as `Stopped::TakenOver`
after a takeover; `heartbeat` keeps `state.json` fresh while a turn runs. `written` and `quality` open a turn's
record in `turns` (`turn_record`, started by `Host::now`) and `end_turn` closes it at the handoff file's
`Host::mtime`; both read the project host's clock. The end of `run` and `release` write `summary.md` (`run_summary`,
with `turns_table`), except after a takeover; `pr_body` adds the same table and `show` prints `turn_lines`. A run
that is done first counts each agent turn's tokens (`count_tokens`: `Host::session_log` reads the session's log
where its harness's `SessionLog` says its harness keeps one, and `turn_tokens` sums the answers within the turn), then writes
`metrics.json` (`run_metrics`), with the quality rounds `quality` records in `quality_rounds` by `gate_failures`;
the `metrics` command prints every run's (`Host::run_metrics`).

## Workflows

A workflow is data: a `Pipeline` with a name, its roles in pane order (role key to pane label) and a tuple of
`Step`s, and optionally a `proof_command`. A step names its id (also its phase), its role, its handoff file (fixed
like `spec.md`, or one per round like `build-{n}.md`), a first prompt, a later-round prompt (`again`) and a note for
a fresh session in a later round (`fresh_note`), and the flags `human_paced`, `edits`, `loop_to` (which makes it the
verdict step), `quality_gated` and `locks`. Prompts take `{task}`, `{cwd}`, `{n}`, `{change}`, `{path}`,
`{proofs_dir}` and, for every step `X`, `{X_path}`, `{prev_X_path}` and `{earlier_X_paths}`. `Pipeline` validates
itself when constructed and refuses the definition, naming the problem, such as a duplicate id, a loop target that does
not come earlier, two verdict loops, two gated steps or an unknown placeholder.

The quality-gated step is the one the gate follows: its answer to quality round Q inserts `-qQ` before its file's
extension, other steps' prompts name its last report of the round, and `QUALITY_PASSED_NOTE` or
`QUALITY_UNRESOLVED_NOTE` ends the verdict step's prompt. The contract step, a first step with a fixed file that
does not edit, titles the branch and the pull request; without one the task does. The pull request body holds the
commit's provenance trailers as a table (`provenance`), the contract, the last editing step's last report, the
last quality file and the verdict file.

A locking step (`locks`: the first editing step, which the verdict loop does not run again, so that what it locks is
its own work) locks the change as it stands when each of its turns ends: `Host::lock` snapshots the tree into the
local ref `refs/orchestrator/<key>/locked/<step>`, which nothing pushes and `done` deletes, and `RunState.locked`
saves the paths that differ from the base. After every other editing turn, `Workflow::hold_locks` writes the turn's
changes to those paths to `locked-<turn's file>.diff` in the run directory, saves a record of them in
`RunState::restored`, and then restores the paths from the ref (`Host::restore`, the working tree only, a path the ref
lacks deleted); a resume restores a saved record again. That happens before the quality round, so the gate judges
the locked files, and `LOCKED_NOTE` tells the verdict step which files of its round were restored and where the
diffs are.

A workflow with a `proof_command`, such as `python -m pytest -q {proofs}`, has its verdict step back findings with
tests in `{proofs_dir}`, `RunState.proofs_dir(n)` in the run directory, so they never enter the snapshot or the
commit. After a review that requests changes, `Workflow::judge_proofs` runs each proof file with the command in the
working tree (`run_proof`: exit status 1 is a failing proof, which demonstrates the finding its name's first number
names; 0 passes and anything else errors, neither demonstrating), saves the outcomes in `RunState.proofs`, and
appends `proofs_section` under `PROOFS_HEADING` to the review; a resume runs no proof of a round that has records,
and appends the section only where the review lacks it. A proof runs from a copy of its directory inside the tree
it judges, `<work_dir>/.orchestrator/proofs/<round that wrote it>/`, so that pytest, looking upwards from it for its
configuration, meets the working tree's first (`run_proof` has the details); the review and the notes name the proof
where the verdict step wrote it, and say where it ran. A proof runs under GNU `timeout` where there is one, which
stops its whole process group. A demonstrated proof is run again in every later round until it passes:
`Workflow::rerun_proofs`, right before the verdict step's turn and so after the editing steps' turns and the quality
loop, runs each carried proof (`carried_proofs`: its last run failed, or a rerun found it erroring) from the proofs
directory of the round that wrote it, and records the outcome in `RunState.proofs` with the round it ran in and, as
`written`, the round that wrote it (`proof_written` reads a record saved before there was one as a first run); a
resume runs no proof again once the round's reruns are recorded. `CARRIED_PROOFS_NOTE`, after the quality and locks
notes, tells the verdict step which proofs still fail or now error, with their last lines, and which now pass, and
asks it to weigh one that does not pass as a finding that stands unless a report shows the proof itself is wrong;
the reruns leave the reviews themselves alone. `PROOFS_NOTE` ends the next round's prompt of the step the review
loops back to, naming every carried proof, from whichever round wrote it.

`WORKFLOWS` holds only `DEFAULT_WORKFLOW`, worded by the built-in prompts; a run with prompt files gets it from
`find_workflow`, worded by its own. Other workflows are TOML files: `run --workflow NAME` finds
`NAME::toml` in `workflows_dir` (`~/.config/ai-agents-orchestrator/workflows/`), and `run --workflow-file` takes
one by path; the README's [Your own workflows](../README.md#your-own-workflows) gives the format. A file's
step can `use` a default step and override some of its fields; a role without a label keeps the default
workflow's label for its key, or else is named after the key; a role's `model` sits under every model flag,
and its `agent` (`Pipeline::agents`, one of `AGENT_KINDS`) under `--agent` and `--role-agent`. A
file cannot take a built-in workflow's name. `Pipeline` also rejects a prompt or file that could not be filled
in at run time (`fill_problem`), and a handoff file outside the run directory or named like the run's own
`state.json`, `summary.md`, `metrics.json`, `quality-*` and `locked-*` files, and two steps whose files can share a name in some round (`Step::name_forms`,
`shared_name` and `collision_problem`, an exact check). `state.json` saves
the workflow's name and, for one that is not built in, its canonical definition (`workflow_definition`), which
a resume rebuilds the `Pipeline` from, so editing or deleting the file does not change a started run. One saved
before workflows existed loads as `default`. Panes: the first role takes the root pane, the second
splits right of it and each further one splits down from the one before; a workflow file's roles are in the
order its steps first use them. The tests define three more workflows
that run on this engine: Builder ⇄ Reviewer with the task as the contract, Spec Collector → Test Writer →
Builder ⇄ Reviewer, and an Implementer ⇄ Reviewer whose gated step writes `impl-N.md`.

## serve

`serve_command` (`engine/src/serve.rs`) starts a `RunServer` on tiny_http, on a free port of `127.0.0.1`, which hands
each request to a thread of its own; prints `{"url", "token"}` as one line; and serves until SIGINT or SIGTERM, then
returns 0. `crate::cli::main` runs it before it starts the other commands' signal handling, and `serve_command` blocks
SIGINT and SIGTERM and waits for them itself, so that SIGHUP keeps its default. Stopping the `RunServer` (dropping it)
ends every event stream and raises the `Interrupt` its projects' `ProcessHost`s run their commands by, so a poll an
SSH round trip holds up ends too. Like `doctor`, it needs no herdr pane. It reads the projects' files and never
writes them; the one thing it changes is what a run's agent is given to read, by `/agent/input`, as the human would
type it into the agent's pane.

- **Projects.** Each request names its project by `cwd` and an optional `machine`. The server connects each
  pair once through `project_host`: the `Host` that reaches the machine, the directory resolved on it, and the
  `AgentConsole` (`herdr.rs`) that reaches the herdr the runs' agents are in, here or forwarded to the machine. It
  keeps them as a `ProjectRuns`, with `target_args` of the pair for the records' resume commands. A run started or
  resumed without a machine saves in its state which herdr server of this machine it reached (`herdr_server`: the
  engine's `HERDR_SOCKET_PATH` and `HERDR_SESSION`, each set or unset), and the console sends each command to an
  agent with exactly those two variables, whatever session serve itself runs in or none: so a window started from
  a file manager still reaches agents a run started in a named session's pane, and a run in the default session is
  reached there from a window inside another session. Only a run saved before runs saved it is reached in the
  session serve's own environment picks: `HERDR_SOCKET_PATH`'s, else `HERDR_SESSION`'s, else the default one. On a
  machine, herdr forwards to the machine's own herdr, and nothing is saved.
- **Endpoints.** Every request names the project by `?cwd=` and, optionally, `?machine=`, and needs the token:

  | Endpoint | Gives |
  |---|---|
  | `/runs` | `{"version": 1, "runs": [...]}`, `list --json`'s document (`run_record` of each of `Host::run_states`) |
  | `/files?run=ID` | the files in the run's directory (`Host::run_files`), oldest first, each `{"name", "mtime"}` |
  | `/file?run=ID&name=NAME` | `{"run", "name", "text"}` for one of them, read through `Host::read` |
  | `/agent?run=ID&role=ROLE` | `{"run", "role", "text"}`: the last lines (`&lines=N`, 200 by default) of what the run's agent of the role shows, through `herdr agent read` |
  | `POST /agent/input?run=ID&role=ROLE` | types the JSON body's `{"text"}` into that agent as a prompt (`herdr agent prompt`), or presses its `{"keys"}` (`herdr agent send-keys`) |
  | `/events` | a `text/event-stream` of `record`, `files` and `problem` events; see Events below |

  `RunServer`'s doc comment is the authority on them.
- **Events.** Each `/events` stream has its own `RunWatch`. It polls `run_states` every second, and every 5 for
  a `--machine` project (`SERVE_POLL`), and compares what it reads with what the stream
  last sent. A run whose record changed, other than in `AGING_RECORD_KEYS`, gets a `record` event, and one whose
  files changed a `files` event; a run whose `state.json` is gone gets a `record` event with `null`. The first
  poll sends every run. An unfinished run's files are read on every poll, a finished or failed run's only when
  its record changes. A project that cannot be read gives one `problem` event, and another with `null` once it
  can. A stream with nothing to send writes an SSE comment instead, so a client that has gone is noticed at the
  next poll.
- **Security.** Any local user can connect to a port on `127.0.0.1`, and any web page can ask the browser to.
  So `RunServer` checks every request, in this order. It answers 403 to a `Host` header that is
  not `127.0.0.1:<port>` or `localhost:<port>`, which is how a DNS-rebinding page's requests arrive. It also
  answers 403 to an `Origin` that is not the server's own or one given with `--origin`. The Tauri shell passes
  its webview's origin: `tauri://localhost`, or Vite's dev server under `tauri dev`. It then wants
  the token, 32 random bytes as `secrets.token_urlsafe` wrote them, compared in constant time as bytes
  (`same_bytes`). It may come as `Authorization: Bearer` or,
  for an `EventSource`, which cannot set headers, as `?token=`; without it the answer is 401. A CORS preflight
  (`OPTIONS`) carries no token, so it gets only the `Host` and `Origin` checks, and a 204 that allows `GET` and
  `POST` with `Authorization` and `Content-Type`. `Access-Control-Allow-Origin` echoes only an origin that passed. `ProjectRuns` lets a
  run id or file name be only a plain name (`plain_name`: no `/`, not `.` or `..`) and refuses `state.json`.
  It refuses such a path before anything is read (`RunReadError::Refused`), which the server answers with 403. An
  agent is named only by its run and role, which `ProjectRuns` looks up in the run's `state.json`, so a client can
  read or type into no other herdr agent; a POST body is read to at most 64 KiB (`MAX_BODY_BYTES`).
- **Streams.** An event stream to an HTTP/1.1 client is chunked, so that it ends with its last chunk at a stop; to
  an HTTP/1.0 client it ends with the connection. The requests tiny_http reads otherwise than the Python engine's
  http.server did are `TODO.md`'s Open issue "serve answers some requests otherwise than Python's http.server".

## The desktop app: `ui/`

A Vite + React + TypeScript frontend in `ui/src/`, in a Tauri 2 shell in `ui/src-tauri/` (`create-tauri-app`'s
layout), with no Python of its own.

- **Shell.** `src-tauri/src/lib.rs` starts the sidecar in `setup`: `serve --origin <the webview's origin>` of
  the orchestrator `Orchestrator::locate` picks, `$ORCHESTRATOR_BIN`, else the binary beside the app's
  executable that a release bundles (`BUNDLED_NAME`), else the checkout's own build, `target/debug/orchestrator`
  of the checkout the app was built in. It reads the first line and keeps the URL, the token and the checkout's
  directory, null for any binary but the checkout's build, or why it failed, for the `server` command. With no checkout and no project opened before,
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
  `serve` sent over a scripted run. `runs.test.ts` covers the pure helpers. `cargo test -p octoflow-ui` starts
  and stops a real sidecar.

## Tests

`cargo test --workspace` runs the engine's tests (above) and the desktop shell's. `uv run pytest` runs the rest:

`tests/test_gate.py` covers `gate.py`: each check on a real git repository in a temporary directory, the quality
file `quality_report` writes, and `TestGate`, the program itself, with stub linters on `PATH`.
`tests/test_properties.py` holds the gate's diff and path parsers to git, by Hypothesis; `tests/test_invariants.py`
and `tests/test_docs.py` hold the rules the docs state, and the docs to the code: the commands and flags the engine's
help lists, the names `engine/src` and `gate.py` define, and every link.

`tests/parity/` is the parity harness, the oracle the Rust engine was ported against: it runs the engine as a program,
with nothing of it imported. Each scenario in `scenarios.py` scripts a `World` (`world.py`): a git project on `main`
with a bare `origin` beside it, the fake herdr, gh and ssh of `fakes.py` first on `PATH` and the real gitleaks after
them (TODO.md says what a missing one does), and Jenkins, and PyPI behind an HTTPS proxy, served from the test process.
It runs this checkout's engine there, built with cargo (`tests/rust_engine.py`), or the program `PARITY_ENGINE` names,
killing and resuming it where the scenario says, and the world records, normalized by `normalize.py` (times, run ids,
pids, hashes, its own paths and ports): each invocation's exit status, the output programs read whole and other
output only as its facts; the commands herdr and gh got and the requests Jenkins and PyPI got, in order, with
`state.json` wherever it changed between them; the run directory's files; and both repositories' refs, commits and
working trees. `test_parity.py` fails a scenario whose record differs from `recorded/<scenario>.json`, naming where.
The recordings are the Python engine's, made before rust-engine ticket 11 removed it; while both engines were there,
each scenario that kills a run and resumes it also ran with one engine before the kill and the other after it, so a
run either engine left resumes in the other. `PARITY_RECORD=1` writes recordings instead, from the Rust engine, for
a scenario added since, whose recording is then a review of what the engine does. `PARITY_COMMANDS` (`run,resume`,
say) leaves out each scenario whose recording invokes the engine with another command. A kill ends the engine's
group and each process below it, since the engine runs each command in a group of its own.

Golden files hold generated text to a reviewed copy of it under `tests/golden/`: `assert_golden` (`tests/golden.py`)
fails with a unified diff naming the file when the text differs, and `UPDATE_GOLDEN=1 uv run pytest` rewrites the
files instead. A change to that text is reviewed as the diff of its golden files. They hold two texts. `gate.py`'s
quality file, which the Builder and the Reviewer read and which no parity scenario writes, since their Jenkins serves
hand-written ones: `TestQualityReport` holds `quality_report` to a file per gate word and per way it words a section,
and `TestGate` the whole file the gate writes for a passing and a failing change, `judge_change`'s intro, conditions
and coverage sentence included. And the engine's prompts: each step's prompts in
the built-in workflow and each example workflow, as `Workflow` renders them, to a file per workflow, round 1 and,
for the steps the review loops over, round 2 as the running session and a fresh one hear it; the Python engine's
tests wrote them, and the engine's `workflow/tests/default_prompts.rs` holds the Rust engine to them. The parity scenarios
record round 1 of each workflow as sent, so a reworded prompt fails both. Of round 2 of the examples and of a fresh
session's prompts, the recordings hold only the templates, in `state.json`; the text as sent, placeholders filled in
and a fresh session's prompt put together, only the golden files hold. The engine's
other texts, its follow-up prompts, notes, commit messages and pull request bodies, have no golden files: the
scenarios record them as sent, where one reaches them. TODO.md lists the texts neither reaches.

`tests/test_evals.py` covers the eval runner, `evals/run.py` ([Evals](design.md#evals)), with
`tests/fake_engine.py` as its engine, a program that plays a scripted run in the clone, and the eval tasks: each
bundle is of its source, and each hidden test rejects its task's starting point and accepts a solution of it. No
agent runs in the suite.

## Tooling

- `pyproject.toml`: `gate.py`'s project, with no runtime dependencies, Python 3.13+; pytest, pytest-cov, Hypothesis,
  mutmut and the linters in the `dev` group. Its version is the release's.
- `ui/package.json`: the desktop app's npm scripts, `dev`, `build`, `test`, `typecheck`, `lint` and `tauri`. Its
  `.npmrc` sets `legacy-peer-deps`, because npm 9.2 as Debian ships it cannot resolve Vitest's optional peers.
  Jenkins runs the frontend's `typecheck`, `lint` and `test`, and the shell's tests under cargo-llvm-cov.
- `Cargo.toml`: the Cargo workspace, whose members are the engine (`engine/`, `octoflow-engine`, whose version is
  `pyproject.toml`'s) and
  the desktop shell (`ui/src-tauri/`), with one `Cargo.lock`, the lints every member takes (`[workspace.lints]`), and
  `rust-toolchain.toml`, the toolchain and components the Jenkins agent's image has.
- `packaging/build-desktop.sh`: builds the desktop app with `tauri build`, the binary `build-binary.sh` built
  copied in as its `externalBin` sidecar and `pyproject.toml`'s version, through a release config the script
  writes and merges over `tauri.conf.json`, which bundles nothing. It writes
  `dist/octoflow-desktop-<os>-<arch>` `.deb` and `.AppImage` on Linux, `.dmg` on macOS (signed ad hoc),
  each with its `.sha256`, and refuses a bundle without the sidecar or without `build-binary.sh`'s third-party
  licenses, which it bundles as the resource `THIRD-PARTY-LICENSES.html`. On Linux it then runs the AppImage as the
  CLI from a temporary directory: `--help` and `workflows` exit 0, and the orchestrator `serve` execs starts in
  that directory with no `$APPDIR` entry in `LD_LIBRARY_PATH` and no `PYTHONHOME`, read from its `/proc` entry.
- `packaging/build-binary.sh`: builds `dist/octoflow-<os>-<arch>` (`linux-x86_64`, `macos-arm64`), the engine's
  `orchestrator` binary, `cargo build --release --locked`, and its `.sha256`, then smoke-tests it: `--help`, and
  `workflows` lists `default`. Before the build, cargo-about writes `dist/octoflow-third-party-licenses.html`, the
  license texts of the crates in the binary and the desktop app, by `packaging/about.toml` and `about.hbs`; a crate
  under a license `about.toml` does not accept fails the script.
- `Jenkinsfile`: `uv sync --frozen`, the engine built, `uv run pytest` with branch coverage of `gate` (without
  `tests/parity` in a quality build whose change touches nothing the scenarios judge; the Test stage lists what) and
  the Cargo workspace's tests with line coverage (`cargo llvm-cov nextest`), a UI stage that runs `ui/`'s `npm ci`,
  `typecheck`, `lint` and `test`, failing the build before the gate, a Gate stage that runs `gate.py` and archives
  `quality.md` (see [quality gate](quality-gate.md)), and a Package stage that runs `packaging/build-binary.sh` and
  `packaging/build-desktop.sh` on every build but a quality build (`GIT_REF` set). On `main` a Release stage publishes a
  new `pyproject.toml` version as a GitHub release, with the Linux binary and the Linux desktop app, each with its
  `.sha256`, and `octoflow-third-party-licenses.html`, in one `gh release create`, and `docs/releases/v<version>.md`, where there is one, before GitHub's
  generated notes. Its parameter `GIT_REF`
  builds another branch; such a build never releases.
- `.github/workflows/automerge.yml`: keeps auto-merge on for a pull request into `main` from a branch of this
  repository, so it merges with a merge commit once Jenkins' required check passes; its header lists the pull
  requests it leaves to a human.
- `.github/workflows/macos-binary.yml`: Jenkins has no Mac, so it runs `packaging/build-binary.sh` and
  `packaging/build-desktop.sh` on `macos-latest`. When a release is published, or by hand for an existing tag, a
  second job uploads `octoflow-macos-arm64`, `octoflow-desktop-macos-arm64.dmg` and their `.sha256` to that release
  with `gh release upload --clobber`; with only `contents: write`, it never creates a release or a tag. For a pull
  request that touches the packaging, the UI or the engine, it only builds, with
  `contents: read`, as an advisory check. Everything else stays in Jenkins.

How to set Jenkins up for it is in [quality-gate.md](quality-gate.md).
