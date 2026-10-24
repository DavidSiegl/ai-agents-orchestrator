//! The desktop shell: a window over `orchestrator.py serve`, which it starts as its sidecar when it launches and
//! kills when it exits. The webview talks to the sidecar's server over HTTP; the shell only hands it the server's
//! address and token, through the `server` command.

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::{Manager, RunEvent};

/// The environment variable that names the orchestrator.py to serve; without it, the checkout's own.
const ORCHESTRATOR_ENV: &str = "ORCHESTRATOR_PY";

/// The line `serve` prints first: where its server listens, and the token every request needs.
#[derive(Deserialize)]
struct Served {
    url: String,
    token: String,
}

/// What the webview needs to reach the sidecar, as the `server` command gives it.
#[derive(Clone, Serialize)]
struct Connection {
    url: String,
    token: String,
    /// The project the app opens first: the directory of the orchestrator.py served, a checkout of its own.
    project: String,
}

/// The sidecar: its process while it runs, and how to reach it, or why it could not be started.
struct Sidecar {
    /// None once it has been stopped, or when it never started.
    child: Mutex<Option<Child>>,
    connection: Result<Connection, String>,
}

impl Sidecar {
    /// Start `python3 <script> serve`, answering requests from origin, and read the server's first line. A sidecar
    /// that cannot be started, or prints no such line, keeps the reason as its connection.
    fn start(script: &Path, origin: &str) -> Sidecar {
        match spawn(script, origin) {
            Ok((child, served)) => {
                let project = script.parent().map(|dir| dir.display().to_string()).unwrap_or_default();
                let connection = Connection { url: served.url, token: served.token, project };
                Sidecar { child: Mutex::new(Some(child)), connection: Ok(connection) }
            }
            Err(reason) => Sidecar { child: Mutex::new(None), connection: Err(reason) },
        }
    }

    /// Kill the sidecar and wait for it; nothing when it is not running.
    fn stop(&self) {
        let mut child = self.child.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(mut child) = child.take() {
            // It may have exited already, which is what killing it is for.
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// The started sidecar and its first line; Err with why, the sidecar killed, when it cannot be started or that line
/// is not `serve`'s.
fn spawn(script: &Path, origin: &str) -> Result<(Child, Served), String> {
    let mut child = Command::new("python3")
        .arg(script)
        .args(["serve", "--origin", origin])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| format!("cannot run python3 {}: {e}", script.display()))?;
    let mut line = String::new();
    let read = match child.stdout.take() {
        Some(stdout) => BufReader::new(stdout).read_line(&mut line).map_err(|e| e.to_string()),
        None => Err("its standard output is not a pipe".to_string()),
    };
    let served = read.and_then(|_| {
        serde_json::from_str::<Served>(&line)
            .map_err(|e| format!("python3 {} serve printed {:?}, not its address: {e}", script.display(), line.trim()))
    });
    match served {
        Ok(served) => Ok((child, served)),
        Err(reason) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(reason)
        }
    }
}

/// The orchestrator.py to serve: $ORCHESTRATOR_PY, or else the one of the checkout this app was built in.
fn orchestrator_script() -> PathBuf {
    let path = std::env::var_os(ORCHESTRATOR_ENV)
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../orchestrator.py"));
    path.canonicalize().unwrap_or(path)
}

/// The origin of the webview's pages, which the sidecar must answer: Vite's dev server under `tauri dev`, else the
/// custom protocol the bundled frontend is served by.
fn webview_origin(app: &tauri::App) -> String {
    if tauri::is_dev() {
        if let Some(url) = &app.config().build.dev_url {
            return url.origin().ascii_serialization();
        }
    }
    if cfg!(windows) { "http://tauri.localhost" } else { "tauri://localhost" }.to_string()
}

/// The sidecar's address and token, or why it could not be started.
#[tauri::command]
fn server(sidecar: tauri::State<'_, Sidecar>) -> Result<Connection, String> {
    sidecar.connection.clone()
}

/// Run the app until its window closes, then stop the sidecar.
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let sidecar = Sidecar::start(&orchestrator_script(), &webview_origin(app));
            app.manage(sidecar);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![server])
        .build(tauri::generate_context!())
        .expect("error while building the app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                app.state::<Sidecar>().stop();
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Whether a process with this pid still exists, zombies included, by its /proc entry.
    fn exists(pid: u32) -> bool {
        Path::new(&format!("/proc/{pid}")).exists()
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn serves_the_checkout_and_stops() {
        let sidecar = Sidecar::start(&orchestrator_script(), "tauri://localhost");
        let connection = sidecar.connection.clone().expect("the sidecar started");
        assert!(connection.url.starts_with("http://127.0.0.1:"), "{}", connection.url);
        assert!(!connection.token.is_empty());
        assert!(Path::new(&connection.project).join("orchestrator.py").is_file(), "{}", connection.project);
        let pid = sidecar.child.lock().unwrap().as_ref().expect("a child").id();
        assert!(exists(pid));
        sidecar.stop();
        assert!(!exists(pid), "serve {pid} is still there");
        sidecar.stop();
    }

    #[test]
    fn a_script_that_is_not_serve_is_an_error() {
        let sidecar = Sidecar::start(Path::new("/nonexistent/orchestrator.py"), "tauri://localhost");
        let reason = sidecar.connection.err().expect("an error");
        assert!(reason.contains("/nonexistent/orchestrator.py"), "{reason}");
        assert!(sidecar.child.lock().unwrap().is_none());
    }
}
