# TODO

What is known to be open in `orchestrator.py`, and why. An entry leaves `Open issues` only for `Fixed`, with what
was done.

## Planned

- **The rename to Octoflow AI, the rest of it.** The product is Octoflow AI (README, window titles, pull request
  text) and the binaries, release assets, `.deb` path, Tauri bundle and crates are `octoflow`; the bundle name has
  no space because Tauri names the `.deb`, `.app` and `.dmg` after it verbatim. Still under the old name, each a
  decision of its own: `~/.config/ai-agents-orchestrator/`, the
  `.orchestrator/` project directory and the `orchestrator-ci/` branches, which are on users' disks and remotes and
  need a fallback that reads the old location; `orchestrator.py` and the `Orchestrator-*` commit trailers; the
  Jenkins folder `AI-Agents-Orchestrator`. The macOS app could show "Octoflow AI" in Finder while staying
  `Octoflow.app`, with `CFBundleDisplayName` in a `src-tauri/Info.plist`, unchecked on a Mac. Agreed 2026-10-25.

- **The desktop app as the terminal client too: the macOS check.** Started with arguments, the app execs the
  orchestrator it bundles (`cli` in `ui/src-tauri/src/lib.rs`), so the AppImage is the CLI too and the README says
  how to get the CLI from each download; on macOS, where a `.app` is a directory, the CLI is a symlink to
  `Octoflow.app/Contents/MacOS/octoflow`. `build-desktop.sh`'s `smoke_dmg` mounts the `.dmg` and runs the CLI
  through such a symlink and as the app's own executable, in `.github/workflows/macos-binary.yml`'s build of each
  release and of each pull request that touches the packaging. Left: checking the README's quarantine advice for
  the `.dmg` against a downloaded copy (`.scratch/desktop-cli/issues/02-macos-cli-symlink.md`). The plain CLI
  binaries stay released: 13 MB against the AppImage's 120 MB of WebKit, and they start without FUSE on headless
  machines and in CI. Decided against: one file on every platform, by embedding the orchestrator's bytes in the
  Rust executable and unpacking them at start (PyInstaller's onefile done twice), or by Python hosting the window,
  which [ui-options.md](docs/ui-options.md) chose against. Agreed 2026-10-24.
- **The quality gate in a `gate.py` of its own.** The gate is reusable for other Python projects, but lives inside
  `orchestrator.py`; and it can only run inside the judged project's environment anyway, since it starts pytest
  and mutmut with `sys.executable`, which in the binary is the binary. Decided 2026-10-24: the quality file is the
  one interface: the engine reads and amends it (`parse_gate`, `gate_failures`, `judge_run`) and imports nothing
  from the gate; each side keeps its own copy of the pieces of the format it needs, the gate's the home, and
  `TestQualityFileContract` checks they agree. The engine writes a BUILD_FAILED file itself, minimally
  (`build_failed_report`: status line, intro, failing tests or the console's end, no build-configuration or
  mutmut-state listing). `gate.py` is one stdlib-only file beside `orchestrator.py`, so a quality build can still
  take main's with `git show origin/main:gate.py`; it is the whole program, `python gate.py --base REV ...`, with
  no subcommand, and the orchestrator loses `gate`. During the cutover, while main has no `gate.py`, the
  Jenkinsfile falls back to main's `orchestrator.py gate`. The gate moves to its own repository only once a second
  project uses it. The seam is drawn: the gate's block in `orchestrator.py` (from "Everything from here to the line
  that ends the gate's block" to that line) uses nothing outside it, and nothing outside uses it but the `gate`
  subcommand (`add_gate_arguments`, `gate_command`). Left: the cut itself, with its tests, CI and docs, then the
  fallback's removal, then making the test-file patterns a setting for other projects.

## Open issues

- **surviving-mutants takes only the top directory's pytest configuration.** Since `_pytest_groups`, flaky-tests and
  passes-at-base run each changed test under the configuration pytest finds for it below the copy's top, but
  `_pytest_config`, which surviving-mutants gives mutmut, still looks in the top directory alone, so a project whose
  configuration is in `tests/pytest.ini` or `pkg/pyproject.toml` runs its mutants without that configuration's
  `pythonpath`, `addopts` and plugins. Raised by cross-review 35 (R1-6). Closes with mutmut's runs taking the changed
  functions' tests' configurations, which mutmut's one `pytest_add_cli_args` makes one per run at most.
- **flaky-tests' reruns get one time limit a run, whatever the number of configurations.** Each pytest run of
  flaky-tests has `FLAKY_RUN_TIMEOUT` of its own, and since `_pytest_groups` a change whose tests fall under several
  configurations collects and reruns once for each, so its worst case grows with them (3 configurations and 5 seeds:
  3 x 6 x 30 s), against `QUALITY_TIMEOUT`. passes-at-base's groups share its one budget. Raised by cross-review 35
  (R1-5), which reads the spec's "the per-group runs share the check's time budget" as meant for passes-at-base,
  flaky-tests having none. Closes with the shared budget `.scratch/gate-open-decisions/issues/01-shared-time-budget.md`
  asks about.
- **SSH connection sharing has not been timed on a real `--machine`.** `ssh_sharing_options` was measured against
  a user-level sshd on this host only, 20 commands in 0.24 s shared against 1.78 s unshared; no herdr machine is
  saved here, so `doctor --machine` was not timed before and after
  (`.scratch/doctor-ssh/issues/01-ssh-connection-sharing.md`, requirement 4). Raised by cross-review 33 (F7). Closes
  with `time orchestrator.py doctor --machine <m>` on the herdr machine, before and after, both times written into
  that ticket.
- **`--machine` over SSH assumes a POSIX login shell and a server that takes several sessions.** Since
  `_run_tool`, doctor takes a remote command's exit status 127 for not on PATH: a csh or tcsh login shell exits 1
  ("Command not found."), so a missing gh reads as `gh auth status fails` and a missing git as a failed
  `--version`; and a tool on PATH that exits 127 itself, such as a shim whose target is gone, reads as not on PATH.
  And `ssh_sharing_options` overrides a target's own `ControlMaster no`, so a server with `MaxSessions 1` refuses
  the second of two commands run at once on the shared connection (the GUI and `serve` run several), which ssh
  then fails. Raised by cross-review 33 (F4, F6), neither reproduced. Closes with doctor asking through `sh -c`, as
  `_on_path` did, and with an explicit `ControlMaster`/`ControlPath` in `ssh -G <target>` left in place.
- **`TestGate.test_a_missing_tool_fails_naming_it` fails where shellcheck lives beside git.** `TestGate.setUp`
  (`tests/test_orchestrator.py`) puts git's own directory on `PATH` after the stubs, so on a machine with
  `/usr/bin/shellcheck` (agentbox, Debian's package) removing the stub still finds the real one and the gate passes.
  Seen 2026-10-25 on `main` at `9448612`. Closes with `PATH` holding a directory with only a link to `git`.
- **unexpected-paths reads only the contract step's file.** `unexpected_paths` judges each path the change touches
  against `RunChange.spec`, the contract step's file, so a workflow whose contract is in business terms, such as
  `examples/workflows/product-team.toml`, whose requirements name no file and whose `design.md` lists the
  components, gets a finding to weigh for every path on every quality round. Raised by review 20 of
  product-team-workflow (F4, 2026-10-25): "the gate reads file names from the spec and this spec is in business
  terms by design". The example's header says so. Closes with the check reading every non-editing step's file that
  precedes the first editing step, or with a workflow key naming the file that holds the paths.
- **An AppImage run without FUSE passes no signal on to the orchestrator.** Under `APPIMAGE_EXTRACT_AND_RUN` the
  AppImage's runtime forks, runs `AppRun` in the child and waits, so the process a caller started is the runtime,
  and `Cli::exec` (`ui/src-tauri/src/lib.rs`) only replaces the child. Raised by the cross-review of desktop-cli
  (F3, 2026-10-25), reproduced with `dist/orchestrator-desktop-linux-x86_64.AppImage serve`: "`kill -TERM <runtime
  pid>` → both orchestrator processes still running 2 s later, orphaned", so `timeout`, a systemd stop or `kill %1`
  on an AppImage `run` on a machine without FUSE leaves the orchestrator and its agents running unsupervised.
  Ctrl-C reaches the whole process group and is not affected; the mounted AppImage was not checked. Closes with
  `Cli::exec` asking the kernel for a signal when its parent, the runtime, dies (`PR_SET_PDEATHSIG`) when the app
  runs extracted, checked on a built AppImage, or with a runtime that forwards signals.
- **An AppImage run without FUSE leaves its extraction in `/tmp`.** Under `APPIMAGE_EXTRACT_AND_RUN` the
  AppImage runtime that linuxdeploy-plugin-appimage bundles extracts into `$TMPDIR/appimage_extracted_<hash>` and
  never removes it. `packaging/build-desktop.sh` sets the variable for the whole Tauri build, so linuxdeploy and
  appimagetool leave two such directories on the build machine at every build (Jenkins's agent among them). Users
  who run the desktop AppImage as the CLI without FUSE are left with one 336 MB directory per version. Its smoke
  test already points `TMPDIR` into its own directory. Found in the second build round of run 20261025-081413-327fc2
  (2026-10-25). Closes with a `TMPDIR` the build removes around `tauri build`, and with a README note, or a newer
  runtime that cleans up, for users.
- **No build has yet been seen waiting for `ci/cargo-lock.sh` on the Jenkins agent.** PR #88's builds ran both
  cargo steps through it on agentbox0 (builds 1-3, 2026-10-25), so the agent image has `flock` and the lock file's
  directory is writable, and a quality-style build (`GIT_REF` set) ran `--no-wait`; but the two run side by side
  reached their cargo steps 15 s apart, the lock free each time, so the wait and its message were seen only on the
  development host. Raised by cross-review 25 (F3). Closes with two builds whose cargo steps overlap on agentbox0,
  one saying that it waits for the lock and both passing.
- **Is 3 GB the right memory limit for the agentbox0 Jenkins agent?** All builds on the node share its
  container's `mem_limit: 3g` (Homelab `jenkins/agentbox0.yaml`), which this repo cannot change. Since
  `ci/cargo-lock.sh`, two of this repository's builds compile Rust at once only when a quality build starts its
  `cargo test` beside another build's cargo step, but everything else they run (pytest, mutmut, PyInstaller, npm)
  still shares the 3 GB beside a locked rustc, as do other jobs' builds on the node. Open in
  `.scratch/ci-cargo-memory/issues/02-agent-container-memory.md`; closes with the Homelab keeping or raising it.
- **mutmut's clean test run fails on a hypothesis health check, so the gate mutation-tests nothing.** Under the
  gate's mutmut 3.8.0, `TestChangedLines.test_names_each_changed_file_and_exactly_its_added_lines` in
  `tests/test_properties.py` fails with `hypothesis.errors.FailedHealthCheck: ... was called from multiple
  different executors`, mutmut exits 1 with "Failed to run clean test", and `surviving-mutants` reports `not-run`
  for every change. Seen on Jenkins build #48 and reproduced locally at `a3b18b6` with a one-line edit to
  `drain_end` (2026-10-25); a plain `uv run pytest` passes it. Cause not established. Closes with the test passing
  under mutmut, e.g. by suppressing `HealthCheck.differing_executors` for it, or the gate's mutmut runs leaving
  `tests/test_properties.py` out.
- **herdr 0.9.1 lists no `gemini` integration.** `herdr integration status` on this host lists `antigravity-cli`
  (whose hook is under `~/.gemini/`) but no `gemini`, so `doctor` warns that it does not list gemini, and the
  README's `herdr integration install gemini` may name a kind herdr no longer has. Found by the doctor run
  (2026-10-24); not checked whether `agent start --kind gemini` still works. Closes once `AGENT_KINDS` and the README
  name the kind herdr installs for Gemini, or herdr is shown to accept `gemini`.
- **`doctor` checks gitleaks's `git` subcommand, not its version.** README asks for gitleaks 8.19 or later; the
  check reuses `Host.gitleaks_problem`, the run's pre-push check, which asks `gitleaks git --help` because a
  distribution's build may print no version number. So `doctor` shows no gitleaks version. Kept by choice
  (2026-10-24).
- **No test shows the proof reruns come after the quality loop.** `Workflow._rerun_proofs` runs right before the
  verdict step's turn, which follows the quality loop by construction, but every proof test uses `QUICK`, which has
  no quality-gated step. Raised by the cross-review of the proof reruns (R1-F5, 2026-10-24): "no test would notice
  the rerun moving before it". Closes with a proof test on a quality-gated workflow, through the CI fakes.
- **A spec left `ready-for-human` by a conflict stays there when `refresh` clears the conflict before a drain does.**
  drain moves such a spec to `in-review` only when its own refresh clears the run's conflict (`refreshed_headers`);
  a run cleared by `orchestrator.py refresh` beforehand has no conflicts left, so drain cannot tell the spec held
  back only by its conflict from one the human set `ready-for-human`, and leaves it, with an open pull request
  ready for review that no drain marks `done` once merged. Kept so that drain never overrides a status the human
  set. Raised while building the refresh command (2026-10-25). Closes with the run recording that a refresh cleared
  its conflict, which drain then reads, or with refresh moving the spec itself.
- **Proofs run after the last round's review too.** No Builder turn follows to read them; only the draft pull
  request's review shows them, and each may take `PROOF_TIMEOUT`. Kept: a demonstrated defect is what the human
  most needs to see in a draft. Raised by the same review.
- **A proof in a worktree run finds the checkout's pytest configuration.** The run directory, and with it
  `RunState.proofs_dir`, is under the project checkout, not the run's worktree, so a `proof_command` of
  `python -m pytest {proofs}` takes its rootdir and `conftest.py` from the checkout while importing the code from the
  worktree it runs in. Found while writing loop-guards ticket 05; not reproduced. Closes with `_run_proof` passing
  the working tree as pytest's rootdir and conftest root, or the proofs copied into a scratch directory of the
  worktree for their run.
- **`expected-value-changed` misses an inline literal's sign, type or structure.** `_changed_literals` pairs
  assertions by `_shape` and compares `_literals`, which drops types and operators: `assert f() == -5` becoming
  `== 5` changes the shape (no pair, no finding), and `== 1` becoming `== True` gives equal literal lists
  (`[1] == [True]`). Raised by the cross-review of `test-integrity-constants` (F4, 2026-10-27): "`_literals`
  equality misses sign, type and structure changes"; the constant path compares `_tree_fingerprint`s instead.
  Closes with the inline path comparing typed literals and pairing a unary minus with its operand.
- **A run that never reaches `done` keeps its local refs.** A locking step's commit stays under
  `refs/orchestrator/<key>/locked/` (`RunState.lock_ref`) until the run ends `done` (`Workflow._clean_up_locks`);
  `close` and `prune` do not delete them, so a failed run's locked snapshots are never collected by `git gc`. Closes
  with `close` and `prune` deleting `RunState.refs_prefix` as they delete the run's branch.
- **An agent's pull request can reach `main` on rules it wrote itself, with no human.** Jenkins's App reports
  `continuous-integration/jenkins/pr-head`, which the agents' token cannot post, but a pull request build runs
  the pull request's own `Jenkinsfile` and gate (`docs/quality-gate.md`, What the Gate stage judges), and since
  `automerge.yml` a pull request into `main` merges once that status is green. Raised by the cross-review of
  `pr-automerge` (F5, 2026-10-24): "A branch the agents push under any other name, once a pull request is open on
  it, merges into `main` (and so into a release) without a human". The orchestrator's own pull requests, under
  `orchestrator/`, were held for a human until the user decided on 2026-10-24 that every pull request of this
  repository merges on a green build; so a run's change now reaches `main` and a release unreviewed too. Closes
  with an approving review required on `main` that the agents cannot give (Homelab `.scratch/agent-identity`,
  ticket 03), or a Jenkinsfile and gate a pull request build takes from `main` rather than from the pull request.
- **A pull request moved off `main` can merge unchecked before `automerge.yml` turns auto-merge off.** No ruleset
  requires a check outside `main`, so once a pull request with auto-merge on targets another branch and has no
  conflict, GitHub merges it at once, racing the run that would turn auto-merge off; and a conflict cleared from
  the base side starts no run. Raised by the cross-review of `pr-automerge` (R3-1(b), 2026-10-24): "After the push
  that resolves the conflict, the PR is CLEAN against a base with no required check, so GitHub merges it at once
  and unchecked", "In (b) that run still races GitHub's immediate merge. A conflict cleared by a change to the base
  rather than the head raises no pull-request event at all." Closes with a ruleset that requires the check on
  every branch, or one that restricts merges outside `main`.
- **The secret scan does not look inside a file git takes for binary.** With no attributes, git still calls a file
  binary when its first 8000 bytes hold a NUL, and `git log -p` prints "Binary files ... differ" for it, to gitleaks
  and to `Host._allow_comments` alike, so a secret or a `gitleaks:allow` in such a file is pushed unscanned. gitleaks'
  own `git` mode has the same blind spot. Raised by the cross-review of `push-secret-scan` (F15, 2026-10-24). Closes
  with a decision in the spec whether binary content is in scope, and, if it is, a `--text` diff or a scan of the
  blobs themselves (`gitleaks dir` over the pushed files).
- **The gate's intro writes the `--rules` path as it is.** `judge_change`'s intro names the rules file without
  `one_line` (`orchestrator.py`, `intro = ...` in `judge_change`), so a rules path holding a line breaker splits the
  quality file's third line, and `docs/quality-gate.md`'s "wherever it appears in a list item or a name" says more
  than holds for it. `gate_failures` does not read the intro, so no round is misjudged. Raised by cross-review 38
  (round 3, 2026-10-26). Closes with the intro written through `one_line`, or with each quality-file writer escaping
  once where it joins its lines.
- **Small leftovers in the changed-file walker and its decorators.** Raised by cross-review 45 (F2-F5, 2026-10-26),
  none giving a wrong result: `_ChangedFunction.decorators` unparses each decorator only for `_unmutatable`'s
  `staticmethod`/`classmethod` test, which an `ast.Name` check would make without `_unparsed`; `_parsed_changed_files`
  computes `_removal_points` for `_judged_files` and `vague_names`, which never read them; `_ChangedPythonFile` is a
  frozen dataclass with `set` fields, so its generated `__hash__` raises; `vague_names` unpacks three fields and reads
  `.why` beside them. Closes with the four changed, in one commit, when the walker is next touched.
- **A flaky-tests rerun sometimes waits out `FLAKY_RUN_TIMEOUT` on Jenkins.**
  `TestFlakyTests.test_a_server_a_test_leaves_holding_the_output_does_not_hold_up_the_reruns` takes 8.0 s on
  Jenkins (PR-98, PR-99/3, PR-100), but 14.4 s on PR-101 build 1 and 31.0 s on build 3, failing its 30 s bound: one
  of its six pytest runs waited about the whole `FLAKY_RUN_TIMEOUT`. PR-101 changes only the tests' git fixture.
  Twenty runs here under the load of a parallel suite, with SIGINT ignored, all passed. A rerun that hangs gives a
  `not-rerun` warning, not a wrong verdict. Closes when the run that waits is caught, e.g. with each pytest run's
  wall time and exit in the test's failure message, and its cause fixed.
- **README and docs left over from the README rewrite (PR #90).** Raised by cross-review 50 (2026-10-26), in what
  PR #90 brought, not in code:
  - The Jenkins job path still uses the old folder name, as
    `--quality-gate AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality` (README.md, the `--quality-gate`
    example; also docs/quality-gate.md), where the folder is `Octoflow-AI` and the job `py-octoflow-ai-quality`.
    The Planned rename entry above still lists the folder as not renamed.
  - docs/architecture.md's endpoint table gives `/files?run=ID` as a list of `{"name", "mtime"}`, while
    `RunRequestHandler`'s docstring, which the table names as the authority, gives `{"run", "files"}`.
  - The README's `gate` example says exit 0 for OK and 1 for ERROR, but not 2, unjudged, when no quality file is
    written.
  - Smaller: a short line mid-paragraph in the rename entry above; the README no longer says which commands take only
    `--machine`/`--cwd`; the `gate` example no longer names the tools it needs (ruff, mypy, pymarkdown, shellcheck,
    actionlint).
  Closes with those lines corrected, or the endpoint table replaced by a pointer to the docstring.
- **`vague-names` does not split CamelCase.** `vague_names` matches a whole name or its last `_`-separated word,
  so `user_data` is found but a class `InfoManager` or `UserData` is not. Closes, if the run metrics show such names
  recur, with the last CamelCase word matched too.
- **`vague-names` reports a name an undecorated override cannot change.** A `def handle(self)` in a
  `socketserver.BaseRequestHandler` subclass, or a parameter the overridden signature names, has no fix; only a
  method marked `@override` is skipped (`_bound_names`). Raised by the cross-review of `written-rules` (F6,
  2026-10-24): "names fixed by a parent class or framework are reported, though they cannot be renamed". Closes, if
  the run metrics show it is common, with methods whose class has a base from outside the repository left out.
- **`passes-at-base` lists the whole tree of the base, and of each submodule, to find its submodules.**
  `_unpack_commit` runs `git ls-tree -r -z` on every run, even in a repository without `.gitmodules`, and splits the
  whole listing, only to find the entries of mode 160000. Raised by cross-review 41 (F9, 2026-10-26): "a full
  recursive `ls-tree` on every run, only to find gitlinks". Checking `<commit>:.gitmodules` first would skip a gitlink
  that has no `.gitmodules` entry, such as a nested repository added by mistake, which is filled today. Closes, if run
  metrics show the listing costs, by listing only the paths `.gitmodules` names, with such gitlinks left out.
- **The copy's `src/` on `PYTHONPATH` comes before the standard library in `passes-at-base`'s run at the base.** A
  module there named as one of the standard library's, such as `src/token.py`, shadows it, as one at the copy's top
  already did through `python -m`. Raised by cross-review 41 (round 1, not recorded, 2026-10-26). Not seen failing.
  Closes, if it is, by putting the roots on the path after the standard library and before site-packages.
- **A test file that cannot be imported at the base hides all its changed tests from `passes-at-base`.** A
  module-level import of something the change adds makes pytest's collection of the whole file fail at the base,
  which `_collect_at_base` takes as the expected outcome for every changed test in it, including ones that would pass
  there. Accepted: the check reports only, and splitting the file's tests from its imports would mean rewriting
  them. Closes, if its run metrics show these files are common, by importing such names inside the tests that use
  them, or by running each test with the failing import stubbed.
- **A submodule under `tests/` whose commit the change moves makes `passes-at-base` not-run.** `_beside_tests`
  takes the submodule's path for a file that goes with the tests, so `_copy_base_with_tests` removes it with
  `os.remove`, which refuses a directory, and the copy fails as a whole (`could not copy the base`). It was so before
  submodules were filled (`_unpack_commit`). Found while filling them (2026-10-26), not seen failing. Closes when the
  overlay leaves a path the base records as a submodule (mode 160000) to `_unpack_commit`.
- **A test file whose name is not UTF-8 is never rerun by `flaky-tests`.** pytest-randomly 5.0.0 hashes each node
  id as strict UTF-8 (`pytest_randomly._crc32`, called from `pytest_runtest_setup` and its neighbours) and stops
  with an internal error, exit status 3. `_pytest` already lets pytest print such an id
  (`PYTHONIOENCODING=utf-8:surrogateescape`), and the collection runs with `-p no:randomly`, so the id is found;
  each rerun then crashes, and `flaky_tests` reports `not-rerun` quoting the `UnicodeEncodeError`. Raised by the
  cross-review of `flaky-tests` (F10, 2026-10-23). Closes when pytest-randomly hashes the id's bytes, or when the
  reruns can seed such a test without it.
- **The reruns' time limit is too short for slow tests on the Jenkins agent.** `FLAKY_RUN_TIMEOUT` is 30 s a run. On
  `flaky-tests`' own change each run took about 9 s on an idle laptop and about twice that under load. Measured on
  Jenkins on 2026-10-24 (PR #50, surviving-mutants' change): the first rerun did not finish within 30 s, so the check
  reported `not-rerun` and judged no test; the tests that change edits start mutmut, and rerunning their four test
  classes once takes about 50 s on the laptop. A limit that covers them (about 90 s, five seeds) would push the quality
  build past `QUALITY_TIMEOUT` beside the mutation budget, so the user kept 30 s (2026-10-24). `not-rerun` is a finding
  to weigh, never a wrong verdict. Closes with one time budget the reruns and mutmut share, or lighter tests for the
  gate's own checks.
- **A quality round waits for the whole build, not for its quality file.** `Workflow._analyse` polls the build's
  result until the build ends and only then reads `quality.md`, which the Gate stage archived before any later
  stage ran. So every stage after Gate counts against `QUALITY_TIMEOUT`; the Package stage was taken out of quality
  builds for that (2026-10-24), and a stage added after Gate slows the rounds again. Raised by the cross-review of
  `skip-package-in-quality-builds` (2026-10-24): "`_analyse` waits for the whole build instead of stopping once
  `quality.md` is archived". Closes with the round reading `quality.md` once the Gate stage has archived it, or
  with a rule in the Jenkinsfile that nothing after Gate runs in a quality build.
- **The mutation budget leaves most of a large change's mutants untried.** `MUTATION_BUDGET` is 600 s. Measured on
  Jenkins on 2026-10-24: on PR #50 (surviving-mutants' change) mutmut tried 756 of 3079 mutants and the Gate stage took
  683 s, the build about 850 s; on PR #51, 677 of 801 in a 638 s Gate stage. That fits `QUALITY_TIMEOUT` (1200 s) with
  about 350 s to spare, but not after a wait in Jenkins' queue, which counts against a quality round (PR #52 waited
  about 860 s behind the others). Those were pull request builds; a quality build has skipped the Package stage
  since 2026-10-24, so it has more to spare than that. The user kept 600 s (2026-10-24): a stopped run says `budget-reached`, never "none
  survived". Closes with the shared time budget above, or mutants chosen by the changed lines rather than whole
  functions.
- **`parse_args` still yields many survivors after `do_not_mutate_patterns = ['\bhelp=']`.** Its `description=` texts
  and flag names are mutated, and few tests check them word for word: 105 of 305 mutants survived on this branch's
  change. Reported per function, so it is one finding. Closes with a decision to add `'\bdescription='` to the
  pattern, or to leave it.
- **Mutation-testing the code that runs mutmut leaves processes behind.** When mutmut stops a mutant at its own
  timeout it kills the worker, and a real mutmut that `TestSurvivingMutants` started in its own session lives on as
  an orphan group of one process per CPU. It only happens when a change edits that code. Jenkins' process-tree
  killer should end them with the build; not checked. Closes with a check on a PR build, or with that test run
  without its own session under mutmut.
- **The surviving-mutants check selects functions by added lines only.** A function whose edit only removes lines,
  or changes only the comments or blank lines above it, is not mutation-tested, unlike flaky-tests' selection
  (`_edits_test`). Kept as the ticket states it. Closes with a decision to pass `_edits_test`-like counting to
  `_changed_python_functions`. The cognitive-complexity check has a walker of its own (`_scored_functions`), so it
  is not affected.
- **Committed mutmut output is read as the change's own code by the linters.** The checks leave `mutants/` out, by the
  user's decision of 2026-10-24 (review round 9, F33): `GateChange.from_tree` diffs the tree without it, so no check
  (test-integrity, flaky-tests, surviving-mutants, cognitive-complexity) selects a test, a function or a changed line
  there or counts an edit there as a change to the code, and the build configuration listing still names such files.
  That filter does not reach the linters: `judge_change` runs them over every tracked file their pathspecs match and
  keeps their findings on its own, unfiltered `changed_lines`, so a change that commits `mutants/calc.py`, mutmut's
  mutated copy with its trampolines, has its added lines there linted, and their findings can fail the gate. The same
  unfiltered lines feed the suppressions list, the stray-line-end refusal and the coverage condition; coverage counts
  such files only when the project's coverage measures them, since `new_coverage` reads only the files the Cobertura
  report lists. Whether the linters should read them, leave them out, or the gate refuse them is open. Closes with that
  decision.
- **mutmut reads a symlinked `.py` file in the surviving-mutants copy.** `_copy_tracked` copies a tracked symlink
  as a link (`orchestrator.py`), `_mark_for_mutmut` leaves links unmarked, and mutmut 3.8.0's `walk_all_files` lists
  a symlink to a file and `create_mutants_for_file` opens it. So once any changed function starts mutmut, a change
  adding `x.py -> calc.py` has every function of `calc.py` mutated a second time, as `x.py`, spending the budget on
  mutants no finding reports; and `x.py -> /dev/zero` or a FIFO reads without end or blocks until the budget runs
  out. The gate's own selection skips such links (`_parsed_changed_files`). Raised by the cross-review of
  `gate-python-files` (R2-F1, 2026-10-26); rests on mutmut's source, not on a run. Closes with the copy or
  `_mark_for_mutmut` keeping links out of mutmut's walk, without breaking a tracked link the tests import through,
  and a test that also edits a real function.
- **passes-at-base misses a deleted Python script with no `.py`.** `_edits_python_source` (`orchestrator.py`)
  counts a deleted file as Python source only by its `.py`, so a change that only deletes a `#!` python script
  outside the tests edits no Python source for it. Found while fixing the cognitive-complexity check's same gap
  (2026-10-25). Closes with that branch asking `_was_python_source`, which the complexity check now uses.
- **A Python script with no `.py` is never judged by the cognitive-complexity check.** `_judged_files` judges only
  paths ending in `.py`, while the pairing reads a script whose `#!` line names python as Python
  (`_is_python_source`), so a function moved unchanged into `bin/tool` keeps its base, but one the change grows
  there, or writes new over the limit, is not a finding. `docs/quality-gate.md` says "a `.py` file". Raised by the
  spec-conformance pass of review round 6 of ticket 06; ticket 06's "the Python functions the change touches" can be
  read either way. Closes with `_judged_files` taking `_is_python_source`, or a decision that `.py` is the scope.
- **A body moved into a file hides a function with that body moved out of it, which is then judged as new.** The
  cognitive-complexity check takes a base function as gone from its file when the file now holds its body fewer times
  than the base did (`_BasePool._gone`), without asking where the working tree's copies came from. So when `calc.py`'s
  `big` moves unchanged to `lib.py` and `other.py`'s `dup`, with the same body, moves unchanged into `calc.py`, `dup`
  takes `big`'s base in `calc.py` and `big` is "a new function": two unchanged moves fail the gate (review 01 round 7,
  R7-F1, a regression from the round-6 rule; deferred by the user, 2026-10-24, as rare: it needs two over-limit
  functions with one body in two of the change's files). Closes with a count that pairs each working copy with the
  base it came from, so an arrival does not make up for a departure.
- **A copy of a function whose original the change edits keeps the old score and passes.** The cognitive-complexity
  check pairs identical bodies before names (`_base_scores`), so that a function renamed unchanged keeps its score
  and a new function under its old name is new. A change that edits `big` in place and adds `copied`, a copy of
  `big`'s old body, has the same shape: `copied` takes `big`'s score and passes, and the edited `big` is judged as
  new, so nothing is reported when the edit takes `big` below the limit. The same holds for a copy in a file the
  check does not judge, such as one under `tests/`, since a function moved unchanged there must keep its base from
  a new function under its old name (review round 6, R6-F1). Decided by the user in review round 4 of ticket 06.
  Closes with a way to tell a copy from a rename, such as git's copy detection (`git diff -C`) on the change's
  files.
- **`Host._paths_between` follows the user's `diff.relative`.** Its `git diff --name-only` has no `--no-relative`,
  so with `diff.relative=true` in the user's git config and a `cwd` that is a subdirectory of the repository (not
  checked whether a run can pass one), it lists only the paths under that subdirectory, and its `_top_literal`
  pathspecs for files elsewhere match nothing. `Host.lock` then returns fewer locked paths and
  `Host.locked_changes` misses edits to them; `locked_changes`'s own `git diff` has the same exposure. Raised by the
  cross-review of `trust-quality-at-pr` (review 49, F7, 2026-10-26), which found it in `_judging_changes` and fixed
  it there with `--no-relative`; of `Host._paths_between` it said: "has the same exposure, but it is outside this
  range". Closes with `--no-relative` on both calls and a test that sets `diff.relative` and locks from a
  subdirectory.

## Fixed

- **`test-integrity` does not see an expected value held outside the assertion.** `expected-value-changed` compares
  each assertion's literals (`_changed_literals`), so editing a golden constant the assertion names, such as the
  tests' `GOLDEN_PR_BODY` or `GOLDEN_SUMMARY`, changes what is expected without a finding, while the same edit inside
  the assertion is one. Found while adding the provenance trailers to the pull request body (loop-guards ticket 03).
  Closes with the assertions' names resolved to module-level constants the change edits.
  Fixed (2026-10-27): when the code changed, `python_weakened_tests` reads each test file's constants at the base
  and now (`_PythonConstants`: a name one module-level `Assign` or `AnnAssign` binds to an expression with no call,
  bound nowhere else in the module's scope, as `_scope_bindings` counts with `_BINDING_NAME`), and
  `_changed_constants` reports `expected-value-changed` once per kept test and constant of the base whose assertions
  name it on both sides, bare or through the module's dotted name or an import alias of it, when its expression
  changed other than in layout, or it is no longer such a constant. Not closed: a constant imported from another
  module or computed by a call at the base; one built from other constants, whose edits are not seen; one reached
  through a local (`expected = GOLDEN`) or a helper that asserts rather than named in the test's assertions; a
  fixture's return value; and an inline value moved into a new constant, which requirement 4 leaves out.

- **A base `.gitleaks.toml` that extends another file reads that file from outside the base.** `scan_unpublished`
  writes the base's `.gitleaks.toml` into the run directory and passes it with `--config`; an `[extend] path = ...`
  in it is resolved by gitleaks against its working directory, not against the base, so a file the change can edit
  may supply rules. Raised by the cross-review of `push-secret-scan` (F6, 2026-10-24): "a base config using
  `[extend] path = ...` resolves that path outside base (process cwd), which the change may control". Closes by
  writing each extended file from the base beside the config and rewriting the path, or refusing such a config.
  Fixed (2026-10-26): `Host._write_base_rules` writes base's `.gitleaks.toml` into a `gitleaks-rules` directory and
  follows its `[extend] path` chain as far as gitleaks reads it (`GITLEAKS_EXTEND_DEPTH`; `_gitleaks_extended_file`
  reads keys whatever their case, as gitleaks does), writing each file from base at the path as spelled, and
  `scan_unpublished` runs gitleaks from that directory, so each path resolves to base's copy. An absolute path, one
  leaving the repository, one base has no file at, a cycle, a file that is not TOML, or `[extend]`/`path` given twice
  in different case refuses the push; so does a symbolic link in the chain, or a linked `.gitleaksignore`
  (`Host._base_rules_file`).

- **A run the budget stops can leave mutmut's results unreadable.** mutmut 3.8.0 saves a module's results by
  rewriting `mutants/<path>.meta` in place after each mutant (`SourceFileMutationData.save`, `open(..., "w")` then
  `json.dump`), so the SIGINT at the budget, or the kill after `MUTMUT_STOP_GRACE`, can land mid-write. `mutmut results`
  then exits with a `JSONDecodeError`, and `_mutmut_findings` reports `not-run`, "mutmut could not list its results",
  without the `budget-reached` finding or anything the run had shown. Seen once in another branch's gate run. Closes
  with the stop waiting for a save to end, or with the gate reading what remains and saying the budget stopped it.
  Fixed (2026-10-26): on a run the budget stopped, `_statuses_after_run` has `_drop_cut_off_saves` remove each
  `mutants/<path>.py.meta` in the copy that does not parse as JSON before `mutmut results` lists the rest. The
  functions of those modules count as not reached, and `_budget_reached` says the stop cut off mutmut's save of their
  results; the other modules' findings stand. A listing failure without a stop, one after the removal, or a `.meta`
  that cannot be removed stays `not-run`. Not closed: those modules' results are lost, not recovered.

- **Ten test classes copy the same git-repository fixture.** `TestFlakyTests`, `TestPassesAtBase`, `TestVagueNames`,
  `TestTooComplexFunctions`, `TestChangedPythonFiles` and five more each define a `setUp` that makes a repository,
  configures it and commits a base, most with the same `git` and `put` helpers; eight of the copies bind the vague
  `value` that `vague-names` now reports on added lines, and `TestVagueNames`' and `TestChangedPythonFiles`' bind
  `setting`. Raised again by the cross-review of `written-rules` (F8, 2026-10-24), and by that of
  `gate-python-files` ("`TestChangedPythonFiles` copies the repo scaffolding once more", R1-F5, 2026-10-26). Closes
  with one mixin or base class those tests share.
  Fixed (2026-10-26): `tests/gitrepo.py`'s `RepositoryTest` makes the repository in `setUp`, configures it, commits
  the class's `BASE_FILES` as "base", and gives `git`, `put`, `edit` and `commit`; `TestScanUnpublished`, `TestGate`,
  `TestWeakenedTests`, `TestFlakyTests`, `TestPassesAtBase`, `TestVagueNames`, `TestSurvivingMutants`,
  `TestTooComplexFunctions`, `TestChangedPythonFiles`, `TestHostSnapshot` and `TestJudgedInQuality` derive from it,
  and what their `setUp`s kept out of the tree (stub tools, a bare origin, a rules file) is under the base's
  `outside`. `TestHostGit` keeps a `setUp` of its own, since it makes two clones of a bare origin rather than one
  repository; its `clone` binds `setting` now. Not closed: single tests still make a repository or a directory of
  their own, such as `TestPassesAtBase`'s submodule.

- **The GitHub repository still had the old name.** It is `DavidSiegl/octoflow-ai` now, and the README's URLs and
  badges point there; GitHub redirects the old URLs.

- **A submodule is empty in `passes-at-base`'s copy of the base.** `git archive` leaves a submodule's directory
  empty, so a changed test that imports from one fails to import at the base and gives nothing, though it might pass
  there; the check misses it. Raised by the cross-review of `passes-at-base` (R1-5, 2026-10-24): "`git archive` omits
  submodule contents, so a test that imports from a submodule fails import at the base and is silently 'nothing'".
  Closes when `_copy_base_with_tests` checks out each submodule at the commit the base records for it.
  Fixed (2026-10-26): `_copy_base_with_tests` unpacks the base through `_unpack_commit`, which lists the commit's
  submodules (`git ls-tree`, mode 160000) and fills each one's directory with `git -C <path> archive` of the commit
  the base records, from its repository in the working tree, its own submodules too. One not initialised there, or
  whose repository lacks that commit, is left empty, and `_base_run_findings` names them in one `not-run` finding.

- **A `conftest.py` that fails at the base other than by `ImportError` makes `passes-at-base` not-run.** One that
  reaches a name the change adds by attribute (`import calc; calc.TWO`, an `AttributeError`) stops pytest with exit 4
  as an `ImportError` does, but `_conftest_import_failed` accepts only `ImportError` and `ModuleNotFoundError`, so
  the whole check is `not-run`, where a test file failing the same way gives nothing. Loud, not wrong. Raised by the
  cross-review of `passes-at-base` (round 3, 2026-10-24). Closes by telling a conftest.py the change edits from one
  it does not, and taking any failure of the first as the expected outcome.
  Fixed (2026-10-26): `_failing_conftest` reads the path of the `conftest.py` that stopped pytest from pytest's
  "ImportError while loading conftest '<path>'", which pytest 9 prints for any exception, and the files its traceback
  passes through; `_outside_failed_conftest` asks `change.changed`, through the `_BaseCopy` it is given, whether the
  change edits any of them, the `conftest.py` or a helper beside the tests that it imports: the tests under one that
  failed so give nothing, whatever it raised, and one that failed through no file the change edits is a `not-run`
  finding naming it, as the base itself fails there.

- **The fallback collection runs one pytest per changed test file.** When a `conftest.py` cannot be imported at the
  base, `_collect_at_base` collects each changed file on its own, each paying pytest's start-up from the shared
  budget, and a failing root `conftest.py` makes every file repeat the failure. Raised by the same review. Closes by
  reading the failing conftest's path from pytest's output and leaving out only the files under it, in one more run.
  Fixed (2026-10-26): `_collect_at_base` leaves out the changed files under the failing `conftest.py`
  (`_failing_conftest`) and collects the rest in one more run, repeating only while a run names another; a failing
  root `conftest.py` costs one collection.

- **`passes-at-base` reads every changed test as passing in a project installed into the gate's interpreter.**
  `_base_run_findings` runs pytest in the gate's interpreter, so the base's code is whatever the copy's
  configuration puts on `sys.path` (`pythonpath = ["."]` here). An editable install, as a `src/` layout under
  `uv sync` has, resolves the package to the working tree first, so the base run tests the change and every changed
  test is reported. This repository is not installed (no `[build-system]`), so it is unaffected. Closes when the run
  at the base puts the copy's own package directories before site-packages, or names such a project as one it cannot
  judge.
  Fixed (2026-10-26): `_base_run_findings` puts the copy's import roots, its top and its `src/` when it has one, on
  `PYTHONPATH` ahead of what was there, so they come before site-packages and the editable install's `.pth` path or
  import finder. The run at the base loads a pytest plugin, `_BASE_IMPORTS_PLUGIN`, which writes the file each module
  in `sys.modules` came from, each pytest-xdist worker its own; `_imported_from_working_tree` picks those that are the
  working tree's files of the project, whatever imported them, the changed tests, a conftest.py or a helper, and a run
  that imported any judges none of its tests and is one `not-run` finding naming their top-level modules, as for a
  package installed editable from a directory other than those roots. Not closed: an import of the working tree's code
  that leaves no module behind, such as a file a test runs with `runpy` or reads, is not seen. The xdist worker
  reports are read but not run here: pytest-xdist is not in the dev dependencies.

- **A deep decorator can crash flaky-tests and surviving-mutants.** `_changed_python_units`, which both select
  through (`_changed_python_tests`, `_changed_python_functions`), writes each decorator of a changed function with
  `ast.unparse(d)`, which recurses as deep as the expression goes, so a function decorated with a deep expression,
  such as a sum of a few thousand terms, can raise `RecursionError` out of the check, which GateCheck says must not
  raise; an int of more than 4300 digits in one raises `ValueError` the same way. Raised by review round 5 of ticket
  06 (R5-F5), its check named right in round 6 (R6-F3); confirmed at the node only, not end to end; the code is
  surviving-mutants'. `_code` already survives both for findings' messages. Closes with the decorators compared by
  `_body_fingerprint`-like fingerprints rather than their text, or the unparse guarded as `_code` guards it.
  Fixed (2026-10-26): `_python_units` writes each decorator through `_unparsed`, the guard `_code` now shares, which
  gives why instead of raising; `_ChangedFunction.decorators` holds None for a decorator too deep or too long to
  write, which `_unmutatable`, its one reader, counts as a decorator like any other. `TestChangedPythonFiles` runs
  flaky-tests and surviving-mutants end to end on a sum of 5000 terms and on an int of 5058 decimal digits, written
  in hexadecimal since a decimal literal that long does not parse.

- **The cognitive-complexity check reads and parses changed files in a loop of its own.** `_judged_files` and
  `_working_functions` repeat `_changed_python_units`' reading, decoding and unreadable-file handling, because that
  walker yields only top-level functions and methods of top-level classes, the units mutmut names, while the check
  judges every function that no other function holds (`_scored_functions`). The two copies of the except clause
  have to be kept alike, as a coding cookie naming an odd codec has shown twice. Closes with `_changed_python_units`
  taking the walker as a parameter, or handing its callers the parsed trees.
  Fixed (2026-10-26): one walker, `_parsed_changed_files(change, wanted)`, reads and parses each changed `.py` file
  through `_parse_working_file` and yields it as a `_ChangedPythonFile` (path, added lines, removal points, source,
  tree or why not); `wanted(path, added, removed)` is each caller's filter. `_changed_python_units`, `_judged_files`
  and `vague_names` all read through it, so the except clause and its wording live in `_parse_working_file` alone.

- **A changed `.py` symlink is read through to its target by the other Python checks.** `_changed_python_units`
  opens every changed `.py` path, and `open()` follows a symlink, so a change that adds `x.py` as a symlink to a FIFO
  blocks the surviving-mutants and flaky-tests selection, and one to `/dev/zero` reads without end. The
  cognitive-complexity check skips such paths (`_is_link_or_special`, review round 5 of ticket 06, R5-F1). Found by
  reading the code, not reproduced. Closes with `_changed_python_units` skipping what `_is_link_or_special` names.
  Fixed (2026-10-26): `_changed_python_units` reads through `_parsed_changed_files`, which skips whatever
  `_is_link_or_special` names for every caller; `TestChangedPythonFiles` runs flaky-tests and surviving-mutants on a
  change adding `x.py` and `tests/test_x.py` as symlinks to a FIFO and to `/dev/zero`, under a time and memory bound.

- **Three gate checks repeat the make-a-scratch-directory, run, remove-it skeleton.** `flaky_tests`,
  `_mutation_test` and `passes_at_base` each call `tempfile.mkdtemp`, word their own finding when it fails, and word
  another when `shutil.rmtree` fails (`not-removed` twice, `cleanup` once). The cross-review of `passes-at-base`
  (R1-8, 2026-10-24) raised it as "a third copy of the scratch-directory skeleton ... with reworded messages". Closes
  with one helper that takes the check's rule and what it was doing, and the three findings worded one way, with the
  flaky-tests and surviving-mutants tests that quote today's wording.
  Fixed (2026-10-26): all three run through `_in_scratch_directory(rule, purpose, run)`, which words both failures
  one way, "could not make a directory <purpose>" under the check's rule and "could not remove the directory
  <purpose>, `<path>`" as `not-removed`, and removes the directory even when `run` raises.

- **A process a test leaves holding pytest's output keeps a finished run waiting until its timeout.** `_pytest`'s
  `communicate` reads to the end of pytest's output, so a server a test started that inherits it keeps a run that has
  finished going until the timeout, reported then as `budget-reached` (passes-at-base) or `not-rerun` (flaky-tests).
  It was so before `_pytest` ran pytest in its own session. Raised by the cross-review of `passes-at-base` (round 2,
  2026-10-24). Closes when `_pytest` waits for pytest itself, then kills its group and reads what is buffered.
  Fixed (2026-10-26): `_run_in_session` waits for the process, reading its pipes meanwhile, then kills its group
  and reads what is left for at most `TOOL_KILL_WAIT` seconds; `_pytest` runs pytest through it.

- **`_pytest` and `_run_mutmut` copy one stop-and-kill sequence.** Both start their tool in a session of its own,
  kill its process group on a timeout and in `finally`, and read what is left with `_abandon`; the copies had already
  drifted in which errors `killpg` may raise. Raised by the same review. Closes with one helper that runs a command in
  its own session and stops it, which both use, mutmut's SIGINT grace an option of it.
  Fixed (2026-10-26): both run through `_run_in_session`, mutmut with `MUTMUT_STOP_GRACE` as its grace; `_abandon`
  is gone, and one `_kill_group` suppresses the same errors for both.

- **A file name that holds a line separator breaks its list item in the quality file.** `quality_report` writes
  names as they are, so `\r` or U+2028 in one shows as a line break in some renderers. The finding is still
  matched to the right file. Closes with names escaped when the quality file is written.
  Fixed (2026-10-26): `one_line` writes each character that ends a line in some reader (`\n`, `\r`, `\v`, `\f`,
  `\x1c`-`\x1e`, `\x85`, U+2028, U+2029) as `\xNN` or `\uNNNN`. Every list item of a quality file goes through it
  whole, path and message alike: the gate's conditions, findings, failing tests and suppressions, judge_run's
  findings and conditions, `build_failed_report`'s tests and `leak_report`'s secrets; so do the names the gate lists
  in a sentence. `gate_failures` reads back the escaped item, the same in every round.

- **Coverage of a file whose name is not UTF-8 is never measured.** coverage.py 7.14 collects no data for it, so
  `new_coverage` finds none of its added lines in `coverage.xml` and counts them as not coverable: the coverage
  condition can pass a change whose new code there is untested. `docs/quality-gate.md` states it. Closes with a
  coverage.py that records such files, or with the gate naming them as unmeasured.
  Fixed (2026-10-26): the quality file names each such `.py` file the change adds lines to under the coverage
  sentence, as `Not measured`; the coverage condition is judged as before, without its lines.

- **A pytest configuration below the copy's top directory is not used, so its tests are never rerun.**
  `_pytest_inifile` looks only in the top directory of `flaky_tests`' copy, and `_pytest` passes what it finds with
  `-c`, or an empty `pytest.ini`. A project whose pytest configuration is in a subdirectory (`pkg/pyproject.toml`,
  `tests/pytest.ini`) loses that configuration's `pythonpath`, `addopts`, plugins and markers, so every change to
  its tests gets `not-rerun` (typically a collection error) and no test is rerun. Before `-c`, pytest's
  `locate_config` walked up from the changed test files and found it, but it also walked past the copy into
  `TMPDIR` (review F12). Raised by the cross-review of `flaky-tests` (F13, 2026-10-23). Closes when each changed
  test's configuration is found by walking from its directory up to the copy's top and no further, in
  `locate_config`'s order, and the reruns run once per configuration found.
  Fixed (2026-10-26): `_pytest_groups` walks each changed test file's directory up to the copy's top, taking the first
  configuration `_pytest_inifile` finds, and flaky-tests and passes-at-base run once per group (`_PytestGroup`), each
  with its `-c`, `--rootdir` its directory, pytest still started from the copy's top and `--confcutdir` the copy; the
  groups of passes-at-base share its budget, and a group that cannot run is a finding about its tests alone, as are
  the groups a spent budget leaves unrun. surviving-mutants still takes the top directory's (Open issues).
- **`doctor --machine` asks the machine twice for each tool.** Each check is its own `Host.run`, so `_tool_version`
  asks `command -v` before `--version`, which already fails for a missing command, and `_gh_logged_in` before
  `gh auth status`, which may wait up to `NETWORK_TIMEOUT`. Since `ssh_sharing_options` the commands share one SSH
  connection, so each costs a round trip, no longer a handshake. Raised by the cross-review of the doctor command
  (F8, 2026-10-24): "redundant probes, each one SSH connection with `--machine`". Closes with each tool asked once
  (`.scratch/doctor-ssh/issues/02-drop-redundant-probes.md`).
  Fixed (2026-10-26): `_tool_version` and `_gh_logged_in` run the tool once through `_run_tool`, which gives the
  same "<command> is not on PATH" for a command that cannot start locally or that the remote shell answers with
  127; `_on_path` stays for the agent CLIs, whose checks only ask whether they exist. With `ssh_sharing_options`,
  20 commands to a user-level sshd on this host took 0.24 s against 1.78 s unshared; `doctor --machine` on a real
  machine was not timed, as none is saved in herdr here.

- **Cognitive complexity scores direct recursion only.** Campbell's paper gives a point to each function in a
  recursion cycle; `_calls_itself` sees a function calling itself by name, or a method as `self.m`, `cls.m` or
  `C.m`, but not a cycle through other functions, nor a call through an alias. A function that recurses through a
  helper scores a point less than the paper says. Closes with a call graph of the module, scored per cycle.
  Fixed (2026-10-25): `_recursive_functions` builds each module's call graph from `_callees`, the name resolution
  `_calls_itself` now asks too, which resolves a call as Python's scopes do (a parameter, local or nested function
  of the caller's name is not the module's), and finds its strongly connected components by Kosaraju's algorithm;
  each function on one, or calling itself, scores the point once, which `cognitive_complexity` is told as
  `recursive`. A call through an alias or into another module, and a cycle through a nested function, stay
  unfollowed, as the comment above `COGNITIVE_COMPLEXITY_LIMIT` says; a move that brings two functions calling
  each other into one module raises both by the point (`docs/quality-gate.md`). No function of `orchestrator.py`
  changed its score.

- **A function moved out of a Python script the change deletes is judged as new.** The cognitive-complexity check
  reads the base of a changed file whose old name has no `.py` only when the file is still Python now
  (`_base_scores`: `new in working`), so the functions of a deleted `bin/tool` lend no score, and one moved unchanged
  from it into `lib.py` is reported as new. Closes with the base read whenever its first line is a `#!` naming
  python, as `_is_python_source` asks of the working tree.
  Fixed (2026-10-25): `_base_scores` reads an old path's base when `_was_python_source` says the base's blob is
  Python, by `_holds_python`, the one test of a path and its first line that `_is_python_source` uses for the
  working tree too.

- **The macOS builds are first tried on a published release.** `.github/workflows/macos-binary.yml` runs only on
  `release: published` or by hand for an existing tag, so a change that breaks the macOS binary or
  `packaging/build-desktop.sh` on macOS merges green, and the release then lacks those assets until it is fixed and
  the workflow is run again for the tag. Found adding the desktop app to releases (2026-10-24), whose `.dmg` had
  never been built before the first release with it. Closes with a macOS build of pull requests that touch
  `packaging/` or `ui/`, or a decision that a rerun after the release is enough.
  Fixed (2026-10-26): `.github/workflows/macos-binary.yml` builds every pull request that touches `packaging/`,
  `ui/`, `pyproject.toml`, `uv.lock`, `.python-version`, `orchestrator.py` or the workflow, in a `build` job with
  `contents: read`; a separate `upload` job, the only one with `contents: write`, adds the build's artifact to a
  release or a dispatch's tag. PR #89's own run built both on `macos-latest`, its upload skipped
  (https://github.com/DavidSiegl/octoflow-ai/actions/runs/40283655516). The release path is first run by the next
  release.

- **Two builds on agentbox0 can still run rustc out of memory.** All builds on the node share its Jenkins agent
  container's 3 GB (`mem_limit: 3g`, not the VM's 8 GiB), and a push to a new branch starts a branch build before
  its pull request exists, so two builds of one commit compiled the Tauri crates at once (PR-74's first build:
  rustc SIGKILLed on `gtk`). Building the UI stage without debug info lowered one build's peak only. Raised by the
  cross-review of ci-no-debuginfo (2026-10-25): "the OOM cause is narrowed and left unrecorded".
  Fixed (2026-10-25): the UI stage's `cargo test` and the Package stage's `build-desktop.sh` run under
  `ci/cargo-lock.sh`, a `flock` on a file outside every workspace that every build of this repository on the node
  takes, and say so when they wait. A quality build never waits: its rounds already take up to 1174 s of
  `QUALITY_TIMEOUT`'s 1200 s, and a Package stage holds the lock for about 300 s, so it takes the lock only when it
  is free and otherwise compiles beside the other build, saying so. Chosen over serialising whole builds: Jenkins
  has neither Lockable Resources nor Throttle Concurrent Builds, and one executor would queue the Python-only
  stages too, against `QUALITY_TIMEOUT`. What else shares the 3 GB stays open under "Is 3 GB the right memory
  limit".

- **A prompt sent right after a startup dialog can be lost.** When an agent starts behind a dialog, such as Claude
  Code's folder-trust question, `Workflow._start` waits in `_wait_for_startup` for herdr to report it `idle` and
  returns; `Workflow._written` then sends the turn's prompt and records it in `RunState.prompted`. On 2026-10-25
  the Builder of run 20261025-090825-73faf7, in a worktree of a clone Claude Code had not seen, was reported idle
  as the dialog closed and before its input took text: the prompt never arrived, yet counted as delivered, and the
  turn timed out after 1800s at an empty prompt. `resume` cannot recover it, since `_prompt_for` only waits on an
  alive agent already prompted. Closes with `_wait_for_startup` waiting until the agent takes input, or with the
  prompt's delivery checked (the agent turning `working`, or its handoff file appearing) and the prompt sent again
  when neither happens.
  Fixed (2026-10-25): `Workflow._written` sends the prompt through `_deliver`, which counts it taken once herdr
  reports the agent `working` or `blocked`, or its handoff file exists, within `PROMPT_DELIVERY_SECONDS` (30), and
  otherwise sends the same prompt once more. A second miss fails the turn naming the role, the file and the pane.
  `prompted` and the turn's record are saved only once the prompt is taken, so a `resume` of such a run prompts the
  agent again instead of only waiting. `_wait_for_startup` is unchanged.
- **drain takes two passes to mark a spec done whose run's pull request already merged.** A `ready-for-agent` spec
  with a `Run:` line gets `in-review` and `PR:` from `finished_run_header`, but `refreshed_headers` asks gh about
  the pull request only for a spec that was `in-review` when the pass began, so a pull request merged since leaves
  the spec `in-review`, its dependants blocked, until the next drain. Met on 2026-10-25 after resuming a run
  outside drain. Closes with the pull request checked in the same pass once `finished_run_header` gives one.
  Fixed (2026-10-25): when `finished_run_header` makes a spec `in-review`, `refreshed_headers` asks
  `pull_request_header` about its pull request in the same pass and merges the result into the spec's one pair, so
  a merged one makes the spec `done` and its dependants startable in that drain, `--dry-run` included.
  Since 2026-10-26 `settled_header` holds those steps, in order, for every spec, so a spec gets one pair a pass, and
  gh is asked once a pass about each pull request (`_PullRequestStates`): an `in-review` or `ready-for-human` spec
  whose conflict refresh finds its pull request merged is `done` from that refresh's answer.
- **`test-integrity` has no way to accept an expected value the spec changes.** `expected-value-changed` fails the
  gate whenever an assertion's literals change beside code, also when the spec asks for the new output, and unlike
  `removed-test` there is no report the Reviewer judges instead. PR #76 changed what the pull request says for a
  gate turned off after its analysis; its edited assertions failed the PR build, and the fix was to drop them where
  other tests already pinned the new text. One check went with them: the cross-review of that fix found that "this
  path no longer checks the analysis text in the body", i.e. that
  `test_off_in_quality_drops_the_analysis_and_goes_to_the_reviewer` no longer pins that the details block shows
  quality-1-1.md's text; an `assertIn` of that text there pairs with the dropped one and is the same finding.
  Closes with a way for the Builder to name the changed expectations in its report, judged by the Reviewer, or
  with such a finding demoted to one to weigh; then that assertion goes back.
  Fixed (2026-10-25): a failing `GateCheck` names the rules it weighs instead (`GateCheck.weighed`), and the gate
  sets each finding's severity to what it does with it (`GateCheck.judged`). `test-integrity` weighs
  `removed-test`, `fewer-assertions` and `expected-value-changed`, what a spec can ask for and the gate cannot read
  the spec to tell; skips and swallowed errors, which only hide a failure, still fail. The details-block assertion
  is back. PR #82 and PR #83 failed their builds on such findings, a test the spec retired and one it reshaped.
- **The orchestrator the AppImage started ran in the bundle's environment and directory.** The AppImage's start-up
  (linuxdeploy's `AppRun`, its GTK hook, AppImageKit's `AppRun.wrapped`) prefixes `LD_LIBRARY_PATH`, `PATH`,
  `PYTHONPATH` and other path lists with `$APPDIR` entries, sets `PYTHONHOME` and GTK's variables into the bundle,
  and starts the app in `$APPDIR/usr`; the window's `serve` sidecar inherited all of it, and passes its environment
  to every process it starts (`child_env`), so git, gh and ssh could load the bundle's libraries. Found while
  grounding `.scratch/desktop-cli/` (2026-10-24), not seen failing. Fixed (2026-10-25): `UserShell` in
  `ui/src-tauri/src/lib.rs` recovers the user's shell's environment and directory (`OWD`) for the orchestrator's
  process, the sidecar and the CLI alike, and keeps the app's own for GTK; `packaging/build-desktop.sh`
  smoke-tests it on the built AppImage.
- **A pull request opened over a conflict stays a draft with its warning after the conflict is resolved.** When
  `Host.merge_upstream` finds conflicts with the base branch, `pr_body` opens the body with a `[!WARNING]` naming
  the conflicting files and `create_pr` opens it as a draft; nothing ever edits the body or marks it ready, and
  `RunState.conflicts` keeps the files. So once someone merges the base in and pushes, the pull request still says
  it conflicts and stays a draft until a human edits it by hand. Met on PR #61 (2026-10-24): resolved and pushed,
  it still showed the warning and the draft. Closes when the orchestrator, or a command of its, notices that the
  head merges cleanly and drops the warning and the conflict-only draft.
  Fixed (2026-10-25): `orchestrator.py refresh [RUN]` asks gh (`Host.pull_request_merge`) about each done run's pull
  request with conflicts, or the one named. One that is open and `MERGEABLE` loses the `conflict_warning` paragraph
  `pr_body` wrote (`without_conflict_warning`; the rest of the body, human edits included, stays, and a body
  without it is not edited), is marked ready (`gh pr ready`) if it is a draft and the verdict is in `SUCCEEDED`, and
  its run's `RunState.conflicts` is emptied; one merged or closed only has the conflicts emptied; one `CONFLICTING`
  or `UNKNOWN` is left, and refresh exits `EXIT_WAITING`. drain refreshes the runs of `in-review` and
  `ready-for-human` specs in `refreshed_headers`, and moves a `ready-for-human` spec whose succeeded run it cleared
  to `in-review`. `show` and `finish` read `RunState.conflicts`, so a cleared run is no longer called a draft.
- **drain models a spec deleted while it runs only in its status.** A spec that cannot be read again is
  `QueuedSpec.unreadable`, which makes `status` None, but `run`, `pr`, `change_line` and the header writes do not
  know it: the running spec deleted mid-run stops the drain at its post-run `update`; a deleted `ready-for-agent`
  spec drops out of `drain_end`'s `left`, so the drain can end "all specs cleared"; a deleted blocker shows as `no
  status` with its old PR, its reason never printed; `change_line` prints `-> None`; and `not running it` is logged
  for `done` specs too. No test makes the failed-run branch's header write fail. The spec does not cover specs
  deleted during a drain. Raised by the cross-review of drain (round 3, 2026-10-25): "'gone' is modelled only in
  `status`". Closes with a gone state that drain_end's summary and exit status, the change lines and the writes all
  honour, or with drain refusing to go on once a spec it read has gone.
  Fixed (2026-10-25): `QueuedSpec.gone` holds why a spec drain could not read again, set and cleared by `reread`,
  and `status`, `run` and `pr` are None while it is set; `status_as_read` keeps the status as drain last read it. A
  header write that finds the spec gone logs what it does not record and drain goes on, a gone spec before its run
  is not run, and the failed-run branch no longer says the spec is `ready-for-human`. `drain_end` counts a spec
  last read `ready-for-agent` and now gone as left (`<slug>: gone (<why>)`, `EXIT_WAITING`), a gone blocker shows
  as `gone (<why>)` without its pull request, `change_line` ends `-> gone`, and `not running it` is logged only for
  a spec last read `ready-for-agent`, another once as `<slug>: <why>; gone`. A test makes the failed-run branch's
  write find the spec gone.
- **A pull request whose gate was turned off shows the last analysis as the change's.** After `resume
  --no-quality-gate`, `Workflow._publish` passed `_last_quality`'s file to `pr_body` and `provenance`, so the quality
  section and the `Orchestrator-Gate` trailer judged code the Builder had since changed; only the Reviewer was told
  (`QUALITY_OFF_NOTE`). Raised by the cross-review of resume-quality-gate (finding 8, 2026-10-24): "nothing in the PR
  says the final change went unjudged". Fixed (2026-10-25): `Workflow._unjudged` decides, for `_quality_note` and
  `_publish` alike, when no quality file judges the published code; `Workflow._gate_evidence` then cites the run's
  last quality file from any round, the trailer reads `..., then turned off`, the section `Quality gate (round R,
  before the last change)`, and the body opens with a `[!NOTE]` saying nothing judged the final change, or that the
  gate was turned off before any analysis finished.
- **Two functions decide which quality file is a round's last.** `Workflow._gate_passed` read
  `quality_path(n, quality_round)` itself, while `_quality_note` and `_publish` used `_last_quality`; they agreed only
  because `_gate_passed` returns True whenever the run has no CI. Raised by the same review (finding 11). Fixed
  (2026-10-25): `_gate_passed` reads `_last_quality`, and fails a quality file older than the current quality round
  as it failed a missing one.

- **A proof that failed is not run again in the next round.** `Workflow._judge_proofs` ran only the proofs the
  verdict step wrote in its own round, so a Builder told to make round N's failing proofs pass (`PROOFS_NOTE`) was
  checked only if the Reviewer wrote them again; one that still failed could be approved. Raised by the code review
  of loop-guards (2026-10-24). Fixed (2026-10-24): `Workflow._rerun_proofs` runs every proof whose last run failed,
  or whose rerun errored, again right before each later round's verdict turn, until it passes, recording each rerun in
  `RunState.proofs` with the round that wrote the proof; `CARRIED_PROOFS_NOTE` tells the verdict step which still
  fail or now error, and `PROOFS_NOTE` names all of them to the step the review loops back to.
- **The frontend's tests failed on Node 25 and later.** Node's own `localStorage` global, undefined without
  `--localstorage-file`, hid jsdom's, since Vitest's jsdom environment copies no global over one Node already has,
  so `ui/src/test/setup.ts`'s `localStorage.clear()` threw and every test failed. `setup.ts` now puts jsdom's
  storage on the global itself; the tests pass on Node 20.20 and 26.12 alike (2026-10-24).
