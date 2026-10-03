# Roadmap

Ideas for what comes next, in no particular order. The designs behind what has shipped are in
[design.md](design.md).

- **`close <run-id>`**: close the run's herdr workspace from its saved `workspace_id`, instead of by hand. Only
  runs that failed or used `--no-pr` need it; the others close their workspace after the pull request.
- **`show <run-id>`**: print a run's state, its handoff file paths, the latest verdict and the open findings of
  the last review. `list` gives only one line per run.
- **`--spec FILE`**: skip the interview when a spec already exists, by copying the file into the run as
  `spec.md`.
- **Per-role permission mode**: give each role its own permission mode. Today one `--permission-mode` applies to
  every role.
- **Overridable prompts**: load the role prompts from `.orchestrator/prompts/*.md` when those files exist, with
  the same placeholders.
- **`--worktree`**: the Builder works in a git worktree per run (`herdr worktree create`), for a clean base and
  parallel runs.
- **Re-prompt the Reviewer once on a malformed review** instead of stopping the run.
- **Builder `BLOCKED` escalation**: a build report that starts with `BLOCKED:` notifies the human and pauses the
  run, instead of going to the Reviewer.
- **`summary.md` and per-turn timings**: write `summary.md` at the end of a run, and record in `state.json` when
  each turn started and ended.
- **Quality gate through Jenkins and SonarQube**: after each Builder turn, analyse a snapshot of the change in
  Jenkins and SonarQube, and send the findings back to the Builder before the Reviewer sees it. The design is in
  [quality-gate.md](quality-gate.md#orchestrator-loop-design-not-implemented).

## Known issues

- **A resume can publish from the wrong branch.** The run switches to its branch only in the spec phase. If you
  check out another branch before resuming a later phase, the commit lands there, and the push is of the run's
  branch.
