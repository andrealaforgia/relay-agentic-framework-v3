"""Closing and reopening a feature, and what Relay then does without being asked.

One pass per project puts right whatever a pause, or a process that ended, left
unfinished. It runs after every resume and when the control plane starts, it is safe to
run any number of times, and nobody has to call anything else for work to carry on.
"""

from __future__ import annotations

import uuid

from relay.application.assignments import announce_again, revoke_every_grant
from relay.application.closure import lock_gate
from relay.application.integration import (
    Integration,
    IntegrationRefused,
    hand_on_unfinished_batches,
)
from relay.application.reviews import Reviews
from relay.application.stories import close_feature
from relay.domain.story import ActionRefused, HumanAction, Receipt
from relay.git.checkout import CheckoutFailed
from relay.persistence.database import Pool

_ROUNDS_OF_TAKING_UP = 3


class Features:
    def __init__(self, pool: Pool, reviews: Reviews, integration: Integration) -> None:
        self._pool = pool
        self._reviews = reviews
        self._integration = integration

    async def close(self, project_id: str, action: HumanAction) -> Receipt:
        """Pause, resume or cancel the feature the action names. A resume, even one
        that changed nothing or is being repeated, is followed by the pass that takes up
        what a pause held: the pass is safe to run again."""
        receipt = await close_feature(self._pool, project_id, action)
        if action.action == "resume":
            await self.reconcile(project_id)
        return receipt

    async def start(self) -> None:
        """Run when the control plane starts. Every worker of the process that ended is
        gone: grants are taken back and checks left running are closed, on every feature."""
        async with self._pool.connection() as connection:
            projects = await (
                await connection.execute("SELECT project_id FROM projects ORDER BY project_id")
            ).fetchall()
        for project in projects:
            await self.reconcile(project["project_id"], starting=True)

    async def reconcile(self, project_id: str, *, starting: bool = False) -> None:
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            if starting:
                await revoke_every_grant(connection, project_id)
            await hand_on_unfinished_batches(connection, project_id, starting=starting)
        await self._integration.reconcile()
        await self._reviews.publish_candidates()
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            await announce_again(connection, project_id)
        # In this process the control plane is the integrator: it takes these up itself.
        # Taking one up can leave another owed (its checks were overtaken by a newly
        # required one, say), so the pass looks again, a bounded number of times: checks
        # that keep being overtaken wait for the next pass instead of holding this one.
        attempted: set[uuid.UUID] = set()
        for _ in range(_ROUNDS_OF_TAKING_UP):
            owed = [
                each for each in await self._integrations_owed(project_id) if each not in attempted
            ]
            if not owed:
                break
            for assignment_id in owed:
                attempted.add(assignment_id)
                try:
                    await self._integration.continue_integration(assignment_id)
                except CheckoutFailed, ActionRefused, IntegrationRefused:
                    # The repository could not be read, or the feature is closed (it may
                    # have been closed a moment ago), or this process cannot observe
                    # checks. The integration stays owed; the pass, and the other
                    # projects, carry on. It is not tried again in this pass.
                    continue

    async def _integrations_owed(self, project_id: str) -> list[uuid.UUID]:
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT assignment_id FROM assignments WHERE project_id = %s "
                    "AND kind = 'integrate' AND state = 'wanted' ORDER BY created_at",
                    (project_id,),
                )
            ).fetchall()
        return [row["assignment_id"] for row in rows]
