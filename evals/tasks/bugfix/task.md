# parse_duration adds up a duration of several units wrongly

## Goal

`parse_duration` gets every duration of more than one unit wrong. Fix it.

## Reproducer

```rust
assert_eq!(durations::parse_duration("1h30m"), Ok(5400));
```

fails: `parse_duration` returns `Ok(11400)`, but one hour and thirty minutes are `5400` seconds. Durations of one
unit, such as `45s` or `2m`, come out right.

## Requirements

- `parse_duration` returns the right number of seconds for any number of units in any order, such as `1h30m`,
  `2h5m10s` and `10s1h`.
- What it rejects today it still rejects with `NotADuration`: the empty string, a number without a unit, a unit
  without a number, any other character.
- `format_duration` and the crate's public interface stay as they are.
- A regression test in `tests/` that calls `parse_duration` with the reproducer's duration, run by `cargo test`.
