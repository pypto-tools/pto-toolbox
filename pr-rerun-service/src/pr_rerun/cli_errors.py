"""Client-only error list/detail rendering; no log downloading on user machines."""

import json
import os
import re
import sys
from contextlib import redirect_stdout
from urllib.parse import urlencode

import httpx
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from pr_rerun.client_config import load_login


def show(args):
    from pr_rerun.cli import (
        configured_endpoint,
        has_credentials,
        progress,
        request,
        resolve_selection,
        safe_text,
        selected_token,
    )

    detail = args.target == "error"
    if detail:
        if not args.request_id or not re.fullmatch(r"err_[a-f0-9]{32}", args.request_id):
            raise ValueError("Use pr-rerun error err_ID --repo REPOSITORY")
        if args.group or any(
            getattr(args, k) is not None
            for k in ("days", "limit", "offset", "category", "workflow", "job", "event", "branch")
        ):
            raise ValueError("List filters require the errors command")
    elif args.request_id:
        raise ValueError("Use pr-rerun errors without an error ID")
    if args.sort is not None:
        raise ValueError("--sort requires the stats command")
    days, limit, offset = args.days or 7, args.limit or 20, args.offset or 0
    if (
        (args.days is not None and not 1 <= args.days <= 30)
        or (args.limit is not None and not 1 <= args.limit <= 200)
        or not 0 <= offset <= 100000
    ):
        raise ValueError("Use days 1..30, limit 1..200 and offset 0..100000")
    saved = load_login()
    server, _ = configured_endpoint(args.server, args.netbird_server, saved)
    if not has_credentials(server, saved):
        raise ValueError("Run pr-rerun login before querying errors")
    repo = args.repo or os.environ.get("PR_RERUN_REPOSITORY")
    if repo is None and saved and httpx.URL(saved.server) == httpx.URL(server):
        repo = saved.repository
    if repo is None:
        raise ValueError("Select a repository with --repo")
    with httpx.Client(
        base_url=server,
        timeout=60,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + selected_token(server, saved, login=False)},
    ) as client:
        with redirect_stdout(sys.stderr):
            repo = resolve_selection(client, repo)
        path = f"/api/repositories/{repo}/errors"
        if detail:
            path += "/" + args.request_id
        else:
            params = {"days": days, "limit": limit, "offset": offset}
            for name in ("workflow", "job", "category", "event", "branch"):
                if getattr(args, name) is not None:
                    params[name] = getattr(args, name)
            path += "?" + urlencode(params)
        with progress("Checking CI error excerpts"):
            result = request(client, "GET", path)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        console = Console(file=sys.stdout, highlight=False)
        console.print(Text("Repository: " + safe_text(result["repository"])))
        if detail:
            item = result["error"]
            for name in (
                "id",
                "workflow",
                "job",
                "step",
                "attempt",
                "status",
                "category",
                "summary",
                "reason",
                "url",
            ):
                if item.get(name) is not None:
                    console.print(Text(f"{name}: {safe_text(item[name])}"))
            if item.get("truncated"):
                console.print(
                    "Log exceeded the download limit; excerpt may omit the error.", style="yellow"
                )
            console.print(
                "Classification and redaction are best-effort; inspect the source to confirm."
            )
            for line in item.get("excerpt", "").splitlines():
                console.print(Text(safe_text(line), overflow="fold"))
        else:
            console.print(Text(f"Window: {result['since']} to {result['until']}"))
            coverage = result["coverage"]
            console.print(
                Text(
                    "Collection: "
                    + (
                        ", ".join(f"{k}={v}" for k, v in sorted(coverage.items()))
                        or "not collected yet"
                    )
                )
            )
            table = Table(box=box.ROUNDED, header_style="bold cyan")
            if args.group:
                for name in ("Category", "Count", "Summary", "Example ID"):
                    table.add_column(
                        name, justify="right" if name == "Count" else "left", overflow="fold"
                    )
                for item in result["groups"]:
                    table.add_row(
                        *(
                            Text(safe_text(item[k]))
                            for k in ("category", "count", "summary", "example_id")
                        )
                    )
                console.print(
                    "Similar diagnostic summaries (all attempts; not confirmed root causes):"
                )
            else:
                for name in ("Error ID / Job", "Step / Attempt", "Category / Status", "Summary"):
                    table.add_column(name, overflow="fold")
                for item in result["items"]:
                    table.add_row(
                        Text(
                            safe_text(item["id"])
                            + "\n"
                            + safe_text(item["workflow"])
                            + " / "
                            + safe_text(item["job"])
                        ),
                        Text(safe_text(item["step"]) + f"\nAttempt {item['attempt']}"),
                        Text(
                            safe_text(item.get("category", "-")) + "\n" + safe_text(item["status"])
                        ),
                        Text(
                            safe_text(
                                item.get("summary", item.get("reason", "Awaiting collection"))
                            )
                        ),
                    )
                console.print(
                    Text(
                        f"Showing {len(result['items'])} of {result['total']} error records "
                        "(all attempts)."
                    )
                )
            console.print(table)
            if not args.group and result.get("next_offset") is not None:
                console.print(f"Next page: repeat with --offset {result['next_offset']}")
            console.print("Details: pr-rerun error <error ID> --repo <repository>")
            if not result["metadata_complete"] or not result["logs_complete"] or result["stale"]:
                console.print(
                    "Partial or stale error coverage; background collection may still be pending.",
                    style="yellow",
                )
    if detail:
        return 0 if result["error"]["status"] == "ready" else 1
    return (
        0 if result["metadata_complete"] and result["logs_complete"] and not result["stale"] else 1
    )
