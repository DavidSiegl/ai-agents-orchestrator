#!/bin/sh
# Run the command given as arguments while holding a lock that every build of this repository on the Jenkins node
# takes for its cargo steps, and exit with the command's status. While another build holds the lock, say so on
# standard error and wait for it with no time limit, so that a slow build reads as waiting rather than hung; with
# --no-wait, say so and run the command at once without the lock. Only this script holds the lock, never the command
# or anything it starts, so a process the command leaves running does not keep it. Jenkins's Install, Test, UI and
# Package stages run every cargo step through it.
#
# The lock is there because all builds on agentbox0 share its Jenkins agent container's 3 GB (mem_limit in the
# Homelab's jenkins/agentbox0.yaml): two builds compiling the Tauri and GTK crates at once ran rustc out of memory
# (PR-74's first build). The lock file lies outside every workspace, which cleanWs empties.
set -eu

wait=true
if [ "${1:-}" = --no-wait ]; then
    wait=false
    shift
fi
[ $# -gt 0 ] || { echo "usage: cargo-lock.sh [--no-wait] <command> [<argument>...]" >&2; exit 2; }

lock="${JENKINS_AGENT_WORKDIR:-$HOME}/cargo-steps.lock"
exec 9>>"$lock"
# flock exits 1 when another process holds the lock, and otherwise fails with its own message.
status=0
flock -n 9 || status=$?
if [ "$status" -eq 1 ] && ! "$wait"; then
    echo "cargo-lock.sh: another build on this node holds $lock; running without it" >&2
elif [ "$status" -eq 1 ]; then
    echo "cargo-lock.sh: another build on this node holds $lock; waiting for it" >&2
    flock 9
elif [ "$status" -ne 0 ]; then
    exit "$status"
fi
"$@" 9>&-
