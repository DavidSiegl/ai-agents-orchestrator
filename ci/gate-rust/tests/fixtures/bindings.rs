//! A binding of each kind gate-rust lists.

use std::collections::HashMap as Map;
use std::fmt::Write as _;

struct Point { x: i32, y: i32 }
enum Shape { Dot }
union Bits { int: u32 }
trait Draw { const SIDES: u8; type Pen; fn draw(&self, canvas: u8); }
type Alias = Point;
const LIMIT: usize = 1;
static mut COUNTER: u32 = 0;

impl Draw for Point {
    const SIDES: u8 = 0;
    type Pen = u8;
    fn draw(&self, (r#ref, _): (u8, u8)) {}
}

fn bind(Point { x, y: renamed }: Point, mut count: u32) {
    let (first, ref second) = (1, 2);
    let Some(else_bound) = maybe() else { return };
    let Shape::Dot = shape();
    for (index, item @ 1..=9) in pairs() {}
    match maybe() {
        Some(captured) if let Some(guarded) = other() => {}
        None => {}
        _ => {}
    }
    if let Some(conditional) = maybe() && let Ok(chained) = parsed() {}
    while let Some(looped) = next() {}
    let apply = |arg, (left, right): (u8, u8)| arg;
    if let Some(scrutinee) = { let inner = 1; Some(inner) } {}
}
