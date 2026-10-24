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
    }

    environment {
        UV_NO_PROGRESS = '1'
        COVERAGE_FILE  = '.coverage'
        // For the UI and Package stages: crates are fetched into the workspace, which cleanWs empties, so no build
        // compiles sources an earlier build left behind.
        CARGO_HOME     = "${env.WORKSPACE}/.cargo-home"
        // The GitHub login Jenkins posts pull request statuses as, its GitHub App's "<slug>[bot]", which the agents
        // cannot post as. Empty: no build of main trusts a pull request build, and each judges itself.
        PR_STATUS_CREATOR = 'davidsiegl-jenkins[bot]'
    }

    stages {
        stage('Checkout') {
            steps {
                script {
                    def checkedOut
                    if (params.GIT_REF) {
                        checkedOut = checkout([$class: 'GitSCM',
                                               branches: [[name: "refs/heads/${params.GIT_REF}"]],
                                               userRemoteConfigs: scm.userRemoteConfigs])
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
                script {
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
            }
            steps {
                // A failing test does not stop the build here: the Gate stage judges it with everything else, so a
                // quality build's report lists failing tests and findings together. Any other exit status means
                // pytest itself could not run.
                sh '''
                    status=0
                    uv run pytest --cov=orchestrator --cov-branch --cov-report=xml --junitxml=test-results.xml || status=$?
                    [ "$status" -le 1 ] || exit "$status"
                '''
            }
            post {
                always {
                    junit 'test-results.xml'
                }
            }
        }

        // The desktop app: its frontend's types, lint and Vitest tests, and its Tauri shell's cargo tests. The gate
        // judges only the Python, so this stage stops the build instead, before Gate: a quality round then reports
        // BUILD_FAILED with the console's end, where a failure after Gate would hide under quality.md's passing GATE
        // line.
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
                dir('ui/src-tauri') {
                    sh 'cargo test --locked'
                }
            }
        }

        // orchestrator.py gate (docs/quality-gate.md): ruff, mypy, pymarkdown, shellcheck and actionlint, the tests and
        // coverage of new code, judged on the lines the change adds. A quality build judges its snapshot by main's
        // gate and main's rules, so a change cannot loosen how it is itself judged, and goes UNSTABLE on a red gate;
        // a pull request or main fails. A gate that could not judge fails any build.
        stage('Gate') {
            when { expression { return env.JUDGED_AT_PR != 'true' } }
            steps {
                script {
                    def gate = 'orchestrator.py'
                    def rules = 'pyproject.toml'
                    def base
                    if (params.GIT_REF) {
                        // The snapshot's only parent is the run's base.
                        base = 'HEAD^'
                        sh '''
                            git fetch --quiet origin +refs/heads/main:refs/remotes/origin/main
                            mkdir -p "$WORKSPACE_TMP/main"
                            git show origin/main:orchestrator.py > "$WORKSPACE_TMP/main/orchestrator.py"
                            git show origin/main:pyproject.toml > "$WORKSPACE_TMP/main/pyproject.toml"
                        '''
                        gate = "${env.WORKSPACE_TMP}/main/orchestrator.py"
                        rules = "${env.WORKSPACE_TMP}/main/pyproject.toml"
                    } else if (env.CHANGE_TARGET) {
                        sh 'git fetch --quiet origin "+refs/heads/$CHANGE_TARGET:refs/remotes/origin/$CHANGE_TARGET"'
                        base = sh(returnStdout: true, script: 'git merge-base HEAD "origin/$CHANGE_TARGET"').trim()
                    } else {
                        base = env.PREVIOUS_SUCCESSFUL_COMMIT ?: 'HEAD^1'
                    }
                    // The values reach the shell as environment variables, never as Groovy-built shell code.
                    def status
                    withEnv(["GATE=${gate}", "GATE_RULES=${rules}", "GATE_BASE=${base}"]) {
                        status = sh(returnStatus: true, script: '''
                            uv run --frozen python "$GATE" gate --base "$GATE_BASE" --config "$GATE_RULES" \\
                                --tests test-results.xml --coverage coverage.xml --out quality.md
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
        // its run waits for the whole build, within QUALITY_TIMEOUT, for a quality file the Gate stage has already
        // archived, and the change is packaged by its pull request's build before it merges. The macOS ones come
        // from .github/workflows/macos-binary.yml, with the same scripts, since Jenkins has no macOS machine.
        stage('Package') {
            when { expression { return !params.GIT_REF } }
            steps {
                sh 'packaging/build-binary.sh'
                sh 'packaging/build-desktop.sh'
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
                        for f in orchestrator-linux-x86_64 orchestrator-desktop-linux-x86_64.deb \
                                 orchestrator-desktop-linux-x86_64.AppImage; do
                            [ -f "dist/$f.sha256" ] || { echo "no dist/$f; run the whole build" >&2; exit 1; }
                        done
                        # orchestrator.py packed as an executable zipapp, which runs on any machine with Python 3.13+.
                        mkdir -p build/pyz dist
                        cp orchestrator.py build/pyz/
                        echo 'import sys, orchestrator; sys.exit(orchestrator.main(sys.argv[1:]))' > build/pyz/__main__.py
                        uv run --frozen python -m zipapp build/pyz -p '/usr/bin/env python3' -c -o dist/orchestrator.pyz
                        uv run --frozen python dist/orchestrator.pyz --help >/dev/null
                        (cd dist && sha256sum orchestrator.pyz > orchestrator.pyz.sha256)
                        # Publishing it starts .github/workflows/macos-binary.yml, which adds the macOS binary and app.
                        gh release create "$tag" dist/orchestrator.pyz dist/orchestrator.pyz.sha256 \
                            dist/orchestrator-linux-x86_64 dist/orchestrator-linux-x86_64.sha256 \
                            dist/orchestrator-desktop-linux-x86_64.deb dist/orchestrator-desktop-linux-x86_64.deb.sha256 \
                            dist/orchestrator-desktop-linux-x86_64.AppImage \
                            dist/orchestrator-desktop-linux-x86_64.AppImage.sha256 \
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
