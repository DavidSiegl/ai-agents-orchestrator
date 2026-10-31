//! `cargo xtask <command> [ARGS...]`: the repository's chores in Rust (the xtask library). The commands are `eval`,
//! the eval set's runner, and `bundles`, which builds its tasks' bundles; each says its arguments and exit status.

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let status = match args.split_first() {
        Some((name, rest)) if name == "eval" => xtask::eval::main(rest),
        Some((name, rest)) if name == "bundles" => xtask::bundles::main(rest),
        _ => {
            eprintln!("usage: cargo xtask <command> [ARGS...], the command eval or bundles");
            2
        }
    };
    std::process::exit(status);
}
