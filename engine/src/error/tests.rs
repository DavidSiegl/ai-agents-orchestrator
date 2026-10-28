use super::*;

#[test]
fn an_error_shows_its_message() {
    let e = OrchestratorError::new("it failed");
    assert_eq!(e.to_string(), "it failed");
    assert_eq!(e, OrchestratorError("it failed".into()));
    let boxed: Box<dyn std::error::Error> = Box::new(e);
    assert_eq!(boxed.to_string(), "it failed");
}

#[test]
fn strerror_leaves_out_the_os_error_number() {
    let missing = std::fs::read("/nonexistent/file").unwrap_err();
    assert_eq!(strerror(&missing), "No such file or directory");
    let custom = std::io::Error::other("no luck (os error 2)");
    assert_eq!(strerror(&custom), "no luck (os error 2)");
}
