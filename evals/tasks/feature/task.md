# The most frequent words: top_words and --top

## Goal

`tally` counts every word of a text. Add a way to ask for only the most frequent ones, from Rust and from the
command line.

## Requirements

- `pub fn top_words(text: &str, n: usize) -> Vec<(String, usize)>` in `src/lib.rs`: the `n` most frequent words of
  `text` with their counts, most frequent first, words as frequent in alphabetical order. Words are what `words`
  finds. Fewer than `n` words gives them all; `n` of 0 gives an empty list.
- `tally --top N` prints only the first `N` lines of what `tally` prints, in the same format: the count, a space and
  the word. `N` must be a positive integer; anything else is a usage error, exit status 2, with a message on standard
  error. Without `--top`, the output stays as it is.
- Tests for both in `tests/`, run by `cargo test`.

## Out of scope

Any change to what counts as a word.
