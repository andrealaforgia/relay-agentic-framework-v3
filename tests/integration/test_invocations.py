"""Calls to a provider cost money and cannot be taken back. So: one decision admits a
call; what it returned is on record before anything is done with it; its effects are
carried out once; and when a worker is gone, what it left is dealt with from the record,
never by asking the model again unless a person says so."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.features import Features
from relay.application.invocations import Effect, EffectRun, Invocations, Overtaken, Prepared
from relay.application.workers import WorkerSession, is_alive
from relay.domain.story import ActionRefused, HumanAction, Principal, PrincipalKind, Receipt
from relay.providers.claude import Outcome, ProviderResult
from relay.providers.definitions import AgentDefinition
from relay.providers.stream import Terminal, read_stream
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import PRODUCT_OWNER, VERIFIER
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
    read_on={},
)
# The commit a build's workspace was kept as.
KEPT = "c" * 40
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
        self.grants: list[uuid.UUID] = []
        self.settled: list[str] = []
        self.settling_refused: list[str] = []
        # What the first effect writes where it settles, returning what it changed.
        self.writing: Any = None
        self.reached = asyncio.Event()

    def effects_for(self, _: dict[str, Any]) -> list[Effect]:
        return [
            Effect("first", self._perform("first"), self._settle),
            Effect("second", self._perform("second")),
        ]

    async def _settle(self, connection: Any, run: EffectRun) -> Mapping[str, Any] | None:
        if self.settling_refused:
            raise ActionRefused(self.settling_refused.pop())
        self.settled.append(f"{run.effect_id.rsplit(':', 1)[1]}:{run.attempt}")
        return await self.writing(connection) if self.writing is not None else None

    def _perform(self, name: str) -> Any:
        async def perform(run: EffectRun) -> Mapping[str, Any] | None:
            this = f"{name}:{run.attempt}"
            self.ran.append(this)
            self.grants.append(run.grant_id)
            for held in (name, this):
                if held in self.waiting:
                    self.reached.set()
                    await self.waiting[held].wait()
            if name in self.failing:
                raise self.failing[name]
            if name in self.doing:
                await self.doing[name](run)
            return {"did": name}

        return perform


class Calling:
    """A project with one build owed, workers to do it, and the service under test."""

    def __init__(self, world: DecisionWorld) -> None:
        self.world = world
        self.applying = Applying()
        self.invocations = Invocations(world.database, self.applying.effects_for)
        # The pass of this project knows about calls: a resume and a start recover them.
        world.features = Features(
            world.database, world.reviews, world.integration, invocations=self.invocations
        )
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

    async def prepared(self) -> Prepared:
        """As a task prepares: on what the work is about as it stands now."""
        return replace(PREPARED, read_on=await self.invocations.as_it_stands(await self.owed()))

    async def admitted(self, worker: WorkerSession) -> uuid.UUID:
        assignment, grant = await self.claimed(worker)
        admission = await self.invocations.admit(worker, assignment, grant, await self.prepared())
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

    admission = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())

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
    first = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    assert first.invocation_id is not None

    during = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    assert (during.outcome, "in flight" in during.reason) == ("refused", True)

    await calling.invocations.record(first.invocation_id, said(), retained=KEPT)
    recorded = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    assert (recorded.outcome, recorded.invocation_id) == ("apply", first.invocation_id)

    assert await calling.invocations.apply(first.invocation_id, worker) == "applied"
    after = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    assert after.outcome == "finished"

    assert await calling.states() == ["applied"]
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_what_a_call_returned_is_stored_whole_with_what_it_used(calling: Calling) -> None:
    invocation = await calling.admitted(await calling.worker())

    assert await calling.invocations.record(invocation, said(), retained=KEPT) == "returned"

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
    first = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    assert first.invocation_id is not None

    state = await calling.invocations.record(
        first.invocation_id, said(), problem="the reply names no commit"
    )
    second = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())

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
        admission = await calling.invocations.admit(
            worker, assignment, grant, await calling.prepared()
        )
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
    paused = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    assert paused.outcome == "refused"
    assert await calling.states() == []
    assert grant_again == [None]  # nothing had been done under it, so the pause took it back

    await calling.world.close("resume")
    invocation = await calling.admitted(worker)
    await calling.world.close("pause", reason="wait again")

    assert (await calling.assignment())[2] is True  # the call is the grant's first effect
    assert await calling.invocations.record(invocation, said(), retained=KEPT) == "returned"


async def test_a_pause_between_a_reply_that_could_not_be_used_and_the_next_call_stops_it(
    calling: Calling,
) -> None:
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    first = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    assert first.invocation_id is not None
    await calling.invocations.record(first.invocation_id, said(), problem="unusable")

    await calling.world.close("pause", reason="wait")
    retry = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())

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
    admission = await calling.invocations.admit(second, assignment, grant, await calling.prepared())
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

    assert await calling.invocations.record(invocation, said(), retained=KEPT) == "returned"

    assert await calling.states() == ["returned"]
    assert await calling.assignment() == ("granted", None, True)
    assert await calling.invocations.parked(PROJECT) == []
    assert await calling.world.column("SELECT retry_authorised_by FROM invocations") == [None]
    second = await calling.worker()
    assert await calling.invocations.apply(invocation, second) == "applied"
    assert await calling.states() == ["applied"]


async def test_a_call_found_late_never_to_have_been_launched_is_not_one_that_ended(
    calling: Calling,
) -> None:
    """3B-S4-1.11: known now, and on record as what it was, whatever was admitted in
    its place. A feature's account of its calls does not count it."""
    first = await calling.worker()
    invocation = await calling.admitted(first)
    await first.stop()
    await calling.invocations.recover(PROJECT)
    await calling.invocations.decide(calling.person(invocation, "retry"))
    second = await calling.worker()
    successor = await calling.admitted(second)

    assert await calling.invocations.record(invocation, NOT_STARTED) == "not_started"

    assert await calling.states() == ["not_started", "intended"]
    # 3B-S4-2.1: and it settles nothing but itself. The work is its successor's, whose
    # grant stands and whose result is applied.
    assert await calling.assignment() == ("granted", None, True)
    assert await calling.invocations.record(successor, said(), retained=KEPT) == "returned"
    assert await calling.invocations.apply(successor, second) == "applied"


async def test_a_late_nothing_was_launched_leaves_the_grant_of_whoever_claimed_the_work_since(
    calling: Calling,
) -> None:
    """3B-S4-3.4: claimed again and not yet admitted a call. The grant the late report
    lets go of is its own, and that one is gone already."""
    first = await calling.worker()
    invocation = await calling.admitted(first)
    await first.stop()
    await calling.invocations.recover(PROJECT)
    await calling.invocations.decide(calling.person(invocation, "retry"))
    second = await calling.worker()
    assignment, grant = await calling.claimed(second)

    assert await calling.invocations.record(invocation, NOT_STARTED) == "not_started"

    assert await calling.assignment() == ("granted", None, True)
    admission = await calling.invocations.admit(second, assignment, grant, await calling.prepared())
    assert admission.outcome == "admitted", admission.reason


async def test_a_late_nothing_was_launched_does_not_clear_what_a_person_was_asked_of_its_successor(
    calling: Calling,
) -> None:
    """3B-S4-2.1, the other order: the successor has itself become uncertain, and a
    person is being asked about it. The first call's late report answers nothing of that:
    no third call is bought without the person saying so."""
    first = await calling.worker()
    invocation = await calling.admitted(first)
    await first.stop()
    await calling.invocations.recover(PROJECT)
    await calling.invocations.decide(calling.person(invocation, "retry"))
    second = await calling.worker()
    successor = await calling.admitted(second)
    assert await calling.invocations.record(successor, UNCERTAIN) == "uncertain"
    parked = await calling.assignment()
    assert parked[1] is not None

    assert await calling.invocations.record(invocation, NOT_STARTED) == "not_started"

    assert await calling.assignment() == parked
    assert len(await calling.invocations.parked(PROJECT)) == 1
    third = await calling.worker()
    with pytest.raises(ActionRefused, match="parked"):
        await calling.claimed(third)


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

    assert await calling.invocations.record(invocation, said(), retained=KEPT) == "overtaken"

    assert await calling.states() == ["overtaken", "intended"]
    assert await calling.world.column(
        "SELECT provider_cost_usd FROM invocations WHERE attempt = 1"
    ) == [Decimal("0.0008912")]  # what it used is on record
    assert await calling.invocations.apply(invocation, second) == "overtaken"
    assert calling.applying.ran == []
    await calling.invocations.record(successor, said(), retained=KEPT)
    assert await calling.invocations.apply(successor, second) == "applied"


async def test_an_apply_cut_short_is_carried_on_from_the_effect_it_had_reached(
    calling: Calling,
) -> None:
    """S3-18.4. The model is not called to finish an apply."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
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
    await calling.invocations.record(invocation, said(), retained=KEPT)
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
    await calling.invocations.record(invocation, said(), retained=KEPT)
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
    await calling.invocations.record(invocation, said(), retained=KEPT)
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
    await calling.invocations.record(invocation, said(), retained=KEPT)

    await calling.world.close("pause", reason="wait")
    assert await calling.invocations.apply(invocation, worker) == "held"
    assert (await calling.states(), calling.applying.ran) == (["returned"], [])

    await calling.world.close("resume")  # and nothing else: the worker is still alive

    assert await calling.states() == ["applied"]
    assert calling.applying.ran == ["first:1", "second:1"]
    assert await calling.world.column("SELECT count(*) FROM invocations") == [1]


async def test_a_known_result_for_work_that_was_ended_is_never_applied(calling: Calling) -> None:
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)

    await calling.world.close("cancel", reason="dropped")

    assert await calling.invocations.apply(invocation, worker) == "overtaken"
    assert (await calling.states(), calling.applying.ran) == (["overtaken"], [])
    assignment = await calling.owed()
    grant = uuid.uuid7()
    assert (
        await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    ).outcome == ("finished")


async def test_a_cancel_between_two_effects_stops_the_second(calling: Calling) -> None:
    """S3-19.4: retained, then cancelled, is not then published."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
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
    await calling.invocations.record(invocation, said(), retained=KEPT)

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
    await calling.invocations.record(invocation, said(), retained=KEPT)
    await worker.stop()

    await calling.world.features.start()  # takes nothing back, and applies what is known

    assert await calling.states() == ["applied"]
    assert calling.applying.ran == ["first:1", "second:1"]
    await calling.invocations.recover(PROJECT)
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_a_build_that_was_reopened_admits_no_call(calling: Calling) -> None:
    """S3-20.4, one after the other: the reopening retires the build."""
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)

    await calling.world.behaviours.reopen_red(VERIFIER, PROJECT, "B1", "the contract was wrong")

    assert (
        await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    ).outcome == ("finished")
    assert await calling.states() == []


async def test_a_grant_that_is_not_the_works_current_one_admits_nothing(calling: Calling) -> None:
    worker = await calling.worker()
    assignment, _ = await calling.claimed(worker)

    stale = await calling.invocations.admit(
        worker, assignment, uuid.uuid7(), await calling.prepared()
    )

    assert (stale.outcome, "not the current one" in stale.reason) == ("refused", True)
    assert await calling.states() == []


async def test_what_was_recorded_is_not_recorded_over(calling: Calling) -> None:
    invocation = await calling.admitted(await calling.worker())
    await calling.invocations.record(invocation, said(), retained=KEPT)

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

    assert await calling.invocations.record(invocation, said(), retained=KEPT) == "returned"

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
        invocation, ProviderResult(Outcome.RETURNED, Terminal(envelope)), retained=KEPT
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
        admission = await calling.invocations.admit(
            worker, assignment, grant, await calling.prepared()
        )
        assert admission.invocation_id is not None, admission.reason
        await calling.invocations.record(admission.invocation_id, said(), retained=KEPT)
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
    await calling.invocations.record(invocation, said(), retained=KEPT)

    await calling.world.behaviours.reopen_red(VERIFIER, PROJECT, "B1", "the contract was wrong")

    assert await calling.invocations.apply(invocation, worker) == "overtaken"
    assert calling.applying.ran == []
    assert await calling.world.column("SELECT reason FROM invocations") == [
        "the work it was for was ended"
    ]


async def test_what_an_effect_changes_where_it_settles_does_not_stop_the_rest(
    calling: Calling,
) -> None:
    """3B-S1-3.1: the change, what it leaves the story at, and the mark commit together,
    and the next effect expects what the first left."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)

    async def adds_to_the_story(connection: Any) -> dict[str, Any]:
        row = await (
            await connection.execute(
                "UPDATE stories SET list_revision = list_revision + 1 RETURNING list_revision"
            )
        ).fetchone()
        return {"list_revision": row["list_revision"]}

    calling.applying.writing = adds_to_the_story

    assert await calling.invocations.apply(invocation, worker) == "applied"
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_a_pass_that_comes_while_an_effect_runs_leaves_it_to_its_carrier(
    calling: Calling,
) -> None:
    """3B-S1-3.1: an effect being carried out is its carrier's to dispose of."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.waiting["first"] = asyncio.Event()
    applying = asyncio.create_task(calling.invocations.apply(invocation, worker))
    await calling.applying.reached.wait()

    await calling.invocations.recover(PROJECT)
    assert await calling.states() == ["returned"]
    calling.applying.waiting["first"].set()

    assert await applying == "applied"
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


async def test_an_effect_done_again_after_it_had_committed_is_done_once(calling: Calling) -> None:
    """3B-S1-1.1: the worker stopped after its effect committed and before it was marked.
    The effect is named the same on every try, so doing it again is doing it once."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    async with calling.world.database.connection() as connection:
        await connection.execute("CREATE TABLE published (effect_id text PRIMARY KEY)")
    stops = [RuntimeError("stopped after the commit")]

    async def publish(run: EffectRun) -> None:
        async with calling.world.database.connection() as connection:
            await connection.execute(
                "INSERT INTO published VALUES (%s) ON CONFLICT DO NOTHING", (run.effect_id,)
            )
        if stops:
            raise stops.pop()

    calling.applying.doing["first"] = publish
    with pytest.raises(RuntimeError):
        await calling.invocations.apply(invocation, worker)

    assert await calling.invocations.apply(invocation, worker) == "applied"

    assert calling.applying.ran == ["first:1", "first:2", "second:1"]
    assert await calling.world.column("SELECT effect_id FROM published") == [f"{invocation}:first"]


async def test_one_process_asked_twice_at_once_carries_an_effect_out_once(calling: Calling) -> None:
    """3B-S1-1.1: two passes overlapping, in the same process."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.waiting["first"] = asyncio.Event()

    applying = asyncio.create_task(calling.invocations.apply(invocation, worker))
    await calling.applying.reached.wait()
    assert await calling.invocations.apply(invocation, worker) == "busy"
    # And being told so lets go of nothing: it is still busy for whoever asks next.
    assert await calling.invocations.apply(invocation, worker) == "busy"
    calling.applying.waiting["first"].set()

    assert await applying == "applied"
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_what_an_effect_writes_is_written_with_its_being_done_or_not_at_all(
    calling: Calling,
) -> None:
    """3B-S1-1.1: only the try that is still current settles."""
    one, other = await calling.worker(), await calling.worker()
    invocation = await calling.admitted(one)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.waiting["first:1"] = asyncio.Event()
    first_try = asyncio.create_task(calling.invocations.apply(invocation, one))
    await calling.applying.reached.wait()
    await one.stop()

    assert await calling.invocations.apply(invocation, other) == "applied"
    calling.applying.waiting["first:1"].set()

    assert await first_try == "superseded"
    assert calling.applying.settled == ["first:2"]


async def test_a_try_that_was_taken_over_cannot_refuse_the_result_either(calling: Calling) -> None:
    """3B-S1-1.2: the old try is refused by its service while the new one is at work."""
    one, other = await calling.worker(), await calling.worker()
    invocation = await calling.admitted(one)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.waiting["first:1"] = asyncio.Event()
    calling.applying.waiting["first:2"] = asyncio.Event()
    refusing = [ActionRefused("the grant is not the current one")]

    async def refuses_the_first_time(_: EffectRun) -> None:
        if refusing:
            raise refusing.pop()

    calling.applying.doing["first"] = refuses_the_first_time
    first_try = asyncio.create_task(calling.invocations.apply(invocation, one))
    await calling.applying.reached.wait()
    await one.stop()
    calling.applying.reached.clear()
    second_try = asyncio.create_task(calling.invocations.apply(invocation, other))
    await calling.applying.reached.wait()

    calling.applying.waiting["first:1"].set()

    assert await first_try == "superseded"
    assert await calling.states() == ["returned"]
    calling.applying.waiting["first:2"].set()
    assert await second_try == "applied"
    assert await calling.states() == ["applied"]


async def test_a_call_prepared_from_what_has_since_changed_is_not_made(calling: Calling) -> None:
    """3B-S1-1.5: prepared, then a decision and its reconfirmations, then admission."""
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    first = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    assert first.invocation_id is not None
    await calling.invocations.record(first.invocation_id, said(), problem="unusable")
    written_before = await calling.prepared()
    await calling.world.answer(await calling.world.ask("S1"))
    await calling.world.reconfirm_everything()

    stale = await calling.invocations.admit(worker, assignment, grant, written_before)

    assert stale.outcome in ("stale", "refused")
    assert await calling.states() == ["rejected"]


async def test_a_decision_between_two_effects_is_not_taken_for_the_first_ones_doing(
    calling: Calling,
) -> None:
    """3B-S1-1.5."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.failing["second"] = RuntimeError("stopped between the two")
    with pytest.raises(RuntimeError):
        await calling.invocations.apply(invocation, worker)
    del calling.applying.failing["second"]

    await calling.world.answer(await calling.world.ask("S1"))
    await calling.world.reconfirm_everything()

    assert await calling.invocations.apply(invocation, worker) == "overtaken"
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_a_result_that_arrives_when_the_work_has_been_granted_again_is_used_under_that_grant(
    calling: Calling,
) -> None:
    """3B-S1-1.6: no second call is bought, and nobody acts under a grant that is not
    the work's."""
    first = await calling.worker()
    invocation = await calling.admitted(first)
    await first.stop()
    await calling.invocations.recover(PROJECT)
    await calling.invocations.decide(calling.person(invocation, "retry"))
    second = await calling.worker()
    assignment, new_grant = await calling.claimed(second)

    assert await calling.invocations.record(invocation, said(), retained=KEPT) == "returned"

    admission = await calling.invocations.admit(
        second, assignment, new_grant, await calling.prepared()
    )
    assert (admission.outcome, admission.invocation_id) == ("apply", invocation)
    assert await calling.invocations.apply(invocation, second) == "applied"
    assert await calling.states() == ["applied"]
    assert set(calling.applying.grants) == {new_grant}
    [ran_under] = await calling.world.column("SELECT grant_id FROM invocations")
    assert ran_under != new_grant  # what it ran under is still on record


async def test_a_grant_is_one_callers_one_workers_and_one_roles(calling: Calling) -> None:
    """3B-S1-1.7."""
    builder, other, reviewer = (
        await calling.worker(),
        await calling.worker(),
        await calling.worker("qa"),
    )
    assignment = await calling.owed()

    with pytest.raises(ActionRefused, match="not the one asking"):
        await calling.world.claims.claim(builder.principal, assignment, other.worker_id)
    with pytest.raises(ActionRefused):
        await calling.world.claims.claim(reviewer.principal, assignment, reviewer.worker_id)

    grant = await calling.world.claims.claim(builder.principal, assignment, builder.worker_id)
    elsewhere = await calling.invocations.admit(other, assignment, grant, await calling.prepared())
    assert (elsewhere.outcome, "not this worker's" in elsewhere.reason) == ("refused", True)
    assert await calling.states() == []


async def test_a_grant_with_no_worker_process_behind_it_admits_no_paid_call(
    calling: Calling,
) -> None:
    """3B-S1-1.7: the older way of claiming, for callers inside the control plane."""
    worker = await calling.worker()
    assignment = await calling.owed()
    grant = await calling.world.claims.claim(worker.principal, assignment)

    refused = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())

    assert refused.outcome == "refused"
    assert await calling.states() == []


async def test_a_call_in_flight_for_work_that_was_ended_is_settled_without_asking_anyone(
    calling: Calling,
) -> None:
    """3B-S1-1.8: the build is reopened, then its worker is lost."""
    worker = await calling.worker()
    await calling.admitted(worker)
    await calling.world.behaviours.reopen_red(VERIFIER, PROJECT, "B1", "the contract was wrong")
    await worker.stop()

    await calling.invocations.recover(PROJECT)

    assert await calling.states() == ["uncertain"]  # still not known, and on record as so
    assert await calling.invocations.parked(PROJECT) == []
    assert (await calling.assignment())[:2] == ("retired", None)


async def test_what_is_learned_of_a_call_for_ended_work_is_kept_and_asks_nothing(
    calling: Calling,
) -> None:
    """3B-S1-1.8, the other order."""
    worker = await calling.worker()
    lost = await calling.admitted(worker)
    await calling.world.behaviours.reopen_red(VERIFIER, PROJECT, "B1", "the contract was wrong")

    assert await calling.invocations.record(lost, UNCERTAIN) == "uncertain"
    assert await calling.invocations.parked(PROJECT) == []

    assert (await calling.assignment())[:2] == ("retired", None)


async def test_a_build_that_returned_with_nothing_kept_of_it_is_not_a_result(
    calling: Calling,
) -> None:
    """3B-S1-1.10."""
    invocation = await calling.admitted(await calling.worker())

    assert await calling.invocations.record(invocation, said()) == "rejected"

    async with calling.world.database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM invocations")).fetchone()
    assert row is not None
    assert row["reason"] == "nothing was kept of what the build made"
    assert (row["envelope"]["subtype"], row["reply"]) == ("success", None)
    assert row["provider_cost_usd"] == Decimal("0.0008912")


async def test_a_result_the_adapter_says_must_not_be_used_is_kept_and_is_not_a_reply(
    calling: Calling,
) -> None:
    """3B-S1-1.9, at the record."""
    invocation = await calling.admitted(await calling.worker())
    unverified = replace(said(), unusable="the run reported a result without saying what it had")

    assert await calling.invocations.record(invocation, unverified, retained=KEPT) == "rejected"

    async with calling.world.database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM invocations")).fetchone()
    assert row is not None
    assert row["reason"] == "the run reported a result without saying what it had"
    assert (row["reply"], row["provider_cost_usd"]) == (None, Decimal("0.0008912"))


async def test_a_worker_registered_for_one_role_cannot_be_claimed_under_as_another(
    calling: Calling,
) -> None:
    """3B-S1-1.7: a caller that names itself after another role's process."""
    reviewer = await calling.worker("qa")
    forged = Principal(PrincipalKind.WORKER, f"builder@{reviewer.worker_id}", role="builder")

    with pytest.raises(ActionRefused, match="not the one asking"):
        await calling.world.claims.claim(forged, await calling.owed(), reviewer.worker_id)


async def test_a_result_whose_grant_no_longer_holds_the_work_is_overtaken_not_defective(
    calling: Calling,
) -> None:
    """3B-S1-1.6. No entry point I know of takes a grant back from under a known
    result, so the grant is moved here by hand: this is the fence, not a history."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    async with calling.world.database.connection() as connection:
        await connection.execute(
            "UPDATE assignments SET grant_id = %s WHERE kind = 'build'", (uuid.uuid7(),)
        )

    assert await calling.invocations.apply(invocation, worker) == "overtaken"

    assert calling.applying.ran == []
    assert await calling.world.column("SELECT count(*) FROM invocations") == [1]
    # Nobody is asked about a bad reply, and whoever holds the work now keeps it.
    assert await calling.assignment() == ("granted", None, True)


async def test_a_result_adopted_under_a_new_grant_is_held_by_a_pause_and_not_lost_to_it(
    calling: Calling,
) -> None:
    """3B-S1-2.3: the grant check reached by a real history. Having a known result to
    apply is the new grant's first effect, so the pause does not take it back."""
    first = await calling.worker()
    invocation = await calling.admitted(first)
    await first.stop()
    await calling.invocations.recover(PROJECT)
    await calling.invocations.decide(calling.person(invocation, "retry"))
    second = await calling.worker()
    _, new_grant = await calling.claimed(second)
    await calling.invocations.record(invocation, said(), retained=KEPT)

    await calling.world.close("pause", reason="wait")
    assert await calling.invocations.apply(invocation, second) == "held"
    await calling.world.close("resume")

    assert await calling.states() == ["applied"]
    assert set(calling.applying.grants) == {new_grant}
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_a_retry_claimed_by_a_worker_that_then_went_is_still_there_for_the_next(
    calling: Calling,
) -> None:
    """3B-S1-2.4, with three workers and one successor call."""
    first = await calling.worker()
    invocation = await calling.admitted(first)
    await first.stop()
    await calling.invocations.recover(PROJECT)
    await calling.invocations.decide(calling.person(invocation, "retry"))
    second = await calling.worker()
    await calling.claimed(second)
    await second.stop()

    await calling.invocations.recover(PROJECT)

    assert await calling.assignment() == ("wanted", None, False)
    third = await calling.worker()
    successor = await calling.admitted(third)
    assert await calling.states() == ["uncertain", "intended"]
    assert await calling.world.column(
        f"SELECT worker_id FROM invocations WHERE invocation_id = '{successor}'"
    ) == [third.worker_id]


async def test_a_decision_made_while_an_effect_runs_stops_what_it_would_have_written(
    calling: Calling,
) -> None:
    """3B-S1-2.5."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.waiting["first"] = asyncio.Event()
    applying = asyncio.create_task(calling.invocations.apply(invocation, worker))
    await calling.applying.reached.wait()

    await calling.world.answer(await calling.world.ask("S1"))
    await calling.world.reconfirm_everything()
    calling.applying.waiting["first"].set()

    assert await applying == "overtaken"
    assert calling.applying.settled == []
    assert await calling.states() == ["overtaken"]
    assert calling.applying.ran == ["first:1"]


async def test_a_stop_that_arrives_while_an_effect_runs_holds_what_it_would_have_written(
    calling: Calling,
) -> None:
    """3B-S1-2.5: paused during `perform`. Nothing is written until the pause clears."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.waiting["first:1"] = asyncio.Event()
    applying = asyncio.create_task(calling.invocations.apply(invocation, worker))
    await calling.applying.reached.wait()

    await calling.world.close("pause", reason="wait")
    calling.applying.waiting["first:1"].set()

    assert await applying == "held"
    assert calling.applying.settled == []
    await calling.world.close("resume")
    assert await calling.states() == ["applied"]
    assert calling.applying.settled == ["first:2"]


async def test_a_refusal_where_an_effect_writes_is_a_reply_that_gave_nothing(
    calling: Calling,
) -> None:
    """3B-S1-2.6."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.settling_refused.append("the contract names no observation")

    assert await calling.invocations.apply(invocation, worker) == "rejected"

    assert await calling.states() == ["rejected"]
    assert await calling.world.column("SELECT reason FROM invocations") == [
        "the contract names no observation"
    ]
    assert await calling.world.column("SELECT state FROM invocation_effects ORDER BY position") == [
        "started",
        "pending",
    ]  # nothing of it was written


async def test_an_apply_cancelled_just_as_it_took_an_effect_does_not_leave_it_taken(
    calling: Calling, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3B-S1-2.7: the effect is durably started, the session lives, and nothing is
    carrying it out."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    real = Invocations._take
    taken = asyncio.Event()

    async def taken_and_never_heard_of(self: Invocations, *arguments: Any) -> Any:
        await real(self, *arguments)
        taken.set()
        await asyncio.Event().wait()

    with monkeypatch.context() as patched:
        patched.setattr(Invocations, "_take", taken_and_never_heard_of)
        applying = asyncio.create_task(calling.invocations.apply(invocation, worker))
        await taken.wait()
        applying.cancel()
        with pytest.raises(asyncio.CancelledError):
            await applying

    assert await calling.invocations.apply(invocation, worker) == "applied"
    assert calling.applying.ran == ["first:2", "second:1"]


async def test_ending_the_work_takes_its_parked_call_out_of_the_inbox(calling: Calling) -> None:
    """3B-S1-2.9."""
    invocation = await calling.admitted(await calling.worker())
    await calling.invocations.record(invocation, UNCERTAIN)
    assert len(await calling.invocations.parked(PROJECT)) == 1

    await calling.world.behaviours.reopen_red(VERIFIER, PROJECT, "B1", "the contract was wrong")

    assert await calling.invocations.parked(PROJECT) == []
    assert (await calling.invocations.decide(calling.person(invocation, "retry"))).disposition == (
        "stale"
    )
    assert await calling.world.column("SELECT retry_authorised_by FROM invocations") == [None]


async def test_two_passes_at_once_recover_through_one_session(
    calling: Calling, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3B-S1-2.10. Starting a session takes long enough for the second pass to arrive."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    await worker.stop()
    real = WorkerSession.start

    async def slowly(pool: Any, role: str) -> WorkerSession:
        await asyncio.sleep(0.3)
        session = await real(pool, role)
        calling.sessions.append(session)
        return session

    monkeypatch.setattr(WorkerSession, "start", slowly)

    await asyncio.gather(calling.invocations.recover(PROJECT), calling.invocations.recover(PROJECT))

    assert await calling.world.column(
        "SELECT count(*) FROM workers WHERE role = 'control-plane'"
    ) == [1]
    assert await calling.states() == ["applied"]
    assert calling.applying.ran == ["first:1", "second:1"]


async def test_an_apply_cut_short_in_a_worker_that_lives_on_is_finished_by_the_pass(
    calling: Calling, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3B-S1-3.2: the pass is another session, and can only ask whether the worker is
    alive. So the worker gives the effect up on record when it stops carrying it out."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    real = Invocations._take
    taken = asyncio.Event()

    async def taken_and_never_heard_of(self: Invocations, *arguments: Any) -> Any:
        await real(self, *arguments)
        taken.set()
        await asyncio.Event().wait()

    with monkeypatch.context() as patched:
        patched.setattr(Invocations, "_take", taken_and_never_heard_of)
        applying = asyncio.create_task(calling.invocations.apply(invocation, worker))
        await taken.wait()
        applying.cancel()
        with pytest.raises(asyncio.CancelledError):
            await applying

    await calling.invocations.recover(PROJECT)

    assert await calling.states() == ["applied"]
    assert calling.applying.ran == ["first:2", "second:1"]
    assert await calling.world.column("SELECT count(*) FROM invocations") == [1]


async def test_a_refusal_that_comes_after_the_work_was_ended_blames_no_reply(
    calling: Calling,
) -> None:
    """3B-S1-3.4: the third attempt, its effect running, and the red reopened under it."""
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    for _ in range(2):
        admission = await calling.invocations.admit(
            worker, assignment, grant, await calling.prepared()
        )
        assert admission.invocation_id is not None
        await calling.invocations.record(admission.invocation_id, said(), problem="unusable")
    third = await calling.invocations.admit(worker, assignment, grant, await calling.prepared())
    assert third.invocation_id is not None
    await calling.invocations.record(third.invocation_id, said(), retained=KEPT)
    calling.applying.waiting["first"] = asyncio.Event()
    calling.applying.failing["first"] = ActionRefused("this grant is not the current one")
    applying = asyncio.create_task(calling.invocations.apply(third.invocation_id, worker))
    await calling.applying.reached.wait()

    await calling.world.behaviours.reopen_red(VERIFIER, PROJECT, "B1", "the contract was wrong")
    calling.applying.waiting["first"].set()

    assert await applying == "overtaken"
    assert await calling.states() == ["rejected", "rejected", "overtaken"]
    assert await calling.invocations.parked(PROJECT) == []
    assert await calling.world.column(
        "SELECT count(*) FROM inbox_items WHERE kind = 'invocation'"
    ) == [0]


async def test_work_finished_by_other_means_is_not_retried_and_its_late_result_does_nothing(
    calling: Calling,
) -> None:
    """3B-S1-3.5. The reachable history is a correction parked on a call and then
    settled by a roadmap approval, which finishes the assignment without retiring it.
    Here the same state is reached on the build by finishing it directly: what is under
    test is that finished work is neither retried nor acted on."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await worker.stop()
    await calling.invocations.recover(PROJECT)
    assert len(await calling.invocations.parked(PROJECT)) == 1
    async with calling.world.database.connection() as connection:
        # As a roadmap approval finishes a parked correction: done, with no grant on it.
        await connection.execute("UPDATE assignments SET state = 'done' WHERE kind = 'build'")

    assert await calling.invocations.parked(PROJECT) == []
    assert (await calling.invocations.decide(calling.person(invocation, "retry"))).disposition == (
        "stale"
    )
    assert await calling.invocations.record(invocation, said(), retained=KEPT) == "returned"
    assert await calling.invocations.apply(invocation, await calling.worker()) == "overtaken"
    assert calling.applying.ran == []
    assert await calling.world.column("SELECT reason FROM invocations") == [
        "the work it was for was finished by other means"
    ]


async def test_asking_whether_a_worker_is_alive_takes_nothing(calling: Calling) -> None:
    """3B-S1-3.7: many observers at once of a worker that is gone, and of one that is not."""
    gone, here = await calling.worker(), await calling.worker()
    await gone.stop()

    async def ask(worker: WorkerSession) -> bool:
        async with calling.world.database.connection() as connection:
            return await is_alive(connection, worker.worker_id)

    assert await asyncio.gather(*(ask(gone) for _ in range(6))) == [False] * 6
    assert await asyncio.gather(*(ask(here) for _ in range(6))) == [True] * 6
    assert await ask(gone) is False


async def test_a_call_given_up_on_and_then_found_never_to_have_started_cost_nothing(
    calling: Calling,
) -> None:
    """3B-S1-3.8: recovery said uncertain; the adapter then established there was no launch."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await worker.stop()
    await calling.invocations.recover(PROJECT)
    assert len(await calling.invocations.parked(PROJECT)) == 1

    assert await calling.invocations.record(invocation, NOT_STARTED) == "not_started"

    assert await calling.invocations.parked(PROJECT) == []
    assert await calling.assignment() == ("wanted", None, False)


async def test_giving_an_effect_up_does_not_take_it_from_whoever_took_it_over(
    calling: Calling,
) -> None:
    """3B-S1-3.2: the worker was taken for gone, another took its effect over, and then
    its own call was cut short. What it gives up is what is still its own."""
    one, other = await calling.worker(), await calling.worker()
    invocation = await calling.admitted(one)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.waiting["first:1"] = asyncio.Event()
    calling.applying.waiting["first:2"] = asyncio.Event()
    first_try = asyncio.create_task(calling.invocations.apply(invocation, one))
    await calling.applying.reached.wait()
    await one.stop()
    calling.applying.reached.clear()
    second_try = asyncio.create_task(calling.invocations.apply(invocation, other))
    await calling.applying.reached.wait()

    first_try.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_try

    assert await calling.world.column(
        "SELECT state, worker_id FROM invocation_effects WHERE name = 'first'"
    ) == ["started"]
    assert await calling.world.column(
        "SELECT worker_id FROM invocation_effects WHERE name = 'first'"
    ) == [other.worker_id]
    calling.applying.waiting["first:2"].set()
    assert await second_try == "applied"
    assert calling.applying.ran == ["first:1", "first:2", "second:1"]


async def test_an_effect_being_let_go_is_not_taken_again_until_it_is_free_on_record(
    calling: Calling, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3B-S2-2.7: a call that was cut short lets its effect go, which takes a moment. A
    second call of the same process that took the effect in that moment would have its
    own try set free by the first one's letting go, while it was carrying it out."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.waiting["first:1"] = asyncio.Event()
    letting_go, may_let_go = asyncio.Event(), asyncio.Event()
    real = Invocations._give_up

    async def slowly(self: Invocations, *arguments: Any) -> None:
        letting_go.set()
        await may_let_go.wait()
        await real(self, *arguments)

    monkeypatch.setattr(Invocations, "_give_up", slowly)
    first = asyncio.create_task(calling.invocations.apply(invocation, worker))
    await calling.applying.reached.wait()
    first.cancel()
    await letting_go.wait()

    assert await calling.invocations.apply(invocation, worker) == "busy"

    may_let_go.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await calling.invocations.apply(invocation, worker) == "applied"
    assert calling.applying.ran == ["first:1", "first:2", "second:1"]


STRICT = {
    "type": "object",
    "properties": {"sum": {"type": "string"}},
    "required": ["sum"],
    "additionalProperties": False,
}


async def test_a_result_is_checked_against_the_schema_stored_with_its_call_whoever_brings_it(
    calling: Calling,
) -> None:
    """3B-S2-2.6: the record takes nobody's word that a reply was checked. A result
    handed straight to it, late or by other hands than the worker's, is held to the
    schema that was stored when its call was admitted."""
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    admission = await calling.invocations.admit(
        worker, assignment, grant, replace(await calling.prepared(), schema=STRICT)
    )
    assert admission.invocation_id is not None

    assert await calling.invocations.record(admission.invocation_id, said(), retained=KEPT) == (
        "rejected"
    )

    async with calling.world.database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM invocations")).fetchone()
    assert row is not None
    assert row["reason"] == "sum of the reply is not a string"
    assert row["reply"] is None and row["usage"] and row["envelope"]["structured_output"]
    await calling.invocations.recover(PROJECT)
    assert calling.applying.ran == []


async def test_a_call_that_returned_no_structured_answer_is_not_a_result(calling: Calling) -> None:
    """3B-S2-2.6."""
    invocation = await calling.admitted(await calling.worker())
    nothing = ProviderResult(
        Outcome.RETURNED, Terminal({"type": "result", "subtype": "success", "is_error": False})
    )

    assert await calling.invocations.record(invocation, nothing, retained=KEPT) == "rejected"

    [reason] = await calling.world.column("SELECT reason FROM invocations")
    assert reason == "the reply did not carry the structured answer it was asked for"


async def test_no_call_is_admitted_on_a_schema_its_reply_could_not_be_checked_against(
    calling: Calling,
) -> None:
    """3B-S2-2.6: found before anything is paid for, not when the reply arrives."""
    worker = await calling.worker()
    assignment, grant = await calling.claimed(worker)
    unchecked = {"type": "object", "properties": {"sum": {"type": "integer", "minimum": 0}}}

    with pytest.raises(ValueError, match="minimum"):
        await calling.invocations.admit(
            worker, assignment, grant, replace(await calling.prepared(), schema=unchecked)
        )

    assert await calling.states() == []


async def test_work_overtaken_through_nobodys_fault_is_wanted_again_until_a_person_is_asked(
    calling: Calling,
) -> None:
    """3B-S2-2.3: a result that is applied to nothing, for work that is still owed and
    still held by the grant the result acted under, does not leave the work there. It
    is wanted again and announced again. And after as many calls as a person is asked
    after, a person is asked: nobody's fault is still somebody's money."""
    worker = await calling.worker()
    calling.applying.failing["first"] = Overtaken("what it was written from is gone")
    announced_again = (
        "SELECT count(*) FROM outbox o JOIN assignments a ON a.command_message_id = o.message_id "
        "WHERE a.kind = 'build' AND o.published_at IS NULL"
    )

    for call in (1, 2, 3):
        invocation = await calling.admitted(worker)
        await calling.invocations.record(invocation, said(), retained=KEPT)
        async with calling.world.database.connection() as connection:
            await connection.execute("UPDATE outbox SET published_at = now()")  # delivered

        assert await calling.invocations.apply(invocation, worker) == "overtaken"

        if call < 3:
            assert await calling.assignment() == ("wanted", None, False)
            assert await calling.world.column(announced_again) == [1]

    assert await calling.states() == ["overtaken", "overtaken", "overtaken"]
    assert await calling.assignment() == (
        "wanted",
        "3 calls gave nothing that could be used",
        False,
    )
    assert len(await calling.invocations.parked(PROJECT)) == 1
    assert await calling.world.column(announced_again) == [0]  # not announced: it is parked


async def test_a_grant_hands_back_only_what_it_does_not_own(calling: Calling) -> None:
    """3B-S2-2.4: what a grant owns is asked now, not whether it ever made a call. With
    a known result waiting under it, its own or one it adopted, it keeps the work. With
    a last call that gave nothing, it owns nothing, and the work goes back."""
    one = await calling.worker()
    invocation = await calling.admitted(one)
    assignment, first_grant = await calling.claimed(one)
    await calling.world.claims.give_back(assignment, first_grant)  # with its call in flight
    assert await calling.assignment() == ("granted", None, True)
    await calling.invocations.record(invocation, UNCERTAIN)
    await calling.invocations.decide(calling.person(invocation, "retry"))
    two = await calling.worker()
    assignment, second_grant = await calling.claimed(two)
    await calling.invocations.record(invocation, said(), retained=KEPT)  # adopted under it

    await calling.world.claims.give_back(assignment, second_grant)

    assert await calling.assignment() == ("granted", None, True)
    calling.applying.failing["first"] = ActionRefused("the service would not have it")
    assert await calling.invocations.apply(invocation, two) == "rejected"

    await calling.world.claims.give_back(assignment, second_grant)

    assert await calling.assignment() == ("wanted", None, False)


async def test_an_effect_that_could_not_be_let_go_on_record_is_not_busy_for_ever(
    calling: Calling, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the call's claim in its own process is dropped whatever became of the record:
    if the record could not be written, the effect is still on record as started by
    this process, and only this process not claiming it lets anyone take it again."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.waiting["first:1"] = asyncio.Event()
    real = Invocations._give_up

    async def with_the_database_gone(self: Invocations, *arguments: Any) -> None:
        pool, self._pool = self._pool, None  # type: ignore[assignment]
        try:
            await real(self, *arguments)
        finally:
            self._pool = pool

    monkeypatch.setattr(Invocations, "_give_up", with_the_database_gone)
    first = asyncio.create_task(calling.invocations.apply(invocation, worker))
    await calling.applying.reached.wait()
    first.cancel()
    with pytest.raises(AttributeError):
        await first

    assert await calling.invocations.apply(invocation, worker) == "applied"
    assert calling.applying.ran == ["first:1", "first:2", "second:1"]


async def test_a_refusal_cut_short_before_it_was_on_record_does_not_leave_the_effect_taken(
    calling: Calling, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3B-S2-3.4: the service refused, and the call was cancelled while it waited to
    put that on record. Its worker lives on. The effect must not stay on record as
    being carried out by it: the pass would find it busy for ever."""
    worker = await calling.worker()
    invocation = await calling.admitted(worker)
    await calling.invocations.record(invocation, said(), retained=KEPT)
    calling.applying.failing["first"] = ActionRefused("the service would not have it")
    waiting = asyncio.Event()

    async def waits_for_a_lock(self: Invocations, *arguments: Any, **named: Any) -> str:
        waiting.set()
        await asyncio.Event().wait()
        return ""

    with monkeypatch.context() as patched:
        patched.setattr(Invocations, "_reject", waits_for_a_lock)
        applying = asyncio.create_task(calling.invocations.apply(invocation, worker))
        await waiting.wait()
        applying.cancel()
        with pytest.raises(asyncio.CancelledError):
            await applying

    await calling.invocations.recover(PROJECT)

    assert await calling.states() == ["rejected"]
