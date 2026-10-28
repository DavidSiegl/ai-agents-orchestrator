//! tests/test_orchestrator.py's TestNewDependencies, and the manifests, requirements, spec names and lines that name a
//! package of checks.json, as the Python engine reads them.

use std::sync::Arc;

use super::*;
use crate::checks::Severity;
use crate::checks::tests::{always, changed, new_file_diff, pyproject_diff, pyproject_files, pyproject_with, Change};
use crate::http::Http;
use crate::py::json::Json;
use crate::pypi::fake::{FakePyPI, ProjectJson};
use crate::test_support::oracle;

fn find(change: &Change, pypi: &Arc<FakePyPI>) -> CheckFindings {
    let index = PyPI::new(Arc::clone(pypi) as Arc<dyn Http>);
    change.run(&always, |c| new_dependencies(c, &index).unwrap())
}

fn rules(findings: &[Finding]) -> Vec<(Option<&str>, Option<u64>, &str)> {
    findings.iter().map(|f| (f.path.as_deref(), f.line, f.rule.as_str())).collect()
}

fn messages(findings: &[Finding]) -> Vec<(&str, &str)> {
    findings.iter().map(|f| (f.rule.as_str(), f.message.as_str())).collect()
}

fn none() -> Arc<FakePyPI> {
    FakePyPI::with(&[])
}

#[test]
fn the_spec_names_a_package_as_a_word_in_any_spelling() {
    let names = spec_names("Use `Requests`. Add pytest_randomly, and ruff>=0.5 to the dev group.");
    for name in ["requests", "pytest-randomly", "ruff"] {
        assert!(names.contains(name), "{name}");
    }
    assert!(!names.contains("pytest"));
    assert!(!names.contains("request"));
}

#[test]
fn a_package_the_lock_alone_adds_is_pulled_in() {
    let lock_base = "version = 1\n\n[[package]]\nname = \"limiter\"\nversion = \"0.1\"\nsource = { virtual = \".\" }\n";
    let mut lock = lock_base.to_string();
    for n in ["limits", "Deprecated", "packaging"] {
        lock += &format!("\n[[package]]\nname = \"{n}\"\nversion = \"1.0\"\nsource = {{ registry = \"https://pypi.org/simple\" }}\n");
    }
    lock += "\n[[package]]\nname = \"vendored\"\nversion = \"1.0\"\nsource = { path = \"vendor/x\" }\n";
    let diff = pyproject_diff(&["limits"]) + "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n@@ -6,0 +7,20 @@\n";
    let mut files = pyproject_files(&["limits"]);
    files.push(("base", "uv.lock", Some(lock_base.to_string())));
    files.push(("new", "uv.lock", Some(lock)));
    let pypi = FakePyPI::with(&[
        ("limits", ProjectJson::default()),
        ("deprecated", ProjectJson { first: "2016-11-01", latest: "2026-10-20", version: "1.2.18", license: None, classifiers: vec!["License :: OSI Approved :: MIT License"], ..ProjectJson::default() }),
        ("packaging", ProjectJson { license: Some(""), expression: Some("Apache-2.0 OR BSD-2-Clause"), ..ProjectJson::default() }),
    ]);
    let found = find(&Change::new(&diff).files(files), &pypi);
    assert_eq!(found.issues, []);
    assert_eq!(rules(&found.to_weigh), [(Some("pyproject.toml"), Some(5), "added"), (Some("uv.lock"), None, "pulled-in")]);
    assert_eq!(
        found.to_weigh[1].message,
        "3 new packages that the change's dependencies pull in: `deprecated` (on PyPI since 2016-11-01, latest 1.2.18 of \
         2026-10-20, license MIT License), `packaging` (on PyPI since 2015-01-03, latest 3.13.0 of 2024-06-22, license \
         Apache-2.0 OR BSD-2-Clause), `vendored` (from the path vendor/x, not from PyPI)."
    );
    assert_eq!(pypi.state().asked, ["limits", "deprecated", "packaging"]);
}

#[test]
fn pypi_is_asked_no_more_once_it_failed() {
    let pypi = none();
    pypi.state().down = true;
    let found = find(&Change::new(&pyproject_diff(&["limits", "requests"])).files(pyproject_files(&["limits", "requests"])), &pypi);
    assert_eq!(pypi.state().asked, ["limits"]);
    assert_eq!(rules(&found.issues).iter().map(|r| (r.0, r.2)).collect::<Vec<_>>(), [(Some("pyproject.toml"), "not-in-spec"), (None, "unchecked")]);
    assert_eq!(found.issues[0].message, "`requests` is a new dependency that the spec does not name.");
    assert!(found.issues[1].message.contains("about `limits`, `requests`: GET"));
    assert_eq!(found.to_weigh, []);
}

#[test]
fn pypi_is_asked_only_while_the_round_has_time() {
    let pypi = FakePyPI::with(&[("limits", ProjectJson::default()), ("rich", ProjectJson::default())]);
    let reqs = ["limits", "rich", "attrs"];
    let change = Change::new(&pyproject_diff(&reqs)).files(pyproject_files(&reqs)).spec("limits, rich and attrs");
    let answers = std::cell::RefCell::new(vec![false, true]);
    let index = PyPI::new(Arc::clone(&pypi) as Arc<dyn Http>);
    let found = change.run(&|| answers.borrow_mut().pop().unwrap(), |c| new_dependencies(c, &index).unwrap());
    assert_eq!(pypi.state().asked, ["limits"]);
    assert_eq!(found.to_weigh.iter().map(|f| f.rule.as_str()).collect::<Vec<_>>(), ["added"]);
    assert_eq!(messages(&found.issues), [("unchecked", "PyPI could not be asked about `rich`, `attrs`: the quality round had too little time left to ask; whether they exist is not known.")]);
}

#[test]
fn a_dependency_is_found_on_the_line_that_names_it_whole() {
    let pypi = FakePyPI::with(&[("zope-interface", ProjectJson::default()), ("zope", ProjectJson::default())]);
    let reqs = ["zope.interface>=5", "zope"];
    let found = find(&Change::new(&pyproject_diff(&reqs)).files(pyproject_files(&reqs)).spec("zope and zope.interface"), &pypi);
    let lines: Vec<(Option<u64>, String)> = found.to_weigh.iter().map(|f| (f.line, f.message.chars().take(16).collect())).collect();
    assert_eq!(lines, [(Some(5), "`zope-interface`".to_string()), (Some(6), "`zope`, which th".to_string())]);
}

#[test]
fn a_dependency_from_a_url_is_not_looked_up() {
    let url = "git+https://example.com/limits.git@v3";
    let requirement = format!("limits @ {url}");
    let pypi = none();
    let found = find(&Change::new(&pyproject_diff(&[&requirement])).files(pyproject_files(&[&requirement])), &pypi);
    assert_eq!(found.issues, []);
    assert!(pypi.state().asked.is_empty());
    assert_eq!(found.to_weigh[0].message, format!("`limits`, which the spec names: from {url}, not from PyPI."));
}

#[test]
fn dependency_lists_of_every_kind_are_read_without_the_project_itself() {
    let text = "[project]\nname = \"My_Proj\"\ndependencies = [\"Foo.Bar[extra] >=1; python_version>'3'\", \"baz @ https://example.com/baz.whl\"]\n\
                [project.optional-dependencies]\ndocs = [\"sphinx\"]\nall = [\"my-proj[docs]\"]\n\
                [dependency-groups]\ndev = [\"pytest\", {include-group = \"lint\"}, \"my.proj[docs]\"]\nlint = [\"ruff==0.5\"]\n\
                [tool.uv]\ndev-dependencies = [\"ignored\"]\n";
    let deps = python_dependencies(&|path: &str| Ok((path == "pyproject.toml").then(|| text.as_bytes().to_vec()))).unwrap();
    let direct: Vec<(&str, Option<PackageSource>)> = deps.direct.iter().map(|(k, s)| (k, s.clone())).collect();
    assert_eq!(direct, [
        ("foo-bar", None),
        ("baz", Some(PackageSource::new(SourceKind::Url, "https://example.com/baz.whl"))),
        ("sphinx", None),
        ("pytest", None),
        ("ruff", None),
    ]);
}

#[test]
fn the_project_itself_in_an_extra_is_no_new_dependency() {
    let new = pyproject_with(&[]) + "[project.optional-dependencies]\nall = [\"limiter[a,b]\"]\n";
    let pypi = none();
    let change = Change::new("").files(vec![("base", "pyproject.toml", Some(pyproject_with(&[]))), ("new", "pyproject.toml", Some(new))])
        .counts(vec![changed("pyproject.toml", "pyproject.toml", 2, 0)]);
    assert_eq!(find(&change, &pypi), CheckFindings::default());
    assert!(pypi.state().asked.is_empty());
}

/// A change whose pyproject.toml, and uv.lock when given, go from base to new.
fn changed_manifests(base: String, new: String, lock_base: Option<String>, lock: Option<String>) -> Change {
    Change::new("")
        .files(vec![("base", "pyproject.toml", Some(base)), ("new", "pyproject.toml", Some(new)), ("base", "uv.lock", lock_base), ("new", "uv.lock", lock)])
        .spec("Use `forked`, `wheel-pkg`, `corp-utils`, `corp-auth` and `rich`.")
        .counts(vec![changed("pyproject.toml", "pyproject.toml", 1, 0), changed("uv.lock", "uv.lock", 1, 0)])
}

fn squatted(names: &[&'static str]) -> Arc<FakePyPI> {
    FakePyPI::with(&names.iter().map(|n| (*n, ProjectJson::default())).collect::<Vec<_>>())
}

#[test]
fn a_package_with_a_uv_source_is_not_looked_up_on_pypi() {
    let new = "[project]\nname = \"limiter\"\ndependencies = [\"httpx>=0.27\", \"member\", \"local\", \"forked\", \"wheel-pkg\"]\n\
               [tool.uv.sources]\nmember = { workspace = true }\nlocal = { path = \"../local\", editable = true }\n\
               forked = [{ git = \"https://github.com/x/forked\", rev = \"v1\" }]\n\
               wheel-pkg = { url = \"https://example.com/wheel_pkg-1.0-py3-none-any.whl\" }\n";
    // Each name squatted on PyPI: none of their metadata may show.
    let pypi = squatted(&["member", "local", "forked", "wheel-pkg"]);
    let found = find(&changed_manifests(pyproject_with(&[]), new.to_string(), None, None), &pypi);
    assert!(pypi.state().asked.is_empty());
    assert_eq!(messages(&found.issues), [("not-in-spec", "`local` is a new dependency that the spec does not name; from the path ../local, not from PyPI.")]);
    assert_eq!(messages(&found.to_weigh), [
        ("added", "`member`: a member of the project's workspace, not from PyPI."),
        ("added", "`forked`, which the spec names: from the repository https://github.com/x/forked, not from PyPI."),
        ("added", "`wheel-pkg`, which the spec names: from https://example.com/wheel_pkg-1.0-py3-none-any.whl, not from PyPI."),
    ]);
}

fn lock(packages: &[(&str, &str)]) -> String {
    let mut out = "version = 1\n".to_string();
    for (n, url) in packages {
        out += &format!("\n[[package]]\nname = \"{n}\"\nversion = \"1.0\"\nsource = {{ registry = \"{url}\" }}\n");
    }
    out
}

#[test]
fn a_package_from_another_index_is_never_checked_against_pypi() {
    let corp = "https://pypi.internal.example/simple";
    let new = format!(
        "[project]\nname = \"limiter\"\ndependencies = [\"httpx>=0.27\", \"corp-utils\", \"corp-auth\", \"corp-secret\"]\n\
         [[tool.uv.index]]\nname = \"corp\"\nurl = \"{corp}\"\nexplicit = true\n\
         [tool.uv.sources]\ncorp-utils = {{ index = \"corp\" }}\ncorp-secret = {{ index = \"corp\" }}\n"
    );
    let pypi_index = "https://pypi.org/simple";
    let lock_base = lock(&[("httpx", pypi_index)]);
    let lock_new = lock(&[("httpx", pypi_index), ("corp-utils", corp), ("corp-auth", corp), ("corp-secret", corp), ("corp-lib", corp)]);
    let pypi = squatted(&["corp-utils", "corp-auth", "corp-secret", "corp-lib"]);
    let found = find(&changed_manifests(pyproject_with(&[]), new, Some(lock_base), Some(lock_new)), &pypi);
    assert!(pypi.state().asked.is_empty());
    let whence = format!("from the index {corp}, not checked against PyPI");
    assert_eq!(messages(&found.issues), [("not-in-spec", format!("`corp-secret` is a new dependency that the spec does not name; {whence}.").as_str())]);
    let weighed: Vec<(Option<&str>, &str, &str)> = found.to_weigh.iter().map(|f| (f.path.as_deref(), f.rule.as_str(), f.message.as_str())).collect();
    assert_eq!(weighed, [
        (Some("pyproject.toml"), "other-index", format!("`corp-utils`, which the spec names: {whence}.").as_str()),
        (Some("pyproject.toml"), "other-index", format!("`corp-auth`, which the spec names: {whence}.").as_str()),
        (Some("uv.lock"), "pulled-in", format!("1 new packages that the change's dependencies pull in: `corp-lib` ({whence}).").as_str()),
    ]);
    assert!(!found.issues.iter().chain(&found.to_weigh).any(|f| f.message.contains("on PyPI since")));
}

#[test]
fn a_dependency_moved_to_another_source_is_reported() {
    let evil = "https://evil.example/simple";
    let new = pyproject_with(&["requests"]) + &format!("[[tool.uv.index]]\nname = \"evil\"\nurl = \"{evil}\"\n[tool.uv.sources]\nrequests = {{ index = \"evil\" }}\n");
    let lock_of = |registry: &str| lock(&[("requests", registry), ("idna", registry), ("httpx", "https://pypi.org/simple")]);
    let files = vec![
        ("base", "pyproject.toml", Some(pyproject_with(&["requests"]))),
        ("new", "pyproject.toml", Some(new.clone())),
        ("base", "uv.lock", Some(lock_of("https://pypi.org/simple"))),
        ("new", "uv.lock", Some(lock_of(evil))),
    ];
    let added: String = new.split('\n').skip(6).filter(|l| !l.is_empty()).map(|l| format!("+{l}\n")).collect();
    let diff = format!("diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -6,0 +7,5 @@\n{added}");
    let pypi = none();
    let found = find(&Change::new(&diff).files(files), &pypi);
    assert_eq!(found.issues, []);
    assert!(pypi.state().asked.is_empty());
    let whence = format!("from the index {evil}, not checked against PyPI");
    assert_eq!(found.to_weigh, [
        Finding::warning(Some("pyproject.toml"), Some(11), "source-changed", format!("`requests`: at the base, from PyPI; in the change, {whence}.")),
        Finding::warning(Some("uv.lock"), None, "source-changed", format!("`idna`: at the base, from PyPI; in the change, {whence}.")),
    ]);
}

#[test]
fn a_uv_source_of_a_shape_this_check_does_not_know_is_reported() {
    let new = pyproject_with(&["forked", "local"]) + "[tool.uv.sources]\nforked = { mirror = \"https://example.com/forked\", tag = \"v1\" }\nlocal = { mirror = \"../local\" }\n";
    let pypi = squatted(&["forked", "local"]);
    let found = find(&changed_manifests(pyproject_with(&[]), new, None, None), &pypi);
    assert!(pypi.state().asked.is_empty());
    assert_eq!(messages(&found.to_weigh), [("unknown-source", "`forked`, which the spec names: from the uv source { mirror = \"https://example.com/forked\", tag = \"v1\" }, which this check does not know, so where it comes from is not checked.")]);
    assert_eq!(messages(&found.issues), [("not-in-spec", "`local` is a new dependency that the spec does not name; from the uv source { mirror = \"../local\" }, which this check does not know, so where it comes from is not checked.")]);
}

#[test]
fn a_default_index_replaces_pypi() {
    let new = pyproject_with(&["rich"]) + "[[tool.uv.index]]\nname = \"mirror\"\nurl = \"https://mirror.example/simple\"\ndefault = true\n";
    let pypi = squatted(&["rich"]);
    let found = find(&changed_manifests(pyproject_with(&[]), new, None, None), &pypi);
    assert_eq!(found.issues, []);
    assert!(pypi.state().asked.is_empty());
    let whence = "from the index https://mirror.example/simple, not checked against PyPI";
    // The default index takes every dependency, httpx too.
    assert_eq!(messages(&found.to_weigh), [
        ("source-changed", format!("`httpx`: at the base, from PyPI; in the change, {whence}.").as_str()),
        ("other-index", format!("`rich`, which the spec names: {whence}.").as_str()),
    ]);
}

#[test]
fn only_the_manifests_at_the_top_level_count() {
    let change = Change::new("")
        .files(vec![
            ("base", "tests/fixtures/x/pyproject.toml", Some(pyproject_with(&[]))),
            ("new", "tests/fixtures/x/pyproject.toml", Some(pyproject_with(&["made-up-package"]))),
            ("new", "tests/fixtures/bad/uv.lock", Some("[[package\n".to_string())),
        ])
        .counts(vec![changed("tests/fixtures/x/pyproject.toml", "tests/fixtures/x/pyproject.toml", 1, 0), changed("tests/fixtures/bad/uv.lock", "tests/fixtures/bad/uv.lock", 1, 0)]);
    let pypi = none();
    assert_eq!(find(&change, &pypi), CheckFindings::default());
    assert!(pypi.state().asked.is_empty());
}

fn broken() -> Option<String> {
    Some("[[package\n".to_string())
}

#[test]
fn a_manifest_broken_at_the_base_and_left_alone_is_weighed_not_failed() {
    let files = vec![("base", "pyproject.toml", Some(pyproject_with(&[]))), ("new", "pyproject.toml", Some(pyproject_with(&["rich"]))), ("base", "uv.lock", broken()), ("new", "uv.lock", broken())];
    let pypi = none();
    let found = find(&Change::new(&pyproject_diff(&["rich"])).files(files), &pypi);
    assert_eq!(found.issues, []);
    assert!(pypi.state().asked.is_empty());
    assert_eq!(found.to_weigh.iter().map(|f| (f.path.as_deref(), f.severity, f.rule.as_str())).collect::<Vec<_>>(), [(Some("uv.lock"), Severity::Warning, "unreadable")]);
    assert!(found.to_weigh[0].message.starts_with("`uv.lock` cannot be read at the base: it is not TOML: "));
}

#[test]
fn each_manifest_is_judged_by_its_own_base() {
    let files = vec![("base", "pyproject.toml", Some(pyproject_with(&[]))), ("new", "pyproject.toml", Some("[project\n".to_string())), ("base", "uv.lock", broken()), ("new", "uv.lock", broken())];
    let pypi = none();
    let found = find(&Change::new(&pyproject_diff(&["rich"])).files(files), &pypi);
    assert_eq!(found.issues.iter().map(|f| (f.path.as_deref(), f.severity, f.rule.as_str())).collect::<Vec<_>>(), [(Some("pyproject.toml"), Severity::Error, "unreadable")]);
    assert!(found.issues[0].message.contains("cannot be read in the change"));
    assert_eq!(found.to_weigh.iter().map(|f| (f.path.as_deref(), f.severity, f.rule.as_str())).collect::<Vec<_>>(), [(Some("uv.lock"), Severity::Warning, "unreadable")]);
    assert!(pypi.state().asked.is_empty());
}

#[test]
fn a_manifest_the_change_breaks_fails_whatever_the_base() {
    let files = vec![("base", "pyproject.toml", Some("[project\n".to_string())), ("new", "pyproject.toml", Some("[project\n".to_string())), ("base", "uv.lock", None), ("new", "uv.lock", broken())];
    let found = find(&Change::new(&pyproject_diff(&["rich"])).files(files), &none());
    assert_eq!(rules(&found.issues), [(Some("uv.lock"), None, "unreadable")]);
    assert_eq!(rules(&found.to_weigh), [(Some("pyproject.toml"), None, "unreadable")]);
    assert!(found.issues[0].message.contains("cannot be read in the change"));
}

#[test]
fn a_manifest_unreadable_at_the_base_is_weighed_not_failed() {
    let files = vec![("base", "pyproject.toml", Some("[project\n".to_string())), ("new", "pyproject.toml", Some(pyproject_with(&["rich"])))];
    let pypi = none();
    let found = find(&Change::new(&pyproject_diff(&["rich"])).files(files), &pypi);
    assert_eq!(found.issues, []);
    assert!(pypi.state().asked.is_empty());
    assert_eq!(found.to_weigh.iter().map(|f| (f.path.as_deref(), f.line, f.severity, f.rule.as_str())).collect::<Vec<_>>(), [(Some("pyproject.toml"), None, Severity::Warning, "unreadable")]);
    assert!(found.to_weigh[0].message.starts_with("`pyproject.toml` cannot be read at the base: it is not TOML: "));
    assert!(found.to_weigh[0].message.ends_with("; the change's new dependencies were not checked."));
}

#[test]
fn a_manifest_that_cannot_be_read_fails_unchecked() {
    let cases: [(&[u8], &str); 4] = [
        (b"[project\n", "it is not TOML"),
        (b"[project]\ndependencies = \"x\"\n", "not a list"),
        (b"[project]\ndependencies = [\"=x\"]\n", "is not a requirement"),
        // As Host::file_at gives a file that is not UTF-8: TOML must be.
        (b"[project]\ndependencies = [\"\xff\"]\n", "codec can't decode byte 0xff"),
    ];
    for (text, why) in cases {
        let base = pyproject_with(&[]).into_bytes();
        let file_at = |commit: &str, path: &str| -> Result<Option<Vec<u8>>, OrchestratorError> {
            Ok((path == "pyproject.toml").then(|| if commit == "base" { base.clone() } else { text.to_vec() }))
        };
        let change = RunChange {
            spec: String::new(),
            base: "base".into(),
            commit: "new".into(),
            changed: Default::default(),
            files: vec![changed("pyproject.toml", "pyproject.toml", 1, 0)],
            file_at: &file_at,
            now: 0.0,
            in_time: &always,
        };
        let found = new_dependencies(&change, &PyPI::new(none())).unwrap();
        assert_eq!(rules(&found.issues), [(Some("pyproject.toml"), None, "unreadable")], "{why}");
        let message = &found.issues[0].message;
        assert!(message.contains(why), "{message}");
        assert!(message.starts_with("`pyproject.toml` cannot be read in the change: "));
        assert!(message.ends_with("; the change's new dependencies were not checked."));
    }
}

#[test]
fn a_deleted_or_untouched_manifest_adds_nothing() {
    let diff = "diff --git a/pyproject.toml b/pyproject.toml\ndeleted file mode 100644\n--- a/pyproject.toml\n+++ /dev/null\n@@ -1,5 +0,0 @@\n";
    let pypi = none();
    let deleted = Change::new(diff).files(vec![("base", "pyproject.toml", Some(pyproject_with(&["x"])))]).counts(vec![changed("pyproject.toml", "pyproject.toml", 0, 5)]);
    assert_eq!(find(&deleted, &pypi), CheckFindings::default());
    assert_eq!(find(&Change::new(&new_file_diff("app.py", 3)), &pypi), CheckFindings::default());
    assert!(pypi.state().asked.is_empty());
}

#[test]
fn a_manifest_moved_out_of_the_top_level_adds_nothing() {
    let moved = pyproject_with(&["requests", "rich"]);
    let pypi = none();
    let change = Change::new("").files(vec![("base", "pyproject.toml", Some(moved.clone())), ("new", "backend/pyproject.toml", Some(moved))]).counts(vec![changed("pyproject.toml", "backend/pyproject.toml", 0, 0)]);
    assert_eq!(find(&change, &pypi), CheckFindings::default());
    assert!(pypi.state().asked.is_empty());
}

#[test]
fn a_manifest_moved_to_the_top_level_brings_no_base_of_its_own() {
    let fixture = pyproject_with(&["evilpkg"]);
    let change = Change::new("").files(vec![("base", "tests/fixtures/pyproject.toml", Some(fixture.clone())), ("new", "pyproject.toml", Some(fixture))]).counts(vec![changed("tests/fixtures/pyproject.toml", "pyproject.toml", 0, 0)]);
    let found = find(&change, &none());
    let heads: Vec<(Option<&str>, &str, String)> = found.issues.iter().map(|f| (f.path.as_deref(), f.rule.as_str(), f.message.chars().take(9).collect())).collect();
    assert_eq!(heads, [(Some("pyproject.toml"), "not-in-spec", "`httpx` i".to_string()), (Some("pyproject.toml"), "not-in-spec", "`evilpkg`".to_string())]);
}

#[test]
fn a_new_manifest_adds_all_it_names() {
    let change = Change::new(&new_file_diff("pyproject.toml", 6)).files(vec![("new", "pyproject.toml", Some(pyproject_with(&[])))]);
    let found = find(&change, &FakePyPI::with(&[("httpx", ProjectJson::default())]));
    assert_eq!(rules(&found.issues), [(Some("pyproject.toml"), Some(4), "not-in-spec")]);
}

#[test]
fn a_read_error_fails_the_check() {
    let file_at = |_: &str, _: &str| -> Result<Option<Vec<u8>>, OrchestratorError> { Err(OrchestratorError::new("git failed")) };
    let change = RunChange {
        spec: String::new(),
        base: "base".into(),
        commit: "new".into(),
        changed: Default::default(),
        files: vec![changed("uv.lock", "uv.lock", 1, 0)],
        file_at: &file_at,
        now: 0.0,
        in_time: &always,
    };
    assert_eq!(new_dependencies(&change, &PyPI::new(none())), Err(OrchestratorError::new("git failed")));
}

// -- The Python engine's readings, from checks.json -----------------------------------------------------------------

fn kind_name(kind: SourceKind) -> &'static str {
    match kind {
        SourceKind::Workspace => "workspace",
        SourceKind::Path => "path",
        SourceKind::Git => "git",
        SourceKind::Url => "url",
        SourceKind::Index => "index",
        SourceKind::Unknown => "unknown",
    }
}

fn sources(map: &Map<Option<PackageSource>>) -> Json {
    Json::Array(map.iter().map(|(k, s)| {
        let s = s.as_ref().map_or(Json::Null, |s| Json::Array(vec![kind_name(s.kind).into(), s.place.as_str().into()]));
        Json::Array(vec![k.into(), s])
    }).collect())
}

fn cases(key: &str) -> Vec<Json> {
    oracle("checks.json").get(key).and_then(Json::as_array).unwrap().clone()
}

#[test]
fn manifests_read_as_the_python_engine_reads_them() {
    for case in cases("dependencies") {
        let file = |k: &str| case.get(k).and_then(Json::as_str).map(|t| t.as_bytes().to_vec());
        let (pyproject, lock) = (file("pyproject"), file("lock"));
        let deps = python_dependencies(&|path: &str| Ok(if path == "pyproject.toml" { pyproject.clone() } else { lock.clone() })).unwrap();
        assert_eq!(sources(&deps.direct), case.get("direct").cloned().unwrap(), "{case:?}");
        assert_eq!(sources(&deps.locked), case.get("locked").cloned().unwrap(), "{case:?}");
        let expected = case.get("unreadable").and_then(Json::as_array).unwrap();
        assert_eq!(deps.unreadable.len(), expected.len(), "{case:?}");
        for ((path, why), want) in deps.unreadable.iter().zip(expected) {
            let want = want.as_array().unwrap();
            assert_eq!(path, want[0].as_str().unwrap());
            let want = want[1].as_str().unwrap();
            // tomllib's own words for what is not TOML are not the toml crate's.
            match want.strip_prefix("it is not TOML: ") {
                Some(_) => assert!(why.starts_with("it is not TOML: ") && why.contains(" (at line "), "{why}"),
                None => assert_eq!(why, want),
            }
        }
    }
}

#[test]
fn requirements_names_and_lines_read_as_the_python_engine_reads_them() {
    for case in cases("requirements") {
        let case = case.as_array().unwrap();
        let got = match requirement_of(&Value::String(case[0].as_str().unwrap().to_string())) {
            Ok((key, url)) => Json::Array(vec![key.into(), url.into()]),
            Err(e) => Json::Object([("error", Json::from(e))].into_iter().collect()),
        };
        assert_eq!(got, case[1], "{:?}", case[0]);
    }
    for case in cases("package_keys") {
        let case = case.as_array().unwrap();
        assert_eq!(package_key(case[0].as_str().unwrap()), case[1].as_str().unwrap());
    }
    for case in cases("spec_names") {
        let case = case.as_array().unwrap();
        let got: Vec<Json> = spec_names(case[0].as_str().unwrap()).into_iter().map(Json::from).collect();
        assert_eq!(Json::Array(got), case[1]);
    }
    for case in cases("line_naming") {
        let case = case.as_array().unwrap();
        let lines = case[1].as_array().unwrap().iter().map(|n| n.as_int().unwrap() as u64).collect();
        let got = line_naming(case[0].as_str().unwrap(), &lines, case[2].as_str().unwrap());
        assert_eq!(got.map(|n| Json::Int(n.into())).unwrap_or(Json::Null), case[3], "{case:?}");
    }
}
