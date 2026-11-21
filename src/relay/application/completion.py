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
            "SELECT state FROM features WHERE project_id = %s AND feature_id = %s FOR UPDATE",
            (project_id, feature_id),
        )
    ).fetchone()
    assert feature is not None
    state = feature["state"]
    if state == "delivering":
        # A feature that is paused or waiting on a question does not become ready: its
        # gates wait and its grants are taken back. So this is not asked here; whether
        # the checks may start is asked when they are taken up.
        if await prerequisites_hold(connection, project_id, feature_id):
            await _admit(connection, project_id, feature_id)
    elif state == "checking":
        if not await prerequisites_hold(connection, project_id, feature_id):
            # Released: in practice a decision has made the roadmap stale. The Analyst
            # can reconfirm it or replace it, which they could not while it was checked.
            await _back_to_delivery(connection, project_id, feature_id)
            await end_request(
                connection, project_id, feature_id, "overtaken", "the feature is no longer ready"
            )
    elif state == "needs_decision" and not await _gate_is_open(connection, project_id, feature_id):
        # A decision superseded the gate on the failed check. Reconfirming or replacing
        # the roadmap makes the feature ready again, and the checks are run afresh.
        await _back_to_delivery(connection, project_id, feature_id)


async def stopped(connection: Connection, project_id: str, feature_id: str) -> str | None:
    """What keeps a feature's final checks from starting or deciding for now: it is
    paused, or a question is open over it. Cancelled is returned exactly. A question
    over one of its stories cannot be open: every story of it is finished with."""
    row = await (
        await connection.execute(
            "SELECT standing(%s, %s, NULL) AS standing", (project_id, feature_id)
        )
    ).fetchone()
    assert row is not None
    return str(row["standing"]) if row["standing"] is not None else None


async def end_request(
    connection: Connection, project_id: str, feature_id: str, state: str, why: str
) -> None:
    """End a feature's live completion request with whatever it still has: a generation
    that is running ends with it, and a completion still wanted is retired. Receipts are
    kept. Nothing is owed after it."""
    request = await (
        await connection.execute(
            "UPDATE completion_requests SET state = %s WHERE project_id = %s AND feature_id = %s "
            "AND state = 'live' RETURNING request_id",
            (state, project_id, feature_id),
        )
    ).fetchone()
    if request is None:
        return
    await connection.execute(
        "UPDATE completion_batches SET state = %s, reason = %s WHERE request_id = %s "
        "AND state = 'running'",
        (state, why, request["request_id"]),
    )
    await connection.execute(
        "UPDATE assignments SET state = 'retired' WHERE project_id = %s AND feature_id = %s "
        "AND kind = 'complete' AND state = 'wanted'",
        (project_id, feature_id),
    )
    await append_event(
        connection,
        project_id,
        f"completion.request_{state}",
        subject_id=feature_id,
        payload={"request_id": str(request["request_id"]), "reason": why},
    )


async def _back_to_delivery(connection: Connection, project_id: str, feature_id: str) -> None:
    await connection.execute(
        "UPDATE features SET state = 'delivering' WHERE project_id = %s AND feature_id = %s",
        (project_id, feature_id),
    )


async def _gate_is_open(connection: Connection, project_id: str, feature_id: str) -> bool:
    return (
        await (
            await connection.execute(
                "SELECT 1 FROM gates WHERE project_id = %s AND feature_id = %s "
                "AND kind = 'completion' AND state = 'open'",
                (project_id, feature_id),
            )
        ).fetchone()
    ) is not None


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


async def settle_gate(connection: Connection, gate: dict[str, Any], outcome: str) -> None:
    """The Product Owner's answer to a final check that failed."""
    project_id, feature_id = gate["project_id"], gate["feature_id"]
    if outcome == "retried":
        await _admit(connection, project_id, feature_id)
        return
    # The roadmap is to change. Until a later revision is approved the feature is not
    # ready, however often that is looked at: a proposal sent back leaves this owed.
    # With its gate decided, the evaluation that follows every decision puts the feature
    # back among those being delivered.
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
