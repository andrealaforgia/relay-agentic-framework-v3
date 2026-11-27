"""Replacing something that is already on trunk. Every integrated behaviour is an
obligation that later candidates must still satisfy. A correction that contradicts one
can integrate only under an authority the Product Owner gives for exactly that obligation
and exactly that replacing behaviour; the old obligation is retired when, and only when,
the replacement reaches trunk."""

from __future__ import annotations

import asyncio
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
from relay.testing.oracle import ContainerIsolation
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import (
    ANALYST,
    CONTRACT,
    PRODUCT_OWNER,
    VERIFIER,
    HeldAtABarrier,
    held_at_the_barrier,
)
from tests.integration.story_world import at_the_story_gate
from tests.integration.test_rejection import reject
from tests.integration.trunk_world import GREETING, PROJECT, ZERO_STAYS_ZERO

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
        return await self.supersessions.propose(
            VERIFIER, self.project, reason=WHY, **await self.as_read(replaced, by)
        )

    async def as_read(self, replaced: str = "B1", by: str = "Z") -> dict[str, Any]:
        """What the Verifier read just before proposing: the obligation, and the
        replacing behaviour's contract, story revision and decision basis."""
        obligation = await self.obligation_of(replaced)
        [story] = await self.column(
            f"SELECT story_id FROM behaviours WHERE behaviour_id = '{by}' "
            f"AND project_id = '{self.project}'"
        )
        assert isinstance(story, str)
        return {
            "obligation": obligation.obligation_id,
            "digest": obligation.digest,
            "behaviour": by,
            "contract_digest": await self.contract_digest(by),
            "story_revision": await self.revision(story),
            "basis": await self.basis(story),
        }


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
    assert item.manifest["with"]["story_revision"] == await world.revision("S2")

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
    named = {**await world.as_read(), "reason": WHY}

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
            await world.supersessions.propose(principal, PROJECT, **{**named, **changed})

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
    before = world.trunk()
    await world.reconfirm_everything()  # and the pass that follows takes up Z's integration

    assert await world.column(
        "SELECT count(*) FROM events WHERE event_type = 'integration.checks_failed'"
    ) == [1]
    assert world.trunk() == before
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

    with pytest.raises(ActionRefused, match="for the verifier"):
        await world.supersessions.withdraw(ANALYST, PROJECT, gate)
    await world.supersessions.withdraw(VERIFIER, PROJECT, gate)

    assert await world.stories.inbox(PROJECT) == []
    assert (await world.decide(gate)).disposition == "stale"
    # Sent again after a lost reply, it is the same withdrawal, and withdraws nothing more.
    await world.supersessions.withdraw(VERIFIER, PROJECT, gate)
    decided = await world.propose()
    await world.decide(decided)
    with pytest.raises(ActionRefused, match="no such proposal waiting"):
        await world.supersessions.withdraw(VERIFIER, PROJECT, decided)
    assert await world.column("SELECT state FROM supersessions ORDER BY proposed_at") == [
        "withdrawn",
        "standing",
    ]


async def test_an_approval_that_comes_after_the_replacement_went_in_the_ordinary_way_is_stale(
    world: Superseding,
) -> None:
    base = await world.awaiting_builder("Z", GREETING, "S2")  # contradicts nothing
    # Proposed once its candidate was already built and reviewed: nothing is built for
    # a behaviour whose replacement the Product Owner has yet to decide.
    await world.reviewed("Z", {"greeting.txt": "hello\n"}, on=base)
    gate = await world.propose()
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
    if meanwhile == "nothing":
        # The authority stands as before. When the person lets the push be tried again,
        # the replacement is still excused from what it replaces.
        world.hook_flag("refuse").unlink()
        await world.integration.retry_integration(PROJECT, "Z", "the hook is fixed")
        assert (await world.integrate("Z")).outcome is Outcome.INTEGRATED
        assert await world.in_force() == [REDUCED_RATE.contract_id]
        return
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


async def test_only_the_replacement_is_excused_and_its_sibling_is_not(world: Superseding) -> None:
    """Two candidates of the same story in play at once, which the story order does not
    allow by itself; the fixture arranges it, as the integration tests do."""
    world.side_by_side = True
    base = await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    await world.awaiting_builder("Y", GREETING, "S2")
    await world.decide(await world.propose())
    await world.reviewed(
        "Y", {"greeting.txt": "hello\n", "vat.py": VAT_APPLIES_REDUCED_RATE}, on=base
    )

    assert (await world.integrate("Y")).outcome is Outcome.CHECKS_FAILED  # it breaks B1

    assert world.trunk() == base
    await world.reviewed("Z", {"vat.py": VAT_APPLIES_REDUCED_RATE}, on=base)
    assert (await world.integrate("Z")).outcome is Outcome.INTEGRATED


async def test_a_behaviour_replaces_one_obligation_at_a_time(world: Superseding) -> None:
    await world.deliver("G", "S2", GREETING, {"greeting.txt": "hello\n"})
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    await world.decide(await world.propose(replaced="B1"))

    with pytest.raises(ActionRefused, match="already"):
        await world.propose(replaced="G")

    assert await world.column("SELECT count(*) FROM supersessions") == [1]


async def test_an_approval_is_stale_while_the_replacement_is_being_pushed_without_it(
    world: Superseding, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = await world.awaiting_builder("Z", GREETING, "S2")
    # Proposed once its candidate was already built and reviewed: nothing is built for
    # a behaviour whose replacement the Product Owner has yet to decide.
    await world.reviewed("Z", {"greeting.txt": "hello\n"}, on=base)
    gate = await world.propose()

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", unreachable)
        assert (await world.integrate("Z")).outcome is Outcome.PENDING

        assert (await world.decide(gate)).disposition == "stale"

    assert await world.column("SELECT state FROM supersessions") == ["void"]
    assert (await world.obligation_of("B1")).state == "active"


async def test_an_authority_survives_a_restart_and_is_used_once(world: Superseding) -> None:
    candidate = await z_reviewed(world)
    await world.decide(await world.propose())

    assert await world.column("SELECT state FROM supersessions") == ["standing"]

    await world.features.start()  # which takes up the integration that is owed

    assert world.trunk() == candidate
    await world.features.start()
    assert await world.column("SELECT state FROM supersessions") == ["used"]
    assert (await world.events()).count("obligation.retired") == 1


async def test_a_required_project_check_is_not_replaced_by_a_storys_work(
    world: Superseding,
) -> None:
    await world.integration.require_check(PROJECT, GREETING.as_check())
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    [check] = [
        each for each in await world.supersessions.obligations(PROJECT) if each.source == "check"
    ]

    with pytest.raises(ActionRefused, match="required project check"):
        await world.supersessions.propose(
            VERIFIER,
            PROJECT,
            reason=WHY,
            **{
                **await world.as_read(),
                "obligation": check.obligation_id,
                "digest": check.digest,
            },
        )

    assert await world.column("SELECT count(*) FROM supersessions") == [0]


async def test_a_replacement_is_proposed_only_for_work_that_can_go_on(world: Superseding) -> None:
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    await world.close("pause", reason="the rates are being reconsidered")

    with pytest.raises(ActionRefused, match="paused"):
        await world.propose()

    assert await world.stories.inbox(PROJECT) == []


async def test_a_behaviour_with_no_contract_yet_replaces_nothing(world: Superseding) -> None:
    await world.behaviours.propose(VERIFIER, 
        PROJECT, "S2", "Z", "the reduced rate", ["AC1"],
        basis=await world.basis("S2"), story_revision=await world.revision("S2"),
    )  # fmt: skip

    obligation = await world.obligation_of("B1")
    with pytest.raises(ActionRefused, match="no registered contract"):
        await world.supersessions.propose(
            VERIFIER,
            PROJECT,
            reason=WHY,
            obligation=obligation.obligation_id,
            digest=obligation.digest,
            behaviour="Z",
            contract_digest=REDUCED_RATE.digest,
            story_revision=await world.revision("S2"),
            basis=await world.basis("S2"),
        )


async def test_a_proposal_is_approved_or_sent_back_and_records_what_it_was_made_on(
    world: Superseding,
) -> None:
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    gate = await world.propose()
    obligation = await world.obligation_of("B1")

    assert await world.column("SELECT basis FROM supersessions") == [
        {
            "obligation_id": str(obligation.obligation_id),
            "obligation_digest": obligation.digest,
            "contract_digest": REDUCED_RATE.digest,
            "story_revision": await world.revision("S2"),
            "decisions": await world.basis("S2"),
        }
    ]
    with pytest.raises(ActionRefused):
        await world.decide(gate, "reject", reason="no")
    assert await world.column("SELECT state FROM supersessions") == ["proposed"]


async def test_trunk_moved_by_someone_else_under_an_excused_push_leaves_the_authority_standing(
    world: Superseding,
) -> None:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)  # something to run
    await z_reviewed(world)
    await world.decide(await world.propose())
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "Z", "first"))
    await held_at_the_barrier(running, barrier)

    elsewhere = world.advance_trunk({"notes.txt": "someone else was here\n"})
    barrier.release.set()

    assert (await running).outcome is Outcome.CONFLICTED
    assert world.trunk() == elsewhere
    assert await world.column("SELECT state FROM supersessions WHERE intent_id IS NULL") == [
        "standing"
    ]
    assert (await world.obligation_of("B1")).state == "active"


@pytest.mark.parametrize(
    "misread", [{"contract_digest": CONTRACT.digest}, {"story_revision": 7}, {"basis": 7}]
)
async def test_a_proposal_names_the_replacement_as_its_author_read_it(
    world: Superseding, misread: dict[str, Any]
) -> None:
    """3AII-S3-II-1.5. A proposal that arrives after the replacement changed is not a
    proposal about what it has become."""
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")

    with pytest.raises(ActionRefused):
        await world.supersessions.propose(
            VERIFIER, PROJECT, reason=WHY, **{**await world.as_read(), **misread}
        )

    assert await world.stories.inbox(PROJECT) == []


async def test_checks_that_ran_across_an_approval_do_not_let_the_replacement_in_without_it(
    world: Superseding,
) -> None:
    """3AII-S3-II-1.1. An independent check asks exactly what B1 asks, so excusing Z from
    B1 changes no check that runs. The batch must still not decide under an authority
    it did not start with: Z would be on trunk with B1 never retired."""
    await world.integration.require_check(PROJECT, CONTRACT.as_check())
    base = await world.awaiting_builder("Z", GREETING, "S2")
    # Proposed once its candidate was already built and reviewed: nothing is built for
    # a behaviour whose replacement the Product Owner has yet to decide.
    await world.reviewed("Z", {"greeting.txt": "hello\n"}, on=base)
    gate = await world.propose()
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "Z", "first"))
    await held_at_the_barrier(running, barrier)

    assert (await world.decide(gate)).disposition == "effective"
    barrier.release.set()

    assert (await running).outcome is Outcome.SUPERSEDED
    assert world.trunk() == base
    assert (await world.integrate("Z")).outcome is Outcome.INTEGRATED
    assert (await world.obligation_of("B1")).state == "retired"
    assert await world.column("SELECT state FROM supersessions") == ["used"]
    assert (await world.events()).count("obligation.retired") == 1


async def test_a_replacement_already_being_pushed_without_it_is_not_proposed(
    world: Superseding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S3-II-1.4, the push first."""
    base = await world.awaiting_builder("Z", GREETING, "S2")
    await world.reviewed("Z", {"greeting.txt": "hello\n"}, on=base)

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", unreachable)
        assert (await world.integrate("Z")).outcome is Outcome.PENDING

        with pytest.raises(ActionRefused, match="integration"):
            await world.propose()

    assert await world.stories.inbox(PROJECT) == []


async def test_an_ordinary_push_of_the_replacement_ends_its_proposal_there_and_then(
    world: Superseding,
) -> None:
    """3AII-S3-II-1.4, the proposal first. The push is refused and the behaviour is
    parked before anyone looks at the proposal again: it has ended all the same."""
    base = await world.awaiting_builder("Z", GREETING, "S2")
    # Proposed once its candidate was already built and reviewed: nothing is built for
    # a behaviour whose replacement the Product Owner has yet to decide.
    await world.reviewed("Z", {"greeting.txt": "hello\n"}, on=base)
    gate = await world.propose()
    world.hook_flag("refuse").touch()

    assert (await world.integrate("Z")).outcome is Outcome.REJECTED

    assert await world.column("SELECT state FROM supersessions") == ["void"]
    assert await world.stories.inbox(PROJECT) == []
    assert (await world.decide(gate)).disposition == "stale"
    assert await world.column("SELECT state FROM supersessions") == ["void"]


async def test_replacing_a_behaviour_of_an_approved_story_leaves_the_current_storys_seal(
    world: Superseding,
) -> None:
    """3AII-S3-II-1.7. What shows story 2's criterion is Z before and after."""
    candidate = await z_reviewed(world)
    await world.decide(await world.propose())
    await world.seal("S2")
    await world.assess("Z shows the reduced rate", "S2")
    sealed = await world.stories.seal_of(PROJECT, "S2")

    assert (await world.integrate("Z")).outcome is Outcome.INTEGRATED

    assert world.trunk() == candidate
    assert await world.stories.seal_of(PROJECT, "S2") == sealed
    assert await world.story("S2") is StoryState.AWAITING_APPROVAL


@pytest.mark.parametrize("ended_by", ["rejecting its story", "cancelling its feature"])
async def test_ending_the_replacements_story_ends_its_proposal_there_and_then(
    world: Superseding, ended_by: str
) -> None:
    """3AII-S3-II-2.1. Nothing is left in the inbox asking for a decision that can no
    longer matter, and a later withdrawal does not rewrite why the proposal ended."""
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    gate = await world.propose()

    if ended_by == "rejecting its story":
        await reject(world, "S2")
    else:
        await world.close("cancel", reason="dropped")

    assert "supersession" not in [item.kind for item in await world.stories.inbox(PROJECT)]
    ended = await world.column("SELECT state || ': ' || ended_because FROM supersessions")
    assert ended == ["void: the replacing behaviour's story or feature has ended"]
    with pytest.raises(ActionRefused, match="no such proposal waiting"):
        await world.supersessions.withdraw(VERIFIER, PROJECT, gate)
    assert await world.column("SELECT state || ': ' || ended_because FROM supersessions") == ended


async def test_a_decision_ends_a_proposal_made_under_the_ones_before_it(
    world: Superseding,
) -> None:
    """3AII-S3-II-3.1. Read straight after the answer, before anything else looks."""
    await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    gate = await world.propose()

    await world.answer(await world.ask("S2"))

    ended = await world.column("SELECT state || ': ' || ended_because FROM supersessions")
    assert ended == ["void: what it was proposed on has changed"]
    assert "supersession" not in [item.kind for item in await world.stories.inbox(PROJECT)]
    with pytest.raises(ActionRefused, match="no such proposal waiting"):
        await world.supersessions.withdraw(VERIFIER, PROJECT, gate)
    assert await world.column("SELECT state || ': ' || ended_because FROM supersessions") == ended
