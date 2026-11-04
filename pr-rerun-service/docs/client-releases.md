# Standalone client releases

English | [中文](client-releases.zh-CN.md)

Use the assets in [GitHub Releases](https://github.com/pypto-tools/pto-toolbox/releases).
The Linux ARM64 (`aarch64`) and x86_64 clients include Python and their dependencies.
They require glibc 2.28 or newer; Alpine/musl is not supported. The initial installer
requires only the Python 3.9+ standard library. No pip, compiler, source checkout,
Docker, or system Python replacement is needed on a client machine.

The standalone archive contains the CLI, updater, runtime and dependency licenses.
It excludes server/admin modules and server dependencies. A separate service wheel
remains available for server operators and source-based deployments. Updating the
client does not update or restart the service. Existing App-mode servers remain
compatible; official aliases require server 0.3.2 or newer.

## First shared installation

An administrator runs this once on each machine. Pin the bootstrap and archive to
the same release. Download the installer from the official repository, inspect it,
then execute it with root privileges:

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  https://github.com/pypto-tools/pto-toolbox/releases/download/pr-rerun-v0.7.1/install-client.py \
  -o install-client.py
python3 install-client.py --dry-run
sudo python3 install-client.py --version 0.7.1
pr-rerun --version
```

If your machine uses a local HTTP/mixed proxy, use it for **both** downloads:

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  --proxy http://127.0.0.1:7892 \
  https://github.com/pypto-tools/pto-toolbox/releases/download/pr-rerun-v0.7.1/install-client.py \
  -o install-client.py
sudo python3 install-client.py --version 0.7.1 --proxy http://127.0.0.1:7892
```

The installer selects the machine's architecture, downloads `SHA256SUMS` and the
archive over HTTPS, verifies the archive, checks its platform and runs version/help
self-tests. It then publishes `/usr/local/bin/pr-rerun` and
`/usr/local/bin/pr-rerun-update`, backed by root-owned releases under `/opt/pr-rerun`.
It refuses unrelated existing commands. Use `command -v pr-rerun` to check whether
an older personal/Conda command is taking precedence; the shared command can always
be invoked as `/usr/local/bin/pr-rerun`.

Each OS user logs in **without sudo**, using their own GitHub account:

```bash
# Replace the example NetBird address with your service address.
# pypto is an example official alias configured by the service administrator.
pr-rerun login --netbird-server http://100.64.0.10:8080 --repo pypto
pr-rerun 123
```

Login state and local aliases remain in each user's `~/.config/pr-rerun` (or
`XDG_CONFIG_HOME`). The installer never reads or migrates that directory.

## Update and rollback

```bash
sudo pr-rerun-update                         # latest published client release
sudo pr-rerun-update --version 0.7.1         # an exact version
sudo pr-rerun-update --proxy http://127.0.0.1:7892
sudo pr-rerun-update --rollback              # previous installed standalone release
pr-rerun --version
```

Updates are explicit; normal rerun commands never update themselves. A lock prevents
concurrent installers. Each version has an immutable directory. Installation switches
the `current` symlink only after validation; failures during activation restore the
previous pointers. Old versions remain available for running processes and rollback.
The updater rejects automatic downgrades and same-version archives with different
checksums. Reinstalling the same archive is safe and preserves the rollback target.
No re-login is needed after updating, unless the service session has expired/revoked.

`--rollback` requires a previous standalone release. It does not downgrade the server
or its database. To use non-default directories, supply the same `--prefix` and
`--bin-dir` values to every install/update/rollback command.

## Update succeeded but the command is still old

`sudo pr-rerun-update` updates the shared installation only. It does not modify
users' PATH, Conda environments or personal launchers. If `pr-rerun repos` reports
`Specify a positive PR number`, or `--version` is unrecognized, check in the
**same terminal and environment that failed**:

```bash
type -a pr-rerun
command -v pr-rerun
pr-rerun --version
/usr/local/bin/pr-rerun --version
```

If the shared command is 0.4.1 but the plain command is old, fix command precedence.
First verify with the full path:

```bash
/usr/local/bin/pr-rerun repos
```

If the obsolete entry is specifically your own `~/.local/bin/pr-rerun` launcher,
back it up and link it to the shared command. This example applies only to that
personal path; run it as the normal user, without sudo:

```bash
# Check the paths/versions above first; choose another backup name if it exists.
if [ ! -e "$HOME/.local/bin/pr-rerun.before-shared" ] && \
   [ ! -L "$HOME/.local/bin/pr-rerun.before-shared" ]; then
  mv "$HOME/.local/bin/pr-rerun" "$HOME/.local/bin/pr-rerun.before-shared" &&
    ln -s /usr/local/bin/pr-rerun "$HOME/.local/bin/pr-rerun"
fi
hash -r
pr-rerun --version
pr-rerun repos
```

Do not apply that move to arbitrary Conda/venv paths. For those, try
`export PATH="/usr/local/bin:$PATH"` and `hash -r` in the current shell, verify,
then decide whether to adjust shell initialization. Activating an environment
again may change precedence. Resolve shell aliases/functions according to
`type -a`. Do not delete `~/.config/pr-rerun` or log in again. Each user with an
old personal installation must check their own command entry.

`repos` also requires an App-mode server at version 0.4.1. If even the full-path
command reports an old server, follow [operations](operations.md) to upgrade the
server; repeating the client update will not fix it.

## Migrate the old shared wheel installation

```bash
sudo python3 install-client.py --version 0.7.1 --migrate-existing
```

This flag replaces only the exact known legacy wrapper from
`deploy/pr-rerun-client`. It saves that wrapper to `/opt/pr-rerun/legacy-pr-rerun`
and leaves `/opt/pr-rerun-client` intact. User login files are untouched. For the
first migration, there is no previous standalone release; to restore the legacy
launcher, run:

```bash
sudo install -m 0755 /opt/pr-rerun/legacy-pr-rerun /usr/local/bin/pr-rerun.legacy-restore
sudo mv -Tf /usr/local/bin/pr-rerun.legacy-restore /usr/local/bin/pr-rerun
```

## Offline installation/update

Download these three assets from the **same trusted release** on a connected machine
and transfer them to the target. Keep the archive filename unchanged:

- `install-client.py`
- `SHA256SUMS`
- `pr-rerun-client-linux-aarch64.tar.gz` or `pr-rerun-client-linux-x86_64.tar.gz`

```bash
sudo python3 install-client.py \
  --bundle pr-rerun-client-linux-aarch64.tar.gz --checksums SHA256SUMS
# Subsequent offline updates can use the installed updater:
sudo pr-rerun-update \
  --bundle pr-rerun-client-linux-aarch64.tar.gz --checksums SHA256SUMS
```

Checksums detect corruption, not publisher impersonation: obtain the installer and
checksum file from the official GitHub release over a trusted connection. Offline
mode does not contact GitHub, PyPI, or the rerun service.

## Maintainer release process

1. Update `src/pr_rerun/version.py`, `pyproject.toml`, the legacy installer version
   and bilingual release notes under `docs/releases/`.
2. Push the tested change. The `PR rerun release` workflow tests the service and
   installer, and builds both clients in native `manylinux_2_28` containers with
   a uv-managed shared CPython 3.12.16 (the image's Python is static and cannot be
   frozen). It exercises installation with Python 3.9 inside each container
   and executes the bundle again on the host runner.
3. Tag the tested commit `pr-rerun-vX.Y.Z` and push that tag. Only after all jobs
   pass does the workflow publish the two client archives, service wheel,
   `install-client.py`, and `SHA256SUMS`. The tag must match the embedded version.

Builds use an allowlist of client source modules in an isolated staging tree, never
server state or machine configuration. Build dependencies are pinned in
`release/requirements-build.lock`. Runtime dependencies, Python version and source
commit are recorded in each archive's `release.json`. Container tags and GitHub
Action major tags can move; builds are tested but are not claimed byte-reproducible.

Keep the repository's latest release assigned to the latest stable `pr-rerun-v*`
tag; other toolbox tools must not replace it. Users can pin `--version` independently.
