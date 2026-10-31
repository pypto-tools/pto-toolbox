# Repository policies and GitHub App deployment

[中文](multi-repository.zh-CN.md)

Version 0.3.0 adds a separate App mode. Existing `gh`/token deployments remain
single-repository and retain their existing configuration, database and logins.
App mode requires Linux and OpenSSL (RSA signing); it does not require public
inbound access, OAuth callbacks or webhooks. Client connections still use NetBird.

## Design and authority

The platform operator controls the App key and initial repository enrollment.
An organization owner installs the App using **Only select repositories**; this
service deliberately rejects installations with **All repositories** selected.
The platform operator checks the repository owner's approval and designates
initial repository maintainers. An installation alone does not enroll a repository
or grant every service user access. Local platform operators are trusted across
all repositories; remote maintainers can manage only their own repository policies.

Each enrollment pins the full repository name, immutable repository ID and App
installation ID. Each policy controls enabled state, numeric GitHub user IDs,
maintainers, draft allowance, base-branch glob patterns, workflow paths, rerun
modes, cooldown and quotas. Repository maintainers can delegate management and
usage to already provisioned service identities. Maintainers are also ordinary
rerun users: they can rerun only their own open PRs, never another author's PR.

User grants are controlled per repository. The default `access: "explicit_users"`
requires a grant in `users` or `maintainers`. The opt-in `access: "authenticated_users"`
allows any authenticated service identity to rerun its own PRs in this repository. Organization membership is not queried;
no organization Members permission is required. A service token alone does not
grant access to every enrolled repository.

Set `allow_drafts: true` and `base_branches: ["*"]` to allow drafts targeting any
branch. Set `cooldown_seconds: 0` and both `max_requests_per_user_per_day` and
`max_active_requests_per_pr` to JSON `null` to disable usage quotas and cooldowns.
Author checks, exact confirmation and duplicate run-attempt protection still apply.
Pending workflow approvals are not approved by this service.

Policies are strict JSON data, not scripts, shell commands, API URLs or credentials.
PR branch files cannot change policy. Active workflow paths are checked against
GitHub at enrollment and policy updates. Ordinary workflows must use the
`pull_request` identity strategy and `failed` mode. The only `all` exception is
`codex_manifest`, which requires a compatible trusted `pull_request_target`
workflow that produces the identity artifact described in the operator guide.
A matching job name alone is not an identity proof. Workflows without a supported
attribution strategy remain unsupported even if their names resemble CI or Codex.

Requests bind repository, PR, head, run attempts, workflow modes and policy revision.
Confirmation selects its repository from the stored ticket, not user input.
Policy changes invalidate outstanding previews, even if a later revision restores
identical settings. Authorization is checked again before every rerun. App
installation authorization is rechecked before each write; cached installation
tokens never substitute for that check. Tokens expire and are refreshed automatically,
and each token request is scoped to one repository and the requested permissions.
A revoked or suspended installation, removed repository, changed installation ID,
or replaced/transferred repository fails closed. As with any external API, a change
concurrent with the final GitHub POST cannot retract an already accepted request.

Policy updates use an expected revision (compare-and-swap), and retain full policy
snapshots, actor ID and time in SQLite. Initial enrollment uses actor `0` for the
local platform operator. HTTP clients cannot replace enrollment IDs or the App key.
Reinstallations and repository renames/transfers require platform migration; there
is no automatic rebinding to a new installation or a same-named replacement.

Cooldown is per repository/user and repository/PR. The daily quota counts reserved
submission batches per repository/user per UTC day, including attempts later
rejected upstream. A deduplicated retry does not consume a second slot. The active
limit bounds submissions that are validating/submitting or have uncertain outcomes;
it does not count all running CI jobs. Reconcile uncertain outcomes with `status`
before retrying. The existing one-attempt claim still prevents duplicate reruns.

## Register and install the App

Register under your account or the organization that will operate the service:
Settings → Developer settings → GitHub Apps → New GitHub App.

- Choose a unique name and use the toolbox repository as Homepage URL.
- Choose **Any account** so other organizations can install it.
- Disable Webhook **Active** and leave callback/setup URLs blank. Enable **Device
  Flow** for CLI login; keep request-user-authorization-during-installation disabled.
- Repository permissions: **Actions: Read & write**, **Checks: Read-only**,
  **Contents: Read-only**, **Pull requests: Read-only**, and default Metadata read.
- No organization permissions or webhook subscriptions are needed.
- Create an RSA private key. Store it outside this checkout, owned by the service
  OS user with mode `0600` or `0400`; symbolic and hard links are rejected.
- Ask the repository's organization owner to install the App and explicitly select
  the repositories to authorize. Ordinary repository write access alone does not
  necessarily allow installation or approval.

GitHub Actions write permission includes operations beyond reruns. The service's
GitHub execution client permits only the two rerun POSTs plus fixed read-only
GraphQL queries. The separate App authentication client mints installation tokens;
policy administration only updates local service policy. Neither user interface
exposes arbitrary GitHub API calls. Protect the App private key: a compromised
platform operator can exercise the App's broader granted permissions.

References: [registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app),
[installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app),
[rerun permissions](https://docs.github.com/en/rest/actions/workflow-runs#re-run-failed-jobs-from-a-workflow-run).

## Bootstrap a repository

Install 0.3.0 in an isolated server environment following the main README. Set the
following in the service's private environment; use an absolute key path:

```bash
export PR_RERUN_GITHUB_AUTH=app
export PR_RERUN_APP_ID=123456
export PR_RERUN_APP_CLIENT_ID=YOUR_APP_CLIENT_ID
export PR_RERUN_APP_PRIVATE_KEY_FILE="$HOME/.config/pr-rerun/app.pem"
export PR_RERUN_DATABASE="$HOME/.local/share/pr-rerun/app.sqlite"
unset PR_RERUN_CONFIG
```

The values above are examples. Use a separate database and unused local test port
for initial validation; keep the old server running until App access is verified.
No real rerun is required to enroll or preview a repository.

Users can provision their own identity with GitHub device login before enrollment.
Use `pr-rerun-admin users` to obtain their verified numeric IDs. Legacy operators
can also provision identities through an App-authorized repository. `issue` prints a
new service token once; deliver it privately. Reissuing rotates the user's global
service token, so reuse existing identities when adding repository grants.

```bash
pr-rerun-admin issue repo-admin --github-auth app --repo team/project
pr-rerun-admin issue contributor --github-auth app --repo team/project
pr-rerun-admin users
```

Copy `policy.example.json` to a private configuration directory. Replace its fake
IDs with the numeric IDs printed above, set actual workflow paths and review all
rules. Then enroll (the platform operator performs this locally):

```bash
pr-rerun-admin repo-add team/project --policy-file /path/to/policy.json
pr-rerun-server --github-auth app --port 8081
```

`repo-add` validates the current selected-repository App installation, required
permissions, repository identity, active workflows and enrolled service users.
It never reruns CI. A disabled policy can be used for initial enrollment.
Use `deploy/pr-rerun-app.service` as the systemd template and adapt its paths,
App ID, user and port. Apply the normal NetBird/systemd deployment instructions. No personal `gh` session is required by App mode.

## GitHub login

Set `PR_RERUN_APP_CLIENT_ID` on the server and enable Device Flow in App settings.
The first `pr-rerun login --netbird-server URL --repo owner/repo` displays a GitHub
URL and one-time code. Complete authorization in any browser. The server exchanges
its own device code and reads `/user`; clients cannot submit a claimed GitHub ID.
The CLI saves an automatically issued service session, never a GitHub token.
Each device has an independent session, valid for 90 days. Re-login after expiry.
Sessions survive server restart; pending device authorizations do not. GitHub user
and refresh tokens are discarded after identity verification. They are not used
for reruns, which continue to use App installation credentials. GitHub authorization
revocation does not automatically revoke this independent service session; an
operator can disable all sessions with `pr-rerun-admin revoke USER_ID`. Disabled
users cannot regain access by logging in. Reissuing a legacy token revokes existing
sessions. `pr-rerun logout` clears the local login only.

Legacy servers and administrator-issued tokens use `pr-rerun login --token-login`.
Existing saved logins keep working. GitHub login does not grant repository-policy
management: only the configured maintainers can update policy.

## Contributor and maintainer commands

Upgrade clients to 0.3.0 before using App mode. Existing login files still load;
legacy clients cannot select a repository on an App server.

```bash
pr-rerun login --netbird-server http://100.64.0.10:8081 --repo team/project
pr-rerun 123
pr-rerun --repo another/project 456
pr-rerun status rr_REQUEST_ID
```

Replace the example address. `--repo` overrides `PR_RERUN_REPOSITORY`, which overrides
the default saved at login. A saved default is used only for its saved server.
Each target requires its own repository grant; changing `--repo` is not authorization.
The existing exact confirmation includes the full repository and PR identity.

A repository maintainer can inspect and update rules:

```bash
pr-rerun policy --repo team/project
pr-rerun policy --repo team/project --history
pr-rerun policy --repo team/project --policy-file /path/to/update.json
```

The update file must contain exactly `expected_revision` and the complete `policy`:

```json
{
  "expected_revision": 1,
  "policy": {
    "enabled": true,
    "maintainers": [10001],
    "users": [10002],
    "allow_drafts": false,
    "base_branches": ["main"],
    "workflows": [{"path": ".github/workflows/ci.yml", "mode": "failed", "identity": "pull_request"}],
    "cooldown_seconds": 60,
    "max_requests_per_user_per_day": 20,
    "max_active_requests_per_pr": 1
  }
}
```

Use real enrolled IDs and the latest revision. A conflict requires fetching and
reviewing the latest policy again; do not blindly overwrite it. To roll back,
copy a previous snapshot from history into `policy` and use the **current** revision
as `expected_revision`. This creates a new audited revision. History returns the
latest 100 revisions; older snapshots remain in the operator's SQLite database.
A policy update takes effect on subsequent checks without restarting the server.
No rerun is triggered by policy inspection, update or rollback.

## Upgrade and validation

Back up the stopped service database before changing server modes. Schema additions
are additive; the legacy identity/request tables are retained. When reusing a legacy
database, provision each repository's explicit grants before cutover and discard
outstanding previews (revision zero cannot authorize a revision-one App policy).
Do not run two servers on one database. Keep the App deployment isolated until
registration, permission checks and a read-only preview have passed, then update
the systemd environment and restart during a quiet period.

Run `python -m pytest tests`, `ruff check src tests`, `ruff format --check src tests`,
`pyright`, and `python -m build`. App tests generate temporary RSA keys and mock
GitHub; they never use production credentials or trigger reruns. Real installation
validation requires an operator-created App and selected repository authorization.

Disabling a policy does not require GitHub to be available; re-enabling validates
App authorization and active workflows again.

For an existing shared client installed under `/opt/pr-rerun-client`, its machine
administrator can upgrade the package after building the new wheel (run from the
service source directory). Dependencies are unchanged from 0.2.3:

```bash
sudo /opt/pr-rerun-client/bin/python -I -m pip --isolated install --no-index --no-deps "$PWD/dist/pr_rerun_service-0.3.0-py3-none-any.whl"
sudo /opt/pr-rerun-client/bin/python -I -m pip --isolated check
/usr/local/bin/pr-rerun --help
```

Log in as the normal user, without sudo. Do not rerun the fresh-install script over
an existing shared installation; it deliberately refuses to overwrite it.

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
