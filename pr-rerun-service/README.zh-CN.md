# PR 重跑服务

面向 PyPTO 贡献者的独立 GitHub Actions 重跑服务。管理员运行一个服务端，
贡献者登录一次后，用 `pr-rerun 123` 查看并确认重跑**本人开放的 PR**。

[English](README.md) | 中文 | [完整运维说明](docs/README.zh-CN.md)

## 功能

- 普通 CI 失败只重跑失败任务；配置的 Codex Review 工作流在核实 PR 身份 artifact 后，
  重跑该工作流的全部任务。
- 每人一个绑定 GitHub 稳定用户 ID 的服务令牌，不需要仓库写权限或登录 `gh`。
- 服务端使用管理员已有的 `gh` 登录，或专用 GitHub Token。
- 每次重跑都要求预览、输入完整确认文字和服务端状态复查。
  没有 `--yes`，也没有合并、删除、取消或 workflow-dispatch 接口。
- 远程监听同时绑定本机 NetBird IP 和网卡；本机客户端使用独立回环入口。
  NetBird 查询失败时退出。


fork CI 的 GitHub PR 关联为空时，会补充核对来源信息和完整分支历史，只有唯一对应
所请求 PR 时才放行。复用或存在歧义的分支仍会拒绝，每次重跑前都会再次检查。

## 服务端安装

需要 Python 3.10+、Linux、已连接的 NetBird 内核网卡、iproute2 的 `ip` 命令，
并能通过 HTTPS 直连 GitHub。macOS 可使用客户端或本机测试模式。
在 `pto-toolbox/pr-rerun-service` 目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -c requirements.lock .

export PR_RERUN_DATABASE="$HOME/.local/share/pr-rerun/rerun.sqlite"
export PR_RERUN_CONFIG="$PWD/config.example.json"

# 使用以后运行服务的同一个系统用户登录。
gh auth login --hostname github.com
pr-rerun-admin issue alice --github-auth gh
pr-rerun-server --github-auth gh --port 8080
```

把 `alice` 换成贡献者的 GitHub 用户名，将输出的 `access_token` 私密交付。
对同一用户再次签发会使旧令牌失效。执行管理员命令时保留数据库和配置环境变量。
服务端自动查询本机 NetBird IP；在 NetBird 策略中允许需要使用的节点访问 TCP 8080。
部署无需 Docker，可选容器方案仅用于本机测试。

开机启动时，将 `deploy/pr-rerun.service` 复制为
`deploy/pr-rerun.local.service`，调整系统用户、HOME、项目和数据库路径，
再按 [systemd 说明](docs/README.zh-CN.md#通过-systemd-开机启动)安装。
本机服务文件由 Git 忽略。

## 全机共享客户端

每台客户端机器可以由管理员安装一份 root 所有的程序，供该机器所有系统用户使用。
先在目标机器从源码构建安装包：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements-dev.lock ".[dev]"
.venv/bin/python -m build --wheel

bash deploy/install-shared-client.sh --dry-run
sudo bash deploy/install-shared-client.sh
```

安装脚本使用系统 Python，在 `/opt/pr-rerun-client` 建立运行环境，
将公共命令放到 `/usr/local/bin/pr-rerun`，拒绝覆盖已有安装。
可将匹配目标 Python 和架构的离线依赖放在 `dist/shared-client-wheelhouse/`；
否则按 `requirements.lock` 约束从 PyPI 安装。生成的离线包不提交到仓库。

## 用户使用

使用自己的普通系统账号，**不加 sudo**。下面是示例地址，需换成管理员提供的
服务端 NetBird IP：

```bash
pr-rerun login --netbird-server http://100.64.0.10:8080
# 按隐藏输入提示粘贴个人服务令牌。

# 之后包括新开终端，直接指定 PR：
pr-rerun 123
pr-rerun logout
```

登录将地址和令牌保存在 `~/.config/pr-rerun/client.json`，或 `XDG_CONFIG_HOME`
指定的目录，目录和文件权限分别为 `0700`、`0600`。登录只验证身份，不触发重跑。
保存的令牌只会用于绑定的服务器地址。同一系统账号只保存一份登录；多人共用同一个
系统账号时，也会共用这份登录信息。

## 安全与发布

凭据、数据库、日志、本机服务文件、虚拟环境和构建压缩包都不进入版本控制。
示例使用虚构用户和占位 VPN 地址，仓库不提供实际部署地址。
公开内容包括源码、测试、依赖锁和部署模板。
服务端主机和管理员的 GitHub 凭据仍是信任边界，GitHub 没有 rerun-only Token 权限。
归属验证限制、未知结果恢复和凭据保存方式见[运维说明](docs/README.zh-CN.md)。

## 开发与测试

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -c requirements-dev.lock -e ".[dev]"
python -m pytest tests
ruff check src tests
ruff format --check src tests
pyright
python -m build
```

测试模拟 GitHub，不执行真实重跑。Linux socket 测试需要具备将 socket 绑定到网卡
（包括回环网卡）的权限。
