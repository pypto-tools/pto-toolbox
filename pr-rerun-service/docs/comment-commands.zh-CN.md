# PR 评论命令

[English](comment-commands.md)

服务端 0.9.0 支持 PR 作者新发布一条内容为 `/ci rerun` 的评论，触发已有失败任务的重跑。
可以有前后空白，不能有其它参数；编辑已有评论、代码审查评论、普通 issue 或机器人评论不会触发。
只有 PR 作者可以操作，仓库管理员和服务管理员也不能代替其他作者重跑。
评论本身就是对当前 PR 的确认；服务绑定当前提交，并在提交重跑前再次检查。

作者无需先登录客户端，GitHub 签名事件和实时评论查询用于确认其数字用户 ID。
`access: authenticated_users` 允许任意 PR 作者使用；`explicit_users` 仍要求作者在
`users` 或 `maintainers` 中。已禁用的身份仍被拒绝，已有客户端登录状态不会被替换。
仓库的 workflow、分支、草稿和使用限制保持生效。

普通 CI 使用 rerun failed，覆盖符合条件的取消任务；身份验证通过的 Codex Review 使用 rerun all。
PyPTO Full CI 仍需已有的 `pypto_ci` 规则。此命令不创建首次运行、不执行 `/ci full`、
不批准待审批 workflow、不替其他作者重跑，也不会回复评论。执行结果到 GitHub Actions 查看；
没有新 attempt 可能表示没有可重跑任务，或命令被拒绝。

## 部署流程

1. 备份数据库，将 API、管理员命令、CI 统计采集器和错误采集器全部升级到 0.9.0，
   再保存新规则字段。保留 App 配置、登录、规则和定时器。客户端无需更新。
2. 按[英文指南中的生成命令](comment-commands.md#deployment)，以服务用户身份创建
   `~/.config/pr-rerun/webhook.secret`。文件权限 0400 或 0600，目录权限 0700，不能是符号链接。
   在 API 的环境中设置 `PR_RERUN_WEBHOOK_SECRET_FILE` 为该文件绝对路径，并重启 API。
   未配置时 `/github/webhook` 返回 404。Webhook Secret 与 App 私钥是不同的凭据。
3. 公网服务器提供 HTTPS，只将 **POST /github/webhook** 经 NetBird 转发到后端。
   `/api/*`、健康检查、文档和其他路径均不公开。可使用 [Nginx 示例](../deploy/webhook-nginx.conf.example)，
   替换其中的后端地址，保留请求原始内容和 GitHub 签名头，关闭代理重试。
   App 私钥和 Webhook Secret 都留在后端；公网代理不需要这些凭据。
   代理可独立运行在已有 TLS 入口的容器网络中，不发布宿主端口；只读、非 root Nginx 容器需可写
   `/tmp`，以 `-e /dev/stderr -g 'daemon off;'` 启动。验证正常 HTTPS 证书，不跳过证书校验。
4. 在 GitHub App 设置添加仓库 **Issues: Read-only**，订阅 **Issue comment**，
   由安装方批准权限更新。保留 Actions write 和 Pull requests read，不需要 Pull requests write。
   开启 Webhook，URL 设为 `https://你的域名/github/webhook`，将私密文件中的 Secret
   直接填入 GitHub。不要把 Secret 发到聊天、工单、Git 或日志中。确认签名 ping 返回 `pong`。
5. 用 `pr-rerun policy --repo owner/repo` 查看规则，通过现有带版本校验的更新流程，
   只将 `comment_reruns_enabled` 设为 `true`，保留其它字段。先在测试仓库启用。
   安装 App 不等于登记仓库或启用命令。改回 `false` 会停止该仓库新的及排队中的命令。
6. 由符合条件的测试 PR 作者新发 `/ci rerun`，检查 Actions 新 attempt 和下方审计记录。
   不用他人的 PR 或伪造真实评论事件做测试。

GitHub 的 [Issue comment 权限说明](https://docs.github.com/en/webhooks/webhook-events-and-payloads#issue_comment)
与 [签名校验说明](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)。

## 查询结果及失败处理

在后端使用与 API 相同的数据库：

```bash
pr-rerun-admin comment-commands --repo owner/repo --limit 20
```

结果包含 PR、用户数字 ID、评论 ID、请求 ID、状态和脱敏说明，不保存凭据或评论正文。
`queued` 表示已持久化，`processing` 表示处理中；`accepted` 只表示 GitHub 接受了重跑，
不代表 CI 通过。`ineligible` 表示没有验证通过的可重跑任务，`rejected` 表示校验失败。
`partial`、`unknown` 或 `interrupted` 需要先检查 GitHub Actions，再决定是否新发评论。
内部请求 ID 用于审计，不能用另一个客户端会话的 `status` 命令查询。

接收端先校验原始请求体的 HMAC-SHA256 签名，再持久化入队，不等待 GitHub API 查询。
请求体最多 256 KiB，排队及处理中的命令最多 1,000 条。后台单 worker 按顺序处理，
评论在创建后十分钟失效，排队时间也计入。服务重新查询原评论、作者、仓库安装和规则；
每次写操作前再次校验，PR 提交变化就停止。workflow 身份校验和已有防重复机制继续生效。

投递 ID 和仓库/评论 ID 共同防止重复执行，换投递 ID 重发同一评论也不会重复运行。
重启后尚未开始的有效命令可以继续，已开始处理的命令标记 `interrupted`，不会自动重放。
对 GitHub 写请求结果不确定时不自动重试。投递失败可检查 App 的 Recent deliveries 和后端审计；
GitHub 不自动重投，过期后需新发评论。参见 [GitHub 投递建议](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks)。

队列表位于原有 rerun SQLite 数据库，只保留小型审计行，不保留事件正文或任务日志，
随数据库一起备份。目前不自动清理审计记录。更换 Secret 时同步更新 GitHub 和后端文件，再重启 API。
