//! The package indexes that the new-dependencies check (crate::checks::dependencies) asks about the packages a change
//! adds: PyPI (crate::pypi) for a Python project's, crates.io (crate::crates_io) for a Rust project's.

use std::fmt;
use std::sync::Arc;

use crate::crates_io::CratesIo;
use crate::http::{Http, HttpClient};
use crate::py::datetime::IsoTime;
use crate::pypi::PyPI;

/// An index gave no answer the engine can use: none at all, an HTTP error other than 404, or something that is not
/// its JSON. The message names the URL and why.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IndexError(pub String);

impl fmt::Display for IndexError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for IndexError {}

/// When a release was published to an index.
#[derive(Clone, Debug, PartialEq)]
pub struct Upload {
    /// Seconds since the epoch, with their fraction; a time the index gave without an offset is the machine's local
    /// time, as Python's timestamp() takes it (crate::py::datetime::IsoTime::timestamp).
    pub at: f64,
    /// Its date, YYYY-MM-DD, in the offset the index gave the time in: UTC, as PyPI and crates.io give every time.
    pub date: String,
}

impl Upload {
    /// The upload at the ISO 8601 time iso, as Python's datetime.fromisoformat reads it; why not, as Python words
    /// it, for a time it does not read.
    pub fn from_iso(iso: &str) -> Result<Upload, String> {
        let time = IsoTime::parse(iso)?;
        Ok(Upload { at: time.timestamp()?, date: time.date() })
    }
}

/// What an index knows of a package.
#[derive(Clone, Debug, PartialEq)]
pub struct IndexedPackage {
    /// Its first release; None when the index has no file of any release.
    pub first_release: Option<Upload>,
    /// Its latest version, as the index chooses it.
    pub version: String,
    /// When that version was published; None when the index does not say.
    pub latest_release: Option<Upload>,
    /// Its license, cut to one line of at most 80 characters; None when it states none.
    pub license: Option<String>,
}

/// An index the new-dependencies check asks about a package.
pub trait PackageIndex {
    /// Its name, as a finding gives it: `PyPI`, `crates.io`.
    fn name(&self) -> &'static str;

    /// The rule of the failing finding on a package it does not know, such as `not-on-pypi`.
    fn unknown_rule(&self) -> &'static str;

    /// What the index knows of the package key, a package_key (crate::checks::dependencies); None when it has no
    /// such package (HTTP 404). Errors: IndexError, naming the URL, when it gives no usable answer within the index's
    /// timeout a step.
    fn package(&self, key: &str) -> Result<Option<IndexedPackage>, IndexError>;
}

/// Every index the new-dependencies check asks, one per DEPENDENCY_ECOSYSTEMS ecosystem.
pub struct PackageIndexes {
    pub pypi: PyPI,
    pub crates_io: CratesIo,
}

impl PackageIndexes {
    /// The indexes over the network, through the proxy the process's environment names (crate::http::HttpClient).
    pub fn from_env() -> PackageIndexes {
        let http: Arc<dyn Http> = Arc::new(HttpClient::from_env());
        PackageIndexes { pypi: PyPI::new(Arc::clone(&http)), crates_io: CratesIo::new(http) }
    }
}

#[cfg(test)]
impl PackageIndexes {
    /// The indexes the tests ask: PyPI behind pypi, and a crates.io that knows no crate.
    pub(crate) fn with_pypi(pypi: Arc<dyn Http>) -> PackageIndexes {
        PackageIndexes {
            pypi: PyPI::new(pypi),
            crates_io: CratesIo::new(crate::crates_io::fake::FakeCratesIo::with(&[])),
        }
    }
}
