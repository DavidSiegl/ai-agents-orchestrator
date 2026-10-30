//! A world: a project with an origin, fakes of everything the engine talks to, and the engine run in it.
//!
//! `World` is what a scenario (tests/scenarios.rs) is written against. It makes, in a directory of its own, a git
//! project whose `origin` is a bare repository beside it and puts the fake herdr, gh and ssh (crate::fakes) first on
//! PATH; the scenario scripts them, runs the engine as a program, and `record` then gives what the engine did,
//! normalized (crate::normalize), for `check` to compare with the scenario's recording.
//!
//! The engine is `E2E_ENGINE`, a command line, when set, else this checkout's orchestrator binary, built with cargo.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use nix::sys::signal::{killpg, Signal};
use nix::unistd::Pid;
use serde_json::{json, Map, Value};

use crate::fakes::{AGENT_KINDS, INVOCATION_VAR};
use crate::ledger::Ledger;
use crate::normalize::{facts, Normalizer};

/// The repository's top directory, where the Cargo workspace and the example workflows are.
pub fn checkout() -> PathBuf {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    dir.canonicalize().unwrap_or_else(|e| panic!("finding the checkout at {}: {e}", dir.display()))
}

/// Who the world's commits are by: the harness's own, and the engine's, through the git configuration it is given.
const GIT_NAME: &str = "Parity";
const GIT_EMAIL: &str = "parity@example.com";
/// The date of every commit the harness makes, so that the project's own history has the same names in every run.
const GIT_DATE: &str = "2026-01-01T00:00:00+00:00";
/// How long one engine invocation may take before the scenario fails.
const INVOCATION_TIMEOUT: Duration = Duration::from_secs(120);
/// How long a wait for a hold or a condition may take before the scenario fails, and how often it looks.
const WAIT_TIMEOUT: Duration = Duration::from_secs(60);
const WAIT_POLL: Duration = Duration::from_millis(50);
/// The project every world starts with, committed to origin's main: a Python package with a test, as the gate and the
/// orchestrator's checks expect to find one.
const PROJECT_FILES: [(&str, &str); 4] = [
    ("README.md", "# Parity project\n\nThe project a parity scenario changes.\n"),
    ("pyproject.toml", "[project]\nname = \"parity-project\"\nversion = \"0.1.0\"\ndependencies = []\n"),
    ("src/calc.py", "def add(a, b):\n    \"\"\"The sum of a and b.\"\"\"\n    return a + b\n"),
    ("tests/test_calc.py", "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"),
];

/// The fakes' programs, as the scenarios' test binary finds them (CARGO_BIN_EXE_fake-herdr and the others).
pub struct Fakes {
    pub herdr: PathBuf,
    pub gh: PathBuf,
    pub ssh: PathBuf,
}

/// The engine a world runs, as a command line: E2E_ENGINE's words, when it is set; else this checkout's orchestrator
/// binary, `cargo build --locked -p octoflow-engine --bin orchestrator` run once per process. Panics, failing the
/// scenario, where the build fails, so that a machine without cargo fails the scenarios rather than skipping them.
pub fn engine_command() -> Vec<String> {
    static BUILT: OnceLock<Vec<String>> = OnceLock::new();
    BUILT
        .get_or_init(|| match std::env::var("E2E_ENGINE") {
            Ok(command) => shell_words(&command),
            Err(_) => vec![built_engine()],
        })
        .clone()
}

/// The path of this checkout's orchestrator binary, built with cargo, which reports where it put it.
fn built_engine() -> String {
    let cargo = std::env::var("CARGO").unwrap_or_else(|_| "cargo".into());
    let out = Command::new(&cargo)
        .args(["build", "--locked", "--quiet", "-p", "octoflow-engine", "--bin", "orchestrator", "--message-format=json"])
        .current_dir(checkout())
        .stderr(Stdio::inherit())
        .output()
        .unwrap_or_else(|e| panic!("running {cargo}, which builds the engine: {e}"));
    assert!(out.status.success(), "cargo could not build the engine: {}", out.status);
    let paths: Vec<String> = String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|m| m["reason"] == "compiler-artifact")
        .filter_map(|m| m["executable"].as_str().map(String::from))
        .collect();
    match paths.as_slice() {
        [one] => one.clone(),
        _ => panic!("cargo reported {} binaries for the engine, not one: {paths:?}", paths.len()),
    }
}

/// command's words, as a POSIX shell splits them: at unquoted white space, with `'...'` taken as it is, and `"..."`
/// and a backslash outside quotes escaping the next character.
fn shell_words(command: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut word: Option<String> = None;
    let mut chars = command.chars();
    while let Some(c) = chars.next() {
        match c {
            c if c.is_whitespace() => words.extend(word.take()),
            '\'' => word.get_or_insert_with(String::new).extend(chars.by_ref().take_while(|&c| c != '\'')),
            '"' => {
                let w = word.get_or_insert_with(String::new);
                while let Some(c) = chars.next() {
                    match c {
                        '"' => break,
                        '\\' => w.extend(chars.next()),
                        c => w.push(c),
                    }
                }
            }
            '\\' => word.get_or_insert_with(String::new).extend(chars.next()),
            c => word.get_or_insert_with(String::new).push(c),
        }
    }
    words.extend(word);
    words
}

/// One run of the engine in a world: its arguments, its process and, once it ended, its exit status.
pub struct Invocation {
    /// Its number in the world, from 1, which its fakes log as E2E_INVOCATION.
    pub n: usize,
    /// The engine's arguments, without the engine's own command.
    pub args: Vec<String>,
    child: Child,
    /// Where its standard output goes; its error goes to the same path with `.err` added.
    out_path: PathBuf,
    /// Whether the record holds its output (World::start).
    output_recorded: bool,
    /// Its exit status once it has ended; for a signal, minus the signal's number, as Python's Popen gives it.
    exit: Option<i32>,
}

impl Invocation {
    /// The pid of the engine's process, the leader of its session and process group.
    pub fn pid(&self) -> i64 {
        i64::from(self.child.id())
    }

    /// Its exit status, once it has ended; kills it and fails the scenario, with its error output, when it runs longer
    /// than INVOCATION_TIMEOUT.
    pub fn wait(&mut self) -> i32 {
        let deadline = Instant::now() + INVOCATION_TIMEOUT;
        loop {
            match self.child.try_wait() {
                Ok(Some(status)) => {
                    let exit = status.code().unwrap_or_else(|| -status.signal().unwrap_or(0));
                    self.exit = Some(exit);
                    return exit;
                }
                Ok(None) if Instant::now() > deadline => {
                    self.kill();
                    let err = self.stderr();
                    let tail = &err[err.len().saturating_sub(3000)..];
                    panic!("engine {:?} ran over {INVOCATION_TIMEOUT:?}:\n{tail}", self.args);
                }
                Ok(None) => std::thread::sleep(WAIT_POLL),
                Err(e) => panic!("waiting for engine {:?}: {e}", self.args),
            }
        }
    }

    /// Kill its process group with SIGKILL, and reap it.
    fn kill(&mut self) {
        let _ = killpg(Pid::from_raw(self.child.id() as i32), Signal::SIGKILL);
        let _ = self.child.wait();
    }

    /// What it printed to its standard output so far.
    pub fn stdout(&self) -> String {
        read_text(&self.out_path)
    }

    /// What it printed to its standard error so far.
    pub fn stderr(&self) -> String {
        read_text(&err_path(&self.out_path))
    }
}

fn err_path(out_path: &Path) -> PathBuf {
    let mut path = out_path.as_os_str().to_owned();
    path.push(".err");
    PathBuf::from(path)
}

/// The text of the file at path, as Python's open() reads it: undecodable bytes replaced, and each "\r\n" and "\r"
/// read as "\n". Panics, failing the scenario, where it cannot be read.
fn read_text(path: &Path) -> String {
    let bytes = fs::read(path).unwrap_or_else(|e| panic!("reading {}: {e}", path.display()));
    python_text(&bytes)
}

/// bytes decoded as Python's text mode decodes them: lossily, with universal newlines.
fn python_text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).replace("\r\n", "\n").replace('\r', "\n")
}

/// A world a scenario runs the engine in, and what it did there.
///
/// Its directory holds `project/` (the git checkout the engine runs in, on main, with origin at `origin.git/`),
/// `home/` and `config/` (the engine's HOME and XDG_CONFIG_HOME, so that its configuration directory is
/// `config/ai-agents-orchestrator/`), `bin/` (the fakes), `fakes/` (their ledger) and `out/` (each invocation's
/// output). Dropping it kills any invocation still running.
pub struct World {
    /// The world's directory.
    pub root: PathBuf,
    /// The git checkout the engine runs in.
    pub project: PathBuf,
    /// The project's origin, a bare repository.
    pub origin: PathBuf,
    pub normalizer: Normalizer,
    pub invocations: Vec<Invocation>,
    pub ledger: Ledger,
    /// What the scenario observed itself, by name, recorded under notes.
    notes: Map<String, Value>,
    /// The scenario's script, which the fakes read from script.json.
    script: Value,
}

impl World {
    /// A world in root, an empty directory it may fill, which its owner removes afterwards; the fakes are fakes'.
    /// Panics, failing the scenario, where gitleaks, which the engine runs for real, is not on PATH.
    pub fn new(root: &Path, fakes: &Fakes) -> World {
        let root = root.canonicalize().unwrap_or_else(|e| panic!("finding {}: {e}", root.display()));
        // Why a missing gitleaks fails the scenario rather than skipping it: TODO.md, "The Rust engine's parity
        // harness".
        let path = std::env::var_os("PATH").unwrap_or_default();
        assert!(
            std::env::split_paths(&path).any(|dir| dir.join("gitleaks").is_file()),
            "gitleaks is not on PATH; the e2e scenarios need it"
        );
        let mut normalizer = Normalizer::new();
        normalizer.literal(&root.to_string_lossy(), "<world>");
        // This checkout, where the example workflows a scenario runs are.
        normalizer.literal(&checkout().to_string_lossy(), "<checkout>");
        let project = root.join("project");
        let origin = root.join("origin.git");
        let script = json!({"project": project, "origin": origin, "agents": {}, "machines": [], "gh": {}});
        for d in ["home", "config/ai-agents-orchestrator", "bin", "fakes", "out", "tmp"] {
            fs::create_dir_all(root.join(d)).unwrap_or_else(|e| panic!("making {d} in {}: {e}", root.display()));
        }
        let world = World {
            ledger: Ledger::new(&root),
            root,
            project,
            origin,
            normalizer,
            invocations: Vec::new(),
            notes: Map::new(),
            script,
        };
        world.write_script();
        world.make_bin(fakes);
        world.make_repositories();
        world
    }

    // -- Scripting the world ------------------------------------------------

    /// Script the role's agent: each action answers one prompt, in order, after those scripted before (see
    /// crate::fakes's perform for what an action holds). A prompt beyond the last makes the agent exit.
    pub fn agent(&mut self, role: &str, actions: impl IntoIterator<Item = Value>) {
        let agents = self.script["agents"].as_object_mut().expect("the script's agents");
        let list = agents.entry(role).or_insert_with(|| json!([])).as_array_mut().expect("a role's actions");
        list.extend(actions);
        self.write_script();
    }

    /// Save a herdr machine of that label, reached over the fake ssh, which runs every command here.
    pub fn machine(&mut self, label: &str) {
        let machine = json!({"label": label, "id": format!("id-{label}"), "enabled": true, "target": format!("parity@{label}")});
        self.script["machines"].as_array_mut().expect("the script's machines").push(machine);
        self.write_script();
    }

    /// Change what gh says of pull request n, the n-th the engine opened: state (OPEN, CLOSED or MERGED), closedAt,
    /// mergeable, isDraft.
    pub fn set_pr(&self, n: usize, fields: Value) {
        self.ledger.change(|state| {
            let pr = state["prs"][&n.to_string()].as_object_mut().unwrap_or_else(|| panic!("no pull request {n}"));
            pr.extend(fields.as_object().cloned().expect("the fields as an object"));
        });
    }

    /// Pull request n as gh holds it: base, head, title, body, isDraft, state, closedAt, mergeable.
    pub fn pr(&self, n: usize) -> Value {
        self.ledger.change(|state| state["prs"][&n.to_string()].clone())
    }

    /// Let what waits on hold go on: an agent's action is done at herdr's next command.
    pub fn release(&self, hold: &str) {
        self.ledger.change(|state| {
            let released = state.entry("released").or_insert_with(|| json!([]));
            released.as_array_mut().expect("the released holds").push(hold.into());
        });
    }

    /// Wait until the engine waits on hold, at a point where it does nothing but poll until hold is released, so that
    /// what it did before is the same in every run: it polled the agent after prompting it.
    pub fn wait_reached(&self, hold: &str) {
        let reached = || {
            self.ledger.change(|state| state.get("reached").and_then(Value::as_array).is_some_and(|r| r.contains(&hold.into())))
        };
        wait_for(reached, &format!("hold {hold} to be reached"));
    }

    /// Write text to path, relative to the world's directory, making its directories; its full path.
    pub fn write(&self, path: &str, text: &str) -> String {
        let full = self.root.join(path);
        if let Some(dir) = full.parent() {
            fs::create_dir_all(dir).unwrap_or_else(|e| panic!("making {}: {e}", dir.display()));
        }
        fs::write(&full, text).unwrap_or_else(|e| panic!("writing {}: {e}", full.display()));
        full.to_string_lossy().into_owned()
    }

    /// Commit files, path to text, on origin's main, as another developer's push would, without touching the
    /// project's checkout.
    pub fn push_to_origin(&self, files: &[(&str, &str)], message: &str) {
        let clone = self.root.join("tmp").join("pusher");
        if !clone.exists() {
            self.git(&self.root, &["clone", "--quiet", &self.origin.to_string_lossy(), &clone.to_string_lossy()]);
        }
        self.git(&clone, &["fetch", "--quiet", "origin"]);
        self.git(&clone, &["checkout", "--quiet", "-B", "main", "origin/main"]);
        for (path, text) in files {
            let full = clone.join(path);
            fs::write(&full, text).unwrap_or_else(|e| panic!("writing {}: {e}", full.display()));
        }
        self.git(&clone, &["add", "--all"]);
        self.git(&clone, &["commit", "--quiet", "-m", message]);
        self.git(&clone, &["push", "--quiet", "origin", "main"]);
    }

    /// git args in cwd, as the harness, with the world's identity and fixed dates; its output. Fails the scenario
    /// when git fails.
    pub fn git(&self, cwd: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .arg("-C")
            .arg(cwd)
            .args(args)
            .env_clear()
            .envs(self.env(None))
            .stdin(Stdio::null())
            .output()
            .unwrap_or_else(|e| panic!("running git {args:?} in {}: {e}", cwd.display()));
        assert!(
            out.status.success(),
            "git {args:?} in {} failed: {}",
            cwd.display(),
            String::from_utf8_lossy(&out.stderr)
        );
        python_text(&out.stdout)
    }

    /// Add observed, anything JSON-ready, to the record under notes/key, normalized with the rest: what a scenario
    /// observed itself, such as a pull request's body.
    pub fn note(&mut self, key: &str, observed: Value) {
        self.notes.insert(key.into(), observed);
    }

    // -- Running the engine ---------------------------------------------------

    /// Start the engine with args in the project, in a session of its own, and return at once; its invocation's
    /// index in `invocations`. Without output_recorded, the record keeps only its exit status: for output that differs
    /// from host to host, such as doctor's version of git, which the scenario notes what it needs of.
    pub fn start(&mut self, args: &[&str], output_recorded: bool) -> usize {
        let n = self.invocations.len() + 1;
        let out_path = self.root.join("out").join(n.to_string());
        let create = |path: &Path| fs::File::create(path).unwrap_or_else(|e| panic!("making {}: {e}", path.display()));
        let engine = engine_command();
        // setsid execs the engine in its own process, as Python's start_new_session made it: the leader of a session
        // and a process group of its own, which a kill takes down whole.
        let child = Command::new("setsid")
            .args(&engine)
            .args(args)
            .current_dir(&self.project)
            .env_clear()
            .envs(self.env(Some(n)))
            .stdin(Stdio::null())
            .stdout(create(&out_path))
            .stderr(create(&err_path(&out_path)))
            .spawn()
            .unwrap_or_else(|e| panic!("starting the engine {engine:?}: {e}"));
        let pid = i64::from(child.id());
        self.normalizer.pid(pid, n);
        self.ledger.change(|state| {
            let engines = state.entry("engines").or_insert_with(|| json!({}));
            engines.as_object_mut().expect("the ledger's engines").insert(n.to_string(), pid.into());
        });
        let args = args.iter().map(|a| a.to_string()).collect();
        self.invocations.push(Invocation { n, args, child, out_path, output_recorded, exit: None });
        n - 1
    }

    /// Run the engine with args, as start does, and wait for it to end; its invocation's index.
    pub fn run(&mut self, args: &[&str]) -> usize {
        let i = self.start(args, true);
        self.invocations[i].wait();
        i
    }

    /// The key of the n-th run the engine started in the project, by the order of their first herdr workspace; fails
    /// the scenario when there is none.
    pub fn run_key(&self, n: usize) -> String {
        let keys: Vec<String> = self
            .ledger
            .events()
            .iter()
            .filter_map(|e| e.get("herdr").and_then(Value::as_array))
            .map(|c| {
                let words: Vec<&str> = c.iter().filter_map(Value::as_str).collect();
                if words.first() == Some(&"--machine") { words[2..].to_vec() } else { words }
            })
            .filter(|c| c.get(..2) == Some(&["workspace", "create"]))
            .map(|c| {
                let i = c.iter().position(|w| *w == "--label").expect("a workspace's --label");
                c[i + 1].split_whitespace().next().unwrap_or_default().to_string()
            })
            .collect();
        keys.get(n - 1).cloned().unwrap_or_else(|| panic!("no run {n} in the project; workspaces were made for {keys:?}"))
    }

    /// The saved state of the run with that key, as state.json holds it now.
    pub fn run_state(&self, key: &str) -> Value {
        let runs = self.project.join(".orchestrator").join("runs");
        let names: Vec<PathBuf> = fs::read_dir(&runs)
            .unwrap_or_else(|e| panic!("listing {}: {e}", runs.display()))
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().ends_with(&format!("-{key}")))
            .map(|e| e.path())
            .collect();
        let [dir] = names.as_slice() else { panic!("no single run for key {key}: {names:?}") };
        let text = read_text(&dir.join("state.json"));
        serde_json::from_str(&text).unwrap_or_else(|e| panic!("the state of run {key} is no JSON: {e}"))
    }

    /// The environment of engine invocation n, or of the harness's own git for None: built afresh, so that nothing of
    /// the developer's own configuration reaches the engine.
    fn env(&self, n: Option<usize>) -> Vec<(String, String)> {
        let at = |p: &str| self.root.join(p).to_string_lossy().into_owned();
        let path = std::env::var("PATH").unwrap_or_default();
        let mut env = vec![
            ("PATH".to_string(), format!("{}:{path}", at("bin"))),
            ("HOME".into(), at("home")),
            ("XDG_CONFIG_HOME".into(), at("config")),
            ("TMPDIR".into(), at("tmp")),
            ("LC_ALL".into(), "C.UTF-8".into()),
            ("TZ".into(), "UTC".into()),
            ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
            ("GIT_CONFIG_GLOBAL".into(), at("gitconfig")),
            ("GIT_CONFIG_NOSYSTEM".into(), "1".into()),
        ];
        match n {
            None => {
                env.push(("GIT_AUTHOR_DATE".into(), GIT_DATE.into()));
                env.push(("GIT_COMMITTER_DATE".into(), GIT_DATE.into()));
            }
            Some(n) => {
                env.push(("HERDR_ENV".into(), "1".into()));
                env.push((INVOCATION_VAR.into(), n.to_string()));
                // An engine instrumented for coverage would write its profile where it runs, into the project.
                env.push(("LLVM_PROFILE_FILE".into(), "/dev/null".into()));
            }
        }
        env
    }

    // -- Making the world -------------------------------------------------------

    fn write_script(&self) {
        let path = self.root.join("script.json");
        let tmp = self.root.join("script.json.tmp");
        fs::write(&tmp, self.script.to_string()).unwrap_or_else(|e| panic!("writing {}: {e}", tmp.display()));
        fs::rename(&tmp, &path).unwrap_or_else(|e| panic!("replacing {}: {e}", path.display()));
    }

    /// Shims of the fakes in bin/, and the harnesses' CLIs.
    fn make_bin(&self, fakes: &Fakes) {
        let root = quoted(&self.root.to_string_lossy());
        let mut shims: Vec<(&str, String)> = [("herdr", &fakes.herdr), ("gh", &fakes.gh), ("ssh", &fakes.ssh)]
            .into_iter()
            .map(|(program, fake)| (program, format!("exec {} {root} \"$@\"", quoted(&fake.to_string_lossy()))))
            .collect();
        // The harnesses' CLIs, which doctor looks for on PATH and herdr would start: nothing runs them here.
        shims.extend(AGENT_KINDS.map(|kind| (kind, "exit 0".to_string())));
        for (program, body) in shims {
            let path = self.root.join("bin").join(program);
            fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap_or_else(|e| panic!("writing {}: {e}", path.display()));
            fs::set_permissions(&path, fs::Permissions::from_mode(0o755))
                .unwrap_or_else(|e| panic!("making {} executable: {e}", path.display()));
        }
    }

    fn make_repositories(&self) {
        let config = format!(
            "[user]\n\tname = {GIT_NAME}\n\temail = {GIT_EMAIL}\n[commit]\n\tgpgsign = false\n\
             [init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n"
        );
        self.write("gitconfig", &config);
        let (origin, project) = (self.origin.to_string_lossy(), self.project.to_string_lossy());
        self.git(&self.root, &["init", "--quiet", "--bare", "-b", "main", &origin]);
        self.git(&self.root, &["init", "--quiet", "-b", "main", &project]);
        for (path, text) in PROJECT_FILES {
            self.write(&format!("project/{path}"), text);
        }
        self.git(&self.project, &["add", "--all"]);
        self.git(&self.project, &["commit", "--quiet", "-m", "Start the parity project"]);
        self.git(&self.project, &["remote", "add", "origin", &origin]);
        self.git(&self.project, &["push", "--quiet", "--set-upstream", "origin", "main"]);
    }

    // -- The record -------------------------------------------------------------

    /// What the scenario did, normalized: each invocation's arguments, exit status and output; per invocation, the
    /// commands herdr and gh got, in order, with state.json wherever it changed between them; the run directories'
    /// files; both repositories' refs, commits and working trees; and the scenario's notes. Output read by programs is
    /// kept whole, a human's only as its facts (crate::normalize::facts).
    pub fn record(&mut self) -> Value {
        let invocations: Vec<Value> = self.invocations.iter().map(invocation_record).collect();
        let events = self.events();
        let raw = json!({
            "invocations": invocations,
            "events": events,
            "run_files": self.run_files(),
            "repositories": {"project": self.repository(&self.project), "origin": self.repository(&self.origin)},
            "notes": self.notes,
        });
        self.normalizer.learn(&raw);
        self.normalized(&raw)
    }

    fn normalized(&self, raw: &Value) -> Value {
        let n = &self.normalizer;
        let mut out = n.json(raw, false, None);
        for inv in out["invocations"].as_array_mut().expect("the invocations") {
            for stream in ["stdout_facts", "stderr_facts"] {
                if let Some(Value::String(text)) = inv.get(stream) {
                    inv[stream] = json!(facts(&n.text(text, true)));
                }
            }
        }
        // Texts that word the clock's times, as the turns tables do.
        for (by, raw_events) in raw["events"].as_object().expect("the events") {
            let events = out["events"][&n.text(by, false)].as_array_mut().expect("an invocation's events");
            for (raw_event, event) in raw_events.as_array().expect("an invocation's events").iter().zip(events) {
                if let Some(Value::String(stdin)) = raw_event.get("stdin") {
                    event["stdin"] = n.text(stdin, true).into();
                }
            }
        }
        for (path, text) in raw["run_files"].as_object().expect("the run files") {
            if path.ends_with("summary.md") {
                out["run_files"][&n.text(path, false)] = n.text(text.as_str().expect("a file's text"), true).into();
            }
        }
        // What a scenario notes, such as a pull request's body, may word the clock's times too.
        out["notes"] = n.json(&raw["notes"], true, None);
        out
    }

    /// The ledger's events by invocation, each state.json parsed, and polls that repeated collapsed into one.
    fn events(&mut self) -> Value {
        let mut by: Map<String, Value> = Map::new();
        for mut event in self.ledger.events() {
            let who = event.shift_remove("by").and_then(|b| b.as_str().map(String::from)).expect("an event's invocation");
            if let Some(state) = event.get_mut("state") {
                let text = state["text"].as_str().expect("a state's text");
                let saved: Value = serde_json::from_str(text).expect("a logged state.json is JSON");
                // The fakes log a state only while its owner, if any, is a process of the invocation logging it.
                if let (Some(pid), Ok(n)) = (saved["owner"]["pid"].as_i64(), who.parse::<usize>()) {
                    self.normalizer.pid(pid, n);
                }
                *state = json!({"path": state["path"], "saved": saved});
            }
            by.entry(who).or_insert_with(|| json!([])).as_array_mut().expect("events").push(Value::Object(event));
        }
        for events in by.values_mut() {
            *events = Value::Array(collapsed(events.as_array().expect("events")));
        }
        Value::Object(by)
    }

    /// Every file under the project's .orchestrator, but the worktrees, by its path there, in path order.
    fn run_files(&self) -> Value {
        let top = self.project.join(".orchestrator");
        let mut files = Map::new();
        walk(&top, &top, &mut files);
        Value::Object(files)
    }

    /// The repository's refs, every commit they reach, from the refs in name order and each commit's parents in
    /// order, and for a repository with a working tree, its worktrees' status and changed files.
    fn repository(&self, repo: &Path) -> Value {
        let mut refs = Map::new();
        for line in self.git(repo, &["for-each-ref", "--format=%(refname) %(objectname)"]).lines() {
            let (name, sha) = line.split_once(' ').expect("a ref and its object");
            refs.insert(name.into(), sha.into());
        }
        let symbolic = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["symbolic-ref", "--quiet", "HEAD"])
            .output()
            .is_ok_and(|out| out.status.success());
        let head = if symbolic { self.git(repo, &["symbolic-ref", "--quiet", "HEAD"]).trim().to_string() } else { "detached".into() };
        let mut sorted: Vec<(&String, &Value)> = refs.iter().collect();
        sorted.sort_by(|a, b| a.0.cmp(b.0));
        let mut todo: Vec<String> = sorted.into_iter().map(|(_, sha)| sha.as_str().expect("a sha").to_string()).collect();
        let mut commits: Vec<(String, Value)> = Vec::new();
        while !todo.is_empty() {
            let sha = todo.remove(0);
            if commits.iter().any(|(s, _)| *s == sha) || self.git(repo, &["cat-file", "-t", &sha]).trim() != "commit" {
                continue;
            }
            let commit = self.commit(repo, &sha);
            let parents: Vec<String> =
                commit["parents"].as_array().expect("parents").iter().map(|p| p.as_str().expect("a sha").into()).collect();
            todo.splice(0..0, parents);
            commits.push((sha, commit));
        }
        let mut out = json!({"head": head, "refs": refs, "commits": commits.into_iter().map(|(_, c)| c).collect::<Vec<_>>()});
        if !repo.to_string_lossy().ends_with(".git") {
            out["worktrees"] = self.worktrees(repo);
        }
        out
    }

    fn commit(&self, repo: &Path, sha: &str) -> Value {
        let parents: Vec<String> = self.git(repo, &["show", "--no-patch", "--format=%P", sha]).split_whitespace().map(String::from).collect();
        let message = self.git(repo, &["show", "--no-patch", "--format=%an <%ae>%n%B", sha]);
        let (author, body) = message.split_once('\n').unwrap_or((&message, ""));
        let changes: Vec<String> = self
            .git(repo, &["diff-tree", "--root", "--no-commit-id", "-r", "--name-status", "-m", "--first-parent", sha])
            .lines()
            .map(String::from)
            .collect();
        let mut files = Map::new();
        for line in &changes {
            let (change, path) = line.split_once('\t').expect("a change and its path");
            if change != "D" {
                files.insert(path.into(), self.git(repo, &["show", &format!("{sha}:{path}")]).into());
            }
        }
        json!({"sha": sha, "parents": parents, "author": author, "message": body, "changes": changes, "files": files})
    }

    /// Each worktree of repo, its path, HEAD, status and the text of each file the status names.
    fn worktrees(&self, repo: &Path) -> Value {
        let mut out = Vec::new();
        let mut tree: Map<String, Value> = Map::new();
        let listing = self.git(repo, &["worktree", "list", "--porcelain"]);
        for line in listing.lines().chain([""]) {
            if !line.is_empty() {
                let (name, text) = line.split_once(' ').unwrap_or((line, ""));
                tree.insert(name.into(), text.into());
                continue;
            }
            if tree.is_empty() {
                continue;
            }
            let path = tree["worktree"].as_str().expect("a worktree's path").to_string();
            let porcelain: Vec<String> =
                self.git(Path::new(&path), &["status", "--porcelain", "--untracked-files=all"]).lines().map(String::from).collect();
            let mut changed = Map::new();
            for entry in &porcelain {
                let name: String = entry.chars().skip(3).collect();
                let full = Path::new(&path).join(&name);
                if full.is_file() && !name.starts_with(".orchestrator/") {
                    changed.insert(name, read_text(&full).into());
                }
            }
            let branch = tree.get("branch").cloned().unwrap_or_else(|| "detached".into());
            out.push(json!({"path": path, "branch": branch, "status": porcelain, "changed": changed}));
            tree = Map::new();
        }
        Value::Array(out)
    }
}

impl Drop for World {
    fn drop(&mut self) {
        for inv in &mut self.invocations {
            if matches!(inv.child.try_wait(), Ok(None)) {
                inv.kill();
            }
        }
    }
}

/// Poll condition() until it is true; fails the scenario, naming what, after WAIT_TIMEOUT.
pub fn wait_for(condition: impl Fn() -> bool, what: &str) {
    let deadline = Instant::now() + WAIT_TIMEOUT;
    while !condition() {
        assert!(Instant::now() <= deadline, "waited {WAIT_TIMEOUT:?} for {what}");
        std::thread::sleep(WAIT_POLL);
    }
}

/// s as a single-quoted word of sh.
fn quoted(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

fn invocation_record(inv: &Invocation) -> Value {
    let exit = inv.exit.map_or(Value::Null, Value::from);
    let mut record = json!({"args": inv.args, "exit": exit});
    if !inv.output_recorded {
        return record;
    }
    let stream = if machine_read(&inv.args) { "stdout" } else { "stdout_facts" };
    record[stream] = inv.stdout().into();
    record["stderr_facts"] = inv.stderr().into();
    record
}

/// Whether the engine's standard output for args is read by a program, so that it must come out byte for byte
/// (spec, Parity): a run's or resume's last line, list --json, metrics, serve's first line, and a workflow printed as
/// a file.
fn machine_read(args: &[String]) -> bool {
    let words: Vec<&str> = args.iter().map(String::as_str).filter(|a| !a.starts_with('-')).collect();
    let command = words.first().copied().unwrap_or_default();
    ["run", "resume", "metrics", "serve"].contains(&command)
        || (command == "list" && args.iter().any(|a| a == "--json"))
        || (command == "workflows" && words.len() > 1)
}

/// events with each run of events that repeats the run just before it dropped, for runs of up to four: a poll the
/// engine repeats while it waits comes out once, however long the wait took.
fn collapsed(events: &[Value]) -> Vec<Value> {
    let mut out = events.to_vec();
    let mut changed = true;
    while changed {
        changed = false;
        for k in 1..5 {
            let mut i = 0;
            while i + 2 * k <= out.len() {
                if out[i..i + k] == out[i + k..i + 2 * k] {
                    out.drain(i + k..i + 2 * k);
                    changed = true;
                } else {
                    i += 1;
                }
            }
        }
    }
    out
}

/// Add each file under dir to files, by its path relative to top, as Python's os.walk visits them: a directory's
/// files in name order, then each of its directories in name order, a symbolic link to one not followed; and at top,
/// no `worktrees`.
fn walk(top: &Path, dir: &Path, files: &mut Map<String, Value>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    let mut names: Vec<(String, PathBuf)> =
        entries.filter_map(|e| e.ok()).map(|e| (e.file_name().to_string_lossy().into_owned(), e.path())).collect();
    names.sort();
    let mut dirs = Vec::new();
    for (name, path) in names {
        if path.is_dir() {
            let link = fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink());
            if !link && !(dir == top && name == "worktrees") {
                dirs.push(path);
            }
        } else {
            let relative = path.strip_prefix(top).expect("a path under top").to_string_lossy().into_owned();
            files.insert(relative, read_text(&path).into());
        }
    }
    for d in dirs {
        walk(top, &d, files);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shell_words_splits_as_sh() {
        assert_eq!(shell_words("env A=/dev/null '/x y/orch' \"a\\\"b\" c\\ d"), ["env", "A=/dev/null", "/x y/orch", "a\"b", "c d"]);
    }

    #[test]
    fn collapsed_drops_repeated_runs() {
        let e: Vec<Value> = [1, 2, 1, 2, 1, 2, 3, 3, 3, 4].map(Value::from).to_vec();
        assert_eq!(collapsed(&e), [1, 2, 3, 4].map(Value::from).to_vec());
    }

    #[test]
    fn output_is_machine_read_for_the_commands_programs_read() {
        let words = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert!(machine_read(&words(&["run", "--", "task"])));
        assert!(machine_read(&words(&["list", "--json"])));
        assert!(!machine_read(&words(&["list"])));
        assert!(machine_read(&words(&["workflows", "default"])));
        assert!(!machine_read(&words(&["workflows"])));
    }
}
