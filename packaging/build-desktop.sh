#!/bin/sh
# Build the desktop app in ui/ for release, with the orchestrator binary that build-binary.sh built for this
# machine inside it as its sidecar, so the app needs no Python and no checkout. Writes to dist/, each with its
# .sha256: on Linux orchestrator-desktop-linux-<arch>.deb and .AppImage, on macOS
# orchestrator-desktop-macos-<arch>.dmg, signed ad hoc only. The app's version is pyproject.toml's. Run
# build-binary.sh first. Jenkins's Package stage builds the Linux app with it, and
# .github/workflows/macos-binary.yml the macOS one.
set -eu

cd "$(dirname "$0")/.."

case "$(uname -s)" in
    Linux) os=linux bundles=deb,appimage ;;
    Darwin) os=macos bundles=app,dmg ;;
    *) echo "build-desktop.sh: no desktop app is built for $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    arm64 | aarch64) arch=arm64 ;;
    *) echo "build-desktop.sh: no desktop app is built for $(uname -m)" >&2; exit 1 ;;
esac
binary="dist/orchestrator-$os-$arch"
[ -x "$binary" ] || { echo "build-desktop.sh: no $binary; run packaging/build-binary.sh first" >&2; exit 1; }

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)
[ -n "$version" ] || { echo "build-desktop.sh: no version in pyproject.toml" >&2; exit 1; }
triple=$(rustc -vV | sed -n 's/^host: //p')
[ -n "$triple" ] || { echo "build-desktop.sh: rustc names no host target" >&2; exit 1; }

# Tauri's externalBin takes binaries/orchestrator to mean binaries/orchestrator-<target triple>, and bundles it
# beside the app's executable as plain orchestrator, where the shell looks for it (BUNDLED_NAME in
# ui/src-tauri/src/lib.rs).
mkdir -p ui/src-tauri/binaries build/desktop
cp "$binary" "ui/src-tauri/binaries/orchestrator-$triple"

# Merged over tauri.conf.json, which bundles nothing, so that cargo test and tauri dev need no sidecar on
# disk. signingIdentity "-" signs the macOS app ad hoc: macOS reports an unsigned app downloaded onto Apple
# silicon as damaged, and an app signed ad hoc only as from an unidentified developer.
config="$PWD/build/desktop/tauri.release.json"
cat > "$config" <<EOF
{
  "version": "$version",
  "bundle": {
    "active": true,
    "externalBin": ["binaries/orchestrator"],
    "macOS": { "signingIdentity": "-" }
  }
}
EOF

# AppImage's tools are themselves AppImages; extracting them needs no FUSE on the build machine.
export APPIMAGE_EXTRACT_AND_RUN=1
(
    cd ui
    npm ci --no-audit --no-fund
    npm run tauri -- build --ci --config "$config" --bundles "$bundles"
)

# publish EXT FILE: copy FILE, the one bundle of this version Tauri made of a kind, to the name the release gives
# it, dist/orchestrator-desktop-<os>-<arch>.EXT, and write its .sha256. FILE is a glob, so none or several of them
# arrive as anything but one existing file.
publish() {
    ext=$1
    shift
    [ $# -eq 1 ] && [ -f "$1" ] || { echo "build-desktop.sh: tauri build made no single $*" >&2; exit 1; }
    name="orchestrator-desktop-$os-$arch.$ext"
    cp "$1" "dist/$name"
    if command -v sha256sum >/dev/null 2>&1; then
        (cd dist && sha256sum "$name" > "$name.sha256")
    else
        (cd dist && shasum -a 256 "$name" > "$name.sha256")
    fi
    echo "built dist/$name and dist/$name.sha256"
}

# A bundle without the sidecar still builds and opens, then says only that it cannot start the orchestrator.
out="ui/src-tauri/target/release/bundle"
if [ "$os" = linux ]; then
    for deb in "$out/deb/Orchestrator_${version}_"*.deb; do
        # A .deb's paths start with ./ when dpkg-deb built it, and may not when Tauri did.
        files=$(dpkg-deb --contents "$deb" | awk '{print $NF}')
        if ! printf '%s\n' "$files" | grep -Eqx '(\./)?usr/bin/orchestrator'; then
            echo "build-desktop.sh: $deb has no usr/bin/orchestrator; its usr/bin holds:" >&2
            printf '%s\n' "$files" | grep -E '^(\./)?usr/bin/' >&2 || true
            exit 1
        fi
    done
    publish deb "$out/deb/Orchestrator_${version}_"*.deb
    publish AppImage "$out/appimage/Orchestrator_${version}_"*.AppImage
else
    [ -x "$out/macos/Orchestrator.app/Contents/MacOS/orchestrator" ] \
        || { echo "build-desktop.sh: Orchestrator.app has no Contents/MacOS/orchestrator" >&2; exit 1; }
    publish dmg "$out/dmg/Orchestrator_${version}_"*.dmg
fi
