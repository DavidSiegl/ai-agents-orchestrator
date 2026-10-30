//! tests/test_orchestrator.py's TestJudgedInQuality, ported case for case: `judged-in-quality` through main's
//! herdrless_command, in a repository with a real bare origin, with the quality job on a FakeJenkins.
//!
//! The repository's HEAD is a pull request's head, one commit on top of main's base commit, as a run publishes a tree
//! its last quality build judged.

use std::cell::RefCell;
use std::ffi::OsString;
use std::sync::Arc;

use crate::ci::fake::{credentials, FakeJenkins, Outcome, JENKINS, JENKINS_TOKEN, JOB, JOB_PATH};
use crate::cli::{ended_status, herdrless_command, parse_args, Local};
use crate::constants::EXIT_ERROR;
use crate::env::ConfigDir;
use crate::error::{os_error, OrchestratorError};
use crate::host::tests::Repo;
use crate::host::Host;
use crate::http::{Http, HttpError, Request, Response};
use crate::interrupt::Interrupt;
use crate::py::json::Map;
use crate::py::text::strip;

/// Python's JUDGING: the files whose change decides the judging, as the test knows them.
const JUDGING: [&str; 6] = ["Jenkinsfile", "gate.py", "orchestrator.py", "pyproject.toml", "uv.lock", ".python-version"];
/// The GIT_REF the builds were of; judged-in-quality never reads it.
const QUALITY_REF: &str = "orchestrator-ci/r-1-q1";

/// The repository, its origin, the pull request's head and its tree, and the quality job.
struct Judging {
    repo: Repo,
    origin: String,
    head: String,
    tree: String,
    fake: Arc<FakeJenkins>,
}

/// What one judged-in-quality printed and how it ended.
struct Judged {
    status: i32,
    out: String,
    err: String,
}

impl Judging {
    fn new() -> Judging {
        let files: Vec<(String, String)> =
            JUDGING.iter().chain(&["f.py"]).map(|name| (name.to_string(), format!("{name} 1\n"))).collect();
        let files: Vec<(&str, &str)> = files.iter().map(|(n, t)| (n.as_str(), t.as_str())).collect();
        let repo = Repo::new(&files);
        let origin = format!("{}/origin.git", repo.outside);
        repo.host.git(&repo.outside, &["init", "--quiet", "--bare", "-b", "main", &origin]).unwrap();
        // As GitHub does, so a commit is fetched by its sha.
        repo.host.git(&origin, &["config", "uploadpack.allowReachableSHA1InWant", "true"]).unwrap();
        repo.git(&["remote", "add", "origin", &origin]);
        repo.git(&["push", "--quiet", "origin", "HEAD:refs/heads/main"]);
        repo.git(&["checkout", "--quiet", "-b", "pr"]);
        let head = repo.commit(&[("f.py", "f.py 2\n")], "change");
        let tree = strip(&repo.git(&["rev-parse", "HEAD^{tree}"])).to_string();
        Judging { repo, origin, head, tree, fake: FakeJenkins::new() }
    }

    fn base(&self) -> String {
        self.repo.base().to_string()
    }

    /// The description of a build of HEAD's tree from its base by main at the base.
    fn description(&self) -> String {
        let base = self.base();
        format!("judged tree={} parent={base} main={base}", self.tree)
    }

    /// A build of the quality job with description, ending with result after polls; its number.
    fn add(&self, description: Option<String>, result: &str, polls: u32) -> i64 {
        let outcome = Outcome { result: result.to_string(), polls, description, ..Outcome::gate("OK", &[]) };
        self.fake.state().add_build(QUALITY_REF, outcome)
    }

    /// A SUCCESS build of description; its number.
    fn build(&self, description: String) -> i64 {
        self.add(Some(description), "SUCCESS", 0)
    }

    /// A build of HEAD's tree from its base by main at the base, ending with result; its number.
    fn build_ending(&self, result: &str) -> i64 {
        self.add(Some(self.description()), result, 0)
    }

    /// Move origin's main by a commit that changes name, made in another clone so this one lacks it; its sha.
    fn push_main(&self, name: &str, text: &str) -> String {
        let other = format!("{}/other", self.repo.outside);
        let host = &self.repo.host;
        if !std::path::Path::new(&other).is_dir() {
            host.git(&self.repo.outside, &["clone", "--quiet", "-b", "main", &self.origin, &other]).unwrap();
        }
        crate::host::tests::put(&format!("{other}/{name}"), text);
        host.git(&other, &["add", "-A"]).unwrap();
        let identity = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"];
        host.git(&other, &[&identity[..], &["commit", "--quiet", "-m", "main moves"]].concat()).unwrap();
        host.git(&other, &["push", "--quiet", "origin", "HEAD:refs/heads/main"]).unwrap();
        strip(&host.git(&other, &["rev-parse", "HEAD"]).unwrap()).to_string()
    }

    /// judged-in-quality with args in place of `--quality-gate JOB --target main`, in the repository's top level.
    fn judged(&self, args: &[&str]) -> Judged {
        self.judged_with(args, &self.repo.path, &credentials(), Arc::clone(&self.fake) as Arc<dyn Http>)
    }

    /// judged, in the current directory cwd gives, with these credentials, Jenkins answering through http.
    fn judged_with(&self, args: &[&str], cwd: &str, creds: &Map<String>, http: Arc<dyn Http>) -> Judged {
        let cwd = Ok(cwd.to_string());
        self.judged_in(args, &|| cwd.clone(), creds, http)
    }

    /// judged, the current directory being what cwd gives.
    fn judged_in(
        &self,
        args: &[&str],
        cwd: &dyn Fn() -> Result<String, OrchestratorError>,
        creds: &Map<String>,
        http: Arc<dyn Http>,
    ) -> Judged {
        let default = ["--quality-gate", JOB, "--target", "main"];
        let argv: Vec<&str> = std::iter::once("judged-in-quality").chain(if args.is_empty() { &default[..] } else { args }.iter().copied()).collect();
        let config = ConfigDir::at("/nowhere");
        let command = parse_args(argv.iter().map(OsString::from), &config).expect("a command line parse_args takes");
        let (out, err) = (RefCell::new(String::new()), RefCell::new(String::new()));
        let jenkins = |job: &str| {
            assert_eq!(job, JOB);
            let fake_path = "/home/u/.config/ai-agents-orchestrator/ci.env";
            Ok(crate::ci::Jenkins::new(job, creds, fake_path, Arc::clone(&http)))
        };
        let interrupt = Interrupt::new();
        let (stdout, stderr) = (|t: &str| out.borrow_mut().push_str(t), |t: &str| err.borrow_mut().push_str(t));
        let herdr = |_: Option<&str>| -> crate::herdr::ProcessHerdr { panic!("judged-in-quality asked for herdr") };
        let local = Local {
            host: &self.repo.host,
            herdr: &herdr,
            in_herdr_pane: false,
            jenkins: &jenkins,
            cwd,
            config: &config,
            stdout: &stdout,
            stderr: &stderr,
            interrupt: &interrupt,
        };
        let ended = herdrless_command(&command, &local).expect("judged-in-quality needs no herdr");
        let status = ended_status(ended, &command, &stdout);
        Judged { status, out: out.into_inner(), err: err.into_inner() }
    }

    /// The error a judged-in-quality with args refuses with, holding each of wanted, as Python's assert_refused.
    fn refused(&self, args: &[&str], wanted: &[&str]) -> String {
        let j = self.judged(args);
        assert_refused(j, wanted)
    }
}

/// The error j printed, once it is a refusal on one line naming each of wanted, no token in it.
fn assert_refused(j: Judged, wanted: &[&str]) -> String {
    assert_eq!(j.status, EXIT_ERROR, "{}", j.err);
    assert_eq!(j.out, "");
    assert_eq!(j.err.matches('\n').count(), 1, "{}", j.err);
    assert!(j.err.ends_with('\n') && j.err.starts_with("judged-in-quality: "), "{}", j.err);
    let mut at = 0;
    for w in wanted {
        let found = j.err[at..].find(w).unwrap_or_else(|| panic!("{w:?} not in order in {:?}", j.err));
        at += found + w.len();
    }
    assert!(!j.err.contains(JENKINS_TOKEN), "{}", j.err);
    j.err
}

/// The refs of the repository's remote-tracking branches.
fn tracking(t: &Judging) -> String {
    t.repo.git(&["for-each-ref", "--format=%(refname)", "refs/remotes/"])
}

#[test]
fn a_successful_build_of_head_s_tree_from_its_merge_base_trusts_it() {
    let t = Judging::new();
    let n = t.build(t.description());
    let j = t.judged(&[]);
    assert_eq!((j.status, j.err.as_str()), (0, ""));
    assert_eq!(j.out, format!("judged-in-quality: build #{n} of {JOB} judged HEAD's tree from its merge base with main\n"));
    assert!(t.fake.state().names.contains(&"builds".to_string()));
}

#[test]
fn an_older_build_trusts_it_behind_newer_ones_that_do_not() {
    let t = Judging::new();
    let n = t.build(t.description());
    let base = t.base();
    t.build(format!("judged tree={} parent={base} main={base}", "1".repeat(40)));
    t.build_ending("UNSTABLE");
    let j = t.judged(&[]);
    assert_eq!(j.status, 0, "{}", j.err);
    assert!(j.out.contains(&format!("#{n} ")), "{}", j.out);
}

#[test]
fn a_build_that_did_not_succeed_does_not() {
    for result in ["UNSTABLE", "FAILURE", "ABORTED"] {
        let t = Judging::new();
        let n = t.build_ending(result);
        t.refused(&[], &[&format!("#{n}"), result]);
    }
}

#[test]
fn a_build_still_running_does_not() {
    let t = Judging::new();
    let n = t.add(Some(t.description()), "SUCCESS", 1);
    t.refused(&[], &[&format!("#{n}"), "still running"]);
}

#[test]
fn a_build_of_another_tree_does_not() {
    let t = Judging::new();
    let base = t.base();
    let other = "1".repeat(40);
    let n = t.build(format!("judged tree={other} parent={base} main={base}"));
    t.refused(&[], &[&format!("#{n}"), &other, &t.tree]);
}

#[test]
fn a_build_from_another_base_does_not() {
    let t = Judging::new();
    let base = t.base();
    let n = t.build(format!("judged tree={} parent={} main={base}", t.tree, t.head));
    t.refused(&[], &[&format!("#{n}"), &t.head, &base]);
}

#[test]
fn a_change_to_a_file_that_decides_the_judging_is_judged_in_full() {
    for name in JUDGING {
        let mut t = Judging::new();
        t.repo.put(name, "changed\n");
        t.repo.commit(&[], &format!("edit {name}"));
        t.tree = strip(&t.repo.git(&["rev-parse", "HEAD^{tree}"])).to_string();
        t.build(t.description());
        t.refused(&[], &[name]);
        assert!(!t.fake.state().names.contains(&"builds".to_string()), "{name}: Jenkins was asked");
    }
}

#[test]
fn the_judging_files_are_compared_whole_from_a_subdirectory_under_diff_relative() {
    let mut t = Judging::new();
    t.repo.git(&["config", "diff.relative", "true"]);
    t.repo.put("sub/g.py", "g\n");
    t.repo.put("Jenkinsfile", "changed\n");
    t.repo.commit(&[], "edit Jenkinsfile");
    t.tree = strip(&t.repo.git(&["rev-parse", "HEAD^{tree}"])).to_string();
    t.build(t.description());
    let sub = format!("{}/sub", t.repo.path);
    let j = t.judged_with(&[], &sub, &credentials(), Arc::clone(&t.fake) as Arc<dyn Http>);
    assert_refused(j, &["Jenkinsfile"]);
}

#[test]
fn a_build_by_a_main_with_other_judging_files_does_not() {
    let t = Judging::new();
    let main = t.push_main("orchestrator.py", "a newer gate\n");
    let base = t.base();
    let n = t.build(format!("judged tree={} parent={base} main={main}", t.tree));
    t.refused(&[], &[&format!("#{n}"), &main, "orchestrator.py"]);
}

#[test]
fn a_build_by_a_main_that_moved_without_touching_them_trusts_it() {
    let t = Judging::new();
    let main = t.push_main("other.py", "x\n");
    let base = t.base();
    // The fetch of the target leaves out a main this clone lacks: a pull request into another branch.
    t.repo.git(&["push", "--quiet", "origin", &format!("{base}:refs/heads/release")]);
    let n = t.build(format!("judged tree={} parent={base} main={main}", t.tree));
    let j = t.judged(&["--quality-gate", JOB, "--target", "release"]);
    assert_eq!((j.status, j.err.as_str()), (0, ""));
    assert!(j.out.contains(&format!("#{n} ")), "{}", j.out);
}

#[test]
fn a_main_commit_that_cannot_be_fetched_fails() {
    let t = Judging::new();
    let base = t.base();
    let main = "2".repeat(40);
    t.build(format!("judged tree={} parent={base} main={main}", t.tree));
    t.refused(&[], &[&main]);
}

#[test]
fn a_build_without_the_description_does_not() {
    let t = Judging::new();
    let (tree, base) = (t.tree.clone(), t.base());
    let descriptions = [
        String::new(),
        format!("judged tree={tree} parent={base}"),
        format!("judged tree={tree} parent={base} main={base} extra"),
        format!("judged tree={} parent={base} main={base}", tree.to_uppercase()),
        format!("judged  tree={tree} parent={base} main={base}"),
        format!("judged tree={} parent={base} main={base}", &tree[..12]),
        format!("judged tree={JENKINS_TOKEN}"),
    ];
    for description in descriptions {
        t.fake.state().builds.clear();
        let n = t.build(description.clone());
        t.refused(&[], &[&format!("#{n}"), "description"]);
    }
}

#[test]
fn a_long_description_is_shortened_with_its_token_masked_whole() {
    let t = Judging::new();
    // 150 characters before the token, so a cut at 160 would split it.
    t.build(format!("{}{JENKINS_TOKEN}{}", "y".repeat(150), "z".repeat(500)));
    let err = t.refused(&[], &["description"]);
    assert!(!err.contains(&JENKINS_TOKEN[..6]), "{err}");
    assert!(err.contains("****"), "{err}");
    assert!(err.len() < 1000, "{err}");
}

#[test]
fn a_build_with_no_description_at_all_does_not() {
    let t = Judging::new();
    let n = t.add(None, "SUCCESS", 0);
    t.refused(&[], &[&format!("#{n}"), "description", "None"]);
}

#[test]
fn the_closest_build_is_the_one_named() {
    let t = Judging::new();
    let n = t.build_ending("UNSTABLE");
    let base = t.base();
    t.build(format!("judged tree={} parent={base} main={base}", "1".repeat(40)));
    t.build(String::new());
    t.refused(&[], &[&format!("#{n}"), "UNSTABLE"]);
}

#[test]
fn a_job_without_builds_fails() {
    Judging::new().refused(&[], &["no builds"]);
}

#[test]
fn jenkins_down_fails_with_its_token_masked() {
    let t = Judging::new();
    t.build(t.description());
    t.fake.state().down = true;
    t.refused(&[], &["Connection refused"]);
}

#[test]
fn an_http_error_fails() {
    let t = Judging::new();
    t.build(t.description());
    for code in [403, 404, 500] {
        t.fake.state().fail = vec![(("GET", format!("{JOB_PATH}api/json")), code)];
        t.refused(&[], &[&format!("HTTP {code}")]);
    }
}

/// Jenkins answering every request with one body, as Python's test patches FakeCI.jenkins.
struct Answering(String);

impl Http for Answering {
    fn send(&self, _request: &Request) -> Result<Response, HttpError> {
        Ok(Response::ok(self.0.clone()))
    }
}

#[test]
fn an_answer_that_is_not_the_job_s_builds_fails() {
    let t = Judging::new();
    for answer in [r#"{"builds": "x"}"#, r#"{"builds": ["x"]}"#, r#"{"builds": [{"number": "7", "result": "SUCCESS"}]}"#] {
        let j = t.judged_with(&[], &t.repo.path, &credentials(), Arc::new(Answering(answer.to_string())));
        assert_refused(j, &["api/json"]);
    }
}

#[test]
fn missing_credentials_fail_before_any_request() {
    let t = Judging::new();
    t.build(t.description());
    let only_url: Map<String> = [("JENKINS_URL", format!("{JENKINS}/"))].into_iter().collect();
    let j = t.judged_with(&[], &t.repo.path, &only_url, Arc::clone(&t.fake) as Arc<dyn Http>);
    assert_refused(j, &["JENKINS_USER"]);
    assert!(t.fake.state().requests.is_empty());
}

#[test]
fn a_target_branch_origin_lacks_fails() {
    let t = Judging::new();
    t.build(t.description());
    let j = t.judged(&["--quality-gate", JOB, "--target", "nowhere"]);
    assert_ne!(j.status, 0);
    assert_eq!(j.err.matches('\n').count(), 1, "{}", j.err);
    assert!(j.err.contains("nowhere"), "{}", j.err);
}

#[test]
fn a_target_that_is_not_a_branch_name_fails_before_git_fetches_it() {
    let t = Judging::new();
    t.build(t.description());
    let before = tracking(&t);
    for target in ["main:refs/heads/x", "-q", "a..b", "x.lock"] {
        let flag = format!("--target={target}");
        let j = t.judged(&["--quality-gate", JOB, &flag]);
        assert_ne!(j.status, 0);
        assert_eq!(j.err.matches('\n').count(), 1, "{}", j.err);
        assert!(j.err.contains(&format!("'{target}' is not a branch name")), "{}", j.err);
        assert_eq!(tracking(&t), before, "{target}: git fetched");
    }
    assert!(t.fake.state().requests.is_empty());
}

#[test]
fn a_working_directory_that_is_gone_fails_in_one_line() {
    let t = Judging::new();
    t.build(t.description());
    // What crate::cli::current_dir gives once the directory is gone: getcwd fails with ENOENT.
    let gone = || Err(OrchestratorError(os_error(&std::io::Error::from_raw_os_error(2), None)));
    let j = t.judged_in(&[], &gone, &credentials(), Arc::clone(&t.fake) as Arc<dyn Http>);
    let err = assert_refused(j, &["No such file or directory"]);
    assert_eq!(err, "judged-in-quality: [Errno 2] No such file or directory\n");
}

#[test]
fn without_a_merge_base_it_fails() {
    let mut t = Judging::new();
    t.repo.git(&["checkout", "--quiet", "--orphan", "lone"]);
    t.repo.put("f.py", "alone\n");
    t.repo.commit(&[], "lone");
    t.tree = strip(&t.repo.git(&["rev-parse", "HEAD^{tree}"])).to_string();
    t.build(t.description());
    t.refused(&[], &["merge base"]);
}

#[test]
fn a_description_reads_only_in_its_format() {
    let (a, b, c) = ("a".repeat(40), "b".repeat(64), "c".repeat(40));
    let description = format!("judged tree={a} parent={b} main={c}");
    let read = super::Judged::read(&description).unwrap();
    assert_eq!((read.tree, read.parent, read.main), (a.as_str(), b.as_str(), c.as_str()));
    for bad in [format!("judged tree={} parent={b} main={c}", "a".repeat(50)), format!("judged tree={a} parent={b} main={c}\n")] {
        assert_eq!(super::Judged::read(&bad), None, "{bad}");
    }
}

#[test]
fn of_builds_that_meet_as_many_conditions_the_newest_is_named() {
    // As Python's max keeps the first of equals, the builds coming newest first.
    let t = Judging::new();
    t.build_ending("UNSTABLE");
    let n = t.build_ending("FAILURE");
    t.refused(&[], &[&format!("#{n}"), "FAILURE"]);
}
