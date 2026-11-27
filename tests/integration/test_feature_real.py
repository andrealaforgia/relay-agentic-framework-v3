"""One small feature delivered by the real Claude CLI: every role a real model run, on
its installed canonical agent definition, within
the bounds the boundary suite showed to hold, and a person's decisions made here by the
test. It makes paid calls (the cheapest model; well under a dollar), so it runs only when
asked: `RELAY_REAL_CLAUDE=1`. It is how the briefings, the reply schemas and the bounds
are shown to work together against the provider, which no scripted test can show."""

from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.boundaries import Boundaries
from relay.application.tasks import Profile
from relay.domain.evidence import Launch
from relay.git.checkout import pristine_checkout
from relay.providers.boundary_suite import run_suite, runtime_now
from relay.providers.claude import ClaudeAdapter, ProviderResult, Request
from relay.providers.definitions import DEFAULT_DEFINITIONS
from tests.integration.completion_world import CompletionWorld
from tests.integration.fixtures import PRODUCT_OWNER
from tests.integration.test_workers_deliver import Delivering
from tests.integration.trunk_world import PROJECT

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_REAL_CLAUDE") != "1", reason="makes paid calls: set RELAY_REAL_CLAUDE=1"
)

MODEL = "claude-haiku-5-5"
INSTALLED = Path.home() / ".claude" / "agents"
CANONICAL = sorted(set(DEFAULT_DEFINITIONS.values()))
STATED = "none: stated by the test that runs this smoke"
PROBLEM = (
    "vat.py prints the amount it is given, unchanged. It must print the gross amount at "
    "the standard VAT rate of 20%, as a whole number: `python3 vat.py 100` must print 120. "
    "That is the whole of the feature: one story with one acceptance criterion is enough."
)
# More calls than a feature this small can need; a run that reaches it is stopped.
CALLS_AT_MOST = 45


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
    missing = [name for name in CANONICAL if not (INSTALLED / f"{name}.md").is_file()]
    if missing:
        pytest.skip(f"the canonical agent definitions are not installed: {missing}")
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    # The canonical agents, as installed, and a cap on what any one call may cost.
    profile = Profile(MODEL, timeout=900.0, budget_usd=Decimal("0.50"))
    at = Delivering(world, tmp_path, profile, definitions=INSTALLED)
    at.boundaries = Boundaries(database)
    # Asked afresh at every admission: a CLI replaced during the run has no pass.
    at.runtime = lambda: runtime_now(STATED)
    root = tmp_path.resolve() / "workspaces"
    root.mkdir(exist_ok=True)
    for role, withheld in (("qa", []), ("builder", [world.mirrors])):
        await at.boundaries.record(
            await run_suite(role, root, model=MODEL, withheld=withheld, remote_policy=STATED)
        )
    provider = Counted()
    await at.staff(**dict.fromkeys(("analyst", "verifier", "builder", "qa", "sentinel"), provider))

    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", PROBLEM)
    for _ in range(12):
        await at.work()
        if await world.feature() == "completed":
            break
        for question in await world.decisions.open_questions(PROJECT):
            await world.answer(question.question_id, "Take the simplest reading of the problem.")
        for item in await world.stories.inbox(PROJECT):
            await world.decide(item.gate_id)

    said = await world.column(
        "SELECT a.kind || ' ' || v.state || coalesce(': ' || v.reason, '') || ' [' || v.role "
        "|| ' attempt ' || v.attempt || ' of ' || left(a.assignment_id::text, 13) || ' ' "
        "|| a.state || coalesce(' after ' || left(a.succeeds::text, 13), '') || ']' "
        "FROM invocations v JOIN assignments a USING (assignment_id) ORDER BY v.started_at"
    )
    assert await world.feature() == "completed", "\n".join(str(each) for each in said)
    summary = await world.summary()
    assert summary is not None
    # What the feature was for, observed by Relay's own oracle on the commit the summary
    # names, whatever criterion and contract the models wrote for themselves.
    workdir = await pristine_checkout(str(world.remote), summary["trunk"], tmp_path / "observed")
    seen = await world.oracle.execute(
        summary["trunk"], workdir, Launch(argv=("python3", "vat.py", "100"), timeout_seconds=30)
    )
    assert (seen.observation.exit_code, seen.observation.stdout) == (0, b"120\n")
    # And the agents that did the work were the installed ones, as they are on disk.
    recorded = await world.column(
        "SELECT DISTINCT agent_name || ' ' || agent_sha256 FROM invocations"
    )
    assert set(recorded) <= {
        f"{name} {hashlib.sha256((INSTALLED / f'{name}.md').read_bytes()).hexdigest()}"
        for name in CANONICAL
    }, recorded
    assert await world.column(
        "SELECT count(*) FROM invocations WHERE envelope->>'subtype' = 'error_max_budget_usd'"
    ) == [0]
    spent = {role: use["provider_cost_usd"] for role, use in summary["usage"]["by_role"].items()}
    print("calls:", len(provider.made), "spent by role:", spent)
    print("\n".join(provider.made))
    # Every call on record is one the provider was asked for, and the other way about.
    assert len(said) == len(provider.made), (said, provider.made)
    print("\n".join(str(each) for each in said))
    assert all(cost != "unknown" for cost in spent.values()), spent
