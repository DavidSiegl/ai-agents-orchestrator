# TODO

What is known to be open in `orchestrator.py` and `gate.py`, and why. An entry leaves `Open issues` only for
`Fixed`, with what was done.

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
- **The quality gate in a `gate.py` of its own: what is left.** The cut is made (Fixed, "The quality gate lives inside
  `orchestrator.py`"), and the Jenkinsfile's cutover fallback is gone. Left: making the test-file patterns a setting,
  for the gate's use in other projects (`.scratch/gate-split/issues/04-configurable-test-files.md`). The gate moves to
  its own repository only once a second project uses it. Decided 2026-10-24.
- **The Python engine is frozen.** From the merge of the parity harness (`tests/parity/`) until the Rust engine
  replaces it in one release, nothing lands in `orchestrator.py`, fixes included: its recordings are the oracle
  each Rust ticket is judged by, and a change to the Python engine would move it. A bug found meanwhile goes into
  `Open issues` here and is fixed on the `rust-engine` branch. `gate.py`, the docs, the tests and CI are not
  frozen. The note at the top of `orchestrator.py` says so too. Decided 2026-10-24 (the Rust engine's spec,
  Decisions).
- **The Rust engine's parity harness: what it does not record yet.** `tests/parity/` runs 43 scenarios against
  `orchestrator.py` as a program and compares each with its recording (docs/architecture.md, Tests); the Rust tickets
  replay them through `PARITY_ENGINE`. Not covered: a secret gitleaks finds, at a quality round or at publish; the
  harnesses other than claude and codex starting an agent (only their arguments differ); a session log's token counts
  (every turn records `unknown`); `--timeout` and the stall notice, which take real minutes; the GUI, which the Rust
  engine drops; round 2 of the example workflows and a fresh session's prompts as sent, whose templates only the
  recorded `state.json` holds and whose text only golden files hold (`TestWorkflowPromptsGolden`, docs/architecture.md,
  Tests), for the Python engine; and texts the engine sends or writes that neither a scenario nor a golden file reaches:
  `CONTINUE_PROMPT`, `QUALITY_OFF_NOTE`, the pull request body of a run that ends `FINISHED` and of one whose gate was
  turned off after its last analysis, and a body section cut at `PR_SECTION_LIMIT`. Each closes with a scenario recorded
  from the Python engine (found by orchestrator-test-methods/06 and its review 74, 2026-10-27, against the recordings).
  Where gitleaks is not on PATH, or openssl cannot make the fake PyPI's certificate, every scenario fails naming it: a
  skip would pass a machine that checked nothing, and the gate's test-integrity check fails an added skip (the user's
  ruling, 2026-10-27); Jenkins's Install stage downloads the pinned gitleaks (`ci/install-tools.sh`), and mutmut's
  runs leave `tests/parity` out (`[tool.mutmut]`), which would take most of the mutation budget. What a record holds by
  design, each a choice a Rust ticket may revisit: output a human reads (logs, errors, `show`, `list`, `prune`) as its
  facts (`normalize.facts`), not its words; a run's files listed by modification time, as `/files` and the event stream
  list them, sorted by name, since seconds of the clock decide that order; ssh's commands not at all, only what they
  did; doctor's lines without git's and Tk's, which are the host's. Two more are open issues below: the `state.json`
  saves between two commands, and serve's events between two checkpoints. The suite takes about 3 minutes more with the
  harness, one engine run each scenario at 2 to 25 seconds (25 for serve's event stream); when a quality build runs them
  is the Jenkinsfile's Test stage's to say. Decided against (2026-10-27): scenarios as data files the harness
  interprets, which needed a language of their own for kills, takeovers and serve's stream; a cassette of the expected
  requests that the fakes answer in order, which breaks on every count of polls; and a stub gitleaks where none is
  installed, which would record what no engine run with gitleaks does. Decided against too (2026-10-27,
  orchestrator-test-methods/06): golden files of the engine's follow-up prompts, notes, commit messages and pull request
  bodies, which the recordings hold as sent where a scenario reaches them, and which would test only the frozen Python
  engine, not the Rust one.

## Open issues

- **A quality build now takes longer than `QUALITY_TIMEOUT`.** The contract test's one live run (live-checks/01,
  2026-10-27) triggered build #59 of `Octoflow-AI/py-octoflow-ai-quality` with `GIT_REF=main` (`f4628cc`): it ended
  SUCCESS after 1299 s, of which Test took 888 s, UI 229 s and Gate 172 s, so `tests/contract/test_jenkins.py` failed
  at what was then its 1200 s cap (it now waits for the build to end, checks every answer, and fails last on the time
  the quality file took), as a quality round of the engine would: `_poll` gives up at `QUALITY_TIMEOUT` (1200 s,
  `orchestrator.py`) from the round's start, and Gate had not begun by then. The suite grew by the parity scenarios
  (`tests/parity`), and PR-160's Test stage took 645 s. A resume gets a fresh timeout and adopts the running build,
  so a run is not lost, but a quality round of this repository whose build takes as long fails once, and is
  judged only after a resume. Not changed here:
  the engine is frozen (Planned, "The Python engine is frozen"), so a larger `QUALITY_TIMEOUT` goes to the Rust
  engine. Narrowed by PR #162 (2026-10-27) for a quality round whose change leaves what the parity scenarios depend on
  alone (Jenkinsfile, Test stage): its build leaves them out. A change that bumps the version touches `pyproject.toml`
  and `uv.lock`, so its build still runs them, and so does the contract test's build of main whenever main's last
  commit was a releasing merge (build #59, before #162, ran them in any case); Gate's mutmut time is
  gate-open-decisions ticket 06's. Closes when
  a quality build that runs the scenarios fits in `QUALITY_TIMEOUT`, shown by the contract test green against one, or
  with a larger `QUALITY_TIMEOUT` in the Rust engine.
- **The Rust engine reads some input no run writes otherwise than the Python engine.** Each is a difference only for a
  file a human edited, or a case no test of the Python engine pins (rust-engine ticket 04, 2026-10-28): `state.json`
  with `NaN`, `Infinity` or an integer beyond 64 bits is refused, as serde_json reads it (`engine/src/py/json.rs`),
  where Python reads it; a time is read as CPython 3.14's `fromisoformat` reads it (`crate::py::datetime`, since
  ticket 06; one without an offset in the machine's zone since ticket 08's follow-ups, Fixed, "The Rust engine named a
  new run by the time in UTC"; 3.13, which a system `python3` may be and `packaging/build-binary.sh` builds the Linux binary with, refuses hour 24,
  which 3.14 reads as the next day's midnight, and reads `T12.5`, `T12:00.5` and `T12:00:00.Z`, which 3.14 refuses
  (cross-review 92, F4); no harness or run writes either), and a turn whose start is not a time is left out of `summary.md` and `metrics.json`, where the Python engine
  fails (`epoch_seconds` and `turn_starts` in `engine/src/summary.rs`); a `state.json` field of another type than the
  Python engine writes is refused naming the run, where Python keeps it, and one it does not know is kept, where Python
  drops it, as the spec asks (`RunState::from_saved`). And from the machine the agents run on (rust-engine ticket 05,
  2026-10-28): a path git prints that is not UTF-8 is refused, naming it, by `Host::lock`, `locked_changes`,
  `restore`, `changed_line_counts` and `crate::patch::added_lines` (so by the secret scan too), where Python reads it
  as `os.fsdecode` does, and a base's gitleaks rules file that is not UTF-8 fails the scan closed, where the Python
  engine crashes writing it (`engine/src/host.rs`, `utf8_path`; `engine/src/host/scan.rs`, `base_rules_file`).
  And from the agents' session logs and herdr (rust-engine ticket 06, 2026-10-28): a session log's line that holds
  `NaN`, `Infinity` or an integer beyond 64 bits is skipped, as `Json::parse` refuses it, where Python counts its
  tokens, and a lone surrogate in a line, as an escape or as a byte that is not UTF-8, is read as U+FFFD, so that two
  Claude Code message ids that differ only there count as one answer, where Python counts two
  (`Harness::session_usage` in `engine/src/harness.rs`, `Json::parse_lossy`); a harness that is not one of
  `AGENT_KINDS`, as a hand-edited `state.json` can name, is refused naming those there are (`crate::harness::harness`),
  where the Python engine fails on a KeyError; and an answer of herdr's that lacks what its command gives, or is not
  UTF-8, is a `bad_response` naming the command or a failure naming it (`engine/src/herdr.rs`), where Python fails on
  a KeyError or a UnicodeDecodeError. And from the Workflow's locks and proofs (rust-engine ticket 08, part 4,
  2026-10-29): a `restored` or `proofs` record of `state.json` without a field the engines write, or with one of
  another type, stops the run naming it (`Restored::read` in `engine/src/workflow/locks.rs`, `ProofRun::read` in
  `engine/src/proofs.rs`), where Python fails on a KeyError or a TypeError, or, for a `restored` record without
  `paths`, restores nothing, or, for a `proofs` record without `said`, reads it only where it needs `said` (no release
  wrote one: v0.24.0, the first with proofs, writes `said` in every record, and only commits before it on main did
  not; cross-review 98, F1); and a proof's output that is not UTF-8 is quoted with each such byte as U+FFFD
  (`Workflow::run_proof`), where Python keeps it as a lone surrogate, which `state.json` holds as an escape and the
  write of the review then fails on (a UnicodeEncodeError, uncaught).
  And from drain (rust-engine ticket 09b, 2026-10-29): a name in drain's DIR that is not UTF-8 is left out of the
  queue (`crate::cli::drain::queue`, `read_queue`), where Python reads its spec.md through os.fsdecode; and doctor
  reads a tool's version number in ASCII digits only (`crate::cli::doctor`, `version_number`), where Python's `\d`
  takes any Unicode digit (cross-review 104, F6).
  Decided against unless a run is found that writes such input.
- **The Rust engine's Ctrl-C, what is left.** Rust-engine ticket 08's part 6 (2026-10-29) has a SIGINT raise the
  `Interrupt`, which stops the command the engine waits on (Fixed, "A Ctrl-C at the terminal does not reach the
  command the Rust engine's `Host` or herdr is running", which since ticket 10 also says how every command of the
  binary takes it). Not closed: (1) A
  Ctrl-C during a Jenkins or PyPI request (`crate::http`, ureq) waits for the request to end, up to its timeout, where
  Python's KeyboardInterrupt stops `urlopen` at once: there is no process to stop, and ureq has no way to abandon a
  request from another thread. The user accepted that wait for a Ctrl-C (2026-10-29, review 100). Since ticket 08's
  follow-ups a SIGTERM or a SIGHUP waits so too (cross-review 102, F4: "a `kill <pid>` or a service manager's stop
  during the quality gate's Jenkins polling or the dependency check's PyPI request waits up to the request
  timeout"), where it ended the Python engine at once; a service manager that sends SIGKILL after a shorter grace
  kills the engine with the run unreleased. Closes with a request the Interrupt can cut short (one whose reads are
  given slices of the timeout, the Interrupt checked between them, or one run on a thread the Workflow stops waiting
  for), or with the user's ruling that the wait is acceptable for SIGTERM and SIGHUP too. (2) Under macOS, `by_signals`
  reads no `/proc/self/status` and takes the kernel to discard a signal that was ignored when the engine started, even
  while blocked, as the BSD kernels do, so `serve`, which waits for SIGINT and SIGTERM also when it started with one
  ignored, as Python's `signal.signal` replaces an ignored disposition (ticket 10, `crate::serve::serve_command`),
  does not stop at one it started ignoring there, and `run_process` gives
  a command the whole 250 ms grace, nix having no `waitid` there; unchecked on a Mac. Closes with
  `engine/tests/signals.rs`, which is Linux's (it reads `/proc` and starts the engine with GNU `env`), ported to macOS
  and run on one. A process that left the command's group, as a daemon does, is not reached, as at
  a timeout; that is no gap, as the terminal's Ctrl-C does not reach one in Python either. (3) On a Linux whose
  `/proc/self/status` cannot be read, as in a container or sandbox without `/proc`, `ignored_signals` finds nothing
  ignored, so `by_signals` waits for a signal the engine started with ignored and takes it, Linux queueing a blocked
  signal whatever its disposition (cross-review 102, F2: "an engine under `nohup` in a container or sandbox without
  /proc is hung up: the Interrupt is raised, the run released as interrupted", and `interrupt::exit`'s raise of the
  still-ignored signal returns, so the program exits 129 by `std::process::exit`). Part 6 had the gap for SIGINT;
  the follow-ups extend it to SIGTERM and SIGHUP. nix reads a signal's disposition only through `sigaction`, which
  is `unsafe`. Closes with that query, the user's choice of a second `unsafe` block, or with refusing to start on a
  Linux without `/proc`.
- **The Rust engine's fromisoformat words some errors as CPython 3.13 does.** `IsoTime::parse`
  (`engine/src/py/datetime.rs`, rust-engine ticket 06) says its errors are 3.14's, but gives 3.13's wording for a
  year, month or day out of range and an offset of a day or more: `year 0 is out of range` where 3.14 says `year must
  be in 1..9999, not 0`, `month must be in 1..12` (3.14 adds `, not 13`), `day is out of range for month` (3.14:
  `day 29 must be in range 1..28 for month 2 in year 2026`), and the offset's message without 3.14's `, not
  datetime.timedelta(...)`; 28 of the 3106 `fromisoformat` cases of `engine/tests/oracle/harness.json` differ so.
  The oracle test compares only whether such a time is refused (found by cross-review 102's F3, 2026-10-29, when it
  began comparing `timestamp()`'s own refusals by their message). Only the wording differs, and only for times no harness,
  run or PyPI writes. Closes with the messages worded as 3.14's and the test comparing every message.
- **The commands the Rust engine runs outlive an engine that is SIGKILLed.** Each command runs in a process group of
  its own (rust-engine tickets 05, 06), so a SIGKILL of the engine, or of its group, ends the engine and leaves a
  running `herdr agent wait`, `gh pr create` or push to finish unwatched, where the Python engine's children are in
  its group and end with it when the group is killed. SIGINT, SIGTERM and SIGHUP now stop them (Fixed, "The commands
  the Rust engine runs outlived an engine that got a SIGTERM or a SIGHUP"); a SIGKILL cannot be caught. The parity
  harness's kill kills each process below the engine too (`Invocation.kill`), as "a crash or a lost machine" does;
  before, `kill_in_publish` against the Rust binary got a pull request from the orphaned `gh`. Closes with the user's
  ruling that this is acceptable, or with the command's group killed when the engine dies (Linux's
  `PR_SET_PDEATHSIG`, set in `run_process`'s `pre_exec`, reaches only the command itself, not what it starts).
- **The Rust engine talks to Jenkins and PyPI otherwise than urllib in a few cases.** Its transport is ureq
  (`HttpClient`, `engine/src/http.rs`, rust-engine ticket 07, 2026-10-28), which differs from `urlopen` where no test of
  the Python engine pins it: ureq reaches an http URL through a proxy with CONNECT, as it does an https one, where
  urllib sends the request itself to the proxy, so a proxy that allows CONNECT only to port 443, as squid does by
  default, refuses a plain-http Jenkins that `http_proxy` sends through it; TLS trusts the certificates of the places
  the environment names, else of the system's as openssl-probe finds them, each read by rustls-native-certs
  (`root_certs`), which trusts otherwise than OpenSSL does for Python in five ways (the user's rulings, 2026-10-28,
  cross-review 93, F22 and F23): with `SSL_CERT_FILE` set non-empty and `SSL_CERT_DIR` unset, or the reverse, it reads
  only what is set, where OpenSSL adds the system's default directory or file (with the other set empty, OpenSSL adds
  nothing either), so an `SSL_CERT_FILE` that cannot be read, such as a removed virtualenv's, trusts none where Python
  still trusts `/etc/ssl/certs`; a variable set empty counts as unset, which departs from OpenSSL only where the other
  is unset or empty too: with `SSL_CERT_FILE` empty and `SSL_CERT_DIR` unset the system's bundle is trusted too, where
  OpenSSL reads no file for it, with `SSL_CERT_DIR` empty and `SSL_CERT_FILE` unset the system's directories are, where
  OpenSSL reads no directory for it, and with both empty the whole system store is, or Mozilla's roots on a machine with
  no certificate, where Python loads no certificate at all; it reads every file of a certificate directory, where
  OpenSSL opens only the files named by a subject's hash, from `.0` up to the first missing or broken one, so a
  certificate under another name, or behind a broken link, is trusted here only; without either variable it takes the
  system's bundle and directories from openssl-probe's list of places, not from OpenSSL's compiled-in ones, also on
  macOS, where neither engine reads the keychain; and without either variable, on a machine with no certificate at all,
  it trusts Mozilla's roots (webpki-roots), where Python trusts none and fails every https request; and the timeout
  bounds the connect and each read and write, as urllib's does, but not name resolution. In what it reads
  (`engine/src/ci.rs`, `engine/src/pypi.rs`, `engine/src/py/toml.rs`): an answer of Jenkins's of another shape than a
  queue item, a build's result, a list of builds or a test report is a `CIError` that may pass, where the Python engine
  fails with an `AttributeError`, and a queue item's `executable.number` that is not an integer is one too, where Python
  keeps it; a manifest is read as TOML 1.1, which tomllib before Python 3.15 refuses in part, and why one is not TOML is
  the toml crate's words with tomllib's `(at line L, column C)`; and rustls refuses a server certificate that is a
  CA's own, as a self-signed one made by `openssl req -x509` with its default extensions is (`CaUsedAsEndEntity`),
  where OpenSSL accepts it when it is trusted, so the parity harness's PyPI has a leaf under a CA of its own (found by
  ticket 08's part 7, 2026-10-29). Closes, for the proxy, with a transport that sends an
  http request through a proxy as urllib does, or with `no_proxy` naming Jenkins; the rest decided against unless a
  setup is found that needs it. Decided against (2026-10-28, ticket 07, design-it-twice): `CI` and `PyPI` as traits of
  their own with in-memory fakes. The engine's tests would then word every URL and error twice, in the code and in the
  fake, and ticket 08's Workflow tests, which read the requests Jenkins got (`FakeCI.names`, `requests`, `auth`), would
  port only by re-deriving them; with the seam at `Http`, as Python's at `urlopen`, `FakeJenkins` and `FakePyPI` play
  the servers and every line of `Jenkins` and `PyPI` runs in those tests. The user kept the `Http` seam and amended the
  spec's Lane to say so (2026-10-28, cross-review 93, F1), and issue 08 says which seam it is given.
- **The contract test leaves its Jenkins build running when it fails.** `tests/contract/test_jenkins.py` triggers one
  quality build and nothing stops it on a failure or a Ctrl-C, so the build holds an executor until it ends, up to
  the 22 minutes build #59 took. Raised by cross-review 73 (F11, 2026-10-27): "a Ctrl-C during the queue wait leaves
  the full ~22-minute build running." The orchestrator does not abort a build either, so `CI` has no call for it.
  Closes with a POST to `<job>/<number>/stop` (or the queue item's cancel) in the test's cleanup, once the `agents`
  user is known to hold Jenkins's Cancel permission on the job.
- **A Rust test's own lines count toward the coverage of new code.** cargo-llvm-cov's report lists the lines of a
  `#[cfg(test)]` module as it lists the code's (docs/quality-gate.md, "Rust tests and coverage"), and a passing
  test runs them all, so a change that adds a Rust function and a test of it is judged on both: 15 untested lines
  beside a 10-line test read as 40%, not 0%. coverage.py's report of the Python has only `orchestrator` and `gate`.
  Found while adding Rust coverage (rust-engine ticket 03, 2026-10-27). Closes with test modules left out of the
  report, by `#[coverage(off)]` once it is stable or by the tests moving to files that `--ignore-filename-regex`
  can leave out, or with `gate.py` skipping the lines a `#[cfg(test)]` item spans. The Rust engine's tests are in
  such files already (`engine/src/**/tests.rs`, `engine/src/prompt_parity.rs`, `engine/src/test_support.rs`); the
  desktop shell's are a `#[cfg(test)]` module in `ui/src-tauri/src/lib.rs`.
- **No build runs the Rust doctests.** cargo-nextest does not run doctests, and since the Test stage's `cargo llvm-cov
  nextest` replaced the UI stage's `cargo test`, nothing does: `octoflow_ui_lib` is an `rlib` among its crate types
  (`ui/src-tauri/Cargo.toml`), so `cargo test` ran them. The crate has no doc example today. Raised by cross-review 72
  (F3, 2026-10-27): "a doc example added later that fails to compile or asserts wrongly passes CI unnoticed".
  Decided against for now: a `cargo test --doc` beside it, which builds the dependencies a third time, uninstrumented,
  under the agent's 3 GB, for no example. Closes with `cargo test --doc --locked` in the Test stage once a doc example
  lands, or with cargo-llvm-cov's `--doctests` once it runs on a stable toolchain.
- **The parity harness records `state.json` at each command the fakes get, not after each save.** Ticket
  rust-engine/01 asks for "`state.json` after each save"; the ledger reads it only when herdr, gh, Jenkins or PyPI
  gets a call, and leaves out a change to `heartbeat_at` alone (`tests/parity/fakes.py`, `_changed_states`). Raised
  by cross-review 65 (F2, 2026-10-27): "a Rust engine that skips or reorders saves between two of those calls, or
  writes a wrong intermediate state that a later save overwrites, still passes." Only the engine's own process sees
  every save: for the Python engine an audit hook through `sitecustomize`, or a `mv` on PATH that `Host.write` calls,
  would catch them, and the Rust engine could be held to neither. Closes with the user's ruling on the ticket's
  wording, or a way to see each write of `state.json` from outside the process (inotify has no stdlib binding).
- **serve answers some requests otherwise than Python's http.server.** The Rust engine's serve is on tiny_http
  (rust-engine ticket 10, `engine/src/serve.rs`), which reads a request otherwise in a few cases no client of serve's
  sends: a request line or header with a byte beyond ASCII gets no answer, its connection closed, where Python reads
  it as Latin-1 and answers, a token so 401 (`non_ascii_tokens`); a header's value loses its trailing blanks, so a
  `Host` or `Authorization` header with one passes where Python's fails it; a header continued on a folded line is
  refused with 400, where Python joins the lines; a netloc with one square bracket in an absolute request target is
  read as it is (`crate::py::url::urlsplit`), where Python's handler fails on urlsplit's ValueError and answers
  nothing; tiny_http answers a bad request line, an `Expect` other than `100-continue` and HTTP/2 itself (400, 417,
  505), and neither those nor the 501 to another method are logged on stderr, where http.server logs each error it
  sends; and a reply names tiny_http in its `Server` header. An event stream to an HTTP/1.1 client is chunked, ending
  with its last chunk, where Python's HTTP/1.0 stream ends with the connection: tiny_http keeps the connection, so a
  close-delimited stream would not end for a client at a server's stop. The parity harness's `EventStream` takes the
  closed chunked stream's AttributeError for its end (`tests/parity/scenarios.py`). Every security check refuses
  what Python's refuses; these change only how. Decided against unless a client is found that sends such a request.
- **serve reads a run's files through a symbolic link.** A run's files are listed and read by `Host.run_files` and
  `Host.read`, which follow a symbolic link (`[ -f ]`, `cat`), so a link an agent leaves in its run directory, or a
  run directory that is a link, lets `/file` and the event stream show a file outside the run, `~/.ssh/id_rsa` say, to
  the desktop app's window; the name a request gives is still only a plain name, and only the token's holder can ask.
  The Rust engine's serve reads them as Python's does (rust-engine ticket 10,
  `a_link_in_the_run_directory_is_followed_as_python_follows_it`), the Python engine being the parity oracle. Closes
  with a ruling: refuse a link whose target is outside the run directory, in both engines' `ProjectRuns` (the Rust
  one only, while the Python engine is frozen), or keep reading through it.
- **The parity harness compares serve's event stream at checkpoints, not event by event.** `serve_endpoints_and_events`
  keeps, at each point where the run waits on an agent and once it is done, the last `record` and `files` event of
  each run (`tests/parity/scenarios.py`, `last_by_run`). Raised by cross-review 65 (F10, 2026-10-27), against the
  spec's "serve's JSON and SSE events" byte for byte: "missing, repeated or reordered intermediate events pass." How
  many events a step sends depends on where serve's one-second poll falls among the run's saves, so the stream
  between two checkpoints differs from run to run. Closes with a serve whose poll the harness can step, which would
  be a hook in the engine, or the user's ruling that the folded stream is the contract, as the desktop app reads it.
- **`test-integrity` does not see a golden file's change.** A golden file under `tests/golden/` (`tests/golden.py`)
  is an expected value outside any assertion, as a text file the check does not parse, so a change that rewrites one
  with `UPDATE_GOLDEN=1` alongside the code it pins gets no `expected-value-changed` finding; the Reviewer sees the
  rewrite only in the diff. Found while adding the golden files (orchestrator-test-methods/06, 2026-10-27). Closes
  with the check reporting a changed file under a project's golden directory, which would need that directory to be
  a setting for projects other than this one (gate-split/04, the test-file patterns).

- **A quality build of a snapshot from before `gate.py` fails mypy.** A quality build judges by main's rules
  (`--config`), and since the split main's `[tool.mypy] files` names `gate.py`, which mypy reads relative to the
  judged tree; a run whose base predates the split has no `gate.py`, so mypy stops with `gate.py: error: Cannot read
  file` (status 2) and the gate is ERROR on a failed condition the Builder cannot fix. mutmut's `source_paths`
  names it too, so surviving-mutants is `not-run` there. Checked by hand against such a tree (2026-10-27). Affects
  only runs started before the split merged that reach a quality round after it; closes by itself once none is
  left, or with the gate passing mypy only the listed files the tree has. Cross-review 61 (F1, 2026-10-27): "the
  gate is ERROR on a failed condition the Builder cannot fix", against the spec's "a quality build is judged by
  main's gate and main's rules".
- **`BUILD_CONFIG_FILES` does not list `gate.py`.** A pull request or main build runs its own tree's `gate.py`, so a
  change that loosens the gate gets no "Build configuration" notice in its quality file; `orchestrator.py`, which
  held the gate, was not listed either. Raised by cross-review 61 (F7, 2026-10-27). Closes with `gate.py` listed,
  with a notice of its own wording (a quality build runs main's gate, not the edit).
- **The quality file's format has two names for each part.** The engine's copies (`QUALITY_OK`,
  `QUALITY_FAILED_CONDITIONS`, `QUALITY_FINDINGS_LIMIT`, `_numbered_finding`, `_check_finding`, `_shortened`) and
  the gate's (`GATE_OK`, `FAILED_CONDITIONS`, `QUALITY_ISSUE_LIMIT`, `_numbered`, `_finding`, `_clip`) differ, so a
  grep for one misses its twin. Most date from gate-split ticket 01. Raised by cross-review 61 (F9, 2026-10-27).
  Closes with one name per part on both sides, a change to both programs.
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
  Seen 2026-10-25 on `main` at `9448612`. On such a machine it also fails mutmut's stats run, which runs every test,
  so `surviving-mutants` is `not-run` there, "failed to collect stats" (2026-10-27). Closes with `PATH` holding a
  directory with only a link to `git`.
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
- **Is 3 GB the right memory limit for the agentbox0 Jenkins agent?** All builds on the node share its container's
  `mem_limit: 3g` (Homelab `jenkins/agentbox0.yaml`), which this repo cannot change. Since `ci/cargo-lock.sh`, two of
  this repository's builds compile Rust at once only when a quality build starts its `cargo llvm-cov nextest` or
  `cargo check` beside another build's cargo step, or when the Gate stage's clippy, outside the lock, checks the desktop
  shell's own crate, the one crate the UI stage's `cargo check` leaves it; but everything else they run (pytest, mutmut,
  PyInstaller, npm) still shares the 3 GB beside a locked rustc, as do other jobs' builds on the node. Open in
  `.scratch/ci-cargo-memory/issues/02-agent-container-memory.md`; closes with the Homelab keeping or raising it.
- **Under the gate's `--cap-lints warn`, an `#[allow]` overrides a workspace's `forbid`.** `Clippy._lint_workspace` in
  `gate.py` caps every lint at warn so that a lint at deny cannot stop cargo before a compile error in a later crate
  (cross-review 66, F1); rustc's cap also lowers `forbid`, so an added `#[allow(unsafe_code)]` under `unsafe_code =
  "forbid"` no longer gives E0453 and its unsafe block is no finding: the gate is OK on a change the workspace's rules
  make uncompilable, and only "Suppressions added" lists the allow, and not even that when the change adds the lint as
  one line inside an existing wrapped `#[allow(\n    a,\n    b,\n)]`, since `_RUST_SUPPRESSION` reads one added line at
  a time (F15). Nothing in this repository sets `forbid` (the top-level `Cargo.toml`'s `[workspace.lints]` are all
  `warn`), and the Test stage's uncapped `cargo llvm-cov nextest` and the UI stage's `cargo check` would still fail
  such a build of the workspace.
  Raised by cross-review 66 (F11, 2026-10-27): "The gate is OK on a change that the workspace's rules make
  uncompilable". Closes with a second, uncapped clippy run whose rustc errors (codes `E` and four digits, such as E0453)
  are compile errors, or with the forbidden lints read from the manifests and an added allow of one failing the gate.
- **A cargo config under the build's `CARGO_HOME` is not listed as build configuration.** `clippy_config_files` in
  `gate.py` lists cargo's config only under a `.cargo` directory, but the Jenkinsfile sets `CARGO_HOME` to
  `$WORKSPACE/.cargo-home`, so a change that tracks `.cargo-home/config.toml` could change clippy's lint levels or
  rustflags without the quality file listing it. Raised by cross-review 70 (round 1, not recorded, 2026-10-27).
  Closes with `CARGO_HOME`'s config files listed too, or `.cargo-home/` refused in a change.
- **A quality build judges Rust by the snapshot's own clippy rules.** `Clippy` in `gate.py` chooses no lints (it caps
  their levels at warn, `--cap-lints`), so clippy reads each workspace's `[lints]`, `clippy.toml`, `.cargo/config.toml`
  rustflags and `rust-toolchain.toml` from the judged tree, where a change can loosen them; `--config` replaces only
  `pyproject.toml`. The quality file lists such an edit under build configuration (`CLIPPY_CONFIG_NAMES`), so the
  Reviewer sees it, but the gate does not prevent it. Found implementing rust-engine ticket 02 (2026-10-27). Closes with
  the gate taking those files from the commit `--config`'s rules come from, or with the Reviewer's prompt naming the
  listing as a finding to judge.
- **herdr 0.9.1 lists no `gemini` integration.** `herdr integration status` on this host lists `antigravity-cli`
  (whose hook is under `~/.gemini/`) but no `gemini`, so `doctor` warns that it does not list gemini, and the
  README's `herdr integration install gemini` may name a kind herdr no longer has. Found by the doctor run
  (2026-10-24); not checked whether `agent start --kind gemini` still works. Closes once `AGENT_KINDS` and the README
  name the kind herdr installs for Gemini, or herdr is shown to accept `gemini`.
- **`doctor` checks gitleaks's `git` subcommand, not its version.** README asks for gitleaks 8.19 or later; the
  check reuses `Host.gitleaks_problem`, the run's pre-push check, which asks `gitleaks git --help` because a
  distribution's build may print no version number. So `doctor` shows no gitleaks version. Kept by choice
  (2026-10-24).
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
- **What a proof writes outside its own directory can enter the commit.** A proof runs in the working tree
  (`Workflow._run_proof`), and only its copy's directory is removed after it, so a file it writes elsewhere, such as
  the `__pycache__` of a module it imports in a project that does not ignore those, stays for `_publish`'s
  `git add --all`. In-place runs always had this; since proof-runs ticket 01 a worktree run has it too, its proofs
  importing the worktree's code. Raised there (2026-10-27). Closes with `_publish` committing only what the turns
  changed, or with proofs running in a throwaway copy of the tree.
- **A proof the Builder moves into the project stays carried as erroring.** `PROOFS_NOTE` lets the Builder move a
  proof into the project as a regression test; `Workflow._run_proof` then records it "it did not run: ... is gone",
  which is not a pass, so `_carried_proofs` keeps carrying it. Raised by the cross-review of proof-runs (review 64,
  F9, 2026-10-27): "is carried and told as a standing finding in every later round": the Builder hears it does not
  pass, and the verdict step hears it "no longer fails but errors", even when the moved test passes in the project.
  Predates that branch, where the same move gave pytest's exit status 4, an error carried the same way. Closes with
  `_carried_proofs` treating a missing proof as moved and dropping it, and the notes saying so.
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
- **The quality file's intro still says `orchestrator.py gate` judged the tree.** `judge_change` writes that intro,
  though since the split the program is `gate.py`. Left as it was on purpose (2026-10-27): `judge_change` scores 24,
  over the cognitive-complexity limit of 15, and the check pairs a function moved into a new file with its base only
  by an unchanged body, so editing it in the pull request that moves it fails the gate as a new function over the
  limit. Cross-review 61 (F3, 2026-10-27): "a Builder or Reviewer following the quality file's own words to
  reproduce the result is sent to a dead command". Closes with the intro naming `gate.py`, in a later change, where
  `judge_change` pairs with its base in `gate.py` by name and is no worse than 24.
- **The gate's intro writes the `--rules` path as it is.** `judge_change`'s intro names the rules file without
  `one_line` (`gate.py`, `intro = ...` in `judge_change`), so a rules path holding a line breaker splits the
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
- **`tests/test_docs.py` gets the parser by patching `argparse.ArgumentParser.parse_args`.** `parse_args` builds
  its parser inline and returns only the parsed arguments, so `cli_commands` calls it with `parse_args` patched
  class-wide to hand the parser back, and reads argparse's private `_actions` and `_SubParsersAction`. Raised by
  cross-review 56 (F8, 2026-10-27): "Breaks if `parse_args` switches to `parse_known_args` or parses twice". It
  fails loudly then (`parse_args parsed without its parser's parse_args`); `built_parser` reads `gate.parse_args`'s
  the same way. Closes with an `orchestrator.build_parser()` and a `gate.build_parser()` that each `parse_args` calls
  and the test reads, a change to both programs and so a version bump.
- **README and docs left over from the README rewrite (PR #90).** Raised by cross-review 50 (2026-10-26), in what
  PR #90 brought, not in code:
  - The Jenkins job path still uses the old folder name, as
    `--quality-gate AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality` (README.md, the `--quality-gate`
    example; also docs/quality-gate.md), where the folder is `Octoflow-AI` and the job `py-octoflow-ai-quality`.
    The Planned rename entry above still lists the folder as not renamed.
  - docs/architecture.md's endpoint table gives `/files?run=ID` as a list of `{"name", "mtime"}`, while
    `RunRequestHandler`'s docstring, which the table names as the authority, gives `{"run", "files"}`.
  - The README's `gate.py` example says exit 0 for OK and 1 for ERROR, but not 2, unjudged, when no quality file is
    written.
  - Smaller: a short line mid-paragraph in the rename entry above; the README no longer says which commands take only
    `--machine`/`--cwd`; the `gate.py` example no longer names the tools it needs (ruff, mypy, pymarkdown, shellcheck,
    actionlint).
  Closes with those lines corrected, or the endpoint table replaced by a pointer to the docstring.
- **Gaps in reading a running build's quality.md (PR #105).** Raised by cross-review 58 (2026-10-27):
  - Each poll of a running build also asks for `artifact/quality.md`, a 404 until Gate has run; asking for
    `artifacts[relativePath]` in the result's `api/json` tree would halve the requests (F2).
  - No test covers a transient `CIError` between two reads, or a build that ends with a file other than the one read
    while it ran (F5).
- **`test_invariants.py`'s `AnyJenkins` and `Answer` copy `FakeCI` and `FakeResponse`** of
  `tests/test_orchestrator.py`. Raised by cross-review 60 (F4, 2026-10-27). Closes with one fake Jenkins the two files
  share.
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
- **A running build's quality.md is taken as whole once two reads agree.** `Workflow._gate_or_end` reads a still
  running build's `quality.md` each poll and takes it when two reads at least `CI_POLL_SECONDS` apart are equal,
  because Jenkins serves an artifact while `archiveArtifacts` is still copying it. Raised by the cross-review of
  `quality-round-early-read` (F1, 2026-10-27): "Equal reads show only that the copy made no progress between them,
  not that it finished." Two such reads are taken only once they hold a whole non-blank line (review 58, F1), so a
  copy stalled that long past its first line and then resumed still hands the round a cut file, which shortens the
  Builder's findings and `quality_rounds`' fingerprints.
  Before 2026-10-27 the file was read only once the build ended; a copy that failed partway, ending the build, was
  taken cut then too, and still is. Closes with a fixed last line that `gate.py` writes and the round
  checks before taking the file (older gates, which the quality build of another project may run, write none), or
  with asking Jenkins whether the Gate stage has completed.
- **The mutation budget leaves most of a large change's mutants untried.** `MUTATION_BUDGET` is 600 s. Measured on
  Jenkins on 2026-10-24: on PR #50 (surviving-mutants' change) mutmut tried 756 of 3079 mutants and the Gate stage took
  683 s, the build about 850 s; on PR #51, 677 of 801 in a 638 s Gate stage. That fits `QUALITY_TIMEOUT` (1200 s) with
  about 350 s to spare, but not after a wait in Jenkins' queue, which counts against a quality round (PR #52 waited
  about 860 s behind the others). Those were pull request builds; a quality build has skipped the Package stage
  since 2026-10-24, so it has more to spare than that. The user kept 600 s (2026-10-24): a stopped run says `budget-reached`, never "none
  survived". Closes with the shared time budget above, or mutants chosen by the changed lines rather than whole
  functions. Worse since mutmut's runs pass the suite (mutmut-hypothesis-health-check, PR #112): on PR #113 (2026-10-27)
  mutmut was stopped at 600 s after trying 0 of 253 mutants, so its stats and clean runs of the now 1348-test suite
  took the whole budget, and surviving-mutants judges nothing on Jenkins; the parity scenarios, left out of mutmut's
  runs, add nothing to that.
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
  as a link (`gate.py`), `_mark_for_mutmut` leaves links unmarked, and mutmut 3.8.0's `walk_all_files` lists
  a symlink to a file and `create_mutants_for_file` opens it. So once any changed function starts mutmut, a change
  adding `x.py -> calc.py` has every function of `calc.py` mutated a second time, as `x.py`, spending the budget on
  mutants no finding reports; and `x.py -> /dev/zero` or a FIFO reads without end or blocks until the budget runs
  out. The gate's own selection skips such links (`_parsed_changed_files`). Raised by the cross-review of
  `gate-python-files` (R2-F1, 2026-10-26); rests on mutmut's source, not on a run. Closes with the copy or
  `_mark_for_mutmut` keeping links out of mutmut's walk, without breaking a tracked link the tests import through,
  and a test that also edits a real function.
- **passes-at-base misses a deleted Python script with no `.py`.** `_edits_python_source` (`gate.py`)
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

- **The Rust engine had no serve.** The desktop app starts `orchestrator serve --origin ...` as its sidecar
  (`ui/src-tauri/src/lib.rs`), which the `orchestrator` binary did not have. Fixed (2026-10-29, rust-engine ticket 10):
  `crate::serve`, on tiny_http, a thread per request: `serve_command` prints `{"url", "token"}` and serves until
  SIGINT or SIGTERM (Fixed, "A Ctrl-C at the terminal does not reach the command the Rust engine's `Host` or herdr is
  running"); `RunServer` binds 127.0.0.1 on a free port and checks each request in Python's order, the `Host` header,
  the `Origin` against its own and `--origin`'s, a CORS preflight answered then, the token from `secrets.token_urlsafe`
  's 32 bytes compared in constant time (`same_bytes`) as `Authorization: Bearer` or `?token=`, the endpoint, its
  parameters, the project (`project_host`, connected once per machine and directory), and in
  `crate::serve::runs` `plain_name` and state.json's refusal (`RunReadError::Refused`, Python's `RunPathRefused`)
  before anything is read; `/runs`, `/files`, `/file` and `/events`, whose `RunWatch` polls every second, every 5 for a
  `--machine`, and sends `record`, `files` and `problem` events, a comment when nothing changed, until the client goes
  or the server stops, which also stops the commands its projects' `Host`s run. A request is read as http.server reads
  it (`crate::py::url`'s `urlsplit` and `parse_qsl`, against Python 3.14's answers in `engine/tests/oracle/python.json`;
  a path that starts with `//` collapsed; the last of a parameter given twice), and any method but GET and OPTIONS gets
  http.server's 501 page. Ported: `TestServeEndpoints`, `TestServeSecurity`, `TestServeEvents`,
  `TestServeEventsOnAMachine`, `TestRunWatch`, `TestServeCommand` and `TestProjectHost` (`engine/src/serve/tests.rs`,
  `engine/src/serve/runs/tests.rs`, and their process cases in `engine/tests/program.rs`), with each refusal; not
  `TestProjectRuns`, whose `project_runs` is the tk GUI's. The parity scenario `serve_endpoints_and_events` passes
  against the binary. Where it answers otherwise than Python: the Open issues "serve answers some requests otherwise
  than Python's http.server" and "serve reads a run's files through a symbolic link".
- **A Host need of the doctor's, left for rust-engine ticket 09b.** (Moved from Open issues, 2026-10-29.) `Host::exec` words a command that cannot be
  started as a message only, so the doctor's `_run_tool`, which tells a tool missing from PATH by its
  FileNotFoundError, cannot tell it so (cross-review 89, F9: "porting `_run_tool`'s 'not on PATH' check will need
  either string matching or an interface change"). Closes with ticket 09b, which ports `doctor`, giving `exec` an
  error of its own for a command not found or having the doctor ask `command -v` first, as `_on_path` already does.
  The three `TestHost*` cases this entry also named (`test_prune_on_real_git`, `test_close_on_real_git`,
  `test_closed_without_a_time`) are ported (Fixed, "The Rust engine's CLI commands about a project's runs").
  Fixed (2026-10-29, ticket 09b): `Host::exec` fails with `ExecError::NotFound` for a command not on PATH and
  `ExecError::Failed` for any other (`engine/src/host.rs`), and the doctor's `run_tool` (`crate::cli::doctor`) tells a
  missing tool by it, as `_run_tool` by FileNotFoundError; `ProcessHost::with_runner` runs a test's FakeTools in
  place of processes, as Python's `Host(run=...)`. `doctor` is ported with `TestCheckSetup` and `TestDoctorCommand`
  (`engine/src/cli/doctor/tests.rs`) but for its tkinter check, which the Rust engine, without the GUI, does not
  make (`test_without_tkinter` not ported); the parity scenario `doctor_checks` passes against the binary.
- **Jenkins calls whose tests need the CLI, left for rust-engine ticket 09b.** (Moved from Open issues, 2026-10-29.) Ticket 07 ported `TestOrchestratorChecks`
  on `judge_run` of the build's quality file; its Workflow cases (`test_a_build_that_failed_before_the_gate_is_not_checked`,
  `test_without_the_quality_gate_none_of_this_runs`, `test_the_spec_is_the_runs_task_without_a_contract_step` and the
  heartbeat kept between PyPI's requests, `test_slow_pypi_keeps_the_run_alive_and_stops_at_the_rounds_deadline`) ticket
  08's part 5 ported through its quality round (`engine/src/workflow/tests/orchestrator_checks.rs`, 2026-10-29), on
  FakePyPI's clock and `events`. `judged_in_quality`, `_build_trust`, `_judging_changes`,
  `JUDGING_FILES` and `JUDGED_DESCRIPTION`, with `TestJudgedInQuality`, are the `judged-in-quality` command's; the
  Rust `Jenkins::retained_builds` they read is ported. Issue 09 names them (2026-10-28); its Plan (split) gives them to
  09b (2026-10-29). Fixed (2026-10-29, ticket 09b): `crate::cli::judged` has `judged_in_quality`, `build_trust`, `judging_changes`,
  `JUDGING_FILES` and the description's format (`Judged::read`, in place of the regular expression), and
  `judged-in-quality` runs without herdr (`crate::cli`'s `herdrless_command`, given a `Local`), with
  `TestJudgedInQuality` in `engine/src/cli/judged/tests.rs`; the parity scenario `judged_in_quality` passes against
  the binary.
- **The Rust engine's CLI commands about a project's runs.** Fixed (2026-10-29, rust-engine ticket 09a): the
  `orchestrator` binary has `list` (and `--json`, `--pr-state`), `metrics`, `show`, `prune`, `close` and `workflows`
  beside `run` and `resume` (`crate::cli::args`' `Command::Runs` and `Command::Workflows`; `crate::cli`'s
  `runs_command`, `herdrless_command` and `record_in_state`; `crate::cli::listing`, `prune`, `close` and
  `workflows`), each flag of Python's parse_args for them, argparse's last-wins for a flag given twice
  (`args_override_self`), and each command's `--help` naming the flags Python's names
  (`engine/tests/oracle/cli.py`, `help_names_the_flags_python_names`, whose `NOT_YET` is drain, refresh,
  judged-in-quality and doctor, ticket 09b's). Ported with them: `TestCLI`, `TestRunRecord`, `TestListJSON`,
  `TestRunHealth`'s print_runs cases, `TestShow`, `TestBlockedListing` but its GUI case, `TestPrune`,
  `TestSweepSummary`, `TestPruneCLI`, `TestClose`, `TestSweepOnRun`, `TestResumeCLI`, `TestWorkflowCLI`,
  `TestWorkflowFileCLI`, `TestSpecCLI`'s main cases, `TestHostGit`'s `test_prune_on_real_git` and
  `test_close_on_real_git`, `TestHostPullRequest.test_closed_without_a_time`, and each CLI case tickets 06 to 08
  left (the Fixed entry "Harness tests left for rust-engine tickets 08 and 09", and the parts of the
  Fixed entry on the Workflow that say "for ticket 09"), in `engine/src/cli/**/tests.rs` and `engine/tests/program.rs`.
  The parity scenarios `workflows_listed`, `runs_listed_and_shown`, `branches_pruned`, `failed_run_closed` and
  `finished_run_closed` pass against the binary. No arguments print the help on stderr with exit status 2, where
  Python without a display exits 2 naming the missing command (the spec's Decisions: no tk GUI). Ticket 09b
  (2026-10-29) adds `drain`, `refresh`, `doctor` and `judged-in-quality` (`crate::cli::drain`, `refresh`, `doctor` and
  `judged`), with `TestDrain`, `TestDrainWorkflows`, TestPromptFiles' drain case, `TestRefresh`, `TestCheckSetup`,
  `TestDoctorCommand` and `TestJudgedInQuality`, and empties `NOT_YET`: every command's `--help` names Python's flags,
  and the parity scenarios `specs_drained`, `conflict_refreshed`, `doctor_checks` and `judged_in_quality` pass. Ticket
  10 (2026-10-29) adds `serve` (`crate::serve`, Fixed, "The Rust engine had no serve"), so the `--help` test covers
  every command but gui (`engine/tests/oracle/cli.py`'s `NOT_PORTED`).
- **Harness tests left for rust-engine tickets 08 and 09.** (Moved from Open issues, 2026-10-29.) Ticket 06 ported `launch_args`, `session_usage` and
  `turn_tokens` (`engine/src/harness/tests.rs`, `engine/src/summary/tests.rs`), but the Python cases that drive them
  through `Workflow`, `parse_args`, `new_state` or `resumable_state` wait for those (cross-review 92, F1: "As merged,
  the ticket's verification is unmet and nothing tracks the gap"). Ticket 08's part 2 (2026-10-29) ported its
  Workflow cases to `engine/src/workflow/tests/harnesses.rs` and `turn_timings.rs`: all of `TestHarnesses`;
  `test_role_agent_starts_that_role_in_its_harness`, `test_resume_with_a_new_harness_relaunches_in_it` and
  `test_a_030_state_resumes_on_claude`, each on the state `new_state` or `resumable_state` would give, built by hand;
  `test_tokens_come_from_the_session_the_turn_ran_in` and `test_a_turn_that_changed_sessions_has_unknown_tokens`.
  Part 3 (2026-10-29) ported the Workflow half of `test_run_dir_granted_in_worktree_runs_only`, with worktree runs.
  Part 7 (2026-10-29), which ports `resumable_state` with resume's flags, ported their `resumable_state` halves
  (`test_a_030_state_resumes_on_claude`'s 0.3.0 state resumed without flags, `test_resume_with_a_new_harness_relaunches_in_it`'s
  `--agent pi`) and `test_resume_keeps_or_overrides_the_saved_harnesses`, in `harnesses.rs`. For ticket 09, which ports
  the CLI: `test_precedence`, `test_flags_reach_the_workflow`, `test_role_agent_for_a_role_the_workflow_lacks` and
  `test_unknown_kinds_are_argparse_errors`, which drive `parse_args`, `new_state` and `main` as a whole. Ported already: `test_launch_args_leave_the_permission_table_alone`,
  the `launch_args` half of `test_run_dir_granted_in_worktree_runs_only`,
  `test_turn_tokens_needs_a_known_window_and_a_harness_it_reads` (ticket 06) and
  `test_a_030_state_without_a_permission_mode` and `test_unreadable_agent_args` (ticket 04, `engine/src/state/tests.rs`
  and states.json). Fixed (2026-10-29, ticket 09a): `test_precedence`, `test_flags_reach_the_workflow`,
  `test_role_agent_for_a_role_the_workflow_lacks` and `test_unknown_kinds_are_argparse_errors` are ported, in
  `engine/src/cli/tests.rs` and `engine/src/cli/args/tests.rs` (Fixed, "The Rust engine's CLI commands about a
  project's runs").
- **The Rust engine's `run` did not prune earlier runs' branches.** Python's `main` runs `sweep_branches` before a
  `run` that opens a pull request; the Rust `run` of rust-engine ticket 08's part 7 did not. Fixed (2026-10-29,
  ticket 09a): `crate::cli::prune` has `prune_branches`, `pull_request_pending`, `sweep_summary` and `sweep_branches`,
  which `crate::cli`'s `new_run` calls before `new_state` for a run without `--no-pr`; a failure is logged and the run
  goes on, but one met while the Interrupt is raised ends the command interrupted. `TestPrune`, `TestSweepSummary`,
  `TestPruneCLI` and `TestSweepOnRun` are ported (`engine/src/cli/prune/tests.rs`), and the parity scenario
  `branches_pruned` passes against the binary.
- **Local times the Rust engine had yet to read.** Python reads a time without an offset in the machine's zone in
  `time.strftime` (`new_run_id`) and `datetime.fromisoformat(...).timestamp()` (`epoch_seconds`, `session_usage`,
  `_young`'s PyPI upload, `pull_request_pending`'s `closedAt`); all but the last were ported so by ticket 08's
  follow-ups. Fixed (2026-10-29, ticket 09a): `crate::cli::prune::pull_request_pending` reads `closedAt` through
  `crate::py::datetime::fromisoformat_timestamp`, and words a `closedAt` that is no time, or none, as Python does
  (`PR #7 is closed at None, which is not a time`).
- **The Rust engine's Workflow is ported in parts, and its Python tests with them.** Rust-engine ticket 08 lands in
  seven parts (its issue's Plan): part 1 (`engine/src/workflow.rs`, 2026-10-29) runs a workflow in the project's
  checkout, its turns, verdict retries, pull request, saves, heartbeat and takeover, and ports `TestWorkflow`,
  `TestPullRequest`, `TestUpToDateBase`, `TestHeartbeat`, `TestRunHealth`'s `run_health` cases (its two `print_runs` cases are ticket 09's, with `list`), `TestPidAlive` and
  `TestOneRunPerCheckout` but its worktree cases (which part 3 ported) and its resume --force
  cases (`test_force_takes_over_the_run_itself`, `test_force_does_not_override_another_live_run`, which go through
  `resumable_state`, with the minimal `run`/`resume`). Part 2 (2026-10-29, tests only: the cases found nothing
  missing in part 1's Workflow) ports `TestResume`, `TestPromptDelivery`, `TestResumePullRequest`, `TestTurnTimings`,
  `TestHarnesses`, the Workflow cases of `TestHarnessFlags` and `TestRunMetrics`, and `TestWorkflowFileAgents`'
  `test_saved_with_the_run` (`engine/src/workflow/tests/`), but for: the cases of a gated run, which part 5 ported
  (`TestResumePullRequest.test_resume_on_another_branch_fails_before_any_turn`'s quality phase, and there its check
  that Jenkins was asked nothing in the other phases; `TestTurnTimings.test_quality_rounds_and_answers_have_records_of_their_own`
  and `test_a_resumed_analysis_keeps_its_start`; `TestRunMetrics.test_a_gated_run_of_two_rounds_records_its_turns_and_quality_rounds`
  and `test_a_resumed_quality_round_is_recorded_once`); `TestTurnTimings.test_a_seeded_spec_has_no_record`, which part 3 ported;
  TestResume's two `RunState.from_dict` cases, which are `crate::state`'s (`engine/src/state/tests.rs`); and the
  CLI's, for ticket 09 (`TestResumePullRequest.test_cli_exits_1_on_another_branch` and
  `test_no_pr_is_saved_with_the_run`, `TestRunMetrics.test_the_metrics_command_prints_a_line_per_run_that_is_done`,
  `TestWorkflowFileAgents.test_workflows_prints_it`). Part 3 (2026-10-29) adds blocked reports (`turn`'s blocks,
  `block`, `unblocked`, `finish_block`, `block_answer` in `engine/src/workflow/turn.rs`), worktree runs
  (`open_worktree`, `check_worktree`, the worktree halves of `switch_to_branch` and `leave_work_dir`, `Host::has_origin`)
  and a seeded spec (`RunSettings::spec`, `seed_spec`, `crate::spec_file`'s `SpecFile` and `read_spec_file`,
  `Pipeline::seeded_start`), and ports `TestBlocked`, `TestBlockedResume`, `TestWorktreeRun`, `TestOneRunPerCheckout`'s
  worktree cases, `TestSeededSpec`, `TestOtherWorkflows`, `TestWorkflowWithoutAVerdict`, `TestSavedBeforeWorkflows`,
  `TestDefaultWorkflowPrompts`, `TestWorkflowPromptsGolden`, `TestSpecBuildReviewExample` and the Workflow-driven cases
  of `TestWorkflowDefinitions`, `TestWorkflowFiles` and `TestPromptFiles`, but for: the cases of a gated run, which part
  5 ported (`TestBlocked.test_a_blocked_quality_answer_waits_then_goes_back_to_the_quality_round` and
  `test_the_note_is_in_every_prompt_of_the_steps_that_can_block_only`; `TestOtherWorkflows.test_gated_step_with_another_name`,
  `test_fresh_gated_step_answering_a_quality_round` and `test_resumed_gated_step_checks_the_credentials_first`;
  `TestWorkflowWithoutAVerdict.test_a_gate_that_still_fails_is_not_finished`, `test_a_missing_last_quality_file_fails_the_gate`
  and `test_a_gate_that_passes_is_finished`; `TestSavedBeforeWorkflows.test_quality_resumes_under_the_default`;
  `TestDefaultWorkflowPrompts.test_gate_passes_after_a_fix_then_runs_out`, `test_gate_runs_out_then_passes`,
  `test_fresh_builder_in_round_two_after_quality_rounds`, `test_fresh_builder_answering_a_quality_round_in_round_two` and
  `test_fresh_reviewer_in_round_two_with_the_gate`; `TestWorkflowDefinitions.test_the_gate_needs_a_gated_step`;
  `TestPromptFiles.test_a_note_file_reaches_the_turn_that_uses_it`); `resumable_state`'s, for part 7
  (`TestWorkflowFiles.test_a_saved_definition_that_cannot_be_filled_in_stops_the_resume` and
  `test_a_corrupt_saved_definition_is_an_error`, `TestPromptFiles.test_a_state_that_replaces_an_unknown_prompt`); and
  the CLI's, for ticket 09 (`TestWorktreeRun.test_a_run_flag` and `test_flag_reaches_the_workflow`, the `print_runs` half
  of `TestBlocked.test_the_wait_is_saved_and_listed`, `TestBlockedListing`, `TestSpecCLI` but for `read_spec_file`'s
  cases, which `engine/src/spec_file/tests.rs` holds, the `role_models` half of
  `TestOtherWorkflows.test_model_reaches_a_role_without_its_own_flag`,
  `TestWorkflowWithoutAVerdict.test_a_failed_gate_exits_as_changes_requested` and `test_finished_exits_zero`,
  `TestWorkflowFiles.test_list_reads_the_gated_step_from_the_saved_definition`, and `TestPromptFiles`' cases of main and
  drain). Part 4 (2026-10-29) adds locked files (`hold_locks`, `locked_changes`, `locks_note` and `clean_up_locks`
  in `engine/src/workflow/locks.rs`) and proofs (`judge_proofs`, `rerun_proofs`, `carried_proofs`, `run_proof` under
  GNU `timeout` and the notes, in `engine/src/workflow/proof_runs.rs`, with `crate::proofs::ProofRun`), and ports
  `TestLockedFiles`, `TestProofs` and `TestProofInAWorktree` (whose proof is a shell script that finds its repository
  with git, where Python's is run by pytest, which the Rust tests do not have), but for the cases of a gated run, which
  part 5 ported (`TestLockedFiles.test_the_gate_judges_the_restored_tests`,
  `TestProofs.test_a_carried_proof_runs_again_after_the_rounds_last_quality_round_and_before_the_review`), and the
  pipeline's and workflow file's, which ticket 04 ported. Part 5 (2026-10-29) adds the quality gate's rounds
  (`judge_quality`, `stuck`, `quality_note`, `gate_passed`, `quality` to `clean_up_quality`, with `require_origin`,
  `delete_ci_refs` and `delete_ci_ref`, in `engine/src/workflow/quality_rounds.rs`; `check_gate` and the credentials
  check of `check_start`; `Collaborators::jenkins` and `pypi`; `crate::quality::under_gate_line`), and ports
  `TestQualityGate`, `TestStuckQualityLoop`, `TestQualityResume`, `TestSecretScanWorkflow`, the Workflow cases of
  `TestOrchestratorChecks` and `TestQualityCLI`'s `test_state_saved_before_the_gate_has_none`, with every gated case
  the earlier parts left for it, on `FakeJenkins` (which gains `cancelled`, `quality_reads` and `quality_replies`) and
  `FakePyPI` (which gains its clock, a `FakeTime`, and `events`); `TestQualityCLI`'s other cases are the CLI's
  (`test_flag_checks`, `test_gate_reaches_the_workflow`, `test_list_shows_the_quality_phase`, ticket 09) and
  `resumable_state`'s (`test_resume_limit_against_the_saved_quality_round`, `test_resume_limit_needs_the_gate`, part
  7). Each test module's doc names the cases it leaves out. Part 6 (2026-10-29) adds the Ctrl-C (`interrupt::on_sigint`,
  now `by_signals`, the command's group stopped by `run_process`, `SystemClocks`, `LocalHuman`, which is Python's
  `notify_locally` and
  `log`); it ports no Python test, whose fakes raise KeyboardInterrupt themselves, and adds the Rust cases the Fixed
  entry on the Ctrl-C names. Fixed (2026-10-29) by part 7, the last: the `orchestrator` binary (`engine/src/main.rs`,
  `crate::cli`) with `run` and `resume`, their flags and checks on clap, `new_state`, `resumable_state`,
  `resumed_state`, `turn_quality_gate_on` and `_off` (`engine/src/workflow/gate_switch.rs`) and `finish`, and the
  Python cases left for it (`TestQualityGateOnResume`, `TestQualityGateOffOnResume` but `test_show_names_the_gate`,
  which is `show`'s and ticket 09's, `TestResumableState`,
  `TestFindRun`, `TestOneRunPerCheckout`'s resume --force cases, `TestWorkflowFiles`', `TestPromptFiles`' and
  `TestQualityCLI`'s `resumable_state` cases, `engine/src/cli/start/tests.rs`); every `run` and `resume` parity scenario
  passes against the binary, and so does each scenario that kills a run, with the engines switched at the kill
  (`tests/parity/test_parity.py`, SWITCHED). What the Rust CLI still lacks is ticket 09's.
- **The Rust engine named a new run by the time in UTC.** `new_run_id` (`engine/src/cli/start.rs`) wrote the time the
  run started in UTC, where Python's `time.strftime` writes the local time, and `IsoTime::timestamp` took a time
  without an offset as UTC, where Python's `timestamp()` takes it as local time: the engine had no time zone database
  to read. So a run id read an hour or more off in another zone, and runs of both engines in one checkout listed out
  of start order there (`run_states` sorts by id). Fixed (2026-10-29, rust-engine ticket 08's follow-ups, the user's
  ruling of 2026-10-29): the jiff crate (0.2.38, its `std`, `tz-system` and `tzdb-zoneinfo` features, no bundled
  database) reads the zone as the C library does for Python, TZ, else `/etc/localtime`, the rules from
  `/usr/share/zoneinfo`, UTC where neither can be read (`crate::py::datetime`); `new_run_id` writes the time by
  `strftime_local`, and `IsoTime::timestamp` solves a time without an offset as CPython's `local_to_seconds` does
  (fold 0: the first of a time shown twice, the offset before the change for one skipped, and its error within a day
  of the years 1 and 9999), for `epoch_seconds`, `session_usage` and PyPI's upload times. Held by
  `engine/tests/oracle/harness.py`'s `fromisoformat` cases, now read in a zone with summer time and with times it skips
  and shows twice, and its `strftime_local` cases in four zones, and by `engine/tests/program.rs`'s
  `a_run_is_named_by_the_time_in_the_machine_s_zone` (TZ 5:45 east of UTC). The parity harness still runs in UTC.
  What Python reads in local time outside run and resume was the Open issue "Local times the Rust engine has yet to
  read, for rust-engine ticket 09", now Fixed ("Local times the Rust engine had yet to read").
- **The parity scenarios did not run against the Rust binary in Jenkins.** The Test stage runs `tests/parity` against
  `orchestrator.py` only, and the Rust tickets ran `PARITY_ENGINE=target/.../orchestrator uv run pytest tests/parity`
  by hand, with the scenarios that switch engines at the kill (`SWITCHED` in `test_parity.py`). Fixed (2026-10-29,
  rust-engine ticket 08's follow-ups, the user's ruling of 2026-10-29): the Jenkinsfile's Rust parity stage, after
  Gate, on a pull request build (`changeRequest()`) whose change touches `engine/`, `tests/parity/`, `Cargo.toml`,
  `Cargo.lock` or `rust-toolchain.toml` (the user's later ruling, 2026-10-29) since the base Gate judges it from
  (`ci/pr-base.sh`, now Gate's too; a `git diff --quiet` as the Test stage's for a quality build; a base or diff
  that fails runs it), runs them against the binary the Test stage's cargo-llvm-cov built
  (`target/llvm-cov-target/debug/orchestrator`, instrumented, its profiles sent to `/dev/null` by `LLVM_PROFILE_FILE`
  through `env`, since the harness gives the engine an environment of its own), limited to the
  scenarios whose recordings invoke only `run` and `resume` (`PARITY_COMMANDS`, new in `test_parity.py`): 33 and the
  18 switched, 51 passing in 198 s locally with SIGINT and SIGHUP ignored, as a Jenkins `sh` step starts them. A
  failing scenario fails the build; its JUnit report is `parity-rust.xml`. Unverified until a pull request build runs
  it: the declarative `when` (`changeRequest()` with an `expression` that runs `sh`), the stage's time on agentbox0
  and the binary's path there. Quality builds and builds of main do not run it, and ticket 09's commands are added to
  `PARITY_COMMANDS` as they land: ticket 09a (2026-10-29) adds `list`, `metrics`, `show`, `prune`, `close` and
  `workflows`, so the stage runs 38 scenarios and the 18 switched, every one but those of `drain`, `refresh`,
  `doctor`, `judged-in-quality` (ticket 09b) and `serve` (ticket 10); 56 passing in 210 s locally with SIGINT and
  SIGHUP ignored, the Python engine's 43 taking 192 s. Ticket 09b (2026-10-29) adds `drain`, `refresh`,
  `judged-in-quality` and `doctor`, so the stage runs every scenario but serve's, 42 and the 18 switched: 60 passing
  in 228 s locally, so too (380 s on agentbox0, PR-178). Ticket 10 (2026-10-29) adds `serve`, so `PARITY_COMMANDS` names
  every command but gui and the stage runs every scenario, 43 and the 18 switched: 61 passing in 253 s locally with
  SIGINT and SIGHUP ignored, `serve_endpoints_and_events` taking 24 s of it; about 405 s expected on agentbox0, by
  PR-178's ratio.
- **The commands the Rust engine runs outlived an engine that got a SIGTERM or a SIGHUP.** The commands run in
  process groups of their own, so a SIGTERM of the engine (a kill, a service manager's stop) or the SIGHUP a closed
  terminal sends its foreground group ended the engine and left the running command to finish unwatched, and the run
  unreleased (Open issue "The commands the Rust engine runs outlive an engine that is SIGKILLed", which this narrows).
  Fixed (2026-10-29, rust-engine ticket 08's follow-ups, the user's ruling of 2026-10-29): `interrupt::by_signals`,
  which was `on_sigint`, waits for SIGTERM and SIGHUP as for SIGINT (`interrupt::SIGNALS`), so each raises the
  `Interrupt`: the running command's group is stopped and the Workflow releases the run as interrupted, the program
  prints the interrupted note with its resume hint (a line stderr does not take, as after a hangup, is lost), and
  `interrupt::exit`, which `main` ends by, ends the process by the first SIGTERM or SIGHUP it got, as the signal's
  default action does (a shell's `$?` 143 or 129), where a SIGINT alone exits 130. The exit status is the Python
  engine's, which those signals kill at once; the release and the note are not: Python leaves the run owned by its
  dead pid, to be resumed as a stale run, and prints nothing. A signal the engine started with ignored stays ignored,
  for it and its commands, as nohup's SIGHUP; the commands start with the three unblocked (`unblock_signals`). Held by
  `engine/tests/signals.rs` (real signals to an engine in a group of its own: a SIGTERM and a SIGHUP stop the command
  and end the engine by the signal, an ignored SIGHUP reaches neither the engine nor its command) and
  `engine/tests/program.rs`'s `a_sigterm_or_a_sighup_before_the_run_ends_the_program_by_it_with_the_interrupted_note`.
- **A Ctrl-C at the terminal does not reach the command the Rust engine's `Host` or herdr is running.** `ProcessHost`
  and `ProcessHerdr` run each command in a process group of its own (rust-engine tickets 05 and 06), which the
  terminal's SIGINT, sent to its foreground group, no longer reaches, where in the Python engine every child gets it
  too, the desktop app's Stop (`killpg` with SIGINT) included; so a Ctrl-C left a push, a scan or an unbounded `herdr
  agent wait` running (cross-review 92, F2). Fixed (2026-10-29, rust-engine ticket 08, part 6): `interrupt::on_sigint`
  (now `by_signals`) blocks SIGINT and starts a thread that waits for it (sigwait) and raises the `Interrupt` at each
  one, from the
  terminal or a kill of the engine's group; `ProcessHost::local` and `ProcessHerdr::new` take the `Interrupt`, and
  `run_process` (`engine/src/host/process.rs`) watches it while a command runs: at a raise the command's group gets
  SIGINT and, once the command exits or `INTERRUPT_GRACE_MS` (250 ms, Python's `Popen._sigint_wait_secs`) pass, SIGKILL,
  and the command fails as interrupted, also one that answered meanwhile, such as a `herdr agent prompt` herdr had
  delivered (review 97, F1); while the `Interrupt` is pending no command starts, until the Workflow catches it
  (`Interrupt::catch`) to release the run; what only logs its failures (the heartbeat, the end's clean-ups, which then
  keep `RunState::ci`) stops the run at a Ctrl-C, as Python's KeyboardInterrupt passes `except OrchestratorError`, and
  the proof copies are removed as Python's finally removes them (`Interrupt::finally`); `SystemClocks`' sleep ends at a
  raise; the children start with SIGINT
  unblocked (`unblock_sigint`, now `unblock_signals`, the engine's one `unsafe`, `CommandExt::pre_exec`). A SIGINT
  ignored when the engine started stays ignored, as in Python. Held by the Ctrl-C cases of `engine/src/host/tests.rs`
  and
  `engine/src/herdr/tests.rs`, `workflow/tests/interrupted.rs` (a run waiting on herdr, one during a prompt herdr
  delivered, one already done), `a_ctrl_c_while_a_proof_runs_stops_it_with_what_it_started` (the proof under GNU
  `timeout`, which leads the group ProcessHost started) and `engine/tests/sigint.rs` (now `signals.rs`: a real SIGINT to
  an engine's group, and one it inherited ignored). Every command of the binary takes it since: `run` and `resume`
  turn a raised `Interrupt` outside a Workflow (resolving the directory, reading the run, switching its gate) into the
  interrupted note and exit status 130 (ticket 08's part 7, `crate::cli`, `stop`), as Python's KeyboardInterrupt
  anywhere does; so do ticket 09a's (`list`, `metrics`, `show`, `prune`, `close`, `workflows`, the sweep before a run:
  `run_command`, `herdrless_command`, and `prune_branches` and `fill_pr_states` stop at a failure met while it is
  raised), and ticket 09b's (`refresh`, `judged-in-quality`, `doctor`, which prints nothing then, and `drain`, which
  stops between runs and after its last, having printed its spec changes: `crate::cli::drain`, `drain_command`). And
  `serve` keeps SIGINT and SIGTERM its own, as Python's does (ticket 10, 2026-10-29): `crate::cli::main` runs it
  before it calls `by_signals`, and `crate::serve::serve_command` blocks the two and waits for them itself, also one
  ignored at start, then stops the server, which raises the `Interrupt` its projects' commands watch, and exits 0;
  SIGHUP keeps its default and ends it, as it ends Python's serve (`engine/tests/program.rs`, the serve cases). What is
  left is the Open issue "The Rust engine's Ctrl-C, what is left".
- **The Rust engine read a workflow file's tables with their keys sorted.** The toml crate keeps a table in a
  `BTreeMap` without its `preserve_order` feature, so a table with two problems could be refused naming the other one
  first, where tomllib gives the file's order. Raised in rust-engine ticket 04 (2026-10-28). Fixed (2026-10-28, the
  user allowed `indexmap`): toml's `preserve_order` (`engine/Cargo.toml`), held by
  `a_table_with_two_problems_names_the_first_in_the_file_as_python_does` (`engine/src/workflow_file/tests.rs`).
- **Jenkins ran the Rust tests of `ui/src-tauri` alone.** The Test stage ran `cargo llvm-cov nextest` in that
  directory, while `gate.py`'s clippy lints every Cargo workspace git tracks; a crate in another workspace, such as
  the Rust engine's, would have had its lines judged by clippy but its tests never run, and its added lines counted as
  nothing to cover. Found while adding Rust coverage (rust-engine ticket 03, 2026-10-27). Fixed (2026-10-28,
  rust-engine ticket 04): the top-level `Cargo.toml` is the one workspace, with `engine/` and `ui/src-tauri/` as
  members, one `Cargo.lock`, `[workspace.lints]` and `rust-toolchain.toml`; the Test stage runs `cargo llvm-cov
  nextest --workspace` and the UI stage `cargo check --workspace` at the top, and the Gate stage reads
  `target/nextest/ci/junit.xml` there.

- **A quality build left out the parity scenarios when only `tests/guard_errors.py` changed.** The Test stage's path
  list named `tests/conftest.py`, whose guards the scenarios run under, but not `tests/guard_errors.py`, which holds
  the errors those guards raise. Raised by cross-review 76 (F1, 2026-10-27), in code PR #162 brought. Fixed
  (2026-10-27): the list names it.

- **`tool_env` passed `UPDATE_GOLDEN` to the pytest and mutmut runs the gate starts.** Under `UPDATE_GOLDEN=1`
  `assert_golden` rewrites its file instead of comparing, so the gate's flaky-tests reruns would rewrite
  `tests/golden/*.txt` and pass a golden test that should fail. Raised by cross-review 76 (F2, 2026-10-27). Fixed
  (2026-10-27): `tool_env` leaves it out, as it does the Jenkins credentials (`TestChildEnvironment`, which also checks it
  under `UPDATE_GOLDEN=1`).

- **No test shows the proof reruns come after the quality loop.** `Workflow._rerun_proofs` runs right before the
  verdict step's turn, which follows the quality loop by construction, but every proof test used `QUICK`, which has
  no quality-gated step. Raised by the cross-review of the proof reruns (R1-F5, 2026-10-24): "no test would notice
  the rerun moving before it". Fixed (2026-10-27): `TestProofs`'s
  `test_a_carried_proof_runs_again_after_the_rounds_last_quality_round_and_before_the_review` runs the default
  workflow through the CI fakes, round 2's first quality round failing, and pins the order of the turns, quality
  rounds and proof runs; it fails with the rerun moved before the gated step's turn.

- **A proof in a worktree run finds the checkout's pytest configuration.** The run directory, and with it
  `RunState.proofs_dir`, is under the project checkout, not the run's worktree, so a `proof_command` of
  `python -m pytest {proofs}` took its rootdir and `conftest.py` from the checkout. Found while writing loop-guards
  ticket 05; reproduced on real git by proof-runs ticket 01: with the project's `pythonpath = ["."]`, the proof
  imported the checkout's module, so it tested the base, not the change. Fixed (2026-10-27): `Workflow._run_proof`
  copies the proof's directory to `<work_dir>/.orchestrator/proofs/<round that wrote it>/`, under a `.gitignore` of
  `*` written as the checkout's is (`_ignore_orchestrator_dir`), runs the proof's copy and removes it; an in-place
  run's copy goes under the checkout's own `.orchestrator/`, so the two kinds of run do not differ.

- **The Jenkins agent lacked rustup's clippy component.** The gate runs clippy on every build that reaches Gate, since
  `ui/src-tauri/` is Rust, and `TestClippy` in `tests/test_gate.py` runs the real one. PR-111's first build showed the
  agent's toolchain (1.99.0) lacked rustup's `clippy` component: the gate was ERROR with "`clippy` is not installed"
  and eight `TestClippy` tests failed. Fixed (2026-10-27, rust-engine ticket 02): the Install stage ran `rustup
  component add clippy`, and PR-111's second build downloaded the component, passed all 1348 tests and the gate was
  OK. Since then the agent's rustup home is root's (`/usr/local/rustup`, PR-163's third build), so no build can add a
  component; the agent image provides clippy, and the Install stage only checks for it (rust-engine ticket 03).

- **`test_invariants.py` and `test_docs.py` fail inside mutmut's copy.** Raised by cross-review 60 (2026-10-27), in
  code PR #106 brought. mutmut runs the tests in `mutants/`, its copy of the repository, whose `orchestrator.py` and
  `gate.py` hold every mutant beside each function and import `mutmut`, and which lacks `README.md`; so the stdlib-only
  and unredirected-header tests read the mutated files, and `test_docs.py` failed reading `README.md`, which at
  `da71e52` failed mutmut's stats run, "failed to collect stats", before the Hypothesis health check below was
  reached. Past those, the clean run failed `test_no_test_sleeps_for_real`: pytest imports `conftest.py` afresh for
  each run in one process, while `test_invariants.py`, imported once, kept the first run's `conftest.RealSleep`.
  Fixed (2026-10-27, mutmut-hypothesis-health-check ticket 01): `tests/conftest.py`'s `REPO` is the repository's top
  directory, the one above `mutants/` under mutmut, and the two files' `ROOT` is it, so they read the files unmutated
  while running the mutated code, whose flags `test_docs.py` still checks; `NetworkUsed` and `RealSleep` moved to
  `tests/guard_errors.py`, which pytest imports once, so the class a test catches is the one the guard raises. Not
  taken: deselecting the tests under mutmut, which would stop `test_docs.py` killing mutants of the parsers' flags,
  nor `also_copy` of `README.md` and what the docs link to, which would leave the source tests reading the mutated
  files.

- **mutmut's clean test run fails on a hypothesis health check, so the gate mutation-tests nothing.** Under the
  gate's mutmut 3.8.0, each `@given` method of `tests/test_properties.py`, first
  `TestChangedLines.test_names_each_changed_file_and_exactly_its_added_lines`, failed with
  `hypothesis.errors.FailedHealthCheck: ... was called from multiple different executors`, mutmut exited 1 with
  "Failed to run clean test", and `surviving-mutants` reported `not-run` for every change (Jenkins build #48).
  Cause confirmed (2026-10-27): mutmut runs pytest in one process more than once (`pytest.main`, the stats run and
  then the clean run), unittest makes a new `TestCase` for each, so the second run calls each `@given` method on
  another `self`; two `pytest.main(["tests/test_properties.py"])` calls in one interpreter fail all twelve the
  second time, the first passing. Fixed (2026-10-27, mutmut-hypothesis-health-check ticket 01): both Hypothesis
  profiles in `tests/conftest.py` suppress `HealthCheck.differing_executors`, which guards against state kept on
  `self` between examples; the `@given` methods keep none, reading `self` only to assert. The other health checks
  stay on. Not taken: leaving `tests/test_properties.py` out of mutmut's runs, which would hide the parsers'
  strongest tests from mutation testing.

- **During the cutover fallback, mutmut cannot test a snapshot that has `gate.py`.** The fallback judges a snapshot
  of this branch by a `$JUDGED_MAIN` from before the split, whose `[tool.mutmut] source_paths` is
  `["orchestrator.py"]`; mutmut copies only `source_paths`, `tests/` and `also_copy` into `mutants/`, so the tests
  that `import gate` fail there and surviving-mutants is `not-run`, and each changed `gate.py` function is
  `not-mutated`. Findings to weigh, not failed conditions. Raised by cross-review 61 (F2, 2026-10-27) from mutmut's
  copy rule and the gate's code, not from a run.
  Fixed (2026-10-27): the fallback is gone (gate-split/03): a quality build is judged by `$JUDGED_MAIN`'s
  `gate.py`, whose `source_paths` name `gate.py`, and a main without one fails the stage unjudged.

- **The quality gate lives inside `orchestrator.py`.** Reusing the gate for another Python project meant copying the
  whole orchestrator, and it can only run inside the judged project's environment anyway, since it starts pytest and
  mutmut with `sys.executable`, which in the binary is the binary. Decided 2026-10-24: the quality file is the one
  interface between the two. Fixed (2026-10-27, gate-split ticket 02): the gate's block moved verbatim into
  `gate.py`, one stdlib-only file beside `orchestrator.py`, whose `main` is the whole program, `python gate.py --base
  REV ...` with the options and exit statuses of the old `gate` subcommand, which `orchestrator.py` no longer has.
  The orchestrator imports nothing from it: it keeps its own copies of the format's pieces it needs (`parse_gate`,
  `gate_failures`, `one_line`, the `QUALITY_*` words), `docs/quality-gate.md`'s "The quality file as a contract"
  describes them, and `TestQualityFileContract` checks the two agree; it also keeps its own
  `_not_ignored_in_children`, for `RunProcess`, and `judged_in_quality` runs git through `Host` and counts `gate.py`
  among `JUDGING_FILES`. The gate's tests are in `tests/test_gate.py`; `test_invariants.py` holds `gate.py` to the
  standard library too, and `test_docs.py` checks the docs' names and `gate.py` flags against both files. The
  Jenkinsfile's Gate stage runs `gate.py`, a quality build the one at `$JUDGED_MAIN` (with a cutover fallback to that
  commit's `orchestrator.py gate`, since removed by gate-split/03); `pyproject.toml`'s mypy files and mutmut
  `source_paths` and the Test stage's coverage name both files. Not closed: a quality build of a snapshot whose tree
  predates `gate.py` is judged by main's rules, whose `[tool.mypy] files` name `gate.py`, and the quality file's
  intro still names `orchestrator.py gate` (Open issues, both).

- **`test-integrity` does not see an expected value held outside the assertion.** `expected-value-changed` compares
  each assertion's literals (`_changed_literals`), so editing a golden constant the assertion names, such as the
  tests' `GOLDEN_PR_BODY` or `GOLDEN_SUMMARY`, changes what is expected without a finding, while the same edit inside
  the assertion is one. Found while adding the provenance trailers to the pull request body (loop-guards ticket 03).
  Closes with the assertions' names resolved to module-level constants the change edits.
  Fixed (2026-10-27): when the code changed, `python_weakened_tests` reads each test file's constants at the base and
  now (`_PythonConstants`: a name one module-level `Assign` or `AnnAssign` binds to an expression with no call, bound
  nowhere else in the module's scope, as `_scope_bindings` counts with `_BINDING_NAME`), and `_changed_constants`
  reports `expected-value-changed` once per kept test and constant of the base whose assertions read it on both sides,
  bare or through the module's dotted name or a module-level import alias of it, when its expression changed other
  than in layout, or it is no longer such a constant. A name the test binds anywhere (`_names_bound_in`) is never read
  as the module's, so shadowing needs no resolving. Not closed: a constant whose name the test binds anywhere, even by
  a binding that does nothing (`global NAME`, a comprehension target, a lambda parameter), is not checked, so such a
  line hides an edited constant (the Reviewer sees it in the diff); a constant imported from another module or
  computed by a call at the base; one built from other constants, whose edits are not seen; one reached through a
  local (`expected = GOLDEN`) or a helper that asserts rather than named in the test's assertions; a fixture's return
  value; an inline value moved into a new constant, which requirement 4 leaves out; a constant changed by a
  module-level mutation (`GOLDEN["k"] = "b"`) or rebound through `global` in a function; and the module reached
  through a relative import, an import inside the test, or an alias imported under a module-level `if` or `try` (only
  a top-level import statement's alias counts as the module).

- **Two empty or cut reads of a running build's quality.md were taken as the file.** `_gate_or_end`'s poll took
  two equal reads as whole, so a `quality.md` that `archiveArtifacts` left at 0 bytes, or cut inside its GATE line,
  for a poll interval stopped the run with "does not start with a GATE line" though its gate ran. Raised by
  cross-review 58 (F1, 2026-10-27), with F3 (the local `ended` in `_quality_report`) and F4 (`_finish_analysis`
  restating the Jenkinsfile's rule).
  Fixed (2026-10-27): two agreeing reads are taken only once they hold a whole non-blank line, as the GATE line is,
  so a whole first line that is no GATE line is still taken and reported at once (review 59, F1);
  `TestQualityGate.test_two_reads_of_a_running_build_without_their_gate_line_are_not_its_quality_file` reads an
  empty file and one cut inside the GATE line twice each before the whole one. The local is `how_it_stood`, and
  `_finish_analysis` points at the Jenkinsfile's Checkout comment.

- **A quality round waits for the whole build, not for its quality file.** `Workflow._analyse` polls the build's
  result until the build ends and only then reads `quality.md`, which the Gate stage archived before any later
  stage ran. So every stage after Gate counts against `QUALITY_TIMEOUT`; the Package stage was taken out of quality
  builds for that (2026-10-24), and a stage added after Gate slows the rounds again. Raised by the cross-review of
  `skip-package-in-quality-builds` (2026-10-24): "`_analyse` waits for the whole build instead of stopping once
  `quality.md` is archived". Closes with the round reading `quality.md` once the Gate stage has archived it, or
  with a rule in the Jenkinsfile that nothing after Gate runs in a quality build.
  Fixed (2026-10-27): `Workflow._analyse` polls `_gate_or_end`, which asks each interval for the build's result and
  then its `quality.md`, and stops at whichever settles the round first. A running build's file is taken once two
  reads agree, since Jenkins serves an artifact while it is still being copied (a heuristic; see the open issue "A
  running build's quality.md is taken as whole once two reads agree"); `_quality_report` then says the
  build "was still running" instead of naming a result. A build that ends without a quality file is handled as
  before. `_finish_analysis` deletes the round's branch while the build may still run, safe for this Jenkinsfile,
  which fetches it only in Checkout; a pipeline that fetches `GIT_REF` after Gate would fail. Package stays out of
  quality builds as wasted work. Not yet observed against the real Jenkins.

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
