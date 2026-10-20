import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from conftest import confirm, preview

from pr_rerun.listener import listeners


def test_login_persists_across_processes_using_existing_api(app, remote, tmp_path):
    token = app.state.service.store.issue_token(7, "alice")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    with listeners(None, 0) as opened:
        port = opened[0].getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={"sockets": opened}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            origin = f"http://127.0.0.1:{port}"
            env = {
                key: value for key, value in os.environ.items() if not key.startswith("PR_RERUN_")
            }
            env["XDG_CONFIG_HOME"] = str(tmp_path / "client-config")
            env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            command = [sys.executable, "-m", "pr_rerun.cli"]
            login = subprocess.run(
                [*command, "login", "--server", origin],
                env={**env, "PR_RERUN_ACCESS_TOKEN": token},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert login.returncode == 0, login.stderr
            assert remote.calls == []  # Login does not contact GitHub.
            with httpx.Client(
                base_url=origin, headers={"Authorization": f"Bearer {token}"}, trust_env=False
            ) as client:
                plan = preview(client)
            status = subprocess.run(
                [*command, "status", plan["request_id"]],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert f"Request: {plan['request_id']}" in status.stdout
            assert status.stderr == ""
            assert token not in login.stdout + login.stderr + status.stdout + status.stderr
            assert not remote.writes
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_real_http_server_preview_confirm_and_shutdown(app, remote):
    """Exercise real TCP and Uvicorn while all GitHub traffic remains simulated."""
    token = app.state.service.store.issue_token(7, "alice")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    with listeners(None, 0) as opened:
        port = opened[0].getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={"sockets": opened}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": f"Bearer {token}"},
                trust_env=False,
                timeout=5,
            ) as client:
                assert client.get("/healthz").json() == {"status": "ok"}
                plan = preview(client)
                result = confirm(client, plan)
                assert result.json()["state"] == "accepted"
                assert len(remote.writes) == 2
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()
