# PR Rerun Service

App deployments support GitHub device login without manually distributed tokens.
See [GitHub login and repository policies](docs/multi-repository.md).

An independent GitHub Actions rerun service for PyPTO contributors. An
administrator runs one server; contributors log in once and use `pr-rerun 123`
to inspect and confirm reruns for **their own open PRs**.

English | [中文](README.zh-CN.md) | [Full operator guide](docs/README.md)

Version **0.3.0** adds GitHub App authentication and independently managed repository
policies: user grants, workflow rules, PR restrictions, quotas and audited revisions.
See the [App and repository policy guide](docs/multi-repository.md) for registration,
bootstrap, maintainer commands and migration. Legacy `gh`/token deployments remain
supported. Multi-repository use requires an App installation and the 0.3.0 client.

## Behavior

- Ordinary CI failures rerun failed jobs. The configured Codex Review workflow
  reruns all jobs in that workflow, after verifying its PR identity artifact.
  Completed cancelled runs qualify only if they contain failed or timed-out jobs.
- Each contributor receives a separate service token bound to their immutable
  GitHub user ID. Contributors need neither repository write access nor a `gh` login.
- The server uses the administrator's saved `gh` login or a dedicated GitHub token.
- Every rerun requires a preview, exact confirmation text, and server-side state
  revalidation. There is no `--yes`, merge, delete, cancel, or workflow-dispatch API.
- Remote listeners bind to the local NetBird IP **and interface**. A separate
  loopback listener serves clients on the server host. Discovery failures abort startup.


Fork CI runs with empty GitHub PR links are supported only when source identity
and the complete branch history uniquely identify the requested PR. Reused or
ambiguous branches remain excluded; checks run again before each rerun.

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
pr-rerun login --token-login --netbird-server http://100.64.0.10:8080
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

## Repository aliases (client 0.3.1)

Configure shorthand locally; no server changes or new login are required:

```bash
pr-rerun alias set pypto hw-native-sys/pypto
pr-rerun alias set toolbox pypto-tools/pto-toolbox
pr-rerun 123 --repo pypto
pr-rerun 456 --repo toolbox
pr-rerun alias list
pr-rerun alias remove toolbox
```

Aliases are case-sensitive and stored per OS user in `~/.config/pr-rerun/aliases.json`
(or under `XDG_CONFIG_HOME`). They contain only full repository names, never tokens,
and survive login/logout. `--repo`, `PR_RERUN_REPOSITORY`, login and policy commands
accept aliases. Login saves the expanded repository name, so remapping an alias
later does not silently change the saved default. Previews and confirmation still
show the full repository name. Server-side grants and author checks remain unchanged.

## Official repository aliases (server and client 0.3.2)

A repository maintainer can publish official aliases for that repository:

```bash
pr-rerun alias set pypto hw-native-sys/pypto --official
pr-rerun alias list --official
pr-rerun 123 --repo pypto
pr-rerun alias remove pypto --repo hw-native-sys/pypto --official
```

Official aliases are the `aliases` list in the repository policy. They are
case-sensitive, unique across the server, limited to 20 per repository and updated
with the same authorization, revision checks and audit history as other policy
fields. A conflicting assignment returns an error and leaves both policies intact.
Changes invalidate existing previews. Removal requires a full repository name;
maintainers cannot redirect another repository's alias. List/resolve endpoints
require login and expose only enabled repositories available to that account.
Disabled repositories retain their reserved aliases until a maintainer removes them.

Resolution order: a full `owner/repo` name, then a local alias, then an official
alias from the selected server. Local aliases override matching official names;
use `alias list --official` to inspect server definitions. The CLI prints the
expanded name, and previews and confirmation always show the full repository.
Official aliases are resolved after authentication, including during GitHub login,
and are never cached across servers. Saved defaults retain the expanded name.
Existing local-only alias commands continue to work without server access.

Upgrade the server as well as clients for official aliases. Back up the stopped
database before switching server versions; after policies acquire the new `aliases`
field, rollback to 0.3.0/0.3.1 requires restoring the corresponding backup.
