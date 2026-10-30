//! The desktop shell: a window over `orchestrator serve`, which it starts as its sidecar when it launches and
//! kills when it exits. The webview talks to the sidecar's server over HTTP; the shell only hands it the server's
//! address and token, through the `server` command. Started with arguments, the app is the orchestrator's CLI
//! instead: see `cli`.

use std::ffi::{OsStr, OsString};
use std::fmt;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::{Manager, RunEvent};

/// The environment variable that names an orchestrator binary to serve, in place of the bundled one or the
/// checkout's own build.
const ORCHESTRATOR_ENV: &str = "ORCHESTRATOR_BIN";

/// The orchestrator binary's file name in a release bundle, beside the app's own executable: Tauri's
/// `externalBin` in tauri.release.conf.json, which drops the target triple that packaging/build-desktop.sh gives
/// it on disk.
const BUNDLED_NAME: &str = if cfg!(windows) { "octoflow.exe" } else { "octoflow" };

/// The orchestrator the app serves, an executable of the Rust engine (engine/).
#[derive(Debug, PartialEq)]
enum Orchestrator {
    /// The build of the checkout this app was built in, target/debug/orchestrator in its Cargo workspace, which
    /// `cargo build -p octoflow-engine` makes: a development build's, which serves that checkout's project.
    Checkout(PathBuf),
    /// Any other: $ORCHESTRATOR_BIN, or the binary a release bundle carries beside the app; it belongs to no
    /// checkout.
    Binary(PathBuf),
}

impl Orchestrator {
    /// The one to serve: $ORCHESTRATOR_BIN when it is set, else the binary beside this executable when there is
    /// one, as in a release bundle, else the checkout's own build.
    fn locate() -> Orchestrator {
        let exe_dir = std::env::current_exe().ok().and_then(|exe| exe.parent().map(Path::to_path_buf));
        Orchestrator::locate_from(std::env::var_os(ORCHESTRATOR_ENV), exe_dir.as_deref())
    }

    /// locate, given $ORCHESTRATOR_BIN's value and the executable's directory, None when either is unknown.
    fn locate_from(env: Option<OsString>, exe_dir: Option<&Path>) -> Orchestrator {
        if let Some(binary) = env {
            return Orchestrator::Binary(canonical(PathBuf::from(binary)));
        }
        if let Some(bundled) = exe_dir.map(|dir| dir.join(BUNDLED_NAME)).filter(|path| path.is_file()) {
            return Orchestrator::Binary(bundled);
        }
        Orchestrator::Checkout(checkout().join("target/debug/orchestrator"))
    }

    /// A command that runs it with no arguments yet, in shell's environment and directory (`UserShell::apply`).
    fn command(&self, shell: &UserShell) -> Command {
        let (Orchestrator::Checkout(binary) | Orchestrator::Binary(binary)) = self;
        let mut command = Command::new(binary);
        shell.apply(&mut command);
        command
    }

    /// The directory of the checkout it belongs to; None for any binary but the checkout's own build.
    fn project(&self) -> Option<String> {
        match self {
            Orchestrator::Checkout(_) => Some(checkout().display().to_string()),
            Orchestrator::Binary(_) => None,
        }
    }
}

/// The top of the checkout this app was built in, two directories above its crate's.
fn checkout() -> PathBuf {
    canonical(Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."))
}

/// How it is run, for messages: the binary's path.
impl fmt::Display for Orchestrator {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let (Orchestrator::Checkout(binary) | Orchestrator::Binary(binary)) = self;
        write!(f, "{}", binary.display())
    }
}

/// The environment and working directory the shell that started the app had, as far as they can be recovered: what
/// the orchestrator's process gets, so that it, and every process it starts, sees the user's libraries, Python and
/// project rather than the bundle's. The app's own process keeps its environment, which GTK and WebKit need.
///
/// Only an AppImage changes them. Its start-up (linuxdeploy's `AppRun`, its GTK hook and AppImageKit's
/// `AppRun.wrapped`, as measured from v0.25.0's) points path lists and Python and GTK settings into the bundle,
/// sets a few variables of its own, and starts the app in `$APPDIR/usr`. It names the bundle both as `$APPDIR` and
/// as what that resolves to, which `AppRun.wrapped` writes, so the two differ when `TMPDIR` is a link. The same
/// start-up runs when a user starts an AppImage they extracted (`squashfs-root/AppRun`), with no `APPIMAGE` set.
/// The user's directory survives in `OWD`,
/// which the runtime sets when it mounts the AppImage but not when it extracts it (`APPIMAGE_EXTRACT_AND_RUN`, as
/// on a machine without FUSE), and in `PWD`, which `AppRun`, a bash script, checks against its directory at start,
/// before `AppRun.wrapped` changes it. Elsewhere, macOS and the .deb included, the environment and directory are
/// the app's own.
#[derive(Debug, PartialEq)]
struct UserShell {
    /// The whole environment, in the order it came.
    vars: Vec<(OsString, OsString)>,
    /// The working directory to start in; None to keep the app's own.
    dir: Option<PathBuf>,
}

/// Path lists the AppImage's start-up prefixes with entries in the bundle, followed by the user's value, or by an
/// empty entry when the user had none. The GTK hook first sets GSETTINGS_SCHEMA_DIR, GI_TYPELIB_PATH,
/// GIO_MODULE_DIR and GTK_PATH to the bundle's alone, so the user's value of those four is already lost and they
/// end up unset.
const APPIMAGE_PATH_LISTS: &[&str] = &[
    "LD_LIBRARY_PATH",
    "PATH",
    "PYTHONPATH",
    "PERLLIB",
    "QT_PLUGIN_PATH",
    "XDG_DATA_DIRS",
    "GSETTINGS_SCHEMA_DIR",
    "GI_TYPELIB_PATH",
    "GIO_MODULE_DIR",
    "GTK_PATH",
    "GST_PLUGIN_SYSTEM_PATH",
    "GST_PLUGIN_SYSTEM_PATH_1_0",
];

/// Entries outside the bundle that the start-up puts in a path list's prefix, after the bundle's own: the GTK hook's
/// `/usr/share` in XDG_DATA_DIRS. Only the first such entry is the start-up's; another is the user's.
const APPIMAGE_PREFIX_ENTRIES: &[(&str, &str)] = &[("XDG_DATA_DIRS", "/usr/share")];

/// Variables the AppImage's start-up sets to a path in the bundle, overwriting the user's; a value outside the
/// bundle is the user's own.
const APPIMAGE_PATHS: &[&str] =
    &["PYTHONHOME", "GTK_DATA_PREFIX", "GTK_EXE_PREFIX", "GTK_IM_MODULE_FILE", "GDK_PIXBUF_MODULE_FILE"];

/// Variables the AppImage's start-up sets whatever the user had, to values that do not name the bundle, so the
/// user's own value, if any, is already lost: unset whatever they hold. GTK_THEME is the GTK hook's
/// `Adwaita:light` or `Adwaita:dark`, set over any GTK_THEME of the user's (who sets APPIMAGE_GTK_THEME to choose
/// it); PYTHONDONTWRITEBYTECODE is AppImageKit's `1`.
const APPIMAGE_ONLY: &[&str] = &["GTK_THEME", "PYTHONDONTWRITEBYTECODE", "APPDIR", "APPIMAGE", "ARGV0", "OWD"];

impl UserShell {
    /// The shell that started this process.
    fn current() -> UserShell {
        UserShell::from_vars(std::env::vars_os(), std::env::current_exe().ok().as_deref())
    }

    /// The user's shell, recovered from the app's environment vars, given the app's executable exe, None when it is
    /// unknown. Under an AppImage (`APPDIR` an absolute path below `/`, with `APPIMAGE` set, or with exe in the
    /// bundle and the bundle's `AppRun` there, as when the user runs an extracted one): each path list in
    /// APPIMAGE_PATH_LISTS keeps only the user's value (`users_path_list`) and is unset when that is empty; each of
    /// APPIMAGE_PATHS is unset when it is in the bundle; each of APPIMAGE_ONLY is unset; and the directory is `OWD`,
    /// else `PWD`, else the app's own. The bundle is `$APPDIR` and what it resolves to, and everything under
    /// either. Otherwise vars and the app's directory as they are.
    fn from_vars(vars: impl IntoIterator<Item = (OsString, OsString)>, exe: Option<&Path>) -> UserShell {
        let vars: Vec<(OsString, OsString)> = vars.into_iter().collect();
        let get = |name: &str| vars.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone());
        // An empty APPDIR, or `/`, would put every path in the bundle.
        let Some(appdir) = get("APPDIR").map(PathBuf::from).filter(|dir| dir.is_absolute() && dir.parent().is_some())
        else {
            return UserShell { vars, dir: None };
        };
        let bundle = [canonical(appdir.clone()), appdir];
        let in_bundle = |path: &Path| bundle.iter().any(|root| path.starts_with(root));
        let extracted = exe.is_some_and(in_bundle) && appdir_marker(&bundle[0]);
        if get("APPIMAGE").is_none() && !extracted {
            return UserShell { vars, dir: None };
        }
        let dir = get("OWD").or_else(|| get("PWD")).map(PathBuf::from);
        let vars = vars
            .into_iter()
            .filter_map(|(name, value)| {
                let Some(name_str) = name.to_str() else { return Some((name, value)) };
                if APPIMAGE_ONLY.contains(&name_str) {
                    return None;
                }
                if APPIMAGE_PATHS.contains(&name_str) {
                    return (!in_bundle(Path::new(&value))).then_some((name, value));
                }
                if APPIMAGE_PATH_LISTS.contains(&name_str) {
                    let users = users_path_list(name_str, &value, in_bundle);
                    return (!users.is_empty()).then_some((name, users));
                }
                Some((name, value))
            })
            .collect();
        UserShell { vars, dir }
    }

    /// Make command start with this environment, and in this directory when it is one. A directory since removed
    /// leaves the app's own, so that the sidecar, which needs none, still starts; `Cli::exec` refuses it instead.
    fn apply(&self, command: &mut Command) {
        command.env_clear().envs(self.vars.iter().map(|(k, v)| (k, v)));
        if let Some(dir) = self.dir.as_ref().filter(|dir| dir.is_dir()) {
            command.current_dir(dir);
        }
    }
}

/// Whether appdir holds the `AppRun` every AppImage's bundle starts with.
fn appdir_marker(appdir: &Path) -> bool {
    appdir.join("AppRun").is_file()
}

/// The user's own value of the path list name, from value as the AppImage's start-up left it: without the prefix
/// the start-up added (its leading entries in_bundle, and the entry APPIMAGE_PREFIX_ENTRIES names for name after
/// them) and without any other entry in_bundle. Empty when nothing of the user's is left: when they had none, which
/// the start-up marks with a lone empty entry after its prefix, or when the start-up overwrote it
/// (APPIMAGE_PATH_LISTS). The user's own empty entries are kept.
fn users_path_list(name: &str, value: &OsStr, in_bundle: impl Fn(&Path) -> bool) -> OsString {
    let mut start_up_entry =
        APPIMAGE_PREFIX_ENTRIES.iter().find(|(list, _)| *list == name).map(|(_, entry)| Path::new(entry));
    let mut entries = std::env::split_paths(value).peekable();
    let mut prefixed = false;
    while let Some(entry) = entries.peek() {
        if in_bundle(entry) {
            prefixed = true;
        } else if prefixed && start_up_entry == Some(entry.as_path()) {
            start_up_entry = None;
        } else {
            break;
        }
        entries.next();
    }
    let kept: Vec<PathBuf> = entries.filter(|entry| !in_bundle(entry)).collect();
    // Rejoining entries split at the separator cannot meet one that holds it.
    std::env::join_paths(kept).expect("entries without a separator")
}

/// The orchestrator's CLI, ready to replace the app's process.
pub struct Cli {
    /// What the command runs, for the message when it cannot.
    orchestrator: Orchestrator,
    /// The whole command line, with the user's shell's environment and, when it exists, directory.
    command: Command,
    /// The user's shell's directory, which the CLI must start in; None to start in the app's own.
    dir: Option<PathBuf>,
}

/// What the app's arguments, without its own name, ask for: with any, the CLI of the orchestrator
/// `Orchestrator::locate` picks, given exactly those arguments, in the user's shell's environment and directory
/// (`UserShell`); with none, or on macOS only the one a Finder launch may pass (`is_finder_launch`), None, for the
/// window.
pub fn cli(args: impl IntoIterator<Item = OsString>) -> Option<Cli> {
    let args: Vec<OsString> = args.into_iter().collect();
    if args.is_empty() || cfg!(target_os = "macos") && is_finder_launch(&args) {
        return None;
    }
    let orchestrator = Orchestrator::locate();
    let shell = UserShell::current();
    let mut command = orchestrator.command(&shell);
    command.args(args);
    Some(Cli { orchestrator, command, dir: shell.dir })
}

/// Whether args are only the process serial number, `-psn_<n>`, that macOS may pass an app opened from the Finder,
/// as on the first, quarantined launch.
fn is_finder_launch(args: &[OsString]) -> bool {
    matches!(args, [only] if only.to_str().is_some_and(|arg| arg.starts_with("-psn_")))
}

impl Cli {
    /// Replace this process with the CLI, so that the terminal, the exit status and the signals sent to the app are
    /// the CLI's own. Not so under an AppImage run without FUSE: the process the caller started is the AppImage's
    /// runtime, which stays this one's parent and passes on no signal sent to it (TODO.md). Returns never: when the
    /// user's directory is gone or the exec fails, prints why on standard error and exits 1. Call it before anything
    /// else starts a thread or opens a window, which an exec would discard.
    #[cfg(unix)]
    pub fn exec(mut self) -> ! {
        use std::os::unix::process::CommandExt;
        if let Some(why) = self.why_not_started() {
            eprintln!("octoflow-ui: {why}");
            std::process::exit(1)
        }
        let e = self.command.exec();
        eprintln!("octoflow-ui: cannot run {}: {e}", self.orchestrator);
        std::process::exit(1)
    }

    /// Why the CLI cannot start where it must, the user's directory being gone; None when it can.
    fn why_not_started(&self) -> Option<String> {
        let dir = self.dir.as_ref().filter(|dir| !dir.is_dir())?;
        Some(format!("cannot start in {}: it is not a directory", dir.display()))
    }
}

/// path made absolute with its links resolved, or path itself when it does not exist.
fn canonical(path: PathBuf) -> PathBuf {
    path.canonicalize().unwrap_or(path)
}

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
    /// The project the app opens first: the checkout the orchestrator served belongs to; None for the bundled
    /// one, which belongs to none.
    project: Option<String>,
}

/// The sidecar: its process while it runs, and how to reach it, or why it could not be started.
struct Sidecar {
    /// None once it has been stopped, or when it never started.
    child: Mutex<Option<Child>>,
    connection: Result<Connection, String>,
}

impl Sidecar {
    /// Start orchestrator's `serve`, answering requests from origin, and read the server's first line. A sidecar
    /// that cannot be started, or prints no such line, keeps the reason as its connection.
    fn start(orchestrator: &Orchestrator, origin: &str) -> Sidecar {
        match spawn(orchestrator, origin) {
            Ok((child, served)) => {
                let connection = Connection { url: served.url, token: served.token, project: orchestrator.project() };
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
fn spawn(orchestrator: &Orchestrator, origin: &str) -> Result<(Child, Served), String> {
    let mut child = orchestrator
        .command(&UserShell::current())
        .args(["serve", "--origin", origin])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| format!("cannot run {orchestrator}: {e}"))?;
    let mut line = String::new();
    let read = match child.stdout.take() {
        Some(stdout) => BufReader::new(stdout).read_line(&mut line).map_err(|e| e.to_string()),
        None => Err("its standard output is not a pipe".to_string()),
    };
    let served = read.and_then(|_| {
        serde_json::from_str::<Served>(&line)
            .map_err(|e| format!("{orchestrator} serve printed {:?}, not its address: {e}", line.trim()))
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
            let sidecar = Sidecar::start(&Orchestrator::locate(), &webview_origin(app));
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
    use std::ffi::OsStr;

    /// Whether a process with this pid still exists, zombies included, by its /proc entry.
    fn exists(pid: u32) -> bool {
        Path::new(&format!("/proc/{pid}")).exists()
    }

    /// A new empty directory of this test's own under the system's temporary directory.
    fn scratch_dir(test: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("octoflow-ui-{test}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("a scratch directory");
        dir
    }

    /// The orchestrator binary of this checkout, built as `cargo build -p octoflow-engine` builds it, with the cargo
    /// that runs these tests; its path is the one cargo reports, wherever CARGO_TARGET_DIR puts it.
    fn built_orchestrator() -> PathBuf {
        let cargo = std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
        let built = Command::new(cargo)
            .args(["build", "--locked", "--quiet", "-p", "octoflow-engine", "--bin", "orchestrator"])
            .args(["--message-format", "json"])
            .current_dir(checkout())
            .output()
            .expect("cargo runs");
        assert!(built.status.success(), "{}", String::from_utf8_lossy(&built.stderr));
        let report = String::from_utf8(built.stdout).expect("UTF-8");
        let executable = report
            .lines()
            .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
            .find_map(|message| message["executable"].as_str().map(PathBuf::from));
        executable.expect("cargo reports the orchestrator it built")
    }

    #[test]
    fn locates_the_checkout_s_build_without_a_bundled_binary() {
        let located = Orchestrator::locate_from(None, Some(&scratch_dir("checkout")));
        let Orchestrator::Checkout(binary) = &located else { panic!("{located:?}") };
        assert!(binary.ends_with("target/debug/orchestrator"), "{located}");
        assert!(Path::new(&located.project().expect("a checkout")).join("engine/Cargo.toml").is_file());
    }

    #[test]
    fn locates_the_bundled_binary_beside_the_executable() {
        let dir = scratch_dir("bundled");
        std::fs::write(dir.join(BUNDLED_NAME), "").expect("a stand-in binary");
        let located = Orchestrator::locate_from(None, Some(&dir));
        assert_eq!(located, Orchestrator::Binary(dir.join(BUNDLED_NAME)));
        assert_eq!(located.project(), None);
    }

    #[test]
    fn the_environment_wins_over_the_bundled_binary() {
        let dir = scratch_dir("environment");
        std::fs::write(dir.join(BUNDLED_NAME), "").expect("a stand-in binary");
        let located = Orchestrator::locate_from(Some("/elsewhere/orchestrator".into()), Some(&dir));
        assert_eq!(located, Orchestrator::Binary(PathBuf::from("/elsewhere/orchestrator")));
    }

    #[test]
    #[cfg(unix)]
    fn serves_a_bundled_binary_without_a_project() {
        use std::os::unix::fs::PermissionsExt;
        let binary = scratch_dir("bundled-serve").join(BUNDLED_NAME);
        // serve's first line, then a process for stop to kill.
        let script = "#!/bin/sh\necho '{\"url\": \"http://127.0.0.1:9\", \"token\": \"t\"}'\nexec sleep 60\n";
        std::fs::write(&binary, script).expect("a stand-in binary");
        std::fs::set_permissions(&binary, std::fs::Permissions::from_mode(0o755)).expect("an executable");
        let sidecar = Sidecar::start(&Orchestrator::Binary(binary), "tauri://localhost");
        let connection = sidecar.connection.clone().expect("the sidecar started");
        assert_eq!(connection.url, "http://127.0.0.1:9");
        assert_eq!(connection.project, None);
        sidecar.stop();
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn serves_the_checkout_and_stops() {
        let sidecar = Sidecar::start(&Orchestrator::Checkout(built_orchestrator()), "tauri://localhost");
        let connection = sidecar.connection.clone().expect("the sidecar started");
        assert!(connection.url.starts_with("http://127.0.0.1:"), "{}", connection.url);
        assert!(!connection.token.is_empty());
        let project = connection.project.expect("a checkout");
        assert!(Path::new(&project).join("engine/Cargo.toml").is_file(), "{project}");
        let pid = sidecar.child.lock().unwrap().as_ref().expect("a child").id();
        assert!(exists(pid));
        sidecar.stop();
        assert!(!exists(pid), "serve {pid} is still there");
        sidecar.stop();
    }

    #[test]
    fn a_binary_that_is_not_there_is_an_error() {
        let sidecar = Sidecar::start(&Orchestrator::Binary("/nonexistent/orchestrator".into()), "tauri://localhost");
        let reason = sidecar.connection.err().expect("an error");
        assert!(reason.contains("/nonexistent/orchestrator"), "{reason}");
        assert!(sidecar.child.lock().unwrap().is_none());
    }

    /// The user's shell from env, the app's executable unknown.
    fn shell_of(env: Vec<(OsString, OsString)>) -> UserShell {
        UserShell::from_vars(env, None)
    }

    /// An environment of name=value pairs, in order.
    fn vars(pairs: &[(&str, &str)]) -> Vec<(OsString, OsString)> {
        pairs.iter().map(|(k, v)| (OsString::from(k), OsString::from(v))).collect()
    }

    /// An AppImage's start-up: APPDIR /tmp/.mount_x, the AppImage, and the user's directory /home/u/proj, before
    /// rest.
    fn appimage(rest: &[(&str, &str)]) -> Vec<(OsString, OsString)> {
        let mut env =
            vars(&[("APPDIR", "/tmp/.mount_x"), ("APPIMAGE", "/home/u/app.AppImage"), ("OWD", "/home/u/proj")]);
        env.extend(vars(rest));
        env
    }

    #[test]
    fn the_arguments_ask_for_the_cli_and_none_for_the_window() {
        assert!(cli(Vec::<OsString>::new()).is_none());
        let Cli { orchestrator, command, .. } = cli(["run".into(), "add a limiter".into()]).expect("the CLI");
        assert_eq!(orchestrator, Orchestrator::locate());
        let args: Vec<&OsStr> = command.get_args().collect();
        let (Orchestrator::Checkout(binary) | Orchestrator::Binary(binary)) = &orchestrator;
        assert_eq!(command.get_program(), binary.as_os_str());
        assert_eq!(args, [OsStr::new("run"), OsStr::new("add a limiter")]);
    }

    #[test]
    #[cfg(unix)]
    fn the_cli_gets_the_arguments_as_they_came() {
        use std::os::unix::ffi::OsStringExt;
        let odd = OsString::from_vec(b"not \xff utf-8".to_vec());
        let Cli { command, .. } = cli([odd.clone(), "--help".into()]).expect("the CLI");
        let args: Vec<&OsStr> = command.get_args().collect();
        assert_eq!(args, [odd.as_os_str(), OsStr::new("--help")]);
    }

    #[test]
    fn outside_an_appimage_nothing_changes() {
        // APPDIR alone, as a user might export it, is not an AppImage.
        let env =
            vars(&[("APPDIR", "/opt/x"), ("LD_LIBRARY_PATH", "/opt/x/lib"), ("PYTHONHOME", "/opt/x"), ("OWD", "/")]);
        assert_eq!(shell_of(env.clone()), UserShell { vars: env.clone(), dir: None });
        let elsewhere = UserShell::from_vars(env.clone(), Some(Path::new("/usr/bin/octoflow-ui")));
        assert_eq!(elsewhere, UserShell { vars: env, dir: None });
    }

    #[test]
    fn an_extracted_appimage_started_by_its_apprun_is_one_too() {
        // squashfs-root/AppRun sets APPDIR and the bundle's variables, but no APPIMAGE.
        let root = scratch_dir("squashfs-root").canonicalize().expect("a scratch directory");
        std::fs::write(root.join("AppRun"), "").expect("a stand-in AppRun");
        let python_home = format!("{}/usr/", root.display());
        let env = vars(&[("APPDIR", root.to_str().unwrap()), ("PYTHONHOME", &python_home), ("PWD", "/u/p")]);
        let exe = root.join("usr/bin/octoflow-ui");
        let shell = UserShell::from_vars(env.clone(), Some(&exe));
        assert_eq!(shell, UserShell { vars: vars(&[("PWD", "/u/p")]), dir: Some("/u/p".into()) });
        // Without AppRun, an APPDIR that holds the executable is a user's, not a bundle.
        std::fs::remove_file(root.join("AppRun")).expect("AppRun removed");
        assert_eq!(UserShell::from_vars(env.clone(), Some(&exe)), UserShell { vars: env, dir: None });
    }

    #[test]
    fn an_empty_relative_or_root_appdir_is_no_appimage() {
        for appdir in ["", "/", "squashfs-root"] {
            let env = vars(&[("APPDIR", appdir), ("APPIMAGE", "/a.AppImage"), ("PATH", "/usr/bin:/bin")]);
            let shell = UserShell::from_vars(env.clone(), Some(Path::new("/usr/bin/octoflow-ui")));
            assert_eq!(shell, UserShell { vars: env, dir: None }, "APPDIR={appdir:?}");
        }
    }

    #[test]
    #[cfg(unix)]
    fn the_bundle_is_appdir_and_what_it_resolves_to() {
        let dir = scratch_dir("linked-appdir");
        std::fs::create_dir_all(dir.join("real/app/usr/lib")).expect("an AppDir");
        std::fs::write(dir.join("real/app/AppRun"), "").expect("a stand-in AppRun");
        std::os::unix::fs::symlink(dir.join("real"), dir.join("link")).expect("a link");
        let (real, link) = (dir.join("real/app"), dir.join("link/app"));
        let real = real.canonicalize().expect("the AppDir resolves");
        // As AppRun.wrapped writes them: from /proc/self/exe, resolved; the GTK hook's from APPDIR, as it is.
        let path_list = format!("{}/usr/lib/:{}/usr/lib:", real.display(), link.display());
        let python_home = format!("{}/usr/", real.display());
        let env = vars(&[
            ("APPDIR", link.to_str().unwrap()),
            ("APPIMAGE", "/a.AppImage"),
            ("LD_LIBRARY_PATH", &path_list),
            ("PYTHONHOME", &python_home),
            ("HOME", "/home/u"),
        ]);
        assert_eq!(shell_of(env).vars, vars(&[("HOME", "/home/u")]));
        let started = UserShell::from_vars(vars(&[("APPDIR", link.to_str().unwrap())]), Some(&real.join("usr/bin/x")));
        assert_eq!(started, UserShell { vars: vec![], dir: None });
    }

    #[test]
    fn under_an_appimage_the_directory_is_the_users() {
        let shell = shell_of(appimage(&[("PWD", "/home/u")]));
        assert_eq!(shell, UserShell { vars: vars(&[("PWD", "/home/u")]), dir: Some("/home/u/proj".into()) });
    }

    #[test]
    fn under_an_extracted_appimage_the_directory_is_pwd() {
        let env =
            vars(&[("APPDIR", "/tmp/appimage_extracted_x"), ("APPIMAGE", "/a.AppImage"), ("PWD", "/home/u/proj")]);
        let shell = shell_of(env);
        assert_eq!(shell, UserShell { vars: vars(&[("PWD", "/home/u/proj")]), dir: Some("/home/u/proj".into()) });
    }

    #[test]
    fn under_an_appimage_without_owd_or_pwd_the_directory_stays() {
        let env = vars(&[("APPDIR", "/tmp/.mount_x"), ("APPIMAGE", "/a.AppImage"), ("HOME", "/home/u")]);
        assert_eq!(shell_of(env), UserShell { vars: vars(&[("HOME", "/home/u")]), dir: None });
    }

    #[test]
    fn each_path_list_loses_the_bundles_entries_and_keeps_the_users_in_order() {
        for name in APPIMAGE_PATH_LISTS {
            let mixed = "/tmp/.mount_x/usr/lib:/usr/local/lib:/tmp/.mount_x:/usr/lib::/tmp/.mount_xy/lib";
            let shell = shell_of(appimage(&[(name, mixed), ("HOME", "/home/u")]));
            // The empty entry is the user's: the current directory, in a PATH.
            let expected = vars(&[(name, "/usr/local/lib:/usr/lib::/tmp/.mount_xy/lib"), ("HOME", "/home/u")]);
            assert_eq!(shell.vars, expected, "{name}");
        }
    }

    #[test]
    fn a_path_list_of_only_the_bundles_entries_is_unset() {
        for name in APPIMAGE_PATH_LISTS {
            // AppRun's prefixing of an unset variable leaves an empty entry at the end.
            let shell = shell_of(appimage(&[(name, "/tmp/.mount_x/usr/lib:/tmp/.mount_x/usr/lib64:")]));
            assert_eq!(shell.vars, vec![], "{name}");
        }
    }

    #[test]
    fn a_path_list_keeps_the_users_value_as_it_was() {
        for name in APPIMAGE_PATH_LISTS {
            let shell = shell_of(appimage(&[(name, "/tmp/.mount_x/usr/lib/:/usr/lib::/lib:")]));
            assert_eq!(shell.vars, vars(&[(name, "/usr/lib::/lib:")]), "{name}");
        }
    }

    #[test]
    fn the_gtk_hooks_system_data_dir_is_the_start_ups() {
        let hooked = "/tmp/.mount_x/usr/share/:/tmp/.mount_x/usr/share:/usr/share:";
        assert_eq!(shell_of(appimage(&[("XDG_DATA_DIRS", hooked)])).vars, vec![]);
        let users = "/tmp/.mount_x/usr/share/:/tmp/.mount_x/usr/share:/usr/share:/usr/share:/opt/share";
        assert_eq!(
            shell_of(appimage(&[("XDG_DATA_DIRS", users)])).vars,
            vars(&[("XDG_DATA_DIRS", "/usr/share:/opt/share")])
        );
        // Only XDG_DATA_DIRS gets the hook's entry.
        let other = shell_of(appimage(&[("LD_LIBRARY_PATH", "/tmp/.mount_x/usr/lib:/usr/share")]));
        assert_eq!(other.vars, vars(&[("LD_LIBRARY_PATH", "/usr/share")]));
    }

    #[test]
    fn a_lone_process_serial_number_is_a_finder_launch() {
        assert!(is_finder_launch(&["-psn_0_12345".into()]));
        assert!(!is_finder_launch(&["-psn_0_12345".into(), "list".into()]));
        assert!(!is_finder_launch(&["list".into()]));
        assert!(!is_finder_launch(&[]));
    }

    #[test]
    fn a_setting_in_the_bundle_is_unset_and_the_users_own_kept() {
        for name in APPIMAGE_PATHS {
            // The GTK hook writes `$APPDIR//usr/...`.
            for bundled in ["/tmp/.mount_x/usr", "/tmp/.mount_x//usr/lib", "/tmp/.mount_x"] {
                let shell = shell_of(appimage(&[(name, bundled)]));
                assert_eq!(shell.vars, vec![], "{name}={bundled}");
            }
            let shell = shell_of(appimage(&[(name, "/opt/python")]));
            assert_eq!(shell.vars, vars(&[(name, "/opt/python")]), "{name}");
        }
    }

    #[test]
    fn what_only_the_appimage_sets_is_unset() {
        for name in APPIMAGE_ONLY {
            let shell = shell_of(appimage(&[(name, "Adwaita:dark"), ("HOME", "/home/u")]));
            assert_eq!(shell.vars, vars(&[("HOME", "/home/u")]), "{name}");
        }
    }

    #[test]
    #[cfg(unix)]
    fn the_command_starts_in_the_users_shell() {
        let mut command = Command::new("sh");
        // HOME is the test's own, which the shell's environment leaves out.
        command.args(["-c", "pwd; echo \"$KEPT${HOME-unset}\""]);
        let shell = UserShell { vars: vars(&[("KEPT", "kept ")]), dir: Some("/".into()) };
        shell.apply(&mut command);
        let out = command.output().expect("sh runs");
        assert_eq!(String::from_utf8_lossy(&out.stdout), "/\nkept unset\n");
    }

    #[test]
    fn the_cli_refuses_a_directory_since_removed() {
        let Cli { orchestrator, command, .. } = cli(["list".into()]).expect("the CLI");
        let gone = Cli { orchestrator, command, dir: Some("/nonexistent/proj".into()) };
        assert_eq!(gone.why_not_started().as_deref(), Some("cannot start in /nonexistent/proj: it is not a directory"));
        let Cli { orchestrator, command, .. } = cli(["list".into()]).expect("the CLI");
        assert_eq!(Cli { orchestrator, command, dir: Some("/".into()) }.why_not_started(), None);
        let Cli { orchestrator, command, .. } = cli(["list".into()]).expect("the CLI");
        assert_eq!(Cli { orchestrator, command, dir: None }.why_not_started(), None);
    }

    #[test]
    #[cfg(unix)]
    fn a_directory_since_removed_leaves_the_apps_own() {
        let mut command = Command::new("pwd");
        let shell = UserShell { vars: vars(&[("PATH", "/usr/bin:/bin")]), dir: Some("/nonexistent/proj".into()) };
        shell.apply(&mut command);
        let out = command.output().expect("pwd runs");
        let own = std::env::current_dir().expect("a working directory");
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim_end(), own.to_str().unwrap());
    }
}
