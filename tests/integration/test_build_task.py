"""A build carried out by a worker. The Builder changes files in a tree it was handed and
says so; the supervisor keeps that tree as a commit, publishes it under a fresh attempt,
and Relay observes whether it is green. The provider is scripted: it edits the workspace
as a model with a shell would, and replies."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.invocations import Invocations
from relay.application.reviews import NewFinding, Review
from relay.application.tasks import BuildTask, Profile, Tasks, Workspaces
from relay.application.worker import Worker
from relay.application.workers import WorkerSession
from relay.domain.behaviour import BehaviourState
from relay.domain.review import Severity
from relay.domain.story import ActionRefused
from relay.git.checkout import CheckoutFailed
from relay.git.mirror import Mirror
from relay.providers.claude import ProviderResult, Request
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import BUILDER, VAT_APPLIES_STANDARD_RATE, VAT_RETURNS_NET
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_review_task import replying
from tests.integration.test_worker import RUNTIME, SeenToHold
from tests.integration.trunk_world import PROJECT

RIGHT = {"vat.py": VAT_APPLIES_STANDARD_RATE}
WRONG = {"vat.py": "import sys\nprint(int(sys.argv[1]) * 2)\n"}


class Builder:
    """Each call: look at what was handed over, make the next set of changes, reply."""

    def __init__(self, *changes: dict[str, str | None], replies: list[Any] | None = None) -> None:
        self._changes = list(changes)
        self._replies = replies or []
        self.asked: list[Request] = []
        self.found: list[dict[str, str]] = []

    async def invoke(self, request: Request, **_: Any) -> ProviderResult:
        self.asked.append(request)
        self.found.append(
            {
                str(path.relative_to(request.workdir)): path.read_text()
                for path in sorted(request.workdir.rglob("*"))
                if path.is_file()
            }
        )
        for name, content in self._changes.pop(0).items():
            target = request.workdir / name
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        return replying(
            self._replies.pop(0) if self._replies else {"note": "applied the standard rate"}
        )


class Building:
    def __init__(self, world: DecisionWorld, tmp_path: Path) -> None:
        self.world = world
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "alf-atdd-developer.md").write_text(
            "---\nname: alf-atdd-developer\n---\nBuild.\n"
        )
        self.invocations = Invocations(world.database)
        self.task = BuildTask(
            world.database,
            world.behaviours,
            self.invocations,
            Workspaces(tmp_path.resolve() / "workspaces"),
            world.mirrors,
            [agents],
            Profile("claude-sonnet-5-5"),
        )
        self.invocations._effects_for = Tasks(
            {"build": self.task, "remediate": self.task}
        ).effects_for
        self.sessions: list[WorkerSession] = []

    async def worker(self, provider: Any) -> Worker:
        session = await WorkerSession.start(self.world.database, "builder")
        self.sessions.append(session)
        return Worker(
            session,
            self.world.claims,
            self.invocations,
            provider,
            {"build": self.task, "remediate": self.task},
            SeenToHold(),
            lambda: RUNTIME,
        )

    async def owed(self) -> dict[str, Any]:
        async with self.world.database.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM assignments WHERE kind IN ('build', 'remediate') "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            ).fetchone()
        assert row is not None
        return row

    def at(self, ref: str) -> str:
        """What the project's repository has under a ref; empty if nothing."""
        listed = subprocess.run(
            ["git", "ls-remote", str(self.world.remote), ref],
            check=True, capture_output=True, text=True,
        ).stdout.split()  # fmt: skip
        return listed[0] if listed else ""

    def show(self, revision: str, path: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.world.remote), "show", f"{revision}:{path}"],
            check=True, capture_output=True, text=True,
        ).stdout  # fmt: skip

    async def states(self) -> list[object]:
        return await self.world.column("SELECT state FROM invocations ORDER BY started_at")


@pytest.fixture
async def building(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> AsyncIterator[Building]:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    building = Building(world, tmp_path)
    yield building
    await building.invocations.close()
    for session in building.sessions:
        await session.stop()


async def test_what_the_builder_left_is_published_as_an_attempt_and_found_green(
    building: Building,
) -> None:
    provider = Builder(RIGHT)
    worker = await building.worker(provider)

    assert await worker.carry_out(await building.owed()) == "applied"

    candidate = building.at(f"refs/relay/attempts/{PROJECT}/B1/1")
    assert candidate
    assert building.show(candidate, "vat.py") == VAT_APPLIES_STANDARD_RATE
    assert building.show(f"{candidate}^", "vat.py") == VAT_RETURNS_NET  # on top of the red's commit
    assert await building.world.column("SELECT retained FROM invocations") == [candidate]
    assert await building.world.column("SELECT state FROM green_runs") == ["finished"]
    assert (await building.owed())["state"] == "done"
    assert await building.world.state("B1") is BehaviourState.REVIEW
    assert await building.world.column(
        "SELECT name || ':' || state FROM invocation_effects ORDER BY position"
    ) == ["publish:done", "green:done"]


async def test_the_builder_is_handed_the_tree_to_start_from_and_no_repository(
    building: Building,
) -> None:
    provider = Builder(RIGHT)
    await (await building.worker(provider)).carry_out(await building.owed())

    [tree], [asked] = provider.found, provider.asked
    assert tree["vat.py"] == VAT_RETURNS_NET
    assert not any(name.startswith(".git") for name in tree)
    assert asked.confinement.tools == ("Read", "Grep", "Glob", "Edit", "Write", "Bash")
    sandbox = asked.confinement.settings["sandbox"]
    assert str(building.world.mirrors) in sandbox["filesystem"]["denyRead"]
    assert sandbox["filesystem"]["allowRead"] == [str(asked.workdir)]
    assert asked.definition.name == "alf-atdd-developer"
    assert "Behaviour B1" in asked.briefing and "Do not use Git" in asked.briefing
    assert "the gross amount printed" in asked.briefing


async def test_a_candidate_that_is_not_green_is_tried_again_under_a_new_attempt(
    building: Building,
) -> None:
    provider = Builder(WRONG, RIGHT)
    worker = await building.worker(provider)

    assert await worker.carry_out(await building.owed()) == "applied"

    assert await building.states() == ["rejected", "applied"]
    assert "not green" in provider.asked[1].briefing
    first, second = (building.at(f"refs/relay/attempts/{PROJECT}/B1/{n}") for n in (1, 2))
    assert first and second and first != second  # the first attempt is left as it was
    assert building.show(second, "vat.py") == VAT_APPLIES_STANDARD_RATE
    assert provider.found[1]["vat.py"] == VAT_RETURNS_NET  # each try starts from the base


async def test_a_workspace_left_with_git_metadata_in_it_is_not_a_candidate(
    building: Building,
) -> None:
    provider = Builder({**RIGHT, ".git/config": "[core]\n\thooksPath = /tmp/x\n"}, RIGHT)
    worker = await building.worker(provider)

    assert await worker.carry_out(await building.owed()) == "applied"

    assert await building.states() == ["rejected", "applied"]
    assert "Git metadata" in provider.asked[1].briefing
    assert building.at(f"refs/relay/attempts/{PROJECT}/B1/2") == ""  # only one was published


async def test_a_build_whose_worker_went_after_publishing_is_judged_without_asking_again(
    building: Building,
) -> None:
    """S3-18.4: the push was accepted, and then the worker stopped. Recovery starts at
    the first effect not done and the model is not called."""
    provider = Builder(RIGHT)
    worker = await building.worker(provider)
    real = building.task._green

    async def the_worker_stops(*_: Any) -> Any:
        raise KeyboardInterrupt

    building.task._green = the_worker_stops  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await worker.carry_out(await building.owed())
    building.task._green = real  # type: ignore[method-assign]
    candidate = building.at(f"refs/relay/attempts/{PROJECT}/B1/1")
    assert candidate and await building.world.column("SELECT count(*) FROM green_runs") == [0]
    await building.sessions[-1].stop()

    await building.invocations.recover(PROJECT)

    assert await building.states() == ["applied"]
    assert len(provider.asked) == 1
    assert building.at(f"refs/relay/attempts/{PROJECT}/B1/1") == candidate
    assert await building.world.column("SELECT state FROM green_runs") == ["finished"]


async def test_a_build_whose_worker_went_just_after_it_replied_loses_nothing(
    building: Building,
) -> None:
    """S3-19.5: the tree was kept before the reply was recorded, so the scratch directory
    can go with the worker."""
    provider = Builder(RIGHT)
    worker = await building.worker(provider)
    real = building.invocations.apply

    async def the_worker_stops(*_: Any) -> str:
        raise KeyboardInterrupt

    building.invocations.apply = the_worker_stops  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await worker.carry_out(await building.owed())
    building.invocations.apply = real  # type: ignore[method-assign]
    await building.sessions[-1].stop()
    shutil.rmtree(provider.asked[0].workdir.parent)

    await building.invocations.recover(PROJECT)

    assert await building.states() == ["applied"]
    candidate = building.at(f"refs/relay/attempts/{PROJECT}/B1/1")
    assert building.show(candidate, "vat.py") == VAT_APPLIES_STANDARD_RATE
    assert len(provider.asked) == 1


async def test_a_repair_starts_from_the_candidate_that_was_sent_back(
    building: Building,
) -> None:
    """S3-19.6: a finding about a file the first build added. The repair has that file,
    and the first attempt is left as it was."""
    world = building.world
    base = world.base
    finding = NewFinding(Severity.HIGH, "helper.py evals its input", "helper.py line 1", "parse it")
    first = await world.reviewed(
        "B1",
        {**RIGHT, "helper.py": "eval('1')\n"},
        on=base,
        reviews={"sentinel": Review(findings=(finding,))},
    )
    owed = await building.owed()
    assert owed["kind"] == "remediate"
    provider = Builder({"helper.py": "int('1')\n"})

    assert await (await building.worker(provider)).carry_out(owed) == "applied"

    [tree] = provider.found
    assert tree["helper.py"] == "eval('1')\n"  # the candidate being repaired, not the red's tree
    assert "helper.py evals its input" in provider.asked[0].briefing
    assert building.at(f"refs/relay/attempts/{PROJECT}/B1/1") == first
    repaired = building.at(f"refs/relay/attempts/{PROJECT}/B1/2")
    assert building.show(repaired, "helper.py") == "int('1')\n"
    assert building.show(f"{repaired}^", "helper.py") == "eval('1')\n"


async def test_a_reply_that_says_nothing_of_what_was_done_is_asked_for_again(
    building: Building,
) -> None:
    provider = Builder(RIGHT, RIGHT, replies=[{"wrote": "vat.py"}])
    worker = await building.worker(provider)

    assert await worker.carry_out(await building.owed()) == "applied"

    assert await building.states() == ["rejected", "applied"]
    assert "the reply has no note" in provider.asked[1].briefing
    assert await building.world.column(
        "SELECT retained IS NULL FROM invocations ORDER BY started_at"
    ) == [True, False]  # nothing was kept of the reply that was not one


async def test_a_push_that_was_accepted_and_not_heard_of_is_not_made_again(
    building: Building, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3-18.4: the worker stopped after the repository took the candidate and before it
    knew. Taken up again, the publication finds the attempt already there."""
    provider = Builder(RIGHT)
    worker = await building.worker(provider)
    real = Mirror.publish

    async def accepted_and_then_the_worker_stops(
        self: Mirror, *arguments: Any, **named: Any
    ) -> None:
        await real(self, *arguments, **named)
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "publish", accepted_and_then_the_worker_stops)
        with pytest.raises(KeyboardInterrupt):
            await worker.carry_out(await building.owed())
    candidate = building.at(f"refs/relay/attempts/{PROJECT}/B1/1")
    assert candidate
    await building.sessions[-1].stop()

    await building.invocations.recover(PROJECT)

    assert await building.states() == ["applied"]
    assert building.at(f"refs/relay/attempts/{PROJECT}/B1/1") == candidate
    assert building.at(f"refs/relay/attempts/{PROJECT}/B1/2") == ""


async def test_an_attempt_that_is_already_someone_elses_candidate_is_not_overwritten(
    building: Building,
) -> None:
    taken = building.world.publish_attempt("B1", WRONG, on=building.world.base)
    provider = Builder(RIGHT, RIGHT)
    worker = await building.worker(provider)

    assert await worker.carry_out(await building.owed()) == "applied"

    assert await building.states() == ["rejected", "applied"]
    assert "already another candidate" in provider.asked[1].briefing
    assert building.at(f"refs/relay/attempts/{PROJECT}/B1/1") == taken
    assert building.show(building.at(f"refs/relay/attempts/{PROJECT}/B1/2"), "vat.py") == (
        VAT_APPLIES_STANDARD_RATE
    )


async def test_a_candidate_already_judged_is_not_judged_again(building: Building) -> None:
    """S3-19.4: the green was observed and recorded, and then the worker stopped."""
    provider = Builder(RIGHT)
    worker = await building.worker(provider)
    real = building.task._green

    async def judged_and_then_the_worker_stops(run: Any) -> Any:
        await real(run)
        raise KeyboardInterrupt

    building.task._green = judged_and_then_the_worker_stops  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await worker.carry_out(await building.owed())
    building.task._green = real  # type: ignore[method-assign]
    await building.sessions[-1].stop()

    await building.invocations.recover(PROJECT)

    assert await building.states() == ["applied"]
    assert await building.world.column("SELECT state FROM green_runs") == ["finished"]
    assert len(provider.asked) == 1


async def test_a_candidate_found_not_green_is_not_taken_for_green_when_taken_up_again(
    building: Building,
) -> None:
    """3B-S2-1.3: the oracle rejected the candidate, the verdict was recorded, and the
    worker stopped before the invocation heard of it. The verdict on record is what is
    replayed, and the next call is told why."""
    provider = Builder(WRONG, RIGHT)
    worker = await building.worker(provider)
    real = building.task._green

    async def judged_and_then_the_worker_stops(run: Any) -> Any:
        with pytest.raises(ActionRefused):
            await real(run)
        raise KeyboardInterrupt

    building.task._green = judged_and_then_the_worker_stops  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await worker.carry_out(await building.owed())
    building.task._green = real  # type: ignore[method-assign]
    await building.sessions[-1].stop()

    await building.invocations.recover(PROJECT)

    assert await building.states() == ["rejected"]
    assert await building.world.column("SELECT count(*) FROM green_runs") == [1]  # not run again
    [reason] = await building.world.column("SELECT reason FROM invocations")
    assert isinstance(reason, str) and reason.startswith("the candidate is not green")
    again = await building.worker(provider)
    assert await again.carry_out(await building.owed()) == "applied"
    assert "not green" in provider.asked[1].briefing


async def test_a_green_run_whose_caller_has_been_taken_over_opens_no_review(
    building: Building,
) -> None:
    """3B-S2-1.4: asked in the transaction that would accept the verdict."""
    world = building.world
    world.publish_attempt("B1", RIGHT, on=world.base)
    grant = await world.claims.claim(BUILDER, (await building.owed())["assignment_id"])

    async def taken_over(_: Any) -> bool:
        return False

    await world.behaviours.run_green(PROJECT, "B1", 1, grant, still_heard=taken_over)

    assert await world.column("SELECT count(*) FROM review_cycles") == [0]
    assert await world.state("B1") is BehaviourState.IMPLEMENTING
    assert (await building.owed())["state"] == "granted"  # still owed: nothing was decided


async def test_a_green_run_is_of_the_candidate_that_was_kept_and_of_no_other(
    building: Building,
) -> None:
    """3B-S2-1.4: the attempt ref is read again at the run; it has to name the same commit."""
    world = building.world
    published = world.publish_attempt("B1", RIGHT, on=world.base)
    grant = await world.claims.claim(BUILDER, (await building.owed())["assignment_id"])

    with pytest.raises(CheckoutFailed, match="no longer names the candidate"):
        await world.behaviours.run_green(PROJECT, "B1", 1, grant, expecting="a" * 40)

    assert await world.column("SELECT count(*) FROM review_cycles") == [0]
    assert (await world.behaviours.run_green(PROJECT, "B1", 1, grant, expecting=published)).is_valid


async def test_only_the_verifier_reopens_a_red(building: Building) -> None:
    """3B-S2-1.10."""
    with pytest.raises(ActionRefused, match="for the verifier"):
        await building.world.behaviours.reopen_red(BUILDER, PROJECT, "B1", "I would like another")

    assert (await building.owed())["state"] == "wanted"


async def test_an_attempt_that_was_moved_after_it_was_published_is_not_observed(
    building: Building,
) -> None:
    """3B-S2-1.4: the build task has Relay observe the commit it kept, and no other the
    ref may come to name. That is nobody's defective reply."""
    provider = Builder(RIGHT)
    worker = await building.worker(provider)
    real = building.task._publish

    def move(ref: str) -> None:
        # Somebody with access to the repository points the attempt at another commit.
        subprocess.run(
            ["git", "-C", str(building.world.builder), "push", "-q", "-f", "origin",
             f"{building.world.base}:{ref}"],
            check=True,
        )  # fmt: skip

    async def published_and_then_moved(run: Any) -> Any:
        done = await real(run)
        move(done["ref"])
        return done

    building.task._publish = published_and_then_moved  # type: ignore[method-assign]

    assert await worker.carry_out(await building.owed()) == "overtaken"

    assert await building.states() == ["overtaken"]
    assert await building.world.column("SELECT count(*) FROM review_cycles") == [0]
    assert await building.invocations.parked(PROJECT) == []


async def test_the_build_task_asks_for_its_own_commit_and_stays_answerable_for_its_try(
    building: Building,
) -> None:
    """3B-S2-1.4: what the task hands the green run. The commit is the one it kept, and
    the question of whether it may still be heard is answered from the record: yes while
    this try is the effect's current one, no once it has been taken over."""
    provider = Builder(RIGHT)
    worker = await building.worker(provider)
    real = building.world.behaviours.run_green
    heard: list[Any] = []

    async def looked_at(*arguments: Any, **named: Any) -> Any:
        async with building.world.database.connection() as connection:
            heard.append(named["expecting"])
            heard.append(await named["still_heard"](connection))
            await connection.execute(
                "UPDATE invocation_effects SET try = try + 1 WHERE name = 'green'"
            )
            heard.append(await named["still_heard"](connection))
            await connection.execute(
                "UPDATE invocation_effects SET try = try - 1 WHERE name = 'green'"
            )
        return await real(*arguments, **named)

    building.world.behaviours.run_green = looked_at  # type: ignore[method-assign]

    assert await worker.carry_out(await building.owed()) == "applied"

    [kept] = await building.world.column("SELECT retained FROM invocations")
    assert heard == [kept, True, False]
