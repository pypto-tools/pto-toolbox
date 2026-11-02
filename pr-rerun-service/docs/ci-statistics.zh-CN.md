# CI 统计（0.5.0）

[English](ci-statistics.md) | 中文

复用当前 GitHub App、服务登录和仓库别名，统计过程不会重跑任务或下载日志。
API 校验用户权限及 App 当前授权后查询独立 SQLite 缓存。采集器只申请目标仓库的
Actions/Metadata 只读临时凭据，与重跑写凭据分开缓存，无需增加 App 权限。

## 服务端启用

服务端和客户端升级到 0.5.0，现有有效登录继续使用。仓库管理员或服务管理员获取完整
规则后，通过正常的版本校验更新流程，在 `policy` 中添加：

```json
{
  "analytics_enabled": true,
  "analytics_access": "maintainers"
}
```

上面只是新增字段，不是可以直接提交的完整更新文件。保留其它字段并提供当前
`expected_revision`，参见[规则更新](multi-repository.zh-CN.md)。旧仓库默认关闭统计。
`maintainers` 仅允许仓库管理员和服务管理员查看；`repository_users` 额外允许符合
该仓库重跑访问规则的用户查看，不会给外部用户新增仓库权限。停用仓库或统计后，
采集和 API 查询都拒绝。仅获得重跑权限默认不意味着可以查看全仓库 CI 明细。

原服务数据库仍保存规则和身份。统计默认保存在 `<PR_RERUN_DATABASE>.stats.sqlite`，
也可以显式设置 `PR_RERUN_STATS_DATABASE`。API 服务和采集器必须使用相同统计路径，
且不能与原数据库相同。保护数据库和父目录权限。采集器只读原服务的接入记录，
不会初始化或迁移原服务数据库。

在服务主机，以服务运行用户加载相同的 App/数据库环境后执行：

```bash
pr-rerun-admin ci-sync --repo owner/repository --days 7
pr-rerun-admin ci-sync --repo owner/repository --days 30 --request-budget 1500 --json
```

采集器使用完整仓库名，不读取客户端别名。完整采集退出码 0；不完整但保留进度为 1；
配置或授权错误为 2。每个统计数据库同一时间只允许一个采集器。已完成且未变化的运行
复用缓存；活动或变化的运行重新查询。每轮重新枚举选定的创建时间窗口，因此能发现
旧运行的新重跑。仅在完整枚举后清理已删除的运行。重复执行不会累加重复数据，失败后
下轮复用已保存的进度。完整枚举后清理该仓库超过 31 天的缓存行。

默认每轮最多 1,500 次 Actions 读取请求，可配置上限为 5,000；App 授权查询也消耗额度，
但不包含在此计数中。GitHub 报告剩余额度不足 100，或返回 401/403/429 时停止，
由下次定时任务重试，不无限等待重试。统计与重跑仍共享 installation 的限流额度，
应结合其它 API 使用量调整预算和间隔。

## 定时采集

修改 `deploy/pr-rerun-stats.service` 和 `.timer` 中的系统用户、环境文件、Python 路径
和完整仓库名。私有环境文件配置 App ID、私钥**路径**和数据库路径；私钥仍保留在独立
受保护文件。使用服务端 Python 环境，不使用独立客户端。检查后将文件安装到
`/etc/systemd/system/`，执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pr-rerun-stats.timer
systemctl list-timers pr-rerun-stats.timer
```

开机后开始采集，每次完成后间隔十分钟再次执行。另一个仓库使用不同 unit 名称并修改
timer 的 `Unit=`；共用统计数据库时错开时间，锁冲突时采集器退出。停用定时采集执行
`sudo systemctl disable --now pr-rerun-stats.timer`；若已有采集正在运行，需要另行停止
对应 service。无需 Webhook 或公网监听。

## 用户查询

```bash
pr-rerun stats --repo pypto --days 7
pr-rerun stats --repo pypto --days 30 --sort failure-rate
pr-rerun stats --repo pypto --event pull_request --limit 50
pr-rerun stats --repo pypto --event push --branch main
pr-rerun stats --repo pypto --workflow .github/workflows/ci.yml --job 'compile (a5, cpu)'
pr-rerun stats --repo pypto --json
```

也支持已保存的默认仓库。筛选字段是精确匹配；`--branch` 指 **head 分支**，不是 PR 的
目标分支。Codex 等工作流的 `pull_request_target` 是独立事件。workflow 使用完整文件
路径。matrix 完整名称分别统计，暂不自动去掉参数或按名称归类。默认返回前 20 组，
最多 200 组，`total_groups` 显示总组数，避免隐藏截断。JSON 标准输出不混入别名提示。

API 为 `GET /api/repositories/{owner}/{repo}/stats`，接受相同查询参数，需要正常服务会话。
查询不会启动采集；缓存不存在返回 503，未启用或无权查看返回 403。旧服务需升级。
除会话过期或被撤销外，无需重新登录。

## 统计口径和限制

- 窗口按 **workflow run 创建时间**选择最近 N 天，以本轮采集开始的 UTC 时间为终点。
  这些运行的后续 attempt 归入同一批次，不等于按 job 实际执行日期统计全部任务。
- 排名只统计首次 attempt。样本数＝成功＋失败＋超时；失败率＝（失败＋超时）÷样本数。
  没有样本时 JSON 为 `null`。取消、跳过、neutral、未完成及其它结果分别保留。
  按失败次数排序时包含超时，但两项数量分开展示。
- 分组使用 workflow ID、路径和完整 job 名称。同一次 attempt 有重名 job 时，执行次数
  正常统计，但不猜测重跑对应关系，以 `ambiguous_recovery_runs` 标注歧义。
- `rerun_executions` 统计后续 attempt 的独立 job，排除沿用的旧 job ID。
  `retried_failures` 为首次失败/超时且有匹配后续 attempt 的任务数；`recovered` 为其中
  后续至少一次成功的数量。恢复不能证明是偶发故障或环境问题。
- 失败 step 统计来自首次 attempt，包括成功 job 内 continue-on-error 的失败 step。
  耗时 P50/P95 使用有起止时间的首次已完成 job，也包含失败和取消，不包含排队时间。
- `complete` 表示请求窗口已采集且没有已知遗漏。API 错误、分页变化、请求预算耗尽或
  查询窗口超过采集范围都会标记不完整，同时保留已知数据。`stale` 表示采集开始时间距今
  超过 30 分钟。客户端完整且新鲜退出 0，不完整或陈旧退出 1，其它错误退出 2。
- GitHub 不提供事务快照，采集后发生的重跑可能下一轮才出现；已删除或过期的历史数据
  无法靠这些接口恢复。第一版保留元数据，不保存完整日志、不自动判断根因、不自动重跑。

参考：[运行查询与上限](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository)、
[按 attempt 查询 job](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)、
[API 限流](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)。

## 客户端显示（0.5.2）

文本输出使用内置的 Rich 边框表格，数值右对齐；省略 workflow 路径前缀，长 job 名称按终端宽度换行，
不截掉 matrix 参数。窄终端为每个 job 单独显示指标表格。失败步骤集中在表格下方，JSON 格式不变。
名称中的方括号按原文显示；重定向输出时不会自动添加颜色，也可用 `NO_COLOR=1`
关闭终端颜色。独立客户端已包含 Rich 及其依赖，用户无需额外安装。
失败率列：当前展示结果中最高的非零失败率标红（包括并列），其它非零值标黄，
零失败率标绿，无有效样本标灰色 `-`。如果全部为零则全部标绿；最高值仅针对
当前展示的分组，不包括未展示的结果。
尚无已完成采集时显示部分结果提示，不再显示 `Last sync: None`。只需更新客户端，
服务端 0.5.0 可以继续使用，不需要重启或重新登录。
