//! A client of serve, as the desktop app talks to it: one request and its answer (`request`), and the event stream
//! read as it arrives (`EventStream`). HTTP/1.1 over a TcpStream, as Python's http.client sends it, which the
//! recordings were made with: the Host header the URL gives unless the request names one, and `Accept-Encoding:
//! identity`.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Shutdown, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use octoflow_engine::py::json::Json;

/// How long a request, or a read of the event stream, may wait on serve.
const TIMEOUT: Duration = Duration::from_secs(60);

/// A response's status, its headers in order with their names as sent, and its body as decoded.
pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    /// The first header of that name, matched as HTTP matches names, without regard to case.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }
}

/// Send method target to the server at address (host:port), with headers, and read its whole response. Panics, failing
/// the scenario, when serve cannot be reached or answers what is not HTTP.
pub fn request(address: &str, method: &str, target: &str, headers: &[(&str, &str)]) -> Response {
    let mut stream = connect(address, method, target, headers);
    let (status, response_headers, mut reader) = read_head(&mut stream);
    let body = read_body(&mut reader, status, &response_headers);
    Response { status, headers: response_headers, body }
}

/// A connection to address, method target sent on it with headers.
fn connect(address: &str, method: &str, target: &str, headers: &[(&str, &str)]) -> TcpStream {
    let mut stream = TcpStream::connect(address).unwrap_or_else(|e| panic!("connecting to serve at {address}: {e}"));
    stream.set_read_timeout(Some(TIMEOUT)).unwrap_or_else(|e| panic!("setting a timeout on serve's connection: {e}"));
    let mut head = format!("{method} {target} HTTP/1.1\r\n");
    if !headers.iter().any(|(k, _)| k.eq_ignore_ascii_case("Host")) {
        head.push_str(&format!("Host: {address}\r\n"));
    }
    head.push_str("Accept-Encoding: identity\r\n");
    for (k, v) in headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).unwrap_or_else(|e| panic!("sending {method} {target} to serve: {e}"));
    stream
}

/// The status and headers of the response on stream, and a reader positioned at its body.
fn read_head(stream: &mut TcpStream) -> (u16, Vec<(String, String)>, BufReader<TcpStream>) {
    let mut reader = BufReader::new(stream.try_clone().unwrap_or_else(|e| panic!("cloning serve's connection: {e}")));
    let status_line = read_line(&mut reader).unwrap_or_else(|| panic!("serve closed the connection before answering"));
    let status = status_line.split(' ').nth(1).and_then(|s| s.parse().ok()).unwrap_or_else(|| panic!("serve answered {status_line:?}"));
    let mut headers = Vec::new();
    loop {
        let line = read_line(&mut reader).unwrap_or_else(|| panic!("serve closed the connection in its headers"));
        if line.is_empty() {
            break;
        }
        let (k, v) = line.split_once(':').unwrap_or_else(|| panic!("serve sent the header line {line:?}"));
        headers.push((k.to_string(), v.trim().to_string()));
    }
    (status, headers, reader)
}

/// One line without its CRLF; None at the end of the stream, or when the read fails, as once the stream is shut down.
fn read_line(reader: &mut impl BufRead) -> Option<String> {
    let mut line = Vec::new();
    match reader.read_until(b'\n', &mut line) {
        Ok(0) | Err(_) => None,
        Ok(_) => Some(String::from_utf8_lossy(line.strip_suffix(b"\n").unwrap_or(&line)).trim_end_matches('\r').to_string()),
    }
}

fn chunked(headers: &[(String, String)]) -> bool {
    headers.iter().any(|(k, v)| k.eq_ignore_ascii_case("Transfer-Encoding") && v.eq_ignore_ascii_case("chunked"))
}

/// The body that follows a response's head: none for a 204 or 304; by its chunks, its Content-Length, or else up to the
/// end of the connection.
fn read_body(reader: &mut BufReader<TcpStream>, status: u16, headers: &[(String, String)]) -> Vec<u8> {
    if matches!(status, 204 | 304) {
        return Vec::new();
    }
    let mut body = Vec::new();
    if chunked(headers) {
        while let Some(chunk) = read_chunk(reader) {
            body.extend(chunk);
        }
        return body;
    }
    let length = headers.iter().find(|(k, _)| k.eq_ignore_ascii_case("Content-Length")).map(|(_, v)| v.parse::<u64>());
    match length {
        Some(Ok(n)) => reader.take(n).read_to_end(&mut body),
        Some(Err(e)) => panic!("serve sent a Content-Length that is no number: {e}"),
        None => reader.read_to_end(&mut body),
    }
    .unwrap_or_else(|e| panic!("reading serve's body: {e}"));
    body
}

/// The next chunk of a chunked body; None after the last, or when the stream ends or its read fails first.
fn read_chunk(reader: &mut BufReader<TcpStream>) -> Option<Vec<u8>> {
    let size_line = read_line(reader)?;
    let size = usize::from_str_radix(size_line.split(';').next().unwrap_or_default().trim(), 16).ok()?;
    if size == 0 {
        return None;
    }
    let mut chunk = vec![0; size];
    reader.read_exact(&mut chunk).ok()?;
    read_line(reader)?;
    Some(chunk)
}

/// One item of an event stream: an event, its kind and payload, or None for a keep-alive comment.
pub type Item = Option<(String, Json)>;

/// serve's /events, read on a thread of its own: each event and each keep-alive comment as it arrives.
pub struct EventStream {
    /// Its response's status and content type.
    pub status: u16,
    pub content_type: Option<String>,
    /// Each item read so far, in order, with when it arrived.
    items: Arc<Mutex<Vec<(Instant, Item)>>>,
    /// The connection, shut down by close, which ends the reader.
    stream: TcpStream,
    reader: Option<JoinHandle<()>>,
}

impl EventStream {
    /// Open target, the stream's path with its query, on the server at address, and start reading it.
    pub fn open(address: &str, target: &str) -> EventStream {
        let mut stream = connect(address, "GET", target, &[]);
        let (status, headers, mut reader) = read_head(&mut stream);
        let content_type = headers.iter().find(|(k, _)| k.eq_ignore_ascii_case("Content-Type")).map(|(_, v)| v.clone());
        let items = Arc::new(Mutex::new(Vec::new()));
        let is_chunked = chunked(&headers);
        let sink = Arc::clone(&items);
        let thread = std::thread::spawn(move || read_events(&mut reader, is_chunked, &sink));
        EventStream { status, content_type, items, stream, reader: Some(thread) }
    }

    /// Whether a poll that began after t has sent nothing new: an item arrived after t, and a comment after that one,
    /// so the comment's poll started after t.
    pub fn settled_since(&self, t: Instant) -> bool {
        let items = self.items.lock().expect("the event stream's items");
        let after: Vec<&Item> = items.iter().filter(|(when, _)| *when > t).map(|(_, item)| item).collect();
        after.len() >= 2 && after.last().is_some_and(|item| item.is_none())
    }

    /// The events read so far, in order, without the comments.
    pub fn events(&self) -> Vec<(String, Json)> {
        self.items.lock().expect("the event stream's items").iter().filter_map(|(_, item)| item.clone()).collect()
    }

    /// Stop reading: shut the connection down and wait for the reader to end.
    pub fn close(&mut self) {
        // A connection serve closed already cannot be shut down again; the reader has ended then.
        let _ = self.stream.shutdown(Shutdown::Both);
        if let Some(reader) = self.reader.take() {
            if reader.join().is_err() {
                eprintln!("the event stream's reader panicked");
            }
        }
    }
}

impl Drop for EventStream {
    fn drop(&mut self) {
        self.close();
    }
}

/// Read the stream's lines, by its chunks when chunked, into items until it ends or its read fails: an event's kind
/// and data lines, ended by a blank line, or a comment, `:`.
fn read_events(reader: &mut BufReader<TcpStream>, chunked: bool, items: &Mutex<Vec<(Instant, Item)>>) {
    let mut pending = Vec::new();
    let (mut kind, mut payload) = (None, Json::Null);
    loop {
        let more = if chunked { read_chunk(reader) } else { read_line(reader).map(|l| format!("{l}\n").into_bytes()) };
        let Some(more) = more else { return };
        pending.extend(more);
        while let Some(end) = pending.iter().position(|&b| b == b'\n') {
            let line: Vec<u8> = pending.drain(..=end).collect();
            let line = String::from_utf8_lossy(&line[..end]).trim_end_matches('\r').to_string();
            let add = |item: Item| items.lock().expect("the event stream's items").push((Instant::now(), item));
            if let Some(k) = line.strip_prefix("event: ") {
                kind = Some(k.to_string());
            } else if let Some(data) = line.strip_prefix("data: ") {
                payload = Json::parse(data).unwrap_or_else(|e| panic!("serve sent an event's data that is no JSON: {e}"));
            } else if line == ":" {
                add(None);
            } else if line.is_empty() {
                if let Some(k) = kind.take() {
                    add(Some((k, std::mem::replace(&mut payload, Json::Null))));
                }
            }
        }
    }
}
