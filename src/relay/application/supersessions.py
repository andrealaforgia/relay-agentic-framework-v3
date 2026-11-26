"""Obligations, and replacing one of them.

Whatever is on trunk is an obligation: every later candidate must still satisfy it. A
correction that contradicts one can integrate only under an authority the Product Owner
gives for exactly that obligation and exactly that replacing behaviour. The authority
excuses candidates of that behaviour alone. An integration records which authority it
was authorised with, so nothing that happens afterwards changes what that push means;
the old obligation is retired when, and only when, that integration reaches trunk.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.assignments import retire_all_of_behaviour
from relay.application.closure import (
    CANCELLED,
    closure_of_behaviour,
    lock_gate,
    require_basis,
    require_startable,
    story_basis,
)
from relay.application.stories import _close_gate, _open_gate
from relay.domain.evidence import CheckContract, ProcessContract
from relay.domain.story import ActionRefused, Principal, PrincipalKind
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event

_LIVE = ("proposed", "standing", "reserved")


@dataclass(frozen=True)
class Obligation:
    obligation_id: uuid.UUID
    source: str
    name: str
    behaviour_id: str | None
    digest: str
    state: str


class Supersessions:
    def __init__(self, pool: Pool, then: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._pool = pool
        # What the control plane does by itself once a proposal has ended: its pass.
        self.then = then

    async def obligations(self, project_id: str) -> list[Obligation]:
        """Everything a candidate of this project has had to satisfy, in force or retired."""
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT obligation_id, source, name, behaviour_id, digest, state "
                    "FROM obligations WHERE project_id = %s ORDER BY created_at",
                    (project_id,),
                )
            ).fetchall()
        return [Obligation(**row) for row in rows]

    async def propose(
        self,
        principal: Principal,
        project_id: str,
        *,
        obligation: uuid.UUID,
        digest: str,
        behaviour: str,
        contract_digest: str,
        story_revision: int,
        basis: int,
        reason: str,
    ) -> uuid.UUID:
        """The Verifier proposes that one behaviour replace one obligation. It names the
        obligation by identity and by the digest it was read at, and the replacement by
        the contract, story revision and decisions it was read on: a proposal that
        arrives after any of them changed is not about what is there now. This opens a
        gate that shows the Product Owner both contracts in full."""
        if principal.kind is not PrincipalKind.WORKER or principal.role != "verifier":
            raise ActionRefused("a supersession is for the verifier to propose")
        if not reason.strip():
            raise ActionRefused("a supersession needs a stated reason")
        async with self._pool.connection() as connection, connection.transaction():
            return await propose_in(
                connection,
                principal,
                project_id,
                obligation=obligation,
                digest=digest,
                behaviour=behaviour,
                contract_digest=contract_digest,
                story_revision=story_revision,
                basis=basis,
                reason=reason,
            )

    async def withdraw(self, principal: Principal, project_id: str, gate_id: uuid.UUID) -> None:
        """The Verifier takes back a proposal nobody has decided."""
        if principal.kind is not PrincipalKind.WORKER or principal.role != "verifier":
            raise ActionRefused("a supersession is for the verifier to withdraw")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id, exclusive=True)
            proposal = await (
                await connection.execute(
                    "SELECT * FROM supersessions WHERE gate_id = %s AND project_id = %s "
                    "AND state = 'proposed' FOR UPDATE",
                    (gate_id, project_id),
                )
            ).fetchone()
            if proposal is None:
                raise ActionRefused("there is no such proposal waiting")
            await _end(connection, proposal, "withdrawn", "withdrawn by the verifier")
        if self.then is not None:
            # The red that waited for this proposal to be decided waits no longer, and
            # nobody is about to apply anything: the control plane looks now.
            await self.then(project_id)


async def add_obligation(
    connection: Connection,
    project_id: str,
    source: str,
    check: CheckContract,
    behaviour_id: str | None = None,
) -> uuid.UUID:
    """Called in the transaction that makes the thing required: a project check being
    registered, or a behaviour reaching trunk."""
    obligation_id = uuid.uuid7()
    await connection.execute(
        "INSERT INTO obligations (obligation_id, project_id, source, name, behaviour_id, "
        "contract, digest) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            obligation_id,
            project_id,
            source,
            check.contract_id,
            behaviour_id,
            Jsonb(check.to_dict()),
            check.digest,
        ),
    )
    return obligation_id


async def regression_set(
    connection: Connection, project_id: str, behaviour_id: str
) -> tuple[list[CheckContract], uuid.UUID | None]:
    """What a candidate of this behaviour must still satisfy, and the authority it is
    excused under, if any. The exemption is by the obligation's identity and is applied
    before obligations asking for the same observation are reduced to one check."""
    await void_what_no_longer_stands(connection, project_id)
    authority = await (
        await connection.execute(
            "SELECT supersession_id, obligation_id FROM supersessions WHERE project_id = %s "
            "AND behaviour_id = %s AND state = 'standing'",
            (project_id, behaviour_id),
        )
    ).fetchone()
    rows = await (
        await connection.execute(
            "SELECT obligation_id, contract FROM obligations WHERE project_id = %s "
            "AND state = 'active' ORDER BY name, obligation_id",
            (project_id,),
        )
    ).fetchall()
    excused = authority["obligation_id"] if authority else None
    checks = [
        CheckContract.from_dict(row["contract"]) for row in rows if row["obligation_id"] != excused
    ]
    return (
        list({check.digest: check for check in checks}.values()),
        authority["supersession_id"] if authority else None,
    )


async def reserve(connection: Connection, supersession_id: uuid.UUID, intent_id: uuid.UUID) -> None:
    """An integration was authorised with this exemption. From here the obligation is
    reserved to that integration: nothing else can replace it, and nothing that happens
    now changes what this integration will do when it settles."""
    await connection.execute(
        "UPDATE supersessions SET state = 'reserved', intent_id = %s WHERE supersession_id = %s",
        (intent_id, supersession_id),
    )


async def settle_with_the_integration(
    connection: Connection, intent: dict[str, Any], *, integrated: bool
) -> None:
    """The integration that was authorised with an exemption has settled. If it reached
    trunk, the old obligation is retired, here and now, from what the integration
    recorded. If it did not, the obligation stays in force and the reservation ends: the
    authority stands again if nothing it rests on has changed, and is void if it has."""
    if intent["supersession_id"] is None:
        return
    authority = await (
        await connection.execute(
            "SELECT * FROM supersessions WHERE supersession_id = %s FOR UPDATE",
            (intent["supersession_id"],),
        )
    ).fetchone()
    assert authority is not None
    if not integrated:
        await connection.execute(
            "UPDATE supersessions SET state = 'standing', intent_id = NULL "
            "WHERE supersession_id = %s",
            (authority["supersession_id"],),
        )
        await void_what_no_longer_stands(connection, intent["project_id"])
        return
    retired = await (
        await connection.execute(
            "UPDATE obligations SET state = 'retired', retired_by = %s "
            "WHERE obligation_id = %s RETURNING behaviour_id",
            (authority["supersession_id"], authority["obligation_id"]),
        )
    ).fetchone()
    assert retired is not None
    await connection.execute(
        "UPDATE supersessions SET state = 'used' WHERE supersession_id = %s",
        (authority["supersession_id"],),
    )
    await connection.execute(
        "UPDATE behaviours SET superseded_by = %s WHERE project_id = %s AND behaviour_id = %s",
        (intent["behaviour_id"], intent["project_id"], retired["behaviour_id"]),
    )
    # Nothing is owed for a behaviour that is now history: a reconfirmation a decision
    # asked for while this push was on its way is withdrawn, claimed or not.
    await retire_all_of_behaviour(connection, intent["project_id"], retired["behaviour_id"])
    # A seal vouches for one mapping of criteria to the behaviours in force. That mapping
    # has just changed for the story that lists the retired behaviour, if it is still
    # being worked on: its seal goes, and its list is a new revision, so that a seal
    # written from the old list is refused. The replacement's own story, if another, is
    # shown by the replacement as before. An approved story is history and is left as it was.
    await connection.execute(
        "UPDATE stories SET seal_id = NULL, sealed_revision = NULL, "
        "list_revision = list_revision + 1 WHERE project_id = %s "
        "AND state IN ('released', 'changes_requested') AND story_id = "
        "(SELECT story_id FROM behaviours WHERE project_id = %s AND behaviour_id = %s)",
        (intent["project_id"], intent["project_id"], retired["behaviour_id"]),
    )
    await append_event(
        connection,
        intent["project_id"],
        "obligation.retired",
        subject_id=retired["behaviour_id"],
        payload={
            "obligation_id": str(authority["obligation_id"]),
            "replaced_by": intent["behaviour_id"],
            "supersession_id": str(authority["supersession_id"]),
        },
    )


async def ended_by_an_ordinary_integration(
    connection: Connection, project_id: str, behaviour_id: str
) -> None:
    """An integration of this behaviour was authorised with no exemption. A proposal
    that it replace something has lost its ground, and ends here, in the transaction
    that authorises the push, whatever then becomes of the push."""
    proposals = await (
        await connection.execute(
            "SELECT * FROM supersessions WHERE project_id = %s AND behaviour_id = %s "
            "AND state IN ('proposed', 'standing') FOR UPDATE",
            (project_id, behaviour_id),
        )
    ).fetchall()
    for each in proposals:
        await _end(connection, each, "void", "the replacing behaviour was sent to trunk without it")


async def replaced_under(connection: Connection, supersession_id: uuid.UUID | None) -> str | None:
    """The behaviour whose obligation this authority replaces."""
    if supersession_id is None:
        return None
    row = await (
        await connection.execute(
            "SELECT o.behaviour_id FROM supersessions s JOIN obligations o USING (obligation_id) "
            "WHERE s.supersession_id = %s",
            (supersession_id,),
        )
    ).fetchone()
    assert row is not None
    return str(row["behaviour_id"])


async def basis_of_gate(connection: Connection, gate: dict[str, Any]) -> dict[str, Any] | None:
    """What a supersession gate would be opened on now, or None if its proposal no longer
    holds: then the gate is stale."""
    await void_what_no_longer_stands(connection, gate["project_id"])
    proposal = await (
        await connection.execute(
            "SELECT basis FROM supersessions WHERE gate_id = %s AND state = 'proposed'",
            (gate["gate_id"],),
        )
    ).fetchone()
    return proposal["basis"] if proposal else None


async def settle_gate(connection: Connection, gate: dict[str, Any], outcome: str) -> None:
    """The Product Owner's decision on a proposal that still holds."""
    proposal = await (
        await connection.execute(
            "SELECT * FROM supersessions WHERE gate_id = %s FOR UPDATE", (gate["gate_id"],)
        )
    ).fetchone()
    assert proposal is not None
    if outcome == "approved":
        await connection.execute(
            "UPDATE supersessions SET state = 'standing' WHERE supersession_id = %s",
            (proposal["supersession_id"],),
        )
        return
    await connection.execute(
        "UPDATE supersessions SET state = 'sent_back', ended_because = 'sent back at its gate' "
        "WHERE supersession_id = %s",
        (proposal["supersession_id"],),
    )


async def void_what_no_longer_stands(connection: Connection, project_id: str) -> None:
    """End every proposal and every unused authority whose ground has gone: what it was
    approved on has changed, or the replacing behaviour can no longer use it. (The
    obligation itself cannot go meanwhile: only its one live supersession retires it.) The obligation stays in force and is free
    for another proposal. One reserved to an integration is left alone until that
    integration settles. Called, under the gate lock, wherever a supersession is about
    to be proposed, decided or used."""
    candidates = await (
        await connection.execute(
            "SELECT s.*, o.digest "
            "FROM supersessions s JOIN obligations o USING (obligation_id) "
            "WHERE s.project_id = %s AND s.state IN ('proposed', 'standing') "
            "ORDER BY s.proposed_at FOR UPDATE OF s",
            (project_id,),
        )
    ).fetchall()
    for each in candidates:
        why = await _why_it_no_longer_stands(connection, project_id, each)
        if why:
            await _end(connection, each, "void", why)


async def _why_it_no_longer_stands(
    connection: Connection, project_id: str, supersession: dict[str, Any]
) -> str:
    replacing = await _replacing_or_none(connection, project_id, supersession["behaviour_id"])
    if replacing is None or replacing["contract"] is None:
        return "the replacing behaviour has no contract"
    if await closure_of_behaviour(connection, project_id, supersession["behaviour_id"]) == (
        CANCELLED
    ):
        return "the replacing behaviour's story or feature has ended"
    if replacing["state"] in ("integrated", "completed"):
        return "the replacing behaviour reached trunk without it"
    if await _being_pushed(connection, project_id, supersession["behaviour_id"]):
        return "the replacing behaviour has an integration authorised without it"
    now = await _basis(
        connection,
        project_id,
        {"obligation_id": supersession["obligation_id"], "digest": supersession["digest"]},
        replacing,
    )
    if now != supersession["basis"]:
        return "what it was proposed on has changed"
    return ""


async def _being_pushed(connection: Connection, project_id: str, behaviour_id: str) -> bool:
    return (
        await (
            await connection.execute(
                "SELECT 1 FROM integration_intents WHERE project_id = %s AND behaviour_id = %s "
                "AND state = 'outstanding'",
                (project_id, behaviour_id),
            )
        ).fetchone()
    ) is not None


async def _basis(
    connection: Connection,
    project_id: str,
    replaced: dict[str, Any],
    replacing: dict[str, Any],
) -> dict[str, Any] | None:
    """Exactly what an approval is an approval of."""
    if replacing["contract"] is None:
        return None
    return {
        "obligation_id": str(replaced["obligation_id"]),
        "obligation_digest": replaced["digest"],
        "contract_digest": ProcessContract.from_dict(replacing["contract"]).digest,
        "story_revision": replacing["story_revision"],
        "decisions": await story_basis(connection, project_id, replacing["story_id"]),
    }


async def _replacing(connection: Connection, project_id: str, behaviour_id: str) -> dict[str, Any]:
    replacing = await _replacing_or_none(connection, project_id, behaviour_id)
    if replacing is None:
        raise ActionRefused(f"unknown behaviour {behaviour_id!r}")
    if replacing["state"] in ("integrated", "completed"):
        raise ActionRefused(f"{behaviour_id} is integrated already: it replaces nothing now")
    if replacing["contract"] is None:
        raise ActionRefused(f"{behaviour_id} has no registered contract to show")
    return replacing


async def _replacing_or_none(
    connection: Connection, project_id: str, behaviour_id: str
) -> dict[str, Any] | None:
    return await (
        await connection.execute(
            "SELECT b.behaviour_id, b.state, b.contract, b.story_id, s.feature_id, "
            "s.revision AS story_revision FROM behaviours b "
            "JOIN stories s USING (project_id, story_id) "
            "WHERE b.project_id = %s AND b.behaviour_id = %s",
            (project_id, behaviour_id),
        )
    ).fetchone()


async def _end(connection: Connection, supersession: dict[str, Any], state: str, why: str) -> None:
    """A proposal or an unused authority ends, with its gate and its place in the inbox
    if it still had them. The obligation and the behaviour are free for another."""
    await connection.execute(
        "UPDATE supersessions SET state = %s, ended_because = %s WHERE supersession_id = %s",
        (state, why, supersession["supersession_id"]),
    )
    gate = await (
        await connection.execute(
            "SELECT * FROM gates WHERE gate_id = %s AND state = 'open' FOR UPDATE",
            (supersession["gate_id"],),
        )
    ).fetchone()
    if gate is not None:
        await _close_gate(connection, gate, "superseded", "superseded")
    await append_event(
        connection,
        supersession["project_id"],
        f"supersession.{state}",
        subject_id=supersession["behaviour_id"],
        payload={
            "supersession_id": str(supersession["supersession_id"]),
            "obligation_id": str(supersession["obligation_id"]),
            "reason": why,
        },
    )


async def propose_in(
    connection: Connection,
    principal: Principal,
    project_id: str,
    *,
    obligation: uuid.UUID,
    digest: str,
    behaviour: str,
    contract_digest: str,
    story_revision: int,
    basis: int,
    reason: str,
) -> uuid.UUID:
    """`Supersessions.propose`, in the caller's transaction."""
    if principal.kind is not PrincipalKind.WORKER or principal.role != "verifier":
        raise ActionRefused("a supersession is for the verifier to propose")
    if not reason.strip():
        raise ActionRefused("a supersession needs a stated reason")
    await lock_gate(connection, project_id, exclusive=True)
    await void_what_no_longer_stands(connection, project_id)
    replaced = await (
        await connection.execute(
            "SELECT * FROM obligations WHERE obligation_id = %s AND project_id = %s FOR UPDATE",
            (obligation, project_id),
        )
    ).fetchone()
    if replaced is None:
        raise ActionRefused("there is no such obligation in this project")
    if replaced["digest"] != digest or replaced["state"] != "active":
        raise ActionRefused("that is not the obligation as it stands now")
    if replaced["source"] != "behaviour":
        raise ActionRefused("a required project check is not replaced by a story's work")
    replacing = await _replacing(connection, project_id, behaviour)
    await require_startable(connection, project_id, replacing["story_id"], behaviour)
    live = await (
        await connection.execute(
            "SELECT 1 FROM supersessions WHERE project_id = %s AND state = ANY(%s) "
            "AND (obligation_id = %s OR behaviour_id = %s) LIMIT 1",
            (project_id, list(_LIVE), obligation, behaviour),
        )
    ).fetchone()
    if live is not None:
        raise ActionRefused(
            "there is already a replacement proposed or standing for that obligation, "
            "or one that this behaviour makes"
        )
    if await _being_pushed(connection, project_id, behaviour):
        raise ActionRefused(
            f"{behaviour} has an integration authorised already, without a replacement"
        )
    on = await _basis(connection, project_id, replaced, replacing)
    assert on is not None
    if (contract_digest, story_revision) != (on["contract_digest"], on["story_revision"]):
        raise ActionRefused(f"that is not {behaviour} as it stands now")
    require_basis(basis, on["decisions"])
    gate_id = await _open_gate(
        connection,
        project_id,
        replacing["feature_id"],
        kind="supersession",
        story_id=replacing["story_id"],
        basis=on,
        manifest={
            "reason": reason,
            "replaces": {
                "obligation_id": str(obligation),
                "behaviour_id": replaced["behaviour_id"],
                "contract": replaced["contract"],
            },
            "with": {
                "behaviour_id": behaviour,
                "story_id": replacing["story_id"],
                "story_revision": replacing["story_revision"],
                "contract": replacing["contract"],
            },
        },
    )
    await connection.execute(
        "INSERT INTO supersessions (supersession_id, project_id, obligation_id, "
        "behaviour_id, reason, proposed_by, basis, gate_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            uuid.uuid7(),
            project_id,
            obligation,
            behaviour,
            reason,
            principal.identity,
            Jsonb(on),
            gate_id,
        ),
    )
    return gate_id
