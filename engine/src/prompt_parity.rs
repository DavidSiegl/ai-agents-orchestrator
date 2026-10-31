//! The prompts the e2e scenarios recorded (engine/e2e/recorded/), checked against the templates this engine
//! holds: each prompt an agent got must be made of the run's step templates and the built-in prompts, joined by
//! blank lines, the step's own template first, with every placeholder whose value the run's state.json gives filled
//! in with that value. That holds every template's text to the Python engine's byte for byte, and its placeholders
//! to the values the Python engine filled in; which notes a prompt carries, and in what order, is Workflow's to
//! decide and to be checked against these recordings when it is ported.

use std::collections::BTreeSet;

use crate::constants::{APPROVE, CHANGES_REQUESTED};
use crate::pipeline::{Pipeline, Step};
use crate::prompts::Prompt;
use crate::py::format::parse;
use crate::py::json::{Json, Map};
use crate::state::RunState;
use crate::test_support::recordings;
use crate::workflow_file::saved_pipeline;

/// A piece of a template's text: literal text, or a placeholder whose value is not known here.
#[derive(Clone, Debug)]
enum Piece {
    Text(String),
    Any,
}

/// template as pieces, each placeholder that known gives a value filled in.
fn pieces(template: &str, known: &dyn Fn(&str) -> Option<String>) -> Vec<Piece> {
    let mut out = Vec::new();
    for chunk in parse(template).expect("a template that parses") {
        out.push(Piece::Text(chunk.literal));
        if let Some(field) = chunk.field {
            out.push(known(&field.name).map_or(Piece::Any, Piece::Text));
        }
    }
    out
}

/// Every place in text that the pieces, matched from at, can end at.
fn ends(text: &str, at: usize, pieces: &[Piece]) -> BTreeSet<usize> {
    let mut positions = BTreeSet::from([at]);
    for piece in pieces {
        let mut next = BTreeSet::new();
        for &pos in &positions {
            match piece {
                Piece::Text(literal) => {
                    if text[pos..].starts_with(literal.as_str()) {
                        next.insert(pos + literal.len());
                    }
                }
                Piece::Any => next.extend((pos..=text.len()).filter(|&i| text.is_char_boundary(i))),
            }
        }
        positions = next;
    }
    positions
}

/// Whether text is one of firsts, then any of others, each after a blank line, to its end.
fn made_of(text: &str, firsts: &[Vec<Piece>], others: &[Vec<Piece>]) -> bool {
    let mut reached: BTreeSet<usize> = firsts.iter().flat_map(|first| ends(text, 0, first)).collect();
    let mut queue: Vec<usize> = reached.iter().copied().collect();
    while let Some(pos) = queue.pop() {
        if pos == text.len() {
            return true;
        }
        if !text[pos..].starts_with("\n\n") {
            continue;
        }
        for other in others {
            for end in ends(text, pos + 2, other) {
                if reached.insert(end) {
                    queue.push(end);
                }
            }
        }
    }
    false
}

/// The run's files of round n of step, oldest first, as Workflow::reports finds them: its file, and for the gated
/// step its answers to quality rounds, as far as they exist among files, the paths of the run directory's files.
fn reports(s: &RunState, st: &Step, n: u32, files: &BTreeSet<String>) -> Vec<String> {
    let dir = s.dir();
    let mut out = vec![st.path(&dir, n, 0)];
    while s.had_quality_gate() && st.quality_gated && files.contains(&st.path(&dir, n, out.len() as u32)) {
        out.push(st.path(&dir, n, out.len() as u32));
    }
    out
}

/// The value of a placeholder of step's prompts in round n, as Workflow::placeholder gives it, from the state
/// alone; None for a name that is not a placeholder.
fn placeholder(name: &str, s: &RunState, p: &Pipeline, step: &Step, n: u32, files: &BTreeSet<String>) -> Option<String> {
    let dir = s.dir();
    let q = if step.quality_gated { s.quality_round } else { 0 };
    let own = step.path(&dir, n, q);
    Some(match name {
        "task" => s.task.clone(),
        "cwd" => s.work_dir().to_string(),
        "n" => n.to_string(),
        "approve" => APPROVE.to_string(),
        "changes" => CHANGES_REQUESTED.to_string(),
        "proofs_dir" => s.proofs_dir(n),
        "path" => own,
        "change" => match &s.base {
            Some(base) => format!("`git diff {base}` in {}, plus the untracked files `git status --porcelain` lists", s.work_dir()),
            None => format!("the files the Builder's report lists, in {} (not a git repository)", s.work_dir()),
        },
        _ => {
            let (_, prefix, other) = p.path_placeholders().into_iter().find(|(x, _, _)| x == name)?;
            match prefix {
                "prev_" => reports(s, other, n.saturating_sub(1), files).pop()?,
                "earlier_" => {
                    let mut all: Vec<String> = (1..n).flat_map(|i| reports(s, other, i, files)).collect();
                    if other.id == step.id && step.quality_gated {
                        all.extend((0..s.quality_round).map(|j| step.path(&dir, n, j)));
                    }
                    all.join(", ")
                }
                _ if other.id == step.id => own,
                _ => other.last_path(&dir, n, s.quality_round),
            }
        }
    })
}

/// The templates a prompt to role may start with in the state s, their known placeholders filled in: the current
/// step's prompt, again and a fresh session's prompt, and the follow-ups of the built-in prompts.
fn firsts(s: &RunState, p: &Pipeline, role: &str, files: &BTreeSet<String>) -> Vec<Vec<Piece>> {
    let prompts = s.prompts().expect("a run's prompts");
    let Some(step) = p.step(&s.phase).or_else(|| p.gated()).filter(|st| st.role == role) else { return Vec::new() };
    let n = s.round;
    let known = |name: &str| placeholder(name, s, p, step, n, files);
    let own = step.path(&s.dir(), n, if step.quality_gated { s.quality_round } else { 0 });
    let quality_fix = |name: &str| match name {
        "quality_path" => Some(s.quality_path(n, s.quality_round)),
        "report_path" => Some(own.clone()),
        _ => None,
    };
    let mut out = vec![pieces(&step.prompt, &known)];
    if let Some(again) = &step.again {
        out.push(pieces(again, &known));
    }
    out.push(pieces(prompts.text(Prompt::QualityFixPrompt), &quality_fix));
    let path = |name: &str| (name == "path").then(|| own.clone());
    out.push(pieces(prompts.text(Prompt::ContinuePrompt), &path));
    out.push(pieces(prompts.text(Prompt::BlockedPrompt), &path));
    let retry = |name: &str| match name {
        "approve" => Some(APPROVE.to_string()),
        "changes" => Some(CHANGES_REQUESTED.to_string()),
        _ => path(name),
    };
    out.push(pieces(prompts.text(Prompt::RetryVerdictPrompt), &retry));
    out
}

/// What may follow a prompt's first template: the step's other templates, as a fresh session hears them, and every
/// note, their placeholders unknown.
fn others(s: &RunState, p: &Pipeline) -> Vec<Vec<Piece>> {
    let prompts = s.prompts().expect("a run's prompts");
    let none = |_: &str| None;
    let mut out: Vec<Vec<Piece>> = p.steps().iter().flat_map(Step::templates).map(|t| pieces(t, &none)).collect();
    out.extend(Prompt::ALL.iter().map(|q| pieces(prompts.text(*q), &none)));
    out
}

/// The latest state.json saved of each run, by its number in the recording's names, as the events give them.
fn latest_states(events: &[Json], into: &mut Map<Map<Json>>) -> Vec<(String, String, Map<Json>)> {
    let mut prompts = Vec::new();
    for event in events {
        if let Some(Json::Object(saved)) = event.get("state").and_then(|s| s.get("saved")) {
            let run = saved.get("run_id").and_then(Json::as_str).unwrap_or_default().to_string();
            into.insert(run, saved.clone());
        }
        let Some(args) = event.get("herdr").and_then(Json::as_array) else { continue };
        let args: Vec<&str> = args.iter().filter_map(Json::as_str).collect();
        if let ["agent", "prompt", name, text] = args[..] {
            let (role, key) = name.rsplit_once("-<key:").expect("an agent named after its run's key");
            let run = format!("<run:{key}");
            let saved = into.get(&run).cloned().unwrap_or_else(|| panic!("no state of {run} before {name}'s prompt"));
            prompts.push((role.to_string(), text.to_string(), saved));
        }
    }
    prompts
}

#[test]
fn every_recorded_prompt_is_made_of_this_engines_templates_filled_in_as_the_python_engine_did() {
    let mut checked = 0;
    for (scenario, recording) in recordings() {
        let files: BTreeSet<String> = recording
            .get("run_files")
            .and_then(Json::as_object)
            .map(|files| files.keys().map(String::from).collect())
            .unwrap_or_default();
        let events = recording.get("events").and_then(Json::as_object).cloned().unwrap_or_default();
        let mut states = Map::new();
        for (_, invocation) in events {
            for (role, text, saved) in latest_states(invocation.as_array().unwrap(), &mut states) {
                let state = RunState::from_saved(&saved).unwrap();
                let pipeline = saved_pipeline(&saved).unwrap_or_else(|| panic!("{scenario}: no workflow"));
                // The run files, named relative to the project's .orchestrator directory, as paths.
                let paths: BTreeSet<String> = files.iter().map(|f| format!("{}/.orchestrator/{f}", state.cwd)).collect();
                let firsts = firsts(&state, &pipeline, &role, &paths);
                assert!(
                    made_of(&text, &firsts, &others(&state, &pipeline)),
                    "{scenario}: the prompt to {role} in phase {} round {} is not made of the templates:\n{text}",
                    state.phase,
                    state.round
                );
                checked += 1;
            }
        }
    }
    assert!(checked >= 130, "{checked} prompts checked");
}

#[test]
fn a_prompt_with_a_word_of_its_own_is_not_made_of_the_templates() {
    let firsts = vec![pieces("Build {task} in {cwd}.", &|name| (name == "cwd").then(|| "/proj".to_string()))];
    let others = vec![pieces("Note {x}.", &|_| None)];
    assert!(made_of("Build it in /proj.", &firsts, &others));
    assert!(made_of("Build it in /proj.\n\nNote a.\n\nNote b.", &firsts, &others));
    assert!(!made_of("Build it in /elsewhere.", &firsts, &others));
    assert!(!made_of("Build it in /proj.\n\nNote a!", &firsts, &others));
    assert!(!made_of("Note a.\n\nBuild it in /proj.", &firsts, &others));
}
