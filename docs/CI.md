# CI（Jenkins）

本仓库由内部 Jenkins 的 Multibranch Pipeline 通过 GitHub webhook 构建，配置参考 `LedgerKernel`。

## 作业

| 项 | 值 |
|---|---|
| Jenkins job | `ledger-domain-core` |
| 触发 | GitHub `push` / `pull_request` |
| Webhook | `https://jenkins.dhr2333.cn/github-webhook/` |
| Script Path | `Jenkinsfile` |
| 构建环境 | `python:3.12-slim` 一次性容器 |

## 构建做什么

1. checkout 当前 commit；
2. 把工作区 tar 流送进 Python 3.12 容器；
3. 安装 `beancount==3.2.3` 与 `beanquery==0.2.0`；
4. `python -m compileall -q ledger_domain`；
5. `python -m unittest discover -s tests -v`（20 项，全部在临时账本副本上运行）；
6. `python -m ledger_domain --root /workspace health` 冒烟。

不访问真实账本，不写用户目录，构建产物只有日志。

## 本地等价命令

```bash
python -m pip install "beancount==3.2.3" "beanquery==0.2.0"
python -m compileall -q ledger_domain
PYTHONPATH=. python -m unittest discover -s tests -v
PYTHONPATH=. python -m ledger_domain --root . health
```

## 凭据

GitHub Branch Source 使用 Jenkins 已有的 GitHub 凭据（与 `LedgerKernel` 相同）；仓库本身不保存任何 token。若仓库改为私有，需确认该凭据具备读取私有仓库的 `repo` 权限。

## 分支保护（可选）

构建成功后，Jenkins Git 插件回写的 commit status context 为 `continuous-integration/jenkins/branch`；如需强制，可在 GitHub 分支保护中要求该检查。
