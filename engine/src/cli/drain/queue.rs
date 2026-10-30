//! The specs drain runs, as orchestrator.py reads and writes them: DIR/<slug>/spec.md, each with a header of
//! `<key>: <entry>` lines before its first `## ` heading (header_field, with_header_field), read into a queue ordered
//! by slug whose blockers are checked (read_queue), and each spec's header changes written back as drain makes them
//! (QueuedSpec).

use std::collections::HashSet;

use crate::error::{os_error, strerror, OrchestratorError};
use crate::py::text::strip;
use crate::spec_file::{read_spec_file, SpecFile};
use crate::workflow::Human;

/// The keys of the spec header's lines that drain reads. It writes all but BLOCKED_BY_KEY. Blocked by: is a
/// comma-separated list of the specs that must be SPEC_DONE first, each by its slug or a path ending in
/// <slug>/spec.md; Run: names the run drain started for the spec, and PR: that run's pull request.
pub const STATUS_KEY: &str = "Status";
pub const BLOCKED_BY_KEY: &str = "Blocked by";
pub const RUN_KEY: &str = "Run";
pub const PR_KEY: &str = "PR";

/// A Blocked by: value that names no blocker, as the specs and tickets here write it, in lowercase.
const NO_BLOCKERS: [&str; 2] = ["-", "none"];

/// Header lines to set, each a key of the header and its entry, in the order set; a key set again keeps its place.
pub type Entries = Vec<(&'static str, String)>;

/// Set each of more in entries, as Python's dict |= does: a key entries has keeps its place and takes the new entry.
pub fn merge(entries: &mut Entries, more: Entries) {
    for (key, entry) in more {
        match entries.iter_mut().find(|(k, _)| *k == key) {
            Some(found) => found.1 = entry,
            None => entries.push((key, entry)),
        }
    }
}

/// The entry entries sets for key, if it sets one.
pub fn entry<'e>(entries: &'e Entries, key: &str) -> Option<&'e str> {
    entries.iter().find(|(k, _)| *k == key).map(|(_, e)| e.as_str())
}

/// The index of the first line of lines that starts the spec's body, a `## ` heading; lines.len() without one.
fn header_end(lines: &[&str]) -> usize {
    lines.iter().position(|line| line.starts_with("## ")).unwrap_or(lines.len())
}

/// The entry on line when it is key's, `<key>:` and the entry, blanks at its ends removed; None for another line.
fn keyed_text<'l>(line: &'l str, key: &str) -> Option<&'l str> {
    let body = line.trim_end_matches(['\r', '\n']);
    body.strip_prefix(key)?.strip_prefix(':').map(strip)
}

/// The entry on the spec header's first key line, blanks at its ends removed; None when the header has none.
pub fn header_field(text: &str, key: &str) -> Option<String> {
    let lines: Vec<&str> = text.split_inclusive('\n').collect();
    lines[..header_end(&lines)].iter().find_map(|line| keyed_text(line, key)).map(String::from)
}

/// text with the header's first key line made `<key>: <entry>`, or with that line added directly below its Status:
/// line when it has none. Every other line, and every line's ending, stays as it was; an added line ends as the
/// Status: line does, and below a Status: line that ends the file unterminated, ends the file so instead. Errors: the
/// header has neither line.
pub fn with_header_field(text: &str, key: &str, entry: &str) -> Result<String, OrchestratorError> {
    let mut lines: Vec<String> = text.split_inclusive('\n').map(String::from).collect();
    let borrowed: Vec<&str> = lines.iter().map(String::as_str).collect();
    let end = header_end(&borrowed);
    let mut status_at = None;
    for (i, line) in borrowed[..end].iter().enumerate() {
        let ending = &line[line.trim_end_matches(['\r', '\n']).len()..];
        if keyed_text(line, key).is_some() {
            lines[i] = format!("{key}: {entry}{ending}");
            return Ok(lines.concat());
        }
        if status_at.is_none() && keyed_text(line, STATUS_KEY).is_some() {
            status_at = Some(i);
        }
    }
    let Some(status_at) = status_at else {
        return Err(OrchestratorError(format!("the spec has no {STATUS_KEY}: line to add {key}: below")));
    };
    let status_line = &lines[status_at];
    let ending = status_line[status_line.trim_end_matches(['\r', '\n']).len()..].to_string();
    if ending.is_empty() {
        // The Status: line ends the file without a line ending: the new line takes its place as the last.
        lines[status_at].push_str(if text.contains("\r\n") { "\r\n" } else { "\n" });
        lines.push(format!("{key}: {entry}"));
    } else {
        lines.insert(status_at + 1, format!("{key}: {entry}{ending}"));
    }
    Ok(lines.concat())
}

/// The slugs a Blocked by: line's value names, in order; none for a spec without the line, or whose value is one of
/// NO_BLOCKERS.
pub fn blocker_slugs(line: Option<&str>) -> Vec<String> {
    let line = line.unwrap_or("");
    if NO_BLOCKERS.contains(&strip(line).to_lowercase().as_str()) {
        return vec![];
    }
    line.split(',')
        .map(strip)
        .filter(|e| !e.is_empty())
        .map(|e| if e.ends_with("/spec.md") { basename(dirname(e)) } else { e }.to_string())
        .collect()
}

/// path's directory, as Python's os.path.dirname gives it: up to its last slash, trailing slashes removed unless it is
/// all slashes.
fn dirname(path: &str) -> &str {
    let head = &path[..path.rfind('/').map_or(0, |i| i + 1)];
    let trimmed = head.trim_end_matches('/');
    if trimmed.is_empty() {
        head
    } else {
        trimmed
    }
}

/// path's last component, as Python's os.path.basename gives it: after its last slash.
fn basename(path: &str) -> &str {
    &path[path.rfind('/').map_or(0, |i| i + 1)..]
}

/// Make text, line endings as they are, the contents of the file at path on this machine.
///
/// Written aside and renamed into place, so a failed write leaves the old file, not half a new one. A symlink at path
/// is followed, so the link stays and its target changes, and an existing file keeps its permissions. Errors: it
/// cannot be written, naming path.
pub fn replace_local_file(path: &str, text: &str) -> Result<(), OrchestratorError> {
    let real = std::fs::canonicalize(path).map_or_else(|_| path.to_string(), |p| p.to_string_lossy().into_owned());
    let tmp = format!("{real}.tmp.{}", std::process::id());
    let written = (|| {
        std::fs::write(&tmp, text).map_err(|e| os_error(&e, Some(&tmp)))?;
        if let Ok(meta) = std::fs::metadata(&real) {
            std::fs::set_permissions(&tmp, meta.permissions()).map_err(|e| os_error(&e, Some(&tmp)))?;
        }
        std::fs::rename(&tmp, &real).map_err(|e| {
            let repr = crate::py::text::repr;
            format!("{}: {} -> {}", os_error(&e, None), repr(&tmp), repr(&real))
        })
    })();
    written.map_err(|why| {
        let _ = std::fs::remove_file(&tmp);
        OrchestratorError(format!("cannot write {path}: {why}"))
    })
}

/// A spec that drain found, DIR/<slug>/spec.md, with what drain has done to it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct QueuedSpec {
    pub slug: String,
    /// Its path as drain found it, and its text as drain last read it, with drain's changes to its header since, line
    /// endings as in the file.
    pub file: SpecFile,
    /// The slugs on its Blocked by: line, each one of a spec drain found.
    pub blockers: Vec<String>,
    /// Its status as drain first read it.
    pub was: Option<String>,
    /// True once drain has changed its header, or would have but for --dry-run.
    pub changed: bool,
    /// Why the spec is gone: drain could not read it again (read_spec_file's error), deleted or emptied while drain
    /// ran, so it is neither run nor done, and file stays as drain last read it. None while it can be read.
    pub gone: Option<String>,
}

impl QueuedSpec {
    /// Its Status: line's value as drain last read or set it, which a gone spec keeps; None without one.
    pub fn status_as_read(&self) -> Option<String> {
        header_field(&self.file.text, STATUS_KEY)
    }

    /// Its Status: line's value now; None without one, or once it is gone.
    pub fn status(&self) -> Option<String> {
        self.field(STATUS_KEY)
    }

    /// The run id on its Run: line; None without one, or once it is gone.
    pub fn run(&self) -> Option<String> {
        self.field(RUN_KEY)
    }

    /// The pull request's URL on its PR: line; None without one, or once it is gone.
    pub fn pr(&self) -> Option<String> {
        self.field(PR_KEY)
    }

    fn field(&self, key: &str) -> Option<String> {
        self.gone.is_none().then(|| header_field(&self.file.text, key)).flatten()
    }

    /// Take the file as it is on disk now, so an edit made while drain runs is kept, not overwritten, and return true;
    /// or return false, the spec gone, when it cannot be read. A gone spec read again is no longer gone. Its blockers
    /// stay as drain first read them.
    pub fn reread(&mut self) -> bool {
        match read_spec_file(&self.file.path) {
            Ok(file) => {
                self.file = file;
                self.gone = None;
                true
            }
            Err(e) => {
                self.gone = Some(e.0);
                false
            }
        }
    }

    /// Set each of the header's lines that entries names (with_header_field), and, with write, save the file as it is
    /// on disk now with those lines changed; return true. With write, a spec found gone is left as it is: it logs the
    /// entries it does not record to human and returns false. Errors: as with_header_field and replace_local_file.
    pub fn update(&mut self, entries: &Entries, write: bool, human: &dyn Human) -> Result<bool, OrchestratorError> {
        if write && !self.reread() {
            let listed: Vec<String> = entries.iter().map(|(k, v)| format!("{k}: {v}")).collect();
            let gone = self.gone.as_deref().unwrap_or_default();
            human.log(&format!("{}: {gone}; not recording {}", self.slug, listed.join(", ")));
            return Ok(false);
        }
        let mut text = self.file.text.clone();
        for (key, entry) in entries {
            text = with_header_field(&text, key, entry)?;
        }
        if write {
            replace_local_file(&self.file.path, &text)?;
        }
        self.file = SpecFile { path: self.file.path.clone(), text };
        self.changed = true;
        Ok(true)
    }

    /// The line drain prints for a spec it changed: its slug, old and new status, `gone` for a gone one, and pull
    /// request.
    pub fn change_line(&self) -> String {
        let new = if self.gone.is_some() { "gone".to_string() } else { py_str(self.status()) };
        let line = format!("{}: {} -> {new}", self.slug, py_str(self.was.clone()));
        match self.pr() {
            Some(pr) if !pr.is_empty() => format!("{line}  {pr}"),
            _ => line,
        }
    }
}

/// value as Python's str of an optional text writes it: "None" for None.
fn py_str(value: Option<String>) -> String {
    value.unwrap_or_else(|| "None".to_string())
}

/// Every spec_dir/<slug>/spec.md on this machine, ordered by slug. A name in spec_dir that is not UTF-8 is left out.
///
/// Errors, naming the specs: spec_dir cannot be listed, a spec cannot be read (read_spec_file), a Blocked by: line
/// names a slug that has no spec.md, or the blockers form a cycle.
pub fn read_queue(spec_dir: &str) -> Result<Vec<QueuedSpec>, OrchestratorError> {
    let listing = |e: std::io::Error| OrchestratorError(format!("cannot list the specs in {spec_dir}: {}", strerror(&e)));
    let mut names: Vec<String> = std::fs::read_dir(spec_dir)
        .map_err(listing)?
        .map(|entry| entry.map(|e| e.file_name().into_string().ok()))
        .collect::<Result<Vec<_>, _>>()
        .map_err(listing)?
        .into_iter()
        .flatten()
        .collect();
    names.sort();
    let mut specs: Vec<QueuedSpec> = Vec::new();
    for name in names {
        // As os.path.join joins them.
        let sep = if spec_dir.is_empty() || spec_dir.ends_with('/') { "" } else { "/" };
        let path = format!("{spec_dir}{sep}{name}/spec.md");
        if std::path::Path::new(&path).is_file() {
            let file = read_spec_file(&path)?;
            let blockers = blocker_slugs(header_field(&file.text, BLOCKED_BY_KEY).as_deref());
            let was = header_field(&file.text, STATUS_KEY);
            specs.push(QueuedSpec { slug: name, file, blockers, was, changed: false, gone: None });
        }
    }
    let slugs: HashSet<&str> = specs.iter().map(|s| s.slug.as_str()).collect();
    for spec in &specs {
        let missing: Vec<&str> = spec.blockers.iter().map(String::as_str).filter(|b| !slugs.contains(b)).collect();
        if !missing.is_empty() {
            return Err(OrchestratorError(format!(
                "spec {} is blocked by {}, which has no spec.md in {spec_dir}",
                spec.slug,
                missing.join(", ")
            )));
        }
    }
    let blockers: Vec<(&str, &[String])> = specs.iter().map(|s| (s.slug.as_str(), s.blockers.as_slice())).collect();
    if let Some(cycle) = blocker_cycle(&blockers) {
        return Err(OrchestratorError(format!("the specs' blockers form a cycle: {}", cycle.join(" -> "))));
    }
    Ok(specs)
}

/// A cycle among the specs, each by its slug with the slugs that block it, in order, as the slugs along it with the
/// first again at the end; None when there is none. Every blocker must be a slug of blockers.
pub fn blocker_cycle(blockers: &[(&str, &[String])]) -> Option<Vec<String>> {
    fn cycle_from<'b>(
        slug: &'b str,
        path: &mut Vec<&'b str>,
        blockers: &[(&'b str, &'b [String])],
        acyclic: &mut HashSet<&'b str>,
    ) -> Option<Vec<String>> {
        if let Some(at) = path.iter().position(|s| *s == slug) {
            return Some(path[at..].iter().chain([&slug]).map(|s| s.to_string()).collect());
        }
        if acyclic.contains(slug) {
            return None;
        }
        let of = blockers.iter().find(|(s, _)| *s == slug).map_or(&[][..], |(_, b)| *b);
        path.push(slug);
        for b in of {
            if let Some(found) = cycle_from(b, path, blockers, acyclic) {
                return Some(found);
            }
        }
        path.pop();
        acyclic.insert(slug);
        None
    }
    let mut acyclic = HashSet::new();
    blockers.iter().find_map(|(slug, _)| cycle_from(slug, &mut vec![], blockers, &mut acyclic))
}
