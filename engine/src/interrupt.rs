//! The human's Ctrl-C, as the engine sees it: Python's KeyboardInterrupt, which the Rust engine cannot raise inside
//! whatever happens to run, so it is a flag that what waits checks instead.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// Whether the human has asked the engine to stop. Raised once, it stays raised. Clones share one flag, so the signal
/// handling (or a test playing the human) raises what a Workflow checks at each of its pauses.
#[derive(Clone, Debug, Default)]
pub struct Interrupt(Arc<AtomicBool>);

impl Interrupt {
    /// One not raised.
    pub fn new() -> Interrupt {
        Interrupt::default()
    }

    /// Ask the engine to stop.
    pub fn raise(&self) {
        self.0.store(true, Ordering::SeqCst);
    }

    /// Whether it has been asked to stop.
    pub fn is_raised(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }
}
