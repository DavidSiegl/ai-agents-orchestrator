//! The secret scan before a push: tests/test_orchestrator.py's TestScanUnpublished, ported case for case, over a real
//! repository and a bare origin, with a fake gitleaks on the Host's PATH; and the scan's helpers against the Python
//! engine's answers.

use super::*;
use crate::host::tests::{environment, put, read, tool, Repo, FAKE_TOKEN};
use crate::host::{ProcessHost, PushError};
use crate::test_support::oracle;

/// The fake gitleaks: writes its argv, its git environment, its working directory and the config and ignore file it
/// was given beside itself, then acts as the file `mode` says.
const FAKE_GITLEAKS: &str = r#"#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "argv"), "w") as f:
    json.dump(args, f)
with open(os.path.join(here, "env"), "w") as f:
    f.write("\n".join(f"{k}={v}" for k, v in sorted(os.environ.items()) if k.startswith("GIT_")))
with open(os.path.join(here, "cwd"), "w") as f:
    f.write(os.getcwd())
mode = open(os.path.join(here, "mode")).read().strip()
if args[:2] == ["git", "--help"]:
    sys.exit(1 if mode == "old" else 0)
for flag, name in (("--config", "config-seen"), ("--gitleaks-ignore-path", "ignore-seen")):
    with open(args[args.index(flag) + 1]) as src, open(os.path.join(here, name), "w") as dst:
        dst.write(src.read())
report = args[args.index("--report-path") + 1]
commit = next(w for w in args[args.index("git") + 1].split("=", 1)[1].split() if not w.startswith("--"))
if mode == "leak":
    with open(report, "w") as f:
        json.dump([{"RuleID": "github-pat", "File": "cfg.py", "StartLine": 1, "Commit": commit,
                    "Fingerprint": f"{commit}:cfg.py:github-pat:1", "Secret": os.environ.get("TOKEN", ""),
                    "Match": "token = " + os.environ.get("TOKEN", "")}], f)
    sys.exit(1)
if mode == "clean":
    with open(report, "w") as f:
        f.write("[]")
    sys.exit(0)
if mode.startswith("report:"):
    with open(report, "w") as f:
        f.write(mode[len("report:"):])
    sys.exit(0)
sys.stderr.write("FTL unable to load gitleaks config\n")
sys.exit(2 if mode == "crash" else 1)
"#;

/// TestScanUnpublished's setup: a repository whose base holds a.txt, pushed to a bare origin outside it, and a Host
/// with the fake gitleaks first on its PATH and the fake token in TOKEN.
struct Scan {
    repo: Repo,
    origin: String,
    bin: String,
    run_dir: String,
}

impl Scan {
    fn new() -> Scan {
        let repo = Repo::new(&[("a.txt", "a\n")]);
        let (origin, bin, run_dir) =
            (format!("{}/origin.git", repo.outside), format!("{}/bin", repo.outside), format!("{}/run", repo.outside));
        tool(&bin, "gitleaks", FAKE_GITLEAKS);
        let scan = Scan { repo: Repo { host: Scan::host(&bin, &[]), ..repo }, origin, bin, run_dir };
        scan.repo.host.git(&scan.repo.outside, &["init", "--quiet", "--bare", &scan.origin]).unwrap();
        scan.repo.git(&["remote", "add", "origin", &scan.origin]);
        scan.repo.git(&["push", "--quiet", "origin", "main"]);
        scan.mode("clean");
        scan
    }

    /// A Host with bin first on PATH, TOKEN the fake token, and each of set.
    fn host(bin: &str, set: &[(&str, &str)]) -> ProcessHost {
        let mut set = set.to_vec();
        set.push(("TOKEN", FAKE_TOKEN));
        ProcessHost::in_environment(environment(Some(bin), &set).into_iter())
    }

    fn mode(&self, mode: &str) {
        put(&format!("{}/mode", self.bin), mode);
    }

    fn seen(&self, name: &str) -> String {
        read(&format!("{}/{name}", self.bin))
    }

    fn argv(&self) -> Vec<String> {
        let argv = Json::parse(&self.seen("argv")).unwrap();
        argv.as_array().unwrap().iter().map(|a| a.as_str().unwrap().to_string()).collect()
    }

    fn push(&self, commit: &str) -> Result<(), PushError> {
        self.push_with(&self.repo.host, commit)
    }

    fn push_with(&self, host: &ProcessHost, commit: &str) -> Result<(), PushError> {
        let report = format!("{}/leaks-snapshot.json", self.run_dir);
        host.push_ref(&self.repo.path, commit, "ci-ref", self.repo.base(), &report)
    }

    /// The push's error, which must not be a LeaksFound.
    fn failure(&self, commit: &str) -> String {
        match self.push(commit) {
            Err(PushError::Failed(e)) => e.0,
            other => panic!("the push did not fail as unjudged: {other:?}"),
        }
    }

    /// The push's leaks.
    fn leaks(&self, commit: &str) -> Vec<Leak> {
        match self.push(commit) {
            Err(PushError::Leaks(found)) => found.leaks,
            other => panic!("the push found no leak: {other:?}"),
        }
    }

    fn on_origin(&self) -> Vec<String> {
        let refs = self.repo.host.git(&self.origin, &["for-each-ref", "--format=%(refname)"]).unwrap();
        refs.split_whitespace().map(String::from).collect()
    }

    fn pushed(&self) -> bool {
        self.on_origin().contains(&"refs/heads/ci-ref".to_string())
    }

    /// Make base a commit that adds files, pushed to origin.
    fn rules(&mut self, files: &[(&str, &str)]) {
        self.repo.base = Some(self.repo.commit(files, "rules"));
        self.repo.git(&["push", "--quiet", "--force", "origin", "main"]);
    }

    /// The file at path, from the top level of the repository, that gitleaks could read in its last run.
    fn seen_rule(&self, path: &str) -> String {
        read(&format!("{}/{path}", self.seen("cwd")))
    }
}

#[test]
fn a_clean_range_is_pushed_after_its_scan() {
    let s = Scan::new();
    let commit = s.repo.commit(&[("b.txt", "b\n")], "clean");
    s.push(&commit).unwrap();
    assert!(s.pushed());
    let argv = s.argv();
    assert!(argv.contains(&format!("--log-opts=--diff-merges=remerge --no-textconv {commit} --not --remotes=origin")));
    // Not the working tree: gitleaks would read its .gitleaksignore.
    assert_eq!(argv.last().unwrap(), &format!("{}/gitleaks-source", s.run_dir));
    assert!(argv.contains(&"--redact".to_string()));
}

#[test]
fn a_secret_blocks_the_push_and_stays_out_of_the_error() {
    let s = Scan::new();
    let commit = s.repo.commit(&[("cfg.py", &format!("token = '{FAKE_TOKEN}'\n"))], "leak");
    s.mode("leak");
    let Err(PushError::Leaks(found)) = s.push(&commit) else { panic!("no leak found") };
    let expected = Leak {
        rule: "github-pat".into(),
        file: "cfg.py".into(),
        line: 1,
        commit: commit.clone(),
        fingerprint: format!("{commit}:cfg.py:github-pat:1"),
    };
    assert_eq!(found.leaks, [expected]);
    let message = found.to_string();
    assert!(message.contains(&format!("`cfg.py:1` github-pat in {}", &commit[..12])), "{message}");
    // The fake report carries the secret unredacted; nothing built from it does.
    assert!(!format!("{message}{:?}", found.leaks).contains(FAKE_TOKEN));
    assert!(!s.pushed());
}

#[test]
fn the_change_s_own_gitleaks_files_do_not_judge_it() {
    let mut s = Scan::new();
    let commit = s.repo.commit(
        &[(".gitleaks.toml", "[allowlist]\npaths = ['cfg.py']\n"), (".gitleaksignore", "anything\n")],
        "allowlist itself",
    );
    s.push(&commit).unwrap();
    assert_eq!((s.seen("config-seen"), s.seen("ignore-seen")), ("[extend]\nuseDefault = true\n".into(), String::new()));
    // The base's own files are what judge a change.
    s.repo.git(&["reset", "--quiet", "--hard", s.repo.base()]);
    s.repo.base = Some(s.repo.commit(&[(".gitleaks.toml", "title = 'base rules'\n")], "rules"));
    s.repo.git(&["push", "--quiet", "origin", "main"]);
    let loosened = s.repo.commit(&[(".gitleaks.toml", "title = 'loosened'\n")], "loosen");
    s.push(&loosened).unwrap();
    assert_eq!(s.seen("config-seen"), "title = 'base rules'\n");
}

#[test]
fn a_file_the_base_s_config_extends_is_the_base_s_copy() {
    let mut s = Scan::new();
    s.rules(&[(".gitleaks.toml", "[extend]\npath = 'rules/extra.toml'\n"), ("rules/extra.toml", "title = 'base'\n")]);
    let commit = s.repo.commit(&[("rules/extra.toml", "[allowlist]\npaths = ['cfg.py']\n")], "allowlist itself");
    s.push(&commit).unwrap();
    let argv = s.argv();
    // gitleaks resolves an extended path against its working directory.
    let config = &argv[argv.iter().position(|a| a == "--config").unwrap() + 1];
    assert_eq!(&format!("{}/.gitleaks.toml", s.seen("cwd")), config);
    assert_eq!(s.seen_rule("rules/extra.toml"), "title = 'base'\n");
}

#[test]
fn a_chain_of_extends_is_copied_whole() {
    let mut s = Scan::new();
    s.rules(&[
        (".gitleaks.toml", "[extend]\npath = 'tmp/../one.toml'\n"),
        ("one.toml", "[Extend]\nPath = 'rules/two.toml'\n"),
        ("rules/two.toml", "title = 'two'\n"),
    ]);
    let edits = [("one.toml", "title = 'loosened'\n"), ("rules/two.toml", "title = 'loosened'\n")];
    let commit = s.repo.commit(&edits, "edit");
    s.push(&commit).unwrap();
    // gitleaks opens a path as it is spelled.
    assert_eq!(s.seen_rule("tmp/../one.toml"), "[Extend]\nPath = 'rules/two.toml'\n");
    assert_eq!(s.seen_rule("rules/two.toml"), "title = 'two'\n");
}

#[test]
fn an_extend_past_the_depth_gitleaks_reads_is_not_followed() {
    let mut s = Scan::new();
    s.rules(&[
        (".gitleaks.toml", "[extend]\npath = 'one.toml'\n"),
        ("one.toml", "[extend]\npath = 'two.toml'\n"),
        ("two.toml", "[extend]\npath = 'one.toml'\n"),
    ]);
    s.push(&s.repo.commit(&[("b.txt", "b\n")], "clean")).unwrap();
    assert_eq!(s.seen_rule("two.toml"), "[extend]\npath = 'one.toml'\n");
}

#[test]
fn an_extended_symbolic_link_blocks_the_push() {
    let mut s = Scan::new();
    std::os::unix::fs::symlink("one.toml", format!("{}/link.toml", s.repo.path)).unwrap();
    s.rules(&[(".gitleaks.toml", "[extend]\npath = 'link.toml'\n"), ("one.toml", "title = 'one'\n")]);
    let e = s.failure(&s.repo.commit(&[("b.txt", "b\n")], "clean"));
    assert!(e.starts_with("nothing was pushed: ") && e.contains(" its link.toml is a symbolic link"), "{e}");
    assert!(!s.pushed());
    // Nor is the base's own config followed through one; file_at, which the gate reads manifests with, still is.
    s.repo.git(&["rm", "--quiet", ".gitleaks.toml"]);
    std::os::unix::fs::symlink("one.toml", format!("{}/.gitleaks.toml", s.repo.path)).unwrap();
    s.rules(&[]);
    let e = s.failure(&s.repo.commit(&[("c.txt", "c\n")], "clean"));
    assert!(e.contains("its .gitleaks.toml is a symbolic link"), "{e}");
    assert!(!s.pushed());
    let linked = s.repo.host.file_at(&s.repo.path, s.repo.base(), ".gitleaks.toml").unwrap();
    assert_eq!(linked.as_deref(), Some(&b"one.toml"[..]));
}

#[test]
fn a_linked_gitleaksignore_in_the_base_blocks_the_push() {
    let mut s = Scan::new();
    // Its text would be the path it points to, which drops every fingerprint the base ignores.
    std::fs::create_dir_all(format!("{}/security", s.repo.path)).unwrap();
    std::os::unix::fs::symlink("security/leaks.ignore", format!("{}/.gitleaksignore", s.repo.path)).unwrap();
    s.rules(&[("security/leaks.ignore", "anything\n")]);
    let e = s.failure(&s.repo.commit(&[("b.txt", "b\n")], "clean"));
    assert!(e.contains("nothing was pushed") && e.contains(".gitleaksignore is a symbolic link"), "{e}");
    assert!(!s.pushed());
}

#[test]
fn an_extend_the_scan_cannot_take_from_the_base_blocks_the_push() {
    let mut s = Scan::new();
    let first = s.repo.base().to_string();
    // Each case: its name, the files of its base, and what the error names.
    type Case<'a> = (&'a str, &'a [(&'a str, &'a str)], &'a str);
    let cases: [Case; 12] = [
        ("absolute", &[(".gitleaks.toml", "[extend]\npath = '/etc/extra.toml'\n")], "/etc/extra.toml"),
        ("in other case", &[(".gitleaks.toml", "[EXTEND]\nPATH = '/etc/extra.toml'\n")], "/etc/extra.toml"),
        ("outside", &[(".gitleaks.toml", "[extend]\npath = 'rules/../../extra.toml'\n")], "../extra.toml"),
        ("missing", &[(".gitleaks.toml", "[extend]\npath = 'rules/gone.toml'\n")], "rules/gone.toml"),
        (
            "cycle",
            &[
                (".gitleaks.toml", "[extend]\npath = 'one.toml'\n"),
                ("one.toml", "[extend]\npath = './.gitleaks.toml'\n"),
            ],
            "one.toml -> .gitleaks.toml",
        ),
        ("pathspec magic", &[(".gitleaks.toml", "[extend]\npath = ':/a.txt'\n")], ":/a.txt"),
        ("not TOML", &[(".gitleaks.toml", "[extend]\npath = 'one.toml'\n"), ("one.toml", "[extend\n")], "one.toml"),
        ("NUL", &[(".gitleaks.toml", "[extend]\npath = \"a\\u0000.toml\"\n")], "a\\x00.toml"),
        ("top level", &[(".gitleaks.toml", "[extend]\npath = 'rules/..'\n")], "rules/.."),
        ("not a string", &[(".gitleaks.toml", "[extend]\npath = 1\n")], ".gitleaks.toml gives a path"),
        ("twice", &[(".gitleaks.toml", "[extend]\npath = 'a.toml'\nPath = 'b.toml'\n")], ".gitleaks.toml"),
        ("not UTF-8", &[(".gitleaks.toml", "[extend]\npath = 'latin.toml'\n")], "latin.toml is not UTF-8"),
    ];
    for (case, files, named) in cases {
        s.repo.git(&["reset", "--quiet", "--hard", &first]);
        if case == "not UTF-8" {
            std::fs::write(format!("{}/latin.toml", s.repo.path), b"title = '\xe9'\n").unwrap();
        }
        s.rules(files);
        let e = s.failure(&s.repo.commit(&[("b.txt", "b\n")], "clean"));
        assert!(e.contains(named) && e.contains("nothing was pushed"), "{case}: {e}");
        assert!(!s.pushed(), "{case}");
    }
}

#[test]
fn an_added_gitleaks_allow_is_a_leak() {
    let s = Scan::new();
    let allowed = s.repo.commit(&[("c.py", "x = 1\ny = 2  # gitleaks:allow\n")], "allow");
    let leaks = s.leaks(&allowed);
    let found: Vec<(&str, &str, u64, &str)> =
        leaks.iter().map(|l| (l.rule.as_str(), l.file.as_str(), l.line, l.commit.as_str())).collect();
    assert_eq!(found, [("gitleaks-allow", "c.py", 2, allowed.as_str())]);
    assert_eq!(leaks[0].fingerprint, format!("{allowed}:c.py:gitleaks-allow:2"));
    // One already on origin publishes nothing new.
    s.repo.git(&["push", "--quiet", "origin", "HEAD:main"]);
    s.push(&s.repo.commit(&[("d.txt", "d\n")], "after")).unwrap();
}

#[test]
fn an_added_line_that_looks_like_a_header_is_still_read() {
    let s = Scan::new();
    let commit = s.repo.commit(&[("notes.md", "a\n++ token # gitleaks:allow\n")], "header-like");
    let leaks: Vec<(String, u64)> = s.leaks(&commit).into_iter().map(|l| (l.file, l.line)).collect();
    assert_eq!(leaks, [("notes.md".to_string(), 2)]);
}

#[test]
fn a_merge_counts_what_it_adds_not_what_its_parents_had() {
    let s = Scan::new();
    let r = &s.repo;
    // Upstream, already on origin, adds a gitleaks:allow; merging it publishes nothing new.
    r.git(&["switch", "--quiet", "-c", "upstream"]);
    r.commit(&[("up.py", "x = 1  # gitleaks:allow\n")], "upstream");
    r.git(&["push", "--quiet", "origin", "upstream"]);
    r.git(&["switch", "--quiet", "main"]);
    r.commit(&[("mine.txt", "mine\n")], "mine");
    r.git(&["merge", "--quiet", "--no-edit", "upstream"]);
    s.push(strip(&r.git(&["rev-parse", "HEAD"]))).unwrap();
    // An edit made in a merge is the merge's own.
    r.git(&["switch", "--quiet", "-c", "side", "HEAD~1"]);
    r.commit(&[("side.txt", "side\n")], "side");
    r.git(&["switch", "--quiet", "main"]);
    r.git(&["merge", "--quiet", "--no-commit", "side"]);
    let merge = r.commit(&[("evil.py", "y = 2  # gitleaks:allow\n")], "merge side");
    let leaks: Vec<(String, String)> = s.leaks(&merge).into_iter().map(|l| (l.file, l.commit)).collect();
    assert_eq!(leaks, [("evil.py".to_string(), merge)]);
}

#[test]
fn an_octopus_merge_is_refused() {
    let s = Scan::new();
    let r = &s.repo;
    // git gives no remerge diff for three parents, so what such a merge adds cannot be scanned.
    for name in ["s1", "s2"] {
        r.git(&["switch", "--quiet", "-c", name, r.base()]);
        r.commit(&[(&format!("{name}.txt"), name)], name);
    }
    r.git(&["switch", "--quiet", "main"]);
    r.commit(&[("mine.txt", "mine\n")], "mine");
    r.git(&["merge", "--quiet", "--no-edit", "s1", "s2"]);
    let e = s.failure(strip(&r.git(&["rev-parse", "HEAD"])));
    assert!(e.contains("three or more parents"), "{e}");
    assert!(!s.pushed());
}

#[test]
fn the_change_s_gitattributes_do_not_hide_an_allow() {
    let s = Scan::new();
    let commit =
        s.repo.commit(&[(".gitattributes", "notes.md -diff\n"), ("notes.md", "x # gitleaks:allow\n")], "hidden");
    let leaks: Vec<(String, u64)> = s.leaks(&commit).into_iter().map(|l| (l.file, l.line)).collect();
    assert_eq!(leaks, [("notes.md".to_string(), 1)]);
}

#[test]
fn committed_gitattributes_do_not_hide_an_allow_whatever_git_s_config() {
    let s = Scan::new();
    // attr.tree=HEAD, or GIT_ATTR_SOURCE, makes git read the attributes a commit holds: the change's.
    s.repo.git(&["config", "attr.tree", "HEAD"]);
    let commit =
        s.repo.commit(&[(".gitattributes", "notes.md -diff\n"), ("notes.md", "x # gitleaks:allow\n")], "hidden");
    let host = Scan::host(&s.bin, &[("GIT_ATTR_SOURCE", &commit)]);
    let Err(PushError::Leaks(found)) = s.push_with(&host, &commit) else { panic!("no leak found") };
    let leaks: Vec<(String, u64)> = found.leaks.into_iter().map(|l| (l.file, l.line)).collect();
    assert_eq!(leaks, [("notes.md".to_string(), 1)]);
    // gitleaks gets the same git environment, which brings back no tree's attributes.
    let seen = s.seen("env");
    let env: Vec<(&str, &str)> = seen.lines().filter_map(|l| l.split_once('=')).collect();
    let empty_tree = strip(&s.repo.git(&["hash-object", "-t", "tree", "/dev/null"])).to_string();
    assert!(env.contains(&("GIT_CONFIG_VALUE_0", empty_tree.as_str())), "{seen}");
    let attributes_back = ["GIT_ATTR_SOURCE", "GIT_WORK_TREE", "GIT_CONFIG_PARAMETERS"];
    assert!(!env.iter().any(|(k, _)| attributes_back.contains(k)), "{seen}");
}

#[test]
fn neither_the_base_s_attributes_nor_git_c_hide_an_allow() {
    let mut s = Scan::new();
    // A -diff the base has for its own reasons is no allowlist; git -c exports GIT_CONFIG_PARAMETERS.
    s.repo.base = Some(s.repo.commit(&[(".gitattributes", "package-lock.json -diff\n")], "base attributes"));
    s.repo.git(&["push", "--quiet", "origin", "main"]);
    let commit = s.repo.commit(&[("package-lock.json", "{\"t\": \"x\"} // gitleaks:allow\n")], "lock");
    let host = Scan::host(&s.bin, &[("GIT_CONFIG_PARAMETERS", "'attr.tree'='HEAD'")]);
    let Err(PushError::Leaks(found)) = s.push_with(&host, &commit) else { panic!("no leak found") };
    assert_eq!(found.leaks.iter().map(|l| l.file.as_str()).collect::<Vec<_>>(), ["package-lock.json"]);
}

#[test]
fn a_nul_inside_a_line_does_not_end_the_commit_s_patch() {
    let s = Scan::new();
    // git still diffs as text a file whose first 8000 bytes hold no NUL.
    let text = format!("{}\na\0b\ny = 2  # gitleaks:allow\n", "x".repeat(9000));
    let commit = s.repo.commit(&[("n.txt", &text)], "nul");
    let leaks: Vec<(String, u64)> = s.leaks(&commit).into_iter().map(|l| (l.file, l.line)).collect();
    assert_eq!(leaks, [("n.txt".to_string(), 3)]);
}

#[test]
fn a_base_git_cannot_read_blocks_the_push() {
    let mut s = Scan::new();
    let commit = s.repo.commit(&[("b.txt", "b\n")], "clean");
    s.repo.base = Some("0".repeat(40));
    s.failure(&commit);
    assert!(!s.pushed());
}

#[test]
fn a_gitleaks_that_cannot_judge_blocks_the_push() {
    let s = Scan::new();
    let commit = s.repo.commit(&[("b.txt", "b\n")], "clean");
    for (mode, status) in [
        ("crash", "status 2: FTL unable to load gitleaks config"),
        ("bad-config", "status 1 and wrote no report"),
        ("report:{}", "status 0 and wrote no finding"),
        ("report:[{\"RuleID\": 1}]", "cannot be read"),
        ("report:[{\"File\": \"f\"}]", "cannot be read: 'RuleID'"),
        (
            "report:[{\"RuleID\": \"r\", \"File\": \"f\", \"StartLine\": \"x\", \"Commit\": \"c\", \
             \"Fingerprint\": \"p\"}]",
            "cannot be read",
        ),
        ("report:not json", "cannot be read"),
    ] {
        s.mode(mode);
        let e = s.failure(&commit);
        assert!(e.contains(status) && e.contains("nothing was pushed"), "{mode}: {e}");
        assert!(!s.pushed(), "{mode}");
    }
    // A finding's line may come as a number of another kind.
    let found = "{\"RuleID\": \"r\", \"File\": \"f\", \"Commit\": \"c\", \"Fingerprint\": \"p\", \"StartLine\": ";
    s.mode(&format!("report:[{found}\"7\"}}, {found}8.0}}]"));
    let lines: Vec<u64> = s.leaks(&commit).into_iter().map(|l| l.line).collect();
    assert_eq!(lines, [7, 8]);
}

#[test]
fn a_report_left_by_an_earlier_scan_is_not_read() {
    let s = Scan::new();
    let commit = s.repo.commit(&[("b.txt", "b\n")], "clean");
    s.push(&commit).unwrap();
    s.mode("bad-config");
    s.failure(&commit);
}

#[test]
fn the_preflight_needs_gitleaks_git() {
    let s = Scan::new();
    s.repo.host.check_gitleaks().unwrap();
    s.mode("old");
    let e = s.repo.host.check_gitleaks().unwrap_err();
    assert!(e.0.contains("`gitleaks git` does not run: install gitleaks 8.19"), "{e}");
    // No gitleaks at all.
    let nowhere = ProcessHost::in_environment(environment(None, &[("PATH", &s.run_dir)]).into_iter());
    let e = nowhere.check_gitleaks().unwrap_err();
    assert!(e.0.contains("gitleaks cannot be started (") && e.0.contains("install gitleaks 8.19"), "{e}");
}

#[test]
fn push_branch_scans_the_branch_s_commit_and_tracks_it() {
    let s = Scan::new();
    s.repo.git(&["switch", "--quiet", "-c", "orchestrator/x-a1b2c3"]);
    let commit = s.repo.commit(&[("b.txt", "b\n")], "clean");
    let report = format!("{}/leaks.json", s.run_dir);
    s.repo.host.push_branch(&s.repo.path, "orchestrator/x-a1b2c3", s.repo.base(), &report).unwrap();
    assert!(s.argv().iter().any(|a| a.contains(&commit)));
    assert!(s.on_origin().contains(&"refs/heads/orchestrator/x-a1b2c3".to_string()));
    assert_eq!(strip(&s.repo.git(&["rev-parse", "--abbrev-ref", "@{upstream}"])), "origin/orchestrator/x-a1b2c3");
    // A secret in the branch's commit stops it.
    let leak = s.repo.commit(&[("c.py", "x = 1  # gitleaks:allow\n")], "allow");
    let pushed = s.repo.host.push_branch(&s.repo.path, "orchestrator/x-a1b2c3", s.repo.base(), &report);
    let Err(PushError::Leaks(found)) = pushed else { panic!("no leak found") };
    assert_eq!(found.leaks[0].commit, leak);
    assert!(OrchestratorError::from(PushError::Leaks(found)).0.starts_with("gitleaks found 1 secret in"));
}

// -- The scan's helpers against the Python engine ---------------------------------------------------------------------

fn recorded(kind: &str) -> Json {
    oracle("host.json").get(kind).cloned().unwrap_or_else(|| panic!("host.json has no {kind}"))
}

#[test]
fn an_extended_file_is_found_as_the_python_engine_finds_it() {
    for case in recorded("extended_file").as_array().unwrap() {
        let case = case.as_array().unwrap();
        let config = case[0].as_str().unwrap();
        let ours = extended_file(config);
        match (case[1].get("ok"), case[1].get("error").and_then(Json::as_str)) {
            (Some(ok), _) => assert_eq!(ours, Ok(ok.as_str().map(String::from)), "{config:?}"),
            // The TOML parser's own words differ from tomllib's; the rest is the Python engine's.
            (_, Some(error)) if error.starts_with("is not TOML: ") => {
                assert!(ours.unwrap_err().starts_with("is not TOML: "), "{config:?}");
            }
            (_, error) => assert_eq!(ours, Err(error.unwrap().to_string()), "{config:?}"),
        }
    }
}

#[test]
fn a_leak_is_worded_as_the_python_engine_words_it() {
    let leaks = [
        Leak {
            rule: "github-pat".into(),
            file: "cfg.py".into(),
            line: 1,
            commit: format!("{}c0ff", "c0ffee".repeat(6)),
            fingerprint: "c0ffee:cfg.py:github-pat:1".into(),
        },
        Leak {
            rule: "gitleaks-allow".into(),
            file: "a b.py".into(),
            line: 12,
            commit: "ab".repeat(20),
            fingerprint: "x:a b.py:gitleaks-allow:12".into(),
        },
    ];
    let words = recorded("leaks");
    let words = words.as_array().unwrap();
    let each: Vec<String> = leaks.iter().map(Leak::to_string).collect();
    let recorded_each: Vec<&str> = words[0].as_array().unwrap().iter().filter_map(Json::as_str).collect();
    assert_eq!(each, recorded_each);
    let found = |leaks: &[Leak]| LeaksFound { leaks: leaks.to_vec(), report_path: "/r/leaks.json".into() }.to_string();
    assert_eq!(found(&leaks), words[1].as_str().unwrap());
    assert_eq!(found(&leaks[..1]), words[2].as_str().unwrap());
    assert_eq!(
        base_rules_refused(&"ab".repeat(20), ".gitleaks.toml is a symbolic link").0,
        recorded("base_rules_refused").as_str().unwrap()
    );
}

#[test]
fn normpath_and_shortened_are_python_s() {
    for case in recorded("normpath").as_array().unwrap() {
        let case = case.as_array().unwrap();
        assert_eq!(normpath(case[0].as_str().unwrap()), case[1].as_str().unwrap(), "{:?}", case[0]);
    }
    for case in recorded("shortened").as_array().unwrap() {
        let case = case.as_array().unwrap();
        assert_eq!(shortened(case[0].as_str().unwrap(), 160), case[1].as_str().unwrap());
    }
}

#[test]
fn bytes_split_as_python_splits_them() {
    assert_eq!(split_bytes(b"\n\0a\n\0b", b"\n\0"), [&b""[..], b"a", b"b"]);
    assert_eq!(split_bytes(b"", b"\n\0"), [&b""[..]]);
    assert!(contains(b"x gitleaks:allow", b"gitleaks:allow") && !contains(b"gitleaks", b"gitleaks:allow"));
}
