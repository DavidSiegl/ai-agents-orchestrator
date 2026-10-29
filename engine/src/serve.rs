//! serve: the runs of any project, read-only, over HTTP on 127.0.0.1 for the desktop app, as orchestrator.py's
//! RunServer, RunRequestHandler and serve_command. [`serve_command`] is the program's `serve`; [`RunServer`] is the
//! server, on tiny_http, each request answered on a thread of its own; crate::serve::runs is what it serves of a
//! project. docs/architecture.md's "serve" describes the protocol and its security checks.

use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use nix::sys::signal::{SigSet, Signal};
use tiny_http::{Header, Request, Response};

use crate::cli::listing::RUN_RECORD_VERSION;
use crate::constants::EXIT_ERROR;
use crate::error::{os_error, OrchestratorError};
use crate::herdr::ProcessHerdr;
use crate::host::{Host, ProcessHost};
use crate::interrupt::Interrupt;
use crate::py::json::Json;
use crate::py::text::repr;
use crate::py::url::{parse_qsl, urlsplit};
use runs::{Event, ProjectRuns, RunReadError, RunWatch};

#[cfg(test)]
mod fake;
pub mod runs;

/// How often an event stream reads its project's runs again. A read of a local project starts a few short processes;
/// on a --machine, each of them is an SSH round trip, so a machine's project is read less often.
pub const SERVE_POLL: Polls = Polls { local: Duration::from_secs(1), remote: Duration::from_secs(5) };

/// How often an event stream polls its project, by where the project is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Polls {
    /// For a project on this machine.
    pub local: Duration,
    /// For a project on a --machine, read over SSH.
    pub remote: Duration,
}

/// How a RunServer reaches the project a request names: given the saved herdr machine (empty for this one) and the
/// directory, which may start with ~, as the request gives them, and the Interrupt the server raises when it stops,
/// which must stop the Host's commands; the Host and the project's absolute directory on it. Errors: the project
/// cannot be reached. [`project_host`] is the real one.
pub type Connect =
    dyn Fn(&str, &str, &Interrupt) -> Result<(Arc<dyn Host + Send + Sync>, String), OrchestratorError> + Send + Sync;

/// The program's `serve`: start a RunServer that also answers origins, print {"url", "token"} as one JSON line on
/// standard output, and serve until SIGINT or SIGTERM; returns 0 then, the server stopped. Returns EXIT_ERROR, with
/// one line on standard error, when no port can be bound, or when the line cannot be printed, as when whoever
/// started serve has stopped reading its standard output: nobody could reach the server then. SIGHUP keeps its
/// default, ending the process, as it ends Python's serve.
///
/// Must be called on the main thread before any other thread starts, as crate::interrupt::by_signals must, and
/// instead of it: SIGINT and SIGTERM are blocked in every thread and waited for here, also when the process started
/// with one ignored, as Python's signal.signal replaces an ignored disposition (Linux delivers a blocked signal that
/// is ignored to sigwait; a BSD kernel, macOS's, discards it, so there serve does not stop at an ignored one).
pub fn serve_command(origins: &[String]) -> i32 {
    let signals: SigSet = [Signal::SIGINT, Signal::SIGTERM].into_iter().collect();
    if let Err(e) = signals.thread_block() {
        eprintln!("error: cannot wait for SIGINT and SIGTERM: {e}");
        return EXIT_ERROR;
    }
    let started = RunServer::start(origins, Box::new(project_host));
    serve(started, &mut io::stdout(), &mut io::stderr(), || {
        // sigwait fails only for a set that holds no valid signal, which this one does.
        let _ = signals.wait();
    })
}

/// serve_command's serving, given the started server: print its line on out, wait for until to return, and stop it;
/// the exit status and errors on err as serve_command's. The server is stopped when this returns.
fn serve(started: io::Result<RunServer>, out: &mut dyn Write, err: &mut dyn Write, until: impl FnOnce()) -> i32 {
    let server = match started {
        Ok(server) => server,
        Err(e) => {
            // Nowhere else to say it, if stderr does not take it; the status still tells.
            let _ = writeln!(err, "error: cannot serve on 127.0.0.1: {}", os_error(&e, None));
            return EXIT_ERROR;
        }
    };
    let line = Json::Object([("url", Json::from(server.url())), ("token", Json::from(server.token()))].into_iter().collect());
    if let Err(e) = writeln!(out, "{}", line.dumps()).and_then(|()| out.flush()) {
        let _ = writeln!(err, "error: cannot print the server's address: {}", os_error(&e, None));
        return EXIT_ERROR;
    }
    until();
    0
}

/// The Host that reaches the project, on the saved herdr machine or, with machine empty, here, and the project's
/// absolute directory on it; cwd may start with ~. Each command is stopped by stopping. Python's project_host:
/// without connect's check for a herdr pane, since reading a project sends herdr no command, so a window started
/// from a file manager reads it too. Errors: the machine is not saved or enabled, or the directory cannot be entered.
pub fn project_host(
    machine: &str,
    cwd: &str,
    stopping: &Interrupt,
) -> Result<(Arc<dyn Host + Send + Sync>, String), OrchestratorError> {
    let here = ProcessHost::local(stopping);
    let host = if machine.is_empty() {
        here
    } else {
        here.over_ssh(&ProcessHerdr::new(Some(machine), stopping).ssh_target()?)
    };
    let dir = host.resolve_dir(cwd)?;
    Ok((Arc::new(host), dir))
}

/// serve's HTTP server, on a free port of 127.0.0.1, with its token, answering until it is dropped. Its endpoints are
/// each a GET of JSON naming the project by ?cwd= and an optional ?machine=:
///
/// - /runs: {"version", "runs"}, as `list --json` prints it.
/// - /files?run=ID: {"run", "files"}, the run's files as ProjectRuns::files lists them.
/// - /file?run=ID&name=NAME: {"run", "name", "text"}; 403 for a name outside the run directory or state.json, 404
///   for no such file.
/// - /events: a text/event-stream of RunWatch's events, polled every SERVE_POLL.local, every SERVE_POLL.remote for a
///   --machine project, until the client goes or the server stops. An HTTP/1.1 client gets it chunked, so that the
///   end of the stream is the end of the response; an HTTP/1.0 client's ends when the connection closes.
///
/// Every request is checked in this order: a Host header other than the server's own address, or an Origin other
/// than its own or one of its origins, gets 403 {"error"}; a CORS preflight (OPTIONS) that passes gets 204, allowing
/// GET with an Authorization header; a GET then needs the server's token as an `Authorization: Bearer` header or,
/// for an EventSource, which cannot set one, as ?token=, compared in constant time; else 401. 404 is an unknown
/// endpoint, 400 a missing parameter, 502 a project that could not be read, each with {"error"}; any other method
/// gets 501, as http.server answers it. A request with a byte beyond ASCII in its request line or a header is not
/// answered: tiny_http closes its connection. Nothing is logged.
pub struct RunServer {
    /// What its request threads share.
    shared: Arc<Shared>,
    /// tiny_http's server, which the dispatcher takes requests from; dropped last, which closes its port.
    http: Arc<tiny_http::Server>,
    /// The thread that hands each request a thread of its own; None once it has been joined.
    dispatcher: Option<JoinHandle<()>>,
}

/// What a RunServer's threads share.
struct Shared {
    /// `http://127.0.0.1:<port>`.
    url: String,
    /// What every request must carry: 32 random bytes, base64url without padding, as secrets.token_urlsafe(32).
    token: String,
    /// The Host headers a request may carry: anything else came through a name that only resolves here, as a DNS
    /// rebinding page's does.
    hosts: [String; 2],
    /// The Origin headers it answers and allows by CORS: its own, and the origins it was started with.
    origins: Vec<String>,
    /// Raised when the server stops, which ends every event stream and stops every command a project's Host runs.
    stopping: Interrupt,
    connect: Box<Connect>,
    /// By the machine and directory a request names, the project connect gave for them, each connected once.
    projects: Mutex<HashMap<(String, String), Arc<ProjectRuns>>>,
    polls: Polls,
}

impl RunServer {
    /// Bind a free port of 127.0.0.1 and start answering on it, also requests from origins (the desktop app's webview,
    /// such as tauri://localhost), each project reached by connect. Errors: no port can be bound, or no token made
    /// from the system's random bytes, or no thread started.
    pub fn start(origins: &[String], connect: Box<Connect>) -> io::Result<RunServer> {
        RunServer::start_polling(origins, connect, SERVE_POLL)
    }

    /// As start, the event streams polling every polls.
    pub(crate) fn start_polling(origins: &[String], connect: Box<Connect>, polls: Polls) -> io::Result<RunServer> {
        let token = new_token()?;
        let http = tiny_http::Server::http("127.0.0.1:0")
            .map_err(|e| e.downcast::<io::Error>().map_or_else(io::Error::other, |e| *e))?;
        let port = http.server_addr().to_ip().map_or(0, |a| a.port());
        let url = format!("http://127.0.0.1:{port}");
        let mut all_origins = vec![url.clone(), format!("http://localhost:{port}")];
        all_origins.extend(origins.iter().cloned());
        let shared = Arc::new(Shared {
            url,
            token,
            hosts: [format!("127.0.0.1:{port}"), format!("localhost:{port}")],
            origins: all_origins,
            stopping: Interrupt::new(),
            connect,
            projects: Mutex::new(HashMap::new()),
            polls,
        });
        let http = Arc::new(http);
        let (from, to) = (Arc::clone(&http), Arc::clone(&shared));
        let dispatcher = std::thread::Builder::new().name("serve".to_string()).spawn(move || {
            // recv fails once the server is unblocked, at its stop, or when tiny_http can accept no more connections.
            while let Ok(request) = from.recv() {
                let shared = Arc::clone(&to);
                // A request that gets no thread is dropped, which tiny_http answers with 500.
                let _ = std::thread::Builder::new().name("serve-request".to_string()).spawn(move || answer(&shared, request));
            }
        })?;
        Ok(RunServer { shared, http, dispatcher: Some(dispatcher) })
    }

    /// Where it answers: `http://127.0.0.1:<port>`.
    pub fn url(&self) -> &str {
        &self.shared.url
    }

    /// The token every request must carry.
    pub fn token(&self) -> &str {
        &self.shared.token
    }
}

/// Stops it: every event stream ends, the commands its projects' Hosts run are stopped, and no request is taken any
/// more; a request being answered meanwhile may fail with 502.
impl Drop for RunServer {
    fn drop(&mut self) {
        self.shared.stopping.raise();
        self.http.unblock();
        if let Some(dispatcher) = self.dispatcher.take() {
            // A dispatcher that panicked has nothing left to stop.
            let _ = dispatcher.join();
        }
    }
}

impl Shared {
    /// The project in directory cwd, which may start with ~, on the saved herdr machine, or here with machine empty.
    /// Errors: it cannot be reached.
    fn project(&self, machine: &str, cwd: &str) -> Result<Arc<ProjectRuns>, OrchestratorError> {
        let key = (machine.to_string(), cwd.to_string());
        if let Some(project) = self.lock_projects().get(&key) {
            return Ok(Arc::clone(project));
        }
        let (host, dir) = (self.connect)(machine, cwd, &self.stopping)?;
        let project = Arc::new(ProjectRuns::new(host, dir, target_args(machine, cwd)));
        self.lock_projects().insert(key, Arc::clone(&project));
        Ok(project)
    }

    fn lock_projects(&self) -> std::sync::MutexGuard<'_, HashMap<(String, String), Arc<ProjectRuns>>> {
        // A poisoned lock only means a thread panicked while it held it; the map is still whole.
        self.projects.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Whether request came through another name than the server's address, or from a foreign origin.
    fn refused(&self, request: &Request) -> bool {
        let host_ok = header(request, "Host").is_some_and(|host| self.hosts.iter().any(|h| h == host));
        let origin_ok = header(request, "Origin").is_none_or(|origin| self.origins.iter().any(|o| o == origin));
        !(host_ok && origin_ok)
    }

    /// Whether the request carries the token, as its Authorization header or as query_token, the ?token= it gave
    /// (empty for none). Both are compared, each in constant time.
    fn has_token(&self, request: &Request, query_token: &str) -> bool {
        let authorization = header(request, "Authorization").unwrap_or("");
        let by_header = same_bytes(authorization.as_bytes(), format!("Bearer {}", self.token).as_bytes());
        let by_query = same_bytes(query_token.as_bytes(), self.token.as_bytes());
        by_header | by_query
    }
}

/// The endpoints of a RunServer, as its doc comment lists them.
#[derive(Clone, Copy)]
enum Endpoint {
    Runs,
    Files,
    File,
    Events,
}

impl Endpoint {
    /// The endpoint at path, and the parameters it needs besides cwd; None for no endpoint.
    fn at(path: &str) -> Option<(Endpoint, &'static [&'static str])> {
        Some(match path {
            "/runs" => (Endpoint::Runs, &[]),
            "/files" => (Endpoint::Files, &["run"]),
            "/file" => (Endpoint::File, &["run", "name"]),
            "/events" => (Endpoint::Events, &[]),
            _ => return None,
        })
    }
}

/// Answer request, as RunServer's doc comment says.
fn answer(shared: &Shared, request: Request) {
    match request.method().as_str() {
        "GET" => get(shared, request),
        "OPTIONS" => preflight(shared, request),
        method => {
            let method = repr(method);
            send(request, unsupported(&method));
        }
    }
}

/// Answer a CORS preflight that passes the Host and Origin checks with 204, allowing GET with an Authorization
/// header. A browser sends no Authorization header with a preflight, so there is no token to check.
fn preflight(shared: &Shared, request: Request) {
    if shared.refused(&request) {
        return send_json(request, 403, &error("the Host or Origin header is not this server's"));
    }
    let mut response = Response::empty(204);
    for (name, value) in allowed_origin(&request).into_iter().chain([
        ("Access-Control-Allow-Methods".to_string(), "GET".to_string()),
        ("Access-Control-Allow-Headers".to_string(), "Authorization".to_string()),
    ]) {
        response = with_header(response, &name, &value);
    }
    send(request, response);
}

/// Answer a GET of one of the endpoints.
fn get(shared: &Shared, request: Request) {
    // As http.server takes a path that starts with //, against open redirects (https://github.com/python/cpython/issues/87389).
    let target = request.url();
    let target = if target.starts_with("//") { format!("/{}", target.trim_start_matches('/')) } else { target.to_string() };
    let url = urlsplit(&target);
    // A parameter given twice is the last one, as Python's dict of the pairs keeps it.
    let query: HashMap<String, String> = parse_qsl(&url.query).into_iter().collect();
    let param = |name: &str| query.get(name).map_or("", String::as_str);
    if shared.refused(&request) {
        return send_json(request, 403, &error("the Host or Origin header is not this server's"));
    }
    if !shared.has_token(&request, param("token")) {
        return send_json(request, 401, &error("the token is missing or wrong"));
    }
    let Some((endpoint, params)) = Endpoint::at(&url.path) else {
        return send_json(request, 404, &error(&format!("no endpoint {}", url.path)));
    };
    let missing: Vec<&str> = std::iter::once("cwd").chain(params.iter().copied()).filter(|p| !query.contains_key(*p)).collect();
    if !missing.is_empty() {
        return send_json(request, 400, &error(&format!("missing parameter {}", missing.join(", "))));
    }
    let project = match shared.project(param("machine"), param("cwd")) {
        Ok(project) => project,
        Err(e) => return send_json(request, 502, &error(&e.0)),
    };
    let (run, name) = (param("run"), param("name"));
    let answered = match endpoint {
        Endpoint::Runs => project.records().map_err(RunReadError::from).map(|records| {
            let runs = Json::Array(records.into_iter().map(Json::Object).collect());
            (200, object([("version", Json::from(RUN_RECORD_VERSION)), ("runs", runs)]))
        }),
        Endpoint::Files => project.files(run).map(|files| (200, object([("run", Json::from(run)), ("files", files)]))),
        Endpoint::File => project.read(run, name).map(|text| match text {
            Some(text) => (200, object([("run", Json::from(run)), ("name", Json::from(name)), ("text", Json::from(text))])),
            None => (404, error(&format!("no file {name} in run {run}"))),
        }),
        Endpoint::Events => return events(shared, request, &project),
    };
    match answered {
        Ok((code, body)) => send_json(request, code, &body),
        Err(RunReadError::Refused(why)) => send_json(request, 403, &error(&why)),
        Err(RunReadError::Failed(e)) => send_json(request, 502, &error(&e.0)),
    }
}

/// Stream project's events to request's client until it goes or the server stops: a poll's events, or a comment when
/// nothing changed, so that a client that went away fails the write and ends the stream.
fn events(shared: &Shared, request: Request, project: &ProjectRuns) {
    let chunked = *request.http_version() >= (1, 1);
    let version = if chunked { "1.1" } else { "1.0" };
    let mut head = format!("HTTP/{version} 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n");
    for (name, value) in allowed_origin(&request) {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    if chunked {
        head.push_str("Transfer-Encoding: chunked\r\n");
    }
    head.push_str("\r\n");
    let mut out = request.into_writer();
    if out.write_all(head.as_bytes()).and_then(|()| out.flush()).is_err() {
        return;
    }
    let mut watch = RunWatch::new(project);
    let interval = if project.remote() { shared.polls.remote } else { shared.polls.local };
    while !shared.stopping.is_raised() {
        let events = watch.poll();
        // A poll the stop cut short reports the commands it stopped, which is no problem of the project's.
        if shared.stopping.is_raised() {
            break;
        }
        let text: String = if events.is_empty() { ":\n\n".to_string() } else { events.iter().map(Event::sse).collect() };
        let part = if chunked { format!("{:X}\r\n{text}\r\n", text.len()) } else { text };
        if out.write_all(part.as_bytes()).and_then(|()| out.flush()).is_err() {
            return;
        }
        shared.stopping.sleep(interval);
    }
    if chunked {
        // The client may have gone meanwhile; there is no one to tell.
        let _ = out.write_all(b"0\r\n\r\n").and_then(|()| out.flush());
    }
}

/// The CORS headers that allow request's Origin, when it has one that is not empty: only after the Host and Origin
/// checks, so an Origin here is one the server answers.
fn allowed_origin(request: &Request) -> Vec<(String, String)> {
    match header(request, "Origin").filter(|o| !o.is_empty()) {
        Some(origin) => vec![
            ("Access-Control-Allow-Origin".to_string(), origin.to_string()),
            ("Vary".to_string(), "Origin".to_string()),
        ],
        None => vec![],
    }
}

/// Answer request with code and body as JSON, as json.dumps writes it, allowing its origin unless code is 403, and
/// naming the Bearer scheme with a 401.
fn send_json(request: Request, code: u16, body: &Json) {
    let mut response = with_header(Response::from_data(body.dumps()).with_status_code(code), "Content-Type", "application/json");
    if code != 403 {
        for (name, value) in allowed_origin(&request) {
            response = with_header(response, &name, &value);
        }
    }
    if code == 401 {
        response = with_header(response, "WWW-Authenticate", "Bearer");
    }
    send(request, response);
}

/// The 501 http.server's send_error gives a request of method, as Python's repr writes it.
fn unsupported(method: &str) -> Response<io::Cursor<Vec<u8>>> {
    let escaped = method.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;");
    let page = format!(
        "<!DOCTYPE HTML>\n<html lang=\"en\">\n    <head>\n        <meta charset=\"utf-8\">\n        \
         <style type=\"text/css\">\n            :root {{\n                color-scheme: light dark;\n            }}\n        \
         </style>\n        <title>Error response</title>\n    </head>\n    <body>\n        <h1>Error response</h1>\n        \
         <p>Error code: 501</p>\n        <p>Message: Unsupported method ({escaped}).</p>\n        \
         <p>Error code explanation: 501 - Server does not support this operation.</p>\n    </body>\n</html>\n"
    );
    let response = with_header(Response::from_data(page).with_status_code(501), "Connection", "close");
    with_header(response, "Content-Type", "text/html;charset=utf-8")
}

/// Answer request with response; a client that has gone is not told.
fn send<R: Read>(request: Request, response: Response<R>) {
    let _ = request.respond(response);
}

/// response with the header added; as it was, for a value tiny_http cannot send, which no value here is: each is a
/// constant or a header tiny_http read, all ASCII.
fn with_header<R: Read>(response: Response<R>, name: &str, value: &str) -> Response<R> {
    match Header::from_bytes(name.as_bytes(), value.as_bytes()) {
        Ok(header) => response.with_header(header),
        Err(()) => response,
    }
}

/// The value of request's first header of that name, in any case, as Python's headers.get gives it; None without one.
fn header<'r>(request: &'r Request, name: &'static str) -> Option<&'r str> {
    request.headers().iter().find(|h| h.field.equiv(name)).map(|h| h.value.as_str())
}

fn object<const N: usize>(pairs: [(&str, Json); N]) -> Json {
    Json::Object(pairs.into_iter().collect())
}

fn error(message: &str) -> Json {
    object([("error", Json::from(message))])
}

/// The --machine and --cwd flags a request's machine and cwd stand for, each left out when empty, as Python's
/// target_args leaves them.
fn target_args(machine: &str, cwd: &str) -> Vec<String> {
    let mut out = vec![];
    if !machine.is_empty() {
        out.extend(["--machine".to_string(), machine.to_string()]);
    }
    if !cwd.is_empty() {
        out.extend(["--cwd".to_string(), cwd.to_string()]);
    }
    out
}

/// Whether given and expected are the same bytes, in a time that depends on their lengths alone and not on where
/// they first differ, as secrets.compare_digest compares them: given is read to its end whatever it holds.
fn same_bytes(given: &[u8], expected: &[u8]) -> bool {
    let mut differ = given.len() ^ expected.len();
    for (i, &b) in given.iter().enumerate() {
        // black_box: so the compiler cannot end the loop at the first difference.
        differ = std::hint::black_box(differ | usize::from(b ^ expected.get(i).copied().unwrap_or(0)));
    }
    differ == 0
}

/// A new token: 32 bytes from the system's random source, in base64url without padding, as
/// secrets.token_urlsafe(32) makes it. Errors: /dev/urandom cannot be read.
fn new_token() -> io::Result<String> {
    let mut bytes = [0u8; 32];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut token = String::with_capacity(43);
    for chunk in bytes.chunks(3) {
        let n = chunk.iter().enumerate().fold(0u32, |n, (i, &b)| n | u32::from(b) << (16 - 8 * i));
        // A chunk of k bytes makes k + 1 characters.
        for i in 0..=chunk.len() {
            token.push(char::from(ALPHABET[((n >> (18 - 6 * i)) & 63) as usize]));
        }
    }
    Ok(token)
}

#[cfg(test)]
mod tests;
