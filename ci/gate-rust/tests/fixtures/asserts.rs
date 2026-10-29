//! The assertion macros a fn calls, wherever in its body gate-rust looks.

fn asserts() {
    assert!(ready);
    assert_eq!(count, -2, "count {}", r#"raw "quoted""#);
    assert_ne!(name, b'x');
    debug_assert!(flag);
    debug_assert_eq!(a, 1.5);
    debug_assert_ne!(
        list,
        vec![1, 2],
    );
    pretty_assertions::assert_eq!(left, "right");
    assert!(matches!(found, Some(3) | None));
    panic!("never {}", 'c');
    let check = || assert!(inside_closure);
    let checks = vec![assert_eq!(in_vec, 4)];
    tokio::select! { _ = later => assert!(unseen) }
    println!("not an assertion");
    fn helper() {
        assert!(nested_own);
    }
}

const OUTSIDE: () = assert!(true);
