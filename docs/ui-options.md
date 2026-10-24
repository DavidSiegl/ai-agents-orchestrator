# A successor to the Tk GUI

Status: **Decided on 2026-10-24: option B, a Tauri 2 desktop app, with a React + TypeScript frontend built by
Vite.** The webview talks to the orchestrator, which runs beside it as a sidecar, over local HTTP and Server-Sent
Events, the first of [option B's transports](#option-b-a-desktop-shell-tauri). This chooses against the
[recommendation](#recommendation), which stays below as the assessment the human decided on. The first step is
read-only: `orchestrator.py serve` serves a project's runs, their files and a stream of their changes, and the app
in [`ui/`](../ui) shows them live ([README](../README.md#desktop-app), [code](architecture.md#serve)). It is
option A's step 3 of the [staged plan](#staged-plan) with the step 2 watcher folded in, as polling inside the
server, and a Tauri window in place of the browser tab. That step's packaging question, open question 2, does not
come up: the frontend lives beside `orchestrator.py` in `ui/`, not inside it. The command is called `serve`
(open question 3). The other [open questions](#open-questions) are still for the human to decide.

The GUI is to become a responsive, reactive feature of its own rather than a launcher over the CLI. This
document assesses today's window ([design](design.md#the-gui), [code](architecture.md#gui)), states what its
successor must do, describes what every option needs from the engine, compares three graphical architectures
with wireframes, proposes one with a frontend framework, and lays out the pull requests that would carry it
out. Code is named by symbol, not line, as in [architecture.md](architecture.md).

## Current GUI

**A launcher over the CLI.** `RunWindow` is a form, the project's run list and an output pane. Start run turns
the `RunForm` into arguments with `run_args`, Resume builds them with `resume_args`, and either starts the
program again (`self_command`) as a `RunProcess`: a child in a session of its own whose stdout and stderr are
read on a thread. Everything a run does happens in that child, exactly as if it had been typed.

**Unstructured output.** The output pane is the child's text, appended as it arrives (`_take_output`): the
command echoed in mint, `log`'s indented lines, and `[exited with status N]` in mint or coral. The window
cannot tell a phase change from a heartbeat failure without parsing English, and it does not try.

**A run list read only on `refresh`.** The list is `run_rows` of `project_runs`, the same rows `print_runs`
prints for `list`, fetched on a background thread when a folder is chosen, Return is pressed in the project
folder or machine field, or Refresh is pressed, and once when the window's run exits. A run started elsewhere,
or the window's own run moving from `build` to `review`, shows up only on the next refresh. `status_tag`
colors the Status column by matching the start of `run_outcome`'s text.

**No live view of a run.** The header's role chain (`role_chain`, `_show_pipeline`) is the selected workflow's
definition, not a run's progress. The window shows no phase, round or quality round as they change, no current
role, no heartbeat age, and none of the handoff files (`spec.md`, `build-N.md`, `quality-N-Q.md`,
`review-N.md`) that are the whole record of a run. `HERDR_NOTE` sends the human to a herdr terminal to answer
the Spec Collector or a `BLOCKED:` question; the window cannot.

**One run per window.** `_launch` refuses a second command while `self.process` is set, and `_set_running`
disables Start run and Resume. That keeps Stop and the output pane unambiguous, at the price of a window per
run.

**Effort spent on Tk's limits.** A good share of the GUI's code and tests works around Tk rather than adding
features:

- Tk 8.6 loads no SVG, so `LOGO_PNG` embeds `docs/logo-64.png`, rendered once from `docs/logo.svg` with a
  command kept in a comment.
- `packaging/build-binary.sh` builds the Linux binary with the distribution's Python, not uv's, because uv's
  Tk lacks Xft and draws every font in one bitmap size; it checks that the libtk the Python maps links
  `libXft`, and that tkinter imports and starts Tcl, since PyInstaller would otherwise leave the GUI out with
  only a warning. The macOS Action sets `UV_PYTHON_PREFERENCE: only-managed` for the opposite reason:
  Homebrew's Python has no tkinter.
- Zoom is hand-built: `ZOOM_LEVELS`, `zoom_keys`, `ZOOM_WHEEL` with `ZOOM_WHEEL_SECONDS` to tame trackpad
  bursts, font sizes rescaled one named font at a time, and the Treeview's row height and column widths scaled
  by hand in `zoom`.
- The palette (`NAVY` … `AMBER`) is a set of constants whose contrast `TestGuiPalette` checks pair by pair,
  and `_theme` restyles every ttk widget on `clam` with per-state maps, since macOS's aqua ignores colors; a
  combobox's drop-down list is reached only through the option database.
- `GUI_FONT_FAMILY` asks for Roboto, which Tk cannot load from a file, so it is used only where installed.

**How it is tested.** `RunWindow` takes the tkinter modules as `ui`, so `TestRunWindow`, `TestRunWindowLook`
and `TestGuiCommand` drive it on hand-written fakes (`FakeWidget`, `FakeText`, `FakeTreeview`, `FakeRoot`
running `after` callbacks on `tick`, `FakeStyle`, `FakeFont`, `FakePhotoImage`, `fake_ui`, `fake_tkinter`).
`TestRunProcess` runs real child processes; `TestGuiByDefault`, `TestGuiPalette` and `TestGuiLogo` cover the
pure parts. Together they are some 850 lines of `tests/test_orchestrator.py`. The fakes mirror the subset of
Tk the window calls, so a test proves the calls, not what is drawn; only `gui --smoke-test` in
`packaging/build-binary.sh` opens a real window, and it is skipped on a Linux agent without a display.

**Worth keeping:**

- **A front end to the CLI, not a second engine.** A run started from the UI is the command one would type, so
  `list`, `show`, resume and the saved state apply unchanged.
- **`state.json` as the source of truth.** The window holds no state of its own about a run; whatever it shows
  comes from `RunState` as saved.
- **Stop as SIGINT.** `RunProcess.interrupt` signals the child's process group as Ctrl-C would, so the run
  ends through `main`'s `KeyboardInterrupt` handler with status 130 and stays resumable.
- **Remote runs over SSH.** `project_runs` lists a `--machine` project through `Host` over
  `ssh -o BatchMode=yes`, with no herdr command, so the window lists runs from anywhere.

## Requirements

The successor must offer four capabilities:

1. **Live progress** for every run of a project, with no manual refresh: its phase, round and quality round,
   the current role, its heartbeat age or that it is stale, and that it is blocked.
2. **Handoff files** (`spec.md`, `build-N.md`, `review-N.md`, the quality files `quality-N-Q.md` and
   `build-N-qQ.md`) rendered as Markdown as soon as they are written.
3. **Talking to agents in the UI:** answering the Spec Collector's interview and a `BLOCKED:` question without
   switching to a herdr terminal.
4. **Several runs from one window:** starting, watching and stopping them side by side.

Under these constraints:

- **C1.** The orchestrator's Python stays stdlib-only, with no runtime dependencies (`dependencies = []` in
  `pyproject.toml`).
- **C2.** A JavaScript (or similar) frontend build step is allowed.
- **C3.** `orchestrator.py` may be split into a package.
- **C4.** The Tk GUI is removed once the successor reaches parity.
- **C5.** Runs on a `--machine` remote host work.
- **C6.** The distribution forms are kept, or their replacement is stated: `orchestrator.py`,
  `orchestrator.pyz`, and the Linux and macOS binaries.

## Engine interface

Every option below needs the same four things from the engine. None of them exists as an interface today; each
is named here with the code it would come from. The UI process, whatever its shell, runs on the machine the
human sits at, as `gui` does now; a `--machine` run's orchestrator runs there too, and only herdr and the
project's files are remote ([design](design.md#running-the-agents-on-another-machine)).

### Runs with their state

Today `run_rows` flattens a run into five strings, and the Status string (`run_outcome`) mixes the health, the
block, the verdict, the pull request and the workflow. The UI needs them apart:

- **From:** `Host.run_states` (each `state.json` with its age by the host's clock), `run_health` (running or
  stale, and why), `RunState.blocked`, `verdict`, `error`, `pr_url`, `closed`, `turns`, and `saved_pipeline`
  for the workflow's steps and role labels.
- **Implemented:** `run_record`, a pure function beside `run_rows` that returns one JSON-ready record per run,
  which `run_rows`, `print_runs`, `show` and the Tk run list format, so the CLI and the UI cannot disagree; its
  docstring lists the record's keys. `list --json` prints `{"version": 1, "runs": [...]}` for scripts and for a
  shell that is not Python, and `list --json --pr-state` fills in each pull request's state through gh. The
  "waits for you" reason comes from `state.json` alone; herdr's live agent status joins it with the watcher.
- **Remote:** `Host` already reads every `state.json` of a project in one `ssh -o BatchMode=yes` call
  (`run_states`), so a remote project costs one SSH round trip per poll, as `project_runs` does now.

### Event stream

The UI subscribes once and is told what changed. Every change the capabilities need is already saved to
`state.json` the moment it happens, because the engine calls `Workflow._save` at each one, so the stream can
be derived by watching `state.json` and the run directory, for every run, including those started from a
terminal and those saved by older versions:

| Event | Written by | Seen as |
|---|---|---|
| Phase or round change | `Workflow._goto`, `_take_turn` (to `quality`), `_judge_quality` (back to the gated step) | `phase`, `round`, `quality_round` |
| Turn start | `Workflow._written`, once `herdr.prompt` returns: `_turn_record` appended to `turns` | a `turns` record with `started_at` and no `ended_at` |
| Turn end | `Workflow._end_turn`, at the handoff file's mtime | that record's `ended_at` and `seconds` |
| Handoff file written | the role agent; the engine notices it in `_await_handoff` | a new path in `Host.run_files`, read with `Host.read` |
| Heartbeat | `Workflow._heartbeat` every `HEARTBEAT_SECONDS`, through `_save` and `_write_state` | `heartbeat_at`, and the age `run_health` judges against `STALE_SECONDS` |
| Blocked | `Workflow._block` sets `blocked`; `_unblocked` clears it | `blocked` with `file`, `question` and `k` |
| Waits for the human | herdr: the agent is `blocked`, or a human-paced step's agent is idle | `herdr api snapshot`'s `agents[].agent_status` |
| Run exit | `Workflow.run` (`phase` `done`, `verdict`, `summary.md`), `_release` (`error`, `owner` cleared) | `phase`, `error`, `owner`; for a run the UI started, also the child's exit status |

Two things live only in the orchestrator process and reach no file: the stall notice (`_await_handoff`'s
`quiet_since`, told by `_tell_human` after `STALL_SECONDS`) and the narrative `log` prints to stderr, which
includes every `_ask_human`. For a run the UI started, both arrive as the child's output, which the UI keeps
as today's output pane does, as text. For a run started elsewhere, the UI can compute a stall itself from
herdr's agent status. If a structured narrative is wanted for every run, `log` could also append a JSON line
to the run directory; that is an [open question](#open-questions), not part of the proposal.

- **Proposed:** a watcher in the engine that polls each watched project's `state.json` files and run
  directories, compares each with what it last saw, and emits the events above as JSON with the run id and a
  sequence number. The UI process holds one watcher per project and fans its events out to every view.
- **Local:** a `stat` of each `state.json` once a second costs nothing; the files are small.
- **Remote:** one long-lived `ssh -o BatchMode=yes` per watched project that runs a shell loop, as
  `run_states` ships one, and prints a `state.json` whenever its mtime changes, so a remote project costs one
  connection, not one per poll. Without that, polling `run_states` every few seconds also works, at one SSH
  handshake each.

### Start, resume and stop

- **From:** `run_args` and `resume_args` (with `target_args`) for the arguments, `self_command` for the
  program, `RunProcess` for the child and its output, and `RunProcess.interrupt` for Stop. These stay as they
  are; what changes is that the UI holds one `RunProcess` per run rather than one per window.
- **Stop for a run the UI did not start:** `RunState.owner` names the host and pid of the orchestrator driving
  it. On the UI's own host the UI could send that pid SIGINT, which ends the run as Ctrl-C does; that is new
  behavior and an [open question](#open-questions). A `--machine` run's orchestrator is local, so its owner is
  too.
- **Outliving the UI:** a run's output now goes to a pipe the window reads, so a child whose UI has gone loses
  its output. A UI that may close while runs go on should send the child's output to a file instead, and read
  it from there.
- **Remote:** unchanged: `--machine` is one of the arguments, and the child forwards every herdr command with
  `herdr --machine`. A run without `--machine` still has to start from a herdr pane (`connect`), so the UI
  process must too, as `gui` must now.

### Agent I/O

Each role's agent runs in a herdr pane, named in `RunState.agents` (`name`, `pane`). The engine drives it
through the `Herdr` wrapper; the UI needs to show the pane and type into it, which the engine never does. The
commands below are herdr 0.9.1's, checked on this machine with `--help` and, where marked, by running them:

| Need | herdr command | Checked |
|---|---|---|
| Show the pane's screen | `herdr pane read <pane> --source visible\|recent\|recent-unwrapped --lines N --format text\|ansi` (also `herdr agent read <name>`) | help, and run: returns the pane's text |
| Type an answer | `herdr pane send-text <pane> <text>`, then `herdr pane send-keys <pane> enter` (or `herdr pane run <pane> <text>`, which sends text and Enter in one call) | help only: sending would have typed into a live agent |
| Answer a dialog | `herdr pane send-keys <pane> <key>...`, with `esc`, `up`, `down`, `enter` or `ctrl+c` for a harness's permission or trust prompt | help; the key names by a call that also named an unknown key, which herdr rejects before sending any |
| Wait for output | `herdr pane wait-output <pane> --match <text>\|--regex <pattern> --timeout <ms>` | help, and run: returns `matched_line` and the read |
| Every agent's status at once | `herdr api snapshot`: `agents[]` with `name`, `pane_id`, `agent_status` (`idle`, `working`, `blocked`, `done`, `unknown`), `revision` and `state_change_seq` | run |
| Bring the pane forward in herdr | `herdr agent focus <name>`, as `Workflow._block_answer` does | help |

`herdr agent prompt` is not the way to answer: its help says a prompt to an agent that is already `blocked` is
rejected with `agent_blocked`, and a blocked agent is exactly the one that needs an answer. `pane send-text`
and `send-keys` type into the pane whatever its state.

Assumptions, not verified here:

- **Remote.** `herdr --machine <label> <command>` runs an API command on a saved SSH machine, its help says,
  and the engine already relies on it for `agent`, `pane split` and `workspace` commands. That `pane read`,
  `send-text`, `send-keys` and `wait-output` forward the same way is assumed: no machine is saved on this one,
  so none could be tried.
- **Change detection.** `pane read` and the snapshot report a `revision`; that it grows when the pane's output
  changes, so a UI can skip unchanged reads, is assumed from its name.
- **Pushed events.** herdr's socket API schema (`herdr api schema --json`) has `events.subscribe` with
  `pane.agent_status_changed` and `pane.output_matched`, which would replace polling. No CLI command exposes
  it; using it means speaking herdr's socket protocol from the UI process, and whether it reaches a
  `--machine` host is unknown. The proposal polls the CLI; subscribing is an optimisation for later.
- **Rendering.** `--format ansi` is assumed to return the screen as text with SGR color codes, which an
  ANSI-to-HTML converter can render, rather than a stream of cursor movements that needs a terminal emulator.

## Options

Three graphical architectures. All three keep the engine interface above in Python and the orchestrator
stdlib-only at its core; they differ in what draws the window and how it reaches the engine.

### Option A: a browser app served by the orchestrator

**How it works.** A new command (here `orchestrator.py web`; the name is an open question) starts a
`http.server.ThreadingHTTPServer` on `127.0.0.1` at a free port, opens the default browser on it with
`webbrowser.open`, and prints the URL for a machine without a display. The server holds the watchers, the
`RunProcess` children and the herdr calls; the page is a single-page app it serves from static files shipped
with the orchestrator. Closing the tab closes nothing; the server stops on Ctrl-C or a Quit button, after
asking what to do with runs it started.

**Frontend stack candidates.** Preact with htm and `@preact/signals`, vendored as ES modules with no build
step; Svelte 5 or SolidJS with Vite and TypeScript; React with Vite. Markdown with `marked` or `markdown-it`,
sanitised with DOMPurify; the agent pane with an ANSI-to-HTML converter such as `ansi_up`, or `xterm.js` if
the rendering assumption fails.

**Engine interface and transport.** JSON over stdlib HTTP for the run list, a run's files and the commands
(start, resume, stop, send text, send keys); Server-Sent Events (`text/event-stream`, a long response the
stdlib server writes line by line, which the browser's `EventSource` reconnects by itself) for the event
stream and for the panes the page has open. WebSocket would need a hand-written handshake and framing on the
stdlib; SSE plus POST covers every need, since the browser only ever sends discrete commands.

**Build and packaging.**

- *`orchestrator.py`:* the frontend is a directory of static files beside it. With no build step they are the
  source itself, vendored libraries included; with one, the built bundle. A single-file `orchestrator.py`
  cannot carry a directory, so either the assets are embedded in it as `LOGO_PNG` is, or `orchestrator.py`
  becomes a package (C3) and a checkout runs it as before through a thin `orchestrator.py`.
- *`.pyz`:* the Release stage of `Jenkinsfile` copies `orchestrator.py` into `build/pyz`; it would copy the
  package and the assets too, which `importlib.resources` reads from inside the zip.
- *`packaging/build-binary.sh`:* gets simpler. The tkinter and Xft checks, and the system-Python override on
  Linux, go once Tk is removed; PyInstaller gains `--add-data` for the assets. The smoke test becomes
  `web --smoke-test`: start the server, fetch `/` and the run list, stop. It needs no display, so it stops
  being skipped on Linux.
- *`.github/workflows/macos-binary.yml`:* unchanged but for dropping `UV_PYTHON_PREFERENCE: only-managed`,
  which exists only for tkinter; with a build step, `actions/setup-node` and the build before the script.
- *`Jenkinsfile`:* nothing with no build step. With one, Node on the `untrusted` agent, an `npm ci` and build
  in Install, the frontend's tests in Test, and its linter in the gate.

**Remote machines.** As today: the server runs locally, lists a `--machine` project through `Host` over SSH,
watches it with one SSH connection, and passes `--machine` to the runs it starts. The browser talks only to
`127.0.0.1`. Where the orchestrator itself runs on the remote host, `ssh -L` forwards the port, with no
change.

**The four capabilities.**

1. *Live progress:* the overview subscribes to the project's event stream and redraws a run's row on each
   event; the heartbeat age ticks in the browser between beats.
2. *Handoff files:* a file event fetches the file and renders it as Markdown in the run's detail view.
3. *Agents:* the detail view's agent area shows the current role's pane, pushed over SSE from `pane read`,
   with a text box (`send-text` and Enter) and key buttons (`send-keys`). A run that waits for the human is
   flagged in the overview with an Answer button that opens it.
4. *Several runs:* the server holds any number of `RunProcess` children; each row has its own Stop, and
   several detail views can be open in browser tabs at once.

**Testing.** The Python side runs on a real server on an ephemeral port, over `FakeHost` and `FakeHerdr`, read
with `urllib`: the JSON and the SSE stream are tested as a browser receives them, with no fakes of a UI
toolkit. The frontend's components are tested in Node on jsdom or happy-dom (Vitest, or `node --test`),
against recorded event streams. An end-to-end test with Playwright against the server on fakes is possible,
but needs browsers on the Jenkins agent; it is optional. Compared with today: the ~850 lines of tkinter fakes
go, and what is tested is the protocol and the DOM rather than calls to a mock of Tk.

**Risks.**

- *A local server that can start agents.* Any page in the browser can send requests to `127.0.0.1`, and any
  local user can connect to the port. The server must bind `127.0.0.1` only, require a random token generated
  at start (in the URL it opens, then a cookie), and reject requests whose `Host` or `Origin` header is not
  its own, against DNS rebinding.
- *A tab is not an app:* no Dock icon of its own, and it is easy to lose among other tabs. Browsers' "install
  as app" (a web app manifest) narrows the gap.
- *The server's lifetime:* closing the terminal it runs in ends it, and with it the pipes of its runs, unless
  their output goes to files (see [Start, resume and stop](#start-resume-and-stop)).

**Constraints.**

| | Meets it? |
|---|---|
| C1 stdlib-only | Yes: `http.server`, `webbrowser`, `json`, `threading`. |
| C2 build step | Allowed, not needed. |
| C3 package | Optional; the simplest way to ship the assets. |
| C4 Tk removed | Yes, at the last step. |
| C5 remote | Yes, as today. |
| C6 distribution | All four forms kept; the binaries no longer need tkinter. |

**Wireframes.** The runs overview, with several live runs:

```text
+------------------------------------------------------------------------------------------------------------+
| <  >  C   http://127.0.0.1:47213/#/runs                                                                    |
+------------------------------------------------------------------------------------------------------------+
| (octopus) ai-agents-orchestrator   Project [ ~/GitHub/shop          v ]  Machine [ local  v ]  [ New run ] |
|============================================================================================================|
| Runs of ~/GitHub/shop                                                            live, last event 2s ago   |
|                                                                                                            |
|  Run     Task                           Progress                             Round  Status                 |
|  ------  -----------------------------  -----------------------------------  -----  ---------------------- |
|  42bcab  add a token-bucket rate limit  spec [x] > build [*] > review [ ]    2 q1   Builder working, 4m    |
|  9f01de  fix the login redirect loop    spec [!] > build [ ] > review [ ]    0      Spec Collector asks    |
|                                                                                     [ Answer ]  [ Stop ]   |
|  c0ffee  migrate config to TOML         spec [x] > build [!] > review [ ]    1      blocked: which schema? |
|                                                                                     [ Answer ]  [ Stop ]   |
|  5e5e5e  cache the price lookups        spec [x] > build [*] > review [ ]    1      stale: no beat for 7m  |
|                                                                                     [ Resume ]             |
|  77aa01  drop the legacy exporter       spec [x] > build [x] > review [x]    2      APPROVE  PR #212       |
|                                                                                                            |
|  [*] working   [x] done   [!] waits for you   [ ] not yet                                                  |
+------------------------------------------------------------------------------------------------------------+
```

A run's detail view, with live progress, a rendered handoff file and the agent area:

```text
+------------------------------------------------------------------------------------------------------------+
| <  >  C   http://127.0.0.1:47213/#/runs/20261023-112329-42bcab                                             |
+------------------------------------------------------------------------------------------------------------+
| < Runs    42bcab  add a token-bucket rate limiter                        running, beat 12s ago   [ Stop ]  |
|============================================================================================================|
|  spec ====> build ----> quality ----> build ----> review ----> publish                                     |
|  done 6m    round 2     q1: GATE OK   q1, 4m...                                                            |
|------------------------------------------------------------------------------------------------------------|
| Files                    | build-2-q1.md                                                  rendered | raw   |
|  spec.md          11:24  |                                                                                 |
|  build-1.md       11:41  |  # Round 2: the review's two findings                                           |
|  quality-1-1.md   11:48  |                                                                                 |
|  review-1.md      11:55  |  ## Files changed                                                               |
|  quality-2-1.md   12:02  |  - `client/limiter.py`: refill on a monotonic clock, not the wall clock.        |
|  build-2-q1.md    (now)  |  - `tests/test_limiter.py`: a burst of 50 is spread over 5 s.                   |
|                          |                                                                                 |
| Turns                    |  ## Verification                                                                |
|  Spec Collector   6m     |  `uv run pytest -q`: 412 passed.                                                |
|  Builder         17m     |                                                                                 |
|  Reviewer         7m     |                                                                                 |
|------------------------------------------------------------------------------------------------------------|
| Agent: Builder, pane wK:p2, working                           [ Esc ] [ Up ] [ Down ] [ Enter ] [ Focus ]  |
|  > Running the tests: uv run pytest -q                                                                     |
|    ........................................................................ 412 passed in 9.81s            |
| [ Type to the Builder...                                                                     ] [ Send ]    |
+------------------------------------------------------------------------------------------------------------+
```

### Option B: a desktop shell (Tauri)

**How it works.** A Tauri 2 application: a small Rust binary that opens a window in the platform's own webview
(WebKitGTK on Linux, WKWebView on macOS) and shows the same kind of web frontend as option A. The orchestrator
runs beside it as a sidecar, a program Tauri bundles and starts: the PyInstaller binary, started as
`orchestrator web --stdio` or with a local port. The Rust side starts and stops the sidecar, owns the window,
the tray icon and native notifications, and nothing else; all engine logic stays in Python. Electron would
work the same way with Chromium and Node bundled, at a far larger download; Tauri is the candidate because it
uses the system's webview rather than bundling a browser.

**Frontend stack candidates.** As option A. A build step is close to given, since Tauri's tooling
(`tauri build`) expects one, though it can serve a directory of static files.

**Engine interface and transport.** Either the webview talks to the sidecar's local HTTP server as a browser
does (JSON and SSE, as in A; Tauri then adds only the window), or Tauri's IPC carries commands to Rust, which
relays them as JSON lines over the sidecar's stdin and stdout. The first keeps one protocol for A and B; the
second needs no port and no token.

**Build and packaging.**

- *`orchestrator.py` and `.pyz`:* the CLI only, unchanged, or with option A's server too. The desktop window
  is only in the app.
- *`packaging/build-binary.sh`:* still builds the PyInstaller binary, now as the sidecar, then `tauri build`
  around it. That needs Rust, Node and on Linux the WebKitGTK and GTK development packages.
- *`Jenkinsfile`:* the `untrusted` agent needs Rust, Node and those packages; Package builds the Linux app as
  an AppImage or `.deb`, and Release attaches it in place of, or beside, `orchestrator-linux-x86_64`.
- *`.github/workflows/macos-binary.yml`:* builds a `.app` in a `.dmg`. An unsigned, unnotarised `.app`
  downloaded from a release is quarantined by Gatekeeper and has to be cleared by hand
  (`xattr -d com.apple.quarantine`), which the bare binary today avoids when fetched with `curl`; signing
  needs an Apple Developer account.

**Remote machines.** As option A: the sidecar is the orchestrator, local, with `--machine` and SSH as today.

**The four capabilities.** As option A, plus native notifications for a run that waits for the human (today
`notify_locally`), and a tray icon that lists the runs that need an answer while the window is closed.

**Testing.** The frontend and the sidecar's protocol as in option A. The Rust side is small enough for unit
tests of its relay. An end-to-end test drives the app through `tauri-driver` (WebDriver), which as far as
known supports Linux and Windows but not macOS: an assumption, from Tauri's documentation, not tried here. The
macOS app's only check would then be a smoke test that it starts.

**Risks.** Two more toolchains (Rust, and Node if A has none) in Jenkins and the Action; WebKitGTK versions
differ between distributions, so the Linux app's look and behavior depend on the machine; a sidecar to start,
supervise and stop; Gatekeeper's quarantine of an unsigned app; and an app to update, where today a new
release is one file.

**Constraints.**

| | Meets it? |
|---|---|
| C1 stdlib-only | Yes for the Python; the app adds a Rust binary and a system webview. |
| C2 build step | Needs one: `tauri build`. |
| C3 package | Optional, as in A. |
| C4 Tk removed | Yes. |
| C5 remote | Yes, as today. |
| C6 distribution | Changed: the binaries become a Linux AppImage or `.deb` and a macOS `.dmg`; `.py` and `.pyz` lose the GUI unless A's server stays in them. |

**Wireframes.** The runs overview: a native window with the projects and the runs that need an answer in a
sidebar, and a tray menu.

```text
+-- ai-agents-orchestrator ---------------------------------------------------------------------- _  []  X --+
| File   Run   View   Help                                                                                   |
+-------------------------+----------------------------------------------------------------------------------+
| PROJECTS                |  All runs                                     Filter [ running  v ]  [ New run ] |
|  ~/GitHub/shop       4  |                                                                                  |
|  build-box:~/api     2  |  +-----------------------------+  +-----------------------------+                |
|                         |  | 42bcab   ~/GitHub/shop      |  | a1b2c3   build-box:~/api    |                |
| NEEDS YOU            2  |  | token-bucket rate limiter   |  | paginate the audit log      |                |
|  9f01de  Spec Collector |  | spec > BUILD > review       |  | spec > build > REVIEW       |                |
|  c0ffee  blocked        |  | round 2 q1  Builder 4m      |  | round 1  Reviewer 2m        |                |
|                         |  | beat 12s ago      [ Stop ]  |  | beat 40s ago      [ Stop ]  |                |
| RUNNING              3  |  +-----------------------------+  +-----------------------------+                |
|  42bcab  build r2       |  +-----------------------------+  +-----------------------------+                |
|  a1b2c3  review r1      |  | 9f01de   ~/GitHub/shop      |  | c0ffee   ~/GitHub/shop      |                |
|  d4e5f6  quality r1     |  | fix the login redirect loop |  | migrate config to TOML      |                |
|                         |  | SPEC > build > review       |  | spec > BUILD > review       |                |
| FINISHED             1  |  | ! Spec Collector asks       |  | ! blocked: which schema?    |                |
|  77aa01  APPROVE        |  |        [ Answer ] [ Stop ]  |  |        [ Answer ] [ Stop ]  |                |
|                         |  +-----------------------------+  +-----------------------------+                |
+-------------------------+----------------------------------------------------------------------------------+

  tray:  (octopus) 2
         +--------------------------------------+
         | 9f01de  Spec Collector asks          |
         | c0ffee  blocked: which schema?       |
         |--------------------------------------|
         | 3 running                            |
         | Open window                          |
         | Quit (the runs go on in herdr)       |
         +--------------------------------------+
```

A run's detail view: the run list stays in the sidebar, the progress and a handoff file in the middle, and the
agent's pane on the right.

```text
+-- 42bcab: token-bucket rate limiter ----------------------------------------------------------- _  []  X --+
| File   Run   View   Help                                                                                   |
+------------------+-----------------------------------------------+-----------------------------------------+
| NEEDS YOU     2  |  spec ==> BUILD --> quality --> review        | Builder  pane wK:p2  working            |
|  9f01de          |  round 2, quality 1, Builder for 4m           |-----------------------------------------|
|  c0ffee          |  beat 12s ago                       [ Stop ]  | > Running the tests                     |
|                  |-----------------------------------------------|   uv run pytest -q                      |
| RUNNING       3  |  [ spec.md ][ build-1.md ][ review-1.md ]     |   ..................................... |
| >42bcab build r2 |  [ quality-2-1.md ][ build-2-q1.md* ]         |   412 passed in 9.81s                   |
|  a1b2c3          |-----------------------------------------------|                                         |
|  d4e5f6          |  # Round 2: the review's two findings         | > Writing build-2-q1.md                 |
|                  |                                               |                                         |
| FINISHED      1  |  ## Files changed                             |                                         |
|  77aa01          |  - client/limiter.py: refill on a monotonic   |                                         |
|                  |    clock, not the wall clock.                 |                                         |
|                  |  - tests/test_limiter.py: a burst of 50 is    |                                         |
|                  |    spread over 5 s.                           |-----------------------------------------|
|                  |                                               | [Esc] [Up] [Down] [Enter]   [Focus]     |
|                  |  ## Verification                              | [ Type to the Builder...      ] [Send]  |
|                  |  uv run pytest -q: 412 passed.                |                                         |
+------------------+-----------------------------------------------+-----------------------------------------+
```

### Option C: option A's app in a native window, through pywebview

Worth comparing because it is the cheapest way to a real window, with a Dock icon and a close button that
means something, while Python stays the only runtime: it shows what keeping today's "a window" experience
costs, and that the cost is C1.

**How it works.** Option A's server and frontend, unchanged, but the binaries open the page in a native window
with pywebview (`webview.create_window(url)`), which wraps the platform's webview: WKWebView through PyObjC on
macOS, WebKitGTK through PyGObject or Qt's WebEngine through PySide or PyQt on Linux. `orchestrator.py` and
the `.pyz` stay stdlib-only and fall back to the browser, as in A; only the binaries carry pywebview. The
window's layout can stay close to today's: one window, the runs on top and the selected run below.

**Frontend stack candidates.** As option A.

**Engine interface and transport.** As option A: JSON over stdlib HTTP and SSE on `127.0.0.1`. pywebview's own
JavaScript bridge (`js_api`) could replace the HTTP calls inside the window, but would split the protocol from
the browser fallback's.

**Build and packaging.**

- *`orchestrator.py` and `.pyz`:* as option A.
- *`packaging/build-binary.sh`:* PyInstaller bundles pywebview and its backend. On macOS that is PyObjC, which
  installs from wheels. On Linux it is PyGObject with WebKitGTK from the distribution, or PySide6 with Qt
  WebEngine at well over 100 MB; either way the system-Python dependence that Tk caused stays in another form.
  The smoke test needs a display again for the window.
- *`Jenkinsfile`:* the `build` dependency group gains pywebview and its backend; the Linux agent needs the GTK
  and WebKitGTK packages, or accepts Qt's size.
- *`.github/workflows/macos-binary.yml`:* unchanged but for the build group.

**Remote machines.** As option A.

**The four capabilities.** As option A, in one window; several runs mean several detail panes, or a second
window from `webview.create_window`.

**Testing.** As option A for the server and the frontend. The native window itself is tested only by the smoke
test, as Tk's is today, and on Linux only where there is a display.

**Risks.** pywebview's Linux backends (GTK, Qt) behave differently from each other and from macOS; PyInstaller
bundling of GTK or Qt is fragile; two ways to show the same UI (window and browser) to keep alike; and the
dependency is in the binary, so C1's "no runtime dependencies" holds only for the `.py` and `.pyz`.

**Constraints.**

| | Meets it? |
|---|---|
| C1 stdlib-only | **No** for the binaries, which bundle pywebview and its backend; yes for `.py` and `.pyz`. |
| C2 build step | Allowed, not needed. |
| C3 package | Optional, as in A. |
| C4 Tk removed | Yes. |
| C5 remote | Yes, as today. |
| C6 distribution | All four forms kept; the binaries trade tkinter for pywebview and a webview. |

**Wireframes.** The runs overview, in one native window laid out as today's, with the form folded away and the
runs live:

```text
+-- ai-agents-orchestrator -------------------------------------------------------------------- _  []  X --+
| (octopus) ai-agents-orchestrator                                                        [ - ] 100% [ + ] |
|==========================================================================================================|
| > New run  (click to open the form)                                                                      |
|----------------------------------------------------------------------------------------------------------|
| Runs of  [ ~/GitHub/shop                         v ]  Machine [ local     v ]           live, 2s ago     |
|  +------------------------------------------------------------------------------------------------------+|
|  | Run     Phase    Round  Role             Status                                   Task               ||
|  | 42bcab  build    2 q1   Builder          working 4m, beat 12s ago                 token-bucket lim.. ||
|  | 9f01de  spec     0      Spec Collector   ! asks you                     [Answer]  login redirect l.. ||
|  | c0ffee  build    1      Builder          ! blocked: which schema?       [Answer]  config to TOML     ||
|  | 5e5e5e  build    1      Builder          stale: no beat for 7m          [Resume]  price lookups      ||
|  | 77aa01  done     2      -                APPROVE  PR #212                         legacy exporter    ||
|  +------------------------------------------------------------------------------------------------------+|
|                                                                            [ Stop selected ]             |
|----------------------------------------------------------------------------------------------------------|
| Select a run to see its progress, files and agent here.                                                  |
+----------------------------------------------------------------------------------------------------------+
```

A run's detail view, below the list in the same window, with tabs:

```text
+-- ai-agents-orchestrator -------------------------------------------------------------------- _  []  X --+
| Runs of  [ ~/GitHub/shop  v ]   42bcab  build  2 q1  Builder  working 4m          live, 2s ago           |
|----------------------------------------------------------------------------------------------------------|
| 42bcab  add a token-bucket rate limiter                                                        [ Stop ]  |
|  spec [x] ---- build [*] ---- quality [x] ---- build [*] ---- review [ ] ---- publish [ ]                |
|               round 1         q1 GATE OK       round 2 q1                                                |
|                                                                                                          |
|  [ Files ]  [ Agent ]  [ Output ]                                                                        |
|  +-----------------------+------------------------------------------------------------------------------+|
|  | spec.md               | # Round 2: the review's two findings                                         ||
|  | build-1.md            |                                                                              ||
|  | quality-1-1.md        | ## Files changed                                                             ||
|  | review-1.md           | - `client/limiter.py`: refill on a monotonic clock, not the wall clock.      ||
|  | quality-2-1.md        | - `tests/test_limiter.py`: a burst of 50 is spread over 5 s.                 ||
|  | build-2-q1.md   new   |                                                                              ||
|  +-----------------------+------------------------------------------------------------------------------+|
|  Agent tab:  Builder, pane wK:p2                                                                         |
|  +------------------------------------------------------------------------------------------------------+|
|  | > Running the tests: uv run pytest -q ............................... 412 passed                     ||
|  +------------------------------------------------------------------------------------------------------+|
|  [ Type to the Builder...                                          ] [ Send ]  [Esc] [Up] [Down] [Enter] |
+----------------------------------------------------------------------------------------------------------+
```

## Comparison

Rated against the same criteria; "as A" means the same answer for the same reason.

| Criterion | A: browser app | B: Tauri | C: pywebview |
|---|---|---|---|
| Live progress | Yes, SSE from the watcher | As A, plus native notifications | As A |
| Handoff files | Yes, rendered Markdown | As A | As A |
| Talking to agents | Yes, `pane read` with `send-text` and `send-keys` | As A | As A |
| Several runs | Yes; several tabs | Yes; one window with a sidebar | Yes; one window, or a second one |
| C1 stdlib-only | Yes | Yes for Python; adds a Rust app | **No** for the binaries |
| C2 build step | Optional | Required | Optional |
| C3 package | Optional | Optional | Optional |
| C4 Tk removed | Yes | Yes | Yes |
| C5 remote | Yes, as today | Yes, as today | Yes, as today |
| C6 distribution | All kept; binaries lose tkinter | Binaries replaced by AppImage/`.deb` and `.dmg` | All kept; binaries swap tkinter for pywebview |
| Build and packaging cost | Low: assets in the `.pyz` and binary; `build-binary.sh` shrinks | High: Rust, Node, WebKitGTK in Jenkins and the Action; unsigned `.app` | Medium: GTK or Qt bundling on Linux |
| Remote support | One SSH watcher per project | As A | As A |
| Testability | High: real HTTP and SSE over fakes; DOM tests in Node; smoke test without a display | As A, plus a Rust relay; no WebDriver on macOS | As A, but the window only by smoke test, with a display |
| Rough effort | 5 pull requests ([staged plan](#staged-plan)) | A's, plus 2 to 3 for the shell and its packaging | A's, plus 1 for the window, plus upkeep of the Linux backend |

## Recommendation

**Proposed: option A, a browser app served by the orchestrator, written in Preact with htm and signals, with
no build step.**

**Why A.** It is the only option that meets every constraint without a "but": the Python stays stdlib-only
everywhere (B adds a Rust app, C a dependency in the binaries), every distribution form is kept, and the
binary build gets simpler rather than harder, since the Tk, Xft and system-Python workarounds go and the smoke
test no longer needs a display. Its remote support is today's. It is the most testable, because the boundary
the tests drive is a real protocol rather than a fake toolkit. And it does not close the door on a window: B
and C both show option A's frontend, so either can be added later, on top of a finished A, if a tab turns out
not to be enough. Building B or C first would pay their packaging cost before knowing that.

**Why Preact with htm and signals over a build-step framework such as Svelte.** The deciding fact is how the
orchestrator ships: a checkout runs `python orchestrator.py` as it is, the `.pyz` is a zip of the source, and
the Jenkins agent has Python and uv but no Node.

- *No build step means no generated code to ship.* With Preact and htm the files the browser loads are the
  source, a few vendored ES modules pinned by version and hash, so a checkout, the `.pyz` and the binary all
  carry the same files, and Jenkins and the macOS Action need no Node. With Svelte, every one of those forms
  needs the built bundle: either committed, so the repository holds generated code that can drift from its
  source, or built in Jenkins and in the Action, which adds Node to both and leaves a checkout without a UI
  until it is built.
- *The UI is small.* Three views (overview, run detail, new run), a dozen components, one event stream.
  Svelte's compiler and its smaller runtime pay off on larger apps; here Preact's runtime is a few kilobytes,
  and `@preact/signals` gives the same fine-grained updates a stream of events needs, without a store library.
- *What the build step would buy can be had without it.* Type checking: JSDoc types checked by
  `tsc --checkJs --noEmit` as a dev-only check, like mypy for the Python, typing the event protocol on both
  sides. Tests: Vitest or `node --test` run the same modules as the browser loads.
- *The cost:* htm's tagged templates are less familiar than JSX or Svelte's templates, and editor support for
  them is weaker; no tree-shaking, which a UI this size does not need. If the UI grows well past three views,
  moving to a build step later changes the tooling, not the server or the protocol.

React with Vite is the same argument as Svelte, with a larger runtime. SolidJS has signals natively but, like
Svelte, needs its compiler.

## Staged plan

Each step is one pull request, mergeable on its own, in this order. Bumps follow the project's release rules:
`patch` for a fix or a change with no new flag, `minor` for a new feature or flag, `major` for a change that
breaks existing flags, exit codes or saved run state; changing or removing `gui`, or what running with no
arguments does, is `major`.

1. **Runs as data.**
   - *Scope:* the per-run record beside `run_rows` (which `run_rows` and `print_runs` then format),
     `list --json`, and `--pr-state` for the pull request's live state, tested on `FakeHost`. No UI. The event
     watcher, which this step first included, is step 2 now.
   - *Unlocks:* every later step's data, and scripts that read runs without parsing `list`'s text.
   - *Bump:* `minor` (`list --json` is a new flag).
   - *Tk and new UI:* only Tk exists; it is unchanged.
2. **The event watcher.**
   - *Scope:* the watcher that turns `state.json` and run-directory changes into events, locally and over one
     SSH connection per remote project, and `list --json --watch`, tested on `FakeHost` and real directories.
     No UI.
   - *Unlocks:* live progress for step 3, and for scripts.
   - *Bump:* `minor` (`--watch` is a new flag).
   - *Tk and new UI:* only Tk exists; it is unchanged.
3. **A read-only live UI.**
   - *Scope:* `orchestrator.py web`: the stdlib server on `127.0.0.1` with its token and `Host`/`Origin`
     checks, the JSON and SSE endpoints, the vendored Preact, htm, signals, Markdown and sanitiser modules,
     and the overview and detail views with live progress and rendered handoff files. Shipping the assets:
     either `orchestrator.py` becomes a package here (C3), with a thin `orchestrator.py` for a checkout and
     the `Jenkinsfile`'s `.pyz` step and `build-binary.sh`'s PyInstaller call updated, or the assets are
     embedded in `orchestrator.py`; see the open questions. `web --smoke-test` joins the binary's smoke tests.
   - *Unlocks:* capabilities 1 and 2, for every run, including those started from a terminal.
   - *Bump:* `minor` (a new command).
   - *Tk and new UI:* coexist; `gui` and no arguments still open Tk.
4. **Run control.**
   - *Scope:* the New run form and Resume in the browser, one `RunProcess` per run with its output sent to a
     file so runs can outlive the server, Stop per run, and the runs' output in the detail view; Stop for a
     run started elsewhere if the human wants it.
   - *Unlocks:* capability 4, and parity with Tk's form, list and output pane.
   - *Bump:* `minor`.
   - *Tk and new UI:* coexist.
5. **Agent I/O.**
   - *Scope:* the agent area: the current role's pane through `herdr pane read`, pushed over SSE; the text box
     through `pane send-text` and Enter, the key buttons through `pane send-keys`, and Focus through
     `herdr agent focus`; the overview's Answer button for a run whose agent is `blocked`, whose human-paced
     step waits, or whose `RunState.blocked` is set. Checks the remote and rendering assumptions on a real
     `--machine` host.
   - *Unlocks:* capability 3: the interview and `BLOCKED:` answers without a herdr terminal. `HERDR_NOTE`'s
     advice no longer applies to the new UI.
   - *Bump:* `minor`.
   - *Tk and new UI:* coexist; the new UI now does everything Tk does, and more.
6. **The new UI becomes `gui`, and Tk goes.**
   - *Scope:* `gui`, and no arguments where `gui_by_default` finds a display, open the web UI; `web` stays as
     an alias or goes (an open question). `RunWindow`, `RunForm`'s Tk parts, the palette constants,
     `LOGO_PNG`, the zoom code and `gui_command`'s tkinter import are deleted, with `TestRunWindow`,
     `TestRunWindowLook`, `TestGuiCommand`, `TestGuiPalette`, `TestGuiLogo` and the tkinter fakes.
     `build-binary.sh` drops the tkinter and Xft checks and the system-Python override; the macOS Action drops
     `only-managed`. README, design.md and architecture.md describe the new UI.
   - *Unlocks:* C4; one UI to maintain.
   - *Bump:* `major` (what `gui` and no arguments do changes).
   - *Tk and new UI:* only the new UI.

If a desktop window is wanted after step 6, option B or C is a further pull request on top of it, with its own
bump: `minor` if it is added beside the browser, `major` if it replaces a distribution form.

## Open questions

1. **The option and the framework.** A with Preact and htm, as proposed, or another option or framework.
2. **Package or single file.** Split `orchestrator.py` into a package at step 3, so the frontend's files ship
   beside it, or keep one file with the assets embedded as `LOGO_PNG` is. Splitting ends "copy
   `orchestrator.py` to the remote machine" ([design](design.md#running-the-agents-on-another-machine)); the
   `.pyz` would take that role. Is that a `patch` (no flag, exit code or state changes) or, for users who copy
   the file, a `major`?
3. **The command's name**, `web` or `ui` or something else, and whether it survives as an alias of `gui` at
   step 6.
4. **No arguments without a display.** Today they print the usage on a Linux without a display. Should they
   start the server and print its URL instead, for use over `ssh -L`?
5. **Stopping runs the UI did not start**, by sending SIGINT to `RunState.owner`'s pid on the same host. Today
   only the window's own child can be stopped.
6. **A structured event log.** Should `log` also append JSON lines to the run directory, so the narrative and
   the stall notices reach the UI for runs started from a terminal too? That adds a reserved name beside
   `state.json` and `summary.md`, which workflow files then cannot use for a handoff file.
7. **Several projects.** The overview shows one project, as the window does now; should it watch a list of
   projects at once, as option B's wireframe does?
8. **Closing the server with runs going.** Interrupt them, as the window's confirmed close does, or leave them
   running with their output in files, to be picked up by the next start of the UI.
