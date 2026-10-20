#!/usr/bin/env bash
# smoke.sh — repeatable, offline, side-effect-free checks for every tool.
#
# Asserts that each tool parses, answers --help, rejects bad arguments, and
# honours --dry-run. Never touches the network and never writes outside a
# temporary directory it creates and removes.
#
#   tests/smoke.sh          # run all checks
#
# Exit code: 0 = all passed, 1 = at least one failure.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

pass=0 fail=0
ok()   { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }
check(){ # check <description> <expected-rc> <cmd...>
  local desc=$1 want=$2; shift 2
  local out rc
  # timeout so a regression that hangs fails the suite instead of wedging it
  out=$(timeout 30 "$@" 2>&1); rc=$?
  if [ "$rc" -eq "$want" ]; then ok "$desc"
  else bad "$desc (want rc=$want, got $rc)"; printf '%s\n' "$out" | sed 's/^/       | /' | head -5; fi
}

echo "== syntax =="
while IFS= read -r f; do
  if bash -n "$f" 2>/dev/null; then ok "bash -n ${f#"$ROOT"/}"
  else bad "bash -n ${f#"$ROOT"/}"; fi
done < <(find "$ROOT" -name '*.sh' -not -path '*/.git/*' | sort)

echo
echo "== every tool answers --help =="
# A tool is a directory holding a script of the same name. This deliberately
# excludes tests/ and skills/ — globbing */*.sh would match this very script
# and recurse forever.
for d in "$ROOT"/*/; do
  name=$(basename "$d")
  t="$d$name.sh"
  [ -f "$t" ] || continue
  out=$(timeout 20 "$t" --help 2>&1); rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$out" ]; then ok "$name --help"
  else bad "$name --help (rc=$rc, ${#out} bytes)"; fi
done

echo
echo "== deploy-git-mirror: argument validation =="
D="$ROOT/deploy-git-mirror/deploy-git-mirror.sh"
check "rejects missing --repo"        1 "$D" --mirror-root "$TMP"
check "rejects non-numeric --interval" 1 "$D" --repo a/b --interval abc --mirror-root "$TMP"
check "rejects uncron-able --interval" 1 "$D" --repo a/b --interval 90  --mirror-root "$TMP"
check "rejects zero --timeout"         1 "$D" --repo a/b --timeout 0    --mirror-root "$TMP"
check "rejects unknown --protocol"     1 "$D" --repo a/b --protocol ftp --mirror-root "$TMP"
check "rejects unknown --scheduler"    1 "$D" --repo a/b --scheduler xx --mirror-root "$TMP"
check "rejects unknown flag"           1 "$D" --repo a/b --bogus        --mirror-root "$TMP"

echo
echo "== deploy-git-mirror: --dry-run writes nothing =="
DRYDIR="$TMP/dry"
check "dry-run install exits 0" 0 \
  "$D" --dry-run --no-cron --repo example/repo --mirror-root "$DRYDIR"
if [ ! -e "$DRYDIR" ]; then ok "dry-run created no mirror-root"
else bad "dry-run created $DRYDIR"; fi

out=$("$D" --dry-run --no-cron --repo example/repo --mirror-root "$DRYDIR" 2>&1)
for expect in 'git clone --mirror' 'insteadOf' 'no-cron'; do
  if printf '%s' "$out" | grep -qF "$expect"; then ok "dry-run mentions '$expect'"
  else bad "dry-run does not mention '$expect'"; fi
done

echo
echo "== netbird-preflight: argument handling =="
N="$ROOT/netbird-preflight/netbird-preflight.sh"
check "rejects unknown flag" 2 "$N" --bogus

echo
echo "== pr-rerun-service: deployment argument handling =="
P="$ROOT/pr-rerun-service/deploy"
check "shared installer --help" 0 bash "$P/install-shared-client.sh" --help
check "shared installer rejects unknown flag" 2 bash "$P/install-shared-client.sh" --bogus
check "shared installer --dry-run" 0 bash "$P/install-shared-client.sh" --dry-run
check "systemd installer --help" 0 bash "$P/install-systemd.sh" --help
check "systemd installer rejects unknown flag" 2 bash "$P/install-systemd.sh" --bogus
check "systemd installer requires explicit unit" 2 bash "$P/install-systemd.sh"
check "systemd installer --dry-run" 0 bash "$P/install-systemd.sh" --dry-run "$P/pr-rerun.service"
check "shared launcher syntax" 0 sh -n "$P/pr-rerun-client"

echo
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
