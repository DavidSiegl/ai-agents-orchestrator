//! The new-dependencies check: the packages a change adds to the project, judged against the spec and PyPI, and the
//! readers of the manifests that say what a project depends on (docs/quality-gate.md, "New dependencies"). The
//! projects are Python projects with uv, as the gate's targets are.

use std::cell::RefCell;
use std::collections::BTreeSet;

use ::toml::{Table, Value};

use super::{CheckFindings, Finding, RunChange};
use crate::error::OrchestratorError;
use crate::py::json::Map;
use crate::py::text::{is_space, utf8_error};
use crate::py::toml::{json_dumps, loads, repr, str as py_str, type_name};
use crate::pypi::{PyPI, PyPIProject};

/// A new dependency whose first release is fewer days old than this is flagged: few have used it yet, and a name
/// registered for the occasion is the slopsquatting move.
pub const YOUNG_PACKAGE_DAYS: u64 = 14;
/// PyPI's index as uv.lock and `[[tool.uv.index]]` name it; a package from any other index is not PyPI's to vouch
/// for.
pub const PYPI_INDEX_URL: &str = "https://pypi.org/simple";

/// Reads a file of one side of the change by its path from the top level: its bytes, None where it has none. Errors:
/// RunChange::file_at's.
pub type ReadFile<'a> = dyn Fn(&str) -> Result<Option<Vec<u8>>, OrchestratorError> + 'a;

/// The kinds of place a package comes from other than PyPI, as uv's sources name them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SourceKind {
    /// A member of the project's workspace.
    Workspace,
    Path,
    Git,
    Url,
    /// An index other than PyPI's.
    Index,
    /// A uv source of a shape this reader does not know.
    Unknown,
}

/// Where a package comes from when it is not PyPI.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PackageSource {
    pub kind: SourceKind,
    /// The path, repository, URL or index URL, as the manifest gives it; for Unknown, the whole entry, as an inline
    /// TOML table; "" for Workspace.
    pub place: String,
}

impl PackageSource {
    fn new(kind: SourceKind, place: impl Into<String>) -> PackageSource {
        PackageSource { kind, place: place.into() }
    }

    /// Where it comes from, as a finding says it.
    pub fn describe(&self) -> String {
        let place = &self.place;
        match self.kind {
            SourceKind::Workspace => "a member of the project's workspace, not from PyPI".to_string(),
            SourceKind::Index => format!("from the index {place}, not checked against PyPI"),
            SourceKind::Unknown => {
                format!("from the uv source {place}, which this check does not know, so where it comes from is not checked")
            }
            SourceKind::Path => format!("from the path {place}, not from PyPI"),
            SourceKind::Git => format!("from the repository {place}, not from PyPI"),
            SourceKind::Url => format!("from {place}, not from PyPI"),
        }
    }
}

/// Packages by package_key, each with where it comes from, None for PyPI.
pub type Sources = Map<Option<PackageSource>>;

/// What a project's manifests say it depends on: each package by package_key, with where it comes from, None for
/// PyPI.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Dependencies {
    /// The project's own choice, which the spec must name.
    pub direct: Sources,
    /// Every package its lock file pins, the project and its workspace aside.
    pub locked: Sources,
    /// Why, by path, for each manifest that cannot be read; direct and locked then come from the others only.
    pub unreadable: Map<String>,
}

/// A kind of project's dependency manifests, at the repository's top level, and how to read them. Manifests anywhere
/// else, such as test fixtures, are not the project's.
pub struct DependencyEcosystem {
    /// The file that lists the project's own dependencies.
    pub manifest: &'static str,
    /// The file beside it that pins every package they pull in.
    pub lock_file: &'static str,
    /// The project's Dependencies, given each file by its path; each manifest it cannot read is in their
    /// unreadable, and the others are still read. Errors: the reader's.
    pub read: fn(&ReadFile) -> Result<Dependencies, OrchestratorError>,
}

/// The ecosystems whose new packages the new-dependencies check judges. Python's so far, whose index is PyPI; another
/// ecosystem's needs its own index too.
pub const DEPENDENCY_ECOSYSTEMS: [DependencyEcosystem; 1] =
    [DependencyEcosystem { manifest: "pyproject.toml", lock_file: "uv.lock", read: python_dependencies }];

/// A Python package's name as PyPI compares names (PEP 503): lower case, each run of -, _ and . one -.
pub fn package_key(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    let mut in_run = false;
    for c in name.chars() {
        if matches!(c, '-' | '_' | '.') {
            if !in_run {
                out.push('-');
            }
            in_run = true;
        } else {
            out.push(c);
            in_run = false;
        }
    }
    out.to_lowercase()
}

/// DependencyEcosystem::read of a Python project with uv.
///
/// Its direct dependencies are pyproject.toml's `[project]` dependencies and optional-dependencies and its
/// `[dependency-groups]`, an `{include-group}` entry and the project itself left out. A dependency's source is its
/// `[tool.uv.sources]` entry, else its PEP 508 URL, else the `[[tool.uv.index]]` marked default, else the index
/// uv.lock has it from. The locked packages are uv.lock's, the project and its workspace members left out.
pub fn python_dependencies(read: &ReadFile) -> Result<Dependencies, OrchestratorError> {
    let mut found = Dependencies::default();
    match toml_manifest(read, "uv.lock")?.and_then(|lock| uv_lock_packages(&lock)) {
        Ok(locked) => found.locked = locked,
        Err(why) => found.unreadable.insert("uv.lock", why),
    }
    match toml_manifest(read, "pyproject.toml")?.and_then(|pyproject| pyproject_dependencies(&pyproject)) {
        Ok(direct) => {
            found.direct = direct.into_iter().map(|(key, source)| {
                let source = source.or_else(|| found.locked.get(&key).cloned().flatten());
                (key, source)
            }).collect();
        }
        Err(why) => found.unreadable.insert("pyproject.toml", why),
    }
    Ok(found)
}

/// The table of the TOML file at path; an empty one where there is none; why not, for one that is not UTF-8, as
/// TOML must be, or not TOML. Errors: read's.
fn toml_manifest(read: &ReadFile, path: &str) -> Result<Result<Table, String>, OrchestratorError> {
    let Some(bytes) = read(path)? else { return Ok(Ok(Table::new())) };
    Ok(match std::str::from_utf8(&bytes) {
        Err(e) => Err(format!("it is not UTF-8: {}", utf8_error(&bytes, &e))),
        Ok(text) => loads(text).map_err(|why| format!("it is not TOML: {why}")),
    })
}

/// python_dependencies's direct dependencies of a pyproject.toml, parsed; why not, for one of another shape.
fn pyproject_dependencies(pyproject: &Table) -> Result<Sources, String> {
    let project = toml_table(pyproject, "project")?;
    let uv = toml_table(toml_table(pyproject, "tool")?, "uv")?;
    let sources = uv_sources(uv)?;
    let default = uv_indexes(uv)?.into_iter().find(|i| i.get("default") == Some(&Value::Boolean(true))).and_then(|i| index_source(i.get("url")));
    let own = match project.get("name").filter(|name| truthy(name)) {
        None => String::new(),
        Some(Value::String(name)) => package_key(name),
        Some(other) => return Err(format!("expected string or bytes-like object, got '{}'", type_name(other))),
    };
    let mut out = Map::new();
    for (place, requirements) in requirement_lists(project, pyproject)? {
        let Value::Array(requirements) = requirements else { return Err(format!("{place} is not a list")) };
        for requirement in requirements.iter().filter(|r| !r.is_table()) {
            let (key, url) = requirement_of(requirement)?;
            if key != own {
                let source = match sources.get(&key).cloned().flatten() {
                    Some(source) => Some(source),
                    None => url.map(|url| PackageSource::new(SourceKind::Url, url)).or_else(|| default.clone()),
                };
                out.insert(key, source);
            }
        }
    }
    Ok(out)
}

/// (where, its list) of each list of requirements in a pyproject.toml, parsed, whose `[project]` table is project.
fn requirement_lists<'a>(project: &'a Table, pyproject: &'a Table) -> Result<Vec<(String, &'a Value)>, String> {
    const EMPTY: &Value = &Value::Array(Vec::new());
    let mut out = vec![("[project] dependencies".to_string(), project.get("dependencies").unwrap_or(EMPTY))];
    out.extend(toml_table(project, "optional-dependencies")?.iter().map(|(k, v)| (format!("[project.optional-dependencies] {k}"), v)));
    out.extend(toml_table(pyproject, "dependency-groups")?.iter().map(|(k, v)| (format!("[dependency-groups] {k}"), v)));
    Ok(out)
}

/// `[tool.uv]`'s `[[index]]` tables.
fn uv_indexes(uv: &Table) -> Result<Vec<&Table>, String> {
    match uv.get("index") {
        None => Ok(Vec::new()),
        Some(Value::Array(items)) => items.iter().map(|i| i.as_table().ok_or(())).collect::<Result<_, _>>()
            .map_err(|()| "[[tool.uv.index]] is not a list of tables".to_string()),
        Some(_) => Err("[[tool.uv.index]] is not a list of tables".to_string()),
    }
}

/// The PackageSource of each package `[tool.uv.sources]` names, by package_key; None for one from PyPI's index. Of a
/// package with several sources, each for some platforms, the first.
fn uv_sources(uv: &Table) -> Result<Sources, String> {
    // Each index's URL by its name, a later index of a name taking its place, as a dict of them does.
    let indexes: Vec<(Option<&Value>, Option<&Value>)> = uv_indexes(uv)?.into_iter().map(|i| (i.get("name"), i.get("url"))).collect();
    let mut out = Map::new();
    for (name, entry) in toml_table(uv, "sources")? {
        let first = match entry {
            Value::Array(items) if !items.is_empty() => &items[0],
            other => other,
        };
        let Value::Table(first) = first else { return Err(format!("[tool.uv.sources] {name} is not a table")) };
        out.insert(package_key(name), uv_source(first, &indexes)?);
    }
    Ok(out)
}

/// The PackageSource of a `[tool.uv.sources]` entry, given the indexes' URLs by name.
fn uv_source(entry: &Table, indexes: &[(Option<&Value>, Option<&Value>)]) -> Result<Option<PackageSource>, String> {
    if entry.get("workspace") == Some(&Value::Boolean(true)) {
        return Ok(Some(PackageSource::new(SourceKind::Workspace, "")));
    }
    if let Some(index) = entry.get("index") {
        let url = indexes.iter().rev().find(|(name, _)| *name == Some(index)).and_then(|(_, url)| *url).filter(|url| truthy(url));
        return Ok(index_source(Some(url.unwrap_or(index))));
    }
    for (key, kind) in [("path", SourceKind::Path), ("git", SourceKind::Git), ("url", SourceKind::Url)] {
        if let Some(place) = entry.get(key) {
            return Ok(Some(PackageSource::new(kind, py_str(place))));
        }
    }
    unknown_source(entry).map(Some)
}

/// The PackageSource of a source table of a shape this reader does not know, written as an inline table; why not, for
/// one json.dumps cannot write.
fn unknown_source(table: &Table) -> Result<PackageSource, String> {
    let entries = table.iter().map(|(k, v)| Ok(format!("{k} = {}", json_dumps(v)?))).collect::<Result<Vec<_>, String>>()?;
    Ok(PackageSource::new(SourceKind::Unknown, format!("{{ {} }}", entries.join(", "))))
}

/// The PackageSource of a package from the index at url, None standing for Python's None; None for PyPI's.
fn index_source(url: Option<&Value>) -> Option<PackageSource> {
    let url = url.map_or_else(|| "None".to_string(), py_str);
    (url.trim_end_matches('/') != PYPI_INDEX_URL).then(|| PackageSource::new(SourceKind::Index, url))
}

/// python_dependencies's locked packages of a uv.lock, parsed; why not, for one of another shape.
fn uv_lock_packages(lock: &Table) -> Result<Sources, String> {
    let packages: Vec<(String, &Table)> = match lock.get("package") {
        None => Vec::new(),
        Some(Value::Array(items)) => items.iter().map(locked_package).collect::<Option<_>>().ok_or("[[package]] is not a list of tables with a name")?,
        Some(_) => return Err("[[package]] is not a list of tables with a name".to_string()),
    };
    let mut out = Map::new();
    // editable and virtual: the project itself and its workspace members.
    for (name, source) in packages.into_iter().filter(|(_, s)| !s.contains_key("editable") && !s.contains_key("virtual")) {
        out.insert(package_key(&name), lock_source(source)?);
    }
    Ok(out)
}

/// The name and the source table, an empty one when it has none, of a uv.lock `[[package]]` entry; None for one of
/// another shape.
fn locked_package(package: &Value) -> Option<(String, &Table)> {
    let package = package.as_table()?;
    let name = package.get("name")?.as_str()?.to_string();
    let source = match package.get("source") {
        None => empty_table(),
        Some(source) => source.as_table()?,
    };
    Some((name, source))
}

/// The PackageSource of a uv.lock package's source table.
fn lock_source(source: &Table) -> Result<Option<PackageSource>, String> {
    if let Some(registry) = source.get("registry") {
        return Ok(index_source(Some(registry)));
    }
    for (key, kind) in [("path", SourceKind::Path), ("directory", SourceKind::Path), ("git", SourceKind::Git), ("url", SourceKind::Url)] {
        if let Some(place) = source.get(key) {
            return Ok(Some(PackageSource::new(kind, py_str(place))));
        }
    }
    unknown_source(source).map(Some)
}

/// The table key of the TOML table parent, an empty one when it has none; why not, when key is not a table.
fn toml_table<'a>(parent: &'a Table, key: &str) -> Result<&'a Table, String> {
    match parent.get(key) {
        None => Ok(empty_table()),
        Some(Value::Table(table)) => Ok(table),
        Some(_) => Err(format!("{key} is not a table")),
    }
}

fn empty_table() -> &'static Table {
    static EMPTY: std::sync::OnceLock<Table> = std::sync::OnceLock::new();
    EMPTY.get_or_init(Table::new)
}

/// Whether Python takes the value as true.
fn truthy(value: &Value) -> bool {
    match value {
        Value::String(s) => !s.is_empty(),
        Value::Integer(n) => *n != 0,
        Value::Float(x) => *x != 0.0,
        Value::Boolean(b) => *b,
        Value::Datetime(_) => true,
        Value::Array(items) => !items.is_empty(),
        Value::Table(table) => !table.is_empty(),
    }
}

/// (package_key, direct URL or None) of a PEP 508 requirement, `name[extras] @ url ; marker` or a version after the
/// name, as the regular expression `\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\]\s*)?(?:@\s*(\S+))?`
/// matches its start; why not, for anything else.
fn requirement_of(requirement: &Value) -> Result<(String, Option<String>), String> {
    let refused = || format!("{} is not a requirement", repr(requirement));
    let Value::String(text) = requirement else { return Err(refused()) };
    let rest = text.trim_start_matches(is_space);
    let name_class = |c: char| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-');
    if !rest.starts_with(|c: char| c.is_ascii_alphanumeric()) {
        return Err(refused());
    }
    let run = &rest[..rest.find(|c| !name_class(c)).unwrap_or(rest.len())];
    let name = run.trim_end_matches(['.', '_', '-']);
    let mut rest = rest[name.len()..].trim_start_matches(is_space);
    if let Some(extras) = rest.strip_prefix('[') {
        if let Some(close) = extras.find(']') {
            rest = extras[close + 1..].trim_start_matches(is_space);
        }
    }
    let url = rest.strip_prefix('@').map(|after| after.trim_start_matches(is_space)).and_then(|after| {
        let url = &after[..after.find(is_space).unwrap_or(after.len())];
        (!url.is_empty()).then(|| url.to_string())
    });
    Ok((package_key(name), url))
}

/// A package a project depends on after the change and not before it.
#[derive(Clone, Debug)]
struct NewPackage {
    key: String,
    /// The manifest that names it: its ecosystem's manifest for a direct one, else its lock file.
    path: &'static str,
    /// The first line the change added to it that names the package; None when none does.
    line: Option<u64>,
    /// One of Dependencies::direct; else a package the lock file alone adds.
    direct: bool,
    /// None for PyPI.
    source: Option<PackageSource>,
}

/// The packages that the change adds to a DEPENDENCY_ECOSYSTEMS project, each from PyPI asked of pypi.
///
/// Fails a direct dependency the spec does not name (spec_names), unless it is a member of the project's workspace;
/// a package PyPI does not know; those PyPI could not be asked about; and a manifest in the change that cannot be
/// read. Reports a direct dependency the spec names, or a workspace member, with where it comes from: what PyPI knows
/// of it, flagged when its first release is under YOUNG_PACKAGE_DAYS days old, or its source, other-index for an
/// index other than PyPI's; the packages a lock file alone adds, those the direct ones pull in, in one finding per
/// lock file; a dependency of the base's that the change takes from another source (source-changed); and a manifest
/// that cannot be read at the base, which leaves its ecosystem unchecked. PyPI is asked only while change.in_time(),
/// and no more after the first request it gives no usable answer to. Errors: change.file_at's.
pub fn new_dependencies(change: &RunChange, pypi: &PyPI) -> Result<CheckFindings, OrchestratorError> {
    let (packages, mut found) = new_packages(change)?;
    let (known, failure) = ask_pypi(&packages, pypi, change.in_time);
    let named = spec_names(&change.spec);
    let mut pulled_in: Map<Vec<String>> = Map::new();
    for p in &packages {
        if !p.direct && (p.source.is_some() || known.get(&p.key).is_some_and(Option::is_some)) {
            let described = format!("`{}` ({})", p.key, whence(p, &known, change.now, ""));
            match pulled_in.get_mut(p.path) {
                Some(list) => list.push(described),
                None => pulled_in.insert(p.path, vec![described]),
            }
        } else if let Some((fails, finding)) = package_finding(p, &named, &known, change.now) {
            if fails { &mut found.issues } else { &mut found.to_weigh }.push(finding);
        }
    }
    for (path, described) in pulled_in.iter() {
        let message = format!("{} new packages that the change's dependencies pull in: {}.", described.len(), described.join(", "));
        found.to_weigh.push(Finding::warning(Some(path), None, "pulled-in", message));
    }
    let mut unasked: Vec<&str> = Vec::new();
    for p in packages.iter().filter(|p| p.source.is_none() && !known.contains_key(&p.key)) {
        if !unasked.contains(&p.key.as_str()) {
            unasked.push(&p.key);
        }
    }
    if !unasked.is_empty() {
        let names: Vec<String> = unasked.iter().map(|k| format!("`{k}`")).collect();
        let message = format!(
            "PyPI could not be asked about {}: {}; whether they exist is not known.",
            names.join(", "),
            failure.unwrap_or_default()
        );
        found.issues.push(Finding::error(None, None, "unchecked", message));
    }
    Ok(found)
}

/// Whether new_dependencies's finding on package p fails the gate, and the finding; None for a package of a lock
/// file that comes from elsewhere than PyPI or that PyPI knows, which is described with the others the lock file
/// adds, or one that PyPI was not asked about, which is unchecked.
fn package_finding(p: &NewPackage, named: &BTreeSet<String>, known: &Map<Option<PyPIProject>>, now: f64) -> Option<(bool, Finding)> {
    let member = p.source.as_ref().is_some_and(|s| s.kind == SourceKind::Workspace);
    let in_spec = named.contains(&p.key);
    if p.direct && !in_spec && !member {
        let message = format!("`{}` is a new dependency that the spec does not name{}.", p.key, whence(p, known, now, "; "));
        return Some((true, Finding::error(Some(p.path), p.line, "not-in-spec", message)));
    }
    if matches!(known.get(&p.key), Some(None)) {
        let message = format!(
            "PyPI does not know `{}`: the name may be mistyped, or name a package that does not exist yet, which anyone \
             could register (slopsquatting).",
            p.key
        );
        return Some((true, Finding::error(Some(p.path), p.line, "not-on-pypi", message)));
    }
    if p.direct && (p.source.is_some() || known.contains_key(&p.key)) {
        let rule = match p.source.as_ref().map(|s| s.kind) {
            Some(SourceKind::Index) => "other-index",
            Some(SourceKind::Unknown) => "unknown-source",
            _ if known.get(&p.key).cloned().flatten().is_some_and(|project| young(&project, now)) => "young",
            _ => "added",
        };
        let spec = if in_spec { ", which the spec names" } else { "" };
        let message = format!("`{}`{spec}: {}.", p.key, whence(p, known, now, ""));
        return Some((false, Finding::warning(Some(p.path), p.line, rule, message)));
    }
    None
}

/// The package_keys of the words in spec that could be a package's name, so a spec names a package when it holds
/// the name as a word, in any case and with -, _ or . in any of their places; a longer name holding it does not.
pub fn spec_names(spec: &str) -> BTreeSet<String> {
    let word_char = |c: char| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-');
    let mut names = BTreeSet::new();
    let mut rest = spec;
    while let Some(start) = rest.find(|c: char| c.is_ascii_alphanumeric()) {
        let word = &rest[start..];
        let end = word.find(|c| !word_char(c)).unwrap_or(word.len());
        names.insert(package_key(word[..end].trim_end_matches(['.', '_', '-'])));
        rest = &word[end..];
    }
    names
}

/// The packages the change adds to each DEPENDENCY_ECOSYSTEMS project whose manifest or lock file it touches, direct
/// ones first; and in found, an unreadable finding for each manifest that cannot be read, which leaves its project
/// unchecked: failing when the change made it so, and to weigh when it cannot be read at the base either, with a
/// source-changed finding to weigh for each package that moved.
fn new_packages(change: &RunChange) -> Result<(Vec<NewPackage>, CheckFindings), OrchestratorError> {
    let touched = |path: &str| change.files.iter().any(|f| f.path == path || f.base_path == path);
    let mut packages = Vec::new();
    let mut found = CheckFindings::default();
    // Read once, for the ecosystem and for the lines that name its packages.
    let read_files: RefCell<Map<Option<Vec<u8>>>> = RefCell::new(Map::new());
    let new_text = |path: &str| -> Result<Option<Vec<u8>>, OrchestratorError> {
        if let Some(text) = read_files.borrow().get(path) {
            return Ok(text.clone());
        }
        let text = (change.file_at)(&change.commit, path)?;
        read_files.borrow_mut().insert(path, text.clone());
        Ok(text)
    };
    for ecosystem in &DEPENDENCY_ECOSYSTEMS {
        if !touched(ecosystem.manifest) && !touched(ecosystem.lock_file) {
            continue;
        }
        // Both sides at the top level, where the project's manifests are: a file the change moves there was not the
        // project's at the base.
        let after = (ecosystem.read)(&new_text)?;
        let before = (ecosystem.read)(&|path: &str| (change.file_at)(&change.base, path))?;
        // Each manifest by its own two sides: the Builder broke only what the base could read.
        for (path, why) in after.unreadable.iter().filter(|(path, _)| !before.unreadable.contains_key(path)) {
            found.issues.push(unreadable(path, why, "in the change", true));
        }
        for (path, why) in before.unreadable.iter() {
            found.to_weigh.push(unreadable(path, why, "at the base", false));
        }
        if after.unreadable.is_empty() && before.unreadable.is_empty() {
            let text = new_text(ecosystem.manifest)?.map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default();
            let empty = BTreeSet::new();
            let added = change.changed.get(ecosystem.manifest).unwrap_or(&empty);
            let line_naming = |key: &str| line_naming(&text, added, key);
            let (new, moved) = compare_dependencies(ecosystem, &before, &after, &line_naming);
            packages.extend(new);
            found.to_weigh.extend(moved);
        }
    }
    packages.sort_by_key(|p| !p.direct);
    Ok((packages, found))
}

/// The packages the project depends on after and not before, a package of its direct dependencies counted as one of
/// them only; and a source-changed finding to weigh for each that it depended on before from another source.
/// line_naming gives the added line of the manifest that names a package, by its key.
fn compare_dependencies(
    ecosystem: &DependencyEcosystem,
    before: &Dependencies,
    after: &Dependencies,
    line_naming: &dyn Fn(&str) -> Option<u64>,
) -> (Vec<NewPackage>, Vec<Finding>) {
    let lock_only: Sources =
        after.locked.iter().filter(|(key, _)| !after.direct.contains_key(key)).map(|(k, v)| (k, v.clone())).collect();
    let mut new: Vec<NewPackage> = after
        .direct
        .iter()
        .filter(|(key, _)| !before.direct.contains_key(key))
        .map(|(key, source)| NewPackage { key: key.to_string(), path: ecosystem.manifest, line: line_naming(key), direct: true, source: source.clone() })
        .collect();
    new.extend(lock_only.iter().filter(|(key, _)| !before.locked.contains_key(key)).map(|(key, source)| NewPackage {
        key: key.to_string(),
        path: ecosystem.lock_file,
        line: None,
        direct: false,
        source: source.clone(),
    }));
    let mut moved = Vec::new();
    let sides =
        [(ecosystem.manifest, &before.direct, &after.direct, true), (ecosystem.lock_file, &before.locked, &lock_only, false)];
    for (path, was, now, has_lines) in sides {
        for (key, source) in now.iter() {
            if let Some(old) = was.get(key).filter(|old| *old != source) {
                let message = format!("`{key}`: at the base, {}; in the change, {}.", source_text(old), source_text(source));
                moved.push(Finding::warning(Some(path), if has_lines { line_naming(key) } else { None }, "source-changed", message));
            }
        }
    }
    (new, moved)
}

fn source_text(source: &Option<PackageSource>) -> String {
    source.as_ref().map_or_else(|| "from PyPI".to_string(), PackageSource::describe)
}

fn unreadable(path: &str, why: &str, place: &str, fails: bool) -> Finding {
    let message = format!("`{path}` cannot be read {place}: {why}; the change's new dependencies were not checked.");
    if fails { Finding::error(Some(path), None, "unreadable", message) } else { Finding::warning(Some(path), None, "unreadable", message) }
}

/// The first of the added lines of text, by number from 1, that names the package key, as spec_names reads a name:
/// any case, a run of ., - or _ for each - of key, and no letter, digit, ., - or _ just before it; a ., - or _ that
/// goes on into more of a name makes it another name, as zope.interface is not zope.
fn line_naming(text: &str, added: &BTreeSet<u64>, key: &str) -> Option<u64> {
    let lines: Vec<&str> = text.split('\n').collect();
    let parts: Vec<&str> = key.split('-').collect();
    added.iter().copied().find(|&n| n >= 1 && n as usize <= lines.len() && names(lines[n as usize - 1], &parts))
}

/// Whether line holds the name of parts, as line_naming says.
fn names(line: &str, parts: &[&str]) -> bool {
    let name_char = |c: char| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-');
    let separator = |c: char| matches!(c, '.' | '_' | '-');
    let mut previous: Option<char> = None;
    for (start, c) in line.char_indices() {
        if !previous.is_some_and(name_char) {
            if let Some(end) = match_parts(&line[start..], parts) {
                let after = line[start + end..].trim_start_matches(separator);
                if !after.starts_with(|c: char| c.is_ascii_alphanumeric()) {
                    return true;
                }
            }
        }
        previous = Some(c);
    }
    false
}

/// How many bytes of text the parts take, each after the first after a run of separators, compared without regard to
/// ASCII case; None when text does not start with them.
fn match_parts(text: &str, parts: &[&str]) -> Option<usize> {
    let mut at = 0;
    for (i, part) in parts.iter().enumerate() {
        if i > 0 {
            let run = text[at..].len() - text[at..].trim_start_matches(['.', '_', '-']).len();
            if run == 0 {
                return None;
            }
            at += run;
        }
        let candidate = text.get(at..at + part.len())?;
        if !candidate.eq_ignore_ascii_case(part) {
            return None;
        }
        at += part.len();
    }
    Some(at)
}

/// What pypi knows of each package from PyPI, by key, None for one it does not know; and why the rest were not asked:
/// in_time() said no before a request, or pypi gave no usable answer to one, which leaves that one and every later
/// one out.
fn ask_pypi(packages: &[NewPackage], pypi: &PyPI, in_time: &dyn Fn() -> bool) -> (Map<Option<PyPIProject>>, Option<String>) {
    let mut known = Map::new();
    for p in packages.iter().filter(|p| p.source.is_none()) {
        if known.contains_key(&p.key) {
            continue;
        }
        if !in_time() {
            return (known, Some("the quality round had too little time left to ask".to_string()));
        }
        match pypi.project(&p.key) {
            Ok(project) => known.insert(p.key.as_str(), project),
            Err(e) => return (known, Some(e.to_string())),
        }
    }
    (known, None)
}

/// Where package p comes from, after lead: its source, or what PyPI knows of it; "" when PyPI was not asked.
fn whence(p: &NewPackage, known: &Map<Option<PyPIProject>>, now: f64, lead: &str) -> String {
    if let Some(source) = &p.source {
        return format!("{lead}{}", source.describe());
    }
    match known.get(&p.key) {
        None => String::new(),
        Some(None) => format!("{lead}PyPI does not know it either"),
        Some(Some(project)) => {
            let since = project.first_release.as_ref().map_or_else(|| "with no release files".to_string(), |u| format!("since {}", u.date));
            let latest = project.latest_release.as_ref().map_or_else(String::new, |u| format!(" of {}", u.date));
            let young = if young(project, now) {
                format!("; it is younger than {YOUNG_PACKAGE_DAYS} days, so few have used it yet")
            } else {
                String::new()
            };
            let license = project.license.as_deref().unwrap_or("not stated");
            format!("{lead}on PyPI {since}, latest {}{latest}, license {license}{young}", project.version)
        }
    }
}

/// Whether the project's first release is under YOUNG_PACKAGE_DAYS days before now; one with none is.
fn young(project: &PyPIProject, now: f64) -> bool {
    project.first_release.as_ref().is_none_or(|first| now - first.at < (YOUNG_PACKAGE_DAYS * 86_400) as f64)
}

#[cfg(test)]
mod tests;
