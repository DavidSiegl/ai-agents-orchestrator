//! The orchestrator program: crate::cli::main, once the signals' thread runs, ending as interrupt::exit ends it.

use octoflow_engine::constants::EXIT_ERROR;
use octoflow_engine::interrupt::{by_signals, exit};

fn main() {
    // First, on the main thread, before any other thread starts (by_signals says why).
    let interrupt = match by_signals() {
        Ok(interrupt) => interrupt,
        Err(e) => {
            eprintln!("error: cannot wait for SIGINT, SIGTERM and SIGHUP: {e}");
            std::process::exit(EXIT_ERROR);
        }
    };
    exit(octoflow_engine::cli::main(std::env::args_os().skip(1), &interrupt));
}
