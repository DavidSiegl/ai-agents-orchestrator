//! The orchestrator program: crate::cli::main, ending as interrupt::exit ends it.

fn main() {
    // cli::main starts the signal handling, so it runs before any other thread starts.
    octoflow_engine::interrupt::exit(octoflow_engine::cli::main(std::env::args_os().skip(1)));
}
