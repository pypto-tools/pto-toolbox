"""Bounded log downloads and deterministic, best-effort diagnostic extraction."""

import hashlib
import re
import time
from urllib.parse import urlsplit

import httpx

from pr_rerun.github import API
from pr_rerun.models import UpstreamError, positive_id
from pr_rerun.stats_github import StatsGitHub

MAX_LOG_BYTES = 4 * 1024 * 1024
PARSER_VERSION = 1
ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
SECRET = re.compile(
    r"(?i)(?:github_pat_[a-z0-9_]+|gh[pousr]_[a-z0-9_]+|prr_[a-z0-9_-]{20,}|"
    r"eyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+|AKIA[A-Z0-9]{16})"
)
ASSIGNMENT = re.compile(
    r"(?i)\b([\w.-]*(?:token|secret|password|passwd|credential|api[_-]?key|"
    r"private[_-]?key|authorization)[\w.-]*[\"']?\s*[=:]\s*).*$"
)
RULES = (
    (
        "network",
        r"could not resolve|connection (?:refused|reset|timed out)|"
        r"failed to connect|unable to access|fatal:.*(?:fetch|clone|repository)|"
        r"http[^\n]*(?:401|403|404|429|502|503)",
    ),
    ("resource", r"out of memory|oom.kill|no space left|disk quota exceeded"),
    ("timeout", r"timed out|timeout(?:error)?|exceeded.*time limit"),
    ("test", r"assertionerror|\bFAILED\b.*::|\bFAIL:|assert .*=="),
    ("compile", r"(?:fatal )?error:|undefined reference|linker command failed"),
    ("exception", r"traceback \(most recent|\b\w+(?:Error|Exception):"),
)
DIAGNOSTIC = re.compile(r"##\[error\]|\berror\b|\bfatal\b|\bfailed\b|exception|traceback", re.I)


def scrub(value: str) -> str:
    """No complete secrecy guarantee: log excerpts have a separate access policy."""
    value = ANSI.sub("", value)
    value = "".join(c for c in value if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))
    value = re.sub(
        r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.S,
    )
    value = SECRET.sub("[REDACTED]", value)
    value = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[REDACTED AUTH]", value)

    # Signed URLs and userinfo can disclose credentials; retain only the hostname.
    def redact_url(match):
        try:
            host = urlsplit(match[0]).hostname or "[redacted]"
        except ValueError:
            host = "[redacted]"
        return "https://" + host + "/[URL REDACTED]"

    value = re.sub(
        r"https?://[^\s<>\"']+",
        redact_url,
        value,
    )
    return "\n".join(ASSIGNMENT.sub(r"\1[REDACTED]", line) for line in value.splitlines())


def extract(raw: bytes, *, truncated: bool = False) -> dict:
    text = scrub(raw.decode("utf-8", errors="replace"))
    lines = [re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", line)[:2000] for line in text.splitlines()]
    category, match = "unclassified", None
    # Classifications are heuristics, not a claim about the root cause.
    for index, line in enumerate(lines):
        for name, pattern in RULES:
            if re.search(pattern, line, re.I):
                category, match = name, index
                break
        if match is not None:
            break
    if match is None:
        match = next((i for i, line in enumerate(lines) if DIAGNOSTIC.search(line)), None)
    found = match is not None
    if match is None:
        match = max(0, len(lines) - 20)
    selected = set(range(max(0, match - 5), min(len(lines), match + 25)))
    for index, line in enumerate(lines):
        if DIAGNOSTIC.search(line) and len(selected) < 70:
            selected.update(range(max(0, index - 2), min(len(lines), index + 4)))
    excerpt = "\n".join(f"{i + 1}: {lines[i]}" for i in sorted(selected)[:80])[:12000]
    summary = (
        lines[match][:300] if lines and found else "No recognized diagnostic; inspect context."
    )
    fingerprint_text = summary if found else "\n".join(lines[-20:])
    normalized = re.sub(r"\b(?:0x[0-9a-f]+|[0-9]+)\b", "#", fingerprint_text.lower())
    return {
        "category": category,
        "summary": summary,
        "excerpt": excerpt,
        "fingerprint": hashlib.sha256((category + normalized).encode()).hexdigest()[:24],
        "truncated": truncated,
        "parser_version": PARSER_VERSION,
    }


class LogGitHub(StatsGitHub):
    """Only GitHub-selected HTTPS storage targets; never forward the API token."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pause_until = 0
        self.downloaded_bytes = 0

    def download(self, job_id: int, step: int | None = None, *, max_bytes=None) -> dict:
        max_bytes = MAX_LOG_BYTES if max_bytes is None else min(MAX_LOG_BYTES, max_bytes)
        if max_bytes < 1:
            raise UpstreamError("Log byte budget exhausted.")
        if self.requests >= self.budget:
            raise UpstreamError("Log request budget exhausted.")
        if self.expires <= time.time() + 60:
            self.authorize()
        path = f"/repos/{self.name}/actions/jobs/{positive_id(job_id)}"
        if step is not None:
            path += f"/steps/{positive_id(step)}"
        self.requests += 1
        try:
            with self.client.stream(
                "GET",
                API + path + "/logs",
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            ) as response:
                if (
                    response.status_code in {401, 403, 429}
                    or int(response.headers.get("x-ratelimit-remaining", "999999")) < 1000
                ):
                    self.blocked = True
                    self.pause_until = max(
                        int(time.time()) + 60,
                        int(response.headers.get("x-ratelimit-reset", "0")),
                        int(time.time()) + int(response.headers.get("retry-after", "60")),
                    )
                    raise UpstreamError("Log collection paused to protect the GitHub API budget.")
                if response.status_code in {404, 410}:
                    return {"status": "unavailable", "reason": "Logs missing, expired or deleted."}
                if response.status_code != 302:
                    raise UpstreamError("Log download URL is unavailable; retry later.")
                target = response.headers.get("location", "")
            parsed = urlsplit(target)
            host = parsed.hostname or ""
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
                or parsed.fragment
                or not any(
                    host.endswith(suffix)
                    for suffix in (
                        ".blob.core.windows.net",
                        ".actions.githubusercontent.com",
                        ".actions-results.githubusercontent.com",
                    )
                )
            ):
                raise UpstreamError("Unrecognized log storage destination.")
            # This client has no default auth headers; the signed URL is never stored.
            with self.client.stream(
                "GET", target, headers={"Accept": "text/plain", "Accept-Encoding": "identity"}
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise UpstreamError("Log storage download failed; retry later.")
                body = bytearray()
                truncated = False
                started = time.monotonic()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if time.monotonic() - started > 30:
                        raise UpstreamError("Log download time limit exceeded.")
                    remaining = max_bytes - len(body)
                    body.extend(chunk[:remaining])
                    self.downloaded_bytes += len(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                        break
                return {
                    "status": "ready",
                    "downloaded_bytes": len(body),
                    **extract(bytes(body), truncated=truncated),
                }
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise UpstreamError("Invalid or unavailable log download.") from None
