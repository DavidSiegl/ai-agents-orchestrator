//! Bodies whose digests gate-rust makes equal or tells apart.

fn original() {
    let total = 1 + 2;
    total
}

fn relaid() { let total=1+2;
    // A comment changes no token.
    total }

fn edited() {
    let total = 1 + 3;
    total
}

trait Bodiless {
    fn declared();
}
