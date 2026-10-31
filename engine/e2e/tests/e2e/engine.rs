//! The engine the scenarios run.

use std::path::PathBuf;
use std::process::Command;
use std::sync::OnceLock;

use octoflow_engine::py::json::Json;

/// The orchestrator binary the scenarios run: E2E_ENGINE's, when it is set; else this checkout's, `cargo build --locked
/// -p octoflow-engine --bin orchestrator` run once per test process, in `e2e-engine/` beside this test's `deps/`. It is
/// built rather than found beside this test, since `cargo nextest run -p octoflow-e2e` builds no binary of another
/// package, and a stale one would be tested in its place. Panics when cargo is missing or the build fails, so that a
/// machine without cargo fails the scenarios rather than skipping them.
pub fn binary() -> PathBuf {
    static BINARY: OnceLock<PathBuf> = OnceLock::new();
    BINARY.get_or_init(|| match std::env::var_os("E2E_ENGINE") {
        Some(given) => PathBuf::from(given),
        None => build(),
    })
    .clone()
}

fn build() -> PathBuf {
    // CARGO is the cargo that runs this test, when one does.
    let cargo = std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
    // A target directory of its own: the engine built alone resolves its dependencies' features without this
    // package's (nix's fs), and the workspace's test build with them, so in the one target directory each build would
    // relink <target>/<profile>/orchestrator, which the engine's own program tests run meanwhile.
    let exe = std::env::current_exe().unwrap_or_else(|e| panic!("finding this test's program: {e}"));
    // The test runs from <target>/<profile>/deps/.
    let target = exe.ancestors().nth(2).expect("a target directory above this test's program").join("e2e-engine");
    let out = Command::new(&cargo)
        .args(["build", "--locked", "--quiet", "-p", "octoflow-engine", "--bin", "orchestrator", "--message-format=json"])
        .arg("--target-dir")
        .arg(&target)
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .output()
        .unwrap_or_else(|e| panic!("running {}: {e}", cargo.to_string_lossy()));
    assert!(out.status.success(), "cargo could not build the engine: {}", String::from_utf8_lossy(&out.stderr));
    let stdout = String::from_utf8_lossy(&out.stdout);
    let paths: Vec<String> = stdout
        .lines()
        .filter_map(|line| Json::parse(line).ok())
        .filter(|m| m.get("reason").and_then(Json::as_str) == Some("compiler-artifact"))
        .filter_map(|m| m.get("executable").and_then(Json::as_str).map(str::to_string))
        .collect();
    let [path] = paths.as_slice() else { panic!("cargo reported {} binaries for the engine, not one", paths.len()) };
    PathBuf::from(path)
}
