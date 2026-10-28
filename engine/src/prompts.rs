//! The texts a run sends agents besides a workflow file's own prompts, and the prompt files that replace them: each
//! byte for byte the Python engine's, since the prompts are the product.

use crate::env::config_dir;
use crate::error::OrchestratorError;
use crate::proof_copies_dir;
use crate::py::format::field_names;
use crate::py::json::Map;
use crate::py::text::close_match;

// -- The built-in texts. Each is a template of str.format's language (crate::py::format) whose placeholders a prompt
// -- file replacing it must keep (prompt_problem).

pub const SPEC_PROMPT: &str = concat!(
    "You are the Spec Collector, the first of three roles (Spec Collector -> Builder -> Reviewer). ",
    "Separate agent sessions play the Builder and the Reviewer; they will know only what you write down. ",
    "A human is at this terminal and answers you directly.\n",
    "\n",
    "Task from the human:\n",
    "{task}\n",
    "\n",
    "Interview the human until the requirements are unambiguous: the goal, what is in and out of scope, ",
    "testable acceptance criteria, constraints, and how the result will be verified. ",
    "Read the code in {cwd} first so your questions are grounded and you can cite the files the change touches. ",
    "Ask a few questions at a time. Do not write or change any code.\n",
    "\n",
    "When the human approves the spec, write it in a single write to {spec_path} as Markdown. ",
    "Its first line is a `# ` heading: a short imperative title for the change, under 70 characters; ",
    "it becomes the commit subject and the pull request title. ",
    "Then these `##` sections: ",
    "Goal, Scope, Non-goals, Acceptance criteria (a numbered list, each one checkable), Relevant code (file:line), Verification. ",
    "Writing that file hands the work to the Builder, so write it only after the human approves it.",
);

pub const BUILD_PROMPT: &str = concat!(
    "You are the Builder, the second of three roles (Spec Collector -> Builder -> Reviewer). ",
    "The spec in {spec_path} was agreed with the human by a separate session; it is your contract.\n",
    "\n",
    "Implement it in {cwd}, following the conventions of the surrounding code. ",
    "Verify the change the way the spec's Verification section says, and run the tests. ",
    "Do not commit, push or switch branches; leave the changes in the working tree for the Reviewer. ",
    "If the spec is wrong or cannot be met, do not deviate silently: say so in your report.\n",
    "\n",
    "As your last step, write a report to {build_path} in a single write; it hands the work to the Reviewer: the files you changed and why, ",
    "how you verified the change (commands and a summary of their results), ",
    "and any acceptance criterion you did not meet, with the reason.",
);

pub const FIX_PROMPT: &str = concat!(
    "The Reviewer requested changes; the findings are in {prev_review_path}. ",
    "Fix each finding, or explain in your report why it is wrong. Re-run the verification. ",
    "Do not commit, push or switch branches. As your last step, write a new report to {build_path} in a single write, in the same shape as before, ",
    "answering each finding by its number.",
);

pub const REVIEW_PROMPT: &str = concat!(
    "You are the Reviewer, the last of three roles (Spec Collector -> Builder -> Reviewer). ",
    "You did not write this change. Judge it only against the spec in {spec_path} and the code itself.\n",
    "\n",
    "The change: {change}\n",
    "The Builder's report is in {build_path}. Treat its claims as unverified: check them, and run the verification yourself. ",
    "Do not modify any file other than your review.\n",
    "\n",
    "As your last step, write your review to {review_path} in a single write. Its first line must be exactly `VERDICT: {approve}` or `VERDICT: {changes}`. ",
    "Then list numbered findings, each with file:line, what is wrong, and which acceptance criterion it violates ",
    "or what failure it causes. Request changes only for defects: an unmet acceptance criterion, a bug, a broken test. ",
    "Style preferences are not defects.",
);

pub const RECHECK_PROMPT: &str = concat!(
    "The Builder has answered your review; the new report is in {build_path}. ",
    "Review the change again ({change}) against the spec in {spec_path} and your previous findings, ",
    "checking the Builder's claims rather than trusting them. ",
    "As your last step, write the review to {review_path} in a single write, with the same first-line verdict and numbered findings as before.",
);

/// For a role relaunched into its saved agent session after it had already been prompted.
pub const CONTINUE_PROMPT: &str = concat!(
    "Your session was restarted in the middle of this turn. Continue where you left off; ",
    "the turn still ends when you write {path} in a single write.",
);

/// For a verdict step whose file had no VERDICT first line. Self-contained, so a fresh session can answer it too.
pub const RETRY_VERDICT_PROMPT: &str = concat!(
    "The file {path} was rejected: it was empty, or its first non-blank line was not a verdict line. ",
    "It has been moved to {rejected_path}; reuse its findings, if it has any. ",
    "Write the file again to {path} in a single write. Its first line must be exactly `VERDICT: {approve}` ",
    "or `VERDICT: {changes}`, followed by the numbered findings.",
);

/// Appended at run time to every prompt of a step that can block (Step::can_block), so workflow definitions,
/// and what `workflows` prints, never contain it.
pub const BLOCKED_NOTE: &str = concat!(
    "If you cannot continue without a decision from the human, write the report with a first line ",
    "`BLOCKED: <your question>` instead.",
);

/// For an agent whose report was BLOCKED, while the human answers it in the agent's pane.
pub const BLOCKED_PROMPT: &str = concat!(
    "Your report asked the human a question and has been moved to {blocked_path}. ",
    "The human has been notified and will answer you in this pane. ",
    "Once the question is resolved, finish the work and write {path} again in a single write.",
);

/// Appended for a Builder or Reviewer that starts a fresh session in a later round: it has not seen the earlier ones.
pub const REBUILD_NOTE: &str = concat!(
    "This is round {n}, and you are a fresh session. An earlier Builder session did the earlier turns; ",
    "its changes are already in the working tree, and its reports are {earlier_build_paths}.",
);

pub const REREVIEW_NOTE: &str = concat!(
    "This is round {n}, and you are a fresh session. The earlier reviews of this change are {earlier_review_paths}; ",
    "check that the Builder has answered each of their findings.",
);

pub const QUALITY_FIX_PROMPT: &str = concat!(
    "The quality gate did not pass on your change; the findings are in {quality_path}. ",
    "Fix each numbered issue, the failed conditions, the failing tests and a failed build, ",
    "or explain in your report why one should stand. Answer each numbered finding to weigh the same way; ",
    "those never fail the gate, but the Reviewer reads your answers. Re-run the verification. ",
    "Do not commit, push or switch branches. As your last step, write a new report to {report_path} in a single write, ",
    "in the same shape as before, answering each numbered issue and finding to weigh by its number.",
);

/// Appended to the Reviewer's prompt in a run with the quality gate.
pub const QUALITY_PASSED_NOTE: &str = "The quality gate passed on this change; its report is in {quality_path}.";

pub const QUALITY_UNRESOLVED_NOTE: &str = concat!(
    "The quality gate still did not pass after the Builder's last quality round; ",
    "its unresolved findings are in {quality_path}. Weigh them as you would your own.",
);

/// QUALITY_UNRESOLVED_NOTE for a quality loop that stopped because a round failed on just what the one before it had.
pub const QUALITY_STUCK_NOTE: &str = concat!(
    "The quality gate still did not pass, and its last quality round failed on just what the round before it ",
    "failed on, so the Builder was not asked again; its unresolved findings are in {quality_path}. ",
    "Weigh them as you would your own.",
);

/// In place of the notes above when the round's change went unjudged (Workflow::unjudged) and the round has a quality
/// file: resume --no-quality-gate turned the gate off after it, and the Builder changed the code since. The pull
/// request says so too (pr_body).
pub const QUALITY_OFF_NOTE: &str = concat!(
    "The quality gate was turned off before the Builder's latest change was analysed, so nothing judged that change; ",
    "the round's earlier quality files are {quality_paths}, and the Builder's reports since answer them.",
);

/// Appended to the prompt of the step a review loops back to, when tests the Reviewer wrote fail against the change.
pub const PROOFS_NOTE: &str = concat!(
    "The Reviewer backed findings with tests, and these do not pass against your change: {proofs}. ",
    "Make each of them pass, without changing it; you may move one into the project as a regression test. ",
    "The orchestrator copies each proof's whole directory under the working tree's `",
    proof_copies_dir!(),
    "/` and ",
    "runs the proof there, so that the working tree's test configuration applies; to run one yourself, copy its whole ",
    "directory there too, or into the project, rather than running it where it lies. If one is wrong, say so in ",
    "your report instead.",
);

/// Appended to the verdict step's prompt, after the quality and locks notes, when proofs carried from an earlier round
/// (Workflow._carried_proofs) were run again against the round's change (Workflow._rerun_proofs); proofs is a list item
/// per proof, as proofs_section writes them.
pub const CARRIED_PROOFS_NOTE: &str = concat!(
    "Proofs that failed against the change in an earlier round were run again against this round's change, with ",
    "`{command}` in {cwd}, each from a copy of its directory under `{cwd}/",
    proof_copies_dir!(),
    "/`, removed ",
    "after each run; a path to a proof in their output is the copy's:\n",
    "\n",
    "{proofs}\n",
    "\n",
    "Weigh a proof that still fails, or now errors, as a finding that stands, unless a report shows the proof ",
    "itself is wrong.",
);

/// Appended to the verdict step's prompt when a turn of the round changed files that an earlier step locked.
pub const LOCKED_NOTE: &str = concat!(
    "This round changed files that step {steps} locked: {paths}. The orchestrator restored them, ",
    "so the change you review does not hold those edits; they are in {diff_paths}. ",
    "If a report says a locked file is wrong, weigh that claim against the diff.",
);

// -- The prompts as a set ---------------------------------------------------------------------------------------

/// Defines Prompt, one variant per built-in text, with the text's constant and the name its prompt file takes.
macro_rules! prompts {
    ($($variant:ident => $constant:ident,)*) => {
        /// One of the texts Prompts holds, named as its constant and its prompt file are.
        #[derive(Clone, Copy, Debug, PartialEq, Eq)]
        pub enum Prompt {
            $($variant,)*
        }

        impl Prompt {
            /// Every prompt, in the order orchestrator.py's Prompts declares them.
            pub const ALL: &[Prompt] = &[$(Prompt::$variant,)*];

            /// The prompt's name: its constant's, and its prompt file's without `.md`.
            pub fn name(self) -> &'static str {
                match self {
                    $(Prompt::$variant => stringify!($constant),)*
                }
            }

            /// The built-in text.
            pub fn built_in(self) -> &'static str {
                match self {
                    $(Prompt::$variant => $constant,)*
                }
            }
        }
    };
}

prompts! {
    SpecPrompt => SPEC_PROMPT,
    BuildPrompt => BUILD_PROMPT,
    FixPrompt => FIX_PROMPT,
    ReviewPrompt => REVIEW_PROMPT,
    RecheckPrompt => RECHECK_PROMPT,
    ContinuePrompt => CONTINUE_PROMPT,
    RetryVerdictPrompt => RETRY_VERDICT_PROMPT,
    BlockedNote => BLOCKED_NOTE,
    BlockedPrompt => BLOCKED_PROMPT,
    RebuildNote => REBUILD_NOTE,
    RereviewNote => REREVIEW_NOTE,
    QualityFixPrompt => QUALITY_FIX_PROMPT,
    QualityPassedNote => QUALITY_PASSED_NOTE,
    QualityUnresolvedNote => QUALITY_UNRESOLVED_NOTE,
    QualityStuckNote => QUALITY_STUCK_NOTE,
    QualityOffNote => QUALITY_OFF_NOTE,
    ProofsNote => PROOFS_NOTE,
    CarriedProofsNote => CARRIED_PROOFS_NOTE,
    LockedNote => LOCKED_NOTE,
}

impl Prompt {
    /// The prompt of that name; None for a name no prompt has.
    pub fn named(name: &str) -> Option<Prompt> {
        Prompt::ALL.iter().copied().find(|p| p.name() == name)
    }

    fn index(self) -> usize {
        Prompt::ALL.iter().position(|&p| p == self).expect("ALL holds every prompt")
    }
}

/// The texts a run sends agents besides a workflow file's own prompts: the default workflow's steps, the follow-ups
/// and the notes. Each is the built-in text of its Prompt unless a prompt file replaced it (load_prompts). Each is a
/// template whose placeholders are the built-in text's; BLOCKED_NOTE has none.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Prompts {
    /// By Prompt::ALL's order.
    texts: Vec<String>,
}

impl Default for Prompts {
    /// The built-in texts: what a run without prompt files, or saved before them, sends.
    fn default() -> Self {
        Prompts { texts: Prompt::ALL.iter().map(|p| p.built_in().to_string()).collect() }
    }
}

impl Prompts {
    /// The built-in texts, each one overrides names replaced by its text; Err with the least name, by code point,
    /// that names no prompt.
    pub fn from_overrides(overrides: &Map<String>) -> Result<Prompts, String> {
        if let Some(unknown) = overrides.keys().filter(|name| Prompt::named(name).is_none()).min() {
            return Err(unknown.to_string());
        }
        let mut prompts = Prompts::default();
        for (name, text) in overrides.iter() {
            let p = Prompt::named(name).expect("checked above");
            prompts.texts[p.index()] = text.clone();
        }
        Ok(prompts)
    }

    pub fn text(&self, p: Prompt) -> &str {
        &self.texts[p.index()]
    }

    /// Name -> text of each prompt that differs from the built-in text, in Prompt::ALL's order;
    /// Prompts::from_overrides gives these prompts back.
    pub fn overrides(&self) -> Map<String> {
        Prompt::ALL
            .iter()
            .filter(|p| self.text(**p) != p.built_in())
            .map(|p| (p.name(), self.text(*p).to_string()))
            .collect()
    }
}

/// The prompts a run started with, from the overrides saved with it (RunState::prompt_overrides); an error when they
/// name a prompt this orchestrator lacks.
pub fn saved_prompts(overrides: &Map<String>, run_id: &str) -> Result<Prompts, OrchestratorError> {
    Prompts::from_overrides(overrides).map_err(|unknown| {
        OrchestratorError(format!("run state {run_id} replaces prompt {unknown}, which this orchestrator lacks"))
    })
}

// -- Prompt files --------------------------------------------------------------------------------------------------

/// Where a new run finds its prompt files, NAME.md for the Prompt named NAME, on the orchestrator's machine.
pub fn prompts_dir() -> String {
    format!("{}/prompts", config_dir())
}

/// Name -> path of each .md file in dir, unread, in name order; none without the directory. Other files are not
/// prompt files, as an editor's backup is not. Errors: the directory cannot be listed.
pub fn prompt_files(dir: &str) -> Result<Map<String>, OrchestratorError> {
    files_with_extension(dir, ".md")
}

/// Name -> path of each file in dir whose extension (os.path.splitext's) is ext, in name order, the name being the
/// file's without its extension; none without the directory. Errors: the directory cannot be listed. A file whose
/// name is not UTF-8 is left out.
pub(crate) fn files_with_extension(dir: &str, ext: &str) -> Result<Map<String>, OrchestratorError> {
    use crate::error::strerror;
    use crate::py::text::splitext;
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Map::new()),
        Err(e) => return Err(OrchestratorError(format!("cannot list {dir}: {}", strerror(&e)))),
    };
    let mut names = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| OrchestratorError(format!("cannot list {dir}: {}", strerror(&e))))?;
        if let Ok(name) = entry.file_name().into_string() {
            names.push(name);
        }
    }
    names.sort();
    Ok(names
        .iter()
        .filter_map(|name| {
            let (stem, found) = splitext(name);
            (found == ext).then(|| (stem, format!("{dir}/{name}")))
        })
        .collect())
}

/// The prompts a new run starts with: the built-in texts, each one a prompt file in prompts_dir() names replaced by
/// the file's content, its line ends read as "\n" and one trailing newline left out.
///
/// Errors, naming the file: one that names no Prompt, cannot be read, is not UTF-8, or uses other placeholders than
/// the built-in text (prompt_problem).
pub fn load_prompts() -> Result<Prompts, OrchestratorError> {
    load_prompts_from(&prompts_dir())
}

/// load_prompts, with its prompt files in dir.
pub fn load_prompts_from(dir: &str) -> Result<Prompts, OrchestratorError> {
    use crate::error::strerror;
    let mut overrides = Map::new();
    for (name, path) in prompt_files(dir)? {
        if Prompt::named(&name).is_none() {
            let names = Prompt::ALL.iter().map(|p| p.name());
            let hint = match close_match(&name, names) {
                Some(close) => format!("did you mean {close}.md?"),
                None => {
                    let mut sorted: Vec<&str> = Prompt::ALL.iter().map(|p| p.name()).collect();
                    sorted.sort();
                    format!("the names are {}", sorted.join(", "))
                }
            };
            return Err(OrchestratorError(format!("{path}: {name} names no built-in prompt; {hint}")));
        }
        let bytes = std::fs::read(&path)
            .map_err(|e| OrchestratorError(format!("cannot read prompt file {path}: {}", strerror(&e))))?;
        let text = String::from_utf8(bytes)
            .map_err(|e| OrchestratorError(format!("{path}: not UTF-8 text: {e}")))?
            .replace("\r\n", "\n")
            .replace('\r', "\n");
        let text = text.strip_suffix('\n').unwrap_or(&text).to_string();
        if let Some(problem) = prompt_problem(&name, &text) {
            return Err(OrchestratorError(format!("{path}: {problem}")));
        }
        overrides.insert(name, text);
    }
    Ok(Prompts::from_overrides(&overrides).expect("each name was checked"))
}

/// What stops text from replacing the built-in prompt of that name, a Prompt's, or None: a placeholder the built-in
/// text lacks or a missing one it has, or a brace, format spec or conversion that cannot be filled in.
pub fn prompt_problem(name: &str, text: &str) -> Option<String> {
    let built_in = Prompt::named(name)?.built_in();
    let used = match field_names(text) {
        Ok(names) => names,
        Err(e) => return Some(format!("{e}; write a literal brace as {{{{ or }}}}")),
    };
    let mut wanted = field_names(built_in).expect("a built-in text parses");
    wanted.sort();
    wanted.dedup();
    let takes = if wanted.is_empty() {
        format!("{name} takes none")
    } else {
        let list: Vec<String> = wanted.iter().map(|p| format!("{{{p}}}")).collect();
        format!("{name} takes {}", list.join(", "))
    };
    if let Some(extra) = used.iter().filter(|p| !wanted.contains(p)).min() {
        return Some(format!("placeholder {{{extra}}} is not one of the built-in text's; {takes}"));
    }
    if let Some(missing) = wanted.iter().filter(|p| !used.contains(p)).min() {
        return Some(format!("placeholder {{{missing}}} is missing; {takes}"));
    }
    let empty = |field: &str| wanted.iter().any(|w| w == field).then(String::new);
    match crate::py::format::format(text, &empty) {
        Ok(_) => None,
        Err(e) => Some(format!("cannot be filled in: {}", e.named())),
    }
}

#[cfg(test)]
mod tests;
