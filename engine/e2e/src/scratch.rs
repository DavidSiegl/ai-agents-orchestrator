//! Directories a test makes for itself under the system's temporary directory.

use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

/// A directory of a test's own, `octoflow-e2e-<name>-<pid>` under the system's temporary directory (TMPDIR's, else
/// /tmp), removed with everything in it when dropped; one that keeps on failure is kept instead when it is dropped
/// while its thread panics, and its path printed, for a look at what the failing test left.
///
/// Two of one name in one process are one directory: name them apart.
pub struct Scratch {
    /// The directory, its symbolic links resolved.
    path: PathBuf,
    /// true means a drop during a panic keeps the directory.
    kept_on_failure: bool,
}

impl Scratch {
    /// A new, empty directory for name, removed when dropped. What an earlier run of this process's pid left there is
    /// removed first. Panics, naming the path, when the directory cannot be removed, made or resolved.
    pub fn new(name: &str) -> Scratch {
        let dir = std::env::temp_dir().join(format!("octoflow-e2e-{name}-{}", std::process::id()));
        match fs::remove_dir_all(&dir) {
            Err(e) if e.kind() != ErrorKind::NotFound => panic!("removing {}: {e}", dir.display()),
            _ => {}
        }
        fs::create_dir_all(&dir).unwrap_or_else(|e| panic!("making {}: {e}", dir.display()));
        let path = dir.canonicalize().unwrap_or_else(|e| panic!("resolving {}: {e}", dir.display()));
        Scratch { path, kept_on_failure: false }
    }

    /// As new, but kept when dropped during a panic.
    pub fn kept_on_failure(name: &str) -> Scratch {
        let mut scratch = Scratch::new(name);
        scratch.kept_on_failure = true;
        scratch
    }

    /// The directory's path, its symbolic links resolved.
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for Scratch {
    /// Reports on standard error, rather than panicking, where the directory cannot be removed.
    fn drop(&mut self) {
        if self.kept_on_failure && std::thread::panicking() {
            eprintln!("kept for a look at the failure: {}", self.path.display());
        } else if let Err(e) = fs::remove_dir_all(&self.path) {
            eprintln!("removing {}: {e}", self.path.display());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_scratch_is_made_empty_and_removed_when_dropped() {
        let first = Scratch::new("scratch-removed");
        fs::write(first.path().join("left"), "").expect("a file in the scratch");
        drop(first);
        let scratch = Scratch::new("scratch-removed");
        assert_eq!(fs::read_dir(scratch.path()).map(Iterator::count).ok(), Some(0));
        let path = scratch.path().to_path_buf();
        drop(scratch);
        assert!(!path.exists());
    }

    #[test]
    fn a_scratch_kept_on_failure_is_kept_by_a_panic_only() {
        let path = Scratch::kept_on_failure("scratch-kept").path().to_path_buf();
        assert!(!path.exists(), "kept though nothing failed");
        let kept = std::panic::catch_unwind(|| {
            let scratch = Scratch::kept_on_failure("scratch-kept");
            panic!("{}", scratch.path().display());
        });
        assert!(kept.is_err());
        assert!(path.exists(), "removed though the test failed");
        drop(Scratch::new("scratch-kept"));
        let removed = std::panic::catch_unwind(|| {
            let _scratch = Scratch::new("scratch-kept");
            panic!("failing");
        });
        assert!(removed.is_err());
        assert!(!path.exists(), "a plain scratch kept by a panic");
    }
}
