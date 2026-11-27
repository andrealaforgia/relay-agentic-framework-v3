"""A build carried out by a worker. The Builder changes files in a tree it was handed and
says so; the supervisor keeps that tree as a commit, publishes it under a fresh attempt,
and Relay observes whether it is green. The provider is scripted: it edits the workspace
as a model with a shell would, and replies."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import behaviours as behaviours_module
from relay.application.invocations import Invocations
from relay.application.reviews import NewFinding, Review
from relay.application.tasks import BuildTask, Profile, Tasks, Workspaces
from relay.application.worker import Worker
from relay.application.workers import WorkerSession
from relay.domain.behaviour import BehaviourState
from relay.domain.review import Severity
from relay.domain.story import ActionRefused
from relay.git.checkout import CheckoutFailed, RemoteUnavailable
from relay.git.mirror import Mirror
from relay.providers.claude import ProviderResult, Request
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import (
    BUILDER,
    VAT_APPLIES_STANDARD_RATE,
    VAT_RETURNS_NET,
    VERIFIER,
)
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
        Tasks({"build": self.task, "remediate": self.task}).attach(self.invocations)
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

    async def carry(self, worker: Worker) -> str:
        """What the stream, the Builder's worker and the control plane's pass do between
        them for a build: the worker makes the call and keeps what it left; the control
        plane publishes and observes it; and work the control plane hands back is
        delivered again. Returns where the build's last call was left."""
        left = "nothing"
        for _ in range(4):
            left = await worker.carry_out(await self.owed())
            if left != "handed_on":
                return left
            await self.invocations.recover(PROJECT)
            [left, wanted] = (
                await self.world.column(
                    "SELECT v.state || '|' || (a.state = 'wanted' AND a.parked IS NULL) "
                    "FROM invocations v JOIN assignments a USING (assignment_id) "
                    "ORDER BY v.started_at DESC LIMIT 1"
                )
            )[0].split("|")  # type: ignore[union-attr]
            if left != "rejected" or wanted != "true":
                return left
        return left

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

    assert await building.carry(worker) == "applied"

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

    assert await building.carry(worker) == "applied"

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

    assert await building.carry(worker) == "applied"

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
        await building.carry(worker)
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
        await building.carry(worker)
    building.invocations.apply = real  # type: ignore[method-assign]
    await building.sessions[-1].stop()
    assert not provider.asked[0].workdir.parent.exists()

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

    assert await building.carry(await building.worker(provider)) == "applied"

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

    assert await building.carry(worker) == "applied"

    assert await building.states() == ["rejected", "applied"]
    assert "the reply has no outcome" in provider.asked[1].briefing
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
            await building.carry(worker)
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

    assert await building.carry(worker) == "applied"

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
        await building.carry(worker)
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
        await building.carry(worker)
    building.task._green = real  # type: ignore[method-assign]
    await building.sessions[-1].stop()

    await building.invocations.recover(PROJECT)

    assert await building.states() == ["rejected"]
    assert await building.world.column("SELECT count(*) FROM green_runs") == [1]  # not run again
    [reason] = await building.world.column("SELECT reason FROM invocations")
    assert isinstance(reason, str) and reason.startswith("the candidate is not green")
    again = await building.worker(provider)
    assert await building.carry(again) == "applied"
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


@pytest.mark.parametrize("what_became_of_it", ["moved", "removed"])
async def test_an_attempt_that_is_no_longer_the_candidate_is_not_observed_and_is_built_again(
    building: Building, what_became_of_it: str
) -> None:
    """3B-S2-1.4 and 3B-S2-2.9: the build task has Relay observe the commit it kept, and
    no other the ref may come to name. A ref that names another commit, and one the
    repository says is not there at all, are the same thing: nothing of the reply is
    left to observe, and that is nobody's defective reply. The work is still owed, so it
    is wanted again, and is done again under a new attempt."""
    provider = Builder(RIGHT, RIGHT)
    worker = await building.worker(provider)
    real = building.task._publish

    def interfere(ref: str) -> None:
        # Somebody with access to the repository points the attempt at another commit,
        # or deletes it.
        pushed = f"{building.world.base}:{ref}" if what_became_of_it == "moved" else f":{ref}"
        subprocess.run(
            ["git", "-C", str(building.world.builder), "push", "-q", "-f", "origin", pushed],
            check=True,
        )

    async def published_and_then_interfered_with(run: Any) -> Any:
        done = await real(run)
        if done["attempt"] == 1:
            interfere(done["ref"])
        return done

    building.task._publish = published_and_then_interfered_with  # type: ignore[method-assign]

    assert await building.carry(worker) == "overtaken"

    assert await building.states() == ["overtaken"]
    assert await building.world.column("SELECT count(*) FROM review_cycles") == [0]
    assert await building.invocations.parked(PROJECT) == []
    assert (await building.owed())["state"] == "wanted"
    await building.invocations.recover(PROJECT)  # nothing is left for the pass to retry
    assert await building.states() == ["overtaken"]
    assert await building.carry(worker) == "applied"
    assert await building.states() == ["overtaken", "applied"]
    assert await building.world.column("SELECT attempt FROM build_attempts ORDER BY attempt") == [
        1,
        2,
    ]


async def test_a_repository_that_does_not_answer_is_not_a_candidate_that_is_gone(
    building: Building, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3B-S2-2.9: nothing is concluded from a repository that could not be asked. The
    result is kept, and is applied when it can be."""
    provider = Builder(RIGHT)
    worker = await building.worker(provider)
    real = behaviours_module.attempt_head
    asked = 0

    async def unreachable_once(*arguments: Any) -> str:
        nonlocal asked
        asked += 1
        if asked == 1:
            raise RemoteUnavailable("the repository did not answer")
        return await real(*arguments)

    monkeypatch.setattr(behaviours_module, "attempt_head", unreachable_once)

    with pytest.raises(RemoteUnavailable):
        await building.carry(worker)

    assert await building.states() == ["returned"]
    await building.invocations.recover(PROJECT)
    assert await building.states() == ["applied"]
    assert len(provider.asked) == 1


@pytest.mark.parametrize("the_first_try_ends", ["before the second reads the record", "last"])
async def test_what_a_try_saw_after_it_was_taken_over_is_nobodys_verdict(
    building: Building, the_first_try_ends: str
) -> None:
    """3B-S2-2.2: the first try of the green effect is still observing when the control
    plane that took it is taken for gone, and another takes the effect over. Whenever
    the first try then ends, what it saw is kept as what was seen and judges nothing:
    the second try does not take it for a verdict, and observes for itself."""
    provider = Builder(RIGHT)
    worker = await building.worker(provider)
    assert await worker.carry_out(await building.owed()) == "handed_on"
    successor = Invocations(building.world.database)
    Tasks({"build": building.task, "remediate": building.task}).attach(successor)
    oracle = building.world.behaviours._oracle
    observe, observed = oracle.execute, 0
    first_is_observing, first_may_end = asyncio.Event(), asyncio.Event()

    async def execute(*arguments: Any) -> Any:
        nonlocal observed
        observed += 1
        mine = observed
        execution = await observe(*arguments)
        if mine == 1:
            first_is_observing.set()
            await first_may_end.wait()
        return execution

    oracle.execute = execute  # type: ignore[method-assign]
    first = asyncio.create_task(building.invocations.recover(PROJECT))
    await first_is_observing.wait()
    assert building.invocations._session is not None
    await building.invocations._session.stop()  # gone, to everyone else; its run goes on
    attempt_of = building.task._attempt

    async def as_the_second_try_is_about_to_read_the_record(invocation: Any) -> int:
        number = await attempt_of(invocation)
        if the_first_try_ends != "last" and not first_may_end.is_set():
            first_may_end.set()
            await first
        return number

    building.task._attempt = as_the_second_try_is_about_to_read_the_record  # type: ignore[method-assign]

    await successor.recover(PROJECT)
    first_may_end.set()
    await first
    await successor.close()

    assert observed == 2
    assert await building.states() == ["applied"]
    assert await building.world.state("B1") is BehaviourState.REVIEW
    assert await building.world.column("SELECT count(*) FROM review_cycles") == [1]
    judged = await building.world.column(
        "SELECT (payload->>'judged')::boolean FROM events WHERE event_type IN "
        "('behaviour.green_verified', 'behaviour.green_rejected') ORDER BY sequence"
    )
    assert sorted(judged) == [False, True]


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

    assert await building.carry(worker) == "applied"

    [kept] = await building.world.column("SELECT retained FROM invocations")
    assert heard == [kept, True, False]


async def test_a_green_found_while_its_story_gained_a_behaviour_opens_no_review(
    building: Building,
) -> None:
    """3B-S2-3.2: the Verifier adds a behaviour to the story while the candidate is
    being observed. The call was admitted on the story as it was, so its result is
    overtaken, and that has to be known where the green would be accepted: a review
    opened from a result the record then calls overtaken is a contradiction on record.
    What was observed is kept; the build is owed again."""
    world = building.world
    provider = Builder(RIGHT)
    worker = await building.worker(provider)
    oracle = world.behaviours._oracle
    observe = oracle.execute

    async def execute(*arguments: Any) -> Any:
        execution = await observe(*arguments)
        await world.behaviours.propose(
            VERIFIER,
            PROJECT,
            "S1",
            "B2",
            "the reduced rate is applied",
            ["AC1"],
            basis=await world.basis(),
            story_revision=await world.revision(),
        )
        return execution

    oracle.execute = execute  # type: ignore[method-assign]

    assert await building.carry(worker) == "overtaken"

    assert await world.column("SELECT count(*) FROM review_cycles") == [0]
    assert await world.state("B1") is BehaviourState.IMPLEMENTING
    assert await world.column("SELECT state FROM green_runs") == ["finished"]
    assert await world.column(
        "SELECT payload->>'rejection' FROM events WHERE event_type = 'behaviour.green_rejected'"
    ) == ["assignment_superseded"]
    owed = await world.column("SELECT state FROM assignments WHERE kind = 'build'")
    assert owed == ["wanted"]


async def test_no_tree_and_no_base_is_kept_once_a_build_is_finished_with(
    building: Building,
) -> None:
    """3B-S2-3.8."""
    worker = await building.worker(Builder(WRONG, RIGHT))

    assert await building.carry(worker) == "applied"

    assert list(building.task._workspaces._root.iterdir()) == []
    assert building.task._bases == {}


async def test_no_base_is_kept_for_a_build_that_was_prepared_and_never_called(
    building: Building,
) -> None:
    """3B-S2-3.8: a call refused at admission was prepared all the same."""
    worker = await building.worker(Builder(WRONG, RIGHT))
    worker._boundaries.holding = False  # type: ignore[attr-defined]

    assert (await building.carry(worker)).startswith("refused")

    assert list(building.task._workspaces._root.iterdir()) == []
    assert building.task._bases == {}


async def test_a_builder_that_asks_keeps_and_publishes_nothing_and_builds_after_the_answer(
    building: Building, broker: Any, namespace: str
) -> None:
    """3B-S3-1.10, through the stream: a question in place of the work."""
    from relay.persistence.outbox import OutboxPublisher
    from relay.transport.streams import Streams

    streams = Streams(broker, prefix=namespace)
    publisher = OutboxPublisher(building.world.database, streams)
    asks = {
        "question": {
            "text": "Is the rate 20% for every amount?",
            "why": "the contract shows one amount only",
            "options": ["yes", "no"],
        }
    }
    builder = Builder(WRONG, RIGHT, replies=[asks, {"note": "applied the standard rate"}])
    worker = await building.worker(builder)
    world = building.world
    await publisher.publish_pending()

    assert await worker.serve_once(streams, PROJECT) == ["applied"]

    assert await world.column("SELECT state FROM assignments WHERE kind = 'build'") == ["asked"]
    assert await world.column("SELECT retained FROM invocations") == [None]
    assert building.at("refs/relay/attempts/vat-calculator/B1/1") == ""
    assert await world.column("SELECT count(*) FROM build_attempts") == [0]
    [question] = await world.decisions.open_questions(PROJECT)

    await world.answer(question.question_id, "yes")
    await world.reconfirm_everything()
    await publisher.publish_pending()
    # The Builder's worker keeps what it made and hands it on; the control plane
    # publishes and observes it.
    assert await worker.serve_once(streams, PROJECT) == ["handed_on"]
    await building.invocations.recover(PROJECT)

    assert await world.column(
        "SELECT state FROM assignments WHERE kind = 'build' ORDER BY created_at"
    ) == ["asked", "done"]
    assert "The Product Owner answered: yes" in builder.asked[1].briefing
    assert await world.state("B1") is BehaviourState.REVIEW


async def test_a_builds_call_may_cost_no_more_than_its_roles_profile_allows(
    building: Building,
) -> None:
    """3B-S4-1.10."""
    building.task._profile = Profile("claude-sonnet-5-5", budget_usd=Decimal(2))
    session = await WorkerSession.start(building.world.database, "builder")
    building.sessions.append(session)
    owed = await building.owed()
    grant = await building.world.claims.claim(
        session.principal, owed["assignment_id"], session.worker_id
    )

    call = await building.task.prepare(owed, grant, None)
    building.task.discard(call)

    assert call.budget_usd == Decimal(2)
