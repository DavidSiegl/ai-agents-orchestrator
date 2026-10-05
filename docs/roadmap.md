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
- **`resume --quality-gate JOB` and `resume --no-quality-gate`**: turn the [quality gate](quality-gate.md) on
  for a run started without it, from its next Builder turn and after the preflight, or off for good, a run in
  `quality` then going on to `review` with any quality file named as unresolved. Today the gate is fixed when
  the run starts.

## Workflows

Only `default` ships ([Workflows](architecture.md#workflows)); others are workflow files, and
[`examples/workflows/`](../examples/workflows) has `quick` and `tdd`. These engine gaps, by letter (G, runs without a verdict, is closed), stand
between the engine and the candidates below:

- **A. Run inputs**: seed a handoff file at `run` (`--spec FILE` as `spec.md`), or name a base ref, branch or
  pull request to work on.
- **B. Human approval gates** between steps: stop until the human approves a step's file, or sends it back.
- **C. Parallel steps**, and a step that waits for all of them.
- **D. Richer verdicts**: more than one verdict loop, a verdict that picks the step to go back to or ends the run
  early, other verdict words.
- **E. Aggregated verdicts**: one decision from several verdict files.
- **F. Other endings** than a pull request or `--no-pr`: a document as the result, comments on an existing pull
  request.
- **H. Runs without an editing step** have no base commit or branch, so they need `--no-pr`.
- **I. Orchestrator checks**: a step without an agent that runs a command and judges it, such as "the new tests
  fail at the base".
- **J. More than one quality-gated step.**
- **K. Workflow choice**: mid-run, by the Spec Collector or the human, or with `resume --workflow`; a default
  workflow other than `default` without passing `--workflow` each time; workflow files that extend another
  workflow file, not only the default's steps.

| Workflow | Roles | Handoff files | Gaps |
|---|---|---|---|
| **quick** | Builder, Reviewer | `spec.md` from `--spec FILE`, or the task as the contract; `build-{n}.md`, `review-{n}.md` | A for `--spec`; none without it |
| **tdd** | Spec Collector, Test Writer, Builder, Reviewer | `spec.md`, `tests.md`, `build-{n}.md`, `review-{n}.md` | none for the shape; I to prove the tests fail first; D to send a review to the Test Writer |
| **plan** | Spec Collector, Planner, Builder, Reviewer | `spec.md`, `plan.md` (approved by the human), `build-{n}.md`, `review-{n}.md` | B; D for a plan sent back (`plan-{n}.md`) |
| **bugfix** | Spec Collector or none, Reproducer, Builder, Reviewer | `spec.md` or the task, `repro.md` with a failing test, `build-{n}.md`, `review-{n}.md` | D for "cannot reproduce"; I for the test failing before and passing after |
| **review-only** | one or more Reviewers | `review.md` on a given branch or pull request | A, F, H; C and E for several reviewers |
| **research / spike** | Spec Collector, Researcher, Critic | `question.md`, `findings-{n}.md`, `critique-{n}.md` | F, H; D for the critic's verdict |
| **panel review** | Spec Collector, Builder, two or three Reviewers (e.g. correctness, security) | `spec.md`, `build-{n}.md`, `review-<lens>-{n}.md` | C, E |
| **solo** | Builder, with or without a Spec Collector | `spec.md` (optional), `build.md` | none |

## Known issues

- **A resume can publish from the wrong branch.** The run switches to its branch only in the spec phase. If you
  check out another branch before resuming a later phase, the commit lands there, and the push is of the run's
  branch.
