//! The hidden acceptance test of the eval task `no-dependency`: `render_table` and `report --format table`, with no
//! dependency added (task.md). `cargo xtask eval` copies it into the clone's tests/ after the run and runs it with the
//! crate's own tests (xtask/src/eval.rs).

use std::path::Path;
use std::process::Command;

use report::render_table;

const TABLE: &str = "name   | qty | price\n-------+-----+------\napple  |   3 |  1.50\nbanana |  12 |  0.25\n";

fn strings(cells: &[&str]) -> Vec<String> {
    cells.iter().map(|cell| cell.to_string()).collect()
}

fn table(header: &[&str], rows: &[&[&str]]) -> String {
    let rows: Vec<Vec<String>> = rows.iter().map(|row| strings(row)).collect();
    render_table(&strings(header), &rows)
}

fn crate_file(path: &str) -> Option<String> {
    std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join(path)).ok()
}

#[test]
fn the_tasks_example() {
    assert_eq!(table(&["name", "qty", "price"], &[&["apple", "3", "1.50"], &["banana", "12", "0.25"]]), TABLE);
}

#[test]
fn alignment_rules() {
    assert_eq!(
        table(&["id", "a long header"], &[&["1", "x"], &["n/a", ""]]),
        "id  | a long header\n----+--------------\n1   | x\nn/a |\n"
    );
    assert_eq!(table(&["n"], &[&["1e3"], &["-2.5"]]), "   n\n----\n 1e3\n-2.5\n");
    assert_eq!(table(&["only", "header"], &[]), "only | header\n-----+-------\n");
}

#[test]
fn the_command_line() {
    let dir = std::env::temp_dir().join(format!("report-hidden-test-{}", std::process::id()));
    std::fs::create_dir_all(&dir).expect("its directory is made");
    let path = dir.join("data.csv");
    std::fs::write(&path, "name,qty,price\napple,3,1.50\nbanana,12,0.25\n").expect("data.csv is written");
    let report = |args: &[&str]| Command::new(env!("CARGO_BIN_EXE_report")).args(args).arg(&path).output();
    let (table, csv) = (report(&["--format", "table"]).expect("report runs"), report(&[]).expect("report runs"));
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(table.status.code(), Some(0), "{}", String::from_utf8_lossy(&table.stderr));
    assert_eq!(String::from_utf8_lossy(&table.stdout), TABLE);
    assert_eq!(csv.status.code(), Some(0), "{}", String::from_utf8_lossy(&csv.stderr));
    assert_eq!(String::from_utf8_lossy(&csv.stdout), "name,qty,price\napple,3,1.50\nbanana,12,0.25\n");
}

/// Until the engine's new-dependencies check read Cargo manifests, this was the task's only guard, and it stays the
/// task's own: every table of Cargo.toml whose name ends in `dependencies` is empty, and Cargo.lock, which the build
/// writes, holds no package but report.
#[test]
fn no_dependency() {
    let manifest = crate_file("Cargo.toml").expect("Cargo.toml reads");
    let mut table = String::new();
    for line in manifest.lines().map(str::trim).filter(|line| !line.is_empty() && !line.starts_with('#')) {
        if line.starts_with('[') {
            table = line.trim_matches(|c| c == '[' || c == ']').trim().to_string();
            assert!(!table.starts_with("patch") && table != "replace", "Cargo.toml has a [{table}] table");
        } else {
            assert!(!table.ends_with("dependencies"), "Cargo.toml's [{table}] lists {line}");
            assert!(!line.contains("dependencies"), "Cargo.toml's [{table}] has {line}");
        }
    }
    if let Some(lock) = crate_file("Cargo.lock") {
        let names: Vec<&str> = lock.lines().filter_map(|line| line.strip_prefix("name = ")).collect();
        assert_eq!(names, ["\"report\""], "Cargo.lock holds other packages");
    }
}
