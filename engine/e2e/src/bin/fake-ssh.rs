//! ssh, as a world plays it (octoflow_e2e::fakes): `fake-ssh WORLD ARGS...`, its command run here with sh.

fn main() {
    let (_, args) = octoflow_e2e::fakes::world_and_args();
    std::process::exit(octoflow_e2e::fakes::ssh(&args));
}
