# An aligned table: --format table, without a new dependency

## Goal

`report FILE` writes a CSV file back as CSV. Add a table format a person can read in a terminal.

## Requirements

- `pub fn render_table(header: &[String], rows: &[Vec<String>]) -> String` in `src/table.rs`, exported from the
  crate's root as `report::render_table`: `header` is the column names and `rows` the rows of cells, as `read_rows`
  returns them. It returns the table as text:
  - a line for the header, a separator line, then a line per row; each line ends with `\n`;
  - the cells of a line joined by ` | `, each padded to its column's width, the longest of its column's header and
    cells, in characters;
  - the separator is a run of `-` as long as each column's width, the runs joined by `-+-`;
  - a column whose cells are all numbers (what `str::parse::<f64>` accepts) and which has at least one row is
    right-aligned, its header too; every other column is left-aligned;
  - no line ends with a space.
- `report --format table FILE` prints `render_table` of the file. `--format csv`, the default, stays as it is.
- Tests in `tests/`, run by `cargo test`.

## Constraint

Do not add a dependency: `Cargo.toml` lists none in any dependency table, and the crate uses only the standard
library and itself. No `comfy-table`, no `tabled`, no `prettytable`.

## Example

For the header `name, qty, price` and the rows `apple, 3, 1.50` and `banana, 12, 0.25`:

```text
name   | qty | price
-------+-----+------
apple  |   3 |  1.50
banana |  12 |  0.25
```
