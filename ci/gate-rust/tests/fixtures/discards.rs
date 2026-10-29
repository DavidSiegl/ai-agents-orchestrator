//! The values a fn's body throws away, and statements that keep theirs.

fn discards() {
    let _ = std::panic::catch_unwind(|| run());
    let _: Result<(), ()> = fallible();
    _ = parse("1");
    fallible().ok();
    let kept = fallible().ok();
    let _named = fallible();
    fallible().ok().unwrap();
    fn helper() {
        let _ = own();
    }
}
