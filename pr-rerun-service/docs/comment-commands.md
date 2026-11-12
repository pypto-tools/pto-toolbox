# PR comment commands

[中文](comment-commands.zh-CN.md)

Server 0.9.0 supports a new PR conversation comment containing exactly:

```text
/ci rerun
```

Surrounding whitespace is allowed. Edits, review comments, issue comments outside
PRs, bots and extra arguments do not trigger commands. Only the PR author may use
it, including when the commenter is a repository maintainer or service admin.
The comment is the user's confirmation for that PR. The service binds the current
head, checks it again before submission and applies the normal repository rules.

The author does not need a CLI login. A verified GitHub comment establishes their
immutable GitHub identity; disabled identities remain blocked. With
`access: authenticated_users`, any PR author can use the command. With
`access: explicit_users`, they must already be listed in `users` or `maintainers`.
Existing device sessions remain valid and are not replaced.

Ordinary CI reruns failed jobs, including eligible cancelled jobs. Trusted Codex
Review uses rerun all. PyPTO Full CI requires the existing `pypto_ci` policy. This
command does not create first runs, invoke `/ci full`, approve pending workflows,
rerun other people's PRs, or write bot replies. Check the PR's Actions page for the
new attempt; no attempt can mean no eligible failures or a rejected command.

## Deployment

1. Upgrade the API, admin launcher, statistics collector and error collector to
   0.9.0 before saving the new policy field. Back up the database and preserve
   existing logins, App credentials, rules and timers. Clients need no upgrade.
2. On the backend, as the service user, create a secret in a private directory:

   ```bash
   install -d -m 700 ~/.config/pr-rerun
   python3 - <<'PY'
   import os, secrets
   from pathlib import Path
   path = Path.home() / '.config/pr-rerun/webhook.secret'
   fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, 'w') as output:
       output.write(secrets.token_urlsafe(48) + '\n')
   print('Created:', path)
   PY
   ```

   Set `PR_RERUN_WEBHOOK_SECRET_FILE` to its absolute path in the API service's
   environment and restart the API. The secret must be an owner-only regular file
   (0400 or 0600), not a symlink. Store it separately from the App private key.
   Without this setting, `/github/webhook` returns 404.
3. Route HTTPS **only** for `POST /github/webhook` through the public server to
   the backend's NetBird address. Keep `/api/*`, `/healthz`, documentation and all
   other paths inaccessible publicly. Use the [Nginx example](../deploy/webhook-nginx.conf.example)
   behind a TLS ingress, replacing its backend address. Preserve the raw body and
   GitHub headers; disable proxy retries. Do not put the App key or webhook secret
   on the relay. Validate TLS certificates normally. A dedicated container on the
   ingress network needs no published host port. The example uses `/tmp` for
   runtime files and supports an unprivileged, read-only Nginx container with a
   writable `/tmp`; start Nginx with `-e /dev/stderr -g 'daemon off;'`.
4. In the GitHub App settings, add repository **Issues: Read-only** and subscribe
   to **Issue comment**. Installation owners must approve the permission update.
   Keep the existing Actions write and Pull requests read permissions. Enable
   Webhook, set `https://YOUR-HOOK-HOST/github/webhook`, and copy the secret from
   its private file directly into GitHub's Secret field. Do not paste it into
   chats, tickets, Git commits or logs. Bot replies are not implemented; Pull
   requests write is not required. Confirm GitHub's signed ping returns `pong`.
5. Use `pr-rerun policy --repo owner/repo` and the existing revision-checked
   policy update flow to set **`comment_reruns_enabled: true`**, preserving every
   other setting. Enable a test repository first. Installation alone does not
   enable commands or enroll a repository. Disable this field to stop new and
   queued commands for that repository.
6. The author of an eligible test PR posts a new `/ci rerun` comment. Verify the
   new Actions attempt and the operator audit below. Do not test against other
   people's PRs or synthesize real comment events.

See GitHub's [Issue comment permission requirements](https://docs.github.com/en/webhooks/webhook-events-and-payloads#issue_comment)
and [signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).

## Audit and delivery behavior

On the backend, using the same database as the API:

```bash
pr-rerun-admin comment-commands --repo owner/repo --limit 20
```

The result includes the PR, numeric actor, comment ID, request ID, state and a
sanitized explanation. It contains no credentials or comment text. `queued` is
durable receipt; `processing` is validation/submission. `accepted` means GitHub
accepted the request, not that CI passed. `ineligible` means no verified jobs need
a rerun. `rejected` means a check failed. `partial`, `unknown` and `interrupted`
require inspecting GitHub Actions before posting another command. Comment requests
use private internal sessions; their request IDs are audit references, not tickets
for another CLI session's `status` command.

The receiver verifies HMAC-SHA256 over the raw body, bounds bodies to 256 KiB and
persists a command before returning. It makes no GitHub calls in the delivery
path. A single worker processes commands in order, independently of the HTTP
response. Commands expire after ten minutes, including queue time. The original
comment, author, repository/installation and policy are checked again through the
GitHub API, including immediately before writes. A changed head cancels processing.
Existing workflow attribution, quotas and run-attempt claims remain in force.

Delivery IDs and repository/comment IDs prevent repeated events from rerunning
twice, including redeliveries with a different delivery ID. At most 1,000 pending
commands are accepted. After a restart, unstarted commands can resume within the
ten-minute window; processing commands become `interrupted` and are not replayed.
Failures with an uncertain GitHub POST outcome are never automatically retried.
If delivery failed, inspect the App's Recent deliveries and backend audit. GitHub
does not automatically redeliver failures; stale commands require a new comment.
See [GitHub delivery guidance](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks).

The inbox is stored in the existing rerun SQLite database. It retains small audit
rows, not payloads or job logs; back it up with that database. There is currently
no automatic inbox retention cleanup. Rotating the secret requires updating both
GitHub and the protected backend file, then restarting the API.
