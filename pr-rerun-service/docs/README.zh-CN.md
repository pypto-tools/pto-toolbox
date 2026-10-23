# PR 重跑服务

独立部署的后台服务：用户预览并确认**自己创建且仍开放的 PR**，服务使用管理员的
GitHub Token 发起重跑。用户只拿到独立的服务访问令牌。
运行它不需要 PyPTO 源码、Python 包或编译器。只有选择复用 GitHub CLI
登录时，服务端才需要 `gh`。

[English documentation](README.md)

版本 `0.2.3` 支持整体已取消、但仍含失败或超时任务的已结束工作流；只有取消任务时
仍不重跑。每次重跑前会再次核对失败任务。升级后需要重启服务端；原有客户端登录和
服务令牌继续有效，客户端无需升级。`0.2.2` 引入的普通 fork 运行归属补充校验保留。

## 使用流程

1. 用户输入 PR 编号，后台核对访问令牌绑定的 GitHub 用户 ID 与 PR 作者 ID。
2. 展示仓库、标题、作者、链接、提交、目标分支、失败任务和重跑范围。
3. 用户手动输入 `hw-native-sys/pypto#123 @alice`。
4. 后台重新检查 PR 和运行状态，调用对应的 GitHub 接口。

普通 CI/Docs 失败或超时执行 `rerun failed`；配置中的 Codex Review 工作流含失败
或超时任务时执行该工作流的 `rerun all`。整体已取消的已结束运行，只要当前尝试仍有
失败或超时任务也可处理；只有取消任务时不重跑。成功、跳过、等待批准、正在运行以及
超过 30 天的记录不处理，每次重跑前会再次核对失败任务。同一工作流只考虑已验证的
最新运行，即使它没有可重跑的失败任务，也不会退回旧记录。草稿 PR 的 Codex Review
不处理。

CLI 必须在交互终端运行，没有 `--yes` 参数。不过服务端不能证明用户确实阅读了
预览，API 调用者可以自动完成两步协议；身份、状态和防重放校验始终在服务端执行。

## 安装与启动

需要 Python 3.10+。NetBird 服务端需要 Linux、已连接的 NetBird 客户端和
`ip` 命令（iproute2），支持内核网卡模式。macOS 可使用 CLI 或本机测试模式。
使用 SQLite 和 POSIX 文件锁，
每份数据库只运行一个服务进程；重复启动会被拒绝。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -c requirements-dev.lock -e ".[dev]"

export PR_RERUN_DATABASE="$PWD/state/rerun.sqlite"
export PR_RERUN_CONFIG="$PWD/config.example.json"
read -rsp "GitHub token: " PR_RERUN_GITHUB_TOKEN
export PR_RERUN_GITHUB_TOKEN

pr-rerun-admin issue alice
pr-rerun-server
```

默认查询本机 NetBird IPv4，核对其所属网卡，绑定该 IP 和网卡接收远程请求。
同时监听 `127.0.0.1:8080` 供本机客户端使用。不会监听公网或普通局域网地址，
也不会创建 IPv6 通配监听。NetBird 查询失败、网卡不存在/未启用、网卡绑定失败
或端口占用时退出，不回退到其他地址。仅需本机测试时使用 `--local-only`。

### 复用已有的 gh 登录

如果运行服务的系统用户已经通过 `gh auth login` 登录 GitHub，可以直接使用：

```bash
unset PR_RERUN_GITHUB_TOKEN PR_RERUN_GITHUB_TOKEN_FILE
pr-rerun-server --github-auth gh --host 100.64.0.10 --port 8080
```

将示例 IP 换成服务器的 NetBird IP，或省略 `--host` 自动获取。
无需手动输入或复制 GitHub Token。尚未登录时，先在该系统用户下执行
`gh auth login --hostname github.com`。

发放服务访问令牌时也可复用登录：

```bash
pr-rerun-admin issue alice --github-auth gh
```

也可以设置 `export PR_RERUN_GITHUB_AUTH=gh`，后续服务端和 `issue` 命令
都使用该来源。默认选择 `github.com` 当前活动账号；多账号时可设置
`PR_RERUN_GITHUB_USER` 固定用户名。

程序通过固定的 `gh auth token --hostname github.com` 命令捕获凭据，
不会打印、写入服务数据库或返回给客户端。忽略 `GH_TOKEN`、`GITHUB_TOKEN`
等环境覆盖，确保读取的是已保存的登录。启动失败或未登录时直接报错，
不会自动登录、切换账号或回退到其他凭据。服务启动后保留该凭据；
切换账号、刷新登录或撤销凭据后需要重启服务。

长期后台运行时，必须使用同一个系统用户，并能访问该用户的 gh 配置和
凭据存储。若将服务改为其他用户运行，需要为那个用户配置登录或使用专用 Token。
GitHub 登录账号仍需具备目标仓库的重跑权限；普通用户仍只获得独立服务令牌，
仅能重跑本人 PR，并需要手动确认。

参考：[GitHub CLI 的 auth token 命令](https://cli.github.com/manual/gh_auth_token)。

### 通过 systemd 开机启动

`deploy/pr-rerun.service` 是通用模板，示例用户为 `rerun`，HOME 为
`/home/rerun`，项目目录为 `/opt/pr-rerun-service`，数据库为
`/var/lib/pr-rerun/rerun.sqlite`。这些路径和账号都是占位配置，不代表实际部署。
可改成已有系统用户，但该用户必须已保存具有仓库重跑权限的 `gh` 登录，
服务需使用相同 HOME 和凭据存储。确保状态目录由该用户所有且权限为 `0700`，
`ExecStart` 指定的 Python 环境已经安装。

先创建 Git 忽略的本机配置副本：

```bash
cp deploy/pr-rerun.service deploy/pr-rerun.local.service
```

在副本中调整 `User`、`Group`、`HOME`、所有项目和状态路径，以及
`RequiresMountsFor`。不要在其中填写凭据。省略 `--host` 即可自动查询本机
NetBird 地址。多 GitHub 账号时，可添加
`Environment=PR_RERUN_GITHUB_USER=YOUR_GITHUB_LOGIN` 固定使用的账号。
安装前自行停止已有的手动服务实例；安装脚本不会向任意 PID 发送信号。

检查并安装已经审阅的配置：

```bash
bash deploy/install-systemd.sh --dry-run deploy/pr-rerun.local.service
sudo bash deploy/install-systemd.sh deploy/pr-rerun.local.service
```

脚本先验证服务文件，若系统已有不同配置则拒绝覆盖；更新配置需管理员审阅。
服务开机自动启动，失败后每隔 15 秒重试，NetBird 尚未就绪时也会重试，
不会回退到公网监听。

```bash
systemctl status pr-rerun.service --no-pager
systemctl is-enabled pr-rerun.service
sudo journalctl -u pr-rerun.service -f
sudo systemctl restart pr-rerun.service
curl --noproxy '*' http://127.0.0.1:8080/healthz
```

`sudo systemctl stop pr-rerun.service` 停止服务但保留开机启动；
`sudo systemctl disable --now pr-rerun.service` 同时停止并禁用开机启动。
重启和升级时保留配置的数据库。

### 使用专用 Token

也可使用只授权目标仓库的专用 GitHub Token，权限为 **Actions: write**、
**Checks: read** 和 **Pull requests: read**。组织策略可能要求管理员批准。
不要为了绕过组织限制而改用权限更大的个人 Token。
也可以使用预先签发的 GitHub App 安装令牌，但本程序不负责自动生成或续期。

后台长期运行时，可以设置 `PR_RERUN_GITHUB_TOKEN_FILE` 指向受保护的令牌文件，
与 `PR_RERUN_GITHUB_TOKEN` 二选一。运行服务的用户需要有读取权限；
更换 GitHub Token 后重启服务。专用 Token 和 `gh` 来源互斥，
通过 `PR_RERUN_GITHUB_AUTH=token` 或 `--github-auth token` 显式选择前者。
通过 systemd 等进程管理器维持后台运行。

服务忽略系统代理及 netrc 配置，需要主机可以直接通过 HTTPS 访问 GitHub
和 GitHub 的 artifact 存储。使用经过审查的独立项目副本运行服务，
不要使用 PR 中的代码启动服务或挂载贡献者工作区。
数据库放在本地持久化磁盘，不使用网络文件系统。
`/healthz` 仅表示进程可用，凭据是否有效在实际查询时验证。

## 发放、轮换和撤销访问令牌

```bash
pr-rerun-admin issue alice
pr-rerun-admin users
pr-rerun-admin revoke 12345678
```

`issue` 先从 GitHub 获取用户的稳定 ID，然后生成随机令牌。只输出一次明文，
请通过可信渠道交付。再次对同一用户执行 `issue` 会撤销旧令牌及其待确认预览。
`revoke` 的参数是 `users` 中展示的 GitHub 数字 ID。
用户名改变不会改变权限归属。

`users` 和 `revoke` 只需要数据库配置；`issue` 还需要 GitHub 凭据。
数据库只存储随机 256 位令牌的 SHA-256 哈希，数据库及其目录应仅供管理员访问。
升级或重启保留数据库，以保留操作记录和防重跑记录。

## 同一机器上的共享客户端

管理员可以在一台 Linux 机器上安装一份客户端，让该机器的系统用户共用。
需要系统 `/usr/bin/python3` 为 Python 3.10+，并支持 `venv` 和 `pip`。
在 `pto-toolbox/pr-rerun-service` 中先构建安装包，再安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements-dev.lock ".[dev]"
.venv/bin/python -m build --wheel
bash deploy/install-shared-client.sh --dry-run
sudo bash deploy/install-shared-client.sh
```

脚本使用系统 Python 建立 `/opt/pr-rerun-client`，在
`/usr/local/bin/pr-rerun` 安装公共启动命令。文件由 root 所有，
普通用户可以运行但不能修改。运行时不依赖管理员的个人 conda 环境；
服务端和现有数据库无需重启或迁移。

若提供了 `dist/shared-client-wheelhouse/`，脚本使用其中的依赖离线安装；
否则从 PyPI 安装 `requirements.lock` 约束的依赖。离线依赖必须匹配目标机器的
架构和 Python 版本，生成的安装包和依赖压缩包不进入仓库。
脚本不会覆盖已存在的公共命令或安装目录，适用于首次共享安装。

安装只需管理员做一次，随后每位用户用**自己的普通系统账号，不加 sudo**执行：

```bash
pr-rerun login --netbird-server http://100.64.0.10:8080
pr-rerun 123
```

公共程序不保存共享令牌，登录信息仍保存在各自 HOME/XDG 配置目录。
同一系统账号只保存一份登录。若 PATH 中有旧的个人客户端，可用
`type -a pr-rerun` 检查，并用 `/usr/local/bin/pr-rerun` 明确选择公共版本。
`/usr/local/bin` 需要在用户的 PATH 中；公共安装只对当前机器生效，
其他 NetBird 机器仍需各安装一次。

## 用户客户端

用户在 Linux/macOS 上安装本项目后，首次登录一次：

```bash
pr-rerun login --server https://rerun.example.com
# 按提示输入管理员给自己的服务访问令牌。

# 以后包括新开终端，直接指定 PR：
pr-rerun 123

pr-rerun status rr_0123456789abcdef0123456789abcdef
```

NetBird 用户使用 `pr-rerun login --netbird-server http://100.64.0.10:8080`；
在服务端本机可以直接 `pr-rerun login`，默认使用 `http://127.0.0.1:8080`。
客户端使用已有 API 进行只读认证检查，成功后才保存配置；登录不查询 GitHub、
不创建预览、不触发重跑，服务端无需为此升级或重启。

服务器地址、NetBird 连接方式和服务令牌保存在当前系统用户的
`~/.config/pr-rerun/client.json`；若设置了绝对路径的 `XDG_CONFIG_HOME`，
则使用 `$XDG_CONFIG_HOME/pr-rerun/client.json`。目录权限为 `0700`，
文件权限为 `0600`，只允许该系统用户访问。令牌保存在受权限保护的本地文件中，
不是加密存储；不要分享或提交此文件。每个人应使用独立的系统账号或 HOME 目录。
客户端拒绝权限过宽、非本人所有以及符号链接等不安全的配置文件，并原子更新配置。

```bash
# 令牌被轮换/撤销后，输入管理员新发的令牌：
pr-rerun login

# 删除本机保存的登录信息：
pr-rerun logout
```

登录失败会保留之前的配置。退出登录不撤销服务端令牌，也不影响其他机器。
每次重跑仍必须核对 PR 并手动确认；保存登录不会跳过本人 PR 校验。

服务器选择顺序为命令行参数、环境变量、已保存地址、本机默认地址。
令牌优先使用 `PR_RERUN_ACCESS_TOKEN`；未设置时，仅在目标地址与保存的服务
具有相同协议、主机和端口时复用已保存令牌，否则重新提示输入。
临时更换目标地址不会自动发送原服务器的令牌，也不会修改保存的登录。
`login` 使用环境变量中的令牌或提示输入新令牌；旧环境变量覆盖配置时，
可执行 `unset PR_RERUN_ACCESS_TOKEN PR_RERUN_URL PR_RERUN_NETBIRD_URL` 清除。
`logout` 不会清除父终端的环境变量。

不保存登录也可以直接运行 `pr-rerun`，按提示输入**服务访问令牌**。
不要把管理员的 `PR_RERUN_GITHUB_TOKEN` 发给其他用户。
普通远程访问要求 HTTPS；回环地址允许 HTTP。经明确配置的 NetBird 连接
也允许 HTTP，见下节。客户端拒绝重定向，并转义标题中的终端控制字符，
保留中文等正常文本。

## 通过 NetBird 从其他服务器使用

服务端和客户端所在机器都加入 NetBird 网络后，服务只通过服务端的
NetBird 网卡接收远程请求。NetBird 为节点之间的流量提供 WireGuard 加密，因此在已验证的
NetBird 路径上可以使用 HTTP，无需额外部署 HTTPS 入口或 Docker。
此方案指访问**服务所在节点自己的 NetBird IP**。

在服务端查询本机地址：

```bash
netbird status --ipv4
```

保留前面的数据库、配置和 GitHub Token 环境变量，启动服务。下面的
`100.64.0.10` 是示例，请替换为刚查到的实际地址：

```bash
pr-rerun-server --host 100.64.0.10 --port 8080
```

`--host` 是可选的地址核对项，必须与本机 NetBird 返回的 IPv4 完全一致。
程序拒绝 `0.0.0.0`、`::`、公网 IP、普通局域网 IP、其他节点 IP 和主机名。
不填时自动获取。本版远程监听使用 Linux `SO_BINDTODEVICE` 同时绑定 NetBird
网卡，其他网卡收到的连接不交给服务；仅绑定 IP 不是唯一的保护措施。

在 NetBird 中允许需要使用的节点组访问该服务节点的 **TCP 8080**，
并确保主机防火墙允许对应流量。无需开放公网入站端口。
无需修改全局防火墙来实现网卡绑定。不要额外配置公网反向代理或端口转发。
NetBird 内哪些节点能访问，由 NetBird 访问策略决定；应用令牌继续限制操作身份。

在另一台已加入 NetBird 的服务器上安装本项目，然后配置一次地址：

```bash
# 检查网络和服务进程；这里不会触发 GitHub 重跑。
curl --noproxy '*' http://100.64.0.10:8080/healthz

# 首次验证并保存服务地址和个人服务令牌：
pr-rerun login --netbird-server http://100.64.0.10:8080

# 后续只需：
pr-rerun 123
```

也可只为一次调用指定地址：

```bash
pr-rerun --netbird-server http://100.64.0.10:8080 123
```

**服务所在机器的客户端推荐使用回环地址**，无需配置 NetBird 目标地址：

```bash
pr-rerun --server http://127.0.0.1:8080 123
```

也可以 `pr-rerun login --server http://127.0.0.1:8080` 保存本机入口。
回环入口同样需要用户令牌、本人 PR 和手动确认。

`PR_RERUN_NETBIRD_URL` / `--netbird-server` 表示管理员或使用者已确认该
目标通过 NetBird 访问。程序不会自动验证 VPN 路由；不能仅凭地址位于内网
就认为流量已加密。该选项只接受 NetBird 常用的 `100.64.0.0/10`、RFC 1918
私有 IPv4 或 ULA IPv6 字面地址，不接受主机名、公网 IP、URL 凭据、路径或查询参数。
若在 NetBird 内使用 HTTPS 入口和主机名，可使用 `PR_RERUN_URL=https://...`；
该入口也必须只允许 NetBird 访问。

不要同时设置 `PR_RERUN_URL` 和 `PR_RERUN_NETBIRD_URL`；显式命令行地址
优先于环境变量。不启用 NetBird 选项时，远程 HTTP 仍被拒绝。
NetBird 负责网络连通与加密，服务访问令牌仍用于识别用户并限制为本人 PR；
预览、手动确认、状态复查和审计规则不变。

参考：[NetBird 工作原理](https://docs.netbird.io/about-netbird/how-netbird-works)、
[节点组和访问策略](https://docs.netbird.io/manage/access-control/manage-network-access)。

## HTTP 接口与确认

请求使用 `Authorization: Bearer <服务令牌>` 和 `application/json`。
请求体最多 4096 字节，不接受额外字段或隐式类型转换。

| 方法 | 接口 | 内容 |
| --- | --- | --- |
| POST | `/api/reruns/preview` | `{"pr":123}`，返回预览 |
| POST | `/api/reruns/confirm` | 提交 `ticket` 和完整 `confirmation` |
| GET | `/api/reruns/{request_id}` | 查询本人请求 |

没有可重跑任务时返回 `eligible: false`，不签发 ticket。
确认凭据有效期为五分钟，绑定用户、完整提交和确定的 run ID / attempt。
同一用户的新预览会替换之前未确认的预览。重复确认已消费的 ticket 只返回
原请求状态，不再次调用重跑接口。重新发放访问令牌会使旧凭据失效。

请求 ID 在预览时已经生成，确认响应丢失后仍可查询。
状态响应不会暴露 ticket 或访问令牌。客户端不能指定仓库、run ID、
重跑模式、HTTP 方法、Git ref、调试参数、任意 URL 或 workflow 输入。
服务没有 cookie 会话、CORS、shell 执行、workflow dispatch、批准、评论、
取消、删除或合并接口。

## 结果、并发和故障恢复

| 状态 | 含义 |
| --- | --- |
| `accepted` | GitHub 已接受请求，不代表任务已通过 |
| `rejected` | 校验失败，或 GitHub 明确拒绝 |
| `partial` | 一部分接受，另一部分拒绝 |
| `unknown` | 可能已经提交，但没有收到可靠响应 |
| `not_attempted` | 因前面的操作失败，该操作没有发送 |
| `interrupted` | 服务在处理中重启，需要查看逐项状态 |
| `rerun_observed` | 查询发现了新的 attempt，无法断言由谁发起 |

发送 POST 前，SQLite 先登记 `(仓库 ID, run ID, attempt)` 唯一记录。
重复点击、并发请求和服务重启不会重复提交已占用的 attempt。
确定没有发送或明确拒绝的操作会释放占用；超时等结果未知的操作保留占用，
**不会自动重试 POST**。状态查询只检查 GitHub，不触发重跑。

未知结果一直没有变化时，需要管理员人工调查。本版不提供强制重试或清除占用
接口；维护者可以核实后直接在 GitHub 操作。不要删除数据库绕过保护。

默认每个用户五秒一次预览/状态查询，每个用户和 PR 六十秒一次确认。
重复确认已经消费的 ticket 只读已有结果。HTTPS、连接限制和未认证流量限制
放在反向代理上。JSON 审计日志记录用户 ID、请求 ID、run ID、模式和结果，
数据库将它们关联到 PR。没有自动清理历史记录的任务，请监控磁盘使用量。

## PR 归属验证及限制

普通 CI 优先使用明确的 PR 关联信息，核对编号、来源仓库、提交、目标仓库和目标分支。
GitHub 对 fork 运行可能返回空关联；仅在这种情况下，服务额外核对 `pull_request`
事件、来源仓库 ID、来源分支和当前 SHA，再通过分页 GraphQL 查询该分支的
**开放、关闭、已合并全部 PR**。该来源仓库的分支必须只对应一条 PR，且编号、
目标仓库/分支和 SHA 与请求一致，PR 创建时间不能晚于运行创建时间。
查询必须完整读取所有分页，每次 POST 前也会重新检查。

复用分支或同一分支对应多条 PR 时仍拒绝放行，即使另一条 PR 已关闭，或它的 SHA、
目标分支不同。有明确关联指向其他 PR 时不使用补充校验；仅凭 SHA 或分支名不放行。
Codex 工作流仍需要可信身份 artifact。

Codex 规则读取 PyPTO 现有受信任 `pull_request_target` 工作流生成的
`codex-target-{run}-{attempt}` artifact；兼容旧的 `codex-context` artifact。
选择最新且未过期的记录，核对仓库、PR、head SHA 和 base ref。
ZIP 大小和成员经过限制，不解压到磁盘，下载重定向不会携带 GitHub Token。
服务只依赖这个 artifact 格式，不导入 PyPTO 模块。
若 prepare 在上传身份 artifact 前就失败，可能无法证明归属，此时拒绝重跑。

每次 POST 前都会再检查，但 GitHub 没有原子的“校验提交并重跑”接口，
最后一瞬间仍可能发生 push。服务始终发送已确认的 run ID，不替换成新运行。
GitHub 保留原事件的 SHA/ref，但工作流内部若重新获取最新 PR 数据
（例如 Codex 上下文采集），仍可能看到后续变化。

GitHub 只有 `Actions: write`，没有 rerun-only Token 权限。服务约束的是
普通用户可以请求的动作；服务器和底层 GitHub 凭据依然是信任边界。
原工作流自己的评论、批准等行为会随重跑正常执行。发起 rerun 的账户是
PAT 所属账户，工作流执行沿用最初触发者权限；实际申请人由本服务日志记录。

## 可选容器内测试

以下命令需要 Docker 和 Compose v2 插件。容器配置使用 `--local-only`，
仅供容器内测试，不发布任何宿主机端口。NetBird 服务请按前文直接在 Linux 主机运行。

```bash
export PR_RERUN_TOKEN_FILE=/absolute/path/to/protected/github-token
docker compose -f deploy/compose.yml up -d --build
docker compose -f deploy/compose.yml exec rerun pr-rerun-admin issue alice
docker compose -f deploy/compose.yml exec rerun pr-rerun 123
docker compose -f deploy/compose.yml logs -f rerun
```

容器以 UID 10001 运行。确保这个 UID 可以读取挂载的令牌文件；
Compose 基于文件的 secret 可能保留宿主机文件的权限和属主，
不要假定容器配置会覆盖这些属性。请相应限制宿主机访问。
数据库持久化到命名卷。容器不暴露到宿主机、公网或 NetBird 网络。
容器除数据库目录和临时目录以外使用只读文件系统。

## 测试与上线验收

```bash
pip install -c requirements-dev.lock -e ".[dev]"
pytest
ruff check src tests
ruff format --check src tests
pyright
python -m build
```

测试模拟 GitHub 响应，验证真实 HTTP 接口、SQLite 事务、CLI 确认、
分页、artifact 下载、归属、令牌撤销、并发、重启和超时，不使用真实凭据或触发重跑。
`requirements.lock` 固定已验证的运行依赖，`requirements-dev.lock` 还固定开发工具；
容器使用运行依赖锁。升级依赖时应更新这些文件并重新运行测试。
上线前用配置好的 PAT 和一个受控的本人 PR 做实际验收，覆盖 fork PR，
确认工作流路径、权限和 artifact 格式；自动测试不能替代环境相关验收。

参考：

- [GitHub 重跑接口](https://docs.github.com/en/rest/actions/workflow-runs)
- [GitHub 重跑语义与 30 天限制](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)
- [GitHub Checks API 的 fork PR 限制](https://docs.github.com/en/rest/checks/runs)
