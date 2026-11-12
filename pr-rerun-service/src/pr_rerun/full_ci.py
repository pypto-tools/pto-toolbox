"""Routing contract for PyPTO's trusted main-branch Full CI controller.

Titles locate candidates only. The planner also verifies repository, workflow,
event, trusted branch, current PR head/base and merge parents before any write.
"""

import re
from datetime import datetime

from pr_rerun.models import UpstreamError, positive_id

FULL_PATHS = {".github/workflows/ci.yml", ".github/workflows/ci-full.yml"}
FULL_TITLE = re.compile(r"CI full PR #([1-9][0-9]*) @([0-9a-f]{40}) base:([0-9a-f]{40})")


def full_target(run: dict) -> tuple[int, str, str] | None:
    title = run.get("display_title")
    match = FULL_TITLE.fullmatch(title) if isinstance(title, str) else None
    return (int(match[1]), match[2], match[3]) if match else None


def run_order(run: dict) -> tuple[datetime, int]:
    """Run numbers are local to a workflow; reruns must not change ordering."""
    try:
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("Missing timezone")
        return created, positive_id(run["id"])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise UpstreamError("Invalid Full CI run identity.") from error
