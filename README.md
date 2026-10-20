# pto-toolbox

Tools for Linux servers, CI runners and shared compute environments, including
self-contained shell scripts and an independently deployed PR rerun service.

English | [中文](README.zh-CN.md)

## Why this repository exists

These scripts used to live in `~/` or a `temp/` directory on whichever machine
they were written for. That had three problems:

- **They were lost.** A machine reinstall, or a pod release-and-re-request, took
  the script with it.
- **They could not be reused.** A tool written on one runner had to be
  rediscovered, re-derived, or copy-pasted to be used on the next one.
- **They were never reviewed.** No history, no diff, no record of which
  edge case a particular line was defending against.

The shell tools are **single files you can `scp` to a fresh machine and run**,
using a POSIX shell and utilities a server already has.

`pr-rerun-service/` is a Python package with its own runtime, tests, and deployment
instructions. Its server runs independently of PyPTO; contributors use a CLI
installed on their NetBird-connected machines. Deployment configuration and
credentials stay on each host. See also the
[organization tool spec](https://github.com/pypto-tools/.github).

## Tools

| Tool | What it does |
| ---- | ------------ |
| [`deploy-git-mirror`](deploy-git-mirror/) | Makes CI clones of a hot repository local. Keeps a bare mirror on the runner and redirects the repo's URLs to it via `url.insteadOf`, turning a network clone into a same-disk hardlink clone (~20 ms). A scheduled job keeps the mirror fresh. |
| [`netbird-preflight`](netbird-preflight/) | Checks whether a host can join the self-hosted NetBird network before you install anything. Read-only: verifies architecture, DNS, clock skew, CA trust, TCP/UDP reachability, WireGuard capability, and conflicts with existing VPNs. |
| [`pr-rerun-service`](pr-rerun-service/) | Lets enrolled contributors preview and confirm reruns of their own PRs through NetBird. Ordinary failures rerun failed jobs; the configured Codex Review workflow reruns all jobs. Includes persistent per-user login, a shared client installer, and systemd deployment. |

## Using a tool

Clone the repository, or pull down the one file you need:

```bash
# whole repository
git clone https://github.com/pypto-tools/pto-toolbox.git

# a single shell tool (uses your gh credentials)
gh api repos/pypto-tools/pto-toolbox/contents/netbird-preflight/netbird-preflight.sh \
  --jq .content | base64 -d > netbird-preflight.sh
chmod +x netbird-preflight.sh
```

Every tool answers `--help`. Read the tool's own README before running anything
that writes.

For the rerun service, start with its [installation and usage guide](pr-rerun-service/README.md).
Once a client is installed, each user logs in once with a service token issued
by the administrator. `100.64.0.10` is an example, not a deployed service address:

```bash
pr-rerun login --netbird-server http://100.64.0.10:8080
pr-rerun 123
```

## Conventions for new tools

One directory per tool. Shell scripts use this layout:

```text
<tool-name>/
├── <tool-name>.sh      # the tool, self-contained
├── README.md           # English — what it does, how to use it, full flow
└── README.zh-CN.md     # Chinese translation
```

A shell tool in this repository should:

- **Be a single file.** No sourcing sibling files, no vendored libraries.
- **Support `-h` / `--help`** and document every flag there.
- **Be idempotent.** Re-running converges to the same state instead of stacking
  up duplicate config entries, cron lines or background processes.
- **Offer `--dry-run`** if it writes anything outside its own working directory.
- **Verify what it claims.** If the tool reports that it installed something,
  it must read that something back — "the command did not error" is not proof.
- **Separate inspection from mutation.** Read-only actions (`--status`,
  `--verify`) must never change state.
- **Exit non-zero on failure**, and validate arguments before doing anything
  slow or networked.
- **Carry no secrets.** No tokens, keys, passwords, hostnames of private
  infrastructure beyond what the tool inherently targets.

Documentation is bilingual: `README.md` is English and authoritative;
`README.zh-CN.md` mirrors it. Code, comments and commit messages stay English;
user-facing script output may be Chinese where the audience is.

The Python service keeps its package metadata, source, tests, dependency locks,
and deployment templates inside `pr-rerun-service/`. Read its own guide for
runtime requirements and validation commands. Never commit databases, user
logins, tokens, local unit files, or generated deployment bundles.

## Tests

```bash
tests/smoke.sh
```

Syntax-checks every tool, asserts `--help` works, and asserts each tool rejects
invalid arguments and honours `--dry-run`. No network access, no side effects —
safe to run anywhere, including CI.

The Python service has a separate suite with simulated GitHub traffic:

```bash
cd pr-rerun-service
python -m pytest tests
ruff check src tests
ruff format --check src tests
pyright
```

See the service guide for creating its Python development environment first.

## AI assistant boundaries

For the shell tools, see [`skills/pto-toolbox/SKILL.md`](skills/pto-toolbox/SKILL.md) for which
operations are read-only, which change machine configuration, and which have
side effects that need explicit human confirmation.
For the service, see its [security boundaries](pr-rerun-service/README.md#security-and-publishing).
