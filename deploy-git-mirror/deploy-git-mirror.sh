#!/usr/bin/env bash
# deploy-git-mirror.sh — 在自托管 runner 上部署「本地 bare 镜像 + url.insteadOf」
#
# 让 CI 里对指定仓库的 git clone 变成同盘硬链接克隆（~20ms，不走网络），
# 由 cron 定时增量同步镜像。幂等：任何时候重跑都收敛到同一状态。
#
# 用法示例：
#   ./deploy-git-mirror.sh --repo hw-native-sys/pto-isa --mirror-root /data/ci-runner/mirrors
#   ./deploy-git-mirror.sh --repo a/b --repo a/c --interval 5 --check-path /data/ci-runner
#   ./deploy-git-mirror.sh --repo a/b --protocol https --proxy http://127.0.0.1:7892
#   ./deploy-git-mirror.sh --status
#   ./deploy-git-mirror.sh --verify
#   ./deploy-git-mirror.sh --uninstall --repo a/b [--purge]
set -uo pipefail

VERSION=1.0
SELF=$(basename "$0")

# ---- 默认值 ----------------------------------------------------------------
GIT_HOST=github.com
MIRROR_ROOT=""
INTERVAL=2          # 分钟
TIMEOUT=100         # 单次 fetch 上限（秒）
PROTOCOL=ssh        # 镜像自身拉取用的协议：ssh | https
PROXY=""            # 仅 --protocol https 时生效
CRED_HELPER=""      # 仅 --protocol https 时生效；留空则从全局 gitconfig 继承
LOG_LINES=2000
SCHEDULER=auto      # auto | user-cron | system-cron | loop
ACTION=install      # install | status | verify | uninstall
PURGE=0
NO_CRON=0
DRY=0
REPOS=()
CHECK_PATHS=()

usage() {
  cat <<'EOF'
deploy-git-mirror.sh — 本地 bare 镜像 + url.insteadOf 部署器

必选
  --repo <org/name>       目标仓库，可重复；也接受完整 URL
                          （https://host/org/name.git 或 git@host:org/name.git）

路径与频率
  --mirror-root <dir>     镜像落点。必须与 runner 的 _work 在同一文件系统，
                          否则退化为复制（仍不走网络，但占盘）。
                          默认：存在 /data/ci-runner 时用 /data/ci-runner/mirrors，
                                否则 $HOME/git-mirrors
  --interval <min>        同步间隔，分钟。默认 2。>=60 且能整除 60 时转为按小时
  --scheduler <mode>      怎么定时。默认 auto
                          user-cron   写当前用户的 crontab（最常用）
                          system-cron 写 /etc/cron.d/（需 root；用户被
                                      cron.allow/deny 挡住时用这个）
                          loop        setsid 后台循环（免 root，但不扛重启）
                          auto        user-cron 可用就用它，被拒则退回 loop
  --timeout <sec>         单次 fetch 超时。默认 100
  --check-path <dir>      校验该目录与 mirror-root 是否同一文件系统，可重复
                          （建议传 runner 的 _work 上层目录）

镜像自身如何拉取（不影响 CI 端）
  --protocol ssh|https    默认 ssh。ssh 用 ssh://git@host/... 这一「第三形式」，
                          天然不被安装的 insteadOf 命中，杜绝镜像 fetch 自己
  --proxy <url>           仅 https 生效，例：http://127.0.0.1:7892
  --credential-helper <c> 仅 https 生效。私有仓需要凭据，而同步脚本用
                          GIT_CONFIG_GLOBAL=/dev/null 屏蔽全局配置时会连
                          credential.helper 一起屏蔽，故需落到镜像 local config。
                          留空则自动从全局 gitconfig 继承。
                          例：'!gh auth git-credential'
  --host <hostname>       git 主机，默认 github.com（GHE 改这里）

动作
  --status                打印当前部署状态后退出
  --verify                实测重定向是否生效（克隆计时 + 硬链接 + 联网自检）
  --uninstall             移除 insteadOf 与 cron（保留镜像目录）
  --purge                 配合 --uninstall，连镜像目录一起删
  --no-cron               只建镜像和 insteadOf，不装 cron
  --dry-run               只打印将要执行的动作
  -h, --help              本帮助

注意
  * 只为 "<url>.git" 这一精确形式安装 insteadOf。不安装无 .git 的前缀形式，
    因为 git 按最长前缀匹配，"…/pypto" 会误伤 "…/pypto-lib.git"。
  * 首次部署请在交互 shell 跑一次，让 GitHub 的 host key 进入 known_hosts，
    否则 cron 下的 ssh 可能因无法确认主机指纹而失败。
  * 卸载回滚：--uninstall，或手工
    git config --global --unset-all url.<mirror>.insteadOf
EOF
}

log()  { printf '[mirror] %s\n' "$*"; }
warn() { printf '[mirror] WARN: %s\n' "$*" >&2; }
die()  { printf '[mirror] ERROR: %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" -eq 1 ]; then printf '  [dry-run] %s\n' "$*"; return 0; fi; "$@"; }

# ---- 参数解析 --------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)        REPOS+=("$2"); shift 2 ;;
    --mirror-root) MIRROR_ROOT=$2; shift 2 ;;
    --interval)    INTERVAL=$2; shift 2 ;;
    --timeout)     TIMEOUT=$2; shift 2 ;;
    --check-path)  CHECK_PATHS+=("$2"); shift 2 ;;
    --protocol)    PROTOCOL=$2; shift 2 ;;
    --proxy)       PROXY=$2; shift 2 ;;
    --credential-helper) CRED_HELPER=$2; shift 2 ;;
    --host)        GIT_HOST=$2; shift 2 ;;
    --status)      ACTION=status; shift ;;
    --verify)      ACTION=verify; shift ;;
    --uninstall)   ACTION=uninstall; shift ;;
    --purge)       PURGE=1; shift ;;
    --no-cron)     NO_CRON=1; shift ;;
    --scheduler)   SCHEDULER=$2; shift 2 ;;
    --dry-run)     DRY=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    --version)     echo "$SELF $VERSION"; exit 0 ;;
    *)             die "未知参数：$1（--help 查看用法）" ;;
  esac
done

[ -n "$MIRROR_ROOT" ] || {
  if [ -d /data/ci-runner ]; then MIRROR_ROOT=/data/ci-runner/mirrors
  else MIRROR_ROOT=$HOME/git-mirrors; fi
}
case "$PROTOCOL" in ssh|https) ;; *) die "--protocol 只能是 ssh 或 https" ;; esac
# 前置校验：非法值必须在动网络之前就报错，别等到最后装 cron 才失败
case "$INTERVAL" in ''|*[!0-9]*) die "--interval 必须是正整数（分钟）" ;; esac
case "$TIMEOUT"  in ''|*[!0-9]*) die "--timeout 必须是正整数（秒）" ;; esac
[ "$INTERVAL" -ge 1 ] || die "--interval 必须 >= 1"
[ "$TIMEOUT"  -ge 1 ] || die "--timeout 必须 >= 1"
[ "$INTERVAL" -lt 60 ] || [ $((INTERVAL % 60)) -eq 0 ] \
  || die "--interval $INTERVAL 无法表示为 cron：需 <60，或为 60 的整数倍"
[ "$INTERVAL" -ge 60 ] || [ "$TIMEOUT" -le $((INTERVAL * 60)) ] \
  || warn "--timeout ${TIMEOUT}s 超过同步间隔 ${INTERVAL}min，一次卡死会连吃多个周期（flock 会跳过重叠的 tick）"
[ "$PROTOCOL" = ssh ] && [ -n "$PROXY" ] && warn "--proxy 对 ssh 无效（OpenSSH 不读代理环境变量），已忽略"
command -v git >/dev/null || die "找不到 git"
command -v flock >/dev/null || warn "找不到 flock，同步脚本将失去并发去重保护"
case "$SCHEDULER" in auto|user-cron|system-cron|loop) ;;
  *) die "--scheduler 只能是 auto / user-cron / system-cron / loop" ;; esac

# 用户 crontab 是否可用。注意「没有 crontab」和「被策略禁止」都返回非 0，
# 靠 stderr 文案区分——被 cron.allow/deny 挡住时必须早发现，否则镜像装好了
# 却永远不更新：CI 端照样 20ms 克隆成功，内容却停在部署那一刻，故障极隐蔽。
probe_user_cron() {
  command -v crontab >/dev/null 2>&1 || return 1
  local out; out=$(crontab -l 2>&1)
  printf '%s' "$out" | grep -qiE 'not allowed|not permitted' && return 2
  return 0
}

if [ "$NO_CRON" -eq 0 ] && [ "$ACTION" = install ]; then
  if command -v systemctl >/dev/null 2>&1 && [ "$SCHEDULER" != loop ]; then
    systemctl is-active crond >/dev/null 2>&1 || systemctl is-active cron >/dev/null 2>&1 \
      || warn "cron 服务未运行（crond/cron 都不 active），定时任务不会触发。检查：systemctl status crond"
  fi
  probe_user_cron; CRON_PROBE=$?
  case "$SCHEDULER" in
    auto)
      case $CRON_PROBE in
        0) SCHEDULER=user-cron ;;
        2) warn "当前用户被 cron 策略禁止使用 crontab（cron.allow/deny），改用 setsid 循环。
       有 root 的话更推荐：--scheduler system-cron（写 /etc/cron.d，扛重启）"
           SCHEDULER=loop ;;
        *) warn "找不到 crontab 命令，改用 setsid 循环"; SCHEDULER=loop ;;
      esac ;;
    user-cron)
      [ "$CRON_PROBE" -eq 2 ] && die "当前用户被 cron 策略禁止使用 crontab。
       改用 --scheduler system-cron（需 root）或 --scheduler loop"
      [ "$CRON_PROBE" -eq 1 ] && die "找不到 crontab 命令。改用 --scheduler loop" ;;
  esac
fi

# "org/repo" | 完整 URL  ->  "org/repo"
normalize_repo() {
  local r=$1
  r=${r%.git}
  r=${r#*://}                 # 去掉 scheme
  r=${r#*@}                   # 去掉 user@
  r=${r#"$GIT_HOST"/}         # https 形式的 host/
  r=${r#"$GIT_HOST":}         # scp 形式的 host:
  printf '%s' "$r"
}

mirror_path() { printf '%s/%s.git' "$MIRROR_ROOT" "${1##*/}"; }
sync_path()   { printf '%s/sync-%s.sh' "$MIRROR_ROOT" "${1##*/}"; }
log_path()    { printf '%s/sync-%s.log' "$MIRROR_ROOT" "${1##*/}"; }
url_https()   { printf 'https://%s/%s.git' "$GIT_HOST" "$1"; }
url_scp()     { printf 'git@%s:%s.git' "$GIT_HOST" "$1"; }
url_ssh()     { printf 'ssh://git@%s/%s.git' "$GIT_HOST" "$1"; }
# 镜像自身的 origin：ssh 用「第三形式」，绕开自己安装的 insteadOf
origin_url()  { if [ "$PROTOCOL" = ssh ]; then url_ssh "$1"; else url_https "$1"; fi; }

# 只返回时间字段；合法性已在前置校验中保证
cron_expr() {
  if [ "$INTERVAL" -lt 60 ]; then printf '*/%s * * * *' "$INTERVAL"
  else printf '0 */%s * * *' "$((INTERVAL / 60))"; fi
}
cron_line()   { printf '%s %s' "$(cron_expr)" "$1"; }
loop_path()   { printf '%s/sync-%s-loop.sh' "$MIRROR_ROOT" "$1"; }
# /etc/cron.d 的文件名不能含点，否则 cron 直接忽略该文件
crond_file()  { printf '/etc/cron.d/git-mirror-%s' "$(printf '%s' "$1" | tr -c 'A-Za-z0-9_-' '-')"; }

# ---- 文件系统同盘校验 -------------------------------------------------------
check_filesystems() {
  [ ${#CHECK_PATHS[@]} -gt 0 ] || return 0
  local mdev p pdev
  mdev=$(stat -c %d "$MIRROR_ROOT" 2>/dev/null) || return 0
  for p in "${CHECK_PATHS[@]}"; do
    pdev=$(stat -c %d "$p" 2>/dev/null) || { warn "--check-path $p 不存在，跳过"; continue; }
    if [ "$mdev" = "$pdev" ]; then
      log "同盘校验 OK: $p 与 $MIRROR_ROOT 在同一文件系统（可硬链接）"
    else
      warn "$p 与 $MIRROR_ROOT 不在同一文件系统：克隆会退化为复制（仍不走网络，但每次占满盘）"
    fi
  done
}

# ---- 凭据（仅 https 模式）---------------------------------------------------
copy_credential_helpers() {
  local mirror=$1 name=$2 key v vals=()
  if [ -n "$CRED_HELPER" ]; then
    run git -C "$mirror" config --unset-all credential.helper
    run git -C "$mirror" config --add credential.helper "$CRED_HELPER"
    log "$name: credential.helper = $CRED_HELPER（--credential-helper 指定）"
    return 0
  fi
  for key in "credential.https://$GIT_HOST.helper" credential.helper; do
    mapfile -t vals < <(git config --global --get-all "$key" 2>/dev/null)
    [ ${#vals[@]} -gt 0 ] || continue
    run git -C "$mirror" config --unset-all "$key"
    for v in "${vals[@]}"; do run git -C "$mirror" config --add "$key" "$v"; done
    log "$name: 已从全局复制 $key（${#vals[@]} 条）到镜像 local config"
    return 0
  done
  warn "$name: 全局没有 credential.helper。私有仓的 https 同步会认证失败；"
  warn "       先 gh auth login，或用 --credential-helper '!gh auth git-credential'"
}

# ---- 安装 ------------------------------------------------------------------
install_repo() {
  local repo=$1 name=${1##*/}
  local mirror sync logf origin
  mirror=$(mirror_path "$repo"); sync=$(sync_path "$repo"); logf=$(log_path "$repo")
  origin=$(origin_url "$repo")

  # 1) bootstrap 全量 bare 镜像（--mirror 保证任意 pin 指向的 commit 都在）
  if [ -d "$mirror/objects" ]; then
    log "$name: 镜像已存在，跳过 clone"
  else
    log "$name: bootstrap 镜像 <- $origin"
    if [ "$DRY" -eq 1 ]; then
      printf '  [dry-run] git clone --mirror %s %s\n' "$origin" "$mirror"
    else
      local i ok=0
      for i in 1 2 3 4 5; do
        rm -rf "$mirror"
        if GIT_CONFIG_GLOBAL=/dev/null git clone --mirror "$origin" "$mirror"; then ok=1; break; fi
        log "$name: 第 $i 次失败，8s 后重试"; sleep 8
      done
      [ "$ok" -eq 1 ] || die "$name: 镜像 bootstrap 失败"
    fi
  fi
  run git -C "$mirror" remote set-url origin "$origin"

  # https 模式下私有仓需要凭据，但同步脚本里的 GIT_CONFIG_GLOBAL=/dev/null
  # 会把全局的 credential.helper 一并屏蔽掉。local config 不受该变量影响，
  # 故把 helper 落到镜像自己的配置里。
  [ "$PROTOCOL" = https ] && copy_credential_helpers "$mirror" "$name"

  # 2) 重定向：只装 .git 精确形式的两条（https 与 scp-like）
  #    git 按最长前缀选规则，故必然压过已有的 url."git@host:".insteadOf = https://host/
  run git config --global --unset-all "url.$mirror.insteadOf"
  run git config --global --add "url.$mirror.insteadOf" "$(url_https "$repo")"
  run git config --global --add "url.$mirror.insteadOf" "$(url_scp   "$repo")"
  log "$name: insteadOf -> $mirror"

  # 3) 同步脚本
  local gitopts=""
  [ "$PROTOCOL" = https ] && gitopts='-c http.version=HTTP/1.1 -c http.postBuffer=524288000'
  if [ "$DRY" -eq 1 ]; then
    printf '  [dry-run] 生成 %s (timeout=%s proxy=%s)\n' "$sync" "$TIMEOUT" "${PROXY:-none}"
  else
    {
      printf '#!/usr/bin/env bash\n'
      printf '# generated by %s v%s — 请改部署器后重跑，勿直接编辑\n' "$SELF" "$VERSION"
      printf 'MIRROR=%s\n' "$mirror"
      printf 'LOG=%s\n'    "$logf"
      printf 'LOCK=%s/.sync-%s.lock\n' "$MIRROR_ROOT" "$name"
      printf 'TIMEOUT=%s\n' "$TIMEOUT"
      printf 'PROXY=%s\n'   "$PROXY"
      printf 'LOG_LINES=%s\n' "$LOG_LINES"
      printf 'GITOPTS=(%s)\n' "$gitopts"
      cat <<'BODY'

# flock 去重：上一轮还没跑完就直接退出，避免堆积
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -n 9 || exit 0
fi
[ -n "$PROXY" ] && export https_proxy="$PROXY" http_proxy="$PROXY" no_proxy=127.0.0.1,localhost
ts=$(date -u +%FT%TZ)
# GIT_CONFIG_GLOBAL=/dev/null 屏蔽全局 insteadOf，否则镜像会 fetch 自己（静默无更新）；
# 代价是全局的 http.* 也被屏蔽，故 https 模式下由 GITOPTS 显式补回。
if GIT_CONFIG_GLOBAL=/dev/null timeout "$TIMEOUT" \
     git -C "$MIRROR" "${GITOPTS[@]}" remote update --prune >>"$LOG" 2>&1; then
  echo "$ts ok $(GIT_CONFIG_GLOBAL=/dev/null git -C "$MIRROR" rev-parse --short HEAD 2>/dev/null)" >>"$LOG"
else
  echo "$ts FAIL rc=$?" >>"$LOG"
fi
tail -n "$LOG_LINES" "$LOG" >"$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
BODY
    } > "$sync"
    chmod +x "$sync"
  fi
  log "$name: 同步脚本 $sync"

  # 4) 定时
  if [ "$NO_CRON" -eq 1 ]; then
    log "$name: --no-cron，跳过定时任务（同步脚本已就绪，可自行调度）"
  else
    install_schedule "$name" "$sync"
  fi
}

# ---- 定时器：三种后端 -------------------------------------------------------
install_schedule() {
  local name=$1 sync=$2
  case "$SCHEDULER" in
    user-cron)   sched_user_cron   "$name" "$sync" ;;
    system-cron) sched_system_cron "$name" "$sync" ;;
    loop)        sched_loop        "$name" "$sync" ;;
  esac
}

# 先 grep -v 过滤同名行再追加，重复执行不会叠加
sched_user_cron() {
  local name=$1 sync=$2 line; line=$(cron_line "$sync")
  if [ "$DRY" -eq 1 ]; then log "$name: [dry-run] user-cron -> $line"; return 0; fi
  ( crontab -l 2>/dev/null | grep -vF "$sync"; printf '%s\n' "$line" ) | crontab -
  # 回读验证：写 crontab 可能因权限/策略而静默失败，不能凭「命令没报错」就宣称装好
  if crontab -l 2>/dev/null | grep -qF "$sync"; then
    log "$name: user-cron 已装并验证 -> $line"
  else
    warn "$name: crontab 写入后回读不到，镜像不会自动更新！改用 --scheduler system-cron 或 loop"
  fi
}

sched_system_cron() {
  local name=$1 sync=$2 f content
  f=$(crond_file "$name")
  # /etc/cron.d 的格式比用户 crontab 多一个用户字段
  content="$(cron_expr) $(id -un) $sync"
  if [ "$DRY" -eq 1 ]; then log "$name: [dry-run] system-cron -> $f: $content"; return 0; fi
  if [ "$(id -u)" = 0 ]; then
    printf '%s\n' "$content" > "$f" && chmod 644 "$f"
  elif sudo -n true 2>/dev/null; then
    printf '%s\n' "$content" | sudo tee "$f" >/dev/null && sudo chmod 644 "$f"
  else
    warn "$name: 需要 root 才能写 $f。请手工执行："
    warn "       echo '$content' | sudo tee $f && sudo chmod 644 $f"
    return 1
  fi
  if [ -s "$f" ]; then log "$name: system-cron 已装并验证 -> $f"
  else warn "$name: $f 写入失败，镜像不会自动更新！"; fi
}

sched_loop() {
  local name=$1 sync=$2 loop; loop=$(loop_path "$name")
  if [ "$DRY" -eq 1 ]; then log "$name: [dry-run] setsid 循环 -> $loop（每 $((INTERVAL*60))s）"; return 0; fi
  { printf '#!/usr/bin/env bash\n'
    printf '# generated by %s v%s — setsid 后台循环（免 root，但不扛重启）\n' "$SELF" "$VERSION"
    printf 'while true; do %s; sleep %s; done\n' "$sync" "$((INTERVAL * 60))"
  } > "$loop"
  chmod +x "$loop"
  if pgrep -f "$loop" >/dev/null 2>&1; then
    log "$name: 循环已在运行，未重复启动"
  else
    setsid "$loop" >/dev/null 2>&1 </dev/null &
    sleep 1
    if pgrep -f "$loop" >/dev/null 2>&1; then log "$name: setsid 循环已启动并验证 -> $loop"
    else warn "$name: 循环启动后查不到进程，镜像不会自动更新！"; fi
  fi
  warn "$name: 循环扛不住重启。机器重启后重跑本脚本即可（幂等，pgrep 去重）；
       有 root 的话改用 --scheduler system-cron 更省心"
}

uninstall_repo() {
  local repo=$1 name=${1##*/} mirror sync loop f
  mirror=$(mirror_path "$repo"); sync=$(sync_path "$repo")
  loop=$(loop_path "$name");     f=$(crond_file "$name")
  run git config --global --unset-all "url.$mirror.insteadOf"
  # 三种调度后端可能装过任意一种，全部清掉
  if [ "$DRY" -eq 1 ]; then
    printf '  [dry-run] 移除 user-cron / %s / 循环进程\n' "$f"
  else
    crontab -l >/dev/null 2>&1 && ( crontab -l 2>/dev/null | grep -vF "$sync" ) | crontab -
    if [ -f "$f" ]; then
      if [ "$(id -u)" = 0 ]; then rm -f "$f"
      elif sudo -n true 2>/dev/null; then sudo rm -f "$f"
      else warn "$name: 需 root 删除 $f：sudo rm -f $f"; fi
    fi
    pkill -f "$loop" 2>/dev/null && log "$name: 已停止 setsid 循环"
  fi
  run rm -f "$sync" "$loop"
  if [ "$PURGE" -eq 1 ]; then run rm -rf "$mirror"; log "$name: 镜像已删除"; fi
  log "$name: 已卸载（CI 克隆恢复走网络）"
}

# ---- 状态 ------------------------------------------------------------------
show_status() {
  echo "mirror-root : $MIRROR_ROOT"
  echo "host        : $GIT_HOST"
  echo
  echo "== insteadOf 规则 =="
  git config --global --get-regexp '^url\..*\.insteadof' 2>/dev/null | sed 's/^/  /' || echo "  (无)"
  echo
  echo "== 定时器 =="
  local found=0
  crontab -l 2>/dev/null | grep -F "$MIRROR_ROOT" | sed 's/^/  user-cron   /' && found=1
  grep -lsF "$MIRROR_ROOT" /etc/cron.d/git-mirror-* 2>/dev/null | while read -r f; do
    sed 's|^|  system-cron |' "$f"; done | grep . && found=1
  pgrep -af -- "-loop.sh" 2>/dev/null | grep -F "$MIRROR_ROOT" | sed 's/^/  loop        /' && found=1
  [ "$found" -eq 1 ] || echo "  (无 —— 镜像不会自动更新！)"
  echo
  echo "== 镜像 =="
  local m name logf
  for m in "$MIRROR_ROOT"/*.git; do
    [ -d "$m/objects" ] || continue
    name=$(basename "$m" .git); logf=$(log_path "$name")
    printf '  %-24s %-6s origin=%s\n' "$name" "$(du -sh "$m" 2>/dev/null | cut -f1)" \
      "$(git -C "$m" remote get-url origin 2>/dev/null)"
    printf '    本地 HEAD %s\n' "$(GIT_CONFIG_GLOBAL=/dev/null git -C "$m" rev-parse --short HEAD 2>/dev/null)"
    [ -f "$logf" ] && printf '    最近同步  %s\n' "$(grep -E ' (ok|FAIL)' "$logf" | tail -1)"
    [ -f "$logf" ] && printf '    近百次    ok=%s fail=%s\n' \
      "$(grep -cE ' ok'   <(tail -100 "$logf"))" "$(grep -cE ' FAIL' <(tail -100 "$logf"))"
  done
}

# ---- 验证 ------------------------------------------------------------------
verify_repo() {
  local repo=$1 name=${1##*/} mirror t dev_ok fail=0
  mirror=$(mirror_path "$repo")
  echo "== $repo =="
  [ -d "$mirror/objects" ] || { echo "  FAIL 镜像不存在：$mirror"; return 1; }

  local tmp="$MIRROR_ROOT/.verify-$name"
  local u
  for u in "$(url_https "$repo")" "$(url_scp "$repo")"; do
    rm -rf "$tmp"
    t=$( { time -p git clone --no-checkout "$u" "$tmp" >/dev/null 2>&1; } 2>&1 | awk '/^real/{print $2}' )
    if [ ! -d "$tmp/.git" ]; then echo "  FAIL 克隆失败：$u"; fail=1; continue; fi
    # 秒数 < 1 视为本地；走网络不可能亚秒完成
    if awk -v t="$t" 'BEGIN{exit !(t < 1)}'; then
      echo "  PASS $u -> ${t}s（本地）"
    else
      echo "  FAIL $u -> ${t}s（疑似仍走网络，检查 insteadOf）"; fail=1
    fi
  done

  # 硬链接：pack 文件链接数 >1 说明与镜像共享 inode
  local nlink
  nlink=$(stat -c %h "$tmp"/.git/objects/pack/*.pack 2>/dev/null | head -1)
  if [ "${nlink:-1}" -gt 1 ]; then echo "  PASS 硬链接生效（pack nlink=$nlink，克隆不额外占盘）"
  else echo "  WARN 未硬链接（mirror-root 与克隆目标不同盘，退化为复制）"; fi
  rm -rf "$tmp"

  # 联网自检：镜像 HEAD 必须与远端一致，否则就是在 fetch 自己
  local lh rh
  lh=$(GIT_CONFIG_GLOBAL=/dev/null git -C "$mirror" rev-parse HEAD 2>/dev/null)
  rh=$(GIT_CONFIG_GLOBAL=/dev/null git -C "$mirror" ls-remote origin HEAD 2>/dev/null | awk '{print $1}')
  if [ -z "$rh" ]; then echo "  WARN 无法访问远端（网络抽风？），跳过一致性检查"
  elif [ "$lh" = "$rh" ]; then echo "  PASS 同步真的联网（本地 == 远端 ${lh:0:8}）"
  else echo "  INFO 落后于远端（本地 ${lh:0:8} != 远端 ${rh:0:8}），等下一轮 cron"; fi
  return $fail
}

# ---- 主流程 ----------------------------------------------------------------
case "$ACTION" in
  status) show_status; exit 0 ;;
esac

[ ${#REPOS[@]} -gt 0 ] || die "必须指定至少一个 --repo（--help 查看用法）"
NORM=()
for r in "${REPOS[@]}"; do NORM+=("$(normalize_repo "$r")"); done

case "$ACTION" in
  uninstall)
    for r in "${NORM[@]}"; do uninstall_repo "$r"; done
    ;;
  verify)
    rc=0
    for r in "${NORM[@]}"; do verify_repo "$r" || rc=1; done
    exit $rc
    ;;
  install)
    run mkdir -p "$MIRROR_ROOT"
    log "mirror-root = $MIRROR_ROOT  host = $GIT_HOST  protocol = $PROTOCOL  interval = ${INTERVAL}min"
    check_filesystems
    for r in "${NORM[@]}"; do install_repo "$r"; done
    echo
    log "完成。自检：$SELF --verify $(printf -- '--repo %s ' "${NORM[@]}")--mirror-root $MIRROR_ROOT"
    log "查看状态：$SELF --status --mirror-root $MIRROR_ROOT"
    ;;
esac
