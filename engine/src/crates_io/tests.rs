//! CratesIo's requests and its reading of crates.io's answers.

use std::sync::Mutex;

use super::fake::{CrateJson, FakeCratesIo};
use super::*;
use crate::http::Response;

fn answered(answer: Result<Response, HttpError>) -> Result<Option<IndexedPackage>, IndexError> {
    struct Once(Mutex<Option<Result<Response, HttpError>>>);
    impl Http for Once {
        fn send(&self, request: &Request) -> Result<Response, HttpError> {
            assert_eq!(request.url, "https://crates.io/api/v1/crates/serde");
            self.0.lock().unwrap().take().expect("one request")
        }
    }
    CratesIo::new(Arc::new(Once(Mutex::new(Some(answer))))).package("serde")
}

#[test]
fn reads_the_creation_the_default_version_its_date_and_license() {
    let fake = FakeCratesIo::with(&[("serde", CrateJson::default())]);
    let found = CratesIo::new(fake.clone()).package("serde").unwrap().unwrap();
    assert_eq!(found.first_release.map(|u| u.date), Some("2014-12-05".to_string()));
    assert_eq!(found.latest_release.map(|u| u.date), Some("2026-09-01".to_string()));
    assert_eq!((found.version.as_str(), found.license.as_deref()), ("1.0.228", Some("MIT OR Apache-2.0")));
    assert_eq!(fake.state().asked, ["serde"]);
}

#[test]
fn a_crate_whose_versions_are_all_yanked_has_its_highest() {
    let text = r#"{"crate": {"created_at": "2020-01-02T03:04:05Z", "default_version": null, "max_version": "0.3.0"},
        "versions": [{"num": "0.3.0", "created_at": "2020-02-03T00:00:00Z", "license": null}]}"#;
    let found = answered(Ok(Response::ok(text))).unwrap().unwrap();
    assert_eq!((found.version.as_str(), found.license), ("0.3.0", None));
    assert_eq!(found.latest_release.map(|u| u.date), Some("2020-02-03".to_string()));
}

#[test]
fn a_default_version_missing_from_the_versions_has_no_date_or_license() {
    let text = r#"{"crate": {"created_at": "2020-01-02T03:04:05Z", "default_version": "1.0.0"}, "versions": []}"#;
    let found = answered(Ok(Response::ok(text))).unwrap().unwrap();
    assert_eq!((found.latest_release, found.license), (None, None));
}

#[test]
fn an_unknown_crate_is_none() {
    assert_eq!(CratesIo::new(FakeCratesIo::with(&[])).package("serde-jsn"), Ok(None));
}

#[test]
fn any_other_failure_is_an_error_naming_the_url() {
    let url = "https://crates.io/api/v1/crates/serde";
    for (answer, error) in [
        (Err(HttpError::Status(503)), format!("GET {url}: HTTP 503")),
        (Err(HttpError::NoAnswer("timed out".to_string())), format!("GET {url}: timed out")),
        (Ok(Response::ok("<html>")), format!("GET {url}: the answer is not crates.io's JSON")),
        (Ok(Response::ok("{}")), format!("GET {url}: the answer is not crates.io's JSON: it has no crate")),
        (
            Ok(Response::ok(r#"{"crate": {"default_version": "1.0.0"}}"#)),
            format!("GET {url}: the answer is not crates.io's JSON: it has no crate.created_at"),
        ),
        (
            Ok(Response::ok(r#"{"crate": {"created_at": "yesterday", "max_version": "1.0.0"}}"#)),
            format!("GET {url}: the answer is not crates.io's JSON: "),
        ),
    ] {
        let IndexError(message) = answered(answer).unwrap_err();
        assert!(message.starts_with(&error), "{message}");
    }
}

#[test]
fn a_name_is_quoted_into_its_url() {
    let fake = FakeCratesIo::with(&[]);
    CratesIo::new(fake.clone()).package("a b").unwrap();
    assert_eq!(fake.state().asked, ["a%20b"]);
}
