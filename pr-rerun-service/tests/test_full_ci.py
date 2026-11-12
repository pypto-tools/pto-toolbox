"""Full CI executes trusted main while testing a separately pinned PR merge."""

import copy
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import HEAD, REPO, Remote, confirm, preview
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pr_rerun.api import create_app
from pr_rerun.github import GitHub
from pr_rerun.models import UpstreamError, WorkflowRule

BASE, MERGE, MAIN = "b" * 40, "c" * 40, "d" * 40
FULL = ".github/workflows/ci-full.yml"
LEGACY = ".github/workflows/ci.yml"


class FullRemote(Remote):
    def __init__(self):
        super().__init__()
        self.pr["base"]["sha"] = BASE
        self.pr["merge_commit_sha"] = MERGE
        self.ref_sha: str | None = MERGE
        self.commit = {"sha": MERGE, "parents": [{"sha": BASE}, {"sha": HEAD}]}
        self.workflows = {
            LEGACY: {"id": 11, "path": LEGACY, "state": "active"},
            FULL: {"id": 33, "path": FULL, "state": "active"},
        }
        self.add_full(303)
        self.full_calls = []
        self.before_full_call = None

    def add_full(self, run_id, path=FULL, age=60, **updates):
        run = self.run(run_id, "ci")
        run.update(
            path=path,
            workflow_id=self.workflows[path]["id"],
            name="CI Full",
            event="workflow_dispatch",
            head_sha=MAIN,
            head_branch="main",
            head_repository={"id": 44},
            # Main-branch PR links can refer to an unrelated historical PR.
            pull_requests=[{"number": 5}],
            display_title=f"CI full PR #123 @{HEAD} base:{BASE}",
            created_at=(datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
        )
        run.update(updates)
        self.runs[run_id] = run
        return run

    def __call__(self, request):
        path = request.url.path
        if "/actions/workflows/" in path or "/git/" in path:
            assert request.method == "GET"
            self.full_calls.append(request)
            if self.before_full_call:
                self.before_full_call(request)
            if path == f"/repos/{REPO}/git/ref/pull/123/merge":
                if self.ref_sha is None:
                    return httpx.Response(404, json={})
                return httpx.Response(
                    200,
                    json={
                        "ref": "refs/pull/123/merge",
                        "object": {"sha": self.ref_sha, "type": "commit"},
                    },
                )
            if path == f"/repos/{REPO}/git/commits/{MERGE}":
                return httpx.Response(200, json=copy.deepcopy(self.commit))
            for workflow_path, workflow in self.workflows.items():
                prefix = f"/repos/{REPO}/actions/workflows/{workflow_path.rsplit('/', 1)[1]}"
                if path == prefix:
                    return httpx.Response(200, json=copy.deepcopy(workflow))
                if path == f"{prefix}/runs":
                    assert request.url.params["event"] == "workflow_dispatch"
                    assert request.url.params["branch"] == "main"
                    runs = [
                        r
                        for r in self.runs.values()
                        if r["event"] == "workflow_dispatch" and r["path"] == workflow_path
                    ]
                    return httpx.Response(
                        200, json={"total_count": len(runs), "workflow_runs": copy.deepcopy(runs)}
                    )
        return super().__call__(request)


@pytest.fixture
def full_remote():
    return FullRemote()


@pytest.fixture
def full_client(settings, full_remote):
    rules = tuple(WorkflowRule(path=p, identity="pypto_ci") for p in (LEGACY, FULL))
    settings = settings.model_copy(update={"workflows": rules})
    github = GitHub(REPO, "github-test-token", httpx.MockTransport(full_remote))
    app = create_app(settings, github)
    token = app.state.service.store.issue_token(7, "alice")
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield client
    github.close()


def selected(client):
    result = client.post("/api/reruns/preview", json={"pr": 123})
    assert result.status_code == 200, result.text
    return [run["run_id"] for run in result.json()["runs"]]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled"])
def test_full_run_not_in_checks_is_rerunnable(full_client, full_remote, conclusion):
    full_remote.runs[303]["conclusion"] = conclusion
    full_remote.job_overrides[303] = [{"name": "unit-tests", "conclusion": conclusion}]
    plan = preview(full_client)
    assert {r["run_id"] for r in plan["runs"]} == {101, 303}
    assert plan["pr"]["base_sha"] == BASE
    assert plan["pr"]["merge_sha"] == MERGE
    assert not full_remote.writes
    assert confirm(full_client, plan).json()["state"] == "accepted"
    assert set(full_remote.writes) == {
        f"/repos/{REPO}/actions/runs/101/rerun-failed-jobs",
        f"/repos/{REPO}/actions/runs/303/rerun-failed-jobs",
    }


def test_old_full_and_lint_share_path_but_have_independent_plans(full_client, full_remote):
    del full_remote.runs[303]
    full_remote.add_full(302, path=LEGACY)
    assert selected(full_client) == [101, 302]


@pytest.mark.parametrize("new_path,old_path", [(FULL, LEGACY), (LEGACY, FULL)])
@pytest.mark.parametrize(
    "state,conclusion", [("completed", "success"), ("in_progress", None), ("waiting", None)]
)
def test_newest_request_suppresses_old_failures_across_workflows(
    full_client, full_remote, new_path, old_path, state, conclusion
):
    full_remote.runs.pop(303)
    old = full_remote.add_full(301, path=old_path, age=120, run_number=9999)
    full_remote.add_full(
        304, path=new_path, age=60, run_number=1, status=state, conclusion=conclusion
    )
    old["updated_at"] = datetime.now(timezone.utc).isoformat()
    full_remote.linked.append(301)
    assert selected(full_client) == [101]


def test_wrong_base_in_latest_request_does_not_resurrect_older_run(full_client, full_remote):
    full_remote.add_full(304, age=30, display_title=f"CI full PR #123 @{HEAD} base:{MAIN}")
    assert selected(full_client) == [101]


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_branch", "feature"),
        ("head_repository", {"id": 88}),
        ("workflow_id", 999),
        ("event", "push"),
        ("repository", {"id": 99, "full_name": REPO}),
        ("repository", {"id": 44, "full_name": "elsewhere/repo"}),
        ("display_title", f"CI full PR #124 @{HEAD} base:{BASE}"),
        ("display_title", f"CI full PR #123 @{MAIN} base:{BASE}"),
        ("display_title", f"CI full PR #123 @{HEAD} base:{MAIN}"),
        ("display_title", f"CI full PR #123 @{HEAD} base:{BASE}\n"),
    ],
)
def test_untrusted_or_wrong_target_is_excluded(full_client, full_remote, field, value):
    full_remote.runs[303][field] = value
    full_remote.linked.append(303)
    assert selected(full_client) == [101]
    assert not full_remote.writes


@pytest.mark.parametrize(
    "change",
    ["merge_missing", "parents", "commit", "base", "branch", "workflow_path", "workflow_disabled"],
)
def test_current_merge_and_workflow_identity_are_required(full_client, full_remote, change):
    if change == "merge_missing":
        full_remote.ref_sha = None
    elif change == "parents":
        full_remote.commit["parents"].reverse()
    elif change == "commit":
        full_remote.commit["sha"] = MAIN
    elif change == "base":
        full_remote.pr["base"]["sha"] = MAIN
    elif change == "branch":
        full_remote.pr["base"]["ref"] = "release"
        full_remote.runs[101]["pull_requests"][0]["base"]["ref"] = "release"
    elif change == "workflow_path":
        full_remote.workflows[FULL]["path"] = LEGACY
    else:
        full_remote.workflows[FULL]["state"] = "disabled_manually"
    assert selected(full_client) == [101]


@pytest.mark.parametrize("change", ["base", "head", "merge", "attempt", "success", "new_request"])
def test_confirmation_revalidates_full_request(full_client, full_remote, change):
    plan = preview(full_client)
    if change == "base":
        full_remote.pr["base"]["sha"] = MAIN
    elif change == "head":
        full_remote.pr["head"]["sha"] = MAIN
        full_remote.graph_head = MAIN
    elif change == "merge":
        full_remote.ref_sha = None
    elif change == "attempt":
        full_remote.runs[303]["run_attempt"] += 1
    elif change == "success":
        full_remote.runs[303]["conclusion"] = "success"
    else:
        full_remote.add_full(304, age=30)
    assert confirm(full_client, plan).json()["state"] == "rejected"
    assert not full_remote.writes


@pytest.mark.parametrize("change", ["base", "new_request"])
def test_changes_during_final_validation_prevent_write(full_client, full_remote, change):
    plan = preview(full_client)
    count = 0

    def mutate(request):
        nonlocal count
        matches = (
            "/git/commits/" in request.url.path
            if change == "base"
            else request.url.path.endswith("/ci-full.yml/runs")
        )
        if matches:
            count += 1
            if count == 2:  # confirm rebuild then final per-run validation
                if change == "base":
                    full_remote.pr["base"]["sha"] = MAIN
                else:
                    full_remote.add_full(304, age=30)

    full_remote.before_full_call = mutate
    assert confirm(full_client, plan).json()["state"] == "rejected"
    assert not full_remote.writes


def test_foreign_author_never_queries_full_history(full_client, full_remote):
    full_remote.pr["user"]["id"] = 999
    assert full_client.post("/api/reruns/preview", json={"pr": 123}).status_code == 403
    assert not full_remote.full_calls


def test_adapter_is_explicit_and_failed_only():
    with pytest.raises(ValidationError):
        WorkflowRule(path=FULL, identity="pypto_ci", mode="all")
    with pytest.raises(ValidationError):
        WorkflowRule(path=".github/workflows/other.yml", identity="pypto_ci")


@pytest.mark.parametrize(
    "fault", ["truncated", "count", "duplicate", "invalid_date", "invalid_count"]
)
def test_discovery_rejects_incomplete_or_invalid_history(fault):
    remote = FullRemote()
    run = remote.runs[303]
    count, batch = 2, [run]
    if fault == "count":
        count = 0
    elif fault == "duplicate":
        batch = [run, run]
    elif fault == "invalid_date":
        run["created_at"] = "yesterday"
        count = 1
    elif fault == "invalid_count":
        count = True
    github = GitHub(
        REPO,
        "test",
        httpx.MockTransport(
            lambda _: httpx.Response(200, json={"total_count": count, "workflow_runs": batch})
        ),
    )

    try:
        with pytest.raises(UpstreamError):
            github.latest_full_run([FULL], 123, HEAD)
    finally:
        github.close()


def test_history_pagination_finds_candidate_beyond_first_page():
    remote = FullRemote()
    runs = [remote.add_full(1000 + index, display_title="CI push main") for index in range(100)]
    calls = []

    def respond(request):
        page = int(request.url.params["page"])
        calls.append(page)
        return httpx.Response(
            200,
            json={"total_count": 101, "workflow_runs": runs if page == 1 else [remote.runs[303]]},
        )

    github = GitHub(REPO, "test", httpx.MockTransport(respond))
    try:
        result = github.latest_full_run([FULL], 123, HEAD)
        assert result is not None and result["id"] == 303
        assert calls == [1, 2]
    finally:
        github.close()


def test_dense_history_is_split_before_github_truncates_it():
    remote = FullRemote()
    windows = []

    def respond(request):
        window = request.url.params["created"]
        windows.append(window)
        if len(windows) == 1:
            return httpx.Response(200, json={"total_count": 1200, "workflow_runs": []})
        lower, upper = [
            datetime.fromisoformat(value.replace("Z", "+00:00")) for value in window.split("..")
        ]
        created = datetime.fromisoformat(remote.runs[303]["created_at"])
        batch = [remote.runs[303]] if lower <= created <= upper else []
        return httpx.Response(200, json={"total_count": len(batch), "workflow_runs": batch})

    github = GitHub(REPO, "test", httpx.MockTransport(respond))
    try:
        result = github.latest_full_run([FULL], 123, HEAD)
        assert result is not None and result["id"] == 303
        assert len(windows) == 3
        left_end = datetime.fromisoformat(windows[1].split("..")[1].replace("Z", "+00:00"))
        right_start = datetime.fromisoformat(windows[2].split("..")[0].replace("Z", "+00:00"))
        assert right_start - left_end == timedelta(seconds=1)
    finally:
        github.close()


def test_history_lookup_budget_fails_closed(monkeypatch):
    monkeypatch.setattr("pr_rerun.github.MAX_PAGES", 1)
    github = GitHub(
        REPO,
        "test",
        httpx.MockTransport(
            lambda _: httpx.Response(200, json={"total_count": 1000, "workflow_runs": []})
        ),
    )
    try:
        with pytest.raises(UpstreamError, match="lookup limit"):
            github.latest_full_run([FULL], 123, HEAD)
    finally:
        github.close()


def test_merge_ref_supports_app_responses_without_merge_sha(full_client, full_remote):
    full_remote.pr.pop("merge_commit_sha")
    assert set(selected(full_client)) == {101, 303}


@pytest.mark.parametrize(
    "response",
    [
        {"ref": "refs/pull/124/merge", "object": {"type": "commit", "sha": MERGE}},
        {"ref": "refs/pull/123/merge", "object": {"type": "tag", "sha": MERGE}},
        {"ref": "refs/pull/123/merge", "object": {"type": "commit", "sha": "../../other"}},
        {},
    ],
)
def test_merge_ref_is_bound_to_requested_pr(response):
    github = GitHub(REPO, "test", httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    try:
        with pytest.raises(UpstreamError, match="merge ref"):
            github.merge_ref(123)
    finally:
        github.close()
