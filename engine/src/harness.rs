//! The agent harnesses a role can run in (AGENT_KINDS, herdr's `--kind`), as orchestrator.py's "Agent harnesses"
//! section and its session logs know them: how each harness's agent starts, resumes a saved session and is granted a
//! directory ([`Harness::launch_args`]), and where and how it records the tokens a session used ([`SessionLog`],
//! [`Harness::session_usage`]).
//!
//! What Python keeps in four tables by harness (PERMISSION_ARGS, RESUME_ARGS, ADD_DIR_ARGS and SESSION_LOGS) is one
//! row per harness here, in [`HARNESSES`], so that adding a harness is adding a row.

use std::collections::HashSet;

use crate::constants::AGENT_KINDS;
use crate::error::OrchestratorError;
use crate::py::json::{Json, Map};

/// How each role's agent starts: the permission mode, and by role its model and its harness (kinds). A role missing
/// from models starts on its harness's default model, and one missing from kinds in DEFAULT_AGENT.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AgentSettings {
    /// --permission-mode as given, in Claude Code's names; None passes none to any harness.
    pub permission_mode: Option<String>,
    /// By role, the model its agent starts on.
    pub models: Map<String>,
    /// By role, the harness its agent starts in, one of AGENT_KINDS.
    pub kinds: Map<String>,
}

/// What the engine knows of one agent harness: the arguments its agent starts with, and its session log.
#[derive(Debug, PartialEq, Eq)]
pub struct Harness {
    /// Its name, as herdr's --kind and AGENT_KINDS have it.
    pub kind: &'static str,
    /// How it takes --permission-mode, which is in Claude Code's names.
    permissions: Permissions,
    /// How it resumes a saved session.
    pub(crate) resume: Resume,
    /// The flag that grants its agent a directory outside its working directory, before the directory; None when it
    /// has none. A worktree run's handoff files are in the project's run directory, not the worktree.
    add_dir: Option<&'static str>,
    /// Where and how it records the tokens of a session; None when it records none the engine reads, so that its
    /// turns' tokens are "unknown" (docs/design.md, Token usage).
    pub session_log: Option<SessionLog>,
}

/// How a harness takes --permission-mode.
#[derive(Debug, PartialEq, Eq)]
enum Permissions {
    /// As `--permission-mode <mode>`, every mode as it is named: Claude Code's own.
    AsNamed,
    /// By its own arguments for each mode it has an equivalent of; a mode not listed has none, and is not passed.
    Equivalents(&'static [(&'static str, &'static [&'static str])]),
}

/// How a harness resumes a saved session: by this word before the session id.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Resume {
    /// A flag, after the permission arguments.
    Flag(&'static str),
    /// A subcommand, which has to come before every other argument: codex's `resume`.
    Subcommand(&'static str),
}

/// Where a harness keeps the record of a session's tokens, and how it writes it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SessionLog {
    /// A glob relative to the home directory of the machine the agents run on, `{session}` standing for the session
    /// id herdr reports (crate::host::Host::session_log finds it).
    pub glob: &'static str,
    /// How it records the tokens of each answer of the model.
    pub format: LogFormat,
}

/// How a session log records the tokens of an answer of the model, one JSON object a line.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LogFormat {
    /// Claude Code's: `{"type": "assistant", "timestamp", "message": {"id", "usage": {"input_tokens",
    /// "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}}}`, one line per content block of
    /// an answer, each with the whole answer's usage.
    ClaudeCode,
    /// pi's: `{"timestamp", "message": {"role": "assistant", "usage": {"input", "output", "cacheRead",
    /// "cacheWrite"}}}`, one line per answer.
    Pi,
}

/// Every harness a role can run in, in AGENT_KINDS' order.
pub const HARNESSES: [Harness; 5] = [
    Harness {
        kind: "claude",
        permissions: Permissions::AsNamed,
        resume: Resume::Flag("--resume"),
        add_dir: Some("--add-dir"),
        session_log: Some(SessionLog { glob: ".claude/projects/*/{session}.jsonl", format: LogFormat::ClaudeCode }),
    },
    Harness {
        kind: "codex",
        // Codex asks before acting by default, so its default needs no flag.
        permissions: Permissions::Equivalents(&[
            ("default", &[]),
            ("acceptEdits", &["--full-auto"]),
            ("bypassPermissions", &["--dangerously-bypass-approvals-and-sandbox"]),
            ("plan", &["--sandbox", "read-only"]),
        ]),
        resume: Resume::Subcommand("resume"),
        add_dir: Some("--add-dir"),
        session_log: None,
    },
    Harness {
        kind: "gemini",
        permissions: Permissions::Equivalents(&[
            ("default", &["--approval-mode", "default"]),
            ("acceptEdits", &["--approval-mode", "auto_edit"]),
            ("bypassPermissions", &["--approval-mode", "yolo"]),
        ]),
        resume: Resume::Flag("--resume"),
        add_dir: Some("--include-directories"),
        session_log: None,
    },
    Harness {
        kind: "opencode",
        permissions: Permissions::Equivalents(&[]),
        resume: Resume::Flag("--session"),
        add_dir: None,
        session_log: None,
    },
    Harness {
        kind: "pi",
        permissions: Permissions::Equivalents(&[]),
        resume: Resume::Flag("--session"),
        add_dir: None,
        session_log: Some(SessionLog { glob: ".pi/agent/sessions/*/*_{session}.jsonl", format: LogFormat::Pi }),
    },
];

/// The harness of the name kind. Errors: kind is not one of AGENT_KINDS, as a hand-edited state.json can name; the
/// Python engine fails on it with a KeyError.
pub fn harness(kind: &str) -> Result<&'static Harness, OrchestratorError> {
    HARNESSES.iter().find(|h| h.kind == kind).ok_or_else(|| {
        OrchestratorError(format!(
            "agent {kind} is not a harness this orchestrator supports; it takes {}",
            AGENT_KINDS.join(", ")
        ))
    })
}

impl Harness {
    /// The harness's arguments for a --permission-mode, or None when it has no equivalent of mode.
    pub fn permission_args(&self, mode: &str) -> Option<Vec<String>> {
        match self.permissions {
            Permissions::AsNamed => Some(vec!["--permission-mode".to_string(), mode.to_string()]),
            Permissions::Equivalents(table) => {
                table.iter().find(|(m, _)| *m == mode).map(|(_, args)| args.iter().map(|a| a.to_string()).collect())
            }
        }
    }

    /// The arguments a role's agent starts with in this harness, after herdr's `--`: permission_mode's equivalent
    /// (none when the harness has none), into session if given one, on model if given one, and with access to
    /// extra_dir where the harness can grant it. An empty string is none, as Python takes it.
    pub fn launch_args(
        &self,
        permission_mode: Option<&str>,
        model: Option<&str>,
        session: Option<&str>,
        extra_dir: Option<&str>,
    ) -> Vec<String> {
        fn given(value: Option<&str>) -> Option<&str> {
            value.filter(|v| !v.is_empty())
        }
        let (permission_mode, model) = (given(permission_mode), given(model));
        let (session, extra_dir) = (given(session), given(extra_dir));
        let mut args = permission_mode.and_then(|mode| self.permission_args(mode)).unwrap_or_default();
        if let Some(session) = session {
            match self.resume {
                Resume::Flag(flag) => args.extend([flag.to_string(), session.to_string()]),
                Resume::Subcommand(word) => {
                    args.splice(0..0, [word.to_string(), session.to_string()]);
                }
            }
        }
        if let Some(model) = model {
            args.extend(["--model".to_string(), model.to_string()]);
        }
        if let (Some(dir), Some(flag)) = (extra_dir, self.add_dir) {
            args.extend([flag.to_string(), dir.to_string()]);
        }
        args
    }

    /// The token usage this harness's session log, as its SessionLog finds it, records for each answer of the model,
    /// oldest first; none for a harness without a session log. log is the file as it is on disk, each line read as
    /// json.loads reads it but for a lone surrogate (Json::parse_lossy), as which os.fsdecode reads a byte that is not
    /// UTF-8 too. A line that is not a record of an answer, or whose counts are not all integers, is skipped. Claude
    /// Code writes one line per content block of an answer, each with the answer's usage, so each answer counts once,
    /// at its first line that names its usage and time.
    pub fn session_usage(&self, log: &[u8]) -> Vec<Usage> {
        let Some(SessionLog { format, .. }) = self.session_log else { return vec![] };
        // The ids of the Claude Code answers counted so far.
        let mut seen = HashSet::new();
        let mut out = vec![];
        for line in log.split(|b| *b == b'\n') {
            let Some(usage) = answer_usage(format, &String::from_utf8_lossy(line), &mut seen) else { continue };
            out.push(usage);
        }
        out
    }
}

/// The tokens of one answer of the model, as a session log records them.
#[derive(Clone, Debug, PartialEq)]
pub struct Usage {
    /// When it was written, in seconds since the epoch, as Python's datetime.timestamp() gives them.
    pub when: f64,
    /// Its tokens, by kind.
    pub tokens: Tokens,
}

/// Token counts, by kind, as metrics.json writes them. A count is whatever integer the log holds, as Python sums it.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Tokens {
    /// Input tokens not read from the cache: Claude Code's input_tokens, pi's input.
    pub input: i128,
    /// Output tokens: Claude Code's output_tokens, pi's output.
    pub output: i128,
    /// Input tokens read from the cache: Claude Code's cache_read_input_tokens, pi's cacheRead; 0 when not logged.
    pub cache_read: i128,
    /// Input tokens written to the cache: Claude Code's cache_creation_input_tokens, pi's cacheWrite; 0 when not
    /// logged.
    pub cache_write: i128,
}

impl Tokens {
    /// The counts as metrics.json and state.json write them: `{"input", "output", "cache_read", "cache_write"}`.
    pub fn to_json(self) -> Json {
        let mut map = Map::new();
        map.insert("input", Json::Int(self.input));
        map.insert("output", Json::Int(self.output));
        map.insert("cache_read", Json::Int(self.cache_read));
        map.insert("cache_write", Json::Int(self.cache_write));
        Json::Object(map)
    }

    /// These counts and other's, added.
    pub fn plus(self, other: Tokens) -> Tokens {
        Tokens {
            input: self.input + other.input,
            output: self.output + other.output,
            cache_read: self.cache_read + other.cache_read,
            cache_write: self.cache_write + other.cache_write,
        }
    }
}

/// A Claude Code message id as Python's set of them tells ids apart: a number equal to another is the same id, true
/// being 1 and false 0, and a missing id the same as null.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum MessageId {
    None,
    Str(String),
    /// A whole number, whether it was written as an int, a float or a bool.
    Int(i128),
    /// The bits of any other float; one JSON reads is never NaN, and -0.0 is Int(0).
    Float(u64),
}

impl MessageId {
    /// The id value is; None for a list or an object, which a set cannot hold (Python's TypeError).
    fn of(value: Option<&Json>) -> Option<MessageId> {
        Some(match value {
            None | Some(Json::Null) => MessageId::None,
            Some(Json::Str(s)) => MessageId::Str(s.clone()),
            Some(Json::Bool(b)) => MessageId::Int(i128::from(*b)),
            Some(Json::Int(n)) => MessageId::Int(*n),
            Some(Json::Float(x)) if x.fract() == 0.0 && x.abs() < 1e38 => MessageId::Int(*x as i128),
            Some(Json::Float(x)) => MessageId::Float(x.to_bits()),
            Some(Json::Array(_) | Json::Object(_)) => return None,
        })
    }
}

/// The usage one line of a session log in format records, as session_usage reads it: None for a line that is not a
/// record of an answer, as Python's loop skips it. seen holds the ids of the Claude Code answers counted so far, and
/// gains this line's, as Python's set does, before its counts are read.
fn answer_usage(format: LogFormat, line: &str, seen: &mut HashSet<MessageId>) -> Option<Usage> {
    // Each step in the order Python's takes it: a step that fails skips the line, and the steps before it have had
    // their effect on seen.
    let entry = Json::parse_lossy(line).ok()?;
    let entry = entry.as_object()?;
    let message = entry.get("message")?.as_object()?;
    let usage = message.get("usage")?;
    let when = crate::py::datetime::fromisoformat_timestamp(entry.get("timestamp")?.as_str()?).ok()?;
    let keys = match format {
        LogFormat::ClaudeCode => {
            if entry.get("type").and_then(Json::as_str) != Some("assistant") {
                return None;
            }
            if !seen.insert(MessageId::of(message.get("id"))?) {
                return None;
            }
            ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]
        }
        LogFormat::Pi => {
            if message.get("role").and_then(Json::as_str) != Some("assistant") {
                return None;
            }
            ["input", "output", "cacheRead", "cacheWrite"]
        }
    };
    // The input and output counts must be there, and the cache counts are 0 when they are not; each is an int, not a
    // bool or a float (Python's `type(v) is int`).
    let usage = usage.as_object()?;
    let (input, output) = (usage.get(keys[0])?, usage.get(keys[1])?);
    let cache = |key: &str| usage.get(key).map_or(Some(0), Json::as_int);
    let tokens = Tokens {
        input: input.as_int()?,
        output: output.as_int()?,
        cache_read: cache(keys[2])?,
        cache_write: cache(keys[3])?,
    };
    Some(Usage { when, tokens })
}

#[cfg(test)]
mod tests;
