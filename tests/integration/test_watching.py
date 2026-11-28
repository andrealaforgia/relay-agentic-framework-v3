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
from relay.cli import reading, watching
from relay.git.checkout import name_attempts_under
from relay.runtime import configure
from tests.integration.conftest import DATABASE_URL, open_pool
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import run_green
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_deciding_commands import Desk
from tests.integration.test_invocations import Calling
from tests.integration.trunk_world import PROJECT, VAT_APPLIES_STANDARD_RATE


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
        # 3C-S3-1.4: what is waiting is said, with the handle that shows it in full.
        [waiting] = facts["waiting"]
        assert waiting["kind"] == "question" and str(question).startswith(waiting["handle"])
        assert f"{waiting['handle']}  question:" in board
        assert f"`relay show {waiting['handle']}`" in board
        assert facts["processes"] == {
            "control_plane": "not running",
            "workers": {"analyst": 0, "verifier": 0, "builder": 1, "qa": 0, "sentinel": 0},
        }
        [call] = facts["calls_in_flight"]
        assert (call["role"], call["work"], call["of"]) == ("builder", "build", "B1")
        assert call["for_seconds"] >= 0 and call["model"]
        assert facts["waiting_for_the_product_owner"] == 1
        [build] = [each for each in facts["owed"] if each["work"] == "build"]
        # 3C-S3-1.4: who holds it, whether they are there, and held is not owed.
        assert build["held_by"] == call["worker"] and build["holder_alive"] is True
        assert build["held_for_seconds"] is not None
        assert build["owed_for_seconds"] >= build["held_for_seconds"] >= 0
        assert f"held by worker {call['worker'][:8]} (alive)" in board
        assert f"by worker {call['worker'][:8]}" in board
        assert facts["features"][0]["stories"][0]["title"] == hostile.title  # whole, in JSON
        await world.answer(question)
        assert "waiting for the Product Owner: nothing" in desk.anyone("watch", "--once").out
        # A call that has ended is no longer in flight.
        async with world.database.connection() as connection:
            await connection.execute("UPDATE invocations SET state = 'errored'")
        assert desk.read("watch", "--once")["calls_in_flight"] == []
        assert "in flight: no call" in desk.anyone("watch", "--once").out
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
        assert costs["by_role"]["builder"]["calls"] == 1  # PO-8: who spent it
        assert costs["by_role"]["builder"]["charged_by_the_provider"]["usd"] == "0.3"
        assert "the builder: 1 call(s)" in text
        assert costs["charged_by_the_provider"]["usd"] == "0.3"
        assert costs["estimated_by_relay"] == [
            {"price_table": "2026-11", "usd": "0.28", "calls": 1}
        ]
        assert "charged by the provider: 0.3 USD over 1 call(s)" in text
        assert "estimated by Relay (an estimate, price table 2026-11): 0.28 USD" in text
        assert "cache written unknown" in text
        assert desk.read("costs", "F1")["calls"] == 1
        # 3C-S3-1.9: a name that is no feature is refused, and is not an account of nothing.
        for mistyped in (["costs", "F9"], ["costs", "F9", "--summary"]):
            ran = desk.anyone(*mistyped)
            assert ran.code == 1 and "there is no feature 'F9'" in ran.printed
        assert desk.read("watch", "--tokens", "--feature", "F1", "--once") == desk.read(
            "costs", "F1"
        )
        assert desk.anyone("watch", "--tokens", "--feature", "F9", "--once").code == 1
        summary = desk.anyone("costs", "F1", "--summary")
        assert summary.code == 0 and "the feature has not completed" in summary.out
        assert desk.anyone("costs", "--summary").code == 1  # a summary is a feature's
    finally:
        await calling.close()


async def test_each_piece_of_work_has_its_own_age_and_work_that_asked_has_one_too(
    desk: Desk,
) -> None:
    """3C-S3-1.3: an age belongs to the piece of work it is of. Two reviews of one
    behaviour, owed at different times, do not show one age; and work whose question is
    open has been waiting since it was owed, not for no time at all."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", world.attempts["B1"])).is_valid
    assert await world.reviews.publish_candidates() == 1
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE assignments SET created_at = created_at - interval '1 hour' "
            "WHERE kind = 'review' AND role = 'sentinel'"
        )

    facts = await watching.board(world.database, PROJECT)

    reviews = {each["to"]: each for each in facts["owed"] if each["work"] == "review"}
    assert set(reviews) == {"qa", "sentinel"}
    assert reviews["sentinel"]["owed_for_seconds"] >= 3600 > reviews["qa"]["owed_for_seconds"]
    assert reviews["qa"]["assignment"] != reviews["sentinel"]["assignment"]
    # And work that asked: listed while its question is open, with its own age.
    question = await world.ask()
    async with world.database.connection() as connection:
        asking = await (
            await connection.execute(
                "UPDATE assignments SET state = 'asked', created_at = created_at - interval "
                "'3 hours' WHERE kind = 'review' AND role = 'qa' RETURNING assignment_id"
            )
        ).fetchone()
        assert asking is not None
        await connection.execute(
            "UPDATE questions SET asked_for = %s WHERE question_id = %s",
            (asking["assignment_id"], question),
        )
    for _ in range(2):  # across two polls
        [asked] = [
            each
            for each in (await watching.board(world.database, PROJECT))["owed"]
            if each["state"].startswith("waiting on a question")
        ]
        assert asked["owed_for_seconds"] >= 3 * 3600
    assert "owed for 3h" in watching.board_text(await watching.board(world.database, PROJECT))


async def test_a_holder_that_has_gone_is_said_to_have_gone(desk: Desk) -> None:
    """3C-S3-1.4: two Builders, one build. The one that holds it exits before recovery.
    The board says the work is held by a worker that is gone, and does not leave the
    person to guess whether the Builder still alive has it."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    calling = Calling(world)
    try:
        holder, other = await calling.worker(), await calling.worker()
        await calling.admitted(holder)
        await holder.stop()

        facts = await watching.board(world.database, PROJECT)
        text = watching.board_text(facts)

        [build] = [each for each in facts["owed"] if each["work"] == "build"]
        assert facts["processes"]["workers"]["builder"] == 1  # the other one
        assert build["held_by"] == str(holder.worker_id) and build["holder_alive"] is False
        assert str(other.worker_id) != build["held_by"]
        assert "GONE: not yet recovered" in text and "GONE: nobody is making this call" in text
    finally:
        await calling.close()


async def test_one_frame_of_the_board_is_of_one_moment(
    desk: Desk, namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S3-1.5: a call ends while the board is being read. The frame shows the workflow
    as it was before or as it is after: not the work taken up with nobody making a call
    for it, which never was."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    calling = Calling(world)
    try:
        await calling.admitted(await calling.worker())
        read = reading.status_in

        async def and_then_the_call_ends(connection: Any, project: str, **more: Any) -> Any:
            standing = await read(connection, project, **more)
            async with (
                open_pool(DATABASE_URL, schema=namespace) as other,
                other.connection() as elsewhere,
            ):
                await elsewhere.execute("UPDATE invocations SET state = 'errored'")
                await elsewhere.execute(
                    "UPDATE assignments SET state = 'wanted', grant_id = NULL, "
                    "granted_to = NULL, granted_worker = NULL "
                    "WHERE kind = 'build'"
                )
            return standing

        monkeypatch.setattr(reading, "status_in", and_then_the_call_ends)
        frame = await watching.board(world.database, PROJECT)
        monkeypatch.undo()
        after = await watching.board(world.database, PROJECT)

        [build] = [each for each in frame["owed"] if each["work"] == "build"]
        assert build["state"] == "taken up" and len(frame["calls_in_flight"]) == 1  # before
        [later] = [each for each in after["owed"] if each["work"] == "build"]
        assert later["state"] == "waiting for a worker" and after["calls_in_flight"] == []
    finally:
        await calling.close()


async def test_one_look_at_the_events_shows_all_there_are_and_ends(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S3-1.7: a ledger longer than a page is shown whole, each event once, and one
    look ends at what there was when it began, whatever arrives meanwhile."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    [there] = await world.column("SELECT count(*) FROM events")
    assert isinstance(there, int) and there > 4
    monkeypatch.setattr(watching, "_PAGE", 2)  # several pages
    read, arrived = reading.ledger, [False]

    async def and_more_arrive(*arguments: Any, **more: Any) -> Any:
        page = await read(*arguments, **more)
        if not arrived[0]:
            arrived[0] = True
            await world.ask()  # something happens while the look is being taken
        return page

    monkeypatch.setattr(reading, "ledger", and_more_arrive)
    shown: list[str] = []

    async with asyncio.timeout(20):
        await watching.watch(
            world.database, PROJECT, "events", once=True, as_json=True, out=shown.append
        )

    events = [json.loads(line) for line in "\n".join(shown).splitlines()]
    # All of them, each once, and none of what arrived after the look began.
    assert [each["sequence"] for each in events] == list(range(1, there + 1))


async def test_json_is_never_preceded_by_anything_that_is_not_json(desk: Desk) -> None:
    """3C-S3-1.8: a terminal is cleared between frames a person reads, and never before a
    frame a program parses."""
    world = desk.world
    await world.decide(await world.roadmap(STANDARD_RATE))
    for view in ("board", "tokens"):
        for as_json in (True, False):
            shown: list[str] = []
            stop = asyncio.Event()
            stop.set()
            await watching.watch(
                world.database, PROJECT, view, as_json=as_json, terminal=True,
                out=shown.append, stop=stop,
            )  # fmt: skip
            [frame] = shown
            if as_json:
                assert json.loads(frame)["project"] == PROJECT
            else:
                assert frame.startswith("\x1b[2J\x1b[H")


def test_an_interval_of_nothing_is_refused_before_anything_is_read(desk: Desk) -> None:
    """3C-S3-1.10."""
    for interval in ("0", "-1", "nan", "inf"):
        ran = desk.anyone("watch", "--once", f"--every={interval}")
        assert ran.code == 1 and "a number of seconds above nothing" in ran.printed
    assert desk.anyone("watch", "--feature", "F1", "--once").code == 1  # only with --tokens
