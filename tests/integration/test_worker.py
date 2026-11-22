"""One piece of work carried out by a worker: claimed, called for, recorded, applied.
The provider here is scripted, or is the stand-in for the CLI replaying a real stream."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.invocations import Effect, EffectRun, Invocations
from relay.application.worker import Call, Worker
from relay.application.workers import WorkerSession
from relay.domain.story import ActionRefused
from relay.providers.claude import (
    ClaudeAdapter,
    Confinement,
    ProviderResult,
    ProviderUnavailable,
    Request,
)
from relay.providers.confinement import boundary_digest
from tests.integration.decision_world import DecisionWorld
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_invocations import KEPT, NOT_STARTED, PREPARED, UNCERTAIN, said
from tests.integration.trunk_world import PROJECT

RECORDED = Path(__file__).parent.parent / "fixtures" / "claude"


RUNTIME = {"cli": "9.9.9 (stub)", "os": "test"}


class SeenToHold:
    """Stands in for the record of boundary passes: every boundary holds, until told not."""

    def __init__(self) -> None:
        self.holding = True
        self.asked: list[str] = []

    async def holds(self, digest: str) -> bool:
        self.asked.append(digest)
        return self.holding


class Scripted:
    """A provider that gives what it was told to, in order, and remembers what it was asked."""

    def __init__(self, *results: ProviderResult | BaseException, then_waits: bool = False) -> None:
        self._results = list(results)
        self.asked: list[Request] = []
        # Reports what it has, and then never returns: a run that lingers after its result.
        self.then_waits = then_waits
        self.waiting = asyncio.Event()
        # Never gets as far as launching anything: a check of the CLI that hangs.
        self.waits_before_launch = False

    async def invoke(
        self, request: Request, *, on_result: Any = None, on_start: Any = None
    ) -> ProviderResult:
        self.asked.append(request)
        if self.waits_before_launch:
            self.waiting.set()
            await asyncio.Event().wait()
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if on_start is not None:
            on_start(4242)
        if result.terminal is not None and on_result is not None:
            on_result(result)
        if self.then_waits:
            self.waiting.set()
            await asyncio.Event().wait()
        return result


class Summing:
    """A task whose reply must be a sum, and which refuses a wrong one."""

    def __init__(self, workdir: Path, invocations: Invocations) -> None:
        self._workdir = workdir
        self._invocations = invocations
        self.problems: list[str | None] = []
        # Set to make the next preparation read what the work is about too early.
        self.reads_too_early = False
        self.cannot_keep: Exception | None = None
        self.keeping: asyncio.Event | None = None
        self.at_keeping = asyncio.Event()
        self.during_preparation: Any = None

    async def prepare(self, assignment: Mapping[str, Any], last_problem: str | None) -> Call:
        self.problems.append(last_problem)
        if self.during_preparation is not None:
            await self.during_preparation()
        read_on = await self._invocations.as_it_stands(assignment["assignment_id"])
        if self.reads_too_early:
            self.reads_too_early = False
            read_on = {**read_on, "basis": read_on["basis"] - 1}
        return Call(
            replace(PREPARED, read_on=read_on), self._workdir, Confinement((), {}), timeout=30.0
        )

    async def retain(self, assignment: Mapping[str, Any], call: Call) -> str | None:
        if self.keeping is not None:
            self.at_keeping.set()
            await self.keeping.wait()
        if self.cannot_keep is not None:
            raise self.cannot_keep
        return KEPT

    def problem_with(self, reply: Any) -> str | None:
        return None if isinstance(reply.get("sum"), int) else "the reply has no sum"


class Bench:
    def __init__(self, world: DecisionWorld, tmp_path: Path) -> None:
        self.world = world
        self.applied: list[Any] = []
        self.refusing: list[str] = []
        self.invocations = Invocations(world.database, self._effects)
        self.task = Summing(tmp_path, self.invocations)
        self.session: WorkerSession | None = None
        self.boundaries = SeenToHold()

    def _effects(self, invocation: dict[str, Any]) -> list[Effect]:
        async def use(run: EffectRun) -> None:
            if self.refusing:
                raise ActionRefused(self.refusing.pop(0))
            self.applied.append(run.invocation["reply"])

        return [Effect("use", use)]

    async def worker(self, provider: Any) -> Worker:
        self.session = self.session or await WorkerSession.start(self.world.database, "builder")
        return Worker(
            self.session,
            self.world.claims,
            self.invocations,
            provider,
            {"build": self.task},
            self.boundaries,
            RUNTIME,
        )

    async def owed(self) -> dict[str, Any]:
        async with self.world.database.connection() as connection:
            row = await (
                await connection.execute("SELECT * FROM assignments WHERE kind = 'build'")
            ).fetchone()
        assert row is not None
        return row

    async def states(self) -> list[object]:
        return await self.world.column("SELECT state FROM invocations ORDER BY attempt")


@pytest.fixture
async def bench(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> AsyncIterator[Bench]:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    bench = Bench(world, tmp_path)
    yield bench
    await bench.invocations.close()
    if bench.session is not None:
        await bench.session.stop()


async def test_work_is_claimed_called_for_recorded_and_applied(bench: Bench) -> None:
    provider = Scripted(said())
    worker = await bench.worker(provider)

    assert await worker.carry_out(await bench.owed()) == "applied"

    assert bench.applied == [{"sum": 5}]
    assert await bench.states() == ["applied"]
    [asked] = provider.asked
    assert await bench.world.column("SELECT invocation_id FROM invocations") == [
        asked.invocation_id
    ]  # the provider's session is the call Relay recorded first
    assert (asked.briefing, asked.model) == ("Make B1 pass.", "claude-haiku-5-5")


async def test_the_grant_a_worker_works_under_is_recorded_as_that_workers(bench: Bench) -> None:
    """So that the pass can tell whose it was if the worker is gone (S3-19.3)."""
    worker = await bench.worker(Scripted(UNCERTAIN))
    owed = await bench.owed()
    assert bench.session is not None

    seen: list[object] = []
    real = bench.invocations.admit

    async def admit_and_look(*arguments: Any) -> Any:
        seen.extend(
            await bench.world.column("SELECT granted_worker FROM assignments WHERE kind = 'build'")
        )
        return await real(*arguments)

    bench.invocations.admit = admit_and_look  # type: ignore[method-assign]
    await worker.carry_out(owed)

    assert seen == [bench.session.worker_id]


async def test_the_same_work_delivered_again_calls_nothing(bench: Bench) -> None:
    provider = Scripted(said())
    worker = await bench.worker(provider)
    owed = await bench.owed()
    await worker.carry_out(owed)

    assert await worker.carry_out(owed) == "finished"

    assert (len(provider.asked), bench.applied) == (1, [{"sum": 5}])


async def test_a_reply_that_cannot_be_used_is_asked_for_again_saying_what_was_wrong(
    bench: Bench,
) -> None:
    unusable = said()
    assert unusable.terminal is not None
    unusable.terminal.envelope["structured_output"] = {"total": 5}
    provider = Scripted(unusable, said())
    worker = await bench.worker(provider)

    assert await worker.carry_out(await bench.owed()) == "applied"

    assert await bench.states() == ["rejected", "applied"]
    assert bench.task.problems == [None, "the reply has no sum"]
    assert bench.applied == [{"sum": 5}]


async def test_a_reply_with_no_structured_answer_is_not_handed_to_the_task(bench: Bench) -> None:
    bare = said()
    assert bare.terminal is not None
    del bare.terminal.envelope["structured_output"]
    worker = await bench.worker(Scripted(bare, said()))

    assert await worker.carry_out(await bench.owed()) == "applied"

    assert bench.task.problems == [
        None,
        "the reply did not carry the structured answer it was asked for",
    ]


async def test_a_reply_the_service_refuses_is_asked_for_again_with_the_refusal(
    bench: Bench,
) -> None:
    bench.refusing.append("the commit does not descend from the red")
    worker = await bench.worker(Scripted(said(), said()))

    assert await worker.carry_out(await bench.owed()) == "applied"

    assert await bench.states() == ["rejected", "applied"]
    assert bench.task.problems == [None, "the commit does not descend from the red"]


async def test_a_run_that_ended_in_an_error_is_tried_again_and_only_so_often(bench: Bench) -> None:
    failed = said("budget_exhausted.jsonl")
    provider = Scripted(failed, failed, failed, said())
    worker = await bench.worker(provider)

    left = await worker.carry_out(await bench.owed())

    assert left.startswith("refused") and "not the current one" in left  # parked: grant gone
    assert await bench.states() == ["errored", "errored", "errored"]
    assert len(provider.asked) == 3
    assert bench.task.problems[1] == "error_max_budget_usd"
    assert len(await bench.invocations.parked(PROJECT)) == 1


async def test_a_call_whose_result_is_not_known_is_not_made_again(bench: Bench) -> None:
    provider = Scripted(UNCERTAIN, said())
    worker = await bench.worker(provider)
    owed = await bench.owed()

    assert await worker.carry_out(owed) == "uncertain"
    assert (await worker.carry_out(owed)).startswith("refused: this work is parked")

    assert len(provider.asked) == 1
    assert bench.applied == []


async def test_a_call_that_could_not_be_launched_leaves_the_work_for_another_time(
    bench: Bench,
) -> None:
    provider = Scripted(NOT_STARTED, said())
    worker = await bench.worker(provider)
    owed = await bench.owed()

    assert await worker.carry_out(owed) == "not_started"
    assert await worker.carry_out(owed) == "applied"

    assert await bench.states() == ["not_started", "applied"]


async def test_work_that_cannot_start_is_not_called_for(bench: Bench) -> None:
    provider = Scripted(said())
    worker = await bench.worker(provider)
    await bench.world.close("pause", reason="wait")

    left = await worker.carry_out(await bench.owed())

    assert left.startswith("refused") and "paused" in left
    assert provider.asked == []


async def test_a_result_already_known_is_applied_and_nothing_is_called(bench: Bench) -> None:
    """The worker stopped after the record. The same worker, delivered the work again."""
    provider = Scripted(said())
    worker = await bench.worker(provider)
    owed = await bench.owed()
    assert bench.session is not None
    grant = await bench.world.claims.claim(
        bench.session.principal, owed["assignment_id"], bench.session.worker_id
    )
    admission = await bench.invocations.admit(
        bench.session,
        owed["assignment_id"],
        grant,
        replace(PREPARED, read_on=await bench.invocations.as_it_stands(owed["assignment_id"])),
    )
    assert admission.invocation_id is not None
    await bench.invocations.record(admission.invocation_id, said(), retained=KEPT)

    assert await worker.carry_out(owed) == "applied"

    assert provider.asked == []
    assert bench.applied == [{"sum": 5}]


async def test_with_the_cli_itself_standing_in_a_recorded_run_is_carried_through(
    bench: Bench, tmp_path: Path
) -> None:
    adapter = ClaudeAdapter(
        str(RECORDED / "stub_claude.py"),
        {
            "PATH": os.environ["PATH"],
            "STUB_STREAM": str(RECORDED / "success.jsonl"),
            "STUB_RECORD": str(tmp_path / "call.json"),
        },
    )
    worker = await bench.worker(adapter)

    assert await worker.carry_out(await bench.owed()) == "applied"

    assert bench.applied == [{"sum": 5}]
    [invocation] = await bench.world.column("SELECT invocation_id FROM invocations")
    assert isinstance(invocation, uuid.UUID)
    assert str(invocation) in (tmp_path / "call.json").read_text()
    assert await bench.world.column("SELECT estimated_cost_usd FROM invocations") == [
        Decimal("0.0008912")
    ]


async def test_a_cli_that_cannot_be_used_is_known_not_to_have_been_called(bench: Bench) -> None:
    """3B-S1-1.4: established before anything was launched, so nothing was spent and
    nobody is asked about it, with the worker still alive."""
    provider = Scripted(ProviderUnavailable("the installed Claude CLI does not offer --tools"))
    worker = await bench.worker(provider)
    owed = await bench.owed()

    assert await worker.carry_out(owed) == "not_started"

    assert await bench.states() == ["not_started"]
    assert await bench.invocations.parked(PROJECT) == []
    assert await bench.world.column("SELECT state FROM assignments WHERE kind = 'build'") == [
        "wanted"
    ]


async def test_a_call_cut_short_with_nothing_read_is_recorded_as_not_known(bench: Bench) -> None:
    """3B-S1-1.4: cancelled after launch. The worker lives, so nobody else would say so."""
    provider = Scripted(UNCERTAIN, then_waits=True)
    worker = await bench.worker(provider)
    carrying = asyncio.create_task(worker.carry_out(await bench.owed()))
    await provider.waiting.wait()

    carrying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await carrying

    assert await bench.states() == ["uncertain"]
    assert len(await bench.invocations.parked(PROJECT)) == 1


async def test_a_call_cut_short_after_its_result_was_read_keeps_that_result(bench: Bench) -> None:
    """3B-S1-1.4: the terminal event had arrived; the process was being waited for."""
    provider = Scripted(said(), then_waits=True)
    worker = await bench.worker(provider)
    carrying = asyncio.create_task(worker.carry_out(await bench.owed()))
    await provider.waiting.wait()

    carrying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await carrying

    assert await bench.states() == ["returned"]
    assert await bench.world.column("SELECT retained FROM invocations") == [KEPT]
    await bench.invocations.recover(PROJECT)  # the pass carries it on; nothing is called
    assert (await bench.states(), bench.applied) == (["applied"], [{"sum": 5}])
    assert len(provider.asked) == 1


async def test_a_briefing_written_from_what_has_moved_is_written_again(bench: Bench) -> None:
    """3B-S1-1.5."""
    provider = Scripted(said())
    worker = await bench.worker(provider)
    bench.task.reads_too_early = True

    assert await worker.carry_out(await bench.owed()) == "applied"

    assert bench.task.problems == [None, None]  # prepared twice, called once
    assert len(provider.asked) == 1
    assert await bench.states() == ["applied"]


async def until(bench: Bench, *states: str) -> None:
    async with asyncio.timeout(30):
        while await bench.states() != list(states):
            await asyncio.sleep(0.05)


async def test_a_reply_whose_tree_could_not_be_kept_is_recorded_with_what_it_used(
    bench: Bench,
) -> None:
    """3B-S1-2.1."""
    bench.task.cannot_keep = OSError("no space left on device")
    worker = await bench.worker(Scripted(said(), UNCERTAIN))

    await worker.carry_out(await bench.owed())

    async with bench.world.database.connection() as connection:
        row = await (
            await connection.execute("SELECT * FROM invocations WHERE attempt = 1")
        ).fetchone()
    assert row is not None
    assert row["state"] == "rejected"
    assert row["reason"] == "what the call left could not be kept: no space left on device"
    assert (row["envelope"]["subtype"], row["retained"]) == ("success", None)
    assert row["provider_cost_usd"] is not None


async def test_a_caller_cancelled_while_the_tree_is_being_kept_still_has_its_call_recorded(
    bench: Bench,
) -> None:
    """3B-S1-2.1: the provider has returned; the handoff to the record is not the caller's
    to lose."""
    bench.task.keeping = asyncio.Event()
    worker = await bench.worker(Scripted(said()))
    carrying = asyncio.create_task(worker.carry_out(await bench.owed()))
    await bench.task.at_keeping.wait()

    carrying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await carrying
    assert await bench.states() == ["intended"]
    bench.task.keeping.set()

    await until(bench, "returned")
    assert await bench.world.column("SELECT retained FROM invocations") == [KEPT]


async def test_a_caller_cancelled_while_the_record_is_being_made_still_has_it_made(
    bench: Bench,
) -> None:
    """3B-S1-2.1: the record transaction is waiting, for a lock say, when the caller goes."""
    worker = await bench.worker(Scripted(said()))
    real = bench.invocations.record
    waiting, go_on = asyncio.Event(), asyncio.Event()

    async def waits_first(*arguments: Any, **named: Any) -> str:
        waiting.set()
        await go_on.wait()
        return await real(*arguments, **named)

    bench.invocations.record = waits_first  # type: ignore[method-assign]
    carrying = asyncio.create_task(worker.carry_out(await bench.owed()))
    await waiting.wait()

    carrying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await carrying
    assert await bench.states() == ["intended"]
    go_on.set()

    await until(bench, "returned")


async def test_a_call_cut_short_after_a_result_that_must_not_be_used_does_not_use_it(
    bench: Bench,
) -> None:
    """3B-S1-2.2: what the adapter decided about the result is what is recorded, also
    when the caller is cancelled while the adapter waits for the process."""
    unverified = replace(said(), unusable="the run reported a result without saying what it had")
    provider = Scripted(unverified, then_waits=True)
    worker = await bench.worker(provider)
    carrying = asyncio.create_task(worker.carry_out(await bench.owed()))
    await provider.waiting.wait()

    carrying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await carrying

    assert await bench.states() == ["rejected"]
    assert await bench.world.column("SELECT provider_cost_usd IS NOT NULL FROM invocations") == [
        True
    ]
    await bench.invocations.recover(PROJECT)
    assert bench.applied == []


async def test_a_worker_that_lost_its_connection_while_preparing_makes_no_call(
    bench: Bench,
) -> None:
    """3B-S1-2.8."""
    provider = Scripted(said())
    worker = await bench.worker(provider)
    assert bench.session is not None
    bench.task.during_preparation = bench.session.stop

    left = await worker.carry_out(await bench.owed())

    assert left.startswith("refused") and "no longer known to be running" in left
    assert provider.asked == []
    assert await bench.states() == []


async def test_a_reply_the_check_itself_cannot_cope_with_is_still_recorded(bench: Bench) -> None:
    """3B-S1-3.3: a list where an object was asked for. The check faults; the call and
    what it used are on record all the same, as a reply that gave nothing."""
    odd = said()
    assert odd.terminal is not None
    odd.terminal.envelope["structured_output"] = []
    worker = await bench.worker(Scripted(odd, UNCERTAIN))

    await worker.carry_out(await bench.owed())

    async with bench.world.database.connection() as connection:
        row = await (
            await connection.execute("SELECT * FROM invocations WHERE attempt = 1")
        ).fetchone()
    assert row is not None
    assert row["state"] == "rejected"
    assert row["reason"].startswith("the reply could not be checked: AttributeError")
    assert row["envelope"]["subtype"] == "success" and row["provider_cost_usd"] is not None


async def test_a_call_cut_short_before_anything_was_launched_cost_nothing(bench: Bench) -> None:
    """3B-S1-3.8: cancelled while the CLI was still being checked."""
    provider = Scripted(said())
    provider.waits_before_launch = True
    worker = await bench.worker(provider)
    carrying = asyncio.create_task(worker.carry_out(await bench.owed()))
    await provider.waiting.wait()

    carrying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await carrying

    assert await bench.states() == ["not_started"]
    assert await bench.invocations.parked(PROJECT) == []
    assert await bench.world.column("SELECT state FROM assignments WHERE kind = 'build'") == [
        "wanted"
    ]


async def test_no_call_is_made_within_bounds_that_have_not_been_shown_to_hold(
    bench: Bench,
) -> None:
    """S3-18.6 and S3-19.7."""
    provider = Scripted(said())
    worker = await bench.worker(provider)
    bench.boundaries.holding = False

    left = await worker.carry_out(await bench.owed())

    assert left == "refused: the bounds of this run have not been shown to hold on this host"
    assert provider.asked == []
    assert await bench.states() == []
    assert bench.boundaries.asked == [
        boundary_digest(Confinement((), {}), RUNTIME)
    ]  # asked about exactly the bounds the call would have had
