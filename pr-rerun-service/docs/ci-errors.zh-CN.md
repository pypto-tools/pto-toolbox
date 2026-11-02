# CI 错误详情

服务端和客户端 0.6.0 新增只读日志采集、错误摘要、上下文、相似摘要聚合和原始页面链接。
不会触发重跑。[English](ci-errors.md)

## 按仓库开启

保持 `analytics_enabled: true`，通过正常的带 revision 校验的仓库规则更新，
在现有完整规则中添加以下字段（这不是一份可独立提交的完整规则）：

```json
{
  "error_logs_enabled": true,
  "error_logs_access": "maintainers"
}
```

默认仅服务管理员和配置的仓库维护者可读。汇总统计向普通用户开放，不会同时开放错误片段。
改为 `error_logs_access: repository_users` 才会向该仓库访问规则允许的用户开放。
禁用用户、禁用仓库或撤销 App 安装后均无法读取缓存详情。私有仓库使用
`authenticated_users` 开放元数据或日志前，维护者应明确决定该范围。
现有 App 的 Actions 只读权限已足够，不需新账号，也不调用外部 AI 服务。
错误分类采用规则匹配，不代表已确定根因。

## 后台采集

先运行 `ci-sync`，再使用相同的 App 配置和统计数据库：

```bash
pr-rerun-admin error-sync --repo owner/repository --days 30 \
  --request-budget 100 --max-logs 100 --json
```

使用独立的 [service](../deploy/pr-rerun-errors.service) 和
[timer](../deploy/pr-rerun-errors.timer)，替换账号、路径、环境文件及仓库。
每轮结束后等待十分钟，再加最多三十秒随机延迟。日志采集独立于统计采集和 HTTP 服务。

优先处理较新的已完成失败或超时记录，成功 job 中失败的步骤也会收集。
已有步骤编号时读取步骤日志，旧缓存缺少步骤编号时读取 job 日志。
按仓库安装、run、attempt、job、step 去重，保留每次 attempt；后续重跑成功不会删除旧错误。
错误列表包含所有 attempt，与失败率只看首次 attempt 的口径不同。

进度与结果保存在现有 `.stats.sqlite` 的 `ci_errors` 和 `ci_error_sync` 表中，可断点续采。
查询不会写入数据库。仅保存脱敏片段与元数据，完整日志和临时签名下载地址不落盘。
后续采集会清理超过 31 天的记录。查询支持 1～30 天，以 run 创建时间和统计快照截止时间
确定窗口；查询 30 天错误时，元数据也必须采集 30 天。

默认每轮最多请求和处理 100 份日志，最多可配置 500；单日志最多 4 MiB、每轮最多 64 MiB。
流式下载限定三十秒加网络超时；拒绝压缩响应和二次跳转。仅接受 GitHub 返回的 HTTPS
Actions/Azure 日志存储地址，不向存储站点转发 API Token。
统计与日志采集共用 App 安装配额，剩余低于 1,000 次时暂停；日志采集遵循额度重置和
Retry-After 时间。这是预留措施，不能保证并发 rerun 永远不会遇到限流。

状态包括 `pending`（等待）、`retry`（稍后重试）、`ready`（已提取）、
`unavailable`（日志缺失、过期或被删除）和 `failed`（三次尝试后仍失败）。
临时错误间隔十分钟重试，最多三次；之后需管理员检查。超大日志标记 `truncated`，
超出下载前缀的错误可能无法提取。分别报告元数据覆盖、日志覆盖和时效；日志下载失败
不影响现有汇总统计。

脱敏覆盖常见 Token、认证头、敏感赋值、私钥块及 URL 路径和查询参数，但不能保证识别
任意密钥、编码内容或项目专属敏感信息。脱敏片段仍按日志资料限制访问；原始日志通过
GitHub 页面按 GitHub 自身权限查看。

## 客户端查询

```bash
pr-rerun errors --repo pypto --days 30
pr-rerun errors --repo pypto --days 30 --group
pr-rerun errors --repo pypto --days 30 --job 'clang-tidy' --category compile
pr-rerun errors --repo pypto --days 30 --limit 20 --offset 20
pr-rerun error err_0123456789abcdef0123456789abcdef --repo pypto
pr-rerun errors --repo pypto --days 30 --json
```

`--group` 按归一化摘要统计所有 attempt 的出现次数，不等于独立根因数或受影响 PR 数。
支持按 workflow 完整路径、job、分类、event、branch 精确筛选。
分类：`network`、`resource`、`timeout`、`test`、`compile`、`exception`、`unclassified`。
列表只返回摘要，详情返回带行号且有长度限制的上下文；方括号等按原文显示。
退出码 1 表示覆盖不完整、过期或详情尚未就绪，2 表示请求错误。

接口为 `GET /api/repositories/{owner}/{repo}/errors` 和
`GET /api/repositories/{owner}/{repo}/errors/{error_id}`。
查询只读取缓存并校验当前权限，不会即时下载日志。

## 升级与回退

先升级服务端，再启用新规则字段和 error-sync 定时任务。
客户端执行 `sudo pr-rerun-update --version 0.6.0`，无需重新登录。
升级前备份服务数据库和统计数据库。旧服务端严格校验规则字段，不能直接带着新增字段
降级；回退需停止新采集器，迁移规则到兼容格式或恢复升级前数据库，恢复备份会丢失备份后的变更。
