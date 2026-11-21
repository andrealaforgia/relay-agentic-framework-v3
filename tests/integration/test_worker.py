"""One piece of work carried out by a worker: claimed, called for, recorded, applied.
The provider here is scripted, or is the stand-in for the CLI replaying a real stream."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.invocations import Effect, EffectRun, Invocations
from relay.application.worker import Call, Worker
from relay.application.workers import WorkerSession
from relay.domain.story import ActionRefused
from relay.providers.claude import ClaudeAdapter, Confinement, ProviderResult, Request
from tests.integration.decision_world import DecisionWorld
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_invocations import NOT_STARTED, PREPARED, UNCERTAIN, said
from tests.integration.trunk_world import PROJECT

RECORDED = Path(__file__).parent.parent / "fixtures" / "claude"


class Scripted:
    """A provider that gives what it was told to, in order, and remembers what it was asked."""

    def __init__(self, *results: ProviderResult) -> None:
        self._results = list(results)
        self.asked: list[Request] = []

    async def invoke(self, request: Request) -> ProviderResult:
        self.asked.append(request)
        return self._results.pop(0)


class Summing:
    """A task whose reply must be a sum, and which refuses a wrong one."""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self.problems: list[str | None] = []

    async def prepare(self, assignment: Mapping[str, Any], last_problem: str | None) -> Call:
        self.problems.append(last_problem)
        return Call(PREPARED, self._workdir, Confinement((), {}), timeout=30.0)

    def problem_with(self, reply: Any) -> str | None:
        return None if isinstance(reply.get("sum"), int) else "the reply has no sum"


class Bench:
    def __init__(self, world: DecisionWorld, tmp_path: Path) -> None:
        self.world = world
        self.task = Summing(tmp_path)
        self.applied: list[Any] = []
        self.refusing: list[str] = []
        self.invocations = Invocations(world.database, self._effects)
        self.session: WorkerSession | None = None

    def _effects(self, invocation: dict[str, Any]) -> list[Effect]:
        async def use(run: EffectRun) -> None:
            if self.refusing:
                raise ActionRefused(self.refusing.pop(0))
            self.applied.append(run.invocation["reply"])

        return [Effect("use", use)]

    async def worker(self, provider: Any) -> Worker:
        self.session = self.session or await WorkerSession.start(self.world.database, "builder")
        return Worker(
            self.session, self.world.claims, self.invocations, provider, {"build": self.task}
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
    admission = await bench.invocations.admit(bench.session, owed["assignment_id"], grant, PREPARED)
    assert admission.invocation_id is not None
    await bench.invocations.record(admission.invocation_id, said())

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
