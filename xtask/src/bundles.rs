//! `cargo xtask bundles [--check]`: build each eval task's `repo.bundle`, the toy repository its agents start from,
//! out of [`crate::toy_repos`]; with `--check`, change nothing and fail, naming them, when a bundle is not of those
//! files.
//!
//! A bundle holds one commit, on `main`, whose id depends only on its files: the author, committer, dates and message
//! are fixed, and neither the user's git configuration nor a repository git's environment names is read. So `--check`
//! compares commit ids, not bytes, which another git version may pack differently.

use std::fs;
use std::path::{Path, PathBuf};

use crate::process::{command, TempDir};
use crate::toy_repos::{ToyRepo, TOY_REPOS};
use crate::Error;

/// A task's bundle, in its directory under evals/tasks/.
pub const BUNDLE_NAME: &str = "repo.bundle";
/// The branch a bundle's one commit is on.
pub const BRANCH: &str = "main";

/// Who made the commit of every toy repository, and when, so that its id is the same on every machine; and no global
/// or system configuration, whose commit.gpgsign, hooks path or default branch would change the commit.
const COMMIT_ENV: &[(&str, &str)] = &[
    ("GIT_AUTHOR_NAME", "Octoflow Evals"),
    ("GIT_AUTHOR_EMAIL", "evals@example.com"),
    ("GIT_COMMITTER_NAME", "Octoflow Evals"),
    ("GIT_COMMITTER_EMAIL", "evals@example.com"),
    ("GIT_AUTHOR_DATE", "2026-10-31T00:00:00+00:00"),
    ("GIT_COMMITTER_DATE", "2026-10-31T00:00:00+00:00"),
    ("GIT_CONFIG_GLOBAL", "/dev/null"),
    ("GIT_CONFIG_NOSYSTEM", "1"),
];

/// The variables by which git, run from a hook or another git command, would work on that repository instead.
const GIT_REPOSITORY_ENV: &[&str] = &["GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY"];

/// The command line `cargo xtask bundles [--check]`: write every task's bundle, saying where and its commit, or check
/// them; the exit status is 0, or 1 when a bundle is missing or stale under --check or git fails, or 2 for a usage
/// error.
pub fn main(argv: &[String]) -> i32 {
    let tasks_dir = crate::eval::tasks_dir(&crate::checkout());
    let outcome = match argv {
        [] => write_bundles(TOY_REPOS, &tasks_dir).map(|written| {
            for (bundle, commit) in written {
                println!("{}: {commit}", bundle.display());
            }
            0
        }),
        [flag] if flag == "--check" => stale_bundles(TOY_REPOS, &tasks_dir).map(|stale| {
            for bundle in &stale {
                eprintln!("{} is not of xtask/src/toy_repos.rs's files: run `cargo xtask bundles`", bundle.display());
            }
            i32::from(!stale.is_empty())
        }),
        _ => {
            eprintln!("usage: cargo xtask bundles [--check]");
            return 2;
        }
    };
    outcome.unwrap_or_else(|e| {
        eprintln!("error: {e}");
        1
    })
}

/// Write each of repos' bundles into its task's directory under tasks_dir, replacing what is there, and return each
/// bundle's path and commit, in repos' order. Errors: a file could not be written, or git failed.
pub fn write_bundles(repos: &[ToyRepo], tasks_dir: &Path) -> Result<Vec<(PathBuf, String)>, Error> {
    repos
        .iter()
        .map(|repo| {
            let dir = tasks_dir.join(repo.task);
            fs::create_dir_all(&dir).map_err(|e| Error(format!("could not make {}: {e}", dir.display())))?;
            let bundle = dir.join(BUNDLE_NAME);
            let commit = make_bundle(repo.files, &bundle, &message(repo.task))?;
            Ok((bundle, commit))
        })
        .collect()
}

/// The bundles of repos, in their tasks' directories under tasks_dir, that are missing or not of repos' files, in
/// repos' order. Errors: git failed, or a bundle there could not be read.
pub fn stale_bundles(repos: &[ToyRepo], tasks_dir: &Path) -> Result<Vec<PathBuf>, Error> {
    let tmp = TempDir::new("toy-bundle-").map_err(|e| Error(format!("could not make a temporary directory: {e}")))?;
    let mut stale = vec![];
    for repo in repos {
        let expected = make_bundle(repo.files, &tmp.path().join(repo.task), &message(repo.task))?;
        let bundle = tasks_dir.join(repo.task).join(BUNDLE_NAME);
        if !bundle.exists() || bundle_commit(&bundle)? != expected {
            stale.push(bundle);
        }
    }
    Ok(stale)
}

/// Write bundle, a git bundle of one commit on main holding files (paths, relative and with `/`, to text) and nothing
/// else, its HEAD on main, and return the commit's id, which depends only on files and message. An existing bundle is
/// replaced. Errors: a file could not be written, or git failed.
pub fn make_bundle(files: &[(&str, &str)], bundle: &Path, message: &str) -> Result<String, Error> {
    let tmp = TempDir::new("toy-repo-").map_err(|e| Error(format!("could not make a temporary directory: {e}")))?;
    let repo = tmp.path();
    git(repo, &["init", "--quiet", &format!("--initial-branch={BRANCH}")])?;
    for (path, text) in files {
        let full = repo.join(path);
        if let Some(parent) = full.parent() {
            fs::create_dir_all(parent).map_err(|e| Error(format!("could not make {}: {e}", parent.display())))?;
        }
        fs::write(&full, text).map_err(|e| Error(format!("could not write {}: {e}", full.display())))?;
    }
    git(repo, &["add", "--all"])?;
    git(repo, &["commit", "--quiet", "--no-verify", "-m", message])?;
    let bundle = std::path::absolute(bundle).map_err(|e| Error(format!("{}: {e}", bundle.display())))?;
    git(repo, &["bundle", "create", "--quiet", &bundle.to_string_lossy(), "HEAD", BRANCH])?;
    Ok(git(repo, &["rev-parse", "HEAD"])?.trim().to_string())
}

/// The id of the commit main points to in bundle. Errors: bundle is not a readable bundle with a main.
pub fn bundle_commit(bundle: &Path) -> Result<String, Error> {
    let bundle = std::path::absolute(bundle).map_err(|e| Error(format!("{}: {e}", bundle.display())))?;
    let dir = bundle.parent().unwrap_or(Path::new("/"));
    let heads = git(dir, &["bundle", "list-heads", &bundle.to_string_lossy()])?;
    heads
        .lines()
        .find_map(|line| line.split_once(' ').filter(|(_, r)| *r == format!("refs/heads/{BRANCH}")).map(|(c, _)| c))
        .map(str::to_string)
        .ok_or_else(|| Error(format!("{} has no {BRANCH} branch", bundle.display())))
}

/// The message of a task's commit.
fn message(task: &str) -> String {
    format!("The starting point of the eval task {task}")
}

/// The standard output of git args, run in cwd with COMMIT_ENV and without GIT_REPOSITORY_ENV. Errors: git could not
/// start, or failed; the error holds its standard error.
fn git(cwd: &Path, args: &[&str]) -> Result<String, Error> {
    let mut git = command("git");
    git.arg("-C").arg(cwd).args(args).envs(COMMIT_ENV.iter().copied());
    for name in GIT_REPOSITORY_ENV {
        git.env_remove(name);
    }
    let output = git.output().map_err(|e| Error(format!("could not run git {}: {e}", args[0])))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(Error(format!("git {} in {} failed: {}", args.join(" "), cwd.display(), stderr.trim())));
    }
    String::from_utf8(output.stdout).map_err(|e| Error(format!("git {} wrote other than UTF-8: {e}", args[0])))
}
