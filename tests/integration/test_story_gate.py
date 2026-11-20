"""Scenarios 8 and 14: when every behaviour of a story is on trunk, Relay stops and asks
the Product Owner, shows exactly what was done, and starts the next story only on a
person's approval of that exact review. A restart changes none of it."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.application import integration as integration_module
from relay.application import stories as stories_module
from relay.application.integration import Outcome
from relay.application.reviews import Review
from relay.application.stories import Stories, StoryDraft
from relay.domain.behaviour import BehaviourState
from relay.domain.review import Carried
from relay.domain.story import ActionRefused, HumanAction, Receipt, SealRefused, StoryState
from relay.persistence.outbox import OutboxPublisher
from relay.transport.streams import Streams
from tests.integration.fixtures import (
    ANALYST,
    CONTRACT,
    PRODUCT_OWNER,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
)
from tests.integration.story_world import (
    GREETS,
    INJECTION,
    STANDARD_RATE,
    STYLE,
    StoryWorld,
    at_the_story_gate,
)
from tests.integration.trunk_world import GREETING, PROJECT


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> StoryWorld:
    world = StoryWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def test_nothing_is_released_until_a_person_approves_the_roadmap(world: StoryWorld) -> None:
    gate = await world.roadmap(STANDARD_RATE, GREETS)

    assert await world.story("S1") is StoryState.PLANNED
    with pytest.raises(ActionRefused, match="planned"):
        await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    [waiting] = await world.stories.inbox(PROJECT)
    assert (waiting.gate_id, waiting.kind) == (gate, "roadmap")
    assert [story["story_id"] for story in waiting.manifest["stories"]] == ["S1", "S2"]

    receipt = await world.decide(gate)

    assert receipt == Receipt("effective", "approved")
    assert (await world.story("S1"), await world.story("S2")) == (
        StoryState.RELEASED,
        StoryState.PLANNED,
    )
    assert await world.stories.inbox(PROJECT) == []


async def test_a_delivered_story_stops_at_its_gate_and_survives_a_restart_with_an_empty_broker(
    world: StoryWorld, broker: redis.Redis, namespace: str
) -> None:
    gate, candidate = await at_the_story_gate(world)
    [shown] = await world.stories.inbox(PROJECT)
    executions = await world.column("SELECT count(*) FROM executions")
    await OutboxPublisher(world.database, Streams(broker, prefix=namespace)).publish_pending()

    assert await world.story("S1") is StoryState.AWAITING_APPROVAL
    assert await world.story("S2") is StoryState.PLANNED
    assert world.trunk() == candidate
    [delivered] = shown.manifest["behaviours"]
    assert delivered["integrated_revision"] == candidate
    assert delivered["demonstrates"] == ["AC1"]
    assert delivered["red_execution_id"] and delivered["green_execution_id"]
    assert shown.manifest["assessment"]["statement"].startswith("B1 shows 120")
    assert shown.manifest["usage"] == "unknown"
    with pytest.raises(ActionRefused, match="awaiting approval"):
        await world.behaviours.propose(PROJECT, "S1", "B2", "one more", ["AC1"])
    with pytest.raises(ActionRefused, match="planned"):
        await world.behaviours.propose(PROJECT, "S2", "B2", "greeting", ["AC1"])

    for key in [key async for key in broker.scan_iter(f"{namespace}:*")]:
        await broker.delete(key)
    restarted = Stories(world.database)
    [after] = await restarted.inbox(PROJECT)

    assert (after.gate_id, after.manifest_digest) == (gate, shown.manifest_digest)
    assert after.manifest == shown.manifest
    assert await world.column("SELECT count(*) FROM executions") == executions

    approval = HumanAction(PRODUCT_OWNER, "the-approval", str(gate), "approve")
    first = await restarted.decide(approval)
    again = await restarted.decide(approval)

    assert first == again == Receipt("effective", "approved")
    second_thoughts = await restarted.decide(
        HumanAction(PRODUCT_OWNER, "another-request", str(gate), "approve")
    )
    assert second_thoughts.disposition == "stale"
    assert await world.story("S1") is StoryState.APPROVED
    assert await world.story("S2") is StoryState.RELEASED
    assert (await world.events()).count("story.released") == 2
    assert await restarted.inbox(PROJECT) == []
    await world.behaviours.propose(PROJECT, "S2", "B2", "greeting", ["AC1"])


async def test_the_gate_opens_whichever_comes_last_the_seal_the_assessment_or_the_integration(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    await world.seal()
    await world.assess("it will show 120 for 100")
    assert await world.stories.inbox(PROJECT) == []

    await world.integrate("B1")

    assert await world.story("S1") is StoryState.AWAITING_APPROVAL
    assert (await world.events())[-2:] == ["behaviour.integrated", "story.gate_opened"]


async def test_a_list_that_leaves_an_acceptance_criterion_out_cannot_be_sealed(
    world: StoryWorld,
) -> None:
    two_criteria = StoryDraft("S1", "Rates", "prices are right", {"AC1": "standard", "AC2": "zero"})
    await world.decide(await world.roadmap(two_criteria))
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])

    with pytest.raises(SealRefused, match="AC2"):
        await world.seal()
    with pytest.raises(ActionRefused, match="no acceptance criteria"):
        await world.behaviours.propose(PROJECT, "S1", "B2", "made up", ["AC9"])


async def test_a_behaviour_added_after_the_seal_keeps_the_gate_shut_until_sealed_again(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.deliver("B1")
    await world.seal()
    await world.behaviours.propose(PROJECT, "S1", "B2", "one more", ["AC1"])

    with pytest.raises(ActionRefused, match="sealed"):
        await world.assess("B1 is enough")

    assert await world.story("S1") is StoryState.RELEASED
    assert await world.stories.inbox(PROJECT) == []
    await world.seal()
    await world.assess("B1 and B2 together")
    assert await world.stories.inbox(PROJECT) == [], "B2 is not on trunk yet"


async def test_a_behaviour_waits_for_the_one_before_it_to_reach_trunk(world: StoryWorld) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    await world.behaviours.propose(PROJECT, "S1", "B2", "one more", ["AC1"])

    with pytest.raises(ActionRefused, match="waits for B1"):
        await world.behaviours.begin_test_design(PROJECT, "B2")

    assert await world.behaviours.state_of(PROJECT, "B2") is BehaviourState.PROPOSED
    await world.behaviours.begin_test_design(PROJECT, "B1")


async def test_a_second_feature_waits_for_the_project_and_gets_its_turn_without_a_restart(
    world: StoryWorld,
) -> None:
    first = await world.roadmap(STANDARD_RATE)
    second = await world.roadmap(GREETS, feature="F2")
    await world.decide(first)
    await world.decide(second)
    assert (await world.story("S1"), await world.story("S2")) == (
        StoryState.RELEASED,
        StoryState.PLANNED,
    )
    await world.deliver("B1")
    await world.seal()
    await world.assess("done")
    assert await world.story("S2") is StoryState.PLANNED

    await world.decide(await world.gate())

    assert await world.story("S2") is StoryState.RELEASED
    assert (await world.events())[-2:] == ["gate.approved", "story.released"]


async def test_asking_for_changes_reopens_the_same_story_and_releases_nothing(
    world: StoryWorld,
) -> None:
    gate, candidate = await at_the_story_gate(world)

    receipt = await world.decide(gate, "request_changes", reason="show the currency too")

    assert receipt == Receipt("effective", "changes_requested")
    assert await world.story("S1") is StoryState.CHANGES_REQUESTED
    assert await world.story("S2") is StoryState.PLANNED
    assert world.trunk() == candidate
    assert await world.column("SELECT revision FROM stories WHERE story_id = 'S1'") == [2]
    assert await world.stories.inbox(PROJECT) == []
    with pytest.raises(ActionRefused, match="reason"):
        await world.decide(gate, "request_changes", reason=" ")


async def test_only_a_person_decides_at_a_gate_and_each_role_does_only_its_own_part(
    world: StoryWorld,
) -> None:
    gate = await world.roadmap(STANDARD_RATE)

    for worker in (ANALYST, VERIFIER):
        with pytest.raises(ActionRefused, match="human"):
            await world.stories.decide(HumanAction(worker, "r", str(gate), "approve"))
    with pytest.raises(ActionRefused, match="human"):
        await world.stories.open_feature(ANALYST, PROJECT, "F9", "a problem of its own making")
    with pytest.raises(ActionRefused, match="analyst"):
        await world.stories.publish_roadmap(PRODUCT_OWNER, PROJECT, "F1", [GREETS])
    await world.decide(gate)
    with pytest.raises(ActionRefused, match="verifier"):
        await world.stories.seal(ANALYST, PROJECT, "S1", story_revision=1, list_revision=0)
    with pytest.raises(ActionRefused, match="analyst"):
        await world.stories.assess(
            VERIFIER, PROJECT, "S1", "looks fine to me", story_revision=1, list_revision=0
        )

    assert await world.story("S1") is StoryState.RELEASED


async def test_a_request_id_reused_for_another_gate_acknowledges_nothing(
    world: StoryWorld,
) -> None:
    first = await world.roadmap(STANDARD_RATE)
    second = await world.roadmap(GREETS, feature="F2")
    await world.stories.decide(HumanAction(PRODUCT_OWNER, "one", str(first), "approve"))

    with pytest.raises(ActionRefused, match="already used"):
        await world.stories.decide(
            HumanAction(PRODUCT_OWNER, "one", str(second), "request_changes", {"reason": "no"})
        )

    assert await world.column(f"SELECT state FROM gates WHERE gate_id = '{second}'") == ["open"]
    assert await world.column("SELECT count(*) FROM human_actions") == [1]


async def test_an_action_that_arrives_after_the_gate_was_decided_is_stale(
    world: StoryWorld,
) -> None:
    gate = await world.roadmap(STANDARD_RATE)
    await world.decide(gate, "request_changes", reason="split it")

    late = await world.decide(gate)

    assert late.disposition == "stale"
    assert await world.story("S1") is StoryState.PLANNED
    assert await world.column("SELECT state FROM roadmaps") == ["superseded"]


async def test_of_an_approval_and_a_change_request_at_once_exactly_one_takes_effect(
    world: StoryWorld,
) -> None:
    gate = await world.roadmap(STANDARD_RATE)

    receipts = await asyncio.gather(
        world.stories.decide(HumanAction(PRODUCT_OWNER, "a", str(gate), "approve")),
        world.stories.decide(
            HumanAction(PRODUCT_OWNER, "b", str(gate), "request_changes", {"reason": "no"})
        ),
    )

    assert sorted(receipt.disposition for receipt in receipts) == ["effective", "stale"]
    [effective] = [receipt for receipt in receipts if receipt.disposition == "effective"]
    released = await world.story("S1") is StoryState.RELEASED
    assert released == (effective.outcome == "approved")


async def test_a_gate_whose_basis_has_changed_behind_it_approves_nothing(
    world: StoryWorld,
) -> None:
    """No path in this step changes a story at its gate, so the change is written
    directly. What is under test is that a decision looks again at what it decides on."""
    gate, _ = await at_the_story_gate(world)
    async with world.database.connection() as connection:
        await connection.execute("UPDATE stories SET list_revision = list_revision + 1")

    receipt = await world.decide(gate)

    assert receipt.disposition == "stale"
    assert await world.story("S2") is StoryState.PLANNED
    assert await world.column(f"SELECT state FROM gates WHERE gate_id = '{gate}'") == ["superseded"]
    assert await world.stories.inbox(PROJECT) == []


async def test_a_gate_that_does_not_exist_cannot_be_decided(world: StoryWorld) -> None:
    for target in ("not-a-gate", str(uuid.uuid4())):
        with pytest.raises(ActionRefused, match="no such gate"):
            await world.stories.decide(HumanAction(PRODUCT_OWNER, "r", target, "approve"))
    with pytest.raises(ActionRefused, match="not something"):
        await world.stories.decide(HumanAction(PRODUCT_OWNER, "r", str(uuid.uuid4()), "merge"))


async def test_nothing_is_started_on_a_behaviour_whose_story_is_not_being_worked_on(
    world: StoryWorld,
) -> None:
    """Written directly: by the real route a story reaches its gate only when all its
    behaviours are on trunk, so none is left to start anything on."""
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    async with world.database.connection() as connection:
        await connection.execute("UPDATE stories SET state = 'awaiting_approval'")

    for start in (
        world.behaviours.begin_test_design(PROJECT, "B1"),
        world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT),
        world.behaviours.run_red(PROJECT, "B1"),
        world.behaviours.run_green(PROJECT, "B1", 1),
        world.integrate("B1"),
    ):
        with pytest.raises(ActionRefused, match="awaiting approval"):
            await start

    assert await world.behaviours.state_of(PROJECT, "B1") is BehaviourState.PROPOSED


async def test_a_feature_has_one_roadmap_and_a_sealed_list_one_assessment(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    with pytest.raises(ActionRefused, match="already has a roadmap"):
        await world.stories.publish_roadmap(ANALYST, PROJECT, "F1", [GREETS])
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    await world.seal()
    await world.assess("it will do")

    with pytest.raises(ActionRefused, match="already been assessed"):
        await world.assess("on second thoughts, no")

    assert await world.column("SELECT statement FROM story_assessments") == ["it will do"]


async def test_an_assessment_of_an_earlier_list_does_not_open_the_gate_for_a_later_one(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    await world.seal()
    await world.assess("B1 alone will do")
    await world.behaviours.propose(PROJECT, "S1", "B2", "and a greeting", ["AC1"])
    for behaviour, contract, files in (
        ("B1", CONTRACT, None),
        ("B2", GREETING, {"greeting.txt": "hello\n"}),
    ):
        await world.behaviours.begin_test_design(PROJECT, behaviour)
        await world.behaviours.register_acceptance_test(PROJECT, behaviour, contract)
        assert (await world.behaviours.run_red(PROJECT, behaviour)).is_valid
        await world.reviewed(behaviour, files, on=world.trunk())
        assert (await world.integrate(behaviour)).outcome is Outcome.INTEGRATED

    assert await world.story("S1") is StoryState.RELEASED
    assert await world.stories.inbox(PROJECT) == []


async def test_a_roadmap_nobody_has_approved_releases_nothing_when_another_gate_is_decided(
    world: StoryWorld,
) -> None:
    first = await world.roadmap(STANDARD_RATE)
    await world.roadmap(GREETS, feature="F2")

    await world.decide(first, "request_changes", reason="split it")

    assert (await world.story("S1"), await world.story("S2")) == (
        StoryState.PLANNED,
        StoryState.PLANNED,
    )


async def test_a_seal_or_an_assessment_of_a_list_that_has_moved_on_is_refused(
    world: StoryWorld,
) -> None:
    """The Analyst read the first sealed list. Its verdict must not land on the second."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    read_by_the_verifier = await world.stories.revisions_of(PROJECT, "S1")
    await world.seal()
    read_by_the_analyst = await world.stories.revisions_of(PROJECT, "S1")
    await world.behaviours.propose(PROJECT, "S1", "B2", "and a greeting", ["AC1"])

    with pytest.raises(ActionRefused, match="moved on"):
        await world.stories.seal(
            VERIFIER,
            PROJECT,
            "S1",
            story_revision=read_by_the_verifier[0],
            list_revision=read_by_the_verifier[1],
        )
    await world.seal()
    with pytest.raises(ActionRefused, match="moved on"):
        await world.stories.assess(
            ANALYST,
            PROJECT,
            "S1",
            "B1 alone will do",
            story_revision=read_by_the_analyst[0],
            list_revision=read_by_the_analyst[1],
        )

    assert await world.column("SELECT count(*) FROM story_assessments") == [0]
    assert await world.column("SELECT sealed_revision FROM stories WHERE story_id = 'S1'") == [2]


class ChangesBetweenReadings(list[StoryDraft]):
    """Gives its stories the first time it is read and nothing after that."""

    def __init__(self, stories: list[StoryDraft]) -> None:
        super().__init__(stories)
        self._read = False

    def __iter__(self) -> Any:
        stories = [] if self._read else list(super().__iter__())
        self._read = True
        return iter(stories)


async def test_the_roadmap_a_person_is_shown_is_the_roadmap_that_is_released(
    world: StoryWorld,
) -> None:
    criteria = {"AC1": "100 net is shown as 120"}
    draft = StoryDraft("S1", "Standard rate", "a gross price is shown", criteria)
    criteria["AC2"] = "slipped in afterwards"
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")

    await world.stories.publish_roadmap(
        ANALYST, PROJECT, "F1", ChangesBetweenReadings([draft, GREETS])
    )

    [shown] = await world.stories.inbox(PROJECT)
    assert [story["story_id"] for story in shown.manifest["stories"]] == await world.column(
        "SELECT story_id FROM stories ORDER BY position"
    )
    assert shown.manifest["stories"][0]["criteria"] == {"AC1": "100 net is shown as 120"}
    assert await world.column("SELECT criteria FROM stories WHERE story_id = 'S1'") == [
        {"AC1": "100 net is shown as 120"}
    ]


async def blocker_fixed_on_a_second_candidate(world: StoryWorld) -> str:
    """Sentinel blocks the first candidate and verifies the fix on the second, which is
    integrated. QA leaves a minor finding open. Returns the minor finding's id."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base, reviews={"sentinel": Review(findings=(INJECTION,))})
    [blocker] = await world.column("SELECT finding_id FROM findings")
    await world.reviewed(
        "B1",
        {"vat.py": VAT_APPLIES_STANDARD_RATE + "# parsed as an integer\n"},
        on=base,
        reviews={
            "sentinel": Review(carried={str(blocker): Carried.VERIFIED_FIXED}),
            "qa": Review(findings=(STYLE,)),
        },
    )
    assert (await world.integrate("B1")).outcome is Outcome.INTEGRATED
    await world.seal()
    await world.assess("B1 shows 120 for 100")
    [minor] = await world.column("SELECT finding_id FROM findings WHERE severity = 1")
    return str(minor)


async def test_the_review_says_how_each_finding_was_settled_on_what_reached_trunk(
    world: StoryWorld,
) -> None:
    await blocker_fixed_on_a_second_candidate(world)

    [shown] = await Stories(world.database).inbox(PROJECT)
    [delivered] = shown.manifest["behaviours"]
    settled = {finding["role"]: finding["settled"] for finding in delivered["findings"]}

    assert settled == {
        "sentinel": "sentinel verified the fix on the integrated candidate",
        "qa": "below the severity that blocks; left open",
    }
    assert delivered["contract_digest"] == CONTRACT.digest


async def test_a_finding_cannot_change_behind_the_review_a_person_is_looking_at(
    world: StoryWorld,
) -> None:
    minor = await blocker_fixed_on_a_second_candidate(world)

    with pytest.raises(ActionRefused, match="awaiting approval"):
        await world.reviews.dispute(PROJECT, minor, "it is fine as it is")

    assert await world.column("SELECT status FROM findings WHERE severity = 1") == ["open"]
    assert (await world.decide(await world.gate())).disposition == "effective"


@pytest.mark.parametrize(
    ("action", "payload", "refusal"),
    [
        ("approve", {"manifest_digest": "sha256:another-review"}, "only that"),
        ("approve", {"by": "someone-else"}, "only that"),
        ("request_changes", {"reason": "split it", "gate_id": "another"}, "only that"),
        ("request_changes", {}, "only that"),
        ("request_changes", {"reason": " "}, "stated reason"),
    ],
)
async def test_an_action_carries_only_what_that_action_has(
    world: StoryWorld, action: str, payload: dict[str, str], refusal: str
) -> None:
    gate = await world.roadmap(STANDARD_RATE)

    with pytest.raises(ActionRefused, match=refusal):
        await world.stories.decide(HumanAction(PRODUCT_OWNER, "r", str(gate), action, payload))

    assert await world.column("SELECT state FROM gates") == ["open"]


async def test_what_a_person_said_is_recorded_apart_from_what_relay_records_about_it(
    world: StoryWorld,
) -> None:
    gate = await world.roadmap(STANDARD_RATE)

    await world.decide(gate, "request_changes", reason="split it")

    [decided] = await world.column(
        "SELECT payload FROM events WHERE event_type = 'gate.changes_requested'"
    )
    assert isinstance(decided, dict)
    assert decided["said"] == {"reason": "split it"}
    assert (decided["gate_id"], decided["by"]) == (str(gate), "product-owner")
    [shown] = await world.column("SELECT manifest_digest FROM gates")
    assert decided["manifest_digest"] == shown


async def test_after_a_change_request_the_old_seal_does_not_carry_over(
    world: StoryWorld,
) -> None:
    gate, _ = await at_the_story_gate(world)
    await world.decide(gate, "request_changes", reason="show the currency too")

    with pytest.raises(ActionRefused, match="sealed"):
        await world.assess("nothing needs to change")

    assert await world.story("S1") is StoryState.CHANGES_REQUESTED
    assert await world.stories.inbox(PROJECT) == []


async def test_a_request_id_recorded_meanwhile_by_another_request_undoes_this_one(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests under one id in different projects do not wait for each other, so
    each can find no receipt when it looks. The receipt's own key settles it."""
    first = await world.roadmap(STANDARD_RATE)
    second = await world.roadmap(GREETS, feature="F2")
    await world.stories.decide(HumanAction(PRODUCT_OWNER, "one", str(first), "approve"))

    async def nothing_recorded_yet(*_: Any) -> None:
        return None

    monkeypatch.setattr(stories_module, "_recorded", nothing_recorded_yet)

    with pytest.raises(ActionRefused, match="already used"):
        await world.stories.decide(HumanAction(PRODUCT_OWNER, "one", str(second), "approve"))

    assert await world.column(f"SELECT state FROM gates WHERE gate_id = '{second}'") == ["open"]
    assert await world.story("S2") is StoryState.PLANNED


async def test_the_last_integration_and_an_assessment_arriving_together_both_finish(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration writes to the project (the ledger) and needs the story; an assessment
    holds the story and writes to the project. Taken in opposite orders they deadlock."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    await world.seal()
    recorded, go_on = asyncio.Event(), asyncio.Event()
    real = integration_module.evaluate_story_gate

    async def held_after_recording_the_integration(*arguments: Any) -> None:
        recorded.set()
        await go_on.wait()
        await real(*arguments)

    monkeypatch.setattr(
        integration_module, "evaluate_story_gate", held_after_recording_the_integration
    )
    integrating = asyncio.create_task(world.integrate("B1"))
    await recorded.wait()
    assessing = asyncio.create_task(world.assess("B1 shows 120 for 100"))
    await world.until_someone_waits_for_a_lock()
    go_on.set()

    assert (await integrating).outcome is Outcome.INTEGRATED
    await assessing
    assert await world.story("S1") is StoryState.AWAITING_APPROVAL
    assert (await world.events()).count("story.gate_opened") == 1


async def test_a_replayed_roadmap_approval_does_not_deadlock_the_release_of_that_roadmap(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approving feature 1's last story releases feature 2's first, reaching into feature
    2 under the project lock, while a replay of feature 2's own approval arrives."""
    first = await world.roadmap(STANDARD_RATE)
    second = await world.roadmap(GREETS, feature="F2")
    await world.decide(first)
    approval_of_second = HumanAction(PRODUCT_OWNER, "approve-second", str(second), "approve")
    original = await world.stories.decide(approval_of_second)
    await world.deliver("B1")
    await world.seal()
    await world.assess("done")
    holding, go_on = asyncio.Event(), asyncio.Event()
    real = stories_module._locked_project

    async def held_with_the_project(*arguments: Any) -> None:
        await real(*arguments)
        if not holding.is_set():
            holding.set()
            await go_on.wait()

    monkeypatch.setattr(stories_module, "_locked_project", held_with_the_project)
    approving = asyncio.create_task(world.decide(await world.gate()))
    await holding.wait()
    replaying = asyncio.create_task(world.stories.decide(approval_of_second))
    await world.until_someone_waits_for_a_lock()
    go_on.set()

    assert (await approving).disposition == "effective"
    assert await replaying == original
    assert await world.story("S2") is StoryState.RELEASED
    assert (await world.events()).count("story.released") == 2


async def test_a_behaviour_that_demonstrates_nothing_is_refused_when_proposed(
    world: StoryWorld,
) -> None:
    """Accepted, it could reach trunk and then keep its story from ever being sealed."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    before = await world.stories.revisions_of(PROJECT, "S1")

    with pytest.raises(ActionRefused, match="at least one acceptance criterion"):
        await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", [])

    assert await world.stories.revisions_of(PROJECT, "S1") == before
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    await world.seal()


class OneThingThenAnother(list[str]):
    """Says AC1 the first time it is read and AC2 every time after."""

    def __init__(self) -> None:
        super().__init__(["AC1"])
        self._read = False

    def __iter__(self) -> Any:
        said = ["AC2"] if self._read else ["AC1"]
        self._read = True
        return iter(said)


async def test_a_proposal_records_the_same_criteria_in_the_behaviour_and_in_the_ledger(
    world: StoryWorld,
) -> None:
    two_criteria = StoryDraft("S1", "Rates", "prices are right", {"AC1": "standard", "AC2": "zero"})
    await world.decide(await world.roadmap(two_criteria))

    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", OneThingThenAnother())

    [stored] = await world.column("SELECT demonstrates FROM behaviours")
    [proposed] = await world.column(
        "SELECT payload->'demonstrates' FROM events WHERE event_type = 'behaviour.proposed'"
    )
    assert stored == proposed == ["AC1"]
