"""A VAT calculator whose baseline returns the net amount unchanged, in a real Git repository."""

import asyncio
import contextlib
import subprocess
import uuid
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from relay.application.assignments import Assignments, announce_again
from relay.application.behaviours import Behaviours
from relay.application.reviews import Receipt as SubmissionReceipt
from relay.application.reviews import Review, Reviews
from relay.application.stories import Stories, StoryDraft
from relay.domain.evidence import Baseline, Expected, GreenVerdict, Launch, ProcessContract
from relay.domain.story import ActionRefused, HumanAction, Principal, PrincipalKind
from relay.testing.oracle import Isolation, Run

CONTRACT = ProcessContract(
    contract_id="B1-standard-rate",
    observes="the gross amount printed for a net amount at the standard rate",
    launch=Launch(argv=("python3", "vat.py", "100"), timeout_seconds=30),
    expect=Expected(exit_code=0, stdout="120\n"),
    baseline=Baseline(exit_code=0, stdout="100\n"),
)
VAT_RETURNS_NET = "import sys\nprint(int(sys.argv[1]))\n"
VAT_APPLIES_STANDARD_RATE = "import sys\nprint(int(sys.argv[1]) * 120 // 100)\n"


def commit(repository: Path, files: dict[str, str]) -> str:
    """Commit files to a real Git repository, creating it on first use; return the commit hash."""

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True
        ).stdout.strip()

    if not (repository / ".git").exists():
        repository.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "verifier@relay.test")
        git("config", "user.name", "Verifier")
    for name, content in files.items():
        (repository / name).write_text(content)
    git("add", "-A")
    git("commit", "-q", "-m", "change")
    return git("rev-parse", "HEAD")


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def publish_attempt(
    repository: Path,
    behaviour_id: str,
    attempt: int,
    files: dict[str, str],
    *,
    on: str,
    project: str = "vat-calculator",
) -> str:
    """What a Builder does: commit a change on top of `on` and publish it under the attempt's
    ref, leaving trunk where it was. Returns the candidate's commit hash."""
    _git(repository, "checkout", "-q", "--detach", on)
    for name, content in files.items():
        (repository / name).write_text(content)
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "--allow-empty", "-m", f"attempt {attempt}")
    candidate = _git(repository, "rev-parse", "HEAD")
    _git(
        repository,
        "update-ref",
        f"refs/relay/attempts/{project}/{behaviour_id}/{attempt}",
        candidate,
    )
    _git(repository, "checkout", "-q", "main")
    return candidate


def publish_tag_as_attempt(
    repository: Path, behaviour_id: str, attempt: int, *, pointing_at: str
) -> str:
    """Publish an annotated tag object, not a commit, under the attempt's ref."""
    _git(repository, "tag", "-a", "-m", "not a commit", f"decoy-{attempt}", pointing_at)
    tag_object = _git(repository, "rev-parse", f"refs/tags/decoy-{attempt}")
    _git(
        repository,
        "update-ref",
        f"refs/relay/attempts/vat-calculator/{behaviour_id}/{attempt}",
        tag_object,
    )
    return tag_object


class CountingLaunches:
    """Wraps an isolation and counts how often a subject was actually launched."""

    def __init__(self, isolation: Isolation) -> None:
        self._isolation = isolation
        self.name = isolation.name
        self.launches = 0

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> Run:
        self.launches += 1
        return await self._isolation.run(workdir, launch, capture_limit)


class HeldAtABarrier:
    """Wraps an isolation so a run finishes observing but does not return until released,
    which lets a test change the world while the run is in flight."""

    def __init__(self, isolation: Isolation) -> None:
        self._isolation = isolation
        self.name = isolation.name
        self.observed = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> Run:
        run = await self._isolation.run(workdir, launch, capture_limit)
        self.observed.set()
        await self.release.wait()
        return run


async def held_at_the_barrier(run: asyncio.Task[Any], barrier: HeldAtABarrier) -> None:
    """Wait until the run is held. If it ends first, fail at once with its own error
    instead of waiting for a barrier it will never reach."""
    reached = asyncio.create_task(barrier.observed.wait())
    async with asyncio.timeout(120):
        await asyncio.wait({run, reached}, return_when=asyncio.FIRST_COMPLETED)
    if not reached.done():
        reached.cancel()
        raise AssertionError("the run ended before reaching the barrier") from run.exception()


PRODUCT_OWNER = Principal(PrincipalKind.HUMAN, "product-owner")
ANALYST = Principal(PrincipalKind.WORKER, "worker-analyst", role="analyst")
VERIFIER = Principal(PrincipalKind.WORKER, "worker-verifier", role="verifier")


async def in_a_story(behaviours: Behaviours, project: str) -> None:
    """The shortest real route to somewhere a behaviour can live: a feature, a one-story
    roadmap the Product Owner approves, and so story S1 released with criterion AC1."""
    stories = Stories(behaviours._pool)
    await stories.open_feature(PRODUCT_OWNER, project, "F1", "prices must include VAT")
    story = StoryDraft("S1", "Standard rate", "a gross price is shown", {"AC1": "120 for 100"})
    gate = await stories.publish_roadmap(ANALYST, project, "F1", [story], basis=0)
    await stories.decide(HumanAction(PRODUCT_OWNER, f"approve-{gate}", str(gate), "approve"))
    # The notice that the roadmap was waiting has done its job. Tests that count what is
    # still to be sent are counting what their own behaviour caused.
    async with behaviours._pool.connection() as connection:
        await connection.execute(
            "UPDATE outbox SET published_at = now() WHERE stream LIKE %s", ("%:inbox:%",)
        )


STORY_SET_UP = [
    "feature.opened",
    "roadmap.gate_opened",
    "gate.approved",
    "story.released",
]


BUILDER = Principal(PrincipalKind.WORKER, "worker-builder", role="builder")


async def build_grant(behaviours: Behaviours, project: str, behaviour: str) -> uuid.UUID:
    """What a Builder's worker does on receiving its command: claim the build. Where
    nothing is owed, a grant nobody was given, as a stray caller would have."""
    pool = behaviours._pool
    async with pool.connection() as connection:
        owed = await (
            await connection.execute(
                "SELECT assignment_id FROM assignments WHERE project_id = %s "
                "AND behaviour_id = %s AND kind IN ('build', 'remediate') "
                "AND state IN ('wanted', 'granted')",
                (project, behaviour),
            )
        ).fetchone()
    if owed is None:
        return uuid.uuid7()
    return await Assignments(pool).claim(BUILDER, owed["assignment_id"])


async def run_green(
    behaviours: Behaviours, project: str, behaviour: str, attempt: int
) -> GreenVerdict:
    """Claim the build, then ask for the green run under that grant."""
    grant = await build_grant(behaviours, project, behaviour)
    return await behaviours.run_green(project, behaviour, attempt, grant)


async def prepare_review(reviews: Reviews, capability: str, into: Path) -> Path:
    """What a reviewer's worker does on receiving its command: claim the review, then
    ask for the tree under that grant. Where the review cannot be claimed, a grant
    nobody was given, as a stray caller would have."""
    pool = reviews._pool
    async with pool.connection() as connection:
        task = await (
            await connection.execute(
                "SELECT assignment_id, role FROM review_tasks WHERE capability = %s",
                (capability,),
            )
        ).fetchone()
    grant = uuid.uuid7()
    if task is not None:
        reviewer = Principal(PrincipalKind.WORKER, f"worker-{task['role']}", role=task["role"])
        with contextlib.suppress(ActionRefused):
            grant = await Assignments(pool).claim(reviewer, task["assignment_id"])
    return (await reviews.prepare_review(capability, into, grant)).tree


async def announce_wanted_work_again(pool: AsyncConnectionPool, project: str) -> int:
    async with pool.connection() as connection, connection.transaction():
        return await announce_again(connection, project)


async def submit_review(reviews: Reviews, capability: str, review: Review) -> SubmissionReceipt:
    """Submit as the worker that holds the review now would: under its current grant, and
    from the tree it was last handed. Where there is neither, identifiers nobody was given."""
    async with reviews._pool.connection() as connection:
        held = await (
            await connection.execute(
                "SELECT a.grant_id, (SELECT c.receipt_id FROM review_checkouts c "
                "WHERE c.task_id = t.task_id AND c.grant_id = a.grant_id "
                "ORDER BY c.prepared_at DESC, c.receipt_id DESC LIMIT 1) AS receipt "
                "FROM review_tasks t JOIN assignments a USING (assignment_id) "
                "WHERE t.capability = %s AND a.grant_id IS NOT NULL",
                (capability,),
            )
        ).fetchone()
    grant = held["grant_id"] if held else uuid.uuid7()
    receipt = (held["receipt"] if held else None) or uuid.uuid7()
    return await reviews.submit_review(capability, review, grant, receipt)
