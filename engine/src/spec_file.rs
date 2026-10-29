//! A spec given with run --spec: read on the orchestrator's machine, it becomes the contract step's file of a run
//! that starts at the step after it (Pipeline::seeded_start), as orchestrator.py's SpecFile and read_spec_file.

use crate::error::{strerror, OrchestratorError};
use crate::pull_request::spec_title;
use crate::py::text::{basename, splitext, strip, utf8_error};

/// A spec as read_spec_file read it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SpecFile {
    /// The path as given, for the log.
    pub path: String,
    /// The file's text, its line ends as the file has them.
    pub text: String,
}

impl SpecFile {
    /// The task of a run given none: the spec's title, or else the file's name without its extension.
    pub fn task(&self) -> String {
        match spec_title(&self.text).filter(|t| !t.is_empty()) {
            Some(title) => title,
            None => splitext(basename(&self.path)).0.to_string(),
        }
    }
}

/// The spec at path on this machine, its line ends kept, so the run gets it unchanged.
///
/// Errors, naming the path: the file cannot be read (with the system's reason), is not UTF-8 (with what Python's
/// decoder says of it), or holds only blanks.
pub fn read_spec_file(path: &str) -> Result<SpecFile, OrchestratorError> {
    let bytes =
        std::fs::read(path).map_err(|e| OrchestratorError(format!("cannot read spec file {path}: {}", strerror(&e))))?;
    let text = String::from_utf8(bytes).map_err(|e| {
        let why = utf8_error(e.as_bytes(), &e.utf8_error());
        OrchestratorError(format!("spec file {path} is not UTF-8 text: {why}"))
    })?;
    if strip(&text).is_empty() {
        return Err(OrchestratorError(format!("spec file {path} is empty")));
    }
    Ok(SpecFile { path: path.to_string(), text })
}

#[cfg(test)]
mod tests;
