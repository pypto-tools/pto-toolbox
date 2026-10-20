"""Coordinate preview, confirmation, and a bounded sequence of rerun requests."""

import json
import logging

from pr_rerun.models import Principal, ServiceError, Settings, UpstreamError
from pr_rerun.planner import Planner
from pr_rerun.store import Store

LOG = logging.getLogger("pr_rerun.audit")


class Service:
    def __init__(self, planner: Planner, store: Store, settings: Settings) -> None:
        self.planner, self.store, self.settings = planner, store, settings

    def preview(self, principal: Principal, number: int) -> dict:
        self.store.rate_limit(f"preview:{principal.github_id}", self.settings.preview_interval)
        plan = self.planner.build(principal, number)
        if not plan.runs:
            # Excluded-run explanations are needed when GitHub cannot prove fork
            # ownership, rather than silently treating the PR as successful.
            return {"eligible": False, **plan.model_dump(mode="json")}
        self.store.authenticate_hash(principal.token_hash)
        return {"eligible": True, **self.store.preview(principal, plan, self.settings.preview_ttl)}

    def confirm(self, principal: Principal, ticket: str, confirmation: str) -> dict:
        request_id, plan, fresh = self.store.claim(principal, ticket, confirmation)
        if not fresh:
            return self.store.result(principal, request_id)
        try:
            self.store.rate_limit(
                f"confirm-user:{principal.github_id}", self.settings.confirm_interval
            )
            self.store.rate_limit(
                f"confirm-pr:{plan.pr.repository_id}:{plan.pr.number}",
                self.settings.confirm_interval,
            )
            if self.planner.build(principal, plan.pr.number).fingerprint != plan.fingerprint:
                raise ServiceError(409, "PR or workflow state changed; request another preview.")
            self.store.reserve(request_id, plan)
        except ServiceError as error:
            self.store.finish(request_id, "rejected", str(error))
            return self.store.result(principal, request_id)
        except (KeyError, TypeError, ValueError, AttributeError):
            self.store.finish(request_id, "rejected", "Unable to verify GitHub's response.")
            return self.store.result(principal, request_id)

        states = []
        stopped = False
        for run in plan.runs:
            state, message = "not_attempted", "An earlier run could not be submitted."
            if not stopped:
                try:
                    self.store.authenticate_hash(principal.token_hash)
                    self.planner.validate_run(principal, plan, run)
                except (ServiceError, KeyError, TypeError, ValueError, AttributeError):
                    state, message = "rejected", "Authorization or workflow state changed."
                    stopped = True
                if not stopped:
                    # This commit must precede the POST. On process death we
                    # cannot distinguish a lost response from a lost request.
                    self.store.item(request_id, run.run_id, "submitting")
                    self._audit(principal, request_id, run.run_id, run.mode, "submitting")
                    try:
                        self.planner.github.rerun(run.run_id, run.mode)
                        state, message = "accepted", "GitHub accepted the rerun request."
                    except UpstreamError as error:
                        state = "unknown" if error.uncertain else "rejected"
                        message, stopped = str(error), True
                    except ServiceError as error:
                        state, message, stopped = "rejected", str(error), True
            self.store.item(
                request_id,
                run.run_id,
                state,
                message,
                release=state in {"rejected", "not_attempted"},
            )
            self._audit(principal, request_id, run.run_id, run.mode, state)
            states.append(state)
        state = "accepted" if all(item == "accepted" for item in states) else "partial"
        if "unknown" in states:
            state = "unknown"
        elif "accepted" not in states:
            state = "rejected"
        self.store.finish(request_id, state)
        return self.store.result(principal, request_id)

    @staticmethod
    def _audit(principal: Principal, request_id: str, run_id: int, mode: str, outcome: str) -> None:
        LOG.info(
            json.dumps(
                {
                    "actor_id": principal.github_id,
                    "actor": principal.login,
                    "request_id": request_id,
                    "run_id": run_id,
                    "mode": mode,
                    "outcome": outcome,
                }
            )
        )

    def status(self, principal: Principal, request_id: str) -> dict:
        result = self.store.result(principal, request_id)  # Check ownership before any lookup.
        self.store.rate_limit(f"status:{principal.github_id}", self.settings.preview_interval)
        pending = self.store.pending_items(request_id)
        for item in pending:
            if item["state"] == "submitting":
                continue  # Another thread may still be waiting for its POST response.
            try:
                run = self.planner.github.run(item["run_id"])
                if run["run_attempt"] > item["attempt"]:
                    self.store.item(
                        request_id,
                        item["run_id"],
                        "rerun_observed",
                        "GitHub shows a newer attempt; who initiated it cannot be determined.",
                    )
            except (ServiceError, KeyError, TypeError, ValueError):
                continue  # Keep uncertainty; never release a claim based on unavailable evidence.
        result = self.store.result(principal, request_id)
        if pending and all(
            run["state"] in {"accepted", "rerun_observed"} for run in result["runs"]
        ):
            self.store.finish(request_id, "rerun_observed")
            result = self.store.result(principal, request_id)
        return result
