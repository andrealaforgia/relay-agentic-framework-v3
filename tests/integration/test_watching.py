"""`relay watch` and `relay costs`: how the Product Owner observes the agents (PO-8, PO-9)."""

from __future__ import annotations

import asyncio
import contextvars
import json
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.leadership import Leadership
from relay.application.stories import StoryDraft
from relay.application.workers import WorkerSession
from relay.cli import watching
from relay.git.checkout import name_attempts_under
from relay.runtime import configure
from tests.integration.decision_world import DecisionWorld
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_deciding_commands import Desk
from tests.integration.test_invocations import Calling
from tests.integration.trunk_world import PROJECT


@pytest.fixture
async def desk(
    database: AsyncConnectionPool, shell: dict[str, str], tmp_path: Path, runner_image: str
) -> Any:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await configure(
        database,
        workspaces=str(tmp_path.resolve() / "workspaces"),
        mirrors=str(world.mirrors),
        image=runner_image,
    )
    yield Desk(world, shell)
    name_attempts_under("")


async def test_the_board_shows_who_is_alive_what_is_being_delivered_and_what_waits(
    desk: Desk,
) -> None:
    world = desk.world
    hostile = StoryDraft("S1", "VAT\x1b[2Jat standard rate", "sees gross", {"AC1": "120"})
    await world.decide(await world.roadmap(hostile))
    await world.awaiting_builder("B1")
    calling = Calling(world)
    try:
        await calling.admitted(await calling.worker())  # a Builder's call, in flight
        question = await world.ask()

        board = desk.anyone("watch", "--once").out
        facts = desk.read("watch", "--once")

        assert "control plane: not running" in board and "`relay up`" in board
        assert "workers:" in board and "builder 1" in board and "qa -" in board
        assert "F1  delivering" in board and "S1  released" in board
        assert "\x1b" not in board and "VAT\\x1b[2Jat standard rate" in board
        assert "in flight:" in board and "builder   build of B1" in board
        assert "waiting for the Product Owner: 1" in board
        assert facts["processes"] == {
            "control_plane": "not running",
            "workers": {"analyst": 0, "verifier": 0, "builder": 1, "qa": 0, "sentinel": 0},
        }
        [call] = facts["calls_in_flight"]
        assert (call["role"], call["work"], call["of"]) == ("builder", "build", "B1")
        assert call["for_seconds"] >= 0 and call["model"]
        assert facts["waiting_for_the_product_owner"] == 1
        assert any(each["work"] == "build" and "for_seconds" in each for each in facts["owed"])
        assert facts["features"][0]["stories"][0]["title"] == hostile.title  # whole, in JSON
        await world.answer(question)
        assert "waiting for the Product Owner: nothing" in desk.anyone("watch", "--once").out
    finally:
        await calling.close()


async def test_the_board_says_when_the_control_plane_and_each_roles_worker_are_alive(
    desk: Desk,
) -> None:
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))

    async def led() -> Leadership:
        return await Leadership.take(world.database)

    mine = await asyncio.create_task(led(), context=contextvars.copy_context())
    qa = await WorkerSession.start(world.database, "qa")
    another = await WorkerSession.start(world.database, "qa")
    try:
        facts = await watching.board(world.database, PROJECT)
        text = watching.board_text(facts)

        assert facts["processes"]["control_plane"] == "running"
        assert facts["processes"]["workers"]["qa"] == 2
        assert "control plane: running" in text and "relay up" not in text and "qa 2" in text
    finally:
        await qa.stop()
        await another.stop()
        await mine.release()
    after = await watching.board(world.database, PROJECT)
    assert after["processes"] == {
        "control_plane": "not running",
        "workers": {"analyst": 0, "verifier": 0, "builder": 0, "qa": 0, "sentinel": 0},
    }


async def test_the_events_view_follows_the_ledger_and_shows_each_event_once(desk: Desk) -> None:
    world = desk.world
    gate = await world.roadmap(STANDARD_RATE)
    shown: list[str] = []
    stop = asyncio.Event()
    following = asyncio.create_task(
        watching.watch(world.database, PROJECT, "events", every=0.1, out=shown.append, stop=stop)
    )
    try:
        async with asyncio.timeout(20):
            while not shown:
                await asyncio.sleep(0.05)
            first = "\n".join(shown)
            assert "roadmap.gate_opened" in first and "gate.approved" not in first
            await world.decide(gate)
            while "gate.approved" not in "\n".join(shown):
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await following
    everything = "\n".join(shown)
    assert everything.count("roadmap.gate_opened") == 1  # not shown again when more arrived
    once = desk.anyone("watch", "--events", "--once").out
    assert "project.opened" in once and "gate.approved" in once
    lines = desk.anyone("watch", "--events", "--once", "--json").out.splitlines()
    assert [json.loads(line)["event"] for line in lines][:2] == ["project.opened", "feature.opened"]


async def test_the_tokens_view_and_costs_are_the_live_account_with_estimates_said_to_be_estimates(
    desk: Desk,
) -> None:
    """PO-9: the estimated cost stays visible, and is never taken for a charge."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    calling = Calling(world)
    try:
        await calling.admitted(await calling.worker())
        async with world.database.connection() as connection:
            await connection.execute(
                "UPDATE invocations SET state = 'applied', provider_cost_usd = 0.30, "
                "estimated_cost_usd = 0.28, price_table = '2026-11', usage = %s::jsonb",
                (
                    json.dumps(
                        [
                            {"model": "claude-opus-5-5", "input_tokens": 100, "output_tokens": 20,
                             "cache_read_tokens": 5, "cache_write_tokens": None},
                            {"model": "claude-haiku-5-5", "input_tokens": 7, "output_tokens": 1,
                             "cache_read_tokens": 0, "cache_write_tokens": 0},
                        ]
                    ),
                ),
            )  # fmt: skip

        costs, tokens = desk.read("costs"), desk.read("watch", "--tokens", "--once")
        text = desk.anyone("costs").out

        assert costs == tokens  # one account, two ways in
        assert costs["calls"] == 1 and set(costs["by_model"]) == {
            "claude-haiku-5-5",
            "claude-opus-5-5",
        }
        assert costs["by_model"]["claude-opus-5-5"]["cache_write_tokens"] == "unknown"
        assert costs["charged_by_the_provider"]["usd"] == "0.3"
        assert costs["estimated_by_relay"] == [
            {"price_table": "2026-11", "usd": "0.28", "calls": 1}
        ]
        assert "charged by the provider: 0.3 USD" in text
        assert "estimated by Relay (an estimate, price table 2026-11): 0.28 USD" in text
        assert "cache written unknown" in text
        assert desk.read("costs", "F1")["calls"] == 1 and desk.read("costs", "F9")["calls"] == 0
        summary = desk.anyone("costs", "F1", "--summary")
        assert summary.code == 0 and "the feature has not completed" in summary.out
        assert desk.anyone("costs", "--summary").code == 1  # a summary is a feature's
    finally:
        await calling.close()
