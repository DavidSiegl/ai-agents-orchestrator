//! Workflow definitions: a workflow is data, a Pipeline of Steps that roles take turns at, each turn ending when its
//! role writes its handoff file. A Pipeline is valid once constructed: it refuses a definition the engine could not
//! run, naming the problem as the Python engine does. docs/architecture.md, "Workflows", has the whole picture.

use std::collections::{BTreeSet, HashMap, VecDeque};
use std::sync::LazyLock;

use crate::constants::{AGENT_KINDS, DEFAULT_MAX_FILES, DEFAULT_MAX_LINES, ROLE_LABELS};
use crate::error::OrchestratorError;
use crate::prompts::{Prompt, Prompts};
use crate::py::format::{field_names, format, parse};
use crate::py::json::Map;
use crate::py::text::splitext;

/// Run phases that are no step's: the quality loop after a quality-gated step's turn, and the run's last two.
pub const QUALITY: &str = "quality";
pub const PUBLISH: &str = "publish";
pub const DONE: &str = "done";

/// The placeholders a step's prompts may use besides the per-step paths (see Step).
pub const PROMPT_FIELDS: [&str; 8] = ["task", "cwd", "n", "change", "approve", "changes", "path", "proofs_dir"];
/// The per-step paths: (prefix, suffix) around a step's id.
pub const PATH_FIELDS: [(&str, &str); 3] = [("", "_path"), ("prev_", "_path"), ("earlier_", "_paths")];

/// The run's state, RunState, in the run directory.
pub const STATE_FILE: &str = "state.json";
/// Written into the run directory whenever the run ends: what it did, and where its time went.
pub const SUMMARY_FILE: &str = "summary.md";
/// Written into the run directory when the run is done: run_metrics, for the metrics command.
pub const METRICS_FILE: &str = "metrics.json";

/// Whether name is one the run writes into its directory itself, beside the steps' handoff files: state.json,
/// SUMMARY_FILE, METRICS_FILE, or quality-*, locked-* or proofs-*, the * holding no newline.
pub fn is_run_file(name: &str) -> bool {
    [STATE_FILE, SUMMARY_FILE, METRICS_FILE].contains(&name)
        || ["quality-", "locked-", "proofs-"]
            .iter()
            .any(|prefix| name.strip_prefix(prefix).is_some_and(|rest| !rest.contains('\n')))
}

/// One role's turn in a workflow, which ends when the role writes its handoff file.
///
/// A prompt is filled in with PROMPT_FIELDS, where n is the round, path the turn's own file and proofs_dir the
/// round's directory for the verdict step's proofs (Pipeline::proof_command), and with three paths for every step X
/// of the workflow: X_path in round n, prev_X_path in round n-1, and earlier_X_paths, rounds 1 to n-1 joined with
/// commas.
///
/// A quality-gated step writes more than one file a round: the answer to quality round q inserts -q<q> before the
/// extension. So for such an X, X_path is the file its current turn writes in its own prompts and its last one of
/// round n in the others', prev_X_path is its last one of round n-1, and earlier_X_paths lists every one before its
/// current turn.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Step {
    /// Also the run's phase while the step is current.
    pub id: String,
    /// Names the agent, and keys its pane, label and model.
    pub role: String,
    /// The handoff file's name; a {n} in it makes one file per round.
    pub file: String,
    /// Round 1, or a step outside the review loop.
    pub prompt: String,
    /// A later round, for an agent that has seen the earlier ones; None repeats prompt.
    pub again: Option<String>,
    /// A fresh session in a later round gets prompt, then this note on the turns it has not seen, then again when
    /// fresh_repeats_again: when again asks for work that prompt does not.
    pub fresh_note: Option<String>,
    pub fresh_repeats_again: bool,
    /// Paced by the human: no turn timeout, no stall notice, and the pane is focused once the agent is ready.
    pub human_paced: bool,
    /// Changes the working tree; the run's base commit and branch are fixed just before the first such step.
    pub edits: bool,
    /// Makes this the verdict step: its file starts with a VERDICT line, and CHANGES_REQUESTED goes back to the step
    /// with this id in round n+1, until max_rounds.
    pub loop_to: Option<String>,
    /// With --quality-gate, each turn is followed by the quality loop, which sends a gate that does not pass back to
    /// this step with QUALITY_FIX_PROMPT, and the verdict step hears how it ended.
    pub quality_gated: bool,
    /// Locks the change as it stands when each of this editing step's turns ends: every file it then changes from
    /// the base. A later turn of another editing step that changes one has it restored, and the verdict step hears
    /// of it (Workflow::hold_locks).
    pub locks: bool,
}

/// A handoff file's name as tokens: Some of each character, None for each {n}, a round or quality round number.
type NameTokens = Vec<Option<char>>;

impl Step {
    /// A step with every optional field at its default: no again, no fresh_note, and every flag off.
    pub fn new(id: &str, role: &str, file: &str, prompt: &str) -> Step {
        Step {
            id: id.to_string(),
            role: role.to_string(),
            file: file.to_string(),
            prompt: prompt.to_string(),
            again: None,
            fresh_note: None,
            fresh_repeats_again: false,
            human_paced: false,
            edits: false,
            loop_to: None,
            quality_gated: false,
            locks: false,
        }
    }

    /// Whether the step writes a file per round: its file has an {n}.
    pub fn per_round(&self) -> bool {
        field_names(&self.file).is_ok_and(|names| names.iter().any(|n| n == "n"))
    }

    /// Whether a BLOCKED report pauses the turn for the human. A human-paced step has the human at hand already,
    /// and the verdict step's file must start with its verdict.
    pub fn can_block(&self) -> bool {
        !self.human_paced && self.loop_to.is_none()
    }

    /// The handoff file of round n in run_dir; with q other than 0, the answer to quality round q. n is a round as
    /// Python's int holds it: -1, the round before round 0, names the file no turn writes that a prev_ placeholder
    /// of round 0 gives.
    pub fn path(&self, run_dir: &str, n: impl Into<i64>, q: u32) -> String {
        format!("{run_dir}/{}", self.name(n, q))
    }

    /// The handoff file's name in round n (as path takes it); with q other than 0, the answer to quality round q,
    /// with -q<q> before the extension.
    pub fn name(&self, n: impl Into<i64>, q: u32) -> String {
        let number = n.into().to_string();
        let name = format(&self.file, &|field| (field == "n").then(|| number.clone())).unwrap_or(self.file.clone());
        if q == 0 {
            return name;
        }
        let (stem, ext) = splitext(&name);
        format!("{stem}-q{q}{ext}")
    }

    /// The step's file names as tokens: its file's, and for the gated step its quality answers'.
    fn name_forms(&self) -> Vec<NameTokens> {
        let mut forms = vec![name_tokens(&self.file)];
        if self.quality_gated {
            // As name() does: -q<q> before the extension, where the template splits at the same dot as the filled-in
            // name, since a round number has none.
            let (stem, ext) = splitext(&self.file);
            let mut tokens = name_tokens(stem);
            tokens.extend([Some('-'), Some('q'), None]);
            tokens.extend(name_tokens(ext));
            forms.push(tokens);
        }
        forms
    }

    /// The step's last file of round n, once the round's quality rounds, if any, are over.
    pub fn last_path(&self, run_dir: &str, n: u32, quality_round: u32) -> String {
        let q = if self.quality_gated { quality_round.saturating_sub(1) } else { 0 };
        self.path(run_dir, n, q)
    }

    /// The step's prompt templates: prompt, and again and fresh_note where it has them.
    pub fn templates(&self) -> Vec<&str> {
        [Some(&self.prompt), self.again.as_ref(), self.fresh_note.as_ref()].into_iter().flatten().map(String::as_str).collect()
    }

    /// The names of every placeholder its templates use; one that does not parse adds none.
    pub fn placeholders(&self) -> BTreeSet<String> {
        self.templates().into_iter().flat_map(|t| field_names(t).unwrap_or_default()).collect()
    }
}

/// A template as its characters, with None for each field: the {n} of a handoff file's name.
fn name_tokens(template: &str) -> NameTokens {
    let mut tokens = Vec::new();
    for chunk in parse(template).unwrap_or_default() {
        tokens.extend(chunk.literal.chars().map(Some));
        if chunk.field.is_some() {
            tokens.push(None);
        }
    }
    tokens
}

/// What a Pipeline holds besides its name, roles and steps; each defaults as a workflow file that leaves it out.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PipelineSettings {
    /// Role key -> the model its agent starts with when no model flag names one.
    pub models: Map<String>,
    /// Shown by `workflows`.
    pub description: String,
    /// Role key -> the harness its agent runs in when no agent flag names one; else DEFAULT_AGENT.
    pub agents: Map<String>,
    /// The size of change past which a quality round asks the Builder why (diff_budget): files touched, and lines
    /// added plus lines removed. The workflow's, not the gated step's: the change a quality round judges runs from
    /// the run's base to the snapshot, so it holds every editing step's work, not only the gated step's.
    pub max_files: i64,
    pub max_lines: i64,
    /// A shell command, with {proofs} for the path of a copy of one proof file inside the working tree
    /// (Workflow::run_proof), that runs the proof in the working tree and exits 1 when the proof fails, as pytest
    /// does. With it, each proof the verdict step writes in proofs_dir with a review that requests changes is run, and
    /// the review gains the outcomes (Workflow::judge_proofs). None runs none.
    pub proof_command: Option<String>,
}

impl Default for PipelineSettings {
    fn default() -> Self {
        PipelineSettings {
            models: Map::new(),
            description: String::new(),
            agents: Map::new(),
            max_files: DEFAULT_MAX_FILES,
            max_lines: DEFAULT_MAX_LINES,
            proof_command: None,
        }
    }
}

/// A named workflow: roles that hand off through files, one step after another.
///
/// A run is in round 0 until its first editing step, round 1 from there, and one round more each time the verdict
/// step loops back. Two pipelines are equal when every part is, their role maps compared as Python's dicts are, in
/// any order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pipeline {
    name: String,
    /// Role key -> pane label. The order lays out the panes: the first role takes the workspace's root pane, the
    /// second a split to its right, and each further one a split below the one before.
    roles: Map<String>,
    steps: Vec<Step>,
    settings: PipelineSettings,
}

impl Pipeline {
    /// The workflow of these roles and steps, with the default settings; the problem that makes it unusable, as
    /// "workflow <name>: <problem>", otherwise.
    pub fn new(name: &str, roles: Map<String>, steps: Vec<Step>) -> Result<Pipeline, String> {
        Pipeline::with(name, roles, steps, PipelineSettings::default())
    }

    /// new, with settings.
    pub fn with(name: &str, roles: Map<String>, steps: Vec<Step>, settings: PipelineSettings) -> Result<Pipeline, String> {
        let p = Pipeline { name: name.to_string(), roles, steps, settings };
        match p.problem() {
            Some(problem) => Err(format!("workflow {name}: {problem}")),
            None => Ok(p),
        }
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn roles(&self) -> &Map<String> {
        &self.roles
    }

    pub fn steps(&self) -> &[Step] {
        &self.steps
    }

    pub fn settings(&self) -> &PipelineSettings {
        &self.settings
    }

    /// The first problem found that makes the definition unusable, in the order of the checks; None for none.
    fn problem(&self) -> Option<String> {
        if self.steps.is_empty() {
            return Some("has no steps".to_string());
        }
        let checks: [fn(&Pipeline) -> Option<String>; 9] = [
            Pipeline::name_problem,
            Pipeline::role_problem,
            Pipeline::uniqueness_problem,
            Pipeline::step_problem,
            Pipeline::collision_problem,
            Pipeline::loop_problem,
            Pipeline::budget_problem,
            Pipeline::lock_problem,
            Pipeline::proof_problem,
        ];
        checks.iter().find_map(|check| check(self))
    }

    fn budget_problem(&self) -> Option<String> {
        [("max_files", self.settings.max_files), ("max_lines", self.settings.max_lines)]
            .into_iter()
            .find(|(_, value)| *value < 1)
            .map(|(key, _)| format!("{key} must be at least 1"))
    }

    fn proof_problem(&self) -> Option<String> {
        let command = self.settings.proof_command.as_ref()?;
        if self.verdict_step().is_none() {
            return Some("proof_command runs the verdict step's proofs, and the workflow has no verdict step".into());
        }
        match field_names(command) {
            Err(e) => Some(format!("proof_command: {e}; write a literal brace as {{{{ or }}}}")),
            Ok(names) if names != ["proofs"] => {
                Some("proof_command must name the proof file once, as {proofs}, and no other placeholder".into())
            }
            Ok(_) => None,
        }
    }

    /// A locking step that is not the first editing step, or that the verdict loop runs again. Host::lock locks what
    /// the change holds when the step's turn ends, which is only the step's own work when no editing turn came
    /// before it.
    fn lock_problem(&self) -> Option<String> {
        let first_edit = self.first_edit().map(|st| st.id.as_str());
        for st in self.steps.iter().filter(|st| st.locks) {
            if !st.edits {
                return Some(format!("step {} locks the files it changes, so it must edit", st.id));
            }
            if Some(st.id.as_str()) != first_edit {
                return Some(format!("step {} locks the files it changes, so it must be the first editing step", st.id));
            }
            if let Some(target) = self.verdict_step().and_then(|v| v.loop_to.as_deref()) {
                if let (Some(t), Some(i)) = (self.index_of(target), self.index_of(&st.id)) {
                    if t <= i {
                        return Some(format!(
                            "step {} locks the files it changes, so the verdict loop must not run it again",
                            st.id
                        ));
                    }
                }
            }
        }
        None
    }

    fn name_problem(&self) -> Option<String> {
        let ids: Vec<&str> = self.steps.iter().map(|st| st.id.as_str()).collect();
        let files: Vec<&str> = self.steps.iter().map(|st| st.file.as_str()).collect();
        for (kind, names) in [("step id", &ids), ("handoff file", &files)] {
            if let Some(dup) = names.iter().find(|x| names.iter().filter(|y| y == x).count() > 1) {
                return Some(format!("duplicate {kind} {dup}"));
            }
        }
        [QUALITY, PUBLISH, DONE]
            .iter()
            .filter(|phase| ids.contains(phase))
            .min()
            .map(|reserved| format!("step id {reserved} is reserved for a phase of the run's own"))
    }

    fn role_problem(&self) -> Option<String> {
        for (role, label) in self.roles.iter() {
            if label.is_empty() {
                return Some(format!("role {role} has no label"));
            }
            if !self.steps.iter().any(|st| st.role == role) {
                return Some(format!("role {role} has no step"));
            }
        }
        for (what, table) in [("a model", &self.settings.models), ("an agent", &self.settings.agents)] {
            if let Some(unknown) = table.keys().filter(|role| !self.roles.contains_key(role)).min() {
                return Some(format!("{what} is set for role {unknown}, which is not one of the workflow's roles"));
            }
        }
        for (role, kind) in self.settings.agents.iter() {
            if !AGENT_KINDS.contains(&kind.as_str()) {
                return Some(format!(
                    "role {role}: agent {kind} is not a harness this orchestrator supports; it takes {}",
                    AGENT_KINDS.join(", ")
                ));
            }
        }
        None
    }

    /// More than one verdict loop or quality-gated step, or a gated step that cannot be one.
    fn uniqueness_problem(&self) -> Option<String> {
        let ids = |steps: Vec<&Step>| steps.iter().map(|st| st.id.as_str()).collect::<Vec<_>>().join(", ");
        let loops: Vec<&Step> = self.steps.iter().filter(|st| st.loop_to.is_some()).collect();
        if loops.len() > 1 {
            return Some(format!("steps {} each loop back; only one verdict loop is supported", ids(loops)));
        }
        let gated: Vec<&Step> = self.steps.iter().filter(|st| st.quality_gated).collect();
        if gated.len() > 1 {
            return Some(format!("steps {} are each quality-gated; only one may be", ids(gated)));
        }
        let st = gated.first()?;
        if !st.edits {
            return Some(format!("step {} is quality-gated, so it must edit", st.id));
        }
        if !st.per_round() {
            return Some(format!("step {} is quality-gated, so its handoff file needs {{n}}", st.id));
        }
        if st.loop_to.is_some() {
            return Some(format!("step {} is quality-gated, so it cannot be the verdict step", st.id));
        }
        None
    }

    fn step_problem(&self) -> Option<String> {
        let names: Vec<String> = self.path_placeholders().into_iter().map(|(name, _, _)| name).collect();
        if let Some(dup) = names.iter().find(|x| names.iter().filter(|y| y == x).count() > 1) {
            return Some(format!("prompt placeholder {dup} names the files of two steps"));
        }
        let known: BTreeSet<&str> = PROMPT_FIELDS.iter().copied().chain(names.iter().map(String::as_str)).collect();
        let first_edit = self.steps.iter().position(|st| st.edits).unwrap_or(self.steps.len());
        for (i, st) in self.steps.iter().enumerate() {
            if !self.roles.contains_key(&st.role) {
                return Some(format!("step {}: role {} has no label", st.id, st.role));
            }
            match field_names(&st.file) {
                Err(e) => return Some(e.to_string()),
                Ok(fields) if fields.iter().any(|f| f != "n") => {
                    return Some(format!("step {}: handoff file {} may use only {{n}}", st.id, st.file));
                }
                Ok(_) => {}
            }
            if st.per_round() && i < first_edit {
                // Round 0 lasts until the first editing step, so such a file would be numbered 0.
                return Some(format!("step {}: a per-round handoff file needs an editing step at or before it", st.id));
            }
            for template in st.templates() {
                if let Err(e) = field_names(template) {
                    return Some(e.to_string());
                }
            }
            if let Some(unknown) = st.placeholders().iter().find(|p| !known.contains(p.as_str())) {
                return Some(format!("step {}: unknown prompt placeholder {unknown}", st.id));
            }
            if let Some(problem) = fill_problem(st, &known) {
                return Some(format!("step {}: {problem}", st.id));
            }
        }
        None
    }

    /// Two steps whose files can have the same name: the later turn would find its file already written.
    fn collision_problem(&self) -> Option<String> {
        for (i, st) in self.steps.iter().enumerate() {
            for other in &self.steps[i + 1..] {
                if let Some(name) = shared_step_name(st, other) {
                    return Some(format!("steps {} and {} can both write {name}", st.id, other.id));
                }
            }
        }
        None
    }

    fn loop_problem(&self) -> Option<String> {
        let verdict = self.verdict_step()?;
        let loop_to = verdict.loop_to.as_deref().unwrap_or_default();
        let Some(target) = self.index_of(loop_to) else {
            return Some(format!("step {} loops back to {loop_to}, which is not a step", verdict.id));
        };
        let end = self.index_of(&verdict.id).unwrap_or_default();
        if target >= end {
            return Some(format!("step {} loops back to {loop_to}, which does not come before it", verdict.id));
        }
        // A later round finds a fixed file already written and would skip the step.
        self.steps[target..=end]
            .iter()
            .find(|st| !st.per_round())
            .map(|fixed| format!("step {} is in the review loop, so its handoff file needs {{n}}", fixed.id))
    }

    fn index_of(&self, step_id: &str) -> Option<usize> {
        self.steps.iter().position(|st| st.id == step_id)
    }

    /// Each per-step path placeholder, with its prefix and the step whose file it names, in step order.
    pub fn path_placeholders(&self) -> Vec<(String, &'static str, &Step)> {
        self.steps
            .iter()
            .flat_map(|st| PATH_FIELDS.iter().map(move |(pre, post)| (format!("{pre}{}{post}", st.id), *pre, st)))
            .collect()
    }

    /// The step of that id; None for an id the workflow has no step of.
    pub fn step(&self, step_id: &str) -> Option<&Step> {
        self.steps.iter().find(|st| st.id == step_id)
    }

    /// The step after the one of that id; None after the last, or for an id the workflow has no step of.
    pub fn after(&self, step_id: &str) -> Option<&Step> {
        self.steps.get(self.index_of(step_id)? + 1)
    }

    pub fn first_edit(&self) -> Option<&Step> {
        self.steps.iter().find(|st| st.edits)
    }

    pub fn last_edit(&self) -> Option<&Step> {
        self.steps.iter().rev().find(|st| st.edits)
    }

    pub fn verdict_step(&self) -> Option<&Step> {
        self.steps.iter().find(|st| st.loop_to.is_some())
    }

    pub fn gated(&self) -> Option<&Step> {
        self.steps.iter().find(|st| st.quality_gated)
    }

    /// The step whose file is the change's spec, titling its branch and pull request: a first step with a fixed file
    /// that does not edit; None leaves the task.
    pub fn contract(&self) -> Option<&Step> {
        let first = &self.steps[0];
        (!first.edits && !first.per_round()).then_some(first)
    }

    /// The step whose file the run ends on.
    pub fn result(&self) -> &Step {
        self.verdict_step().unwrap_or(&self.steps[self.steps.len() - 1])
    }

    pub fn models(&self) -> &Map<String> {
        &self.settings.models
    }

    pub fn agents(&self) -> &Map<String> {
        &self.settings.agents
    }

    pub fn description(&self) -> &str {
        &self.settings.description
    }

    pub fn max_files(&self) -> i64 {
        self.settings.max_files
    }

    pub fn max_lines(&self) -> i64 {
        self.settings.max_lines
    }

    pub fn proof_command(&self) -> Option<&str> {
        self.settings.proof_command.as_deref()
    }
}

/// What stops the step's file or prompts from being filled in at run time, which a bad format spec, conversion or
/// nested field would, though its placeholders are all known; None for nothing.
fn fill_problem(step: &Step, known: &BTreeSet<&str>) -> Option<String> {
    // A plain {n}, so that name_tokens knows every name it can give.
    let chunks = parse(&step.file).unwrap_or_default();
    let fields = chunks.iter().filter_map(|c| c.field.as_ref()).filter(|f| !f.name.is_empty());
    if fields.into_iter().any(|f| !f.spec.is_empty() || f.conversion.is_some()) {
        return Some(format!(
            "handoff file {} may use {{n}} only as it is, with no format spec or conversion",
            step.file
        ));
    }
    let name = step.name(1, 0);
    if name.contains('/') || matches!(name.as_str(), "" | "." | "..") || is_run_file(&name) {
        return Some(format!(
            "handoff file {} must be a plain file name, and not {STATE_FILE}, {SUMMARY_FILE}, {METRICS_FILE}, \
             quality-*, locked-* or proofs-*, which the run writes itself",
            step.file
        ));
    }
    // Every value a prompt is filled in with is a string.
    let empty = |field: &str| known.contains(field).then(String::new);
    for (field, template) in [("prompt", Some(&step.prompt)), ("again", step.again.as_ref()), ("fresh_note", step.fresh_note.as_ref())] {
        let Some(template) = template else { continue };
        if let Err(e) = format(template, &empty) {
            return Some(format!("{field} cannot be filled in: {}", e.named()));
        }
    }
    None
}

/// The shortest name that files of both steps can have, or None.
fn shared_step_name(st: &Step, other: &Step) -> Option<String> {
    for a in st.name_forms() {
        for b in other.name_forms() {
            if let Some(name) = shared_name(&a, &b) {
                return Some(name);
            }
        }
    }
    None
}

/// A state of shared_name's automaton for one token list: the position in it, and whether inside a number.
type TokenState = (usize, bool);

/// The shortest name that both token lists give, or None when they share none; of the shortest, the first in
/// character order.
///
/// Each list is matched by a small automaton whose states are (position, inside a number), a number being
/// [1-9][0-9]*, as rounds are. A breadth-first walk over pairs of state sets, one character at a time, finds a name
/// both accept, so the answer is exact: no sampling of rounds. The one approximation is a template that repeats {n},
/// whose numbers are taken as independent; it can only reject more.
fn shared_name(a: &[Option<char>], b: &[Option<char>]) -> Option<String> {
    let mut alphabet: BTreeSet<char> = a.iter().chain(b).flatten().copied().collect();
    alphabet.extend('0'..='9');
    let start = token_closure(BTreeSet::from([(0, false)]));
    let start = (start.clone(), start);
    // Each pair of state sets reached, with the name that first reached it.
    let mut reached: HashMap<(BTreeSet<TokenState>, BTreeSet<TokenState>), String> = HashMap::new();
    reached.insert(start.clone(), String::new());
    let mut queue = VecDeque::from([start]);
    while let Some(pair) = queue.pop_front() {
        let name = reached[&pair].clone();
        if pair.0.contains(&(a.len(), false)) && pair.1.contains(&(b.len(), false)) {
            return Some(name);
        }
        for &c in &alphabet {
            let next = (token_advance(&pair.0, a, c), token_advance(&pair.1, b, c));
            if !next.0.is_empty() && !next.1.is_empty() && !reached.contains_key(&next) {
                reached.insert(next.clone(), format!("{name}{c}"));
                queue.push_back(next);
            }
        }
    }
    None
}

/// The states, with each number also ended: one may end after any digit.
fn token_closure(states: BTreeSet<TokenState>) -> BTreeSet<TokenState> {
    let ended: Vec<TokenState> = states.iter().filter(|(_, inside)| *inside).map(|(i, _)| (i + 1, false)).collect();
    states.into_iter().chain(ended).collect()
}

fn token_advance(states: &BTreeSet<TokenState>, tokens: &[Option<char>], c: char) -> BTreeSet<TokenState> {
    token_closure(states.iter().filter_map(|&state| token_step(state, tokens, c)).collect())
}

/// Where one state goes on the character c, or None when c ends it.
fn token_step((i, inside): TokenState, tokens: &[Option<char>], c: char) -> Option<TokenState> {
    if inside {
        return c.is_ascii_digit().then_some((i, true));
    }
    match tokens.get(i)? {
        // A number starts with a digit other than 0.
        None => ('1'..='9').contains(&c).then_some((i, true)),
        Some(t) => (*t == c).then_some((i + 1, false)),
    }
}

/// The default workflow, its steps worded by prompts: Spec Collector -> Builder <-> Reviewer.
pub fn default_workflow(prompts: &Prompts) -> Pipeline {
    let text = |p: Prompt| prompts.text(p).to_string();
    let mut spec = Step::new("spec", "spec", "spec.md", &text(Prompt::SpecPrompt));
    spec.human_paced = true;
    let mut build = Step::new("build", "build", "build-{n}.md", &text(Prompt::BuildPrompt));
    build.again = Some(text(Prompt::FixPrompt));
    build.fresh_note = Some(text(Prompt::RebuildNote));
    build.fresh_repeats_again = true;
    build.edits = true;
    build.quality_gated = true;
    let mut review = Step::new("review", "review", "review-{n}.md", &text(Prompt::ReviewPrompt));
    review.again = Some(text(Prompt::RecheckPrompt));
    review.fresh_note = Some(text(Prompt::RereviewNote));
    review.loop_to = Some("build".to_string());
    let roles = ROLE_LABELS.iter().map(|(k, v)| (*k, v.to_string())).collect();
    Pipeline::new("default", roles, vec![spec, build, review]).expect("the default workflow is valid with any prompts")
}

/// The default workflow worded by the built-in prompts.
pub static DEFAULT_WORKFLOW: LazyLock<Pipeline> = LazyLock::new(|| default_workflow(&Prompts::default()));

/// The names of the workflows `run --workflow` offers built in; a workflow file cannot take one.
pub const WORKFLOWS: [&str; 1] = ["default"];

/// The built-in workflow of that name, worded by prompts; an error naming the built-in ones when there is none.
pub fn find_workflow(name: &str, prompts: &Prompts) -> Result<Pipeline, OrchestratorError> {
    if name == "default" {
        // The one built-in workflow, whose steps take their texts from Prompts.
        return Ok(default_workflow(prompts));
    }
    let mut names = WORKFLOWS.to_vec();
    names.sort();
    Err(OrchestratorError(format!("unknown workflow {name}; this orchestrator has {}", names.join(", "))))
}

/// Whether p is the built-in workflow of its name as prompts word it, which a run need not save the definition of:
/// its name and prompts give it back (run_pipeline).
pub fn is_built_in(p: &Pipeline, prompts: &Prompts) -> bool {
    WORKFLOWS.contains(&p.name()) && find_workflow(p.name(), prompts).is_ok_and(|built_in| &built_in == p)
}

/// The workflow's steps in order, e.g. spec -> build -> review (back to build).
pub fn workflow_shape(p: &Pipeline) -> String {
    p.steps()
        .iter()
        .map(|st| match &st.loop_to {
            Some(target) => format!("{} (back to {target})", st.id),
            None => st.id.clone(),
        })
        .collect::<Vec<_>>()
        .join(" -> ")
}

#[cfg(test)]
mod tests;
