import copy
import gzip
import json
import zlib

import httpx
import pytest
from conftest import HEAD, REPO, archive

from pr_rerun.github import CHECK_QUERY, HEAD_PULLS_QUERY, GitHub
from pr_rerun.models import ServiceError, UpstreamError


def encoded_response(status, content, encoding="gzip", headers=None):
    compressed = gzip.compress(content) if encoding == "gzip" else zlib.compress(content)
    return httpx.Response(
        status,
        headers={
            **(headers or {}),
            "Content-Encoding": encoding,
            "Content-Length": str(len(compressed)),
        },
        stream=httpx.ByteStream(compressed),
    )


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("operation", ["user", "pull", "checks", "jobs", "rerun"])
def test_compressed_github_responses_are_decoded_once(remote, encoding, operation):
    def respond(request):
        original = remote(request)
        return encoded_response(original.status_code, original.content, encoding)

    github = GitHub(REPO, "github-test-token", httpx.MockTransport(respond))
    try:
        if operation == "user":
            assert github.user("alice") == {"id": 7, "login": "alice", "type": "User"}
        elif operation == "pull":
            assert github.pull(123)["head"]["sha"] == HEAD
        elif operation == "checks":
            assert github.checked_runs(123, HEAD) == [202, 101]
        elif operation == "jobs":
            assert github.jobs(101, 1)[0]["conclusion"] == "failure"
        else:
            github.rerun(101, "failed")
            assert remote.writes == [f"/repos/{REPO}/actions/runs/101/rerun-failed-jobs"]
    finally:
        github.close()


def test_decoded_response_keeps_metadata_and_updates_body_headers():
    content = b'{"id":7,"login":"alice","type":"User"}'
    github = GitHub(
        REPO,
        "secret",
        httpx.MockTransport(
            lambda request: encoded_response(
                200,
                content,
                headers={"Content-Type": "application/json", "X-GitHub-Request-Id": "id"},
            )
        ),
    )
    try:
        result = github._request("GET", "/users/alice")
        assert result.content == content
        assert "content-encoding" not in result.headers
        assert result.headers["content-length"] == str(len(content))
        assert result.headers["x-github-request-id"] == "id"
        assert result.headers["content-type"] == "application/json"
    finally:
        github.close()


def test_compressed_response_size_limit_applies_after_decompression(monkeypatch):
    monkeypatch.setattr("pr_rerun.github.MAX_BYTES", 128)
    content = b"x" * 4096
    assert len(gzip.compress(content)) < 128
    github = GitHub(
        REPO,
        "secret",
        httpx.MockTransport(lambda request: encoded_response(200, content)),
    )
    try:
        with pytest.raises(UpstreamError, match="size limit"):
            github.user("alice")
    finally:
        github.close()


@pytest.mark.parametrize("write", [False, True])
def test_corrupt_compression_is_sanitized_and_never_retried(write):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            201 if write else 200,
            headers={"Content-Encoding": "gzip"},
            stream=httpx.ByteStream(b"credential=secret-sensitive-not-gzip"),
        )

    github = GitHub(REPO, "secret", httpx.MockTransport(respond))
    try:
        with pytest.raises(UpstreamError, match="invalid compressed response") as error:
            if write:
                github.rerun(101, "failed")
            else:
                github.user("alice")
        assert error.value.uncertain == write
        assert "secret" not in str(error.value)
        assert len(calls) == 1
    finally:
        github.close()


@pytest.mark.parametrize(
    "method,path,payload,write",
    [
        ("POST", f"/repos/{REPO}/actions/runs/101/cancel", None, True),
        ("DELETE", f"/repos/{REPO}/actions/runs/101", None, True),
        ("POST", f"/repos/{REPO}/pulls/123/merge", None, True),
        ("POST", "/repos/other/repo/actions/runs/101/rerun", None, True),
        ("GET", "https://evil.example/secret", None, False),
        ("POST", "/graphql", {"query": "mutation { deleteSomething }"}, False),
        ("POST", f"/repos/{REPO}/actions/runs/101/rerun", {"ref": "main"}, True),
        ("GET", f"/repos/{REPO}/contents/secrets", None, False),
    ],
)
def test_gateway_rejects_every_other_operation(github, remote, method, path, payload, write):
    with pytest.raises(ServiceError):
        github._request(method, path, payload=payload, write=write)
    assert not remote.calls


def test_jobs_paginate_and_pin_attempt():
    urls = []

    def respond(request):
        urls.append(str(request.url))
        assert "/runs/101/attempts/3/jobs" in request.url.path
        page = request.url.params["page"]
        jobs = (
            [{"name": "ok", "conclusion": "success"}] * 100
            if page == "1"
            else [{"name": "last failure", "conclusion": "failure"}]
        )
        return httpx.Response(200, json={"jobs": jobs})

    github = GitHub(REPO, "secret", httpx.MockTransport(respond))
    assert len(github.jobs(101, 3)) == 101
    assert len(urls) == 2
    github.close()


def test_branch_history_includes_all_states_and_drains_later_pages(remote):
    cursors = []

    def respond(request):
        body = json.loads(request.content)
        assert body["query"] == HEAD_PULLS_QUERY
        assert "states: [OPEN, CLOSED, MERGED]" in body["query"]
        cursors.append(body["variables"]["cursor"])
        data = remote(request).json()
        connection = data["data"]["repository"]["pullRequests"]
        if body["variables"]["cursor"] is None:
            connection["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
        else:
            connection["nodes"] = [dict(remote.head_pull(), number=124, state="CLOSED")]
        return httpx.Response(200, json=data)

    github = GitHub(REPO, "github-test-token", httpx.MockTransport(respond))
    try:
        assert [item["number"] for item in github.head_pull_requests("feature")] == [123, 124]
        assert cursors == [None, "next"]
    finally:
        github.close()


@pytest.mark.parametrize(
    "problem", ["repository", "errors", "page_state", "cursor", "missing_base", "duplicate"]
)
def test_branch_history_fails_closed_on_incomplete_or_invalid_data(remote, problem):
    def respond(request):
        data = remote(request).json()
        repository = data["data"]["repository"]
        connection = repository["pullRequests"]
        if problem == "repository":
            repository["nameWithOwner"] = "other/repo"
        elif problem == "errors":
            data["errors"] = [{"message": "partial data"}]
        elif problem == "page_state":
            connection["pageInfo"]["hasNextPage"] = "false"
        elif problem == "cursor":
            connection["pageInfo"] = {"hasNextPage": True, "endCursor": None}
        elif problem == "missing_base":
            connection["nodes"][0].pop("baseRepository")
        else:
            connection["nodes"].append(connection["nodes"][0])
        return httpx.Response(200, json=data)

    github = GitHub(REPO, "github-test-token", httpx.MockTransport(respond))
    try:
        with pytest.raises(UpstreamError):
            github.head_pull_requests("feature")
        assert not remote.writes
    finally:
        github.close()


def test_branch_history_never_returns_partial_result_when_later_page_fails(remote):
    def respond(request):
        if json.loads(request.content)["variables"]["cursor"] is not None:
            return httpx.Response(200, json={"data": None, "errors": [{"message": "rate limit"}]})
        data = remote(request).json()
        data["data"]["repository"]["pullRequests"]["pageInfo"] = {
            "hasNextPage": True,
            "endCursor": "next",
        }
        return httpx.Response(200, json=data)

    github = GitHub(REPO, "github-test-token", httpx.MockTransport(respond))
    try:
        with pytest.raises(UpstreamError, match="complete"):
            github.head_pull_requests("feature")
    finally:
        github.close()


def test_branch_history_page_limit_never_returns_a_partial_identity(remote, monkeypatch):
    monkeypatch.setattr("pr_rerun.github.MAX_PAGES", 1)

    def respond(request):
        data = remote(request).json()
        data["data"]["repository"]["pullRequests"]["pageInfo"] = {
            "hasNextPage": True,
            "endCursor": "next",
        }
        return httpx.Response(200, json=data)

    github = GitHub(REPO, "github-test-token", httpx.MockTransport(respond))
    try:
        with pytest.raises(UpstreamError, match="Too many"):
            github.head_pull_requests("feature")
    finally:
        github.close()


def test_graphql_paginates_checks_without_arbitrary_queries(remote):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        assert body["query"] == CHECK_QUERY
        calls.append(body["variables"]["cursor"])
        data = remote(request).json()
        contexts = data["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"
        ]["contexts"]
        if body["variables"]["cursor"] is None:
            contexts["nodes"] = contexts["nodes"][:1]
            contexts["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
        else:
            contexts["nodes"] = contexts["nodes"][1:]
        return httpx.Response(200, json=data)

    github = GitHub(REPO, "github-test-token", httpx.MockTransport(respond))
    assert github.checked_runs(123, HEAD) == [202, 101]
    assert calls == [None, "next"]
    github.close()


def test_repeated_graphql_cursor_fails_closed(remote):
    def respond(request):
        data = remote(request).json()
        context = data["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"
        ]["contexts"]
        context["pageInfo"] = {"hasNextPage": True, "endCursor": "repeated"}
        return httpx.Response(200, json=data)

    github = GitHub(REPO, "github-test-token", httpx.MockTransport(respond))
    with pytest.raises(UpstreamError, match="repeated"):
        github.checked_runs(123, HEAD)
    github.close()


@pytest.mark.parametrize("compressed", [False, True])
def test_artifact_redirect_drops_credentials_and_does_not_follow_again(compressed):
    observed = []

    def respond(request):
        observed.append(request)
        if request.url.host == "api.github.com":
            assert request.headers["authorization"] == "Bearer secret"
            if compressed:
                return encoded_response(
                    302,
                    b"redirect",
                    headers={
                        "Location": "https://production.blob.core.windows.net/signed?sig=private"
                    },
                )
            return httpx.Response(
                302,
                headers={"Location": "https://production.blob.core.windows.net/signed?sig=private"},
            )
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"artifact")

    github = GitHub(REPO, "secret", httpx.MockTransport(respond))
    assert github._artifact_bytes(9) == b"artifact"
    assert len(observed) == 2
    github.close()


@pytest.mark.parametrize(
    "location",
    [
        "http://production.blob.core.windows.net/file",
        "https://evil.example/file",
        "https://production.blob.core.windows.net.evil.example/file",
        "https://api.github.com/repos/other/repo",
        "https://user:password@production.blob.core.windows.net/file",
    ],
)
def test_artifact_redirect_rejects_other_origins(location):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": location})

    github = GitHub(REPO, "secret", httpx.MockTransport(respond))
    with pytest.raises(UpstreamError, match="Untrusted"):
        github._artifact_bytes(9)
    assert len(calls) == 1
    github.close()


def test_zip_members_are_checked_without_extracting_files(remote):
    def respond(request):
        if request.url.path.endswith("/zip"):
            return httpx.Response(200, content=archive(remote.manifest, "../review-target.json"))
        return remote(request)

    github = GitHub(REPO, "github-test-token", httpx.MockTransport(respond))
    with pytest.raises(UpstreamError, match="members"):
        github.codex_identity(202, 1)
    github.close()


def test_expired_future_and_older_identity_artifacts(remote):
    def respond(request):
        response = remote(request)
        if request.url.path.endswith("/artifacts"):
            original = response.json()["artifacts"][0]
            future = copy.deepcopy(original)
            future["name"] = "codex-target-202-4"
            expired = copy.deepcopy(original)
            expired.update(name="codex-target-202-2", expired=True)
            return httpx.Response(200, json={"artifacts": [future, expired, original]})
        return response

    github = GitHub(REPO, "github-test-token", httpx.MockTransport(respond))
    assert github.codex_identity(202, 3) == remote.manifest
    github.close()


def test_response_size_limit_before_json_parse(monkeypatch):
    monkeypatch.setattr("pr_rerun.github.MAX_BYTES", 16)
    github = GitHub(
        REPO, "secret", httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 17))
    )
    with pytest.raises(UpstreamError, match="size limit"):
        github.pull(123)
    github.close()


@pytest.mark.parametrize("code,uncertain", [(403, False), (422, False), (500, True), (503, True)])
@pytest.mark.parametrize("compressed", [False, True])
def test_write_errors_do_not_leak_upstream_body_or_auto_retry(code, uncertain, compressed):
    calls = []

    def respond(request):
        calls.append(request)
        if compressed:
            return encoded_response(code, b"credential=secret-sensitive")
        return httpx.Response(code, text="credential=secret-sensitive")

    github = GitHub(REPO, "secret", httpx.MockTransport(respond))
    with pytest.raises(UpstreamError) as error:
        github.rerun(101, "failed")
    assert error.value.uncertain == uncertain
    assert "sensitive" not in str(error.value)
    assert len(calls) == 1
    github.close()
