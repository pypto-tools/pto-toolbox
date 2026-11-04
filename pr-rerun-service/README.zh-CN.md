# PR 重跑服务

独立部署的 GitHub Actions 重跑服务，支持多个仓库。管理员运行一个服务端，
用户通过 GitHub 登录一次后，用 `pr-rerun 123` 预览并确认重跑**本人开放的 PR**。

[English](README.md) | 中文

当前版本 **0.7.1**。推荐 GitHub App 模式：每仓库单独授权、独立规则，
无需人工分发令牌。旧的 `gh`/Token 单仓库模式继续兼容。

| 需要做什么 | 文档 |
| --- | --- |
| 在一台或多台机器安装、更新共享客户端 | [独立客户端发布指南](docs/client-releases.zh-CN.md) |
| 日常登录、选择仓库、确认重跑 | [用户使用](#用户使用) |
| 注册 App、接入仓库、管理规则 | [多仓库与 App 指南](docs/multi-repository.zh-CN.md) |
| 升级服务、检查状态、排查故障 | [运维与排障](docs/operations.zh-CN.md) |
| 旧单仓库部署、归属校验和请求结果语义 | [旧模式及协议说明](docs/README.zh-CN.md) |

## 功能

- 普通 CI 失败只重跑失败任务；配置的 Codex Review 工作流在核实 PR 身份 artifact 后，
  重跑该工作流的全部任务。已结束运行中有失败、超时或取消任务时均可重跑，
  预览分别列出失败和取消任务。
- GitHub 设备登录验证稳定用户 ID，自动签发持久化服务会话；不需要仓库写权限或登录 `gh`。
- App 模式使用限制到目标仓库的临时安装凭据执行重跑；App 私钥和 GitHub Token 不发给客户端。
  旧模式仍使用管理员的 `gh` 登录或专用 GitHub Token。
- 每次重跑都要求预览、手动确认和服务端状态复查。
  没有 `--yes`，也没有合并、删除、取消或 workflow-dispatch 接口。
- 远程监听同时绑定本机 NetBird IP 和网卡；本机客户端使用独立回环入口。
  NetBird 查询失败时退出。


fork CI 的 GitHub PR 关联为空时，会补充核对来源信息和完整分支历史，只有唯一对应
所请求 PR 时才放行。复用或存在歧义的分支仍会拒绝，每次重跑前都会再次检查。

## 服务端安装

服务端需要 Python 3.10+、Linux、OpenSSL、已连接的 NetBird 内核网卡、iproute2 的
`ip` 命令，以及到 GitHub 的 HTTPS 连接。在 `pto-toolbox/pr-rerun-service` 执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock .
source .venv/bin/activate
```

然后按[App 注册及仓库接入流程](docs/multi-repository.zh-CN.md)配置 App、数据库和规则，
使用 `deploy/pr-rerun-app.service` 模板设置 systemd 开机启动。模板中的账户、路径和
App ID 都必须替换；本机配置不要提交 Git。旧 `gh`/Token 部署见
[旧模式说明](docs/README.zh-CN.md)。

NetBird 策略需允许目标节点访问服务 TCP 端口（默认 8080）。服务只监听 NetBird 网卡
及本机回环地址，无需 Docker 或公网入口。直接运行 `pr-rerun-server` 会占用前台，
只有安装并启用 systemd 服务后才会在开机时启动。
其它机器只需安装客户端并连接现有服务，不需要部署新的服务端。

## 全机共享客户端

推荐使用[独立客户端发布指南](docs/client-releases.zh-CN.md)。GitHub Releases 提供
ARM64 和 x86_64 安装包，自带 Python 运行环境，首次安装器兼容系统 Python 3.9 以上。
每台机器安装一次，之后用 `sudo pr-rerun-update` 更新，各用户保留自己的登录和别名。

开发者仍可使用旧 `deploy/install-shared-client.sh` 安装本地构建的 wheel，
该路径要求 `/usr/bin/python3` 为 3.10 以上。独立客户端无需运行这个旧脚本。

## 用户使用

使用自己的普通系统账号，**不加 sudo**。下面是示例地址，需换成管理员提供的
服务端 NetBird IP：

```bash
pr-rerun login --netbird-server http://100.64.0.10:8080 --repo pypto
# 按提示完成 GitHub 设备授权；pypto 是已配置的官方别名。

# 之后包括新开终端，直接指定 PR：
pr-rerun 123
pr-rerun 456 --repo another/project
pr-rerun repos
# 可选：清除当前用户的登录状态
pr-rerun logout
```

`--repo pypto` 设置以后只输入 PR 编号时使用的默认仓库。它支持完整 `owner/repo`
或已配置的别名，不会授予仓库权限。单次重跑用 `--repo` 切换其它已接入仓库，不改变
保存的默认仓库。仓库规则为 `access: "authenticated_users"` 时，无需逐人授权，
但仍只能操作本人 PR；其它规则可能要求单独授权。

检查预览后，在 `Confirm? [y/N]` 输入 `y` 提交，回车取消。没有符合条件的失败或取消任务时
不会要求确认。`accepted` 只表示 GitHub 接受请求，不表示 CI 通过；用 Actions 链接查看
任务运行结果，用 `pr-rerun status rr_REQUEST_ID` 查询服务提交状态。
排查任务为何被排除时使用 `pr-rerun 123 --verbose`。

登录将地址和令牌保存在 `~/.config/pr-rerun/client.json`，或 `XDG_CONFIG_HOME`
指定的目录，目录和文件权限分别为 `0700`、`0600`。登录只验证身份，不触发重跑。
保存的令牌只会用于绑定的服务器地址。同一系统账号只保存一份登录；多人共用同一个
系统账号时，也会共用这份登录信息。


## 通过 MCP 分析 CI（0.7.0）

客户端新增只读 MCP，复用当前用户的登录状态。在 AI 工具中配置
`pr-rerun mcp --repo pypto`，通过 stdio 查询仓库、CI 统计和错误详情。
现有 0.6.0 服务端无需升级，详见 [MCP 配置与权限边界](docs/mcp.zh-CN.md)。

## CI 错误详情（0.6.0）

错误片段需按仓库单独开启，默认只允许维护者查看，与汇总统计权限独立。
需要同时更新服务端和客户端，并配置独立采集任务；已有登录继续有效。

```bash
pr-rerun errors --repo pypto --days 30
pr-rerun errors --repo pypto --days 30 --group
pr-rerun error <错误ID> --repo pypto
```

参见[采集、权限、限制与部署](docs/ci-errors.zh-CN.md)。

## CI 统计（0.5.0）

仓库管理员启用统计并完成后台采集后，使用现有登录查询：

```bash
pr-rerun stats --repo pypto --days 7
pr-rerun stats --repo pypto --days 30 --sort failure-rate
pr-rerun stats --repo pypto --json
```

展示首次 job 的失败次数、失败率、失败 step 和重跑恢复情况，支持事件、head 分支、
workflow 路径及完整 job 名称筛选。查询只读，不触发重跑；取消和跳过不算失败样本。
客户端和服务端均需升级，后台需单独配置定时采集；不需要重新登录或增加 App 权限。
默认关闭、仅管理员查看，可按仓库向有使用权限的用户开放。
[配置、采集、命令和统计口径](docs/ci-statistics.zh-CN.md)。

## 仓库清单查询（0.4.1）

```bash
pr-rerun repos
pr-rerun repos --json
```

列出服务已登记的仓库，包括没有别名的仓库。服务管理员看到全部仓库（包括停用项），
仓库管理员也能看到自己管理的停用仓库；普通用户只看到有使用权限且启用的仓库。
此命令不实时检查 GitHub 授权，不改变本人 PR 限制。需要 App 模式服务端升级到 0.4.1，
仅更新客户端不够；连接旧服务会明确提示升级。

在服务所在机器，加载与运行服务相同的 App 和数据库环境配置后执行：

```bash
pr-rerun-admin installations
pr-rerun-admin installations --json
```

实时对比 App 安装授权与服务登记，区分 `enrolled`（已登记）、`not_enrolled`（未登记）、
`identity_changed`（改名、重装或仓库身份变化），以及仅存在于服务的记录。
暂停和查询失败会明确显示，无法确认的授权标记为 `unknown`，不会误报为已撤销。
查询完整退出码为 0，不完整为 1，配置/API 错误为 2；JSON 中包含 `complete` 字段。
查询以只读方式打开现有数据库，只申请临时元数据只读凭据，不自动登记仓库或重跑任务。
管理员命令仅在服务端提供，不随独立客户端分发。

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

## 查询速度与等待提示（0.3.4）

客户端会立即提示正在检查 PR 和 workflow。从客户端 0.3.6 起，交互终端使用单行
旋转提示和计时，在显示预览或确认输入前自动清除。确认后的重新校验和提交也使用
旋转提示。输出重定向或终端不支持动画时，仅显示开始和结束信息，不重复刷行。
提示输出到 stderr，说明当前请求范围，不代表服务端具体子步骤的实时进度。

服务端每轮计划最多并行检查四个 run，同一轮中的同一源分支只查询一次完整 PR 历史。
预览、确认和每次重跑前的检查之间不复用身份结果。run 选择和排除原因保持原顺序，
任意查询失败都会拒绝整轮计划。实际 rerun 写请求仍逐个提交，不自动重试。服务日志
记录计划查询耗时和 run 数量，不记录凭据。

客户端升级后获得等待提示；服务端升级后启用并行查询。找到符合条件的任务时仍须
手动确认，没有符合条件的任务则直接结束。GitHub 和网络延迟仍影响响应
速度；提交成功只表示 GitHub 接受请求，不会等待 CI 跑完。

## 简短确认（客户端 0.3.5）

客户端显示完整仓库、PR、作者和待重跑任务后，会询问 `Confirm? [y/N]`。
输入 `y` 才提交，直接回车、输入 `n` 或其他内容均取消；大写 `Y` 和两侧空格也接受。
无需复制完整仓库和 PR 身份文字。客户端只在明确确认后发送原预览的确认字段和
凭据，服务端校验保持不变。这次仅需更新客户端，不需要重启服务端。

## 简洁预览（客户端 0.3.6）

默认隐藏 `Excluded` 排除明细，只展示 PR 信息和待重跑任务；没有符合条件的任务时
会简短说明不会提交。需要排查原因时使用 `pr-rerun 123 --verbose`，也可以配合
`--repo`。这只调整客户端显示，归属检查、workflow 选择和确认保持不变。
升级客户端即可，不需要重启服务端。

## GitHub 登录连通性（服务端 0.3.7）

设备登录需要服务主机能访问 `github.com/login/device/code`、
`github.com/login/oauth/access_token` 和 `api.github.com/user`。
仓库 API 可用不代表登录网站也可达。客户端已经通过 NetBird 连上服务后，申请
授权码或等待授权时报错，仍可能是服务主机到 GitHub 的连接问题。

如果直连登录接口失败，服务管理员可以在服务端环境中显式设置可信 HTTP(S) 代理
`PR_RERUN_LOGIN_PROXY`，例如 `http://127.0.0.1:7890`，然后重启服务。
代理地址含凭据时使用受保护的 systemd EnvironmentFile。代理必须能被服务进程
访问。此设置仅作用于设备授权和身份查询；重跑请求及客户端到服务的连接路线不变。
通用 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 环境变量仍不自动读取，不跟随重定向，
也不关闭 TLS 校验。此设置不支持 SOCKS 代理。

登录错误日志会记录操作阶段及 HTTP 状态码或异常类型，不记录授权码、令牌、代理
凭据或响应正文。服务关闭 HTTP 库的普通请求日志，避免签名 artifact 下载链接
进入日志。现有客户端无需重装或清除登录状态即可使用此次服务端更新。
