# CI statistics (0.5.0)

English | [中文](ci-statistics.zh-CN.md)

CI statistics reuse the existing App installation, service login and repository
aliases. They do not trigger reruns or download logs. The API serves a separate
SQLite cache after checking current user policy and live App authorization.
The collector requests only repository-scoped Actions/Metadata read credentials,
with a separate token cache from rerun credentials. No new App permission is needed.

## Enable on the service host

Upgrade the server and client to 0.5.0. Existing sessions remain valid. A repository
maintainer or service administrator retrieves its full policy, then submits a
normal revision-checked policy update adding:

```json
{
  "analytics_enabled": true,
  "analytics_access": "maintainers"
}
```

These are fields to add to the complete `policy`, not a standalone update payload.
Preserve its other fields and include `expected_revision`; see the
[policy update instructions](multi-repository.md). Existing repositories default
to statistics disabled. `maintainers` allows repository and service administrators.
`repository_users` additionally allows users authorized by that repository's
rerun access policy. It does not grant repository access to outsiders. Disabling
the repository or analytics blocks both collection and API access. Granting rerun
access alone does not expose repository-wide CI metadata by default.

The normal service database remains the source for policy and identities. Statistics
use `<PR_RERUN_DATABASE>.stats.sqlite`, or `PR_RERUN_STATS_DATABASE` when explicitly
configured. Configure the same path for the API and collector; it must differ from
the service database. Protect both databases and their parent directories. The
collector reads enrollment without initializing or migrating the service database.

With the service's App/database environment loaded, as the service OS user:

```bash
pr-rerun-admin ci-sync --repo owner/repository --days 7
pr-rerun-admin ci-sync --repo owner/repository --days 30 --request-budget 1500 --json
```

The collector requires a full repository name, not a client alias. Exit status is
0 for complete collection, 1 for incomplete collection with saved progress, and
2 for setup/authorization errors. A lock allows one collector per statistics
file. Completed unchanged runs reuse cached jobs; changed/active runs are refreshed.
Each pass re-enumerates the selected creation window to discover reruns of old runs.
Removed runs are reconciled only after a complete listing. Repeated runs are
idempotent; partial progress resumes on the next pass. Rows older than 31 days are
pruned for the repository after a fully enumerated pass.

The default budget is 1,500 Actions read requests per invocation (maximum 5,000).
App authentication requests also consume quota and are not included in that counter.
The collector stops when GitHub reports fewer than 100 remaining requests, or a
401/403/429. It does not sleep/retry indefinitely; the next timer invocation retries.
Statistics and reruns still share installation rate limits. Adjust the budget and
timer interval for the installation's other API users.

## Periodic collection

Adapt `deploy/pr-rerun-stats.service` and `.timer`: service account, environment
file, Python path and full repository name. The environment file contains the
App ID, key *path*, and database paths; the private key stays in its protected file.
Use the server Python environment, not the standalone client. Then install the
reviewed files under `/etc/systemd/system/` and run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pr-rerun-stats.timer
systemctl list-timers pr-rerun-stats.timer
```

It runs after boot and ten minutes after the preceding collection ends. For another
repository use a separate pair of unit names and matching `Unit=`; stagger schedules
when sharing one statistics database. A conflicting collector exits rather than
running concurrently. Stop collection with `sudo systemctl disable --now
pr-rerun-stats.timer`; stop an already running invocation separately if needed.
No webhook or public listener is required.

## Query

```bash
pr-rerun stats --repo pypto --days 7
pr-rerun stats --repo pypto --days 30 --sort failure-rate
pr-rerun stats --repo pypto --event pull_request --limit 50
pr-rerun stats --repo pypto --event push --branch main
pr-rerun stats --repo pypto --workflow .github/workflows/ci.yml --job 'compile (a5, cpu)'
pr-rerun stats --repo pypto --json
```

A saved default repository also works. Filters use exact values. `--branch` is the
run's **head branch**, not a PR's target branch. `pull_request_target` is a separate
event, including Codex workflows that use it. Workflow filters use the full file
path. Matrix job names remain separate; arbitrary name stripping/grouping is not
implemented. Queries return 20 groups by default, at most 200, with `total_groups`
to make truncation explicit. JSON output contains no alias messages on stdout.

The API is `GET /api/repositories/{owner}/{repo}/stats`, with the same query
parameters. It requires a normal service session. Queries do not launch collection.
A missing cache returns 503; disabled/unauthorized access returns 403. Old servers
need an upgrade. No new client login is required unless the session expired/revoked.

## Interpretation and limits

- The window covers **workflow runs created** in the last N days, ending at the
  collection start time in UTC. Later attempts of those runs belong to that cohort;
  this is not a count of all job executions that occurred during those days.
- Rankings use first-attempt jobs only. `samples = success + failure + timed_out`;
  `failure_rate = (failure + timed_out) / samples`. No samples produces JSON `null`.
  Cancelled, skipped, neutral, pending and other results are retained separately.
  Failure-count sorting includes timeouts; the displayed counts remain separate.
- Group identity includes workflow ID, path and full job name. Duplicate names in
  one attempt are counted as executions but excluded from recovery matching;
  `ambiguous_recovery_runs` reports this ambiguity.
- `rerun_executions` counts distinct later-attempt jobs, excluding carried-forward
  job IDs. `retried_failures` counts initially failed/timed-out jobs with a matched
  later attempt; `recovered` counts those with any subsequent success. Recovery
  does not prove flakiness or environment failure.
- Failed step counts come from first attempts, including continue-on-error steps
  inside successful jobs. Duration P50/P95 use completed first-attempt jobs and
  include failed/cancelled jobs with both timestamps; they exclude queue time.
- `complete` means the requested window was collected without known omissions.
  It is false for API failures, pagination changes, request limits or a requested
  window longer than the collected window. Reports preserve known partial data.
  `stale` means the collection start is more than 30 minutes old. The CLI exits 1
  for incomplete/stale data, 0 for complete/fresh data, and 2 for errors.
- GitHub does not provide a transactional snapshot. A rerun after collection may
  appear only on the next pass. Deleted/expired historical data cannot be rebuilt
  from this API. The first version retains metadata, not full logs or root-cause
  classifications. It does not auto-rerun jobs.

References: [runs and the search cap](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository),
[job attempts](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt),
[rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

## Client display (0.5.3)

Text output uses bundled Rich tables with borders and right-aligned numbers.
Workflow path prefixes are abbreviated and long job names wrap to the terminal width without dropping
matrix parameters. Narrow terminals use a separate metric/value table per job.
Failed steps appear in a separate table with Workflow / Job, Failed step and Count
columns. Steps are grouped by job with separators; each job shows its top three
steps. Narrow terminals show a separate step/count table per job. Names containing
brackets are shown literally. Redirected output has
no automatic colors; `NO_COLOR=1` also disables colors in the terminal. Rich and its
dependencies are included in the standalone client. The Rate column uses red for
the highest nonzero rate among displayed groups (including ties), yellow for other
nonzero rates, green for zero, and dim `-` for no terminal samples. All-zero results
are entirely green; the maximum is based on the displayed selection, not hidden
groups. JSON output is unchanged. A missing completed sync is described as
partial collection rather than `Last sync: None`. Update only the client; server
0.5.0 remains compatible, with no restart or new login required.
