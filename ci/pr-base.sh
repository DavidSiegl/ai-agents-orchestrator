#!/bin/sh
# Print the commit a pull request build's change is judged against: the merge base of HEAD and its target branch,
# $CHANGE_TARGET, which Jenkins sets on a pull request build, fetched from origin first. Exit non-zero, git saying why
# on standard error, when the target cannot be fetched or has no merge base with HEAD. Jenkins's Gate stage judges a
# pull request from it.
set -eu
: "${CHANGE_TARGET:?is unset; only a pull request build has a target}"
git fetch --quiet origin "+refs/heads/$CHANGE_TARGET:refs/remotes/origin/$CHANGE_TARGET"
git merge-base HEAD "origin/$CHANGE_TARGET"
