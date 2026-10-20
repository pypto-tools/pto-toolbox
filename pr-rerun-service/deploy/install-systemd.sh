#!/bin/bash
# Install an explicitly supplied, operator-reviewed unit. Never embed credentials.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

usage() {
    cat <<'HELP'
Usage: install-systemd.sh [--dry-run] UNIT_FILE
Install a reviewed local service unit as /etc/systemd/system/pr-rerun.service.
Copy deploy/pr-rerun.service to deploy/pr-rerun.local.service and adapt it first.
  -h, --help     Show this help without changing anything.
  --dry-run      Print the intended actions without installing or starting a service.
Stop any manually started server before installing. Existing different units
are never overwritten. Keep credentials outside this repository.
HELP
}
dry_run=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --dry-run) dry_run=true; shift ;;
        --*) echo "Unknown option: $1" >&2; exit 2 ;;
        *) break ;;
    esac
done
if [[ $# -ne 1 || ! -f "$1" || "$1" != *.service ]]; then
    usage >&2
    exit 2
fi
unit_source="$(realpath -- "$1")"
unit_destination=/etc/systemd/system/pr-rerun.service
if $dry_run; then
    printf 'Would verify %s, install it as %s, reload systemd, and enable/start pr-rerun.service.\n' "$unit_source" "$unit_destination"
    exit 0
fi
if [[ "$EUID" -ne 0 ]]; then
    echo "Run this installer with sudo from an interactive terminal." >&2
    exit 1
fi
if [[ -e "$unit_destination" || -L "$unit_destination" ]]; then
    if [[ -L "$unit_destination" ]] || ! cmp -s "$unit_source" "$unit_destination"; then
        echo "An existing pr-rerun.service differs; refusing to overwrite it." >&2
        exit 1
    fi
fi
systemd-analyze verify "$unit_source"
install -o root -g root -m 0644 "$unit_source" "$unit_destination"
systemctl daemon-reload
systemctl enable --now pr-rerun.service
systemctl is-enabled pr-rerun.service
systemctl is-active pr-rerun.service
systemctl status pr-rerun.service --no-pager --lines=8
