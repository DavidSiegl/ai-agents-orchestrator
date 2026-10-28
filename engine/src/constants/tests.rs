use super::*;
use crate::py::json::Json;
use crate::test_support::oracle;

/// A constant as json.dumps writes the Python engine's: a number, a string, or a tuple of them as a list.
trait AsJson {
    fn as_json(&self) -> Json;
}

impl AsJson for &str {
    fn as_json(&self) -> Json {
        Json::from(*self)
    }
}

macro_rules! numbers {
    ($($t:ty),*) => {$(
        impl AsJson for $t {
            fn as_json(&self) -> Json {
                Json::Int(*self as i128)
            }
        }
    )*};
}
numbers!(u32, u64, i32, i64, usize);

impl<T: AsJson, const N: usize> AsJson for [T; N] {
    fn as_json(&self) -> Json {
        Json::Array(self.iter().map(AsJson::as_json).collect())
    }
}

impl AsJson for (&str, &str) {
    fn as_json(&self) -> Json {
        Json::Array(vec![self.0.as_json(), self.1.as_json()])
    }
}

macro_rules! constants {
    ($($name:ident),* $(,)?) => {
        vec![$((stringify!($name), $name.as_json()),)*]
    };
}

#[test]
fn every_constant_is_the_python_engines() {
    let ported = constants![
        RUNS_DIR, WORKTREES_DIR, PROOF_COPIES_DIR, DEFAULT_TURN_TIMEOUT, DEFAULT_MAX_ROUNDS, AGENT_START_TIMEOUT_MS,
        POLL_SECONDS, STALL_SECONDS, PROMPT_DELIVERY_SECONDS, HEARTBEAT_SECONDS, STALE_SECONDS, NETWORK_TIMEOUT,
        GITLEAKS_TIMEOUT, GITLEAKS_INSTALL, GITLEAKS_ALLOW, GITLEAKS_EXTEND_DEPTH, BRANCH_PREFIX, REMOTE,
        CLOSED_BRANCH_GRACE, PR_SECTION_LIMIT, APPROVE, CHANGES_REQUESTED, FINISHED, QUALITY_GATE_FAILED, SUCCEEDED,
        BLOCKED, NO_QUESTION, DEFAULT_MAX_QUALITY_ROUNDS, DEFAULT_MAX_FILES, DEFAULT_MAX_LINES, BUDGET_KEYS,
        HTTP_TIMEOUT, PYPI_TIMEOUT, CI_POLL_SECONDS, QUALITY_TIMEOUT, CI_ENV, CI_TOKENS, GIT_PATHSPEC_ENV,
        JOB_PARAMETERS, CI_REF_PREFIX, LOCAL_REFS_PREFIX, CONSOLE_TAIL_LINES, AGENT_KINDS, DEFAULT_AGENT,
        EXIT_ERROR, EXIT_CHANGES_REQUESTED, EXIT_CONFLICT, EXIT_WAITING, EXIT_INTERRUPTED,
    ];
    let texts = oracle("texts.json");
    let python = texts.get("constants").and_then(Json::as_object).unwrap();
    for (name, value) in &ported {
        assert_eq!(python.get(name).map(Json::dumps), Some(value.dumps()), "{name}");
    }
    // ROLE_LABELS is a dict in Python, a list of pairs here.
    let labels: Vec<(&str, &str)> = python.get("ROLE_LABELS").and_then(Json::as_object).unwrap()
        .iter().map(|(k, v)| (k, v.as_str().unwrap())).collect();
    assert_eq!(labels, ROLE_LABELS);
    assert_eq!(ported.len() + 1, python.len(), "a constant of texts.py's list is not compared here");
}
