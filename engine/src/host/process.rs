//! The Host that runs real processes ([`ProcessHost`]), here or over SSH, and how one command is run: in a process
//! group of its own, its output read to the end, and the whole group killed when it runs out of time.

use std::ffi::OsString;
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use nix::sys::signal::{killpg, Signal};
use nix::unistd::{getuid, Pid};

use super::{Exec, Host};
use crate::error::{os_error, OrchestratorError};
use crate::py::text::{is_space, repr, shlex_join};

/// How long, in seconds, an SSH master connection (ssh_sharing_options) outlives the last command that used it.
pub const SSH_CONTROL_PERSIST_SECONDS: u64 = 60;
/// How often, in seconds, an SSH master asks its server whether the connection is alive, and how many asks left
/// unanswered end it: so one that died without a reset ends within 30 s, and the next command opens a new one, rather
/// than waiting on it until TCP gives up, while the commands keep it from ever idling out.
pub const SSH_ALIVE_INTERVAL_SECONDS: u64 = 10;
pub const SSH_ALIVE_COUNT: u64 = 3;
/// The longest control socket path ssh can bind, in bytes: a Unix socket's path is at most 103 on macOS (sun_path holds
/// 104 with its NUL; 108 on Linux), and ssh binds a temporary name 17 bytes longer before renaming it.
const SSH_SOCKET_PATH_MAX: usize = 103 - 17;
/// The name of a control directory: this prefix, then 8 characters of its own, as mkdtemp names one.
const SSH_DIR_PREFIX: &str = "octoflow-ssh-";

/// The machine the agents run on, reached by running real processes: this machine, or one over SSH
/// (`ssh -o BatchMode=yes`), all of whose commands to one target share a single connection (ssh_sharing_options).
///
/// Clones, and the hosts over_ssh makes, share their SSH control directory, which the last of them to be dropped
/// removes.
#[derive(Clone, Debug)]
pub struct ProcessHost {
    /// The SSH target its commands run on; None for this machine.
    ssh_target: Option<String>,
    /// The environment every command it starts gets, the engine's own as child_env gives it
    /// (crate::env::child_env): without the CI credentials and git's pathspec switches.
    env: Arc<Vec<(OsString, OsString)>>,
    /// This engine's SSH control sockets.
    ssh: Arc<Mutex<SshControl>>,
}

impl ProcessHost {
    /// A Host that runs its commands on this machine, in the engine's environment as child_env gives it.
    pub fn local() -> ProcessHost {
        ProcessHost::in_environment(std::env::vars_os())
    }

    /// A Host that runs its commands on this machine, in the environment vars as child_env would give it, were vars
    /// the engine's own: what the tests give a Host in place of the process's environment, which is shared by every
    /// test.
    pub(crate) fn in_environment(vars: impl Iterator<Item = (OsString, OsString)>) -> ProcessHost {
        ProcessHost {
            ssh_target: None,
            env: Arc::new(crate::env::without_withheld(vars)),
            ssh: Arc::new(Mutex::new(SshControl::default())),
        }
    }

    /// A Host that runs its commands on ssh_target over SSH, starting them as this one does.
    pub fn over_ssh(&self, ssh_target: &str) -> ProcessHost {
        ProcessHost { ssh_target: Some(ssh_target.to_string()), ..self.clone() }
    }

    /// The value of the variable name in the environment the commands get; None when it is unset or not UTF-8.
    fn var(&self, name: &str) -> Option<String> {
        self.env.iter().rev().find(|(k, _)| k == name).and_then(|(_, v)| v.to_str()).map(String::from)
    }

    /// The ssh options that let this host's commands to one target share a single connection (ssh_config(5),
    /// ControlMaster), which ends SSH_CONTROL_PERSIST_SECONDS after its last command, or once it stops answering
    /// (SSH_ALIVE_INTERVAL_SECONDS). Its socket is in a directory only the user can use, under $XDG_RUNTIME_DIR, else
    /// /tmp, made on the first call, made again when it is gone or no longer the user's alone, and removed when the
    /// last host that shares it is dropped.
    ///
    /// Empty when no such directory with a socket path ssh accepts can be made: ssh would fail the command, not
    /// connect on its own (ssh_config(5), ControlPath), so the command runs unshared.
    pub(super) fn ssh_sharing_options(&self) -> Vec<String> {
        // A poisoned lock only means another thread panicked while it held it; the control is still whole.
        let mut control = self.ssh.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if control.dir.as_deref().is_some_and(private_dir) {
            return control.options.clone();
        }
        control.dir = None;
        control.options = vec![];
        // /tmp rather than the system's temporary directory, whose path on macOS leaves no room for the socket's;
        // the directory there is private and unguessable, and private_dir sees that it stays so.
        for parent in [self.var("XDG_RUNTIME_DIR"), Some("/tmp".to_string())].into_iter().flatten() {
            if let Some(made) = new_ssh_control_dir(&parent) {
                control.options = [
                    "ControlMaster=auto".to_string(),
                    format!("ControlPersist={SSH_CONTROL_PERSIST_SECONDS}"),
                    format!("ControlPath={made}/%C"),
                    format!("ServerAliveInterval={SSH_ALIVE_INTERVAL_SECONDS}"),
                    format!("ServerAliveCountMax={SSH_ALIVE_COUNT}"),
                ]
                .into_iter()
                .flat_map(|option| ["-o".to_string(), option])
                .collect();
                control.dir = Some(made);
                break;
            }
        }
        control.options.clone()
    }
}

impl Host for ProcessHost {
    fn ssh_target(&self) -> Option<&str> {
        self.ssh_target.as_deref()
    }

    fn exec(&self, argv: &[&str], stdin: Option<&[u8]>, timeout: u64) -> Result<Exec, OrchestratorError> {
        let argv: Vec<String> = match &self.ssh_target {
            // BatchMode fails fast instead of hanging on a password prompt nobody can see.
            Some(target) => ["ssh", "-o", "BatchMode=yes"]
                .map(String::from)
                .into_iter()
                .chain(self.ssh_sharing_options())
                .chain([target.clone(), shlex_join(argv)])
                .collect(),
            None => argv.iter().map(|a| a.to_string()).collect(),
        };
        let command = shlex_join(&argv);
        match run_process(&argv, stdin, Duration::from_secs(timeout), &self.env) {
            Ok((status, stdout, stderr)) => Ok(Exec { command, status, stdout, stderr }),
            Err(ProcessError::Start(e)) => {
                Err(OrchestratorError(format!("{command}: {}", os_error(&e, Some(&argv[0])))))
            }
            Err(ProcessError::Io(e)) => Err(OrchestratorError(format!("{command}: {}", os_error(&e, None)))),
            Err(ProcessError::Timeout) => Err(OrchestratorError(format!("{command}: {}", timed_out(&argv, timeout)))),
        }
    }
}

/// What Python's subprocess.TimeoutExpired says of argv, run with a timeout of seconds.
fn timed_out(argv: &[String], seconds: u64) -> String {
    let listed: Vec<String> = argv.iter().map(|a| repr(a)).collect();
    format!("Command '[{}]' timed out after {seconds} seconds", listed.join(", "))
}

/// Why run_process gave no output.
#[derive(Debug)]
enum ProcessError {
    /// The command could not be started.
    Start(std::io::Error),
    /// Its input could not be written, or its output read.
    Io(std::io::Error),
    /// It had not exited and closed its output when its time ran out; its process group was killed.
    Timeout,
}

/// argv's exit status, as Python's returncode gives it, and its standard output and error, once it has exited and
/// closed both: run with exactly env as its environment, stdin as its standard input (none when None), in a process
/// group of its own, so that when it has not finished within timeout, it and every process it started that stayed in
/// its group are killed (SIGKILL). A process that left the group, as a daemon does, is not; if it holds the output
/// open, the threads reading it end when it closes it.
fn run_process(
    argv: &[String],
    stdin: Option<&[u8]>,
    timeout: Duration,
    env: &[(OsString, OsString)],
) -> Result<(i32, Vec<u8>, Vec<u8>), ProcessError> {
    let deadline = Instant::now() + timeout;
    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .env_clear()
        .envs(env.iter().map(|(k, v)| (k, v)))
        .stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
        .map_err(ProcessError::Start)?;
    // Input and output each on a thread of their own, so that a command that writes much before it reads all its
    // input, or fills one pipe while the other is read, never waits on the engine.
    let writer = match (child.stdin.take(), stdin) {
        (Some(mut pipe), Some(data)) => {
            let data = data.to_vec();
            Some(thread::spawn(move || match pipe.write_all(&data) {
                // A command that stops reading its input is judged by its status and output, as Python's
                // communicate judges it.
                Err(e) if e.kind() == ErrorKind::BrokenPipe => Ok(()),
                done => done,
            }))
        }
        _ => None,
    };
    let (sender, received) = mpsc::channel();
    let stdout: Box<dyn Read + Send> = Box::new(child.stdout.take().expect("stdout is piped"));
    let stderr: Box<dyn Read + Send> = Box::new(child.stderr.take().expect("stderr is piped"));
    for (n, mut pipe) in [stdout, stderr].into_iter().enumerate() {
        let sender = sender.clone();
        thread::spawn(move || {
            let mut out = vec![];
            let read = pipe.read_to_end(&mut out).map(|_| out);
            // The receiver is gone only once the command timed out, and then nothing reads what was printed.
            let _ = sender.send((n, read));
        });
    }
    drop(sender);
    let mut outputs: [Option<Vec<u8>>; 2] = [None, None];
    for _ in 0..2 {
        let Ok((n, read)) = received.recv_timeout(deadline.saturating_duration_since(Instant::now())) else {
            return Err(kill_group(&mut child));
        };
        outputs[n] = Some(read.map_err(ProcessError::Io)?);
    }
    let status = loop {
        if let Some(status) = child.try_wait().map_err(ProcessError::Io)? {
            break status;
        }
        if Instant::now() >= deadline {
            return Err(kill_group(&mut child));
        }
        thread::sleep(Duration::from_millis(5));
    };
    if let Some(writer) = writer {
        writer.join().expect("the input's thread does not panic").map_err(ProcessError::Io)?;
    }
    let code = status.code().or_else(|| status.signal().map(|signal| -signal)).unwrap_or(-1);
    let [stdout, stderr] = outputs.map(Option::unwrap_or_default);
    Ok((code, stdout, stderr))
}

/// Kill child's process group, child included, and reap child: what a command that ran out of time gets.
fn kill_group(child: &mut Child) -> ProcessError {
    let group = Pid::from_raw(i32::try_from(child.id()).expect("a pid fits an i32"));
    // ESRCH: every process of the group has exited. EPERM: macOS refuses a group of zombies.
    match killpg(group, Signal::SIGKILL) {
        Ok(()) | Err(nix::errno::Errno::ESRCH | nix::errno::Errno::EPERM) => {}
        Err(e) => return ProcessError::Io(e.into()),
    }
    match child.wait() {
        Ok(_) => ProcessError::Timeout,
        Err(e) => ProcessError::Io(e),
    }
}

/// An engine's SSH control sockets, for ssh_sharing_options.
#[derive(Debug, Default)]
struct SshControl {
    /// The directory, made by this engine, they are in; None while there is none.
    dir: Option<String>,
    /// ssh_sharing_options' answer while dir is that directory.
    options: Vec<String>,
}

impl Drop for SshControl {
    fn drop(&mut self) {
        if let Some(dir) = &self.dir {
            // As Python's atexit rmtree(ignore_errors=True): a directory something else removed is no failure, and
            // nothing is left to report another to.
            let _ = std::fs::remove_dir_all(dir);
        }
    }
}

/// A new directory for SSH control sockets in parent, only the user's; None when parent would give a socket path ssh
/// rejects, too long or with a character its option parser takes apart, or the directory cannot be made.
fn new_ssh_control_dir(parent: &str) -> Option<String> {
    if parent.is_empty() || parent.chars().any(|c| is_space(c) || "%\"'\\".contains(c)) {
        return None;
    }
    // The socket's path: parent, the directory's name, and %C, the hash of the connection's endpoints, 40 hex digits.
    if parent.len() + 1 + SSH_DIR_PREFIX.len() + 8 + 1 + 40 > SSH_SOCKET_PATH_MAX {
        return None;
    }
    // As mkdtemp: a name no one can guess, made only if nothing has it yet, tried again a few times if something does.
    for attempt in 0..100u64 {
        let path = format!("{parent}/{SSH_DIR_PREFIX}{}", unguessable_name(attempt));
        match std::fs::DirBuilder::new().mode(0o700).create(&path) {
            Ok(()) => return Some(path),
            Err(e) if e.kind() == ErrorKind::AlreadyExists => continue,
            Err(_) => return None,
        }
    }
    None
}

/// 8 characters of [a-z0-9_] that differ from call to call and cannot be guessed from outside the process: from the
/// random keys std's hash maps are seeded with.
fn unguessable_name(salt: u64) -> String {
    use std::hash::{BuildHasher, Hasher};
    const LETTERS: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789_";
    let mut hasher = std::collections::hash_map::RandomState::new().build_hasher();
    hasher.write_u64(salt);
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    hasher.write_u128(now.as_nanos());
    let mut n = hasher.finish();
    (0..8)
        .map(|_| {
            let c = LETTERS[(n % LETTERS.len() as u64) as usize];
            n /= LETTERS.len() as u64;
            char::from(c)
        })
        .collect()
}

/// Whether path is still a directory, not a symlink, that is the user's and no one else's (mode 0700): one this engine
/// made may have been removed, and in /tmp another user, who sees its name in ps, may make one so named.
fn private_dir(path: &str) -> bool {
    match std::fs::symlink_metadata(path) {
        Ok(meta) => meta.is_dir() && meta.uid() == getuid().as_raw() && meta.permissions().mode() & 0o7777 == 0o700,
        Err(_) => false,
    }
}

#[cfg(test)]
mod tests;
