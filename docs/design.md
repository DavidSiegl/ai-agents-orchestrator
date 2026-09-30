# Design

How resuming, stale-run detection, notifications and remote machines work, and why. The
[README](../README.md) covers installing and running the orchestrator.

## Resuming a run

The agents and the handoff files outlive the orchestrator: Ctrl-C, a dropped SSH session or a crash stops only
the process that drives them. `resume` picks the run up where `state.json` says it stopped:

- A handoff file written while no orchestrator was watching is taken as it is, without prompting the role again.
- A role whose agent still runs is reused. If it was already prompted for its current file, it is only waited on.
- An agent that exited is relaunched in its pane with `claude --resume`, keeping its conversation. When its
  session is gone, a fresh one starts with a prompt that points it at the earlier rounds, and a Spec Collector's
  interview starts over.
- A closed pane is split again from a surviving one, and a closed workspace is replaced by a new one.
- The diff under review stays against the commit the run started from, even if you commit in between.
- A run stopped while opening its pull request does not commit twice or open a second pull request.
- An empty handoff file or a review without a verdict stops the resume with the file's name: fix it, or delete
  it to have the role write it again.

Resuming a finished run does nothing but print its verdict.

## Stale runs

While a run is going, its orchestrator rewrites `state.json` at least once a minute and records its host and
pid there. `list` shows each unfinished run as `running` or `stale`. A run is stale when `state.json` is more
than five minutes old, measured by the clock of the machine that holds it, or when its orchestrator ran on this
host and its pid is gone. A stale run's line ends with the `resume` command for it. `resume` refuses a run
that is still running unless you pass `--force`, and an orchestrator whose run is taken over stops at its next
save. Ctrl-C is recorded as the error `interrupted`.

## When the orchestrator needs you

A role's turn ends when it writes its handoff file, not when herdr reports it `idle` or `done`: Claude Code
ends a turn while a background task it started still runs, and resumes when the task finishes. So the
orchestrator polls for the file, and sends a herdr notification when a role:

- is **blocked** on a permission prompt, a question, or a startup dialog such as folder trust;
- has sat **idle for 3 minutes** without writing its file (not the Spec Collector, which waits on you by design).

Notifications appear in the herdr where the orchestrator runs. Answer in the named pane, and the run continues.

## Running the agents on another machine

Steps 1–3 run on the machine you start the orchestrator from.

1. **Save the machine** in herdr: `herdr machine add --label <machine> <ssh-target>`.
2. **Check it** with `herdr machine list`. `--machine` matches the label or the id, and the machine must be
   enabled.
3. **Check that SSH works non-interactively.** File and `git` access runs over `ssh -o BatchMode=yes`, so
   `ssh -o BatchMode=yes <ssh-target> true` must succeed without a password, passphrase or host-key prompt.
4. **Prepare the remote machine** as in the README's Requirements: herdr with the Claude integration, `claude`,
   the project's git checkout with an `origin` it can push to, and `gh` logged in unless you use `--no-pr`.

With `--machine`, every herdr command is forwarded with `herdr --machine`. The orchestrator can also run on the
remote machine itself: copy `orchestrator.py` there and run it with the system `python3` (3.13+) from a herdr
pane, without `--machine`.

## Design decisions

### Resume

- **`run()` is re-entrant.** A new run is a resume from phase `spec` with no workspace, and every step first
  checks whether it was already done. With one code path, the workflow tests cover every step a resume takes,
  and the two cannot drift apart.
- **The handoff file is checked before prompting, and a `prompted` marker is saved after the prompt.** The file
  check catches work handed over while no orchestrator watched. The marker keeps a live Builder in mid-turn from
  getting its prompt twice; dying between the prompt and the save costs one duplicate prompt, which the role
  answers by writing the same file again.
- **An exited agent is relaunched with `claude --resume`, falling back to a fresh session.** For the Spec
  Collector the conversation is the interview; for the others it saves re-reading the spec and earlier rounds.
  The fallback keeps resume working when the session is gone.
- **A missing pane or workspace is recreated.** The handoff files are the whole contract between roles, so a
  new pane or workspace loses nothing a role needs. Failing would leave you at the dead end resume removes.
- **`base` is read from `state.json`.** It is computed once, before the first build. Recomputing it would diff
  against today's HEAD, and if you had committed the Builder's work the Reviewer would see an empty diff.
- **A resumed turn gets a fresh timeout.** The downtime is not the role's fault, and a saved deadline would
  often expire at once.
- **An invalid handoff file stops the resume and is named.** Resume does not judge a role's output. Stopping
  also clears `prompted`, so once you delete the file the role is prompted again instead of waited on.
- **`done` is a no-op.** Resume is idempotent, so a script can call it without checking the phase first. A run
  in the `publish` phase opens no workspace and skips the commit and the pull request that `state.json` shows
  are already made.
- **Run settings are saved and `resume` flags override them.** Whoever resumes rarely remembers the original
  flags, and an override is how they grant one more round. The saved settings include the per-role models, and
  a `--max-rounds` below the saved round is refused.
- **The saved `--machine` is not compared with the one given.** The same herdr can be reached with `--machine`
  from elsewhere or without it from a pane on that machine; finding the saved workspace is what proves the right
  herdr is addressed.

### Stale-run detection

- **The heartbeat is written every 60 s, and a run goes stale after 300 s.** Without a heartbeat a long turn
  or interview looks dead. One write a minute is noise next to the polling, and five missed beats ride out a
  slow SSH hop without false alarms. The wait on a startup dialog is cut into 60 s steps so it keeps beating,
  and a failed beat is logged, not fatal.
- **`state.json` is written atomically**, to a temporary file that is then renamed, so a `list` running at the
  same time never reads a half-written file.
- **The owner is checked before every write, for takeover.** When `resume --force` has taken the run, the old
  orchestrator stops without saving. Checking before every write, not only before a heartbeat, keeps a phase
  save from overwriting the new owner's state.
- **Staleness is measured by the age of `state.json`'s mtime on the machine that holds it.** That uses one
  clock, so there is no skew between machines, and it works for state files that predate the heartbeat. A pid
  can be checked only on the owner's host, and counts only once a beat is late, because host names can collide
  and pids are reused.
- **Ctrl-C is recorded as the error `interrupted`.** `list` already shows errors, and resume already treats a
  run with an error as resumable, so no separate status field is needed.
