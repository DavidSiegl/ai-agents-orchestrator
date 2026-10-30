use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use super::*;

/// A request as a LocalServer received it.
#[derive(Clone, Debug)]
pub(crate) struct Seen {
    pub method: String,
    /// The path and the query.
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: String,
}

impl Seen {
    pub fn header(&self, name: &str) -> Option<&str> {
        find_header(&self.headers, name)
    }
}

/// What a LocalServer answers a request with: a status, headers and a body.
pub(crate) struct Answer {
    pub status: u16,
    pub headers: Vec<(&'static str, String)>,
    pub body: Vec<u8>,
}

impl Answer {
    pub fn new(status: u16, body: impl Into<Vec<u8>>) -> Answer {
        Answer { status, headers: Vec::new(), body: body.into() }
    }

    pub fn with_header(mut self, name: &'static str, value: impl Into<String>) -> Answer {
        self.headers.push((name, value.into()));
        self
    }
}

/// An HTTP server on 127.0.0.1, on a port of its own, that answers each request with what its function gives for it
/// and records the requests; it stops when dropped.
pub(crate) struct LocalServer {
    /// `http://127.0.0.1:<port>`, without a slash at the end.
    pub url: String,
    seen: Arc<Mutex<Vec<Seen>>>,
    server: Arc<tiny_http::Server>,
    thread: Option<JoinHandle<()>>,
}

impl LocalServer {
    pub fn start(answer: impl Fn(&Seen) -> Answer + Send + 'static) -> LocalServer {
        let server = Arc::new(tiny_http::Server::http("127.0.0.1:0").expect("a local server starts"));
        let url = format!("http://{}", server.server_addr().to_ip().expect("an IP address"));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (server_, seen_) = (Arc::clone(&server), Arc::clone(&seen));
        let thread = std::thread::spawn(move || {
            for mut request in server_.incoming_requests() {
                let mut body = String::new();
                std::io::Read::read_to_string(request.as_reader(), &mut body).expect("a UTF-8 body");
                let req = Seen {
                    method: request.method().as_str().to_string(),
                    url: request.url().to_string(),
                    headers: request.headers().iter().map(|h| (h.field.to_string(), h.value.to_string())).collect(),
                    body,
                };
                let out = answer(&req);
                seen_.lock().unwrap().push(req);
                let mut response = tiny_http::Response::from_data(out.body).with_status_code(out.status);
                for (name, value) in out.headers {
                    response.add_header(tiny_http::Header::from_bytes(name, value).unwrap());
                }
                request.respond(response).expect("the answer is sent");
            }
        });
        LocalServer { url, seen, server, thread: Some(thread) }
    }

    /// The requests received so far, in order.
    pub fn seen(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().clone()
    }
}

impl Drop for LocalServer {
    fn drop(&mut self) {
        self.server.unblock();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// A client that reaches every server directly and trusts Mozilla's roots, whatever this machine's environment says.
pub(crate) fn direct() -> HttpClient {
    HttpClient { proxies: Proxies::default(), roots: RootCerts::WebPki }
}

fn get(url: &str) -> Request {
    Request { method: Method::Get, url: url.to_string(), headers: Vec::new(), form: None, timeout: 5 }
}

#[test]
fn a_2xx_answer_is_its_status_headers_and_whole_body() {
    let big = "x".repeat(11 << 20);
    let body = big.clone();
    let server = LocalServer::start(move |_| Answer::new(201, body.clone()).with_header("Location", "/queue/item/7/"));
    let mut request = get(&format!("{}/a?tree=b%2Cc", server.url));
    request.headers.push(("Accept".to_string(), "application/json".to_string()));
    let answer = direct().send(&request).unwrap();
    assert_eq!(answer.status, 201);
    assert_eq!(answer.header("location"), Some("/queue/item/7/"));
    assert_eq!(answer.body.len(), big.len());
    let seen = server.seen();
    assert_eq!((seen[0].method.as_str(), seen[0].url.as_str()), ("GET", "/a?tree=b%2Cc"));
    assert_eq!(seen[0].header("accept"), Some("application/json"));
}

#[test]
fn a_post_sends_its_form() {
    let server = LocalServer::start(|_| Answer::new(200, ""));
    let request = Request { method: Method::Post, form: Some("GIT_REF=a%2Fb".to_string()), ..get(&server.url) };
    direct().send(&request).unwrap();
    let empty = Request { method: Method::Post, ..get(&server.url) };
    direct().send(&empty).unwrap();
    let seen = server.seen();
    assert_eq!((seen[0].method.as_str(), seen[0].body.as_str()), ("POST", "GIT_REF=a%2Fb"));
    assert_eq!(seen[0].header("content-type"), Some("application/x-www-form-urlencoded"));
    assert_eq!((seen[1].method.as_str(), seen[1].body.as_str()), ("POST", ""));
}

#[test]
fn any_other_status_is_an_error_with_it() {
    let server = LocalServer::start(|req| Answer::new(if req.url == "/gone" { 404 } else { 503 }, "<html>"));
    assert_eq!(direct().send(&get(&format!("{}/gone", server.url))), Err(HttpError::Status(404)));
    assert_eq!(direct().send(&get(&server.url)), Err(HttpError::Status(503)));
}

#[test]
fn a_redirect_is_followed_without_the_authorization() {
    let target = LocalServer::start(|_| Answer::new(200, "moved here"));
    let to = format!("{}/there", target.url);
    let origin = LocalServer::start(move |_| Answer::new(302, "").with_header("Location", to.clone()));
    let mut request = get(&format!("{}/here", origin.url));
    request.headers.push(("Authorization".to_string(), "Basic c2VjcmV0".to_string()));
    request.headers.push(("Accept".to_string(), "application/json".to_string()));
    assert_eq!(direct().send(&request).unwrap().body, b"moved here");
    assert_eq!(origin.seen()[0].header("authorization"), Some("Basic c2VjcmV0"));
    let moved = &target.seen()[0];
    assert_eq!(moved.url, "/there");
    assert_eq!(moved.header("authorization"), None);
}

#[test]
fn endless_redirects_end_in_the_last_status() {
    let server = LocalServer::start(|_| Answer::new(302, "").with_header("Location", "/again"));
    assert_eq!(direct().send(&get(&server.url)), Err(HttpError::Status(302)));
    assert_eq!(server.seen().len(), 11);
}

#[test]
fn no_answer_is_worded_as_urllib_words_it() {
    let refusing = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/", refusing.local_addr().unwrap());
    drop(refusing);
    assert_eq!(direct().send(&get(&url)), Err(HttpError::NoAnswer("[Errno 111] Connection refused".to_string())));

    // Accepted by the kernel, never answered.
    let silent = TcpListener::bind("127.0.0.1:0").unwrap();
    let request = Request { timeout: 1, ..get(&format!("http://{}/", silent.local_addr().unwrap())) };
    assert_eq!(direct().send(&request), Err(HttpError::NoAnswer("timed out".to_string())));

    // getaddrinfo's own error: EAI_NONAME, or EAI_AGAIN on a machine that has no resolver to ask.
    let unknown = direct().send(&get("http://unknown-host.invalid/"));
    assert!(matches!(&unknown, Err(HttpError::NoAnswer(why)) if why.starts_with("[Errno -")), "{unknown:?}");
    let bad = get("nonsense");
    assert!(matches!(direct().send(&bad), Err(HttpError::NoAnswer(_))));
}

fn env<'a>(vars: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
    move |name| vars.iter().find(|(k, _)| *k == name).map(|(_, v)| v.to_string())
}

#[test]
fn the_proxy_is_the_schemes_and_the_lower_case_variable_wins() {
    let proxies = Proxies::from_env(&env(&[
        ("HTTPS_PROXY", "http://upper:1"),
        ("https_proxy", "http://lower:2"),
        ("HTTP_PROXY", "http://plain:3"),
    ]));
    assert_eq!(proxies.for_url("https://pypi.org/pypi/x/json"), Some("http://lower:2"));
    assert_eq!(proxies.for_url("http://jenkins:8080/job/"), Some("http://plain:3"));
    assert_eq!(proxies.for_url("ftp://x/"), None);
    assert_eq!(proxies.for_url("nonsense"), None);

    let cancelled = Proxies::from_env(&env(&[("HTTPS_PROXY", "http://upper:1"), ("https_proxy", "")]));
    assert_eq!(cancelled.for_url("https://pypi.org/"), None);
    let cgi = Proxies::from_env(&env(&[("HTTP_PROXY", "http://header:1"), ("REQUEST_METHOD", "GET")]));
    assert_eq!(cgi.for_url("http://jenkins/"), None);
}

#[test]
fn no_proxy_names_hosts_and_their_domains() {
    let proxies = Proxies::from_env(&env(&[
        ("http_proxy", "http://p:1"),
        ("no_proxy", " .Example.com, localhost:8080 ,,127.0.0.1"),
    ]));
    for url in
        ["http://example.com/", "http://ci.EXAMPLE.com:8084/job", "http://localhost:8080/", "http://user@127.0.0.1:9/"]
    {
        assert_eq!(proxies.for_url(url), None, "{url}");
    }
    for url in ["http://notexample.com/", "http://localhost:9090/", "http://127.0.0.2/"] {
        assert_eq!(proxies.for_url(url), Some("http://p:1"), "{url}");
    }
    let all = Proxies::from_env(&env(&[("http_proxy", "http://p:1"), ("no_proxy", "*")]));
    assert_eq!(all.for_url("http://anything/"), None);
}

#[test]
fn a_request_goes_through_the_proxy_its_scheme_names() {
    // A CONNECT proxy, which is the only kind ureq speaks, for http URLs too, where urllib would send the request
    // itself to the proxy (TODO.md).
    let proxy = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", proxy.local_addr().unwrap());
    let tunnel = std::thread::spawn(move || {
        let (stream, _) = proxy.accept().unwrap();
        let mut reader = std::io::BufReader::new(stream.try_clone().unwrap());
        let head = |reader: &mut std::io::BufReader<std::net::TcpStream>| {
            let mut lines = Vec::new();
            loop {
                let mut line = String::new();
                std::io::BufRead::read_line(reader, &mut line).unwrap();
                if line.trim().is_empty() {
                    return lines;
                }
                lines.push(line.trim_end().to_string());
            }
        };
        let connect = head(&mut reader);
        let mut writer = stream;
        std::io::Write::write_all(&mut writer, b"HTTP/1.1 200 Connection established\r\n\r\n").unwrap();
        let tunnelled = head(&mut reader);
        std::io::Write::write_all(&mut writer, b"HTTP/1.1 200 OK\r\nContent-Length: 14\r\n\r\nfrom the proxy").unwrap();
        (connect[0].clone(), tunnelled[0].clone())
    });
    let client = HttpClient::with_env(&env(&[("http_proxy", &url)]));
    let answer = client.send(&get("http://jenkins.invalid/job/x/api/json")).unwrap();
    assert_eq!(answer.body, b"from the proxy");
    assert_eq!(
        tunnel.join().unwrap(),
        ("CONNECT jenkins.invalid:80 HTTP/1.1".to_string(), "GET /job/x/api/json HTTP/1.1".to_string())
    );

    let broken = HttpClient::with_env(&env(&[("http_proxy", "http://alice:s3cret@[::1")]));
    let answer = broken.send(&get("http://jenkins.invalid/"));
    assert!(
        matches!(&answer, Err(HttpError::NoAnswer(m)) if m.starts_with("the proxy ") && !m.contains("s3cret")),
        "{answer:?}"
    );
}

#[test]
fn a_resolver_error_has_its_number_when_its_text_gives_it() {
    assert_eq!(
        resolver_error("Temporary failure in name resolution"),
        "[Errno -3] Temporary failure in name resolution"
    );
    assert_eq!(resolver_error("No address associated with hostname"), "[Errno -5] No address associated with hostname");
    assert_eq!(resolver_error("something else"), "something else");
}

// A self-signed certificate for pypi.org, as the parity harness makes one.
const CERT: &str = include_str!("../../tests/data/pypi-cert.pem");

// A second, for a store that holds two.
const OTHER_CERT: &str = include_str!("../../tests/data/other-cert.pem");

/// How many certificates roots trusts; None for Mozilla's.
fn count(roots: RootCerts) -> Option<usize> {
    match roots {
        RootCerts::Specific(certs) => Some(certs.len()),
        _ => None,
    }
}

#[test]
fn tls_trusts_the_certificates_ssl_cert_file_and_ssl_cert_dir_name() {
    let dir = crate::test_support::TempDir::new();
    let (file, missing, certs, more) = (dir.join("cert.pem"), dir.join("missing"), dir.join("certs"), dir.join("more"));
    std::fs::write(&file, format!("{CERT}garbage\n")).unwrap();
    std::fs::create_dir(&certs).unwrap();
    std::fs::create_dir(&more).unwrap();
    // Any name, as rustls-native-certs reads every file of a directory; a file of no certificate adds none.
    std::fs::write(format!("{certs}/jenkins-ca.pem"), OTHER_CERT).unwrap();
    std::fs::write(format!("{certs}/5ed36f99.0"), CERT).unwrap();
    std::fs::write(format!("{more}/notes.txt"), "not a certificate\n").unwrap();
    let roots = |vars: &[(&str, &str)]| count(root_certs(&env(vars)));
    assert_eq!(roots(&[("SSL_CERT_FILE", &file)]), Some(1));
    assert_eq!(roots(&[("SSL_CERT_DIR", &format!("{certs}:{more}:/nowhere"))]), Some(2));
    // The same certificate from the file and a directory counts once.
    assert_eq!(roots(&[("SSL_CERT_FILE", &file), ("SSL_CERT_DIR", &certs)]), Some(2));
    // A variable that names nothing usable trusts none: not the system's, nor Mozilla's.
    assert_eq!(roots(&[("SSL_CERT_FILE", &missing)]), Some(0));
    assert_eq!(roots(&[("SSL_CERT_DIR", &more)]), Some(0));
}

/// How many certificates the system's store holds where openssl-probe finds it, each once; None when it holds none, so
/// that root_certs trusts Mozilla's roots there.
fn system_store() -> Option<usize> {
    let system = openssl_probe::probe();
    let mut found = rustls_native_certs::load_certs_from_paths(system.cert_file.as_deref(), None).certs;
    for dir in &system.cert_dir {
        found.extend(rustls_native_certs::load_certs_from_paths(None, Some(dir)).certs);
    }
    let mut ders: Vec<&[u8]> = found.iter().map(|der| der.as_ref()).collect();
    ders.sort_unstable();
    ders.dedup();
    (!ders.is_empty()).then_some(ders.len())
}

/// The variable whose presence marks a run of this test binary as a_variable_set_empty_is_unset's child, the one run of
/// the_system_store_in_this_process with the environment it is about.
const EMPTY_CERT_VARIABLES_CHILD: &str = "OCTOFLOW_HTTP_TESTS_EMPTY_CERT_VARIABLES_CHILD";

#[test]
fn the_system_store_in_this_process() {
    // Any other run has the test runner's environment, which a test may not change, as tests share it.
    if std::env::var_os(EMPTY_CERT_VARIABLES_CHILD).is_none() {
        return;
    }
    for name in ["SSL_CERT_FILE", "SSL_CERT_DIR"] {
        assert_eq!(std::env::var(name).as_deref(), Ok(""), "{name} is not set empty in this process");
    }
    assert_eq!(count(HttpClient::from_env().roots), system_store(), "the system's store was not what was trusted");
}

#[test]
fn a_variable_set_empty_is_unset() {
    // The process's own environment, as the engine has it: an empty SSL_CERT_FILE and SSL_CERT_DIR, which shells and CI
    // wrappers export to unset them, leave the system's store, not none, nor Mozilla's roots where it has certificates.
    let child = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "http::tests::the_system_store_in_this_process", "--nocapture"])
        .env(EMPTY_CERT_VARIABLES_CHILD, "1")
        .env("SSL_CERT_FILE", "")
        .env("SSL_CERT_DIR", "")
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&child.stdout);
    assert!(child.status.success() && stdout.contains("1 passed"), "{stdout}");
}

#[test]
fn without_either_variable_tls_trusts_this_machine_s_certificates() {
    let roots = count(root_certs(&env(&[])));
    // Mozilla's roots only on a machine without certificates of its own.
    assert_eq!(roots, system_store());
    if std::fs::read("/etc/ssl/certs/ca-certificates.crt").is_ok() {
        assert!(roots.is_some_and(|n| n > 0), "Debian's bundle was not read: {roots:?}");
    }
}

#[test]
fn methods_and_headers_read_as_named() {
    assert_eq!((Method::Get.as_str(), Method::Post.as_str()), ("GET", "POST"));
    let mut request = get("http://x/");
    request.headers.push(("Authorization".to_string(), "Basic x".to_string()));
    assert_eq!(request.header("AUTHORIZATION"), Some("Basic x"));
    assert_eq!(request.header("Accept"), None);
    assert_eq!(Response::ok("b"), Response { status: 200, headers: Vec::new(), body: b"b".to_vec() });
}
