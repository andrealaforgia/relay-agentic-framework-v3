"""A project whose repository is a bare remote, as a real one is: trunk can be pushed to,
hooks can refuse, and a Builder works in a clone of its own."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.application.integration import Integration, IntegrationReceipt
from relay.application.reviews import Review, Reviews
from relay.domain.behaviour import BehaviourState
from relay.domain.evidence import Baseline, CheckContract, Expected, Launch, ProcessContract
from relay.persistence.ledger import read_events
from relay.persistence.outbox import pending_messages
from relay.testing.oracle import ContainerIsolation, Isolation, ProcessOracle
from tests.integration.fixtures import (
    CONTRACT,
    RELAY_ITSELF,
    VAT_APPLIES_STANDARD_RATE,
    VAT_RETURNS_NET,
    VERIFIER,
    in_a_story,
    prepare_review,
    run_green,
    submit_review,
)

PROJECT = "vat-calculator"
TRUNK = "refs/heads/main"
ZERO_STAYS_ZERO = CheckContract(
    contract_id="zero-stays-zero",
    observes="the gross amount printed for a net amount of zero",
    launch=Launch(argv=("python3", "vat.py", "0"), timeout_seconds=30),
    expect=Expected(exit_code=0, stdout="0\n"),
)
# Satisfies the standard-rate contract and nothing else.
VAT_ALWAYS_PRINTS_120 = "print(120)\n"
GREETING = ProcessContract(
    contract_id="B2-greeting",
    observes="the greeting the product prints",
    launch=Launch(
        argv=("sh", "-c", "cat greeting.txt 2>/dev/null || echo none"), timeout_seconds=30
    ),
    expect=Expected(exit_code=0, stdout="hello\n"),
    baseline=Baseline(exit_code=0, stdout="none\n"),
)
# Refuses any update of trunk while `refuse` exists beside the hook, and holds one while
# `hold` exists. Other refs pass untouched, so candidates can still be published.
_PRE_RECEIVE = """#!/bin/sh
here="$(dirname "$0")"
while read old new ref; do
  if [ "$ref" = "refs/heads/main" ]; then
    if [ -e "$here/refuse" ]; then echo "trunk is closed" >&2; exit 1; fi
    touch "$here/reached"
    while [ -e "$here/hold" ]; do sleep 0.05; done
  fi
done
exit 0
"""


class TrunkWorld:
    side_by_side = True

    def __init__(
        self,
        database: AsyncConnectionPool,
        tmp_path: Path,
        runner_image: str,
        isolation: Isolation | None = None,
        project: str = PROJECT,
        mirrors: Path | None = None,
    ) -> None:
        self.project = project
        self.database = database
        self.tmp_path = tmp_path
        self.remote = tmp_path / "remote.git"
        self.builder = tmp_path / "builder"
        self.mirrors = mirrors or tmp_path / "mirrors"
        self.image = runner_image
        self.oracle = ProcessOracle(isolation or ContainerIsolation(runner_image))
        self.behaviours = Behaviours(database, self.mirrors, self.oracle)
        self.reviews = Reviews(database, self.mirrors)
        self.integration = Integration(database, self.mirrors, self.oracle)
        self.attempts: dict[str, int] = {}
        self.requests = 0

    def git(self, *arguments: str, at: Path | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(at or self.builder), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    async def open(self, address: str | None = None) -> str:
        """A bare remote whose trunk holds a calculator that returns the net amount."""
        self.remote.mkdir()
        self.git("init", "-q", "--bare", "-b", "main", at=self.remote)
        # No keep-alive traffic: a push whose client has gone must be able to finish.
        self.git("config", "receive.keepAlive", "0", at=self.remote)
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(_PRE_RECEIVE)
        hook.chmod(0o755)
        self.builder.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "builder@relay.test")
        self.git("config", "user.name", "Builder")
        self.git("remote", "add", "origin", str(self.remote))
        self.advance_trunk({"README": "a VAT calculator\n"})
        base = self.advance_trunk({"vat.py": VAT_RETURNS_NET})
        await self.open_project(address or str(self.remote))
        return base

    async def open_project(self, address: str) -> None:
        await self.behaviours.open_project(self.project, address)
        await in_a_story(self.behaviours, self.project)

    def advance_trunk(self, files: dict[str, str]) -> str:
        """Another writer moves trunk, outside Relay."""
        self.git("fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
        if self.git("branch", "--list", "-r", "origin/main"):
            self.git("checkout", "-q", "-B", "main", "origin/main")
        for name, content in files.items():
            (self.builder / name).write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "trunk moves")
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        return self.git("rev-parse", "HEAD")

    def trunk(self) -> str:
        return self.git("rev-parse", TRUNK, at=self.remote)

    def hook_flag(self, name: str) -> Path:
        return self.remote / "hooks" / name

    async def awaiting_builder(
        self, behaviour: str = "B1", contract: ProcessContract = CONTRACT, story: str = "S1"
    ) -> str:
        """A behaviour with a verified red on the current trunk. Returns that revision."""
        basis = await self.basis(story)
        await self.behaviours.propose(
            VERIFIER,
            self.project,
            story,
            behaviour,
            contract.observes,
            ["AC1"],
            basis=basis,
            story_revision=await self.revision(story),
        )
        if self.side_by_side:
            # Relay makes a behaviour wait for the ones before it in its story. The
            # integration tests need candidates competing for trunk, which no caller can
            # arrange, so this fixture puts them all in first place.
            async with self.database.connection() as connection:
                await connection.execute(
                    "UPDATE behaviours SET position = 1 WHERE project_id = %s", (self.project,)
                )
        await self.behaviours.begin_test_design(VERIFIER, self.project, behaviour)
        await self.behaviours.register_acceptance_test(
            VERIFIER, self.project, behaviour, contract, basis=basis
        )
        assert (await self.behaviours.run_red(RELAY_ITSELF, self.project, behaviour)).is_valid
        return self.trunk()

    def publish_attempt(self, behaviour: str, files: dict[str, str], *, on: str) -> str:
        """What a Builder does: commit on top of `on` and publish under the attempt ref."""
        attempt = self.attempts[behaviour] = self.attempts.get(behaviour, 0) + 1
        self.git("fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
        self.git("checkout", "-q", "--detach", on)
        for name, content in files.items():
            (self.builder / name).write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", f"{behaviour} attempt {attempt}")
        candidate = self.git("rev-parse", "HEAD")
        ref = f"refs/relay/attempts/{self.project}/{behaviour}/{attempt}"
        self.git("push", "-q", "origin", f"HEAD:{ref}")
        return candidate

    async def reviewed(
        self,
        behaviour: str = "B1",
        files: dict[str, str] | None = None,
        *,
        on: str,
        reviews: dict[str, Review] | None = None,
    ) -> str:
        """A candidate verified green, published, and approved by every required reviewer."""
        candidate = self.publish_attempt(
            behaviour, files or {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=on
        )
        verdict = await run_green(
            self.behaviours, self.project, behaviour, self.attempts[behaviour]
        )
        assert verdict.is_valid, verdict.reason
        assert await self.reviews.publish_candidates() == 1
        for message in await pending_messages(self.database):
            payload = message.envelope["payload"]
            if payload.get("candidate_revision") == candidate and "task_id" in payload:
                tree = self.tmp_path / f"review-{payload['task_id'][:8]}"
                shutil.rmtree(tree, ignore_errors=True)
                await prepare_review(self.reviews, payload["task_id"], tree)
                said = (reviews or {}).get(message.envelope["recipient"], Review())
                await submit_review(self.reviews, payload["task_id"], said)
        return candidate

    async def integrate(
        self, behaviour: str = "B1", request: str | None = None
    ) -> IntegrationReceipt:
        if request is None:
            self.requests += 1
            request = f"request-{self.requests}"
        return await self.integration.integrate(self.project, behaviour, request)

    def integration_with(
        self, isolation: Isolation | None = None, push_timeout: float = 120.0
    ) -> Integration:
        """Another control-plane process over the same database, mirrors and remote."""
        oracle = ProcessOracle(isolation) if isolation else self.oracle
        return Integration(self.database, self.mirrors, oracle, push_timeout)

    async def until_trunk_is(self, revision: str) -> None:
        async with asyncio.timeout(30):
            while self.trunk() != revision:
                await asyncio.sleep(0.05)

    async def until_someone_waits_for_a_lock(self) -> None:
        """Until PostgreSQL itself reports one of this test's own transactions blocked
        behind another. Other tests share the database, each in a schema of its own, and
        a connection carries its schema in its application name."""
        async with asyncio.timeout(30):
            while await self.column(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND application_name = 'relay:' || current_schema()"
            ) == [0]:
                await asyncio.sleep(0.05)

    async def until_exists(self, flag: Path) -> None:
        async with asyncio.timeout(30):
            while not flag.exists():
                await asyncio.sleep(0.05)

    async def revision(self, story: str = "S1") -> int:
        """The story's revision, as a role would read it just before proposing."""
        [revision] = await self.column(
            f"SELECT revision FROM stories WHERE project_id = '{self.project}' "
            f"AND story_id = '{story}'"
        )
        assert isinstance(revision, int)
        return revision

    async def basis(self, story: str = "S1") -> int:
        """The decision basis a role would read with the story, just before it submits."""
        [basis] = await self.column(f"SELECT story_basis('{self.project}', '{story}')")
        assert isinstance(basis, int)
        return basis

    async def state(self, behaviour: str = "B1") -> BehaviourState:
        return await self.behaviours.state_of(self.project, behaviour)

    async def events(self) -> list[str]:
        return [event.event_type for event in await read_events(self.database, self.project)]

    async def column(self, query: str) -> list[object]:
        async with self.database.connection() as connection:
            rows = await (await connection.execute(query)).fetchall()
        return [next(iter(row.values())) for row in rows]
