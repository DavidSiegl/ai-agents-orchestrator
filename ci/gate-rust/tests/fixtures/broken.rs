//! A file that does not parse: the second fn's let has no value.

fn fine() {}

fn broken() {
    let x = ;
}
