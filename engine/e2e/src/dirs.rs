//! Directories as a world's harness reads and clears them, every failure but a missing directory failing the test.

use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

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

#[cfg(test)]
mod tests {
    use super::*;

    /// A directory of the test's own under the system's temporary directory, named name, made empty.
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("octoflow-e2e-dirs-{name}-{}", std::process::id()));
        remove_dir(&dir);
        fs::create_dir_all(dir.join("b")).expect("a scratch directory");
        fs::write(dir.join("a"), "").expect("a scratch file");
        dir
    }

    #[test]
    fn entries_are_listed_in_name_order_and_a_missing_directory_has_none() {
        let dir = scratch("entries");
        assert_eq!(entries(&dir), Some(vec![("a".into(), dir.join("a")), ("b".into(), dir.join("b"))]));
        assert_eq!(entries(&dir.join("missing")), None);
        remove_dir(&dir);
    }

    #[test]
    fn a_file_cannot_be_listed() {
        let dir = scratch("file-listed");
        let listed = std::panic::catch_unwind(|| entries(&dir.join("a")));
        remove_dir(&dir);
        assert!(listed.is_err(), "a file was listed as a directory");
    }

    #[test]
    fn remove_dir_removes_what_is_there_and_takes_a_missing_one_as_removed() {
        let dir = scratch("removed");
        remove_dir(&dir);
        assert!(!dir.exists());
        remove_dir(&dir);
    }

    #[test]
    fn a_file_is_no_directory_to_remove() {
        let dir = scratch("file-removed");
        let removed = std::panic::catch_unwind(|| remove_dir(&dir.join("a")));
        remove_dir(&dir);
        assert!(removed.is_err(), "a file was removed as a directory");
    }
}
