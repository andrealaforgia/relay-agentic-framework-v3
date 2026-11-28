"""A feature delivered with nothing but the command line (3c step 4): the processes an
operator starts, and the commands a Product Owner types at a terminal. The provider is
the stand-in CLI. The same history against the real provider is `test_smoke_cli_real.py`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from relay.application import authority
from relay.runtime import ROLES
from tests.integration.fixtures import VAT_APPLIES_STANDARD_RATE
from tests.integration.person import Ran, at_a_terminal, with_no_terminal
from tests.integration.test_processes import Running, running  # noqa: F401 - the fixture
from tests.integration.trunk_world import PROJECT

PASSPHRASE = "correct horse battery"


class Operator:
    """Someone with a shell in the project's checkout: no service object, only commands."""

    def __init__(self, running: Running, state: Path) -> None:  # noqa: F811
        self.running, self.here = running, running.world.builder
        self.shell = {
            **running.environment,
            "RELAY_STATE_DIR": str(state),
            "RELAY_PROJECT": PROJECT,
        }

    def types(self, *command: str) -> Ran:
        """A command that decides: at a terminal, with the passphrase."""
        ran = at_a_terminal(list(command), [PASSPHRASE], self.shell, self.here)
        assert ran.code == 0, ran.printed + ran.shown
        return ran

    def reads(self, *command: str) -> Any:
        ran = with_no_terminal([*command, "--json"], self.shell, self.here)
        assert ran.code == 0, ran.printed
        return json.loads(ran.out)

    def sees(self, *command: str) -> str:
        ran = with_no_terminal(list(command), self.shell, self.here)
        assert ran.code == 0, ran.printed
        return ran.out

    async def waits_for_something_to_decide(self, within: float = 240.0) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            waiting = self.reads("inbox")
            if waiting:
                return dict(waiting[0])
            for process in self.running.started:
                assert process.poll() is None, self.running.said(process)
            assert time.monotonic() - started < within, self.sees("status")
            await asyncio.sleep(0.5)


@pytest.mark.timeout(900)
async def test_a_feature_is_delivered_with_nothing_but_the_command_line(
    running: Running,  # noqa: F811
    tmp_path: Path,
) -> None:
    world = running.world
    await authority.set_first(world.database, PASSPHRASE)  # as `relay init` does, once
    operator = Operator(running, tmp_path / "state")
    running.start("up")
    for role in ROLES:
        running.start("worker", "start", "--role", role)

    started = operator.types("feature", "start", "prices need VAT")

    assert "prices need VAT" in started.shown and "effective" in started.printed
    board = operator.sees("watch", "--once")
    assert "control plane: running" in board and "F1" in board
    roadmap = await operator.waits_for_something_to_decide()
    assert roadmap["kind"] == "gate" and "roadmap" in json.dumps(roadmap)
    assert roadmap["handle"] in operator.sees("show", roadmap["handle"])
    approved = operator.types("approve", roadmap["handle"])
    assert "effective: approved" in approved.printed
    decided = 1
    while operator.reads("status")["features"][0]["state"] != "completed":
        try:
            item = await operator.waits_for_something_to_decide(within=20.0)
        except AssertionError:
            continue  # nothing to decide yet: the agents are at work
        operator.types("approve", item["handle"])
        decided += 1
        assert decided < 6, operator.sees("status")

    assert decided == 2  # the roadmap and the one story: nothing else was asked
    on_trunk = world.git("show", f"{world.trunk()}:vat.py", at=world.remote)
    assert on_trunk == VAT_APPLIES_STANDARD_RATE.strip()
    status = operator.sees("status")
    assert "F1  completed" in status and "waiting for the Product Owner: nothing" in status
    costs = operator.reads("costs", "F1")
    assert set(costs["by_role"]) == set(ROLES) and costs["calls"] >= 5
    assert costs["calls_whose_end_is_not_known"] == 0
    summary = operator.sees("costs", "F1", "--summary")
    assert "as recorded when the feature completed" in summary
    events = [each["event"] for each in operator.reads("ledger")]
    assert "feature.completed" in events or len(events) == 200  # a page of the ledger
    assert "feature.completed" in operator.sees("watch", "--events", "--once")
