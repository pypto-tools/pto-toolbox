# PR Rerun Service

An independently deployed GitHub Actions rerun service for multiple repositories.
An administrator runs one server; users log in through GitHub once, then use
`pr-rerun 123` to preview and confirm reruns of **their own open PRs**.

English | [中文](README.zh-CN.md)

Current version: **0.9.1**. Use GitHub App mode for per-repository authorization
and policies without manually distributing tokens. Legacy single-repository
`gh`/token deployments remain supported.

| Task | Guide |
| --- | --- |
| Install or update shared clients on one or more machines | [Standalone clients](docs/client-releases.md) |
| Log in, select a repository and confirm reruns | [Contributor usage](#contributor-usage) |
| Register an App, enroll repositories and manage policies | [App and repository policies](docs/multi-repository.md) |
| Upgrade the server, check status and troubleshoot | [Operations](docs/operations.md) |
| Legacy deployment, attribution and request-result semantics | [Legacy mode and protocol guide](docs/README.md) |

Server 0.9.0 adds opt-in [PR comment commands](docs/comment-commands.md): the PR author can post `/ci rerun` to request the same verified reruns. No CLI login or bot replies are required.

Server 0.8.1 supports PyPTO comment-triggered **CI Full**, with an explicit
[`pypto_ci` repository policy](docs/multi-repository.md#pypto-comment-triggered-full-ci-server-081).
Upgrade the API and collectors before enabling it.

## Behavior

- Ordinary CI failures rerun failed jobs. The configured Codex Review workflow
  reruns all jobs in that workflow, after verifying its PR identity artifact.
  Completed runs qualify when they contain failed, timed-out or cancelled jobs.
  The preview lists failed and cancelled jobs separately.
- GitHub device login verifies an immutable user ID and issues a persistent service
  session. Contributors need neither repository write access nor a `gh` login.
- In App mode, the server obtains temporary installation credentials restricted to
  the target repository. The App key and GitHub tokens are never sent to clients.
  Legacy mode uses an administrator's saved `gh` login or dedicated GitHub token.
- Every rerun requires a preview, explicit confirmation, and server-side state
  revalidation. There is no `--yes`, merge, delete, cancel, or workflow-dispatch API.
- Remote listeners bind to the local NetBird IP **and interface**. A separate
  loopback listener serves clients on the server host. Discovery failures abort startup.


Fork CI runs with empty GitHub PR links are supported only when source identity
and the complete branch history uniquely identify the requested PR. Reused or
ambiguous branches remain excluded; checks run again before each rerun.

## Server setup

The server requires Python 3.10+, Linux, OpenSSL, a connected NetBird kernel
interface, `ip` from iproute2, and HTTPS connectivity to GitHub. From
`pto-toolbox/pr-rerun-service`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock .
source .venv/bin/activate
```

Then follow [App registration and enrollment](docs/multi-repository.md) to configure
the App, database and policies, and adapt `deploy/pr-rerun-app.service` for systemd
boot startup. Replace the template's accounts, paths and App ID; keep local
configuration out of Git. For `gh`/token deployments, use the
[legacy guide](docs/README.md).

Allow the intended peers to reach the service's TCP port (8080 by default) in
NetBird policies. The server binds only to NetBird and loopback; Docker and public
inbound access are unnecessary. Running `pr-rerun-server` directly stays in the
foreground; boot startup requires an installed, enabled systemd service.
Additional machines need only the client connecting to the existing service.

## Shared client installation

Use the [standalone client release guide](docs/client-releases.md). GitHub Releases
provides ARM64 and x86_64 packages with their own Python runtime; the bootstrap
installer works with system Python 3.9+. Install once per machine, then update with
`sudo pr-rerun-update`. Each user keeps their own login and aliases.

For developers who need a source-based installation, the legacy
`deploy/install-shared-client.sh` still installs a locally built wheel using
`/usr/bin/python3` (3.10+). It is not required for standalone releases.

## Contributor usage

Use your own normal OS account, **without sudo**. The address below is an example;
replace it with the service host's NetBird IP supplied by the administrator.

```bash
pr-rerun login --netbird-server http://100.64.0.10:8080 --repo pypto
# Complete GitHub device authorization; pypto is a configured official alias.

# Subsequent invocations, including in new terminals:
pr-rerun 123
pr-rerun 10 11 12 --repo simpler
pr-rerun 456 --repo another/project
pr-rerun repos
# Optional: clear this user's saved login
pr-rerun logout
```

`--repo pypto` sets the default repository for future bare PR numbers. It accepts a
full `owner/repo` name or an existing alias; it does not grant access. Use `--repo`
on an individual rerun to select another enrolled repository without changing the
saved default. With `access: "authenticated_users"`, users need no individual grant,
but must still own the PR. Other policies may require explicit access.

Check the preview, then type `y` at `Confirm? [y/N]`; Enter cancels. No eligible
failed runs means no confirmation prompt. `accepted` means GitHub accepted the
request, not that CI passed. Use the Actions link to follow the run, or
`pr-rerun status rr_REQUEST_ID` to inspect the service submission state. Use
`pr-rerun 123 --verbose` to show excluded runs when troubleshooting.

Client 0.8.0+ accepts multiple PR numbers in one repository, such as
`pr-rerun 10 11 12 --repo simpler`. Each PR is independently previewed and requires
its own `y` confirmation. Enter or `n` skips that PR; Ctrl-C or EOF stops the
remaining PRs. Duplicates are removed in input order, and all numbers are validated
before credentials or requests. Ineligible PRs and request errors are reported
while the batch continues. A final table includes each result and request ID.
A lost/interrupted confirmation is never retried automatically; inspect its ID.
Exit 0 means all unique PRs were accepted; 1 means a decline or interruption;
2 means an error, ineligible PR, partial submission or uncertain confirmation.
Existing server versions need no update; repository limits still apply per PR.

Login saves the origin and token in `~/.config/pr-rerun/client.json` (or under
`XDG_CONFIG_HOME`) with directory/file modes `0700`/`0600`. It checks authentication
without creating a rerun. A saved token is only reused for its original server.
One OS account stores one login; contributors sharing an OS account share that login.

## AI analysis over MCP (0.7.0)

The client includes a read-only MCP adapter using your saved login. Configure your
AI host to launch `pr-rerun mcp --repo pypto` via stdio; it can query repositories,
CI statistics and error excerpts. Existing 0.6.0 servers need no update.
See [MCP setup and permission boundaries](docs/mcp.md).

## CI error excerpts (0.6.0)

Error excerpts require a separate per-repository opt-in and default to maintainer
access, independently of aggregate statistics. Upgrade the server and client,
then configure the independent collector. Existing logins remain valid.

```bash
pr-rerun errors --repo pypto --days 30
pr-rerun errors --repo pypto --days 30 --group
pr-rerun error <error-ID> --repo pypto
```

See [collection, access policy, limits and deployment](docs/ci-errors.md).

## CI statistics (0.5.0)

After a maintainer enables analytics and the operator collects data, use your
existing login:

```bash
pr-rerun stats --repo pypto --days 7
pr-rerun stats --repo pypto --days 30 --sort failure-rate
pr-rerun stats --repo pypto --json
```

Reports show first-attempt failures, rates, failed steps and rerun recoveries,
with event, head branch, workflow path and exact job filters. Queries never rerun
jobs; cancelled/skipped jobs do not enter the failure-rate denominator.
Upgrade both server and client and configure the separate collection timer.
No new login or App permission is required. Analytics are disabled by default;
viewing defaults to administrators and can be opened to repository users.
See [configuration, collection and metric definitions](docs/ci-statistics.md).

## Repository inventory (0.4.1)

```bash
pr-rerun repos
pr-rerun repos --json
```

Lists service enrollment, including repositories without aliases. Service admins
see all repositories, including disabled ones; repository maintainers see their
disabled repositories too. Other users see only enabled repositories they may
access. This does not check current GitHub authorization or bypass PR ownership.
Requires an App-mode server upgraded to 0.4.1; a client-only update is insufficient.

On the service host, with the running service's App/database environment loaded:

```bash
pr-rerun-admin installations
pr-rerun-admin installations --json
```

Compares live App installations/repositories with local enrollment: `enrolled`,
`not_enrolled`, `identity_changed`, and service-only entries whose access is
missing or unknown. Suspensions and incomplete queries are explicit; incomplete
inventory exits 1, configuration/API errors exit 2. JSON includes `complete`.
The query opens the existing database read-only and creates only temporary
metadata-read installation tokens; it never enrolls repositories or reruns jobs.
The admin command stays on the service host and is not in the standalone client.

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

## Service administrators (server 0.3.3)

Service administrators can manage policies and official aliases for every enrolled
repository, including its maintainer list. Repository maintainers still manage
only their assigned repositories. Roles use immutable GitHub user IDs and are
separate from repository policies; neither a policy update nor GitHub login can
grant a service administrator role.

The server's database owner grants the role locally after the account has logged
in through GitHub (or been verified with `issue`). Set `PR_RERUN_DATABASE` to the
server database, inspect `users`, and use the verified numeric ID:

```bash
pr-rerun-admin users
pr-rerun-admin service-admin grant 123456
pr-rerun-admin service-admin list
pr-rerun-admin service-admin revoke 123456
```

The commands require local access to the private server database and do not need
GitHub credentials. Grants and revocations record the target GitHub ID, local OS
UID and time. There is no HTTP endpoint for granting this role. After granting,
the administrator uses their existing GitHub login on a 0.3.2+ client:

```bash
pr-rerun alias set pypto hw-native-sys/pypto --official
pr-rerun alias list --official
```

Policy changes retain conflict checks, revision checks and the actual GitHub actor
in audit history. Role removal is checked again in the policy update transaction.
Service administrators can discover enabled repositories' aliases; rerun access,
PR authorship and explicit confirmation still follow each repository's rules.
Revoking service access also blocks administrators. Explicitly revoke the role to
remove it permanently; re-enabling the same identity otherwise retains the role.

## Query latency and progress (0.3.4)

The client immediately reports when it is checking the PR and workflows. Since
client 0.3.6, an interactive terminal shows a single-line spinner with elapsed time;
it clears before the preview or confirmation prompt. Confirmation uses the same
spinner while revalidating and submitting. Redirected output and dumb terminals
receive plain start/end messages without animation or repeated polling lines.
Progress goes to stderr and describes the request, not individual server substeps.

The server inspects independent runs with at most four concurrent readers per
plan. Complete branch history is fetched once per source branch within that plan.
No identity result is shared between preview, confirmation and the final per-run
check. Run selection and exclusions keep their original ordering; a failed lookup
rejects the entire plan. Rerun writes remain sequential and are never retried
implicitly. The service logs plan lookup duration and run counts without credentials.

Upgrade clients for progress output, and the server for concurrent lookups. Explicit
confirmation remains required whenever eligible runs are found; no eligible runs
means no confirmation and no rerun. GitHub/network latency still affects response
time, and an accepted submission does not wait for CI completion.

## Short confirmation (client 0.3.5)

After showing the full repository, PR, author and planned reruns, the client asks
`Confirm? [y/N]`. Type `y` to submit; Enter, `n` or any other input cancels.
Uppercase `Y` and surrounding whitespace are accepted. No need to copy the full
repository/PR identity. The client sends the original preview's confirmation and
ticket only after this explicit approval, so server validation is unchanged.
This is a client-only update and does not require a server restart.

## Compact preview (client 0.3.6)

Excluded runs are hidden by default. The preview shows the PR and proposed reruns;
if none qualify, it reports that nothing will be submitted. To inspect why runs
were skipped, use `pr-rerun 123 --verbose` (also works with `--repo`). This only
changes client output: ownership checks, workflow selection and confirmation
remain unchanged. Upgrade the client; no server restart is needed.

## GitHub login connectivity (server 0.3.7)

Device login requires the server to reach `github.com/login/device/code` and
`github.com/login/oauth/access_token`, as well as `api.github.com/user`. Working
repository API access alone does not prove that login is reachable. A failure
before the device code or during authorization polling can be a server-side
connection problem, even when the client can reach the service over NetBird.

If direct login connections fail, the operator can explicitly configure a trusted
HTTP(S) proxy using `PR_RERUN_LOGIN_PROXY`, for example
`http://127.0.0.1:7890`, in the server's environment and restart the service.
Use a protected systemd EnvironmentFile if the proxy URL contains credentials.
The proxy must be reachable from the server process. This setting applies only
to device authorization and identity lookup; rerun traffic and client-to-service
traffic keep their existing routes. Ambient HTTP_PROXY/HTTPS_PROXY/ALL_PROXY
variables remain ignored, redirects remain disabled, and TLS stays verified.
SOCKS proxies are not supported by this setting.

Login failure logs identify the operation and HTTP status or exception class,
without logging authorization codes, tokens, proxy credentials or response bodies.
Routine HTTP library request logs are suppressed to avoid signed artifact URLs
appearing in the service journal. Existing clients can use this server update
without reinstalling or clearing their saved login.
