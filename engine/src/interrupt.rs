//! The human's Ctrl-C, as the engine sees it: Python's KeyboardInterrupt, which the Rust engine cannot raise inside
//! whatever happens to run, so it is a value that what waits checks instead, and that stops the commands the engine
//! is waiting on (crate::host::run_process) when it is raised.
//!
//! In Python every command the engine runs is in the terminal's foreground group and gets the Ctrl-C's SIGINT too.
//! The Rust engine runs each command in a process group of its own, which the terminal does not signal, so raising
//! the Interrupt is what reaches them: [`on_sigint`] has SIGINT raise it, from the terminal or from a kill of the
//! engine's group, such as the desktop app's Stop.

use std::collections::HashMap;
use std::sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

use nix::sys::signal::{SigSet, Signal};

/// The Ctrl-C a run may get. Raising it is the human's Ctrl-C: it is then *pending*, as Python's KeyboardInterrupt is
/// from its raise until a handler catches it. While it is pending, every command a Host or herdr waits on fails
/// (crate::host::run_process), one already running and one started meanwhile, and a sleep on it ends; the Workflow
/// stops at its next pause, and catches it (Interrupt::catch) to release the run. Clones share one Interrupt, so the
/// signal thread (on_sigint) or a test playing the human raises what a Workflow and its commands see.
#[derive(Clone, Debug, Default)]
pub struct Interrupt(Arc<Shared>);

#[derive(Debug, Default)]
struct Shared {
    state: Mutex<Raises>,
    /// Notified at each raise, for the sleeps.
    raised: Condvar,
}

#[derive(Default)]
struct Raises {
    /// How many times it has been raised.
    raised: u64,
    /// The value of raised when it was last caught: it is pending while raised is greater.
    caught: u64,
    /// What each command now waiting on it is told at a raise, by the key of the Watch that registered it.
    watchers: HashMap<u64, Box<dyn Fn() + Send>>,
    /// The key the next Watch registers under.
    next_watch: u64,
}

impl std::fmt::Debug for Raises {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Raises").field("raised", &self.raised).field("caught", &self.caught).finish_non_exhaustive()
    }
}

impl Interrupt {
    /// One not raised.
    pub fn new() -> Interrupt {
        Interrupt::default()
    }

    /// The human's Ctrl-C: makes it pending, ends every sleep on it and tells every command waiting on it to stop.
    /// Raised again, while pending or after it was caught, it does all that again.
    pub fn raise(&self) {
        let mut s = self.lock();
        s.raised += 1;
        for wake in s.watchers.values() {
            wake();
        }
        self.0.raised.notify_all();
    }

    /// Whether it is pending: raised since it was last caught.
    pub fn is_raised(&self) -> bool {
        let s = self.lock();
        s.raised > s.caught
    }

    /// Take it as handled, as Python's except KeyboardInterrupt does: no longer pending, so that the commands that
    /// record why the run stopped can run, until it is raised again.
    pub fn catch(&self) {
        let mut s = self.lock();
        s.caught = s.raised;
    }

    /// Wait for duration to pass, or less once it is pending: at once when it is pending already.
    pub fn sleep(&self, duration: Duration) {
        let deadline = Instant::now() + duration;
        let mut s = self.lock();
        while s.raised == s.caught {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return;
            }
            s = self.0.raised.wait_timeout(s, left).unwrap_or_else(|poisoned| poisoned.into_inner()).0;
        }
    }

    /// Have wake called at each raise from now until the Watch is dropped, for a command about to start: None, and
    /// nothing registered, when it is pending already, so that the command is not started.
    pub(crate) fn watch(&self, wake: impl Fn() + Send + 'static) -> Option<Watch<'_>> {
        let mut s = self.lock();
        if s.raised > s.caught {
            return None;
        }
        let key = s.next_watch;
        s.next_watch += 1;
        s.watchers.insert(key, Box::new(wake));
        let raised = s.raised;
        Some(Watch { interrupt: self, key, raised })
    }

    fn lock(&self) -> MutexGuard<'_, Raises> {
        // A poisoned lock only means a thread panicked while it held it; the counts are still whole.
        self.0.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

/// A command's registration with an Interrupt (Interrupt::watch), which ends when it is dropped.
pub(crate) struct Watch<'a> {
    interrupt: &'a Interrupt,
    /// Its key in Raises::watchers.
    key: u64,
    /// Raises::raised when it was registered.
    raised: u64,
}

impl Watch<'_> {
    /// End the registration; whether the Interrupt was raised while it lasted. A command that answered meanwhile is
    /// failed for it all the same, since what the engine would do with the answer comes after the Ctrl-C.
    pub(crate) fn interrupted(self) -> bool {
        self.interrupt.lock().raised != self.raised
    }
}

impl Drop for Watch<'_> {
    fn drop(&mut self) {
        self.interrupt.lock().watchers.remove(&self.key);
    }
}

/// The Interrupt this process's SIGINT raises, from the terminal's Ctrl-C or from any kill, such as the desktop app's
/// Stop, which signals the engine's process group: each SIGINT raises it, and none ends the process any more, so
/// whatever runs must check it. Every call gives the same one; the first blocks SIGINT in the calling thread, which
/// every thread started afterwards inherits, and starts the thread that waits for it (sigwait). So it must be called
/// on the main thread before any other thread starts: a thread that does not block SIGINT would get it and end the
/// process. The commands the engine starts get SIGINT unblocked (crate::host::run_process).
///
/// Where the process started with SIGINT ignored, as a Jenkins sh step's command or nohup's does, it stays ignored and
/// the Interrupt is never raised, as Python then never raises KeyboardInterrupt. That is read from Linux's
/// /proc/self/status, since Linux keeps an ignored signal for a thread that blocks it; where it cannot be read, as on
/// macOS, the kernel is taken to discard an ignored signal even while it is blocked, as the BSD kernels do.
///
/// Errors: SIGINT could not be blocked, or the thread could not be started.
pub fn on_sigint() -> std::io::Result<Interrupt> {
    static ONE: OnceLock<Interrupt> = OnceLock::new();
    // Whether the thread is waiting for SIGINT.
    static STARTED: Mutex<bool> = Mutex::new(false);
    let interrupt = ONE.get_or_init(Interrupt::new).clone();
    let mut started = STARTED.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    if *started || sigint_ignored() {
        return Ok(interrupt);
    }
    let mut sigint = SigSet::empty();
    sigint.add(Signal::SIGINT);
    sigint.thread_block()?;
    let raised = interrupt.clone();
    std::thread::Builder::new().name("sigint".to_string()).spawn(move || {
        // sigwait fails only for a set that holds no valid signal, which this one does.
        while sigint.wait().is_ok() {
            raised.raise();
        }
    })?;
    *started = true;
    Ok(interrupt)
}

/// Whether this process ignores SIGINT, as /proc/self/status's SigIgn mask says; false where that cannot be read.
fn sigint_ignored() -> bool {
    let Ok(status) = std::fs::read_to_string("/proc/self/status") else { return false };
    let mask = status.lines().find_map(|line| line.strip_prefix("SigIgn:")).map(str::trim);
    // Bit n-1 of the mask stands for signal n.
    mask.and_then(|hex| u64::from_str_radix(hex, 16).ok())
        .is_some_and(|mask| mask & (1 << (Signal::SIGINT as i32 - 1)) != 0)
}

#[cfg(test)]
mod tests;
