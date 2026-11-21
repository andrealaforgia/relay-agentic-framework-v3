"""What becomes of a feature's final checks when something else happens meanwhile: a
pause, a cancel, a decision, a restart, a push, an unreachable repository. A batch of
checks decides only what it recorded, and only while it is the latest generation of a
request that is still wanted."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import final_checks
from relay.application.features import Features
from relay.application.final_checks import Completion
from relay.application.integration import Outcome
from relay.application.stories import Dropped, Kept, StoryDraft
from relay.domain.story import ActionRefused, Receipt, StoryState
from relay.git.checkout import CheckoutFailed, RemoteUnavailable
from relay.git.mirror import Mirror
from relay.testing.oracle import ContainerIsolation, Isolation, NativeIsolation, ProcessOracle
from tests.integration.completion_world import CompletionWorld, last_story_at_its_gate
from tests.integration.fixtures import (
    ANALYST,
    CONTRACT,
    PRODUCT_OWNER,
    VERIFIER,
    HeldAtABarrier,
    held_at_the_barrier,
)
from tests.integration.story_world import GREETS, STANDARD_RATE
from tests.integration.trunk_world import GREETING, PROJECT, ZERO_STAYS_ZERO


@pytest.fixture
async def world(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> CompletionWorld:
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


def checking_with(world: CompletionWorld, isolation: Isolation) -> Features:
    """Another control plane over the same project, whose final checks run in this."""
    return Features(
        world.database,
        world.reviews,
        world.integration,
        Completion(world.database, ProcessOracle(isolation)),
    )


async def checks_held(
    world: CompletionWorld, gate: uuid.UUID | None = None
) -> tuple[asyncio.Task[Receipt], HeldAtABarrier]:
    """The last story approved, and the feature's final checks observed and not yet
    returned: whatever the test does next happens while they are in flight."""
    gate = gate or await last_story_at_its_gate(world)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(checking_with(world, barrier).decide(world.action(gate)))
    await held_at_the_barrier(running, barrier)
    return running, barrier


async def a_second_feature(world: CompletionWorld) -> uuid.UUID:
    """Another feature of the same project, proposed and not yet approved."""
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F2", "shoppers want a greeting")
    return await world.stories.publish_roadmap(ANALYST, PROJECT, "F2", [GREETS], basis=0)


RECEIPT = StoryDraft("S3", "Receipt", "the shopper gets a receipt", {"AC1": "one is printed"})


async def two_features_at_work(world: CompletionWorld) -> None:
    """F1 with one story accepted and one still planned, while F2 holds the project's
    place: F1's planned story was waiting on a question when the place came free."""
    await world.decide(await world.roadmap(STANDARD_RATE, RECEIPT))
    await world.stories.decide(world.action(await a_second_feature(world)))
    await world.deliver("B1")
    await world.seal()
    await world.assess("B1 shows 120 for 100, as the story asks")
    question = await world.ask("S3")
    await world.decide(await world.gate())
    assert await world.story("S2") is StoryState.RELEASED
    await world.answer(question)
    await world.reconfirm_everything()
    assert [await world.feature("F1"), await world.feature("F2")] == ["delivering"] * 2
    assert await world.story("S3") is StoryState.PLANNED


async def admitted_and_not_taken_up(
    world: CompletionWorld, gate: uuid.UUID | None = None
) -> uuid.UUID:
    """The last story approved by a process that stopped before it took up the
    completion it then owed. Returns the assignment."""
    gate = gate or await last_story_at_its_gate(world)
    await world.stories.decide(world.action(gate))
    assert (await world.feature(), await world.batches()) == ("checking", [])
    [assignment] = await world.column(
        "SELECT assignment_id FROM assignments WHERE kind = 'complete' AND state = 'wanted'"
    )
    assert isinstance(assignment, uuid.UUID)
    return assignment


async def requests(world: CompletionWorld) -> list[object]:
    return await world.column("SELECT state FROM completion_requests ORDER BY created_at")


async def test_a_pause_during_the_checks_holds_them_and_resume_completes_the_feature(
    world: CompletionWorld,
) -> None:
    running, barrier = await checks_held(world)

    await world.close("pause", reason="wait for the audit")
    barrier.release.set()
    await running

    assert (await world.feature(), await world.batches()) == ("checking", ["held"])
    assert await world.summary() is None
    assert await world.completions_wanted() == 1
    await world.close("resume")
    assert (await world.feature(), await world.batches()) == ("completed", ["held", "completed"])


async def test_a_cancel_during_the_checks_abandons_them_for_good(world: CompletionWorld) -> None:
    running, barrier = await checks_held(world)

    await world.close("cancel", reason="dropped")
    barrier.release.set()
    await running

    assert (await world.feature(), await world.batches()) == ("cancelled", ["abandoned"])
    assert await requests(world) == ["abandoned"]
    assert await world.completions_wanted() == 0
    await world.features.start()
    assert (await world.feature(), await world.batches()) == ("cancelled", ["abandoned"])
    assert await world.summary() is None
    assert await world.column("SELECT count(*) FROM completion_receipts") == [1]  # kept


async def test_a_check_required_while_the_checks_run_is_run_too_before_anything_is_completed(
    world: CompletionWorld,
) -> None:
    running, barrier = await checks_held(world)

    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    barrier.release.set()
    await running

    assert await world.batches() == ["discarded", "completed"]
    summary = await world.summary()
    assert summary is not None
    assert len(summary["checked"]) == 2


async def test_trunk_moved_while_the_checks_ran_is_checked_again_on_the_new_head(
    world: CompletionWorld,
) -> None:
    running, barrier = await checks_held(world)

    elsewhere = world.advance_trunk({"notes.txt": "someone else was here\n"})
    barrier.release.set()
    await running

    assert await world.batches() == ["discarded", "completed"]
    summary = await world.summary()
    assert summary is not None and summary["trunk"] == elsewhere


async def test_another_features_integration_during_the_checks_is_in_what_gets_certified(
    world: CompletionWorld,
) -> None:
    """Amendment 2. The integration settles while the first feature's checks are still
    running, so its wake-up finds nothing owed; the batch that then finds trunk and the
    obligations changed has to see its own successor taken up."""
    gate = await last_story_at_its_gate(world)
    second = await a_second_feature(world)
    running, barrier = await checks_held(world, gate)
    await world.decide(second)
    assert await world.story("S2") is StoryState.RELEASED

    greeted = await world.deliver("G", "S2", GREETING, {"greeting.txt": "hello\n"})
    barrier.release.set()
    await running

    assert await world.batches() == ["discarded", "completed"]
    summary = await world.summary()
    assert summary is not None and summary["trunk"] == greeted
    assert sorted(each["behaviour_id"] for each in summary["checked"]) == ["B1", "G"]


async def test_checks_cut_short_are_run_again_from_the_start(world: CompletionWorld) -> None:
    class Broken:
        name = "container"

        async def run(self, *_: Any) -> Any:
            raise RuntimeError("the container runtime went away")

    gate = await last_story_at_its_gate(world)

    with pytest.raises(RuntimeError):
        await checking_with(world, Broken()).decide(world.action(gate))  # type: ignore[arg-type]

    assert (await world.feature(), await world.batches()) == ("checking", ["interrupted"])
    assert await world.completions_wanted() == 1
    await world.features.start()
    assert (await world.feature(), await world.batches()) == (
        "completed",
        ["interrupted", "completed"],
    )


async def test_a_process_that_stops_after_the_runs_and_before_deciding_completes_nothing(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = await last_story_at_its_gate(world)

    async def the_process_dies(*_: Any, **__: Any) -> None:
        raise KeyboardInterrupt

    async def and_cleans_nothing_up(*_: Any, **__: Any) -> None:
        return None

    with monkeypatch.context() as patched:
        patched.setattr(Completion, "_end", the_process_dies)
        patched.setattr(Completion, "_cut_short", and_cleans_nothing_up)
        with pytest.raises(KeyboardInterrupt):
            await world.decide(gate)
    assert (await world.feature(), await world.batches()) == ("checking", ["running"])
    assert await world.column("SELECT count(*) FROM completion_receipts") == [1]

    await world.features.start()

    assert await world.batches() == ["interrupted", "completed"]
    assert await world.feature() == "completed"
    # What the first batch saw is still on record, and was not reused.
    assert await world.column("SELECT count(*) FROM completion_receipts") == [2]


async def test_a_pause_before_the_completion_is_taken_up_starts_nothing(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await admitted_and_not_taken_up(world)
    await world.close("pause", reason="wait")

    async def unreachable(*_: Any, **__: Any) -> str:
        raise AssertionError("the repository was asked")

    with monkeypatch.context() as patched:
        patched.setattr(final_checks, "trunk_head", unreachable)
        await world.features.complete_what_is_owed(PROJECT)

    assert (await world.batches(), await world.completions_wanted()) == ([], 1)
    await world.close("resume")
    assert (await world.feature(), await world.batches()) == ("completed", ["completed"])


async def test_a_pause_between_reading_trunk_and_starting_the_checks_starts_nothing(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await admitted_and_not_taken_up(world)
    real = final_checks.trunk_head

    async def paused_just_after(*arguments: Any, **named: Any) -> str:
        head = await real(*arguments, **named)
        await world.close("pause", reason="wait")
        return head

    with monkeypatch.context() as patched:
        patched.setattr(final_checks, "trunk_head", paused_just_after)
        await world.features.complete_what_is_owed(PROJECT)

    assert (await world.batches(), await world.completions_wanted()) == ([], 1)
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'final_check'") == [
        0
    ]
    await world.close("resume")
    assert await world.feature() == "completed"


async def test_a_repository_that_does_not_answer_leaves_the_completion_owed(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await admitted_and_not_taken_up(world)

    async def unreachable(*_: Any, **__: Any) -> str:
        raise CheckoutFailed("the repository did not answer")

    with monkeypatch.context() as patched:
        patched.setattr(final_checks, "trunk_head", unreachable)
        await world.features.complete_what_is_owed(PROJECT)
        assert (await world.batches(), await world.completions_wanted()) == ([], 1)

    await world.features.reconcile(PROJECT)  # the ordinary pass, with no restart

    assert (await world.feature(), await world.batches()) == ("completed", ["completed"])


async def test_trunk_unreadable_when_the_checks_have_passed_puts_the_decision_off(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await admitted_and_not_taken_up(world)
    real = final_checks.trunk_head
    reads = 0

    async def answers_once(*arguments: Any, **named: Any) -> str:
        nonlocal reads
        reads += 1
        if reads > 1:
            raise CheckoutFailed("the repository did not answer")
        return await real(*arguments, **named)

    with monkeypatch.context() as patched:
        patched.setattr(final_checks, "trunk_head", answers_once)
        await world.features.complete_what_is_owed(PROJECT)
        assert (await world.batches(), await world.completions_wanted()) == (["deferred"], 1)
        assert await world.feature() == "checking"

    await world.features.reconcile(PROJECT)

    assert await world.batches() == ["deferred", "completed"]


async def test_a_completion_delivered_again_is_answered_from_the_record(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amendment 4: nothing is asked of the repository to say what already happened."""
    done = await admitted_and_not_taken_up(world)
    completion = Completion(world.database, world.oracle)
    first = await completion.take_up(done)
    assert first.outcome == "completed"

    async def unreachable(*_: Any, **__: Any) -> str:
        raise AssertionError("the repository was asked")

    monkeypatch.setattr(final_checks, "trunk_head", unreachable)

    assert await completion.take_up(done) == first
    assert await world.batches() == ["completed"]


async def test_a_completion_cancelled_before_it_was_taken_up_is_answered_as_retired(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amendments 3 and 4: a cancel between admission and take-up ends the request,
    though there is no batch to end with it."""
    unused = await admitted_and_not_taken_up(world)
    await world.close("cancel", reason="dropped")
    assert await requests(world) == ["abandoned"]

    async def unreachable(*_: Any, **__: Any) -> str:
        raise AssertionError("the repository was asked")

    monkeypatch.setattr(final_checks, "trunk_head", unreachable)

    assert (await Completion(world.database, world.oracle).take_up(unused)).outcome == "retired"
    await world.features.start()
    assert (await world.batches(), await world.completions_wanted()) == ([], 0)
    assert await world.summary() is None


async def test_two_take_ups_of_one_completion_at_once_run_the_checks_once(
    world: CompletionWorld,
) -> None:
    owed = await admitted_and_not_taken_up(world)
    completion = Completion(world.database, world.oracle)

    first, second = await asyncio.gather(completion.take_up(owed), completion.take_up(owed))

    assert await world.batches() == ["completed"]
    assert {first.outcome, second.outcome} <= {"completed", "running"}
    assert first.batch_id == second.batch_id
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'final_check'") == [
        1
    ]


async def test_a_decision_over_the_feature_during_the_checks_takes_it_out_of_them(
    world: CompletionWorld,
) -> None:
    """S3-16.2 and amendment 3. The roadmap is no longer current, so the feature is not
    ready; its request ends with the generation that was running, and what that
    generation then finds decides nothing."""
    running, barrier = await checks_held(world)

    await world.answer(await world.ask(story=None))
    assert await world.feature() == "delivering"
    assert (await requests(world), await world.batches()) == (["overtaken"], ["overtaken"])
    assert await world.completions_wanted() == 0
    barrier.release.set()
    await running

    assert await world.batches() == ["overtaken"]
    assert await world.summary() is None
    await world.features.start()
    assert (await world.feature(), await world.batches()) == ("delivering", ["overtaken"])

    await world.reconfirm_everything()  # the Analyst: the roadmap stands under the answer

    assert await world.feature() == "completed"
    assert await requests(world) == ["overtaken", "completed"]


async def test_a_decision_over_the_feature_supersedes_the_gate_of_a_failed_check(
    world: CompletionWorld,
) -> None:
    """S3-15.6."""
    gate = await last_story_at_its_gate(world)
    await world.integration.require_check(PROJECT, GREETING.as_check())
    await world.decide(gate)
    [failed] = await world.stories.inbox(PROJECT)

    await world.answer(await world.ask(story=None))

    assert await world.feature() == "delivering"
    assert [item.kind for item in await world.stories.inbox(PROJECT)] == []
    assert (await world.decide(failed.gate_id, "retry")).disposition == "stale"
    assert await world.batches() == ["failed"]
    await world.reconfirm_everything()
    # Ready again under the answer: the checks run afresh, and fail as before.
    assert await world.batches() == ["failed", "failed"]
    assert await world.feature() == "needs_decision"


@pytest.mark.parametrize("landed", [False, True])
@pytest.mark.parametrize("settled_by_another_control_plane", [False, True])
async def test_a_push_on_its_way_puts_the_decision_off_until_it_has_settled(
    world: CompletionWorld,
    monkeypatch: pytest.MonkeyPatch,
    landed: bool,
    settled_by_another_control_plane: bool,
) -> None:
    """S3-15.1, S3-16.4 and 3AII-S4-1.4. With a push authorised and unanswered, the
    repository and the records cannot be read as one moment, whether or not the push has
    arrived. Once it settles, the completion is taken up with nothing else called,
    whichever control plane settled it."""
    gate = await last_story_at_its_gate(world)
    second = await a_second_feature(world)
    await admitted_and_not_taken_up(world, gate)
    await world.stories.decide(world.action(second))
    base = await world.awaiting_builder("G", GREETING, "S2")
    greeted = await world.reviewed("G", {"greeting.txt": "hello\n"}, on=base)
    real = Mirror._push

    async def no_reply(self: Mirror, *arguments: Any, **named: Any) -> Any:
        if landed:
            await real(self, *arguments, **named)
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", no_reply)
        assert (await world.integrate("G")).outcome is Outcome.PENDING
        assert world.trunk() == (greeted if landed else base)

        await world.features.complete_what_is_owed(PROJECT)

        assert (await world.batches(), await world.feature()) == (["deferred"], "checking")
        assert await world.completions_wanted() == 1

    settler = world.integration_with() if settled_by_another_control_plane else world.integration
    await settler.reconcile()  # the push is settled; nothing else is called

    assert world.trunk() == greeted
    assert await world.batches() == ["deferred", "completed"]
    summary = await world.summary()
    assert summary is not None and summary["trunk"] == greeted
    assert sorted(each["behaviour_id"] for each in summary["checked"]) == ["B1", "G"]


async def test_checks_that_cannot_be_observed_in_a_container_start_nothing(
    world: CompletionWorld,
) -> None:
    gate = await last_story_at_its_gate(world)

    await checking_with(world, NativeIsolation()).decide(world.action(gate))

    assert (await world.feature(), await world.batches()) == ("checking", [])
    assert await world.completions_wanted() == 1


async def test_no_worker_can_claim_a_completion(world: CompletionWorld) -> None:
    owed = await admitted_and_not_taken_up(world)

    with pytest.raises(ActionRefused, match="control plane"):
        await world.claims.claim(VERIFIER, owed)

    assert await world.completions_wanted() == 1


async def test_two_obligations_that_ask_the_same_thing_are_each_observed(
    world: CompletionWorld,
) -> None:
    """S3-16.6."""
    gate = await last_story_at_its_gate(world)
    await world.integration.require_check(PROJECT, CONTRACT.as_check())

    await world.decide(gate)

    summary = await world.summary()
    assert summary is not None
    first, second = summary["checked"]
    assert first["digest"] == second["digest"]
    assert first["obligation_id"] != second["obligation_id"]
    assert first["execution_id"] != second["execution_id"]
    assert sorted((first["source"], second["source"])) == ["behaviour", "check"]


async def test_a_feature_is_ready_once_a_proposal_that_was_waiting_is_sent_back(
    world: CompletionWorld,
) -> None:
    """S3-15.4."""
    gate = await last_story_at_its_gate(world)
    proposal = await world.revise(Kept("S1"), GREETS)
    await world.decide(gate)
    assert (await world.feature(), await world.batches()) == ("delivering", [])

    await world.decide(proposal, "request_changes", reason="not needed after all")

    assert (await world.feature(), await world.batches()) == ("completed", ["completed"])


async def test_a_question_before_the_completion_is_taken_up_starts_nothing_and_its_answer_ends_it(
    world: CompletionWorld,
) -> None:
    """S3-16.3. The completion that was owed is retired with its request: taken up
    after the answer, it would check a feature whose roadmap no longer stands."""
    await admitted_and_not_taken_up(world)

    question = await world.ask(story=None)  # followed by the pass, which starts nothing

    assert (await world.batches(), await world.completions_wanted()) == ([], 1)
    assert await world.feature() == "checking"

    await world.answer(question)

    assert (await world.feature(), await requests(world)) == ("delivering", ["overtaken"])
    assert (await world.batches(), await world.completions_wanted()) == ([], 0)
    await world.reconfirm_everything()
    assert (await world.feature(), await world.batches()) == ("completed", ["completed"])


async def test_readiness_is_settled_in_the_transaction_that_changes_it(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that stops between committing a decision and its pass leaves nothing
    half-said: the feature is already out of its checks, or already back in them."""
    await admitted_and_not_taken_up(world)

    async def the_process_stops(*_: Any, **__: Any) -> None:
        return None

    with monkeypatch.context() as patched:
        patched.setattr(Features, "reconcile", the_process_stops)
        await world.answer(await world.ask(story=None))
        assert (await world.feature(), await requests(world)) == ("delivering", ["overtaken"])
        assert await world.completions_wanted() == 0

        await world.reconfirm_everything()
        assert (await world.feature(), await world.batches()) == ("checking", [])
        assert await world.completions_wanted() == 1

    await world.features.start()
    assert (await world.feature(), await world.batches()) == ("completed", ["completed"])


async def test_the_pass_admits_a_feature_that_is_ready_and_was_never_looked_at(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The net under every transition: a feature whose last story was accepted before
    Relay looked at readiness there, as one accepted by an earlier version was."""
    from relay.application import stories

    gate = await last_story_at_its_gate(world)

    async def not_looked_at(*_: Any, **__: Any) -> None:
        return None

    with monkeypatch.context() as patched:
        patched.setattr(stories, "evaluate_completion", not_looked_at)
        await world.stories.decide(world.action(gate))
    assert (await world.feature(), await world.completions_wanted()) == ("delivering", 0)

    await world.features.reconcile(PROJECT)

    assert (await world.feature(), await world.batches()) == ("completed", ["completed"])


async def test_checks_left_running_under_a_pause_are_held_when_the_control_plane_starts(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3-16.5."""
    gate = await last_story_at_its_gate(world)

    async def the_process_dies(*_: Any, **__: Any) -> None:
        raise KeyboardInterrupt

    async def and_cleans_nothing_up(*_: Any, **__: Any) -> None:
        return None

    with monkeypatch.context() as patched:
        patched.setattr(Completion, "_end", the_process_dies)
        patched.setattr(Completion, "_cut_short", and_cleans_nothing_up)
        with pytest.raises(KeyboardInterrupt):
            await world.decide(gate)
    await world.close("pause", reason="wait for the audit")

    await world.features.start()

    assert (await world.feature(), await world.batches()) == ("checking", ["held"])
    assert await world.completions_wanted() == 1
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'final_check'") == [
        1
    ]
    await world.close("resume")
    assert (await world.feature(), await world.batches()) == ("completed", ["held", "completed"])


async def test_one_settlement_looks_for_completions_once(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S4-1.4."""
    await world.decide(await world.roadmap(STANDARD_RATE))
    looked: list[str] = []
    real = Completion.take_up_what_is_owed

    async def counted(self: Completion, project_id: str) -> None:
        looked.append(project_id)
        await real(self, project_id)

    monkeypatch.setattr(Completion, "take_up_what_is_owed", counted)

    await world.deliver("B1")

    assert looked == [PROJECT]


async def test_a_caller_cancelled_while_the_checks_are_being_ended_leaves_them_to_the_next_pass(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S4-1.2. The process lives on, so nothing but the ordinary pass will come."""
    await admitted_and_not_taken_up(world)
    real = final_checks.trunk_head
    reads = 0
    at_the_last_read = asyncio.Event()

    async def never_answers_the_second_time(*arguments: Any, **named: Any) -> str:
        nonlocal reads
        reads += 1
        if reads > 1:
            at_the_last_read.set()
            await asyncio.Event().wait()
        return await real(*arguments, **named)

    with monkeypatch.context() as patched:
        patched.setattr(final_checks, "trunk_head", never_answers_the_second_time)
        passing = asyncio.create_task(world.features.complete_what_is_owed(PROJECT))
        await at_the_last_read.wait()
        passing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await passing

    assert (await world.feature(), await world.batches()) == ("checking", ["interrupted"])
    assert await world.completions_wanted() == 1

    await world.features.reconcile(PROJECT)

    assert (await world.feature(), await world.batches()) == (
        "completed",
        ["interrupted", "completed"],
    )
    assert await world.column("SELECT count(*) FROM feature_summaries") == [1]


async def test_a_completion_delivered_again_after_its_checks_were_discarded_is_answered_the_same(
    world: CompletionWorld,
) -> None:
    """3AII-S4-1.6. The successor is driven, and the answer is still this completion's."""
    owed = await admitted_and_not_taken_up(world)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    completion = Completion(world.database, ProcessOracle(barrier))
    running = asyncio.create_task(completion.take_up(owed))
    await held_at_the_barrier(running, barrier)
    world.advance_trunk({"notes.txt": "someone else was here\n"})
    barrier.release.set()

    first = await running

    assert first.outcome == "discarded"
    assert await world.batches() == ["discarded", "completed"]
    assert await completion.take_up(owed) == first


async def test_the_pass_holds_one_features_row_at_a_time(world: CompletionWorld) -> None:
    """3AII-S4-1.3. A decision that releases a story into another feature holds its own
    feature's row and asks for the other's; the pass must never do the reverse."""
    await two_features_at_work(world)

    async with world.database.connection() as held, held.transaction():
        await held.execute("SELECT 1 FROM features WHERE feature_id = 'F2' FOR UPDATE")
        passing = asyncio.create_task(world.features.reconcile(PROJECT))
        await world.until_someone_waits_for_a_lock()
        async with world.database.connection() as other, other.transaction():
            await other.execute("SELECT 1 FROM features WHERE feature_id = 'F1' FOR UPDATE NOWAIT")
    await passing


async def test_dropping_the_last_planned_story_is_what_completes_the_feature(
    world: CompletionWorld,
) -> None:
    """3AII-S4-1.7. The roadmap decision alone makes the feature ready."""
    await two_features_at_work(world)
    revision, _ = await world.stories.revisions_of(PROJECT, "S3")
    proposal = await world.revise(
        Kept("S1"), Dropped("S3", story_revision=revision, story_basis=await world.basis("S3"))
    )
    assert (await world.feature("F1"), await world.batches()) == ("delivering", [])

    await world.decide(proposal)

    assert [await world.feature("F1"), await world.feature("F2")] == ["completed", "delivering"]
    summary = await world.summary()
    assert summary is not None
    assert [(story["story_id"], story["outcome"]) for story in summary["stories"]] == [
        ("S1", "accepted"),
        ("S3", "withdrawn"),
    ]


async def test_checks_that_fail_to_run_after_a_cancel_owe_nothing_more(
    world: CompletionWorld,
) -> None:
    """S3-16.5. The cancel has ended the batch; cleaning up after the failure that
    follows must not owe a completion for a feature that is finished with."""

    class ThenFails(HeldAtABarrier):
        async def run(self, *arguments: Any) -> Any:
            await super().run(*arguments)
            raise RuntimeError("the container runtime went away")

    gate = await last_story_at_its_gate(world)
    barrier = ThenFails(ContainerIsolation(world.image))
    running = asyncio.create_task(checking_with(world, barrier).decide(world.action(gate)))
    await held_at_the_barrier(running, barrier)

    await world.close("cancel", reason="dropped")
    barrier.release.set()
    with pytest.raises(RuntimeError):
        await running

    assert (await world.feature(), await world.batches()) == ("cancelled", ["abandoned"])
    assert await world.completions_wanted() == 0
    await world.features.start()
    assert (await world.batches(), await world.completions_wanted()) == (["abandoned"], 0)


async def test_a_caller_cancelled_just_as_the_checks_were_started_leaves_them_to_the_next_pass(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S4-2.3. The generation is committed and its completion consumed; the caller
    is cancelled before it hears so."""
    await admitted_and_not_taken_up(world)
    real = Completion._start
    committed = asyncio.Event()

    async def committed_and_never_heard_of(self: Completion, *arguments: Any) -> Any:
        await real(self, *arguments)
        committed.set()
        await asyncio.Event().wait()

    with monkeypatch.context() as patched:
        patched.setattr(Completion, "_start", committed_and_never_heard_of)
        passing = asyncio.create_task(world.features.complete_what_is_owed(PROJECT))
        await committed.wait()
        assert await world.batches() == ["running"]
        passing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await passing

    assert (await world.batches(), await world.completions_wanted()) == (["interrupted"], 1)

    await world.features.reconcile(PROJECT)

    assert (await world.feature(), await world.batches()) == (
        "completed",
        ["interrupted", "completed"],
    )


async def test_cleaning_up_after_cut_short_checks_asks_for_the_project_before_the_feature(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S4-2.4. An approval sent again holds the project and asks for the feature;
    the clean-up must never hold the feature and ask for the project."""
    await admitted_and_not_taken_up(world)
    real_start, real_cut = Completion._start, Completion._cut_short
    committed, cleaning, go_on = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def committed_and_never_heard_of(self: Completion, *arguments: Any) -> Any:
        await real_start(self, *arguments)
        committed.set()
        await asyncio.Event().wait()

    async def when_told(self: Completion, *arguments: Any) -> None:
        cleaning.set()
        await go_on.wait()
        await real_cut(self, *arguments)

    monkeypatch.setattr(Completion, "_start", committed_and_never_heard_of)
    monkeypatch.setattr(Completion, "_cut_short", when_told)
    passing = asyncio.create_task(world.features.complete_what_is_owed(PROJECT))
    await committed.wait()
    passing.cancel()
    await cleaning.wait()

    async with world.database.connection() as held, held.transaction():
        await held.execute("SELECT 1 FROM projects FOR UPDATE")
        go_on.set()
        await world.until_someone_waits_for_a_lock()
        async with world.database.connection() as other, other.transaction():
            await other.execute("SELECT 1 FROM features WHERE feature_id = 'F1' FOR UPDATE NOWAIT")
    with pytest.raises(asyncio.CancelledError):
        await passing

    assert (await world.batches(), await world.completions_wanted()) == (["interrupted"], 1)


async def deferred_behind_a_push_that_landed(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1's final checks put off behind F2's push, which arrived and was not answered."""
    gate = await last_story_at_its_gate(world)
    second = await a_second_feature(world)
    await admitted_and_not_taken_up(world, gate)
    await world.stories.decide(world.action(second))
    base = await world.awaiting_builder("G", GREETING, "S2")
    await world.reviewed("G", {"greeting.txt": "hello\n"}, on=base)
    real = Mirror._push

    async def lost_reply(self: Mirror, *arguments: Any, **named: Any) -> Any:
        await real(self, *arguments, **named)
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", lost_reply)
        assert (await world.integrate("G", "the-push")).outcome is Outcome.PENDING
        await world.features.complete_what_is_owed(PROJECT)
    assert (await world.batches(), await world.completions_wanted()) == (["deferred"], 1)


@pytest.mark.parametrize("made_up_for_by", ["the integration asked for again", "the scan"])
async def test_a_wake_up_cut_short_after_the_push_settled_is_made_up_for(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch, made_up_for_by: str
) -> None:
    """3AII-S4-2.5. The settlement is committed, so no outstanding push is left to find
    the completion by."""
    await deferred_behind_a_push_that_landed(world, monkeypatch)
    settled = asyncio.Event()

    async def never_gets_to_it(self: Completion, project_id: str) -> None:
        settled.set()
        await asyncio.Event().wait()

    with monkeypatch.context() as patched:
        patched.setattr(Completion, "take_up_what_is_owed", never_gets_to_it)
        settling = asyncio.create_task(world.integration.reconcile())
        await settled.wait()
        settling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await settling
    assert (await world.batches(), await world.completions_wanted()) == (["deferred"], 1)
    assert await world.column(
        "SELECT count(*) FROM integration_intents WHERE state = 'outstanding'"
    ) == [0]

    if made_up_for_by == "the scan":
        assert await world.integration.reconcile() == 0
    else:
        assert (await world.integrate("G", "the-push")).outcome is Outcome.INTEGRATED

    assert (await world.feature(), await world.batches()) == (
        "completed",
        ["deferred", "completed"],
    )


async def test_two_control_planes_settling_one_push_complete_the_feature_once(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S4-2.5. Both look for completions afterwards; looking twice is safe."""
    await deferred_behind_a_push_that_landed(world, monkeypatch)

    await asyncio.gather(world.integration.reconcile(), world.integration_with().reconcile())

    assert (await world.feature(), await world.batches()) == (
        "completed",
        ["deferred", "completed"],
    )
    assert await world.column("SELECT count(*) FROM feature_summaries") == [1]
    assert (await world.events()).count("feature.completed") == 1


async def test_a_caller_cancelled_again_does_not_take_its_clean_up_with_it(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S4-3.3. The clean-up waits for a row; the caller is cancelled a second time.
    The process lives on, and the clean-up is the only thing that will end the batch."""
    await admitted_and_not_taken_up(world)
    real_start, real_cut = Completion._start, Completion._cut_short
    committed, cleaning, go_on = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def committed_and_never_heard_of(self: Completion, *arguments: Any) -> Any:
        await real_start(self, *arguments)
        committed.set()
        await asyncio.Event().wait()

    async def when_told(self: Completion, *arguments: Any) -> None:
        cleaning.set()
        await go_on.wait()
        await real_cut(self, *arguments)

    with monkeypatch.context() as patched:
        patched.setattr(Completion, "_start", committed_and_never_heard_of)
        patched.setattr(Completion, "_cut_short", when_told)
        passing = asyncio.create_task(world.features.complete_what_is_owed(PROJECT))
        await committed.wait()
        passing.cancel()
        await cleaning.wait()
        async with world.database.connection() as held, held.transaction():
            await held.execute("SELECT 1 FROM projects FOR UPDATE")
            go_on.set()
            await world.until_someone_waits_for_a_lock()
            passing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await passing
            assert await world.batches() == ["running"]
        async with asyncio.timeout(30):
            while await world.batches() != ["interrupted"]:
                await asyncio.sleep(0.05)

    await world.features.reconcile(PROJECT)

    assert (await world.feature(), await world.batches()) == (
        "completed",
        ["interrupted", "completed"],
    )
    assert await world.column("SELECT count(*) FROM feature_summaries") == [1]


async def test_a_checkout_that_stalls_is_given_up_on_and_the_completion_stays_owed(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S4-3.4. The final checks fetch the commit with the patience they have for
    any other exchange with the repository, and the pass goes on to whatever is next."""
    await admitted_and_not_taken_up(world)
    deadlines: list[float | None] = []

    async def stalls(*_: Any, timeout: float | None = None) -> Path:
        deadlines.append(timeout)
        raise CheckoutFailed("the repository stopped answering")

    with monkeypatch.context() as patched:
        patched.setattr(final_checks, "pristine_checkout", stalls)
        await Completion(world.database, world.oracle, remote_timeout=7.0).take_up_what_is_owed(
            PROJECT
        )

    assert deadlines == [7.0]
    assert (await world.batches(), await world.completions_wanted()) == (["interrupted"], 1)
    await world.features.reconcile(PROJECT)
    assert await world.feature() == "completed"


async def test_one_pass_attempts_a_request_that_is_put_off_once(
    world: CompletionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S4-3.5."""
    await admitted_and_not_taken_up(world)
    real = final_checks.trunk_head
    reads = 0

    async def every_last_read_fails(*arguments: Any, **named: Any) -> str:
        nonlocal reads
        reads += 1
        if reads % 2 == 0:
            raise CheckoutFailed("the repository did not answer")
        return await real(*arguments, **named)

    monkeypatch.setattr(final_checks, "trunk_head", every_last_read_fails)

    await world.features.reconcile(PROJECT)

    assert (await world.batches(), await world.completions_wanted()) == (["deferred"], 1)
