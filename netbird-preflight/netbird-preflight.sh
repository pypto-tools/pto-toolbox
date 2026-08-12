#!/usr/bin/env bash
# netbird-preflight.sh — check whether this host can join the self-hosted
# NetBird network at netbird.pypto.cloud:443.
#
# Usage:
#   ./netbird-preflight.sh                       # check the default server
#   ./netbird-preflight.sh -s host:port          # override management endpoint
#   ./netbird-preflight.sh --stun-port 3478      # override STUN port
#
# Exit code: 0 = ready (warnings allowed), 1 = at least one blocking failure.

set -uo pipefail

SERVER_HOST="netbird.pypto.cloud"
SERVER_PORT="443"
STUN_PORT="3478"
WG_PORT="51820"
NB_CIDR="100.64.0.0/10"   # NetBird / CGNAT range used by the overlay

while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--server)     SERVER_HOST="${2%%:*}"; [[ "$2" == *:* ]] && SERVER_PORT="${2##*:}"; shift 2 ;;
    --stun-port)     STUN_PORT="$2"; shift 2 ;;
    --wg-port)       WG_PORT="$2"; shift 2 ;;
    -h|--help)       sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

BASE="https://${SERVER_HOST}:${SERVER_PORT}"

if [[ -t 1 ]]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; D=$'\e[2m'; N=$'\e[0m'
else
  R=; G=; Y=; B=; D=; N=
fi

n_pass=0; n_warn=0; n_fail=0
declare -a FAILURES=() WARNINGS=()

LABEL_W=24   # label column width, in *display columns* (not bytes)

# printf '%-Ns' pads by bytes, but a CJK glyph is 3 bytes wide and only 2
# columns wide — mixing Chinese and ASCII labels then misaligns the value
# column. Derive the true width instead: for a string of ASCII (1 byte,
# 1 column) and CJK (3 bytes, 2 columns), width == (chars + bytes) / 2.
label() {
  local s="$1" chars bytes pad
  chars=${#s}
  local LC_ALL=C
  bytes=${#s}
  pad=$(( LABEL_W - (chars + bytes) / 2 ))
  (( pad < 0 )) && pad=0
  printf '%s%*s' "$s" "$pad" ''
}

pass() { printf '  %s✔%s %s  %s\n' "$G" "$N" "$(label "$1")" "${2-}"; ((n_pass++)); }
warn() { printf '  %s!%s %s  %s\n' "$Y" "$N" "$(label "$1")" "${2-}"; ((n_warn++)); WARNINGS+=("$1 — ${2-}"); }
fail() { printf '  %s✘%s %s  %s\n' "$R" "$N" "$(label "$1")" "${2-}"; ((n_fail++)); FAILURES+=("$1 — ${2-}"); }
info() { printf '  %s·%s %s  %s\n' "$D" "$N" "$(label "$1")" "${2-}"; }
skip() { printf '  %s-%s %s  %s\n' "$D" "$N" "$(label "$1")" "${2:-跳过（前置检查未通过）}"; }
section() { printf '\n%s%s%s\n' "$B" "$1" "$N"; }

have() { command -v "$1" >/dev/null 2>&1; }

# Resolved address of the management server; empty when DNS failed entirely.
TARGET_IP=""

# curl that ignores any HTTP proxy (the netbird daemon never uses one) and
# pins the hostname to TARGET_IP so a broken system resolver does not mask a
# perfectly reachable server. SNI/cert validation still uses the real hostname.
nbcurl() {
  local -a extra=()
  [[ -n "$TARGET_IP" ]] && extra=(--resolve "${SERVER_HOST}:${SERVER_PORT}:${TARGET_IP}")
  curl -sS --noproxy '*' --max-time 10 "${extra[@]}" "$@"
}

printf '%sNetBird preflight%s  target=%s  host=%s  %s\n' \
  "$B" "$N" "$BASE" "$(hostname)" "$(date '+%F %T %Z')"

# ---------------------------------------------------------------- 1. system
section "1. 系统环境"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64|aarch64|arm64|armv7l|armv6l|i386|i686)
    pass "CPU 架构" "$ARCH (官方有预编译包)" ;;
  *) fail "CPU 架构" "$ARCH 无官方 netbird 包，需自行编译" ;;
esac

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  pass "操作系统" "${PRETTY_NAME:-$ID $VERSION_ID}"
else
  warn "操作系统" "/etc/os-release 缺失，无法识别发行版"
fi

KREL="$(uname -r)"
KMAJ="${KREL%%.*}"; KMIN="${KREL#*.}"; KMIN="${KMIN%%.*}"
if [[ "$KMAJ" -gt 5 || ( "$KMAJ" -eq 5 && "$KMIN" -ge 6 ) ]]; then
  pass "内核版本" "$KREL (>=5.6，内建 WireGuard)"
else
  warn "内核版本" "$KREL (<5.6，需 wireguard 内核模块或走 userspace)"
fi

PKGMGR=""
for m in dnf yum apt-get apk zypper pacman; do have "$m" && { PKGMGR="$m"; break; }; done
if [[ -n "$PKGMGR" ]]; then
  pass "包管理器" "$PKGMGR"
else
  warn "包管理器" "未识别，只能用二进制 tarball 安装"
fi

if [[ "$(id -u)" -eq 0 ]]; then
  pass "安装权限" "当前是 root"
elif sudo -n true 2>/dev/null; then
  pass "安装权限" "sudo 免密可用"
elif have sudo; then
  warn "安装权限" "有 sudo 但需要密码，安装时需交互输入"
else
  fail "安装权限" "非 root 且无 sudo，无法安装/启动 netbird 服务"
fi

if have systemctl && [[ -d /run/systemd/system ]]; then
  pass "init 系统" "systemd (netbird 以 systemd 服务运行)"
else
  warn "init 系统" "非 systemd，需手动托管 netbird 守护进程"
fi

missing=()
for t in curl ip; do have "$t" || missing+=("$t"); done
if [[ ${#missing[@]} -eq 0 ]]; then
  pass "必需工具" "curl, ip 均存在"
else
  fail "必需工具" "缺少: ${missing[*]}"
fi
have python3 || warn "python3" "缺失，STUN(UDP 3478) 检测将跳过"

# ------------------------------------------------------- 2. DNS / clock / CA
section "2. DNS、时钟与证书信任链"

SYS_IP=""
if have getent; then SYS_IP="$(getent ahostsv4 "$SERVER_HOST" 2>/dev/null | awk 'NR==1{print $1}')"; fi
if [[ -z "$SYS_IP" ]] && have python3; then
  SYS_IP="$(python3 -c "import socket,sys
try: print(socket.gethostbyname(sys.argv[1]))
except Exception: pass" "$SERVER_HOST" 2>/dev/null)"
fi

# Fallback: ask public resolvers directly, so we can tell "本机 DNS 坏了"
# apart from "服务端/网络不通" instead of cascading one failure into six.
ALT_IP=""
if [[ -z "$SYS_IP" ]]; then
  for ns in 1.1.1.1 8.8.8.8 223.5.5.5; do
    if have dig; then
      ALT_IP="$(timeout 5 dig +short +time=2 +tries=1 "@$ns" "$SERVER_HOST" A 2>/dev/null | grep -m1 -E '^[0-9.]+$')"
    elif have nslookup; then
      ALT_IP="$(timeout 5 nslookup "$SERVER_HOST" "$ns" 2>/dev/null | awk '/^Address: /{print $2; exit}')"
    fi
    [[ -n "$ALT_IP" ]] && break
  done
fi

if [[ -n "$SYS_IP" ]]; then
  TARGET_IP="$SYS_IP"
  pass "DNS 解析" "$SERVER_HOST -> $SYS_IP"
elif [[ -n "$ALT_IP" ]]; then
  TARGET_IP="$ALT_IP"
  fail "DNS 解析" "系统解析器解析不了，但公共 DNS 可解析到 $ALT_IP —— 修 /etc/resolv.conf 或写入 /etc/hosts"
  info "" "后续检查改用 $ALT_IP 继续，以便区分是 DNS 问题还是网络问题"
else
  fail "DNS 解析" "任何解析器都解析不出 $SERVER_HOST，检查域名与出网 DNS"
fi

# NetBird 会改写 /etc/resolv.conf 指向自己的 overlay 解析器。若该解析器不应答，
# 全机所有用户的 DNS 都会挂掉（只有 /etc/hosts 和走代理的程序还能用）——实测踩过。
if grep -qi 'Generated by NetBird' /etc/resolv.conf 2>/dev/null; then
  NB_NS="$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf)"
  info "resolv.conf" "已被 NetBird 接管，nameserver=$NB_NS"
  if timeout 6 python3 -c "
import socket,sys
try: socket.gethostbyname('example.com')
except Exception: sys.exit(1)
" 2>/dev/null; then
    pass "NetBird DNS 解析器" "$NB_NS 工作正常"
  else
    fail "NetBird DNS 解析器" "$NB_NS 不应答，本机 DNS 已全局失效（影响所有用户）；\
用 'netbird up --disable-dns' 或恢复 /etc/resolv.conf.original.netbird"
  fi
fi

CA_OK=0
for f in /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem; do
  [[ -s "$f" ]] && { CA_OK=1; pass "系统 CA 证书库" "$f"; break; }
done
[[ $CA_OK -eq 1 ]] || fail "系统 CA 证书库" "未找到，Let's Encrypt 证书将校验失败(安装 ca-certificates)"

# ------------------------------------------------------------- 3. 网络连通性
section "3. 到 NetBird 服务端的连通性"

tcp_open() { timeout 5 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

TCP_OK=0
if [[ -z "$TARGET_IP" ]]; then
  skip "TCP ${SERVER_PORT} 连通性" "跳过（域名无法解析）"
elif tcp_open "$TARGET_IP" "$SERVER_PORT"; then
  TCP_OK=1
  pass "TCP ${SERVER_PORT} 可达" "management / signal / relay 共用此端口"
else
  fail "TCP ${SERVER_PORT} 不可达" "出方向被防火墙拦截，netbird 完全无法工作"
fi

# Everything below rides on the same TCP connection — report it once, then
# skip rather than emit five more red lines for one root cause.
if [[ $TCP_OK -eq 0 ]]; then
  skip "HTTPS 控制台"
  skip "TLS 证书校验"
  skip "HTTP/2 (ALPN h2)"
  skip "Management API"
  skip "Relay 端点"
  skip "系统时钟偏差"
else
  HTTP_INFO="$(nbcurl -o /dev/null -w '%{http_code} %{ssl_verify_result} %{http_version}' "$BASE/" 2>/dev/null)"
  read -r HCODE HVERIFY HVER <<<"${HTTP_INFO:-000 99 0}"
  if [[ "$HCODE" == "200" ]]; then
    pass "HTTPS 控制台" "$BASE/ -> 200"
  elif [[ "$HCODE" == "000" ]]; then
    fail "HTTPS 控制台" "TCP 通但 HTTPS 无响应，多为 TLS 握手被拦截"
  else
    warn "HTTPS 控制台" "返回 $HCODE（服务端可能在升级）"
  fi

  if [[ "$HCODE" == "000" ]]; then
    skip "TLS 证书校验" "跳过（未建立 TLS 连接）"
    skip "HTTP/2 (ALPN h2)" "跳过（未建立 TLS 连接）"
  else
    if [[ "$HVERIFY" == "0" ]]; then
      pass "TLS 证书校验" "通过（无中间人代理）"
    else
      fail "TLS 证书校验" "ssl_verify_result=$HVERIFY，检查系统时间 / CA 证书库 / TLS 拦截代理"
    fi
    if [[ "$HVER" == "2" ]]; then
      pass "HTTP/2 (ALPN h2)" "可用 — management 走 gRPC，必需"
    else
      fail "HTTP/2 (ALPN h2)" "协商到 HTTP/$HVER，gRPC 会失败（多为 TLS 拦截代理所致）"
    fi
  fi

  API_CODE="$(nbcurl -o /dev/null -w '%{http_code}' "$BASE/api/users" 2>/dev/null)"
  if [[ "$API_CODE" == "401" || "$API_CODE" == "403" ]]; then
    pass "Management API" "/api/users -> $API_CODE（服务存活，未认证符合预期）"
  elif [[ "$API_CODE" == "000" ]]; then
    fail "Management API" "无响应"
  else
    warn "Management API" "返回 $API_CODE（预期 401）"
  fi

  RELAY_CODE="$(nbcurl -o /dev/null -w '%{http_code}' "$BASE/relay" 2>/dev/null)"
  if [[ "$RELAY_CODE" == "426" || "$RELAY_CODE" == "400" ]]; then
    pass "Relay 端点" "/relay -> $RELAY_CODE（等待 WebSocket 升级，正常）"
  elif [[ "$RELAY_CODE" == "000" ]]; then
    fail "Relay 端点" "无响应，P2P 打洞失败时将无中继可用"
  else
    warn "Relay 端点" "返回 $RELAY_CODE（预期 426）"
  fi
fi

# 时钟偏差 —— TLS 与 JWT 都依赖
SRV_DATE=""
[[ $TCP_OK -eq 1 ]] && SRV_DATE="$(nbcurl -sI "$BASE/" 2>/dev/null | awk 'tolower($1)=="date:"{sub(/^[Dd]ate: /,""); print; exit}' | tr -d '\r')"
if [[ -n "$SRV_DATE" ]]; then
  s_epoch="$(date -d "$SRV_DATE" +%s 2>/dev/null)"
  l_epoch="$(date +%s)"
  if [[ -n "$s_epoch" ]]; then
    skew=$(( l_epoch - s_epoch )); skew=${skew#-}
    if   [[ $skew -le 30  ]]; then pass "系统时钟偏差" "${skew}s"
    elif [[ $skew -le 300 ]]; then warn "系统时钟偏差" "${skew}s，建议开启 NTP/chronyd"
    else                           fail "系统时钟偏差" "${skew}s，TLS 与登录令牌会失效，必须校时"
    fi
  fi
elif [[ $TCP_OK -eq 1 ]]; then
  info "系统时钟偏差" "未取到服务端 Date，跳过"
fi

# STUN (UDP 3478) —— NAT 发现 / P2P 打洞
if [[ -z "$TARGET_IP" ]]; then
  skip "STUN UDP ${STUN_PORT}" "跳过（域名无法解析）"
elif have python3; then
  STUN_OUT="$(python3 - "$TARGET_IP" "$STUN_PORT" <<'PY' 2>/dev/null
import socket, os, struct, sys
host, port = sys.argv[1], int(sys.argv[2])
msg = b'\x00\x01\x00\x00\x21\x12\xa4\x42' + os.urandom(12)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(6)
try:
    s.sendto(msg, (host, port))
    d, _ = s.recvfrom(2048)
except Exception:
    sys.exit(1)
if len(d) < 20 or d[0:2] != b'\x01\x01':
    sys.exit(1)
i, n = 20, struct.unpack('>H', d[2:4])[0]
while i < 20 + n:
    t, l = struct.unpack('>HH', d[i:i+4])
    if t == 0x0020:
        p = struct.unpack('>H', d[i+6:i+8])[0] ^ 0x2112
        ip = bytes(a ^ b for a, b in zip(d[i+8:i+12], b'\x21\x12\xa4\x42'))
        print(f"{socket.inet_ntoa(ip)}:{p}")
        sys.exit(0)
    i += 4 + l + ((4 - l % 4) % 4)
sys.exit(1)
PY
)"
  if [[ -n "$STUN_OUT" ]]; then
    pass "STUN UDP ${STUN_PORT}" "公网映射 $STUN_OUT"
  else
    warn "STUN UDP ${STUN_PORT}" "无响应：UDP 出方向被封，P2P 打洞会失败，只能走 ${SERVER_PORT} 中继（可用但更慢）"
  fi
else
  info "STUN UDP ${STUN_PORT}" "无 python3，跳过"
fi

if [[ -n "${HTTPS_PROXY:-${https_proxy:-}}" ]]; then
  warn "HTTP 代理环境变量" "已设置 (${HTTPS_PROXY:-$https_proxy})；netbird 守护进程不走代理，请确认可直连出网"
fi

# ------------------------------------------------------- 4. WireGuard 能力
section "4. WireGuard 数据面能力"

if lsmod 2>/dev/null | grep -q '^wireguard'; then
  pass "WireGuard 模式" "内核模块已加载（kernel 模式，性能最佳）"
elif modinfo wireguard >/dev/null 2>&1; then
  pass "WireGuard 模式" "内核模块可加载（首次启动时自动 modprobe）"
elif [[ "$KMAJ" -gt 5 || ( "$KMAJ" -eq 5 && "$KMIN" -ge 6 ) ]]; then
  pass "WireGuard 模式" "内核 $KREL 内建支持"
else
  warn "WireGuard 模式" "无内核支持，将回退 userspace（性能下降，且强依赖 /dev/net/tun）"
fi

if [[ -c /dev/net/tun ]]; then
  pass "/dev/net/tun" "存在"
else
  fail "/dev/net/tun" "缺失；容器请加 --device /dev/net/tun，物理机执行 modprobe tun"
fi

if [[ -r /proc/self/status ]] && grep -q '^CapEff' /proc/self/status; then
  capeff="$(awk '/^CapEff/{print $2}' /proc/self/status)"
  if [[ "$(id -u)" -eq 0 ]]; then
    # NET_ADMIN = bit 12 (0x1000)
    if (( 0x${capeff: -8} & 0x1000 )); then
      pass "NET_ADMIN 能力" "具备（可创建 wt0 网卡）"
    else
      fail "NET_ADMIN 能力" "缺失；容器请加 --cap-add NET_ADMIN"
    fi
  else
    info "NET_ADMIN 能力" "非 root 无法判定，安装时以 root 运行即可"
  fi
fi

if have ss; then
  if ss -lun 2>/dev/null | awk '{print $5}' | grep -qE "[:.]${WG_PORT}\$"; then
    warn "本机 UDP ${WG_PORT}" "已被占用，netbird 会自动改用其它端口"
  else
    pass "本机 UDP ${WG_PORT}" "空闲"
  fi
fi

# -------------------------------------------------------------- 5. 冲突检查
section "5. 已有环境冲突"

if have netbird; then
  warn "已安装 netbird" "$(netbird version 2>/dev/null || echo '版本未知')；重复安装前先 netbird down / 卸载"
else
  pass "已安装 netbird" "未安装，可全新安装"
fi

if ip link show wt0 >/dev/null 2>&1; then
  warn "wt0 网卡" "已存在，说明本机已入网或存在残留"
else
  pass "wt0 网卡" "不存在"
fi

for vpn in tailscaled zerotier-one openvpn; do
  if pgrep -x "$vpn" >/dev/null 2>&1; then
    warn "其它 VPN 在运行" "$vpn；可能与 NetBird 的路由/DNS 抢占冲突"
  fi
done

if ip route show 2>/dev/null | grep -qE '(^|[^0-9.])100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\.'; then
  if ip link show wt0 >/dev/null 2>&1; then
    info "地址段 $NB_CIDR" "现有路由来自 wt0，正常"
  else
    fail "地址段 $NB_CIDR" "已被其它网络占用，会与 NetBird 覆盖网地址冲突"
  fi
else
  pass "地址段 $NB_CIDR" "未被占用"
fi

# ------------------------------------------------------------------ summary
section "结论"
printf '  通过 %s%d%s   警告 %s%d%s   失败 %s%d%s\n' "$G" "$n_pass" "$N" "$Y" "$n_warn" "$N" "$R" "$n_fail" "$N"

if [[ $n_fail -gt 0 ]]; then
  printf '\n  %s必须先解决以下阻塞项：%s\n' "$R" "$N"
  for f in "${FAILURES[@]}"; do printf '   - %s\n' "$f"; done
  exit 1
fi

if [[ $n_warn -gt 0 ]]; then
  printf '\n  %s可以安装，但请留意：%s\n' "$Y" "$N"
  for w in "${WARNINGS[@]}"; do printf '   - %s\n' "$w"; done
fi

cat <<EOF

  ${G}本机满足接入条件。${N}安装步骤：

    curl -fsSL https://pkgs.netbird.io/install.sh | sh
    netbird up --management-url ${BASE} --setup-key <SETUP_KEY>

  SETUP_KEY 在 ${BASE}/ 控制台的 Setup Keys 页面创建。
  安装后用 \`netbird status -d\` 确认 Management / Signal 均为 Connected。
EOF
exit 0
