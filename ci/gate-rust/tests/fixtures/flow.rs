//! A fn per kind of the schema's flow nodes, and syntax that is none.

fn branches(a: bool, b: Option<u8>) {
    if a {
        one();
    } else if let Some(n) = b {
        two(n);
    } else {
        if a {}
    }
}

fn let_else(b: Option<u8>) {
    let Some(n) = pick(b) else { return };
}

fn matches(b: Option<u8>) {
    match b {
        Some(n) if n > 1 => {}
        _ => self.other(),
    }
}

fn loops(items: &[u8]) {
    'outer: loop {
        while let Some(i) = next() {
            for item in items.iter() {
                break 'outer;
            }
            continue;
        }
        break 7;
    }
}

fn logic(a: bool, b: bool, c: bool) -> bool {
    a && (b || c) && !(a || b) && f(a && b)
}

fn holders() {
    let add = |x: u8| if x > 1 { 1 } else { 2 };
    fn inner() {
        inner();
    }
    struct Local;
    impl Local {
        fn method(&self) {
            Self::method(self);
        }
    }
}

fn not_flow(x: Result<u8, ()>) -> Result<u8, ()> {
    let v = unsafe { async { x? } };
    g::<u8>(x?).map(|_| 0);
    println!("{}", if true { 1 } else { 2 });
    tokio::select! { _ = h() => if true {} }
    Ok(v)
}
