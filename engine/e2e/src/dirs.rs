//! Directories as a world's harness makes, reads and clears them, every failure but a missing directory failing the
//! test.

use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

/// The entries of the directory at dir, each its name and its path, in name order; None when there is no dir. Panics
/// where it cannot be listed otherwise.
pub fn entries(dir: &Path) -> Option<Vec<(String, PathBuf)>> {
    let listing = match fs::read_dir(dir) {
        Ok(listing) => listing,
        Err(e) if e.kind() == ErrorKind::NotFound => return None,
        Err(e) => panic!("listing {}: {e}", dir.display()),
    };
    let mut found: Vec<(String, PathBuf)> = listing
        .map(|e| {
            let e = e.unwrap_or_else(|err| panic!("listing {}: {err}", dir.display()));
            (e.file_name().to_string_lossy().into_owned(), e.path())
        })
        .collect();
    found.sort();
    Some(found)
}

/// Remove the directory at path and all it holds, if it is there; panics where it cannot.
pub fn remove_dir(path: &Path) {
    match fs::remove_dir_all(path) {
        Err(e) if e.kind() != ErrorKind::NotFound => panic!("removing {}: {e}", path.display()),
        _ => {}
    }
}


/// The message f panics with; panics itself when f returns. For a test that holds a failure to its words.
pub fn panic_message<T>(f: impl FnOnce() -> T + std::panic::UnwindSafe) -> String {
    let Err(payload) = std::panic::catch_unwind(f) else {
        panic!("it did not panic");
    };
    match payload.downcast::<String>() {
        Ok(message) => *message,
        Err(payload) => payload.downcast::<&str>().map_or_else(|_| "(no message)".into(), |m| m.to_string()),
    }
}

/// The directory temporary files go to: TMPDIR, else the system's.
pub fn temp() -> PathBuf {
    std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, PathBuf::from)
}

/// An empty directory of a test's own, removed with all it holds when the Scratch is dropped, a test's panic
/// included, unless it is kept for a look at a failure (kept_on_failure).
pub struct Scratch {
    path: PathBuf,
    /// True means a drop during a panic leaves the directory where it is.
    kept_on_failure: bool,
}

impl Scratch {
    /// A new empty directory under base, named for name, this process and how many scratches it made before, so that
    /// no other test's, in this process or another, is the same. Panics where it cannot be made.
    pub fn new(base: &Path, name: &str) -> Scratch {
        static MADE: AtomicUsize = AtomicUsize::new(0);
        let n = MADE.fetch_add(1, Ordering::Relaxed);
        let path = base.join(format!("octoflow-e2e-{name}-{}-{n}", std::process::id()));
        remove_dir(&path);
        fs::create_dir_all(&path).unwrap_or_else(|e| panic!("making {}: {e}", path.display()));
        Scratch { path, kept_on_failure: false }
    }

    /// As new, but a drop during a panic keeps the directory, for a look at what the failing test left there.
    pub fn kept_on_failure(base: &Path, name: &str) -> Scratch {
        let mut scratch = Scratch::new(base, name);
        scratch.kept_on_failure = true;
        scratch
    }

    /// The directory's path.
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        if !(self.kept_on_failure && std::thread::panicking()) {
            remove_dir(&self.path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A scratch directory under the temporary one that holds a file, a, and a directory, b.
    fn with_a_and_b(name: &str) -> Scratch {
        let scratch = Scratch::new(&temp(), name);
        fs::create_dir(scratch.path().join("b")).expect("a directory in the scratch");
        fs::write(scratch.path().join("a"), "").expect("a file in the scratch");
        scratch
    }

    #[test]
    fn entries_are_listed_in_name_order_and_a_missing_directory_has_none() {
        let scratch = with_a_and_b("entries");
        let dir = scratch.path();
        assert_eq!(entries(dir), Some(vec![("a".into(), dir.join("a")), ("b".into(), dir.join("b"))]));
        assert_eq!(entries(&dir.join("missing")), None);
    }

    #[test]
    fn a_file_cannot_be_listed() {
        let scratch = with_a_and_b("file-listed");
        let file = scratch.path().join("a");
        let message = panic_message(|| entries(&file));
        assert!(message.starts_with(&format!("listing {}: ", file.display())), "{message}");
    }

    #[test]
    fn remove_dir_removes_what_is_there_and_takes_a_missing_one_as_removed() {
        let scratch = with_a_and_b("removed");
        remove_dir(scratch.path());
        assert!(!scratch.path().exists());
        remove_dir(scratch.path());
    }

    #[test]
    fn a_file_is_no_directory_to_remove() {
        let scratch = with_a_and_b("file-removed");
        let file = scratch.path().join("a");
        let message = panic_message(|| remove_dir(&file));
        assert!(message.starts_with(&format!("removing {}: ", file.display())), "{message}");
    }

    #[test]
    fn a_scratch_is_removed_when_dropped() {
        let scratch = Scratch::new(&temp(), "dropped");
        let path = scratch.path().to_path_buf();
        drop(scratch);
        assert!(!path.exists());
    }

    #[test]
    fn a_scratch_kept_on_failure_is_kept_by_a_panic_only() {
        let kept = Scratch::kept_on_failure(&temp(), "kept");
        let path = kept.path().to_path_buf();
        let failed = std::panic::catch_unwind(move || {
            let _kept = kept;
            panic!("a failing test");
        });
        assert!(failed.is_err());
        assert!(path.exists(), "a scratch kept on failure was removed by a panic");
        remove_dir(&path);
        let passing = Scratch::kept_on_failure(&temp(), "passing");
        let path = passing.path().to_path_buf();
        drop(passing);
        assert!(!path.exists(), "a scratch kept on failure was kept by a passing test");
    }

    #[test]
    fn two_scratches_of_one_name_are_two_directories() {
        let (a, b) = (Scratch::new(&temp(), "twice"), Scratch::new(&temp(), "twice"));
        assert_ne!(a.path(), b.path());
    }
}
