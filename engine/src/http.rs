//! One HTTP request at a time, as orchestrator.py's `urlopen` makes it: the seam through which the engine talks to
//! Jenkins (crate::ci) and PyPI (crate::pypi), and through which their tests stand in for both servers.
//!
//! [`Http`] is the trait; [`HttpClient`] sends over the network. A fake, such as the tests' `FakeJenkins` and
//! `FakePyPI`, answers in memory, so that everything above the transport (each URL the engine builds, each answer it
//! reads, each error it words) runs in the tests as it runs for real.

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use ureq::config::RedirectAuthHeaders;
use ureq::tls::{Certificate, RootCerts, TlsConfig};
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
/// rustls-native-certs finds as OpenSSL finds them (root_certs).
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
                    "the proxy the environment names for this URL is not a URL ureq can use".to_string(),
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

/// The certificates TLS trusts, found by rustls-native-certs as OpenSSL finds them for Python's default context: when
/// env sets `SSL_CERT_FILE` or `SSL_CERT_DIR` (directories separated by colons), those of that file and of every file
/// in those directories, and none from the system; else the system's, from its bundle and certificate directory as
/// openssl-probe finds them (rustls-native-certs then reads the variables from the process's own environment, which
/// is env in the engine). A file that cannot be read, and an item that is not a certificate, adds none, as in
/// OpenSSL, so a variable naming nothing usable trusts none. Mozilla's roots only when neither variable is set and the
/// system has no certificate, where Python would trust none and fail every https request. TODO.md says where this
/// trusts otherwise than Python.
fn root_certs(env: &dyn Fn(&str) -> Option<String>) -> RootCerts {
    let file = env("SSL_CERT_FILE").filter(|path| !path.is_empty());
    let dirs = env("SSL_CERT_DIR").unwrap_or_default();
    let dirs: Vec<&str> = dirs.split(':').filter(|dir| !dir.is_empty()).collect();
    let configured = file.is_some() || !dirs.is_empty();
    // The errors name each file that could not be read; OpenSSL passes over such a file without a word, and so does
    // this, for a store that may still hold the certificate that matters.
    let certs = if configured {
        let mut certs = rustls_native_certs::load_certs_from_paths(file.as_deref().map(Path::new), None).certs;
        for dir in dirs {
            certs.extend(rustls_native_certs::load_certs_from_paths(None, Some(Path::new(dir))).certs);
        }
        certs
    } else {
        rustls_native_certs::load_native_certs().certs
    };
    // Each certificate once, though a file and a directory, or a bundle and its links, hold it twice.
    let mut ders: Vec<&[u8]> = certs.iter().map(|der| der.as_ref()).collect();
    ders.sort_unstable();
    ders.dedup();
    if ders.is_empty() && !configured {
        return RootCerts::WebPki;
    }
    RootCerts::Specific(Arc::new(ders.into_iter().map(|der| Certificate::from_der(der).to_owned()).collect()))
}

#[cfg(test)]
pub(crate) mod tests;
