//! `judged-in-quality`: whether a retained build of the quality job already judged the checkout's HEAD as a pull
//! request's build into a branch would, so that build need not judge it again (docs/quality-gate.md says when), as
//! orchestrator.py's judged_in_quality decides it.

use crate::ci::{Jenkins, QualityBuild};
use crate::constants::REMOTE;
use crate::error::OrchestratorError;
use crate::host::{top_literal, Host, DEFAULT_TIMEOUT};
use crate::py::text::{repr, shortened, strip};

/// The files that decide how a change is judged: the stages, the gate, its rules, the versions of its tools and the
/// Python they run on. judged_in_quality trusts a quality build only when none of them differs.
pub const JUDGING_FILES: [&str; 6] = ["Jenkinsfile", "gate.py", "orchestrator.py", "pyproject.toml", "uv.lock", ".python-version"];

/// What a quality build's description says it judged, in the format the comment above the Jenkinsfile's Checkout
/// stage defines: `judged tree=<sha> parent=<sha> main=<sha>`, each sha SHA-1's 40 lowercase hex digits or SHA-256's
/// 64.
#[derive(Debug, PartialEq, Eq)]
struct Judged<'a> {
    /// The tree the build judged.
    tree: &'a str,
    /// The commit it judged the tree's change from.
    parent: &'a str,
    /// The main commit whose JUDGING_FILES judged it.
    main: &'a str,
}

impl Judged<'_> {
    /// The description read, when it is exactly in that format; None otherwise.
    fn read(description: &str) -> Option<Judged<'_>> {
        let rest = description.strip_prefix("judged tree=")?;
        let (tree, rest) = sha_then(rest)?;
        let (parent, rest) = sha_then(rest.strip_prefix(" parent=")?)?;
        let (main, rest) = sha_then(rest.strip_prefix(" main=")?)?;
        rest.is_empty().then_some(Judged { tree, parent, main })
    }
}

/// The sha text starts with, the longest of 64 or 40 lowercase hex digits it can be, and what follows it.
fn sha_then(text: &str) -> Option<(&str, &str)> {
    let digits = text.bytes().take_while(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b)).count();
    let len = [64, 40].into_iter().find(|&n| digits >= n)?;
    Some(text.split_at(len))
}

/// The number of a build of ci's job that judged the HEAD of the checkout at root on host as a pull request's build
/// into the branch target would, so that build need not judge it again.
///
/// Such a build is among ci's retained builds and meets each of these conditions, in this order: its description is
/// in Judged's format; it names HEAD's tree; it names as parent the merge base of HEAD and REMOTE's target; it ended
/// SUCCESS; and each of JUDGING_FILES is the same, or absent alike, at the main commit it names as in HEAD and at that
/// merge base. Fetches target from REMOTE into refs/remotes/REMOTE/target, and a build's main commit when root lacks
/// it.
///
/// Errors: no build meets them all, naming the first condition the closest build fails (the newest of those that
/// meet the most conditions in a row), its description masked and the message shortened to 400 characters; target is
/// not a branch name; HEAD has no merge base with it, or its JUDGING_FILES differ from that merge base; the
/// credentials are missing; Jenkins fails or answers otherwise than with the job's builds; git fails, or a main
/// commit cannot be fetched.
pub fn judged_in_quality(host: &dyn Host, root: &str, ci: &Jenkins, target: &str) -> Result<i64, OrchestratorError> {
    // A leading "-" is refused as `check-ref-format --branch` refuses it; --branch itself is not used, since it turns
    // @{-N} into the branch checked out N switches ago.
    let branch_ref = format!("refs/heads/{target}");
    if target.starts_with('-') || host.git_run(root, &["check-ref-format", &branch_ref], DEFAULT_TIMEOUT)?.status != 0 {
        return Err(OrchestratorError(format!("{} is not a branch name", repr(target))));
    }
    let tracking = format!("refs/remotes/{REMOTE}/{target}");
    host.git(root, &["fetch", "--quiet", REMOTE, &format!("+{branch_ref}:{tracking}")])?;
    let tree = strip(&host.git(root, &["rev-parse", "--verify", "HEAD^{tree}"])?).to_string();
    let merge_base = host.git_run(root, &["merge-base", "HEAD", &tracking], DEFAULT_TIMEOUT)?;
    if merge_base.status != 0 {
        // merge-base exits 1, printing nothing, when the commits have no common ancestor.
        let why = Some(strip(&merge_base.stderr)).filter(|s| !s.is_empty()).unwrap_or("no common ancestor");
        return Err(OrchestratorError(format!("HEAD has no merge base with {REMOTE}/{target}: {why}")));
    }
    let base = strip(&merge_base.stdout).to_string();
    let changed = judging_changes(host, root, &base, "HEAD")?;
    if !changed.is_empty() {
        return Err(OrchestratorError(format!(
            "HEAD changes {} since its merge base {base}, which decide how it is judged",
            changed.join(", ")
        )));
    }
    let builds = ci.retained_builds()?;
    if builds.is_empty() {
        return Err(OrchestratorError(format!("the quality job {} has no builds", ci.job())));
    }
    // The closest refusal so far: the first, newest, of those that meet the most conditions, as Python's max keeps it.
    let mut closest: Option<(u32, String)> = None;
    for build in &builds {
        match build_trust(host, root, build, &tree, &base)? {
            (_, None) => return Ok(build.number),
            (met, Some(why)) => {
                if closest.as_ref().is_none_or(|(most, _)| met > *most) {
                    closest = Some((met, why));
                }
            }
        }
    }
    let why = closest.map(|(_, why)| why).unwrap_or_default();
    // A description is text from Jenkins, of any length, so it is masked before the cut, which could split a token.
    Err(OrchestratorError(shortened(&ci.mask(&why), 400)))
}

/// How many of judged_in_quality's conditions build meets in a row, for HEAD's tree and merge base base, and why it
/// fails the first it does not, None when it meets them all; the reason quotes the build's description whole,
/// unmasked. Fetches the build's main commit, as judged_in_quality says, only when it meets the conditions before that
/// one. Errors: git fails, as for a main commit that cannot be fetched.
fn build_trust(
    host: &dyn Host,
    root: &str,
    build: &QualityBuild,
    tree: &str,
    base: &str,
) -> Result<(u32, Option<String>), OrchestratorError> {
    let n = build.number;
    let Some(judged) = build.description.as_deref().and_then(Judged::read) else {
        let quoted = build.description.as_deref().map_or_else(|| "None".to_string(), repr);
        return Ok((0, Some(format!("build #{n}'s description is not `judged tree=<sha> parent=<sha> main=<sha>`: {quoted}"))));
    };
    if judged.tree != tree {
        return Ok((1, Some(format!("build #{n} judged the tree {}, not HEAD's {tree}", judged.tree))));
    }
    if judged.parent != base {
        return Ok((2, Some(format!("build #{n} judged from {}, not from HEAD's merge base {base}", judged.parent))));
    }
    match build.result.as_deref() {
        Some("SUCCESS") => {}
        None => return Ok((3, Some(format!("build #{n} is still running")))),
        Some(result) => return Ok((3, Some(format!("build #{n} ended {result}")))),
    }
    let commit = format!("{}^{{commit}}", judged.main);
    if host.git_run(root, &["cat-file", "-e", &commit], DEFAULT_TIMEOUT)?.status != 0 {
        host.git(root, &["fetch", "--quiet", REMOTE, judged.main])?;
    }
    let changed = judging_changes(host, root, base, judged.main)?;
    if !changed.is_empty() {
        let main = judged.main;
        return Ok((4, Some(format!("build #{n} judged by main at {main}, where {} differ from HEAD's", changed.join(", ")))));
    }
    Ok((5, None))
}

/// Those of JUDGING_FILES whose file differs between commits a and b, its mode included, or that only one of them
/// has, in git's order. Errors: git fails.
fn judging_changes(host: &dyn Host, root: &str, a: &str, b: &str) -> Result<Vec<String>, OrchestratorError> {
    // --no-relative: under the user's diff.relative, a diff run in a subdirectory lists none of the top's files.
    let mut args = vec!["diff", "--name-only", "-z", "--no-relative", "--no-renames", "--no-ext-diff", a, b, "--"];
    let literal = top_literal(&JUDGING_FILES);
    args.extend(literal.iter().map(String::as_str));
    let names = host.git_verbatim(root, &args)?;
    // The pathspecs admit only JUDGING_FILES, whose names are ASCII.
    Ok(names.split(|&c| c == 0).filter(|p| !p.is_empty()).map(|p| String::from_utf8_lossy(p).into_owned()).collect())
}

#[cfg(test)]
mod tests;
