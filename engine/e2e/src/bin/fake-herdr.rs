//! herdr, as a world plays it (octoflow_e2e::fakes): `fake-herdr WORLD ARGS...`.

fn main() {
    let (ledger, args) = octoflow_e2e::fakes::world_and_args();
    std::process::exit(octoflow_e2e::fakes::herdr(&ledger, &args));
}
