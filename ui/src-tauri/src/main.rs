// No console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // Before the window initialises GTK or WebKit, which the exec would discard. Windows builds no CLI: its release
    // has no console to print to.
    #[cfg(unix)]
    if let Some(cli) = orchestrator_ui_lib::cli(std::env::args_os().skip(1)) {
        cli.exec()
    }
    orchestrator_ui_lib::run()
}
