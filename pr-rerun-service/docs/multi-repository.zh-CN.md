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

按主 README 安装 0.3.0，建议首先使用独立环境、独立数据库和空闲端口验证，保留旧
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
先通过已授权给 App 的目标仓库核实并签发真人服务身份：

```bash
pr-rerun-admin issue repo-admin --github-auth app --repo team/project
pr-rerun-admin issue contributor --github-auth app --repo team/project
pr-rerun-admin users
```

`issue` 会输出一次服务 Token，需私密交付。同一用户重新签发会撤销其原有全局服务
Token；新增仓库授权时直接复用已有服务身份，不必重复签发。

复制 `policy.example.json` 到私有配置目录，将示例 ID 替换为刚才得到的 GitHub 数字
ID，填写实际工作流路径并检查所有规则，然后由平台管理员在服务端执行：

```bash
pr-rerun-admin repo-add team/project --policy-file /path/to/policy.json
pr-rerun-server --github-auth app --port 8081
```

接入会核实 App 安装、权限、仓库身份、工作流和已签发用户，不会重跑 CI。
可以先用 `enabled: false` 接入。正式启动使用 `deploy/pr-rerun-app.service` 模板，修改路径、App ID、用户和端口，
再沿用 NetBird/systemd 的部署说明。App 的真实安装授权必须由仓库方完成，不能靠配置文件模拟。

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
本人 PR 校验、精确确认、同一次运行的重复提交保护仍然有效。
服务不会批准等待人工 approval 的 workflow。

App 模式需要 0.3.0 客户端；旧登录文件仍可读取，但旧客户端不能选择仓库。

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

已经使用 `/opt/pr-rerun-client` 全机共享客户端的机器，可以由该机器管理员在构建
新版 wheel 后升级软件包。以下从服务源码目录执行；相比 0.2.3，Python 依赖没有变更：

```bash
sudo /opt/pr-rerun-client/bin/python -I -m pip --isolated install --no-index --no-deps "$PWD/dist/pr_rerun_service-0.3.0-py3-none-any.whl"
sudo /opt/pr-rerun-client/bin/python -I -m pip --isolated check
/usr/local/bin/pr-rerun --help
```

用户仍用普通账号登录，不加 sudo。首次安装脚本会拒绝覆盖已有共享安装，不用于升级。

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
