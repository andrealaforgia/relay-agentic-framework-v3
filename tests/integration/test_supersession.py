"""Replacing something that is already on trunk. Every integrated behaviour is an
obligation that later candidates must still satisfy. A correction that contradicts one
can integrate only under an authority the Product Owner gives for exactly that obligation
and exactly that replacing behaviour; the old obligation is retired when, and only when,
the replacement reaches trunk."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.integration import Outcome
from relay.application.stories import StoryDraft
from relay.application.supersessions import Supersessions
from relay.domain.behaviour import BehaviourState
from relay.domain.evidence import Baseline, Expected
from relay.domain.story import ActionRefused, StoryState
from relay.git.checkout import RemoteUnavailable
from relay.git.mirror import Mirror
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import ANALYST, CONTRACT, PRODUCT_OWNER, VERIFIER
from tests.integration.story_world import at_the_story_gate
from tests.integration.trunk_world import GREETING, PROJECT

REDUCED_RATE = replace(
    CONTRACT,
    contract_id="Z-reduced-rate",
    observes="the gross amount printed for a net amount at the reduced rate",
    expect=Expected(exit_code=0, stdout="105\n"),
    baseline=Baseline(exit_code=0, stdout="120\n"),
)
VAT_APPLIES_REDUCED_RATE = "import sys\nprint(int(sys.argv[1]) * 105 // 100)\n"
WHY = "the reduced rate replaces the standard one"


class Superseding(DecisionWorld):
    def __init__(self, *arguments: Any, **options: Any) -> None:
        super().__init__(*arguments, **options)
        self.supersessions = Supersessions(self.database)

    async def obligation_of(self, behaviour: str) -> Any:
        [found] = [
            each
            for each in await self.supersessions.obligations(self.project)
            if each.behaviour_id == behaviour
        ]
        return found

    async def in_force(self) -> list[str]:
        return sorted(
            each.name
            for each in await self.supersessions.obligations(self.project)
            if each.state == "active"
        )

    async def propose(self, replaced: str = "B1", by: str = "Z") -> uuid.UUID:
        obligation = await self.obligation_of(replaced)
        return await self.supersessions.propose(
            VERIFIER,
            self.project,
            obligation=obligation.obligation_id,
            digest=obligation.digest,
            behaviour=by,
            reason=WHY,
        )


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> Superseding:
    world = Superseding(database, tmp_path, runner_image)
    world.base = await world.open()
    gate, _ = await at_the_story_gate(world)
    await world.decide(gate)  # story 1 approved with B1 on trunk; story 2 is being worked on
    return world


async def z_reviewed(world: Superseding) -> str:
    """Z, in story 2, with a contract that contradicts B1, verified green and reviewed."""
    base = await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    return await world.reviewed("Z", {"vat.py": VAT_APPLIES_REDUCED_RATE}, on=base)


async def test_what_is_on_trunk_is_an_obligation_every_later_candidate_must_meet(
    world: Superseding,
) -> None:
    assert await world.in_force() == [CONTRACT.contract_id]
    obligation = await world.obligation_of("B1")
    assert (obligation.source, obligation.state) == ("behaviour", "active")
    before = world.trunk()
    await z_reviewed(world)

    receipt = await world.integrate("Z")

    assert receipt.outcome is Outcome.CHECKS_FAILED
    assert world.trunk() == before
    assert await world.in_force() == [CONTRACT.contract_id]


async def test_an_approved_supersession_lets_the_replacement_in_and_retires_what_it_replaces(
    world: Superseding,
) -> None:
    candidate = await z_reviewed(world)
    gate = await world.propose()
    [item] = await world.stories.inbox(PROJECT)
    assert (item.kind, item.gate_id) == ("supersession", gate)
    assert item.manifest["replaces"]["contract"]["contract_id"] == CONTRACT.contract_id
    assert item.manifest["with"]["contract"]["contract_id"] == REDUCED_RATE.contract_id
    assert (item.manifest["with"]["story_id"], item.manifest["reason"]) == ("S2", WHY)

    assert (await world.decide(gate)).disposition == "effective"
    assert await world.in_force() == [CONTRACT.contract_id]  # still in force for everyone else

    assert (await world.integrate("Z")).outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate
    assert await world.in_force() == [REDUCED_RATE.contract_id]
    retired = await world.obligation_of("B1")
    assert retired.state == "retired"
    assert await world.column("SELECT superseded_by FROM behaviours WHERE behaviour_id = 'B1'") == [
        "Z"
    ]
    assert await world.state("B1") is BehaviourState.INTEGRATED


async def test_a_supersession_is_proposed_by_the_verifier_for_an_obligation_as_it_stands(
    world: Superseding,
) -> None:
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    obligation = await world.obligation_of("B1")
    named = {"obligation": obligation.obligation_id, "digest": obligation.digest, "reason": WHY}

    for principal, changed, why in (
        (ANALYST, {}, "verifier"),
        (PRODUCT_OWNER, {}, "verifier"),
        (VERIFIER, {"digest": "sha256:other"}, "not the obligation"),
        (VERIFIER, {"obligation": uuid.uuid7()}, "no such obligation"),
        (VERIFIER, {"reason": " "}, "reason"),
        (VERIFIER, {"behaviour": "B1"}, "integrated"),
        (VERIFIER, {"behaviour": "nobody"}, "unknown behaviour"),
    ):
        with pytest.raises(ActionRefused, match=why):
            await world.supersessions.propose(
                principal, PROJECT, **{"behaviour": "Z", **named, **changed}
            )

    assert await world.stories.inbox(PROJECT) == []


async def test_an_independent_check_asking_the_same_thing_stays_in_force(
    world: Superseding,
) -> None:
    await world.integration.require_check(PROJECT, CONTRACT.as_check())
    before = world.trunk()
    await z_reviewed(world)
    await world.decide(await world.propose())

    receipt = await world.integrate("Z")

    assert receipt.outcome is Outcome.CHECKS_FAILED
    assert world.trunk() == before
    assert (await world.obligation_of("B1")).state == "active"


async def test_an_authority_given_under_other_decisions_is_void(world: Superseding) -> None:
    await z_reviewed(world)
    await world.decide(await world.propose())
    assumed = await world.decisions.assume(ANALYST, PROJECT, "F1", story_id="S2", text="5%")
    await world.replace(assumed, "5%, for books only")
    await world.reconfirm_everything()

    assert (await world.integrate("Z")).outcome is Outcome.CHECKS_FAILED
    assert (await world.obligation_of("B1")).state == "active"


async def test_one_obligation_has_one_replacement_and_one_behaviour_replaces_one_thing(
    world: Superseding,
) -> None:
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    first = await world.propose()

    with pytest.raises(ActionRefused, match="already"):
        await world.propose()

    await world.decide(first, "request_changes", reason="not that one")
    assert await world.stories.inbox(PROJECT) == []
    again = await world.propose()
    assert (await world.decide(first)).disposition == "stale"
    assert (await world.decide(again)).disposition == "effective"
    with pytest.raises(ActionRefused, match="already"):
        await world.propose()
    assert await world.in_force() == [CONTRACT.contract_id]


async def test_the_verifier_can_withdraw_a_proposal_nobody_has_decided(
    world: Superseding,
) -> None:
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    gate = await world.propose()

    await world.supersessions.withdraw(VERIFIER, PROJECT, gate)

    assert await world.stories.inbox(PROJECT) == []
    assert (await world.decide(gate)).disposition == "stale"
    await world.propose()


async def test_an_approval_that_comes_after_the_replacement_went_in_the_ordinary_way_is_stale(
    world: Superseding,
) -> None:
    base = await world.awaiting_builder("Z", GREETING, "S2")  # contradicts nothing
    gate = await world.propose()
    await world.reviewed("Z", {"greeting.txt": "hello\n"}, on=base)
    assert (await world.integrate("Z")).outcome is Outcome.INTEGRATED

    assert (await world.decide(gate)).disposition == "stale"

    assert sorted(await world.in_force()) == sorted([CONTRACT.contract_id, GREETING.contract_id])
    assert await world.column("SELECT state FROM supersessions") == ["void"]


async def test_cancelling_the_replacements_feature_leaves_the_obligation_to_someone_else(
    world: Superseding,
) -> None:
    other = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})
    await world.decide(await world.roadmap(other, feature="F2"))
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    await world.decide(await world.propose())

    await world.close("cancel", reason="dropped")

    assert await world.in_force() == [CONTRACT.contract_id]
    assert await world.story("T1") is StoryState.RELEASED
    await world.awaiting_builder("Y", REDUCED_RATE, "T1")
    await world.decide(await world.propose(by="Y"))
    assert await world.column("SELECT state FROM supersessions ORDER BY proposed_at") == [
        "void",
        "standing",
    ]


async def test_a_push_authorised_with_an_exemption_and_then_lost_retires_exactly_that(
    world: Superseding, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = await z_reviewed(world)
    await world.decide(await world.propose())

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", unreachable)
        assert (await world.integrate("Z")).outcome is Outcome.PENDING
        assert await world.column("SELECT state FROM supersessions") == ["reserved"]
        with pytest.raises(ActionRefused, match="already"):
            await world.propose()  # reserved to that push: nothing else may replace it
        assumed = await world.decisions.assume(ANALYST, PROJECT, "F1", story_id="S2", text="5%")
        await world.replace(assumed, "5%, for books only")  # the basis moves under the push
        assert await world.column("SELECT state FROM supersessions") == ["reserved"]

    await world.integration.reconcile()

    assert world.trunk() == candidate
    assert await world.in_force() == [REDUCED_RATE.contract_id]
    assert await world.column("SELECT state FROM supersessions") == ["used"]
    assert (await world.events()).count("obligation.retired") == 1


@pytest.mark.parametrize("meanwhile", ["nothing", "a decision"])
async def test_a_refused_push_leaves_the_obligation_and_the_authority_as_they_now_are(
    world: Superseding, monkeypatch: pytest.MonkeyPatch, meanwhile: str
) -> None:
    await z_reviewed(world)
    before = world.trunk()
    await world.decide(await world.propose())
    assumed = await world.decisions.assume(ANALYST, PROJECT, "F1", story_id="S2", text="5%")
    world.hook_flag("refuse").touch()
    real = Mirror._push

    async def decided_while_the_push_is_on_its_way(
        self: Mirror, *arguments: Any, **named: Any
    ) -> Any:
        if meanwhile == "a decision":
            await world.replace(assumed, "5%, for books only")
        return await real(self, *arguments, **named)

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", decided_while_the_push_is_on_its_way)
        assert (await world.integrate("Z")).outcome is Outcome.REJECTED

    assert world.trunk() == before
    assert await world.in_force() == [CONTRACT.contract_id]
    assert await world.column("SELECT state FROM supersessions") == [
        "standing" if meanwhile == "nothing" else "void"
    ]
    if meanwhile == "a decision":
        # The obligation is free again, for this behaviour or another.
        await world.reconfirm_everything()
        await world.propose()


async def test_an_approved_story_is_left_exactly_as_it_was_when_its_behaviour_is_replaced(
    world: Superseding,
) -> None:
    async def story_one() -> list[Any]:
        async with world.database.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT s.state, s.seal_id, s.sealed_revision, g.state AS gate, g.outcome, "
                    "g.manifest_digest FROM stories s JOIN gates g ON g.story_id = s.story_id "
                    "AND g.kind = 'story' WHERE s.story_id = 'S1'"
                )
            ).fetchone()
        assert row is not None
        return list(row.values())

    before = await story_one()
    assert before[0] == "approved" and before[1] is not None
    await z_reviewed(world)
    await world.decide(await world.propose())

    assert (await world.integrate("Z")).outcome is Outcome.INTEGRATED

    assert await story_one() == before
    assert (await world.obligation_of("B1")).state == "retired"
