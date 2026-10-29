//! The state a run starts from: a new run's, from run's flags (new_state), and a stopped run's, from its saved state
//! and resume's flags (resumed_state), each with its workflow.

use std::io::Read;

use super::args::{ResumeArgs, RunArgs, Settings};
use super::Connected;
use crate::constants::{DEFAULT_MAX_QUALITY_ROUNDS, DEFAULT_MAX_ROUNDS, DEFAULT_TURN_TIMEOUT, RUNS_DIR, WORKTREES_DIR};
use crate::error::OrchestratorError;
use crate::pipeline::{Pipeline, DONE, QUALITY};
use crate::prompts::{load_prompts, Prompts};
use crate::py::json::{Json, Map};
use crate::runs::{run_health, RUNNING};
use crate::spec_file::SpecFile;
use crate::state::RunState;
use crate::summary::iso_utc;
use crate::workflow::{turn_quality_gate_off, turn_quality_gate_on};
use crate::workflow_file::{load_workflow_file, named_workflow, run_pipeline};

/// A new run's state from run's flags, and its workflow, for the project at cwd; with spec, run --spec's file, the run
/// starts at the step after the contract step. Its prompts are the prompt files' as they are now (load_prompts).
///
/// Errors: as load_prompts; the workflow cannot be found or loaded, or with spec has no step to start at; a
/// --role-model or --role-agent names a role the workflow lacks; no run id can be made.
pub fn new_state(
    args: &RunArgs,
    cwd: &str,
    spec: Option<&SpecFile>,
    world: &Connected,
) -> Result<(RunState, Pipeline), OrchestratorError> {
    let prompts = load_prompts(world.config)?;
    let pipeline = flagged_workflow(args, &prompts, world)?;
    let first = match spec {
        Some(_) => pipeline.seeded_start()?,
        None => &pipeline.steps()[0],
    };
    // parse_args leaves the task out only with --spec.
    let task = match (&args.task, spec) {
        (Some(task), _) => task.clone(),
        (None, Some(spec)) => spec.task(),
        (None, None) => String::new(),
    };
    let mut state = RunState::new(&new_run_id(world.with.clocks.wall())?, &task, cwd);
    state.machine = args.target.machine.clone();
    state.phase = first.id.clone();
    state.workflow = pipeline.name().to_string();
    state.prompt_overrides = prompts.overrides();
    let s = &args.settings;
    state.max_rounds = s.max_rounds.map_or(DEFAULT_MAX_ROUNDS, saturating_u32);
    state.turn_timeout = s.timeout.filter(|&t| t != 0).map_or(DEFAULT_TURN_TIMEOUT, |t| t.max(0).unsigned_abs());
    state.permission_mode = s.permission_mode.clone();
    state.models = merged(pipeline.models(), role_models(s, pipeline.roles())?);
    state.agent_kinds = merged(pipeline.agents(), role_agents(s, pipeline.roles())?);
    state.pull_request = !args.no_pr;
    // Its path from the start, so round 0 creates the worktree there.
    state.worktree = args.starts.worktree.then(|| format!("{cwd}/{WORKTREES_DIR}/{}", state.key()));
    state.quality_job = args.starts.quality_gate.clone();
    state.max_quality_rounds = s.max_quality_rounds.map_or(DEFAULT_MAX_QUALITY_ROUNDS, saturating_u32);
    Ok((state, pipeline))
}

/// The workflow run's --workflow-file or --workflow names, worded by prompts.
fn flagged_workflow(args: &RunArgs, prompts: &Prompts, world: &Connected) -> Result<Pipeline, OrchestratorError> {
    match &args.starts.workflow_file {
        Some(path) => load_workflow_file(path, prompts),
        None => named_workflow(world.config, &args.starts.workflow, prompts),
    }
}

/// A new run's id: the time wall (seconds since the epoch) names, in UTC, and six random hex digits, as
/// 20261029-142233-a1b2c3. Python's new_run_id names the local time; this engine has no time zone database to read.
/// Errors: the system's random bytes cannot be read.
fn new_run_id(wall: f64) -> Result<String, OrchestratorError> {
    let mut bytes = [0u8; 3];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|e| OrchestratorError(format!("cannot read /dev/urandom for a run id: {e}")))?;
    // iso_utc gives 2026-10-29T14:22:33+00:00.
    let time: String = iso_utc(wall.floor() as i64).chars().take(19).filter(char::is_ascii_digit).collect();
    let key: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    Ok(format!("{}-{}-{key}", &time[..8], &time[8..]))
}

/// The state of the run args.run_ref names among cwd's runs, as resumable_state finds it, and its workflow, with the
/// quality gate turned on or off as args asks (turn_quality_gate_on and _off: the job's preflight, and the run's
/// refs on origin deleted). Changes nothing on disk.
///
/// Errors: the run cannot be resumed (resumable_state), its gate cannot be switched, or the job's credentials
/// cannot be read.
pub fn resumed_state(args: &ResumeArgs, cwd: &str, world: &Connected) -> Result<(RunState, Pipeline), OrchestratorError> {
    let w = &world.with;
    let runs = w.host.run_states(cwd)?;
    let mut state = resumable_state(&runs, args, cwd, world.local_host, w.pid_alive)?;
    let pipeline = run_pipeline(&state.workflow, state.workflow_definition.as_ref(), &state.prompts()?)?;
    if let Some(job) = args.quality_gate.as_deref().filter(|j| !j.is_empty()) {
        turn_quality_gate_on(&mut state, &pipeline, w.host, w.human, &(world.jenkins)(job)?)?;
    } else if args.no_quality_gate {
        turn_quality_gate_off(&mut state, &pipeline, w.host, w.human)?;
    }
    Ok((state, pipeline))
}

/// The saved state of the run to resume among runs, each (seconds since its state.json was written, the state as
/// saved), with resume's settings flags applied and its machine the one args names.
///
/// The run's health is judged from local_host by pid_alive (crate::runs::run_health). Errors: no run or several
/// match args.run_ref; its state cannot be read, or its saved workflow or prompts; it was closed; it looks alive and
/// args.force is not given; it is past the round limit it would have; a --role-model or --role-agent names a role
/// its workflow lacks; or its quality round limit does not hold (resume_quality_limit).
pub fn resumable_state(
    runs: &[(u64, Json)],
    args: &ResumeArgs,
    cwd: &str,
    local_host: &str,
    pid_alive: &dyn Fn(i128) -> bool,
) -> Result<RunState, OrchestratorError> {
    let (age, saved) = find_run(runs, &args.run_ref, cwd)?;
    let saved_map = saved.as_object().ok_or_else(|| OrchestratorError("a run state is a JSON object".into()))?;
    let mut state = RunState::from_saved(saved_map)?;
    // Not even with --force: close may have deleted its worktree and branch.
    if state.closed.is_some() {
        return Err(OrchestratorError(format!("run {} was closed; start a new run", state.run_id)));
    }
    let pipeline = run_pipeline(&state.workflow, state.workflow_definition.as_ref(), &state.prompts()?)?;
    if let Some((RUNNING, why)) = run_health(saved, *age, local_host, pid_alive) {
        if !args.force {
            return Err(OrchestratorError(format!(
                "run {} looks alive ({why}); pass --force to take it over",
                state.run_id
            )));
        }
    }
    let s = &args.settings;
    if let Some(n) = s.max_rounds {
        state.max_rounds = saturating_u32(n);
    }
    if let Some(t) = s.timeout {
        state.turn_timeout = t.max(0).unsigned_abs();
    }
    if let Some(mode) = s.permission_mode.as_ref().filter(|m| !m.is_empty()) {
        state.permission_mode = Some(mode.clone());
    }
    state.models = merged(&state.models, role_models(s, pipeline.roles())?);
    // Taken up by each role's next agent; one still running keeps its harness.
    state.agent_kinds = merged(&state.agent_kinds, role_agents(s, pipeline.roles())?);
    if state.phase != DONE && state.max_rounds < state.round {
        return Err(OrchestratorError(format!(
            "run {} is already in round {}; --max-rounds {} is too low",
            state.run_id, state.round, state.max_rounds
        )));
    }
    resume_quality_limit(&mut state, &pipeline, args)?;
    // The herdr this resume addresses: the saved one may name the same herdr reached another way.
    state.machine = args.target.machine.clone();
    Ok(state)
}

/// The run of runs named by its full id, or else by its key. Errors: none or several match.
fn find_run<'r>(runs: &'r [(u64, Json)], run_ref: &str, cwd: &str) -> Result<&'r (u64, Json), OrchestratorError> {
    let id = |run: &(u64, Json)| run.1.get("run_id").map(|v| v.as_str().map_or_else(|| v.python_str(), String::from));
    let mut matches: Vec<_> = runs.iter().filter(|r| id(r).as_deref() == Some(run_ref)).collect();
    if matches.is_empty() {
        let key = |r: &(u64, Json)| id(r).unwrap_or_default().rsplit('-').next().map(String::from);
        matches = runs.iter().filter(|r| key(r).as_deref() == Some(run_ref)).collect();
    }
    match matches[..] {
        [] => Err(OrchestratorError(format!("no run {run_ref} under {cwd}/{RUNS_DIR}"))),
        [one] => Ok(one),
        _ => {
            let ids: Vec<String> = matches.iter().map(|r| id(r).unwrap_or_default()).collect();
            Err(OrchestratorError(format!("{run_ref} matches several runs ({}); give the full run id", ids.join(", "))))
        }
    }
}

/// Apply resume's --max-quality-rounds to state, of workflow pipeline, and check the limit, given or saved, against
/// where the run is. Errors: the flag is given to a run without the quality gate, unless args turn it on; or the run
/// is already past the limit.
fn resume_quality_limit(state: &mut RunState, pipeline: &Pipeline, args: &ResumeArgs) -> Result<(), OrchestratorError> {
    if let Some(n) = args.settings.max_quality_rounds {
        let gate_on = args.quality_gate.as_deref().is_some_and(|j| !j.is_empty());
        if state.quality_job.as_deref().is_none_or(str::is_empty) && !gate_on {
            return Err(OrchestratorError(format!(
                "run {} has no quality gate, so --max-quality-rounds does not apply",
                state.run_id
            )));
        }
        state.max_quality_rounds = saturating_u32(n);
    }
    let (q, limit) = (state.quality_round, state.max_quality_rounds);
    // In quality, round q is analysed whatever the limit; in the gated step with q >= 1, it is answering quality round
    // q, and only a round q + 1 would judge that answer.
    let in_gated = pipeline.gated().is_some_and(|g| g.id == state.phase);
    if (state.phase == QUALITY && limit < q) || (in_gated && q >= 1 && limit <= q) {
        return Err(OrchestratorError(format!(
            "run {} is already in quality round {q} of its {} phase; --max-quality-rounds {limit} is too low",
            state.run_id, state.phase
        )));
    }
    Ok(())
}

/// The model each of roles (role to label) starts with by the flags: --role-model's, else the default workflow's
/// --ROLE-model, else --model; a role with none is left out, keeping its workflow's, or its harness's.
///
/// Errors: a --role-model names a role not among roles.
fn role_models(s: &Settings, roles: &Map<String>) -> Result<Map<String>, OrchestratorError> {
    let named = named_roles("--role-model", &s.role_model, roles)?;
    let label = |role: &str| s.label_models.iter().find(|(r, _)| r == role).map(|(_, m)| m.as_str());
    Ok(roles
        .keys()
        .filter_map(|role| {
            let model = [named.get(role).map(String::as_str), label(role), s.model.as_deref()]
                .into_iter()
                .flatten()
                .find(|m| !m.is_empty())?;
            Some((role, model.to_string()))
        })
        .collect())
}

/// The harness each of roles runs in by the flags, each one of AGENT_KINDS as the parser takes them: --role-agent's,
/// else --agent's; a role with none is left out,
/// keeping its workflow's, or the saved one. Errors: a --role-agent names a role not among roles.
fn role_agents(s: &Settings, roles: &Map<String>) -> Result<Map<String>, OrchestratorError> {
    let named = named_roles("--role-agent", &s.role_agent, roles)?;
    Ok(roles
        .keys()
        .filter_map(|role| named.get(role).cloned().or_else(|| s.agent.clone()).map(|kind| (role, kind)))
        .collect())
}

/// flag's ROLE=VALUE pairs by role, a later pair for a role replacing an earlier one. Errors: a role not among roles,
/// naming the first by name and the roles there are.
fn named_roles(flag: &str, pairs: &[(String, String)], roles: &Map<String>) -> Result<Map<String>, OrchestratorError> {
    let named: Map<String> = pairs.iter().map(|(r, v)| (r.as_str(), v.clone())).collect();
    if let Some(unknown) = named.keys().filter(|r| !roles.contains_key(r)).min() {
        let roles: Vec<&str> = roles.keys().collect();
        return Err(OrchestratorError(format!(
            "{flag} names role {unknown}, which the workflow does not have; its roles are {}",
            roles.join(", ")
        )));
    }
    Ok(named)
}

/// base with each of over's entries put in, a key of base keeping its place: Python's {**base, **over}.
fn merged(base: &Map<String>, over: Map<String>) -> Map<String> {
    let mut out = base.clone();
    for (k, v) in over.iter() {
        out.insert(k, v.clone());
    }
    out
}

/// n as a u32, the nearest one for an n out of its range.
fn saturating_u32(n: i64) -> u32 {
    u32::try_from(n.max(0)).unwrap_or(u32::MAX)
}

#[cfg(test)]
mod tests;
