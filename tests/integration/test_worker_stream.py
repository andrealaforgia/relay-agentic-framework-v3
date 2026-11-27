"""A worker reading its role's commands. The stream is only the announcement: what is
owed is on record, so a command that comes twice buys nothing twice, and one whose
worker failed part-way is taken over."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.application.worker import Worker
from relay.domain.review import SubmissionRefused
from relay.domain.story import HumanAction
from relay.persistence.outbox import OutboxPublisher
from relay.providers.claude import ProviderResult, Request
from relay.transport.streams import Streams
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import PRODUCT_OWNER, VAT_APPLIES_STANDARD_RATE, run_green
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_review_task import (
    NO_USAGE_MESSAGE,
    NOTHING_FOUND,
    Reviewer,
    Reviewing,
    replying,
)
from tests.integration.trunk_world import PROJECT


class Served(Reviewing):
    streams: Streams
    publisher: OutboxPublisher


@pytest.fixture
async def served(
    database: AsyncConnectionPool,
    broker: redis.Redis,
    namespace: str,
    tmp_path: Path,
    runner_image: str,
) -> AsyncIterator[Served]:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE))
    base = await world.awaiting_builder("B1")
    served = Served(world, tmp_path)
    served.streams = Streams(broker, prefix=namespace)
    served.publisher = OutboxPublisher(database, served.streams)
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", world.attempts["B1"])).is_valid
    assert await world.reviews.publish_candidates() == 1
    await served.publisher.publish_pending()
    yield served
    await served.invocations.close()
    for session in served.sessions:
        await session.stop()


async def test_a_worker_carries_out_what_its_roles_stream_announces(served: Served) -> None:
    provider = Reviewer(NOTHING_FOUND)
    worker = await served.worker("qa", provider)

    assert await worker.serve_once(served.streams, PROJECT) == ["applied"]

    assert (await served.owed("qa"))["state"] == "done"
    assert (await served.owed("sentinel"))["state"] == "wanted"  # not this role's stream
    assert await worker.serve_once(served.streams, PROJECT) == []
    # Dealt with, so acknowledged: nobody finds it lying there to take over.
    another = await served.worker("qa", Reviewer())
    assert await another.serve_once(served.streams, PROJECT, abandoned_after_ms=0) == []


async def test_a_command_that_comes_again_buys_nothing_more(served: Served) -> None:
    provider = Reviewer(NOTHING_FOUND)
    worker = await served.worker("qa", provider)
    await worker.serve_once(served.streams, PROJECT)

    async with served.world.database.connection() as connection:
        await connection.execute("UPDATE outbox SET published_at = NULL")
    await served.publisher.publish_pending()
    again = await worker.serve_once(served.streams, PROJECT)

    assert len(again) == 1 and again[0].startswith(("refused", "finished"))
    assert len(provider.asked) == 1


async def test_a_command_whose_worker_failed_part_way_is_taken_over_by_another(
    served: Served,
) -> None:
    class Failing:
        async def invoke(self, *_: Any, **__: Any) -> Any:
            raise KeyboardInterrupt

    first = await served.worker("qa", Failing())
    with pytest.raises(KeyboardInterrupt):
        await first.serve_once(served.streams, PROJECT)
    await served.sessions[-1].stop()
    await served.invocations.recover(PROJECT)  # the call was cut short before launch
    provider = Reviewer(NOTHING_FOUND)
    second = await served.worker("qa", provider)

    assert await second.serve_once(served.streams, PROJECT) == []  # not yet abandoned
    assert await second.serve_once(served.streams, PROJECT, abandoned_after_ms=0) == ["applied"]

    assert len(provider.asked) == 1
    assert (await served.owed("qa"))["state"] == "done"


async def test_a_command_for_work_a_worker_does_not_do_is_set_aside(served: Served) -> None:
    worker = await served.worker("qa", Reviewer())
    worker._tasks = {}  # a worker started without the review task

    assert await worker.serve_once(served.streams, PROJECT) == ["not work this worker does"]
    assert (await served.owed("qa"))["state"] == "wanted"


async def test_a_review_adopted_under_another_grant_is_done_again_from_its_announcement(
    served: Served,
) -> None:
    """3B-S1-1.7 and 3B-S2-2.3: the first reviewer's call was given up on, a retry was
    allowed and the work claimed again, and then the first call's reply arrived. It was
    written from a checkout made under a grant that no longer holds the work, so
    nothing is submitted from it and nobody is blamed. The reviewer that holds the work
    is alive and has been told nothing: so the work is wanted again and announced again,
    and is done from that announcement, with nobody calling for it by hand."""
    await served.worker("qa", Reviewer())
    one = served.sessions[-1]
    owed = await served.owed("qa")
    grant = await served.world.claims.claim(one.principal, owed["assignment_id"], one.worker_id)
    call = await served.task.prepare(owed, grant, None)
    admission = await served.invocations.admit(one, owed["assignment_id"], grant, call.prepared)
    assert admission.invocation_id is not None
    await one.stop()
    await served.invocations.recover(PROJECT)
    await served.invocations.decide(
        HumanAction(PRODUCT_OWNER, "retry-the-review", str(admission.invocation_id), "retry", {})
    )
    provider = Reviewer(NOTHING_FOUND)
    second = await served.worker("qa", provider)
    two = served.sessions[-1]
    await served.world.claims.claim(two.principal, owed["assignment_id"], two.worker_id)
    # Every announcement so far has been read and dealt with by somebody.
    await served.publisher.publish_pending()
    stream = f"relay:{PROJECT}:cmd:qa"
    for delivery in await served.streams.read(stream, group="qa", consumer="somebody"):
        await served.streams.acknowledge(stream, "qa", delivery)
    late = replying({"findings": [NO_USAGE_MESSAGE], "carried": []})
    assert await served.invocations.record(admission.invocation_id, late) == "returned"

    await served.invocations.recover(PROJECT)  # the pass finds the result and applies it

    assert await served.world.column("SELECT state FROM invocations") == ["overtaken"]
    assert (await served.owed("qa"))["state"] == "wanted"
    assert await served.invocations.parked(PROJECT) == []
    await served.publisher.publish_pending()
    assert await second.serve_once(served.streams, PROJECT) == ["applied"]
    assert (await served.owed("qa"))["state"] == "done"
    assert await served.world.column("SELECT count(*) FROM findings") == [0]
    assert len(provider.asked) == 1


async def test_a_reviewer_whose_bounds_stop_holding_after_a_reply_it_could_not_use_goes_on_later(
    served: Served,
) -> None:
    """3B-S2-2.4, through the stream: the first reply is not a review, and the boundary's
    pass has gone by the time the next call would be made. The command is acknowledged,
    so the work must not be left with this worker: it is announced again, and done when
    the boundary holds."""
    worker: Worker

    class LosesItsPass(Reviewer):
        async def invoke(self, request: Request, **named: Any) -> ProviderResult:
            if not self.asked:
                worker._boundaries.holding = False  # type: ignore[attr-defined]
            return await super().invoke(request, **named)

    provider = LosesItsPass({"findings": "none", "carried": []}, NOTHING_FOUND)
    worker = await served.worker("qa", provider)

    [left] = await worker.serve_once(served.streams, PROJECT)

    assert left.startswith("refused: the bounds of this run")
    assert (await served.owed("qa"))["state"] == "wanted"
    assert await worker.serve_once(served.streams, PROJECT) == []
    worker._boundaries.holding = True  # type: ignore[attr-defined]
    await served.publisher.publish_pending()
    assert await worker.serve_once(served.streams, PROJECT) == ["applied"]
    assert await served.world.column("SELECT state FROM invocations ORDER BY attempt") == [
        "rejected",
        "applied",
    ]
    assert "findings of delivered of the reply is not a array" in provider.asked[1].briefing


async def test_a_review_that_could_not_be_prepared_is_taken_up_when_the_repository_answers(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3B-S2-3.3: the reviewer has claimed the work when its repository does not answer.
    It stays alive. The work must not stay with it: when the repository answers again
    the work is announced, and whoever reads the stream does it. Nothing was paid for
    in between."""
    first_provider, second_provider = Reviewer(), Reviewer(NOTHING_FOUND)
    first = await served.worker("qa", first_provider)
    real = served.world.reviews.prepare_review

    async def unreachable(*_: Any) -> Any:
        raise SubmissionRefused("the repository cannot be reached: it did not answer")

    monkeypatch.setattr(served.world.reviews, "prepare_review", unreachable)

    [left] = await first.serve_once(served.streams, PROJECT)

    assert left == "refused: the repository cannot be reached: it did not answer"
    assert (await served.owed("qa"))["state"] == "wanted"
    monkeypatch.setattr(served.world.reviews, "prepare_review", real)
    second = await served.worker("qa", second_provider)
    await served.publisher.publish_pending()
    assert await second.serve_once(served.streams, PROJECT) == ["applied"]
    assert (len(first_provider.asked), len(second_provider.asked)) == (0, 1)


WHICH_RATE = {
    "question": {
        "text": "Should a negative amount be refused?",
        "why": "the candidate prints a negative gross amount, and no criterion says",
        "options": ["refuse it", "print it"],
    },
    "assumptions": ["amounts are whole numbers"],
}


async def test_a_reviewer_that_asks_is_answered_and_reviews_afresh_under_a_successor(
    served: Served,
) -> None:
    """3B-S3-1.10: a product question from a reviewer reaches the Product Owner, and
    the review is owed again on the same task, from a checkout of its own."""
    provider = Reviewer(WHICH_RATE, NOTHING_FOUND)
    worker = await served.worker("qa", provider)
    world = served.world

    assert await worker.serve_once(served.streams, PROJECT) == ["applied"]

    first = await served.owed("qa")
    assert first["state"] == "asked"
    [question] = await world.decisions.open_questions(PROJECT)
    assert question.text == "Should a negative amount be refused?"
    assert await world.column("SELECT text FROM decisions") == ["amounts are whole numbers"]
    assert await world.column("SELECT count(*) FROM review_submissions") == [0]

    await world.answer(question.question_id, "print it")
    await world.reconfirm_everything()
    await served.publisher.publish_pending()
    assert await worker.serve_once(served.streams, PROJECT) == ["applied"]

    successor = await served.owed("qa")
    assert (successor["succeeds"], successor["state"]) == (first["assignment_id"], "done")
    assert "The Product Owner answered: print it" in provider.asked[1].briefing
    assert await world.column(
        "SELECT count(DISTINCT c.grant_id) FROM review_checkouts c JOIN review_tasks t "
        "USING (task_id) WHERE t.role = 'qa'"
    ) == [2]
    assert provider.asked[0].workdir != provider.asked[1].workdir
    assert await world.column("SELECT count(*) FROM review_submissions") == [1]
