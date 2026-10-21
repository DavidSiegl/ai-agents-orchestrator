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
    }

    stages {
        stage('Checkout') {
            steps {
                script {
                    if (params.GIT_REF) {
                        checkout([$class: 'GitSCM',
                                  branches: [[name: "refs/heads/${params.GIT_REF}"]],
                                  userRemoteConfigs: scm.userRemoteConfigs])
                    } else {
                        checkout scm
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
            steps {
                sh 'uv run pytest --cov=orchestrator --cov-branch --junitxml=test-results.xml'
            }
            post {
                always {
                    junit 'test-results.xml'
                }
            }
        }

        // The Linux binary, built on every build, so a change that breaks it fails before it is merged; Release
        // attaches it. The macOS binary comes from .github/workflows/macos-binary.yml, with the same script, since
        // Jenkins has no macOS machine.
        stage('Package') {
            steps {
                sh 'packaging/build-binary.sh'
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
                        # Package builds it on every build that reaches Release.
                        [ -f dist/orchestrator-linux-x86_64.sha256 ] || { echo "no Linux binary; run the whole build" >&2; exit 1; }
                        # orchestrator.py packed as an executable zipapp, which runs on any machine with Python 3.13+.
                        mkdir -p build/pyz dist
                        cp orchestrator.py build/pyz/
                        echo 'import sys, orchestrator; sys.exit(orchestrator.main(sys.argv[1:]))' > build/pyz/__main__.py
                        uv run --frozen python -m zipapp build/pyz -p '/usr/bin/env python3' -c -o dist/orchestrator.pyz
                        uv run --frozen python dist/orchestrator.pyz --help >/dev/null
                        (cd dist && sha256sum orchestrator.pyz > orchestrator.pyz.sha256)
                        # Publishing it starts .github/workflows/macos-binary.yml, which adds the macOS binary.
                        gh release create "$tag" dist/orchestrator.pyz dist/orchestrator.pyz.sha256 \
                            dist/orchestrator-linux-x86_64 dist/orchestrator-linux-x86_64.sha256 \
                            --target "$sha" --title "$tag" --generate-notes
                    '''
                }
            }
        }
    }

    post {
        always {
            cleanWs()
        }
    }
}
