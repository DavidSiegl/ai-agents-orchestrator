//! tests/test_orchestrator.py's TestCheckSetup and TestDoctorCommand, ported case for case, on FakeTools: the
//! commands of a machine as ProcessHost and ProcessHerdr start them, played as Python's FakeTools plays them behind
//! Host(run=...) and Herdr(run=...). Not ported: test_without_tkinter and the tkinter check of the others, since the
//! Rust engine has no GUI and doctor checks no tkinter.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::ffi::OsString;
use std::sync::{Arc, Mutex};

use super::*;
use crate::cli::{ended_status, herdrless_command, parse_args, Local};
use crate::env::ConfigDir;
use crate::host::ProcessError;
use crate::interrupt::Interrupt;

/// What `herdr integration status` lists with every harness's integration current.
fn integrations_current() -> String {
    let current: String =
        AGENT_KINDS.iter().map(|k| format!("{k}: current (v3) (/home/u/.{k}/herdr-agent-state.sh)\n")).collect();
    format!("{current}letta (experimental): not installed (/l)\n")
}

/// How a command FakeTools plays ends.
#[derive(Clone, Debug)]
enum Answer {
    /// It exits with the status, having printed stdout and stderr.
    Ran(i32, String, String),
    /// It runs out of time.
    TimesOut,
    /// Its program cannot be started, as Python's FileNotFoundError.
    NotFound,
}

fn completed(stdout: &str) -> Answer {
    Answer::Ran(0, stdout.to_string(), String::new())
}

fn failing(stderr: &str, status: i32) -> Answer {
    Answer::Ran(status, String::new(), stderr.to_string())
}

/// A test's own answers, given a command's SSH target (None here) and argv; None leaves the command to the rest.
type AnswerWith = Box<dyn Fn(Option<&str>, &[String]) -> Option<Answer> + Send>;

/// What FakeTools holds and records.
struct Tools {
    /// Each tool on PATH, by name, with its path.
    paths: BTreeMap<String, String>,
    /// What each command answers, by its argv.
    answers: HashMap<Vec<String>, Answer>,
    /// The SSH targets that answer.
    targets: Vec<String>,
    /// The directories Host::resolve_dir finds.
    dirs: Vec<String>,
    /// (SSH target or None, argv) of each command, in order.
    calls: Vec<(Option<String>, Vec<String>)>,
    /// The environment each command was started in, in order.
    envs: Vec<Vec<(OsString, OsString)>>,
    /// Answers in place of answers when it gives one, given the target and argv: a test's own.
    answer_with: Option<AnswerWith>,
}

/// A machine's commands with every tool doctor checks present; a test changes paths or answers to take one away.
/// Over SSH it answers for targets, and records each command's target in calls.
#[derive(Clone)]
struct FakeTools(Arc<Mutex<Tools>>);

impl FakeTools {
    fn new() -> FakeTools {
        FakeTools::with_targets(&["me@box"])
    }

    fn with_targets(targets: &[&str]) -> FakeTools {
        let names = AGENT_KINDS.iter().copied().chain(["herdr", "git", "gh"]);
        let paths = names.map(|n| (n.to_string(), format!("/usr/bin/{n}"))).collect();
        let argv = |words: &str| words.split(' ').map(String::from).collect::<Vec<_>>();
        let machines = r#"[{"label": "box", "id": "m1", "enabled": true, "target": "me@box"}, {"label": "off", "id": "m2", "enabled": false, "target": "me@off"}]"#;
        let answers = [
            ("herdr --version", completed("herdr 0.9.1\n")),
            ("herdr integration status", completed(&integrations_current())),
            ("git --version", completed("git version 2.47.3\n")),
            ("gh auth status", completed("github.com\n  ✓ Logged in to github.com\n")),
            ("gitleaks git --help", completed("Usage: gitleaks git [flags]\n")),
            ("git -C /proj rev-parse --show-toplevel", completed("/proj\n")),
            ("git -C /proj remote get-url origin", completed("git@github.com:me/proj.git\n")),
            ("git -C /home/u/proj rev-parse --show-toplevel", completed("/home/u/proj\n")),
            ("git -C /home/u/proj remote get-url origin", completed("git@github.com:me/p.git\n")),
            ("herdr machine list --json", completed(machines)),
        ]
        .into_iter()
        .map(|(words, answer)| (argv(words), answer))
        .collect();
        FakeTools(Arc::new(Mutex::new(Tools {
            paths,
            answers,
            targets: targets.iter().map(|t| t.to_string()).collect(),
            dirs: vec!["/proj".into(), "/home/u/proj".into()],
            calls: vec![],
            envs: vec![],
            answer_with: None,
        })))
    }

    fn tools(&self) -> std::sync::MutexGuard<'_, Tools> {
        self.0.lock().unwrap()
    }

    /// Take a tool off PATH.
    fn remove(&self, name: &str) {
        self.tools().paths.remove(name);
    }

    /// Have the command of words, split at spaces, answer so.
    fn answer(&self, words: &str, answer: Answer) {
        self.tools().answers.insert(words.split(' ').map(String::from).collect(), answer);
    }

    /// Whether the command of words, split at spaces, ran on target.
    fn ran(&self, target: Option<&str>, words: &str) -> bool {
        let argv: Vec<String> = words.split(' ').map(String::from).collect();
        self.tools().calls.iter().any(|(t, a)| t.as_deref() == target && *a == argv)
    }

    /// Play one command, as ProcessHost's or ProcessHerdr's runner, started in env.
    fn call(&self, argv: &[String], env: &[(OsString, OsString)]) -> Result<(i32, Vec<u8>, Vec<u8>), ProcessError> {
        let mut t = self.tools();
        let ran = |status: i32, stdout: &str, stderr: &str| Ok((status, stdout.as_bytes().to_vec(), stderr.as_bytes().to_vec()));
        let (target, argv) = if argv[0] == "ssh" {
            let target = argv[argv.len() - 2].clone();
            if !t.targets.contains(&target) {
                return ran(255, "", &format!("ssh: connect to host {target}: Connection refused"));
            }
            (Some(target), shlex_split(&argv[argv.len() - 1]))
        } else {
            (None, argv.to_vec())
        };
        t.calls.push((target.clone(), argv.clone()));
        t.envs.push(env.to_vec());
        if let Some(answer) = t.answer_with.as_ref().and_then(|with| with(target.as_deref(), &argv)) {
            return played(&answer);
        }
        if argv == ["true"] {
            return ran(0, "", "");
        }
        if argv[0] == "sh" && argv[2].contains("command -v") {
            return match t.paths.get(argv.last().unwrap()) {
                Some(path) => ran(0, &format!("{path}\n"), ""),
                None => ran(1, "", ""),
            };
        }
        if argv[0] == "sh" && argv[2].ends_with("&& pwd") {
            // Host::resolve_dir, home being /home/u.
            let home = if argv[2].contains("$HOME") { "/home/u" } else { "" };
            let path = format!("{home}{}", argv.last().unwrap());
            return match t.dirs.contains(&path) {
                true => ran(0, &format!("{path}\n"), ""),
                false => ran(2, "", "cd: no such dir"),
            };
        }
        if !t.paths.contains_key(&argv[0]) && argv[0] != "gitleaks" {
            // The remote shell answers for a command it cannot find; locally it cannot be started at all.
            if target.is_some() {
                return ran(127, "", &format!("sh: 1: {}: not found\n", argv[0]));
            }
            return Err(ProcessError::Start(std::io::ErrorKind::NotFound.into()));
        }
        let answer = t.answers.get(&argv).cloned().unwrap_or_else(|| panic!("FakeTools got an unscripted {argv:?}"));
        played(&answer)
    }

    /// A Host on this machine whose commands these tools play, in an environment of its own with set added.
    fn host(&self, set: &[(&str, &str)]) -> ProcessHost {
        let tools = self.clone();
        let vars = set.iter().map(|(k, v)| (OsString::from(k), OsString::from(v))).chain([("PATH".into(), "/usr/bin".into())]);
        ProcessHost::with_runner(vars.collect::<Vec<_>>().into_iter(), move |argv, _, _, env| tools.call(argv, env))
    }

    /// The herdr here, forwarded to machine, whose commands these tools play.
    fn herdr(&self, machine: Option<&str>) -> ProcessHerdr {
        let tools = self.clone();
        ProcessHerdr::with_runner(machine, move |argv, _| tools.call(argv, &[]))
    }
}

/// How answer plays out for the runner.
fn played(answer: &Answer) -> Result<(i32, Vec<u8>, Vec<u8>), ProcessError> {
    match answer {
        Answer::Ran(status, stdout, stderr) => Ok((*status, stdout.as_bytes().to_vec(), stderr.as_bytes().to_vec())),
        Answer::TimesOut => Err(ProcessError::Timeout),
        Answer::NotFound => Err(ProcessError::Start(std::io::ErrorKind::NotFound.into())),
    }
}

/// The words of a line of sh as shlex_join writes them: Python's shlex.split, for what the fake ssh is given.
fn shlex_split(line: &str) -> Vec<String> {
    let (mut words, mut word, mut in_word) = (vec![], String::new(), false);
    let mut chars = line.chars();
    while let Some(c) = chars.next() {
        match c {
            '\'' => {
                in_word = true;
                word.extend(chars.by_ref().take_while(|&c| c != '\''));
            }
            '"' => {
                in_word = true;
                word.extend(chars.by_ref().take_while(|&c| c != '"'));
            }
            ' ' if in_word => {
                words.push(std::mem::take(&mut word));
                in_word = false;
            }
            ' ' => {}
            c => {
                in_word = true;
                word.push(c);
            }
        }
    }
    if in_word {
        words.push(word);
    }
    words
}

/// The checks of check_setup on tools, the agents on machine, the project at cwd, from a herdr pane, by name.
fn setup(tools: &FakeTools, machine: Option<&str>, cwd: Option<&str>) -> BTreeMap<String, Check> {
    check_in_pane(tools, machine, cwd, true)
}

fn check_in_pane(tools: &FakeTools, machine: Option<&str>, cwd: Option<&str>, in_pane: bool) -> BTreeMap<String, Check> {
    let checks = check_setup(&tools.host(&[]), &tools.herdr(machine), cwd, in_pane);
    checks.into_iter().map(|c| (c.name.clone(), c)).collect()
}

/// The names of check_setup's checks on tools, in order.
fn names(tools: &FakeTools, cwd: Option<&str>) -> Vec<String> {
    check_setup(&tools.host(&[]), &tools.herdr(None), cwd, true).into_iter().map(|c| c.name).collect()
}

/// What report_checks prints for checks, and its exit status.
fn reported(checks: &BTreeMap<String, Check>, order: &[String]) -> (String, i32) {
    let checks: Vec<Check> = order.iter().filter_map(|n| checks.get(n).cloned()).collect();
    report_list(&checks)
}

fn report_list(checks: &[Check]) -> (String, i32) {
    let out = RefCell::new(String::new());
    let status = report_checks(checks, &|line| out.borrow_mut().push_str(&format!("{line}\n")));
    (out.into_inner(), status)
}

fn failed(checks: &BTreeMap<String, Check>, order: &[String]) -> Vec<String> {
    order.iter().filter(|n| checks.get(*n).is_some_and(|c| !c.ok)).cloned().collect()
}

fn check_of(name: &str, level: Level, ok: bool, detail: &str, fix: &str) -> Check {
    Check { name: name.into(), level, ok, detail: detail.into(), fix: fix.into() }
}

#[test]
fn all_present() {
    let tools = FakeTools::new();
    let order = names(&tools, Some("/proj"));
    let mut want: Vec<String> = vec!["herdr".into(), "reaching herdr".into()];
    for kind in AGENT_KINDS {
        want.extend([format!("{kind} CLI"), format!("{kind} integration")]);
    }
    want.extend(["git", "gh", "gitleaks", "git checkout", "origin"].map(String::from));
    assert_eq!(order, want);
    let checks = setup(&tools, None, Some("/proj"));
    assert_eq!(failed(&checks, &order), Vec::<String>::new());
    assert_eq!(checks["herdr"].detail, "herdr 0.9.1");
    assert_eq!(checks["claude integration"].detail, "current (v3)");
    assert_eq!(checks["origin"].detail, "git@github.com:me/proj.git");
    let (out, status) = reported(&checks, &order);
    assert_eq!(status, 0);
    assert!(out.lines().all(|l| l.starts_with("ok      ")), "{out}");
    assert!(out.contains("ok      herdr: herdr 0.9.1\n"), "{out}");
}

#[test]
fn levels() {
    let checks = setup(&FakeTools::new(), None, Some("/proj"));
    let levels: BTreeMap<&str, &str> =
        checks.iter().filter(|(_, c)| c.level != Level::Optional).map(|(n, c)| (n.as_str(), c.level.as_str())).collect();
    let want: BTreeMap<&str, &str> = [
        ("herdr", "required"),
        ("reaching herdr", "required"),
        ("claude CLI", "required"),
        ("claude integration", "required"),
        ("git", "required"),
        ("gh", "needed-for-pr"),
        ("gitleaks", "needed-for-pr"),
        ("git checkout", "needed-for-pr"),
        ("origin", "needed-for-pr"),
    ]
    .into_iter()
    .collect();
    assert_eq!(levels, want);
}

#[test]
fn without_herdr() {
    let tools = FakeTools::new();
    tools.remove("herdr");
    let order = names(&tools, None);
    let checks = setup(&tools, None, None);
    let install = "install herdr 0.9 or later, or update it with `herdr update`";
    assert_eq!(checks["herdr"], check_of("herdr", Level::Required, false, "herdr is not on PATH", install));
    assert_eq!(checks["claude integration"].detail, "skipped: needs herdr");
    assert_eq!(checks["codex integration"].detail, "skipped: needs herdr; needed only for a role that runs in codex");
    assert!(checks["git"].ok && checks["gh"].ok && checks["claude CLI"].ok);
    assert!(!tools.ran(None, "herdr integration status"));
    let (out, status) = reported(&checks, &order);
    assert_eq!(status, EXIT_ERROR);
    assert!(out.contains(&format!("missing herdr: herdr is not on PATH\n        fix: {install}\n")), "{out}");
    assert!(out.contains("missing claude integration: skipped: needs herdr\n        fix: fix herdr first\nok      codex CLI"), "{out}");
}

#[test]
fn old_herdr() {
    let tools = FakeTools::new();
    tools.answer("herdr --version", completed("herdr 0.8.4\n"));
    let herdr = &setup(&tools, None, None)["herdr"];
    assert_eq!((herdr.ok, herdr.detail.as_str()), (false, "herdr 0.8.4 is older than 0.9"));
    for (said, ok) in [("herdr 0.9\n", true), ("herdr 0.10.0\n", true), ("herdr 1.0.0\n", true), ("herdr dev\n", false)] {
        tools.answer("herdr --version", completed(said));
        assert_eq!(setup(&tools, None, None)["herdr"].ok, ok, "{said}");
    }
}

#[test]
fn integrations_not_installed() {
    let tools = FakeTools::new();
    let listing = integrations_current()
        .replace("claude: current (v3)", "claude: not installed")
        .replace("codex: current (v3)", "codex: not installed")
        .replace("gemini: current (v3)", "gemini: outdated (v1)");
    tools.answer("herdr integration status", completed(&listing));
    let order = names(&tools, Some("/proj"));
    let checks = setup(&tools, None, Some("/proj"));
    let claude = check_of("claude integration", Level::Required, false, "not installed", "herdr integration install claude");
    assert_eq!(checks["claude integration"], claude);
    let codex = check_of(
        "codex integration",
        Level::Optional,
        false,
        "not installed; needed only for a role that runs in codex",
        "herdr integration install codex",
    );
    assert_eq!(checks["codex integration"], codex);
    assert_eq!(checks["gemini integration"].detail, "outdated (v1); needed only for a role that runs in gemini");
    assert_eq!(failed(&checks, &order), ["claude integration", "codex integration", "gemini integration"]);
    let asked = tools.tools().calls.iter().filter(|(t, a)| t.is_none() && *a == ["herdr", "integration", "status"]).count();
    // Once for each check_setup: names' and check's.
    assert_eq!(asked, 2);
    let (out, status) = reported(&checks, &order);
    assert_eq!(status, EXIT_ERROR);
    assert!(out.contains("missing claude integration: not installed\n        fix: herdr integration install claude\n"), "{out}");
    assert!(out.contains("warn    codex integration: not installed;"), "{out}");
}

#[test]
fn optional_failures_alone_pass() {
    let tools = FakeTools::new();
    tools.remove("codex");
    tools.answer("herdr integration status", completed(&integrations_current().replace("pi: current", "pi: x")));
    let order = names(&tools, None);
    let checks = setup(&tools, None, None);
    assert_eq!(checks["codex CLI"].fix, "install the codex CLI where the agents run");
    assert_eq!(reported(&checks, &order).1, 0);
}

#[test]
fn integration_unlisted_or_unasked() {
    let tools = FakeTools::new();
    tools.answer("herdr integration status", completed("claude: current (v3) (/c)\n"));
    assert_eq!(
        setup(&tools, None, None)["pi integration"].detail,
        "herdr integration status does not list pi; needed only for a role that runs in pi"
    );
    tools.answer("herdr integration status", failing("unknown command", 2));
    let checks = setup(&tools, None, None);
    assert!(checks["claude integration"].detail.contains("herdr integration status failed: unknown command"));
    for kind in AGENT_KINDS {
        assert_eq!(
            checks[&format!("{kind} integration")].fix,
            "update herdr with `herdr update`, then check that `herdr integration status` runs"
        );
    }
}

#[test]
fn integration_states_are_read_from_the_listing() {
    let listing = "pi: current (v9) (/home/u/.pi/a.ts)\n\
                   codex: not installed (/home/u/.codex/a.sh)\n\
                   letta (experimental): not installed (/home/u/.letta/a.sh)\n\
                   gemini: outdated (v2, current is v3) (/g)\n\
                   \n\
                   noise\n";
    let pairs = |pairs: &[(&str, &str)]| pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect::<Vec<_>>();
    assert_eq!(
        integration_states(listing),
        pairs(&[
            ("pi", "current (v9)"),
            ("codex", "not installed"),
            ("letta", "not installed"),
            ("gemini", "outdated (v2, current is v3)")
        ])
    );
    // The pattern's corners, as Python's re reads them.
    let cases = [
        ("a:b: x", Some(("a:b", "x"))),
        ("k:  (p)", Some(("k", ""))),
        ("k: x (y (z))", Some(("k", "x (y (z))"))),
        ("k: x(p)", Some(("k", "x(p)"))),
        ("k:x", None),
        ("k: a (b) (c)", Some(("k", "a (b)"))),
    ];
    for (line, want) in cases {
        assert_eq!(integration_line(line), want.map(|(k, s)| (k.to_string(), s.to_string())), "{line}");
    }
}

#[test]
fn git() {
    let tools = FakeTools::new();
    for (said, ok) in
        [("git version 2.36.0\n", true), ("git version 2.39.3 (Apple Git-145)\n", true), ("git version 2.35.9\n", false)]
    {
        tools.answer("git --version", completed(said));
        assert_eq!(setup(&tools, None, None)["git"].ok, ok, "{said}");
    }
    let git = setup(&tools, None, None)["git"].clone();
    assert_eq!((git.detail.as_str(), git.fix.as_str()), ("git 2.35.9 is older than 2.36", "install git 2.36 or later"));
    tools.remove("git");
    let checks = setup(&tools, None, Some("/proj"));
    assert_eq!(checks["git"].detail, "git is not on PATH");
    assert_eq!(checks["git checkout"].detail, "skipped: needs git");
    assert_eq!(checks["origin"].detail, "skipped: needs git checkout");
}

#[test]
fn gh_logged_out() {
    let tools = FakeTools::new();
    let logged_out = "You are not logged into any GitHub hosts. To log in, run: gh auth login\n";
    tools.answer("gh auth status", failing(logged_out, 1));
    let order = names(&tools, None);
    let checks = setup(&tools, None, None);
    let want = check_of("gh", Level::NeededForPr, false, &format!("gh auth status fails: {}", logged_out.trim()), "gh auth login");
    assert_eq!(checks["gh"], want);
    assert!(checks["gitleaks"].ok);
    assert_eq!(reported(&checks, &order).1, EXIT_ERROR);
    let said = "github.com\n  X Failed to log in to github.com using token (GH_TOKEN)\n  - The token in GH_TOKEN is invalid.\n";
    tools.answer("gh auth status", Answer::Ran(1, said.into(), String::new()));
    assert_eq!(
        setup(&tools, None, None)["gh"].detail,
        "gh auth status fails: github.com X Failed to log in to github.com using token (GH_TOKEN) - The token in \
         GH_TOKEN is invalid."
    );
    tools.remove("gh");
    let gh = setup(&tools, None, None)["gh"].clone();
    assert_eq!((gh.detail.as_str(), gh.fix.as_str()), ("gh is not on PATH", "install gh (https://cli.github.com)"));
}

#[test]
fn a_missing_git_or_gh_is_found_by_running_it_once() {
    for (machine, target) in [(None, None), (Some("box"), Some("me@box"))] {
        for (tool, argv) in [("git", &["git", "--version"][..]), ("gh", &["gh", "auth", "status"][..])] {
            let tools = FakeTools::new();
            tools.remove(tool);
            assert_eq!(setup(&tools, machine, None)[tool].detail, format!("{tool} is not on PATH"));
            let asked: Vec<Vec<String>> = tools
                .tools()
                .calls
                .iter()
                .filter(|(t, a)| t.as_deref() == target && (a.first().map(String::as_str) == Some(tool) || a.last().map(String::as_str) == Some(tool)))
                .map(|(_, a)| a.clone())
                .collect();
            assert_eq!(asked, [argv.iter().map(|a| a.to_string()).collect::<Vec<_>>()], "{machine:?} {tool}");
        }
    }
}

#[test]
fn gh_times_out() {
    let tools = FakeTools::new();
    tools.answer("gh auth status", Answer::TimesOut);
    let checks = setup(&tools, None, None);
    assert!(!checks["gh"].ok);
    assert!(checks["gh"].detail.contains("timed out"), "{}", checks["gh"].detail);
    assert!(checks["gitleaks"].ok);
}

#[test]
fn gitleaks() {
    let tools = FakeTools::new();
    tools.answer("gitleaks git --help", failing("unknown command \"git\"", 1));
    let order = names(&tools, None);
    let checks = setup(&tools, None, None);
    assert_eq!(checks["gitleaks"], check_of("gitleaks", Level::NeededForPr, false, "`gitleaks git` does not run", GITLEAKS_INSTALL));
    assert!(checks["gitleaks"].fix.contains("https://github.com/gitleaks/gitleaks#installing"));
    assert_eq!(reported(&checks, &order).1, EXIT_ERROR);
    tools.answer("gitleaks git --help", Answer::NotFound);
    let detail = setup(&tools, None, None)["gitleaks"].detail.clone();
    assert!(detail.starts_with("gitleaks cannot be started ("), "{detail}");
}

#[test]
fn not_a_checkout() {
    let tools = FakeTools::new();
    let stderr = "fatal: not a git repository (or any of the parent directories): .git\nmore\n";
    tools.answer("git -C /proj rev-parse --show-toplevel", failing(stderr, 128));
    let checks = setup(&tools, None, Some("/proj"));
    assert_eq!(
        checks["git checkout"].detail,
        "/proj is not in a git checkout: fatal: not a git repository (or any of the parent directories): .git"
    );
    let origin = check_of("origin", Level::NeededForPr, false, "skipped: needs git checkout", "fix git checkout first");
    assert_eq!(checks["origin"], origin);
    assert!(!tools.ran(None, "git -C /proj remote get-url origin"));
}

#[test]
fn no_origin() {
    let tools = FakeTools::new();
    tools.answer("git -C /proj remote get-url origin", failing("error: No such remote 'origin'", 2));
    let origin = setup(&tools, None, Some("/proj"))["origin"].clone();
    assert!(!origin.ok);
    assert_eq!(origin.fix, "git remote add origin <url>");
}

#[test]
fn cwd_resolved_as_a_run_resolves_it() {
    let tools = FakeTools::new();
    let checks = setup(&tools, Some("box"), Some("~/proj"));
    assert_eq!(
        (checks["git checkout"].detail.as_str(), checks["origin"].detail.as_str()),
        ("/home/u/proj", "git@github.com:me/p.git")
    );
    let resolve = ["sh", "-c", "cd -- \"$HOME$1\" && pwd", "_", "/proj"].map(String::from).to_vec();
    assert!(tools.tools().calls.contains(&(Some("me@box".into()), resolve)));
    let checkout = setup(&tools, None, Some("/gone"))["git checkout"].clone();
    assert_eq!((checkout.ok, checkout.detail.split(':').next()), (false, Some("/gone is not a directory")));
}

#[test]
fn without_cwd_the_project_is_not_checked() {
    assert!(!setup(&FakeTools::new(), None, None).contains_key("git checkout"));
}

#[test]
fn outside_a_herdr_pane() {
    let tools = FakeTools::new();
    let order = names(&tools, None);
    let checks = check_in_pane(&tools, None, None, false);
    let reach = check_of(
        "reaching herdr",
        Level::Required,
        false,
        "not inside a herdr pane",
        "run from a herdr pane, or pass --machine",
    );
    assert_eq!(checks["reaching herdr"], reach);
    assert_eq!(failed(&checks, &order), ["reaching herdr"]);
    let (out, status) = reported(&checks, &order);
    assert_eq!(status, EXIT_ERROR);
    assert!(out.contains("missing reaching herdr: not inside a herdr pane\n        fix: run from a herdr pane, or pass --machine\n"), "{out}");
}

#[test]
fn on_a_machine() {
    let tools = FakeTools::new();
    let checks = check_in_pane(&tools, Some("box"), Some("/proj"), false);
    assert!(checks.values().all(|c| c.ok), "{checks:?}");
    assert_eq!(checks["reaching herdr"].detail, "herdr machine box, over SSH to me@box");
    assert_eq!((checks["herdr"].detail.as_str(), checks["herdr on box"].detail.as_str()), ("herdr 0.9.1", "herdr 0.9.1"));
    // Only the herdr a run drives is checked here; everything else goes over SSH to the machine.
    let calls = tools.tools().calls.clone();
    let local: Vec<Vec<String>> = calls.iter().filter(|(t, _)| t.is_none()).map(|(_, a)| a.clone()).collect();
    let words = |w: &str| w.split(' ').map(String::from).collect::<Vec<_>>();
    assert_eq!(local, [words("herdr --version"), words("herdr machine list --json")]);
    let remote: Vec<Vec<String>> = calls.iter().filter(|(t, _)| t.as_deref() == Some("me@box")).map(|(_, a)| a.clone()).collect();
    // A tool whose version or login is checked is run, never first looked up; only the agent CLIs are.
    let mut looked_up: Vec<String> = remote
        .iter()
        .filter(|a| a.len() > 3 && a[..3] == ["sh", "-c", "command -v -- \"$1\""])
        .map(|a| a.last().unwrap().clone())
        .collect();
    looked_up.sort();
    let mut kinds: Vec<String> = AGENT_KINDS.iter().map(|k| k.to_string()).collect();
    kinds.sort();
    assert_eq!(looked_up, kinds);
    for argv in ["herdr --version", "herdr integration status", "git --version", "gh auth status", "gitleaks git --help", "git -C /proj rev-parse --show-toplevel"] {
        assert!(remote.contains(&words(argv)), "{argv}");
    }
    let codex = ["sh", "-c", "command -v -- \"$1\"", "_", "codex"].map(String::from).to_vec();
    assert!(remote.contains(&codex));
}

#[test]
fn machine_not_saved_disabled_or_unreachable() {
    let tools = FakeTools::with_targets(&[]);
    for (machine, detail) in [
        ("nowhere", "no saved herdr machine named nowhere"),
        ("off", "herdr machine off is disabled"),
        ("box", "true failed on me@box: ssh: connect to host me@box: Connection refused"),
    ] {
        let checks = setup(&tools, Some(machine), Some("/proj"));
        let reach = &checks["reaching herdr"];
        assert_eq!((reach.ok, reach.detail.as_str()), (false, detail));
        assert!(reach.fix.contains(machine));
        assert!(checks["herdr"].ok);
        assert_eq!(checks[&format!("herdr on {machine}")].detail, "skipped: needs reaching herdr");
        assert_eq!(checks["gh"].detail, "skipped: needs reaching herdr");
        assert_eq!(checks["origin"].detail, "skipped: needs git checkout");
        let order: Vec<String> = check_setup(&tools.host(&[]), &tools.herdr(Some(machine)), Some("/proj"), true)
            .into_iter()
            .map(|c| c.name)
            .collect();
        let (out, status) = reported(&checks, &order);
        assert_eq!(status, EXIT_ERROR);
        assert!(out.contains(&format!("missing reaching herdr: {detail}\n")), "{out}");
    }
}

#[test]
fn machine_without_local_herdr() {
    let tools = FakeTools::new();
    tools.remove("herdr");
    let checks = setup(&tools, Some("box"), None);
    let herdr = &checks["herdr"];
    let install = "install herdr 0.9 or later, or update it with `herdr update`";
    assert_eq!((herdr.detail.as_str(), herdr.fix.as_str()), ("herdr is not on PATH", install));
    let reach = &checks["reaching herdr"];
    assert_eq!((reach.detail.as_str(), reach.fix.as_str()), ("skipped: needs herdr", "fix herdr first"));
    assert!(!tools.ran(None, "herdr machine list --json"));
}

#[test]
fn machine_herdr_versions_checked_apart() {
    // The herdr here drives the run and the one on the machine hosts its agents; each must be recent enough.
    for (local, remote, old) in [("0.8.4", "0.9.1", "herdr"), ("0.9.1", "0.8.4", "herdr on box")] {
        let tools = FakeTools::new();
        let (local, remote) = (format!("herdr {local}\n"), format!("herdr {remote}\n"));
        tools.tools().answer_with = Some(Box::new(move |target, argv| {
            (argv == ["herdr", "--version"]).then(|| completed(if target.is_none() { &local } else { &remote }))
        }));
        let checks = setup(&tools, Some("box"), None);
        assert_eq!(checks[old].detail, "herdr 0.8.4 is older than 0.9");
        let oks: Vec<bool> = ["herdr", "reaching herdr", "herdr on box"].iter().map(|n| checks[*n].ok).collect();
        assert_eq!(oks, if old == "herdr" { [false, false, false] } else { [true, true, false] });
    }
}

#[test]
fn commands_get_the_child_env() {
    let tools = FakeTools::new();
    let host = tools.host(&[("JENKINS_TOKEN", "secret")]);
    check_setup(&host, &tools.herdr(Some("box")), Some("/proj"), true);
    let envs = tools.tools().envs.clone();
    // The herdr runner's commands are not given theirs here; ProcessHerdr's own tests hold it to child_env.
    assert!(envs.iter().any(|env| !env.is_empty()));
    for env in envs {
        assert!(!env.iter().any(|(k, _)| k == "JENKINS_TOKEN"), "{env:?}");
    }
}

// -- TestDoctorCommand -----------------------------------------------------------------------------------------------

#[test]
fn needs_no_herdr_pane() {
    let tools = FakeTools::new();
    let argv = ["doctor", "--machine", "box", "--cwd", "~/proj"];
    let config = ConfigDir::at("/nowhere");
    let command = parse_args(argv.iter().map(OsString::from), &config).unwrap();
    let out = RefCell::new(String::new());
    let host = tools.host(&[]);
    let machines = RefCell::new(vec![]);
    let herdr = |machine: Option<&str>| {
        machines.borrow_mut().push(machine.map(String::from));
        tools.herdr(machine)
    };
    let (interrupt, print) = (Interrupt::new(), |t: &str| out.borrow_mut().push_str(t));
    let jenkins = |_: &str| -> Result<crate::ci::Jenkins, OrchestratorError> { panic!("doctor made a Jenkins") };
    let cwd = || -> Result<String, OrchestratorError> { panic!("doctor read the current directory") };
    let local = Local {
        host: &host,
        herdr: &herdr,
        in_herdr_pane: false,
        jenkins: &jenkins,
        cwd: &cwd,
        config: &config,
        stdout: &print,
        stderr: &print,
        interrupt: &interrupt,
    };
    let ended = herdrless_command(&command, &local).expect("doctor needs no herdr pane, and is not connected");
    assert_eq!(ended_status(ended, &command, &print), 0);
    assert_eq!(*machines.borrow(), [Some("box".to_string())]);
    assert!(out.borrow().contains("ok      git checkout: /home/u/proj\n"), "{}", out.borrow());
    assert!(out.borrow().lines().all(|l| l.starts_with("ok      ")), "{}", out.borrow());
    // An interrupted doctor prints nothing and ends interrupted.
    out.borrow_mut().clear();
    interrupt.raise();
    assert!(matches!(herdrless_command(&command, &local), Some(Err(crate::cli::Stop::Interrupted(None)))));
    assert_eq!(*out.borrow(), "");
}

#[test]
fn machine_needs_cwd() {
    let config = ConfigDir::at("/nowhere");
    let parse = |argv: &[&str]| parse_args(argv.iter().map(OsString::from), &config);
    assert_eq!(parse(&["doctor", "--machine", "box"]).unwrap_err().exit_code(), 2);
    assert_eq!(parse(&["doctor"]).unwrap(), crate::cli::Command::Doctor(crate::cli::Target::default()));
}

#[test]
fn the_report() {
    let checks = [
        check_of("a", Level::Required, true, "fine", ""),
        check_of("b", Level::Optional, false, "gone", "get b"),
        check_of("c", Level::NeededForPr, false, "skipped: needs a", "fix a first"),
    ];
    let (out, status) = report_list(&checks);
    assert_eq!(status, EXIT_ERROR);
    assert_eq!(out, "ok      a: fine\nwarn    b: gone\n        fix: get b\nmissing c: skipped: needs a\n        fix: fix a first\n");
    assert_eq!(report_list(&checks[..2]).1, 0);
}

#[test]
fn a_version_number_is_found_as_python_finds_it() {
    let cases = [("herdr 0.9.1", Some("0.9.1")), ("git version 2.39.3 (Apple Git-145)", Some("2.39.3")), ("v12 then 1.2.", Some("1.2")), ("herdr dev", None), ("7", None)];
    for (text, want) in cases {
        assert_eq!(version_number(text), want, "{text}");
    }
}
