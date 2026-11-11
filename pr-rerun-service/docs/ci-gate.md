# Comment approval for a native CI gate

[中文](ci-gate.zh-CN.md)

This optional listener extends the existing GitHub App. The App is the identity
and permission grant; `pr-ci-gate-server` is the code that receives its webhooks
and approves waiting environments. Installing the App alone does not install
this code or enable an environment rule.

The intended workflow is `lint + clang-tidy → full-gate → tests → ci/full`.
`full-gate` references a protected environment. GitHub waits before allocating
its runner. A new `/ci full` comment from any human user approves that gate;
downstream jobs continue in the same run with native PR checks. `ci/full` is the
final result, not the approval job. No replacement run, commit-status publisher,
or NPU polling job is involved.

## Configure the App and listener

1. Keep the existing rerun permissions. Add repository **Deployments: Read and
   write**, enable Webhook **Active**, and subscribe to **Issue comment** and
   **Deployment protection rule**. Gate tokens separately request Actions,
   Contents and Pull requests read, and Deployments write; rerun tokens keep
   their existing permissions.
2. Install the App using **Only select repositories** and select the test fork.
   Existing installations must accept the new permission request. Obtain the
   installation ID from the installation settings URL, or run
   `pr-rerun-admin installations` on the existing App server. Obtain the numeric
   repository ID with `gh api repos/OWNER/REPO --jq .id`.
3. Install this service version on the App server. Copy
   `deploy/ci-gate.example.json` into a private operator configuration directory
   and replace the example repository name, repository ID, installation ID,
   base branch, workflow path, environment and lint job **display names**.
   Enroll only the test fork initially. This policy is independent of rerun
   enrollment: permission to comment does not grant rerun access.
4. Generate a random webhook secret of at least 32 bytes. Save it outside the
   checkout, owned by the service user, mode `0600` or `0400`. Set the same value
   in the App's Webhook secret field. Reuse the existing App ID and private-key
   file through `PR_RERUN_APP_ID` and `PR_RERUN_APP_PRIVATE_KEY_FILE`.
5. Adapt `deploy/pr-ci-gate.service` and start the additional listener. It binds
   to `127.0.0.1:8082`. Keep the existing rerun API private. Configure an HTTPS
   reverse proxy to expose **only** `POST /github/ci-gate` from this listener,
   with a 1 MiB body limit and a short request-body timeout. Set that public
   HTTPS URL as the App's Webhook URL. Keep `/health` internal for monitoring.
   Do not route the NetBird rerun API or its administrative endpoints publicly.
6. In the fork, create a `ci-full` environment with no secrets. Under its custom
   deployment protection rules, enable this App and disable administrator
   bypass if bypass is not intended. Install the prototype workflow only after
   verifying the protection rule is enabled. An environment with no protection
   rules would run immediately.

The App owner changes App registration; the fork administrator accepts
permissions and configures the environment; the service operator deploys the
listener. These may be the same person. The existing CLI login and rerun API do
not need new public endpoints or changes to their confirmation flow.

## Fork-only acceptance test

Copy `deploy/ci-app-gate-prototype.yml` to
`.github/workflows/ci-app-gate-prototype.yml` on the fork's
`ci-app-gate-sandbox` branch. The supplied template is restricted to
`luohuan19/pypto`; change both policy and template when testing another fork.
It contains hosted-runner fixtures, not actual lint, compilation or NPU tests.

1. Open a PR targeting the sandbox branch. Verify lint and clang-tidy finish,
   `full-gate` waits for the App, and downstream test jobs have not started.
2. Comment exactly `/ci full` on its own. Verify the App's delivery succeeds,
   the gate releases, and all native checks finish under the original run ID.
3. Use `[fail-once]` in a test PR's title. Rerun failed jobs after its intentional
   system-test failure. Verify lint, clang-tidy and the successful gate retain
   their original execution times; only failed/dependent jobs run again.
4. Push a new head commit. Verify the previous comment does not release the new
   run; add a new comment and verify release. Also test a PR from another fork,
   where the Actions API can omit `pull_requests` links.
5. Restart the listener while a gate waits, then release it. Redeliver a webhook
   and verify there is no second CI run or duplicate approval operation.

Do not migrate production CI until this live acceptance sequence passes. Local
mocked API tests do not prove GitHub's environment configuration or live payload
behavior. The listener requires this exact workflow run-name contract:

```yaml
run-name: 'CI gate PR #${{ github.event.pull_request.number }} @${{ github.event.pull_request.head.sha }} base:${{ github.event.pull_request.base.sha }}'
```

Run names route requests; they are not trusted identity by themselves. The
listener verifies the live run, workflow, repository IDs, head repository,
branch, PR head/base, deployment commit parents and configured successful lint
jobs. Use workflow concurrency with `cancel-in-progress: true` for each PR.

## Delivery and retry behavior

Signed deliveries are normalized and committed to SQLite before HTTP 202;
GitHub calls do not delay the webhook response. A single worker processes the
durable queue. Comments arriving before the gate are found via the PR comments
API. Comments arriving afterwards wake pending gates; periodic reconciliation
also repairs missed comment notifications. Only new, unedited human comments
created strictly after the run/attempt start count. A new commit, changed base,
closed PR, changed installation or stale attempt cannot reuse the approval.
After a base update, create a fresh PR run before requesting approval.

Transient failures retry with bounded backoff. After 20 consecutive failures,
the delivery becomes `failed`; fix the underlying problem, then redeliver it
from the App's Recent deliveries page. Redelivery requeues a failed entry and
does not reapprove a completed one. The log records delivery IDs and successful
repository/run/attempt IDs, never credentials or raw payloads. Keep the database
on persistent storage and monitor `/health` and `state=failed` log entries.
Completed inbox rows are kept for 31 days; approval records for 36 days. The
queue admits at most 10,000 active deliveries and fails closed when full.

GitHub's approval API has no expected-head or expected-attempt parameter. The
listener rechecks immediately before approval, but cannot make that POST atomic
with a concurrent push or rerun. Workflow concurrency cancels obsolete runs;
their checks remain attached to their original version. An interrupted approval
can be retried against the same environment, never by starting another run.

This is a CI scheduling mechanism, not complete protection from malicious PR
workflows. A PR can modify its workflow or add jobs that omit the gate. Control
self-hosted runner access independently, keep secrets out of the gate
environment, and review workflow changes before production rollout.

References: [custom rules](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/create-custom-protection-rules),
[environment configuration](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/configure-custom-protection-rules),
[webhook validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).
