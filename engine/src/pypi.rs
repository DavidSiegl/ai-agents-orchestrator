//! The Python Package Index's JSON API (https://docs.pypi.org/api/json/), as orchestrator.py's PyPI asks it: what
//! the new-dependencies check (crate::checks) knows of a package from PyPI.

use std::fmt;
use std::sync::Arc;

use crate::constants::PYPI_TIMEOUT;
use crate::http::{Http, HttpClient, HttpError, Method, Request};
use crate::py::json::Json;
use crate::py::text::{shortened, splitlines, strip, utf8_error};
use crate::py::url::quote;
use crate::summary::epoch_seconds;

#[cfg(test)]
pub(crate) mod fake;

/// PyPI gave no answer the engine can use: none at all, an HTTP error other than 404, or something that is not its
/// JSON. The message names the URL and why.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PyPIError(pub String);

impl fmt::Display for PyPIError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for PyPIError {}

/// When a file was uploaded to PyPI.
#[derive(Clone, Debug, PartialEq)]
pub struct Upload {
    /// Seconds since the epoch, with their fraction; a time PyPI gave without an offset is taken as UTC.
    pub at: f64,
    /// Its date, YYYY-MM-DD, in the offset PyPI gave the time in: UTC, as PyPI gives every time.
    pub date: String,
}

/// What PyPI knows of a project.
#[derive(Clone, Debug, PartialEq)]
pub struct PyPIProject {
    /// The upload of its earliest file; None when no release has a file.
    pub first_release: Option<Upload>,
    /// Its latest version.
    pub version: String,
    /// The upload of that version's earliest file; None when it has none.
    pub latest_release: Option<Upload>,
    /// Its license expression, else the first line of its license field, cut to 80 characters, else its license
    /// classifier's last part; None when it states none.
    pub license: Option<String>,
}

/// The index, reached through an [`Http`].
pub struct PyPI {
    http: Arc<dyn Http>,
}

impl PyPI {
    pub fn new(http: Arc<dyn Http>) -> PyPI {
        PyPI { http }
    }

    /// The index over the network, through the proxy the process's environment names (crate::http::HttpClient).
    pub fn from_env() -> PyPI {
        PyPI::new(Arc::new(HttpClient::from_env()))
    }

    /// What PyPI knows of the project name; None when it has no such project (HTTP 404). Errors: PyPIError, naming
    /// the URL, when it gives no usable answer within PYPI_TIMEOUT seconds a step.
    pub fn project(&self, name: &str) -> Result<Option<PyPIProject>, PyPIError> {
        let url = format!("https://pypi.org/pypi/{}/json", quote(name));
        let request = Request {
            method: Method::Get,
            url: url.clone(),
            headers: vec![("Accept".to_string(), "application/json".to_string())],
            form: None,
            timeout: PYPI_TIMEOUT,
        };
        let body = match self.http.send(&request) {
            Ok(answer) => answer.body,
            Err(HttpError::Status(404)) => return Ok(None),
            Err(HttpError::Status(status)) => return Err(PyPIError(format!("GET {url}: HTTP {status}"))),
            Err(HttpError::NoAnswer(why)) => return Err(PyPIError(format!("GET {url}: {why}"))),
        };
        project_of(&body).map(Some).map_err(|why| PyPIError(format!("GET {url}: the answer is not PyPI's JSON: {why}")))
    }
}

/// The project of PyPI's JSON answer; why not, as Python's error of the step that fails says it, for an answer of
/// another shape.
fn project_of(body: &[u8]) -> Result<PyPIProject, String> {
    let text = std::str::from_utf8(body).map_err(|e| utf8_error(body, &e))?;
    let answer = Json::parse(text)?;
    let info = match &answer {
        Json::Object(map) => map.get("info").ok_or("'info'")?,
        _ => return Err("it has no info.version".to_string()),
    };
    let version = match info.get("version") {
        Some(Json::Str(version)) => version.clone(),
        _ => return Err("it has no info.version".to_string()),
    };
    let mut files = Vec::new();
    match answer.get("releases").filter(|r| r.truthy()) {
        None => {}
        Some(Json::Object(releases)) => {
            for release in releases.values() {
                files.extend(items(release)?);
            }
        }
        Some(other) => return Err(format!("'{}' object has no attribute 'values'", type_name(other))),
    }
    let first = first_upload(&files)?;
    let latest = first_upload(&answer.get("urls").map_or(Ok(Vec::new()), items)?)?;
    Ok(PyPIProject { first_release: first, version, latest_release: latest, license: license(info)? })
}

/// The items of `value or []`; an error for a true value that is not a list.
fn items(value: &Json) -> Result<Vec<&Json>, String> {
    match value {
        Json::Array(items) => Ok(items.iter().collect()),
        other if !other.truthy() => Ok(Vec::new()),
        other => Err(format!("'{}' object is not iterable", type_name(other))),
    }
}

/// The earliest upload_time_iso_8601 of PyPI's files, None for none.
fn first_upload(files: &[&Json]) -> Result<Option<Upload>, String> {
    let mut first: Option<Upload> = None;
    for file in files {
        let time = match file {
            Json::Object(file) => file.get("upload_time_iso_8601").ok_or("'upload_time_iso_8601'")?,
            other => return Err(format!("'{}' object is not subscriptable", type_name(other))),
        };
        let Json::Str(time) = time else { return Err("fromisoformat: argument must be str".to_string()) };
        let upload = upload_of(time)?;
        if first.as_ref().is_none_or(|f| upload.at < f.at) {
            first = Some(upload);
        }
    }
    Ok(first)
}

/// The upload at the ISO 8601 time iso, as datetime.fromisoformat reads it.
fn upload_of(iso: &str) -> Result<Upload, String> {
    let seconds = epoch_seconds(iso)?;
    // epoch_seconds leaves out the fraction of a second, which orders two files uploaded in one second.
    let clock = iso.get(10..).unwrap_or("");
    let fraction = clock.split_once('.').map_or("", |(_, f)| f.split(['Z', '+', '-']).next().unwrap_or(""));
    let fraction: f64 = if fraction.is_empty() { 0.0 } else { format!("0.{fraction}").parse().unwrap_or(0.0) };
    Ok(Upload { at: seconds as f64 + fraction, date: iso[..10].to_string() })
}

/// The project's license, as PyPIProject::license says.
fn license(info: &Json) -> Result<Option<String>, String> {
    let text = |key: &str| -> Result<String, String> {
        match info.get(key) {
            Some(Json::Str(s)) => Ok(strip(s).to_string()),
            Some(value) if value.truthy() => Err(format!("'{}' object has no attribute 'strip'", type_name(value))),
            _ => Ok(String::new()),
        }
    };
    let expression = text("license_expression")?;
    if !expression.is_empty() {
        return Ok(Some(expression));
    }
    let field = text("license")?;
    if !field.is_empty() {
        return Ok(Some(shortened(splitlines(&field)[0], 80)));
    }
    let classifiers = info.get("classifiers").map_or(Ok(Vec::new()), items)?;
    for classifier in classifiers {
        let Json::Str(classifier) = classifier else {
            return Err(format!("'{}' object has no attribute 'startswith'", type_name(classifier)));
        };
        if classifier.starts_with("License :: ") {
            return Ok(classifier.rsplit_once(" :: ").map(|(_, last)| last.to_string()));
        }
    }
    Ok(None)
}

/// The name of the Python type json.loads reads value as.
fn type_name(value: &Json) -> &'static str {
    match value {
        Json::Null => "NoneType",
        Json::Bool(_) => "bool",
        Json::Int(_) => "int",
        Json::Float(_) => "float",
        Json::Str(_) => "str",
        Json::Array(_) => "list",
        Json::Object(_) => "dict",
    }
}

#[cfg(test)]
mod tests;
