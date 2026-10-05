pipeline {
    agent any

    // All empty in an ordinary build, which then behaves as before.
    parameters {
        string(name: 'GIT_REF', defaultValue: '', description: 'Branch to build, e.g. orchestrator-ci/cd3492-1-q2')
        string(name: 'SONAR_PROJECT_KEY', defaultValue: '', description: 'SonarQube project key; empty uses sonar-project.properties')
        string(name: 'SONAR_PROJECT_VERSION', defaultValue: '', description: 'sonar.projectVersion; empty leaves it unset')
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
            }
        }

        stage('Test') {
            steps {
                script {
                    def pytest = 'uv run pytest --cov=orchestrator --cov-branch --cov-report=xml:coverage.xml --junitxml=test-results.xml'
                    if (params.SONAR_PROJECT_KEY) {
                        // A quality build is analysed even when tests fail, so the Builder hears about both at once.
                        catchError(buildResult: 'UNSTABLE', stageResult: 'FAILURE') {
                            sh pytest
                        }
                    } else {
                        sh pytest
                    }
                }
            }
            post {
                always {
                    junit 'test-results.xml'
                }
            }
        }

        // A multibranch job analyses only main: Community Edition has no branch analysis, so a branch or pull
        // request would overwrite main's analysis and its new-code baseline. Quality builds always analyse,
        // into their run's own project; their plain Pipeline job has no BRANCH_NAME.
        stage('SonarQube') {
            when {
                anyOf {
                    branch 'main'
                    expression { return params.SONAR_PROJECT_KEY as boolean }
                }
            }
            stages {
                stage('SonarQube Analysis') {
                    steps {
                        withSonarQubeEnv('Sonarqube') {
                            script {
                                def scannerHome = tool 'sonarqube-scanner'
                                // The parameters reach the shell as environment variables, never as Groovy-built shell code.
                                // The name is set too, or the analysis would rename the per-run project after sonar-project.properties.
                                sh """
                                    ${scannerHome}/bin/sonar-scanner \
                                      -Dsonar.python.coverage.reportPaths=coverage.xml \
                                      -Dsonar.python.xunit.reportPath=test-results.xml \
                                      \${SONAR_PROJECT_KEY:+-Dsonar.projectKey=\$SONAR_PROJECT_KEY -Dsonar.projectName=\$SONAR_PROJECT_KEY} \
                                      \${SONAR_PROJECT_VERSION:+-Dsonar.projectVersion=\$SONAR_PROJECT_VERSION}
                                """
                                if (params.SONAR_PROJECT_KEY) {
                                    // Holds the ceTaskId the orchestrator follows; cleanWs() would delete it.
                                    archiveArtifacts artifacts: '.scannerwork/report-task.txt'
                                }
                            }
                        }
                    }
                }

                stage('Quality Gate') {
                    steps {
                        timeout(time: 5, unit: 'MINUTES') {
                            script {
                                def gate = waitForQualityGate abortPipeline: false
                                if (gate.status != 'OK') {
                                    if (params.SONAR_PROJECT_KEY) {
                                        unstable "Quality gate ${gate.status}"
                                    } else {
                                        error "Quality gate ${gate.status}"
                                    }
                                }
                            }
                        }
                    }
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
