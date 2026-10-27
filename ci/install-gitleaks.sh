#!/bin/sh
# Put gitleaks, the pinned release below, at <dir>/gitleaks, and exit 0; exit non-zero, saying why on standard
# error, when it cannot be downloaded or its archive does not match the pinned sha256. Jenkins's Install stage runs it
# with a directory the Jenkinsfile puts first on PATH, because the parity scenarios (tests/parity) run the real
# gitleaks and fail where there is none, and the agent image does not provide it.
#
# The version is the one the parity recordings were made with; a new one is pinned with the sha256 its release's
# gitleaks_<version>_checksums.txt gives for the linux_x64 archive.
set -eu

[ $# -eq 1 ] || { echo "usage: install-gitleaks.sh <dir>" >&2; exit 2; }
dir=$1
version=8.30.1
sha256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb

archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
curl -fsSL -o "$archive" \
    "https://github.com/gitleaks/gitleaks/releases/download/v$version/gitleaks_${version}_linux_x64.tar.gz"
echo "$sha256  $archive" | sha256sum -c --quiet - || {
    echo "install-gitleaks.sh: the gitleaks $version archive does not match its pinned sha256" >&2
    exit 1
}
mkdir -p "$dir"
tar -xzf "$archive" -C "$dir" gitleaks
"$dir/gitleaks" version
