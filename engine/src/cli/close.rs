//! Cleaning up after a run nothing drives any more, as orchestrator.py's close_run: the `close` command.

use super::start::find_run;
use super::{record_in_state, Connected};
use crate::constants::RUNS_DIR;
use crate::error::OrchestratorError;
use crate::herdr::Herdr;
use crate::py::json::Json;
use crate::py::text::splitlines;
use crate::runs::{run_health, RUNNING};
use crate::state::RunState;

/// Clean up after the run of the project at cwd that run_ref names, by its id or key, on world's herdr and host,
/// record what was done as the run's closed in its state.json, so that resume refuses it, and return that. Prints
/// each step on world's standard output.
///
/// Every run loses its workspace. A failed or stale --worktree run also loses its worktree, changes and all (listed
/// first), and its branch if that holds no commit beyond the run's base: the Builder never commits, so such a branch
/// has nothing to lose. A finished run's worktree is a --no-pr run's change, and an in-place run's checkout and
/// branch are the human's to sort out, so both stay. Each step skips what is already gone, so a close that failed
/// part way can be run again; closed is recorded only once every step has succeeded. A run already closed is left
/// as it is, and its closed returned.
///
/// Errors: no run or several match run_ref; the run is still running; a step fails; or the state.json changed
/// since it was read.
pub fn close_run(world: &Connected, cwd: &str, run_ref: &str) -> Result<String, OrchestratorError> {
    let host = world.with.host;
    let runs = host.run_states(cwd)?;
    let (age, saved) = find_run(&runs, run_ref, cwd)?;
    let state = RunState::from_saved(saved.as_object_or_empty())?;
    if let Some(closed) = state.closed.as_deref().filter(|c| !c.is_empty()) {
        world.say(&format!("run {} is already closed: {closed}", state.run_id));
        return Ok(closed.to_string());
    }
    let health = run_health(saved, *age, world.local_host, world.with.pid_alive);
    if let Some((RUNNING, why)) = &health {
        return Err(OrchestratorError(format!(
            "run {} is still running ({why}); stop it before closing it",
            state.run_id
        )));
    }
    let failed = state.error.as_deref().is_some_and(|e| !e.is_empty()) || health.is_some();
    let mut done = vec![close_run_workspace(world, &state.workspace_id)?];
    match state.worktree.as_deref().filter(|w| !w.is_empty()) {
        Some(_) if failed => done.extend(discard_run_worktree(world, cwd, &state)?),
        Some(worktree) if host.is_worktree(cwd, worktree)? => done.push(format!("worktree kept: {worktree}")),
        _ => {}
    }
    let closed = done.join("; ");
    let path = format!("{cwd}/{RUNS_DIR}/{}/state.json", state.run_id);
    record_in_state(host, &path, saved, "closed", Json::from(closed.as_str()), "the run was closed")?;
    world.say(&format!("closed run {}: {closed}", state.run_id));
    Ok(closed)
}

/// Close the herdr workspace, unless it is already gone or the run never had one, and say which.
fn close_run_workspace(world: &Connected, workspace: &str) -> Result<String, OrchestratorError> {
    let herdr: &dyn Herdr = world.with.herdr;
    if !workspace.is_empty() && herdr.workspace_exists(workspace)? {
        herdr.close_workspace(workspace)?;
        world.say(&format!("closed workspace {workspace}"));
        return Ok("workspace closed".to_string());
    }
    Ok("workspace already gone".to_string())
}

/// Force-remove the worktree of cwd that the failed run of state built in, after listing the changes that go with
/// it, then its branch (discard_run_branch); say what became of each.
fn discard_run_worktree(world: &Connected, cwd: &str, state: &RunState) -> Result<Vec<String>, OrchestratorError> {
    let host = world.with.host;
    let worktree = state.worktree.as_deref().unwrap_or_default();
    let mut done = vec![];
    if host.is_worktree(cwd, worktree)? {
        let status = host.git(worktree, &["status", "--porcelain"])?;
        let changes = splitlines(&status);
        if !changes.is_empty() {
            world.say(&format!("discarding in {worktree}:"));
            for line in changes {
                world.say(&format!("    {line}"));
            }
        }
        host.remove_worktree(cwd, worktree, true)?;
        world.say(&format!("deleted worktree {worktree}"));
        done.push("worktree deleted".to_string());
    } else {
        done.push("worktree already gone".to_string());
    }
    // Even with the worktree gone: a close that failed at the branch must find it again.
    if let Some(branch) = state.branch.as_deref().filter(|b| !b.is_empty()) {
        if host.branch_commit(cwd, branch)?.is_some() {
            done.push(discard_run_branch(world, cwd, state, branch)?);
        }
    }
    Ok(done)
}

/// Delete the run of state's branch of cwd unless it has commits beyond the run's base, or the run has no base to
/// tell; say which.
fn discard_run_branch(world: &Connected, cwd: &str, state: &RunState, branch: &str) -> Result<String, OrchestratorError> {
    let host = world.with.host;
    let why = match state.base.as_deref() {
        None => "the run has no base to compare it with".to_string(),
        Some(base) => match host.commits_beyond(cwd, base, branch)? {
            0 => {
                host.delete_branch(cwd, branch)?;
                world.say(&format!("deleted branch {branch}"));
                return Ok("branch deleted".to_string());
            }
            n => format!("{n} commit{} beyond base", if n == 1 { "" } else { "s" }),
        },
    };
    world.say(&format!("kept branch {branch}: {why}"));
    Ok(format!("branch kept: {why}"))
}

#[cfg(test)]
mod tests;
