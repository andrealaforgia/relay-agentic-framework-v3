"""One small feature delivered by the real Claude CLI: every role a real model run within
the bounds the boundary suite showed to hold, and a person's decisions made here by the
test. It makes paid calls (the cheapest model; well under a dollar), so it runs only when
asked: `RELAY_REAL_CLAUDE=1`. It is how the briefings, the reply schemas and the bounds
are shown to work together against the provider, which no scripted test can show."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.boundaries import Boundaries
from relay.application.tasks import Profile
from relay.providers.boundary_suite import run_suite, runtime_of
from relay.providers.claude import ClaudeAdapter, ProviderResult, Request
from tests.integration.completion_world import CompletionWorld
from tests.integration.fixtures import PRODUCT_OWNER
from tests.integration.test_workers_deliver import Delivering
from tests.integration.trunk_world import PROJECT

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_REAL_CLAUDE") != "1", reason="makes paid calls: set RELAY_REAL_CLAUDE=1"
)

MODEL = "claude-haiku-5-5"
STATED = "none: stated by the test that runs this smoke"
PROBLEM = (
    "vat.py prints the amount it is given, unchanged. It must print the gross amount at "
    "the standard VAT rate of 20%, as a whole number: `python3 vat.py 100` must print 120. "
    "That is the whole of the feature: one story with one acceptance criterion is enough."
)
# More calls than a feature this small can need; a run that reaches it is stopped.
CALLS_AT_MOST = 30


class Counted:
    """The real adapter, which will not make more calls than the smoke allows."""

    def __init__(self) -> None:
        self._adapter = ClaudeAdapter()
        self.made: list[str] = []

    async def invoke(self, request: Request, **named: Any) -> ProviderResult:
        assert len(self.made) < CALLS_AT_MOST, f"the smoke made {CALLS_AT_MOST} calls: {self.made}"
        self.made.append(request.briefing.splitlines()[0][:60])
        return await self._adapter.invoke(request, **named)


@pytest.mark.timeout(3600)
async def test_a_small_feature_is_delivered_by_the_real_provider(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    at = Delivering(world, tmp_path, Profile(MODEL, timeout=900.0))
    version = (await ClaudeAdapter().capabilities()).version
    at.boundaries = Boundaries(database)
    at.runtime = lambda: runtime_of(version, STATED)
    root = tmp_path.resolve() / "workspaces"
    root.mkdir(exist_ok=True)
    for role, withheld in (("qa", []), ("builder", [world.mirrors])):
        await at.boundaries.record(
            await run_suite(role, root, model=MODEL, withheld=withheld, remote_policy=STATED)
        )
    provider = Counted()
    await at.staff(**dict.fromkeys(("analyst", "verifier", "builder", "qa", "sentinel"), provider))

    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", PROBLEM)
    for _ in range(8):
        await at.work()
        if await world.feature() == "completed":
            break
        for question in await world.decisions.open_questions(PROJECT):
            await world.answer(question.question_id, "Take the simplest reading of the problem.")
        for item in await world.stories.inbox(PROJECT):
            await world.decide(item.gate_id)

    said = await world.column(
        "SELECT a.kind || ' ' || v.state || coalesce(': ' || v.reason, '') FROM invocations v "
        "JOIN assignments a USING (assignment_id) ORDER BY v.started_at"
    )
    assert await world.feature() == "completed", "\n".join(str(each) for each in said)
    assert world.git("show", f"{world.trunk()}:vat.py", at=world.remote)  # something is there
    summary = await world.summary()
    assert summary is not None
    spent = {role: use["provider_cost_usd"] for role, use in summary["usage"]["by_role"].items()}
    print("calls:", len(provider.made), "spent by role:", spent)
    print("\n".join(str(each) for each in said))
    assert all(cost != "unknown" for cost in spent.values()), spent
