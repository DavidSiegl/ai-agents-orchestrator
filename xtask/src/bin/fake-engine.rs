//! A stand-in for the engine `cargo xtask eval` runs, for tests/eval.rs: a program that plays one scripted run.
//!
//! The script is the JSON file FAKE_ENGINE_PLAN names, an object of:
//!
//! - "files": paths, relative to --cwd, and the text to write there, as the agents would;
//! - "exit": the run's exit status;
//! - "run_id": the name of the run directory to make under .orchestrator/runs, or null for none;
//! - "metrics": what to write as that directory's metrics.json, or null for none.
//!
//! Each invocation appends a JSON line to the file FAKE_ENGINE_LOG names: its arguments as "argv", and for `run`, what
//! the agents could see: every path under --cwd but .git's ("paths"), origin's URL ("origin"), and the spec's path and
//! text ("spec", "spec_text"). It panics on whatever it cannot do, which fails the test that ran it.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::process::{Command, ExitCode};

use serde_json::{json, Value};

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let plan_path = std::env::var("FAKE_ENGINE_PLAN").expect("FAKE_ENGINE_PLAN is set");
    let plan: Value =
        serde_json::from_str(&fs::read_to_string(&plan_path).expect("the plan reads")).expect("the plan is JSON");
    let mut entry = json!({"argv": argv});
    let mut exit = 0;
    if argv.first().is_some_and(|command| command == "run") {
        let cwd = Path::new(after(&argv, "--cwd"));
        let spec = after(&argv, "--spec");
        entry["paths"] = json!(paths_under(cwd));
        entry["origin"] = json!(origin(cwd));
        entry["spec"] = json!(spec);
        entry["spec_text"] = json!(fs::read_to_string(spec).expect("the spec reads"));
        for (path, text) in plan["files"].as_object().into_iter().flatten() {
            let full = cwd.join(path);
            fs::create_dir_all(full.parent().expect("a file has a directory")).expect("its directory is made");
            fs::write(&full, text.as_str().expect("a file's text is a string")).expect("the file is written");
        }
        if let Some(run_id) = plan["run_id"].as_str() {
            let run_dir = cwd.join(".orchestrator/runs").join(run_id);
            fs::create_dir_all(&run_dir).expect("the run directory is made");
            if !plan["metrics"].is_null() {
                fs::write(run_dir.join("metrics.json"), plan["metrics"].to_string()).expect("metrics.json is written");
            }
        }
        exit = plan["exit"].as_u64().unwrap_or(0);
    }
    let log = std::env::var("FAKE_ENGINE_LOG").expect("FAKE_ENGINE_LOG is set");
    let mut log = OpenOptions::new().create(true).append(true).open(log).expect("the log opens");
    writeln!(log, "{entry}").expect("the log is written");
    ExitCode::from(u8::try_from(exit).expect("the exit status is a byte"))
}

/// The argument after flag in argv.
fn after<'a>(argv: &'a [String], flag: &str) -> &'a str {
    let i = argv.iter().position(|arg| arg == flag).unwrap_or_else(|| panic!("{flag} is given"));
    &argv[i + 1]
}

/// Every path under dir but .git's, relative to it, sorted.
fn paths_under(dir: &Path) -> Vec<String> {
    let mut paths = vec![];
    let mut dirs = vec![dir.to_path_buf()];
    while let Some(d) = dirs.pop() {
        for entry in fs::read_dir(&d).expect("the clone lists") {
            let path = entry.expect("an entry reads").path();
            if path.file_name().is_some_and(|name| name == ".git") {
                continue;
            }
            paths.push(path.strip_prefix(dir).expect("under dir").to_string_lossy().into_owned());
            if path.is_dir() {
                dirs.push(path);
            }
        }
    }
    paths.sort();
    paths
}

/// The URL of the origin of the repository at dir.
fn origin(dir: &Path) -> String {
    let output =
        Command::new("git").arg("-C").arg(dir).args(["remote", "get-url", "origin"]).output().expect("git runs");
    assert!(output.status.success(), "git remote get-url origin failed");
    String::from_utf8(output.stdout).expect("the URL is UTF-8").trim().to_string()
}
