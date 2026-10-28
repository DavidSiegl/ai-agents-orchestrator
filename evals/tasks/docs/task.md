# Document kvstore: its usage and its functions

## Goal

`kvstore`'s README says what it is but not how to use it, and the functions in `kvstore/store.py` and
`kvstore/__main__.py` have no docstrings. Document both. This is a documentation change: the code must not change.

## Requirements

- A `## Usage` section in `README.md` that documents each command of `python -m kvstore`: `get`, `set`, `delete` and
  `list`, each with an example command line in a fenced code block, and the `--file` option and its default.
- It says what each command prints, and that `get` and `delete` exit with status 1 when the key does not exist.
- A docstring on every function in `kvstore/store.py` and `kvstore/__main__.py`, the private ones included, and on
  the module `kvstore/__main__.py`, saying what it does, its arguments and its return value.
- No change to any statement of the code: only docstrings are added, and the tests still pass with
  `python -m unittest`.
