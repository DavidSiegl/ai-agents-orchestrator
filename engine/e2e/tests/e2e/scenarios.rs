//! The scenarios: each a function that scripts a World and runs the engine in it, listed in main.rs's scenarios!.
//!
//! A scenario says only what happens: what each role's agent writes when prompted, what gh answers, and which engine
//! commands run, in which order, with which stopped midway. What the engine did in return is the world's record,
//! compared with the recording by main.rs.

use octoflow_e2e::object;
use octoflow_engine::py::json::{Json, Map};

use crate::world::{checkout, World};

// -- What the agents write ------------------------------------------------------

const SPEC: &str = "# Add a subtract function\n\nAdd `subtract(a, b)` to `src/calc.py`, returning a - b, with a test.\n";
const CALC_WITH_SUBTRACT: &str = "def add(a, b):\n    \"\"\"The sum of a and b.\"\"\"\n    return a + b\n\n\n\
                                  def subtract(a, b):\n    \"\"\"a minus b.\"\"\"\n    return a - b\n";
const TEST_WITH_SUBTRACT: &str = "from calc import add, subtract\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n\
                                  def test_subtract():\n    assert subtract(3, 2) == 1\n";
const TASK: &str = "Add a subtract function";

/// An agent's action (octoflow_e2e::fakes says what one holds), built up from `writes`.
struct Action(Map<Json>);

impl Action {
    /// Also make these changes to the working tree, path to text, before writing the action's file.
    fn edit(mut self, files: &[(&str, &str)]) -> Action {
        let edits: Map<Json> = files.iter().map(|(path, text)| (*path, Json::from(*text))).collect();
        self.0.insert("edit", Json::Object(edits));
        self
    }

    /// Do it only once the world releases hold.
    fn hold(mut self, hold: &str) -> Action {
        self.0.insert("hold", Json::from(hold));
        self
    }

    /// Exit the agent afterwards.
    fn exit(mut self) -> Action {
        self.0.insert("exit", Json::Bool(true));
        self
    }
}

impl From<Action> for Json {
    fn from(action: Action) -> Json {
        Json::Object(action.0)
    }
}

/// The action of an agent that writes text to file in the run directory.
fn writes(file: &str, text: &str) -> Action {
    Action(Map::from_iter([("write", object([(file, Json::from(text))])), ("edit", object([]))]))
}

/// An agent that exits without doing anything.
fn exits() -> Json {
    object([("exit", Json::Bool(true))])
}

fn spec_written() -> Action {
    writes("spec.md", SPEC)
}

/// The Builder's report of round n, answering quality round q when q is not 0, having added subtract and its test.
fn built(n: u32, q: u32) -> Action {
    let file = if q == 0 { format!("build-{n}.md") } else { format!("build-{n}-q{q}.md") };
    writes(&file, &format!("Added subtract and its test (round {n}). `pytest` passes.\n"))
        .edit(&[("src/calc.py", CALC_WITH_SUBTRACT), ("tests/test_calc.py", TEST_WITH_SUBTRACT)])
}

fn reviewed(n: u32, verdict: &str, findings: &str) -> Action {
    writes(&format!("review-{n}.md"), &format!("VERDICT: {verdict}\n\n{findings}\n"))
}

fn approved(n: u32) -> Action {
    reviewed(n, "APPROVE", "None.")
}

/// The path of the example workflow name in this checkout.
fn example(name: &str) -> String {
    checkout().join("examples/workflows").join(name).to_string_lossy().into_owned()
}

/// The default workflow's three roles, approved in round 1.
fn approved_roles(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [built(1, 0)]);
    w.agent("review", [approved(1)]);
}

// -- The default workflow ------------------------------------------------------------

/// The default workflow, approved in its first round, ending in a pull request.
pub fn default_approved(w: &mut World) {
    approved_roles(w);
    w.run(&["run", "--", TASK]);
}

/// The Reviewer requests changes in round 1 and approves round 2's fix.
pub fn default_changes_then_approved(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [built(1, 0).edit(&[("src/calc.py", CALC_WITH_SUBTRACT)]), built(2, 0)]);
    w.agent("review", [reviewed(1, "CHANGES_REQUESTED", "1. tests/test_calc.py:1: subtract has no test."), approved(2)]);
    w.run(&["run", "--", TASK]);
}

/// Changes requested in every round until --max-rounds: a draft pull request, exit status 3.
pub fn default_max_rounds(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [built(1, 0), built(2, 0)]);
    w.agent(
        "review",
        (1..=2).map(|n| reviewed(n, "CHANGES_REQUESTED", &format!("1. src/calc.py:5: still wrong in round {n}."))),
    );
    w.run(&["run", "--max-rounds", "2", "--", TASK]);
}

/// The Builder's first report is BLOCKED: the human is asked, and the Builder's next report ends the turn.
pub fn blocked_build_answered(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [writes("build-1.md", "BLOCKED: Should subtract accept floats too?\n"), built(1, 0)]);
    w.agent("review", [approved(1)]);
    w.run(&["run", "--", TASK]);
}

/// A review without a VERDICT line is moved aside and asked for once more.
pub fn verdict_retried(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [built(1, 0)]);
    w.agent("review", [writes("review-1.md", "Looks right to me.\n"), approved(1)]);
    w.run(&["run", "--", TASK]);
}

/// run --no-pr: the change stays uncommitted in the checkout and the workspace stays open.
pub fn no_pr_run(w: &mut World) {
    approved_roles(w);
    w.run(&["run", "--no-pr", "--", TASK]);
}

/// The Builder exits without writing its report: the run fails, keeping its workspace and branch.
pub fn agent_exits(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [exits()]);
    w.run(&["run", "--", TASK]);
}

// -- The other workflows ------------------------------------------------------------

/// examples/workflows/quick.toml by --workflow-file: Builder and Reviewer, the task as the contract.
pub fn workflow_quick(w: &mut World) {
    w.agent("build", [built(1, 0)]);
    w.agent("review", [approved(1)]);
    w.run(&["run", "--workflow-file", &example("quick.toml"), "--", TASK]);
}

/// examples/workflows/spec-build-review.toml installed as a workflow file and run by --workflow, with --role-model and
/// --agent.
pub fn workflow_spec_build_review(w: &mut World) {
    w.write("config/ai-agents-orchestrator/workflows/sbr.toml", &read(&example("spec-build-review.toml")));
    approved_roles(w);
    w.run(&[
        "run", "--workflow", "sbr", "--agent", "codex", "--role-model", "review=opus", "--permission-mode", "acceptEdits",
        "--", TASK,
    ]);
}

const TESTS_WEAKENED: &str = "from calc import add, subtract\n\n\ndef test_add():\n    assert add(1, 2) == 3\n";

/// examples/workflows/product-team.toml, approved in round 1, its Engineer making engineer_edits.
fn product_team(w: &mut World, engineer_edits: &[(&str, &str)]) {
    w.agent("spec", [spec_written()]);
    w.agent("design", [writes("design.md", "# Design\n\nAdd subtract beside add in src/calc.py.\n")]);
    w.agent(
        "tests",
        [writes("tests.md", "Acceptance test: tests/test_calc.py::test_subtract.\n")
            .edit(&[("tests/test_calc.py", TEST_WITH_SUBTRACT)])],
    );
    w.agent("build", [built(1, 0).edit(engineer_edits)]);
    w.agent("review", [approved(1)]);
    w.run(&["run", "--workflow-file", &example("product-team.toml"), "--", TASK]);
}

/// examples/workflows/product-team.toml: five roles, the QA Engineer's tests locked, the Engineer leaving them.
pub fn workflow_product_team(w: &mut World) {
    product_team(w, &[("src/calc.py", CALC_WITH_SUBTRACT)]);
}

/// The product team's Engineer weakens the locked acceptance test: it is restored, its diff kept, and the Tech Lead
/// told.
pub fn locks_restored(w: &mut World) {
    product_team(w, &[("src/calc.py", CALC_WITH_SUBTRACT), ("tests/test_calc.py", TESTS_WEAKENED)]);
}

const PROOF_WORKFLOW: &str = r#"description = "Builder <-> Reviewer, the Reviewer's findings backed by proofs"
proof_command = "sh {proofs}"

[roles]
build = "Builder"
review = "Reviewer"

[[steps]]
use = "build"
prompt = "Implement {task} in {cwd}. As your last step, write a report to {build_path}."

[[steps]]
use = "review"
prompt = """Review {change} against the task, {task}. Back each finding with a proof in {proofs_dir}. \
As your last step, write {review_path}, its first line VERDICT: {approve} or VERDICT: {changes}."""
again = "Review the change again; the Builder's report is {build_path}. Write {review_path} as before."
fresh_note = "You are a fresh session; the earlier reviews are {earlier_review_paths}."
"#;
const PROOF: &str = "grep -q 'return a - b' src/calc.py || exit 1\n";

/// A workflow with a proof_command: round 1's review backs its finding with a proof that fails, the proof is run again
/// before round 2's review and passes.
pub fn proofs_run(w: &mut World) {
    let path = w.write("workflows/proofs.toml", PROOF_WORKFLOW);
    let calc_wrong = CALC_WITH_SUBTRACT.replace("return a - b", "return b - a");
    w.agent("build", [built(1, 0).edit(&[("src/calc.py", &calc_wrong)]), built(2, 0)]);
    let review = "VERDICT: CHANGES_REQUESTED\n\n1. src/calc.py:7: the sign is wrong.\n";
    let proof = object([(
        "write",
        object([("proofs-1/test_1_sign.sh", Json::from(PROOF)), ("review-1.md", Json::from(review))]),
    )]);
    w.agent("review", [proof, approved(2).into()]);
    w.run(&["run", "--workflow-file", &path, "--", TASK]);
}

// -- Where the run works -------------------------------------------------------------

/// run --worktree: the run builds in a worktree of its own from origin's main, which is removed once the pull request is
/// open; the checkout itself, dirty, is left alone.
pub fn worktree_run(w: &mut World) {
    w.write("project/notes.txt", "the human's own uncommitted work\n");
    approved_roles(w);
    w.run(&["run", "--worktree", "--", TASK]);
}

/// run --machine: herdr is addressed by the machine, and every file and git command goes over ssh, which the fake runs
/// here.
pub fn machine_run(w: &mut World) {
    w.machine("box");
    approved_roles(w);
    let project = w.project.to_string_lossy().into_owned();
    w.run(&["run", "--machine", "box", "--cwd", &project, "--", TASK]);
}

/// origin's main changes the same lines while the run reviews: publishing finds the conflict, pushes the branch without
/// the merge and opens a draft pull request, exit status 4.
pub fn publish_with_conflict(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [built(1, 0)]);
    w.agent("review", [approved(1).hold("review")]);
    let run = w.start(&["run", "--", TASK], true);
    w.wait_reached("review");
    let upstream = "def add(a, b):\n    \"\"\"a plus b.\"\"\"\n    return b + a\n\n\ndef subtract(a, b):\n    return -(b - a)\n";
    w.push_to_origin(&[("src/calc.py", upstream)], "Add subtract upstream");
    w.release("review");
    w.invocation(run).wait();
}

// -- Stopped and resumed ------------------------------------------------------------

/// Run the engine with args until it reaches hold, kill it there, then let hold go and resume the run with --force,
/// which a run whose orchestrator died a moment ago needs: its heartbeat is still fresh.
fn killed_and_resumed(w: &mut World, hold: &str, args: &[&str]) {
    let run = w.start(args, true);
    w.wait_reached(hold);
    w.invocation(run).kill();
    w.release(hold);
    let key = w.run_key(1);
    w.run(&["resume", "--force", &key]);
}

/// Killed while the Spec Collector interviews; resumed, the interview's file is found written.
pub fn kill_in_spec(w: &mut World) {
    w.agent("spec", [spec_written().hold("spec")]);
    w.agent("build", [built(1, 0)]);
    w.agent("review", [approved(1)]);
    killed_and_resumed(w, "spec", &["run", "--", TASK]);
}

/// Killed while the Builder works.
pub fn kill_in_build(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [built(1, 0).hold("build")]);
    w.agent("review", [approved(1)]);
    killed_and_resumed(w, "build", &["run", "--", TASK]);
}

/// Killed while the Reviewer works, in round 2.
pub fn kill_in_review(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [built(1, 0), built(2, 0)]);
    w.agent("review", [reviewed(1, "CHANGES_REQUESTED", "1. src/calc.py:5: no docstring."), approved(2).hold("review")]);
    killed_and_resumed(w, "review", &["run", "--", TASK]);
}

/// Killed while gh opens the pull request, after the commit and the push; resumed, publishing finishes.
pub fn kill_in_publish(w: &mut World) {
    w.hold_pr_create("publish");
    approved_roles(w);
    killed_and_resumed(w, "publish", &["run", "--", TASK]);
}

/// examples/workflows/product-team.toml killed in step hold, design or tests, and resumed.
fn product_team_killed(w: &mut World, hold: &str) {
    let held = |action: Action, step: &str| if hold == step { action.hold(step) } else { action };
    w.agent("spec", [spec_written()]);
    w.agent("design", [held(writes("design.md", "# Design\n\nAdd subtract beside add in src/calc.py.\n"), "design")]);
    let tests = writes("tests.md", "Acceptance test: tests/test_calc.py::test_subtract.\n")
        .edit(&[("tests/test_calc.py", TEST_WITH_SUBTRACT)]);
    w.agent("tests", [held(tests, "tests")]);
    w.agent("build", [built(1, 0).edit(&[("src/calc.py", CALC_WITH_SUBTRACT)])]);
    w.agent("review", [approved(1)]);
    killed_and_resumed(w, hold, &["run", "--workflow-file", &example("product-team.toml"), "--", TASK]);
}

/// The product team's run killed while the Architect works, before the run has a branch.
pub fn kill_in_design(w: &mut World) {
    product_team_killed(w, "design");
}

/// The product team's run killed while the QA Engineer writes the locked tests, its first editing step.
pub fn kill_in_tests(w: &mut World) {
    product_team_killed(w, "tests");
}

/// The Builder exits without its report and the run fails; resume restarts it in its saved session, which it is
/// prompted to continue in.
pub fn resume_after_agent_exit(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [exits(), built(1, 0).into()]);
    w.agent("review", [approved(1)]);
    w.run(&["run", "--", TASK]);
    let key = w.run_key(1);
    w.run(&["resume", &key]);
}

/// resume --force while the run's orchestrator is alive: the new one takes the run over, and the old one stops at its
/// next save without saving.
pub fn takeover(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [built(1, 0).hold("build")]);
    // Held, so that the second still owns the run when the first next looks, rather than having finished it.
    w.agent("review", [approved(1).hold("review")]);
    let first = w.start(&["run", "--", TASK], true);
    w.wait_reached("build");
    let key = w.run_key(1);
    let second = w.start(&["resume", "--force", &key], true);
    let second_pid = i128::from(w.invocation(second).pid());
    w.wait_for(
        || w.run_state(&key).get("owner").and_then(|o| o.get("pid")).and_then(Json::as_int) == Some(second_pid),
        "the second orchestrator to claim the run",
    );
    // The second has claimed it; once it waits on the Builder too, let the Builder finish.
    w.wait_for(
        || {
            w.ledger.events().iter().any(|e| {
                let by_second = e.get("by").and_then(Json::as_str) == Some("2");
                let words: Vec<&str> =
                    e.get("herdr").and_then(Json::as_array).into_iter().flatten().filter_map(Json::as_str).collect();
                by_second && words.starts_with(&["agent", "get"])
            })
        },
        "the second orchestrator to wait on the Builder",
    );
    w.release("build");
    w.invocation(first).wait();
    w.wait_reached("review");
    w.release("review");
    w.invocation(second).wait();
}

// -- The commands about runs ------------------------------------------------------------

/// A default run approved in round 1, its pull request open.
fn approved_run(w: &mut World) {
    approved_roles(w);
    w.run(&["run", "--", TASK]);
}

fn merged() -> [(&'static str, Json); 2] {
    [("state", Json::from("MERGED")), ("closedAt", Json::from("2026-01-02T00:00:00Z"))]
}

/// list, list --json with --pr-state, show and metrics, over a finished run and a failed one.
pub fn runs_listed_and_shown(w: &mut World) {
    approved_run(w);
    w.agent("spec", [exits()]);
    w.run(&["run", "--", "Add a multiply function"]);
    w.run(&["list"]);
    w.set_pr(1, &merged());
    w.run(&["list", "--json", "--pr-state"]);
    let (first, second) = (w.run_key(1), w.run_key(2));
    w.run(&["show", &first]);
    w.run(&["show", &second]);
    w.run(&["metrics"]);
}

/// prune once the pull request is merged: --dry-run first, then for real, locally and on origin; then a new run's
/// sweep, which finds nothing left.
pub fn branches_pruned(w: &mut World) {
    approved_run(w);
    w.run(&["prune"]);
    w.set_pr(1, &merged());
    w.run(&["prune", "--dry-run"]);
    w.run(&["prune"]);
    approved_roles(w);
    w.run(&["run", "--no-pr", "--", "Add a subtract function again"]);
}

/// close of a failed --worktree run: its workspace closed, its worktree and its branch, holding no commit, deleted;
/// then resume refuses it, and a second close says it is closed.
pub fn failed_run_closed(w: &mut World) {
    w.agent("spec", [spec_written()]);
    w.agent("build", [writes("build-1.md", "").edit(&[("src/calc.py", CALC_WITH_SUBTRACT)]).exit()]);
    w.run(&["run", "--worktree", "--", TASK]);
    let key = w.run_key(1);
    w.run(&["close", &key]);
    w.run(&["resume", &key]);
    w.run(&["close", &key]);
}

/// close of a finished --no-pr run: the workspace is closed and the checkout keeps the change.
pub fn finished_run_closed(w: &mut World) {
    approved_roles(w);
    w.run(&["run", "--no-pr", "--", TASK]);
    let key = w.run_key(1);
    w.run(&["close", &key]);
}

/// workflows: the list, with a workflow file and a prompt file installed; the default printed as a file; an example by
/// its path; and a workflow file that does not load.
pub fn workflows_listed(w: &mut World) {
    w.write("config/ai-agents-orchestrator/workflows/quick.toml", &read(&example("quick.toml")));
    w.write(
        "config/ai-agents-orchestrator/prompts/BUILD_PROMPT.md",
        "Build {spec_path} in {cwd}; follow the conventions. Report to {build_path}.\n",
    );
    w.run(&["workflows"]);
    w.run(&["workflows", "default"]);
    w.run(&["workflows", &example("product-team.toml")]);
    let broken = w.write("workflows/broken.toml", "[[steps]]\nuse = \"nothing\"\n");
    w.run(&["workflows", &broken]);
}

/// A run with a prompt file: the Builder gets its text, the run saves it, and the commit says so.
pub fn prompt_file_run(w: &mut World) {
    w.write(
        "config/ai-agents-orchestrator/prompts/BUILD_PROMPT.md",
        "Build what {spec_path} says, in {cwd}. As your last step, report to {build_path}.\n",
    );
    approved_run(w);
}

/// refresh after a pull request opened over conflicts: first GitHub still finds it conflicting (exit status 5), then
/// mergeable, so the warning goes from its description, it is marked ready, and the run forgets its conflicts.
pub fn conflict_refreshed(w: &mut World) {
    publish_with_conflict(w);
    w.set_pr(1, &[("mergeable", Json::from("CONFLICTING"))]);
    w.run(&["refresh"]);
    w.set_pr(1, &[("mergeable", Json::from("MERGEABLE"))]);
    let key = w.run_key(1);
    w.run(&["refresh", &key]);
    let pr = w.pr(1);
    w.note("pull request", pr);
}

// -- The other commands -----------------------------------------------------------------

/// A spec as drain reads one.
fn drain_spec(title: &str, blocked: &str, goal: &str) -> String {
    format!("# {title}\n\nStatus: ready-for-agent\n{blocked}\n## Goal\n\n{goal}\n")
}

/// drain over two specs, the second blocked by the first: --dry-run, then a run of the first, which ends in review,
/// leaving the second waiting (exit status 5); the specs' headers are noted.
pub fn specs_drained(w: &mut World) {
    let specs = [
        ("a-subtract", drain_spec(TASK, "", "Add `subtract(a, b)` to src/calc.py.")),
        ("b-multiply", drain_spec("Add a multiply function", "Blocked by: a-subtract\n", "Add `multiply(a, b)`.")),
    ];
    for (slug, text) in &specs {
        w.write(&format!("specs/{slug}/spec.md"), text);
    }
    w.agent("build", [built(1, 0)]);
    w.agent("review", [approved(1)]);
    let dir = format!("{}/specs", w.root().display());
    w.run(&["drain", "--dry-run", &dir]);
    w.run(&["drain", &dir]);
    w.set_pr(1, &merged());
    w.run(&["drain", "--dry-run", &dir]);
    let notes: Map<Json> =
        specs.iter().map(|(slug, _)| (*slug, Json::Str(read(&format!("{dir}/{slug}/spec.md"))))).collect();
    w.note("specs", Json::Object(notes));
}

/// doctor here, with --cwd, and on a machine: what it asks herdr and gh, and its exit status. Its lines are noted
/// without the version of git, which is the host's.
pub fn doctor_checks(w: &mut World) {
    w.machine("box");
    let project = w.project.to_string_lossy().into_owned();
    for args in [
        &["doctor"][..],
        &["doctor", "--cwd", &project],
        &["doctor", "--machine", "box", "--cwd", &project],
        &["doctor", "--machine", "nowhere", "--cwd", &project],
    ] {
        let n = w.start(args, false);
        w.invocation(n).wait();
    }
    let lines: Vec<Json> =
        w.invocations.iter().map(|inv| octoflow_e2e::strings(&host_free(&inv.stdout()))).collect();
    w.note("lines", Json::Array(lines));
}

/// doctor's lines without those of the git and tkinter checks, and their fixes, which say what the host has.
fn host_free(report: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut skipping = false;
    for line in report.lines() {
        if !line.starts_with(' ') {
            let check = line.split(':').next().unwrap_or_default().split_whitespace().last().unwrap_or_default();
            skipping = check == "git" || check == "tkinter";
        }
        if !skipping {
            lines.push(line.to_string());
        }
    }
    lines
}

/// run --spec: the spec is copied in as spec.md, the interview skipped, and the task taken from its title.
pub fn spec_file_run(w: &mut World) {
    let path = w.write("specs/subtract.md", SPEC);
    w.agent("build", [built(1, 0)]);
    w.agent("review", [approved(1)]);
    w.run(&["run", "--spec", &path]);
}

fn read(path: &str) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|e| panic!("reading {path}: {e}"))
}
