"""Error excerpt boundaries: credentials, bounded downloads, scopes and resumability."""

import json
import sqlite3
import time

import httpx
import pytest
from test_cli_login import invoke, mock_server
from test_registry import change
from test_registry import multi as registry_fixture
from test_stats import NOW, RECORD, TOKEN, ReadAuth, job, remote, run

from pr_rerun.client_config import SavedLogin, save_login
from pr_rerun.error_collect import sync
from pr_rerun.error_logs import LogGitHub, extract, scrub
from pr_rerun.errors import ErrorStore, candidates, read
from pr_rerun.models import ServiceError, UpstreamError
from pr_rerun.stats import StatsStore
from pr_rerun.stats_collect import sync as stats_sync

multi = registry_fixture
STORAGE = "https://example.blob.core.windows.net/log?sig=never-display"


def downloader(respond, budget=100):
    return LogGitHub(ReadAuth(), RECORD, transport=httpx.MockTransport(respond), budget=budget)


def seeded(path, jobs=None, attempt=1, now=NOW):
    client, _ = remote([run(attempt=attempt)], jobs or {1: [job()]})
    try:
        stats_sync(client, StatsStore(path), RECORD, days=30, now=now)
    finally:
        client.close()
    return ErrorStore(path)


def test_download_step_uses_fixed_api_and_never_forwards_auth():
    calls = []

    def respond(req):
        calls.append(req)
        if req.url.host == "api.github.com":
            assert req.url.path.endswith("/actions/jobs/12/steps/4/logs")
            assert req.headers["authorization"] == "Bearer read-token"
            return httpx.Response(302, headers={"location": STORAGE})
        assert str(req.url) == STORAGE
        assert "authorization" not in req.headers
        return httpx.Response(200, text="main.cpp:10: error: unknown type\nTOKEN=not-for-users")

    client = downloader(respond)
    try:
        result = client.download(12, 4)
        assert result["status"] == "ready" and result["category"] == "compile"
        assert "not-for-users" not in json.dumps(result) and "never-display" not in json.dumps(
            result
        )
        assert client.requests == 1 and len(calls) == 2
    finally:
        client.close()


@pytest.mark.parametrize(
    "target",
    [
        "http://example.blob.core.windows.net/log",
        "https://example.blob.core.windows.net.evil.test/log",
        "https://127.0.0.1/log",
        "https://user:password@example.blob.core.windows.net/log",
        "https://example.blob.core.windows.net:8443/log",
    ],
)
def test_log_redirect_rejects_untrusted_destinations(target):
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(302, headers={"location": target})

    client = downloader(respond)
    try:
        with pytest.raises(UpstreamError, match="storage destination"):
            client.download(1)
        assert len(calls) == 1
    finally:
        client.close()


@pytest.mark.parametrize(
    "status,headers",
    [
        (429, {"retry-after": "120"}),
        (403, {"x-ratelimit-reset": str(NOW + 3600)}),
        (302, {"x-ratelimit-remaining": "999"}),
    ],
)
def test_log_quota_pause_does_not_download_or_leak_response(status, headers):
    client = downloader(
        lambda req: httpx.Response(status, headers={**headers, "location": STORAGE}, text="SECRET")
    )
    try:
        with pytest.raises(UpstreamError) as caught:
            client.download(1)
        assert client.blocked and client.pause_until > time.time()
        assert "SECRET" not in str(caught.value) and "never-display" not in str(caught.value)
    finally:
        client.close()


def test_log_size_limit_and_missing_logs(monkeypatch):
    import pr_rerun.error_logs as module

    monkeypatch.setattr(module, "MAX_LOG_BYTES", 30)
    client = downloader(
        lambda req: httpx.Response(302, headers={"location": STORAGE})
        if req.url.host == "api.github.com"
        else httpx.Response(200, content=b"error: " + b"x" * 100)
    )
    try:
        result = client.download(1)
        assert result["truncated"] and result["downloaded_bytes"] == 30
    finally:
        client.close()
    client = downloader(lambda req: httpx.Response(410))
    try:
        assert client.download(1)["status"] == "unavailable"
    finally:
        client.close()


def test_scrubbing_context_and_heuristic_fingerprints():
    secret = "ghp_" + "a" * 36
    source = (
        "start\n\x1b[31mfile.cpp:42: error: failed build\x1b[0m\n"
        "Password=hidden\nAuthorization: Bearer not-allowed\n"
        "https://user:pass@host/path?sig=private\n" + secret + "\n"
        "-----BEGIN RSA PRIVATE KEY-----\nkeybody\n-----END RSA PRIVATE KEY-----\n"
    )
    result = extract(source.encode())
    for private in ("hidden", "not-allowed", "user:pass", "sig=private", secret, "keybody", "\x1b"):
        assert private not in json.dumps(result)
    assert "start" in result["excerpt"] and result["category"] == "compile"
    assert result["fingerprint"] == extract(source.replace(":42:", ":97:").encode())["fingerprint"]
    assert extract(b"ordinary output")["category"] == "unclassified"
    assert "keybody" not in scrub("-----BEGIN PRIVATE KEY-----\nkeybody")
    assert scrub("https://[invalid")
    assert "json-secret" not in scrub('{"token":"json-secret"}')
    assert extract(b"ordinary output")["fingerprint"] != extract(b"unrelated output")["fingerprint"]


def test_queue_attempts_resume_and_scope(tmp_path):
    store = seeded(
        tmp_path / "stats.sqlite",
        {1: [job(1), job(2, result="success")], 2: [job(3, attempt=2)]},
        attempt=2,
    )
    items, metadata = candidates(store.path, RECORD, 30, NOW)
    assert len(items) == 2 and {i["attempt"] for i in items} == {1, 2}

    def respond(req):
        return (
            httpx.Response(302, headers={"location": STORAGE})
            if req.url.host == "api.github.com"
            else httpx.Response(200, text="error: build failed")
        )

    first = downloader(respond)
    try:
        result = sync(first, store, RECORD, days=30, max_logs=1, now=NOW)
        assert result["coverage"]["ready"] == 1 and result["coverage"]["pending"] == 1
    finally:
        first.close()
    second = downloader(respond)
    try:
        result = sync(second, store, RECORD, days=30, now=NOW)
        assert result["complete"] and result["processed"] == 1
        value = read(store.path, RECORD, days=30, now=NOW, limit=1)
        assert (
            value["total"] == 2 and value["next_offset"] == 1 and value["groups"][0]["count"] == 2
        )
        assert "excerpt" not in value["items"][0]
        identity = value["items"][0]["id"]
        assert read(store.path, RECORD, error_id=identity, now=NOW)["error"]["excerpt"]
        with pytest.raises(ServiceError, match="not found"):
            read(store.path, {**RECORD, "installation_id": 99}, error_id=identity, now=NOW)
    finally:
        second.close()


def test_retries_stop_after_three_and_respect_pause(tmp_path):
    store = seeded(tmp_path / "stats.sqlite")
    client = downloader(lambda req: httpx.Response(500, text="private failure"))
    try:
        for offset in (0, 601, 1202):
            sync(client, store, RECORD, now=NOW + offset)
        value = read(store.path, RECORD, now=NOW + 1202)
        assert value["items"][0]["status"] == "failed" and value["items"][0]["tries"] == 3
        before = client.requests
        sync(client, store, RECORD, now=NOW + 2000)
        assert client.requests == before
    finally:
        client.close()
    store.state(RECORD, {"pause_until": NOW + 3600})
    client = downloader(lambda req: (_ for _ in ()).throw(AssertionError("must not call")))
    try:
        assert sync(client, store, RECORD, now=NOW)["pause_until"] == NOW + 3600
        assert client.requests == 0
    finally:
        client.close()


def test_metadata_partial_and_policy_change_never_mark_complete(tmp_path):
    store = seeded(tmp_path / "stats.sqlite")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE ci_sync SET data=?",
            (json.dumps({"since": NOW - 30 * 86400, "until": NOW, "complete": False}),),
        )
    client = downloader(lambda req: httpx.Response(410))
    try:
        result = sync(client, store, RECORD, now=NOW)
        assert not result["complete"] and result["coverage"]["unavailable"] == 1
    finally:
        client.close()
    store = seeded(tmp_path / "other.sqlite")
    client = downloader(lambda req: (_ for _ in ()).throw(AssertionError("must not call")))
    try:
        with pytest.raises(ValueError, match="changed"):
            sync(
                client,
                store,
                RECORD,
                now=NOW,
                authorize=lambda: (_ for _ in ()).throw(ValueError("changed")),
            )
        assert read(store.path, RECORD, now=NOW)["sync"]["error_count"] == 1
    finally:
        client.close()


def test_error_permissions_are_independent_and_rechecked(multi, monkeypatch):
    client, registry, auth, manager, outsider = multi
    monkeypatch.setattr(
        auth, "read_credentials", lambda *a: ("read", time.time() + 3600), raising=False
    )
    change(
        registry,
        manager,
        analytics_enabled=True,
        analytics_access="repository_users",
        access="authenticated_users",
    )
    url = "/api/repositories/hw-native-sys/pypto/errors"
    assert client.get(url).status_code == 403
    change(registry, manager, error_logs_enabled=True)
    assert client.get(url).status_code == 403
    client.headers["Authorization"] = "Bearer " + manager
    assert client.get(url).status_code == 200
    assert client.get(url + "?days=31").status_code == 422
    assert client.get(url + "?category=nonsense").status_code == 400
    change(registry, manager, error_logs_access="repository_users")
    client.headers["Authorization"] = "Bearer " + outsider
    assert client.get(url).status_code == 200

    def revoked(*args):
        change(registry, manager, error_logs_enabled=False)
        return "read", time.time() + 3600

    monkeypatch.setattr(auth, "read_credentials", revoked)
    assert client.get(url).status_code == 403
    assert not auth.remote.writes
    client.headers["Authorization"] = "Bearer invalid"
    assert client.get(url).status_code == 401


def test_cli_errors_and_detail_json_and_literal_text(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in (
        "PR_RERUN_URL",
        "PR_RERUN_NETBIRD_URL",
        "PR_RERUN_ACCESS_TOKEN",
        "PR_RERUN_REPOSITORY",
    ):
        monkeypatch.delenv(name, raising=False)
    save_login(SavedLogin("http://127.0.0.1:8080", False, TOKEN, RECORD["name"]))
    store = seeded(tmp_path / "stats.sqlite")
    client = downloader(
        lambda req: httpx.Response(302, headers={"location": STORAGE})
        if req.url.host == "api.github.com"
        else httpx.Response(200, text="error: [red]literal[/red]")
    )
    try:
        sync(client, store, RECORD, now=NOW)
    finally:
        client.close()
    value = read(store.path, RECORD, days=30, now=NOW)
    error = read(store.path, RECORD, error_id=value["items"][0]["id"], now=NOW)
    response_status = [200]
    mock_server(
        monkeypatch,
        lambda req: httpx.Response(
            response_status[0],
            json=(value if req.url.path.endswith("/errors") else error)
            if response_status[0] == 200
            else {"detail": "Not Found"},
        ),
    )
    assert invoke(monkeypatch, "errors", "--days", "30", "--json") == 0
    assert json.loads(capsys.readouterr().out) == value
    assert invoke(monkeypatch, "error", error["error"]["id"]) == 0
    output = capsys.readouterr().out
    assert "[red]literal[/red]" in output and "\x1b" not in output
    assert invoke(monkeypatch, "error", "../../secret") == 2
    assert invoke(monkeypatch, "errors", "--days", "0") == 2
    assert invoke(monkeypatch, "stats", "--group") == 2
    response_status[0] = 404
    assert invoke(monkeypatch, "errors") == 2
    assert "0.6.0" in capsys.readouterr().err


def test_error_collection_uses_metadata_snapshot_window(tmp_path):
    from pr_rerun.stats_github import iso

    path = tmp_path / "stats.sqlite"
    client, _ = remote([run(created_at=iso(NOW - 30 * 86400 + 60))], {1: [job()]})
    try:
        stats_sync(client, StatsStore(path), RECORD, days=30, now=NOW)
    finally:
        client.close()
    items, metadata = candidates(path, RECORD, 30, NOW + 120)
    assert len(items) == 1 and metadata["until"] == NOW
    store = ErrorStore(path)
    client = downloader(lambda req: httpx.Response(410))
    try:
        sync(client, store, RECORD, now=NOW + 120)
        value = read(path, RECORD, days=30, now=NOW + 120)
        assert value["total"] == 1 and value["until"] == iso(NOW)
    finally:
        client.close()
