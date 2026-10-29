//! `refresh`: what became of the pull request a finished run opened over conflicts with its base, and the clean-up
//! once it merges cleanly, is merged or is closed, as orchestrator.py's refresh_command, ConflictCheck and
//! refresh_conflict do it. drain refreshes the runs of its specs so too (crate::cli::drain).

use super::start::find_run;
use super::{record_in_state, Connected};
use crate::constants::{EXIT_WAITING, RUNS_DIR, SUCCEEDED};
use crate::error::OrchestratorError;
use crate::pipeline::DONE;
use crate::pull_request::without_conflict_warning;
use crate::py::json::Json;

/// What gh says now of the pull request a run opened over conflicts with its base (has_conflict), and so what
/// refresh_conflict does about it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConflictCheck {
    /// The run's id, as its state saves it.
    pub run_id: String,
    /// The pull request's URL.
    pub url: String,
    /// OPEN, CLOSED or MERGED.
    pub pr_state: String,
    /// MERGEABLE, CONFLICTING, or UNKNOWN while GitHub has not computed it; it decides nothing for a pull request
    /// that is not OPEN.
    pub mergeable: String,
    /// The pull request's body with the run's conflict warning removed; None when it is not OPEN and MERGEABLE, or
    /// has no such warning left to remove.
    pub body: Option<String>,
    /// Whether clearing the conflict takes the pull request out of draft: it is OPEN, MERGEABLE and a draft, and the
    /// run's verdict is one of SUCCEEDED.
    pub ready: bool,
}

impl ConflictCheck {
    /// Whether the run's conflicts are forgotten: its pull request merges cleanly, or is merged or closed.
    pub fn clears(&self) -> bool {
        self.pr_state != "OPEN" || self.mergeable == "MERGEABLE"
    }

    /// What refresh prints for the run.
    pub fn line(&self) -> String {
        let (run_id, url) = (&self.run_id, &self.url);
        if self.pr_state != "OPEN" {
            return format!("{run_id}: {}, conflict forgotten", self.pr_state.to_lowercase());
        }
        match self.mergeable.as_str() {
            "MERGEABLE" => {
                let ready = if self.ready { ", ready for review" } else { "" };
                format!("{run_id}: conflict cleared{ready}  {url}")
            }
            "CONFLICTING" => format!("{run_id}: still conflicts  {url}"),
            _ => format!("{run_id}: mergeability not known yet, try again  {url}"),
        }
    }
}

/// Whether the run saved as saved is done, with a pull request it opened over conflicts it still records.
pub fn has_conflict(saved: &Json) -> bool {
    let truthy = |key: &str| saved.get(key).is_some_and(Json::truthy);
    saved.get("phase").and_then(Json::as_str) == Some(DONE) && truthy("pr_url") && truthy("conflicts")
}

/// Ask gh about the pull request of the run saved as saved, which has_conflict, and, unless dry_run, act on what it
/// says: for one that merges cleanly now, remove the conflict warning from its body, every other edit to the body
/// kept, take it out of draft if the run succeeded, and record no conflicts in the run's state; for one merged or
/// closed, only that record. One that still conflicts, or whose mergeability GitHub has not computed yet, is left as
/// it is. Returns what gh said, and so what was done.
///
/// cwd is the project, where gh runs and the run's state is, on world's host. Errors, naming the run: gh fails, or the
/// state changed since it was read as saved (record_in_state).
pub fn refresh_conflict(
    world: &Connected,
    cwd: &str,
    saved: &Json,
    dry_run: bool,
) -> Result<ConflictCheck, OrchestratorError> {
    let text = |key: &str| saved.get(key).map(Json::python_str).unwrap_or_default();
    let run_id = text("run_id");
    let refreshed = || -> Result<ConflictCheck, OrchestratorError> {
        let host = world.with.host;
        let url = text("pr_url");
        let pr = host.pull_request_merge(cwd, &url)?;
        let clean = pr.state == "OPEN" && pr.mergeable == "MERGEABLE";
        let body = clean.then(|| {
            let base_branch = saved.get("base_branch").filter(|b| !b.is_null()).map(Json::python_str);
            let conflicts: Vec<String> =
                saved.get("conflicts").and_then(Json::as_array).into_iter().flatten().map(Json::python_str).collect();
            without_conflict_warning(&pr.body, base_branch.as_deref(), &conflicts)
        });
        let succeeded = saved.get("verdict").and_then(Json::as_str).is_some_and(|v| SUCCEEDED.contains(&v));
        let check = ConflictCheck {
            run_id: run_id.clone(),
            url: url.clone(),
            body: body.filter(|b| *b != pr.body),
            ready: clean && pr.is_draft && succeeded,
            pr_state: pr.state,
            mergeable: pr.mergeable,
        };
        if dry_run || !check.clears() {
            return Ok(check);
        }
        if let Some(body) = &check.body {
            host.edit_pr_body(cwd, &url, body)?;
        }
        if check.ready {
            host.mark_pr_ready(cwd, &url)?;
        }
        let path = format!("{cwd}/{RUNS_DIR}/{run_id}/state.json");
        record_in_state(host, &path, saved, "conflicts", Json::Array(vec![]), "its pull request was refreshed")?;
        Ok(check)
    };
    refreshed().map_err(|e| OrchestratorError(format!("run {run_id}: {e}")))
}

/// Refresh the run run_ref names (find_run), or without one every run in cwd that has_conflict, in run id order,
/// printing a line each (ConflictCheck::line). Returns the exit status: 0 when each conflict was forgotten, or there
/// was none to refresh, and EXIT_WAITING when any pull request still conflicts or is not known to merge yet. Errors:
/// run_ref names no run, or one that is not done; and as refresh_conflict.
pub fn refresh_command(world: &Connected, cwd: &str, run_ref: Option<&str>) -> Result<i32, OrchestratorError> {
    let states = world.with.host.run_states(cwd)?;
    let mut runs: Vec<&Json> = states.iter().map(|(_, saved)| saved).collect();
    if let Some(run_ref) = run_ref {
        let (_, saved) = find_run(&states, run_ref, cwd)?;
        let run_id = saved.get("run_id").map(Json::python_str).unwrap_or_default();
        if saved.get("phase").and_then(Json::as_str) != Some(DONE) {
            return Err(OrchestratorError(format!(
                "run {run_id} is not done; refresh only looks at a finished run's pull request"
            )));
        }
        if !has_conflict(saved) {
            world.say(&format!("{run_id}: no conflict to clear"));
            return Ok(0);
        }
        runs = vec![saved];
    }
    runs.retain(|saved| has_conflict(saved));
    runs.sort_by_key(|saved| saved.get("run_id").map(Json::python_str).unwrap_or_default());
    let mut waiting = false;
    for saved in runs {
        let check = refresh_conflict(world, cwd, saved, false)?;
        world.say(&check.line());
        waiting = waiting || !check.clears();
    }
    Ok(if waiting { EXIT_WAITING } else { 0 })
}

#[cfg(test)]
mod tests;
