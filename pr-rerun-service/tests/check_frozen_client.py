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
                ["alias", "set", "demo", "example/project"],
                ["alias", "list"],
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
                )
                print(completed.stdout, end="")
                if args == ["stats"]:
                    assert "[a2a3]" in completed.stdout and "37.5%" in completed.stdout
                    assert "Build [release]" in completed.stdout and "\x1b" not in completed.stdout
                elif args == ["stats", "--json"]:
                    assert json.loads(completed.stdout)["jobs"][0]["failure_rate"] == 0.375
            assert not (Path(temporary) / "pr-rerun/client.json").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print("Frozen client login, saved session, status, aliases and logout passed")


if __name__ == "__main__":
    main()
