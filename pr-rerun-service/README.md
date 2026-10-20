# PR Rerun Service

An independent GitHub Actions rerun service for PyPTO contributors. An
administrator runs one server; contributors log in once and use `pr-rerun 123`
to inspect and confirm reruns for **their own open PRs**.

English | [中文](README.zh-CN.md) | [Full operator guide](docs/README.md)

## Behavior

- Ordinary CI failures rerun failed jobs. The configured Codex Review workflow
  reruns all jobs in that workflow, after verifying its PR identity artifact.
- Each contributor receives a separate service token bound to their immutable
  GitHub user ID. Contributors need neither repository write access nor a `gh` login.
- The server uses the administrator's saved `gh` login or a dedicated GitHub token.
- Every rerun requires a preview, exact confirmation text, and server-side state
  revalidation. There is no `--yes`, merge, delete, cancel, or workflow-dispatch API.
- Remote listeners bind to the local NetBird IP **and interface**. A separate
  loopback listener serves clients on the server host. Discovery failures abort startup.

## Server setup

Requires Python 3.10+, Linux, a connected NetBird kernel interface, `ip` from
iproute2, and direct HTTPS access to GitHub. macOS supports the client and local-only mode.
Run these commands from `pto-toolbox/pr-rerun-service`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -c requirements.lock .

export PR_RERUN_DATABASE="$HOME/.local/share/pr-rerun/rerun.sqlite"
export PR_RERUN_CONFIG="$PWD/config.example.json"

# Log in as the same OS user that will run the server.
gh auth login --hostname github.com
pr-rerun-admin issue alice --github-auth gh
pr-rerun-server --github-auth gh --port 8080
```

Replace `alice` with the contributor's GitHub username. Deliver `access_token`
privately; issuing another token for the same user revokes the previous one.
Keep the database/configuration variables set when running administrator commands.
The server discovers its own NetBird address. Allow the intended peers to reach
TCP 8080 in NetBird policies. Docker is optional and is not needed for deployment.

For boot startup, copy `deploy/pr-rerun.service` to
`deploy/pr-rerun.local.service`, adapt its user, home, project, and database paths,
and follow the [systemd instructions](docs/README.md#boot-startup-with-systemd).
Local unit files are ignored by Git.

## Shared client installation

Each client machine can have one root-owned installation for all its OS users.
First build the wheel from the source on that machine:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements-dev.lock ".[dev]"
.venv/bin/python -m build --wheel

bash deploy/install-shared-client.sh --dry-run
sudo bash deploy/install-shared-client.sh
```

The installer uses the system Python under `/opt/pr-rerun-client` and publishes
`/usr/local/bin/pr-rerun`. It refuses to overwrite an existing installation.
Optional offline wheels belong in `dist/shared-client-wheelhouse/` and must match
the destination's Python version and architecture. Otherwise dependencies come
from PyPI under `requirements.lock`. No wheel bundles are committed here.

## Contributor usage

Use your own normal OS account, **without sudo**. The address below is an example;
replace it with the service host's NetBird IP supplied by the administrator.

```bash
pr-rerun login --netbird-server http://100.64.0.10:8080
# Enter your personal service token at the hidden prompt.

# Subsequent invocations, including in new terminals:
pr-rerun 123
pr-rerun logout
```

Login saves the origin and token in `~/.config/pr-rerun/client.json` (or under
`XDG_CONFIG_HOME`) with directory/file modes `0700`/`0600`. It checks authentication
without creating a rerun. A saved token is only reused for its original server.
One OS account stores one login; contributors sharing an OS account share that login.

## Security and publishing

Credentials, databases, logs, local deployment units, virtual environments,
and build bundles belong outside version control. Examples use fictitious users
and a placeholder VPN address; this repository contains no deployed service URL.
The public files are source, tests, dependency locks, and deployment templates.
The service host and administrator's GitHub credential remain trusted: GitHub
does not provide a rerun-only token scope. See the [operator guide](docs/README.md)
for attribution limits, unknown-result handling, and credential storage details.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -c requirements-dev.lock -e ".[dev]"
python -m pytest tests
ruff check src tests
ruff format --check src tests
pyright
python -m build
```

Tests simulate GitHub and never trigger real reruns. Linux socket tests require
permission to bind sockets to interfaces, including loopback.
