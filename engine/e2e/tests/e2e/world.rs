//! A world: a project with an origin, fakes of everything the engine talks to, and the engine run in it.
//!
//! `World` is what a scenario (scenarios.rs) is written against. It makes, in a temporary directory, a git project
//! whose `origin` is a bare repository beside it, and puts the fake herdr, gh and ssh (octoflow_e2e::fakes) first on
//! PATH; the scenario scripts each of them, runs the engine as a program, and `record` then gives what the run did,
//! normalized, for main.rs to compare with the scenario's recording.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use nix::sys::signal::{kill, killpg, Signal};
use nix::unistd::Pid;
use octoflow_e2e::fakes::AGENT_KINDS;
use octoflow_e2e::ledger::{write_atomically, Ledger, INVOCATION_VAR};
use octoflow_e2e::normalize::{facts, Normalizer};
use octoflow_e2e::scratch::Scratch;
use octoflow_e2e::{array_at, object, object_at, procs, str_at, strings};
use octoflow_engine::py::json::{Json, Map};
use octoflow_engine::py::text::shlex_quote;

/// Who the world's commits are by: the harness's own, and the engine's, through the git configuration it is given.
const GIT_NAME: &str = "Parity";
const GIT_EMAIL: &str = "parity@example.com";
/// The date of every commit the harness makes, so that the project's own history has the same names in every run.
const GIT_DATE: &str = "2026-01-01T00:00:00+00:00";
/// How long one engine invocation may take before the scenario fails, and a wait for a hold or a condition.
const INVOCATION_TIMEOUT: Duration = Duration::from_secs(120);
const WAIT_TIMEOUT: Duration = Duration::from_secs(60);
const WAIT_POLL: Duration = Duration::from_millis(50);
/// The project every world starts with, committed to origin's main: a Python package with a test, as the orchestrator's
/// checks expect to find one.
const PROJECT_FILES: [(&str, &str); 4] = [
    ("README.md", "# Parity project\n\nThe project a parity scenario changes.\n"),
    ("pyproject.toml", "[project]\nname = \"parity-project\"\nversion = \"0.1.0\"\ndependencies = []\n"),
    ("src/calc.py", "def add(a, b):\n    \"\"\"The sum of a and b.\"\"\"\n    return a + b\n"),
    ("tests/test_calc.py", "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"),
];

/// The repository's top directory, where the example workflows a scenario runs are.
pub fn checkout() -> PathBuf {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    manifest.parent().and_then(Path::parent).expect("engine/e2e is two levels below the checkout").to_path_buf()
}

/// One run of the engine in a world: its arguments, its process and, once it ended, its exit status.
pub struct Invocation {
    /// The engine's arguments, without the engine itself.
    pub args: Vec<String>,
    process: Child,
    /// Where its standard output goes; its standard error goes to the same path with `.err` added.
    out_path: PathBuf,
    /// Whether the record holds its output (World::start).
    output_recorded: bool,
    /// Whether the scenario killed it, rather than letting it end.
    killed: bool,
    /// Its exit status once it ended, a signal's as minus its number, as Python's returncode gives it.
    exit: Option<i32>,
}

impl Invocation {
    /// The pid of its process.
    pub fn pid(&self) -> i32 {
        i32::try_from(self.process.id()).expect("a pid fits an i32")
    }

    /// Its exit status, once it has ended; kills it and fails the scenario, with its error output, when it runs longer
    /// than INVOCATION_TIMEOUT.
    pub fn wait(&mut self) -> i32 {
        let deadline = Instant::now() + INVOCATION_TIMEOUT;
        loop {
            let ended = self.process.try_wait().unwrap_or_else(|e| panic!("waiting for the engine: {e}"));
            if let Some(ended) = ended {
                let exit = ended.code().unwrap_or_else(|| -ended.signal().unwrap_or(0));
                self.exit = Some(exit);
                return exit;
            }
            if Instant::now() > deadline {
                self.kill();
                let err = self.stderr();
                let tail = &err[err.len().saturating_sub(3000)..];
                panic!("engine {:?} ran over {INVOCATION_TIMEOUT:?}:\n{tail}", self.args);
            }
            sleep(WAIT_POLL);
        }
    }

    /// Whether it is still running.
    pub fn running(&mut self) -> bool {
        matches!(self.process.try_wait(), Ok(None))
    }

    /// Kill it with SIGKILL, and every process it started, as a crash or a lost machine would stop it: its process
    /// group, and each process below it in groups of their own, as the engine starts every command
    /// (engine/src/host/process.rs), which a kill of the engine's group alone would leave running. Each is stopped
    /// first, until a listing finds none new, so that none starts another between the listing and the kill.
    ///
    /// A listing of the processes that fails ends the stopping, and what is stopped so far is killed all the same, so
    /// that nothing is left stopped; it is reported on standard error, as is a signal that cannot be sent to a process
    /// that is still there. Neither panics, since a kill runs from World's drop, also while a scenario fails.
    pub fn kill(&mut self) {
        let group = Pid::from_raw(self.pid());
        let mut stopped = std::collections::BTreeSet::new();
        loop {
            // The engine's group, then what the listing finds below it that is not stopped yet.
            signal(killpg(group, Signal::SIGSTOP), "stopping the engine's group");
            let below: Vec<i32> = match procs::descendants(self.pid()) {
                Ok(found) => found.into_iter().filter(|p| *p != self.pid() && !stopped.contains(p)).collect(),
                Err(e) => {
                    eprintln!("listing /proc to stop the engine's processes: {e}; killing what is stopped");
                    break;
                }
            };
            if below.is_empty() {
                break;
            }
            for pid in below {
                signal(kill(Pid::from_raw(pid), Signal::SIGSTOP), "stopping a process of the engine's");
                stopped.insert(pid);
            }
        }
        signal(killpg(group, Signal::SIGKILL), "killing the engine's group");
        for pid in stopped {
            signal(kill(Pid::from_raw(pid), Signal::SIGKILL), "killing a process of the engine's");
        }
        if let Err(e) = self.process.wait() {
            eprintln!("waiting for the killed engine: {e}");
        }
        self.killed = true;
    }

    /// What it has written to its standard output so far, without waiting for it to end. Panics when the file it
    /// writes to cannot be read.
    pub fn stdout(&self) -> String {
        read_lossy(&self.out_path)
    }

    /// What it has written to its standard error so far, as stdout reads it.
    pub fn stderr(&self) -> String {
        read_lossy(&err_path(&self.out_path))
    }
}

/// Report a signal that was not sent, unless its process had ended already (ESRCH), which is what a kill is for.
fn signal(sent: nix::Result<()>, what: &str) {
    match sent {
        Ok(()) | Err(nix::errno::Errno::ESRCH) => {}
        Err(e) => eprintln!("{what}: {e}"),
    }
}

fn err_path(out: &Path) -> PathBuf {
    let mut name = out.as_os_str().to_owned();
    name.push(".err");
    PathBuf::from(name)
}

fn read_lossy(path: &Path) -> String {
    let bytes = fs::read(path).unwrap_or_else(|e| panic!("reading {}: {e}", path.display()));
    String::from_utf8_lossy(&bytes).into_owned()
}

/// A temporary world a scenario runs the engine in, and what it did there.
///
/// The directory holds `project/` (the git checkout the engine runs in, on main, with origin at `origin.git/`),
/// `home/` and `config/` (the engine's HOME and XDG_CONFIG_HOME, so that its configuration directory is
/// `config/ai-agents-orchestrator/`), `bin/` (the fakes' shims), `fakes/` (their ledger) and `out/` (each invocation's
/// output). Dropping it kills any invocation still running and removes the directory, unless the scenario failed,
/// when the directory is kept for a look and its path printed.
pub struct World {
    /// The world's directory, removed when the world is dropped, or kept when the scenario failed. Fields drop after
    /// World's own drop has killed the invocations, so nothing of the engine's still runs in it then.
    scratch: Scratch,
    /// The git checkout the engine runs in, `root/project`.
    pub project: PathBuf,
    /// The bare repository that is the project's origin, `root/origin.git`.
    pub origin: PathBuf,
    /// The engine every invocation runs.
    engine: PathBuf,
    /// What the record's normalization replaces: the world's own paths, and the pids of its invocations.
    pub normalizer: Normalizer,
    /// Every invocation started, in order: invocation n is the n-th, from 1.
    pub invocations: Vec<Invocation>,
    /// What the scenario observed itself, by key, recorded under notes.
    notes: Map<Json>,
    /// The scenario's script, as the fakes read it from script.json.
    script: Map<Json>,
    /// The fakes' state and the commands they got, which the world reads and changes too.
    pub ledger: Ledger,
}

/// The fake programs a world puts on PATH, by the name they stand in for.
fn fakes() -> [(&'static str, &'static str); 3] {
    [
        ("herdr", env!("CARGO_BIN_EXE_fake-herdr")),
        ("gh", env!("CARGO_BIN_EXE_fake-gh")),
        ("ssh", env!("CARGO_BIN_EXE_fake-ssh")),
    ]
}

impl World {
    /// A world in a new directory named for the scenario, running engine. Panics, failing the scenario, where gitleaks
    /// is not on PATH: the engine runs the real one, and a scenario without it would record its absence.
    pub fn new(scenario: &str, engine: &Path) -> World {
        if find_on_path("gitleaks").is_none() {
            panic!("gitleaks is not on PATH; the e2e scenarios need it");
        }
        let scratch = Scratch::kept_on_failure(scenario);
        let root = scratch.path().to_path_buf();
        let mut normalizer = Normalizer::new();
        normalizer.literal(&root.to_string_lossy(), "<world>");
        normalizer.literal(&checkout().to_string_lossy(), "<checkout>");
        let project = root.join("project");
        let origin = root.join("origin.git");
        let script = Map::from_iter([
            ("project", Json::from(project.to_string_lossy().as_ref())),
            ("origin", Json::from(origin.to_string_lossy().as_ref())),
            ("agents", object([])),
            ("machines", Json::Array(Vec::new())),
            ("gh", object([])),
        ]);
        for d in ["home", "config/ai-agents-orchestrator", "bin", "fakes", "out", "tmp"] {
            let path = root.join(d);
            fs::create_dir_all(&path).unwrap_or_else(|e| panic!("making {}: {e}", path.display()));
        }
        let world = World {
            ledger: Ledger::new(&root),
            scratch,
            project,
            origin,
            engine: engine.to_path_buf(),
            normalizer,
            invocations: Vec::new(),
            notes: Map::new(),
            script,
        };
        world.write_script();
        world.make_bin();
        world.make_repositories();
        world
    }

    /// The world's directory, its symbolic links resolved.
    pub fn root(&self) -> &Path {
        self.scratch.path()
    }

    // -- Scripting the world ------------------------------------------------

    /// Script the role's agent: each action answers one prompt, in order, after those scripted before (see
    /// octoflow_e2e::fakes for what an action holds). A prompt beyond the last makes the agent exit.
    pub fn agent<A: Into<Json>>(&mut self, role: &str, actions: impl IntoIterator<Item = A>) {
        array_at(object_at(&mut self.script, "agents"), role).extend(actions.into_iter().map(Into::into));
        self.write_script();
    }

    /// Save a herdr machine of that label, reached over the fake ssh, which runs every command here.
    pub fn machine(&mut self, label: &str) {
        let machine = object([
            ("label", Json::from(label)),
            ("id", Json::Str(format!("id-{label}"))),
            ("enabled", Json::Bool(true)),
            ("target", Json::Str(format!("parity@{label}"))),
        ]);
        array_at(&mut self.script, "machines").push(machine);
        self.write_script();
    }

    /// Change what gh says of pull request n, the n-th the engine opened: state (OPEN, CLOSED or MERGED), closedAt,
    /// mergeable, isDraft.
    pub fn set_pr(&self, n: usize, fields: &[(&str, Json)]) {
        self.ledger.change(|state| {
            let Some(Json::Object(pr)) = object_at(state, "prs").get_mut(&n.to_string()) else {
                panic!("gh has opened no pull request {n}")
            };
            for (k, v) in fields {
                pr.insert(*k, v.clone());
            }
        });
    }

    /// Pull request n as gh holds it: base, head, title, body, isDraft, state, closedAt, mergeable.
    pub fn pr(&self, n: usize) -> Json {
        self.ledger.change(|state| object_at(state, "prs").get(&n.to_string()).cloned())
            .unwrap_or_else(|| panic!("gh has opened no pull request {n}"))
    }

    /// Let what waits on hold go on: an agent's action is done at herdr's next command and gh's pr create goes on.
    pub fn release(&self, hold: &str) {
        self.ledger.change(|state| array_at(state, "released").push(Json::from(hold)));
    }

    /// Wait until the engine waits on hold, at a point where it does nothing but poll until hold is released, so that
    /// what it did before is the same in every run: it polled the agent after prompting it, or started gh's pr create.
    pub fn wait_reached(&self, hold: &str) {
        self.wait_for(|| self.ledger.change(|state| octoflow_e2e::holds(state, "reached", hold)), &format!("hold {hold} to be reached"));
    }

    /// Poll condition until it is true; fails the scenario, naming what, after WAIT_TIMEOUT.
    pub fn wait_for(&self, mut condition: impl FnMut() -> bool, what: &str) {
        let deadline = Instant::now() + WAIT_TIMEOUT;
        while !condition() {
            assert!(Instant::now() <= deadline, "waited {WAIT_TIMEOUT:?} for {what}");
            sleep(WAIT_POLL);
        }
    }

    /// Write text to path, relative to the world's directory, making its directories; returns its full path.
    pub fn write(&self, path: &str, text: &str) -> String {
        let full = self.root().join(path);
        if let Some(dir) = full.parent() {
            fs::create_dir_all(dir).unwrap_or_else(|e| panic!("making {}: {e}", dir.display()));
        }
        fs::write(&full, text).unwrap_or_else(|e| panic!("writing {}: {e}", full.display()));
        full.to_string_lossy().into_owned()
    }

    /// Commit files, path to text, on origin's main, as another developer's push would, without touching the project's
    /// checkout.
    pub fn push_to_origin(&self, files: &[(&str, &str)], message: &str) {
        let clone = self.root().join("tmp").join("pusher");
        if !clone.exists() {
            self.git(self.root(), &["clone", "--quiet", &self.origin.to_string_lossy(), &clone.to_string_lossy()]);
        }
        self.git(&clone, &["fetch", "--quiet", "origin"]);
        self.git(&clone, &["checkout", "--quiet", "-B", "main", "origin/main"]);
        for (path, text) in files {
            let full = clone.join(path);
            if let Some(dir) = full.parent() {
                fs::create_dir_all(dir).unwrap_or_else(|e| panic!("making {}: {e}", dir.display()));
            }
            fs::write(&full, text).unwrap_or_else(|e| panic!("writing {}: {e}", full.display()));
        }
        self.git(&clone, &["add", "--all"]);
        self.git(&clone, &["commit", "--quiet", "-m", message]);
        self.git(&clone, &["push", "--quiet", "origin", "main"]);
    }

    /// git args in cwd, as the harness, with the world's identity and fixed dates; its output. Fails the scenario when
    /// git fails.
    pub fn git(&self, cwd: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .arg("-C")
            .arg(cwd)
            .args(args)
            .env_clear()
            .envs(self.env(None))
            .output()
            .unwrap_or_else(|e| panic!("running git {args:?}: {e}"));
        assert!(out.status.success(), "git {args:?} in {} failed: {}", cwd.display(), String::from_utf8_lossy(&out.stderr));
        String::from_utf8_lossy(&out.stdout).into_owned()
    }

    /// Add observed to the record under notes/key, normalized with the rest: what a scenario observed itself.
    pub fn note(&mut self, key: &str, observed: Json) {
        self.notes.insert(key, observed);
    }

    // -- Running the engine ---------------------------------------------------

    /// Start the engine with args in the project, in a process group of its own, and return its number at once.
    /// Without output_recorded, the record keeps only its exit status: for output that differs from host to host, such
    /// as doctor's versions of git, which the scenario notes what it needs of.
    pub fn start(&mut self, args: &[&str], output_recorded: bool) -> usize {
        let n = self.invocations.len() + 1;
        let out_path = self.root().join("out").join(n.to_string());
        let stdout = fs::File::create(&out_path).unwrap_or_else(|e| panic!("making {}: {e}", out_path.display()));
        let stderr_path = err_path(&out_path);
        let stderr = fs::File::create(&stderr_path).unwrap_or_else(|e| panic!("making {}: {e}", stderr_path.display()));
        let process = Command::new(&self.engine)
            .args(args)
            .current_dir(&self.project)
            .env_clear()
            .envs(self.env(Some(n)))
            .stdin(Stdio::null())
            .stdout(stdout)
            .stderr(stderr)
            .process_group(0)
            .spawn()
            .unwrap_or_else(|e| panic!("starting {}: {e}", self.engine.display()));
        let pid = process.id();
        self.normalizer.pid(i64::from(pid), n);
        self.ledger.change(|state| object_at(state, "engines").insert(n.to_string(), Json::Int(pid.into())));
        let args = args.iter().map(|a| (*a).to_string()).collect();
        self.invocations.push(Invocation { args, process, out_path, output_recorded, killed: false, exit: None });
        n
    }

    /// Run the engine with args, as start does with its output recorded, and wait for it to end.
    pub fn run(&mut self, args: &[&str]) -> usize {
        let n = self.start(args, true);
        self.invocation(n).wait();
        n
    }

    /// Invocation n, from 1.
    pub fn invocation(&mut self, n: usize) -> &mut Invocation {
        &mut self.invocations[n - 1]
    }

    /// The key of the n-th run the engine started in the project, by the order of their first herdr workspace; fails
    /// the scenario when there is none.
    pub fn run_key(&self, n: usize) -> String {
        let mut keys = Vec::new();
        for event in self.ledger.events() {
            let Some(command) = event.get("herdr").and_then(Json::as_array) else { continue };
            let words: Vec<&str> = command.iter().filter_map(Json::as_str).collect();
            let words = if words.first() == Some(&"--machine") { &words[2..] } else { &words[..] };
            if words.starts_with(&["workspace", "create"]) {
                let label = words.iter().position(|w| *w == "--label").map(|i| words[i + 1]).unwrap_or_default();
                keys.push(label.split_whitespace().next().unwrap_or_default().to_string());
            }
        }
        assert!(keys.len() >= n, "no run {n} in the project; workspaces were made for {keys:?}");
        keys.swap_remove(n - 1)
    }

    /// The saved state of the run with that key, as state.json holds it now.
    pub fn run_state(&self, key: &str) -> Json {
        let path = octoflow_e2e::run_dir(&self.project, key).join("state.json");
        Json::parse(&read_lossy(&path)).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
    }

    /// The environment of engine invocation n, or of the harness's own git for None: built afresh, so that nothing of
    /// the developer's own configuration reaches the engine. LLVM_PROFILE_FILE passes through, so that an engine built
    /// for coverage, as `cargo llvm-cov` runs this test, adds its profile to the test's rather than writing one into
    /// the project, where the record would find it.
    fn env(&self, n: Option<usize>) -> Vec<(String, String)> {
        let path = std::env::var("PATH").unwrap_or_default();
        let mut env = vec![
            ("PATH".to_string(), format!("{}:{path}", self.root().join("bin").display())),
            ("HOME".to_string(), self.root().join("home").display().to_string()),
            ("XDG_CONFIG_HOME".to_string(), self.root().join("config").display().to_string()),
            ("TMPDIR".to_string(), self.root().join("tmp").display().to_string()),
            ("LC_ALL".to_string(), "C.UTF-8".to_string()),
            ("TZ".to_string(), "UTC".to_string()),
            ("GIT_CONFIG_GLOBAL".to_string(), self.root().join("gitconfig").display().to_string()),
            ("GIT_CONFIG_NOSYSTEM".to_string(), "1".to_string()),
        ];
        let Some(n) = n else {
            env.push(("GIT_AUTHOR_DATE".to_string(), GIT_DATE.to_string()));
            env.push(("GIT_COMMITTER_DATE".to_string(), GIT_DATE.to_string()));
            return env;
        };
        env.push(("HERDR_ENV".to_string(), "1".to_string()));
        env.push((INVOCATION_VAR.to_string(), n.to_string()));
        if let Ok(profile) = std::env::var("LLVM_PROFILE_FILE") {
            env.push(("LLVM_PROFILE_FILE".to_string(), profile));
        }
        env
    }

    // -- Making the world -------------------------------------------------------

    fn write_script(&self) {
        write_atomically(&self.root().join("script.json"), &Json::Object(self.script.clone()).dumps());
    }

    /// Shims of the fakes in bin/, and the harnesses' CLIs.
    fn make_bin(&self) {
        let root = shlex_quote(&self.root().to_string_lossy());
        let mut shims: Vec<(String, String)> = fakes()
            .iter()
            .map(|(name, fake)| ((*name).to_string(), format!("exec {} {root} \"$@\"", shlex_quote(fake))))
            .collect();
        // The harnesses' CLIs, which doctor looks for on PATH and herdr would start: nothing runs them here.
        shims.extend(AGENT_KINDS.iter().map(|kind| ((*kind).to_string(), "exit 0".to_string())));
        for (program, body) in shims {
            let path = self.root().join("bin").join(program);
            fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap_or_else(|e| panic!("writing {}: {e}", path.display()));
            fs::set_permissions(&path, fs::Permissions::from_mode(0o755))
                .unwrap_or_else(|e| panic!("making {} executable: {e}", path.display()));
        }
    }

    fn make_repositories(&self) {
        let config = format!(
            "[user]\n\tname = {GIT_NAME}\n\temail = {GIT_EMAIL}\n[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n"
        );
        let path = self.root().join("gitconfig");
        fs::write(&path, config).unwrap_or_else(|e| panic!("writing {}: {e}", path.display()));
        self.git(self.root(), &["init", "--quiet", "--bare", "-b", "main", &self.origin.to_string_lossy()]);
        self.git(self.root(), &["init", "--quiet", "-b", "main", &self.project.to_string_lossy()]);
        for (path, text) in PROJECT_FILES {
            self.write(&format!("project/{path}"), text);
        }
        self.git(&self.project, &["add", "--all"]);
        self.git(&self.project, &["commit", "--quiet", "-m", "Start the parity project"]);
        self.git(&self.project, &["remote", "add", "origin", &self.origin.to_string_lossy()]);
        self.git(&self.project, &["push", "--quiet", "--set-upstream", "origin", "main"]);
    }

    // -- The record -------------------------------------------------------------

    /// What the scenario did, normalized: each invocation's arguments, exit status and output; per invocation, the
    /// commands herdr and gh got, in order, with state.json wherever it changed between them; the run directories'
    /// files; both repositories' refs, commits and working trees; and the scenario's notes. Output read by programs is
    /// kept whole, a human's only as its facts (octoflow_e2e::normalize::facts).
    pub fn record(&mut self) -> Json {
        let invocations: Vec<Json> = self.invocations.iter().map(invocation_record).collect();
        let events = self.events();
        let raw = object([
            ("invocations", Json::Array(invocations)),
            ("events", events),
            ("run_files", self.run_files()),
            (
                "repositories",
                object([("project", self.repository(&self.project)), ("origin", self.repository(&self.origin))]),
            ),
            ("notes", Json::Object(self.notes.clone())),
        ]);
        self.normalizer.learn(&raw);
        self.normalized(&raw)
    }

    fn normalized(&self, raw: &Json) -> Json {
        let normalizer = &self.normalizer;
        let Json::Object(mut out) = normalizer.json(raw, false, None) else { unreachable!("a record is an object") };
        if let Some(Json::Array(invocations)) = out.get_mut("invocations") {
            for inv in invocations {
                let Json::Object(inv) = inv else { continue };
                for stream in ["stdout", "stderr"] {
                    let key = format!("{stream}_facts");
                    if let Some(Json::Str(text)) = inv.remove(&key) {
                        inv.insert(key, strings(&facts(&normalizer.text(&text, true))));
                    }
                }
            }
        }
        // Texts that word the clock's times, as the turns tables do.
        let raw_events = raw.get("events").map(Json::as_object_or_empty).cloned().unwrap_or_default();
        let out_events = object_at(&mut out, "events");
        for (by, raw_list) in raw_events.iter() {
            let Some(Json::Array(list)) = out_events.get_mut(&normalizer.text(by, false)) else { continue };
            for (raw_event, event) in raw_list.as_array().into_iter().flatten().zip(list.iter_mut()) {
                if let (Some(stdin), Json::Object(event)) = (raw_event.get("stdin").and_then(Json::as_str), event) {
                    event.insert("stdin", Json::Str(normalizer.text(stdin, true)));
                }
            }
        }
        let raw_files = raw.get("run_files").map(Json::as_object_or_empty).cloned().unwrap_or_default();
        let out_files = object_at(&mut out, "run_files");
        for (path, text) in raw_files.iter() {
            if path.ends_with("summary.md") {
                out_files.insert(normalizer.text(path, false), Json::Str(normalizer.text(text.as_str().unwrap_or_default(), true)));
            }
        }
        // What a scenario notes may word the clock's times too.
        out.insert("notes", normalizer.json(&Json::Object(self.notes.clone()), true, None));
        Json::Object(out)
    }

    /// The ledger's events by invocation, each state.json parsed, and polls that repeated collapsed into one.
    fn events(&mut self) -> Json {
        let mut by: Map<Json> = Map::new();
        for event in self.ledger.events() {
            let Json::Object(mut event) = event else { continue };
            let who = event.remove("by").and_then(|b| b.as_str().map(str::to_string)).unwrap_or_default();
            if let Some(logged) = event.get("state").cloned() {
                let saved = Json::parse(str_at(&logged, "text")).unwrap_or_else(|e| panic!("a logged state: {e}"));
                // The fakes log a state only while its owner, if any, is a process of the invocation logging it.
                let owner_pid = saved.get("owner").and_then(|o| o.get("pid")).and_then(Json::as_int);
                if let (Some(pid), Ok(n)) = (owner_pid, who.parse::<usize>()) {
                    self.normalizer.pid(i64::try_from(pid).expect("a pid fits an i64"), n);
                }
                event.insert("state", object([("path", octoflow_e2e::at(&logged, "path").clone()), ("saved", saved)]));
            }
            array_at(&mut by, &who).push(Json::Object(event));
        }
        Json::Object(by.into_iter().map(|(who, events)| (who, Json::Array(collapsed(events.as_array().cloned().unwrap_or_default())))).collect())
    }

    /// Every file under the project's .orchestrator, but the worktrees, by its path there: each directory's files in
    /// name order, then its directories', as os.walk lists them.
    fn run_files(&self) -> Json {
        let top = self.project.join(".orchestrator");
        let mut files = Map::new();
        walk(&top, &top, &mut files);
        Json::Object(files)
    }

    /// The repository's refs, every commit they reach, from the refs in name order and each commit's parents in order,
    /// and for a repository with a working tree, its worktrees' status and changed files.
    fn repository(&self, repo: &Path) -> Json {
        let lines = self.git(repo, &["for-each-ref", "--format=%(refname) %(objectname)"]);
        let refs: Map<Json> = lines
            .lines()
            .filter_map(|line| line.split_once(' '))
            .map(|(name, sha)| (name, Json::from(sha)))
            .collect();
        let symbolic = Command::new("git")
            .args(["-C", &repo.to_string_lossy(), "symbolic-ref", "--quiet", "HEAD"])
            .env_clear()
            .envs(self.env(None))
            .output()
            .unwrap_or_else(|e| panic!("running git symbolic-ref: {e}"));
        let head = if symbolic.status.success() {
            String::from_utf8_lossy(&symbolic.stdout).trim().to_string()
        } else {
            "detached".to_string()
        };
        let mut sorted: Vec<(&str, &Json)> = refs.iter().collect();
        sorted.sort_by_key(|(name, _)| *name);
        let mut todo: Vec<String> = sorted.iter().filter_map(|(_, sha)| sha.as_str().map(str::to_string)).collect();
        let mut commits: Map<Json> = Map::new();
        while !todo.is_empty() {
            let sha = todo.remove(0);
            if commits.contains_key(&sha) || self.git(repo, &["cat-file", "-t", &sha]).trim() != "commit" {
                continue;
            }
            let commit = self.commit(repo, &sha);
            let parents: Vec<String> =
                commit.get("parents").and_then(Json::as_array).into_iter().flatten().filter_map(|p| p.as_str().map(str::to_string)).collect();
            commits.insert(sha, commit);
            todo.splice(0..0, parents);
        }
        let mut out = Map::from_iter([
            ("head", Json::Str(head)),
            ("refs", Json::Object(refs)),
            ("commits", Json::Array(commits.into_iter().map(|(_, c)| c).collect())),
        ]);
        if !repo.to_string_lossy().ends_with(".git") {
            out.insert("worktrees", self.worktrees(repo));
        }
        Json::Object(out)
    }

    fn commit(&self, repo: &Path, sha: &str) -> Json {
        let parents: Vec<String> = self.git(repo, &["show", "--no-patch", "--format=%P", sha]).split_whitespace().map(str::to_string).collect();
        let message = self.git(repo, &["show", "--no-patch", "--format=%an <%ae>%n%B", sha]);
        let (author, body) = message.split_once('\n').unwrap_or((&message, ""));
        let changes: Vec<String> = self
            .git(repo, &["diff-tree", "--root", "--no-commit-id", "-r", "--name-status", "-m", "--first-parent", sha])
            .lines()
            .map(str::to_string)
            .collect();
        let mut files = Map::new();
        for line in &changes {
            let (change, path) = line.split_once('\t').unwrap_or_else(|| panic!("a name-status line: {line}"));
            if change != "D" {
                files.insert(path, Json::Str(self.git(repo, &["show", &format!("{sha}:{path}")])));
            }
        }
        object([
            ("sha", Json::from(sha)),
            ("parents", strings(&parents)),
            ("author", Json::from(author)),
            ("message", Json::from(body)),
            ("changes", strings(&changes)),
            ("files", Json::Object(files)),
        ])
    }

    /// Each worktree of repo: its path, branch, status and the text of each file the status names.
    fn worktrees(&self, repo: &Path) -> Json {
        let mut out = Vec::new();
        let mut tree: Map<String> = Map::new();
        let listing = self.git(repo, &["worktree", "list", "--porcelain"]);
        for line in listing.lines().chain(std::iter::once("")) {
            if !line.is_empty() {
                let (name, text) = line.split_once(' ').unwrap_or((line, ""));
                tree.insert(name, text.to_string());
                continue;
            }
            if tree.is_empty() {
                continue;
            }
            let path = tree.get("worktree").cloned().unwrap_or_default();
            let porcelain: Vec<String> = self
                .git(Path::new(&path), &["status", "--porcelain", "--untracked-files=all"])
                .lines()
                .map(str::to_string)
                .collect();
            let mut changed = Map::new();
            for entry in &porcelain {
                let name = entry.get(3..).unwrap_or_default();
                let full = Path::new(&path).join(name);
                if full.is_file() && !name.starts_with(".orchestrator/") {
                    changed.insert(name, Json::Str(read_lossy(&full)));
                }
            }
            out.push(object([
                ("path", Json::Str(path)),
                ("branch", Json::Str(tree.get("branch").cloned().unwrap_or_else(|| "detached".to_string()))),
                ("status", strings(&porcelain)),
                ("changed", Json::Object(changed)),
            ]));
            tree = Map::new();
        }
        Json::Array(out)
    }
}

impl Drop for World {
    fn drop(&mut self) {
        for inv in &mut self.invocations {
            if inv.running() {
                inv.kill();
            }
        }
    }
}

fn invocation_record(inv: &Invocation) -> Json {
    let exit = if inv.killed { Json::from("killed") } else { inv.exit.map_or(Json::Null, |e| Json::from(i64::from(e))) };
    let mut record = Map::from_iter([("args", strings(&inv.args)), ("exit", exit)]);
    if !inv.output_recorded {
        return Json::Object(record);
    }
    if machine_read(&inv.args) {
        record.insert("stdout", Json::Str(inv.stdout()));
    } else {
        record.insert("stdout_facts", Json::Str(inv.stdout()));
    }
    record.insert("stderr_facts", Json::Str(inv.stderr()));
    Json::Object(record)
}

/// Whether the engine's standard output for args is read by a program, so that it must come out byte for byte: a
/// run's or resume's last line, list --json, metrics, serve's first line, and a workflow printed as a file.
fn machine_read(args: &[String]) -> bool {
    let words: Vec<&str> = args.iter().map(String::as_str).filter(|a| !a.starts_with('-')).collect();
    let command = words.first().copied().unwrap_or_default();
    ["run", "resume", "metrics", "serve"].contains(&command)
        || (command == "list" && args.iter().any(|a| a == "--json"))
        || (command == "workflows" && words.len() > 1)
}

/// events with each run of events that repeats the run just before it dropped, for runs of up to four: a poll the
/// engine repeats while it waits comes out once, however long the wait took.
fn collapsed(events: Vec<Json>) -> Vec<Json> {
    let mut out = events;
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

/// Add each file under dir to files, by its path relative to top, as os.walk lists them from top: the directory's
/// files in name order, then each subdirectory's in name order; top's worktrees directory, and directories reached by
/// a symbolic link, left out.
fn walk(top: &Path, dir: &Path, files: &mut Map<Json>) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    let mut names: Vec<_> = entries.flatten().map(|e| e.path()).collect();
    names.sort();
    let (dirs, plain): (Vec<_>, Vec<_>) = names.into_iter().partition(|p| p.is_dir());
    for path in plain {
        let relative = path.strip_prefix(top).expect("below top").to_string_lossy().into_owned();
        files.insert(relative, Json::Str(read_lossy(&path)));
    }
    for path in dirs {
        let linked = fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink());
        if linked || (dir == top && path.file_name().is_some_and(|n| n == "worktrees")) {
            continue;
        }
        walk(top, &path, files);
    }
}

/// The full path of program on PATH, if any.
fn find_on_path(program: &str) -> Option<PathBuf> {
    std::env::var_os("PATH")?.to_str()?.split(':').map(|dir| Path::new(dir).join(program)).find(|p| p.is_file())
}

/// Whether process pid has ended: gone from /proc, or a zombie its parent has not reaped yet.
#[cfg(test)]
fn ended(pid: i32) -> bool {
    fs::read_to_string(format!("/proc/{pid}/stat"))
        .map_or(true, |stat| stat.rsplit_once(')').is_some_and(|(_, rest)| rest.trim_start().starts_with('Z')))
}

/// A world dropped while its engine waits on an agent kills the engine and every process below it.
#[test]
fn a_dropped_world_leaves_nothing_of_its_engine_running() {
    let mut w = World::new("world_dropped", &crate::engine::binary());
    let spec = object([("write", object([("spec.md", Json::from("# A spec\n"))])), ("hold", Json::from("spec"))]);
    w.agent("spec", [spec]);
    let n = w.start(&["run", "--", "A task"], true);
    w.wait_reached("spec");
    // What is below the engine while it polls the held agent: often nothing but the engine itself.
    let engine = procs::descendants(w.invocation(n).pid()).expect("listing the engine's processes");
    drop(w);
    let running: Vec<i32> = engine.into_iter().filter(|&p| !ended(p)).collect();
    assert!(running.is_empty(), "still running after the drop: {running:?}");
}
