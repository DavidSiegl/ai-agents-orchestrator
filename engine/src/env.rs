//! The environment the engine gives the processes it starts, where its configuration lives (ConfigDir), and the
//! quality gate's credentials, from the environment or the file ci.env.

use std::ffi::OsString;
use std::fs::File;
use std::io::{ErrorKind, Read};
use std::os::unix::fs::PermissionsExt;

use crate::constants::{CI_ENV, GIT_PATHSPEC_ENV};
use crate::error::{os_error, OrchestratorError};
use crate::py::text::utf8_error;
use crate::py::json::Map;

/// The environment for every process the orchestrator starts: its own, without the CI credentials (CI_ENV) and
/// without GIT_PATHSPEC_ENV.
///
/// So the agents, which herdr, git, ssh and gh start or reach, never inherit the tokens. They can still read a file
/// the tokens came from, as the same user. And the git the orchestrator runs reads each pathspec in its default
/// syntax, whatever switch the user exported. An agent does not inherit the switches either, but its pane's shell
/// reads the user's startup files and takes any they set. Over SSH this is the local ssh process's environment; the
/// remote git reads the remote shell's.
pub fn child_env() -> Vec<(OsString, OsString)> {
    without_withheld(std::env::vars_os())
}

/// vars without the variables child_env withholds, in their order.
pub(crate) fn without_withheld(vars: impl Iterator<Item = (OsString, OsString)>) -> Vec<(OsString, OsString)> {
    vars.filter(|(k, _)| !CI_ENV.iter().chain(&GIT_PATHSPEC_ENV).any(|w| k == w)).collect()
}

/// The orchestrator's own configuration directory, on the machine it runs on, and where in it each of its files is:
/// ci.env, the prompt files and the workflow files.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigDir {
    /// The directory's path, without a trailing "/".
    path: String,
}

impl ConfigDir {
    /// The configuration directory of the user running the engine: ai-agents-orchestrator in $XDG_CONFIG_HOME when
    /// that is an absolute path, else in ~/.config, ~ being $HOME, or without it the user's home in /etc/passwd.
    pub fn from_env() -> ConfigDir {
        ConfigDir::for_env(&|name| std::env::var(name).ok(), &passwd_home)
    }

    /// from_env for the environment env gives, by variable name, and home, the user's home without HOME.
    fn for_env(env: &dyn Fn(&str) -> Option<String>, home: &dyn Fn() -> Option<String>) -> ConfigDir {
        let config = env("XDG_CONFIG_HOME").filter(|c| c.starts_with('/'));
        let config = config.unwrap_or_else(|| match env("HOME").or_else(home) {
            Some(home) => format!("{}/.config", home.trim_end_matches('/')),
            None => "~/.config".to_string(),
        });
        ConfigDir::at(&format!("{}/ai-agents-orchestrator", config.trim_end_matches('/')))
    }

    /// The configuration directory at path.
    pub fn at(path: &str) -> ConfigDir {
        let trimmed = path.trim_end_matches('/');
        ConfigDir { path: if trimmed.is_empty() { "/".to_string() } else { trimmed.to_string() } }
    }

    pub fn path(&self) -> &str {
        &self.path
    }

    /// The file the quality gate's credentials come from when the environment lacks them.
    pub fn ci_env_path(&self) -> String {
        self.join("ci.env")
    }

    /// Where a new run finds its prompt files, NAME.md for the Prompt named NAME (crate::prompts).
    pub fn prompts_dir(&self) -> String {
        self.join("prompts")
    }

    /// Where `run --workflow NAME` finds NAME.toml (crate::workflow_file).
    pub fn workflows_dir(&self) -> String {
        self.join("workflows")
    }

    fn join(&self, name: &str) -> String {
        format!("{}/{name}", self.path.trim_end_matches('/'))
    }
}

/// The home directory /etc/passwd gives the user the engine runs as, which os.path.expanduser falls back on without
/// HOME; None when either file cannot be read or names none. Unlike Python's pwd module, it reads the file alone, not
/// whatever else the system's name service has.
fn passwd_home() -> Option<String> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    let uid = status.lines().find_map(|line| line.strip_prefix("Uid:"))?.split_whitespace().next()?.to_string();
    home_of(&std::fs::read_to_string("/etc/passwd").ok()?, &uid)
}

/// The home directory of the user of uid in passwd, a passwd(5) file's text.
fn home_of(passwd: &str, uid: &str) -> Option<String> {
    passwd.lines().find_map(|line| {
        let fields: Vec<&str> = line.split(':').collect();
        (fields.len() >= 7 && fields[2] == uid).then(|| fields[5].to_string())
    })
}

/// CI_ENV from environ, by name, each one it lacks or has empty taken from the file at path, with every other
/// assignment the file holds; a value in environ wins over the file's.
///
/// The file's values stay in the returned map and never enter the process's environment, so child_env needs no
/// help to keep them from the agents. It is read only when something is missing, and refused when others may read
/// it: it holds two tokens. A missing file is no error. Errors: the file is readable by others, cannot be read, is
/// not UTF-8, or holds a line parse_env_file refuses.
pub fn ci_credentials(environ: &dyn Fn(&str) -> Option<String>, path: &str) -> Result<Map<String>, OrchestratorError> {
    let found: Map<String> = CI_ENV
        .iter()
        .filter_map(|k| environ(k).filter(|v| !v.is_empty()).map(|v| (*k, v)))
        .collect();
    if found.len() == CI_ENV.len() {
        return Ok(found);
    }
    // Opening names the file in its error, as Python's open does; a look at the open file or a read of it does not.
    let unreadable = |e: &std::io::Error, named: bool| {
        OrchestratorError(format!("could not read {path}: {}", os_error(e, named.then_some(path))))
    };
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(found),
        Err(e) => return Err(unreadable(&e, true)),
    };
    let metadata = file.metadata().map_err(|e| unreadable(&e, false))?;
    if metadata.is_dir() {
        // Python's open refuses a directory, before any look at its mode.
        return Err(unreadable(&std::io::Error::from_raw_os_error(21), true));
    }
    let mode = metadata.permissions().mode();
    if mode & 0o077 != 0 {
        return Err(OrchestratorError(format!(
            "{path} holds tokens but others may read it; run: chmod 600 {path}"
        )));
    }
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).map_err(|e| unreadable(&e, false))?;
    let text = String::from_utf8(bytes).map_err(|e| {
        OrchestratorError(format!("could not read {path}: {}", utf8_error(e.as_bytes(), &e.utf8_error())))
    })?;
    let mut values = parse_env_file(&text, path)?;
    for (k, v) in found {
        values.insert(k, v);
    }
    Ok(values)
}

/// The NAME=value lines of text, with the quotes, `export ` and comments that a file also sourced by a shell has, by
/// name in the order they first come; a name given twice takes its last value.
///
/// Whatever it accepts means the same to sh: a line ends at "\n" alone, only spaces and tabs are blanks, and a value
/// is quoted, or unquoted without blanks, quotes or shell syntax. Errors, naming path and the line's number but never
/// its content: a line in any other form, which sh would read otherwise or not at all, and a carriage return outside
/// a comment, which a file with Windows line ends puts at the end of every value.
pub fn parse_env_file(text: &str, path: &str) -> Result<Map<String>, OrchestratorError> {
    let mut values = Map::new();
    for (i, raw) in text.split('\n').enumerate() {
        let line = raw.trim_matches([' ', '\t']);
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let number = i + 1;
        if line.contains('\r') {
            return Err(OrchestratorError(format!(
                "{path}:{number}: a carriage return; save the file with Unix line ends"
            )));
        }
        let (name, value) = assignment(line)
            .ok_or_else(|| OrchestratorError(format!("{path}:{number}: expected NAME=value")))?;
        values.insert(name, value.to_string());
    }
    Ok(values)
}

/// The name and value of an assignment line, blanks at its ends removed, in the forms sh reads literally: `export `
/// or not, NAME=, then a value that is single-quoted, double-quoted without what expands inside double quotes, or
/// unquoted without blanks, quotes, a ~ (expanded at the start and after a ":"), a { (brace-expanded in an argument
/// of export) or what sh treats as syntax there; then nothing, or blanks and a comment. No value holds a NUL, which
/// sh drops. None for a line in any other form.
fn assignment(line: &str) -> Option<(&str, &str)> {
    if let Some(rest) = line.strip_prefix("export") {
        let after = rest.trim_start_matches([' ', '\t']);
        if after.len() < rest.len() {
            if let Some(found) = name_and_value(after) {
                return Some(found);
            }
        }
    }
    name_and_value(line)
}

fn name_and_value(text: &str) -> Option<(&str, &str)> {
    let (name, rest) = text.split_once('=')?;
    let mut chars = name.chars();
    let first = chars.next()?;
    if !(first.is_ascii_alphabetic() || first == '_') || !chars.all(|c| c.is_ascii_alphanumeric() || c == '_') {
        return None;
    }
    let (value, after) = if let Some(quoted) = rest.strip_prefix('\'') {
        let end = quoted.find('\'')?;
        let value = &quoted[..end];
        if value.contains('\0') {
            return None;
        }
        (value, &quoted[end + 1..])
    } else if let Some(quoted) = rest.strip_prefix('"') {
        let end = quoted.find(['"', '\\', '$', '`', '\0'])?;
        if !quoted[end..].starts_with('"') {
            return None;
        }
        (&quoted[..end], &quoted[end + 1..])
    } else {
        let end = rest.find(UNQUOTED_EXCLUDED).unwrap_or(rest.len());
        (&rest[..end], &rest[end..])
    };
    let comment = after.trim_start_matches([' ', '\t']);
    let ends_well = after.is_empty() || (comment.len() < after.len() && (comment.is_empty() || comment.starts_with('#')));
    ends_well.then_some((name, value))
}

/// The characters an unquoted value may not hold: blanks, quotes and what sh treats as syntax there.
const UNQUOTED_EXCLUDED: [char; 17] =
    [' ', '\t', '\'', '"', '\\', '$', '`', ';', '&', '|', '<', '>', '(', ')', '~', '{', '\0'];

/// N bytes from the system's random source, /dev/urandom, for run ids and serve's tokens. Errors: it cannot be read.
pub fn random_bytes<const N: usize>() -> std::io::Result<[u8; N]> {
    let mut bytes = [0u8; N];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes)
}

#[cfg(test)]
mod tests;
