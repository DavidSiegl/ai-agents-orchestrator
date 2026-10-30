//! The e2e scenarios' harness: the orchestrator program run as a program in a world of fakes (world), the fakes it
//! talks to in place of herdr, gh and ssh (fakes) and their shared ledger (ledger), what of a run differs from run to
//! run (normalize), the machine's processes (procs), JSON as Python wrote the recordings (pyjson), and the comparison with them (check), and the directories they read and clear (dirs). The scenarios
//! themselves are tests/scenarios.rs. Linux only: the fakes find their engine's process through /proc.

pub mod check;
pub mod dirs;
pub mod fakes;
pub mod ledger;
pub mod normalize;
pub mod procs;
pub mod pyjson;
pub mod world;
