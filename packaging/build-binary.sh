#!/bin/sh
# Build dist/octoflow-<os>-<arch>, the Rust engine's orchestrator binary (engine/, a release build), write its
# .sha256 beside it, and smoke-test it. Jenkins's Package stage builds the Linux binary with it, and
# .github/workflows/macos-binary.yml the macOS one, so the two builds cannot drift apart. The binary links the
# C library of the machine it was built on, as the PyInstaller binary of the Python engine did.
set -eu

cd "$(dirname "$0")/.."

case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=macos ;;
    *) echo "build-binary.sh: no binary is built for $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    arm64 | aarch64) arch=arm64 ;;
    *) echo "build-binary.sh: no binary is built for $(uname -m)" >&2; exit 1 ;;
esac
name="octoflow-$os-$arch"
bin="dist/$name"

# The toolchain is rust-toolchain.toml's; --locked holds the build to Cargo.lock. CARGO_TARGET_DIR, where a caller
# sets one, is where cargo puts the build, so the binary's path is asked of cargo rather than assumed.
report=$(mktemp)
trap 'rm -f "$report"' EXIT
cargo build --release --locked -p octoflow-engine --bin orchestrator --message-format=json-render-diagnostics \
    > "$report"
built=$(sed -n 's/.*"executable":"\([^"]*\/orchestrator\)".*/\1/p' "$report" | tail -n 1)
if [ -z "$built" ]; then
    echo "build-binary.sh: cargo did not report the orchestrator binary it built" >&2
    exit 1
fi
mkdir -p dist
cp "$built" "$bin"

if command -v sha256sum >/dev/null 2>&1; then
    (cd dist && sha256sum "$name" > "$name.sha256")
else
    (cd dist && shasum -a 256 "$name" > "$name.sha256")
fi

echo "smoke test: $bin --help"
"$bin" --help >/dev/null
echo "smoke test: $bin workflows"
"$bin" workflows | grep -q '^default '

echo "built $bin and $bin.sha256"
