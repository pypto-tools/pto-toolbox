"""Interactive client. There is deliberately no --yes or client-selected run/mode."""

import argparse
import getpass
import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

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
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("Invalid service response")
    if response.is_error:
        raise ValueError(str(value.get("error", f"HTTP {response.status_code}")))
    return value


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
    client: httpx.Client, target: str, request_id: str | None, repository: str | None = None
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
    preview = request(client, "POST", "/api/reruns/preview", payload)
    pr = preview["pr"]
    print(f"Repository: {safe_text(preview['repository'])}")
    print(f"PR #{pr['number']}: {safe_text(pr['title'])}")
    print(f"Author: @{safe_text(pr['author'])} (verified against your service identity)")
    print(f"Head: {safe_text(pr['head_sha'])}")
    print(f"Base: {safe_text(pr['base_ref'])}")
    print(safe_text(pr["url"]))
    for reason in preview.get("skipped", []):
        print(f"Excluded: {safe_text(reason)}")
    if not preview["eligible"]:
        print("No verified failed runs are eligible. No rerun was requested.")
        return 2
    print("Proposed reruns:")
    for run in preview["runs"]:
        print(f"  {safe_text(run['name'])}: rerun {run['mode']} (attempt {run['attempt']})")
        print(f"    Failed: {safe_text(', '.join(run['failed_jobs']))}")
        print(f"    {safe_text(run['url'])}")
    request_id = preview["request_id"]
    print(f"Request ID: {safe_text(request_id)}")
    print(f"Type exactly: {safe_text(preview['confirmation'])}")
    answer = input("> ")
    if answer != preview["confirmation"]:
        print("Confirmation did not match. No rerun was requested.")
        return 1
    try:
        result = request(
            client,
            "POST",
            "/api/reruns/confirm",
            {
                "ticket": preview["ticket"],
                "confirmation": answer,
            },
        )
    except (httpx.HTTPError, ValueError):
        print("Confirmation response was unavailable. Do not blindly submit another rerun.")
        print(f"Inspect: pr-rerun status {safe_text(request_id)}")
        return 2
    return show_result(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview and confirm reruns for your own PRs")
    parser.add_argument("target", help="PR number, 'login', 'logout', or 'status'")
    parser.add_argument("request_id", nargs="?")
    endpoint = parser.add_mutually_exclusive_group()
    endpoint.add_argument(
        "--server", help="HTTPS server, or PR_RERUN_URL (default: saved login, then loopback)"
    )
    endpoint.add_argument(
        "--netbird-server",
        help="Server at a verified NetBird IP; allows HTTP (or set PR_RERUN_NETBIRD_URL)",
    )
    parser.add_argument("--repo", help="Target repository; saved as default during login")
    parser.add_argument(
        "--policy-file", type=Path, help="For policy: JSON with expected_revision and policy"
    )
    parser.add_argument("--history", action="store_true", help="For policy: show policy history")
    parser.add_argument(
        "--token-login",
        action="store_true",
        help="For login: use a legacy administrator-issued token",
    )
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
        if args.repo is not None and not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", args.repo):
            raise ValueError("--repo must be owner/repository")
        if args.target != "policy" and (args.policy_file is not None or args.history):
            raise ValueError("Policy options require the policy command")
        saved = load_login()
        server, netbird = configured_endpoint(args.server, args.netbird_server, saved)
        repository = args.repo or os.environ.get("PR_RERUN_REPOSITORY")
        if (
            repository is None
            and saved is not None
            and httpx.URL(server) == httpx.URL(saved.server)
        ):
            repository = saved.repository
        if repository is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository
        ):
            raise ValueError("Repository must be owner/repository")
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
                code = run_client(client, args.target, args.request_id, repository)
    except (ValueError, OSError, httpx.HTTPError) as error:
        print(f"Error: {safe_text(error)}", file=sys.stderr)
        code = 2
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
