# CI error excerpts

Server and client 0.6.0 add read-only log collection, deterministic error summaries,
context, similar-summary counts, and source links. No reruns are triggered.
[中文](ci-errors.zh-CN.md)

## Enable per repository

Keep `analytics_enabled: true` and explicitly set these fields in the normal,
revision-checked repository policy update:

```json
{
  "error_logs_enabled": true,
  "error_logs_access": "maintainers"
}
```

These are fields to merge into the full existing policy, not a standalone policy.
Only service administrators and configured repository maintainers can read excerpts
by default. Aggregate `analytics_access: repository_users` does not grant excerpt
access. Setting `error_logs_access: repository_users` deliberately shares excerpts
with users admitted by that repository's `access` policy. Disabled identities,
disabled policies and revoked App installations cannot read cached excerpts.
A private repository's metadata or logs should not be exposed through an unrestricted
`authenticated_users` policy without an explicit decision by its maintainer.

Existing App Actions-read permissions suffice. No new external account or AI service
is used. Error categories are heuristic labels, not proven root causes.

## Collection

Run normal `ci-sync` first. Then, with the same App settings and statistics database:

```bash
pr-rerun-admin error-sync --repo owner/repository --days 30 \
  --request-budget 100 --max-logs 100 --json
```

Use [the independent service](../deploy/pr-rerun-errors.service) and
[timer](../deploy/pr-rerun-errors.timer), adapting user, paths, environment file and
repository. They wait ten minutes after each collection, with up to thirty seconds
of jitter. They run separately from statistics and the HTTP service.

Newer completed failures and timeouts are processed first. A successful job with a
failed step is included too. Step logs are used when stored step numbers are
available; older cached records use job logs. Each repository installation, run,
attempt, job and step has a stable ID. All attempts are shown; rerun success does
not erase an earlier error. This differs from first-attempt-only failure rates.

Progress survives restarts and is stored in `ci_errors` and `ci_error_sync` in the
existing independent `.stats.sqlite` database. Reads do not create or update tables.
Only redacted snippets and metadata are stored; raw logs and signed download URLs
are not persisted. Records are pruned after 31 days by subsequent collection.
Run creation time defines the 1–30 day query window, using the statistics snapshot
cutoff. For a 30-day error report, collect 30 days of metadata too.

Limits: 100 log API requests/logs per invocation by default, configurable up to 500;
4 MiB per log; at most 64 MiB per invocation; thirty seconds per streaming download
plus network timeout. Compressed payloads and further redirects are rejected.
Only GitHub-selected HTTPS Actions/Azure log storage destinations are accepted;
the API bearer token is never forwarded to storage. API calls share the App
installation's quota. Both statistics and log collection pause below 1,000
remaining requests. Log collection also observes rate-reset/Retry-After headers;
this is a reserve, not a guarantee that concurrent reruns cannot be rate-limited.

Statuses: `pending`, `retry`, `ready`, `unavailable`, `failed`. Temporary errors
retry after ten minutes, up to three attempts; exhausted records require operator
investigation. Missing/deleted/expired logs are `unavailable`. Oversized logs are
marked `truncated`; errors beyond the downloaded prefix may be absent. Metadata
coverage, excerpt coverage and freshness are reported separately. A failed log
download never invalidates the aggregate statistics cache.

Redaction covers common token formats, authorization headers, secret assignments,
private-key blocks, and URL paths/queries. It is best-effort: arbitrary secrets,
encoded values and project-specific sensitive text may remain. Treat even redacted
excerpts as restricted log data. Follow the GitHub source link for the full original
log under GitHub's own permissions.

## Query

```bash
pr-rerun errors --repo pypto --days 30
pr-rerun errors --repo pypto --days 30 --group
pr-rerun errors --repo pypto --days 30 --job 'clang-tidy' --category compile
pr-rerun errors --repo pypto --days 30 --limit 20 --offset 20
pr-rerun error err_0123456789abcdef0123456789abcdef --repo pypto
pr-rerun errors --repo pypto --days 30 --json
```

`--group` counts matching normalized summaries across attempts, not unique root
causes or affected PRs. List filters include exact workflow path, job, category,
event and branch. Categories: `network`, `resource`, `timeout`, `test`, `compile`,
`exception`, `unclassified`. List responses omit context; detail responses include
bounded excerpts with line numbers. IDs and bracket-containing log text are literal.
Exit 1 means partial/stale coverage (or an unready detail), 2 means a request error.

API: `GET /api/repositories/{owner}/{repo}/errors` and
`GET /api/repositories/{owner}/{repo}/errors/{error_id}`. Requests only read cached
excerpts and validate current access; they never download logs on demand.

## Upgrade and rollback

Upgrade the server before enabling the new policy fields or scheduling error-sync.
Update clients with `sudo pr-rerun-update --version 0.6.0`. There is no login reset.
Back up the service and statistics databases before upgrading. Older servers use
strict policy parsing: do not downgrade with the new policy fields still stored.
A rollback must stop the new collectors and use a compatible policy migration or
restore the pre-upgrade databases; restoration loses changes made since the backup.
