"""Calls to a provider cost money and cannot be taken back. So: one decision admits a
call; what it returned is on record before anything is done with it; its effects are
carried out once; and when a worker is gone, what it left is dealt with from the record,
never by asking the model again unless a person says so."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.features import Features
from relay.application.invocations import Effect, EffectRun, Invocations, Prepared
from relay.application.workers import WorkerSession, is_alive
from relay.domain.story import ActionRefused, HumanAction, Receipt
from relay.providers.claude import Outcome, ProviderResult
from relay.providers.definitions import AgentDefinition
from relay.providers.stream import Terminal, read_stream
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import PRODUCT_OWNER
from tests.integration.story_world import STANDARD_RATE
from tests.integration.trunk_world import PROJECT

RECORDED = Path(__file__).parent.parent / "fixtures" / "claude"
DEFINITION = AgentDefinition(
    name="alf-atdd-developer",
    path=Path("/agents/alf-atdd-developer.md"),
    sha256="a" * 64,
    revision="b" * 40,
    dirty=False,
    description="Builds by acceptance test.",
    prompt="You build.\n",
)
PREPARED = Prepared(
    provider="claude",
    model="claude-haiku-5-5",
    effort=None,
    definition=DEFINITION,
    effective_agent_sha256="c" * 64,
    briefing="Make B1 pass.",
    schema={"type": "object"},
)
UNCERTAIN = ProviderResult(Outcome.UNCERTAIN, reason="no result within 5 seconds")
NOT_STARTED = ProviderResult(Outcome.NOT_STARTED, reason="could not be launched")


def said(recorded: str = "success.jsonl") -> ProviderResult:
    """What the adapter makes of a stream recorded from a real run."""
    [terminal] = [
        each
        for each in read_stream((RECORDED / recorded).read_text().splitlines())
        if isinstance(each, Terminal)
    ]
    return ProviderResult(Outcome.RETURNED if terminal.succeeded else Outcome.ERRORED, terminal)


class Applying:
    """Two effects, each of which says when it ran and can be made to fail or to wait."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.failing: dict[str, Exception] = {}
        self.waiting: dict[str, asyncio.Event] = {}
        self.doing: dict[str, Any] = {}
        self.reached = asyncio.Event()

    def effects_for(self, _: dict[str, Any]) -> list[Effect]:
        return [Effect("first", self._perform("first")), Effect("second", self._perform("second"))]

    def _perform(self, name: str) -> Any:
        async def perform(run: EffectRun) -> Mapping[str, Any] | None:
            this = run.identity.split(":", 1)[1]
            self.ran.append(this)
            for held in (name, this):
                if held in self.waiting:
                    self.reached.set()
                    await self.waiting[held].wait()
            if name in self.failing:
                raise self.failing[name]
            if name in self.doing:
                await self.doing[name]()
            return {"did": name}

        return perform


class Calling:
    """A project with one build owed, workers to do it, and the service under test."""

    def __init__(self, world: DecisionWorld) -> None:
        self.world = world
        self.applying = Applying()
        self.invocations = Invocations(world.database, self.applying.effects_for)
        self.sessions: list[WorkerSession] = []
        self.requests = 0

    async def worker(self, role: str = "builder") -> WorkerSession:
        session = await WorkerSession.start(self.world.database, role)
        self.sessions.append(session)
        return session

    async def owed(self) -> uuid.UUID:
        [assignment] = await self.world.column(
            "SELECT assignment_id FROM assignments WHERE kind = 'build'"
        )
        assert isinstance(assignment, uuid.UUID)
        return assignment

    async def claimed(self, worker: WorkerSession) -> tuple[uuid.UUID, uuid.UUID]:
        assignment = await self.owed()
        grant = await self.world.claims.claim(worker.principal, assignment, worker.worker_id)
        return assignment, grant

    async def admitted(self, worker: WorkerSession) -> uuid.UUID:
        assignment, grant = await self.claimed(worker)
        admission = await self.invocations.admit(worker, assignment, grant, PREPARED)
        assert admission.outcome == "admitted", admission.reason
        assert admission.invocation_id is not None
        return admission.invocation_id

    async def states(self) -> list[object]:
        return await self.world.column("SELECT state FROM invocations ORDER BY attempt")

    async def assignment(self) -> tuple[object, ...]:
        async with self.world.database.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT state, parked, grant_id IS NOT NULL AS granted FROM assignments "
                    "WHERE kind = 'build'"
                )
            ).fetchone()
        assert row is not None
        return (row["state"], row["parked"], row["granted"])

    def person(self, invocation: uuid.UUID, action: str) -> HumanAction:
        self.requests += 1
        return HumanAction(
            PRODUCT_OWNER, f"call-request-{self.requests}", str(invocation), action, {}
        )

    async def close(self) -> None:
        await self.invocations.close()
        for session in self.sessions:
            await session.stop()


@pytest.fixture
async def calling(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> AsyncIterator[Calling]:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    calling = Calling(world)
    yield calling
    await calling.close()


async def test_a_call_is_on_record_before_it_is_made(calling: Calling) -> None:
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)

    admission = await calling.invocations.admit(worker, assignment, grant, PREPARED)

    assert (admission.outcome, admission.attempt) == ("admitted", 1)
    async with calling.world.database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM invocations")).fetchone()
    assert row is not None
    assert (row["state"], row["invocation_id"]) == ("intended", admission.invocation_id)
    assert (row["role"], row["model"], row["worker_id"]) == (
        "builder",
        "claude-haiku-5-5",
        worker.worker_id,
    )
    assert (row["agent_name"], row["agent_sha256"], row["agent_revision"]) == (
        "alf-atdd-developer",
        "a" * 64,
        "b" * 40,
    )
    assert (row["briefing"], row["effective_agent_sha256"]) == ("Make B1 pass.", "c" * 64)
    assert row["admitted_on"] == {
        "grant_id": str(grant),
        "attempt": 1,
        "story_revision": await calling.world.revision("S1"),
        "list_revision": row["admitted_on"]["list_revision"],
        "basis": await calling.world.basis("S1"),
    }
    assert "invocation.intended" in await calling.world.events()


async def test_a_delivery_that_comes_again_never_buys_a_second_call(calling: Calling) -> None:
    """S3-18.1, on both sides of the record and after the apply."""
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    first = await calling.invocations.admit(worker, assignment, grant, PREPARED)
    assert first.invocation_id is not None

    during = await calling.invocations.admit(worker, assignment, grant, PREPARED)
    assert (during.outcome, "in flight" in during.reason) == ("refused", True)

    await calling.invocations.record(first.invocation_id, said())
    recorded = await calling.invocations.admit(worker, assignment, grant, PREPARED)
    assert (recorded.outcome, recorded.invocation_id) == ("apply", first.invocation_id)

    assert await calling.invocations.apply(first.invocation_id, worker) == "applied"
    after = await calling.invocations.admit(worker, assignment, grant, PREPARED)
    assert after.outcome == "finished"

    assert await calling.states() == ["applied"]
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_what_a_call_returned_is_stored_whole_with_what_it_used(calling: Calling) -> None:
    invocation = await calling.admitted(await calling.worker())

    assert await calling.invocations.record(invocation, said()) == "returned"

    async with calling.world.database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM invocations")).fetchone()
    assert row is not None
    assert row["envelope"]["subtype"] == "success"
    assert row["reply"] == {"sum": 5}
    assert row["usage"] == [
        {
            "model": "claude-haiku-5-5",
            "input_tokens": 2,
            "output_tokens": 52,
            "cache_read_tokens": 0,
            "cache_write_tokens": 4325,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 4325,
        }
    ]
    assert row["provider_cost_usd"] == Decimal("0.0008912")
    assert row["estimated_cost_usd"] == Decimal("0.0008912")
    assert row["price_table"] == "2026-11-03"


async def test_a_call_that_ended_in_an_error_is_known_and_keeps_what_it_used(
    calling: Calling,
) -> None:
    invocation = await calling.admitted(await calling.worker())

    assert await calling.invocations.record(invocation, said("budget_exhausted.jsonl")) == "errored"

    async with calling.world.database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM invocations")).fetchone()
    assert row is not None
    assert (row["reason"], row["reply"]) == ("error_max_budget_usd", None)
    assert row["usage"][0]["cache_read_tokens"] == 1080
    assert row["provider_cost_usd"] is not None
    assert await calling.assignment() == ("granted", None, True)  # the same worker may try again


async def test_a_reply_that_cannot_be_used_is_a_known_call_and_the_next_is_another(
    calling: Calling,
) -> None:
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    first = await calling.invocations.admit(worker, assignment, grant, PREPARED)
    assert first.invocation_id is not None

    state = await calling.invocations.record(
        first.invocation_id, said(), problem="the reply names no commit"
    )
    second = await calling.invocations.admit(worker, assignment, grant, PREPARED)

    assert state == "rejected"
    assert await calling.world.column("SELECT reply FROM invocations WHERE attempt = 1") == [
        None
    ]  # it arrived, and it is not a reply
    assert (second.outcome, second.attempt) == ("admitted", 2)
    assert await calling.states() == ["rejected", "intended"]
    assert await calling.world.column("SELECT reason FROM invocations WHERE attempt = 1") == [
        "the reply names no commit"
    ]


async def test_after_three_calls_that_gave_nothing_a_person_is_asked(calling: Calling) -> None:
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    for _ in range(3):
        admission = await calling.invocations.admit(worker, assignment, grant, PREPARED)
        assert admission.invocation_id is not None
        await calling.invocations.record(admission.invocation_id, said(), problem="unusable")

    state, parked, granted = await calling.assignment()
    assert (state, granted) == ("wanted", False)
    assert parked == "3 calls gave nothing that could be used"
    [waiting] = await calling.invocations.parked(PROJECT)
    assert (waiting.state, waiting.role) == ("rejected", "builder")
    with pytest.raises(ActionRefused, match="parked"):
        await calling.claimed(await calling.worker())


async def test_the_attempts_are_counted_over_the_work_whoever_was_granted_it(
    calling: Calling,
) -> None:
    """S3-20.1: taking a grant back and giving it again buys no further attempts."""
    for _ in range(3):
        worker = await calling.worker()
        invocation = await calling.admitted(worker)
        await calling.invocations.record(invocation, said(), problem="unusable")
        await worker.stop()
        await calling.invocations.recover(PROJECT)

    assert await calling.states() == ["rejected", "rejected", "rejected"]
    assert (await calling.assignment())[1] == "3 calls gave nothing that could be used"
    assert len(await calling.world.column("SELECT DISTINCT grant_id FROM invocations")) == 3


async def test_a_call_that_could_not_be_launched_cost_nothing_and_the_work_is_wanted_again(
    calling: Calling,
) -> None:
    invocation = await calling.admitted(await calling.worker())

    assert await calling.invocations.record(invocation, NOT_STARTED) == "not_started"

    assert await calling.assignment() == ("wanted", None, False)
    assert await calling.invocations.parked(PROJECT) == []
    again = await calling.admitted(await calling.worker())
    assert again != invocation


async def test_a_call_whose_result_was_not_received_waits_for_a_person(calling: Calling) -> None:
    """D-6: it may have completed and been paid for."""
    invocation = await calling.admitted(await calling.worker())

    assert await calling.invocations.record(invocation, UNCERTAIN) == "uncertain"

    state, parked, granted = await calling.assignment()
    assert (state, granted) == ("wanted", False)
    assert parked is not None and "may have completed" in parked
    [waiting] = await calling.invocations.parked(PROJECT)
    assert waiting.invocation_id == invocation
    async with calling.world.database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM invocations")).fetchone()
    assert row is not None
    assert (row["usage"], row["provider_cost_usd"], row["estimated_cost_usd"]) == (None, None, None)


async def test_a_pause_before_the_call_is_admitted_stops_it_and_one_after_does_not_lose_it(
    calling: Calling,
) -> None:
    """S3-18.3, both orders."""
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)

    await calling.world.close("pause", reason="wait")
    grant_again = await calling.world.column(
        "SELECT grant_id FROM assignments WHERE kind = 'build'"
    )
    paused = await calling.invocations.admit(worker, assignment, grant, PREPARED)
    assert paused.outcome == "refused"
    assert await calling.states() == []
    assert grant_again == [None]  # nothing had been done under it, so the pause took it back

    await calling.world.close("resume")
    invocation = await calling.admitted(worker)
    await calling.world.close("pause", reason="wait again")

    assert (await calling.assignment())[2] is True  # the call is the grant's first effect
    assert await calling.invocations.record(invocation, said()) == "returned"


async def test_a_pause_between_a_reply_that_could_not_be_used_and_the_next_call_stops_it(
    calling: Calling,
) -> None:
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    first = await calling.invocations.admit(worker, assignment, grant, PREPARED)
    assert first.invocation_id is not None
    await calling.invocations.record(first.invocation_id, said(), problem="unusable")

    await calling.world.close("pause", reason="wait")
    retry = await calling.invocations.admit(worker, assignment, grant, PREPARED)

    assert (retry.outcome, "paused" in retry.reason) == ("refused", True)
    assert await calling.states() == ["rejected"]


async def test_a_worker_that_is_gone_before_it_called_anything_leaves_the_work_wanted(
    calling: Calling,
) -> None:
    """S3-19.3: no call, so nothing to ask a person about."""
    worker = await calling.worker()
    await calling.claimed(worker)
    await worker.stop()

    await Features(
        calling.world.database,
        calling.world.reviews,
        calling.world.integration,
        invocations=calling.invocations,
    ).reconcile(PROJECT)

    assert await calling.assignment() == ("wanted", None, False)
    assert await calling.invocations.parked(PROJECT) == []
    assert await calling.admitted(await calling.worker())  # a live worker takes it up


async def test_a_live_workers_call_is_left_alone_whoever_else_starts(calling: Calling) -> None:
    """S3-18.2: another role starting says nothing about this worker."""
    builder = await calling.worker()
    await calling.admitted(builder)

    await calling.worker("qa")
    await calling.invocations.recover(PROJECT)
    await calling.world.features.start()  # and neither does the control plane starting

    assert await calling.states() == ["intended"]
    assert await calling.assignment() == ("granted", None, True)


async def test_a_worker_must_be_running_to_be_granted_work(calling: Calling) -> None:
    gone = await calling.worker()
    await gone.stop()
    async with calling.world.database.connection() as connection:
        assert not await is_alive(connection, gone.worker_id)

    with pytest.raises(ActionRefused, match="not running"):
        await calling.claimed(gone)


async def test_a_worker_gone_during_its_call_leaves_a_person_to_say_whether_to_call_again(
    calling: Calling,
) -> None:
    """S3-20.1, with two workers."""
    first = await calling.worker()
    invocation = await calling.admitted(first)
    await first.stop()

    await calling.invocations.recover(PROJECT)

    assert await calling.states() == ["uncertain"]
    [waiting] = await calling.invocations.parked(PROJECT)
    assert waiting.invocation_id == invocation
    second = await calling.worker()
    with pytest.raises(ActionRefused, match="parked"):
        await calling.claimed(second)

    retry = calling.person(invocation, "retry")
    assert await calling.invocations.decide(retry) == Receipt("effective", "retry_authorised")

    assert await calling.invocations.parked(PROJECT) == []
    assignment, grant = await calling.claimed(second)
    admission = await calling.invocations.admit(second, assignment, grant, PREPARED)
    assert (admission.outcome, admission.attempt) == ("admitted", 2)
    assert await calling.world.column("SELECT worker_id FROM invocations ORDER BY attempt") == [
        first.worker_id,
        second.worker_id,
    ]
    # The same request sent again is answered as it was, and buys nothing more.
    assert await calling.invocations.decide(retry) == Receipt("effective", "retry_authorised")
    assert admission.invocation_id is not None
    await calling.invocations.record(admission.invocation_id, UNCERTAIN)
    third = await calling.worker()
    with pytest.raises(ActionRefused, match="parked"):
        await calling.claimed(third)


async def test_a_retry_is_for_the_call_the_work_is_parked_on_and_only_a_persons(
    calling: Calling,
) -> None:
    worker = await calling.worker()
    invocation = await calling.admitted(worker)

    assert (await calling.invocations.decide(calling.person(invocation, "retry"))).disposition == (
        "stale"
    )  # it is not parked
    with pytest.raises(ActionRefused):
        await calling.invocations.decide(
            HumanAction(worker.principal, "r-1", str(invocation), "retry", {})
        )
    with pytest.raises(ActionRefused):
        await calling.invocations.decide(calling.person(invocation, "approve"))
    with pytest.raises(ActionRefused, match="no such call"):
        await calling.invocations.decide(calling.person(uuid.uuid7(), "retry"))

    assert await calling.states() == ["intended"]


async def test_work_set_aside_stays_parked_and_leaves_the_inbox(calling: Calling) -> None:
    invocation = await calling.admitted(await calling.worker())
    await calling.invocations.record(invocation, UNCERTAIN)

    receipt = await calling.invocations.decide(calling.person(invocation, "set_aside"))

    assert receipt == Receipt("effective", "set_aside")
    assert await calling.invocations.parked(PROJECT) == []
    assert (await calling.assignment())[1] is not None
    with pytest.raises(ActionRefused, match="parked"):
        await calling.claimed(await calling.worker())


async def test_a_result_that_arrives_late_and_first_is_used_and_the_retry_is_not(
    calling: Calling,
) -> None:
    """S3-19.2: the worker lost its connection, was declared gone, and then heard back."""
    first = await calling.worker()
    invocation = await calling.admitted(first)
    await first.stop()
    await calling.invocations.recover(PROJECT)
    await calling.invocations.decide(calling.person(invocation, "retry"))

    assert await calling.invocations.record(invocation, said()) == "returned"

    assert await calling.states() == ["returned"]
    assert await calling.assignment() == ("granted", None, True)
    assert await calling.invocations.parked(PROJECT) == []
    assert await calling.world.column("SELECT retry_authorised_by FROM invocations") == [None]
    second = await calling.worker()
    assert await calling.invocations.apply(invocation, second) == "applied"
    assert await calling.states() == ["applied"]


async def test_a_result_that_arrives_after_its_retry_was_admitted_is_kept_and_not_used(
    calling: Calling,
) -> None:
    """S3-19.2, the other order."""
    first = await calling.worker()
    invocation = await calling.admitted(first)
    await first.stop()
    await calling.invocations.recover(PROJECT)
    await calling.invocations.decide(calling.person(invocation, "retry"))
    second = await calling.worker()
    successor = await calling.admitted(second)

    assert await calling.invocations.record(invocation, said()) == "overtaken"

    assert await calling.states() == ["overtaken", "intended"]
    assert await calling.world.column(
        "SELECT provider_cost_usd FROM invocations WHERE attempt = 1"
    ) == [Decimal("0.0008912")]  # what it used is on record
    assert await calling.invocations.apply(invocation, second) == "overtaken"
    assert calling.applying.ran == []
    await calling.invocations.record(successor, said())
    assert await calling.invocations.apply(successor, second) == "applied"


async def test_an_apply_cut_short_is_carried_on_from_the_effect_it_had_reached(
    calling: Calling,
) -> None:
    """S3-18.4. The model is not called to finish an apply."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said())
    calling.applying.failing["second"] = RuntimeError("the process stopped")

    with pytest.raises(RuntimeError):
        await calling.invocations.apply(invocation, worker)
    del calling.applying.failing["second"]

    assert await calling.invocations.apply(invocation, worker) == "applied"
    assert calling.applying.ran == ["first:1", "second:1", "second:2"]
    assert await calling.states() == ["applied"]
    assert await calling.world.column(
        "SELECT detail->>'did' FROM invocation_effects ORDER BY position"
    ) == ["first", "second"]


async def test_two_deliveries_of_one_result_do_not_both_carry_out_an_effect(
    calling: Calling,
) -> None:
    """S3-19.4."""
    one, other = await calling.worker(), await calling.worker()
    invocation = await calling.admitted(one)
    await calling.invocations.record(invocation, said())
    calling.applying.waiting["first"] = asyncio.Event()

    applying = asyncio.create_task(calling.invocations.apply(invocation, one))
    await calling.applying.reached.wait()
    assert await calling.invocations.apply(invocation, other) == "busy"
    calling.applying.waiting["first"].set()

    assert await applying == "applied"
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_a_try_that_was_taken_over_while_it_ran_decides_nothing(calling: Calling) -> None:
    """S3-20.2: the worker lost its connection with its effect still running. Its try
    comes back first, while the try that took over is still running."""
    one, other = await calling.worker(), await calling.worker()
    invocation = await calling.admitted(one)
    await calling.invocations.record(invocation, said())
    calling.applying.waiting["first:1"] = asyncio.Event()
    calling.applying.waiting["first:2"] = asyncio.Event()
    first_try = asyncio.create_task(calling.invocations.apply(invocation, one))
    await calling.applying.reached.wait()
    await one.stop()
    calling.applying.reached.clear()
    second_try = asyncio.create_task(calling.invocations.apply(invocation, other))
    await calling.applying.reached.wait()

    calling.applying.waiting["first:1"].set()

    assert await first_try == "superseded"
    assert await calling.world.column("SELECT state FROM invocation_effects ORDER BY position") == [
        "started",
        "pending",
    ]  # the late try settled nothing
    calling.applying.waiting["first:2"].set()
    assert await second_try == "applied"
    assert calling.applying.ran == ["first:1", "first:2", "second:1"]


async def test_a_reply_the_service_refuses_is_a_known_call_that_gave_nothing(
    calling: Calling,
) -> None:
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said())
    calling.applying.failing["second"] = ActionRefused("the commit does not descend from the red")

    assert await calling.invocations.apply(invocation, worker) == "rejected"

    assert await calling.states() == ["rejected"]
    assert await calling.world.column("SELECT reason FROM invocations") == [
        "the commit does not descend from the red"
    ]
    assert await calling.assignment() == ("granted", None, True)


async def test_a_known_result_is_held_through_a_pause_and_applied_once_after_it(
    calling: Calling,
) -> None:
    """S3-20.3."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said())

    await calling.world.close("pause", reason="wait")
    assert await calling.invocations.apply(invocation, worker) == "held"
    assert (await calling.states(), calling.applying.ran) == (["returned"], [])

    await calling.world.close("resume")
    assert await calling.invocations.apply(invocation, worker) == "applied"
    assert calling.applying.ran == ["first:1", "second:1"]
    assert await calling.world.column("SELECT count(*) FROM invocations") == [1]


async def test_a_known_result_for_work_that_was_ended_is_never_applied(calling: Calling) -> None:
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said())

    await calling.world.close("cancel", reason="dropped")

    assert await calling.invocations.apply(invocation, worker) == "overtaken"
    assert (await calling.states(), calling.applying.ran) == (["overtaken"], [])
    assignment = await calling.owed()
    grant = uuid.uuid7()
    assert (await calling.invocations.admit(worker, assignment, grant, PREPARED)).outcome == (
        "finished"
    )


async def test_a_cancel_between_two_effects_stops_the_second(calling: Calling) -> None:
    """S3-19.4: retained, then cancelled, is not then published."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said())
    calling.applying.failing["second"] = RuntimeError("stopped between the two")
    with pytest.raises(RuntimeError):
        await calling.invocations.apply(invocation, worker)
    del calling.applying.failing["second"]

    await calling.world.close("cancel", reason="dropped")

    assert await calling.invocations.apply(invocation, worker) == "overtaken"
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_a_result_about_a_story_that_has_since_changed_is_not_applied(
    calling: Calling,
) -> None:
    """S3-20.3: revalidated before anything is done with it."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said())

    await calling.world.answer(await calling.world.ask("S1"))

    assert await calling.invocations.apply(invocation, worker) in ("overtaken", "held")
    await calling.world.reconfirm_everything()
    assert await calling.invocations.apply(invocation, worker) == "overtaken"
    assert calling.applying.ran == []
    assert await calling.world.column("SELECT reason FROM invocations") == [
        "what it was about has changed since the call was admitted"
    ]


async def test_the_pass_applies_what_a_worker_that_is_gone_had_already_been_told(
    calling: Calling,
) -> None:
    """S3-18.2: a known result is used once, under the grant it was obtained under."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said())
    await worker.stop()

    await calling.world.features.start()  # the control plane starting takes nothing back
    assert await calling.assignment() == ("granted", None, True)
    await calling.invocations.recover(PROJECT)

    assert await calling.states() == ["applied"]
    assert calling.applying.ran == ["first:1", "second:1"]
    await calling.invocations.recover(PROJECT)
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_a_build_that_was_reopened_admits_no_call(calling: Calling) -> None:
    """S3-20.4, one after the other: the reopening retires the build."""
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)

    await calling.world.behaviours.reopen_red(PROJECT, "B1", "the contract was wrong")

    assert (await calling.invocations.admit(worker, assignment, grant, PREPARED)).outcome == (
        "finished"
    )
    assert await calling.states() == []


async def test_a_grant_that_is_not_the_works_current_one_admits_nothing(calling: Calling) -> None:
    worker = await calling.worker()
    assignment, _ = await calling.claimed(worker)

    stale = await calling.invocations.admit(worker, assignment, uuid.uuid7(), PREPARED)

    assert (stale.outcome, "not the current one" in stale.reason) == ("refused", True)
    assert await calling.states() == []


async def test_what_was_recorded_is_not_recorded_over(calling: Calling) -> None:
    invocation = await calling.admitted(await calling.worker())
    await calling.invocations.record(invocation, said())

    assert await calling.invocations.record(invocation, UNCERTAIN) == "returned"

    assert await calling.states() == ["returned"]
    assert await calling.invocations.parked(PROJECT) == []
    assert await calling.world.column("SELECT reply FROM invocations") == [{"sum": 5}]


async def test_a_result_that_arrives_before_anyone_decided_ends_the_waiting(
    calling: Calling,
) -> None:
    """S3-19.2: declared gone, then heard from, with no person involved yet."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await worker.stop()
    await calling.invocations.recover(PROJECT)
    assert len(await calling.invocations.parked(PROJECT)) == 1

    assert await calling.invocations.record(invocation, said()) == "returned"

    assert await calling.invocations.parked(PROJECT) == []
    assert await calling.assignment() == ("granted", None, True)
    assert await calling.invocations.apply(invocation, await calling.worker()) == "applied"


async def test_an_estimate_is_of_the_whole_call_or_it_is_not_given(calling: Calling) -> None:
    """S3-18.10: one model of two that the table does not know."""
    invocation = await calling.admitted(await calling.worker())
    envelope = dict(said().terminal.envelope) if said().terminal else {}
    known = envelope["modelUsage"]["claude-haiku-5-5"]
    envelope["modelUsage"] = {"claude-haiku-5-5": known, "claude-unheard-of": known}

    await calling.invocations.record(
        invocation, ProviderResult(Outcome.RETURNED, Terminal(envelope))
    )

    async with calling.world.database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM invocations")).fetchone()
    assert row is not None
    assert (row["estimated_cost_usd"], row["price_table"]) == (None, None)
    assert row["provider_cost_usd"] == Decimal("0.0008912")  # what was observed still stands
    assert [each["model"] for each in row["usage"]] == ["claude-haiku-5-5", "claude-unheard-of"]


async def test_three_replies_the_service_refused_leave_a_person_to_decide(calling: Calling) -> None:
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    calling.applying.failing["first"] = ActionRefused("not a descendant of the red")
    for _ in range(3):
        admission = await calling.invocations.admit(worker, assignment, grant, PREPARED)
        assert admission.invocation_id is not None, admission.reason
        await calling.invocations.record(admission.invocation_id, said())
        assert await calling.invocations.apply(admission.invocation_id, worker) == "rejected"

    assert (await calling.assignment())[1] == "3 calls gave nothing that could be used"
    assert len(await calling.invocations.parked(PROJECT)) == 1


async def test_a_call_that_was_never_launched_is_not_one_of_the_three(calling: Calling) -> None:
    for _ in range(2):
        invocation = await calling.admitted(await calling.worker())
        await calling.invocations.record(invocation, NOT_STARTED)
    worker = await calling.worker()
    for _ in range(2):
        invocation = await calling.admitted(worker)
        await calling.invocations.record(invocation, said(), problem="unusable")

    assert (await calling.assignment())[1] is None  # four rows, two calls
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), problem="unusable")
    assert (await calling.assignment())[1] is not None


async def test_a_known_result_for_a_build_that_was_reopened_is_never_applied(
    calling: Calling,
) -> None:
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said())

    await calling.world.behaviours.reopen_red(PROJECT, "B1", "the contract was wrong")

    assert await calling.invocations.apply(invocation, worker) == "overtaken"
    assert calling.applying.ran == []
    assert await calling.world.column("SELECT reason FROM invocations") == [
        "the work it was for was ended"
    ]


async def test_what_an_apply_itself_changes_does_not_stop_the_rest_of_it(calling: Calling) -> None:
    """Whether a result is still about what there is, is asked before anything is done
    with it, not between its own effects."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said())

    async def adds_to_the_story() -> None:
        async with calling.world.database.connection() as connection:
            await connection.execute("UPDATE stories SET list_revision = list_revision + 1")

    calling.applying.doing["first"] = adds_to_the_story

    assert await calling.invocations.apply(invocation, worker) == "applied"
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_hearing_nothing_again_about_a_call_already_given_up_on_changes_nothing(
    calling: Calling,
) -> None:
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await worker.stop()
    await calling.invocations.recover(PROJECT)

    await calling.invocations.decide(calling.person(invocation, "set_aside"))

    assert await calling.invocations.record(invocation, UNCERTAIN) == "uncertain"

    assert await calling.invocations.parked(PROJECT) == []  # what the person set aside stays so
    assert await calling.states() == ["uncertain"]
