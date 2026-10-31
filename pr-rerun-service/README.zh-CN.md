# PR 重跑服务

App 部署支持通过 GitHub 设备授权登录，无需人工分发服务令牌。
详见 [GitHub 登录与仓库规则](docs/multi-repository.zh-CN.md)。

面向 PyPTO 贡献者的独立 GitHub Actions 重跑服务。管理员运行一个服务端，
贡献者登录一次后，用 `pr-rerun 123` 查看并确认重跑**本人开放的 PR**。

[English](README.md) | 中文 | [完整运维说明](docs/README.zh-CN.md)

版本 **0.3.0** 新增 GitHub App 认证和每仓库独立规则：用户授权、工作流、PR 限制、
额度及规则审计。注册、接入、管理员命令和迁移步骤见
[多仓库与 App 指南](docs/multi-repository.zh-CN.md)。原有 `gh`/Token 单仓库部署继续
兼容；多仓库使用需要真实 App 安装授权和 0.3.0 客户端。

## 功能

- 普通 CI 失败只重跑失败任务；配置的 Codex Review 工作流在核实 PR 身份 artifact 后，
  重跑该工作流的全部任务。整体已取消的已结束运行，只有仍含失败或超时任务时才处理。
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
pr-rerun login --token-login --netbird-server http://100.64.0.10:8080
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

## 仓库别名（客户端 0.3.1）

在客户端配置一次即可，无需改服务端或重新登录：

```bash
pr-rerun alias set pypto hw-native-sys/pypto
pr-rerun alias set toolbox pypto-tools/pto-toolbox
pr-rerun 123 --repo pypto
pr-rerun 456 --repo toolbox
pr-rerun alias list
pr-rerun alias remove toolbox
```

别名区分大小写，按 Linux 用户保存在 `~/.config/pr-rerun/aliases.json`，也支持
`XDG_CONFIG_HOME`。只保存完整仓库名，不包含令牌，登录或退出不会删除别名。
`--repo`、`PR_RERUN_REPOSITORY`、登录和规则管理命令都支持别名。
登录保存的是展开后的完整仓库名，后来改别名不会悄悄改变默认仓库。
预览和确认仍显示完整仓库名，服务端授权及本人 PR 校验保持不变。

## 官方仓库别名（服务端和客户端 0.3.2）

仓库管理员可以为自己管理的仓库发布官方别名：

```bash
pr-rerun alias set pypto hw-native-sys/pypto --official
pr-rerun alias list --official
pr-rerun 123 --repo pypto
pr-rerun alias remove pypto --repo hw-native-sys/pypto --official
```

官方别名保存在仓库规则的 `aliases` 列表中，区分大小写，每仓库最多 20 个，且在服务
内全局唯一。修改沿用规则管理的身份校验、版本检查和审计历史，冲突时整次更新回滚，
不会覆盖其他仓库的定义；修改会使已有预览失效。删除时必须指定完整仓库名。
管理员不能重定向其他仓库的别名。查询和解析需要登录，仅展示当前用户有权使用且
已启用的仓库；停用仓库仍保留别名占用，直到管理员主动删除。

解析顺序为：完整 `owner/repo` 名称 → 本地别名 → 当前服务器的官方别名。
本地同名别名优先，可用 `alias list --official` 查看官方定义。终端会显示展开结果，
预览和确认始终显示完整仓库名。官方别名在认证后解析，也支持首次 GitHub 登录时
使用；不会跨服务器缓存。保存的默认仓库仍为展开后的完整名称。
原有本地别名管理命令仍可离线使用。

官方别名需要服务端和客户端都升级。切换服务版本前先停服备份数据库；规则写入新的
`aliases` 字段后，回退到 0.3.0/0.3.1 必须同时恢复对应数据库备份。

## 服务管理员（服务端 0.3.3）

服务管理员可以管理所有已接入仓库的规则、官方别名和仓库管理员列表；仓库管理员
仍只管理自己负责的仓库。服务管理员角色绑定不可变的 GitHub 用户 ID，独立于仓库
规则，修改仓库规则或通过 GitHub 登录都不能自行获得该角色。

服务器数据库的所有者在本机授予角色。目标账号需先通过 GitHub 登录，或已通过
`issue` 校验身份。将 `PR_RERUN_DATABASE` 指向服务数据库，查看 `users` 后使用对应的
数字 ID（以下为示例）：

```bash
pr-rerun-admin users
pr-rerun-admin service-admin grant 123456
pr-rerun-admin service-admin list
pr-rerun-admin service-admin revoke 123456
```

这些命令需要本地私有数据库访问权限，不需要 GitHub 凭据。授予和撤销会记录目标
GitHub ID、执行命令的本机 UID 和时间，服务不提供授予角色的 HTTP 接口。授权后，
管理员可以继续使用已有 GitHub 登录状态，在 0.3.2 或更新的客户端执行：

```bash
pr-rerun alias set pypto hw-native-sys/pypto --official
pr-rerun alias list --official
```

规则修改仍检查别名冲突和版本，并记录实际 GitHub 操作者；在提交规则的事务内会
再次检查角色是否已撤销。服务管理员可以查看已启用仓库的官方别名，但重跑仍需满足
仓库访问规则、本人 PR 校验和明确确认。撤销用户的服务访问也会阻止管理员操作。
要永久取消管理员身份，需显式撤销角色；重新启用同一用户时，否则仍会保留原角色。
