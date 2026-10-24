#!/bin/sh
# Exit 0 when a pull request's Jenkins build already judged the tree of HEAD, a commit on main, and everything
# since the last successful build of main, so that this build of main need not test and gate it again; exit
# non-zero, saying why on standard error, when it must. Jenkins's "Judged at PR" stage runs it on builds of main.
#
# Judged means: some commit has HEAD's tree, contains HEAD^1, and carries a successful pr-head status that
# $PR_STATUS_CREATOR posted, with HEAD^1 the commit of main's last successful build,
# $PREVIOUS_SUCCESSFUL_COMMIT. The same tree means that build ran HEAD's own Jenkinsfile, tests and gate; a base
# no later than HEAD^1 means it judged every line since. Only Jenkins can post as $PR_STATUS_CREATOR, its own
# GitHub identity: a status the agents post with their token never counts. The commits tried are the heads of the
# pull requests GitHub associates with HEAD.
#
# Runs in the checkout, with gh logged in through GH_TOKEN. Any failure, such as an unset variable or a GitHub
# error, exits non-zero, so the build then judges HEAD itself.
set -eu

context=continuous-integration/jenkins/pr-head
: "${PR_STATUS_CREATOR:?is unset: Jenkins has no GitHub identity of its own to trust}"
parent=$(git rev-parse HEAD^1)
tree=$(git rev-parse 'HEAD^{tree}')

if [ "$parent" != "${PREVIOUS_SUCCESSFUL_COMMIT:-}" ]; then
    echo "judged-at-pr.sh: the last successful build of main was not of HEAD^1 ($parent)" >&2
    exit 1
fi

heads=$(gh api "repos/{owner}/{repo}/commits/$(git rev-parse HEAD)/pulls" --jq '.[].head.sha')
for head in $heads; do
    [ "$(gh api "repos/{owner}/{repo}/git/commits/$head" --jq .tree.sha)" = "$tree" ] || continue
    case "$(gh api "repos/{owner}/{repo}/compare/$parent...$head" --jq .status)" in
        identical | ahead) ;;
        *) continue ;;
    esac
    # Newest first; the newest of Jenkins's own, since Jenkins posts pending before the result.
    state=$(PR_STATUS_CONTEXT=$context gh api "repos/{owner}/{repo}/commits/$head/statuses?per_page=100" \
        --jq '[.[] | select(.context == env.PR_STATUS_CONTEXT and .creator.login == env.PR_STATUS_CREATOR)][0].state')
    if [ "$state" = success ]; then
        echo "judged-at-pr.sh: $head has HEAD's tree and passed its pull request build"
        exit 0
    fi
done
echo "judged-at-pr.sh: no pull request build by $PR_STATUS_CREATOR passed on HEAD's tree on top of $parent" >&2
exit 1
