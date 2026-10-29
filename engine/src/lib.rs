//! The orchestrator's engine: the Rust port of orchestrator.py (the rust-engine spec), which reads and writes every
//! file, prompt and run state as the Python engine does.

pub mod py;
pub mod ci;
pub mod checks;
pub mod constants;
pub mod env;
pub mod error;
pub mod handoff;
pub mod harness;
pub mod herdr;
pub mod host;
pub mod http;
pub mod interrupt;
pub mod patch;
pub mod pipeline;
pub mod proofs;
pub mod prompts;
pub mod pull_request;
pub mod pypi;
pub mod quality;
pub mod runs;
pub mod spec_file;
pub mod state;
pub mod summary;
pub mod workflow;
pub mod workflow_file;

#[cfg(test)]
mod prompt_parity;
#[cfg(test)]
mod test_support;
