"""Closing and reopening a feature, and what Relay then does without being asked.

One pass per project puts right whatever a pause, or a process that ended, left
unfinished. It runs after every resume and when the control plane starts, it is safe to
run any number of times, and nobody has to call anything else for work to carry on.
"""

from __future__ import annotations

import uuid
from typing import Any

from relay.application.assignments import announce_again, revoke_every_grant
from relay.application.behaviours import Behaviours
from relay.application.closure import lock_gate, lock_project
from relay.application.completion import evaluate_completion
from relay.application.final_checks import Completion, end_batches_left_running, usage_of
from relay.application.integration import (
    Integration,
    IntegrationRefused,
    hand_on_unfinished_batches,
)
from relay.application.invocations import Invocations
from relay.application.owed import owe_what_is_needed
from relay.application.reviews import Reviews
from relay.application.stories import Stories, close_feature
from relay.domain.story import ActionRefused, HumanAction, Receipt
from relay.git.checkout import CheckoutFailed
from relay.persistence.database import Pool

_ROUNDS_OF_TAKING_UP = 3


class Features:
    def __init__(
        self,
        pool: Pool,
        reviews: Reviews,
        integration: Integration,
        completion: Completion | None = None,
        invocations: Invocations | None = None,
        behaviours: Behaviours | None = None,
    ) -> None:
        self._behaviours = behaviours
        self._pool = pool
        self._reviews = reviews
        self._integration = integration
        self._invocations = invocations
        # In this process the control plane observes final checks as it observes an
        # integration's: with the same oracle, and the same patience with the remote.
        # A completion put off behind a push is taken up by whichever control plane
        # settles that push: that is the integration's own doing, not wired here.
        self._completion = completion or integration.completion

    async def decide(self, action: HumanAction) -> Receipt:
        """What the Product Owner decides at a gate, followed by whatever that leaves
        for Relay to do by itself: a feature whose last story was just accepted is
        checked and completed with nothing else called. The receipt is the decision's;
        the same request sent again is given it again, and the completions are looked at
        again, which changes nothing if they are done."""
        receipt = await Stories(self._pool).decide(action)
        async with self._pool.connection() as connection:
            gate = await (
                await connection.execute(
                    "SELECT project_id FROM gates WHERE gate_id = %s", (action.target,)
                )
            ).fetchone()
        assert gate is not None
        # The whole pass, which ends with the completions: a decision can leave a red to
        # observe or an integration to take up, with no worker about to apply anything.
        await self.reconcile(gate["project_id"])
        return receipt

    async def retry_integration(
        self, project_id: str, behaviour_id: str, reason: str, request_id: str | None = None
    ) -> None:
        """A person allows an integration the repository refused to be tried again, and
        Relay tries it: the retry owes the integration, and the pass takes it up. The
        same request sent again allows no further push; the pass is run again, which
        changes nothing if there is nothing owed."""
        await self._integration.retry_integration(project_id, behaviour_id, reason, request_id)
        await self.reconcile(project_id)

    async def usage_of(self, project_id: str, feature_id: str) -> Any:
        """What the feature's calls have used, as known now: the summary of a completed
        feature keeps what was known when it completed."""
        return await usage_of(self._pool, project_id, feature_id)

    async def summary_of(self, project_id: str, feature_id: str) -> dict[str, Any] | None:
        """The summary of a completed feature; none until it is completed."""
        return await self._completion.summary_of(project_id, feature_id)

    async def complete_what_is_owed(self, project_id: str) -> None:
        await self._completion.take_up_what_is_owed(project_id)

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
                await end_batches_left_running(connection, project_id)
            await hand_on_unfinished_batches(connection, project_id, starting=starting)
            features = await (
                await connection.execute(
                    "SELECT feature_id FROM features WHERE project_id = %s "
                    "AND state IN ('delivering', 'checking', 'needs_decision') ORDER BY opened",
                    (project_id,),
                )
            ).fetchall()
        for feature in features:
            # Each in a transaction of its own: this holds one feature's row at a time,
            # and so never waits for one while holding another, as a decision that
            # releases a story into another feature does.
            async with self._pool.connection() as connection, connection.transaction():
                await lock_gate(connection, project_id)
                await lock_project(connection, project_id)
                await evaluate_completion(connection, project_id, feature["feature_id"])
        async with self._pool.connection() as connection, connection.transaction():
            # The net under every trigger: whatever a story needs and is not owed.
            await lock_gate(connection, project_id)
            await owe_what_is_needed(connection, project_id)
        if self._invocations is not None:
            # What a worker that is gone left behind: a grant, a call, a known result.
            await self._invocations.recover(project_id)
        if self._behaviours is not None:
            # A red is the control plane's to observe, and nobody else asks for one.
            await self._behaviours.observe_owed_reds(project_id)
        # The completions are taken up once, at the end of this pass.
        await self._integration.reconcile(then_completions=False, project_id=project_id)
        await self._reviews.publish_candidates(project_id)
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
                    await self._integration.take_up(assignment_id)
                except CheckoutFailed, ActionRefused, IntegrationRefused:
                    # The repository could not be read, or the feature is closed (it may
                    # have been closed a moment ago), or this process cannot observe
                    # checks. The integration stays owed; the pass, and the other
                    # projects, carry on. It is not tried again in this pass.
                    continue
        await self.complete_what_is_owed(project_id)

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
