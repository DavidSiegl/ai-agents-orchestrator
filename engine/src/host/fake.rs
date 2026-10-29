//! FakeHost, the machine the Workflow tests run on: an in-memory filesystem and git checkout, played as
//! tests/test_orchestrator.py's FakeHost plays them.

use std::cell::{RefCell, RefMut};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::rc::Rc;

use super::{ChangedFile, Completed, Exec, Host};
use crate::quality::Leak;
use crate::constants::WORKTREES_DIR;
use crate::error::OrchestratorError;
use crate::host::tests::FAKE_TOKEN;
use crate::py::json::{Json, Map};

/// What a FakeHost's clock reads when the workflow's clock reads 0: far from it, so a time taken from the wrong clock
/// shows.
pub(crate) const HOST_EPOCH: i64 = 1_791_000_000;
/// FakeState::leaks' stand-in for the commit a scan is of.
pub(crate) const SCANNED: &str = "scanned";

/// An in-memory filesystem and git checkout standing in for the machine the agents run on.
///
/// Files written outside .orchestrator/ count as uncommitted changes until a commit. Git is played at the level of
/// single commands (Host::git_run), so Host's own fetch, fast-forward and merge steps run on it; a process of any other
/// kind panics the test. A test scripts it through [`FakeHost::state`], whose fields say what each scripts.
pub(crate) struct FakeHost {
    state: RefCell<FakeState>,
}

/// A pull request as `gh pr view` answers for it.
#[derive(Clone, Debug)]
pub(crate) enum FakePr {
    /// What gh prints: Host's two views print the whole object.
    Prints(Json),
    /// gh fails so.
    Fails(Completed),
}

/// A pull request create_pr opened.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct OpenedPr {
    pub base: String,
    pub head: String,
    pub title: String,
    pub body: String,
    pub draft: bool,
}

/// An edit of a pull request: its body, or out of draft.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum PrEdit {
    Body { url: String, body: String },
    Ready { url: String },
}

/// What a FakeHost holds and records. Each field is what Python's FakeHost has of that name.
pub(crate) struct FakeState {
    /// Each file's text, by path, in the order first written.
    pub files: Map<String>,
    /// The commit HEAD is at; None outside a git repository with a commit.
    pub head: Option<String>,
    /// Every path written, in order.
    pub writes: Vec<String>,
    /// The checked-out branch of /proj; "HEAD" for a detached one.
    pub branch: String,
    /// The paths of the uncommitted changes.
    pub changed: BTreeSet<String>,
    /// The arguments of each git command, in order.
    pub git_calls: Vec<Vec<String>>,
    /// The directory and arguments of each git command, in order.
    pub git_log: Vec<(String, Vec<String>)>,
    /// The pull requests create_pr opened.
    pub prs: Vec<OpenedPr>,
    /// The commit origin's branch is at when HEAD lacks it; None when HEAD has it all.
    pub upstream: Option<String>,
    /// The stderr of a failing fetch, e.g. offline or no such branch on origin.
    pub fetch_error: Option<String>,
    /// The local branch and origin's each have commits the other lacks.
    pub diverged: bool,
    /// The files a merge of upstream conflicts in.
    pub conflicts: Vec<String>,
    /// A merge is in progress, as one that conflicted and was never aborted leaves it.
    pub merge_head: bool,
    /// (parent, message) of each snapshot, whose commit is snap<n>.
    pub snapshots: Vec<(String, String)>,
    /// origin's branches, by name, with their commit: those `push --force` created, which `ls-remote` lists.
    pub remote: Map<String>,
    /// What `git diff -U0` from the base to a snapshot prints.
    pub diff: String,
    /// The checkout has an origin at all.
    pub has_origin: bool,
    /// The seconds since each run's state.json was written, by run id; 0 for one not listed.
    pub ages: HashMap<String, u64>,
    /// For prune: the local branches besides the checked-out one, by name, with their commit.
    pub branches: Map<String>,
    /// The branches origin/<name> exists for.
    pub tracking: BTreeSet<String>,
    /// The linked worktrees' branches, by path, "HEAD" for a detached one; /proj has `branch` checked out. git in a
    /// worktree sees its own branch, and the changed files under its path as its own changes.
    pub worktrees: Map<String>,
    /// (commit, of) pairs where commit is an ancestor of of.
    pub ancestors: HashSet<(String, String)>,
    /// By branch, the commits it has beyond the commit rev-list --count compares it to.
    pub ahead: HashMap<String, u64>,
    /// Commits this clone lacks.
    pub missing_commits: HashSet<String>,
    /// What `gh pr view` answers, by URL.
    pub pull_requests: HashMap<String, FakePr>,
    /// Each edit of a pull request, in order.
    pub pr_edits: Vec<PrEdit>,
    /// The stderr of a failing `git worktree remove`.
    pub remove_error: Option<String>,
    /// The arguments of each gh command, in order.
    pub gh_calls: Vec<Vec<String>>,
    /// Every path written with keep_mtime.
    pub mtime_kept: Vec<String>,
    /// The workflow's clock, in seconds; the host's own time is HOST_EPOCH later: now() reads it, and write() stamps
    /// each file's mtime with it. None reads 0.
    pub clock: Option<Rc<dyn Fn() -> f64>>,
    /// Each file's modification time, by path; a file put straight into files has none, so mtime() gives now.
    pub mtimes: HashMap<String, i64>,
    /// What scan_unpublished finds: every scan finds them all, a Leak whose commit is SCANNED in the commit scanned.
    pub leaks: Vec<Leak>,
    /// (commit, base, report path) of each scan.
    pub scans: Vec<(String, String, String)>,
    /// check_gitleaks fails as on a machine without gitleaks.
    pub gitleaks_missing: bool,
    /// The text of each file in a commit, by (commit, path); file_at reads it, and records each read in file_reads.
    pub committed: HashMap<(String, String), String>,
    /// (commit, path) of each file_at.
    pub file_reads: Vec<(String, String)>,
    /// What changed_line_counts gives; None gives each file added_lines finds in diff, under its own path, with the
    /// lines it adds and none removed.
    pub line_counts: Option<Vec<ChangedFile>>,
    /// By snapshot: the text of each file changed then, by path, None for a deleted one; lock, locked_changes and
    /// restore play the locked files on it.
    pub trees: HashMap<String, BTreeMap<String, Option<String>>>,
    /// The local refs lock made, with their snapshot.
    pub refs: Map<String>,
    /// (ref, paths) of each restore.
    pub restores: Vec<(String, Vec<String>)>,
    /// What session_log reads, by (harness, session).
    pub session_logs: HashMap<(String, String), String>,
    /// Run before each write, given its path, when set: an error fails the write, as a Python test's own
    /// FakeHost.write fails it.
    pub before_write: Option<BeforeWrite>,
    /// What run_states answers instead, when set and it gives Some: a Python test's own FakeHost.run_states.
    pub run_states_with: Option<RunStatesWith>,
}

/// A test's hook on writes (FakeState::before_write).
pub(crate) type BeforeWrite = Box<dyn FnMut(&str) -> Result<(), OrchestratorError>>;
/// A test's own run_states (FakeState::run_states_with).
pub(crate) type RunStatesWith = Box<dyn FnMut(&str) -> Option<Result<Vec<(u64, Json)>, OrchestratorError>>>;

impl FakeHost {
    /// A checkout of /proj at commit head (None: no repository with a commit), on branch.
    pub fn new(head: Option<&str>, branch: &str) -> FakeHost {
        FakeHost {
            state: RefCell::new(FakeState {
                files: Map::new(),
                head: head.map(String::from),
                writes: vec![],
                branch: branch.to_string(),
                changed: BTreeSet::new(),
                git_calls: vec![],
                git_log: vec![],
                prs: vec![],
                upstream: None,
                fetch_error: None,
                diverged: false,
                conflicts: vec![],
                merge_head: false,
                snapshots: vec![],
                remote: Map::new(),
                diff: String::new(),
                has_origin: true,
                ages: HashMap::new(),
                branches: Map::new(),
                tracking: BTreeSet::new(),
                worktrees: Map::new(),
                ancestors: HashSet::new(),
                ahead: HashMap::new(),
                missing_commits: HashSet::new(),
                pull_requests: HashMap::new(),
                pr_edits: vec![],
                remove_error: None,
                gh_calls: vec![],
                mtime_kept: vec![],
                clock: None,
                mtimes: HashMap::new(),
                leaks: vec![],
                scans: vec![],
                gitleaks_missing: false,
                committed: HashMap::new(),
                file_reads: vec![],
                line_counts: None,
                trees: HashMap::new(),
                refs: Map::new(),
                restores: vec![],
                session_logs: HashMap::new(),
                before_write: None,
                run_states_with: None,
            }),
        }
    }

    /// What the host holds and records, to script it or look at it; dropped before the next call on the host.
    pub fn state(&self) -> RefMut<'_, FakeState> {
        self.state.borrow_mut()
    }

    /// Delete a file of the working tree, as an agent may.
    pub fn delete(&self, path: &str) {
        let mut s = self.state();
        s.files.remove(path);
        s.changed.insert(path.to_string());
    }
}

impl Default for FakeHost {
    fn default() -> FakeHost {
        FakeHost::new(Some("abc123"), "main")
    }
}

impl FakeState {
    /// The changed files in the worktree at cwd, or else in the checkout: those outside every worktree.
    fn changed_in(&self, cwd: &str) -> BTreeSet<String> {
        if self.worktrees.contains_key(cwd) {
            return self.changed.iter().filter(|p| p.starts_with(&format!("{cwd}/"))).cloned().collect();
        }
        let outside = |p: &&String| !self.worktrees.keys().any(|w| p.starts_with(&format!("{w}/")));
        self.changed.iter().filter(outside).cloned().collect()
    }

    fn now(&self) -> i64 {
        HOST_EPOCH + self.clock.as_ref().map_or(0, |clock| clock() as i64)
    }

    /// Snapshot what is changed under cwd: snap<n>, which trees then holds.
    fn snapshot(&mut self, cwd: &str, parent: &str, message: &str) -> String {
        self.snapshots.push((parent.to_string(), message.to_string()));
        let sha = format!("snap{}", self.snapshots.len());
        let tree = self.changed_in(cwd).into_iter().map(|p| (p.clone(), self.files.get(&p).cloned())).collect();
        self.trees.insert(sha.clone(), tree);
        sha
    }

    /// The tree a lock stored under git_ref.
    fn locked_tree(&self, git_ref: &str) -> &BTreeMap<String, Option<String>> {
        let sha = self.refs.get(git_ref).unwrap_or_else(|| panic!("FakeHost has no lock {git_ref}"));
        &self.trees[sha]
    }

    /// A git command in a linked worktree at cwd, or None for one played as in the checkout.
    fn git_in_worktree(&mut self, cwd: &str, args: &[&str]) -> Option<Completed> {
        match args {
            ["rev-parse", "--abbrev-ref", "HEAD"] => Some(ok(&format!("{}\n", self.worktrees.get(cwd)?))),
            ["switch", "-c", name, _] => {
                self.worktrees.insert(cwd, name.to_string());
                Some(ok(""))
            }
            ["status", "--porcelain"] => Some(ok(&porcelain(&self.changed_in(cwd)))),
            ["commit", ..] => {
                let changed = self.changed_in(cwd);
                self.changed.retain(|p| !changed.contains(p));
                self.commit_head();
                Some(ok(""))
            }
            _ => None,
        }
    }

    /// HEAD moved on by a commit.
    fn commit_head(&mut self) {
        if let Some(head) = &mut self.head {
            head.push_str("+commit");
        }
    }

    /// A git command, played on what the state holds.
    fn git(&mut self, cwd: &str, args: &[&str]) -> Completed {
        if self.worktrees.contains_key(cwd) {
            if let Some(done) = self.git_in_worktree(cwd, args) {
                return done;
            }
        }
        let conflicted = |s: &FakeState| s.conflicts.iter().map(|f| format!("/proj/{f}")).collect::<Vec<_>>();
        match args {
            ["rev-parse", "--abbrev-ref", "HEAD"] => return ok(&format!("{}\n", self.branch)),
            ["rev-parse", "-q", "--verify", "MERGE_HEAD"] => return status(if self.merge_head { 0 } else { 1 }),
            ["status", "--porcelain"] => return ok(&porcelain(&self.changed_in(cwd))),
            ["switch", "-c", name] | ["switch", "--quiet", name] => self.branch = name.to_string(),
            ["commit", ..] => {
                let changed = self.changed_in(cwd);
                self.changed.retain(|p| !changed.contains(p));
                self.commit_head();
            }
            ["worktree", "add", "--detach", path, _] => self.worktrees.insert(*path, "HEAD".to_string()),
            ["worktree", "remove", path] | ["worktree", "remove", "--force", path] => {
                if let Some(error) = &self.remove_error {
                    return Completed::new(128, "", error);
                }
                self.worktrees.remove(path);
                self.changed.retain(|p| !p.starts_with(&format!("{path}/")));
            }
            ["fetch", "--quiet", "origin", _] if self.fetch_error.is_some() => {
                return Completed::new(128, "", self.fetch_error.as_deref().unwrap_or_default());
            }
            ["merge-base", "--is-ancestor", "HEAD", _] => return status(if self.diverged { 1 } else { 0 }),
            ["merge-base", "--is-ancestor", _, "HEAD"] => {
                return status(if self.upstream.is_none() && !self.diverged { 0 } else { 1 });
            }
            ["merge", "--ff-only", "--quiet", _] => {
                if self.diverged {
                    return Completed::new(128, "", "fatal: Not possible to fast-forward, aborting.");
                }
                if let Some(upstream) = self.upstream.take() {
                    self.head = Some(upstream);
                }
            }
            ["merge", "--no-edit", "--quiet", _] => {
                if let Some(first) = self.conflicts.first() {
                    let said = format!("CONFLICT (content): Merge conflict in {first}");
                    self.merge_head = true;
                    self.changed.extend(conflicted(self));
                    return Completed::new(1, &said, "");
                }
                let upstream = self.upstream.take();
                self.head = Some(format!("merge-{}", upstream.as_deref().unwrap_or("None")));
            }
            ["diff", "--name-only", "-z", "--diff-filter=U"] => {
                if !self.merge_head {
                    return ok("");
                }
                return ok(&self.conflicts.iter().map(|f| format!("{f}\0")).collect::<String>());
            }
            ["merge", "--abort"] => {
                self.merge_head = false;
                for f in conflicted(self) {
                    self.changed.remove(&f);
                }
            }
            ["push", "--quiet", "--force", "origin", refspec] => {
                let (commit, git_ref) = refspec.split_once(':').unwrap_or((refspec, ""));
                self.remote.insert(git_ref.strip_prefix("refs/heads/").unwrap_or(git_ref), commit.to_string());
            }
            ["push", "--quiet", "origin", "--delete", git_ref] => {
                if self.remote.remove(git_ref.strip_prefix("refs/heads/").unwrap_or(git_ref)).is_none() {
                    let said = format!("error: unable to delete '{git_ref}': remote ref does not exist");
                    return Completed::new(1, "", &said);
                }
            }
            ["ls-remote", "--heads", "origin"] => {
                return ok(&self.remote.iter().map(|(b, c)| format!("{c}\trefs/heads/{b}\n")).collect::<String>());
            }
            ["-c", "core.quotePath=false", "diff", ..] => return ok(&self.diff),
            ["remote", "get-url", "origin"] => {
                if !self.has_origin {
                    return Completed::new(2, "", "error: No such remote 'origin'");
                }
                return ok("git@github.com:o/r.git\n");
            }
            ["ls-remote", "origin", git_ref] => {
                let b = git_ref.strip_prefix("refs/heads/").unwrap_or(git_ref);
                return ok(&self.remote.get(b).map(|c| format!("{c}\t{git_ref}\n")).unwrap_or_default());
            }
            ["push", "--quiet", lease, "origin", "--delete", git_ref] if lease.starts_with("--force-with-lease=") => {
                let b = git_ref.strip_prefix("refs/heads/").unwrap_or(git_ref);
                let at = self.remote.get(b).map_or("None", String::as_str);
                if *lease != format!("--force-with-lease={git_ref}:{at}") {
                    return Completed::new(1, "", &format!(" ! [rejected]        {b} (stale info)"));
                }
                self.remote.remove(b);
                self.tracking.remove(b);
            }
            ["update-ref", "-d", git_ref] => {
                let b = git_ref.strip_prefix("refs/remotes/origin/").unwrap_or(git_ref);
                self.tracking.remove(b);
            }
            ["rev-parse", "-q", "--verify", git_ref] if git_ref.starts_with("refs/heads/") => {
                return match self.branches.get(&git_ref["refs/heads/".len()..]) {
                    Some(commit) => ok(&format!("{commit}\n")),
                    None => status(1),
                };
            }
            ["rev-parse", "-q", "--verify", object] if object.ends_with("^{commit}") => {
                let commit = &object[..object.len() - "^{commit}".len()];
                return status(if self.missing_commits.contains(commit) { 1 } else { 0 });
            }
            ["merge-base", "--is-ancestor", commit, of] => {
                let is = commit == of || self.ancestors.contains(&(commit.to_string(), of.to_string()));
                return status(if is { 0 } else { 1 });
            }
            ["rev-list", "--count", commits] => {
                let branch = commits.split_once("..refs/heads/").map_or("", |(_, b)| b);
                let n = self.ahead.get(branch).unwrap_or_else(|| panic!("FakeHost has no ahead for {branch}"));
                return ok(&format!("{n}\n"));
            }
            ["branch", "--quiet", "-D", b] => {
                self.branches.remove(b);
            }
            ["worktree", "list", "--porcelain"] => {
                let mut trees = vec![("/proj".to_string(), self.branch.clone())];
                trees.extend(self.worktrees.iter().map(|(p, b)| (p.to_string(), b.clone())));
                let listed: String = trees
                    .iter()
                    .map(|(path, b)| {
                        let head = if b == "HEAD" { "detached".to_string() } else { format!("branch refs/heads/{b}") };
                        format!("worktree {path}\nHEAD {}\n{head}\n\n", "0".repeat(40))
                    })
                    .collect();
                return ok(&listed);
            }
            _ => {}
        }
        ok("")
    }
}

impl Host for FakeHost {
    fn ssh_target(&self) -> Option<&str> {
        None
    }

    fn exec(&self, argv: &[&str], _stdin: Option<&[u8]>, _timeout: u64) -> Result<Exec, OrchestratorError> {
        panic!("FakeHost ran a real process: {argv:?}")
    }

    fn read(&self, path: &str) -> Result<Option<String>, OrchestratorError> {
        Ok(self.state().files.get(path).cloned())
    }

    fn write(&self, path: &str, text: &str, keep_mtime: bool) -> Result<(), OrchestratorError> {
        // Run with the host's state released, so that the hook may read or write the host itself.
        let hook = self.state().before_write.take();
        if let Some(mut hook) = hook {
            let allowed = hook(path);
            self.state().before_write = Some(hook);
            allowed?;
        }
        let mut s = self.state();
        s.writes.push(path.to_string());
        if keep_mtime {
            s.mtime_kept.push(path.to_string());
        } else {
            let now = s.now();
            s.mtimes.insert(path.to_string(), now);
        }
        s.files.insert(path, text.to_string());
        let orchestrator_file =
            path.starts_with("/proj/.orchestrator/") && !path.starts_with(&format!("/proj/{WORKTREES_DIR}/"));
        if !orchestrator_file {
            s.changed.insert(path.to_string());
        }
        Ok(())
    }

    fn rename(&self, path: &str, new_path: &str) -> Result<(), OrchestratorError> {
        let mut s = self.state();
        let text = s.files.remove(path).unwrap_or_else(|| panic!("FakeHost has no {path} to rename"));
        s.files.insert(new_path, text);
        if let Some(mtime) = s.mtimes.remove(path) {
            s.mtimes.insert(new_path.to_string(), mtime);
        }
        Ok(())
    }

    fn now(&self) -> Result<i64, OrchestratorError> {
        Ok(self.state().now())
    }

    fn mtime(&self, path: &str) -> Result<i64, OrchestratorError> {
        let s = self.state();
        Ok(s.mtimes.get(path).copied().unwrap_or_else(|| s.now()))
    }

    fn git_head(&self, _cwd: &str) -> Result<Option<String>, OrchestratorError> {
        Ok(self.state().head.clone())
    }

    fn git_run(&self, cwd: &str, args: &[&str], _timeout: u64) -> Result<Completed, OrchestratorError> {
        let mut s = self.state();
        let owned: Vec<String> = args.iter().map(|a| a.to_string()).collect();
        s.git_calls.push(owned.clone());
        s.git_log.push((cwd.to_string(), owned));
        Ok(s.git(cwd, args))
    }

    fn git_run_verbatim(
        &self,
        cwd: &str,
        args: &[&str],
        timeout: u64,
    ) -> Result<Completed<Vec<u8>>, OrchestratorError> {
        let proc = self.git_run(cwd, args, timeout)?;
        Ok(Completed { status: proc.status, stdout: proc.stdout.into_bytes(), stderr: proc.stderr })
    }

    fn snapshot(&self, cwd: &str, parent: &str, message: &str) -> Result<String, OrchestratorError> {
        Ok(self.state().snapshot(cwd, parent, message))
    }

    fn lock(&self, cwd: &str, base: &str, git_ref: &str) -> Result<Vec<String>, OrchestratorError> {
        let mut s = self.state();
        let sha = s.snapshot(cwd, base, &format!("locked files, {git_ref}"));
        s.refs.insert(git_ref, sha.clone());
        let prefix = format!("{cwd}/");
        Ok(s.trees[&sha].keys().map(|p| p.strip_prefix(&prefix).unwrap_or(p).to_string()).collect())
    }

    fn locked_changes(
        &self,
        cwd: &str,
        git_ref: &str,
        paths: &[String],
    ) -> Result<(Vec<String>, String), OrchestratorError> {
        let s = self.state();
        let tree = s.locked_tree(git_ref);
        let shown = |text: Option<&String>| text.map_or("None".to_string(), String::clone);
        let mut differ = vec![];
        let mut diff = String::new();
        for p in paths {
            let path = format!("{cwd}/{p}");
            let (was, is) = (tree.get(&path).cloned().flatten(), s.files.get(&path));
            if was.as_ref() != is {
                diff.push_str(&format!("--- {p}: {}\n+++ {p}: {}\n", shown(was.as_ref()), shown(is)));
                differ.push(p.clone());
            }
        }
        Ok((differ, diff))
    }

    fn restore(&self, cwd: &str, git_ref: &str, paths: &[String]) -> Result<(), OrchestratorError> {
        let mut s = self.state();
        s.restores.push((git_ref.to_string(), paths.to_vec()));
        let tree = s.locked_tree(git_ref).clone();
        for p in paths {
            let path = format!("{cwd}/{p}");
            match tree.get(&path).cloned().flatten() {
                None => {
                    s.files.remove(&path);
                }
                Some(text) => s.files.insert(path, text),
            }
        }
        Ok(())
    }

    fn delete_refs(&self, _cwd: &str, prefix: &str) -> Result<(), OrchestratorError> {
        let mut s = self.state();
        s.refs = s.refs.iter().filter(|(r, _)| !r.starts_with(prefix)).map(|(r, sha)| (r, sha.clone())).collect();
        Ok(())
    }

    fn file_at(&self, _cwd: &str, commit: &str, path: &str) -> Result<Option<Vec<u8>>, OrchestratorError> {
        let mut s = self.state();
        s.file_reads.push((commit.to_string(), path.to_string()));
        Ok(s.committed.get(&(commit.to_string(), path.to_string())).map(|text| text.clone().into_bytes()))
    }

    fn changed_line_counts(
        &self,
        _cwd: &str,
        _base: &str,
        _commit: &str,
    ) -> Result<Vec<ChangedFile>, OrchestratorError> {
        let s = self.state();
        if let Some(counts) = &s.line_counts {
            return Ok(counts.clone());
        }
        let added = crate::patch::added_lines(s.diff.as_bytes())?;
        Ok(added
            .iter()
            .map(|(path, lines)| ChangedFile {
                base_path: path.to_string(),
                path: path.to_string(),
                added: lines.len() as u64,
                removed: 0,
            })
            .collect())
    }

    fn check_gitleaks(&self) -> Result<(), OrchestratorError> {
        if self.state().gitleaks_missing {
            return Err(OrchestratorError(format!(
                "this run pushes, and every push is scanned for secrets first: {}",
                crate::constants::GITLEAKS_INSTALL
            )));
        }
        Ok(())
    }

    fn scan_unpublished(
        &self,
        _cwd: &str,
        commit: &str,
        base: &str,
        report_path: &str,
    ) -> Result<Vec<Leak>, OrchestratorError> {
        let mut s = self.state();
        s.scans.push((commit.to_string(), base.to_string(), report_path.to_string()));
        if !s.leaks.is_empty() {
            // As a gitleaks run without --redact would write it: the report is the one place the secret may be.
            let report: Vec<String> = s
                .leaks
                .iter()
                .map(|leak| {
                    let file = Json::from(leak.file.as_str()).dumps();
                    format!("{{\"Secret\": \"{FAKE_TOKEN}\", \"File\": {file}}}")
                })
                .collect();
            s.files.insert(report_path, format!("[{}]", report.join(", ")));
        }
        Ok(s.leaks
            .iter()
            .map(|leak| {
                let commit = if leak.commit == SCANNED { commit.to_string() } else { leak.commit.clone() };
                Leak { commit, ..leak.clone() }
            })
            .collect())
    }

    fn is_worktree(&self, _cwd: &str, path: &str) -> Result<bool, OrchestratorError> {
        Ok(self.state().worktrees.contains_key(path))
    }

    fn gh_run(&self, _cwd: &str, args: &[&str]) -> Result<Completed, OrchestratorError> {
        let mut s = self.state();
        s.gh_calls.push(args.iter().map(|a| a.to_string()).collect());
        match args {
            ["pr", "view", url, "--json", "state,headRefOid,closedAt" | "state,isDraft,mergeable,body"] => {
                match s.pull_requests.get(*url).unwrap_or_else(|| panic!("FakeHost has no pull request {url}")) {
                    FakePr::Prints(pr) => Ok(ok(&pr.dumps())),
                    FakePr::Fails(proc) => Ok(proc.clone()),
                }
            }
            _ => panic!("FakeHost got an unscripted gh command: {args:?}"),
        }
    }

    fn create_pr(
        &self,
        _cwd: &str,
        base: &str,
        head: &str,
        title: &str,
        body: &str,
        draft: bool,
    ) -> Result<String, OrchestratorError> {
        let opened =
            OpenedPr { base: base.into(), head: head.into(), title: title.into(), body: body.into(), draft };
        self.state().prs.push(opened);
        Ok("https://github.com/o/r/pull/7".to_string())
    }

    fn edit_pr_body(&self, _cwd: &str, url: &str, body: &str) -> Result<(), OrchestratorError> {
        self.state().pr_edits.push(PrEdit::Body { url: url.into(), body: body.into() });
        Ok(())
    }

    fn mark_pr_ready(&self, _cwd: &str, url: &str) -> Result<(), OrchestratorError> {
        self.state().pr_edits.push(PrEdit::Ready { url: url.into() });
        Ok(())
    }

    fn run_states(&self, cwd: &str) -> Result<Vec<(u64, Json)>, OrchestratorError> {
        let hook = self.state().run_states_with.take();
        if let Some(mut hook) = hook {
            let answer = hook(cwd);
            self.state().run_states_with = Some(hook);
            if let Some(answer) = answer {
                return answer;
            }
        }
        let s = self.state();
        let runs = format!("{cwd}/{}/", crate::constants::RUNS_DIR);
        let mut states = vec![];
        for (path, text) in s.files.iter() {
            let Some((run_id, name)) = path.strip_prefix(&runs).and_then(|rest| rest.split_once('/')) else {
                continue;
            };
            if name == "state.json" {
                let state = Json::parse(text).map_err(OrchestratorError)?;
                states.push((s.ages.get(run_id).copied().unwrap_or(0), state));
            }
        }
        Ok(states)
    }

    fn session_log(&self, kind: &str, session: &str) -> Result<Option<Vec<u8>>, OrchestratorError> {
        let s = self.state();
        Ok(s.session_logs.get(&(kind.to_string(), session.to_string())).map(|log| log.clone().into_bytes()))
    }
}

/// A command that exited with status 0, having printed stdout.
fn ok(stdout: &str) -> Completed {
    Completed::new(0, stdout, "")
}

/// A command that exited with status, printing nothing.
fn status(status: i32) -> Completed {
    Completed::new(status, "", "")
}

/// `git status --porcelain` of changed, each an untracked file.
fn porcelain(changed: &BTreeSet<String>) -> String {
    changed.iter().map(|p| format!("?? {p}\n")).collect()
}

#[cfg(test)]
mod tests;
