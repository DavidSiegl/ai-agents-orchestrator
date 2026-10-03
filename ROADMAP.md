# Roadmap

Where the orchestrator should go next. The first three sections are designs, with a recommendation for each
choice: **resuming an interrupted run**, **detecting stale runs** and a **quality gate through Jenkins and
SonarQube**. The first two come first because together they close the biggest gap today: a run lives only as
long as the process that drives it. The two designs share the new `RunState` fields, and resume relies on
stale detection to know when it may take over a run. The third, not implemented yet, puts a Jenkins build and
a SonarQube analysis of the Builder's change between the Builder and the Reviewer, and sends a failed gate
back to the Builder.
After them comes a backlog of smaller ideas, in no particular order, and one housekeeping note.

`file:line` references in sections 1, 2, 4 and 5 point at commit `25e756b`, those in section 3 at commit
`3f7f439`. All of them will drift as the code changes.

**Status:** sections 1 and 2 are implemented. Section 3 is a design only, and none of it is implemented yet.
Sections 1 and 2 stay here as the rationale behind the code. Their implementation differs from the design in
four places:

- The owner check runs before every write of `state.json`, not only before a heartbeat. A phase save made
  after a `resume --force` would otherwise overwrite the new owner's state.
- The run settings saved in `state.json` include the per-role models, which landed after this design.
- A handoff file that ends the run as invalid also clears `prompted`. After the human deletes the file, the
  resume prompts the role again instead of waiting for a file nobody is going to write.
- Pull requests, which also landed after this design, add a `publish` phase between the last review and
  `done`. The saved `pull_request` setting decides whether a run has it, and a run saved before it existed
  does not. A resume in `publish` opens no workspace, and it skips the commit and the pull request that
  the saved state shows are already made.

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

## 3. Quality gate through Jenkins and SonarQube

`file:line` references in this section point at commit `3f7f439`.

### Problem

Jenkins builds this repository in four stages (`Jenkinsfile:10-48`). Install runs `uv sync --frozen`
(`Jenkinsfile:10-14`). Test runs pytest with coverage and publishes the JUnit results (`Jenkinsfile:16-25`).
SonarQube Analysis runs the scanner inside `withSonarQubeEnv('Sonarqube')` (`Jenkinsfile:27-40`). Quality
Gate waits for SonarQube's verdict and aborts the pipeline when the gate fails (`Jenkinsfile:42-48`).

None of that reaches a run. The Builder→Reviewer loop (`orchestrator.py:690-708`) goes from the Builder's
report straight to the Reviewer, and the change leaves the agents' machine only in `_publish`
(`orchestrator.py:720-746`), after the last review. So Jenkins and SonarQube first see a change once its pull
request is open, when no role is left to act on what they find. The Reviewer runs the tests itself, but
nobody runs the static analysis, and a human reads the SonarQube result, if anyone does.

The repository's SonarQube project is `py-ai-agents-orchestrator`, the key in `sonar-project.properties`.
The server is the Community Edition, which has no branch or pull request analysis: every analysis of a
project key is an analysis of that project's one branch.

### What is decided

These choices were made with the human. The rest of this section builds on them and does not reopen them.

- **Placement in the loop (pattern A).** Builder → quality gate → (on failure, back to the Builder) →
  Reviewer. The gate runs after *every* Builder turn: in round 1, and in each fix round after
  `CHANGES_REQUESTED`.

  ```
  Builder ──build-n.md──▶ quality gate ──GATE: OK──▶ Reviewer ──review-n.md──▶ …
     ▲                         │
     └──── quality-n-q.md ─────┘  gate failed, at most --max-quality-rounds analyses per build round
  ```

- **Trigger.** Jenkins runs the analysis, not a local `sonar-scanner`.
- **Pushing.** Each quality round pushes a snapshot of the working tree to a throwaway ref on `origin` and
  deletes the ref afterwards. The run's branch, its single commit and `_publish` (`orchestrator.py:720-746`)
  stay as they are.
- **One SonarQube project per run.** Community Edition has no branch analysis, so each run analyses into its
  own project, `py-ai-agents-orchestrator-<key>`, where `<key>` is the run's six-hex key. The key goes to
  Jenkins as a parameter. The run analyses its base commit first, so that "new code" is only the Builder's
  change, and it removes the project at the end of the run.
- **What goes to the Builder.** Only the issues on lines the change touched, taken from `git diff -U0`
  against `state.base`. Added to them are the failing tests and the coverage on new code from the Jenkins
  Test stage.
- **Pass condition.** The SonarQube quality gate status is `OK`.
- **When the rounds run out.** After `--max-quality-rounds` rounds (default 3), the change goes to the
  Reviewer anyway, and the Reviewer's prompt names the last quality file as unresolved findings.

A quality round is one analysis. With the default of 3, a build round has at most three analyses and two
Builder fix turns between them. If the third analysis fails too, its findings go to the Reviewer unresolved,
just as the last review's findings stay unresolved once `--max-rounds` is reached (`orchestrator.py:705`).
Each build round has its own budget of quality rounds: the first build round, and each one after
`CHANGES_REQUESTED`.

### Snapshotting the uncommitted tree

The Builder leaves its work uncommitted (`orchestrator.py:102`), and the Reviewer diffs it against `base`
(`orchestrator.py:928-932`). Jenkins can only fetch commits. So each quality round needs a commit that holds
the working tree, untracked files included, without moving HEAD, the index or the branch: `_publish` later
turns the same tree into the run's single commit (`orchestrator.py:732-735`).

- (a) `git stash create`. It leaves HEAD and the index alone. But its commit leaves out untracked files, and
  it is a merge of HEAD and an index commit, which confuses SonarQube's blame.
- (b) Commit on the run's branch, push, then `git reset --soft` back. That moves HEAD and writes the reflog.
  A crash between the commit and the reset leaves an extra commit that `_publish` would build on. Under
  `--no-pr` the commit would land on the human's own branch.
- (c) A temporary index. Copy the index to a temporary file, run `GIT_INDEX_FILE=<tmp> git add -A` and
  `git write-tree`, then `git commit-tree <tree> -p <state.base>`. The result is a commit that no ref points
  to yet.

**Recommendation:** (c). It touches nothing the Builder, the Reviewer or `_publish` sees: the real index is
only copied, and no ref moves. `git add -A` honours `.gitignore`, and `_claim` writes
`.orchestrator/.gitignore` with `*` (`orchestrator.py:610-614`). So the run's own files stay out, and the
snapshot holds what `_publish`'s `git add --all` would commit (`orchestrator.py:733`). Its parent is
`state.base`, not HEAD, so it is the same change the Reviewer diffs, even if the human has committed in the
meantime. Copying the real index, rather than starting from an empty one, keeps git's stat cache, so only
changed files are hashed.

The steps run as one `sh -c` script through `Host.run`, as `Host.write` does (`orchestrator.py:335-339`).
That way the temporary index is removed even when a step fails, and `GIT_INDEX_FILE` reaches git, which
`Host.git` (`orchestrator.py:354-355`) cannot pass. The new method is
`Host.snapshot(cwd, parent, message) -> sha`:

```sh
cd -- "$1" || exit 1
t=$(mktemp) || exit 1
trap 'rm -f -- "$t"' EXIT
cp -- "$(git rev-parse --git-path index)" "$t" &&
    GIT_INDEX_FILE=$t git add -A &&
    tree=$(GIT_INDEX_FILE=$t git write-tree) &&
    git commit-tree "$tree" -p "$2" -m "$3"
```

The push then goes through `Host.git`, with the network timeout that `_publish`'s push uses
(`orchestrator.py:46`, `:738`):

```
git push --quiet --force origin <sha>:refs/heads/orchestrator-ci/<key>-<n>-q<q>
```

Git cannot infer a ref type for a bare sha, so the destination spells out `refs/heads/`. `--force` replaces a
ref that an earlier, failed attempt at the same round left behind. The name adds the build round `<n>` to
the decided `orchestrator-ci/<key>-q<N>`: quality round numbers restart in every build round, and a resume
looks Jenkins builds up by their ref (see "Run state and resume"), so the name must be unique within the
run. The base commit goes to `orchestrator-ci/<key>-base`. `commit-tree` needs a git identity, as
`_publish`'s commit already does (`orchestrator.py:734-735`).

### The Jenkins job's interface

Three string parameters, each empty by default:

| Parameter | Set by the orchestrator to | When empty (ordinary builds) |
|---|---|---|
| `GIT_REF` | `orchestrator-ci/<key>-base` or `orchestrator-ci/<key>-<n>-q<q>` | `checkout scm`, which is what the declarative default checkout does today |
| `SONAR_PROJECT_KEY` | `py-ai-agents-orchestrator-<key>` | the key in `sonar-project.properties`, and the gate fails the build as today |
| `SONAR_PROJECT_VERSION` | `base` or `change` (see "The Community Edition baseline") | no `sonar.projectVersion` |

The orchestrator only ever sends values made of `[A-Za-z0-9._/-]`.

**Which job.**

- (a) Add the parameters to the job that builds the repository today, and trigger that job.
- (b) A separate Pipeline job, such as `ai-agents-orchestrator-quality`, that reads the same Jenkinsfile from
  `main`.

**Recommendation:** (b), with one Jenkinsfile for both jobs. If today's job is a multibranch job,
`buildWithParameters` addresses a branch job that already exists, and a throwaway ref only becomes one after
a branch scan has found it. A plain Pipeline job takes any ref as a parameter. A job that loads its
Jenkinsfile from `main` also means that a Builder that edits the `Jenkinsfile` cannot change how its own
change is judged. The quality builds stay out of the ordinary job's history. The job's name is what
`--quality-gate` takes (see "CLI").

**The gate result.** `waitForQualityGate abortPipeline: true` (`Jenkinsfile:45`) fails the build when the
gate fails, and that looks the same as a failed Install or Test stage. The orchestrator reads the gate from
SonarQube itself, but it has to know whether an analysis happened at all, and where to find it.

- (a) Keep `abortPipeline: true`, and tell the cases apart by whether the build archived `report-task.txt`.
- (b) In quality builds, `abortPipeline: false`, and mark the build UNSTABLE when the gate is not `OK`.
  Ordinary builds still fail as today. In both, archive `report-task.txt` from the analysis stage, before
  `cleanWs()` (`Jenkinsfile:51-55`) removes it.

**Recommendation:** (b). The archived file holds the `ceTaskId` that the orchestrator follows (see "Getting
the result"), whatever the build's result. UNSTABLE against FAILURE tells a human looking at Jenkins whether
the change was analysed and failed the gate or never got that far. Ordinary builds keep calling `error` on a
failed gate, which is what `abortPipeline: true` does.

**Failing tests.** A failing test fails the Test stage (`Jenkinsfile:18`), and the analysis never runs.

- (a) Keep that: the Builder hears about the failing tests first and about SonarQube's findings in a later
  round.
- (b) In quality builds only, wrap pytest in `catchError`, so the analysis runs anyway and the build ends
  UNSTABLE. The quality file then carries both the failing tests and the issues.

**Recommendation:** (b). One round carries all the findings, and there are only three rounds. The cost is
that the decided pass condition is the gate status alone, and SonarQube's gate does not count failing tests.
So a change whose tests fail but whose gate is `OK` goes on to the Reviewer. The quality file named in the
Reviewer's prompt lists the failing tests, and the Reviewer runs the tests anyway (`orchestrator.py:120`).
Whether failing tests should also fail a round is under "Open questions".

The proposed Jenkinsfile. It is a proposal only and is not applied:

```groovy
pipeline {
    agent any

    // All empty in an ordinary build, which then behaves as before.
    parameters {
        string(name: 'GIT_REF', defaultValue: '', description: 'Branch to build, e.g. orchestrator-ci/cd3492-1-q2')
        string(name: 'SONAR_PROJECT_KEY', defaultValue: '', description: 'SonarQube project key; empty uses sonar-project.properties')
        string(name: 'SONAR_PROJECT_VERSION', defaultValue: '', description: 'sonar.projectVersion; empty leaves it unset')
    }

    options {
        skipDefaultCheckout()
    }

    environment {
        UV_NO_PROGRESS = '1'
        COVERAGE_FILE  = '.coverage'
    }

    stages {
        stage('Checkout') {
            steps {
                script {
                    if (params.GIT_REF) {
                        checkout([$class: 'GitSCM',
                                  branches: [[name: "refs/heads/${params.GIT_REF}"]],
                                  userRemoteConfigs: scm.userRemoteConfigs])
                    } else {
                        checkout scm
                    }
                }
            }
        }

        stage('Install') {
            steps {
                sh 'uv sync --frozen'
            }
        }

        stage('Test') {
            steps {
                script {
                    def pytest = 'uv run pytest --cov=. --cov-report=xml:coverage.xml --junitxml=test-results.xml'
                    if (params.SONAR_PROJECT_KEY) {
                        // A quality build is analysed even when tests fail, so the Builder hears about both at once.
                        catchError(buildResult: 'UNSTABLE', stageResult: 'FAILURE') {
                            sh pytest
                        }
                    } else {
                        sh pytest
                    }
                }
            }
            post {
                always {
                    junit 'test-results.xml'
                }
            }
        }

        stage('SonarQube Analysis') {
            steps {
                withSonarQubeEnv('Sonarqube') {
                    script {
                        def scannerHome = tool 'sonarqube-scanner'
                        // The parameters reach the shell as environment variables, never as Groovy-built shell code.
                        // The name is set too, or the analysis would rename the per-run project after sonar-project.properties.
                        sh """
                            ${scannerHome}/bin/sonar-scanner \
                              -Dsonar.python.coverage.reportPaths=coverage.xml \
                              -Dsonar.python.xunit.reportPath=test-results.xml \
                              \${SONAR_PROJECT_KEY:+-Dsonar.projectKey=\$SONAR_PROJECT_KEY -Dsonar.projectName=\$SONAR_PROJECT_KEY} \
                              \${SONAR_PROJECT_VERSION:+-Dsonar.projectVersion=\$SONAR_PROJECT_VERSION}
                        """
                        if (params.SONAR_PROJECT_KEY) {
                            // Holds the ceTaskId the orchestrator follows; cleanWs() would delete it.
                            archiveArtifacts artifacts: '.scannerwork/report-task.txt'
                        }
                    }
                }
            }
        }

        stage('Quality Gate') {
            steps {
                timeout(time: 5, unit: 'MINUTES') {
                    script {
                        def gate = waitForQualityGate abortPipeline: false
                        if (gate.status != 'OK') {
                            if (params.SONAR_PROJECT_KEY) {
                                unstable "Quality gate ${gate.status}"
                            } else {
                                error "Quality gate ${gate.status}"
                            }
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            cleanWs()
        }
    }
}
```

An ordinary build runs the same commands with the same result as today. It shows one more stage,
`Checkout`, in place of the implicit `Declarative: Checkout SCM`. Jenkins learns a Jenkinsfile's
`parameters` only by running it, so the new job needs one build before it accepts `buildWithParameters`. The
preflight checks for the parameters and says so (see "Edge cases").

### Getting the result

- (a) Poll the Jenkins build until it ends, take `ceTaskId` from the archived `report-task.txt`, and follow
  that SonarQube task to its analysis.
- (b) Parse the console log for the scanner's "More about the report processing at …/api/ce/task?id=…"
  line. That line is the scanner's output for humans, and it changes between versions.
- (c) Skip Jenkins, and ask SonarQube for the per-run project's latest task with `/api/ce/activity`. That
  cannot tell this round's task from an earlier one, which matters on resume, and it misses a build that
  failed before the analysis.

**Recommendation:** (a). Each link of the chain is an id that one response hands to the next request, and
each can be saved, so a resume picks the chain up where it broke. The orchestrator does not rely on the
Jenkins Quality Gate stage: it follows the SonarQube task itself. So a stage that timed out waiting for
SonarQube's webhook still leaves a usable `report-task.txt`, even though the expired `timeout` ends the build
ABORTED (see "Once a build ends" below).

The endpoints, in the order the orchestrator calls them. `<job>` is the job's path, `job/a/job/b` for job
`b` in folder `a`. Jenkins requests use HTTP Basic with `JENKINS_USER:JENKINS_TOKEN`, and requests made with
an API token need no CSRF crumb. SonarQube requests use HTTP Basic with `SONAR_TOKEN` as the user name and an
empty password, which every version accepts.

Preflight, before the interview:

1. `GET {JENKINS_URL}/<job>/api/json?tree=property[parameterDefinitions[name]]`: the job exists and has the
   three parameters.
2. `GET {SONAR_HOST_URL}/api/authentication/validate`: `{"valid": true}`.

Once per run, at its first quality round:

3. `GET {SONAR_HOST_URL}/api/qualitygates/get_by_project?project=py-ai-agents-orchestrator`: the gate of the
   repository's own project.
4. `POST {SONAR_HOST_URL}/api/projects/create` with `project` and `name` both `py-ai-agents-orchestrator-<key>`.
5. `POST {SONAR_HOST_URL}/api/qualitygates/select` with `gateName` from call 3 and
   `projectKey=py-ai-agents-orchestrator-<key>`.
6. `POST {SONAR_HOST_URL}/api/new_code_periods/set` with `project=py-ai-agents-orchestrator-<key>` and
   `type=PREVIOUS_VERSION`.

For each analysis, first the base and then every quality round:

7. `POST {JENKINS_URL}/<job>/buildWithParameters` with `GIT_REF`, `SONAR_PROJECT_KEY` and
   `SONAR_PROJECT_VERSION`. The answer is 201, with the queue item's URL in `Location`.
8. `GET {Location}api/json`, until `executable.number` is set, or `cancelled` is true.
9. `GET {JENKINS_URL}/<job>/<number>/api/json?tree=building,result`, until `building` is false.
10. `GET {JENKINS_URL}/<job>/<number>/artifact/.scannerwork/report-task.txt`: the `ceTaskId=` line, read
    whatever the build's result. A 404 means there was no analysis; what that means depends on the result
    (see "Once a build ends" below).
11. `GET {SONAR_HOST_URL}/api/ce/task?id=<ceTaskId>`, until `task.status` is `SUCCESS`, `FAILED` or
    `CANCELED`. A successful task gives `task.analysisId`.
12. `GET {SONAR_HOST_URL}/api/qualitygates/project_status?analysisId=<analysisId>`: `projectStatus.status`
    and its conditions.
13. `GET {SONAR_HOST_URL}/api/issues/search?components=py-ai-agents-orchestrator-<key>&resolved=false&ps=500&p=<page>`,
    page by page: every open issue. The orchestrator then keeps the ones on changed lines.
14. `GET {SONAR_HOST_URL}/api/measures/component?component=py-ai-agents-orchestrator-<key>&metricKeys=new_coverage,new_lines_to_cover,new_uncovered_lines`.
15. `GET {JENKINS_URL}/<job>/<number>/testReport/api/json?tree=failCount,suites[cases[className,name,status,errorDetails]]`:
    the cases whose status is `FAILED` or `REGRESSION`. A 404 means no test results were recorded.
16. `GET {JENKINS_URL}/<job>/<number>/consoleText`, only for a build that ended before the analysis with no
    failing test to show for it. Its last 60 lines go into the quality file.

For the base analysis the chain stops at call 11: only its success matters, not its gate or its issues. For
a build that ended before the analysis it goes from call 10 to calls 15 and 16. Calls 13 and 14 read the
project's current state rather than one analysis. That is this round's analysis, because only this run
analyses into the project, one build at a time.

**Once a build ends.** One rule decides what follows call 9, in a live run and in a resumed one alike. The
archived file decides first, and the build's result only when the file is missing:

| `report-task.txt` (call 10) | Build result | Then |
|---|---|---|
| present | any, ABORTED included | Follow the SonarQube task (call 11). The analysis happened; a Quality Gate stage that timed out, which Jenkins ends ABORTED, does not change that. |
| missing | ABORTED, or the queue item was cancelled | Nobody judged the change: someone stopped the build, or Jenkins did. The run fails with `ci` cut back to its `ref` and `sha`, so the next resume triggers a new build of the same snapshot. The Builder is not given a fix turn. |
| missing | any other | The build failed before the analysis. In a quality round that is `GATE: BUILD_FAILED`, with calls 15 and 16; for the base analysis the run fails (see "Edge cases"). |

At the end of a run that reaches `done`:

17. `POST {SONAR_HOST_URL}/api/projects/delete` with `project=py-ai-agents-orchestrator-<key>`.

The issues kept from call 13 are those on the lines that `git diff -U0 <state.base> <snapshot>` marks as
added, read from each hunk's `+start,count`. The decided `git diff -U0` against `state.base` is diffed
against the snapshot, not the working tree, for two reasons. The snapshot holds the untracked files, which a
diff of the working tree leaves out, so a new file counts as touched in full. And the snapshot is exactly
what Jenkins analysed. An issue without a line, on a file the change touched, is kept too.

### Who makes the HTTP calls, and where the credentials live

- (a) The orchestrator process, with `urllib.request`. The orchestrator stays stdlib-only
  (`pyproject.toml:9-11`), and `urllib` is part of the standard library.
- (b) `curl` through `Host` on the agents' machine, next to the git push.

**Recommendation:** (a). `Host.run` and `Host.check` put the whole command line into their error messages
(`orchestrator.py:316`, `:322`), and a failed run records its error in `state.json` (`orchestrator.py:620-627`).
So a token on curl's command line would end up there, besides showing in `ps` on the agents' machine. Feeding
curl its config on stdin, as `Host.write` feeds its text (`orchestrator.py:335-339`), avoids that, but it
adds an SSH round trip to every poll and a shell layer to every test. A small `CI` class over an injected
`urlopen` can be tested the way `Herdr` and `Host` are, with a fake in place of `subprocess.run`
(`orchestrator.py:156`, `:304`).

The credentials are environment variables of the orchestrator process: `JENKINS_URL`, `JENKINS_USER`,
`JENKINS_TOKEN`, `SONAR_HOST_URL` and `SONAR_TOKEN`. `main` reads them once and hands them to `CI`. The
preflight names any that are missing. They never reach `state.json`, a handoff file or the pull request body:

- `RunState` saves the job name, the project key, the queue item's URL, the build number and the CE task id.
  None of these is a secret. The two base URLs are not saved; a resume reads them from its own environment.
- The tokens travel only in the `Authorization` header, never in a URL. `CI`'s errors name the method, the
  URL and the HTTP status, nothing else, so `_release` (`orchestrator.py:620-627`) cannot record a token.
- Text copied from Jenkins and SonarQube into a quality file, which can reach the pull request body, has each
  occurrence of either token replaced with `****` first. Jenkins already masks the credentials it knows of in
  its console; this catches the ones it does not.

**With `--machine`.** The HTTP calls come from the machine the orchestrator runs on, and the environment
variables are read there. The git work (the snapshot and the pushes to `origin`) runs on the agents' machine
through `Host`, as the push in `_publish` does today. The two sides meet only at `origin`, which Jenkins
fetches from. The agents' machine therefore needs no access to Jenkins, and the orchestrator's machine needs
no git access, but it must reach `JENKINS_URL` and `SONAR_HOST_URL` (see "Open questions").

### The Community Edition baseline

SonarQube decides what is new code by the project's new code definition. Community Edition has one branch
per project, so it cannot compare a branch with `main`. The run gets the same effect from its own project:

1. The orchestrator creates the per-run project with the new code definition "previous version" (call 6).
2. Jenkins analyses the base commit with `sonar.projectVersion=base`.
3. Jenkins analyses every quality round of every build round with `sonar.projectVersion=change`.

With "previous version", the new code period starts at the analysis of the version before the current one.
The first quality round changes the version from `base` to `change`, so the base analysis becomes the
baseline. Later rounds keep `change`, so the baseline stays at the base analysis, and each round measures the
whole change against the base, not only what the last fix turn touched. A line is new when its SCM date is
later than the baseline. The Builder's lines are blamed to the snapshot commit, which is younger than the
base analysis, and every other line to an older commit. Issue tracking carries the base's issues forward with
their old dates, so they do not count as new either.

- (a) The version baseline above.
- (b) New code definition "specific analysis", set to the base analysis once it has finished. That names the
  baseline exactly, but SonarQube documents the setting at branch level, and whether Community Edition takes
  it for a project's one branch needs checking.
- (c) No base analysis. The first quality round is then the project's first analysis, for which SonarQube
  reports no new code at all, and every gate condition on new code passes.

**Recommendation:** (a), with (b) as the fallback if a check against a live SonarQube shows that the version
baseline does not hold across rounds (see "Open questions"). (c) shows why the base analysis is not
optional.

SonarQube ignores the coverage and duplication conditions while a change has fewer than 20 new lines. The
quality file reports the coverage on new code regardless, so the Builder sees it even when the gate does not
judge it.

**Creating the project.**

- (a) Let the base analysis create the project, then set its gate and new code definition. The token in
  Jenkins's `Sonarqube` server then needs the global Create Projects permission, and a missing permission
  shows only as a failed Jenkins build.
- (b) The orchestrator creates and configures it with calls 3–6, before the base analysis.

**Recommendation:** (b). The project is set up before anything is analysed into it, and a permission problem
fails on a SonarQube call that names it. The gate is copied from the repository's own project, so the change
is judged by the same rules as an ordinary build. A new project would otherwise get the instance's default
gate.

**Deleting the project.**

- (a) Delete it when the run ends, however it ends.
- (b) Delete it when the run reaches `done`, and keep it when the run fails.

**Recommendation:** (b). A failed run keeps its workspace and its branch (`orchestrator.py:537-540`) so that
the human can see what happened, and the project belongs with them: its analyses explain the last quality
file. A resume also needs the project and its baseline. The run deletes the project (call 17) once it
reaches `done`, after the pull request is open, together with its throwaway refs. A failed run that is never
resumed leaves its project behind. The `close` item in the backlog (section 4) is where that clean-up
belongs.

### Handoff files and prompts

The files of build round n:

| File | Written by | When |
|---|---|---|
| `build-<n>.md` | Builder | its first turn in round n, as today (`orchestrator.py:455-456`) |
| `quality-<n>-<q>.md` | orchestrator | quality round q of build round n |
| `build-<n>-q<q>.md` | Builder | its answer to `quality-<n>-<q>.md` |
| `review-<n>.md` | Reviewer | as today (`orchestrator.py:458-459`) |

Round 1 with the default of three quality rounds, failing twice: `build-1.md`, `quality-1-1.md`,
`build-1-q1.md`, `quality-1-2.md`, `build-1-q2.md`, `quality-1-3.md`, `review-1.md`.

- (a) The names above.
- (b) Number every Builder report in one sequence: `build-1.md`, `build-2.md` and so on, whichever kind of
  turn wrote it.

**Recommendation:** (a). Every basename is unique within the run, which `state.prompted` depends on
(`orchestrator.py:424-425`). `_turn` compares `prompted` with the basename of the file it waits for
(`orchestrator.py:776`, `:787-800`), so a name reused for a second turn would make a resume take the second
turn as already prompted. Both options give unique names, but (b) breaks the meaning `build-<n>.md` has
everywhere today: the round's report, as `REBUILD_NOTE` lists it (`orchestrator.py:717`) and `_publish` reads
it (`orchestrator.py:739`). With (a), a fix report sits next to the quality file it answers.

**The quality file is a handoff file too.** The orchestrator writes it in a single write once the round's
result is complete. Its first line is the result: `GATE: OK`, `GATE: ERROR` (SonarQube's gate failed) or
`GATE: BUILD_FAILED` (the build ended before the analysis). On resume it plays the part that a role's file
plays in `_turn` (`orchestrator.py:777-780`): if it exists, the round is decided, and nothing is called
again. A first line that is none of the three is rejected, as a review without a verdict is
(`orchestrator.py:702-703`).

An example:

```markdown
GATE: ERROR

Quality round 2 of 3 in build round 1. Jenkins build #57 of ai-agents-orchestrator-quality analysed snapshot
9c41e07, the working tree on base 3f7f439, into SonarQube project py-ai-agents-orchestrator-cd3492.

### Failed conditions

- Coverage on new code is 61.5%; the gate requires at least 80%.
- Maintainability rating on new code is C; the gate requires A.

### Issues on changed lines

1. limiter.py:42 python:S3776 CRITICAL: Refactor this function to reduce its Cognitive Complexity from 19 to the 15 allowed.
2. limiter.py:57 python:S1192 MINOR: Define a constant instead of duplicating this literal "tokens" 3 times.
3. tests/test_limiter.py:12 python:S1481 MINOR: Remove the unused local variable "bucket".

4 more open issues are on lines this change did not touch, and are left out.

### Failing tests

1. tests/test_limiter.py::TestBucket::test_refill_after_burst: AssertionError: 4 != 5

### Coverage on new code

61.5%: 15 of the 39 new lines to cover are not covered.
```

The issues are sorted by file and line, and numbered, so that the Builder can answer them by number as it
answers review findings (`orchestrator.py:113`). The severity is SonarQube's `severity` field. An issue
without a line shows its file alone. The file lists at most 50 issues, then counts the rest. A
`BUILD_FAILED` file has no conditions, issues or coverage; when no test failed either, it carries the last
60 lines of the console instead. When the change touches `sonar-project.properties` or the `Jenkinsfile`,
the file says so in a line of its own, because those decide how the change is analysed.

The new prompts, next to `FIX_PROMPT` and `REVIEW_PROMPT` (`orchestrator.py:109-126`):

```python
QUALITY_FIX_PROMPT = """\
Your change failed the quality gate: a Jenkins build and a SonarQube analysis of a snapshot of the working tree. \
The findings are in {quality_path}; they cover only the lines your change touched. \
Fix each finding, or explain in your report why it is wrong or outside the spec. Re-run the verification. \
Do not commit, push or switch branches. As your last step, write a new report to {report_path} in a single write, \
in the same shape as before, answering each finding by its number."""

# Appended to the Reviewer's prompt in a run with the quality gate.
QUALITY_PASSED_NOTE = """\
A Jenkins build and a SonarQube analysis of this change passed the quality gate; the result is in {quality_path}."""

QUALITY_UNRESOLVED_NOTE = """\
The change still failed the quality gate after {rounds} rounds, so the findings in {quality_path} are unresolved. \
Check each one, and request changes for those that are defects under the spec."""
```

- The Builder's fix turn goes through `_turn` (`orchestrator.py:767-834`) unchanged, with
  `QUALITY_FIX_PROMPT`. Its text for a fresh session is `BUILD_PROMPT`, the `REBUILD_NOTE` with every earlier
  report of the run, and `QUALITY_FIX_PROMPT`, as `_build_prompts` builds the fresh text for `FIX_PROMPT`
  (`orchestrator.py:710-718`).
- `_review_prompts` (`orchestrator.py:755-765`) appends one of the two notes to both `REVIEW_PROMPT` and
  `RECHECK_PROMPT`, with the round's last quality file. Its `report_path` becomes the Builder's last report
  of the round, `build-<n>-q<q>.md` when there were fix turns. Every report is in the same shape as the first
  (`orchestrator.py:112-113`), so the last one is complete.
- `_publish` reads that last report too, instead of `build_path(s.round)` (`orchestrator.py:739`), and
  `pr_body` (`orchestrator.py:496-508`) adds the last quality file as one more collapsed section.

### Run state and resume

- (a) A new phase, `quality`, between `build` and `review`, with a `quality_round` counter that also tells
  the Builder's first turn of a round from its fix turns.
- (b) No new phase: the gate as a step at the end of the `build` phase, with its progress kept only in
  sub-state fields.

**Recommendation:** (a). `list` then shows `quality` (`orchestrator.py:1110`), which tells the human that the
run is waiting on Jenkins, not on an agent. And resume keeps deciding by phase first, as it does today.

The phases of build round n, as `_build_and_review` (`orchestrator.py:690-708`) moves through them:

| Saved state | Meaning | Next |
|---|---|---|
| `build`, `quality_round` 0 | the Builder's first turn, `build-<n>.md` | with the gate, `quality` with q = 1; without it, `review`, as today |
| `quality`, `quality_round` q | the analysis behind `quality-<n>-<q>.md` | `OK`, or q ≥ `max_quality_rounds`: `review`; otherwise `build` with the same q |
| `build`, `quality_round` q ≥ 1 | the Builder answering `quality-<n>-<q>.md` with `build-<n>-q<q>.md` | `quality` with q + 1 |
| `review` | as today | `CHANGES_REQUESTED`: `build`, round n + 1, `quality_round` 0 |

New `RunState` fields, next to those in `orchestrator.py:398-428`:

| Field | Written | Purpose |
|---|---|---|
| `quality_job: str \| None` | when the run starts, and by `resume --quality-gate` or `--no-quality-gate` | The Jenkins job. None turns the gate off, so a `state.json` from before this change keeps today's flow. |
| `max_quality_rounds: int` | when the run starts | The run setting, saved like `max_rounds`. |
| `quality_round: int` | with each phase change | As in the table above. |
| `quality_project: str \| None` | once calls 4–6 have succeeded | The per-run project key. Set means the project exists and is configured, so a resume does not create it again, and `done` knows what to delete. |
| `quality_baseline: str \| None` | once the base analysis has succeeded | Its analysis id. Set means the base needs no second analysis. |
| `ci: dict \| None` | step by step during an analysis | The analysis in flight: `{"ref", "sha", "queue_url", "build", "ce_task"}`. Each key is saved as soon as it is known. The dict is cleared when the quality file is written, or for the base analysis when `quality_baseline` is. |

A resume in phase `quality` takes the furthest step the saved state allows:

1. `quality-<n>-<q>.md` exists: act on its first line, and call nothing.
2. `quality_project` is not set: calls 3–6. `quality_baseline` is not set: the base analysis, through the
   steps below.
3. `ci.ce_task` is set: poll it (call 11) and go on from there.
4. `ci.build` is set: poll that build (call 9), and once it has ended apply "Once a build ends" exactly as a
   live run does. An ABORTED build that archived `report-task.txt` is followed to its SonarQube task, not
   built again.
5. `ci.queue_url` is set: poll the queue item (call 8). Jenkins forgets a queue item a few minutes after its
   build starts, so on a 404 the build is looked up by its `GIT_REF` among the job's latest builds, with
   `GET {JENKINS_URL}/<job>/api/json?tree=builds[number,actions[parameters[name,value]]]{0,20}`.
6. `ci.ref` is set but `ci.queue_url` is not: the process may have died between call 7 and saving its
   answer.
7. Otherwise: snapshot, push, and trigger the build.

For step 6:

- (a) Trigger again, accepting a second build, as section 1 accepts a duplicate prompt.
- (b) First look for a build with this `GIT_REF`, in the queue (`GET {JENKINS_URL}/queue/api/json`) and
  among the job's latest builds as in step 5, and trigger only if there is none.

**Recommendation:** (b). The ref names one round of one run, so the lookup cannot adopt a wrong build, and it
costs two requests, once per resume. A duplicate build costs a full Jenkins run and a second analysis
waiting in SonarQube's queue.

**Heartbeat.** Every polling loop calls `self._heartbeat()` (`orchestrator.py:956-967`) between polls, as
`_turn` does (`orchestrator.py:830`), so a run that waits for Jenkins does not look stale. It polls every
`CI_POLL_SECONDS = 10`. Each HTTP request is bounded by `HTTP_TIMEOUT = 30` seconds, far below
`STALE_SECONDS` (`orchestrator.py:44`), so one hung request cannot make a live run look stale.

**Timeout for each quality round.**

- (a) Reuse `--timeout`, the limit on a Builder or Reviewer turn.
- (b) A constant, `QUALITY_TIMEOUT = 1200`, next to `HEARTBEAT_SECONDS` and `STALE_SECONDS`.

**Recommendation:** (b). The wait covers the Jenkins queue, the build and SonarQube's processing, which
depend on the Jenkins and SonarQube instance, not on the agents or the task. The deadline runs on the
injected `clock`, so `FakeClock` drives it in tests. When it passes, the run fails with an error that names
the step it was waiting on, such as "Jenkins build #57 still running after 1200s", and keeps `ci`. A resume
then polls the same build with a fresh timeout, as section 1 gives a resumed turn a fresh timeout. The base
analysis has a timeout of the same length.

### CLI

```bash
python orchestrator.py run TASK --quality-gate JOB [--max-quality-rounds N] ...
python orchestrator.py resume RUN [--quality-gate JOB | --no-quality-gate] [--max-quality-rounds N] ...
```

- (a) `--quality-gate JOB`: one flag names the Jenkins job and turns the gate on.
- (b) A plain `--quality-gate` switch, with the job in a `JENKINS_JOB` environment variable next to the
  credentials.
- (c) No flag: the gate is on whenever `JENKINS_URL` is set.

**Recommendation:** (a). The job belongs to the project, as `--cwd` does, while the credentials belong to the
machine. Saved in `state.json`, the job no longer depends on a resume's environment. (c) would turn the gate
on, without the human asking, in every run started from a shell that holds Jenkins credentials for something
else.

- `--quality-gate JOB` and `--max-quality-rounds N` join the `settings` parent parser
  (`orchestrator.py:988-998`), so that both `run` and `resume` take them. An unset flag is None, as for the
  other settings (`orchestrator.py:987`). `--max-quality-rounds` below 1 is refused, as `--max-rounds` is
  (`orchestrator.py:1019-1020`). On `run`, `--max-quality-rounds` without `--quality-gate` is refused, since
  it would do nothing.
- `--no-quality-gate` is for `resume` only, like `--force` (`orchestrator.py:1012`). It turns the gate off
  for the rest of the run, for when Jenkins is gone for good. A run resumed with it in phase `quality` goes
  on to `review`. If a quality file of that round exists, the Reviewer's prompt names it as unresolved. The
  per-run project is then left to the human, since the credentials may be missing too.
- `main` (`orchestrator.py:1170-1176`) puts `quality_job` and `max_quality_rounds` on the new state, and
  `Workflow.__init__` takes and saves them like the other settings (`orchestrator.py:554-559`).
- `resumable_state` (`orchestrator.py:1127-1148`) applies the two settings flags as it applies
  `--max-rounds` (`orchestrator.py:1136-1137`). In phase `quality`, it refuses a `--max-quality-rounds`
  below the saved `quality_round`, as it refuses a `--max-rounds` below the saved round
  (`orchestrator.py:1143-1145`). In phase `build` with `quality_round` q ≥ 1, the Builder is already
  answering quality round q, and its answer leads to round q + 1, so `resume` refuses any value up to and
  including q. The stop condition is still `q ≥ max_quality_rounds`, not equality, so a limit lowered past
  the current round can only end the rounds early, never let them run on, as `n >= s.max_rounds` guards the
  review rounds (`orchestrator.py:705`).
- A `state.json` without the new fields loads with `quality_job` None (`RunState.from_dict`,
  `orchestrator.py:430-440`), so a run started before them resumes without a gate, as today. `resume
  --quality-gate JOB` on such a run, or on any run started without the gate, turns it on from the next
  Builder turn, and runs the preflight first.

### Edge cases

The one choice that runs through most of the cases: what to do when Jenkins or SonarQube fails, rather than
the change.

- (a) Fail the run at once, keeping `ci`, so that `resume` carries on once the service is back.
- (b) Skip the gate for that round, and send the change to the Reviewer with a note.
- (c) Retry until the round's timeout, then fail as in (a).

**Recommendation:** (c). A failed poll is logged and repeated at the next poll, as a failed heartbeat is
(`orchestrator.py:963-967`), so a Jenkins restart costs nothing. Once `QUALITY_TIMEOUT` has passed, the run
fails and can be resumed. (b) would let a change past a gate the human asked for, without anyone deciding
so. `resume --no-quality-gate` is that decision, made by the human.

- **Jenkins or SonarQube unreachable.** At the start, the preflight fails the run before the interview, as
  `_check_repo` does for a repository that cannot take a pull request (`orchestrator.py:573-574`,
  `:629-639`), and names the URL it could not reach. Later, as recommended above.
- **The job missing**, or lacking the three parameters. The preflight fails with "no Jenkins job JOB", or
  with "Jenkins job JOB has no parameters GIT_REF, SONAR_PROJECT_KEY, SONAR_PROJECT_VERSION; build it once
  with the new Jenkinsfile". A job deleted during the run makes call 7 answer 404. That fails the run at
  once, because no retry brings the job back.
- **A build that fails before the analysis.** uv sync fails, pytest crashes before writing its results, or the
  scanner fails: the build ends without `report-task.txt` (call 10), and not ABORTED. In a quality round that
  is `GATE: BUILD_FAILED`, and the Builder gets the failing tests, or the console tail when there are none.
  For the base analysis it is fatal. Without a baseline the gate cannot work, and the base is not the
  Builder's to fix. The run fails with "base commit 3f7f439 does not build: Jenkins build #N", and the human
  fixes the base or resumes with `--no-quality-gate`. A SonarQube task that ends `FAILED` or `CANCELED` fails
  the run too, since its analysis is lost. As with an aborted build, `ci` is cut back to its `ref` and `sha`,
  so the next resume triggers a new build instead of polling the lost task again.
- **A build stopped by hand, or by a timeout.** A build ABORTED before the analysis is not the change's
  fault, so it fails the run rather than giving the Builder a fix turn, and the next resume builds the same
  snapshot again. A build ABORTED after the analysis, typically by the Quality Gate stage's 5-minute
  `timeout` while SonarQube's queue is long, has archived `report-task.txt`, and the round goes on from
  the SonarQube task. Both follow "Once a build ends", whether the run was resumed or not.
- **Cleanup when the run fails.** Nothing is deleted: not the per-run project, and not the ref of the round
  in flight, which Jenkins may not have fetched yet. The refs of finished rounds are deleted as each round
  ends, with `git push origin --delete` through `Host.git`. At `done`, the run deletes its project and every
  `orchestrator-ci/<key>-*` ref that `git ls-remote origin` still lists, including any left by failed
  attempts. A failed clean-up is logged and does not fail the run, as with closing the workspace
  (`orchestrator.py:748-753`).
- **Concurrent runs.** Each run has its own key, so its own project and refs. They share only the Jenkins
  executors and SonarQube's queue, where Community Edition processes one analysis at a time. Concurrent runs
  wait longer, and `QUALITY_TIMEOUT` has to allow for that. Ordinary builds keep analysing into
  `py-ai-agents-orchestrator`, which no run touches.
- **Two runs in one checkout.** A snapshot takes every change in the working tree, so another run's
  uncommitted files would be analysed as this run's change. Pull-request runs check for a clean tree
  (`_require_clean`, `orchestrator.py:641-646`) before the interview and again before they branch
  (`orchestrator.py:639`, `:686`), which refuses a second run once the first Builder has changed files. A
  `--no-pr` run checks neither (`orchestrator.py:573-574`, `:583-584`). With the gate, it must: the
  preflight calls `_require_clean` for a `--no-pr` run too, and the step from spec to build calls it again,
  as `_switch_to_branch` does. One window stays open for both kinds of run: two runs whose interviews both
  end before either Builder has written a file pass both checks. Closing it needs one checkout per run, the
  `--worktree` item in the backlog.
- **`--no-pr` runs.** The gate needs no run branch: the snapshot's parent is `state.base`, and the throwaway
  ref is a branch of its own. It does need a commit and an `origin` the agents' machine can push to, which a
  `--no-pr` run does not otherwise need. `_check_repo` runs only for pull requests (`orchestrator.py:573-574`),
  so the preflight checks for both itself, with `git remote get-url origin`, and for a clean tree, as under
  "Two runs in one checkout". Clean-up at `done` is the same as with a pull request.
- **A project that is not a git repository.** There is no base and so no snapshot. `--quality-gate` is
  refused at the start with "the quality gate needs a git repository with a commit and an origin remote",
  and `resume --quality-gate` is refused for a run whose `base` is None.
- **The Builder changed nothing.** The snapshot's tree is the base's, the analysis finds no new code, and the
  gate passes. `_publish` already refuses an empty change (`orchestrator.py:736-737`).
- **The Builder edits the `Jenkinsfile` or `sonar-project.properties`.** The job runs the `Jenkinsfile` from
  `main`, so an edit there does not change the gate. The scanner reads `sonar-project.properties` from the
  snapshot, so a `sonar.exclusions` added there can hide code. The quality file names either edit, and the
  Reviewer judges it.

### Test plan

Reuse the fakes in `tests/test_orchestrator.py:25-197`:

- `FakeHost` (`:25-67`) gets `snapshot(cwd, parent, message)`, which records the call and returns a sha
  derived from the `changed` files. Its `git` (`:52-63`) records `push` and `ls-remote`.
- `FakeHerdr` (`:70-152`) plays the Builder as today, with one script entry per turn, fix turns included.
  `build_turn` (`:198-202`) and `review_turn` (`:205-206`) cover the usual turns.
- `FakeClock` (`:167-180`) drives the polls and the round's timeout, and its hooks move the fake Jenkins on.
- `make_workflow` (`:183-192`) takes `ci=` and `quality_job=`; `saved_run` (`:748-755`) takes the new fields.
- New: `FakeCI`, the fake HTTP layer in place of `urlopen`. It serves scripted responses by method and path.
  It keeps a list of builds, each with its `GIT_REF`, its result, an optional `report-task.txt`, its test
  cases and its console text, and a list of SonarQube tasks. It records every request with its headers, and
  it can raise `URLError` for a set number of calls.

Tests:

1. **The gate passes the first time.** The requests follow the order in "Getting the result": the preflight,
   the project set-up, the base build with `SONAR_PROJECT_VERSION=base`, then the round's build with
   `change`. `quality-1-1.md` starts with `GATE: OK`, the Reviewer's prompt names it, and the run approves.
   Both refs are pushed and deleted, and the project is deleted at `done`.
2. **The gate fails, then passes.** `quality-1-1.md` starts with `GATE: ERROR` and numbers the scripted
   issues with file:line. The Builder's second prompt is `QUALITY_FIX_PROMPT`, naming `quality-1-1.md` and
   `build-1-q1.md`. `quality-1-2.md` is `GATE: OK`, and the Reviewer's prompt names `build-1-q1.md` as the
   report.
3. **The rounds run out.** With `max_quality_rounds=2` and both gates failing, there are exactly two quality
   builds and one fix turn. The change goes to the Reviewer, whose prompt contains the
   `QUALITY_UNRESOLVED_NOTE` text and the path of `quality-1-2.md`.
4. **A later build round.** After `CHANGES_REQUESTED` in round 1, round 2 writes `quality-2-1.md` from a
   build of `orchestrator-ci/a1b2c3-2-q1`, and the base is analysed only once in the whole run.
5. **Issues on changed lines only.** A scripted diff, with issues on touched lines, on untouched lines, in an
   untracked new file and without a line: only the touched ones are numbered, and the rest are counted.
6. **A resume while a Jenkins build is pending.** `saved_run` in phase `quality`, with `ci` holding a
   `queue_url` and a `build`. There is no `buildWithParameters` request and no push, only polls of that
   build, and the round's quality file comes from it.
7. As test 6, with only `ci.ce_task` saved: no Jenkins request other than the test report.
8. As test 6, with `ci.ref` but no `queue_url`, and a build with that `GIT_REF` among the job's builds: that
   build is adopted, and nothing is triggered.
9. A resume in phase `quality` with `quality-1-1.md` present: no request for that round.
10. **Jenkins unreachable at the start.** `FakeCI` raises `URLError` on the preflight. The run fails before
    the Spec Collector is prompted, and the error names `JENKINS_URL`.
11. **Jenkins unreachable in mid-round.** The polls fail. If a hook brings Jenkins back, the run carries on;
    if not, it fails after `QUALITY_TIMEOUT` and keeps `ci`. In both cases neither token appears in
    `state.json` or in any written file.
12. **A build that fails before the analysis.** Without `report-task.txt`, the quality file is
    `GATE: BUILD_FAILED` with the failing tests, or with the console tail when there are none, and the
    Builder gets a fix turn. The same failure on the base build fails the run and names the base.
13. **Heartbeat.** During a Jenkins build that runs for 20 minutes, `state.json` is written about once per
    `HEARTBEAT_SECONDS`, as in `tests/test_orchestrator.py:1047-1064`.
14. **No gate.** Without `quality_job`, the existing workflow tests pass unchanged, and `FakeCI` sees no
    request.
15. **The snapshot on a real repository**, in a temporary directory, like
    `tests/test_orchestrator.py:658-666`: a modified file, a deleted file, an untracked file and an ignored
    file. The snapshot's tree matches the working tree for the first three and lacks the ignored one, its
    parent is the base, and HEAD, the index, the branch and `git status --porcelain` are the same before and
    after.
16. **`changed_lines(diff)`**, a pure function: added, modified, deleted and renamed files, hunks with and
    without a count, and pure deletions (`+start,0`).
17. **The quality file**, a pure function of the gate, the issues, the tests and the coverage: the numbering,
    the cap of 50 issues and its count, `BUILD_FAILED` with the console tail, the line about a changed
    `Jenkinsfile`, and tokens replaced with `****`.
18. **`CI` over a fake `urlopen`**, a `MagicMock` as in `tests/test_orchestrator.py:608-666`: the Basic
    `Authorization` headers, no token in any URL or error message, the queue item taken from `Location`, and
    a 404 returned as None where the chain expects one.
19. **CLI**, next to `tests/test_orchestrator.py:669-745` and `:1229-1262`. `--max-quality-rounds 0`, and
    `--max-quality-rounds` without `--quality-gate` on `run`, exit. `resume` overrides the saved job and
    rounds, refuses rounds below the saved `quality_round` in phase `quality` and up to it in phase `build`,
    and clears the job with `--no-quality-gate`. A
    `state.json` without the new fields resumes with the gate off. Missing environment variables are named.
20. **`--no-pr` with the gate.** The run pushes its refs without a run branch. Without an `origin` remote,
    or with uncommitted changes in the tree, before the interview or at its end, the run is refused.
21. **Not a git repository** with the gate (`FakeHost(head=None)`, as in
    `tests/test_orchestrator.py:369-379`): refused before the interview.
22. **Cleanup.** A run that fails keeps its project and the ref in flight. A run that reaches `done` deletes
    the project and the refs that `ls-remote` lists. A delete that fails is logged, and the run still ends
    with its verdict.
23. **Once a build ends**, each row of the rule, run twice: live, and as a resume with `ci.build` saved. The
    outcome is the same both times. ABORTED with `report-task.txt`: the round goes on to the SonarQube task,
    and no second build is triggered. ABORTED without it: the run fails, `ci` holds only `ref` and `sha`, and
    the Builder is not prompted. FAILURE without it: `GATE: BUILD_FAILED` and a fix turn.
24. **The rounds stop.** A saved state in phase `quality` whose `quality_round` is already above
    `max_quality_rounds` goes to `review` after its analysis, however the gate ends.

### Open questions

- **The version baseline.** Does "previous version" keep the base analysis as the baseline across several
  analyses of version `change`, as described above? Check it on the live SonarQube with three analyses, and
  read the new code period's date with `/api/measures/component?...&additionalFields=period` after each. If
  it does not hold, use "specific analysis", if Community Edition accepts it.
- **Blame in Jenkins.** New lines need blame, which a shallow clone lacks: the scanner then warns, and
  reports no new lines. The job must clone with history down to the base. Check its clone options.
- **Permissions.** Can the user behind `SONAR_TOKEN` create, configure and delete projects (Create Projects,
  and Administer on the projects it created, through the permission template)? Can the token of Jenkins's
  `Sonarqube` server analyse into a project it did not create?
- **API details of the installed version.** `/api/issues/search` takes `components` in recent versions and
  `componentKeys` in older ones. Recent versions also report software-quality impacts beside the `severity`
  the quality file uses. Check both against the installed SonarQube.
- **Branch discovery.** If the job that builds the repository today is a multibranch job, or a GitHub
  webhook triggers builds of pushed branches, each `orchestrator-ci/*` ref would also be built as a branch
  and analysed into `py-ai-agents-orchestrator`. That prefix must be excluded there.
- **Reachability under `--machine`.** The design assumes that the orchestrator's machine can reach Jenkins
  and SonarQube. If only the agents' machine can, the fallback is option (b) of "Who makes the HTTP calls",
  with curl reading its config, tokens included, from stdin.
- **Failing tests with a passing gate.** Quality builds analyse the change even when tests fail, so a change
  whose tests fail but whose gate is `OK` goes on to the Reviewer, as the decided pass condition says.
  Should failing tests fail a round as well? That would change a decision made with the human, so it is only
  raised here.
- Should `QUALITY_TIMEOUT` and `CI_POLL_SECONDS` be flags? As with `HEARTBEAT_SECONDS` in section 2, the
  design starts with constants. Revisit if a busy Jenkins makes rounds time out.

## 4. Backlog

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

## 5. Housekeeping

`.gitignore:12` ignores `.orchestrator_context.json`. The orchestrator before the herdr rewrite persisted its
context to that file, and 25e756b removed that code. Nothing reads or writes the file now, so the entry looks
like a leftover. It is recorded here only, to be removed in a separate change.
