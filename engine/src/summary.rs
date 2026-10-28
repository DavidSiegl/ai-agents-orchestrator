//! What a run's records say once it ends: its turns as summary.md, the pull request and show print them, summary.md
//! itself, metrics.json for the metrics command, and the times and durations they are written in.

use crate::constants::DEFAULT_AGENT;
use crate::pipeline::Pipeline;
use crate::py::datetime::{civil_from_days, fromisoformat_timestamp};
use crate::py::json::{Json, Map};
use crate::py::text::{rstrip, strip};
use crate::state::RunState;

/// The version of run_metrics' shape, raised when a key changes its meaning or goes.
pub const METRICS_VERSION: i64 = 1;

/// seconds since the epoch as an ISO 8601 UTC time to the second, as datetime.isoformat writes it:
/// 2026-10-28T08:34:31+00:00.
pub fn iso_utc(seconds: i64) -> String {
    let (days, secs) = (seconds.div_euclid(86_400), seconds.rem_euclid(86_400));
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}+00:00", secs / 3600, secs % 3600 / 60, secs % 60)
}

/// The seconds since the epoch of an ISO 8601 time, as int(datetime.fromisoformat(iso).timestamp()) gives them, a time
/// without an offset taken as UTC (crate::py::datetime::fromisoformat_timestamp). Errors: iso is not a time
/// fromisoformat reads.
pub fn epoch_seconds(iso: &str) -> Result<i64, String> {
    fromisoformat_timestamp(iso).map(|seconds| seconds.trunc() as i64)
}

/// A duration as summary.md and the tables write it: 42s, 3m 07s, 2h 05m.
pub fn format_duration(seconds: i64) -> String {
    if seconds < 60 {
        format!("{seconds}s")
    } else if seconds < 3600 {
        format!("{}m {:02}s", seconds / 60, seconds % 60)
    } else {
        format!("{}h {:02}m", seconds / 3600, seconds % 3600 / 60)
    }
}

/// An age as list and show write it: 42s, 3m, 2h5m.
pub fn format_age(seconds: i64) -> String {
    if seconds < 60 {
        format!("{seconds}s")
    } else if seconds < 3600 {
        format!("{}m", seconds / 60)
    } else {
        format!("{}h{}m", seconds / 3600, seconds % 3600 / 60)
    }
}

/// value as Python's str() writes it: a string as it is, None, True and False by name, a number as JSON does.
fn py_str(value: Option<&Json>) -> String {
    match value {
        None | Some(Json::Null) => "None".into(),
        Some(Json::Str(s)) => s.clone(),
        Some(Json::Bool(b)) => if *b { "True" } else { "False" }.into(),
        Some(other) => other.dumps(),
    }
}

/// The cells of a turn record (RunState::turns) as the tables show them.
struct TurnCells {
    step: String,
    role: String,
    round: String,
    harness: String,
    model: String,
    start: String,
    duration: String,
    file: String,
}

impl TurnCells {
    /// A turn record's values as the tables show them. A start or duration that is not known shows as ?: a file
    /// written before turns were recorded, or a turn still open. A quality analysis runs in no harness.
    fn of(t: &Map<Json>) -> TurnCells {
        let truthy = |key: &str| t.get(key).filter(|v| v.truthy());
        let harness = truthy("agent");
        let round = match truthy("quality_round") {
            Some(q) => format!("{} q{}", py_str(t.get("round")), py_str(Some(q))),
            None => py_str(t.get("round")),
        };
        let model = match (truthy("model"), harness) {
            (Some(model), _) => py_str(Some(model)),
            (None, Some(_)) => "default".into(),
            (None, None) => "-".into(),
        };
        let start = match truthy("started_at") {
            Some(at) => {
                let at = py_str(Some(at)).replace('T', " ");
                at.strip_suffix("+00:00").unwrap_or(&at).to_string()
            }
            None => "?".into(),
        };
        let duration = match t.get("seconds") {
            None | Some(Json::Null) => "?".into(),
            Some(Json::Int(n)) => format_duration(i64::try_from(*n).unwrap_or(i64::MAX)),
            Some(other) => py_str(Some(other)),
        };
        TurnCells {
            step: py_str(t.get("step")),
            role: py_str(t.get("role")),
            round,
            harness: harness.map_or("-".into(), |h| py_str(Some(h))),
            model,
            start,
            duration,
            file: py_str(t.get("file")),
        }
    }
}

/// The turns as a Markdown table, the human-paced ones marked: their time is the human's as much as the agent's.
pub fn turns_table(turns: &[Map<Json>]) -> String {
    let head = ["step", "role", "round", "harness", "model", "start (UTC)", "duration", "file"].map(String::from);
    let mut rows = vec![head.to_vec(), vec!["---".to_string(); head.len()]];
    for t in turns {
        let mut c = TurnCells::of(t);
        if t.get("human_paced").is_some_and(Json::truthy) {
            c.step.push_str(" (human-paced)");
        }
        rows.push(vec![c.step, c.role, c.round, c.harness, c.model, c.start, c.duration, c.file]);
    }
    rows.iter()
        .map(|row| format!("| {} |", row.iter().map(|c| c.replace('|', "\\|")).collect::<Vec<_>>().join(" | ")))
        .collect::<Vec<_>>()
        .join("\n")
}

/// The turns as show prints them, a line each, in columns of step, round, harness, start, duration and file, each as
/// wide as its widest cell, in characters.
pub fn turn_lines(turns: &[Map<Json>]) -> Vec<String> {
    let rows: Vec<[String; 6]> = turns
        .iter()
        .map(|t| {
            let c = TurnCells::of(t);
            [c.step, c.round, c.harness, c.start, c.duration, c.file]
        })
        .collect();
    let widths: Vec<usize> = (0..6).map(|k| rows.iter().map(|r| r[k].chars().count()).max().unwrap_or(0)).collect();
    rows.iter()
        .map(|r| {
            let cells: Vec<String> = r.iter().zip(&widths).map(|(cell, w)| format!("{cell:<w$}")).collect();
            rstrip(&cells.join("  ")).to_string()
        })
        .collect()
}

/// The start of each turn that has one, in seconds since the epoch; a start that is not a time is left out, where
/// the Python engine would fail.
fn turn_starts(state: &RunState) -> Vec<i64> {
    state
        .turns
        .iter()
        .filter_map(|t| t.get("started_at").filter(|at| at.truthy()))
        .filter_map(|at| at.as_str().and_then(|at| epoch_seconds(at).ok()))
        .collect()
}

/// summary.md of a run that ended at now, in seconds since the epoch: how it turned out, who played each role, and
/// where its time went. rounds holds a line per round, with what its handoff files say (Workflow::round_results).
pub fn run_summary(state: &RunState, pipeline: &Pipeline, rounds: &[String], now: i64) -> String {
    let s = state;
    let outcome = match (&s.error, &s.verdict) {
        (Some(e), _) if !e.is_empty() => format!("error: {e}"),
        (_, Some(v)) if !v.is_empty() => v.clone(),
        _ => "unfinished".into(),
    };
    let wall = turn_starts(s).into_iter().min().map_or("?".to_string(), |start| format_duration(now - start));
    let roles = pipeline.roles().iter().map(|(role, label)| {
        let kind = s.agents.get(role).and_then(|a| a.get("kind")).filter(|k| !k.is_empty());
        let harness = kind.or_else(|| s.agent_kinds.get(role)).map_or(DEFAULT_AGENT, String::as_str);
        let model = s.models.get(role).filter(|m| !m.is_empty()).map_or("default", String::as_str);
        format!("| {label} ({role}) | {harness} | {model} |")
    });
    let pr = s.pr_url.as_deref().filter(|u| !u.is_empty()).unwrap_or("none");
    let branch = s.branch.as_deref().filter(|b| !b.is_empty()).map_or("none".to_string(), |b| format!("`{b}`"));
    let mut lines = vec![
        format!("# Run {}", s.run_id),
        String::new(),
        format!("- **Workflow:** {}", s.workflow),
        format!("- **Outcome:** {outcome}"),
        format!("- **Pull request:** {pr}"),
        format!("- **Branch:** {branch}"),
        format!("- **Wall time:** {wall}, from the first turn's start to {}", iso_utc(now)),
        String::new(),
        "## Task".into(),
        String::new(),
        strip(&s.task).to_string(),
        String::new(),
        "## Roles".into(),
        String::new(),
        "| role | harness | model |".into(),
        "| --- | --- | --- |".into(),
    ];
    lines.extend(roles);
    lines.extend([String::new(), "## Turns".into(), String::new()]);
    lines.push(if s.turns.is_empty() { "No turns were recorded.".into() } else { turns_table(&s.turns) });
    if !rounds.is_empty() {
        lines.extend([String::new(), "## Rounds".into(), String::new()]);
        lines.extend(rounds.iter().map(|line| format!("- {line}")));
    }
    lines.join("\n") + "\n"
}

/// metrics.json of a run that is done, at now, in seconds since the epoch: one object of the run's outcome, its
/// roles, its turns with their times and tokens, and its quality rounds with their fingerprints, never their
/// findings' text. docs/design.md gives every key.
pub fn run_metrics(state: &RunState, now: i64) -> Json {
    let s = state;
    let start = turn_starts(s).into_iter().min();
    let get = |t: &Map<Json>, key: &str| t.get(key).cloned().unwrap_or(Json::Null);
    let turns = s.turns.iter().map(|t| {
        let mut out = Map::new();
        out.insert("step", get(t, "step"));
        out.insert("role", get(t, "role"));
        out.insert("harness", get(t, "agent"));
        out.insert("model", get(t, "model"));
        out.insert("round", get(t, "round"));
        out.insert("quality_round", t.get("quality_round").cloned().unwrap_or(Json::Int(0)));
        out.insert("human_paced", Json::Bool(t.get("human_paced").is_some_and(Json::truthy)));
        out.insert("started_at", get(t, "started_at"));
        out.insert("ended_at", get(t, "ended_at"));
        out.insert("seconds", get(t, "seconds"));
        let tokens = match t.get("tokens").filter(|v| v.truthy()) {
            Some(tokens) => tokens.clone(),
            None => Json::from("unknown"),
        };
        out.insert("tokens", if t.get("agent").is_some_and(Json::truthy) { tokens } else { Json::Null });
        Json::Object(out)
    });
    let roles = s.agents.iter().map(|(role, agent)| {
        let mut out = Map::new();
        out.insert("harness", Json::from(agent.get("kind").map_or(DEFAULT_AGENT, String::as_str)));
        out.insert("model", Json::from(s.models.get(role).cloned()));
        (role, Json::Object(out))
    });
    let restored: usize = s
        .restored
        .iter()
        .filter_map(|r| r.get("paths").and_then(Json::as_object))
        .flat_map(|paths| paths.values())
        .map(|ps| ps.as_array().map_or(0, Vec::len))
        .sum();
    let outcome = match (&s.verdict, &s.error) {
        (Some(v), _) if !v.is_empty() => v.clone(),
        (_, Some(e)) if !e.is_empty() => "error".into(),
        _ => "unfinished".into(),
    };
    let mut out = Map::new();
    out.insert("version", Json::from(METRICS_VERSION));
    out.insert("run_id", Json::from(s.run_id.as_str()));
    out.insert("workflow", Json::from(s.workflow.as_str()));
    out.insert("outcome", Json::from(outcome));
    out.insert("rounds", Json::Int(s.round.into()));
    out.insert("max_rounds", Json::Int(s.max_rounds.into()));
    out.insert("quality_gate", Json::Bool(s.had_quality_gate()));
    out.insert("max_quality_rounds", if s.had_quality_gate() { Json::Int(s.max_quality_rounds.into()) } else { Json::Null });
    out.insert("pull_request", Json::from(s.pr_url.clone()));
    out.insert("started_at", Json::from(start.map(iso_utc)));
    out.insert("ended_at", Json::from(iso_utc(now)));
    out.insert("seconds", Json::from(start.map(|start| now - start)));
    out.insert("roles", Json::Object(roles.collect()));
    out.insert("turns", Json::Array(turns.collect()));
    out.insert("quality_rounds", Json::Array(s.quality_rounds.iter().cloned().map(Json::Object).collect()));
    out.insert("restored_locked_files", Json::Int(restored as i128));
    Json::Object(out)
}

#[cfg(test)]
mod tests;
