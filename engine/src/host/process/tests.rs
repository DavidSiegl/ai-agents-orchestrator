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
fn a_host_over_ssh_says_where() {
    let host = ProcessHost::local();
    assert_eq!(host.ssh_target(), None);
    assert_eq!(host.over_ssh("me@box").ssh_target(), Some("me@box"));
    let vars = [("A".into(), "1".into()), ("A".into(), "2".into())];
    assert_eq!(ProcessHost::in_environment(vars.into_iter()).var("A").as_deref(), Some("2"));
}
