# parse_duration adds up a duration of several units wrongly

## Goal

`parse_duration` gets every duration of more than one unit wrong. Fix it.

## Reproducer

```sh
python -c 'from durations import parse_duration; print(parse_duration("1h30m"))'
```

prints `11400`; one hour and thirty minutes are `5400` seconds. Durations of one unit, such as `45s` or `2m`, come
out right.

## Requirements

- `parse_duration` returns the right number of seconds for any number of units in any order, such as `1h30m`,
  `2h5m10s` and `10s1h`.
- What it rejects today it still rejects with `ValueError`: the empty string, a number without a unit, a unit
  without a number, any other character.
- `format_duration` and the module's interface stay as they are.
- A regression test in `tests/` with the reproducer's duration, run by `python -m unittest`.
