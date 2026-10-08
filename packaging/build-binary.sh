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

# Installs the build Python and PyInstaller first, so a failure there is reported as itself.
uv sync --frozen --group build

# PyInstaller leaves tkinter out, with only a warning, when the build Python cannot import it or start Tcl, and
# the GUI smoke test below is skipped on a Linux without a display, so a binary without its GUI could otherwise
# be released. Tcl(), unlike Tk(), needs no display.
if ! uv run --frozen --group build python -c 'import tkinter; tkinter.Tcl()'; then
    echo "build-binary.sh: the build Python has no tkinter; use uv's own, e.g. UV_PYTHON_PREFERENCE=only-managed" >&2
    exit 1
fi

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
