//! Deleting the branches of runs whose pull request is done, as orchestrator.py's prune_branches: the `prune`
//! command, and the sweep a run that opens a pull request makes first (sweep_branches). A branch is deleted only when
//! its pull request's head commit holds all of it, so every deleted branch can be restored from its pull request
//! (GitHub keeps refs/pull/<n>/head; docs/design.md).

use super::{record_in_state, Connected};
use crate::constants::{BRANCH_PREFIX, CLOSED_BRANCH_GRACE, RUNS_DIR};
use crate::error::OrchestratorError;
use crate::host::{GhError, Host, PullRequest};
use crate::py::datetime::fromisoformat_timestamp;
use crate::py::json::Json;
use crate::py::text::repr;
use crate::runs::{run_health, RUNNING};
use crate::state::RunState;

/// A run's branch_cleanup once both sides of its branch are gone.
pub const BRANCH_DELETED: &str = "deleted";
/// How a run's branch_cleanup, and a side's outcome, starts when a side was kept; the reason follows.
pub const BRANCH_KEPT: &str = "kept: ";

/// What prune_branches did with one run's branch, locally and on origin, and what it recorded for it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PrunedRun {
    /// The run's id, as its state.json saves it.
    pub run_id: String,
    /// The run's branch, as its state.json saves it: one the orchestrator made (BRANCH_PREFIX), since prune takes
    /// no other.
    pub branch: String,
    /// What became of the local branch: BRANCH_DELETED, "would be deleted", "gone", or BRANCH_KEPT and why; empty
    /// when it was not decided.
    pub local: String,
    /// What became of origin's branch, as local words it.
    pub remote: String,
    /// Why nothing was decided: the pull request is not done yet, or pruning the run failed. Nothing is recorded
    /// then.
    pub skipped: Option<String>,
    /// The run's branch_cleanup: written, or what would be in a dry run; None when skipped.
    pub cleanup: Option<String>,
}

impl PrunedRun {
    /// The run as prune prints it.
    pub fn line(&self) -> String {
        let what = match &self.skipped {
            Some(why) => format!("skipped: {why}"),
            None => format!("local {}, remote {}", self.local, self.remote),
        };
        format!("{}  {}  {what}", self.run_id, self.branch)
    }
}

/// Delete the branches of the runs of the project at cwd, on world's host, whose pull request is merged, or closed
/// for CLOSED_BRANCH_GRACE by world's wall clock, and record what became of each in its state.json
/// (branch_cleanup); with dry_run, decide each and change nothing.
///
/// A local branch goes when it is at or behind its pull request's head commit, origin's when it is at it, deleted
/// under a lease; a branch checked out in a worktree stays. Only runs with no branch_cleanup yet are candidates, and
/// with revisit the runs whose branch was kept too; a run without a pull request, one whose branch the orchestrator
/// did not make, and one an orchestrator drives now, are not. Returns each candidate, in run_states' order, but
/// those after gh was found unusable, which ends the sweep. A failure on one run is logged and skips that run.
///
/// Errors: the runs or the checked-out branches cannot be read, or a run's failure while world's interrupt is raised.
pub fn prune_branches(
    world: &Connected,
    cwd: &str,
    dry_run: bool,
    revisit: bool,
) -> Result<Vec<PrunedRun>, OrchestratorError> {
    let host = world.with.host;
    let runs: Vec<Json> = host
        .run_states(cwd)?
        .into_iter()
        .filter(|(age, s)| is_prune_candidate(s, *age, world, revisit))
        .map(|(_, s)| s)
        .collect();
    if runs.is_empty() {
        return Ok(vec![]);
    }
    let checked_out = host.checked_out_branches(cwd)?;
    let mut pruned = vec![];
    for saved in runs {
        let text = |key: &str| saved.get(key).map_or(String::new(), Json::python_str);
        let mut run = PrunedRun { run_id: text("run_id"), branch: text("branch"), ..PrunedRun::default() };
        match prune_run(world, cwd, &saved, &mut run, &checked_out, dry_run) {
            Ok(()) => {}
            Err(e) if world.with.interrupt.is_raised() => return Err(e.into()),
            Err(GhError::Unusable(e)) => {
                world.with.human.log(&format!("stopped pruning run branches: {e}"));
                break;
            }
            Err(GhError::Failed(e)) => {
                world.with.human.log(&format!("could not prune the branch of run {}: {e}", run.run_id));
                run.skipped = Some(format!("failed: {e}"));
            }
        }
        pruned.push(run);
    }
    Ok(pruned)
}

/// Whether prune may touch the branch of the run saved as s, its state.json written age seconds ago: one the
/// orchestrator made and opened a pull request for, and that no orchestrator drives now, as world tells (run_health).
/// A run without a pull request may hold the Builder's uncommitted work. One with a branch_cleanup is done with, or
/// with revisit, only one whose branch was kept.
fn is_prune_candidate(s: &Json, age: u64, world: &Connected, revisit: bool) -> bool {
    let branch = s.get("branch").and_then(Json::as_str);
    let cleanup = s.get("branch_cleanup").filter(|c| c.truthy());
    if !(branch.is_some_and(|b| b.starts_with(BRANCH_PREFIX)) && s.get("pr_url").is_some_and(Json::truthy)) {
        return false;
    }
    if let Some(cleanup) = cleanup {
        let kept = cleanup.as_str().is_some_and(|c| c.starts_with(BRANCH_KEPT));
        if !(revisit && kept) {
            return false;
        }
    }
    let health = run_health(s, age, world.local_host, world.with.pid_alive);
    !health.is_some_and(|(h, _)| h == RUNNING)
}

/// Decide each side of the branch of the run saved as saved, delete what may go, unless dry_run, and record the
/// result in its state.json, on world's host, filling run in; checked_out maps each branch checked out to its
/// worktree. Errors: GhError::Unusable when gh is, else GhError::Failed with what failed.
fn prune_run(
    world: &Connected,
    cwd: &str,
    saved: &Json,
    run: &mut PrunedRun,
    checked_out: &crate::py::json::Map<String>,
    dry_run: bool,
) -> Result<(), GhError> {
    let failed = GhError::Failed;
    let host = world.with.host;
    // Before gh pr view, as Python reads wall() for prune_run's call.
    let now = world.with.clocks.wall();
    let state = RunState::from_saved(saved.as_object_or_empty()).map_err(failed)?;
    // is_prune_candidate takes only runs with both.
    let (Some(url), Some(branch)) = (state.pr_url.as_deref(), state.branch.as_deref()) else {
        return Err(failed(OrchestratorError::new("the run has no branch or no pull request")));
    };
    let number = url.trim_end_matches('/').rsplit('/').next().unwrap_or("");
    let pr = host.pull_request(cwd, url)?;
    if let Some(why) = pull_request_pending(&pr, number, now).map_err(failed)? {
        run.skipped = Some(why);
        return Ok(());
    }
    let head = &pr.head_ref_oid;
    // Each side is decided on its own, so keeping one does not keep the other.
    let (remote, remote_kept) = prune_remote(host, cwd, branch, head, number, dry_run).map_err(failed)?;
    let (local, local_kept) = prune_local(host, cwd, branch, head, number, checked_out, dry_run).map_err(failed)?;
    (run.remote, run.local) = (remote, local);
    let kept: Vec<String> = [local_kept, remote_kept].into_iter().flatten().collect();
    let cleanup = if kept.is_empty() { BRANCH_DELETED.to_string() } else { format!("{BRANCH_KEPT}{}", kept.join("; ")) };
    run.cleanup = Some(cleanup.clone());
    if !dry_run {
        let path = format!("{cwd}/{RUNS_DIR}/{}/state.json", state.run_id);
        record_in_state(host, &path, saved, "branch_cleanup", Json::from(cleanup), "its branch was pruned").map_err(failed)?;
    }
    Ok(())
}

/// Why the branch of pull request pr, number number, stays for now; None once it is merged, or closed for
/// CLOSED_BRANCH_GRACE before now, in seconds since the epoch. Its closing time is read as Python's
/// datetime.fromisoformat(...).timestamp() reads it, one without an offset in the machine's zone. Errors: it is
/// closed at what is not such a time.
pub fn pull_request_pending(pr: &PullRequest, number: &str, now: f64) -> Result<Option<String>, OrchestratorError> {
    if pr.state == "MERGED" {
        return Ok(None);
    }
    if pr.state != "CLOSED" {
        return Ok(Some(format!("PR #{number} is {}", pr.state.to_lowercase())));
    }
    let closed = pr.closed_at.as_deref().map(fromisoformat_timestamp);
    let Some(Ok(closed)) = closed else {
        let at = pr.closed_at.as_deref().map_or("None".to_string(), repr);
        return Err(OrchestratorError(format!("PR #{number} is closed at {at}, which is not a time")));
    };
    if now - closed > CLOSED_BRANCH_GRACE as f64 {
        return Ok(None);
    }
    Ok(Some(format!("PR #{number} was closed less than {} days ago", CLOSED_BRANCH_GRACE / 86400)))
}

/// What became of origin's branch, and why it was kept, if it was: gone already (its tracking ref then deleted),
/// kept with commits beyond the pull request's head, or deleted at head.
fn prune_remote(
    host: &dyn Host,
    cwd: &str,
    branch: &str,
    head: &str,
    number: &str,
    dry_run: bool,
) -> Result<(String, Option<String>), OrchestratorError> {
    let Some(tip) = host.remote_branch_commit(cwd, branch)? else {
        if !dry_run {
            host.delete_tracking_ref(cwd, branch)?;
        }
        return Ok(("gone".to_string(), None));
    };
    if tip != head {
        let why = format!("remote has commits beyond PR #{number}");
        return Ok((format!("{BRANCH_KEPT}{why}"), Some(why)));
    }
    if !dry_run {
        host.delete_remote_branch_at(cwd, branch, head)?;
    }
    Ok((deletion(dry_run), None))
}

/// What became of the local branch, and why it was kept, if it was: kept while checked out (checked_out maps each
/// branch checked out to its worktree), gone already, kept with commits the pull request's head lacks or that cannot
/// be told without that head, or deleted.
fn prune_local(
    host: &dyn Host,
    cwd: &str,
    branch: &str,
    head: &str,
    number: &str,
    checked_out: &crate::py::json::Map<String>,
    dry_run: bool,
) -> Result<(String, Option<String>), OrchestratorError> {
    let kept = |why: String| Ok((format!("{BRANCH_KEPT}{why}"), Some(why)));
    if let Some(worktree) = checked_out.get(branch) {
        return kept(format!("checked out in {worktree}"));
    }
    let Some(tip) = host.branch_commit(cwd, branch)? else { return Ok(("gone".to_string(), None)) };
    if tip != head {
        if !host.has_commit(cwd, head)? {
            let short: String = head.chars().take(12).collect();
            return kept(format!("PR #{number}'s head {short} is not in this clone; fetch it to decide"));
        }
        if !host.is_ancestor(cwd, &tip, head)? {
            let n = host.commits_beyond(cwd, head, branch)?;
            return kept(format!("local has {n} commit{} beyond PR #{number}", if n == 1 { "" } else { "s" }));
        }
    }
    if !dry_run {
        host.delete_branch(cwd, branch)?;
    }
    Ok((deletion(dry_run), None))
}

/// What a side that may go became: deleted, or in a dry run, would be.
fn deletion(dry_run: bool) -> String {
    if dry_run { "would be deleted" } else { BRANCH_DELETED }.to_string()
}

/// Print each of pruned's lines on world's standard output, or that there was none.
pub fn print_pruned(world: &Connected, pruned: &[PrunedRun]) {
    if pruned.is_empty() {
        world.say("No run branches to prune.");
    }
    for run in pruned {
        world.say(&run.line());
    }
}

/// The one line a run logs about the branches of earlier runs it pruned; None when it did nothing.
pub fn sweep_summary(pruned: &[PrunedRun]) -> Option<String> {
    let deleted = pruned
        .iter()
        .filter(|r| r.cleanup.as_deref() == Some(BRANCH_DELETED) && (r.local == BRANCH_DELETED || r.remote == BRANCH_DELETED))
        .count();
    let kept = pruned.iter().filter(|r| r.cleanup.as_deref().is_some_and(|c| c.starts_with(BRANCH_KEPT))).count();
    let mut parts = vec![];
    if deleted > 0 {
        parts.push(format!("deleted {} of earlier runs", branches(deleted)));
    }
    if kept > 0 {
        parts.push(if deleted > 0 {
            format!("kept {kept}, see prune")
        } else {
            format!("kept {} of earlier runs, see prune", branches(kept))
        });
    }
    (!parts.is_empty()).then(|| parts.join("; "))
}

/// n branches, in words.
fn branches(n: usize) -> String {
    if n == 1 { "1 branch".to_string() } else { format!("{n} branches") }
}

/// Prune the branches of the project's earlier runs, as a new run in cwd starts, logging what was done to world's
/// human; nothing that goes wrong stops the run, but a Ctrl-C. Errors: only one that failed while world's interrupt
/// is raised.
pub fn sweep_branches(world: &Connected, cwd: &str) -> Result<(), OrchestratorError> {
    match prune_branches(world, cwd, false, false) {
        Err(e) if world.with.interrupt.is_raised() => Err(e),
        Err(e) => {
            world.with.human.log(&format!("could not prune the branches of earlier runs: {e}"));
            Ok(())
        }
        Ok(pruned) => {
            if let Some(line) = sweep_summary(&pruned) {
                world.with.human.log(&line);
            }
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests;
