"""Exercise a release binary against a local fake service, with no real reruns."""

import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "prr_" + "a" * 43
REQUEST = "rr_" + "1" * 32
ERROR = {
    "id": "err_" + "2" * 32,
    "workflow": "ci.yml",
    "job": "compile [a2a3]",
    "step": "Build [release]",
    "attempt": 1,
    "status": "ready",
    "category": "compile",
    "summary": "error: [literal]",
    "excerpt": "1: error: [literal]",
}


class Handler(BaseHTTPRequestHandler):
    batch_events = []

    def log_message(self, format, *args):
        pass

    def reply(self, status, value):
        content = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/api/auth/config":
            self.reply(200, {"github_login": True})
        elif self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.reply(401, {"error": "Unauthenticated"})
        elif self.path == "/api/repositories":
            self.reply(200, {"scope": "visible", "github_checked": False, "repositories": []})
        elif self.path.startswith("/api/repositories/example/project/stats?"):
            self.reply(
                200,
                {
                    "repository": "example/project",
                    "since": "2026-10-01T00:00:00Z",
                    "until": "2026-10-08T00:00:00Z",
                    "last_sync": "2026-10-08T00:00:00Z",
                    "complete": True,
                    "stale": False,
                    "jobs": [
                        {
                            "workflow": ".github/workflows/ci.yml",
                            "job": "compile [a2a3]",
                            "samples": 8,
                            "failures": 3,
                            "timeouts": 0,
                            "failure_rate": 0.375,
                            "recovered": 1,
                            "retried_failures": 2,
                            "failed_steps": {"Build [release]": 3},
                        }
                    ],
                    "total_groups": 1,
                },
            )
        elif self.path.startswith("/api/repositories/example/project/errors?"):
            self.reply(
                200,
                {
                    "repository": "example/project",
                    "since": "2026-10-01",
                    "until": "2026-10-08",
                    "coverage": {"ready": 1},
                    "metadata_complete": True,
                    "logs_complete": True,
                    "stale": False,
                    "total": 1,
                    "offset": 0,
                    "next_offset": None,
                    "items": [ERROR],
                    "groups": [
                        {
                            "category": "compile",
                            "count": 1,
                            "summary": ERROR["summary"],
                            "example_id": ERROR["id"],
                        }
                    ],
                },
            )
        elif self.path == "/api/repositories/example/project/errors/" + ERROR["id"]:
            self.reply(200, {"repository": "example/project", "error": ERROR})
        elif self.path == "/api/reruns/" + REQUEST:
            self.reply(200, {"request_id": REQUEST, "state": "accepted", "runs": []})
        else:
            self.reply(404, {"error": "Request not found."})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path == "/api/auth/device/start":
            self.reply(
                200,
                {
                    "verification_uri": "https://github.com/login/device",
                    "ticket": "b" * 43,
                    "user_code": "TEST-CODE",
                    "expires_in": 60,
                    "interval": 1,
                },
            )
        elif self.path == "/api/auth/device/poll":
            self.reply(200, {"state": "authorized", "access_token": TOKEN, "login": "test-user"})
        elif self.path == "/api/reruns/preview":
            assert self.headers.get("Authorization") == "Bearer " + TOKEN
            number = body["pr"]
            assert body["repository"] == "example/project"
            self.batch_events.append(("preview", number))
            self.reply(
                200,
                {
                    "eligible": True,
                    "repository": "example/project",
                    "pr": {
                        "number": number,
                        "title": "Batch test",
                        "author": "test-user",
                        "head_sha": "a" * 40,
                        "base_ref": "main",
                        "url": "https://github.com/pr",
                    },
                    "runs": [
                        {
                            "name": "CI",
                            "mode": "failed",
                            "attempt": 1,
                            "failed_jobs": ["test"],
                            "url": "https://github.com/run",
                        }
                    ],
                    "request_id": f"rr_{number:032x}",
                    "ticket": f"ticket-{number}",
                    "confirmation": f"example/project#{number} @test-user",
                },
            )
        elif self.path == "/api/reruns/confirm":
            assert self.headers.get("Authorization") == "Bearer " + TOKEN
            number = int(body["ticket"].removeprefix("ticket-"))
            assert body["confirmation"] == f"example/project#{number} @test-user"
            self.batch_events.append(("confirm", number))
            self.reply(202, {"request_id": f"rr_{number:032x}", "state": "accepted", "runs": []})
        else:
            self.reply(400, {"error": "Unexpected write; no reruns are supported by this test"})


def check_batch(binary, environment):
    master, slave = pty.openpty()
    process = None
    try:
        process = subprocess.Popen(
            [binary, "10", "11", "10", "12", "--repo", "example/project"],
            env=environment,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.write(master, b"y\ny\ny\n")
        output, _ = process.communicate(timeout=30)
        assert process.returncode == 0, output
        assert Handler.batch_events == [
            ("preview", 10),
            ("confirm", 10),
            ("preview", 11),
            ("confirm", 11),
            ("preview", 12),
            ("confirm", 12),
        ]
        assert "Batch results" in output and "PR 3/3: #12" in output
        assert all(f"rr_{number:032x}" in output for number in (10, 11, 12))
        print("Client batch preview, per-PR confirmation, deduplication and summary passed")
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
        os.close(slave)


def check_terminal_confirmation(binary, environment):
    # Exercise the actual input() TTY path, with stdout/stderr on the terminal too.
    # Wait for each prompt before sending keys, as an interactive user would.
    for answers, confirmed in [
        ([b"y\n"], True),
        ([b"maybe\n", b"y\n"], True),
        ([b"maybe\n", b"n\n"], False),
        ([b"\n"], False),
    ]:
        Handler.batch_events = []
        master, slave = pty.openpty()
        process = None
        output = b""
        sent = 0
        try:
            process = subprocess.Popen(
                [binary, "10", "--repo", "example/project"],
                env={**environment, "TERM": "xterm-256color"},
                stdin=slave,
                stdout=slave,
                stderr=slave,
            )
            os.close(slave)
            slave = -1
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break  # Linux PTYs return EIO after the slave closes.
                if not chunk:
                    break
                output += chunk
                if sent < len(answers) and output.count(b"Confirm? [y/N] ") > sent:
                    assert Handler.batch_events == [("preview", 10)], output
                    os.write(master, answers[sent])
                    sent += 1
            assert sent == len(answers), output
            assert process.wait(timeout=5) == (0 if confirmed else 1), output
            assert Handler.batch_events == (
                [("preview", 10), ("confirm", 10)] if confirmed else [("preview", 10)]
            ), output
            assert (b"Cancelled. No rerun was requested." in output) == (not confirmed), output
            if len(answers) > 1:
                assert output.count(b"Input not recognized.") == 1, output
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)
            if slave != -1:
                os.close(slave)
    print("Interactive TTY confirmation, invalid input correction and cancellation passed")


def main():
    binary = str(Path(sys.argv[1]).absolute())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                key: value for key, value in os.environ.items() if not key.startswith("PR_RERUN_")
            }
            environment["XDG_CONFIG_HOME"] = temporary
            commands = [
                [
                    "login",
                    "--server",
                    f"http://127.0.0.1:{server.server_port}",
                    "--repo",
                    "example/project",
                ],
                ["status", REQUEST],
                ["repos"],
                ["repos", "--json"],
                ["stats"],
                ["stats", "--json"],
                ["errors"],
                ["errors", "--group"],
                ["errors", "--json"],
                ["error", ERROR["id"]],
                ["error", ERROR["id"], "--json"],
                ["alias", "set", "demo", "example/project"],
                ["alias", "list"],
                ["mcp", "--repo", "demo"],
                ["logout"],
            ]
            for args in commands:
                if args == ["logout"]:
                    check_batch(binary, environment)
                    check_terminal_confirmation(binary, environment)
                completed = subprocess.run(
                    [binary, *args],
                    env=environment,
                    check=True,
                    timeout=30,
                    capture_output=True,
                    text=True,
                    input=(
                        "\n".join(
                            json.dumps(frame)
                            for frame in [
                                {
                                    "jsonrpc": "2.0",
                                    "id": 1,
                                    "method": "initialize",
                                    "params": {
                                        "protocolVersion": "2025-11-25",
                                        "capabilities": {},
                                        "clientInfo": {"name": "release-test", "version": "1"},
                                    },
                                },
                                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                *[
                                    {
                                        "jsonrpc": "2.0",
                                        "id": index + 3,
                                        "method": "tools/call",
                                        "params": {
                                            "name": name,
                                            "arguments": arguments,
                                        },
                                    }
                                    for index, (name, arguments) in enumerate(
                                        [
                                            ("list_repositories", {}),
                                            ("get_ci_stats", {}),
                                            ("list_ci_errors", {}),
                                            ("get_ci_error", {"error_id": ERROR["id"]}),
                                        ]
                                    )
                                ],
                            ]
                        )
                        + "\n"
                        if args[0] == "mcp"
                        else None
                    ),
                )
                print(completed.stdout, end="")
                if args == ["stats"]:
                    assert "[a2a3]" in completed.stdout and "37.5%" in completed.stdout
                    assert "Build [release]" in completed.stdout and "\x1b" not in completed.stdout
                elif args[0] in {"errors", "error"}:
                    assert "[literal]" in completed.stdout
                    if "--json" in args:
                        assert json.loads(completed.stdout)["repository"] == "example/project"
                elif args == ["stats", "--json"]:
                    assert json.loads(completed.stdout)["jobs"][0]["failure_rate"] == 0.375
                elif args[0] == "mcp":
                    replies = [json.loads(line) for line in completed.stdout.splitlines()]
                    assert len(replies) == 6
                    assert len(replies[1]["result"]["tools"]) == 4
                    assert all(not reply["result"]["isError"] for reply in replies[2:])
                    assert replies[-1]["result"]["structuredContent"]["error"]["id"] == ERROR["id"]
                    assert TOKEN not in completed.stdout and not completed.stderr
            assert not (Path(temporary) / "pr-rerun/client.json").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print("Frozen client login, saved session, status, aliases and logout passed")


if __name__ == "__main__":
    main()
