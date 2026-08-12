# pto-toolbox

面向 Linux 服务器、CI runner 和共享计算环境的自包含单文件 shell 小工具集合
——那些小到不值得单独开一个仓库的工具。

[English](README.md) | 中文

## 建这个仓库的初衷

这些脚本原本散落在各台机器的 `~/` 或某个 `temp/` 目录里，带来三个问题：

- **会丢。** 机器重装、pod release 之后再 re-request，脚本就跟着没了。
- **没法复用。** 在一台 runner 上写的工具，换一台就得重新翻找、重新推导，
  或者靠复制粘贴。
- **没人 review。** 没有历史、没有 diff，也没有任何记录说明某一行到底在防哪个坑。

这里的每个工具都是**可以直接 `scp` 到一台全新机器上就能跑的单个文件**。
不需要安装、不需要运行时，除了 POSIX shell 和服务器本来就有的工具之外没有依赖。

如果一个工具需要真正的安装形态——常驻进程、`/home/pypto-tools/<tool>/` 下的配置、
`PATH` 里的 `pto-<name>` 命令——那就单独建仓，遵循
[组织工具规范](https://github.com/pypto-tools/.github)。

## 工具清单

| 工具 | 作用 |
| ---- | ---- |
| [`deploy-git-mirror`](deploy-git-mirror/README.zh-CN.md) | 让 CI 对热点仓库的 clone 走本地。在 runner 上放一份 bare 镜像，用 `url.insteadOf` 把该仓的 URL 透明重定向过去，网络克隆变成同盘硬链接克隆（~20ms）。定时任务负责保持镜像新鲜。 |
| [`netbird-preflight`](netbird-preflight/README.zh-CN.md) | 在动手安装之前，检查本机能否接入自建 NetBird 网络。纯只读：检查 CPU 架构、DNS、时钟偏差、CA 信任链、TCP/UDP 连通性、WireGuard 能力，以及与现有 VPN 的冲突。 |

## 取用某个工具

整仓克隆，或者只把需要的那一个文件拉下来：

```bash
# 整仓
git clone git@github.com:pypto-tools/pto-toolbox.git

# 单个工具（私有仓，走你的 gh 凭据）
gh api repos/pypto-tools/pto-toolbox/contents/netbird-preflight/netbird-preflight.sh \
  --jq .content | base64 -d > netbird-preflight.sh
chmod +x netbird-preflight.sh
```

每个工具都支持 `--help`。**跑任何会写入的操作之前，先读该工具自己的 README。**

## 新增工具的约定

一个工具一个目录，目录名与脚本同名：

```text
<tool-name>/
├── <tool-name>.sh      # 工具本体，自包含
├── README.md           # 英文——作用、用法、完整流程
└── README.zh-CN.md     # 中文翻译
```

本仓的工具应当做到：

- **单文件。** 不 source 兄弟文件，不内嵌第三方库。
- **支持 `-h` / `--help`**，并在其中说明每一个参数。
- **幂等。** 重复执行收敛到同一状态，而不是叠加出重复的配置项、cron 行或后台进程。
- **提供 `--dry-run`**，只要它会写自己工作目录之外的任何东西。
- **自证其言。** 如果工具声称装好了某个东西，就必须回读确认——
  「命令没报错」不算证据。
- **检查与修改分离。** 只读动作（`--status`、`--verify`）绝不改变状态。
- **失败时返回非零**，并且在做任何耗时或联网操作之前先校验参数。
- **不携带任何机密。** 不放令牌、密钥、口令，也不放超出工具本身目标范围的
  私有基础设施主机名。

文档中英双语：`README.md` 是英文且为准，`README.zh-CN.md` 与之对应。
代码、注释和提交信息一律英文；脚本面向用户的输出可以按受众用中文。

## 测试

```bash
tests/smoke.sh
```

对每个工具做语法检查，断言 `--help` 可用，断言非法参数会被拒绝、`--dry-run` 被尊重。
不联网、无副作用，任何环境（包括 CI）都可以安全执行。

## AI 助手的操作边界

见 [`skills/pto-toolbox/SKILL.md`](skills/pto-toolbox/SKILL.md)：哪些操作是只读的、
哪些会改动机器配置、哪些副作用需要人工明确确认。
