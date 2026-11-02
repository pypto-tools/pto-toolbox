# 独立客户端发布与更新

[English](client-releases.md) | 中文

从 [GitHub Releases](https://github.com/pypto-tools/pto-toolbox/releases) 获取安装包。
提供 Linux ARM64（`aarch64`）和 x86_64 两种客户端，包含 Python 和运行依赖，
要求 glibc 2.28 以上，不支持 Alpine/musl。首次安装器只需要 Python 3.9 以上标准库。
客户端机器无需 pip、编译器、源码仓库、Docker，也不用替换系统 Python。

独立包包含客户端、更新器、运行环境及依赖许可证，不包含服务端、管理工具及服务端依赖。
同时发布单独的服务 wheel，供服务管理员或源码部署使用。更新客户端不会升级或重启服务端。
已有 App 模式服务可以继续使用；官方别名需要服务端 0.3.2 以上。

## 首次全机安装

每台机器由管理员安装一次。从官方仓库下载并检查安装器，再以 root 执行。
安装器和目标包使用同一个版本：

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  https://github.com/pypto-tools/pto-toolbox/releases/download/pr-rerun-v0.5.2/install-client.py \
  -o install-client.py
python3 install-client.py --dry-run
sudo python3 install-client.py --version 0.5.2
pr-rerun --version
```

如果机器使用本地 HTTP/混合代理，两次下载都需要指定代理：

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  --proxy http://127.0.0.1:7892 \
  https://github.com/pypto-tools/pto-toolbox/releases/download/pr-rerun-v0.5.2/install-client.py \
  -o install-client.py
sudo python3 install-client.py --version 0.5.2 --proxy http://127.0.0.1:7892
```

安装器自动选择架构，通过 HTTPS 下载 `SHA256SUMS` 和安装包，校验摘要、平台兼容性，
并运行版本及帮助自检。通过后创建 `/usr/local/bin/pr-rerun` 和
`/usr/local/bin/pr-rerun-update`，运行文件保存在 root 所有的 `/opt/pr-rerun` 中。
遇到不属于本工具的现有命令会停止。可用 `command -v pr-rerun` 检查是否被旧个人版或
Conda 命令覆盖；完整路径 `/usr/local/bin/pr-rerun` 始终指向共享版。

每个用户使用自己的普通系统账户，以自己的 GitHub 身份登录，不加 sudo：

```bash
# 将示例 NetBird 地址替换为实际服务地址。
# pypto 是示例官方别名，需要服务管理员提前配置。
pr-rerun login --netbird-server http://100.64.0.10:8080 --repo pypto
pr-rerun 123
```

登录信息和个人别名仍保存在各用户自己的 `~/.config/pr-rerun` 或 `XDG_CONFIG_HOME` 下。
安装器不会读取或迁移这些文件。

## 更新和回退

```bash
sudo pr-rerun-update                         # 最新已发布的客户端
sudo pr-rerun-update --version 0.5.2         # 指定版本
sudo pr-rerun-update --proxy http://127.0.0.1:7892
sudo pr-rerun-update --rollback              # 上一个已安装的独立版本
pr-rerun --version
```

更新由管理员主动执行，普通 rerun 命令不会自行更新。安装锁阻止并发操作，每个版本使用独立
目录，验证通过后才切换 `current` 链接；切换阶段失败会恢复旧链接。旧版本保留用于正在运行
的进程和回退。更新器拒绝自动降级，也拒绝同一版本号对应不同摘要的包。
重复安装同一包不会破坏回退目标。升级无需重新登录，除非服务端会话已经过期或撤销。

`--rollback` 需要之前安装过另一版独立客户端，不会回退服务端或数据库。
如果使用自定义目录，每次安装、更新及回退都应传入相同的 `--prefix` 和 `--bin-dir`。

## 更新成功，但命令仍是旧版

`sudo pr-rerun-update` 只更新共享版，不会修改每个人的 PATH、Conda 环境或个人启动脚本。
出现 `pr-rerun repos` 报 `Specify a positive PR number`，或 `--version` 不被识别时，
先在**出错的同一个终端和环境**检查：

```bash
type -a pr-rerun
command -v pr-rerun
pr-rerun --version
/usr/local/bin/pr-rerun --version
```

共享版为 0.4.1、普通命令仍旧时，就是命令优先级问题。可先用完整路径验证：

```bash
/usr/local/bin/pr-rerun repos
```

若确认优先命中的是自己不再需要的 `~/.local/bin/pr-rerun` 旧启动文件，
可备份后链接到共享版；下面只适用于该个人路径，由普通用户执行，不加 sudo：

```bash
# 先确认上面的路径与版本输出；备份目标已存在时，请另选备份名称。
if [ ! -e "$HOME/.local/bin/pr-rerun.before-shared" ] && \
   [ ! -L "$HOME/.local/bin/pr-rerun.before-shared" ]; then
  mv "$HOME/.local/bin/pr-rerun" "$HOME/.local/bin/pr-rerun.before-shared" &&
    ln -s /usr/local/bin/pr-rerun "$HOME/.local/bin/pr-rerun"
fi
hash -r
pr-rerun --version
pr-rerun repos
```

不要对 Conda/venv 内的其它路径照搬移动命令。若它们优先，可在当前终端先执行
`export PATH="/usr/local/bin:$PATH"` 和 `hash -r`，验证后再决定是否调整 shell 初始化配置；
重新激活环境可能再次改变顺序。shell alias/function 需按 `type -a` 的结果单独调整。
无需删除 `~/.config/pr-rerun` 或重新登录。每个有个人旧版的系统用户都要检查自己的入口。

`repos` 还要求服务端为 App 模式 0.4.1；若完整路径也提示服务端版本不足，请由管理员
按[运维指南](operations.zh-CN.md)升级服务端，重复更新客户端不能解决。

## 从旧共享 wheel 安装迁移

```bash
sudo python3 install-client.py --version 0.5.2 --migrate-existing
```

这个选项只替换与 `deploy/pr-rerun-client` 完全一致的旧启动脚本，先备份到
`/opt/pr-rerun/legacy-pr-rerun`，保留旧 `/opt/pr-rerun-client` 环境及用户登录文件。
首次迁移没有可回退的旧独立包，如需恢复原启动脚本：

```bash
sudo install -m 0755 /opt/pr-rerun/legacy-pr-rerun /usr/local/bin/pr-rerun.legacy-restore
sudo mv -Tf /usr/local/bin/pr-rerun.legacy-restore /usr/local/bin/pr-rerun
```

## 离线安装与更新

从同一个可信 Release 下载以下三个文件，传到目标机器，保留安装包原文件名：

- `install-client.py`
- `SHA256SUMS`
- `pr-rerun-client-linux-aarch64.tar.gz` 或 `pr-rerun-client-linux-x86_64.tar.gz`

```bash
sudo python3 install-client.py \
  --bundle pr-rerun-client-linux-aarch64.tar.gz --checksums SHA256SUMS
# 后续离线更新可直接使用已安装的更新器：
sudo pr-rerun-update \
  --bundle pr-rerun-client-linux-aarch64.tar.gz --checksums SHA256SUMS
```

摘要校验用于检测文件损坏，不能替代发布者身份验证：安装器和摘要文件应通过可信连接从官方
GitHub Release 获取。离线安装不会访问 GitHub、PyPI 或 rerun 服务。

## 维护者发布流程

1. 更新 `src/pr_rerun/version.py`、`pyproject.toml`、旧安装器中的版本，以及
   `docs/releases/` 下的中英文发布说明。
2. 推送代码。`PR rerun release` 工作流测试服务端和安装器，使用原生架构的
   `manylinux_2_28` 容器及 uv 管理的共享版 CPython 3.12.16 构建两种客户端
   （镜像自带 Python 是静态版，无法用于打包），在容器中用 Python 3.9
   验证安装，并在宿主 runner 再次执行安装包。
3. 为验证通过的提交创建并推送 `pr-rerun-vX.Y.Z` 标签。全部任务通过后才发布两种客户端、
   服务 wheel、`install-client.py` 和 `SHA256SUMS`。标签必须与包内版本一致。

构建只复制白名单中的客户端源码，使用隔离的暂存目录，不复制服务器状态和机器配置。
构建依赖固定在 `release/requirements-build.lock`，运行依赖、Python 版本、源码提交记录在
包内 `release.json` 中。容器标签和 GitHub Action 主版本标签可能更新，不承诺二进制逐字节可重复。

仓库的 latest release 应保留给最新稳定 `pr-rerun-v*` 标签，其他工具发布不能替换它。
用户也可以通过 `--version` 固定版本。
