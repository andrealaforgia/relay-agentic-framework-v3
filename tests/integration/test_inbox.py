"""Everything waiting for the Product Owner, in one typed list, and what each item is
bound to when it is decided."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.inbox import FINAL_CHECK, GATE, PARKED_CALL, QUESTION, REFUSED_PUSH, Inbox
from relay.application.integration import IntegrationRefused, Outcome
from relay.application.stories import closure_in
from relay.domain.behaviour import BehaviourState
from relay.domain.story import ActionRefused, HumanAction, Receipt
from tests.integration.completion_world import CompletionWorld
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import PRODUCT_OWNER
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_completion_failure import failed_on_trunk
from tests.integration.test_invocations import Calling, said
from tests.integration.trunk_world import PROJECT


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> DecisionWorld:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


@pytest.fixture
async def calling(world: DecisionWorld) -> AsyncIterator[Calling]:
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    calling = Calling(world)
    yield calling
    await calling.close()


async def parked_call(calling: Calling) -> None:
    """Three calls that gave nothing: the work is stopped and a person is asked."""
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    for _ in range(3):
        admission = await calling.invocations.admit(
            worker, assignment, grant, await calling.prepared()
        )
        assert admission.invocation_id is not None
        await calling.invocations.record(admission.invocation_id, said(), problem="unusable")


def by(request: str, target: str, action: str, **payload: str) -> HumanAction:
    return HumanAction(PRODUCT_OWNER, request, target, action, payload)


async def test_a_gate_and_a_question_are_items_with_what_there_is_to_see_and_to_do(
    world: DecisionWorld,
) -> None:
    inbox = Inbox(world.database)
    assert await inbox.items(PROJECT) == []

    gate = await world.roadmap(STANDARD_RATE)

    [roadmap] = await inbox.items(PROJECT)
    assert (roadmap.item_id, roadmap.kind) == (str(gate), GATE)
    assert roadmap.actions == ("approve", "request-changes")
    assert roadmap.about == "the roadmap of F1" and roadmap.basis.startswith("sha256:")
    assert roadmap.evidence["gate"] == "roadmap"
    # Found by the start of its identity, if that is one item's only.
    assert await inbox.item(PROJECT, str(gate)[:8]) == roadmap
    assert await inbox.item(PROJECT, "0") is None and await inbox.item(PROJECT, "nonesuch") is None
    await world.decide(gate)
    assert await inbox.items(PROJECT) == []

    asked = await world.ask()

    [question] = await inbox.items(PROJECT)
    assert (question.item_id, question.kind, question.actions) == (
        str(asked),
        QUESTION,
        ("answer",),
    )
    assert {"text", "why", "options", "asked_by"} <= set(question.evidence)
    await world.answer(asked)
    assert await inbox.items(PROJECT) == []


async def test_a_story_at_its_gate_can_also_be_rejected_and_a_failed_final_check_retried(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    inbox = Inbox(world.database)

    failed = await failed_on_trunk(world)

    [item] = await inbox.items(PROJECT)
    assert (item.item_id, item.kind) == (str(failed.gate_id), FINAL_CHECK)
    assert item.actions == ("retry", "request-changes")
    assert item.about == "the final check of F1 failed"
    assert [each["satisfied"] for each in item.evidence["checked"]] == [True, False]
    assert (await world.decide(failed.gate_id, "retry")).disposition == "effective"
    [again] = await inbox.items(PROJECT)  # it failed again, and is another gate
    assert again.kind == FINAL_CHECK and again.item_id != item.item_id


async def test_a_parked_call_set_aside_is_no_longer_a_question_and_a_retry_of_it_is_stale(
    calling: Calling,
) -> None:
    """3C-3.5: setting a call aside closes the item and leaves the work parked. A retry
    prepared while the item was open answers a question that has been closed: nothing
    is authorised by it."""
    inbox = Inbox(calling.world.database)
    await parked_call(calling)

    [item] = await inbox.items(PROJECT)
    assert item.kind == PARKED_CALL and item.actions == ("retry", "set-aside")
    assert item.evidence["calls_launched"] == 3 and item.evidence["role"] == "builder"
    assert item.evidence["why_parked"] == "3 calls gave nothing that could be used"
    assert item.evidence["work"] == "build of B1"

    aside = await calling.invocations.decide(by("r-1", item.item_id, "set_aside"))
    late = await calling.invocations.decide(by("r-2", item.item_id, "retry"))

    assert aside == Receipt("effective", "set_aside")
    assert late.disposition == "stale" and "already decided" in late.outcome
    assert await inbox.items(PROJECT) == []
    _, parked, _ = await calling.assignment()
    assert parked is not None  # the work is stopped still, and no call was allowed
    with pytest.raises(ActionRefused, match="parked"):
        await calling.claimed(await calling.worker())


async def test_a_parked_call_and_a_refused_push_are_each_decided_from_the_inbox_alone(
    world: DecisionWorld,
) -> None:
    """3C-1.8: two things waiting at once, read by services that knew nothing of how
    they came about, as after a restart. Each is decided by its item, and deciding one
    leaves the other as it was."""
    await world.decide(await world.roadmap(STANDARD_RATE))
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    world.hook_flag("refuse").touch()
    assert (await world.integrate("B1")).outcome is Outcome.REJECTED
    # And the plan of a second feature, stopped on three calls that gave nothing.
    await world.stories.start_feature(PROJECT, by("r-0", "", "start", problem="a second rate"))
    [plan] = await world.column(
        "SELECT assignment_id FROM assignments WHERE kind = 'plan' AND feature_id = 'F2'"
    )
    calling = Calling(world)
    try:
        analyst = await calling.worker("analyst")
        grant = await world.claims.claim(analyst.principal, plan, analyst.worker_id)  # type: ignore[arg-type]
        for _ in range(3):
            prepared = dataclasses.replace(
                await calling.prepared(),
                read_on=await calling.invocations.as_it_stands(plan),  # type: ignore[arg-type]
            )
            admission = await calling.invocations.admit(analyst, plan, grant, prepared)  # type: ignore[arg-type]
            assert admission.invocation_id is not None
            await calling.invocations.record(admission.invocation_id, said(), problem="unusable")
        inbox = Inbox(world.database)  # nothing carried over but the database

        waiting = {each.kind: each for each in await inbox.items(PROJECT)}

        assert set(waiting) == {PARKED_CALL, REFUSED_PUSH}
        assert waiting[PARKED_CALL].evidence["work"] == "plan of F2"
        push = waiting[REFUSED_PUSH]
        assert push.actions == ("retry",) and push.evidence["behaviour"] == "B1"
        assert "declined" in push.evidence["what_the_repository_said"]
        assert push.evidence["sends_made"] == 1

        world.hook_flag("refuse").unlink()
        await world.integration.retry_integration(
            PROJECT, "B1", "the hook was put right", "r-1", uuid.UUID(push.item_id)
        )

        assert [each.kind for each in await inbox.items(PROJECT)] == [PARKED_CALL]
        assert await world.state("B1") is BehaviourState.REVIEW
        retried = await calling.invocations.decide(by("r-2", waiting[PARKED_CALL].item_id, "retry"))
        assert retried == Receipt("effective", "retry_authorised")
        assert await inbox.items(PROJECT) == []
    finally:
        await calling.close()


async def test_a_retry_of_a_refused_push_answers_the_refusal_it_was_shown(
    world: DecisionWorld,
) -> None:
    """3C-1.6: retried and refused again, a behaviour has another refusal, for whatever
    reason the repository had then. A retry that names the first is stale."""
    await world.decide(await world.roadmap(STANDARD_RATE))
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    inbox = Inbox(world.database)
    world.hook_flag("refuse").touch()
    await world.integrate("B1")
    [first] = await inbox.items(PROJECT)

    await world.integration.retry_integration(
        PROJECT, "B1", "tried again", "r-1", uuid.UUID(first.item_id)
    )
    await world.integrate("B1")  # and it is refused again
    [second] = await inbox.items(PROJECT)

    assert second.kind == REFUSED_PUSH and second.item_id != first.item_id
    with pytest.raises(IntegrationRefused, match="stale"):
        await world.integration.retry_integration(
            PROJECT, "B1", "prepared against the first", "r-2", uuid.UUID(first.item_id)
        )
    assert [each.item_id for each in await inbox.items(PROJECT)] == [second.item_id]


async def test_a_resume_lifts_the_pause_it_names_and_no_later_one(world: DecisionWorld) -> None:
    """3C-2.7: each pause is a closure of its own. A resume prepared against one, with
    the feature resumed and paused again since, lifts nothing."""
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.close("pause", reason="waiting on legal")
    async with world.database.connection() as connection:
        first = await closure_in(connection, PROJECT, "F1")
    assert first is not None
    await world.close("resume")
    async with world.database.connection() as connection:
        assert await closure_in(connection, PROJECT, "F1") is None
    await world.close("pause", reason="waiting on finance")
    async with world.database.connection() as connection:
        second = await closure_in(connection, PROJECT, "F1")
    assert second is not None and second != first

    old = await world.close("lift", closure=str(first))

    assert old.disposition == "stale" and "paused again since" in old.outcome
    assert await world.close("lift", closure=str(second)) == Receipt("effective", "resumed")
    assert (await world.close("lift", closure=str(second))).disposition == "stale"  # not paused


async def test_starting_a_feature_is_a_request_with_a_receipt_that_names_it(
    world: DecisionWorld,
) -> None:
    """3C-1.7: the receipt is committed with the feature. Sent again, the request is
    given it again and starts nothing more."""
    start = by("r-1", "", "start", problem="prices need a second VAT rate")

    first = await world.stories.start_feature(PROJECT, start)
    again = await world.stories.start_feature(PROJECT, start)

    assert first == again == Receipt("effective", "started F1")
    assert await world.column("SELECT feature_id FROM features ORDER BY feature_id") == ["F1"]
    assert await world.column(
        "SELECT count(*) FROM assignments WHERE kind = 'plan' AND feature_id = 'F1'"
    ) == [1]
    with pytest.raises(ActionRefused, match="different action"):
        await world.stories.start_feature(PROJECT, by("r-1", "", "start", problem="another"))
    with pytest.raises(ActionRefused):
        await world.stories.start_feature(PROJECT, by("r-2", "", "start", problem="  "))


async def test_a_feature_is_paused_or_cancelled_only_as_it_was_when_the_person_was_shown_it(
    world: DecisionWorld,
) -> None:
    """3C-2.7, where it is decided: the command sends how the feature stood when it was
    shown, and the service compares that under the feature's lock. This is what holds a
    decision in the time a person takes over the passphrase."""
    from relay.application.stories import standing_of, transition_in

    await world.decide(await world.roadmap(STANDARD_RATE))

    async def standing() -> str:
        async with world.database.connection() as connection:
            feature = await (
                await connection.execute("SELECT state, closed_by FROM features")
            ).fetchone()
            assert feature is not None
            return standing_of(feature, await transition_in(connection, PROJECT, "F1"))

    shown = await standing()
    await world.close("pause", reason="paused by someone else meanwhile")

    cancelled = await world.close("cancel", reason="as I saw it", as_seen=shown)
    paused = await world.close("pause", reason="as I saw it", as_seen=shown)

    assert cancelled.disposition == "stale" and "no longer as it was" in cancelled.outcome
    assert paused.disposition == "stale"
    assert await world.column("SELECT closed_by FROM features") == ["paused"]
    assert await world.close("cancel", reason="as it is now", as_seen=await standing()) == Receipt(
        "effective", "cancelled"
    )


async def test_a_cancellation_is_of_the_feature_as_it_was_shown_and_not_of_one_that_looks_the_same(
    world: DecisionWorld,
) -> None:
    """3C-S2-1.5: shown open. Paused and resumed since, with its state unchanged: open as
    it was, and not as it was shown. A cancellation of what was shown is stale."""
    from relay.application.stories import standing_of, transition_in

    await world.decide(await world.roadmap(STANDARD_RATE))
    async with world.database.connection() as connection:
        feature = await (
            await connection.execute("SELECT state, closed_by FROM features")
        ).fetchone()
        assert feature is not None
        shown = standing_of(feature, await transition_in(connection, PROJECT, "F1"))
    await world.close("pause", reason="someone else's reason")
    await world.close("resume")
    async with world.database.connection() as connection:
        again = await (await connection.execute("SELECT state, closed_by FROM features")).fetchone()
        assert again == feature  # it looks the same
        now = standing_of(again, await transition_in(connection, PROJECT, "F1"))

    old = await world.close("cancel", reason="as I saw it", as_seen=shown)

    assert shown != now
    assert old.disposition == "stale" and "no longer as it was" in old.outcome
    assert await world.column("SELECT closed_by FROM features") == [None]
    assert await world.close("cancel", reason="as it is", as_seen=now) == Receipt(
        "effective", "cancelled"
    )


async def test_a_second_retry_of_one_refused_push_allows_nothing_and_has_a_receipt_saying_so(
    world: DecisionWorld,
) -> None:
    """3C-S2-1.7: two people saw the same refusal. The first retry puts the behaviour
    back in review; the second finds it is no longer blocked on that refusal."""
    await world.decide(await world.roadmap(STANDARD_RATE))
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    world.hook_flag("refuse").touch()
    await world.integrate("B1")
    [push] = await Inbox(world.database).items(PROJECT)
    intent = uuid.UUID(push.item_id)

    first = await world.integration.retry_integration(
        PROJECT,
        "B1",
        "put right",
        "r-1",
        intent,
        by("r-1", push.item_id, "retry", reason="put right"),
    )
    second = await world.integration.retry_integration(
        PROJECT, "B1", "me too", "r-2", intent, by("r-2", push.item_id, "retry", reason="me too")
    )

    assert first == Receipt("effective", "retry_allowed")
    assert second is not None and second.disposition == "stale"
    assert "already been allowed" in second.outcome
    assert await world.column(
        "SELECT count(*) FROM assignments WHERE kind = 'integrate' AND state = 'wanted'"
    ) == [1]
    assert (
        await world.integration.retry_integration(
            PROJECT,
            "B1",
            "me too",
            "r-2",
            intent,
            by("r-2", push.item_id, "retry", reason="me too"),
        )
        == second
    )  # durable: sent again, the same receipt


async def test_a_parked_call_shows_what_was_charged_estimated_and_not_known_and_is_held_to_it(
    calling: Calling,
) -> None:
    """3C-S2-1.8: calls that never started are not calls made; a charge is not an estimate;
    what is not known is counted, not left out of a sum. And a decision about the work is
    held to all of that as it was shown: a bill that turns up late for an earlier call
    changes the evidence, and a retry confirmed on the old evidence is stale."""
    world = calling.world
    await parked_call(calling)
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE invocations SET provider_cost_usd = 0.02, estimated_cost_usd = 0.03 "
            "WHERE attempt = 1"
        )
        await connection.execute(
            "UPDATE invocations SET provider_cost_usd = NULL, estimated_cost_usd = 0.05 "
            "WHERE attempt = 2"
        )
        await connection.execute(
            "UPDATE invocations SET provider_cost_usd = NULL, estimated_cost_usd = NULL "
            "WHERE attempt = 3"
        )
    inbox = Inbox(world.database)

    [item] = await inbox.items(PROJECT)

    assert item.evidence["calls_launched"] == 3
    assert item.evidence["calls_that_never_started"] == 0
    assert item.evidence["charged_usd"] == "0.02"
    assert item.evidence["estimated_usd_where_no_charge_was_reported"] == "0.05"
    assert item.evidence["calls_whose_cost_is_not_known"] == 1
    # A bill arrives late for the third call.
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE invocations SET provider_cost_usd = 0.40 WHERE attempt = 3"
        )

    late = await calling.invocations.decide(by("r-1", item.item_id, "retry", as_seen=item.basis))

    assert late.disposition == "stale" and "changed since it was shown" in late.outcome
    [now] = await inbox.items(PROJECT)
    assert now.basis != item.basis and now.evidence["charged_usd"] == "0.42"
    # A call that never started was not made: it is counted apart, and is no call launched.
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE invocations SET state = 'not_started', provider_cost_usd = NULL, "
            "estimated_cost_usd = NULL WHERE attempt = 1"
        )
    [recounted] = await inbox.items(PROJECT)
    assert recounted.evidence["calls_launched"] == 2
    assert recounted.evidence["calls_that_never_started"] == 1
    assert recounted.evidence["calls_whose_cost_is_not_known"] == 0
    assert await calling.invocations.decide(
        by("r-2", item.item_id, "retry", as_seen=recounted.basis)
    ) == Receipt("effective", "retry_authorised")


async def test_a_failed_final_check_shows_what_was_expected_and_what_was_observed(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    """3C-S2-1.9: what a person weighs between checking again and changing the roadmap."""
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await failed_on_trunk(world)

    [item] = await Inbox(world.database).items(PROJECT)

    failed = [each for each in item.evidence["observed"] if not each["satisfied"]]
    passed = [each for each in item.evidence["observed"] if each["satisfied"]]
    assert len(failed) == 1 and len(passed) == 1
    assert failed[0]["expected"] and failed[0]["ran"]  # what it was to produce, and how
    assert {"exit_code", "stdout", "stderr"} <= set(failed[0])
    assert passed[0]["stdout"].strip() == "120"  # what the oracle saw the other produce


async def test_items_made_in_the_same_moment_each_have_a_handle_of_their_own(
    world: DecisionWorld,
) -> None:
    """3C-S2-1.10: identities made together begin alike. Each item is shown with the
    shortest start that is its alone, and that start finds it."""
    await world.decide(await world.roadmap(STANDARD_RATE))
    for _ in range(12):
        await world.ask()
    inbox = Inbox(world.database)

    items = await inbox.items(PROJECT)

    handles = [each.handle for each in items]
    assert len(items) == 12 and len(set(handles)) == 12
    for each in items:
        assert each.item_id.startswith(each.handle) and len(each.handle) >= 8
        assert await inbox.item(PROJECT, each.handle) == each
    # A start that two share finds neither.
    shared = items[0].item_id[:8]
    if sum(1 for each in items if each.item_id.startswith(shared)) > 1:
        assert await inbox.item(PROJECT, shared) is None


async def test_a_feature_is_given_a_name_nobody_has(world: DecisionWorld) -> None:
    """3C-S2-1.12: features opened by other means chose their own names."""
    from tests.integration.fixtures import PRODUCT_OWNER as PO

    await world.stories.open_feature(PO, PROJECT, "F2", "named by its caller")

    started = await world.stories.start_feature(PROJECT, by("r-1", "", "start", problem="a third"))
    again = await world.stories.start_feature(PROJECT, by("r-2", "", "start", problem="a fourth"))

    assert (started.outcome, again.outcome) == ("started F3", "started F4")


async def test_a_cancellation_does_not_survive_a_final_check_retried_and_failed_again(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    """3C-S2-2.6: the feature needs a decision, the check is retried, and it fails again.
    It reads as it did, with another batch and another gate. A cancellation of the
    feature as it was first shown is stale."""
    from relay.application.stories import standing_of, transition_in

    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    failed = await failed_on_trunk(world)

    async def standing() -> str:
        async with world.database.connection() as connection:
            feature = await (
                await connection.execute("SELECT state, closed_by FROM features")
            ).fetchone()
            assert feature is not None
            return standing_of(feature, await transition_in(connection, PROJECT, "F1"))

    shown = await standing()
    await world.decide(failed.gate_id, "retry")
    assert await world.feature() == "needs_decision"  # the same state, by another failure

    old = await world.features.close(
        PROJECT, by("r-1", "F1", "cancel", reason="as I saw it", as_seen=shown)
    )

    assert shown != await standing()
    assert old.disposition == "stale"
    assert await world.column("SELECT closed_by FROM features") == [None]


async def test_a_call_whose_launch_nobody_can_vouch_for_is_not_shown_as_a_call_launched(
    calling: Calling,
) -> None:
    """3C-S2-2.8: a worker can die after a call is admitted and before the provider is
    started. Recovery records the call as uncertain. A person deciding whether to pay
    for another is told that, and not that a call was made."""
    world = calling.world
    await parked_call(calling)
    async with world.database.connection() as connection:
        await connection.execute("UPDATE invocations SET state = 'uncertain' WHERE attempt = 3")

    [item] = await Inbox(world.database).items(PROJECT)

    assert item.evidence["calls_launched"] == 2
    assert item.evidence["calls_that_may_or_may_not_have_been_launched"] == 1
    assert item.evidence["calls_that_never_started"] == 0


async def test_a_final_check_that_timed_out_says_so(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    """3C-S2-2.9: no exit status and nothing printed, and Relay knows why."""
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await failed_on_trunk(world)
    async with world.database.connection() as connection:
        await connection.execute("ALTER TABLE executions DISABLE TRIGGER USER")
        await connection.execute(
            "UPDATE executions SET ineligibility = 'timed_out', exit_code = NULL "
            "WHERE execution_id IN (SELECT execution_id FROM completion_receipts "
            "WHERE NOT satisfied)"
        )
        await connection.execute("ALTER TABLE executions ENABLE TRIGGER USER")

    [item] = await Inbox(world.database).items(PROJECT)

    [failed] = [each for each in item.evidence["observed"] if not each["satisfied"]]
    assert failed["why_the_run_does_not_count"] == "timed_out" and failed["exit_code"] is None
