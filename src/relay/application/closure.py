"""Closing a feature to new work, and the one lock that makes a closure certain.

A pause or a cancel has to hold against everything that might start or release work,
some of which has not been created yet when it is asked for. So every workflow
transaction first takes its project's gate lock, shared, and a closure takes it
exclusively: either the closure commits first and is seen, or it waits for the
transaction that got there before it. Nothing holds the lock across an oracle run, a Git
export or a push.
"""

from __future__ import annotations

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
    """What stops work on a story, if anything: its feature is paused or cancelled, or a
    question is open over it. Read under the gate lock, so it cannot change before the
    reading transaction commits. `CANCELLED` is returned exactly; anything else names
    every reason that applies."""
    row = await (
        await connection.execute(
            "SELECT standing(s.project_id, s.feature_id, s.story_id) AS standing "
            "FROM stories s WHERE s.project_id = %s AND s.story_id = %s",
            (project_id, story_id),
        )
    ).fetchone()
    return row["standing"] if row else None


async def closure_of_behaviour(
    connection: Connection, project_id: str, behaviour_id: str
) -> str | None:
    row = await (
        await connection.execute(
            "SELECT standing(s.project_id, s.feature_id, s.story_id) AS standing "
            "FROM behaviours b JOIN stories s USING (project_id, story_id) "
            "WHERE b.project_id = %s AND b.behaviour_id = %s",
            (project_id, behaviour_id),
        )
    ).fetchone()
    return row["standing"] if row else None


async def closure_of_feature(
    connection: Connection, project_id: str, feature_id: str, story_id: str | None = None
) -> str | None:
    """For work that belongs to the feature as a whole, or to a story named apart."""
    row = await (
        await connection.execute(
            "SELECT standing(%s, %s, %s) AS standing", (project_id, feature_id, story_id)
        )
    ).fetchone()
    assert row is not None
    standing: str | None = row["standing"]
    return standing


def require_open(closed_by: str | None) -> None:
    """Refuse to start anything on a feature that has been closed, by whatever route the
    request came."""
    if closed_by is not None:
        raise ActionRefused(f"the feature is {closed_by}: nothing new starts here")


async def feature_basis(connection: Connection, project_id: str, feature_id: str) -> int:
    """The number of the latest feature-wide decision of the Product Owner's; zero if none."""
    row = await (
        await connection.execute("SELECT feature_basis(%s, %s) AS basis", (project_id, feature_id))
    ).fetchone()
    assert row is not None
    return int(row["basis"])


async def story_basis(connection: Connection, project_id: str, story_id: str) -> int:
    """The number of the latest decision of the Product Owner's in force over a story,
    feature-wide or its own; zero if none."""
    row = await (
        await connection.execute("SELECT story_basis(%s, %s) AS basis", (project_id, story_id))
    ).fetchone()
    assert row is not None
    return int(row["basis"])


def require_basis(read: int, now: int) -> None:
    """What a role submits says which decisions it was written under. Relay never
    stamps a submission with whatever is in force when it happens to arrive."""
    if read != now:
        raise ActionRefused(
            f"this was written on decision basis {read}; the basis is now {now}: "
            "read the context again"
        )


async def require_current(
    connection: Connection, project_id: str, story_id: str, behaviour_id: str | None = None
) -> None:
    """Refuse to start work that relies on something made under decisions that have
    since changed: the story, and with a behaviour named, that behaviour's contract and
    the evidence gathered for it."""
    row = await (
        await connection.execute(
            "SELECT not_current(%s, %s, %s) AS why", (project_id, story_id, behaviour_id)
        )
    ).fetchone()
    assert row is not None
    if row["why"] is not None:
        raise ActionRefused(f"{row['why']}: its owner reconfirms it before work goes on")


async def decisions_in_force(
    connection: Connection, project_id: str, feature_id: str, story_id: str | None = None
) -> list[dict[str, Any]]:
    """The decisions that stand over a feature, or over one story of it: those not
    replaced, the Product Owner's and a model's told apart, in the order they were made."""
    return await (
        await connection.execute(
            "SELECT d.decision_id, d.number, d.source, d.text, d.scope_story, d.replaces "
            "FROM decisions d WHERE d.project_id = %s AND d.feature_id = %s "
            "AND (d.scope_story = '' OR d.scope_story = %s) AND NOT EXISTS "
            "(SELECT 1 FROM decisions n WHERE n.replaces = d.decision_id) ORDER BY d.number",
            (project_id, feature_id, story_id),
        )
    ).fetchall()


def as_shown(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decisions as a gate's manifest records them: what was in force when it opened."""
    return [
        {"number": each["number"], "source": each["source"], "text": each["text"]}
        for each in decisions
    ]
