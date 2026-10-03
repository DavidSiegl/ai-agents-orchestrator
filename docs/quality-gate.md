# Quality gate

How Jenkins builds this repository with a SonarQube quality gate, and how the orchestrator is meant to use it.
The [Jenkins setup](#jenkins-setup) is in place. The [orchestrator loop](#orchestrator-loop-design-not-implemented)
is a design that `orchestrator.py` does not implement yet.

## Jenkins setup

One `Jenkinsfile` serves two jobs. An ordinary build checks out the job's own branch, tests with branch coverage
of `orchestrator`, analyses into the SonarQube project of `sonar-project.properties` and fails on a red gate. A
quality build gets parameters from the orchestrator and analyses one snapshot into one run's own project.

### What Jenkins needs

- **Plugins**: Pipeline, Git, SonarQube Scanner, JUnit and Workspace Cleanup.
- **The SonarQube server**, under Manage Jenkins → System → SonarQube servers, named `Sonarqube`, with a token
  that may analyse into the project. `withSonarQubeEnv('Sonarqube')` finds it by that name.
- **The scanner**, under Manage Jenkins → Tools → SonarQube Scanner installations, named `sonarqube-scanner`.
- **A SonarQube webhook** to `<jenkins-url>/sonarqube-webhook/`, under Administration → Configuration → Webhooks
  in SonarQube. `waitForQualityGate` waits for it, and without it the Quality Gate stage times out after 5
  minutes.
- **`uv` and `git` on the agent**: the Install stage runs `uv sync --frozen`, and `uv` brings the Python of
  `.python-version`.

### The two jobs

Both load the `Jenkinsfile` with "Pipeline script from SCM". The Checkout stage needs that: it runs
`checkout scm`, or fetches `GIT_REF` from `scm`'s remote.

- **The ordinary job** builds the repository as it does today, whatever its kind (Pipeline or multibranch).
- **The quality job**, e.g. `ai-agents-orchestrator-quality`: a plain Pipeline job that loads the `Jenkinsfile`
  from `main`. It takes any ref as a parameter, keeps quality builds out of the ordinary job's history, and a
  change to the `Jenkinsfile` cannot alter how that same change is judged.

Jenkins learns a Jenkinsfile's `parameters` only by running it, so a new job needs one build, by hand and with
empty parameters, before it accepts `buildWithParameters`.

### Parameters

| Parameter | Set by the orchestrator to | When empty (ordinary builds) |
|---|---|---|
| `GIT_REF` | a branch on `origin`, e.g. `orchestrator-ci/cd3492-1-q2` | `checkout scm` |
| `SONAR_PROJECT_KEY` | the run's project, `py-ai-agents-orchestrator-<key>` | the key in `sonar-project.properties`; a failing test or a red gate fails the build |
| `SONAR_PROJECT_VERSION` | `base` or `change` | no `sonar.projectVersion` |

The parameters reach the scanner as shell variables, never as Groovy-built shell code, so no value can inject a
command. `GIT_REF` is interpolated only into the checkout's branch spec.

### What each kind of build reports

- **Ordinary builds** stop at a failing test, and fail on a gate status other than `OK`, as before the
  parameters.
- **Quality builds** (`SONAR_PROJECT_KEY` set) run the analysis even when tests fail, end UNSTABLE when tests
  fail or the gate is not `OK`, and archive `.scannerwork/report-task.txt`, whose `ceTaskId` leads to the
  analysis. FAILURE then means the change never got as far as the gate.

## Orchestrator loop (design, not implemented)

Condensed from section 3 of `ROADMAP.md` in PR #9; none of it is in `orchestrator.py` yet. `file:line` references
point at commit `1698d1e`. Today the change leaves the agents' machine only in `_publish`
(`orchestrator.py:801-839`), after the last review, when no role is left to act on what Jenkins finds.

### What is decided

These were decided with the human, and the rest builds on them.

- **Placement (pattern A).** Builder → quality gate → (on failure, back to the Builder) → Reviewer, after
  *every* Builder turn: in round 1 and in each round after `CHANGES_REQUESTED`.
- **Trigger.** Jenkins runs the analysis, not a local `sonar-scanner`.
- **Pushing.** Each quality round pushes a snapshot of the working tree to a throwaway ref on `origin` and deletes
  the ref afterwards. The run's branch, its single commit and `_publish` stay as they are.
- **One SonarQube project per run**, `py-ai-agents-orchestrator-<key>` with the run's six-hex key, passed to
  Jenkins as a parameter, because Community Edition has no branch analysis. The run analyses its base commit
  first, so that new code is only the Builder's change, and removes the project at the end.
- **What goes to the Builder.** Only the issues on lines the change touched, from `git diff -U0` against
  `state.base`, plus the failing tests and the coverage on new code from the Jenkins Test stage.
- **Pass condition.** The SonarQube quality gate status is `OK`.
- **When the rounds run out.** After `--max-quality-rounds` rounds (default 3) the change goes to the Reviewer
  anyway, whose prompt names the last quality file as unresolved findings. A quality round is one analysis, so
  three rounds are at most three analyses and two fix turns, and each build round has its own budget.

### Snapshotting the uncommitted tree

The Builder leaves its work uncommitted (`orchestrator.py:104`), and Jenkins can only fetch commits. Options:
(a) `git stash create`, which leaves out untracked files and makes a merge commit that confuses blame; (b) commit
on the run's branch and `reset --soft` back, where a crash leaves an extra commit for `_publish`, and under
`--no-pr` on the human's branch; (c) a temporary index and `git commit-tree`.

**Recommendation:** (c). It moves no ref and only copies the real index, keeping git's stat cache. `git add -A`
honours `.gitignore`, and `_claim` ignores `.orchestrator/` (`orchestrator.py:681-685`), so the snapshot holds
what `_publish`'s `git add --all` commits (`orchestrator.py:817`). Its parent is `state.base`: the change the
Reviewer diffs. A new `Host.snapshot(cwd, parent, message) -> sha` runs it as one `sh -c` script, as
`Host.write` does (`orchestrator.py:339-343`), because `Host.git` cannot pass `GIT_INDEX_FILE`:

```sh
cd -- "$1" || exit 1
t=$(mktemp) || exit 1
trap 'rm -f -- "$t"' EXIT
cp -- "$(git rev-parse --git-path index)" "$t" &&
    GIT_INDEX_FILE=$t git add -A &&
    tree=$(GIT_INDEX_FILE=$t git write-tree) &&
    git commit-tree "$tree" -p "$2" -m "$3"
```

`Host.git` pushes it with `NETWORK_TIMEOUT`, as `_publish` pushes (`orchestrator.py:829`):
`git push --quiet --force origin <sha>:refs/heads/orchestrator-ci/<key>-<n>-q<q>`, and the base to
`orchestrator-ci/<key>-base`. `--force` replaces a ref a failed attempt left; `<n>` keeps names unique.

### The job's interface

The parameters are in the [table above](#parameters); the orchestrator sends only `[A-Za-z0-9._/-]`.
**Which job:** (a) add the parameters to today's job, or (b) a separate quality job reading the same
`Jenkinsfile` from `main`. **Recommendation:** (b). A multibranch job only builds a ref that a branch scan has
found; a plain Pipeline job takes any. Loading from `main` keeps a Builder's edit to the `Jenkinsfile` from
judging its own change. The job's name is what `--quality-gate` takes.

**The gate result:** (a) keep `abortPipeline: true` and tell the cases apart by the archived `report-task.txt`,
or (b) `abortPipeline: false` in quality builds, UNSTABLE on a red gate, and archive `report-task.txt`.
**Recommendation:** (b), as the `Jenkinsfile` now does. The file holds the `ceTaskId` whatever the result, and
UNSTABLE against FAILURE tells a human whether the change was analysed. Ordinary builds still call `error`.

**Failing tests:** (a) let them stop the build before the analysis, or (b) wrap pytest in `catchError` in
quality builds. **Recommendation:** (b), as the `Jenkinsfile` now does: one round carries all findings. The
gate does not count tests, so a change with failing tests and an `OK` gate reaches the Reviewer, who runs the
tests anyway (`orchestrator.py:122`).

### Getting the result

(a) Poll the Jenkins build, read `ceTaskId` from the archived `report-task.txt`, and follow that SonarQube
task; (b) parse the scanner's console line, which changes between versions; (c) ask `/api/ce/activity` for the
latest task, which cannot tell this round's from an earlier one and misses a build that failed early.
**Recommendation:** (a). Each link is an id handed from one response to the next request, and each is saved, so
a resume picks the chain up where it broke. Jenkins takes HTTP Basic `JENKINS_USER:JENKINS_TOKEN` (no crumb with
an API token); SonarQube takes `SONAR_TOKEN` as the Basic user with an empty password. The calls:

- **Preflight**: (1) `<job>/api/json?tree=property[parameterDefinitions[name]]`; (2) `/api/authentication/validate`.
- **Once per run**: (3) `/api/qualitygates/get_by_project?project=py-ai-agents-orchestrator`; (4) POST
  `/api/projects/create`; (5) POST `/api/qualitygates/select` with that gate; (6) POST
  `/api/new_code_periods/set` with `type=PREVIOUS_VERSION`.
- **Per analysis**, the base first: (7) POST `<job>/buildWithParameters`, queue item in `Location`; (8) poll
  `{Location}api/json` for `executable.number`; (9) poll `<job>/<number>/api/json?tree=building,result`; (10)
  `<job>/<number>/artifact/.scannerwork/report-task.txt`; (11) poll `/api/ce/task?id=<ceTaskId>` for
  `analysisId`; (12) `/api/qualitygates/project_status?analysisId=…`; (13) `/api/issues/search?resolved=false`,
  page by page; (14) `/api/measures/component` for `new_coverage`, `new_lines_to_cover`, `new_uncovered_lines`;
  (15) `<job>/<number>/testReport/api/json`; (16) the last 60 lines of `consoleText`, only for a build that
  ended before the analysis with no failing test. The base stops at call 11.
- **At `done`**: (17) POST `/api/projects/delete`.

The issues kept are those on lines that `git diff -U0 <state.base> <snapshot>` adds, and lineless ones on touched
files; the snapshot includes untracked files and is what Jenkins analysed. **Once a build ends**, live or
resumed: with `report-task.txt`, whatever the result (ABORTED included, as when the Quality Gate stage times
out), follow the task. Without it and ABORTED or cancelled, nobody judged the change: the run fails with `ci` cut
back to `ref` and `sha`, and the Builder gets no fix turn. Without it otherwise: `GATE: BUILD_FAILED`, or for
the base a failed run.

### Who makes the HTTP calls, and where the credentials live

(a) The orchestrator process with `urllib.request`, staying stdlib-only (`pyproject.toml:9`); (b) `curl` through
`Host` on the agents' machine. **Recommendation:** (a). `Host.run` and `Host.check` put the command line into
their errors (`orchestrator.py:318`, `:326`), and `_release` records errors in `state.json`
(`orchestrator.py:691-698`), so a token on curl's command line would land there and in `ps`. A `CI` class over
an injected `urlopen` is tested like `Herdr` and `Host` with their injected `run` (`orchestrator.py:158`, `:306`).

The credentials are environment variables of the orchestrator process: `JENKINS_URL`, `JENKINS_USER`,
`JENKINS_TOKEN`, `SONAR_HOST_URL`, `SONAR_TOKEN`; the preflight names missing ones. `RunState` saves only the
job, project key, queue URL, build number and CE task id. Tokens travel only in the `Authorization` header,
`CI`'s errors name method, URL and status, and text copied into a quality file has both tokens replaced with
`****`. Under `--machine`, HTTP runs on the orchestrator's machine and git on the agents', meeting at `origin`.

### The Community Edition baseline

(a) New code definition "previous version": the base is analysed as version `base` and every quality round as
`change`, so the base analysis stays the baseline and each round measures the whole change; (b) "specific
analysis" set to the base analysis, unconfirmed for Community Edition; (c) no base analysis, where the first
analysis has no new code and every condition passes. **Recommendation:** (a), with (b) as the fallback if a live
check shows the baseline drifts. SonarQube ignores coverage and duplication conditions below 20 new lines; the
quality file reports coverage on new code regardless.

**Creating the project:** (a) let the base analysis create it, needing Create Projects on Jenkins's token, or
(b) the orchestrator creates and configures it (calls 3–6). **Recommendation:** (b). A permission problem fails
on a call that names it, and the gate is copied from `py-ai-agents-orchestrator`, not the instance default.

**Deleting the project:** (a) at the end of every run, or (b) only at `done`. **Recommendation:** (b). A failed
run keeps its workspace and branch (`orchestrator.py:610`), the project explains its last quality file, and a
resume needs it. One never resumed leaves the project behind for the `close` item in the roadmap.

### Handoff files and prompts

Build round n writes `build-<n>.md` (Builder), `quality-<n>-<q>.md` (orchestrator), `build-<n>-q<q>.md` (the
Builder's answer to it) and `review-<n>.md`. Options: (a) these names, or (b) one sequence `build-1.md`,
`build-2.md` for every Builder report. **Recommendation:** (a). `_turn` compares `state.prompted` with the
basename it waits for (`orchestrator.py:882`), so names must be unique, and (b) changes what `build-<n>.md`
means to `REBUILD_NOTE` (`orchestrator.py:142-144`) and `_publish` (`orchestrator.py:830`).

The quality file is written in one write. Its first line is `GATE: OK`, `GATE: ERROR` or `GATE: BUILD_FAILED`;
anything else is rejected like a review without a verdict (`orchestrator.py:784`). On resume an existing file
decides the round, as a role's file does (`orchestrator.py:871-873`). It lists the failed conditions, the issues
on changed lines by file and line, numbered (at most 50, then a count), the failing tests, the coverage on new
code, or the console tail, and names an edit to `sonar-project.properties` or the `Jenkinsfile`.

New prompts beside `FIX_PROMPT` (`orchestrator.py:111-115`): `QUALITY_FIX_PROMPT` names the quality file and the
new report, findings answered by number. `_review_prompts` (`orchestrator.py:848-858`) appends
`QUALITY_PASSED_NOTE` or `QUALITY_UNRESOLVED_NOTE`, and its `report_path` becomes the round's last report. A
fresh Builder gets `BUILD_PROMPT`, `REBUILD_NOTE` and `QUALITY_FIX_PROMPT`, as `_build_prompts`
(`orchestrator.py:791-799`) builds it for `FIX_PROMPT`. `pr_body` (`orchestrator.py:560-578`) adds the quality file.

### Run state and resume

(a) A phase `quality` between `build` and `review`, with a `quality_round` counter; (b) the gate as a step at the
end of `build`. **Recommendation:** (a). `list` shows `quality` (`orchestrator.py:1203`), and resume still
decides by phase first. `build` with q = 0 goes to `quality` q = 1; `quality` q goes to `review` on `OK` or at
q ≥ `max_quality_rounds` (as `orchestrator.py:786` stops review rounds), else to the fix turn, `build` with the
same q, then `quality` q + 1; `CHANGES_REQUESTED` goes to `build`, round n + 1, q = 0. New `RunState` fields
(`orchestrator.py:461-492`): `quality_job` (None means no gate, as for an old `state.json`), `max_quality_rounds`,
`quality_round`, `quality_project` (set once calls 4–6 succeed), `quality_baseline` (the base's analysis id) and
`ci`, `{ref, sha, queue_url, build, ce_task}`, saved key by key.

A resume in `quality` takes the furthest step the state allows: an existing quality file; the project, then the
base; `ci.ce_task`; `ci.build`; `ci.queue_url` (on a 404, the build with its `GIT_REF` among the job's last 20);
`ci.ref` alone; else snapshot, push and trigger. For `ci.ref` alone: (a) trigger again, or (b) first look for a
build with that `GIT_REF`. **Recommendation:** (b). The ref names one round of one run, so no wrong build is
adopted, and two requests are cheaper than a duplicate build.

Polling loops call `_heartbeat` (`orchestrator.py:1049-1060`), as `_turn` does (`orchestrator.py:923`), every
`CI_POLL_SECONDS = 10`, and requests time out after `HTTP_TIMEOUT = 30`, below `STALE_SECONDS`
(`orchestrator.py:44`). **Timeout per round:** (a) reuse `--timeout`, or (b) `QUALITY_TIMEOUT = 1200`.
**Recommendation:** (b). The wait depends on Jenkins and SonarQube, not on the agents. On expiry the run fails
naming the step and keeps `ci`, and a resume gets a fresh timeout.

### CLI

`run` takes `--quality-gate JOB` and `--max-quality-rounds N`; `resume` takes those and `--no-quality-gate`.
(a) `--quality-gate JOB` names the job and turns the gate on; (b) a switch with the job in `JENKINS_JOB`; (c) the
gate is on whenever `JENKINS_URL` is set. **Recommendation:** (a). The job belongs to the project and is saved in
`state.json`, the credentials belong to the machine; (c) would gate runs unasked.

- Both flags join the `settings` parser (`orchestrator.py:1081-1091`), and `main` (`orchestrator.py:1263-1275`)
  and `Workflow.__init__` (`orchestrator.py:624-629`) carry them. `--max-quality-rounds` below 1 is refused
  like `--max-rounds` (`orchestrator.py:1112-1113`), and so is it on `run` without `--quality-gate`.
- `--no-quality-gate`, `resume` only like `--force` (`orchestrator.py:1105`), turns the gate off for good; a run
  in `quality` goes on to `review`, naming any quality file as unresolved.
- `resumable_state` applies them like `--max-rounds` (`orchestrator.py:1229-1230`), and refuses a limit below
  `quality_round` in `quality`, or up to it in `build` with q ≥ 1 (`orchestrator.py:1236-1238`).
  `resume --quality-gate` on a run without the gate turns it on from the next Builder turn, after the preflight.

### Edge cases

When Jenkins or SonarQube fails, rather than the change: (a) fail at once and keep `ci`; (b) skip the gate for
that round; (c) retry until the round's timeout, then fail as in (a). **Recommendation:** (c). A failed poll is
retried, as a failed heartbeat is (`orchestrator.py:1056-1060`). (b) would pass a change the human asked to
gate; `resume --no-quality-gate` is that decision, made by the human.

- **Unreachable at the start, or a job without the parameters**: the preflight fails before the interview and
  says which. A job deleted mid-run (404 on call 7) fails the run at once.
- **Build fails before the analysis**: `GATE: BUILD_FAILED` and a fix turn; for the base, a failed run naming
  the base commit. A SonarQube task that ends `FAILED` or `CANCELED` fails the run, with `ci` cut back.
- **Cleanup**: a finished round deletes its ref; a failed run keeps the project and the ref in flight; `done`
  deletes the rest, logging a failure as `_close_workspace` does (`orchestrator.py:841-846`).
- **Concurrent runs** share only executors and SonarQube's one-at-a-time queue; `QUALITY_TIMEOUT` allows for it.
- **Two runs in one checkout**: a snapshot takes every change in the tree. Pull-request runs call
  `_require_clean` (`orchestrator.py:716-721`), but `--no-pr` runs skip `_check_repo` (`orchestrator.py:643-644`)
  and with the gate must check too, along with `origin`. Two interviews that end before either Builder writes
  still pass; `--worktree` closes that.
- **Not a git repository**: the gate is refused. **No change**: the gate passes, and `_publish` refuses the empty
  change (`orchestrator.py:820-821`).
- **The Builder edits the `Jenkinsfile` or `sonar-project.properties`**: the job reads the first from `main`, the
  scanner the second from the snapshot, so the quality file names either edit for the Reviewer.

### Test plan

On the fakes in `tests/test_orchestrator.py`: `FakeHost` (`:29-109`) gains `snapshot` and records `push` and
`ls-remote`, `FakeHerdr` (`:112`) scripts each turn, `FakeClock` (`:209-222`) drives polls and timeouts, and
`make_workflow` (`:225-234`) and `saved_run` (`:1068-1075`) take the new settings. A new `FakeCI` replaces
`urlopen`, serving scripted builds and tasks, recording requests, and raising `URLError` on demand.

1. Gate passes first time: chain order, `base` then `change`, `GATE: OK` to the Reviewer, refs and project deleted.
2. Fails, then passes: numbered issues, `QUALITY_FIX_PROMPT` with `quality-1-1.md`, `build-1-q1.md` reviewed.
3. Rounds run out at `max_quality_rounds=2`: one fix turn, `QUALITY_UNRESOLVED_NOTE` with `quality-1-2.md`.
4. A later build round builds `orchestrator-ci/a1b2c3-2-q1`; the base is analysed once per run.
5. Only issues on changed lines, an untracked file and lineless ones included, are numbered; the rest counted.
6. Resume with `ci.queue_url` and `ci.build`: no trigger and no push, only polls of that build.
7. Resume with only `ci.ce_task`: no Jenkins request but the test report.
8. Resume with only `ci.ref` and a matching build: it is adopted, nothing triggered.
9. Resume with `quality-1-1.md` present: no request for that round.
10. Jenkins unreachable at the start: fails before the Spec Collector's prompt, naming `JENKINS_URL`.
11. Unreachable mid-round: recovers, or fails after `QUALITY_TIMEOUT` keeping `ci`; no token in any file.
12. Build fails before the analysis: `BUILD_FAILED` and a fix turn; on the base, a failed run.
13. Heartbeat during a 20-minute build, as `TestHeartbeat` (`:1385`) checks during turns.
14. No gate: the existing workflow tests pass unchanged, and `FakeCI` sees no request.
15. `Host.snapshot` on real git, like `TestHostGit` (`:885`): four kinds of file; HEAD, index and status kept.
16. `changed_lines(diff)`: added, deleted and renamed files, hunks with and without counts, `+start,0`.
17. The quality file: numbering, the cap of 50, the console tail, the `Jenkinsfile` line, `****` masking.
18. `CI` on a `MagicMock` `urlopen`, as in `TestHerdr` (`:749`): Basic auth, no token in URLs or errors, 404s.
19. CLI, like `TestCLI` (`:989`) and `TestResumableState` (`:1571`): flag checks, overrides, old `state.json`.
20. `--no-pr` with the gate: refs pushed without a run branch; refused without `origin` or with a dirty tree.
21. Not a git repository (`FakeHost(head=None)`, as at `:523`): refused before the interview.
22. Cleanup: a failed run keeps project and ref; `done` deletes them; a failed delete is logged only.
23. Each row of "Once a build ends", live and as a resume with `ci.build`, with the same outcome.
24. A saved `quality_round` above `max_quality_rounds` goes to `review` after its analysis.

### Open questions

- **The version baseline**: does "previous version" hold across three `change` analyses on the live server?
- **Blame**: new lines need history down to the base, which a shallow clone lacks; check the clone options.
- **Permissions**: can `SONAR_TOKEN` create, configure and delete projects, and Jenkins's token analyse into them?
- **API versions**: `components` or `componentKeys` in `/api/issues/search`, and impacts beside `severity`.
- **Branch discovery**: a multibranch job or push webhook must not build `orchestrator-ci/*` refs.
- **Reachability under `--machine`**: if only the agents' machine reaches Jenkins, curl with config on stdin.
- **Failing tests with a passing gate**: should they fail a round too? That reopens a decision made with the human.
- Should `QUALITY_TIMEOUT` and `CI_POLL_SECONDS` be flags?
