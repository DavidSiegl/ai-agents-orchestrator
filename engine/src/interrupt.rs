//! The human's Ctrl-C, as the engine sees it: Python's KeyboardInterrupt, which the Rust engine cannot raise inside
//! whatever happens to run, so it is a value that what waits checks instead, and that stops the commands the engine
//! is waiting on (crate::host::run_process) when it is raised.
//!
//! In Python every command the engine runs is in the terminal's foreground group and gets the Ctrl-C's SIGINT too, and
//! the SIGHUP of a closed terminal. The Rust engine runs each command in a process group of its own, which the
//! terminal does not signal, so raising the Interrupt is what reaches them: [`by_signals`] has SIGINT raise it, from
//! the terminal or from a kill of the engine's group, such as the desktop app's Stop, and SIGTERM and SIGHUP too, which
//! end the Python engine at once, so that no command outlives the engine; [`exit`] then ends the process by the
//! signal, as they end Python's.

use std::collections::HashMap;
use std::sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

use nix::sys::signal::{SigSet, Signal};

/// The Ctrl-C a run may get. Raising it is the human's Ctrl-C: it is then *pending*, as Python's KeyboardInterrupt is
/// from its raise until a handler catches it. While it is pending, every command a Host or herdr waits on fails
/// (crate::host::run_process), one already running and one started meanwhile, and a sleep on it ends; the Workflow
/// stops at its next pause, and catches it (Interrupt::catch) to release the run. Clones share one Interrupt, so the
/// signal thread (by_signals) or a test playing the human raises what a Workflow and its commands see.
#[derive(Clone, Debug, Default)]
pub struct Interrupt(Arc<Shared>);

/// What the clones of one Interrupt share.
#[derive(Debug, Default)]
struct Shared {
    /// Its raises and the commands watching it.
    state: Mutex<Raises>,
    /// Notified at each raise, under state's lock, for the sleeps waiting on it.
    raised: Condvar,
}

/// An Interrupt's history, from which whether it is pending follows, and who is to hear of its next raise.
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

    /// Run f as Python runs a finally block that a KeyboardInterrupt passes through: f's commands start, as if it
    /// were caught, and afterwards it is pending again if it was before f or was raised during it.
    pub fn finally<T>(&self, f: impl FnOnce() -> T) -> T {
        let held = {
            let mut s = self.lock();
            let held = s.caught;
            s.caught = s.raised;
            held
        };
        let done = f();
        self.lock().caught = held;
        done
    }

    /// Wait for duration to pass, or less once it is pending: at once when it is pending already. A duration too
    /// long for the clock to reach waits until it is raised.
    pub fn sleep(&self, duration: Duration) {
        let deadline = Instant::now().checked_add(duration);
        let mut s = self.lock();
        while s.raised == s.caught {
            s = match deadline {
                Some(deadline) => {
                    let left = deadline.saturating_duration_since(Instant::now());
                    if left.is_zero() {
                        return;
                    }
                    self.0.raised.wait_timeout(s, left).unwrap_or_else(|poisoned| poisoned.into_inner()).0
                }
                None => self.0.raised.wait(s).unwrap_or_else(|poisoned| poisoned.into_inner()),
            };
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

/// The signals that raise by_signals' Interrupt: the Ctrl-C's, a kill's default and a closed terminal's.
pub(crate) const SIGNALS: [Signal; 3] = [Signal::SIGINT, Signal::SIGTERM, Signal::SIGHUP];

/// The first SIGTERM or SIGHUP the signal thread took, which exit ends the process by; unset while none came.
static ENDING: OnceLock<Signal> = OnceLock::new();

/// The Interrupt this process's SIGINT, SIGTERM and SIGHUP raise (SIGNALS): the terminal's Ctrl-C, the desktop app's
/// Stop, which sends the engine's process group SIGINT, a kill, and a closed terminal. Each such signal raises it, and
/// none ends the process any more, so whatever runs must check it, and the program must end by [`exit`], which ends
/// it by the first SIGTERM or SIGHUP. Every call gives the same one; the first blocks the signals in the calling
/// thread, which every thread started afterwards inherits, and starts the thread that waits for them (sigwait). So it
/// must be called on the main thread before any other thread starts: a thread that does not block them would get one
/// and end the process. The commands the engine starts get them unblocked (crate::host::run_process).
///
/// A signal the process started with ignored, as a Jenkins sh step's command ignores SIGINT and nohup's SIGHUP, stays
/// ignored and never raises it, as Python then never raises KeyboardInterrupt, and nohup's Python is not hung up.
/// That is read from Linux's /proc/self/status, since Linux keeps an ignored signal for a thread that blocks it; where
/// it cannot be read, as on macOS, the kernel is taken to discard an ignored signal even while it is blocked, as the
/// BSD kernels do.
///
/// Errors: the signals could not be blocked, or the thread could not be started.
pub fn by_signals() -> std::io::Result<Interrupt> {
    static ONE: OnceLock<Interrupt> = OnceLock::new();
    // Whether the thread is waiting for the signals.
    static STARTED: Mutex<bool> = Mutex::new(false);
    let interrupt = ONE.get_or_init(Interrupt::new).clone();
    let mut started = STARTED.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    if *started {
        return Ok(interrupt);
    }
    let ignored = ignored_signals();
    let waited: Vec<Signal> = SIGNALS.into_iter().filter(|signal| !ignored.contains(signal)).collect();
    if waited.is_empty() {
        return Ok(interrupt);
    }
    let waited: SigSet = waited.into_iter().collect();
    waited.thread_block()?;
    let raised = interrupt.clone();
    std::thread::Builder::new().name("signals".to_string()).spawn(move || {
        // sigwait fails only for a set that holds no valid signal, which this one does.
        while let Ok(signal) = waited.wait() {
            if signal != Signal::SIGINT {
                // The first stays: a later one changes nothing about how the process ends.
                let _ = ENDING.set(signal);
            }
            raised.raise();
        }
    })?;
    *started = true;
    Ok(interrupt)
}

/// End the process with status, as std::process::exit does; but once a SIGTERM or SIGHUP has raised by_signals'
/// Interrupt, by the first of them, as its default action ends a process, so that its parent sees the signal's death
/// (a shell's $? 128 plus its number, 143 and 129), as the Python engine dies by it. stdout is flushed first, as at
/// the end of main.
pub fn exit(status: i32) -> ! {
    use std::io::Write;
    // Nowhere to say that stdout did not take its last line.
    let _ = std::io::stdout().flush();
    if let Some(&signal) = ENDING.get() {
        // Blocked in every thread, and at its default, since by_signals waits only for a signal not ignored at start
        // and sets no handler: unblocked here, raising it ends the process before raise returns.
        let _ = SigSet::from(signal).thread_unblock();
        let _ = nix::sys::signal::raise(signal);
        // Reached only if raise failed: the status a shell gives that death.
        std::process::exit(128 + signal as i32);
    }
    std::process::exit(status)
}

/// The signals among SIGNALS this process ignores, as /proc/self/status's SigIgn mask says; none where that cannot be
/// read.
fn ignored_signals() -> Vec<Signal> {
    let Ok(status) = std::fs::read_to_string("/proc/self/status") else { return vec![] };
    let mask = status.lines().find_map(|line| line.strip_prefix("SigIgn:")).map(str::trim);
    let Some(mask) = mask.and_then(|hex| u64::from_str_radix(hex, 16).ok()) else { return vec![] };
    // Bit n-1 of the mask stands for signal n.
    SIGNALS.into_iter().filter(|&signal| mask & (1 << (signal as i32 - 1)) != 0).collect()
}

#[cfg(test)]
mod tests;
