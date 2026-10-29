//! What the CLI's tests run a command on, as tests/test_orchestrator.py runs main with Host and Herdr patched: a
//! workflow Fixture's fakes, the program's standard output as text, and each quality job a Jenkins was made for.

use std::cell::RefCell;
use std::ffi::OsString;
use std::rc::Rc;

use super::{
    ended_status, herdrless_command, parse_args, run_command, run_workflow, Command, Connected, Ended, Local,
    RunWorkflow, Stop,
};
use crate::ci::fake::jenkins_on;
use crate::env::ConfigDir;
use crate::error::OrchestratorError;
use crate::herdr::fake::Turn;
use crate::herdr::HerdrError;
use crate::host::fake::FakeHost;
use crate::host::ProcessHost;
use crate::py::json::Json;
use crate::state::RunState;
use crate::workflow::fixture::{new_state, Fixture};

/// A command line's world: the fakes of `f`, this machine named "here", and what the commands print.
pub(super) struct Cli {
    pub f: Fixture,
    /// Everything printed on standard output, in order.
    pub out: RefCell<String>,
    /// Everything a command printed on standard error itself, in order: not main's error or interrupted note.
    pub err: RefCell<String>,
    /// The quality job of each Jenkins a command made, in order.
    pub jobs: RefCell<Vec<String>>,
    /// The configuration directory: /nowhere unless a test gives one.
    pub config: ConfigDir,
}

impl Cli {
    /// A world on a default FakeHost, for the default run's state.
    pub fn new() -> Cli {
        Cli::on(Rc::new(FakeHost::default()))
    }

    /// A world on host.
    pub fn on(host: Rc<FakeHost>) -> Cli {
        Cli::of(Fixture::on(host, new_state(), vec![]))
    }

    /// A world on f's fakes.
    pub fn of(f: Fixture) -> Cli {
        Cli {
            f,
            out: RefCell::new(String::new()),
            err: RefCell::new(String::new()),
            jobs: RefCell::new(vec![]),
            config: ConfigDir::at("/nowhere"),
        }
    }

    /// Run f with the world: run_command's, on f's fakes, with a Jenkins on f.ci for each job, its runs driven by
    /// run_workflow.
    pub fn with<T>(&self, f: impl FnOnce(&Connected) -> T) -> T {
        self.with_workflow(&run_workflow, f)
    }

    /// with, its runs driven by workflow, as a Python test patches Workflow.run.
    pub fn with_workflow<T>(&self, workflow: &RunWorkflow<'_>, f: impl FnOnce(&Connected) -> T) -> T {
        let jenkins = |job: &str| {
            self.jobs.borrow_mut().push(job.to_string());
            Ok(jenkins_on(&self.f.ci))
        };
        let print = |text: &str| self.out.borrow_mut().push_str(text);
        let stderr = |text: &str| self.err.borrow_mut().push_str(text);
        let mut with = self.f.collaborators();
        with.jenkins = None;
        let world = Connected {
            with,
            jenkins: &jenkins,
            config: &self.config,
            local_host: "here",
            stdout: &print,
            stderr: &stderr,
            workflow,
        };
        f(&world)
    }

    /// How main's command of argv ended, connected to the fakes, or run without herdr as main runs workflows, on
    /// this machine in /nowhere.
    pub fn ended(&self, argv: &[&str]) -> Result<Ended, Stop> {
        self.ended_here(argv, &ProcessHost::local(&self.f.interrupt), "/nowhere")
    }

    /// ended, a command that needs no herdr pane run on host in the current directory cwd.
    pub fn ended_here(&self, argv: &[&str], host: &ProcessHost, cwd: &str) -> Result<Ended, Stop> {
        let command = self.command(argv);
        let jenkins = |job: &str| {
            self.jobs.borrow_mut().push(job.to_string());
            Ok(jenkins_on(&self.f.ci))
        };
        let print = |text: &str| self.out.borrow_mut().push_str(text);
        let stderr = |text: &str| self.err.borrow_mut().push_str(text);
        let cwd = || Ok(cwd.to_string());
        let herdr = |_: Option<&str>| -> crate::herdr::ProcessHerdr { panic!("{argv:?} asked for herdr here") };
        let local = Local {
            host,
            herdr: &herdr,
            in_herdr_pane: true,
            jenkins: &jenkins,
            cwd: &cwd,
            config: &self.config,
            stdout: &print,
            stderr: &stderr,
            interrupt: &self.f.interrupt,
        };
        if let Some(ended) = herdrless_command(&command, &local) {
            return ended;
        }
        self.with(|world| run_command(&command, world))
    }

    /// main's exit status for argv, its last line printed as the command's output, its error or interrupted note
    /// on stderr.
    pub fn main(&self, argv: &[&str]) -> i32 {
        let ended = self.ended(argv);
        self.status(ended, argv)
    }

    /// main's exit status for argv's command that ended so, its last line printed as the command's output.
    pub fn status(&self, ended: Result<Ended, Stop>, argv: &[&str]) -> i32 {
        ended_status(ended, &self.command(argv), &|text: &str| self.out.borrow_mut().push_str(text))
    }

    /// The error main prints for argv, without its "error: ", or a panic if it ended otherwise.
    pub fn error(&self, argv: &[&str]) -> String {
        match self.ended(argv) {
            Err(Stop::Failed(e)) => e.0,
            Err(Stop::Interrupted(_)) => panic!("{argv:?} was interrupted"),
            Ok(_) => panic!("{argv:?} did not fail"),
        }
    }

    /// argv parsed, with the configuration directory.
    pub fn command(&self, argv: &[&str]) -> Command {
        parse_args(argv.iter().map(OsString::from), &self.config).expect("a command line parse_args takes")
    }

    /// What was printed on standard output so far, and forget it.
    pub fn take_out(&self) -> String {
        std::mem::take(&mut *self.out.borrow_mut())
    }

    /// Its lines.
    pub fn lines(&self) -> Vec<String> {
        self.take_out().lines().map(String::from).collect()
    }

    /// Put a run's saved state in /proj's runs, saved age seconds ago.
    pub fn save(&self, state: &Json, age: u64) {
        let run_id = state.get("run_id").map(Json::python_str).unwrap_or_default();
        let mut host = self.f.host.state();
        host.files.insert(format!("/proj/{}/{run_id}/state.json", crate::constants::RUNS_DIR), state.dumps());
        host.ages.insert(run_id, age);
    }

    /// A new run of argv (after `run`) in /proj, stopped at its first prompt as Python's tests stop one at
    /// Workflow.__init__ or run: how main's command ended, and the state.json of the run it started, None when it
    /// saved none.
    pub fn started(&self, argv: &[&str]) -> (Result<Ended, Stop>, Option<Json>) {
        let states = |host: &FakeHost| -> Vec<String> {
            host.state().files.keys().filter(|p| p.ends_with("/state.json")).map(String::from).collect()
        };
        let before = states(&self.f.host);
        self.f.herdr.state().prompt_with = Some(Box::new(|name: &str, _: &str| {
            Err(HerdrError::Failed(OrchestratorError(format!("the test stops {name} at its first prompt"))))
        }));
        let argv: Vec<&str> = ["run", "--cwd", "/proj"].iter().chain(argv).copied().collect();
        let ended = self.ended(&argv);
        self.f.herdr.state().prompt_with = None;
        let new = states(&self.f.host).into_iter().find(|p| !before.contains(p));
        let saved = new.map(|p| Json::parse(&self.f.host.state().files.get(&p).cloned().unwrap_or_default()));
        (ended, saved.map(|s| s.expect("state.json is JSON")))
    }

    /// A new run of argv (after `run`) in /proj, its roles played by script against the run it started: how main's
    /// command ended (playing).
    pub fn played(&self, argv: &[&str], script: Vec<(&str, Vec<Turn>)>) -> Result<Ended, Stop> {
        let argv: Vec<&str> = ["run", "--cwd", "/proj"].iter().chain(argv).copied().collect();
        self.playing(script, || self.ended(&argv))
    }

    /// What f gives, the roles of each run it starts in /proj played by script, each role's turns in order whichever
    /// run its agent is in. The fake herdr's turns are written for the fixture's run, so each prompt first points the
    /// fake at the run whose agent is prompted, by the key ending the agent's name.
    pub fn playing<T>(&self, script: Vec<(&str, Vec<Turn>)>, f: impl FnOnce() -> T) -> T {
        for (role, turns) in script {
            self.f.play(role, turns);
        }
        let (host, herdr) = (Rc::clone(&self.f.host), Rc::downgrade(&self.f.herdr));
        self.f.herdr.state().prompt_with = Some(Box::new(move |name: &str, _: &str| {
            let key = name.rsplit('-').next().unwrap_or(name);
            let text = host.state().files.iter().find_map(|(path, text)| {
                let run = path.strip_suffix("/state.json")?.rsplit('/').next()?;
                run.ends_with(&format!("-{key}")).then(|| text.clone())
            });
            if let (Some(text), Some(herdr)) = (text, herdr.upgrade()) {
                herdr.state().run = RunState::from_text(&text).expect("the run's state.json reads");
            }
            Ok(())
        }));
        let played = f();
        self.f.herdr.state().prompt_with = None;
        played
    }

    /// The saved state of run_id in /proj, as JSON.
    pub fn saved(&self, run_id: &str) -> Json {
        let path = format!("/proj/{}/{run_id}/state.json", crate::constants::RUNS_DIR);
        let text = self.f.host.state().files.get(&path).cloned().unwrap_or_else(|| panic!("no {path}"));
        Json::parse(&text).expect("state.json is JSON")
    }
}
