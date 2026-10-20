#!/bin/bash
# Run once as root on each machine that needs a client shared by all OS users.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 022

usage() {
    cat <<'HELP'
Usage: install-shared-client.sh [--dry-run]
Install the built 0.2.1 wheel for all OS users using system Python 3.10+.
  -h, --help     Show this help without changing anything.
  --dry-run      Show the planned paths without installing anything.
Build dist/pr_rerun_service-0.2.1-py3-none-any.whl first.
An optional dist/shared-client-wheelhouse supplies offline dependencies;
otherwise dependencies are downloaded from PyPI under requirements.lock.
Existing public commands or runtime directories are never overwritten.
HELP
}
dry_run=false
for argument in "$@"; do
    case "$argument" in
        -h|--help) usage; exit 0 ;;
        --dry-run) dry_run=true ;;
        *) echo "Unknown argument: $argument" >&2; exit 2 ;;
    esac
done
if $dry_run; then
    echo "Would install the built wheel under /opt/pr-rerun-client and publish /usr/local/bin/pr-rerun."
    echo "Existing installations will be refused. Each user must log in separately."
    exit 0
fi
if [[ "$EUID" -ne 0 ]]; then
    echo "Run: sudo bash deploy/install-shared-client.sh" >&2
    exit 1
fi

deployment_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(dirname -- "$deployment_dir")"
wheel="$project_dir/dist/pr_rerun_service-0.2.1-py3-none-any.whl"
wheelhouse="$project_dir/dist/shared-client-wheelhouse"
destination=/opt/pr-rerun-client
command_path=/usr/local/bin/pr-rerun

for target in "$destination" "$command_path"; do
    if [[ -e "$target" || -L "$target" ]]; then
        echo "$target already exists; refusing to replace an existing installation." >&2
        exit 1
    fi
done
for source in "$wheel" "$project_dir/requirements.lock" "$deployment_dir/pr-rerun-client"; do
    if [[ ! -f "$source" ]]; then
        echo "Required installation file is missing: $source" >&2
        exit 1
    fi
done

# The system interpreter avoids dependencies on the administrator's personal
# conda installation. The resulting runtime is readable, but not writable, by users.
/usr/bin/python3 -I -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
install -d -o root -g root -m 0755 "$destination"
/usr/bin/python3 -I -m venv --copies "$destination"
package_source=(--index-url https://pypi.org/simple)
if [[ -d "$wheelhouse" ]]; then
    package_source=(--no-index --find-links "$wheelhouse")
fi
"$destination/bin/python" -I -m pip --isolated install \
    --disable-pip-version-check --no-cache-dir "${package_source[@]}" \
    --constraint "$project_dir/requirements.lock" "$wheel"
"$destination/bin/python" -I -m pip --isolated --no-cache-dir check
"$destination/bin/python" -I -m pr_rerun.cli --help >/dev/null
chmod -R u+rwX,go+rX,go-w "$destination"
install -o root -g root -m 0755 "$deployment_dir/pr-rerun-client" "$command_path"
cmp -s "$deployment_dir/pr-rerun-client" "$command_path"
"$command_path" --help >/dev/null

echo "Shared client installed: $command_path"
echo "Each user should log in WITHOUT sudo:"
echo "  pr-rerun login --netbird-server http://100.64.0.10:8080"
echo "  pr-rerun 123"
echo "If an older personal client takes precedence, use /usr/local/bin/pr-rerun."
