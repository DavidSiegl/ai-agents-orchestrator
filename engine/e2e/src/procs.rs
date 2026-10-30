//! The process tree, as /proc shows it: which processes are an engine invocation's. Linux only, as the engine's other
//! process tests.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;

/// The parent of process pid, from /proc/<pid>/stat; None when pid has ended or its stat cannot be read.
pub fn parent(pid: i32) -> Option<i32> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // The command name, field 2, is in parentheses and may hold spaces; the parent is the second field after it.
    stat.rsplit_once(')')?.1.split_whitespace().nth(1)?.parse().ok()
}

/// The pids of this process's ancestors, its parent first, up to but not including init.
pub fn ancestors() -> Vec<i32> {
    let mut chain = Vec::new();
    let mut pid = i32::try_from(std::os::unix::process::parent_id()).unwrap_or(1);
    while pid > 1 && !chain.contains(&pid) {
        chain.push(pid);
        pid = parent(pid).unwrap_or(1);
    }
    chain
}

/// pid and every process below it, as /proc lists them now; the error of listing /proc when it cannot be.
pub fn descendants(pid: i32) -> std::io::Result<BTreeSet<i32>> {
    let mut children: BTreeMap<i32, Vec<i32>> = BTreeMap::new();
    let entries = fs::read_dir("/proc")?;
    for entry in entries.flatten() {
        let Some(child) = entry.file_name().to_str().and_then(|name| name.parse::<i32>().ok()) else { continue };
        // A process that ended since the listing has no parent to read.
        if let Some(p) = parent(child) {
            children.entry(p).or_default().push(child);
        }
    }
    let mut found = BTreeSet::new();
    let mut todo = vec![pid];
    while let Some(p) = todo.pop() {
        if found.insert(p) {
            todo.extend(children.get(&p).into_iter().flatten());
        }
    }
    Ok(found)
}
