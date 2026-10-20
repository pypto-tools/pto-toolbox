"""Load an explicitly selected credential without logging credential material."""

import os
import re
import subprocess
from pathlib import Path


def github_credential(auth: str | None = None) -> str:
    mode = auth if auth is not None else os.environ.get("PR_RERUN_GITHUB_AUTH", "token")
    token = os.environ.get("PR_RERUN_GITHUB_TOKEN")
    token_file = os.environ.get("PR_RERUN_GITHUB_TOKEN_FILE")
    if mode not in {"token", "gh"}:
        raise ValueError("PR_RERUN_GITHUB_AUTH must be token or gh")
    if mode == "token":
        if token_file:
            if token:
                raise ValueError("Set either PR_RERUN_GITHUB_TOKEN or its _FILE variant")
            token = Path(token_file).read_text().strip()
        if not token:
            raise ValueError(
                "Configure PR_RERUN_GITHUB_TOKEN[_FILE], or select --github-auth gh "
                "to use an existing GitHub CLI login"
            )
        return token
    if token or token_file:
        raise ValueError("Unset PR_RERUN_GITHUB_TOKEN and its _FILE variant when selecting gh")
    arguments = ["gh", "auth", "token", "--hostname", "github.com"]
    if user := os.environ.get("PR_RERUN_GITHUB_USER"):
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", user):
            raise ValueError("PR_RERUN_GITHUB_USER must be a GitHub username")
        arguments.extend(["--user", user])
    environment = os.environ.copy()
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_DEBUG",
    ):
        environment.pop(key, None)
    environment["GH_PROMPT_DISABLED"] = "1"
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        # In particular, never propagate CalledProcessError stdout/stderr.
        raise ValueError(
            "Cannot read the GitHub CLI login. Run gh auth login --hostname github.com "
            "as the same OS user, then restart the service."
        ) from None
    token = result.stdout.strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("GitHub CLI did not return a usable credential")
    return token
