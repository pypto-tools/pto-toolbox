"""Exercise a release binary against a local fake service, with no real reruns."""

import json
import os
import subprocess
import sys
import tempfile
import threading
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
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
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
        else:
            self.reply(400, {"error": "Unexpected write; no reruns are supported by this test"})


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
