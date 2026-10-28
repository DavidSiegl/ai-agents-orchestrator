#!/bin/sh
# Build the desktop app in ui/ for release, with the orchestrator binary that build-binary.sh built for this
# machine inside it as its sidecar, so the app needs no Python and no checkout. Writes to dist/, each with its
# .sha256: on Linux octoflow-desktop-linux-<arch>.deb and .AppImage, on macOS
# octoflow-desktop-macos-<arch>.dmg, signed ad hoc only. The app's version is pyproject.toml's. Run
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
binary="dist/octoflow-$os-$arch"
[ -x "$binary" ] || { echo "build-desktop.sh: no $binary; run packaging/build-binary.sh first" >&2; exit 1; }

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)
[ -n "$version" ] || { echo "build-desktop.sh: no version in pyproject.toml" >&2; exit 1; }
triple=$(rustc -vV | sed -n 's/^host: //p')
[ -n "$triple" ] || { echo "build-desktop.sh: rustc names no host target" >&2; exit 1; }

# Tauri's externalBin takes binaries/octoflow to mean binaries/octoflow-<target triple>, and bundles it
# beside the app's executable as plain orchestrator, where the shell looks for it (BUNDLED_NAME in
# ui/src-tauri/src/lib.rs).
mkdir -p ui/src-tauri/binaries build/desktop
cp "$binary" "ui/src-tauri/binaries/octoflow-$triple"

# Merged over tauri.conf.json, which bundles nothing, so that cargo test and tauri dev need no sidecar on
# disk. signingIdentity "-" signs the macOS app ad hoc: macOS reports an unsigned app downloaded onto Apple
# silicon as damaged, and an app signed ad hoc only as from an unidentified developer.
config="$PWD/build/desktop/tauri.release.json"
cat > "$config" <<EOF
{
  "version": "$version",
  "bundle": {
    "active": true,
    "externalBin": ["binaries/octoflow"],
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
# it, dist/octoflow-desktop-<os>-<arch>.EXT, and write its .sha256. FILE is a glob, so none or several of them
# arrive as anything but one existing file.
publish() {
    ext=$1
    shift
    [ $# -eq 1 ] && [ -f "$1" ] || { echo "build-desktop.sh: tauri build made no single $*" >&2; exit 1; }
    name="octoflow-desktop-$os-$arch.$ext"
    cp "$1" "dist/$name"
    if command -v sha256sum >/dev/null 2>&1; then
        (cd dist && sha256sum "$name" > "$name.sha256")
    else
        (cd dist && shasum -a 256 "$name" > "$name.sha256")
    fi
    echo "built dist/$name and dist/$name.sha256"
}

# smoke_cli DIR PROGRAM: run PROGRAM, an absolute path, as the CLI from DIR, as a user would from a project: --help
# exits 0, and workflows exits 0 listing the built-in default, as build-binary.sh asks of the binary. Calls
# smoke_fail, which the caller defines, naming the check, when one fails.
smoke_cli() {
    (cd "$1" && "$2" --help >/dev/null) || smoke_fail "$2 --help did not exit 0"
    (cd "$1" && "$2" workflows >"$1/workflows.out") || smoke_fail "$2 workflows did not exit 0"
    grep -q '^default ' "$1/workflows.out" || smoke_fail "$2 workflows did not list the default workflow"
    rm -f "$1/workflows.out"
}

# smoke_appimage APPIMAGE: run the AppImage, an absolute path, as the CLI from a new directory, as smoke_cli does,
# and check that the orchestrator that serve execs starts in that directory without
# the bundle's LD_LIBRARY_PATH entries or PYTHONHOME (UserShell in ui/src-tauri/src/lib.rs). serve is the one
# command that needs no herdr and stays up long enough to read its /proc entry. Exits 1, naming the check, when
# one fails.
smoke_appimage() {
    smoke=$(mktemp -d)
    smoke=$(cd "$smoke" && pwd -P)
    # APPIMAGE_EXTRACT_AND_RUN extracts the AppImage under $TMPDIR and never removes it, 336 MB a build: under
    # $smoke it goes with the rest. Nothing after this function, the script's last step, makes temporary files.
    export TMPDIR="$smoke"
    serve_pid='' orchestrator=''
    smoke_fail() {
        echo "build-desktop.sh: the AppImage as the CLI: $*" >&2
        # The orchestrator first: the AppImage's runtime does not pass a signal on to it.
        for pid in $orchestrator $serve_pid; do kill "$pid" 2>/dev/null || true; done
        rm -rf "$smoke"
        exit 1
    }
    smoke_cli "$smoke" "$1"

    (cd "$smoke" && exec "$1" serve >serve.out) &
    serve_pid=$!
    tries=0
    until [ -s "$smoke/serve.out" ]; do
        tries=$((tries + 1))
        [ "$tries" -le 30 ] || smoke_fail "serve printed no address in 30 s"
        kill -0 "$serve_pid" 2>/dev/null || smoke_fail "serve exited before printing its address"
        sleep 1
    done

    # The AppImage's runtime extracts the bundle and starts AppRun, which execs the app, which execs the
    # orchestrator: the nearest descendant running $APPDIR/usr/bin/octoflow is the process the app became (the
    # one below it is PyInstaller's).
    pids=$serve_pid
    while [ -z "$orchestrator" ] && [ -n "$pids" ]; do
        next=
        for pid in $pids; do
            case "$(readlink "/proc/$pid/exe" 2>/dev/null)" in
                */usr/bin/octoflow) orchestrator=$pid; break ;;
            esac
            # ps exits 1 for a process with no children, which is only a leaf of the walk.
            next="$next $(ps -o pid= --ppid "$pid" || true)"
        done
        pids=$next
    done
    [ -n "$orchestrator" ] || smoke_fail "no process of serve's runs the bundled orchestrator"
    appdir=$(readlink "/proc/$orchestrator/exe") || smoke_fail "the orchestrator exited before its check"
    appdir=${appdir%/usr/bin/octoflow}
    env=$(tr '\0' '\n' <"/proc/$orchestrator/environ") || smoke_fail "cannot read the orchestrator's environment"

    cwd=$(readlink "/proc/$orchestrator/cwd") || smoke_fail "cannot read the orchestrator's directory"
    [ "$cwd" = "$smoke" ] || smoke_fail "serve runs in $cwd, not the caller's $smoke"
    if printf '%s\n' "$env" | sed -n 's/^LD_LIBRARY_PATH=//p' | tr ':' '\n' | grep -q "^$appdir\(/\|\$\)"; then
        smoke_fail "serve's LD_LIBRARY_PATH has the bundle's $appdir in it"
    fi
    if printf '%s\n' "$env" | grep -q '^PYTHONHOME='; then
        smoke_fail "serve has PYTHONHOME set: $(printf '%s\n' "$env" | grep '^PYTHONHOME=')"
    fi

    kill "$orchestrator"
    wait "$serve_pid" || smoke_fail "serve did not exit 0 on SIGTERM"
    rm -rf "$smoke"
    echo "smoke-tested dist/$(basename "$1") as the CLI"
}

# smoke_dmg DMG: get the CLI from the .dmg, an absolute path, as the README tells a Mac user to, and run it from a
# new directory: as smoke_cli does, through a symlink to Contents/MacOS/octoflow in the mounted Octoflow.app; and
# --help, exiting 0, through the app's own executable, CFBundleExecutable in its Info.plist, which runs the CLI when
# given arguments (cli in ui/src-tauri/src/lib.rs). Detaches the .dmg; exits 1, naming the check, when one fails.
smoke_dmg() {
    smoke=$(mktemp -d)
    smoke=$(cd "$smoke" && pwd -P)
    mounted=''
    smoke_fail() {
        echo "build-desktop.sh: the .dmg's app as the CLI: $*" >&2
        [ -z "$mounted" ] || hdiutil detach -quiet -force "$smoke/mnt" 2>/dev/null || true
        rm -rf "$smoke"
        exit 1
    }
    mkdir "$smoke/mnt" "$smoke/bin" "$smoke/project" || smoke_fail "could not make directories in $smoke"
    hdiutil attach -quiet -nobrowse -readonly -mountpoint "$smoke/mnt" "$1" || smoke_fail "could not mount $1"
    mounted=yes
    app="$smoke/mnt/Octoflow.app"
    ln -s "$app/Contents/MacOS/octoflow" "$smoke/bin/octoflow" || smoke_fail "could not link the orchestrator"
    smoke_cli "$smoke/project" "$smoke/bin/octoflow"
    exe=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$app/Contents/Info.plist") \
        || smoke_fail "Info.plist names no CFBundleExecutable"
    # With PATH an empty directory and no ORCHESTRATOR_PY, the app can run only the orchestrator it bundles: its
    # fallback for a build with none, python3 with the checkout's orchestrator.py (Orchestrator::locate_from), would
    # pass here, since this script runs in that checkout. PATH is emptied, not unset: unset, execvp would search the
    # default /usr/bin:/bin, where macOS has a python3.
    (cd "$smoke/project" && env -u ORCHESTRATOR_PY PATH=/var/empty "$app/Contents/MacOS/$exe" --help >/dev/null) \
        || smoke_fail "$exe --help did not exit 0 with only the bundled orchestrator to run"
    # The volume can be busy for a moment after the processes started from it exit, as Spotlight looks at it.
    tries=0
    until hdiutil detach -quiet "$smoke/mnt"; do
        tries=$((tries + 1))
        if [ "$tries" -ge 5 ]; then
            hdiutil detach -quiet -force "$smoke/mnt" || smoke_fail "could not detach $smoke/mnt"
            break
        fi
        sleep 2
    done
    mounted=''
    rm -rf "$smoke"
    echo "smoke-tested dist/$(basename "$1") as the CLI, by symlink and as $exe"
}

# A bundle without the sidecar still builds and opens, then says only that it cannot start the orchestrator.
# The Cargo workspace's target directory (Cargo.toml at the top), where tauri build writes the shell's bundles.
out="target/release/bundle"
if [ "$os" = linux ]; then
    for deb in "$out/deb/Octoflow_${version}_"*.deb; do
        # A .deb's paths start with ./ when dpkg-deb built it, and may not when Tauri did.
        files=$(dpkg-deb --contents "$deb" | awk '{print $NF}')
        if ! printf '%s\n' "$files" | grep -Eqx '(\./)?usr/bin/octoflow'; then
            echo "build-desktop.sh: $deb has no usr/bin/octoflow; its usr/bin holds:" >&2
            printf '%s\n' "$files" | grep -E '^(\./)?usr/bin/' >&2 || true
            exit 1
        fi
    done
    publish deb "$out/deb/Octoflow_${version}_"*.deb
    publish AppImage "$out/appimage/Octoflow_${version}_"*.AppImage
    smoke_appimage "$PWD/dist/octoflow-desktop-$os-$arch.AppImage"
else
    [ -x "$out/macos/Octoflow.app/Contents/MacOS/octoflow" ] \
        || { echo "build-desktop.sh: Octoflow.app has no Contents/MacOS/octoflow" >&2; exit 1; }
    publish dmg "$out/dmg/Octoflow_${version}_"*.dmg
    smoke_dmg "$PWD/dist/octoflow-desktop-$os-$arch.dmg"
fi
