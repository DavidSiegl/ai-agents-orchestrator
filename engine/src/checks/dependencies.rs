//! The new-dependencies check: the packages a change adds to the project, judged against the spec and the package
//! index of their ecosystem, and the readers of the manifests that say what a project depends on
//! (docs/quality-gate.md, "New dependencies"). The projects are Python projects with uv, whose index is PyPI, and
//! Cargo workspaces, whose index is crates.io.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use ::toml::{Table, Value};

use super::{CheckFindings, Finding, RunChange};
use crate::error::OrchestratorError;
use crate::py::json::Map;
use crate::py::text::{is_space, utf8_error};
use crate::py::toml::{hashable, json_dumps, loads, python_type, repr, same_key, str as py_str, truthy};
use crate::package_index::{IndexedPackage, PackageIndex, PackageIndexes};

/// A new dependency whose first release is fewer days old than this is flagged: few have used it yet, and a name
/// registered for the occasion is the slopsquatting move.
pub const YOUNG_PACKAGE_DAYS: u64 = 14;
/// PyPI's index as uv.lock and `[[tool.uv.index]]` name it; a package from any other index is not PyPI's to vouch
/// for.
pub const PYPI_INDEX_URL: &str = "https://pypi.org/simple";

/// Reads a file of one side of the change by its path from the top level: its bytes, None where it has none. Errors:
/// RunChange::file_at's.
pub type ReadFile<'a> = dyn Fn(&str) -> Result<Option<Vec<u8>>, OrchestratorError> + 'a;

/// The kinds of place a package comes from other than its ecosystem's index, as uv's sources name them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SourceKind {
    /// A member of the project's workspace.
    Workspace,
    Path,
    Git,
    Url,
    /// An index other than the ecosystem's own, or a Cargo registry other than crates.io.
    Index,
    /// A uv source of a shape this reader does not know.
    Unknown,
}

/// Where a package comes from when it is not its ecosystem's index.
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

    /// Where it comes from, as a finding says it, index being the name of the index it does not come from.
    pub fn describe(&self, index: &str) -> String {
        let place = &self.place;
        match self.kind {
            SourceKind::Workspace => format!("a member of the project's workspace, not from {index}"),
            SourceKind::Index => format!("from the index {place}, not checked against {index}"),
            SourceKind::Unknown => {
                format!(
                    "from the uv source {place}, which this check does not know, so where it comes from is not checked"
                )
            }
            SourceKind::Path => format!("from the path {place}, not from {index}"),
            SourceKind::Git => format!("from the repository {place}, not from {index}"),
            SourceKind::Url => format!("from {place}, not from {index}"),
        }
    }
}

/// Packages by package_key, each with where it comes from, None for the ecosystem's index: what one manifest names.
pub type Sources = Map<Option<PackageSource>>;

/// A package a project depends on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dependency {
    /// Where it comes from; None for the ecosystem's index.
    pub source: Option<PackageSource>,
    /// The manifest that names it, by its path from the top level; for a locked package, the lock file. Of a package
    /// several manifests name, the first the ecosystem reads.
    pub manifest: String,
}

/// What a project's manifests say it depends on, each package by package_key.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Dependencies {
    /// The project's own choice, which the spec must name.
    pub direct: Map<Dependency>,
    /// Every package its lock file pins, the project and its workspace aside.
    pub locked: Map<Dependency>,
    /// Why, by path, for each manifest that cannot be read; direct and locked then come from the others only.
    pub unreadable: Map<String>,
}

/// A kind of project's dependency manifests, from the repository's top level, how to read them, and the index its
/// packages come from. Manifests anywhere else, such as test fixtures, are not the project's.
pub struct DependencyEcosystem {
    /// Whether a change to the file at path, from the top level, may change what the project depends on: whether it
    /// is, or may be, one of the manifests read reads.
    pub reads: fn(&str) -> bool,
    /// The project's Dependencies, given each file by its path; each manifest it cannot read is in their
    /// unreadable, and the others are still read. Errors: the reader's.
    pub read: fn(&ReadFile) -> Result<Dependencies, OrchestratorError>,
    /// The index of indexes that its packages come from, unless their source says otherwise.
    pub index: fn(&PackageIndexes) -> &dyn PackageIndex,
}

/// The ecosystems whose new packages the new-dependencies check judges: Python's, with uv, whose index is PyPI, and
/// Rust's, with Cargo, whose index is crates.io.
pub const DEPENDENCY_ECOSYSTEMS: [DependencyEcosystem; 2] = [
    DependencyEcosystem {
        reads: |path| path == "pyproject.toml" || path == "uv.lock",
        read: python_dependencies,
        index: |indexes| &indexes.pypi,
    },
    DependencyEcosystem {
        // Any Cargo.toml, since a workspace's members can be anywhere below the top level.
        reads: |path| path == "Cargo.lock" || path == "Cargo.toml" || path.ends_with("/Cargo.toml"),
        read: cargo_dependencies,
        index: |indexes| &indexes.crates_io,
    },
];

/// A Python package's name as PyPI compares names (PEP 503): lower case, each run of -, _ and . one -. A crate's
/// name, which holds no ., too: crates.io takes - and _ for each other, case aside.
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
        Ok(locked) => add_named(&mut found.locked, locked, "uv.lock"),
        Err(why) => found.unreadable.insert("uv.lock", why),
    }
    match toml_manifest(read, "pyproject.toml")?.and_then(|pyproject| pyproject_dependencies(&pyproject)) {
        Ok(direct) => {
            let direct = direct
                .into_iter()
                .map(|(key, source)| {
                    let source = source.or_else(|| found.locked.get(&key).and_then(|d| d.source.clone()));
                    (key, source)
                })
                .collect();
            add_named(&mut found.direct, direct, "pyproject.toml");
        }
        Err(why) => found.unreadable.insert("pyproject.toml", why),
    }
    Ok(found)
}

/// Adds to packages each of sources that it does not hold yet, as named by the manifest at path.
fn add_named(packages: &mut Map<Dependency>, sources: Sources, path: &str) {
    for (key, source) in sources {
        if !packages.contains_key(&key) {
            packages.insert(key, Dependency { source, manifest: path.to_string() });
        }
    }
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
    let default = uv_indexes(uv)?
        .into_iter()
        .find(|i| i.get("default") == Some(&Value::Boolean(true)))
        .and_then(|i| index_source(i.get("url")));
    let own = match project.get("name").filter(|name| truthy(name)) {
        None => String::new(),
        Some(Value::String(name)) => package_key(name),
        Some(other) => return Err(format!("expected string or bytes-like object, got '{}'", python_type(other))),
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
    out.extend(
        toml_table(project, "optional-dependencies")?
            .iter()
            .map(|(k, v)| (format!("[project.optional-dependencies] {k}"), v)),
    );
    out.extend(
        toml_table(pyproject, "dependency-groups")?.iter().map(|(k, v)| (format!("[dependency-groups] {k}"), v)),
    );
    Ok(out)
}

/// `[tool.uv]`'s `[[index]]` tables.
fn uv_indexes(uv: &Table) -> Result<Vec<&Table>, String> {
    match uv.get("index") {
        None => Ok(Vec::new()),
        Some(Value::Array(items)) => items
            .iter()
            .map(|i| i.as_table().ok_or(()))
            .collect::<Result<_, _>>()
            .map_err(|()| "[[tool.uv.index]] is not a list of tables".to_string()),
        Some(_) => Err("[[tool.uv.index]] is not a list of tables".to_string()),
    }
}

/// The PackageSource of each package `[tool.uv.sources]` names, by package_key; None for one from PyPI's index. Of a
/// package with several sources, each for some platforms, the first.
fn uv_sources(uv: &Table) -> Result<Sources, String> {
    // Each index's URL by its name, a later index of a name taking its place, as a dict of them does.
    let indexes: Vec<(Option<&Value>, Option<&Value>)> =
        uv_indexes(uv)?.into_iter().map(|i| (i.get("name"), i.get("url"))).collect();
    for name in indexes.iter().filter_map(|(name, _)| *name) {
        hashable(name)?;
    }
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
        hashable(index)?;
        let url = indexes
            .iter()
            .rev()
            .find(|(name, _)| name.is_some_and(|name| same_key(name, index)))
            .and_then(|(_, url)| *url)
            .filter(|url| truthy(url));
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
    let entries =
        table.iter().map(|(k, v)| Ok(format!("{k} = {}", json_dumps(v)?))).collect::<Result<Vec<_>, String>>()?;
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
        Some(Value::Array(items)) => items
            .iter()
            .map(locked_package)
            .collect::<Option<_>>()
            .ok_or("[[package]] is not a list of tables with a name")?,
        Some(_) => return Err("[[package]] is not a list of tables with a name".to_string()),
    };
    let mut out = Map::new();
    // editable and virtual: the project itself and its workspace members.
    for (name, source) in
        packages.into_iter().filter(|(_, s)| !s.contains_key("editable") && !s.contains_key("virtual"))
    {
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
    for (key, kind) in [
        ("path", SourceKind::Path),
        ("directory", SourceKind::Path),
        ("git", SourceKind::Git),
        ("url", SourceKind::Url),
    ] {
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

/// crates.io's index as Cargo.lock names it: the git index, and the sparse one Cargo has used by default since 1.70.
const CRATES_IO_INDEXES: [&str; 2] = ["https://github.com/rust-lang/crates.io-index", "https://index.crates.io/"];

/// The tables of a Cargo manifest, at its top level and under each `[target.<cfg>]`, that list dependencies, the
/// underscore forms being what Cargo before the 2024 edition also reads.
const CARGO_DEPENDENCY_TABLES: [&str; 5] =
    ["dependencies", "dev-dependencies", "build-dependencies", "dev_dependencies", "build_dependencies"];

/// DependencyEcosystem::read of a Rust project with Cargo.
///
/// Its direct dependencies are those of the top-level Cargo.toml and of each manifest its `[workspace] members` names
/// (one by its path, not a glob, which this reader does not expand), in each CARGO_DEPENDENCY_TABLES table and its
/// `[target.<cfg>]` forms, and its `[workspace.dependencies]`, each by its crate's name (its `package` when it renames
/// the crate). A dependency's source is its entry's: `path`, a member of the workspace; `git`; `registry` or
/// `registry-index`, an index other than crates.io; else crates.io. One that takes `workspace = true` has the
/// `[workspace.dependencies]` entry's. The locked packages are Cargo.lock's that come from a registry or a repository,
/// the workspace's own crates and the others it has from a path left out.
pub fn cargo_dependencies(read: &ReadFile) -> Result<Dependencies, OrchestratorError> {
    let mut found = Dependencies::default();
    match toml_manifest(read, "Cargo.lock")?.and_then(|lock| cargo_lock_packages(&lock)) {
        Ok(locked) => add_named(&mut found.locked, locked, "Cargo.lock"),
        Err(why) => found.unreadable.insert("Cargo.lock", why),
    }
    let root = match toml_manifest(read, "Cargo.toml")? {
        Ok(root) => root,
        Err(why) => {
            found.unreadable.insert("Cargo.toml", why);
            return Ok(found);
        }
    };
    let workspace = match workspace_of(&root) {
        Ok(workspace) => workspace,
        Err(why) => {
            found.unreadable.insert("Cargo.toml", why);
            return Ok(found);
        }
    };
    match cargo_manifest_dependencies(&root, &workspace.shared) {
        Ok(direct) => {
            add_named(&mut found.direct, direct, "Cargo.toml");
            add_named(&mut found.direct, workspace.shared.values().cloned().collect(), "Cargo.toml");
        }
        Err(why) => found.unreadable.insert("Cargo.toml", why),
    }
    for member in &workspace.members {
        let path = format!("{member}/Cargo.toml");
        let manifest = match read(&path)? {
            None => Err("the workspace names it a member, but it does not exist".to_string()),
            Some(_) => toml_manifest(read, &path)?,
        };
        match manifest.and_then(|manifest| cargo_manifest_dependencies(&manifest, &workspace.shared)) {
            Ok(direct) => add_named(&mut found.direct, direct, &path),
            Err(why) => found.unreadable.insert(path, why),
        }
    }
    Ok(found)
}

/// What a top-level Cargo.toml's `[workspace]` says.
struct CargoWorkspace {
    /// Each member's directory from the top level, without a trailing /; the top level itself left out.
    members: Vec<String>,
    /// `[workspace.dependencies]`: by each entry's name, its crate's package_key and source.
    shared: Map<(String, Option<PackageSource>)>,
}

/// The CargoWorkspace of the parsed top-level Cargo.toml root, which has none when root has no `[workspace]`; why
/// not, for one of another shape.
fn workspace_of(root: &Table) -> Result<CargoWorkspace, String> {
    let workspace = toml_table(root, "workspace")?;
    let mut shared = Map::new();
    for (name, entry) in toml_table(workspace, "dependencies")? {
        shared.insert(name, cargo_entry(name, entry, "[workspace.dependencies]")?);
    }
    let excluded = string_list(workspace, "exclude", "[workspace] exclude")?;
    let mut members = Vec::new();
    for member in string_list(workspace, "members", "[workspace] members")? {
        if member.contains(['*', '?', '[']) {
            return Err(format!("its workspace member `{member}` is a glob, which this check does not expand"));
        }
        let member = member.trim_end_matches('/').trim_start_matches("./");
        if !member.is_empty() && member != "." && !excluded.iter().any(|e| e.trim_end_matches('/') == member) {
            members.push(member.to_string());
        }
    }
    Ok(CargoWorkspace { members, shared })
}

/// The strings of the list table[key], none when it has no such key; why not, naming it as place, when it is not a
/// list of strings.
fn string_list<'a>(table: &'a Table, key: &str, place: &str) -> Result<Vec<&'a str>, String> {
    match table.get(key) {
        None => Ok(Vec::new()),
        Some(Value::Array(items)) => {
            items.iter().map(|i| i.as_str().ok_or(format!("{place} is not a list of strings"))).collect()
        }
        Some(_) => Err(format!("{place} is not a list of strings")),
    }
}

/// The dependencies of a parsed Cargo manifest, as cargo_dependencies reads them, given its workspace's shared
/// `[workspace.dependencies]`; why not, for one of another shape.
fn cargo_manifest_dependencies(
    manifest: &Table,
    shared: &Map<(String, Option<PackageSource>)>,
) -> Result<Sources, String> {
    let mut tables = Vec::new();
    for table in CARGO_DEPENDENCY_TABLES {
        tables.push((format!("[{table}]"), toml_table(manifest, table)?));
    }
    for (cfg, target) in toml_table(manifest, "target")? {
        let Value::Table(target) = target else { return Err(format!("[target.{cfg}] is not a table")) };
        for table in CARGO_DEPENDENCY_TABLES {
            tables.push((format!("[target.{cfg}.{table}]"), toml_table(target, table)?));
        }
    }
    let mut out = Map::new();
    for (place, table) in tables {
        for (name, entry) in table {
            let inherits = entry.as_table().and_then(|t| t.get("workspace")) == Some(&Value::Boolean(true));
            let (key, source) = if inherits {
                shared.get(name).cloned().ok_or_else(|| {
                    format!(
                        "`{name}` in {place} takes `workspace = true`, but [workspace.dependencies] has no `{name}`"
                    )
                })?
            } else {
                cargo_entry(name, entry, &place)?
            };
            if !out.contains_key(&key) {
                out.insert(key, source);
            }
        }
    }
    Ok(out)
}

/// The package_key of the crate that the dependency entry, under name in the table at place, names, and where it
/// comes from, as cargo_dependencies says; why not, for an entry that is neither a version nor a table.
fn cargo_entry(name: &str, entry: &Value, place: &str) -> Result<(String, Option<PackageSource>), String> {
    let table = match entry {
        Value::String(_) => return Ok((package_key(name), None)),
        Value::Table(table) => table,
        _ => return Err(format!("`{name}` in {place} is neither a version nor a table")),
    };
    let crate_name = match table.get("package") {
        None => name,
        Some(Value::String(package)) => package,
        Some(_) => return Err(format!("`{name}` in {place} has a package that is not a string")),
    };
    let source = if table.contains_key("path") {
        Some(PackageSource::new(SourceKind::Workspace, ""))
    } else if let Some(git) = table.get("git") {
        Some(PackageSource::new(SourceKind::Git, py_str(git)))
    } else {
        table
            .get("registry-index")
            .or_else(|| table.get("registry"))
            .map(|registry| PackageSource::new(SourceKind::Index, py_str(registry)))
    };
    Ok((package_key(crate_name), source))
}

/// cargo_dependencies's locked packages of a Cargo.lock, parsed; why not, for one of another shape.
fn cargo_lock_packages(lock: &Table) -> Result<Sources, String> {
    let refused = || "[[package]] is not a list of tables with a name".to_string();
    let packages = match lock.get("package") {
        None => return Ok(Map::new()),
        Some(Value::Array(packages)) => packages,
        Some(_) => return Err(refused()),
    };
    let mut out = Map::new();
    for package in packages {
        let package = package.as_table().ok_or_else(refused)?;
        let name = package.get("name").and_then(Value::as_str).ok_or_else(refused)?;
        // A package without a source is one of the workspace's own crates, or one it has from a path.
        let Some(source) = package.get("source") else { continue };
        let Some(source) = source.as_str() else { return Err(format!("the source of `{name}` is not a string")) };
        out.insert(package_key(name), cargo_lock_source(source));
    }
    Ok(out)
}

/// The PackageSource of a Cargo.lock package's source, `registry+URL`, `sparse+URL` or `git+URL#commit`; None for
/// crates.io. A repository's commit is left out of its place, so that a change of commit is no change of source.
fn cargo_lock_source(source: &str) -> Option<PackageSource> {
    if let Some(repository) = source.strip_prefix("git+") {
        let repository = repository.split_once('#').map_or(repository, |(url, _)| url);
        return Some(PackageSource::new(SourceKind::Git, repository));
    }
    let index = source.strip_prefix("registry+").or_else(|| source.strip_prefix("sparse+")).unwrap_or(source);
    (!CRATES_IO_INDEXES.contains(&index)).then(|| PackageSource::new(SourceKind::Index, index))
}

/// A package a project depends on after the change and not before it.
#[derive(Clone)]
struct NewPackage<'a> {
    key: String,
    /// The manifest that names it, Dependency::manifest.
    path: String,
    /// The first line the change added to it that names the package; None when none does.
    line: Option<u64>,
    /// One of Dependencies::direct; else a package the lock file alone adds.
    direct: bool,
    /// None for index.
    source: Option<PackageSource>,
    /// Its ecosystem's index.
    index: &'a dyn PackageIndex,
}

/// What ask_indexes learned: by (PackageIndex::name, package_key), what the index knows of the package, None for one
/// it does not know. A package missing from it was not asked about.
type Known = BTreeMap<(&'static str, String), Option<IndexedPackage>>;

/// The packages that the change adds to a DEPENDENCY_ECOSYSTEMS project, each from its ecosystem's index asked of
/// indexes.
///
/// Fails a direct dependency the spec does not name (spec_names), unless it is a member of the project's workspace;
/// a package its index does not know (PackageIndex::unknown_rule); those an index could not be asked about, in one
/// unchecked finding per index; and a manifest in the change that cannot be read. Reports a direct dependency the
/// spec names, or a workspace member, with where it comes from: what its index knows of it, flagged when its first
/// release is under YOUNG_PACKAGE_DAYS days old, or its source, other-index for another index; the packages a lock
/// file alone adds, those the direct ones pull in, in one finding per lock file; a dependency of the base's that the
/// change takes from another source (source-changed); and a manifest that cannot be read at the base, which leaves its
/// ecosystem unchecked. An index is asked only while change.in_time(), and no more after the first request it gives
/// no usable answer to. Errors: change.file_at's.
pub fn new_dependencies(change: &RunChange, indexes: &PackageIndexes) -> Result<CheckFindings, OrchestratorError> {
    let (packages, mut found) = new_packages(change, indexes)?;
    let (known, failures) = ask_indexes(&packages, change.in_time);
    let named = spec_names(&change.spec);
    let mut pulled_in: Map<Vec<String>> = Map::new();
    for p in &packages {
        if !p.direct && (p.source.is_some() || known_of(&known, p).is_some_and(Option::is_some)) {
            let described = format!("`{}` ({})", p.key, whence(p, &known, change.now, ""));
            match pulled_in.get_mut(&p.path) {
                Some(list) => list.push(described),
                None => pulled_in.insert(p.path.as_str(), vec![described]),
            }
        } else if let Some((fails, finding)) = package_finding(p, &named, &known, change.now) {
            if fails { &mut found.issues } else { &mut found.to_weigh }.push(finding);
        }
    }
    for (path, described) in pulled_in.iter() {
        let message = format!(
            "{} new packages that the change's dependencies pull in: {}.",
            described.len(),
            described.join(", ")
        );
        found.to_weigh.push(Finding::warning(Some(path), None, "pulled-in", message));
    }
    found.issues.extend(unchecked_findings(&packages, &known, &failures));
    Ok(found)
}

/// What known holds of package p: None when its index was not asked about it, Some(None) when the index does not know
/// it.
fn known_of<'k>(known: &'k Known, p: &NewPackage) -> Option<&'k Option<IndexedPackage>> {
    known.get(&(p.index.name(), p.key.clone()))
}

/// The unchecked findings new_dependencies fails the gate with, one per index that ask_indexes did not ask about each
/// of packages from it: it names, once each and in the order of packages, those it was not asked about, known being
/// what ask_indexes learned, and gives the index's entry of failures, its reason for stopping.
fn unchecked_findings(packages: &[NewPackage], known: &Known, failures: &Map<String>) -> Vec<Finding> {
    let mut unasked: Map<Vec<&str>> = Map::new();
    for p in packages.iter().filter(|p| p.source.is_none() && known_of(known, p).is_none()) {
        match unasked.get_mut(p.index.name()) {
            Some(keys) if keys.contains(&p.key.as_str()) => {}
            Some(keys) => keys.push(&p.key),
            None => unasked.insert(p.index.name(), vec![p.key.as_str()]),
        }
    }
    unasked
        .iter()
        .map(|(index, keys)| {
            let names: Vec<String> = keys.iter().map(|k| format!("`{k}`")).collect();
            let message = format!(
                "{index} could not be asked about {}: {}; whether they exist is not known.",
                names.join(", "),
                failures.get(index).map_or("", String::as_str)
            );
            Finding::error(None, None, "unchecked", message)
        })
        .collect()
}

/// Whether new_dependencies's finding on package p fails the gate, and the finding; None for a package of a lock
/// file that comes from elsewhere than its index or that its index knows, which is described with the others the lock
/// file adds, or one that its index was not asked about, which is unchecked.
fn package_finding(p: &NewPackage, named: &BTreeSet<String>, known: &Known, now: f64) -> Option<(bool, Finding)> {
    let member = p.source.as_ref().is_some_and(|s| s.kind == SourceKind::Workspace);
    let in_spec = named.contains(&p.key);
    let path = Some(p.path.as_str());
    if p.direct && !in_spec && !member {
        let message =
            format!("`{}` is a new dependency that the spec does not name{}.", p.key, whence(p, known, now, "; "));
        return Some((true, Finding::error(path, p.line, "not-in-spec", message)));
    }
    let index = p.index.name();
    if matches!(known_of(known, p), Some(None)) {
        let message = format!(
            "{index} does not know `{}`: the name may be mistyped, or name a package that does not exist yet, which \
             anyone could register (slopsquatting).",
            p.key
        );
        return Some((true, Finding::error(path, p.line, p.index.unknown_rule(), message)));
    }
    if p.direct && (p.source.is_some() || known_of(known, p).is_some()) {
        let rule = match p.source.as_ref().map(|s| s.kind) {
            Some(SourceKind::Index) => "other-index",
            Some(SourceKind::Unknown) => "unknown-source",
            _ if known_of(known, p).cloned().flatten().is_some_and(|package| young(&package, now)) => "young",
            _ => "added",
        };
        let spec = if in_spec { ", which the spec names" } else { "" };
        let message = format!("`{}`{spec}: {}.", p.key, whence(p, known, now, ""));
        return Some((false, Finding::warning(path, p.line, rule, message)));
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

/// The packages the change adds to each DEPENDENCY_ECOSYSTEMS project whose files it touches (DependencyEcosystem::
/// reads), direct ones first, each with its ecosystem's index of indexes; and in found, an unreadable finding for each
/// manifest that cannot be read, which leaves its project unchecked: failing when the change made it so, and to weigh
/// when it cannot be read at the base either, with a source-changed finding to weigh for each package that moved.
fn new_packages<'a>(
    change: &RunChange,
    indexes: &'a PackageIndexes,
) -> Result<(Vec<NewPackage<'a>>, CheckFindings), OrchestratorError> {
    let touched = |ecosystem: &DependencyEcosystem| {
        change.files.iter().any(|f| (ecosystem.reads)(&f.path) || (ecosystem.reads)(&f.base_path))
    };
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
    for ecosystem in DEPENDENCY_ECOSYSTEMS.iter().filter(|ecosystem| touched(ecosystem)) {
        // Both sides from the top level, where the project's manifests are: a file the change moves there was not the
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
            let mut added_line = |manifest: &str, key: &str| -> Result<Option<u64>, OrchestratorError> {
                let Some(added) = change.changed.get(manifest) else { return Ok(None) };
                let text = new_text(manifest)?.map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default();
                Ok(line_naming(&text, added, key))
            };
            let (new, moved) = compare_dependencies((ecosystem.index)(indexes), &before, &after, &mut added_line)?;
            packages.extend(new);
            found.to_weigh.extend(moved);
        }
    }
    packages.sort_by_key(|p| !p.direct);
    Ok((packages, found))
}

/// The first line the change added to the manifest at path that names the package key, by (path, key); None when
/// none does. Errors: RunChange::file_at's.
type AddedLine<'a> = dyn FnMut(&str, &str) -> Result<Option<u64>, OrchestratorError> + 'a;

/// The packages the project depends on after and not before, a package of its direct dependencies counted as one of
/// them only, each from index unless its source says otherwise; and a source-changed finding to weigh for each that
/// it depended on before from another source. Errors: added_line's.
fn compare_dependencies<'a>(
    index: &'a dyn PackageIndex,
    before: &Dependencies,
    after: &Dependencies,
    added_line: &mut AddedLine,
) -> Result<(Vec<NewPackage<'a>>, Vec<Finding>), OrchestratorError> {
    let lock_only: Map<Dependency> =
        after.locked.iter().filter(|(key, _)| !after.direct.contains_key(key)).map(|(k, v)| (k, v.clone())).collect();
    let mut new = Vec::new();
    for (key, dependency) in after.direct.iter().filter(|(key, _)| !before.direct.contains_key(key)) {
        new.push(NewPackage {
            key: key.to_string(),
            path: dependency.manifest.clone(),
            line: added_line(&dependency.manifest, key)?,
            direct: true,
            source: dependency.source.clone(),
            index,
        });
    }
    new.extend(lock_only.iter().filter(|(key, _)| !before.locked.contains_key(key)).map(|(key, dependency)| {
        NewPackage {
            key: key.to_string(),
            path: dependency.manifest.clone(),
            line: None,
            direct: false,
            source: dependency.source.clone(),
            index,
        }
    }));
    let mut moved = Vec::new();
    for (was, now, has_lines) in [(&before.direct, &after.direct, true), (&before.locked, &lock_only, false)] {
        for (key, dependency) in now.iter() {
            if let Some(old) = was.get(key).filter(|old| old.source != dependency.source) {
                let message = format!(
                    "`{key}`: at the base, {}; in the change, {}.",
                    source_text(&old.source, index),
                    source_text(&dependency.source, index)
                );
                let line = if has_lines { added_line(&dependency.manifest, key)? } else { None };
                moved.push(Finding::warning(Some(&dependency.manifest), line, "source-changed", message));
            }
        }
    }
    Ok((new, moved))
}

fn source_text(source: &Option<PackageSource>, index: &dyn PackageIndex) -> String {
    source.as_ref().map_or_else(|| format!("from {}", index.name()), |s| s.describe(index.name()))
}

fn unreadable(path: &str, why: &str, place: &str, fails: bool) -> Finding {
    let message = format!("`{path}` cannot be read {place}: {why}; the change's new dependencies were not checked.");
    if fails {
        Finding::error(Some(path), None, "unreadable", message)
    } else {
        Finding::warning(Some(path), None, "unreadable", message)
    }
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

/// What each package's index knows of it, of those from their index; and why an index was not asked about the rest
/// of its packages, by its name: in_time() said no before a request, which leaves every later one of every index
/// out, or the index gave no usable answer to one, which leaves that one and every later one of the index out.
fn ask_indexes(packages: &[NewPackage], in_time: &dyn Fn() -> bool) -> (Known, Map<String>) {
    let mut known = Known::new();
    let mut failures: Map<String> = Map::new();
    let mut out_of_time = false;
    for p in packages.iter().filter(|p| p.source.is_none()) {
        let index = p.index.name();
        if failures.contains_key(index) || known_of(&known, p).is_some() {
            continue;
        }
        out_of_time = out_of_time || !in_time();
        if out_of_time {
            failures.insert(index, "the quality round had too little time left to ask".to_string());
            continue;
        }
        match p.index.package(&p.key) {
            Ok(package) => {
                known.insert((index, p.key.clone()), package);
            }
            Err(e) => failures.insert(index, e.to_string()),
        }
    }
    (known, failures)
}

/// Where package p comes from, after lead: its source, or what its index knows of it; "" when the index was not
/// asked.
fn whence(p: &NewPackage, known: &Known, now: f64, lead: &str) -> String {
    let index = p.index.name();
    if let Some(source) = &p.source {
        return format!("{lead}{}", source.describe(index));
    }
    match known_of(known, p) {
        None => String::new(),
        Some(None) => format!("{lead}{index} does not know it either"),
        Some(Some(package)) => {
            let since = package
                .first_release
                .as_ref()
                .map_or_else(|| "with no release files".to_string(), |u| format!("since {}", u.date));
            let latest = package.latest_release.as_ref().map_or_else(String::new, |u| format!(" of {}", u.date));
            let young = if young(package, now) {
                format!("; it is younger than {YOUNG_PACKAGE_DAYS} days, so few have used it yet")
            } else {
                String::new()
            };
            let license = package.license.as_deref().unwrap_or("not stated");
            format!("{lead}on {index} {since}, latest {}{latest}, license {license}{young}", package.version)
        }
    }
}

/// Whether the package's first release is under YOUNG_PACKAGE_DAYS days before now; one with none is.
fn young(package: &IndexedPackage, now: f64) -> bool {
    package.first_release.as_ref().is_none_or(|first| now - first.at < (YOUNG_PACKAGE_DAYS * 86_400) as f64)
}

#[cfg(test)]
mod tests;
