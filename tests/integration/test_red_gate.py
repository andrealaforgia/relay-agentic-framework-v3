"""Scenario 4 (verified red), at the level of durable state: the Builder stays blocked until
the registered test fails for the registered reason, and every transition is on the ledger."""

from __future__ import annotations

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.domain.behaviour import BehaviourState, TransitionRefused
from relay.persistence.ledger import read_events
from relay.persistence.outbox import pending_messages
from tests.integration.fixtures import CONTRACT, FAILS_AS_REGISTERED, SYNTAX_ERROR

PROJECT = "vat-calculator"


async def behaviour_awaiting_red(database: AsyncConnectionPool) -> Behaviours:
    behaviours = Behaviours(database)
    await behaviours.open_project(PROJECT)
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)
    return behaviours


async def test_an_invalid_red_keeps_the_builder_blocked(database: AsyncConnectionPool) -> None:
    behaviours = await behaviour_awaiting_red(database)

    verdict = await behaviours.record_red_run(PROJECT, "B1", SYNTAX_ERROR)

    assert not verdict.is_valid
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.VALIDATING_RED
    assert await pending_messages(database) == []
    rejected = (await read_events(database, PROJECT))[-1]
    assert rejected.event_type == "behaviour.red_rejected"
    assert rejected.payload["rejection"] == "unexpected_failure"


async def test_a_valid_red_releases_exactly_one_build_request(
    database: AsyncConnectionPool,
) -> None:
    behaviours = await behaviour_awaiting_red(database)

    verdict = await behaviours.record_red_run(PROJECT, "B1", FAILS_AS_REGISTERED)

    assert verdict.is_valid
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING
    verified = (await read_events(database, PROJECT))[-1]
    assert verified.event_type == "behaviour.test_red_verified"
    [request] = await pending_messages(database)
    assert request.stream == f"relay:{PROJECT}:cmd:builder"
    assert request.envelope["event_type"] == "build.requested"
    assert request.envelope["behaviour_id"] == "B1"
    assert request.envelope["causation_id"] == str(verified.event_id)


async def test_the_ledger_is_gap_free_and_in_order(database: AsyncConnectionPool) -> None:
    behaviours = await behaviour_awaiting_red(database)
    await behaviours.record_red_run(PROJECT, "B1", SYNTAX_ERROR)
    await behaviours.record_red_run(PROJECT, "B1", FAILS_AS_REGISTERED)

    events = await read_events(database, PROJECT)

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.event_type for event in events] == [
        "project.opened",
        "behaviour.proposed",
        "behaviour.test_design_started",
        "behaviour.acceptance_test_registered",
        "behaviour.red_rejected",
        "behaviour.test_red_verified",
    ]


async def test_a_refused_transition_changes_nothing(database: AsyncConnectionPool) -> None:
    behaviours = await behaviour_awaiting_red(database)
    await behaviours.record_red_run(PROJECT, "B1", FAILS_AS_REGISTERED)
    before = await read_events(database, PROJECT)

    with pytest.raises(TransitionRefused):
        await behaviours.record_red_run(PROJECT, "B1", FAILS_AS_REGISTERED)

    assert await read_events(database, PROJECT) == before
    assert len(await pending_messages(database)) == 1


async def test_state_ledger_and_outbox_commit_together_or_not_at_all(
    database: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    behaviours = await behaviour_awaiting_red(database)
    before = await read_events(database, PROJECT)

    async def crash(*_: object, **__: object) -> None:
        raise RuntimeError("crash while writing the outbox")

    monkeypatch.setattr("relay.application.behaviours.enqueue", crash)
    with pytest.raises(RuntimeError):
        await behaviours.record_red_run(PROJECT, "B1", FAILS_AS_REGISTERED)

    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.VALIDATING_RED
    assert await read_events(database, PROJECT) == before
    assert await pending_messages(database) == []


async def test_the_ledger_cannot_be_rewritten(database: AsyncConnectionPool) -> None:
    await behaviour_awaiting_red(database)

    async with database.connection() as connection:
        with pytest.raises(Exception, match="append-only"):
            await connection.execute("UPDATE events SET event_type = 'forged'")
    async with database.connection() as connection:
        with pytest.raises(Exception, match="append-only"):
            await connection.execute("DELETE FROM events")
