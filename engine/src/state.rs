//! RunState: what a run has done so far, saved as state.json in its run directory, in the format the Python engine
//! reads and writes, so that a run started by either engine resumes in the other.

use crate::constants::{
    DEFAULT_MAX_QUALITY_ROUNDS, DEFAULT_MAX_ROUNDS, DEFAULT_TURN_TIMEOUT, LOCAL_REFS_PREFIX, RUNS_DIR,
};
use crate::error::OrchestratorError;
use crate::herdr::HerdrServer;
use crate::prompts::{saved_prompts, Prompts};
use crate::py::json::{Json, Map};
use crate::py::text::repr;
use crate::workflow_file::saved_pipeline;

/// What a run has done so far, saved as state.json in its run directory.
///
/// state.json holds these fields in this order, as orchestrator.py's RunState declares them, herdr_server after
/// machine and only when it is known (the field says why); a state read with fields
/// this engine does not know keeps them, after its own, so that a state a newer engine wrote survives a round trip.
/// The records a field holds as Json are kept as they were read, key order included.
#[derive(Clone, Debug, PartialEq)]
pub struct RunState {
    pub run_id: String,
    pub task: String,
    /// The project's checkout, which holds the run directory (dir) and .orchestrator/.gitignore.
    pub cwd: String,
    /// The herdr machine the agents run on; None for this one.
    pub machine: Option<String>,
    /// The herdr server of this machine the run's agents run in: the one the engine that started the run, or last
    /// resumed it, reached, where it finds or starts each agent it comes to (one a resume has not come to yet may
    /// still be in an earlier server). serve reaches them there, from whatever environment it runs in. None for a
    /// run on a machine, whose herdr serves it, for a run saved before the field existed, and for one whose server
    /// could not be told (HerdrServer::of_environment): serve then reaches the server its own environment picks.
    /// Left out of state.json while None, so that a state saved before it writes back as it was.
    pub herdr_server: Option<HerdrServer>,
    /// The current step's id, or quality, publish or done. main starts a run at its workflow's first step.
    pub phase: String,
    pub round: u32,
    pub workspace_id: String,
    /// The first role's pane, which the second role's pane is split from.
    pub root_pane: String,
    /// The commit the change is judged against, fixed before the first editing step.
    pub base: Option<String>,
    pub base_branch: Option<String>,
    pub branch: Option<String>,
    pub pr_url: Option<String>,
    pub verdict: Option<String>,
    /// The files that conflicted with the base branch when the pull request was opened, as a draft; empty again once
    /// refresh found the pull request merging cleanly, merged or closed (refresh_conflict).
    pub conflicts: Vec<String>,
    pub error: Option<String>,
    /// Per role: the agent's name, its pane, the harness it runs in (kind) and, once herdr reports it, its session id.
    /// A record without a kind is a claude agent's, saved before there were others.
    pub agents: Map<Map<String>>,
    pub max_rounds: u32,
    /// Seconds a turn may take.
    pub turn_timeout: u64,
    /// As --permission-mode names it; each harness's agent gets its own equivalent (permission_args).
    pub permission_mode: Option<String>,
    pub models: Map<String>,
    /// Per role: the harness its next agent starts in. A role missing here runs in DEFAULT_AGENT.
    pub agent_kinds: Map<String>,
    /// False by default because a run saved before pull requests existed never switched to its own branch.
    pub pull_request: bool,
    /// Basename of the handoff file whose prompt was delivered, so a resume does not prompt for it again.
    pub prompted: Option<String>,
    /// Basenames of the verdict files whose one retry was used, and the one whose retry turn is under way, so a
    /// resume neither grants a second retry nor sends the step's own prompt in place of the retry prompt.
    pub retried: Vec<String>,
    pub retrying: Option<String>,
    /// The orchestrator process driving the run, {"host", "pid", "started_at"}; None while none does.
    pub owner: Option<Map<Json>>,
    pub heartbeat_at: Option<String>,
    /// The Jenkins job that runs the quality gate; None for a run without the gate, as for one saved before it.
    pub quality_job: Option<String>,
    /// True once resume --no-quality-gate turned the gate off, for good: quality_job is None then, and the quality
    /// files written until then stay the run's (turn_quality_gate_off).
    pub quality_gate_disabled: bool,
    pub max_quality_rounds: u32,
    /// The quality round the round is in: 0 until the Builder's first report, then q from quality round q on. A
    /// quality-fix turn that ends after resume --no-quality-gate leaves it one past the last quality round, which
    /// never runs.
    pub quality_round: u32,
    /// The quality build in flight, {ref, sha, queue_url, build}, each saved as soon as it is known. rejected lists
    /// builds of ref that ended without a verdict, so a resume does not adopt them again.
    pub ci: Map<Json>,
    /// The name of the run's workflow; a run saved before workflows existed ran the default.
    pub workflow: String,
    /// The workflow_definition of one that is not built in, so a resume does not depend on its file.
    pub workflow_definition: Option<Json>,
    /// Prompts::overrides of the prompts the run started with (load_prompts), so a resume does not depend on the
    /// prompt files either; empty for the built-in texts, as for a run saved before prompt files.
    pub prompt_overrides: Map<String>,
    /// What prune did with branch once the pull request was done: "deleted", or "kept: <why>". Written by
    /// prune_branches, never by the run itself.
    pub branch_cleanup: Option<String>,
    /// A run --worktree run's own git worktree, by its absolute path, from the run's start on: round 0 creates it
    /// there. None for a run in the project's checkout, cwd. cwd stays the project, where the run directory is.
    pub worktree: Option<String>,
    /// What close did, one line such as "workspace closed; worktree deleted; branch deleted". Written by close_run,
    /// never by the run itself; a closed run is not resumed.
    pub closed: Option<String>,
    /// One record per handoff file, in the order the turns started (Workflow::turn_record). A turn's record opens
    /// when its prompt is delivered, or a quality analysis starts, and closes when its file is found. Absent from a
    /// run saved before them.
    pub turns: Vec<Map<Json>>,
    /// One record per quality round judged, in order: {"round", "quality_round", "gate", "conditions",
    /// "fingerprints_by_check"}, the last two as gate_failures reads them from its quality file. Fingerprints, never
    /// the findings' text, so that state.json stays small.
    pub quality_rounds: Vec<Map<Json>>,
    /// The BLOCKED report the current turn waits on the human for, {"file", "question", "k"}: file is the handoff
    /// file's basename, and the report was moved to its k-th blocked name (Workflow::blocked_path). None otherwise.
    pub blocked: Option<Map<Json>>,
    /// Per locking step (Step::locks), by id: the paths its last turn locked, as Host::lock gives them. Their locked
    /// versions are in the commit at lock_ref(step id).
    pub locked: Map<Vec<String>>,
    /// One record per editing turn that changed locked files, {"round", "file", "paths"}, in the order of the turns:
    /// file is the basename of the diff of what the turn changed in them, in the run directory, and paths are the
    /// files, by the id of the step that locked them. The files were restored once the record was saved.
    pub restored: Vec<Map<Json>>,
    /// One record per run of a proof the verdict step wrote with a review that requested changes, {"round",
    /// "written", "file", "finding", "demonstrated", "said"}, in the order they ran: round is the round it ran in,
    /// written the round whose verdict step wrote it, file its basename in proofs_dir(written), finding the first
    /// number in that name, demonstrated whether it failed against the change, and said how it exited and the last
    /// PROOF_TAIL_LINES lines it printed. A proof first runs in the round that wrote it (Workflow::judge_proofs), then
    /// again in every later round until it does not fail (Workflow::rerun_proofs). A record saved before written
    /// existed is a first run, as crate::proofs::ProofRun reads it.
    pub proofs: Vec<Map<Json>>,
    /// The fields of a state.json that this engine does not know, as they were read, in their order.
    pub unknown: Map<Json>,
}

/// The keys of state.json this engine knows, which from_saved keeps out of unknown: every field to_json writes, in its
/// order, then agent_args, which it only reads. A state/tests.rs test holds to_json's keys to this list; that every
/// recorded state writes back as it was holds Fields::read to it.
const FIELDS: [&str; 46] = [
    "run_id",
    "task",
    "cwd",
    "machine",
    "herdr_server",
    "phase",
    "round",
    "workspace_id",
    "root_pane",
    "base",
    "base_branch",
    "branch",
    "pr_url",
    "verdict",
    "conflicts",
    "error",
    "agents",
    "max_rounds",
    "turn_timeout",
    "permission_mode",
    "models",
    "agent_kinds",
    "pull_request",
    "prompted",
    "retried",
    "retrying",
    "owner",
    "heartbeat_at",
    "quality_job",
    "quality_gate_disabled",
    "max_quality_rounds",
    "quality_round",
    "ci",
    "workflow",
    "workflow_definition",
    "prompt_overrides",
    "branch_cleanup",
    "worktree",
    "closed",
    "turns",
    "quality_rounds",
    "blocked",
    "locked",
    "restored",
    "proofs",
    // 0.3.0's permission mode, read into permission_mode and never written.
    "agent_args",
];

impl RunState {
    /// A new run's state: every field but these at its default.
    pub fn new(run_id: &str, task: &str, cwd: &str) -> RunState {
        RunState {
            run_id: run_id.to_string(),
            task: task.to_string(),
            cwd: cwd.to_string(),
            machine: None,
            herdr_server: None,
            phase: "spec".to_string(),
            round: 0,
            workspace_id: String::new(),
            root_pane: String::new(),
            base: None,
            base_branch: None,
            branch: None,
            pr_url: None,
            verdict: None,
            conflicts: Vec::new(),
            error: None,
            agents: Map::new(),
            max_rounds: DEFAULT_MAX_ROUNDS,
            turn_timeout: DEFAULT_TURN_TIMEOUT,
            permission_mode: None,
            models: Map::new(),
            agent_kinds: Map::new(),
            pull_request: false,
            prompted: None,
            retried: Vec::new(),
            retrying: None,
            owner: None,
            heartbeat_at: None,
            quality_job: None,
            quality_gate_disabled: false,
            max_quality_rounds: DEFAULT_MAX_QUALITY_ROUNDS,
            quality_round: 0,
            ci: Map::new(),
            workflow: "default".to_string(),
            workflow_definition: None,
            prompt_overrides: Map::new(),
            branch_cleanup: None,
            worktree: None,
            closed: None,
            turns: Vec::new(),
            quality_rounds: Vec::new(),
            blocked: None,
            locked: Map::new(),
            restored: Vec::new(),
            proofs: Vec::new(),
            unknown: Map::new(),
        }
    }

    /// The state a state.json's text holds; an error for text that is not a JSON object, and as from_saved.
    pub fn from_text(text: &str) -> Result<RunState, OrchestratorError> {
        match Json::parse(text) {
            Ok(Json::Object(saved)) => RunState::from_saved(&saved),
            Ok(_) => Err(OrchestratorError("a run state is a JSON object".into())),
            Err(e) => Err(OrchestratorError(format!("a run state that is not JSON: {e}"))),
        }
    }

    /// A saved state, including one written before some of the fields existed, each of those at its default: one
    /// saved by 0.3.0 has its agent_args read as its permission mode, and one without a root pane takes its
    /// workflow's first role's pane. Errors, naming the run: run_id, task or cwd is missing, a field holds a value
    /// of another type than its own, or agent_args name no permission mode.
    pub fn from_saved(saved: &Map<Json>) -> Result<RunState, OrchestratorError> {
        let run_id = saved.get("run_id").and_then(Json::as_str).unwrap_or("?");
        let fields = Fields { saved, run_id };
        let mut state = RunState::new(
            &fields.required("run_id")?,
            &fields.required("task")?,
            &fields.required("cwd")?,
        );
        fields.read(&mut state)?;
        if !saved.contains_key("permission_mode") {
            state.permission_mode = saved_permission_mode(saved)?;
        }
        state.unknown = saved.iter().filter(|(k, _)| !FIELDS.contains(k)).map(|(k, v)| (k, v.clone())).collect();
        if state.root_pane.is_empty() {
            let first = saved_pipeline(saved).and_then(|p| p.roles().keys().next().map(String::from));
            if let Some(pane) = first.and_then(|role| state.agents.get(&role)).and_then(|agent| agent.get("pane")) {
                state.root_pane = pane.clone();
            }
        }
        Ok(state)
    }

    /// The state as state.json holds it: every field FIELDS lists but agent_args, which is only read, and herdr_server
    /// while it is None, in FIELDS's order, then the unknown ones.
    pub fn to_json(&self) -> Json {
        let text = |s: &str| Json::from(s);
        let opt = |s: &Option<String>| Json::from(s.clone());
        let list = |items: &[String]| Json::Array(items.iter().map(|s| text(s)).collect());
        let strings = |map: &Map<String>| Json::Object(map.iter().map(|(k, v)| (k, text(v))).collect());
        let records = |items: &[Map<Json>]| Json::Array(items.iter().cloned().map(Json::Object).collect());
        let number = |n: u64| Json::Int(n.into());
        let mut out = Map::new();
        let s = self;
        out.insert("run_id", text(&s.run_id));
        out.insert("task", text(&s.task));
        out.insert("cwd", text(&s.cwd));
        out.insert("machine", opt(&s.machine));
        if let Some(server) = &s.herdr_server {
            out.insert("herdr_server", server.to_json());
        }
        out.insert("phase", text(&s.phase));
        out.insert("round", number(s.round.into()));
        out.insert("workspace_id", text(&s.workspace_id));
        out.insert("root_pane", text(&s.root_pane));
        out.insert("base", opt(&s.base));
        out.insert("base_branch", opt(&s.base_branch));
        out.insert("branch", opt(&s.branch));
        out.insert("pr_url", opt(&s.pr_url));
        out.insert("verdict", opt(&s.verdict));
        out.insert("conflicts", list(&s.conflicts));
        out.insert("error", opt(&s.error));
        out.insert("agents", Json::Object(s.agents.iter().map(|(k, v)| (k, strings(v))).collect()));
        out.insert("max_rounds", number(s.max_rounds.into()));
        out.insert("turn_timeout", number(s.turn_timeout));
        out.insert("permission_mode", opt(&s.permission_mode));
        out.insert("models", strings(&s.models));
        out.insert("agent_kinds", strings(&s.agent_kinds));
        out.insert("pull_request", Json::Bool(s.pull_request));
        out.insert("prompted", opt(&s.prompted));
        out.insert("retried", list(&s.retried));
        out.insert("retrying", opt(&s.retrying));
        out.insert("owner", s.owner.clone().map_or(Json::Null, Json::Object));
        out.insert("heartbeat_at", opt(&s.heartbeat_at));
        out.insert("quality_job", opt(&s.quality_job));
        out.insert("quality_gate_disabled", Json::Bool(s.quality_gate_disabled));
        out.insert("max_quality_rounds", number(s.max_quality_rounds.into()));
        out.insert("quality_round", number(s.quality_round.into()));
        out.insert("ci", Json::Object(s.ci.clone()));
        out.insert("workflow", text(&s.workflow));
        out.insert("workflow_definition", s.workflow_definition.clone().unwrap_or(Json::Null));
        out.insert("prompt_overrides", strings(&s.prompt_overrides));
        out.insert("branch_cleanup", opt(&s.branch_cleanup));
        out.insert("worktree", opt(&s.worktree));
        out.insert("closed", opt(&s.closed));
        out.insert("turns", records(&s.turns));
        out.insert("quality_rounds", records(&s.quality_rounds));
        out.insert("blocked", s.blocked.clone().map_or(Json::Null, Json::Object));
        out.insert("locked", Json::Object(s.locked.iter().map(|(k, v)| (k, list(v))).collect()));
        out.insert("restored", records(&s.restored));
        out.insert("proofs", records(&s.proofs));
        // Never over a field of its own, which a stale copy of the state's would otherwise replace.
        for (k, v) in s.unknown.iter() {
            if !out.contains_key(k) {
                out.insert(k, v.clone());
            }
        }
        Json::Object(out)
    }

    /// state.json's text: to_json as json.dumps writes it, and a newline.
    pub fn to_text(&self) -> String {
        self.to_json().dumps() + "\n"
    }

    /// The prompts the run sends: those it started with (saved_prompts). Errors: its prompt_overrides name a prompt
    /// this orchestrator lacks.
    pub fn prompts(&self) -> Result<Prompts, OrchestratorError> {
        saved_prompts(&self.prompt_overrides, &self.run_id)
    }

    /// The run id's random suffix, which names its agents and labels its workspace.
    pub fn key(&self) -> &str {
        self.run_id.rsplit('-').next().unwrap_or(&self.run_id)
    }

    /// The run directory, in the project's checkout.
    pub fn dir(&self) -> String {
        format!("{}/{RUNS_DIR}/{}", self.cwd, self.run_id)
    }

    /// Where the agents work and the run's git steps run: its worktree, or else the project's checkout.
    pub fn work_dir(&self) -> &str {
        self.worktree.as_deref().unwrap_or(&self.cwd)
    }

    /// Where the run keeps its local refs, which nothing pushes and done deletes.
    pub fn refs_prefix(&self) -> String {
        format!("{LOCAL_REFS_PREFIX}{}/", self.key())
    }

    /// Where the verdict step of round n writes its proofs (Pipeline::proof_command).
    pub fn proofs_dir(&self, n: u32) -> String {
        format!("{}/proofs-{n}", self.dir())
    }

    /// The local ref of the commit that holds the files the step locked.
    pub fn lock_ref(&self, step_id: &str) -> String {
        format!("{}locked/{step_id}", self.refs_prefix())
    }

    /// The default workflow's spec; the quality files below are every workflow's.
    pub fn spec_path(&self) -> String {
        format!("{}/spec.md", self.dir())
    }

    pub fn build_path(&self, n: u32) -> String {
        format!("{}/build-{n}.md", self.dir())
    }

    pub fn review_path(&self, n: u32) -> String {
        format!("{}/review-{n}.md", self.dir())
    }

    /// Where gitleaks' report on the push named push ("snapshot" or "publish") goes; each scan replaces it.
    pub fn leaks_path(&self, push: &str) -> String {
        format!("{}/leaks-{push}.json", self.dir())
    }

    pub fn quality_path(&self, n: u32, q: u32) -> String {
        format!("{}/quality-{n}-{q}.md", self.dir())
    }

    /// The Builder's answer to quality-<n>-<q>.md.
    pub fn quality_build_path(&self, n: u32, q: u32) -> String {
        format!("{}/build-{n}-q{q}.md", self.dir())
    }

    /// The report of the Builder's turn in round n that answers quality round q, or none when q is 0.
    pub fn builder_report_path(&self, n: u32, q: u32) -> String {
        if q == 0 {
            self.build_path(n)
        } else {
            self.quality_build_path(n, q)
        }
    }

    /// Whether the run has the quality gate, or had it until resume turned it off: whether it may have quality
    /// files.
    pub fn had_quality_gate(&self) -> bool {
        self.quality_job.is_some() || self.quality_gate_disabled
    }

    /// The Builder's last report in round n, once its quality rounds are over: the one the Reviewer reads.
    pub fn last_report_path(&self, n: u32) -> String {
        self.builder_report_path(n, self.quality_round.saturating_sub(1))
    }
}

/// The permission mode of a state saved by 0.3.0, which kept it as Claude Code's agent_args; None for none. Errors:
/// agent_args that are not --permission-mode and a mode.
fn saved_permission_mode(saved: &Map<Json>) -> Result<Option<String>, OrchestratorError> {
    let args = match saved.get("agent_args") {
        Some(Json::Array(args)) if !args.is_empty() => args,
        Some(other) if other.truthy() => &vec![other.clone()],
        _ => return Ok(None),
    };
    match args.as_slice() {
        [Json::Str(flag), Json::Str(mode)] if flag == "--permission-mode" => Ok(Some(mode.clone())),
        _ => {
            let run_id = saved.get("run_id").and_then(Json::as_str).unwrap_or("?");
            Err(OrchestratorError(format!(
                "run state {run_id} has agent_args {}, which name no permission mode",
                list_repr(args)
            )))
        }
    }
}

/// A list as Python's repr writes it, its strings quoted as repr quotes them.
fn list_repr(items: &[Json]) -> String {
    let parts: Vec<String> = items
        .iter()
        .map(|item| match item {
            Json::Str(s) => repr(s),
            Json::Null => "None".to_string(),
            Json::Bool(b) => if *b { "True" } else { "False" }.to_string(),
            other => other.dumps(),
        })
        .collect();
    format!("[{}]", parts.join(", "))
}

/// A saved state's fields, read into a RunState one by one, each one missing keeping its default.
struct Fields<'a> {
    saved: &'a Map<Json>,
    run_id: &'a str,
}

impl Fields<'_> {
    fn invalid(&self, key: &str, what: &str) -> OrchestratorError {
        OrchestratorError(format!("run state {} is invalid: {key} is not {what}", self.run_id))
    }

    fn required(&self, key: &str) -> Result<String, OrchestratorError> {
        match self.saved.get(key) {
            None => Err(OrchestratorError(format!("run state {} is incomplete: it has no {key}", self.run_id))),
            Some(Json::Str(s)) => Ok(s.clone()),
            Some(_) => Err(self.invalid(key, "a string")),
        }
    }

    fn text(&self, key: &str, slot: &mut String) -> Result<(), OrchestratorError> {
        match self.saved.get(key) {
            None => Ok(()),
            Some(Json::Str(s)) => {
                *slot = s.clone();
                Ok(())
            }
            Some(_) => Err(self.invalid(key, "a string")),
        }
    }

    fn optional(&self, key: &str, slot: &mut Option<String>) -> Result<(), OrchestratorError> {
        match self.saved.get(key) {
            None => Ok(()),
            Some(Json::Null) => {
                *slot = None;
                Ok(())
            }
            Some(Json::Str(s)) => {
                *slot = Some(s.clone());
                Ok(())
            }
            Some(_) => Err(self.invalid(key, "a string or null")),
        }
    }

    fn number<T: TryFrom<i128>>(&self, key: &str, slot: &mut T) -> Result<(), OrchestratorError> {
        match self.saved.get(key) {
            None => Ok(()),
            Some(value) => {
                let n = value.as_int().and_then(|n| T::try_from(n).ok());
                *slot = n.ok_or_else(|| self.invalid(key, "a whole number of its range"))?;
                Ok(())
            }
        }
    }

    fn flag(&self, key: &str, slot: &mut bool) -> Result<(), OrchestratorError> {
        match self.saved.get(key) {
            None => Ok(()),
            Some(Json::Bool(b)) => {
                *slot = *b;
                Ok(())
            }
            Some(_) => Err(self.invalid(key, "true or false")),
        }
    }

    fn strings(&self, key: &str, slot: &mut Vec<String>) -> Result<(), OrchestratorError> {
        let Some(value) = self.saved.get(key) else { return Ok(()) };
        *slot = string_list(value).ok_or_else(|| self.invalid(key, "a list of strings"))?;
        Ok(())
    }

    fn string_map(&self, key: &str, slot: &mut Map<String>) -> Result<(), OrchestratorError> {
        let Some(value) = self.saved.get(key) else { return Ok(()) };
        *slot = string_map(value).ok_or_else(|| self.invalid(key, "an object of strings"))?;
        Ok(())
    }

    fn object(&self, key: &str, slot: &mut Map<Json>) -> Result<(), OrchestratorError> {
        match self.saved.get(key) {
            None => Ok(()),
            Some(Json::Object(map)) => {
                *slot = map.clone();
                Ok(())
            }
            Some(_) => Err(self.invalid(key, "an object")),
        }
    }

    fn optional_object(&self, key: &str, slot: &mut Option<Map<Json>>) -> Result<(), OrchestratorError> {
        match self.saved.get(key) {
            None => Ok(()),
            Some(Json::Null) => {
                *slot = None;
                Ok(())
            }
            Some(Json::Object(map)) => {
                *slot = Some(map.clone());
                Ok(())
            }
            Some(_) => Err(self.invalid(key, "an object or null")),
        }
    }

    fn records(&self, key: &str, slot: &mut Vec<Map<Json>>) -> Result<(), OrchestratorError> {
        let Some(value) = self.saved.get(key) else { return Ok(()) };
        let records = value.as_array().and_then(|items| items.iter().map(|r| r.as_object().cloned()).collect());
        *slot = records.ok_or_else(|| self.invalid(key, "a list of objects"))?;
        Ok(())
    }

    /// Every field but run_id, task and cwd into state.
    fn read(&self, s: &mut RunState) -> Result<(), OrchestratorError> {
        self.optional("machine", &mut s.machine)?;
        if let Some(value) = self.saved.get("herdr_server").filter(|v| !v.is_null()) {
            let server = HerdrServer::from_json(value);
            let server = server.ok_or_else(|| self.invalid("herdr_server", "an object of herdr's variables"))?;
            s.herdr_server = Some(server);
        }
        self.text("phase", &mut s.phase)?;
        self.number("round", &mut s.round)?;
        self.text("workspace_id", &mut s.workspace_id)?;
        self.text("root_pane", &mut s.root_pane)?;
        self.optional("base", &mut s.base)?;
        self.optional("base_branch", &mut s.base_branch)?;
        self.optional("branch", &mut s.branch)?;
        self.optional("pr_url", &mut s.pr_url)?;
        self.optional("verdict", &mut s.verdict)?;
        self.strings("conflicts", &mut s.conflicts)?;
        self.optional("error", &mut s.error)?;
        if let Some(value) = self.saved.get("agents") {
            let agents = value.as_object().and_then(|map| {
                map.iter().map(|(role, agent)| string_map(agent).map(|a| (role, a))).collect::<Option<Map<_>>>()
            });
            s.agents = agents.ok_or_else(|| self.invalid("agents", "an object of agents"))?;
        }
        self.number("max_rounds", &mut s.max_rounds)?;
        self.number("turn_timeout", &mut s.turn_timeout)?;
        self.optional("permission_mode", &mut s.permission_mode)?;
        self.string_map("models", &mut s.models)?;
        self.string_map("agent_kinds", &mut s.agent_kinds)?;
        self.flag("pull_request", &mut s.pull_request)?;
        self.optional("prompted", &mut s.prompted)?;
        self.strings("retried", &mut s.retried)?;
        self.optional("retrying", &mut s.retrying)?;
        self.optional_object("owner", &mut s.owner)?;
        self.optional("heartbeat_at", &mut s.heartbeat_at)?;
        self.optional("quality_job", &mut s.quality_job)?;
        self.flag("quality_gate_disabled", &mut s.quality_gate_disabled)?;
        self.number("max_quality_rounds", &mut s.max_quality_rounds)?;
        self.number("quality_round", &mut s.quality_round)?;
        self.object("ci", &mut s.ci)?;
        self.text("workflow", &mut s.workflow)?;
        if let Some(definition) = self.saved.get("workflow_definition") {
            s.workflow_definition = (!definition.is_null()).then(|| definition.clone());
        }
        self.string_map("prompt_overrides", &mut s.prompt_overrides)?;
        self.optional("branch_cleanup", &mut s.branch_cleanup)?;
        self.optional("worktree", &mut s.worktree)?;
        self.optional("closed", &mut s.closed)?;
        self.records("turns", &mut s.turns)?;
        self.records("quality_rounds", &mut s.quality_rounds)?;
        self.optional_object("blocked", &mut s.blocked)?;
        if let Some(value) = self.saved.get("locked") {
            let locked = value.as_object().and_then(|map| {
                map.iter().map(|(step, paths)| string_list(paths).map(|p| (step, p))).collect::<Option<Map<_>>>()
            });
            s.locked = locked.ok_or_else(|| self.invalid("locked", "an object of lists of paths"))?;
        }
        self.records("restored", &mut s.restored)?;
        self.records("proofs", &mut s.proofs)?;
        Ok(())
    }
}

fn string_list(value: &Json) -> Option<Vec<String>> {
    value.as_array()?.iter().map(|item| item.as_str().map(String::from)).collect()
}

fn string_map(value: &Json) -> Option<Map<String>> {
    value.as_object()?.iter().map(|(k, v)| v.as_str().map(|v| (k, v.to_string()))).collect()
}

#[cfg(test)]
mod tests;
