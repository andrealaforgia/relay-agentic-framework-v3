"""When a feature is ready to be completed, and what admitting it to its final checks
records. The checks themselves are run by `final_checks`; this module is the part every
transition calls, so it depends on nothing that depends on them.

The prerequisites are defined once, here. A feature that meets them while it is being
delivered is admitted: it is `checking`, a completion request exists for it, and one
completion is owed to the control plane.
"""

from __future__ import annotations

import uuid
from typing import Any

from relay.application.assignments import COMPLETE, REVISE, owe
from relay.application.closure import feature_basis
from relay.domain.story import Member, StoryState, is_resolved
from relay.persistence.database import Connection
from relay.persistence.ledger import append_event
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"


async def evaluate_completion(connection: Connection, project_id: str, feature_id: str) -> None:
    """Called, under the gate lock, in every transaction that can change whether a
    feature is ready to be completed."""
    feature = await (
        await connection.execute(
            "SELECT state, closed_by, standing(project_id, feature_id, NULL) AS standing "
            "FROM features WHERE project_id = %s AND feature_id = %s FOR UPDATE",
            (project_id, feature_id),
        )
    ).fetchone()
    assert feature is not None
    ready = await prerequisites_hold(connection, project_id, feature_id)
    if feature["state"] == "delivering" and ready and feature["standing"] is None:
        await _admit(connection, project_id, feature_id)


async def prerequisites_hold(connection: Connection, project_id: str, feature_id: str) -> bool:
    """A roadmap in force that is current, with no proposal waiting and no change to it
    owed; and every story of it resolved, with none still to be released or being
    worked on."""
    roadmap = await roadmap_in_force(connection, project_id, feature_id)
    if roadmap is None or not roadmap["current"] or roadmap["proposal_waiting"]:
        return False
    if await _roadmap_change_owed(connection, project_id, feature_id):
        return False
    members = [
        Member(story["story_id"], StoryState(story["state"]), story["corrects"])
        for story in await stories_of(connection, project_id, feature_id, roadmap["revision"])
    ]
    return bool(members) and all(is_resolved(member, members) for member in members)


async def roadmap_in_force(
    connection: Connection, project_id: str, feature_id: str
) -> dict[str, Any] | None:
    return await (
        await connection.execute(
            "SELECT r.revision, r.current_on = feature_basis(r.project_id, r.feature_id) AS current, "
            "EXISTS (SELECT 1 FROM roadmaps p WHERE p.project_id = r.project_id "
            "AND p.feature_id = r.feature_id AND p.state = 'proposed') AS proposal_waiting "
            "FROM roadmaps r WHERE r.project_id = %s AND r.feature_id = %s AND r.state = 'approved'",
            (project_id, feature_id),
        )
    ).fetchone()


async def stories_of(
    connection: Connection, project_id: str, feature_id: str, revision: int
) -> list[dict[str, Any]]:
    """The stories of one roadmap revision as they are now, in its order."""
    return await (
        await connection.execute(
            "SELECT s.story_id, s.title, s.state, s.revision, s.corrects "
            "FROM roadmap_entries e JOIN stories s USING (project_id, story_id) "
            "WHERE e.project_id = %s AND e.feature_id = %s AND e.revision = %s "
            "AND e.kind <> 'dropped' ORDER BY e.position",
            (project_id, feature_id, revision),
        )
    ).fetchall()


async def owe_completion(
    connection: Connection, project_id: str, feature_id: str, request_id: uuid.UUID
) -> uuid.UUID:
    """One completion owed for a request. No worker can claim it: the control plane
    takes it up, and its grant and its first effect are made together."""
    return await owe(
        connection,
        COMPLETE,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type="completion.requested",
            project_id=project_id,
            sender=CONTROL_PLANE,
            recipient="integrator",
            idempotency_key=f"complete:{uuid.uuid7()}",
            correlation_id=uuid.uuid7(),
            payload={"request_id": str(request_id), "feature_id": feature_id},
        ),
        issued_on={"request_id": str(request_id)},
        feature_id=feature_id,
    )


async def basis_of_gate(connection: Connection, gate: dict[str, Any]) -> dict[str, Any] | None:
    """What a gate on a failed final check would be opened on now: the same batch and
    the same evidence, under the roadmap and the decisions in force now. Trunk is not
    part of it: after trunk has moved, a retry still means fresh checks on the head."""
    feature = await (
        await connection.execute(
            "SELECT state FROM features WHERE project_id = %s AND feature_id = %s",
            (gate["project_id"], gate["feature_id"]),
        )
    ).fetchone()
    roadmap = await roadmap_in_force(connection, gate["project_id"], gate["feature_id"])
    if feature is None or feature["state"] != "needs_decision" or roadmap is None:
        return None
    return {
        "batch_id": gate["basis"]["batch_id"],
        "evidence": gate["basis"]["evidence"],
        "roadmap_revision": roadmap["revision"],
        "decisions": await feature_basis(connection, gate["project_id"], gate["feature_id"]),
    }


async def settle_gate(connection: Connection, gate: dict[str, Any], outcome: str) -> None:
    """The Product Owner's answer to a final check that failed."""
    project_id, feature_id = gate["project_id"], gate["feature_id"]
    if outcome == "retried":
        await _admit(connection, project_id, feature_id)
        return
    # The roadmap is to change. Until a later revision is approved the feature is not
    # ready, however often that is looked at: a proposal sent back leaves this owed.
    await connection.execute(
        "UPDATE features SET state = 'delivering' WHERE project_id = %s AND feature_id = %s",
        (project_id, feature_id),
    )
    await owe(
        connection,
        REVISE,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type="roadmap.revision_requested",
            project_id=project_id,
            sender=CONTROL_PLANE,
            recipient="analyst",
            idempotency_key=f"revise:{gate['gate_id']}",
            correlation_id=uuid.uuid7(),
            payload={"feature_id": feature_id, "batch_id": gate["basis"]["batch_id"]},
        ),
        issued_on={
            "batch_id": gate["basis"]["batch_id"],
            "roadmap_revision": gate["basis"]["roadmap_revision"],
        },
        feature_id=feature_id,
    )


async def roadmap_changed(connection: Connection, project_id: str, feature_id: str) -> None:
    """A roadmap revision was approved: a change that was owed has been made."""
    await connection.execute(
        "UPDATE assignments SET state = 'done' WHERE project_id = %s AND feature_id = %s "
        "AND kind = 'revise' AND state = 'wanted'",
        (project_id, feature_id),
    )


async def _roadmap_change_owed(connection: Connection, project_id: str, feature_id: str) -> bool:
    return (
        await (
            await connection.execute(
                "SELECT 1 FROM assignments WHERE project_id = %s AND feature_id = %s "
                "AND kind = 'revise' AND state IN ('wanted', 'granted') LIMIT 1",
                (project_id, feature_id),
            )
        ).fetchone()
    ) is not None


async def _admit(connection: Connection, project_id: str, feature_id: str) -> None:
    request_id = uuid.uuid7()
    await connection.execute(
        "UPDATE features SET state = 'checking' WHERE project_id = %s AND feature_id = %s",
        (project_id, feature_id),
    )
    await connection.execute(
        "INSERT INTO completion_requests (request_id, project_id, feature_id) VALUES (%s, %s, %s)",
        (request_id, project_id, feature_id),
    )
    await owe_completion(connection, project_id, feature_id, request_id)
    await append_event(
        connection,
        project_id,
        "feature.checking",
        subject_id=feature_id,
        payload={"request_id": str(request_id)},
    )
