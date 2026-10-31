"""Server-owned GitHub device authorization; no GitHub credential reaches the CLI."""

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

import httpx

from pr_rerun.github import GitHub
from pr_rerun.models import ServiceError, positive_id
from pr_rerun.store import Store, digest


@dataclass
class PendingLogin:
    device_code: str = field(repr=False)
    expires: float
    interval: int
    next_poll: float


class DeviceLogin:
    def __init__(self, client_id: str, store: Store, transport=None, clock=time.monotonic):
        self.client_id, self.store, self.clock = client_id, store, clock
        self.client = httpx.Client(
            timeout=30, trust_env=False, follow_redirects=False, transport=transport
        )
        self.pending: dict[str, PendingLogin] = {}
        self.lock = threading.Lock()

    def close(self):
        self.pending.clear()
        self.client.close()

    def call(self, path: str, payload: dict | None = None, token: str | None = None):
        url = "https://api.github.com/user" if token else "https://github.com" + path
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with self.client.stream(
                "GET" if token else "POST", url, headers=headers, data=payload
            ) as response:
                if response.status_code != 200:
                    raise ServiceError(502, "GitHub login is unavailable; try again later.")
                value = json.loads(GitHub._read_bounded(response))
                if not isinstance(value, dict):
                    raise ValueError
                return value
        except (httpx.HTTPError, ValueError):
            raise ServiceError(502, "GitHub login is unavailable; try again later.") from None

    def start(self, address: str):
        self.store.rate_limit("login-start:" + digest(address), 5)
        with self.lock:
            now = self.clock()
            self.pending = {
                key: value for key, value in self.pending.items() if value.expires > now
            }
            if len(self.pending) >= 100:
                raise ServiceError(429, "Too many pending logins; try again later.")
            value = self.call(
                "/login/device/code", {"client_id": self.client_id, "scope": "offline_access"}
            )
            if value.get("error"):
                raise ServiceError(
                    503, "Enable Device Flow in the GitHub App settings before logging in."
                )
            code, device = value.get("user_code"), value.get("device_code")
            expires, interval = value.get("expires_in"), value.get("interval")
            if (
                not isinstance(code, str)
                or not re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", code)
                or not isinstance(device, str)
                or not 1 <= len(device) <= 256
                or value.get("verification_uri") != "https://github.com/login/device"
                or type(expires) is not int
                or not 1 <= expires <= 1800
                or type(interval) is not int
                or not 1 <= interval <= 60
            ):
                raise ServiceError(502, "Invalid GitHub device authorization response.")
            ticket = secrets.token_urlsafe(32)
            self.pending[digest(ticket)] = PendingLogin(
                device, now + expires, interval, now + interval
            )
            return {
                "ticket": ticket,
                "user_code": code,
                "verification_uri": value["verification_uri"],
                "expires_in": expires,
                "interval": interval,
            }

    def poll(self, ticket: str):
        with self.lock:
            key = digest(ticket)
            pending = self.pending.get(key)
            if pending is None or pending.expires <= self.clock():
                self.pending.pop(key, None)
                raise ServiceError(410, "Login expired or already completed; start login again.")
            if self.clock() < pending.next_poll:
                return {"state": "pending", "interval": pending.interval}
            pending.next_poll = self.clock() + pending.interval
            value = self.call(
                "/login/oauth/access_token",
                {
                    "client_id": self.client_id,
                    "device_code": pending.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            error = value.get("error")
            if error in {"authorization_pending", "slow_down"}:
                if error == "slow_down":
                    pending.interval += 5
                    pending.next_poll = self.clock() + pending.interval
                return {"state": "pending", "interval": pending.interval}
            # Never accept client-supplied GitHub tokens, usernames or IDs.
            self.pending.pop(key, None)
            if error:
                raise ServiceError(403, "GitHub login was denied or expired; start login again.")
            token = value.get("access_token")
            if (
                not isinstance(token, str)
                or not token
                or value.get("token_type", "").lower() != "bearer"
            ):
                raise ServiceError(502, "Invalid GitHub login response.")
            user = self.call("/user", token=token)
            login = user.get("login")
            if (
                user.get("type") != "User"
                or not isinstance(login, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login)
            ):
                raise ServiceError(403, "Only verified human GitHub accounts may log in.")
            identity = positive_id(user.get("id"))
            session = self.store.login_session(identity, login)
            return {
                "state": "authorized",
                "login": login,
                "github_id": identity,
                "access_token": session,
            }
