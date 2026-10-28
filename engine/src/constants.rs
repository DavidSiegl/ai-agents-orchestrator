//! The engine's defaults, intervals, names and exit statuses: the constants at the top of orchestrator.py, each with
//! its comment there. A constant that belongs to one part of the engine lives in that part's module instead.

/// Where a project keeps its runs' directories, relative to it.
pub const RUNS_DIR: &str = ".orchestrator/runs";
/// Where run --worktree puts each run's worktree, named by the run's key.
pub const WORKTREES_DIR: &str = ".orchestrator/worktrees";

/// PROOF_COPIES_DIR as a literal, for the prompts that concat! it into their text.
#[macro_export]
macro_rules! proof_copies_dir {
    () => {
        ".orchestrator/proofs"
    };
}
/// Where, in a run's working tree, a proof is copied to for its run (Workflow._run_proof), under the round that wrote
/// it.
pub const PROOF_COPIES_DIR: &str = proof_copies_dir!();

/// Seconds a turn may take before it times out (--timeout's default).
pub const DEFAULT_TURN_TIMEOUT: u64 = 1800;
/// How many review rounds a run takes at most (--max-rounds's default).
pub const DEFAULT_MAX_ROUNDS: u32 = 3;
/// How long herdr waits for an agent to start, in milliseconds.
pub const AGENT_START_TIMEOUT_MS: u64 = 60_000;
/// Seconds between two looks at a turn's agent and handoff file.
pub const POLL_SECONDS: u64 = 3;
/// How long a Builder or Reviewer may sit idle without its handoff file before the human is told.
pub const STALL_SECONDS: u64 = 180;
/// How long one attempt waits for an agent to take a prompt: herdr reports it working or blocked, or its handoff
/// file appears. An agent reported idle before its input takes text, as behind a startup dialog, drops the prompt.
pub const PROMPT_DELIVERY_SECONDS: u64 = 30;
/// A live orchestrator rewrites state.json at least this often, so `list` can tell a live run from a dead one.
pub const HEARTBEAT_SECONDS: u64 = 60;
/// A run whose state.json is older than this is stale: five missed beats, which rides out a slow SSH hop
/// or a Host.run that hits its 60 s limit.
pub const STALE_SECONDS: u64 = 300;
/// git fetch, git push and gh talk to the network; everything else Host runs is local to the machine.
pub const NETWORK_TIMEOUT: u64 = 300;
/// Seconds gitleaks may take to scan what one push publishes, which is local work.
pub const GITLEAKS_TIMEOUT: u64 = 300;
/// What the preflight tells a user whose machine lacks gitleaks with its `git` subcommand (8.19 and later).
pub const GITLEAKS_INSTALL: &str = "install gitleaks 8.19 or later where the agents run \
    (https://github.com/gitleaks/gitleaks#installing; on Arch, `pacman -S gitleaks`)";
/// The comment that makes gitleaks skip its line. Spelled in two halves so that a change moving the line that holds
/// it is not refused by the push's own check for added allow comments (Host._allow_comments).
pub const GITLEAKS_ALLOW: &str = concat!("gitleaks", ":allow");
/// How many `[extend] path` steps gitleaks follows from its --config: the files one and two steps away are read, and
/// an [extend] in the second is not (maxExtendDepth in gitleaks' config/config.go; 8.30.1 checked). A gitleaks that
/// follows more steps finds no copy of the third file in Host._write_base_rules' directory, and fails to load.
pub const GITLEAKS_EXTEND_DEPTH: u32 = 2;
/// The namespace that marks a branch as a run's, locally and on origin; prune touches no branch outside it.
pub const BRANCH_PREFIX: &str = "orchestrator/";
/// The remote a run fetches from, pushes to and opens its pull request on.
pub const REMOTE: &str = "origin";
/// prune deletes a run's branch once its pull request is merged, or closed this long: time to reopen it. Seconds.
pub const CLOSED_BRANCH_GRACE: u64 = 14 * 24 * 3600;
/// GitHub rejects pull request bodies over 65536 characters; this leaves room for the frame. In characters.
pub const PR_SECTION_LIMIT: usize = 20_000;

/// The verdicts a verdict step's file starts with.
pub const APPROVE: &str = "APPROVE";
pub const CHANGES_REQUESTED: &str = "CHANGES_REQUESTED";
/// The outcomes of a run whose workflow has no verdict step, once its last step is done. FINISHED counts as
/// approved: the run did all it was asked to, though no agent judged the change. With the quality gate,
/// QUALITY_GATE_FAILED says it still failed after the last quality round, and no agent weighed that.
pub const FINISHED: &str = "FINISHED";
pub const QUALITY_GATE_FAILED: &str = "QUALITY_GATE_FAILED";
/// The outcomes that count as a run's success.
pub const SUCCEEDED: [&str; 2] = [APPROVE, FINISHED];
/// The first line of a report whose agent cannot go on without the human (parse_blocked).
pub const BLOCKED: &str = "BLOCKED:";
/// How a BLOCKED line with nothing after the colon shows its question.
pub const NO_QUESTION: &str = "(no question given)";

/// The quality gate. Requests time out below STALE_SECONDS, so a hung one cannot make a live run look stale.
pub const DEFAULT_MAX_QUALITY_ROUNDS: u32 = 3;
/// A workflow's max_files and max_lines when it sets none (Pipeline).
pub const DEFAULT_MAX_FILES: i64 = 15;
pub const DEFAULT_MAX_LINES: i64 = 600;
/// The workflow keys that hold them; a workflow file sets each to a whole number of at least 1.
pub const BUDGET_KEYS: [&str; 2] = ["max_files", "max_lines"];
/// Seconds an HTTP request to Jenkins may take.
pub const HTTP_TIMEOUT: u64 = 30;
/// Seconds each blocking step of a request to PyPI may take, to connect or to read, so one request can take it more
/// than once. What bounds the new-dependencies check as a whole is the quality round's deadline (SLOW_STEP_MARGIN).
pub const PYPI_TIMEOUT: u64 = 15;
/// Seconds between two looks at a quality build.
pub const CI_POLL_SECONDS: u64 = 10;
/// How long one quality round may wait on Jenkins, failed requests retried, before the run fails. Seconds.
pub const QUALITY_TIMEOUT: u64 = 1200;
/// The orchestrator's own environment variables that hold the credentials; no process it starts inherits them.
pub const CI_ENV: [&str; 3] = ["JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN"];
/// The ones of CI_ENV that are secret, which CI.mask hides in text from Jenkins.
pub const CI_TOKENS: [&str; 1] = ["JENKINS_TOKEN"];
/// git's switches for how it reads every pathspec, which no process the orchestrator starts inherits: its pathspecs
/// are written in git's default syntax, and each switch changes what one matches without an error, as under ICASE a
/// `:(literal)` path such as _top_literal's matches a file whose name differs in case. The gate leaves them out of
/// its own processes' environment for its own pathspecs, as gate.py's tool_env says.
pub const GIT_PATHSPEC_ENV: [&str; 4] =
    ["GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"];
/// The parameters of the Jenkins quality job.
pub const JOB_PARAMETERS: [&str; 1] = ["GIT_REF"];
/// Snapshots are pushed to orchestrator-ci/<key>-<round>-q<quality round>.
pub const CI_REF_PREFIX: &str = "orchestrator-ci/";
/// A run's local refs are under refs/orchestrator/<key>/ (RunState::refs_prefix).
pub const LOCAL_REFS_PREFIX: &str = "refs/orchestrator/";
/// How much of a build's console the quality file of a build that failed before its Gate stage shows, in lines.
pub const CONSOLE_TAIL_LINES: usize = 60;

/// The default workflow's roles, key and label, in pane order. Each has its own --ROLE-model flag; another workflow's
/// roles take --model.
pub const ROLE_LABELS: [(&str, &str); 3] = [("spec", "Spec Collector"), ("build", "Builder"), ("review", "Reviewer")];

/// The agent harnesses a role can run in, by herdr's --kind. Each needs its herdr integration and its CLI
/// where the agents run.
pub const AGENT_KINDS: [&str; 5] = ["claude", "codex", "gemini", "opencode", "pi"];
/// The harness of a role that names none.
pub const DEFAULT_AGENT: &str = "claude";

/// The exit statuses of the orchestrator's commands, besides 0 for success.
pub const EXIT_ERROR: i32 = 1;
pub const EXIT_CHANGES_REQUESTED: i32 = 3;
/// Approved, but the pull request conflicts with its base, so it was opened as a draft.
pub const EXIT_CONFLICT: i32 = 4;
/// drain ran every spec it could, and specs remain whose blockers are not done, such as a pull request still open.
pub const EXIT_WAITING: i32 = 5;
pub const EXIT_INTERRUPTED: i32 = 130;

#[cfg(test)]
mod tests;
