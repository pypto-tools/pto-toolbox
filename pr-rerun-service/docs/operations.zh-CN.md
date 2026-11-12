# 运维与排障

[English](operations.md) | 中文

本文面向 0.4.1 App 部署。首次配置、仓库接入见[仓库规则指南](multi-repository.zh-CN.md)；
客户端安装、迁移、离线更新和回退见[发布指南](client-releases.zh-CN.md)。
文档中的地址、路径和系统账号均为示例。

## 升级 PyPTO Full CI 支持（0.8.1）

准备 0.8.1 wheel，先将 API、管理员命令和两个采集服务切换到新代码，再应用
[`pypto_ci` 规则](multi-repository.zh-CN.md#pypto-评论触发的-full-ci服务端-081)。
采集器也会解析仓库规则，旧版会拒绝新的身份策略。切换时暂停采集并备份数据库，
启动新 API 后按当前规则版本更新仓库规则，再恢复定时采集。保留原有采集预算、
周期、登录状态及 NetBird 绑定。此次后端适配不要求更新客户端。

回滚时先通过新版服务恢复原规则，再把 API、管理员命令及采集器切回旧代码。
保留旧 wheel 和规则快照；旧代码不能读取含 `pypto_ci` 的规则，也不要直接恢复旧
数据库而丢失备份后产生的会话和审计记录。

## 各部分部署在哪

| 组件 | 位置 | 更新方式 |
| --- | --- | --- |
| `pr-rerun`、`pr-rerun-update` | 每台客户端机器，全机安装一次 | 各机器执行 `sudo pr-rerun-update --version 0.4.1` |
| `pr-rerun-server`、`pr-rerun-admin` | 只在服务主机的服务端 Python 环境 | 升级服务 wheel，再重启服务 |
| 登录和个人别名 | 各系统用户的 `~/.config/pr-rerun`，或 `XDG_CONFIG_HOME` | 客户端更新保留 |
| 用户、会话、仓库登记、规则、官方别名和请求历史 | `PR_RERUN_DATABASE` 指定的服务端 SQLite 数据库 | 服务升级时保留并备份 |
| App 私钥和服务配置 | 服务主机受保护的文件及环境配置 | 保留，不向客户端分发 |

服务主机也可以安装共享客户端，客户端和服务端版本相互独立。三台机器更新客户端，
不代表需要三套服务或重新安装 App。用户以普通系统账号登录，不加 sudo；多人共用
同一系统账号时也共用其登录状态。

## 检查版本和服务健康

```bash
type -a pr-rerun
pr-rerun --version
/usr/local/bin/pr-rerun --version
systemctl is-active pr-rerun.service
systemctl is-enabled pr-rerun.service
curl --fail --noproxy '*' http://127.0.0.1:8080/healthz
```

systemd 和回环地址检查在服务主机执行，按部署调整服务名及端口。
`healthz` 只报告进程健康，不报告版本，也不代表能访问 GitHub。
核对服务端代码时，在本机检查配置的 `ExecStart`，使用它实际调用的 Python 环境
及相同的模块路径：

```bash
# 示例：替换为该部署 ExecStart 使用的 Python 和模块路径。
/opt/pr-rerun-service/.venv/bin/python -I -c 'from pr_rerun.version import VERSION; print(VERSION)'
```

如果启动脚本注入了单独的版本目录 `app`，检查时也要保留这项选择；底层 Python 环境
安装的包可能不是运行服务使用的包。求助时不要发布完整环境变量或凭据。

使用已有登录执行 `pr-rerun repos` 可检查服务登记可见性。在服务主机以服务运行用户
加载相同的 App/数据库配置，执行 `pr-rerun-admin installations` 可检查实时 App 授权。
两者都不会重跑任务。前者要求客户端和 App 服务端均为 0.4.1；管理命令本身也要来自
0.4.1 的服务环境，不包含在独立客户端中。

## 升级已有服务端

客户端升级保留登录、别名和仓库登记。服务端升级使用原数据库和配置时同样保留这些
状态；会话仍可能独立到期或被撤销。升级不需要创建空数据库、用 `issue` 轮换令牌，
也不需要重新安装 App。

1. 从可信 `pr-rerun-v0.4.1` Release 获取服务 wheel，以服务运行用户准备独立的
   Python 3.10+ 环境，不覆盖正在运行的环境。安装 wheel、解析依赖并运行 `pip check`。
   独立客户端内置的 Python 用于客户端，不能代替服务端环境配置。

   ```bash
   # 示例暂存环境：使用新目录，不要填写当前运行环境的路径。
   python3.10 -m venv "$HOME/pr-rerun-server-0.4.1"
   "$HOME/pr-rerun-server-0.4.1/bin/python" -m pip install ./pr_rerun_service-0.4.1-py3-none-any.whl
   "$HOME/pr-rerun-server-0.4.1/bin/python" -m pip check
   "$HOME/pr-rerun-server-0.4.1/bin/pr-rerun-server" --help
   "$HOME/pr-rerun-server-0.4.1/bin/pr-rerun-admin" installations --help
   ```

2. 记录原启动命令、systemd unit 和 drop-in，准备指向新环境的 `ExecStart`。保留服务
   系统用户、HOME、数据库路径、App 配置、私钥路径、登录代理、NetBird 绑定及端口。
   通用安装器会拒绝覆盖不同的已有 unit，升级已有部署需检查实际 unit 和 drop-in。
3. 在空闲时段停服，用 SQLite 备份功能将数据库备份到私有目录；完成备份前保留 WAL
   状态，不能只复制仍运行中的 `.sqlite` 文件。保留旧环境和服务配置。包括临时验证
   服务在内，不要让两个服务同时打开同一数据库。
4. 应用检查过的启动配置，执行 `sudo systemctl daemon-reload`，再执行
   `sudo systemctl start pr-rerun.service`。个人 `pr-rerun-admin` 启动脚本也要指向新环境；
   仅修改 systemd 不会自动更新该命令。
5. 核对健康状态及版本，再执行 `pr-rerun repos` 和 `pr-rerun-admin installations`，
   确认仓库登记、规则和官方别名仍在。上述验收无需提交任何 PR 重跑。

故障恢复时先停新服务，再切换启动入口。旧代码运行前检查数据库结构是否兼容；恢复
数据库备份会丢失备份后的会话、规则变更和请求记录，必须作为明确的恢复操作处理。
客户端的 `--rollback` 不会回退服务端。

## 常见问题

| 现象 | 检查及处理 |
| --- | --- |
| `repos` 报 `Specify a positive PR number` | 执行的是旧客户端，按发布指南排查 PATH，不要清除登录文件。 |
| `repos` 提示需要 App 服务端 0.4.1 | 升级服务端，仅更新共享客户端无效。 |
| 找不到 `pr-rerun-admin` | 使用服务主机的服务端环境，独立客户端不包含管理命令。 |
| 系统 Python 为 3.9，或 pip 下载不了 `build` | 客户端使用独立 Release，无需构建 wheel 或替换 Python；受限网络使用代理或离线安装。 |
| 客户端无法连接服务 | 检查 NetBird 连通性、节点访问策略、服务状态和 IP/端口；先在服务主机验证回环入口，不要改为监听 `0.0.0.0`。 |
| `GitHub login is unavailable` | 检查服务端设备登录网络和主 README 中的 `PR_RERUN_LOGIN_PROXY`；客户端下载代理不会替服务端配置登录代理。 |
| 首次 App 配置后登录不可用 | 检查 App Client ID、Enable Device Flow；修改服务环境后重启。不需要 Members 权限或公网回调。 |
| `repos` 看不到仓库 | 检查启用状态和用户访问规则；服务管理员能看到停用仓库。仅安装 App 不等于已登记。 |
| `installations` 显示 `not_enrolled` | App 已授权，按仓库指南完成本地服务登记。 |
| 清单不完整或 `unknown` | 恢复 GitHub 连通性后重新只读查询，不能把查不到当成授权已撤销。 |
| PR ownership could not be verified | 加 `--verbose` 排查；关联缺失、有歧义或 Codex 身份 artifact 缺失时仍不允许重跑，不能绕过归属校验。 |
| 没有确认提示 | 没有符合条件的失败任务时不会提交；有任务时必须在终端明确输入 `y`。 |
| 提交结果未知 | 重试前先用 `pr-rerun status rr_REQUEST_ID` 检查；`accepted` 只代表 GitHub 接受，不代表 CI 通过。 |

排障提供版本、命令路径、仓库/请求 ID 和脱敏错误信息即可。不要分享客户端登录 JSON、
App 私钥、服务令牌或带签名的 artifact 下载 URL。两种查询的可见范围和退出码详见主 README。
