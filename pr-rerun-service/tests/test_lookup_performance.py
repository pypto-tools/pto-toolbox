import copy
import threading

import pytest
from conftest import REPO, confirm, preview

from pr_rerun.cli import progress
from pr_rerun.github import GitHub
from pr_rerun.models import UpstreamError


def test_run_inspections_overlap_with_bounded_concurrency(client, remote, monkeypatch):
    remote.linked = list(range(101, 109))
    remote.runs = {run_id: remote.run(run_id, "ci") for run_id in remote.linked}
    barrier = threading.Barrier(4, timeout=5)
    lock = threading.Lock()
    active = peak = 0
    original = GitHub.run

    def get_run(github, run_id):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait()
            return original(github, run_id)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(GitHub, "run", get_run)
    plan = preview(client)
    assert peak == 4 and active == 0
    assert [run["run_id"] for run in plan["runs"]] == [108]
    assert not remote.writes


def test_branch_history_shared_only_within_one_build(client, remote):
    for run_id in (101, 102, 103, 104):
        remote.runs[run_id] = remote.run(run_id, "ci")
        remote.runs[run_id]["pull_requests"] = []
    remote.linked = [101, 102, 103, 104]
    plan = preview(client)
    assert remote.head_queries == 1
    assert [run["run_id"] for run in plan["runs"]] == [104]
    assert confirm(client, plan).json()["state"] == "accepted"
    assert remote.head_queries == 3  # New evidence at confirmation and before writing.
    assert remote.writes == [f"/repos/{REPO}/actions/runs/104/rerun-failed-jobs"]


def test_parallel_lookup_failure_does_not_return_partial_plan(client, remote, monkeypatch):
    original = GitHub.run

    def fail_one(github, run_id):
        if run_id == 202:
            raise UpstreamError("Cannot verify run")
        return original(github, run_id)

    monkeypatch.setattr(GitHub, "run", fail_one)
    result = client.post("/api/reruns/preview", json={"pr": 123})
    assert result.status_code == 502
    assert "ticket" not in result.json() and not remote.writes


def test_parallel_lookup_keeps_newer_success_and_deterministic_exclusions(client, remote):
    remote.runs[102] = copy.deepcopy(remote.runs[101])
    remote.runs[102].update(id=102, run_number=102, conclusion="success")
    remote.runs[203] = remote.run(203, "unapproved")
    remote.runs[204] = remote.run(204, "unapproved")
    remote.linked = [101, 102, 203, 204]
    result = client.post("/api/reruns/preview", json={"pr": 123}).json()
    assert not result["eligible"]
    assert result["skipped"] == [
        "Run 204: workflow is not allowed.",
        "Run 203: workflow is not allowed.",
        "Run 102: completed / success.",
    ]
    assert not remote.writes


def test_progress_starts_before_work_and_reports_while_waiting(monkeypatch):
    reported = threading.Event()
    messages = []

    class Output:
        encoding = "utf-8"

        def isatty(self):
            return True

        def write(self, message):
            messages.append(message)
            if "⠙" in message:
                reported.set()

        def flush(self):
            pass

    monkeypatch.setattr("pr_rerun.cli.sys.stderr", Output())
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("pr_rerun.cli.PROGRESS_INTERVAL", 0.01)
    with progress("Checking PR"):
        assert "⠋ Checking PR (" in "".join(messages)
        assert reported.wait(5)
    output = "".join(messages)
    assert "\n" not in output
    assert output.endswith("\r\033[2K")
    assert not any(t.name == "pr-rerun-progress" for t in threading.enumerate())


def test_progress_stops_on_interruption_without_claiming_success(capsys):
    with pytest.raises(KeyboardInterrupt), progress("Checking PR"):
        raise KeyboardInterrupt
    output = capsys.readouterr().err
    assert "Waiting stopped" in output and "Response received" not in output
    assert not any(t.name == "pr-rerun-progress" for t in threading.enumerate())


@pytest.mark.parametrize("tty,term", [(False, "xterm"), (True, "dumb")])
def test_progress_without_terminal_animation_is_plain_and_does_not_poll(monkeypatch, tty, term):
    import io

    output = io.StringIO()
    monkeypatch.setattr(output, "isatty", lambda: tty)
    monkeypatch.setattr("pr_rerun.cli.sys.stderr", output)
    monkeypatch.setenv("TERM", term)
    with progress("Checking PR"):
        assert not any(t.name == "pr-rerun-progress" for t in threading.enumerate())
    assert output.getvalue().count("\n") == 2
    assert "\r" not in output.getvalue() and "\033" not in output.getvalue()


def test_spinner_clears_on_interrupt_and_fits_narrow_ascii_terminal(monkeypatch):
    import io
    import os

    class Terminal(io.StringIO):
        encoding = "ascii"

        def isatty(self):
            return True

    output = Terminal()
    monkeypatch.setattr("pr_rerun.cli.sys.stderr", output)
    monkeypatch.setattr("pr_rerun.cli.shutil.get_terminal_size", lambda: os.terminal_size((20, 24)))
    monkeypatch.setenv("TERM", "xterm")
    with pytest.raises(KeyboardInterrupt), progress("Checking a very long description"):
        raise KeyboardInterrupt
    text = output.getvalue()
    assert text.startswith("\r\033[2K| ") and text.endswith("\r\033[2K")
    assert all(len(frame) <= 19 for frame in text.split("\r\033[2K"))
    assert not any(t.name == "pr-rerun-progress" for t in threading.enumerate())
