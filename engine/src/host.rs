//! The machine the agents run on, as orchestrator.py's Host: its files, its clock and its git checkout, locally or
//! over `ssh -o BatchMode=yes`, and gh and gitleaks run there.
//!
//! [`Host`] is a trait so that the engine's tests can stand a fake in for the machine. Every operation is a provided
//! method built on two primitives, [`Host::exec`], which runs one command, and [`Host::ssh_target`]: [`ProcessHost`]
//! implements only those, and a fake overrides what it plays in memory, as `FakeHost` (host/fake.rs) plays git one
//! command at a time through [`Host::git_run`], so that Host's own git steps run on it.

use crate::constants::{NETWORK_TIMEOUT, REMOTE, RUNS_DIR};
use crate::error::OrchestratorError;
use crate::harness::{harness, SessionLog};
use crate::py::json::{Json, Map};
use crate::py::text::{repr, shlex_join, strip, universal_newlines};

mod process;
mod scan;

pub use process::ProcessHost;
pub use scan::LeaksFound;
pub(crate) use process::{run_process, ProcessError};

use crate::quality::Leak;

#[cfg(test)]
pub(crate) mod fake;

/// How long a command may take when its caller names no other time, in seconds.
pub const DEFAULT_TIMEOUT: u64 = 60;
/// The status the scripts of read and session_log exit with when the file they read is not there.
pub const MISSING_FILE_STATUS: i32 = 3;
/// gh's exit status when it is not logged in, and sh's when gh is not installed: no gh call can succeed then.
pub const GH_UNUSABLE_STATUS: [i32; 2] = [4, 127];
/// Runs a command in a directory: gh, which finds the repository from its working directory and has no -C, and
/// gitleaks, which reads an `[extend] path` from its working directory.
pub const IN_DIR: &str = "cd -- \"$1\" && shift && exec \"$@\"";
/// The name of a run directory's file that the engine's metrics command reads (crate::summary::run_metrics).
const METRICS_FILE: &str = "metrics.json";

/// What one command printed, and how it ended, as [`Host::exec`] gives it: its output as the bytes it wrote.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Exec {
    /// The command as it ran, as one line of sh (shlex.join): over SSH, the ssh command that carried it.
    pub command: String,
    /// Its exit status, or minus the number of the signal that ended it, as Python's returncode.
    pub status: i32,
    /// What the command wrote to its standard output; over SSH, the remote command's, which ssh passes on.
    pub stdout: Vec<u8>,
    /// What it wrote to its standard error; over SSH, the remote command's and ssh's own, such as why it could not
    /// connect.
    pub stderr: Vec<u8>,
}

/// A command's exit status and output, as [`Host::run`] decodes it: `Completed` for text, `Completed<Vec<u8>>` for
/// output kept verbatim ([`Host::run_verbatim`]).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Completed<T = String> {
    /// The exit status, or minus the number of the signal that ended it, as Python's returncode.
    pub status: i32,
    /// What it wrote to its standard output, as Exec::stdout says.
    pub stdout: T,
    /// What it wrote to its standard error, as Exec::stderr says. For a verbatim run, decoded with each byte that is
    /// not UTF-8 replaced, as it is only ever shown.
    pub stderr: String,
}

impl Completed {
    /// A command that exited with status, having printed stdout and stderr: what a fake answers a command with.
    pub fn new(status: i32, stdout: &str, stderr: &str) -> Completed {
        Completed { status, stdout: stdout.to_string(), stderr: stderr.to_string() }
    }
}

/// A file a change touches, as [`Host::changed_line_counts`] gives it.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct ChangedFile {
    /// Its path at the base: path, unless git paired the file with another as a rename.
    pub base_path: String,
    /// Its path in the change; for a file the change adds or deletes, the side without it has no file there.
    pub path: String,
    /// Lines added; 0 for a binary file.
    pub added: u64,
    /// Lines removed; 0 for a binary file.
    pub removed: u64,
}

/// A file in a run directory, as [`Host::run_files`] lists it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunFile {
    /// The run directory's path, a slash and the file's name.
    pub path: String,
    /// When it was last modified, in seconds since the epoch by its host's clock.
    pub mtime: i64,
}

/// A pull request as [`Host::pull_request`] reads it from gh.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PullRequest {
    /// OPEN, CLOSED or MERGED.
    pub state: String,
    /// The commit its head branch is at: 40 to 64 lowercase hex digits.
    pub head_ref_oid: String,
    /// When it was closed or merged, as GitHub writes the time; None while it is open.
    pub closed_at: Option<String>,
}

/// A pull request as [`Host::pull_request_merge`] reads it from gh.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PullRequestMerge {
    /// OPEN, CLOSED or MERGED.
    pub state: String,
    /// Whether it is a draft, which cannot be merged until it is marked ready.
    pub is_draft: bool,
    /// MERGEABLE, CONFLICTING, or UNKNOWN while GitHub has not computed it.
    pub mergeable: String,
    /// Its description, as Markdown.
    pub body: String,
}

/// Why a gh command about a pull request failed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GhError {
    /// gh is missing or logged out where the project is, so no gh call can succeed: Python's GhUnusable.
    Unusable(OrchestratorError),
    /// Any other failure: gh failed, or printed what is not the pull request asked for.
    Failed(OrchestratorError),
}

impl From<GhError> for OrchestratorError {
    fn from(e: GhError) -> OrchestratorError {
        match e {
            GhError::Unusable(e) | GhError::Failed(e) => e,
        }
    }
}

/// Why a push of a run's work did not happen.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PushError {
    /// The secret scan found secrets in what the push would publish: Python's LeaksFound.
    Leaks(LeaksFound),
    /// The scan could not judge, or the push failed.
    Failed(OrchestratorError),
}

impl From<OrchestratorError> for PushError {
    fn from(e: OrchestratorError) -> PushError {
        PushError::Failed(e)
    }
}

impl From<PushError> for OrchestratorError {
    fn from(e: PushError) -> OrchestratorError {
        match e {
            PushError::Leaks(leaks) => OrchestratorError(leaks.to_string()),
            PushError::Failed(e) => e,
        }
    }
}

/// The machine the agents run on: its files, its clock, its git checkouts, gh and gitleaks, reached locally or over
/// SSH. Each path is one on that machine; each command runs with the environment child_env gives
/// (crate::env::child_env), so no process it starts inherits the CI credentials.
///
/// Every error is an [`OrchestratorError`] naming the command, and, over SSH, the target, unless a method says
/// otherwise. Timeouts are in seconds; a method that names none gives each command [`DEFAULT_TIMEOUT`], and those that
/// reach origin or GitHub NETWORK_TIMEOUT.
pub trait Host {
    /// The SSH target the commands run on, as ssh takes it (`[user@]host`); None for this machine.
    fn ssh_target(&self) -> Option<&str>;

    /// Runs argv on this host and gives its exit status and output, as they are, once it has exited and closed its
    /// output. stdin, when given, is its standard input, else it reads none. Errors: argv cannot be started, or runs
    /// longer than timeout seconds, in which case every process it started that has not left its process group is
    /// killed.
    fn exec(&self, argv: &[&str], stdin: Option<&[u8]>, timeout: u64) -> Result<Exec, OrchestratorError>;

    /// argv's exit status and output as text, each line end made "\n", which suits output that is parsed, such as
    /// JSON or a state file. stdin is as for exec. Errors: as exec's, and output that is not UTF-8.
    fn run(&self, argv: &[&str], stdin: Option<&str>, timeout: u64) -> Result<Completed, OrchestratorError> {
        let Exec { command, status, stdout, stderr } = self.exec(argv, stdin.map(str::as_bytes), timeout)?;
        let decode = |bytes: Vec<u8>| {
            String::from_utf8(bytes).map(|text| universal_newlines(&text)).map_err(|e| {
                let why = crate::py::text::utf8_error(e.as_bytes(), &e.utf8_error());
                OrchestratorError(format!("{command}: printed what is not UTF-8: {why}"))
            })
        };
        let stdout = decode(stdout)?;
        Ok(Completed { status, stdout, stderr: decode(stderr)? })
    }

    /// argv's exit status and output as the command printed it, for output that carries paths or file contents: line
    /// ends as they are, and bytes that are not UTF-8 kept. stdin and the errors are as exec's.
    fn run_verbatim(
        &self,
        argv: &[&str],
        stdin: Option<&[u8]>,
        timeout: u64,
    ) -> Result<Completed<Vec<u8>>, OrchestratorError> {
        let exec = self.exec(argv, stdin, timeout)?;
        Ok(Completed {
            status: exec.status,
            stdout: exec.stdout,
            stderr: String::from_utf8_lossy(&exec.stderr).into_owned(),
        })
    }

    /// argv's standard output, run as run runs it. Errors: as run's, and argv exiting with a status other than 0
    /// (checked).
    fn check(&self, argv: &[&str], stdin: Option<&str>, timeout: u64) -> Result<String, OrchestratorError> {
        checked(self, argv, self.run(argv, stdin, timeout)?)
    }

    /// The file's contents, or None when it does not exist yet. Errors: it cannot be read, or is not UTF-8.
    fn read(&self, path: &str) -> Result<Option<String>, OrchestratorError> {
        let script = format!("[ -f \"$1\" ] || exit {MISSING_FILE_STATUS}; cat -- \"$1\"");
        let proc = self.run(&["sh", "-c", &script, "_", path], None, DEFAULT_TIMEOUT)?;
        match proc.status {
            MISSING_FILE_STATUS => Ok(None),
            0 => Ok(Some(proc.stdout)),
            _ => Err(OrchestratorError(format!("reading {path}: {}", strip(&proc.stderr)))),
        }
    }

    /// Replace the file's contents with text, making its directory if it is missing; with keep_mtime, the existing
    /// file keeps its modification time. keep_mtime is for a state.json written by someone other than its run: its
    /// age is the run's heartbeat. The file is written aside and renamed into place, so a reader never sees half of
    /// it.
    fn write(&self, path: &str, text: &str, keep_mtime: bool) -> Result<(), OrchestratorError> {
        let keep = if keep_mtime { " && touch -r \"$1\" -- \"$t\"" } else { "" };
        let script = format!(
            "t=\"$1.tmp.$$\"; mkdir -p -- \"$(dirname -- \"$1\")\" && cat > \"$t\"{keep} && mv -f -- \"$t\" \"$1\" \
             || {{ rm -f -- \"$t\"; exit 1; }}"
        );
        self.check(&["sh", "-c", &script, "_", path], Some(text), DEFAULT_TIMEOUT).map(drop)
    }

    /// Move the file at path to new_path, replacing what is there.
    fn rename(&self, path: &str, new_path: &str) -> Result<(), OrchestratorError> {
        self.check(&["mv", "-f", "--", path, new_path], None, DEFAULT_TIMEOUT).map(drop)
    }

    /// The time on this host, in seconds since the epoch. now and mtime read this host's clock, the one that stamps
    /// the handoff files, so a turn's start and end come from one clock, and skew between machines cannot distort its
    /// duration. Errors: date prints what is not a time.
    fn now(&self) -> Result<i64, OrchestratorError> {
        seconds(&self.check(&["date", "+%s"], None, DEFAULT_TIMEOUT)?, "date +%s")
    }

    /// When the file was last modified, in seconds since the epoch by this host's clock. Errors: there is no such
    /// file, or stat prints what is not a time.
    fn mtime(&self, path: &str) -> Result<i64, OrchestratorError> {
        // stat -c is GNU, stat -f is BSD.
        let script = "stat -c %Y -- \"$1\" 2>/dev/null || stat -f %m -- \"$1\"";
        seconds(&self.check(&["sh", "-c", script, "_", path], None, DEFAULT_TIMEOUT)?, &format!("stat of {path}"))
    }

    /// The absolute form of a directory's path, a leading ~ expanded to the home directory on this host. Errors:
    /// there is no such directory.
    fn resolve_dir(&self, path: &str) -> Result<String, OrchestratorError> {
        let (script, arg) = if path == "~" || path.starts_with("~/") {
            ("cd -- \"$HOME$1\" && pwd", &path[1..])
        } else {
            ("cd -- \"$1\" && pwd", path)
        };
        Ok(strip(&self.check(&["sh", "-c", script, "_", arg], None, DEFAULT_TIMEOUT)?).to_string())
    }

    /// HEAD's commit, or None when cwd is not a git repository with a commit.
    fn git_head(&self, cwd: &str) -> Result<Option<String>, OrchestratorError> {
        let proc = self.git_run(cwd, &["rev-parse", "--verify", "HEAD"], DEFAULT_TIMEOUT)?;
        Ok((proc.status == 0).then(|| strip(&proc.stdout).to_string()))
    }

    /// The standard output of git args in cwd, as run gives it. Errors: as run's, and git exiting with a status other
    /// than 0.
    fn git(&self, cwd: &str, args: &[&str]) -> Result<String, OrchestratorError> {
        git_timed(self, cwd, args, DEFAULT_TIMEOUT)
    }

    /// The standard output of git args in cwd, verbatim, as run_verbatim gives it. Errors: as git's.
    fn git_verbatim(&self, cwd: &str, args: &[&str]) -> Result<Vec<u8>, OrchestratorError> {
        let proc = self.git_run_verbatim(cwd, args, DEFAULT_TIMEOUT)?;
        checked(self, &git_argv(cwd, args), proc)
    }

    /// A git command in cwd whose failure the caller interprets, git using exit status 1 for answers such as "no";
    /// as run runs it.
    fn git_run(&self, cwd: &str, args: &[&str], timeout: u64) -> Result<Completed, OrchestratorError> {
        self.run(&git_argv(cwd, args), None, timeout)
    }

    /// git_run, with the output verbatim, as run_verbatim gives it.
    fn git_run_verbatim(
        &self,
        cwd: &str,
        args: &[&str],
        timeout: u64,
    ) -> Result<Completed<Vec<u8>>, OrchestratorError> {
        self.run_verbatim(&git_argv(cwd, args), None, timeout)
    }

    /// Fetch the branch from origin and return its remote-tracking ref, `origin/<branch>`. Errors: the fetch fails,
    /// as for a branch origin lacks.
    fn fetch(&self, cwd: &str, branch: &str) -> Result<String, OrchestratorError> {
        git_timed(self, cwd, &["fetch", "--quiet", REMOTE, branch], NETWORK_TIMEOUT)
            .map_err(|e| OrchestratorError(format!("could not fetch {branch} from {REMOTE}: {e}")))?;
        Ok(format!("{REMOTE}/{branch}"))
    }

    /// Bring the checked-out branch up to origin's; one only ahead of origin's is left as it is. Errors: the fetch
    /// fails, or the branch has diverged from origin's or cannot be fast-forwarded to it, saying to reconcile it.
    fn fast_forward(&self, cwd: &str, branch: &str) -> Result<(), OrchestratorError> {
        let upstream = self.fetch(cwd, branch)?;
        let args = ["merge", "--ff-only", "--quiet", upstream.as_str()];
        let proc = self.git_run(cwd, &args, DEFAULT_TIMEOUT)?;
        if proc.status == 0 {
            return Ok(());
        }
        let why = if git_test(self, cwd, &["merge-base", "--is-ancestor", "HEAD", &upstream])? {
            format!("{branch} cannot be fast-forwarded to {upstream}")
        } else {
            format!("{branch} has diverged from {upstream}")
        };
        Err(OrchestratorError(format!(
            "{why}; reconcile it first. git {} failed: {}",
            shlex_join(&args),
            strip(&proc.stderr)
        )))
    }

    /// Merge origin's latest branch into HEAD with a merge commit, and return the files that conflict. Nothing is
    /// merged when HEAD already holds it, so a repeated call merges once. A conflicting merge is aborted, leaving HEAD
    /// and the working tree as they were. Errors: the fetch fails, or the merge fails without a conflict.
    fn merge_upstream(&self, cwd: &str, branch: &str) -> Result<Vec<String>, OrchestratorError> {
        let upstream = self.fetch(cwd, branch)?;
        if git_test(self, cwd, &["merge-base", "--is-ancestor", &upstream, "HEAD"])? {
            return Ok(vec![]);
        }
        let merge = self.git_run(cwd, &["merge", "--no-edit", "--quiet", &upstream], DEFAULT_TIMEOUT)?;
        if merge.status == 0 {
            return Ok(vec![]);
        }
        // -z: paths with unusual characters come unquoted.
        let conflicts = nul_separated(&self.git(cwd, &["diff", "--name-only", "-z", "--diff-filter=U"])?);
        self.abort_merge(cwd)?;
        if conflicts.is_empty() {
            return Err(OrchestratorError(format!(
                "git merge {upstream} failed in {cwd}: {}",
                strip(&merge.stderr)
            )));
        }
        Ok(conflicts)
    }

    /// Abort a merge in progress, if there is one; true when there was.
    fn abort_merge(&self, cwd: &str) -> Result<bool, OrchestratorError> {
        if !git_test(self, cwd, &["rev-parse", "-q", "--verify", "MERGE_HEAD"])? {
            return Ok(false);
        }
        self.git(cwd, &["merge", "--abort"])?;
        Ok(true)
    }

    /// Commit the working tree as it is, untracked files included, on top of parent, with message; returns the
    /// commit. No ref moves and the index stays as it was. `.gitignore` is honoured, which keeps `.orchestrator/` out,
    /// so the snapshot holds what committing the change would. Errors: git fails, or prints what is no commit.
    fn snapshot(&self, cwd: &str, parent: &str, message: &str) -> Result<String, OrchestratorError> {
        // Through a copy of the index, in one sh -c script, because git -C cannot pass GIT_INDEX_FILE.
        let script = "cd -- \"$1\" || exit 1; i=$(git rev-parse --git-path index) || exit 1; t=$(mktemp) || exit 1; \
                      trap 'rm -f -- \"$t\"' EXIT; \
                      if [ -f \"$i\" ]; then cp -- \"$i\" \"$t\" || exit 1; else rm -f -- \"$t\"; fi; \
                      GIT_INDEX_FILE=$t git add -A && tree=$(GIT_INDEX_FILE=$t git write-tree) && \
                      git commit-tree \"$tree\" -p \"$2\" -m \"$3\"";
        // The `else` above: a repository whose index was never written, from which git starts the copy afresh.
        let out = self.check(&["sh", "-c", script, "_", cwd, parent, message], None, DEFAULT_TIMEOUT)?;
        let sha = strip(&out);
        if !is_commit_id(sha) {
            return Err(OrchestratorError(format!(
                "snapshot of {cwd}: git commit-tree printed {}, not a commit",
                repr(sha)
            )));
        }
        Ok(sha.to_string())
    }

    // Locked files (Step.locks). Their paths are relative to the repository's top level, whatever directory of the
    // repository cwd is.

    /// Snapshot the working tree on top of base, store the commit under ref, replacing what ref held, and return the
    /// paths whose files the snapshot changes from base: added, edited or deleted, a rename as its two paths. ref is a
    /// local ref, which keeps the commit from git gc; nothing pushes it.
    fn lock(&self, cwd: &str, base: &str, git_ref: &str) -> Result<Vec<String>, OrchestratorError> {
        let sha = self.snapshot(cwd, base, &format!("locked files, {git_ref}"))?;
        self.git(cwd, &["update-ref", git_ref, &sha])?;
        paths_between(self, cwd, base, &sha, &[])
    }

    /// Of paths, the ones whose file in the working tree, untracked files included, differs from ref's, a file
    /// missing on one side counting as different; and the diff from ref to the working tree over them, as text for a
    /// reader, a byte that is not UTF-8 replaced. ([], "") when none differs. Reads only: ref, the tree and the index
    /// stay as they are.
    fn locked_changes(
        &self,
        cwd: &str,
        git_ref: &str,
        paths: &[String],
    ) -> Result<(Vec<String>, String), OrchestratorError> {
        if paths.is_empty() {
            return Ok((vec![], String::new()));
        }
        let sha = self.snapshot(cwd, git_ref, &format!("the working tree against {git_ref}"))?;
        let differ = paths_between(self, cwd, git_ref, &sha, paths)?;
        if differ.is_empty() {
            return Ok((vec![], String::new()));
        }
        let mut args = vec!["diff", "--no-color", "--no-ext-diff", "--no-renames", git_ref, &sha, "--"];
        let literal = top_literal(&differ);
        args.extend(literal.iter().map(String::as_str));
        let diff = self.git_verbatim(cwd, &args)?;
        Ok((differ, String::from_utf8_lossy(&diff).into_owned()))
    }

    /// Make each of paths in the working tree what it is in ref: written as ref has it, or deleted where ref has none.
    /// The index stays as it is.
    fn restore(&self, cwd: &str, git_ref: &str, paths: &[String]) -> Result<(), OrchestratorError> {
        let held: Vec<String> = if paths.is_empty() {
            vec![]
        } else {
            let mut args = vec!["ls-tree", "-r", "--name-only", "-z", "--full-tree", git_ref, "--"];
            let literal = top_literal(paths);
            args.extend(literal.iter().map(String::as_str));
            utf8_paths(&self.git_verbatim(cwd, &args)?)?
        };
        let kept: Vec<&String> = paths.iter().filter(|p| held.contains(p)).collect();
        if !kept.is_empty() {
            let source = format!("--source={git_ref}");
            let mut args = vec!["restore", source.as_str(), "--worktree", "--"];
            let literal = top_literal(&kept);
            args.extend(literal.iter().map(String::as_str));
            self.git(cwd, &args)?;
        }
        // git restore refuses a path that neither ref nor the index has, so a file ref lacks is removed here.
        let gone: Vec<&String> = paths.iter().filter(|p| !held.contains(p)).collect();
        if !gone.is_empty() {
            let top = utf8_path(&self.git_verbatim(cwd, &["rev-parse", "--show-toplevel"])?)?;
            let top = top.strip_suffix('\n').unwrap_or(&top);
            let files: Vec<String> = gone.iter().map(|p| format!("{top}/{p}")).collect();
            let mut argv = vec!["rm", "-f", "--"];
            argv.extend(files.iter().map(String::as_str));
            self.check(&argv, None, DEFAULT_TIMEOUT)?;
        }
        Ok(())
    }

    /// Delete every local ref whose name starts with prefix, such as `refs/orchestrator/<key>/`; none is no error.
    fn delete_refs(&self, cwd: &str, prefix: &str) -> Result<(), OrchestratorError> {
        for git_ref in self.git(cwd, &["for-each-ref", "--format=%(refname)", prefix])?.split_whitespace() {
            self.git(cwd, &["update-ref", "-d", git_ref])?;
        }
        Ok(())
    }

    /// `git diff -U0` from base to commit, or to the working tree's tracked files without one, in the one shape
    /// crate::patch::added_lines parses, whatever the user's git config. Verbatim: a line that is not UTF-8, such as
    /// Latin-1 text or a binary file git diffs as text, is kept, so a change to such a file is judged like any other,
    /// and only git's "\n" ends a line, so nothing inside an added line can pass for a header.
    fn change_diff(&self, cwd: &str, base: &str, commit: Option<&str>) -> Result<Vec<u8>, OrchestratorError> {
        let mut args = vec![
            "-c",
            "core.quotePath=false",
            "diff",
            "-U0",
            "-M",
            "--no-color",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base,
        ];
        args.extend(commit);
        args.push("--");
        self.git_verbatim(cwd, &args)
    }

    /// The file at path, relative to the repository's top level and taken literally, as commit has it, its bytes as
    /// git stores them; None where commit has no file there. A symbolic link gives the path it points to. Errors: git
    /// cannot read commit, or cwd is not in a git repository.
    fn file_at(&self, cwd: &str, commit: &str, path: &str) -> Result<Option<Vec<u8>>, OrchestratorError> {
        Ok(scan::blob_at(self, cwd, commit, path)?.map(|(text, _)| text))
    }

    /// Each file the change from base to commit touches, a rename paired as change_diff pairs it, with the lines it
    /// adds and removes, from `git diff --numstat`. Errors: git fails, or names a path that is not UTF-8.
    fn changed_line_counts(&self, cwd: &str, base: &str, commit: &str) -> Result<Vec<ChangedFile>, OrchestratorError> {
        let out = self.git_verbatim(cwd, &["diff", "--numstat", "-z", "-M", "--no-ext-diff", base, commit])?;
        numstat(&out)
    }

    /// Push commit to origin's branch, replacing what is there, once scan_unpublished finds no secret in what it
    /// would publish; base and report_path are as scan_unpublished takes them. Errors: PushError::Leaks with what the
    /// scan found, or PushError::Failed when it cannot judge or the push fails; nothing is pushed then.
    fn push_ref(&self, cwd: &str, commit: &str, branch: &str, base: &str, report_path: &str) -> Result<(), PushError> {
        self.refuse_leaks(cwd, commit, base, report_path)?;
        // --force replaces what a failed attempt left under the same name.
        let refspec = format!("{commit}:refs/heads/{branch}");
        git_timed(self, cwd, &["push", "--quiet", "--force", REMOTE, &refspec], NETWORK_TIMEOUT)?;
        Ok(())
    }

    /// Push the local branch to origin's branch of that name and track it, once scan_unpublished finds no secret in
    /// what its commit would publish; base and report_path are as scan_unpublished takes them. Errors: as push_ref's.
    fn push_branch(&self, cwd: &str, branch: &str, base: &str, report_path: &str) -> Result<(), PushError> {
        let commit = self.git(cwd, &["rev-parse", "--verify", &format!("refs/heads/{branch}")])?;
        self.refuse_leaks(cwd, strip(&commit), base, report_path)?;
        git_timed(self, cwd, &["push", "--quiet", "--set-upstream", REMOTE, branch], NETWORK_TIMEOUT)?;
        Ok(())
    }

    /// Why gitleaks with its `git` subcommand (8.19 and later), which scan_unpublished runs, cannot run here; None
    /// when it can. Its help, not its version, is asked: a distribution's build may print no version number.
    fn gitleaks_problem(&self) -> Option<String> {
        match self.run(&["gitleaks", "git", "--help"], None, DEFAULT_TIMEOUT) {
            Err(e) => Some(format!("gitleaks cannot be started ({e})")),
            Ok(proc) if proc.status != 0 => Some("`gitleaks git` does not run".to_string()),
            Ok(_) => None,
        }
    }

    /// Errors, saying how to install it, when gitleaks_problem finds a problem.
    fn check_gitleaks(&self) -> Result<(), OrchestratorError> {
        match self.gitleaks_problem() {
            Some(problem) => Err(OrchestratorError(format!(
                "this run pushes, and every push is scanned for secrets first, but {problem}: {}",
                crate::constants::GITLEAKS_INSTALL
            ))),
            None => Ok(()),
        }
    }

    /// PushError::Leaks when scan_unpublished finds any in what a push of commit would publish, and
    /// PushError::Failed when it cannot judge, as scan_unpublished says, which also says what it writes beside
    /// report_path.
    fn refuse_leaks(&self, cwd: &str, commit: &str, base: &str, report_path: &str) -> Result<(), PushError> {
        let leaks = self.scan_unpublished(cwd, commit, base, report_path)?;
        if leaks.is_empty() {
            return Ok(());
        }
        Err(PushError::Leaks(LeaksFound { leaks, report_path: report_path.to_string() }))
    }

    /// The secrets gitleaks finds, and the `gitleaks:allow` comments added, in the commits a push of commit would
    /// publish: those reachable from it and from no origin ref. A merge's own content is what it adds beyond git's
    /// automatic merge of its parents (git's remerge diff), and a range holding a merge of three or more parents, of
    /// which git gives no such diff, is refused. A remote-tracking ref for a branch origin has since deleted or
    /// rewritten narrows the range: what only it reaches was public once, and is not scanned again.
    ///
    /// The rules come from base, a commit before the change: its .gitleaks.toml (else gitleaks' defaults), each file
    /// it extends with `[extend] path`, in turn, and its .gitleaksignore (else none), and nothing the change holds, so
    /// that the change cannot allowlist itself. Every file git takes for text by its content is diffed as text, with
    /// no textconv, whatever any .gitattributes says; one whose first 8000 bytes hold a NUL is binary to git and
    /// scanned by neither gitleaks nor the comment check (TODO.md). gitleaks honours a `gitleaks:allow` comment
    /// silently, so each one an added line holds is a Leak of its own. gitleaks runs with --redact and writes its JSON
    /// report to report_path, a path in the run's directory, beside the .gitleaksignore copy, the rules directory and
    /// the empty directory it scans from.
    ///
    /// Errors, failing closed: git cannot read base or the range; base's rules cannot be taken whole from base (a
    /// file in the chain is no TOML, extends what base lacks or what leaves the repository, or is a symbolic link) or
    /// its .gitleaksignore is a symbolic link; the range holds an octopus merge; or gitleaks cannot be started, exits
    /// other than 0 or 1, writes no report or one that is not a list of findings, or exits 1 with an empty one, as it
    /// does when it cannot load its config.
    fn scan_unpublished(
        &self,
        cwd: &str,
        commit: &str,
        base: &str,
        report_path: &str,
    ) -> Result<Vec<Leak>, OrchestratorError> {
        scan::scan_unpublished(self, cwd, commit, base, report_path)
    }

    /// Delete origin's branch.
    fn delete_remote_branch(&self, cwd: &str, branch: &str) -> Result<(), OrchestratorError> {
        let git_ref = format!("refs/heads/{branch}");
        git_timed(self, cwd, &["push", "--quiet", REMOTE, "--delete", &git_ref], NETWORK_TIMEOUT).map(drop)
    }

    /// origin's branches whose names start with prefix.
    fn remote_branches(&self, cwd: &str, prefix: &str) -> Result<Vec<String>, OrchestratorError> {
        let out = git_timed(self, cwd, &["ls-remote", "--heads", REMOTE], NETWORK_TIMEOUT)?;
        let wanted = format!("refs/heads/{prefix}");
        Ok(crate::py::text::splitlines(&out)
            .into_iter()
            .filter_map(|line| line.split_once('\t').map(|(_, name)| name))
            .filter(|name| name.starts_with(&wanted))
            .map(|name| name["refs/heads/".len()..].to_string())
            .collect())
    }

    /// The commit origin's branch is at; None when origin has no such branch.
    fn remote_branch_commit(&self, cwd: &str, branch: &str) -> Result<Option<String>, OrchestratorError> {
        let git_ref = format!("refs/heads/{branch}");
        let out = git_timed(self, cwd, &["ls-remote", REMOTE, &git_ref], NETWORK_TIMEOUT)?;
        // ls-remote matches its pattern against the end of each ref's name, so the exact name is picked out.
        Ok(crate::py::text::splitlines(&out).into_iter().find_map(|line| {
            let (commit, name) = line.split_once('\t').unwrap_or((line, ""));
            (name == git_ref).then(|| commit.to_string())
        }))
    }

    /// Delete origin's branch while it is at commit; the lease fails the push if anyone pushed since.
    fn delete_remote_branch_at(&self, cwd: &str, branch: &str, commit: &str) -> Result<(), OrchestratorError> {
        let git_ref = format!("refs/heads/{branch}");
        let lease = format!("--force-with-lease={git_ref}:{commit}");
        git_timed(self, cwd, &["push", "--quiet", &lease, REMOTE, "--delete", &git_ref], NETWORK_TIMEOUT).map(drop)
    }

    /// Drop `origin/<branch>`, which outlives the branch on origin until a fetch prunes it; none is no error.
    fn delete_tracking_ref(&self, cwd: &str, branch: &str) -> Result<(), OrchestratorError> {
        self.git(cwd, &["update-ref", "-d", &format!("refs/remotes/{REMOTE}/{branch}")]).map(drop)
    }

    /// The commit the local branch is at; None when there is no such branch.
    fn branch_commit(&self, cwd: &str, branch: &str) -> Result<Option<String>, OrchestratorError> {
        let git_ref = format!("refs/heads/{branch}");
        let args = ["rev-parse", "-q", "--verify", git_ref.as_str()];
        let proc = self.git_run(cwd, &args, DEFAULT_TIMEOUT)?;
        if proc.status == 1 {
            return Ok(None);
        }
        Ok(Some(strip(&checked(self, &git_argv(cwd, &args), proc)?).to_string()))
    }

    /// Whether this clone has commit.
    fn has_commit(&self, cwd: &str, commit: &str) -> Result<bool, OrchestratorError> {
        git_test(self, cwd, &["rev-parse", "-q", "--verify", &format!("{commit}^{{commit}}")])
    }

    /// Whether commit is of or one of its ancestors.
    fn is_ancestor(&self, cwd: &str, commit: &str, of: &str) -> Result<bool, OrchestratorError> {
        git_test(self, cwd, &["merge-base", "--is-ancestor", commit, of])
    }

    /// How many commits the local branch has that commit lacks. Errors: git fails, or prints what is no count.
    fn commits_beyond(&self, cwd: &str, commit: &str, branch: &str) -> Result<u64, OrchestratorError> {
        let range = format!("{commit}..refs/heads/{branch}");
        let out = self.git(cwd, &["rev-list", "--count", &range])?;
        let count = strip(&out);
        match count.parse() {
            Ok(n) if count.bytes().all(|b| b.is_ascii_digit()) => Ok(n),
            _ => Err(OrchestratorError(format!("git rev-list --count {range} printed {}, not a count", repr(count)))),
        }
    }

    /// Delete the local branch, whether or not it is merged.
    fn delete_branch(&self, cwd: &str, branch: &str) -> Result<(), OrchestratorError> {
        // -D: -d checks the branch against HEAD or its upstream, not its pull request, so it refuses a squash merge.
        self.git(cwd, &["branch", "--quiet", "-D", branch]).map(drop)
    }

    /// Check commit out, on a detached HEAD, in a new worktree of cwd's repository at path.
    fn add_worktree(&self, cwd: &str, path: &str, commit: &str) -> Result<(), OrchestratorError> {
        self.git(cwd, &["worktree", "add", "--detach", path, commit]).map(drop)
    }

    /// Whether cwd's repository has a remote named origin; any failure of git's to name its URL counts as none.
    fn has_origin(&self, cwd: &str) -> Result<bool, OrchestratorError> {
        Ok(self.git_run(cwd, &["remote", "get-url", REMOTE], DEFAULT_TIMEOUT)?.status == 0)
    }

    /// Whether path is a worktree of cwd's repository, registered and still there.
    fn is_worktree(&self, cwd: &str, path: &str) -> Result<bool, OrchestratorError> {
        // git lists a worktree by its physical path, which differs from path when path goes through a symlink.
        let proc = self.run(&["sh", "-c", "cd -P -- \"$1\" && pwd -P", "_", path], None, DEFAULT_TIMEOUT)?;
        if proc.status != 0 {
            return Ok(false);
        }
        let listed = format!("worktree {}", strip(&proc.stdout));
        let out = self.git(cwd, &["worktree", "list", "--porcelain"])?;
        // One record per worktree, separated by a blank line; "prunable" marks one whose directory is gone.
        Ok(out.split("\n\n").any(|record| {
            let lines = crate::py::text::splitlines(record);
            lines.first() == Some(&listed.as_str()) && !lines.iter().any(|l| l.starts_with("prunable"))
        }))
    }

    /// Remove the worktree at path; without force, git refuses one with changes it would lose.
    fn remove_worktree(&self, cwd: &str, path: &str, force: bool) -> Result<(), OrchestratorError> {
        let mut args = vec!["worktree", "remove"];
        if force {
            args.push("--force");
        }
        args.push(path);
        self.git(cwd, &args).map(drop)
    }

    /// The branches checked out in the repository's worktrees, the main checkout's included, each with its path, in
    /// the order git lists them.
    fn checked_out_branches(&self, cwd: &str) -> Result<Map<String>, OrchestratorError> {
        let mut branches = Map::new();
        let mut path = String::new();
        for line in crate::py::text::splitlines(&self.git(cwd, &["worktree", "list", "--porcelain"])?) {
            let (key, value) = line.split_once(' ').unwrap_or((line, ""));
            if key == "worktree" {
                path = value.to_string();
            } else if key == "branch" {
                branches.insert(value.strip_prefix("refs/heads/").unwrap_or(value), path.clone());
            }
        }
        Ok(branches)
    }

    /// A gh command in cwd whose failure the caller interprets.
    fn gh_run(&self, cwd: &str, args: &[&str]) -> Result<Completed, OrchestratorError> {
        let mut argv = vec!["sh", "-c", IN_DIR, "_", cwd, "gh"];
        argv.extend(args);
        self.run(&argv, None, NETWORK_TIMEOUT)
    }

    /// The pull request at url's state, head commit and closing time, as gh reports them. Errors: GhError::Unusable
    /// when gh is missing or logged out, GhError::Failed when it fails otherwise or prints what is not that.
    fn pull_request(&self, cwd: &str, url: &str) -> Result<PullRequest, GhError> {
        let (pr, out) = pr_view(self, cwd, url, "state,headRefOid,closedAt")?;
        // Each field read and judged in turn, as the Python engine's check reads them, so that the first one wrong is
        // the one an error names.
        let parsed = (|| {
            let Some(state) = subscript(&pr, "state")?.as_str() else { return Ok(None) };
            let head = subscript(&pr, "headRefOid")?;
            let Some(head) = head.as_str() else {
                return Err(format!("expected string or bytes-like object, got '{}'", py_type(head)));
            };
            if !is_commit_id(head) {
                return Ok(None);
            }
            let closed = subscript(&pr, "closedAt")?;
            if !(closed.is_null() || closed.as_str().is_some()) {
                return Ok(None);
            }
            let closed_at = closed.as_str().map(String::from);
            Ok(Some(PullRequest { state: state.to_string(), head_ref_oid: head.to_string(), closed_at }))
        })();
        pr_fields(url, &out, parsed)
    }

    /// The pull request at url's state, whether it is a draft, whether it can be merged and its body, as gh reports
    /// them. Errors: as pull_request's.
    fn pull_request_merge(&self, cwd: &str, url: &str) -> Result<PullRequestMerge, GhError> {
        let (pr, out) = pr_view(self, cwd, url, "state,isDraft,mergeable,body")?;
        // As pull_request reads its fields.
        let parsed = (|| {
            let Some(state) = subscript(&pr, "state")?.as_str() else { return Ok(None) };
            let Some(is_draft) = subscript(&pr, "isDraft")?.as_bool() else { return Ok(None) };
            let mergeable = subscript(&pr, "mergeable")?.as_str();
            let Some(mergeable) = mergeable.filter(|m| ["MERGEABLE", "CONFLICTING", "UNKNOWN"].contains(m)) else {
                return Ok(None);
            };
            let Some(body) = subscript(&pr, "body")?.as_str() else { return Ok(None) };
            let (state, mergeable, body) = (state.to_string(), mergeable.to_string(), body.to_string());
            Ok(Some(PullRequestMerge { state, is_draft, mergeable, body }))
        })();
        pr_fields(url, &out, parsed)
    }

    /// Make body the description of the pull request at url, with gh.
    fn edit_pr_body(&self, cwd: &str, url: &str, body: &str) -> Result<(), OrchestratorError> {
        let argv = ["sh", "-c", IN_DIR, "_", cwd, "gh", "pr", "edit", url, "--body-file", "-"];
        self.check(&argv, Some(body), NETWORK_TIMEOUT).map(drop)
    }

    /// Take the pull request at url out of draft, with gh.
    fn mark_pr_ready(&self, cwd: &str, url: &str) -> Result<(), OrchestratorError> {
        self.check(&["sh", "-c", IN_DIR, "_", cwd, "gh", "pr", "ready", url], None, NETWORK_TIMEOUT).map(drop)
    }

    /// Open a pull request of head into base with gh, a draft when draft says so, and return its URL. Errors: gh
    /// fails, or prints no URL.
    fn create_pr(
        &self,
        cwd: &str,
        base: &str,
        head: &str,
        title: &str,
        body: &str,
        draft: bool,
    ) -> Result<String, OrchestratorError> {
        let mut argv =
            vec!["sh", "-c", IN_DIR, "_", cwd, "gh", "pr", "create", "--base", base, "--head", head, "--title", title];
        argv.extend(["--body-file", "-"]);
        if draft {
            argv.push("--draft");
        }
        let out = self.check(&argv, Some(body), NETWORK_TIMEOUT)?;
        match out.split_whitespace().last() {
            Some(url) => Ok(url.to_string()),
            None => Err(OrchestratorError(format!("gh pr create printed no URL for {head}"))),
        }
    }

    /// Each run's saved state under the project at cwd, with the seconds since its state.json was last written,
    /// measured by this host's own clock, the one that stamped the file, so clock skew between the machines involved
    /// cannot shift it. Errors: a state.json that is not JSON.
    fn run_states(&self, cwd: &str) -> Result<Vec<(u64, Json)>, OrchestratorError> {
        // stat -c is GNU, stat -f is BSD.
        let script = format!(
            "now=$(date +%s) || exit 1; for f in \"$1\"/{RUNS_DIR}/*/state.json; do [ -f \"$f\" ] || continue; \
             m=$(stat -c %Y -- \"$f\" 2>/dev/null || stat -f %m -- \"$f\") || exit 1; \
             printf \"%s \" \"$((now - m))\"; cat -- \"$f\"; echo; done; true"
        );
        let out = self.check(&["sh", "-c", &script, "_", cwd], None, DEFAULT_TIMEOUT)?;
        let mut runs = vec![];
        for line in crate::py::text::splitlines(&out) {
            if strip(line).is_empty() {
                continue;
            }
            let (age, text) = line.split_once(' ').unwrap_or((line, ""));
            let corrupt = |e: String| OrchestratorError(format!("corrupt run state under {cwd}/{RUNS_DIR}: {e}"));
            let not_int = || corrupt(format!("invalid literal for int() with base 10: {}", repr(age)));
            let age: i64 = python_int(age).ok_or_else(not_int)?;
            runs.push((age.max(0).unsigned_abs(), Json::parse(text).map_err(corrupt)?));
        }
        Ok(runs)
    }

    /// The log in which the harness kind keeps a session's token usage (crate::harness::SessionLog), found in the
    /// home directory here, as it is on disk; None for a harness without one, a session id that is not a plain name,
    /// or no such file.
    fn session_log(&self, kind: &str, session: &str) -> Result<Option<Vec<u8>>, OrchestratorError> {
        let Some(SessionLog { glob: pattern, .. }) = harness(kind).ok().and_then(|h| h.session_log) else {
            return Ok(None);
        };
        if session.is_empty() || !session.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-') {
            return Ok(None);
        }
        let script = format!(
            "cd -- \"$HOME\" || exit 1; for f in $1; do [ -f \"$f\" ] && exec cat -- \"$f\"; done; \
             exit {MISSING_FILE_STATUS}"
        );
        let glob = pattern.replace("{session}", session);
        let proc = self.run_verbatim(&["sh", "-c", &script, "_", &glob], None, DEFAULT_TIMEOUT)?;
        if proc.status == MISSING_FILE_STATUS {
            return Ok(None);
        }
        checked(self, &["sh", "-c", &script], proc).map(Some)
    }

    /// The metrics.json of each run of the project at cwd that has one, oldest run first. Errors: one that is not
    /// JSON.
    fn run_metrics(&self, cwd: &str) -> Result<Vec<Json>, OrchestratorError> {
        // Each file is one line; echo ends one that lacks its line end.
        let script = format!(
            "for f in \"$1\"/{RUNS_DIR}/*/{METRICS_FILE}; do [ -f \"$f\" ] || continue; cat -- \"$f\" && echo; done; \
             true"
        );
        let out = self.check(&["sh", "-c", &script, "_", cwd], None, DEFAULT_TIMEOUT)?;
        crate::py::text::splitlines(&out)
            .into_iter()
            .filter(|line| !strip(line).is_empty())
            .map(|line| {
                Json::parse(line)
                    .map_err(|e| OrchestratorError(format!("corrupt {METRICS_FILE} under {cwd}/{RUNS_DIR}: {e}")))
            })
            .collect()
    }

    /// The files in a run directory, oldest first, those of one time by name, except state.json and write's temporary
    /// files; none for a directory that is not there. Errors: stat prints what is not a time.
    fn run_files(&self, run_dir: &str) -> Result<Vec<RunFile>, OrchestratorError> {
        // Each entry is "<mtime> <path>" ended by a NUL, which no path holds. stat -c is GNU, stat -f is BSD.
        let script = "for f in \"$1\"/*; do [ -f \"$f\" ] || continue; \
                      case \"${f##*/}\" in state.json|*.tmp.*) continue;; esac; \
                      m=$(stat -c %Y -- \"$f\" 2>/dev/null || stat -f %m -- \"$f\") || exit 1; \
                      printf \"%s %s\\0\" \"$m\" \"$f\"; done; true";
        let out = self.check(&["sh", "-c", script, "_", run_dir], None, DEFAULT_TIMEOUT)?;
        let mut files = vec![];
        for entry in out.split('\0').filter(|e| !e.is_empty()) {
            let (mtime, path) = entry.split_once(' ').unwrap_or((entry, ""));
            let mtime = python_int(mtime).ok_or_else(|| {
                OrchestratorError(format!("listing {run_dir}: stat printed {}, not a time", repr(mtime)))
            })?;
            files.push((mtime, path.to_string()));
        }
        files.sort();
        Ok(files.into_iter().map(|(mtime, path)| RunFile { path, mtime }).collect())
    }
}

/// proc's output, proc being argv run on host; Errors, naming argv, the SSH target if any, and proc's standard error,
/// when it exited with a status other than 0.
pub fn checked<H: Host + ?Sized, T>(host: &H, argv: &[&str], proc: Completed<T>) -> Result<T, OrchestratorError> {
    if proc.status == 0 {
        return Ok(proc.stdout);
    }
    let on = host.ssh_target().map(|t| format!(" on {t}")).unwrap_or_default();
    Err(OrchestratorError(format!("{} failed{on}: {}", shlex_join(argv), strip(&proc.stderr))))
}

/// `git -C cwd args`.
fn git_argv<'a>(cwd: &'a str, args: &[&'a str]) -> Vec<&'a str> {
    let mut argv = vec!["git", "-C", cwd];
    argv.extend(args);
    argv
}

/// Host::git with timeout in seconds.
fn git_timed<H: Host + ?Sized>(host: &H, cwd: &str, args: &[&str], timeout: u64) -> Result<String, OrchestratorError> {
    checked(host, &git_argv(cwd, args), host.git_run(cwd, args, timeout)?)
}

/// Whether a yes-or-no git command answers yes: status 0 is yes, 1 is no. Errors: any other status.
fn git_test<H: Host + ?Sized>(host: &H, cwd: &str, args: &[&str]) -> Result<bool, OrchestratorError> {
    let proc = host.git_run(cwd, args, DEFAULT_TIMEOUT)?;
    match proc.status {
        0 => Ok(true),
        1 => Ok(false),
        _ => checked(host, &git_argv(cwd, args), proc).map(|_| false),
    }
}

/// git pathspecs that each match exactly one of paths, relative to the repository's top level, whatever directory git
/// runs in, and with no glob in them.
fn top_literal<S: AsRef<str>>(paths: &[S]) -> Vec<String> {
    paths.iter().map(|p| format!(":(top,literal){}", p.as_ref())).collect()
}

/// The paths whose files differ between commits a and b, a rename as its two paths; with paths, only those.
fn paths_between<H: Host + ?Sized>(
    host: &H,
    cwd: &str,
    a: &str,
    b: &str,
    paths: &[String],
) -> Result<Vec<String>, OrchestratorError> {
    let mut args = vec!["diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", a, b, "--"];
    let literal = top_literal(paths);
    args.extend(literal.iter().map(String::as_str));
    utf8_paths(&host.git_verbatim(cwd, &args)?)
}

/// The NUL-separated names of out, the empty ones left out.
fn nul_separated(out: &str) -> Vec<String> {
    out.split('\0').filter(|p| !p.is_empty()).map(String::from).collect()
}

/// The NUL-separated paths git printed in out, verbatim, the empty ones left out. Errors: one that is not UTF-8.
fn utf8_paths(out: &[u8]) -> Result<Vec<String>, OrchestratorError> {
    out.split(|&b| b == 0).filter(|p| !p.is_empty()).map(utf8_path).collect()
}

/// A path git printed verbatim, as text. Errors: it is not UTF-8, which the Python engine reads as os.fsdecode
/// decodes it (TODO.md, "The Rust engine reads some input ... otherwise").
fn utf8_path(bytes: &[u8]) -> Result<String, OrchestratorError> {
    String::from_utf8(bytes.to_vec()).map_err(|_| {
        OrchestratorError(format!("git printed a path that is not UTF-8: {}", repr(&String::from_utf8_lossy(bytes))))
    })
}

/// The files of a `git diff --numstat -z` out. -z gives each entry as `added<TAB>removed<TAB>path`, or for a rename
/// `added<TAB>removed<TAB>` and then the old and new paths as fields of their own; a binary file counts "-". The list
/// ends with an empty field.
fn numstat(out: &[u8]) -> Result<Vec<ChangedFile>, OrchestratorError> {
    let fields: Vec<&[u8]> = out.split(|&b| b == 0).collect();
    let bad = || OrchestratorError(format!("git diff --numstat printed {}", repr(&String::from_utf8_lossy(out))));
    let count = |n: &[u8]| -> Result<u64, OrchestratorError> {
        if n == b"-" {
            return Ok(0);
        }
        std::str::from_utf8(n).ok().and_then(python_int).and_then(|n| u64::try_from(n).ok()).ok_or_else(bad)
    };
    let mut files = vec![];
    let mut i = 0;
    while i < fields.len() && !fields[i].is_empty() {
        let mut parts = fields[i].splitn(3, |&b| b == b'\t');
        let (added, removed, path) = match (parts.next(), parts.next(), parts.next()) {
            (Some(a), Some(r), Some(p)) => (count(a)?, count(r)?, p),
            _ => return Err(bad()),
        };
        let (base_path, path) = if path.is_empty() {
            let (old, new) = (fields.get(i + 1).ok_or_else(bad)?, fields.get(i + 2).ok_or_else(bad)?);
            i += 3;
            (utf8_path(old)?, utf8_path(new)?)
        } else {
            i += 1;
            (utf8_path(path)?, utf8_path(path)?)
        };
        files.push(ChangedFile { base_path, path, added, removed });
    }
    Ok(files)
}

/// gh pr view's JSON of json_fields, comma-separated, for the pull request at url, and the text it came as. Errors:
/// GhError::Unusable when gh is missing or logged out, GhError::Failed when it fails otherwise or prints what is no
/// JSON.
fn pr_view<H: Host + ?Sized>(host: &H, cwd: &str, url: &str, json_fields: &str) -> Result<(Json, String), GhError> {
    let args = ["pr", "view", url, "--json", json_fields];
    let proc = host.gh_run(cwd, &args).map_err(GhError::Failed)?;
    if GH_UNUSABLE_STATUS.contains(&proc.status) {
        let said = if proc.stderr.is_empty() { &proc.stdout } else { &proc.stderr };
        return Err(GhError::Unusable(OrchestratorError(format!(
            "gh {} failed with status {}: {}",
            shlex_join(&args),
            proc.status,
            strip(said)
        ))));
    }
    let mut argv = vec!["gh"];
    argv.extend(args);
    let out = checked(host, &argv, proc).map_err(GhError::Failed)?;
    match Json::parse(&out) {
        Ok(pr) => Ok((pr, out)),
        Err(e) => {
            Err(GhError::Failed(OrchestratorError(format!("gh pr view {url} printed {}: {e}", repr(strip(&out))))))
        }
    }
}

/// What a view of the pull request at url gave, out being what gh printed: the fields parsed found, or the error
/// naming out when parsing it failed (Err, saying why) or found it not a pull request's (None).
fn pr_fields<T>(url: &str, out: &str, parsed: Result<Option<T>, String>) -> Result<T, GhError> {
    let printed = repr(strip(out));
    match parsed {
        Ok(Some(fields)) => Ok(fields),
        Ok(None) => Err(GhError::Failed(OrchestratorError(format!(
            "gh pr view {url} printed {printed}, not a pull request's state"
        )))),
        Err(why) => Err(GhError::Failed(OrchestratorError(format!("gh pr view {url} printed {printed}: {why}")))),
    }
}

/// value[key], as Python's subscript reads a JSON value: an object's entry. Errors, as Python's KeyError and TypeError
/// say them: an object without key, or a value of another type.
fn subscript<'a>(value: &'a Json, key: &str) -> Result<&'a Json, String> {
    match value {
        Json::Object(map) => map.get(key).ok_or_else(|| repr(key)),
        Json::Array(_) => Err("list indices must be integers or slices, not str".to_string()),
        Json::Str(_) => Err("string indices must be integers, not 'str'".to_string()),
        other => Err(format!("'{}' object is not subscriptable", py_type(other))),
    }
}

/// The name of the Python type json.loads reads value as.
fn py_type(value: &Json) -> &'static str {
    match value {
        Json::Null => "NoneType",
        Json::Bool(_) => "bool",
        Json::Int(_) => "int",
        Json::Float(_) => "float",
        Json::Str(_) => "str",
        Json::Array(_) => "list",
        Json::Object(_) => "dict",
    }
}

/// Whether text is a full commit id: 40 to 64 lowercase hex digits, as SHA-1 and SHA-256 repositories write them.
fn is_commit_id(text: &str) -> bool {
    (40..=64).contains(&text.len()) && text.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The seconds a command printed, out, as int() reads them; what names what printed them. Errors: out is no whole
/// number.
fn seconds(out: &str, what: &str) -> Result<i64, OrchestratorError> {
    let text = strip(out);
    python_int(text).ok_or_else(|| OrchestratorError(format!("{what} printed {}, not a time", repr(text))))
}

/// text as Python's int() reads a whole number in base 10: blanks around it, an optional sign, and ASCII digits with
/// single underscores between them; None for anything else, or a number beyond 64 bits.
fn python_int(text: &str) -> Option<i64> {
    let text = strip(text);
    let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
    let well_formed = !digits.is_empty()
        && !digits.starts_with('_')
        && !digits.ends_with('_')
        && !digits.contains("__")
        && digits.bytes().all(|b| b.is_ascii_digit() || b == b'_');
    if !well_formed {
        return None;
    }
    text.replace('_', "").parse().ok()
}

#[cfg(test)]
pub(crate) mod tests;
