# PR Rerun Service

An independent service that lets enrolled contributors request GitHub Actions
reruns for **their own open PRs**. The server uses the administrator's GitHub token.
Contributors receive separate service tokens and never receive that GitHub token.
No PyPTO checkout, package, or compiler is needed. The server needs `gh` only
when explicitly reusing a GitHub CLI login.

[中文使用说明](README.zh-CN.md)

Version `0.2.2` verifies ordinary fork runs whose GitHub PR links are empty.
It checks source repository, branch, SHA, timestamps, and the complete PR branch
history before allowing a rerun. Restart the server after upgrading; existing
client logins and service tokens remain valid, and clients do not need an upgrade.

## Behavior

1. The contributor enters a PR number.
2. The service checks the contributor's immutable GitHub user ID against the PR
   author, then discovers the PR's current Actions checks.
3. The CLI displays the repository, PR, author, commit, target branch, failed jobs,
   and proposed operations.
4. The contributor types `hw-native-sys/pypto#123 @alice` exactly.
5. The service rechecks the PR and run attempts and requests the approved reruns.

Ordinary CI/Docs failures use `rerun-failed-jobs`. The configured Codex Review
workflow uses `rerun` (all jobs in that workflow run). Successful, cancelled,
skipped, approval-required, active, and more-than-30-day-old runs are excluded.
Timeout failures are included. A newer successful/active run supersedes an older
failed run of the same workflow.

The CLI requires a terminal and has no `--yes` option. A service cannot prove a
human actually read a preview: API callers can automate the two-step protocol.
Authorization, snapshot validation, and one-time execution are enforced server-side.

## Install and run

Python 3.10+ is required. NetBird serving requires Linux, a connected NetBird
client using a kernel interface, and `ip` from iproute2. macOS supports the CLI
and local-only testing. The server uses a local SQLite database
and a POSIX process lock. Run exactly one server process for each database.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -c requirements-dev.lock -e ".[dev]"

export PR_RERUN_DATABASE="$PWD/state/rerun.sqlite"
export PR_RERUN_CONFIG="$PWD/config.example.json"
read -rsp "GitHub token: " PR_RERUN_GITHUB_TOKEN
export PR_RERUN_GITHUB_TOKEN

# Resolve alice via GitHub and issue a random service token.
# This prints the service token once; deliver it through a trusted channel.
pr-rerun-admin issue alice

# Discover and bind the local NetBird interface; also allow this host's clients.
pr-rerun-server
```

By default the server obtains the local NetBird IPv4 address, verifies its kernel
interface, and binds both the address and interface for remote requests. A second
listener on `127.0.0.1:8080` serves clients on this host. There is no public/LAN
listener or IPv6 wildcard listener. Failed discovery, missing/down interfaces,
device-binding failure, or occupied ports abort startup without falling back.
Use `--local-only` for testing without any remote access.

### Reuse an existing gh login

If the OS user running the service is already logged in with `gh auth login`, use:

```bash
unset PR_RERUN_GITHUB_TOKEN PR_RERUN_GITHUB_TOKEN_FILE
pr-rerun-server --github-auth gh --host 100.64.0.10 --port 8080
```

Replace the example IP with the server's NetBird IP, or omit `--host` to discover
it. No manual token entry or copying is needed. If necessary, first run
`gh auth login --hostname github.com` as that OS user.

User provisioning can reuse the same login:

```bash
pr-rerun-admin issue alice --github-auth gh
```

Alternatively, set `export PR_RERUN_GITHUB_AUTH=gh` for both the server and `issue`.
The active `github.com` account is selected by default. Set `PR_RERUN_GITHUB_USER`
to pin a particular username when multiple accounts are stored.

The fixed `gh auth token --hostname github.com` subprocess captures the credential
without printing it, saving it in the service database, or returning it to clients.
Ambient `GH_TOKEN`, `GITHUB_TOKEN`, and enterprise token overrides are removed so
the saved login is used. Failure stops startup; the service never logs in,
switches accounts, or falls back to a different credential source. The credential
is retained in memory until shutdown. Restart after switching accounts, refreshing
the login, or revoking credentials.

An unattended service must run as the same OS user and have access to that user's
gh configuration and credential store. A different service user needs its own
login or a dedicated token. The GitHub account must have permission to rerun the
target repository's workflows. Contributors still use separate service tokens,
author-only PR authorization, and explicit confirmation.

Reference: [GitHub CLI auth token](https://cli.github.com/manual/gh_auth_token).

### Boot startup with systemd

`deploy/pr-rerun.service` is a generic template. Its example account is `rerun`,
with HOME `/home/rerun`, project `/opt/pr-rerun-service`, and database
`/var/lib/pr-rerun/rerun.sqlite`. These are placeholders, not an existing deployment.
Use an account with a saved `gh` login and repository rerun permission. You can
adapt the template to an existing account; the service must use that account's
HOME and credential store. Ensure the configured state directory is owned by
that account with mode `0700`, and the Python environment exists at `ExecStart`.

Make an ignored local copy first:

```bash
cp deploy/pr-rerun.service deploy/pr-rerun.local.service
```

Edit `User`, `Group`, `HOME`, all project/state paths, and `RequiresMountsFor`
in the local copy. No credentials belong in this file. Leave `--host` omitted
to discover the machine's NetBird address. For multiple GitHub accounts, add
`Environment=PR_RERUN_GITHUB_USER=YOUR_GITHUB_LOGIN` to pin the intended account.
Stop any manually started instance yourself before installing; the installer
never sends signals to arbitrary PIDs.

Then inspect and install the reviewed unit:

```bash
bash deploy/install-systemd.sh --dry-run deploy/pr-rerun.local.service
sudo bash deploy/install-systemd.sh deploy/pr-rerun.local.service
```

The installer verifies the unit and refuses to replace a different existing
system unit. Updating an existing unit requires administrator review. The
service starts at boot and retries failures every 15 seconds, including while
NetBird is unavailable. It does not fall back to a public listener.

```bash
systemctl status pr-rerun.service --no-pager
systemctl is-enabled pr-rerun.service
sudo journalctl -u pr-rerun.service -f
sudo systemctl restart pr-rerun.service
curl --noproxy '*' http://127.0.0.1:8080/healthz
```

`sudo systemctl stop pr-rerun.service` stops it but keeps boot startup.
`sudo systemctl disable --now pr-rerun.service` stops it and disables boot startup.
Preserve the configured database when restarting or upgrading.

### Use a dedicated token

Alternatively, configure a dedicated GitHub credential restricted to the target
repository. Grant **Actions: write**, **Checks: read**, and **Pull requests: read**.
Organization policy may require an administrator to approve a fine-grained PAT.
Do not use a broad personal token merely to work around organization restrictions.
A pre-issued GitHub App installation token also works but must be refreshed by
the operator; automatic App token minting is not implemented.

For unattended service management, set `PR_RERUN_GITHUB_TOKEN_FILE` to a protected
token file instead of `PR_RERUN_GITHUB_TOKEN`. Do not set both. The process identity
must be able to read that file. Token changes require a server restart.
Dedicated tokens and the `gh` source are mutually exclusive; select dedicated
tokens with `PR_RERUN_GITHUB_AUTH=token` or `--github-auth token`.
The service deliberately ignores ambient proxy/netrc configuration for GitHub
requests. Its host must have direct HTTPS access to GitHub and GitHub artifact
storage. Run the server from a trusted, reviewed project copy; do not run it from
PR-controlled code or mount a contributor workspace into it.

Use your supervisor (for example systemd) to keep `pr-rerun-server` running.
Keep the database on local persistent storage, not a network filesystem.
The unauthenticated `/healthz` endpoint reports process liveness only.
Credential readiness is checked when the first GitHub query is made.

## Shared client on one machine

An administrator can install one client for all OS users on a Linux machine.
The system `/usr/bin/python3` must be Python 3.10+ with `venv` and `pip` support.
From `pto-toolbox/pr-rerun-service`, build the wheel and then install:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements-dev.lock ".[dev]"
.venv/bin/python -m build --wheel
bash deploy/install-shared-client.sh --dry-run
sudo bash deploy/install-shared-client.sh
```

The installer creates `/opt/pr-rerun-client` with the system interpreter and
publishes `/usr/local/bin/pr-rerun`. Files are owned by root and readable/executable,
but not writable, by ordinary users. Runtime does not depend on the administrator's
personal conda environment. No server restart or database migration is needed.

When `dist/shared-client-wheelhouse/` is present, dependencies are installed
offline from it; otherwise they are installed from PyPI under `requirements.lock`
constraints. Offline dependencies must match the destination architecture and Python
version; generated wheel bundles are not included in this repository. This first-install script refuses to replace an existing runtime or
public command.

After the administrator installs once, each contributor uses their **own normal
OS account, without sudo**:

```bash
pr-rerun login --netbird-server http://100.64.0.10:8080
pr-rerun 123
```

There is no shared access token; login stays in each user's HOME/XDG configuration
directory. One OS account stores one login. If a personal installation takes
precedence in PATH, inspect it with `type -a pr-rerun` and select the shared
version explicitly with `/usr/local/bin/pr-rerun`. `/usr/local/bin` must be on
the user's PATH. This installation applies to one machine; each other NetBird
machine still needs its own installation.

## Contributor usage

Install this project on a Linux/macOS client and log in once:

```bash
pr-rerun login --server https://rerun.example.com
# Enter your personal SERVICE access token issued by the administrator.

# Subsequent invocations, including in new terminals:
pr-rerun 123

# Inspect a request after a timeout or lost response.
pr-rerun status rr_0123456789abcdef0123456789abcdef
```

NetBird clients use `pr-rerun login --netbird-server http://100.64.0.10:8080`.
On the service host, plain `pr-rerun login` defaults to `http://127.0.0.1:8080`.
Login performs a read-only authentication check through the existing API before
saving anything. It makes no GitHub calls, creates no preview, and triggers no
rerun. No server upgrade or restart is required.

The server origin, NetBird mode, and service token are saved per OS user in
`~/.config/pr-rerun/client.json`, or `$XDG_CONFIG_HOME/pr-rerun/client.json` when
an absolute `XDG_CONFIG_HOME` is set. The directory has mode `0700` and the file
has mode `0600`. Storage is protected by filesystem permissions, not encryption;
do not share or commit this file. Contributors should have separate OS accounts
or HOME directories. The client rejects unsafe permissions, foreign ownership,
and links, and updates the file atomically.

```bash
# After a token is rotated/revoked, enter the replacement token:
pr-rerun login

# Remove the locally saved login:
pr-rerun logout
```

Failed login leaves the previous configuration intact. Logout does not revoke
the token on the server or affect other machines. Every rerun still requires
PR inspection and exact manual confirmation, and author-only checks still apply.

Endpoint precedence is command-line arguments, environment, saved endpoint, then
loopback. `PR_RERUN_ACCESS_TOKEN` overrides the saved token. Otherwise, the saved
token is reused only for the same scheme, host, and port; a different destination
prompts for another token. A temporary endpoint override neither sends the saved
token to another origin nor changes the stored login.
`login` uses the environment token or prompts for a new one. Clear stale overrides
with `unset PR_RERUN_ACCESS_TOKEN PR_RERUN_URL PR_RERUN_NETBIRD_URL` if needed.
`logout` cannot clear variables in the parent shell.

Without a saved login, direct `pr-rerun` commands still prompt for a service token.
Never give contributors `PR_RERUN_GITHUB_TOKEN`.
Ordinary remote connections require HTTPS. HTTP is accepted on loopback or for
an explicitly configured NetBird endpoint, as described below.
The client rejects redirects and escapes terminal control characters in PR titles.

## Access from other servers through NetBird

When both hosts are NetBird peers, the service accepts remote traffic only
through its host's NetBird interface.
NetBird encrypts traffic between peers with WireGuard, so HTTP over a verified
NetBird path does not require an additional HTTPS proxy or Docker.
This setup addresses the **service host's own NetBird IP**.

Find the service host's address:

```bash
netbird status --ipv4
```

Keep the database, configuration, and GitHub credential environment variables
from the installation steps. Replace the example `100.64.0.10` below with the
actual address and start the server:

```bash
pr-rerun-server --host 100.64.0.10 --port 8080
```

`--host` is an optional assertion: it must exactly match the local IPv4 reported
by NetBird. Wildcards (`0.0.0.0`, `::`), public/LAN IPs, other peers' addresses,
and hostnames are rejected. Omit it to discover the address automatically.
The remote socket also uses Linux `SO_BINDTODEVICE`, so packets received on
other interfaces are not delivered to it. Address binding is not the only guard.

Allow the intended peer groups to reach the service peer on **TCP 8080** in
NetBird access policies, and allow that traffic through the host firewall.
No public inbound port or global firewall modification is required for interface
binding. Do not add a public reverse proxy or port forward. NetBird policies
determine which peers can connect; service tokens still identify contributors.

Install this project on another NetBird-connected server, then configure:

```bash
# Check connectivity and process liveness without triggering any GitHub reruns.
curl --noproxy '*' http://100.64.0.10:8080/healthz

# Verify and save the endpoint and personal service token once:
pr-rerun login --netbird-server http://100.64.0.10:8080

# Subsequent usage:
pr-rerun 123
```

A one-off endpoint override is also available:

```bash
pr-rerun --netbird-server http://100.64.0.10:8080 123
```

**Clients on the service host should use loopback**, without needing to configure
the NetBird destination address:

```bash
pr-rerun --server http://127.0.0.1:8080 123
```

Alternatively, save loopback with `pr-rerun login --server http://127.0.0.1:8080`.
Loopback requests have the same token, authorship, and confirmation requirements.

`PR_RERUN_NETBIRD_URL` / `--netbird-server` asserts that the operator or user has
verified the destination is reached through NetBird. The program does not
automatically attest VPN routing; a private IP alone does not establish encryption.
This option accepts literal IPs in `100.64.0.0/10`, RFC 1918 IPv4 networks, or IPv6
ULA space. It rejects hostnames, public IPs, URL credentials, paths, and queries.
Use `PR_RERUN_URL=https://...` for a hostname with an HTTPS endpoint inside NetBird;
that endpoint must also restrict access to NetBird.

Do not set both endpoint environment variables. An explicit command-line endpoint
takes precedence over them. Without the NetBird option, remote HTTP is still
rejected. NetBird supplies network access and encryption; service tokens continue
to identify contributors and enforce PR authorship. Preview, confirmation, state
revalidation, and audit behavior are unchanged.

References: [How NetBird works](https://docs.netbird.io/about-netbird/how-netbird-works),
[groups and access policies](https://docs.netbird.io/manage/access-control/manage-network-access).

## Manage users

```bash
pr-rerun-admin users
pr-rerun-admin issue alice       # Rotates the old token and invalidates its pending previews.
pr-rerun-admin revoke 12345678    # Immutable GitHub user ID, shown by "users".
```

`users` and `revoke` require only `PR_RERUN_DATABASE`. `issue` also requires the
GitHub credential, because it verifies the login's ID using GitHub.
The database stores SHA-256 hashes of random 256-bit tokens, never plaintext tokens.
The database and containing state directory must be writable only by the operator.
Claims and audit history persist across restarts; retain the database when upgrading.

## HTTP API

Use `Authorization: Bearer <service-token>` and `Content-Type: application/json`.
Bodies are limited to 4096 bytes. Unknown fields and coercions are rejected.

| Method | Route | Input / result |
| --- | --- | --- |
| POST | `/api/reruns/preview` | `{"pr":123}` → PR, planned runs, skipped explanations, request ID, ticket |
| POST | `/api/reruns/confirm` | `{"ticket":"…","confirmation":"hw-native-sys/pypto#123 @alice"}` |
| GET | `/api/reruns/{request_id}` | The authenticated author's request status |

A preview with no eligible runs returns `eligible: false` and no ticket.
An eligible ticket expires after five minutes. Creating a newer preview supersedes
the previous unconsumed preview for that user.
Repeated confirmation of an already-consumed ticket returns the existing request.
The request ID is known before confirmation, so a lost response can be inspected.
Status responses never contain a ticket or credential.

Only the server selects the repository, run IDs, mode, and workflow rules.
Clients cannot provide URLs, workflow inputs, Git refs, debug options, or HTTP methods.
There are no cookie sessions, CORS policy, shell execution, workflow dispatch,
approval, comment, cancellation, deletion, or merge endpoints.

## Results, concurrency, and recovery

- `accepted`: GitHub accepted the request, **not** proof the jobs passed.
- `rejected`: validation failed or GitHub explicitly rejected the operation.
- `partial`: some requests were accepted and another was rejected.
- `unknown`: a request may have been accepted but no reliable response was received.
- `not_attempted`: the batch stopped before this operation was sent.
- `interrupted`: the process restarted during an unfinished request; inspect each run.
- `rerun_observed`: a later read found a higher attempt number. This does not prove
  which person or service initiated that attempt.

The service serializes confirmation consumption and claims each
`(repository ID, run ID, attempt)` in SQLite before sending writes. Concurrent
confirmations, multiple previews, and restarts cannot resubmit a claimed attempt.
Definite rejections and operations never sent release their claims. Uncertain
POSTs retain claims and are **never automatically retried**. Status lookups can
observe a newer GitHub attempt but do not initiate reruns.

If an unknown result never advances on GitHub, an operator must investigate it;
this version intentionally provides no force-retry/reset endpoint. A maintainer
can inspect and rerun directly on GitHub if appropriate. Do not delete the database
to bypass uncertain claims.

Default limits are one preview/status lookup per user per five seconds, and one
confirmation per user and PR per 60 seconds. Replayed confirmations return stored
results without submitting writes. Put connection, TLS, and unauthenticated
traffic limits at the reverse proxy. JSON audit events include actor ID, request
ID, run ID, operation, and outcome; request records link those events to the PR.
No automatic retention cleanup is implemented; operators should monitor disk usage.

## PR identity and limits

Ordinary workflows first use explicit PR associations matching number, source
repository, head SHA, target repository, and base branch. GitHub can return empty
PR links for fork runs. For those runs only, the service additionally verifies
`pull_request` event type, source repository ID, source branch, and current SHA.
A paginated GraphQL lookup of that branch across **open, closed, and merged PRs**
must identify exactly one PR from that source repository, matching the requested
PR, current target branch/repository, and current SHA. The PR must predate the run.
All pages must be read successfully, and the check is repeated before each POST.

Reused branches and branches used for multiple PRs remain ambiguous and are
excluded, even if another PR is closed or has a different SHA/base branch.
Explicit links to another PR are never overridden. Matching a SHA or branch alone
is not sufficient evidence. Codex runs still require their trusted artifact.

The `codex_manifest` rule supports PyPTO's existing trusted `pull_request_target`
workflow. It reads the newest unexpired `codex-target-{run}-{attempt}` artifact,
or the legacy `codex-context-{run}-{attempt}` artifact, and validates repository,
PR number, head SHA, and base ref. Zip contents are bounded, never extracted, and
signed artifact redirects receive no GitHub Authorization header.
The rule depends on that artifact contract, not on importing or cloning PyPTO code.
If preparation failed before a manifest was uploaded, ownership may be unprovable
and the service will exclude the run.

The service revalidates state immediately before each POST, but GitHub has no
conditional rerun transaction that locks a PR revision. A push can race the final
check. Only the already-confirmed run ID is sent; the service does not substitute
a new run. GitHub retains the original event SHA/ref, while a workflow that itself
fetches mutable state (including the Codex context collector) can observe later data.

GitHub grants `Actions: write`, not a rerun-only token scope. The service restricts
what its clients can request; the trusted host and GitHub credential remain a
security boundary. Rerunning a workflow executes its normal effects, including
any comments or approvals the workflow itself performs. Reruns use the original
workflow actor's privileges. The PAT owner is the account requesting the rerun;
the service audit trail identifies the contributor who requested it.

## Optional in-container testing

The following commands require Docker with the Compose v2 plugin installed.
This configuration uses `--local-only` for testing inside the container and
publishes no host ports. Run the NetBird service directly on Linux as shown above.

```bash
export PR_RERUN_TOKEN_FILE=/absolute/path/to/protected/github-token
docker compose -f deploy/compose.yml up -d --build
docker compose -f deploy/compose.yml exec rerun pr-rerun-admin issue alice
docker compose -f deploy/compose.yml exec rerun pr-rerun 123
docker compose -f deploy/compose.yml logs -f rerun
```

The image runs as UID 10001. Ensure that UID can read the mounted token file;
Compose file-backed secrets may preserve host ownership/mode instead of applying
the requested container ownership. Restrict host access accordingly.
The named volume retains SQLite state. No listener is exposed to the host,
public networks, or NetBird. The container filesystem is read-only
except the state volume and temporary directory.

## Development and validation

```bash
pip install -c requirements-dev.lock -e ".[dev]"
pytest
ruff check src tests
ruff format --check src tests
pyright
python -m build
```

Tests simulate GitHub through HTTPX transports and exercise the real HTTP API,
SQLite transactions, CLI confirmation, run attribution, pagination, artifact
redirects, credential revocation, concurrency, restart recovery, and timeout paths.
They do not use real credentials or trigger GitHub reruns.
`requirements.lock` pins the validated runtime dependencies;
`requirements-dev.lock` additionally pins the development tools. The container
uses the runtime lock. Dependency upgrades should update these files and rerun tests.

Before production use, perform a controlled live test using the configured PAT
and one author-owned PR, including a fork PR. Verify the actual workflow paths and
artifact contract. Automated tests do not replace that deployment-specific check.

References:

- [GitHub workflow rerun endpoints](https://docs.github.com/en/rest/actions/workflow-runs)
- [GitHub rerun semantics and 30-day limit](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)
- [GitHub Checks API and fork PR association](https://docs.github.com/en/rest/checks/runs)
