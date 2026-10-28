//! The error that ends a run.

use std::fmt;

/// A failure that ends the run; the message is shown to the user, as orchestrator.py's OrchestratorError.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OrchestratorError(pub String);

impl OrchestratorError {
    pub fn new(message: impl Into<String>) -> Self {
        OrchestratorError(message.into())
    }
}

impl fmt::Display for OrchestratorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for OrchestratorError {}

/// What an I/O error says, without Rust's " (os error N)": Python's OSError.strerror, as the engine's messages quote
/// it.
pub fn strerror(e: &std::io::Error) -> String {
    let text = e.to_string();
    match text.rfind(" (os error ") {
        Some(i) if e.raw_os_error().is_some() => text[..i].to_string(),
        _ => text,
    }
}
