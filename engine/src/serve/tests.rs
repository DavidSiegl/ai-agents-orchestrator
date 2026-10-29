//! TestServeEndpoints, TestServeSecurity, TestServeEvents, TestServeEventsOnAMachine and TestServeCommand's in-process
//! cases: a RunServer on a free port of 127.0.0.1, asked over a TCP connection of the test's own, so that each header
//! and byte on the wire is the test's. The process cases are engine/tests/program.rs'.

use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::sync::mpsc;
use std::time::Instant;

use super::*;
use crate::cli::listing::run_record;
use crate::constants::{APPROVE, RUNS_DIR, STALE_SECONDS};
use crate::py::json::Map;
use crate::py::url::urlencode;
use crate::serve::fake::ServeHost;

const SERVE_ID: &str = "20261024-120000-5e7e00";
const TAURI_ORIGIN: &str = "tauri://localhost";

fn run_dir() -> String {
    format!("/proj/{RUNS_DIR}/{SERVE_ID}")
}

/// serve_state(**kw) of the Python tests, for run_id, as state.json's text.
fn serve_state(run_id: &str, extra: &[(&str, Json)]) -> String {
    let mut state: Map<Json> = [
        ("run_id", Json::from(run_id)),
        ("task", Json::from("add a rate limiter")),
        ("phase", Json::from("build")),
        ("round", Json::from(1)),
    ]
    .into_iter()
    .collect();
    for (key, value) in extra {
        state.insert(*key, value.clone());
    }
    Json::Object(state).dumps()
}

/// A ServeHost for /proj that holds the run's state, as serve_state() saves it.
fn host_with_run(ssh_target: Option<&str>) -> Arc<ServeHost> {
    let host = Arc::new(ServeHost::on(ssh_target));
    host.state().files.insert(format!("{}/state.json", run_dir()), serve_state(SERVE_ID, &[]));
    host
}

/// A RunServer for a test, and the projects it connected, as (machine, cwd).
struct Served {
    server: RunServer,
    port: u16,
    connects: Arc<Mutex<Vec<(String, String)>>>,
}

impl Served {
    /// A server whose every project is host's /proj.
    fn fake(host: &Arc<ServeHost>, origins: &[&str]) -> Served {
        Served::fake_polling(host, origins, SERVE_POLL)
    }

    fn fake_polling(host: &Arc<ServeHost>, origins: &[&str], polls: Polls) -> Served {
        let host = Arc::clone(host);
        let connects = Arc::new(Mutex::new(vec![]));
        let seen = Arc::clone(&connects);
        let connect = move |machine: &str, cwd: &str, _: &Interrupt| {
            seen.lock().unwrap().push((machine.to_string(), cwd.to_string()));
            let host: Arc<dyn Host + Send + Sync> = host.clone();
            Ok((host, "/proj".to_string()))
        };
        Served::start(Box::new(connect), origins, polls, connects)
    }

    /// A server whose projects are the real directories here.
    fn real() -> Served {
        Served::start(Box::new(project_host), &[], SERVE_POLL, Arc::default())
    }

    fn start(connect: Box<Connect>, origins: &[&str], polls: Polls, connects: Arc<Mutex<Vec<(String, String)>>>) -> Served {
        let origins: Vec<String> = origins.iter().map(|o| o.to_string()).collect();
        let server = RunServer::start_polling(&origins, connect, polls).expect("the server starts");
        let port = port_of(server.url());
        Served { server, port, connects }
    }

    fn token(&self) -> String {
        self.server.token().to_string()
    }

    /// The reply to a GET of target with the token as a Bearer header, the server's Host and the headers given.
    fn get(&self, target: &str, headers: &[(&str, &str)]) -> Reply {
        let bearer = format!("Bearer {}", self.token());
        let mut all = vec![("Authorization", bearer.as_str())];
        all.extend_from_slice(headers);
        self.ask("GET", target, &all)
    }

    /// The reply to method of target, with the server's Host header unless headers name one, and headers, each value's
    /// characters sent as Latin-1 bytes, as http.client sends a header's str.
    fn ask(&self, method: &str, target: &str, headers: &[(&str, &str)]) -> Reply {
        let mut raw = format!("{method} {target} HTTP/1.1\r\nConnection: close\r\n").into_bytes();
        if !headers.iter().any(|(name, _)| name.eq_ignore_ascii_case("Host")) {
            raw.extend_from_slice(format!("Host: 127.0.0.1:{}\r\n", self.port).as_bytes());
        }
        for (name, value) in headers {
            raw.extend_from_slice(format!("{name}: ").as_bytes());
            raw.extend(value.chars().map(|c| u8::try_from(u32::from(c)).expect("a Latin-1 header")));
            raw.extend_from_slice(b"\r\n");
        }
        raw.extend_from_slice(b"\r\n");
        exchange(self.port, &raw)
    }

    /// endpoint with cwd=~/proj and params as its query.
    fn query(endpoint: &str, params: &[(&str, &str)]) -> String {
        let mut all = vec![("cwd", "~/proj")];
        all.extend_from_slice(params);
        format!("{endpoint}?{}", urlencode(&all))
    }
}

fn port_of(url: &str) -> u16 {
    url.strip_prefix("http://127.0.0.1:").and_then(|p| p.parse().ok()).expect("a url of 127.0.0.1 with a port")
}

/// A reply as the client read it: its status (None when the connection closed unanswered), headers and body.
#[derive(Debug)]
struct Reply {
    status: Option<u16>,
    headers: Vec<(String, String)>,
    body: String,
}

impl Reply {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(n, _)| n.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }

    fn json(&self) -> Json {
        assert_eq!(self.header("Content-Type"), Some("application/json"), "{self:?}");
        Json::parse(&self.body).expect("a JSON body")
    }

    fn error(&self) -> String {
        self.json().get("error").map(Json::python_str).expect("an error")
    }
}

/// Send raw on a new connection to port, and read the reply to the connection's end, or for 5 s at most: an event
/// stream the server should have refused never ends, and fails the test on what came meanwhile.
fn exchange(port: u16, raw: &[u8]) -> Reply {
    let mut conn = TcpStream::connect(("127.0.0.1", port)).expect("the server takes the connection");
    conn.write_all(raw).expect("the request is sent");
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut got = vec![];
    let mut buf = [0u8; 4096];
    while let Some(left) = deadline.checked_duration_since(Instant::now()).filter(|d| !d.is_zero()) {
        conn.set_read_timeout(Some(left)).unwrap();
        // A reset after the reply is read is the end of it too, as is a read that times out at the deadline.
        match conn.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => got.extend_from_slice(&buf[..n]),
        }
    }
    let text = String::from_utf8_lossy(&got).into_owned();
    let Some((head, body)) = text.split_once("\r\n\r\n") else {
        return Reply { status: None, headers: vec![], body: text };
    };
    let mut lines = head.split("\r\n");
    let status = lines.next().and_then(|l| l.split(' ').nth(1)).and_then(|s| s.parse().ok());
    let headers = lines.filter_map(|l| l.split_once(": ")).map(|(n, v)| (n.to_string(), v.to_string())).collect();
    Reply { status, headers, body: body.to_string() }
}

/// The status of a GET of / at port; None when the connection is refused or closed unanswered.
fn status_at(port: u16) -> Option<u16> {
    let mut conn = TcpStream::connect(("127.0.0.1", port)).ok()?;
    conn.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    conn.write_all(b"GET / HTTP/1.0\r\n\r\n").ok()?;
    let mut got = String::new();
    let _ = conn.read_to_string(&mut got);
    got.split(' ').nth(1).and_then(|s| s.parse().ok())
}

/// A directory of the test's own, removed when it is dropped.
struct TempDir(String);

impl TempDir {
    fn new(name: &str) -> TempDir {
        let base = std::env::var("TMPDIR").unwrap_or_else(|_| std::env::temp_dir().display().to_string());
        let dir = format!("{base}/octoflow-serve-{name}-{}", std::process::id());
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("a directory for the test");
        TempDir(dir)
    }

    fn write(&self, path: &str, text: &str) {
        let path = format!("{}/{path}", self.0);
        std::fs::create_dir_all(std::path::Path::new(&path).parent().unwrap()).unwrap();
        std::fs::write(path, text).expect("the file is written");
    }

    fn save(&self, run_id: &str, extra: &[(&str, Json)]) {
        self.write(&format!("{RUNS_DIR}/{run_id}/state.json"), &serve_state(run_id, extra));
    }

    fn query(&self, endpoint: &str) -> String {
        format!("{endpoint}?{}", urlencode(&[("cwd", &self.0)]))
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// record without the keys that age with every read.
fn without_aging(mut record: Map<Json>) -> Json {
    for key in runs::AGING_RECORD_KEYS {
        record.remove(key);
    }
    Json::Object(record)
}

// -- TestServeEndpoints ------------------------------------------------------------------------------------------

#[test]
fn runs_are_list_json_records_on_a_machine() {
    let host = Arc::new(ServeHost::on(Some("remote-host")));
    let owner = object([
        ("host", Json::from(crate::runs::local_host())),
        ("pid", Json::from(i64::from(std::process::id()))),
        ("started_at", Json::from("x")),
    ]);
    host.state().files.insert(format!("{}/state.json", run_dir()), serve_state(SERVE_ID, &[("owner", owner)]));
    host.state().ages.insert(SERVE_ID.to_string(), STALE_SECONDS + 1);
    let served = Served::fake(&host, &[]);
    let reply = served.get(&Served::query("/runs", &[("machine", "remote")]), &[]);
    assert_eq!(reply.status, Some(200));
    assert_eq!(*served.connects.lock().unwrap(), [("remote".to_string(), "~/proj".to_string())]);
    // As list --json --machine remote --cwd ~/proj prints it.
    let target = ["--machine", "remote", "--cwd", "~/proj"].map(String::from);
    let listed: Vec<Json> = host
        .run_states("/proj")
        .unwrap()
        .iter()
        .map(|(age, s)| Json::Object(run_record(s, *age, &crate::runs::local_host(), &crate::runs::pid_alive, &target)))
        .collect();
    let doc = reply.json();
    assert_eq!(doc, object([("version", Json::from(1)), ("runs", Json::Array(listed))]));
    assert_eq!(
        doc.get("runs").and_then(Json::as_array).and_then(|r| r[0].get("resume_command")),
        Some(&Json::from("orchestrator.py resume 5e7e00 --machine remote --cwd '~/proj'"))
    );
}

#[test]
fn runs_are_list_json_records_locally() {
    let d = TempDir::new("records");
    d.save(SERVE_ID, &[]);
    d.save("x-beef00", &[("phase", Json::from("done")), ("verdict", Json::from(APPROVE))]);
    let served = Served::real();
    let reply = served.get(&d.query("/runs"), &[]);
    assert_eq!(reply.status, Some(200));
    let target = ["--cwd".to_string(), d.0.clone()];
    // A second may pass between the two reads; everything else is the same.
    let listed: Vec<Json> = ProcessHost::local(&Interrupt::new())
        .run_states(&d.0)
        .unwrap()
        .iter()
        .map(|(age, s)| without_aging(run_record(s, *age, &crate::runs::local_host(), &crate::runs::pid_alive, &target)))
        .collect();
    let doc = reply.json();
    assert_eq!(doc.get("version"), Some(&Json::from(1)));
    let served_runs: Vec<Json> =
        doc.get("runs").and_then(Json::as_array).unwrap().iter().map(|r| without_aging(r.as_object().unwrap().clone())).collect();
    assert_eq!(served_runs.len(), 2);
    assert_eq!(served_runs, listed);
}

#[test]
fn a_project_is_connected_once() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    served.get(&Served::query("/runs", &[]), &[]);
    served.get(&Served::query("/runs", &[]), &[]);
    served.get(&Served::query("/runs", &[("machine", "")]), &[]);
    assert_eq!(*served.connects.lock().unwrap(), [(String::new(), "~/proj".to_string())]);
}

#[test]
fn files_with_their_mtimes() {
    let host = host_with_run(None);
    {
        let mut s = host.state();
        s.files.insert(format!("{}/spec.md", run_dir()), "# Spec\n".to_string());
        s.files.insert(format!("{}/build-1.md", run_dir()), "built".to_string());
        s.mtimes.insert(format!("{}/spec.md", run_dir()), 100);
        s.mtimes.insert(format!("{}/build-1.md", run_dir()), 200);
    }
    let served = Served::fake(&host, &[]);
    let reply = served.get(&Served::query("/files", &[("run", SERVE_ID)]), &[]);
    assert_eq!(reply.status, Some(200));
    assert_eq!(
        reply.body,
        format!(r#"{{"run": "{SERVE_ID}", "files": [{{"name": "spec.md", "mtime": 100}}, {{"name": "build-1.md", "mtime": 200}}]}}"#)
    );
}

#[test]
fn file() {
    let host = Arc::new(ServeHost::on(None));
    host.state().files.insert(format!("{}/spec.md", run_dir()), "# Spec\n".to_string());
    let served = Served::fake(&host, &[]);
    let reply = served.get(&Served::query("/file", &[("run", SERVE_ID), ("name", "spec.md")]), &[]);
    assert_eq!(reply.status, Some(200));
    assert_eq!(reply.body, format!(r##"{{"run": "{SERVE_ID}", "name": "spec.md", "text": "# Spec\n"}}"##));
}

#[test]
fn missing_file() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let reply = served.get(&Served::query("/file", &[("run", SERVE_ID), ("name", "review-1.md")]), &[]);
    assert_eq!(reply.status, Some(404));
    assert_eq!(reply.error(), format!("no file review-1.md in run {SERVE_ID}"));
}

#[test]
fn paths_outside_the_run_directory_are_refused_unread() {
    let host = host_with_run(None);
    host.state().files.insert("/proj/secret".to_string(), "secret".to_string());
    let served = Served::fake(&host, &[]);
    let run_dot_dot = format!("{SERVE_ID}/..");
    let cases: [(&str, &str); 11] = [
        (SERVE_ID, "../../../secret"),
        (SERVE_ID, "/proj/secret"),
        (SERVE_ID, "state.json"),
        (SERVE_ID, ".."),
        (SERVE_ID, "."),
        (SERVE_ID, ""),
        (SERVE_ID, "spec\0.md"),
        ("..", "secret"),
        (".", "secret"),
        (&run_dot_dot, "secret"),
        ("/proj", "secret"),
    ];
    for (run, name) in cases {
        let reply = served.get(&Served::query("/file", &[("run", run), ("name", name)]), &[]);
        assert_eq!(reply.status, Some(403), "{run:?} {name:?}");
        assert_eq!(reply.json().get("text"), None);
        let why = if name == "state.json" {
            "state.json is not a handoff file".to_string()
        } else {
            format!("{} is not a name in the run directory", repr(if run == SERVE_ID { name } else { run }))
        };
        assert_eq!(reply.error(), why, "{run:?} {name:?}");
    }
    assert_eq!(host.state().reads, Vec::<String>::new());
}

#[test]
fn files_of_a_run_outside_the_runs_directory_are_refused() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    for run in ["../..", "..", "/proj", ""] {
        assert_eq!(served.get(&Served::query("/files", &[("run", run)]), &[]).status, Some(403), "{run:?}");
    }
}

#[test]
fn a_link_in_the_run_directory_is_followed_as_python_follows_it() {
    // Python's Host.read cats the path, so a symbolic link an agent left in the run directory is read through; the
    // name is still a plain name (TODO.md, "serve reads a run's files through a symbolic link").
    let d = TempDir::new("link");
    d.save(SERVE_ID, &[]);
    d.write("outside.txt", "outside");
    std::os::unix::fs::symlink(format!("{}/outside.txt", d.0), format!("{}/{RUNS_DIR}/{SERVE_ID}/link.md", d.0)).unwrap();
    let served = Served::real();
    let reply = served.get(&format!("/file?{}", urlencode(&[("cwd", &d.0), ("run", SERVE_ID), ("name", "link.md")])), &[]);
    assert_eq!((reply.status, reply.json().get("text")), (Some(200), Some(&Json::from("outside"))));
    // A run directory that is a link is listed through it, as Python lists it.
    std::os::unix::fs::symlink(&d.0, format!("{}/{RUNS_DIR}/linked", d.0)).unwrap();
    let reply = served.get(&format!("/files?{}", urlencode(&[("cwd", &d.0), ("run", "linked")])), &[]);
    let names: Vec<String> = reply.json().get("files").and_then(Json::as_array).unwrap().iter().map(|f| f.get("name").unwrap().python_str()).collect();
    assert_eq!((reply.status, names), (Some(200), vec!["outside.txt".to_string()]));
}

#[test]
fn missing_parameter() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    for (path, missing) in [
        ("/runs".to_string(), "cwd"),
        (Served::query("/file", &[("run", SERVE_ID)]), "name"),
        (Served::query("/files", &[]), "run"),
        ("/file".to_string(), "cwd, run, name"),
        ("/events?machine=x".to_string(), "cwd"),
    ] {
        let reply = served.get(&path, &[]);
        assert_eq!(reply.status, Some(400), "{path}");
        assert_eq!(reply.error(), format!("missing parameter {missing}"));
    }
    assert_eq!(*served.connects.lock().unwrap(), Vec::<(String, String)>::new());
}

#[test]
fn unknown_endpoint() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let reply = served.get(&Served::query("/state", &[]), &[]);
    assert_eq!((reply.status, reply.error()), (Some(404), "no endpoint /state".to_string()));
}

#[test]
fn an_unreadable_project_is_a_bad_gateway() {
    let connect = |_: &str, _: &str, _: &Interrupt| Err(OrchestratorError::new("no saved herdr machine named nosuch"));
    let served = Served::start(Box::new(connect), &[], SERVE_POLL, Arc::default());
    let reply = served.get(&Served::query("/runs", &[("machine", "nosuch")]), &[]);
    assert_eq!(reply.status, Some(502));
    assert_eq!(reply.body, r#"{"error": "no saved herdr machine named nosuch"}"#);
}

#[test]
fn runs_that_cannot_be_read_are_a_bad_gateway() {
    let host = Arc::new(ServeHost::on(None));
    host.state().run_states_error = Some("ssh: connection refused".to_string());
    let served = Served::fake(&host, &[]);
    let reply = served.get(&Served::query("/runs", &[]), &[]);
    assert_eq!((reply.status, reply.error()), (Some(502), "ssh: connection refused".to_string()));
}

#[test]
fn a_target_read_as_http_server_reads_it() {
    // A path that starts with //, a fragment, an absolute URL, and a parameter given twice, the last one counting.
    let host = Arc::new(ServeHost::on(None));
    host.state().files.insert(format!("{}/spec.md", run_dir()), "x".to_string());
    let served = Served::fake(&host, &[]);
    let base = format!("cwd=%7E%2Fproj&run={SERVE_ID}&name=nope&name=spec.md");
    for target in [format!("//file?{base}"), format!("/file?{base}#frag"), format!("http://127.0.0.1:{}/file?{base}", served.port)] {
        let reply = served.get(&target, &[]);
        assert_eq!((reply.status, reply.json().get("text")), (Some(200), Some(&Json::from("x"))), "{target}");
    }
    let reply = served.get(&format!("/file?{base}&name=..%2Fx"), &[]);
    assert_eq!((reply.status, reply.error()), (Some(403), "'../x' is not a name in the run directory".to_string()));
}

// -- TestServeSecurity -------------------------------------------------------------------------------------------

#[test]
fn no_token() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let reply = served.ask("GET", &Served::query("/runs", &[]), &[]);
    assert_eq!(reply.status, Some(401));
    assert_eq!(reply.header("WWW-Authenticate"), Some("Bearer"));
    assert_eq!(reply.error(), "the token is missing or wrong");
    assert_eq!(*served.connects.lock().unwrap(), Vec::<(String, String)>::new());
}

#[test]
fn wrong_token() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let token = served.token();
    let longer = format!("Bearer {token}x");
    let shorter = format!("Bearer {}", &token[..token.len() - 1]);
    let last = if token.ends_with('A') { 'B' } else { 'A' };
    let other_last = format!("Bearer {}{last}", &token[..token.len() - 1]);
    let lower = format!("bearer {token}");
    for header in ["Bearer nope", &token, &longer, &shorter, &other_last, &lower, "Bearer"] {
        let reply = served.ask("GET", &Served::query("/runs", &[]), &[("Authorization", header)]);
        assert_eq!(reply.status, Some(401), "{header:?}");
    }
    for query_token in ["nope", "", &token[1..], &format!("{token}x"), &format!("Bearer {token}")] {
        let reply = served.ask("GET", &Served::query("/runs", &[("token", query_token)]), &[]);
        assert_eq!(reply.status, Some(401), "{query_token:?}");
    }
}

#[test]
fn non_ascii_tokens() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    // é's UTF-8 bytes on the wire, as http.client sends a header's str as Latin-1. Python answers 401; tiny_http
    // closes the connection of a request with a header beyond ASCII unanswered (TODO.md, "serve answers some requests
    // otherwise than Python's http.server").
    for header in ["Bearer \u{c3}\u{a9}".to_string(), format!("Bearer {}\u{c3}\u{a9}", served.token())] {
        let reply = served.ask("GET", &Served::query("/runs", &[]), &[("Authorization", &header)]);
        assert_eq!(reply.status, None, "{header:?}");
    }
    // é, an encoded lone surrogate, and a byte that is not UTF-8.
    for token in ["%C3%A9", "%ED%B2%80", "%FF"] {
        let reply = served.ask("GET", &format!("{}&token={token}", Served::query("/runs", &[])), &[]);
        assert_eq!(reply.status, Some(401), "{token}");
    }
}

#[test]
fn token_in_the_query() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let token = served.token();
    assert_eq!(served.ask("GET", &Served::query("/runs", &[("token", &token)]), &[]).status, Some(200));
    // Either place is enough: a wrong token in the other does not undo it.
    let reply = served.ask("GET", &Served::query("/runs", &[("token", &token)]), &[("Authorization", "Bearer x")]);
    assert_eq!(reply.status, Some(200));
    assert_eq!(served.get(&Served::query("/runs", &[("token", "x")]), &[]).status, Some(200));
}

#[test]
fn tokens_differ_per_server() {
    let host = Arc::new(ServeHost::on(None));
    let (one, other) = (Served::fake(&host, &[]), Served::fake(&host, &[]));
    assert_ne!(one.token(), other.token());
    assert_eq!(one.token().len(), 43);
}

#[test]
fn tokens_are_32_random_bytes_in_base64url() {
    // Every character of base64url's 64 turns up among a few hundred tokens; the last of each token's 43 holds the 4
    // bits left of 32 bytes, its low 2 bits 0, so it is one of 16.
    let tokens: Vec<String> = (0..300).map(|_| new_token().unwrap()).collect();
    let seen: std::collections::BTreeSet<char> = tokens.iter().flat_map(|t| t.chars()).collect();
    assert_eq!(seen.len(), 64, "{seen:?}");
    assert!(seen.iter().all(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_'), "{seen:?}");
    for t in &tokens {
        assert_eq!(t.len(), 43);
        assert!("AEIMQUYcgkosw048".contains(t.chars().last().unwrap()), "{t}");
    }
}

#[test]
fn bytes_are_the_same_only_when_equal() {
    assert!(same_bytes(b"", b""));
    assert!(same_bytes(b"abc", b"abc"));
    let differ: [(&[u8], &[u8]); 7] =
        [(b"abd", b"abc"), (b"xbc", b"abc"), (b"ab", b"abc"), (b"abcd", b"abc"), (b"", b"a"), (b"a", b""), (b"abc\0", b"abc")];
    for (given, expected) in differ {
        assert!(!same_bytes(given, expected), "{given:?} {expected:?}");
    }
}

#[test]
fn bound_to_loopback_only() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    assert_eq!(served.server.url(), format!("http://127.0.0.1:{}", served.port));
    let http = lock(&served.server.listening).clone().expect("it listens");
    let bound = http.server_addr().to_ip().map(|a| a.ip().to_string());
    assert_eq!(bound.as_deref(), Some("127.0.0.1"));
}

#[test]
fn foreign_host_header() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let port = served.port;
    for host in [
        "evil.example".to_string(),
        format!("evil.example:{port}"),
        "127.0.0.1".to_string(),
        format!("127.0.0.1:{}", port + 1),
        format!("LOCALHOST:{port}"),
        format!("[::1]:{port}"),
        String::new(),
    ] {
        let reply = served.get(&Served::query("/runs", &[]), &[("Host", &host)]);
        assert_eq!(reply.status, Some(403), "{host:?}");
        assert_eq!(reply.error(), "the Host or Origin header is not this server's");
    }
    assert_eq!(*served.connects.lock().unwrap(), Vec::<(String, String)>::new());
}

#[test]
fn a_request_without_a_host_header_is_refused() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let raw = format!("GET {} HTTP/1.0\r\nAuthorization: Bearer {}\r\n\r\n", Served::query("/runs", &[]), served.token());
    assert_eq!(exchange(served.port, raw.as_bytes()).status, Some(403));
}

#[test]
fn own_host_headers() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    for host in [format!("127.0.0.1:{}", served.port), format!("localhost:{}", served.port)] {
        assert_eq!(served.get(&Served::query("/runs", &[]), &[("Host", &host)]).status, Some(200), "{host}");
    }
}

#[test]
fn foreign_origin() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[TAURI_ORIGIN]);
    for origin in ["http://evil.example", "null", "http://localhost:1420", "", "TAURI://localhost"] {
        let reply = served.get(&Served::query("/runs", &[]), &[("Origin", origin)]);
        assert_eq!(reply.status, Some(403), "{origin:?}");
        assert_eq!(reply.header("Access-Control-Allow-Origin"), None);
    }
}

#[test]
fn foreign_origin_even_without_a_token() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let reply = served.ask("GET", &Served::query("/runs", &[]), &[("Origin", "http://evil.example")]);
    assert_eq!(reply.status, Some(403));
}

#[test]
fn the_webview_origin_is_allowed_by_cors() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[TAURI_ORIGIN]);
    let reply = served.get(&Served::query("/runs", &[]), &[("Origin", TAURI_ORIGIN)]);
    assert_eq!(reply.status, Some(200));
    assert_eq!(reply.header("Access-Control-Allow-Origin"), Some(TAURI_ORIGIN));
    assert_eq!(reply.header("Vary"), Some("Origin"));
    // A refusal after the Origin check allows it too, so the webview can read why.
    let reply = served.ask("GET", &Served::query("/runs", &[]), &[("Origin", TAURI_ORIGIN)]);
    assert_eq!((reply.status, reply.header("Access-Control-Allow-Origin")), (Some(401), Some(TAURI_ORIGIN)));
}

#[test]
fn own_origin_and_none() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let reply = served.get(&Served::query("/runs", &[]), &[]);
    assert_eq!(reply.status, Some(200));
    assert_eq!(reply.header("Access-Control-Allow-Origin"), None);
    for origin in [format!("http://localhost:{}", served.port), format!("http://127.0.0.1:{}", served.port)] {
        assert_eq!(served.get(&Served::query("/runs", &[]), &[("Origin", &origin)]).status, Some(200), "{origin}");
    }
}

#[test]
fn preflight_of_the_webview() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[TAURI_ORIGIN]);
    let reply = served.ask("OPTIONS", "/runs", &[("Origin", TAURI_ORIGIN)]);
    assert_eq!(reply.status, Some(204));
    assert_eq!(reply.header("Access-Control-Allow-Origin"), Some(TAURI_ORIGIN));
    assert_eq!(reply.header("Access-Control-Allow-Headers"), Some("Authorization"));
    assert_eq!(reply.header("Access-Control-Allow-Methods"), Some("GET"));
    assert_eq!(reply.body, "");
}

#[test]
fn preflight_of_a_foreign_origin() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[TAURI_ORIGIN]);
    let reply = served.ask("OPTIONS", "/runs", &[("Origin", "http://evil.example")]);
    assert_eq!(reply.status, Some(403));
    assert_eq!(reply.header("Access-Control-Allow-Origin"), None);
    let reply = served.ask("OPTIONS", "/runs", &[("Origin", TAURI_ORIGIN), ("Host", "evil.example")]);
    assert_eq!(reply.status, Some(403));
}

#[test]
fn writes_are_not_served() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    for method in ["POST", "PUT", "DELETE", "PATCH", "HEAD", "get"] {
        let reply = served.get(&Served::query("/runs", &[]), &[]);
        assert_eq!(reply.status, Some(200));
        let bearer = format!("Bearer {}", served.token());
        let reply = served.ask(method, &Served::query("/runs", &[]), &[("Authorization", &bearer)]);
        assert_eq!(reply.status, Some(501), "{method}");
        assert_eq!(reply.header("Content-Type"), Some("text/html;charset=utf-8"));
        if method != "HEAD" {
            assert!(reply.body.contains(&format!("<p>Message: Unsupported method ('{method}').</p>")), "{}", reply.body);
        }
    }
    assert_eq!(*served.connects.lock().unwrap(), [(String::new(), "~/proj".to_string())]);
}

// -- TestServeEvents and TestServeEventsOnAMachine ---------------------------------------------------------------

/// An event stream as an EventSource reads it, with the token in the URL, its events parsed on a thread as they
/// arrive. It ends when the server stops.
struct EventStream {
    /// The response's status line and headers.
    head: String,
    events: mpsc::Receiver<(String, Json)>,
    /// Gets one message when the response has ended.
    ended: mpsc::Receiver<()>,
    /// The connection, which the stream's reader shares, closed when the stream is dropped, as a client that goes.
    conn: TcpStream,
}

impl Drop for EventStream {
    fn drop(&mut self) {
        let _ = self.conn.shutdown(std::net::Shutdown::Both);
    }
}

impl EventStream {
    /// The stream of query, a path with its query, asked with extra_headers, each a line with its line end.
    fn open(served: &Served, query: &str, extra_headers: &str) -> EventStream {
        let mut conn = TcpStream::connect(("127.0.0.1", served.port)).unwrap();
        let target = format!("{query}&token={}", served.token());
        write!(conn, "GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n{extra_headers}\r\n", served.port).unwrap();
        let mut reader = BufReader::new(conn.try_clone().expect("the connection is shared"));
        let mut head = String::new();
        while !head.ends_with("\r\n\r\n") {
            assert_ne!(reader.read_line(&mut head).expect("the head"), 0, "the response ended in its head: {head}");
        }
        let (send, events) = mpsc::channel();
        let (end, ended) = mpsc::channel();
        std::thread::spawn(move || {
            let (mut kind, mut payload) = (None, None);
            each_chunked_line(reader, |line| {
                if let Some(k) = line.strip_prefix("event: ") {
                    kind = Some(k.to_string());
                } else if let Some(d) = line.strip_prefix("data: ") {
                    payload = Some(Json::parse(d).expect("JSON data"));
                } else if let (true, Some(k), Some(p)) = (line.is_empty(), kind.take(), payload.take()) {
                    let _ = send.send((k, p));
                }
            });
            let _ = end.send(());
        });
        EventStream { head, events, ended, conn }
    }

    /// The next event, as (kind, payload); fails the test when none comes within timeout.
    fn next(&self, timeout: Duration) -> (String, Json) {
        self.events.recv_timeout(timeout).unwrap_or_else(|_| panic!("no event within {timeout:?}"))
    }

    fn kinds(&self, n: usize) -> Vec<String> {
        (0..n).map(|_| self.next(Duration::from_secs(2)).0).collect()
    }
}

/// Call on_line with each line of a chunked body as it arrives, without its line end, until its last chunk or the
/// connection's end.
fn each_chunked_line(mut reader: BufReader<TcpStream>, mut on_line: impl FnMut(&str)) {
    let mut body = Vec::new();
    loop {
        let mut size = String::new();
        if reader.read_line(&mut size).unwrap_or(0) == 0 {
            return;
        }
        let n = usize::from_str_radix(size.trim(), 16).expect("a chunk's size");
        if n == 0 {
            return;
        }
        let mut chunk = vec![0; n + 2];
        if reader.read_exact(&mut chunk).is_err() {
            return;
        }
        assert_eq!(&chunk[n..], b"\r\n");
        body.extend_from_slice(&chunk[..n]);
        while let Some(i) = body.iter().position(|&b| b == b'\n') {
            let line: Vec<u8> = body.drain(..=i).collect();
            on_line(&String::from_utf8_lossy(&line[..i]));
        }
    }
}

#[test]
fn the_stream() {
    let d = TempDir::new("stream");
    d.save(SERVE_ID, &[]);
    d.write(&format!("{RUNS_DIR}/{SERVE_ID}/spec.md"), "# Spec\n");
    let served = Served::real();
    let stream = EventStream::open(&served, &d.query("/events"), "");
    assert!(stream.head.starts_with("HTTP/1.1 200 OK\r\n"), "{}", stream.head);
    assert!(stream.head.contains("\r\nContent-Type: text/event-stream\r\n"), "{}", stream.head);
    assert!(stream.head.contains("\r\nCache-Control: no-store\r\n"), "{}", stream.head);
    let (kind, record) = stream.next(Duration::from_secs(2));
    assert_eq!((kind.as_str(), record.get("run")), ("record", Some(&Json::from(SERVE_ID))));
    assert_eq!(record.get("record").and_then(|r| r.get("phase")), Some(&Json::from("build")));
    let (kind, files) = stream.next(Duration::from_secs(2));
    assert_eq!(kind, "files");
    let names = |files: &Json| -> Vec<String> {
        files.get("files").and_then(Json::as_array).unwrap().iter().map(|f| f.get("name").unwrap().python_str()).collect()
    };
    assert_eq!(names(&files), ["spec.md"]);

    // The next event is expected, and arrived within 2 s of started.
    let soon = |started: Instant, expected_kind: &str, run: &str| {
        let (kind, payload) = stream.next(Duration::from_secs(3));
        assert!(started.elapsed() <= Duration::from_secs(2), "{kind} after {:?}", started.elapsed());
        assert_eq!((kind.as_str(), payload.get("run")), (expected_kind, Some(&Json::from(run))));
        payload
    };
    let started = Instant::now();
    d.save(SERVE_ID, &[("phase", Json::from("review"))]);
    let record = soon(started, "record", SERVE_ID);
    assert_eq!(record.get("record").and_then(|r| r.get("phase")), Some(&Json::from("review")));

    let started = Instant::now();
    // A second later, so that the new file sorts after spec.md by its modification time.
    std::thread::sleep(Duration::from_millis(1100));
    d.write(&format!("{RUNS_DIR}/{SERVE_ID}/build-1.md"), "built");
    let files = soon(started + Duration::from_millis(1100), "files", SERVE_ID);
    assert_eq!(names(&files), ["spec.md", "build-1.md"]);

    let started = Instant::now();
    d.save("20261024-130000-a11ce0", &[]);
    soon(started, "record", "20261024-130000-a11ce0");
    assert_eq!(soon(started, "files", "20261024-130000-a11ce0").get("files"), Some(&Json::Array(vec![])));

    let started = Instant::now();
    std::fs::remove_dir_all(format!("{}/{RUNS_DIR}/{SERVE_ID}", d.0)).unwrap();
    assert_eq!(soon(started, "record", SERVE_ID).get("record"), Some(&Json::Null));
}

#[test]
fn nothing_while_nothing_changes() {
    let served = Served::fake(&host_with_run(None), &[]);
    let stream = EventStream::open(&served, &Served::query("/events", &[]), "");
    stream.kinds(2);
    assert!(stream.events.recv_timeout(Duration::from_millis(2500)).is_err());
}

#[test]
fn an_unreadable_project_is_a_problem_event() {
    let d = TempDir::new("problem");
    d.write(&format!("{RUNS_DIR}/{SERVE_ID}/state.json"), "{not json");
    let served = Served::real();
    let stream = EventStream::open(&served, &d.query("/events"), "");
    let (kind, payload) = stream.next(Duration::from_secs(2));
    assert_eq!(kind, "problem");
    assert!(payload.get("error").unwrap().python_str().contains("corrupt run state"), "{payload:?}");
    let started = Instant::now();
    d.save(SERVE_ID, &[]);
    assert_eq!(stream.next(Duration::from_secs(3)), ("problem".to_string(), object([("error", Json::Null)])));
    assert_eq!(stream.next(Duration::from_secs(3)).0, "record");
    assert!(started.elapsed() <= Duration::from_secs(2), "{:?}", started.elapsed());
}

#[test]
fn the_stream_ends_when_the_server_stops() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let stream = EventStream::open(&served, &Served::query("/events", &[]), "");
    drop(served);
    assert!(stream.ended.recv_timeout(Duration::from_secs(3)).is_ok());
}

#[test]
fn a_stream_to_an_http_1_0_client_ends_with_its_connection() {
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let raw = format!("GET {}&token={} HTTP/1.0\r\nHost: 127.0.0.1:{}\r\n\r\n", Served::query("/events", &[]), served.token(), served.port);
    let port = served.port;
    let reader = std::thread::spawn(move || exchange(port, raw.as_bytes()));
    std::thread::sleep(Duration::from_millis(300));
    drop(served);
    let reply = reader.join().unwrap();
    assert_eq!(reply.status, Some(200));
    assert_eq!(reply.header("Transfer-Encoding"), None);
    assert_eq!(reply.body, ":\n\n");
}

#[test]
fn a_client_that_goes_ends_its_stream() {
    // The stream's thread finds the client gone at a write, and polls no more.
    let host = host_with_run(None);
    let fast = Polls { local: Duration::from_millis(20), remote: Duration::from_millis(20) };
    let served = Served::fake_polling(&host, &[], fast);
    let stream = EventStream::open(&served, &Served::query("/events", &[]), "");
    stream.kinds(2);
    drop(stream);
    // A write or two may still succeed before the server sees the connection reset.
    std::thread::sleep(Duration::from_millis(500));
    let polls = host.state().run_states_calls;
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(host.state().run_states_calls, polls);
}

#[test]
fn changes_are_polled_on_a_machine() {
    let host = host_with_run(Some("remote-host"));
    let polls = Polls { local: Duration::from_secs(60), remote: Duration::from_millis(100) };
    let served = Served::fake_polling(&host, &[], polls);
    let stream = EventStream::open(&served, &Served::query("/events", &[("machine", "remote")]), "");
    assert_eq!(stream.kinds(2), ["record", "files"]);
    host.state().files.insert(format!("{}/review-1.md", run_dir()), "VERDICT: APPROVE\n".to_string());
    let (kind, payload) = stream.next(Duration::from_secs(2));
    assert_eq!(kind, "files");
    assert_eq!(payload.dumps(), format!(r#"{{"run": "{SERVE_ID}", "files": [{{"name": "review-1.md", "mtime": 0}}]}}"#));
    host.state().files.remove(&format!("{}/state.json", run_dir()));
    let gone = object([("run", Json::from(SERVE_ID)), ("record", Json::Null)]);
    assert_eq!(stream.next(Duration::from_secs(2)), ("record".to_string(), gone));
}

#[test]
fn a_machine_is_polled_less_often() {
    let host = host_with_run(Some("remote-host"));
    let polls = Polls { local: Duration::from_millis(100), remote: Duration::from_secs(60) };
    let served = Served::fake_polling(&host, &[], polls);
    let stream = EventStream::open(&served, &Served::query("/events", &[("machine", "remote")]), "");
    // The first poll, whenever the handler gets to it, sends the run; only a change after it waits for the next.
    assert_eq!(stream.kinds(2), ["record", "files"]);
    host.state().files.insert(format!("{}/state.json", run_dir()), serve_state(SERVE_ID, &[("phase", Json::from("review"))]));
    assert!(stream.events.recv_timeout(Duration::from_secs(1)).is_err());
}

#[test]
fn the_stream_allows_the_webview_origin() {
    let served = Served::fake(&host_with_run(None), &[TAURI_ORIGIN]);
    let stream = EventStream::open(&served, &Served::query("/events", &[]), &format!("Origin: {TAURI_ORIGIN}\r\n"));
    assert!(stream.head.contains(&format!("\r\nAccess-Control-Allow-Origin: {TAURI_ORIGIN}\r\nVary: Origin\r\n")), "{}", stream.head);
    assert_eq!(stream.kinds(2), ["record", "files"]);
}

#[test]
fn a_stream_is_refused_as_any_request() {
    let served = Served::fake(&host_with_run(None), &[TAURI_ORIGIN]);
    let events = Served::query("/events", &[]);
    assert_eq!(served.ask("GET", &events, &[]).status, Some(401));
    assert_eq!(served.ask("GET", &format!("{events}&token=x"), &[]).status, Some(401));
    let with_token = format!("{events}&token={}", served.token());
    assert_eq!(served.ask("GET", &with_token, &[("Origin", "http://evil.example")]).status, Some(403));
    assert_eq!(served.ask("GET", &with_token, &[("Host", "evil.example")]).status, Some(403));
}

// -- TestServeCommand's in-process cases -------------------------------------------------------------------------

/// A writer that hands each write to a channel, for a line printed while serve still runs.
struct Lines(mpsc::Sender<Vec<u8>>);

impl Write for Lines {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let _ = self.0.send(buf.to_vec());
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn in_process() {
    let host = Arc::new(ServeHost::on(None));
    let connect = move |_: &str, _: &str, _: &Interrupt| {
        let host: Arc<dyn Host + Send + Sync> = host.clone();
        Ok((host, "/proj".to_string()))
    };
    let (lines, printed) = mpsc::channel();
    let (stop, stopped) = mpsc::channel::<()>();
    let serving = std::thread::spawn(move || {
        let mut err = vec![];
        let status = serve(RunServer::start(&[], Box::new(connect)), &mut Lines(lines), &mut err, || {
            stopped.recv().map_err(io::Error::other)
        });
        (status, err)
    });
    let mut line = vec![];
    while !line.contains(&b'\n') {
        line.extend(printed.recv_timeout(Duration::from_secs(10)).expect("serve prints its line"));
    }
    let line = String::from_utf8(line).unwrap();
    assert!(line.ends_with('\n') && line.matches('\n').count() == 1, "{line:?}");
    let first = Json::parse(&line).unwrap();
    assert_eq!(first.as_object().unwrap().keys().collect::<Vec<_>>(), ["url", "token"]);
    let port = port_of(&first.get("url").unwrap().python_str());
    let token = first.get("token").unwrap().python_str();
    let raw = format!("GET /runs?cwd=x&token={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    let reply = exchange(port, raw.as_bytes());
    assert_eq!((reply.status, reply.body.as_str()), (Some(200), r#"{"version": 1, "runs": []}"#));
    stop.send(()).unwrap();
    let (status, err) = serving.join().unwrap();
    assert_eq!((status, err), (0, vec![]));
    assert_eq!(status_at(port), None, "the stopped server still answers");
}

/// A writer whose every write fails as a pipe whose reader has gone.
struct BrokenPipe;

impl Write for BrokenPipe {
    fn write(&mut self, _: &[u8]) -> io::Result<usize> {
        Err(io::Error::from_raw_os_error(32))
    }

    fn flush(&mut self) -> io::Result<()> {
        Err(io::Error::from_raw_os_error(32))
    }
}

#[test]
fn an_unprintable_line_ends_it() {
    let connect = |_: &str, _: &str, _: &Interrupt| Err(OrchestratorError::new("not asked"));
    let started = RunServer::start(&[], Box::new(connect)).unwrap();
    let port = port_of(started.url());
    let mut err = vec![];
    let status = serve(Ok(started), &mut BrokenPipe, &mut err, || -> io::Result<()> { panic!("serve waited") });
    assert_eq!(status, EXIT_ERROR);
    assert_eq!(String::from_utf8(err).unwrap(), "error: cannot print the server's address: [Errno 32] Broken pipe\n");
    assert_eq!(status_at(port), None, "the stopped server still answers");
}

#[test]
fn no_port() {
    let mut err = vec![];
    let unbound = Err(io::Error::from_raw_os_error(98));
    let status = serve(unbound, &mut Vec::new(), &mut err, || -> io::Result<()> { panic!("serve waited") });
    assert_eq!(status, EXIT_ERROR);
    assert_eq!(String::from_utf8(err).unwrap(), "error: cannot serve on 127.0.0.1: [Errno 98] Address already in use\n");
}

#[test]
fn a_wait_for_the_signals_that_fails_ends_it() {
    let connect = |_: &str, _: &str, _: &Interrupt| Err(OrchestratorError::new("not asked"));
    let started = RunServer::start(&[], Box::new(connect)).unwrap();
    let port = port_of(started.url());
    let (mut out, mut err) = (vec![], vec![]);
    let status = serve(Ok(started), &mut out, &mut err, || Err(io::Error::from_raw_os_error(22)));
    assert_eq!(status, EXIT_ERROR);
    assert!(String::from_utf8(out).unwrap().starts_with(r#"{"url": "http://127.0.0.1:"#));
    assert_eq!(String::from_utf8(err).unwrap(), "error: cannot wait for SIGINT and SIGTERM: [Errno 22] Invalid argument\n");
    assert_eq!(status_at(port), None, "the stopped server still answers");
}

#[test]
fn a_server_that_stops_accepting_is_started_again_on_the_port() {
    // As tiny_http's server ends at the first error accept gives: its recv fails while the RunServer is not stopping.
    let served = Served::fake(&Arc::new(ServeHost::on(None)), &[]);
    let first = lock(&served.server.listening).clone().expect("it listens");
    first.unblock();
    let started = Instant::now();
    while lock(&served.server.listening).as_ref().is_some_and(|http| Arc::ptr_eq(http, &first)) {
        assert!(started.elapsed() < Duration::from_secs(5), "no server was started again");
        std::thread::sleep(Duration::from_millis(10));
    }
    drop(first);
    assert_eq!(served.get(&Served::query("/runs", &[]), &[]).status, Some(200));
    let port = served.port;
    drop(served);
    assert_eq!(status_at(port), None, "the stopped server still answers");
}

#[test]
fn a_request_names_its_target_flags() {
    assert_eq!(target_args("", "~/proj"), ["--cwd", "~/proj"]);
    assert_eq!(target_args("box", "/p"), ["--machine", "box", "--cwd", "/p"]);
    assert_eq!(target_args("", ""), Vec::<String>::new());
}
