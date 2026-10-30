//! Tests' attributes, and the inline modules cfg_test marks or does not.

#[cfg(test)]
mod plain {
    #[test]
    fn passes() {}

    /// Documented.
    #[test]
    #[ignore = "slow"]
    #[should_panic(expected = "boom")]
    #[cfg(windows)]
    fn weighed() {}

    #[tokio::test]
    async fn awaits() {}

    mod deeper {}
}

#[cfg(all(unix, test))]
mod unix_tests {}

mod inner_cfg {
    #![cfg(test)]
}

#[cfg(any(test, feature = "x"))]
mod any_test {}

#[cfg(not(test))]
mod not_test {}

#[cfg_attr(test, derive(Debug))]
mod attr_only {}

mod declared;
