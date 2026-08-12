# deploy-git-mirror

Turn a CI runner's repeated network clones of one repository into same-disk
hardlink clones (~20 ms), and keep the local copy fresh on a schedule.

English | [中文](README.zh-CN.md)

## What problem this solves

Some CI jobs clone a dependency repository on every run — a pinned toolchain, a
generated-header repo, an ISA definition. On a self-hosted runner behind a flaky
egress path this is a recurring failure:

```text
error: RPC failed; curl 56 GnuTLS recv error (-9): Error decoding the received TLS packet.
fetch-pack: unexpected disconnect while reading sideband packet
fatal: early EOF
```

Retrying does not help, and the repository is usually not even large — the
problem is the network path, not the payload.

## How it works

The tool puts a **bare mirror** of the repository on the runner and adds a
`url.<mirror>.insteadOf` rule for that repository's URLs. Git rewrites the
transport before connecting, so the unchanged `git clone` line inside CI
resolves to a local path:

```text
CI: git clone https://github.com/<org>/<repo>.git build/<repo>
      │  ~/.gitconfig: url.<mirror>.insteadOf = that URL
      ▼
    git clone /srv/mirrors/<repo>.git build/<repo>     # same disk → hardlinks, ~20 ms
    git checkout --detach <pin>                        # any commit: mirror is --mirror

/srv/mirrors/<repo>.git        ← bare mirror
   └── sync-<repo>.sh          ← scheduled: git remote update --prune
```

Because the clone target and the mirror are on one filesystem, git hardlinks the
pack files: the clone costs no extra disk, and no bytes cross the network.

CI configuration is untouched. Rolling back is one `git config --unset-all`.

## Prerequisites

- `git`, `flock` (optional but recommended), and a scheduler — see
  [Choosing a scheduler](#choosing-a-scheduler).
- Working authentication to the git host **for the account running the tool**,
  and the host key already in `known_hosts` (a scheduled job has no TTY and
  cannot answer the fingerprint prompt).
- A directory for the mirror **on the same filesystem** as the runner's work
  directory. A different filesystem still avoids the network but degrades
  hardlinks to full copies.

## Quick start

```bash
./deploy-git-mirror.sh \
  --repo <org>/<repo> \
  --mirror-root /srv/mirrors \
  --check-path /srv/runner \
  --interval 2

./deploy-git-mirror.sh --verify --repo <org>/<repo> --mirror-root /srv/mirrors
```

Re-running is safe at any time: it converges to the same state rather than
stacking duplicate config entries or cron lines.

## Options

| Option | Meaning |
| ------ | ------- |
| `--repo <org/name>` | Repository to mirror. Repeatable. Full URLs accepted. |
| `--mirror-root <dir>` | Where mirrors, sync scripts and logs live. Must share a filesystem with the CI clone target. |
| `--check-path <dir>` | Assert this path is on the same filesystem as `--mirror-root`. Repeatable, read-only. |
| `--interval <min>` | Sync interval in minutes. Default 2. Must be `<60` or a multiple of 60. |
| `--scheduler <mode>` | `auto` (default), `user-cron`, `system-cron`, `loop`. See below. |
| `--timeout <sec>` | Per-fetch timeout. Default 100. |
| `--protocol ssh\|https` | How the *mirror itself* fetches. Default `ssh`. |
| `--proxy <url>` | HTTPS proxy for the mirror's own fetches. `https` only. |
| `--credential-helper <cmd>` | Credential helper for private repos. `https` only; inherited from global config when omitted. |
| `--host <hostname>` | Git host. Default `github.com`; change for GitHub Enterprise. |
| `--status` | Print current deployment state and exit. Read-only. |
| `--verify` | Prove the redirect works: clone timing, hardlinks, mirror freshness. |
| `--uninstall` [`--purge`] | Remove the redirect and the scheduler. `--purge` also deletes the mirror. |
| `--no-cron` | Install everything except the scheduler. |
| `--dry-run` | Print the actions without performing them. |

## Full deployment flow on a fresh server

### 1 — Survey the machine

```bash
echo "HOME=$HOME"                       # often not /home/<user>
command -v git flock crontab
df -h | grep -vE 'tmpfs|devtmpfs'
find "$HOME" -maxdepth 5 -type d -name '_work' 2>/dev/null | head
```

Note which filesystem holds the runner's work directory.

### 2 — Establish git-host authentication

Three independent gates. All of them must pass before deploying.

**Host key** — without this, the scheduled sync fails with no obvious cause:

```bash
ssh-keyscan github.com >> ~/.ssh/known_hosts
ssh-keygen -lF github.com
```

`ssh-keyscan` is silent on failure and still exits 0, so always verify with
`ssh-keygen -lF`. Compare the printed ED25519 fingerprint against
[GitHub's published key fingerprints](https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
If port 22 is blocked, use GitHub's port-443 endpoint instead:

```bash
printf '\nHost github.com\n    Hostname ssh.github.com\n    Port 443\n    User git\n' >> ~/.ssh/config
chmod 600 ~/.ssh/config
ssh-keyscan -p 443 ssh.github.com >> ~/.ssh/known_hosts   # keyed by host *and port*
ssh-keygen -lF '[ssh.github.com]:443'
```

This is transparent to the tool — the mirror origin still names `github.com`.

**A key the host accepts:**

```bash
ls -l ~/.ssh/id_* || ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
timeout 20 ssh -T -o ConnectTimeout=10 git@github.com     # expect: Hi <account>!
```

Register the public key on a CI bot account (title it after the machine, so it
can be revoked individually), or as a read-only deploy key if you have
repository admin. Do not copy one private key between machines.

**Repository access** — authenticating is not authorization:

```bash
timeout 60 git ls-remote git@github.com:<org>/<repo>.git HEAD
```

`Repository not found` means the account lacks access to that repository.

> Put `timeout` on every networked command. A stalled connection to a git host
> is common; waiting on it is not diagnosis.

### 3 — Pick the mirror location

```bash
./deploy-git-mirror.sh --dry-run \
  --repo <org>/<repo> \
  --mirror-root <candidate> \
  --check-path <parent of _work>
```

Look for `同盘校验 OK` / same-filesystem confirmation. `--dry-run` writes
nothing, so try as many candidates as you like.

### 4 — Choose a scheduler

```bash
crontab -l 2>&1 | head -2
```

| Result | Use |
| ------ | --- |
| Lists entries, or `no crontab for user` | default (`auto` picks `user-cron`) |
| `You are not allowed to use this program` **and** you have sudo | `--scheduler system-cron` (run `sudo -v` first) |
| Denied and no sudo | `--scheduler loop` — survives logout, not reboot |

```bash
sudo -v      # system-cron only: without cached credentials the tool falls back to printing the command
./deploy-git-mirror.sh \
  --repo <org>/<repo> --mirror-root <dir> --check-path <dir> \
  --interval 2 --scheduler <mode>
```

The tool reads the scheduler back after writing it and says
`已装并验证` / installed-and-verified. Anything else means it is not scheduled.

### 5 — Verify

```bash
./deploy-git-mirror.sh --verify --repo <org>/<repo> --mirror-root <dir>
```

All four checks must pass:

1. the HTTPS URL resolves locally (sub-second),
2. the SSH URL resolves locally (sub-second),
3. pack files are hardlinked (`nlink > 1`),
4. the mirror's HEAD equals the remote's — this is what catches a mirror
   silently fetching *itself*.

Then wait one interval and confirm the scheduler is firing:

```bash
./deploy-git-mirror.sh --status --mirror-root <dir>
```

## Reading the sync log

`<mirror-root>/sync-<repo>.log`

- **The sync script prints nothing to the terminal by design** — all output goes
  to the log. Silence is not a hang. For live progress, run the fetch in the
  foreground:
  `GIT_CONFIG_GLOBAL=/dev/null timeout 60 git -C <mirror> remote update --prune`
- **`FAIL rc=124`** — the fetch stalled and `timeout` killed it. Expected on a
  flaky path; the next tick recovers.
- **A missing tick** — `flock` skipped a run that overlapped a previous one.
  Also expected.

An occasional `FAIL` does not affect CI: clones read the local mirror
regardless. The only consequence is that a very recently pushed commit may not
be there yet. Investigate only if failures become a large fraction of runs; then
switch to the port-443 SSH endpoint, or to `--protocol https --proxy`.

## Troubleshooting

| Symptom | Cause |
| ------- | ----- |
| `Permission denied (publickey)` | Key not registered on the git host account. |
| `Repository not found` | Authenticated, but that account cannot read the repository. |
| Scheduler installed but never fires | User denied by `cron.allow`/`cron.deny`; use `--scheduler system-cron`. |
| `--status` shows no scheduler | Nothing is refreshing the mirror. CI still succeeds, so this is easy to miss — treat it as a real failure. |
| `--verify` reports seconds, not milliseconds | The redirect is not in effect; check both URL forms are installed. |
| No hardlinks | `--mirror-root` and the clone target are on different filesystems. |
| Mirror never advances | The mirror is fetching itself — its `origin` is matched by the installed `insteadOf` rules. |

## Rollback

```bash
./deploy-git-mirror.sh --uninstall --repo <org>/<repo> --mirror-root <dir>   # add --purge to delete the mirror
```

Removes the redirect and all three scheduler backends. CI clones immediately go
back over the network.

## Design notes

Three details that are not obvious and that the tool handles for you:

**Two URL forms are redirected.** A repository is cloned as
`https://host/org/repo.git` by some tools and `git@host:org/repo.git` by others,
and a global `url."git@host:".insteadOf = https://host/` rule may already
rewrite one into the other. Both exact forms get a rule; git picks the longest
matching prefix, so these win over any broader existing rule. Prefix-only forms
(without `.git`) are deliberately *not* installed — `…/pypto` would also match
`…/pypto-lib.git`.

**The mirror must not fetch itself.** The mirror's own `origin` uses a third URL
spelling (`ssh://git@host/org/repo.git`) that none of the installed rules match,
and the sync runs with `GIT_CONFIG_GLOBAL=/dev/null`. Without both, the mirror
resolves its own origin to its own path, fetches nothing, and reports success
forever. `--verify` checks for this explicitly.

**Masking the global config also masks credentials.** `GIT_CONFIG_GLOBAL=/dev/null`
hides `credential.helper` along with `insteadOf`, which breaks HTTPS fetches of
private repositories. In `https` mode the tool copies the credential helper into
the mirror's *local* config, which the variable does not affect.
