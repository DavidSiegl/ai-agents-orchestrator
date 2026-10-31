//! The repository's chores in Rust, run as `cargo xtask <command>` through `.cargo/config.toml`'s alias: the eval
//! set's runner, `eval` ([`eval`]), and the builder of its tasks' bundles, `bundles` ([`bundles`]). src/main.rs reads
//! the command line and hands each command its arguments.

pub mod bundles;
pub mod eval;
mod process;
pub mod toy_repos;

use std::fmt;
use std::path::{Path, PathBuf};

pub use process::TempDir;

/// The checkout this xtask was built from: the workspace's top directory, which holds evals/.
pub fn checkout() -> PathBuf {
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    manifest_dir.parent().unwrap_or(manifest_dir).to_path_buf()
}

/// A chore that could not be done, for a reason of the chore's own rather than of what it judges: its message says
/// what was being done and why it failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error(pub String);

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}
