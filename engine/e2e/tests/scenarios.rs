//! The e2e scenarios: each a function that scripts a World and runs the engine in it, compared with its recording
//! by octoflow_e2e::check::check, one test each.
//!
//! A scenario says only what happens: what each role's agent writes when prompted, what gh answers, and which engine
//! commands run, in which order, with which stopped midway. What the engine did in return is the world's record.
//! Ported from tests/parity/scenarios.py, whose recordings these are; that harness keeps the scenarios not here yet
//! (kills, the quality gate and serve) until octoflow-without-python ticket 02 ports them and removes it.

use serde_json::{json, Map, Value};

use octoflow_e2e::check::check;
use octoflow_e2e::world::{checkout, wait_for, Fakes, World};

/// The fakes this package builds, which every world puts on the engine's PATH.
fn fakes() -> Fakes {
    Fakes {
        herdr: env!("CARGO_BIN_EXE_fake-herdr").into(),
        gh: env!("CARGO_BIN_EXE_fake-gh").into(),
        ssh: env!("CARGO_BIN_EXE_fake-ssh").into(),
    }
}

/// A test per scenario, each the function of that name checked against recorded/<name>.json.
macro_rules! scenarios {
    ($($name:ident),* $(,)?) => {
        mod run {
            $(
                #[test]
                fn $name() {
                    super::check(stringify!($name), &super::fakes(), super::$name);
                }
            )*
        }
    };
}

scenarios!(
    default_approved,
    default_changes_then_approved,
    default_max_rounds,
    blocked_build_answered,
    verdict_retried,
    no_pr_run,
    agent_exits,
    workflow_quick,
    workflow_spec_build_review,
    workflow_product_team,
    locks_restored,
    proofs_run,
    worktree_run,
    machine_run,
    publish_with_conflict,
    runs_listed_and_shown,
    branches_pruned,
    failed_run_closed,
    finished_run_closed,
    workflows_listed,
    prompt_file_run,
    conflict_refreshed,
    specs_drained,
    doctor_checks,
    spec_file_run,
    takeover,
);

// -- What the agents write ------------------------------------------------------

const TASK: &str = "Add a subtract function";
const SPEC: &str = "# Add a subtract function\n\nAdd `subtract(a, b)` to `src/calc.py`, returning a - b, with a test.\n";
const CALC_WITH_SUBTRACT: &str = "def add(a, b):\n    \"\"\"The sum of a and b.\"\"\"\n    return a + b\n\n\n\
                                  def subtract(a, b):\n    \"\"\"a minus b.\"\"\"\n    return a - b\n";
const TEST_WITH_SUBTRACT: &str = "from calc import add, subtract\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n\
                                  def test_subtract():\n    assert subtract(3, 2) == 1\n";

/// What an agent's action changes besides its handoff file, and how it ends (see octoflow_e2e::fakes's perform).
#[derive(Default)]
struct Turn {
    /// Its changes to the working tree, path to text, in order.
    edit: Vec<(&'static str, &'static str)>,
    /// The hold it waits on before it is done, World::release's.
    hold: Option<&'static str>,
    /// Whether the agent exits afterwards.
    exit: bool,
}

/// The action of an agent that makes the turn's changes to the working tree, then writes text to file in the run
/// directory.
fn writes(file: &str, text: &str, turn: Turn) -> Value {
    let edit: Map<String, Value> = turn.edit.into_iter().map(|(p, t)| (p.to_string(), t.into())).collect();
    let mut action = json!({"write": {file: text}, "edit": edit});
    if let Some(hold) = turn.hold {
        action["hold"] = hold.into();
    }
    if turn.exit {
        action["exit"] = true.into();
    }
    action
}

fn spec_written(hold: Option<&'static str>) -> Value {
    writes("spec.md", SPEC, Turn { hold, ..Turn::default() })
}

/// The Builder's report of round n, having made the changes edit gives, which are adding subtract and its test when
/// it gives none.
fn built_with(n: u32, edit: Vec<(&'static str, &'static str)>, hold: Option<&'static str>) -> Value {
    let edit = if edit.is_empty() { vec![("src/calc.py", CALC_WITH_SUBTRACT), ("tests/test_calc.py", TEST_WITH_SUBTRACT)] } else { edit };
    writes(&format!("build-{n}.md"), &format!("Added subtract and its test (round {n}). `pytest` passes.\n"), Turn { edit, hold, exit: false })
}

fn built(n: u32) -> Value {
    built_with(n, vec![], None)
}

fn reviewed_with(n: u32, verdict: &str, findings: &str, hold: Option<&'static str>) -> Value {
    writes(&format!("review-{n}.md"), &format!("VERDICT: {verdict}\n\n{findings}\n"), Turn { hold, ..Turn::default() })
}

fn reviewed(n: u32) -> Value {
    reviewed_with(n, "APPROVE", "None.", None)
}

fn examples(name: &str) -> String {
    checkout().join("examples/workflows").join(name).to_string_lossy().into_owned()
}

fn read(path: &str) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|e| panic!("reading {path}: {e}"))
}

// -- The default workflow ------------------------------------------------------------

/// The default workflow, approved in its first round, ending in a pull request.
fn default_approved(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--", TASK]);
}

/// The Reviewer requests changes in round 1 and approves round 2's fix.
fn default_changes_then_approved(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built_with(1, vec![("src/calc.py", CALC_WITH_SUBTRACT)], None), built(2)]);
    w.agent(
        "review",
        [reviewed_with(1, "CHANGES_REQUESTED", "1. tests/test_calc.py:1: subtract has no test.", None), reviewed(2)],
    );
    w.run(&["run", "--", TASK]);
}

/// Changes requested in every round until --max-rounds: a draft pull request, exit status 3.
fn default_max_rounds(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1), built(2)]);
    let still_wrong = |n: u32| {
        reviewed_with(n, "CHANGES_REQUESTED", &format!("1. src/calc.py:5: still wrong in round {n}."), None)
    };
    w.agent("review", [still_wrong(1), still_wrong(2)]);
    w.run(&["run", "--max-rounds", "2", "--", TASK]);
}

/// The Builder's first report is BLOCKED: the human is asked, and the Builder's next report ends the turn.
fn blocked_build_answered(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [writes("build-1.md", "BLOCKED: Should subtract accept floats too?\n", Turn::default()), built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--", TASK]);
}

/// A review without a VERDICT line is moved aside and asked for once more.
fn verdict_retried(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [writes("review-1.md", "Looks right to me.\n", Turn::default()), reviewed(1)]);
    w.run(&["run", "--", TASK]);
}

/// run --no-pr: the change stays uncommitted in the checkout and the workspace stays open.
fn no_pr_run(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--no-pr", "--", TASK]);
}

/// The Builder exits without writing its report: the run fails, keeping its workspace and branch.
fn agent_exits(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [json!({"exit": true})]);
    w.run(&["run", "--", TASK]);
}

// -- The other workflows ------------------------------------------------------------

/// examples/workflows/quick.toml by --workflow-file: Builder and Reviewer, the task as the contract.
fn workflow_quick(w: &mut World) {
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--workflow-file", &examples("quick.toml"), "--", TASK]);
}

/// examples/workflows/spec-build-review.toml installed as a workflow file and run by --workflow, with --role-model and
/// --agent.
fn workflow_spec_build_review(w: &mut World) {
    w.write("config/ai-agents-orchestrator/workflows/sbr.toml", &read(&examples("spec-build-review.toml")));
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&[
        "run", "--workflow", "sbr", "--agent", "codex", "--role-model", "review=opus", "--permission-mode", "acceptEdits",
        "--", TASK,
    ]);
}

const TESTS_LOCKED: &str = TEST_WITH_SUBTRACT;
const TESTS_WEAKENED: &str = "from calc import add, subtract\n\n\ndef test_add():\n    assert add(1, 2) == 3\n";

/// examples/workflows/product-team.toml, approved in round 1, its Engineer making engineer_edits.
fn product_team(w: &mut World, engineer_edits: Vec<(&'static str, &'static str)>) {
    w.agent("spec", [spec_written(None)]);
    w.agent("design", [writes("design.md", "# Design\n\nAdd subtract beside add in src/calc.py.\n", Turn::default())]);
    let tests = Turn { edit: vec![("tests/test_calc.py", TESTS_LOCKED)], ..Turn::default() };
    w.agent("tests", [writes("tests.md", "Acceptance test: tests/test_calc.py::test_subtract.\n", tests)]);
    w.agent("build", [built_with(1, engineer_edits, None)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--workflow-file", &examples("product-team.toml"), "--", TASK]);
}

/// examples/workflows/product-team.toml: five roles, the QA Engineer's tests locked, the Engineer leaving them.
fn workflow_product_team(w: &mut World) {
    product_team(w, vec![("src/calc.py", CALC_WITH_SUBTRACT)]);
}

/// The product team's Engineer weakens the locked acceptance test: it is restored, its diff kept, and the Tech Lead
/// told.
fn locks_restored(w: &mut World) {
    product_team(w, vec![("src/calc.py", CALC_WITH_SUBTRACT), ("tests/test_calc.py", TESTS_WEAKENED)]);
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
fn proofs_run(w: &mut World) {
    let path = w.write("workflows/proofs.toml", PROOF_WORKFLOW);
    let calc_wrong: &'static str = CALC_WITH_SUBTRACT.replace("return a - b", "return b - a").leak();
    w.agent("build", [built_with(1, vec![("src/calc.py", calc_wrong)], None), built(2)]);
    let review = "VERDICT: CHANGES_REQUESTED\n\n1. src/calc.py:7: the sign is wrong.\n";
    w.agent("review", [json!({"write": {"proofs-1/test_1_sign.sh": PROOF, "review-1.md": review}}), reviewed(2)]);
    w.run(&["run", "--workflow-file", &path, "--", TASK]);
}

// -- Where the run works -------------------------------------------------------------

/// run --worktree: the run builds in a worktree of its own from origin's main, which is removed once the pull request
/// is open; the checkout itself, dirty, is left alone.
fn worktree_run(w: &mut World) {
    w.write("project/notes.txt", "the human's own uncommitted work\n");
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--worktree", "--", TASK]);
}

/// run --machine: herdr is addressed by the machine, and every file and git command goes over ssh, which the fake runs
/// here.
fn machine_run(w: &mut World) {
    w.machine("box");
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    let project = w.project.to_string_lossy().into_owned();
    w.run(&["run", "--machine", "box", "--cwd", &project, "--", TASK]);
}

/// origin's main changes the same lines while the run reviews: publishing finds the conflict, pushes the branch without
/// the merge and opens a draft pull request, exit status 4.
fn publish_with_conflict(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed_with(1, "APPROVE", "None.", Some("review"))]);
    let run = w.start(&["run", "--", TASK], true);
    w.wait_reached("review");
    let upstream = "def add(a, b):\n    \"\"\"a plus b.\"\"\"\n    return b + a\n\n\ndef subtract(a, b):\n    return -(b - a)\n";
    w.push_to_origin(&[("src/calc.py", upstream)], "Add subtract upstream");
    w.release("review");
    w.invocations[run].wait();
}

/// resume --force while the run's orchestrator is alive: the new one takes the run over, and the old one stops at its
/// next save without saving.
fn takeover(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built_with(1, vec![], Some("build"))]);
    // Held, so that the second still owns the run when the first next looks, rather than having finished it.
    w.agent("review", [reviewed_with(1, "APPROVE", "None.", Some("review"))]);
    let first = w.start(&["run", "--", TASK], true);
    w.wait_reached("build");
    let key = w.run_key(1);
    let second = w.start(&["resume", "--force", &key], true);
    let second_pid = w.invocations[second].pid();
    wait_for(|| w.run_state(&key)["owner"]["pid"].as_i64() == Some(second_pid), "the second orchestrator to claim the run");
    // The second has claimed it; once it waits on the Builder too, let the Builder finish.
    let waits_on_builder = || {
        w.ledger.events().iter().any(|e| {
            let herdr: Vec<&str> = e.get("herdr").and_then(Value::as_array).map_or(vec![], |c| c.iter().filter_map(Value::as_str).collect());
            e.get("by").and_then(Value::as_str) == Some("2") && herdr.get(..2) == Some(&["agent", "get"])
        })
    };
    wait_for(waits_on_builder, "the second orchestrator to wait on the Builder");
    w.release("build");
    w.invocations[first].wait();
    w.wait_reached("review");
    w.release("review");
    w.invocations[second].wait();
}

// -- The commands about runs ------------------------------------------------------------

/// A default run approved in round 1, its pull request open.
fn approved_run(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--", TASK]);
}

const MERGED: fn() -> Value = || json!({"state": "MERGED", "closedAt": "2026-01-02T00:00:00Z"});

/// list, list --json with --pr-state, show and metrics, over a finished run and a failed one.
fn runs_listed_and_shown(w: &mut World) {
    approved_run(w);
    w.agent("spec", [json!({"exit": true})]);
    w.run(&["run", "--", "Add a multiply function"]);
    w.run(&["list"]);
    w.set_pr(1, MERGED());
    w.run(&["list", "--json", "--pr-state"]);
    let (first, second) = (w.run_key(1), w.run_key(2));
    w.run(&["show", &first]);
    w.run(&["show", &second]);
    w.run(&["metrics"]);
}

/// prune once the pull request is merged: --dry-run first, then for real, locally and on origin; then a new run's
/// sweep, which finds nothing left.
fn branches_pruned(w: &mut World) {
    approved_run(w);
    w.run(&["prune"]);
    w.set_pr(1, MERGED());
    w.run(&["prune", "--dry-run"]);
    w.run(&["prune"]);
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--no-pr", "--", "Add a subtract function again"]);
}

/// close of a failed --worktree run: its workspace closed, its worktree and its branch, holding no commit, deleted;
/// then resume refuses it, and a second close says it is closed.
fn failed_run_closed(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    let exits = Turn { edit: vec![("src/calc.py", CALC_WITH_SUBTRACT)], exit: true, ..Turn::default() };
    w.agent("build", [writes("build-1.md", "", exits)]);
    w.run(&["run", "--worktree", "--", TASK]);
    let key = w.run_key(1);
    w.run(&["close", &key]);
    w.run(&["resume", &key]);
    w.run(&["close", &key]);
}

/// close of a finished --no-pr run: the workspace is closed and the checkout keeps the change.
fn finished_run_closed(w: &mut World) {
    w.agent("spec", [spec_written(None)]);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--no-pr", "--", TASK]);
    let key = w.run_key(1);
    w.run(&["close", &key]);
}

/// workflows: the list, with a workflow file and a prompt file installed; the default printed as a file; an example by
/// its path; and a workflow file that does not load.
fn workflows_listed(w: &mut World) {
    w.write("config/ai-agents-orchestrator/workflows/quick.toml", &read(&examples("quick.toml")));
    w.write(
        "config/ai-agents-orchestrator/prompts/BUILD_PROMPT.md",
        "Build {spec_path} in {cwd}; follow the conventions. Report to {build_path}.\n",
    );
    w.run(&["workflows"]);
    w.run(&["workflows", "default"]);
    w.run(&["workflows", &examples("product-team.toml")]);
    let broken = w.write("workflows/broken.toml", "[[steps]]\nuse = \"nothing\"\n");
    w.run(&["workflows", &broken]);
}

/// A run with a prompt file: the Builder gets its text, the run saves it, and the commit says so.
fn prompt_file_run(w: &mut World) {
    w.write(
        "config/ai-agents-orchestrator/prompts/BUILD_PROMPT.md",
        "Build what {spec_path} says, in {cwd}. As your last step, report to {build_path}.\n",
    );
    approved_run(w);
}

/// refresh after a pull request opened over conflicts: first GitHub still finds it conflicting (exit status 5), then
/// mergeable, so the warning goes from its description, it is marked ready, and the run forgets its conflicts.
fn conflict_refreshed(w: &mut World) {
    publish_with_conflict(w);
    w.set_pr(1, json!({"mergeable": "CONFLICTING"}));
    w.run(&["refresh"]);
    w.set_pr(1, json!({"mergeable": "MERGEABLE"}));
    let key = w.run_key(1);
    w.run(&["refresh", &key]);
    let pr = w.pr(1);
    w.note("pull request", pr);
}

// -- The other commands -----------------------------------------------------------------

/// A spec drain reads: its title, Status: line, a Blocked by: line when it has one, and its goal.
fn drain_spec(title: &str, blocked: &str, goal: &str) -> String {
    format!("# {title}\n\nStatus: ready-for-agent\n{blocked}\n## Goal\n\n{goal}\n")
}

/// drain over two specs, the second blocked by the first: --dry-run, then a run of the first, which ends in review,
/// leaving the second waiting (exit status 5); the specs' headers are noted.
fn specs_drained(w: &mut World) {
    let specs = [
        ("a-subtract", drain_spec(TASK, "", "Add `subtract(a, b)` to src/calc.py.")),
        ("b-multiply", drain_spec("Add a multiply function", "Blocked by: a-subtract\n", "Add `multiply(a, b)`.")),
    ];
    for (slug, text) in &specs {
        w.write(&format!("specs/{slug}/spec.md"), text);
    }
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    let dir = w.root.join("specs").to_string_lossy().into_owned();
    w.run(&["drain", "--dry-run", &dir]);
    w.run(&["drain", &dir]);
    w.set_pr(1, MERGED());
    w.run(&["drain", "--dry-run", &dir]);
    let notes: Map<String, Value> =
        specs.iter().map(|(slug, _)| (slug.to_string(), read(&format!("{dir}/{slug}/spec.md")).into())).collect();
    w.note("specs", Value::Object(notes));
}

/// doctor here, with --cwd, and on a machine: what it asks herdr and gh, and its exit status. Its lines are noted
/// without the version of git, which is the host's.
fn doctor_checks(w: &mut World) {
    w.machine("box");
    let project = w.project.to_string_lossy().into_owned();
    for args in [
        vec!["doctor"],
        vec!["doctor", "--cwd", &project],
        vec!["doctor", "--machine", "box", "--cwd", &project],
        vec!["doctor", "--machine", "nowhere", "--cwd", &project],
    ] {
        let i = w.start(&args, false);
        w.invocations[i].wait();
    }
    let lines: Vec<Value> = w.invocations.iter().map(|inv| json!(host_free(&inv.stdout()))).collect();
    w.note("lines", Value::Array(lines));
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
fn spec_file_run(w: &mut World) {
    let path = w.write("specs/subtract.md", SPEC);
    w.agent("build", [built(1)]);
    w.agent("review", [reviewed(1)]);
    w.run(&["run", "--spec", &path]);
}
