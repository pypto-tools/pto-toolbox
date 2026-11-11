# 使用评论放行原生 CI 门禁

[English](ci-gate.md)

这个可选服务复用现有 GitHub App。App 提供身份和权限；`pr-ci-gate-server`
负责接收 webhook 并批准等待中的 environment。仅安装 App，不会自动部署这段
处理代码，也不会自动启用 environment 保护规则。

流程为 `lint + clang-tidy → full-gate → tests → ci/full`。
`full-gate` 引用受保护的 environment，GitHub 在为该 job 分配 runner 前等待。
任何真实用户发表新的 `/ci full` 评论后，App 放行；后续测试在同一次 run 中继续，
在 PR 上显示原生 job 检查。`ci/full` 是最后的汇总结果，放行项叫 `full-gate`。
不需要另开一次 CI、回写 commit status，也不需要占用 NPU runner 轮询。

## 配置 App 和服务

1. 保留现有 rerun 权限，增加仓库 **Deployments: Read and write**。
   启用 Webhook **Active**，订阅 **Issue comment** 和 **Deployment protection rule**。
   门禁单独申请 Actions、Contents、Pull requests 只读和 Deployments 写权限；
   rerun token 的权限不变。
2. 使用 **Only select repositories** 安装 App，选择测试 fork。
   已安装的仓库需要接受新增权限。在安装设置页面 URL 中获取 installation ID，
   或在现有 App 服务器运行 `pr-rerun-admin installations`。
   仓库数字 ID 可用 `gh api repos/OWNER/REPO --jq .id` 查询。
3. 在 App 服务器安装此版本。将 `deploy/ci-gate.example.json` 复制到运维配置目录，
   替换仓库名称、仓库 ID、installation ID、目标分支、workflow 路径、environment
   和 lint job 的**显示名称**。初期只登记测试 fork。
   此配置与 rerun 登记独立；可以发表评论不代表获得 rerun 权限。
4. 生成至少 32 字节的随机 webhook secret，保存在 checkout 外，由服务用户拥有，
   权限设为 `0600` 或 `0400`。在 App 的 Webhook secret 中填写相同值。
   通过 `PR_RERUN_APP_ID` 和 `PR_RERUN_APP_PRIVATE_KEY_FILE` 复用现有 App 身份。
5. 调整 `deploy/pr-ci-gate.service`，启动这个额外进程，监听 `127.0.0.1:8082`。
   用 HTTPS 反向代理只暴露其 `POST /github/ci-gate`，限制请求体为 1 MiB，
   配置较短的请求体读取超时。将公开的 HTTPS URL 填入 App 的 Webhook URL。
   `/health` 留作内部监控。现有 NetBird rerun API 和管理入口继续保持私有。
6. 在 fork 中创建没有 secret 的 `ci-full` environment，启用该 App 的自定义部署
   保护规则；如果不允许管理员绕过，也关闭管理员 bypass。
   确认保护规则已启用后再安装测试 workflow；没有保护规则的 environment 会直接运行。

App 所有者修改 App 注册配置，fork 管理员接受权限并设置 environment，服务运维者
部署进程。这些角色可以是同一个人。现有 CLI 登录和 rerun 确认流程无需变更。

## 只在 fork 验收

将 `deploy/ci-app-gate-prototype.yml` 放到 fork 的 `ci-app-gate-sandbox` 分支，
路径为 `.github/workflows/ci-app-gate-prototype.yml`。模板限定为 `luohuan19/pypto`；
使用其他 fork 时，同时修改模板和服务配置。这里都是 GitHub 托管 runner 上的
轻量模拟任务，不执行真实 lint、编译或 NPU 测试。

1. 创建目标为 sandbox 分支的 PR，确认 lint、clang-tidy 完成，`full-gate`
   等待 App，后续测试没有启动。
2. 单独发表评论 `/ci full`，确认 webhook 成功、门禁放行，原 run ID 下所有
   原生检查结束。
3. 在测试 PR 标题中加入 `[fail-once]`。模拟 system-test 首次失败后 rerun failed，
   确认成功的 lint、clang-tidy、gate 保留原执行时间，只重跑失败及依赖任务。
4. 推送新提交，确认旧评论不能放行新 run；重新评论后才能继续。还要测试外部 fork
   PR，因为其 Actions API 返回的 `pull_requests` 可能为空。
5. 等待期间重启服务，再评论放行；重投 webhook，确认不会另开 CI 或重复批准。

上述真实验收通过前不要迁移主线。模拟 API 的本地测试不能证明 GitHub 配置和真实
webhook 行为正确。服务要求 workflow 使用以下 run-name 格式：

```yaml
run-name: 'CI gate PR #${{ github.event.pull_request.number }} @${{ github.event.pull_request.head.sha }} base:${{ github.event.pull_request.base.sha }}'
```

run-name 只用于定位，不能单独证明身份。服务还检查实时 run、workflow、仓库 ID、
head 仓库及分支、PR head/base、deployment 提交父节点、指定 lint job 的成功结果。
workflow 应按 PR 配置 concurrency，并启用 `cancel-in-progress: true`。

## 消息和重试

签名验证后的事件先规范化并写入 SQLite，再返回 HTTP 202；处理 GitHub API 不会
阻塞 webhook 响应。单个 worker 处理持久化队列。先评论、后收到门禁事件也能通过
查询 PR 评论识别；后到的评论唤醒门禁，定期检查也可修复丢失的评论通知。
仅接受严格晚于本次 run/attempt 开始时间的、未编辑的真实用户新评论。新 head、
base 变更、PR 关闭、安装变化或旧 attempt 不会沿用批准。base 更新后需要新的 PR run。

临时错误按有上限的退避间隔重试；连续失败 20 次后记录为 `failed`。
修复原因后，在 App 的 Recent deliveries 中重投该事件；失败记录会重新入队，
已完成记录不会重新批准。日志只记录 delivery ID 和成功的仓库/run/attempt ID，
不记录凭据及原始 payload。数据库需要持久化存储，监控 `/health` 和日志中的
`state=failed`。已完成事件保留 31 天，批准记录保留 36 天；最多接收 10,000 个
活跃事件，队列满时拒绝新请求。

GitHub 批准 API 没有 expected-head 或 expected-attempt 参数。服务会在写入前
重新核验，但无法让批准操作与同时发生的 push/rerun 成为原子事务。
workflow concurrency 取消旧 run，旧检查仍属于原来的版本。批准时断线可能重试
同一 environment，但不会因此启动另一轮 CI。

这只是 CI 调度机制，不提供完整的防恶意 PR 保障。PR 仍可能修改 workflow 或新增
不依赖门禁的任务。应独立控制自托管 runner 的访问，门禁 environment 不放 secret，
并在正式启用前审核 workflow 变更。

参考：[自定义保护规则](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/create-custom-protection-rules)、
[environment 配置](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/configure-custom-protection-rules)、
[webhook 签名](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)。
