# netbird-preflight

在动手安装之前，检查一台机器能否接入自建 NetBird 网络。

[English](README.md) | 中文

## 作用

NetBird 装不上，原因很少出在 NetBird 本身：可能是出方向端口被封、时钟漂移到证书
有效期之外、缺 CA 证书库、容器里没有 `/dev/net/tun`，或者已有另一个 VPN 占着
`100.64.0.0/10` 网段。这些问题都会在你**已经装完守护进程、`/etc/resolv.conf`
也被改写之后**，才以一条难以理解的报错浮现出来。

这个脚本把它们全部前置检查一遍。**纯只读**——只建连接、只读系统状态，不改任何东西。

## 检查哪些项

**1. 系统环境**——CPU 架构是否有官方预编译包；发行版；内核版本（≥5.6 内建
WireGuard）；包管理器；安装权限（root / 免密 sudo / 需密码的 sudo / 都没有）；
init 是否为 systemd；`curl`、`ip`、`python3` 是否存在。

**2. DNS、时钟与证书信任链**——管理端域名的解析，解析不了时回退到公共 DNS，
以便区分「本机解析器坏了」和「服务端不可达」；`/etc/resolv.conf` 是否已被 NetBird
接管、以及那个解析器是否还在应答；系统 CA 证书库；以及用服务端 `Date` 响应头测出的
时钟偏差——TLS 和登录令牌都依赖它。

**3. 到 NetBird 服务端的连通性**——管理端口的 TCP；HTTPS 控制台；TLS 证书校验
（这里失败通常意味着有中间人代理）；gRPC 必需的 ALPN `h2`；Management API；
Relay 端点；以及 P2P 打洞需要的 UDP 3478 STUN。

**4. WireGuard 数据面**——内核模块是否已加载、可加载或内建；`/dev/net/tun`；
`NET_ADMIN` 能力；WireGuard 的 UDP 端口是否已被占用。

**5. 与现有环境的冲突**——是否已装过 NetBird、是否残留 `wt0` 网卡、
是否有其它 VPN 守护进程在跑、NetBird 的 CGNAT 网段是否已被别的东西占用。

## 用法

```bash
./netbird-preflight.sh                      # 检查默认服务端
./netbird-preflight.sh -s host:port         # 指定管理端地址
./netbird-preflight.sh --stun-port 3478     # 指定 STUN 端口
./netbird-preflight.sh --wg-port 51820      # 指定 WireGuard 端口
./netbird-preflight.sh -h                   # 帮助
```

| 参数 | 含义 |
| ---- | ---- |
| `-s`, `--server <host[:port]>` | 管理端地址。默认为内置服务端，端口 443。 |
| `--stun-port <port>` | 探测 STUN 的 UDP 端口，默认 3478。 |
| `--wg-port <port>` | 检查占用情况的本机 UDP 端口，默认 51820。 |
| `-h`, `--help` | 用法。 |

## 退出码

| 码 | 含义 |
| -- | ---- |
| `0` | 可以安装。可能仍有警告，会在结尾列出。 |
| `1` | 至少有一项阻塞失败，结尾会重复列出所有阻塞项。 |
| `2` | 命令行参数错误。 |

适合用来给批量部署把关：在一批机器上跑一遍，只在退出码为 0 的机器上安装。

## 看懂输出

| 标记 | 含义 |
| ---- | ---- |
| `✔` | 通过。 |
| `!` | 警告——可以装，但要清楚后果（例如 UDP 被封意味着流量退回中继，能用但更慢）。 |
| `✘` | 阻塞——不解决就装不成。 |
| `-` | 因前置检查未通过而跳过。 |

依赖同一条连接的检查会只报一次然后跳过，所以一个端口不通只会出现一行红色，
而不是六行。

## 常见失败与处理

| 现象 | 处理 |
| ---- | ---- |
| 管理端口 TCP 不可达 | 出方向防火墙拦截。不打通的话 NetBird 完全无法工作。 |
| TLS 证书校验失败 | 系统时钟、缺 CA 证书库，或存在 TLS 拦截代理。 |
| ALPN 协商到 HTTP/1.1 而非 h2 | 几乎必然是拦截代理；到管理端的 gRPC 会失败。 |
| STUN 无响应 | 出方向 UDP 被封。不致命——对端会退回走管理端口中继，代价是时延。 |
| 时钟偏差超过 300 秒 | 开启 NTP/chronyd，否则 TLS 与登录令牌都会被拒。 |
| 缺 `/dev/net/tun` | 容器加 `--device /dev/net/tun`；物理机执行 `modprobe tun`。 |
| 缺 `NET_ADMIN` | 容器加 `--cap-add NET_ADMIN`。 |
| CGNAT 网段已被路由占用 | 另一个覆盖网正在使用 `100.64.0.0/10`，地址会冲突。 |
| NetBird 自己的 DNS 解析器不应答 | NetBird 会改写 `/etc/resolv.conf`；它的解析器一挂，**该机所有用户**的 DNS 全部失效。用 `netbird up --disable-dns` 或恢复 `/etc/resolv.conf.original.netbird`。 |

## 通过之后

脚本会按你检查的那个地址打印安装命令：

```bash
curl -fsSL https://pkgs.netbird.io/install.sh | sh
netbird up --management-url <BASE> --setup-key <SETUP_KEY>
```

SETUP_KEY 在控制台的 Setup Keys 页面创建。装完用 `netbird status -d` 确认
Management 和 Signal 都是 `Connected`。
