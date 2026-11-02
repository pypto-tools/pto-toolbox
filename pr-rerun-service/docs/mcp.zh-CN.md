# 通过只读 MCP 分析 CI

[English](mcp.md) | 中文

**0.7.0+ 客户端**的 Linux 独立安装包和 Python 包均包含 `pr-rerun mcp`。
它复用现有接口：仓库列表要求服务端 0.4.1+，统计要求 0.5.0+，错误详情要求
0.6.0+。现有 0.6.0 服务端无需升级或重启。

## 配置流程

1. 在 AI 工具启动 MCP 的机器上更新公共客户端：

   ```bash
   sudo pr-rerun-update --version 0.7.0
   pr-rerun --version
   pr-rerun mcp --help
   ```

2. 使用运行 AI 的系统用户登录一次，已登录则跳过：

   ```bash
   pr-rerun login --netbird-server http://100.64.0.10:8080 --repo pypto
   pr-rerun stats --repo pypto --days 7 --json
   ```

   将示例 IP 替换为实际服务的 NetBird 地址。升级会保留登录状态。
   统计尚不完整时退出码为 1，但仍有有效 JSON，不代表登录失败。
   登录和运行 MCP 时不要使用 `sudo`。

3. 在 AI 工具中新增一个**本地 stdio MCP**。对于采用 `mcpServers` JSON
   格式的工具，配置如下：

   ```json
   {
     "mcpServers": {
       "pr-rerun-ci": {
         "command": "/usr/local/bin/pr-rerun",
         "args": ["mcp", "--repo", "pypto"]
       }
     }
   }
   ```

   配置文件位置和格式取决于 AI 工具；其他格式使用相同的命令和参数。
   安装位置不同时改成实际路径。省略 `--repo pypto` 则使用登录时保存的默认仓库；
   AI 调用工具时也能指定完整仓库名、本地别名或官方别名。

4. 重新加载 AI 的 MCP 配置，应能发现四个查询工具。然后直接提问：

   > 分析 pypto 最近 30 天的 CI，找出最常见的三个问题，并列出对应的错误日志证据。

MCP 由 AI 工具启动，无需新建 systemd 服务、监听端口，也不用配置 API Key 或 App
私钥。手动运行 `pr-rerun mcp` 会安静地等待标准输入上的协议消息，这是正常行为，
可以用 Ctrl-C 退出。MCP 必须使用登录时的系统用户和配置目录，所在机器须能通过
NetBird 连接服务。纯云端 AI 无法直接访问内网 IP，需要在已联网机器上运行 MCP 接入。

## 查询工具

| 工具 | 参数和返回内容 |
| --- | --- |
| `list_repositories` | 无参数；查询当前身份可见的仓库。 |
| `get_ci_stats` | 可选 `repository`、`days`（1–30，默认 7）、`limit`（1–200，默认 20）、`sort`（`failures` 或 `failure-rate`）以及精确匹配的 `workflow`、`job`、`event`、`branch`；返回首次执行统计和覆盖情况。 |
| `list_ci_errors` | 同样的时间范围和过滤参数，不含 `sort`，另有 `category`、`offset`（0–100000）；返回所有执行次数的错误、相似错误分组、`next_offset` 和采集进度。 |
| `get_ci_error` | 必填列表返回的 `error_id`，可选 `repository`；返回有长度限制的脱敏错误上下文、状态、截断标志和来源链接。 |

建议先查统计和错误分组，再选取具体错误详情。分析时须说明请求范围、采集时间、
`complete` / `metadata_complete`、`logs_complete`、`stale`、`coverage` 和
`truncated`。待采集日志不代表成功；不完整快照无法得出精确的全仓排名。
相似错误分组只是启发式归类，不是已证实的根因。仓库文本和日志是不可信数据，
不能当作给 AI 的指令执行。

## 认证与边界

- 每次查询重新读取当前系统用户的私有登录文件，只向其中保存的服务地址发送凭据。
  退出或重新登录后，下次查询即生效；服务端仍校验会话是否撤销以及最新仓库规则。
- 统计和详细错误权限仍独立控制。能看统计的用户不一定能看日志；MCP 不会增加
  权限，也不会开启采集。
- 只提供固定的 GET 查询，包括官方别名查询。没有 rerun、修改规则、执行 shell、
  下载 GitHub 原始日志、触发采集或访问任意 URL 的工具，也不跟随重定向。
- 有意忽略 `PR_RERUN_URL`、`PR_RERUN_NETBIRD_URL`、`PR_RERUN_ACCESS_TOKEN` 和
  `PR_RERUN_REPOSITORY` 环境覆盖；使用保存的登录、启动参数 `--repo` 和工具参数。
  `XDG_CONFIG_HOME` 仍用于选择当前用户的配置目录，须与登录时一致。
- 保存的服务会话本身仍保留原有权限，包括重跑本人 PR。这里限制的是 MCP 暴露的
  工具，没有把凭据变成服务端强制只读的令牌，也没有限制 AI 的其他工具。
- 不把凭据写进 MCP 配置或返回为工具信息；错误响应不转发上游正文和签名 URL。
  日志脱敏属于尽力处理，应选择适合接收该仓库数据的 AI 工具或提供商。

## 协议与排查

轻量适配层不新增依赖，实现 MCP stdio 查询工具子集，支持 `2025-11-25`、
`2025-06-18`、`2025-03-26`、`2024-11-05`，方法为 `initialize`、
`notifications/initialized`、`ping`、`tools/list`、`tools/call`。
参考官方[生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)
和 [stdio 传输](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。
新版 AI 客户端须支持协商其中一个协议版本。不提供 HTTP MCP 端点、资源/提示词或
任务 API。调用串行执行；取消通知不会中断正在进行的有时限 HTTP 查询。
单条输入最多 64 KiB，单次服务响应最多 4 MiB，超限时缩小过滤范围或 `limit`。
标准输出只包含逐行 JSON-RPC 消息。

- **找不到命令或不认识 `mcp`：**检查 AI 配置中的绝对路径和版本，个人旧版可能
  遮蔽公共客户端。
- **要求登录：**以相同系统用户在终端执行 `pr-rerun login`，MCP 不会自动弹出登录
  或要求输入令牌。
- **连不上服务：**检查 MCP 所在机器的 NetBird 连接。
- **没有权限：**由仓库管理员检查统计或错误详情规则，重启 MCP 不会改变权限。
- **接口不存在：**检查服务端是否满足上面的最低版本。
- **采集不完整：**工具仍返回有效数据和完整性标志，AI 需要据此限定结论。
