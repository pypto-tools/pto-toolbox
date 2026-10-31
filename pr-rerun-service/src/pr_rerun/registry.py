"""Route requests by trusted enrollment and ticket data, never client credentials."""

from contextlib import contextmanager

from pr_rerun.app_auth import AppAuth
from pr_rerun.models import Principal, ServiceError, Settings
from pr_rerun.planner import Planner
from pr_rerun.policy import PolicyStore, PolicyUpdate
from pr_rerun.service import Service
from pr_rerun.store import Store


class Registry:
    def __init__(self, settings: Settings, store: Store, auth: AppAuth):
        self.settings, self.store, self.auth = settings, store, auth
        self.policies = PolicyStore(store)

    def close(self):
        self.auth.close()

    @contextmanager
    def service(self, name: str, principal: Principal):
        record = self.policies.get(name)
        self.policies.authorize(record, principal)
        policy = record["policy"]
        settings = self.settings.model_copy(
            update={
                "repository": record["name"],
                "repository_id": record["repository_id"],
                "policy_revision": record["revision"],
                "workflows": tuple(policy.workflows),
                "allow_drafts": policy.allow_drafts,
                "base_branches": tuple(policy.base_branches),
                "confirm_interval": policy.cooldown_seconds,
                "preview_interval": min(self.settings.preview_interval, policy.cooldown_seconds),
                "daily_limit": policy.max_requests_per_user_per_day,
                "active_limit": policy.max_active_requests_per_pr,
            }
        )
        github = self.auth.github(
            record["name"], record["repository_id"], record["installation_id"]
        )

        def authorize(actor):
            current = self.policies.get(record["name"])
            self.policies.authorize(current, actor)
            if current["revision"] != record["revision"]:
                raise ServiceError(409, "Repository policy changed; request another preview.")

        def before_write():
            self.store.authenticate_hash(principal.token_hash)
            authorize(principal)

        github.before_write = before_write
        try:
            yield Service(Planner(github, settings, authorize), self.store, settings)
        finally:
            github.close()

    def preview(self, actor: Principal, pr: int, repository: str | None = None):
        if repository is None:
            raise ServiceError(400, "Choose a repository with --repo or save a default at login.")
        with self.service(repository, actor) as service:
            return service.preview(actor, pr)

    def confirm(self, actor: Principal, ticket: str, confirmation: str):
        plan = self.store.request_plan(actor, ticket=ticket)
        with self.service(plan.repository, actor) as service:
            return service.confirm(actor, ticket, confirmation)

    def status(self, actor: Principal, request_id: str):
        plan = self.store.request_plan(actor, request_id=request_id)
        with self.service(plan.repository, actor) as service:
            return service.status(actor, request_id)

    def policy(self, name: str, actor: Principal):
        record = self.policies.get(name)
        self.policies.authorize(record, actor, manage=True)
        return {
            "repository": record["name"],
            "revision": record["revision"],
            "policy": record["policy"].model_dump(mode="json"),
        }

    def update_policy(self, name: str, update: PolicyUpdate, actor: Principal):
        record = self.policies.get(name)
        self.policies.authorize(record, actor, manage=True)
        if update.policy.enabled:
            self.auth.validate_workflows(
                record["name"],
                record["repository_id"],
                record["installation_id"],
                update.policy.workflows,
            )
        self.store.authenticate_hash(actor.token_hash)
        # Reject invented numeric identities: all delegates must be provisioned locally.
        enrolled = {user["github_id"] for user in self.store.users() if user["enabled"]}
        if not set(update.policy.maintainers + update.policy.users) <= enrolled:
            raise ServiceError(400, "Policy users must have active service identities.")
        return self.policies.update(name, update, actor)
