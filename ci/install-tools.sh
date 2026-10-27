#!/bin/sh
# Put the pinned releases below into <dir> and exit 0; exit non-zero, saying why on standard error, when one cannot be
# downloaded or its archive does not match its pinned sha256. Jenkins's Install stage runs it with a directory the
# Jenkinsfile puts first on PATH, for tools the agent image does not provide:
#
# - gitleaks, which the parity scenarios (tests/parity) run for real, failing where there is none. Its version is the
#   one the parity recordings were made with.
# - cargo-llvm-cov and cargo-nextest, with which the Test stage runs the Rust tests and measures their coverage
#   (docs/quality-gate.md). cargo-llvm-cov also needs rustup's llvm-tools-preview component, which the Install stage
#   adds itself.
#
# Each is pinned to the sha256 of its linux x86_64 archive as its release publishes it: gitleaks's in
# gitleaks_<version>_checksums.txt, cargo-nextest's in the .sha256 file beside the archive, and cargo-llvm-cov's as the
# asset's digest on its GitHub release page, which has no checksums file. The musl builds of the two cargo tools are
# static, so they run whatever C library the agent image has.
set -eu

[ $# -eq 1 ] || { echo "usage: install-tools.sh <dir>" >&2; exit 2; }
dir=$1

archive=$(mktemp)
trap 'rm -f "$archive"' EXIT

# install NAME VERSION URL SHA256: download the .tar.gz at URL, check it against SHA256, and extract the executable
# NAME from its top level into $dir.
install() {
    curl -fsSL -o "$archive" "$3"
    echo "$4  $archive" | sha256sum -c --quiet - || {
        echo "install-tools.sh: the $1 $2 archive does not match its pinned sha256" >&2
        exit 1
    }
    tar -xzf "$archive" -C "$dir" "$1"
}

mkdir -p "$dir"

version=8.30.1
install gitleaks "$version" \
    "https://github.com/gitleaks/gitleaks/releases/download/v$version/gitleaks_${version}_linux_x64.tar.gz" \
    551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
"$dir/gitleaks" version

version=0.9.1
release=https://github.com/taiki-e/cargo-llvm-cov/releases/download/v$version
install cargo-llvm-cov "$version" "$release/cargo-llvm-cov-x86_64-unknown-linux-musl.tar.gz" \
    3fca950394a3c49457657c158b1619cec8dfd2647ae5b48746734c0ab969a522
"$dir/cargo-llvm-cov" llvm-cov --version

version=0.9.148
release=https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-$version
install cargo-nextest "$version" "$release/cargo-nextest-$version-x86_64-unknown-linux-musl.tar.gz" \
    bf92cb81450872d960ffa13857a83084d557ec0cdc141663d263bb23ef756f97
"$dir/cargo-nextest" nextest --version
