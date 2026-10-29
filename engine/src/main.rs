//! The orchestrator program: crate::cli::main, once the Ctrl-C's signal thread runs.

use octoflow_engine::constants::EXIT_ERROR;
use octoflow_engine::interrupt::on_sigint;

fn main() {
    // First, on the main thread, before any other thread starts (on_sigint says why).
    let interrupt = match on_sigint() {
        Ok(interrupt) => interrupt,
        Err(e) => {
            eprintln!("error: cannot wait for the Ctrl-C's SIGINT: {e}");
            std::process::exit(EXIT_ERROR);
        }
    };
    std::process::exit(octoflow_engine::cli::main(std::env::args_os().skip(1), &interrupt));
}
