pipeline {
    // Agents wrote the code this builds, so it runs where they already do: the agent on their own box.
    // Never on the agents that deploy the homelab. Jenkins enforces that per node, not this label.
    agent { label 'untrusted' }

    // Empty in an ordinary build, which builds its own branch or pull request.
    parameters {
        string(name: 'GIT_REF', defaultValue: '', description: 'Branch to build, e.g. orchestrator-ci/cd3492-1-q2')
    }

    options {
        skipDefaultCheckout()
        // Per job, and per branch or pull request in the multibranch one; a discarded build takes its archived
        // quality.md with it, and a resumed run that still names it builds its ref again. Jenkins never discards the
        // last successful build, which GIT_PREVIOUS_SUCCESSFUL_COMMIT names for the Gate stage.
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    environment {
        UV_NO_PROGRESS = '1'
        COVERAGE_FILE  = '.coverage'
        // For the Test, UI, Gate and Package stages: crates are fetched into the workspace, which cleanWs empties, so
        // no build compiles sources an earlier build left behind.
        CARGO_HOME     = "${env.WORKSPACE}/.cargo-home"
        // Tools the Install stage downloads into the workspace (ci/install-tools.sh), first on PATH for every stage.
        PATH           = "${env.WORKSPACE}/.tools/bin:${env.PATH}"
        // The dev profile, which the Test stage's test build and clippy's checks inherit, without debug info. Debug
        // info for the GTK and Tauri crates took enough memory that rustc was OOM-killed with two builds on
        // agentbox0. ci/cargo-lock.sh now keeps two builds' cargo steps apart, except a quality build's, which never
        // waits for the lock, and the Gate stage's clippy, which gate.py runs outside it; building without debug info
        // keeps each build's own peak lower. A failing test still prints its panic's file:line, which comes from
        // core::panic::Location, not from debug info. Set for the whole build, not the UI stage alone, so that the
        // Gate stage's clippy finds the dependencies the UI stage's cargo check checked fresh, and checks only the
        // shell's own crate outside the lock; a difference between the two stages' cargo environments, such as a
        // RUSTFLAGS one of them sets, would have it check them all again there.
        CARGO_PROFILE_DEV_DEBUG = '0'
        // The GitHub login Jenkins posts pull request statuses as, its GitHub App's "<slug>[bot]", which the agents
        // cannot post as. Empty: no build of main trusts a pull request build, and each judges itself.
        PR_STATUS_CREATOR = 'davidsiegl-jenkins[bot]'
    }

    stages {
        // A quality build (GIT_REF set) records what it judges as its build description, before Install runs any
        // code of the change: exactly `judged tree=<sha> parent=<sha> main=<sha>`, full lowercase shas, one space
        // apart. tree is the snapshot's tree, parent its only parent, the run's base, and main the origin/main
        // commit whose gate and rules the Gate stage judges with. `orchestrator judged-in-quality` parses it, by
        // JUDGED_DESCRIPTION. A build restarted from a later stage records none, so nothing trusts it. No later stage
        // may fetch GIT_REF: the orchestrator deletes that branch once the Gate stage has archived quality.md, while
        // the build may still run (docs/quality-gate.md, "The calls").
        stage('Checkout') {
            steps {
                script {
                    def checkedOut
                    if (params.GIT_REF) {
                        checkedOut = checkout([$class: 'GitSCM',
                                               branches: [[name: "refs/heads/${params.GIT_REF}"]],
                                               userRemoteConfigs: scm.userRemoteConfigs])
                        sh 'git fetch --quiet origin +refs/heads/main:refs/remotes/origin/main'
                        env.JUDGED_MAIN = sh(returnStdout: true,
                                             script: 'git rev-parse --verify "origin/main^{commit}"').trim()
                        // Each assignment fails the step on its own, under sh's -e, where a substitution inside
                        // printf's arguments would not.
                        currentBuild.description = sh(returnStdout: true, script: '''
                            tree=$(git rev-parse --verify 'HEAD^{tree}')
                            parent=$(git rev-parse --verify 'HEAD^')
                            printf 'judged tree=%s parent=%s main=%s' "$tree" "$parent" "$JUDGED_MAIN"
                        ''').trim()
                    } else {
                        checkedOut = checkout scm
                    }
                    // The Gate stage measures a build of main from here.
                    env.PREVIOUS_SUCCESSFUL_COMMIT = checkedOut.GIT_PREVIOUS_SUCCESSFUL_COMMIT ?: ''
                }
            }
        }

        // A build of main skips Test and Gate when a pull request build already judged its tree
        // (ci/judged-at-pr.sh). Whatever stops the script from saying so, the build judges the tree itself.
        stage('Judged at PR') {
            when {
                branch 'main'
                expression { return !params.GIT_REF }
            }
            steps {
                withCredentials([string(credentialsId: 'GitHub-Agents', variable: 'GH_TOKEN')]) {
                    script {
                        if (sh(returnStatus: true, script: 'ci/judged-at-pr.sh') == 0) {
                            env.JUDGED_AT_PR = 'true'
                        }
                    }
                }
            }
        }

        stage('Install') {
            steps {
                sh 'uv sync --frozen'
                // The Test stage's TestClippy and the Gate stage's gate.py run the real clippy, and the Test stage's
                // cargo-llvm-cov reads its coverage data with llvm-tools' llvm-profdata and llvm-cov, which must be the
                // toolchain's own LLVM. Both are rustup components the agent image provides: its rustup home,
                // /usr/local/rustup, is root's, so a build cannot add to it (PR-163's builds 1-3 failed adding
                // llvm-tools-preview: "component download failed ... No such file or directory"). This step names a
                // missing one here rather than letting a later stage fail on it. rustup reads rust-toolchain.toml here,
                // so it lists the components of the toolchain that file pins, which must be the image's too: rustup
                // cannot install another one either.
                sh '''
                    installed=$(rustup component list --installed)
                    for component in clippy llvm-tools; do
                        printf '%s\\n' "$installed" | grep -q "^$component-" || {
                            echo "rustup's $component component is not on the agent; its image must add it" >&2
                            exit 1
                        }
                    done
                '''
                sh 'ci/install-tools.sh "$WORKSPACE/.tools/bin"'
                script {
                    // gate.py reads Rust through the gate-rust helper (docs/quality-gate.md, "Reading Rust: the
                    // gate-rust helper"), built here from the tree the Gate stage takes gate.py from: a quality build's
                    // from the judged main, as Checkout named it, any other build's from its own. A main from before
                    // the helper has no ci/gate-rust, and its gate.py no --rust-syntax, so nothing is built. The target
                    // directory is in the workspace, which cleanWs empties, so no build runs what an earlier one
                    // compiled; the lock as in the UI stage, which says why a quality build may skip it. The Gate
                    // stage's clippy lints the tree's own ci/gate-rust as a workspace of its own, so that is checked
                    // here too, for the reason the UI stage checks the shell: with syn checked, clippy checks only the
                    // helper, in under a second rather than 5 on the development host. Neither stops the build when it
                    // fails: a helper that did not build is one the gate cannot run, which each check that reads Rust
                    // reports as not run on a change to Rust (so a failing check fails the gate), while a change to no
                    // Rust is judged as before; and a check that failed leaves the error to the Gate stage's clippy.
                    if (env.JUDGED_AT_PR != 'true') {
                        sh '''
                            src=ci/gate-rust
                            if [ -n "${GIT_REF:-}" ]; then
                                : "${JUDGED_MAIN:?is unset; run the whole build, not a restart from a later stage}"
                                src="$WORKSPACE_TMP/main/ci/gate-rust"
                                rm -rf "$src"
                                mkdir -p "$WORKSPACE_TMP/main"
                                if git cat-file -e "$JUDGED_MAIN:ci/gate-rust/Cargo.toml" 2>/dev/null; then
                                    git archive -o "$WORKSPACE_TMP/main/gate-rust.tar" "$JUDGED_MAIN" ci/gate-rust
                                    tar -x -f "$WORKSPACE_TMP/main/gate-rust.tar" -C "$WORKSPACE_TMP/main"
                                fi
                            fi
                            if [ -f "$src/Cargo.toml" ]; then
                                "$WORKSPACE/ci/cargo-lock.sh" ${GIT_REF:+--no-wait} cargo build --release --locked \\
                                    --manifest-path "$src/Cargo.toml" --target-dir "$WORKSPACE/.gate-rust-target" \\
                                    || echo "the gate-rust helper did not build; gate.py reports Rust as not run" >&2
                            fi
                            if [ -f ci/gate-rust/Cargo.toml ]; then
                                "$WORKSPACE/ci/cargo-lock.sh" ${GIT_REF:+--no-wait} cargo check --locked --all-targets \\
                                    --manifest-path ci/gate-rust/Cargo.toml \\
                                    || echo "cargo check of ci/gate-rust failed; the Gate stage's clippy judges it" >&2
                            fi
                        '''
                    }
                    // The version a release on main is tagged with.
                    env.PROJECT_VERSION = sh(returnStdout: true, script: '''
                        uv run --frozen python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])'
                    ''').trim()
                }
            }
        }

        stage('Test') {
            when { expression { return env.JUDGED_AT_PR != 'true' } }
            environment {
                // tests/conftest.py's larger profile: a build tries ten times the examples a local run does.
                HYPOTHESIS_PROFILE = 'ci'
                // What both harnesses' scenarios depend on, which a quality build's change must leave alone for it to
                // leave them out (the steps below): the engine and the Cargo workspace it builds in, the example
                // workflows they run, and ci/, which pins the gitleaks they run. Each sh step adds its harness's own.
                SCENARIO_INPUTS = 'engine Cargo.toml Cargo.lock rust-toolchain.toml examples ci'
            }
            steps {
                // A failing test does not stop the build here: the Gate stage judges it with everything else, so a
                // quality build's report lists failing tests and findings together. Any other exit status means
                // pytest itself could not run.
                //
                // The Rust tests of every member of the top-level Cargo workspace (Cargo.toml), the engine's and the
                // desktop shell's, with line coverage, for the Gate stage likewise: --ignore-run-fail writes the
                // coverage report whatever tests fail and exits 0 for them, so a non-zero status is a build that did
                // not compile or a tool that could not run. ci/nextest.toml has the ci profile write the JUnit report
                // the Gate stage reads. Once the tests are built, --ignore-run-fail turns any failure of nextest's run
                // into a warning (run_nextest in cargo-llvm-cov 0.9.1's src/main.rs), also one that runs no test,
                // such as a test binary that cannot list its tests; so the JUnit report is removed first and must be
                // there after. The lock as in the UI stage, which says why a quality build may skip it.
                //
                // The e2e scenarios (engine/e2e) run the orchestrator binary this run builds, through E2E_ENGINE, as
                // the parity scenarios below do; their World keeps its profile out of the scenario's project itself.
                // A quality build leaves them out by a nextest filter when its change leaves SCENARIO_INPUTS alone.
                sh '''
                    junit=target/nextest/ci/junit.xml
                    rm -f "$junit"
                    set --
                    # $SCENARIO_INPUTS unquoted: the stage's list of paths, split into its words, here and below.
                    if [ -n "${GIT_REF:-}" ] && git diff --quiet HEAD^ HEAD -- $SCENARIO_INPUTS; then
                        set -- -E 'not package(octoflow-e2e)'
                    fi
                    E2E_ENGINE="'$WORKSPACE/target/llvm-cov-target/debug/orchestrator'" \\
                    ci/cargo-lock.sh ${GIT_REF:+--no-wait} cargo llvm-cov nextest --locked --workspace \\
                        --ignore-run-fail --profile ci --config-file "$WORKSPACE/ci/nextest.toml" \\
                        --cobertura --output-path "$WORKSPACE/coverage-rust.xml" "$@"
                    if [ ! -f "$junit" ]; then
                        echo "cargo-nextest wrote no $junit, so the Rust tests did not run" >&2
                        exit 1
                    fi
                '''
                // The parity scenarios (tests/parity) replay the Python engine's recordings against the Rust engine's
                // orchestrator binary, and tests/test_docs.py reads its help: the one the Rust tests above built,
                // instrumented for coverage, through tests/rust_engine.py's ORCHESTRATOR_BIN and the harness's
                // PARITY_ENGINE, so that pytest compiles no engine of its own. Run so, it would write its profile
                // where it runs, into a scenario's project, so LLVM_PROFILE_FILE is /dev/null, which PARITY_ENGINE
                // passes through env, the harness giving the engine an environment of its own.
                //
                // A quality build (GIT_REF set) leaves out the parity scenarios when its change, against HEAD^, the
                // run's base, touches nothing they depend on: SCENARIO_INPUTS, the harness, tests/conftest.py's guards
                // they run under and tests/guard_errors.py's errors those guards raise, and the Python environment. They took quality build #59 past the orchestrator's QUALITY_TIMEOUT
                // (1299 s of 1200). A git diff that fails counts as a change, so they run. Pull request builds always
                // run them. [tool.mutmut] in pyproject.toml leaves them out of mutmut's runs.
                //
                // --cov-context=test records which tests run each line, in COVERAGE_FILE, for the Gate stage's
                // --test-contexts: mutmut then runs only the tests that reach the changed functions. It takes about 7 %
                // longer, under the ctrace core pyproject.toml pins. COVERAGE_FILE stays relative, in the workspace:
                // set to an absolute path, under TMPDIR or elsewhere, the file vanished during a run and over 2300
                // tests errored.
                sh '''
                    engine="$WORKSPACE/target/llvm-cov-target/debug/orchestrator"
                    if [ ! -x "$engine" ]; then
                        echo "the Rust tests built no $engine" >&2
                        exit 1
                    fi
                    parity=''
                    if [ -n "${GIT_REF:-}" ] && git diff --quiet HEAD^ HEAD -- $SCENARIO_INPUTS tests/parity \
                            tests/rust_engine.py tests/conftest.py tests/guard_errors.py pyproject.toml uv.lock \
                            .python-version; then
                        parity='--ignore=tests/parity'
                    fi
                    status=0
                    LLVM_PROFILE_FILE=/dev/null ORCHESTRATOR_BIN="$engine" \\
                    PARITY_ENGINE="env LLVM_PROFILE_FILE=/dev/null '$engine'" \\
                    uv run pytest $parity --cov=gate --cov-branch --cov-context=test --cov-report=xml \\
                        --junitxml=test-results.xml || status=$?
                    [ "$status" -le 1 ] || exit "$status"
                '''
            }
            post {
                always {
                    junit 'test-results.xml, target/nextest/ci/junit.xml'
                }
            }
        }

        // The desktop app's frontend: its types, lint and Vitest tests; its Tauri shell's tests run in the Test
        // stage, with the rest of the Cargo workspace's. The gate judges no tests but those the Test stage reports, so
        // this stage stops the build instead, before Gate: a quality round then reports BUILD_FAILED with the
        // console's end, where a failure after Gate would hide under quality.md's passing GATE line.
        stage('UI') {
            when { expression { return env.JUDGED_AT_PR != 'true' } }
            steps {
                dir('ui') {
                    sh '''
                        npm ci --no-audit --no-fund
                        npm run typecheck
                        npm run lint
                        npm test
                    '''
                }
                // The Gate stage's clippy checks the workspace's dependencies as cargo check does, not as the Test
                // stage's cargo-llvm-cov builds them, instrumented and in a target directory of its own: checked here,
                // under the lock, they leave clippy only the workspace's own crates to check. A quality build (GIT_REF
                // set) runs within the orchestrator's QUALITY_TIMEOUT, which a wait behind another build's Package
                // stage would exceed, so it takes the lock only when it is free.
                sh 'ci/cargo-lock.sh ${GIT_REF:+--no-wait} cargo check --locked --workspace --all-targets'
            }
        }

        // gate.py (docs/quality-gate.md): ruff, mypy, pymarkdown, shellcheck, actionlint and clippy, the tests and
        // coverage of new code, judged on the lines the change adds. A quality build judges its snapshot by main's gate
        // and main's rules, so a change cannot loosen how it is itself judged, but for clippy's, which are the tree's
        // own Cargo.toml (TODO.md); it goes UNSTABLE on a red gate, and a pull request or main fails. A gate that could
        // not judge fails any build.
        stage('Gate') {
            when { expression { return env.JUDGED_AT_PR != 'true' } }
            steps {
                script {
                    def gate = 'gate.py'
                    def rules = 'pyproject.toml'
                    def base
                    if (params.GIT_REF) {
                        // The snapshot's only parent is the run's base.
                        base = 'HEAD^'
                        // The gate and rules of the main commit Checkout named in the build's description, fetched
                        // there. A main without gate.py, from before the gate split, cannot judge: this step fails, and
                        // with it the build, writing no quality.md, rather than running some other program.
                        sh '''
                            : "${JUDGED_MAIN:?is unset; run the whole build, not a restart from a later stage}"
                            if ! git cat-file -e "$JUDGED_MAIN:gate.py" 2>/dev/null; then
                                echo "main at $JUDGED_MAIN has no gate.py, so nothing can judge this snapshot" >&2
                                exit 2
                            fi
                            mkdir -p "$WORKSPACE_TMP/main"
                            git show "$JUDGED_MAIN:pyproject.toml" > "$WORKSPACE_TMP/main/pyproject.toml"
                            git show "$JUDGED_MAIN:gate.py" > "$WORKSPACE_TMP/main/gate.py"
                        '''
                        gate = "${env.WORKSPACE_TMP}/main/gate.py"
                        rules = "${env.WORKSPACE_TMP}/main/pyproject.toml"
                    } else if (env.CHANGE_TARGET) {
                        base = sh(returnStdout: true, script: 'ci/pr-base.sh').trim()
                    } else {
                        // A history rewritten since the last successful build, by a force-push, leaves that build's
                        // commit out of this checkout, where gate.py cannot judge from it ("names no commit", exit
                        // status 2) in this and every later build; such a build measures from HEAD^1, as one with no
                        // successful build before it does.
                        def previous = env.PREVIOUS_SUCCESSFUL_COMMIT
                        def kept = previous && sh(returnStatus: true,
                                                  script: 'git cat-file -e "$PREVIOUS_SUCCESSFUL_COMMIT^{commit}"') == 0
                        base = kept ? previous : 'HEAD^1'
                    }
                    // The values reach the shell as environment variables, never as Groovy-built shell code.
                    def status
                    // Each report of the Rust's before the Python's: a main whose gate.py takes --tests and --coverage
                    // once, as main's did before the commit that brought this order, keeps the last of each. A quality
                    // build judged by such a main, of a snapshot that carries this Jenkinsfile, is then judged on its
                    // Python as before, but not on its Rust tests, which only the junit step's UNSTABLE shows, nor on
                    // the Rust's coverage; that window closes when main's gate.py takes both more than once. Likewise
                    // --test-contexts goes only to a gate.py that defines it, so a quality build judged by a main from
                    // before it has mutmut run its whole selection rather than the gate fail on an unknown option; and
                    // so does --rust-syntax, with the helper the Install stage built from the same tree as that gate.py.
                    withEnv(["GATE=${gate}", "GATE_RULES=${rules}", "GATE_BASE=${base}"]) {
                        status = sh(returnStatus: true, script: '''
                            set --
                            if grep -q -- '"--test-contexts"' "$GATE"; then
                                set -- --test-contexts "$COVERAGE_FILE"
                            fi
                            if grep -q -- '"--rust-syntax"' "$GATE"; then
                                set -- "$@" --rust-syntax "$WORKSPACE/.gate-rust-target/release/gate-rust"
                            fi
                            uv run --frozen python "$GATE" --base "$GATE_BASE" --config "$GATE_RULES" \\
                                --tests target/nextest/ci/junit.xml --tests test-results.xml \\
                                --coverage coverage-rust.xml --coverage coverage.xml "$@" --out quality.md
                        ''')
                    }
                    if (status == 1 && params.GIT_REF) {
                        unstable('The quality gate did not pass; see quality.md')
                    } else if (status == 1) {
                        error('The quality gate did not pass; see quality.md')
                    } else if (status != 0) {
                        error("The quality gate could not judge the change (exit status ${status})")
                    }
                }
            }
            post {
                always {
                    archiveArtifacts artifacts: 'quality.md', allowEmptyArchive: true
                }
            }
        }

        // The Linux binary and the desktop app around it, built on every pull request and main build, so a change
        // that breaks either fails before it is merged; Release attaches them. Not in a quality build (GIT_REF set):
        // nothing uses a snapshot's package, since the change is packaged by its pull request's build before it
        // merges, and the orchestrator run that triggered the build has taken the Gate stage's quality.md by then.
        // The macOS ones come from .github/workflows/macos-binary.yml, with the same scripts, since Jenkins has no
        // macOS machine.
        stage('Package') {
            when { expression { return !params.GIT_REF } }
            steps {
                sh 'packaging/build-binary.sh'
                sh 'ci/cargo-lock.sh packaging/build-desktop.sh'
            }
        }

        // A merge into main that raises the version in pyproject.toml releases it; any other merge finds that
        // release already there. gh creates the tag v<version> on GitHub at the commit built, so Jenkins needs
        // no git push credentials.
        stage('Release') {
            when {
                branch 'main'
                // A build pointed at main's job with GIT_REF checks out that branch, never to be released.
                expression { return !params.GIT_REF }
            }
            steps {
                withCredentials([string(credentialsId: 'GitHub-Agents', variable: 'GH_TOKEN')]) {
                    sh '''
                        # Install sets it, so a restart from this stage, which skips Install, has none and must not release "v".
                        : "${PROJECT_VERSION:?is unset; run the whole build, not a restart from the Release stage}"
                        tag="v$PROJECT_VERSION"
                        sha=$(git rev-parse HEAD)
                        if gh release view "$tag" >/dev/null 2>gh-release-view.err; then
                            echo "$tag is already released"
                            exit 0
                        fi
                        # Anything but a missing release, e.g. a bad token, must fail rather than release twice.
                        if ! grep -q 'release not found' gh-release-view.err; then
                            cat gh-release-view.err >&2
                            exit 1
                        fi
                        # Package builds them on every build that reaches Release.
                        for f in octoflow-linux-x86_64 octoflow-desktop-linux-x86_64.deb \
                                 octoflow-desktop-linux-x86_64.AppImage; do
                            [ -f "dist/$f.sha256" ] || { echo "no dist/$f; run the whole build" >&2; exit 1; }
                        done
                        # One file for every asset, the macOS ones too (packaging/about.toml).
                        [ -f dist/octoflow-third-party-licenses.html ] \
                            || { echo "no dist/octoflow-third-party-licenses.html; run the whole build" >&2; exit 1; }
                        # A release's own words, docs/releases/v<version>.md, when there is such a file: GitHub puts
                        # them before the notes it generates from the pull requests.
                        notes=docs/releases/$tag.md
                        set --
                        [ ! -f "$notes" ] || set -- --notes-file "$notes"
                        # Publishing it starts .github/workflows/macos-binary.yml, which adds the macOS binary and app.
                        gh release create "$tag" "$@" \
                            dist/octoflow-linux-x86_64 dist/octoflow-linux-x86_64.sha256 \
                            dist/octoflow-desktop-linux-x86_64.deb dist/octoflow-desktop-linux-x86_64.deb.sha256 \
                            dist/octoflow-desktop-linux-x86_64.AppImage \
                            dist/octoflow-desktop-linux-x86_64.AppImage.sha256 \
                            dist/octoflow-third-party-licenses.html \
                            --target "$sha" --title "$tag" --generate-notes
                    '''
                }
            }
        }
    }

    // Mail on a failure and on the first green build after one, never on a run that merely succeeded again. Not for
    // a quality build: the orchestrator run that triggered it hands its result to the Builder.
    post {
        always {
            cleanWs()
        }
        failure {
            script {
                if (!params.GIT_REF) {
                    emailext(
                        to: 'david.siegl95@gmail.com',
                        subject: "FAILED  ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                        body: """Failed - see the attached log.

Gate report, if the build reached the Gate stage: ${env.BUILD_URL}artifact/quality.md
${env.BUILD_URL}console""",
                        attachLog: true,
                        compressLog: true
                    )
                }
            }
        }
        fixed {
            script {
                if (!params.GIT_REF) {
                    emailext(
                        to: 'david.siegl95@gmail.com',
                        subject: "RECOVERED  ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                        body: "${env.BUILD_URL}"
                    )
                }
            }
        }
    }
}
