#!/usr/bin/env groovy

// ledger-domain-core CI — triggered by the GitHub webhook, tested in a throwaway
// Python container that installs Beancount/beanquery but never a real ledger.
// Server-side steps mirror LedgerKernel's docs/CI.md.

pipeline {
    agent any

    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }

    environment {
        PYTHONUNBUFFERED = '1'
        PIP_DISABLE_PIP_VERSION_CHECK = '1'
    }

    stages {
        stage('Checkout') {
            steps {
                sh '''
                    git log -1 --oneline
                    git status --short
                '''
            }
        }

        stage('Test in Python 3.12') {
            steps {
                sh '''
                    set -eu
                    tar --exclude=.git --exclude=.venv --exclude='__pycache__' -cf - . \
                      | docker run --rm -i python:3.12-slim bash -lc '
                          set -eux
                          mkdir -p /workspace
                          tar -xf - -C /workspace
                          cd /workspace
                          python -m pip install --disable-pip-version-check \
                            "beancount==3.2.3" "beanquery==0.2.0"
                          python -m compileall -q ledger_domain
                          PYTHONPATH=/workspace python -m unittest discover -s tests -v
                          PYTHONPATH=/workspace python -m ledger_domain --root /workspace health
                        '
                '''
            }
        }
    }

    post {
        success {
            echo '✅ ledger-domain-core CI 通过'
        }
        failure {
            echo '❌ ledger-domain-core CI 失败，请查看测试日志'
        }
        always {
            cleanWs()
        }
    }
}
