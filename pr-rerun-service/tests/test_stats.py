"""Analytics: pagination, attempt semantics, permissions and credential isolation."""

import json
import time

import httpx
import pytest
from test_cli_login import invoke, mock_server
from test_registry import change
from test_registry import multi as registry_fixture
from test_repository_inventory import installation, make_auth

from pr_rerun.app_auth import AppAuth
from pr_rerun.client_config import SavedLogin, save_login
from pr_rerun.models import ServiceError, UpstreamError
from pr_rerun.stats import StatsStore, report, stats_path
from pr_rerun.stats_collect import enrollment, sync
from pr_rerun.stats_github import StatsGitHub, iso, read_run

multi = registry_fixture
NOW = 1793520000
RECORD = {"name": "hw-native-sys/pypto", "repository_id": 44, "installation_id": 55}
TOKEN = "prr_" + "b" * 43


def run(identity=101, attempt=1, **updates):
    return {
        "id": identity,
        "repository": {"id": 44},
        "workflow_id": 12,
        "path": ".github/workflows/ci.yml",
        "event": "pull_request",
        "head_branch": "feature",
        "head_sha": "a" * 40,
        "created_at": iso(NOW - 86400),
        "updated_at": iso(NOW - 60),
        "run_attempt": attempt,
        "status": "completed",
        "conclusion": "failure",
        **updates,
    }


def job(identity=1, attempt=1, result="failure", name="compile (a5, cpu)", **updates):
    return {
        "id": identity,
        "run_id": 101,
        "run_attempt": attempt,
        "name": name,
        "status": "completed",
        "conclusion": result,
        "started_at": iso(NOW - 100),
        "completed_at": iso(NOW - 40),
        "steps": [{"name": "Build", "conclusion": result}],
        **updates,
    }


class ReadAuth(AppAuth):
    def __init__(self):
        self.calls = []

    def read_credentials(self, *identity):
        self.calls.append(identity)
        return "read-token", time.time() + 3600


def remote(rows, jobs, *, budget=100):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer read-token"
        if request.url.path.endswith("/jobs"):
            attempt = int(request.url.path.split("/")[-2])
            values = jobs[attempt]
            return httpx.Response(200, json={"total_count": len(values), "jobs": values})
        if request.url.path.endswith("/runs"):
            return httpx.Response(200, json={"total_count": len(rows), "workflow_runs": rows})
        identity = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json=next(row for row in rows if row["id"] == identity))

    client = StatsGitHub(ReadAuth(), RECORD, transport=httpx.MockTransport(respond), budget=budget)
    return client, calls


def test_first_attempts_recovery_and_incremental_reruns(tmp_path):
    store = StatsStore(tmp_path / "stats.sqlite")
    runs = [run(attempt=2)]
    jobs = {
        1: [
            job(),
            job(2, result="success", name="unit"),
            job(3, result="cancelled", name="cancelled"),
            job(4, result="timed_out", name="timeout"),
            job(5, result="skipped", name="skipped"),
        ],
        2: [job(), job(6, attempt=2, result="success")],
    }
    client, calls = remote(runs, jobs)
    try:
        assert sync(client, store, RECORD, now=NOW)["complete"]
        value = report(store.path, RECORD, now=NOW)
        compile_row = next(row for row in value["jobs"] if row["job"].startswith("compile"))
        assert compile_row["samples"] == 1 and compile_row["failures"] == 1
        assert compile_row["failure_rate"] == 1 and compile_row["recovered"] == 1
        assert compile_row["rerun_executions"] == 1
        assert compile_row["failed_steps"] == {"Build": 1}
        assert compile_row["p95_seconds"] == 60
        assert next(r for r in value["jobs"] if r["job"] == "cancelled")["samples"] == 0
        assert next(r for r in value["jobs"] if r["job"] == "timeout")["timeouts"] == 1
        calls.clear()
        assert sync(client, store, RECORD, now=NOW + 1)["runs_fetched"] == 0
        assert len(calls) == 1  # list old runs, reuse terminal job metadata
        runs[0]["run_attempt"] = 3
        jobs[3] = [job(7, attempt=3, result="failure")]
        assert sync(client, store, RECORD, now=NOW + 2)["runs_fetched"] == 1
        value = report(store.path, RECORD, now=NOW + 2)
        row = next(r for r in value["jobs"] if r["job"].startswith("compile"))
        assert row["failures"] == 1 and row["rerun_executions"] == 2
        assert report(store.path, RECORD, days=30, now=NOW)["complete"] is False
        assert report(store.path, RECORD, now=NOW + 3600)["stale"] is True
        assert report(store.path, RECORD, event="push", now=NOW)["jobs"] == []
        assert report(store.path, RECORD, branch="main", now=NOW)["jobs"] == []
    finally:
        client.close()


def test_duplicate_names_do_not_guess_recovery(tmp_path):
    client, _ = remote(
        [run(attempt=2)], {1: [job(), job(2)], 2: [job(3, attempt=2, result="success")]}
    )
    try:
        store = StatsStore(tmp_path / "stats.sqlite")
        sync(client, store, RECORD, now=NOW)
        row = report(store.path, RECORD, now=NOW)["jobs"][0]
        assert row["samples"] == 2 and row["recovered"] == 0
        assert row["ambiguous_recovery_runs"] == 1
    finally:
        client.close()


def test_failure_preserves_checkpoint_and_marks_partial(tmp_path):
    store = StatsStore(tmp_path / "stats.sqlite")
    client, _ = remote([run()], {1: [job()]}, budget=2)
    try:
        state = sync(client, store, RECORD, now=NOW)
        assert not state["complete"] and state["error_count"] == 1
        assert not report(store.path, RECORD, now=NOW)["complete"]
        client.budget = 100
        assert sync(client, store, RECORD, now=NOW)["complete"]
        assert len(report(store.path, RECORD, now=NOW)["jobs"]) == 1
    finally:
        client.close()


def test_deleted_runs_reconciled_only_after_complete_listing(tmp_path):
    rows = [run()]
    client, _ = remote(rows, {1: [job()]})
    try:
        store = StatsStore(tmp_path / "stats.sqlite")
        sync(client, store, RECORD, now=NOW)
        rows.clear()
        assert sync(client, store, RECORD, now=NOW)["complete"]
        assert report(store.path, RECORD, now=NOW)["jobs"] == []
    finally:
        client.close()


def test_run_search_cap_splits_windows(monkeypatch):
    client, _ = remote([], {})
    windows = []

    def get(path, params=None):
        assert params is not None
        window = params["created"]
        windows.append(window)
        if len(windows) == 1:
            return {"total_count": 1000}
        value = run(identity=len(windows), created_at=iso(NOW - 2 if len(windows) == 2 else NOW))
        return {"total_count": 1, "workflow_runs": [value]}

    monkeypatch.setattr(client, "get", get)
    try:
        assert len(list(client.runs(NOW - 2, NOW))) == 2
        assert len(windows) == 3 and windows[1] != windows[2]
        monkeypatch.setattr(client, "get", lambda *a, **k: {"total_count": 1000})
        with pytest.raises(UpstreamError, match="per-second"):
            list(client.runs(NOW, NOW))
    finally:
        client.close()


def test_jobs_paginate_and_reject_truncation(monkeypatch):
    client, _ = remote([], {})
    pages = []

    def get(path, params=None):
        assert params is not None
        pages.append(params["page"])
        values = [job(i) for i in range(1, 101)] if params["page"] == 1 else [job(101)]
        return {"total_count": 101, "jobs": values}

    monkeypatch.setattr(client, "get", get)
    try:
        assert len(list(client.jobs(101, 1))) == 101 and pages == [1, 2]
        monkeypatch.setattr(client, "get", lambda *a, **k: {"total_count": 2, "jobs": [job()]})
        with pytest.raises(UpstreamError, match="incomplete"):
            list(client.jobs(101, 1))
    finally:
        client.close()


@pytest.mark.parametrize(
    "status,headers", [(403, {}), (429, {}), (200, {"x-ratelimit-remaining": "99"})]
)
def test_rate_limits_stop_without_echoing_upstream_secrets(status, headers):
    client = StatsGitHub(
        ReadAuth(),
        RECORD,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(status, headers=headers, json={"secret": "TOKEN"})
        ),
    )
    try:
        with pytest.raises(UpstreamError) as caught:
            client.get(client.prefix)
        assert "TOKEN" not in str(caught.value) and client.blocked
        with pytest.raises(ValueError, match="Unsupported"):
            client.get(client.prefix + "/101/rerun")
    finally:
        client.close()


def test_read_run_rejects_mutation_during_collection(monkeypatch):
    client, _ = remote([run(attempt=2)], {1: [job()]})
    try:
        with pytest.raises(UpstreamError, match="changed"):
            read_run(client, run(attempt=1))
    finally:
        client.close()


def test_read_tokens_cannot_reuse_write_credentials(monkeypatch):
    requests = []

    def respond(req):
        requests.append(req)
        if req.url.path.endswith("/installation"):
            return httpx.Response(200, json=installation(55))
        if req.url.path.endswith("/access_tokens"):
            assert json.loads(req.content) == {
                "repository_ids": [44],
                "permissions": {"actions": "read", "metadata": "read"},
            }
            return httpx.Response(
                201,
                json={
                    "token": "read-token",
                    "expires_at": iso(int(time.time()) + 3600),
                    "permissions": {"actions": "read", "metadata": "read"},
                },
            )
        assert req.headers["Authorization"] == "Bearer read-token"
        return httpx.Response(200, json={"id": 44, "full_name": RECORD["name"]})

    auth = make_auth(monkeypatch, respond)
    auth.tokens[(RECORD["name"], 55)] = ("write-token", time.time() + 3600)
    try:
        assert auth.read_credentials(RECORD["name"], 44, 55)[0] == "read-token"
        assert auth.tokens[(RECORD["name"], 55)][0] == "write-token"
        assert len([req for req in requests if req.method == "POST"]) == 1
    finally:
        auth.close()


def test_api_permissions_revocation_and_separate_database(multi, monkeypatch):
    client, registry, auth, manager, outsider = multi
    monkeypatch.setattr(
        auth, "read_credentials", lambda *a: ("read", time.time() + 3600), raising=False
    )
    path = stats_path(registry.settings.database)
    remote_client, _ = remote([run()], {1: [job()]})
    try:
        sync(remote_client, StatsStore(path), RECORD, now=int(time.time()))
    finally:
        remote_client.close()
    url = "/api/repositories/hw-native-sys/pypto/stats"
    assert client.get(url).status_code == 403
    change(registry, manager, analytics_enabled=True)
    assert client.get(url).status_code == 403
    client.headers["Authorization"] = "Bearer " + manager
    assert client.get(url).status_code == 200
    assert client.get(url + "?days=31").status_code == 422
    assert client.get(url + "?sort=garbage").status_code == 422
    change(registry, manager, analytics_access="repository_users")
    client.headers["Authorization"] = "Bearer " + outsider
    assert client.get(url).status_code == 403
    change(registry, manager, access="authenticated_users")
    assert client.get(url).status_code == 200
    change(registry, manager, analytics_access="maintainers")
    registry.store.set_service_admin(9, enabled=True)
    assert client.get(url).status_code == 200
    monkeypatch.setattr(
        auth,
        "read_credentials",
        lambda *a: (_ for _ in ()).throw(ServiceError(403, "Installation revoked")),
    )
    assert client.get(url).status_code == 403
    assert not auth.remote.writes
    client.headers["Authorization"] = "Bearer invalid"
    assert client.get(url).status_code == 401
    with pytest.raises(ValueError, match="separate"):
        stats_path(registry.settings.database, registry.settings.database)


def test_enrollment_read_only_and_disabled_by_default(multi):
    _, registry, _, manager, _ = multi
    before = registry.store.users()
    with pytest.raises(ValueError, match="analytics_enabled"):
        enrollment(registry.settings.database, RECORD["name"])
    change(registry, manager, analytics_enabled=True)
    assert enrollment(registry.settings.database, RECORD["name"])["repository_id"] == 44
    assert registry.store.users() == before


def test_cli_stats_saved_login_alias_and_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in (
        "PR_RERUN_URL",
        "PR_RERUN_NETBIRD_URL",
        "PR_RERUN_ACCESS_TOKEN",
        "PR_RERUN_REPOSITORY",
    ):
        monkeypatch.delenv(name, raising=False)
    save_login(SavedLogin("http://127.0.0.1:8080", False, TOKEN, RECORD["name"]))
    payload = {"repository": RECORD["name"], "complete": True, "stale": False, "jobs": []}
    calls = []

    def respond(req):
        calls.append(req)
        if "/repository-aliases/" in req.url.path:
            return httpx.Response(200, json={"repository": RECORD["name"]})
        assert req.method == "GET" and req.url.params["days"] == "7"
        return httpx.Response(200, json=payload)

    mock_server(monkeypatch, respond)
    assert invoke(monkeypatch, "stats", "--repo", "pypto", "--json") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload and "Official repository alias" in captured.err
    payload["complete"] = False
    assert invoke(monkeypatch, "stats", "--json") == 1
    assert invoke(monkeypatch, "stats", "--days", "31") == 2
    assert invoke(monkeypatch, "123", "--days", "7") == 2
    assert invoke(monkeypatch, "stats", "123") == 2


def test_cli_text_output_escapes_job_names_and_reports_partial(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", TOKEN)
    store = StatsStore(tmp_path / "stats.sqlite")
    client, _ = remote([run()], {1: [job(name="bad\x1b[31mjob")]})
    try:
        sync(client, store, RECORD, now=NOW)
    finally:
        client.close()
    value = report(store.path, RECORD, now=NOW + 4000)
    mock_server(monkeypatch, lambda req: httpx.Response(200, json=value))
    assert (
        invoke(monkeypatch, "stats", "--repo", RECORD["name"], "--server", "http://127.0.0.1:8080")
        == 1
    )
    output = capsys.readouterr().out
    assert "100.0%" in output and "stale" in output
    assert "\x1b" not in output and "Failed step: Build (1)" in output


def test_cli_stats_old_server_and_missing_login(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", TOKEN)
    mock_server(monkeypatch, lambda req: httpx.Response(404, json={"detail": "Not Found"}))
    assert (
        invoke(monkeypatch, "stats", "--repo", RECORD["name"], "--server", "http://127.0.0.1:8080")
        == 2
    )
    assert "0.5.0" in capsys.readouterr().err
    monkeypatch.delenv("PR_RERUN_ACCESS_TOKEN")
    assert invoke(monkeypatch, "stats", "--repo", RECORD["name"]) == 2
    assert "login" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["write", "identity", "suspended"])
def test_read_credentials_fail_closed(monkeypatch, bad):
    def respond(req):
        if req.url.path.endswith("/installation"):
            return httpx.Response(
                200, json=installation(55, suspended_at="now" if bad == "suspended" else None)
            )
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "not-for-output",
                    "expires_at": iso(int(time.time()) + 3600),
                    "permissions": {"actions": "write" if bad == "write" else "read"},
                },
            )
        return httpx.Response(200, json={"id": 99, "full_name": RECORD["name"]})

    auth = make_auth(monkeypatch, respond)
    try:
        with pytest.raises(ServiceError) as caught:
            auth.read_credentials(RECORD["name"], 44, 55)
        assert "not-for-output" not in str(caught.value)
        assert not auth.read_tokens
    finally:
        auth.close()


def test_run_list_missing_page_never_marked_complete(monkeypatch, tmp_path):
    client, _ = remote([], {})
    monkeypatch.setattr(client, "get", lambda *a, **k: {"total_count": 1, "workflow_runs": []})
    try:
        store = StatsStore(tmp_path / "stats.sqlite")
        state = sync(client, store, RECORD, now=NOW)
        assert state["complete"] is False and state["error_count"] == 1
        assert report(store.path, RECORD, now=NOW)["complete"] is False
    finally:
        client.close()
