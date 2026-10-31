//! The commands the chores start, the human's Ctrl-C while they run, and the temporary directories they work in.
//!
//! A Ctrl-C reaches every process in the terminal's foreground group: the runner, and the engine it waits for. The
//! engine saves its run as it stops, so the runner must outlive it: [`watch_interrupts`] blocks SIGINT in the runner,
//! where a thread waits for it and records it ([`interrupted`]), and [`command`] unblocks it in every command the
//! runner starts, since a child inherits a blocked signal across exec.

use std::fs;
use std::io::{self, ErrorKind};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use nix::sys::signal::{SigSet, Signal};

/// true means a SIGINT arrived since watch_interrupts started waiting for one.
static INTERRUPTED: AtomicBool = AtomicBool::new(false);

/// From now on, a SIGINT no longer ends this process but makes [`interrupted`] true. Call it before starting any
/// other thread, so that every thread has SIGINT blocked. Errors: SIGINT could not be blocked, or the thread that
/// waits for it could not be started.
pub(crate) fn watch_interrupts() -> io::Result<()> {
    let sigint = SigSet::from(Signal::SIGINT);
    sigint.thread_block()?;
    std::thread::Builder::new().name("sigint".to_string()).spawn(move || {
        // sigwait fails only for a set that holds no valid signal, which this one does.
        while sigint.wait().is_ok() {
            INTERRUPTED.store(true, Ordering::SeqCst);
        }
    })?;
    Ok(())
}

/// Whether a SIGINT has arrived since [`watch_interrupts`].
pub(crate) fn interrupted() -> bool {
    INTERRUPTED.load(Ordering::SeqCst)
}

/// A Command for program that starts with SIGINT unblocked, so that it hears a Ctrl-C as one started from a shell
/// does, whether or not [`watch_interrupts`] blocked it here.
pub(crate) fn command(program: impl AsRef<std::ffi::OsStr>) -> Command {
    use std::os::unix::process::CommandExt;
    let mut command = Command::new(program);
    let sigint = SigSet::from(Signal::SIGINT);
    // SAFETY: the closure runs in the child between fork and exec, where only async-signal-safe functions may be
    // called; it calls pthread_sigmask alone, which is one (POSIX.1-2008, signal-safety(7)), on a set made before the
    // fork, and allocates nothing. As engine/src/host/process.rs's unblock_signals.
    #[allow(
        unsafe_code,
        reason = "CommandExt::pre_exec is unsafe, and nix has no safe way to start a child with a signal unblocked"
    )]
    unsafe {
        command.pre_exec(move || sigint.thread_unblock().map_err(io::Error::from));
    }
    command
}

/// A new, empty directory under the system's temporary directory (TMPDIR's, else /tmp), named prefix followed by
/// what makes it unique. The caller removes it. Errors: it could not be made.
pub(crate) fn make_temp_dir(prefix: &str) -> io::Result<PathBuf> {
    static MADE: AtomicU64 = AtomicU64::new(0);
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.subsec_nanos());
    loop {
        let n = MADE.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("{prefix}{}-{nanos}-{n}", std::process::id()));
        match fs::create_dir(&dir) {
            Err(e) if e.kind() == ErrorKind::AlreadyExists => continue,
            made => return made.map(|()| dir),
        }
    }
}

/// A directory of [`make_temp_dir`]'s, removed with everything in it when dropped; a removal that fails is reported
/// on standard error.
pub struct TempDir(PathBuf);

impl TempDir {
    /// A new, empty one, named prefix and what makes it unique. Errors: it could not be made.
    pub fn new(prefix: &str) -> io::Result<TempDir> {
        make_temp_dir(prefix).map(TempDir)
    }

    pub fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        if let Err(e) = fs::remove_dir_all(&self.0) {
            eprintln!("warning: could not remove {}: {e}", self.0.display());
        }
    }
}
