//! FakeJenkins, the quality job the Workflow tests of ticket 08 run on: Jenkins behind an [`Http`], played as
//! tests/test_orchestrator.py's FakeCI plays it behind urlopen, so that every URL, answer and error of
//! [`Jenkins`] runs in those tests as it runs for real.

use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex, MutexGuard};

use super::Jenkins;
use crate::constants::{HTTP_TIMEOUT, JOB_PARAMETERS};
use crate::http::{Http, HttpError, Request, Response};
use crate::py::json::{Json, Map};

/// The Jenkins of the tests: JENKINS_URL, without its slash.
pub(crate) const JENKINS: &str = "https://jenkins.example";
pub(crate) const JENKINS_TOKEN: &str = "jenkins-secret-token";
/// The quality job, in a folder.
pub(crate) const JOB: &str = "AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality";
/// JOB's path under JENKINS.
pub(crate) const JOB_PATH: &str = "/job/AI-Agents-Orchestrator/job/py-ai-agents-orchestrator-quality/";

/// CI_ENV of the tests: JENKINS, the user agents and JENKINS_TOKEN.
pub(crate) fn credentials() -> Map<String> {
    [
        ("JENKINS_URL", format!("{JENKINS}/")),
        ("JENKINS_USER", "agents".to_string()),
        ("JENKINS_TOKEN", JENKINS_TOKEN.to_string()),
    ]
    .into_iter()
    .collect()
}

/// JOB on fake, with credentials(): Python's ci_for.
pub(crate) fn jenkins_on(fake: &Arc<FakeJenkins>) -> Jenkins {
    jenkins_with(fake, &credentials())
}

/// JOB on fake, with these credentials of CI_ENV, by name: Python's ci_for with env.
pub(crate) fn jenkins_with(fake: &Arc<FakeJenkins>, credentials: &Map<String>) -> Jenkins {
    Jenkins::new(JOB, credentials, "/home/u/.config/ai-agents-orchestrator/ci.env", Arc::clone(fake) as Arc<dyn Http>)
}

/// What one build does: Python's outcome().
#[derive(Clone, Debug)]
pub(crate) struct Outcome {
    /// The result it ends with.
    pub result: String,
    /// The quality.md its Gate stage archives; None for a build that ended before that stage.
    pub quality: Option<String>,
    /// (className, name, errorDetails) of each failing test in its test report.
    pub tests: Vec<(String, String, String)>,
    /// Whether it has a test report.
    pub test_report: bool,
    pub console: String,
    /// How many polls still see it building.
    pub polls: u32,
    /// How many of those, besides the last, see quality.md archived: 0 for a file that appears only on the last.
    pub after_gate: u32,
    /// Its description; None when it has none.
    pub description: Option<String>,
}

impl Outcome {
    /// A build whose Gate stage archives a quality.md of gate and the numbered findings, and that ends with what
    /// the Jenkinsfile ends such a build with: SUCCESS for OK, else UNSTABLE.
    pub fn gate(gate: &str, findings: &[&str]) -> Outcome {
        let numbered: Vec<String> = findings.iter().enumerate().map(|(k, f)| format!("{}. {f}", k + 1)).collect();
        let numbered = if numbered.is_empty() { "None.".to_string() } else { numbered.join("\n") };
        Outcome {
            result: if gate == "OK" { "SUCCESS" } else { "UNSTABLE" }.to_string(),
            quality: Some(format!(
                "GATE: {gate}\n\n`orchestrator.py gate` judged the working tree.\n\n## Issues on changed \
                    lines\n\n{numbered}\n"
            )),
            tests: Vec::new(),
            test_report: true,
            console: String::new(),
            polls: 2,
            after_gate: 0,
            description: None,
        }
    }

    /// A build that ends FAILURE before its Gate stage, archiving nothing.
    pub fn unarchived() -> Outcome {
        Outcome { result: "FAILURE".to_string(), quality: None, ..Outcome::gate("OK", &[]) }
    }
}

/// A request as FakeJenkins received it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Received {
    pub method: &'static str,
    pub url: String,
    /// The form it posted, decoded; empty for none.
    pub form: Map<String>,
}

/// A build as the fake Jenkins has it.
#[derive(Clone, Debug)]
pub(crate) struct Build {
    /// The GIT_REF it builds.
    pub git_ref: String,
    /// How many more polls see it building.
    pub left: u32,
    pub outcome: Outcome,
}

/// A queue item: how many more polls see it waiting, and the build it starts.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Waiting {
    pub left: u32,
    pub number: i64,
}

/// What FakeJenkins holds and records; each field is what Python's FakeCI has of that name.
#[derive(Debug)]
pub(crate) struct FakeJenkinsState {
    /// Every request, in order.
    pub requests: Vec<Received>,
    /// Each request by a short name: parameters, builds, find, trigger, queue, build, quality, tests, console, or
    /// `<method> <path>` for one it does not know, `<method> <path> failing` for one in fail, `jenkins down`.
    pub names: Vec<String>,
    /// Each request's Authorization header.
    pub auth: Vec<Option<String>>,
    /// The job's parameters.
    pub parameters: Vec<String>,
    /// The outcome each triggered build plays, in order; an OK one once they run out.
    pub outcomes: VecDeque<Outcome>,
    /// Whether Jenkins does not answer.
    pub down: bool,
    /// The HTTP status each (method, path) is answered with instead.
    pub fail: Vec<((&'static str, String), u16)>,
    /// Whether the job was deleted.
    pub job_gone: bool,
    /// Whether Jenkins forgot its queue items.
    pub forgotten: bool,
    /// Whether Jenkins cancelled each queue item: it answers `{"cancelled": true}` for it.
    pub cancelled: bool,
    /// The text of each archived quality.md served, in order.
    pub quality_reads: Vec<String>,
    /// What the k-th read of an archived quality.md, from 1, is answered with instead of the file, by k: a copy that
    /// archiveArtifacts has not finished.
    pub quality_replies: BTreeMap<usize, String>,
    pub builds: BTreeMap<i64, Build>,
    pub queue: BTreeMap<i64, Waiting>,
    /// The number of the newest build.
    pub number: i64,
}

/// Jenkins behind an Http; each triggered build plays the next of its outcomes. Its JSON answers have the fields and
/// types of the live Jenkins's, which tests/contract/test_jenkins.py checks against the real job; the texts, such as
/// a console, and how many cases a test report holds are each test's own.
pub(crate) struct FakeJenkins {
    state: Mutex<FakeJenkinsState>,
}

/// What the fake answers: a status, or a body and its Location header.
enum Answer {
    Status(u16),
    Body(String, Option<String>),
}

impl Answer {
    /// The body that is value, as JSON, with no Location.
    fn json(value: Json) -> Answer {
        Answer::Body(value.dumps(), None)
    }
}

impl FakeJenkins {
    pub fn new() -> Arc<FakeJenkins> {
        Arc::new(FakeJenkins {
            state: Mutex::new(FakeJenkinsState {
                requests: Vec::new(),
                names: Vec::new(),
                auth: Vec::new(),
                parameters: JOB_PARAMETERS.iter().map(|p| p.to_string()).collect(),
                outcomes: VecDeque::new(),
                down: false,
                fail: Vec::new(),
                job_gone: false,
                forgotten: false,
                cancelled: false,
                quality_reads: Vec::new(),
                quality_replies: BTreeMap::new(),
                builds: BTreeMap::new(),
                queue: BTreeMap::new(),
                number: 40,
            }),
        })
    }

    /// What it holds and records, for a test to script and read.
    pub fn state(&self) -> MutexGuard<'_, FakeJenkinsState> {
        self.state.lock().unwrap()
    }
}

impl FakeJenkinsState {
    /// Adds a build of git_ref as Jenkins has it; its number.
    pub fn add_build(&mut self, git_ref: &str, outcome: Outcome) -> i64 {
        self.number += 1;
        self.builds.insert(self.number, Build { git_ref: git_ref.to_string(), left: outcome.polls, outcome });
        self.number
    }

    /// The refs of the builds, in the order they were triggered or added.
    pub fn triggered(&self) -> Vec<String> {
        self.builds.values().map(|b| b.git_ref.clone()).collect()
    }

    /// The short name of a request of method to path, with query and the form it posted, decoded, as names records
    /// it, and what Jenkins answers it with.
    fn answer(
        &mut self,
        method: &'static str,
        path: &str,
        query: &Map<String>,
        form: &Map<String>,
    ) -> (String, Answer) {
        let unknown = || (format!("{method} {path}"), Answer::Status(404));
        if let Some(item) = path.strip_prefix("/queue/item/") {
            return ("queue".to_string(), self.queue_item(item));
        }
        let Some(rest) = path.strip_prefix(JOB_PATH).filter(|_| !self.job_gone) else { return unknown() };
        let tree = query.get("tree").map_or("", String::as_str);
        let answered = match rest {
            "api/json" if tree.contains("property") => {
                let definitions = self.parameters.iter().map(|p| object(&[("name", p.as_str().into())])).collect();
                Some((
                    "parameters",
                    Answer::json(object(&[(
                        "property",
                        Json::Array(vec![object(&[]), object(&[("parameterDefinitions", Json::Array(definitions))])]),
                    )])),
                ))
            }
            "api/json" if tree.contains("description") => {
                let builds = self.builds.iter().rev().map(|(&n, b)| {
                    let result = if b.left > 0 { Json::Null } else { b.outcome.result.as_str().into() };
                    object(&[
                        ("number", n.into()),
                        ("result", result),
                        ("description", b.outcome.description.clone().into()),
                    ])
                });
                Some(("builds", Answer::json(object(&[("builds", Json::Array(builds.collect()))]))))
            }
            "api/json" => {
                let builds = self.builds.iter().rev().take(20).map(|(&n, b)| {
                    let parameter = object(&[("name", "GIT_REF".into()), ("value", b.git_ref.as_str().into())]);
                    object(&[
                        ("number", n.into()),
                        (
                            "actions",
                            Json::Array(vec![object(&[]), object(&[("parameters", Json::Array(vec![parameter]))])]),
                        ),
                    ])
                });
                Some(("find", Answer::json(object(&[("builds", Json::Array(builds.collect()))]))))
            }
            "buildWithParameters" if method == "POST" => {
                let outcome = self.outcomes.pop_front().unwrap_or_else(|| Outcome::gate("OK", &[]));
                let n = self.add_build(form.get("GIT_REF").map_or("", String::as_str), outcome);
                self.queue.insert(900 + n, Waiting { left: 1, number: n });
                Some(("trigger", Answer::Body(String::new(), Some(format!("{JENKINS}/queue/item/{}/", 900 + n)))))
            }
            _ => self.build_answer(rest),
        };
        answered.map_or_else(unknown, |(name, answer)| (name.to_string(), answer))
    }

    /// What Jenkins answers a poll of the queue item at `/queue/item/<item>`, item being the rest of the path: 404
    /// for one it does not have, or when it forgot them all; cancelled when it cancels them; waiting, which uses up
    /// one of its polls, while it has any left; else the number of the build it started.
    fn queue_item(&mut self, item: &str) -> Answer {
        let item: i64 = item.split('/').next().unwrap_or("").parse().unwrap_or(-1);
        let Some(waiting) = self.queue.get_mut(&item).filter(|_| !self.forgotten) else {
            return Answer::Status(404);
        };
        if self.cancelled {
            return Answer::json(object(&[("cancelled", true.into())]));
        }
        if waiting.left > 0 {
            waiting.left -= 1;
            return Answer::json(object(&[("why", "Waiting for next available executor".into())]));
        }
        Answer::json(object(&[
            ("cancelled", false.into()),
            ("executable", object(&[("number", waiting.number.into())])),
        ]))
    }

    /// The short name of a request for one build's page and what Jenkins answers it with, rest being its path under
    /// JOB_PATH, `<number>/<page>`; None for a build it does not have or a page it does not serve. A poll of a build
    /// that still runs uses up one of its polls, and a read of its quality.md is recorded in quality_reads.
    fn build_answer(&mut self, rest: &str) -> Option<(&'static str, Answer)> {
        let (n, what) = rest.split_once('/').unwrap_or((rest, ""));
        let build = n.parse::<i64>().ok().and_then(|n| self.builds.get_mut(&n))?;
        let answered = match what {
            "api/json" if build.left > 0 => {
                build.left -= 1;
                ("build", Answer::json(object(&[("building", true.into()), ("result", Json::Null)])))
            }
            "api/json" => (
                "build",
                Answer::json(object(&[("building", false.into()), ("result", build.outcome.result.as_str().into())])),
            ),
            "artifact/quality.md" => match &build.outcome.quality {
                Some(quality) if build.left <= build.outcome.after_gate => {
                    let quality = quality.clone();
                    self.quality_reads.push(quality.clone());
                    let reply = self.quality_replies.get(&self.quality_reads.len()).cloned();
                    ("quality", Answer::Body(reply.unwrap_or(quality), None))
                }
                _ => ("quality", Answer::Status(404)),
            },
            "testReport/api/json" if !build.outcome.test_report => ("tests", Answer::Status(404)),
            "testReport/api/json" => {
                let mut cases: Vec<Json> = build
                    .outcome
                    .tests
                    .iter()
                    .map(|(c, t, e)| {
                        object(&[
                            ("className", c.as_str().into()),
                            ("name", t.as_str().into()),
                            ("status", "FAILED".into()),
                            ("errorDetails", e.as_str().into()),
                        ])
                    })
                    .collect();
                cases.push(object(&[
                    ("className", "t".into()),
                    ("name", "ok".into()),
                    ("status", "PASSED".into()),
                    ("errorDetails", Json::Null),
                ]));
                (
                    "tests",
                    Answer::json(object(&[("suites", Json::Array(vec![object(&[("cases", Json::Array(cases))])]))])),
                )
            }
            "consoleText" => ("console", Answer::Body(build.outcome.console.clone(), None)),
            _ => return None,
        };
        Some(answered)
    }
}

impl Http for FakeJenkins {
    fn send(&self, request: &Request) -> Result<Response, HttpError> {
        assert_eq!(request.timeout, HTTP_TIMEOUT);
        let mut state = self.state();
        let method = request.method.as_str();
        let (path, query) = split_url(&request.url);
        let form = request.form.as_deref().map(parse_qsl).unwrap_or_default();
        state.requests.push(Received { method, url: request.url.clone(), form: form.clone() });
        state.auth.push(request.header("Authorization").map(str::to_string));
        if state.down {
            state.names.push("jenkins down".to_string());
            // A reason that quotes the token, to show it never gets any further.
            return Err(HttpError::NoAnswer(format!("[Errno 111] Connection refused (token {JENKINS_TOKEN})")));
        }
        if let Some(&(_, status)) = state.fail.iter().find(|((m, p), _)| *m == method && *p == path) {
            state.names.push(format!("{method} {path} failing"));
            return Err(HttpError::Status(status));
        }
        let (name, answer) = state.answer(method, &path, &parse_qsl(&query), &form);
        state.names.push(name);
        match answer {
            Answer::Status(status) => Err(HttpError::Status(status)),
            Answer::Body(body, location) => Ok(Response {
                status: 200,
                headers: location.map(|l| ("Location".to_string(), l)).into_iter().collect(),
                body: body.into_bytes(),
            }),
        }
    }
}

fn object(pairs: &[(&str, Json)]) -> Json {
    Json::Object(pairs.iter().cloned().collect())
}

/// The path and the query of an absolute URL.
pub(crate) fn split_url(url: &str) -> (String, String) {
    let after_scheme = url.split_once("://").map_or(url, |(_, rest)| rest);
    let path_start = after_scheme.find('/').unwrap_or(after_scheme.len());
    let (path, query) = after_scheme[path_start..].split_once('?').unwrap_or((&after_scheme[path_start..], ""));
    (path.to_string(), query.to_string())
}

/// A query string or form decoded, as urllib.parse.parse_qsl decodes it: `+` a blank, `%XX` a byte, pairs without
/// a value left out.
pub(crate) fn parse_qsl(query: &str) -> Map<String> {
    let decode = |s: &str| {
        let bytes = s.replace('+', " ").into_bytes();
        let mut out = Vec::new();
        let mut i = 0;
        while i < bytes.len() {
            let hex = bytes.get(i + 1..i + 3).and_then(|h| u8::from_str_radix(std::str::from_utf8(h).ok()?, 16).ok());
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
    };
    query
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .filter(|(_, v)| !v.is_empty())
        .map(|(k, v)| (decode(k), decode(v)))
        .collect()
}
