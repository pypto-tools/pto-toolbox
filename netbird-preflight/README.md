# netbird-preflight

Check whether a host can join the self-hosted NetBird network — before
installing anything.

English | [中文](README.zh-CN.md)

## What it does

NetBird installation failures are rarely about NetBird. They are a blocked
outbound port, a clock that drifted past certificate validity, a missing CA
bundle, a container without `/dev/net/tun`, or another VPN already holding the
`100.64.0.0/10` range. Each of those surfaces as an unhelpful error *after* you
have installed a daemon and rewritten `/etc/resolv.conf`.

This script checks all of them first. It is **entirely read-only** — it opens
sockets and reads system state, and changes nothing.

## What it checks

**1. System environment** — CPU architecture against the architectures with
official packages; distribution; kernel version (≥5.6 has in-tree WireGuard);
package manager; install privilege (root / passwordless sudo / sudo with
password / neither); whether the init system is systemd; presence of `curl`,
`ip`, `python3`.

**2. DNS, clock and trust chain** — resolution of the management host, falling
back to public resolvers so a broken local resolver is distinguishable from an
unreachable server; whether `/etc/resolv.conf` has already been taken over by
NetBird and whether that resolver still answers; the system CA bundle; and clock
skew measured against the server's own `Date` header, which both TLS and login
tokens depend on.

**3. Reachability of the NetBird server** — TCP to the management port; the
HTTPS console; TLS certificate validation (a failure here usually means an
intercepting proxy); ALPN `h2`, which gRPC requires; the management API; the
relay endpoint; and STUN over UDP 3478, which P2P hole punching needs.

**4. WireGuard data plane** — kernel module loaded, loadable, or built in;
`/dev/net/tun`; the `NET_ADMIN` capability; whether the WireGuard UDP port is
already taken.

**5. Conflicts with the existing environment** — an existing NetBird install,
a leftover `wt0` interface, other VPN daemons running, and whether the NetBird
CGNAT range is already routed by something else.

## Usage

```bash
./netbird-preflight.sh                      # check the default server
./netbird-preflight.sh -s host:port         # override the management endpoint
./netbird-preflight.sh --stun-port 3478     # override the STUN port
./netbird-preflight.sh --wg-port 51820      # override the WireGuard port
./netbird-preflight.sh -h                   # help
```

| Option | Meaning |
| ------ | ------- |
| `-s`, `--server <host[:port]>` | Management endpoint. Defaults to the built-in server on port 443. |
| `--stun-port <port>` | UDP port probed for STUN. Default 3478. |
| `--wg-port <port>` | Local UDP port checked for availability. Default 51820. |
| `-h`, `--help` | Usage. |

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0` | Ready to install. Warnings may still be present and are listed. |
| `1` | At least one blocking failure. The blockers are repeated at the end. |
| `2` | Bad command line. |

Suitable for gating an automated rollout: run it across a fleet and install only
where it exits 0.

## Reading the output

| Mark | Meaning |
| ---- | ------- |
| `✔` | Passed. |
| `!` | Warning — installation can proceed, but note the consequence (for example: UDP blocked means traffic falls back to the relay, which works but is slower). |
| `✘` | Blocking — installation will not work until it is fixed. |
| `-` | Skipped because a prerequisite check already failed. |

Checks that all depend on one connection are reported once and then skipped, so
a single blocked port produces one red line rather than six.

## Common failures

| Finding | Fix |
| ------- | --- |
| TCP to the management port unreachable | Outbound firewall. NetBird cannot work at all until this is open. |
| TLS verification fails | System clock, missing CA bundle, or a TLS-intercepting proxy. |
| ALPN negotiated HTTP/1.1 instead of h2 | Almost always an intercepting proxy; gRPC to the management server will fail. |
| STUN gets no reply | Outbound UDP blocked. Not fatal — peers fall back to the relay over the management port, at a latency cost. |
| Clock skew over 300s | Enable NTP/chronyd. TLS and login tokens will otherwise be rejected. |
| `/dev/net/tun` missing | In a container, add `--device /dev/net/tun`; on a host, `modprobe tun`. |
| `NET_ADMIN` missing | In a container, add `--cap-add NET_ADMIN`. |
| CGNAT range already routed | Another overlay network is using `100.64.0.0/10`; addresses would collide. |
| NetBird's own DNS resolver not answering | NetBird rewrites `/etc/resolv.conf`; if its resolver is down, **every** user's DNS on that host breaks. Recover with `netbird up --disable-dns` or restore `/etc/resolv.conf.original.netbird`. |

## After it passes

The script prints the install commands for the endpoint you checked:

```bash
curl -fsSL https://pkgs.netbird.io/install.sh | sh
netbird up --management-url <BASE> --setup-key <SETUP_KEY>
```

Create the setup key in the console's Setup Keys page. After installing, confirm
with `netbird status -d` that Management and Signal both report `Connected`.
