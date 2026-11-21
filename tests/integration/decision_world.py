"""A project in which workers ask, a person answers, and the owning roles reconfirm."""

from __future__ import annotations

import uuid
from typing import Any

from relay.application.assignments import Assignments
from relay.application.decisions import Decisions
from relay.domain.evidence import ProcessContract
from relay.domain.story import HumanAction, Receipt
from tests.integration.fixtures import ANALYST, PRODUCT_OWNER, VERIFIER
from tests.integration.story_world import StoryWorld

RATE = {
    "text": "Which VAT rate applies to books?",
    "why": "the expected gross amount depends on it",
    "options": ["zero-rated", "standard rate"],
}


class DecisionWorld(StoryWorld):
    def __init__(self, *arguments: Any, **options: Any) -> None:
        super().__init__(*arguments, **options)
        self.decisions = Decisions(self.database, self.features)
        self.claims = Assignments(self.database)
        self.asked = 0

    async def ask(self, story: str | None = "S1", feature: str = "F1") -> uuid.UUID:
        self.asked += 1
        return await self.decisions.ask(
            VERIFIER,
            f"{self.project}-ask-{self.asked}",
            self.project,
            feature,
            story_id=story,
            **RATE,
        )

    async def answer(self, question: uuid.UUID, text: str = "zero-rated") -> Receipt:
        self.requests += 1
        return await self.decisions.answer(
            HumanAction(
                PRODUCT_OWNER,
                f"{self.project}-answer-{self.requests}",
                str(question),
                "answer",
                {"text": text},
            )
        )

    async def replace(self, decision: uuid.UUID, text: str) -> Receipt:
        self.requests += 1
        return await self.decisions.replace(
            HumanAction(
                PRODUCT_OWNER,
                f"{self.project}-replace-{self.requests}",
                str(decision),
                "replace",
                {"text": text},
            )
        )

    async def owed(self) -> list[dict[str, Any]]:
        """The reconfirmations still owed, in the order they became so."""
        async with self.database.connection() as connection:
            return await (
                await connection.execute(
                    "SELECT assignment_id, subject, feature_id, story_id, behaviour_id, role, "
                    "(issued_on->>'basis')::integer AS basis, "
                    "(issued_on->>'revision')::integer AS revision FROM assignments "
                    "WHERE project_id = %s AND kind = 'reconfirm' AND state IN ('wanted', 'granted') "
                    "ORDER BY created_at",
                    (self.project,),
                )
            ).fetchall()

    async def reconfirm(self, owed: dict[str, Any]) -> None:
        """What the owning role's worker does with one reconfirmation: claim it, read
        what it is about, and state that it stands."""
        worker = ANALYST if owed["role"] == "analyst" else VERIFIER
        grant = await self.claims.claim(worker, owed["assignment_id"])
        basis = owed["basis"]
        if owed["subject"] == "roadmap":
            await self.decisions.reconfirm_roadmap(
                worker,
                self.project,
                owed["feature_id"],
                grant=grant,
                basis=basis,
                revision=owed["revision"],
            )
        elif owed["subject"] == "story":
            revision, _ = await self.stories.revisions_of(self.project, owed["story_id"])
            await self.decisions.reconfirm_story(
                worker, self.project, owed["story_id"], grant=grant, basis=basis, revision=revision
            )
        else:
            await self.decisions.reconfirm_contract(
                worker,
                self.project,
                owed["behaviour_id"],
                grant=grant,
                basis=basis,
                contract_digest=await self.contract_digest(owed["behaviour_id"]),
                evidence=await self.decisions.evidence_of(self.project, owed["behaviour_id"]),
            )

    async def contract_digest(self, behaviour: str) -> str:
        [contract] = await self.column(
            f"SELECT contract FROM behaviours WHERE behaviour_id = '{behaviour}' "
            f"AND project_id = '{self.project}'"
        )
        assert isinstance(contract, dict)
        return ProcessContract.from_dict(contract).digest

    async def reconfirm_everything(self) -> int:
        """Every owner states that what is theirs stands. Returns how many did."""
        owed = await self.owed()
        for each in owed:
            await self.reconfirm(each)
        return len(owed)
