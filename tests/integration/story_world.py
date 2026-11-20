"""A project with a roadmap of its own, driven the way roles and a Product Owner would."""

from __future__ import annotations

import uuid
from typing import Any

from relay.application.features import Features
from relay.application.integration import Outcome
from relay.application.reviews import NewFinding
from relay.application.stories import Stories, StoryDraft
from relay.domain.evidence import ProcessContract
from relay.domain.review import Severity
from relay.domain.story import HumanAction, Receipt, StoryState
from tests.integration.fixtures import ANALYST, CONTRACT, PRODUCT_OWNER, VERIFIER
from tests.integration.trunk_world import TrunkWorld

STANDARD_RATE = StoryDraft(
    "S1", "Standard rate", "a gross price is shown", {"AC1": "100 net is shown as 120"}
)
INJECTION = NewFinding(
    Severity.HIGH, "the amount is passed to eval", "vat.py line 2", "parse it as an integer"
)
STYLE = NewFinding(Severity.LOW, "no usage message", "vat.py", "print usage on bad input")
GREETS = StoryDraft("S2", "Greeting", "the shopper is greeted", {"AC1": "hello is printed"})


class StoryWorld(TrunkWorld):
    """A project with a roadmap of its own, and behaviours kept in their order."""

    side_by_side = False

    def __init__(self, *arguments: Any, **options: Any) -> None:
        super().__init__(*arguments, **options)
        self.stories = Stories(self.database)
        self.features = Features(self.database, self.reviews, self.integration)
        self.base = ""
        self.requests = 0

    async def open_project(self, address: str) -> None:
        await self.behaviours.open_project(self.project, address)

    async def roadmap(self, *stories: StoryDraft, feature: str = "F1") -> uuid.UUID:
        await self.stories.open_feature(PRODUCT_OWNER, self.project, feature, "prices need VAT")
        return await self.stories.publish_roadmap(
            ANALYST, self.project, feature, list(stories), basis=0
        )

    async def decide(self, gate: uuid.UUID, action: str = "approve", **payload: str) -> Receipt:
        self.requests += 1
        return await self.stories.decide(
            HumanAction(
                PRODUCT_OWNER, f"{self.project}-request-{self.requests}", str(gate), action, payload
            )
        )

    async def deliver(
        self,
        behaviour: str,
        story: str = "S1",
        contract: ProcessContract = CONTRACT,
        files: dict[str, str] | None = None,
    ) -> str:
        """One behaviour from proposal to trunk, by the real route."""
        base = await self.awaiting_builder(behaviour, contract, story)
        candidate = await self.reviewed(behaviour, files, on=base)
        assert (await self.integrate(behaviour)).outcome is Outcome.INTEGRATED
        return candidate

    async def seal(self, story: str = "S1") -> None:
        revision, listed = await self.stories.revisions_of(self.project, story)
        await self.stories.seal(
            VERIFIER,
            self.project,
            story,
            story_revision=revision,
            list_revision=listed,
            basis=await self.basis(story),
        )

    async def assess(self, statement: str, story: str = "S1") -> None:
        revision, listed = await self.stories.revisions_of(self.project, story)
        seal = await self.stories.seal_of(self.project, story)
        await self.stories.assess(
            ANALYST,
            self.project,
            story,
            statement,
            story_revision=revision,
            list_revision=listed,
            basis=await self.basis(story),
            seal=seal or uuid.uuid7(),
        )

    async def story(self, story: str = "S1") -> StoryState:
        return await self.stories.state_of(self.project, story)

    async def gate(self) -> uuid.UUID:
        [item] = await self.stories.inbox(self.project)
        return item.gate_id

    async def close(self, action: str, feature: str = "F1", **payload: str) -> Receipt:
        self.requests += 1
        return await self.features.close(
            self.project,
            HumanAction(
                PRODUCT_OWNER, f"{self.project}-request-{self.requests}", feature, action, payload
            ),
        )


async def at_the_story_gate(world: StoryWorld) -> tuple[uuid.UUID, str]:
    """Story 1 of two is delivered, sealed and assessed. Returns its gate and its commit."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    candidate = await world.deliver("B1")
    await world.seal()
    await world.assess("B1 shows 120 for 100, as the story asks")
    return await world.gate(), candidate
