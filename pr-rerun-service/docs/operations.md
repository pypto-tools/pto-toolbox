# Operations and troubleshooting

English | [中文](operations.zh-CN.md)

This guide covers the 0.4.1 App deployment. For initial setup and enrollment see
[repository policies](multi-repository.md); for client installation, migration,
offline updates and rollback see [client releases](client-releases.md).
All addresses, paths and account names in these guides are examples.

## What runs where

| Component | Location | Updates |
| --- | --- | --- |
| `pr-rerun`, `pr-rerun-update` | Each client machine, installed once for all OS users | `sudo pr-rerun-update --version 0.4.1` on each machine |
| `pr-rerun-server`, `pr-rerun-admin` | Service host only, in the server Python environment | Upgrade the service wheel and restart the service |
| Login and local aliases | Each OS user's `~/.config/pr-rerun`, or `XDG_CONFIG_HOME` | Preserved by client updates |
| Users, sessions, enrollment, rules, official aliases and request history | Service SQLite database selected by `PR_RERUN_DATABASE` | Preserve and back up during server upgrades |
| App key and service settings | Protected files/environment on the service host | Preserve; never distribute to clients |

The service host may also have the shared client installed. Its client and server
versions are independent. Updating clients on three machines does not require
three servers or a new App installation. Each user logs in without sudo; users
sharing an OS account also share its saved login.

## Check versions and service health

```bash
type -a pr-rerun
pr-rerun --version
/usr/local/bin/pr-rerun --version
systemctl is-active pr-rerun.service
systemctl is-enabled pr-rerun.service
curl --fail --noproxy '*' http://127.0.0.1:8080/healthz
```

Run systemd and loopback checks on the service host, adapting the unit name/port.
`healthz` reports process health, not GitHub connectivity or a version number.
To check server code, inspect the configured `ExecStart` locally and run its actual
Python environment with the same module path:

```bash
# Example only: use the Python/module path from this deployment's ExecStart.
/opt/pr-rerun-service/.venv/bin/python -I -c 'from pr_rerun.version import VERSION; print(VERSION)'
```

If the launcher injects a versioned `app` directory, preserve that selection when
checking; the underlying Python environment's installed package may differ.
Do not post environment dumps or credentials when asking for support.

With a saved login, `pr-rerun repos` verifies service enrollment visibility.
`pr-rerun-admin installations`, run as the service OS user with its App/database
settings loaded, checks current GitHub App authorization. Neither triggers reruns.
The first query needs both client and App server 0.4.1. The admin command must also
come from 0.4.1; it is not part of the standalone client.

## Upgrade an existing server

Client updates preserve logins, aliases and repository registrations. Server
upgrades also preserve them when the same database and settings are retained.
Sessions still expire or may be revoked independently. Do not initialize an empty
database, rotate tokens with `issue`, or reinstall the App as an upgrade step.

1. Obtain the service wheel from the trusted `pr-rerun-v0.4.1` release. Prepare a
   separate Python 3.10+ environment as the service OS user, outside the live
   environment. Install the wheel, resolve dependencies, and run `pip check`.
   The standalone archive's embedded Python is for the client, not server setup.

   ```bash
   # Example staging directory; use a new directory, not the live environment.
   python3.10 -m venv "$HOME/pr-rerun-server-0.4.1"
   "$HOME/pr-rerun-server-0.4.1/bin/python" -m pip install ./pr_rerun_service-0.4.1-py3-none-any.whl
   "$HOME/pr-rerun-server-0.4.1/bin/python" -m pip check
   "$HOME/pr-rerun-server-0.4.1/bin/pr-rerun-server" --help
   "$HOME/pr-rerun-server-0.4.1/bin/pr-rerun-admin" installations --help
   ```

2. Record the current service launcher, unit and drop-ins; prepare the new
   `ExecStart` using the staged environment. Preserve the service OS user, HOME,
   database path, App settings, private key path, login proxy, NetBird binding and
   port. The generic installer refuses to replace a different existing unit;
   updating an existing deployment requires reviewing its actual unit/drop-ins.
3. During a quiet period, stop the service and back up its database with SQLite's
   backup facility into a private directory. Keep any WAL state intact until the
   backup is complete; copying only a live `.sqlite` file is not a reliable backup.
   Retain the old environment and unit configuration. Never run two servers on the
   same database, including a temporary validation server.
4. Apply the reviewed launcher/unit change, run `sudo systemctl daemon-reload`, then
   `sudo systemctl start pr-rerun.service`. Update any personal `pr-rerun-admin`
   launcher to the new environment too; updating only the unit leaves that command
   on its old code.
5. Check process health and versions, then run `pr-rerun repos` and
   `pr-rerun-admin installations`. Confirm enrollment, rules and official aliases
   remain present. These checks do not require submitting a PR rerun.

For failure recovery, stop the new service before switching launchers. Check schema
compatibility before running old code against the upgraded database. Restore a
database backup only as a deliberate recovery step: it discards later sessions,
policy changes and request records. Client `--rollback` does not roll back the server.

## Common problems

| Symptom | Check and next step |
| --- | --- |
| `repos` reports `Specify a positive PR number` | Old CLI is running. Follow the PATH diagnosis in the client release guide; do not clear login files. |
| `repos` asks for an App-mode server 0.4.1 | Upgrade the server, not just the shared client. |
| `pr-rerun-admin` is missing | Use the service host's server environment; the standalone client intentionally excludes it. |
| System Python is 3.9 or pip cannot download `build` | Clients use the standalone release; no wheel build or Python replacement is needed. For restricted networks use the documented proxy or offline installer. |
| Client cannot reach the service | Check NetBird connectivity and peer policy, service status, and the configured IP/port. Test loopback on the server first. Do not change binding to `0.0.0.0`. |
| `GitHub login is unavailable` | Check the server's device-login connectivity and `PR_RERUN_LOGIN_PROXY` setting described in the main README. A client download proxy does not configure server login. |
| Login unavailable after initial App setup | Check App Client ID and Enable Device Flow, then restart after changing server settings. No Members permission or public callback is required. |
| Repository absent from `repos` | Check enabled state and the user's access rules; service admins can see disabled repositories too. App installation alone does not enroll it. |
| `not_enrolled` in `installations` | App authorization exists; complete local enrollment using the repository guide. |
| Inventory incomplete / `unknown` | Restore GitHub connectivity and repeat the read-only query. Do not treat unavailable data as proof of revoked access. |
| PR ownership cannot be verified | Use `--verbose`. A missing/ambiguous PR association or Codex identity artifact remains ineligible; do not bypass attribution checks. |
| No confirmation prompt | No eligible failures means no submission. Otherwise a terminal and explicit `y` are required. |
| Submission outcome is unknown | Query `pr-rerun status rr_REQUEST_ID` before retrying. `accepted` means accepted by GitHub, not successful CI. |

Share versions, command paths, repository/request IDs and sanitized error messages
for diagnosis. Never share client login JSON, App keys, service tokens, or raw
signed artifact URLs. See the main README for the two inventory commands' visibility
and exit status semantics.
