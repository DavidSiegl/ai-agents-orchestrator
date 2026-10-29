//! The real run's clocks and human: Python's Clocks defaults and notify_locally.

use std::time::{Duration, Instant};

use super::*;
use crate::host::tests::{environment, tool};
use crate::test_support::TempDir;

#[test]
fn the_clocks_are_the_systems() {
    let interrupt = Interrupt::new();
    let clocks = SystemClocks::new(&interrupt).unwrap();
    let wall = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64();
    assert!((clocks.wall() - wall).abs() < 5.0);
    let now = clock_gettime(ClockId::CLOCK_MONOTONIC).unwrap();
    assert!((clocks.monotonic() - now.tv_sec() as f64).abs() < 5.0);
    let (start, before) = (Instant::now(), clocks.monotonic());
    clocks.sleep(0.05);
    assert!(start.elapsed() >= Duration::from_millis(50));
    assert!(clocks.monotonic() - before >= 0.05);
    // The Ctrl-C cuts a sleep short, a sleep of no time is none, and neither is one of a time that is not one.
    interrupt.raise();
    let start = Instant::now();
    clocks.sleep(600.0);
    interrupt.catch();
    clocks.sleep(-1.0);
    clocks.sleep(f64::NAN);
    assert!(start.elapsed() < Duration::from_secs(60));
}

#[test]
fn the_human_is_notified_through_the_local_herdr() {
    let d = TempDir::new();
    let args = d.join("args");
    tool(&d.path(), "herdr", &format!("#!/bin/sh\necho \"$@\" > '{args}'\necho '{{\"result\": {{}}}}'\n"));
    let herdr = ProcessHerdr::in_environment(None, environment(Some(&d.path()), &[]).into_iter(), &Interrupt::new());
    let human = LocalHuman { herdr };
    human.notify("Run finished: APPROVE", "the task");
    assert_eq!(std::fs::read_to_string(&args).unwrap(), "notification show Run finished: APPROVE --body the task --sound request\n");
    // A notification that fails costs only itself.
    tool(&d.path(), "herdr", "#!/bin/sh\nexit 3\n");
    human.notify("t", "b");
    human.log("a line");
    assert!(format!("{:?}", LocalHuman::new(&Interrupt::new())).contains("machine: None"));
}
