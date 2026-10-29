//! Whether a run is driven by a live orchestrator, as its state.json tells: what `list` shows of each run, and what
//! keeps two runs from sharing one checkout (Workflow's check_alone).

use crate::constants::{HEARTBEAT_SECONDS, STALE_SECONDS};
use crate::pipeline::DONE;
use crate::py::json::Json;
use crate::summary::format_age;

/// A run an orchestrator still drives, as far as its heartbeat tells.
pub const RUNNING: &str = "running";
/// An unfinished run nothing drives any more: its orchestrator died, or its machine was lost.
pub const STALE: &str = "stale";

/// (RUNNING or STALE, why) for an unfinished run; None for a finished or failed one, which nothing drives.
///
/// state is the run's saved state.json; age is the seconds since it was written, which a live owner does every
/// HEARTBEAT_SECONDS. The owner's pid can be checked only on its own host, local_host, by pid_alive. Host names can
/// collide, so a dead pid counts only once a beat is late too; a live pid proves nothing, since pids are reused, so
/// the age still applies to it. An owner's pid that is not a whole number is not checked.
pub fn run_health(state: &Json, age: u64, local_host: &str, pid_alive: &dyn Fn(i128) -> bool) -> Option<(&'static str, String)> {
    if state.get("phase").and_then(Json::as_str) == Some(DONE) || state.get("error").is_some_and(Json::truthy) {
        return None;
    }
    let owner = state.get("owner").filter(|o| o.truthy());
    let field = |key: &str| owner.and_then(|o| o.get(key)).map_or("None".to_string(), Json::python_str);
    let pid = owner.and_then(|o| o.get("pid")).and_then(Json::as_int);
    let local = owner.and_then(|o| o.get("host")).and_then(Json::as_str) == Some(local_host);
    if local && age > HEARTBEAT_SECONDS && pid.is_some_and(|pid| !pid_alive(pid)) {
        return Some((STALE, format!("orchestrator pid {} exited", field("pid"))));
    }
    let beat = format_age(age as i64);
    if age > STALE_SECONDS {
        return Some((STALE, format!("no heartbeat for {beat}")));
    }
    if owner.is_some() {
        return Some((RUNNING, format!("pid {} on {}, beat {beat} ago", field("pid"), field("host"))));
    }
    Some((RUNNING, format!("beat {beat} ago")))
}

/// Whether a process of that id exists on this machine, under this user or another, as kill(pid, 0) tells; false for
/// an id beyond a pid's range. As in Python, an id of 0 or below is not a process's: it names a process group (0 this
/// one's, -1 every process this one may signal, -N group N), which counts as alive when kill finds a process in it.
pub fn pid_alive(pid: i128) -> bool {
    use nix::errno::Errno;
    use nix::sys::signal::kill;
    use nix::unistd::Pid;
    let Ok(pid) = i32::try_from(pid) else { return false };
    match kill(Pid::from_raw(pid), None) {
        Ok(()) => true,
        // It exists, under another user.
        Err(Errno::EPERM) => true,
        Err(_) => false,
    }
}

/// This machine's name, as socket.gethostname gives it: what an owner record's host holds, and what run_health
/// compares it with. "localhost" when the system cannot say.
pub fn local_host() -> String {
    nix::unistd::gethostname().map_or_else(|_| "localhost".to_string(), |name| name.to_string_lossy().into_owned())
}

#[cfg(test)]
mod tests;
