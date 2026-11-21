"""A project whose features are carried all the way to completion: the Product Owner's
decisions go in through `Features.decide`, which is what makes Relay carry on by itself."""

from __future__ import annotations

import uuid
from typing import Any

from relay.application.stories import StoryDraft
from relay.domain.story import HumanAction, Receipt
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import PRODUCT_OWNER
from tests.integration.story_world import STANDARD_RATE


class CompletionWorld(DecisionWorld):
    def action(self, subject: uuid.UUID, action: str = "approve", **payload: str) -> HumanAction:
        self.requests += 1
        return HumanAction(
            PRODUCT_OWNER, f"{self.project}-request-{self.requests}", str(subject), action, payload
        )

    async def decide(self, gate: uuid.UUID, action: str = "approve", **payload: str) -> Receipt:
        return await self.features.decide(self.action(gate, action, **payload))

    async def feature(self, feature: str = "F1") -> str:
        [state] = await self.column(
            f"SELECT state FROM features WHERE project_id = '{self.project}' "
            f"AND feature_id = '{feature}'"
        )
        assert isinstance(state, str)
        return state

    async def summary(self, feature: str = "F1") -> dict[str, Any] | None:
        return await self.features.summary_of(self.project, feature)

    async def batches(self) -> list[object]:
        """The outcome of every final-check batch of this project, oldest first."""
        return await self.column(
            f"SELECT state FROM completion_batches WHERE project_id = '{self.project}' "
            "ORDER BY created_at"
        )

    async def completions_wanted(self) -> int:
        [count] = await self.column(
            f"SELECT count(*) FROM assignments WHERE project_id = '{self.project}' "
            "AND kind = 'complete' AND state = 'wanted'"
        )
        assert isinstance(count, int)
        return count


async def last_story_at_its_gate(world: CompletionWorld, *more: StoryDraft) -> uuid.UUID:
    """A feature of one story, delivered, sealed and assessed: one approval from done."""
    await world.decide(await world.roadmap(STANDARD_RATE, *more))
    await world.deliver("B1")
    await world.seal()
    await world.assess("B1 shows 120 for 100, as the story asks")
    return await world.gate()
