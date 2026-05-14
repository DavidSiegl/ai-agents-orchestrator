pipeline {
    agent any

    environment {
        UV_NO_PROGRESS = '1'
        COVERAGE_FILE  = '.coverage'
    }

    stages {
        stage('Install') {
            steps {
                sh 'uv sync --frozen'
            }
        }

        stage('Test') {
            steps {
                sh 'uv run pytest --cov=. --cov-report=xml:coverage.xml --junitxml=test-results.xml'
            }
            post {
                always {
                    junit 'test-results.xml'
                }
            }
        }

        stage('SonarQube Analysis') {
            steps {
                withSonarQubeEnv('Sonarqube') {
                    script {
                        def scannerHome = tool 'sonarqube-scanner'
                        sh """
                            ${scannerHome}/bin/sonar-scanner \
                              -Dsonar.python.coverage.reportPaths=coverage.xml \
                              -Dsonar.python.xunit.reportPath=test-results.xml
                        """
                    }
                }
            }
        }

        stage('Quality Gate') {
            steps {
                timeout(time: 5, unit: 'MINUTES') {
                    waitForQualityGate abortPipeline: true
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
