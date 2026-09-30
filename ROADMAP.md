# Roadmap

Where the orchestrator should go next. The first two sections are designs, with a recommendation for each
choice: **resuming an interrupted run** and **detecting stale runs**. They come first because together they
close the biggest gap today: a run lives only as long as the process that drives it. The two designs share
the new `RunState` fields, and resume relies on stale detection to know when it may take over a run. After
them comes a backlog of smaller ideas, in no particular order, and one housekeeping note.

`file:line` references point at commit `25e756b` and will drift as the code changes.

**Status:** sections 1 and 2 are implemented. They stay here as the rationale behind the code. The
implementation differs from the design in three places:

- The owner check runs before every write of `state.json`, not only before a heartbeat. A phase save made
  after a `resume --force` would otherwise overwrite the new owner's state.
- The run settings saved in `state.json` include the per-role models, which landed after this design.
- A handoff file that ends the run as invalid also clears `prompted`. After the human deletes the file, the
  resume prompts the role again instead of waiting for a file nobody is going to write.

## 1. Resume an interrupted run

### Problem

The agents and the handoff files outlive the `orchestrator.py` process, but nothing can pick them up again.
When the process dies (Ctrl-C, a dropped SSH session, a closed laptop, a crash), `main` says "the role agents
keep running in herdr" (`orchestrator.py:600-602`), and that is the end of the run. `run` always makes a new
run id (`orchestrator.py:336-337`, `:591`) and a new herdr workspace (`orchestrator.py:393`), so the only way
on is to start over, interview included, or to drive the roles by hand.

A real example is run `20260929-235937-e292fb`. Its Spec Collector wrote `spec.md` after the orchestrator
had gone, so its `state.json` still says `phase: "spec"`, round 0. Its workspace `wA` still exists and its
agent `spec-e292fb` does not. Its spec was carried out by starting a new run, `20260930-065000-c38252`.

### Command

```bash
python orchestrator.py resume RUN [--machine NAME --cwd PATH] \
    [--max-rounds N] [--timeout SECONDS] [--permission-mode MODE] [--force]
```

- `--machine` and `--cwd` come from the shared `target` parent parser (`orchestrator.py:532-534`), as for
  `list` (`orchestrator.py:545`), and the "`--cwd` is required with `--machine`" check
  (`orchestrator.py:548-549`) applies unchanged. They are needed just to find `state.json`, which lives in
  the project directory on the agents' machine (`orchestrator.py:321-323`).
- Exit status is the same as for `run` (`orchestrator.py:40-42`, `README.md:43`).
- `--force` takes over a run that does not look stale (see section 2).

**How `RUN` is named.** Either the full run id only, or also its six-hex key.
**Recommendation:** accept the full id or the key, and fail when the key matches no run or more than one.
The key is what the human sees in herdr, in the workspace label (`orchestrator.py:392`) and the agent names
(`orchestrator.py:447-449`), and it is short enough to type.

**Run settings.** `--max-rounds`, `--timeout` and `--permission-mode` are not saved today
(`orchestrator.py:306-319`), so a resumed run cannot know them. Either the human passes them again, or they
are saved in `state.json` and the flags on `resume` override them.
**Recommendation:** save them and let the flags override. Whoever resumes after a crash rarely remembers the
original flags, and an override is how they grant one more review round. Refuse a `--max-rounds` below the
saved round.

**`--machine` against the saved `machine`.** `state.json` records the `--machine` the run started with
(`orchestrator.py:591`), but the same herdr is reachable two ways: with `--machine slave0` from elsewhere, or
without `--machine` from a pane on slave0 itself (`README.md:76-79`). Run `e292fb` records
`"machine": "slave0"`, yet its directory and workspace sit on a host whose own herdr has no saved machines.
From there the right resume passes no `--machine`. The options are to require the two to match, or not to
compare them and instead check that the saved workspace exists in the herdr being addressed.
**Recommendation:** do not compare. Check the workspace, and save the `--machine` used for the resume. A
strict comparison would refuse the valid resume above, and the workspace check is what actually proves that
the right herdr is being addressed.

### `RunState` changes

| Field | Written | Purpose |
|---|---|---|
| `root_pane: str` | in `_prepare`, with the workspace (`orchestrator.py:393-395`) | The Spec Collector's pane and the pane the Builder splits from. Today it is known only once `_start` saves `agents["spec"]` (`orchestrator.py:452-453`), so a crash between `:395` and `:453` loses it. |
| `max_rounds`, `turn_timeout`, `agent_args` | when the run starts | The run settings above. |
| `prompted: str \| None` | right after `herdr.prompt` returns in `_turn` (`orchestrator.py:466`) | Basename of the handoff file whose prompt was delivered, e.g. `"build-2.md"`. |
| `agents[role]["session"]` | on the first status check after `_start` | The Claude Code session id, used to relaunch an exited role with `claude --resume`. |
| `owner`, `heartbeat_at` | see section 2 | Who drives the run and when it was last alive. |

`herdr agent get` already returns the session id: its `agent` object carries `agent_session.value` next to
`agent_status`. `Herdr.status` (`orchestrator.py:204-211`) keeps only the status, so add
`Herdr.agent(name) -> dict | None` with the same None-on-`agent_not_found` rule and build `status` on it.

`RunState.load(host, cwd, run_id)` reads the file through `host.read`, fails with "no run RUN under
CWD/.orchestrator/runs" when it is missing, drops keys it does not know, and lets the dataclass defaults fill
the missing ones. That is how `state.json` files written before this change, such as `e292fb`'s, load.

### One code path

Resume could be a separate `Workflow.resume()` that repeats the phase sequence with skip logic, or `run()`
could become re-entrant: it starts from `state.phase` and `state.round`, and a new run is simply a resume
from `("spec", 0)` with no workspace.
**Recommendation:** make `run()` re-entrant. With one path, the existing workflow tests
(`tests/test_orchestrator.py:158-322`) keep covering every step a resume takes, and the two cannot drift
apart. Concretely: `_prepare` creates a workspace only when `workspace_id` is empty or the workspace is gone;
each phase checks its handoff file before anything else; `_start` reuses a live agent instead of starting a
new one.

### Handoff file first, then prompt

Today `_turn` prompts the role (`orchestrator.py:466`) before it looks for the handoff file
(`orchestrator.py:471`). A file written while the orchestrator was down, like `e292fb`'s `spec.md`, would
never be noticed: the role would be prompted for work it has already handed over. The new order in `_turn`:

1. The file exists: return it without prompting. An empty file still fails as today
   (`orchestrator.py:496-497`).
2. `state.prompted` names this file and the agent is alive: the prompt was delivered, so only poll.
3. Otherwise: prompt, save `prompted`, then poll.

In a fresh run, step 1 never fires and the flow is the same as today.

**The `prompted` marker.** Without a marker, resume prompts every live agent whose file is missing, and a
Builder in mid-turn gets its prompt twice and does the turn twice. A marker saved after the prompt returns is
wrong only if the process dies between the prompt and the save. A marker saved both before and after the
prompt, with the human asked to settle the ambiguous case, closes even that window.
**Recommendation:** save the marker once, after the prompt. The window it leaves open is one SSH write long,
and the cost of losing it is one duplicate prompt, which the role answers by writing the same file again. The
two-step marker adds a state and a question to the human to close a window of milliseconds.

### Finding and checking agents

The agent names are deterministic, `{role}-{key}` (`orchestrator.py:447-449`), and they are also saved in
`state.agents`. For each role the resumed phase needs, `Herdr.status(name)` (`orchestrator.py:204-211`)
decides:

- **A status:** the agent is alive and is reused as it is. If it is `blocked`, the human is notified, as
  `_turn` does (`orchestrator.py:486-488`).
- **None:** the agent has exited.

Panes and the workspace are checked the same way: `herdr pane get` answers `pane_not_found` and
`herdr workspace get` answers `workspace_not_found`. Add `Herdr.pane_exists` and `Herdr.workspace_exists`
with the None-on-not-found shape of `status`.

**An exited agent, its pane still there.** Start an agent with the same name in the same pane
(`herdr agent start` takes an existing pane). If a session id is saved, pass `--resume SESSION` after `--`,
the path `start_agent` already uses for Claude Code arguments (`orchestrator.py:180-181`). The role then
keeps its conversation and gets its normal prompt, or, if it was already prompted, a short "continue; your
turn ends when you write FILE". Without a session id, or when the resumed session exits at once, start a
fresh session with a recovery prompt. For a Builder in round n > 1 that is `BUILD_PROMPT` plus a note that
the working tree holds its earlier rounds, plus `FIX_PROMPT` (`orchestrator.py:76-93`). For a Reviewer in
round n > 1 it is `REVIEW_PROMPT` plus the paths of the earlier reviews to check against.
The alternative is always to start fresh.
**Recommendation:** resume the session and fall back to a fresh one. For the Spec Collector, the
conversation *is* the interview: a fresh session means the human answers every question again. For the other
roles, the session saves them re-reading the spec and the earlier rounds. The fallback keeps resume working
when there is no session to resume.

**The pane gone, the workspace still there.** Split a surviving pane of the run in the direction the layout
uses: the Builder to the right of the root pane (`orchestrator.py:415`), the Reviewer below the Builder
(`orchestrator.py:420`). If that parent pane is gone too, split any pane of the run that survives.

**The whole workspace gone.** The run can fail and tell the human, or it can create a new workspace
(labelled as in `orchestrator.py:392`), save the new `workspace_id` and `root_pane`, and start the roles it
still needs as described above.
**Recommendation:** create a new workspace. The handoff files are the whole contract between roles, so they
are all a new session needs. Failing would leave the human at the same dead end that resume exists to remove.

### Resume per saved phase

| Saved state | Resume |
|---|---|
| `phase: "spec"` (round 0, `base` null), `spec.md` exists | The `e292fb` case. Accept the spec, leave the Spec Collector alone, and go on to the Builder. `base` is computed now, as `orchestrator.py:411` does: no build has started, so HEAD at this moment is the right base. |
| `phase: "spec"`, no `spec.md` | Focus the Spec Collector and notify the human (`orchestrator.py:401-402`), then apply the `_turn` rules. A live, prompted collector is simply waited on. An exited one is relaunched with `--resume`. Without a session, it restarts with `SPEC_PROMPT`, and the notification says that the interview starts over. If `agents` has no `spec` entry (a crash before `orchestrator.py:453`), the collector is started in `root_pane`. |
| `phase: "build"`, round n, `build-n.md` exists | Go on to review round n. Start the Reviewer only if `agents` has no `review` entry. For n = 1 the saved state can be `build`, round 1 with a Reviewer already recorded, because `_start("review")` saves (`orchestrator.py:453`) before the review loop does (`orchestrator.py:424`). |
| `phase: "build"`, round n, no `build-n.md` | The `_turn` and agent rules above. Without an entry in `agents` (a crash between `orchestrator.py:413` and `:453`), the Builder is started in a split of `root_pane`. |
| `phase: "review"`, round n, `review-n.md` exists | Parse the verdict (`orchestrator.py:430`) and branch as `orchestrator.py:434-440` does. APPROVE or the last round is done; otherwise go to build round n + 1. |
| `phase: "review"`, round n, no `review-n.md` | The `_turn` and agent rules above. |
| `phase: "done"` | Nothing to do. Print the verdict line as `main` does (`orchestrator.py:604-605`) and exit 0 or 3. |
| `error` set, any phase | Clear `error`, then resume at the saved phase and round as above. |

**`done` is a no-op** rather than an error, so that resume is idempotent and a script can call it without
first checking the phase.

**Errors.** Most recorded errors clear up by resuming: a timeout (`orchestrator.py:476-478`), an agent that
exited (`orchestrator.py:473-474`), an SSH failure. The exception is an invalid handoff file: an empty one
(`orchestrator.py:496-497`) or a review without a verdict (`orchestrator.py:431-432`). Step 1 of `_turn`
reads the same file on resume and fails the same way. Resume can refuse and name the file for the human to
fix or delete, or it can move the file aside and prompt the role again.
**Recommendation:** refuse and name the file. Resume should not pass judgement on a role's output. Prompting
again automatically is its own backlog item (re-prompt the Reviewer), and once that exists it applies to
resumed runs as well.

### `base` comes from `state.json`

`_build_and_review` sets `base` from HEAD (`orchestrator.py:411`). A resume that recomputed it would diff
against whatever HEAD is now. If the human committed the Builder's work in the meantime, the Reviewer would
see an empty diff and approve nothing. So only the step from spec to build computes `base`, as today. Every
later phase reads it from `state.json`, and a resumed run past the spec phase must never reach `:411` again.
With the re-entrant `run()` that follows from the structure, and a test pins it down (test 6 below).

### Turn timeouts

`_turn`'s deadline runs on the monotonic clock (`orchestrator.py:365`, `:468`), which does not survive the
process. Resume could persist a wall-clock deadline, or give the resumed turn a fresh `turn_timeout`.
**Recommendation:** start a fresh timeout. The downtime is not the role's fault, and a persisted deadline
would often make a resumed turn time out at once.

### Edge cases

- **Two orchestrators on one run.** A resume while the original orchestrator is alive, or two resumes at
  once, would drive the same agents twice. Resume takes ownership of the run (section 2) and refuses a run
  whose owner is not stale, unless `--force` is given.
- **A half-written handoff file.** Every prompt asks for its file "in a single write"
  (`orchestrator.py:72`, `:85`, `:92`, `:103`, `:112`), so a file that exists is taken as complete. `_turn`
  makes the same assumption today.
- **`--max-rounds` below the saved round.** Refused, as above.
- **A `state.json` from before this change.** It has no `prompted`, `session` or settings. A live agent is
  then prompted again (one duplicate prompt, the price of an old run), an exited one starts fresh, and the
  settings come from the flags or their defaults.
- **Not a git repository.** `base` stays None, and `_change_description` (`orchestrator.py:506-510`) works as
  today.
- **The human edited the working tree during the downtime.** This cannot be detected and is out of scope. The
  Reviewer sees those edits in the diff like any other.

### Test plan

Reuse the fakes in `tests/test_orchestrator.py:21-130`:

- `make_workflow` (`:122-130`) gets a `state=` argument, so a test can hand it a saved `RunState`.
- `FakeHost` (`:21-35`) is pre-seeded with the run's files.
- `FakeHerdr` (`:38-91`) has `statuses` pre-set for live agents and no entry for exited ones, so `status`
  returns None (`:87-88`). It gets `workspaces` and `panes` sets for the new existence checks, and it records
  the session ids it hands out.
- `FakeClock` (`:106-119`) drives polling and its hooks play the world, as today.

Tests:

1. **The `e292fb` shape.** Phase `spec`, `spec.md` present, the collector gone. There is no
   `("prompt", "spec-…")` and no `("workspace", …)` call, the Builder is split from `root_pane`, and the run
   approves.
2. Phase `spec`, no `spec.md`, the collector alive, `prompted == "spec.md"`: no prompt is sent, and a hook
   that writes `spec.md` moves the run on.
3. As test 2 but with `prompted` None: exactly one prompt is sent.
4. Phase `build`, round 2, the Builder gone. With a session id, the start arguments end with
   `--resume SESSION`. Without one, the prompt contains the `BUILD_PROMPT` text and the path of `review-1.md`.
5. Phase `review`, round 1, `review-1.md` present: the Reviewer is not prompted, and the verdict is taken from
   the file.
6. **`base`.** `FakeHost(head="moved")`, saved `base` `"abc123"`, phase `build`: the Reviewer's prompt
   contains `git diff abc123`, as in `tests/test_orchestrator.py:178-187`.
7. Phase `done`: `run()` returns the saved verdict, and `herdr.calls` stays empty.
8. `error` set and a `review-1.md` without a verdict: an `OrchestratorError` names the file. After the test
   deletes it from `host.files`, a second resume prompts the Reviewer.
9. The workspace gone: exactly one `("workspace", …)` call, the new `workspace_id` is saved, and the roles are
   started again.
10. Phase `build`, round 1 with `build-1.md` and a recorded Reviewer: the review goes ahead without a second
    `("start", "review-…")`.
11. `RunState.load` of the `e292fb` keys (no new fields) and of a dict with an unknown key.
12. CLI tests next to `tests/test_orchestrator.py:445-482`: `resume RUN --machine m` without `--cwd` exits;
    `main(["resume", …])` with `Host` patched as in `:460-474`; a key that matches one run, several runs, or
    none.

### Open questions

- Does `claude --resume SESSION` started by `herdr agent start` keep the same session id, and does herdr
  report it in `agent_session`? The design re-reads the session id after every start, so it works either way.
  This needs checking against herdr and Claude Code before building.
- Does herdr release an agent's name as soon as it exits, so that the same name can be started again? The
  comment at `orchestrator.py:185-186` says herdr keeps the name bound while an agent is blocked at startup.
  The exited case needs checking.

## 2. Stale-run detection

### Problem

`state.json` is written only when the phase changes (`_save`, `orchestrator.py:512-513`, called at `:395`,
`:413`, `:424`, `:438`, `:443`, `:453`) and when a run fails (`orchestrator.py:515-519`). Ctrl-C is not
recorded at all (`orchestrator.py:600-602`). So `list` cannot tell a dead run from a live one: `print_runs`
(`orchestrator.py:563-570`) shows `e292fb` as `spec  round 0` with no outcome. That line could be a human in
mid-interview, or a process that has been gone since yesterday.

### New `RunState` fields

- `owner: {"host": str, "pid": int, "started_at": str} | None`: the orchestrator process that drives the run,
  from `socket.gethostname()`, `os.getpid()` and the UTC time it took the run. It is set when a run starts or
  is resumed, and set back to None when that process ends the run: done, failed, or interrupted.
- `heartbeat_at: str`: the owner's UTC wall-clock time (ISO 8601) at its last write of `state.json`.

A pid alone is ambiguous because, under `--machine`, the orchestrator and the run are on different
machines. The host names the machine the pid belongs to.

**Recording Ctrl-C.** `Workflow.run` also catches `KeyboardInterrupt`. It sets `error = "interrupted"` and
`owner = None`, saves on a best-effort basis through `_save_after_error` (`orchestrator.py:515-519`), and
re-raises, so `main` still returns 130. The alternative is a separate status field.
**Recommendation:** use `error`. `print_runs` already shows it (`orchestrator.py:568`), and resume already
treats a run with an error as resumable.

### When the fields are written

**Is a heartbeat needed?** Yes. The longest gaps between phase saves are the turns themselves: up to
`--timeout`, 1800 s, for the Builder and the Reviewer, and unbounded for the interview
(`orchestrator.py:404-407`). Without a heartbeat, a live run in a long interview looks exactly like a dead
one.

**How often?** `_turn` polls every 3 s (`orchestrator.py:31`, `:494`). Each poll already makes a `host.read`
(`orchestrator.py:471`) and a `herdr status` call (`orchestrator.py:472`), and under `--machine` both go over
SSH (`Host.run`, `orchestrator.py:246-253`; `herdr --machine`, `orchestrator.py:133`). There are three
options:

- (a) Write on every poll. That is one more SSH write every 3 s, 50% more round trips, and 20 rewrites of
  `state.json` a minute.
- (b) Write every `HEARTBEAT_SECONDS = 60`, and call a run stale after `STALE_SECONDS = 300`.
- (c) No heartbeat, only pid checks. These work only on the machine the orchestrator runs on.

**Recommendation:** (b). One write a minute is noise next to the roughly 40 SSH calls a minute that polling
already makes. A threshold of five missed beats absorbs a slow SSH hop, or a `Host.run` that hits its 60 s
limit (`orchestrator.py:251`), without false alarms. Five minutes to notice is fine when turns take tens of
minutes.

**Where.** In `_turn`'s poll loop (`orchestrator.py:471-494`): `if now - last_beat >= HEARTBEAT_SECONDS`,
save. `now` comes from the injected `clock`, so `FakeClock` drives it in tests. `heartbeat_at` comes from a
new injected wall clock, because the monotonic clock means nothing to another process.

**The startup-dialog wait.** `_start` waits without a limit while a startup dialog is open
(`orchestrator.py:457`), and no heartbeat can run inside that call. A folder-trust question left for five
minutes would make a live run look stale.
**Recommendation:** replace it with a loop of bounded waits (60 s each) and a heartbeat between them. The
startup wait itself (`orchestrator.py:183`, at most 120 s) is below the threshold and needs no change.

**A failed heartbeat.** If an SSH blip makes the heartbeat write fail, log it and carry on. A failed phase
save still ends the run, as today. A missed beat costs nothing if the next one succeeds.

**Atomic writes.** `Host.write` truncates the file and writes it in place (`orchestrator.py:273`). With a
write every minute, a `list` that runs at the same time has a real chance of reading a half-written file,
and `run_states` then aborts the whole listing as corrupt (`orchestrator.py:297-298`). The fix is to write
`state.json.tmp` and `mv` it into place.
**Recommendation:** make the write atomic. A rename within one directory is atomic, the change stays inside
the same shell script, and the glob in `run_states` (`orchestrator.py:289`) never matches the `.tmp` file.
With a single writer, no locking is needed.

**Takeover.** Before each heartbeat, the owner reads `state.json` again. If `owner` names another process,
because a `resume --force` has taken the run over, the old process stops without saving: saving would
overwrite the new owner's state. That costs one extra SSH read a minute, and it keeps two orchestrators from
driving the same agents.

### Deciding that a run is stale, across machines

Under `--machine`, three machines can be involved:

- the orchestrator's machine (`owner.host`), where the pid lives and whose clock wrote `heartbeat_at`;
- the agents' machine, which holds `state.json`;
- wherever `list` runs.

A pid can be checked only on `owner.host`. Comparing `heartbeat_at` with the clock of the machine running
`list` mixes two clocks, so any skew between them shifts the result.

There are two ways to measure a heartbeat's age:

- (a) The listing machine's clock minus `heartbeat_at`.
- (b) The age of `state.json`'s mtime, measured by the agents' machine: `date +%s` minus the file's mtime, in
  the same shell call that `run_states` (`orchestrator.py:288-299`) already makes.

**Recommendation:** (b), keeping `heartbeat_at` for display and for `show`. It uses one clock, so there is no
skew. The orchestrator is the only writer of `state.json`, so the file's mtime is its heartbeat. It also
works for `state.json` files written before this change: `e292fb`'s was last written on 2026-09-29 at 23:59.
`stat -c %Y` is GNU and `stat -f %m` is BSD, so the script tries one and then the other.

For a run whose phase is not `done` and that has no `error`:

1. `owner.host` equals this machine's host name, the pid is gone (`os.kill(pid, 0)` raises
   `ProcessLookupError`), and the heartbeat is more than `HEARTBEAT_SECONDS` old: **stale** ("orchestrator
   pid N exited").
2. Otherwise, the age is more than `STALE_SECONDS`: **stale** ("no heartbeat for 12m").
3. Otherwise: **running**.

A finished or failed run is never stale. The pid check needs its extra condition because host names can
collide (two machines named `localhost`). That way a collision can only mislabel a run whose heartbeat is
already late. A pid that is alive proves nothing, because pids get reused, so rule 2 still applies to it. An
orchestrator that is alive but cut off from the agents' machine, such as a laptop gone to sleep, shows as
stale. That is accurate: nothing is driving the run. If it wakes before anyone resumes the run, its next
heartbeat makes the run show as running again.

Resume uses the same rules: it refuses a **running** run unless `--force` is given, and it sets `owner` to
itself when it takes over.

### How `list` shows a stale run

`run_states` returns `(age_seconds, state)` pairs. `print_runs` keeps today's line
(`orchestrator.py:568-569`) and fills the outcome column for an unfinished run:

```
20260929-235937-e292fb  spec    round 0  stale: no heartbeat for 7h12m; resume: orchestrator.py resume e292fb --machine slave0 --cwd ~/GitHub/ai-agents-orchestrator
    assess this repo and brainstrom possible useful feature additions
20260930-065000-c38252  build   round 1  running: pid 4242 on laptop, beat 40s ago
    implement recent changes specified in spec
```

The hint repeats the `--machine` and `--cwd` that `list` was given. The other option is a filter,
`list --stale`.
**Recommendation:** mark stale runs in the outcome column. The list is short, and the human reads it to find
what needs them. A filter hides the answer behind a question they do not yet know to ask.

For tests, `print_runs` takes the local host name and a `pid_alive` function as arguments.

### Edge cases

- **The agents' machine changes its clock.** Its mtime and its `date` move together, so the age is
  unaffected.
- **A human edits `state.json` by hand.** That bumps the mtime and delays staleness by at most
  `STALE_SECONDS`.
- **A run from before this change.** It has no `owner`, so rule 1 cannot apply. The age decides, which marks
  `e292fb` stale.

### Test plan

Reuse the fakes in `tests/test_orchestrator.py:21-130`:

- `FakeHost` (`:21-35`) records each write, so a test can count heartbeats.
- `Workflow` takes a `wallclock`, and `FakeClock` (`:106-119`) serves as both clocks.
- `make_workflow` (`:122-130`) passes both clocks through.
- `FakeHerdr.wait` (`:83-85`) can return `blocked` for its first few calls.

Tests:

1. A Spec Collector kept idle for 10 × `STALL_SECONDS`, as in `tests/test_orchestrator.py:250-261`.
   `state.json` is written about once every `HEARTBEAT_SECONDS`, not on every poll. `heartbeat_at` advances,
   and `owner` holds the patched pid and host.
2. The startup dialog (`tests/test_orchestrator.py:263-271`): the waits are bounded, and there are
   heartbeats between them. The existing `herdr.waits == [("idle", "done")]` assertion changes accordingly.
3. A `FakeHost.write` that fails once for `state.json` during a poll: the run carries on.
4. **Takeover.** A hook rewrites `state.json` with another `owner`. The run stops with an error and does not
   overwrite the file.
5. **Ctrl-C.** A `FakeHerdr` script turn raises `KeyboardInterrupt`. The saved state has
   `error == "interrupted"` and `owner` None, and `main` still returns 130.
6. `owner` is None after an approved run (extend `tests/test_orchestrator.py:159-176`).
7. `Host.run_states` parses the age in front of each state, with a `MagicMock` run as in
   `tests/test_orchestrator.py:429-432`.
8. `print_runs` as a pure function:
   - a running run;
   - a stale run by age;
   - a stale run by a dead local pid;
   - a run from before this change;
   - `done` and error rows, which are never stale;
   - the resume hint with `--machine` and `--cwd`.
9. The atomic write leaves no `.tmp` file behind (a real-filesystem test, like
   `tests/test_orchestrator.py:434-442`).

### Open questions

- Should `HEARTBEAT_SECONDS` and `STALE_SECONDS` be flags? The design starts with constants, next to
  `POLL_SECONDS` and `STALL_SECONDS` (`orchestrator.py:31-33`). Revisit if slow links produce false stale
  marks.

## 3. Backlog

- **`close <run-id>`**: close the run's herdr workspace with `herdr workspace close` on the saved
  `workspace_id` (`orchestrator.py:315`). Today the README says to close it by hand (`README.md:60`).
- **`show <run-id>`**: print the state, the handoff file paths (`orchestrator.py:321-333`), the latest
  verdict and the open findings of the last review. `list` gives one line per run (`orchestrator.py:563-570`).
- **`--spec FILE`**: skip the interview when a spec already exists. The file is copied into the run as
  `spec.md`, and `_collect_spec` (`orchestrator.py:398-407`) is skipped.
- **Per-role agent options**: give each role its own permission mode. Today one
  `--permission-mode` applies to every role (`orchestrator.py:610`), through the single `agent_args` that
  every `start_agent` gets (`orchestrator.py:456`).
- **Overridable prompts**: load the role prompts (`orchestrator.py:59-112`) from `.orchestrator/prompts/*.md`
  when those files exist, with the same placeholders.
- **`--worktree`**: the Builder works in a git worktree or branch per run (`herdr worktree create`), for a
  clean base (`orchestrator.py:411`) and parallel runs. The panes and the diff follow `state.cwd`
  (`orchestrator.py:393`, `:508`).
- **Re-prompt the Reviewer once on a malformed review** instead of aborting the run
  (`orchestrator.py:431-432`).
- **Builder `BLOCKED` escalation**: the Builder is told to report when the spec cannot be met
  (`orchestrator.py:83`), but the report goes to the Reviewer regardless. A report that starts with
  `BLOCKED:` should instead notify the human (`orchestrator.py:501-504`) and pause the run.
- **`summary.md` and per-turn timings**: at the end of a run (`orchestrator.py:442-444`), write `summary.md`,
  and record when each `_turn` (`orchestrator.py:460-499`) started and ended in `state.json`.
- **`--commit` on APPROVE**: commit the change with a message built from the spec's Goal (a section required
  by `orchestrator.py:72-73`) when the verdict is APPROVE (`orchestrator.py:434`).

## 4. Housekeeping

`.gitignore:12` ignores `.orchestrator_context.json`. The orchestrator before the herdr rewrite persisted its
context to that file, and 25e756b removed that code. Nothing reads or writes the file now, so the entry looks
like a leftover. It is recorded here only, to be removed in a separate change.
