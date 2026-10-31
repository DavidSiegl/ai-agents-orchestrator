//! The fake ssh a world puts on the engine's PATH (octoflow_e2e::fakes).

fn main() {
    octoflow_e2e::fakes::main(|_, args| octoflow_e2e::fakes::ssh(args))
}
