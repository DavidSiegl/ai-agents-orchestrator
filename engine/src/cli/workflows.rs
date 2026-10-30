//! The `workflows` command, as orchestrator.py's workflows_command: the workflows `run --workflow` offers and the
//! prompt files a new run would start with, or one workflow as a workflow file. It reads this machine's configuration
//! directory only, so it needs no herdr.

use crate::env::ConfigDir;
use crate::error::OrchestratorError;
use crate::pipeline::{find_workflow, workflow_shape, Pipeline, WORKFLOWS};
use crate::prompts::{load_prompts, Prompts};
use crate::workflow_file::{load_workflow_file, named_workflow, workflow_files, workflow_toml};

/// Print, by print (which writes its text on standard output as it is), the workflow reference names as a workflow
/// file, worded by config's prompt files as a new run would be; without reference, the list print_workflows prints.
/// reference is a workflow's name, or a file's path when it has a / or ends in .toml. Errors: the workflow cannot be
/// found or loaded, or the prompt files or the workflows directory cannot be read.
pub fn workflows_command(config: &ConfigDir, reference: Option<&str>, print: &dyn Fn(&str)) -> Result<(), OrchestratorError> {
    let Some(reference) = reference.filter(|r| !r.is_empty()) else { return print_workflows(config, print) };
    let prompts = load_prompts(config)?;
    let pipeline = if reference.contains('/') || reference.ends_with(".toml") {
        load_workflow_file(reference, &prompts)?
    } else {
        named_workflow(config, reference, &prompts)?
    };
    print(&workflow_toml(&pipeline));
    Ok(())
}

/// Print each workflow `run --workflow` offers, the built-in ones then config's workflow files (one that does not
/// load with the error that keeps it from loading), how to add one, then the prompt files a new run would start with.
/// Errors: the workflows directory cannot be listed.
fn print_workflows(config: &ConfigDir, print: &dyn Fn(&str)) -> Result<(), OrchestratorError> {
    let say = |line: &str| print(&format!("{line}\n"));
    // The shape and description only, which no prompt file changes.
    let built_in = Prompts::default();
    let mut names = WORKFLOWS.to_vec();
    names.sort_unstable();
    for name in names {
        print_workflow(&say, name, "built in", &find_workflow(name, &built_in)?);
    }
    for (name, path) in workflow_files(config)?.iter() {
        match load_workflow_file(path, &built_in) {
            Ok(p) => print_workflow(&say, name, path, &p),
            Err(e) => {
                let prefix = format!("{path}: ");
                say(&format!("{name}  {path}\n    error: {}", e.0.strip_prefix(&prefix).unwrap_or(&e.0)));
            }
        }
    }
    say(&format!(
        "\nAdd one as {}/NAME.toml; `orchestrator.py workflows default` prints the default as a file to start from.",
        config.workflows_dir()
    ));
    print_prompt_overrides(config, &say);
    Ok(())
}

/// A workflow's lines in the list: its name and where it comes from, its shape, and its description if it has one.
fn print_workflow(say: &dyn Fn(&str), name: &str, source: &str, p: &Pipeline) {
    say(&format!("{name}  {source}"));
    say(&format!("    {}", workflow_shape(p)));
    if !p.description().is_empty() {
        say(&format!("    {}", p.description()));
    }
}

/// Print the prompt files of config a new run would start with, one line each, or why it would not start; nothing
/// without any.
fn print_prompt_overrides(config: &ConfigDir, say: &dyn Fn(&str)) {
    let dir = config.prompts_dir();
    let overrides = match load_prompts(config) {
        Ok(prompts) => prompts.overrides(),
        Err(e) => {
            say(&format!("\nPrompt files in {dir}:\n    error: {e}"));
            return;
        }
    };
    if overrides.is_empty() {
        return;
    }
    say(&format!("\nPrompt files in {dir}, in place of the built-in texts:"));
    for name in overrides.keys() {
        say(&format!("    {name}  {}.md", std::path::Path::new(&dir).join(name).display()));
    }
}

#[cfg(test)]
mod tests;
