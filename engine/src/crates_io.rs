//! crates.io's API (https://crates.io/data-access), as the new-dependencies check (crate::checks::dependencies) asks
//! it about a crate a Rust project adds.

use std::sync::Arc;

use serde_json::Value;

use crate::constants::CRATES_IO_TIMEOUT;
use crate::http::{Http, HttpError, Method, Request};
use crate::package_index::{IndexError, IndexedPackage, PackageIndex, Upload};
use crate::py::text::{shortened, utf8_error};
use crate::py::url::quote;

#[cfg(test)]
pub(crate) mod fake;

/// The User-Agent of every request: crates.io refuses a request without one, and asks that it name who is asking
/// (its crawler policy). It names the project, never the user, whose email or name must not reach crates.io.
pub const USER_AGENT: &str = "octoflow (https://github.com/DavidSiegl/octoflow-ai)";

/// The index, reached through an [`Http`].
pub struct CratesIo {
    http: Arc<dyn Http>,
}

impl CratesIo {
    pub fn new(http: Arc<dyn Http>) -> CratesIo {
        CratesIo { http }
    }
}

impl PackageIndex for CratesIo {
    fn name(&self) -> &'static str {
        "crates.io"
    }

    fn unknown_rule(&self) -> &'static str {
        "not-on-crates-io"
    }

    /// What crates.io knows of the crate key: its first release is when the crate was created, its latest version
    /// crates.io's default one (the highest that is neither yanked nor a pre-release, if there is one), with that
    /// version's date and license. crates.io finds a crate by any spelling of its name that differs only in case and
    /// in `-` for `_`, so the key finds it.
    fn package(&self, key: &str) -> Result<Option<IndexedPackage>, IndexError> {
        let url = format!("https://crates.io/api/v1/crates/{}", quote(key));
        let request = Request {
            method: Method::Get,
            url: url.clone(),
            headers: vec![
                ("Accept".to_string(), "application/json".to_string()),
                ("User-Agent".to_string(), USER_AGENT.to_string()),
            ],
            form: None,
            timeout: CRATES_IO_TIMEOUT,
        };
        let body = match self.http.send(&request) {
            Ok(answer) => answer.body,
            Err(HttpError::Status(404)) => return Ok(None),
            Err(HttpError::Status(status)) => return Err(IndexError(format!("GET {url}: HTTP {status}"))),
            Err(HttpError::NoAnswer(why)) => return Err(IndexError(format!("GET {url}: {why}"))),
        };
        crate_of(&body)
            .map(Some)
            .map_err(|why| IndexError(format!("GET {url}: the answer is not crates.io's JSON: {why}")))
    }
}

/// The crate of crates.io's JSON answer; why not, for an answer of another shape.
fn crate_of(body: &[u8]) -> Result<IndexedPackage, String> {
    let text = std::str::from_utf8(body).map_err(|e| utf8_error(body, &e))?;
    let answer: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let krate = answer.get("crate").filter(|c| c.is_object()).ok_or("it has no crate")?;
    let created = krate.get("created_at").and_then(Value::as_str).ok_or("it has no crate.created_at")?;
    // default_version is null when every version is yanked; max_version is then the highest of them.
    let version = ["default_version", "max_version"]
        .iter()
        .find_map(|key| krate.get(*key).and_then(Value::as_str))
        .ok_or("it has neither crate.default_version nor crate.max_version")?;
    let versions = match answer.get("versions") {
        None | Some(Value::Null) => &Vec::new(),
        Some(Value::Array(versions)) => versions,
        Some(_) => return Err("its versions is not a list".to_string()),
    };
    let latest = versions.iter().find(|v| v.get("num").and_then(Value::as_str) == Some(version));
    let latest_release = match latest.and_then(|v| v.get("created_at")).and_then(Value::as_str) {
        Some(time) => Some(Upload::from_iso(time)?),
        None => None,
    };
    let license = latest
        .and_then(|v| v.get("license"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|license| !license.is_empty())
        .map(|license| shortened(license.lines().next().unwrap_or_default(), 80));
    Ok(IndexedPackage {
        first_release: Some(Upload::from_iso(created)?),
        version: version.to_string(),
        latest_release,
        license,
    })
}

#[cfg(test)]
mod tests;
