"""One small feature delivered by the real Claude CLI with nothing but the command line:
`relay policy state`, `relay doctor --boundary`, `relay up`, a worker of each role, and a
Product Owner typing `relay feature start`, `relay approve` and `relay answer` at a
terminal. Every role is a real model run on its installed canonical agent definition.

It makes paid calls (the cheapest model; well under a dollar), so it runs only when asked:
`RELAY_REAL_CLAUDE=1`. It is 3b's smoke driven through what 3c built.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.application import authority
from relay.domain.evidence import Launch
from relay.git.checkout import name_attempts_under, pristine_checkout
from relay.providers.definitions import DEFAULT_DEFINITIONS
from relay.runtime import ROLES, configure
from tests.integration.completion_world import CompletionWorld
from tests.integration.test_processes import Running
from tests.integration.test_smoke_cli import PASSPHRASE, Operator

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_REAL_CLAUDE") != "1", reason="makes paid calls: set RELAY_REAL_CLAUDE=1"
)

INSTALLED = Path.home() / ".claude" / "agents"
CANONICAL = sorted(set(DEFAULT_DEFINITIONS.values()))
PROBLEM = (
    "vat.py prints the amount it is given, unchanged. It must print the gross amount at "
    "the standard VAT rate of 20%, as a whole number: `python3 vat.py 100` must print 120. "
    "That is the whole of the feature: one story with one acceptance criterion is enough."
)
DECISIONS_AT_MOST = 12


@pytest.mark.timeout(3600)
async def test_a_small_feature_is_delivered_by_the_real_provider_through_the_command_line(
    database: AsyncConnectionPool,
    broker: redis.Redis,
    namespace: str,
    tmp_path: Path,
    runner_image: str,
) -> None:
    missing = [name for name in CANONICAL if not (INSTALLED / f"{name}.md").is_file()]
    if missing:
        pytest.skip(f"the canonical agent definitions are not installed: {missing}")
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await configure(
        database,
        workspaces=str(tmp_path.resolve() / "workspaces"),
        mirrors=str(world.mirrors),
        image=runner_image,
        definitions=[str(INSTALLED)],
        profiles={"default": {"model": "claude-haiku-5-5", "timeout": 900, "budget_usd": "0.50"}},
    )
    await authority.set_first(database, PASSPHRASE)
    running = Running(world, namespace, tmp_path, broker)
    operator = Operator(running, tmp_path / "state")
    try:
        stated = operator.types("policy", "state", "none: stated by the test that runs this smoke")
        assert "revision 1" in stated.printed
        # The bounds of each role, shown to hold on this machine by paid runs, before
        # any worker is allowed a call.
        checked = operator.sees("doctor", "--boundary")
        print(checked)
        running.start("up")
        for role in ROLES:
            running.start("worker", "start", "--role", role)

        operator.types("feature", "start", PROBLEM)
        decided = 0
        while operator.reads("status")["features"][0]["state"] != "completed":
            try:
                item = await operator.waits_for_something_to_decide(within=30.0)
            except AssertionError:
                continue  # the agents are at work
            assert decided < DECISIONS_AT_MOST, operator.sees("status") + operator.sees("inbox")
            decided += 1
            print(f"deciding {item['kind']} {item['handle']}")
            if item["kind"] == "question":
                operator.types(
                    "answer", item["handle"], "Take the simplest reading of the problem."
                )
            elif item["kind"] == "gate":
                operator.types("approve", item["handle"])
            else:
                raise AssertionError(operator.sees("show", item["handle"]))
            await asyncio.sleep(1.0)

        print(operator.sees("status"))
        print(operator.sees("costs", "F1", "--summary"))
        print(operator.sees("watch", "--once"))
        # What the feature was for, observed by Relay's own oracle on the trunk.
        trunk = world.trunk()
        workdir = await pristine_checkout(str(world.remote), trunk, tmp_path / "observed")
        seen = await world.oracle.execute(
            trunk, workdir, Launch(argv=("python3", "vat.py", "100"), timeout_seconds=30)
        )
        assert (seen.observation.exit_code, seen.observation.stdout) == (0, b"120\n")
        costs = operator.reads("costs", "F1")
        assert costs["calls_whose_end_is_not_known"] == 0 and set(costs["by_role"]) == set(ROLES)
        assert costs["charged_by_the_provider"]["calls_it_did_not"] == 0
    finally:
        running.stop_all()
        name_attempts_under("")
