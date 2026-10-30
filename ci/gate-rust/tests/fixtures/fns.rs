//! Every kind of fn gate-rust lists, and those of an extern block and a macro's tokens, which it does not.

/// A free fn, with a doc comment and an attribute above it.
#[inline]
pub fn free() {}

struct Holder;

impl Holder {
    async fn method(&self) {
        fn nested() {}
    }
}

trait Shape {
    fn declared(&self);
    fn defaulted(&self) {
    }
}

impl std::fmt::Debug for Holder {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let _closure = || {
            fn in_closure() {}
        };
        f.write_str("Holder")
    }
}

const TABLE: () = {
    fn in_const() {}
};

mod inner {
    pub(crate) fn
    spread() {
    }
}

fn r#loop() {}

extern "C" {
    fn abs(input: i32) -> i32;
}

macro_rules! make {
    () => {
        fn made() {}
    };
}
