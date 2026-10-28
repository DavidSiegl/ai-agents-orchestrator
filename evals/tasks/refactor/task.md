# One home for the invoice's rounding and formatting

## Goal

`render_invoice` and `render_credit_note` in `invoice/render.py` repeat each other: the rounding to the cent, the
item line, the tax percentage and the summary lines are written out twice, and the rounding five times. Refactor so
that each is written once. The output must not change.

## Requirements

- One function in `render.py`, `money(amount)`, rounds an amount to the cent, half up, returning a `Decimal`; every rounding in
  `render.py` goes through it, and `render.py` calls `quantize` nowhere else.
- One function, `item_line(name, quantity, price)`, returns an item's line as both renderers print it, without
  its line ending, for the price as given (the credit note passes its negated price).
- `render_invoice` and `render_credit_note` keep their names, arguments and output, byte for byte, for every input
  they accept today, an empty list of items included.
- The existing tests pass unchanged; add tests for `money` and `item_line`. Run them with `python -m unittest`.

## Out of scope

Any change to the output's format, or to the module's other names.
