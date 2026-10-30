//! FakeCratesIo, the crates.io the new-dependencies tests ask: crates.io behind an [`Http`], as FakePyPI
//! (crate::pypi::fake) plays PyPI, so that [`CratesIo`](super::CratesIo)'s URL, headers, answers and errors run in
//! those tests.

use std::sync::{Arc, Mutex, MutexGuard};

use serde_json::{json, Value};

use super::USER_AGENT;
use crate::constants::CRATES_IO_TIMEOUT;
use crate::http::{Http, HttpError, Request, Response};

/// crates.io's JSON for a crate created on one day, whose default version was published on another, as
/// https://crates.io/api/v1/crates/serde answers. Each field is the part of the answer of its name.
#[derive(Clone, Debug)]
pub(crate) struct CrateJson {
    /// The day the crate was created, YYYY-MM-DD.
    pub created: &'static str,
    /// The day its default version was published.
    pub latest: &'static str,
    pub version: &'static str,
    /// That version's license; None for null.
    pub license: Option<&'static str>,
}

impl Default for CrateJson {
    fn default() -> Self {
        CrateJson {
            created: "2014-12-05",
            latest: "2026-09-01",
            version: "1.0.228",
            license: Some("MIT OR Apache-2.0"),
        }
    }
}

impl CrateJson {
    pub fn json(&self) -> Value {
        json!({
            "crate": {
                "created_at": format!("{}T20:20:39.487502Z", self.created),
                "default_version": self.version,
                "max_version": self.version,
            },
            "versions": [
                {
                    "num": self.version,
                    "created_at": format!("{}T10:00:00.123456Z", self.latest),
                    "license": self.license,
                },
                {"num": "0.1.0", "created_at": format!("{}T20:20:39.487502Z", self.created), "license": "MIT"},
            ],
        })
    }
}

/// What FakeCratesIo holds and records.
#[derive(Debug, Default)]
pub(crate) struct FakeCratesIoState {
    /// Each crate's JSON, by the name in its URL; any other name is a 404.
    pub crates: Vec<(String, Value)>,
    /// Whether every request times out.
    pub down: bool,
    /// The crate names asked for, in order.
    pub asked: Vec<String>,
}

pub(crate) struct FakeCratesIo {
    state: Mutex<FakeCratesIoState>,
}

impl FakeCratesIo {
    /// crates.io with these crates, by name.
    pub fn with(crates: &[(&str, CrateJson)]) -> Arc<FakeCratesIo> {
        let crates = crates.iter().map(|(name, c)| (name.to_string(), c.json())).collect();
        Arc::new(FakeCratesIo { state: Mutex::new(FakeCratesIoState { crates, ..FakeCratesIoState::default() }) })
    }

    pub fn state(&self) -> MutexGuard<'_, FakeCratesIoState> {
        self.state.lock().unwrap()
    }
}

impl Http for FakeCratesIo {
    fn send(&self, request: &Request) -> Result<Response, HttpError> {
        assert_eq!(request.timeout, CRATES_IO_TIMEOUT);
        assert_eq!(request.header("User-Agent"), Some(USER_AGENT));
        let name = request
            .url
            .strip_prefix("https://crates.io/api/v1/crates/")
            .filter(|name| !name.is_empty() && !name.contains('/'))
            .unwrap_or_else(|| panic!("not a crate's URL: {}", request.url));
        let mut state = self.state();
        state.asked.push(name.to_string());
        if state.down {
            return Err(HttpError::NoAnswer("timed out".to_string()));
        }
        match state.crates.iter().find(|(n, _)| n == name) {
            Some((_, json)) => Ok(Response::ok(json.to_string())),
            None => Err(HttpError::Status(404)),
        }
    }
}
