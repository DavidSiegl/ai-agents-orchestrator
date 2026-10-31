#!/bin/sh
# Exit 0 when the release version at HEAD, pyproject.toml's [project] version, is greater than that of $CHANGE_TARGET,
# the branch a pull request build merges into, fetched from origin first; exit non-zero, saying why on standard error,
# when it is not, or when either version cannot be read. Versions are compared as sort -V orders them, number by
# number, so 0.10.0 is greater than 0.9.0. Jenkins's Version stage runs it on a pull request build into main: the
# Release stage releases a merge into main only when its version has no release yet, so a merge that kept main's
# version would go unreleased. tests/test_invariants.py holds engine/Cargo.toml's version to pyproject.toml's.
set -eu
: "${CHANGE_TARGET:?is unset; only a pull request build has a target}"
git fetch --quiet origin "+refs/heads/$CHANGE_TARGET:refs/remotes/origin/$CHANGE_TARGET"

# The version of pyproject.toml's [project] table, read from standard input; nothing when it has none.
project_version() {
    sed -n '/^\[project\]$/,/^\[/s/^version = "\([^"]*\)"$/\1/p' | head -n 1
}

target_text=$(git show "origin/$CHANGE_TARGET:pyproject.toml")
target=$(printf '%s\n' "$target_text" | project_version)
head=$(project_version < pyproject.toml)
if [ -z "$target" ] || [ -z "$head" ]; then
    echo "version-raised.sh: no [project] version in pyproject.toml, here ('$head') or on $CHANGE_TARGET ('$target')" >&2
    exit 1
fi
greater=$(printf '%s\n%s\n' "$target" "$head" | sort -V | tail -n 1)
if [ "$head" = "$target" ] || [ "$greater" != "$head" ]; then
    echo "version-raised.sh: pyproject.toml's version is $head, not greater than $CHANGE_TARGET's $target; raise it," \
        "and engine/Cargo.toml's with it, or the merge goes unreleased (Jenkinsfile, Release)" >&2
    exit 1
fi
