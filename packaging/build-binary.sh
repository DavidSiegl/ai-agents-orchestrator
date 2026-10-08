#!/bin/sh
# Build dist/orchestrator-<os>-<arch>, a PyInstaller one-file binary of orchestrator.py with Python and tkinter
# inside, write its .sha256 beside it, and smoke-test it. Jenkins's Package stage builds the Linux binary with
# it, and .github/workflows/macos-binary.yml the macOS one, so the two builds cannot drift apart.
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
name="orchestrator-$os-$arch"
bin="dist/$name"

# The build group holds PyInstaller; the runtime needs nothing beyond the standard library.
uv run --frozen --group build pyinstaller --onefile --name "$name" --noconfirm --clean \
    --distpath dist --workpath build/pyinstaller --specpath build/pyinstaller orchestrator.py

if command -v sha256sum >/dev/null 2>&1; then
    (cd dist && sha256sum "$name" > "$name.sha256")
else
    (cd dist && shasum -a 256 "$name" > "$name.sha256")
fi

echo "smoke test: $bin --help"
"$bin" --help >/dev/null
echo "smoke test: $bin workflows"
"$bin" workflows | grep -q '^default '
# The GUI needs a display: macOS has one, Linux one when DISPLAY or WAYLAND_DISPLAY is set.
if [ "$os" = macos ] || [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    echo "smoke test: $bin gui --smoke-test"
    "$bin" gui --smoke-test
else
    echo "smoke test: $bin gui --smoke-test skipped, no display"
fi

echo "built $bin and $bin.sha256"
