//! The world's servers, which the engine reaches over the network: Jenkins's REST API for the quality job, and an
//! HTTPS proxy that answers for PyPI. Each serves from threads of the test process on a free port of 127.0.0.1, reads
//! what to answer from the world's script, and logs each request it gets in the world's ledger, by the latest
//! invocation, the one whose requests these are in every scenario.

use std::collections::BTreeSet;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;

use octoflow_e2e::ledger::Ledger;
use octoflow_e2e::{array_at, holds, object, object_at, procs};
use octoflow_engine::py::json::{Json, Map};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};

/// The Jenkins job the quality-gate scenarios run, by its full name.
pub const JOB: &str = "parity/quality";
/// The credentials the engine sends Jenkins.
pub const JENKINS_USER: &str = "parity";
pub const JENKINS_TOKEN: &str = "parity-token";
/// The file, in the world's directory, of the certificate authority that signed PyPI's certificate, which the engine
/// trusts by SSL_CERT_FILE.
pub const PYPI_CA: &str = "pypi-ca.pem";

/// A world's Jenkins and PyPI proxy, serving until dropped.
pub struct Servers {
    /// Jenkins's server; unblocked by a drop, which ends its thread.
    jenkins: Arc<tiny_http::Server>,
    /// Jenkins's base URL, `http://127.0.0.1:<port>`.
    pub jenkins_url: String,
    /// The proxy's URL, as https_proxy names it.
    pub proxy_url: String,
    /// The proxy's address, which a drop connects to, so that its accept loop sees `stopping` set.
    proxy_address: std::net::SocketAddr,
    /// true once the servers are being dropped: the proxy accepts no more connections.
    stopping: Arc<AtomicBool>,
    /// The two servers' accept loops.
    threads: Vec<JoinHandle<()>>,
}

impl Servers {
    /// Start both for the world in directory world, after making PyPI's certificate there with openssl. Panics,
    /// failing the scenario, when openssl is missing or fails, or a server cannot listen.
    pub fn start(world: &Path) -> Servers {
        let tls = Arc::new(pypi_tls(world));
        let jenkins = Arc::new(tiny_http::Server::http("127.0.0.1:0").unwrap_or_else(|e| panic!("serving Jenkins: {e}")));
        let jenkins_port = jenkins.server_addr().to_ip().expect("Jenkins listens on an IP address").port();
        let jenkins_url = format!("http://127.0.0.1:{jenkins_port}");
        let proxy = TcpListener::bind("127.0.0.1:0").unwrap_or_else(|e| panic!("serving the PyPI proxy: {e}"));
        let proxy_address = proxy.local_addr().unwrap_or_else(|e| panic!("the PyPI proxy's address: {e}"));
        let stopping = Arc::new(AtomicBool::new(false));
        let threads = vec![
            {
                let (server, world, url) = (Arc::clone(&jenkins), world.to_path_buf(), jenkins_url.clone());
                std::thread::spawn(move || {
                    for request in server.incoming_requests() {
                        jenkins_answer(&world, &url, request);
                    }
                })
            },
            {
                let (world, stopping) = (world.to_path_buf(), Arc::clone(&stopping));
                std::thread::spawn(move || proxy_accept(&proxy, &world, &tls, &stopping))
            },
        ];
        Servers { jenkins, jenkins_url, proxy_url: format!("http://{proxy_address}"), proxy_address, stopping, threads }
    }
}

impl Drop for Servers {
    /// Stops both servers; panics, failing the scenario at its cause, when one of them panicked while it served,
    /// unless the scenario fails already.
    fn drop(&mut self) {
        self.jenkins.unblock();
        self.stopping.store(true, Ordering::SeqCst);
        // Wakes the proxy's accept, which then sees stopping; a failure means it no longer listens.
        let _ = TcpStream::connect(self.proxy_address);
        let panicked = self.threads.drain(..).filter_map(|thread| thread.join().err()).count();
        if panicked > 0 && !std::thread::panicking() {
            panic!("{panicked} of the world's servers panicked while serving; their messages are above");
        }
    }
}

/// Log event, a request a server got, in the world's ledger, by the latest invocation, "0" before the first, with
/// that invocation's processes as the ones whose run states count (Ledger::log).
fn log_request(ledger: &Ledger, event: Map<Json>) {
    ledger.change(|state| {
        let latest = object_at(state, "engines")
            .iter()
            .filter_map(|(n, pid)| Some((n.parse::<usize>().ok()?, pid.as_int()?)))
            .max();
        let (n, pids) = match latest {
            Some((n, pid)) => {
                let pid = i32::try_from(pid).expect("a pid fits an i32");
                (n, procs::descendants(pid).unwrap_or_else(|e| panic!("listing invocation {n}'s processes: {e}")))
            }
            None => (0, BTreeSet::new()),
        };
        ledger.log(state, event, &n.to_string(), &pids);
    });
}

// -- Jenkins ------------------------------------------------------------------------

/// Answer one request to Jenkins's REST API for the job JOB, as the engine's CI class uses it, from the world's
/// scripted builds (World::jenkins_build and retained_build); the request is logged, with its form, first.
///
/// The retained builds keep their numbers; the triggered ones follow the highest of them, in the order they were
/// triggered, from 1 where there are none. Queue item k is build k's. The JSON answers have the fields and types of
/// the live Jenkins's; the texts, such as a console, are each scenario's own.
fn jenkins_answer(world: &Path, base_url: &str, mut request: tiny_http::Request) {
    let method = request.method().as_str().to_string();
    let target = request.url().to_string();
    let mut event = Map::from_iter([("jenkins", Json::from(format!("{method} {target}")))]);
    let form = (method == "POST").then(|| {
        let mut body = String::new();
        request.as_reader().read_to_string(&mut body).unwrap_or_else(|e| panic!("reading a Jenkins request: {e}"));
        parse_qs(&body)
    });
    if let Some(form) = &form {
        event.insert("form", form_json(form));
    }
    let ledger = Ledger::new(world);
    log_request(&ledger, event);
    let authorized = request.headers().iter().any(|h| h.field.equiv("Authorization") && h.value.as_str().starts_with("Basic "));
    let answer = if authorized { jenkins_route(&ledger, base_url, &method, &target, form.as_ref()) } else { Answer::text(401, "no credentials") };
    answer.send(request);
}

/// What Jenkins answers a request: its status, its body, and a Location header where it has one.
struct Answer {
    status: u16,
    body: String,
    json: bool,
    location: Option<String>,
}

impl Answer {
    fn text(status: u16, body: &str) -> Answer {
        Answer { status, body: body.to_string(), json: false, location: None }
    }

    fn json(body: &Json) -> Answer {
        Answer { status: 200, body: body.dumps(), json: true, location: None }
    }

    fn send(self, request: tiny_http::Request) {
        let mut response = tiny_http::Response::from_string(self.body).with_status_code(self.status);
        if self.json {
            response.add_header(header("Content-Type", "application/json"));
        }
        if let Some(location) = self.location {
            response.add_header(header("Location", &location));
        }
        if let Err(e) = request.respond(response) {
            eprintln!("answering the engine's Jenkins request: {e}");
        }
    }
}

fn header(name: &str, value: &str) -> tiny_http::Header {
    tiny_http::Header::from_bytes(name.as_bytes(), value.as_bytes()).expect("a valid header")
}

/// The answer to an authorized request for target, a path with its query.
fn jenkins_route(ledger: &Ledger, base_url: &str, method: &str, target: &str, form: Option<&Vec<(String, Vec<String>)>>) -> Answer {
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let prefix: String = JOB.split('/').map(|part| format!("/job/{part}")).collect::<String>() + "/";
    if let Some(item) = path.strip_prefix("/queue/item/") {
        let number: i64 = item.split('/').next().and_then(|n| n.parse().ok()).expect("a queue item's number");
        return Answer::json(&object([("cancelled", Json::Bool(false)), ("executable", object([("number", Json::Int(number.into()))]))]));
    }
    let Some(rest) = path.strip_prefix(&prefix) else { return Answer::text(404, "no such job") };
    let script = ledger.script();
    let (build, held) = match ledger.change(|state| jenkins_build_of(state, &script, base_url, method, rest, query, form)) {
        Ok(answer) => return answer,
        Err(found) => found,
    };
    let (_, what) = rest.split_once('/').unwrap_or((rest, ""));
    let quality = build.get("quality").and_then(Json::as_str);
    match what {
        "api/json" => {
            let result = if held { Json::Null } else { build.get("result").cloned().unwrap_or(Json::Null) };
            Answer::json(&object([("building", Json::Bool(held)), ("result", result)]))
        }
        "artifact/quality.md" if quality.is_some() && !held => Answer::text(200, quality.unwrap_or_default()),
        "testReport/api/json" if build.get("tests").is_some_and(|t| *t != Json::Null) => Answer::json(&test_report(&build)),
        "consoleText" => Answer::text(200, build.get("console").and_then(Json::as_str).unwrap_or_default()),
        _ => Answer::text(404, "not found"),
    }
}

/// Under the ledger's lock: the answer to a request about the job itself, or Err with the build rest names and
/// whether it is held, for jenkins_route to answer from. A poll of a held build's quality.md marks its hold reached:
/// it is the last request of the engine's poll of a running build, after which it sleeps and polls again.
fn jenkins_build_of(
    state: &mut Map<Json>,
    script: &Json,
    base_url: &str,
    method: &str,
    rest: &str,
    query: &str,
    form: Option<&Vec<(String, Vec<String>)>>,
) -> Result<Answer, (Json, bool)> {
    if rest == "buildWithParameters" && method == "POST" {
        let git_ref = form.and_then(|f| f.iter().find(|(k, _)| k == "GIT_REF")).map(|(_, v)| v[0].clone()).unwrap_or_default();
        array_at(state, "triggered").push(Json::from(git_ref));
        let n = builds(script, state).keys().max().copied().unwrap_or_default();
        let location = Some(format!("{base_url}/queue/item/{n}/"));
        return Ok(Answer { status: 201, body: String::new(), json: false, location });
    }
    let builds = builds(script, state);
    if rest == "api/json" {
        let tree = parse_qs(query).into_iter().find(|(k, _)| k == "tree").map(|(_, v)| v[0].clone()).unwrap_or_default();
        return Ok(Answer::json(&job(&tree, &builds)));
    }
    let (number, what) = rest.split_once('/').unwrap_or((rest, ""));
    let Some(build) = number.parse::<i64>().ok().and_then(|n| builds.get(&n)).cloned() else {
        return Ok(Answer::text(404, "no such build"));
    };
    let hold = build.get("hold").and_then(Json::as_str).filter(|h| !h.is_empty()).map(str::to_string);
    let held = hold.as_deref().is_some_and(|h| !holds(state, "released", h));
    if let Some(hold) = hold.filter(|_| held && what == "artifact/quality.md") {
        if !holds(state, "reached", &hold) {
            array_at(state, "reached").push(Json::from(hold));
        }
    }
    Err((build, held))
}

/// The job's builds by number: the retained ones, then those triggered so far, each its scripted outcome (a FAILURE
/// without a quality file past the last scripted) with its GIT_REF under "ref".
fn builds(script: &Json, state: &mut Map<Json>) -> std::collections::BTreeMap<i64, Json> {
    let jenkins = script.get("jenkins");
    let list = |key: &str| jenkins.and_then(|j| j.get(key)).and_then(Json::as_array).cloned().unwrap_or_default();
    let mut out = std::collections::BTreeMap::new();
    for retained in list("retained") {
        let mut build = retained.as_object().cloned().expect("a retained build is an object");
        build.insert("ref", Json::Null);
        let number = build.get("number").and_then(Json::as_int).expect("a retained build's number");
        out.insert(i64::try_from(number).expect("a build number fits an i64"), Json::Object(build));
    }
    let first = out.keys().max().copied().unwrap_or(0) + 1;
    let scripted = list("builds");
    for (k, git_ref) in array_at(state, "triggered").iter().enumerate() {
        let mut build = scripted.get(k).and_then(Json::as_object).cloned().unwrap_or_else(|| {
            Map::from_iter([("result", Json::from("FAILURE")), ("quality", Json::Null)])
        });
        let number = first + i64::try_from(k).expect("a build count fits an i64");
        build.insert("number", Json::Int(number.into()));
        build.insert("ref", git_ref.clone());
        out.insert(number, Json::Object(build));
    }
    out
}

/// The job's api/json for the tree the engine asks for: its parameters, its builds' GIT_REFs, or its builds' results
/// and descriptions; builds newest first.
fn job(tree: &str, builds: &std::collections::BTreeMap<i64, Json>) -> Json {
    let newest = builds.values().rev();
    let number = |b: &Json| b.get("number").cloned().unwrap_or(Json::Null);
    if tree.contains("parameterDefinitions") {
        let parameters = Json::Array(vec![object([("name", Json::from("GIT_REF"))])]);
        return object([("property", Json::Array(vec![object([("parameterDefinitions", parameters)])]))]);
    }
    if tree.contains("actions") {
        let builds = newest.map(|b| {
            let parameter = object([("name", Json::from("GIT_REF")), ("value", b.get("ref").cloned().unwrap_or(Json::Null))]);
            let actions = Json::Array(vec![object([("parameters", Json::Array(vec![parameter]))])]);
            object([("number", number(b)), ("actions", actions)])
        });
        return object([("builds", Json::Array(builds.collect()))]);
    }
    let builds = newest.map(|b| {
        let field = |k: &str| b.get(k).cloned().unwrap_or(Json::Null);
        object([("number", number(b)), ("result", field("result")), ("description", field("description"))])
    });
    object([("builds", Json::Array(builds.collect()))])
}

/// A build's test report: each of its failing tests, (dotted name, detail), as a failed case of one suite.
fn test_report(build: &Json) -> Json {
    let tests = build.get("tests").and_then(Json::as_array).cloned().unwrap_or_default();
    let cases = tests.iter().map(|t| {
        let pair = t.as_array().expect("a test is a [name, detail] pair");
        let name = pair[0].as_str().expect("a test's name");
        let (class, short) = name.rsplit_once('.').unwrap_or((name, name));
        object([
            ("className", Json::from(class)),
            ("name", Json::from(short)),
            ("status", Json::from("FAILED")),
            ("errorDetails", pair[1].clone()),
        ])
    });
    object([("suites", Json::Array(vec![object([("cases", Json::Array(cases.collect()))])]))])
}

/// A form or query string's fields, each with its values in order, as Python's urllib.parse.parse_qs reads it: `+`
/// and `%XX` decoded, a field without a value left out.
fn parse_qs(text: &str) -> Vec<(String, Vec<String>)> {
    let mut out: Vec<(String, Vec<String>)> = Vec::new();
    for pair in text.split('&').filter(|p| !p.is_empty()) {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        let value = unquote(&value.replace('+', " "));
        if value.is_empty() {
            continue;
        }
        let key = unquote(&key.replace('+', " "));
        match out.iter_mut().find(|(k, _)| *k == key) {
            Some((_, values)) => values.push(value),
            None => out.push((key, vec![value])),
        }
    }
    out
}

fn form_json(form: &[(String, Vec<String>)]) -> Json {
    Json::Object(form.iter().map(|(k, v)| (k.as_str(), octoflow_e2e::strings(v))).collect())
}

/// text with each `%XX` decoded to its byte, X a hex digit, the bytes read as UTF-8 with replacement, as
/// urllib.parse.unquote does; a `%` before anything else stays as it is.
///
/// Not the engine's `py::url::unquote_plus`, which octoflow-without-python ticket 06 removes with `crate::py`.
fn unquote(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        // Two hex digits, not a number from_str_radix would take, which may start with a sign.
        let hex = bytes
            .get(i + 1..i + 3)
            .filter(|h| h.iter().all(u8::is_ascii_hexdigit))
            .and_then(|h| u8::from_str_radix(std::str::from_utf8(h).ok()?, 16).ok());
        match (bytes[i], hex) {
            (b'%', Some(b)) => {
                out.push(b);
                i += 3;
            }
            (b, _) => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

// -- PyPI, behind its proxy ---------------------------------------------------------

/// The TLS configuration of pypi.org's server: a certificate for pypi.org signed by a certificate authority of the
/// world's own, PYPI_CA, made with openssl in world. A leaf under a CA, as PyPI's is, since rustls refuses a server
/// certificate that is itself a CA's, as a self-signed one is (CaUsedAsEndEntity).
fn pypi_tls(world: &Path) -> rustls::ServerConfig {
    let path = |name: &str| world.join(name).to_string_lossy().into_owned();
    let key = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "30"];
    let (ca, ca_key, cert, cert_key) = (path(PYPI_CA), path("pypi-ca-key.pem"), path("pypi-cert.pem"), path("pypi-key.pem"));
    let ca_args = ["-subj", "/CN=Parity CA", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign"];
    let leaf_args = ["-subj", "/CN=pypi.org", "-addext", "subjectAltName=DNS:pypi.org", "-addext", "basicConstraints=critical,CA:FALSE"];
    openssl(&[&["req", "-x509"], &key[..], &["-keyout", &ca_key, "-out", &ca], &ca_args[..]].concat());
    openssl(&[&["req", "-x509"], &key[..], &["-CA", &ca, "-CAkey", &ca_key, "-keyout", &cert_key, "-out", &cert], &leaf_args[..]].concat());
    let chain = CertificateDer::pem_file_iter(&cert)
        .unwrap_or_else(|e| panic!("reading {cert}: {e}"))
        .collect::<Result<Vec<_>, _>>()
        .unwrap_or_else(|e| panic!("reading {cert}: {e}"));
    let private = PrivateKeyDer::from_pem_file(&cert_key).unwrap_or_else(|e| panic!("reading {cert_key}: {e}"));
    rustls::ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()
        .expect("ring supports the default TLS versions")
        .with_no_client_auth()
        .with_single_cert(chain, private)
        .unwrap_or_else(|e| panic!("PyPI's certificate: {e}"))
}

/// Run openssl with args; panics, naming it, where it cannot run or fails.
fn openssl(args: &[&str]) {
    let out = std::process::Command::new("openssl")
        .args(args)
        .output()
        .unwrap_or_else(|e| panic!("running openssl, which makes the fake PyPI's certificate: {e}"));
    assert!(
        out.status.success(),
        "openssl could not make the fake PyPI's certificate, {}: {}",
        out.status,
        String::from_utf8_lossy(&out.stderr).trim()
    );
}

/// Accept the proxy's connections until stopping, each answered on a thread of its own.
fn proxy_accept(listener: &TcpListener, world: &Path, tls: &Arc<rustls::ServerConfig>, stopping: &AtomicBool) {
    let mut connections = Vec::new();
    for stream in listener.incoming() {
        if stopping.load(Ordering::SeqCst) {
            break;
        }
        let Ok(stream) = stream else { continue };
        let (world, tls): (PathBuf, _) = (world.to_path_buf(), Arc::clone(tls));
        connections.push(std::thread::spawn(move || proxy_connection(stream, &world, tls)));
    }
    // A connection's panic is the proxy's: its thread panics in turn, which Servers::drop reports.
    let panicked = connections.into_iter().filter_map(|connection| connection.join().err()).count();
    assert_eq!(panicked, 0, "{panicked} connections to the PyPI proxy panicked");
}

/// One connection to the proxy: a CONNECT to pypi.org:443, answered by PyPI over TLS with the world's certificate;
/// any other target is refused with 403. A client that goes away midway ends it quietly.
fn proxy_connection(mut stream: TcpStream, world: &Path, tls: Arc<rustls::ServerConfig>) {
    let Some(head) = read_head(&mut stream) else { return };
    let target = head.split(' ').nth(1).unwrap_or_default();
    if !head.starts_with("CONNECT ") || target != "pypi.org:443" {
        let _ = stream.write_all(b"HTTP/1.0 403 the parity proxy reaches only pypi.org\r\nContent-Length: 0\r\n\r\n");
        return;
    }
    if stream.write_all(b"HTTP/1.0 200 OK\r\n\r\n").is_err() {
        return;
    }
    let connection = rustls::ServerConnection::new(tls).unwrap_or_else(|e| panic!("PyPI's TLS: {e}"));
    let mut tls_stream = rustls::StreamOwned::new(connection, stream);
    let Some(head) = read_head(&mut tls_stream) else { return };
    let path = head.split(' ').nth(1).unwrap_or_default().to_string();
    log_request(&Ledger::new(world), Map::from_iter([("pypi", Json::from(format!("GET {path}")))]));
    let name = path.strip_prefix("/pypi/").map(|rest| unquote(rest.split('/').next().unwrap_or_default()));
    let answer = name.and_then(|name| Ledger::new(world).script().get("pypi").and_then(|p| p.get(&name)).cloned());
    let (status, body) = match answer {
        Some(answer) => ("200 OK", answer.dumps()),
        None => ("404 Not Found", "not found".to_string()),
    };
    let response = format!("HTTP/1.0 {status}\r\nContent-Length: {}\r\n\r\n{body}", body.len());
    if tls_stream.write_all(response.as_bytes()).and_then(|()| tls_stream.flush()).is_ok() {
        tls_stream.conn.send_close_notify();
        let _ = tls_stream.flush();
    }
}

/// An HTTP request's head, its request line and headers, read byte by byte up to the blank line that ends it, so that
/// nothing after it is consumed; None when the client goes away first.
fn read_head(stream: &mut impl Read) -> Option<String> {
    let mut head = Vec::new();
    let mut byte = [0u8];
    while !head.ends_with(b"\r\n\r\n") {
        match stream.read(&mut byte) {
            Ok(1) => head.push(byte[0]),
            _ => return None,
        }
    }
    Some(String::from_utf8_lossy(&head).into_owned())
}

#[test]
fn a_form_is_read_as_parse_qs_reads_it() {
    let form = parse_qs("GIT_REF=orchestrator-ci%2Fa+b&EMPTY=&x=1&x=2");
    let expected = vec![("GIT_REF".to_string(), vec!["orchestrator-ci/a b".to_string()]), ("x".to_string(), vec!["1".to_string(), "2".to_string()])];
    assert_eq!(form, expected);
    assert_eq!(unquote("100%25%zz%e2%80%94"), "100%%zz\u{2014}");
    assert_eq!(unquote("a%+1%-1%"), "a%+1%-1%");
}
