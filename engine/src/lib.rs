//! The orchestrator's engine: the Rust port of orchestrator.py (the rust-engine spec), which reads and writes every
//! file, prompt and run state as the Python engine does.

pub mod py;
pub mod constants;
pub mod env;
pub mod error;
pub mod handoff;
pub mod patch;
pub mod pipeline;
pub mod proofs;
pub mod prompts;
pub mod pull_request;
pub mod quality;
pub mod state;
pub mod summary;
pub mod workflow_file;

#[cfg(test)]
mod prompt_parity;
#[cfg(test)]
mod test_support;
