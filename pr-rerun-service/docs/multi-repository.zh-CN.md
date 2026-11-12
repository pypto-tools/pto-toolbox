# 多仓库规则与 GitHub App 部署

[English](multi-repository.md)

0.3.0 增加独立的 App 模式。现有 `gh`/Token 模式继续使用单仓库配置，原数据库和
登录兼容。App 模式需要 Linux、用于 RSA 签名的 OpenSSL，以及访问 GitHub 的网络；
不需要公网入口、OAuth 回调或 Webhook，客户端仍通过 NetBird 连接。

## 权限与规则设计

平台管理员保管 App 私钥、首次接入仓库并指定仓库管理员。组织所有者安装 App 时
选择 **Only select repositories**，显式授权仓库；服务会拒绝 **All repositories**
安装。安装 App 不等于自动接入服务，也不会自动授权所有服务用户。
平台管理员需确认仓库所有者同意授权及初始管理员名单。平台管理员是全局信任角色；
远程仓库管理员只能管理自己被指定的仓库。

接入记录固定仓库完整名称、GitHub 仓库 ID 和 App installation ID。每个仓库单独配置：

- 启用状态、管理员和使用者的 GitHub 数字 ID。
- 是否允许草稿 PR、目标分支 glob 模式。
- 允许的工作流路径、归属验证策略和重跑方式。
- 冷却时间、每用户每日提交上限、每 PR 活跃提交上限。

管理员也只能重跑自己创建的开放 PR。可以向已签发服务身份的用户授予本仓库的使用
或管理权限，不能通过规则越权访问其他仓库。用户改名不改变数字 ID。
规则是严格校验的 JSON 数据，不允许脚本、命令、任意 API 地址或凭据；PR 分支里的
文件不能修改服务规则。接入和更新时会查询 GitHub，确认工作流存在且处于 active 状态。

普通工作流使用 `pull_request` 归属验证和 `failed` 模式。`all` 模式只允许与
`codex_manifest` 一起使用，仓库需要已经部署可信的 `pull_request_target` 工作流，
按运维说明生成身份 artifact。仅凭任务名称不能证明归属；没有受支持归属验证方式的
工作流暂不支持，不能仅配置成 CI/Codex 名称就放行。

预览绑定仓库、PR、提交、运行尝试、重跑方式和规则版本。确认时根据已保存的 ticket
选择仓库，客户端不能替换。规则更新使旧预览失效，即使随后恢复成相同内容也要重新
预览。每次重跑前再次验证规则、用户身份和 App 安装授权；缓存的 Token 不替代授权
检查。Token 请求限制到一个仓库及所需权限，并在过期前自动刷新。

移除仓库授权、暂停/卸载 App、安装 ID 改变、仓库被替换或转移都会拒绝执行。
已被 GitHub 接受的请求无法因同时发生的撤销而追回。重新安装、改名或转移需要平台
管理员制定迁移步骤，程序不会自动绑定到新的安装或同名替代仓库。

规则更新要求提交当前版本号，避免覆盖他人的更新。SQLite 保存完整历史、操作者 ID
及时间；首次本地接入记为平台操作者 `0`。HTTP 客户端不能替换接入记录或 App 私钥。

冷却时间分别按仓库/用户、仓库/PR 计算。每日限额按 UTC 日统计已预留的提交批次，
包含后续被上游拒绝的尝试；同一请求的幂等重试不重复扣次数。活跃上限统计正在验证、
提交或结果未知的请求，不等于限制 GitHub 中所有运行中的 CI。未知结果需先用
`status` 核查，再决定下一步；原有运行尝试防重复机制继续生效。

### PyPTO 评论触发的 Full CI（服务端 0.8.1）

运行 PyPTO 可信 `ci_control.py` 协议的仓库，可以给下面两个路径显式启用
`pypto_ci`。将现有规则中的 `workflows` 数组改成下面内容，保留其他字段，提交时
使用当前 `expected_revision`。

```json
[
  {"path": ".github/workflows/ci.yml", "mode": "failed", "identity": "pypto_ci"},
  {"path": ".github/workflows/ci-full.yml", "mode": "failed", "identity": "pypto_ci"},
  {"path": ".github/workflows/codex-review.yml", "mode": "all", "identity": "codex_manifest"}
]
```

`ci.yml` 中自动触发的 PR lint 继续按普通 PR 校验。Full CI 在可信 `main` 上通过
`workflow_dispatch` 运行，标题为 `CI full PR #NUMBER @HEAD base:BASE`，控制器在
测试前验证不可变的 PR head/base/merge 输入。服务查询授权 workflow 的近期 dispatch
记录，独立于镜像 job checks 和 `ci/full` 状态，核对仓库和 workflow ID、事件、源仓库、
`main` 分支、当前 PR 作者/head/base，以及当前合并提交的 base/head 父提交顺序。
标题只用于定位候选运行，不能单独证明归属。其他仅模仿标题的工作流不能启用此策略。

两个路径中的 Full CI 按创建时间及 run ID 共同选取最新请求。旧任务被重跑也不会
改变排序。最新请求成功、运行中、等待审批或已过期时，不会重新选择旧失败任务。
已结束且含失败、超时或取消任务的运行使用 `rerun failed`，确认时再次校验身份和
最新请求。目标已过期时，需要在 PR 中评论 `/ci full` 创建新测试请求；本服务不代发
评论、不创建首次运行，也不批准 workflow。

查询覆盖 GitHub 的 30 天重跑窗口。记录密集时拆分时间窗口，避免 GitHub 每次查询
最多返回 1,000 条导致遗漏，每次查找最多读取 100 页。数据不完整、分页期间变化或
超出预算时停止校验。源提交或目标提交改变后需要重新预览。与其他工作流一样，最后
校验和 GitHub 接受重跑之间仍存在并发变更的时间窗口。

保存新规则前，需升级读取规则的全部服务端进程，包括 API、管理员命令、统计采集器
和错误采集器。旧版本不认识 `pypto_ci`。原有客户端和登录可继续使用，现有 App 权限
已覆盖新增的读取操作。

CI 统计已能收集 Full CI。可用 `.github/workflows/ci-full.yml` 筛选；如指定事件应选
`workflow_dispatch`。`pull_request` 事件筛选只包含自动 PR 工作流，历史 `ci.yml`
记录仍保留为独立 workflow 分组。

## 注册和安装 App

在个人账号或用于运营服务的组织中进入：
Settings → Developer settings → GitHub Apps → New GitHub App。

1. 选择未被占用的名称，Homepage URL 可以填写 toolbox 仓库地址。
2. 安装范围选择 **Any account**，使其他组织可以安装。
3. 关闭 Webhook 的 **Active**；Callback/Setup URL 留空，开启 **Enable Device Flow**。
   不勾选安装时请求用户 OAuth 授权；普通用户通过设备授权登录。
4. 仓库权限设置：Actions **Read & write**；Checks、Contents、Pull requests
   **Read-only**；保留 Metadata 默认只读。无需组织权限或事件订阅。
5. 生成 RSA 私钥，放在代码仓库外，由服务运行用户持有，权限为 `0600` 或 `0400`。
   不接受符号链接或硬链接，不把私钥发给普通用户或提交 Git。
6. 请目标仓库的组织所有者安装 App，选择 **Only select repositories** 并勾选仓库。
   仅拥有仓库写权限的人不一定有权安装，可能需要组织管理员批准。

GitHub 的 Actions 写权限比 rerun 更广。服务的工作流客户端只允许两个重跑 POST 和
固定的只读 GraphQL 查询；独立认证客户端负责获取安装 Token，规则管理只修改服务
本地数据。用户无法指定任意 GitHub 请求。App 私钥仍是信任边界，服务器管理员或
被入侵的服务器可能使用 App 的全部已授权权限。

参考：[注册 App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)、
[安装 Token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)、
[重跑权限](https://docs.github.com/en/rest/actions/workflow-runs#re-run-failed-jobs-from-a-workflow-run)。

## 首次接入仓库

按主 README 安装当前版本（0.5.0），建议首先使用独立环境、独立数据库和空闲端口验证，保留旧
服务运行。给 App 服务设置私有环境配置，私钥路径使用绝对路径：

```bash
export PR_RERUN_GITHUB_AUTH=app
export PR_RERUN_APP_ID=123456
export PR_RERUN_APP_CLIENT_ID=YOUR_APP_CLIENT_ID
export PR_RERUN_APP_PRIVATE_KEY_FILE="$HOME/.config/pr-rerun/app.pem"
export PR_RERUN_DATABASE="$HOME/.local/share/pr-rerun/app.sqlite"
unset PR_RERUN_CONFIG
```

以上 ID 为示例。App 模式不读取旧的单仓库配置文件，也不需要个人 `gh` 登录。
先启动已配置 Device Flow 的服务，仓库管理员通过 GitHub 登录注册身份，再由平台
管理员查询数字 ID。服务可以在没有已接入仓库时接受登录：

```bash
# 服务终端（前台运行；正式部署使用下文 systemd 模板）
pr-rerun-server --github-auth app --port 8081
```

```bash
# 仓库管理员自己的终端；初次接入前省略 --repo，避免引用尚未设置的别名
pr-rerun login --netbird-server http://100.64.0.10:8081
```

```bash
# 服务主机另一个终端，使用上面同一份 App/数据库环境
pr-rerun-admin users
```

将示例 IP 换为实际 NetBird 地址。旧模式兼容命令
`pr-rerun-admin issue repo-admin --github-auth app --repo team/project` 也能注册身份，
但会输出令牌并撤销该用户现有令牌及会话，日常接入新仓库无需使用它。

复制 `policy.example.json` 到私有配置目录，将示例 ID 替换为刚才得到的 GitHub 数字
ID，填写实际工作流路径并检查所有规则，然后由平台管理员在服务端执行：

```bash
pr-rerun-admin repo-add team/project --policy-file /path/to/policy.json
pr-rerun-admin installations
```

接入会核实 App 安装、权限、仓库身份、工作流和已签发用户，不会重跑 CI。
可以先用 `enabled: false` 接入。正式启动使用 `deploy/pr-rerun-app.service` 模板，修改路径、App ID、用户和端口，
按 [systemd 部署步骤](README.zh-CN.md#通过-systemd-开机启动)操作，但使用 App 模板，
不要复制旧模式模板。使用同一数据库启动 systemd 前，先停止前台测试服务。
App 的真实安装授权必须由仓库方完成，不能靠配置文件模拟。

## 为已运行的服务添加仓库

1. 仓库方在现有 App 安装中通过 **Only select repositories** 加入目标仓库；无需新建 App。
2. 平台管理员在服务主机加载相同的 App/数据库环境，执行 `pr-rerun-admin installations`，
   确认目标仓库已授权且权限满足要求；`not_enrolled` 表示还未登记到服务。
3. 仓库管理员先通过 GitHub 登录；平台管理员用 `pr-rerun-admin users` 获取已验证的数字 ID。
4. 准备该仓库的独立规则 JSON，填入管理员 ID 和真实 workflow 路径，再执行
   `pr-rerun-admin repo-add team/project --policy-file /path/to/policy.json`。
5. 仓库管理员或服务管理员登录后设置官方别名：
   `pr-rerun alias set project team/project --official`。
6. 用 `pr-rerun repos` 检查服务登记，用 `pr-rerun-admin installations` 检查实时 App 授权。
   用户随后可执行 `pr-rerun 123 --repo project`。上述接入与查询步骤不会触发重跑。

规则保存在服务端 `PR_RERUN_DATABASE` 指向的 SQLite 数据库中，按仓库保存版本和历史；
输入 JSON 只是导入/更新材料，修改本地文件不会自动生效。不要把规则放到 PR 分支作为授权来源。
接入新仓库、更新规则无需重启或让已有用户重新登录。
`repo-add` 用于首次登记，不用于覆盖现有登记；修改规则用下文 `pr-rerun policy`。

要允许所有已登录用户重跑本人 PR，设置 `access: "authenticated_users"`，`users: []`；
`maintainers` 仍必须是已注册的数字 ID。规则里的 workflow 必须使用真实文件路径，
Codex Review 还必须提供受支持的身份 artifact，不能仅匹配工作流名称。

## GitHub 登录

服务端设置 `PR_RERUN_APP_CLIENT_ID`，并在 App 设置中开启 Device Flow。
用户首次执行 `pr-rerun login --netbird-server URL --repo owner/repo`，终端显示 GitHub
网址和一次性验证码，在浏览器中完成授权即可。服务端通过自己申请的设备授权读取
`/user` 验证身份，客户端不能自行声称用户名或 ID。无需管理员手工发令牌。
用户可在仓库接入前登录，管理员用 `pr-rerun-admin users` 获取已验证的数字 ID。

客户端只保存自动签发的服务会话，不保存 GitHub Token。每台设备会话独立，有效期
90 天，到期后重新登录；会话可跨服务重启使用，未完成的设备授权在重启后需要重试。
GitHub 用户 Token 和刷新 Token 在身份验证后丢弃；重跑仍使用 App 安装凭据。
撤销 GitHub 授权不会自动撤销独立的服务会话，平台管理员可使用
`pr-rerun-admin revoke USER_ID` 禁用该用户的全部会话，用户无法通过再次登录绕过禁用。
重新签发旧式令牌会撤销该用户已有会话。`pr-rerun logout` 仅删除本地登录。

旧服务及管理员签发的令牌使用 `pr-rerun login --token-login`，已有客户端登录仍兼容。
GitHub 登录本身不授予规则管理权限，仍需在仓库规则中指定管理员。

## 用户和仓库管理员使用

使用权限按仓库配置：默认 `access: "explicit_users"` 要求在用户或管理员名单中；
可选 `access: "authenticated_users"` 允许所有已认证服务用户重跑本人 PR。
不自动查询组织成员身份，因此不需要组织 Members 权限。
持有服务令牌不代表可以访问所有已接入仓库。

设置 `allow_drafts: true`、`base_branches: ["*"]` 可允许草稿和所有目标分支。
将 `cooldown_seconds` 设为 `0`，将 `max_requests_per_user_per_day` 和
`max_active_requests_per_pr` 都设为 JSON `null`，即可取消使用配额和冷却时间。
本人 PR 校验、手动确认、同一次运行的重复提交保护仍然有效。
服务不会批准等待人工 approval 的 workflow。

使用当前 0.5.0 客户端；旧登录文件仍可读取。仓库查询还要求 App 服务端 0.4.1。

```bash
pr-rerun login --netbird-server http://100.64.0.10:8081 --repo team/project
pr-rerun 123
pr-rerun --repo another/project 456
pr-rerun status rr_REQUEST_ID
```

将示例 IP 换成实际 NetBird 地址。仓库选择优先级为 `--repo`、环境变量
`PR_RERUN_REPOSITORY`、登录时保存的默认仓库；保存的默认仓库仅用于对应服务器。
选择另一个仓库不会自动获得其权限。确认文字仍包含完整仓库和 PR 身份。

仓库管理员查看、修改规则：

```bash
pr-rerun policy --repo team/project
pr-rerun policy --repo team/project --history
pr-rerun policy --repo team/project --policy-file /path/to/update.json
```

更新文件必须只包含 `expected_revision` 和完整的 `policy`：

```json
{
  "expected_revision": 1,
  "policy": {
    "enabled": true,
    "access": "explicit_users",
    "aliases": ["project"],
    "maintainers": [10001],
    "users": [10002],
    "allow_drafts": false,
    "base_branches": ["main"],
    "workflows": [{"path": ".github/workflows/ci.yml", "mode": "failed", "identity": "pull_request"}],
    "cooldown_seconds": 60,
    "max_requests_per_user_per_day": 20,
    "max_active_requests_per_pr": 1
  }
}
```

更新是整份规则替换，不是局部合并；请保留现有 `access`、`aliases` 和其它仍需使用的字段。

使用真实已签发身份的 ID 和当前规则版本。如果版本冲突，重新获取并审查最新规则。
回滚时，从历史复制旧规则到 `policy`，但 `expected_revision` 填**当前版本**，提交后
产生一个新的审计版本。历史接口返回最近 100 个版本，更早记录仍保存在数据库中。
规则更新在后续检查中立即生效，无需重启；查看、更新和回滚规则均不触发重跑。

## 升级和验证

变更服务模式前，停服并备份数据库。新表是增量添加，旧身份和请求表保留。如果复用
旧数据库，应先建立每个仓库的明确授权，再切换服务；旧预览版本为零，不能授权 App
模式下的新规则。不要让两个服务同时使用同一个数据库。

先隔离验证 App 注册、权限和只读预览，验证通过后再在空闲时段切换 systemd 环境并
重启。执行 `python -m pytest tests`、`ruff check src tests`、
`ruff format --check src tests`、`pyright` 和 `python -m build`。
测试生成临时 RSA 密钥并模拟 GitHub，不读取生产凭据，不执行真实重跑。

禁用规则不依赖 GitHub 可用；重新启用时会再次检查 App 授权和工作流状态。

客户端更新和旧共享 wheel 迁移统一按[客户端发布指南](client-releases.zh-CN.md)执行。
不要用历史版本的 wheel 覆盖当前安装。服务端升级、备份和版本核对见
[运维与排障](operations.zh-CN.md)；`pr-rerun-update` 不升级服务端。

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

## CI 统计规则

从 0.5.0 起，每仓库支持 `analytics_enabled`（默认 `false`）和 `analytics_access`
（默认 `maintainers`，可选 `repository_users`）。采集数据独立存储，查看全仓库统计
需要独立授权，不受“只允许重跑本人 PR”替代。见 [CI 统计指南](ci-statistics.zh-CN.md)。

升级前备份数据库。规则写入这些字段后，旧版严格校验器无法读取新规则；降级前需制定
兼容迁移或恢复对应备份，不能直接把运行代码切回 0.4.1。
