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
