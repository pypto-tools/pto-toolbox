---
name: pto-toolbox
description: Operate the single-file shell tools in pypto-tools/pto-toolbox — deploy-git-mirror (local git mirror + url.insteadOf on CI runners) and netbird-preflight (read-only NetBird join readiness check). Use when asked to deploy, inspect, verify, or roll back either tool on a server, or when adding a new tool to this repository.
---

# pto-toolbox

Self-contained shell tools. Each lives in its own directory with a bilingual
README. Read that README before running anything that writes.

## Operation classes

Classify every command before running it. When a request is ambiguous, assume
the more restrictive class and ask.

### Read-only — safe to run unprompted

These inspect and report. They never change machine state.

- `netbird-preflight.sh` in full, with any flags. It opens sockets and reads
  system state only.
- `deploy-git-mirror.sh --status` — prints redirect rules, scheduler, mirror
  freshness.
- `deploy-git-mirror.sh --dry-run …` — prints the actions it *would* take.
- `deploy-git-mirror.sh --help` / `--version`.
- Reading `<mirror-root>/sync-<repo>.log`.

`deploy-git-mirror.sh --verify` is nearly read-only: it creates and deletes a
temporary clone under `--mirror-root`. Safe on a machine already running the
tool; do not use it to probe an unrelated directory.

### Changes machine configuration — confirm first

`deploy-git-mirror.sh` install writes outside its own directory:

- **Global git config.** Adds `url.<mirror>.insteadOf` entries to
  `~/.gitconfig`. This affects **every** git operation by that user on that
  machine, including running CI jobs.
- **Scheduling.** Writes the user crontab, or `/etc/cron.d/git-mirror-<name>`
  (root), or starts a detached background loop.
- **`sudo`.** `--scheduler system-cron` uses cached sudo credentials to write
  under `/etc/`. If sudo would prompt, the tool prints the command instead of
  running it — surface that command to the human rather than trying to supply
  a password.

Before installing, confirm the target `--mirror-root` shares a filesystem with
the runner's work directory (`--check-path`), and confirm the intended
`--scheduler`.

### Side effects — never do implicitly

- **Network clone.** First install fetches a full mirror. On a metered or
  fragile link this is not free.
- **`--uninstall --purge`** deletes the mirror directory.
- **Editing `~/.ssh/config` or `~/.ssh/known_hosts`.** Sometimes required (port
  22 blocked, host key absent), but it changes how *all* SSH on that host
  behaves. Propose the exact lines; let the human apply them.
- **Registering an SSH key on a git host.** Always a human action. Print the
  public key and the steps; never attempt to upload a key.

## Credentials

- Never print, log, copy, or commit a private key, token, password, or the
  contents of `~/.ssh/id_*` (the `.pub` file is fine).
- Never copy a private key between machines. Generate a new key per machine so
  it can be revoked individually.
- Never write credentials into this repository, into a tool's flags in
  documentation, or into commit messages. `--credential-helper` takes a helper
  *command*, never a token.
- If a task appears to require a secret, stop and ask.

## Diagnosing "it deployed but nothing happens"

The characteristic failure of `deploy-git-mirror` is silent: CI clones keep
succeeding in ~20 ms while the mirror never updates. Check in this order:

1. `--status` — is a scheduler listed at all? "no scheduler" is a real failure,
   not a cosmetic one.
2. `sync-<repo>.log` — `FAIL rc=124` is a stalled fetch killed by `timeout`
   (expected occasionally); a missing tick is `flock` skipping an overlapping
   run (expected); no lines at all means the scheduler never fired.
3. `--verify` — its fourth check compares the mirror's HEAD against the remote,
   which is what catches a mirror fetching itself.

Silence from the sync script is by design: all its output goes to the log.
Do not interpret "no terminal output" as a hang.

## Adding a tool

One directory per tool: `<tool-name>/<tool-name>.sh`, `README.md` (English,
authoritative), `README.zh-CN.md`. The tool must be a single file, support
`--help`, be idempotent, offer `--dry-run` if it writes outside its working
directory, read back what it claims to have installed, and exit non-zero on
failure. Add it to both root READMEs and make sure `tests/smoke.sh` covers it.

Code, comments and commit messages are English. Commit messages carry no AI
co-author or generated-by trailers.
