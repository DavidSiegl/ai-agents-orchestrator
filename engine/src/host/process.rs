//! The Host that runs real processes ([`ProcessHost`]), here or over SSH, and how one command is run: in a process
//! group of its own, its output read to the end, and the whole group killed when it runs out of time.

use std::ffi::OsString;
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use nix::sys::signal::{killpg, SigSet, Signal};
use nix::unistd::{getuid, Pid};

use super::{Exec, ExecError, Host};
use crate::error::{os_error, OrchestratorError};
use crate::interrupt::Interrupt;
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
/// A command fails as interrupted when the Interrupt it was given is raised while it runs (run_process).
///
/// Clones, and the hosts over_ssh makes, share their SSH control directory, which the last of them to be dropped
/// removes.
#[derive(Clone)]
pub struct ProcessHost {
    /// The SSH target its commands run on; None for this machine. Never empty.
    ssh_target: Option<String>,
    /// The environment every command it starts gets, the engine's own as child_env gives it
    /// (crate::env::child_env): without the CI credentials and git's pathspec switches.
    env: Arc<Vec<(OsString, OsString)>>,
    /// Runs one command, over SSH the ssh that carries it: run_process, stopped by the host's Interrupt.
    run: Arc<RunCommand>,
    /// This engine's SSH control sockets.
    ssh: Arc<Mutex<SshControl>>,
}

/// Runs one command as run_process does, given its argv, standard input, time limit and environment, and gives its
/// exit status and outputs, or why it gave none.
pub(crate) type RunCommand = dyn Fn(&[String], Option<&[u8]>, Option<Duration>, &[(OsString, OsString)]) -> Result<(i32, Vec<u8>, Vec<u8>), ProcessError>
    + Send
    + Sync;

impl ProcessHost {
    /// A Host that runs its commands on this machine, in the engine's environment as child_env gives it, each
    /// stopped by interrupt.
    pub fn local(interrupt: &Interrupt) -> ProcessHost {
        ProcessHost::in_environment(std::env::vars_os(), interrupt)
    }

    /// As local, in the environment vars as child_env would give it, were vars the engine's own: what the tests
    /// give a Host in place of the process's environment, which is shared by every test.
    pub(crate) fn in_environment(vars: impl Iterator<Item = (OsString, OsString)>, interrupt: &Interrupt) -> ProcessHost {
        let interrupt = interrupt.clone();
        ProcessHost::with_runner(vars, move |argv, stdin, timeout, env| run_process(argv, stdin, timeout, env, &interrupt))
    }

    /// As in_environment, each command run by run in place of a process, ssh included for a host over_ssh makes of
    /// it: Python's Host(run=...), for the tests.
    pub(crate) fn with_runner(
        vars: impl Iterator<Item = (OsString, OsString)>,
        run: impl Fn(&[String], Option<&[u8]>, Option<Duration>, &[(OsString, OsString)]) -> Result<(i32, Vec<u8>, Vec<u8>), ProcessError>
            + Send
            + Sync
            + 'static,
    ) -> ProcessHost {
        ProcessHost {
            ssh_target: None,
            env: Arc::new(crate::env::without_withheld(vars)),
            run: Arc::new(run),
            ssh: Arc::new(Mutex::new(SshControl::default())),
        }
    }

    /// A Host that runs its commands on ssh_target over SSH, starting them as this one does; on this machine when
    /// ssh_target is empty, as Python's Host takes an empty target.
    pub fn over_ssh(&self, ssh_target: &str) -> ProcessHost {
        ProcessHost { ssh_target: (!ssh_target.is_empty()).then(|| ssh_target.to_string()), ..self.clone() }
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

/// Where the host runs its commands, and nothing of their environment, whose values may be tokens of the user's.
impl std::fmt::Debug for ProcessHost {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ProcessHost").field("ssh_target", &self.ssh_target).finish_non_exhaustive()
    }
}

impl Host for ProcessHost {
    fn ssh_target(&self) -> Option<&str> {
        self.ssh_target.as_deref()
    }

    fn exec(&self, argv: &[&str], stdin: Option<&[u8]>, timeout: u64) -> Result<Exec, ExecError> {
        let argv: Vec<String> = match &self.ssh_target {
            Some(target) => ssh_argv(target, self.ssh_sharing_options(), argv),
            None => argv.iter().map(|a| a.to_string()).collect(),
        };
        let command = shlex_join(&argv);
        let failed = |why: String| ExecError::Failed(OrchestratorError(format!("{command}: {why}")));
        match (self.run)(&argv, stdin, Some(Duration::from_secs(timeout)), &self.env) {
            Ok((status, stdout, stderr)) => Ok(Exec { command, status, stdout, stderr }),
            Err(ProcessError::Start(e)) if e.kind() == ErrorKind::NotFound => {
                Err(ExecError::NotFound(OrchestratorError(format!("{command}: {}", os_error(&e, Some(&argv[0]))))))
            }
            Err(ProcessError::Start(e)) => Err(failed(os_error(&e, Some(&argv[0])))),
            Err(ProcessError::Io(e)) => Err(failed(os_error(&e, None))),
            Err(ProcessError::Timeout) => Err(failed(timed_out(&argv, timeout))),
            Err(ProcessError::Interrupted) => Err(failed("interrupted".to_string())),
        }
    }
}

/// The ssh command that runs argv on target, sharing a connection as options say (ssh_sharing_options).
fn ssh_argv(target: &str, options: Vec<String>, argv: &[&str]) -> Vec<String> {
    // BatchMode fails fast instead of hanging on a password prompt nobody can see.
    let batch = ["ssh", "-o", "BatchMode=yes"].map(String::from);
    batch.into_iter().chain(options).chain([target.to_string(), shlex_join(argv)]).collect()
}

/// What Python's subprocess.TimeoutExpired says of argv, run with a timeout of seconds.
fn timed_out(argv: &[String], seconds: u64) -> String {
    let listed: Vec<String> = argv.iter().map(|a| repr(a)).collect();
    format!("Command '[{}]' timed out after {seconds} seconds", listed.join(", "))
}

/// Why run_process gave no output.
#[derive(Debug)]
pub(crate) enum ProcessError {
    /// The command could not be started.
    Start(std::io::Error),
    /// Its input could not be written, its output read, or its exit waited for.
    Io(std::io::Error),
    /// It had not exited and closed its output when its time ran out; its process group was killed.
    Timeout,
    /// The Interrupt was raised while it ran: its process group was stopped (stop_group), whatever it had done or
    /// answered by then. Or it was pending already, and the command was not started.
    Interrupted,
}

/// How long a command has after the SIGINT of an Interrupt to exit before its process group is killed (SIGKILL), in
/// milliseconds: Python's subprocess waits as long (Popen._sigint_wait_secs) for a child to exit on the Ctrl-C it got
/// too before it kills it. The SIGINT first lets git remove its lock files, as it does at a Ctrl-C.
const INTERRUPT_GRACE_MS: u64 = 250;

/// argv's exit status, as Python's returncode gives it, and its standard output and error, once it has exited and
/// closed both: run with exactly env as its environment, argv[0] found on its PATH, stdin as its standard input (none
/// when None), in a process group of its own, so that when it has not finished within timeout, it and every process it
/// started that stayed in its group are killed (SIGKILL); a timeout of None lets it run as long as it takes. When
/// interrupt is raised while it runs, the command fails as Interrupted, its group sent SIGINT, and SIGKILL once it has
/// exited or INTERRUPT_GRACE_MS have passed (stop_group); while interrupt is pending, no command starts. A process that
/// left the group, as a daemon does, is not killed; if it holds the output open, the threads reading it end when it
/// closes it.
pub(crate) fn run_process(
    argv: &[String],
    stdin: Option<&[u8]>,
    timeout: Option<Duration>,
    env: &[(OsString, OsString)],
    interrupt: &Interrupt,
) -> Result<(i32, Vec<u8>, Vec<u8>), ProcessError> {
    let deadline = timeout.map(|timeout| Instant::now() + timeout);
    // Before the command starts, so that a raise between its start and the wait below still reaches it.
    let (sender, received) = mpsc::channel();
    let woken = sender.clone();
    let wake = move || {
        // As for the pipes' sends below.
        let _ = woken.send(Piped::Interrupted);
    };
    let Some(watch) = interrupt.watch(wake) else { return Err(ProcessError::Interrupted) };
    let mut command = Command::new(&argv[0]);
    command
        .args(&argv[1..])
        .env_clear()
        .envs(env.iter().map(|(k, v)| (k, v)))
        .stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    unblock_signals(&mut command);
    let mut child = command.spawn().map_err(ProcessError::Start)?;
    // Input and output each on a thread of their own, so that a command that writes much before it reads all its
    // input, or fills one pipe while the other is read, never waits on the engine, and the engine waits on none of
    // them past the deadline.
    let mut pending = 2;
    if let (Some(mut pipe), Some(data)) = (child.stdin.take(), stdin) {
        let (sender, data) = (sender.clone(), data.to_vec());
        pending += 1;
        thread::spawn(move || {
            let written = match pipe.write_all(&data) {
                // A command that stops reading its input is judged by its status and output, as Python's
                // communicate judges it.
                Err(e) if e.kind() == ErrorKind::BrokenPipe => Ok(()),
                done => done,
            };
            // The receiver is gone only once the command has failed, and then nothing waits for its input.
            let _ = sender.send(Piped::Input(written));
        });
    }
    let stdout: Box<dyn Read + Send> = Box::new(child.stdout.take().expect("stdout is piped"));
    let stderr: Box<dyn Read + Send> = Box::new(child.stderr.take().expect("stderr is piped"));
    for (n, mut pipe) in [stdout, stderr].into_iter().enumerate() {
        let sender = sender.clone();
        thread::spawn(move || {
            let mut out = vec![];
            let read = pipe.read_to_end(&mut out).map(|_| out);
            // As for the input's.
            let _ = sender.send(Piped::Output(n, read));
        });
    }
    drop(sender);
    let [stdout, stderr] = read_outputs(&mut child, &received, pending, deadline)?;
    let status = wait_for_exit(&mut child, &received, deadline)?;
    if watch.interrupted() {
        return Err(ProcessError::Interrupted);
    }
    let code = status.code().or_else(|| status.signal().map(|signal| -signal)).unwrap_or(-1);
    Ok((code, stdout, stderr))
}

/// child's standard output and error, each read to the end, once pending of its pipes have ended. received is what
/// run_process's pipe threads and its Interrupt's watch send on (Piped); the watch must outlive the call. When
/// deadline (None for none) passes first, or a pipe fails, fails as Timeout or Io with child's group killed and child
/// reaped (abandon); when the Interrupt comes first, fails as stop_group does.
fn read_outputs(
    child: &mut Child,
    received: &mpsc::Receiver<Piped>,
    mut pending: usize,
    deadline: Option<Instant>,
) -> Result<[Vec<u8>; 2], ProcessError> {
    let mut outputs: [Vec<u8>; 2] = [vec![], vec![]];
    while pending > 0 {
        let piped = match deadline {
            Some(deadline) => received.recv_timeout(deadline.saturating_duration_since(Instant::now())).ok(),
            None => received.recv().ok(),
        };
        // The watch's sender lasts as long as the watch, so a None is the deadline passing.
        let Some(piped) = piped else { return Err(abandon(child, ProcessError::Timeout)) };
        match piped {
            Piped::Output(n, Ok(out)) => outputs[n] = out,
            Piped::Output(_, Err(e)) | Piped::Input(Err(e)) => return Err(abandon(child, ProcessError::Io(e))),
            Piped::Input(Ok(())) => {}
            Piped::Interrupted => return Err(stop_group(child)),
        }
        pending -= 1;
    }
    Ok(outputs)
}

/// child's exit status, once it has exited and been reaped, for a command whose pipes have all ended; received is
/// what run_process's Interrupt's watch sends on. When deadline (None for none) passes first, or the wait fails,
/// fails as Timeout or Io with child's group killed and child reaped (abandon); when the Interrupt comes first, fails
/// as stop_group does.
fn wait_for_exit(
    child: &mut Child,
    received: &mpsc::Receiver<Piped>,
    deadline: Option<Instant>,
) -> Result<ExitStatus, ProcessError> {
    loop {
        // A command that closed its output and runs on is stopped by the interrupt as one that did not.
        if let Ok(Piped::Interrupted) = received.try_recv() {
            return Err(stop_group(child));
        }
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) if deadline.is_some_and(|deadline| Instant::now() >= deadline) => {
                return Err(abandon(child, ProcessError::Timeout))
            }
            Ok(None) => thread::sleep(Duration::from_millis(5)),
            Err(e) => return Err(abandon(child, ProcessError::Io(e))),
        }
    }
}

/// Have command start with SIGINT, SIGTERM and SIGHUP unblocked (crate::interrupt::SIGNALS). The engine blocks them
/// in every thread for the thread that waits for them (crate::interrupt::by_signals), a child inherits that across
/// exec, and std's Command leaves it so; unblocked, the command, and an agent or a herdr server it starts, hears a
/// Ctrl-C, a kill and a hangup as one started from a shell does. One the engine started with ignored stays ignored.
fn unblock_signals(command: &mut Command) {
    let signals: SigSet = crate::interrupt::SIGNALS.into_iter().collect();
    // SAFETY: the closure runs in the child between fork and exec, where only async-signal-safe functions may be
    // called; it calls pthread_sigmask alone, which is one (POSIX.1-2008, signal-safety(7)), on a set made before the
    // fork, and allocates nothing.
    #[allow(unsafe_code, reason = "CommandExt::pre_exec is unsafe, and nix has no safe way to start a child with a signal unblocked")]
    unsafe {
        command.pre_exec(move || signals.thread_unblock().map_err(std::io::Error::from));
    }
}

/// What one of a command's pipes ended with: its input written (Input), or one of its outputs read to the end, by
/// number, 0 for stdout and 1 for stderr (Output); or the Interrupt was raised (Interrupted).
enum Piped {
    Input(std::io::Result<()>),
    Output(usize, std::io::Result<Vec<u8>>),
    Interrupted,
}

/// Stop child's process group for an Interrupt, child included, and reap child: SIGINT, then SIGKILL to what is left
/// of the group once child has exited, or INTERRUPT_GRACE_MS have passed. Gives Interrupted, or the error that stopped
/// the signals or the wait.
fn stop_group(child: &mut Child) -> ProcessError {
    if let Err(e) = kill_group(child, Signal::SIGINT) {
        return abandon(child, ProcessError::Io(e));
    }
    let grace = Instant::now() + Duration::from_millis(INTERRUPT_GRACE_MS);
    loop {
        match exited(child) {
            Ok(false) if Instant::now() < grace => thread::sleep(Duration::from_millis(5)),
            Ok(_) => break,
            Err(e) => return abandon(child, ProcessError::Io(e)),
        }
    }
    // Killed while child is not reaped, so that its pid, the group's id, names no other group.
    abandon(child, ProcessError::Interrupted)
}

/// Whether child has exited, without reaping it (waitid's WNOWAIT), so that its pid still names its group.
#[cfg(target_os = "linux")]
fn exited(child: &Child) -> std::io::Result<bool> {
    use nix::sys::wait::{waitid, Id, WaitPidFlag, WaitStatus};
    let pid = Pid::from_raw(i32::try_from(child.id()).expect("a pid fits an i32"));
    let flags = WaitPidFlag::WEXITED | WaitPidFlag::WNOHANG | WaitPidFlag::WNOWAIT;
    Ok(!matches!(waitid(Id::Pid(pid), flags)?, WaitStatus::StillAlive))
}

/// Never known before the grace ends where nix has no waitid, as on macOS: the group then has the whole grace.
#[cfg(not(target_os = "linux"))]
fn exited(_child: &Child) -> std::io::Result<bool> {
    Ok(false)
}

/// Kill child's process group, child included, and reap child, for a command that failed with why: why, or the error
/// that stopped the kill.
fn abandon(child: &mut Child, why: ProcessError) -> ProcessError {
    if let Err(e) = kill_group(child, Signal::SIGKILL) {
        return ProcessError::Io(e);
    }
    match child.wait() {
        Ok(_) => why,
        Err(e) => ProcessError::Io(e),
    }
}

/// Send signal to the process group child leads, which child must not have been reaped from yet; a group with no
/// process left to signal is no error.
fn kill_group(child: &Child, signal: Signal) -> std::io::Result<()> {
    let group = Pid::from_raw(i32::try_from(child.id()).expect("a pid fits an i32"));
    // ESRCH: every process of the group has exited. EPERM: macOS refuses a group of zombies.
    match killpg(group, signal) {
        Ok(()) | Err(nix::errno::Errno::ESRCH | nix::errno::Errno::EPERM) => Ok(()),
        Err(e) => Err(e.into()),
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
