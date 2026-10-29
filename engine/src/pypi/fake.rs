//! FakePyPI, the index the new-dependencies tests ask: PyPI behind an [`Http`], played as tests/test_orchestrator.py's
//! FakePyPI plays it behind urlopen, so that [`PyPI`](super::PyPI)'s URL, answers and errors run in those tests.

use std::sync::{Arc, Mutex, MutexGuard};

use crate::constants::PYPI_TIMEOUT;
use crate::host::fake::FakeTime;
use crate::http::{Http, HttpError, Request, Response};
use crate::py::json::{Json, Map};

/// PyPI's JSON for a project with one early release and its latest, as https://docs.pypi.org/api/json/ has it:
/// Python's pypi_project(). Each field is that function's argument of the name.
#[derive(Clone, Debug)]
pub(crate) struct ProjectJson {
    /// The day of its first release's files, YYYY-MM-DD.
    pub first: &'static str,
    /// The day of its latest release's files.
    pub latest: &'static str,
    pub version: &'static str,
    /// info.license; None for null.
    pub license: Option<&'static str>,
    /// info.license_expression; None for null.
    pub expression: Option<&'static str>,
    pub classifiers: Vec<&'static str>,
}

impl Default for ProjectJson {
    fn default() -> Self {
        ProjectJson {
            first: "2015-01-03",
            latest: "2024-06-22",
            version: "3.13.0",
            license: Some("MIT"),
            expression: None,
            classifiers: Vec::new(),
        }
    }
}

impl ProjectJson {
    pub fn json(&self) -> Json {
        let files = |day: &str| {
            let file = |time: &str| format!(r#"{{"upload_time_iso_8601": "{day}T{time}"}}"#);
            Json::parse(&format!("[{}, {}]", file("10:00:00.123456Z"), file("11:00:00Z"))).unwrap()
        };
        let info: Map<Json> = [
            ("version", Json::from(self.version)),
            ("license", self.license.into()),
            ("license_expression", self.expression.into()),
            ("classifiers", Json::Array(self.classifiers.iter().map(|&c| c.into()).collect())),
        ]
        .into_iter()
        .collect();
        let releases: Map<Json> =
            [("0.1", files(self.first)), ("0.2", Json::Array(Vec::new())), (self.version, files(self.latest))]
                .into_iter()
                .collect();
        Json::Object(
            [("info", Json::Object(info)), ("releases", Json::Object(releases)), ("urls", files(self.latest))]
                .into_iter()
                .collect(),
        )
    }
}

/// What FakePyPI holds and records.
#[derive(Debug, Default)]
pub(crate) struct FakePyPIState {
    /// Each project's JSON, by the name in its URL; any other name is a 404.
    pub projects: Map<Json>,
    /// Whether every request times out.
    pub down: bool,
    /// The project names asked for, in order.
    pub asked: Vec<String>,
    /// The time each request lets pass, seconds: a FakeClock's time, which the request moves on by seconds; None lets
    /// none pass.
    pub clock: Option<(FakeTime, f64)>,
    /// What happened, in order: `ask <name>` for each request, and what a test adds between them.
    pub events: Vec<String>,
}

pub(crate) struct FakePyPI {
    state: Mutex<FakePyPIState>,
}

impl FakePyPI {
    /// PyPI with these projects, by name.
    pub fn with(projects: &[(&str, ProjectJson)]) -> Arc<FakePyPI> {
        FakePyPI::from_json(projects.iter().map(|(name, p)| (*name, p.json())).collect())
    }

    /// PyPI with these projects' JSON, by name.
    pub fn from_json(projects: Map<Json>) -> Arc<FakePyPI> {
        Arc::new(FakePyPI { state: Mutex::new(FakePyPIState { projects, ..FakePyPIState::default() }) })
    }

    pub fn state(&self) -> MutexGuard<'_, FakePyPIState> {
        self.state.lock().unwrap()
    }
}

impl Http for FakePyPI {
    fn send(&self, request: &Request) -> Result<Response, HttpError> {
        assert_eq!(request.timeout, PYPI_TIMEOUT);
        let name = request
            .url
            .strip_prefix("https://pypi.org/pypi/")
            .and_then(|rest| rest.strip_suffix("/json"))
            .filter(|name| !name.is_empty() && !name.contains('/'))
            .unwrap_or_else(|| panic!("not a PyPI project's URL: {}", request.url));
        let mut state = self.state();
        state.asked.push(name.to_string());
        state.events.push(format!("ask {name}"));
        if let Some((time, seconds)) = &state.clock {
            time.advance(*seconds);
        }
        if state.down {
            return Err(HttpError::NoAnswer("timed out".to_string()));
        }
        match state.projects.get(name) {
            Some(project) => Ok(Response::ok(project.dumps())),
            None => Err(HttpError::Status(404)),
        }
    }
}
