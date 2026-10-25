#!/bin/sh
# Run the command given as arguments while holding a lock that every build on this Jenkins node shares, and exit
# with the command's status. While another build holds the lock, say so on standard error and wait for it, with no
# time limit, so that a slow build reads as waiting rather than hung. Jenkins's UI and Package stages run every
# cargo step through it.
#
# The lock is there because all builds on agentbox0 share its Jenkins agent container's 3 GB (mem_limit in the
# Homelab's jenkins/agentbox0.yaml): two builds compiling the Tauri and GTK crates at once ran rustc out of memory
# (PR-74's first build). The lock file lies outside every workspace, which cleanWs empties.
set -eu

[ $# -gt 0 ] || { echo "usage: cargo-lock.sh <command> [<argument>...]" >&2; exit 2; }

lock="${JENKINS_AGENT_WORKDIR:-$HOME}/cargo-steps.lock"
if ! flock -n "$lock" true; then
    echo "cargo-lock.sh: another build on this node holds $lock; waiting for it" >&2
fi
exec flock "$lock" "$@"
