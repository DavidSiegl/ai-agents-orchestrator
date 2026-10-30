#!/bin/sh
# Put the pinned releases below into <dir> and exit 0; exit non-zero, saying why on standard error, when one cannot be
# downloaded or its archive does not match its pinned sha256. Jenkins's Install stage runs it with a directory the
# Jenkinsfile puts first on PATH, for tools the agent image does not provide:
#
# - gitleaks, which the parity scenarios (tests/parity) run for real, failing where there is none. Its version is the
#   one the parity recordings were made with.
# - cargo-llvm-cov and cargo-nextest, with which the Test stage runs the Rust tests and measures their coverage
#   (docs/quality-gate.md). cargo-llvm-cov also needs rustup's llvm-tools component, which the agent image provides.
# - cargo-mutants, with which gate.py's surviving-mutants check mutation-tests the Rust a change edits.
# - cargo-about, with which the Package stage's packaging/build-binary.sh writes the third-party licenses a release
#   ships.
#
# Each is pinned to the sha256 of its linux x86_64 archive as its release publishes it: gitleaks's in
# gitleaks_<version>_checksums.txt, cargo-nextest's and cargo-about's in the .sha256 file beside the archive, and cargo-llvm-cov's as the
# asset's digest on its GitHub release page, which has no checksums file. cargo-mutants publishes neither, so its
# sha256 is that of the archive as downloaded when it was pinned (2026-10-30). The musl builds of cargo-llvm-cov,
# cargo-nextest and cargo-about are static, so they run whatever C library the agent image has; cargo-mutants releases only a glibc
# build for linux x86_64.
set -eu

[ $# -eq 1 ] || { echo "usage: install-tools.sh <dir>" >&2; exit 2; }
dir=$1

archive=$(mktemp)
trap 'rm -f "$archive"' EXIT

# fetch_tool NAME VERSION URL SHA256 [FOLDER]: download the .tar.gz at URL, check it against SHA256, and extract the
# executable NAME into $dir, from the archive's top level or, given FOLDER, from the top-level folder of that name.
fetch_tool() {
    curl -fsSL -o "$archive" "$3"
    echo "$4  $archive" | sha256sum -c --quiet - || {
        echo "install-tools.sh: the $1 $2 archive does not match its pinned sha256" >&2
        exit 1
    }
    if [ $# -eq 5 ]; then
        tar -xzf "$archive" -C "$dir" --strip-components=1 "$5/$1"
    else
        tar -xzf "$archive" -C "$dir" "$1"
    fi
}

mkdir -p "$dir"

version=8.30.1
fetch_tool gitleaks "$version" \
    "https://github.com/gitleaks/gitleaks/releases/download/v$version/gitleaks_${version}_linux_x64.tar.gz" \
    551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
"$dir/gitleaks" version

version=0.9.1
release=https://github.com/taiki-e/cargo-llvm-cov/releases/download/v$version
fetch_tool cargo-llvm-cov "$version" "$release/cargo-llvm-cov-x86_64-unknown-linux-musl.tar.gz" \
    3fca950394a3c49457657c158b1619cec8dfd2647ae5b48746734c0ab969a522
"$dir/cargo-llvm-cov" llvm-cov --version

version=0.9.148
release=https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-$version
fetch_tool cargo-nextest "$version" "$release/cargo-nextest-$version-x86_64-unknown-linux-musl.tar.gz" \
    bf92cb81450872d960ffa13857a83084d557ec0cdc141663d263bb23ef756f97
"$dir/cargo-nextest" nextest --version

version=27.1.0
fetch_tool cargo-mutants "$version" \
    "https://github.com/sourcefrog/cargo-mutants/releases/download/v$version/cargo-mutants-x86_64-unknown-linux-gnu.tar.gz" \
    dfe6dc37d0342c891d2829b5a695aa57c2d0edecef7e7d0399a30cc6e206411e
"$dir/cargo-mutants" mutants --version

version=0.9.3
fetch_tool cargo-about "$version" \
    "https://github.com/EmbarkStudios/cargo-about/releases/download/$version/cargo-about-$version-x86_64-unknown-linux-musl.tar.gz" \
    3ad1e94ea1e63d69d1961f4fb15ccbbe9575d906111f96d2cb4dce6df48f71b4 "cargo-about-$version-x86_64-unknown-linux-musl"
"$dir/cargo-about" --version
