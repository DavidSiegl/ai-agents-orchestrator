//! gh, as a world plays it (octoflow_e2e::fakes): `fake-gh WORLD ARGS...`.

fn main() {
    let (ledger, args) = octoflow_e2e::fakes::world_and_args();
    std::process::exit(octoflow_e2e::fakes::gh(&ledger, &args));
}
