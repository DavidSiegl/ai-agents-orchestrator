//! The process tree, as /proc shows it: which processes are an engine invocation's. Linux only, as the engine's other
//! process tests.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;

/// The fields of /proc/<pid>/stat after the command name, from the state on; None when pid has ended and been
/// reaped, its stat gone, whether before it was opened or while it was read (gone). Errors: a stat that cannot be read
/// for another reason.
fn stat_fields(pid: i32) -> std::io::Result<Option<Vec<String>>> {
    let stat = match fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => stat,
        Err(e) if gone(&e) => return Ok(None),
        Err(e) => return Err(e),
    };
    // The command name, field 2, is in parentheses and may hold spaces and parentheses itself.
    Ok(stat.rsplit_once(')').map(|(_, rest)| rest.split_whitespace().map(str::to_string).collect()))
}

/// Whether e, from reading a process's stat, says the process is gone: no stat to open, or ESRCH, a process reaped
/// between the stat's open and its read (proc_single_show in the kernel's fs/proc/base.c).
fn gone(e: &std::io::Error) -> bool {
    e.kind() == std::io::ErrorKind::NotFound || e.raw_os_error() == Some(nix::errno::Errno::ESRCH as i32)
}

/// The parent of process pid, from /proc/<pid>/stat; None when pid has ended or its stat cannot be read.
pub fn parent(pid: i32) -> Option<i32> {
    stat_fields(pid).ok()??.get(1)?.parse().ok()
}

/// Whether process pid has ended: gone from /proc, or a zombie its parent has not reaped yet. Errors: a stat that
/// cannot be read but for its being gone (stat_fields), such as one /proc's hidepid hides.
pub fn ended(pid: i32) -> std::io::Result<bool> {
    Ok(stat_fields(pid)?.is_none_or(|fields| fields.first().map(String::as_str) == Some("Z")))
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

/// pid and every process below it, as /proc lists them now; the error of listing /proc when it cannot be opened or
/// read to its end, rather than a set that may lack processes.
pub fn descendants(pid: i32) -> std::io::Result<BTreeSet<i32>> {
    let mut children: BTreeMap<i32, Vec<i32>> = BTreeMap::new();
    for entry in fs::read_dir("/proc")? {
        let Some(child) = entry?.file_name().to_str().and_then(|name| name.parse::<i32>().ok()) else { continue };
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_running_process_has_not_ended_and_a_reaped_one_has() {
        let me = i32::try_from(std::process::id()).expect("a pid fits an i32");
        assert!(!ended(me).expect("reading this process's stat"));
        let mut child = std::process::Command::new("true").spawn().expect("starting true");
        let pid = i32::try_from(child.id()).expect("a pid fits an i32");
        child.wait().expect("waiting for true");
        assert!(ended(pid).expect("reading a reaped process's stat"));
        assert_eq!(parent(me), i32::try_from(std::os::unix::process::parent_id()).ok());
    }

    #[test]
    fn a_process_reaped_while_its_stat_is_read_is_gone() {
        assert!(gone(&std::io::Error::from_raw_os_error(nix::errno::Errno::ESRCH as i32)));
        assert!(gone(&std::io::Error::from(std::io::ErrorKind::NotFound)));
        assert!(!gone(&std::io::Error::from_raw_os_error(nix::errno::Errno::EACCES as i32)));
    }

    #[test]
    fn a_zombie_has_ended() {
        let child = std::process::Command::new("true").spawn().expect("starting true");
        let pid = i32::try_from(child.id()).expect("a pid fits an i32");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while !ended(pid).expect("reading the child's stat") {
            assert!(std::time::Instant::now() < deadline, "true still runs after 5 s");
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert!(stat_fields(pid).expect("reading the zombie's stat").is_some(), "the unreaped child is gone");
        drop(child);
    }
}
