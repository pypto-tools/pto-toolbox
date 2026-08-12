# deploy-git-mirror

把 CI runner 上对某个仓库的反复网络克隆，变成同盘硬链接克隆（~20ms），
并由定时任务保持本地副本新鲜。

[English](README.md) | 中文

## 解决什么问题

有些 CI 任务每次跑都要克隆一个依赖仓——固定版本的工具链、生成的头文件仓、
ISA 定义。在出网链路不稳的自托管 runner 上，这是一类反复出现的故障：

```text
error: RPC failed; curl 56 GnuTLS recv error (-9): Error decoding the received TLS packet.
fetch-pack: unexpected disconnect while reading sideband packet
fatal: early EOF
```

重试没用，而且这类仓通常根本不大——问题在链路，不在数据量。

## 原理

工具在 runner 上放一份该仓的 **bare 镜像**，并为这个仓的 URL 添加
`url.<镜像>.insteadOf` 规则。git 在建立连接之前就完成改写，于是 CI 里那条
一个字都没改的 `git clone` 落到了本地路径：

```text
CI: git clone https://github.com/<org>/<repo>.git build/<repo>
      │  ~/.gitconfig: url.<镜像>.insteadOf = 上面那条 URL
      ▼
    git clone /srv/mirrors/<repo>.git build/<repo>     # 同盘 → 硬链接，~20ms
    git checkout --detach <pin>                        # 任意 commit：镜像是 --mirror 全量

/srv/mirrors/<repo>.git        ← bare 镜像
   └── sync-<repo>.sh          ← 定时执行 git remote update --prune
```

由于克隆目标和镜像在同一个文件系统上，git 会硬链接 pack 文件：克隆不额外占盘，
也没有任何字节走网络。

CI 配置完全不用改。回滚就是一条 `git config --unset-all`。

## 前置条件

- `git`、`flock`（可选但建议），以及一个调度器——见[选择调度器](#4--选择调度器)。
- **运行本工具的那个账号**对 git 主机有可用的认证，且主机公钥已在 `known_hosts` 里
  （定时任务没有 TTY，无法回答指纹确认）。
- 一个与 runner 工作目录**在同一文件系统**上的镜像目录。不同文件系统仍然能避开网络，
  但硬链接会退化成完整复制。

## 快速开始

```bash
./deploy-git-mirror.sh \
  --repo <org>/<repo> \
  --mirror-root /srv/mirrors \
  --check-path /srv/runner \
  --interval 2

./deploy-git-mirror.sh --verify --repo <org>/<repo> --mirror-root /srv/mirrors
```

任何时候重跑都安全：它收敛到同一状态，不会叠加出重复的配置项或 cron 行。

## 参数

| 参数 | 含义 |
| ---- | ---- |
| `--repo <org/name>` | 要镜像的仓库，可重复。也接受完整 URL。 |
| `--mirror-root <dir>` | 镜像、同步脚本、日志的落点。必须与 CI 克隆目标同一文件系统。 |
| `--check-path <dir>` | 断言该路径与 `--mirror-root` 同盘。可重复，纯只读。 |
| `--interval <min>` | 同步间隔（分钟），默认 2。需 `<60` 或为 60 的整数倍。 |
| `--scheduler <mode>` | `auto`（默认）、`user-cron`、`system-cron`、`loop`。见下。 |
| `--timeout <sec>` | 单次 fetch 超时，默认 100。 |
| `--protocol ssh\|https` | **镜像自身**如何拉取，默认 `ssh`。 |
| `--proxy <url>` | 镜像自身拉取用的 HTTPS 代理，仅 `https` 生效。 |
| `--credential-helper <cmd>` | 私有仓的凭据 helper，仅 `https` 生效；不填则从全局配置继承。 |
| `--host <hostname>` | git 主机，默认 `github.com`；GHE 改这里。 |
| `--status` | 打印当前部署状态后退出，只读。 |
| `--verify` | 实测重定向是否生效：克隆计时、硬链接、镜像新鲜度。 |
| `--uninstall` [`--purge`] | 移除重定向与调度器；`--purge` 连镜像一起删。 |
| `--no-cron` | 除调度器外全部安装。 |
| `--dry-run` | 只打印将要执行的动作。 |

## 全新服务器上的完整流程

### 1 — 摸底

```bash
echo "HOME=$HOME"                       # 往往不是 /home/<user>
command -v git flock crontab
df -h | grep -vE 'tmpfs|devtmpfs'
find "$HOME" -maxdepth 5 -type d -name '_work' 2>/dev/null | head
```

记下 runner 工作目录在哪块盘上。

### 2 — 打通 git 主机认证

三个互相独立的关卡，部署前必须全部通过。

**主机公钥**——不做这步，定时同步会失败且看不出原因：

```bash
ssh-keyscan github.com >> ~/.ssh/known_hosts
ssh-keygen -lF github.com
```

`ssh-keyscan` 失败时完全静默且退出码仍是 0，所以务必用 `ssh-keygen -lF` 回验。
把打印出的 ED25519 指纹与
[GitHub 公布的指纹](https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)
核对。如果 22 端口被封，改用 GitHub 的 443 入口：

```bash
printf '\nHost github.com\n    Hostname ssh.github.com\n    Port 443\n    User git\n' >> ~/.ssh/config
chmod 600 ~/.ssh/config
ssh-keyscan -p 443 ssh.github.com >> ~/.ssh/known_hosts   # 条目按「主机名 + 端口」记
ssh-keygen -lF '[ssh.github.com]:443'
```

这对本工具是透明的——镜像 origin 里写的仍是 `github.com`。

**一把主机认可的密钥：**

```bash
ls -l ~/.ssh/id_* || ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
timeout 20 ssh -T -o ConnectTimeout=10 git@github.com     # 期望 Hi <账号>!
```

把公钥登记到 CI bot 账号（Title 填机器名，便于单独吊销），
或者在你有仓库 admin 权限时登记成只读 deploy key。**不要在机器之间复制私钥。**

**仓库权限**——能认证不等于有授权：

```bash
timeout 60 git ls-remote git@github.com:<org>/<repo>.git HEAD
```

`Repository not found` 说明该账号没有这个仓的访问权。

> 所有联网命令都加 `timeout`。到 git 主机的连接卡死很常见，干等不算诊断。

### 3 — 定镜像落点

```bash
./deploy-git-mirror.sh --dry-run \
  --repo <org>/<repo> \
  --mirror-root <候选目录> \
  --check-path <_work 的上层目录>
```

要看到 `同盘校验 OK`。`--dry-run` 不写任何东西，候选目录可以随便试。

### 4 — 选择调度器

```bash
crontab -l 2>&1 | head -2
```

| 结果 | 用哪个 |
| ---- | ------ |
| 正常列出，或 `no crontab for user` | 默认即可（`auto` 会选 `user-cron`） |
| `You are not allowed to use this program` **且**有 sudo | `--scheduler system-cron`（先跑 `sudo -v`） |
| 被禁且没有 sudo | `--scheduler loop`——扛得住退出登录，扛不住重启 |

```bash
sudo -v      # 仅 system-cron 需要：没有缓存凭据时工具会退回打印手工命令
./deploy-git-mirror.sh \
  --repo <org>/<repo> --mirror-root <目录> --check-path <目录> \
  --interval 2 --scheduler <模式>
```

工具写完调度器会回读确认，输出 `已装并验证`。**没有这句就是没装上。**

### 5 — 验证

```bash
./deploy-git-mirror.sh --verify --repo <org>/<repo> --mirror-root <目录>
```

四项必须全过：

1. HTTPS 形式的 URL 落到本地（亚秒级）；
2. SSH 形式的 URL 落到本地（亚秒级）；
3. pack 文件是硬链接（`nlink > 1`）；
4. 镜像 HEAD 与远端一致——这一项抓的是**镜像在 fetch 自己**的静默故障。

然后等一个同步周期，确认调度器真的在触发：

```bash
./deploy-git-mirror.sh --status --mirror-root <目录>
```

## 读同步日志

`<mirror-root>/sync-<repo>.log`

- **同步脚本设计上不向终端打印任何东西**，输出全部进日志。**没反应不等于卡住。**
  想看实时进度就前台跑：
  `GIT_CONFIG_GLOBAL=/dev/null timeout 60 git -C <镜像> remote update --prune`
- **`FAIL rc=124`**——fetch 卡住被 `timeout` 砍掉。链路不稳时属正常，下一轮自愈。
- **少了某一轮**——`flock` 跳过了与上一轮重叠的执行，同样正常。

偶发 `FAIL` **不影响 CI**：克隆读的始终是本地镜像，唯一后果是刚 push 的 commit
可能还没同步过来。只有当失败占比很大时才需要处理——那时改用 443 的 SSH 入口，
或改成 `--protocol https --proxy`。

## 排错

| 现象 | 原因 |
| ---- | ---- |
| `Permission denied (publickey)` | 密钥没有登记到 git 主机账号。 |
| `Repository not found` | 认证通过了，但该账号读不了这个仓。 |
| 调度器装了却从不触发 | 用户被 `cron.allow`/`cron.deny` 禁；改用 `--scheduler system-cron`。 |
| `--status` 显示没有调度器 | 没有任何东西在刷新镜像。CI 却照常成功，很容易被忽略——**要当成真故障处理**。 |
| `--verify` 报的是秒级而非毫秒级 | 重定向没生效；检查两种 URL 形式是否都装上了。 |
| 没有硬链接 | `--mirror-root` 与克隆目标不在同一文件系统。 |
| 镜像永远不前进 | 镜像在 fetch 自己：它的 `origin` 被已安装的 `insteadOf` 规则命中了。 |

## 回滚

```bash
./deploy-git-mirror.sh --uninstall --repo <org>/<repo> --mirror-root <目录>   # 加 --purge 连镜像一起删
```

清掉重定向和三种调度后端。CI 克隆立刻恢复走网络。

## 设计要点

三个不那么显然、但工具已经替你处理掉的细节：

**重定向两种 URL 形式。** 同一个仓，有的工具用 `https://host/org/repo.git` 克隆，
有的用 `git@host:org/repo.git`，而全局可能已经存在一条
`url."git@host:".insteadOf = https://host/` 在把前者改写成后者。
工具为这两种精确形式各装一条规则；git 按最长前缀匹配，所以它们必然压过更宽泛的既有规则。
不带 `.git` 的前缀形式是**故意不装**的——`…/pypto` 会连 `…/pypto-lib.git` 一起误伤。

**镜像不能 fetch 自己。** 镜像自身的 `origin` 用第三种 URL 写法
（`ssh://git@host/org/repo.git`），已安装的规则一条都命中不了；同步时还额外加
`GIT_CONFIG_GLOBAL=/dev/null`。两者缺一，镜像就会把自己的 origin 解析成自己的路径，
什么都没拉到却永远报成功。`--verify` 会专门检查这一项。

**屏蔽全局配置的同时也屏蔽了凭据。** `GIT_CONFIG_GLOBAL=/dev/null` 在挡住 `insteadOf`
的同时也挡住了 `credential.helper`，私有仓的 HTTPS 拉取会因此认证失败。
所以 `https` 模式下工具会把凭据 helper 复制进镜像的 **local** 配置——那个环境变量影响不到它。
