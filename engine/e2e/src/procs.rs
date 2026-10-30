//! The machine's processes as /proc lists them: whose child each is.

use std::collections::BTreeSet;
use std::fs;

/// The parent of process pid; None when pid has ended, or /proc shows no parent for it.
pub fn parent(pid: i64) -> Option<i64> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // The command name, field 2, is in parentheses and may hold spaces; the parent is the second field after it.
    stat.rsplit_once(')')?.1.split_whitespace().nth(1)?.parse().ok()
}

/// pid and every process below it, its children and theirs, as /proc lists them now; a process that ends during the
/// listing may be in it or not. Panics where /proc cannot be listed.
pub fn descendants(pid: i64) -> BTreeSet<i64> {
    let pids: Vec<i64> = fs::read_dir("/proc")
        .unwrap_or_else(|e| panic!("listing /proc: {e}"))
        .filter_map(|e| e.ok()?.file_name().to_str()?.parse().ok())
        .collect();
    let parents: Vec<(i64, i64)> = pids.into_iter().filter_map(|p| Some((p, parent(p)?))).collect();
    let mut found = BTreeSet::from([pid]);
    let mut todo = vec![pid];
    while let Some(p) = todo.pop() {
        for (child, _) in parents.iter().filter(|(_, of)| *of == p) {
            if found.insert(*child) {
                todo.push(*child);
            }
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_child_is_below_its_parent() {
        let mut child = std::process::Command::new("sleep").arg("5").spawn().expect("sleep");
        let me = i64::from(std::process::id());
        let pid = i64::from(child.id());
        assert_eq!(parent(pid), Some(me));
        assert!(descendants(me).contains(&pid));
        assert!(!descendants(pid).contains(&me));
        child.kill().expect("killing sleep");
        child.wait().expect("reaping sleep");
    }
}
