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

# On Linux, the distribution's Python, not uv's own: uv's Tk is built without Xft, so it draws every font with
# the X server's bitmap "fixed", one size only - too small on a high-DPI screen, deaf to the zoom, and never
# Roboto. A distribution's Tk draws with Xft and fontconfig, and PyInstaller bundles both. It overrides
# .python-version, which asks for a newer Python than e.g. Debian trixie's 3.13, and builds in a venv of its own,
# so the .venv that the tests and the release run in keeps uv's Python. A caller's own UV_PYTHON wins, and the
# Xft check below then still holds it to a Tk whose fonts scale.
if [ "$os" = linux ]; then
    export UV_PYTHON="${UV_PYTHON:-python3}" UV_PYTHON_PREFERENCE="${UV_PYTHON_PREFERENCE:-only-system}" \
        UV_PROJECT_ENVIRONMENT=build/binary-venv
fi

# Installs the build Python and PyInstaller first, so a failure there is reported as itself.
uv sync --frozen --group build

# PyInstaller leaves tkinter out, with only a warning, when the build Python cannot import it or start Tcl, and
# the GUI smoke test below is skipped on a Linux without a display, so a binary without its GUI could otherwise
# be released. Tcl(), unlike Tk(), needs no display.
if ! uv run --frozen --group build python -c 'import tkinter; tkinter.Tcl()'; then
    if [ "$os" = linux ]; then
        echo "build-binary.sh: the system python3 has no tkinter; install it, e.g. apt install python3-tk" >&2
    else
        echo "build-binary.sh: the build Python has no tkinter; use uv's own, e.g. UV_PYTHON_PREFERENCE=only-managed" >&2
    fi
    exit 1
fi

# And on Linux a Tk that draws with Xft, which the smoke test cannot see: a Tk without it still opens the window.
# The libtk checked is the one the Python has mapped (libtk8.6.so, or Tk 9's libtcl9tk9.0.so), not what ldd makes
# of _tkinter: ldd ignores the Python's own search path, and finds the system's libtk beside uv's.
if [ "$os" = linux ]; then
    libtk=$(uv run --frozen --group build python -c '
import _tkinter, re
print(next(m[1] for m in map(re.compile(r"(/\S*/lib(?:tcl\d+)?tk[\d.]*\.so\S*)$").search, open("/proc/self/maps")) if m))')
    if ! ldd "$libtk" | grep -q libXft; then
        echo "build-binary.sh: $libtk does not link libXft, so its fonts would not scale" >&2
        exit 1
    fi
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
