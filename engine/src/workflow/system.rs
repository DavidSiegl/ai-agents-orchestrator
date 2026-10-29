//! The Clocks and the Human of a real run on this machine, which the CLI gives a Workflow: Python's default Clocks
//! (time.sleep, time.monotonic, time.time), notify_locally and log.

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use nix::time::{clock_gettime, ClockId};

use super::{Clocks, Human};
use crate::error::OrchestratorError;
use crate::herdr::ProcessHerdr;
use crate::interrupt::Interrupt;

/// The system's clocks, whose sleep the Ctrl-C cuts short.
#[derive(Clone, Debug)]
pub struct SystemClocks {
    /// What cuts a sleep short.
    interrupt: Interrupt,
    /// When the clocks were made, as an Instant and on CLOCK_MONOTONIC in seconds: monotonic reads the system's
    /// monotonic clock, as Python's time.monotonic does, from Instant, which never fails.
    made: (Instant, f64),
}

impl SystemClocks {
    /// The system's clocks, a sleep ending early once interrupt is pending (Interrupt::sleep).
    ///
    /// Errors: the system's monotonic clock cannot be read.
    pub fn new(interrupt: &Interrupt) -> Result<SystemClocks, OrchestratorError> {
        let now = clock_gettime(ClockId::CLOCK_MONOTONIC)
            .map_err(|e| OrchestratorError(format!("cannot read the monotonic clock: {e}")))?;
        let seconds = now.tv_sec() as f64 + now.tv_nsec() as f64 / 1e9;
        Ok(SystemClocks { interrupt: interrupt.clone(), made: (Instant::now(), seconds) })
    }
}

impl Clocks for SystemClocks {
    /// A time below zero or not finite sleeps not at all.
    fn sleep(&self, seconds: f64) {
        self.interrupt.sleep(Duration::try_from_secs_f64(seconds).unwrap_or(Duration::ZERO));
    }

    /// Seconds since a point the system chooses, such as its boot, as Python's time.monotonic counts them.
    fn monotonic(&self) -> f64 {
        self.made.1 + self.made.0.elapsed().as_secs_f64()
    }

    fn wall(&self) -> f64 {
        match SystemTime::now().duration_since(UNIX_EPOCH) {
            Ok(since) => since.as_secs_f64(),
            Err(before) => -before.duration().as_secs_f64(),
        }
    }
}

/// The human at this machine: notified through the herdr the orchestrator runs beside, where they are watching, never
/// the run's --machine, and told everything else on stderr, each line indented by two spaces. Python's notify_locally
/// and log.
#[derive(Clone, Debug)]
pub struct LocalHuman {
    /// The herdr on this machine.
    herdr: ProcessHerdr,
}

impl LocalHuman {
    /// The human, whose notifications interrupt stops as it stops every herdr command.
    pub fn new(interrupt: &Interrupt) -> LocalHuman {
        LocalHuman { herdr: ProcessHerdr::new(None, interrupt) }
    }
}

impl Human for LocalHuman {
    /// A notification herdr fails to show costs only itself, and is logged.
    fn notify(&self, title: &str, body: &str) {
        if let Err(e) = self.herdr.notify(title, body) {
            self.log(&format!("notification failed: {e}"));
        }
    }

    /// A line stderr does not take is lost: there is nowhere else to tell the human, and a run is not stopped for it.
    fn log(&self, line: &str) {
        use std::io::Write;
        let _ = writeln!(std::io::stderr(), "  {line}");
    }
}

#[cfg(test)]
mod tests;
