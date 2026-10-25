# Roadmap

Ideas for what comes next, in no particular order. The designs behind what has shipped are in
[design.md](design.md).

- **Per-role permission mode**: give each role its own permission mode. Today one `--permission-mode` applies to
  every role.
- **A reactive UI**: a successor to the Tk window that shows every run's progress and handoff files live, lets
  you answer the agents in it, and starts and watches several runs from one window. The options and the
  decision, a Tauri app, are in [ui-options.md](ui-options.md); its first, read-only step is in [`ui/`](../ui).

## Workflows

Only `default` ships ([Workflows](architecture.md#workflows)); others are workflow files, and
[`examples/workflows/`](../examples/workflows) has `spec-build-review`, `quick` and `product-team`. These engine gaps, by letter (G, runs without a verdict, is closed), stand
between the engine and the candidates below:

- **A. Run inputs**: `--spec FILE` seeds `spec.md` for a workflow whose first step writes the spec (a contract
  step), but not yet for one without, such as `quick`; to come: name a base ref, branch or pull request to work
  on.
- **B. Human approval gates** between steps: stop until the human approves a step's file, or sends it back.
- **C. Parallel steps**, and a step that waits for all of them.
- **D. Richer verdicts**: more than one verdict loop, a verdict that picks the step to go back to or ends the run
  early, other verdict words.
- **E. Aggregated verdicts**: one decision from several verdict files.
- **F. Other endings** than a pull request or `--no-pr`: a document as the result, comments on an existing pull
  request.
- **H. Runs without an editing step** have no base commit or branch, so they need `--no-pr`.
- **I. Orchestrator checks**: a step without an agent that runs a command and judges it, such as "the new tests
  fail at the base". A first, fixed form ships: each quality round runs `orchestrator_checks` after the build's gate,
  each an `OrchestratorCheck` that judges the run's change with its spec and workflow ([The orchestrator's
  checks](quality-gate.md#the-orchestrators-checks)). To come: checks a workflow chooses, and outside the quality
  round.
- **J. More than one quality-gated step.**
- **K. Workflow choice**: mid-run, by the Spec Collector or the human, or with `resume --workflow`; a default
  workflow other than `default` without passing `--workflow` each time; workflow files that extend another
  workflow file, not only the default's steps.

| Workflow | Roles | Handoff files | Gaps |
|---|---|---|---|
| **quick** | Builder, Reviewer | `spec.md` from `--spec FILE`, or the task as the contract; `build-{n}.md`, `review-{n}.md` | A for `--spec`, which works only for workflows with a contract step so far; none without it |
| **tdd** | Spec Collector, Test Writer, Builder, Reviewer | `spec.md`, `tests.md`, `build-{n}.md`, `review-{n}.md` | none for the shape; I to prove the tests fail first; D to send a review to the Test Writer |
| **product-team** (`examples/workflows/product-team.toml`) | Product Manager, Architect, QA Engineer, Engineer, Tech Lead | `spec.md` (requirements), `design.md`, `tests.md`, `build-{n}.md`, `review-{n}.md` | B for the human to approve the design before QA starts; D for the Product Manager's acceptance after the Tech Lead's review |
| **plan** | Spec Collector, Planner, Builder, Reviewer | `spec.md`, `plan.md` (approved by the human), `build-{n}.md`, `review-{n}.md` | B; D for a plan sent back (`plan-{n}.md`) |
| **bugfix** | Spec Collector or none, Reproducer, Builder, Reviewer | `spec.md` or the task, `repro.md` with a failing test, `build-{n}.md`, `review-{n}.md` | D for "cannot reproduce"; I for the test failing before and passing after |
| **review-only** | one or more Reviewers | `review.md` on a given branch or pull request | A, F, H; C and E for several reviewers |
| **research / spike** | Spec Collector, Researcher, Critic | `question.md`, `findings-{n}.md`, `critique-{n}.md` | F, H; D for the critic's verdict |
| **panel review** | Spec Collector, Builder, two or three Reviewers (e.g. correctness, security) | `spec.md`, `build-{n}.md`, `review-<lens>-{n}.md` | C, E |
| **solo** | Builder, with or without a Spec Collector | `spec.md` (optional), `build.md` | none |
