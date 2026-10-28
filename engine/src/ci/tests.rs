//! The tests of tests/test_orchestrator.py's TestCI and TestQualityReport.test_tokens_are_masked, the credentials
//! rule of tests/test_invariants.py, and tests/contract/test_jenkins.py's chain of calls played on FakeJenkins.

use std::sync::{Arc, Mutex};

use super::fake::{credentials, jenkins_on, parse_qsl, split_url, FakeJenkins, Outcome, JENKINS, JENKINS_TOKEN, JOB, JOB_PATH};
use super::*;
use crate::http::tests::{direct, Answer, LocalServer};
use crate::http::Response;

const CI_ENV_PATH: &str = "/home/u/.config/ai-agents-orchestrator/ci.env";
const REF_1_1: &str = "orchestrator-ci/a1b2c3-1-q1";

/// JOB on a local server that gives the answers in order, then 500s, with the tests' credentials but the server as
/// JENKINS_URL.
fn scripted(answers: Vec<Answer>) -> (LocalServer, Jenkins) {
    let answers = Mutex::new(answers.into_iter());
    let server = LocalServer::start(move |_| answers.lock().unwrap().next().unwrap_or_else(|| Answer::new(500, "")));
    let mut env = credentials();
    env.insert("JENKINS_URL", format!("{}/", server.url));
    let jenkins = Jenkins::new(JOB, &env, CI_ENV_PATH, Arc::new(direct()));
    (server, jenkins)
}

fn leaks(text: &str) -> bool {
    text.contains(JENKINS_TOKEN)
}

fn parameters() -> String {
    let definitions: Vec<String> = JOB_PARAMETERS.iter().map(|p| format!("{{\"name\": \"{p}\"}}")).collect();
    format!("{{\"property\": [{{\"parameterDefinitions\": [{}]}}]}}", definitions.join(", "))
}

#[test]
fn job_url_maps_folders() {
    let http: Arc<dyn Http> = FakeJenkins::new();
    assert_eq!(Jenkins::new(JOB, &credentials(), CI_ENV_PATH, Arc::clone(&http)).job_url(), format!("{JENKINS}{JOB_PATH}"));
    assert_eq!(Jenkins::new("a b", &credentials(), CI_ENV_PATH, http).job_url(), format!("{JENKINS}/job/a%20b/"));
}

#[test]
fn preflight_sends_basic_auth_in_the_header_only() {
    let (server, jenkins) = scripted(vec![Answer::new(200, parameters())]);
    jenkins.preflight().unwrap();
    let seen = server.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].header("Authorization"), Some("Basic YWdlbnRzOmplbmtpbnMtc2VjcmV0LXRva2Vu"));
    assert!(!leaks(&seen[0].url));
    assert_eq!(seen[0].url, format!("{JOB_PATH}api/json?tree=property%5BparameterDefinitions%5Bname%5D%5D"));
}

#[test]
fn errors_name_method_url_and_status() {
    let (server, jenkins) = scripted(vec![Answer::new(403, "<html>")]);
    let e = jenkins.build_result(7).unwrap_err();
    assert_eq!(e.to_string(), format!("GET {}{JOB_PATH}7/api/json?tree=building%2Cresult: HTTP 403", server.url));
    assert_eq!(e.status(), Some(403));
    assert!(!e.transient());
}

#[test]
fn network_errors_are_transient_and_masked() {
    let fake = FakeJenkins::new();
    fake.state().down = true;
    let (server, jenkins) = scripted(vec![Answer::new(503, "")]);
    let refused = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut env = credentials();
        env.insert("JENKINS_URL", format!("http://{}", listener.local_addr().unwrap()));
        Jenkins::new(JOB, &env, CI_ENV_PATH, Arc::new(direct()))
    };
    let refused_base = refused.base().to_string();
    let cases = [(jenkins_on(&fake), JENKINS.to_string()), (jenkins, server.url.clone()), (refused, refused_base)];
    for (jenkins, base) in cases {
        let e = jenkins.build_result(7).unwrap_err();
        assert!(e.transient(), "{e}");
        assert!(!leaks(&e.to_string()), "{e}");
        assert!(e.to_string().contains(&format!("GET {base}{JOB_PATH}7/api/json?tree=building%2Cresult")), "{e}");
    }
}

#[test]
fn trigger_returns_the_queue_item() {
    let (server, jenkins) = scripted(vec![Answer::new(201, "").with_header("Location", "http://internal:8080/queue/item/77/")]);
    assert_eq!(jenkins.trigger(REF_1_1).unwrap(), format!("{}/queue/item/77/", server.url));
    let seen = &server.seen()[0];
    assert_eq!((seen.method.as_str(), seen.url.clone()), ("POST", format!("{JOB_PATH}buildWithParameters")));
    assert_eq!(parse_qsl(&seen.body), [("GIT_REF", REF_1_1.to_string())].into_iter().collect());
}

#[test]
fn trigger_on_a_deleted_job_is_not_retried() {
    let (server, jenkins) = scripted(vec![Answer::new(404, "<html>")]);
    let e = jenkins.trigger(REF_1_1).unwrap_err();
    assert!(matches!(e, CIError::Refused(_)));
    assert_eq!(e.to_string(), format!("Jenkins job {JOB} is gone: POST {}{JOB_PATH}buildWithParameters: HTTP 404", server.url));
}

#[test]
fn missing_credentials_are_named_before_any_request() {
    let fake = FakeJenkins::new();
    let env: Map<String> = [("JENKINS_URL", JENKINS.to_string())].into_iter().collect();
    let jenkins = Jenkins::new(JOB, &env, CI_ENV_PATH, Arc::clone(&fake) as Arc<dyn Http>);
    let e = jenkins.build_result(1).unwrap_err();
    assert_eq!(e.to_string(), format!("the quality gate needs JENKINS_USER, JENKINS_TOKEN in the orchestrator's environment or in {CI_ENV_PATH}"));
    assert!(!e.transient());
    assert_eq!(jenkins.preflight().unwrap_err().to_string(), e.to_string());
    assert!(fake.state().requests.is_empty());
}

#[test]
fn quality_file_is_the_archived_quality_md() {
    let (server, jenkins) = scripted(vec![Answer::new(200, b"GATE: OK\n\nintro \xff\n".to_vec())]);
    assert_eq!(jenkins.quality_file(3).unwrap().as_deref(), Some("GATE: OK\n\nintro \u{fffd}\n"));
    assert_eq!(server.seen()[0].url, format!("{JOB_PATH}3/artifact/quality.md"));
}

#[test]
fn missing_artifact_and_test_report() {
    let (_server, jenkins) = scripted(vec![Answer::new(404, ""), Answer::new(404, "")]);
    assert_eq!(jenkins.quality_file(3), Ok(None));
    assert_eq!(jenkins.failed_tests(3), Ok(None));
    let (_server, jenkins) = scripted(vec![Answer::new(403, ""), Answer::new(500, "")]);
    assert_eq!(jenkins.quality_file(3).unwrap_err().status(), Some(403));
    assert_eq!(jenkins.failed_tests(3).unwrap_err().status(), Some(500));
}

#[test]
fn malformed_answers() {
    let (_server, jenkins) = scripted(vec![Answer::new(200, "<html>proxy error</html>"), Answer::new(200, "[1]"), Answer::new(200, b"\xff".to_vec()), Answer::new(200, "")]);
    for _ in 0..3 {
        let e = jenkins.build_result(1).unwrap_err();
        assert!(e.to_string().contains(": the answer is not"), "{e}");
        assert!(e.transient());
    }
    let e = jenkins.trigger(REF_1_1).unwrap_err();
    assert!(e.to_string().ends_with("buildWithParameters named no queue item"), "{e}");
    assert!(!e.transient());
}

#[test]
fn not_json_says_why() {
    let (_server, jenkins) = scripted(vec![Answer::new(200, b"{\"a\": \xff}".to_vec()), Answer::new(200, "[1]")]);
    assert!(jenkins.build_result(1).unwrap_err().to_string().ends_with(": the answer is not JSON: 'utf-8' codec can't decode byte 0xff in position 6: invalid start byte"));
    assert!(jenkins.build_result(1).unwrap_err().to_string().ends_with("?tree=building%2Cresult: the answer is not a JSON object"));
}

#[test]
fn tokens_are_masked() {
    let jenkins = jenkins_on(&FakeJenkins::new());
    assert_eq!(jenkins.mask(&format!("a {JENKINS_TOKEN} b {JENKINS_TOKEN}")), "a **** b ****");
    let bare = Jenkins::new(JOB, &Map::new(), CI_ENV_PATH, FakeJenkins::new());
    assert_eq!(bare.mask("text"), "text");
    assert_eq!(bare.job(), JOB);
}

/// One answer to every request, enough for each of Jenkins's methods to finish, recording each request: Python's
/// AnyJenkins.
struct AnyJenkins {
    requests: Mutex<Vec<Request>>,
}

impl Http for AnyJenkins {
    fn send(&self, request: &Request) -> Result<Response, HttpError> {
        self.requests.lock().unwrap().push(request.clone());
        let parameters: Vec<String> = JOB_PARAMETERS.iter().map(|p| format!("{{\"name\": \"{p}\"}}")).collect();
        let body = format!(
            "{{\"property\": [{{\"parameterDefinitions\": [{}]}}], \"builds\": [], \"building\": false, \"result\": \"SUCCESS\", \"suites\": []}}",
            parameters.join(", ")
        );
        Ok(Response { status: 200, headers: vec![("Location".to_string(), "http://jenkins.example:8080/queue/item/7/".to_string())], body: body.into_bytes() })
    }
}

/// A call of one method, which unwraps what it returns.
type Call = fn(&Jenkins);

/// One call of each public method of Jenkins but its constructors, by name: a method added without a line here fails
/// the credentials test, so that its requests are checked too.
const CALLS: &[(&str, Call)] = &[
    ("job", |j| { let _ = j.job(); }),
    ("job_url", |j| { let _ = j.job_url(); }),
    ("mask", |j| { let _ = j.mask("a tok-3f9a1c"); }),
    ("check_credentials", |j| j.check_credentials().unwrap()),
    ("preflight", |j| j.preflight().unwrap()),
    ("trigger", |j| { let _ = j.trigger(REF_1_1).unwrap(); }),
    ("queue_item", |j| { let _ = j.queue_item("http://jenkins.example:8080/queue/item/7/").unwrap(); }),
    ("find_build", |j| { let _ = j.find_build(REF_1_1, &[]).unwrap(); }),
    ("retained_builds", |j| { let _ = j.retained_builds().unwrap(); }),
    ("build_result", |j| { let _ = j.build_result(41).unwrap(); }),
    ("quality_file", |j| { let _ = j.quality_file(41).unwrap(); }),
    ("failed_tests", |j| { let _ = j.failed_tests(41).unwrap(); }),
    ("console_tail", |j| { let _ = j.console_tail(41).unwrap(); }),
];

#[test]
fn credentials_travel_only_in_the_authorization_header() {
    let rule = "the token travels only in the Authorization header, never in a URL, and only to the server asked \
                (docs/quality-gate.md, Credentials and permissions)";
    // Every public method of Jenkins, read from its source.
    let source = include_str!("../ci.rs");
    let body = &source[source.find("impl Jenkins {").unwrap()..];
    let body = &body[..body.find("\n}\n").unwrap()];
    let public: Vec<&str> = body.lines().filter_map(|l| l.strip_prefix("    pub fn ")).map(|l| &l[..l.find(['(', '<']).unwrap()]).filter(|n| !["new", "from_env"].contains(n)).collect();
    assert_eq!(public, CALLS.iter().map(|(n, _)| *n).collect::<Vec<_>>(), "CALLS calls each public method of Jenkins once");

    // The one place that sets an Authorization header, among the engine's sources.
    let src = concat!(env!("CARGO_MANIFEST_DIR"), "/src");
    let mut setters = Vec::new();
    let mut dirs = vec![std::path::PathBuf::from(src)];
    while let Some(dir) = dirs.pop() {
        for entry in std::fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            let name = path.file_name().unwrap().to_string_lossy().to_string();
            if path.is_dir() {
                dirs.push(path);
            } else if name.ends_with(".rs") && name != "tests.rs" && name != "fake.rs" {
                let text = std::fs::read_to_string(&path).unwrap();
                setters.extend(text.lines().filter(|l| l.contains("\"Authorization\"") && !l.trim_start().starts_with("//")).map(|l| format!("{name}: {}", l.trim())));
            }
        }
    }
    assert_eq!(setters, ["ci.rs: headers: vec![(\"Authorization\".to_string(), auth)],"], "{rule}");

    let token = "tok-3f9a1c";
    let any = Arc::new(AnyJenkins { requests: Mutex::new(Vec::new()) });
    let env: Map<String> = [("JENKINS_URL", "http://jenkins.example:8080"), ("JENKINS_USER", "agents"), ("JENKINS_TOKEN", token)]
        .into_iter()
        .map(|(k, v)| (k, v.to_string()))
        .collect();
    let jenkins = Jenkins::new("Folder/quality", &env, CI_ENV_PATH, Arc::clone(&any) as Arc<dyn Http>);
    for (_, call) in CALLS {
        call(&jenkins);
    }
    let requests = any.requests.lock().unwrap();
    assert!(requests.len() >= CALLS.len() - 4, "job, job_url, mask and check_credentials ask nothing");
    for request in requests.iter() {
        assert!(!request.url.contains(token), "{rule}: {}", request.url);
        assert!(!request.form.as_deref().unwrap_or("").contains(token), "{rule}");
        assert!(request.header("Authorization").is_some_and(|a| a.starts_with("Basic ")), "{rule}");
    }
}

#[test]
fn basic_auth_is_base64_of_user_and_password() {
    assert_eq!(basic_auth("a", ""), "Basic YTo=");
    assert_eq!(basic_auth("ab", "c"), "Basic YWI6Yw==");
    assert_eq!(basic_auth("u", "é"), "Basic dTrDqQ==");
}

/// tests/contract/test_jenkins.py's chain of calls, which a quality round makes, on FakeJenkins: each answer the
/// fake gives is one Jenkins reads.
#[test]
fn a_quality_round_s_calls_read_the_fakes_answers() {
    let fake = FakeJenkins::new();
    fake.state().outcomes.push_back(Outcome {
        tests: vec![("tests.T".to_string(), "test_x".to_string(), "AssertionError: 1 != 2".to_string())],
        console: "Started\nFinished: UNSTABLE".to_string(),
        description: Some("judged".to_string()),
        ..Outcome::gate("ERROR", &["`a.py:3` error ruff F401: unused"])
    });
    let jenkins = jenkins_on(&fake);
    jenkins.preflight().unwrap();
    let queue_url = jenkins.trigger(REF_1_1).unwrap();
    assert_eq!(queue_url, format!("{JENKINS}/queue/item/941/"));
    assert_eq!(jenkins.queue_item(&queue_url).unwrap(), QueueItem { cancelled: false, build: None });
    assert_eq!(jenkins.queue_item(&queue_url).unwrap(), QueueItem { cancelled: false, build: Some(41) });
    assert_eq!(jenkins.find_build(REF_1_1, &[]).unwrap(), Some(41));
    assert_eq!(jenkins.find_build(REF_1_1, &[41]).unwrap(), None);
    assert_eq!(jenkins.retained_builds().unwrap(), [QualityBuild { number: 41, result: None, description: Some("judged".to_string()) }]);
    assert_eq!(jenkins.quality_file(41).unwrap(), None);
    assert_eq!(jenkins.build_result(41).unwrap(), None);
    assert_eq!(jenkins.build_result(41).unwrap(), None);
    assert_eq!(jenkins.build_result(41).unwrap().as_deref(), Some("UNSTABLE"));
    assert!(jenkins.quality_file(41).unwrap().unwrap().starts_with("GATE: ERROR\n"));
    assert_eq!(jenkins.failed_tests(41).unwrap().unwrap(), [("tests.T.test_x".to_string(), "AssertionError: 1 != 2".to_string())]);
    assert_eq!(jenkins.console_tail(41).unwrap(), "Started\nFinished: UNSTABLE");
    assert_eq!(jenkins.retained_builds().unwrap()[0].result.as_deref(), Some("UNSTABLE"));
    let state = fake.state();
    assert_eq!(state.names, ["parameters", "trigger", "queue", "queue", "find", "find", "builds", "quality", "build", "build", "build", "quality", "tests", "console", "builds"]);
    assert_eq!(state.triggered(), [REF_1_1]);
    assert_eq!(state.requests[1].form, [("GIT_REF", REF_1_1.to_string())].into_iter().collect());
    assert!(state.auth.iter().all(|a| a.as_deref() == Some("Basic YWdlbnRzOmplbmtpbnMtc2VjcmV0LXRva2Vu")));
}

#[test]
fn the_fake_plays_a_gone_job_forgotten_queue_failures_and_builds_without_a_report() {
    let fake = FakeJenkins::new();
    let jenkins = jenkins_on(&fake);
    let number = {
        let mut state = fake.state();
        state.add_build("r", Outcome { test_report: false, polls: 0, ..Outcome::unarchived() })
    };
    assert_eq!(jenkins.failed_tests(number).unwrap(), None);
    assert_eq!(jenkins.quality_file(number).unwrap(), None);
    assert_eq!(jenkins.build_result(number).unwrap().as_deref(), Some("FAILURE"));
    assert_eq!(jenkins.build_result(99).unwrap_err().status(), Some(404));
    assert_eq!(jenkins.queue_item(&format!("{JENKINS}/queue/item/5/")).unwrap_err().status(), Some(404));
    fake.state().fail.push((("GET", format!("{JOB_PATH}{number}/consoleText")), 502));
    assert_eq!(jenkins.console_tail(number).unwrap_err().status(), Some(502));
    let queue_url = jenkins.trigger("s").unwrap();
    fake.state().forgotten = true;
    assert_eq!(jenkins.queue_item(&queue_url).unwrap_err().status(), Some(404));
    fake.state().parameters.clear();
    assert!(jenkins.preflight().unwrap_err().to_string().starts_with(&format!("Jenkins job {JOB} lacks the parameters GIT_REF; build it once by hand")));
    fake.state().job_gone = true;
    assert_eq!(jenkins.preflight().unwrap_err().to_string(), format!("there is no Jenkins job {JOB} at JENKINS_URL: GET {JENKINS}{JOB_PATH}api/json?tree=property%5BparameterDefinitions%5Bname%5D%5D: HTTP 404"));
    assert!(jenkins.trigger("t").unwrap_err().to_string().starts_with(&format!("Jenkins job {JOB} is gone: POST ")));
    assert_eq!(fake.state().names.last().map(String::as_str), Some(format!("POST {JOB_PATH}buildWithParameters").as_str()));
    fake.state().down = true;
    assert!(jenkins.preflight().unwrap_err().to_string().starts_with("Jenkins at JENKINS_URL failed the preflight: GET "));
    assert_eq!(split_url("https://h/p?q=1"), ("/p".to_string(), "q=1".to_string()));
}

/// A Jenkins answering every GET with body.
fn answering(body: &str) -> Jenkins {
    struct Fixed(String);
    impl Http for Fixed {
        fn send(&self, _: &Request) -> Result<Response, HttpError> {
            Ok(Response::ok(self.0.clone()))
        }
    }
    Jenkins::new(JOB, &credentials(), CI_ENV_PATH, Arc::new(Fixed(body.to_string())))
}

#[test]
fn answers_of_another_shape_are_errors_that_may_pass() {
    let shape = |e: CIError, what: &str| {
        assert!(e.to_string().ends_with(&format!(": the answer is not {what}")), "{e}");
        assert!(e.transient());
    };
    shape(answering(r#"{"executable": [1]}"#).queue_item("q/").unwrap_err(), "a queue item");
    shape(answering(r#"{"executable": {"number": "5"}}"#).queue_item("q/").unwrap_err(), "a queue item");
    shape(answering(r#"{"executable": {"number": 18446744073709551615}}"#).queue_item("q/").unwrap_err(), "a queue item");
    assert_eq!(answering(r#"{"cancelled": true, "executable": {}}"#).queue_item("q/").unwrap(), QueueItem { cancelled: true, build: None });
    shape(answering(r#"{"builds": [1]}"#).find_build("r", &[]).unwrap_err(), "the job's builds");
    shape(answering(r#"{"builds": [{"actions": [{"parameters": [1]}]}]}"#).find_build("r", &[]).unwrap_err(), "the job's builds");
    assert_eq!(answering(r#"{"builds": [{"number": 3, "actions": [7, {"parameters": [{"name": "GIT_REF", "value": "r"}]}]}]}"#).find_build("r", &[]).unwrap(), Some(3));
    for builds in [r#"{"builds": {}}"#, r#"{"builds": [{"number": true}]}"#, r#"{"builds": [{"number": 1, "result": 2}]}"#, r#"{"builds": [{"number": 1, "description": []}]}"#] {
        shape(answering(builds).retained_builds().unwrap_err(), "the job's builds");
    }
    shape(answering(r#"{"building": false, "result": 1}"#).build_result(1).unwrap_err(), "the build's result");
    assert_eq!(answering(r#"{"building": 1}"#).build_result(1).unwrap(), None);
    shape(answering(r#"{"suites": [1]}"#).failed_tests(1).unwrap_err(), "a test report");
    shape(answering(r#"{"suites": [{"cases": [null]}]}"#).failed_tests(1).unwrap_err(), "a test report");
    let odd = r#"{"suites": [{"cases": [{"status": "REGRESSION", "className": null, "name": 3, "errorDetails": ["x"]}, {"status": "FAILED", "name": "t", "className": "c", "errorDetails": ""}]}, {}]}"#;
    assert_eq!(answering(odd).failed_tests(1).unwrap().unwrap(), [("None.3".to_string(), "['x']".to_string()), ("c.t".to_string(), String::new())]);
}

#[test]
fn the_console_s_tail_is_its_last_lines_as_python_splits_them() {
    let console: String = (0..70).map(|i| format!("line {i}\r\n")).collect::<String>() + "last\u{2028}end";
    let tail = answering(&console).console_tail(1).unwrap();
    let lines: Vec<&str> = tail.split('\n').collect();
    assert_eq!(lines.len(), CONSOLE_TAIL_LINES);
    assert_eq!((lines[0], lines[58], lines[59]), ("line 12", "last", "end"));
}

#[test]
fn a_queue_item_is_named_by_its_digits() {
    assert_eq!(queue_item_number("http://x/queue/item/77/"), Some("77"));
    assert_eq!(queue_item_number("/queue/item/٣"), Some("٣"));
    assert_eq!(queue_item_number("/queue/item/"), None);
    assert_eq!(queue_item_number("/queue/item/7a/"), None);
}

#[test]
fn errors_convert_and_say_what_they_hold() {
    let refused = CIError::from(OrchestratorError::new("no"));
    assert_eq!((refused.status(), refused.transient(), refused.to_string()), (None, false, "no".to_string()));
    assert_eq!(OrchestratorError::from(refused), OrchestratorError::new("no"));
    let request = CIError::Request { message: "GET u: HTTP 502".to_string(), status: Some(502) };
    assert!(request.transient());
    assert_eq!(OrchestratorError::from(request), OrchestratorError::new("GET u: HTTP 502"));
}

#[test]
fn from_env_reads_the_ci_env_beside_the_environment() {
    let dir = crate::test_support::TempDir::new();
    let config = ConfigDir::at(&dir.path());
    std::fs::write(config.ci_env_path(), "JENKINS_USER=agents\n").unwrap();
    std::fs::set_permissions(config.ci_env_path(), std::os::unix::fs::PermissionsExt::from_mode(0o644)).unwrap();
    let all_set = CI_ENV.iter().all(|k| std::env::var(k).is_ok_and(|v| !v.is_empty()));
    match Jenkins::from_env(JOB, &config) {
        Ok(jenkins) => assert!(all_set && jenkins.job() == JOB),
        Err(e) => assert!(!all_set && e.to_string().ends_with(&format!("holds tokens but others may read it; run: chmod 600 {}", config.ci_env_path())), "{e}"),
    }
    std::fs::set_permissions(config.ci_env_path(), std::os::unix::fs::PermissionsExt::from_mode(0o600)).unwrap();
    assert_eq!(Jenkins::from_env(JOB, &config).unwrap().job(), JOB);
}
