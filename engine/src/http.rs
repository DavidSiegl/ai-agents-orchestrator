//! One HTTP request at a time, as orchestrator.py's `urlopen` makes it: the seam through which the engine talks to
//! Jenkins (crate::ci) and PyPI (crate::pypi), and through which their tests stand in for both servers.
//!
//! [`Http`] is the trait; [`HttpClient`] sends over the network. A fake, such as the tests' `FakeJenkins` and
//! `FakePyPI`, answers in memory, so that everything above the transport (each URL the engine builds, each answer it
//! reads, each error it words) runs in the tests as it runs for real.

use std::sync::Arc;
use std::time::Duration;

use ureq::config::RedirectAuthHeaders;
use ureq::tls::{parse_pem, PemItem, RootCerts, TlsConfig};
use ureq::{Agent, Proxy};

use crate::error::os_error;

/// The two methods the engine sends.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Method {
    Get,
    Post,
}

impl Method {
    /// The method's name as a request line and an error message write it.
    pub fn as_str(self) -> &'static str {
        match self {
            Method::Get => "GET",
            Method::Post => "POST",
        }
    }
}

/// A request, as [`Http::send`] sends it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    pub method: Method,
    /// The absolute http or https URL, its query included.
    pub url: String,
    /// Headers to send, by name and value. An `Authorization` header goes only to url's server: a redirect leaves it
    /// behind, as urllib's add_unredirected_header does.
    pub headers: Vec<(String, String)>,
    /// The body, as `application/x-www-form-urlencoded` (crate::py::url::urlencode); None for none.
    pub form: Option<String>,
    /// Seconds that connecting, and then each read or write of the connection, may take, as urlopen's timeout.
    pub timeout: u64,
}

impl Request {
    /// The value of the header name, compared without regard to case; None when the request has none.
    pub fn header(&self, name: &str) -> Option<&str> {
        find_header(&self.headers, name)
    }
}

/// A 2xx answer: the status, the headers, and the whole body.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    /// By name and value, in the order the server sent them; a value that is not UTF-8 is left out.
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    /// A 200 answer with body and no headers: what a fake answers most requests with.
    pub fn ok(body: impl Into<Vec<u8>>) -> Response {
        Response { status: 200, headers: Vec::new(), body: body.into() }
    }

    /// The value of the header name, compared without regard to case; None when the answer has none.
    pub fn header(&self, name: &str) -> Option<&str> {
        find_header(&self.headers, name)
    }
}

fn find_header<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
}

/// Why a request got no 2xx answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HttpError {
    /// The server answered with this status, after following up to 10 redirects: urllib's HTTPError. A redirect
    /// beyond those is the status of its answer.
    Status(u16),
    /// No answer came, worded as urllib's URLError reason words it: `timed out`, `[Errno 111] Connection refused`.
    NoAnswer(String),
}

/// Sends one request and waits for its whole answer.
pub trait Http: Send + Sync {
    /// The 2xx answer to request, its body read to the end, after following redirects as [`Request::headers`] says.
    /// Errors: [`HttpError`].
    fn send(&self, request: &Request) -> Result<Response, HttpError>;
}

/// The network, reached as urllib reaches it: through the proxy that the environment's `http_proxy` or
/// `https_proxy` names for the URL's scheme, unless `no_proxy` names its host, and over TLS trusting the certificates
/// OpenSSL's default paths give Python (root_certs).
#[derive(Clone)]
pub struct HttpClient {
    proxies: Proxies,
    roots: RootCerts,
}

impl HttpClient {
    /// The client of the process's environment, read now: the proxies and the certificates as [`HttpClient`] says.
    pub fn from_env() -> HttpClient {
        HttpClient::with_env(&|name| std::env::var(name).ok())
    }

    /// The client of the environment env gives each variable of, by name.
    pub fn with_env(env: &dyn Fn(&str) -> Option<String>) -> HttpClient {
        HttpClient { proxies: Proxies::from_env(env), roots: root_certs(env) }
    }

    fn agent(&self, request: &Request) -> Result<Agent, HttpError> {
        let proxy = match self.proxies.for_url(&request.url) {
            // Not the proxy's URL, which can hold a password.
            Some(url) => Some(Proxy::new(url).map_err(|_| {
                HttpError::NoAnswer(
                    "the proxy that http_proxy or https_proxy names is not a URL ureq can use".to_string(),
                )
            })?),
            None => None,
        };
        let timeout = Some(Duration::from_secs(request.timeout));
        let config = Agent::config_builder()
            .http_status_as_error(false)
            .max_redirects(10)
            .max_redirects_will_error(false)
            .redirect_auth_headers(RedirectAuthHeaders::Never)
            .timeout_connect(timeout)
            .timeout_per_read(timeout)
            .timeout_per_write(timeout)
            .proxy(proxy)
            .tls_config(TlsConfig::builder().root_certs(self.roots.clone()).build())
            .build();
        Ok(Agent::new_with_config(config))
    }
}

impl Http for HttpClient {
    fn send(&self, request: &Request) -> Result<Response, HttpError> {
        let agent = self.agent(request)?;
        let answer = match (request.method, &request.form) {
            (Method::Get, _) => {
                let mut builder = agent.get(&request.url);
                for (name, value) in &request.headers {
                    builder = builder.header(name, value);
                }
                builder.call()
            }
            (Method::Post, form) => {
                let mut builder = agent.post(&request.url);
                for (name, value) in &request.headers {
                    builder = builder.header(name, value);
                }
                match form {
                    Some(form) => builder.content_type("application/x-www-form-urlencoded").send(form.as_bytes()),
                    None => builder.send_empty(),
                }
            }
        };
        let mut answer = answer.map_err(|e| no_answer(&e))?;
        let status = answer.status().as_u16();
        if !(200..300).contains(&status) {
            return Err(HttpError::Status(status));
        }
        let headers = answer
            .headers()
            .iter()
            .filter_map(|(k, v)| Some((k.as_str().to_string(), v.to_str().ok()?.to_string())))
            .collect();
        let body = answer.body_mut().with_config().limit(u64::MAX).read_to_vec().map_err(|e| no_answer(&e))?;
        Ok(Response { status, headers, body })
    }
}

/// e as urllib's URLError words why no answer came.
fn no_answer(e: &ureq::Error) -> HttpError {
    let reason = match e {
        ureq::Error::Timeout(_) => "timed out".to_string(),
        ureq::Error::Io(io) if io.kind() == std::io::ErrorKind::TimedOut => "timed out".to_string(),
        ureq::Error::Io(io) => match io.to_string().strip_prefix("failed to lookup address information: ") {
            Some(why) => resolver_error(why),
            None => os_error(io, None),
        },
        ureq::Error::HostNotFound => resolver_error("Name or service not known"),
        other => other.to_string(),
    };
    HttpError::NoAnswer(reason)
}

/// Why a host name did not resolve, as Python's socket.gaierror writes it: with getaddrinfo's error number, for the
/// errors whose text gives it away.
fn resolver_error(why: &str) -> String {
    let number = match why {
        "Name or service not known" => Some(-2),
        "Temporary failure in name resolution" => Some(-3),
        "No address associated with hostname" => Some(-5),
        _ => None,
    };
    number.map_or_else(|| why.to_string(), |n| format!("[Errno {n}] {why}"))
}

/// The proxies the environment names, as urllib's getproxies_environment and proxy_bypass_environment read them.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct Proxies {
    /// The proxy of http URLs, as `http_proxy` names it; None for none.
    http: Option<String>,
    /// The proxy of https URLs, as `https_proxy` names it; None for none.
    https: Option<String>,
    /// `no_proxy`: the hosts reached directly, comma-separated; None for none.
    bypass: Option<String>,
}

impl Proxies {
    fn from_env(env: &dyn Fn(&str) -> Option<String>) -> Proxies {
        // The lower-case variable wins, and an empty one cancels the upper-case; under CGI, HTTP_PROXY is the
        // request's Proxy header, not the user's choice (httpoxy), so http's is taken only from http_proxy.
        let var = |lower: &str, cgi: bool| -> Option<String> {
            match env(lower) {
                Some(value) => Some(value).filter(|v| !v.is_empty()),
                None if cgi => None,
                None => env(&lower.to_uppercase()).filter(|v| !v.is_empty()),
            }
        };
        let cgi = env("REQUEST_METHOD").is_some();
        Proxies { http: var("http_proxy", cgi), https: var("https_proxy", false), bypass: var("no_proxy", false) }
    }

    /// The proxy url goes through; None to reach its server directly.
    fn for_url(&self, url: &str) -> Option<&str> {
        let (scheme, rest) = url.split_once("://")?;
        let proxy = match scheme.to_ascii_lowercase().as_str() {
            "http" => self.http.as_deref()?,
            "https" => self.https.as_deref()?,
            _ => return None,
        };
        let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
        let host = authority.rsplit_once('@').map_or(authority, |(_, host)| host);
        (!self.bypasses(host)).then_some(proxy)
    }

    /// Whether no_proxy names host, a host and maybe a port: `*`, or an entry that is the host, or a domain it is
    /// in, a leading dot ignored, compared without regard to case.
    fn bypasses(&self, host: &str) -> bool {
        let Some(bypass) = &self.bypass else { return false };
        if bypass == "*" {
            return true;
        }
        let host = host.to_lowercase();
        let host_only = match host.rsplit_once(':') {
            Some((name, port)) if port.bytes().all(|b| b.is_ascii_digit()) => name,
            _ => host.as_str(),
        };
        bypass
            .split(',')
            .map(|name| name.trim().trim_start_matches('.').to_lowercase())
            .filter(|name| !name.is_empty())
            .any(|name| {
                let domain = format!(".{name}");
                host_only == name || host == name || host_only.ends_with(&domain) || host.ends_with(&domain)
            })
    }
}

/// The certificates TLS trusts, as OpenSSL's default paths give them to Python's default context: those of the file
/// `SSL_CERT_FILE` names, else of the first of SYSTEM_BUNDLES that exists, and those of the files named by their
/// subject's hash (`<8 hex digits>.<n>`, as c_rehash links them) in the directories `SSL_CERT_DIR` names, separated
/// by colons, else in SYSTEM_CERT_DIR. A file that cannot be read, and an item of it that is not a certificate, adds
/// none: OpenSSL ignores them too, and a variable naming nothing usable then trusts none, as it does in Python. When
/// neither variable is set and the system has no certificate there, Mozilla's roots, where the Python engine, which
/// does not use certifi, trusts none.
fn root_certs(env: &dyn Fn(&str) -> Option<String>) -> RootCerts {
    const SYSTEM_BUNDLES: [&str; 4] = [
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/ca-bundle.pem",
        "/etc/ssl/cert.pem",
    ];
    const SYSTEM_CERT_DIR: &str = "/etc/ssl/certs";
    let file = env("SSL_CERT_FILE").filter(|path| !path.is_empty());
    let dirs = env("SSL_CERT_DIR").filter(|dirs| !dirs.is_empty());
    let configured = file.is_some() || dirs.is_some();
    let mut pem = match &file {
        Some(path) => std::fs::read(path).unwrap_or_default(),
        None => SYSTEM_BUNDLES.iter().find_map(|path| std::fs::read(path).ok()).unwrap_or_default(),
    };
    for dir in dirs.as_deref().unwrap_or(SYSTEM_CERT_DIR).split(':') {
        let Ok(entries) = std::fs::read_dir(dir) else { continue };
        for entry in entries.flatten() {
            if is_hash_name(&entry.file_name().to_string_lossy()) && entry.path().is_file() {
                pem.extend(std::fs::read(entry.path()).unwrap_or_default());
                pem.push(b'\n');
            }
        }
    }
    let certs: Vec<_> = parse_pem(&pem)
        .filter_map(|item| match item {
            Ok(PemItem::Certificate(cert)) => Some(cert),
            _ => None,
        })
        .collect();
    if certs.is_empty() && !configured {
        return RootCerts::WebPki;
    }
    RootCerts::Specific(Arc::new(certs))
}

/// Whether name is one OpenSSL looks a certificate up by in a directory: the subject's hash, 8 lowercase hex digits,
/// a dot and a number.
fn is_hash_name(name: &str) -> bool {
    let Some((hash, n)) = name.split_once('.') else { return false };
    hash.len() == 8
        && hash.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        && !n.is_empty()
        && n.bytes().all(|b| b.is_ascii_digit())
}

#[cfg(test)]
pub(crate) mod tests;
