# Quality gate

How Jenkins builds this repository with a SonarQube quality gate, and how `orchestrator.py run --quality-gate`
uses it: the [Jenkins setup](#jenkins-setup), the [credentials and permissions](#credentials-and-permissions)
the orchestrator needs, and the [orchestrator loop](#orchestrator-loop) itself.

## Jenkins setup

One `Jenkinsfile` serves two jobs. An ordinary build checks out its branch or pull request and tests it with
branch coverage of `orchestrator`; on `main` it also analyses into the SonarQube project of
`sonar-project.properties` and fails on a red gate. A quality build gets parameters from the orchestrator and
analyses one snapshot into one run's own project.

### What Jenkins needs

- **Plugins**: Pipeline, Git, SonarQube Scanner, JUnit, Workspace Cleanup and Credentials Binding, and GitHub
  Branch Source for the multibranch job.
- **The SonarQube server**, under Manage Jenkins → System → SonarQube servers, named `Sonarqube`, with a token
  that may analyse into the project. `withSonarQubeEnv('Sonarqube')` finds it by that name.
- **The scanner**, under Manage Jenkins → Tools → SonarQube Scanner installations, named `sonarqube-scanner`.
- **A SonarQube webhook** to `<jenkins-url>/sonarqube-webhook/`, under Administration → Configuration → Webhooks
  in SonarQube. `waitForQualityGate` waits for it, and without it the Quality Gate stage times out after 5
  minutes.
- **`uv` and `git` on the agent**: the Install stage runs `uv sync --frozen`, and `uv` brings the Python of
  `.python-version`.
- **`gh` on the agent, and a GitHub token** for the Release stage: a Secret text credential with the ID
  `GitHub-Agents`, holding a token with *Contents: read and write* on this repository. It is the agents' own
  `gh` token, so when that is rotated the credential must be too, or every build of `main` fails at Release.
  Only builds of `main` use it.

### The two jobs

Both load the `Jenkinsfile` with "Pipeline script from SCM". The Checkout stage needs that: it runs
`checkout scm`, or fetches `GIT_REF` from `scm`'s remote.

- **The ordinary job**, a Multibranch Pipeline, builds `main` and every pull request. Only a build of `main`,
  that is a merge, runs the SonarQube stages: Community Edition has no branch analysis, so analysing a pull
  request would overwrite `main`'s analysis and its new-code baseline. Its branch source needs:
  - **Discover branches** set to *Exclude branches that are also filed as PRs*, so an `orchestrator/*` branch
    is built once, as its pull request.
  - **Filter by name (with wildcards)** excluding `orchestrator-ci/*`. The orchestrator pushes its snapshots
    there for the quality job and deletes them afterwards; the multibranch job would otherwise test each one
    as a branch of its own.

  The stages run on `BRANCH_NAME`, which only a multibranch job sets, so a plain Pipeline job running
  ordinary builds would test without analysing.
- **The quality job**, here `AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality`: a plain Pipeline job that loads the `Jenkinsfile`
  from `main`. It takes any ref as a parameter, keeps quality builds out of the ordinary job's history, and a
  change to the `Jenkinsfile` cannot alter how that same change is judged.

Jenkins learns a Jenkinsfile's `parameters` only by running it, so a new job needs one build, by hand and with
empty parameters, before it accepts `buildWithParameters`.

### Parameters

| Parameter | Set by the orchestrator to | When empty (ordinary builds) |
|---|---|---|
| `GIT_REF` | a branch on `origin`, e.g. `orchestrator-ci/cd3492-1-q2` | `checkout scm` |
| `SONAR_PROJECT_KEY` | the run's project, `py-ai-agents-orchestrator-<key>` | the key in `sonar-project.properties`; a failing test or a red gate fails the build |
| `SONAR_PROJECT_VERSION` | `base` or `change` | on `main`, the version in `pyproject.toml` |

The parameters reach the scanner as shell variables, never as Groovy-built shell code, so no value can inject a
command. `GIT_REF` is interpolated only into the checkout's branch spec.

### What each kind of build reports

- **Ordinary builds** stop at a failing test. On `main` they also fail on a gate status other than `OK`; a
  pull request or another branch is not analysed. `main` is analysed as the version in `pyproject.toml`, and
  the project's new code is set to "previous version", so the gate judges what changed since the version was
  last raised. Code from before the first versioned analysis counts as existing, not new.

  A build of `main` that passes the gate then releases the version in `pyproject.toml`: if GitHub has no
  release `v<version>` yet, `gh release create` makes one, with the tag at the commit built, notes generated
  from the pull requests merged since the last release, and `orchestrator.pyz` with its `.sha256` attached.
  The `.pyz` is `orchestrator.py` packed as a zipapp, built and started once with `--help` before the upload.
  To release, raise `version` in the pull request; a merge that leaves it alone releases nothing. A release
  that already exists is skipped whole, so its files are never replaced.
- **Quality builds** (`SONAR_PROJECT_KEY` set) run the analysis even when tests fail, end UNSTABLE when tests
  fail or the gate is not `OK`, and archive `.scannerwork/report-task.txt`, whose `ceTaskId` leads to the
  analysis. FAILURE then means the change never got as far as the gate.

## Credentials and permissions

The orchestrator talks to Jenkins and SonarQube itself, over HTTP from the machine it runs on, with these
credentials:

| Variable | What it holds |
|---|---|
| `JENKINS_URL` | Jenkins's base URL, e.g. `https://jenkins.example/` |
| `JENKINS_USER`, `JENKINS_TOKEN` | the technical user `agents` and an API token of it |
| `SONAR_HOST_URL` | SonarQube's base URL |
| `SONAR_TOKEN` | a user token of the technical user `agents` on SonarQube |

Put them in `~/.config/ai-agents-orchestrator/ci.env` (`$XDG_CONFIG_HOME/ai-agents-orchestrator/ci.env` if that
is set), one `NAME=value` per line, and `chmod 600` it:

```bash
JENKINS_URL=https://jenkins.example/
JENKINS_USER=agents
JENKINS_TOKEN=…
SONAR_HOST_URL=https://sonar.example/
SONAR_TOKEN=…
```

The orchestrator reads the file itself, on `run` and `resume`, so no wrapper is needed. Comments, blank lines,
quotes and `export ` are allowed, so the same file can also be sourced by a shell. A variable set in the
environment wins over the file, and the file is read only when one is missing. The orchestrator refuses a file
that others may read.

Never export them from `~/.bashrc` or a direnv `.envrc`: every agent's shell would have them. Values from the
file stay inside the orchestrator process and are never put into its environment.

- **Jenkins**: a user `agents` with Overall/Read, and Job/Read and Job/Build on the `AI-Agents-Orchestrator`
  folder.
- **SonarQube**: a user `agents` with the global permissions Create Projects and Execute Analysis, and a user
  token. A permission template `orchestrator-runs` with the key pattern `py-ai-agents-orchestrator-.+` gives
  Project Creators Browse, See Source Code and Administer, so `agents` can configure, read and delete the
  projects it creates. The project `py-ai-agents-orchestrator`, whose quality gate ("Sonar way") each run's
  project copies, must exist.
- **Jenkins's scanner token**, the one of the `Sonarqube` server in Jenkins, must be able to analyse into new
  projects.

The tokens travel only in the `Authorization` header, never in a URL, so no error message, `state.json` or log
can carry them; text copied from Jenkins or SonarQube into a quality file has both replaced with `****`. Every
process the orchestrator starts (herdr, and through `Host` git, ssh and gh) gets its environment without these
five variables, so the agents cannot inherit them.

**Known limitation**: the agents run as the same OS user as the orchestrator, so they can still read `ci.env`.
Stripping the variables from subprocesses prevents inheritance, not access.

## Orchestrator loop

With `--quality-gate JOB`, every Builder turn is followed by a quality round, in round 1 and in each round after
`CHANGES_REQUESTED`: Builder → quality gate → (if it does not pass, back to the Builder) → Reviewer.

```
build ──▶ quality ──▶ review
  ▲          │
  └── not OK, quality round < --max-quality-rounds
```

A quality round snapshots the working tree, pushes the snapshot to a throwaway branch on `origin`, has the
quality job analyse it into the run's own SonarQube project, and writes `quality-<n>-<q>.md`. The gate passes
when SonarQube's quality gate status is `OK`. Otherwise the Builder gets the quality file and answers it in
`build-<n>-q<q>.md`, and the next quality round analyses that. After `--max-quality-rounds` rounds (default 3)
the change goes to the Reviewer anyway, whose prompt names the last quality file as unresolved findings. A
quality round is one analysis, so three rounds are at most three analyses and two fix turns, and each review
round has its own budget. Without `--quality-gate`, a run behaves as it did before.

Failing tests with an `OK` gate do not fail a quality round: the gate does not count tests, and the Reviewer
runs them anyway.

### Snapshotting the uncommitted tree

The Builder leaves its work uncommitted, and Jenkins can only fetch commits. `Host.snapshot(cwd, parent,
message)` commits the working tree through a temporary index, in one `sh -c` script: it copies the real index,
runs `git add -A` and `git write-tree` on the copy, and `git commit-tree` with `state.base` as the parent. No ref
moves, and the index, HEAD and `git status` stay as they were. `git add -A` honours `.gitignore`, and
`.orchestrator/` ignores itself, so the snapshot holds what the pull request's commit would.

The snapshot is pushed with `--force` to `orchestrator-ci/<key>-<n>-q<q>`, and the base to
`orchestrator-ci/<key>-base`; `--force` replaces what a failed attempt left. A finished analysis deletes its
branch, and `done` deletes any the run left. The run's branch, its single commit and `_publish` are as before.

### The run's SonarQube project

Community Edition has no branch analysis, so each run gets its own project, `py-ai-agents-orchestrator-<key>`,
created by the orchestrator: it copies the quality gate of `py-ai-agents-orchestrator` and sets the new-code
period to "previous version". The base is analysed first, once per run, as version `base`, and every quality
round as `change`, so the base's analysis stays the baseline and each round measures the whole change. The
project is deleted at `done`; a failed run keeps it, because a resume needs it and it explains the last quality
file. SonarQube ignores coverage and duplication conditions below 20 new lines; the quality file reports
coverage on new code regardless.

### The calls

`CI` makes them with `urllib.request` over an injected `urlopen`, so the orchestrator stays stdlib-only and the
tests replace the servers. Jenkins takes HTTP Basic `JENKINS_USER:JENKINS_TOKEN` (no crumb with an API token);
SonarQube takes `SONAR_TOKEN` as the Basic user with an empty password. Requests time out after
`HTTP_TIMEOUT = 30` seconds. The job's full name may include folders: each `/` in it becomes `/job/` in its URL,
so `AI-Agents-Orchestrator/py-ai-agents-orchestrator-quality` is
`<JENKINS_URL>/job/AI-Agents-Orchestrator/job/py-ai-agents-orchestrator-quality/` (`<job>` below).

- **Preflight**, before the interview: the five variables are set; (1) `<job>/api/json?tree=property[parameterDefinitions[name]]`
  lists `GIT_REF`, `SONAR_PROJECT_KEY` and `SONAR_PROJECT_VERSION`; (2) `/api/authentication/validate` accepts
  the token. The directory must be a git repository, and a `--no-pr` run also needs a clean tree and an
  `origin`.
- **Once per run**: (3) `/api/qualitygates/get_by_project?project=py-ai-agents-orchestrator`; (4) POST
  `/api/projects/create` (when that answers 400, `/api/components/show` checks whether an earlier attempt
  already created it); (5) POST `/api/qualitygates/select` with that gate's name; (6) POST
  `/api/new_code_periods/set` with `type=PREVIOUS_VERSION`.
- **Per analysis**, the base first: (7) POST `<job>/buildWithParameters`, the queue item in `Location`; (8)
  poll `<JENKINS_URL>/queue/item/<id>/api/json` for `executable.number`; (9) poll
  `<job>/<number>/api/json?tree=building,result`; (10) `<job>/<number>/artifact/.scannerwork/report-task.txt`;
  (11) poll `/api/ce/task?id=<ceTaskId>` for `analysisId`; (12) `/api/qualitygates/project_status?analysisId=…`;
  (13) `/api/issues/search?components=<project>&resolved=false`, page by page; (14) `/api/measures/component`
  for `new_coverage`, `new_lines_to_cover`, `new_uncovered_lines`; (15) `<job>/<number>/testReport/api/json`;
  (16) the last 60 lines of `<job>/<number>/consoleText`, only for a build that ended before the analysis with
  no failing test. The base stops at call 11.
- **At `done`**: (17) POST `/api/projects/delete`.

An issue's severity is its highest `impacts` severity, or its older `severity` when it lists no impacts.

**Once a build ends**, live or resumed: with `report-task.txt`, whatever the result (ABORTED included, as when
the Quality Gate stage times out), the SonarQube task is followed. Without it and ABORTED, or cancelled in the
queue, nothing judged the change: the run fails with `ci` cut back to `ref` and `sha`, and the Builder gets no
fix turn. Without it otherwise: `GATE: BUILD_FAILED` and a fix turn, or for the base a failed run naming the
base commit. A SonarQube task that ends `FAILED` or `CANCELED` fails the run with `ci` cut back too. A build cut
back that way is recorded in `ci.rejected`, so a resume builds the snapshot again instead of adopting that
build.

**When Jenkins or SonarQube fails**, rather than the change: a request that got no answer, or a 5xx, is retried
every `CI_POLL_SECONDS = 10` until `QUALITY_TIMEOUT = 1200` seconds after the round started; the run then fails,
naming the step and keeping `ci`, and a resume gets a fresh timeout. Any other answer, such as 401, 403 or 404,
fails the run at once, naming the step, method, URL and status. A 404 on the trigger means the job was deleted.
The poll loops call `_heartbeat`, so a long build does not make the run look stale.

### What goes to the Builder

`quality-<n>-<q>.md` is written by the orchestrator in one write. Its first line is `GATE: OK`, `GATE: ERROR`
or `GATE: BUILD_FAILED`. Then:

- the failed conditions of the gate;
- the issues on lines that `git diff -U0 <base> <snapshot>` adds, and the lineless ones on files the diff
  touches, numbered by file and line, at most 50, then a count of the rest; the snapshot includes untracked
  files and is what Jenkins analysed; the other open issues are only counted;
- the failing tests, and the coverage on new code;
- for a build that ended before the analysis without a failing test, the last 60 lines of its console;
- a note when the change edits the `Jenkinsfile`, which the quality job reads from `main` so the edit did not
  take part, or `sonar-project.properties`, which the scanner read from the snapshot.

`QUALITY_FIX_PROMPT` names the quality file and the Builder's next report, `build-<n>-q<q>.md`, whose issues it
answers by number. A Builder in a fresh session gets `BUILD_PROMPT`, `REBUILD_NOTE` with every earlier report,
and `QUALITY_FIX_PROMPT`. The Reviewer's prompt ends with `QUALITY_PASSED_NOTE` or `QUALITY_UNRESOLVED_NOTE`,
naming the round's last quality file, and points at the Builder's last report of the round. The pull request's
description includes the last quality file.

### Run state and resume

`state.json` gains `quality_job` (None for a run without the gate, as for one saved before it),
`max_quality_rounds`, `quality_round`, `quality_project` (once calls 3–6 succeeded), `quality_baseline` (the
base's analysis id) and `ci`, `{ref, sha, queue_url, build, ce_task}`, saved key by key, with `rejected` as
above. Phase `build` with q = 0 goes to `quality` q = 1; `quality` q goes to `review` on `OK` or at q ≥
`max_quality_rounds`, else to the fix turn, `build` with the same q, then `quality` q + 1; `CHANGES_REQUESTED`
goes to `build`, round n + 1, q = 0. `list` shows the phase `quality` and, in `build` and `quality`, the
quality round.

A resume in `quality` takes the furthest step the state allows: an existing quality file (one without a valid
`GATE:` line is refused, as an empty handoff file is); the project, then the base analysis; `ci.ce_task`;
`ci.build`; `ci.queue_url` (when Jenkins has forgotten the queue item, the build with that `GIT_REF` among the
job's last 20); `ci.ref` alone, where a build with that `GIT_REF` is adopted before a new one is triggered;
otherwise snapshot, push and trigger.

### CLI

`run` takes `--quality-gate JOB` and `--max-quality-rounds N`; `resume` takes `--max-quality-rounds N`.
`--max-quality-rounds` below 1 is refused, and so is it on `run` without `--quality-gate` or on `resume` of a run
without the gate. `resume` refuses a limit below `quality_round` in phase `quality`, or up to it in `build` with
q ≥ 1, where the Builder is answering quality round q. Turning the gate on or off in the middle of a run is
not supported; see the [roadmap](roadmap.md).

### Edge cases

- **Concurrent runs** share only executors and SonarQube's one-at-a-time queue; `QUALITY_TIMEOUT` allows for it.
- **Two runs in one checkout**: a snapshot takes every change in the tree, so it would take the other run's
  too. A run refuses to start or resume while another run in its checkout is live, so the two never overlap;
  parallel runs need separate clones until `--worktree` in the roadmap.
- **No change**: the gate passes, and `_publish` refuses the empty change.
- **Cleanup**: a failed run keeps its project and the branch in flight; `done` deletes the run's
  `orchestrator-ci/<key>-*` branches and its project, and a failed delete is only logged.

### Open questions

- **The version baseline**: does "previous version" hold across three `change` analyses on the live server? If
  the baseline drifts, set the new-code period to "specific analysis", the base's, instead.
- **Blame**: new lines need history down to the base, which a shallow clone lacks; check the clone options.
- **Permissions**: can Jenkins's scanner token analyse into the projects `agents` creates? Confirm on the first
  live run.
- **Reachability under `--machine`**: HTTP runs on the orchestrator's machine and git on the agents', meeting
  at `origin`. If only the agents' machine reaches Jenkins, the calls would need to go through `Host`, with
  curl taking its config on stdin.
