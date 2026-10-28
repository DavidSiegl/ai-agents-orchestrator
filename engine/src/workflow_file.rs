//! Workflow files: a workflow's definition as TOML in the configuration directory, or as saved in a run's
//! state.json, read into a Pipeline and written back.
//!
//! A workflow file is TOML holding a workflow's definition, named after the file:
//!
//! ```text
//! description = "..."                      # optional, shown by `workflows`
//! max_files = 15                           # optional: the change's budget (Pipeline), 15 files and 600 lines
//! max_lines = 600                          # when left out
//! proof_command = "python -m pytest {proofs}"   # optional: runs the Reviewer's proofs (Pipeline)
//! [roles]                                  # optional
//! build = "Builder"                        # a role's pane label,
//! tests = { label = "Test Writer", model = "sonnet", agent = "codex" }   # or its label, default model
//!                                                                       # and harness (AGENT_KINDS)
//! [[steps]]                                # one table per step, in order
//! use = "build"                            # optional: start from that step of the default workflow
//! id = "build"                             # and Step's fields; role defaults to id
//! ```
//!
//! A role the [roles] table leaves out, or gives no label, is labelled after the default workflow's role of that
//! key, or else after the key itself. The panes are laid out in the order the steps first use the roles.
//! workflow_definition writes the canonical form, which such a file may also be: every role with its label, and every
//! step with its fields.
//!
//! The toml crate gives a table's keys sorted, where Python's tomllib gives them in the file's order, so a file with
//! two problems in one table may be refused naming the other one first. A definition saved in state.json keeps its
//! order.

use crate::constants::{BUDGET_KEYS, DEFAULT_MAX_FILES, DEFAULT_MAX_LINES};
use crate::env::ConfigDir;
use crate::error::{strerror, OrchestratorError};
use crate::pipeline::{default_workflow, find_workflow, Pipeline, PipelineSettings, Step, DEFAULT_WORKFLOW, WORKFLOWS};
use crate::prompts::{files_with_extension, saved_prompts, Prompts};
use crate::py::format::field_names;
use crate::py::json::{unicode_string, Json, Map};
use crate::py::text::{basename, close_match, splitext, title_ascii};

/// The keys a workflow definition's top table may have.
pub const WORKFLOW_KEYS: [&str; 6] = ["description", "roles", "steps", "proof_command", "max_files", "max_lines"];
/// The keys a role's table may have.
pub const ROLE_KEYS: [&str; 3] = ["label", "model", "agent"];
/// A step's keys: Step's fields, in their order.
pub const STEP_KEYS: [&str; 12] = [
    "id",
    "role",
    "file",
    "prompt",
    "again",
    "fresh_note",
    "fresh_repeats_again",
    "human_paced",
    "edits",
    "loop_to",
    "quality_gated",
    "locks",
];
/// The steps' flags, the fields of STEP_KEYS that are false by default.
const STEP_FLAGS: [&str; 5] = ["fresh_repeats_again", "human_paced", "edits", "quality_gated", "locks"];

/// Whether name may name a role, a step or a workflow, which name agents, panes, phases and prompt placeholders:
/// letters, digits, - and _, at least one.
fn is_name(name: &str) -> bool {
    !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

/// Name -> path of each workflow file in config's workflows_dir, unparsed, in name order; none without the
/// directory. Errors: the directory cannot be listed.
pub fn workflow_files(config: &ConfigDir) -> Result<Map<String>, OrchestratorError> {
    files_with_extension(&config.workflows_dir(), ".toml")
}

/// The names of the built-in workflows and the workflow files, in order.
pub fn workflow_names(config: &ConfigDir) -> Result<Vec<String>, OrchestratorError> {
    let mut names: Vec<String> = WORKFLOWS.iter().map(|n| n.to_string()).collect();
    for name in workflow_files(config)?.keys() {
        if !names.iter().any(|n| n == name) {
            names.push(name.to_string());
        }
    }
    names.sort();
    Ok(names)
}

/// The built-in workflow of that name, or else the one in config's workflows_dir, worded by prompts as
/// load_workflow_file words one. Errors: no workflow has that name, or its file cannot be read or defines none.
pub fn named_workflow(config: &ConfigDir, name: &str, prompts: &Prompts) -> Result<Pipeline, OrchestratorError> {
    if WORKFLOWS.contains(&name) {
        return find_workflow(name, prompts);
    }
    if let Some(path) = workflow_files(config)?.get(name) {
        return load_workflow_file(path, prompts);
    }
    Err(OrchestratorError(format!("unknown workflow {name}; there are {}", workflow_names(config)?.join(", "))))
}

/// The workflow the file at path defines, named after the file, a step that uses one of the default workflow's
/// taking its texts from prompts. Errors, naming the file: it cannot be read, is not TOML, takes a built-in
/// workflow's name or one that is not a name, or defines no valid workflow (parse_workflow).
pub fn load_workflow_file(path: &str, prompts: &Prompts) -> Result<Pipeline, OrchestratorError> {
    let name = splitext(basename(path)).0;
    let bytes = std::fs::read(path)
        .map_err(|e| OrchestratorError(format!("cannot read workflow file {path}: {}", strerror(&e))))?;
    let definition = String::from_utf8(bytes)
        .map_err(|e| e.to_string())
        .and_then(|text| parse_toml(&text))
        .map_err(|e| OrchestratorError(format!("{path}: {e}")))?;
    if WORKFLOWS.contains(&name) {
        return Err(OrchestratorError(format!("{path}: workflow {name} is built in; give the file another name")));
    }
    if !is_name(name) {
        return Err(OrchestratorError(format!(
            "{path}: a workflow's name, its file's, may use only letters, digits, - and _"
        )));
    }
    parse_workflow(name, &definition, prompts).map_err(|e| OrchestratorError(format!("{path}: {e}")))
}

/// The table TOML text holds, as Json; the parser's error, with its line and column, for text that is not TOML.
pub fn parse_toml(text: &str) -> Result<Json, String> {
    toml::from_str::<Json>(text).map_err(|e| {
        let message = e.message().trim_end();
        match e.span() {
            Some(span) => {
                let before = &text[..span.start.min(text.len())];
                let line = before.matches('\n').count() + 1;
                let column = before.rsplit('\n').next().unwrap_or(before).chars().count() + 1;
                format!("{message} (at line {line}, column {column})")
            }
            None => message.to_string(),
        }
    })
}

/// The workflow a definition describes, as a workflow file or a run's state.json holds it, a step with a use taking
/// the fields of that step of the default workflow as prompts words it; what is wrong with it otherwise.
pub fn parse_workflow(name: &str, definition: &Json, prompts: &Prompts) -> Result<Pipeline, String> {
    let empty = Map::new();
    let definition = definition.as_object().unwrap_or(&empty);
    check_keys(definition, &WORKFLOW_KEYS, "the file")?;
    let raw_steps = match definition.get("steps").and_then(Json::as_array) {
        Some(steps) if !steps.is_empty() => steps,
        _ => return Err("it has no [[steps]]".into()),
    };
    let default = default_workflow(prompts);
    let steps = raw_steps.iter().enumerate().map(|(i, raw)| parse_step(i + 1, raw, &default)).collect::<Result<Vec<_>, _>>()?;
    let declared = match definition.get("roles") {
        None => Map::new(),
        Some(Json::Object(raw_roles)) => {
            raw_roles.iter().map(|(role, raw)| parse_role(role, raw).map(|table| (role, table))).collect::<Result<_, _>>()?
        }
        Some(_) => return Err("roles must be a table".into()),
    };
    // The panes follow the steps; a declared role no step uses goes last, for Pipeline to reject.
    let mut labels: Map<String> = Map::new();
    for role in steps.iter().map(|st| st.role.as_str()).chain(declared.keys()) {
        if !labels.contains_key(role) {
            let label = declared.get(role).and_then(|t: &Map<String>| t.get("label")).filter(|l| !l.is_empty());
            labels.insert(role, label.cloned().unwrap_or_else(|| default_label(role)));
        }
    }
    let role_setting = |key: &str| -> Map<String> {
        declared
            .iter()
            .filter_map(|(role, table)| table.get(key).filter(|v| !v.is_empty()).map(|v| (role, v.clone())))
            .collect()
    };
    let description = match definition.get("description") {
        None => String::new(),
        Some(Json::Str(s)) => s.clone(),
        Some(_) => return Err("description must be a string".into()),
    };
    let proof_command = match definition.get("proof_command") {
        None | Some(Json::Null) => None,
        Some(Json::Str(s)) => Some(s.clone()),
        Some(_) => return Err("proof_command must be a string".into()),
    };
    let mut budget = [DEFAULT_MAX_FILES, DEFAULT_MAX_LINES];
    for (key, slot) in BUDGET_KEYS.iter().zip(budget.iter_mut()) {
        if let Some(value) = definition.get(key) {
            // A bool is no number of lines, though Python's isinstance takes true for an int.
            let number = value.as_int().and_then(|n| i64::try_from(n).ok());
            *slot = number.ok_or_else(|| format!("{key} must be a whole number"))?;
        }
    }
    let settings = PipelineSettings {
        models: role_setting("model"),
        description,
        agents: role_setting("agent"),
        max_files: budget[0],
        max_lines: budget[1],
        proof_command,
    };
    Pipeline::with(name, labels, steps, settings)
}

/// The ROLE_KEYS a role's entry in [roles] sets: a string is its label.
fn parse_role(role: &str, raw: &Json) -> Result<Map<String>, String> {
    let place = format!("role {role}");
    if !is_name(role) {
        return Err(format!("{place}: a role key may use only letters, digits, - and _"));
    }
    let table = match raw {
        Json::Str(label) => return Ok([("label", label.clone())].into_iter().collect()),
        Json::Object(table) => table,
        _ => return Err(format!("{place}: give a label, or a table with label, model and agent")),
    };
    check_keys(table, &ROLE_KEYS, &place)?;
    table
        .iter()
        .map(|(key, value)| match value {
            Json::Str(s) => Ok((key, s.clone())),
            _ => Err(format!("{place}: {key} must be a string")),
        })
        .collect()
}

/// Step i of a definition, its use resolved against default, the default workflow.
fn parse_step(i: usize, raw: &Json, default: &Pipeline) -> Result<Step, String> {
    let Json::Object(raw) = raw else {
        return Err(format!("step {i} must be a table"));
    };
    let place = match raw.get("id") {
        Some(Json::Str(id)) => format!("step {i} ({id})"),
        _ => format!("step {i}"),
    };
    let mut keys: Vec<&str> = STEP_KEYS.to_vec();
    keys.push("use");
    check_keys(raw, &keys, &place)?;
    let mut fields = used_step(raw, &place, default)?;
    for (key, value) in raw.iter().filter(|(key, _)| *key != "use") {
        fields.insert(key, value.clone());
    }
    if !fields.contains_key("role") {
        fields.insert("role", fields.get("id").cloned().unwrap_or(Json::Null));
    }
    for key in ["id", "file", "prompt"] {
        if !fields.contains_key(key) {
            return Err(format!("{place}: {key} is missing"));
        }
    }
    for (key, value) in fields.iter() {
        check_step_value(key, value, &place)?;
    }
    let text = |key: &str| fields.get(key).and_then(Json::as_str).map(String::from);
    let flag = |key: &str| fields.get(key).and_then(Json::as_bool).unwrap_or(false);
    let mut st = Step::new(
        &text("id").unwrap_or_default(),
        &text("role").unwrap_or_default(),
        &text("file").unwrap_or_default(),
        &text("prompt").unwrap_or_default(),
    );
    st.again = text("again");
    st.fresh_note = text("fresh_note");
    st.fresh_repeats_again = flag("fresh_repeats_again");
    st.human_paced = flag("human_paced");
    st.edits = flag("edits");
    st.loop_to = text("loop_to");
    st.quality_gated = flag("quality_gated");
    st.locks = flag("locks");
    Ok(st)
}

/// The fields of the step of default, the default workflow, that raw's use names; none without a use.
fn used_step(raw: &Map<Json>, place: &str, default: &Pipeline) -> Result<Map<Json>, String> {
    let Some(used) = raw.get("use") else { return Ok(Map::new()) };
    match used.as_str().and_then(|id| default.step(id)) {
        Some(st) => Ok(step_fields(st)),
        None => {
            let ids: Vec<&str> = default.steps().iter().map(|st| st.id.as_str()).collect();
            Err(format!("{place}: use names no step of the default workflow; it has {}", ids.join(", ")))
        }
    }
}

fn check_step_value(key: &str, value: &Json, place: &str) -> Result<(), String> {
    if STEP_FLAGS.contains(&key) {
        return match value {
            Json::Bool(_) => Ok(()),
            _ => Err(format!("{place}: {key} must be true or false")),
        };
    }
    let Json::Str(text) = value else {
        return Err(format!("{place}: {key} must be a string"));
    };
    if (key == "id" || key == "role") && !is_name(text) {
        return Err(format!("{place}: {key} may use only letters, digits, - and _"));
    }
    field_names(text)
        .map(|_| ())
        .map_err(|e| format!("{place}: {key}: {e}; write a literal brace as {{{{ or }}}}"))
}

/// An error naming the first key of table that is not one of known, with the known key closest to it, or else all
/// of them.
fn check_keys(table: &Map<Json>, known: &[&str], place: &str) -> Result<(), String> {
    for key in table.keys() {
        if known.contains(&key) {
            continue;
        }
        let hint = match close_match(key, known.iter().copied()) {
            Some(close) => format!("did you mean {close}?"),
            None => {
                let mut sorted = known.to_vec();
                sorted.sort();
                format!("it takes {}", sorted.join(", "))
            }
        };
        return Err(format!("{place}: unknown key {key}; {hint}"));
    }
    Ok(())
}

/// The label of a role no table labels: the default workflow's role of that key's, else the key with each run of -
/// and _ as a space, title-cased.
fn default_label(role: &str) -> String {
    if let Some(label) = DEFAULT_WORKFLOW.roles().get(role) {
        return label.clone();
    }
    let mut spaced = String::new();
    let mut separating = false;
    for c in role.chars() {
        let separator = c == '-' || c == '_';
        if !separator {
            spaced.push(c);
        } else if !separating {
            spaced.push(' ');
        }
        separating = separator;
    }
    title_ascii(&spaced)
}

/// The step's fields that differ from their defaults, in declaration order.
pub fn step_fields(st: &Step) -> Map<Json> {
    let mut fields = Map::new();
    fields.insert("id", Json::from(st.id.as_str()));
    fields.insert("role", Json::from(st.role.as_str()));
    fields.insert("file", Json::from(st.file.as_str()));
    fields.insert("prompt", Json::from(st.prompt.as_str()));
    let optional = [("again", &st.again), ("fresh_note", &st.fresh_note)];
    for (key, value) in optional {
        if let Some(text) = value {
            fields.insert(key, Json::from(text.as_str()));
        }
    }
    let flags = [("fresh_repeats_again", st.fresh_repeats_again), ("human_paced", st.human_paced), ("edits", st.edits)];
    for (key, on) in flags {
        if on {
            fields.insert(key, Json::Bool(true));
        }
    }
    if let Some(target) = &st.loop_to {
        fields.insert("loop_to", Json::from(target.as_str()));
    }
    for (key, on) in [("quality_gated", st.quality_gated), ("locks", st.locks)] {
        if on {
            fields.insert(key, Json::Bool(true));
        }
    }
    fields
}

/// The canonical definition of a workflow, which parse_workflow turns back into it: description, max_files,
/// max_lines and proof_command where they are not the default, every role with its label, model and agent, and
/// every step's step_fields.
pub fn workflow_definition(p: &Pipeline) -> Json {
    let mut d = Map::new();
    if !p.description().is_empty() {
        d.insert("description", Json::from(p.description()));
    }
    if p.max_files() != DEFAULT_MAX_FILES {
        d.insert("max_files", Json::from(p.max_files()));
    }
    if p.max_lines() != DEFAULT_MAX_LINES {
        d.insert("max_lines", Json::from(p.max_lines()));
    }
    if let Some(command) = p.proof_command().filter(|c| !c.is_empty()) {
        d.insert("proof_command", Json::from(command));
    }
    let roles = p
        .roles()
        .iter()
        .map(|(role, label)| {
            let mut table = Map::new();
            table.insert("label", Json::from(label.as_str()));
            for (key, setting) in [("model", p.models()), ("agent", p.agents())] {
                if let Some(value) = setting.get(role) {
                    table.insert(key, Json::from(value.as_str()));
                }
            }
            (role, Json::Object(table))
        })
        .collect();
    d.insert("roles", Json::Object(roles));
    d.insert("steps", Json::Array(p.steps().iter().map(|st| Json::Object(step_fields(st))).collect()));
    Json::Object(d)
}

/// The workflow as a workflow file: workflow_definition as TOML, a role and a step a table each.
pub fn workflow_toml(p: &Pipeline) -> String {
    let d = workflow_definition(p);
    let text = |key: &str| d.get(key).and_then(Json::as_str);
    let mut lines = Vec::new();
    if let Some(description) = text("description") {
        lines.push(format!("description = {}", toml_string(description)));
    }
    for key in BUDGET_KEYS {
        if let Some(value) = d.get(key) {
            lines.push(format!("{key} = {}", value.dumps()));
        }
    }
    if let Some(command) = text("proof_command") {
        lines.push(format!("proof_command = {}", toml_string(command)));
    }
    let tables = |key: &str| d.get(key).cloned();
    if let Some(Json::Object(roles)) = tables("roles") {
        for (role, table) in roles.iter() {
            lines.push(String::new());
            lines.push(format!("[roles.{role}]"));
            for (k, v) in table.as_object().into_iter().flat_map(Map::iter) {
                lines.push(format!("{k} = {}", toml_string(v.as_str().unwrap_or_default())));
            }
        }
    }
    if let Some(Json::Array(steps)) = tables("steps") {
        for st in &steps {
            lines.push(String::new());
            lines.push("[[steps]]".to_string());
            for (k, v) in st.as_object().into_iter().flat_map(Map::iter) {
                let value = match v {
                    Json::Bool(b) => b.to_string(),
                    _ => toml_string(v.as_str().unwrap_or_default()),
                };
                lines.push(format!("{k} = {value}"));
            }
        }
    }
    lines.join("\n") + "\n"
}

/// s as a TOML string. A multi-line literal string keeps a prompt readable, but cannot hold ''' or end in a quote,
/// and holds no control characters besides tab and newline; anything else is written with JSON's escapes, which are
/// TOML's too, except that TOML also forbids a raw DEL.
fn toml_string(s: &str) -> String {
    let literal_ok = s.chars().all(|c| c == '\t' || c == '\n' || (c >= ' ' && c != '\u{7f}'));
    if s.contains('\n') && !s.contains("'''") && !s.ends_with('\'') && literal_ok {
        return format!("'''\n{s}'''");
    }
    unicode_string(s).replace('\u{7f}', "\\u007f")
}

/// A run's workflow: the definition saved with it, or the built-in workflow of its name worded by the run's prompts.
/// Errors: no built-in workflow has the name, or the saved definition is invalid.
pub fn run_pipeline(name: &str, definition: Option<&Json>, prompts: &Prompts) -> Result<Pipeline, OrchestratorError> {
    match definition {
        None => find_workflow(name, prompts),
        // A saved definition is canonical, with no use for prompts to word.
        Some(definition) => parse_workflow(name, definition, prompts).map_err(|e| {
            OrchestratorError(format!("the definition of workflow {name} saved with the run is invalid: {e}"))
        }),
    }
}

/// The workflow of a saved state.json, or None when this orchestrator cannot tell: its workflow, by its
/// workflow_definition, else the built-in one of its name, worded by its prompt_overrides; a state saved before
/// workflows existed ran the default.
pub fn saved_pipeline(saved: &Map<Json>) -> Option<Pipeline> {
    let name = saved.get("workflow").filter(|w| w.truthy()).map_or(Some("default"), Json::as_str)?;
    let definition = saved.get("workflow_definition").filter(|d| !d.is_null());
    let overrides = match saved.get("prompt_overrides").filter(|o| o.truthy()) {
        None => Map::new(),
        Some(o) => o.as_object()?.iter().map(|(k, v)| v.as_str().map(|v| (k, v.to_string()))).collect::<Option<_>>()?,
    };
    let run_id = saved.get("run_id").and_then(Json::as_str).unwrap_or("?");
    let prompts = saved_prompts(&overrides, run_id).ok()?;
    run_pipeline(name, definition, &prompts).ok()
}

#[cfg(test)]
mod tests;
