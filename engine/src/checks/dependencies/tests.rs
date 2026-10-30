//! tests/test_orchestrator.py's TestNewDependencies, and the manifests, requirements, spec names and lines that name a
//! package of checks.json, as the Python engine reads them.

use std::sync::Arc;

use super::*;
use crate::checks::tests::{always, changed, new_file_diff, pyproject_diff, pyproject_files, pyproject_with, Change};
use crate::checks::Severity;
use crate::crates_io::fake::{CrateJson, FakeCratesIo};
use crate::crates_io::CratesIo;
use crate::http::Http;
use crate::package_index::PackageIndexes;
use crate::py::json::Json;
use crate::pypi::fake::{FakePyPI, ProjectJson};
use crate::pypi::PyPI;
use crate::test_support::oracle;

fn find(change: &Change, pypi: &Arc<FakePyPI>) -> CheckFindings {
    let indexes = PackageIndexes::with_pypi(Arc::clone(pypi) as Arc<dyn Http>);
    change.run(&always, |c| new_dependencies(c, &indexes).unwrap())
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
        lock += &format!(
            "\n[[package]]\nname = \"{n}\"\nversion = \"1.0\"\nsource = {{ registry = \"https://pypi.org/simple\" }}\n"
        );
    }
    lock += "\n[[package]]\nname = \"vendored\"\nversion = \"1.0\"\nsource = { path = \"vendor/x\" }\n";
    let diff = pyproject_diff(&["limits"])
        + "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n@@ -6,0 +7,20 @@\n";
    let mut files = pyproject_files(&["limits"]);
    files.push(("base", "uv.lock", Some(lock_base.to_string())));
    files.push(("new", "uv.lock", Some(lock)));
    let pypi = FakePyPI::with(&[
        ("limits", ProjectJson::default()),
        (
            "deprecated",
            ProjectJson {
                first: "2016-11-01",
                latest: "2026-10-20",
                version: "1.2.18",
                license: None,
                classifiers: vec!["License :: OSI Approved :: MIT License"],
                ..ProjectJson::default()
            },
        ),
        (
            "packaging",
            ProjectJson { license: Some(""), expression: Some("Apache-2.0 OR BSD-2-Clause"), ..ProjectJson::default() },
        ),
    ]);
    let found = find(&Change::new(&diff).files(files), &pypi);
    assert_eq!(found.issues, []);
    assert_eq!(
        rules(&found.to_weigh),
        [(Some("pyproject.toml"), Some(5), "added"), (Some("uv.lock"), None, "pulled-in")]
    );
    assert_eq!(
        found.to_weigh[1].message,
        "3 new packages that the change's dependencies pull in: `deprecated` (on PyPI since 2016-11-01, latest 1.2.18 \
         of 2026-10-20, license MIT License), `packaging` (on PyPI since 2015-01-03, latest 3.13.0 of 2024-06-22, \
         license Apache-2.0 OR BSD-2-Clause), `vendored` (from the path vendor/x, not from PyPI)."
    );
    assert_eq!(pypi.state().asked, ["limits", "deprecated", "packaging"]);
}

#[test]
fn pypi_is_asked_no_more_once_it_failed() {
    let pypi = none();
    pypi.state().down = true;
    let found = find(
        &Change::new(&pyproject_diff(&["limits", "requests"])).files(pyproject_files(&["limits", "requests"])),
        &pypi,
    );
    assert_eq!(pypi.state().asked, ["limits"]);
    assert_eq!(
        rules(&found.issues).iter().map(|r| (r.0, r.2)).collect::<Vec<_>>(),
        [(Some("pyproject.toml"), "not-in-spec"), (None, "unchecked")]
    );
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
    let indexes = PackageIndexes::with_pypi(Arc::clone(&pypi) as Arc<dyn Http>);
    let found = change.run(&|| answers.borrow_mut().pop().unwrap(), |c| new_dependencies(c, &indexes).unwrap());
    assert_eq!(pypi.state().asked, ["limits"]);
    assert_eq!(found.to_weigh.iter().map(|f| f.rule.as_str()).collect::<Vec<_>>(), ["added"]);
    assert_eq!(
        messages(&found.issues),
        [(
            "unchecked",
            "PyPI could not be asked about `rich`, `attrs`: the quality \
        round had too little time left to ask; whether they exist is not known."
        )]
    );
}

#[test]
fn a_dependency_is_found_on_the_line_that_names_it_whole() {
    let pypi = FakePyPI::with(&[("zope-interface", ProjectJson::default()), ("zope", ProjectJson::default())]);
    let reqs = ["zope.interface>=5", "zope"];
    let found =
        find(&Change::new(&pyproject_diff(&reqs)).files(pyproject_files(&reqs)).spec("zope and zope.interface"), &pypi);
    let lines: Vec<(Option<u64>, String)> =
        found.to_weigh.iter().map(|f| (f.line, f.message.chars().take(16).collect())).collect();
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
    let text = "[project]\nname = \"My_Proj\"\ndependencies = [\"Foo.Bar[extra] >=1; python_version>'3'\", \"baz @ \
        https://example.com/baz.whl\"]\n\
                [project.optional-dependencies]\ndocs = [\"sphinx\"]\nall = [\"my-proj[docs]\"]\n\
                [dependency-groups]\ndev = [\"pytest\", {include-group = \"lint\"}, \"my.proj[docs]\"]\nlint = \
                    [\"ruff==0.5\"]\n\
                [tool.uv]\ndev-dependencies = [\"ignored\"]\n";
    let deps =
        python_dependencies(&|path: &str| Ok((path == "pyproject.toml").then(|| text.as_bytes().to_vec()))).unwrap();
    let direct: Vec<(&str, Option<PackageSource>)> = deps.direct.iter().map(|(k, d)| (k, d.source.clone())).collect();
    assert_eq!(
        direct,
        [
            ("foo-bar", None),
            ("baz", Some(PackageSource::new(SourceKind::Url, "https://example.com/baz.whl"))),
            ("sphinx", None),
            ("pytest", None),
            ("ruff", None),
        ]
    );
}

#[test]
fn the_project_itself_in_an_extra_is_no_new_dependency() {
    let new = pyproject_with(&[]) + "[project.optional-dependencies]\nall = [\"limiter[a,b]\"]\n";
    let pypi = none();
    let change = Change::new("")
        .files(vec![("base", "pyproject.toml", Some(pyproject_with(&[]))), ("new", "pyproject.toml", Some(new))])
        .counts(vec![changed("pyproject.toml", "pyproject.toml", 2, 0)]);
    assert_eq!(find(&change, &pypi), CheckFindings::default());
    assert!(pypi.state().asked.is_empty());
}

/// A change whose pyproject.toml, and uv.lock when given, go from base to new.
fn changed_manifests(base: String, new: String, lock_base: Option<String>, lock: Option<String>) -> Change {
    Change::new("")
        .files(vec![
            ("base", "pyproject.toml", Some(base)),
            ("new", "pyproject.toml", Some(new)),
            ("base", "uv.lock", lock_base),
            ("new", "uv.lock", lock),
        ])
        .spec("Use `forked`, `wheel-pkg`, `corp-utils`, `corp-auth` and `rich`.")
        .counts(vec![changed("pyproject.toml", "pyproject.toml", 1, 0), changed("uv.lock", "uv.lock", 1, 0)])
}

fn squatted(names: &[&'static str]) -> Arc<FakePyPI> {
    FakePyPI::with(&names.iter().map(|n| (*n, ProjectJson::default())).collect::<Vec<_>>())
}

#[test]
fn a_package_with_a_uv_source_is_not_looked_up_on_pypi() {
    let new = "[project]\nname = \"limiter\"\ndependencies = [\"httpx>=0.27\", \"member\", \"local\", \"forked\", \
        \"wheel-pkg\"]\n\
               [tool.uv.sources]\nmember = { workspace = true }\nlocal = { path = \"../local\", editable = true }\n\
               forked = [{ git = \"https://github.com/x/forked\", rev = \"v1\" }]\n\
               wheel-pkg = { url = \"https://example.com/wheel_pkg-1.0-py3-none-any.whl\" }\n";
    // Each name squatted on PyPI: none of their metadata may show.
    let pypi = squatted(&["member", "local", "forked", "wheel-pkg"]);
    let found = find(&changed_manifests(pyproject_with(&[]), new.to_string(), None, None), &pypi);
    assert!(pypi.state().asked.is_empty());
    assert_eq!(
        messages(&found.issues),
        [(
            "not-in-spec",
            "`local` is a new dependency that the spec does not name; from the path ../local, not from PyPI."
        )]
    );
    assert_eq!(
        messages(&found.to_weigh),
        [
            ("added", "`member`: a member of the project's workspace, not from PyPI."),
            (
                "added",
                "`forked`, which the spec names: from the repository https://github.com/x/forked, not from PyPI."
            ),
            (
                "added",
                "`wheel-pkg`, which the spec names: from https://example.com/wheel_pkg-1.0-py3-none-any.whl, not \
            from PyPI."
            ),
        ]
    );
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
        "[project]\nname = \"limiter\"\ndependencies = [\"httpx>=0.27\", \"corp-utils\", \"corp-auth\", \
            \"corp-secret\"]\n\
         [[tool.uv.index]]\nname = \"corp\"\nurl = \"{corp}\"\nexplicit = true\n\
         [tool.uv.sources]\ncorp-utils = {{ index = \"corp\" }}\ncorp-secret = {{ index = \"corp\" }}\n"
    );
    let pypi_index = "https://pypi.org/simple";
    let lock_base = lock(&[("httpx", pypi_index)]);
    let lock_new = lock(&[
        ("httpx", pypi_index),
        ("corp-utils", corp),
        ("corp-auth", corp),
        ("corp-secret", corp),
        ("corp-lib", corp),
    ]);
    let pypi = squatted(&["corp-utils", "corp-auth", "corp-secret", "corp-lib"]);
    let found = find(&changed_manifests(pyproject_with(&[]), new, Some(lock_base), Some(lock_new)), &pypi);
    assert!(pypi.state().asked.is_empty());
    let whence = format!("from the index {corp}, not checked against PyPI");
    assert_eq!(
        messages(&found.issues),
        [("not-in-spec", format!("`corp-secret` is a new dependency that the spec does not name; {whence}.").as_str())]
    );
    let weighed: Vec<(Option<&str>, &str, &str)> =
        found.to_weigh.iter().map(|f| (f.path.as_deref(), f.rule.as_str(), f.message.as_str())).collect();
    assert_eq!(
        weighed,
        [
            (Some("pyproject.toml"), "other-index", format!("`corp-utils`, which the spec names: {whence}.").as_str()),
            (Some("pyproject.toml"), "other-index", format!("`corp-auth`, which the spec names: {whence}.").as_str()),
            (
                Some("uv.lock"),
                "pulled-in",
                format!("1 new packages that the change's dependencies pull in: `corp-lib` ({whence}).").as_str()
            ),
        ]
    );
    assert!(!found.issues.iter().chain(&found.to_weigh).any(|f| f.message.contains("on PyPI since")));
}

#[test]
fn a_dependency_moved_to_another_source_is_reported() {
    let evil = "https://evil.example/simple";
    let new = pyproject_with(&["requests"])
        + &format!(
            "[[tool.uv.index]]\nname = \"evil\"\nurl = \
        \"{evil}\"\n[tool.uv.sources]\nrequests = {{ index = \"evil\" }}\n"
        );
    let lock_of =
        |registry: &str| lock(&[("requests", registry), ("idna", registry), ("httpx", "https://pypi.org/simple")]);
    let files = vec![
        ("base", "pyproject.toml", Some(pyproject_with(&["requests"]))),
        ("new", "pyproject.toml", Some(new.clone())),
        ("base", "uv.lock", Some(lock_of("https://pypi.org/simple"))),
        ("new", "uv.lock", Some(lock_of(evil))),
    ];
    let added: String = new.split('\n').skip(6).filter(|l| !l.is_empty()).map(|l| format!("+{l}\n")).collect();
    let diff = format!(
        "diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ \
        -6,0 +7,5 @@\n{added}"
    );
    let pypi = none();
    let found = find(&Change::new(&diff).files(files), &pypi);
    assert_eq!(found.issues, []);
    assert!(pypi.state().asked.is_empty());
    let whence = format!("from the index {evil}, not checked against PyPI");
    assert_eq!(
        found.to_weigh,
        [
            Finding::warning(
                Some("pyproject.toml"),
                Some(11),
                "source-changed",
                format!("`requests`: at the base, from PyPI; in the change, {whence}.")
            ),
            Finding::warning(
                Some("uv.lock"),
                None,
                "source-changed",
                format!("`idna`: at the base, from PyPI; in the change, {whence}.")
            ),
        ]
    );
}

#[test]
fn a_uv_source_of_a_shape_this_check_does_not_know_is_reported() {
    let new = pyproject_with(&["forked", "local"])
        + "[tool.uv.sources]\nforked = { mirror = \
        \"https://example.com/forked\", tag = \"v1\" }\nlocal = { mirror = \"../local\" }\n";
    let pypi = squatted(&["forked", "local"]);
    let found = find(&changed_manifests(pyproject_with(&[]), new, None, None), &pypi);
    assert!(pypi.state().asked.is_empty());
    assert_eq!(
        messages(&found.to_weigh),
        [(
            "unknown-source",
            "`forked`, which the spec names: from the uv source { \
        mirror = \"https://example.com/forked\", tag = \"v1\" }, which this check does not know, so where it comes \
        from is not checked."
        )]
    );
    assert_eq!(
        messages(&found.issues),
        [(
            "not-in-spec",
            "`local` is a new dependency that the spec does not name; \
        from the uv source { mirror = \"../local\" }, which this check does not know, so where it comes from is not \
        checked."
        )]
    );
}

#[test]
fn a_default_index_replaces_pypi() {
    let new = pyproject_with(&["rich"])
        + "[[tool.uv.index]]\nname = \"mirror\"\nurl = \"https://mirror.example/simple\"\ndefault = true\n";
    let pypi = squatted(&["rich"]);
    let found = find(&changed_manifests(pyproject_with(&[]), new, None, None), &pypi);
    assert_eq!(found.issues, []);
    assert!(pypi.state().asked.is_empty());
    let whence = "from the index https://mirror.example/simple, not checked against PyPI";
    // The default index takes every dependency, httpx too.
    assert_eq!(
        messages(&found.to_weigh),
        [
            ("source-changed", format!("`httpx`: at the base, from PyPI; in the change, {whence}.").as_str()),
            ("other-index", format!("`rich`, which the spec names: {whence}.").as_str()),
        ]
    );
}

#[test]
fn only_the_manifests_at_the_top_level_count() {
    let change = Change::new("")
        .files(vec![
            ("base", "tests/fixtures/x/pyproject.toml", Some(pyproject_with(&[]))),
            ("new", "tests/fixtures/x/pyproject.toml", Some(pyproject_with(&["made-up-package"]))),
            ("new", "tests/fixtures/bad/uv.lock", Some("[[package\n".to_string())),
        ])
        .counts(vec![
            changed("tests/fixtures/x/pyproject.toml", "tests/fixtures/x/pyproject.toml", 1, 0),
            changed("tests/fixtures/bad/uv.lock", "tests/fixtures/bad/uv.lock", 1, 0),
        ]);
    let pypi = none();
    assert_eq!(find(&change, &pypi), CheckFindings::default());
    assert!(pypi.state().asked.is_empty());
}

fn broken() -> Option<String> {
    Some("[[package\n".to_string())
}

#[test]
fn a_manifest_broken_at_the_base_and_left_alone_is_weighed_not_failed() {
    let files = vec![
        ("base", "pyproject.toml", Some(pyproject_with(&[]))),
        ("new", "pyproject.toml", Some(pyproject_with(&["rich"]))),
        ("base", "uv.lock", broken()),
        ("new", "uv.lock", broken()),
    ];
    let pypi = none();
    let found = find(&Change::new(&pyproject_diff(&["rich"])).files(files), &pypi);
    assert_eq!(found.issues, []);
    assert!(pypi.state().asked.is_empty());
    assert_eq!(
        found.to_weigh.iter().map(|f| (f.path.as_deref(), f.severity, f.rule.as_str())).collect::<Vec<_>>(),
        [(Some("uv.lock"), Severity::Warning, "unreadable")]
    );
    assert!(found.to_weigh[0].message.starts_with("`uv.lock` cannot be read at the base: it is not TOML: "));
}

#[test]
fn each_manifest_is_judged_by_its_own_base() {
    let files = vec![
        ("base", "pyproject.toml", Some(pyproject_with(&[]))),
        ("new", "pyproject.toml", Some("[project\n".to_string())),
        ("base", "uv.lock", broken()),
        ("new", "uv.lock", broken()),
    ];
    let pypi = none();
    let found = find(&Change::new(&pyproject_diff(&["rich"])).files(files), &pypi);
    assert_eq!(
        found.issues.iter().map(|f| (f.path.as_deref(), f.severity, f.rule.as_str())).collect::<Vec<_>>(),
        [(Some("pyproject.toml"), Severity::Error, "unreadable")]
    );
    assert!(found.issues[0].message.contains("cannot be read in the change"));
    assert_eq!(
        found.to_weigh.iter().map(|f| (f.path.as_deref(), f.severity, f.rule.as_str())).collect::<Vec<_>>(),
        [(Some("uv.lock"), Severity::Warning, "unreadable")]
    );
    assert!(pypi.state().asked.is_empty());
}

#[test]
fn a_manifest_the_change_breaks_fails_whatever_the_base() {
    let files = vec![
        ("base", "pyproject.toml", Some("[project\n".to_string())),
        ("new", "pyproject.toml", Some("[project\n".to_string())),
        ("base", "uv.lock", None),
        ("new", "uv.lock", broken()),
    ];
    let found = find(&Change::new(&pyproject_diff(&["rich"])).files(files), &none());
    assert_eq!(rules(&found.issues), [(Some("uv.lock"), None, "unreadable")]);
    assert_eq!(rules(&found.to_weigh), [(Some("pyproject.toml"), None, "unreadable")]);
    assert!(found.issues[0].message.contains("cannot be read in the change"));
}

#[test]
fn a_manifest_unreadable_at_the_base_is_weighed_not_failed() {
    let files = vec![
        ("base", "pyproject.toml", Some("[project\n".to_string())),
        ("new", "pyproject.toml", Some(pyproject_with(&["rich"]))),
    ];
    let pypi = none();
    let found = find(&Change::new(&pyproject_diff(&["rich"])).files(files), &pypi);
    assert_eq!(found.issues, []);
    assert!(pypi.state().asked.is_empty());
    assert_eq!(
        found.to_weigh.iter().map(|f| (f.path.as_deref(), f.line, f.severity, f.rule.as_str())).collect::<Vec<_>>(),
        [(Some("pyproject.toml"), None, Severity::Warning, "unreadable")]
    );
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
        let found = new_dependencies(&change, &PackageIndexes::with_pypi(none())).unwrap();
        assert_eq!(rules(&found.issues), [(Some("pyproject.toml"), None, "unreadable")], "{why}");
        let message = &found.issues[0].message;
        assert!(message.contains(why), "{message}");
        assert!(message.starts_with("`pyproject.toml` cannot be read in the change: "));
        assert!(message.ends_with("; the change's new dependencies were not checked."));
    }
}

#[test]
fn a_deleted_or_untouched_manifest_adds_nothing() {
    let diff = "diff --git a/pyproject.toml b/pyproject.toml\ndeleted file mode 100644\n--- a/pyproject.toml\n+++ \
        /dev/null\n@@ -1,5 +0,0 @@\n";
    let pypi = none();
    let deleted = Change::new(diff)
        .files(vec![("base", "pyproject.toml", Some(pyproject_with(&["x"])))])
        .counts(vec![changed("pyproject.toml", "pyproject.toml", 0, 5)]);
    assert_eq!(find(&deleted, &pypi), CheckFindings::default());
    assert_eq!(find(&Change::new(&new_file_diff("app.py", 3)), &pypi), CheckFindings::default());
    assert!(pypi.state().asked.is_empty());
}

#[test]
fn a_manifest_moved_out_of_the_top_level_adds_nothing() {
    let moved = pyproject_with(&["requests", "rich"]);
    let pypi = none();
    let change = Change::new("")
        .files(vec![("base", "pyproject.toml", Some(moved.clone())), ("new", "backend/pyproject.toml", Some(moved))])
        .counts(vec![changed("pyproject.toml", "backend/pyproject.toml", 0, 0)]);
    assert_eq!(find(&change, &pypi), CheckFindings::default());
    assert!(pypi.state().asked.is_empty());
}

#[test]
fn a_manifest_moved_to_the_top_level_brings_no_base_of_its_own() {
    let fixture = pyproject_with(&["evilpkg"]);
    let change = Change::new("")
        .files(vec![
            ("base", "tests/fixtures/pyproject.toml", Some(fixture.clone())),
            ("new", "pyproject.toml", Some(fixture)),
        ])
        .counts(vec![changed("tests/fixtures/pyproject.toml", "pyproject.toml", 0, 0)]);
    let found = find(&change, &none());
    let heads: Vec<(Option<&str>, &str, String)> = found
        .issues
        .iter()
        .map(|f| (f.path.as_deref(), f.rule.as_str(), f.message.chars().take(9).collect()))
        .collect();
    assert_eq!(
        heads,
        [
            (Some("pyproject.toml"), "not-in-spec", "`httpx` i".to_string()),
            (Some("pyproject.toml"), "not-in-spec", "`evilpkg`".to_string())
        ]
    );
}

#[test]
fn a_new_manifest_adds_all_it_names() {
    let change = Change::new(&new_file_diff("pyproject.toml", 6)).files(vec![(
        "new",
        "pyproject.toml",
        Some(pyproject_with(&[])),
    )]);
    let found = find(&change, &FakePyPI::with(&[("httpx", ProjectJson::default())]));
    assert_eq!(rules(&found.issues), [(Some("pyproject.toml"), Some(4), "not-in-spec")]);
}

#[test]
fn a_read_error_fails_the_check() {
    let file_at =
        |_: &str, _: &str| -> Result<Option<Vec<u8>>, OrchestratorError> { Err(OrchestratorError::new("git failed")) };
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
    assert_eq!(
        new_dependencies(&change, &PackageIndexes::with_pypi(none())),
        Err(OrchestratorError::new("git failed"))
    );
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

fn sources(map: &Map<Dependency>) -> Json {
    Json::Array(
        map.iter()
            .map(|(k, d)| {
                let s = d
                    .source
                    .as_ref()
                    .map_or(Json::Null, |s| Json::Array(vec![kind_name(s.kind).into(), s.place.as_str().into()]));
                Json::Array(vec![k.into(), s])
            })
            .collect(),
    )
}

fn cases(key: &str) -> Vec<Json> {
    oracle("checks.json").get(key).and_then(Json::as_array).unwrap().clone()
}

#[test]
fn manifests_read_as_the_python_engine_reads_them() {
    for case in cases("dependencies") {
        let file = |k: &str| case.get(k).and_then(Json::as_str).map(|t| t.as_bytes().to_vec());
        let (pyproject, lock) = (file("pyproject"), file("lock"));
        let deps = python_dependencies(&|path: &str| {
            Ok(if path == "pyproject.toml" { pyproject.clone() } else { lock.clone() })
        })
        .unwrap();
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

// -- Cargo workspaces and crates.io --------------------------------------------------------------------------------

/// The top-level Cargo.toml of a workspace of the members engine and app, sharing serde, with crates from line 6 on.
fn cargo_root_with(crates: &[&str]) -> String {
    let crates: String = crates.iter().map(|c| format!("{c}\n")).collect();
    format!(
        "[workspace]\nmembers = [\"engine\", \"app/\"]\nexclude = [\"tools\"]\n\n[workspace.dependencies]\n{crates}\
         serde = \"1\"\n"
    )
}

const ENGINE_MANIFEST: &str =
    "[package]\nname = \"engine\"\n\n[dependencies]\nserde = { workspace = true }\n";
const APP_MANIFEST: &str =
    "[package]\nname = \"app\"\n\n[dependencies]\nengine = { path = \"../engine\" }\n";

/// `git diff -U0` of path gaining lines from line from on.
fn adding(path: &str, from: usize, lines: &[&str]) -> String {
    let added: String = lines.iter().map(|l| format!("+{l}\n")).collect();
    format!(
        "diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{},0 +{from},{} @@\n{added}",
        from - 1,
        lines.len()
    )
}

/// The workspace's files at base and new: the top-level Cargo.toml without and with the crates, and the members'.
fn cargo_files(crates: &[&str]) -> Vec<(&'static str, &'static str, Option<String>)> {
    let mut files = vec![
        ("base", "Cargo.toml", Some(cargo_root_with(&[]))),
        ("new", "Cargo.toml", Some(cargo_root_with(crates))),
    ];
    for side in ["base", "new"] {
        files.push((side, "engine/Cargo.toml", Some(ENGINE_MANIFEST.to_string())));
        files.push((side, "app/Cargo.toml", Some(APP_MANIFEST.to_string())));
    }
    files
}

fn find_crates(change: &Change, crates: &Arc<FakeCratesIo>) -> CheckFindings {
    let indexes = PackageIndexes {
        pypi: PyPI::new(none()),
        crates_io: CratesIo::new(Arc::clone(crates) as Arc<dyn Http>),
    };
    change.run(&always, |c| new_dependencies(c, &indexes).unwrap())
}

/// cargo_dependencies of the files, by path.
fn cargo_read(files: &[(&str, &str)]) -> Dependencies {
    cargo_dependencies(&|path: &str| {
        Ok(files
            .iter()
            .find(|(p, _)| *p == path)
            .map(|(_, text)| text.as_bytes().to_vec()))
    })
    .unwrap()
}

fn sourced(deps: &Map<Dependency>) -> Vec<(&str, &str, Option<PackageSource>)> {
    deps.iter().map(|(k, d)| (k, d.manifest.as_str(), d.source.clone())).collect()
}

#[test]
fn every_dependency_table_of_the_workspace_and_its_members_is_direct() {
    let root = "[package]\nname = \"top\"\n\n[dependencies]\nanyhow = \"1\"\nfast = { package = \"Fast_Hash\", \
        version = \
        \"2\" }\n\n[target.'cfg(unix)'.dependencies]\nnix = \"0.31\"\n\n[build-dependencies]\ncc = \"1\"\n\n\
        [workspace]\nmembers = [\"./engine\"]\n\n[workspace.dependencies]\nserde = { version = \"1\", package = \
        \"serde_derive\" }\nmine = { git = \"https://example.com/mine.git\", branch = \"main\" }\n";
    let engine = "[package]\nname = \"engine\"\n\n[dependencies]\nserde = { workspace = true }\nanyhow = \"1\"\n\
        private = { version = \"1\", registry = \"corp\" }\nlocal = { path = \"../local\" }\n\n[dev-dependencies]\n\
        tempfile = \"3\"\n";
    let deps = cargo_read(&[("Cargo.toml", root), ("engine/Cargo.toml", engine)]);
    assert_eq!(deps.unreadable, Map::new());
    assert_eq!(
        sourced(&deps.direct),
        [
            ("anyhow", "Cargo.toml", None),
            ("fast-hash", "Cargo.toml", None),
            ("cc", "Cargo.toml", None),
            ("nix", "Cargo.toml", None),
            ("serde-derive", "Cargo.toml", None),
            (
                "mine",
                "Cargo.toml",
                Some(PackageSource::new(SourceKind::Git, "https://example.com/mine.git"))
            ),
            (
                "private",
                "engine/Cargo.toml",
                Some(PackageSource::new(SourceKind::Index, "corp"))
            ),
            (
                "local",
                "engine/Cargo.toml",
                Some(PackageSource::new(SourceKind::Workspace, ""))
            ),
            ("tempfile", "engine/Cargo.toml", None),
        ]
    );
}

#[test]
fn the_lock_file_pins_what_comes_from_a_registry_or_a_repository() {
    let lock = "version = 4\n\n[[package]]\nname = \"engine\"\nversion = \"0.1.0\"\n\n\
        [[package]]\nname = \"serde\"\nversion = \"1.0.0\"\n\
        source = \"registry+https://github.com/rust-lang/crates.io-index\"\n\n\
        [[package]]\nname = \"serde_json\"\nversion = \"1.0.0\"\nsource = \"sparse+https://index.crates.io/\"\n\n\
        [[package]]\nname = \"corp-log\"\nversion = \"1.0.0\"\n\
        source = \"sparse+https://crates.corp.example/index/\"\n\n\
        [[package]]\nname = \"mine\"\nversion = \"0.1.0\"\n\
        source = \"git+https://example.com/mine.git?branch=main#0123abc\"\n";
    let deps = cargo_read(&[("Cargo.lock", lock)]);
    assert_eq!(
        sourced(&deps.locked),
        [
            ("serde", "Cargo.lock", None),
            ("serde-json", "Cargo.lock", None),
            (
                "corp-log",
                "Cargo.lock",
                Some(PackageSource::new(SourceKind::Index, "https://crates.corp.example/index/"))
            ),
            (
                "mine",
                "Cargo.lock",
                Some(PackageSource::new(SourceKind::Git, "https://example.com/mine.git?branch=main"))
            ),
        ]
    );
}

#[test]
fn a_workspace_this_check_cannot_follow_is_unreadable() {
    let cases = [
        (
            "[workspace]\nmembers = [\"crates/*\"]\n",
            "Cargo.toml",
            "its workspace member `crates/*` is a glob",
        ),
        (
            "[workspace]\nmembers = [\"gone\"]\n",
            "gone/Cargo.toml",
            "the workspace names it a member, but it does not exist",
        ),
        (
            "[workspace]\nmembers = \"engine\"\n",
            "Cargo.toml",
            "[workspace] members is not a list of strings",
        ),
        (
            "[dependencies]\nserde = { workspace = true }\n",
            "Cargo.toml",
            "`serde` in [dependencies] takes `workspace = true`",
        ),
        (
            "[dependencies]\nserde = 1\n",
            "Cargo.toml",
            "`serde` in [dependencies] is neither a version nor a table",
        ),
    ];
    for (root, path, why) in cases {
        let deps = cargo_read(&[("Cargo.toml", root)]);
        assert!(
            deps.unreadable
                .get(path)
                .is_some_and(|w| w.starts_with(why)),
            "{root}: {:?}",
            deps.unreadable
        );
    }
}

#[test]
fn a_crate_the_spec_names_is_reported_with_what_crates_io_knows_and_one_it_does_not_name_fails() {
    let crates = FakeCratesIo::with(&[
        ("tokio", CrateJson::default()),
        (
            "serde-yaml",
            CrateJson {
                created: "2026-10-20",
                latest: "2026-10-21",
                version: "0.1.0",
                license: None,
            },
        ),
    ]);
    let change = Change::new(&adding(
        "Cargo.toml",
        6,
        &["tokio = \"1\"", "serde_yaml = \"0.1\""],
    ))
    .files(cargo_files(&["tokio = \"1\"", "serde_yaml = \"0.1\""]))
    .spec("Run it on Tokio.");
    let found = find_crates(&change, &crates);
    assert_eq!(
        rules(&found.issues),
        [(Some("Cargo.toml"), Some(7), "not-in-spec")]
    );
    assert_eq!(
        found.issues[0].message,
        "`serde-yaml` is a new dependency that the spec does not name; on crates.io since 2026-10-20, latest 0.1.0 of \
         2026-10-21, license not stated; it is younger than 14 days, so few have used it yet."
    );
    assert_eq!(
        rules(&found.to_weigh),
        [(Some("Cargo.toml"), Some(6), "added")]
    );
    assert_eq!(
        found.to_weigh[0].message,
        "`tokio`, which the spec names: on crates.io since 2014-12-05, latest 1.0.228 of 2026-09-01, license MIT OR \
         Apache-2.0."
    );
    assert_eq!(crates.state().asked, ["tokio", "serde-yaml"]);
}

#[test]
fn a_crate_crates_io_does_not_know_fails() {
    let crates = FakeCratesIo::with(&[]);
    let change = Change::new(&adding("Cargo.toml", 6, &["tokioo = \"1\""]))
        .files(cargo_files(&["tokioo = \"1\""]))
        .spec("tokioo");
    let found = find_crates(&change, &crates);
    assert_eq!(
        rules(&found.issues),
        [(Some("Cargo.toml"), Some(6), "not-on-crates-io")]
    );
    assert!(found.issues[0]
        .message
        .starts_with("crates.io does not know `tokioo`: "));
}

#[test]
fn a_member_s_new_crate_is_found_in_its_own_manifest() {
    let engine = format!("{ENGINE_MANIFEST}rand = \"0.9\"\n");
    let mut files = cargo_files(&[]);
    files.retain(|(side, path, _)| !(*side == "new" && *path == "engine/Cargo.toml"));
    files.push(("new", "engine/Cargo.toml", Some(engine)));
    let crates = FakeCratesIo::with(&[("rand", CrateJson::default())]);
    let found = find_crates(
        &Change::new(&adding("engine/Cargo.toml", 6, &["rand = \"0.9\""])).files(files),
        &crates,
    );
    assert_eq!(
        rules(&found.issues),
        [(Some("engine/Cargo.toml"), Some(6), "not-in-spec")]
    );
}

#[test]
fn a_crate_from_a_repository_or_another_registry_is_not_asked_of_crates_io() {
    let added = [
        "mine = { git = \"https://example.com/mine.git\" }",
        "corp = { version = \"1\", registry = \"corp\" }",
    ];
    let crates = FakeCratesIo::with(&[
        ("mine", CrateJson::default()),
        ("corp", CrateJson::default()),
    ]);
    let found = find_crates(
        &Change::new(&adding("Cargo.toml", 6, &added))
            .files(cargo_files(&added))
            .spec("mine, corp"),
        &crates,
    );
    assert_eq!(found.issues, []);
    assert_eq!(
        messages(&found.to_weigh),
        [
            (
                "added",
                "`mine`, which the spec names: from the repository https://example.com/mine.git, not from crates.io."
            ),
            ("other-index", "`corp`, which the spec names: from the index corp, not checked against crates.io."),
        ]
    );
    assert!(crates.state().asked.is_empty());
}

#[test]
fn crates_io_unreachable_leaves_its_crates_unchecked_and_pypi_asked() {
    let crates = FakeCratesIo::with(&[]);
    crates.state().down = true;
    let pypi = FakePyPI::with(&[("limits", ProjectJson::default())]);
    let mut files = cargo_files(&["tokio = \"1\"", "rand = \"0.9\""]);
    files.extend(pyproject_files(&["limits"]));
    let diff = adding("Cargo.toml", 6, &["tokio = \"1\"", "rand = \"0.9\""])
        + &pyproject_diff(&["limits"]);
    let change = Change::new(&diff)
        .files(files)
        .spec("limits, tokio and rand");
    let indexes = PackageIndexes {
        pypi: PyPI::new(Arc::clone(&pypi) as Arc<dyn Http>),
        crates_io: CratesIo::new(Arc::clone(&crates) as Arc<dyn Http>),
    };
    let found = change.run(&always, |c| new_dependencies(c, &indexes).unwrap());
    assert_eq!(
        (crates.state().asked.clone(), pypi.state().asked.clone()),
        (vec!["tokio".to_string()], vec!["limits".to_string()])
    );
    assert_eq!(
        messages(&found.issues),
        [(
            "unchecked",
            "crates.io could not be asked about `tokio`, `rand`: GET https://crates.io/api/v1/crates/tokio: timed out; \
             whether they exist is not known."
        )]
    );
    assert_eq!(
        rules(&found.to_weigh),
        [(Some("pyproject.toml"), Some(5), "added")]
    );
}

#[test]
fn the_crates_the_lock_file_alone_adds_are_pulled_in() {
    let lock = |crates: &[&str]| -> String {
        let source = "source = \"sparse+https://index.crates.io/\"";
        crates.iter().map(|c| format!("[[package]]\nname = \"{c}\"\nversion = \"1.0.0\"\n{source}\n\n")).collect()
    };
    let mut files = cargo_files(&["tokio = \"1\""]);
    files.push(("base", "Cargo.lock", Some(lock(&["serde"]))));
    files.push(("new", "Cargo.lock", Some(lock(&["serde", "tokio", "mio"]))));
    let diff =
        adding("Cargo.toml", 6, &["tokio = \"1\""]) + &adding("Cargo.lock", 6, &["[[package]]"]);
    let crates = FakeCratesIo::with(&[
        ("tokio", CrateJson::default()),
        ("mio", CrateJson::default()),
    ]);
    let found = find_crates(&Change::new(&diff).files(files).spec("tokio"), &crates);
    assert_eq!(found.issues, []);
    assert_eq!(
        rules(&found.to_weigh),
        [
            (Some("Cargo.toml"), Some(6), "added"),
            (Some("Cargo.lock"), None, "pulled-in")
        ]
    );
    assert_eq!(
        found.to_weigh[1].message,
        "1 new packages that the change's dependencies pull in: `mio` (on crates.io since 2014-12-05, latest 1.0.228 \
         of 2026-09-01, license MIT OR Apache-2.0)."
    );
}

#[test]
fn a_cargo_manifest_anywhere_starts_the_check_and_one_untouched_does_not() {
    let crates = FakeCratesIo::with(&[]);
    let untouched = find_crates(
        &Change::new(&new_file_diff("src/lib.rs", 1)).files(cargo_files(&["tokio = \"1\""])),
        &crates,
    );
    assert_eq!(untouched, CheckFindings::default());
    let member = Change::new(&adding("app/Cargo.toml", 6, &["# a comment"]))
        .files(cargo_files(&["tokio = \"1\""]));
    assert_eq!(
        rules(&find_crates(&member, &crates).issues),
        [(Some("Cargo.toml"), None, "not-in-spec")]
    );
}
