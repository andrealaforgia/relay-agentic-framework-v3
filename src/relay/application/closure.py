"""Closing a feature to new work, and the one lock that makes a closure certain.

A pause or a cancel has to hold against everything that might start or release work,
some of which has not been created yet when it is asked for. So every workflow
transaction first takes its project's gate lock, shared, and a closure takes it
exclusively: either the closure commits first and is seen, or it waits for the
transaction that got there before it. Nothing holds the lock across an oracle run, a Git
export or a push.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from relay.domain.story import ActionRefused
from relay.persistence.database import Connection

PAUSED = "paused"
CANCELLED = "cancelled"


async def lock_gate(connection: Connection, project_id: str, *, exclusive: bool = False) -> None:
    """Taken before any other row of the project, in every workflow transaction."""
    strength = "UPDATE" if exclusive else "SHARE"
    await connection.execute(
        f"SELECT 1 FROM project_gates WHERE project_id = %s FOR {strength}",
        (project_id,),
    )


async def closure_of_story(connection: Connection, project_id: str, story_id: str) -> str | None:
    """How the feature a story belongs to is closed, if it is. Read under the gate lock,
    so it cannot change before the reading transaction commits."""
    row = await (
        await connection.execute(
            "SELECT f.closed_by FROM stories s JOIN features f USING (project_id, feature_id) "
            "WHERE s.project_id = %s AND s.story_id = %s",
            (project_id, story_id),
        )
    ).fetchone()
    return row["closed_by"] if row else None


async def closure_of_behaviour(
    connection: Connection, project_id: str, behaviour_id: str
) -> str | None:
    row = await (
        await connection.execute(
            "SELECT f.closed_by FROM behaviours b JOIN stories s USING (project_id, story_id) "
            "JOIN features f USING (project_id, feature_id) "
            "WHERE b.project_id = %s AND b.behaviour_id = %s",
            (project_id, behaviour_id),
        )
    ).fetchone()
    return row["closed_by"] if row else None


def require_open(closed_by: str | None) -> None:
    """Refuse to start anything on a feature that has been closed, by whatever route the
    request came."""
    if closed_by is not None:
        raise ActionRefused(f"the feature is {closed_by}: nothing new starts on it")


def is_open(feature: Mapping[str, Any]) -> bool:
    return feature["closed_by"] is None
