//! The secret scan before each push (Host::scan_unpublished): gitleaks over what the push would publish, with the
//! rules taken from the run's base, and the `gitleaks:allow` comments the change adds.

use std::fmt;

use super::{checked, git_timed, Host, DEFAULT_TIMEOUT, IN_DIR};
use crate::constants::{GITLEAKS_ALLOW, GITLEAKS_EXTEND_DEPTH, GITLEAKS_TIMEOUT, REMOTE};
use crate::error::OrchestratorError;
use crate::py::json::Json;
use crate::py::text::{repr, shortened, strip};
use crate::quality::Leak;

/// A push refused because what it would publish holds secrets, as Host::refuse_leaks finds them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LeaksFound {
    /// What the scan found; never empty.
    pub leaks: Vec<Leak>,
    /// Where gitleaks wrote its report.
    pub report_path: String,
}

impl LeaksFound {
    /// How many secrets gitleaks found, as a clause: "gitleaks found 2 secrets".
    pub fn found(&self) -> String {
        let n = self.leaks.len();
        format!("gitleaks found {n} secret{}", if n == 1 { "" } else { "s" })
    }
}

impl fmt::Display for LeaksFound {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let (found, report) = (self.found(), &self.report_path);
        write!(f, "{found} in what the push would publish, so nothing was pushed (report: {report}):")?;
        self.leaks.iter().try_for_each(|leak| write!(f, "\n- {leak}"))
    }
}

/// Host::scan_unpublished, which says what it does.
pub(super) fn scan_unpublished<H: Host + ?Sized>(
    host: &H,
    cwd: &str,
    commit: &str,
    base: &str,
    report_path: &str,
) -> Result<Vec<Leak>, OrchestratorError> {
    let directory = report_path.rsplit_once('/').map_or(report_path, |(d, _)| d);
    let [rules, ignore, source] = ["-rules", "ignore", "-source"].map(|name| format!("{directory}/gitleaks{name}"));
    let config = write_base_rules(host, cwd, base, &rules)?;
    host.write(&ignore, &base_rules_file(host, cwd, base, ".gitleaksignore")?.unwrap_or_default(), false)?;
    // gitleaks also reads <source>/.gitleaksignore, which in the working tree the change could write: it scans from an
    // empty directory, the repository named by GIT_DIR. And a report an earlier scan left would be read as this one's.
    let clear = ["sh", "-c", "rm -rf -- \"$1\" \"$2\" && mkdir -p -- \"$1\"", "_", &source, report_path];
    host.check(&clear, None, DEFAULT_TIMEOUT)?;
    let git_dir = host.git(cwd, &["rev-parse", "--absolute-git-dir"])?;
    let on = host.ssh_target().map(|t| format!(" on {t}")).unwrap_or_default();
    let failed = format!("gitleaks could not scan what the push would publish{on}, so nothing was pushed");
    let remotes = format!("--remotes={REMOTE}");
    let octopus = git_timed(host, cwd, &["rev-list", "--min-parents=3", commit, "--not", &remotes], GITLEAKS_TIMEOUT)?;
    if let Some(merge) = octopus.split_whitespace().next() {
        let short: String = merge.chars().take(12).collect();
        return Err(OrchestratorError(format!(
            "{failed}: merge {short} has three or more parents, and git shows no diff of what such a merge adds; \
             merge its branches one at a time"
        )));
    }
    let log_opts = format!("--diff-merges=remerge --no-textconv {commit} --not {remotes}");
    let empty_tree = host.git(cwd, &["hash-object", "-t", "tree", "/dev/null"])?;
    let git_env = scan_git_env(strip(&git_dir), strip(&empty_tree));
    let log_flag = format!("--log-opts={log_opts}");
    let mut argv = vec!["sh", "-c", IN_DIR, "_", &rules];
    argv.extend(git_env.iter().map(String::as_str));
    argv.extend(["gitleaks", "git", &log_flag, "--redact", "--no-banner", "--no-color", "--exit-code", "1"]);
    argv.extend(["--report-format", "json", "--report-path", report_path, "--config", &config]);
    argv.extend(["--gitleaks-ignore-path", &ignore, &source]);
    let proc = host.run(&argv, None, GITLEAKS_TIMEOUT)?;
    let last = strip(&proc.stderr).rsplit('\n').next().unwrap_or_default();
    let said = shortened(if last.is_empty() { "nothing" } else { last }, 160);
    if proc.status != 0 && proc.status != 1 {
        return Err(OrchestratorError(format!("{failed}: it exited with status {}: {said}", proc.status)));
    }
    let text = host.read(report_path)?;
    let leaks = match &text {
        None => None,
        Some(text) => findings(text)
            .map_err(|e| OrchestratorError(format!("{failed}: its report {report_path} cannot be read: {e}")))?,
    };
    match leaks {
        Some(leaks) if proc.status == 0 || !leaks.is_empty() => {
            let mut leaks = leaks;
            leaks.extend(allow_comments(host, &git_env, &source, &log_opts)?);
            Ok(leaks)
        }
        _ => Err(OrchestratorError(format!(
            "{failed}: it exited with status {} and wrote {}: {said}",
            proc.status,
            if text.is_none() { "no report" } else { "no finding" }
        ))),
    }
}

/// The Leaks of a gitleaks JSON report, text; None when it is no list. Errors, saying why: text is not JSON, or a
/// finding in the list lacks a field or has one of another type.
fn findings(text: &str) -> Result<Option<Vec<Leak>>, String> {
    let Json::Array(items) = Json::parse(text)? else {
        return Ok(None);
    };
    items
        .iter()
        .map(|f| {
            let field = |key: &str| f.get(key).ok_or_else(|| repr(key));
            let string = |key: &str| field(key)?.as_str().map(String::from).ok_or(format!("{key} is no string"));
            // In the order the Python engine reads them, so that the first field missing is the one named.
            let (rule, file) = (string("RuleID")?, string("File")?);
            let line = match field("StartLine")? {
                Json::Int(n) => u64::try_from(*n).ok(),
                Json::Float(x) if x.is_finite() && *x >= 0.0 => Some(x.trunc() as u64),
                Json::Str(s) => super::python_int(s).and_then(|n| u64::try_from(n).ok()),
                _ => None,
            };
            let line = line.ok_or("StartLine is no line number")?;
            Ok(Leak { rule, file, line, commit: string("Commit")?, fingerprint: string("Fingerprint")? })
        })
        .collect::<Result<Vec<Leak>, String>>()
        .map(Some)
}

/// A Leak for each line holding `gitleaks:allow` that the commits log_opts selects add, as git log -p shows them, read
/// as change_diff's diffs are. git runs as gitleaks does, in source under git_env (scan_git_env), so both read the same
/// patch.
fn allow_comments<H: Host + ?Sized>(
    host: &H,
    git_env: &[String],
    source: &str,
    log_opts: &str,
) -> Result<Vec<Leak>, OrchestratorError> {
    let mut argv: Vec<&str> = git_env.iter().map(String::as_str).collect();
    argv.extend(["git", "-C", source, "-c", "core.quotePath=false", "log", "-p", "-U0", "--no-color"]);
    argv.extend(["--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", "--format=%x00%H"]);
    argv.extend(log_opts.split_whitespace());
    let out = checked(host, &argv, host.run_verbatim(&argv, None, GITLEAKS_TIMEOUT)?)?;
    let mut leaks = vec![];
    // Each commit's patch follows its sha on a line of its own that starts with a NUL; a NUL inside a line of a patch
    // does not start one.
    let mut text = b"\n".to_vec();
    text.extend(out);
    for block in split_bytes(&text, b"\n\0").into_iter().skip(1) {
        let (commit, patch) = match block.iter().position(|&b| b == b'\n') {
            Some(i) => (&block[..i], &block[i + 1..]),
            None => (block, &b""[..]),
        };
        let commit = String::from_utf8_lossy(commit);
        for (path, lines) in crate::patch::added_lines(patch)?.iter() {
            for (line, added) in lines {
                if contains(added, GITLEAKS_ALLOW.as_bytes()) {
                    leaks.push(Leak {
                        rule: "gitleaks-allow".into(),
                        file: path.to_string(),
                        line: *line,
                        commit: commit.to_string(),
                        fingerprint: format!("{commit}:{path}:gitleaks-allow:{line}"),
                    });
                }
            }
        }
    }
    Ok(leaks)
}

/// The `env` prefix under which the secret scan runs git, and gitleaks, which runs git: the repository at git_dir,
/// with no tree's attributes, so that no .gitattributes the change commits or the base holds decides how git diffs a
/// file; git diffs as text every file it takes for text by its content. empty_tree is the repository's empty tree.
/// $GIT_DIR/info/attributes and core.attributesFile still apply, which only the repository's or the user's own files
/// set, never a change.
///
/// GIT_DIR, from a directory outside the working tree, leaves out the working tree's .gitattributes. attr.tree goes in
/// as GIT_CONFIG_COUNT sets config, so that it reaches the git gitleaks starts and overrides the user's. GIT_WORK_TREE,
/// GIT_ATTR_SOURCE and GIT_CONFIG_PARAMETERS (which `git -c` exports, read after GIT_CONFIG_COUNT) would bring a
/// tree's attributes back, and are unset.
fn scan_git_env(git_dir: &str, empty_tree: &str) -> Vec<String> {
    let mut env: Vec<String> = ["env", "-u", "GIT_WORK_TREE", "-u", "GIT_ATTR_SOURCE", "-u", "GIT_CONFIG_PARAMETERS"]
        .map(String::from)
        .into();
    env.push(format!("GIT_DIR={git_dir}"));
    env.extend(["GIT_CONFIG_COUNT=1".into(), "GIT_CONFIG_KEY_0=attr.tree".into()]);
    env.push(format!("GIT_CONFIG_VALUE_0={empty_tree}"));
    env
}

/// Make the directory rules hold base's gitleaks rules, for a gitleaks run with rules as its working directory, and
/// return the path to give it as --config, in rules: base's .gitleaks.toml, or gitleaks' defaults where base has none.
/// Each file it extends with `[extend] path`, and each one that file extends in turn, as far as GITLEAKS_EXTEND_DEPTH
/// says gitleaks reads, is written from base at that path under rules: gitleaks reads such a path from its working
/// directory, and base's authors write it from the repository's top level, so every file gitleaks reads is base's.
/// Whatever rules held before is removed.
///
/// Errors, failing closed and naming the file: a file in the chain is refused by extended_file or base_rules_file, or
/// extends what is no file in base or a file already in the chain; and git cannot read base.
fn write_base_rules<H: Host + ?Sized>(
    host: &H,
    cwd: &str,
    base: &str,
    rules: &str,
) -> Result<String, OrchestratorError> {
    host.check(&["rm", "-rf", "--", rules], None, DEFAULT_TIMEOUT)?;
    let mut chain = vec![".gitleaks.toml".to_string()];
    let refused =
        |chain: &[String], why: String| base_rules_refused(base, &format!("{} {why}", chain[chain.len() - 1]));
    let config = format!("{rules}/{}", chain[0]);
    let mut spelled = chain[0].clone();
    let mut text = base_rules_file(host, cwd, base, &chain[0])?
        .unwrap_or_else(|| "[extend]\nuseDefault = true\n".to_string());
    loop {
        host.write(&format!("{rules}/{spelled}"), &text, false)?;
        if chain.len() > GITLEAKS_EXTEND_DEPTH as usize {
            return Ok(config);
        }
        let Some(extended) = extended_file(&text).map_err(|why| refused(&chain, why))? else {
            return Ok(config);
        };
        // gitleaks opens the path as spelled, and the copy is written so; every directory on the way is one this
        // makes, so the path reaches that copy, which in base is at the path's normal form.
        let name = normpath(&extended);
        if chain.contains(&name) {
            let round = [chain.as_slice(), std::slice::from_ref(&name)].concat().join(" -> ");
            return Err(refused(&chain, format!("extends {}, which comes back round: {round}", repr(&extended))));
        }
        text = match base_rules_file(host, cwd, base, &name)? {
            Some(text) => text,
            None => return Err(refused(&chain, format!("extends {}, which is no file in base", repr(&extended)))),
        };
        chain.push(name);
        spelled = extended;
    }
}

/// base's file at name, one the secret scan takes its rules from, as file_at gives it, as text; None where base has
/// none. Errors, failing closed as base_rules_refused says: base has a symbolic link there, whose target this does not
/// follow, or a file that is not UTF-8; and as file_at's.
fn base_rules_file<H: Host + ?Sized>(
    host: &H,
    cwd: &str,
    base: &str,
    name: &str,
) -> Result<Option<String>, OrchestratorError> {
    match blob_at(host, cwd, base, name)? {
        None => Ok(None),
        Some((_, true)) => {
            Err(base_rules_refused(base, &format!("{name} is a symbolic link in base, and only a file is read there")))
        }
        Some((bytes, false)) => {
            String::from_utf8(bytes).map(Some).map_err(|_| base_rules_refused(base, &format!("{name} is not UTF-8")))
        }
    }
}

/// What Host::file_at gives for path, and whether commit has a symbolic link there, whose bytes are then the path it
/// points to; None where file_at gives None. Errors: as file_at's.
pub(super) fn blob_at<H: Host + ?Sized>(
    host: &H,
    cwd: &str,
    commit: &str,
    path: &str,
) -> Result<Option<(Vec<u8>, bool)>, OrchestratorError> {
    // Each entry is `<mode> <type> <object>\t<path>`; a directory's type is tree, a symbolic link's mode 120000. A path
    // ending in "/", or none, lists a directory's entries instead, so only an entry at path itself counts.
    let literal = format!(":(top,literal){path}");
    let listing = host.git_verbatim(cwd, &["ls-tree", "--full-tree", "-z", commit, "--", &literal])?;
    let entry = listing.split(|&b| b == 0).next().unwrap_or_default();
    let (fields, listed) = match entry.iter().position(|&b| b == b'\t') {
        Some(i) => (&entry[..i], &entry[i + 1..]),
        None => (entry, &b""[..]),
    };
    let mut fields = fields.split(|&b| b == b' ');
    let (mode, kind, blob) = (fields.next().unwrap_or_default(), fields.next(), fields.next());
    let (Some(b"blob"), Some(blob)) = (kind, blob) else {
        return Ok(None);
    };
    if listed != path.as_bytes() {
        return Ok(None);
    }
    let blob = String::from_utf8_lossy(blob);
    Ok(Some((host.git_verbatim(cwd, &["cat-file", "blob", &blob])?, mode == b"120000")))
}

/// The error the secret scan fails closed with when it cannot take its rules from base, a commit, as gitleaks would
/// read them; why names the file and says what is wrong with it, and follows "its".
fn base_rules_refused(base: &str, why: &str) -> OrchestratorError {
    let short: String = base.chars().take(12).collect();
    OrchestratorError(format!("nothing was pushed: the secret scan takes its rules from base {short}, but its {why}"))
}

/// The path of the file that config, a gitleaks config's text, extends with `[extend] path`, as config spells it, from
/// the repository's top level, from which a gitleaks run there reads it; None where config extends none (no [extend],
/// no path, or an empty one). gitleaks reads its config's keys whatever their case, so `[Extend] Path` counts too.
///
/// Errors, saying why in words that follow the config file's name: config is not TOML, gives [extend] or its path
/// twice in letters that differ only in case, or one that is not a table or a string, or the path is absolute, holds a
/// NUL, or leaves the repository (`..`).
fn extended_file(config: &str) -> Result<Option<String>, String> {
    let parsed = crate::workflow_file::parse_toml(config).map_err(|e| format!("is not TOML: {e}"))?;
    let path = match gitleaks_key(&parsed, "extend", true)? {
        Some(extend) => gitleaks_key(extend, "path", false)?,
        None => None,
    };
    let Some(path) = path.and_then(Json::as_str).filter(|p| !p.is_empty()) else {
        return Ok(None);
    };
    let name = normpath(path);
    let why = if path.starts_with('/') {
        "is an absolute path"
    } else if path.contains('\0') {
        "holds a NUL"
    } else if name == ".." || name.starts_with("../") {
        "leaves the repository"
    } else if name == "." {
        "is the top level, no file"
    } else {
        return Ok(Some(path.to_string()));
    };
    Err(format!("extends {}, which {why}", repr(path)))
}

/// table's entry for key, a lower-case key of a gitleaks config, in whatever case table spells it, as gitleaks reads
/// it; None where table has none. Errors, saying why as extended_file's do: table spells key more than one way, or its
/// entry is not a table (a_table) or a string (not a_table).
fn gitleaks_key<'a>(table: &'a Json, key: &str, a_table: bool) -> Result<Option<&'a Json>, String> {
    let found: Vec<&Json> = match table.as_object() {
        Some(map) => map.iter().filter(|(k, _)| k.to_lowercase() == key).map(|(_, v)| v).collect(),
        None => vec![],
    };
    if found.len() > 1 {
        return Err(format!("gives {key} {} times, in letters that differ only in case", found.len()));
    }
    let Some(value) = found.first() else {
        return Ok(None);
    };
    let fits = if a_table { value.as_object().is_some() } else { value.as_str().is_some() };
    if !fits {
        return Err(format!("gives a {key} that is not a {}", if a_table { "table" } else { "string" }));
    }
    Ok(Some(value))
}

/// path in its normal form, as posixpath.normpath gives it: "." and empty components dropped, each ".." taking the one
/// before it away where there is one to take, and "." for nothing; one leading "/" kept, or two, as POSIX allows.
fn normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".to_string();
    }
    let slashes = path.len() - path.trim_start_matches('/').len();
    let lead = match slashes {
        0 => "",
        2 => "//",
        _ => "/",
    };
    let mut parts: Vec<&str> = vec![];
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." if lead.is_empty() && (parts.is_empty() || parts.last() == Some(&"..")) => parts.push(part),
            ".." => {
                parts.pop();
            }
            _ => parts.push(part),
        }
    }
    let joined = format!("{lead}{}", parts.join("/"));
    if joined.is_empty() {
        ".".to_string()
    } else {
        joined
    }
}

/// Whether needle occurs in haystack.
fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

/// bytes split at each occurrence of separator, as Python's bytes.split(separator).
fn split_bytes<'a>(bytes: &'a [u8], separator: &[u8]) -> Vec<&'a [u8]> {
    let mut parts = vec![];
    let mut start = 0;
    let mut i = 0;
    while i + separator.len() <= bytes.len() {
        if &bytes[i..i + separator.len()] == separator {
            parts.push(&bytes[start..i]);
            i += separator.len();
            start = i;
        } else {
            i += 1;
        }
    }
    parts.push(&bytes[start..]);
    parts
}

#[cfg(test)]
mod tests;
