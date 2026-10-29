//! The Interrupt as a value: raised, caught and raised again, its sleeps and the commands' watches. Its SIGINT
//! (on_sigint) is engine/tests/sigint.rs's, in processes of its own.

use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use super::*;

#[test]
fn raised_until_caught_and_raised_again() {
    let interrupt = Interrupt::new();
    assert!(!interrupt.is_raised());
    let clone = interrupt.clone();
    clone.raise();
    assert!(interrupt.is_raised());
    interrupt.catch();
    assert!(!clone.is_raised());
    clone.raise();
    assert!(interrupt.is_raised());
    assert!(format!("{interrupt:?}").contains("raised: 2"));
}

#[test]
fn a_sleep_ends_at_a_raise() {
    let interrupt = Interrupt::new();
    let start = Instant::now();
    interrupt.sleep(Duration::from_millis(50));
    assert!(start.elapsed() >= Duration::from_millis(50));
    let raiser = interrupt.clone();
    let raising = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(100));
        raiser.raise();
    });
    let start = Instant::now();
    interrupt.sleep(Duration::from_secs(600));
    assert!(start.elapsed() < Duration::from_secs(60));
    raising.join().unwrap();
    // Pending, it sleeps not at all; caught, it sleeps again.
    interrupt.sleep(Duration::from_secs(600));
    interrupt.catch();
    let start = Instant::now();
    interrupt.sleep(Duration::from_millis(50));
    assert!(start.elapsed() >= Duration::from_millis(50));
}

#[test]
fn a_watch_hears_each_raise_while_it_lasts() {
    let interrupt = Interrupt::new();
    let woken = std::sync::Arc::new(AtomicU32::new(0));
    let counter = woken.clone();
    let watch = interrupt.watch(move || {
        counter.fetch_add(1, Ordering::SeqCst);
    });
    let watch = watch.expect("not pending");
    interrupt.raise();
    interrupt.catch();
    interrupt.raise();
    assert_eq!(woken.load(Ordering::SeqCst), 2);
    assert!(watch.interrupted());
    // Ended, it hears no more.
    interrupt.raise();
    assert_eq!(woken.load(Ordering::SeqCst), 2);
    // While pending, no command is watched, so none starts; once caught, one is, and one that hears no raise says so.
    assert!(interrupt.watch(|| {}).is_none());
    interrupt.catch();
    assert!(!interrupt.watch(|| {}).expect("caught").interrupted());
}
