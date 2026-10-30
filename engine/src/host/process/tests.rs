use super::*;
use crate::test_support::{oracle, TempDir};

#[test]
fn a_timeout_is_worded_as_python_words_it() {
    let argv: Vec<String> = ["sh", "-c", "it's", "_", "é\n"].map(String::from).into();
    let recorded = oracle("host.json");
    assert_eq!(timed_out(&argv, 60), recorded.get("timeout").and_then(crate::py::json::Json::as_str).unwrap());
}

#[test]
fn no_control_directory_where_ssh_could_not_use_one() {
    let d = TempDir::new();
    // Missing, too long for a socket's path, or with what ssh's option parser takes apart.
    let long = format!("/tmp/{}", "x".repeat(80));
    for parent in [d.join("missing"), long, "/tmp/a b".into(), "/tmp/%h".into(), String::new()] {
        assert_eq!(new_ssh_control_dir(&parent), None, "{parent}");
    }
    let made = new_ssh_control_dir("/tmp").expect("a directory in /tmp");
    assert!(made.starts_with("/tmp/octoflow-ssh-") && made.len() == "/tmp/octoflow-ssh-".len() + 8, "{made}");
    assert!(private_dir(&made));
    std::fs::remove_dir(&made).unwrap();
    assert!(!private_dir(&made));
}

#[test]
fn names_differ_from_call_to_call() {
    let names: std::collections::HashSet<String> = (0..50).map(unguessable_name).collect();
    assert_eq!(names.len(), 50);
    let mkdtemp_like = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_';
    assert!(names.iter().all(|n| n.len() == 8 && n.bytes().all(mkdtemp_like)));
}

#[test]
fn without_a_usable_control_path_a_command_runs_on_a_connection_of_its_own() {
    // OpenSSH 10 fails a command whose ControlPath it cannot bind rather than connect without sharing, so then no
    // option is passed (TestHost's case of the same name).
    assert_eq!(ssh_argv("remote-host", vec![], &["true"]), ["ssh", "-o", "BatchMode=yes", "remote-host", "true"]);
    let options = vec!["-o".to_string(), "ControlMaster=auto".to_string()];
    let argv = ssh_argv("remote-host", options, &["sh", "-c", "a b"]);
    assert_eq!(argv, ["ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=auto", "remote-host", "sh -c 'a b'"]);
}

#[test]
fn a_host_over_ssh_says_where() {
    let host = ProcessHost::local(&Interrupt::new());
    assert_eq!(host.ssh_target(), None);
    assert_eq!(host.over_ssh("me@box").ssh_target(), Some("me@box"));
    // An empty target is this machine, as Python's Host takes it.
    assert_eq!(host.over_ssh("").ssh_target(), None);
    let secret = ProcessHost::in_environment([("GH_TOKEN".into(), "token-value".into())].into_iter(), &Interrupt::new());
    let shown = format!("{:?}", secret.over_ssh("box"));
    assert!(shown.contains("box") && !shown.contains("token-value"), "{shown}");
    let vars = [("A".into(), "1".into()), ("A".into(), "2".into())];
    assert_eq!(ProcessHost::in_environment(vars.into_iter(), &Interrupt::new()).var("A").as_deref(), Some("2"));
}
