"""Interactive client. There is deliberately no --yes or client-selected run/mode."""

import argparse
import getpass
import ipaddress
import json
import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from pr_rerun import aliases
from pr_rerun.client_config import (
    TOKEN_PATTERN,
    SavedLogin,
    clear_login,
    config_path,
    load_login,
    save_login,
)

# Generated request IDs are UUID4s; this all-zero ID can never identify a rerun.
# The existing status route authenticates before returning this exact 404, so
# login works against deployed 0.1 servers without GitHub calls or mutations.
LOGIN_PROBE = "/api/reruns/rr_" + "0" * 32
AUTH_ERROR = "Service login is expired or revoked. Run 'pr-rerun login' again."
PROGRESS_INTERVAL = 0.1


@contextmanager
def progress(message: str):
    """Animate one terminal line; redirected output gets no animation or polling."""
    started = time.monotonic()
    stopped = threading.Event()
    output = sys.stderr
    animated = output.isatty() and os.environ.get("TERM") != "dumb"
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    try:
        frames.encode(output.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        frames = "|/-\\"

    def render(index: int):
        width = max(1, shutil.get_terminal_size().columns - 1)
        line = f"{frames[index % len(frames)]} {message} ({time.monotonic() - started:.0f}s)"
        print("\r\033[2K" + line[:width], end="", file=output, flush=True)

    if animated:
        render(0)
    else:
        print(f"{message}...", file=output, flush=True)

    def report():
        index = 1
        while not stopped.wait(PROGRESS_INTERVAL):
            render(index)
            index += 1

    reporter = threading.Thread(target=report, name="pr-rerun-progress", daemon=True)
    if animated:
        reporter.start()
    completed = False
    try:
        yield
        completed = True
    finally:
        stopped.set()
        if animated:
            reporter.join()
            print("\r\033[2K", end="", file=output, flush=True)
        else:
            outcome = "Response received" if completed else "Waiting stopped"
            print(f"{outcome} after {time.monotonic() - started:.1f}s.", file=output, flush=True)


def safe_text(value: object) -> str:
    """Escape terminal controls in untrusted PR titles and workflow/job names."""
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in str(value))


def server_origin(value: str, *, netbird: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Server must be an origin URL without credentials, a path, or a query")
    # Force validation here, before requesting a token or making a connection.
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Server port must be between 1 and 65535")
    loopback = parsed.hostname == "localhost"
    address = None
    try:
        address = ipaddress.ip_address(parsed.hostname)
        loopback = loopback or address.is_loopback
    except ValueError:
        pass
    if netbird:
        # This explicit option asserts that the operator has verified the VPN route.
        # A private IP alone does not establish that traffic traverses NetBird.
        networks = ("100.64.0.0/10", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        if (
            address is None
            or "%" in parsed.hostname
            or not any(address in ipaddress.ip_network(network) for network in networks)
        ):
            raise ValueError(
                "NetBird server must use a literal VPN IP, not a hostname or public IP"
            )
    if parsed.scheme != "https" and not (parsed.scheme == "http" and (loopback or netbird)):
        raise ValueError(
            "Remote servers require HTTPS; for a verified NetBird route use --netbird-server"
        )
    return value.rstrip("/")


def configured_endpoint(
    server: str | None, netbird_server: str | None, saved: SavedLogin | None = None
) -> tuple[str, bool]:
    """Resolve command-line > environment > saved endpoint > loopback."""
    if server is None and netbird_server is None:
        server = os.environ.get("PR_RERUN_URL") or None
        netbird_server = os.environ.get("PR_RERUN_NETBIRD_URL") or None
    if server is not None and netbird_server is not None:
        raise ValueError("Set only one of PR_RERUN_URL and PR_RERUN_NETBIRD_URL")
    if netbird_server is not None:
        return server_origin(netbird_server, netbird=True), True
    if server is not None:
        return server_origin(server), False
    if saved is not None:
        return server_origin(saved.server, netbird=saved.netbird), saved.netbird
    return "http://127.0.0.1:8080", False


def configured_server(server: str | None, netbird_server: str | None) -> str:
    return configured_endpoint(server, netbird_server)[0]


def selected_token(server: str, saved: SavedLogin | None, *, login: bool) -> str:
    token = os.environ.get("PR_RERUN_ACCESS_TOKEN")
    if not token and not login and saved is not None:
        saved_server = server_origin(saved.server, netbird=saved.netbird)
        if httpx.URL(server) == httpx.URL(saved_server):
            token = saved.access_token
    if not token:
        token = getpass.getpass("Service access token: ")
    if not re.fullmatch(TOKEN_PATTERN, token):
        raise ValueError("Invalid service access token format")
    return token


def verify_login(client: httpx.Client) -> None:
    response = client.get(LOGIN_PROBE)
    if response.is_redirect:
        raise ValueError("Server redirects are not accepted")
    if response.status_code == 401:
        raise ValueError(AUTH_ERROR)
    try:
        if response.status_code == 404 and response.json() == {"error": "Request not found."}:
            return
    except ValueError:
        pass
    raise ValueError(
        f"Could not verify login with this service (HTTP {response.status_code}). "
        "Check the server address; no login was saved."
    )


def request(client: httpx.Client, method: str, path: str, body: dict | None = None) -> dict:
    response = client.request(method, path, json=body)
    if response.is_redirect:
        raise ValueError("Server redirects are not accepted")
    if response.status_code == 401:
        raise ValueError(AUTH_ERROR)
    if response.status_code == 404 and path == "/api/repositories":
        raise ValueError("Repository listing requires an App-mode server version 0.4.1 or later")
    if (
        response.status_code == 404
        and path.startswith("/api/repository-aliases")
        and (path == "/api/repository-aliases" or response.json().get("detail") == "Not Found")
    ):
        raise ValueError("Official aliases require an App-mode server version 0.3.2 or later")
    if (
        response.status_code == 404
        and "/stats?" in path
        and response.json().get("detail") == "Not Found"
    ):
        raise ValueError("CI statistics require an App-mode server version 0.5.0 or later")
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("Invalid service response")
    if response.is_error:
        raise ValueError(str(value.get("error", f"HTTP {response.status_code}")))
    return value


def resolve_selection(client: httpx.Client, repository: str | None) -> str | None:
    if repository is None:
        return None
    expanded = aliases.resolve_repository(
        repository,
        lambda alias: request(client, "GET", f"/api/repository-aliases/{alias}")["repository"],
    )
    if expanded != repository:
        print(f"Official repository alias: {safe_text(repository)} -> {safe_text(expanded)}")
    return expanded


def list_repositories(args) -> None:
    if args.repo is not None or args.request_id is not None:
        raise ValueError("Use pr-rerun repos [--json] without a PR or repository selector")
    saved = load_login()
    server, _ = configured_endpoint(args.server, args.netbird_server, saved)
    if not has_credentials(server, saved):
        raise ValueError("Run pr-rerun login before listing repositories")
    with httpx.Client(
        base_url=server,
        headers={"Authorization": "Bearer " + selected_token(server, saved, login=False)},
        timeout=30,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        value = request(client, "GET", "/api/repositories")
    rows = value.get("repositories")
    if value.get("scope") not in {"all", "visible"} or not isinstance(rows, list):
        raise ValueError("Invalid repository listing response")
    if args.json:
        print(json.dumps(value, indent=2))
        return
    print(
        "Service repositories: " + ("all" if value["scope"] == "all" else "visible to your account")
    )
    print("GitHub App authorization is not checked by this listing.")
    if not rows:
        print("No repositories available for your account.")
        return
    print("REPOSITORY  STATUS  ALIASES  ROLE  REVISION")
    for row in rows:
        try:
            if not isinstance(row["aliases"], list) or type(row["enabled"]) is not bool:
                raise ValueError
            values = [
                row["repository"],
                "enabled" if row["enabled"] else "disabled",
                ",".join(row["aliases"]) or "-",
                row["role"],
                row["revision"],
            ]
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid repository listing response") from None
        print("  ".join(safe_text(value) for value in values))


def official_alias_command(args) -> int:
    if args.command != "list" and not re.fullmatch(aliases.ALIAS, args.name):
        raise ValueError("Invalid alias name")
    repository = args.repository if args.command == "set" else getattr(args, "repo", None)
    if args.command != "list" and (
        not repository or not re.fullmatch(aliases.REPOSITORY, repository)
    ):
        raise ValueError(
            "Official alias changes require a full repository name; removal uses --repo"
        )
    saved = load_login()
    server, _ = configured_endpoint(args.server, args.netbird_server, saved)
    if not has_credentials(server, saved):
        raise ValueError("Run pr-rerun login before using official aliases")
    token = selected_token(server, saved, login=False)
    try:
        with httpx.Client(
            base_url=server,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            if args.command == "list":
                value = request(client, "GET", "/api/repository-aliases")["aliases"]
                if not isinstance(value, dict):
                    raise ValueError("Invalid official alias response")
                for alias, name in sorted(value.items()):
                    print(f"{safe_text(alias)} -> {safe_text(name)} (official)")
                if not value:
                    print("No official aliases available for your account.")
                return 0
            path = f"/api/repositories/{repository}/policy"
            current = request(client, "GET", path)
            policy = current["policy"]
            if "aliases" not in policy:
                raise ValueError("Official aliases require server version 0.3.2 or later")
            names = policy.get("aliases", [])
            if args.command == "set":
                policy["aliases"] = sorted(set(names) | {args.name})
            else:
                if args.name not in names:
                    raise ValueError("Official alias is not assigned to this repository")
                policy["aliases"] = [name for name in names if name != args.name]
            result = request(
                client, "POST", path, {"expected_revision": current["revision"], "policy": policy}
            )
            print(f"Official aliases updated for {repository} (revision {result['revision']}).")
            return 0
    except httpx.HTTPError:
        raise ValueError(
            "Service unavailable; inspect current policy before retrying an update"
        ) from None


def has_credentials(server: str, saved: SavedLogin | None) -> bool:
    return bool(os.environ.get("PR_RERUN_ACCESS_TOKEN")) or (
        saved is not None and httpx.URL(server) == httpx.URL(saved.server)
    )


def show_result(result: dict) -> int:
    print(f"Request: {safe_text(result['request_id'])}")
    print(f"Submission state: {safe_text(result['state'])}")
    if result.get("error"):
        print(safe_text(result["error"]))
    for run in result["runs"]:
        print(f"  {safe_text(run['name'])}: {safe_text(run['state'])}")
        if run.get("message"):
            print(f"    {safe_text(run['message'])}")
        print(f"    {safe_text(run['url'])}")
    print("Accepted means GitHub accepted the request; it does not mean the jobs passed.")
    return 0 if result["state"] in {"accepted", "rerun_observed"} else 2


def github_login(server: str, netbird: bool, repository: str | None) -> None:
    with httpx.Client(
        base_url=server, timeout=40, follow_redirects=False, trust_env=False
    ) as client:
        config = request(client, "GET", "/api/auth/config")
        if config.get("github_login") is not True:
            raise ValueError(
                "GitHub login is not enabled on this server; "
                "legacy users can use login --token-login."
            )
        started = request(client, "POST", "/api/auth/device/start", {})
        if started.get("verification_uri") != "https://github.com/login/device":
            raise ValueError("Unexpected GitHub verification URL")
        ticket, code = started.get("ticket"), started.get("user_code")
        expires, interval = started.get("expires_in"), started.get("interval")
        if (
            not isinstance(ticket, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket)
            or not isinstance(code, str)
            or not re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", code)
            or type(expires) is not int
            or not 1 <= expires <= 1800
            or type(interval) is not int
            or not 1 <= interval <= 60
        ):
            raise ValueError("Invalid login response")
        print("Open https://github.com/login/device in your browser.")
        print(f"Enter code: {code}")
        print("Waiting for GitHub authorization...")
        deadline = time.monotonic() + expires
        while time.monotonic() < deadline:
            time.sleep(interval)
            result = request(client, "POST", "/api/auth/device/poll", {"ticket": ticket})
            if result.get("state") == "authorized":
                token = result.get("access_token")
                if not isinstance(token, str) or not re.fullmatch(TOKEN_PATTERN, token):
                    raise ValueError("Invalid login response")
                client.headers["Authorization"] = f"Bearer {token}"
                verify_login(client)
                repository = resolve_selection(client, repository)
                save_login(SavedLogin(server, netbird, token, repository))
                print(f"Logged in as @{safe_text(result['login'])}.")
                print("Login saved. You can now run: pr-rerun <PR-number>")
                return
            interval = result.get("interval")
            if (
                result.get("state") != "pending"
                or type(interval) is not int
                or not 1 <= interval <= 300
            ):
                raise ValueError("Invalid login response")
        raise ValueError("GitHub login expired; run 'pr-rerun login' again.")


def run_client(
    client: httpx.Client,
    target: str,
    request_id: str | None,
    repository: str | None = None,
    *,
    verbose: bool = False,
) -> int:
    if target == "status":
        if not request_id or not re.fullmatch(r"rr_[0-9a-f]{32}", request_id):
            raise ValueError("Usage: pr-rerun status rr_<request-id>")
        return show_result(request(client, "GET", f"/api/reruns/{request_id}"))
    if request_id is not None or not re.fullmatch(r"[1-9][0-9]{0,9}", target):
        raise ValueError("Specify a positive PR number, or 'status' and a request ID")
    if not sys.stdin.isatty():
        raise ValueError("An interactive terminal is required to inspect and confirm a PR")
    payload: dict = {"pr": int(target)}
    if repository is not None:
        payload["repository"] = repository
    with progress("Checking PR and workflows"):
        preview = request(client, "POST", "/api/reruns/preview", payload)
    pr = preview["pr"]
    print(f"Repository: {safe_text(preview['repository'])}")
    print(f"PR #{pr['number']}: {safe_text(pr['title'])}")
    print(f"Author: @{safe_text(pr['author'])} (verified against your service identity)")
    print(f"Head: {safe_text(pr['head_sha'])}")
    print(f"Base: {safe_text(pr['base_ref'])}")
    print(safe_text(pr["url"]))
    if verbose:
        for reason in preview.get("skipped", []):
            print(f"Excluded: {safe_text(reason)}")
    if not preview["eligible"]:
        print("No verified failed runs are eligible. No rerun was requested.")
        if preview.get("skipped") and not verbose:
            print("Use --verbose to see excluded runs.")
        return 2
    print("Proposed reruns:")
    for run in preview["runs"]:
        print(f"  {safe_text(run['name'])}: rerun {run['mode']} (attempt {run['attempt']})")
        print(f"    Failed: {safe_text(', '.join(run['failed_jobs']))}")
        print(f"    {safe_text(run['url'])}")
    request_id = preview["request_id"]
    print(f"Request ID: {safe_text(request_id)}")
    print(f"Confirm reruns for: {safe_text(preview['confirmation'])}")
    answer = input("Confirm? [y/N] ").strip().lower()
    if answer != "y":
        print("Cancelled. No rerun was requested.")
        return 1
    try:
        with progress("Rechecking and submitting reruns"):
            result = request(
                client,
                "POST",
                "/api/reruns/confirm",
                {
                    "ticket": preview["ticket"],
                    "confirmation": preview["confirmation"],
                },
            )
    except (httpx.HTTPError, ValueError):
        print("Confirmation response was unavailable. Do not blindly submit another rerun.")
        print(f"Inspect: pr-rerun status {safe_text(request_id)}")
        return 2
    return show_result(result)


def show_stats(args) -> int:
    from contextlib import redirect_stdout
    from urllib.parse import urlencode

    if args.request_id is not None:
        raise ValueError("Use stats --repo REPOSITORY without a PR or request ID")
    days = args.days if args.days is not None else 7
    limit = args.limit if args.limit is not None else 20
    if not 1 <= days <= 30 or not 1 <= limit <= 200:
        raise ValueError("Use --days 1..30 and --limit 1..200")
    saved = load_login()
    server, _ = configured_endpoint(args.server, args.netbird_server, saved)
    if not has_credentials(server, saved):
        raise ValueError("Run pr-rerun login before querying statistics")
    repository = args.repo or os.environ.get("PR_RERUN_REPOSITORY")
    if repository is None and saved is not None and httpx.URL(saved.server) == httpx.URL(server):
        repository = saved.repository
    if repository is None:
        raise ValueError("Select a statistics repository with --repo")
    with httpx.Client(
        base_url=server,
        headers={"Authorization": "Bearer " + selected_token(server, saved, login=False)},
        timeout=60,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        with redirect_stdout(sys.stderr):
            repository = resolve_selection(client, repository)
        params = {"days": days, "sort": args.sort or "failures", "limit": limit}
        for name in ("event", "branch", "workflow", "job"):
            if value := getattr(args, name):
                params[name] = value
        with progress("Checking CI statistics"):
            result = request(
                client, "GET", f"/api/repositories/{repository}/stats?{urlencode(params)}"
            )
    if not isinstance(result.get("jobs"), list) or type(result.get("complete")) is not bool:
        raise ValueError("Invalid statistics response")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Repository: {safe_text(result['repository'])}")
        print(f"Window: {safe_text(result['since'])} to {safe_text(result['until'])}")
        print(
            f"Last sync: {safe_text(str(result['last_sync']))}; "
            f"complete={result['complete']}; stale={result['stale']}"
        )
        print("First attempts only. Failure rate = (failures + timeouts) / terminal samples.")
        print("WORKFLOW / JOB | SAMPLES | FAILED | TIMEOUT | RATE | RECOVERED/RETRIED")
        for row in result["jobs"]:
            rate = "-" if row["failure_rate"] is None else f"{row['failure_rate']:.1%}"
            print(
                f"{safe_text(row['workflow'])} / {safe_text(row['job'])} | {row['samples']} | "
                f"{row['failures']} | {row['timeouts']} | {rate} | "
                f"{row['recovered']}/{row['retried_failures']}"
            )
            for step, count in list(row["failed_steps"].items())[:3]:
                print(f"  Failed step: {safe_text(step)} ({count})")
        print(f"Showing {len(result['jobs'])} of {result['total_groups']} job groups.")
        if not result["complete"] or result["stale"]:
            print("Warning: incomplete or stale data; ask the operator to run ci-sync.")
    return 0 if result["complete"] and not result["stale"] else 1


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "alias":
        raise SystemExit(aliases.main(sys.argv[2:], official_alias_command))
    parser = argparse.ArgumentParser(description="Preview and confirm reruns for your own PRs")
    from pr_rerun.version import VERSION

    parser.add_argument("--version", action="version", version=f"pr-rerun {VERSION}")
    parser.add_argument(
        "target",
        help="PR number, 'login', 'logout', 'status', 'policy', 'alias', 'repos', or 'stats'",
    )
    parser.add_argument("request_id", nargs="?")
    parser.add_argument("--verbose", action="store_true", help="Show excluded runs in the preview")
    endpoint = parser.add_mutually_exclusive_group()
    endpoint.add_argument(
        "--server", help="HTTPS server, or PR_RERUN_URL (default: saved login, then loopback)"
    )
    endpoint.add_argument(
        "--netbird-server",
        help="Server at a verified NetBird IP; allows HTTP (or set PR_RERUN_NETBIRD_URL)",
    )
    parser.add_argument(
        "--repo", help="Full repository name or local alias; saved as default during login"
    )
    parser.add_argument(
        "--policy-file", type=Path, help="For policy: JSON with expected_revision and policy"
    )
    parser.add_argument("--history", action="store_true", help="For policy: show policy history")
    parser.add_argument(
        "--json", action="store_true", help="For repos/stats: output structured JSON"
    )
    parser.add_argument(
        "--token-login",
        action="store_true",
        help="For login: use a legacy administrator-issued token",
    )
    parser.add_argument("--days", type=int, help="For stats: 1..30 days (default: 7)")
    parser.add_argument("--sort", choices=("failures", "failure-rate"), help="For stats: ranking")
    parser.add_argument("--limit", type=int, help="For stats: 1..200 groups (default: 20)")
    for name in ("event", "branch", "workflow", "job"):
        parser.add_argument("--" + name, help="For stats: exact " + name + " filter")
    args = parser.parse_args()
    try:
        if args.target in {"login", "logout"} and args.request_id is not None:
            raise ValueError(f"Usage: pr-rerun {args.target}")
        if args.target == "logout":
            if args.server is not None or args.netbird_server is not None:
                raise ValueError("Logout clears local login; do not specify a server")
            removed = clear_login()
            print("Saved login removed." if removed else "No saved login.")
            if os.environ.get("PR_RERUN_ACCESS_TOKEN"):
                print("PR_RERUN_ACCESS_TOKEN is still set; unset it to stop using that token.")
            return
        if args.target != "policy" and (args.policy_file is not None or args.history):
            raise ValueError("Policy options require the policy command")
        if args.json and args.target not in {"repos", "stats"}:
            raise ValueError("--json requires the repos or stats command")
        if args.target != "stats" and any(
            getattr(args, name) is not None
            for name in ("days", "sort", "limit", "event", "branch", "workflow", "job")
        ):
            raise ValueError("Statistics options require the stats command")
        if args.token_login and args.target != "login":
            raise ValueError("--token-login requires the login command")
        if args.target == "stats":
            raise SystemExit(show_stats(args))
        if args.target == "repos":
            list_repositories(args)
            return
        saved = load_login()
        server, netbird = configured_endpoint(args.server, args.netbird_server, saved)
        repository = args.repo or os.environ.get("PR_RERUN_REPOSITORY")
        if (
            repository is None
            and saved is not None
            and httpx.URL(server) == httpx.URL(saved.server)
        ):
            repository = saved.repository
        if repository is not None:
            try:
                expanded = aliases.resolve_repository(repository)
            except aliases.UnknownAlias:
                expanded = repository
                if args.target != "login" and not has_credentials(server, saved):
                    raise ValueError(
                        "Log in to resolve official aliases, or configure a local alias"
                    ) from None
            if expanded != repository:
                print(f"Local repository alias: {repository} -> {expanded}")
            repository = expanded
        logging_in = args.target == "login"
        if args.token_login and not logging_in:
            raise ValueError("--token-login requires the login command")
        if logging_in:
            print(f"Server: {safe_text(server)}")
            if not args.token_login:
                github_login(server, netbird, repository)
                return
        token = selected_token(server, saved, login=logging_in)
        with httpx.Client(
            base_url=server,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15 if logging_in else 300,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            repository = resolve_selection(client, repository)
            if logging_in:
                verify_login(client)
                save_login(SavedLogin(server, netbird, token, repository))
                print(f"Login saved to {safe_text(config_path())} (owner-only access).")
                print("You can now run: pr-rerun <PR-number>")
                code = 0
            elif args.target == "policy":
                if (
                    repository is None
                    or args.request_id is not None
                    or (args.history and args.policy_file)
                ):
                    raise ValueError(
                        "Use policy --repo owner/repo [--history | --policy-file FILE]"
                    )
                path = f"/api/repositories/{repository}/" + (
                    "history" if args.history else "policy"
                )
                payload = json.loads(args.policy_file.read_text()) if args.policy_file else None
                value = request(client, "POST" if payload is not None else "GET", path, payload)
                print(json.dumps(value, indent=2))
                code = 0
            else:
                code = run_client(
                    client, args.target, args.request_id, repository, verbose=args.verbose
                )
    except (ValueError, OSError, httpx.HTTPError) as error:
        print(f"Error: {safe_text(error)}", file=sys.stderr)
        code = 2
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
